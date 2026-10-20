# -*- coding: utf-8 -*-
"""窗口诊断: 列出所有顶层窗口, 找出 MuMu 候选, 生成 config.yaml 片段

用途: 换机器后 via:win 步骤报「未找到 MuMu 窗口」时, 在目标机器上跑这个:

    <包内 python> scripts/list_windows.py

输出里标着「← MuMu候选」的那行就是答案:
  · 把它的 hwnd 填进 config.yaml → win_input.hwnd
  · 或把标题里的特征词填进 win_input.title_contains

加 --json 可输出结构化结果(供脚本消费)。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from device.win_input import (          # noqa: E402
    DEFAULT_EXE_KEYWORDS, DEFAULT_TITLE_KEYWORDS, LEGACY_TITLE,
    diagnose_text, list_windows, resolve_window,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="MuMu 窗口诊断 (win 输入通道)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--all", action="store_true", help="包含不可见窗口")
    args = ap.parse_args()

    wins = list_windows(only_visible=not args.all)
    if args.json:
        print(json.dumps({
            "windows": wins,
            "title_keywords": list(DEFAULT_TITLE_KEYWORDS),
            "exe_keywords": list(DEFAULT_EXE_KEYWORDS),
        }, ensure_ascii=False, indent=2))
        return 0

    print("=" * 72)
    print(diagnose_text())
    print("=" * 72)
    print(f"标题关键字: {list(DEFAULT_TITLE_KEYWORDS)}")
    print(f"进程关键字: {list(DEFAULT_EXE_KEYWORDS)}")
    print(f"旧版写死的标题(仅作精确优先): {LEGACY_TITLE}")
    print("-" * 72)
    try:
        hwnd, info = resolve_window({})
        print(f"[结果] 找到 MuMu 窗口: hwnd=0x{hwnd:X} 标题「{info.get('title')}」"
              f" 进程={info.get('exe_name')} 位置={info.get('rect')}")
        print("[建议] 若点击位置有偏差, 用 scripts/calib_win.py 重新校准 scale/offset。")
        print("[可粘贴] config.yaml 片段:")
        print(f"win_input:\n  hwnd: 0   # 留 0 自动查找; 多开想固定某个窗口就填 {hwnd}")
    except Exception as e:
        print(f"[结果] 未找到 MuMu 窗口\n{e}")
        print("-" * 72)
        print("[怎么修] 在上面清单里找到 MuMu 那一行, 二选一填入 config.yaml:")
        print("  win_input:\n    hwnd: 0x<该行 hwnd 的十进制或十六进制>")
        print("  win_input:\n    title_contains: [\"<标题里的特征词>\"]")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
