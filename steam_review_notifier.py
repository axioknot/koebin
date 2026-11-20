#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam新着レビュー通知ツール（最小版） by Axioknot

指定したSteamタイトルの新着レビューを定期的にチェックし、
新しいレビューが投稿されたら Discord Webhook へ日本語で通知します。

- 依存ライブラリなし（Python 3.9+ 標準ライブラリのみ）
- 初回実行時は既存レビューを「既読」として記録するだけで通知しません（通知洪水の防止）
- 状態は state.json に保存されます
- Steamの公開レビューAPIを使うため、APIキーは不要です

使い方:
  1. config.example.json を config.json にコピーして編集する
  2. python steam_review_notifier.py --config config.json        （1回だけチェック）
  3. python steam_review_notifier.py --config config.json --loop （設定した間隔で繰り返し）
"""

import argparse
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

STEAM_REVIEWS_URL = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
USER_AGENT = "steam-review-notifier/0.1 (+https://x.com/axioknot_jp)"


def http_get_json(url, params):
    qs = "&".join(
        "{}={}".format(k, urllib.request.quote(str(v), safe=""))
        for k, v in params.items()
    )
    req = urllib.request.Request(
        "{}?{}".format(url, qs),
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = resp.read().decode("utf-8")
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("Steam APIから想定外の応答が返りました（JSONオブジェクトではありません）")
    return data


def http_post_json(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def fetch_app_name(appid, base_url=None):
    """ゲーム名を取得する。失敗したら appid 表記にフォールバックする。"""
    try:
        url = base_url or STEAM_APPDETAILS_URL
        data = http_get_json(url, {"appids": appid})
        entry = data.get(str(appid), {})
        if entry.get("success"):
            return entry["data"].get("name", "App {}".format(appid))
    except Exception:
        pass
    return "App {}".format(appid)


def fetch_recent_reviews(appid, num=20, base_url=None):
    """新着順にレビューを取得する。

    2026-10-22に旧エンドポイント（store.steampowered.com/appreviews）が廃止され、
    IUserReviewsService/GetAppReviews に移行した。パラメータは input_json で渡し、
    filter は数値（1=Recent）、language は languages（配列）、purchase_type は数値（1=All）。
    レスポンスに success フィールドはない（HTTPステータスで判定する）。
    """
    url = base_url or STEAM_REVIEWS_URL
    input_json = json.dumps(
        {
            "appid": appid,
            "filter": 1,
            "languages": ["all"],
            "purchase_type": 1,
            "num_per_page": num,
            "cursor": "*",
        }
    )
    data = http_get_json(url, {"input_json": input_json})
    # 実際の応答は {"response": {"reviews": [...]}} の形で包まれている（2026-11-20 実地確認）
    body = data.get("response", data)
    return body.get("reviews", [])


def format_review_message(app_name, appid, review):
    author = review.get("author", {})
    playtime_h = author.get("playtime_forever", 0) / 60.0
    verdict = "👍 おすすめ" if review.get("voted_up") else "👎 おすすめしない"
    lang = review.get("language", "")
    text = (review.get("review") or "").strip().replace("\n", " ")
    if len(text) > 300:
        text = text[:300] + "…"
    ts = review.get("timestamp_created", 0)
    when = ""
    if ts:
        when = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    lines = [
        "**【新着レビュー】{}**".format(app_name),
        "{}｜言語: {}｜プレイ時間: {:.1f}時間｜{}".format(verdict, lang, playtime_h, when),
        text,
        "https://store.steampowered.com/app/{}/".format(appid),
    ]
    return "\n".join(lines)


def load_state(path):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_state(path, state):
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def check_once(config, state_path, notify):
    """1回分のチェック。通知した件数を返す。"""
    state = load_state(state_path)
    total_notified = 0
    base_url = config.get("_reviews_base_url")  # テスト用に差し替え可能（通常は未設定）
    for appid in config.get("app_ids", []):
        appid = str(appid)
        try:
            reviews = fetch_recent_reviews(
                appid, num=int(config.get("fetch_count", 20)), base_url=base_url
            )
        except Exception as e:
            print("[警告] appid={} のレビュー取得に失敗しました: {}".format(appid, e), file=sys.stderr)
            continue
        current_ids = [r.get("recommendationid") for r in reviews]
        if appid not in state:
            # 初回: 既存レビューは通知せず、既読として記録するだけ
            state[appid] = current_ids
            print("[初期化] appid={}: 既存レビュー {} 件を既読として記録しました".format(appid, len(current_ids)))
            continue
        seen = set(state.get(appid, []))
        app_name = None
        for review in reversed(reviews):  # 古い順に通知する
            rid = review.get("recommendationid")
            if rid and rid not in seen:
                if app_name is None:
                    app_name = fetch_app_name(appid, base_url=config.get("_appdetails_base_url"))
                msg = format_review_message(app_name, appid, review)
                notify(msg)
                total_notified += 1
                seen.add(rid)
        merged = [r for r in current_ids if r] + [s for s in state[appid] if s not in current_ids]
        state[appid] = merged[:200]
    save_state(state_path, state)
    return total_notified


def make_discord_notifier(webhook_url):
    def notify(message):
        http_post_json(webhook_url, {"content": message[:1900]})

    return notify


def make_slack_notifier(webhook_url):
    def notify(message):
        http_post_json(webhook_url, {"text": message[:2900]})

    return notify


def make_generic_notifier(webhook_url):
    def notify(message):
        http_post_json(webhook_url, {"text": message})

    return notify


def build_notifier(config):
    """設定にある通知先をすべて束ねる。1つの失敗で他を止めない。"""
    notifiers = []
    if config.get("discord_webhook_url", "").strip():
        notifiers.append(("Discord", make_discord_notifier(config["discord_webhook_url"].strip())))
    if config.get("slack_webhook_url", "").strip():
        notifiers.append(("Slack", make_slack_notifier(config["slack_webhook_url"].strip())))
    if config.get("webhook_url", "").strip():
        notifiers.append(("Webhook", make_generic_notifier(config["webhook_url"].strip())))
    if not notifiers:
        return None

    def notify(message):
        for name, fn in notifiers:
            try:
                fn(message)
            except Exception as e:
                print("[警告] {} への通知に失敗しました: {}".format(name, e), file=sys.stderr)

    return notify


def main():
    parser = argparse.ArgumentParser(description="Steam新着レビュー通知ツール（最小版）")
    parser.add_argument("--config", default="config.json", help="設定ファイルのパス")
    parser.add_argument("--loop", action="store_true", help="設定した間隔で繰り返し実行する")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(
            "設定ファイルが見つかりません: {}\nconfig.example.json をコピーして作成してください。".format(config_path),
            file=sys.stderr,
        )
        return 1
    config = json.loads(config_path.read_text(encoding="utf-8"))

    notify = build_notifier(config)
    if notify is None:
        print(
            "通知先が未設定です。config.json に discord_webhook_url / slack_webhook_url / webhook_url のいずれかを設定してください。",
            file=sys.stderr,
        )
        return 1
    state_path = config_path.parent / config.get("state_file", "state.json")
    interval = int(config.get("interval_minutes", 30))

    while True:
        n = check_once(config, state_path, notify)
        print("{} チェック完了（新規通知 {} 件）".format(datetime.now().strftime("%H:%M:%S"), n))
        if not args.loop:
            break
        time.sleep(max(interval, 5) * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
