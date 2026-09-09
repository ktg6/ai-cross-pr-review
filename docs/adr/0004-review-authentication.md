# ADR-0004: Review authentication

## Status

Proposed

## Context

review jobでClaude Codeを実行するには認証が必要である。選択肢はAPI key、subscription OAuth token、Workload Identity Federation（WIF）である。MVPは単一repositoryのpilotであり、運用負荷を抑えつつSecretの到達範囲を限定したい。

## Decision

- MVPでは`CLAUDE_CODE_OAUTH_TOKEN`をGitHub Actions Secretとして利用する。
- 管理者が`claude setup-token`で生成する。公式仕様上、これは1年有効のOAuth tokenであり、Pro/Max/Team/Enterprise planを要し、model requestのみ可能である。コマンドはtokenを保存せず端末へ出力する。
- token値、認証応答、環境変数一覧をログへ出さない。Secretはreview jobのClaude実行stepだけへ渡す。
- Secret所有者、更新担当、期限通知を決め、90日程度のrotation通知を運用案とする。旧tokenが自動失効するとは仮定しない。
- 認証方式はClaude実行stepへ閉じ込め、publisherやschemaから分離する。
- Phase 2の実装では、Secretはreusable workflowの`secrets.claude_code_oauth_token`からreview jobのClaude実行stepへだけ渡す。`prepare`と将来の`publish`へは渡さない。review jobは`permissions: {}`とし、GitHub tokenと同一jobに置かない。
- CLI adapterはClaude Code CLIへ最小の環境変数だけを構築して渡す。`CLAUDE_CODE_OAUTH_TOKEN`以外の認証情報、GitHub token、Actions関連変数は継承させない。tokenはargv、設定file、bundle、artifactへ書かない。
- モデル出力とstderrはログ・artifactへ出す前にcredentialらしき値を除去し、渡したtoken値そのものも除去対象に含める。
- 複数repository・組織運用ではAPI keyまたはWIFを再評価する。WIF導入時だけ`id-token: write`を追加し、audience・subject・repository・ref条件を限定する。

## Rationale

OAuth tokenは追加のConsole設定なしにpilotを開始でき、認証をstep単位に閉じ込めれば境界も維持できる。公式docsは組織横断の共有にはAPI keyまたはWIFを推奨しており、tokenは`setup-token`実行者のsubscriptionに紐づくため、MVP後の切り替え先として明記する。

## Alternatives Considered

- API key（Claude Console）: 組織運用向きだが、MVP pilotでは不要なConsole管理が増える。将来候補とする。
- WIF: 長期Secretを持たず最も望ましいが、`id-token: write`とConsole側設定が必要で、MVP範囲を超える。将来候補とする。
- `secrets: inherit`: Secretの到達範囲が広がるため採用しない。

## Consequences

- Phase 2でreview job実装時に本ADRに従いSecretをstep限定で渡す。
- OAuth tokenの明示的な失効手順は、Phase 2で再確認しても公式docsの参照範囲では確認できなかった。`claude setup-token`はtokenを保存せず端末へ出力するだけであり、`/logout`が撤回するのは端末に保存されたlogin credentialである。したがってrotationは「新token発行 + 旧Secret削除」として運用し、旧tokenが自動失効するとは仮定しない。有効期間は公式docsどおり1年として扱う。
- `--bare`モードは`CLAUDE_CODE_OAUTH_TOKEN`を読まないため、Phase 2のCLI adapter案では`--bare`を使わない。

## References

- `docs/plan/implementation-plan.md` 8章
- Claude Code authentication docs（code.claude.com/docs/en/authentication）「Generate a long-lived token」
- Claude Code GitHub Actions docs（code.claude.com/docs/en/github-actions）
- ADR-0002
