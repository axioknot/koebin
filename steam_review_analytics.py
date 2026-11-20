#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
こえびん 分析層（最小版） by Axioknot

通知ツール（steam_review_notifier.py）が取得したレビューを SQLite に蓄積し、
蓄積した履歴から次の2つを生成する。

- 週次ダイジェスト：直近7日の件数・おすすめ比率・前週比、言語の内訳、
  論点キーワード集計、同時プレイヤー数の平均・最大、
  参考になった数上位のレビュー抜粋。集計と整形は通常のコードのみ（AI不使用）
- 低評価の急増検知：直近24時間の低評価の割合・件数が、過去28日の
  ベースラインをしきい値以上に上回ったらアラートを送る
- そのほか、公開APIで取れる情報は保存を優先する（時系列は後から取れないため）：
  レビューは全フィールド＋生JSON、チェック時点のスナップショット
  （公式の総数・評価ラベル・同時プレイヤー数・割引率）、割引の開始/終了の
  イベント自動登録、アプリ基本情報（変更時のみ）、実績の全体達成率（週1回）

- 依存ライブラリなし（Python 3.9+ 標準ライブラリのみ、sqlite3 を含む）
- 通知ツールから import して使うほか、単体でも実行できる：
    python steam_review_analytics.py --config config.json digest        （生成して送信）
    python steam_review_analytics.py --config config.json digest --dry-run（表示のみ）
    python steam_review_analytics.py --config config.json check-surge   （急増検知のみ）
    python steam_review_analytics.py --config config.json backfill      （過去分をさかのぼって取り込む）
    python steam_review_analytics.py --config config.json sync-events   （公式発表からイベント候補を登録）
    python steam_review_analytics.py --config config.json events        （登録済みイベントの一覧）
    python steam_review_analytics.py --config config.json add-event --appid 123 --date 2026-11-15 --title "大型アップデート"
    python steam_review_analytics.py --config config.json remove-event --appid 123 --event-id feed-456
