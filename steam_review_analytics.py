#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
こえびん 分析層（最小版） by Axioknot

通知ツール（steam_review_notifier.py）が取得したレビューを SQLite に蓄積し、
蓄積した履歴から次の2つを生成する。

- 週次ダイジェスト：直近7日の件数・おすすめ比率・前週比、言語の内訳、
  参考になった数上位のレビュー抜粋。集計と整形は通常のコードのみ（AI不使用）
- 低評価の急増検知：直近24時間の低評価の割合・件数が、過去28日の
  ベースラインをしきい値以上に上回ったらアラートを送る

- 依存ライブラリなし（Python 3.9+ 標準ライブラリのみ、sqlite3 を含む）
- 通知ツールから import して使うほか、単体でも実行できる：
    python steam_review_analytics.py --config config.json digest        （生成して送信）
    python steam_review_analytics.py --config config.json digest --dry-run（表示のみ）
    python steam_review_analytics.py --config config.json check-surge   （急増検知のみ）
    python steam_review_analytics.py --config config.json backfill      （過去分をさかのぼって取り込む）
"""

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

DAY = 86400

# Steamの言語コード → 表示名（ダイジェスト用。未知のコードはそのまま表示する）
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

DEFAULT_SURGE = {
    "recent_hours": 24,         # 急増を見る直近の窓
    "baseline_days": 28,        # ベースラインの期間（直近の窓は含めない）
    "min_recent_reviews": 5,    # 直近の窓の総レビュー数の下限（少なすぎると判定しない）
    "min_recent_negatives": 3,  # 直近の窓の低評価数の下限
    "ratio_delta": 0.20,        # ベースラインの低評価率をどれだけ上回ったら急増とするか
    "ratio_floor": 0.30,        # 直近の低評価率の絶対的な下限（これ未満では鳴らさない）
    "cooldown_hours": 24,       # 同じタイトルへの再アラート抑制時間
}


def language_name(code):
    return LANGUAGE_NAMES.get(code or "", code or "不明")


def review_permalink(review):
    steamid = review.get("author_steamid") or ""
    appid = review.get("appid") or ""
    if steamid and appid:
        return "https://steamcommunity.com/profiles/{}/recommended/{}/".format(steamid, appid)
    return None


class ReviewStore:
    """レビュー履歴の SQLite ストア。"""

    def __init__(self, db_path):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reviews (
                appid TEXT NOT NULL,
                recommendationid TEXT NOT NULL,
                author_steamid TEXT,
                voted_up INTEGER NOT NULL,
                language TEXT,
                review_text TEXT,
                votes_up INTEGER NOT NULL DEFAULT 0,
                votes_funny INTEGER NOT NULL DEFAULT 0,
                playtime_forever INTEGER NOT NULL DEFAULT 0,
                timestamp_created INTEGER NOT NULL,
                first_seen_at INTEGER NOT NULL,
                PRIMARY KEY (appid, recommendationid)
            );
            CREATE INDEX IF NOT EXISTS idx_reviews_app_ts
                ON reviews (appid, timestamp_created);
            CREATE TABLE IF NOT EXISTS apps (
                appid TEXT PRIMARY KEY,
                name TEXT,
                updated_at INTEGER
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            """
        )
        self.conn.commit()

    def close(self):
        self.conn.close()

    def upsert_reviews(self, appid, reviews, now=None):
        """レビューを保存する。新規に保存できた件数を返す（重複は内容の更新のみ）。"""
        now = int(now if now is not None else time.time())
        inserted = 0
        for r in reviews:
            rid = r.get("recommendationid")
            if not rid:
                continue
            exists = self.conn.execute(
                "SELECT 1 FROM reviews WHERE appid = ? AND recommendationid = ?",
                (str(appid), str(rid)),
            ).fetchone()
            if not exists:
                inserted += 1
            author = r.get("author", {}) or {}
            self.conn.execute(
                """
                INSERT INTO reviews (
                    appid, recommendationid, author_steamid, voted_up, language,
                    review_text, votes_up, votes_funny, playtime_forever,
                    timestamp_created, first_seen_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(appid, recommendationid) DO UPDATE SET
                    voted_up = excluded.voted_up,
                    votes_up = excluded.votes_up,
                    votes_funny = excluded.votes_funny,
                    review_text = excluded.review_text
                """,
                (
                    str(appid),
                    str(rid),
                    str(author.get("steamid", "") or ""),
                    1 if r.get("voted_up") else 0,
                    r.get("language", "") or "",
                    (r.get("review") or ""),
                    int(r.get("votes_up", 0) or 0),
                    int(r.get("votes_funny", 0) or 0),
                    int(author.get("playtime_forever", 0) or 0),
                    int(r.get("timestamp_created", 0) or 0),
                    now,
                ),
            )
        self.conn.commit()
        return inserted

    def count_reviews(self, appid):
        row = self.conn.execute(
            "SELECT COUNT(*) AS c FROM reviews WHERE appid = ?", (str(appid),)
        ).fetchone()
        return row["c"]

    def set_app_name(self, appid, name):
        if not name:
            return
        self.conn.execute(
            """
            INSERT INTO apps (appid, name, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(appid) DO UPDATE SET name = excluded.name, updated_at = excluded.updated_at
            """,
            (str(appid), name, int(time.time())),
        )
        self.conn.commit()

    def get_app_name(self, appid):
        row = self.conn.execute(
            "SELECT name FROM apps WHERE appid = ?", (str(appid),)
        ).fetchone()
        return row["name"] if row else None

    def reviews_between(self, appid, start_ts, end_ts):
        """[start_ts, end_ts) のレビューを新しい順に返す。"""
        rows = self.conn.execute(
            """
            SELECT * FROM reviews
            WHERE appid = ? AND timestamp_created >= ? AND timestamp_created < ?
            ORDER BY timestamp_created DESC
            """,
            (str(appid), int(start_ts), int(end_ts)),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_meta(self, key):
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key, value):
        self.conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )
        self.conn.commit()


