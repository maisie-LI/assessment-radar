# -*- coding: utf-8 -*-
"""
测评雷达 —— 自动从 QQ 邮箱抓测评通知、提取截止时间、置顶弹窗提醒。

启动：python app.py
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# 先把输出兜住：pythonw.exe 启动时 stdout 是 None，任何 print 都会崩
_LOG_FH = None
try:
    (BASE / "data").mkdir(parents=True, exist_ok=True)
    _LOG_FH = open(BASE / "data" / "app.log", "a", encoding="utf-8", buffering=1)
except Exception:
    _LOG_FH = None

if sys.stdout is None or sys.stderr is None:
    if _LOG_FH is not None:
        sys.stdout = _LOG_FH
        sys.stderr = _LOG_FH
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import core
import mailer
import notify
import reminder
from parser import html_to_text  # noqa: F401
from parser import RULE_VERSION

STATE = {
    "syncing": False,
    "logs": [],
    "last_sync": None,
    "last_result": "",
}
# 开机自启时用 --silent：只起来跑，不弹浏览器
SILENT = "--silent" in sys.argv
LOCK = threading.Lock()
ALERT: reminder.AlertCenter | None = None


def log(msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    # 控制台模式下也同步落盘，出问题好回溯
    try:
        if _LOG_FH is not None and sys.stdout is not _LOG_FH:
            _LOG_FH.write(line + "\n")
    except Exception:
        pass
    with LOCK:
        STATE["logs"].append(line)
        STATE["logs"] = STATE["logs"][-120:]


# ---------------------------------------------------------------- 同步

def _exam_task_title(exam: dict) -> str:
    """单列的考试提醒标题：「正式笔试时间：11-01 14:00 → 16:00」"""
    name = exam.get("name") or "考试"
    st = exam.get("start") or ""
    en = exam.get("end") or exam.get("time") or ""
    if st and en and st[:10] != en[:10]:
        span = f"{st[5:16]} → {en[5:16]}"          # 跨天：10-31 09:00 → 11-01 18:00
    elif st and en:
        span = f"{st[5:16]} → {en[11:16]}"         # 同一天：11-01 14:00 → 16:00
    else:
        span = (en or st)[5:16]
    return f"正式{name}时间：{span}"


def run_sync(manual: bool = False, force: bool = False) -> None:
    with LOCK:
        if STATE["syncing"]:
            return
        STATE["syncing"] = True
        STATE["logs"] = []
    try:
        cfg = core.load_config()
        if not core.is_configured(cfg):
            log("尚未配置邮箱，先在工作台里填邮箱地址和授权码")
            return
        if force:
            bk = core.backup_db("规则重算前")
            log(f"规则升级重算模式：先备份数据库 → {bk or '备份失败（已跳过备份）'}")
        log("开始连接 QQ 邮箱…")
        results, _ = mailer.fetch_recent(cfg, progress=log)
        if not results:
            log("这轮没有发现新的测评邮件")
            STATE["last_result"] = "0"
            return
        counts = {"added": 0, "updated": 0, "merged": 0, "skipped": 0,
                  "archived": 0, "expired": 0}
        exam_added = 0
        for r in results:
            action = core.ingest_mail(
                company=r["company"], title=r["title"], due=r["due"],
                confidence=r["confidence"], evidence=r["evidence"],
                vendor=r["vendor"], subject=r["subject"], sender=r["sender"],
                msgid=r["msgid"], mail_dt=r["mail_dt"],
                fallback_hours=int(cfg.get("fallback_hours", 48)),
                events=r.get("events"), force=force,
            )
            counts[action] = counts.get(action, 0) + 1
            if action in ("added", "updated"):
                due_s = r["due"].strftime("%Y-%m-%d %H:%M") if r["due"] else "推算"
                log(f"  + {r['company']} · {r['title'][:22]} → {due_s} ({r['confidence']})")

            # 时间分层：一封邮件里既有前置/回执截止、又有正式考试时间时，
            # 除主任务外再单列一条「正式笔试/面试时间」提醒（用户 2026-10-31 要求）
            evs = r.get("events") or []
            exam = next((e for e in evs if e.get("kind") == "exam"), None)
            has_front = any(e.get("kind") in ("pretest", "sub", "receipt", "deadline")
                            for e in evs)
            if exam and has_front:
                st = exam.get("start") or exam.get("time") or ""
                if st and core.ensure_exam_task(
                    r["company"], _exam_task_title(exam), st,
                    f"原邮件：{r['subject'][:60]}",
                ) in ("added", "fixed"):
                    exam_added += 1
                    log(f"  ↳ 单列考试提醒：{r['company']} · {_exam_task_title(exam)}")

        log(f"入库完成：新增 {counts['added']}，延期更新 {counts['updated']}，"
            f"重复跳过 {counts['skipped']}"
            + (f"，已存在 {counts['merged']}" if counts["merged"] else "")
            + (f"，过期历史跳过 {counts['expired']}" if counts["expired"] else "")
            + (f"，单列考试提醒 {exam_added}" if exam_added else ""))
        # 记下本次生效的规则版本：下次规则再变时才重算，平时只做增量
        cfg["rule_version"] = RULE_VERSION
        core.save_config(cfg)
        STATE["last_result"] = f"+{counts['added']}"
    except mailer.MailError as e:
        log(f"❌ {e}")
        STATE["last_result"] = "error"
    except Exception as e:
        log(f"❌ 同步出错：{e}")
        log(traceback.format_exc()[-800:])
        STATE["last_result"] = "error"
    finally:
        STATE["last_sync"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOCK:
            STATE["syncing"] = False
        refresh_bar()


def check_reminders() -> None:
    cfg = core.load_config()
    tiers = sorted([int(t) for t in cfg.get("remind_tiers_hours", [72, 24, 6, 2])])
    if not tiers:
        return

    popup_on = bool(cfg.get("popup_enabled", True)) and ALERT is not None
    wechat_on = notify.is_enabled(cfg)
    if not popup_on and not wechat_on:
        return  # 两个通道都关着，不要白标记提醒状态

    now = datetime.now()
    qh = cfg.get("quiet_hours") or [0, 7]
    if isinstance(qh, list) and len(qh) == 2 and qh[0] <= now.hour < qh[1]:
        return  # 免打扰时段不弹窗也不推送，也不标记，等白天补

    due_tasks = core.due_soon(hours=max(tiers))
    fresh = []
    for t in due_tasks:
        due = datetime.strptime(t["due_at"], "%Y-%m-%d %H:%M")
        hours_left = (due - now).total_seconds() / 3600
        hit = next((tier for tier in tiers if hours_left <= tier), None)
        if hit is None:
            continue
        if core.mark_reminded(t["id"], str(hit), t["due_at"]):
            fresh.append(t)

    if not fresh:
        return

    if popup_on:
        ALERT.safe(lambda: ALERT.show_alert(fresh))

    if wechat_on:
        batch = fresh[:4]

        def _push():
            ok, msg = notify.send(cfg, batch)
            log(("✅ 微信推送：" if ok else "⚠️ 微信推送失败：") + msg)

        threading.Thread(target=_push, daemon=True).start()


def refresh_bar() -> None:
    if not ALERT:
        return
    tasks = core.due_soon(hours=24 * 365, include_overdue=True)
    tasks = [t for t in tasks if t["status"] == "pending"]
    nxt = tasks[0] if tasks else None
    ALERT.safe(lambda: ALERT.set_bar_task(nxt))


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "AssessmentRadar/1.0"

    def log_message(self, fmt, *args):
        pass  # 静音，避免刷控制台

    # -- helpers
    def _send(self, code: int, body: bytes, ctype: str):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -- routes
    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path in ("/", "/index.html"):
                html = (BASE / "ui" / "index.html").read_bytes()
                self._send(200, html, "text/html; charset=utf-8")
                return
            if path == "/api/ping":
                self._json({"app": "assessment-radar", "ok": True,
                            "syncing": STATE["syncing"]})
                return
            if path == "/api/tasks":
                cfg = core.load_config()
                self._json({
                    "tasks": core.list_tasks(),
                    "stats": core.stats(),
                    "configured": core.is_configured(cfg),
                    "email": cfg.get("email", ""),
                    "poll_minutes": cfg.get("poll_minutes", 20),
                    "tiers": cfg.get("remind_tiers_hours", []),
                    "syncing": STATE["syncing"],
                    "last_sync": STATE["last_sync"],
                    "wechat_on": notify.is_enabled(cfg),
                    "popup_on": bool(cfg.get("popup_enabled", True)),
                })
                return
            if path == "/api/sync/status":
                with LOCK:
                    self._json({
                        "syncing": STATE["syncing"],
                        "logs": list(STATE["logs"]),
                        "last_sync": STATE["last_sync"],
                        "last_result": STATE["last_result"],
                    })
                return
            if path == "/api/config":
                cfg = core.load_config()
                safe = {k: v for k, v in cfg.items() if k != "auth_code"}
                safe["has_auth_code"] = bool(cfg.get("auth_code"))
                w = dict(cfg.get("wechat") or {})
                w["has_token"] = bool(w.get("token"))
                w["token"] = ""  # 不回传令牌
                safe["wechat"] = w
                self._json(safe)
                return
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self._read_json()

            if path == "/api/sync":
                threading.Thread(target=run_sync, args=(True,), daemon=True).start()
                self._json({"ok": True})
                return

            if path == "/api/config":
                cfg = core.load_config()
                for k in ("email", "auth_code", "imap_host", "imap_port",
                          "lookback_days", "poll_minutes", "web_port",
                          "show_floating_bar", "scan_subfolders", "fallback_hours"):
                    if k in body and body[k] not in (None, ""):
                        cfg[k] = body[k]
                if "remind_tiers_hours" in body and isinstance(body["remind_tiers_hours"], list):
                    cfg["remind_tiers_hours"] = [int(x) for x in body["remind_tiers_hours"]]
                if "popup_enabled" in body:
                    cfg["popup_enabled"] = bool(body["popup_enabled"])
                if isinstance(body.get("wechat"), dict):
                    w = cfg.setdefault("wechat", {"enabled": False, "channel": "pushplus", "token": ""})
                    for k in ("enabled", "channel"):
                        if k in body["wechat"]:
                            w[k] = body["wechat"][k]
                    tk_ = str(body["wechat"].get("token") or "").strip()
                    if tk_:
                        w["token"] = tk_
                core.save_config(cfg)
                self._json({"ok": True})
                return

            if path == "/api/wechat/test":
                cfg = core.load_config()
                w = dict(cfg.get("wechat") or {})
                incoming = body.get("wechat") or {}
                if str(incoming.get("token") or "").strip():
                    w["token"] = str(incoming["token"]).strip()
                if incoming.get("channel"):
                    w["channel"] = incoming["channel"]
                cfg["wechat"] = w
                if not w.get("token"):
                    self._json({"ok": False, "message": "还没填 token / SendKey"})
                    return
                ok, msg = notify.test(cfg)
                self._json({"ok": ok, "message": msg})
                return

            if path == "/api/config/test":
                cfg = core.load_config()
                if str(body.get("email") or "").strip():
                    cfg["email"] = str(body["email"]).strip()
                if str(body.get("auth_code") or "").strip():
                    cfg["auth_code"] = body["auth_code"]
                if not cfg.get("email") or not cfg.get("auth_code"):
                    self._json({"ok": False,
                                "message": "邮箱地址和授权码都要填（授权码不是邮箱密码）",
                                "steps": []})
                    return
                # 硬兜底：任何情况下 26 秒内一定给前端一个结果
                box: dict = {}

                def _work():
                    try:
                        box.update(mailer.diagnose(cfg, timeout=10))
                    except Exception as e:
                        box.update({"ok": False,
                                    "message": f"{type(e).__name__}: {e}",
                                    "steps": []})

                th = threading.Thread(target=_work, daemon=True)
                th.start()
                th.join(26)
                if not box:
                    self._json({"ok": False,
                                "message": "测试超时（26 秒无响应）。多半是网络到 imap.qq.com:993 "
                                           "被拦住了，或者授权码触发了服务端频率限制，过几分钟再试。",
                                "steps": []})
                    return
                self._json(box)
                return

            if path == "/api/tasks":
                if not body.get("company") or not body.get("due_at"):
                    self._json({"error": "公司和截止时间必填"}, 400)
                    return
                tid = core.add_manual(body["company"], body.get("title") or "测评",
                                      body["due_at"], body.get("note", ""))
                refresh_bar()
                self._json({"ok": True, "id": tid})
                return

            if path.startswith("/api/tasks/") and path.endswith("/remind"):
                tid = int(path.split("/")[3])
                tasks = [t for t in core.list_tasks() if t["id"] == tid]
                if tasks and ALERT:
                    ALERT.safe(lambda: ALERT.show_alert(tasks))
                self._json({"ok": True})
                return

            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_PATCH(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/tasks/"):
                tid = int(path.rsplit("/", 1)[-1])
                body = self._read_json()
                core.update_task(tid, **body)
                if body.get("due_at"):
                    core.reset_reminders(tid)
                refresh_bar()
                self._json({"ok": True})
                return
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/tasks/"):
                tid = int(path.rsplit("/", 1)[-1])
                core.delete_task(tid)
                refresh_bar()
                self._json({"ok": True})
                return
            self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)


def start_server(port: int) -> int:
    for p in range(port, port + 8):
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", p), Handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            return p
        except OSError:
            continue
    raise RuntimeError("端口被占用，找不到可用端口")


def _running_instance(port: int) -> bool:
    """检测这个端口上是不是已经有一个测评雷达在跑。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            d = json.loads(r.read().decode("utf-8"))
            return d.get("app") == "assessment-radar"
    except Exception:
        return False


