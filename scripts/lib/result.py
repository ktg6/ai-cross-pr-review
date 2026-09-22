"""Validation of the final, merged review document (ADR-0005, ADR-0006).

``finalize-review.py`` produces the document and re-validates its own output;
``report-summary.py`` and ``publish-review.py`` validate it again before using
it. The publisher does not trust the finalizer: a document that fails here is
never rendered into a comment, and no field of it can choose a destination.

Standard library only.
"""

from __future__ import annotations

from . import bundle as bundle_mod
from . import limits as limits_mod
from . import models as models_mod

SEVERITY_ORDER = {name: index for index, name in enumerate(limits_mod.SEVERITIES)}
BUCKETS: tuple[str, ...] = ("adopted", "added", "deferred", "rejected", "duplicates")

_SHA40 = "0123456789abcdef"


class ResultError(Exception):
    """Deterministic stop: the final document cannot be trusted."""


def _exact_keys(payload: object, expected: tuple[str, ...], field: str) -> dict:
    if not isinstance(payload, dict):
        raise ResultError(f"{field} is not an object")
    unknown = sorted(set(payload) - set(expected))
    if unknown:
        raise ResultError(f"{field} has unknown fields: {','.join(unknown)}")
    missing = sorted(set(expected) - set(payload))
    if missing:
        raise ResultError(f"{field} is missing fields: {','.join(missing)}")
    return payload


def _required_and_optional_keys(
    payload: object,
    required: tuple[str, ...],
    optional: tuple[str, ...],
    field: str,
) -> dict:
    if not isinstance(payload, dict):
        raise ResultError(f"{field} is not an object")
    allowed = set(required) | set(optional)
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ResultError(f"{field} has unknown fields: {','.join(unknown)}")
    missing = sorted(set(required) - set(payload))
    if missing:
        raise ResultError(f"{field} is missing fields: {','.join(missing)}")
    return payload


def _text(value: object, field: str, max_chars: int) -> str:
    if not isinstance(value, str):
        raise ResultError(f"{field} is not a string")
    text = bundle_mod.sanitize_text(value, max_chars).strip()
    if not text:
        raise ResultError(f"{field} is empty")
    return text


def _optional_text(value: object, field: str, max_chars: int) -> str | None:
    if value is None:
        return None
    return _text(value, field, max_chars)