def window_stats(reviews):
    """レビューのリストから集計値を返す。"""
    total = len(reviews)
    up = sum(1 for r in reviews if r["voted_up"])
    down = total - up
    languages = Counter(r["language"] or "" for r in reviews)
    top = sorted(
        reviews,
        key=lambda r: (int(r.get("votes_up", 0) or 0), int(r.get("timestamp_created", 0) or 0)),
        reverse=True,
    )
    return {
        "total": total,
        "up": up,
        "down": down,
        "up_ratio": (up / total) if total else None,
        "languages": languages,
        "top_reviews": top,
    }


def _excerpt(text, limit=120):
    t = (text or "").strip().replace("\n", " ")
    if len(t) > limit:
        t = t[:limit] + "…"
    return t


def _fmt_date(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime("%m/%d")


def _digest_conclusion(cur, prev):
    """集計から結論の1文を作る（ルールベース。AI不使用）。"""
    if cur["total"] == 0:
        return "今週は新しいレビューがありませんでした"
    ratio = cur["up_ratio"]
    prev_ratio = prev["up_ratio"]
    if ratio < 0.5:
        return "低評価が目立っています（おすすめ率 {:.0f}%）".format(ratio * 100)
    if prev_ratio is not None and ratio <= prev_ratio - 0.15:
        return "おすすめ率が前週より下がっています（{:.0f}% → {:.0f}%）".format(
            prev_ratio * 100, ratio * 100
        )
    if ratio >= 0.8:
        return "好調です（おすすめ率 {:.0f}%）".format(ratio * 100)
    return "概ね安定しています（おすすめ率 {:.0f}%）".format(ratio * 100)


def build_digest_text(store, appid, app_name, now=None, days=7):
    """週次ダイジェストのテキストを生成する。"""
    now = int(now if now is not None else time.time())
    cur_start = now - days * DAY
    prev_start = now - 2 * days * DAY
    cur = window_stats(store.reviews_between(appid, cur_start, now))
    prev = window_stats(store.reviews_between(appid, prev_start, cur_start))
    name = app_name or store.get_app_name(appid) or "App {}".format(appid)

    lines = []
    lines.append(
        "📊【週次ダイジェスト】{}（{}〜{}）".format(name, _fmt_date(cur_start), _fmt_date(now - 1))
    )
    lines.append("結論：{}".format(_digest_conclusion(cur, prev)))
    if cur["total"] == 0:
        lines.append("https://store.steampowered.com/app/{}/".format(appid))
        return "\n".join(lines)

    prev_ratio_txt = (
        "{:.0f}%".format(prev["up_ratio"] * 100) if prev["up_ratio"] is not None else "—"
    )
    lines.append(
        "今週：{}件（前週 {}件）｜👍 {} / 👎 {}｜おすすめ率 {:.0f}%（前週 {}）".format(
            cur["total"], prev["total"], cur["up"], cur["down"], cur["up_ratio"] * 100, prev_ratio_txt
        )
    )
    lang_parts = [
        "{} {}件".format(language_name(code), n)
        for code, n in cur["languages"].most_common(3)
    ]
    rest = sum(n for _, n in cur["languages"].most_common()[3:])
    if rest:
        lang_parts.append("その他 {}件".format(rest))
    lines.append("言語：{}".format("、".join(lang_parts)))

    top = [r for r in cur["top_reviews"] if int(r.get("votes_up", 0) or 0) > 0][:3]
    if top:
        lines.append("▼ 参考になったレビュー")
        for i, r in enumerate(top, 1):
            verdict = "👍" if r["voted_up"] else "👎"
            playtime_h = int(r.get("playtime_forever", 0) or 0) / 60.0
            lines.append(
                "{}. {}｜{:.0f}時間｜参考になった {}件".format(
                    i, verdict, playtime_h, int(r.get("votes_up", 0) or 0)
                )
            )
            lines.append("「{}」".format(_excerpt(r["review_text"])))
            link = review_permalink(r)
            if link:
                lines.append(link)
    lines.append("https://store.steampowered.com/app/{}/".format(appid))
    return "\n".join(lines)


def evaluate_surge(store, appid, cfg=None, now=None):
    """急増判定。アラートすべきなら根拠の辞書を、そうでなければ None を返す。

    判定式（すべて満たしたときだけアラート）:
      recent_total >= min_recent_reviews
      recent_neg   >= min_recent_negatives
      recent_neg_ratio >= baseline_neg_ratio + ratio_delta
      recent_neg_ratio >= ratio_floor
    ベースラインは直近の窓を含まない過去 baseline_days 日。
    ベースラインのレビューが0件なら「データ不足」として判定しない。
    """
    cfg = {**DEFAULT_SURGE, **(cfg or {})}
    now = int(now if now is not None else time.time())
    recent_start = now - int(cfg["recent_hours"]) * 3600
    baseline_start = recent_start - int(cfg["baseline_days"]) * DAY

    recent = store.reviews_between(appid, recent_start, now)
    baseline = store.reviews_between(appid, baseline_start, recent_start)
    if not baseline:
        return None
    r = window_stats(recent)
    b = window_stats(baseline)
    if r["total"] == 0:
        return None
    recent_neg_ratio = r["down"] / r["total"]
    baseline_neg_ratio = b["down"] / b["total"]
    if (
        r["total"] >= int(cfg["min_recent_reviews"])
        and r["down"] >= int(cfg["min_recent_negatives"])
        and recent_neg_ratio >= baseline_neg_ratio + float(cfg["ratio_delta"])
        and recent_neg_ratio >= float(cfg["ratio_floor"])
    ):
        return {
            "recent": r,
            "baseline": b,
            "recent_neg_ratio": recent_neg_ratio,
            "baseline_neg_ratio": baseline_neg_ratio,
            "recent_start": recent_start,
            "cfg": cfg,
        }
    return None


def format_surge_text(appid, app_name, result):
    r = result["recent"]
    cfg = result["cfg"]
    name = app_name or "App {}".format(appid)
    lines = []
    lines.append("🚨【低評価の急増】{}".format(name))
    lines.append(
        "結論：直近{}時間で低評価が増えています（👎 {}件／全{}件 = {:.0f}%）".format(
            cfg["recent_hours"], r["down"], r["total"], result["recent_neg_ratio"] * 100
        )
    )
    lines.append(
        "平常時：過去{}日の低評価率は {:.0f}%（{}件中 {}件）".format(
            cfg["baseline_days"],
            result["baseline_neg_ratio"] * 100,
            result["baseline"]["total"],
            result["baseline"]["down"],
        )
    )
    negatives = [x for x in r["top_reviews"] if not x["voted_up"]]
    negatives.sort(key=lambda x: int(x.get("timestamp_created", 0) or 0), reverse=True)
    if negatives:
        lines.append("▼ 直近の低評価レビュー")
        for x in negatives[:3]:
            playtime_h = int(x.get("playtime_forever", 0) or 0) / 60.0
            lines.append(
                "・{:.0f}時間｜「{}」".format(playtime_h, _excerpt(x["review_text"], 100))
            )
            link = review_permalink(x)
            if link:
                lines.append("  {}".format(link))
    lines.append("https://store.steampowered.com/app/{}/".format(appid))
    return "\n".join(lines)


def evaluate_and_alert(store, config, appid, app_name, notify, now=None):
    """急増を判定し、アラートすべきなら notify する。送ったら True。

    クールダウン中は送らない（最終アラート時刻を meta に保存する）。
    通知ツール（steam_review_notifier.check_once）から呼ばれる。
    """
    now = int(now if now is not None else time.time())
    analytics_cfg = config.get("analytics", {}) or {}
    cfg = {**DEFAULT_SURGE, **(analytics_cfg.get("surge", {}) or {})}
    key = "surge_last_alert:{}".format(appid)
    last = store.get_meta(key)
    if last is not None:
        try:
            if now - int(last) < int(cfg["cooldown_hours"]) * 3600:
                return False
        except ValueError:
            pass
    result = evaluate_surge(store, appid, cfg, now=now)
    if result is None:
        return False
    name = app_name or store.get_app_name(appid) or "App {}".format(appid)
    notify(format_surge_text(appid, name, result))
    store.set_meta(key, now)
    return True


def open_store(config, config_dir):
    analytics_cfg = config.get("analytics", {}) or {}
    db_file = analytics_cfg.get("db_file", "reviews.db")
    return ReviewStore(Path(config_dir) / db_file)


DEFAULT_BACKFILL = {
    "days": 90,            # さかのぼる日数
    "max_reviews": 2000,   # 取り込む件数の上限（タイトルごと）
    "page_sleep_sec": 1.0, # ページ取得の間隔（レート制限への配慮）
}


def backfill_history(store, appid, fetch_page, cfg=None, now=None, sleep=None):
    """初回に過去分をさかのぼって履歴へ取り込む。

    fetch_page(appid, cursor) -> (reviews, next_cursor) をカーソルでたどり、
    直近 days 日分を最大 max_reviews 件まで保存する。
    途中で取得に失敗したら、そこまでの分を保存して status="partial" で戻る
    （例外は投げない）。実施結果は meta の `backfill:<appid>` に JSON で記録し、
    通知ツール側はこの記録がある限り自動では再実行しない。
    戻り値: {"status": "complete"|"capped"|"partial", "fetched": int, "reason": str}
    """
    cfg = {**DEFAULT_BACKFILL, **(cfg or {})}
    sleep = sleep if sleep is not None else time.sleep
    now = int(now if now is not None else time.time())
    cutoff = now - int(cfg["days"]) * DAY
    max_reviews = int(cfg["max_reviews"])

    fetched = 0
    status = "complete"
    reason = ""
    cursor = "*"
    pages = 0
    while True:
        try:
            page_reviews, next_cursor = fetch_page(appid, cursor)
        except Exception as e:
            status = "partial"
            reason = "取得に失敗しました: {}".format(e)
            break
        pages += 1
        # 期間内の分だけ保存する（ページ末尾には期間外が混ざることがある）
        in_range = [
            r for r in page_reviews
            if int(r.get("timestamp_created", 0) or 0) >= cutoff
        ]
        if in_range:
            store.upsert_reviews(appid, in_range, now=now)
            fetched += len(in_range)
        oldest_ts = min(
            (int(r.get("timestamp_created", 0) or 0) for r in page_reviews),
            default=0,
        )
        if fetched >= max_reviews:
            status = "capped"
            reason = "上限 {} 件に達しました".format(max_reviews)
            break
        if not page_reviews or oldest_ts < cutoff:
            break  # 期間の末尾まで到達した
        if not next_cursor or next_cursor == cursor:
            break  # これ以上古いページが無い
        cursor = next_cursor
        if float(cfg["page_sleep_sec"]) > 0:
            sleep(float(cfg["page_sleep_sec"]))

    store.set_meta(
        "backfill:{}".format(appid),
        json.dumps(
            {"status": status, "fetched": fetched, "at": now, "reason": reason},
            ensure_ascii=False,
        ),
    )
    return {"status": status, "fetched": fetched, "reason": reason, "pages": pages}


def main():
    parser = argparse.ArgumentParser(description="こえびん 分析層（ダイジェスト／急増検知）")
    parser.add_argument("--config", default="config.json", help="設定ファイルのパス")
    parser.add_argument("command", choices=["digest", "check-surge", "backfill"], help="実行する処理")
    parser.add_argument("--dry-run", action="store_true", help="送信せず標準出力に表示する")
    parser.add_argument("--force", action="store_true", help="backfill を実施済みでも再実行する")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print("設定ファイルが見つかりません: {}".format(config_path), file=sys.stderr)
        return 1
    config = json.loads(config_path.read_text(encoding="utf-8"))

    # 通知の組み立ては通知ツール側の実装を再利用する
    from steam_review_notifier import build_notifier, fetch_app_name, fetch_reviews_page

    if args.command == "backfill":
        store = open_store(config, config_path.parent)
        try:
            bf_cfg = (config.get("analytics", {}) or {}).get("backfill")

            def fetch_page(a, cursor):
                return fetch_reviews_page(
                    a, cursor=cursor, num=100,
                    base_url=config.get("_reviews_base_url"),
                )

            for appid in config.get("app_ids", []):
                appid = str(appid)
                key = "backfill:{}".format(appid)
                if not args.force and store.get_meta(key) is not None:
                    print("appid={}: 実施済みのためスキップしました（--force で再実行）".format(appid))
                    continue
                if args.force:
                    # meta を消してから取り込む（backfill_history が結果を記録し直す）
                    store.conn.execute("DELETE FROM meta WHERE key = ?", (key,))
                    store.conn.commit()
                result = backfill_history(store, appid, fetch_page, cfg=bf_cfg)
                print(
                    "appid={}: {}（{}件{}）".format(
                        appid, result["status"], result["fetched"],
                        "、理由: " + result["reason"] if result["reason"] else "",
                    )
                )
        finally:
            store.close()
        return 0

    if args.dry_run:
        def notify(msg):
            print(msg)
            print("---")
    else:
        notify = build_notifier(config)
        if notify is None:
            print("通知先が未設定です。config.json を確認してください。", file=sys.stderr)
            return 1

    store = open_store(config, config_path.parent)
    try:
        for appid in config.get("app_ids", []):
            appid = str(appid)
            name = store.get_app_name(appid)
            if not name:
                name = fetch_app_name(appid)
                store.set_app_name(appid, name)
            if args.command == "digest":
                notify(build_digest_text(store, appid, name))
            else:
                sent = evaluate_and_alert(store, config, appid, name, notify)
                print("appid={}: {}".format(appid, "アラート送信" if sent else "急増なし"))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
