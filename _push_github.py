# -*- coding: utf-8 -*-
"""临时脚本：用本机已存的 GitHub 凭证创建仓库（不打印令牌）。用完即删。"""
import json
import subprocess
import urllib.error
import urllib.request

REPO = "assessment-radar"
DESC = "自动抓取 QQ 邮箱里的秋招测评/笔试/面试通知，提取 DDL 并桌面弹窗提醒（纯 Python 标准库，数据全部本地）"
TOPICS = ["job-hunting", "email-automation", "imap", "deadline-reminder", "python", "tkinter", "sqlite"]


def get_cred():
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout
    d = {}
    for line in out.splitlines():
        k, _, v = line.partition("=")
        d[k.strip()] = v.strip()
    return d.get("username", ""), d.get("password", "")


def api(method, path, token, payload=None):
    url = f"https://api.github.com{path}"
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "assessment-radar-publisher")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"raw": body[:300]}


user, token = get_cred()
if not token:
    raise SystemExit("没有拿到 GitHub 凭证")

status, me = api("GET", "/user", token)
if status != 200:
    raise SystemExit(f"凭证无效（HTTP {status}）：{me.get('message')}")
login = me.get("login")
print(f"账号：{login}")

# 1) 先探测仓库是否已存在
status, repo = api("GET", f"/repos/{login}/{REPO}", token)
if status == 200:
    print(f"仓库已存在：{repo['html_url']}（{'私有' if repo['private'] else '公开'}）")
else:
    status, repo = api("POST", "/user/repos", token, {
        "name": REPO, "description": DESC, "private": True,
        "has_issues": True, "has_wiki": False, "auto_init": False,
    })
    if status not in (200, 201):
        raise SystemExit(f"创建仓库失败（HTTP {status}）：{repo.get('message')}")
    print(f"仓库已创建（私有）：{repo['html_url']}")

# 2) 补 topics（失败不影响主流程）
st, _ = api("PUT", f"/repos/{login}/{REPO}/topics", token, {"names": TOPICS})
print(f"topics 设置：HTTP {st}")

print(f"REMOTE_URL=https://github.com/{login}/{REPO}.git")
