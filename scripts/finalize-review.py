#!/usr/bin/env python3
"""finalize step: merge the two stages into one deterministic result.

This step performs, in order: schema validation of both stage results,
snapshot-fingerprint matching against the value the prepare job published
outside the artifacts, referential-integrity checks, redaction, merging into the
final buckets, and rendering of the Markdown and JSON artifacts.

Failure is never converted into success. A missing, failed, or malformed stage
result produces a document whose stage status says so and whose ``publishable``
flag is false; it never produces an empty, green review.

No model output can choose a destination here: repository, PR number and output
mode come from the validated request, and the snapshot comes from the prepare
job's outputs.

Standard library only. No network access, no credentials.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import limits as limits_mod  # noqa: E402
from lib import models as models_mod  # noqa: E402
from lib import render as render_mod  # noqa: E402
from lib import result as result_mod  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_STOP = 2

FINAL_NAME = "final-review.json"
SUMMARY_NAME = "review-summary.md"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_JOB_RESULTS = ("success", "failure", "cancelled", "skipped")


class FinalizeError(Exception):
    """Deterministic stop: the final document cannot be produced at all."""


def log(message: str) -> None:
    sys.stderr.write(f"finalize-review: {message}\n")


# -- stage loading ------------------------------------------------------------


class StageInput:
    """One stage's artifact plus how its job ended."""

    def __init__(self, name: str, job_result: str, path: Path | None, model_requested: str):
        self.name = name
        self.job_result = job_result if job_result in _JOB_RESULTS else "failure"
        self.path = path
        self.model_requested = model_requested
        self.document: dict | None = None
        self.status = "missing"
        self.detail: str | None = None

    @property
    def model_reported(self) -> str | None:
        if not isinstance(self.document, dict):
            return None
        run = self.document.get("run")
        value = run.get("model_reported") if isinstance(run, dict) else None
        return value if isinstance(value, str) and value else None

    def as_stage(self) -> dict:
        return {
            "status": self.status,
            "model_requested": self.model_requested,
            "model_reported": self.model_reported,
            "detail": self.detail,
        }


def _load_stage(stage: StageInput, *, expected_keys: tuple[str, ...], limits: limits_mod.Limits, validator=None) -> None:
    """Populate ``stage`` with its document, or record why it is unusable."""
    if stage.job_result != "success":
        stage.status = "skipped" if stage.job_result == "skipped" else "failed"
        stage.detail = f"{stage.name} job result: {stage.job_result}"
        return
    if stage.path is None or not Path(stage.path).is_file():
        stage.status = "missing"
        stage.detail = f"{stage.name} result artifact is missing"
        return
    try:
        data = Path(stage.path).read_bytes()
        limits_mod.check_limit(f"{stage.name} result size", len(data), limits.max_final_result_bytes)
        document = json.loads(data.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, limits_mod.LimitExceeded) as err:
        stage.status = "invalid"
        stage.detail = f"{stage.name} result could not be read: {err.__class__.__name__}"
        return
    if not isinstance(document, dict):
        stage.status = "invalid"
        stage.detail = f"{stage.name} result is not an object"
        return
    missing = [key for key in expected_keys if key not in document]
    if missing:
        stage.status = "invalid"
        stage.detail = f"{stage.name} result is missing: {','.join(sorted(missing))}"
        return
    if document.get("framework_version") != limits_mod.FRAMEWORK_VERSION:
        stage.status = "invalid"
        stage.detail = f"{stage.name} result was produced by a different framework version"
        return
    if validator is not None:
        try:
            document = validator(document)
        except result_mod.ResultError as err:
            stage.status = "invalid"
            stage.detail = f"{stage.name} result schema is invalid: {err}"
            return
    stage.document = document
    stage.status = "success"


# Snapshot fields the prepare job publishes as job outputs (outside every artifact).
# A stage result must agree with all of them, not only with the snapshot ID, so an
# artifact that borrowed the ID but carries a different head SHA is still refused.
PREPARE_SNAPSHOT_KEYS: tuple[str, ...] = (
    "snapshot_id",
    "head_sha",
    "base_sha",
    "merge_base_sha",
    "diff_sha256",
    "policy_source",
    "policy_present",
    "is_fork",
)


def _fingerprint_ok(stage: StageInput, prepare_snapshot: dict) -> bool:
    if stage.status != "success" or not isinstance(stage.document, dict):
        return False
    snapshot = stage.document.get("snapshot")
    if not isinstance(snapshot, dict):
        return False
    return all(snapshot.get(key) == prepare_snapshot[key] for key in PREPARE_SNAPSHOT_KEYS)


