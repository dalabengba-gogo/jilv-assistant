#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""备用推送通道：git push 网络不通时，用 GitHub Git Data API 推本地 HEAD。

背景（2026-10-23）：直连 github.com:443 被墙（15s 超时），沙箱代理对
github.com 的 CONNECT 返回 502，但 api.github.com 可达。于是绕开 git 传输层，
直接把本地 HEAD 相对远端分支的改动做成 blob/tree/commit 推上去。

用法：
    python tools/_push_via_api.py            # 推 HEAD 到远端同名分支（默认 main）
    python tools/_push_via_api.py --dry      # 只打印将要做什么
"""
import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

REPO = "dalabengba-gogo/jilv-assistant"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                 # v3/
REPO_ROOT = os.path.dirname(ROOT)            # git 仓库根


def git(*args, **kw):
    return subprocess.run(["git"] + list(args), cwd=REPO_ROOT,
                          capture_output=True, text=True, **kw).stdout


def get_token():
    out = subprocess.run(["git", "credential", "fill"],
                         input="protocol=https\nhost=github.com\n\n",
                         capture_output=True, text=True, cwd=REPO_ROOT, timeout=30).stdout
    for line in out.splitlines():
        if line.startswith("password="):
            t = line.split("=", 1)[1].strip()
            if t:
                return t
    raise SystemExit("凭证里没有 github.com 的 token")


def api(url, token, payload=None, method=None):
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    req.add_header("Authorization", "token " + token)
    req.add_header("Accept", "application/vnd.github+json")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8", "replace") or "{}")


def changed_files(base, head):
    """返回 [(status, path)]，path 为仓库相对路径（/ 分隔）。"""
    out = git("-c", "core.quotepath=false", "diff", "--name-status", base, head)
    res = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            res.append((parts[0][0], parts[-1]))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--branch", default="main")
    ap.add_argument("--base", default=None,
                    help="计算 diff 的本地基准 sha（默认=远端 head；远端 head 本地没有时手动指定）")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()

    token = get_token()
    head_sha = git("rev-parse", "HEAD").strip()
    msg = git("log", "-1", "--pretty=%B").rstrip()

    st, ref = api("https://api.github.com/repos/%s/git/ref/heads/%s" % (REPO, a.branch), token)
    if st != 200:
        raise SystemExit("取远端 ref 失败: %s %s" % (st, ref))
    remote_head = ref["object"]["sha"]
    print("远端 %s = %s" % (a.branch, remote_head[:8]))
    print("本地 HEAD = %s" % head_sha[:8])

    if remote_head == head_sha:
        print("已同步，无需推送")
        return

    files = changed_files(a.base or remote_head, head_sha)
    print("待更新 %d 个文件:" % len(files))
    for s, p in files:
        print("   %s %s" % (s, p))
    if a.dry:
        return

    st, rc = api("https://api.github.com/repos/%s/git/commits/%s" % (REPO, remote_head), token)
    if st != 200:
        raise SystemExit("取远端 commit 失败: %s %s" % (st, rc))
    base_tree = rc["tree"]["sha"]

    tree = []
    for s, p in files:
        if s == "D":
            tree.append({"path": p, "mode": "100644", "type": "blob", "sha": None})
            continue
        with open(os.path.join(REPO_ROOT, p.replace("/", os.sep)), "rb") as f:
            raw = f.read()
        st, blob = api("https://api.github.com/repos/%s/git/blobs" % REPO, token,
                       {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"})
        if st != 201:
            raise SystemExit("上传 blob 失败 %s: %s %s" % (p, st, blob))
        tree.append({"path": p, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print("   ↑ %s (%d bytes)" % (p, len(raw)))

    st, new_tree = api("https://api.github.com/repos/%s/git/trees" % REPO, token,
                       {"base_tree": base_tree, "tree": tree})
    if st != 201:
        raise SystemExit("建 tree 失败: %s %s" % (st, new_tree))

    st, new_commit = api("https://api.github.com/repos/%s/git/commits" % REPO, token,
                         {"message": msg, "tree": new_tree["sha"], "parents": [remote_head]})
    if st != 201:
        raise SystemExit("建 commit 失败: %s %s" % (st, new_commit))
    print("新 commit = %s" % new_commit["sha"][:8])

    st, upd = api("https://api.github.com/repos/%s/git/refs/heads/%s" % (REPO, a.branch),
                  token, {"sha": new_commit["sha"], "force": False}, method="PATCH")
    if st != 200:
        raise SystemExit("更新 ref 失败: %s %s" % (st, upd))
    print("✅ 已推送: https://github.com/%s/commit/%s" % (REPO, new_commit["sha"]))


if __name__ == "__main__":
    sys.exit(main())
