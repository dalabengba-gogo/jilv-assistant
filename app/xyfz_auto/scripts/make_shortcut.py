# -*- coding: utf-8 -*-
"""创建桌面快捷方式(鸡驴助手.lnk) —— 2026-09-14 一次性工具
依赖 pywin32(已装入 venv)。用法: venv python scripts/make_shortcut.py
"""
import os
import sys

import win32com.client

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHONW = r"C:\Users\Desktop\.workbuddy\binaries\python\envs\default\Scripts\pythonw.exe"


def main():
    ws = win32com.client.Dispatch("WScript.Shell")
    desk = ws.SpecialFolders("Desktop")
    lnk = os.path.join(desk, "鸡驴助手.lnk")
    sc = ws.CreateShortcut(lnk)
    sc.TargetPath = PYTHONW
    sc.Arguments = "gui_main.py"
    sc.WorkingDirectory = ROOT
    sc.IconLocation = os.path.join(ROOT, "assets", "app_icon.ico")
    sc.Description = "大话西游日常任务助手 (xyfz_auto)"
    sc.WindowStyle = 7  # 最小化, 避免闪控制台(pythonw 本无控制台, 保险)
    sc.Save()
    print("created:", os.path.exists(lnk), lnk)


if __name__ == "__main__":
    main()
