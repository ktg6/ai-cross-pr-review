"""Model and effort allowlists for the two-stage review (ADR-0007).

Free-text model names are forbidden. The workflow's ``choice`` inputs are a
first line of defence, but they are specific to one entry point; these lists are
the authority because every entry point must pass through them.

Only model IDs verified against the providers' official model documentation
appear here. Adding an ID without that verification is prohibited by AGENTS.md.

Lifecycle (ADR-0010): every entry records the date on which its ID was last
confirmed in the official documentation. An entry whose date is missing or older
than ``Limits.model_verification_max_age_days`` is reported as due for
re-verification in the job summary. The check only warns: a retired ID already
fails closed, because the provider rejects it and the stage is recorded as
failed. Updates follow "official docs → this file → workflow choices → ADR
References → tests", and an unverified ID is never added.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelEntry:
    """One allowlisted model ID and the provenance of its verification."""

    model_id: str
    # ISO date (YYYY-MM-DD) of the last confirmation in the official docs, or
    # None when the date was not recorded at the time the ID was added.
    verified_on: str | None
    # Where the ID was confirmed, or None when it was not recorded.
    reference: str | None


# Claude models usable by the pinned Claude Code CLI adapter. Both IDs are listed
# in Anthropic's official model documentation and support every effort level in
# CLAUDE_EFFORTS. claude-haiku-4-5 is deliberately absent: it does not accept an
# effort setting, and its behaviour under the CLI's --effort flag is unverified.
# The verification date and source page were not recorded when these IDs were
# added, so they are left as None rather than back-filled with a guess.
CLAUDE_MODEL_ENTRIES: tuple[ModelEntry, ...] = (
    ModelEntry("claude-opus-5", None, None),
    ModelEntry("claude-sonnet-5", None, None),
)
CLAUDE_MODELS: tuple[str, ...] = tuple(entry.model_id for entry in CLAUDE_MODEL_ENTRIES)
DEFAULT_CLAUDE_MODEL = "claude-opus-5"

# Claude Code CLI --effort levels.
CLAUDE_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
DEFAULT_CLAUDE_EFFORT = "high"

# OpenAI models for the verification stage, run through the Codex CLI with a
# ChatGPT subscription sign-in (ADR-0012). The dates record confirmation in the
# official Models documentation. Availability also depends on the ChatGPT plan:
# gpt-5.6-sol was confirmed through the pinned CLI with a Plus sign-in on
# 2026-09-25; an ID the plan does not serve fails the stage closed.
# Keep unverified IDs out of both this list and the workflow choices.
_OPENAI_MODELS_DOC = "https://developers.openai.com/api/docs/models"
CODEX_MODEL_ENTRIES: tuple[ModelEntry, ...] = (
    ModelEntry("gpt-6-astra", "2026-09-21", _OPENAI_MODELS_DOC),
    ModelEntry("gpt-5.6-sol", "2026-09-21", _OPENAI_MODELS_DOC),
    ModelEntry("gpt-5.6-terra", "2026-09-21", _OPENAI_MODELS_DOC),
    ModelEntry("gpt-5.6-luna", "2026-09-21", _OPENAI_MODELS_DOC),
)
CODEX_MODELS: tuple[str, ...] = tuple(entry.model_id for entry in CODEX_MODEL_ENTRIES)
DEFAULT_CODEX_MODEL = "gpt-5.6-sol"

# Reasoning effort levels (the CLI's model_reasoning_effort). "none" is
# deliberately excluded: the verification stage must reason.
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


def model_entry(model_id: str) -> ModelEntry | None:
    """Return the allowlist entry for ``model_id``, or None when it is not listed."""
    for entry in CLAUDE_MODEL_ENTRIES + CODEX_MODEL_ENTRIES:
        if entry.model_id == model_id:
            return entry
    return None
