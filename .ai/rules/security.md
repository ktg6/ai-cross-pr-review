# Security Rules

`AGENTS.md`のSecurityおよびGit / GitHub Restrictionsを補足する。本ファイルが自動ロードされる前提を置かない。矛盾する場合は`AGENTS.md`を優先する。

## 禁止操作

- `git push`、`git reset --hard`、`git clean`、`git branch -D`
- `gh pr merge`、`gh pr close`、remote Git stateの変更
- `sudo`、`rm -rf`、`terraform apply`、`terraform destroy`
- `git commit`（明示的な承認がある場合を除く）

## 機密情報

- `~/.aws/*`、`~/.ssh/*`、`.env*`、`terraform.tfstate*`を読まない。
- Secret、credential、tokenを生成・表示・保存しない。
- 環境変数一覧や認証応答をログへ出さない。

## 設定ファイル

- 各Agentの設定は公式仕様で確認済みのキーだけを使う。
- 未確認のpermissionキーを防御策として扱わない。
- Agent設定にはルールの強制方法だけを書き、ルールの意味は`AGENTS.md`に置く。

## subprocess

- argv配列で実行する。`shell=True`とshell文字列の組み立てを禁止する。
- untrusted dataをshellやActions式へ直接埋め込まない。
