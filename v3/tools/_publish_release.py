#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把绿色版 zip 发到 GitHub Releases（附件方式，不进 git 仓库）。

为什么不直接 git add zip：
  GitHub 单文件硬上限 100MB，本包 112.5MB 会被拒；而且项目约定
  "绿色版只通过 Releases 分发，不进仓库"（否则每次 clone 都要下 112MB、历史永久膨胀）。

附件名必须英文：GitHub Releases 的资产名不支持中文 → JilvAssistant_vX.Y.Z.zip

用法：
    python tools/_publish_release.py                    # 版本读 common.py，自动发当前版本
    python tools/_publish_release.py --version 6.0.0
    python tools/_publish_release.py --dry              # 只打印将要做什么
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO = "dalabengba-gogo/jilv-assistant"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # v3/
DIST = os.path.join(ROOT, "dist")
REPO_ROOT = os.path.dirname(ROOT)                  # git 仓库根（凭证在这里取）


def read_version():
    import re
    p = os.path.join(ROOT, "app", "xyfz_auto", "common.py")
    with open(p, encoding="utf-8") as f:
        m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', f.read())
    if not m:
        raise SystemExit("读不到 APP_VERSION")
    return m.group(1)


def get_token():
    """从 git 凭证管理器取 token（不落盘、不打印）。"""
    try:
        out = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, cwd=REPO_ROOT, timeout=30,
        ).stdout
    except Exception as e:
        raise SystemExit("调用 git credential 失败: %s" % e)
    for line in out.splitlines():
        if line.startswith("password="):
            t = line.split("=", 1)[1].strip()
            if t:
                return t
    raise SystemExit("凭证里没有 github.com 的 token（先 git push 一次让凭证落盘）")


def api(url, token, payload=None, method=None, ctype="application/json"):
    data = None
    if payload is not None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Authorization", "token " + token)
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def upload_asset(upload_url, token, path, name, dry=False):
    size = os.path.getsize(path)
    print("  上传 %-34s %6.1f MB" % (name, size / 1048576))
    if dry:
        return True
    url = "%s?name=%s" % (upload_url.split("{")[0], urllib.parse.quote(name))
    req = urllib.request.Request(url, method="POST")
    req.add_header("Authorization", "token " + token)
    req.add_header("Content-Type", "application/zip" if name.endswith(".zip") else "text/plain")
    req.add_header("Content-Length", str(size))
    with open(path, "rb") as f:
        req.data = f.read()
    try:
        with urllib.request.urlopen(req, timeout=1800) as r:
            d = json.loads(r.read().decode("utf-8"))
            print("  ✓ %s  (%.1f MB 已在 Release 上)" % (d.get("name"), d.get("size", 0) / 1048576))
            return True
    except urllib.error.HTTPError as e:
        print("  ✗ HTTP %s: %s" % (e.code, e.read().decode("utf-8", "replace")[:300]))
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=None)
    ap.add_argument("--notes", default=None, help="Release 说明(Markdown 文件路径)")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    ver = a.version or read_version()
    tag = "v" + ver
    zip_local = os.path.join(DIST, "鸡驴助手_v%s.zip" % ver)
    md5_local = os.path.join(DIST, "鸡驴助手_v%s.md5.txt" % ver)
    for p in (zip_local, md5_local):
        if not os.path.isfile(p):
            raise SystemExit("缺少产物: %s" % p)

    if a.notes and os.path.isfile(a.notes):
        with open(a.notes, encoding="utf-8") as f:
            body = f.read()
    else:
        body = "绿色版，解压即用（无需安装 Python）。详见包内「使用说明.txt」。"

    print("仓库 %s / tag %s" % (REPO, tag))
    token = get_token()
    print("  token 已取得（%d 字符）" % len(token))

    # 已存在就直接复用，避免重复建
    st, rel = api("https://api.github.com/repos/%s/releases/tags/%s" % (REPO, tag), token, method="GET")
    if st == 200:
        print("  Release %s 已存在，复用 (id=%s)" % (tag, rel["id"]))
    else:
        print("  创建 Release %s ..." % tag)
        st, rel = api("https://api.github.com/repos/%s/releases" % REPO, token, {
            "tag_name": tag,
            "target_commitish": "main",
            "name": "鸡驴助手 %s" % tag,
            "body": body,
            "draft": False,
            "prerelease": False,
        })
        if st not in (200, 201):
            raise SystemExit("创建 Release 失败 HTTP %s: %s" % (st, rel))
        print("  ✓ 已创建 (id=%s)" % rel["id"])

    have = {x.get("name") for x in rel.get("assets", [])}
    ok = True
    for local, name in (
        (zip_local, "JilvAssistant_%s.zip" % tag),
        (md5_local, "JilvAssistant_%s.md5.txt" % tag),
    ):
        if name in have:
            print("  - %s 已存在，跳过" % name)
            continue
        ok = upload_asset(rel["upload_url"], token, local, name, a.dry) and ok

    print("\n页面: https://github.com/%s/releases/tag/%s" % (REPO, tag))
    print("直链: https://github.com/%s/releases/download/%s/JilvAssistant_%s.zip" % (REPO, tag, tag))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
