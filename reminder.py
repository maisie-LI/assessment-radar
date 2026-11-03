# -*- coding: utf-8 -*-
"""置顶弹窗提醒 + 桌面悬浮倒计时条（tkinter，零依赖）。"""
from __future__ import annotations

import tkinter as tk
from datetime import datetime
from typing import Callable

try:
    import winsound
except Exception:
    winsound = None

# 配色：深色卡片，在浅色桌面上足够跳出来
BG = "#161a24"
CARD = "#212734"
CARD_HL = "#2b3346"
TEXT = "#eef2fa"
MUTED = "#8a94a8"
LINE = "#333c50"
RED = "#ff5f5f"
ORANGE = "#ffa63d"
GREEN = "#3ddc84"
BLUE = "#5b9cff"

FONT = "Microsoft YaHei UI"


def enable_dpi_awareness() -> None:
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def fmt_remaining(due: datetime, now: datetime | None = None) -> tuple[str, str]:
    """倒计时口径统一放在 core，弹窗和微信推送共用。"""
    import core
    return core.fmt_remaining(due, now)


class AlertCenter:
    """管理 tkinter 主循环、提醒弹窗、悬浮条。"""

    def __init__(self, root: tk.Tk, on_open_dashboard: Callable[[], None],
                 on_check_now: Callable[[], None], on_quit: Callable[[], None]):
        self.root = root
        self.on_open_dashboard = on_open_dashboard
        self.on_check_now = on_check_now
        self.on_quit = on_quit
        self.popup: tk.Toplevel | None = None
        self.bar: tk.Toplevel | None = None
        self._bar_labels: dict[str, tk.Widget] = {}
        self._next_task: dict | None = None
        self._drag = (0, 0)
        self._tick_job = None

    # ------------------------------------------------ 线程安全
    def safe(self, fn: Callable[[], None]) -> None:
        try:
            self.root.after(0, fn)
        except Exception:
            pass

    # ------------------------------------------------ 弹窗
    def show_alert(self, tasks: list[dict]) -> None:
        if not tasks:
            return
        if self.popup is not None and self.popup.winfo_exists():
            self.popup.destroy()

        if winsound:
            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass

        win = tk.Toplevel(self.root)
        self.popup = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=LINE)

        items = tasks[:4]
        more = len(tasks) - len(items)
        W = 580

        outer = tk.Frame(win, bg=BG)
        outer.pack(fill="both", expand=True, padx=2, pady=2)

        head = tk.Frame(outer, bg=BG)
        head.pack(fill="x", padx=18, pady=(14, 6))
        tk.Label(head, text="⏰ 测评截止提醒", bg=BG, fg=TEXT,
                 font=(FONT, 13, "bold")).pack(side="left")
        close = tk.Label(head, text="✕", bg=BG, fg=MUTED, font=(FONT, 11), cursor="hand2")
        close.pack(side="right")
        close.bind("<Button-1>", lambda e: win.destroy())
        win.bind("<Escape>", lambda e: win.destroy())

        for t in items:
            self._alert_card(outer, t)

        if more:
            tk.Label(outer, text=f"还有 {more} 项即将到期，打开工作台查看全部",
                     bg=BG, fg=MUTED, font=(FONT, 9)).pack(pady=(2, 0))

        btns = tk.Frame(outer, bg=BG)
        btns.pack(fill="x", padx=18, pady=(10, 14))
        self._btn(btns, "去工作台", self.on_open_dashboard, BLUE, True).pack(side="left")
        self._btn(btns, "打开 QQ 邮箱", self._open_mail, MUTED, False).pack(side="left", padx=8)
        self._btn(btns, "知道了", win.destroy, MUTED, False).pack(side="right")

        # 按内容真实高度定型，避免高 DPI 下字体放大把内容挤出窗口
        win.update_idletasks()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        H = min(outer.winfo_reqheight() + 4, sh - 100)
        y = max(40, sh - H - 150)
        win.geometry(f"{W}x{H}+{sw - W - 28}+{y}")

        # 轻微呼吸效果把注意力拉过来
        try:
            win.bell()
        except Exception:
            pass

    def _alert_card(self, parent: tk.Widget, t: dict) -> None:
        due = datetime.strptime(t["due_at"], "%Y-%m-%d %H:%M")
        txt, color = fmt_remaining(due)
        card = tk.Frame(parent, bg=CARD)
        card.pack(fill="x", padx=14, pady=3)

        # 先占位右侧，再让左侧抢剩余空间，否则右侧会被挤出窗口
        right = tk.Frame(card, bg=CARD)
        right.pack(side="right", padx=(6, 14), pady=9)
        tk.Label(right, text=txt, bg=CARD, fg=color,
                 font=(FONT, 11, "bold")).pack(anchor="e")

        left = tk.Frame(card, bg=CARD)
        left.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=9)
        tk.Label(left, text=t["company"][:14], bg=CARD, fg=TEXT,
                 font=(FONT, 12, "bold"), anchor="w").pack(fill="x")
        sub = t.get("title") or ""
        tk.Label(left, text=sub, bg=CARD, fg=MUTED, font=(FONT, 9),
                 anchor="w", justify="left", wraplength=330).pack(fill="x", pady=(1, 0))
        tk.Label(left, text=f"截止 {due.strftime('%m月%d日 %H:%M')}", bg=CARD,
                 fg=MUTED, font=(FONT, 8), anchor="w").pack(fill="x")

        def mark_done(_e, tid=t["id"], c=card):
            import core
            core.update_task(tid, status="done")
            for w in c.winfo_children():
                w.destroy()
            tk.Label(c, text="✓ 已标记完成", bg=CARD, fg=GREEN,
                     font=(FONT, 10)).pack(pady=16)

        done = tk.Label(right, text="标记完成", bg=CARD, fg=MUTED,
                        font=(FONT, 8), cursor="hand2")
        done.pack(anchor="e", pady=(4, 0))
        done.bind("<Button-1>", mark_done)

    def _btn(self, parent, text, cmd, color, fill):
        if fill:
            b = tk.Label(parent, text=text, bg=color, fg="#0d1117",
                         font=(FONT, 9, "bold"), padx=14, pady=6, cursor="hand2")
        else:
            b = tk.Label(parent, text=text, bg=CARD, fg=color,
                         font=(FONT, 9), padx=12, pady=6, cursor="hand2")
        b.bind("<Button-1>", lambda e: cmd())
        b.bind("<Enter>", lambda e: b.configure(bg=CARD_HL if not fill else color))
        b.bind("<Leave>", lambda e: b.configure(bg=CARD if not fill else color))
        return b

    def _open_mail(self):
        import webbrowser
        webbrowser.open("https://mail.qq.com/")

    # ------------------------------------------------ 悬浮条
    def build_bar(self) -> None:
        bar = tk.Toplevel(self.root)
        self.bar = bar
        bar.overrideredirect(True)
        bar.attributes("-topmost", True)
        bar.configure(bg=LINE)

        inner = tk.Frame(bar, bg=BG)
        inner.pack(fill="both", expand=True, padx=2, pady=2)

        self._bar_dot = tk.Label(inner, text="●", bg=BG, fg=GREEN, font=(FONT, 9))
        self._bar_dot.pack(side="left", padx=(10, 4), pady=6)
        self._bar_company = tk.Label(inner, text="测评雷达", bg=BG, fg=TEXT,
                                     font=(FONT, 9, "bold"))
        self._bar_company.pack(side="left", pady=6)
        self._bar_time = tk.Label(inner, text="待同步", bg=BG, fg=MUTED, font=(FONT, 9))
        self._bar_time.pack(side="left", padx=(8, 12), pady=6)

        sw, sh = bar.winfo_screenwidth(), bar.winfo_screenheight()
        bar.geometry(f"+{sw - 300}+{sh - 130}")

        for w in (inner, self._bar_dot, self._bar_company, self._bar_time):
            w.bind("<Button-1>", self._start_drag)
            w.bind("<B1-Motion>", self._do_drag)
            w.bind("<Double-Button-1>", lambda e: self.on_open_dashboard())
            w.bind("<Button-3>", self._bar_menu)

        self._bar_menu_ref = tk.Menu(bar, tearoff=0)
        self._bar_menu_ref.add_command(label="打开工作台", command=self.on_open_dashboard)
        self._bar_menu_ref.add_command(label="立即检查邮件", command=self.on_check_now)
        self._bar_menu_ref.add_separator()
        self._bar_menu_ref.add_command(label="退出", command=self.on_quit)

        self._tick()

    def _bar_menu(self, event):
        try:
            self._bar_menu_ref.tk_popup(event.x_root, event.y_root)
        finally:
            self._bar_menu_ref.grab_release()

    def _start_drag(self, e):
        self._drag = (e.x_root - self.bar.winfo_x(), e.y_root - self.bar.winfo_y())

    def _do_drag(self, e):
        x = e.x_root - self._drag[0]
        y = e.y_root - self._drag[1]
        self.bar.geometry(f"+{x}+{y}")

    def set_bar_task(self, task: dict | None) -> None:
        self._next_task = task
        self._render_bar()

    def _render_bar(self) -> None:
        if self.bar is None or not self.bar.winfo_exists():
            return
        t = self._next_task
        if not t:
            self._bar_dot.configure(fg=GREEN)
            self._bar_company.configure(text="测评雷达")
            self._bar_time.configure(text="暂无待办", fg=MUTED)
            return
        due = datetime.strptime(t["due_at"], "%Y-%m-%d %H:%M")
        txt, color = fmt_remaining(due)
        name = t["company"][:10]
        self._bar_dot.configure(fg=color)
        self._bar_company.configure(text=name)
        self._bar_time.configure(text=txt, fg=color)

    def _tick(self) -> None:
        self._render_bar()
        try:
            self._tick_job = self.root.after(20000, self._tick)
        except Exception:
            pass

    def quit(self) -> None:
        try:
            if self._tick_job:
                self.root.after_cancel(self._tick_job)
        except Exception:
            pass
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass
