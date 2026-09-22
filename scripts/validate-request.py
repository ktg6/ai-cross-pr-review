#!/usr/bin/env python3
"""validate_request step: turn workflow inputs into a validated review request.

This is the single entry point for operator-supplied values. Every later job
reads the outputs of this step, never the raw workflow inputs, so a second entry
point cannot bypass the allowlists (ADR-0005, ADR-0007).

Nothing here talks to the network, to GitHub, or to a model. The step holds no
credentials.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import github as gh  # noqa: E402
from lib import limits as limits_mod  # noqa: E402
from lib import models as models_mod  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_STOP = 2

DEFAULT_SERVER_URL = "https://github.com"
DEFAULT_POLICY_PATH = ".github/ai-review.md"

_PR_URL_RE = re.compile(r"^/([^/]+)/([^/]+)/pull/([0-9]+)(?:/[^?#]*)?$")
# Control characters and whitespace have no place in a pasted URL. urlsplit would
# silently drop some of them, so refuse the input instead of parsing a lookalike.
_UNSAFE_URL_CHARS = re.compile(r"[\x00-\x20\x7f]")


class RequestError(Exception):
    """Deterministic stop: the request cannot be accepted as given."""


def log(message: str) -> None:
    sys.stderr.write(f"validate-request: {message}\n")


def parse_pull_request(value: str, repository: str, server_url: str) -> int:
    """Accept either a bare PR number or a PR URL on the expected server.

    A URL must point at the same repository as ``repository``; a mismatch is a
    stop, never a silent redirect to whatever the URL names.
    """
    if not isinstance(value, str):
        raise RequestError("pull_request is not a string")
    text = value.strip()
    if not text:
        raise RequestError("pull_request is empty")
    if not text.lower().startswith(("http://", "https://")):
        return gh.validate_pr_number(text)

    if _UNSAFE_URL_CHARS.search(text):
        raise RequestError("pull request URL contains control characters or whitespace")
    expected = gh.validate_https_url(server_url, "server_url")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme != "https":
        raise RequestError("pull request URL must use https")
    if parsed.netloc.lower() != urllib.parse.urlsplit(expected).netloc.lower():
        raise RequestError("pull request URL host does not match the configured server")
    match = _PR_URL_RE.match(parsed.path)
    if match is None:
        raise RequestError("pull request URL is not a /owner/name/pull/N URL")
    owner, name, number = match.groups()
    url_repo = f"{owner}/{name}"
    try:
        url_owner, url_name = gh.validate_repository(url_repo)
    except gh.ValidationError as err:
        raise RequestError(f"pull request URL repository is invalid: {err}") from None
    if f"{url_owner}/{url_name}".lower() != repository.lower():
        raise RequestError("pull request URL does not match the requested repository")
    return gh.validate_pr_number(number)


def build_request(
    *,
    repository: str,
    pull_request: str,
    output_mode: str,
    claude_model: str,
    codex_model: str,
    claude_effort: str,
    codex_effort: str,
    policy_path: str,
    server_url: str = DEFAULT_SERVER_URL,
) -> dict:
    owner, name = gh.validate_repository(repository)
    canonical_repo = f"{owner}/{name}"
    number = parse_pull_request(pull_request, canonical_repo, server_url)

    if not isinstance(policy_path, str) or not policy_path:
        raise RequestError("policy path is empty")
    if policy_path.startswith("/") or ".." in policy_path.split("/"):
        raise RequestError("policy path must be a relative path without '..'")
    if limits_mod.forbidden_path_reason(policy_path):
        raise RequestError("policy path is not allowed")
    if len(policy_path) > 256:
        raise RequestError("policy path is too long")

    return {
        "repository": canonical_repo,
        "pr_number": number,
        "output_mode": models_mod.validate_output_mode(output_mode),
        "claude_model": models_mod.validate_claude_model(claude_model),
        "codex_model": models_mod.validate_codex_model(codex_model),
        "claude_effort": models_mod.validate_claude_effort(claude_effort),
        "codex_effort": models_mod.validate_codex_effort(codex_effort),
        "policy_path": policy_path,
    }


def write_github_output(path: str, values: dict) -> None:
    lines = []
    for key, value in values.items():
        text = str(value)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\n" in text or "\r" in text:
            raise RequestError(f"refusing to write unsafe GITHUB_OUTPUT entry {key}")
        lines.append(f"{key}={text}\n")
    with open(path, "a", encoding="utf-8") as fh:
        fh.writelines(lines)


def validate_request(args: argparse.Namespace, run_env: dict | None = None) -> dict:
    run_env = os.environ if run_env is None else run_env
    request = build_request(
        repository=args.repository,
        pull_request=args.pull_request,
        output_mode=args.output_mode,
        claude_model=args.claude_model,
        codex_model=args.codex_model,
        claude_effort=args.claude_effort,
        codex_effort=args.codex_effort,
        policy_path=args.policy_path,
        server_url=args.server_url,
    )
    log(
        "accepted: repository={r} pr={n} mode={m} claude={c} codex={x}".format(
            r=request["repository"],
            n=request["pr_number"],
            m=request["output_mode"],
            c=request["claude_model"],
            x=request["codex_model"],
        )
    )
    if args.output_file:
        Path(args.output_file).write_text(
            json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    github_output = run_env.get("GITHUB_OUTPUT")
    if github_output:
        write_github_output(github_output, request)
    return request


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repository", required=True, help="owner/name of the target repository")
    parser.add_argument("--pull-request", required=True, help="PR number or PR URL")
    parser.add_argument("--output-mode", default=models_mod.DEFAULT_OUTPUT_MODE)
    parser.add_argument("--claude-model", default=models_mod.DEFAULT_CLAUDE_MODEL)
    parser.add_argument("--codex-model", default=models_mod.DEFAULT_CODEX_MODEL)
    parser.add_argument("--claude-effort", default=models_mod.DEFAULT_CLAUDE_EFFORT)
    parser.add_argument("--codex-effort", default=models_mod.DEFAULT_CODEX_EFFORT)
    parser.add_argument("--policy-path", default=DEFAULT_POLICY_PATH)
    parser.add_argument("--server-url", default=DEFAULT_SERVER_URL)
    parser.add_argument("--output-file", default="", help="optional path for the validated request JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        validate_request(args)
    except (RequestError, models_mod.ModelNotAllowed, gh.ValidationError) as err:
        log(f"stop: {err}")
        return EXIT_STOP
    except Exception as err:  # noqa: BLE001 - last-resort guard, message only
        log(f"unexpected error: {err.__class__.__name__}")
        return EXIT_UNEXPECTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
