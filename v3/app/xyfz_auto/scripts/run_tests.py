# -*- coding: utf-8 -*-
"""回归测试一键入口 (2026-10-18 v4.7.0 批次四·工程 P2-2)

用法:
    python scripts/run_tests.py                  # 跑 scripts/test_*.py 全部
    python scripts/run_tests.py --only "*心跳*"   # 不好使: 用文件名片段过滤
    python scripts/run_tests.py --only test_log_rotate.py
    python scripts/run_tests.py --exclude "test_match.py,test_wait_battle.py"
    python scripts/run_tests.py --timeout 600     # 单个脚本上限(秒, 默认 300)
    python scripts/run_tests.py --no-junit        # 不写 JUnit XML

为什么不用 pytest:
  打包 runtime 里**没装 pytest**, 而 v3/vendor 是指向 D:\\鸡驴助手_dev\\vendor 的
  junction(v2 也在用同一个 runtime)。往里装 pytest 等于把两个工程的运行环境一
  起改掉, 还会被打进绿色版白白变大。
  再说本仓库的 test_*.py 全是**脚本式**的: 模块级直接跑、内部用 check() 断言、
  末尾 sys.exit(1 if FAILS else 0)。pytest 收集它们时会在 import 阶段就把整个
  脚本执行一遍(collection error), 改成 pytest 风格要重写 19 个文件 —— 收益远
  小于风险。
  所以这里自己撮了个 runner: 逐个 subprocess 跑、按退出码判定、产出 JUnit XML
  (与 pytest --junitxml **同格式**, CI / Jenkins / GitHub Actions 都能直接吃)。

为什么报告写到这里而不是 reports/:
  打包是自 app/ 目录整棵拷贝, reports/ 会被一起打进绿色版 —— CI 产物跟 programa
  一起外发不合适。放到工程根的 _reports/ 下, 打包天然看不到。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import html
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]        # v3/
#                                    ↑ scripts → xyfz_auto → app → v3
# 千万别写成 parents[2]: 那是 app/, 报告会被 build_portable 一起打进绿色版
# (打包是按 app/ 整棵拷贝的), CI 产物白白跟着外发。
SCRIPTS = Path(__file__).resolve().parent
REPORT = ROOT / "_reports" / "junit.xml"

# 2026-10-19 v5.2.0: 每个 test_*.py 都是一个独立的 python.exe 子进程。
# 用户曾反馈"运行时会闪一堆黑色命令行窗口" —— 排查确认主程序所有 adb 调用都带了
# CREATE_NO_WINDOW(实测 0 个可见窗口), 唯独**本测试运行器**没带: 从没有控制台的
# 父进程(如 pythonw / 后台 shell)拉起来时, Windows 会给每个子进程新分配一个控制台,
# 28 个用例 = 28 个一闪而过的黑窗, 观感上就是"弹了几十次"。
# 加这个标志后子进程完全不创建控制台, 与主程序口径一致。
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 依赖真实模拟器/adb 的脚本: 没设备时它会在 stdout 里说"需要真机/截图失败"。
# 直判 FAIL 会让人误以为改坏了 —— 标 skipped 才诚实("这次没条件跑")。
SKIP_HINTS = ("需要真机", "截图失败", "无设备", "未连接", "no device",
              "device not found", "请打开模拟器")


def discover(patterns=None, excludes=()):
    files = sorted(SCRIPTS.glob("test_*.py"))
    if patterns:
        sel = []
        for pat in patterns:
            sel += [f for f in files if fnmatch.fnmatch(f.name, pat)]
        files = sorted(set(sel))
    if excludes:
        files = [f for f in files
                 if not any(fnmatch.fnmatch(f.name, e.strip()) for e in excludes)]
    return files


def classify(rc: int, out: str) -> str:
    """退出码 + 输出 → passed / skipped / failed"""
    if rc == 0:
        return "passed"
    low = out.lower()
    if any(h.lower() in low for h in SKIP_HINTS):
        return "skipped"
    return "failed"


def tail(out: str, n: int = 40) -> str:
    lines = [l for l in out.splitlines() if l.strip()]
    return "\n".join(lines[-n:])


def run_one(f: Path, timeout: int, verbose: bool = False):
    t0 = time.time()
    try:
        cp = subprocess.run(
            [sys.executable, str(f)], cwd=str(SCRIPTS.parent),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, creationflags=_NO_WINDOW)
        rc, out = cp.returncode, cp.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as e:
        rc, out = 124, (e.stdout or b"").decode("utf-8", "replace") + \
            f"\n[TIMEOUT] 超过 {timeout}s 未结束"
    except Exception as e:                                  # noqa: BLE001
        rc, out = 1, f"[RUNNER] {type(e).__name__}: {e}"
    return classify(rc, out), time.time() - t0, rc, out


def write_junit(results, elapsed):
    n_fail = sum(1 for r in results if r["state"] == "failed")
    n_skip = sum(1 for r in results if r["state"] == "skipped")
    root = ET.Element("testsuites")
    root.set("name", "xyfz_auto")
    root.set("tests", str(len(results)))
    root.set("failures", str(n_fail))
    root.set("errors", "0")
    root.set("time", "%.3f" % elapsed)
    suite = ET.SubElement(root, "testsuite")
    suite.set("name", "scripts")
    suite.set("tests", str(len(results)))
    suite.set("failures", str(n_fail))
    suite.set("skipped", str(n_skip))
    suite.set("time", "%.3f" % elapsed)
    suite.set("timestamp", _dt.datetime.now().isoformat(timespec="seconds"))
    for r in results:
        tc = ET.SubElement(suite, "testcase")
        tc.set("classname", "scripts." + r["name"][:-3])
        tc.set("name", r["name"])
        tc.set("time", "%.3f" % r["sec"])
        # 失败/跳过都留输出尾巴 —— CI 里点开就能看到到底哪一步没过,
        # 不用回本地复现。
        if r["state"] == "failed":
            fe = ET.SubElement(tc, "failure")
            fe.set("message", "exit=%s" % r["rc"])
            fe.text = html.escape(tail(r["out"], 60))
        elif r["state"] == "skipped":
            sk = ET.SubElement(tc, "skipped")
            sk.set("message", "依赖真机/adb, 本机不具备条件")
            sk.text = html.escape(tail(r["out"], 8))
        so = ET.SubElement(tc, "system-out")
        so.text = html.escape(tail(r["out"], 20))
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    tree = ET.ElementTree(root)
    tree.write(str(REPORT), encoding="utf-8", xml_declaration=True)
    return REPORT


def main():
    ap = argparse.ArgumentParser(description="跑 scripts/test_*.py 并出 JUnit 报告")
    ap.add_argument("--only", nargs="*", default=None,
                    help="只跑匹配的文件名/通配符")
    ap.add_argument("--exclude", nargs="*", default=(),
                    help="跳过匹配的文件名/通配符(逗号分隔或空格分隔)")
    ap.add_argument("--timeout", type=int, default=300,
                    help="单个脚本超时秒数(默认 300)")
    ap.add_argument("--no-junit", action="store_true", help="不写 JUnit XML")
    ap.add_argument("--quiet", action="store_true", help="只打汇总")
    args = ap.parse_args()

    files = discover(args.only, args.exclude)
    if not files:
        print("没有匹配的测试脚本")
        return 2

    print("=" * 66)
    print("  鸡驴助手 回归测试   共 %d 个脚本" % len(files))
    print("=" * 66)

    t0 = time.time()
    results = []
    for f in files:
        state, sec, rc, out = run_one(f, args.timeout)
        results.append({"name": f.name, "state": state, "sec": sec,
                        "rc": rc, "out": out})
        mark = {"passed": "✔", "skipped": "○", "failed": "✘"}[state]
        note = {True: "", False: "  exit=%s" % rc}[rc == 0]
        if state == "skipped":
            note = "  (需要真机)"
        print("  %s %-30s %6.1fs%s" % (mark, f.name, sec, note))
        if state == "failed" and not args.quiet:
            for l in tail(out, 12).splitlines():
                print("        | " + l)
    elapsed = time.time() - t0

    n_p = sum(1 for r in results if r["state"] == "passed")
    n_s = sum(1 for r in results if r["state"] == "skipped")
    n_f = sum(1 for r in results if r["state"] == "failed")

    print("-" * 66)
    print("  通过 %d   跳过 %d   失败 %d      总耗时 %.1fs"
          % (n_p, n_s, n_f, elapsed))
    if n_f:
        print("  失败清单: " + ", ".join(r["name"] for r in results
                                     if r["state"] == "failed"))
    if not args.no_junit:
        p = write_junit(results, elapsed)
        print("  JUnit XML: %s" % p)
    print("=" * 66)
    return 1 if n_f else 0


if __name__ == "__main__":
    sys.exit(main())
