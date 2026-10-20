# -*- coding: utf-8 -*-
"""渲染性能回归 (2026-10-19 v4.9.0 P1-2) —— offscreen Qt, 不连模拟器

背景: 5 个号同时出帧时, 主线程 10~20% 在纯像素搬运。四个改法各有一条断言:
  1. `bgr_to_qimage` 用 Format_BGR888, 不再翻通道(少一遍全幅拷贝), 且颜色不变
  2. 缩略图按 (port, frame_seq) 去重 + 限到 ~3fps
  3. 一帧只转一次码: 缩略图与大画面共用同一次转码结果
  4. 不在「实时画面」页签时完全不转码, 切回来补一帧
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, os.getcwd())

import time

import numpy as np
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


print("[1] bgr_to_qimage: 不翻通道、颜色正确")
from gui.screen_view import bgr_to_qimage

img = np.zeros((12, 20, 3), np.uint8)
img[:, :, 0] = 255          # BGR 的 B 通道
q = bgr_to_qimage(img)
check(isinstance(q, QImage) and q.width() == 20 and q.height() == 12, "尺寸正确")
check(q.format() == QImage.Format_BGR888,
      f"格式是 BGR888(不再做 cvtColor 翻转), 实测 {q.format()}")
c = q.pixelColor(5, 5)
check((c.red(), c.green(), c.blue()) == (0, 0, 255),
      f"BGR→RGB 颜色没有交换, 实测 {(c.red(), c.green(), c.blue())}")
gray = bgr_to_qimage(np.full((8, 8), 77, np.uint8))
check(gray.format() == QImage.Format_Grayscale8 and gray.pixelColor(1, 1).red() == 77,
      "灰度帧也支持")
try:
    bgr_to_qimage(None)
    check(False, "空帧应当报错")
except ValueError:
    check(True, "空帧明确报错(不是静默返回空图)")

print("\n[2] 转码成本: 新实现 vs 旧实现(翻通道 + 双拷贝)")
frame = np.random.randint(0, 255, (960, 540, 3), np.uint8)


def old_impl(bgr):
    rgb = np.ascontiguousarray(bgr[:, :, ::-1])
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()


def bench(fn, n=30):
    fn(frame)
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn(frame)
        ts.append(time.perf_counter() - t0)
    ts.sort()
    return ts[len(ts) // 2] * 1000


t_old, t_new = bench(old_impl), bench(bgr_to_qimage)
print(f"   旧: {t_old:.2f} ms/帧   新: {t_new:.2f} ms/帧   提速 {t_old / t_new:.1f}x")
check(t_new < t_old, f"新实现更快({t_new:.2f}ms < {t_old:.2f}ms)")

print("\n[3] 缩略图: 同帧去重 + 限频")
from gui.live_panel import THUMB_MIN_INTERVAL, ThumbCard

card = ThumbCard(16384)
frame_a = np.zeros((960, 540, 3), np.uint8)
frame_b = np.full((960, 540, 3), 200, np.uint8)
card.set_frame(frame_a, seq=1)
check(card.lbl_img.pixmap() is not None, "第一帧画上去了")
key1 = card.lbl_img.pixmap().cacheKey()
card.set_frame(frame_b, seq=1)
check(card.lbl_img.pixmap().cacheKey() == key1, "同一个 seq 不重复画(去重)")
card.set_frame(frame_b, seq=2)
check(card.lbl_img.pixmap().cacheKey() == key1, "间隔不足 ~3fps 的下一帧被跳过(限频)")
card._drawn_at -= (THUMB_MIN_INTERVAL + 0.01)
check(card.wants_frame(2), "过了间隔就愿意画下一帧")
card.set_frame(frame_b, seq=2)
check(card.lbl_img.pixmap().cacheKey() != key1, "过间隔后新帧真的画上去了")

print("\n[4] 一帧只转一次码(缩略图 + 大画面共用)")
import gui.live_panel as lp

real_convert = lp.bgr_to_qimage
calls = []


def counting(bgr):
    calls.append(1)
    return real_convert(bgr)


lp.bgr_to_qimage = counting
page = lp.AccountScreenPage()
from gui.live_store import ChannelStore
store = ChannelStore()
store.ensure(16384, name="账号1", source="child")
store.ensure(16416, name="账号2", source="child")
page.refresh(store.channels())
page.select_port(16384)
calls.clear()
page.set_frame(16384, frame_a, seq=1)
check(len(calls) == 1, f"当前账号的一帧只转 1 次码(实测 {len(calls)} 次)")
check(page.view._pixmap is not None, "大画面拿到了这一帧")
check(page._thumbs[16384].lbl_img.pixmap() is not None, "缩略图也拿到了这一帧")
calls.clear()
page._thumbs[16384]._drawn_at -= (THUMB_MIN_INTERVAL + 0.01)
page.set_frame(16384, frame_b, seq=2)
check(len(calls) == 1, "下一帧仍只转 1 次")
lp.bgr_to_qimage = real_convert

print("\n[5] 不在「实时画面」页签就不转码; 切回来补一帧")
from gui.main_window import MainWindow
from scripts._gui_stub import no_disclaimer, sync_background

no_disclaimer(MainWindow)
sync_background(MainWindow)
w = MainWindow()
port = w._local_port
lp.bgr_to_qimage = counting

w.tabs.setCurrentIndex(0)
w.store.set_frame(port, frame_a)
calls.clear()
w._render_tick()
check(len(calls) == 1, f"画面页可见 → 该帧转 1 次码(实测 {len(calls)})")

w.tabs.setCurrentIndex(1)          # 实时步骤
w.store.set_frame(port, frame_b)
w._frame_drawn.clear()             # 逼它必须重新判断(否则不去画也不转码)
calls.clear()
w._render_tick()
check(len(calls) == 0, f"不在画面页 → 完全不转码(实测 {len(calls)})")

calls.clear()
w.tabs.setCurrentIndex(0)          # 触发 currentChanged → 补一帧
check(len(calls) >= 1, f"切回画面页立刻补一帧(实测 {len(calls)} 次转码)")
check(w.page_screen.view._pixmap is not None, "大画面确实更新了(不是空白)")
lp.bgr_to_qimage = real_convert

print("\n[6] 状态灯 tooltip 不再每 tick 无条件重写")
import gui.lights_mixin as lm
src = open("gui/lights_mixin.py", encoding="utf-8").read()
i = src.find("def _refresh_port_states")
seg = src[i:i + 900]
check("if lbl.toolTip() != tip" in seg, "tooltip 先比对再写")
check("if lbl.styleSheet() != css" in seg, "样式表先比对再写")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
