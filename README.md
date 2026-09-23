# ai-cross-pr-review

GitHub Actions 上で、Claude が初期レビューを行い、Codex が同一スナップショットを再検証する二段階の PR レビュー基盤である。

レビュー対象 repository には何も追加しない。レビューは、信頼された非公開の**中央実行 repository**（本 repository）から手動起動する。PR のコードは checkout も実行もせず、AI に GitHub の書き込み権限を渡さない。

## 仕組み

```text
中央実行 repository（本 repository）
└── workflow_dispatch: AI Cross Review
    ├── validate_request  入力の allowlist 検証
    ├── prepare           対象 PR の snapshot を固定して取得（read token）
    ├── claude_review     一次レビュー（Claude、GitHub credential なし）
    ├── codex_review      再検証（Codex、GitHub credential なし）
    ├── finalize          検証・突合・無害化・整形
    ├── report            Job Summary に表示、artifact を保存
    └── comment           pr_comment のときだけ PR にコメント（comment token）

レビュー対象 repository
└── （workflow の追加は不要）
```

- Claude は PR の snapshot だけを根拠に、構造化された指摘を返す。
- Codex は同じ snapshot と Claude の結果を受け取り、Claude の各指摘を次のいずれかに分類する。
  - `adopted`: 採用
  - `duplicate`: 重複
  - `rejected`: 不採用（理由つき）
  - `deferred`: 判断保留
- さらに Codex は、Claude が見逃した問題を追加指摘し、情報不足を明示する。
- Claude の結果は untrusted data として扱い、命令としては扱わない。

## 出力モード

| モード | PR へのコメント | 必要な token |
|---|---|---|
| `summary_only`（既定） | しない。Job Summary と artifact に出す | read token のみ |
| `pr_comment` | Job Summary に加えて PR にコメントする | read token と comment token |

不完全、stale、schema 不正、AI 失敗のいずれかの場合は、PR に投稿しない。Job Summary と artifact には、失敗した stage と理由が表示される。**指摘が 0 件であることと、実行が失敗したことは、区別して表示される。**

最終結果では、次を識別できる。

- Codex が採用した Claude の指摘
- Codex が追加した指摘
- 判断保留
- 不採用となった Claude の指摘とその理由、重複と判定された指摘
- Claude と Codex それぞれの実行状態
- schema 検証、snapshot 検証、最終整形の状態
- 要求したモデルと、実際に使われたモデル

## 使い方

### 1. Secret を登録する

中央実行 repository の Actions Secret に、次の 4 つを登録する。値はここへ書かない。

