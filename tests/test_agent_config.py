"""Phase 0 tests: agent configuration skeleton, permission semantics, AGENTS SSOT.

Standard library only. Run with: python3 -m unittest discover -s tests
"""

import ast
import json
import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CLAUDE_SETTINGS = ROOT / ".claude" / "settings.json"
CODEX_CONFIG = ROOT / ".codex" / "config.toml"
CODEX_RULES = ROOT / ".codex" / "rules" / "safety.rules"
OPENCODE_CONFIG = ROOT / "opencode.json"
AGENTS_MD = ROOT / "AGENTS.md"
CLAUDE_MD = ROOT / "CLAUDE.md"
PYPROJECT = ROOT / "pyproject.toml"
GITIGNORE = ROOT / ".gitignore"
ADR_DIR = ROOT / "docs" / "adr"
RULES_DIR = ROOT / ".ai" / "rules"

PHASE0_FILES = [
    CLAUDE_SETTINGS,
    CODEX_CONFIG,
    CODEX_RULES,
    OPENCODE_CONFIG,
    AGENTS_MD,
    CLAUDE_MD,
    PYPROJECT,
    GITIGNORE,
    RULES_DIR / "security.md",
    RULES_DIR / "review.md",
]

# Fictional canary shapes only; never real credentials.
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"ghp_[A-Za-z0-9]{8,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{8,}"),
    re.compile(r"CLAUDE_CODE_OAUTH_TOKEN\s*[=:]\s*\S{8,}"),
    re.compile(r"ANTHROPIC_API_KEY\s*[=:]\s*\S{8,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def _claude_permissions():
    data = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
    return data["permissions"]


def _codex_rules():
    """Parse Starlark prefix_rule() calls via Python's ast (syntax-compatible subset)."""
    tree = ast.parse(CODEX_RULES.read_text(encoding="utf-8"))
    rules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None)
            kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in node.keywords}
            rules.append((name, kwargs))
    return rules


def _rule_matches(pattern, argv):
    """Minimal prefix matcher mirroring prefix_rule semantics (list = union)."""
    if len(argv) < len(pattern):
        return False
    for token, arg in zip(pattern, argv):
        options = token if isinstance(token, list) else [token]
        if arg not in options:
            return False
    return True


def _codex_decision(argv):
    order = {"allow": 0, "prompt": 1, "forbidden": 2}
    decisions = [
        kw["decision"]
        for name, kw in _codex_rules()
        if name == "prefix_rule" and _rule_matches(kw["pattern"], argv)
    ]
    if not decisions:
        return None
    return max(decisions, key=order.__getitem__)


class ClaudeSettingsTest(unittest.TestCase):
    def test_permissions_shape(self):
        data = json.loads(CLAUDE_SETTINGS.read_text(encoding="utf-8"))
        self.assertEqual(set(data.keys()), {"permissions"})
        perms = data["permissions"]
        self.assertTrue(set(perms.keys()) <= {"allow", "ask", "deny"})
        for key in ("allow", "ask", "deny"):
            self.assertIsInstance(perms[key], list)
            self.assertTrue(all(isinstance(r, str) for r in perms[key]))

    def test_rule_syntax_is_recognized_by_claude(self):
        perms = _claude_permissions()
        for rule in perms["allow"] + perms["ask"] + perms["deny"]:
            # ':*' is only recognized at the end of a pattern.
            if ":*" in rule:
                self.assertTrue(rule.endswith(":*)"), rule)
            # Bash(command:...) is ignored by Claude Code; must not be used.
            self.assertFalse(rule.startswith("Bash(command:"), rule)
            # Write(path) rules are never consulted for file checks; use Edit(path).
            self.assertFalse(rule.startswith("Write("), rule)

    def test_dangerous_operations_denied(self):
        deny = set(_claude_permissions()["deny"])
        for rule in (
            "Bash(git push:*)",
            "Bash(gh pr merge:*)",
            "Bash(gh pr close:*)",
            "Bash(sudo:*)",
            "Bash(rm -rf:*)",
            "Bash(terraform apply:*)",
            "Bash(terraform destroy:*)",
            "Bash(git reset --hard:*)",
            "Bash(git clean:*)",
        ):
            self.assertIn(rule, deny)

    def test_credential_paths_denied(self):
        deny = set(_claude_permissions()["deny"])
        for rule in (
            "Read(.env*)",
            "Read(~/.aws/**)",
            "Read(~/.ssh/**)",
            "Read(**/terraform.tfstate*)",
        ):
            self.assertIn(rule, deny)

    def test_commit_requires_approval_not_silent_allow(self):
        perms = _claude_permissions()
        self.assertIn("Bash(git commit:*)", perms["ask"])
        self.assertNotIn("Bash(git commit:*)", perms["allow"])

    def test_normal_development_not_over_blocked(self):
        perms = _claude_permissions()
        allow = set(perms["allow"])
        deny = set(perms["deny"])
        for rule in ("Read", "Grep", "Glob", "Bash(git status:*)", "Bash(git diff:*)"):
            self.assertIn(rule, allow)
        self.assertTrue(any("unittest" in r for r in allow))
        # Broad deny rules would override every narrower allow rule (deny-first).
        for broad in ("Bash", "Bash(*)", "Read", "Edit", "Bash(git:*)", "Bash(python3:*)"):
            self.assertNotIn(broad, deny)

    def test_no_dangerous_modes_enabled(self):
        text = CLAUDE_SETTINGS.read_text(encoding="utf-8")
        self.assertNotIn("bypassPermissions", text)
        self.assertNotIn("dangerously", text)


