# ADR-0009: Canonical agent instructions and documentation policy

## Status

Accepted

Supersedes ADR-0003

## Context

ADR-0003は、`AGENTS.md`を共通project instructionsのSingle Source of Truthとし、`CLAUDE.md`をthin adapterとすること、Agent固有設定は強制方法だけを定義すること、そしてdocs生成禁止の例外として承認済みPlanとADRだけを認めることを決めた。

運用上、最後の点が問題になった。

- README更新を一律に禁止していたため、利用者向けの導入手順や仕組みの説明が実装と乖離しても更新できなかった。ADR-0005で構成が大きく変わり、READMEの記述（consumer wrapperの配置手順）は実態と矛盾する。
- 一方で、README更新を無条件に許可すると、AI Agentが大量の自動生成docsを追加する、あるいはSecretや内部限定情報を書く、という別の問題が生じる。
- 「docsを生成しない」というルールの範囲が曖昧で、「既存ファイルの更新」まで禁止されていると読めた。

`AGENTS.md`をSingle Source of Truthとする構造自体は維持したうえで、documentation policyを明確化する必要がある。

## Decision

### 維持する内容（ADR-0003から変更しない）

- `AGENTS.md`を共通project instructionsのSingle Source of Truthとする。Agent固有設定はルールの強制方法だけを定義し、ルールの意味は`AGENTS.md`に置く。
- `CLAUDE.md`はsymbolic linkではなくthin adapterとする。
- 詳細は`.ai/rules/security.md`、`.ai/rules/review.md`へ分離できるが、参照先が自動ロードされる前提を置かない。常時適用ルールは`AGENTS.md`本文に置く。
- Agent固有設定の強制方法（Claude Code: `.claude/settings.json`の`permissions`、Codex: `.codex/config.toml`と`.codex/rules/safety.rules`、OpenCode: `opencode.json`の`permission`オブジェクト）を維持する。
- 公式仕様で確認できないpermissionキーを作成しない。未確認の設定を防御策として扱わない。

### 変更する内容（documentation policy）

- **README更新を条件付きで許可する。** 利用者向け情報に実質的な変更がある場合（導入手順、実行方法、入力、出力、権限要件、制限値の変更など）はREADMEを更新してよい。
- **README更新は常に必須ではない。** 内部実装だけの変更、リファクタリング、テスト追加ではREADMEを更新しない。
- **READMEへSecret、credential、token、内部限定情報を書かない。** 実際のtoken値、Secret名以外の認証情報、社内限定のURL・identifier・運用手順は記載しない。Secretの「名前」と「必要な権限」の記載は許可する。
- **自動生成された大量docsの無条件追加を禁止する。** API reference一式、コードから機械生成した解説、章立てだけの空ドキュメント群をまとめて追加しない。
- **「docsを生成しない」の範囲を次のとおり明確化する。**
  - 禁止: 指示されていない新規ドキュメントtree（`docs/`配下の新カテゴリ、`README.md`以外の解説ファイル群、`docs/adr/README.md`のような索引ファイル）の作成。
  - 許可: 承認済みPlanの`docs/plan/`への保存、ADRの`docs/adr/`への作成と状態更新、既存`README.md`の更新、コード内コメントとdocstring。
- **承認済みPlan / ADRの管理規則を維持する。** 連番、番号の再利用禁止、Accepted ADRの理由を書き換えないこと、変更時は新ADRを作り`Supersedes ADR-XXXX`を記載して旧ADRを`Superseded`にすること、基本順序`Plan → Decision → ADR → Implementation`。
- Agent固有adapterとrules（`CLAUDE.md`、`.ai/rules/*`、`.codex/*`、`opencode.json`）は`AGENTS.md`と矛盾させない。`AGENTS.md`を先に更新し、必要な範囲でadapterを追従させる。

## Rationale

README更新の一律禁止は、「AIが勝手にドキュメントを増やすこと」を防ぐための手段であって、目的ではなかった。実際に防ぎたいのは、内容が薄い自動生成docsの氾濫と、機密情報の漏洩である。そのため、禁止の対象を「新規docs treeの無断作成」と「Secret・内部限定情報の記載」に絞り、利用者向けの実質的な変更に限ってREADME更新を許可する。

更新を「必須ではない」と明示するのは、必須化するとすべての変更にREADME差分が付き、意味のない更新が増えるためである。

## Alternatives Considered

- README更新の全面禁止を維持する: 実装と利用者向け情報の乖離が解消できない。ADR-0005で構成が変わった以上、現実的でない。
- README更新を無条件に許可する: 自動生成docsの氾濫と機密情報記載のリスクが戻る。
- README更新を必須にする: 内部変更でも差分が必要になり、意味のない更新が増える。
- documentation policyを`AGENTS.md`だけに書いてADRを作らない: ADR-0003のDecision（docs生成禁止の例外範囲）と矛盾するため、新ADRによるSupersedeが必要である。
- ADR-0003の該当節だけを書き換える: Accepted ADRの理由を後から書き換えないという規則に反するため採用しない。

## Consequences

- `AGENTS.md`のOutput節を更新し、README更新の条件と禁止事項を明記する。`AGENTS.md`が先、adapterが後という順序を維持する。
- READMEは中央実行方式の内容へ更新する。旧構成（consumer wrapperの配置手順）は削除する。
- 「利用者向け情報の実質的変更」の判断はAgentに委ねられる。判断がぶれた場合でも、禁止事項（Secret記載、大量docs追加）は具体的であるため、重大な逸脱は起きにくい。
- `tests/test_agent_config.py`のcanonical instructions検証を、新しいdocumentation policyに合わせて更新する。

## References

- `docs/plan/two-stage-cross-review-plan.md` 4.8節
- `AGENTS.md`、`CLAUDE.md`、`.ai/rules/review.md`、`.ai/rules/security.md`、`README.md`
- `tests/test_agent_config.py`
- ADR-0003（Superseded）、ADR-0005
