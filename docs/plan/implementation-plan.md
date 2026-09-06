# AI PR Review基盤 実装Plan

## 1. 文書の位置付け

本書は、GitHub Actions、Claude Code、Codex、OpenCodeを利用した汎用AI PRレビュー基盤の承認済み実装Planである。

本Planの承認前に実装を開始しない。各Phaseは、実装、Reviewerによるレビュー、修正、確認後のcommit候補という単位で進める。commit・push・GitHub上の設定変更は、別途明示的に承認された場合だけ行う。

## 2. 現在のrepository

現状は次の構成である。

```text
ai-cross-pr-review/
├── README.md
├── AGENTS.md       # 既存・未追跡
└── src/
    └── main.ts     # 既存・未追跡。今回のレビュー基盤の対象外
```

既存のworkflow、依存管理、テスト、ADR、Agent固有設定はない。READMEと既存の未追跡ファイルは変更しない。

### 実装技術方針

- deterministicな処理はPython 3を第一候補とする。
- JSON、path、hash、subprocess、入出力などは可能な限りPython標準ライブラリで実装する。
- テストは標準ライブラリの`unittest`を第一候補とし、`python -m unittest discover -s tests`で実行できる構成にする。
- 外部dependencyは、標準ライブラリでは満たせない必要性と保守上の理由を確認した場合だけ追加する。初期MVPではruntime dependencyなしを目標とする。
- subprocessを使う場合、shell文字列を組み立てず、実行ファイルと引数をargv配列で渡す。`shell=True`は使用しない。
- Python 3の対応versionと実行方法は`pyproject.toml`で固定・文書化する。
- これはarchitectureではなくimplementation technologyの変更であり、単独ではADRを追加しない。runtime、依存、security boundary、運用方式を変更する場合だけ既存ADRを確認し、必要なら新ADRを作成する。

## 3. 推奨architecture

```text
Consumer repository
│
├── Normal CI
│   └── test / lint / build
│
└── workflow_dispatch(pr_number)
    └── Common reusable workflow @ full commit SHA
        │
        ├── prepare
        │   └── PR検証、SHA固定、diff取得、policy取得
        │
        ├── review
        │   └── Claude Codeによるread-onlyレビュー
        │
        └── publish
            └── 結果検証、stale確認、PRコメント投稿

Codex:
PR → GitHub上で @codex review
```

### 採用方針

- 通常CIとAI Reviewを分離する。
- Consumer側は`workflow_dispatch`のthin wrapperだけを持つ。
- 共通repository側は`workflow_call`のReusable Workflowを提供する。
- Reusable Workflowはfull commit SHAで固定する。
- `prepare`、`review`、`publish`を別jobにする。
- ClaudeにはGitHub write権限を渡さない。
- PublisherはAI結果を解釈せず、決定論的に検証・整形・投稿する。
- PRコード、PR由来workflow、依存script、hooks、Agent設定はcheckout・実行しない。
- CodexはActionsへ組み込まず、`@codex review`で独立した二次レビューを行う。
- Claudeのレビュー結果をCodexへ渡さない。
- Judge、Consensus、自動修正、automatic mergeはMVP対象外とする。

GitHub.comでは、Reusable Workflow内から`$/actions/review-runtime`を使い、calleeと同一commitの内部Actionをcheckoutなしで参照する。GHES対応は将来の別decisionとする。

## 4. Workflow設計

### Consumer側

予定ファイルは`.github/workflows/ai-review.yml`である。

- `workflow_dispatch`のみを公開する。
- inputは`pr_number`のみとする。
- 共通workflowをfull commit SHAで呼び出す。
- `secrets: inherit`は使用しない。
- 必要なSecretだけ明示的に渡す。
- `pr_number`は正の整数として検証する。

### 共通repository側

予定ファイルは`.github/workflows/claude-review.yml`である。`workflow_call`で`pr_number`と必要なClaude Secretだけを受け取る。

