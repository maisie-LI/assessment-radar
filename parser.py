# -*- coding: utf-8 -*-
"""
测评邮件识别 + DDL（截止时间）提取。

设计原则：
1. 宁可早报，不可晚报 —— 同一优先级别下取更早的日期。
2. 每个结论都带置信度，让用户知道这是"邮件明写的"还是"猜的"。
3. 不追求 100% 全自动，抽不准则标记成 low，交给人工在工作台里改。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from html.parser import HTMLParser

# 规则版本号。改了解析规则就 +1 —— 程序启动时发现版本变了，
# 会自动用新规则重算一遍待办（保留已完成/手动改过的），
# 用户不需要再手动点一次"检查邮件"。
RULE_VERSION = "v6-2026-11-01"


# ---------------------------------------------------------------- 正文清洗

class _HTMLText(HTMLParser):
    """极简 HTML -> 文本，够用且零依赖。"""

    BLOCK = {"br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4", "table", "section"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buf: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.buf.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self.buf.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.buf.append(data)

    @property
    def text(self) -> str:
        return "".join(self.buf)


def html_to_text(html: str) -> str:
    p = _HTMLText()
    try:
        p.feed(html or "")
    except Exception:
        return re.sub(r"<[^>]+>", " ", html or "")
    return p.text


def normalize(text: str) -> str:
    """全角转半角 + 空白收敛 + 去掉括号里的星期。"""
    out = []
    for ch in text or "":
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    s = "".join(out)
    # 关键：先剔除「(周三)(星期三)」这类插入语，否则会把后面的时刻整段打断
    # 例：10月31日(周六)上午9:30 —— 不清洗就只解析出日期，时间丢失
    s = re.sub(r"[（(]\s*(?:周|星期)[一二三四五六日天]\s*[）)]", " ", s)
    s = re.sub(r"[（(]\s*星期\s*[一二三四五六日天]\s*[）)]", " ", s)
    # 裸写的星期（不带括号）夹在日期和时刻之间也会打断解析：
    # 例：「2026年11月13日 周五 11:26 失效」——只在「日期→时刻」之间精准剔除
    s = re.sub(
        r"(?<=[日号\d])\s*(?:周|星期)[一二三四五六日天](?=\s*\d{1,2}\s*[:时点])",
        " ",
        s,
    )
    # 「10月28日-30日」这种省略后一个月分的区间，先补全成两个完整日期，
    # 否则只会解析出开始日，把开始当成截止
    s = re.sub(
        r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?\s*[-~～—–－至到]\s*(\d{1,2})\s*[日号]",
        lambda m: f"{m.group(1)}月{m.group(2)}日-{m.group(1)}月{m.group(3)}日",
        s,
    )
    # 「10月30日19:00-19:15」纯时刻区间：补全第二个日期，
    # 否则只解析出开始时刻 19:00，真正的硬截止 19:15 被漏掉
    # （后面若直接跟「10月31日」这类完整日期则不处理，交给上面的日期区间逻辑）
    s = re.sub(
        r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?\s*(\d{1,2}[:时点]\d{1,2}(?:分)?)"
        r"\s*[-~～—–－至到]\s*(?!\s*\d{1,2}\s*[月年])(\d{1,2})[:时点](\d{1,2})(?:分)?",
        lambda m: f"{m.group(1)}月{m.group(2)}日{m.group(3)}-"
                  f"{m.group(1)}月{m.group(2)}日{m.group(4)}:{m.group(5)}",
        s,
    )
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s


# ---------------------------------------------------------------- 日期解析

# 允许：2026年11月3日 / 2026-11-03 / 2026/11/3 / 11月3日 / 11-03 / 11.3
_DATETIME_RE = re.compile(
    r"(?P<y>20\d{2})?\s*[年\-/.]?\s*"
    r"(?P<m>\d{1,2})\s*[月\-/.]\s*"
    r"(?P<d>\d{1,2})\s*[日号]?"
    # 注意：时刻里的空白只允许空格/Tab，不能跨行（\s 会吃掉换行，
    # 导致"09:00\n截止时间：…"被当成一句话，下一行的截止标签串味）
    r"(?:[ \t]*(?P<ap>上午|下午|晚上|中午|凌晨|晚|早)?[ \t]*"
    r"(?P<H>\d{1,2})[ \t]*[:时点][ \t]*(?P<M>\d{1,2})[ \t]*分?"
    r"|[ \t]*(?P<ap2>上午|下午|晚上|中午|凌晨|晚|早)?[ \t]*"
    r"(?P<H2>\d{1,2})[ \t]*[时点](?:[ \t]*(?P<M2>\d{1,2})[ \t]*分?)?"
    r")?"
)

# 排除：3.5分 / 1.2倍 这类非日期
_BAD_TAIL = re.compile(r"^\s*(分|小时|天|个|倍|折|%|％|万|k|K|w|W|人|元)")

_CN_NUM = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_num(s: str) -> int:
    """「五」「十五」「3」都能转成数字；转不了返回 0。"""
    s = (s or "").strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    if "十" in s:
        parts = s.split("十")
        tens = _CN_NUM.get(parts[0], 1) if parts[0] else 1
        ones = _CN_NUM.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        return tens * 10 + ones
    return _CN_NUM.get(s, 0)


def _add_workdays(dt: datetime, n: int) -> datetime:
    """「5 个工作日内」按真实工作日累加，跳过周六周日。"""
    cur = dt
    left = max(0, n)
    while left > 0:
        cur += timedelta(days=1)
        if cur.weekday() < 5:
            left -= 1
    return cur


_REL_DUR = re.compile(
    r"(?:有效期(?:为|是)?\s*[:：]?\s*|有效\s*)"
    r"(?P<n>\d{1,3}|[一二两三四五六七八九十]{1,3})\s*(?:个)?\s*(?P<u>天|日|工作日|小时|钟头)"
    r"|(?P<n2>\d{1,3}|[一二两三四五六七八九十]{1,3})\s*(?:个)?\s*(?P<u2>天|日|工作日|小时|钟头)\s*(?:之?内|以内)"
)

# ---------------------------------------------------------------- 事件分类
#
# 一封测评邮件里常常有多个时间点，性质完全不同：
#   「考点预约时间：10月28日10:00-10月30日12:00」  ← 前置任务截止，错过就没法考试
#   「笔试时间：10月31日上午9:30」                  ← 考试开始
#   「测评链接有效期至11月3日23:59」                ← 通用截止
# 早期版本只挑"权重最高的一个日期"，会把区间起点当截止，甚至一个都抓不到。
# 现在改成：先抽成事件列表，再按"错过了后果最严重且最早"挑主 DDL。

# 语义分层（用户 2026-10-31 定）：一封邮件里的时间要按性质分层展示
#   ① 试考时间      —— 提前试做/模拟/设备检测，错过还能救，但要早提醒
#   ② 确认/回执时间 —— 回复组织方的动作（确认参加、反馈意愿）
#   ③ 正式笔试时间  —— 真正的考试，往往还要单独一条提醒
# 下面四个关键词表一一对应这四层（外加"通用截止"）。
_KW_PRETEST = [
    "试考", "模拟考", "模拟测试", "模拟笔试", "模拟面试", "模拟", "设备测试",
    "测试设备", "环境测试", "调试", "候考", "提前登录", "提前进入",
]
# 其余"必须先做的准备工作"：预约、打印、上传、缴费…错过同样进不了考场
_KW_SUB = [
    "考点预约", "预约考点", "选考点", "预约", "准考证", "打印",
    "完善简历", "上传", "问卷", "信息确认",
    "签约", "缴费", "确认信息", "资料审核",
]
# 回执/意愿反馈类：只是"回复组织方"的动作，不是测评本身的截止。
# 单独成块展示（回执提醒），永远不作为主截止。
_KW_RECEIPT = [
    "回执", "确认参加", "参加确认", "反馈意愿", "意愿反馈", "意向确认", "确认意向",
    "回复确认", "确认出席", "是否参加", "参会确认", "查收邮件并反馈",
]
_KW_EXAM = [
    "笔试时间", "考试时间", "测评时间", "开考", "考试安排", "正式考试", "在线笔试",
    "在线测评", "作答时间", "考试将于", "笔试将于", "测评将于", "考试日期",
    "笔试日期", "测试时间", "面试时间", "笔试开始", "考试开始",
    "面试安排", "面试将于", "面试开始", "面试日期", "面试地点",
]
_KW_DEADLINE = [
    "截止", "有效期", "失效", "过期", "前完成", "前提交", "前作答", "最晚",
    "不晚于", "deadline", "关闭", "结束时间", "务必于",
]
_KW_START = [
    "开放时间", "开始时间", "起始", "开放日期", "开始日期", "开放", "可登录",
    "入口开放", "开始打印", "开始预约", "开放打印", "生效", "启用",
]

# 时间段分隔符：10:00-10月30日12:00 / 10月28日至10月30日
_RANGE_SEP = re.compile(r"^[\s\-~～—–－至到]+$")

# 日期后面紧跟「前/之前」= 最标准的截止写法（如「请在11月3日前完成」）
_POST_DEADLINE_RE = re.compile(r"^[\s,，、。:：]{0,3}(?:之前|以前|前)(?![\u4e00-\u9fa5]{0,2}(?:天|周|月))")

# 「正式笔试安排在…」「考试将于…」这类没有"时间"二字的写法
_EXAM_CTX_RE = re.compile(
    r"(?:笔试|考试|测评|测试|面试)(?:将|安排|定|预计|正式)"
    r"|正式(?:笔试|考试|测评|面试)"
    r"|(?:笔试|考试|测评|测试)(?:时间|日期|开始|开考|进行|举行)"
)
_EXAM_AFTER_RE = re.compile(r"^[\s,，、。:：]{0,6}(?:\S{0,8})?(?:开始|开考|进行|举行|正式开始)")


def _label_before(norm: str, s: int) -> str:
    """取日期前面同一句里的事件标签，如「3.准考证打印时间:」。"""
    line_start = norm.rfind("\n", 0, s) + 1
    seg = norm[line_start:s]
    cut = max(seg.rfind("。"), seg.rfind("；"), seg.rfind(";"), seg.rfind("|"))
    return seg[cut + 1:].strip()[-60:]


def _classify_ctx(label: str, after: str) -> str:
    """同时看日期前标签和日期后语境，避免只看一边导致漏判。"""
    low = (label or "").lower()
    aft = after or ""

    # 回执/意愿反馈：标签或紧随日期的后文（18字窗口）命中都算。
    # 例：「请于10月28日24点前查收邮件并反馈意愿」——标签只有"请于"，
    # 必须看后文里的"反馈意愿"才能识别出来。
    for kw in _KW_RECEIPT:
        if kw in low or kw in aft:
            return "receipt"

    # 「试考时间」「设备检测截止」——提前试做，单独一层
    for kw in _KW_PRETEST:
        if kw in low:
            return "pretest"

    # 「考点预约时间」「准考证打印时间」——前置任务
    for kw in _KW_SUB:
        if kw in low:
            return "sub"

    # 「请在11月3日前完成试考」——日期后面跟"前"，是截止而不是起点
    if _POST_DEADLINE_RE.match(aft):
        for kw in _KW_START:
            if kw in low:
                return "start"
        return "deadline"

    for kw in _KW_EXAM:
        if kw in low:
            return "exam"
    if _EXAM_CTX_RE.search(low) or _EXAM_AFTER_RE.match(aft):
        return "exam"

    # 「…11月13日 11:26 失效」「…18:00 截止」——截止/失效词紧跟在日期后面。
    # 只看同一行内日期后的一小段，避免误吃下一行的"截止时间："标签。
    aft_line = aft.split("\n", 1)[0]
    aft_head = aft_line[:8]
    for kw in _KW_DEADLINE:
        if kw in low or kw in aft_head:
            return "deadline"
    for kw in _KW_START:
        if kw in low or kw in aft_head:
            return "start"
    return "other"


def _classify(label: str) -> str:
    return _classify_ctx(label, "")


def _between_is_range(a_end: int, b_start: int, norm: str) -> bool:
    """两个日期之间只有连接符（可含括号插入语），说明是一个时间段。"""
    if b_start <= a_end:
        return False
    between = norm[a_end:b_start]
    between = re.sub(r"[（(][^）)]{0,14}[）)]", " ", between).strip()
    return bool(between) and bool(_RANGE_SEP.match(between))


def extract_events(text: str, mail_dt: datetime) -> list[dict]:
    """
    把正文里的时间点抽成事件列表。
    每个事件：{dt, has_time, label, kind, is_range_end, evidence}
    kind: sub(前置任务) / exam(正式考试) / deadline(通用截止) / start(开放) / other
    时间段只保留结束点（起点不是截止）。
    """
    norm = normalize(text or "")
    seq: list[dict] = []
    for dt, has_time, s, e in _iter_dates(norm, mail_dt):
        if dt < mail_dt - timedelta(days=3) or dt > mail_dt + timedelta(days=400):
            continue
        seq.append({
            "dt": dt, "has_time": has_time, "s": s, "e": e,
            "label": _label_before(norm, s), "kind": None,
            "is_range_end": False, "is_range_start": False,
            "range_start": None, "norm": norm,
        })

    # 标记时间段：A-B 里 A 是起点、B 是终点，标签沿用 A 前面的。
    # 起点要记下来 —— 用户要求正式考试展示成「开始时间：结束时间」。
    for i in range(1, len(seq)):
        if _between_is_range(seq[i - 1]["e"], seq[i]["s"], norm):
            seq[i]["is_range_end"] = True
            seq[i - 1]["is_range_start"] = True
            seq[i]["range_start"] = seq[i - 1]["dt"]
            if seq[i - 1]["label"]:
                seq[i]["label"] = seq[i - 1]["label"]

    events: list[dict] = []
    for ev in seq:
        if ev["is_range_start"]:
            continue  # 区间起点不是截止时间
        ev["kind"] = _classify_ctx(ev["label"], norm[ev["e"]: ev["e"] + 18])
        events.append(ev)
    return events


def _snippet(norm: str, ev: dict) -> str:
    s, e = ev["s"], ev["e"]
    return norm[max(0, s - 40): min(len(norm), e + 40)].strip().replace("\n", " ")[:120]


def extract_ddl_full(
    text: str, mail_dt: datetime
) -> tuple[datetime | None, str, str, list[dict]]:
    """
    返回 (主截止时间, 置信度, 证据, 全部事件)
    置信度: exact / range(时间段取结束) / day / relative / inferred / none
    主截止的选法：在"必须行动"的事件里取**最早且未过期**的一个——宁可早提醒，绝不晚报。
    """
    norm = normalize(text or "")
    events = extract_events(text, mail_dt)

    # 主截止的优先级（宁可早提醒，绝不晚报）：
    #   ① 前置任务/通用截止（登录截止、预约截止、打印截止…）——错过直接进不了考场
    #   ② 试考截止（试考通常可选，不参加也能考，所以排在①之后）
    #   ③ 回执确认截止（没有别的截止时，它就是最该盯的动作——如 AI 面试"确认参加"）
    #   ④ 考试/面试时间（只是日程，最后兜底）
    actionable = [e for e in events if e["kind"] in ("sub", "deadline")]
    if not actionable:
        actionable = [e for e in events if e["kind"] == "pretest"]
    if not actionable:
        actionable = [e for e in events if e["kind"] == "receipt"]
    if not actionable:
        actionable = [e for e in events if e["kind"] == "exam"]
    if actionable:
        future = [e for e in actionable if e["dt"] >= mail_dt - timedelta(days=1)]
        pool = future or actionable
        pool.sort(key=lambda e: e["dt"])
        best = pool[0]
        if best["is_range_end"]:
            conf = "range"
        else:
            conf = "exact" if best["has_time"] else "day"
        return best["dt"], conf, _snippet(norm, best), events

    # 没有任何事件标签时：全文只有一个日期，就先记下（标低置信度）
    others = [e for e in events if e["kind"] not in ("start", "receipt")]
    if len(others) == 1:
        e = others[0]
        return (e["dt"], "exact" if e["has_time"] else "day",
                _snippet(norm, e), events)

    # 相对时长兜底：「有效期 7 天」「3 天内完成」「在五天内完成面试」
    # ⚠️ 关键：按「邮件发送时刻 + N 天」推算，**保留原时刻**。
    # 用户给的真值：达能邮件 2026-10-26 17:59 收到、写"5 天内完成" → 截止 10-31 17:59。
    # 早期版本强改成 23:59，会晚报近 6 小时，违反"宁可早报"。
    m = _REL_DUR.search(norm)
    if m:
        n = _cn_num(m.group("n") or m.group("n2") or "")
        unit = (m.group("u") or m.group("u2") or "天")
        if n:
            if unit in ("小时", "钟头"):
                dt = mail_dt + timedelta(hours=n)
            elif unit == "工作日":
                dt = _add_workdays(mail_dt, n)
            else:
                dt = mail_dt + timedelta(days=n)
            dt = dt.replace(second=0, microsecond=0)
            snip = norm[max(0, m.start() - 40): m.end() + 40].strip().replace("\n", " ")[:120]
            return dt, "relative", snip, events

    return None, "none", "", events


def extract_ddl(text: str, mail_dt: datetime) -> tuple[datetime | None, str, str]:
    """兼容旧调用：只要 (截止时间, 置信度, 证据)。"""
    dt, conf, ev, _ = extract_ddl_full(text, mail_dt)
    return dt, conf, ev


def _exam_name(label: str) -> str:
    """正式环节叫什么：面试 / 笔试 / 考试。"""
    lab = label or ""
    if "面试" in lab:
        return "面试"
    if "笔试" in lab:
        return "笔试"
    return "考试"


def fmt_events(events: list[dict], limit: int = 6) -> list[dict]:
    """
    给入库用：挑出需要展示的关键节点，按语义分层排序。
    层级（用户 2026-10-31 定）：试考 → 前置任务 → 回执 → 正式考试。
    区间事件同时给 start/end，前端才能显示「开始：结束」。
    """
    order = {"pretest": 0, "sub": 1, "receipt": 2, "deadline": 2, "exam": 3}
    keep = [e for e in events if e["kind"] in order]
    keep.sort(key=lambda e: (order[e["kind"]], e["dt"]))
    out: list[dict] = []
    seen: set[tuple] = set()
    for e in keep:
        if e["kind"] == "receipt":
            label = "回执提醒"
        else:
            label = re.sub(r"^\s*[\d.、)（(]+\s*", "", e["label"] or "").strip(" :：")
            # 取「关键词前的几个字 + 关键词」，把长句子残留截掉
            m = re.search(
                r"([\u4e00-\u9fa5A-Za-z0-9]{0,6}(?:时间|日期|截止|预约|打印|试考|笔试|测评|考试|面试))",
                label,
            )
            if m:
                label = m.group(1)
            # 「请于」「务必于」这类纯虚词当标签没有信息量，退回类型名
            if not label or re.fullmatch(r"(?:请|请您|务必)?[于在]?|北京时间|时间", label.strip()):
                label = {"pretest": "试考时间", "sub": "前置任务",
                         "exam": "正式考试", "deadline": "截止"}[e["kind"]]
        if len(label) > 18:
            label = label[:18]

        end_s = e["dt"].strftime("%Y-%m-%d %H:%M")
        start_s = e["range_start"].strftime("%Y-%m-%d %H:%M") if e.get("range_start") else ""
        key = (e["kind"], label, start_s, end_s)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "kind": e["kind"],
            "label": label,
            "name": _exam_name(e["label"]) if e["kind"] == "exam" else "",
            "start": start_s,
            "end": end_s,
            "time": end_s,          # 兼容旧字段
        })
        if len(out) >= limit:
            break
    return out


def _fix_ampm(h: int, ap: str | None) -> int:
    if not ap:
        return h
    if ap in ("下午", "晚上", "晚") and h < 12:
        return h + 12
    if ap == "中午" and h < 12:
        return 12 if h <= 1 else h
    if ap == "凌晨" and h == 12:
        return 0
    return h


def _infer_year(month: int, mail_dt: datetime) -> int:
    """无年份时推断：若月日比邮件日期早 90 天以上，视为次年。"""
    y = mail_dt.year
    try:
        probe = datetime(y, month, 1)
    except ValueError:
        return y
    if (mail_dt - probe).days > 90:
        return y + 1
    return y


def _iter_dates(text: str, mail_dt: datetime):
    """产出 (datetime, has_time, start, end, confidence_hint)"""
    for m in _DATETIME_RE.finditer(text):
        raw_m, raw_d = m.group("m"), m.group("d")
        if not raw_m or not raw_d:
            continue
        month, day = int(raw_m), int(raw_d)
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        tail = text[m.end(): m.end() + 4]
        if _BAD_TAIL.match(tail):
            continue
        year = int(m.group("y")) if m.group("y") else _infer_year(month, mail_dt)

        hh = mm = None
        has_time = False
        if m.group("H"):
            hh, mm = int(m.group("H")), int(m.group("M") or 0)
            hh = _fix_ampm(hh, m.group("ap"))
            has_time = True
        elif m.group("H2"):
            hh, mm = int(m.group("H2")), int(m.group("M2") or 0)
            hh = _fix_ampm(hh, m.group("ap2"))
            has_time = True

        if has_time and not (0 <= hh <= 23 and 0 <= (mm or 0) <= 59):
            has_time, hh, mm = False, None, None

        try:
            if has_time:
                dt = datetime(year, month, day, hh, mm or 0)
            else:
                dt = datetime(year, month, day, 23, 59)
        except ValueError:
            continue
        # 正则可能把日期前的连接符一起吃掉（如 "-10月30日"），
        # 起点后移，否则无法判断这两个日期是不是一个时间段的起止
        start = m.start()
        while start < m.end() and text[start] in " \t-~～—–－/.":
            start += 1
        yield dt, has_time, start, m.end()


# ---------------------------------------------------------------- 测评识别

VENDORS = {
    "beisen": "北森", "italent": "北森", "nowcoder": "牛客", "acmcoder": "赛码",
    "shl": "SHL", "mokahr": "Moka", "moka": "Moka", "zhiye": "智鼎",
    "zhiding": "智鼎", "beichuang": "倍智", "ceping": "测评平台",
    "hackerrank": "HackerRank", "codility": "Codility", "codesignal": "CodeSignal",
    "hirevue": "HireVue", "hrtps": "HRTPS", "dayee": "大易", "ehr": "",
    "51job": "前程无忧", "zhaopin": "智联招聘", "liepin": "猎聘", "lagou": "拉勾",
    "wjx": "问卷星", "wenjuan": "问卷星", "hires": "Hires", "taleo": "",
    "workday": "Workday", "successfactors": "SAP", "smartrecruiters": "",
    "北极星": "北极星", "kaopu": "靠谱", "xiaoyuan": "", "yingjiesheng": "应届生",
}

# 供应商自己的名字，出现在公司位时要跳过
_VENDOR_NAMES = {"北森", "牛客", "赛码", "SHL", "Moka", "智鼎", "倍智", "HackerRank",
                 "Codility", "CodeSignal", "HireVue", "HRTPS", "大易", "问卷星",
                 "前程无忧", "智联招聘", "猎聘", "拉勾", "Workday", "SAP", "Hires",
                 "Taleo", "北极星", "靠谱", "应届生", "北森云计算", "北森测评"}

_ASSESS_KW = [
    "测评", "笔试", "在线测试", "在线考试", "在线作答", "能力测试", "性格测试",
    "职业性格", "认知能力", "行测", "在线编程", "编程考试", "专业笔试",
    "assessment", "online test", "written test", "test link", "coding test",
    "答题链接", "测评链接", "作答链接", "答题网址", "在线评估",
    "综合能力测试", "专业能力测试", "笔试通知", "测评通知", "考试通知",
    "考试邀请", "面试", "邀请您参加", "邀请您完成", "作答", "答题",
]
# 强否定：出现在标题里直接判为不用跟踪的邮件。
# 注意：面试类邮件自 2026-10-31 起也要跟踪（用户明确要求），
# 所以「面试通知」「面试邀请」已从排除项移出，只保留"结果通知/拒信/无关"类。
_HARD_NEG = [
    "笔试结果", "测评结果", "面试结果", "面试报告", "录用通知", "入职通知",
    "宣讲会", "招聘会", "双选会", "空中宣讲", "体检通知", "背景调查",
    "满意度", "感谢您", "遗憾", "未通过", "退订", "unsubscribe",
    "现在沟通",  # BOSS直聘等平台的聊天提醒，不是面试通知
    # 平台营销 / 流程性通知：只谈岗位、不谈让你去做测评/笔试/面试
    "直播", "攻略", "指南", "反诈", "投递成功", "投递反馈", "欢迎开启",
]
# 正文级扣分：只认真正的拒信特征。
# ⚠️ 「感谢您」「测评结果」「退订/unsubscribe」绝不能放这里——
# 正向测评邀请的开头几乎都是「感谢您投递…现进入测评阶段」，
# 「测评结果将作为录用参考」也是测评邮件的常规句式，
# 页脚 unsubscribe 更是标配，放进来会把好消息全部拦掉（2026-10-31 实际翻车）。
# ⚠️ 「不符合要求」也不能裸放——设备检测说明里就有
# 「如不符合要求，请调整或更换电脑设备」（2026-11-01 万兴邮件实际翻车），
# 只认带宾语的拒信写法。
_BODY_NEG = [
    "遗憾", "未通过", "抱歉地通知", "不再进入", "未能进入",
    "不符合任职要求", "不符合岗位要求", "不符合招聘要求", "不符合我们的要求",
]
_SOFT_NEG = ["offer", "录用", "体检", "宣讲"]  # 「面试」已不再扣分：面试类也要跟踪


# 口径（用户 2026-10-31 定，第一道筛子）：
#   邮件里要同时出现「动作词」和「对象词」，才算需要跟踪的邮件。
#   动作词：邀请 / 参加 / 完成 …（要求你去做某件事）
#   对象词：测评 / 测试 / 笔试 / 面试 …（做的是什么）
# 只出现对象词不出现动作词的（如"测评结果查询""笔试成绩"）不算。
_KW_ACTION = [
    "邀请", "参加", "参与", "完成", "作答", "答题", "登录", "进入", "确认",
    "invite", "invitation", "complete", "take the",
]
_KW_OBJECT_CN = ["测评", "测试", "笔试", "面试", "考试", "评估", "作答"]
_KW_OBJECT_EN = re.compile(
    r"\b(?:assessment|online test|written test|coding test|exam|interview)\b", re.I)


def _action_object_hit(subj: str, body_low: str) -> bool:
    """
    动作词 × 对象词 是否**构成搭配**（同一句话里），而不是全文各出现一次。
    例：
      ✓「邀请您参加在线测评」  ✓「请在11月3日前完成笔试」  ✓「测评邀请函」
      ✗「欢迎参加宣讲会…（正文别处提到测评）」  ✗「求职反诈指南…面试」
    只看标题 + 正文前 2500 字，动作词后 28 字窗口内出现对象词才算命中。
    """
    text = f"{subj}\n{body_low[:2500]}"
    act_re = "|".join(re.escape(k) for k in _KW_ACTION)
    obj_re = "|".join(re.escape(k) for k in _KW_OBJECT_CN)

    # 正向：动作词 → 后 28 字内出现对象词
    for m in re.finditer(act_re, text):
        win = text[m.start(): m.start() + 28]
        if re.search(obj_re, win) or _KW_OBJECT_EN.search(win):
            return True
    # 反向：对象词前 10 字内出现动作词（「测评邀请」「笔试确认」）
    for m in re.finditer(obj_re, text):
        win = text[max(0, m.start() - 10): m.start() + 6]
        if re.search(act_re, win):
            return True
    return False


def is_assessment_mail(subject: str, body: str, sender: str) -> tuple[bool, int]:
    """返回 (是否需要跟踪, 强度分)。阈值 3。"""
    subj = normalize(subject or "").lower()
    body_low = normalize(body or "").lower()[:6000]

    for neg in _HARD_NEG:
        if neg in subj:
            return False, -99

    # 正文里出现明确的拒信措辞 → 直接排除，不再看分数
    for neg in _BODY_NEG:
        if neg in body_low[:2000]:
            return False, -99

    hit_subj = [kw for kw in _ASSESS_KW if kw in subj]
    score = 3 * min(len(hit_subj), 2)

    hit_body = [kw for kw in _ASSESS_KW if kw in body_low and kw not in hit_subj]
    score += min(len(hit_body), 3)

    # 动作词 × 对象词 组合命中：最强信号，直接拉过阈值
    if _action_object_hit(subj, body_low):
        score += 5

    s_low = (sender or "").lower()
    if any(dom in s_low for dom in VENDORS):
        score += 2

    for neg in _SOFT_NEG:
        if neg in subj:
            score -= 2

    return score >= 3, score


def _clean_company(name: str) -> str:
    name = name.strip()
    # 「来自苏州银行的AI面试邀请」→ 公司名不该带「来自」
    name = re.sub(r"^(?:来自|回复[:：]?|转发[:：]?|关于|内推[:：]?)\s*", "", name)
    name = re.sub(r"(股份有限公司|有限责任公司|有限公司|集团|控股|科技|信息技术)$", "", name)
    # 显示名尾部的团队词也不该当公司名
    name = re.sub(r"(校园招聘?|招聘|校招|人力)(团队|组|中心|部)?$", "", name.strip())
    # 「万兴科技人才测评邀请」→「万兴科技」：公司名后不该带"人才/招聘团队"这类词
    name = re.sub(r"(人才|招聘)(团队|中心|组|部)?$", "", name.strip())
    name = re.sub(r"的$", "", name.strip())   # 「金山办公的考试邀请」→「金山办公」
    name = name.strip(" 　-—·,，。:：()（）[]【】\"'")
    return name[:24]


# 这些是邮箱显示名/主题里的通用词，不是公司
_GENERIC_NAMES = {
    "通知", "提醒", "公告", "招聘", "测评", "笔试", "邮件", "校招", "秋招", "春招",
    "校园招聘", "招聘中心", "测评中心", "笔试通知", "测评通知", "考试通知", "邀请函",
    "人力资源", "人力资源部", "招聘组", "招聘团队", "考试系统", "测评系统", "在线测评",
    "notice", "notification", "service", "support", "hr", "recruitment", "recruiting",
    "campus", "campusrecruitment", "mail", "mailer", "system", "admin", "info",
    "noreply", "no-reply", "donotreply", "do-not-reply", "test", "assessment",
    "exam", "hrsystem", "career", "careers", "jobs",
}


def _valid_company(name: str) -> bool:
    n = (name or "").strip()
    if len(n) < 2:
        return False
    if n.lower() in _GENERIC_NAMES:
        return False
    if re.fullmatch(r"[\d\W_]+", n):   # 纯符号/数字
        return False
    # 「测评邀请」「笔试通知」这类是事由，不是公司名（发件人显示名常这么写）
    if re.search(r"(邀请|通知|提醒|确认|反馈|问卷|测评|笔试|面试|考试|招聘|校招|秋招)",
                 n) and not re.search(
            r"(公司|集团|银行|证券|保险|基金|科技|汽车|地产|事务所|研究院|大学|学院)", n):
        return False
    return True


def extract_company(subject: str, body: str, sender: str) -> str:
    subj = normalize(subject or "")
    # 1) 【xxx】 或 [xxx]
    for m in re.finditer(r"[【\[]([^】\]]{2,30})[】\]]", subj):
        cand = m.group(1).strip()
        if cand in _VENDOR_NAMES or any(v in cand for v in _VENDOR_NAMES):
            continue
        if re.search(r"(校招|秋招|春招|招聘|测评|笔试|测试|通知|邀请|20\d{2})", cand) and len(cand) > 8:
            continue
        # 方括号里常放考试时间，如「…笔试通知【11月4日19:00】」——
        # 这种不是公司名（2026-11-03 申万宏源实际翻车），跳过
        if re.fullmatch(r"[\d\s:：\-—~/年月日时分\s]+", cand):
            continue
        if not re.search(r"[\u4e00-\u9fa5A-Za-z]", cand):
            continue
        c = _clean_company(cand)
        if _valid_company(c):
            return c
    # 2) 主题里的 "XX公司 / XX银行"
    # 先认带分支机构的：「中信银行杭州分行」比「中信银行」更精确
    m = re.search(r"([\u4e00-\u9fa5]{2,12}(?:银行|证券|保险)[\u4e00-\u9fa5]{2,6}?(?:分行|支行|分公司))", subj)
    if m:
        c = _clean_company(m.group(1))
        if _valid_company(c):
            return c
    m = re.search(r"([\u4e00-\u9fa5A-Za-z0-9]{2,20}?)(?:公司|集团|银行|证券|保险|研究院|事务所)", subj)
    if m:
        c = _clean_company(m.group(0))
        if _valid_company(c):
            return c
    # 2.4) 「中国平安测评邀请函」「易方达基金2027届校园招聘笔试通知」
    #      → 取对象词（测评/笔试/面试/考试）前面的那段当公司名
    m = re.match(
        r"^(?!来自|回复|转发)([\u4e00-\u9fa5A-Za-z0-9·&\s]{2,18}?)\s*(?:20\d{2}\s*届?)?\s*"
        r"(?:校园招聘|校招|秋招|春招)?\s*(?:在线)?(?:测评|笔试|面试|考试|评估)", subj)
    if m:
        c = _clean_company(m.group(1))
        if _valid_company(c):
            return c
    # 2.5) 主题开头直接是公司名：「易方达基金2027届…」「亿滋中国2027校园招聘…」
    m = re.search(r"^([\u4e00-\u9fa5A-Za-z·&]{2,16}?)(?=20\d{2}年?|校园招聘|校招|秋招|春招|暑期|实习)", subj)
    if m:
        c = _clean_company(m.group(1))
        if _valid_company(c) and c not in ("邀请", "通知", "提醒", "您的"):
            return c
    # 2.6) 英文格式「P&G - Invitation to ...」：破折号前是公司名
    m = re.match(r"^([A-Za-z][A-Za-z&·.\- ']{1,24}?)\s*[-–—]", subj)
    if m:
        c = _clean_company(m.group(1))
        if _valid_company(c):
            return c
    # 3) "邀请您参加 XX 的测评"
    m = re.search(r"参加\s*([\u4e00-\u9fa5A-Za-z0-9]{2,20}?)\s*的?(?:测评|笔试|在线测试|在线考试)", subj)
    if m:
        c = _clean_company(m.group(1))
        if _valid_company(c):
            return c
    # 4) 发件人显示名（最容易踩坑：notice / service / 通知 这类通用词要跳过）
    m = re.match(r"^([^<@]{2,30})", sender or "")
    if m:
        nm = _clean_company(m.group(1))
        if _valid_company(nm) and nm not in _VENDOR_NAMES \
                and not re.search(r"(no-?reply|donotreply|招聘|测评中心)", nm, re.I):
            return nm
    # 5) 从正文找
    m = re.search(r"([\u4e00-\u9fa5]{2,12}(?:公司|集团|银行|证券))", normalize(body[:2000]))
    if m:
        c = _clean_company(m.group(1))
        if _valid_company(c):
            return c
    # 6) 域名兜底
    dom = re.search(r"@([\w\-]+)\.", sender or "")
    if dom:
        d = dom.group(1).lower()
        if d not in ("qq", "163", "126", "gmail", "outlook", "hotmail", "sina", "foxmail"):
            return dom.group(1)
    return "未知来源"


def detect_vendor(sender: str, body: str) -> str:
    s = (sender or "").lower()
    for dom, nm in VENDORS.items():
        if nm and dom in s:
            return nm
    b = normalize(body[:3000])
    for dom, nm in VENDORS.items():
        if nm and nm in b:
            return nm
    return ""


def extract_title(subject: str, company: str) -> str:
    s = normalize(subject or "")
    s = re.sub(r"[【\[].*?[】\]]", "", s).strip(" -—·,，。")
    s = re.sub(r"^(关于|请您|邀请您|来自|通知[:：]?)", "", s)
    if len(s) > 40:
        s = s[:40] + "…"
    return s or f"{company} 测评"
