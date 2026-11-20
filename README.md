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
- あなたのPCやサーバーの中で動きます。レビュー情報が外部のサーバーに送られることはありません

## 使い方

1. このフォルダをダウンロードします
2. `config.example.json` を `config.json` という名前でコピーします
3. `config.json` を編集します
   - `app_ids`: 監視したいゲームのApp ID（SteamのストアURL `store.steampowered.com/app/<この数字>/` の数字です）
   - 通知先（いずれか1つ以上）：`discord_webhook_url`（Discordの「サーバー設定 → 連携サービス → ウェブフック」で発行）、`slack_webhook_url`（SlackのIncoming Webhook）、`webhook_url`（任意のWebhookへ `{"text": ...}` 形式で送信）
   - `interval_minutes`: `--loop` 実行時のチェック間隔（分）。初期値は30分です
4. 実行します

```
python steam_review_notifier.py --config config.json          # 1回だけチェック
python steam_review_notifier.py --config config.json --loop   # 繰り返しチェック
```

定期実行には、お使いのOSの仕組み（Windowsのタスクスケジューラ、Mac/Linuxのcronなど）で
`--loop` なしの実行を30分おきに登録する方法も使えます。

## 通知の例

```
【新着レビュー】（ゲーム名）
👍 おすすめ｜言語: japanese｜プレイ時間: 12.5時間｜2026-11-20 10:26
（レビュー本文の抜粋）
https://store.steampowered.com/app/xxxxxx/
```

## 注意と運用について

- 本ツールはAxioknotの実験的プロダクトです。継続するかどうかは公開後90日の利用状況で判断します
- 保守はSteam側の仕様変更への追随のみで、ベストエフォート（保証なし）です
- SteamのAPIの仕様上、取得できるのは直近のレビューです。チェック間隔が空きすぎると取りこぼす場合があります
- 2026年10月にSteamのレビューAPIが移行しました（旧エンドポイントは廃止）。本ツールは新しいAPIに対応済みです

## ライセンス

MIT License
