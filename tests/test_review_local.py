"""Phase 7 tests: the local CLI (ADR-0011).

Every external system is replaced: GitHub is an in-memory transport over a
local git remote, the Claude Code CLI is a fake executable, and the OpenAI
Responses API is an injected transport. No network, no real credential, no real
PR, and no reviewed code is ever executed.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402
from support import codex_payload, fake_transport, responses_envelope  # noqa: E402

import test_prepare_review as prepare_tests  # noqa: E402
import test_review_result as claude_tests  # noqa: E402

from lib import github as gh  # noqa: E402
from lib import models as models_mod  # noqa: E402

local = support.load_script("review_local", "review-local.py")
local.log = lambda message: None
for _module in (
    local.prepare_mod,
    local.run_review_mod,
    local.normalize_review_mod,
    local.run_codex_mod,
    local.normalize_codex_mod,
    local.finalize_mod,
    local.report_mod,
):
    _module.log = lambda message: None

# Fictional canaries. Never real credentials.
GITHUB_CANARY = "canary-local-github-read-token-0001"
CLAUDE_CANARY = "canary-local-claude-oauth-token-0001"
OPENAI_CANARY = "canary-local-openai-api-key-0001"
FOREIGN_CANARIES = {
    "GITHUB_TOKEN": "canary-foreign-github-token-0001",
    "GH_TOKEN": "canary-foreign-gh-token-0001",
    "AI_REVIEW_COMMENT_TOKEN": "canary-foreign-comment-token-0001",
    "AI_REVIEW_READ_TOKEN": "canary-foreign-read-token-0001",
}
ALL_CANARIES = (GITHUB_CANARY, CLAUDE_CANARY, OPENAI_CANARY, *FOREIGN_CANARIES.values())


def write_recording_cli(path: Path, record_path: Path, *, envelope_obj=None, exit_code: int = 0) -> Path:
    """The fake Claude CLI, recording outside its scratch cwd (which is deleted)."""
    template = claude_tests.FAKE_CLI_TEMPLATE.replace(
        'os.path.join(os.getcwd(), "record.json")', repr(str(record_path))
    )
    body = template.format(
        version=local.limits_mod.CLAUDE_CODE_VERSION,
        envelope=json.dumps(envelope_obj if envelope_obj is not None else claude_tests.envelope(claude_tests.model_payload())),
        sleep=0.0,
        exit_code=exit_code,
        pad="",
        stderr_extra="",
    )
    path.write_text(body, encoding="utf-8")
    path.chmod(0o700)
    return path


class LocalCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="ai-review-local-test-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.remote, shas = prepare_tests.build_standard_remote(self.tmp)
        self.github = prepare_tests.FakeGitHub(shas)
        self.record_path = self.tmp / "claude-record.json"
        self.codex_requests: list[dict] = []
        self.out = self.tmp / "out"
        self.summary = self.tmp / "summary.md"

    def run_local(self, *, cli_exit=0, envelope_obj=None, responses=None, output_dir=None, **overrides):
        cli = write_recording_cli(self.tmp / "fake-claude", self.record_path, envelope_obj=envelope_obj, exit_code=cli_exit)
        responses = responses if responses is not None else [(200, responses_envelope(codex_payload()))]
        kwargs = dict(
            repository=prepare_tests.REPOSITORY,
            pull_request=str(prepare_tests.PR_NUMBER),
            output_dir=output_dir or self.out,
            github_token=GITHUB_CANARY,
            claude_token=CLAUDE_CANARY,
            openai_key=OPENAI_CANARY,
            claude_bin=str(cli),
            github_transport=self.github,
            prepare_options={"runner_factory": prepare_tests._runner_factory(), "remote_url": self.remote.url},
            claude_options={"expected_sha256": claude_tests.sha256_file(cli)},
            codex_options={"transport": fake_transport(*responses, record=self.codex_requests), "sleep": lambda s: None},
            summary_path=str(self.summary),
        )
        kwargs.update(overrides)
        with mock.patch.dict(os.environ, FOREIGN_CANARIES):
            return local.review_local(**kwargs)

    def claude_record(self) -> dict:
        return json.loads(self.record_path.read_text("utf-8"))


class EndToEndTests(LocalCase):
    def test_successful_run_writes_only_the_final_result(self):
        document = self.run_local()
        self.assertTrue(document["publishable"])
        self.assertEqual(document["request"]["output_mode"], "summary_only")
        self.assertEqual(document["stages"]["claude"]["status"], "success")
        self.assertEqual(document["stages"]["codex"]["status"], "success")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["final-review.json", "review-summary.md"])
        self.assertEqual(self.out.stat().st_mode & 0o777, 0o700)
        saved = json.loads((self.out / "final-review.json").read_text("utf-8"))
        self.assertEqual(saved["snapshot"]["snapshot_id"], document["snapshot"]["snapshot_id"])
        self.assertTrue(self.summary.read_text("utf-8").strip())

    def test_scratch_directory_is_removed(self):
        self.run_local()
        scratch = Path(self.claude_record()["cwd"]).parents[1]
        self.assertTrue(scratch.name.startswith("ai-review-local-"))
        self.assertFalse(scratch.exists(), "bundle, raw responses and git scratch must not remain")

    def test_github_is_only_read(self):
        self.run_local()
        self.assertTrue(self.github.requests)
        self.assertEqual({method for method, _url, _headers in self.github.requests}, {"GET"})

    def test_claude_failure_skips_codex_and_is_not_publishable(self):
        document = self.run_local(cli_exit=1)
        self.assertFalse(document["publishable"])
        self.assertEqual(document["stages"]["claude"]["status"], "failed")
        self.assertEqual(document["stages"]["codex"]["status"], "skipped")
        self.assertEqual(self.codex_requests, [])
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["final-review.json", "review-summary.md"])
        self.assertIn("問題がないことを意味しない", document["review"]["summary"])

    def test_codex_failure_is_not_publishable(self):
        document = self.run_local(responses=[(400, {"error": {"message": "bad request"}})])
        self.assertFalse(document["publishable"])
        self.assertEqual(document["stages"]["claude"]["status"], "success")
        self.assertEqual(document["stages"]["codex"]["status"], "failed")

    def test_prepare_failure_writes_nothing(self):
        def not_found(method, url, headers, body=None):
            return 404, {}, b'{"message":"Not Found"}'

        with self.assertRaises(local.LocalError):
            self.run_local(github_transport=not_found)
        self.assertFalse(self.out.exists())
        self.assertFalse(self.record_path.exists(), "no model stage may run without a bundle")
        self.assertEqual(self.codex_requests, [])


class CredentialBoundaryTests(LocalCase):
    def test_each_token_reaches_only_its_stage(self):
        self.run_local()
        record = self.claude_record()
        claude_seen = json.dumps(record)
        self.assertEqual(record["env"].get("CLAUDE_CODE_OAUTH_TOKEN"), CLAUDE_CANARY)
        for canary in (GITHUB_CANARY, OPENAI_CANARY, *FOREIGN_CANARIES.values()):
            self.assertNotIn(canary, claude_seen)
        self.assertNotIn(str(Path.home()), record["env"].get("HOME", ""), "the user's HOME must not be used")

        self.assertEqual(len(self.codex_requests), 1)
        codex_seen = json.dumps(
            [{"url": r["url"], "headers": r["headers"], "body": (r["body"] or b"").decode("utf-8")} for r in self.codex_requests]
        )
        self.assertIn(OPENAI_CANARY, codex_seen)
        for canary in (GITHUB_CANARY, CLAUDE_CANARY, *FOREIGN_CANARIES.values()):
            self.assertNotIn(canary, codex_seen)

        github_seen = json.dumps([[m, u, h] for m, u, h in self.github.requests])
        self.assertIn(GITHUB_CANARY, github_seen)
        for canary in (CLAUDE_CANARY, OPENAI_CANARY, *FOREIGN_CANARIES.values()):
            self.assertNotIn(canary, github_seen)

    def test_no_token_is_written_to_the_output(self):
        self.run_local()
        for path in [*self.out.iterdir(), self.summary]:
            text = path.read_text("utf-8")
            for canary in ALL_CANARIES:
                self.assertNotIn(canary, text, path.name)

    def test_main_reads_tokens_from_the_environment_only(self):
        seen = {}

        def fake_review(**kwargs):
            seen.update(kwargs)
            return {"publishable": True}

        env = {
            "AI_REVIEW_GITHUB_TOKEN": GITHUB_CANARY,
            "CLAUDE_CODE_OAUTH_TOKEN": CLAUDE_CANARY,
            "OPENAI_API_KEY": OPENAI_CANARY,
            **FOREIGN_CANARIES,
        }
        argv = ["--repository", "acme/widgets", "--pull-request", "7", "--output-dir", str(self.out), "--claude-bin", "/bin/true"]
        with mock.patch.object(local, "review_local", fake_review):
            self.assertEqual(local.main(argv, env=env), local.EXIT_OK)
        self.assertEqual(seen["github_token"], GITHUB_CANARY)
        self.assertEqual(seen["claude_token"], CLAUDE_CANARY)
        self.assertEqual(seen["openai_key"], OPENAI_CANARY)
        self.assertNotIn(FOREIGN_CANARIES["GITHUB_TOKEN"], json.dumps(seen, default=str))

    def test_generic_github_token_is_not_used(self):
        # Only AI_REVIEW_GITHUB_TOKEN is a read token. GITHUB_TOKEN/GH_TOKEN are ignored.
        self.run_local(github_token=None)
        for _method, _url, headers in self.github.requests:
            self.assertNotIn("Authorization", headers)

    def test_missing_ai_credentials_stop_before_any_network_access(self):
        for missing in ("claude_token", "openai_key"):
            with self.subTest(missing=missing):
                self.github.requests.clear()
                with self.assertRaises(local.LocalError):
                    self.run_local(**{missing: None})
                self.assertEqual(self.github.requests, [])
                self.assertFalse(self.out.exists())

    def test_no_option_accepts_a_credential(self):
        parser_dests = set(vars(local.parse_args(["--repository", "a/b", "--pull-request", "1", "--output-dir", "x"])))
        for dest in parser_dests:
            for word in ("token", "key", "secret", "password"):
                self.assertNotIn(word, dest)
        for option in ("--github-token", "--token", "--openai-api-key", "--claude-token"):
            with self.subTest(option=option), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                local.parse_args(["--repository", "a/b", "--pull-request", "1", "--output-dir", "x", option, "v"])

    def test_main_exit_codes(self):
        argv = ["--repository", "acme/widgets", "--pull-request", "7", "--output-dir", str(self.out), "--claude-bin", "/bin/true"]
        for publishable, code in ((True, local.EXIT_OK), (False, local.EXIT_STOP)):
            with self.subTest(publishable=publishable), mock.patch.object(local, "review_local", return_value={"publishable": publishable}):
                self.assertEqual(local.main(argv, env={}), code)
        self.assertEqual(local.main(argv, env={}), local.EXIT_STOP, "missing AI credentials stop the run")


class NoWritePathTests(unittest.TestCase):
    def test_publisher_is_not_imported(self):
        code = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('review_local', {str(support.SCRIPTS / 'review-local.py')!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "sys.modules['review_local'] = module\n"
            "spec.loader.exec_module(module)\n"
            "print(sorted(name for name in sys.modules if 'publish' in name))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            timeout=60,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(proc.stdout.decode("utf-8").strip(), "[]")

    def test_source_has_no_write_call(self):
        source = (support.SCRIPTS / "review-local.py").read_text("utf-8")
        for forbidden in ("publish-review", "publish_review", "issue_comment", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY"):
            self.assertNotIn(forbidden, source, forbidden)
        self.assertIn("read_only_transport(", source)

    def test_read_only_transport_refuses_writes(self):
        calls = []

        def inner(method, url, headers, body=None):
            calls.append(method)
            return 200, {}, b"{}"

        transport = gh.read_only_transport(inner)
        self.assertEqual(transport("GET", "https://api.github.com/x", {}, None)[0], 200)
        for method in ("POST", "PATCH", "PUT", "DELETE"):
            with self.subTest(method=method), self.assertRaises(gh.GitHubError):
                transport(method, "https://api.github.com/x", {}, b"{}")
        with self.assertRaises(gh.GitHubError):
            transport("GET", "https://api.github.com/x", {}, b"{}")
        self.assertEqual(calls, ["GET"])

    def test_read_only_transport_blocks_client_writes(self):
        client = gh.GitHubClient("https://api.github.com", None, transport=gh.read_only_transport(lambda *a: (201, {}, b"{}")))
        writer = getattr(client, "create_" + "issue_comment")
        with self.assertRaises(gh.GitHubError):
            writer("acme", "widgets", 7, "body")


class InputTests(LocalCase):
    def test_non_empty_or_non_directory_output_is_refused_before_network(self):
        occupied = self.tmp / "occupied"
        occupied.mkdir()
        (occupied / "old.json").write_text("{}", encoding="utf-8")
        a_file = self.tmp / "a-file"
        a_file.write_text("x", encoding="utf-8")
        for target in (occupied, a_file):
            with self.subTest(target=target.name), self.assertRaises(local.LocalError):
                self.run_local(output_dir=target)
        self.assertEqual(self.github.requests, [])
        self.assertEqual((occupied / "old.json").read_text("utf-8"), "{}")

    def test_existing_empty_directory_is_accepted(self):
        self.out.mkdir()
        self.out.chmod(0o700)
        self.assertTrue(self.run_local()["publishable"])

    def test_permissive_empty_directory_is_refused_before_network(self):
        for mode in (0o755, 0o770):
            with self.subTest(mode=oct(mode)):
                self.out.mkdir(exist_ok=True)
                self.out.chmod(mode)
                with self.assertRaises(local.LocalError):
                    self.run_local()
                self.assertEqual(self.github.requests, [])
                self.assertEqual(self.out.stat().st_mode & 0o777, mode)

    def test_model_outside_the_allowlist_is_refused_before_network(self):
        with self.assertRaises(models_mod.ModelNotAllowed):
            self.run_local(claude_model="claude-unknown")
        with self.assertRaises(models_mod.ModelNotAllowed):
            self.run_local(codex_model="gpt-unknown")
        self.assertEqual(self.github.requests, [])

    def test_pull_request_url_must_match_the_repository(self):
        with self.assertRaises(local.validate_mod.RequestError):
            self.run_local(pull_request="https://github.com/other/repo/pull/7")
        self.run_local(pull_request=f"https://github.com/{prepare_tests.REPOSITORY}/pull/{prepare_tests.PR_NUMBER}")

    def test_bare_cli_name_is_resolved_on_path(self):
        self.assertEqual(local.resolve_claude_bin("/opt/claude"), "/opt/claude")
        with mock.patch.object(local.shutil, "which", return_value=None), self.assertRaises(local.LocalError):
            local.resolve_claude_bin("claude")
        with mock.patch.object(local.shutil, "which", return_value="/usr/local/bin/claude"):
            self.assertEqual(local.resolve_claude_bin("claude"), "/usr/local/bin/claude")


if __name__ == "__main__":
    unittest.main()
