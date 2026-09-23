"""Static policy tests for the central execution workflow and its runtime action.

These read the YAML as text (the repository's YAML style is constrained) and pin
the security properties the ADRs rely on: one manual entry point, an allowlist
gate in front of every other job, credentials scoped to exactly one job each,
no write permission anywhere, and no checkout of the reviewed repository.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import support  # noqa: E402  (also puts scripts/ on sys.path)
from lib import limits as limits_mod  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_DIR = ROOT / ".github" / "workflows"
CROSS_WORKFLOW = WORKFLOW_DIR / "cross-review.yml"
CI_WORKFLOW = WORKFLOW_DIR / "ci.yml"
ACTION_FILE = ROOT / "actions" / "review-runtime" / "action.yml"
ADR_DIR = ROOT / "docs" / "adr"
WORKFLOW_PATHS = tuple(WORKFLOW_DIR.glob("*.yml"))

FULL_SHA_REF = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")

JOB_ORDER = ("validate_request", "prepare", "claude_review", "codex_review", "finalize", "report", "comment")

INPUTS = (
    "target_repository",
    "pull_request",
    "output_mode",
    "claude_model",
    "codex_model",
    "claude_effort",
    "codex_effort",
    "policy_path",
)

SECRETS = {
    "AI_REVIEW_READ_TOKEN": "prepare",
    "CLAUDE_CODE_OAUTH_TOKEN": "claude_review",
    "OPENAI_API_KEY": "codex_review",
    "AI_REVIEW_COMMENT_TOKEN": "comment",
}


def jobs_of(workflow: str) -> dict[str, str]:
    """Return each top-level job's body."""
    marker = "\njobs:\n"
    if marker not in workflow:
        raise AssertionError("workflow has no top-level jobs mapping")
    body = workflow.split(marker, 1)[1]
    names = re.findall(r"(?m)^  ([a-z_]+):$", body)
    sections: dict[str, str] = {}
    for index, name in enumerate(names):
        chunk = body.split(f"\n  {name}:\n", 1)[1] if index else body.split(f"  {name}:\n", 1)[1]
        if index + 1 < len(names):
            chunk = chunk.split(f"\n  {names[index + 1]}:\n", 1)[0]
        sections[name] = chunk
    return sections


def strip_comments(text: str) -> str:
    """Drop full-line YAML comments so prose cannot satisfy or trip a check."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def input_block(trigger: str, name: str) -> str:
    """Return one workflow_dispatch input's body."""
    match = re.search(rf"(?ms)^      {name}:\n(.*?)(?=^      [a-z_]+:\n|\Z)", trigger)
    if match is None:
        raise AssertionError(f"no workflow input {name!r}")
    return match.group(1)


def trigger_names(workflow: str) -> list[str]:
    """Event names under the top-level ``on:`` key."""
    block = workflow.split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    return re.findall(r"(?m)^  ([a-z_]+):$", block)


def step_body(action: str, step_name: str) -> str:
    """Return one composite-action step's text by its ``name:``."""
    parts = action.split(f"    - name: {step_name}\n", 1)
    if len(parts) != 2:
        raise AssertionError(f"action has no step {step_name!r}")
    return re.split(r"\n    (?:# [^\n]*\n    )*- name: ", parts[1], maxsplit=1)[0]


