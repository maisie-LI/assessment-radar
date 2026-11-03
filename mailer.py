# -*- coding: utf-8 -*-
"""QQ 邮箱 IMAP 拉取与邮件解析。"""
from __future__ import annotations

import base64
import email
import email.policy
import imaplib
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import parser as P

_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
        "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class MailError(RuntimeError):
    pass


def _imap_date(dt: datetime) -> str:
    return f"{dt.day:02d}-{_MON[dt.month - 1]}-{dt.year}"


def imap_utf7_decode(s: str) -> str:
    """modified UTF-7 解码，用于显示中文文件夹名。"""
    out, i = [], 0
    while i < len(s):
        ch = s[i]
        if ch == "&":
            j = s.find("-", i)
            if j == -1:
                out.append(ch)
                i += 1
                continue
            chunk = s[i + 1: j]
            if not chunk:
                out.append("&")
            else:
                b = chunk.replace(",", "/")
                b += "=" * (-len(b) % 4)
                try:
                    out.append(base64.b64decode(b).decode("utf-16-be"))
                except Exception:
                    out.append(chunk)
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def clean_code(raw: Any) -> str:
    """授权码常见误输入：带空格、换行、连字符。QQ 授权码是 16 位小写字母。"""
    s = re.sub(r"\s+", "", str(raw or ""))
    return s.strip("-")


def _send_client_id(M: imaplib.IMAP4_SSL) -> None:
    """
    QQ / 网易邮箱要求客户端先发 IMAP ID（RFC 2971），否则部分账号会返回
    "Unsafe Login" 或被服务端拖着不放。imaplib 没有内置这个命令，用 xatom 发。
    失败不影响主流程。
    """
    try:
        M.xatom("ID", '("name" "AssessmentRadar" "version" "1.0" '
                       '"vendor" "local-app" "support-email" "none")')
    except Exception:
        pass


def _pretty_err(e: BaseException) -> str:
    """imaplib 抛出的错误常是 bytes 的 repr，转成人话。"""
    raw = str(e)
    if raw[:2] in ("b'", 'b"'):
        try:
            import ast
            v = ast.literal_eval(raw)
            if isinstance(v, bytes):
                raw = v.decode("utf-8", "replace")
        except Exception:
            pass
    return raw.strip()


def connect(cfg: dict[str, Any], timeout: int = 12,
            trace=None) -> imaplib.IMAP4_SSL:
    email_addr = str(cfg.get("email") or "").strip()
    code = clean_code(cfg.get("auth_code"))
    if not email_addr or not code:
        raise MailError("还没填邮箱地址或授权码")

    def step(msg: str) -> None:
        if trace:
            try:
                trace(msg)
            except Exception:
                pass

    host = str(cfg.get("imap_host") or "imap.qq.com")
    port = int(cfg.get("imap_port") or 993)

    step(f"连接 {host}:{port} …")
    try:
        M = imaplib.IMAP4_SSL(host, port, timeout=timeout)
    except TimeoutError as e:
        raise MailError(
            f"连接 {host}:{port} 超时（{timeout} 秒）。常见原因：网络或代理拦了 993 端口。"
        ) from e
    except Exception as e:
        raise MailError(f"连不上 {host}:{port}：{type(e).__name__}: {e}") from e

    step("加密通道已建立，正在登录 …")
    _send_client_id(M)
    try:
        M.login(email_addr, code)
    except imaplib.IMAP4.error as e:
        try:
            M.logout()
        except Exception:
            pass
        raw = _pretty_err(e)
        up = raw.upper()
        # QQ 的报错文案本身很含糊（一句话列了四种原因），所以按错误码分，不硬猜
        if "UNSAFE LOGIN" in up:
            hint = "服务端认为这个客户端不安全（Unsafe Login），把 data/app.log 发给开发者看"
        elif "LOGIN FAIL" in up or "AUTHENTICATIONFAILED" in up:
            hint = ("① 授权码填错或已过期 —— 去 QQ 邮箱重新生成一次（旧码会失效）\n"
                    "② 邮箱设置里没开启 IMAP 服务\n"
                    "③ 刚连着点太多次，被临时限制，等 5–10 分钟再试")
        else:
            hint = "授权码或 IMAP 服务有问题"
        if len(code) != 16:
            hint += f"\n（提示：你填的授权码是 {len(code)} 位，QQ 授权码通常是 16 位）"
        raise MailError(f"登录被拒绝。{hint}\n服务器原文：{raw}") from e
    except TimeoutError as e:
        raise MailError(
            f"登录超时（{timeout} 秒无响应）。可能是网络不稳，或该邮箱账号被临时限制登录，"
            "过几分钟再试。"
        ) from e
    except Exception as e:
        raise MailError(f"登录时出错：{type(e).__name__}: {e}") from e

    step("登录成功")
    return M


