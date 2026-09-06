# ADR-0002: Review trust boundaries

## Status

Proposed

## Context

AIレビューはuntrustedなPR内容（title、body、diff、filenames等）を入力とする。Prompt Injectionにより、AIがSecretを読む、コードを実行する、GitHubへ書き込む、といった行為を誘導される可能性がある。Phase 0時点ではworkflow・script本体は未実装であり、本ADRは境界の方針を先に確定するものである。

## Decision

- `prepare`、`review`、`publish`を別jobにし、既定を`permissions: {}`とする。`pull-requests: write`はpublisherだけに付与し、Claude Secretはreview jobのClaude実行stepだけに渡す。
- PRコード、PR由来workflow、依存script、hooks、Agent設定はcheckout・実行しない。bare Git環境で固定SHAからdiffを取得する。
- head SHA、base SHA、merge-base SHA、policy SHA、diff hashを記録し、投稿直前にhead/base/open状態を再確認する。不一致またはclosedの場合は投稿しない。
- AIにはGitHub write操作、repository・PR番号・投稿先・API endpointの決定をさせない。publisherは構造化結果をschema検証・無害化してから定型Markdownとして投稿する。
- 公式`claude-code-action`の採用は、固定版が「bundle外を読めない、Bash/Edit/Write/network/subagent/MCPを使えない、GitHub write不可、PR由来命令でtool権限を変更できない、raw prompt・Secretを公開ログへ出さない、構造化結果をschemaどおり取得できる」というgateを満たす場合に限る。満たさない場合は権限を緩めず、固定版CLI adapterを代替とする。

## Rationale

Promptだけでは防御にならない。filesystem境界、tool制限、GitHub権限分離、PRコード非実行、schema検証、出力無害化を重ねることで、Injectionが成功しても到達できる資産を限定する。

## Alternatives Considered

- 単一jobでレビューと投稿を行う: Claude SecretとGitHub write権限が同一jobに共存するため採用しない。
- PR headをcheckoutしてレビューする: PR由来のhooks・設定・scriptが実行される経路ができるため採用しない。
- AI出力をそのまま投稿する: HTML・mention・secretらしき値・不正pathの混入を防げないため採用しない。

## Consequences

- diff取得、schema、publisherの実装はPhase 1〜3で本ADRに従う。
- Action採用gateの検証結果によっては、Phase 2でCLI adapter案を提示して停止する。
- Claude Code CLI 2.1.263には`--restricted`（Bash等のcode実行toolとWebFetchを除去、file toolを作業ディレクトリへ限定、user/project/local settingsを無視）と`--safe-mode`が存在することをPhase 0で確認した。gate充足の実証はPhase 2で行う。

## References

- `docs/plan/implementation-plan.md` 5章、6章、7章、9章、11章、12章
- ADR-0001、ADR-0004
