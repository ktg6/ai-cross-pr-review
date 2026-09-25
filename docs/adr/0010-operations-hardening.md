# ADR-0010: Operations hardening (model allowlist lifecycle, credential expiry warnings, usage recording)

## Status

Accepted

## Context

Phase 5（ADR-0005〜0009）で中央実行・二段階レビューが稼働可能になった。運用を続けるうえで、Planの「残る懸念」とADRのConsequencesに次の課題が残っていた。

- **model allowlistのライフサイクル**: ADR-0007は「公式docsで確認したIDだけをallowlistへ入れる」と定めた。一方で、いつ確認したかをコード上で追跡する仕組みがない。providerがIDを廃止しても、allowlistは古いまま残る。確認日はコメントに書かれているか（OpenAI）、記録がない（Claude）。
- **credentialのrotation**: ADR-0008のSecretは4種類ある（read token、comment token、Claude token、OpenAI key）。所有者・更新担当・期限通知の運用が必要とされたが、期限を知らせる仕組みがない。GitHubはSecretの値も期限も参照させない。したがって、期限切れはレビューの失敗として初めて表面化する。
- **費用上限の運用値**: Claudeは`--max-budget-usd`と`--max-turns`、Codexは`max_output_tokens`で上限を持つ。ただし、実際の使用量が結果に残らない。運用値を調整する根拠がない。Codexの使用token数はnormalized resultに記録されているが、最終結果とSummaryには出ない。Claudeの費用とtoken数は記録されていない。

GitHub App移行もPhase 6の候補であった。今回は範囲外とし、ADR-0008の決定（fine-grained PATの分離運用）を維持する。

## Decision

### 1. model allowlistのライフサイクル

- `scripts/lib/models.py`の各IDに、公式docsでの最終確認日（`verified_on`）と確認元（`reference`）を持たせる。IDのtupleはこのentryから導出する。これにより、allowlistの唯一の定義はentryだけになる。
- 確認日と確認元が記録されていないIDは`None`のまま残す。推測で埋めない。Phase 5時点のClaude 2件がこれにあたる。
- 実行時、選択されたmodelの確認日が未記録か、`Limits.model_verification_max_age_days`（90日）より古い場合、Job Summaryとworkflow annotationで警告する。
- 警告はレビューを停止しない。廃止されたIDはprovider側で拒否され、stageの失敗として記録される。つまり、すでにfail closedである。警告の目的は、その失敗が起きる前に再確認を促すことにある。
- 更新手順は「公式docsで確認 → `models.py`のentry（ID、確認日、確認元）を更新 → workflowのchoice optionsを更新 → ADRのReferences更新 → テスト更新」とする。未確認のIDは追加しない。

### 2. credential期限の警告

- 期限日は**repository variable**で管理する。期限日はSecretではない。`vars`コンテキストから`validate_request` jobだけへ渡す。
  - `AI_REVIEW_READ_TOKEN_EXPIRES_ON`
  - `AI_REVIEW_COMMENT_TOKEN_EXPIRES_ON`
  - `AI_REVIEW_CLAUDE_TOKEN_EXPIRES_ON`
  - `AI_REVIEW_OPENAI_KEY_EXPIRES_ON`（期限のないkeyには、rotation期日を設定する）
- `validate_request` jobの運用チェックstep（`scripts/check-operations.py`）が、日付を`YYYY-MM-DD`として厳密に検証する。そのうえで、期限切れ、または`Limits.credential_expiry_warning_days`（30日）以内に期限が来るものを警告する。未設定は「期限監視の対象外」とSummaryに表示するが、annotationにはしない。形式不正は警告とし、値そのものは表示しない。
- workflowを追加しない。`schedule` triggerによる定期通知は採用しない（Alternatives参照）。警告はレビュー実行時にだけ出る。
- 運用チェックstepはcredentialを受け取らない。networkへもアクセスしない。modelは`validate` stepで検証済みのoutputだけを読む。
- 警告はレビューを停止しない。期限切れのcredentialは、それを使うstageで失敗として現れる（fail closed）。

### 3. 使用量の記録と費用上限