手動workflowはdefault branchに存在する必要がある。実行refもdefault branchに限定する。workflow自体を変更できる管理者は信頼対象として扱う。

## 5. Job権限

| Job | GitHub権限 | Claude Secret |
|---|---|---|
| `prepare` | `contents: read`、`pull-requests: read` | なし |
| `review` | `contents: read`、`pull-requests: read` | あり |
| `publish` | `pull-requests: write` | なし |

基本値は`permissions: {}`とし、必要jobだけ明示する。`contents: write`、`issues: write`、`actions: write`、MVPでの`id-token: write`は付与しない。

`pull-requests: write`はpublisherだけが利用する。AI結果から投稿先やAPI endpointを決定させない。

## 6. PR取得、SHA固定、diff制御

`prepare`で次を行う。

1. `pr_number`を検証する。
2. PR metadataを取得する。
3. base SHA、head SHA、merge-base SHAを記録する。
4. default branch側の`.github/ai-review.md`の取得元SHAを記録する。
5. changed filesを取得する。
6. 固定SHA間からdiffを生成する。
7. diff hash、framework version、schema versionを記録する。
8. AI入力bundleを作成する。

PR checkout、build、test、dependency installは行わない。bare Git環境で固定SHAを取得し、外部diff、textconv、hook、submodule、LFSを無効化する。filenameはshell文字列へ連結せず固定argvで処理する。credential、`.env*`、state等はpatch生成前に除外する。

初期制限案はchanged files 100件、diff合計200 KiB、1ファイル50 KiB、policy 16 KiB、metadata 8 KiB、結果32 KiB、findings 20件、Claude実行10分以内とする。上限超過時は「問題なし」と投稿せず停止する。binaryは対象外として表示する。

投稿直前にhead SHA、base SHA、PRのopen状態を再確認する。不一致またはclosed化した場合は投稿しない。自動再レビューは行わない。コメントには`Reviewed commit: <head SHA>`を含める。

## 7. Claude Code

公式`claude-code-action`を第一候補とする。ただし固定版で次の採用gateを満たすことを条件とする。

- bundle外を読めない。
- Bash、Edit、Write、network、subagent、MCPを使えない。
- GitHub write操作ができない。
- PR由来命令でtool権限を変更できない。
- raw prompt、tool結果、Secretを公開ログへ出さない。
- 構造化結果をschemaどおり取得できる。

Actionのpromptにはdiff全文やPR本文を埋め込まない。固定promptとbundleの固定pathだけを渡す。consumer repository全体をreview jobへ配置せず、`AGENTS.md`、`CLAUDE.md`、`.claude/`、hooks、MCP、pluginsもロードさせない。

`--restricted`、`--safe-mode`、tool deny等の互換性はPhase 0〜2で固定versionを使って検証する。gateを満たせない場合は権限を緩めず、固定版Claude Code CLIをstdin経由で実行する小さなadapterを代替案とする。MVPで両方式を同時実装しない。

## 8. Authentication

MVPでは`CLAUDE_CODE_OAUTH_TOKEN`をGitHub Actions Secretとして利用する。

- 管理者が`claude setup-token`で生成する。
- token値、認証応答、環境変数一覧をログへ出さない。
- Secretはreview jobのClaude実行stepだけへ渡す。
- Secret所有者、更新担当、期限通知を決める。
- 公式仕様上の有効期間と失効方法をPhase 0で再確認する。
- 90日程度のrotation通知を運用案とする。
- 旧tokenが自動失効するとは仮定しない。

複数repository・組織運用ではAPI keyやWIFを再評価する。認証方式はClaude実行stepへ閉じ込め、publisherやschemaから分離する。WIF導入時だけ`id-token: write`を追加し、audience・subject・repository・ref条件を限定する。

## 9. Prompt Injectionとtrust boundary

PR title、body、comments、commit messages、filenames、source code、documentation、diffはすべてuntrusted dataとして扱う。