def _request_snapshot_ok(stage: StageInput, request: dict) -> bool:
    if stage.status != "success" or not isinstance(stage.document, dict):
        return False
    snapshot = stage.document.get("snapshot")
    return (
        isinstance(snapshot, dict)
        and snapshot.get("repository", "").lower() == request["repository"].lower()
        and snapshot.get("pr_number") == request["pr_number"]
    )


def _validate_prepare_snapshot(values: object) -> dict:
    """Check the values the workflow copied from the prepare job's outputs."""
    if not isinstance(values, dict) or set(values) != set(PREPARE_SNAPSHOT_KEYS):
        raise FinalizeError("prepare snapshot values are incomplete")
    for key in ("head_sha", "base_sha", "merge_base_sha"):
        if not isinstance(values[key], str) or not re.fullmatch(r"[0-9a-f]{40}", values[key]):
            raise FinalizeError(f"prepare {key} is not a 40-hex SHA")
    for key in ("snapshot_id", "diff_sha256"):
        if not isinstance(values[key], str) or not _SHA256_RE.fullmatch(values[key]):
            raise FinalizeError(f"prepare {key} is not a 64-hex digest")
    if values["policy_source"] not in limits_mod.POLICY_SOURCES:
        raise FinalizeError("prepare policy_source is not a known source")
    for key in ("policy_present", "is_fork"):
        if not isinstance(values[key], bool):
            raise FinalizeError(f"prepare {key} is not a boolean")
    return dict(values)


# -- merging ------------------------------------------------------------------


def _entry(
    *,
    origin: str,
    claude_index: int | None,
    title: str,
    detail: str,
    severity: str,
    confidence: str,
    category: str,
    path: str,
    line: object,
    rationale: str | None,
    suggested_fix: str | None,
    duplicate_of: int | None,
) -> dict:
    return {
        "origin": origin,
        "claude_index": claude_index,
        "title": title,
        "detail": detail,
        "severity": severity,
        "confidence": confidence,
        "category": category,
        "path": path,
        "line": line if isinstance(line, int) and not isinstance(line, bool) else None,
        "rationale": rationale,
        "suggested_fix": suggested_fix,
        "duplicate_of": duplicate_of,
    }


UNVERIFIED_RATIONALE = "検証stageがこの指摘に対する判定を返さなかったため、未検証として保留する。"
STAGE_FAILED_RATIONALE = "検証stageが失敗したため、この指摘は未検証である。問題の有無は確定していない。"

_BUCKET_BY_STATUS = {
    "adopted": "adopted",
    "duplicate": "duplicates",
    "rejected": "rejected",
    "deferred": "deferred",
}


def _unique_limited(values: list[str], maximum: int) -> list[str]:
    """Deduplicate in source order and keep the final schema within its limit."""
    if maximum <= 0:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= maximum:
            break
    return result


