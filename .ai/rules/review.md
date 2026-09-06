# Review Rules

`AGENTS.md`のAI Review Trust Boundariesを補足する。本ファイルは開発Agentが本repositoryをレビューする際の観点であり、Actions上のレビュー契約（`prompts/review.md`、将来作成）や、consumer側の`.github/ai-review.md`とは責務を分ける。

## 観点

- Planおよび`docs/adr/`のAccepted ADRとの整合性
- trust boundary（AIへのwrite権限、Secretの到達範囲、PRコード非実行）の維持
- subprocessのargv配列使用、shell文字列の不使用
- 入力上限・schema検証・出力無害化の有無
- 標準ライブラリのみでの実装、不要なdependency追加の有無
- `unittest`によるdeterministicな境界のテスト

## 禁止

- Claudeのレビュー結果をCodexへ事前に渡す。
- 同一レビューを複数Agentへ重複依頼する。
- レビュー中にPRコードや依存scriptを実行する。
