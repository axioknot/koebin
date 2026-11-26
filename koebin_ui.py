#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""こえびん操作画面 — ローカル専用の小さな Web UI。

127.0.0.1 のみに bind し、起動している間だけ動く。公開サーバーではない。
通知そのものは従来どおり steam_review_notifier.py が Webhook で送る仕組みで、
この画面はその操作（実行・確認・設定の編集）をブラウザから行うためのもの。
追加のライブラリは使わず、Python 標準ライブラリのみで動く。

使い方:
    python koebin_ui.py [--config config.json] [--port 8765] [--no-browser]
"""

import argparse
import contextlib
import copy
import html
import io
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from steam_review_notifier import (
    MultiNotifier,
    acquire_lock,
    build_notifier,
    check_once,
    fetch_reviews_page,
    validate_config,
)
from steam_review_analytics import (
    backfill_history,
    build_digest_text,
    export_reviews_csv_text,
    export_snapshots_csv,
    open_store,
)

TEST_MESSAGE = "【テスト通知】こえびん\nこのメッセージが届いていれば、通知設定は正常です。"

DESTINATION_LABELS = (
    ("discord_webhook_url", "Discord"),
    ("slack_webhook_url", "Slack"),
    ("webhook_url", "Webhook（汎用）"),
)


# ---------------------------------------------------------------- 設定まわり

def load_config_raw(config_path):
    """config.json を dict で返す。無い／読めない場合は None。

    UI 側は BOM 付き保存（メモ帳等）でも開けるよう utf-8-sig で読む。
    保存し直せば BOM なしの通常形式になる。
    """
    config_path = Path(config_path)
    if not config_path.exists():
        return None
    try:
        return json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return None


def default_config():
    """新規作成時の下敷き。config.example.json があればそれを使い、無ければ最小構成。"""
    example = Path(__file__).with_name("config.example.json")
    if example.exists():
        try:
            return json.loads(example.read_text(encoding="utf-8-sig"))
        except Exception:
            pass
    return {
        "app_ids": [],
        "discord_webhook_url": "",
        "slack_webhook_url": "",
        "webhook_url": "",
        "fetch_count": 20,
        "interval_minutes": 30,
        "state_file": "state.json",
        "translate_link": True,
        "translate_display": False,
        "label_alert": {"enabled": False, "warn_within": 5},
        "analytics": {"enabled": True, "db_file": "reviews.db"},
    }


def save_config(config_path, config):
    """検証済み config を保存する。既存ファイルは config.json.bak へ1世代退避し、
    書き込みは一時ファイルを経由した原子的な置き換えにする。"""
    config_path = Path(config_path)
    if config_path.exists():
        backup = config_path.with_name(config_path.name + ".bak")
        backup.write_bytes(config_path.read_bytes())
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, config_path)


def _parse_int(form, key, label, errors, minimum=None, maximum=None):
    raw = (form.get(key) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        errors.append("{}: 数字で入力してください".format(label))
        return None
    if minimum is not None and value < minimum:
        errors.append("{}: {} 以上で入力してください".format(label, minimum))
        return None
    if maximum is not None and value > maximum:
        errors.append("{}: {} 以下で入力してください".format(label, maximum))
        return None
    return value


def apply_settings(base_config, form):
    """設定フォームの値を base_config に反映した新しい dict と、エラーの一覧を返す。

    フォームが扱わないキー（高度な設定）は base_config の値をそのまま保持する。
    最後に本体の validate_config で検証し、通らなければ保存しない前提でエラーを返す。
    """
    config = copy.deepcopy(base_config) if isinstance(base_config, dict) else {}
    errors = []

    raw_ids = form.get("app_ids") or ""
    app_ids = []
    for token in re.split(r"[\s,]+", raw_ids.strip()):
        if not token:
            continue
        if token.isdigit():
            app_ids.append(int(token))
        else:
            errors.append("App ID: 「{}」は数字ではありません".format(token))
    config["app_ids"] = app_ids

    for key, _label in DESTINATION_LABELS:
        config[key] = (form.get(key) or "").strip()

    fetch_count = _parse_int(form, "fetch_count", "取得件数", errors, minimum=1, maximum=100)
    if fetch_count is not None:
        config["fetch_count"] = fetch_count
    interval = _parse_int(form, "interval_minutes", "チェック間隔", errors, minimum=1)
    if interval is not None:
        config["interval_minutes"] = interval

    config["translate_link"] = "translate_link" in form
    config["translate_display"] = "translate_display" in form

    label_alert = dict(config.get("label_alert") or {})
    label_alert["enabled"] = "label_alert_enabled" in form
    warn = _parse_int(form, "label_alert_warn_within", "目安アラートの件数", errors, minimum=1)
    if warn is not None:
        label_alert["warn_within"] = warn
    config["label_alert"] = label_alert

    analytics = dict(config.get("analytics") or {})
    analytics["enabled"] = "analytics_enabled" in form
    config["analytics"] = analytics

    if not errors:
        try:
            config = validate_config(config)
        except ValueError as e:
            errors.extend(
                line for line in str(e).splitlines() if line.strip()
            )
    return config, errors


def mask_url(url):
    """Webhook URL を画面表示用に伏せる（方式とホスト＋末尾4文字だけ出す）。"""
    url = (url or "").strip()
    if not url:
        return ""
    parsed = urllib.parse.urlparse(url)
    tail = parsed.path[-4:] if parsed.path else ""
    return "{}://{}…{}".format(parsed.scheme, parsed.netloc, tail)


# ---------------------------------------------------------------- 実行まわり

class JobState:
    """画面からの実行（チェック／テスト通知／まとめ送信）の状態とログ。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.kind = ""
        self.log = ""
        self.ok = None
        self.finished_at = None

    def start(self, kind, target, config_path):
        with self.lock:
            if self.running:
                return False
            self.running = True
            self.kind = kind
            self.log = ""
            self.ok = None
            self.finished_at = None
        thread = threading.Thread(
            target=self._run, args=(target, config_path), daemon=True
        )
        thread.start()
        return True

    def _run(self, target, config_path):
        buf = io.StringIO()
        ok = False
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                ok = target(config_path)
        except Exception as e:  # 実行側の想定外はログに残して画面で見せる
            print("[エラー] 実行中に例外が発生しました: {}".format(e))
            ok = False
        with self.lock:
            self.log = buf.getvalue()
            self.ok = ok
            self.running = False
            self.finished_at = datetime.now().strftime("%H:%M:%S")