"""

import argparse
import hashlib
import json
import re
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


# ===== キーワード辞書（致命語の即時通知・論点集計） =====
# どちらも「キーワード一致」ベースの単純な仕組みで、意味の分類ではない。
# 誤検知・見逃しがあり得ることを前提に、辞書は config で追加できるようにしている。

DEFAULT_FATAL = {
    "enabled": True,
    # 致命語の初期辞書（日英中）。config の analytics.fatal_alerts.extra_keywords と
    # per_app_keywords（appid ごとの追加）で足せる
    "keywords": [
        # 日本語（部分一致で探す）
        "クラッシュ", "起動しない", "起動できない", "強制終了",
        "セーブデータが消", "セーブが消", "セーブデータ消失", "セーブ消失",
        "セーブデータ破損", "セーブデータが破損", "データが消えた",
        "画面が真っ黒", "ブラックスクリーン",
        # 英語（単語境界つき。単一語は語尾変化も拾う）
        "crash", "won't start", "wont start", "won't launch",
        "does not start", "doesn't start", "game won't run", "black screen",
        "save lost", "lost my save", "save deleted", "save corrupted",
        "corrupted save", "save file corrupted", "save data lost",
        # 中国語（簡体字）（部分一致で探す）
        "闪退", "打不开", "无法启动", "启动不了", "存档丢失", "存档损坏",
        "存档没了", "丢档", "黑屏", "崩溃", "无法进入游戏",
    ],
}

DEFAULT_TOPICS = {
    "enabled": True,
    # 論点の初期辞書（日英中）。config の analytics.topics.extra_keywords と
    # per_app_keywords（appid ごと）で、論点への語の追加や新しい論点の追加ができる
    "topics": {
        "バグ・不具合": [
            "バグ", "不具合", "クラッシュ", "フリーズ", "強制終了",
            "bug", "crash", "glitch", "buggy", "闪退", "卡死", "漏洞",
        ],
        "操作性": [
            "操作性", "操作が", "キー配置", "コントローラー",
            "controls", "controller", "clunky", "手感", "操作手感",
        ],
        "難易度": [
            "難易度", "難しすぎ", "簡単すぎ", "バランス調整",
            "difficulty", "too hard", "too easy", "unbalanced",
            "难度", "太难", "太简单",
        ],
        "翻訳・日本語": [
            "翻訳", "誤訳", "日本語訳", "機械翻訳",
            "translation", "localization", "machine translation",
            "翻译", "机翻", "本地化",
        ],
        "価格・ボリューム": [
            "価格", "高すぎ", "ボリューム", "短すぎ", "コスパ",
            "price", "overpriced", "too short",
            "价格", "太贵", "性价比", "内容量",
        ],
        "パフォーマンス": [
            "動作が重い", "重い", "カクカク", "フレームレート", "fps", "最適化",
            "performance", "laggy", "stutter", "optimization",
            "卡顿", "掉帧", "优化",
        ],
    },
}


def _keyword_negated(text, start, end, keyword):
    """一致箇所が否定文脈（「クラッシュしない」「no crashes」等）なら True。

    誤検知を減らすための簡易ガードで、完全ではない。
    """
    before = text[max(0, start - 24):start]
    if re.search(r"(?:\bno|\bnot|\bnever|\bwithout|n't)\s+(?:\S+\s+){0,2}$", before):
        return True
    if keyword in ("クラッシュ", "crash"):
        after = text[end:end + 6]
        if after.startswith(("しない", "しません", "せず", "しなかった")):
            return True
    return False


def find_keyword_hit(text, keywords):
    """テキストに最初に一致したキーワードを返す（無ければ None）。

    英語（ASCII）の語は単語境界で探し、単一語は語尾変化（crashes 等）も拾う。
    日本語・中国語は部分一致で探す。
    """
    if not text:
        return None
    low = text.lower()
    for kw in keywords or []:
        if not kw:
            continue
        k = str(kw).lower()
        if k.isascii():
            if " " in k or "'" in k or "-" in k:
                pattern = r"\b" + re.escape(k) + r"\b"
            else:
                pattern = r"\b" + re.escape(k) + r"\w*"
            for m in re.finditer(pattern, low):
                if not _keyword_negated(low, m.start(), m.end(), k):
                    return kw
        else:
            idx = low.find(k)
            while idx >= 0:
                if not _keyword_negated(low, idx, idx + len(k), k):
                    return kw
                idx = low.find(k, idx + 1)
    return None


def resolve_fatal_keywords(cfg=None, appid=None):
    """致命語の実効辞書（既定＋config の追加分）を返す。"""
    merged = dict(DEFAULT_FATAL)
    merged.update(cfg or {})
    words = list(DEFAULT_FATAL["keywords"])
    words.extend(merged.get("extra_keywords") or [])
    if appid is not None:
        per_app = merged.get("per_app_keywords") or {}
        words.extend(per_app.get(str(appid)) or [])
    return words


def match_fatal_keyword(text, cfg=None, appid=None):
    """レビュー本文に致命語が含まれれば、そのキーワードを返す。"""
    merged = dict(DEFAULT_FATAL)
    merged.update(cfg or {})
    if not merged.get("enabled", True):
        return None
    return find_keyword_hit(text, resolve_fatal_keywords(cfg, appid))


def resolve_topic_dict(cfg=None, appid=None):
    """論点辞書の実効形（既定＋config の追加分）を返す。"""
    merged = dict(DEFAULT_TOPICS)
    merged.update(cfg or {})
    topics = {name: list(words) for name, words in DEFAULT_TOPICS["topics"].items()}
    for source in (merged.get("topics") or {}, merged.get("extra_keywords") or {}):
        for name, words in source.items():
            topics.setdefault(name, []).extend(words or [])
    if appid is not None:
        per_app = (merged.get("per_app_keywords") or {}).get(str(appid)) or {}
        for name, words in per_app.items():
            topics.setdefault(name, []).extend(words or [])
    return topics


def count_topics(reviews, topics):
    """レビュー（DB行の辞書）のリストを論点ごとに数える。

    1件のレビューは、論点のキーワードのどれかに一致すればその論点で1件と数える
    （同じ論点の語が複数一致しても1件）。{論点: {"total": n, "down": n}} を返す。
    """
    counts = {name: {"total": 0, "down": 0} for name in topics}
    for r in reviews:
        text = r.get("review_text") or ""
        if not text:
            continue
        for name, words in topics.items():
            if find_keyword_hit(text, words):
                counts[name]["total"] += 1
                if not r.get("voted_up"):
                    counts[name]["down"] += 1
    return counts


def build_topic_lines(reviews, cfg=None, appid=None):
    """ダイジェスト用の論点集計セクション（1行）。該当が無ければ空リスト。"""
    merged = dict(DEFAULT_TOPICS)
    merged.update(cfg or {})
    if not merged.get("enabled", True):
        return []
    counts = count_topics(reviews, resolve_topic_dict(cfg, appid))
    items = [(name, c) for name, c in counts.items() if c["total"] > 0]
    if not items:
        return []
    items.sort(key=lambda kv: (-kv[1]["total"], kv[0]))
    parts = [
        "{} {}件（低評価{}件）".format(name, c["total"], c["down"])
        for name, c in items
    ]
    return ["論点：{}".format("、".join(parts))]


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
            CREATE TABLE IF NOT EXISTS events (
                appid TEXT NOT NULL,
                event_id TEXT NOT NULL,
                title TEXT,
                event_ts INTEGER NOT NULL,
                source TEXT NOT NULL DEFAULT 'feed',
                url TEXT,
                PRIMARY KEY (appid, event_id)
            );
            CREATE INDEX IF NOT EXISTS idx_events_app_ts
                ON events (appid, event_ts);
            CREATE TABLE IF NOT EXISTS player_counts (
                appid TEXT NOT NULL,
                ts INTEGER NOT NULL,
                players INTEGER NOT NULL,
                PRIMARY KEY (appid, ts)
            );
            CREATE TABLE IF NOT EXISTS snapshots (
                appid TEXT NOT NULL,
                ts INTEGER NOT NULL,
                total_reviews INTEGER,
                total_positive INTEGER,
                total_negative INTEGER,
                review_score_desc TEXT,
                player_count INTEGER,
                discount_percent INTEGER,
                PRIMARY KEY (appid, ts)
            );
            CREATE TABLE IF NOT EXISTS app_metadata (
                appid TEXT NOT NULL,
                ts INTEGER NOT NULL,
                info_json TEXT NOT NULL,
                PRIMARY KEY (appid, ts)
            );
            CREATE TABLE IF NOT EXISTS achievement_snapshots (
                appid TEXT NOT NULL,
                ts INTEGER NOT NULL,
                data_json TEXT NOT NULL,
                PRIMARY KEY (appid, ts)
            );
            """
        )
        self._migrate_reviews_columns()
        self.conn.commit()

    # レビューオブジェクトの項目を落とさず保存するための追加列。
    # 既存DBには「無ければ足す」形で移行する（値は後から取得した分から埋まる）。
    _REVIEW_EXTRA_COLUMNS = {
        "weighted_vote_score": "REAL",
        "comment_count": "INTEGER NOT NULL DEFAULT 0",
        "timestamp_updated": "INTEGER NOT NULL DEFAULT 0",
        "playtime_at_review": "INTEGER NOT NULL DEFAULT 0",
        "playtime_last_two_weeks": "INTEGER NOT NULL DEFAULT 0",
        "steam_purchase": "INTEGER",
        "received_for_free": "INTEGER",
        "written_during_early_access": "INTEGER",
        "refunded": "INTEGER",
        "developer_response": "TEXT",
        "timestamp_dev_responded": "INTEGER NOT NULL DEFAULT 0",
        "author_num_games_owned": "INTEGER",
        "author_num_reviews": "INTEGER",
        "raw_json": "TEXT",
    }

    def _migrate_reviews_columns(self):
        existing = {
            row[1] for row in self.conn.execute("PRAGMA table_info(reviews)")
        }
        for col, decl in self._REVIEW_EXTRA_COLUMNS.items():
            if col not in existing:
                self.conn.execute(
                    "ALTER TABLE reviews ADD COLUMN {} {}".format(col, decl)
                )

    def close(self):
        self.conn.close()

    def upsert_reviews(self, appid, reviews, now=None):
        """レビューを保存する。新規に保存できた件数を返す（重複は内容の更新のみ）。

        GetAppReviews のレビューオブジェクトが返す項目は落とさず保存し、
        生の JSON も raw_json 列にそのまま残す（将来の仕様追加に備える）。
        フィールドが応答に無い場合は NULL / 0 で保存する。
        """
        now = int(now if now is not None else time.time())
        inserted = 0

        def bool_int(v):
            return None if v is None else (1 if v else 0)

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
            try:
                weighted = float(r.get("weighted_vote_score"))
            except (TypeError, ValueError):
                weighted = None
            self.conn.execute(
                """
                INSERT INTO reviews (
                    appid, recommendationid, author_steamid, voted_up, language,
                    review_text, votes_up, votes_funny, playtime_forever,
                    timestamp_created, first_seen_at,
                    weighted_vote_score, comment_count, timestamp_updated,
                    playtime_at_review, playtime_last_two_weeks,
                    steam_purchase, received_for_free,
                    written_during_early_access, refunded,
                    developer_response, timestamp_dev_responded,
                    author_num_games_owned, author_num_reviews, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(appid, recommendationid) DO UPDATE SET
                    voted_up = excluded.voted_up,
                    votes_up = excluded.votes_up,
                    votes_funny = excluded.votes_funny,
                    review_text = excluded.review_text,
                    weighted_vote_score = excluded.weighted_vote_score,
                    comment_count = excluded.comment_count,
                    timestamp_updated = excluded.timestamp_updated,
                    playtime_forever = excluded.playtime_forever,
                    playtime_at_review = excluded.playtime_at_review,
                    playtime_last_two_weeks = excluded.playtime_last_two_weeks,
                    developer_response = excluded.developer_response,
                    timestamp_dev_responded = excluded.timestamp_dev_responded,
                    refunded = excluded.refunded,
                    raw_json = excluded.raw_json
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
                    weighted,
                    int(r.get("comment_count", 0) or 0),
                    int(r.get("timestamp_updated", 0) or 0),
                    int(author.get("playtime_at_review", 0) or 0),
                    int(author.get("playtime_last_two_weeks", 0) or 0),
                    bool_int(r.get("steam_purchase")),
                    bool_int(r.get("received_for_free")),
                    bool_int(r.get("written_during_early_access")),
                    bool_int(r.get("refunded")),
                    r.get("developer_response"),
                    int(r.get("timestamp_dev_responded", 0) or 0),
                    author.get("num_games_owned"),
                    author.get("num_reviews"),
                    json.dumps(r, ensure_ascii=False),
                ),
            )
        self.conn.commit()
        return inserted

    def record_snapshot(self, appid, ts=None, total_reviews=None, total_positive=None,
                        total_negative=None, review_score_desc=None,
                        player_count=None, discount_percent=None):
        """チェック時点のスナップショット（公式集計・同時プレイヤー数・割引率）を記録する。

        取れた項目だけ渡す。時系列データは後からさかのぼって取れないため、
        集計に使わなくても保存しておく。
        """
        ts = int(ts if ts is not None else time.time())
        self.conn.execute(
            """
            INSERT OR REPLACE INTO snapshots (
                appid, ts, total_reviews, total_positive, total_negative,
                review_score_desc, player_count, discount_percent
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(appid), ts, total_reviews, total_positive, total_negative,
                review_score_desc, player_count, discount_percent,
            ),
        )
        self.conn.commit()

    def latest_snapshot(self, appid):
        row = self.conn.execute(
            """
            SELECT * FROM snapshots WHERE appid = ?
            ORDER BY ts DESC LIMIT 1
            """,
            (str(appid),),
        ).fetchone()
        return dict(row) if row else None

    def record_app_metadata_if_changed(self, appid, info, ts=None):
        """アプリ基本情報を、変更があったときだけ履歴に残す。残したら True。"""
        ts = int(ts if ts is not None else time.time())
        payload = json.dumps(info, ensure_ascii=False, sort_keys=True)
        row = self.conn.execute(
            """
            SELECT info_json FROM app_metadata WHERE appid = ?
            ORDER BY ts DESC LIMIT 1
            """,
            (str(appid),),
        ).fetchone()
        if row and row["info_json"] == payload:
            return False
        self.conn.execute(
            "INSERT INTO app_metadata (appid, ts, info_json) VALUES (?, ?, ?)",
            (str(appid), ts, payload),
        )
        self.conn.commit()
        return True

    def record_achievement_snapshot(self, appid, achievements, ts=None):
        """実績の全体達成率のスナップショットを保存する。"""
        ts = int(ts if ts is not None else time.time())
        self.conn.execute(
            """
            INSERT OR REPLACE INTO achievement_snapshots (appid, ts, data_json)
            VALUES (?, ?, ?)
            """,
            (str(appid), ts, json.dumps(achievements, ensure_ascii=False)),
        )
        self.conn.commit()

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

    def upsert_event(self, appid, event_id, title, event_ts, source="feed", url=None):
        """イベントを保存する（同じ appid × event_id は上書き）。"""
        self.conn.execute(
            """
            INSERT INTO events (appid, event_id, title, event_ts, source, url)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(appid, event_id) DO UPDATE SET
                title = excluded.title,
                event_ts = excluded.event_ts,
                source = excluded.source,
                url = excluded.url
            """,
            (str(appid), str(event_id), title or "", int(event_ts), source, url or ""),
        )
        self.conn.commit()

    def list_events(self, appid):
        """イベントを新しい順に返す。"""
        rows = self.conn.execute(
            """
            SELECT * FROM events WHERE appid = ?
            ORDER BY event_ts DESC
            """,
            (str(appid),),
        ).fetchall()
        return [dict(r) for r in rows]

    def latest_event(self, appid, at_or_before=None):
        """指定時刻以前で最も新しいイベントを返す。無ければ None。"""
        if at_or_before is None:
            rows = self.list_events(appid)
            return rows[0] if rows else None
        row = self.conn.execute(
            """
            SELECT * FROM events WHERE appid = ? AND event_ts <= ?
            ORDER BY event_ts DESC LIMIT 1
            """,
            (str(appid), int(at_or_before)),
        ).fetchone()
        return dict(row) if row else None

    def delete_event(self, appid, event_id):
        """イベントを削除する。削除できたら True。"""
        cur = self.conn.execute(
            "DELETE FROM events WHERE appid = ? AND event_id = ?",
            (str(appid), str(event_id)),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def record_player_count(self, appid, players, ts=None):
        """同時プレイヤー数の観測を1点記録する（取得した時点の点として貯める）。"""
        ts = int(ts if ts is not None else time.time())
        self.conn.execute(
            """
            INSERT OR REPLACE INTO player_counts (appid, ts, players)
            VALUES (?, ?, ?)
            """,
            (str(appid), ts, int(players)),
        )
        self.conn.commit()

    def player_count_stats(self, appid, start_ts, end_ts):
        """[start_ts, end_ts) の同時プレイヤー数の観測点の集計。無ければ None。"""
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS c, AVG(players) AS a, MAX(players) AS m
            FROM player_counts
            WHERE appid = ? AND ts >= ? AND ts < ?
            """,
            (str(appid), int(start_ts), int(end_ts)),
        ).fetchone()
        if not row or not row["c"]:
            return None
        return {"count": row["c"], "avg": row["a"], "max": row["m"]}


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
        "neg_languages": Counter(
            r["language"] or "" for r in reviews if not r["voted_up"]
        ),
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


def build_digest_text(store, appid, app_name, now=None, days=7, config=None):
    """週次ダイジェストのテキストを生成する。"""
    now = int(now if now is not None else time.time())
    cur_start = now - days * DAY
    prev_start = now - 2 * days * DAY
    cur_reviews = store.reviews_between(appid, cur_start, now)
    cur = window_stats(cur_reviews)
    prev = window_stats(store.reviews_between(appid, prev_start, cur_start))
    name = app_name or store.get_app_name(appid) or "App {}".format(appid)
    analytics_cfg = (config or {}).get("analytics", {}) or {}

    lines = []
    lines.append(
        "📊【週次ダイジェスト】{}（{}〜{}）".format(name, _fmt_date(cur_start), _fmt_date(now - 1))
    )
    lines.append("結論：{}".format(_digest_conclusion(cur, prev)))
    if cur["total"] == 0:
        lines.extend(
            build_player_lines(
                store, appid, now=now, days=days, cfg=analytics_cfg.get("players")
            )
        )
        lines.extend(
            build_event_comparison_lines(
                store, appid, now=now, days=days, cfg=analytics_cfg.get("events")
            )
        )
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
    # 論点キーワード集計（軽い版：辞書一致ベース、AI不使用）
    lines.extend(
        build_topic_lines(cur_reviews, cfg=analytics_cfg.get("topics"), appid=appid)
    )
    # 同時プレイヤー数（記録がある期間だけ）
    lines.extend(
        build_player_lines(
            store, appid, now=now, days=days, cfg=analytics_cfg.get("players")
        )
    )

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
    # イベント前後の比較（イベントがあるタイトルだけ）
    events_cfg = ((config or {}).get("analytics", {}) or {}).get("events")
    lines.extend(
        build_event_comparison_lines(store, appid, now=now, days=days, cfg=events_cfg)
    )
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
    # 言語別の増加内訳（低評価のみ。平常時の同言語の件数を窓の長さで割り付けた期待値との差）
    scale = (
        cfg["recent_hours"] / (cfg["baseline_days"] * 24.0)
        if cfg["baseline_days"] else 0.0
    )
    deltas = []
    lang_codes = set(result["recent"]["neg_languages"]) | set(
        result["baseline"]["neg_languages"]
    )
    for code in lang_codes:
        delta = result["recent"]["neg_languages"].get(code, 0) - (
            result["baseline"]["neg_languages"].get(code, 0) * scale
        )
        if delta >= 1:
            deltas.append((code, delta))
    deltas.sort(key=lambda kv: -kv[1])
    if deltas:
        parts = [
            "{} +{}".format(language_name(code), int(round(delta)))
            for code, delta in deltas[:3]
        ]
        lines.append("言語別の増加（低評価）：{}".format("、".join(parts)))
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


DEFAULT_SALE_EVENTS = {
    "enabled": True,  # 割引の開始・終了をイベントとして自動登録するか
}


def detect_sale_event(store, appid, discount_percent, now=None):
    """割引率の変化を見て、セール開始／終了をイベントとして自動登録する。

    前回の割引率は meta の `sale_discount:<appid>` に保持する。
    登録したら {"event_id", "title", "event_ts"} を、変化が無ければ None を返す。
    初回観測で割引中だった場合は、その時点で「セール開始」として登録する
    （実際にいつ始まったかは分からないため、検知した時点の日付になる）。
    """
    now = int(now if now is not None else time.time())
    cur = int(discount_percent or 0)
    key = "sale_discount:{}".format(appid)
    raw = store.get_meta(key)
    prev = None
    if raw is not None:
        try:
            prev = int(raw)
        except (TypeError, ValueError):
            prev = None
    store.set_meta(key, cur)
    title = None
    if cur > 0 and (prev is None or prev <= 0):
        title = "セール開始（-{}%）".format(cur)
    elif cur <= 0 and prev is not None and prev > 0:
        title = "セール終了"
    if title is None:
        return None
    event_id = "sale-{}-{}".format(now, "start" if cur > 0 else "end")
    store.upsert_event(appid, event_id, title, now, source="sale", url="")
    return {"event_id": event_id, "title": title, "event_ts": now}


DEFAULT_PLAYERS = {
    "enabled": True,  # 同時プレイヤー数を記録し、ダイジェストに出すか
}


def build_player_lines(store, appid, now=None, days=7, cfg=None):
    """同時プレイヤー数のダイジェスト行。記録が無い期間は出さない。

    記録は「取得した時点の点」の集まりであり、欠けた時間帯の値は分からない。
    """
    merged = {**DEFAULT_PLAYERS, **(cfg or {})}
    if not merged.get("enabled", True):
        return []
    now = int(now if now is not None else time.time())
    cur = store.player_count_stats(appid, now - days * DAY, now)
    if cur is None:
        return []
    span = "今週" if days == 7 else "直近{}日".format(days)
    line = "同時プレイヤー：{}の平均 {:.0f}人・最大 {}人".format(
        span, cur["avg"], cur["max"]
    )
    prev = store.player_count_stats(appid, now - 2 * days * DAY, now - days * DAY)
    if prev is not None:
        line += "（先週の平均 {:.0f}人）".format(prev["avg"])
    return [line]


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


DEFAULT_EVENTS = {
    "enabled": True,  # 公式発表からのイベント候補の抽出・前後比較を使うか
    # タイトルに含まれていたらイベント候補にするキーワード（小文字で比較）
    "keywords": ["update", "patch", "hotfix", "アップデート", "更新", "修正"],
    "version_pattern": True,  # 「1.6.14」のようなバージョン番号らしい文字列も候補にする
    "feed_count": 20,          # 1回の同期で取得する公式発表の件数
}

# 「1.6.14」「v2.0」「Ver2.5」のようなバージョン番号らしい文字列
_VERSION_RE = re.compile(r"\b(?:ver\.?\s*)?v?\d+\.\d+(?:\.\d+)?\b", re.IGNORECASE)


def is_event_candidate(title, cfg=None):
    """公式発表のタイトルが「イベント候補」かどうかを判定する。

    あくまで候補であり、実際のアップデートかどうかは断定しない
    （告知があっても内容がアップデートとは限らないため）。
    """
    cfg = {**DEFAULT_EVENTS, **(cfg or {})}
    t = title or ""
    low = t.lower()
    for kw in cfg.get("keywords") or []:
        if kw and str(kw).lower() in low:
            return True
    if cfg.get("version_pattern", True) and _VERSION_RE.search(t):
        return True
    return False


def extract_event_candidates(news_items, cfg=None):
    """公式発表のリストからイベント候補だけを抽出して返す。"""
    cfg = {**DEFAULT_EVENTS, **(cfg or {})}
    if not cfg.get("enabled", True):
        return []
    candidates = []
    for item in news_items or []:
        title = item.get("title") or ""
        if not is_event_candidate(title, cfg):
            continue
        candidates.append(
            {
                "event_id": "feed-{}".format(item.get("gid") or ""),
                "title": title,
                "event_ts": int(item.get("date", 0) or 0),
                "url": item.get("url") or "",
            }
        )
    return candidates


def sync_feed_events(store, appid, news_items, cfg=None):
    """公式発表からイベント候補を抽出して保存する。保存した候補の件数を返す。"""
    candidates = extract_event_candidates(news_items, cfg)
    for c in candidates:
        if c["event_ts"] > 0:
            store.upsert_event(
                appid, c["event_id"], c["title"], c["event_ts"],
                source="feed", url=c["url"],
            )
    return len(candidates)


def add_manual_event(store, appid, title, event_ts):
    """手動イベントを追加する。event_id を返す。"""
    digest = hashlib.sha1(
        "{}:{}".format(title, int(event_ts)).encode("utf-8")
    ).hexdigest()[:12]
    event_id = "manual-{}".format(digest)
    store.upsert_event(appid, event_id, title, int(event_ts), source="manual", url="")
    return event_id


def _lang_summary(stats, limit=3):
    parts = [
        "{} {}件".format(language_name(code), n)
        for code, n in stats["languages"].most_common(limit)
    ]
    return "、".join(parts) if parts else "—"


def build_event_comparison_lines(store, appid, now=None, days=7, cfg=None):
    """直近のイベントを境にした「前（同日数）vs 後」の比較セクションを作る。

    イベントが無いタイトルでは空リストを返す（セクション自体を出さない）。
    """
    cfg = {**DEFAULT_EVENTS, **(cfg or {})}
    if not cfg.get("enabled", True):
        return []
    now = int(now if now is not None else time.time())
    event = store.latest_event(appid, at_or_before=now)
    if event is None:
        return []
    ets = int(event["event_ts"])
    span = int(days) * DAY
    before = window_stats(store.reviews_between(appid, ets - span, ets))
    after_end = min(ets + span, now)
    after = window_stats(store.reviews_between(appid, ets, after_end))

    def ratio_txt(stats):
        return (
            "{:.0f}%".format(stats["up_ratio"] * 100)
            if stats["up_ratio"] is not None else "—"
        )

    lines = []
    lines.append(
        "▼ イベント前後の比較：「{}」（{}）".format(event["title"], _fmt_date(ets))
    )
    if ets + span > now:
        lines.append("※イベント後はまだ{}日分そろっていません（集計途中）".format(days))
    lines.append(
        "前（{}日）：{}件｜👍 {} / 👎 {}｜おすすめ率 {}".format(
            days, before["total"], before["up"], before["down"], ratio_txt(before)
        )
    )
    lines.append(
        "後（{}日）：{}件｜👍 {} / 👎 {}｜おすすめ率 {}".format(
            days, after["total"], after["up"], after["down"], ratio_txt(after)
        )
    )
    lines.append("言語（前）：{}".format(_lang_summary(before)))
    lines.append("言語（後）：{}".format(_lang_summary(after)))
    return lines


def main():
    parser = argparse.ArgumentParser(description="こえびん 分析層（ダイジェスト／急増検知）")
    parser.add_argument("--config", default="config.json", help="設定ファイルのパス")
    parser.add_argument(
        "command",
        choices=["digest", "check-surge", "backfill", "sync-events", "events",
                 "add-event", "remove-event"],
        help="実行する処理",
    )
    parser.add_argument("--dry-run", action="store_true", help="送信せず標準出力に表示する")
    parser.add_argument("--force", action="store_true", help="backfill を実施済みでも再実行する")
    parser.add_argument("--appid", help="add-event / remove-event の対象タイトル")
    parser.add_argument("--date", help="add-event の日付（YYYY-MM-DD）")
    parser.add_argument("--title", help="add-event のイベント名")
    parser.add_argument("--event-id", help="remove-event で消すイベントのID")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        print("設定ファイルが見つかりません: {}".format(config_path), file=sys.stderr)
        return 1
    config = json.loads(config_path.read_text(encoding="utf-8"))

    # 通知の組み立ては通知ツール側の実装を再利用する
    from steam_review_notifier import (
        build_notifier,
        fetch_announcements,
        fetch_app_name,
        fetch_reviews_page,
    )

    if args.command in ("sync-events", "events", "add-event", "remove-event"):
        store = open_store(config, config_path.parent)
        try:
            app_ids = [str(a) for a in config.get("app_ids", [])]
            if args.command == "sync-events":
                events_cfg = (config.get("analytics", {}) or {}).get("events")
                feed_count = int(
                    {**DEFAULT_EVENTS, **(events_cfg or {})}.get("feed_count", 20)
                )
                for appid in app_ids:
                    items = fetch_announcements(
                        appid, count=feed_count,
                        base_url=config.get("_news_base_url"),
                    )
                    n = sync_feed_events(store, appid, items, cfg=events_cfg)
                    print("appid={}: イベント候補 {} 件を登録しました".format(appid, n))
            elif args.command == "events":
                for appid in app_ids:
                    events = store.list_events(appid)
                    print("appid={}: {} 件".format(appid, len(events)))
                    for ev in events:
                        print(
                            "  {} | {} | {} | {}".format(
                                ev["event_id"],
                                datetime.fromtimestamp(
                                    ev["event_ts"], tz=timezone.utc
                                ).astimezone().strftime("%Y-%m-%d"),
                                ev["source"],
                                ev["title"],
                            )
                        )
            elif args.command == "add-event":
                if not args.appid or not args.date or not args.title:
                    print("add-event には --appid, --date, --title が必要です", file=sys.stderr)
                    return 1
                try:
                    ts = int(
                        datetime.strptime(args.date, "%Y-%m-%d")
                        .replace(tzinfo=timezone.utc).timestamp()
                    )
                except ValueError:
                    print("--date は YYYY-MM-DD で指定してください", file=sys.stderr)
                    return 1
                event_id = add_manual_event(store, str(args.appid), args.title, ts)
                print("登録しました: {}".format(event_id))
            else:  # remove-event
                if not args.appid or not args.event_id:
                    print("remove-event には --appid, --event-id が必要です", file=sys.stderr)
                    return 1
                ok = store.delete_event(str(args.appid), args.event_id)
                print("削除しました" if ok else "該当するイベントがありませんでした")
        finally:
            store.close()
        return 0

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
                notify(build_digest_text(store, appid, name, config=config))
            else:
                sent = evaluate_and_alert(store, config, appid, name, notify)
                print("appid={}: {}".format(appid, "アラート送信" if sent else "急増なし"))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
