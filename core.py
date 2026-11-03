# -*- coding: utf-8 -*-
"""配置读写 + SQLite 存储 + 任务入库（去重/延期合并）。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import parser as P

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
DB_PATH = BASE / "data" / "radar.db"

DEFAULT_CONFIG: dict[str, Any] = {
    "email": "",
    "auth_code": "",
    "imap_host": "imap.qq.com",
    "imap_port": 993,
    "folder": "INBOX",
    "lookback_days": 60,
    "poll_minutes": 20,
    "remind_tiers_hours": [72, 24, 6, 2],
    "fallback_hours": 48,
    "web_port": 8765,
    "show_floating_bar": True,
    "auto_open_browser": True,
    "quiet_hours": [0, 7],
    "popup_enabled": True,
    "wechat": {"enabled": False, "channel": "wecom", "token": ""},
}


def load_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded.get("wechat"), dict):
                merged = dict(DEFAULT_CONFIG["wechat"])
                merged.update(loaded["wechat"])
                loaded["wechat"] = merged
            cfg.update(loaded)
        except Exception as e:
            print(f"[warn] config.json 解析失败，使用默认配置: {e}")
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def is_configured(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("email") and cfg.get("auth_code"))


def backup_db(tag: str = "") -> str:
    """动数据前先留一份完整副本，返回备份文件路径（失败返回空串）。"""
    import shutil
    if not DB_PATH.exists():
        return ""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suf = f"-{tag}" if tag else ""
    dst = DB_PATH.with_name(f"radar.db.bak-{stamp}{suf}")
    try:
        shutil.copy2(DB_PATH, dst)
        return dst.name
    except Exception:
        return ""


# ---------------------------------------------------------------- 倒计时文案

RED_HEX = "#ff5f5f"
ORANGE_HEX = "#ffa63d"
GREEN_HEX = "#3ddc84"


def fmt_remaining(due: datetime, now: datetime | None = None) -> tuple[str, str]:
    """返回 (倒计时文本, 颜色)。弹窗和微信推送共用同一套口径。"""
    now = now or datetime.now()
    secs = (due - now).total_seconds()
    if secs < 0:
        h = int(-secs // 3600)
        d = h // 24
        txt = f"已超时 {d} 天" if d >= 1 else f"已超时 {max(h, 1)} 小时"
        return txt, RED_HEX
    if secs < 3600:
        return f"剩 {int(secs // 60)} 分钟", RED_HEX
    if secs < 86400:
        h, m = int(secs // 3600), int((secs % 3600) // 60)
        return f"剩 {h} 小时 {m} 分", (RED_HEX if secs < 6 * 3600 else ORANGE_HEX)
    d = int(secs // 86400)
    h = int((secs % 86400) // 3600)
    # 用真实秒数判断，避免「3 天 23 小时」这种临界情况被误标成紧急
    return f"剩 {d} 天 {h} 小时", (ORANGE_HEX if secs <= 3 * 86400 else GREEN_HEX)


# ---------------------------------------------------------------- 存储

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    key            TEXT UNIQUE,
    company        TEXT,
    title          TEXT,
    due_at         TEXT,
    confidence     TEXT,
    evidence       TEXT,
    vendor         TEXT,
    source_subject TEXT,
    source_from    TEXT,
    source_msgid   TEXT,
    mail_date      TEXT,
    status         TEXT DEFAULT 'pending',
    note           TEXT,
    events         TEXT DEFAULT '',
    created_at     TEXT,
    updated_at     TEXT
);
CREATE TABLE IF NOT EXISTS seen_mails (
    msgid   TEXT PRIMARY KEY,
    seen_at TEXT
);
CREATE TABLE IF NOT EXISTS reminders (
    task_id INTEGER,
    tier    TEXT,
    due_at  TEXT,
    sent_at TEXT,
    PRIMARY KEY (task_id, tier, due_at)
);
CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due_at);
"""


def conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    _migrate(c)
    return c


def _migrate(c: sqlite3.Connection) -> None:
    """老库平滑升级：补上后来新增的列。"""
    cols = {r[1] for r in c.execute("PRAGMA table_info(tasks)")}
    if "events" not in cols:
        c.execute("ALTER TABLE tasks ADD COLUMN events TEXT DEFAULT ''")
        c.commit()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def task_key(company: str, title: str) -> str:
    norm = re.sub(r"[\s\-—_·,，。:：()（）【】\[\]]+", "", f"{company}|{title}")
    return hashlib.md5(norm.lower().encode("utf-8")).hexdigest()[:16]


