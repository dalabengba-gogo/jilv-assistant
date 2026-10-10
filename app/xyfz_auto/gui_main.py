# -*- coding: utf-8 -*-
"""鸡驴助手 GUI 启动入口

用法:
  python gui_main.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui import run_gui

if __name__ == "__main__":
    run_gui()
