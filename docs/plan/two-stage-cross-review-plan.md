# 二段階AI PRレビュー基盤 実装Plan（中央実行方式）

## 1. 文書の位置付け

本書は、`docs/plan/implementation-plan.md`（Phase 0〜4のMVP、実装済み）に続く承認済み実装Planである。旧Planを削除・改変せず、本書が旧Planの3章（architecture）、7章（Claude Code）、8章（Authentication）、10章（repository固有rules）、11章（Review ResultとPublisher）、14章（Codex独立レビュー）を置き換える。旧Planのその他の章（trust boundary、diff取得、テスト方針、ADR運用）は本書でも有効である。

本Planに対応するarchitecture decisionはADR-0005〜ADR-0009である。既存のADR-0001〜ADR-0004は後継ADRにより`Superseded`とする。

## 2. 目的

Claudeが初期PRレビューを行い、Codexが同一スナップショットを再検証する二段階レビューへ変更する。

- Claudeの結果はuntrusted dataとして扱う。
- CodexはClaudeの指摘を検証し、重要度・修正案・重複・誤検知を整理し、見逃しを追加指摘する。
- 情報不足は推測せず判断保留（deferred）とする。
- Codexの失敗、入力不足、schema不正を「問題なし」と扱わない。

## 3. 現在のrepositoryとの差分

現状（Phase 4時点）は次の構成である。

```text
Consumer repository
└── .github/workflows/ai-review.yml   # workflow_dispatch thin wrapper
        └── ktg6/ai-cross-pr-review/.github/workflows/claude-review.yml@<full SHA>
                ├── prepare
                ├── review    （Claude Code CLI）
                └── publish   （PRコメント投稿）
```

本Planでは次へ変更する。

```text
信頼された非公開の中央実行repository
└── .github/workflows/cross-review.yml   # workflow_dispatchのみ
        ├── validate_request   入力allowlist検証
        ├── prepare            対象repositoryのPR snapshot取得
        ├── claude_review      一次レビュー（Claude）
        ├── codex_review       再検証（Codex / OpenAI Responses API）
        ├── finalize           schema検証・snapshot一致検証・無害化・整形
        ├── report             Job Summary表示・artifact保存
        └── comment            条件付きPRコメント（pr_commentのみ）

対象repository
└── （workflowを追加しない）
```

対象repositoryへ`.github/workflows/ai-review.yml`を追加しない。対象repositoryに必要なのは、中央実行側の認証情報が対象repositoryへアクセスできることだけである。

## 4. 決定事項（Decision）

### 4.1 実行方式

- 中央実行repositoryに`workflow_dispatch`のworkflowを1本だけ置く。
- `workflow_call`は提供しない。入口を1つにすることで、別経路からallowlist検証を回避できないようにする。将来`workflow_call`を追加する場合も、`validate_request` jobを必須の前段とし、後続jobは`validate_request`の出力だけを入力にする。
- jobは`validate_request`、`prepare`、`claude_review`、`codex_review`、`finalize`、`report`、`comment`に分離する。
- AI jobにGitHub write権限を与えない。PRコメントは決定論的なpublisher（`comment` job）だけが行う。
- PRコード、PR由来workflow、hooks、Agent設定をcheckout・実行しない。
- 対象repository、PR番号、base/head SHA、投稿先、API endpoint、merge判断はAIに決定させない。

### 4.2 入力

`workflow_dispatch`の入力は次のとおりとする。

| 入力 | 型 | 既定 | 説明 |
|---|---|---|---|
| `target_repository` | string | なし | `owner/name` |
| `pull_request` | string | なし | PR番号またはPR URL |
| `output_mode` | choice | `summary_only` | `summary_only` / `pr_comment` |
| `claude_model` | choice | `claude-opus-5` | allowlist（`claude-opus-5`、`claude-sonnet-5`）。Haiku 4.5はeffort未対応のため除外 |
| `codex_model` | choice | `gpt-5.6-sol` | allowlist（公式Modelsドキュメントで確認したID）。実API呼び出しでの確認は未実施 |
| `claude_effort` | choice | `high` | Claude Code CLIのeffort |
| `codex_effort` | choice | `high` | Responses APIの`reasoning.effort` |
| `policy_path` | string | `.github/ai-review.md` | 対象repositoryのreview policy |

