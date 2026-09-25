"""End-to-end tests of the two-stage pipeline, with every external system mocked.

Each scenario runs the real step scripts in the workflow's order:

    prepare (bundle) -> claude review -> normalize -> codex verification
    -> normalize -> finalize -> report -> publish

GitHub is an in-memory transport, the Claude CLI is a fake executable, and the
Codex CLI is an injected ``run`` function. No network, no real credential or
sign-in, no real PR, and no reviewed code is ever executed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402
from support import (  # noqa: E402
    CANARY_GITHUB,
    CANARY_OPENAI,
    SNAPSHOT_ID,
    claim_review,
    codex_events,
    codex_finding,
    codex_payload,
    fake_codex_run,
    make_bundle,
)

import test_publish_review as publish_tests  # noqa: E402
import test_review_result as claude_tests  # noqa: E402

from lib import bundle as bundle_mod  # noqa: E402
from lib import limits as limits_mod  # noqa: E402

run_review = claude_tests.run_review
normalize_review = claude_tests.normalize_review
run_codex = support.load_script("run_codex_review", "run-codex-review.py")
normalize_codex = support.load_script("normalize_codex_review", "normalize-codex-review.py")
finalize = support.load_script("finalize_review", "finalize-review.py")
report = support.load_script("report_summary", "report-summary.py")
publish = support.load_script("publish_review", "publish-review.py")
for module in (run_codex, normalize_codex, finalize, report, publish):
    module.log = lambda message: None

ROOT = support.ROOT
COMMENT_TOKEN = "canary-comment-token-not-real-0002"
GITHUB_CANARY_ENV = {
    "GITHUB_TOKEN": CANARY_GITHUB,
    "AI_REVIEW_READ_TOKEN": "canary-read-token-not-real-0001",
    "AI_REVIEW_COMMENT_TOKEN": COMMENT_TOKEN,
    "GH_TOKEN": CANARY_GITHUB,
    # An API key in the environment must never reach the Codex CLI (ADR-0012).
    "OPENAI_API_KEY": CANARY_OPENAI,
}


class StageFailure(Exception):
    """A pipeline stage stopped, as the corresponding job would have failed."""


class Pipeline:
    """Runs the stages against a bundle and records what each one saw."""

    def __init__(self, tmp: Path, *, bundle_kwargs: dict | None = None):
        self.tmp = Path(tmp)
        self.bundle_dir = make_bundle(self.tmp / "bundle", **(bundle_kwargs or {}))
        self.claude_result: Path | None = None
        self.codex_result: Path | None = None
        self.claude_job = "skipped"
        self.codex_job = "skipped"
        self.codex_calls: list[dict] = []
        self.claude_record: dict | None = None
        self.final: dict | None = None
        self.final_dir = self.tmp / "final"

    # -- stage 1: primary review -------------------------------------------------

    def claude(self, *, payload=None, exit_code=0, envelope_obj=None, model="claude-opus-5"):
        cli = claude_tests.write_fake_cli(
            self.tmp / "fake-claude",
            envelope_obj=envelope_obj if envelope_obj is not None else claude_tests.envelope(payload or claude_tests.model_payload()),
            exit_code=exit_code,
        )
        workdir = self.tmp / "claude-work"
        try:
            with mock.patch.dict(os.environ, GITHUB_CANARY_ENV):
                run_review.run_review(
                    bundle_dir=self.bundle_dir,
                    workdir=workdir,
                    prompt_file=ROOT / "prompts" / "review.md",
                    schema_file=ROOT / "schemas" / "review-result.schema.json",
                    claude_bin=str(cli),
                    model=model,
                    token=claude_tests.CANARY_OAUTH,
                    expected_sha256=claude_tests.sha256_file(cli),
                    run_env={"GITHUB_RUN_ID": "42"},
                )
            self.claude_record = json.loads((workdir / "cwd" / "record.json").read_text("utf-8"))
            normalize_review.normalize(
                bundle_dir=self.bundle_dir,
                raw_file=workdir / run_review.RAW_RESULT_NAME,
                invocation_file=workdir / run_review.INVOCATION_NAME,
                output_dir=self.tmp / "claude-out",
                token=claude_tests.CANARY_OAUTH,
                run_env={},
            )
        except Exception as err:
            self.claude_job = "failure"
            raise StageFailure(str(err)) from err
        self.claude_job = "success"
        self.claude_result = self.tmp / "claude-out" / "review-result.json"
        return self.claude_result

    # -- stage 2: verification ---------------------------------------------------

    @property
    def codex_requests(self) -> list[dict]:
        """The ``codex exec`` calls, i.e. the times the model was called."""
        return [call for call in self.codex_calls if call["argv"][1:2] == ["exec"]]

    def codex(self, *, payload=None, stream=None, returncode=0, model="gpt-5.6-sol", claude_result: Path | None = None):
        stream = stream if stream is not None else codex_events(payload if payload is not None else codex_payload())
        workdir = self.tmp / "codex-work"
        codex_home = self.tmp / "codex-home"
        codex_home.mkdir(exist_ok=True)
        source = claude_result or self.claude_result
        try:
            with mock.patch.dict(os.environ, GITHUB_CANARY_ENV):
                run_codex.run_codex_review(
                    bundle_dir=self.bundle_dir,
                    claude_result_file=source,
                    workdir=workdir,
                    prompt_file=ROOT / "prompts" / "codex-verify.md",
                    schema_file=ROOT / "schemas" / "codex-review.schema.json",
                    model=model,
                    codex_home=codex_home,
                    run=fake_codex_run(stream, returncode=returncode, record=self.codex_calls),
                    run_env={"GITHUB_RUN_ID": "42"},
                )
            normalize_codex.normalize(
                bundle_dir=self.bundle_dir,
                claude_result_file=source,
                raw_file=workdir / run_codex.RAW_RESULT_NAME,
                invocation_file=workdir / run_codex.INVOCATION_NAME,
                output_dir=self.tmp / "codex-out",
            )
        except Exception as err:
            self.codex_job = "failure"
            raise StageFailure(str(err)) from err
        self.codex_job = "success"
        self.codex_result = self.tmp / "codex-out" / "codex-result.json"
        return self.codex_result

    # -- stage 3: merge, report, publish ----------------------------------------------

    def finalize(self, *, output_mode="pr_comment", expected_snapshot_id=SNAPSHOT_ID, claude_model="claude-opus-5", codex_model="gpt-5.6-sol"):
        self.final = finalize.finalize(
            repository=support.REPOSITORY,
            pr_number=support.PR_NUMBER,
            output_mode=output_mode,
            claude_model=claude_model,
            codex_model=codex_model,
            claude_effort="high",
            codex_effort="high",
            policy_path=".github/ai-review.md",
            prepare_snapshot=self.prepare_snapshot(expected_snapshot_id),
            claude_result_file=self.claude_result,
            codex_result_file=self.codex_result,
            claude_job_result=self.claude_job,
            codex_job_result=self.codex_job,
            output_dir=self.final_dir,
            run_env={},
        )
        return self.final

    def prepare_snapshot(self, snapshot_id: str = SNAPSHOT_ID) -> dict:
        """What the workflow copies from prepare's outputs, read from the real bundle."""
        bundle = bundle_mod.load_bundle(self.bundle_dir)
        manifest = bundle.manifest
        return {
            "snapshot_id": snapshot_id,
            "head_sha": manifest["head_sha"],
            "base_sha": manifest["base_sha"],
            "merge_base_sha": manifest["merge_base_sha"],
            "diff_sha256": manifest["diff"]["sha256"],
            "policy_source": bundle.policy_source,
            "policy_present": bundle.policy_present,
            "is_fork": bool(manifest.get("is_fork")),
        }

    def report(self) -> str:
        summary = self.tmp / "step-summary.md"
        report.report(result_file=self.final_dir / "final-review.json", summary_path=str(summary))
        return summary.read_text("utf-8")

    def publish(self, fake, *, output_mode="pr_comment", expected=SNAPSHOT_ID, token=COMMENT_TOKEN):
        return publish.publish(
            repository=support.REPOSITORY,
            pr_number=support.PR_NUMBER,
            result_file=self.final_dir / "final-review.json",
            expected_snapshot_id=expected,
            output_mode=output_mode,
            token=token,
            transport=fake,
            sleep=lambda seconds: None,
            run_env={},
        )

    def run_all(self, *, output_mode="pr_comment", **kwargs):
        self.claude(payload=kwargs.get("claude_payload"))
        self.codex(payload=kwargs.get("codex_payload"))
        return self.finalize(output_mode=output_mode)


class PipelineCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ai-review-pipeline-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def pipeline(self, **kwargs) -> Pipeline:
        return Pipeline(self.tmp, **kwargs)


# -- output modes ------------------------------------------------------------------


class SummaryOnlyTests(PipelineCase):
    def test_summary_only_shows_the_result_and_never_touches_the_pull_request(self):
        pipe = self.pipeline()
        final = pipe.run_all(output_mode="summary_only")
        self.assertTrue(final["publishable"])
        summary = pipe.report()
        self.assertIn("Codexが採用したClaudeの指摘 (1)", summary)
        self.assertIn("Codexが追加した指摘 (1)", summary)

        # The publisher refuses to run, with or without a token, and reads nothing.
        fake = publish_tests.FakeGitHub()
        for token in (COMMENT_TOKEN, None):
            with self.subTest(token=bool(token)), self.assertRaises(publish.PublishError):
                pipe.publish(fake, output_mode="summary_only", token=token)
        self.assertEqual(fake.requests, [])

    def test_summary_only_needs_no_comment_credential_anywhere_in_the_pipeline(self):
        pipe = self.pipeline()
        with mock.patch.dict(os.environ, {}, clear=False):
            for name in ("AI_REVIEW_COMMENT_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
                os.environ.pop(name, None)
            final = pipe.run_all(output_mode="summary_only")
            pipe.report()
        self.assertEqual(final["request"]["output_mode"], "summary_only")
        self.assertTrue(final["publishable"])

    def test_artifacts_are_written_for_summary_only(self):
        pipe = self.pipeline()
        pipe.run_all(output_mode="summary_only")
        self.assertEqual(sorted(p.name for p in pipe.final_dir.iterdir()), ["final-review.json", "review-summary.md"])
        markdown = (pipe.final_dir / "review-summary.md").read_text("utf-8")
        self.assertEqual(markdown, pipe.report())
        json.loads((pipe.final_dir / "final-review.json").read_text("utf-8"))


class PrCommentTests(PipelineCase):
    def test_pr_comment_posts_once_through_the_publisher(self):
        pipe = self.pipeline()
        pipe.run_all()
        fake = publish_tests.FakeGitHub()
        outcome = pipe.publish(fake)
        self.assertEqual(outcome["action"], "created")
        self.assertEqual(len(fake.writes), 1)
        body = fake.writes[0][2]["body"]
        self.assertIn("Codexが採用したClaudeの指摘 (1)", body)
        self.assertIn("実使用model", body)

    def test_claude_finding_without_line_reaches_the_published_comment(self):
        pipe = self.pipeline()
        payload = claude_tests.model_payload()
        del payload["findings"][0]["line"]
        pipe.claude(payload=payload)
        normalized = json.loads(pipe.claude_result.read_text("utf-8"))
        self.assertNotIn("line", normalized["review"]["findings"][0])
        pipe.codex()
        final = pipe.finalize()
        self.assertTrue(final["publishable"])
        self.assertIsNone(final["review"]["adopted"][0]["line"])

        fake = publish_tests.FakeGitHub()
        self.assertEqual(pipe.publish(fake)["action"], "created")
        body = fake.writes[0][2]["body"]
        self.assertIn("- location: `src/app.py`", body)
        self.assertNotIn("src/app.py:None", body)

    def test_ai_processing_never_receives_a_github_credential_even_in_pr_comment_mode(self):
        pipe = self.pipeline()
        pipe.run_all(output_mode="pr_comment")
        # The environment was poisoned with GitHub canaries during both AI stages.
        claude_env = pipe.claude_record["env"]
        for name in GITHUB_CANARY_ENV:
            self.assertNotIn(name, claude_env)
        self.assertNotIn(CANARY_GITHUB, json.dumps(pipe.claude_record))
        self.assertNotIn(COMMENT_TOKEN, json.dumps(pipe.claude_record))
        self.assertNotIn(claude_tests.CANARY_OAUTH, json.dumps(pipe.claude_record["argv"]))

        self.assertEqual(len(pipe.codex_requests), 1)
        for call in pipe.codex_calls:
            for name in GITHUB_CANARY_ENV:
                self.assertNotIn(name, call["env"])
            blob = json.dumps({**call, "input": call["input"].decode("utf-8") if call["input"] else None})
            for canary in (*GITHUB_CANARY_ENV.values(), COMMENT_TOKEN):
                self.assertNotIn(canary, blob)

    def test_the_publisher_holds_no_ai_credential(self):
        pipe = self.pipeline()
        pipe.run_all()
        seen: list[dict] = []
        fake = publish_tests.FakeGitHub()

        def transport(method, url, headers, body=None):
            seen.append(headers)
            return fake(method, url, headers, body)

        pipe.publish(transport)
        for headers in seen:
            self.assertEqual(headers["Authorization"], f"Bearer {COMMENT_TOKEN}")
            self.assertNotIn(CANARY_OPENAI, json.dumps(headers))
            self.assertNotIn(claude_tests.CANARY_OAUTH, json.dumps(headers))

    def test_a_second_run_of_the_same_snapshot_updates_the_comment(self):
        pipe = self.pipeline()
        pipe.run_all()
        fake = publish_tests.FakeGitHub()
        pipe.publish(fake)
        self.assertEqual(pipe.publish(fake)["action"], "updated")
        self.assertEqual(len(fake.comments), 1)


# -- failure never reads as success ------------------------------------------------------


class FailureTests(PipelineCase):
    def assert_nothing_is_posted(self, pipe: Pipeline):
        self.assertFalse(pipe.final["publishable"])
        fake = publish_tests.FakeGitHub()
        with self.assertRaisesRegex(publish.PublishError, "not publishable"):
            pipe.publish(fake)
        self.assertEqual(fake.requests, [])
        summary = pipe.report()
        self.assertIn("この実行は完了しなかった", summary)
        self.assertIn("not-publishable", summary)

    def test_codex_cli_failure(self):
        pipe = self.pipeline()
        pipe.claude()
        with self.assertRaises(StageFailure):
            pipe.codex(returncode=1)
        pipe.finalize()
        self.assertEqual(pipe.final["stages"]["codex"]["status"], "failed")
        self.assertEqual(pipe.final["stages"]["claude"]["status"], "success")
        self.assert_nothing_is_posted(pipe)
        # The unverified primary finding is shown as unverified, never as adopted.
        self.assertEqual(pipe.final["review"]["adopted"], [])
        self.assertEqual(len(pipe.final["review"]["deferred"]), 1)

    def test_codex_failed_turn(self):
        pipe = self.pipeline()
        pipe.claude()
        failed = codex_events(codex_payload(), completed=False) + b'{"type": "turn.failed", "error": {"message": "usage limit"}}\n'
        with self.assertRaises(StageFailure):
            pipe.codex(stream=failed)
        pipe.finalize()
        self.assert_nothing_is_posted(pipe)

    def test_codex_tool_attempt_is_a_failure(self):
        pipe = self.pipeline()
        pipe.claude()
        acted = codex_events(codex_payload(), items=[{"type": "command_execution", "command": "cat ~/.codex/auth.json"}])
        with self.assertRaises(StageFailure):
            pipe.codex(stream=acted)
        pipe.finalize()
        self.assert_nothing_is_posted(pipe)

    def test_codex_schema_violation(self):
        pipe = self.pipeline()
        pipe.claude()
        with self.assertRaises(StageFailure):
            pipe.codex(payload={**codex_payload(), "unexpected": True})
        pipe.finalize()
        self.assert_nothing_is_posted(pipe)

    def test_codex_returning_no_verdicts_is_deferred_never_clean(self):
        pipe = self.pipeline()
        pipe.claude()
        pipe.codex(payload=codex_payload(claim_reviews=[], additional_findings=[]))
        final = pipe.finalize()
        self.assertEqual(final["review"]["adopted"], [])
        self.assertEqual(len(final["review"]["deferred"]), 1)
        self.assertIn("未検証", final["review"]["deferred"][0]["rationale"])

    def test_claude_cli_failure_skips_verification(self):
        pipe = self.pipeline()
        with self.assertRaises(StageFailure):
            pipe.claude(exit_code=3)
        pipe.finalize()
        self.assertEqual(pipe.final["stages"]["claude"]["status"], "failed")
        self.assertEqual(pipe.final["stages"]["codex"]["status"], "skipped")
        self.assert_nothing_is_posted(pipe)
        self.assertEqual(pipe.codex_requests, [], "Codex must not run without a primary review")

    def test_claude_schema_violation(self):
        pipe = self.pipeline()
        bad = claude_tests.envelope({"schema_version": "1", "summary": "x"})
        with self.assertRaises(StageFailure):
            pipe.claude(envelope_obj=bad)
        pipe.finalize()
        self.assert_nothing_is_posted(pipe)

    def test_claude_tool_attempt_is_a_failure(self):
        pipe = self.pipeline()
        with self.assertRaises(StageFailure):
            pipe.claude(envelope_obj=claude_tests.envelope(claude_tests.model_payload(), permission_denials=[{"tool_name": "Bash"}]))
        pipe.finalize()
        self.assert_nothing_is_posted(pipe)

    def test_both_stages_failing_is_still_reported(self):
        pipe = self.pipeline()
        with self.assertRaises(StageFailure):
            pipe.claude(exit_code=1)
        pipe.finalize()
        summary = pipe.report()
        self.assertIn("claude=failed", summary)
        self.assertIn("codex=skipped", summary)


# -- snapshot binding ----------------------------------------------------------------


class SnapshotBindingTests(PipelineCase):
    def other_snapshot_claude_result(self, pipe: Pipeline) -> Path:
        document = json.loads(pipe.claude_result.read_text("utf-8"))
        document["snapshot"]["snapshot_id"] = "b" * 64
        path = self.tmp / "claude-other-snapshot.json"
        path.write_bytes(support.json_bytes(document))
        return path

    def test_codex_refuses_a_primary_review_from_another_snapshot(self):
        pipe = self.pipeline()
        pipe.claude()
        stale = self.other_snapshot_claude_result(pipe)
        with self.assertRaises(StageFailure) as ctx:
            pipe.codex(claude_result=stale)
        self.assertIn("different snapshot", str(ctx.exception))
        self.assertEqual(pipe.codex_requests, [], "the model must never see a mismatched pair")

    def test_finalize_refuses_a_primary_review_from_another_snapshot(self):
        pipe = self.pipeline()
        pipe.run_all()
        pipe.claude_result = self.other_snapshot_claude_result(pipe)
        final = pipe.finalize()
        self.assertFalse(final["publishable"])
        self.assertEqual(final["stages"]["claude"]["status"], "invalid")
        self.assertFalse(final["verification"]["snapshot_match"])

    def test_publisher_refuses_when_prepare_published_a_different_snapshot(self):
        pipe = self.pipeline()
        pipe.run_all()
        fake = publish_tests.FakeGitHub()
        with self.assertRaisesRegex(publish.PublishError, "does not belong"):
            pipe.publish(fake, expected="c" * 64)
        self.assertEqual(fake.writes, [])

    def test_finalize_refuses_when_both_artifacts_were_swapped_together(self):
        pipe = self.pipeline()
        pipe.run_all()
        final = pipe.finalize(expected_snapshot_id="c" * 64)
        self.assertFalse(final["publishable"])
        self.assertFalse(final["verification"]["claude_fingerprint_match"])
        self.assertFalse(final["verification"]["codex_fingerprint_match"])


# -- stale PR ---------------------------------------------------------------------------


class StalePullRequestTests(PipelineCase):
    def test_pr_that_moved_after_review_is_not_posted(self):
        pipe = self.pipeline()
        pipe.run_all()
        for label, pull in (
            ("head moved", publish_tests.pull_payload(head="9" * 40)),
            ("base moved", publish_tests.pull_payload(base="9" * 40)),
            ("closed", publish_tests.pull_payload(state="closed")),
            ("merged", publish_tests.pull_payload(merged=True)),
            ("gone", None),
        ):
            with self.subTest(case=label):
                fake = publish_tests.FakeGitHub(pull=pull)
                with self.assertRaises(publish.PublishError):
                    pipe.publish(fake)
                self.assertEqual(fake.writes, [])


# -- content handling -------------------------------------------------------------------


class ContextAndContentTests(PipelineCase):
    def test_insufficient_context_survives_to_the_summary_and_the_comment(self):
        pipe = self.pipeline()
        pipe.run_all(codex_payload=codex_payload(insufficient_context=["呼び出し元の実装がbundleに無い。"]))
        self.assertIn("呼び出し元の実装がbundleに無い。", pipe.report())
        fake = publish_tests.FakeGitHub()
        pipe.publish(fake)
        self.assertIn("呼び出し元の実装がbundleに無い。", fake.writes[0][2]["body"])

    def test_deferred_claim_is_reported_as_deferred_with_its_reason(self):
        pipe = self.pipeline()
        pipe.run_all(codex_payload=codex_payload(
            claim_reviews=[claim_review(status="deferred", rationale="呼び出し元がbundleに無く判断できない。")],
            additional_findings=[],
        ))
        self.assertEqual(len(pipe.final["review"]["deferred"]), 1)
        self.assertIn("判断保留 (1)", pipe.report())
        self.assertIn("呼び出し元がbundleに無く判断できない。", pipe.report())

    def test_rejected_claim_keeps_its_reason_in_the_report(self):
        pipe = self.pipeline()
        pipe.run_all(codex_payload=codex_payload(
            claim_reviews=[claim_review(status="rejected", rationale="変更前から同じ挙動でdiffの範囲外だった。")],
            additional_findings=[],
        ))
        summary = pipe.report()
        self.assertIn("不採用となったClaudeの指摘 (1)", summary)
        self.assertIn("変更前から同じ挙動でdiffの範囲外だった。", summary)

    def test_sensitive_and_binary_files_never_reach_either_model_or_the_result(self):
        pipe = self.pipeline()
        pipe.run_all(codex_payload=codex_payload(additional_findings=[
            codex_finding(path=".env"),
            codex_finding(path="assets/logo.png"),
            codex_finding(path="src/app.py"),
        ]))
        sent = pipe.codex_requests[0]["input"].decode("utf-8")
        # The excluded file is listed as excluded (so it cannot be cited) but has no patch.
        self.assertIn('"excluded": "forbidden_filename"', sent)
        self.assertNotIn("diff --git a/.env", sent)
        self.assertNotIn("Binary files", sent)
        self.assertNotIn("SECRET_LOOKING", sent)
        self.assertEqual([e["path"] for e in pipe.final["review"]["added"]], ["src/app.py"])
        self.assertEqual(pipe.final["review"]["dropped"], 2)
        claude_input = pipe.claude_record["stdin"]
        self.assertNotIn("SECRET_LOOKING", claude_input)

    def test_pr_text_is_data_for_both_models_and_never_an_instruction(self):
        pipe = self.pipeline()
        pipe.run_all()
        claude_argv = json.dumps(pipe.claude_record["argv"])
        codex_call = pipe.codex_requests[0]
        self.assertNotIn("Ignore previous instructions", claude_argv)
        self.assertNotIn("Ignore previous instructions", json.dumps(codex_call["argv"]))
        self.assertNotIn("Ignore previous instructions", (ROOT / "prompts" / "codex-verify.md").read_text("utf-8"))
        self.assertIn("Ignore previous instructions", pipe.claude_record["stdin"])
        self.assertIn("Ignore previous instructions", codex_call["input"].decode("utf-8"))

    def test_both_models_receive_the_same_snapshot(self):
        pipe = self.pipeline()
        pipe.run_all()
        claude_input = pipe.claude_record["stdin"]
        codex_input = pipe.codex_requests[0]["input"].decode("utf-8")
        diff = bundle_mod.load_bundle(pipe.bundle_dir).diff.decode("utf-8")
        self.assertIn(diff, claude_input)
        self.assertIn(diff, codex_input)
        for section in ("## POLICY", "## PR_METADATA", "## FILES", "## DIFF"):
            self.assertIn(section, claude_input)
            self.assertIn(section, codex_input)
        # The bundle both saw is the one whose fingerprint gates the result.
        self.assertEqual(pipe.final["snapshot"]["snapshot_id"], bundle_mod.load_bundle(pipe.bundle_dir).snapshot_id)

    def test_requested_and_reported_models_are_both_recorded_end_to_end(self):
        pipe = self.pipeline()
        pipe.run_all()
        stages = pipe.final["stages"]
        self.assertEqual((stages["claude"]["model_requested"], stages["claude"]["model_reported"]), ("claude-opus-5", "claude-opus-5"))
        self.assertEqual((stages["codex"]["model_requested"], stages["codex"]["model_reported"]), ("gpt-5.6-sol", None))


class PolicyAndForkTests(PipelineCase):
    def test_central_default_policy_is_used_and_recorded_when_the_repository_has_none(self):
        default = (ROOT / "policies" / "default-review-policy.md").read_bytes()
        pipe = self.pipeline(bundle_kwargs={"policy": default, "policy_source": "central_default"})
        pipe.claude_result = None
        snapshot = support.snapshot_block(policy_source="central_default", policy_present=False)
        pipe.claude()
        # Re-key the normalized results' snapshot block so it reflects the bundle.
        document = json.loads(pipe.claude_result.read_text("utf-8"))
        self.assertEqual(document["snapshot"]["policy_source"], "central_default")
        self.assertFalse(document["snapshot"]["policy_present"])
        pipe.codex()
        sent = pipe.codex_requests[0]["input"].decode("utf-8")
        self.assertIn("source: central_default", sent)
        self.assertIn("DEFAULT REVIEW POLICY", sent)
        final = pipe.finalize()
        self.assertEqual(final["snapshot"]["policy_source"], snapshot["policy_source"])
        self.assertFalse(final["snapshot"]["policy_present"])
        self.assertIn("central_default", pipe.report())

    def test_repository_policy_is_recorded_as_such(self):
        pipe = self.pipeline()
        final = pipe.run_all()
        self.assertEqual(final["snapshot"]["policy_source"], "repository")
        self.assertTrue(final["snapshot"]["policy_present"])

    def test_fork_pull_request_is_reviewed_as_untrusted_data_and_flagged(self):
        pipe = self.pipeline(bundle_kwargs={"is_fork": True})
        final = pipe.run_all()
        self.assertTrue(final["snapshot"]["is_fork"])
        self.assertIn("fork: `true`", pipe.report())
        # Nothing about the fork widens what either AI can do.
        self.assertNotIn("--dangerously", json.dumps(pipe.claude_record["argv"]))
        codex_argv = pipe.codex_requests[0]["argv"]
        self.assertEqual(codex_argv[codex_argv.index("--sandbox") + 1], "read-only")
        self.assertIn("shell_tool", codex_argv)
        for name in GITHUB_CANARY_ENV:
            self.assertNotIn(name, pipe.claude_record["env"])


# -- central execution: the target repository needs nothing ---------------------------------


class TargetRepositoryNeedsNoWorkflowTests(PipelineCase):
    def test_prepare_reads_only_data_and_never_asks_the_target_for_workflows(self):
        from test_prepare_review import FakeGitHub, _run_prepare, build_standard_remote

        remote, shas = build_standard_remote(self.tmp)
        fake = FakeGitHub(shas, policy=None)  # the target has no policy and no workflow file
        manifest = _run_prepare(self.tmp, remote, fake)
        self.assertEqual(manifest["policy"]["source"], "central_default")
        paths = fake.requested_paths
        self.assertTrue(paths)
        for path in paths:
            self.assertNotIn("/actions/", path)
            self.assertNotIn("workflows", path)
            self.assertNotIn("/dispatches", path)
        methods = {method for method, _url, _headers in fake.requests}
        self.assertEqual(methods, {"GET"})

    def test_the_bundle_from_a_workflowless_target_drives_the_whole_pipeline(self):
        from test_prepare_review import FakeGitHub, _run_prepare, build_standard_remote

        remote, shas = build_standard_remote(self.tmp)
        _run_prepare(self.tmp, remote, FakeGitHub(shas, policy=None))
        real_bundle = self.tmp / "bundle"
        bundle = bundle_mod.load_bundle(real_bundle)
        self.assertEqual(bundle.policy_source, "central_default")
        self.assertFalse(bundle.policy_present)
        self.assertNotIn(b".github/workflows", bundle.diff)

        pipe = Pipeline.__new__(Pipeline)
        pipe.tmp = self.tmp
        pipe.bundle_dir = real_bundle
        pipe.claude_result = pipe.codex_result = None
        pipe.claude_job = pipe.codex_job = "skipped"
        pipe.codex_calls = []
        pipe.claude_record = None
        pipe.final = None
        pipe.final_dir = self.tmp / "final"

        reviewable = next(f.path for f in bundle.files if f.reviewable)
        pipe.claude(payload={**claude_tests.model_payload(), "findings": [{**claude_tests.model_payload()["findings"][0], "path": reviewable}]})
        pipe.codex(payload=codex_payload(claim_reviews=[claim_review()], additional_findings=[codex_finding(path=reviewable)]))
        final = pipe.finalize(expected_snapshot_id=bundle.snapshot_id)
        self.assertTrue(final["publishable"])
        self.assertEqual(final["snapshot"]["snapshot_id"], bundle.snapshot_id)
        self.assertFalse(final["snapshot"]["policy_present"])


if __name__ == "__main__":
    unittest.main()
