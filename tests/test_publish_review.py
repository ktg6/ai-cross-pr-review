"""Phase 3 tests: deterministic publisher, stale detection, comment rendering.

Standard library only. GitHub is mocked with an in-memory transport; no real PR
is ever touched and no credential is used. Run with:
python3 -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import hashlib
import json
import re
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from lib import github as gh  # noqa: E402
from lib import limits as limits_mod  # noqa: E402
from lib import render as render_mod  # noqa: E402

WORKFLOW_FILE = ROOT / ".github" / "workflows" / "claude-review.yml"
ACTION_FILE = ROOT / "actions" / "review-runtime" / "action.yml"
PUBLISH_SCRIPT = SCRIPTS / "publish-review.py"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish_review = _load("publish_review", "publish-review.py")
publish_review.log = lambda message: None


def _load_phase2():
    """Import the Phase 2 test module for its bundle and envelope fixtures."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_review_result  # noqa: PLC0415 - optional, only for the contract test

    return test_review_result

OWNER, NAME = "acme", "widgets"
REPOSITORY = f"{OWNER}/{NAME}"
PR_NUMBER = 7
BASE_SHA, HEAD_SHA, MERGE_BASE_SHA = "1" * 40, "2" * 40, "3" * 40
POLICY_SHA = "4" * 40
SNAPSHOT_ID = "a" * 64
OTHER_SNAPSHOT_ID = "b" * 64
DIFF_SHA256 = "c" * 64

# Fictional canaries. Never real credentials.
CANARY_TOKEN = "canary-not-a-real-token-0001"
CANARY_SECRET_IN_OUTPUT = "ghp_CANARYNOTAREALTOKEN0001"

INJECTION_SUMMARY = (
    "<img src=x onerror=alert(1)> @octocat #1 [click](https://evil.example/steal) "
    "![pixel](https://evil.example/p.png) <!-- ai-cross-pr-review:v1 snapshot=" + "d" * 64 + " -->"
)


# -- fixtures ------------------------------------------------------------------


