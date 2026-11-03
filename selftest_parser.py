# -*- coding: utf-8 -*-
"""parser 自测：用真实风格的测评邮件样本验证识别与 DDL 提取。"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parser as P

SAMPLES = [
    dict(
        name="北森·中国银行·明确日期时刻",
        sender="北森测评 <no-reply@beisen.com>",
        subject="【中国银行】2027年校园招聘在线测评通知",
        body="""尊敬的张三同学：
您好！感谢您申请中国银行2027年校园招聘岗位。现邀请您参加在线测评。
测评链接：https://bankofchina.beisen.com/abc
测评时长：60分钟
请在 2026年11月3日 18:00 前完成测评，逾期链接将自动失效。
提示：测评开始后不可中断，请预留完整时间。""",
        mail="2026-10-25 09:12",
        want_company="中国银行", want_ddl="2026-11-03 18:00", want_conf="exact", want_is=True,
    ),
    dict(
        name="牛客·字节·相对有效期",
        sender="牛客网 <notice@nowcoder.com>",
        subject="【字节跳动】2027校园招聘笔试邀请",
        body="""同学你好：
你已通过简历筛选，现邀请你参加在线笔试。
答题链接：https://www.nowcoder.com/test/xxx
本次笔试链接有效期为 7 天，请尽快完成作答。""",
        mail="2026-10-20 10:00",
        want_company="字节跳动", want_ddl="2026-10-27 10:00", want_conf="relative", want_is=True,
    ),
    dict(
        name="招行·只有日期无时刻",
        sender="招商银行招聘 <hr@cmbchina.com>",
        subject="【招商银行】2027届校园招聘在线测评",
        body="""您好，请您于 11月5日 前完成本次在线测评，
测评地址：https://cmb.example.com/t
如未在规定时间内完成，视为自动放弃。""",
        mail="2026-10-25 14:30",
        want_company="招商银行", want_ddl="2026-11-05 23:59", want_conf="day", want_is=True,
    ),
    dict(
        name="赛码·斜杠日期带秒",
        sender="赛码网 <service@acmcoder.com>",
        subject="华为技术有限公司 在线编程考试通知",
        body="""考试截止时间：2026/11/8 23:59:59
考试时长 120 分钟，请提前 10 分钟登录。""",
        mail="2026-10-26 08:00",
        want_company="华为技术", want_ddl="2026-11-08 23:59", want_conf="exact", want_is=True,
    ),
    dict(
        name="下午时刻归一",
        sender="测评中心 <assessment@huawei.com>",
        subject="【华为】校园招聘在线测评邀请",
        body="您好，请在11月10日下午3点前完成测评，谢谢。",
        mail="2026-10-26 08:00",
        want_company="华为", want_ddl="2026-11-10 15:00", want_conf="exact", want_is=True,
    ),
    dict(
        name="多个日期·取截止语境那条",
        sender="北森 <no-reply@beisen.com>",
        subject="【中国电信】2027校招测评通知",
        body="""测评开放时间：2026年10月28日 09:00
截止时间：2026年11月2日 23:59
请合理安排时间。""",
        mail="2026-10-26 09:00",
        want_company="中国电信", want_ddl="2026-11-02 23:59", want_conf="exact", want_is=True,
    ),
    dict(
        name="跨年推断（12月收到，1月截止）",
        sender="北森 <no-reply@beisen.com>",
        subject="【宁波银行】在线测评通知",
        body="请您在1月5日前完成在线测评。",
        mail="2026-12-20 10:00",
        want_company="宁波银行", want_ddl="2027-01-05 23:59", want_conf="day", want_is=True,
    ),
    dict(
        name="误判防护·数字非日期",
        sender="北森 <no-reply@beisen.com>",
        subject="【浙商银行】2027校招在线笔试",
        body="""薪资待遇 3.5 万元起，涨幅 1.2 倍。
