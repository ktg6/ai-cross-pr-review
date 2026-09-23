"""Tests for the operational health check (ADR-0010).

The check reads dates, never credentials, and only warns: it must not stop a
review, must not echo operator text it cannot validate, and must keep the model
allowlist's verification dates honest.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402

from lib import limits as limits_mod  # noqa: E402
from lib import models as models_mod  # noqa: E402

ops = support.load_script("check_operations", "check-operations.py")
ops.log = lambda message: None

TODAY = dt.date(2026, 9, 23)
LIMITS = limits_mod.DEFAULT_LIMITS
CANARY = "ghp_CANARYCANARYCANARYCANARYCANARY0000"


def credential(value: str) -> "ops.Check":
    return ops.check_credential("read token", "AI_REVIEW_READ_TOKEN_EXPIRES_ON", value, TODAY, LIMITS)


def days(n: int) -> str:
    return (TODAY + dt.timedelta(days=n)).isoformat()


class CredentialExpiryTests(unittest.TestCase):
    def test_far_expiry_is_ok(self):
        self.assertEqual(credential(days(LIMITS.credential_expiry_warning_days + 1)).status, ops.OK)

    def test_expiry_within_the_window_warns(self):
        for offset in (0, 1, LIMITS.credential_expiry_warning_days):
            with self.subTest(offset=offset):
                self.assertEqual(credential(days(offset)).status, ops.EXPIRING)

    def test_past_expiry_is_expired(self):
        check = credential(days(-3))
        self.assertEqual(check.status, ops.EXPIRED)
        self.assertIn("3日経過", check.message)

    def test_unset_is_reported_but_not_a_warning(self):
        for value in ("", "   "):
            with self.subTest(value=value):
                check = credential(value)
                self.assertEqual(check.status, ops.UNSET)
                self.assertNotIn(check.status, ops.WARNING_STATUSES)

    def test_malformed_dates_are_invalid_and_never_echoed(self):
        for value in ("2026-13-01", "2026-02-30", "26-09-23", "2026/09/23", "tomorrow", "2026-09-23T00:00", CANARY, "2026-09-23\n::error::x"):
            with self.subTest(value=value):
                check = credential(value)
                self.assertEqual(check.status, ops.INVALID)
                self.assertNotIn(value.strip(), check.message)
                self.assertNotIn(CANARY, ops.render([check], TODAY) + ops.annotations([check]))


class ModelFreshnessTests(unittest.TestCase):
    def test_recent_verification_is_ok(self):
        entry = models_mod.CODEX_MODEL_ENTRIES[0]
        today = dt.date.fromisoformat(entry.verified_on)
        self.assertEqual(ops.check_model(entry.model_id, today, LIMITS).status, ops.OK)

    def test_old_verification_is_stale(self):
        entry = models_mod.CODEX_MODEL_ENTRIES[0]
        today = dt.date.fromisoformat(entry.verified_on) + dt.timedelta(days=LIMITS.model_verification_max_age_days + 1)
        self.assertEqual(ops.check_model(entry.model_id, today, LIMITS).status, ops.STALE)

    def test_unrecorded_verification_is_a_warning(self):
        for entry in models_mod.CLAUDE_MODEL_ENTRIES:
            if entry.verified_on is None:
                with self.subTest(model=entry.model_id):
                    self.assertEqual(ops.check_model(entry.model_id, TODAY, LIMITS).status, ops.UNRECORDED)

    def test_unknown_model_is_invalid(self):
        self.assertEqual(ops.check_model("gpt-unknown", TODAY, LIMITS).status, ops.INVALID)


class AllowlistMetadataTests(unittest.TestCase):
    def test_every_entry_has_a_well_formed_provenance(self):
        today = dt.datetime.now(dt.timezone.utc).date()
        for entry in models_mod.CLAUDE_MODEL_ENTRIES + models_mod.CODEX_MODEL_ENTRIES:
            with self.subTest(model=entry.model_id):
                # A date and its source are recorded together or not at all.
                self.assertEqual(entry.verified_on is None, entry.reference is None)
                if entry.verified_on is not None:
                    verified = ops.parse_date(entry.verified_on)
                    self.assertIsNotNone(verified)
                    self.assertLessEqual(verified, today)
                    self.assertTrue(entry.reference.startswith("https://"))

    def test_the_id_tuples_are_derived_from_the_entries(self):
        self.assertEqual(models_mod.CLAUDE_MODELS, tuple(e.model_id for e in models_mod.CLAUDE_MODEL_ENTRIES))
        self.assertEqual(models_mod.CODEX_MODELS, tuple(e.model_id for e in models_mod.CODEX_MODEL_ENTRIES))
        ids = models_mod.CLAUDE_MODELS + models_mod.CODEX_MODELS
        self.assertEqual(len(ids), len(set(ids)))


class OutputTests(unittest.TestCase):
    def run_main(self, *extra: str) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = ops.main(
                    [
                        "--claude-model", "claude-opus-5",
                        "--codex-model", "gpt-5.6-sol",
                        "--today", TODAY.isoformat(),
                        "--summary-file", str(summary),
                        *extra,
                    ]
                )
            return code, out.getvalue(), summary.read_text("utf-8") if summary.exists() else ""

    def test_warnings_never_stop_the_run(self):
        code, annotations, summary = self.run_main(
            "--read-token-expires-on", days(-1),
            "--comment-token-expires-on", "garbage",
            "--claude-token-expires-on", days(5),
        )
        self.assertEqual(code, ops.EXIT_OK)
        self.assertIn(ops.HEADING, summary)
        self.assertIn("`expired`", summary)
        self.assertIn("`invalid`", summary)
        self.assertIn("`expiring`", summary)
        self.assertIn("`unset`", summary)
        # expired, invalid, expiring, plus the unrecorded Claude model date.
        self.assertEqual(annotations.count("::warning "), 4)

    def test_annotations_are_single_line_workflow_commands(self):
        _code, annotations, _summary = self.run_main("--read-token-expires-on", days(-1))
        for line in annotations.splitlines():
            self.assertTrue(line.startswith("::warning title=AI Cross Review operations::"), line)
            self.assertEqual(line.count("::"), 2)

    def test_all_clear_produces_no_annotation(self):
        far = days(365)
        code, annotations, summary = self.run_main(
            "--read-token-expires-on", far,
            "--comment-token-expires-on", far,
            "--claude-token-expires-on", far,
            "--openai-key-expires-on", far,
        )
        self.assertEqual(code, ops.EXIT_OK)
        # Only the Claude model's unrecorded verification date remains.
        claude = models_mod.model_entry("claude-opus-5")
        self.assertEqual(annotations.count("::warning "), 0 if claude.verified_on else 1)
        self.assertEqual(summary.count("`ok`"), 6 if claude.verified_on else 5)

    def test_the_step_holds_no_credential(self):
        source = (support.ROOT / "scripts" / "check-operations.py").read_text("utf-8")
        for name in ("urllib", "subprocess", "socket", "http.client"):
            self.assertNotIn(name, source)
        # The only environment variable it reads is the summary path.
        self.assertEqual(re.findall(r"os\.environ\.get\(\"([A-Z_]+)\"", source), ["GITHUB_STEP_SUMMARY"])


if __name__ == "__main__":
    unittest.main()
