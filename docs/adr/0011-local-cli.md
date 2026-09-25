# ADR-0011: Local CLI without a GitHub write path

## Status

Accepted

## Context

ADR-0005は、レビューを信頼された非公開の中央実行repositoryでだけ実行すると定めた。Planの4.9は、既存Python処理を再利用したローカルCLIをPhase 7へ分離し、前提を次のとおり定めた。

- tokenはCLI引数へ渡さず、環境変数（`AI_REVIEW_GITHUB_TOKEN`、`CLAUDE_CODE_OAUTH_TOKEN`、`OPENAI_API_KEY`）からだけ読む。
- 出力はローカルディレクトリへのMarkdown / JSON保存に限り、GitHubへの書き込み経路を持たない。
- `publish-review.py`をimportせず、投稿コードがローカル経路に存在しないことをテストで固定する。

ローカルCLIは中央実行repositoryの外で動く第2の実行経路である。trust boundaryと認証の扱いを変えるため、ADRで決定する。

中央実行と異なり、ローカル実行には次の性質がある。

- job間のprocess分離がない。1つのprocessが3種類のtokenを同時に環境変数として持つ。
- GitHub Actionsの`permissions:`・Secretのjob単位スコープ・ephemeral runnerがない。
- 実行者は手元のmachineと、そこにある設定（`~/.claude`、git config等）を持つ。

## Decision

### 1. 実行経路

- entry pointを`scripts/review-local.py`とする。既存scriptの関数（`build_request`、`prepare`、`run_review`、`normalize`、`run_codex_review`、`finalize`、`report`）をprocess内で順に呼ぶ。検証・正規化・最終処理のロジックを複製しない。
- stageの順序と失敗の扱いは中央実行と同じにする。Claudeが失敗した場合はCodexを実行せず、`finalize`へ`failure` / `skipped`を渡す。失敗を「問題なし」に変換しない。`prepare`が失敗した場合は最終結果を生成しない。
- 入力はworkflowと同じallowlist（`build_request`）を通す。`output_mode`は`summary_only`に固定し、最終結果のschemaを変えない。

### 2. GitHubへの書き込み経路を持たない

- `review-local.py`は`publish-review.py`をimportしない。GitHubのコメント作成・更新を呼び出す名前をローカル経路に置かない。既存の静的テスト（書き込み呼び出しは`lib/github.py`と`publish-review.py`だけ）を維持する。
- 加えて、`prepare`へはGET以外のmethodを拒否するread-only transport（`lib/github.py`の`read_only_transport`）を渡す。静的な不在と実行時の拒否を二重に置く。
- git fetchは既存`prepare`の経路（固定SHAの取得だけ、hooks無効）のままとする。

### 3. token

- 3種類のtokenを環境変数からだけ読む。CLI引数では受け取らない。tokenはshell historyとprocess一覧（argv）に現れない。
  - `AI_REVIEW_GITHUB_TOKEN`: read token。`prepare`だけへ渡す。未設定の場合は未認証で取得する（public repositoryだけが対象になる）。
  - `CLAUDE_CODE_OAUTH_TOKEN`: Claude stageだけへ渡す。
  - `OPENAI_API_KEY`: Codex stageだけへ渡す。
- `GITHUB_TOKEN`、`GH_TOKEN`、`AI_REVIEW_COMMENT_TOKEN`など、他の環境変数は読まない。comment tokenを使う経路はない。
- AI provider用の2つが未設定なら、network accessの前に停止する。
- Claude CLIは既存の最小環境（専用の`HOME`・`CLAUDE_CONFIG_DIR`、tool全無効）で起動する。実行者の`~/.claude`の設定・hooks・MCP serverは読まれない。Claude CLIは中央実行と同じ固定version・release digestを検証する。digest照合は`--version`を含むCLI実行より前に行い、`--version`はtokenなしの最小環境で実行する。照合した実体パスを本実行にも使う。
- Codex stageは既存どおりResponses APIだけを呼び、GitHub tokenを受け取らない。

### 4. 出力