def _mark_failure(steps: list[dict]) -> None:
    """失败时：除最后一步外都算成功，最后一步标失败并把文案改成过去式。"""
    if not steps:
        return
    for s in steps[:-1]:
        if s["ok"] is None:
            s["ok"] = True
            s["text"] = s["text"].replace("…", "").strip()
    last = steps[-1]
    last["ok"] = False
    last["text"] = (last["text"]
                    .replace("正在登录 …", "登录失败")
                    .replace("正在登录…", "登录失败")
                    .replace("…", "")
                    .strip())


def diagnose(cfg: dict[str, Any], timeout: int = 12) -> dict:
    """
    分步体检，返回每一步的结果，方便一眼看出卡在哪。
    """
    steps: list[dict] = []
    t0 = time.time()

    def trace(msg: str) -> None:
        steps.append({"text": msg, "ok": None,
                      "t": round(time.time() - t0, 1)})

    try:
        M = connect(cfg, timeout=timeout, trace=trace)
    except MailError as e:
        _mark_failure(steps)
        return {"ok": False, "message": str(e), "steps": steps,
                "elapsed": round(time.time() - t0, 1)}
    except Exception as e:
        _mark_failure(steps)
        return {"ok": False, "message": f"{type(e).__name__}: {e}",
                "steps": steps, "elapsed": round(time.time() - t0, 1)}

    for s in steps:
        if s["ok"] is None:
            s["ok"] = True
            s["text"] = s["text"].replace("…", "").strip()

    try:
        trace("读取收件箱 …")
        folder = cfg.get("folder", "INBOX")
        typ, data = M.select(folder, readonly=True)
        total = 0
        if typ == "OK" and data and data[0]:
            try:
                total = int(data[0])
            except Exception:
                total = 0
        steps[-1]["ok"] = typ == "OK"
        if typ != "OK":
            steps[-1]["text"] = f"读取 {folder} 失败（{typ}）"
            return {"ok": False,
                    "message": f"能登录，但读不到收件箱 {folder}：{typ}",
                    "steps": steps, "elapsed": round(time.time() - t0, 1)}
        steps[-1]["text"] = f"读取收件箱成功，共 {total} 封邮件"
        return {"ok": True,
                "message": f"连接成功，收件箱共 {total} 封邮件",
                "steps": steps, "elapsed": round(time.time() - t0, 1),
                "total": total}
    finally:
        try:
            M.logout()
        except Exception:
            pass


def test_connection(cfg: dict[str, Any]) -> tuple[bool, str]:
    r = diagnose(cfg)
    return bool(r["ok"]), r["message"]


def list_folders(cfg: dict[str, Any]) -> list[str]:
    M = connect(cfg)
    try:
        typ, data = M.list()
        names = []
        for line in data or []:
            if not line:
                continue
            s = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
            m = re.search(r'"([^"]*)"\s*$', s)
            raw = m.group(1) if m else s.split()[-1].strip('"')
            names.append(imap_utf7_decode(raw))
        return names
    finally:
        try:
            M.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- 报文解析

_HDR_FIELDS = "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE MESSAGE-ID)])"
_LOOSE_KW = ["测评", "笔试", "测试", "考试", "评估", "答题", "作答", "assessment",
             "test", "exam", "invitation", "邀请", "招聘", "校招", "校园"]


def _decode_header_val(msg: email.message.Message, key: str) -> str:
    v = msg.get(key, "")
    if v is None:
        return ""
    try:
        return str(v).replace("\n", " ").replace("\r", " ").strip()
    except Exception:
        return ""


def _mail_datetime(msg: email.message.Message) -> datetime:
    raw = msg.get("Date")
    if raw:
        try:
            dt = parsedate_to_datetime(str(raw))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            return dt.astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
        except Exception:
            pass
    return datetime.now()


def _body_text(msg: email.message.Message) -> str:
    plain, html = [], []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp.lower():
                continue
            if ctype not in ("text/plain", "text/html"):
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                text = payload.decode(charset, "replace")
            except Exception:
                continue
            (plain if ctype == "text/plain" else html).append(text)
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            text = payload.decode(charset, "replace") if payload else ""
        except Exception:
            text = ""
        if msg.get_content_type() == "text/html":
            html.append(text)
        else:
            plain.append(text)

    body = "\n".join(plain).strip()
    if len(body) < 80:  # 纯文本太少，退回 HTML 版本（很多测评邮件只有 HTML）
        body = (body + "\n" + P.html_to_text("\n".join(html))).strip()
    return body