class CentralWorkflowTriggerTests(unittest.TestCase):
    def setUp(self):
        self.workflow = CROSS_WORKFLOW.read_text(encoding="utf-8")
        self.trigger = self.workflow.split("\npermissions:", 1)[0]

    def test_manual_dispatch_is_the_only_entry_point(self):
        self.assertEqual(trigger_names(self.workflow), ["workflow_dispatch"])

    def test_inputs_are_exactly_the_documented_set(self):
        inputs = re.findall(r"(?m)^      ([a-z][a-z0-9_]*):$", self.trigger)
        self.assertEqual(inputs, list(INPUTS))

    def test_summary_only_is_the_default_output_mode(self):
        block = input_block(self.trigger, "output_mode")
        self.assertIn("type: choice", block)
        self.assertIn("default: summary_only", block)

    def test_model_and_effort_inputs_are_choices_never_free_text(self):
        for name in ("output_mode", "claude_model", "codex_model", "claude_effort", "codex_effort"):
            body = input_block(self.trigger, name)
            self.assertIn("type: choice", body, name)
            self.assertIn("options:", body, name)
        for name in ("target_repository", "pull_request"):
            self.assertIn("type: string", input_block(self.trigger, name), name)

    def test_no_reusable_workflow_is_offered_or_called(self):
        self.assertNotRegex(self.workflow, r"(?m)^    uses: \S+/\.github/workflows/")
        self.assertFalse((WORKFLOW_DIR / "claude-review.yml").exists())

    def test_the_old_consumer_wrapper_is_gone(self):
        self.assertFalse((WORKFLOW_DIR / "ai-review.yml").exists())

    def test_runs_for_the_same_target_are_serialized_without_cancellation(self):
        pre_jobs = self.workflow.split("\njobs:\n", 1)[0]
        self.assertIn("concurrency:\n  group: ai-cross-review-${{ inputs.target_repository }}-${{ inputs.pull_request }}", pre_jobs)
        self.assertIn("  cancel-in-progress: false", pre_jobs)

    def test_top_level_permissions_are_empty(self):
        pre_jobs = self.workflow.split("\njobs:\n", 1)[0]
        self.assertIn("\npermissions: {}\n", pre_jobs)


class JobGraphTests(unittest.TestCase):
    def setUp(self):
        self.workflow = CROSS_WORKFLOW.read_text(encoding="utf-8")
        self.jobs = jobs_of(self.workflow)

    def test_jobs_are_exactly_the_documented_seven_in_order(self):
        self.assertEqual(tuple(self.jobs), JOB_ORDER)

    def test_every_job_waits_for_the_allowlist_gate(self):
        for name in JOB_ORDER[1:]:
            with self.subTest(job=name):
                needs = re.search(r"(?m)^    needs: (.+)$", self.jobs[name])
                self.assertIsNotNone(needs, name)
                self.assertIn("validate_request", needs.group(1))

    def test_later_jobs_read_the_validated_outputs_not_the_raw_inputs(self):
        for name in JOB_ORDER[1:]:
            with self.subTest(job=name):
                self.assertNotIn("${{ inputs.", self.jobs[name])
        # Only the gate itself reads workflow inputs.
        self.assertIn("${{ inputs.target_repository }}", self.jobs["validate_request"])

    def test_dependencies_form_the_documented_pipeline(self):
        def needs(name):
            match = re.search(r"(?m)^    needs: \[?([^\]\n]+)\]?$", self.jobs[name])
            return [part.strip() for part in match.group(1).split(",")] if match else []

        self.assertEqual(needs("prepare"), ["validate_request"])
        self.assertEqual(needs("claude_review"), ["validate_request", "prepare"])
        self.assertEqual(needs("codex_review"), ["validate_request", "prepare", "claude_review"])
        self.assertEqual(needs("finalize"), ["validate_request", "prepare", "claude_review", "codex_review"])
        self.assertEqual(needs("report"), ["validate_request", "prepare", "claude_review", "codex_review", "finalize"])
        self.assertEqual(needs("comment"), ["validate_request", "prepare", "finalize", "report"])

    def test_codex_never_runs_without_the_primary_review(self):
        self.assertIn("claude_review", re.search(r"(?m)^    needs: (.+)$", self.jobs["codex_review"]).group(1))
        self.assertNotIn("if:", self.jobs["codex_review"].split("steps:", 1)[0])

    def test_finalize_and_report_run_even_after_a_failure_but_only_after_the_gate(self):
        for name in ("finalize", "report"):
            head = self.jobs[name].split("steps:", 1)[0]
            self.assertIn("always()", head, name)
            self.assertIn("needs.validate_request.result == 'success'", head, name)
        self.assertIn("needs.prepare.result == 'success'", self.jobs["finalize"].split("steps:", 1)[0])

    def test_failures_are_not_masked(self):
        self.assertNotIn("continue-on-error", self.workflow)
        self.assertEqual(self.workflow.count("if-no-files-found: error"), self.workflow.count("upload-artifact@"))

    def test_comment_job_is_gated_on_mode_and_publishability(self):
        head = self.jobs["comment"].split("steps:", 1)[0]
        match = re.search(r"(?m)^    if: (.+)$", head)
        self.assertIsNotNone(match)
        condition = match.group(1)
        self.assertIn("needs.validate_request.outputs.output_mode == 'pr_comment'", condition)
        self.assertIn("needs.finalize.outputs.publishable == 'true'", condition)
        self.assertNotIn("always()", condition)
        self.assertNotIn("||", condition)

    def test_no_job_but_comment_is_conditioned_on_the_output_mode(self):
        for name in JOB_ORDER:
            if name != "comment":
                self.assertNotIn("output_mode ==", self.jobs[name], name)

    def test_summary_only_leaves_the_report_path_without_any_write_step(self):
        for name in ("finalize", "report"):
            self.assertNotIn("step: publish", self.jobs[name])
        self.assertEqual(self.workflow.count("step: publish"), 1)
        self.assertIn("step: publish", self.jobs["comment"])

    def test_job_outputs_needed_downstream_are_declared(self):
        for output in ("repository", "pr_number", "output_mode", "claude_model", "codex_model",
                       "claude_effort", "codex_effort", "policy_path"):
            self.assertIn(f"      {output}: ${{{{ steps.validate.outputs.{output} }}}}", self.jobs["validate_request"])
        for output in ("snapshot_id", "head_sha", "base_sha", "merge_base_sha", "policy_source", "policy_present", "diff_sha256"):
            self.assertIn(f"      {output}: ${{{{ steps.prepare.outputs.{output} }}}}", self.jobs["prepare"])
        for output in ("publishable", "snapshot_id", "claude_status", "codex_status"):
            self.assertIn(f"      {output}: ${{{{ steps.finalize.outputs.{output} }}}}", self.jobs["finalize"])


