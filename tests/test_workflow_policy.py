"""Phase 4 static policy tests for consumer integration and normal CI."""

from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONSUMER_WORKFLOW = ROOT / ".github" / "workflows" / "ai-review.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
ADR_DIR = ROOT / "docs" / "adr"
WORKFLOW_PATHS = tuple((ROOT / ".github" / "workflows").glob("*.yml"))

FULL_SHA_REF = re.compile(r"^[\w.-]+/[\w.-]+(?:/[\w./-]+)?@[0-9a-f]{40}$")


def job_sections(workflow: str, *job_names: str) -> dict[str, str]:
    """Return top-level job bodies from the repository's constrained YAML style."""
    jobs_marker = "\njobs:\n"
    if jobs_marker not in workflow:
        raise AssertionError("workflow has no top-level jobs mapping")
    jobs = workflow.split(jobs_marker, 1)[1]
    sections: dict[str, str] = {}
    for index, name in enumerate(job_names):
        start = f"  {name}:\n"
        if start not in jobs:
            raise AssertionError(f"workflow has no {name!r} job")
        body = jobs.split(start, 1)[1]
        if index + 1 < len(job_names):
            next_start = f"\n  {job_names[index + 1]}:\n"
            if next_start not in body:
                raise AssertionError(
                    f"workflow job {job_names[index + 1]!r} is missing or out of order"
                )
            body = body.split(next_start, 1)[0]
        sections[name] = body
    return sections


def reusable_ref(consumer: str) -> str:
    """Return the reusable workflow reference from a consumer wrapper."""
    match = re.search(r"(?m)^    uses: (\S+)$", consumer)
    if match is None:
        raise AssertionError("consumer has no reusable workflow reference")
    return match.group(1)


