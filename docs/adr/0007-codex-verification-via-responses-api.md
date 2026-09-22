# ADR-0007: Codex verification via the OpenAI Responses API

## Status

Accepted

## Context

ADR-0005で、Codexによる再検証を中央実行workflowの`codex_review` jobとして実装することを決めた。実行方式には次の候補があった。

1. Codex CLI（`codex`）をrunnerへ導入して実行する。
2. GitHub上の`@codex review`を利用する（ADR-0001の方式）。
3. OpenAI Responses APIを直接呼び出す。

また、モデル名を自由入力にすると、未検証のモデルや存在しないモデルが実行時に使われ、失敗の原因が入力にあるのか実装にあるのか判別できなくなる。要求モデルと実使用モデルの記録も必要である。

## Decision

### 実行方式

- Codexへ実行手段（shell、filesystem、network、MCP、subagent）を与えない構成を採用し、**OpenAI Responses APIを直接呼び出す**。CLIは導入しない。
- 呼び出しは`POST {base}/v1/responses`とし、次を満たす。
  - `tools: []` と `tool_choice: "none"` を明示的に送る。
  - `text.format` に `{"type": "json_schema", "name": ..., "schema": ..., "strict": true}` を指定する。schemaは`additionalProperties: false`で、すべてのpropertyを`required`に列挙する（strict modeの要件）。省略可能な値はnullableな型で表現する。
  - `store: false` を明示する。Responses APIの既定は保存有効であるため、省略しない。
  - `reasoning: {"effort": <allowlist値>}` を指定する。
  - `max_output_tokens` を明示し、出力サイズの上限を持つ。
  - GitHub credentialを入力へ渡さない。認証は`Authorization: Bearer <OPENAI_API_KEY>`だけを使う。
  - PR由来データとClaude結果はどちらも`input`のuser message内でnonce付き境界マーカーに囲み、命令ではなくデータとして扱う。固定のレビュー契約は`instructions`（中央側の`prompts/codex-verify.md`、calleeと同一commit）で渡す。
- 応答の扱いは次のとおりとする。いずれも失敗として停止し、「問題なし」に変換しない。
  - HTTPが200以外である。
  - `status`が`completed`以外である（`incomplete`の場合は`incomplete_details.reason`を状態として記録する）。
  - `output`に`refusal` content itemが含まれる。
  - `output`から`output_text`を取り出せない、またはJSONとして解釈できない。
  - structured outputがschemaに適合しない。
- HTTP通信は標準ライブラリ（`urllib`）で行い、transportを差し替え可能にしてテストではネットワークへ出ない。retryは冪等なリクエストに限り、上限回数を設ける。

### モデルallowlist