class PermissionAndSecretTests(unittest.TestCase):
    def setUp(self):
        self.workflow = CROSS_WORKFLOW.read_text(encoding="utf-8")
        self.jobs = jobs_of(self.workflow)

    def test_no_write_permission_exists_anywhere(self):
        self.assertNotRegex(self.workflow, r"(?m)^\s+[a-z-]+: write$")
        for forbidden in ("contents: write", "pull-requests: write", "issues: write", "id-token: write", "actions: write", "checks: write"):
            self.assertNotIn(forbidden, self.workflow)

    def test_every_job_has_exactly_read_access_to_this_repository_and_nothing_else(self):
        for name, body in self.jobs.items():
            with self.subTest(job=name):
                match = re.search(r"(?m)^    permissions:\n((?:      [a-z-]+: \w+\n)+)", body)
                self.assertIsNotNone(match, name)
                self.assertEqual(re.findall(r"(?m)^      ([a-z-]+): (\w+)$", match.group(1)), [("contents", "read")])

    def test_each_secret_is_referenced_exactly_once_and_only_by_its_own_job(self):
        self.assertEqual(sorted(re.findall(r"\$\{\{ secrets\.([A-Z_]+) \}\}", self.workflow)), sorted(SECRETS))
        for secret, owner in SECRETS.items():
            for name, body in self.jobs.items():
                expected = 1 if name == owner else 0
                with self.subTest(secret=secret, job=name):
                    self.assertEqual(body.count(f"secrets.{secret}"), expected)

    def test_secrets_are_passed_only_as_action_inputs_never_via_env_or_run(self):
        for match in re.finditer(r"secrets\.[A-Z_]+", self.workflow):
            line = self.workflow[: match.start()].rsplit("\n", 1)[-1]
            self.assertRegex(line, r"^\s+(github_token|claude_code_oauth_token|openai_api_key): ")
        self.assertNotIn("secrets: inherit", self.workflow)
        self.assertNotIn("toJSON(secrets", self.workflow)
        self.assertNotRegex(self.workflow, r"(?m)^\s+env:")

    def test_ai_jobs_receive_no_github_credential_input(self):
        for name in ("claude_review", "codex_review"):
            body = strip_comments(self.jobs[name])
            with self.subTest(job=name):
                self.assertNotIn("github_token", body)
                self.assertNotIn("github.token", body)
                self.assertNotIn("GITHUB_TOKEN", body)
                self.assertNotIn("AI_REVIEW_READ_TOKEN", body)
                self.assertNotIn("AI_REVIEW_COMMENT_TOKEN", body)

    def test_ai_provider_secrets_never_reach_the_read_or_comment_jobs(self):
        for name in ("prepare", "comment", "finalize", "report", "validate_request"):
            body = self.jobs[name]
            with self.subTest(job=name):
                self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", body)
                self.assertNotIn("OPENAI_API_KEY", body)
                self.assertNotIn("claude_code_oauth_token", body)
                self.assertNotIn("openai_api_key", body)

    def test_claude_and_codex_secrets_are_kept_apart(self):
        self.assertNotIn("OPENAI_API_KEY", self.jobs["claude_review"])
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.jobs["codex_review"])

    def test_read_token_and_comment_token_are_separate_secrets(self):
        self.assertEqual(SECRETS["AI_REVIEW_READ_TOKEN"], "prepare")
        self.assertEqual(SECRETS["AI_REVIEW_COMMENT_TOKEN"], "comment")
        self.assertIn("github_token: ${{ secrets.AI_REVIEW_READ_TOKEN }}", self.jobs["prepare"])
        self.assertIn("github_token: ${{ secrets.AI_REVIEW_COMMENT_TOKEN }}", self.jobs["comment"])

    def test_the_central_repositorys_own_token_is_never_handed_to_a_step(self):
        code = strip_comments(self.workflow)
        self.assertNotIn("github.token", code)
        self.assertNotIn("secrets.GITHUB_TOKEN", code)

    def test_secret_bearing_jobs_are_the_only_ones_holding_provider_input_names(self):
        for name, body in self.jobs.items():
            if name not in ("claude_review", "codex_review", "prepare", "comment"):
                self.assertNotIn("secrets.", body, name)

    def test_credential_expiry_dates_are_variables_read_only_by_validate_request(self):
        # ADR-0010: expiry dates are non-secret repository variables. They reach
        # the operational check in validate_request and nothing else.
        expected = {
            "read_token_expires_on": "AI_REVIEW_READ_TOKEN_EXPIRES_ON",
            "comment_token_expires_on": "AI_REVIEW_COMMENT_TOKEN_EXPIRES_ON",
            "claude_token_expires_on": "AI_REVIEW_CLAUDE_TOKEN_EXPIRES_ON",
            "openai_key_expires_on": "AI_REVIEW_OPENAI_KEY_EXPIRES_ON",
        }
        found = re.findall(r"(?m)^\s+([a-z_]+): \$\{\{ vars\.([A-Z_]+) \}\}$", self.workflow)
        self.assertEqual(dict(found), expected)
        self.assertEqual(len(found), len(expected))
        for name, body in self.jobs.items():
            if name != "validate_request":
                self.assertNotIn("vars.", body, name)
        for variable in expected.values():
            self.assertNotIn(variable, SECRETS)


