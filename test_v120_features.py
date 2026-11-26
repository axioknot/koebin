# v1.2.0 の追加機能のテスト（論点集計の作り直し／並びの選択／操作画面の集める系）
import contextlib
import http.client
import io
import json
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

import koebin_ui
from koebin_ui import App, Handler
from steam_review_analytics import (
    ReviewStore,
    backfill_history,
    count_topics,
    find_keyword_hit,
    resolve_topic_dict,
)
from steam_review_notifier import (
    MultiNotifier,
    _retry_pending,
    fetch_reviews_page,
)

APPID = 123
NOW = 1_800_000_000


class KeywordBoundaryTest(unittest.TestCase):
    def test_ascii_keyword_next_to_cjk_matches(self):
        # 「修bug」のように漢字に隣接した英語キーワードも一致する
        self.assertEqual(find_keyword_hit("修bug修不明白", ["bug"]), "bug")
        self.assertEqual(find_keyword_hit("中文のkey bindsが変", ["key binds"]), "key binds")

    def test_ascii_keyword_still_needs_boundary(self):
        # 英単語の途中では一致しない
        self.assertIsNone(find_keyword_hit("debugger", ["bug"]))


class TopicsElevenTest(unittest.TestCase):
    def test_eleven_themes(self):
        topics = resolve_topic_dict(None, APPID)
        self.assertEqual(
            set(topics.keys()),
            {"更新", "MOD", "AI素材", "オンライン", "バグ", "難易度",
             "ローカライズ", "操作性", "価格", "パフォーマンス", "設計"},
        )

    def test_new_themes_classify(self):
        reviews = [
            {"review_text": "アップデートでセーブが壊れた", "voted_up": 0},
            {"review_text": "MODが使えなくなった", "voted_up": 0},
            {"review_text": "AI生成の音楽がひどい", "voted_up": 0},
            {"review_text": "つまらない。作業ゲー", "voted_up": 0},
        ]
        counts = count_topics(reviews, resolve_topic_dict(None, APPID))
        self.assertEqual(counts["更新"]["total"], 1)
        self.assertEqual(counts["MOD"]["total"], 1)
        self.assertEqual(counts["AI素材"]["total"], 1)
        self.assertEqual(counts["設計"]["total"], 1)


class FetchSortTest(unittest.TestCase):
    def _capture(self, **kwargs):
        seen = {}

        def fake_get(url, params):
            seen.update(json.loads(params["input_json"]))
            return {"response": {"reviews": [], "cursor": "c2"}}

        with mock.patch(
            "steam_review_notifier.http_get_json", side_effect=fake_get
        ):
            fetch_reviews_page(APPID, **kwargs)
        return seen

    def test_recent_is_filter_1(self):
        self.assertEqual(self._capture()["filter"], 1)

    def test_helpful_is_filter_0_with_unlimited_day_range(self):
        # 公式文書の列挙（0=Helpful／1=Recent／3=Funny）。v1.2.0 で 3 と
        # 取り違えた回帰の防止。helpful では day_range=0（無制限）も渡す
        params = self._capture(sort="helpful")
        self.assertEqual(params["filter"], 0)
        self.assertEqual(params["day_range"], 0)

    def test_recent_has_no_day_range(self):
        self.assertNotIn("day_range", self._capture(sort="recent"))

    def test_unknown_sort_rejected(self):
        with self.assertRaises(ValueError):
            fetch_reviews_page(APPID, sort="funny")


def _page(reviews, cursor="next"):
    return (reviews, cursor)


class BackfillSortTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _old_reviews(self, n, base_ts):
        return [
            {
                "recommendationid": str(i),
                "author": {"steamid": "1"},
                "review": "x",
                "voted_up": True,
                "timestamp_created": base_ts - i * 60,
            }
            for i in range(n)
        ]

    def test_helpful_ignores_days_cutoff(self):
        old_ts = NOW - 300 * 86400  # 300日前（days=90 の範囲外）
        pages = {0: self._old_reviews(3, old_ts)}

        def fetch_page(appid, cursor):
            return _page(pages[0], cursor=None)

        result = backfill_history(
            self.store, APPID, fetch_page,
            cfg={"sort": "helpful", "days": 90, "max_reviews": 2000,
                 "page_sleep_sec": 0},
            now=NOW, sleep=lambda s: None,
        )
        self.assertEqual(result["fetched"], 3)

    def test_recent_respects_days_cutoff(self):
        old_ts = NOW - 300 * 86400

        def fetch_page(appid, cursor):
            return _page(self._old_reviews(3, old_ts), cursor=None)

        result = backfill_history(
            self.store, APPID, fetch_page,
            cfg={"sort": "recent", "days": 90, "max_reviews": 2000,
                 "page_sleep_sec": 0},
            now=NOW, sleep=lambda s: None,
        )
        self.assertEqual(result["fetched"], 0)