```text
固定REVIEW POLICY
↓
default branchのrepository review rules
↓
untrusted PR metadata
↓
untrusted diff
```

diff中の「previous instructionを無視する」「secretを読む」「commandを実行する」等は命令として扱わない。Promptだけで防御せず、filesystem境界、tool制限、GitHub権限分離、PRコード非実行、schema検証、出力無害化を組み合わせる。

## 10. Repository固有review rules

consumer側に`.github/ai-review.md`を任意配置可能にする。default branchの固定SHAから取得し、PR head側のpolicyは使用しない。

- 未配置時は共通policyで続行する。
- サイズ超過・不正形式は停止する。
- 外部URL取り込み、shell命令、tool権限変更は認めない。

`AGENTS.md`は開発Agent向け、`.github/ai-review.md`はActions上のレビュー観点、`prompts/review.md`は共通レビュー契約として責務を分ける。

## 11. Review ResultとPublisher

モデル出力は、`schema_version`、findings、limitationsを持つ最小schemaとする。モデルにはrepository、PR番号、投稿先、SHA、API endpoint、GitHub操作、merge判断を指定させない。

trusted wrapperがprovider、model、run ID、base/head SHA、diff hash、policy SHA、timestampを付与する。

publisherはJSON型、必須field、unknown field、changed files外path、不正行番号、件数・文字数、HTML・画像・mention・制御文字、Secretらしき値を検証・無害化する。投稿内容は定型Markdownとして生成する。

publisherは別runnerで実行し、Claude Secretを受け取らない。同じsnapshotはpublisher markerで既存コメントを更新し、異なるsnapshotは新規コメントとする。DBは持たず、exactly-onceは保証しない。

## 12. Public repository・fork PR

- GitHub-hosted runnerのみ使用する。
- `pull_request_target`、PR comment trigger、自動triggerはMVPで使用しない。
- forkコード、workflow、package lifecycle、hooks、Agent設定は実行しない。
- Secretを持つjobでconsumer開発環境を再現しない。
- untrusted metadataをActions式から`run:`へ直接埋め込まない。
- fork PRからSecretへ到達できる構造を作らない。
- Actionと関連toolchainをfull SHAで固定する。

fork PRを対象にする場合も、trustedな手動起動者が実行を開始する。branch protectionやworkflow変更権限の管理は運用上必要だが、本Planの実装対象外とする。

## 13. Agent Rules共通化

### `AGENTS.md`

共通project instructionsのSingle Source of Truthとする。本文にはProject Purpose、Architecture Principles、Agent役割、Development Workflow、Security、Git/GitHub restrictions、Testing、AI Review trust boundaries、ADR運用、Definition of Doneを記載する。

重要な常時適用ルールは本文に直接置く。詳細は`.ai/rules/security.md`、`.ai/rules/review.md`へ分離できるが、参照先が自動ロードされるとは仮定しない。

### `CLAUDE.md`

symbolic linkではなくthin adapterとする。共通ルールを複製せず、次だけを記載する。

1. `AGENTS.md`を読む。
2. `AGENTS.md`に従う。
3. Claude固有permissionは`.claude/`で管理する。

### Agent固有設定

| Agent | 設定 | 役割 |
|---|---|---|
| Codex | `.codex/config.toml`、`.codex/rules/safety.rules` | Owner / Implementer |
| Claude Code | `.claude/settings.json` | 設計・コード・security Reviewer |
| OpenCode | rootの`opencode.json` | local LLMによるAuxiliary Reviewer |

Claudeは`allow`、`ask`、`deny`を使い、push、merge、close、destructive操作、credential読み取り、secret出力を拒否またはapproval対象とする。

OpenCodeは固定したV2仕様の`permissions`配列を使う。shell、edit、network、subagentは既定で許可しない。未確認のV1形式や架空のpermissionキーは作らない。

Codexは公式sandbox、approval、rules機構を使う。通常開発のworkspace内編集は可能にし、外部操作・危険shell・GitHub破壊操作はapprovalまたは禁止とする。

