# こえびん（Koebin）— Steam新着レビュー通知ツール

プレイヤーの声を、あなたのDiscordへ。

Steamでゲームを販売している開発者向けの、小さな通知ツールです。
指定したタイトルの新着レビューを定期的にチェックして、新しいレビューが投稿されたら Discord / Slack / Webhook に通知します。

by Axioknot

## 特徴

- 複数タイトルをまとめて監視できます
- 依存ライブラリなし（Python 3.9 以上があれば動きます）
- Steamの公開APIを使うため、APIキーやログインは不要です
- 初回実行時は既存レビューを通知しません（過去分で通知が溢れることはありません）
- 実行も状態の保存も、あなたのPCやサーバーの中だけで行われます。通知の内容は、あなたが設定した送信先（Discord / Slack / Webhook）にだけ送られ、Axioknotのサーバーを経由することはありません
- 通知の送信に失敗したレビューは既読にならず、次回の実行で失敗した送信先へだけ再送されます
- 取得したレビューは手元のデータベース（SQLite）に蓄積され、週次ダイジェストと低評価の急増アラートに使えます（下記の「分析層」）

## 使い方

1. このフォルダをダウンロードします
2. `config.example.json` を `config.json` という名前でコピーします
3. `config.json` を編集します
   - `app_ids`: 監視したいゲームのApp ID（SteamのストアURL `store.steampowered.com/app/<この数字>/` の数字です）
   - 通知先（いずれか1つ以上を設定します）
     - `discord_webhook_url`: Discordの「サーバー設定 → 連携サービス → ウェブフック」で発行したURL
     - `slack_webhook_url`: SlackのIncoming WebhookのURL
     - `webhook_url`: 任意のWebhookのURL（`{"text": ...}` 形式で送ります）
   - `interval_minutes`: `--loop` 実行時のチェック間隔（分）。初期値は30分です
   - `fetch_count`: 1回のチェックで取得する新着レビューの件数です。初期値は20です。前回のチェックからこの件数を超える新着があった場合は、古い方へページをたどって取りに行きます（最大10ページ。それでも届かない場合は警告を出します）
   - `state_file`: 既読の記録を保存するファイル名です。初期値は `state.json` で、`config.json` と同じフォルダに作られます
4. 実行します

```
python steam_review_notifier.py --config config.json          # 1回だけチェック
python steam_review_notifier.py --config config.json --loop   # 繰り返しチェック
python steam_review_notifier.py --config config.json --test   # テスト通知を1通送る
```

最初は `--test` で、設定した送信先にテスト通知が届くかを確認してください。

定期実行には、お使いのOSの仕組み（Windowsのタスクスケジューラ、Mac/Linuxのcronなど）で
`--loop` なしの実行を30分おきに登録する方法も使えます。
タスクスケジューラなどでは実行時のフォルダが一定しないため、`--config` には相対パスではなく
絶対パス（例：`--config C:\koebin\config.json`）を指定してください。
状態ファイル（`state_file`）と分析層のデータベース（`reviews.db`）は、`config.json` のあるフォルダに作られます。

> **注意：`config.json` は公開しないでください。** Webhook URLは、そのURLを知っている人なら誰でもそのチャンネルへ投稿できる鍵です。GitHubなどに上げてしまった場合は、Discord / Slack 側でWebhookを再発行してください。

## 通知の例

```
【新着レビュー】（ゲーム名）
👍 おすすめ｜言語: 日本語｜プレイ時間: 12.5時間｜2026-11-20 10:26
（レビュー本文の抜粋）
https://steamcommunity.com/profiles/（投稿者のSteamID）/recommended/（App ID）/
```

リンクはレビュー個別ページへの直リンクです（そのまま開発者返信を書きに行けます）。
プレイ時間は、レビューを書いた時点でのプレイ時間を表示します。

## 分析層（履歴・週次ダイジェスト・低評価の急増アラート）

取得したレビューは、設定ファイルと同じフォルダの `reviews.db`（SQLite）に蓄積されます。
蓄積した履歴から、次の2つが使えます。どちらも集計と整形はプログラムだけで行い、AIは使いません。

初回実行時には、過去分もさかのぼって履歴に取り込みます（初期値：直近90日・最大2,000件。
`config.json` の `analytics.backfill` の `days` / `max_reviews` で変更できます）。
取り込んだ過去分が通知されることはありません。履歴への取り込みだけです。
手動で取り込み直す場合は `python steam_review_analytics.py --config config.json backfill` を実行してください。
データベースのファイル名は `analytics.db_file` で変更できます（初期値は `reviews.db`）。
分析層を使わない場合は `analytics.enabled` を `false` にしてください。

### 週次ダイジェスト

直近7日のレビュー件数・おすすめ比率・前週比、言語の内訳、参考になった数上位のレビュー抜粋をまとめて送ります。