自由入力のモデル名は禁止する。`choice`のoptionsとPython側allowlistの二重で検証し、どちらか一方だけでは通過できないようにする。要求モデルと実使用モデルの双方を結果へ記録する。

`summary_only`を安全なデフォルトとする。

### 4.3 出力モード

**`summary_only`**

- PRへコメントしない。
- Job Summaryへ最終結果を表示する。
- 詳細Markdown / JSONをartifactへ保存する。
- PR write権限と投稿credentialを必要としない（`comment` jobがskipされ、comment tokenを参照するstepが実行されない）。

**`pr_comment`**

- Job Summaryへ表示する。
- 最終結果を対象PRへコメントする。
- AI処理へGitHub write credentialを渡さない。
- 投稿は決定論的publisherだけが行う。

不完全、stale、schema不正、AI失敗のいずれかである場合はPRへ投稿しない。Job Summaryとartifactには失敗状態を明示する。

### 4.4 snapshotとcontext

ClaudeとCodexへ同一の固定snapshotを渡す。snapshotはPhase 1の`prepare`が生成するbundleであり、base SHA、head SHA、merge-base、PR diff、変更ファイル情報、review policy、PR metadataを含む。

- GitHubから固定SHAを読み取り専用で取得する。取得範囲、ファイル数、サイズに上限を設ける（既存`Limits`を継続使用）。
- sensitive file除外、binary除外は既存の`forbidden_path_reason`と`diff`処理を継続使用する。
- PR由来の記述は命令として扱わない。境界マーカーとnonceで囲む方式を両モデルへ適用する。
- AIに関連ファイル選択を全面委任しない。bundleに含めるファイルは`prepare`が決定論的に決める。
- context不足はモデルに`limitations` / `insufficient_context`として報告させ、最終結果へ残す。
- Claude結果のsnapshot fingerprint（`snapshot_id`）を`codex_review`、`finalize`、`comment`の各段で検証する。異なるsnapshotの結果はCodex処理・投稿へ進めない。
- Claude/Codex間の参照情報一致検証を行う。Codexが参照するClaude findingのindex、path、lineは、snapshotのreviewable fileおよびClaude結果の範囲内でなければならない。
- stale PR検出は`prepare`（取得後の再確認）と`comment`（投稿直前の再確認）で行う。

review policyが対象repositoryに存在しない場合は、中央側の固定default policy（`policies/default-review-policy.md`）を使用し、`policy_source`（`repository` / `central_default`）と`policy_present`を結果へ記録する。壊れたpolicy（非UTF-8、NUL混入）や上限超過policyは失敗扱いとする。

### 4.5 Claude / Codex実行

**Claude**: 既存のCLI adapterを維持する。

- ADR-0002のAction採用gate検証結果（公式Actionはreview jobでGitHub tokenとcheckoutを要求するため不採用）は変更されていない。
- 直接API利用への移行も検討したが、今回は採用しない。理由は次のとおり。
  - 既存実装（version固定、digest照合、tool全無効、`--restricted` / `--safe-mode`、structured output）がADR-0002のgateを満たしており、移行は安全性の向上ではなく置き換えである。
  - Anthropic公式SDKを使う場合はruntime dependencyが発生し、「MVPではruntime dependencyなし」という方針と衝突する。標準ライブラリでMessages APIを直接叩く場合は、SDKが持つretry・エラー分類を自作することになる。
  - 認証方式が`CLAUDE_CODE_OAUTH_TOKEN`（subscription）から`ANTHROPIC_API_KEY`（Console）へ変わり、ADR-0004の運用前提を同時に変更することになる。
  - テスト容易性は現状でも確保されている（fake CLIをsubprocessで差し込む既存テスト）。
  - 費用・effort設定の公開範囲は、CLIの`--effort`とAPIの`output_config.effort`で同等である。
- したがってClaudeはCLI adapterのまま、モデルをallowlistから選ぶ形にする。API移行は将来のPhaseの候補として残す。

**Codex**: 実行手段を与えない構成とし、OpenAI Responses APIを直接使用する。

