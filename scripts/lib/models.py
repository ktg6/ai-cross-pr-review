"""Model and effort allowlists for the two-stage review (ADR-0007).

Free-text model names are forbidden. The workflow's ``choice`` inputs are a
first line of defence, but they are specific to one entry point; these lists are
the authority because every entry point must pass through them.

Only model IDs verified against the providers' official model documentation
appear here. Adding an ID without that verification is prohibited by AGENTS.md.

Standard library only.
"""

from __future__ import annotations

# Claude models usable by the pinned Claude Code CLI adapter. Both IDs are listed
# in Anthropic's official model documentation and support every effort level in
# CLAUDE_EFFORTS. claude-haiku-4-5 is deliberately absent: it does not accept an
# effort setting, and its behaviour under the CLI's --effort flag is unverified.
CLAUDE_MODELS: tuple[str, ...] = (
    "claude-opus-5",
    "claude-sonnet-5",
)
DEFAULT_CLAUDE_MODEL = "claude-opus-5"

# Claude Code CLI --effort levels.
CLAUDE_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
DEFAULT_CLAUDE_EFFORT = "high"

# OpenAI models usable through the Responses API for the verification stage.
# Verified against https://developers.openai.com/api/docs/models on 2026-09-21.
# Keep unverified IDs out of both this list and the workflow choices.
CODEX_MODELS: tuple[str, ...] = (
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"

# Responses API reasoning.effort levels. "none" is deliberately excluded: the
# verification stage must reason.
CODEX_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
DEFAULT_CODEX_EFFORT = "high"

# Output modes. summary_only is the safe default: it needs no comment token.
OUTPUT_MODES: tuple[str, ...] = ("summary_only", "pr_comment")
DEFAULT_OUTPUT_MODE = "summary_only"


class ModelNotAllowed(ValueError):
    """A requested model or effort is not on the allowlist. Deterministic stop."""


def _check(value: object, allowed: tuple[str, ...], what: str) -> str:
    # ``value`` arrives from a workflow input, so it is only half-trusted: it is
    # chosen by the operator, not by a model, but it is still free text on the
    # wire. Compare by identity against the allowlist, never by pattern.
    if not isinstance(value, str) or value not in allowed:
        raise ModelNotAllowed(f"{what} is not on the allowlist")
    return value


def validate_claude_model(value: object) -> str:
    return _check(value, CLAUDE_MODELS, "claude model")


def validate_claude_effort(value: object) -> str:
    return _check(value, CLAUDE_EFFORTS, "claude effort")


def validate_codex_model(value: object) -> str:
    return _check(value, CODEX_MODELS, "codex model")


def validate_codex_effort(value: object) -> str:
    return _check(value, CODEX_EFFORTS, "codex effort")


def validate_output_mode(value: object) -> str:
    return _check(value, OUTPUT_MODES, "output mode")
