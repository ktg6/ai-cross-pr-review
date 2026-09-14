# ADR-0002: Review trust boundaries

## Status

Proposed

## Context

AIレビューはuntrustedなPR内容（title、body、diff、filenames等）を入力とする。Prompt Injectionにより、AIがSecretを読む、コードを実行する、GitHubへ書き込む、といった行為を誘導される可能性がある。Phase 0時点ではworkflow・script本体は未実装であり、本ADRは境界の方針を先に確定するものである。

Phase 1では`prepare`の実体（`scripts/prepare-review.py`、`scripts/lib/`）を実装した。PR snapshotの固定方法、diff取得方式、上限超過時の挙動、git実行の強化設定は境界そのものであるため、本ADRのDecisionへ追記する。

Phase 2では`review`を実装するにあたり、公式`claude-code-action`が本ADRの採用gateを満たすかを固定版（v1、base-actionがClaude Code CLI 2.1.263を導入）で検証した。検証結果と、それに基づくreview jobの実行方式もDecisionへ追記する。

## Decision

- `prepare`、`review`、`publish`を別jobにし、既定を`permissions: {}`とする。`pull-requests: write`はpublisherだけに付与し、Claude Secretはreview jobのClaude実行stepだけに渡す。
- PRコード、PR由来workflow、依存script、hooks、Agent設定はcheckout・実行しない。bare Git環境で固定SHAからdiffを取得する。
- head SHA、base SHA、merge-base SHA、policy SHA、diff hashを記録し、投稿直前にhead/base/open状態を再確認する。不一致またはclosedの場合は投稿しない。
- AIにはGitHub write操作、repository・PR番号・投稿先・API endpointの決定をさせない。publisherは構造化結果をschema検証・無害化してから定型Markdownとして投稿する。
- SHA固定・diff取得方式（Phase 1）:
  - base SHA、head SHAはPR metadata取得時に記録し、merge-base SHAは`compare/{base_sha}...{head_sha}`のようにbranch名ではなくSHA同士で求める。同一snapshotからは同一のmerge-baseが得られる。
  - 毎回新規に作成したbare repositoryへ、`--depth=1`かつ明示refspec（`refs/pull/N/head`とmerge-base SHA）でその2 commitだけをfetchする。fetch後にheadをAPI記録値と照合し、不一致なら停止する。diff生成後にPR metadataを再取得し、head・base・open状態の変化があれば停止する。
  - fetch時は`blob:limit=1 MiB`のpartial clone filterを指定し、`GIT_NO_LAZY_FETCH=1`で上限超過blobの遅延取得を禁止する。filter非対応または欠落blobを必要とする場合は停止する。
  - diffはmerge-base〜headの範囲でgitが生成し、changed filesの一覧もgitの`--raw`出力を正とする。GitHub APIの`changed_files`は上限の事前判定にだけ使う。
  - git実行はargv配列と`--literal-pathspecs`のみで行い、filenameは`--`の後に渡す。`--no-ext-diff`、`--no-textconv`、hooks無効、submodule再帰無効、LFS smudge無効、global/system config無効、`protocol.allow=never`（httpsのみ許可）を毎回指定する。
  - credentialは`GIT_ASKPASS`とfetch subprocess限定の環境変数でのみgitへ渡し、argv・config file・bundle・ログへ出さない。
  - `.env*`、`*.tfstate*`、鍵・証明書、`.aws/`・`.ssh/`配下等の禁止pathとsubmoduleはpatchから除外し、除外理由を`files.json`に記録する。binaryはpatchなしで一覧にだけ載せる。
  - changed files数、1 file diff、diff合計、policy、metadataの上限超過、merge-base取得失敗、snapshot不整合はいずれもbundleを生成せず停止する。部分的なbundleで「問題なし」と判定させない。
  - 1 MiBを超えるblobを含むPRは、runnerの資源を保護するためbundle生成を停止する。
  - policyはdefault branchのhead commit SHAを固定してから取得し、PR head側の同名fileは使用しない。
  - bundleは`manifest.json`に各fileのSHA-256、head/base/merge-base/policy SHA、diff hash、framework versionを持ち、これらから決まる`snapshot_id`を後続jobとpublisher markerが参照する。
