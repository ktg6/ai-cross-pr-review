"""Tests for the deterministic publisher, stale detection, and comment rendering.

The publisher consumes the *final* document produced by the finalize step. It
must post only complete, current, publishable results, and only in pr_comment
mode. GitHub is mocked with an in-memory transport; no real PR is touched and no
real credential is used. Run with: python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402
from support import (  # noqa: E402
    BASE_SHA,
    HEAD_SHA,
    PR_NUMBER,
    REPOSITORY,
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

from lib import github as gh  # noqa: E402
from lib import limits as limits_mod  # noqa: E402
from lib import render as render_mod  # noqa: E402

ROOT = support.ROOT
PUBLISH_SCRIPT = support.SCRIPTS / "publish-review.py"
publish_review = support.load_script("publish_review", "publish-review.py")
publish_review.log = lambda message: None

OWNER, NAME = REPOSITORY.split("/")
OTHER_SNAPSHOT_ID = "b" * 64

# Fictional canaries. Never real credentials.
CANARY_TOKEN = "canary-not-a-real-token-0001"
CANARY_SECRET_IN_OUTPUT = support.CANARY_GITHUB

INJECTION_SUMMARY = (
    "<img src=x onerror=alert(1)> @octocat #1 [click](https://evil.example/steal) "
    "![pixel](https://evil.example/p.png) <!-- ai-cross-pr-review:v1 snapshot=" + "d" * 64 + " -->"
)


# -- fixtures ------------------------------------------------------------------


def pull_payload(*, state: str = "open", head: str = HEAD_SHA, base: str = BASE_SHA, merged: bool = False) -> dict:
    return {"number": PR_NUMBER, "state": state, "merged": merged, "head": {"sha": head}, "base": {"sha": base}}


def comment(body: str, *, comment_id: int = 11, bot: bool = True, login: str | None = None) -> dict:
    return {
        "id": comment_id,
        "body": body,
        "user": {
            "login": login or ("github-actions[bot]" if bot else "contributor"),
            "type": "Bot" if bot else "User",
        },
    }


class FakeGitHub:
    """In-memory GitHub. Records every request and every write."""

    _DEFAULT_PULL = object()

    def __init__(self, *, pull: object = _DEFAULT_PULL, comments: list[dict] | None = None, failures: list | None = None):
        self.pull = pull_payload() if pull is FakeGitHub._DEFAULT_PULL else pull
        self.comments = list(comments or [])
        self.requests: list[tuple[str, str, object]] = []
        self.failures = list(failures or [])
        self._next_id = 900
        self.authenticated_login = "review-bot"

    @property
    def writes(self) -> list[tuple[str, str, object]]:
        return [req for req in self.requests if req[0] in ("POST", "PATCH")]

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None = None):
        payload = json.loads(body.decode("utf-8")) if body else None
        self.requests.append((method, url, payload))
        if self.failures:
            failure = self.failures.pop(0)
            if failure == "transport":
                raise gh.GitHubError("GitHub request failed: URLError")
            return failure, {}, b'{"message":"error"}'
        parsed = urllib.parse.urlsplit(url)
        path = parsed.path
        query = dict(urllib.parse.parse_qsl(parsed.query))
        base = f"/repos/{OWNER}/{NAME}"
        if method == "GET" and path == f"{base}/pulls/{PR_NUMBER}":
            if self.pull is None:
                return 404, {}, b'{"message":"Not Found"}'
            return 200, {}, json.dumps(self.pull).encode()
        if method == "GET" and path == "/user":
            return 200, {}, json.dumps({"login": self.authenticated_login}).encode()
        if method == "GET" and path == f"{base}/issues/{PR_NUMBER}/comments":
            per_page = int(query.get("per_page", 100))
            page = int(query.get("page", 1))
            chunk = self.comments[(page - 1) * per_page : page * per_page]
            return 200, {}, json.dumps(chunk).encode()
        if method == "POST" and path == f"{base}/issues/{PR_NUMBER}/comments":
            self._next_id += 1
            created = comment(payload["body"], comment_id=self._next_id)
            self.comments.append(created)
            return 201, {}, json.dumps(created).encode()
        match = re.fullmatch(rf"{re.escape(base)}/issues/comments/([0-9]+)", path)
        if method == "PATCH" and match:
            target = int(match.group(1))
            for item in self.comments:
                if item["id"] == target:
                    item["body"] = payload["body"]
                    return 200, {}, json.dumps(item).encode()
            return 404, {}, b'{"message":"Not Found"}'
        return 404, {}, b'{"message":"Not Found"}'


def final_document(tmp: Path, **kwargs) -> dict:
    document, _ = run_finalize(tmp, **kwargs)
    return document


class PublishCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ai-review-publish-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.slept: list[float] = []

    def make_document(self, **kwargs) -> dict:
        return final_document(self.tmp / "gen", **kwargs)

    def write_result(self, document: dict | None = None, *, name: str = "final-review.json") -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(document if document is not None else self.make_document()), encoding="utf-8")
        return path

    def publish(self, fake, *, result: Path | None = None, expected=SNAPSHOT_ID, **kwargs):
        return publish_review.publish(
            repository=kwargs.pop("repository", REPOSITORY),
            pr_number=kwargs.pop("pr_number", PR_NUMBER),
            result_file=result if result is not None else self.write_result(),
            expected_snapshot_id=expected,
            output_mode=kwargs.pop("output_mode", "pr_comment"),
            token=kwargs.pop("token", CANARY_TOKEN),
            transport=fake,
            sleep=self.slept.append,
            run_env=kwargs.pop("run_env", {}),
            **kwargs,
        )


# -- rendering -----------------------------------------------------------------


class RenderTests(PublishCase):
    def test_marker_is_the_first_line_and_carries_the_snapshot(self):
        body = render_mod.render_comment(self.make_document())
        self.assertEqual(body.split("\n", 1)[0], render_mod.marker(SNAPSHOT_ID))
        self.assertEqual(render_mod.read_marker(body), SNAPSHOT_ID)
        self.assertIn(f"Reviewed commit: `{HEAD_SHA}`", body)

    def test_usage_is_shown_in_the_job_summary_but_never_in_the_comment(self):
        document = self.make_document()
        summary = render_mod.render_summary(document)
        comment = render_mod.render_comment(document)
        self.assertIn("### 使用量（Job Summaryのみ）", summary)
        self.assertIn(f"Claude budget USD `{limits_mod.DEFAULT_LIMITS.claude_max_budget_usd}`", summary)
        self.assertIn(f"Codex max output tokens `{limits_mod.DEFAULT_LIMITS.codex_max_output_tokens}`", summary)
        self.assertNotIn("使用量", comment)
        self.assertNotIn("cost USD", comment)
        self.assertNotIn("budget", comment)

    def test_job_summary_has_no_marker(self):
        summary = render_mod.render_summary(self.make_document())
        self.assertNotIn(render_mod.MARKER_PREFIX, summary)
        self.assertIn(f"Reviewed commit: `{HEAD_SHA}`", summary)

    def test_marker_must_be_on_the_first_line(self):
        self.assertIsNone(render_mod.read_marker("quoted:\n" + render_mod.marker(SNAPSHOT_ID)))
        self.assertIsNone(render_mod.read_marker("<!-- ai-cross-pr-review:v1 snapshot=zz -->"))
        self.assertIsNone(render_mod.read_marker(None))

    def test_each_bucket_has_its_own_heading_and_provenance(self):
        findings = [claude_finding(title=f"f{i}") for i in range(4)]
        claude = claude_document()
        claude["review"] = {**claude["review"], "findings": findings}
        payload = codex_payload(
            claim_reviews=[
                claim_review(claude_index=0, status="adopted"),
                claim_review(claude_index=1, status="rejected", rationale="呼び出し側で処理済み。"),
                claim_review(claude_index=2, status="deferred", rationale="呼び出し元が不明。"),
                claim_review(claude_index=3, status="duplicate", duplicate_of=0, rationale="index 0と同一。"),
            ]
        )
        document = self.make_document(claude=claude, codex=codex_document(verification=payload))
        body = render_mod.render_comment(document)
        for heading in (
            "### Codexが採用したClaudeの指摘 (1)",
            "### Codexが追加した指摘 (1)",
            "### 判断保留 (1)",
            "### 不採用となったClaudeの指摘 (1)",
            "### 重複と判定されたClaudeの指摘 (1)",
        ):
            self.assertIn(heading, body)
        self.assertIn("呼び出し側で処理済み。", body)
        self.assertIn("指摘元: `claude`", body)
        self.assertIn("指摘元: `codex`", body)
        self.assertIn("重複元index: `0`", body)
        self.assertIn("### 情報不足", body)

    def test_stage_status_models_and_checks_are_visible(self):
        body = render_mod.render_comment(self.make_document())
        self.assertIn("一次レビュー(Claude): status `success`", body)
        self.assertIn("再検証(Codex): status `success`", body)
        self.assertIn("要求model `claude-opus-5` / 実使用model `claude-opus-5`", body)
        self.assertIn("要求model `gpt-5.6-sol` / 実使用model `gpt-5.6-sol-2026-04-24`", body)
        self.assertIn("schema `ok` / snapshot `ok`", body)
        self.assertIn("Review policy: `.github/ai-review.md` (source: `repository`", body)

    def test_failed_run_says_it_is_not_a_clean_result(self):
        document = self.make_document(codex=None, codex_job="failure")
        summary = render_mod.render_summary(document)
        self.assertIn("この実行は完了しなかった", summary)
        self.assertIn("status `failed`", summary)
        self.assertIn("not-publishable", summary)

    def test_html_links_images_and_mentions_are_neutralized(self):
        document = self.make_document()
        document["review"]["summary"] = INJECTION_SUMMARY
        body = render_mod.render_comment(document)
        rendered = body.split("### Summary", 1)[1].split("### Codexが採用", 1)[0]
        for char in "<>[]":
            self.assertNotIn(char, rendered.replace("\\" + char, ""), char)
        self.assertIn("\\<img src=x onerror=alert(1)\\>", rendered)
        self.assertIn("@\u200boctocat", rendered)
        self.assertIn("\\#\u200b1", rendered)
        self.assertNotIn("@octocat", rendered)
        self.assertNotIn("https://", rendered)
        self.assertIn("https:\u200b//evil.example", rendered)
        self.assertEqual(render_mod.read_marker(body), SNAPSHOT_ID)
        self.assertEqual(sum(1 for line in body.split("\n") if line.startswith(render_mod.MARKER_PREFIX)), 1)

    def test_oversized_comment_drops_the_least_important_entries(self):
        long_detail = "あ" * limits_mod.DEFAULT_LIMITS.max_finding_detail_chars
        findings = [claude_finding(title="重大", severity="high", detail=long_detail)] + [
            claude_finding(title=f"軽微{i}", severity="low", detail=long_detail) for i in range(19)
        ]
        claude = claude_document()
        claude["review"] = {**claude["review"], "findings": findings}
        claims = [claim_review(claude_index=i, status="adopted", severity="high" if i == 0 else "low") for i in range(20)]
        codex = codex_document(verification=codex_payload(claim_reviews=claims, additional_findings=[]))
        document = self.make_document(claude=claude, codex=codex)
        limits = limits_mod.Limits(max_comment_chars=12000)
        body = render_mod.render_comment(document, limits)
        self.assertLessEqual(len(body), limits.max_comment_chars)
        self.assertIn("### Codexが採用したClaudeの指摘 (20)", body)
        self.assertIn("重大", body)
        self.assertRegex(body, r"長さの上限により省略した項目: \d+件")

    def test_multiline_text_cannot_restructure_the_comment(self):
        document = self.make_document()
        document["review"]["summary"] = "見出し風\n---\n次の行"
        document["review"]["adopted"][0]["title"] = "一行目\n## 偽の見出し"
        document["review"]["limitations"] = ["一行目\n- 偽の項目"]
        body = render_mod.render_comment(document)
        headings = [line for line in body.split("\n") if line.startswith("#")]
        self.assertIn("#### 1. 一行目 \\#\\# 偽の見出し", headings)
        self.assertNotIn("## 偽の見出し", body.replace("\\#\\# 偽の見出し", ""))
        self.assertIn("\\---", body)
        self.assertEqual(body.count("\n---\n"), 1)

    def test_render_refuses_an_unusable_snapshot_id(self):
        document = self.make_document()
        document["snapshot"] = {**document["snapshot"], "snapshot_id": "nope"}
        with self.assertRaises(render_mod.RenderError):
            render_mod.render_comment(document)


# -- result verification -------------------------------------------------------


class ResultVerificationTests(PublishCase):
    def _stops(self, document, message: str | None = None, *, expected=SNAPSHOT_ID, exc=Exception):
        fake = FakeGitHub()
        with self.assertRaises(exc) as ctx:
            self.publish(fake, result=self.write_result(document), expected=expected)
        self.assertEqual(fake.writes, [])
        if message:
            self.assertIn(message, str(ctx.exception))

    def test_forged_artifact_for_another_snapshot_stops(self):
        document = self.make_document()
        self._stops(document, "does not belong", expected=OTHER_SNAPSHOT_ID, exc=publish_review.PublishError)

    def test_artifact_for_another_repository_or_pr_stops(self):
        document = self.make_document()
        with self.assertRaises(publish_review.PublishError):
            self.publish(FakeGitHub(), result=self.write_result(document), repository="acme/other")
        with self.assertRaises(publish_review.PublishError):
            self.publish(FakeGitHub(), result=self.write_result(document), pr_number=8)

    def test_malformed_artifact_stops(self):
        for name, data in (("empty.json", b""), ("bad.json", b"{not json"), ("list.json", b"[]"), ("bin.json", b"\xff\xfe")):
            with self.subTest(name=name):
                path = self.tmp / name
                path.write_bytes(data)
                fake = FakeGitHub()
                with self.assertRaises(Exception):
                    self.publish(fake, result=path)
                self.assertEqual(fake.writes, [])
        with self.assertRaises(publish_review.PublishError):
            self.publish(FakeGitHub(), result=self.tmp / "missing.json")

    def test_oversized_artifact_stops(self):
        path = self.tmp / "big.json"
        path.write_bytes(b" " * (limits_mod.DEFAULT_LIMITS.max_final_result_bytes + 1))
        fake = FakeGitHub()
        with self.assertRaises(limits_mod.LimitExceeded):
            self.publish(fake, result=path)
        self.assertEqual(fake.writes, [])

    def test_version_mismatch_stops(self):
        for key, value in (("framework_version", "0.0.1"), ("final_schema_version", "9")):
            with self.subTest(key=key):
                self._stops({**self.make_document(), key: value})

    def test_schema_violations_stop(self):
        def mutate(fn):
            document = self.make_document()
            fn(document)
            return document

        cases = {
            "unknown top-level field": lambda d: d.update(extra=1),
            "missing field": lambda d: d.pop("stages"),
            "bad severity": lambda d: d["review"]["adopted"][0].update(severity="critical"),
            "bad origin": lambda d: d["review"]["adopted"][0].update(origin="human"),
            "unknown entry field": lambda d: d["review"]["adopted"][0].update(extra="x"),
            "line zero": lambda d: d["review"]["adopted"][0].update(line=0),
            "line bool": lambda d: d["review"]["adopted"][0].update(line=True),
            "line huge": lambda d: d["review"]["adopted"][0].update(line=10**9),
            "empty title": lambda d: d["review"]["adopted"][0].update(title="  "),
            "bucket not a list": lambda d: d["review"].update(adopted={}),
            "bad stage status": lambda d: d["stages"]["codex"].update(status="ok"),
            "non-bool check": lambda d: d["verification"].update(schema_valid="yes"),
            "non-bool publishable": lambda d: d.update(publishable="true"),
            "negative dropped": lambda d: d["review"].update(dropped=-1),
            "too many limitations": lambda d: d["review"].update(limitations=["x"] * 11),
            "model outside allowlist": lambda d: d["request"].update(codex_model_requested="gpt-4"),
            "effort outside allowlist": lambda d: d["request"].update(claude_effort="ultra"),
            "bad output mode": lambda d: d["request"].update(output_mode="everywhere"),
            "bad policy source": lambda d: d["snapshot"].update(policy_source="pr_head"),
            "bad head sha": lambda d: d["snapshot"].update(head_sha="HEAD"),
            "unsorted path hashes": lambda d: d["snapshot"].update(reviewable_path_hashes=["b" * 64, "a" * 64]),
        }
        for label, fn in cases.items():
            with self.subTest(case=label):
                self._stops(mutate(fn))

    def test_entry_outside_the_reviewed_diff_stops(self):
        document = self.make_document()
        document["review"]["added"][0]["path"] = "somewhere/else.py"
        self._stops(document, "not a reviewable file", exc=Exception)

    def test_entry_with_a_forbidden_path_stops(self):
        document = self.make_document()
        document["review"]["added"][0]["path"] = ".env"
        self._stops(document, "not publishable")

    def test_publishable_flag_cannot_hide_a_failed_stage(self):
        document = self.make_document(codex=None, codex_job="failure")
        document["publishable"] = True
        self._stops(document, "requires both stages")

    def test_publishable_flag_cannot_hide_a_failed_check(self):
        document = self.make_document()
        document["verification"]["snapshot_match"] = False
        self._stops(document, "every verification check")

    def test_expected_snapshot_id_is_mandatory(self):
        with self.assertRaises(publish_review.PublishError):
            self.publish(FakeGitHub(), expected="")

    def test_secret_shaped_values_are_redacted_before_posting(self):
        document = self.make_document()
        document["review"]["adopted"][0]["detail"] = f"設定に{CANARY_SECRET_IN_OUTPUT}が含まれる。"
        fake = FakeGitHub()
        outcome = self.publish(fake, result=self.write_result(document))
        self.assertNotIn(CANARY_SECRET_IN_OUTPUT, outcome["body"])
        self.assertNotIn(CANARY_SECRET_IN_OUTPUT, json.dumps(fake.writes))
        self.assertIn("REDACTED", outcome["body"])
        self.assertIn("credentialらしき値", outcome["body"])

    def test_publisher_never_reads_ai_provider_secrets(self):
        source = PUBLISH_SCRIPT.read_text("utf-8")
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC", "OPENAI_API_KEY"):
            self.assertNotIn(name, source)


# -- what may be published -----------------------------------------------------


class PublishabilityTests(PublishCase):
    def test_summary_only_never_posts(self):
        document = self.make_document(output_mode="summary_only")
        fake = FakeGitHub()
        with self.assertRaisesRegex(publish_review.PublishError, "pr_comment"):
            self.publish(fake, result=self.write_result(document), output_mode="summary_only")
        self.assertEqual(fake.requests, [])

    def test_a_summary_only_result_cannot_be_posted_by_claiming_pr_comment(self):
        document = self.make_document(output_mode="summary_only")
        fake = FakeGitHub()
        with self.assertRaisesRegex(publish_review.PublishError, "different output mode"):
            self.publish(fake, result=self.write_result(document), output_mode="pr_comment")
        self.assertEqual(fake.writes, [])

    def test_failed_stages_are_never_posted(self):
        cases = {
            "codex failed": dict(codex=None, codex_job="failure"),
            "claude failed": dict(claude=None, codex=None, claude_job="failure", codex_job="skipped"),
            "codex artifact corrupt": dict(codex=b"{oops"),
            "claude for another snapshot": dict(claude=claude_document(snapshot=snapshot_block(snapshot_id=OTHER_SNAPSHOT_ID))),
            "codex for another snapshot": dict(codex=codex_document(snapshot=snapshot_block(snapshot_id=OTHER_SNAPSHOT_ID))),
        }
        for label, kwargs in cases.items():
            with self.subTest(case=label):
                document = self.make_document(**kwargs)
                fake = FakeGitHub()
                with self.assertRaisesRegex(publish_review.PublishError, "not publishable"):
                    self.publish(fake, result=self.write_result(document))
                self.assertEqual(fake.requests, [], "nothing may even be read from GitHub")

    def test_empty_but_complete_review_is_publishable(self):
        claude = claude_document()
        claude["review"] = {**claude["review"], "findings": []}
        codex = codex_document(verification=codex_payload(claim_reviews=[], additional_findings=[]))
        outcome = self.publish(FakeGitHub(), result=self.write_result(self.make_document(claude=claude, codex=codex)))
        self.assertEqual(outcome["action"], "created")
        self.assertIn("該当なし", outcome["body"])


# -- stale detection -----------------------------------------------------------


class StaleTests(PublishCase):
    def _stops_on(self, pull, message):
        fake = FakeGitHub(pull=pull)
        with self.assertRaises(publish_review.PublishError) as ctx:
            self.publish(fake)
        self.assertIn(message, str(ctx.exception))
        self.assertEqual(fake.writes, [])

    def test_moved_head_is_not_published(self):
        self._stops_on(pull_payload(head="9" * 40), "head moved")

    def test_moved_base_is_not_published(self):
        self._stops_on(pull_payload(base="9" * 40), "base moved")

    def test_closed_or_merged_pr_is_not_published(self):
        self._stops_on(pull_payload(state="closed"), "no longer open")
        self._stops_on(pull_payload(merged=True), "no longer open")

    def test_deleted_pr_is_not_published(self):
        self._stops_on(None, "no longer exists")

    def test_state_is_checked_before_any_write(self):
        fake = FakeGitHub(pull=pull_payload(head="9" * 40))
        with self.assertRaises(publish_review.PublishError):
            self.publish(fake)
        self.assertEqual([method for method, _url, _body in fake.requests], ["GET"])

    def test_state_is_rechecked_immediately_before_write(self):
        fake = FakeGitHub()
        pull_reads = 0

        def transport(method, url, headers, body=None):
            nonlocal pull_reads
            if method == "GET" and f"/pulls/{PR_NUMBER}" in url:
                pull_reads += 1
                if pull_reads == 2:
                    fake.pull = pull_payload(head="9" * 40)
            return fake(method, url, headers, body)

        with self.assertRaisesRegex(publish_review.PublishError, "head moved"):
            self.publish(transport)
        self.assertEqual(pull_reads, 2)
        self.assertEqual(fake.writes, [])


# -- comment identity ----------------------------------------------------------


class CommentIdentityTests(PublishCase):
    def test_first_run_creates_a_comment(self):
        fake = FakeGitHub()
        outcome = self.publish(fake)
        self.assertEqual(outcome["action"], "created")
        self.assertEqual(len(fake.writes), 1)
        method, url, payload = fake.writes[0]
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith(f"/repos/{OWNER}/{NAME}/issues/{PR_NUMBER}/comments"))
        self.assertEqual(render_mod.read_marker(payload["body"]), SNAPSHOT_ID)

    def test_destination_is_fixed_by_the_request_not_by_the_result(self):
        fake = FakeGitHub()
        self.publish(fake)
        for _method, url, _payload in fake.requests:
            if not url.endswith("/user"):
                self.assertTrue(url.startswith("https://api.github.com/repos/acme/widgets/"), url)

    def test_rerun_of_the_same_snapshot_updates_in_place(self):
        fake = FakeGitHub()
        self.publish(fake)
        second = self.publish(fake)
        self.assertEqual(second["action"], "updated")
        self.assertEqual(len(fake.comments), 1)
        self.assertEqual([w[0] for w in fake.writes], ["POST", "PATCH"])

    def test_a_different_snapshot_gets_a_new_comment(self):
        other = render_mod.marker(OTHER_SNAPSHOT_ID) + "\nold review"
        fake = FakeGitHub(comments=[comment(other)])
        outcome = self.publish(fake)
        self.assertEqual(outcome["action"], "created")
        self.assertEqual(len(fake.comments), 2)

    def test_forged_marker_from_a_human_is_ignored(self):
        fake = FakeGitHub(comments=[comment(render_mod.marker(SNAPSHOT_ID) + "\nhi", bot=False)])
        outcome = self.publish(fake)
        self.assertEqual(outcome["action"], "created")
        self.assertEqual(fake.comments[0]["body"].split("\n", 1)[1], "hi")

    def test_marker_from_the_pat_identity_is_updated(self):
        fake = FakeGitHub(
            comments=[comment(render_mod.marker(SNAPSHOT_ID) + "\nold", bot=False, login="review-bot")]
        )
        outcome = self.publish(fake)
        self.assertEqual(outcome["action"], "updated")
        self.assertEqual([method for method, _url, _payload in fake.writes], ["PATCH"])

    def test_marker_quoted_inside_a_comment_is_ignored(self):
        fake = FakeGitHub(comments=[comment("> " + render_mod.marker(SNAPSHOT_ID))])
        self.assertEqual(self.publish(fake)["action"], "created")

    def test_oldest_matching_comment_wins(self):
        mine = render_mod.marker(SNAPSHOT_ID) + "\nold"
        fake = FakeGitHub(comments=[comment(mine, comment_id=30), comment(mine, comment_id=20)])
        outcome = self.publish(fake)
        self.assertEqual(outcome["action"], "updated")
        self.assertEqual(outcome["comment_id"], "20")

    def test_paginated_comments_are_scanned(self):
        filler = [comment("noise", comment_id=1000 + i) for i in range(150)]
        mine = comment(render_mod.marker(SNAPSHOT_ID) + "\nold", comment_id=5000)
        fake = FakeGitHub(comments=filler + [mine])
        self.assertEqual(self.publish(fake)["action"], "updated")

    def test_too_many_comments_stop_instead_of_duplicating(self):
        fake = FakeGitHub(comments=[comment("noise", comment_id=1000 + i) for i in range(1000)])
        limits = limits_mod.Limits(max_comment_pages=2)
        with self.assertRaises(gh.GitHubError):
            publish_review.publish(
                repository=REPOSITORY,
                pr_number=PR_NUMBER,
                result_file=self.write_result(),
                expected_snapshot_id=SNAPSHOT_ID,
                output_mode="pr_comment",
                token=CANARY_TOKEN,
                transport=fake,
                limits=limits,
                run_env={},
            )
        self.assertEqual(fake.writes, [])


# -- API failures and retry ----------------------------------------------------


class ApiFailureTests(PublishCase):
    def test_read_failures_are_retried_then_succeed(self):
        outcome = self.publish(FakeGitHub(failures=[500, "transport"]))
        self.assertEqual(outcome["action"], "created")
        self.assertEqual(len(self.slept), 2)

    def test_read_failures_beyond_the_retry_budget_stop(self):
        fake = FakeGitHub(failures=[500, 500, 500])
        with self.assertRaises(gh.GitHubError):
            self.publish(fake)
        self.assertEqual(fake.writes, [])

    def test_create_is_not_retried_on_server_error(self):
        """A 5xx on POST may already have created the comment, so never repeat it."""
        fake = FakeGitHub()

        def transport(method, url, headers, body=None):
            if method == "POST":
                fake.requests.append((method, url, None))
                return 502, {}, b'{"message":"bad gateway"}'
            return FakeGitHub.__call__(fake, method, url, headers, body)

        with self.assertRaises(gh.GitHubError):
            self.publish(transport)
        self.assertEqual(len([r for r in fake.requests if r[0] == "POST"]), 1)

    def test_rate_limited_write_is_retried(self):
        calls = {"post": 0}
        fake = FakeGitHub()

        def transport(method, url, headers, body=None):
            if method == "POST":
                calls["post"] += 1
                if calls["post"] == 1:
                    return 429, {}, b'{"message":"rate limited"}'
            return FakeGitHub.__call__(fake, method, url, headers, body)

        self.assertEqual(self.publish(transport)["action"], "created")
        self.assertEqual(calls["post"], 2)

    def test_missing_token_stops_before_reading_anything(self):
        fake = FakeGitHub()
        with self.assertRaises(publish_review.PublishError):
            self.publish(fake, token=None)
        self.assertEqual(fake.requests, [])

    def test_invalid_pr_number_or_repository_stops(self):
        with self.assertRaises(gh.ValidationError):
            self.publish(FakeGitHub(), pr_number="0")
        with self.assertRaises(gh.ValidationError):
            self.publish(FakeGitHub(), repository="acme")

    def test_only_the_comment_token_is_sent_and_never_leaks_into_the_body(self):
        seen: list[dict] = []
        fake = FakeGitHub()

        def transport(method, url, headers, body=None):
            seen.append(headers)
            return fake(method, url, headers, body)

        outcome = self.publish(transport)
        self.assertTrue(all(h.get("Authorization") == f"Bearer {CANARY_TOKEN}" for h in seen))
        self.assertNotIn(CANARY_TOKEN, outcome["body"])


# -- CLI surface ---------------------------------------------------------------


class CliTests(PublishCase):
    def test_outputs_are_written_for_the_workflow(self):
        output = self.tmp / "gh-output"
        self.publish(FakeGitHub(), run_env={"GITHUB_OUTPUT": str(output)})
        written = dict(line.split("=", 1) for line in output.read_text("utf-8").splitlines())
        self.assertEqual(written["comment_action"], "created")
        self.assertEqual(written["head_sha"], HEAD_SHA)
        self.assertEqual(written["snapshot_id"], SNAPSHOT_ID)
        self.assertTrue(written["comment_id"].isdigit())

    def test_main_returns_stop_on_failure_and_never_raises(self):
        code = publish_review.main(
            ["--repository", REPOSITORY, "--pr-number", str(PR_NUMBER), "--result-file", str(self.tmp / "missing.json")]
        )
        self.assertEqual(code, publish_review.EXIT_STOP)

    def test_main_defaults_to_refusing_without_a_token(self):
        result = self.write_result()
        code = publish_review.main(
            ["--repository", REPOSITORY, "--pr-number", str(PR_NUMBER), "--result-file", str(result),
             "--expected-snapshot-id", SNAPSHOT_ID]
        )
        self.assertEqual(code, publish_review.EXIT_STOP)


# -- no write path outside the publisher --------------------------------------


class NoWriteOutsideThePublisherTests(unittest.TestCase):
    def test_no_merge_push_or_approve_anywhere_in_the_publisher(self):
        source = PUBLISH_SCRIPT.read_text("utf-8")
        for forbidden in ("/merge", "/reviews", "git push", "approve"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_only_the_publisher_and_github_client_can_write(self):
        writers = []
        for path in sorted(support.SCRIPTS.rglob("*.py")):
            text = path.read_text("utf-8")
            if re.search(r"create_issue_comment|update_issue_comment", text):
                writers.append(path.relative_to(support.SCRIPTS).as_posix())
        self.assertEqual(writers, ["lib/github.py", "publish-review.py"])


if __name__ == "__main__":
    unittest.main()
