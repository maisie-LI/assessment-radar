# -*- coding: utf-8 -*-
"""
重新解析已有邮件（解析规则升级后用这个重建数据）。

默认是【预览模式】：只告诉你会动哪些数据，一行都不会改。
确认要执行时加参数：python reparse.py --yes
执行前一定会先把数据库备份到 data/radar.db.bak-时间戳。

保留不动的内容：
- 手动添加的（confidence='manual'）
- 已完成的（status='done'）—— 你勾过的完成标记不会丢
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import core
import mailer


def main() -> int:
    cfg = core.load_config()
    if not core.is_configured(cfg):
        print("还没配置邮箱，先在工作台里填授权码。")
        return 1
    if not core.DB_PATH.exists():
        print("没有已有数据库，直接正常同步即可。")
        return 1

    dry_run = "--yes" not in sys.argv

    c = core.conn()
    try:
        total = c.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        keep = c.execute("SELECT COUNT(*) FROM tasks WHERE confidence='manual' OR status='done'").fetchone()[0]
        doomed = c.execute(
            "SELECT company, title, due_at, confidence FROM tasks "
            "WHERE confidence!='manual' AND status!='done' ORDER BY due_at"
        ).fetchall()
    finally:
        c.close()

    print(f"当前共 {total} 条：保留 {keep} 条（手动添加 / 已完成），"
          f"待重建 {len(doomed)} 条\n")

    if dry_run:
        print("将要重建的条目（不会删除已完成和手动添加的）：")
        for r in doomed:
            print(f"  - {r['company']} | {r['title'][:34]} | {r['due_at']} ({r['confidence']})")
        print("\n【预览模式】没有改动任何数据。")
        print("确认执行请运行：python reparse.py --yes")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = core.DB_PATH.with_name(f"radar.db.bak-{stamp}")
    shutil.copy2(core.DB_PATH, bak)
    print(f"[1/3] 已备份数据库 → {bak.name}")

    c = core.conn()
    try:
        c.execute("DELETE FROM tasks WHERE confidence!='manual' AND status!='done'")
        c.execute("DELETE FROM reminders WHERE task_id NOT IN (SELECT id FROM tasks)")
        c.execute("DELETE FROM seen_mails")
        c.commit()
        print(f"[2/3] 已清除 {len(doomed)} 条待重建记录")
    finally:
        c.close()

    print("[3/3] 重新抓取并解析邮件 …")
    results, logs = mailer.fetch_recent(cfg, progress=lambda m: print("   ", m))
    if not results:
        print("没抓到测评邮件。")
        return 0

    counts = {"added": 0, "updated": 0, "merged": 0, "skipped": 0}
    for r in results:
        action = core.ingest_mail(
            company=r["company"], title=r["title"], due=r["due"],
            confidence=r["confidence"], evidence=r["evidence"],
            vendor=r["vendor"], subject=r["subject"], sender=r["sender"],
            msgid=r["msgid"], mail_dt=r["mail_dt"],
            fallback_hours=int(cfg.get("fallback_hours", 48)),
            events=r.get("events"),
        )
        counts[action] = counts.get(action, 0) + 1

    print(f"\n完成：新增 {counts['added']}，延期更新 {counts['updated']}，"
          f"合并 {counts['merged']}，跳过 {counts['skipped']}")
    print(f"备份文件：{bak}（不满意就把它改名为 radar.db 还原）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
