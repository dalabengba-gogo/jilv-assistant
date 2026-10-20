# -*- coding: utf-8 -*-
"""校准 Windows 鼠标通道的坐标映射 (scripts/calib_win.py)

用法: python scripts/calib_win.py
原理: adb 截图取几块静态 UI 模板, 在 MuMu 窗口屏幕截图中多尺度搜索,
      联合求解 scale 与 y 偏移, 打印可直接写进 WinMouse 的参数。
"""
import ctypes, subprocess, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import numpy as np
import cv2
from PIL import ImageGrab

from device.win_input import WinMouse, resolve_window
from common import find_adb, ADB_NOT_FOUND_MSG

# 不再写死 C 盘路径: 自动在 配置/包内自带/PATH/MuMu常见位置 之间查找
ADB = find_adb() or sys.exit(ADB_NOT_FOUND_MSG)
SERIAL = os.environ.get("XYFZ_ADB_SERIAL", "127.0.0.1:16384")

# 2026-09-28: 改用 resolve_window —— 模糊匹配 + 找不到时打印窗口清单
try:
    _hwnd, _info = resolve_window({"index": int(os.environ.get("XYFZ_WIN_INDEX", "0"))})
except Exception as e:
    sys.exit(f"未找到 MuMu 窗口:\n{e}")
win = WinMouse(hwnd=_hwnd)
print(f"目标窗口: hwnd=0x{_hwnd:X} 标题「{_info.get('title')}」进程={_info.get('exe_name')}")
ox, oy = win.client_origin()

user32 = ctypes.windll.user32
crect = ctypes.wintypes.RECT()
user32.GetClientRect(win.hwnd, ctypes.byref(crect))
w, h = crect.right, crect.bottom
shot = np.array(ImageGrab.grab(bbox=(ox, oy, ox + w, oy + h), all_screens=True).convert("RGB"))
win_img = cv2.cvtColor(shot, cv2.COLOR_RGB2GRAY)
print(f"窗口客户区 {w}x{h} @ 屏幕({ox},{oy})")

raw = subprocess.run([ADB, "-s", SERIAL, "exec-out", "screencap", "-p"],
                     capture_output=True).stdout
ad = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)

# 静态 UI 模板 (名称, x, y, w, h) 安卓坐标 —— 选低动画区域
TPLS = [
    ("底菜单·角色", 176, 895, 64, 60),
    ("左栏·商城", 18, 168, 52, 46),
]
results = []
for name, tx, ty, tw, th in TPLS:
    tpl0 = ad[ty:ty + th, tx:tx + tw]
    best = None
    for s in np.arange(0.60, 0.80, 0.005):
        t = cv2.resize(tpl0, None, fx=s, fy=s)
        r = cv2.matchTemplate(win_img, t, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if best is None or mx > best[0]:
            best = (mx, s, loc)
    conf, s, (lx, ly) = best
    results.append((name, tx, ty, lx, ly, s, conf))
    print(f"{name}: scale={s:.3f} conf={conf:.3f} 安卓({tx},{ty})→客户区({lx},{ly})")

# 联合求解: 至少两个点才能算 scale+offset
if len(results) >= 2:
    (n1, ax1, ay1, wx1, wy1, _, _), (n2, ax2, ay2, wx2, wy2, _, _) = results[:2]
    if abs(ax1 - ax2) > 1e-6 and abs(ay1 - ay2) > 1e-6:
        sx = (wx1 - wx2) / (ax1 - ax2)
        sy = (wy1 - wy2) / (ay1 - ay2)
        x_off = wx1 - ax1 * sx
        y_off = wy1 - ay1 * sy
        print("\n===== 校准结果 =====")
        print(f"scale_x={sx:.4f}  scale_y={sy:.4f}")
        print(f"x_offset={x_off:.1f}  y_offset={y_off:.1f}  (x_offset 应≈0)")
        print(f"\nWinMouse(hwnd=wins[0], scale_x={sx:.4f}, scale_y={sy:.4f}, y_offset={y_off:.1f})")
    else:
        print("两点重合, 无法求解")