- Claudeのnormalized result（`run`）に、CLIが報告した`input_tokens`（非cache、cache作成、cache読込の合計）、`output_tokens`、`cost_usd`を記録する。報告がない場合や型が不正な場合は`null`とし、推測しない。
- 最終結果の`stages.claude` / `stages.codex`に同じ3項目を持たせる。Responses APIは費用を報告しないため、Codexの`cost_usd`は常に`null`である。
- 使用量と、適用中の上限（Claude budget、Claude max turns、Codex max output tokens）を**Job Summaryだけ**に表示する。PRコメントには表示しない。PRコメントの読者はPR作成者であり、運用上の費用情報は不要である。
- 上限値そのもの（`claude_max_budget_usd`、`claude_max_turns`、`codex_max_output_tokens`、各timeout）は変更しない。実運用のデータが蓄積されるまで調整の根拠がないためである。調整は、Summaryに記録された使用量を根拠にして行う。調整は「architectureを変えない軽微な上限調整」であり、ADRを必要としない。
- 最終結果とnormalized resultの形が変わるため、`FRAMEWORK_VERSION`を`0.4.0`へ上げる。stage間のartifactは同一run内でだけ受け渡されるので、互換性の問題は生じない。

## Rationale

3つとも、「失敗はすでにfail closedだが、失敗してから気付く」という問題への対処である。いずれも警告と記録に留め、レビューの成否を変えない。これにより、trust boundaryとfail closedの性質（ADR-0006）に影響を与えずに運用性だけを上げられる。

期限日をrepository variableにしたのは、次の理由による。

- GitHubにはSecretの期限を参照する手段がない。
- 期限日はSecretではない。variableとして扱えば、コミットなしに更新できる。
- `vars`コンテキストはworkflowから直接参照でき、追加のcredentialを必要としない。

確認日をコードに持たせたのは、allowlist自体がコード（`models.py`）であり、確認日の更新をallowlistの更新と同じレビューに乗せるためである。

## Alternatives Considered

- **`schedule` triggerの別workflowで定期通知する**: 実行しなくても通知が届く利点がある。一方で、「中央repositoryは`workflow_dispatch`のworkflowを1本だけ提供する」（AGENTS.md、ADR-0005）に反し、入口の検証範囲が増える。今回は採用しない。
- **期限切れのcredentialやstaleなmodelで実行を停止する**: 期限日はoperatorが手で入れる値であり、誤設定でレビュー全体が止まる。期限切れは使用stageで確実に失敗するので、停止による安全性の上積みはない。採用しない。
- **期限日を中央repositoryのファイルに置く**: 更新にcommitが必要になる。rotationのたびにPRを通す運用は重い。採用しない。
- **token価格表を持ちCodexの費用を推計する**: 価格は公式情報で変わりうる。未確認の値をコードへ持ち込むことになる。採用しない。token数だけを記録する。
- **使用量をPRコメントにも表示する**: fork PRを含め、PR作成者に運用情報を見せる必要がない。採用しない。
- **GitHub Appへの移行**: 今回の範囲外とした。ADR-0008の決定を維持する。

## Consequences

- operatorは、Secretを更新するたびに対応するrepository variableの期限日も更新する必要がある。未設定のままでもレビューは動くが、期限監視は行われない。
- Claudeのallowlist 2件は確認日が未記録であるため、公式docsで再確認して記録するまで、毎回の実行で警告が出る。
- OpenAIのallowlistは2026-09-21に確認済みである。90日を超える2026-12-21以降に再確認の警告が出る。
- 警告はレビュー実行時にしか出ない。長期間実行がない場合、期限切れに気付くのは次回実行時になる。
- Job Summaryに使用量が残るため、上限値の調整根拠を得られる。上限値自体は本ADRでは変更しない。
- 最終結果のstageに3項目が増える。`schemas/final-review.schema.json`、`scripts/lib/result.py`、テストがこれに追従する。

## References

- `docs/plan/two-stage-cross-review-plan.md` 5章 Phase 6
- `scripts/lib/models.py`、`scripts/lib/limits.py`、`scripts/check-operations.py`
- `scripts/normalize-review.py`、`scripts/finalize-review.py`、`scripts/lib/result.py`、`scripts/lib/render.py`
- `schemas/final-review.schema.json`
- `.github/workflows/cross-review.yml`、`actions/review-runtime/action.yml`
- GitHub Actions: `vars`コンテキストによるconfiguration variablesの参照（公式docs）
- GitHub Actions: workflow commandsの`::warning::`とjob summary（`GITHUB_STEP_SUMMARY`）（公式docs）
- ADR-0005、ADR-0006、ADR-0007、ADR-0008