class NoTargetCodeTests(unittest.TestCase):
    def setUp(self):
        self.workflow = CROSS_WORKFLOW.read_text(encoding="utf-8")

    def test_only_the_central_repository_is_ever_checked_out(self):
        checkouts = re.findall(r"uses: actions/checkout@[^\n]*\n        with:\n((?:          .*\n)+)", self.workflow)
        self.assertEqual(len(checkouts), self.workflow.count("actions/checkout@"))
        self.assertGreaterEqual(len(checkouts), 7)
        for block in checkouts:
            self.assertEqual(block.strip().splitlines(), ["persist-credentials: false"])
        for forbidden in ("repository:", "ref:", "token:", "submodules", "fetch-depth"):
            for block in checkouts:
                self.assertNotIn(forbidden, block)

    def test_the_reviewed_repository_is_not_the_workflows_own_repository(self):
        # The target comes only from the validated request; the running repo is never the target.
        self.assertNotIn("github.repository", self.workflow)
        self.assertNotIn("github.event.pull_request", self.workflow)
        self.assertNotIn("github.head_ref", self.workflow)

    def test_no_shell_steps_run_in_the_workflow(self):
        self.assertNotIn("run:", self.workflow.split("\njobs:\n", 1)[1])

    def test_every_local_action_is_the_runtime_action(self):
        uses = re.findall(r"uses: (\S+)", self.workflow)
        for ref in uses:
            if ref.startswith("./"):
                self.assertEqual(ref, "./actions/review-runtime")

    def test_third_party_actions_are_pinned_to_full_sha(self):
        uses = [ref for ref in re.findall(r"uses: (\S+)", self.workflow) if not ref.startswith("./")]
        self.assertTrue(uses)
        for ref in uses:
            self.assertRegex(ref, FULL_SHA_REF, ref)

    def test_artifacts_are_short_lived_and_carry_no_raw_provider_output(self):
        for match in re.finditer(r"retention-days: (\d+)", self.workflow):
            self.assertLessEqual(int(match.group(1)), 7)
        paths = re.findall(r"(?m)^          path: (.+)$", self.workflow)
        for path in paths:
            for raw in ("claude-raw", "codex-raw", "invocation", "ai-review-git", "ai-review-claude/", "ai-review-codex/"):
                self.assertNotIn(raw, path)
        uploaded = "\n".join(paths)
        self.assertIn("review-result.json", uploaded)
        self.assertIn("codex-result.json", uploaded)