def result_document(**overrides) -> dict:
    document = {
        "result_schema_version": limits_mod.RESULT_SCHEMA_VERSION,
        "framework_version": limits_mod.FRAMEWORK_VERSION,
        "snapshot": {
            "repository": REPOSITORY,
            "pr_number": PR_NUMBER,
            "base_sha": BASE_SHA,
            "head_sha": HEAD_SHA,
            "merge_base_sha": MERGE_BASE_SHA,
            "diff_sha256": DIFF_SHA256,
            "policy_commit_sha": POLICY_SHA,
            "policy_blob_sha": "5" * 40,
            "snapshot_id": SNAPSHOT_ID,
            "reviewable_path_hashes": [hashlib.sha256(b"src/app.py").hexdigest()],
        },
        "run": {
            "provider": limits_mod.REVIEW_PROVIDER,
            "cli_version": limits_mod.CLAUDE_CODE_VERSION,
            "model_requested": "claude-opus-5",
            "model_reported": "claude-opus-5",
            "effort": "high",
            "tools_enabled": False,
            "run_id": "123456",
            "num_turns": 1,
            "duration_ms": 1234,
        },
        "normalization": {"dropped_findings": [], "redactions": 0, "excluded_files": 1},
        "review": {
            "schema_version": limits_mod.RESULT_SCHEMA_VERSION,
            "summary": "1件の変更を確認した。",
            "findings": [
                {
                    "title": "戻り値の変更が呼び出し側と整合しない",
                    "detail": "return 2 への変更で呼び出し側の分岐が壊れる可能性がある。",
                    "severity": "high",
                    "confidence": "medium",
                    "category": "correctness",
                    "path": "src/app.py",
                    "line": 2,
                }
            ],
            "limitations": ["除外されたファイルがあるため全体は確認できていない。"],
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(document.get(key), dict):
            document[key] = {**document[key], **value}
        else:
            document[key] = value
    return document


def finding(**overrides) -> dict:
    item = {
        "title": "タイトル",
        "detail": "詳細",
        "severity": "medium",
        "confidence": "medium",
        "category": "correctness",
        "path": "src/app.py",
    }
    item.update(overrides)
    return item


def pull_payload(*, state: str = "open", head: str = HEAD_SHA, base: str = BASE_SHA, merged: bool = False) -> dict:
    return {
        "number": PR_NUMBER,
        "state": state,
        "merged": merged,
        "head": {"sha": head},
        "base": {"sha": base},
    }


def comment(body: str, *, comment_id: int = 11, bot: bool = True) -> dict:
    return {
        "id": comment_id,
        "body": body,
        "user": {"login": "github-actions[bot]" if bot else "contributor", "type": "Bot" if bot else "User"},
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


class PublishCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ai-review-phase3-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.slept: list[float] = []

    def write_result(self, document: dict | None = None, *, name: str = "review-result.json") -> Path:
        path = self.tmp / name
        path.write_text(json.dumps(document if document is not None else result_document()), encoding="utf-8")
        return path

    def publish(self, fake: FakeGitHub, *, result: Path | None = None, expected=SNAPSHOT_ID, **kwargs):
        return publish_review.publish(
            repository=kwargs.pop("repository", REPOSITORY),
            pr_number=kwargs.pop("pr_number", PR_NUMBER),
            result_file=result if result is not None else self.write_result(),
            expected_snapshot_id=expected,
            token=kwargs.pop("token", CANARY_TOKEN),
            transport=fake,
            sleep=self.slept.append,
            run_env=kwargs.pop("run_env", {}),
            **kwargs,
        )


# -- rendering -----------------------------------------------------------------


class RenderTests(unittest.TestCase):
    def test_marker_is_the_first_line_and_carries_the_snapshot(self):
        body = render_mod.render_comment(result_document())
        first = body.split("\n", 1)[0]
        self.assertEqual(first, render_mod.marker(SNAPSHOT_ID))
        self.assertEqual(render_mod.read_marker(body), SNAPSHOT_ID)
        self.assertIn(f"Reviewed commit: `{HEAD_SHA}`", body)

    def test_marker_must_be_on_the_first_line(self):
        self.assertIsNone(render_mod.read_marker("quoted:\n" + render_mod.marker(SNAPSHOT_ID)))
        self.assertIsNone(render_mod.read_marker("<!-- ai-cross-pr-review:v1 snapshot=zz -->"))
        self.assertIsNone(render_mod.read_marker(None))

    def test_html_links_images_and_mentions_are_neutralized(self):
        document = result_document(review={**result_document()["review"], "summary": INJECTION_SUMMARY})
        body = render_mod.render_comment(document)
        rendered = body.split("### Summary", 1)[1].split("### Findings", 1)[0]
        # Every HTML and link construct is backslash-escaped, so none of it can
        # become markup, an image request, a mention, or an issue backlink.
        for char in "<>[]":
            self.assertNotIn(char, rendered.replace("\\" + char, ""), char)
        self.assertIn("\\<img src=x onerror=alert(1)\\>", rendered)
        self.assertIn("@\u200boctocat", rendered)
        self.assertIn("\\#\u200b1", rendered)
        self.assertNotIn("@octocat", rendered)
        self.assertNotIn("https://", rendered)
        self.assertIn("https:\u200b//evil.example", rendered)
        # The forged marker inside model text cannot become a marker line.
        self.assertEqual(render_mod.read_marker(body), SNAPSHOT_ID)
        self.assertEqual(
            sum(1 for line in body.split("\n") if line.startswith(render_mod.MARKER_PREFIX)), 1
        )

    def test_findings_keep_severity_order_and_locations(self):
        body = render_mod.render_comment(result_document())
        self.assertIn("### Findings (1)", body)
        self.assertIn("`src/app.py:2`", body)
        self.assertIn("severity: `high`", body)

    def test_empty_findings_render_a_clean_comment(self):
        document = result_document()
        document["review"] = {**document["review"], "findings": [], "limitations": []}
        body = render_mod.render_comment(document)
        self.assertIn("### Findings (0)", body)
        self.assertNotIn("### Limitations", body)

    def test_oversized_comment_drops_the_least_severe_findings(self):
        document = result_document()
        long_detail = "あ" * limits_mod.DEFAULT_LIMITS.max_finding_detail_chars
        document["review"]["findings"] = [
            finding(severity="high", detail=long_detail, title="重大"),
            *[finding(severity="low", detail=long_detail) for _ in range(19)],
        ]
        limits = limits_mod.Limits(max_comment_chars=12000)
        body = render_mod.render_comment(document, limits)
        self.assertLessEqual(len(body), limits.max_comment_chars)
        self.assertIn("### Findings (20)", body)
        self.assertIn("重大", body)
        self.assertRegex(body, r"コメント長の上限により省略したfinding: \d+件")

    def test_multiline_and_setext_text_cannot_restructure_the_comment(self):
        document = result_document()
        document["review"] = {
            **document["review"],
            "summary": "見出し風\n---\n次の行",
            "findings": [finding(title="一行目\n## 偽の見出し")],
            "limitations": ["一行目\n- 偽の項目"],
        }
        body = render_mod.render_comment(document)
        headings = [line for line in body.split("\n") if line.startswith("#")]
        self.assertEqual(headings, ["## AI Review (Claude)", "### Summary", "### Findings (1)",
                                    "#### 1. 一行目 \\#\\# 偽の見出し", "### Limitations", "### Notes"])
        self.assertIn("\\---", body)
        self.assertEqual(body.count("\n---\n"), 1)

    def test_render_refuses_an_unusable_snapshot_id(self):
        document = result_document()
        document["snapshot"] = {**document["snapshot"], "snapshot_id": "nope"}
        with self.assertRaises(render_mod.RenderError):
            render_mod.render_comment(document)


# -- result verification -------------------------------------------------------


class ResultVerificationTests(PublishCase):
    def _stops(self, document, message: str | None = None, *, expected=SNAPSHOT_ID):
        fake = FakeGitHub()
        with self.assertRaises(publish_review.PublishError) as ctx:
            self.publish(fake, result=self.write_result(document), expected=expected)
        self.assertEqual(fake.writes, [])
        if message:
            self.assertIn(message, str(ctx.exception))
        return ctx.exception

    def test_forged_artifact_for_another_snapshot_stops(self):
        document = result_document()
        document["snapshot"] = {**document["snapshot"], "snapshot_id": OTHER_SNAPSHOT_ID}
        self._stops(document, "does not belong to the snapshot prepared by this run")

    def test_artifact_for_another_repository_or_pr_stops(self):
        document = result_document()
        document["snapshot"] = {**document["snapshot"], "repository": "evil/other"}
        self._stops(document, "different repository")
        document = result_document()
        document["snapshot"] = {**document["snapshot"], "pr_number": 8}
        self._stops(document, "different pull request")

    def test_result_produced_with_tools_or_another_provider_stops(self):
        self._stops(result_document(run={"tools_enabled": True}), "every tool disabled")
        self._stops(result_document(run={"provider": "someone-else"}), "unknown provider")

    def test_version_mismatch_stops(self):
        self._stops(result_document(result_schema_version="99"), "unsupported result schema version")
        self._stops(result_document(framework_version="0.0.1"), "different framework version")

    def test_malformed_artifact_stops(self):
        path = self.tmp / "broken.json"
        path.write_text("not json", encoding="utf-8")
        fake = FakeGitHub()
        with self.assertRaises(publish_review.PublishError):
            self.publish(fake, result=path)
        self.assertEqual(fake.writes, [])
        with self.assertRaises(publish_review.PublishError):
            self.publish(FakeGitHub(), result=self.tmp / "missing.json")

    def test_oversized_artifact_stops(self):
        document = result_document()
        document["review"]["limitations"] = ["x" * 400] * 10
        path = self.write_result(document)
        limits = limits_mod.Limits(max_result_bytes=100)
        with self.assertRaises(limits_mod.LimitExceeded):
            publish_review.publish(
                repository=REPOSITORY,
                pr_number=PR_NUMBER,
                result_file=path,
                expected_snapshot_id=SNAPSHOT_ID,
                token=CANARY_TOKEN,
                transport=FakeGitHub(),
                limits=limits,
                run_env={},
            )

    def test_schema_violations_in_the_review_stop(self):
        for review in (
            {"schema_version": "2"},
            {"findings": "not-a-list"},
            {"findings": [finding(severity="critical")]},
            {"findings": [finding(category="unknown")]},
            {"findings": [finding(line=0)]},
            {"findings": [finding(line=True)]},
            {"findings": [{**finding(), "extra": 1}]},
            {"findings": [finding(path="../../etc/passwd")]},
            {"findings": [finding(path=".env")]},
            {"findings": [finding(path="a\nb")]},
            {"findings": [finding() for _ in range(21)]},
            {"summary": ""},
            {"summary": 5},
        ):
            with self.subTest(review=sorted(review)):
                self._stops(result_document(review=review))

    def test_finding_outside_the_reviewed_diff_stops(self):
        review = {**result_document()["review"], "findings": [finding(path="src/other.py")]}
        self._stops(result_document(review=review), "not a changed reviewable file")

    def test_unknown_fields_in_normalized_artifact_stop(self):
        documents = []
        for section in (None, "snapshot", "run", "normalization"):
            document = result_document()
            target = document if section is None else document[section]
            target["unexpected"] = True
            documents.append((section or "result", document))
        for section, document in documents:
            with self.subTest(section=section):
                self._stops(document, "unknown fields")

    def test_malformed_normalization_metadata_stops(self):
        for normalization in (
            {"redactions": -1},
            {"redactions": True},
            {"excluded_files": "1"},
            {"dropped_findings": [{"index": 0}]},
            {"dropped_findings": [{"index": -1, "reason": "invalid"}]},
        ):
            with self.subTest(normalization=normalization):
                self._stops(result_document(normalization=normalization))

    def test_expected_snapshot_id_is_mandatory(self):
        self._stops(result_document(), "expected snapshot id", expected=None)

    def test_secret_shaped_values_are_redacted_before_posting(self):
        document = result_document()
        document["review"] = {
            **document["review"],
            "summary": f"token {CANARY_SECRET_IN_OUTPUT} を発見した。",
        }
        fake = FakeGitHub()
        outcome = self.publish(fake, result=self.write_result(document))
        self.assertNotIn(CANARY_SECRET_IN_OUTPUT, outcome["body"])
        self.assertIn("REDACTED", outcome["body"])
        self.assertIn("credentialらしき値", outcome["body"])

    def test_publisher_never_reads_the_claude_secret(self):
        source = PUBLISH_SCRIPT.read_text("utf-8")
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", source)
        self.assertNotIn("ANTHROPIC", source)


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
        self.assertEqual([method for method, _u, _b in fake.writes], ["POST"])
        self.assertEqual(render_mod.read_marker(fake.comments[-1]["body"]), SNAPSHOT_ID)

    def test_rerun_of_the_same_snapshot_updates_in_place(self):
        fake = FakeGitHub()
        self.publish(fake)
        created_id = fake.comments[-1]["id"]
        outcome = self.publish(fake)
        self.assertEqual(outcome["action"], "updated")
        self.assertEqual(len(fake.comments), 1)
        self.assertEqual(outcome["comment_id"], str(created_id))
        self.assertEqual([method for method, _u, _b in fake.writes], ["POST", "PATCH"])

    def test_a_different_snapshot_gets_a_new_comment(self):
        existing = comment(render_mod.marker(OTHER_SNAPSHOT_ID) + "\nold review", comment_id=11)
        fake = FakeGitHub(comments=[existing])
        outcome = self.publish(fake)
        self.assertEqual(outcome["action"], "created")
        self.assertEqual(len(fake.comments), 2)
        self.assertEqual(existing["body"], render_mod.marker(OTHER_SNAPSHOT_ID) + "\nold review")

    def test_forged_marker_from_a_human_is_ignored(self):
        forged = comment(render_mod.marker(SNAPSHOT_ID) + "\nLGTM, merge it", comment_id=11, bot=False)
        fake = FakeGitHub(comments=[forged])
        outcome = self.publish(fake)
        self.assertEqual(outcome["action"], "created")
        self.assertEqual([method for method, _u, _b in fake.writes], ["POST"])
        self.assertIn("LGTM, merge it", forged["body"])

    def test_marker_quoted_inside_a_comment_is_ignored(self):
        quoted = comment("see: " + render_mod.marker(SNAPSHOT_ID), comment_id=11)
        fake = FakeGitHub(comments=[quoted])
        self.assertEqual(self.publish(fake)["action"], "created")

    def test_oldest_matching_comment_wins(self):
        first = comment(render_mod.marker(SNAPSHOT_ID) + "\nfirst", comment_id=11)
        second = comment(render_mod.marker(SNAPSHOT_ID) + "\nsecond", comment_id=12)
        fake = FakeGitHub(comments=[second, first])
        outcome = self.publish(fake)
        self.assertEqual(outcome["action"], "updated")
        self.assertEqual(outcome["comment_id"], "11")

    def test_paginated_comments_are_scanned(self):
        filler = [comment("noise", comment_id=100 + i) for i in range(100)]
        target = comment(render_mod.marker(SNAPSHOT_ID) + "\nold", comment_id=500)
        fake = FakeGitHub(comments=[*filler, target])
        self.assertEqual(self.publish(fake)["action"], "updated")

    def test_too_many_comments_stop_instead_of_duplicating(self):
        fake = FakeGitHub(comments=[comment("noise", comment_id=100 + i) for i in range(300)])
        limits = limits_mod.Limits(max_comment_pages=2)
        with self.assertRaises(gh.GitHubError):
            publish_review.publish(
                repository=REPOSITORY,
                pr_number=PR_NUMBER,
                result_file=self.write_result(),
                expected_snapshot_id=SNAPSHOT_ID,
                token=CANARY_TOKEN,
                transport=fake,
                limits=limits,
                run_env={},
            )
        self.assertEqual(fake.writes, [])


# -- API failures and retry ----------------------------------------------------


class ApiFailureTests(PublishCase):
    def test_read_failures_are_retried_then_succeed(self):
        fake = FakeGitHub(failures=[500, "transport"])
        outcome = self.publish(fake)
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
        """429 means the request was refused, so repeating it cannot duplicate."""
        calls = {"post": 0}
        fake = FakeGitHub()

        def transport(method, url, headers, body=None):
            if method == "POST":
                calls["post"] += 1
                if calls["post"] == 1:
                    return 429, {}, b'{"message":"rate limited"}'
            return FakeGitHub.__call__(fake, method, url, headers, body)

        outcome = self.publish(transport)
        self.assertEqual(outcome["action"], "created")
        self.assertEqual(calls["post"], 2)

    def test_missing_token_stops(self):
        with self.assertRaises(publish_review.PublishError):
            self.publish(FakeGitHub(), token=None)

    def test_invalid_pr_number_or_repository_stops(self):
        with self.assertRaises(gh.ValidationError):
            self.publish(FakeGitHub(), pr_number="0")
        with self.assertRaises(gh.ValidationError):
            self.publish(FakeGitHub(), repository="acme")


# -- CLI surface ---------------------------------------------------------------


class CliTests(PublishCase):
    def test_outputs_are_written_for_the_workflow(self):
        output = self.tmp / "gh-output"
        fake = FakeGitHub()
        self.publish(fake, run_env={"GITHUB_OUTPUT": str(output)})
        written = dict(line.split("=", 1) for line in output.read_text("utf-8").splitlines())
        self.assertEqual(written["comment_action"], "created")
        self.assertEqual(written["head_sha"], HEAD_SHA)
        self.assertEqual(written["snapshot_id"], SNAPSHOT_ID)
        self.assertTrue(written["comment_id"].isdigit())

    def test_main_returns_stop_on_failure_and_never_raises(self):
        code = publish_review.main(
            [
                "--repository",
                REPOSITORY,
                "--pr-number",
                str(PR_NUMBER),
                "--result-file",
                str(self.tmp / "missing.json"),
            ]
        )
        self.assertEqual(code, publish_review.EXIT_STOP)


# -- contract with the review job ----------------------------------------------


class NormalizerContractTests(PublishCase):
    """The publisher must accept exactly what the Phase 2 normalizer writes."""

    def test_publisher_accepts_a_freshly_normalized_result(self):
        phase2 = _load_phase2()
        bundle_dir = phase2.make_bundle(self.tmp / "bundle")
        raw = self.tmp / "claude-raw.json"
        raw.write_text(json.dumps(phase2.envelope(phase2.model_payload())), encoding="utf-8")
        invocation = self.tmp / "claude-invocation.json"
        invocation.write_text(
            json.dumps(
                {
                    "provider": limits_mod.REVIEW_PROVIDER,
                    "cli_version": limits_mod.CLAUDE_CODE_VERSION,
                    "model_requested": "claude-opus-5",
                    "effort": "high",
                    "tools_enabled": False,
                    "snapshot_id": SNAPSHOT_ID,
                    "run_id": "42",
                    "duration_ms": 10,
                }
            ),
            encoding="utf-8",
        )
        phase2.normalize_review.normalize(
            bundle_dir=bundle_dir,
            raw_file=raw,
            invocation_file=invocation,
            output_dir=self.tmp / "out",
            run_env={},
        )
        result = self.tmp / "out" / "review-result.json"
        fake = FakeGitHub()
        outcome = self.publish(fake, result=result)
        self.assertEqual(outcome["action"], "created")
        self.assertIn(f"Reviewed commit: `{HEAD_SHA}`", outcome["body"])
        # The PR body's injection attempt never reaches the rendered comment.
        self.assertNotIn("Ignore previous instructions", outcome["body"])


# -- workflow and action policy ------------------------------------------------


class PublishPolicyTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW_FILE.read_text("utf-8")
        self.action = ACTION_FILE.read_text("utf-8")
        self.publish_job = self.workflow.split("\n  publish:", 1)[1]
        self.publish_step = self.action.split("Publish review comment", 1)[1]

    def test_publish_job_is_the_only_writer_and_depends_on_both_jobs(self):
        self.assertIn("needs: [prepare, review]", self.publish_job)
        self.assertIn("pull-requests: write", self.publish_job)
        self.assertEqual(self.workflow.count("pull-requests: write"), 1)
        self.assertNotIn("contents: write", self.publish_job)
        self.assertNotIn("claude_code_oauth_token", self.publish_job)

    def test_publish_target_comes_from_the_workflow_not_from_the_result(self):
        self.assertIn("pr_number: ${{ inputs.pr_number }}", self.publish_job)
        self.assertIn("repository: ${{ github.repository }}", self.publish_job)
        self.assertIn("expected_snapshot_id: ${{ needs.prepare.outputs.snapshot_id }}", self.publish_job)

    def test_publish_outputs_are_exposed_by_the_action_and_job(self):
        self.assertIn(
            "value: ${{ steps.prepare.outputs.head_sha || steps.publish.outputs.head_sha }}",
            self.action,
        )
        self.assertIn(
            "value: ${{ steps.prepare.outputs.snapshot_id || steps.publish.outputs.snapshot_id }}",
            self.action,
        )
        self.assertIn("id: publish", self.publish_job)
        for name in ("comment_action", "comment_id", "head_sha", "snapshot_id"):
            self.assertIn(f"{name}: ${{{{ steps.publish.outputs.{name} }}}}", self.publish_job)

    def test_publish_step_passes_untrusted_values_through_the_environment(self):
        self.assertIn("GITHUB_TOKEN: ${{ inputs.github_token }}", self.publish_step)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.publish_step)
        for block in re.findall(r"run: \|\n((?:[ ]{8}.*\n?)+)", self.publish_step):
            self.assertNotIn("${{", block)
        self.assertIn("publish", re.search(r"case \"\$AI_REVIEW_STEP\" in\n\s+(\S+)", self.action).group(1))

    def test_no_merge_push_or_approve_anywhere_in_the_publisher(self):
        source = PUBLISH_SCRIPT.read_text("utf-8")
        for forbidden in ("/merge", "/reviews", "git push", "approve"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main()