Agent固有設定は「ルールの意味」を複製せず、各Agentの強制方法だけを定義する。Claude、Codex、OpenCodeへ同一レビューを重複依頼しない。

## 14. Codex独立レビュー

CodexはActionsへ組み込まず、必要なPRでGitHub上から`@codex review`を実行する。Claude結果は事前に渡さない。native連携だけで厳密なblind reviewを保証するとは扱わない。

厳密なblind性が必要になった場合は、Claude結果の投稿前にCodexを実行する運用や専用harnessを別途検討する。Judgeや自動ConsensusはMVP外とする。

## 15. Directory Structure

```text
ai-cross-pr-review/
├── AGENTS.md
├── CLAUDE.md
├── .ai/
│   └── rules/
│       ├── security.md
│       └── review.md
├── .claude/
│   └── settings.json
├── .codex/
│   ├── config.toml
│   └── rules/
│       └── safety.rules
├── opencode.json
├── .github/
│   ├── ai-review.md
│   └── workflows/
│       ├── ci.yml
│       ├── ai-review.yml
│       └── claude-review.yml
├── actions/
│   └── review-runtime/
│       └── action.yml
├── prompts/
│   └── review.md
├── schemas/
│   └── review-result.schema.json
├── scripts/
│   ├── prepare-review.py
│   ├── normalize-review.py
│   ├── publish-review.py
│   └── lib/
│       ├── github.py
│       ├── diff.py
│       ├── limits.py
│       └── render-review.py
├── tests/
│   ├── test_prepare_review.py
│   ├── test_review_result.py
│   ├── test_publish_review.py
│   ├── test_workflow_policy.py
│   └── fixtures/
├── docs/
│   └── adr/
│       ├── 0001-manual-reusable-ai-review.md
│       ├── 0002-review-trust-boundaries.md
│       ├── 0003-canonical-agent-instructions.md
│       └── 0004-review-authentication.md
├── pyproject.toml
├── .gitignore
└── README.md
```

READMEと既存の`src/main.ts`は変更しない。`src/main.ts`はレビュー基盤のPython実装とは独立した既存資産であり、最終構成図には含めない。`docs/adr/README.md`、commands、skills、agents、subagents、DB、external stateは作成しない。

## 16. ADR運用

### 作成基準

次をADR対象とする。

- workflow trigger、Reusable Workflow構成の変更
- Claude、Codex、OpenCodeの責務変更
- AIとpublisherのtrust boundary変更
- 認証方式変更
- fork PR security model変更
- SHA固定・diff取得方式変更
- AGENTS、CLAUDE adapter、Agent permission方針変更
- Judge、自動merge、inline comment等のMVP範囲変更

単純な関数分割、命名、formatter、通常のtest追加、architectureを変えない軽微な上限調整はADRにしない。

### Format

```markdown
# ADR-NNNN: Title

## Status

Proposed / Accepted / Superseded / Deprecated

## Context

## Decision

## Rationale

## Alternatives Considered

## Consequences

## References
```

### Numberingと更新

- `0001`から連番にする。
- 番号を再利用しない。
- Accepted ADRの理由を後から書き換えない。
- Accepted ADRを変更する場合は新しいADRを作る。
- 新ADRに`Supersedes ADR-XXXX`を記載する。
- 旧ADRは`Superseded`とし、後継ADRへのリンクを追加する。
- 新規decisionは原則Proposedで作成し、承認後Acceptedにする。

Agentはarchitecture変更前に`docs/adr/`の関連ADRを確認する。Accepted ADRと矛盾する場合は、Plan、Decision、新ADRの順で明示する。基本順序は`Plan → Decision → ADR → Implementation`とする。

### 初期ADR候補

