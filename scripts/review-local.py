#!/usr/bin/env python3
"""local CLI: run the two-stage review on a developer machine and save the result.

Runs the same steps as the central workflow, in the same order and with the
same failure semantics, by calling the step scripts' functions in-process:

    validate -> prepare -> claude review -> normalize
    -> codex verification -> normalize -> finalize -> report

The result (``final-review.json`` and ``review-summary.md``) is written to a
local directory and the summary is printed to stdout. Nothing is posted: this
entry point has no GitHub write path. The publisher is never imported, and the
GitHub transport handed to prepare refuses every method other than GET
(ADR-0011).

Credentials are read from the environment only, never from argv, and each one
is passed to the single stage that needs it:

    AI_REVIEW_GITHUB_TOKEN   prepare (optional; unset = unauthenticated reads)
    CLAUDE_CODE_OAUTH_TOKEN  Claude primary review
    OPENAI_API_KEY           Codex verification

The bundle, git scratch space, raw provider responses and normalized stage
results live in a private temporary directory that is removed on exit.

Exit status: 0 when the final result is complete (``publishable``), 2 when a
stage failed or a check did not pass, 1 on an unexpected error.

Standard library only. No PR code is checked out or executed.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import bundle as bundle_mod  # noqa: E402
from lib import diff as diff_mod  # noqa: E402
from lib import github as gh  # noqa: E402
from lib import limits as limits_mod  # noqa: E402
from lib import models as models_mod  # noqa: E402
from lib import openai_api  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_STOP = 2

GITHUB_TOKEN_ENV = "AI_REVIEW_GITHUB_TOKEN"
CLAUDE_TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"
OPENAI_KEY_ENV = "OPENAI_API_KEY"

# The local path never posts. The final document still records a valid mode.
OUTPUT_MODE = "summary_only"

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


class LocalError(Exception):
    """Deterministic stop before or outside the review stages."""


def log(message: str) -> None:
    sys.stderr.write(f"review-local: {message}\n")


def _load(name: str, filename: str):
    """Import a hyphenated step script, reusing an already loaded copy."""
    existing = sys.modules.get(name)
    if existing is not None and Path(getattr(existing, "__file__", "")).resolve() == SCRIPTS / filename:
        return existing
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


validate_mod = _load("validate_request", "validate-request.py")
prepare_mod = _load("prepare_review", "prepare-review.py")
run_review_mod = _load("run_review", "run-review.py")
normalize_review_mod = _load("normalize_review", "normalize-review.py")
run_codex_mod = _load("run_codex_review", "run-codex-review.py")
normalize_codex_mod = _load("normalize_codex_review", "normalize-codex-review.py")
finalize_mod = _load("finalize_review", "finalize-review.py")
report_mod = _load("report_summary", "report-summary.py")

# Errors whose message is safe to show, per stage. Anything else is reported by
# class name only, like the step scripts' last-resort guards.
_CLAUDE_ERRORS = (
    run_review_mod.ReviewError,
    normalize_review_mod.NormalizeError,
    bundle_mod.BundleError,
    limits_mod.LimitExceeded,
    models_mod.ModelNotAllowed,
)
_CODEX_ERRORS = (
    run_codex_mod.CodexError,
    normalize_codex_mod.NormalizeError,
    bundle_mod.BundleError,
    limits_mod.LimitExceeded,
    models_mod.ModelNotAllowed,
    openai_api.OpenAIError,
)
_PREPARE_ERRORS = (
    prepare_mod.PrepareError,
    gh.ValidationError,
    gh.GitHubError,
    diff_mod.GitError,
    limits_mod.LimitExceeded,
)


def _describe(err: Exception, known: tuple[type[Exception], ...]) -> str:
    return str(err) if isinstance(err, known) else f"unexpected error: {err.__class__.__name__}"


def check_output_dir(path: Path) -> None:
    """An existing output directory must be empty and private."""
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise LocalError("output directory exists and is not a directory")
    if path.is_dir():
        if stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise LocalError("output directory must not be accessible by group or other users")
        if any(path.iterdir()):
            raise LocalError("output directory is not empty")


def resolve_claude_bin(value: str) -> str:
    """Resolve a bare command name on PATH; the digest check needs a real file."""
    if os.sep in value:
        return value
    found = shutil.which(value)
    if found is None:
        raise LocalError(f"Claude Code CLI not found on PATH: {value}")
    return found


def _prepare_snapshot(manifest: dict) -> dict:
    """The values the workflow copies from prepare's job outputs."""
    return {
        "snapshot_id": manifest["snapshot_id"],
        "head_sha": manifest["head_sha"],
        "base_sha": manifest["base_sha"],
        "merge_base_sha": manifest["merge_base_sha"],
        "diff_sha256": manifest["diff"]["sha256"],
        "policy_source": manifest["policy"]["source"],
        "policy_present": bool(manifest["policy"]["present"]),
        "is_fork": bool(manifest["is_fork"]),
    }


