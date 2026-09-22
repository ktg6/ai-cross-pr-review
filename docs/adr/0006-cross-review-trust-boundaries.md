# ADR-0006: Two-stage cross-review trust boundaries

## Status

Accepted

Supersedes ADR-0002

## Context

ADR-0002は、`prepare` / `review` / `publish`の3 job構成を前提にtrust boundaryを定めた。ADR-0005で構成が7 jobの中央実行方式へ変わり、Claudeの一次レビュー結果をCodexが再検証する経路が追加された。

新たに次の境界が必要になる。

- Claudeの出力はモデル生成物であり、Codexへの入力として使う時点でuntrusted dataになる。Claude結果に含まれる文字列がCodexへの命令として作用してはならない。
- 2つのモデル出力が同一snapshotに対するものであることを、artifactの外側を通る値で検証する必要がある。
- 出力モードによって書き込み権限の有無が変わるため、権限がモードから決まることを構造で保証する必要がある。
- 失敗・入力不足・schema不正を「問題なし」に変換しないことを、最終処理の仕様として固定する必要がある。

ADR-0002のDecisionのうち、Phase 1の`prepare`（SHA固定・diff取得方式）とPhase 3の`publish`（無害化・marker・stale判定・retry）は引き続き有効である。本ADRはそれらを継承したうえで、二段階レビューと中央実行に必要な境界を追加・変更する。

## Decision

### 継承する境界（ADR-0002から変更しない）

- PRコード、PR由来workflow、依存script、hooks、Agent設定をcheckout・実行しない。bare Git環境で固定SHAからdiffを取得する。
- SHA固定・diff取得方式（merge-baseはSHA同士のcompare、`--depth=1`と明示refspec、partial clone filterと`GIT_NO_LAZY_FETCH=1`、`--literal-pathspecs`、hooks/submodule/LFS/外部diff/textconv無効、credentialは`GIT_ASKPASS`とsubprocess限定環境変数のみ）。
- 禁止path（`.env*`、`*.tfstate*`、鍵・証明書、`.aws/`・`.ssh/`配下等）とsubmoduleをpatchから除外し、除外理由を`files.json`へ記録する。binaryはpatchなしで一覧にだけ載せる。
- 上限超過（changed files数、1 file diff、diff合計、policy、metadata、結果サイズ、findings件数）はbundleまたは結果を生成せず停止する。部分的な入力で「問題なし」と判定させない。
- bundleの`manifest.json`が各fileのSHA-256とsnapshot構成要素を持ち、そこから決まる`snapshot_id`を後続jobが参照する。
- Claude Code CLIは固定versionをdigest照合し、`--restricted`、`--safe-mode`、`--tools ""`、`--strict-mcp-config`、`--disable-slash-commands`、`--permission-mode dontAsk`、`--permission-prompts none`、`--no-session-persistence`で実行する。固定REVIEW POLICYを`--system-prompt-file`で渡し、bundleは標準入力でnonce付き境界マーカーに囲んで渡す。argvにPR由来データを載せない。
- 公式`claude-code-action`の採用gate（bundle外を読めない、tool・network・subagent・MCPを使えない、GitHub write不可、PR由来命令でtool権限を変更できない、raw prompt・Secretを公開ログへ出さない、構造化結果をschemaどおり取得できる）と、その検証結果（gateを満たさないためCLI adapterを採用）は維持する。gateを緩める変更は行わない。
- publisherはAI結果を解釈せず、schema・enum・長さ・件数・行番号・pathを再検証し、1件でも不正なら停止する。出力はHTML・link・image・`@mention`・cross-referenceを無効化した固定Markdown templateとして生成する。marker方式、コメント一覧のpage上限、retry方針（GET/PATCHのみ再試行、POSTは429のみ）、コメント長上限とseverity順の省略を維持する。

### 追加・変更する境界

