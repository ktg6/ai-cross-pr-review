# ADR-0003: Canonical agent instructions

## Status

Proposed

## Context

Claude Code、Codex、OpenCodeはそれぞれ異なるinstruction fileとpermission機構を持つ。ルールを各Agent設定へ複製すると乖離が生じ、symbolic linkは環境依存で壊れやすい。既存の`AGENTS.md`（OpenCode向けrules）には保持すべきユーザー定義の禁止事項がある。

## Decision

- `AGENTS.md`を共通project instructionsのSingle Source of Truthとする。Project Purpose、Architecture Principles、Agent Roles、Development Workflow、Security、Git/GitHub Restrictions、Testing、AI Review Trust Boundaries、ADR運用、Definition of Doneを本文に置く。
- `CLAUDE.md`はsymbolic linkではなくthin adapterとし、「`AGENTS.md`を読む」「`AGENTS.md`に従う」「Claude固有permissionは`.claude/`で管理する」だけを記載する。
- 詳細は`.ai/rules/security.md`、`.ai/rules/review.md`へ分離できるが、参照先が自動ロードされる前提を置かない。
- Agent固有設定はルールの強制方法だけを定義する。
  - Claude Code: `.claude/settings.json`の`permissions.allow / ask / deny`。
  - Codex: `.codex/config.toml`（`approval_policy`、`sandbox_mode`）と`.codex/rules/safety.rules`（Starlark `prefix_rule`）。
  - OpenCode: rootの`opencode.json`の`permission`オブジェクト。
- 公式仕様で確認できないpermissionキーは作成しない。
- docs生成禁止の例外として、承認済みPlanの`docs/plan/`保存と承認済みADRの`docs/adr/`作成・状態更新を認める。

## Rationale

意味を1箇所に置くことで、Agent間の乖離とレビュー重複を防ぐ。thin adapterはClaude Codeが`CLAUDE.md`を自動ロードする仕様を活かしつつ、内容の複製を避ける。強制方法は各Agentの公式仕様に従うため、Agentごとに異なるファイルへ分ける。

## Alternatives Considered

- `CLAUDE.md`を`AGENTS.md`へのsymbolic linkにする: OS・Git設定・Actions環境で挙動が変わるため採用しない。
- 各Agent設定へルール本文を複製する: 乖離が発生するため採用しない。
- 実装Planで想定していたOpenCodeの`permissions`配列（V2）: OpenCode 1.18.29の公式schemaでは`permission`オブジェクト（tool名→`allow | ask | deny`またはpattern→値のobject）であり、配列形式は存在しないため公式仕様に従う。

## Consequences

- 既存`AGENTS.md`のLanguage、Security、Output、Workflowの各ルールは保持し、SSOTとして拡張した。
- Codexの`.codex/`はprojectがtrustedの場合だけロードされる。untrustedではproject-scoped config・rulesは適用されない。
- Claude Codeの`Write(path)`ルールはfile permission checkで参照されないため、`Edit(path)`を使う。
- 設定の妥当性は`tests/test_agent_config.py`で検証する。

## References

- `docs/plan/implementation-plan.md` 13章、15章
- Claude Code permissions / settings docs（code.claude.com/docs/en/permissions, /settings）
- Codex config reference / rules docs（learn.chatgpt.com/docs/config-file/config-reference, /docs/agent-configuration/rules）
- OpenCode permissions docs / schema（opencode.ai/docs/permissions, opencode.ai/config.json）