- 公式`claude-code-action`の採用は、固定版が「bundle外を読めない、Bash/Edit/Write/network/subagent/MCPを使えない、GitHub write不可、PR由来命令でtool権限を変更できない、raw prompt・Secretを公開ログへ出さない、構造化結果をschemaどおり取得できる」というgateを満たす場合に限る。満たさない場合は権限を緩めず、固定版CLI adapterを代替とする。
- Action採用gateの検証結果（Phase 2）: **満たさない**。gateを緩めないため、公式Actionを採用せず固定版CLI adapterを採用する。判定根拠は次のとおりである。
  - GitHub tokenを回避できない。`claude-code-action`は`github_token` inputか、`id-token: write`によるOIDC交換で得るGitHub App token（既定で`contents: write`、`pull_requests: write`、`issues: write`）のいずれかを必須とし、どちらも得られない場合はエラーで停止する。review jobにGitHub tokenを置かないという境界と両立しない。
  - workspaceのcheckoutを前提とする。automation modeのprepareはgit認証設定（`git config user.name`、`git remote set-url origin`でのtoken埋め込み）を実行するため、review jobにconsumer repositoryのworking treeとcredentialを含む`.git/config`が存在することになる。「bundle外を読めない」を満たせない。
  - 実行時にtoolchainを取得する。Bunの導入と`bun install --production`がSecretを持つjob内で走るため、full SHA固定の対象外となる依存が増える。
  - 一方で、tool制限（`claude_args`の`--allowedTools`／`--disallowedTools`）、構造化結果（`--json-schema`と`structured_output` output）、raw出力の非表示（`show_full_output`既定false）はgateを満たす。不足しているのは上記3点である。
- review jobの実行方式（Phase 2、CLI adapter）:
  - 固定版Claude Code CLI（`2.1.263`）を公式installerで導入し、`claude --version`と、署名済みrelease manifest由来のSHA-256でbinaryを照合する。不一致ならレビューを実行しない。
  - CLIは`--print`で起動し、`--restricted`、`--safe-mode`、`--tools ""`、`--strict-mcp-config`、`--disable-slash-commands`、`--permission-mode dontAsk`、`--permission-prompts none`、`--no-session-persistence`を常に指定する。toolもMCP serverもskillも持たないため、bundle外のfile・command・networkへ到達する経路がない。untrusted textに含まれる`/skill`表記がcommandとして展開されることも防ぐ。
  - 固定REVIEW POLICY（`prompts/review.md`、calleeと同一commit）を`--system-prompt-file`で渡す。bundleは標準入力から渡し、実行ごとに生成する乱数nonce付き境界マーカーで囲む。argvにはPR由来データを載せない。
  - review jobは`permissions: {}`とし、GitHub tokenを渡さない。Claude Secretはcomposite actionのClaude実行stepにだけ渡し、subprocessへは最小の環境変数だけを構築して渡す。job環境のtokenやActions関連変数は継承させない。
  - `--output-format json`のenvelopeとpromptはjob間へ渡さない。artifact化するのは正規化済み結果だけとし、workdirはstep終了時に削除する。
  - 正規化（`scripts/normalize-review.py`）は決定論的に行う。envelopeの`is_error`・`subtype`・`permission_denials`を検査し、structured outputに対して未知field、必須field、enum、長さ、件数、changed files外のpath、除外済みpath、不正な行番号を検証する。findingsの問題は個別に破棄して`normalization.dropped_findings`へ記録し、result全体の破損・上限超過・tool要求は停止させる。
  - repository、PR番号、base/head/merge-base SHA、diff hash、policy SHA、provider、model、run IDはtrusted wrapperがbundleのmanifestと自前のinvocation記録から付与する。モデル出力のschemaにはこれらのfieldを置かない。
  - 出力にはcredentialらしき値の除去を適用し、review stepへ渡したtoken値そのものも除去対象に含める。stderrはredactionと制御文字除去のうえ末尾のみをログへ出す。

