"""Tests for the finalize step: merging, fingerprint checks, fail-closed states.

The finalizer combines the primary (Claude) review and the verification (Codex)
result. Its job is to make failure visible and never let it read as success.

Standard library only. Nothing here touches the network or a real credential.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402
from support import (  # noqa: E402
    SNAPSHOT_ID,
    claim_review,
    claude_document,
    claude_finding,
    codex_document,
    codex_finding,
    codex_payload,
    run_finalize,
    snapshot_block,
)

from lib import limits as limits_mod  # noqa: E402
from lib import result as result_mod  # noqa: E402

finalize_review = support.load_script("finalize_review", "finalize-review.py")
finalize_review.log = lambda message: None

OTHER_SNAPSHOT_ID = "b" * 64


def claude_with(findings: list[dict], **kwargs) -> dict:
    document = claude_document(**kwargs)
    document["review"] = {**document["review"], "findings": findings}
    return document


def codex_with(payload: dict) -> dict:
    return codex_document(verification=payload)


class FinalizeCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ai-review-finalize-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def finalize(self, **kwargs):
        return run_finalize(self.tmp, **kwargs)


class BucketingTests(FinalizeCase):
    def test_adopted_claim_is_reported_as_adopted_with_codex_judgement(self):
        document, _ = self.finalize()
        adopted = document["review"]["adopted"]
        self.assertEqual(len(adopted), 1)
        self.assertEqual(adopted[0]["origin"], "claude")
        self.assertEqual(adopted[0]["claude_index"], 0)
        # Codex's severity/confidence/rationale replace the primary reviewer's.
        self.assertEqual(adopted[0]["confidence"], "high")
        self.assertIn("diff", adopted[0]["rationale"])
        self.assertTrue(adopted[0]["suggested_fix"])

    def test_codex_findings_are_kept_apart_from_adopted_claude_findings(self):
        document, _ = self.finalize()
        added = document["review"]["added"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["origin"], "codex")
        self.assertIsNone(added[0]["claude_index"])
        self.assertEqual(document["review"]["adopted"][0]["origin"], "claude")

    def test_every_status_lands_in_its_own_bucket(self):
        findings = [claude_finding(title=f"f{i}") for i in range(4)]
        payload = codex_payload(
            claim_reviews=[
                claim_review(claude_index=0, status="adopted"),
                claim_review(claude_index=1, status="rejected", rationale="呼び出し側は対応済みだった。"),
                claim_review(claude_index=2, status="deferred", rationale="呼び出し元がbundleに無い。"),
                claim_review(claude_index=3, status="duplicate", duplicate_of=0, rationale="index 0と同一原因。"),
            ],
            additional_findings=[],
        )
        document, _ = self.finalize(claude=claude_with(findings), codex=codex_with(payload))
        review = document["review"]
        self.assertEqual([e["claude_index"] for e in review["adopted"]], [0])
        self.assertEqual([e["claude_index"] for e in review["rejected"]], [1])
        self.assertEqual([e["claude_index"] for e in review["deferred"]], [2])
        self.assertEqual([e["claude_index"] for e in review["duplicates"]], [3])
        self.assertEqual(review["duplicates"][0]["duplicate_of"], 0)
        self.assertEqual(review["rejected"][0]["rationale"], "呼び出し側は対応済みだった。")
        self.assertTrue(document["publishable"])

    def test_rejected_claim_keeps_its_reason(self):
        payload = codex_payload(
            claim_reviews=[claim_review(status="rejected", rationale="変更前から同じ挙動で、diffの範囲外だった。")],
            additional_findings=[],
        )
        document, _ = self.finalize(codex=codex_with(payload))
        self.assertEqual(document["review"]["rejected"][0]["rationale"], "変更前から同じ挙動で、diffの範囲外だった。")
        self.assertEqual(document["review"]["adopted"], [])

    def test_a_finding_without_a_verdict_is_deferred_not_dropped(self):
        findings = [claude_finding(title="a"), claude_finding(title="b")]
        payload = codex_payload(claim_reviews=[claim_review(claude_index=0)], additional_findings=[])
        document, _ = self.finalize(claude=claude_with(findings), codex=codex_with(payload))
        deferred = document["review"]["deferred"]
        self.assertEqual([e["claude_index"] for e in deferred], [1])
        self.assertIn("未検証", deferred[0]["rationale"])
        self.assertGreaterEqual(document["review"]["dropped"], 1)
        self.assertFalse(document["publishable"])

    def test_claude_dropped_findings_are_carried_into_the_final_count(self):
        claude = claude_document()
        claude["normalization"]["dropped_findings"] = [{"index": 0, "reason": "outside snapshot"}]
        document, _ = self.finalize(claude=claude)
        self.assertEqual(document["review"]["dropped"], 1)

    def test_combined_limitations_are_deduplicated_and_bounded(self):
        claude = claude_document()
        claude["review"]["limitations"] = [f"shared-{i}" for i in range(6)]
        codex = codex_document(verification=codex_payload())
        codex["verification"]["limitations"] = ["shared-0"] + [f"codex-{i}" for i in range(6)]
        document, _ = self.finalize(claude=claude, codex=codex)
        self.assertEqual(len(document["review"]["limitations"]), limits_mod.DEFAULT_LIMITS.max_limitations)
        self.assertEqual(document["review"]["limitations"][:6], [f"shared-{i}" for i in range(6)])
        self.assertEqual(document["review"]["limitations"][6:], [f"codex-{i}" for i in range(4)])

    def test_insufficient_context_and_limitations_reach_the_result(self):
        document, _ = self.finalize()
        self.assertEqual(document["review"]["insufficient_context"], ["呼び出し元のコードがbundleに含まれていない。"])
        self.assertTrue(document["review"]["limitations"])

    def test_models_requested_and_reported_are_recorded_for_both_stages(self):
        document, _ = self.finalize()
        self.assertEqual(document["stages"]["claude"]["model_requested"], "claude-opus-5")
        self.assertEqual(document["stages"]["claude"]["model_reported"], "claude-opus-5")
        self.assertEqual(document["stages"]["codex"]["model_requested"], "gpt-5.6-sol")
        self.assertEqual(document["stages"]["codex"]["model_reported"], "gpt-5.6-sol-2026-04-24")

    def test_clean_run_reports_every_verification_check(self):
        document, _ = self.finalize()
        self.assertTrue(document["publishable"])
        self.assertTrue(all(document["verification"].values()))
        self.assertEqual(document["stages"]["claude"]["status"], "success")
        self.assertEqual(document["stages"]["codex"]["status"], "success")

    def test_claude_finding_without_line_is_publishable_with_null_line(self):
        finding = claude_finding()
        del finding["line"]
        document, _ = self.finalize(claude=claude_with([finding]))
        self.assertTrue(document["publishable"])
        self.assertEqual(document["stages"]["claude"]["status"], "success")
        self.assertIsNone(document["review"]["adopted"][0]["line"])


class FailureIsNeverSuccessTests(FinalizeCase):
    def assert_not_publishable(self, document: dict):
        self.assertFalse(document["publishable"])
        self.assertIn("問題がないことを意味しない", document["review"]["summary"])

    def test_codex_job_failure_is_not_a_clean_review(self):
        document, _ = self.finalize(codex=None, codex_job="failure")
        self.assertEqual(document["stages"]["codex"]["status"], "failed")
        self.assertEqual(document["stages"]["claude"]["status"], "success")
        self.assert_not_publishable(document)
        # The primary findings survive, but only as unverified (deferred).
        self.assertEqual(document["review"]["adopted"], [])
        self.assertEqual(len(document["review"]["deferred"]), 1)
        self.assertIn("検証stageが失敗", document["review"]["deferred"][0]["rationale"])

    def test_claude_job_failure_skips_verification_and_is_not_publishable(self):
        document, _ = self.finalize(claude=None, codex=None, claude_job="failure", codex_job="skipped")
        self.assertEqual(document["stages"]["claude"]["status"], "failed")
        self.assertEqual(document["stages"]["codex"]["status"], "skipped")
        self.assert_not_publishable(document)
        self.assertEqual(document["review"]["adopted"], [])

    def test_cancelled_job_is_a_failure(self):
        document, _ = self.finalize(codex=None, codex_job="cancelled")
        self.assertEqual(document["stages"]["codex"]["status"], "failed")
        self.assertFalse(document["publishable"])

    def test_unknown_job_result_is_treated_as_failure(self):
        document, _ = self.finalize(codex=None, codex_job="neutral")
        self.assertEqual(document["stages"]["codex"]["status"], "failed")
        self.assertFalse(document["publishable"])

    def test_successful_job_without_an_artifact_is_missing_not_clean(self):
        document, _ = self.finalize(codex=None, codex_job="success")
        self.assertEqual(document["stages"]["codex"]["status"], "missing")
        self.assert_not_publishable(document)

    def test_corrupt_artifact_is_invalid(self):
        document, _ = self.finalize(codex=b"{not json")
        self.assertEqual(document["stages"]["codex"]["status"], "invalid")
        self.assert_not_publishable(document)

    def test_artifact_with_missing_fields_is_invalid(self):
        broken = codex_document()
        del broken["verification"]
        document, _ = self.finalize(codex=broken)
        self.assertEqual(document["stages"]["codex"]["status"], "invalid")
        self.assertIn("verification", document["stages"]["codex"]["detail"])
        self.assertFalse(document["publishable"])

    def test_malformed_claude_nested_review_is_invalid(self):
        broken = claude_document(review={})
        document, _ = self.finalize(claude=broken)
        self.assertEqual(document["stages"]["claude"]["status"], "invalid")
        self.assertFalse(document["verification"]["schema_valid"])
        self.assertFalse(document["publishable"])

    def test_malformed_codex_nested_verification_is_invalid(self):
        broken = codex_document(verification={})
        document, _ = self.finalize(codex=broken)
        self.assertEqual(document["stages"]["codex"]["status"], "invalid")
        self.assertFalse(document["verification"]["schema_valid"])
        self.assertFalse(document["publishable"])

    def test_codex_self_duplicate_claim_is_invalid(self):
        payload = codex_payload(claim_reviews=[claim_review(status="duplicate", duplicate_of=0)])
        document, _ = self.finalize(codex=codex_with(payload))
        self.assertEqual(document["stages"]["codex"]["status"], "invalid")
        self.assertFalse(document["publishable"])

    def test_framework_version_mismatch_is_invalid(self):
        document, _ = self.finalize(codex=codex_document(framework_version="0.0.1"))
        self.assertEqual(document["stages"]["codex"]["status"], "invalid")
        self.assertFalse(document["publishable"])

    def test_both_stages_failing_still_produces_a_visible_failure(self):
        document, out = self.finalize(claude=None, codex=None, claude_job="failure", codex_job="failure")
        self.assertFalse(document["publishable"])
        self.assertTrue((out / "final-review.json").is_file())
        self.assertTrue((out / "review-summary.md").is_file())
        text = (out / "review-summary.md").read_text("utf-8")
        self.assertIn("完了しなかった", text)
        self.assertIn("claude=failed", text)

    def test_empty_findings_with_a_failed_codex_stage_never_reads_as_clean(self):
        document, _ = self.finalize(claude=claude_with([]), codex=None, codex_job="failure")
        self.assertFalse(document["publishable"])
        self.assertEqual(document["review"]["adopted"] + document["review"]["deferred"], [])
        self.assertIn("問題がないことを意味しない", document["review"]["summary"])


class SnapshotFingerprintTests(FinalizeCase):
    def test_claude_result_for_another_snapshot_is_rejected(self):
        stale = claude_document(snapshot=snapshot_block(snapshot_id=OTHER_SNAPSHOT_ID))
        document, _ = self.finalize(claude=stale)
        self.assertEqual(document["stages"]["claude"]["status"], "invalid")
        self.assertFalse(document["verification"]["claude_fingerprint_match"])
        self.assertFalse(document["verification"]["snapshot_match"])
        self.assertFalse(document["publishable"])
        # None of the mismatched result's content is merged.
        self.assertEqual(document["review"]["adopted"], [])
        self.assertEqual(document["review"]["deferred"], [])

    def test_codex_result_for_another_snapshot_is_rejected(self):
        stale = codex_document(snapshot=snapshot_block(snapshot_id=OTHER_SNAPSHOT_ID))
        document, _ = self.finalize(codex=stale)
        self.assertEqual(document["stages"]["codex"]["status"], "invalid")
        self.assertFalse(document["verification"]["codex_fingerprint_match"])
        self.assertFalse(document["publishable"])
        self.assertEqual(document["review"]["added"], [])

    def test_results_that_agree_with_each_other_but_not_with_prepare_are_rejected(self):
        # Both artifacts were swapped for another snapshot's pair; only the
        # job output from prepare (outside the artifacts) can tell.
        other = snapshot_block(snapshot_id=OTHER_SNAPSHOT_ID)
        document, _ = self.finalize(
            claude=claude_document(snapshot=other),
            codex=codex_document(snapshot=other),
        )
        self.assertFalse(document["publishable"])
        self.assertFalse(document["verification"]["snapshot_match"])

    def test_prepare_values_are_mandatory_and_validated(self):
        bad_values = (
            {"snapshot_id": ""},
            {"snapshot_id": "not-a-digest"},
            {"head_sha": "HEAD"},
            {"base_sha": "1" * 39},
            {"merge_base_sha": None},
            {"diff_sha256": "x"},
            {"policy_source": "pr_head"},
            {"policy_present": "true"},
            {"is_fork": 1},
        )
        for override in bad_values:
            with self.subTest(override=override), self.assertRaises(finalize_review.FinalizeError):
                self.finalize(prepare_overrides=override)
        with self.assertRaises(finalize_review.FinalizeError):
            finalize_review._validate_prepare_snapshot({"snapshot_id": SNAPSHOT_ID})

    def test_an_artifact_that_borrows_the_snapshot_id_but_not_the_head_is_refused(self):
        # Same snapshot_id (which is public in job outputs), different head SHA.
        forged = claude_document(snapshot=snapshot_block(head_sha="9" * 40))
        document, _ = self.finalize(claude=forged)
        self.assertFalse(document["publishable"])
        self.assertEqual(document["stages"]["claude"]["status"], "invalid")
        self.assertFalse(document["verification"]["claude_fingerprint_match"])
        for key, value in (("base_sha", "9" * 40), ("merge_base_sha", "9" * 40), ("diff_sha256", "9" * 64),
                           ("policy_source", "central_default"), ("policy_present", False), ("is_fork", True)):
            with self.subTest(field=key):
                forged = codex_document(snapshot=snapshot_block(**{key: value}))
                document, _ = self.finalize(codex=forged)
                self.assertEqual(document["stages"]["codex"]["status"], "invalid")
                self.assertFalse(document["publishable"])

    def test_snapshot_is_never_taken_from_a_mismatched_stage(self):
        stale = claude_document(snapshot=snapshot_block(snapshot_id=OTHER_SNAPSHOT_ID, head_sha="9" * 40))
        document, _ = self.finalize(claude=stale, codex=None, codex_job="failure")
        self.assertEqual(document["snapshot"]["snapshot_id"], SNAPSHOT_ID)
        self.assertNotEqual(document["snapshot"]["head_sha"], "9" * 40)

    def test_a_run_with_no_usable_stage_still_names_the_commit_it_was_about(self):
        document, out = self.finalize(claude=None, codex=None, claude_job="failure", codex_job="skipped")
        self.assertEqual(document["snapshot"]["head_sha"], support.HEAD_SHA)
        self.assertEqual(document["snapshot"]["base_sha"], support.BASE_SHA)
        self.assertEqual(document["snapshot"]["snapshot_id"], SNAPSHOT_ID)
        self.assertEqual(document["snapshot"]["reviewable_path_hashes"], [])
        self.assertNotIn("0" * 40, (out / "review-summary.md").read_text("utf-8"))


class RequestBindingTests(FinalizeCase):
    def test_models_outside_the_allowlist_stop_the_finalizer(self):
        from lib import models as models_mod

        with self.assertRaises(models_mod.ModelNotAllowed):
            self.finalize(claude_model="claude-opus-4-1")
        with self.assertRaises(models_mod.ModelNotAllowed):
            self.finalize(codex_model="gpt-4")

    def test_a_stage_that_disagrees_with_prepare_about_the_policy_or_fork_is_refused(self):
        snapshot = snapshot_block(policy_source="central_default", policy_present=False, is_fork=True)
        document, _ = self.finalize(claude=claude_document(snapshot=snapshot), codex=codex_document(snapshot=snapshot))
        self.assertFalse(document["publishable"])
        self.assertEqual(document["stages"]["claude"]["status"], "invalid")

    def test_output_mode_is_bound_from_the_request_not_from_the_models(self):
        document, _ = self.finalize(output_mode="summary_only")
        self.assertEqual(document["request"]["output_mode"], "summary_only")
        with self.assertRaises(Exception):
            self.finalize(output_mode="pr_comment; rm -rf")

    def test_policy_source_and_fork_flags_are_carried_into_the_result(self):
        snapshot = snapshot_block(policy_source="central_default", policy_present=False, is_fork=True)
        document, _ = self.finalize(
            claude=claude_document(snapshot=snapshot),
            codex=codex_document(snapshot=snapshot),
            prepare_overrides={"policy_source": "central_default", "policy_present": False, "is_fork": True},
        )
        self.assertTrue(document["publishable"])
        self.assertEqual(document["snapshot"]["policy_source"], "central_default")
        self.assertFalse(document["snapshot"]["policy_present"])
        self.assertTrue(document["snapshot"]["is_fork"])
        summary = (self.tmp / "final" / "review-summary.md").read_text("utf-8")
        self.assertIn("central_default", summary)


class HygieneTests(FinalizeCase):
    def test_credential_shaped_values_are_redacted_and_counted(self):
        canary = support.CANARY_GITHUB
        payload = codex_payload(
            claim_reviews=[claim_review(rationale=f"設定に{canary}が含まれている。")],
            additional_findings=[codex_finding(detail=f"{canary} をログへ出している。")],
        )
        document, out = self.finalize(codex=codex_with(payload))
        blob = (out / "final-review.json").read_text("utf-8") + (out / "review-summary.md").read_text("utf-8")
        self.assertNotIn(canary, blob)
        self.assertGreaterEqual(document["review"]["redactions"], 2)

    def test_output_validates_against_the_publishers_own_checks(self):
        document, out = self.finalize()
        written = json.loads((out / "final-review.json").read_text("utf-8"))
        self.assertEqual(result_mod.validate_final_document(written), document)

    def test_finding_outside_the_reviewed_files_cannot_be_written(self):
        payload = codex_payload(additional_findings=[codex_finding(path="other/file.py")])
        # The normalizer would have dropped it; a forged artifact must not slip
        # through the finalizer's own output validation either.
        document, _ = self.finalize(codex=codex_with(payload))
        self.assertEqual(document["stages"]["codex"]["status"], "invalid")
        self.assertFalse(document["publishable"])

    def test_github_output_lists_state_for_the_workflow(self):
        out = self.tmp / "gh-output.txt"
        finalize_review._write_github_output(str(out), {"publishable": "true", "snapshot_id": SNAPSHOT_ID})
        text = out.read_text()
        self.assertIn("publishable=true\n", text)
        with self.assertRaises(finalize_review.FinalizeError):
            finalize_review._write_github_output(str(out), {"bad\nkey": "x"})
        with self.assertRaises(finalize_review.FinalizeError):
            finalize_review._write_github_output(str(out), {"ok": "line1\nline2"})

    def test_result_size_limit_is_enforced(self):
        tiny = limits_mod.Limits(max_final_result_bytes=512)
        with self.assertRaises(Exception):
            finalize_review.finalize(
                repository=support.REPOSITORY,
                pr_number=support.PR_NUMBER,
                output_mode="pr_comment",
                claude_model="claude-opus-5",
                codex_model="gpt-5.6-sol",
                claude_effort="high",
                codex_effort="high",
                policy_path=".github/ai-review.md",
                prepare_snapshot=support.prepare_snapshot(),
                claude_result_file=None,
                codex_result_file=None,
                claude_job_result="failure",
                codex_job_result="failure",
                output_dir=self.tmp / "tiny",
                limits=tiny,
                run_env={},
            )


if __name__ == "__main__":
    unittest.main()