class RuntimeActionTests(unittest.TestCase):
    def setUp(self):
        self.action = ACTION_FILE.read_text(encoding="utf-8")

    def test_supported_steps(self):
        match = re.search(r'case "\$AI_REVIEW_STEP" in\n\s+(\S+) ;;', self.action)
        self.assertIsNotNone(match)
        self.assertEqual(sorted(match.group(1).rstrip(")").split("|")), sorted(["validate", "prepare", "review", "codex", "finalize", "report", "publish"]))

    def test_untrusted_values_reach_scripts_only_through_the_environment(self):
        for block in re.findall(r"run: \|\n((?:[ ]{8}.*\n?)+)", self.action):
            self.assertNotIn("${{", block)

    def test_each_step_sees_only_its_own_credential(self):
        expectations = {
            "Validate request": set(),
            "Check operational health": set(),
            "Prepare review bundle": {"GITHUB_TOKEN"},
            "Run read-only primary review": {"CLAUDE_CODE_OAUTH_TOKEN"},
            "Run verification review": {"OPENAI_API_KEY"},
            "Finalize review": set(),
            "Report to job summary": set(),
            "Publish review comment": {"GITHUB_TOKEN"},
        }
        every = {"GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY"}
        for step, allowed in expectations.items():
            body = step_body(self.action, step)
            for credential in every:
                with self.subTest(step=step, credential=credential):
                    if credential in allowed:
                        self.assertRegex(body, rf"(?m)^        {credential}: \$\{{\{{ inputs\.")
                    else:
                        self.assertNotIn(credential, body)

    def test_the_two_ai_steps_do_not_receive_a_github_token(self):
        for step in ("Run read-only primary review", "Run verification review", "Install pinned Claude Code CLI"):
            body = step_body(self.action, step)
            self.assertNotIn("github_token", body)
            self.assertNotIn("GITHUB_TOKEN", body)

    def test_operational_check_reads_validated_models_and_holds_no_credential(self):
        body = step_body(self.action, "Check operational health")
        self.assertIn("if: ${{ inputs.step == 'validate' }}", body)
        self.assertIn("continue-on-error: true", body)
        self.assertEqual(self.action.count("continue-on-error: true"), 1)
        self.assertIn("${{ steps.validate.outputs.claude_model }}", body)
        self.assertIn("${{ steps.validate.outputs.codex_model }}", body)
        self.assertNotIn("inputs.claude_model", body)
        self.assertNotIn("inputs.codex_model", body)
        for name in ("github_token", "claude_code_oauth_token", "openai_api_key"):
            self.assertNotIn(name, body)

    def test_publish_step_never_receives_an_ai_provider_secret(self):
        body = step_body(self.action, "Publish review comment")
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "openai_api_key", "claude_code_oauth_token"):
            self.assertNotIn(name, body)

    def test_codex_step_uses_only_central_files_for_policy_and_schema(self):
        body = step_body(self.action, "Run verification review")
        self.assertIn('--prompt-file "$GITHUB_ACTION_PATH/../../prompts/codex-verify.md"', body)
        self.assertIn('--schema-file "$GITHUB_ACTION_PATH/../../schemas/codex-review.schema.json"', body)

    def test_default_policy_file_comes_from_the_central_repository(self):
        body = step_body(self.action, "Prepare review bundle")
        self.assertIn('--default-policy-file "$GITHUB_ACTION_PATH/../../policies/default-review-policy.md"', body)

    def test_claude_version_pin_is_consistent(self):
        self.assertIn(f'AI_REVIEW_CLAUDE_VERSION: "{limits_mod.CLAUDE_CODE_VERSION}"', self.action)
        self.assertNotIn("install.sh | bash", self.action)
        self.assertIn("--proto '=https'", self.action)
        self.assertRegex(self.action, r'AI_REVIEW_INSTALLER_SHA256: "[0-9a-f]{64}"')
        self.assertNotIn("rm -rf", self.action)

    def test_api_url_is_an_input_not_a_model_decision(self):
        self.assertIn("api_url:", self.action)
        self.assertIn("default: ${{ github.api_url }}", self.action)

    def test_outputs_cover_what_the_workflow_reads(self):
        for name in ("repository", "pr_number", "output_mode", "claude_model", "codex_model", "claude_effort",
                     "codex_effort", "policy_path", "head_sha", "base_sha", "merge_base_sha", "policy_sha",
                     "policy_source", "policy_present", "diff_sha256", "snapshot_id", "publishable",
                     "claude_status", "codex_status", "comment_id", "comment_action"):
            self.assertRegex(self.action, rf"(?m)^  {name}:\n    description:", name)