## Rationale

Promptだけでは防御にならない。filesystem境界、tool制限、GitHub権限分離、PRコード非実行、schema検証、出力無害化を重ねることで、Injectionが成功しても到達できる資産を限定する。

## Alternatives Considered

- 単一jobでレビューと投稿を行う: Claude SecretとGitHub write権限が同一jobに共存するため採用しない。
- PR headをcheckoutしてレビューする: PR由来のhooks・設定・scriptが実行される経路ができるため採用しない。
- GitHub APIのdiff/filesエンドポイントだけでpatchを得る: rename判定や省略の仕様がgitと異なり、hash付きの再現可能なbundleを保証しにくいため、patch生成はgitに統一する。
- 履歴全体をfetchしてgitでmerge-baseを計算する: fetch量が無制限になるため採用せず、SHA同士のcompare APIで求める。
- git credentialを`-c http.extraheader`や設定fileで渡す: argvや設定fileへtokenが残るため採用しない。
- AI出力をそのまま投稿する: HTML・mention・secretらしき値・不正pathの混入を防げないため採用しない。

## Consequences

- diff取得、schema、publisherの実装はPhase 1〜3で本ADRに従う。Phase 1の`prepare`は本ADRのSHA固定・diff取得方式に従って実装済みである。Phase 2の`review`は本ADRのCLI adapter方式に従って実装済みである。
- 公式Actionのgate再評価は、Actionがreview jobでGitHub tokenとcheckoutを必須としなくなった時点で行う。それまではCLI adapterを維持し、gateを緩める変更は行わない。
- CLI adapterはClaude Code CLIのflag仕様に依存する。version固定とdigest照合により、仕様変更が無警告で持ち込まれることは防げるが、version更新時は本ADRのflag一覧を再検証する必要がある。
- toolを一切持たせないため、モデルはbundleに含まれない周辺コードを参照できない。文脈不足による見落としは`limitations`として報告させ、レビューの完全性より境界の単純さを優先する。
- merge-baseの決定をGitHubのcompare APIへ依存する。APIが利用できない場合はレビューを停止する。
- 巨大diffや禁止pathの多いPRはレビューされない。上限値の調整はarchitectureを変えない限りADR対象外とする。
- `.env.example`のような無害なfileも禁止pattern（`.env*`）で除外される。名前だけで判定する代わりに内容判定を持たない。
- Action採用gateの検証結果によっては、Phase 2でCLI adapter案を提示して停止する。
- Claude Code CLI 2.1.263には`--restricted`（Bash等のcode実行toolとWebFetchを除去、file toolを作業ディレクトリへ限定、user/project/local settingsを無視）と`--safe-mode`が存在することをPhase 0で確認した。gate充足の実証はPhase 2で行う。

## References

- `docs/plan/implementation-plan.md` 5章、6章、7章、9章、11章、12章
- `scripts/prepare-review.py`、`scripts/lib/diff.py`、`scripts/lib/github.py`、`scripts/lib/limits.py`、`tests/test_prepare_review.py`
- `scripts/run-review.py`、`scripts/normalize-review.py`、`scripts/lib/bundle.py`、`prompts/review.md`、`schemas/review-result.schema.json`、`.github/workflows/claude-review.yml`、`tests/test_review_result.py`
- Claude Code CLI reference（code.claude.com/docs/en/cli-reference）、headless（同/headless）
- `anthropics/claude-code-action`の`action.yml`、`base-action/action.yml`、`docs/security.md`、`src/github/token.ts`、`src/modes/agent/index.ts`
- ADR-0001、ADR-0004
