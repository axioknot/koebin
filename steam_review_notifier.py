#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
こえびん（Koebin）— Steam新着レビュー通知ツール by Axioknot

指定したSteamタイトルの新着レビューを定期的にチェックし、
新しいレビューが投稿されたら Discord / Slack / 汎用 Webhook へ日本語で通知します。

- 依存ライブラリなし（Python 3.9+ 標準ライブラリのみ）
- 初回実行時は既存レビューを「既読」として記録するだけで通知しません（通知洪水の防止）
- 通知の送信に失敗したレビューは既読にせず、次回実行時に失敗した送信先へだけ再送します
- 状態は state.json に保存されます（書き込みは一時ファイル経由で原子的に行います）
- Steamの公開レビューAPI（IUserReviewsService/GetAppReviews）を使うため、APIキーは不要です
- 分析層（steam_review_analytics.py）と併用すると、履歴の蓄積・週次ダイジェスト・
  低評価の急増アラートに加え、致命語の即時アラート、評価ラベルの目安アラート、
  セール開始/終了の自動記録、同時プレイヤー数の記録などが使えます

使い方:
  1. config.example.json を config.json にコピーして編集する
  2. python steam_review_notifier.py --config config.json        （1回だけチェック）
  3. python steam_review_notifier.py --config config.json --loop （設定した間隔で繰り返し）
  4. python steam_review_notifier.py --config config.json --test （テスト通知を1通送る）