def review_local(
    *,
    repository: str,
    pull_request: str,
    output_dir: Path,
    github_token: str | None,
    claude_token: str | None,
    openai_key: str | None,
    claude_model: str = models_mod.DEFAULT_CLAUDE_MODEL,
    codex_model: str = models_mod.DEFAULT_CODEX_MODEL,
    claude_effort: str = models_mod.DEFAULT_CLAUDE_EFFORT,
    codex_effort: str = models_mod.DEFAULT_CODEX_EFFORT,
    policy_path: str = validate_mod.DEFAULT_POLICY_PATH,
    claude_bin: str = "claude",
    limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS,
    github_transport: gh.Transport | None = None,
    prepare_options: dict | None = None,
    claude_options: dict | None = None,
    codex_options: dict | None = None,
    summary_path: str | None = None,
) -> dict:
    """Run every stage and return the final document.

    ``github_transport``, ``prepare_options``, ``claude_options`` and
    ``codex_options`` exist for tests (fake GitHub, local git remote, fake CLI
    digest, fake Responses API). Credentials are never taken from them.
    """
    request = validate_mod.build_request(
        repository=repository,
        pull_request=pull_request,
        output_mode=OUTPUT_MODE,
        claude_model=claude_model,
        codex_model=codex_model,
        claude_effort=claude_effort,
        codex_effort=codex_effort,
        policy_path=policy_path,
    )
    output_dir = Path(output_dir)
    check_output_dir(output_dir)
    # Checked before any network access: a run that cannot reach both models
    # would only produce a failed result after fetching the PR.
    if not claude_token:
        raise LocalError(f"{CLAUDE_TOKEN_ENV} is not set")
    if not openai_key:
        raise LocalError(f"{OPENAI_KEY_ENV} is not set")
    log(
        "request: repository={r} pr={n} claude={c} codex={x}".format(
            r=request["repository"], n=request["pr_number"], c=request["claude_model"], x=request["codex_model"]
        )
    )

    with tempfile.TemporaryDirectory(prefix="ai-review-local-") as scratch:
        scratch_dir = Path(scratch)
        bundle_dir = scratch_dir / "bundle"

        try:
            manifest = prepare_mod.prepare(
                repository=request["repository"],
                pr_number=request["pr_number"],
                output_dir=bundle_dir,
                workdir=scratch_dir / "git",
                token=github_token,
                policy_path=request["policy_path"],
                default_policy_file=ROOT / limits_mod.DEFAULT_POLICY_RELPATH,
                limits=limits,
                transport=gh.read_only_transport(github_transport, max_bytes=limits.max_api_response_bytes),
                run_env={},
                **(prepare_options or {}),
            )
        except Exception as err:  # noqa: BLE001 - reported, never converted to success
            raise LocalError(f"prepare failed: {_describe(err, _PREPARE_ERRORS)}") from None

        claude_result: Path | None = None
        claude_job = "failure"
        claude_work = scratch_dir / "claude"
        try:
            run_review_mod.run_review(
                bundle_dir=bundle_dir,
                workdir=claude_work,
                prompt_file=ROOT / "prompts" / "review.md",
                schema_file=ROOT / "schemas" / "review-result.schema.json",
                claude_bin=claude_bin,
                model=request["claude_model"],
                effort=request["claude_effort"],
                token=claude_token,
                limits=limits,
                run_env={},
                **(claude_options or {}),
            )
            normalize_review_mod.normalize(
                bundle_dir=bundle_dir,
                raw_file=claude_work / run_review_mod.RAW_RESULT_NAME,
                invocation_file=claude_work / run_review_mod.INVOCATION_NAME,
                output_dir=scratch_dir / "claude-result",
                limits=limits,
                token=claude_token,
                run_env={},
            )
        except Exception as err:  # noqa: BLE001 - recorded as a failed stage
            log(f"claude stage failed: {_describe(err, _CLAUDE_ERRORS)}")
        else:
            claude_job = "success"
            claude_result = scratch_dir / "claude-result" / "review-result.json"

        # As in the workflow, verification needs a successful primary review.
        codex_result: Path | None = None
        codex_job = "skipped"
        if claude_result is not None:
            codex_job = "failure"
            codex_work = scratch_dir / "codex"
            try:
                run_codex_mod.run_codex_review(
                    bundle_dir=bundle_dir,
                    claude_result_file=claude_result,
                    workdir=codex_work,
                    prompt_file=ROOT / "prompts" / "codex-verify.md",
                    schema_file=ROOT / "schemas" / "codex-review.schema.json",
                    model=request["codex_model"],
                    effort=request["codex_effort"],
                    api_key=openai_key,
                    limits=limits,
                    run_env={},
                    **(codex_options or {}),
                )
                normalize_codex_mod.normalize(
                    bundle_dir=bundle_dir,
                    claude_result_file=claude_result,
                    raw_file=codex_work / run_codex_mod.RAW_RESULT_NAME,
                    invocation_file=codex_work / run_codex_mod.INVOCATION_NAME,
                    output_dir=scratch_dir / "codex-result",
                    limits=limits,
                    token=openai_key,
                )
            except Exception as err:  # noqa: BLE001 - recorded as a failed stage
                log(f"codex stage failed: {_describe(err, _CODEX_ERRORS)}")
            else:
                codex_job = "success"
                codex_result = scratch_dir / "codex-result" / "codex-result.json"

        check_output_dir(output_dir)
        if not output_dir.exists():
            output_dir.mkdir(mode=0o700, parents=True)
        document = finalize_mod.finalize(
            repository=request["repository"],
            pr_number=request["pr_number"],
            output_mode=request["output_mode"],
            claude_model=request["claude_model"],
            codex_model=request["codex_model"],
            claude_effort=request["claude_effort"],
            codex_effort=request["codex_effort"],
            policy_path=request["policy_path"],
            prepare_snapshot=_prepare_snapshot(manifest),
            claude_result_file=claude_result,
            codex_result_file=codex_result,
            claude_job_result=claude_job,
            codex_job_result=codex_job,
            output_dir=output_dir,
            limits=limits,
            run_env={},
        )

    report_mod.report(result_file=output_dir / finalize_mod.FINAL_NAME, summary_path=summary_path, limits=limits)
    log(f"result written to {output_dir}: publishable={document['publishable']}")
    return document