class ActionInvocationContractTests(unittest.TestCase):
    """The action's command lines must match what each script's parser accepts.

    A flag that drifts (renamed, added, or dropped on one side) would otherwise
    only surface as a failed job on a real run.
    """

    def parser_options(self, script: str) -> tuple[set[str], set[str]]:
        import argparse
        from unittest import mock

        captured: dict = {}

        class Captured(Exception):
            pass

        def spy(parser, args=None, namespace=None):
            captured["known"] = set(parser._option_string_actions)
            captured["required"] = {
                option for action in parser._actions if action.required for option in action.option_strings[:1]
            }
            raise Captured

        module = support.load_script(script.replace("-", "_").removesuffix(".py"), script)
        with mock.patch.object(argparse.ArgumentParser, "parse_args", spy):
            try:
                module.parse_args([])
            except Captured:
                pass
        return captured["known"], captured["required"]

    def test_every_script_invocation_matches_its_parser(self):
        action = ACTION_FILE.read_text(encoding="utf-8")
        calls = re.findall(r'python3 "\$GITHUB_ACTION_PATH/\.\./\.\./scripts/([a-z-]+\.py)" \\\n((?:\s+--[^\n]*\n?)+)', action)
        scripts = [name for name, _ in calls]
        self.assertEqual(
            sorted(scripts),
            sorted([
                "validate-request.py", "check-operations.py", "prepare-review.py", "run-review.py", "normalize-review.py",
                "run-codex-review.py", "normalize-codex-review.py", "finalize-review.py",
                "report-summary.py", "publish-review.py",
            ]),
        )
        for script, flag_lines in calls:
            used = set(re.findall(r"(--[a-z0-9-]+)", flag_lines))
            known, required = self.parser_options(script)
            with self.subTest(script=script):
                self.assertEqual(used - known, set(), "flags the script does not accept")
                self.assertEqual(required - used, set(), "required flags the action does not pass")


class NormalCIWorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    def test_ci_is_separate_from_manual_ai_review(self):
        trigger = self.workflow.split("\npermissions:", 1)[0]
        self.assertIn("  push:", trigger)
        self.assertIn("  pull_request:", trigger)
        self.assertNotIn("workflow_dispatch:", trigger)
        self.assertNotIn("workflow_call:", trigger)
        for name in ("CLAUDE_CODE_OAUTH_TOKEN", "OPENAI_API_KEY", "AI_REVIEW_READ_TOKEN", "AI_REVIEW_COMMENT_TOKEN"):
            self.assertNotIn(name, self.workflow)
        self.assertNotRegex(self.workflow.lower(), r"(?m)^\s*uses:\s*[^\n]*claude")
        self.assertNotIn("pull-requests: write", self.workflow)

    def test_ci_is_read_only_and_does_not_persist_checkout_credentials(self):
        match = re.search(r"(?m)^permissions:\n((?:  [a-z-]+: (?:read|write|none)\n)+)", self.workflow)
        self.assertIsNotNone(match)
        self.assertEqual(re.findall(r"(?m)^  ([a-z-]+): (read|write|none)$", match.group(1)), [("contents", "read")])
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertIn("fetch-depth: 0", self.workflow)

    def test_ci_actions_are_pinned_and_runs_the_standard_test_command(self):
        uses = re.findall(r"(?m)^\s+-?\s*uses: (\S+)", self.workflow)
        self.assertTrue(uses)
        for ref in uses:
            self.assertRegex(ref, FULL_SHA_REF)
        self.assertIn("python-version-file: pyproject.toml", self.workflow)
        self.assertIn("run: python3 -m unittest discover -s tests", self.workflow)

    def test_ci_validates_the_workflow_and_action_yaml(self):
        for path in (".github/workflows/ci.yml", ".github/workflows/cross-review.yml", "actions/review-runtime/action.yml"):
            self.assertIn(path, self.workflow)
        self.assertNotIn("ai-review.yml", self.workflow)
        self.assertNotIn("claude-review.yml", self.workflow)


