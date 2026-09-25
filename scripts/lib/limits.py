"""Framework-wide constants: versions, input limits, forbidden paths.

Standard library only. Every limit here is a hard stop: when an input exceeds a
limit the run fails without producing a bundle, so that a partial snapshot can
never be reviewed and reported as "no issues".
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, asdict

# Framework identity. Recorded in every bundle so downstream jobs can verify
# they are consuming output from a known producer.
FRAMEWORK_VERSION = "0.5.0"
BUNDLE_SCHEMA_VERSION = "1"
# Version of the model-output schema (schemas/review-result.schema.json) and of
# the normalized result written by scripts/normalize-review.py.
RESULT_SCHEMA_VERSION = "1"
# Pinned Claude Code CLI version. The review job refuses any other version.
# Keep in sync with actions/review-runtime/action.yml (AI_REVIEW_CLAUDE_VERSION).
CLAUDE_CODE_VERSION = "2.1.263"

# SHA-256 of the pinned release binary, from the GPG-signed release manifest at
# https://downloads.claude.ai/claude-code-releases/2.1.263/manifest.json .
# The review step verifies the installed binary against this map and stops when
# the platform is unknown or the digest differs.
CLAUDE_CODE_SHA256: dict[str, str] = {
    "linux-x64": "26d020351e8112f4006790f3cfce43b4c9df0c1bb1d0e542364d64151b81d5ba",
    "linux-arm64": "7d25d7c8ae6c6e009cc7dae4e817f674179fd31fb7761bcd56fee4c2902b4c03",
    "darwin-x64": "a94a8b229fa85c3a316c6b4a35e0aa22bec1aabbd3d1422826ce1d10ddc88751",
    "darwin-arm64": "ef5d2909c8af49f31ab6d5487e90316777bc2fac170adfe8160716caa8aaf4f9",
}

# Model providers recorded in the trusted wrapper. A model never sets its own.
REVIEW_PROVIDER = "anthropic-claude-code-cli"
CODEX_PROVIDER = "openai-codex-cli"

# Pinned Codex CLI version for the verification stage (ADR-0012). The CLI is
# installed and signed in by the operator of the self-hosted runner (or by the
# local CLI user); the stage refuses any other version.
CODEX_CLI_VERSION = "0.155.1"
# The only accepted Codex sign-in: a ChatGPT subscription. An API-key sign-in
# (usage-based billing) is refused before the model is called.
CODEX_AUTH_MODE = "chatgpt"

# Version of the final, merged result document written by finalize-review.py.
FINAL_SCHEMA_VERSION = "2"

# Where the review policy came from. Recorded in every result.
POLICY_SOURCES: tuple[str, ...] = ("repository", "central_default")
# Central fallback policy, relative to the repository root.
DEFAULT_POLICY_RELPATH = "policies/default-review-policy.md"

KIB = 1024

# Vocabulary of the review-result schema (schemas/review-result.schema.json).
# Both the normalizer and the publisher validate against these tuples so the two
# steps cannot drift apart.
SEVERITIES: tuple[str, ...] = ("high", "medium", "low")
CONFIDENCES: tuple[str, ...] = ("high", "medium", "low")
CATEGORIES: tuple[str, ...] = (
    "correctness",
    "security",
    "reliability",
    "maintainability",
    "testing",
    "other",
)
REQUIRED_FINDING_KEYS: tuple[str, ...] = ("title", "detail", "severity", "confidence", "category", "path")
OPTIONAL_FINDING_KEYS: tuple[str, ...] = ("line",)
RESULT_KEYS: tuple[str, ...] = ("schema_version", "summary", "findings", "limitations")
NORMALIZED_RESULT_KEYS: tuple[str, ...] = (
    "result_schema_version",
    "framework_version",
    "snapshot",
    "run",
    "normalization",
    "review",
)
SNAPSHOT_KEYS: tuple[str, ...] = (
    "repository",
    "pr_number",
    "base_sha",
    "head_sha",
    "merge_base_sha",
    "diff_sha256",
    "policy_commit_sha",
    "policy_blob_sha",
    "policy_source",
    "policy_present",
    "is_fork",
    "snapshot_id",
    "reviewable_path_hashes",
)
RUN_KEYS: tuple[str, ...] = (
    "provider",
    "cli_version",
    "model_requested",
    "model_reported",
    "effort",
    "tools_enabled",
    "run_id",
    "num_turns",
    "duration_ms",
    "input_tokens",
    "output_tokens",
    "cost_usd",
)
NORMALIZATION_KEYS: tuple[str, ...] = (
    "dropped_findings",
    "redactions",
    "excluded_files",
)
DROPPED_FINDING_KEYS: tuple[str, ...] = ("index", "reason")
MAX_FINDING_LINE = 1000000

# -- Verification stage (Codex) vocabulary (ADR-0006, ADR-0007) ---------------

# Status of each Claude finding after the verification stage.
CLAIM_STATUSES: tuple[str, ...] = ("adopted", "duplicate", "rejected", "deferred")

CODEX_RESULT_KEYS: tuple[str, ...] = (
    "schema_version",
    "summary",
    "claim_reviews",
    "additional_findings",
    "insufficient_context",
    "limitations",
)
CLAIM_REVIEW_KEYS: tuple[str, ...] = (
    "claude_index",
    "status",
    "severity",
    "confidence",
    "rationale",
    "suggested_fix",
    "duplicate_of",
)
CODEX_FINDING_KEYS: tuple[str, ...] = (
    "title",
    "detail",
    "severity",
    "confidence",
    "category",
    "path",
    "line",
    "suggested_fix",
)
CODEX_NORMALIZED_RESULT_KEYS: tuple[str, ...] = (
    "result_schema_version",
    "framework_version",
    "stage",
    "snapshot",
    "run",
    "normalization",
    "verification",
)
CODEX_RUN_KEYS: tuple[str, ...] = (
    "provider",
    "cli_version",
    "auth_mode",
    "model_requested",
    "model_reported",
    "effort",
    "tools_enabled",
    "run_id",
    "thread_id",
    "status",
    "input_tokens",
    "output_tokens",
    "duration_ms",
)
CODEX_NORMALIZATION_KEYS: tuple[str, ...] = (
    "dropped_claim_reviews",
    "dropped_findings",
    "redactions",
)

# -- Final merged document (ADR-0005, ADR-0006) -------------------------------

# Execution state of one stage. "skipped" means the job never ran because an
# earlier stage failed; it is never rendered as "no issues".
STAGE_STATUSES: tuple[str, ...] = ("success", "failed", "skipped", "missing", "invalid")

FINAL_RESULT_KEYS: tuple[str, ...] = (
    "final_schema_version",
    "framework_version",
    "request",
    "snapshot",
    "stages",
    "verification",
    "review",
    "publishable",
)
FINAL_REQUEST_KEYS: tuple[str, ...] = (
    "repository",
    "pr_number",
    "output_mode",
    "claude_model_requested",
    "codex_model_requested",
    "claude_effort",
    "codex_effort",
    "policy_path",
)
FINAL_STAGE_KEYS: tuple[str, ...] = (
    "status",
    "model_requested",
    "model_reported",
    "detail",
    # Usage reported by the provider (ADR-0010). Null when the stage did not
    # produce a usable result or the provider did not report the value. Shown in
    # the job summary only, never in the PR comment.
    "input_tokens",
    "output_tokens",
    "cost_usd",
)
FINAL_VERIFICATION_KEYS: tuple[str, ...] = (
    "schema_valid",
    "snapshot_match",
    "claude_fingerprint_match",
    "codex_fingerprint_match",
    "reference_consistency",
    "finalized",
)
# One entry in any of the final review buckets. The same shape is used for
# adopted, added, deferred, rejected and duplicate entries so that the renderer
# and the publisher validate a single structure.
FINAL_ENTRY_KEYS: tuple[str, ...] = (
    "origin",
    "claude_index",
    "title",
    "detail",
    "severity",
    "confidence",
    "category",
    "path",
    "line",
    "rationale",
    "suggested_fix",
    "duplicate_of",
)
ENTRY_ORIGINS: tuple[str, ...] = ("claude", "codex")

FINAL_REVIEW_KEYS: tuple[str, ...] = (
    "summary",
    "adopted",
    "added",
    "deferred",
    "rejected",
    "duplicates",
    "insufficient_context",
    "limitations",
    "dropped",
    "redactions",
)


@dataclass(frozen=True)
class Limits:
    """Numeric limits from the implementation plan (section 6)."""

    max_changed_files: int = 100
    max_diff_total_bytes: int = 200 * KIB
    max_file_diff_bytes: int = 50 * KIB
    max_policy_bytes: int = 16 * KIB
    max_metadata_bytes: int = 8 * KIB
    max_result_bytes: int = 32 * KIB
    max_findings: int = 20
    claude_timeout_seconds: int = 600
    # Bounds that protect the prepare step itself.
    max_api_response_bytes: int = 4 * KIB * KIB
    # Blobs above this size are never downloaded by the prepare step.
    max_blob_bytes: int = 1024 * KIB
    git_fetch_timeout_seconds: int = 300
    git_command_timeout_seconds: int = 60
    max_title_chars: int = 256
    max_body_bytes: int = 6 * KIB
    max_labels: int = 20
    # Phase 2: review job (Claude Code CLI adapter) and result normalization.
    # レビューはtoolなしの単一turnで完了する。turn数超過はCLI側でエラーとする。
    claude_max_turns: int = 4
    claude_max_budget_usd: str = "5.00"
    # Upper bound for the CLI's JSON envelope on stdout; larger output is discarded unparsed.
    max_raw_result_bytes: int = 1024 * KIB
    max_summary_chars: int = 2000
    max_finding_title_chars: int = 200
    max_finding_detail_chars: int = 2000
    max_finding_path_chars: int = 512
    max_limitations: int = 10
    max_limitation_chars: int = 500
    # Phase 3: deterministic publisher.
    # GitHub rejects issue comments over 65536 characters; stay below it so the
    # rendered comment can never be truncated by the API.
    max_comment_chars: int = 60000
    max_comment_pages: int = 10
    publish_retry_attempts: int = 3
    publish_retry_delay_seconds: float = 2.0
    # Phase 5: verification stage and final merge. Phase 8 (ADR-0012): the stage
    # runs the Codex CLI, whose JSONL event stream on stdout is bounded here.
    codex_timeout_seconds: int = 600
    codex_max_event_stream_bytes: int = 4 * KIB * KIB
    max_claim_reviews: int = 20
    max_additional_findings: int = 20
    max_insufficient_context: int = 10
    max_rationale_chars: int = 2000
    max_suggested_fix_chars: int = 2000
    # The merged document carries both stages, so it is larger than either one.
    max_final_result_bytes: int = 256 * KIB
    # GitHub truncates a job summary above 1 MiB; stay well below it.
    max_job_summary_chars: int = 500000
    # Phase 6: operational checks (ADR-0010). Both only produce warnings.
    # A credential whose recorded expiry is this close is reported as expiring.
    credential_expiry_warning_days: int = 30
    # An allowlisted model ID not re-confirmed in the official docs within this
    # many days is reported as due for re-verification.
    model_verification_max_age_days: int = 90

    def as_dict(self) -> dict:
        return asdict(self)


DEFAULT_LIMITS = Limits()


class LimitExceeded(Exception):
    """An input exceeded a hard limit. The caller must stop, not degrade."""


def check_limit(name: str, value: int, maximum: int) -> None:
    if value > maximum:
        raise LimitExceeded(f"{name} exceeds limit: {value} > {maximum}")


# Paths that must never reach the AI input, matched against the final path
# component (case-insensitive) or against any directory segment.
FORBIDDEN_BASENAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    "*.tfstate",
    "*.tfstate.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "id_rsa",
    "id_rsa.*",
    "id_dsa",
    "id_dsa.*",
    "id_ecdsa",
    "id_ecdsa.*",
    "id_ed25519",
    "id_ed25519.*",
    ".netrc",
    "_netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
    "service-account*.json",
)

FORBIDDEN_DIR_SEGMENTS: frozenset[str] = frozenset(
    {".aws", ".ssh", ".gnupg", ".terraform", ".docker"}
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def forbidden_path_reason(path: str) -> str | None:
    """Return a reason string if ``path`` must be excluded, else None.

    ``path`` is a repository-relative POSIX path as emitted by git.
    """
    if not path or path.startswith("/") or path.startswith("\\"):
        return "absolute_path"
    segments = path.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        return "unsafe_path_segment"
    if _CONTROL_CHARS.search(path):
        return "control_characters_in_path"
    for seg in segments[:-1]:
        if seg.lower() in FORBIDDEN_DIR_SEGMENTS:
            return "forbidden_directory"
    base = segments[-1].lower()
    for pattern in FORBIDDEN_BASENAME_PATTERNS:
        if fnmatch.fnmatchcase(base, pattern):
            return "forbidden_filename"
    return None
