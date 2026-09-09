#!/usr/bin/env python3
"""review step: validate and normalize the model output into a trusted result.

The model returns only ``schema_version``, ``summary``, ``findings`` and
``limitations``. Everything a publisher needs to act on (repository, PR number,
SHAs, diff hash, policy SHA, provider, model, run ID) is added here from the
verified bundle and the invocation record, never from the model.

Validation is deterministic and fails closed: a malformed envelope, a schema
violation, an unexpected tool-permission denial, or an oversized payload stops
the run without writing a result, so nothing downstream can post "no issues".

Standard library only. No network access, no GitHub token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import bundle as bundle_mod  # noqa: E402
from lib import limits as limits_mod  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_STOP = 2

RESULT_NAME = "review-result.json"
TOKEN_ENV = "CLAUDE_CODE_OAUTH_TOKEN"

SEVERITIES = ("high", "medium", "low")
CONFIDENCES = ("high", "medium", "low")
CATEGORIES = ("correctness", "security", "reliability", "maintainability", "testing", "other")
SEVERITY_ORDER = {name: index for index, name in enumerate(SEVERITIES)}

REQUIRED_FINDING_KEYS = ("title", "detail", "severity", "confidence", "category", "path")
OPTIONAL_FINDING_KEYS = ("line",)
RESULT_KEYS = ("schema_version", "summary", "findings", "limitations")


class NormalizeError(Exception):
    """Deterministic stop: the model output cannot be trusted or repaired."""


def log(message: str) -> None:
    sys.stderr.write(f"normalize-review: {message}\n")


# -- envelope -----------------------------------------------------------------


def load_envelope(path: Path, limits: limits_mod.Limits) -> dict:
    """Read the CLI's ``--output-format json`` envelope and check it succeeded."""
    data = Path(path).read_bytes()
    if len(data) > limits.max_raw_result_bytes:
        raise NormalizeError(f"raw result exceeds limit: {len(data)} > {limits.max_raw_result_bytes}")
    try:
        envelope = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NormalizeError("raw result is not valid JSON") from None
    if not isinstance(envelope, dict):
        raise NormalizeError("raw result is not an object")
    if envelope.get("type") != "result":
        raise NormalizeError("raw result is not a result message")
    if envelope.get("is_error") is True:
        raise NormalizeError(f"claude reported an error result: {_subtype(envelope)}")
    if envelope.get("subtype") != "success":
        raise NormalizeError(f"claude did not finish successfully: {_subtype(envelope)}")
    denials = envelope.get("permission_denials")
    if isinstance(denials, list) and denials:
        # No tool is enabled for the review, so any denial means the model tried
        # to act. Stop rather than publish a review produced under that attempt.
        raise NormalizeError(f"claude requested {len(denials)} denied tool call(s)")
    return envelope


def _subtype(envelope: dict) -> str:
    value = envelope.get("subtype")
    return value if isinstance(value, str) and value.isascii() and value.isprintable() else "unknown"


# -- schema validation --------------------------------------------------------


