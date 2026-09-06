# ADR-0001: Manual trigger and reusable workflow for AI PR review

## Status

Proposed

## Context

汎用AI PRレビュー基盤を複数のconsumer repositoryへ薄く導入したい。自動trigger、`pull_request_target`、Actions内でのCodex実行は、fork PRからのSecret到達や攻撃面の拡大を招く。通常CIとAI Reviewを同一workflowに混在させると、権限とSecretの境界が曖昧になる。

## Decision

- 通常CIとAI Reviewを分離する。
- Consumer側は`workflow_dispatch(pr_number)`のthin wrapper（`.github/workflows/ai-review.yml`）だけを持つ。
- 共通repository側は`workflow_call`のReusable Workflow（`.github/workflows/claude-review.yml`）を提供し、consumerはfull commit SHAで固定して呼び出す。
- `secrets: inherit`を使わず、必要なSecretだけを明示的に渡す。
- Claude Codeを一次レビューとし、CodexはActionsへ組み込まず、GitHub上の`@codex review`による独立した二次レビューとする。
- 対象はGitHub.comとし、GHES対応は将来の別decisionとする。

## Rationale

手動起動はtrustedな起動者を保証し、fork PRでもSecretがuntrusted codeへ到達する構造を作らない。Reusable Workflowのfull SHA固定は、共通側の変更がconsumerへ無断で波及することを防ぐ。CodexをActions外に置くことで、Claudeの結果に依存しない独立レビューを得られる。

## Alternatives Considered

- `pull_request`/`pull_request_target`による自動trigger: fork PRとSecretの境界が複雑になり、MVPでは採用しない。
- PR comment trigger: 起動者検証とbot loopの制御が必要になり、MVPでは採用しない。
- CodexをActionsへ統合: Secretと権限の面が増え、独立性も下がるため採用しない。
- Composite Actionのみでの配布: 3 job分離と権限分離が表現しにくいため採用しない。

## Consequences

- レビューは自動では走らず、人が`pr_number`を指定して起動する。
- consumerは共通workflowのSHA更新を明示的に行う必要がある。
- GHESや自動trigger対応は後続ADRで扱う。

## References

- `docs/plan/implementation-plan.md` 3章、4章、12章、14章
- ADR-0002（trust boundaries）