def _load_validated(config_path):
    raw = load_config_raw(config_path)
    if raw is None:
        print("設定ファイルが見つからないか、読み込めません: {}".format(config_path))
        return None
    try:
        return validate_config(raw)
    except ValueError as e:
        for line in str(e).splitlines():
            print("[設定エラー] {}".format(line))
        return None


def job_check(config_path):
    config = _load_validated(config_path)
    if config is None:
        return False
    notify = build_notifier(config)
    if notify is None:
        print("通知先が未設定です。設定画面でWebhook URLを保存してください。")
        return False
    config_path = Path(config_path)
    state_path = config_path.parent / config.get("state_file", "state.json")
    lock_path = state_path.with_name(state_path.name + ".lock")
    if not acquire_lock(lock_path):
        print("既にチェックが実行中です（コマンド側で動いている場合も含みます）。")
        return False
    store = None
    try:
        analytics_cfg = config.get("analytics", {}) or {}
        if analytics_cfg.get("enabled", True):
            try:
                store = open_store(config, config_path.parent)
            except Exception as e:
                print("[警告] 分析層のDBを開けませんでした（通知のみ継続します）: {}".format(e))
                store = None
        result = check_once(config, state_path, notify, store=store)
        print(
            "{} チェック完了（新規通知 {} 件）".format(
                datetime.now().strftime("%H:%M:%S"), result.notified
            )
        )
        if result.failed_apps:
            print("[警告] {} 件のタイトルでレビュー取得に失敗しました".format(result.failed_apps))
            return False
        return True
    finally:
        if store is not None:
            store.close()
        try:
            lock_path.unlink()
        except OSError:
            pass


def job_test(config_path):
    config = _load_validated(config_path)
    if config is None:
        return False
    notify = build_notifier(config)
    if notify is None:
        print("通知先が未設定です。設定画面でWebhook URLを保存してください。")
        return False
    results = notify.send(TEST_MESSAGE)
    for name, ok in results.items():
        print("{}: {}".format(name, "送信成功" if ok else "送信失敗"))
    return bool(results) and all(results.values())


def job_collect_check(config_path):
    """通知を送らずに、取得と蓄積だけを行うチェック（調査用）。

    送信先が空の通知役で通常チェックを回す。挙動はコマンド側の
    fetch_only と同じ（README の「通知せずに貯める（取得のみ）」節）。
    """
    config = _load_validated(config_path)
    if config is None:
        return False
    notify = MultiNotifier([])
    config_path = Path(config_path)
    state_path = config_path.parent / config.get("state_file", "state.json")
    lock_path = state_path.with_name(state_path.name + ".lock")
    if not acquire_lock(lock_path):
        print("既にチェックが実行中です（コマンド側で動いている場合も含みます）。")
        return False
    store = None
    try:
        analytics_cfg = config.get("analytics", {}) or {}
        if analytics_cfg.get("enabled", True):
            try:
                store = open_store(config, config_path.parent)
            except Exception as e:
                print("[警告] 分析層のDBを開けませんでした: {}".format(e))
                store = None
        result = check_once(config, state_path, notify, store=store)
        print(
            "{} 取得のみチェック完了（新規に取り込んだレビュー {} 件）".format(
                datetime.now().strftime("%H:%M:%S"), result.notified
            )
        )
        if result.failed_apps:
            print("[警告] {} 件のタイトルでレビュー取得に失敗しました".format(result.failed_apps))
            return False
        return True
    finally:
        if store is not None:
            store.close()
        try:
            lock_path.unlink()
        except OSError:
            pass


