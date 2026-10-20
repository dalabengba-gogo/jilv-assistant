# -*- coding: utf-8 -*-
"""崩溃诊断能力回归 (2026-10-19 v5.2.0)

覆盖:
  1. note() 面包屑真的落盘、last_actions() 能读回 —— 崩溃后靠它看"死前在干什么";
  2. dump_threads() 写出线程名清单(faulthandler 自己不给线程名);
  3. minidump 钩子安装成功;
  4. **真让子进程触发一次原生异常**, 验证 crash.log 里出现
     「FATAL NATIVE EXCEPTION + 异常码/地址 + 归因到具体 DLL」。

第 4 条是这轮增强的全部意义: 2026-10-19 11:48 那次 access violation 只留下
  "Thread 0x... / <no Python frame>", 连是哪个 DLL 都说不出来, 只能靠猜。
这里必须真崩一次来证明"归因"确实落盘。

⚠️ 关于 .dmp: 在异常上下文里调 MiniDumpWriteDump 会重入失败(实测只得到 0 字节),
   所以 dmp 只是 best-effort, 本测试**不硬要求**它存在; 归因文本才是主判据。

运行: python scripts/test_crash_guard.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOGS = ROOT / "logs"
sys.path.insert(0, str(ROOT))

import crash_guard  # noqa: E402

FAILS: list[str] = []


def check(cond, msg):
    print(("  ✅ " if cond else "  ❌ ") + msg)
    if not cond:
        FAILS.append(msg)


def test_breadcrumb():
    print("[1] 面包屑 note() / last_actions()")
    crash_guard.note("TEST-面包屑-甲")
    crash_guard.note("TEST-面包屑-乙")
    acts = crash_guard.last_actions(5)
    check(any("TEST-面包屑-甲" in a for a in acts), "note 写入了 last_action.log")
    check(any("TEST-面包屑-乙" in a for a in acts), "last_actions 能读回最新一条")
    check(all("[" in a for a in acts[-2:]), "每行带毫秒时间戳")


def test_thread_dump():
    print("[2] 线程快照 dump_threads()")
    ok = crash_guard.dump_threads("unit-test")
    check(ok, "dump_threads 返回成功")
    p = LOGS / "threads.log"
    check(p.exists() and p.stat().st_size > 0, "threads.log 有内容")
    txt = p.read_text(encoding="utf-8", errors="replace")
    check("unit-test" in txt, "快照带 tag")
    check("MainThread" in txt, "线程清单里有线程名(不是只有 0x 地址)")


def test_minidump_hook_installed():
    print("[3] 崩溃钩子安装 + 模块表")
    crash_guard.install_crash_guard()
    check(crash_guard.minidump_enabled(), "异常过滤器已挂上")
    n = crash_guard.refresh_modules()
    check(n > 5, f"模块表拿到 {n} 个模块")
    log = LOGS / "modules.log"
    check(log.exists() and "python" in log.read_text(encoding="utf-8", errors="replace").lower(),
          "modules.log 里有 python 解释器自身")


CHILD = r'''
import sys, ctypes
sys.path.insert(0, r"{root}")
import crash_guard
crash_guard.install_crash_guard()
sys.stdout.write("HOOK=%d\n" % (1 if crash_guard.minidump_enabled() else 0))
sys.stdout.flush()
k32 = ctypes.WinDLL("kernel32")
k32.RaiseException.argtypes = [ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
# 真 SEH 异常(0xC0000005)。这才会走我们的 vectored handler;
# faulthandler._sigsegv() 走的是 CRT 的 raise(), 不产生 SEH, 测不出来。
k32.RaiseException(0xC0000005, 0, 0, None)
sys.stdout.write("SHOULD-NOT-REACH\n")
'''


def test_native_crash_attribution():
    print("[4] 真崩一次: 子进程 access violation → crash.log 归因")
    clog = LOGS / "crash.log"
    s0 = clog.stat().st_size if clog.exists() else 0
    with tempfile.TemporaryDirectory() as td:
        child = Path(td) / "boom.py"
        child.write_text(CHILD.replace("{root}", str(ROOT)), encoding="utf-8")
        cp = subprocess.run([sys.executable, str(child)],
                            capture_output=True, text=True, timeout=120,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    check("HOOK=1" in (cp.stdout or ""), "子进程里钩子安装成功")
    check(cp.returncode != 0, f"子进程异常退出(退出码 {cp.returncode})")

    new = ""
    try:
        with open(clog, "r", encoding="utf-8", errors="replace") as f:
            f.seek(s0)
            new = f.read()
    except Exception as e:  # noqa: BLE001
        check(False, f"读 crash.log 增量失败: {e}")
    check("FATAL NATIVE EXCEPTION" in new, "crash.log 记下 FATAL NATIVE EXCEPTION")
    check("0xC0000005" in new, "记下异常码 0xC0000005")
    check("归因:" in new, "记下『归因』(地址落在哪个模块)")
    if "归因:" in new:
        line = [ln for ln in new.splitlines() if ln.startswith("归因:")][-1]
        print("      " + line)
        check("KERNELBASE" in line.upper() or ".DLL" in line.upper(),
              "归因指向具体 DLL(本次 RaiseException 在 KERNELBASE)")


def main():
    test_breadcrumb()
    test_thread_dump()
    test_minidump_hook_installed()
    test_native_crash_attribution()
    # 清理测试痕迹: 面包屑是"最近干了什么", 留着会干扰真事故复盘
    try:
        (LOGS / "last_action.log").unlink(missing_ok=True)
    except OSError:
        pass
    for p in LOGS.glob("crash_*.dmp"):
        try:
            if p.stat().st_size == 0:
                p.unlink()
        except OSError:
            pass
    print()
    if FAILS:
        print(f"❌ {len(FAILS)} 项失败:")
        for f in FAILS:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
