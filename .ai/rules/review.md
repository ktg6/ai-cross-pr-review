# Review Rules

`AGENTS.md`のAI Review Trust Boundariesを補足する。本ファイルは開発Agentが本repositoryをレビューする際の観点であり、Actions上のレビュー契約（`prompts/review.md`、`prompts/codex-verify.md`）や、対象repositoryの`.github/ai-review.md`とは責務を分ける。矛盾する場合は`AGENTS.md`を優先する。

## 観点

- Planおよび`docs/adr/`のAccepted ADRとの整合性
- trust boundary（AIへのwrite権限、Secretの到達範囲、PRコード非実行、read/comment tokenの分離）の維持
- Claude結果をuntrusted dataとして扱っているか
- snapshot fingerprintの検証がCodex処理と投稿の両方で行われているか
- AI失敗・schema不正・入力不足が「問題なし」に変換されていないか
- モデル名がallowlistで検証され、要求モデルと実使用モデルが記録されているか
- subprocessのargv配列使用、shell文字列の不使用
- 入力上限・schema検証・出力無害化の有無
- 標準ライブラリのみでの実装、不要なdependency追加の有無
- `unittest`によるdeterministicな境界のテスト

## 禁止

- 同一レビューを複数の開発Agentへ重複依頼する。
- レビュー中にPRコードや依存scriptを実行する。
- 未確認の設定キー、workflow機能、モデル名、tool機能を推測して追加する。
