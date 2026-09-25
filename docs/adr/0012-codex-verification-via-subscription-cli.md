# ADR-0012: Codex verification via the Codex CLI with ChatGPT authentication

## Status

Proposed

Supersedes ADR-0007

## Context

ADR-0007は、Codexの再検証をOpenAI Responses APIの直接呼び出しで行い、認証を`OPENAI_API_KEY`とすることを決めた。この方式は利用量に応じたAPIの従量課金になる。

運用要件が次のように変わった。

- 当初は手元のPlus契約枠での再検証を想定した。今回の要件はChatGPTアカウントでの認証を使い、API keyによる従量課金を使わずに実行することであり、設計はPlusプランに限定しない。
- 従量課金への自動フォールバックは設けない。サブスクリプションの利用上限に達したら、再検証は失敗として停止してよい。
- ChatGPT側で追加クレジットが使われる場合、その扱いはChatGPTの契約に従うものとして許容する（API keyの従量課金とは別である）。

ChatGPTサブスクリプションでCodexを使う経路はCodex CLI（ChatGPTアカウントでのsign-in）である。Responses APIへサブスクリプションの認証を渡す手段はない。このため、ADR-0007が採用しなかったCodex CLIの導入を再検討する必要がある。

公式ドキュメント（2026-09-25時点で確認）の要点は次のとおりである。

- CI/CDでCodexのアクセストークン（`CODEX_ACCESS_TOKEN`）を使う方式は、現時点ではChatGPT Business／Enterprise workspace向けであり、手元のPlus契約では使えない。
- ChatGPT管理の認証（`auth.json`）をCI/CDで使う場合、`auth.json`を信頼済みの非公開環境に保持し、CLIが更新した`auth.json`を次のjobへ引き継ぐ必要がある。永続（self-hosted）runnerでは、`codex login`で一度作成した`auth.json`をdisk上に置き続ければよい。ephemeral runnerでは、実行後に更新済みの`auth.json`を安全な保管先へ書き戻す仕組みが要る。
- 1つの`auth.json`を使うのは、1台のmachineか、直列化されたjob列だけに限る。`auth.json`はパスワードと同等に扱う。

ADR-0007はCLIを採用しない理由として「実行手段を持つ構成になる」ことを挙げていた。今回の変更では、Responses APIの`tools: []`と同等の「全ツール禁止」をCLIで維持できるかを先に確認した。pinned CLI（`codex-cli 0.155.1`）で2026-09-25に次を確認した。

- `--disable <feature>`でshell・unified exec・apps・plugins・hooks・multi agent・browser・computer use・画像生成などのtool系featureを無効化できる。未知のfeature名は`Unknown feature flag`で起動が失敗する。
- `-c web_search="disabled"`でWeb検索を無効化できる。
- 上記の無効化と`--sandbox read-only`の下で「shell commandを実行せよ」と指示しても、モデルはtoolを使えないと回答し、`--json`のevent streamにtool系のitemは現れなかった。
- `-c model_instructions_file=<path>`でCLIの基本指示を固定の契約（`prompts/codex-verify.md`）に置き換えられる。
- 作業directoryの`AGENTS.md`は既定で読み込まれるが、`-c project_doc_max_bytes=0`で読み込まれなくなる。
- `--output-schema`で最終応答のJSON Schemaを指定できる。
- ChatGPTアカウントでのsign-inで`gpt-5.6-sol`を、reasoning effort `low` / `xhigh` / `max`で実行できた。
- `-c forced_login_method=...`はsign-in方式が一致しない場合にCLIが**ログアウトを実行する**。副作用があるため採用しない。

## Decision

### 実行方式

- Codexの再検証は、固定versionのCodex CLI（`codex exec`）で実行する。versionは`scripts/lib/limits.py`の`CODEX_CLI_VERSION`（`0.155.1`）とし、`codex --version`の出力が一致しなければ停止する。
- 認証はChatGPTアカウントでのsign-inだけとする。モデルを呼ぶ前に`codex login status`を実行し、`Logged in using ChatGPT`の行がなければ停止する。これは認証方式の確認であり、契約プランや残りの利用枠の証明ではない。API keyでのsign-in、未sign-inはどちらも停止し、API従量課金へフォールバックしない。
- CLIへ渡す環境変数は最小限とし、`OPENAI_API_KEY`、`CODEX_API_KEY`、GitHub tokenを含めない。`CODEX_HOME`はsign-in済みのdirectoryを指し、`HOME`と`TMPDIR`はjob workdir配下に置く。
- CLIのargvは固定とし、次を必ず指定する。
  - `--json`、`--ephemeral`（session fileを残さない）、`--ignore-user-config`、`--ignore-rules`、`--skip-git-repo-check`
  - `--sandbox read-only`
  - `--model <allowlist値>`、`-c model_reasoning_effort=<allowlist値>`
  - `--output-schema schemas/codex-review.schema.json`
  - `-c model_instructions_file=prompts/codex-verify.md`（中央側、calleeと同一commit）
  - `-c project_doc_max_bytes=0`、`-c web_search="disabled"`
  - tool系featureの`--disable`（一覧は`scripts/run-codex-review.py`の`DISABLED_FEATURES`）
