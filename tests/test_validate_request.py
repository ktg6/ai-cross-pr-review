"""Tests for the request validator and the model/effort/output-mode allowlists.

validate_request is the single entry point for operator-supplied values. Every
later job reads its outputs, so a value that is not on the allowlist here can
never reach an argv, a prompt, or a GitHub call.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402

from lib import github as gh  # noqa: E402
from lib import models as models_mod  # noqa: E402

validate = support.load_script("validate_request", "validate-request.py")
validate.log = lambda message: None

SERVER = "https://github.com"


def build(**overrides) -> dict:
    args = dict(
        repository="acme/widgets",
        pull_request="7",
        output_mode="summary_only",
        claude_model="claude-opus-5",
        codex_model="gpt-5.6-sol",
        claude_effort="high",
        codex_effort="high",
        policy_path=".github/ai-review.md",
    )
    args.update(overrides)
    return validate.build_request(**args)


class DefaultsTests(unittest.TestCase):
    def test_summary_only_is_the_safe_default(self):
        self.assertEqual(models_mod.DEFAULT_OUTPUT_MODE, "summary_only")
        args = validate.parse_args(["--repository", "acme/widgets", "--pull-request", "7"])
        self.assertEqual(args.output_mode, "summary_only")
        self.assertEqual(args.claude_model, models_mod.DEFAULT_CLAUDE_MODEL)
        self.assertEqual(args.codex_model, models_mod.DEFAULT_CODEX_MODEL)

    def test_defaults_are_themselves_on_the_allowlists(self):
        self.assertIn(models_mod.DEFAULT_CLAUDE_MODEL, models_mod.CLAUDE_MODELS)
        self.assertIn(models_mod.DEFAULT_CODEX_MODEL, models_mod.CODEX_MODELS)
        self.assertIn(models_mod.DEFAULT_CLAUDE_EFFORT, models_mod.CLAUDE_EFFORTS)
        self.assertIn(models_mod.DEFAULT_CODEX_EFFORT, models_mod.CODEX_EFFORTS)
        self.assertIn(models_mod.DEFAULT_OUTPUT_MODE, models_mod.OUTPUT_MODES)

    def test_a_valid_request_round_trips(self):
        request = build()
        self.assertEqual(request["repository"], "acme/widgets")
        self.assertEqual(request["pr_number"], 7)
        self.assertEqual(request["output_mode"], "summary_only")


class PullRequestTests(unittest.TestCase):
    def test_number_and_url_forms_are_equivalent(self):
        for value in ("7", " 7 ", "https://github.com/acme/widgets/pull/7", "https://github.com/acme/widgets/pull/7/files", "https://GitHub.com/Acme/Widgets/pull/7"):
            with self.subTest(value=value):
                self.assertEqual(build(pull_request=value)["pr_number"], 7)

    def test_bad_numbers_stop(self):
        for value in ("", "0", "-1", "07", "1.5", "abc", "7; rm -rf /", "2147483648", "١٢٣", "7\n8"):
            with self.subTest(value=value), self.assertRaises((validate.RequestError, gh.ValidationError)):
                build(pull_request=value)

    def test_url_for_another_repository_stops(self):
        with self.assertRaisesRegex(validate.RequestError, "does not match the requested repository"):
            build(pull_request="https://github.com/evil/other/pull/7")

    def test_encoded_repository_or_pr_number_stops(self):
        for url in (
            "https://github.com/acme%2Fother/widgets/pull/7",
            "https://github.com/acme/widgets%2Fother/pull/7",
            "https://github.com/acme/widgets/pull/%37",
        ):
            with self.subTest(url=url), self.assertRaises((validate.RequestError, gh.ValidationError)):
                build(pull_request=url)

    def test_url_on_another_host_or_scheme_stops(self):
        for value in (
            "https://evil.example/acme/widgets/pull/7",
            "http://github.com/acme/widgets/pull/7",
            "https://github.com.evil.example/acme/widgets/pull/7",
            "https://user@github.com/acme/widgets/pull/7",
            "https://github.com/acme/widgets/issues/7",
            "https://github.com/acme/widgets/pull/x",
            "https://github.com/acme/widgets",
            "https://github.com/acme/widgets/pull/7?x=1#frag" + "\x00",
        ):
            with self.subTest(value=value), self.assertRaises((validate.RequestError, gh.ValidationError)):
                build(pull_request=value)

    def test_url_with_a_query_is_reduced_to_its_path(self):
        self.assertEqual(build(pull_request="https://github.com/acme/widgets/pull/7?diff=split")["pr_number"], 7)

    def test_non_string_input_stops(self):
        for value in (None, 7, ["7"]):
            with self.subTest(value=value), self.assertRaises(validate.RequestError):
                validate.parse_pull_request(value, "acme/widgets", SERVER)


class RepositoryTests(unittest.TestCase):
    def test_bad_repositories_stop(self):
        for value in ("", "acme", "acme/", "/widgets", "a/b/c", "acme/widgets.git", "../etc/passwd", "acme/wid gets", "acme/widgets;x", "-a/b", "acme/.."):
            with self.subTest(value=value), self.assertRaises(gh.ValidationError):
                build(repository=value)

    def test_repository_is_canonicalized_from_owner_and_name(self):
        self.assertEqual(build(repository="Acme/Widgets")["repository"], "Acme/Widgets")


class AllowlistBypassTests(unittest.TestCase):
    """Anything that is not an exact allowlist entry must be refused."""

    NEAR_MISSES = (
        "", " ", "claude-opus-5 ", " claude-opus-5", "CLAUDE-OPUS-5", "claude-opus-5\n", "claude-opus-5;id",
        "claude-opus-4-1", "claude-opus-5-20260401", "claude-haiku-4-5", "claude", "opus", "*", "../claude-opus-5",
        "--dangerously-skip-permissions", "-p", "gpt-4", "gpt-5.6-sol", "o3", None, 5, ["claude-opus-5"],
    )

    def test_claude_model_bypass_attempts_are_refused(self):
        for value in self.NEAR_MISSES:
            with self.subTest(value=value), self.assertRaises(models_mod.ModelNotAllowed):
                build(claude_model=value)

    def test_codex_model_bypass_attempts_are_refused(self):
        attempts = ("", " ", "gpt-5.6-sol ", "GPT-5.6-SOL", "gpt-5.6", "gpt-5.6-sol;id", "gpt-4", "gpt-5.5", "o3", "claude-opus-5", "--flag", None, 5)
        for value in attempts:
            with self.subTest(value=value), self.assertRaises(models_mod.ModelNotAllowed):
                build(codex_model=value)

    def test_effort_bypass_attempts_are_refused(self):
        for value in ("", "none", "minimal", "ultra", "High", "high ", "max\n", None, 3):
            with self.subTest(value=value):
                with self.assertRaises(models_mod.ModelNotAllowed):
                    build(claude_effort=value)
                with self.assertRaises(models_mod.ModelNotAllowed):
                    build(codex_effort=value)

    def test_output_mode_bypass_attempts_are_refused(self):
        for value in ("", "comment", "PR_COMMENT", "pr_comment ", "summary_only,pr_comment", "always", None, True):
            with self.subTest(value=value), self.assertRaises(models_mod.ModelNotAllowed):
                build(output_mode=value)

    def test_every_allowlisted_value_is_accepted(self):
        for model in models_mod.CLAUDE_MODELS:
            self.assertEqual(build(claude_model=model)["claude_model"], model)
        for model in models_mod.CODEX_MODELS:
            self.assertEqual(build(codex_model=model)["codex_model"], model)
        for effort in models_mod.CLAUDE_EFFORTS:
            self.assertEqual(build(claude_effort=effort)["claude_effort"], effort)
        for effort in models_mod.CODEX_EFFORTS:
            self.assertEqual(build(codex_effort=effort)["codex_effort"], effort)
        for mode in models_mod.OUTPUT_MODES:
            self.assertEqual(build(output_mode=mode)["output_mode"], mode)

    def test_allowlist_contents_are_exactly_the_verified_models(self):
        self.assertEqual(models_mod.CLAUDE_MODELS, ("claude-opus-5", "claude-sonnet-5"))
        # Haiku 4.5 takes no effort setting, so it cannot be offered with one.
        self.assertNotIn("claude-haiku-4-5", models_mod.CLAUDE_MODELS)
        self.assertEqual(
            models_mod.CODEX_MODELS,
            ("gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"),
        )
        self.assertNotIn("none", models_mod.CODEX_EFFORTS)

    def test_the_workflow_choices_match_the_python_allowlists(self):
        workflow = (support.ROOT / ".github" / "workflows" / "cross-review.yml").read_text("utf-8")
        for name, allowed in (
            ("claude_model", models_mod.CLAUDE_MODELS),
            ("codex_model", models_mod.CODEX_MODELS),
            ("claude_effort", models_mod.CLAUDE_EFFORTS),
            ("codex_effort", models_mod.CODEX_EFFORTS),
            ("output_mode", models_mod.OUTPUT_MODES),
        ):
            body = workflow.split(f"\n      {name}:\n", 1)[1]
            options_block = body.split("        options:\n", 1)[1]
            options = []
            for line in options_block.splitlines():
                if line.startswith("          - "):
                    options.append(line[len("          - "):].strip())
                else:
                    break
            with self.subTest(input=name):
                self.assertEqual(sorted(options), sorted(allowed))
                self.assertIn(f"default: {getattr(models_mod, 'DEFAULT_' + name.upper())}", body.split("options:", 1)[0])


class PolicyPathTests(unittest.TestCase):
    def test_default_is_accepted(self):
        self.assertEqual(build()["policy_path"], ".github/ai-review.md")
        self.assertEqual(build(policy_path="docs/review/policy.md")["policy_path"], "docs/review/policy.md")

    def test_unsafe_paths_stop(self):
        for value in ("", "/etc/passwd", "../secrets.md", "a/../../b", "a//b", ".env", "config/.env", "keys/id_rsa", "x/.aws/credentials", "a\x00b", "a/" + "b" * 300):
            with self.subTest(value=value), self.assertRaises(validate.RequestError):
                build(policy_path=value)


class OutputTests(unittest.TestCase):
    def test_outputs_are_written_for_later_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh-output"
            args = validate.parse_args(
                ["--repository", "acme/widgets", "--pull-request", "https://github.com/acme/widgets/pull/7", "--output-mode", "pr_comment", "--codex-model", "gpt-5.6-terra"]
            )
            validate.validate_request(args, run_env={"GITHUB_OUTPUT": str(out)})
            written = dict(line.split("=", 1) for line in out.read_text("utf-8").splitlines())
        self.assertEqual(written["repository"], "acme/widgets")
        self.assertEqual(written["pr_number"], "7")
        self.assertEqual(written["output_mode"], "pr_comment")
        self.assertEqual(written["codex_model"], "gpt-5.6-terra")
        self.assertEqual(set(written), {"repository", "pr_number", "output_mode", "claude_model", "codex_model", "claude_effort", "codex_effort", "policy_path"})

    def test_request_json_can_be_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "request.json"
            args = validate.parse_args(["--repository", "acme/widgets", "--pull-request", "7", "--output-file", str(target)])
            validate.validate_request(args, run_env={})
            self.assertEqual(json.loads(target.read_text("utf-8"))["pr_number"], 7)

    def test_unsafe_output_values_are_never_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh-output"
            with self.assertRaises(validate.RequestError):
                validate.write_github_output(str(out), {"a": "x\ninjected=1"})
            with self.assertRaises(validate.RequestError):
                validate.write_github_output(str(out), {"bad key": "x"})
            self.assertFalse(out.exists())

    def test_rejected_request_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "gh-output"
            args = validate.parse_args(["--repository", "acme/widgets", "--pull-request", "7", "--claude-model", "gpt-4"])
            with self.assertRaises(models_mod.ModelNotAllowed):
                validate.validate_request(args, run_env={"GITHUB_OUTPUT": str(out)})
            self.assertFalse(out.exists())

    def test_main_maps_failures_to_the_stop_code(self):
        code = validate.main(["--repository", "acme/widgets", "--pull-request", "7", "--codex-model", "gpt-4"])
        self.assertEqual(code, validate.EXIT_STOP)
        code = validate.main(["--repository", "not-a-repo", "--pull-request", "7"])
        self.assertEqual(code, validate.EXIT_STOP)
        self.assertEqual(validate.main(["--repository", "acme/widgets", "--pull-request", "7"]), validate.EXIT_OK)

    def test_validator_touches_neither_network_nor_credentials(self):
        source = (support.SCRIPTS / "validate-request.py").read_text("utf-8")
        for needle in ("urlopen", "GITHUB_TOKEN", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "subprocess"):
            self.assertNotIn(needle, source)


if __name__ == "__main__":
    unittest.main()