- `POST {base}/v1/responses`
- `tools: []`、`tool_choice: "none"`
- `text.format` に `{"type": "json_schema", "name": ..., "schema": ..., "strict": true}`
- `store: false`
- `reasoning: {"effort": <allowlist値>}`
- `max_output_tokens` を明示
- GitHub credentialを入力へ渡さない。
- PR由来データ、Claude結果はどちらも境界マーカー内のデータとして渡し、命令として扱わない。
- 応答の`status`が`completed`でない場合、`incomplete_details`がある場合、`refusal` content itemがある場合はいずれも失敗として扱う。

モデルIDは公式のModelsドキュメントで検証したものだけをallowlistへ入れる。ライフサイクル更新は「公式docsで確認 → allowlist更新 → ADRのReferences更新 → テスト更新」の順で行い、未確認のIDを追加しない。

### 4.6 認証と権限

中央実行方式では、中央repositoryの`GITHUB_TOKEN`は中央repositoryにしか権限を持たない。対象repositoryのPRを読む・コメントするには別の認証が必要である。この制約を前提に、次を比較して決定する。

| 候補 | 長所 | 短所 |
|---|---|---|
| GitHub App installation token | 権限が細かい、有効期限1時間、組織単位で管理できる | installation tokenの生成にJWT（RS256）署名が必要で、標準ライブラリだけでは実装できない。外部Actionまたは追加dependencyが要る |
| fine-grained PAT | 追加実装不要、repository単位・permission単位で絞れる | 人に紐づく。有効期限管理とrotationが運用依存 |
| 組織標準方式（OIDC等） | 長期Secretを持たない | GitHub APIに対する汎用的なOIDC交換先がなく、結局App/PATへ帰着する |

MVPの推奨方式は**fine-grained PATを2本に分離**する方式とする。GitHub Appは将来候補とし、導入時は`actions/create-github-app-token`をfull SHAで固定して`comment` jobだけで使う。

- read token: metadata read、contents read、pull requests read。`prepare` jobだけへ渡す。
- comment token: pull requests write。`comment` jobだけへ渡す。`summary_only`では不要。
- AI job（`claude_review`、`codex_review`）にはGitHub credentialを一切渡さない。
- Claude用Secretは`claude_review`のClaude実行stepだけへ渡す。
- OpenAI用Secretは`codex_review`のCodex実行stepだけへ渡す。

運用上の明記事項:

- private repositoryを対象にする場合、read tokenにそのrepositoryへのアクセスを付与する必要がある。
- 組織SSOが有効な場合、PATはSSO authorizationを通さないと対象repositoryへアクセスできない。
- 対象repositoryのGitHub Actions policyは中央実行に影響しない（対象側でworkflowを動かさないため）。逆に、対象組織の外部AI利用policyは遵守が必要であり、対象外repositoryを実行しないよう起動者が責任を持つ。
- fork PRのtrust boundary: fork PRも読み取りは`refs/pull/N/head`の固定SHAから行い、コードを実行しない。fork由来の内容はuntrustedとして扱う。fork PRであることは結果へ記録する。
- 中央repositoryは非公開とし、Summaryとartifactの閲覧権限は中央repositoryのread権限保持者に限られることを前提とする。対象PRの内容がSummary/artifactへ載るため、中央repositoryの閲覧範囲は対象repositoryの閲覧範囲と同等以下に保つ。
- raw provider responseはartifact化しない。完全なcontext bundleはjob間の受け渡しに必要な短期artifact（保持1日）に限り、最終artifactに残すのは正規化済み結果、最終Markdown/JSON、実行状態に限る（保持7日以内）。無制限にartifact化しない。
- 全jobは中央repository自身のcheckoutのため`contents: read`を持つ。この`GITHUB_TOKEN`は中央repositoryにしか届かず、stepへ渡さない。AI jobが受け取らないのは、対象repositoryへのcredentialである。

### 4.7 schemaと最終処理

Codex結果は、Claude指摘ごとの状態（`adopted` / `duplicate` / `rejected` / `deferred`）を必ず持つ。加えてCodex追加指摘、判断保留、情報不足、limitationsを持つ。実行状態、検証状態、snapshot fingerprint、要求モデル、実使用モデルはtrusted wrapperが付与する。

`finalize`は決定論的に次を行う。