- CLIは空の作業directoryで起動する。PR由来データとClaude結果はstdinだけで渡し、nonce付き境界マーカーの内側に置く（ADR-0006の境界を維持する）。argvにPR由来データを含めない。
- **全ツール禁止を維持する。** feature無効化は設定であり構造的な保証ではないため、`--json`のevent streamを検査する。許可するitem typeは`agent_message`、`reasoning`、`error`だけとし、それ以外（command実行、file変更、MCP・Web・その他tool呼び出し、未知のtype）が1件でも現れたら、出力を破棄して失敗とする。
- 次の場合も失敗として停止し、「問題なし」に変換しない。
  - CLIが非ゼロで終了する、timeoutする、実行できない。timeoutまたは出力上限超過時は、CLIラッパーと子プロセスを同一プロセスグループごと停止する。
  - `turn.failed`が現れる（サブスクリプションの利用上限を含む）。
  - `turn.completed`がちょうど1件でない、最終の`agent_message`がない・空である。
  - event streamがJSON Linesとして解釈できない、未知のevent typeを含む、上限（`codex_max_event_stream_bytes`）を超える。stdoutは逐次読み取りで上限を適用し、stderrも別途64 KiBに制限する。超過時はCLIを停止する。
  - 最終応答がschemaに適合しない（既存の正規化で検証する）。

### 実行場所

- 中央実行workflowの`codex_review` jobは、中央実行repository専用のself-hosted runner（ラベル`[self-hosted, ai-review-codex]`）で実行する。他のjobはGitHub-hosted runnerのままとする。
- runnerの運用者がCodex CLIを導入し、runnerの実行ユーザーで`codex login`を行う。`auth.json`はrunnerの`CODEX_HOME`（未設定なら`~/.codex`）に置き、CLIが更新する。workflowとSecretには置かない。
- 1つの`auth.json`を使うrunnerは1台だけとする。self-hosted runnerは1台で同時に1 jobだけを処理するため、`codex_review`は直列に実行される。job levelの`concurrency`は使わない（待機中のjobが取り消され、再検証の失敗が増えるため）。
- ローカルCLI（ADR-0011）は、利用者の手元のCodex CLIとsign-in（`CODEX_HOME`、既定`~/.codex`）を使う。`OPENAI_API_KEY`は読まない。

### 記録

- 実行記録（`run`）は`provider: openai-codex-cli`、`cli_version`、`auth_mode: chatgpt`、`thread_id`、`input_tokens`、`output_tokens`を持つ。ADR-0007の`endpoint`、`store`、`response_id`は削除する。`finalize`は`auth_mode`が`chatgpt`でない結果と、`cli_version`が固定値と異なる結果を受け付けない。
- CLIのevent streamは実使用モデルを報告しないため、`model_reported`は`null`とする。推測で埋めない。
- 結果の形が変わるため、`FRAMEWORK_VERSION`を`0.5.0`へ上げる。

### ADR-0007から維持する決定

