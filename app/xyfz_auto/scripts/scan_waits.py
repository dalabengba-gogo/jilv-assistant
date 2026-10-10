# -*- coding: utf-8 -*-
"""扫描任务脚本里的「长等待」，按耗时排序输出。

用途：排查“为什么这一步要等这么久 / 哪些等待是纯粹干等”。
区分两类，处理方式完全不同：

  sleep  —— 固定停顿，无论如何都要睡满。属于纯浪费，可改成“轮询直到条件满足”。
  wait   —— 条件等待，条件一满足立刻返回，只在条件迟迟不出现时才耗满 timeout。
             如果 timeout 很长，说明这一步正常情况下可能真的要等很久（如战斗、
             一条龙跑完），缩短会误判。

用法：
    python scripts/scan_waits.py            # 列出 >10s 的 sleep 和 >60s 的 wait
    python scripts/scan_waits.py 5 30       # 自定义阈值: sleep>5s, wait>30s
"""
from __future__ import annotations

import re
import sys
import pathlib

SLEEP_TH = float(sys.argv[1]) if len(sys.argv) > 1 else 10
WAIT_TH = int(sys.argv[2]) if len(sys.argv) > 2 else 60

ROOT = pathlib.Path(__file__).resolve().parent.parent
TASKS = ROOT / "assets" / "tasks"

RE_NAME = re.compile(r"^\s*-\s*name:\s*(.+?)\s*$")
RE_SLEEP = re.compile(r"^\s*sleep:\s*([0-9.]+)\s*$")
RE_TIMEOUT = re.compile(r"^\s*timeout:\s*([0-9]+)\s*$")


def main():
    rows = []
    for p in sorted(TASKS.rglob("*.yaml")):
        if "tests" in p.parts:
            continue
        cur_name = "(未命名)"
        cur_line = 0
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            m = RE_NAME.match(line)
            if m:
                cur_name, cur_line = m.group(1), i
                continue
            m = RE_SLEEP.match(line)
            if m and float(m.group(1)) > SLEEP_TH:
                rows.append(("sleep", float(m.group(1)), cur_name, p.name, cur_line))
                continue
            m = RE_TIMEOUT.match(line)
            if m and int(m.group(1)) > WAIT_TH:
                rows.append(("timeout", int(m.group(1)), cur_name, p.name, cur_line))

    rows.sort(key=lambda r: -r[1])
    print(f"sleep > {SLEEP_TH:g}s / timeout > {WAIT_TH}s 的步骤（共 {len(rows)} 处）")
    print("-" * 78)
    for kind, sec, name, fname, line in rows:
        tail = f"  ← {fname}:{line - 1}"   # name 行上一行通常是步骤名行
        print(f"{kind:<8}{sec:>7.0f}s  {name}{tail}")
    print("-" * 78)
    sleep_total = sum(s for k, s, *_ in rows if k == "sleep")
    print(f"固定停顿合计 {sleep_total:.0f}s（这部分是可以优化掉的纯干等）")


if __name__ == "__main__":
    main()