1. schema検証（Claude結果、Codex結果の双方）
2. snapshot一致検証（両結果のfingerprintと`prepare`のjob outputの突合）
3. 参照整合性検証（Codexが参照するClaude indexとpathの実在確認）
4. 無害化（制御文字除去、credentialらしき値の除去、Markdown escape）
5. Markdown / JSON整形
6. Summary生成
7. artifact生成
8. 投稿可否（`publishable`）の決定

publisherはAI出力を解釈して投稿先や権限を決めない。投稿先は`validate_request`の出力、snapshotは`prepare`の出力から決まる。

最終結果は少なくとも次を識別可能にする。

- Claudeの指摘をCodexが採用したもの（adopted）
- Codexが追加した指摘（added）
- 判断保留（deferred）
- 不採用となったClaudeの指摘と理由（rejected / duplicate）
- Claude / Codexの実行状態
- schema検証・snapshot検証・最終整形の状態
- 要求モデルと実使用モデル

### 4.8 READMEとAGENTS

`AGENTS.md`をSingle Source of Truthとして先に更新し、Agent固有adapterやrulesを矛盾させない。README方針は次へ変更する。

- 利用者向け情報に実質的変更がある場合はREADME更新を許可する。
- README更新は常に必須ではない。
- Secret、credential、token、内部限定情報をREADMEへ書かない。
- 自動生成された大量docsの無条件追加は禁止する。
- docs生成禁止の範囲は「新しい解説ドキュメントのtreeを勝手に作らない」ことであり、`docs/plan/`の承認済みPlan、`docs/adr/`のADR、既存READMEの更新は対象外とする。
- 承認済みPlan / ADRの管理規則（連番、Supersede、理由の非改変）は維持する。

### 4.9 ローカルCLI

既存Python処理を再利用したローカルCLI（repositoryとPRを指定し、GitHubから取得し、Claude/Codexレビューを実行し、Markdown/JSONをローカル保存し、PRには投稿しない）は、**後続Phase 7へ分離する**。理由は次のとおり。

- 初回実装の変更面（workflow再構成、Codex追加、二段階統合、認証分離）が既に大きく、同時にローカル実行経路を足すとtrust boundaryのテスト対象が二重になる。
- ローカルCLIはオフライン実行ではない。GitHub（read token）、Anthropic（OAuth token）、OpenAI（API key）への通信と認証が必要であり、tokenの受け渡し規約（環境変数のみ、CLI引数禁止）を別途固める必要がある。
- `finalize`までの処理は既にscript単位で分離されているため、後から薄いentry pointを追加するコストは低い。先に中央実行経路を安定させる方が総コストが小さい。

Phase 7で実装する際の前提は次のとおり。

- tokenはCLI引数へ渡さず、環境変数（`AI_REVIEW_GITHUB_TOKEN`、`CLAUDE_CODE_OAUTH_TOKEN`、`OPENAI_API_KEY`）からのみ読む。
- 出力はローカルディレクトリへのMarkdown/JSON保存に限り、GitHubへの書き込み経路を持たない。
- `publish-review.py`をimportしない構成とし、投稿コードがローカル経路に存在しないことをテストで固定する。

## 5. Phase別実装計画

### Phase 5：中央実行・二段階レビュー（本Planの実装対象）

- **Goal**: 対象repositoryにworkflowを置かずに、Claude一次 + Codex再検証の結果をJob Summaryとartifactへ出し、`pr_comment`指定時だけ対象PRへ投稿する。
- **実装するもの**: `cross-review.yml`、`validate-request.py`、model allowlist（`lib/models.py`）、default policy fallback（`policies/default-review-policy.md`）、`run-codex-review.py`（`lib/openai_api.py`）、`normalize-codex-review.py`、`finalize-review.py`（`lib/result.py`）、`report-summary.py`、publisher更新、schema 2本、Codex用prompt、既存scriptのcross-repo対応。finalizeはprepare jobの出力（snapshot ID、head/base/merge-base SHA、diff hash、policy source、policy有無、fork有無）と各stage結果を突合する。
- **実装しないもの**: ローカルCLI、Judge、自動修正、automatic merge、inline review comments、GHES対応、自動trigger。
- **作成・更新するADR**: 0005、0006、0007、0008、0009（いずれもProposedで作成し、承認後Accepted）。0001〜0004はSuperseded。
- **Security considerations**: AI jobへGitHub credentialを渡さない、comment tokenを`comment` jobへ限定、Claude結果をuntrustedとして再検証、snapshot fingerprint不一致の遮断、失敗を「問題なし」に変換しない。
- **Tests**: 後述6章。
- **Completion criteria**: `python3 -m unittest discover -s tests`が成功し、6章の境界がすべてmockで検証されている。

