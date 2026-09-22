# ADR-0008: Cross-repository authentication and credential separation

## Status

Accepted

Supersedes ADR-0004

## Context

ADR-0004は、単一repository内でReusable Workflowを実行する前提で認証を決めた。`prepare`と`publish`は呼び出し元repositoryの`GITHUB_TOKEN`を使い、Claude認証は`CLAUDE_CODE_OAUTH_TOKEN`だけであった。

ADR-0005で中央実行方式へ移行したため、前提が成り立たなくなった。

- 中央実行repositoryで発行される`GITHUB_TOKEN`は、中央repositoryに対してしか権限を持たない。対象repositoryのPRを読むことも、コメントすることもできない。`permissions:`をどれだけ広げても、別repositoryへは届かない。
- Codexの追加により、OpenAIの認証情報が必要になった。
- 出力モードの導入により、投稿credentialが不要な実行が存在するようになった。

## Decision

### 対象repositoryへの認証

候補を比較する。

| 候補 | 長所 | 短所 |
|---|---|---|
| GitHub App installation token | 権限が細かい。tokenは1時間で失効。人ではなく組織に紐づく。repository単位でinstall範囲を制御できる | installation tokenの取得にJWT（RS256）署名が必要で、Python標準ライブラリだけでは実装できない。外部Action（`actions/create-github-app-token`）または追加dependencyが必要になる |
| fine-grained PAT | 追加実装・追加dependencyが不要。repository単位、permission単位で絞れる | 発行者個人に紐づく。有効期限とrotationが運用依存。組織SSOの認可が必要な場合がある |
| 組織標準方式（OIDC等） | 長期Secretを持たない | GitHub API自体に対する汎用のOIDC交換先がなく、結局App/PATへ帰着する |

**MVPの推奨方式はfine-grained PATの分離運用とする。** GitHub Appは将来候補とし、移行時は`actions/create-github-app-token`をfull commit SHAで固定し、`comment` jobだけで使う。

### credentialの分離

読み取り用認証とコメント投稿用認証を分離する。

- **read token**（`target_read_token`）: metadata read、contents read、pull requests read。`prepare` jobだけへ渡す。
- **comment token**（`target_comment_token`）: pull requests write。`comment` jobだけへ渡す。`summary_only`では`comment` job自体が実行されないため不要である。
- **Claude Secret**（`claude_code_oauth_token`）: `claude_review`のClaude実行stepだけへ渡す。
- **OpenAI Secret**（`openai_api_key`）: `codex_review`のCodex実行stepだけへ渡す。
- AI job（`claude_review`、`codex_review`）には対象repositoryへのcredentialを一切渡さない。write credentialは出力モードに関わらずAI処理へ到達しない。
- 全jobは中央repository自身のcheckoutのため`contents: read`を持つ。この`GITHUB_TOKEN`は中央repositoryにしか届かず、どのstepにも入力として渡さない。private repositoryのcheckoutには必要で、`permissions: {}`のままではcheckoutが失敗する。
- `secrets: inherit`を使わない。必要なSecretだけを明示的にstepへ渡す。
- token値、認証応答、環境変数一覧をログへ出さない。tokenはargv、設定file、bundle、artifactへ書かない。gitへ渡す場合は`GIT_ASKPASS`とsubprocess限定の環境変数だけを使う（ADR-0006から継承）。
- モデル出力とstderrは、ログ・artifactへ出す前にcredentialらしき値を除去する。渡したtoken値そのものも除去対象に含める。

### Claude認証

- ADR-0004の`CLAUDE_CODE_OAUTH_TOKEN`運用を継続する。`claude setup-token`で管理者が発行し、有効期間は公式記載どおり1年として扱う。旧tokenが自動失効するとは仮定せず、rotationは「新token発行 + 旧Secret削除」として運用する。
- 複数repository・組織運用が広がった場合は、API keyまたはWorkload Identity Federationを再評価する。WIF導入時だけ`id-token: write`を追加し、audience・subject・repository・ref条件を限定する。

### 運用上の明記事項