1. `0001-manual-reusable-ai-review.md`：manual trigger、通常CI分離、Reusable Workflow、Claude一次、Codex別経路、GitHub.com対象。
2. `0002-review-trust-boundaries.md`：3 job分離、fork、SHA固定、stale非投稿、AI write禁止、Action採用gate。
3. `0003-canonical-agent-instructions.md`：AGENTS SSOT、CLAUDE thin adapter、symlink不採用、Agent固有permission、承認済みPlan/ADRのdocumentation例外。
4. `0004-review-authentication.md`：OAuth pilot、rotation、API key・WIFへの将来切り替え。

Python 3、標準ライブラリ、`unittest`、argv配列を採用する判断は実装技術の選択であり、architecture decisionではないため初期ADRにはしない。外部dependencyの追加や実行境界の変更を伴う場合は、必要性を評価してADR化する。

## 17. Testing方針

deterministicな境界を中心に自動テストする。

- workflow syntax、full SHA pin、job permissions、Secret明示
- invalid、nonexistent、closed PR
- fork PR
- 巨大diff、巨大file、binary、rename、特殊filename
- policy未配置、policy超過、PR head側policyの無視
- Prompt Injection、tool起動要求、credential読み取り要求
- head/base SHA変更、merge-base失敗
- Claude failure、timeout、authentication failure、schema不正、過大出力
- publisher failure、stale、偽artifact、偽marker、重複実行
- HTML、画像、mention、制御文字、secretらしき出力
- 別consumer repositoryからのworkflow再利用

Python `unittest`でdeterministicな境界を自動テストする。API・Claude出力はmock化する。危険操作は実行せず、架空のcanary値でログ漏洩を検証する。実Secret・実PRへの投稿は、承認済みtest repositoryで人間が別途実施する。

## 18. Phase別実装計画

### Phase 0：Rules・version・互換性の確定

- **Goal**：共通規則、Agent固有設定、公式仕様の適用範囲を確定する。
- **実装するもの**：AGENTS SSOT、CLAUDE thin adapter、Claude/OpenCode/Codex設定の最小骨格、Python 3 test入口（`unittest`）、Python version固定方針。
- **実装しないもの**：Claude実行、認証登録、PR取得、コメント投稿、README変更。
- **作成・変更予定ファイル**：`AGENTS.md`、`CLAUDE.md`、`.ai/rules/*`、`.claude/settings.json`、`.codex/*`、`opencode.json`、`pyproject.toml`、`.gitignore`。
- **作成・更新するADR**：0001、0003。0002、0004はProposedとして整理する。
- **Security considerations**：既存禁止事項を維持し、未確認のpermission設定を防御策として扱わない。認証情報を設定fileへ保存しない。
- **Tests**：`unittest`による設定schema、permission semantics、AGENTS参照、危険操作deny、通常開発操作の過剰阻害。
- **Completion criteria**：versionと検証方法が確定し、設計判断がADRへ反映される。

### Phase 1：PR snapshot・diff取得

- **Goal**：AIなしで固定SHAのレビューbundleを生成する。
- **実装するもの**：PR検証、head/base固定、merge-base、policy取得、diff制限、binary・禁止path処理。
- **実装しないもの**：Claude、GitHub投稿、PR checkout、build、dependency install。
- **作成・変更予定ファイル**：`scripts/prepare-review.py`、`scripts/lib/github.py`、`scripts/lib/diff.py`、`scripts/lib/limits.py`、`actions/review-runtime/action.yml`、`tests/test_prepare_review.py`、fixtures。
- **作成・更新するADR**：0002。
- **Security considerations**：特殊filename、shell injection、無制限fetch、PR由来workflow、credential path、巨大入力を制御する。subprocessはargv配列のみを使い、shell文字列と`shell=True`を禁止する。
- **Tests**：`unittest`でinvalid/nonexistent/closed PR、fork、巨大diff、binary、head変更、base変更、policy未配置、policy超過、argv境界を検証する。
- **Completion criteria**：同一snapshotから再現可能なbundleを生成し、不完全な取得時に停止する。

### Phase 2：Claude read-only review