### Phase 6：運用整備

- **Goal**: 失敗がfail closedになる性質を維持したまま、「失敗してから気付く」運用課題を警告と記録で先回りする。レビューの成否とtrust boundaryは変えない。
- **実装するもの**:
  - model allowlistのライフサイクル: `scripts/lib/models.py`の各IDに公式docsでの確認日と確認元を持たせる。選択modelの確認日が未記録、または90日を超えた場合にJob Summaryとannotationで警告する。確認日が不明なIDは推測で埋めない。
  - credential期限の警告: 期限日をrepository variable（`AI_REVIEW_READ_TOKEN_EXPIRES_ON`、`AI_REVIEW_COMMENT_TOKEN_EXPIRES_ON`、`AI_REVIEW_CLAUDE_TOKEN_EXPIRES_ON`、`AI_REVIEW_OPENAI_KEY_EXPIRES_ON`）で受け取る。`validate_request` job内の運用チェック（`scripts/check-operations.py`）が、期限切れと30日以内の期限を警告する。workflowは追加しない。
  - 費用上限の運用値調整の準備: Claudeの費用・token数を記録し、両stageの使用量と適用中の上限をJob Summaryだけに表示する。上限値は、実運用データが揃うまで変更しない。
- **実装しないもの**: GitHub App移行（ADR-0008の決定を維持）、`schedule` triggerによる定期通知、警告によるレビュー停止、token価格表による費用推計、上限値の変更。
- **作成するADR**: 0010（Proposedで作成し、承認後Accepted）。
- **Security considerations**: 運用チェックはcredentialを受け取らず、networkへもアクセスしない。期限日の形式不正時に値を表示しない。modelは検証済みoutputだけを読む。`vars`は`validate_request` jobにだけ渡す。使用量はPRコメントに載せない。
- **Tests**: 期限判定の境界（期限当日、警告窓、期限切れ、未設定、形式不正、値の非表示）、確認日の鮮度判定、allowlist entryの整合、警告がexit codeを変えないこと、`vars`の到達範囲、運用チェックstepがcredentialを持たないこと、使用量の記録（未報告・型不正は`null`）、使用量がSummaryに出てPRコメントに出ないこと。
- **Completion criteria**: `python3 -m unittest discover -s tests`が成功する。

上限値の調整は、Summaryに記録された使用量を根拠にして別途行う。これは「architectureを変えない軽微な上限調整」であり、ADRを必要としない。

### Phase 7：ローカルCLI

- **Goal**: trustedな利用者が手元で、中央実行と同じ処理（validate → prepare → Claude一次 → Codex再検証 → finalize）を実行し、最終Markdown / JSONをローカルディレクトリへ保存する。PRへは投稿しない。
- **実装するもの**:
  - entry point `scripts/review-local.py`。既存scriptの関数（`build_request`、`prepare`、`run_review`、`normalize`、`run_codex_review`、`finalize`、`report`）をprocess内で順に呼ぶ薄いorchestratorとする。検証・正規化・最終処理のロジックを複製しない。
  - token: `AI_REVIEW_GITHUB_TOKEN`（read、未設定なら未認証で取得）、`CLAUDE_CODE_OAUTH_TOKEN`、`OPENAI_API_KEY`を環境変数からだけ読む。CLI引数では受け取らない。各tokenは使用するstageだけへ渡す。`GITHUB_TOKEN`など他の環境変数は読まない。AI provider用の2つが未設定なら、network accessの前に停止する。
  - GitHub API: GET以外のmethodを拒否するread-only transport（`lib/github.py`の`read_only_transport`）を`prepare`へ渡す。
  - 出力: `--output-dir`（存在しないか空のdirectory。新規作成時はmode 0700、既存の場合はgroup・otherに権限があれば拒否）へ`final-review.json`と`review-summary.md`だけを書く。summaryはstdoutにも表示する。bundle、git作業領域、raw provider response、normalized resultは一時directoryに置き、終了時に削除する。
  - 実行状態: stageの失敗は中央実行と同じく`finalize`へ`failure` / `skipped`として渡し、「問題なし」に変換しない。Claudeが失敗した場合はCodexを実行しない。`prepare`の失敗時は最終結果を生成しない。exit codeは、最終結果が`publishable`（両stage成功かつ全検証通過）なら0、それ以外は2とする。
  - `request.output_mode`は`summary_only`に固定する。最終結果のschemaは変えない。
  - Claude CLIは中央実行と同じ固定version・release digest検証を通す。利用者は固定versionを手元に導入する。