```
python steam_review_analytics.py --config config.json digest
```

週に1回、タスクスケジューラやcronで実行する想定です。`--dry-run` を付けると送信せず内容だけ表示します。

実例（サンプルデータで生成したものです）：

```
📊【週次ダイジェスト】フィクスチャゲーム（11/13〜11/20）
結論：好調です（おすすめ率 80%）
今週：10件（前週 5件）｜👍 8 / 👎 2｜おすすめ率 80%（前週 60%）
言語：日本語 5件、英語 3件、韓国語 2件
▼ 参考になったレビュー
1. 👍｜120時間｜参考になった 45件
「今週のレビュー0」
https://steamcommunity.com/profiles/76561198000000000/recommended/1234560/
（以下略）
```

### 低評価の急増アラート

通常のチェックのたびに自動で判定します。直近24時間の低評価の割合と件数が、
過去28日の平常時の水準を大きく上回ったときだけ、理由の数字と一緒にアラートを送ります。
しきい値は `config.json` の `analytics.surge` で変更できます
（`min_recent_reviews` 直近の件数の下限、`ratio_delta` 平常時をどれだけ上回ったら鳴らすか、など）。
同じタイトルへのアラートは24時間に1回までです。

### イベント前後の比較

アップデートのような「イベント」を境に、その前後でレビューの動きがどう変わったかを比べます。
週次ダイジェストに、直近のイベントについて「前（7日）vs 後（7日）」の件数・おすすめ率・言語の内訳を比べるセクションが付きます（イベントが無いタイトルでは付きません）。

イベントの出どころは、そのゲームの公式発表です。Steamの公式ニュースAPI
（ISteamNews/GetNewsForApp、`feeds=steam_community_announcements`）で発表を取得し、
タイトルに `update` `patch` `アップデート` などを含むものや、「1.6.14」のような
バージョン番号らしい文字列を含むものを「イベント候補」として自動で登録します
（通常のチェックのついでに、1日1回まで自動で取りに行きます）。
候補にするキーワードは `config.json` の `analytics.events.keywords` で変更でき、
`analytics.events.enabled` を `false` にすると、この機能全体を止められます。

手動での管理もできます。

```
python steam_review_analytics.py --config config.json sync-events   # 公式発表から候補を取り込む
python steam_review_analytics.py --config config.json events        # 登録済みイベントの一覧
python steam_review_analytics.py --config config.json add-event --appid 1234560 --date 2026-11-15 --title "大型アップデート"
python steam_review_analytics.py --config config.json remove-event --appid 1234560 --event-id feed-456
```

限界も正直に書いておきます。

- 告知のないアップデートは拾えません（その場合は `add-event` で足してください）
- 判定は告知のタイトルだけで行うため、実際のアップデートでない告知が候補に入ることがあります。あくまで「候補」として扱い、違うものは `remove-event` で消す前提です
- レビュー自体にはバージョン情報がないため、比較は「イベントの日付の前後」での集計です。数字の変化がそのイベントによるものかどうかまでは断定できません

### 通知の見た目（色分けの方針）

通知は「結論 → 数字 → 代表レビュー → リンク」の順で、3秒で要点が分かる並びにしています。

- 新着レビュー：見出しに評価（👍 おすすめ／👎 おすすめしない）を先に置きます
- 週次ダイジェスト：📊 を付け、1行目に結論（好調／安定／低下／低評価が目立つ）を置きます
- 急増アラート：🚨 を付け、1行目に「何が・どれだけ増えたか」を数字で置きます

ホスティング版（開発予定）では、Discordの埋め込み色も同じ意味で使い分けます：
おすすめ＝緑、低評価と急増＝赤、ダイジェスト＝青。

## 注意と運用について

- 本ツールはAxioknotの実験的プロダクトです。公開後90日の利用状況を見て、今後の保守を続けるかどうかを判断します。なお、判断の結果にかかわらず、入手済みの版はそのまま使い続けられます（保守や更新が止まるだけで、利用を止めるものではありません）
- 保守はSteam側の仕様変更への追随のみで、ベストエフォート（保証なし）です
- SteamのAPIの仕様上、取得できるのは直近のレビューです。チェック間隔が空きすぎると取りこぼす場合があります
- 2026年10月22日にSteamのレビューAPIが移行しました（旧エンドポイント `store.steampowered.com/appreviews` は廃止）。本ツールは新しいAPIに対応済みです。出典：[Changes to Steam User Reviews API（Steamworks Development 公式アナウンス）](https://store.steampowered.com/news/group/4145017/view/676258795703241580)
- 分析層の履歴は、初回の取り込み（直近90日・最大2,000件まで）と、本ツールを動かしていた期間に取得できたレビューです。それより古い分や、止めていた期間に流れた大量のレビューは蓄積されません。急増判定の材料（平常時の水準）が十分でない間は、アラートは出ません

## ライセンス

MIT License
