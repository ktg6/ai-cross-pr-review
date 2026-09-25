#!/usr/bin/env python3
"""codex_review step: validate and normalize the verification output.

The model returns only the verification vocabulary (claim reviews, additional
findings, insufficient context, limitations). Everything a downstream step acts
on - repository, PR number, SHAs, snapshot fingerprint, provider, model - is
added here from the verified bundle and the invocation record, never from the
model.

Validation fails closed for structural damage and drops individual items that
break referential integrity, recording how many were dropped. A dropped item is
never silently turned into "no issue".

Standard library only. No network access, no GitHub token.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import bundle as bundle_mod  # noqa: E402
from lib import limits as limits_mod  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_STOP = 2

RESULT_NAME = "codex-result.json"

SEVERITY_ORDER = {name: index for index, name in enumerate(limits_mod.SEVERITIES)}


class NormalizeError(Exception):
    """Deterministic stop: the verification output cannot be trusted."""


def log(message: str) -> None:
    sys.stderr.write(f"normalize-codex-review: {message}\n")


# -- primitives ---------------------------------------------------------------


def _require_str(value: object, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise NormalizeError(f"{field} is not a string")
    text = bundle_mod.sanitize_text(value, max_chars).strip()
    if not text:
        raise NormalizeError(f"{field} is empty")
    return text


def _optional_str(value: object, field: str, max_chars: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise NormalizeError(f"{field} is not a string or null")
    text = bundle_mod.sanitize_text(value, max_chars).strip()
    return text or None


def _require_enum(value: object, field: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise NormalizeError(f"{field} is not one of {'/'.join(allowed)}")
    return str(value)


def _require_index(value: object, field: str, upper: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < upper:
        raise NormalizeError(f"{field} is not an index of the primary review")
    return value


def _exact_keys(payload: dict, expected: tuple[str, ...], field: str) -> None:
    unknown = sorted(set(payload) - set(expected))
    if unknown:
        raise NormalizeError(f"{field} has unknown fields: {','.join(unknown)}")
    missing = sorted(set(expected) - set(payload))
    if missing:
        raise NormalizeError(f"{field} is missing fields: {','.join(missing)}")


# -- validation ---------------------------------------------------------------


def validate_claim_review(
    item: object, *, claude_count: int, limits: limits_mod.Limits
) -> dict:
    if not isinstance(item, dict):
        raise NormalizeError("claim review is not an object")
    _exact_keys(item, limits_mod.CLAIM_REVIEW_KEYS, "claim review")
    status = _require_enum(item["status"], "claim review status", limits_mod.CLAIM_STATUSES)
    claim = {
        "claude_index": _require_index(item["claude_index"], "claim review claude_index", claude_count),
        "status": status,
        "severity": _require_enum(item["severity"], "claim review severity", limits_mod.SEVERITIES),
        "confidence": _require_enum(item["confidence"], "claim review confidence", limits_mod.CONFIDENCES),
        "rationale": _require_str(item["rationale"], "claim review rationale", limits.max_rationale_chars),
        "suggested_fix": _optional_str(
            item["suggested_fix"], "claim review suggested_fix", limits.max_suggested_fix_chars
        ),
        "duplicate_of": None,
    }
    duplicate_of = item["duplicate_of"]
    if status == "duplicate":
        index = _require_index(duplicate_of, "claim review duplicate_of", claude_count)
        if index == claim["claude_index"]:
            raise NormalizeError("claim review cannot be a duplicate of itself")
        claim["duplicate_of"] = index
    elif duplicate_of is not None:
        raise NormalizeError("duplicate_of is only allowed for duplicate claims")
    return claim


def validate_additional_finding(
    item: object,
    *,
    file_index: dict[str, bundle_mod.ChangedFileEntry],
    limits: limits_mod.Limits,
) -> dict:
    if not isinstance(item, dict):
        raise NormalizeError("additional finding is not an object")
    _exact_keys(item, limits_mod.CODEX_FINDING_KEYS, "additional finding")
    path = item["path"]
    if not isinstance(path, str) or len(path) > limits.max_finding_path_chars:
        raise NormalizeError("additional finding path is invalid")
    entry = file_index.get(path)
    if entry is None:
        raise NormalizeError("additional finding path is not a changed file in this snapshot")
    if not entry.reviewable:
        raise NormalizeError("additional finding path was excluded from the reviewed diff")
    finding = {
        "title": _require_str(item["title"], "additional finding title", limits.max_finding_title_chars),
        "detail": _require_str(item["detail"], "additional finding detail", limits.max_finding_detail_chars),
        "severity": _require_enum(item["severity"], "additional finding severity", limits_mod.SEVERITIES),
        "confidence": _require_enum(item["confidence"], "additional finding confidence", limits_mod.CONFIDENCES),
        "category": _require_enum(item["category"], "additional finding category", limits_mod.CATEGORIES),
        "path": path,
        "line": None,
        "suggested_fix": _optional_str(
            item["suggested_fix"], "additional finding suggested_fix", limits.max_suggested_fix_chars
        ),
    }
    line = item["line"]
    if line is not None:
        if not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= limits_mod.MAX_FINDING_LINE:
            raise NormalizeError("additional finding line is not a positive integer within range")
        finding["line"] = line
    return finding


def _string_list(values: object, field: str, max_items: int, max_chars: int) -> list[str]:
    if not isinstance(values, list):
        raise NormalizeError(f"{field} is not an array")
    out: list[str] = []
    for item in values[:max_items]:
        if isinstance(item, str):
            text = bundle_mod.sanitize_text(item, max_chars).strip()
            if text:
                out.append(text)
    return out


def validate_verification(
    payload: object, *, claude_count: int, file_index: dict, limits: limits_mod.Limits
) -> tuple[dict, list[dict], list[dict]]:
    """Validate the verification payload.

    Returns ``(verification, dropped_claim_reviews, dropped_findings)``.
    """
    if not isinstance(payload, dict):
        raise NormalizeError("structured output is not an object")
    _exact_keys(payload, limits_mod.CODEX_RESULT_KEYS, "structured output")
    if payload["schema_version"] != limits_mod.RESULT_SCHEMA_VERSION:
        raise NormalizeError("unsupported verification schema version")

    summary = _require_str(payload["summary"], "summary", limits.max_summary_chars)

    raw_claims = payload["claim_reviews"]
    if not isinstance(raw_claims, list):
        raise NormalizeError("claim_reviews is not an array")
    raw_findings = payload["additional_findings"]
    if not isinstance(raw_findings, list):
        raise NormalizeError("additional_findings is not an array")

    claims: list[dict] = []
    dropped_claims: list[dict] = []
    seen: set[int] = set()
    for index, item in enumerate(raw_claims[: limits.max_claim_reviews]):
        try:
            claim = validate_claim_review(item, claude_count=claude_count, limits=limits)
        except NormalizeError as err:
            dropped_claims.append({"index": index, "reason": str(err)})
            continue
        if claim["claude_index"] in seen:
            # Two verdicts for one finding would make the final state ambiguous.
            dropped_claims.append({"index": index, "reason": "duplicate verdict for the same finding"})
            continue
        seen.add(claim["claude_index"])
        claims.append(claim)
    if len(raw_claims) > limits.max_claim_reviews:
        dropped_claims.append(
            {"index": limits.max_claim_reviews, "reason": "claim_reviews truncated to the limit"}
        )

    findings: list[dict] = []
    dropped_findings: list[dict] = []
    for index, item in enumerate(raw_findings[: limits.max_additional_findings]):
        try:
            findings.append(validate_additional_finding(item, file_index=file_index, limits=limits))
        except NormalizeError as err:
            dropped_findings.append({"index": index, "reason": str(err)})
    if len(raw_findings) > limits.max_additional_findings:
        dropped_findings.append(
            {"index": limits.max_additional_findings, "reason": "additional_findings truncated to the limit"}
        )

    claims.sort(key=lambda c: c["claude_index"])
    findings.sort(key=lambda f: SEVERITY_ORDER[f["severity"]])
    verification = {
        "schema_version": limits_mod.RESULT_SCHEMA_VERSION,
        "summary": summary,
        "claim_reviews": claims,
        "additional_findings": findings,
        "insufficient_context": _string_list(
            payload["insufficient_context"],
            "insufficient_context",
            limits.max_insufficient_context,
            limits.max_limitation_chars,
        ),
        "limitations": _string_list(
            payload["limitations"], "limitations", limits.max_limitations, limits.max_limitation_chars
        ),
    }
    return verification, dropped_claims, dropped_findings


# -- orchestration ------------------------------------------------------------


def _redact(verification: dict, token: str | None) -> int:
    extra = (token,) if token else ()
    total = 0

    def scrub(text: str | None) -> str | None:
        nonlocal total
        if text is None:
            return None
        cleaned, hits = bundle_mod.redact_secrets(text, extra)
        total += hits
        return cleaned

    verification["summary"] = scrub(verification["summary"])
    verification["insufficient_context"] = [scrub(item) for item in verification["insufficient_context"]]
    verification["limitations"] = [scrub(item) for item in verification["limitations"]]
    for claim in verification["claim_reviews"]:
        claim["rationale"] = scrub(claim["rationale"])
        claim["suggested_fix"] = scrub(claim["suggested_fix"])
    for finding in verification["additional_findings"]:
        finding["title"] = scrub(finding["title"])
        finding["detail"] = scrub(finding["detail"])
        finding["suggested_fix"] = scrub(finding["suggested_fix"])
    return total


def normalize(
    *,
    bundle_dir: Path,
    claude_result_file: Path,
    raw_file: Path,
    invocation_file: Path,
    output_dir: Path,
    limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS,
    token: str | None = None,
) -> dict:
    b = bundle_mod.load_bundle(Path(bundle_dir), limits)

    claude = json.loads(Path(claude_result_file).read_text("utf-8"))
    if not isinstance(claude, dict) or not isinstance(claude.get("review"), dict):
        raise NormalizeError("primary review is not a normalized result")
    claude_snapshot = claude.get("snapshot")
    if not isinstance(claude_snapshot, dict) or claude_snapshot.get("snapshot_id") != b.snapshot_id:
        raise NormalizeError("primary review belongs to a different snapshot")
    claude_findings = claude["review"].get("findings")
    if not isinstance(claude_findings, list):
        raise NormalizeError("primary review has no findings array")

    invocation = json.loads(Path(invocation_file).read_text("utf-8"))
    if not isinstance(invocation, dict):
        raise NormalizeError("invocation record is not an object")
    if invocation.get("snapshot_id") != b.snapshot_id:
        raise NormalizeError("invocation record does not belong to this bundle")

    raw = Path(raw_file).read_bytes()
    limits_mod.check_limit("raw verification size", len(raw), limits.max_raw_result_bytes)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise NormalizeError("raw verification output is not valid JSON") from None

    verification, dropped_claims, dropped_findings = validate_verification(
        payload,
        claude_count=len(claude_findings),
        file_index=b.file_index(),
        limits=limits,
    )
    redactions = _redact(verification, token)

    manifest = b.manifest
    policy = manifest["policy"] if isinstance(manifest.get("policy"), dict) else {}
    document = {
        "result_schema_version": limits_mod.RESULT_SCHEMA_VERSION,
        "framework_version": limits_mod.FRAMEWORK_VERSION,
        "stage": "codex",
        "snapshot": {
            "repository": manifest["repository"],
            "pr_number": manifest["pr_number"],
            "base_sha": manifest["base_sha"],
            "head_sha": manifest["head_sha"],
            "merge_base_sha": manifest["merge_base_sha"],
            "diff_sha256": manifest["diff"]["sha256"],
            "policy_commit_sha": policy.get("commit_sha"),
            "policy_blob_sha": policy.get("blob_sha"),
            "policy_source": policy.get("source"),
            "policy_present": bool(policy.get("present")),
            "is_fork": bool(manifest.get("is_fork")),
            "snapshot_id": b.snapshot_id,
            "reviewable_path_hashes": sorted(
                {
                    bundle_mod.sha256_hex(entry.path.encode("utf-8"))
                    for entry in b.files
                    if entry.reviewable
                }
            ),
        },
        "run": {
            "provider": invocation.get("provider"),
            "cli_version": invocation.get("cli_version"),
            "auth_mode": invocation.get("auth_mode"),
            "model_requested": invocation.get("model_requested"),
            "model_reported": invocation.get("model_reported"),
            "effort": invocation.get("effort"),
            "tools_enabled": invocation.get("tools_enabled"),
            "run_id": invocation.get("run_id"),
            "thread_id": invocation.get("thread_id"),
            "status": invocation.get("status"),
            "input_tokens": invocation.get("input_tokens"),
            "output_tokens": invocation.get("output_tokens"),
            "duration_ms": invocation.get("duration_ms"),
        },
        "normalization": {
            "dropped_claim_reviews": dropped_claims,
            "dropped_findings": dropped_findings,
            "redactions": redactions,
        },
        "verification": verification,
    }

    payload_bytes = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    limits_mod.check_limit("normalized verification size", len(payload_bytes), limits.max_final_result_bytes)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / RESULT_NAME).write_bytes(payload_bytes)
    log(
        "result written: claims={c} added={a} dropped={d} redactions={r}".format(
            c=len(verification["claim_reviews"]),
            a=len(verification["additional_findings"]),
            d=len(dropped_claims) + len(dropped_findings),
            r=redactions,
        )
    )
    return document


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--bundle-dir", required=True, type=Path)
    parser.add_argument("--claude-result-file", required=True, type=Path)
    parser.add_argument("--raw-file", required=True, type=Path)
    parser.add_argument("--invocation-file", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        normalize(
            bundle_dir=args.bundle_dir,
            claude_result_file=args.claude_result_file,
            raw_file=args.raw_file,
            invocation_file=args.invocation_file,
            output_dir=args.output_dir,
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