"""

import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

STEAM_REVIEWS_URL = "https://api.steampowered.com/IUserReviewsService/GetAppReviews/v1/"
STEAM_APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
STEAM_NEWS_URL = "https://api.steampowered.com/ISteamNews/GetNewsForApp/v2/"
STEAM_PLAYERS_URL = "https://api.steampowered.com/ISteamUserStats/GetNumberOfCurrentPlayers/v1/"
STEAM_ACHIEVEMENTS_URL = "https://api.steampowered.com/ISteamUserStats/GetGlobalAchievementPercentagesForApp/v2/"
STEAM_HISTOGRAM_URL = "https://store.steampowered.com/appreviewhistogram"
USER_AGENT = "Koebin/0.2 (+https://x.com/axioknot_jp)"

PENDING_KEY = "__pending__"  # state.json 内で「送信に失敗して再送待ち」のレビューを置くキー
LOCK_STALE_SEC = 6 * 3600     # ロックファイルがこの時間より古ければ残骸とみなす
MAX_FETCH_PAGES = 10          # ページングでたどる最大ページ数

# Steamの言語コード → 日本語表示名（steam_review_analytics.py と内容をそろえること）
LANGUAGE_NAMES = {
    "japanese": "日本語",
    "english": "英語",
    "schinese": "中国語（簡体字）",
    "tchinese": "中国語（繁体字）",
    "koreana": "韓国語",
    "spanish": "スペイン語",
    "latam": "スペイン語（中南米）",
    "french": "フランス語",
    "german": "ドイツ語",
    "russian": "ロシア語",
    "portuguese": "ポルトガル語",
    "brazilian": "ポルトガル語（ブラジル）",
    "thai": "タイ語",
    "vietnamese": "ベトナム語",
    "indonesian": "インドネシア語",
    "italian": "イタリア語",
    "polish": "ポーランド語",
    "turkish": "トルコ語",
    "ukrainian": "ウクライナ語",
    "dutch": "オランダ語",
}


def language_name(code):
    return LANGUAGE_NAMES.get(code or "", code or "不明")


# 分析層（履歴の蓄積・急増検知・バックフィル）は任意モジュール。無くても通知は動く。
try:
    from steam_review_analytics import (
        DEFAULT_EVENTS,
        ReviewStore,
        backfill_history,
        detect_sale_event,
        evaluate_and_alert,
        import_review_histogram,
        match_fatal_keyword,
        open_store,
        sync_feed_events,
    )
except Exception:
    DEFAULT_EVENTS = {}
    ReviewStore = None
    backfill_history = None
    detect_sale_event = None
    evaluate_and_alert = None
    import_review_histogram = None
    match_fatal_keyword = None
    open_store = None
    sync_feed_events = None


def http_get_json(url, params):
    qs = urllib.parse.urlencode(params)
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
    """ゲーム名を日本語で取得する。失敗したら appid 表記にフォールバックする。"""
    try:
        url = base_url or STEAM_APPDETAILS_URL
        data = http_get_json(url, {"appids": appid, "l": "japanese"})
        entry = data.get(str(appid), {})
        if entry.get("success"):
            return entry["data"].get("name", "App {}".format(appid))
    except Exception:
        pass
    return "App {}".format(appid)


def fetch_reviews_page(appid, cursor="*", num=100, base_url=None):
    """レビューを1ページ分取得する。(レビューのリスト, 次ページのカーソル) を返す。

    2026-10-22に旧エンドポイント（store.steampowered.com/appreviews）が廃止され、
    IUserReviewsService/GetAppReviews に移行した。パラメータは input_json で渡し、
    filter は数値（1=Recent）、language は languages（配列）、purchase_type は数値（1=All）。
    レスポンスに success フィールドはない（HTTPステータスで判定する）。

    HTTP 200 でも `reviews` キーが無い応答は「新着0件」ではなくエラーとして扱う
    （仕様変更やエラー応答を静かに飲み込むと、初回に空のベースラインが記録され、
    次回に過去分が一斉通知される事故になるため）。
    """
    url = base_url or STEAM_REVIEWS_URL
    input_json = json.dumps(
        {
            "appid": appid,
            "filter": 1,
            "languages": ["all"],
            "purchase_type": 1,
            "num_per_page": num,
            "cursor": cursor,
        }
    )
    data = http_get_json(url, {"input_json": input_json})
    # 実際の応答は {"response": {"reviews": [...]}} の形で包まれている（2026-11-20 実地確認）
    body = data.get("response", data)
    if not isinstance(body, dict) or "reviews" not in body:
        raise ValueError(
            "Steam APIの応答に reviews が含まれていません（仕様変更やエラーの可能性があります）"
        )
    return body.get("reviews") or [], body.get("cursor")


def fetch_recent_reviews(appid, num=20, base_url=None):
    """新着順にレビューを1ページ分取得する。"""
    reviews, _ = fetch_reviews_page(appid, cursor="*", num=num, base_url=base_url)
    return reviews


def fetch_announcements(appid, count=20, base_url=None):
    """公式発表（Steam Community Announcements）を取得する。APIキーは不要。

    ISteamNews/GetNewsForApp に feeds=steam_community_announcements を指定すると、
    そのゲームの公式発表が日付つきで返る（アップデート告知も、それ以外も混ざる）。
    返り値は newsitems のリスト（各要素に gid / title / url / date が入る）。
    """
    url = base_url or STEAM_NEWS_URL
    data = http_get_json(
        url,
        {
            "appid": appid,
            "count": count,
            "maxlength": 0,
            "feeds": "steam_community_announcements",
        },
    )
    appnews = data.get("appnews", {})
    if not isinstance(appnews, dict):
        raise ValueError("Steam News APIから想定外の応答が返りました")
    return appnews.get("newsitems", []) or []


def fetch_reviews_until(appid, known_ids=None, page_size=20, max_pages=MAX_FETCH_PAGES,
                        base_url=None):
    """カーソルでページングしながら、前回見た位置までさかのぼって取得する。

    (レビューのリスト, 前回位置に追いつけたか) を返す。
    known_ids が None のときは1ページだけ取得して追いつけたものとする。
    最大ページ数までたどっても既知のレビューに届かない場合は False を返し、
    呼び出し側で「取りこぼしの可能性」を警告できるようにする。
    """
    reviews = []
    seen_rids = set()
    caught_up = not known_ids  # 既知が無い/空なら1ページで十分
    cursor = "*"
    for _ in range(max(1, max_pages)):
        page_reviews, next_cursor = fetch_reviews_page(
            appid, cursor=cursor, num=page_size, base_url=base_url
        )
        for r in page_reviews:
            rid = r.get("recommendationid")
            if rid in seen_rids:
                continue
            seen_rids.add(rid)
            reviews.append(r)
            if known_ids and rid in known_ids:
                caught_up = True
        if caught_up:
            break
        if not page_reviews or not next_cursor or next_cursor == cursor:
            caught_up = True  # これ以上古いページが無い＝末尾まで到達した
            break
        cursor = next_cursor
    return reviews, caught_up


def fetch_query_summary(appid, base_url=None):
    """query_summary（総レビュー数・おすすめ数・公式の評価ラベル等）を取る。失敗時は None。

    レビュー取得と同じエンドポイントに num_per_page=1 で問い合わせる。
    """
    try:
        url = base_url or STEAM_REVIEWS_URL
        input_json = json.dumps(
            {
                "appid": appid,
                "filter": 1,
                "languages": ["all"],
                "purchase_type": 1,
                "num_per_page": 1,
                "cursor": "*",
            }
        )
        data = http_get_json(url, {"input_json": input_json})
        body = data.get("response", data)
        if not isinstance(body, dict):
            return None
        qs = body.get("query_summary")
        return qs if isinstance(qs, dict) else None
    except Exception:
        return None


def fetch_discount_percent(appid, base_url=None):
    """appdetails（filters=price_overview, cc=jp）から割引率（%）を取る。

    無料ゲームなど price_overview が無い場合や取得失敗時は None
    （「割引なし＝0%」と区別するため）。
    """
    try:
        url = base_url or STEAM_APPDETAILS_URL
        data = http_get_json(
            url, {"appids": appid, "filters": "price_overview", "cc": "jp"}
        )
        entry = data.get(str(appid), {})
        if not entry.get("success"):
            return None
        overview = (entry.get("data") or {}).get("price_overview")
        if not isinstance(overview, dict):
            return None
        return int(overview.get("discount_percent", 0) or 0)
    except Exception:
        return None


def fetch_current_players(appid, base_url=None):
    """同時プレイヤー数の現在値を取る（キー不要）。失敗時は None。"""
    try:
        url = base_url or STEAM_PLAYERS_URL
        data = http_get_json(url, {"appid": appid})
        resp = data.get("response", {})
        if int(resp.get("result", 0) or 0) != 1:
            return None
        return int(resp.get("player_count", 0) or 0)
    except Exception:
        return None


def fetch_app_metadata(appid, base_url=None):
    """appdetails の基本情報を、履歴保存用に要約した辞書で取る。失敗時は None。"""
    try:
        url = base_url or STEAM_APPDETAILS_URL
        data = http_get_json(url, {"appids": appid, "l": "japanese"})
        entry = data.get(str(appid), {})
        if not entry.get("success"):
            return None
        d = entry.get("data") or {}
        po = d.get("price_overview") or {}
        return {
            "name": d.get("name"),
            "developers": d.get("developers") or [],
            "publishers": d.get("publishers") or [],
            "genres": [g.get("description") for g in d.get("genres") or []],
            "categories": [c.get("description") for c in d.get("categories") or []],
            "release_date": (d.get("release_date") or {}).get("date"),
            "supported_languages": d.get("supported_languages"),
            "price": (
                {
                    "currency": po.get("currency"),
                    "initial": po.get("initial"),
                    "final": po.get("final"),
                    "discount_percent": po.get("discount_percent"),
                }
                if po else None
            ),
            "metacritic": (d.get("metacritic") or {}).get("score"),
            "recommendations": (d.get("recommendations") or {}).get("total"),
        }
    except Exception:
        return None


def fetch_review_histogram(appid, base_url=None):
    """レビュー件数の推移（ヒストグラム）から rollups を取る。失敗時は None。

    store.steampowered.com/appreviewhistogram/<appid> が返す月次（粒度は
    タイトル・期間で変わり得る）の集計で、各要素に date（UNIX時刻）と
    recommendations_up / recommendations_down が入る。レビュー個別の
    データではなく、件数の集計だけが返る。
    このエンドポイントは公式ドキュメントに載らない非公式寄りのもので、
    仕様が変わり得るため、取れなくても本体は止めない前提で扱う。
    """
    try:
        base = (base_url or STEAM_HISTOGRAM_URL).rstrip("/")
        data = http_get_json("{}/{}".format(base, appid), {"l": "english"})
        if int(data.get("success", 0) or 0) != 1:
            return None
        results = data.get("results")
        if not isinstance(results, dict):
            return None
        rollups = results.get("rollups")
        return rollups if isinstance(rollups, list) else None
    except Exception:
        return None


def fetch_global_achievements(appid, base_url=None):
    """実績の全体達成率（全実績の name / percent のリスト）を取る。失敗時は None。"""
    try:
        url = base_url or STEAM_ACHIEVEMENTS_URL
        data = http_get_json(url, {"gameid": appid})
        achievements = (data.get("achievementpercentages") or {}).get("achievements")
        return achievements if isinstance(achievements, list) else None
    except Exception:
        return None


# ===== 評価ラベルの目安と陥落アラート =====
# Steam の表示ラベルは、キー経由レビューの除外など細かい仕様で決まるため、
# ここでの計算はすべて「目安」。公式ラベル（query_summary.review_score_desc）が
# 取れた場合は、その帯域の下限（おすすめ率）を使って残り件数を計算する。
# 帯域の下限は Steamworks ドキュメントに公開されているレビュースコアの
# 一般的な目安（圧倒的に好評=95%、非常に好評/好評=80%、やや好評=70%、
# 賛否両論=40%、やや不評=20%）に基づく。

OFFICIAL_LABEL_FLOORS = {
    "Overwhelmingly Positive": 0.95,
    "Very Positive": 0.80,
    "Positive": 0.80,
    "Mostly Positive": 0.70,
    "Mixed": 0.40,
    "Mostly Negative": 0.20,
    "Negative": 0.0,
    "Very Negative": 0.0,
    "Overwhelmingly Negative": 0.0,
}

OFFICIAL_LABEL_JA = {
    "Overwhelmingly Positive": "圧倒的に好評",
    "Very Positive": "非常に好評",
    "Positive": "好評",
    "Mostly Positive": "やや好評",
    "Mixed": "賛否両論",
    "Mostly Negative": "やや不評",
    "Negative": "不評",
    "Very Negative": "非常に不評",
    "Overwhelmingly Negative": "圧倒的に不評",
}


def estimate_review_label(total, positive):
    """総件数とおすすめ数から表示ラベルの目安を (ラベル名, 帯域のおすすめ率の下限) で返す。

    レビューが10件未満では Steam はラベルを表示しないため None。
    """
    if not total or total < 10:
        return None
    ratio = positive / total
    if ratio >= 0.80:
        if ratio >= 0.95 and total >= 500:
            return ("圧倒的に好評", 0.95)
        if total >= 50:
            return ("非常に好評", 0.80)
        return ("好評", 0.80)
    if ratio >= 0.70:
        return ("やや好評", 0.70)
    if ratio >= 0.40:
        return ("賛否両論", 0.40)
    if ratio >= 0.20:
        return ("やや不評", 0.20)
    if total >= 500:
        return ("圧倒的に不評", 0.0)
    if total >= 50:
        return ("非常に不評", 0.0)
    return ("不評", 0.0)


def negatives_until_label_drop(total, positive, official_desc=None):
    """あと何件の低評価で評価ラベルが下がるか（目安）を計算する。

    公式ラベルがあればその帯域の下限を使い、無ければ総数とおすすめ率から推定する。
    計算できない・下限が0の帯域なら None。戻り値は根拠を含む辞書。
    """
    if not total:
        return None
    label = None
    floor = None
    if official_desc and official_desc in OFFICIAL_LABEL_FLOORS:
        floor = OFFICIAL_LABEL_FLOORS[official_desc]
        label = OFFICIAL_LABEL_JA[official_desc]
    else:
        est = estimate_review_label(total, positive)
        if est is None:
            return None
        label, floor = est
    if floor <= 0:
        return None
    # positive / (total + n) < floor となる最小の n
    n = int(math.floor(positive / floor - total)) + 1
    if n < 1:
        return None
    new_est = estimate_review_label(total + n, positive)
    next_label = new_est[0] if new_est else None
    if next_label is None or next_label == label:
        return None
    return {
        "count": n,
        "current_label": label,
        "next_label": next_label,
        "floor": floor,
        "total": total,
        "positive": positive,
        "official_desc": official_desc,
    }


def format_label_alert(app_name, appid, info):
    name = app_name or "App {}".format(appid)
    if info.get("official_desc"):
        first = "Steam公式の評価ラベルは「{}」です。".format(info["current_label"])
    else:
        first = "評価ラベルの目安は「{}」です。".format(info["current_label"])
    lines = [
        "⚠【評価ラベルの目安】{}".format(name),
        "{}あと低評価{}件で、おすすめ率が目安の{:.0f}%を下回り、"
        "「{}」へ下がる可能性があります（目安）".format(
            first, info["count"], info["floor"] * 100, info["next_label"]
        ),
        "現在の合計：{}件（おすすめ {}件）".format(info["total"], info["positive"]),
        "※Steamのラベルはキー経由レビューの扱いなど細かい仕様で決まるため、この件数は目安です",
        "https://store.steampowered.com/app/{}/".format(appid),
    ]
    return "\n".join(lines)


LABEL_ALERT_KEY = "__label_alert__"  # state.json 内の重複防止マーカー置き場


def _maybe_label_alert(config, state, appid, app_name, notify, base_url=None,
                       query_summary=None):
    """評価ラベルが下がるまでの残り件数が少なくなったら知らせる（目安）。

    同じ状況（ラベルと残り件数の組）では繰り返し送らない。
    """
    cfg = config.get("label_alert", {}) or {}
    # 評価ラベルの陥落アラートは初期状態では無効（目安の計算がずれて
    # 誤報になる可能性があるため）。使う場合は label_alert.enabled を true にする
    if cfg.get("enabled", False) is not True:
        return
    try:
        warn_within = int(cfg.get("warn_within", 5))
    except (TypeError, ValueError):
        warn_within = 5
    qs = query_summary
    if qs is None:
        qs = fetch_query_summary(appid, base_url=base_url)
    if not qs:
        return
    try:
        total = int(qs.get("total_reviews", 0) or 0)
        positive = int(qs.get("total_positive", 0) or 0)
    except (TypeError, ValueError):
        return
    info = negatives_until_label_drop(total, positive, qs.get("review_score_desc"))
    markers = state.setdefault(LABEL_ALERT_KEY, {})
    if info is None or info["count"] > warn_within:
        markers.pop(appid, None)
        return
    marker = "{}:{}".format(info["current_label"], info["count"])
    if markers.get(appid) == marker:
        return
    name = app_name or fetch_app_name(
        appid, base_url=config.get("_appdetails_base_url")
    )
    results = deliver(notify, format_label_alert(name, appid, info))
    if results and any(results.values()):
        markers[appid] = marker


def format_review_message(app_name, appid, review, translate_link=True):
    author = review.get("author", {}) or {}
    minutes = author.get("playtime_at_review")
    if minutes is None:
        minutes = author.get("playtime_forever", 0)
    playtime_h = (minutes or 0) / 60.0
    verdict = "👍 おすすめ" if review.get("voted_up") else "👎 おすすめしない"
    lang = language_name(review.get("language", ""))
    text = (review.get("review") or "").strip().replace("\n", " ")
    if len(text) > 300:
        text = text[:300] + "…"
    ts = review.get("timestamp_created", 0)
    when = ""
    if ts:
        when = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M")
    steamid = author.get("steamid")
    if steamid:
        # レビュー個別ページ（開発者がそのまま返信しに行けるリンク）
        link = "https://steamcommunity.com/profiles/{}/recommended/{}/".format(steamid, appid)
    else:
        link = "https://store.steampowered.com/app/{}/".format(appid)
    lines = [
        "**【新着レビュー】{}**".format(app_name),
        "{}｜言語: {}｜プレイ時間: {:.1f}時間｜{}".format(verdict, lang, playtime_h, when),
        text,
        link,
    ]
    # 日本語以外のレビューには、機械翻訳ページへのリンクを付ける
    # （リンク先は外部サービス。運営側にコストはかからない）
    if translate_link and (review.get("language") or "") not in ("", "japanese"):
        raw = (review.get("review") or "").strip()
        if raw:
            snippet = raw[:250]
            turl = (
                "https://translate.google.com/?sl=auto&tl=ja&text={}&op=translate"
                .format(urllib.parse.quote(snippet))
            )
            lines.append("翻訳で読む: {}".format(turl))
    return "\n".join(lines)


def format_fatal_message(app_name, appid, review, keyword):
    """致命語を含むレビューの即時アラート文。"""
    author = review.get("author", {}) or {}
    verdict = "👍 おすすめ" if review.get("voted_up") else "👎 おすすめしない"
    text = (review.get("review") or "").strip().replace("\n", " ")
    if len(text) > 200:
        text = text[:200] + "…"
    steamid = author.get("steamid")
    if steamid:
        link = "https://steamcommunity.com/profiles/{}/recommended/{}/".format(steamid, appid)
    else:
        link = "https://store.steampowered.com/app/{}/".format(appid)
    lines = [
        "⚠【致命的な報告の可能性】{}".format(app_name),
        "キーワード「{}」を含むレビューが投稿されました（{}）"
        "※キーワード一致による自動検知です".format(keyword, verdict),
        text,
        link,
    ]
    return "\n".join(lines)


def _fatal_alerts_cfg(config):
    """致命語アラートの設定を返す。無効なら None。

    初期状態では無効（キーワード一致ベースで誤報があり得るため）。
    analytics.fatal_alerts.enabled が明示的に true の場合だけ有効になる。
    """
    cfg = (config.get("analytics", {}) or {}).get("fatal_alerts", {}) or {}
    if cfg.get("enabled", False) is not True:
        return None
    return cfg


class MultiNotifier:
    """複数の送信先を束ねる。送信先ごとに成功・失敗を記録して返す。"""

    def __init__(self, destinations):
        # destinations: [(表示名, 送信関数), ...]
        self.destinations = list(destinations)

    def names(self):
        return [name for name, _ in self.destinations]

    def send(self, message, only=None):
        """送信する。only を渡すとその送信先にだけ送る。{送信先名: 成功か} を返す。"""
        results = {}
        for name, fn in self.destinations:
            if only is not None and name not in only:
                continue
            try:
                fn(message)
                results[name] = True
            except Exception as e:
                print("[警告] {} への通知に失敗しました: {}".format(name, e), file=sys.stderr)
                results[name] = False
        return results

    def __call__(self, message):
        """従来の notify(message) 互換。全送信先に送り、全成功なら True。"""
        results = self.send(message)
        return bool(results) and all(results.values())


def deliver(notify, message, only=None):
    """通知を送り、{送信先名: 成功か} を返す。

    notify が MultiNotifier なら送信先ごとに判定する。素の関数なら
    "default" という1つの送信先として扱う（例外が出たら失敗）。
    """
    if isinstance(notify, MultiNotifier):
        return notify.send(message, only=only)
    if only is not None and "default" not in only:
        return {}
    try:
        notify(message)
        return {"default": True}
    except Exception as e:
        print("[警告] 通知に失敗しました: {}".format(e), file=sys.stderr)
        return {"default": False}


def make_discord_notifier(webhook_url):
    def notify(message):
        # allowed_mentions を空にし、レビュー本文中の @everyone 等が効かないようにする
        http_post_json(
            webhook_url,
            {"content": message[:1900], "allowed_mentions": {"parse": []}},
        )

    return notify


def to_slack_format(message):
    """Slack の mrkdwn では太字は *1つ* で書く。Discord 形式の ** を * に直す。"""
    return message.replace("**", "*")


def to_plain_format(message):
    """汎用 Webhook 向けに、Markdown の太字記号を外したプレーンな文にする。"""
    return message.replace("**", "")


def make_slack_notifier(webhook_url):
    def notify(message):
        http_post_json(webhook_url, {"text": to_slack_format(message)[:2900]})

    return notify


def make_generic_notifier(webhook_url):
    def notify(message):
        http_post_json(webhook_url, {"text": to_plain_format(message)})

    return notify


def build_notifier(config):
    """設定にある通知先をすべて束ねた MultiNotifier を返す。未設定なら None。"""
    destinations = []
    if (config.get("discord_webhook_url") or "").strip():
        destinations.append(("Discord", make_discord_notifier(config["discord_webhook_url"].strip())))
    if (config.get("slack_webhook_url") or "").strip():
        destinations.append(("Slack", make_slack_notifier(config["slack_webhook_url"].strip())))
    if (config.get("webhook_url") or "").strip():
        destinations.append(("Webhook", make_generic_notifier(config["webhook_url"].strip())))
    if not destinations:
        return None
    return MultiNotifier(destinations)


def _alert_notify(notify):
    """急増アラート用のラッパ。全送信先に失敗したときだけ例外にする。

    （分析層は「送れたら最終送信時刻を記録する」ため、全失敗を例外として伝え、
    次回に再判定・再送できるようにする。1つでも届けば成功扱い。）
    """

    def _send(message):
        results = deliver(notify, message)
        if results and not any(results.values()):
            raise RuntimeError("全送信先へのアラート送信に失敗しました")
        return True

    return _send


def _maybe_surge_alert(config, store, appid, app_name, notify):
    """分析層が有効なら低評価の急増を判定し、必要ならアラートを送る。"""
    if store is None or evaluate_and_alert is None:
        return
    try:
        evaluate_and_alert(store, config, appid, app_name, _alert_notify(notify))
    except Exception as e:
        print("[警告] appid={} の急増検知に失敗しました: {}".format(appid, e), file=sys.stderr)


def _maybe_backfill(config, store, appid):
    """未実施なら初回のバックフィル（過去分の取り込み）を1回だけ行う。"""
    if store is None or backfill_history is None:
        return
    try:
        if store.get_meta("backfill:{}".format(appid)) is not None:
            return
        base_url = config.get("_reviews_base_url")

        def fetch_page(a, cursor):
            return fetch_reviews_page(a, cursor=cursor, num=100, base_url=base_url)

        result = backfill_history(
            store,
            appid,
            fetch_page,
            cfg=(config.get("analytics", {}) or {}).get("backfill"),
        )
        if result.get("status") != "complete":
            print(
                "[警告] appid={} の過去分取り込みは途中までです（{}件、理由: {}）".format(
                    appid, result.get("fetched", 0), result.get("reason", "")
                ),
                file=sys.stderr,
            )
        else:
            print(
                "[過去分取り込み] appid={}: {}件を履歴に取り込みました".format(
                    appid, result.get("fetched", 0)
                )
            )
    except Exception as e:
        print("[警告] appid={} の過去分取り込みに失敗しました: {}".format(appid, e), file=sys.stderr)


def _maybe_histogram(config, store, appid):
    """未実施なら月次集計（ヒストグラム）の取り込みを1回だけ行う。

    バックフィルと同時に初回だけ自動実行する。取得に失敗しても記録だけ残し、
    本体は止めない（手動の import-history --force でやり直せる）。
    """
    if store is None or import_review_histogram is None:
        return
    hist_cfg = (config.get("analytics", {}) or {}).get("histogram", {}) or {}
    if hist_cfg.get("enabled", True) is False:
        return
    try:
        if store.get_meta("histogram:{}".format(appid)) is not None:
            return
        rollups = fetch_review_histogram(
            appid, base_url=config.get("_histogram_base_url")
        )
        result = import_review_histogram(store, appid, rollups)
        if result.get("status") == "imported":
            print(
                "[月次集計取り込み] appid={}: {}か月分を取り込みました".format(
                    appid, result.get("months", 0)
                )
            )
        elif result.get("status") == "failed":
            print(
                "[警告] appid={} の月次集計の取得に失敗しました"
                "（import-history --force で再実行できます）".format(appid),
                file=sys.stderr,
            )
    except Exception as e:
        print("[警告] appid={} の月次集計取り込みに失敗しました: {}".format(appid, e), file=sys.stderr)


def _maybe_snapshots(config, store, appid, base_url=None):
    """チェック時点のスナップショットを取得して保存する。query_summary を返す。

    取るもの：公式の総レビュー数・おすすめ数・低評価数・評価ラベル（query_summary）、
    同時プレイヤー数、価格の割引率。時系列データは後からさかのぼって取れないため、
    集計に使うかどうかに関わらず保存する。割引率の変化はセールイベントとして
    自動登録する。どれも失敗しても本体は止めない。
    """
    if store is None:
        return None
    analytics_cfg = config.get("analytics", {}) or {}
    qs = fetch_query_summary(appid, base_url=base_url)

    players = None
    players_cfg = analytics_cfg.get("players", {}) or {}
    if players_cfg.get("enabled", True):
        players = fetch_current_players(
            appid, base_url=config.get("_players_base_url")
        )
        if players is not None:
            try:
                store.record_player_count(appid, players)
            except Exception as e:
                print(
                    "[警告] appid={} の同時プレイヤー数保存に失敗しました: {}".format(appid, e),
                    file=sys.stderr,
                )

    discount = None
    sale_cfg = analytics_cfg.get("sale_events", {}) or {}
    if sale_cfg.get("enabled", True):
        discount = fetch_discount_percent(
            appid, base_url=config.get("_appdetails_base_url")
        )
        if discount is not None and detect_sale_event is not None:
            try:
                ev = detect_sale_event(store, appid, discount)
                if ev:
                    print(
                        "[イベント] appid={}: 「{}」を登録しました".format(appid, ev["title"])
                    )
            except Exception as e:
                print(
                    "[警告] appid={} のセール検知に失敗しました: {}".format(appid, e),
                    file=sys.stderr,
                )

    if qs is not None or players is not None or discount is not None:
        try:
            store.record_snapshot(
                appid,
                total_reviews=(
                    int(qs.get("total_reviews"))
                    if qs and qs.get("total_reviews") is not None else None
                ),
                total_positive=(
                    int(qs.get("total_positive"))
                    if qs and qs.get("total_positive") is not None else None
                ),
                total_negative=(
                    int(qs.get("total_negative"))
                    if qs and qs.get("total_negative") is not None else None
                ),
                review_score_desc=qs.get("review_score_desc") if qs else None,
                player_count=players,
                discount_percent=discount,
            )
        except Exception as e:
            print(
                "[警告] appid={} のスナップショット保存に失敗しました: {}".format(appid, e),
                file=sys.stderr,
            )
    return qs


METADATA_SYNC_INTERVAL_SEC = 24 * 3600  # アプリ基本情報の確認は1日1回まで


def _maybe_sync_app_metadata(config, store, appid):
    """アプリ基本情報を取得し、変更があったときだけ履歴に残す（1日1回まで確認）。"""
    if store is None:
        return
    meta_cfg = (config.get("analytics", {}) or {}).get("metadata", {}) or {}
    if meta_cfg.get("enabled", True) is False:
        return
    try:
        key = "metadata_sync:{}".format(appid)
        last = store.get_meta(key)
        now = int(time.time())
        if last is not None and now - int(last) < METADATA_SYNC_INTERVAL_SEC:
            return
        info = fetch_app_metadata(
            appid, base_url=config.get("_appdetails_base_url")
        )
        if info is None:
            return
        store.set_meta(key, now)
        if store.record_app_metadata_if_changed(appid, info):
            print("[メタデータ] appid={}: アプリ基本情報の変更を記録しました".format(appid))
    except Exception as e:
        print(
            "[警告] appid={} のアプリ基本情報の取得に失敗しました: {}".format(appid, e),
            file=sys.stderr,
        )


def _maybe_achievements(config, store, appid):
    """実績の全体達成率をスナップショット保存する（既定では週1回まで）。"""
    if store is None:
        return
    ach_cfg = (config.get("analytics", {}) or {}).get("achievements", {}) or {}
    if ach_cfg.get("enabled", True) is False:
        return
    try:
        interval_days = int(ach_cfg.get("interval_days", 7))
    except (TypeError, ValueError):
        interval_days = 7
    try:
        key = "achievements_sync:{}".format(appid)
        last = store.get_meta(key)
        now = int(time.time())
        if last is not None and now - int(last) < interval_days * 86400:
            return
        achievements = fetch_global_achievements(
            appid, base_url=config.get("_achievements_base_url")
        )
        if achievements is None:
            return
        store.record_achievement_snapshot(appid, achievements)
        store.set_meta(key, now)
    except Exception as e:
        print(
            "[警告] appid={} の実績達成率の取得に失敗しました: {}".format(appid, e),
            file=sys.stderr,
        )


EVENTS_SYNC_INTERVAL_SEC = 24 * 3600  # 公式発表の同期は1日1回まで


def _maybe_sync_events(config, store, appid):
    """公式発表からイベント候補を同期する（1日1回まで、失敗しても通知は続ける）。"""
    if store is None or sync_feed_events is None:
        return
    events_cfg = (config.get("analytics", {}) or {}).get("events") or {}
    merged = {**DEFAULT_EVENTS, **events_cfg}
    if not merged.get("enabled", True):
        return
    try:
        key = "events_sync:{}".format(appid)
        last = store.get_meta(key)
        now = int(time.time())
        if last is not None and now - int(last) < EVENTS_SYNC_INTERVAL_SEC:
            return
        items = fetch_announcements(
            appid,
            count=int(merged.get("feed_count", 20)),
            base_url=config.get("_news_base_url"),
        )
        n = sync_feed_events(store, appid, items, cfg=events_cfg)
        store.set_meta(key, now)
        if n:
            print("[イベント] appid={}: 公式発表からイベント候補 {} 件を登録しました".format(appid, n))
    except Exception as e:
        print("[警告] appid={} のイベント同期に失敗しました: {}".format(appid, e), file=sys.stderr)


def load_state(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("状態ファイルの形式が想定と違います（オブジェクトではありません）")
        return data
    except Exception as e:
        # 黙って初期化すると原因に気づけないため、壊れたファイルを退避して明示的に警告する。
        # 初回扱い（ベースライン記録のみ・通知なし）でやり直すので、通知洪水にはならない。
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        broken = path.with_name("{}.broken-{}".format(path.name, stamp))
        try:
            os.replace(path, broken)
            kept = "壊れたファイルは {} に退避しました".format(broken.name)
        except OSError:
            kept = "壊れたファイルの退避にも失敗しました"
        print(
            "[警告] 状態ファイル {} を読み込めませんでした（{}）。{}。"
            "初回としてベースラインからやり直します（過去分は通知されません）。".format(path, e, kept),
            file=sys.stderr,
        )
        return {}


def save_state(path, state):
    """一時ファイルに書いてから os.replace で差し替える（書き込み中の事故で壊さない）。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class CheckResult(int):
    """通知件数（int互換）に、取得失敗したタイトル数を付けたチェック結果。"""

    def __new__(cls, notified=0, failed_apps=0):
        obj = super().__new__(cls, notified)
        obj.notified = int(notified)
        obj.failed_apps = int(failed_apps)
        return obj


