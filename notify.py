# -*- coding: utf-8 -*-
"""
微信推送通道（可选，默认关闭）。

个人微信没有官方发消息接口，这里提供两条都是官方支持的路径：

  wecom      企业微信群机器人 + 微信插件  （推荐：官方、免费、不限条数，消息直达个人微信）
  pushplus   https://www.pushplus.plus    （备选：第三方服务号，2 分钟配好）
  serverchan https://sct.ftqq.com         （本机证书校验异常，已停用，代码保留）

注意：不使用 itchat / wechaty 等逆向个人号方案，那类做法违反微信协议、有封号风险。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import core

WECOM_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"
PUSHPLUS_URL = "https://www.pushplus.plus/send"
SERVERCHAN_URL = "https://sctapi.ftqq.com/{key}.send"
UA = "AssessmentRadar/1.0"


def _wecom_url(token: str) -> str:
    """既支持粘完整 Webhook 地址，也支持只粘 key。"""
    t = (token or "").strip()
    if t.startswith("http://") or t.startswith("https://"):
        return t
    return f"{WECOM_URL}?key={t}"


def _title_for(tasks: list[dict]) -> str:
    if len(tasks) == 1:
        t = tasks[0]
        due = datetime.strptime(t["due_at"], "%Y-%m-%d %H:%M")
        txt, _ = core.fmt_remaining(due)
        s = f"⏰ {t['company']}测评 · {txt}"
    else:
        s = f"⏰ {len(tasks)} 个测评即将截止"
    # Server酱 标题上限 32 字符，留余量
    return s[:30]


def _html_body(tasks: list[dict]) -> str:
    rows = []
    for t in tasks:
        due = datetime.strptime(t["due_at"], "%Y-%m-%d %H:%M")
        txt, color = core.fmt_remaining(due)
        # pushplus 的 html 模板对复杂样式支持有限，只用安全的行内样式
        rows.append(
            f'<tr>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #eee;">'
            f'<b>{_esc(t["company"])}</b><br>'
            f'<span style="color:#888;font-size:12px;">{_esc((t.get("title") or "")[:40])}</span>'
            f'</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #eee;white-space:nowrap;'
            f'color:#888;font-size:12px;">{due.strftime("%m月%d日 %H:%M")}</td>'
            f'<td style="padding:8px 10px;border-bottom:1px solid #eee;white-space:nowrap;'
            f'color:{color};font-weight:bold;">{txt}</td>'
            f'</tr>'
        )
    more = ""
    if len(tasks) > 4:
        more = f'<p style="color:#888;font-size:12px;">还有 {len(tasks)-4} 项，打开电脑上的工作台查看</p>'
    return (
        '<div style="font-family:-apple-system,BlinkMacSystemFont,\'Helvetica Neue\',sans-serif;">'
        '<p style="margin:0 0 10px;">以下测评即将到期，别忘了完成：</p>'
        '<table style="border-collapse:collapse;width:100%;font-size:14px;">'
        '<tr style="color:#888;font-size:12px;text-align:left;">'
        '<th style="padding:6px 10px;border-bottom:1px solid #ddd;">测评</th>'
        '<th style="padding:6px 10px;border-bottom:1px solid #ddd;">截止</th>'
        '<th style="padding:6px 10px;border-bottom:1px solid #ddd;">剩余</th>'
        '</tr>' + "".join(rows) + '</table>' + more +
        '<p style="color:#aaa;font-size:11px;margin-top:14px;">来自本机「测评雷达」，可随时在悬浮条右键退出</p>'
        '</div>'
    )


def _md_body(tasks: list[dict]) -> str:
    lines = ["以下测评即将到期，别忘了完成：", ""]
    for t in tasks:
        due = datetime.strptime(t["due_at"], "%Y-%m-%d %H:%M")
        txt, _ = core.fmt_remaining(due)
        lines.append(f"**{t['company']}** — {due.strftime('%m月%d日 %H:%M')}（{txt}）")
        if t.get("title"):
            lines.append(f"　{t['title'][:40]}")
        lines.append("")
    if len(tasks) > 4:
        lines.append(f"> 还有 {len(tasks)-4} 项，打开电脑上的工作台查看")
    return "\n\n".join(lines)


def _wecom_md(tasks: list[dict]) -> str:
    """
    企业微信 markdown 子集：支持 **加粗**、> 引用、<font color="info|comment|warning">，
    不支持表格，所以用列表排版。
    """
    head = (f"**⏰ {len(tasks)} 个测评即将截止**" if len(tasks) > 1
            else "**⏰ 测评截止提醒**")
    blocks = [head]
    for t in tasks:
        due = datetime.strptime(t["due_at"], "%Y-%m-%d %H:%M")
        txt, color = core.fmt_remaining(due)
        c = "warning" if color in (core.RED_HEX, core.ORANGE_HEX) else "info"
        line = [f"**{t['company']}** · <font color=\"{c}\">{txt}</font>"]
        if t.get("title"):
            line.append(f"> {t['title'][:40]}")
        line.append(f"> 截止 {due.strftime('%m月%d日 %H:%M')}")
        blocks.append("\n".join(line))
    if len(tasks) > 4:
        blocks.append(f"<font color=\"comment\">还有 {len(tasks)-4} 项，打开工作台查看</font>")
    return "\n\n".join(blocks)


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _post_json(url: str, payload: dict, timeout: int = 10) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _post_form(url: str, payload: dict, timeout: int = 10) -> dict:
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                 "User-Agent": UA},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def is_enabled(cfg: dict) -> bool:
    w = cfg.get("wechat") or {}
    return bool(w.get("enabled") and w.get("token"))


def send(cfg: dict, tasks: list[dict]) -> tuple[bool, str]:
    """推送一组到期任务。返回 (是否成功, 说明)"""
    w = cfg.get("wechat") or {}
    if not is_enabled(cfg):
        return False, "微信推送未启用"
    token = str(w.get("token", "")).strip()
    channel = (w.get("channel") or "pushplus").lower()
    title = _title_for(tasks)

    try:
        if channel == "wecom":
            resp = _post_json(_wecom_url(token), {
                "msgtype": "markdown",
                "markdown": {"content": _wecom_md(tasks)},
            })
            if resp.get("errcode") == 0:
                return True, "已推送到企业微信"
            err = resp.get("errmsg", "")
            hint = ""
            if "invalid webhook url" in err or "key" in err.lower():
                hint = "（Webhook 地址不对，或机器人已被移出群）"
            elif "freq" in err.lower():
                hint = "（触发 20 条/分钟限频，稍后会自动恢复）"
            return False, f"企业微信返回：{err or resp}{hint}"
        if channel == "pushplus":
            resp = _post_json(PUSHPLUS_URL, {
                "token": token,
                "title": title,
                "content": _html_body(tasks),
                "template": "html",
                "channel": "wechat",
            })
            code = resp.get("code")
            if code == 200:
                return True, "已推送到微信"
            return False, f"PushPlus 返回：{resp.get('msg') or resp}"
        elif channel == "serverchan":
            resp = _post_form(SERVERCHAN_URL.format(key=token), {
                "title": title,
                "desp": _md_body(tasks),
            })
            if resp.get("code") == 0:
                return True, "已推送到微信"
            return False, f"Server酱返回：{resp.get('message') or resp}"
        else:
            return False, f"未知通道：{channel}"
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return False, f"HTTP {e.code}：{body or e.reason}"
    except urllib.error.URLError as e:
        reason = str(e.reason)
        up = reason.upper()
        if "CERTIFICATE" in up or "SSL" in up:
            return False, ("SSL 证书校验失败，该通道当前不可用（建议改用 PushPlus）："
                           + reason[:90])
        return False, f"网络不通：{reason}"
    except Exception as e:
        return False, f"推送失败：{e}"


def test(cfg: dict) -> tuple[bool, str]:
    """发一条测试消息，验证 token 和网络是否正常。"""
    now = datetime.now()
    fake = [{
        "id": -1,
        "company": "测评雷达",
        "title": "这是一条测试消息，收到说明推送通了",
        "due_at": now.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M"),
        "status": "pending",
    }]
    old = cfg.get("wechat", {}).get("enabled")
    cfg.setdefault("wechat", {})["enabled"] = True
    try:
        return send(cfg, fake)
    finally:
        if old is not None:
            cfg["wechat"]["enabled"] = old
