# -*- coding: utf-8 -*-
"""通过 Windows 鼠标事件向 MuMu 窗口注入点击 (绕过 adb input 反注入)"""
import ctypes, os, subprocess, sys, time
from ctypes import wintypes
import numpy as np, cv2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from common import find_adb, ADB_NOT_FOUND_MSG   # noqa: E402

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()

HWND = 0x560ec8
# 不再写死 C 盘路径: 自动查找 adb
ADB = find_adb() or sys.exit(ADB_NOT_FOUND_MSG)
SERIAL = "127.0.0.1:16384"

# 校准参数(模板匹配两模板联合求解): 屏幕 = 客户区原点 + 安卓坐标 * scale + 偏移
CLIENT_ORIGIN = (14, 18)   # GetClientRect+ClientToScreen 实测
SCALE_X = 0.6772
SCALE_Y = 0.6768
Y_OFFSET = 39.3            # MuMu 自绘标题栏占客户区顶部


def ax2screen(ax, ay):
    sx = CLIENT_ORIGIN[0] + ax * SCALE_X
    sy = CLIENT_ORIGIN[1] + Y_OFFSET + ay * SCALE_Y
    return int(round(sx)), int(round(sy))


def click_android(ax, ay, hold_ms=60):
    """把 MuMu 置前后, 在安卓坐标 (ax,ay) 处模拟鼠标点击"""
    sx, sy = ax2screen(ax, ay)
    user32.SetForegroundWindow(HWND)
    time.sleep(0.35)
    user32.SetCursorPos(sx, sy)
    time.sleep(0.12)
    user32.mouse_event(0x0002, 0, 0, 0, 0)   # LEFTDOWN
    time.sleep(hold_ms / 1000.0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)   # LEFTUP
    return sx, sy


def screencap(tag):
    raw = subprocess.run([ADB, "-s", SERIAL, "exec-out", "screencap", "-p"],
                         capture_output=True).stdout
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    p = rf"C:/Users/Desktop/WorkBuddy/Claw/tmp_dbg/{tag}.png"
    cv2.imwrite(p, img)
    return p


if __name__ == "__main__":
    import sys
    ax, ay = int(sys.argv[1]), int(sys.argv[2])
    sx, sy = click_android(ax, ay)
    print(f"安卓({ax},{ay}) -> 屏幕({sx},{sy}) 已点击")