def _retry_pending(state, notify):
    """前回までに送信失敗したレビューを、失敗した送信先へだけ再送する。"""
    pending = state.get(PENDING_KEY)
    if not pending:
        return
    for appid in list(pending.keys()):
        entries = pending[appid]
        for rid in list(entries.keys()):
            entry = entries[rid]
            remaining = list(entry.get("remaining") or [])
            results = deliver(notify, entry.get("message", ""), only=remaining)
            # 送信先が設定から消えていたら、その分は完了扱いにして残さない
            still_failed = [
                name for name in remaining
                if name in results and not results[name]
            ]
            if still_failed:
                entry["remaining"] = still_failed
                print(
                    "[警告] appid={} レビュー {} の再送に失敗しました（残り: {}）".format(
                        appid, rid, ", ".join(still_failed)
                    ),
                    file=sys.stderr,
                )
            else:
                del entries[rid]
                seen_list = state.setdefault(appid, [])
                if rid not in seen_list:
                    seen_list.append(rid)
                print("[再送完了] appid={} レビュー {} を届けました".format(appid, rid))
        if not entries:
            del pending[appid]
    if not pending:
        state.pop(PENDING_KEY, None)


def check_once(config, state_path, notify, store=None):
    """1回分のチェック。CheckResult（通知件数＋取得失敗タイトル数）を返す。"""
    state = load_state(state_path)
    total_notified = 0
    failed_apps = 0
    base_url = config.get("_reviews_base_url")  # テスト用に差し替え可能（通常は未設定）

    # まず前回の送信失敗分を再送する（取得が失敗しても再送は試みる）
    _retry_pending(state, notify)
    pending = state.setdefault(PENDING_KEY, {})

    for appid in config.get("app_ids", []):
        appid = str(appid)
        is_baseline = appid not in state
        known_ids = None if is_baseline else set(state.get(appid, []))
        try:
            reviews, caught_up = fetch_reviews_until(
                appid,
                known_ids=known_ids,
                page_size=int(config.get("fetch_count", 20)),
                base_url=base_url,
            )
        except Exception as e:
            # 取得失敗時はベースラインも既読も更新しない（次回に正しくやり直す）
            failed_apps += 1
            print("[警告] appid={} のレビュー取得に失敗しました: {}".format(appid, e), file=sys.stderr)
            continue
        if not caught_up:
            print(
                "[警告] appid={}: 前回のチェック位置までたどり着けませんでした。"
                "一部のレビューを取りこぼした可能性があります".format(appid),
                file=sys.stderr,
            )
        query_summary = None
        if store is not None:
            # 履歴の蓄積（分析層）。名前が未登録なら1回だけ取得して保存する
            try:
                if not store.get_app_name(appid):
                    store.set_app_name(
                        appid,
                        fetch_app_name(appid, base_url=config.get("_appdetails_base_url")),
                    )
                store.upsert_reviews(appid, reviews)
            except Exception as e:
                print("[警告] appid={} の履歴保存に失敗しました: {}".format(appid, e), file=sys.stderr)
            _maybe_backfill(config, store, appid)
            _maybe_histogram(config, store, appid)
            _maybe_sync_events(config, store, appid)
            _maybe_sync_app_metadata(config, store, appid)
            _maybe_achievements(config, store, appid)
            query_summary = _maybe_snapshots(config, store, appid, base_url=base_url)
        # 評価ラベルの陥落アラート（目安）。スナップショットで取れた query_summary を使い回す
        try:
            _maybe_label_alert(
                config, state, appid, None, notify,
                base_url=base_url, query_summary=query_summary,
            )
        except Exception as e:
            print("[警告] appid={} のラベルアラート判定に失敗しました: {}".format(appid, e), file=sys.stderr)
        current_ids = [r.get("recommendationid") for r in reviews]
        if is_baseline:
            # 初回: 既存レビューは通知せず、既読として記録するだけ
            state[appid] = current_ids
            print("[初期化] appid={}: 既存レビュー {} 件を既読として記録しました".format(appid, len(current_ids)))
            _maybe_surge_alert(config, store, appid, None, notify)
            continue
        seen = set(state.get(appid, []))
        pending_rids = set((pending.get(appid) or {}).keys())
        app_name = None
        for review in reversed(reviews):  # 古い順に通知する
            rid = review.get("recommendationid")
            if not rid or rid in seen or rid in pending_rids:
                continue
            if app_name is None:
                app_name = fetch_app_name(appid, base_url=config.get("_appdetails_base_url"))
            msg = format_review_message(
                app_name, appid, review,
                translate_link=bool(config.get("translate_link", True)),
            )
            results = deliver(notify, msg)
            failed_dests = [name for name, ok in results.items() if not ok]
            if any(results.values()):
                total_notified += 1
            # 致命語を含むレビューは、通常の通知とは別に目立つ形で知らせる
            fatal_cfg = _fatal_alerts_cfg(config)
            if fatal_cfg is not None and match_fatal_keyword is not None:
                keyword = match_fatal_keyword(
                    review.get("review") or "", fatal_cfg, appid
                )
                if keyword:
                    alert_msg = format_fatal_message(app_name, appid, review, keyword)
                    alert_results = deliver(notify, alert_msg)
                    alert_failed = [
                        name for name, ok in alert_results.items() if not ok
                    ]
                    if alert_failed:
                        pending.setdefault(appid, {})[rid + "#fatal"] = {
                            "message": alert_msg,
                            "remaining": alert_failed,
                        }
            if failed_dests:
                # 失敗した送信先へは次回再送する。既読にはしない
                pending.setdefault(appid, {})[rid] = {
                    "message": msg,
                    "remaining": failed_dests,
                }
                print(
                    "[警告] appid={} レビュー {} は {} への送信に失敗しました（次回再送します）".format(
                        appid, rid, ", ".join(failed_dests)
                    ),
                    file=sys.stderr,
                )
            else:
                seen.add(rid)
        # 再送待ち（pending）のレビューは既読リストに入れない
        still_pending = set((pending.get(appid) or {}).keys())
        merged = [r for r in current_ids if r and r not in still_pending] + [
            s for s in state[appid] if s not in current_ids and s not in still_pending
        ]
        state[appid] = merged[:200]
        _maybe_surge_alert(config, store, appid, app_name, notify)
    if not pending:
        state.pop(PENDING_KEY, None)
    save_state(state_path, state)
    return CheckResult(total_notified, failed_apps=failed_apps)