请务必于 2026-11-06 12:00 前完成在线笔试。""",
        mail="2026-10-26 10:00",
        want_company="浙商银行", want_ddl="2026-11-06 12:00", want_conf="exact", want_is=True,
    ),
    dict(
        name="工作日有效期",
        sender="SHL <no-reply@shl.com>",
        subject="【联合利华】Management Trainee Assessment",
        body="Your assessment link will remain valid for 5 个工作日内. Please complete it.",
        mail="2026-10-23 09:00",
        want_company="联合利华", want_ddl="2026-10-30 09:00", want_conf="relative", want_is=True,
    ),
    dict(
        name="面试通知·规则变更后必须抓（2026-10-31）",
        sender="hr@somecorp.com",
        subject="【某公司】面试通知",
        body="您好，邀请您于 11月5日 来我司参加面试，地址：杭州市西湖区。",
        mail="2026-10-25 09:00",
        want_company="某公司", want_ddl="2026-11-05 23:59", want_conf="day", want_is=True,
    ),
    dict(
        name="HTML 正文",
        sender="Moka <no-reply@mokahr.com>",
        subject="【美团】2027届校园招聘-在线测评邀请",
        body="""<html><body><p>您好，</p><p>请在 <b>2026年11月4日 20:00</b> 前完成在线测评。</p>
        <p><a href="https://app.mokahr.com/x">点击开始</a></p></body></html>""",
        mail="2026-10-25 09:00",
        want_company="美团", want_ddl="2026-11-04 20:00", want_conf="exact", want_is=True,
    ),
    dict(
        name="正文无 DDL·仅提示尽快",
        sender="北森 <no-reply@beisen.com>",
        subject="【中国平安】在线测评",
        body="请登录系统完成在线测评。",
        mail="2026-10-25 09:00",
        want_company="中国平安", want_ddl=None, want_conf="none", want_is=True,
    ),
    # ---- v2 新增：回执独立 / 时刻区间 / 主截止选登录截止 ----
    dict(
        name="苏州银行·回执不污染主截止",
        sender="苏州银行 <notice@suzhoubank.com>",
        subject="苏州银行2027秋季校园招聘在线笔试",
        body="""您已通过简历筛选，请参加在线笔试。
这是您的专属回执链接，请勿转发分享。请于10月28日24点前查收邮件并反馈意愿。
请于10月30日18:00前登录考试系统完成设备检测。
考试时间：10月30日20:30-21:27。""",
        mail="2026-10-27 10:00",
        want_company="苏州银行", want_ddl="2026-10-30 18:00", want_conf="exact", want_is=True,
        want_receipt=True,
    ),
    dict(
        name="纯时刻区间·19:00-19:15取19:15",
        sender="北森 <no-reply@beisen.com>",
        subject="【杭州银行】在线笔试通知",
        body="考试时间：10月31日19:00-19:15，请提前15分钟登录候考。",
        mail="2026-10-28 12:00",
        want_company="杭州银行", want_ddl="2026-10-31 19:15", want_conf="range", want_is=True,
    ),
    # ---- v3 新增：正向邀请被「感谢您」误杀的回归样本（2026-10-31 真实翻车）----
    dict(
        name="娇韵诗·感谢您开头的正向测评",
        sender="娇韵诗 <service@mail.example.com>",
        subject="娇韵诗2027届校园招聘项目线上测评",
        body="""尊敬的张三：您好！感谢您参与娇韵诗2027届校园招聘项目，现进入我们的人才测评阶段。
现需要您作答一份测评，测试时长约为60分钟，请您抽出宝贵时间，在五天内完成测评。""",
        mail="2026-10-29 22:18",
        want_company="娇韵诗", want_ddl="2026-11-03 22:18", want_conf="relative", want_is=True,
    ),
    dict(
        name="万兴科技·正文含测评结果字样",
        sender="iTalent招聘助手 <noreply@italent.example.com>",
        subject="万兴科技集团股份有限公司的人才测评邀请",
        body="""张三，你好！感谢您投递万兴科技集团股份有限公司，我们需要您作答一份测评问卷。
本次测评结果将作为招聘录用评估的重要参考。请您认真对待，务必完成。""",
        mail="2026-10-30 11:31",
        want_company="万兴科技", want_ddl=None, want_conf="none", want_is=True,
    ),
    dict(
        name="金山办公·考试邀请+页脚unsubscribe",
        sender="金山办公软件 <wps-no-reply@mail.example.com>",
        subject="来自金山办公的考试邀请",
        body="""您好！张三，感谢您投递金山办公的运营专员（AI应用专项）-面向2027届-武汉，
您的简历已经通过了筛选，您有资格进入我们的人才考试阶段。我们需要您作答一份笔试试卷，请尽快作答。
unsubscribe""",
        mail="2026-10-30 15:39",
        want_company="金山办公软件", want_ddl=None, want_conf="none", want_is=True,
    ),
    # ---- v4：面试类邮件也要抓（2026-10-31 用户要求）----
    dict(
        name="中信AI面试·确认截止优先于面试窗口",
        sender="中信银行杭州分行 <service@quickmail.51job.com>",
        subject="中信银行杭州分行2027年秋季校园招聘第一轮线上AI面试邀请",
        body="""亲爱的张三同学：恭喜您进入中信银行杭州分行2027年秋季校园招聘第一轮面试环节。
