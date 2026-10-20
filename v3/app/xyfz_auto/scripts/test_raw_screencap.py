# -*- coding: utf-8 -*-
"""RAW 抓屏解析的确定性测试(不依赖模拟器)

2026-10-19 v5.5.0: 截图是一轮日常里第二大的开销(实测 417 次 / 6 分钟)。
`adb exec-out screencap`(RAW) 实测 359ms, 而 `-p`(PNG) 要 684ms ——
慢的是**设备端 PNG 编码**, 不是传输。所以改成优先拿 RAW。

风险全在解析上: 头部长度写错一个字段, 整屏像素就整体平移 1 个像素
(实测踩过: 与 PNG 比对时 dx 恒为 +1.00), 画面肉眼看不出, 但固定坐标点击
和模板匹配都会悄悄偏。所以这里把解析单独测死:
  [1] 16 字节头(MuMu 实测): 宽/高/格式/colorspace
  [2] 12 字节头(老设备): 宽/高/格式
  [3] 通道顺序 RGB(A) → BGR
  [4] 认不出来的数据必须返回 None(交给上层退回 PNG), 不许瞎猜
"""
import os
import struct
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

from device.adb_client import decode_screencap_raw      # noqa: E402

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


# 2x2 的像素, 用 (R,G,B,A) 表示
PIX = [(1, 2, 3, 255), (4, 5, 6, 255), (7, 8, 9, 255), (10, 11, 12, 255)]
BODY = b"".join(bytes(p) for p in PIX)
EXPECT = [[[3, 2, 1], [6, 5, 4]], [[9, 8, 7], [12, 11, 10]]]   # BGR

print("[1] 16 字节头(MuMu: 宽/高/格式/colorspace)")
img = decode_screencap_raw(struct.pack("<IIII", 2, 2, 1, 1) + BODY)
check(img is not None and img.shape == (2, 2, 3), "解析出 2x2x3")
check(img.tolist() == EXPECT, f"像素与坐标完全对应 (实际 {img.tolist() if img is not None else None})")

print("\n[2] 12 字节头(老设备)")
img12 = decode_screencap_raw(struct.pack("<III", 2, 2, 1) + BODY)
check(img12 is not None and img12.tolist() == EXPECT, "12 字节头同样解析正确(头长度按总长反推)")

print("\n[3] 通道顺序")
check(img[0, 0].tolist() == [3, 2, 1], "RGBA(1,2,3,255) → BGR(3,2,1)")

print("\n[4] 认不出来就返回 None(上层退回 PNG, 不瞎猜)")
check(decode_screencap_raw(b"") is None, "空数据")
check(decode_screencap_raw(b"xy") is None, "太短的数据")
check(decode_screencap_raw(struct.pack("<III", 2, 2, 1) + BODY[:-4]) is None,
      "像素数据缺一截(头部长度反推不出来)")
check(decode_screencap_raw(struct.pack("<III", 2, 2, 9) + BODY) is None,
      "未知像素格式(format=9)")

# 关键回归: 上一次的 bug 是"写死 12 字节头", 导致整屏右移 1 像素。
# 用 16 字节头的真实结构造一张 4x1 的图: 首像素必须是 (0,0), 不能是第 2 个像素。
row = struct.pack("<IIII", 4, 1, 1, 1) + bytes([10, 20, 30, 255]) + bytes([40, 50, 60, 255]) * 3
img4 = decode_screencap_raw(row)
check(img4 is not None and img4[0, 0].tolist() == [30, 20, 10],
      "首像素没有整体位移(写死 12 字节头时这里会读到第 2 个像素)")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