def validate_config(config):
    """設定を検証し、正規化した設定を返す。誤りは ValueError（設定名入りの文）で知らせる。"""
    if not isinstance(config, dict):
        raise ValueError("設定ファイルの形式が想定と違います（JSONオブジェクトではありません）")
    errors = []

    app_ids = config.get("app_ids")
    if not isinstance(app_ids, list) or not app_ids:
        errors.append("app_ids: 監視するApp IDの配列が必要です（空は不可）")
    else:
        normalized = []
        for v in app_ids:
            if isinstance(v, bool):
                errors.append("app_ids: {!r} は数値ではありません".format(v))
            elif isinstance(v, int):
                normalized.append(v)
            elif isinstance(v, str) and v.strip().isdigit():
                normalized.append(int(v.strip()))
            else:
                errors.append("app_ids: {!r} は数値ではありません".format(v))
        if len(normalized) == len(app_ids):
            config["app_ids"] = normalized

    any_webhook = False
    for key in ("discord_webhook_url", "slack_webhook_url", "webhook_url"):
        v = config.get(key, "")
        if v is None:
            v = ""
        if not isinstance(v, str):
            errors.append("{}: 文字列で指定してください".format(key))
            continue
        v = v.strip()
        config[key] = v
        if v:
            any_webhook = True
            parsed = urllib.parse.urlparse(v)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                errors.append("{}: URLの形式が正しくありません（{}）".format(key, v))
    if not any_webhook:
        errors.append(
            "通知先が未設定です: discord_webhook_url / slack_webhook_url / webhook_url のいずれかを設定してください"
        )

    interval = config.get("interval_minutes", 30)
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not (1 <= interval <= 1440):
        errors.append("interval_minutes: 1〜1440 の数値で指定してください（現在値: {!r}）".format(interval))

    fetch_count = config.get("fetch_count", 20)
    if isinstance(fetch_count, bool) or not isinstance(fetch_count, int) or not (1 <= fetch_count <= 100):
        errors.append("fetch_count: 1〜100 の整数で指定してください（現在値: {!r}）".format(fetch_count))

    state_file = config.get("state_file", "state.json")
    if not isinstance(state_file, str) or not state_file:
        errors.append("state_file: ファイル名を文字列で指定してください")

    label_alert = config.get("label_alert", {})
    if label_alert is not None:
        if not isinstance(label_alert, dict):
            errors.append("label_alert: オブジェクトで指定してください")
        else:
            warn_within = label_alert.get("warn_within", 5)
            if (isinstance(warn_within, bool) or not isinstance(warn_within, int)
                    or not (1 <= warn_within <= 50)):
                errors.append(
                    "label_alert.warn_within: 1〜50 の整数で指定してください（現在値: {!r}）".format(warn_within)
                )

    if errors:
        raise ValueError("\n".join(errors))
    return config