| Secret 名 | 用途 | 渡される job |
|---|---|---|
| `AI_REVIEW_READ_TOKEN` | 対象 repository の metadata・contents・pull requests の read | `prepare` だけ |
| `AI_REVIEW_COMMENT_TOKEN` | 対象 repository の pull requests の write | `comment` だけ |
| `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code の認証（`claude setup-token` で発行） | `claude_review` だけ |
| `OPENAI_API_KEY` | OpenAI Responses API の認証 | `codex_review` だけ |

`summary_only` だけを使う場合、`AI_REVIEW_COMMENT_TOKEN` は不要である。

read token と comment token は、対象 repository に限定した fine-grained personal access token を別々に発行する。private repository を対象にする場合は、そのアクセスを付与する。組織の SSO が有効な場合は、SSO の認可も必要である。

中央 repository の `GITHUB_TOKEN` は、別の repository にはアクセスできない。そのため、対象 repository へのアクセスには上記の token を使う。

各 Secret の期限日は、中央 repository の repository variable（Secret ではない）に `YYYY-MM-DD` 形式で設定できる（任意）。設定すると、実行のたびに期限を確認し、期限切れや 30 日以内の期限を Job Summary と annotation で警告する。未設定の場合、期限は監視されない。Secret を更新したら、期限日も更新すること。

| Variable | 対象 |
|---|---|
| `AI_REVIEW_READ_TOKEN_EXPIRES_ON` | `AI_REVIEW_READ_TOKEN` |
| `AI_REVIEW_COMMENT_TOKEN_EXPIRES_ON` | `AI_REVIEW_COMMENT_TOKEN` |
| `AI_REVIEW_CLAUDE_TOKEN_EXPIRES_ON` | `CLAUDE_CODE_OAUTH_TOKEN` |
| `AI_REVIEW_OPENAI_KEY_EXPIRES_ON` | `OPENAI_API_KEY`（期限のない key には rotation 期日を設定する） |

### 2. 実行する

中央実行 repository の Actions から「AI Cross Review」を選び、次を指定して実行する。

| 入力 | 説明 |
|---|---|
| `target_repository` | 対象 repository（`owner/name`） |
| `pull_request` | PR 番号、または PR の URL |
| `output_mode` | `summary_only`（既定）または `pr_comment` |
| `claude_model` | 一次レビューのモデル（`claude-opus-5`、`claude-sonnet-5`） |
| `codex_model` | 再検証のモデル（`gpt-5.6-sol`、`gpt-6-astra`、`gpt-5.6-terra`、`gpt-5.6-luna`） |
| `claude_effort` / `codex_effort` | effort の水準（`low`、`medium`、`high`、`xhigh`、`max`） |
| `policy_path` | 対象 repository のレビュー方針の path（既定は `.github/ai-review.md`） |

モデル名は自由入力できない。許可されたモデルだけが、workflow の選択肢と実行時の検証の両方を通る。

### 3. 結果を確認する

- 実行の Job Summary に、最終結果が表示される。
- Job Summary には、各 stage の使用量（token 数、Claude の費用）と、適用中の上限も表示される。これらは PR コメントには載らない。
- Job Summary の「運用チェック」に、Secret の期限と、選択したモデルの公式ドキュメントでの確認日が表示される。警告が出てもレビューは止まらない。
- 詳細な Markdown と JSON は、`ai-review-final-*` という名前の artifact に保存される（保持 7 日）。
- `pr_comment` を選び、投稿できる状態のとき、対象 PR に定型のコメントが 1 件付く。同じ snapshot への再実行は、そのコメントを更新する。
- 実行中に PR の head または base が変わった場合、結果は投稿されない。

### 4. リポジトリ固有の方針を追加する（任意）

対象 repository の default branch に `.github/ai-review.md` を置くと、レビュー観点として使われる。PR 側の同名ファイルは使われない。

置かなかった場合は、この repository の `policies/default-review-policy.md` が使われ、結果に `policy_source: central_default` と記録される。壊れた方針（UTF-8 でない、空、上限超過）は、レビューを停止する。

## セキュリティ

- 手動起動だけで、入口は `workflow_dispatch` の 1 つである。`workflow_call` は提供しない。
- PR のコード、workflow、hook、依存 script を checkout も実行もしない。
- PR の title、本文、ファイル名、diff、Claude の結果を、命令としては扱わない。
- AI に、対象 repository、PR 番号、投稿先、SHA、API endpoint、merge 判断を決めさせない。
- AI の job は、対象 repository への credential を持たない。各 token が渡されるのは、上表の job だけである。
- 投稿は、決定論的な publisher（`comment` job）だけが行う。
- Claude の結果と Codex の結果は、prepare job が出力した snapshot の値と突合される。一致しない結果は、Codex の処理にも投稿にも使われない。
- 認証情報らしき値は出力から除去され、`.env` や鍵などの path、バイナリの内容は AI に渡されない。
- Claude Code CLI と外部 Action は、固定した version または full commit SHA で使う。Codex には tool を渡さず、応答を保存しない設定（`store: false`）で呼び出す。

### 運用上の注意

- 対象の PR の diff と、レビュー結果は、中央実行 repository の Job Summary と artifact に残る。中央 repository は非公開にし、閲覧できる人を、対象 repository を閲覧できる人と同等以下に保つこと。
- 対象 repository のコードは、Anthropic と OpenAI に送信される。対象組織の外部 AI 利用方針に従い、許可された repository だけを起動すること。
- モデルの ID は公式ドキュメントの記載から転記しており、実 API での動作確認はまだ行っていない。初回の実行で、モデルが受理されない場合がある。その場合は失敗として表示される。
- 各モデル ID には、公式ドキュメントで確認した日付を `scripts/lib/models.py` に記録している。確認日が未記録か 90 日を超えると、運用チェックが再確認を促す。再確認では、公式ドキュメント、`models.py`、workflow の選択肢、ADR の References、テストの順に更新する。

## 入力制限

- 変更ファイル数: 100
- diff の合計: 200 KiB
- 1 ファイルの diff: 50 KiB
- review policy: 16 KiB
- Claude の指摘: 20 件
- 再検証の指摘: 20 件
- Claude の実行時間: 600 秒、Codex の実行時間: 600 秒

上限を超えた場合は、部分的なレビューを行わず、処理を停止する。詳細は `scripts/lib/limits.py` を参照。

## 開発

Python 3.11 以上と標準ライブラリだけを使う。runtime の依存はない。

```bash
python3 -m unittest discover -s tests
```

テストでは、GitHub、Claude、OpenAI をすべて mock にしている。実際の Secret も、実際の PR も使わない。

設計の背景は `docs/plan/` の Plan と、`docs/adr/` の ADR にある。作業ルールは `AGENTS.md` にまとまっている。