- **private repository**: read tokenに対象repositoryのアクセスを付与しない限り取得できない。付与範囲は対象repositoryに限定する。
- **組織SSO**: SSOが有効な組織のrepositoryを対象にする場合、PATはSSO authorizationを通す必要がある。未認可のtokenは404として観測されるため、「PRが存在しない」と誤解しないよう失敗メッセージで区別できるようにする。
- **GitHub Actions policy**: 対象repository側ではworkflowを実行しないため、対象側のActions policyは中央実行に影響しない。中央repository側のActions policy（許可するActionの範囲）は、full SHA固定の外部Actionだけを使う前提で設定する。
- **外部AI利用policy**: 対象repositoryのコードが外部AI provider（Anthropic、OpenAI）へ送信される。対象組織のpolicyで許可されたrepositoryだけを起動対象にする責任は、workflowを起動する人間が負う。allowlistによるrepository制限は本ADRの対象外とし、必要になった時点で別decisionとする。
- **fork PRのtrust boundary**: fork PRも`refs/pull/N/head`の固定SHAから読み取り専用で取得し、コードを実行しない。forkであることを結果に記録する。fork由来の内容はuntrustedとして扱う。fork元repositoryへの書き込み権限は一切持たない。
- **中央repository・Summary・artifactの閲覧権限**: 中央実行repositoryは非公開とする。対象PRのdiff、metadata、レビュー結果がJob Summaryとartifactに載るため、中央repositoryのread権限は、対象repositoryのread権限を持つ者と同等以下に保つ。
- **機密情報の取り扱い**: PR由来の機密情報を公開repositoryやartifactへ保存しない。raw provider responseはartifact化しない。完全なcontext bundleはjob間の受け渡し用に保持1日の短期artifactとしてだけ存在し、最終artifactには含めない（ADR-0006）。最終artifactの保持期間は7日以内とする。

## Rationale

read権限とwrite権限を別のcredentialに分けると、最も危険な操作（PRへの書き込み）が到達できるjobが1つだけになる。`summary_only`ではそのjob自体が存在しないため、権限は「使われない」のではなく「配られない」。

fine-grained PATを推奨するのは、standard libraryだけで完結し、full SHA固定の原則を崩さずに導入できるためである。GitHub Appはより望ましいが、installation token取得に外部Actionまたは追加dependencyが必要で、MVPの範囲では導入コストがtrust boundaryの改善を上回らない。

cross-repositoryでは`GITHUB_TOKEN`が使えないという制約を明記しておかないと、`permissions:`の調整で解決しようとする誤った修正を招く。この制約はpermissionsの設定では解消できない。

## Alternatives Considered

- 単一のPATでreadとwriteの両方を賄う: `prepare`にもwrite権限が渡り、AI入力を作るjobが書き込み可能になるため採用しない。
- 対象repositoryに中央実行用のworkflowとSecretを置く: ADR-0005の前提（対象側へworkflowを追加しない）に反する。
- GitHub Appを最初から採用する: 外部Action依存または`cryptography`依存が必要になる。将来候補として残す。
- `secrets: inherit`で一括して渡す: Secretの到達範囲が全jobへ広がるため採用しない。
- Claude認証をAPI keyへ同時移行する: 認証方式の変更と構成変更を同時に行うと、失敗時の切り分けができなくなるため採用しない。
- PR由来の機密情報を含むbundle全体をartifactに残して監査する: 保存範囲が広がりすぎるため採用しない。

## Consequences

- Secretは4種類になる（read token、comment token、Claude token、OpenAI key）。所有者、更新担当、期限通知を定める運用が必要になる。fine-grained PATは期限が明示的であるため、期限切れは失敗として表面化する。
- read tokenの権限を対象repositoryごとに追加する運用が発生する。対象を増やす作業は「tokenのrepository範囲を広げる」ことに集約される。
- `summary_only`での実行は、comment tokenが未設定でも成功する。導入初期はcomment tokenなしで運用を開始できる。
- 組織SSO環境では、PATの認可切れがレビュー失敗として現れる。失敗メッセージで認可問題とPR不在を区別できるようにする必要がある。
- GitHub Appへの移行時は、`comment` jobへのtoken供給方法だけが変わり、job構成と権限分離は変えずに済む。

## References

- `docs/plan/two-stage-cross-review-plan.md` 4.6節
- `.github/workflows/cross-review.yml`、`actions/review-runtime/action.yml`
- `scripts/prepare-review.py`、`scripts/publish-review.py`、`scripts/lib/github.py`
- GitHub Actions: 自動生成される`GITHUB_TOKEN`は当該repositoryを対象とする（公式docs）
- Claude Code authentication docs（「Generate a long-lived token」）
- ADR-0004（Superseded）、ADR-0005、ADR-0006、ADR-0007