def acquire_lock(lock_path):
    """簡易ロック。取得できたら True。古い残骸は片付けて取り直す。"""
    for _ in range(2):
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                continue
            if age > LOCK_STALE_SEC:
                print(
                    "[警告] 古いロックファイル {} を削除して続行します".format(lock_path),
                    file=sys.stderr,
                )
                try:
                    lock_path.unlink()
                except OSError:
                    return False
                continue
            return False
    return False


def main():
    parser = argparse.ArgumentParser(description="こえびん — Steam新着レビュー通知ツール")
    parser.add_argument("--config", default="config.json", help="設定ファイルのパス")
    parser.add_argument("--loop", action="store_true", help="設定した間隔で繰り返し実行する")
    parser.add_argument("--test", action="store_true", help="設定した送信先にテスト通知を1通送って終了する")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print(
            "設定ファイルが見つかりません: {}\nconfig.example.json をコピーして作成してください。".format(config_path),
            file=sys.stderr,
        )
        return 1
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as e:
        print("設定ファイルを読み込めませんでした: {}（{}）".format(config_path, e), file=sys.stderr)
        return 1
    try:
        config = validate_config(config)
    except ValueError as e:
        for line in str(e).splitlines():
            print("[設定エラー] {}".format(line), file=sys.stderr)
        return 1

    notify = build_notifier(config)
    if notify is None:
        print("通知先が未設定です。config.json を確認してください。", file=sys.stderr)
        return 1

    if args.test:
        message = (
            "【テスト通知】こえびん\n"
            "このメッセージが届いていれば、通知設定は正常です。"
        )
        results = notify.send(message)
        for name, ok in results.items():
            print("{}: {}".format(name, "送信成功" if ok else "送信失敗"))
        return 0 if results and all(results.values()) else 1

    state_path = config_path.parent / config.get("state_file", "state.json")
    lock_path = state_path.with_name(state_path.name + ".lock")
    if not acquire_lock(lock_path):
        print(
            "[エラー] 既に実行中です（ロックファイル: {}）。多重起動を避けるため終了します。".format(lock_path),
            file=sys.stderr,
        )
        return 1
    try:
        store = None
        analytics_cfg = config.get("analytics", {}) or {}
        if analytics_cfg.get("enabled", True) and open_store is not None:
            try:
                store = open_store(config, config_path.parent)
            except Exception as e:
                print("[警告] 分析層のDBを開けませんでした（通知のみ継続します）: {}".format(e), file=sys.stderr)
                store = None
        interval = int(config.get("interval_minutes", 30))

        exit_code = 0
        while True:
            try:
                result = check_once(config, state_path, notify, store=store)
                print(
                    "{} チェック完了（新規通知 {} 件）".format(
                        datetime.now().strftime("%H:%M:%S"), result.notified
                    )
                )
                if result.failed_apps:
                    print(
                        "[警告] {} 件のタイトルでレビュー取得に失敗しました".format(result.failed_apps),
                        file=sys.stderr,
                    )
                    if not args.loop:
                        exit_code = 2
            except Exception as e:
                # --loop では1回の失敗で全体を止めない。単発実行では異常終了として返す
                print("[エラー] チェック中に例外が発生しました: {}".format(e), file=sys.stderr)
                if not args.loop:
                    return 1
            if not args.loop:
                break
            time.sleep(max(interval, 5) * 60)
        return exit_code
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
