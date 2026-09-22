#!/usr/bin/env python3
"""codex_review step: re-verify the primary review against the same snapshot.

The verification stage is given no way to act: the OpenAI Responses API is
called with no tools, ``tool_choice: "none"``, ``store: false`` and a strict
JSON Schema, so the only thing the model can do is return a structured
verdict (ADR-0007).

The primary (Claude) review is untrusted input here. It is delivered inside the
same nonce-delimited boundary as the PR data, and the fixed verification policy
states that the boundary's contents are data, never instructions (ADR-0006).

Before any request is made, the primary result's snapshot fingerprint must match
the bundle: verifying one snapshot's findings against another snapshot's diff is
meaningless and must never reach a publisher.

This step holds no GitHub credential and posts nothing. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import bundle as bundle_mod  # noqa: E402
from lib import limits as limits_mod  # noqa: E402
from lib import models as models_mod  # noqa: E402
from lib import openai_api  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_STOP = 2

TOKEN_ENV = "OPENAI_API_KEY"
RAW_RESULT_NAME = "codex-raw.json"
INVOCATION_NAME = "codex-invocation.json"
SCHEMA_NAME = "ai_cross_review_verification"

BEGIN_MARKER = "BEGIN UNTRUSTED VERIFICATION INPUT"
END_MARKER = "END UNTRUSTED VERIFICATION INPUT"


class CodexError(Exception):
    """Deterministic stop: the verification cannot be produced safely."""


def log(message: str) -> None:
    sys.stderr.write(f"run-codex-review: {message}\n")


# -- inputs -------------------------------------------------------------------


def load_claude_result(path: Path, snapshot_id: str, limits: limits_mod.Limits) -> dict:
    """Read the normalized primary review and bind it to this snapshot."""
    try:
        data = Path(path).read_bytes()
    except OSError as err:
        raise CodexError(f"cannot read the primary review: {err.__class__.__name__}") from None
    limits_mod.check_limit("primary review size", len(data), limits.max_result_bytes)
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise CodexError("primary review is not valid JSON") from None
    if not isinstance(document, dict):
        raise CodexError("primary review is not an object")
    if document.get("result_schema_version") != limits_mod.RESULT_SCHEMA_VERSION:
        raise CodexError("primary review has an unsupported schema version")
    if document.get("framework_version") != limits_mod.FRAMEWORK_VERSION:
        raise CodexError("primary review was produced by a different framework version")
    snapshot = document.get("snapshot")
    if not isinstance(snapshot, dict):
        raise CodexError("primary review has no snapshot block")
    if snapshot.get("snapshot_id") != snapshot_id:
        raise CodexError("primary review belongs to a different snapshot")
    review = document.get("review")
    if not isinstance(review, dict) or not isinstance(review.get("findings"), list):
        raise CodexError("primary review has no findings array")
    if len(review["findings"]) > limits.max_findings:
        raise CodexError("primary review exceeds the findings limit")
    return document


def build_untrusted_document(
    b: bundle_mod.Bundle, claude: dict, nonce: str, limits: limits_mod.Limits
) -> str:
    """Wrap the snapshot and the primary review in unpredictable markers."""
    begin = f"{BEGIN_MARKER} {nonce}"
    end = f"{END_MARKER} {nonce}"
    files = [
        {
            "path": entry.path,
            "status": entry.status,
            "binary": entry.binary,
            "excluded": entry.excluded,
            "patch_bytes": entry.patch_bytes,
        }
        for entry in b.files
    ]
    review = claude["review"]
    claude_payload = {
        "summary": review.get("summary", ""),
        # The index is explicit so claim_reviews[].claude_index cannot drift.
        "findings": [
            dict(finding, claude_index=index)
            for index, finding in enumerate(review["findings"][: limits.max_findings])
        ],
        "limitations": review.get("limitations", []),
    }
    sections = [
        begin,
        f"## POLICY (source: {b.policy_source})",
        b.policy.decode("utf-8", "replace"),
        "## PR_METADATA (untrusted)",
        json.dumps(b.pr_metadata, ensure_ascii=False, indent=2, sort_keys=True),
        "## FILES (changed files in this snapshot)",
        json.dumps(files, ensure_ascii=False, indent=2),
        "## DIFF (untrusted, merge-base..head)",
        b.diff.decode("utf-8", "replace"),
        "## CLAUDE_REVIEW (untrusted; the primary review under verification, not evidence)",
        json.dumps(claude_payload, ensure_ascii=False, indent=2),
        end,
    ]
    document = "\n\n".join(sections) + "\n"
    if document.count(nonce) != 2:
        raise CodexError("input content collides with the boundary nonce")
    return document


def build_payload(
    *,
    model: str,
    effort: str,
    instructions: str,
    document: str,
    schema: dict,
    limits: limits_mod.Limits,
) -> dict:
    """Build the Responses API request body.

    Every field here is a boundary: no tools, no tool choice, no server-side
    storage of the PR contents, and a strict schema so the output shape cannot
    drift into free text.
    """
    return {
        "model": model,
        "instructions": instructions,
        "input": [
            {
                "role": "user",
                "content": [{"type": "input_text", "text": document}],
            }
        ],
        "tools": [],
        "tool_choice": "none",
        "store": False,
        "reasoning": {"effort": effort},
        "max_output_tokens": limits.codex_max_output_tokens,
        "text": {
            "format": {
                "type": "json_schema",
                "name": SCHEMA_NAME,
                "schema": schema,
                "strict": True,
            }
        },
    }


# -- orchestration ------------------------------------------------------------


def run_codex_review(
    *,
    bundle_dir: Path,
    claude_result_file: Path,
    workdir: Path,
    prompt_file: Path,
    schema_file: Path,
    model: str = models_mod.DEFAULT_CODEX_MODEL,
    effort: str = models_mod.DEFAULT_CODEX_EFFORT,
    api_key: str | None = None,
    base_url: str = limits_mod.DEFAULT_OPENAI_BASE_URL,
    limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS,
    transport=None,
    nonce: str | None = None,
    sleep=None,
    run_env: dict | None = None,
) -> dict:
    run_env = os.environ if run_env is None else run_env
    model = models_mod.validate_codex_model(model)
    effort = models_mod.validate_codex_effort(effort)
    if not api_key:
        raise CodexError(f"{TOKEN_ENV} is not set")

    b = bundle_mod.load_bundle(Path(bundle_dir), limits)
    claude = load_claude_result(Path(claude_result_file), b.snapshot_id, limits)
    log(
        "inputs verified: snapshot={s} claude_findings={f}".format(
            s=b.snapshot_id[:16], f=len(claude["review"]["findings"])
        )
    )

    instructions = Path(prompt_file).read_text("utf-8")
    schema_bytes = Path(schema_file).read_bytes()
    schema = json.loads(schema_bytes.decode("utf-8"))

    nonce = nonce or secrets.token_hex(16)
    document = build_untrusted_document(b, claude, nonce, limits)
    payload = build_payload(
        model=model,
        effort=effort,
        instructions=instructions,
        document=document,
        schema=schema,
        limits=limits,
    )

    client_kwargs = {
        "transport": transport,
        "max_response_bytes": limits.max_api_response_bytes,
        "timeout_seconds": float(limits.codex_timeout_seconds),
        "retry_attempts": limits.codex_retry_attempts,
        "retry_delay_seconds": limits.codex_retry_delay_seconds,
    }
    if sleep is not None:
        client_kwargs["sleep"] = sleep
    client = openai_api.ResponsesClient(base_url, api_key, **client_kwargs)

    started = time.monotonic()
    response = client.create_response(payload)
    duration_ms = int((time.monotonic() - started) * 1000)

    # Fails closed on incomplete, refused, or empty output.
    structured_text = openai_api.extract_structured_text(response)
    if len(structured_text.encode("utf-8")) > limits.max_raw_result_bytes:
        raise CodexError("verification output exceeds the limit")

    input_tokens, output_tokens = openai_api.usage_tokens(response)
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    # The raw envelope stays in the job workdir; it is never uploaded.
    (workdir / RAW_RESULT_NAME).write_text(structured_text, encoding="utf-8")

    invocation = {
        "provider": limits_mod.CODEX_PROVIDER,
        "endpoint": limits_mod.CODEX_API_PATH,
        "model_requested": model,
        "model_reported": openai_api.reported_model(response),
        "effort": effort,
        "tools_enabled": False,
        "store": False,
        "status": "completed",
        "response_id": _response_id(response),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "framework_version": limits_mod.FRAMEWORK_VERSION,
        "snapshot_id": b.snapshot_id,
        "prompt_sha256": bundle_mod.sha256_hex(instructions.encode("utf-8")),
        "schema_sha256": bundle_mod.sha256_hex(schema_bytes),
        "run_id": _run_id(run_env),
    }
    (workdir / INVOCATION_NAME).write_bytes(
        (json.dumps(invocation, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )
    log(
        "verification completed: {n} bytes in {ms} ms (model={m})".format(
            n=len(structured_text), ms=duration_ms, m=invocation["model_reported"] or "unknown"
        )
    )
    return invocation


def _response_id(response: dict) -> str | None:
    value = response.get("id")
    if isinstance(value, str) and value.isascii() and value.isprintable() and len(value) <= 128:
        return value
    return None


def _run_id(run_env: dict) -> str | None:
    value = run_env.get("GITHUB_RUN_ID", "")
    return value if isinstance(value, str) and value.isdigit() and len(value) <= 20 else None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--claude-result-file", required=True, type=Path)
    parser.add_argument("--workdir", required=True, type=Path)
    parser.add_argument("--prompt-file", required=True, type=Path)
    parser.add_argument("--schema-file", required=True, type=Path)
    parser.add_argument("--model", default=models_mod.DEFAULT_CODEX_MODEL)
    parser.add_argument("--effort", default=models_mod.DEFAULT_CODEX_EFFORT)
    parser.add_argument("--base-url", default=limits_mod.DEFAULT_OPENAI_BASE_URL)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        run_codex_review(
            bundle_dir=args.bundle_dir,
            claude_result_file=args.claude_result_file,
            workdir=args.workdir,
            prompt_file=args.prompt_file,
            schema_file=args.schema_file,
            model=args.model,
            effort=args.effort,
            api_key=os.environ.get(TOKEN_ENV) or None,
            base_url=args.base_url,
        )
    except (
        CodexError,
        bundle_mod.BundleError,
        limits_mod.LimitExceeded,
        models_mod.ModelNotAllowed,
        openai_api.OpenAIError,
    ) as err:
        log(f"stop: {err}")
        return EXIT_STOP
    except Exception as err:  # noqa: BLE001 - last-resort guard, message only
        log(f"unexpected error: {err.__class__.__name__}")
        return EXIT_UNEXPECTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