def ingest_mail(
    company: str,
    title: str,
    due: datetime | None,
    confidence: str,
    evidence: str,
    vendor: str,
    subject: str,
    sender: str,
    msgid: str,
    mail_dt: datetime,
    fallback_hours: int = 48,
    events: list[dict] | None = None,
    force: bool = False,
) -> str:
    """
    把一封测评邮件写进库。返回动作描述：added / updated / merged / skipped / nodue
    events: 邮件里的关键时间节点（预约截止/考试时间等），存 JSON 供工作台展示
    force: 解析规则升级后的重算模式 —— 忽略"已见过"记录，用新规则覆盖待办的时间与节点。
           已完成的任务、手动改过时间的任务一律不动。
    """
    c = conn()
    try:
        if not force and c.execute(
            "SELECT 1 FROM seen_mails WHERE msgid=?", (msgid,)
        ).fetchone():
            return "skipped"

        ev_json = json.dumps(events or [], ensure_ascii=False)

        if due is None:
            # 邮件里没写截止时间 —— 用邮件时间 + 兜底小时数，明确标记为推算
            due = (mail_dt + timedelta(hours=fallback_hours)).replace(
                minute=59, second=0, microsecond=0
            )
            confidence = "inferred"
            evidence = evidence or f"邮件未写明截止时间，按收到时间 +{fallback_hours} 小时推算"

        # 历史旧邮件：邮件发出已超过 3 天、且截止时间也早已过去 → 不入库。
        # 早期做法是"归档成已完成"，结果一次补录就往「已完成」里塞几十条历史噪音，
        # 用户看到的就是"怎么又冒出来一堆"。现在直接不建记录（只记 msgid 防重复解析）。
        # 注意：已存在于库里的任务在前面 row 分支就返回了，不受影响。
        _now_dt = datetime.now()
        if (mail_dt < _now_dt - timedelta(days=3)) and (due < _now_dt - timedelta(days=1)):
            c.execute(
                "INSERT OR IGNORE INTO seen_mails(msgid, seen_at) VALUES (?,?)",
                (msgid, _now()),
            )
            c.commit()
            return "expired"

        init_status = "pending"
        init_note = ""

        c.execute(
            "INSERT OR IGNORE INTO seen_mails(msgid, seen_at) VALUES (?,?)",
            (msgid, _now()),
        )

        key = task_key(company, title)
        due_s = due.strftime("%Y-%m-%d %H:%M")
        row = c.execute("SELECT * FROM tasks WHERE key=?", (key,)).fetchone()

        if row:
            # 规则重算模式：待办且非手动改过的条目，直接用新解析结果覆盖时间与节点。
            # 已完成的、手动的（confidence=manual）永远不动 —— 那是用户的劳动成果。
            if force and row["status"] == "pending" and row["confidence"] != "manual":
                if row["due_at"] != due_s or (
                    ev_json and ev_json != "[]" and ev_json != (row["events"] or "")
                ):
                    c.execute(
                        "UPDATE tasks SET due_at=?, confidence=?, evidence=?, events=?, "
                        "updated_at=? WHERE id=?",
                        (due_s, confidence, evidence, ev_json or (row["events"] or ""),
                         _now(), row["id"]),
                    )
                    c.commit()
                    return "updated"
                c.commit()
                return "merged"
            # 同公司同标题再来一封：视为延期通知，取更晚的截止时间
            old_due = datetime.strptime(row["due_at"], "%Y-%m-%d %H:%M")
            if row["status"] == "pending" and due > old_due:
                c.execute(
                    "UPDATE tasks SET due_at=?, confidence=?, evidence=?, note=?, "
                    "events=?, updated_at=? WHERE id=?",
                    (
                        due_s, confidence,
                        evidence,
                        f"收到延期通知，截止从 {row['due_at']} 延至 {due_s}",
                        ev_json or (row["events"] or ""),
                        _now(), row["id"],
                    ),
                )
                c.commit()
                return "updated"
            # 截止没变，但这次解析出了更多节点
            if ev_json and ev_json != "[]" and (row["events"] or "") in ("", "[]"):
                # 旧记录本来就是"没抓到就瞎猜"的时间，而这次解析出了真实节点
                # → 顺手把主截止也修正，否则会出现「截止 10-15、考试 10-17」这种自相矛盾
                if row["confidence"] == "inferred" and confidence != "inferred":
                    c.execute(
                        "UPDATE tasks SET due_at=?, confidence=?, evidence=?, events=?, "
                        "note=?, updated_at=? WHERE id=?",
                        (
                            due_s, confidence, evidence, ev_json,
                            f"解析规则升级后修正：{row['due_at']} → {due_s}",
                            _now(), row["id"],
                        ),
                    )
                    c.commit()
                    return "updated"
                c.execute("UPDATE tasks SET events=?, updated_at=? WHERE id=?",
                          (ev_json, _now(), row["id"]))
            c.commit()
            return "merged"

        # 同一公司已有一条 pending 任务、且新邮件是 30 天内的补充通知 → 合并到已有条目
        pend = c.execute(
            "SELECT * FROM tasks WHERE status='pending' ORDER BY due_at DESC"
        ).fetchall()
        cand = next((r for r in pend if _same_company(r["company"], company)), None)
        if cand:
            same_round = abs((datetime.strptime(cand["due_at"], "%Y-%m-%d %H:%M") - due).days) <= 3
            if same_round:
                # 别把新邮件里的关键节点丢掉
                if ev_json and ev_json != "[]" and (cand["events"] or "") in ("", "[]"):
                    c.execute("UPDATE tasks SET events=?, updated_at=? WHERE id=?",
                              (ev_json, _now(), cand["id"]))
                c.commit()
                return "merged"

        # 历史旧邮件保护：同一家公司已经有"已完成"记录，且这封邮件的日期
        # 早于她点完成的时间 → 说明这件事她早就处理过了。
        # 不再补录成新的待办，否则工作台会冒出"我明明做完了怎么又来了"的重复项。
        # （2026-10-31 真实翻车：识别器修好后把 5 封旧邮件补录成待办）
        for d in c.execute(
            "SELECT id, updated_at FROM tasks WHERE company=? AND status='done' "
            "ORDER BY updated_at DESC", (company,)
        ).fetchall():
            if mail_dt.strftime("%Y-%m-%d %H:%M") <= (d["updated_at"] or ""):
                c.commit()
                return "merged"

        c.execute(
            """INSERT INTO tasks
               (key, company, title, due_at, confidence, evidence, vendor,
                source_subject, source_from, source_msgid, mail_date,
                status, note, events, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key, company, title, due_s, confidence, evidence, vendor,
                subject, sender, msgid, mail_dt.strftime("%Y-%m-%d %H:%M"),
                init_status, init_note, ev_json, _now(), _now(),
            ),
        )
        c.commit()
        return "added"
    finally:
        c.close()


def list_tasks(status: str | None = None) -> list[dict]:
    c = conn()
    try:
        if status and status != "all":
            rows = c.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY due_at ASC", (status,)
            ).fetchall()
        else:
            rows = c.execute("SELECT * FROM tasks ORDER BY due_at ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        c.close()


def update_task(tid: int, **fields) -> None:
    if not fields:
        return
    allowed = {"company", "title", "due_at", "status", "note", "confidence", "evidence"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    sets.append("updated_at=?")
    vals.extend([_now(), tid])
    c = conn()
    try:
        c.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", vals)
        c.commit()
    finally:
        c.close()


def delete_task(tid: int) -> None:
    c = conn()
    try:
        c.execute("DELETE FROM tasks WHERE id=?", (tid,))
        c.commit()
    finally:
        c.close()


def add_manual(company: str, title: str, due_s: str, note: str = "") -> int:
    c = conn()
    try:
        cur = c.execute(
            """INSERT INTO tasks
               (key, company, title, due_at, confidence, evidence, vendor,
                source_subject, source_from, source_msgid, mail_date,
                status, note, created_at, updated_at)
               VALUES (?,?,?,?,'manual','手动添加','','','','','', 'pending', ?,?,?)""",
            (task_key(company, title + due_s), company, title, due_s, note, _now(), _now()),
        )
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _same_company(a: str, b: str) -> bool:
    """「吉利汽车」和「吉利汽车集团」是同一家，别建两条。"""
    a, b = (a or "").strip(), (b or "").strip()
    if not a or not b:
        return False
    return a == b or a.startswith(b) or b.startswith(a)


def ensure_exam_task(company: str, title: str, due_s: str, note: str = "") -> str:
    """
    多时间层次的邮件（既有前置/回执截止、又有正式考试时间）→ 额外单列一条
    「正式笔试/面试时间」提醒，避免只盯着主截止而错过考试本身。
    幂等：同公司同标题同时间只建一次；已经考完的不建。
    """
    try:
        due = datetime.strptime(due_s, "%Y-%m-%d %H:%M")
    except Exception:
        return "skip"
    if due < datetime.now():
        return "skip"
    key = task_key(company, title)
    c = conn()
    try:
        if c.execute("SELECT 1 FROM tasks WHERE key=?", (key,)).fetchone():
            return "exists"
        # 同一家公司只保留一条「正式xx时间」提醒，别一封邮件建一条
        dups = [dict(r) for r in c.execute(
            "SELECT id, company, due_at FROM tasks WHERE status='pending' "
            "AND title LIKE '正式%时间：%'").fetchall()]
        dup = next((r for r in dups if _same_company(r["company"], company)), None)
        if dup:
            # 旧的只有日期（23:59 是"没写时刻"的默认值）、新的有具体时刻 → 用新的修正
            if dup["due_at"] != due_s and dup["due_at"].endswith("23:59") \
                    and not due_s.endswith("23:59"):
                c.execute(
                    "UPDATE tasks SET due_at=?, title=?, updated_at=? WHERE id=?",
                    (due_s, title, _now(), dup["id"]),
                )
                c.commit()
                return "fixed"
            return "exists"
        c.execute(
            """INSERT INTO tasks
               (key, company, title, due_at, confidence, evidence, vendor,
                source_subject, source_from, source_msgid, mail_date,
                status, note, created_at, updated_at)
               VALUES (?,?,?,?,'exact','从邮件时间节点拆出','','','','','',
                       'pending', ?,?,?)""",
            (key, company, title, due_s, note, _now(), _now()),
        )
        c.commit()
        return "added"
    finally:
        c.close()


def due_soon(hours: int = 24, include_overdue: bool = True) -> list[dict]:
    now = datetime.now()
    limit = now + timedelta(hours=hours)
    c = conn()
    try:
        rows = c.execute(
            "SELECT * FROM tasks WHERE status='pending' ORDER BY due_at ASC"
        ).fetchall()
    finally:
        c.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            dt = datetime.strptime(d["due_at"], "%Y-%m-%d %H:%M")
        except Exception:
            continue
        if dt <= limit and (include_overdue or dt >= now):
            d["_due_dt"] = dt
            out.append(d)
    return out


def mark_reminded(task_id: int, tier: str, due_at: str) -> bool:
    """返回 True 表示这次是第一次提醒该档位。"""
    c = conn()
    try:
        cur = c.execute(
            "INSERT OR IGNORE INTO reminders(task_id, tier, due_at, sent_at) VALUES (?,?,?,?)",
            (task_id, tier, due_at, _now()),
        )
        c.commit()
        return cur.rowcount > 0
    finally:
        c.close()


def reset_reminders(task_id: int) -> None:
    c = conn()
    try:
        c.execute("DELETE FROM reminders WHERE task_id=?", (task_id,))
        c.commit()
    finally:
        c.close()


def stats() -> dict:
    tasks = list_tasks()
    now = datetime.now()
    pending = [t for t in tasks if t["status"] == "pending"]
    overdue = today = week = 0
    for t in pending:
        try:
            dt = datetime.strptime(t["due_at"], "%Y-%m-%d %H:%M")
        except Exception:
            continue
        delta = (dt - now).total_seconds() / 3600
        if delta < 0:
            overdue += 1
        elif delta <= 24:
            today += 1
        if 0 <= delta <= 168:
            week += 1
    return {
        "total": len(tasks),
        "pending": len(pending),
        "overdue": overdue,
        "within24h": today,
        "within7d": week,
        "done": len([t for t in tasks if t["status"] == "done"]),
    }
