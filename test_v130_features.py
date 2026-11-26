# -*- coding: utf-8 -*-
"""v1.3.0 分析画面の集計関数のテスト。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import steam_review_analytics as A

NOW = 1_800_000_000
DAY = 86400


class FakeStore:
    """reviews_between／get_app_name／iter_snapshots だけを持つ簡易ストア。"""

    def __init__(self, reviews, names=None, snapshots=None, events=None):
        self.reviews = reviews  # {appid: [row,...]}
        self.names = names or {}
        self.snapshots = snapshots or {}
        self.events = events or {}

    def reviews_between(self, appid, start_ts, end_ts):
        return [
            r
            for r in self.reviews.get(str(appid), [])
            if start_ts <= r["timestamp_created"] < end_ts
        ]

    def get_app_name(self, appid):
        return self.names.get(str(appid))

    def iter_snapshots(self, appid):
        return list(self.snapshots.get(str(appid), []))

    def list_events(self, appid):
        return sorted(
            self.events.get(str(appid), []),
            key=lambda e: -e["event_ts"],
        )

    def latest_event(self, appid, at_or_before=None):
        for ev in self.list_events(appid):
            if at_or_before is None or ev["event_ts"] <= at_or_before:
                return ev
        return None


def review(text, voted_up, ts):
    return {
        "review_text": text,
        "voted_up": 1 if voted_up else 0,
        "timestamp_created": ts,
        "language": "japanese",
        "votes_up": 0,
    }


class CrossTitleSummaryTests(unittest.TestCase):
    def setUp(self):
        recent = NOW - 3 * DAY
        older = NOW - 40 * DAY
        self.store = FakeStore(
            {
                "100": [
                    review("バグで落ちる", False, recent),
                    review("クラッシュして進めないバグ", False, recent),
                    review("面白い", True, recent),
                    review("バグ報告", False, older),
                ],
                "200": [review("最高", True, recent)],
            },
            names={"100": "GameA", "200": "GameB"},
        )

    def test_counts_and_ratio(self):
        rows = A.cross_title_summary(self.store, ["100", "200"], now=NOW, days=30)
        a = rows[0]
        self.assertEqual(a["name"], "GameA")
        self.assertEqual(a["total"], 3)
        self.assertEqual(a["down"], 2)
        self.assertAlmostEqual(a["down_ratio"], 2 / 3)
        b = rows[1]
        self.assertEqual(b["total"], 1)
        self.assertEqual(b["down"], 0)
        self.assertEqual(b["top_topics"], [])

    def test_top_topics_use_down_counts_and_rose_flag(self):
        rows = A.cross_title_summary(self.store, ["100"], now=NOW, days=30)
        top = rows[0]["top_topics"]
        self.assertEqual(top[0]["name"], "バグ")
        self.assertEqual(top[0]["down"], 2)
        # 前の期間（30〜60日前）のバグ低評価は1件なので、増えた印が付く
        self.assertTrue(top[0]["rose"])

    def test_all_period_has_no_rose(self):
        rows = A.cross_title_summary(self.store, ["100"], now=NOW, days=None)
        self.assertEqual(rows[0]["total"], 4)
        self.assertFalse(rows[0]["top_topics"][0]["rose"])


class SnapshotSeriesTests(unittest.TestCase):
    def test_series_shape_and_ratio(self):
        store = FakeStore(
            {},
            snapshots={
                "100": [
                    {
                        "ts": NOW - DAY,
                        "total_reviews": 200,
                        "total_positive": 150,
                        "player_count": 42,
                        "discount_percent": 10,
                    },
                    {
                        "ts": NOW,
                        "total_reviews": None,
                        "total_positive": None,
                        "player_count": None,
                        "discount_percent": None,
                    },
                ]
            },
        )
        series = A.snapshot_series(store, "100")
        self.assertEqual(len(series), 2)
        self.assertAlmostEqual(series[0]["up_ratio"], 0.75)
        self.assertEqual(series[0]["player_count"], 42)
        self.assertIsNone(series[1]["up_ratio"])
        self.assertIsNone(series[1]["total_reviews"])


if __name__ == "__main__":
    unittest.main()


class AnalysisPageTests(unittest.TestCase):
    def setUp(self):
        import json as _json
        import time as _time

        import koebin_ui
        from steam_review_analytics import ReviewStore
        from test_analytics import make_review

        self.koebin_ui = koebin_ui
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        now = int(_time.time())
        store = ReviewStore(os.path.join(root, "reviews.db"))
        store.set_app_name("100", "GameA")
        store.upsert_reviews(
            "100",
            [
                make_review(1, 1, voted_up=False, text="バグで落ちる"),
                make_review(2, 2, voted_up=False, text="またバグ"),
                make_review(3, 1, voted_up=True, text="面白い"),
            ],
            now=now,
        )
        store.record_snapshot(
            "100", ts=now - 2 * DAY, total_reviews=100, total_positive=80,
            total_negative=20, player_count=10, discount_percent=0,
        )
        store.record_snapshot(
            "100", ts=now - DAY, total_reviews=120, total_positive=90,
            total_negative=30, player_count=12, discount_percent=10,
        )
        store.upsert_event("100", "ev1", "アップデート1.1", now - 3 * DAY)
        store.close()
        config = {
            "app_ids": [100],
            "analytics": {"enabled": True, "db_file": "reviews.db"},
        }
        config_path = os.path.join(root, "config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            _json.dump(config, f)
        self.app = koebin_ui.App(config_path, "token123")

    def tearDown(self):
        self.tmp.cleanup()

    def test_page_shows_cross_title_table_and_chart(self):
        html_out = self.app.page_analysis()
        self.assertIn("タイトル横断の論点", html_out)
        self.assertIn("GameA", html_out)
        self.assertIn("バグ 2", html_out)
        self.assertIn("<svg", html_out)
        self.assertIn("イベント前後の比較", html_out)
        self.assertIn("アップデート1.1", html_out)
        self.assertIn("前（7日）", html_out)
        self.assertIn("後（7日）", html_out)
        # ナビに分析が入り、現在ページになる
        self.assertIn('aria-current="page">分析</a>', html_out)

    def test_page_without_store_shows_note(self):
        import json as _json

        config_path = os.path.join(self.tmp.name, "config2.json")
        with open(config_path, "w", encoding="utf-8") as f:
            _json.dump({"app_ids": [100], "analytics": {"enabled": True, "db_file": "none.db"}}, f)
        app2 = self.koebin_ui.App(config_path, "token123")
        html_out = app2.page_analysis()
        self.assertIn("レビュー履歴がまだありません", html_out)


class EventComparisonTests(unittest.TestCase):
    def setUp(self):
        ets = NOW - 3 * DAY
        self.ets = ets
        self.store = FakeStore(
            {
                "100": [
                    review("バグで落ちる", False, ets - 2 * DAY),
                    review("面白い", True, ets - DAY),
                    review("バグが直っていない", False, ets + DAY),
                    review("バグ報告です", False, ets + 2 * DAY),
                    review("楽しい", True, ets + DAY),
                ]
            },
            events={
                "100": [
                    {"event_id": "ev1", "title": "アップデート1.1", "event_ts": ets}
                ]
            },
        )

    def test_before_after_counts_and_topics(self):
        comp = A.event_comparison(self.store, "100", now=NOW, days=7)
        self.assertIsNotNone(comp)
        self.assertEqual(comp["before"]["total"], 2)
        self.assertEqual(comp["before"]["down"], 1)
        self.assertEqual(comp["after"]["total"], 3)
        self.assertEqual(comp["after"]["down"], 2)
        topics = {t["name"]: t for t in comp["topics"]}
        self.assertEqual(topics["バグ"]["before_down"], 1)
        self.assertEqual(topics["バグ"]["after_down"], 2)
        self.assertTrue(comp["partial"])  # 後7日はまだそろっていない

    def test_no_event_returns_none(self):
        store = FakeStore({"100": []})
        self.assertIsNone(A.event_comparison(store, "100", now=NOW))
