# -*- coding: utf-8 -*-
"""残留进程清理 —— 让便携版文件夹能顺利删除

什么时候用:
    关闭程序后(尤其点了右上角 X 只是最小化到托盘的情况)删除便携版文件夹,
    提示"文件正在被另一个程序使用""需要管理员权限", 就在包根目录双击:

        清理残留进程.bat

它会: 结束所有【从本包目录启动的】python/pythonw/adb 进程 → 收回本包 adb server
      → 清掉设备互斥锁文件; 然后可以放心删除整个文件夹。

安全说明: 只处理可执行文件路径位于本包目录之下的进程, 不会影响系统的
          Python、MuMu 自带的 adb、或其它程序。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent          # .../app/xyfz_auto
# 便携版布局: <包根>/app/xyfz_auto/{scripts,..} + <包根>/runtime/
CAND_ROOTS = [APP_DIR]
if APP_DIR.name == "xyfz_auto" and APP_DIR.parent.name == "app":
    CAND_ROOTS.append(APP_DIR.parent.parent)              # 包根(含 runtime/)

sys.path.insert(0, str(APP_DIR))

from common import find_adb                              # noqa: E402
from proc_guard import (kill_own_adb_server, kill_pids,   # noqa: E402
                        processes_under, release_all_device_locks)


def main() -> int:
    ap = argparse.ArgumentParser(description="清理本便携版的残留进程")
    ap.add_argument("--yes", "-y", action="store_true", help="不询问直接清理")
    ap.add_argument("--list", action="store_true", help="只列出, 不结束")
    args = ap.parse_args()

    roots = [str(r) for r in CAND_ROOTS]
    print("=" * 66)
    print("鸡驴助手 —— 残留进程清理")
    print("检查范围:")
    for r in roots:
        print(f"  {r}")
    print("=" * 66)

    procs = processes_under(*roots)
    if not procs:
        print("没有发现本程序的残留进程。")
    else:
        print(f"发现 {len(procs)} 个本程序的进程:")
        for p in procs:
            print(f"  PID {p['pid']:6d}  {p['name']:16s} {p['exe']}")
        if args.list:
            print("\n(--list 模式: 未结束任何进程)")
            return 1
        if not args.yes:
            try:
                ans = input("\n结束这些进程? [y/N] ").strip().lower()
            except EOFError:
                ans = "y"
            if ans not in ("y", "yes"):
                print("已取消。")
                return 1
        n = kill_pids([p["pid"] for p in procs])
        print(f"已结束 {n} 个进程。")
        left = processes_under(*roots)
        if left:
            print(f"!! 仍有 {len(left)} 个残留(可能需要管理员权限):")
            for p in left:
                print(f"  PID {p['pid']:6d}  {p['exe']}")
            print("提示: 右键以管理员身份运行「清理残留进程.bat」再试一次。")
            return 2

    # adb server: 只在它确由本包 adb.exe 拉起时才收
    adb = None
    try:
        adb = find_adb()
    except Exception:
        pass
    print(f"\n本包 adb: {adb or '(未找到)'}")
    print("adb server:", kill_own_adb_server(adb))

    n_lock = release_all_device_locks()
    print(f"设备锁: 已清理 {n_lock} 个锁文件")

    print("\n清理完成 —— 现在可以删除整个文件夹了。")
    print("(若仍提示占用: 关掉文件资源管理器里打开的该文件夹窗口, 或重试一次)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
