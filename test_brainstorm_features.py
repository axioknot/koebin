#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
こえびん 推奨5件＋データ取得拡張のテスト（フィクスチャのみ。実APIには接続しない）

実行: python3 -m unittest test_brainstorm_features -v

対象:
- 致命語キーワード照合（日英中・否定文脈のガード・タイトル固有語）と即時アラート
- 急増アラートへの言語別増加内訳の追加
- 評価ラベル陥落の計算（公式ラベル使用）とアラートの重複防止
- 通知への翻訳リンク付与
- ダイジェストへの論点キーワード集計
- セール開始/終了の自動イベント登録、同時プレイヤー数の記録とダイジェスト行
- レビューの全フィールド保存＋生JSON、旧DBからの列マイグレーション
- スナップショット（query_summary・プレイヤー数・割引率）の保存
- アプリ基本情報の変更時のみ記録、実績スナップショット
- Slack / 汎用Webhook の書式変換
"""

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import steam_review_notifier as notifier
from steam_review_analytics import (
    ReviewStore,
    aggregate_rollups_by_month,
    build_digest_text,
    build_longterm_lines,
    count_topics,
    detect_sale_event,
    evaluate_surge,
    format_surge_text,
    import_review_histogram,
    match_fatal_keyword,
    resolve_topic_dict,
    review_permalink,
)

DAY = 86400
NOW = int(time.time())
APPID = "1234560"


def make_review(rid, days_ago=0, hours_offset=1, voted_up=True, language="japanese",
                text="レビュー本文", votes_up=0, **extra):
    r = {
        "recommendationid": str(rid),
        "author": {"steamid": "76561198000000000", "playtime_forever": 600},
        "voted_up": voted_up,
        "language": language,
        "review": text,
        "votes_up": votes_up,
        "votes_funny": 0,
        "timestamp_created": NOW - int(days_ago * DAY) - hours_offset * 3600,
    }
    r.update(extra)
    return r


# ===== 1. 致命語キーワード照合 =====

class FatalKeywordTest(unittest.TestCase):
    ON = {"enabled": True}

    def test_hits_japanese(self):
        self.assertEqual(match_fatal_keyword("ゲームがクラッシュして進めません", self.ON), "クラッシュ")
        self.assertEqual(
            match_fatal_keyword("アップデート後にセーブデータが消えました", self.ON),
            "セーブデータが消",
        )
        self.assertEqual(match_fatal_keyword("起動しないので返金しました", self.ON), "起動しない")

    def test_hits_english(self):
        self.assertEqual(match_fatal_keyword("It crashes every time I enter the dungeon", self.ON), "crash")
        self.assertEqual(match_fatal_keyword("The game won't start at all", self.ON), "won't start")
        self.assertEqual(match_fatal_keyword("My save corrupted and I lost everything", self.ON), "save corrupted")
        self.assertEqual(match_fatal_keyword("All I see is a black screen", self.ON), "black screen")

    def test_hits_chinese(self):
        self.assertEqual(match_fatal_keyword("一进入游戏就闪退", self.ON), "闪退")
        self.assertEqual(match_fatal_keyword("存档丢失了，太难受了", self.ON), "存档丢失")
        self.assertEqual(match_fatal_keyword("更新后直接黑屏", self.ON), "黑屏")

    def test_no_hit_on_benign_text(self):
        self.assertIsNone(match_fatal_keyword("セーブ機能が便利で快適です", self.ON))
        self.assertIsNone(match_fatal_keyword("とても面白いです。おすすめ", self.ON))
        self.assertIsNone(match_fatal_keyword("A relaxing farming game, very cozy", self.ON))

    def test_negation_guard(self):
        self.assertIsNone(match_fatal_keyword("安定していてクラッシュしません", self.ON))
        self.assertIsNone(match_fatal_keyword("No crashes at all, runs perfectly", self.ON))

    def test_per_app_keywords(self):
        cfg = {"enabled": True, "per_app_keywords": {"999": ["不具合XYZ"]}}
        self.assertEqual(
            match_fatal_keyword("固有の不具合XYZで止まる", cfg, appid="999"),
            "不具合XYZ",
        )
        self.assertIsNone(match_fatal_keyword("固有の不具合XYZで止まる", cfg, appid="888"))

    def test_disabled(self):
        self.assertIsNone(match_fatal_keyword("クラッシュした", {"enabled": False}))

    def test_default_off(self):
        # 設定なし・空の設定では、致命語が含まれていても発火しない（初期オフ）
        self.assertIsNone(match_fatal_keyword("ゲームがクラッシュして進めません"))
        self.assertIsNone(match_fatal_keyword("ゲームがクラッシュして進めません", {}))
        self.assertIsNone(
            match_fatal_keyword("ゲームがクラッシュして進めません", {"extra_keywords": []})
        )


# ===== 3. 評価ラベルの計算 =====

class LabelMathTest(unittest.TestCase):
    def test_official_label_drop(self):
        info = notifier.negatives_until_label_drop(60, 50, "Very Positive")
        self.assertIsNotNone(info)
        self.assertEqual(info["count"], 3)
        self.assertEqual(info["current_label"], "非常に好評")
        self.assertEqual(info["next_label"], "やや好評")

    def test_estimated_label_drop(self):
        info = notifier.negatives_until_label_drop(60, 50)
        self.assertIsNotNone(info)
        self.assertEqual(info["count"], 3)
        self.assertEqual(info["current_label"], "非常に好評")

    def test_overwhelmingly_positive_drop(self):
        info = notifier.negatives_until_label_drop(1000, 960, "Overwhelmingly Positive")
        self.assertIsNotNone(info)
        self.assertEqual(info["count"], 11)
        self.assertEqual(info["next_label"], "非常に好評")

    def test_too_few_reviews(self):
        self.assertIsNone(notifier.negatives_until_label_drop(5, 5))
        self.assertIsNone(notifier.negatives_until_label_drop(5, 5, "Very Positive"))
        self.assertIsNone(notifier.negatives_until_label_drop(0, 0, "Very Positive"))


# ===== 4. 翻訳リンク =====

class TranslateLinkTest(unittest.TestCase):
    def test_english_review_gets_link(self):
        review = make_review(1, language="english", text="Great game, loved it")
        msg = notifier.format_review_message("Fixture Game", "123", review)
        self.assertIn("翻訳で読む: https://translate.google.com/", msg)
        self.assertIn("op=translate", msg)

    def test_japanese_review_has_no_link(self):
        review = make_review(1, language="japanese", text="とても面白い")
        msg = notifier.format_review_message("Fixture Game", "123", review)
        self.assertNotIn("translate.google.com", msg)

    def test_link_can_be_disabled(self):
        review = make_review(1, language="english", text="Great game")
        msg = notifier.format_review_message(
            "Fixture Game", "123", review, translate_link=False
        )
        self.assertNotIn("translate.google.com", msg)


# ===== 送信先ごとの書式 =====

class DestinationFormatTest(unittest.TestCase):
    def _capture(self, factory):
        captured = {}
        with mock.patch.object(
            notifier, "http_post_json",
            lambda url, payload: captured.update(payload=payload),
        ):
            factory("https://example.invalid/hook")("**【見出し】**本文")
        return captured["payload"]

    def test_slack_single_asterisk(self):
        payload = self._capture(notifier.make_slack_notifier)
        self.assertEqual(payload["text"], "*【見出し】*本文")

    def test_generic_plain(self):
        payload = self._capture(notifier.make_generic_notifier)
        self.assertEqual(payload["text"], "【見出し】本文")

    def test_discord_keeps_double_asterisk(self):
        payload = self._capture(notifier.make_discord_notifier)
        self.assertEqual(payload["content"], "**【見出し】**本文")


# ===== 1（統合）. 急増アラートの言語別内訳 =====

class SurgeLanguageDeltaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_language_delta_line(self):
        baseline = []
        for i in range(50):
            baseline.append(make_review(
                "b{}".format(i), days_ago=2 + (i % 26),
                voted_up=(i >= 5), language="japanese",
                text="ベースライン{}".format(i),
            ))
        self.store.upsert_reviews("spike", baseline, now=NOW)
        recent = []
        for i in range(5):
            recent.append(make_review(
                "r-sc{}".format(i), hours_offset=2, voted_up=False,
                language="schinese", text="差评{}".format(i),
            ))
        for i in range(2):
            recent.append(make_review(
                "r-en{}".format(i), hours_offset=2, voted_up=False,
                language="english", text="bad {}".format(i),
            ))
        for i in range(3):
            recent.append(make_review(
                "r-up{}".format(i), hours_offset=1, voted_up=True,
                language="japanese", text="良い{}".format(i),
            ))
        self.store.upsert_reviews("spike", recent, now=NOW)
        result = evaluate_surge(self.store, "spike", now=NOW)
        self.assertIsNotNone(result)
        text = format_surge_text("spike", "スパイクゲーム", result)
        self.assertIn("言語別の増加（低評価）：中国語（簡体字） +5、英語 +2", text)


# ===== 5. 論点集計 =====

class TopicDigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.set_app_name(APPID, "論点ゲーム")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_count_topics(self):
        reviews = [
            {"review_text": "操作が分かりにくい", "voted_up": 0},
            {"review_text": "操作が快適", "voted_up": 1},
            {"review_text": "翻訳が機械翻訳でひどい", "voted_up": 0},
            {"review_text": "楽しい", "voted_up": 1},
        ]
        counts = count_topics(reviews, resolve_topic_dict(None, APPID))
        self.assertEqual(counts["操作性"], {"total": 2, "down": 1})
        self.assertEqual(counts["ローカライズ"], {"total": 1, "down": 1})
        self.assertEqual(counts["バグ"], {"total": 0, "down": 0})

    def test_digest_has_topic_line(self):
        reviews = [
            make_review("t1", days_ago=1, voted_up=False, text="操作が分かりにくい"),
            make_review("t2", days_ago=1, voted_up=False, text="翻訳が機械翻訳でひどい"),
            make_review("t3", days_ago=2, voted_up=False, text="バグが多くて進めない"),
            make_review("t4", days_ago=2, voted_up=True, text="楽しい"),
        ]
        self.store.upsert_reviews(APPID, reviews, now=NOW)
        text = build_digest_text(
            self.store, APPID, "論点ゲーム", now=NOW, config={"analytics": {}}
        )
        self.assertIn("論点：", text)
        self.assertIn("操作性 1件（低評価1件）", text)
        self.assertIn("ローカライズ 1件（低評価1件）", text)
        self.assertIn("バグ 1件（低評価1件）", text)


# ===== セール検知・同時プレイヤー =====

class SaleEventTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_sale_start_and_end(self):
        self.assertIsNone(detect_sale_event(self.store, "s1", 0, now=NOW))
        ev = detect_sale_event(self.store, "s1", 40, now=NOW + 60)
        self.assertEqual(ev["title"], "セール開始（-40%）")
        # 継続中は再登録しない
        self.assertIsNone(detect_sale_event(self.store, "s1", 40, now=NOW + 120))
        ev2 = detect_sale_event(self.store, "s1", 0, now=NOW + 180)
        self.assertEqual(ev2["title"], "セール終了")
        events = self.store.list_events("s1")
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["source"] == "sale" for e in events))

    def test_first_observation_on_sale(self):
        ev = detect_sale_event(self.store, "s2", 30, now=NOW)
        self.assertEqual(ev["title"], "セール開始（-30%）")


class PlayerCountDigestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.set_app_name(APPID, "プレイヤーゲーム")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_digest_player_line(self):
        for days_ago, players in [(1, 100), (2, 200), (3, 300)]:
            self.store.record_player_count(APPID, players, ts=NOW - days_ago * DAY)
        for days_ago, players in [(8, 50), (9, 150)]:
            self.store.record_player_count(APPID, players, ts=NOW - days_ago * DAY)
        text = build_digest_text(
            self.store, APPID, "プレイヤーゲーム", now=NOW, config={"analytics": {}}
        )
        self.assertIn("同時プレイヤー：今週の平均 200人・最大 300人（先週の平均 100人）", text)

    def test_no_records_no_line(self):
        text = build_digest_text(
            self.store, APPID, "プレイヤーゲーム", now=NOW, config={"analytics": {}}
        )
        self.assertNotIn("同時プレイヤー", text)


# ===== レビューの全フィールド保存 =====

class FullFieldStorageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_all_fields_saved(self):
        rich = {
            "recommendationid": "r1",
            "author": {
                "steamid": "76561198000000009",
                "playtime_forever": 5000,
                "playtime_at_review": 4000,
                "playtime_last_two_weeks": 300,
                "num_games_owned": 120,
                "num_reviews": 7,
            },
            "voted_up": False,
            "language": "english",
            "review": "rich review",
            "votes_up": 42,
            "votes_funny": 3,
            "weighted_vote_score": 0.87,
            "comment_count": 5,
            "timestamp_created": NOW - 3600,
            "timestamp_updated": NOW - 1800,
            "steam_purchase": True,
            "received_for_free": False,
            "written_during_early_access": True,
            "refunded": False,
            "developer_response": "Thank you for the feedback!",
            "timestamp_dev_responded": NOW - 900,
        }
        self.store.upsert_reviews("777", [rich], now=NOW)
        row = self.store.conn.execute(
            "SELECT * FROM reviews WHERE appid = '777'"
        ).fetchone()
        self.assertAlmostEqual(row["weighted_vote_score"], 0.87)
        self.assertEqual(row["comment_count"], 5)
        self.assertEqual(row["timestamp_updated"], NOW - 1800)
        self.assertEqual(row["playtime_at_review"], 4000)
        self.assertEqual(row["playtime_last_two_weeks"], 300)
        self.assertEqual(row["steam_purchase"], 1)
        self.assertEqual(row["received_for_free"], 0)
        self.assertEqual(row["written_during_early_access"], 1)
        self.assertEqual(row["refunded"], 0)
        self.assertEqual(row["developer_response"], "Thank you for the feedback!")
        self.assertEqual(row["timestamp_dev_responded"], NOW - 900)
        self.assertEqual(row["author_num_games_owned"], 120)
        self.assertEqual(row["author_num_reviews"], 7)
        raw = json.loads(row["raw_json"])
        self.assertEqual(raw["recommendationid"], "r1")
        self.assertEqual(raw["weighted_vote_score"], 0.87)

    def test_raw_json_excludes_author_steamid_but_keeps_structured_id_and_link(self):
        review = make_review("raw-safe")
        self.store.upsert_reviews(APPID, [review], now=NOW)
        row = self.store.conn.execute(
            "SELECT * FROM reviews WHERE appid = ? AND recommendationid = 'raw-safe'",
            (APPID,),
        ).fetchone()
        raw = json.loads(row["raw_json"])
        self.assertNotIn("steamid", raw["author"])
        self.assertEqual(raw["author"]["playtime_forever"], 600)
        self.assertEqual(row["author_steamid"], "76561198000000000")
        self.assertEqual(
            review_permalink(dict(row)),
            "https://steamcommunity.com/profiles/76561198000000000/recommended/{}/".format(APPID),
        )
        self.assertEqual(review["author"]["steamid"], "76561198000000000")

    def test_migration_from_legacy_schema(self):
        legacy_path = Path(self.tmp.name) / "legacy.db"
        conn = sqlite3.connect(str(legacy_path))
        conn.execute(
            """
            CREATE TABLE reviews (
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
            )
            """
        )
        conn.commit()
        conn.close()
        store = ReviewStore(legacy_path)
        try:
            cols = {r[1] for r in store.conn.execute("PRAGMA table_info(reviews)")}
            self.assertIn("raw_json", cols)
            self.assertIn("weighted_vote_score", cols)
            store.upsert_reviews("888", [make_review("m1")], now=NOW)
            self.assertEqual(store.count_reviews("888"), 1)
        finally:
            store.close()


# ===== メタデータ・実績の保存 =====

class MetadataStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_metadata_only_on_change(self):
        info = {"name": "Game", "genres": ["Action"]}
        self.assertTrue(self.store.record_app_metadata_if_changed("m1", info, ts=NOW))
        self.assertFalse(self.store.record_app_metadata_if_changed("m1", info, ts=NOW + 60))
        changed = {"name": "Game", "genres": ["RPG"]}
        self.assertTrue(self.store.record_app_metadata_if_changed("m1", changed, ts=NOW + 120))
        rows = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM app_metadata WHERE appid = 'm1'"
        ).fetchone()
        self.assertEqual(rows["c"], 2)

    def test_achievement_snapshot_saved(self):
        ach = [{"name": "ACH1", "percent": "12.5"}, {"name": "ACH2", "percent": "3.1"}]
        self.store.record_achievement_snapshot("m1", ach, ts=NOW)
        row = self.store.conn.execute(
            "SELECT data_json FROM achievement_snapshots WHERE appid = 'm1'"
        ).fetchone()
        self.assertEqual(json.loads(row["data_json"]), ach)


# ===== 月次集計（ヒストグラム）の取り込み =====

class HistogramStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def _ts(y, m, d=15):
        return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())

    def _rollups(self):
        return [
            {"date": self._ts(2026, 8, 5), "recommendations_up": 30, "recommendations_down": 5},
            {"date": self._ts(2026, 9, 3), "recommendations_up": 40, "recommendations_down": 10},
            # 同じ月に2件（週次粒度が混ざるケース）→ 合算される
            {"date": self._ts(2026, 9, 20), "recommendations_up": 50, "recommendations_down": 10},
            {"date": self._ts(2026, 10, 10), "recommendations_up": 80, "recommendations_down": 20},
        ]

    def test_aggregate_by_month(self):
        monthly = aggregate_rollups_by_month(self._rollups())
        self.assertEqual(monthly["2026-08"], {"up": 30, "down": 5})
        self.assertEqual(monthly["2026-09"], {"up": 90, "down": 20})
        self.assertEqual(monthly["2026-10"], {"up": 80, "down": 20})

    def test_import_and_no_duplicates(self):
        result = import_review_histogram(self.store, "999", self._rollups(), now=NOW)
        self.assertEqual(result, {"status": "imported", "months": 3})
        self.assertEqual(self.store.count_monthly_stats("999"), 3)
        # 再取り込みしても月は増えず、値も同じ（上書き）
        result2 = import_review_histogram(self.store, "999", self._rollups(), now=NOW + 60)
        self.assertEqual(result2["months"], 3)
        self.assertEqual(self.store.count_monthly_stats("999"), 3)
        rows = {r["year_month"]: r for r in self.store.monthly_stats("999")}
        self.assertEqual(rows["2026-09"]["up"], 90)

    def test_import_failure_recorded(self):
        result = import_review_histogram(self.store, "999", None, now=NOW)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.store.count_monthly_stats("999"), 0)
        self.assertIsNotNone(self.store.get_meta("histogram:999"))

    def test_longterm_lines(self):
        import_review_histogram(self.store, "999", self._rollups(), now=NOW)
        lines = build_longterm_lines(self.store, "999")
        self.assertEqual(len(lines), 1)
        self.assertIn("長期の傾向", lines[0])
        self.assertIn("直近3か月", lines[0])
        # 合計 245件 / 3か月 = 月平均 82件、おすすめ率 200/245 = 82%
        self.assertIn("月平均 82件", lines[0])
        self.assertIn("おすすめ率 82%", lines[0])

    def test_longterm_lines_absent_without_data(self):
        self.assertEqual(build_longterm_lines(self.store, "999"), [])
        import_review_histogram(self.store, "999", self._rollups(), now=NOW)
        self.assertEqual(
            build_longterm_lines(self.store, "999", {"enabled": False}), []
        )

    def test_digest_includes_longterm_line(self):
        import_review_histogram(self.store, "999", self._rollups(), now=NOW)
        text = build_digest_text(self.store, "999", "Game", now=NOW, config={})
        self.assertIn("長期の傾向", text)


# ===== 統合（フィクスチャサーバー経由） =====

class _Handler(BaseHTTPRequestHandler):
    reviews = []
    query_summary = None
    discount = 0
    players = 42
    histogram_rollups = []

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/histogram"):
            body = {"success": 1, "results": {"rollups": list(self.histogram_rollups)}}
        elif parsed.path.startswith("/players"):
            body = {"response": {"player_count": self.players, "result": 1}}
        elif parsed.path.startswith("/achievements"):
            body = {
                "achievementpercentages": {
                    "achievements": [{"name": "ACH1", "percent": "12.5"}]
                }
            }
        elif "appdetails" in self.path:
            if "price_overview" in self.path and "filters" in self.path:
                body = {"123": {"success": True, "data": {"price_overview": {
                    "currency": "JPY", "initial": 1000, "final": 600,
                    "discount_percent": self.discount,
                }}}}
            else:
                body = {"123": {"success": True, "data": {
                    "name": "Fixture Game",
                    "developers": ["Dev"], "publishers": ["Pub"],
                    "genres": [{"description": "Action"}],
                    "categories": [{"description": "Single-player"}],
                    "release_date": {"date": "2026-01-01"},
                    "supported_languages": "Japanese",
                    "price_overview": {"currency": "JPY", "initial": 1000,
                                       "final": 1000, "discount_percent": 0},
                    "metacritic": {"score": 80},
                    "recommendations": {"total": 1234},
                }}}
        else:
            body = {"response": {"reviews": list(self.reviews)}}
            if self.query_summary is not None:
                body["response"]["query_summary"] = self.query_summary
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class IntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        _Handler.reviews = []
        _Handler.query_summary = None
        _Handler.discount = 0
        _Handler.players = 42
        _Handler.histogram_rollups = []
        self.server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        root = "http://127.0.0.1:{}".format(port)
        self.config = {
            "app_ids": [123],
            "fetch_count": 20,
            "_reviews_base_url": root + "/reviews",
            "_appdetails_base_url": root + "/appdetails",
            "_news_base_url": root + "/news",
            "_players_base_url": root + "/players",
            "_achievements_base_url": root + "/achievements",
            "_histogram_base_url": root + "/histogram",
            "analytics": {"enabled": True, "db_file": "reviews.db", "surge": {}},
        }
        self.state_path = base / "state.json"
        self.store = ReviewStore(base / "reviews.db")
        self.sent = []

    def tearDown(self):
        self.server.shutdown()
        self.store.close()
        self.tmp.cleanup()

    def _check(self):
        return notifier.check_once(
            self.config, self.state_path, self.sent.append, store=self.store
        )

    def test_fatal_alert_sent_for_new_review(self):
        self.config["analytics"]["fatal_alerts"] = {"enabled": True}
        _Handler.reviews = [make_review(1, text="通常のレビュー")]
        self._check()
        self.assertEqual(len(self.sent), 0)
        _Handler.reviews.insert(0, make_review(
            2, voted_up=False, text="アップデート後にセーブデータが消えました",
        ))
        self._check()
        fatal = [m for m in self.sent if "致命的な報告の可能性" in m]
        self.assertEqual(len(fatal), 1)
        self.assertIn("セーブデータが消", fatal[0])

    def test_label_alert_fires_once(self):
        self.config["label_alert"] = {"enabled": True, "warn_within": 5}
        _Handler.query_summary = {
            "total_reviews": 60, "total_positive": 50, "total_negative": 10,
            "review_score_desc": "Very Positive",
        }
        _Handler.reviews = [make_review(1)]
        self._check()
        alerts = [m for m in self.sent if "評価ラベルの目安" in m]
        self.assertEqual(len(alerts), 1)
        self.assertIn("あと低評価3件", alerts[0])
        self.assertIn("非常に好評", alerts[0])
        self.assertIn("目安", alerts[0])
        # 同じ状況では繰り返さない
        self._check()
        alerts = [m for m in self.sent if "評価ラベルの目安" in m]
        self.assertEqual(len(alerts), 1)

    def test_alerts_default_off(self):
        # 設定で有効化していない初期状態では、条件を満たしても
        # 致命語アラートもラベルアラートも発火しない
        _Handler.query_summary = {
            "total_reviews": 60, "total_positive": 50, "total_negative": 10,
            "review_score_desc": "Very Positive",
        }
        _Handler.reviews = [make_review(1, text="通常のレビュー")]
        self._check()
        _Handler.reviews.insert(0, make_review(
            2, voted_up=False, text="アップデート後にセーブデータが消えました",
        ))
        self._check()
        self.assertEqual(
            [m for m in self.sent if "致命的な報告の可能性" in m], []
        )
        self.assertEqual(
            [m for m in self.sent if "評価ラベルの目安" in m], []
        )

    def test_histogram_auto_import_and_digest_line(self):
        def ts(y, m, d=15):
            return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp())

        _Handler.histogram_rollups = [
            {"date": ts(2026, 9, 3), "recommendations_up": 40, "recommendations_down": 10},
            {"date": ts(2026, 9, 20), "recommendations_up": 50, "recommendations_down": 10},
            {"date": ts(2026, 10, 10), "recommendations_up": 80, "recommendations_down": 20},
        ]
        _Handler.reviews = [make_review(1)]
        self._check()
        # 初回チェックで自動取り込みされ、同じ月は合算される
        self.assertEqual(self.store.count_monthly_stats("123"), 2)
        rows = {r["year_month"]: r for r in self.store.monthly_stats("123")}
        self.assertEqual(rows["2026-09"]["up"], 90)
        self.assertEqual(rows["2026-09"]["down"], 20)
        self.assertIsNotNone(self.store.get_meta("histogram:123"))
        # 再チェックしても重複しない
        self._check()
        self.assertEqual(self.store.count_monthly_stats("123"), 2)
        # ダイジェストに長期の傾向の行が出る
        text = build_digest_text(self.store, "123", "Fixture Game", now=NOW, config=self.config)
        self.assertIn("長期の傾向", text)
        self.assertIn("直近2か月", text)

    def test_snapshot_players_sale_metadata_achievements(self):
        _Handler.query_summary = {
            "total_reviews": 60, "total_positive": 50, "total_negative": 10,
            "review_score_desc": "Very Positive",
        }
        _Handler.reviews = [make_review(1)]
        self._check()
        snap = self.store.latest_snapshot("123")
        self.assertIsNotNone(snap)
        self.assertEqual(snap["total_reviews"], 60)
        self.assertEqual(snap["total_positive"], 50)
        self.assertEqual(snap["review_score_desc"], "Very Positive")
        self.assertEqual(snap["player_count"], 42)
        self.assertEqual(snap["discount_percent"], 0)
        stats = self.store.player_count_stats("123", 0, NOW + 3600)
        self.assertIsNotNone(stats)
        self.assertEqual(stats["max"], 42)
        meta_rows = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM app_metadata WHERE appid = '123'"
        ).fetchone()
        self.assertEqual(meta_rows["c"], 1)
        ach_rows = self.store.conn.execute(
            "SELECT COUNT(*) AS c FROM achievement_snapshots WHERE appid = '123'"
        ).fetchone()
        self.assertEqual(ach_rows["c"], 1)

        # セール開始 → イベント登録。継続中は増えない。終了で2件目
        _Handler.discount = 40
        self._check()
        events = [e for e in self.store.list_events("123") if e["source"] == "sale"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "セール開始（-40%）")
        self._check()
        events = [e for e in self.store.list_events("123") if e["source"] == "sale"]
        self.assertEqual(len(events), 1)
        _Handler.discount = 0
        self._check()
        events = [e for e in self.store.list_events("123") if e["source"] == "sale"]
        self.assertEqual(len(events), 2)


if __name__ == "__main__":
    unittest.main()