# ---------------------------------------------------------------- 调度

def scheduler() -> None:
    while True:
        try:
            cfg = core.load_config()
            interval = max(5, int(cfg.get("poll_minutes", 20))) * 60
            time.sleep(interval)
            if core.is_configured(cfg):
                log("定时检查邮件…")
                run_sync()
        except Exception:
            time.sleep(600)


def reminder_loop() -> None:
    while True:
        try:
            check_reminders()
        except Exception as e:
            log(f"提醒检查异常：{e}")
        time.sleep(45)


# ---------------------------------------------------------------- main

def main() -> None:
    global ALERT
    cfg = core.load_config()
    want_port = int(cfg.get("web_port", 8765))

    # 避免开出第二个实例：用户在旧页面上操作，看起来就像"卡住了"
    if _running_instance(want_port):
        url = f"http://127.0.0.1:{want_port}/"
        log("检测到已经有测评雷达在运行，直接打开工作台（不会重复启动）")
        if not SILENT:
            webbrowser.open(url)
        time.sleep(1.5)
        return

    log(f"启动中… Python {sys.version.split()[0]} | 配置：{'已填' if core.is_configured(cfg) else '未填'}")
    port = start_server(want_port)
    url = f"http://127.0.0.1:{port}/"
    if port != want_port:
        log(f"端口 {want_port} 被别的程序占用，改用 {port}")

    # 桌面弹窗失败不应该让整个程序挂掉 —— 降级成只有网页提醒
    root = None
    try:
        reminder.enable_dpi_awareness()
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        root.title("测评雷达")

        ALERT = reminder.AlertCenter(
            root,
            on_open_dashboard=lambda: webbrowser.open(url),
            on_check_now=lambda: threading.Thread(target=run_sync, args=(True,), daemon=True).start(),
            on_quit=lambda: ALERT.quit(),
        )

        if cfg.get("show_floating_bar", True):
            ALERT.build_bar()
        refresh_bar()
    except Exception as e:
        log(f"⚠️ 桌面弹窗组件没起来（{type(e).__name__}: {e}），降级为只在网页里提醒")
        ALERT = None

    threading.Thread(target=scheduler, daemon=True).start()
    threading.Thread(target=reminder_loop, daemon=True).start()

    if cfg.get("auto_open_browser", True) and not SILENT:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    if core.is_configured(cfg):
        # 规则版本变了 → 自动用新规则重算一遍待办，用户不用再手动点"检查邮件"
        need_recompute = (cfg.get("rule_version") or "") != RULE_VERSION
        if need_recompute:
            log(f"解析规则升级（{cfg.get('rule_version') or '旧版'} → {RULE_VERSION}），"
                f"启动后自动重算待办（已完成/手动修改的条目不动）")
        threading.Timer(2.5, lambda: run_sync(force=need_recompute)).start()
    else:
        log("等待配置：请在工作台里填写 QQ 邮箱和授权码")

    log(f"已启动 → {url}（关闭请用悬浮条右键「退出」）")
    try:
        if root is not None:
            root.mainloop()
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        try:
            print(tb, flush=True)
            if _LOG_FH is not None:
                _LOG_FH.write(tb + "\n")
                _LOG_FH.flush()
        except Exception:
            pass
        raise
