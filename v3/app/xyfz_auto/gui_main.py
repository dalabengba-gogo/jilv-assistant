# -*- coding: utf-8 -*-
"""鸡驴助手 GUI 启动入口

用法:
  python gui_main.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 2026-10-16 崩溃诊断: pythonw 丢弃 stderr, 若无 faulthandler/excepthook,
# 原生崩溃或主线程未捕获异常会"静默消失"(本次 v3 闪退即此)。必须在 import 任何
# 可能崩溃的模块之前启用, 这样下一步的真正崩溃才能被抓到 logs/crash.log。
try:
    import crash_guard
    crash_guard.install_crash_guard()
    # 2026-10-19 v5.2.0: 每 30 秒落一份"全线程 + 模块表"快照。
    # 原生崩溃(access violation)时 faulthandler 只有一个 <no Python frame>,
    # 唯一能反推"那会儿在跑什么"的就是崩溃**前**最后一份快照 + 面包屑。
    crash_guard.start_thread_watchdog(interval=30.0)
except Exception:
    pass

from gui import run_gui

if __name__ == "__main__":
    run_gui()