- **Goal**：GitHub write権限なしで構造化レビュー結果を得る。
- **実装するもの**：review job、固定prompt、Claude Action、tool制限、schema、結果正規化。
- **実装しないもの**：コメント投稿、Codex連携、Judge、inline comment。
- **作成・変更予定ファイル**：`.github/workflows/claude-review.yml`、`prompts/review.md`、`schemas/review-result.schema.json`、`scripts/normalize-review.py`、tests。
- **作成・更新するADR**：0002、0004。
- **Security considerations**：read-only token、step限定Secret、bundle外読取拒否、MCP無効化、raw log禁止、Prompt Injection対策。
- **Tests**：Injection、bundle外アクセス、tool追加要求、Claude失敗、timeout、認証失敗、schema不正、ログcanary。
- **Completion criteria**：Action採用gateを満たす。満たさない場合はCLI adapter案を提示し、承認まで停止する。

### Phase 3：Deterministic Publisher

- **Goal**：正しいPR・SHAに対して、検証済み結果だけを投稿する。
- **実装するもの**：publish job、stale判定、定型Markdown、marker、重複抑制、限定retry。
- **実装しないもの**：merge、push、commit、automatic fix、DB、external state。
- **作成・変更予定ファイル**：`scripts/publish-review.py`、`scripts/lib/render-review.py`、publish tests、`claude-review.yml`。
- **作成・更新するADR**：0001、0002。
- **Security considerations**：publisherへClaude Secretを渡さず、AI結果から投稿先・API endpointを決めない。
- **Tests**：stale、closed PR、偽artifact、偽marker、API失敗、重複実行、HTML・mention注入。
- **Completion criteria**：mockで投稿境界を検証し、stale結果が投稿されない。

### Phase 4：Consumer wrapper・通常CI・統合検証

- **Goal**：別repositoryへ薄く導入できるMVPにする。
- **実装するもの**：`workflow_dispatch` wrapper、通常CI、workflow静的検証、別consumer接続確認。
- **実装しないもの**：自動trigger、Judge、Codex Actions統合、GHES対応、追加provider。
- **作成・変更予定ファイル**：`.github/workflows/ai-review.yml`、`.github/workflows/ci.yml`、`tests/test_workflow_policy.py`、必要なfixtures。
- **作成・更新するADR**：ADRのStatus確定。architecture変更時のみ後継ADRを作成する。
- **Security considerations**：full SHA固定、明示Secret、forkでのSecret exposure防止、artifact・ログ確認。
- **Tests**：workflow syntax、permission、full SHA、別consumer、fork、認証失敗、publisher失敗、missing policy。
- **Completion criteria**：thin wrapperとSecret設定だけで別repositoryへ導入できる。

## 19. MVP除外と将来拡張

### MVPで実装しないもの

- Judge、Consensus
- Claude/Codex自動統合
- Actions内Codex
- PR comment trigger、自動trigger
- automatic fix、automatic merge
- inline review comments
- 複雑なseverity policy、review_type
- database、external state
- 大量のcommands、skills、agents、subagents
- self-hosted runner、GHES対応

### 将来候補

- API keyまたはWIF
- 追加provider
- repository別diff設定
- 必要性が確認された場合のJudge
- strict blind review用harness
- GHES向け共通Action配布方式

## 20. 残る懸念

- 固定版`claude-code-action`でOAuth、restricted filesystem、structured output、tool denyが同時成立するかはPhase 0〜2で実証する。
- OAuth tokenの失効・更新責任はAnthropic仕様と組織運用の確認が必要である。
- `@codex review`は厳密なblind reviewを保証しない。
- GitHub tokenはcomment-onlyではないため、publisher実装の監査が必要である。
- 巨大diff拒否により完全性は下がるが、context overflow、費用、攻撃面を抑えるためMVPでは停止を優先する。
- AI出力とdiffのprovider送信に残余情報漏洩リスクがある。
- 既存の未追跡`AGENTS.md`と`src/main.ts`は、実装Phaseで内容を確認して保持する。