- `--output-dir`へ`final-review.json`と`review-summary.md`だけを書く。directoryは存在しないか空でなければならず、新規作成時はmode 0700とする。既存の空directoryはgroup・otherに権限があれば拒否する。summaryはstdoutにも表示する。
- bundle、git作業領域、raw provider response、normalized resultは一時directoryに置き、終了時に削除する。raw provider responseを残さない。
- 最終結果が`publishable`（両stage成功かつ全検証通過）ならexit code 0、それ以外は2とする。`publishable`はローカルでは投稿を意味しないが、「完全な結果が得られたか」の判定として使う。
- `GITHUB_OUTPUT`と`GITHUB_STEP_SUMMARY`へは書かない。

### 5. 範囲外

GHES対応（API URL・server URLの指定）、運用チェック（ADR-0010の警告）、結果のキャッシュ・再開、workflowの変更は行わない。

## Rationale

- 既存scriptは関数単位で分離され、transport・CLI binary・環境を引数で受け取る。process内で呼べば、検証ロジックを複製せずに中央実行と同じ判定を得られる。scriptをsubprocessで連結する方式は、tokenを環境変数で子processへ継承させることになり、stage単位のtoken分離が崩れる。
- job単位のSecret分離がない代わりに、tokenを`main`で1度だけ読み、使用するstageの引数へだけ渡す。Claude CLIの子processは既存の最小環境を使うため、他のtokenを継承しない。
- 投稿経路の不在を静的テストだけに頼ると、将来`lib/github.py`経由で書き込みが紛れ込んでも検出できない。read-only transportを重ねることで、実行時にも拒否できる。
- 出力directoryを空に限るのは、別PR・別runの結果との混在を防ぐためである。新規・既存ともgroup・otherのアクセスを許さないのは、PRの内容（private repositoryを含む）を同一machineの他userへ見せないためである。

## Alternatives Considered

- **scriptをsubprocessで順に起動する**: 実装は単純だが、各scriptはtokenを環境変数から読むため、3種類のtokenを子processへ継承させるか、stageごとに環境を組み立て直す必要がある。後者は結局orchestratorになる。採用しない。
- **tokenをCLI引数で受け取る**: shell historyとprocess一覧へ残る。Planの前提に反する。採用しない。
- **`output_mode`に`local`を追加する**: 最終結果のschema、`result.py`、publisherの検証を同時に変えることになる。ローカル経路は投稿しないので、`summary_only`で意味が足りる。採用しない。
- **中間ファイルを残す（`--keep-workdir`）**: raw provider responseとbundle全体が手元に残る。ADR-0006のartifact方針（raw responseを残さない）と揃えるため、採用しない。
- **Claude CLIのdigest検証をローカルでは省略する**: 利用者の環境差を吸収できるが、検証済みversion以外で結果が出ることになる。中央実行と同じgateを維持する。採用しない。

## Consequences

- 利用者は固定versionのClaude Code CLIを手元へ導入する必要がある。version・digestが一致しない場合はClaude stageが失敗し、最終結果は`publishable=false`（exit code 2）になる。
- ローカル実行では、中央repositoryの閲覧権限によるアクセス制御が効かない。対象PRの内容と結果は実行者のmachineに残る（出力directoryだけ）。対象repositoryの読み取り権限を持つ実行者が使う前提である。
- 3種類のtokenが同一processの環境変数に存在する。process内の分離は関数引数の規律とテストで担保する。OS上のprocess分離は提供しない。
- CLIの実行前検証とtokenなしの`--version`は共有の`run-review.py`で行うため、中央workflowにも適用される。
- 運用チェック（ADR-0010）の警告はローカルでは出ない。
- GitHubへの書き込みを追加する変更は、静的テストとread-only transportのテストの両方で検出される。

## References

- `docs/plan/two-stage-cross-review-plan.md` 4.9、5章 Phase 7
- `scripts/review-local.py`、`scripts/lib/github.py`
- `tests/test_review_local.py`、`tests/test_publish_review.py`
- ADR-0005、ADR-0006、ADR-0007、ADR-0008、ADR-0010