class RetryPendingMessageTest(unittest.TestCase):
    def test_no_destination_message(self):
        state = {
            "__pending__": {
                str(APPID): {
                    "r1": {"message": "m", "remaining": ["Discord"]},
                }
            }
        }
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _retry_pending(state, MultiNotifier([]))
        out = buf.getvalue()
        self.assertIn("再送待ち解除", out)
        self.assertNotIn("届けました", out)
        self.assertNotIn("__pending__", state)

    def test_delivered_message_kept(self):
        state = {
            "__pending__": {
                str(APPID): {
                    "r1": {"message": "m", "remaining": ["Discord"]},
                }
            }
        }
        notify = MultiNotifier([("Discord", lambda msg: None)])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            _retry_pending(state, notify)
        self.assertIn("届けました", buf.getvalue())


class UiCollectHttpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.config_path = Path(self.tmp.name) / "config.json"
        self.config_path.write_text(
            json.dumps({
                "app_ids": [APPID],
                "discord_webhook_url": "https://discord.com/api/webhooks/x/y",
                "fetch_only": True,
            }),
            encoding="utf-8",
        )
        self.app = App(self.config_path, "test-token")
        Handler.app = self.app
        from http.server import ThreadingHTTPServer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def wait_job_done(self):
        for _ in range(50):
            _, data = self.request("GET", "/job-status?t=test-token")
            payload = json.loads(data.decode("utf-8"))
            if not payload["running"]:
                return payload
            time.sleep(0.1)
        self.fail("ジョブが終わりませんでした")

    def test_top_has_collect_section(self):
        status, body = self.request("GET", "/?t=test-token")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        self.assertIn("/action/collect-check", text)
        self.assertIn("/action/backfill", text)
        self.assertIn("/download/snapshots", text)

    def test_collect_check_uses_empty_notifier(self):
        seen = {}

        def fake_check(config, state_path, notify, store=None):
            seen["notify"] = notify
            return SimpleNamespace(notified=2, failed_apps=0)

        with mock.patch.object(koebin_ui, "check_once", side_effect=fake_check):
            status, _ = self.request(
                "POST", "/action/collect-check?t=test-token", body="",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(status, 303)
            payload = self.wait_job_done()
        self.assertTrue(payload["ok"])
        self.assertEqual(seen["notify"].destinations, [])
        self.assertIn("新規に取り込んだレビュー 2 件", payload["log"])

    def test_backfill_job_passes_sort_and_max(self):
        seen = {}

        def fake_backfill(store, appid, fetch_page, cfg=None):
            seen.update(cfg or {})
            return {"status": "complete", "fetched": 0, "reason": ""}

        form = urllib.parse.urlencode(
            {"t": "test-token", "sort": "helpful", "max_reviews": "50"}
        )
        with mock.patch.object(koebin_ui, "backfill_history", side_effect=fake_backfill):
            status, _ = self.request(
                "POST", "/action/backfill", body=form,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            self.assertEqual(status, 303)
            payload = self.wait_job_done()
        self.assertTrue(payload["ok"])
        self.assertEqual(seen.get("sort"), "helpful")
        self.assertEqual(seen.get("max_reviews"), 50)

    def test_download_snapshots(self):
        status, body = self.request("GET", "/download/snapshots?t=test-token")
        self.assertEqual(status, 200)
        self.assertIn("App ID", body.decode("utf-8-sig"))


if __name__ == "__main__":
    unittest.main()


class AutoBackfillSortTest(unittest.TestCase):
    def test_maybe_backfill_passes_sort_to_fetch(self):
        import steam_review_notifier as notifier

        store = mock.Mock()
        store.get_meta.return_value = None
        captured = {}

        def fake_backfill(store_, appid, fetch_page, cfg=None):
            captured["cfg"] = cfg
            captured["page"] = fetch_page(appid, "*")
            return {"status": "complete", "fetched": 0}

        config = {"analytics": {"backfill": {"sort": "helpful", "days": 90}}}
        with mock.patch.object(notifier, "backfill_history", side_effect=fake_backfill), \
             mock.patch.object(notifier, "fetch_reviews_page", return_value=([], "*")) as fp:
            notifier._maybe_backfill(config, store, 123)
        self.assertEqual(fp.call_args.kwargs.get("sort"), "helpful")
        self.assertEqual(captured["cfg"]["sort"], "helpful")


class CollectJobNoNotifierTest(unittest.TestCase):
    def test_collect_check_runs_without_notifier_or_fetch_only(self):
        with TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.json"
            cfg_path.write_text(json.dumps({
                "app_ids": [123],
                "webhook_urls": [],
                "analytics": {"enabled": False},
            }), encoding="utf-8")
            with mock.patch.object(koebin_ui, "check_once", return_value=SimpleNamespace(notified=0, failed_apps=0)) as co:
                with contextlib.redirect_stdout(io.StringIO()):
                    koebin_ui.job_collect_check(str(cfg_path))
        self.assertTrue(co.called)
