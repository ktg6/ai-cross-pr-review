# ADR-0005: Central execution repository and two-stage review

## Status

Accepted

Supersedes ADR-0001

## Context

ADR-0001は、consumer repositoryへ`workflow_dispatch`のthin wrapper（`.github/workflows/ai-review.yml`）を置き、共通repositoryのReusable Workflowをfull commit SHAで呼び出す構成を決めた。またCodexはActionsへ組み込まず、GitHub上の`@codex review`で独立した二次レビューを行うものとした。

運用要件が次のように変わった。

- レビュー対象repositoryへworkflowを追加したくない。対象repositoryのdefault branchへ変更を入れる権限、Actions有効化、Secret登録をレビュー導入の前提にしたくない。
- Claudeの一次レビューをCodexが同一スナップショットで再検証する二段階レビューが必要になった。`@codex review`は起動経路・入力・出力形式を制御できず、Claude結果との突合を決定論的に行えない。
- 出力先をJob Summaryだけに限定する運用（PR write権限も投稿credentialも不要）が必要になった。

これらはADR-0001のDecisionと両立しないため、後継ADRとして本ADRを作成する。ADR-0001の理由は書き換えない。

## Decision

- レビューは、信頼された非公開の**中央実行repository**でだけ実行する。対象repositoryには`.github/workflows/ai-review.yml`を含め、いかなるworkflowも追加しない。
- 中央実行repositoryは`workflow_dispatch`のworkflowを1本（`.github/workflows/cross-review.yml`）だけ提供する。`workflow_call`は提供しない。入口を1つに保ち、別経路からallowlist検証を迂回できないようにする。将来`workflow_call`を追加する場合も、`validate_request` jobを必須の前段とし、後続jobは`validate_request`のjob outputだけを入力とする。
- jobは次に分離する。
  - `validate_request`: 入力のallowlist検証。permissionsなし。
  - `prepare`: 対象repositoryからPR snapshotを取得。read tokenだけを持つ。
  - `claude_review`: Claudeによる一次レビュー。GitHub credentialを持たない。
  - `codex_review`: Codexによる再検証。GitHub credentialを持たない。
  - `finalize`: 決定論的な検証・統合・整形。credentialを持たない。
  - `report`: Job Summary表示とartifact保存。credentialを持たない。
  - `comment`: `pr_comment`かつ投稿可の場合だけ実行する条件付きjob。comment tokenだけを持つ。
- AI jobにGitHub write権限と対象repositoryへのcredentialを与えない。全jobは中央repository自身のcheckoutのために`contents: read`を持つが、この`GITHUB_TOKEN`は中央repositoryにしか届かず、いずれのstepにも渡さず、checkoutはpersist-credentials: falseで行う。PRコメントは`comment` jobだけが行う。
- PRコード、PR由来workflow、hooks、Agent設定をcheckout・実行しない。
- 対象repository、PR番号、base/head SHA、投稿先、API endpoint、merge判断をAIに決定させない。これらは`validate_request`と`prepare`のjob outputで決まる。
- 二段階レビューの責務を次のとおり固定する。
  - Claude: 一次レビュー。PR snapshotだけを根拠に構造化findingsを返す。
  - Codex: 再検証。同一snapshotとClaude結果を入力に、Claudeの各指摘を`adopted` / `duplicate` / `rejected` / `deferred`に分類し、追加指摘と情報不足を報告する。
  - `@codex review`による独立レビューは、本基盤の一部としては扱わない。人が必要に応じて別途実行することを妨げない。
- 出力モードを導入する。`summary_only`を安全なデフォルトとする。
  - `summary_only`: PRへコメントしない。Job Summaryとartifactへ出す。comment tokenを一切参照しない。
  - `pr_comment`: Job Summaryとartifactに加え、対象PRへ決定論的publisherがコメントする。
- 不完全、stale、schema不正、AI失敗のいずれかであればPRへ投稿しない。Job Summaryとartifactには失敗状態を明示する。失敗を「問題なし」として表示・投稿しない。
- 対象はGitHub.comとする。GHES対応は将来の別decisionとする（ADR-0001から変更なし）。

## Rationale

対象repositoryへworkflowを置かない構成は、導入コストと攻撃面の両方を下げる。対象側にSecretを置かず、対象側のworkflow変更権限を持つ者がレビュー経路を書き換えることもできない。

二段階レビューをActions内で行うことで、両モデルへ同一のsnapshotを渡し、結果の突合を決定論的に行える。`@codex review`では入力snapshotを固定できず、Claude結果との参照整合性を検証できない。

`summary_only`を既定にすると、最も権限の少ない経路が既定になる。PR write権限と投稿credentialは、明示的に`pr_comment`を選んだ実行でのみ必要になる。

## Alternatives Considered

- ADR-0001の構成を維持し、consumer wrapperにCodex stepを足す: 対象repositoryへworkflowを置く前提が残り、要件を満たさない。対象側でSecretが必要になる点も変わらない。
- 中央実行repositoryで`workflow_call`を併設する: 呼び出し元ごとに入力検証の経路が分かれ、allowlist検証の迂回経路になりうるため採用しない。
- Claudeの結果をCodexへ渡さないblind構成を維持する: 重複・誤検知の整理という今回の目的を満たせないため採用しない。代わりに、Claude結果をuntrusted dataとして扱う境界（ADR-0006）を設ける。
- `pr_comment`を既定にする: 既定で書き込み権限と投稿credentialを要求することになるため採用しない。
- 単一jobで一次レビューと再検証を行う: 2つのprovider credentialが同一jobに共存し、失敗の切り分けもできないため採用しない。

## Consequences

- consumer側のthin wrapper（`.github/workflows/ai-review.yml`）と、そのためのReusable Workflow（`.github/workflows/claude-review.yml`）は廃止する。本repositoryからは削除し、`cross-review.yml`へ統合する。
- レビューは自動では走らない。中央実行repositoryのActionsから、trustedな起動者が対象repositoryとPRを指定して起動する。
- 中央実行repositoryは対象PRの内容（diff、metadata、レビュー結果）をJob Summaryとartifactに保持する。中央repositoryの閲覧権限が実質的なアクセス制御になるため、非公開であることと閲覧範囲の管理が前提になる。
- Codexの失敗はレビュー全体の失敗として扱われ、部分的な結果を「問題なし」として投稿しない。Claudeだけ成功した場合も、Codexの検証を経ていないため投稿しない。
- 対象repositoryが増えるたびに必要なのは認証の到達範囲の追加だけで、対象側のfile追加は不要になる。
- 起動入力が増えるため、入力検証の責務が`validate_request`に集中する。検証はPython側のallowlistを正とし、workflowの`choice`は二重化として扱う。

## References

- `docs/plan/two-stage-cross-review-plan.md` 3章、4.1節、4.2節、4.3節、5章
- `.github/workflows/cross-review.yml`、`actions/review-runtime/action.yml`
- `scripts/validate-request.py`、`scripts/finalize-review.py`、`tests/test_workflow_policy.py`、`tests/test_cross_pipeline.py`、`tests/test_finalize_review.py`、`tests/test_validate_request.py`
- ADR-0001（Superseded）、ADR-0006、ADR-0007、ADR-0008