def fetch_recent(cfg: dict[str, Any], progress=None) -> tuple[list[dict], list[str]]:
    """
    拉取近 lookback_days 天内可能是测评的邮件并解析。
    返回 (解析结果列表, 日志行)
    """
    logs: list[str] = []
    days = int(cfg.get("lookback_days", 60))
    since = _imap_date(datetime.now() - timedelta(days=days))

    def log(msg: str):
        logs.append(msg)
        if progress:
            try:
                progress(msg)
            except Exception:
                pass

    M = connect(cfg)
    results: list[dict] = []
    try:
        folders = [cfg.get("folder", "INBOX")]
        if cfg.get("scan_subfolders"):
            try:
                typ, data = M.list()
                extra = []
                for line in data or []:
                    if not line:
                        continue
                    s = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
                    m = re.search(r'"([^"]*)"\s*$', s)
                    if m and m.group(1) not in folders:
                        extra.append(m.group(1))
                folders += extra[:6]
            except Exception:
                pass

        for folder in folders:
            try:
                typ, data = M.select(folder, readonly=True)
                if typ != "OK":
                    continue
            except Exception:
                continue
            typ, data = M.uid("search", None, "SINCE", since)
            if typ != "OK" or not data or not data[0]:
                log(f"[{imap_utf7_decode(folder)}] 没有近期邮件")
                continue
            uids = data[0].split()
            log(f"[{imap_utf7_decode(folder)}] 近 {days} 天 {len(uids)} 封，开始筛查")
            if not uids:
                continue

            # 第一步：只抓邮件头，便宜地筛出候选
            candidates = []
            for i in range(0, len(uids), 80):
                chunk = b",".join(uids[i: i + 80]).decode()
                typ, hdr_data = M.uid("fetch", chunk, _HDR_FIELDS)
                if typ != "OK":
                    continue
                for item in hdr_data or []:
                    if not isinstance(item, tuple):
                        continue
                    head = item[1]
                    uid_m = re.search(rb"UID (\d+)", item[0] or b"")
                    hmsg = email.message_from_bytes(head, policy=email.policy.default)
                    subj = _decode_header_val(hmsg, "Subject")
                    sender = _decode_header_val(hmsg, "From")
                    low = (subj + " " + sender).lower()
                    if any(k in low for k in _LOOSE_KW):
                        candidates.append((uid_m.group(1).decode() if uid_m else "", subj, sender))

            log(f"  主题命中测评/招聘关键词 {len(candidates)} 封，抓取正文")

            for uid, subj, sender in candidates:
                try:
                    typ, body_data = M.uid("fetch", uid, "(RFC822)")
                    if typ != "OK" or not body_data:
                        continue
                    raw = next((x[1] for x in body_data if isinstance(x, tuple)), None)
                    if not raw:
                        continue
                    msg = email.message_from_bytes(raw, policy=email.policy.default)
                except Exception as e:
                    log(f"  跳过一封（读取失败：{e}）")
                    continue

                subj = _decode_header_val(msg, "Subject") or subj
                sender = _decode_header_val(msg, "From") or sender
                body = _body_text(msg)
                mail_dt = _mail_datetime(msg)
                msgid = _decode_header_val(msg, "Message-ID") or f"{uid}@{folder}"

                ok, score = P.is_assessment_mail(subj, body, sender)
                if not ok:
                    continue

                company = P.extract_company(subj, body, sender)
                title = P.extract_title(subj, company)
                due, conf, evidence, events = P.extract_ddl_full(body, mail_dt)
                vendor = P.detect_vendor(sender, body)

                results.append({
                    "msgid": msgid,
                    "company": company,
                    "title": title,
                    "due": due,
                    "confidence": conf,
                    "evidence": evidence,
                    "events": P.fmt_events(events),
                    "vendor": vendor,
                    "subject": subj,
                    "sender": sender,
                    "mail_dt": mail_dt,
                    "score": score,
                })
            log(f"  识别出测评邮件 {len([r for r in results])} 封（累计）")
    finally:
        try:
            M.close()
        except Exception:
            pass
        try:
            M.logout()
        except Exception:
            pass
    return results, logs
