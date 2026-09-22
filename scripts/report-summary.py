#!/usr/bin/env python3
"""report step: show the final result in the job summary.

The report is written for every run, including failed ones: a run that produced
no review must say so where the operator looks first. The step re-validates the
final document instead of trusting the finalizer's Markdown, then re-renders the
summary out of the validated JSON.

This step holds no credentials and posts nothing.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import limits as limits_mod  # noqa: E402
from lib import render as render_mod  # noqa: E402
from lib import result as result_mod  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_STOP = 2

FAILURE_HEADING = "## AI Cross Review: 結果を表示できない"


class ReportError(Exception):
    """The final document is unusable; the summary says so instead."""


def log(message: str) -> None:
    sys.stderr.write(f"report-summary: {message}\n")


def load_document(path: Path, limits: limits_mod.Limits) -> dict:
    try:
        data = Path(path).read_bytes()
    except OSError as err:
        raise ReportError(f"final result cannot be read: {err.__class__.__name__}") from None
    limits_mod.check_limit("final result size", len(data), limits.max_final_result_bytes)
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ReportError("final result is not valid JSON") from None
    return result_mod.validate_final_document(document, limits)


def failure_summary(reason: str) -> str:
    return "\n".join(
        [
            FAILURE_HEADING,
            "",
            f"- 理由: {render_mod.inline(reason)}",
            "- 最終結果が生成されなかったか、検証に失敗した。",
            "- **これは「問題なし」ではない。** 対象PRへのコメントも行わない。",
            "",
        ]
    )


def write_summary(text: str, summary_path: str | None, limits: limits_mod.Limits) -> None:
    body = text[: limits.max_job_summary_chars]
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(body if body.endswith("\n") else body + "\n")
    else:
        sys.stdout.write(body)


def report(
    *,
    result_file: Path,
    summary_path: str | None,
    limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS,
) -> bool:
    """Write the summary. Returns True when a valid document was rendered."""
    try:
        document = load_document(Path(result_file), limits)
    except (ReportError, result_mod.ResultError, limits_mod.LimitExceeded) as err:
        log(f"unusable final result: {err}")
        write_summary(failure_summary(str(err)), summary_path, limits)
        return False
    try:
        text = render_mod.render_summary(document, limits)
    except render_mod.RenderError as err:
        # A result too large to show is still a result the operator must hear about.
        log(f"summary cannot be rendered: {err}")
        write_summary(failure_summary(str(err)), summary_path, limits)
        return False
    write_summary(text, summary_path, limits)
    log(
        "summary written: publishable={p} claude={c} codex={x}".format(
            p=document["publishable"],
            c=document["stages"]["claude"]["status"],
            x=document["stages"]["codex"]["status"],
        )
    )
    return True


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--result-file", required=True, type=Path)
    parser.add_argument("--summary-file", default="", help="defaults to $GITHUB_STEP_SUMMARY")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    import os

    args = parse_args(sys.argv[1:] if argv is None else argv)
    summary_path = args.summary_file or os.environ.get("GITHUB_STEP_SUMMARY") or ""
    try:
        ok = report(result_file=args.result_file, summary_path=summary_path or None)
    except Exception as err:  # noqa: BLE001 - last-resort guard, message only
        log(f"unexpected error: {err.__class__.__name__}")
        return EXIT_UNEXPECTED
    return EXIT_OK if ok else EXIT_STOP


if __name__ == "__main__":
    sys.exit(main())