本轮面试采用线上AI面试，仅有一次作答机会。
现确认您是否参加本轮AI面试，请您在11月1日12时前点击"确认参加"或"放弃"按钮。
一、面试时间：2026年10月31日9:00-11月1日18:00
二、面试时长：约30分钟""",
        mail="2026-10-30 15:19",
        want_company="中信银行杭州分行", want_ddl="2026-11-01 12:00", want_conf="exact",
        want_is=True, want_receipt=True,
    ),
    # ---- v4.1：相对时长保留邮件时刻（用户给的真值，2026-10-31）----
    dict(
        name="达能·5天内完成·保留邮件时刻17:59",
        sender="达能校园招聘 <campus@danone.example.com>",
        subject="2027达能管培生招聘 - AI 面试邀请 - 市场营销管培生-达能中国",
        body="""张三，您好！
感谢您申请达能2027届管培生项目，现邀请您参加AI面试。
面试时长约20分钟，请在收到本邮件后的5天内完成面试。""",
        mail="2026-10-26 17:59",
        want_company="达能", want_ddl="2026-10-31 17:59", want_conf="relative", want_is=True,
    ),
    dict(
        name="满意度调查仍应排除",
        sender="好未来招聘 <tal-no-reply@mail.example.com>",
        subject="来自好未来招聘的面试满意度调查",
        body="感谢您参加面试，请花一分钟填写面试满意度调查问卷。",
        mail="2026-10-30 20:15",
        want_company=None, want_ddl=None, want_conf=None, want_is=False,
    ),
    dict(
        name="万兴·年份日期+裸星期+生效失效",
        sender="万兴科技 <hr@wondershare.com>",
        subject="万兴科技人才测评邀请",
        body="""本次测试邀请于 2026年10月30日 周五 11:26 生效，于 2026年11月13日 周五 11:26 失效。请抓紧时间完成测评。
万兴科技集团股份有限公司
2026年10月30日""",
        mail="2026-10-30 11:30",
        want_company="万兴科技", want_ddl="2026-11-13 11:26", want_conf="exact", want_is=True,
    ),
    dict(
        name="万兴真实邮件·设备检测含不符合要求不误杀",
        sender="iTalent招聘助手 <noreply@shmail.ibeisen.com>",
        subject="万兴科技集团股份有限公司的人才测评邀请",
        body="""接下来，请输入你的登录信息登录作答：
邮件：candidate@example.com
通行证：00000000000000
本次测试邀请于\xa02026年10月30日 周五 11:26\xa0生效，于\xa02026年11月13日 周五 11:26\xa0失效。请抓紧时间完成测评。
1.请先登录 https://xxx.ceping.com(非正式测评链接),对电脑浏览器、摄像头、手机拍照和网速进行测试,如不符合要求,请调整或更换电脑设备或网络;
2.请使用谷歌浏览器作答。""",
        mail="2026-10-30 11:31",
        want_company="万兴科技", want_ddl="2026-11-13 11:26", want_conf="exact", want_is=True,
    ),
]

ok = fail = 0
rows = []
for s in SAMPLES:
    mail_dt = datetime.strptime(s["mail"], "%Y-%m-%d %H:%M")
    body_text = P.html_to_text(s["body"]) if "<" in s["body"] else s["body"]
    is_a, score = P.is_assessment_mail(s["subject"], body_text, s["sender"])

    company = P.extract_company(s["subject"], body_text, s["sender"]) if is_a else "-"
    ddl, conf, snip = P.extract_ddl(body_text, mail_dt) if is_a else (None, "none", "")
    ddl_s = ddl.strftime("%Y-%m-%d %H:%M") if ddl else None

    errs = []
    if is_a != s["want_is"]:
        errs.append(f"识别={is_a}(期望{s['want_is']})")
    if s["want_is"]:
        if s["want_company"] and s["want_company"] != company:
            errs.append(f"公司={company}(期望{s['want_company']})")
        if s["want_ddl"] != ddl_s:
            errs.append(f"DDL={ddl_s}(期望{s['want_ddl']})")
        if s["want_conf"] and conf != s["want_conf"]:
            errs.append(f"置信={conf}(期望{s['want_conf']})")
        if s.get("want_receipt"):
            evs = P.fmt_events(P.extract_events(body_text, mail_dt))
            if not any(e["kind"] == "receipt" for e in evs):
                errs.append(f"缺少回执提醒节点: {evs}")

    if errs:
        fail += 1
        rows.append(("FAIL", s["name"], "; ".join(errs)))
    else:
        ok += 1
        rows.append(("PASS", s["name"], f"{company} | {ddl_s} | {conf}"))

for st, name, msg in rows:
    print(f"[{st}] {name:32s} {msg}")
print(f"\n通过 {ok} / {ok+fail}")
sys.exit(0 if fail == 0 else 1)
