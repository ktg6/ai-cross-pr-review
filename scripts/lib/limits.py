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
FRAMEWORK_VERSION = "0.2.0"
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

# Model provider recorded in the trusted wrapper. The model itself never sets it.
REVIEW_PROVIDER = "anthropic-claude-code-cli"

KIB = 1024


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