def merge_review(
    claude: dict | None,
    codex: dict | None,
    *,
    limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS,
) -> tuple[dict, int]:
    """Merge both stages into the final buckets. Returns (review, dropped)."""
    buckets: dict[str, list[dict]] = {name: [] for name in result_mod.BUCKETS}
    insufficient: list[str] = []
    limitations: list[str] = []
    dropped = 0
    summary_parts: list[str] = []

    findings = []
    if isinstance(claude, dict):
        review = claude.get("review")
        if isinstance(review, dict) and isinstance(review.get("findings"), list):
            findings = review["findings"]
        if isinstance(review, dict):
            if isinstance(review.get("summary"), str) and review["summary"].strip():
                summary_parts.append("一次レビュー: " + review["summary"].strip())
            for item in review.get("limitations") or []:
                if isinstance(item, str) and item.strip():
                    limitations.append(item.strip())
        normalization = claude.get("normalization")
        if isinstance(normalization, dict) and isinstance(normalization.get("dropped_findings"), list):
            dropped += len(normalization["dropped_findings"])

    claims: dict[int, dict] = {}
    if isinstance(codex, dict):
        verification = codex.get("verification")
        if isinstance(verification, dict):
            for claim in verification.get("claim_reviews") or []:
                if isinstance(claim, dict) and isinstance(claim.get("claude_index"), int):
                    claims[claim["claude_index"]] = claim
            if isinstance(verification.get("summary"), str) and verification["summary"].strip():
                summary_parts.append("再検証: " + verification["summary"].strip())
            for item in verification.get("insufficient_context") or []:
                if isinstance(item, str) and item.strip():
                    insufficient.append(item.strip())
            for item in verification.get("limitations") or []:
                if isinstance(item, str) and item.strip():
                    limitations.append(item.strip())
            for finding in verification.get("additional_findings") or []:
                if not isinstance(finding, dict):
                    continue
                buckets["added"].append(
                    _entry(
                        origin="codex",
                        claude_index=None,
                        title=str(finding.get("title", "")),
                        detail=str(finding.get("detail", "")),
                        severity=str(finding.get("severity", "")),
                        confidence=str(finding.get("confidence", "")),
                        category=str(finding.get("category", "")),
                        path=str(finding.get("path", "")),
                        line=finding.get("line"),
                        rationale=None,
                        suggested_fix=finding.get("suggested_fix"),
                        duplicate_of=None,
                    )
                )
        normalization = codex.get("normalization")
        if isinstance(normalization, dict):
            for key in ("dropped_claim_reviews", "dropped_findings"):
                value = normalization.get(key)
                if isinstance(value, list):
                    dropped += len(value)

    verification_ran = bool(claims) or isinstance(codex, dict)
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            dropped += 1
            continue
        claim = claims.get(index)
        if claim is None:
            # Every primary finding must end up with a state. An unverified one
            # is deferred, never dropped and never shown as resolved.
            bucket = "deferred"
            severity = str(finding.get("severity", ""))
            confidence = str(finding.get("confidence", ""))
            rationale = UNVERIFIED_RATIONALE if verification_ran else STAGE_FAILED_RATIONALE
            suggested_fix = None
            duplicate_of = None
            if verification_ran:
                dropped += 1
        else:
            bucket = _BUCKET_BY_STATUS[claim["status"]]
            severity = str(claim.get("severity", ""))
            confidence = str(claim.get("confidence", ""))
            rationale = claim.get("rationale")
            suggested_fix = claim.get("suggested_fix")
            duplicate_of = claim.get("duplicate_of")
        buckets[bucket].append(
            _entry(
                origin="claude",
                claude_index=index,
                title=str(finding.get("title", "")),
                detail=str(finding.get("detail", "")),
                severity=severity,
                confidence=confidence,
                category=str(finding.get("category", "")),
                path=str(finding.get("path", "")),
                line=finding.get("line"),
                rationale=rationale,
                suggested_fix=suggested_fix,
                duplicate_of=duplicate_of,
            )
        )

    review = {
        "summary": " / ".join(summary_parts),
        "insufficient_context": insufficient,
        "limitations": _unique_limited(limitations, limits.max_limitations),
        "dropped": dropped,
        "redactions": 0,
    }
    review.update(buckets)
    return review, dropped


def _reference_consistency(claude: StageInput, codex: StageInput) -> bool:
    """Require exactly one verification claim for every normalized Claude finding."""
    if claude.status != "success" or codex.status != "success":
        return False
    claude_review = claude.document.get("review") if isinstance(claude.document, dict) else None
    verification = codex.document.get("verification") if isinstance(codex.document, dict) else None
    findings = claude_review.get("findings") if isinstance(claude_review, dict) else None
    claims = verification.get("claim_reviews") if isinstance(verification, dict) else None
    if not isinstance(findings, list) or not isinstance(claims, list):
        return False
    indices = [
        claim.get("claude_index")
        for claim in claims
        if isinstance(claim, dict)
        and isinstance(claim.get("claude_index"), int)
        and not isinstance(claim.get("claude_index"), bool)
    ]
    expected = set(range(len(findings)))
    return len(indices) == len(expected) and set(indices) == expected


def _failure_summary(claude: StageInput, codex: StageInput) -> str:
    parts = [
        f"claude={claude.status}",
        f"codex={codex.status}",
    ]
    reasons = [stage.detail for stage in (claude, codex) if stage.detail]
    text = "二段階レビューは完了しなかった（" + ", ".join(parts) + "）。"
    if reasons:
        text += " 理由: " + " / ".join(reasons)
    text += " 問題がないことを意味しない。"
    return text


# -- orchestration ------------------------------------------------------------