def parse_args(argv: list[str]) -> argparse.Namespace:
    # No option takes a credential. Tokens come from the environment only.
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repository", required=True, help="owner/name of the target repository")
    parser.add_argument("--pull-request", required=True, help="PR number or PR URL")
    parser.add_argument("--output-dir", required=True, type=Path, help="new or empty directory for the result")
    parser.add_argument("--claude-model", default=models_mod.DEFAULT_CLAUDE_MODEL, choices=models_mod.CLAUDE_MODELS)
    parser.add_argument("--codex-model", default=models_mod.DEFAULT_CODEX_MODEL, choices=models_mod.CODEX_MODELS)
    parser.add_argument("--claude-effort", default=models_mod.DEFAULT_CLAUDE_EFFORT, choices=models_mod.CLAUDE_EFFORTS)
    parser.add_argument("--codex-effort", default=models_mod.DEFAULT_CODEX_EFFORT, choices=models_mod.CODEX_EFFORTS)
    parser.add_argument("--policy-path", default=validate_mod.DEFAULT_POLICY_PATH)
    parser.add_argument("--claude-bin", default="claude", help="pinned Claude Code CLI (name on PATH or path)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, env: dict | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    try:
        document = review_local(
            repository=args.repository,
            pull_request=args.pull_request,
            output_dir=args.output_dir,
            github_token=env.get(GITHUB_TOKEN_ENV) or None,
            claude_token=env.get(CLAUDE_TOKEN_ENV) or None,
            openai_key=env.get(OPENAI_KEY_ENV) or None,
            claude_model=args.claude_model,
            codex_model=args.codex_model,
            claude_effort=args.claude_effort,
            codex_effort=args.codex_effort,
            policy_path=args.policy_path,
            claude_bin=resolve_claude_bin(args.claude_bin),
        )
    except (
        LocalError,
        validate_mod.RequestError,
        finalize_mod.FinalizeError,
        finalize_mod.result_mod.ResultError,
        finalize_mod.render_mod.RenderError,
        limits_mod.LimitExceeded,
        # models.ModelNotAllowed and github.ValidationError are ValueErrors.
        ValueError,
    ) as err:
        log(f"stop: {err}")
        return EXIT_STOP
    except Exception as err:  # noqa: BLE001 - last-resort guard, message only
        log(f"unexpected error: {err.__class__.__name__}")
        return EXIT_UNEXPECTED
    return EXIT_OK if document["publishable"] else EXIT_STOP


if __name__ == "__main__":
    sys.exit(main())