class OpenCodeConfigTest(unittest.TestCase):
    VALID_ACTIONS = {"allow", "ask", "deny"}
    # Verified against https://opencode.ai/config.json (OpenCode 1.18.29).
    VALID_TOOLS = {
        "read", "edit", "glob", "grep", "list", "bash", "task", "external_directory",
        "todowrite", "question", "webfetch", "websearch", "lsp", "doom_loop", "skill",
    }
    STRING_ONLY_TOOLS = {"todowrite", "question", "webfetch", "websearch", "doom_loop"}

    def _permission(self):
        data = json.loads(OPENCODE_CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(data.get("$schema"), "https://opencode.ai/config.json")
        self.assertNotIn("permissions", data, "official key is singular 'permission'")
        return data["permission"]

    def test_permission_keys_and_values(self):
        perm = self._permission()
        self.assertIsInstance(perm, dict)
        for tool, value in perm.items():
            self.assertIn(tool, self.VALID_TOOLS, tool)
            if isinstance(value, dict):
                self.assertNotIn(tool, self.STRING_ONLY_TOOLS, tool)
                for pattern, action in value.items():
                    self.assertIsInstance(pattern, str)
                    self.assertIn(action, self.VALID_ACTIONS, f"{tool}:{pattern}")
            else:
                self.assertIn(value, self.VALID_ACTIONS, tool)

    def test_auxiliary_reviewer_is_read_only(self):
        perm = self._permission()
        self.assertEqual(perm["edit"], "deny")
        self.assertEqual(perm["task"], "deny")
        self.assertEqual(perm["webfetch"], "deny")
        self.assertEqual(perm["websearch"], "deny")
        self.assertEqual(perm["external_directory"], "deny")
        # Shell default deny; last matching rule wins, so '*' must come first.
        bash = perm["bash"]
        self.assertEqual(bash["*"], "deny")
        self.assertEqual(next(iter(bash)), "*")
        self.assertFalse(any(p.startswith("git push") and a == "allow" for p, a in bash.items()))

    def test_credential_reads_denied(self):
        read = self._permission()["read"]
        self.assertEqual(read["*"], "allow")
        self.assertEqual(next(iter(read)), "*")
        for pattern in ("*.env", "*.env.*", "*terraform.tfstate*", "~/.aws/*", "~/.ssh/*"):
            self.assertEqual(read[pattern], "deny", pattern)

    def test_read_only_git_allowed(self):
        bash = self._permission()["bash"]
        for pattern in ("git status*", "git diff*", "git log*", "git show*"):
            self.assertEqual(bash[pattern], "allow", pattern)


class CodexConfigTest(unittest.TestCase):
    # Keys that cannot be overridden at project level per the official reference.
    USER_ONLY_KEYS = {
        "openai_base_url", "chatgpt_base_url", "apps_mcp_product_sku", "model_provider",
        "model_providers", "notify", "profile", "profiles",
        "experimental_realtime_ws_base_url", "otel",
    }

    def test_config_values(self):
        cfg = tomllib.loads(CODEX_CONFIG.read_text(encoding="utf-8"))
        self.assertIn(cfg["approval_policy"], {"untrusted", "on-request"})
        self.assertIn(cfg["sandbox_mode"], {"read-only", "workspace-write"})
        self.assertNotEqual(cfg["sandbox_mode"], "danger-full-access")
        self.assertFalse(self.USER_ONLY_KEYS & set(cfg.keys()))
        # Mutually exclusive with sandbox_mode per reference.
        self.assertNotIn("default_permissions", cfg)
        self.assertNotIn("sandbox_workspace_write", cfg)

    def test_rules_parse_and_decisions_valid(self):
        rules = _codex_rules()
        self.assertTrue(rules)
        for name, kw in rules:
            self.assertEqual(name, "prefix_rule")
            self.assertIn(kw["decision"], {"allow", "prompt", "forbidden"})
            self.assertIsInstance(kw["pattern"], list)
            self.assertTrue(kw["pattern"])

    def test_dangerous_commands_forbidden(self):
        for argv in (
            ["git", "push", "origin", "main"],
            ["git", "push"],
            ["gh", "pr", "merge", "1"],
            ["gh", "pr", "close", "1"],
            ["sudo", "ls"],
            ["rm", "-rf", "/"],
            ["rm", "-fr", "x"],
            ["terraform", "apply"],
            ["terraform", "destroy"],
            ["git", "reset", "--hard", "HEAD~1"],
            ["git", "clean", "-fd"],
        ):
            self.assertEqual(_codex_decision(argv), "forbidden", argv)

    def test_commit_prompts_and_readonly_allowed(self):
        self.assertEqual(_codex_decision(["git", "commit", "-m", "x"]), "prompt")
        for argv in (
            ["git", "status"],
            ["git", "diff", "--stat"],
            ["git", "log", "-3"],
            ["python3", "-m", "unittest", "discover", "-s", "tests"],
        ):
            self.assertEqual(_codex_decision(argv), "allow", argv)
        # Unlisted commands fall through to sandbox/approval policy, not to allow.
        self.assertIsNone(_codex_decision(["make", "test"]))


class CanonicalInstructionsTest(unittest.TestCase):
    REQUIRED_SECTIONS = (
        "## Project Purpose",
        "## Architecture Principles",
        "## Agent Roles",
        "## Development Workflow",
        "## Security",
        "## Git / GitHub Restrictions",
        "## Testing",
        "## AI Review Trust Boundaries",
        "## ADR",
        "## Definition of Done",
    )

    def test_agents_md_is_ssot(self):
        text = AGENTS_MD.read_text(encoding="utf-8")
        for section in self.REQUIRED_SECTIONS:
            self.assertIn(section, text)
        # Existing user rules preserved.
        for rule in ("`git push`", "`sudo`", "`rm -rf`", "`terraform apply`",
                     "`terraform destroy`", "`~/.aws/*`", "`~/.ssh/*`", "`.env*`",
                     "`terraform.tfstate*`", "READMEを更新しない", "回答は日本語で行う"):
            self.assertIn(rule, text)
        self.assertIn(".ai/rules/security.md", text)
        self.assertIn(".ai/rules/review.md", text)

    def test_claude_md_is_thin_adapter(self):
        self.assertFalse(CLAUDE_MD.is_symlink())
        text = CLAUDE_MD.read_text(encoding="utf-8")
        self.assertIn("AGENTS.md", text)
        self.assertIn(".claude/settings.json", text)
        self.assertLess(len(text.splitlines()), 15)
        # Must not duplicate the rule body.
        self.assertNotIn("## Security", text)

    def test_rule_files_reference_agents(self):
        for name in ("security.md", "review.md"):
            text = (RULES_DIR / name).read_text(encoding="utf-8")
            self.assertIn("AGENTS.md", text)


class ProjectSkeletonTest(unittest.TestCase):
    def test_pyproject(self):
        cfg = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        project = cfg["project"]
        self.assertTrue(project["requires-python"].startswith(">=3.1"))
        self.assertEqual(project["dependencies"], [])

    def test_gitignore_excludes_secrets_and_local_settings(self):
        text = GITIGNORE.read_text(encoding="utf-8").splitlines()
        for entry in (".env", ".env.*", "*.tfstate", ".claude/settings.local.json", "__pycache__/"):
            self.assertIn(entry, text)

    def test_adrs_exist_with_required_format(self):
        expected = {
            "0001-manual-reusable-ai-review.md",
            "0002-review-trust-boundaries.md",
            "0003-canonical-agent-instructions.md",
            "0004-review-authentication.md",
        }
        self.assertEqual({p.name for p in ADR_DIR.glob("*.md")}, expected)
        for path in ADR_DIR.glob("*.md"):
            text = path.read_text(encoding="utf-8")
            number = path.name[:4]
            self.assertTrue(text.startswith(f"# ADR-{number}: "), path.name)
            for header in ("## Status", "## Context", "## Decision", "## Rationale",
                           "## Alternatives Considered", "## Consequences", "## References"):
                self.assertIn(header, text, path.name)
            status = re.search(r"## Status\n\n(\S+)", text).group(1)
            self.assertIn(status, {"Proposed", "Accepted", "Superseded", "Deprecated"}, path.name)

    def test_no_secret_like_values_in_phase0_files(self):
        for path in PHASE0_FILES + sorted(ADR_DIR.glob("*.md")):
            text = path.read_text(encoding="utf-8")
            for pattern in SECRET_PATTERNS:
                self.assertIsNone(pattern.search(text), f"{path.name}: {pattern.pattern}")


if __name__ == "__main__":
    unittest.main()