def finalize(
    *,
    repository: str,
    pr_number: str | int,
    output_mode: str,
    claude_model: str,
    codex_model: str,
    claude_effort: str,
    codex_effort: str,
    policy_path: str,
    prepare_snapshot: dict,
    claude_result_file: Path | None,
    codex_result_file: Path | None,
    claude_job_result: str,
    codex_job_result: str,
    output_dir: Path,
    limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS,
    run_env: dict | None = None,
) -> dict:
    run_env = os.environ if run_env is None else run_env
    prepare_snapshot = _validate_prepare_snapshot(prepare_snapshot)
    expected_snapshot_id = prepare_snapshot["snapshot_id"]

    request = {
        "repository": repository,
        "pr_number": int(pr_number),
        "output_mode": models_mod.validate_output_mode(output_mode),
        "claude_model_requested": models_mod.validate_claude_model(claude_model),
        "codex_model_requested": models_mod.validate_codex_model(codex_model),
        "claude_effort": models_mod.validate_claude_effort(claude_effort),
        "codex_effort": models_mod.validate_codex_effort(codex_effort),
        "policy_path": policy_path,
    }

    claude_stage = StageInput("claude", claude_job_result, claude_result_file, request["claude_model_requested"])
    codex_stage = StageInput("codex", codex_job_result, codex_result_file, request["codex_model_requested"])
    _load_stage(
        claude_stage,
        expected_keys=limits_mod.NORMALIZED_RESULT_KEYS,
        limits=limits,
        validator=lambda document: result_mod.validate_normalized_claude_document(document, limits),
    )
    claude_count = (
        len(claude_stage.document["review"]["findings"])
        if claude_stage.status == "success" and isinstance(claude_stage.document, dict)
        else 0
    )
    _load_stage(
        codex_stage,
        expected_keys=limits_mod.CODEX_NORMALIZED_RESULT_KEYS,
        limits=limits,
        validator=lambda document: result_mod.validate_normalized_codex_document(
            document, claude_count=claude_count, limits=limits
        ),
    )

    if claude_stage.status == "success" and not _request_snapshot_ok(claude_stage, request):
        claude_stage.status = "invalid"
        claude_stage.detail = "primary review belongs to a different repository or pull request"
        claude_stage.document = None
    if codex_stage.status == "success" and not _request_snapshot_ok(codex_stage, request):
        codex_stage.status = "invalid"
        codex_stage.detail = "verification result belongs to a different repository or pull request"
        codex_stage.document = None

    claude_match = _fingerprint_ok(claude_stage, prepare_snapshot)
    codex_match = _fingerprint_ok(codex_stage, prepare_snapshot)
    if claude_stage.status == "success" and not claude_match:
        claude_stage.status = "invalid"
        claude_stage.detail = "primary review belongs to a different snapshot"
        claude_stage.document = None
    if codex_stage.status == "success" and not codex_match:
        codex_stage.status = "invalid"
        codex_stage.detail = "verification result belongs to a different snapshot"
        codex_stage.document = None

    snapshot = _snapshot_block(claude_stage, codex_stage, request, prepare_snapshot)

    both_ok = claude_stage.status == "success" and codex_stage.status == "success"
    review, _dropped = merge_review(claude_stage.document, codex_stage.document, limits=limits)
    if not review["summary"]:
        review["summary"] = _failure_summary(claude_stage, codex_stage)
    if not both_ok:
        # Surface the failure in the summary even when partial text exists.
        review["summary"] = _failure_summary(claude_stage, codex_stage) + " " + review["summary"]

    verification = {
        "schema_valid": both_ok,
        "snapshot_match": bool(snapshot) and claude_match and codex_match,
        "claude_fingerprint_match": claude_match,
        "codex_fingerprint_match": codex_match,
        "reference_consistency": _reference_consistency(claude_stage, codex_stage),
        "finalized": True,
    }

    document = {
        "final_schema_version": limits_mod.FINAL_SCHEMA_VERSION,
        "framework_version": limits_mod.FRAMEWORK_VERSION,
        "request": request,
        "snapshot": snapshot,
        "stages": {"claude": claude_stage.as_stage(), "codex": codex_stage.as_stage()},
        "verification": verification,
        "review": review,
        "publishable": both_ok and all(verification.values()),
    }

    review["redactions"] = result_mod.redact_document(document)
    # The finalizer validates its own output: a document that cannot pass the
    # publisher's checks must not be written at all.
    document = result_mod.validate_final_document(document, limits)

    payload = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    limits_mod.check_limit("final result size", len(payload), limits.max_final_result_bytes)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / FINAL_NAME).write_bytes(payload)
    summary_markdown = render_mod.render_summary(document, limits)
    (output_dir / SUMMARY_NAME).write_text(summary_markdown, encoding="utf-8")

    log(
        "final result written: publishable={p} claude={c} codex={x} entries={e}".format(
            p=document["publishable"],
            c=claude_stage.status,
            x=codex_stage.status,
            e=sum(len(document["review"][b]) for b in result_mod.BUCKETS),
        )
    )

    github_output = run_env.get("GITHUB_OUTPUT")
    if github_output:
        _write_github_output(
            github_output,
            {
                "publishable": "true" if document["publishable"] else "false",
                "output_mode": request["output_mode"],
                "snapshot_id": document["snapshot"]["snapshot_id"],
                "head_sha": document["snapshot"]["head_sha"],
                "claude_status": claude_stage.status,
                "codex_status": codex_stage.status,
            },
        )
    return document


