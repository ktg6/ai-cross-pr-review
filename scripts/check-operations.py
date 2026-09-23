#!/usr/bin/env python3
"""validate_request step: operational health check (ADR-0010).

Two checks, both warnings only:

- Credential expiry. GitHub does not expose when a Secret expires, so operators
  record the expiry (or rotation due date) of each credential in a repository
  variable. A date that has passed, or is within
  ``Limits.credential_expiry_warning_days``, is reported. A credential without a
  recorded date is listed as unmonitored.
- Model allowlist freshness. The models selected for this run must have been
  re-confirmed in the official docs within
  ``Limits.model_verification_max_age_days`` (see ``lib/models.py``).

Neither check stops the review. An expired credential already fails closed at
the stage that uses it, and a retired model ID fails closed at the provider;
this step only makes those failures visible before they happen. The findings go
to the job summary and to workflow annotations, never to the PR.

The step reads dates, not credentials. It holds no Secret and talks to nothing.

Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import limits as limits_mod  # noqa: E402
from lib import models as models_mod  # noqa: E402

EXIT_OK = 0
EXIT_UNEXPECTED = 1

HEADING = "## AI Cross Review: 運用チェック"

# (argument name, label, repository variable that carries the date). The label
# and the variable name are fixed text; the date itself is never echoed back.
CREDENTIALS: tuple[tuple[str, str, str], ...] = (
    ("read_token", "read token (AI_REVIEW_READ_TOKEN)", "AI_REVIEW_READ_TOKEN_EXPIRES_ON"),
    ("comment_token", "comment token (AI_REVIEW_COMMENT_TOKEN)", "AI_REVIEW_COMMENT_TOKEN_EXPIRES_ON"),
    ("claude_token", "Claude token (CLAUDE_CODE_OAUTH_TOKEN)", "AI_REVIEW_CLAUDE_TOKEN_EXPIRES_ON"),
    ("openai_key", "OpenAI key (OPENAI_API_KEY)", "AI_REVIEW_OPENAI_KEY_EXPIRES_ON"),
)

_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# Statuses. Only the WARNING_STATUSES become workflow annotations.
OK = "ok"
EXPIRING = "expiring"
EXPIRED = "expired"
UNSET = "unset"
INVALID = "invalid"
STALE = "stale"
UNRECORDED = "unrecorded"
WARNING_STATUSES = frozenset({EXPIRING, EXPIRED, INVALID, STALE, UNRECORDED})


@dataclass(frozen=True)
class Check:
    label: str
    status: str
    message: str


def log(message: str) -> None:
    sys.stderr.write(f"check-operations: {message}\n")


def parse_date(value: object) -> dt.date | None:
    """Parse a strict YYYY-MM-DD date; anything else is None."""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        return None


def check_credential(label: str, variable: str, value: str, today: dt.date, limits: limits_mod.Limits) -> Check:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return Check(label, UNSET, f"期限が未設定（repository variable `{variable}`）。期限監視の対象外")
    expires = parse_date(text)
    if expires is None:
        # The raw value is not shown: it is operator text, and a Secret pasted into
        # the wrong field must not end up in the summary.
        return Check(label, INVALID, f"`{variable}`がYYYY-MM-DD形式でない。期限を判定できない")
    remaining = (expires - today).days
    if remaining < 0:
        return Check(label, EXPIRED, f"期限切れ（{expires.isoformat()}、{-remaining}日経過）。rotationが必要")
    if remaining <= limits.credential_expiry_warning_days:
        return Check(label, EXPIRING, f"期限まで{remaining}日（{expires.isoformat()}）。rotationを準備する")
    return Check(label, OK, f"期限まで{remaining}日（{expires.isoformat()}）")


def check_model(model_id: str, today: dt.date, limits: limits_mod.Limits) -> Check:
    label = f"model {model_id}"
    entry = models_mod.model_entry(model_id)
    if entry is None:
        # validate-request.py has already refused such a model; stay defensive.
        return Check(label, INVALID, "allowlistに存在しない")
    verified = parse_date(entry.verified_on)
    if verified is None:
        return Check(label, UNRECORDED, "公式docsでの確認日が未記録。公式docsで再確認し、lib/models.pyへ記録する")
    age = (today - verified).days
    if age > limits.model_verification_max_age_days:
        return Check(
            label,
            STALE,
            f"公式docsでの最終確認から{age}日経過（{verified.isoformat()}）。再確認期限"
            f"{limits.model_verification_max_age_days}日を超過",
        )
    return Check(label, OK, f"公式docsでの最終確認: {verified.isoformat()}（{age}日前）")


def run_checks(
    *,
    expiries: dict[str, str],
    models: list[str],
    today: dt.date,
    limits: limits_mod.Limits = limits_mod.DEFAULT_LIMITS,
) -> list[Check]:
    checks = [
        check_credential(label, variable, expiries.get(key, ""), today, limits)
        for key, label, variable in CREDENTIALS
    ]
    checks += [check_model(model, today, limits) for model in models]
    return checks


def render(checks: list[Check], today: dt.date) -> str:
    lines = [
        HEADING,
        "",
        f"- 判定日（UTC）: `{today.isoformat()}`",
        "- 警告はレビューを停止しない。期限切れのcredentialや廃止されたmodelは、使用するstageで失敗として現れる。",
        "",
    ]
    for check in checks:
        mark = "⚠️" if check.status in WARNING_STATUSES else "-"
        lines.append(f"{mark} {check.label}: `{check.status}` {check.message}")
    return "\n".join(lines) + "\n"


def annotations(checks: list[Check]) -> str:
    # Workflow commands are line oriented; every part of the text is fixed or
    # derived from a validated date, so it cannot contain a newline or "::".
    return "".join(
        f"::warning title=AI Cross Review operations::{check.label}: {check.message}\n"
        for check in checks
        if check.status in WARNING_STATUSES
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    for key, _label, _variable in CREDENTIALS:
        parser.add_argument(f"--{key.replace('_', '-')}-expires-on", dest=key, default="")
    parser.add_argument("--claude-model", required=True)
    parser.add_argument("--codex-model", required=True)
    parser.add_argument("--today", default="", help="YYYY-MM-DD; defaults to the current UTC date")
    parser.add_argument("--summary-file", default="", help="defaults to $GITHUB_STEP_SUMMARY")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        today = parse_date(args.today) if args.today else dt.datetime.now(dt.timezone.utc).date()
        if today is None:
            log("stop: --today is not a YYYY-MM-DD date")
            return EXIT_UNEXPECTED
        checks = run_checks(
            expiries={key: getattr(args, key) for key, _label, _variable in CREDENTIALS},
            models=[args.claude_model, args.codex_model],
            today=today,
        )
        summary_path = args.summary_file or os.environ.get("GITHUB_STEP_SUMMARY") or ""
        text = render(checks, today)
        if summary_path:
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write(text)
        else:
            sys.stderr.write(text)
        sys.stdout.write(annotations(checks))
        warned = sum(1 for check in checks if check.status in WARNING_STATUSES)
        log(f"checked: {len(checks)} items, {warned} warnings")
    except Exception as err:  # noqa: BLE001 - last-resort guard, message only
        log(f"unexpected error: {err.__class__.__name__}")
        return EXIT_UNEXPECTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