- **実装しないもの**: PRへの投稿経路、`publish-review.py`のimport、tokenのCLI引数、GHES対応（API URL・server URLの指定）、運用チェック（credential期限・model確認日の警告）、結果のキャッシュ・再開、workflowの変更。
- **作成するADR**: 0011（Proposedで作成し、承認後Accepted）。
- **Security considerations**: ローカル経路にGitHub書き込みの呼び出しを置かない（静的検査とread-only transportの二重）。tokenはargvに現れない。AI stageへGitHub tokenを渡さず、Claude CLIの環境は既存の最小環境（利用者の`HOME`・設定を読まない）を使う。raw provider responseを残さない。PRコード・hooks・Agent設定を実行しない点は既存`prepare`のまま。
- **Tests**: 固定remote・fake GitHub・fake Claude CLI・fake Responses APIによるend-to-end、tokenの到達範囲（canary）、GitHub requestがGETだけであること、`publish-review.py`がimportされないこと、token用のCLI引数がないこと、AI tokenの事前検査、stage失敗時のexit codeと最終結果、出力directoryの拒否条件、出力に中間ファイルとtokenが残らないこと。
- **Completion criteria**: `python3 -m unittest discover -s tests`が成功する。

## 6. テスト方針

標準ライブラリの`unittest`だけを使う。GitHub API、Claude CLI、OpenAI APIはすべてmockする。実Secret、実PRへの投稿、危険操作、PRコードの実行は行わない。

検証する境界:

- GitHub APIのmock（PR取得、policy取得、コメント一覧・作成・更新）
- Claude CLIのmock（fake実行ファイル）
- OpenAI / Codex APIのmock（transport差し替え）
- `summary_only`でPR writeが発生しないこと
- `pr_comment`でもAI処理へwrite credentialが渡らないこと
- snapshot不一致のClaude結果をCodexやpublisherが受け付けないこと
- Codex失敗（HTTPエラー、`incomplete`、`refusal`、JSON不正）
- Claude失敗（非ゼロ終了、envelope error、tool denial）
- schema不正（未知field、enum違反、件数超過、型不正）
- stale PR（head移動、base移動、closed、merged、消失）
- context不足（`insufficient_context`が結果に残ること）
- model allowlist迂回（workflow choiceを回避した値、Python側での拒否）
- 対象repositoryにworkflowがなくても中央実行できること（対象repo側workflowを一切参照しないこと）
- fork PRのtrust boundary
- sensitive / binary除外
- policy未配置（central default採用）・policy不正（停止）
- 既存安全境界の維持（shell未使用、argv配列、third-party import不使用、full SHA固定、権限分離）

最終的に`python3 -m unittest discover -s tests`が成功することをDefinition of Doneとする。

## 7. 残る懸念

- fine-grained PATは人に紐づくため、退職・権限変更時の影響が大きい。GitHub App移行を運用課題として残す。
- Codexのモデルライフサイクルは外部都合で変わる。allowlistの更新漏れは「要求モデルが拒否される」形で失敗するため、安全側に倒れる。
- Claude結果をCodexへ渡す構成は、旧Planのblind review前提を放棄する。Codexが誤検知を追認する可能性があるため、Codexのpromptで「Claudeの主張を証拠なしに採用しない」ことを明示し、`rejected`の理由記載を必須にする。
- 中央repositoryのSummary/artifactに対象PRの内容が載るため、中央repositoryの閲覧権限管理が実質的なアクセス制御になる。
- exactly-onceは引き続き保証しない。runをまたぐ重複投稿は理論上起こりうる。