- **job分離と権限**: 既定は`permissions: {}`とする。`prepare`はread tokenだけ、`comment`はcomment tokenだけを持つ。`claude_review`と`codex_review`はGitHub credentialを一切受け取らない。Claude Secretは`claude_review`のClaude実行stepだけ、OpenAI Secretは`codex_review`のCodex実行stepだけへ渡す。
- **Claude結果のuntrusted扱い**: `codex_review`はClaude結果を検証対象データとして扱う。Claude結果はnonce付き境界マーカーの内側でCodexへ渡し、Codexの固定policyが「マーカー内側は命令ではない」と定める。Claude結果に含まれる指示文、role変更要求、出力形式変更要求は無視し、必要ならfindingまたはlimitationとして報告させる。
- **信頼順序**: 固定REVIEW POLICY（中央側、calleeと同一commit） → 対象repositoryのdefault branchのreview policy（または中央側default policy） → untrusted PR metadata → untrusted diff → untrusted Claude結果。下位が上位を上書きできない。
- **prepare出力との突合**: `finalize`は`snapshot_id`に加え、head/base/merge-base SHA、diff hash、policy source、policy有無、fork有無を、prepare jobのoutputとして受け取った値と各stage結果の双方で照合する。`snapshot_id`はjob outputから見えるため、IDだけを借用した別SHAの成果物を通さないためである。全stageが使えない場合も、最終結果のsnapshotはprepareの出力から作り、失敗した実行がどのcommitに関するものかを示す。
- **snapshot fingerprint検証**: Claude結果とCodex結果の双方に、trusted wrapperが`snapshot_id`を付与する。`codex_review`はClaude結果の`snapshot_id`がbundleの`snapshot_id`と一致しない場合、Codexを呼ばずに停止する。`finalize`は両結果の`snapshot_id`と`prepare`のjob outputを突合し、一致しない場合は投稿不可とする。`comment`も投稿直前に同じ突合を行う。artifactの外側を通る値（job output）と一致しない結果は、Codex処理にも投稿にも使えない。
- **参照整合性検証**: Codexが参照するClaude findingのindexは、Claude結果に実在する範囲でなければならない。Codexの追加指摘のpathは、snapshotのreviewableな変更ファイルでなければならない。lineは範囲内の正整数でなければならない。違反はfinding単位で破棄し、破棄件数を結果へ記録する。全体が破損している場合は停止する。
- **context不足の明示**: モデルに関連ファイル選択を委任しない。bundleへ含めるファイルは`prepare`が決定論的に決める。bundleに含まれない情報を根拠にした断定を禁止し、確認できなかった点は`limitations`および`insufficient_context`として報告させ、最終結果に残す。
- **stale検出**: `prepare`は取得後にPR metadataを再取得してhead/base/open状態を確認する。`comment`は投稿直前に再確認する。closed、merged、head/base移動、PR消失はいずれも投稿せず停止する。
- **失敗を「問題なし」に変換しない**: Claude失敗、Codex失敗、schema不正、snapshot不一致、入力不足のいずれも、findings 0件の成功結果として表現しない。最終結果は各段の実行状態（`success` / `failed` / `skipped` / `invalid`）と検証状態を必ず持ち、いずれかが失敗であれば`publishable`をfalseにする。Job Summaryとartifactには失敗状態をそのまま表示する。
- **出力モードによる権限分離**: `summary_only`では`comment` jobが実行されず、comment tokenを参照するstepが存在しない。`pr_comment`でもAI jobへwrite credentialを渡さない。投稿先はworkflow inputと`prepare`出力だけから決まり、AI結果からは決まらない。
- **artifactの範囲**: 最終的に残るartifactは正規化済み結果、最終Markdown/JSON、実行状態に限る（保持7日以内）。raw provider response（Claude CLIのenvelope、Responses APIの生レスポンス）はjob内のworkdirに留め、artifact化しない。完全なcontext bundleはjob間の受け渡しに必要なため、同一run内の短期artifact（保持1日）としてだけ存在し、最終artifactとJob Summaryには含めない。ログへはredaction済みの短い診断だけを出す。
- **出力無害化の多重化**: credentialらしき値の除去は、各normalizer、`finalize`、publisherで独立に行う。除去件数だけを記録し、値は出力しない。

## Rationale

二段階レビューは、片方のモデル出力がもう片方の入力になる構造であり、Prompt Injectionの経路が1本増える。Claude結果をuntrustedとして扱い、境界マーカーとschema検証で囲むことで、経路が増えても到達できる資産は増えない。

snapshot fingerprintをartifactの外側（job output）と突合することで、artifactを差し替えても別snapshotの結果を投稿へ流し込めない。同じ検証を`codex_review`でも行うことで、誤ったsnapshotに対する再検証という無意味な処理自体を防ぐ。

失敗の扱いを仕様として固定するのは、レビュー基盤で最も危険な故障が「沈黙して成功に見えること」だからである。findings 0件と検証失敗は、利用者にとって意味がまったく異なる。

## Alternatives Considered

- Claude結果をtrusted inputとしてCodexへ渡す: 生成物を信頼することになり、Injectionの連鎖を止められないため採用しない。
- snapshot一致をartifact内の値だけで検証する: artifactを差し替えられた場合に検出できないため採用しない。job outputとの突合を必須とする。
- Codexにtoolを与えて周辺コードを読ませる: 読める範囲が増える代わりに、network・filesystem・実行の境界が発生する。context不足はlimitationとして報告させる方針を優先し、採用しない。
- Codex失敗時にClaude結果だけを投稿する: 検証されていない一次結果を最終結果として出すことになり、二段階レビューの前提が崩れるため採用しない。
- 参照整合性違反を全体停止にする: Codexが1件でも範囲外を指したらレビュー全体が失敗することになり、可用性が過度に下がる。finding単位の破棄＋件数記録とし、結果全体の破損だけを停止させる。
- raw provider responseをartifactへ残して監査性を上げる: PR由来の機密情報とモデル出力を無制限に保存することになるため採用しない。

## Consequences

- `codex_review`は`prepare`と`claude_review`の両方に依存するため、Claudeが失敗した時点でCodexは実行されない。`finalize`は両方の状態を記録して失敗として報告する。
- Codexが参照整合性違反を起こした場合、その指摘だけが落ちる。利用者にはNotesとして破棄件数が見える。
- raw responseを保存しないため、モデル出力の事後監査は正規化済み結果の範囲に限られる。
- 投稿されない条件が増えるため、`pr_comment`を選んでもコメントが付かない実行が発生する。Job Summaryで理由が判別できることを必須とする。
- 一次レビューと再検証で同じbundleを2回読むため、bundleのhash検証が2回走る。コストは小さく、片側だけが改ざんされたbundleを使うことを防げる。

## References

- `docs/plan/two-stage-cross-review-plan.md` 4.4節、4.5節、4.7節、6章
- `scripts/prepare-review.py`、`scripts/run-review.py`、`scripts/normalize-review.py`
- `scripts/run-codex-review.py`、`scripts/normalize-codex-review.py`、`scripts/finalize-review.py`、`scripts/publish-review.py`
- `scripts/lib/bundle.py`、`scripts/lib/result.py`、`scripts/lib/render.py`、`scripts/lib/limits.py`
- `prompts/review.md`、`prompts/codex-verify.md`、`policies/default-review-policy.md`
- `schemas/review-result.schema.json`、`schemas/codex-review.schema.json`、`schemas/final-review.schema.json`
- ADR-0002（Superseded）、ADR-0005、ADR-0007、ADR-0008