- モデル名の自由入力を禁止する。`workflow_dispatch`の`choice`と、Python側の固定allowlistの二重で検証する。`choice`だけでは`workflow_call`や将来の入口で迂回されうるため、Python側の検証を正とする。
- 初期allowlistは公式のModelsドキュメントで確認したIDだけとする。ここでいう「確認済み」は公式ドキュメント上のID・対応状況の確認であり、実APIを呼び出した動作確認は含まない。実行前に、対象IDが公式ドキュメントに載っていることを起動者が再確認する（Consequencesを参照）。
  - Codex（OpenAI）: `gpt-6-astra`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`。既定は`gpt-5.6-sol`。いずれも公式ModelsドキュメントでIDとResponses API対応を確認済み。
  - Claude: `claude-opus-5`、`claude-sonnet-5`。既定は`claude-opus-5`。`claude-haiku-4-5`はeffort設定を受け付けず、CLIの`--effort`との組み合わせも未確認のため含めない。
- effort / reasoning設定もallowlist化する。
  - Claude（CLIの`--effort`）: `low` / `medium` / `high` / `xhigh` / `max`。既定は`high`。
  - Codex（`reasoning.effort`）: `low` / `medium` / `high` / `xhigh` / `max`。既定は`high`。`none`は許可しない（検証タスクで推論を無効化しない）。
- 要求モデル（`model_requested`）と実使用モデル（`model_reported`）を結果へ記録する。実使用モデルはprovider応答の`model`フィールド（Responses APIのresponse object、Claude CLIのenvelope）から取得する。取得できない場合はnullとして記録し、推測しない。
- ライフサイクル更新は「公式docsでIDと対応状況を確認 → `scripts/lib/models.py`のallowlistを更新 → workflowの`choice` optionsを更新 → 本ADRのReferencesを更新 → テストを更新」の順で行う。未確認のIDを追加しない。

### 費用と認証の公開範囲

- 認証はOpenAI API key（`OPENAI_API_KEY`）をGitHub Actions Secretとして保持し、`codex_review`のCodex実行stepだけへ渡す。argv、設定file、bundle、artifact、ログへ出さない。
- 費用はモデル単価×トークン量で発生する。実行ごとのトークン使用量（`usage`）を結果へ記録し、Job Summaryに表示する。ハードな費用上限はAPI側に無いため、`max_output_tokens`と入力上限（既存の`Limits`）で間接的に抑える。
- Claude側の費用上限は既存のCLI `--max-budget-usd` を継続使用する。

### Claudeの実行方式（比較の結論）

Claudeも直接API利用を検討したが、今回は既存のCLI adapterを維持する。理由は`docs/plan/two-stage-cross-review-plan.md` 4.5節に記載した比較のとおりで、要点は次の3つである。

- 既存CLI adapterはADR-0002/0006のgateを満たしており、移行は安全性の向上にならない。
- 公式SDKはruntime dependencyを生み、標準ライブラリ直叩きはretry・エラー分類の自作を招く。
- 認証方式（subscription OAuth token）と運用（ADR-0008）を同時に変えることになる。

API移行は将来のPhaseの候補として残す。

## Rationale

Responses APIを直接使う構成は、Codexに実行手段を与えないという要件を構造で満たす。CLIを導入すると、CLIが持つtool・設定・自動更新の挙動を毎version検証する必要が生じ、攻撃面と検証コストが増える。

strict JSON Schemaは、再検証結果に必要な「Claude指摘ごとの状態」を欠落なく取得するための仕組みである。自由形式のテキストを後からparseする方式では、状態の欠落と誤読が避けられない。

`store: false`は、対象PRの内容とレビュー結果がprovider側に保存される範囲を減らす。既定が保存有効であるため、明示が必須である。

モデルallowlistを二重化するのは、`choice`がworkflow入口固有の仕組みであり、入口が増えたときに検証が失われるためである。Python側allowlistはどの入口からでも必ず通る。

## Alternatives Considered

- Codex CLIをrunnerへ導入する: 実行手段を持つ構成になり、tool制限・version固定・digest照合・設定無効化をClaude CLIと同様に自前で検証する必要がある。実行手段を与えない要件に反するため採用しない。
- `@codex review`を使う: 入力snapshotを固定できず、Claude結果との参照整合性を検証できない。出力形式も制御できないため採用しない。
- Chat Completions APIを使う: structured outputは利用できるが、`store`の扱いとreasoning設定の指定方法がResponses APIと異なり、公式が推奨する新しい経路はResponses APIであるため採用しない。
- モデル名を自由入力にしてAPI側のエラーに任せる: 失敗の原因が入力かAPIかを判別できず、未検証モデルの結果が投稿されうるため採用しない。
- 実使用モデルをallowlistの要求値で代用する: providerがエイリアスを解決した実体を記録できず、要求と実使用の差分が見えなくなるため採用しない。
- 公式SDK（`openai`パッケージ）を使う: runtime dependencyなしという方針に反する。標準ライブラリで足りる範囲の呼び出しであるため採用しない。

## Consequences

- OpenAI API keyという長期Secretが1つ増える。到達範囲は`codex_review`のCodex実行stepに限定する。
- providerのAPI仕様変更（field名、status値、structured outputの形）は`codex_review`の失敗として表面化する。失敗は「問題なし」に変換されないため、誤った成功にはならない。
- allowlistに無いモデルは実行できない。新モデルの利用には本ADRとコードの更新が必要になる。
- OpenAIのallowlist IDは公式Modelsドキュメントで確認したものだが、実APIでの呼び出しは本ADRの作成時点で未実施である。初回の実行では、モデルIDの不受理（HTTP 4xx）が`codex_review`の失敗として現れうる。その場合も「問題なし」にはならず、allowlist更新の契機になる。
- `store: false`のため、provider側のdashboardから過去のresponseを参照できない。デバッグは正規化済み結果とログの範囲で行う。
- reasoningの内容（chain of thought）は取得・保存しない。結果に残るのは構造化出力と`usage`だけである。
- Claudeは当面CLI依存を維持する。CLI versionを上げる際は、ADR-0006のflag一覧を再検証する必要がある。

## References

- `docs/plan/two-stage-cross-review-plan.md` 4.2節、4.5節
- OpenAI Responses API / Structured outputs / Reasoning / Data controls（developers.openai.com の API docs、2026-09時点で確認）
- OpenAI Models（https://developers.openai.com/api/docs/models、2026-09-21時点で確認）
- `scripts/run-codex-review.py`、`scripts/normalize-codex-review.py`、`scripts/lib/models.py`
- `prompts/codex-verify.md`、`schemas/codex-review.schema.json`、`tests/test_codex_review.py`
- ADR-0005、ADR-0006、ADR-0008