def _require_str(value: object, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise NormalizeError(f"{field} is not a string")
    text = bundle_mod.sanitize_text(value, max_chars).strip()
    if not text:
        raise NormalizeError(f"{field} is empty")
    return text


def _require_enum(value: object, field: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise NormalizeError(f"{field} is not one of {'/'.join(allowed)}")
    return str(value)


def validate_result(
    payload: object,
    *,
    file_index: dict[str, bundle_mod.ChangedFileEntry],
    limits: limits_mod.Limits,
) -> tuple[dict, list[dict]]:
    """Validate the model payload. Returns the clean result and dropped findings."""
    if not isinstance(payload, dict):
        raise NormalizeError("structured output is not an object")
    unknown = sorted(set(payload) - set(RESULT_KEYS))
    if unknown:
        raise NormalizeError(f"structured output has unknown fields: {','.join(unknown)}")
    for key in RESULT_KEYS:
        if key not in payload:
            raise NormalizeError(f"structured output is missing {key}")
    if payload["schema_version"] != limits_mod.RESULT_SCHEMA_VERSION:
        raise NormalizeError("unsupported result schema version")

    summary = _require_str(payload["summary"], "summary", limits.max_summary_chars)

    raw_findings = payload["findings"]
    if not isinstance(raw_findings, list):
        raise NormalizeError("findings is not an array")
    raw_limitations = payload["limitations"]
    if not isinstance(raw_limitations, list):
        raise NormalizeError("limitations is not an array")

    findings: list[dict] = []
    dropped: list[dict] = []
    for index, item in enumerate(raw_findings[: limits.max_findings]):
        try:
            findings.append(_validate_finding(item, file_index=file_index, limits=limits))
        except NormalizeError as err:
            dropped.append({"index": index, "reason": str(err)})
    if len(raw_findings) > limits.max_findings:
        dropped.append({"index": limits.max_findings, "reason": "findings truncated to the limit"})

    limitations: list[str] = []
    for item in raw_limitations[: limits.max_limitations]:
        if isinstance(item, str):
            text = bundle_mod.sanitize_text(item, limits.max_limitation_chars).strip()
            if text:
                limitations.append(text)

    findings.sort(key=lambda f: SEVERITY_ORDER[f["severity"]])
    return (
        {
            "schema_version": limits_mod.RESULT_SCHEMA_VERSION,
            "summary": summary,
            "findings": findings,
            "limitations": limitations,
        },
        dropped,
    )


def _validate_finding(
    item: object,
    *,
    file_index: dict[str, bundle_mod.ChangedFileEntry],
    limits: limits_mod.Limits,
) -> dict:
    if not isinstance(item, dict):
        raise NormalizeError("finding is not an object")
    unknown = sorted(set(item) - set(REQUIRED_FINDING_KEYS) - set(OPTIONAL_FINDING_KEYS))
    if unknown:
        raise NormalizeError(f"finding has unknown fields: {','.join(unknown)}")
    for key in REQUIRED_FINDING_KEYS:
        if key not in item:
            raise NormalizeError(f"finding is missing {key}")

    path = item["path"]
    if not isinstance(path, str) or len(path) > limits.max_finding_path_chars:
        raise NormalizeError("finding path is invalid")
    entry = file_index.get(path)
    if entry is None:
        raise NormalizeError("finding path is not a changed file in this snapshot")
    if not entry.reviewable:
        raise NormalizeError("finding path was excluded from the reviewed diff")

    finding = {
        "title": _require_str(item["title"], "finding title", limits.max_finding_title_chars),
        "detail": _require_str(item["detail"], "finding detail", limits.max_finding_detail_chars),
        "severity": _require_enum(item["severity"], "finding severity", SEVERITIES),
        "confidence": _require_enum(item["confidence"], "finding confidence", CONFIDENCES),
        "category": _require_enum(item["category"], "finding category", CATEGORIES),
        "path": path,
    }
    if "line" in item and item["line"] is not None:
        line = item["line"]
        if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= 1000000:
            raise NormalizeError("finding line is not a positive integer within range")
        finding["line"] = line
    return finding


# -- orchestration ------------------------------------------------------------


def _redact_result(result: dict, token: str | None) -> tuple[dict, int]:
    extra = (token,) if token else ()
    total = 0

    def scrub(text: str) -> str:
        nonlocal total
        cleaned, hits = bundle_mod.redact_secrets(text, extra)
        total += hits
        return cleaned

    result["summary"] = scrub(result["summary"])
    result["limitations"] = [scrub(item) for item in result["limitations"]]
    for finding in result["findings"]:
        finding["title"] = scrub(finding["title"])
        finding["detail"] = scrub(finding["detail"])
    return result, total


def normalize(
    *,
    bundle_dir: Path,
    raw_file: Path,
    invocation_file: Path,
    output_dir: Path,
    limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS,
    token: str | None = None,
    run_env: dict | None = None,
) -> dict:
    run_env = os.environ if run_env is None else run_env
    b = bundle_mod.load_bundle(Path(bundle_dir), limits)
    envelope = load_envelope(Path(raw_file), limits)

    invocation = json.loads(Path(invocation_file).read_text("utf-8"))
    if not isinstance(invocation, dict):
        raise NormalizeError("invocation record is not an object")
    if invocation.get("snapshot_id") != b.snapshot_id:
        raise NormalizeError("invocation record does not belong to this bundle")

    structured = envelope.get("structured_output")
    if structured is None:
        raise NormalizeError("claude returned no structured output")

    result, dropped = validate_result(structured, file_index=b.file_index(), limits=limits)
    result, redactions = _redact_result(result, token)

    manifest = b.manifest
    policy = manifest["policy"] if isinstance(manifest.get("policy"), dict) else {}
    document = {
        "result_schema_version": limits_mod.RESULT_SCHEMA_VERSION,
        "framework_version": limits_mod.FRAMEWORK_VERSION,
        "snapshot": {
            "repository": manifest["repository"],
            "pr_number": manifest["pr_number"],
            "base_sha": manifest["base_sha"],
            "head_sha": manifest["head_sha"],
            "merge_base_sha": manifest["merge_base_sha"],
            "diff_sha256": manifest["diff"]["sha256"],
            "policy_commit_sha": policy.get("commit_sha"),
            "policy_blob_sha": policy.get("blob_sha"),
            "snapshot_id": b.snapshot_id,
        },
        "run": {
            "provider": invocation.get("provider"),
            "cli_version": invocation.get("cli_version"),
            "model_requested": invocation.get("model_requested"),
            "model_reported": _model_reported(envelope),
            "effort": invocation.get("effort"),
            "tools_enabled": invocation.get("tools_enabled"),
            "run_id": invocation.get("run_id"),
            "num_turns": envelope.get("num_turns") if isinstance(envelope.get("num_turns"), int) else None,
            "duration_ms": invocation.get("duration_ms"),
        },
        "normalization": {
            "dropped_findings": dropped,
            "redactions": redactions,
            "excluded_files": sum(1 for f in b.files if f.excluded is not None),
        },
        "review": result,
    }

    payload = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    limits_mod.check_limit("normalized result size", len(payload), limits.max_result_bytes)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / RESULT_NAME).write_bytes(payload)
    log(
        "result written: findings={f} dropped={d} limitations={l} redactions={r}".format(
            f=len(result["findings"]), d=len(dropped), l=len(result["limitations"]), r=redactions
        )
    )
    return document


def _model_reported(envelope: dict) -> str | None:
    for key in ("model", "modelUsage"):
        value = envelope.get(key)
        if isinstance(value, str):
            return bundle_mod.sanitize_text(value, 100)
        if isinstance(value, dict) and value:
            first = sorted(value)[0]
            return bundle_mod.sanitize_text(first, 100)
    return None


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--raw-file", required=True, type=Path)
    parser.add_argument("--invocation-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        normalize(
            bundle_dir=args.bundle_dir,
            raw_file=args.raw_file,
            invocation_file=args.invocation_file,
            output_dir=args.output_dir,
            token=os.environ.get(TOKEN_ENV) or None,
        )
    except (NormalizeError, bundle_mod.BundleError, limits_mod.LimitExceeded) as err:
        log(f"stop: {err}")
        return EXIT_STOP
    except Exception as err:  # noqa: BLE001 - last-resort guard, message only
        log(f"unexpected error: {err.__class__.__name__}")
        return EXIT_UNEXPECTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