def make_backfill_job(sort="recent", max_reviews=None):
    """過去分の一括取り込みを行うジョブを作る（ボタン押下＝明示の実施）。"""
    def _job(config_path):
        config = _load_validated(config_path)
        if config is None:
            return False
        store = open_store(config, Path(config_path).parent)
        try:
            bf_cfg = dict((config.get("analytics", {}) or {}).get("backfill") or {})
            if sort in ("recent", "helpful"):
                bf_cfg["sort"] = sort
            if max_reviews:
                bf_cfg["max_reviews"] = max_reviews
            bf_sort = str(bf_cfg.get("sort") or "recent")

            def fetch_page(a, cursor):
                return fetch_reviews_page(
                    a, cursor=cursor, num=100,
                    base_url=config.get("_reviews_base_url"),
                    sort=bf_sort,
                )

            ok = True
            for appid in config.get("app_ids", []):
                appid = str(appid)
                result = backfill_history(store, appid, fetch_page, cfg=bf_cfg)
                print(
                    "appid={}: {}（{}件{}）".format(
                        appid, result["status"], result["fetched"],
                        "、理由: " + result["reason"] if result["reason"] else "",
                    )
                )
                if result["status"] == "partial":
                    ok = False
            return ok
        finally:
            store.close()

    return _job


def make_digest_send_job(app_id):
    def _job(config_path):
        config = _load_validated(config_path)
        if config is None:
            return False
        notify = build_notifier(config)
        if notify is None:
            print("通知先が未設定です。設定画面でWebhook URLを保存してください。")
            return False
        store = open_store(config, Path(config_path).parent)
        try:
            text = build_digest_text(
                store, app_id, store.get_app_name(app_id), config=config
            )
        finally:
            store.close()
        results = notify.send(text)
        for name, ok in results.items():
            print("{}: {}".format(name, "送信成功" if ok else "送信失敗"))
        return bool(results) and all(results.values())

    return _job


# ---------------------------------------------------------------- 画面まわり

CSS = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; background: #FAFAF7; color: #1C1C1C; font-size: 16px;
  font-family: "IBM Plex Sans JP", "Hiragino Kaku Gothic ProN", "Hiragino Sans", "Yu Gothic UI", "Yu Gothic", Meiryo, "Noto Sans JP", sans-serif;
  line-height: 1.8; line-break: strict; }