class WorkflowSyntaxTests(unittest.TestCase):
    def test_workflows_and_action_parse_as_yaml(self):
        ruby = shutil.which("ruby")
        if ruby is None:
            self.skipTest("Ruby is unavailable; CI performs the required YAML parse")
        result = subprocess.run(
            [ruby, "-e", 'require "yaml"; ARGV.each { |path| Psych.parse_file(path) }',
             *(str(path) for path in WORKFLOW_PATHS), str(ACTION_FILE)],
            cwd=ROOT, check=False, capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ArtifactRetentionTests(unittest.TestCase):
    def test_bundle_is_short_lived_and_final_result_carries_no_bundle(self):
        workflow = CROSS_WORKFLOW.read_text(encoding="utf-8")
        blocks = re.findall(r"uses: actions/upload-artifact@[^\n]*\n        with:\n((?:          .*\n)+)", workflow)
        by_name = {}
        for block in blocks:
            name = re.search(r"name: (\S+)", block).group(1)
            by_name[name.split("-${{")[0]] = block
        self.assertEqual(set(by_name), {"ai-review-bundle", "ai-review-claude", "ai-review-codex", "ai-review-final"})
        self.assertIn("retention-days: 1", by_name["ai-review-bundle"])
        for name in ("ai-review-claude", "ai-review-codex"):
            self.assertIn("retention-days: 1", by_name[name])
        self.assertIn("retention-days: 7", by_name["ai-review-final"])
        self.assertIn("ai-review-final", by_name["ai-review-final"])
        self.assertNotIn("ai-review-bundle", by_name["ai-review-final"].split("path:")[1])
        # Stage artifacts hold one normalized JSON file each, never a directory.
        self.assertTrue(re.search(r"path: .*review-result\.json", by_name["ai-review-claude"]))
        self.assertTrue(re.search(r"path: .*codex-result\.json", by_name["ai-review-codex"]))


class DocumentationConsistencyTests(unittest.TestCase):
    """Paths the Plan and ADRs cite must exist, so the docs cannot drift silently."""

    DOCS = ("docs/plan/two-stage-cross-review-plan.md", *(f"docs/adr/{n}" for n in (
        "0005-central-execution-two-stage-review.md",
        "0006-cross-review-trust-boundaries.md",
        "0007-codex-verification-via-responses-api.md",
        "0008-cross-repository-authentication.md",
        "0009-documentation-and-readme-policy.md",
        "0010-operations-hardening.md",
    )))

    def test_repository_paths_cited_in_the_new_docs_exist(self):
        pattern = re.compile(r"`((?:scripts|tests|schemas|prompts|policies|actions|docs|\.github)/[A-Za-z0-9_./-]+\.[a-z]+)`")
        # Cited on purpose: files that were removed, files that must not be created, and
        # the review policy path, which lives in the *target* repository, not this one.
        gone = {
            ".github/workflows/ai-review.yml",
            ".github/workflows/claude-review.yml",
            "docs/adr/README.md",
            ".github/ai-review.md",
        }
        missing = []
        for doc in self.DOCS:
            text = (ROOT / doc).read_text(encoding="utf-8")
            for cited in pattern.findall(text):
                if cited not in gone and not (ROOT / cited).exists():
                    missing.append(f"{doc}: {cited}")
        self.assertEqual(missing, [])

    def test_the_active_plan_is_named_in_the_ssot(self):
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("docs/plan/two-stage-cross-review-plan.md", agents)
        self.assertTrue((ROOT / "docs/plan/two-stage-cross-review-plan.md").is_file())
        self.assertTrue((ROOT / "docs/plan/implementation-plan.md").is_file())

    def test_the_plan_defines_the_phase_that_follows_the_earlier_ones(self):
        plan = (ROOT / "docs/plan/two-stage-cross-review-plan.md").read_text(encoding="utf-8")
        self.assertIn("### Phase 5：中央実行・二段階レビュー", plan)
        self.assertIn("Phase 0〜4", plan)


class DecisionStatusTests(unittest.TestCase):
    SUPERSEDED = {1: 5, 2: 6, 3: 9, 4: 8}
    ACCEPTED = (5, 6, 7, 8, 9)

    def read(self, number: int) -> tuple[Path, str]:
        path = next(ADR_DIR.glob(f"{number:04d}-*.md"))
        return path, path.read_text(encoding="utf-8")

    def status(self, text: str) -> str:
        return re.search(r"## Status\n\n(.+)\n", text).group(1).strip()

    def test_superseded_adrs_link_to_their_successors(self):
        for old, new in self.SUPERSEDED.items():
            path, text = self.read(old)
            successor, successor_text = self.read(new)
            with self.subTest(adr=old):
                self.assertEqual(self.status(text), f"Superseded by ADR-{new:04d}")
                self.assertIn(successor.name, text)
                self.assertIn(f"Supersedes ADR-{old:04d}", successor_text)

    def test_superseded_adrs_keep_their_original_reasoning(self):
        for old in self.SUPERSEDED:
            _path, text = self.read(old)
            for header in ("## Context", "## Decision", "## Rationale", "## Alternatives Considered", "## Consequences"):
                self.assertIn(header, text)

    def test_new_decisions_are_recorded_and_consistent_with_the_implementation(self):
        for number in self.ACCEPTED:
            _path, text = self.read(number)
            self.assertEqual(self.status(text).splitlines()[0], "Accepted", number)

    def test_every_adr_is_numbered_without_gaps(self):
        numbers = sorted(int(p.name[:4]) for p in ADR_DIR.glob("*.md"))
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))


if __name__ == "__main__":
    unittest.main()
