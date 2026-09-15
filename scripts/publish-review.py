#!/usr/bin/env python3
"""publish step: post a verified review to the right PR, or post nothing.

The publisher does not interpret the review. It re-validates the normalized
result against the same deterministic rules the normalizer used, confirms that
the result belongs to the snapshot this run prepared, re-checks the PR is still
open at the same head and base SHAs, renders a fixed Markdown template with all
model text neutralized, and then updates the comment carrying this snapshot's
marker or creates a new one.

Every failure path stops without posting (exit 2). Nothing in the result can
choose the repository, the PR, the API endpoint, or the comment to edit: those
come from the workflow context and from the prepare job's outputs.

Standard library only. No Claude secret is read here.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import bundle as bundle_mod  # noqa: E402
from lib import github as gh  # noqa: E402
from lib import limits as limits_mod  # noqa: E402
from lib import render as render_mod  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_STOP = 2

DEFAULT_API_URL = "https://api.github.com"
TOKEN_ENV = "GITHUB_TOKEN"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# Values that are only ever displayed. Anything else is replaced, never shown.
_DISPLAY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,63}$")

SEVERITY_ORDER = {name: index for index, name in enumerate(limits_mod.SEVERITIES)}


class PublishError(Exception):
    """Deterministic stop: nothing may be posted for this run."""


def log(message: str) -> None:
    sys.stderr.write(f"publish-review: {message}\n")


# -- result verification ------------------------------------------------------


def _display(value: object) -> str | None:
    if isinstance(value, str) and _DISPLAY_RE.fullmatch(value):
        return value
    return None


def _require_exact_keys(payload: dict, expected: tuple[str, ...], field: str) -> None:
    unknown = sorted(set(payload) - set(expected))
    if unknown:
        raise PublishError(f"{field} has unknown fields: {','.join(unknown)}")
    missing = sorted(set(expected) - set(payload))
    if missing:
        raise PublishError(f"{field} is missing fields: {','.join(missing)}")


def _require_display(value: object, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    clean = _display(value)
    if clean is None:
        raise PublishError(f"{field} is not a safe display value")
    return clean


def _require_nonnegative_int(value: object, field: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PublishError(f"{field} is not a non-negative integer")
    return value


def _require_sha256(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise PublishError(f"{field} is not a 64-hex digest")
    return value


def _require_text(value: object, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise PublishError(f"{field} is not a string")
    text = bundle_mod.sanitize_text(value, max_chars).strip()
    if not text:
        raise PublishError(f"{field} is empty")
    return text


def _require_enum(value: object, field: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise PublishError(f"{field} is not one of {'/'.join(allowed)}")
    return str(value)


def _validate_finding(
    item: object,
    limits: limits_mod.Limits,
    reviewable_path_hashes: frozenset[str],
) -> dict:
    if not isinstance(item, dict):
        raise PublishError("finding is not an object")
    unknown = sorted(set(item) - set(limits_mod.REQUIRED_FINDING_KEYS) - set(limits_mod.OPTIONAL_FINDING_KEYS))
    if unknown:
        raise PublishError(f"finding has unknown fields: {','.join(unknown)}")
    for key in limits_mod.REQUIRED_FINDING_KEYS:
        if key not in item:
            raise PublishError(f"finding is missing {key}")
    path = item["path"]
    if not isinstance(path, str) or not path or len(path) > limits.max_finding_path_chars:
        raise PublishError("finding path is invalid")
    reason = limits_mod.forbidden_path_reason(path)
    if reason is not None:
        raise PublishError(f"finding path is not publishable: {reason}")
    path_hash = bundle_mod.sha256_hex(path.encode("utf-8"))
    if path_hash not in reviewable_path_hashes:
        raise PublishError("finding path is not a changed reviewable file in this snapshot")
    finding = {
        "title": _require_text(item["title"], "finding title", limits.max_finding_title_chars),
        "detail": _require_text(item["detail"], "finding detail", limits.max_finding_detail_chars),
        "severity": _require_enum(item["severity"], "finding severity", limits_mod.SEVERITIES),
        "confidence": _require_enum(item["confidence"], "finding confidence", limits_mod.CONFIDENCES),
        "category": _require_enum(item["category"], "finding category", limits_mod.CATEGORIES),
        "path": path,
    }
    line = item.get("line")
    if line is not None:
        if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= limits_mod.MAX_FINDING_LINE:
            raise PublishError("finding line is not a positive integer within range")
        finding["line"] = line
    return finding


def _validate_review(
    payload: object,
    limits: limits_mod.Limits,
    reviewable_path_hashes: frozenset[str],
) -> dict:
    if not isinstance(payload, dict):
        raise PublishError("review is not an object")
    _require_exact_keys(payload, limits_mod.RESULT_KEYS, "review")
    if payload.get("schema_version") != limits_mod.RESULT_SCHEMA_VERSION:
        raise PublishError("unsupported review schema version")
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise PublishError("findings is not an array")
    if len(findings) > limits.max_findings:
        raise PublishError(f"findings exceed the limit: {len(findings)} > {limits.max_findings}")
    limitations = payload.get("limitations")
    if not isinstance(limitations, list) or len(limitations) > limits.max_limitations:
        raise PublishError("limitations is not an array within the limit")
    clean = [_validate_finding(item, limits, reviewable_path_hashes) for item in findings]
    clean.sort(key=lambda f: SEVERITY_ORDER[f["severity"]])
    return {
        "schema_version": limits_mod.RESULT_SCHEMA_VERSION,
        "summary": _require_text(payload.get("summary"), "summary", limits.max_summary_chars),
        "findings": clean,
        "limitations": [
            _require_text(item, "limitation", limits.max_limitation_chars) for item in limitations
        ],
    }


def _redact_review(review: dict) -> int:
    """Second, independent redaction pass over everything the model wrote."""
    total = 0

    def scrub(text: str) -> str:
        nonlocal total
        cleaned, hits = bundle_mod.redact_secrets(text)
        total += hits
        return cleaned

    review["summary"] = scrub(review["summary"])
    review["limitations"] = [scrub(item) for item in review["limitations"]]
    for finding in review["findings"]:
        finding["title"] = scrub(finding["title"])
        finding["detail"] = scrub(finding["detail"])
    return total


def _validate_snapshot(
    payload: object,
    *,
    repository: str,
    pr_number: int,
    limits: limits_mod.Limits,
) -> dict:
    if not isinstance(payload, dict):
        raise PublishError("snapshot is not an object")
    _require_exact_keys(payload, limits_mod.SNAPSHOT_KEYS, "snapshot")
    owner, name = gh.validate_repository(str(payload.get("repository")))
    if f"{owner}/{name}".lower() != repository.lower():
        raise PublishError("result was produced for a different repository")
    if payload.get("pr_number") != pr_number:
        raise PublishError("result was produced for a different pull request")
    snapshot = {
        "repository": f"{owner}/{name}",
        "pr_number": pr_number,
        "base_sha": gh.validate_sha(payload.get("base_sha"), "result base sha"),
        "head_sha": gh.validate_sha(payload.get("head_sha"), "result head sha"),
        "merge_base_sha": gh.validate_sha(payload.get("merge_base_sha"), "result merge-base sha"),
        "diff_sha256": _require_sha256(payload.get("diff_sha256"), "result diff sha256"),
        "snapshot_id": _require_sha256(payload.get("snapshot_id"), "result snapshot id"),
        "policy_commit_sha": None,
        "policy_blob_sha": None,
        "reviewable_path_hashes": [],
    }
    for key in ("policy_commit_sha", "policy_blob_sha"):
        value = payload.get(key)
        if value is not None:
            snapshot[key] = gh.validate_sha(value, f"result {key}")
    path_hashes = payload["reviewable_path_hashes"]
    if not isinstance(path_hashes, list) or len(path_hashes) > limits.max_changed_files:
        raise PublishError("snapshot.reviewable_path_hashes is not an array within the limit")
    clean_hashes = [
        _require_sha256(value, "reviewable path hash") for value in path_hashes
    ]
    if clean_hashes != sorted(set(clean_hashes)):
        raise PublishError("snapshot.reviewable_path_hashes is not sorted and unique")
    snapshot["reviewable_path_hashes"] = clean_hashes
    return snapshot


def _validate_run(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise PublishError("run is not an object")
    _require_exact_keys(payload, limits_mod.RUN_KEYS, "run")
    if payload.get("tools_enabled") is not False:
        raise PublishError("result was not produced with every tool disabled")
    if payload.get("provider") != limits_mod.REVIEW_PROVIDER:
        raise PublishError("result was produced by an unknown provider")
    return {
        "provider": limits_mod.REVIEW_PROVIDER,
        "cli_version": _require_display(payload["cli_version"], "run.cli_version"),
        "model_requested": _require_display(payload["model_requested"], "run.model_requested"),
        "model_reported": _require_display(
            payload["model_reported"], "run.model_reported", optional=True
        ),
        "effort": _require_display(payload["effort"], "run.effort"),
        "run_id": _require_display(payload["run_id"], "run.run_id", optional=True),
        "tools_enabled": False,
        "num_turns": _require_nonnegative_int(
            payload["num_turns"], "run.num_turns", optional=True
        ),
        "duration_ms": _require_nonnegative_int(payload["duration_ms"], "run.duration_ms"),
    }


def _validate_normalization(payload: object, limits: limits_mod.Limits) -> dict:
    if not isinstance(payload, dict):
        raise PublishError("normalization is not an object")
    _require_exact_keys(payload, limits_mod.NORMALIZATION_KEYS, "normalization")
    dropped = payload["dropped_findings"]
    if not isinstance(dropped, list) or len(dropped) > limits.max_findings + 1:
        raise PublishError("normalization.dropped_findings is not an array within the limit")
    clean_dropped = []
    for item in dropped:
        if not isinstance(item, dict):
            raise PublishError("normalization dropped finding is not an object")
        _require_exact_keys(item, limits_mod.DROPPED_FINDING_KEYS, "normalization dropped finding")
        clean_dropped.append(
            {
                "index": _require_nonnegative_int(item["index"], "dropped finding index"),
                "reason": _require_text(
                    item["reason"], "dropped finding reason", limits.max_finding_detail_chars
                ),
            }
        )
    redactions = _require_nonnegative_int(payload["redactions"], "normalization.redactions")
    excluded_files = _require_nonnegative_int(
        payload["excluded_files"], "normalization.excluded_files"
    )
    if redactions > limits.max_result_bytes:
        raise PublishError("normalization.redactions exceeds the limit")
    if excluded_files > limits.max_changed_files:
        raise PublishError("normalization.excluded_files exceeds the limit")
    return {
        "dropped_findings": clean_dropped,
        "redactions": redactions,
        "excluded_files": excluded_files,
    }


def load_result(
    path: Path, *, repository: str, pr_number: int, limits: limits_mod.Limits
) -> dict:
    """Read and re-validate the normalized result artifact."""
    try:
        data = Path(path).read_bytes()
    except OSError as err:
        raise PublishError(f"cannot read the result artifact: {err.__class__.__name__}") from None
    limits_mod.check_limit("result artifact size", len(data), limits.max_result_bytes)
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PublishError("result artifact is not valid JSON") from None
    if not isinstance(document, dict):
        raise PublishError("result artifact is not an object")
    _require_exact_keys(document, limits_mod.NORMALIZED_RESULT_KEYS, "result artifact")
    if document.get("result_schema_version") != limits_mod.RESULT_SCHEMA_VERSION:
        raise PublishError("unsupported result schema version")
    if document.get("framework_version") != limits_mod.FRAMEWORK_VERSION:
        raise PublishError("result was produced by a different framework version")
    snapshot = _validate_snapshot(
        document.get("snapshot"), repository=repository, pr_number=pr_number, limits=limits
    )
    run = _validate_run(document.get("run"))
    normalization = _validate_normalization(document.get("normalization"), limits)
    review = _validate_review(
        document.get("review"), limits, frozenset(snapshot["reviewable_path_hashes"])
    )
    redacted = _redact_review(review)
    if redacted:
        log(f"redacted {redacted} credential-shaped value(s) before rendering")
    normalization["redactions"] += redacted
    return {
        "result_schema_version": limits_mod.RESULT_SCHEMA_VERSION,
        "framework_version": limits_mod.FRAMEWORK_VERSION,
        "snapshot": snapshot,
        "run": run,
        "normalization": normalization,
        "review": review,
    }


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


def find_existing_comment(comments: list[dict], snapshot_id: str) -> dict | None:
    """Return our own comment for this snapshot, if one is already there.

    A comment only qualifies when the marker is its first line *and* its author
    is a bot. A PR author can copy the marker text into a comment, but cannot
    make that comment appear to come from a bot account, so a forged marker
    cannot redirect the update (and GitHub would reject editing it anyway).
    """
    matches = []
    for comment in comments:
        if render_mod.read_marker(comment.get("body")) != snapshot_id:
            continue
        user = comment.get("user")
        if not isinstance(user, dict) or user.get("type") != "Bot":
            log("ignoring a comment that carries our marker but was not written by a bot")
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
    log(f"result verified: snapshot={snapshot['snapshot_id'][:16]} findings={len(document['review']['findings'])}")

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
    existing = find_existing_comment(comments, snapshot["snapshot_id"])
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
    parser.add_argument("--repository", required=True, help="owner/name of the base repository")
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--result-file", required=True, type=Path)
    parser.add_argument("--expected-snapshot-id", default="", help="snapshot_id emitted by the prepare job")
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
            token=os.environ.get(TOKEN_ENV) or None,
            api_url=args.api_url,
        )
    except (
        PublishError,
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