main { max-width: 880px; margin: 0 auto; padding: 48px 24px 64px; }
.brand { font-size: 12px; color: #57534B; margin: 0 0 8px; }
.brand a { color: #57534B; text-decoration: none; }
h1 { font-size: 28px; font-weight: 700; line-height: 1.4; margin: 0 0 4px; letter-spacing: .04em; text-wrap: balance; }
.sub { color: #57534B; font-size: 14px; margin: 0 0 8px; max-width: 40em; }
.nav { display: flex; gap: 24px; margin: 8px 0 32px; font-size: 14px; }
.nav a { text-decoration: none; }
.nav [aria-current="page"] { color: #1C1C1C; font-weight: 700; }
section { margin: 0 0 48px; }
h2 { font-size: 14px; letter-spacing: .04em; line-height: 1.4; color: #1C1C1C; font-weight: 700;
  border-bottom: 1px solid #8F887C; padding-bottom: 8px; margin: 0 0 16px; }
h2 .no { color: #072A7C; font-size: 12px; margin-right: 8px; }
h2 .en { color: #57534B; font-size: 12px; font-weight: 400; letter-spacing: .1em; margin-right: 8px; }
.tblabel { font-size: 12px; color: #57534B; margin: 16px 0 8px; }
.tblabel.first { margin-top: 0; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
table.fixed { table-layout: fixed; }
th, td { text-align: left; padding: 8px 8px 8px 0; vertical-align: top; }
th { color: #57534B; font-weight: 400; font-size: 12px; letter-spacing: .04em; white-space: nowrap;
  border-bottom: 1px solid #8F887C; }
td { font-size: 16px; }
.actions { display: flex; flex-wrap: wrap; gap: 8px; margin: 4px 0 8px; }
.actions form { margin: 0; display: flex; }
.actions.equal > form, .actions.equal > .btn { flex: 1 1 152px; }
.actions.equal form button { flex: 1; width: 100%; }
button, .btn { appearance: none; border: 1px solid #1C1C1C; background: #1C1C1C; color: #FAFAF7;
  font: inherit; font-size: 14px; padding: 8px 16px; border-radius: 3px; cursor: pointer;
  white-space: nowrap;
  text-decoration: none; display: inline-flex; align-items: center; justify-content: center;
  min-height: 44px; box-sizing: border-box;
  transition: background-color .15s ease, border-color .15s ease, color .15s ease, transform .05s ease; }
button:hover, .btn:hover { background: #072A7C; border-color: #072A7C; color: #FAFAF7; }
button.subtle, .btn.subtle { background: transparent; color: #1C1C1C; border-color: #8F887C; }
button.subtle:hover, .btn.subtle:hover { background: #F4F2EC; border-color: #1C1C1C; color: #1C1C1C; }
button:active, .btn:active { transform: translateY(1px); }
button:disabled { opacity: .45; cursor: default; }
button:disabled:hover { background: #1C1C1C; border-color: #1C1C1C; }
button.subtle:disabled:hover { background: transparent; border-color: #8F887C; }
@media (prefers-reduced-motion: reduce) {
  button, .btn { transition: none; }
  button:active, .btn:active { transform: none; }
}
:focus-visible { outline: 2px solid #072A7C; outline-offset: 2px; }
.note { color: #57534B; font-size: 12px; max-width: 40em; }
pre.log, pre.digest { background: #F4F2EC; border-radius: 3px;
  padding: 16px; font-size: 14px; line-height: 1.7; white-space: pre-wrap; word-break: break-word;
  font-family: "SFMono-Regular", Consolas, "Courier New", monospace; }
pre.digest a { display: inline-block; padding: 3px 0; }
.jobupd { display: inline-block; padding: 5px 8px; }
.banner { border: 1px solid #072A7C; color: #072A7C; border-radius: 3px; padding: 8px 16px; font-size: 14px; margin: 0 0 16px; }
.errors { border: 1px solid #B3402A; color: #B3402A; border-radius: 3px; padding: 8px 16px 8px 32px; font-size: 14px; margin: 0 0 16px; }
label.f { display: block; font-size: 14px; color: #57534B; letter-spacing: .04em; margin: 16px 0 8px; }
input[type=text], input[type=password], input[type=number], textarea, select { width: 100%; font: inherit; font-size: 16px;
  padding: 8px 16px; border: 1px solid #8F887C; border-radius: 3px; background: #FFFFFC; color: #1C1C1C; }
textarea { min-height: 72px; }
.check { display: flex; align-items: center; gap: 8px; font-size: 14px; margin: 16px 0 0; color: #1C1C1C; min-height: 32px; }
.check input { width: 20px; height: 20px; accent-color: #072A7C; flex: none; }
.brand a { display: inline-block; padding: 4px 0; }
.inline-num { width: 110px !important; }
.nested { margin-left: 24px; }
.sendarea { margin-top: 24px; }
footer { margin-top: 64px; color: #57534B; font-size: 12px; max-width: 40em; }
a { color: #072A7C; }
"""


def esc(value):
    return html.escape(str(value), quote=True)


_URL_RE = re.compile(r"https?://[^\s<]+")


def linkify(text):
    """表示専用：テキスト内の URL をリンクにして返す（送信本文には使わない）。

    エスケープしたうえで URL 部分だけを <a> に置き換える。URL の末尾に
    付いた句読点・括弧はリンクに含めない。
    """
    def repl(match):
        url = match.group(0)
        trail = ""
        while url and url[-1] in "。、）)":
            trail = url[-1] + trail
            url = url[:-1]
        return '<a href="{}" target="_blank" rel="noopener noreferrer">{}</a>{}'.format(
            url, url, trail
        )

    return _URL_RE.sub(repl, esc(text))


class App:
    def __init__(self, config_path, token):
        self.config_path = Path(config_path)
        self.token = token
        self.job = JobState()

    def url(self, path, **params):
        params["t"] = self.token
        return "{}?{}".format(path, urllib.parse.urlencode(params))

    # ---- 状態の取得 -------------------------------------------------
    def config(self):
        return load_config_raw(self.config_path)

    def store_if_exists(self, config):
        """状態表示用：DB ファイルがある場合だけ開く（閲覧でファイルを作らない）。"""
        analytics_cfg = (config or {}).get("analytics", {}) or {}
        if not analytics_cfg.get("enabled", True):
            return None
        db_path = self.config_path.parent / analytics_cfg.get("db_file", "reviews.db")
        if not db_path.exists():
            return None
        try:
            return open_store(config, self.config_path.parent)
        except Exception:
            return None

    # ---- 画面 -------------------------------------------------------
    def layout(self, title, body, job_watch=False):
        script = ""
        if job_watch:
            script = (
                "<script>(function(){"
                "var s=document.getElementById('job-status'),l=document.getElementById('job-log');"
                "if(!s||!l)return;"
                "function lf(t){var e=t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\"/g,'&quot;');"
                "return e.replace(/https?:\\/\\/[^\\s<]+/g,function(u){var tr='';"
                "while(u&&'。、）)'.indexOf(u.charAt(u.length-1))>=0){tr=u.charAt(u.length-1)+tr;u=u.slice(0,-1);}"
                "return '<a href=\"'+u+'\" target=\"_blank\" rel=\"noopener noreferrer\">'+u+'</a>'+tr;});}"
                "var t=setInterval(function(){"
                "fetch(\"" + self.url("/job-status") + "\").then(function(r){return r.json();}).then(function(j){"
                "l.innerHTML=lf(j.log||\"……\");"
                "if(j.running){s.textContent=\"実行中です……\";}"
                "else{s.textContent=j.ok?\"完了しました（\"+(j.finished_at||\"\")+\"）\""
                ":\"エラーで終了しました（\"+(j.finished_at||\"\")+\"）\";"
                "var b=document.querySelectorAll('button[disabled]');"
                "for(var i=0;i<b.length;i++){b[i].disabled=false;}"
                "clearInterval(t);}"
                "}).catch(function(){});"
                "},3000);"
                "})();</script>"
            )
        return (
            "<!DOCTYPE html><html lang=\"ja\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>{} — こえびん操作画面</title><style>{}</style></head>"
            "<body><main>{}</main>{}</body></html>"
        ).format(esc(title), CSS, body, script)

    def header(self, title, sub, current):
        parts = []
        if title != "こえびん操作画面":
            parts.append(
                '<p class="brand"><a href="{}">← こえびん操作画面</a></p>'.format(self.url("/"))
            )
        parts.append("<h1>{}</h1>".format(esc(title)))
        if sub:
            parts.append('<p class="sub">{}</p>'.format(esc(sub)))
        nav_items = []
        for key, label, path in (
            ("top", "トップ", "/"),
            ("digest", "週次まとめ", "/digest"),
            ("settings", "設定", "/settings"),
        ):
            if key == current:
                nav_items.append(
                    '<a href="{}" aria-current="page">{}</a>'.format(self.url(path), label)
                )
            else:
                nav_items.append('<a href="{}">{}</a>'.format(self.url(path), label))
        parts.append('<nav class="nav">{}</nav>'.format("".join(nav_items)))
        return "".join(parts)

    def footer(self):
        return (
            "<footer>この画面は、あなたのパソコンの中だけで動いています"
            "（接続先は127.0.0.1のみ・外部からは開けません）。<br>"
            "終わるときは、起動した黒い画面でCtrl+Cを押してください。</footer>"
        )

    def job_area(self, back_url):
        job = self.job
        if not job.running and not job.log:
            return ""
        if job.running:
            status = "実行中です……"
        elif job.ok:
            status = "完了しました（{}）".format(job.finished_at or "")
        else:
            status = "エラーで終了しました（{}）".format(job.finished_at or "")
        update = ""
        if job.running:
            update = ' <a class="jobupd" href="{}">最新の状態に更新</a>'.format(esc(back_url))
        return (
            '<section><h2>実行結果（{}）</h2>'
            '<p class="note"><span id="job-status" aria-live="polite">{}</span>{}</p>'
            '<pre class="log" id="job-log">{}</pre></section>'
        ).format(esc(job.kind), esc(status), update, linkify(job.log or "……"))

    def page_top(self):
        config = self.config()
        parts = [self.header("こえびん操作画面", "", "top")]
        if config is None:
            parts.append(
                '<div class="banner">設定ファイル（config.json）がまだありません。'
                '先に <a href="{}">設定画面</a> で、App IDと通知先のWebhook URLを保存してください。</div>'
                .format(self.url("/settings"))
            )
            parts.append(self.footer())
            return self.layout("こえびん", "".join(parts), job_watch=self.job.running)

        store = self.store_if_exists(config)
        try:
            rows = []
            latest_ts = None
            for app_id in config.get("app_ids", []):
                name = ""
                count = ""
                if store is not None:
                    name = store.get_app_name(app_id) or ""
                    count = store.count_reviews(app_id)
                    snap = store.latest_snapshot(app_id)
                    if snap and snap.get("ts"):
                        latest_ts = max(latest_ts or 0, snap["ts"])
                rows.append(
                    "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
                        esc(name or "（名称未取得）"), esc(app_id), esc(count)
                    )
                )
            dests = []
            for key, label in DESTINATION_LABELS:
                url = (config.get(key) or "").strip()
                if url:
                    dests.append(
                        "<tr><td>{}</td><td>{}</td></tr>".format(esc(label), esc(mask_url(url)))
                    )
            last_check = ""
            if latest_ts:
                last_check = '<p class="note">最終チェック：{}</p>'.format(
                    esc(datetime.fromtimestamp(latest_ts).strftime("%Y-%m-%d %H:%M"))
                )
            status_html = (
                '<section><h2><span class="no">01</span><span class="en">STATUS</span>状態</h2>'
                '<p class="tblabel first">監視タイトル</p>'
                '<table class="fixed"><colgroup><col style="width:40%"><col style="width:20%"><col style="width:40%"></colgroup>'
                "<tr><th>タイトル</th><th>App ID</th><th>蓄積レビュー</th></tr>{}</table>"
                '<p class="tblabel">通知先</p>'
                '<table class="fixed"><colgroup><col style="width:40%"><col style="width:60%"></colgroup>'
                "<tr><th>通知先</th><th>URL</th></tr>{}</table>"
                "{}"
                "</section>"
            ).format(
                "".join(rows) or '<tr><td colspan="3">タイトルが未登録です</td></tr>',
                "".join(dests) or '<tr><td colspan="2">未設定です</td></tr>',
                last_check,
            )
        finally:
            if store is not None:
                store.close()
        parts.append(status_html)

        disabled = " disabled" if self.job.running else ""
        parts.append(
            '<section><h2><span class="no">02</span><span class="en">ACTIONS</span>実行</h2><div class="actions equal">'
            '<form method="post" action="{}"><button type="submit"{}>今すぐチェック</button></form>'
            '<form method="post" action="{}"><button type="submit" class="subtle"{}>テスト通知を送る</button></form>'
            '<a class="btn subtle" href="{}">CSVをダウンロード</a>'
            "</div>"
            '<p class="note">チェックは、新しいレビューがあれば通知先へ送ります。</p></section>'.format(
                self.url("/action/check"), disabled,
                self.url("/action/test"), disabled,
                self.url("/download/csv"),
            )
        )
        parts.append(
            '<section><h2><span class="no">03</span><span class="en">COLLECT</span>集める（調査用）</h2>'
            '<div class="actions equal">'
            '<form method="post" action="{}"><button type="submit" class="subtle"{}>通知せずに取得する</button></form>'
            '<a class="btn subtle" href="{}">スナップショットCSVをダウンロード</a>'
            "</div>"
            '<p class="note">通知せずに取得するチェックは、通知先が設定されていても送らず、'
            "レビューを取り込んで蓄積します。以前に送れずに残っていた通知（再送待ち）がある場合は、"
            "送られないまま再送待ちから外れます。</p>"
            '<form method="post" action="{}">'
            '<label class="f" for="f-bf-sort">過去分を取り込むときの並び</label>'
            '<select name="sort" id="f-bf-sort">'
            '<option value="recent">新着順</option>'
            '<option value="helpful">参考になった順</option>'
            "</select>"
            '<label class="f" for="f-bf-max">取り込む件数の上限（タイトルごと）</label>'
            '<input name="max_reviews" id="f-bf-max" inputmode="numeric" placeholder="2000">'
            '<div class="actions" style="margin-top:16px">'
            '<button type="submit" class="subtle"{}>過去分を取り込む</button></div></form>'
            '<p class="note">過去分の取り込みは、監視を始める前のレビューをさかのぼって蓄積します。'
            "参考になった順では、期間ではなく件数の上限で止まります。</p>"
            "</section>".format(
                self.url("/action/collect-check"), disabled,
                self.url("/download/snapshots"),
                self.url("/action/backfill"), disabled,
            )
        )
        parts.append(self.job_area(self.url("/")))
        parts.append(self.footer())
        return self.layout("こえびん", "".join(parts), job_watch=self.job.running)

    def page_digest(self, app_id=None):
        config = self.config()
        parts = [self.header("週次まとめ", "直近7日を、その前の7日と比べて確認できます。", "digest")]
        if config is None:
            parts.append(
                '<div class="banner">設定ファイルがありません。先に <a href="{}">設定画面</a> で保存してください。</div>'
                .format(self.url("/settings"))
            )
            parts.append(self.footer())
            return self.layout("週次まとめ", "".join(parts), job_watch=self.job.running)
        app_ids = [str(a) for a in config.get("app_ids", [])]
        if app_id is None or str(app_id) not in app_ids:
            app_id = app_ids[0] if app_ids else None
        store = self.store_if_exists(config)
        names = {}
        if store is not None:
            for a in app_ids:
                name = store.get_app_name(int(a))
                if name:
                    names[a] = name
        options = "".join(
            '<option value="{}"{}>{}</option>'.format(
                esc(a), " selected" if a == str(app_id) else "",
                esc("{} ({})".format(names[a], a) if a in names else a),
            )
            for a in app_ids
        )
        parts.append(
            '<section>'
            '<form method="get" action="/digest"><input type="hidden" name="t" value="{}">'
            '<label class="f" for="f-digest-app">タイトル（App ID）</label>'
            '<select name="app" id="f-digest-app">{}</select> '
            '<div class="actions" style="margin-top:16px"><button type="submit" class="subtle">プレビューを表示</button></div></form>'
            .format(esc(self.token), options)
        )
        if store is None:
            parts.append('<p class="note">レビュー履歴がまだありません。先にチェックを実行してください。</p></section>')
        else:
            try:
                text = build_digest_text(
                    store, app_id, store.get_app_name(app_id), config=config
                )
            finally:
                store.close()
            disabled = " disabled" if self.job.running else ""
            dest_labels = [
                label for key, label in DESTINATION_LABELS
                if (config.get(key) or "").strip()
            ]
            send_label = "{}に送る".format("・".join(dest_labels)) if dest_labels else "この内容を通知先に送る"
            parts.append(
                '<pre class="digest">{}</pre>'
                '<div class="sendarea"><form method="post" action="{}">'
                '<input type="hidden" name="app" value="{}">'
                '<button type="submit"{}>{}</button></form></div></section>'.format(
                    linkify(text), self.url("/action/digest-send"), esc(app_id), disabled,
                    esc(send_label),
                )
            )
        parts.append(self.job_area(self.url("/digest", app=app_id)))
        parts.append(self.footer())
        return self.layout("週次まとめ", "".join(parts), job_watch=self.job.running)

    def page_settings(self, values=None, errors=None, saved=False):
        config = self.config()
        base = config if config is not None else default_config()
        v = values if values is not None else base
        parts = [self.header("設定", "通知先のURLと監視タイトルなど、基本の設定を変更できます。", "settings")]
        if saved:
            parts.append('<div class="banner">設定を保存しました。</div>')
        if errors:
            items = "".join("<li>{}</li>".format(esc(e)) for e in errors)
            parts.append('<ul class="errors">{}</ul>'.format(items))
        label_alert = v.get("label_alert") or {}
        analytics = v.get("analytics") or {}
        app_ids_text = "\n".join(str(a) for a in v.get("app_ids", []))

        def checked(flag):
            return " checked" if flag else ""

        parts.append(
            '<form method="post" action="{}">'
            '<section><h2><span class="no">01</span>監視するタイトル</h2>'
            '<label class="f" for="f-app-ids">App ID（1行に1つ）</label>'
            '<textarea name="app_ids" id="f-app-ids">{}</textarea></section>'
            '<section><h2><span class="no">02</span>通知先</h2>'
            '<label class="f" for="f-discord">Discord Webhook URL</label>'
            '<input type="password" name="discord_webhook_url" id="f-discord" value="{}" autocomplete="off">'
            '<label class="f" for="f-slack">Slack Webhook URL</label>'
            '<input type="password" name="slack_webhook_url" id="f-slack" value="{}" autocomplete="off">'
            '<label class="f" for="f-webhook">汎用 Webhook URL</label>'
            '<input type="password" name="webhook_url" id="f-webhook" value="{}" autocomplete="off"></section>'
            '<section><h2><span class="no">03</span>取得と間隔</h2>'
            '<label class="f" for="f-fetch">取得件数（1回のチェックで見る新着の数）</label>'
            '<input type="number" name="fetch_count" id="f-fetch" class="inline-num" value="{}">'
            '<label class="f" for="f-interval">チェック間隔（分）</label>'
            '<input type="number" name="interval_minutes" id="f-interval" class="inline-num" value="{}">'
            '<p class="note">チェック間隔は、繰り返し実行のときだけ使います。</p></section>'
            '<section><h2><span class="no">04</span>翻訳とアラート</h2>'
            '<label class="check"><input type="checkbox" name="translate_link"{}> 翻訳リンクを付ける</label>'
            '<label class="check"><input type="checkbox" name="translate_display"{}> 翻訳文を表示する（任意機能・別途Argos Translateが必要です）</label>'
            '<label class="check"><input type="checkbox" name="label_alert_enabled"{}> 評価ラベルの目安アラートを使う</label>'
            '<div class="nested"><label class="f" for="f-warn">目安アラート（あと何件で知らせるか）</label>'
            '<input type="number" name="label_alert_warn_within" id="f-warn" class="inline-num" value="{}"></div></section>'
            '<section><h2><span class="no">05</span>履歴と分析</h2>'
            '<label class="check"><input type="checkbox" name="analytics_enabled"{}> レビュー履歴の蓄積と分析を使う</label>'
            '<p class="note" style="margin-top:24px">保存先はこれまでと同じconfig.jsonです。急増アラートのしきい値などの高度な設定は、'
            "この画面では扱いません（config.jsonに手で書いた内容は、ここで保存しても消えません）。"
            "保存の前に、元の内容をconfig.json.bakへ控えます。</p>"
            '<div class="actions" style="margin-top:24px"><button type="submit">保存する</button></div></section>'
            "</form>".format(
                self.url("/settings/save"),
                esc(app_ids_text),
                esc(v.get("discord_webhook_url") or ""),
                esc(v.get("slack_webhook_url") or ""),
                esc(v.get("webhook_url") or ""),
                esc(v.get("fetch_count", 20)),
                esc(v.get("interval_minutes", 30)),
                checked(v.get("translate_link")),
                checked(v.get("translate_display")),
                checked(label_alert.get("enabled")),
                esc(label_alert.get("warn_within", 5)),
                checked(analytics.get("enabled", True)),
            )
        )
        parts.append(self.footer())
        return self.layout("設定", "".join(parts))


# ---------------------------------------------------------------- HTTP まわり

class Handler(BaseHTTPRequestHandler):
    server_version = "KoebinUI/1.0"
    app = None  # main() で差し込む

    def log_message(self, fmt, *args):  # コンソールを静かに保つ
        pass

    # -- 守り ------------------------------------------------------
    def _host_ok(self):
        host = (self.headers.get("Host") or "").strip()
        hostname = host.rsplit(":", 1)[0].strip("[]").lower() if host else ""
        return hostname in ("127.0.0.1", "localhost")

    def _token_ok(self, params):
        given = params.get("t") or ""
        if isinstance(given, list):
            given = given[0] if given else ""
        return secrets.compare_digest(given, self.app.token)

    def _guard(self, params):
        if not self._host_ok():
            self._send(403, "text/plain; charset=utf-8", "Forbidden")
            return False
        if not self._token_ok(params):
            self._send(403, "text/plain; charset=utf-8", "Forbidden（URLのトークンが一致しません）")
            return False
        return True

    # -- 応答 ------------------------------------------------------
    def _send(self, code, content_type, body, extra_headers=None):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _html(self, page):
        self._send(200, "text/html; charset=utf-8", page)

    def _redirect(self, location):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    # -- ルーティング ----------------------------------------------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if not self._guard(params):
            return
        app = self.app
        if parsed.path == "/":
            self._html(app.page_top())
        elif parsed.path == "/job-status":
            job = app.job
            payload = {
                "running": job.running,
                "kind": job.kind,
                "ok": job.ok,
                "finished_at": job.finished_at,
                "log": job.log,
            }
            self._send(
                200, "application/json; charset=utf-8",
                json.dumps(payload, ensure_ascii=False),
            )
        elif parsed.path == "/digest":
            self._html(app.page_digest((params.get("app") or [None])[0]))
        elif parsed.path == "/settings":
            self._html(app.page_settings())
        elif parsed.path == "/download/csv":
            self._download_csv()
        elif parsed.path == "/download/snapshots":
            self._download_snapshots()
        else:
            self._send(404, "text/plain; charset=utf-8", "Not Found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        form = {k: vals[0] for k, vals in urllib.parse.parse_qs(body).items()}
        # フォームはトークンを送信先URLのクエリに載せる（App.url）。本文だけを見ると
        # ブラウザからの正規の送信を弾いてしまうため、クエリ側も合わせて照合する。
        query = {k: vals[0] for k, vals in urllib.parse.parse_qs(parsed.query).items()}
        if not self._guard({**query, **form}):
            return
        app = self.app
        if parsed.path == "/action/check":
            app.job.start("チェック", job_check, app.config_path)
            self._redirect(app.url("/"))
        elif parsed.path == "/action/test":
            app.job.start("テスト通知", job_test, app.config_path)
            self._redirect(app.url("/"))
        elif parsed.path == "/action/collect-check":
            app.job.start("取得のみチェック", job_collect_check, app.config_path)
            self._redirect(app.url("/"))
        elif parsed.path == "/action/backfill":
            sort = form.get("sort") or "recent"
            if sort not in ("recent", "helpful"):
                sort = "recent"
            try:
                max_reviews = int(form.get("max_reviews") or "")
            except ValueError:
                max_reviews = None
            if max_reviews is not None and max_reviews <= 0:
                max_reviews = None
            app.job.start(
                "過去分の取り込み", make_backfill_job(sort, max_reviews),
                app.config_path,
            )
            self._redirect(app.url("/"))
        elif parsed.path == "/action/digest-send":
            app_id = form.get("app") or ""
            app.job.start(
                "週次まとめの送信", make_digest_send_job(app_id), app.config_path
            )
            self._redirect(app.url("/digest", app=app_id))
        elif parsed.path == "/settings/save":
            base = app.config()
            if base is None:
                base = default_config()
            config, errors = apply_settings(base, form)
            if errors:
                self._html(app.page_settings(values=_form_values(form), errors=errors))
            else:
                save_config(app.config_path, config)
                self._html(app.page_settings(saved=True))
        else:
            self._send(404, "text/plain; charset=utf-8", "Not Found")

    def _download_csv(self):
        app = self.app
        config = app.config()
        if config is None:
            self._send(400, "text/plain; charset=utf-8", "設定ファイルがありません")
            return
        try:
            validated = validate_config(copy.deepcopy(config))
        except ValueError:
            validated = config
        store = open_store(validated, app.config_path.parent)
        try:
            text, _count = export_reviews_csv_text(store)
        finally:
            store.close()
        data = b"\xef\xbb\xbf" + text.encode("utf-8")
        self._send(
            200,
            "text/csv; charset=utf-8",
            data,
            {"Content-Disposition": 'attachment; filename="koebin-reviews.csv"'},
        )


    def _download_snapshots(self):
        app = self.app
        config = app.config()
        if config is None:
            self._send(400, "text/plain; charset=utf-8", "設定ファイルがありません")
            return
        try:
            validated = validate_config(copy.deepcopy(config))
        except ValueError:
            validated = config
        store = open_store(validated, app.config_path.parent)
        tmp = None
        try:
            fd, tmp = tempfile.mkstemp(suffix=".csv")
            os.close(fd)
            export_snapshots_csv(store, tmp)
            data = Path(tmp).read_bytes()
        finally:
            store.close()
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        self._send(
            200,
            "text/csv; charset=utf-8",
            data,
            {"Content-Disposition": 'attachment; filename="koebin-snapshots.csv"'},
        )


def _form_values(form):
    """検証エラー時に、入力した値をフォームへ戻すための dict を作る。"""
    return {
        "app_ids": [
            int(tok)
            for tok in re.split(r"[\s,]+", (form.get("app_ids") or "").strip())
            if tok.isdigit()
        ],
        "discord_webhook_url": form.get("discord_webhook_url") or "",
        "slack_webhook_url": form.get("slack_webhook_url") or "",
        "webhook_url": form.get("webhook_url") or "",
        "fetch_count": form.get("fetch_count") or "",
        "interval_minutes": form.get("interval_minutes") or "",
        "translate_link": "translate_link" in form,
        "translate_display": "translate_display" in form,
        "label_alert": {
            "enabled": "label_alert_enabled" in form,
            "warn_within": form.get("label_alert_warn_within") or "",
        },
        "analytics": {"enabled": "analytics_enabled" in form},
    }


def main():
    parser = argparse.ArgumentParser(description="こえびん操作画面（ローカル専用）")
    parser.add_argument("--config", default="config.json", help="設定ファイルのパス")
    parser.add_argument("--port", type=int, default=8765, help="待ち受けポート（初期値 8765）")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    args = parser.parse_args()

    token = secrets.token_urlsafe(18)
    app = App(Path(args.config), token)
    Handler.app = app

    server = None
    port = args.port
    for candidate in range(args.port, args.port + 10):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), Handler)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        print("ポート {}〜{} がすべて使用中でした。".format(args.port, args.port + 9), file=sys.stderr)
        return 1
    server.daemon_threads = True

    url = "http://127.0.0.1:{}/?t={}".format(port, token)
    print("こえびん操作画面を起動しました。")
    print("URL: {}".format(url))
    print("この画面はこのパソコンの中だけで開けます（127.0.0.1 のみ）。終了するには Ctrl+C を押してください。")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n操作画面を終了しました。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
