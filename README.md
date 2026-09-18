# ai-cross-pr-review

GitHub Actions上でClaude CodeによるPull Requestレビューを実行する、再利用可能なレビュー基盤である。

Consumer repositoryには手動起動用のthin wrapperだけを配置する。レビュー本体は本repositoryのReusable Workflowが担当し、PRコードを実行せず、AIへGitHubの書き込み権限を渡さない。

Phase 0〜4のMVPを実装済みである。

## 仕組み

```text

Consumer repository

└── workflow_dispatch(pr_number)

    └── Reusable Workflow @ full commit SHA

        ├── prepare  PR検証、SHA固定、diff生成

        ├── review   Claude Codeによるread-onlyレビュー

        └── publish  結果検証、stale確認、PRコメント投稿

```

- `prepare`はPRのhead・base・merge-baseを固定し、レビュー用bundleを生成する

- `reviewpermissions: {}`で実行し、GitHubへの書き込み権限を持たない

- `publish`だけ`pull-requests: write`を持ち、Claudeのcredentialは受け取らない

- Codexの二次レビューはActionsへ統合せず、GitHub上`@codex review`で独立して実行する

## セキュリティ

- AI Reviewは手動起動とし、通常CIから分離する

- PRのコード、workflow、hook、依存scriptをcheckout・実行しない

- PR metadata、filename、documentation、diffをuntrusted dataとして扱う

- AI出力をschema検証・無害化し、投稿先や対象SHAをAIに決定させない

- 投稿前にPRがopenであり、head・base SHAが変わっていないことを再確認する

- Claude Code CLI、installer、外部Actionを固定versionまたはfull SHAで使用する

- credentialに該当するpathはdiffから除外し、binaryファイルは内容をAIへ渡さない

防御をpromptだけに依存させず、権限分離、tool制限、入力上限、hash検証、出力無害化を組み合わせている。

## 導入

### 1. Secretを登録する

Claude Code OAuth tokenを生成する。

```bash

claude setup-token

```

Consumer repositoryのActions Secretへ`CLAUDE_CODE_OAUTH_TOKEN`という名前で登録する。

### 2. Wrapperを配置する

`.github/workflows/ai-review.yml`](.github/workflows/ai-review.yml)をConsumer repositoryのdefault branchへコピーする。

Reusable Workflowの参照先は、監査済みのfull commit SHAで固定する。branchやtagは使用せず`secrets: inherit`も指定しない。

wrapperにはdefault branchとPR番号を検証する処理が含まれるため、単純なworkflowへ置き換えないこと。

### 3. レビューを実行する

GitHub Actionsの「AI Review」から対象PR番号を指定して実行する。

手動workflowはdefault branchから実行する。レビュー結果は検証後、対象PRへ定型コメントとして投稿される。

実行中にPRのheadまたはbaseが変わった場合、結果は投稿されない。

### 4. Repository固有のルールを追加する

必要に応じて、Consumer repositoryのdefault branch`.github/[ai-review.md](http://ai-review.md)`を配置する。

このファイルはdefault branchの固定SHAから取得され、PR側の同名ファイルは使用されない。未配置の場合は共通policyだけでレビューする。

## 入力制限

主な上限は次のとおり。

- 変更ファイル数: 100

- diff合計: 200 KiB

- 1ファイルのdiff: 50 KiB

- findings: 20件

- Claude実行時間: 600秒

上限超過時は部分的なレビューを行わず、処理を停止する。

詳細は`scripts/lib/[limits.py](http://limits.py)`](scripts/lib/[limits.py](http://limits.py))を参照。

## 開発

Python