def pinned_reusable_workflow(testcase: unittest.TestCase) -> str:
    """Read the exact reusable workflow commit selected by the consumer."""
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        testcase.skipTest("git worktree is required to inspect the pinned workflow")
    ref = reusable_ref(CONSUMER_WORKFLOW.read_text(encoding="utf-8"))
    sha = ref.rsplit("@", 1)[1]
    path = ".github/workflows/claude-review.yml"
    result = subprocess.run(
        [git, "show", f"{sha}:{path}"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    testcase.assertEqual(
        result.returncode,
        0,
        f"cannot read pinned reusable workflow {sha}: {result.stderr.strip()}",
    )
    return result.stdout


class ConsumerWorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.workflow = CONSUMER_WORKFLOW.read_text(encoding="utf-8")
        self.jobs = job_sections(self.workflow, "validate", "review")

    def test_manual_trigger_has_only_pr_number_input(self):
        trigger = self.workflow.split("\npermissions:", 1)[0]
        self.assertIn("  workflow_dispatch:\n", trigger)
        for forbidden in ("pull_request:", "pull_request_target:", "issue_comment:",
                          "schedule:", "push:", "workflow_call:"):
            self.assertNotIn(forbidden, trigger)
        inputs = re.findall(r"(?m)^      ([a-z][a-z0-9_]*):$", trigger)
        self.assertEqual(inputs, ["pr_number"])
        self.assertIn("        required: true", trigger)
        self.assertIn("        type: string", trigger)

    def test_pr_number_is_validated_before_the_reusable_workflow(self):
        validate = self.jobs["validate"]
        review = self.jobs["review"]
        self.assertIn("AI_REVIEW_PR_NUMBER: ${{ inputs.pr_number }}", validate)
        self.assertIn('re.fullmatch(r"[1-9][0-9]{0,9}", value)', validate)
        self.assertIn("if int(value) > 2**31 - 1:", validate)
        self.assertIn("pr_number: ${{ steps.validate.outputs.pr_number }}", validate)
        self.assertIn("needs: validate", review)
        self.assertIn("pr_number: ${{ needs.validate.outputs.pr_number }}", review)
        for block in re.findall(r"run: \|\n((?:[ ]{10}.*\n?)+)", validate):
            self.assertNotIn("${{", block)

    def test_execution_is_limited_to_the_default_branch(self):
        validate = self.jobs["validate"]
        self.assertIn("AI_REVIEW_REF: ${{ github.ref }}", validate)
        self.assertIn(
            "AI_REVIEW_DEFAULT_BRANCH: ${{ github.event.repository.default_branch }}",
            validate,
        )
        self.assertIn('ref != f"refs/heads/{default_branch}"', validate)
        self.assertLess(validate.index("Reject non-default ref"), validate.index("Validate PR number"))

    def test_runs_for_the_same_pr_are_serialized_without_cancellation(self):
        pre_jobs = self.workflow.split("\njobs:\n", 1)[0]
        self.assertIn("concurrency:\n  group: ai-review-${{ inputs.pr_number }}", pre_jobs)
        self.assertIn("  cancel-in-progress: false", pre_jobs)

    def test_external_reusable_workflow_is_pinned_to_full_sha(self):
        review = self.jobs["review"]
        ref = reusable_ref(self.workflow)
        self.assertRegex(ref, FULL_SHA_REF)
        self.assertTrue(
            ref.startswith("ktg6/ai-cross-pr-review/.github/workflows/claude-review.yml@")
        )

    def test_secret_and_permissions_are_explicit_and_minimal(self):
        validate = self.jobs["validate"]
        review = self.jobs["review"]
        self.assertIn("permissions: {}", self.workflow)
        self.assertIn("permissions: {}", validate)
        self.assertNotIn("secrets:", validate)
        self.assertNotIn("secrets: inherit", self.workflow)
        permissions = re.search(
            r"(?m)^    permissions:\n((?:      [a-z-]+: (?:read|write|none)\n)+)",
            review,
        )
        self.assertIsNotNone(permissions)
        self.assertEqual(
            re.findall(r"(?m)^      ([a-z-]+): (read|write|none)$", permissions.group(1)),
            [("contents", "read"), ("pull-requests", "write")],
        )
        self.assertEqual(review.count("${{ secrets."), 1)
        self.assertIn(
            "claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}",
            review,
        )

    def test_wrapper_contains_no_checkout_or_review_implementation(self):
        review = self.jobs["review"]
        self.assertNotIn("actions/checkout", self.workflow)
        self.assertNotIn("runs-on:", review)
        self.assertNotIn("steps:", review)
        self.assertNotIn("continue-on-error", self.workflow)
        self.assertNotIn("if: always()", self.workflow)


class NormalCIWorkflowPolicyTests(unittest.TestCase):
    def setUp(self):
        self.workflow = CI_WORKFLOW.read_text(encoding="utf-8")

    def test_ci_is_separate_from_manual_ai_review(self):
        trigger = self.workflow.split("\npermissions:", 1)[0]
        self.assertIn("  push:", trigger)
        self.assertIn("  pull_request:", trigger)
        self.assertNotIn("workflow_dispatch:", trigger)
        self.assertNotIn("workflow_call:", trigger)
        self.assertNotIn("CLAUDE_CODE_OAUTH_TOKEN", self.workflow)
        self.assertNotRegex(self.workflow.lower(), r"(?m)^\s*uses:\s*[^\n]*claude")
        self.assertNotIn("pull-requests: write", self.workflow)

    def test_ci_is_read_only_and_does_not_persist_checkout_credentials(self):
        self.assertIn("permissions:\n  contents: read", self.workflow)
        permissions = re.search(
            r"(?m)^permissions:\n((?:  [a-z-]+: (?:read|write|none)\n)+)",
            self.workflow,
        )
        self.assertIsNotNone(permissions)
        self.assertEqual(
            re.findall(r"(?m)^  ([a-z-]+): (read|write|none)$", permissions.group(1)),
            [("contents", "read")],
        )
        self.assertIn("persist-credentials: false", self.workflow)
        self.assertIn("fetch-depth: 0", self.workflow)

    def test_ci_actions_are_pinned_and_runs_the_standard_test_command(self):
        uses = re.findall(r"(?m)^\s+-?\s*uses: (\S+)", self.workflow)
        self.assertTrue(uses)
        for ref in uses:
            self.assertRegex(ref, FULL_SHA_REF)
        self.assertIn("actions/setup-python@", self.workflow)
        self.assertIn("python-version-file: pyproject.toml", self.workflow)
        self.assertIn("Psych.parse_file(path)", self.workflow)
        self.assertIn("run: python3 -m unittest discover -s tests", self.workflow)


class WorkflowSyntaxTests(unittest.TestCase):
    def test_workflows_parse_as_yaml(self):
        ruby = shutil.which("ruby")
        if ruby is None:
            self.skipTest("Ruby is unavailable; CI performs the required YAML parse")
        result = subprocess.run(
            [
                ruby,
                "-e",
                'require "yaml"; ARGV.each { |path| Psych.parse_file(path) }',
                *(str(path) for path in WORKFLOW_PATHS),
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ReusableWorkflowConnectionTests(unittest.TestCase):
    def test_consumer_inputs_and_secret_match_the_reusable_contract(self):
        consumer = CONSUMER_WORKFLOW.read_text(encoding="utf-8")
        reusable = pinned_reusable_workflow(self)
        contract = reusable.split("\npermissions:", 1)[0]
        self.assertIn("      pr_number:", contract)
        self.assertIn("        type: string", contract)
        self.assertIn("      claude_code_oauth_token:", contract)
        self.assertIn("        required: true", contract)
        self.assertIn("pr_number: ${{ needs.validate.outputs.pr_number }}", consumer)
        self.assertIn("claude_code_oauth_token: ${{ secrets.", consumer)

    def test_auth_and_publish_failures_are_not_masked(self):
        consumer = CONSUMER_WORKFLOW.read_text(encoding="utf-8")
        reusable = pinned_reusable_workflow(self)
        self.assertNotIn("continue-on-error", consumer)
        self.assertNotIn("continue-on-error", reusable)
        self.assertIn("if-no-files-found: error", reusable)
        self.assertIn("needs: [prepare, review]", reusable)

    def test_default_policy_contract_is_preserved(self):
        reusable = pinned_reusable_workflow(self)
        self.assertIn("default: .github/ai-review.md", reusable)

    def test_reusable_workflow_targets_the_callers_repository(self):
        reusable = pinned_reusable_workflow(self)
        self.assertIn("repository: ${{ github.repository }}", reusable)


class Phase4DecisionStatusTests(unittest.TestCase):
    def test_implemented_architecture_decisions_are_accepted(self):
        for number in range(1, 5):
            path = next(ADR_DIR.glob(f"{number:04d}-*.md"))
            text = path.read_text(encoding="utf-8")
            status = re.search(r"## Status\n\n(\S+)", text)
            self.assertIsNotNone(status, path.name)
            self.assertEqual(status.group(1), "Accepted", path.name)


if __name__ == "__main__":
    unittest.main()
