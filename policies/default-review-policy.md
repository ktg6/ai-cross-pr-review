# DEFAULT REVIEW POLICY (central fallback)

対象repositoryのdefault branchにreview policy（既定では`.github/ai-review.md`）が存在しない場合、中央実行側の本ファイルをpolicyとして使用する。本ファイルは中央実行repositoryと同一commitから読み込まれ、Pull Requestの内容によって変更されない。

本ファイルは、固定REVIEW POLICY（`prompts/review.md`、`prompts/codex-verify.md`）の禁止事項、出力形式、信頼順序を上書きしない。レビュー観点の補足だけを与える。

## 重点を置く観点

1. correctness: 変更された分岐・境界条件・例外処理・後方互換性。特にnull／空・型変換・オフバイワン。
2. security: 入力検証、injection、認証・認可、secretの取り扱い、権限昇格、安全でない既定値、依存の信頼境界。
3. reliability: エラー処理、timeout、retryの安全性（冪等でない操作の再試行）、資源解放、部分失敗時の状態。
4. testing: 変更に対するテストの欠落、テスト自体の誤り、失敗するはずのケースが通っていないか。
5. maintainability: 重大な重複、誤解を招く命名、明らかに不要な複雑さ。

## 報告しないもの

- 意味を変えないformatting、空白、行末、import順。
- 個人的な好みに基づくstyle。
- 変更されていないコードに対する一般的な改善提案。
- 同一原因に対する重複した指摘。

## 判断の基準

- diffに現れない行を根拠にしない。
- bundleに含まれない情報を推測して事実として述べない。確認できない点はlimitationsまたはinsufficient_contextとして報告する。
- 不確実な指摘はconfidenceを下げる。断定しない。