- モデルallowlist（`gpt-6-astra`、`gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、既定`gpt-5.6-sol`）とeffort allowlist（`low`〜`max`、`none`は不許可）、workflowの`choice`とPython側allowlistの二重検証、ライフサイクル更新の手順。
- strict JSON Schema（`schemas/codex-review.schema.json`）による構造化出力と、正規化での検証。
- 固定契約（`prompts/codex-verify.md`）、境界マーカー、Claude結果をuntrusted dataとして扱うこと。
- GitHub credentialをCodexへ渡さないこと。raw出力をartifact化しないこと。
- Claudeは既存のCLI adapterを維持すること。

### 関連ADRへの影響

- ADR-0008: `OPENAI_API_KEY` Secretを廃止する。`codex_review` jobはSecretを参照しない。
- ADR-0010: `AI_REVIEW_OPENAI_KEY_EXPIRES_ON`による期限監視を廃止する。ChatGPTのsign-inはCLIが更新するため、Secretの期限日として管理しない。Job Summaryの上限表示は「Codex max output tokens」から「Codex timeout seconds」に変わる。
- ADR-0011: ローカルCLIのCodex stageは`OPENAI_API_KEY`ではなく`CODEX_HOME`のChatGPT sign-inを使う。`--codex-bin`で固定versionのCLIを指定できる。

## Rationale

ChatGPTアカウントの認証でCodexを使う経路としてCLIを採用する。`codex login status`で確認できるのは認証方式だけなので、契約プランと利用枠はrunner運用者が別途確認する。

ADR-0007がCLIを退けた理由（実行手段を持つ構成になる）には、次の多層の対策で応える。

1. tool系featureとWeb検索の無効化、read-only sandbox、空の作業directory、ユーザー設定・rules・project docsの不読込で、実行手段と外部入力を減らす。
2. event streamの検査で、tool呼び出しが記録された場合は出力を破棄し、投稿へ進めない。ただし検査はCLI終了後であり、実行済みの読み取りや通信を取り消せない。`auth.json`などの認証情報保護を、この検査だけで保証しない。
3. CLI versionを固定し、feature名が変わった場合は起動失敗として表面化させる。
4. GitHub credentialとAPI keyを環境変数から除き、CLIへ渡さない。

self-hosted runnerは、公式ドキュメントが永続runnerとして想定する運用に一致する。GitHub-hosted runnerで`auth.json`を扱うには、Secretへの書き戻しのために中央repositoryのSecretを書き換えられるcredentialをworkflowへ渡す必要があり、攻撃面と運用負荷が大きい。

`forced_login_method`は一致しないsign-inを検出できるが、その場でログアウトを実行するため、runnerのsign-inを失わせる。`codex login status`の読み取りで同じ判定ができるため、副作用のない方を採用する。

## Alternatives Considered

- Responses APIとAPI keyを維持する（ADR-0007）: 従量課金になり、要件を満たさない。
- API keyへの自動フォールバックを設ける: 従量課金を使わない要件に反し、課金経路が暗黙に切り替わるため採用しない。
- `CODEX_ACCESS_TOKEN`をSecretとして使う: 対応プランとSecretの管理方法が異なり、今回のChatGPT sign-in方式の範囲外である。
- GitHub-hosted runnerで、`auth.json`をSecretから復元し、実行後に書き戻す: 書き戻しに中央repositoryのSecretを変更できるcredentialが必要になり、AI jobの権限分離（ADR-0006、ADR-0008）と両立しない。書き戻しに失敗するとsign-inを失う。
- ローカルCLIだけでサブスクリプション実行し、中央CIの再検証をやめる: 中央実行の二段階レビュー（ADR-0005）を維持できないため、利用者の判断によりself-hosted runnerを採用した。
- tool無効化の設定だけに依存し、event streamを検査しない: 設定の効き方はCLIのversionと実装に依存し、構造的な保証にならないため採用しない。
- `codex exec review`（CLI組み込みのreview）を使う: repositoryのcheckoutを前提とし、入力snapshotと出力形式を制御できないため採用しない。

## Consequences

- OpenAI API keyのSecretがなくなる。代わりにself-hosted runner上の`auth.json`が長期credentialになる。runnerのdisk・実行ユーザー・登録範囲の保護が、この構成の安全性の前提になる。runnerは中央実行repository専用とし、他のrepositoryや組織のrunner groupと共有しない。
- self-hosted runnerは中央repositoryの他のworkflowからもラベル指定で使われうる。中央repositoryは非公開とし、workflowの変更権限を持つ人をrunnerの管理者と同等以下に保つ必要がある。
- tool系featureの無効化はCLI `0.155.1`で確認した設定上の制限であり、構造的な隔離ではない。event streamの検査は事後検知なので、もしtoolが実行された場合、読み取りや外部通信の影響は取り消せない。runner上の`auth.json`を含むcredentialの残余リスクは、専用runnerと変更権限の管理を前提に受容する。
- `codex_review`はrunnerが停止している間は実行されず、job timeoutまで待機する。その場合は失敗として記録され、投稿されない。
- サブスクリプションの利用上限に達すると`codex_review`は失敗する。失敗は「問題なし」に変換されない。
- `codex login status`では契約プランや残りの利用枠を判定できない。runnerのアカウントの契約・クレジット設定は運用者が別途確認する。
- `model_reported`は常に`null`になり、要求モデルと実使用モデルの差分は記録できない。
- CLIのversionを上げるたびに、feature名、event形式、`codex login status`の出力、`model_instructions_file`・`project_doc_max_bytes`の挙動を再確認し、`CODEX_CLI_VERSION`と本ADRのReferencesを更新する必要がある。
- `gpt-5.6-sol`以外のallowlist IDは、ChatGPT sign-inでの実行を確認していない。プランが提供しないIDは`codex_review`の失敗として現れる。
- Responses APIの`store: false`に相当する指定はCLIにない。ChatGPTアカウント経由で送信された内容の保持は、ChatGPTのデータ管理設定に従う。
- `scripts/lib/openai_api.py`は使われなくなる。

## References

- `docs/plan/two-stage-cross-review-plan.md` 4.5節、Phase 8
- OpenAI Codex: Authentication、Maintain Codex account auth in CI/CD (advanced)（https://learn.chatgpt.com/docs/auth/ci-cd-auth）、Access tokens（https://learn.chatgpt.com/docs/enterprise/access-tokens、2026-09-25時点で確認）
- Codex CLI `0.155.1`の`codex exec --help`、`codex features list`、および本ADRのContextに記載した動作確認（2026-09-25）
- `scripts/run-codex-review.py`、`scripts/normalize-codex-review.py`、`scripts/lib/limits.py`、`scripts/lib/result.py`、`scripts/review-local.py`
- `.github/workflows/cross-review.yml`、`actions/review-runtime/action.yml`
- `tests/test_codex_review.py`、`tests/test_cross_pipeline.py`、`tests/test_review_local.py`、`tests/test_workflow_policy.py`
- ADR-0005、ADR-0006、ADR-0007、ADR-0008、ADR-0010、ADR-0011