def _snapshot_block(claude: StageInput, codex: StageInput, request: dict, prepare_snapshot: dict) -> dict:
    """Take the snapshot from a stage that matched, or from prepare's own outputs.

    A stage result is used only when it agrees with every value prepare
    published. When no stage matched (both failed, say), the snapshot is built
    from prepare's outputs, so a failed run still says which commit it was about.
    The model-facing fields it cannot know (path hashes, policy blob) stay empty.
    """
    for stage in (claude, codex):
        if stage.status == "success" and isinstance(stage.document, dict):
            snapshot = stage.document.get("snapshot")
            if isinstance(snapshot, dict) and all(
                snapshot.get(key) == prepare_snapshot[key] for key in PREPARE_SNAPSHOT_KEYS
            ):
                return dict(snapshot)
    return {
        "repository": request["repository"],
        "pr_number": request["pr_number"],
        "base_sha": prepare_snapshot["base_sha"],
        "head_sha": prepare_snapshot["head_sha"],
        "merge_base_sha": prepare_snapshot["merge_base_sha"],
        "diff_sha256": prepare_snapshot["diff_sha256"],
        "policy_commit_sha": None,
        "policy_blob_sha": None,
        "policy_source": prepare_snapshot["policy_source"],
        "policy_present": prepare_snapshot["policy_present"],
        "is_fork": prepare_snapshot["is_fork"],
        "snapshot_id": prepare_snapshot["snapshot_id"],
        "reviewable_path_hashes": [],
    }


def _write_github_output(path: str, values: dict) -> None:
    lines = []
    for key, value in values.items():
        text = str(value)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or "\n" in text or "\r" in text:
            raise FinalizeError(f"refusing to write unsafe GITHUB_OUTPUT entry {key}")
        lines.append(f"{key}={text}\n")
    with open(path, "a", encoding="utf-8") as fh:
        fh.writelines(lines)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr-number", required=True)
    parser.add_argument("--output-mode", required=True)
    parser.add_argument("--claude-model", required=True)
    parser.add_argument("--codex-model", required=True)
    parser.add_argument("--claude-effort", required=True)
    parser.add_argument("--codex-effort", required=True)
    parser.add_argument("--policy-path", required=True)
    # Values copied from the prepare job's outputs, outside every artifact.
    parser.add_argument("--expected-snapshot-id", required=True)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--merge-base-sha", required=True)
    parser.add_argument("--diff-sha256", required=True)
    parser.add_argument("--policy-source", required=True)
    parser.add_argument("--policy-present", required=True, choices=("true", "false"))
    parser.add_argument("--is-fork", required=True, choices=("true", "false"))
    parser.add_argument("--claude-result-file", default="")
    parser.add_argument("--codex-result-file", default="")
    parser.add_argument("--claude-job-result", default="failure")
    parser.add_argument("--codex-job-result", default="failure")
    parser.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        finalize(
            repository=args.repository,
            pr_number=args.pr_number,
            output_mode=args.output_mode,
            claude_model=args.claude_model,
            codex_model=args.codex_model,
            claude_effort=args.claude_effort,
            codex_effort=args.codex_effort,
            policy_path=args.policy_path,
            prepare_snapshot={
                "snapshot_id": args.expected_snapshot_id,
                "head_sha": args.head_sha,
                "base_sha": args.base_sha,
                "merge_base_sha": args.merge_base_sha,
                "diff_sha256": args.diff_sha256,
                "policy_source": args.policy_source,
                "policy_present": args.policy_present == "true",
                "is_fork": args.is_fork == "true",
            },
            claude_result_file=Path(args.claude_result_file) if args.claude_result_file else None,
            codex_result_file=Path(args.codex_result_file) if args.codex_result_file else None,
            claude_job_result=args.claude_job_result,
            codex_job_result=args.codex_job_result,
            output_dir=args.output_dir,
        )
    except (
        FinalizeError,
        result_mod.ResultError,
        models_mod.ModelNotAllowed,
        render_mod.RenderError,
        limits_mod.LimitExceeded,
        ValueError,
    ) as err:
        log(f"stop: {err}")
        return EXIT_STOP
    except Exception as err:  # noqa: BLE001 - last-resort guard, message only
        log(f"unexpected error: {err.__class__.__name__}")
        return EXIT_UNEXPECTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
