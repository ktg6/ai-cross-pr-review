#!/usr/bin/env python3
"""comment step: post a verified cross-review to the right PR, or post nothing.

The publisher does not interpret the review. It re-validates the final document
with the same deterministic rules the finalizer used, confirms that the document
belongs to the snapshot this run prepared and to the pull request this run was
asked to review, refuses anything that is not publishable, re-checks that the PR
is still open at the same head and base SHAs, renders a fixed Markdown template
with all model text neutralized, and then updates the comment carrying this
snapshot's marker or creates a new one.

Every failure path stops without posting (exit 2). Nothing in the document can
choose the repository, the PR, the API endpoint, or the comment to edit: those
come from the validated request and from the prepare job's outputs.

This step receives only the comment token. It holds no Claude or OpenAI secret.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import github as gh  # noqa: E402
from lib import limits as limits_mod  # noqa: E402
from lib import render as render_mod  # noqa: E402
from lib import result as result_mod  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_STOP = 2

DEFAULT_API_URL = "https://api.github.com"
TOKEN_ENV = "GITHUB_TOKEN"
PUBLISHING_MODE = "pr_comment"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class PublishError(Exception):
    """Deterministic stop: nothing may be posted for this run."""


def log(message: str) -> None:
    sys.stderr.write(f"publish-review: {message}\n")


# -- result verification ------------------------------------------------------


def load_result(
    path: Path, *, repository: str, pr_number: int, limits: limits_mod.Limits
) -> dict:
    """Read and re-validate the final document produced by the finalize job."""
    try:
        data = Path(path).read_bytes()
    except OSError as err:
        raise PublishError(f"cannot read the result artifact: {err.__class__.__name__}") from None
    limits_mod.check_limit("result artifact size", len(data), limits.max_final_result_bytes)
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PublishError("result artifact is not valid JSON") from None

    document = result_mod.validate_final_document(document, limits)

    owner, name = gh.validate_repository(document["snapshot"]["repository"])
    if f"{owner}/{name}".lower() != repository.lower():
        raise PublishError("result was produced for a different repository")
    if document["snapshot"]["pr_number"] != pr_number:
        raise PublishError("result was produced for a different pull request")

    # A second, independent redaction pass over everything a model wrote.
    redacted = result_mod.redact_document(document)
    if redacted:
        log(f"redacted {redacted} credential-shaped value(s) before rendering")
        document["review"]["redactions"] += redacted
    return document


def assert_publishable(document: dict, expected_output_mode: str) -> None:
    """Refuse to post incomplete, failed, or summary-only results."""
    if expected_output_mode != PUBLISHING_MODE:
        raise PublishError("publishing is only allowed in pr_comment mode")
    if document["request"]["output_mode"] != PUBLISHING_MODE:
        raise PublishError("result was produced for a different output mode")
    if not document["publishable"]:
        stages = document["stages"]
        raise PublishError(
            "result is not publishable (claude={c}, codex={x})".format(
                c=stages["claude"]["status"], x=stages["codex"]["status"]
            )
        )


# -- stale detection ----------------------------------------------------------


def assert_snapshot_is_current(pr: dict | None, snapshot: dict) -> None:
    """Refuse to post when the PR moved or closed after the snapshot was taken."""
    if pr is None:
        raise PublishError("pull request no longer exists")
    if pr.get("state") != "open" or pr.get("merged") is True:
        raise PublishError("pull request is no longer open")
    try:
        head_sha = gh.validate_sha(pr["head"]["sha"], "current head sha")
        base_sha = gh.validate_sha(pr["base"]["sha"], "current base sha")
    except (KeyError, TypeError):
        raise PublishError("pull request payload is missing head/base sha") from None
    if head_sha != snapshot["head_sha"]:
        raise PublishError("pull request head moved since the review; result is stale")
    if base_sha != snapshot["base_sha"]:
        raise PublishError("pull request base moved since the review; result is stale")


# -- comment selection --------------------------------------------------------


def find_existing_comment(
    comments: list[dict], snapshot_id: str, *, authenticated_login: str | None = None
) -> dict | None:
    """Return our own comment for this snapshot, if one is already there.

    Bot comments remain supported for GitHub Actions identities. For a
    fine-grained PAT, GitHub reports the author as ``User``; that case is only
    accepted when its login matches the identity returned by ``/user``.
    """
    matches = []
    for comment in comments:
        if render_mod.read_marker(comment.get("body")) != snapshot_id:
            continue
        user = comment.get("user")
        is_bot = isinstance(user, dict) and user.get("type") == "Bot"
        is_authenticated_user = (
            isinstance(user, dict)
            and user.get("type") == "User"
            and isinstance(authenticated_login, str)
            and isinstance(user.get("login"), str)
            and user["login"].casefold() == authenticated_login.casefold()
        )
        if not is_bot and not is_authenticated_user:
            log("ignoring a comment that carries our marker but was not written by the publisher")
            continue
        try:
            matches.append((gh.validate_comment_id(comment.get("id")), comment))
        except gh.ValidationError:
            continue
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    return matches[0][1]


def write_github_output(path: str, values: dict[str, str]) -> None:
    lines = []
    for key, value in values.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\n" in value or "\r" in value:
            raise PublishError(f"refusing to write unsafe GITHUB_OUTPUT entry {key}")
        lines.append(f"{key}={value}\n")
    with open(path, "a", encoding="utf-8") as fh:
        fh.writelines(lines)


# -- orchestration ------------------------------------------------------------


def publish(
    *,
    repository: str,
    pr_number: str | int,
    result_file: Path,
    expected_snapshot_id: str | None,
    output_mode: str = PUBLISHING_MODE,
    token: str | None = None,
    api_url: str = DEFAULT_API_URL,
    limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS,
    transport: gh.Transport | None = None,
    sleep=None,
    run_env: dict | None = None,
) -> dict:
    run_env = os.environ if run_env is None else run_env
    owner, name = gh.validate_repository(repository)
    number = gh.validate_pr_number(pr_number)
    if not token:
        raise PublishError(f"{TOKEN_ENV} is not set")
    if not isinstance(expected_snapshot_id, str) or not _SHA256_RE.fullmatch(expected_snapshot_id):
        raise PublishError("expected snapshot id is not a 64-hex digest")

    document = load_result(
        Path(result_file), repository=f"{owner}/{name}", pr_number=number, limits=limits
    )
    snapshot = document["snapshot"]
    if snapshot["snapshot_id"] != expected_snapshot_id:
        # The prepare job published this ID as a job output, outside the
        # artifact, so a forged artifact cannot match it.
        raise PublishError("result does not belong to the snapshot prepared by this run")
    assert_publishable(document, output_mode)
    log(
        "result verified: snapshot={s} adopted={a} added={b} deferred={d}".format(
            s=snapshot["snapshot_id"][:16],
            a=len(document["review"]["adopted"]),
            b=len(document["review"]["added"]),
            d=len(document["review"]["deferred"]),
        )
    )

    client_kwargs = {
        "transport": transport,
        "max_response_bytes": limits.max_api_response_bytes,
        "retry_attempts": limits.publish_retry_attempts,
        "retry_delay_seconds": limits.publish_retry_delay_seconds,
    }
    if sleep is not None:
        client_kwargs["sleep"] = sleep
    client = gh.GitHubClient(api_url, token, **client_kwargs)

    assert_snapshot_is_current(client.get_pull(owner, name, number), snapshot)
    body = render_mod.render_comment(document, limits)

    comments = client.list_issue_comments(owner, name, number, max_pages=limits.max_comment_pages)
    authenticated_login = client.get_authenticated_login()
    existing = find_existing_comment(
        comments, snapshot["snapshot_id"], authenticated_login=authenticated_login
    )
    # Comment pagination can take long enough for the PR to move. Close the
    # check/use window as much as the GitHub API permits by checking again
    # immediately before the only write in this process.
    assert_snapshot_is_current(client.get_pull(owner, name, number), snapshot)
    if existing is None:
        posted = client.create_issue_comment(owner, name, number, body)
        action = "created"
    else:
        posted = client.update_issue_comment(owner, name, gh.validate_comment_id(existing["id"]), body)
        action = "updated"

    comment_id = str(gh.validate_comment_id(posted.get("id")))
    log(f"comment {action}: id={comment_id} bytes={len(body.encode('utf-8'))}")

    github_output = run_env.get("GITHUB_OUTPUT")
    if github_output:
        write_github_output(
            github_output,
            {
                "comment_action": action,
                "comment_id": comment_id,
                "head_sha": snapshot["head_sha"],
                "snapshot_id": snapshot["snapshot_id"],
            },
        )
    return {"action": action, "comment_id": comment_id, "body": body, "document": document}


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repository", required=True, help="owner/name of the target repository")
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--result-file", required=True, type=Path)
    parser.add_argument("--expected-snapshot-id", default="", help="snapshot_id emitted by the prepare job")
    parser.add_argument("--output-mode", default=PUBLISHING_MODE)
    parser.add_argument("--api-url", default=DEFAULT_API_URL)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        publish(
            repository=args.repository,
            pr_number=args.pr_number,
            result_file=args.result_file,
            expected_snapshot_id=args.expected_snapshot_id,
            output_mode=args.output_mode,
            token=os.environ.get(TOKEN_ENV) or None,
            api_url=args.api_url,
        )
    except (
        PublishError,
        result_mod.ResultError,
        render_mod.RenderError,
        gh.ValidationError,
        gh.GitHubError,
        limits_mod.LimitExceeded,
    ) as err:
        log(f"stop: {err}")
        return EXIT_STOP
    except Exception as err:  # noqa: BLE001 - last-resort guard, message only
        log(f"unexpected error: {err.__class__.__name__}")
        return EXIT_UNEXPECTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
