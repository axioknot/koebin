#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
こえびん 通知ツールの修正に対するテスト（フィクスチャのみ。実APIには接続しない）

実行: python3 -m unittest test_notifier_fixes -v

対象:
- 送信失敗したレビューを既読にせず、失敗した送信先へだけ次回再送する
- 全送信先が失敗したら「新規通知」として数えない
- APIのエラー応答（reviews キーなし）ではベースラインを保存しない
- バックフィルの上限・期間・途中失敗の動作
- 設定の検証、状態ファイルの原子的な保存と破損時の警告
- Discord送信の allowed_mentions、通知文の形式（個別ページリンク等）
- --test オプションでテスト通知が送れること
"""

import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import steam_review_notifier as notifier
from steam_review_analytics import ReviewStore, backfill_history, evaluate_surge

DAY = 86400
NOW = int(time.time())


def make_review(rid, hours_ago=1, voted_up=True, language="japanese",
                text="レビュー本文", playtime_at_review=120, playtime_forever=6000):
    return {
        "recommendationid": str(rid),
        "author": {
            "steamid": "76561198000000001",
            "playtime_forever": playtime_forever,
            "playtime_at_review": playtime_at_review,
        },
        "voted_up": voted_up,
        "language": language,
        "review": text,
        "votes_up": 0,
        "votes_funny": 0,
        "timestamp_created": NOW - hours_ago * 3600,
    }


class _FixtureHandler(BaseHTTPRequestHandler):
    reviews = []
    mode = "ok"  # "ok" | "error"（reviews キーの無い応答を返す）

    def do_GET(self):
        if "appdetails" in self.path:
            body = {"123": {"success": True, "data": {"name": "Fixture Game"}}}
        elif self.mode == "error":
            body = {"response": {"query_summary": {"total_reviews": 0}}}
        else:
            body = {"response": {"reviews": list(self.reviews)}}
        data = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class _FixtureServerMixin:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        _FixtureHandler.reviews = []
        _FixtureHandler.mode = "ok"
        self.server = HTTPServer(("127.0.0.1", 0), _FixtureHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        port = self.server.server_address[1]
        self.config = {
            "app_ids": [123],
            "fetch_count": 20,
            "_reviews_base_url": "http://127.0.0.1:{}/reviews".format(port),
            "_appdetails_base_url": "http://127.0.0.1:{}/appdetails".format(port),
        }
        self.state_path = base / "state.json"

    def tearDown(self):
        self.server.shutdown()
        self.tmp.cleanup()


class SendFailureResendTest(_FixtureServerMixin, unittest.TestCase):
    def test_failed_destination_gets_resend_only(self):
        _FixtureHandler.reviews = [make_review(1), make_review(2)]
        discord_received = []
        slack_received = []
        discord_fail = {"on": False}

        def discord_fn(msg):
            if discord_fail["on"]:
                raise RuntimeError("Discord送信失敗（フィクスチャ）")
            discord_received.append(msg)

        notify = notifier.MultiNotifier(
            [("Discord", discord_fn), ("Slack", slack_received.append)]
        )

        # 初回: ベースラインのみ
        n = notifier.check_once(self.config, self.state_path, notify)
        self.assertEqual(n, 0)

        # 新着1件。Discordだけ失敗させる
        _FixtureHandler.reviews.insert(0, make_review(3, text="新しいレビュー"))
        discord_fail["on"] = True
        n = notifier.check_once(self.config, self.state_path, notify)
        self.assertEqual(n, 1)  # Slackには届いたので1件と数える
        self.assertEqual(len(slack_received), 1)
        self.assertEqual(len(discord_received), 0)
        # 既読に入っていないこと（state に rid=3 が無い）＋ pending に残ること
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("3", state.get("123", []))
        self.assertIn("3", state[notifier.PENDING_KEY]["123"])

        # 次回: Discordが復旧したら、Discordへだけ再送される（Slackに二重送信しない）
        discord_fail["on"] = False
        n = notifier.check_once(self.config, self.state_path, notify)
        self.assertEqual(len(discord_received), 1)
        self.assertIn("新しいレビュー", discord_received[0])
        self.assertEqual(len(slack_received), 1)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("3", state.get("123", []))
        self.assertNotIn(notifier.PENDING_KEY, state)

    def test_all_fail_counts_zero_and_retries(self):
        _FixtureHandler.reviews = [make_review(1)]
        received = {"Discord": [], "Slack": []}
        fail = {"on": False}

        def make_flaky(name):
            def flaky(msg):
                if fail["on"]:
                    raise RuntimeError("送信失敗（フィクスチャ）")
                received[name].append(msg)
            return flaky

        notify = notifier.MultiNotifier(
            [("Discord", make_flaky("Discord")), ("Slack", make_flaky("Slack"))]
        )
        notifier.check_once(self.config, self.state_path, notify)

        _FixtureHandler.reviews.insert(0, make_review(2, text="全滅レビュー"))
        fail["on"] = True
        n = notifier.check_once(self.config, self.state_path, notify)
        self.assertEqual(n, 0)  # 全送信先が失敗したら「新規通知」と数えない
        self.assertEqual(received, {"Discord": [], "Slack": []})

        fail["on"] = False
        notifier.check_once(self.config, self.state_path, notify)
        # 復旧後の再送で、各送信先に1通ずつ届く
        self.assertEqual(len(received["Discord"]), 1)
        self.assertEqual(len(received["Slack"]), 1)
        self.assertIn("全滅レビュー", received["Discord"][0])


class ErrorResponseBaselineTest(_FixtureServerMixin, unittest.TestCase):
    def test_error_response_does_not_save_baseline(self):
        _FixtureHandler.mode = "error"
        sent = []
        result = notifier.check_once(self.config, self.state_path, sent.append)
        self.assertEqual(result, 0)
        self.assertEqual(result.failed_apps, 1)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("123", state)  # ベースラインが保存されていない

        # 正常な応答に戻ったら、その回が初回としてベースラインになる（通知洪水なし）
        _FixtureHandler.mode = "ok"
        _FixtureHandler.reviews = [make_review(1), make_review(2)]
        result = notifier.check_once(self.config, self.state_path, sent.append)
        self.assertEqual(result, 0)
        self.assertEqual(result.failed_apps, 0)
        self.assertEqual(len(sent), 0)
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(sorted(state["123"]), ["1", "2"])


def _backfill_review(rid, ts):
    return {
        "recommendationid": str(rid),
        "author": {"steamid": "76561198000000002", "playtime_forever": 600},
        "voted_up": True,
        "language": "japanese",
        "review": "過去レビュー{}".format(rid),
        "votes_up": 0,
        "votes_funny": 0,
        "timestamp_created": ts,
    }


class BackfillTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    @staticmethod
    def _paged_fetch(pages):
        """pages: [(reviews, next_cursor), ...] を cursor の添字で返す fetch_page。"""
        def fetch_page(appid, cursor):
            idx = 0 if cursor == "*" else int(cursor)
            return pages[idx]
        return fetch_page

    def test_backfill_respects_days_cutoff(self):
        # 1ページ目: 10日前×3、2ページ目: 100日前×3（期間90日では全部入る想定を変え days=30 で切る）
        pages = [
            ([_backfill_review("a{}".format(i), NOW - 10 * DAY) for i in range(3)], "1"),
            ([_backfill_review("b{}".format(i), NOW - 100 * DAY) for i in range(3)], None),
        ]
        result = backfill_history(
            self.store, "999", self._paged_fetch(pages),
            cfg={"days": 30, "max_reviews": 2000, "page_sleep_sec": 0}, now=NOW,
        )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.store.count_reviews("999"), 3)  # 100日前の分は入らない
        self.assertIsNotNone(self.store.get_meta("backfill:999"))

    def test_backfill_respects_max_reviews(self):
        pages = []
        for p in range(5):
            pages.append((
                [_backfill_review("p{}-{}".format(p, i), NOW - (p + 1) * DAY) for i in range(3)],
                str(p + 1) if p < 4 else None,
            ))
        result = backfill_history(
            self.store, "999", self._paged_fetch(pages),
            cfg={"days": 90, "max_reviews": 5, "page_sleep_sec": 0}, now=NOW,
        )
        self.assertEqual(result["status"], "capped")
        # 上限5件＋ページ境界の余り（1ページ分未満）まで
        self.assertLessEqual(self.store.count_reviews("999"), 5 + 3)

    def test_backfill_partial_on_error_keeps_fetched(self):
        def fetch_page(appid, cursor):
            if cursor == "*":
                return ([_backfill_review("x{}".format(i), NOW - DAY) for i in range(3)], "1")
            raise RuntimeError("レート制限（フィクスチャ）")

        result = backfill_history(
            self.store, "999", fetch_page,
            cfg={"days": 90, "max_reviews": 2000, "page_sleep_sec": 0}, now=NOW,
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.store.count_reviews("999"), 3)  # 取れた分は保存される
        meta = json.loads(self.store.get_meta("backfill:999"))
        self.assertEqual(meta["status"], "partial")

    def test_surge_works_on_backfilled_history(self):
        # バックフィルで過去28日の平常時（低評価率10%）を取り込み、
        # その後ふつうに保存した直近の急増でアラート判定が効くことを確認する
        baseline = []
        for i in range(50):
            r = _backfill_review("base{}".format(i), NOW - (2 + i % 26) * DAY)
            r["voted_up"] = i >= 5
            baseline.append(r)
        pages = [(baseline, None)]
        result = backfill_history(
            self.store, "999", self._paged_fetch(pages),
            cfg={"days": 90, "max_reviews": 2000, "page_sleep_sec": 0}, now=NOW,
        )
        self.assertEqual(result["status"], "complete")
        recent = []
        for i in range(10):
            r = _backfill_review("new{}".format(i), NOW - 3 * 3600)
            r["voted_up"] = i >= 7
            recent.append(r)
        self.store.upsert_reviews("999", recent, now=NOW)
        self.assertIsNotNone(evaluate_surge(self.store, "999", now=NOW))


class ValidateConfigTest(unittest.TestCase):
    def _base(self):
        return {"app_ids": [123], "discord_webhook_url": "https://discord.com/api/webhooks/x"}

    def test_valid_and_normalizes(self):
        cfg = self._base()
        cfg["app_ids"] = ["123", 456]
        out = notifier.validate_config(cfg)
        self.assertEqual(out["app_ids"], [123, 456])

    def test_empty_app_ids(self):
        cfg = self._base()
        cfg["app_ids"] = []
        with self.assertRaises(ValueError) as cm:
            notifier.validate_config(cfg)
        self.assertIn("app_ids", str(cm.exception))

    def test_app_ids_not_a_list(self):
        cfg = self._base()
        cfg["app_ids"] = "123"
        with self.assertRaises(ValueError) as cm:
            notifier.validate_config(cfg)
        self.assertIn("app_ids", str(cm.exception))

    def test_app_ids_non_numeric_element(self):
        cfg = self._base()
        cfg["app_ids"] = [123, "abc"]
        with self.assertRaises(ValueError) as cm:
            notifier.validate_config(cfg)
        self.assertIn("app_ids", str(cm.exception))

    def test_bad_webhook_url(self):
        cfg = self._base()
        cfg["discord_webhook_url"] = "discord-webhook"
        with self.assertRaises(ValueError) as cm:
            notifier.validate_config(cfg)
        self.assertIn("discord_webhook_url", str(cm.exception))

    def test_no_webhook(self):
        cfg = self._base()
        cfg["discord_webhook_url"] = ""
        with self.assertRaises(ValueError):
            notifier.validate_config(cfg)

    def test_interval_out_of_range(self):
        cfg = self._base()
        cfg["interval_minutes"] = 0
        with self.assertRaises(ValueError) as cm:
            notifier.validate_config(cfg)
        self.assertIn("interval_minutes", str(cm.exception))


class StateFileTest(unittest.TestCase):
    def test_atomic_save_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            notifier.save_state(path, {"123": ["1", "2"]})
            self.assertEqual(notifier.load_state(path), {"123": ["1", "2"]})
            self.assertFalse((Path(d) / "state.json.tmp").exists())

    def test_corrupt_state_warns_and_backs_up(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "state.json"
            path.write_text("{壊れたJSON", encoding="utf-8")
            state = notifier.load_state(path)
            self.assertEqual(state, {})
            broken = list(Path(d).glob("state.json.broken-*"))
            self.assertEqual(len(broken), 1)


class MessageFormatTest(unittest.TestCase):
    def test_permalink_language_and_playtime_at_review(self):
        review = make_review(7, language="japanese", playtime_at_review=120,
                             playtime_forever=6000)
        msg = notifier.format_review_message("Fixture Game", "123", review)
        self.assertIn("言語: 日本語", msg)
        self.assertIn("プレイ時間: 2.0時間", msg)  # playtime_at_review（120分）を優先
        self.assertIn(
            "https://steamcommunity.com/profiles/76561198000000001/recommended/123/", msg
        )


class DiscordPayloadTest(unittest.TestCase):
    def test_allowed_mentions_blocked(self):
        captured = {}

        def fake_post(url, payload):
            captured["url"] = url
            captured["payload"] = payload

        with mock.patch.object(notifier, "http_post_json", fake_post):
            fn = notifier.make_discord_notifier("https://example.invalid/webhook")
            fn("@everyone テスト")
        self.assertEqual(captured["payload"].get("allowed_mentions"), {"parse": []})


class _PostRecorder(BaseHTTPRequestHandler):
    bodies = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.bodies.append(self.rfile.read(length).decode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class TestOptionTest(unittest.TestCase):
    def test_test_option_sends_one_message(self):
        _PostRecorder.bodies = []
        server = HTTPServer(("127.0.0.1", 0), _PostRecorder)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            with tempfile.TemporaryDirectory() as d:
                cfg_path = Path(d) / "config.json"
                cfg_path.write_text(json.dumps({
                    "app_ids": [123],
                    "webhook_url": "http://127.0.0.1:{}/hook".format(port),
                }), encoding="utf-8")
                argv = ["steam_review_notifier.py", "--config", str(cfg_path), "--test"]
                with mock.patch.object(sys, "argv", argv):
                    rc = notifier.main()
            self.assertEqual(rc, 0)
            self.assertEqual(len(_PostRecorder.bodies), 1)
            payload = json.loads(_PostRecorder.bodies[0])
            self.assertIn("テスト通知", payload["text"])
        finally:
            server.shutdown()

    def test_main_rejects_bad_config(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = Path(d) / "config.json"
            cfg_path.write_text(json.dumps({
                "app_ids": [],
                "webhook_url": "https://example.invalid/hook",
            }), encoding="utf-8")
            argv = ["steam_review_notifier.py", "--config", str(cfg_path)]
            with mock.patch.object(sys, "argv", argv):
                rc = notifier.main()
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()


class TestTranslateLinkFormats(unittest.TestCase):
    MSG = ("**【新着レビュー】Game**\n👎 おすすめしない\n本文です\n"
           "https://steamcommunity.com/profiles/1/recommended/2/\n"
           "翻訳で読む: https://translate.google.com/?sl=auto&tl=ja&text=abc&op=translate")

    def test_discord_masks_translate_link(self):
        out = notifier.to_discord_format(self.MSG)
        self.assertIn("[翻訳で読む](https://translate.google.com/?sl=auto&tl=ja&text=abc&op=translate)", out)
        self.assertNotIn("翻訳で読む: http", out)
        self.assertIn("**【新着レビュー】Game**", out)

    def test_slack_masks_translate_link(self):
        out = notifier.to_slack_format(self.MSG)
        self.assertIn("<https://translate.google.com/?sl=auto&tl=ja&text=abc&op=translate|翻訳で読む>", out)
        self.assertIn("*【新着レビュー】Game*", out)

    def test_plain_keeps_raw_url(self):
        out = notifier.to_plain_format(self.MSG)
        self.assertIn("翻訳で読む: https://translate.google.com/", out)
        self.assertNotIn("**", out)


class TestTranslateDisplay(unittest.TestCase):
    REVIEW = {
        "author": {"playtime_at_review": 600, "steamid": "123"},
        "voted_up": False,
        "language": "koreana",
        "review": "이 게임은 정말 재미없어요. 환불하고 싶어요.",
        "timestamp_created": 1763600000,
        "recommendationid": "999",
    }

    def test_translation_shown_and_link_omitted(self):
        def fake(text, lang):
            self.assertEqual(lang, "koreana")
            return "このゲームは本当につまらないです。返金したいです。"
        out = notifier.format_review_message(
            "Game", "646570", self.REVIEW, translator=fake)
        self.assertIn("翻訳（機械翻訳）: このゲームは本当につまらないです", out)
        self.assertIn("이 게임은 정말 재미없어요", out)  # 原文も残る
        self.assertNotIn("翻訳で読む", out)  # 翻訳が出たらリンクは付けない

    def test_fallback_to_link_when_translation_fails(self):
        out = notifier.format_review_message(
            "Game", "646570", self.REVIEW, translator=lambda t, l: None)
        self.assertIn("翻訳で読む: https://translate.google.com/", out)
        self.assertNotIn("翻訳（機械翻訳）", out)

    def test_japanese_review_never_translated(self):
        review = dict(self.REVIEW, language="japanese", review="面白いです")
        called = []
        out = notifier.format_review_message(
            "Game", "646570", review,
            translator=lambda t, l: called.append(1) or "訳")
        # build_translator 側で日本語は弾くが、format 側は translator の
        # 戻りに従う。ここでは translator が呼ばれても日本語リンクは付かない
        self.assertNotIn("翻訳で読む", out)

    def test_build_translator_disabled(self):
        self.assertIsNone(notifier.build_translator({}))
        self.assertIsNone(notifier.build_translator({"translate_display": False}))

    def test_build_translator_missing_argos(self):
        # この環境に argostranslate は無いので None（リンクで代用）になる
        self.assertIsNone(notifier.build_translator({"translate_display": True}))

    def test_steam_to_iso_mapping(self):
        self.assertEqual(notifier.STEAM_TO_ISO["koreana"], "ko")
        self.assertEqual(notifier.STEAM_TO_ISO["schinese"], "zh")
        self.assertEqual(notifier.STEAM_TO_ISO["japanese"], "ja")


class TestBuildTranslatorWithFakeArgos(unittest.TestCase):
    def setUp(self):
        import types
        self.calls = []
        calls = self.calls

        class FakeTranslation:
            def __init__(self, tag):
                self.tag = tag

            def translate(self, text):
                return self.tag + text

        def fake_from_codes(frm, to):
            calls.append((frm, to))
            if (frm, to) == ("en", "ja"):
                return FakeTranslation("訳文：")
            if (frm, to) == ("ko", "ja"):
                return None  # 日本語への直接パッケージは無い（実索引どおり）
            return FakeTranslation("EN:")

        pkg = types.ModuleType("argostranslate")
        tr = types.ModuleType("argostranslate.translate")
        tr.get_translation_from_codes = fake_from_codes
        pkg.translate = tr
        self._saved = {k: sys.modules.get(k) for k in ("argostranslate", "argostranslate.translate")}
        sys.modules["argostranslate"] = pkg
        sys.modules["argostranslate.translate"] = tr

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_translates_korean_to_ja(self):
        tr = notifier.build_translator({"translate_display": True})
        self.assertIsNotNone(tr)
        # 直接パッケージが無いので英語経由（ko→en→ja）になる
        self.assertEqual(tr("원문", "koreana"), "訳文：EN:원문")
        self.assertEqual(
            self.calls, [("ko", "ja"), ("ko", "en"), ("en", "ja")])

    def test_skips_japanese_and_unknown(self):
        tr = notifier.build_translator({"translate_display": True})
        self.assertIsNone(tr("原文", "japanese"))
        self.assertIsNone(tr("text", "klingon"))
        self.assertEqual(self.calls, [])


class StdioEncodingTest(unittest.TestCase):
    """Windows 既定（cp932）など標準出力が UTF-8 でない環境でも、
    スクリプトの日本語出力が UTF-8 で出ること（文字化け対策の回帰テスト）。"""

    def _run_script(self, args):
        import os
        import subprocess

        env = dict(os.environ, PYTHONIOENCODING="cp932")
        return subprocess.run(
            [sys.executable, "steam_review_notifier.py"] + args,
            cwd=str(Path(__file__).parent),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )

    def test_stderr_is_utf8_under_cp932_stdio(self):
        proc = self._run_script(["--config", "no_such_config.json"])
        self.assertEqual(proc.returncode, 1)
        # cp932 のバイト列のままなら UTF-8 として読めず例外になる
        err = proc.stderr.decode("utf-8")
        self.assertIn("設定ファイルが見つかりません", err)

    def test_stdout_is_utf8_under_cp932_stdio(self):
        proc = self._run_script(["--help"])
        self.assertEqual(proc.returncode, 0)
        out = proc.stdout.decode("utf-8")
        self.assertIn("Steam新着レビュー通知ツール", out)