def _enum(value: object, field: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise ResultError(f"{field} is not one of {'/'.join(allowed)}")
    return str(value)


def _bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ResultError(f"{field} is not a boolean")
    return value


def _sha(value: object, field: str, length: int) -> str:
    if not isinstance(value, str) or len(value) != length or any(c not in _SHA40 for c in value):
        raise ResultError(f"{field} is not a {length}-hex digest")
    return value


def _optional_sha(value: object, field: str, length: int) -> str | None:
    return None if value is None else _sha(value, field, length)


def _index(value: object, field: str, limits: limits_mod.Limits) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < limits.max_findings:
        raise ResultError(f"{field} is not a valid finding index")
    return value


def validate_entry(item: object, limits: limits_mod.Limits, path_hashes: frozenset[str]) -> dict:
    """Validate one review entry, whatever bucket it came from."""
    payload = _exact_keys(item, limits_mod.FINAL_ENTRY_KEYS, "review entry")
    path = payload["path"]
    if not isinstance(path, str) or not path or len(path) > limits.max_finding_path_chars:
        raise ResultError("review entry path is invalid")
    reason = limits_mod.forbidden_path_reason(path)
    if reason is not None:
        raise ResultError(f"review entry path is not publishable: {reason}")
    if bundle_mod.sha256_hex(path.encode("utf-8")) not in path_hashes:
        raise ResultError("review entry path is not a reviewable file in this snapshot")

    line = payload["line"]
    if line is not None and (
        not isinstance(line, int) or isinstance(line, bool) or not 1 <= line <= limits_mod.MAX_FINDING_LINE
    ):
        raise ResultError("review entry line is not a positive integer within range")

    return {
        "origin": _enum(payload["origin"], "review entry origin", limits_mod.ENTRY_ORIGINS),
        "claude_index": _index(payload["claude_index"], "review entry claude_index", limits),
        "title": _text(payload["title"], "review entry title", limits.max_finding_title_chars),
        "detail": _text(payload["detail"], "review entry detail", limits.max_finding_detail_chars),
        "severity": _enum(payload["severity"], "review entry severity", limits_mod.SEVERITIES),
        "confidence": _enum(payload["confidence"], "review entry confidence", limits_mod.CONFIDENCES),
        "category": _enum(payload["category"], "review entry category", limits_mod.CATEGORIES),
        "path": path,
        "line": line,
        "rationale": _optional_text(payload["rationale"], "review entry rationale", limits.max_rationale_chars),
        "suggested_fix": _optional_text(
            payload["suggested_fix"], "review entry suggested_fix", limits.max_suggested_fix_chars
        ),
        "duplicate_of": _index(payload["duplicate_of"], "review entry duplicate_of", limits),
    }


def _validate_stage(payload: object, field: str) -> dict:
    stage = _exact_keys(payload, limits_mod.FINAL_STAGE_KEYS, field)
    return {
        "status": _enum(stage["status"], f"{field}.status", limits_mod.STAGE_STATUSES),
        "model_requested": _optional_text(stage["model_requested"], f"{field}.model_requested", 100),
        "model_reported": _optional_text(stage["model_reported"], f"{field}.model_reported", 100),
        "detail": _optional_text(stage["detail"], f"{field}.detail", 500),
    }


def _validate_request(payload: object) -> dict:
    request = _exact_keys(payload, limits_mod.FINAL_REQUEST_KEYS, "request")
    pr_number = request["pr_number"]
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise ResultError("request.pr_number is not a positive integer")
    repository = request["repository"]
    if not isinstance(repository, str) or repository.count("/") != 1:
        raise ResultError("request.repository is not owner/name")
    return {
        "repository": repository,
        "pr_number": pr_number,
        "output_mode": _enum(request["output_mode"], "request.output_mode", models_mod.OUTPUT_MODES),
        # Model and effort values must still be on the allowlist at publish time.
        "claude_model_requested": models_mod.validate_claude_model(request["claude_model_requested"]),
        "codex_model_requested": models_mod.validate_codex_model(request["codex_model_requested"]),
        "claude_effort": models_mod.validate_claude_effort(request["claude_effort"]),
        "codex_effort": models_mod.validate_codex_effort(request["codex_effort"]),
        "policy_path": _text(request["policy_path"], "request.policy_path", 256),
    }


def _validate_snapshot(payload: object, limits: limits_mod.Limits) -> dict:
    snapshot = _exact_keys(payload, limits_mod.SNAPSHOT_KEYS, "snapshot")
    pr_number = snapshot["pr_number"]
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise ResultError("snapshot.pr_number is not a positive integer")
    hashes = snapshot["reviewable_path_hashes"]
    if not isinstance(hashes, list) or len(hashes) > limits.max_changed_files:
        raise ResultError("snapshot.reviewable_path_hashes is not an array within the limit")
    clean = [_sha(value, "reviewable path hash", 64) for value in hashes]
    if clean != sorted(set(clean)):
        raise ResultError("snapshot.reviewable_path_hashes is not sorted and unique")
    repository = snapshot["repository"]
    if not isinstance(repository, str) or repository.count("/") != 1:
        raise ResultError("snapshot.repository is not owner/name")
    return {
        "repository": repository,
        "pr_number": pr_number,
        "base_sha": _sha(snapshot["base_sha"], "snapshot.base_sha", 40),
        "head_sha": _sha(snapshot["head_sha"], "snapshot.head_sha", 40),
        "merge_base_sha": _sha(snapshot["merge_base_sha"], "snapshot.merge_base_sha", 40),
        "diff_sha256": _sha(snapshot["diff_sha256"], "snapshot.diff_sha256", 64),
        "policy_commit_sha": _optional_sha(snapshot["policy_commit_sha"], "snapshot.policy_commit_sha", 40),
        "policy_blob_sha": _optional_sha(snapshot["policy_blob_sha"], "snapshot.policy_blob_sha", 40),
        "policy_source": _enum(snapshot["policy_source"], "snapshot.policy_source", limits_mod.POLICY_SOURCES),
        "policy_present": _bool(snapshot["policy_present"], "snapshot.policy_present"),
        "is_fork": _bool(snapshot["is_fork"], "snapshot.is_fork"),
        "snapshot_id": _sha(snapshot["snapshot_id"], "snapshot.snapshot_id", 64),
        "reviewable_path_hashes": clean,
    }


def _nonnegative_int(value: object, field: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ResultError(f"{field} is not a non-negative integer")
    return value


def _positive_line(value: object, field: str, *, nullable: bool = False) -> int | None:
    if nullable and value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= limits_mod.MAX_FINDING_LINE:
        raise ResultError(f"{field} is not a positive integer within range")
    return value


def _validate_path(value: object, field: str, limits: limits_mod.Limits, path_hashes: frozenset[str]) -> str:
    if not isinstance(value, str) or not value or len(value) > limits.max_finding_path_chars:
        raise ResultError(f"{field} is invalid")
    reason = limits_mod.forbidden_path_reason(value)
    if reason is not None:
        raise ResultError(f"{field} is not publishable: {reason}")
    if bundle_mod.sha256_hex(value.encode("utf-8")) not in path_hashes:
        raise ResultError(f"{field} is not a reviewable file in this snapshot")
    return value


def _validate_text_list(value: object, field: str, maximum: int, item_max_chars: int) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ResultError(f"{field} is not an array within the limit")
    return [_text(item, f"{field} item", item_max_chars) for item in value]


def _validate_dropped_items(value: object, field: str, limits: limits_mod.Limits) -> list[dict]:
    if not isinstance(value, list) or len(value) > limits.max_findings + limits.max_additional_findings:
        raise ResultError(f"{field} is not an array within the limit")
    result = []
    for index, item in enumerate(value):
        dropped = _exact_keys(item, limits_mod.DROPPED_FINDING_KEYS, f"{field}[{index}]")
        result.append(
            {
                "index": _nonnegative_int(dropped["index"], f"{field}[{index}].index"),
                "reason": _text(dropped["reason"], f"{field}[{index}].reason", limits.max_limitation_chars),
            }
        )
    return result


def _validate_common_stage(payload: object, expected_keys: tuple[str, ...], limits: limits_mod.Limits) -> tuple[dict, dict]:
    document = _exact_keys(payload, expected_keys, "stage result")
    if document["result_schema_version"] != limits_mod.RESULT_SCHEMA_VERSION:
        raise ResultError("unsupported result schema version")
    if document["framework_version"] != limits_mod.FRAMEWORK_VERSION:
        raise ResultError("result was produced by a different framework version")
    snapshot = _validate_snapshot(document["snapshot"], limits)
    return document, snapshot


def _validate_model_run(
    payload: object,
    limits: limits_mod.Limits,
    *,
    expected_keys: tuple[str, ...],
    provider: str,
    endpoint: str | None = None,
    cli_version: str | None = None,
    model_validator=None,
    effort_validator=None,
) -> dict:
    run = _exact_keys(payload, expected_keys, "stage result.run")
    if run["provider"] != provider:
        raise ResultError("stage result.run.provider is unexpected")
    if endpoint is not None and run["endpoint"] != endpoint:
        raise ResultError("stage result.run.endpoint is unexpected")
    if cli_version is not None and run["cli_version"] != cli_version:
        raise ResultError("stage result.run.cli_version is unexpected")
    try:
        requested = model_validator(run["model_requested"])
        effort = effort_validator(run["effort"])
    except (ValueError, TypeError) as err:
        raise ResultError(f"stage result.run allowlist validation failed: {err}") from None
    reported = _optional_text(run["model_reported"], "stage result.run.model_reported", 100)
    tools_enabled = _bool(run["tools_enabled"], "stage result.run.tools_enabled")
    if tools_enabled:
        raise ResultError("stage result.run.tools_enabled must be false")
    result = {
        **run,
        "model_requested": requested,
        "model_reported": reported,
        "effort": effort,
        "run_id": _optional_text(run["run_id"], "stage result.run.run_id", 256),
        "duration_ms": _nonnegative_int(run["duration_ms"], "stage result.run.duration_ms", nullable=True),
    }
    if "num_turns" in expected_keys:
        result["num_turns"] = _nonnegative_int(run["num_turns"], "stage result.run.num_turns", nullable=True)
    return result


def _validate_claude_finding(item: object, index: int, limits: limits_mod.Limits, path_hashes: frozenset[str]) -> dict:
    finding = _required_and_optional_keys(
        item,
        limits_mod.REQUIRED_FINDING_KEYS,
        limits_mod.OPTIONAL_FINDING_KEYS,
        f"review.findings[{index}]",
    )
    result = {
        "title": _text(finding["title"], f"review.findings[{index}].title", limits.max_finding_title_chars),
        "detail": _text(finding["detail"], f"review.findings[{index}].detail", limits.max_finding_detail_chars),
        "severity": _enum(finding["severity"], f"review.findings[{index}].severity", limits_mod.SEVERITIES),
        "confidence": _enum(finding["confidence"], f"review.findings[{index}].confidence", limits_mod.CONFIDENCES),
        "category": _enum(finding["category"], f"review.findings[{index}].category", limits_mod.CATEGORIES),
        "path": _validate_path(finding["path"], f"review.findings[{index}].path", limits, path_hashes),
    }
    if "line" in finding:
        result["line"] = _positive_line(finding["line"], f"review.findings[{index}].line", nullable=True)
    return result


def _validate_codex_finding(item: object, index: int, limits: limits_mod.Limits, path_hashes: frozenset[str]) -> dict:
    finding = _exact_keys(item, limits_mod.CODEX_FINDING_KEYS, f"verification.additional_findings[{index}]")
    return {
        "title": _text(finding["title"], f"additional finding[{index}].title", limits.max_finding_title_chars),
        "detail": _text(finding["detail"], f"additional finding[{index}].detail", limits.max_finding_detail_chars),
        "severity": _enum(finding["severity"], f"additional finding[{index}].severity", limits_mod.SEVERITIES),
        "confidence": _enum(finding["confidence"], f"additional finding[{index}].confidence", limits_mod.CONFIDENCES),
        "category": _enum(finding["category"], f"additional finding[{index}].category", limits_mod.CATEGORIES),
        "path": _validate_path(finding["path"], f"additional finding[{index}].path", limits, path_hashes),
        "line": _positive_line(finding["line"], f"additional finding[{index}].line", nullable=True),
        "suggested_fix": _optional_text(
            finding["suggested_fix"], f"additional finding[{index}].suggested_fix", limits.max_suggested_fix_chars
        ),
    }


def validate_normalized_claude_document(
    document: object, limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS
) -> dict:
    """Validate the complete normalized Claude artifact before merging it."""
    payload, snapshot = _validate_common_stage(document, limits_mod.NORMALIZED_RESULT_KEYS, limits)
    run = _validate_model_run(
        payload["run"],
        limits,
        expected_keys=limits_mod.RUN_KEYS,
        provider=limits_mod.REVIEW_PROVIDER,
        cli_version=limits_mod.CLAUDE_CODE_VERSION,
        model_validator=models_mod.validate_claude_model,
        effort_validator=models_mod.validate_claude_effort,
    )
    normalization = _exact_keys(payload["normalization"], limits_mod.NORMALIZATION_KEYS, "normalization")
    findings = payload["review"].get("findings") if isinstance(payload["review"], dict) else None
    review = _exact_keys(payload["review"], limits_mod.RESULT_KEYS, "review")
    if not isinstance(findings, list) or len(findings) > limits.max_findings:
        raise ResultError("review.findings is not an array within the limit")
    clean_review = {
        "schema_version": review["schema_version"],
        "summary": _text(review["summary"], "review.summary", limits.max_summary_chars),
        "findings": [
            _validate_claude_finding(item, index, limits, frozenset(snapshot["reviewable_path_hashes"]))
            for index, item in enumerate(findings)
        ],
        "limitations": _validate_text_list(
            review["limitations"], "review.limitations", limits.max_limitations, limits.max_limitation_chars
        ),
    }
    if clean_review["schema_version"] != limits_mod.RESULT_SCHEMA_VERSION:
        raise ResultError("review.schema_version is unsupported")
    return {
        **payload,
        "run": run,
        "normalization": {
            "dropped_findings": _validate_dropped_items(normalization["dropped_findings"], "normalization.dropped_findings", limits),
            "redactions": _nonnegative_int(normalization["redactions"], "normalization.redactions"),
            "excluded_files": _nonnegative_int(normalization["excluded_files"], "normalization.excluded_files"),
        },
        "review": clean_review,
    }


def validate_normalized_codex_document(
    document: object,
    *,
    claude_count: int,
    limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS,
) -> dict:
    """Validate the complete normalized Codex artifact before merging it."""
    payload, snapshot = _validate_common_stage(document, limits_mod.CODEX_NORMALIZED_RESULT_KEYS, limits)
    if payload["stage"] != "codex":
        raise ResultError("stage result.stage is not codex")
    run = _validate_model_run(
        payload["run"],
        limits,
        expected_keys=limits_mod.CODEX_RUN_KEYS,
        provider=limits_mod.CODEX_PROVIDER,
        endpoint=limits_mod.CODEX_API_PATH,
        model_validator=models_mod.validate_codex_model,
        effort_validator=models_mod.validate_codex_effort,
    )
    if run["store"] is not False:
        raise ResultError("stage result.run.store must be false")
    if run["status"] != "completed":
        raise ResultError("stage result.run.status is not completed")
    run["store"] = False
    run["response_id"] = _optional_text(run["response_id"], "stage result.run.response_id", 256)
    for field in ("input_tokens", "output_tokens"):
        run[field] = _nonnegative_int(run[field], f"stage result.run.{field}", nullable=True)

    normalization = _exact_keys(payload["normalization"], limits_mod.CODEX_NORMALIZATION_KEYS, "codex normalization")
    verification = _exact_keys(payload["verification"], limits_mod.CODEX_RESULT_KEYS, "verification")
    if verification["schema_version"] != limits_mod.RESULT_SCHEMA_VERSION:
        raise ResultError("verification.schema_version is unsupported")
    claims = verification["claim_reviews"]
    if not isinstance(claims, list) or len(claims) > limits.max_claim_reviews:
        raise ResultError("verification.claim_reviews is not an array within the limit")
    seen: set[int] = set()
    clean_claims = []
    for index, item in enumerate(claims):
        claim = _exact_keys(item, limits_mod.CLAIM_REVIEW_KEYS, f"verification.claim_reviews[{index}]")
        claude_index = claim["claude_index"]
        if not isinstance(claude_index, int) or isinstance(claude_index, bool) or not 0 <= claude_index < claude_count:
            raise ResultError(f"verification.claim_reviews[{index}].claude_index is invalid")
        if claude_index in seen:
            raise ResultError("verification.claim_reviews contains duplicate claude_index")
        seen.add(claude_index)
        status = _enum(claim["status"], f"verification.claim_reviews[{index}].status", limits_mod.CLAIM_STATUSES)
        duplicate_of = _index(claim["duplicate_of"], f"verification.claim_reviews[{index}].duplicate_of", limits)
        if status == "duplicate":
            if duplicate_of is None or duplicate_of >= claude_count or duplicate_of == claude_index:
                raise ResultError("duplicate claim must reference a Claude finding")
        elif duplicate_of is not None:
            raise ResultError("non-duplicate claim cannot have duplicate_of")
        clean_claims.append(
            {
                "claude_index": claude_index,
                "status": status,
                "severity": _enum(claim["severity"], f"verification.claim_reviews[{index}].severity", limits_mod.SEVERITIES),
                "confidence": _enum(claim["confidence"], f"verification.claim_reviews[{index}].confidence", limits_mod.CONFIDENCES),
                "rationale": _text(claim["rationale"], f"verification.claim_reviews[{index}].rationale", limits.max_rationale_chars),
                "suggested_fix": _optional_text(
                    claim["suggested_fix"], f"verification.claim_reviews[{index}].suggested_fix", limits.max_suggested_fix_chars
                ),
                "duplicate_of": duplicate_of,
            }
        )
    additional = verification["additional_findings"]
    if not isinstance(additional, list) or len(additional) > limits.max_additional_findings:
        raise ResultError("verification.additional_findings is not an array within the limit")
    clean_verification = {
        "schema_version": verification["schema_version"],
        "summary": _text(verification["summary"], "verification.summary", limits.max_summary_chars),
        "claim_reviews": clean_claims,
        "additional_findings": [
            _validate_codex_finding(item, index, limits, frozenset(snapshot["reviewable_path_hashes"]))
            for index, item in enumerate(additional)
        ],
        "insufficient_context": _validate_text_list(
            verification["insufficient_context"],
            "verification.insufficient_context",
            limits.max_insufficient_context,
            limits.max_limitation_chars,
        ),
        "limitations": _validate_text_list(
            verification["limitations"], "verification.limitations", limits.max_limitations, limits.max_limitation_chars
        ),
    }
    return {
        **payload,
        "run": run,
        "normalization": {
            "dropped_claim_reviews": _validate_dropped_items(
                normalization["dropped_claim_reviews"], "codex normalization.dropped_claim_reviews", limits
            ),
            "dropped_findings": _validate_dropped_items(
                normalization["dropped_findings"], "codex normalization.dropped_findings", limits
            ),
            "redactions": _nonnegative_int(normalization["redactions"], "codex normalization.redactions"),
        },
        "verification": clean_verification,
    }


def _validate_review(payload: object, limits: limits_mod.Limits, path_hashes: frozenset[str]) -> dict:
    review = _exact_keys(payload, limits_mod.FINAL_REVIEW_KEYS, "review")
    out: dict = {"summary": _text(review["summary"], "review.summary", limits.max_summary_chars)}
    total = 0
    for bucket in BUCKETS:
        items = review[bucket]
        if not isinstance(items, list):
            raise ResultError(f"review.{bucket} is not an array")
        total += len(items)
        if total > limits.max_findings + limits.max_additional_findings:
            raise ResultError("review entries exceed the combined limit")
        entries = [validate_entry(item, limits, path_hashes) for item in items]
        entries.sort(key=lambda e: SEVERITY_ORDER[e["severity"]])
        out[bucket] = entries
    for key, max_items in (
        ("insufficient_context", limits.max_insufficient_context),
        ("limitations", limits.max_limitations),
    ):
        values = review[key]
        if not isinstance(values, list) or len(values) > max_items:
            raise ResultError(f"review.{key} is not an array within the limit")
        out[key] = [_text(item, f"review.{key} item", limits.max_limitation_chars) for item in values]
    for key in ("dropped", "redactions"):
        value = review[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ResultError(f"review.{key} is not a non-negative integer")
        out[key] = value
    return out


def validate_final_document(document: object, limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS) -> dict:
    """Validate the whole final document and return a clean copy."""
    payload = _exact_keys(document, limits_mod.FINAL_RESULT_KEYS, "final result")
    if payload["final_schema_version"] != limits_mod.FINAL_SCHEMA_VERSION:
        raise ResultError("unsupported final schema version")
    if payload["framework_version"] != limits_mod.FRAMEWORK_VERSION:
        raise ResultError("result was produced by a different framework version")

    request = _validate_request(payload["request"])
    snapshot = _validate_snapshot(payload["snapshot"], limits)
    if snapshot["repository"].lower() != request["repository"].lower():
        raise ResultError("snapshot repository does not match the request")
    if snapshot["pr_number"] != request["pr_number"]:
        raise ResultError("snapshot pull request does not match the request")

    stages = _exact_keys(payload["stages"], ("claude", "codex"), "stages")
    clean_stages = {name: _validate_stage(stages[name], f"stages.{name}") for name in ("claude", "codex")}

    verification = _exact_keys(payload["verification"], limits_mod.FINAL_VERIFICATION_KEYS, "verification")
    clean_verification = {
        key: _bool(verification[key], f"verification.{key}") for key in limits_mod.FINAL_VERIFICATION_KEYS
    }

    review = _validate_review(payload["review"], limits, frozenset(snapshot["reviewable_path_hashes"]))
    publishable = _bool(payload["publishable"], "publishable")

    # A publishable document must have both stages green and every check true.
    if publishable:
        if any(stage["status"] != "success" for stage in clean_stages.values()):
            raise ResultError("a publishable result requires both stages to succeed")
        if not all(clean_verification.values()):
            raise ResultError("a publishable result requires every verification check to pass")

    return {
        "final_schema_version": limits_mod.FINAL_SCHEMA_VERSION,
        "framework_version": limits_mod.FRAMEWORK_VERSION,
        "request": request,
        "snapshot": snapshot,
        "stages": clean_stages,
        "verification": clean_verification,
        "review": review,
        "publishable": publishable,
    }


def redact_document(document: dict) -> int:
    """Independent redaction pass over every model-derived string."""
    total = 0

    def scrub(text: str | None) -> str | None:
        nonlocal total
        if text is None:
            return None
        cleaned, hits = bundle_mod.redact_secrets(text)
        total += hits
        return cleaned

    review = document["review"]
    review["summary"] = scrub(review["summary"])
    for key in ("insufficient_context", "limitations"):
        review[key] = [scrub(item) for item in review[key]]
    for bucket in BUCKETS:
        for entry in review[bucket]:
            for field in ("title", "detail", "rationale", "suggested_fix"):
                entry[field] = scrub(entry[field])
    return total
