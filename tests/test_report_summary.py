"""Tests for the report step: the job summary is shown for every run.

A run that produced no usable result must say so where the operator looks
first, and must never look like a review that found nothing.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402
from support import claude_document, codex_document, run_finalize  # noqa: E402

from lib import limits as limits_mod  # noqa: E402

report = support.load_script("report_summary", "report-summary.py")
report.log = lambda message: None


class ReportCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ai-review-report-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.summary = self.tmp / "summary.md"

    def final_file(self, **kwargs) -> Path:
        _document, out = run_finalize(self.tmp / "gen", **kwargs)
        return out / "final-review.json"

    def run_report(self, result_file: Path, **kwargs) -> bool:
        return report.report(result_file=result_file, summary_path=str(self.summary), **kwargs)


class ReportTests(ReportCase):
    def test_a_complete_result_is_rendered_into_the_summary(self):
        self.assertTrue(self.run_report(self.final_file()))
        text = self.summary.read_text("utf-8")
        self.assertIn("## AI Cross Review", text)
        self.assertIn("Codexが採用したClaudeの指摘 (1)", text)
        self.assertNotIn("<!-- ai-cross-pr-review", text, "the summary is never posted, so it carries no marker")

    def test_a_failed_run_is_reported_as_failed_not_as_clean(self):
        path = self.final_file(codex=None, codex_job="failure")
        self.assertTrue(self.run_report(path))
        text = self.summary.read_text("utf-8")
        self.assertIn("この実行は完了しなかった", text)
        self.assertIn("codex=failed", text)
        self.assertNotIn("報告すべきfindingはなかった", text)

    def test_missing_final_result_says_so_and_returns_false(self):
        self.assertFalse(self.run_report(self.tmp / "nope.json"))
        text = self.summary.read_text("utf-8")
        self.assertIn(report.FAILURE_HEADING, text)
        self.assertIn("問題なし", text)
        self.assertIn("ではない", text)

    def test_corrupt_or_invalid_final_result_says_so_and_returns_false(self):
        cases = {
            "not json": b"{oops",
            "wrong shape": b"[]",
            "unknown field": json.dumps({**json.loads(self.final_file().read_text("utf-8")), "extra": 1}).encode(),
        }
        for label, data in cases.items():
            with self.subTest(case=label):
                path = self.tmp / f"{label.replace(' ', '_')}.json"
                path.write_bytes(data)
                self.summary.write_text("", encoding="utf-8")
                self.assertFalse(self.run_report(path))
                self.assertIn(report.FAILURE_HEADING, self.summary.read_text("utf-8"))

    def test_oversized_final_result_is_reported_not_crashed(self):
        path = self.tmp / "big.json"
        path.write_bytes(b" " * (limits_mod.DEFAULT_LIMITS.max_final_result_bytes + 1))
        self.assertFalse(self.run_report(path))
        self.assertIn(report.FAILURE_HEADING, self.summary.read_text("utf-8"))

    def test_a_tampered_publishable_flag_cannot_produce_a_green_summary(self):
        path = self.final_file(codex=None, codex_job="failure")
        document = json.loads(path.read_text("utf-8"))
        document["publishable"] = True
        path.write_text(json.dumps(document), encoding="utf-8")
        self.assertFalse(self.run_report(path))
        self.assertIn(report.FAILURE_HEADING, self.summary.read_text("utf-8"))

    def test_summary_is_appended_not_overwritten(self):
        self.summary.write_text("earlier step output\n", encoding="utf-8")
        self.run_report(self.final_file())
        text = self.summary.read_text("utf-8")
        self.assertTrue(text.startswith("earlier step output\n"))
        self.assertIn("## AI Cross Review", text)

    def test_a_long_result_is_shortened_to_fit_the_summary_limit(self):
        limits = limits_mod.Limits(max_job_summary_chars=4000)
        self.assertTrue(self.run_report(self.final_file(), limits=limits))
        text = self.summary.read_text("utf-8")
        self.assertLessEqual(len(text), 4001)
        self.assertIn("## AI Cross Review", text)

    def test_a_result_that_cannot_fit_at_all_is_reported_as_a_failure_not_a_crash(self):
        limits = limits_mod.Limits(max_job_summary_chars=800)
        self.assertFalse(self.run_report(self.final_file(), limits=limits))
        text = self.summary.read_text("utf-8")
        self.assertIn(report.FAILURE_HEADING, text)
        self.assertLessEqual(len(text), 801)

    def test_untrusted_text_in_the_failure_reason_is_neutralized(self):
        text = report.failure_summary("<img src=x> @octocat [x](https://evil.example)")
        self.assertNotIn("<img", text.replace("\\<img", ""))
        self.assertNotIn("@octocat", text)
        # Every bracket is backslash-escaped, so no live link can form.
        self.assertNotRegex(text, r"(?<!\\)\]\(")
        self.assertNotIn("https://", text)
        self.assertIn("\\<img", text)

    def test_stdout_is_used_when_no_summary_file_is_configured(self):
        import contextlib
        import io

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            report.report(result_file=self.final_file(), summary_path=None)
        self.assertIn("## AI Cross Review", buffer.getvalue())

    def test_main_returns_stop_when_the_result_is_unusable(self):
        code = report.main(["--result-file", str(self.tmp / "nope.json"), "--summary-file", str(self.summary)])
        self.assertEqual(code, report.EXIT_STOP)
        ok = report.main(["--result-file", str(self.final_file()), "--summary-file", str(self.summary)])
        self.assertEqual(ok, report.EXIT_OK)

    def test_report_holds_no_credentials_and_no_network(self):
        source = (support.SCRIPTS / "report-summary.py").read_text("utf-8")
        for needle in ("GITHUB_TOKEN", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "urlopen", "subprocess"):
            self.assertNotIn(needle, source)


if __name__ == "__main__":
    unittest.main()
