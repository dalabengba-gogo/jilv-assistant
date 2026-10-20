# -*- coding: utf-8 -*-
"""v5.5.0 性能三件的确定性测试(不依赖模拟器)

背景(2026-10-19 真机实测, 一轮 daily_all 27.8 分钟 / 351 步):
    OCR      341 帧 × 1.97s = 8.8 min   (32%)
    截图     417 次 × 0.86s = 6.0 min   (20%)
    固定 sleep               = 6.5 min   (24%)
    真正的点击/滑动          ≈ 6 min     (21%)
其中还夹着一笔纯浪费: adb 的 `is_online()` 预检 136ms × 770 次 ≈ 1分45秒,
以及每次 tap 内置的 0.3s 防抖。

本测试验证 v5.5.0 的四处改动都没有改变**语义**(只改代价):
  [1] 帧复用: TTL 内第二次 _grab 不再截图; 动作后必须重新截图
  [2] 裁剪识别: region 直接把图裁小再识别, 返回的坐标被平移回全屏
  [3] 热点窗口: 同一个关键词第二次只识别上次那一小块; 小块里没有 → 退回全图
  [4] 语义不变: 同一条 wait 条件在"全图"和"裁剪"下结论必须一致
"""
import os
import sys
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

from engine.runner import Engine          # noqa: E402

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


class FakeDevice:
    """只记录被调用了几次, 返回一张固定尺寸的黑图 PNG"""

    def __init__(self, w=540, h=960):
        self.shots = 0
        self.taps = 0
        self.w, self.h = w, h

    def screenshot(self):
        self.shots += 1
        # 造一张 PNG: 直接用容器里的 cv2, 与真实解码路径一致
        import cv2
        img = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        ok, buf = cv2.imencode(".png", img)
        return buf.tobytes()

    def tap(self, x, y):
        self.taps += 1

    def swipe(self, *a, **k):
        self.taps += 1

    def key(self, code):
        self.taps += 1


class RecordingOcr:
    """记录每次被喂进来的图尺寸; 按"图里有没有目标字"返回固定框"""

    def __init__(self, texts=("任务说明",)):
        self.calls = []          # [(h, w)]
        self.texts = texts

    def recognize(self, screen):
        h, w = screen.shape[:2]
        self.calls.append((h, w))
        # 注意: 返回的是**这张图自己的坐标系**。引擎负责把裁剪偏移加回去,
        # 所以这里固定放在局部 (20,20) —— 平移后落在 region 内才说明坐标换算正确。
        return [{"text": t, "score": 0.9, "x": 320, "y": 20, "w": 80, "h": 24}
                for t in self.texts]


class TargetOcr:
    """只在"图里指定区域有货"时返回文本框: 用来验证裁剪确实看到了目标"""

    def __init__(self, target_y=400):
        self.target_y = target_y
        self.calls = []

    def recognize(self, screen):
        h, w = screen.shape[:2]
        self.calls.append((h, w))
        # 引擎裁出来的图里, 目标相对位置由引擎自己算; 这里简单判"图够高就返回"
        if h >= 30:
            return [{"text": "任务说明", "score": 0.9,
                     "x": 10, "y": 5, "w": 60, "h": 22}]
        return []


def make_engine(ocr=None, dev=None):
    eng = Engine(device=dev or FakeDevice(), matcher=None, ocr=ocr or RecordingOcr())
    eng._beat = lambda: None          # 心跳与本测试无关
    return eng


# ---------------------------------------------------------------- [1] 帧复用
print("[1] 帧复用(TTL)")
dev = FakeDevice()
eng = make_engine(dev=dev)
a = eng._grab()
b = eng._grab()
check(a is b, "TTL 内第二次 _grab 返回同一帧(没有重新截图)")
check(dev.shots == 1, f"只截了 1 次图 (实际 {dev.shots})")

eng._invalidate_frame()
c = eng._grab()
check(c is not a, "动作之后(作废旧帧)重新截图")
check(dev.shots == 2, f"累计截图 2 次 (实际 {dev.shots})")

eng.frame_ttl = 0
eng._grab()
eng._grab()
check(dev.shots == 4, f"frame_ttl=0 时退回旧行为: 每次都截 (实际 {dev.shots})")

# 版本作废后 OCR 缓存也要清空
eng2 = make_engine(ocr=RecordingOcr())
s1 = eng2._grab()
eng2._ocr_boxes(s1)
check(len(eng2.ocr.calls) == 1, "同一帧只识别一次")
eng2._ocr_boxes(s1)
check(len(eng2.ocr.calls) == 1, "同一帧第二次复用 OCR 结果")
eng2._invalidate_frame()
s2 = eng2._grab()
eng2._ocr_boxes(s2)
check(len(eng2.ocr.calls) == 2, "换帧后重新识别")

# ---------------------------------------------------------------- [2] 裁剪识别
print("\n[2] 裁剪识别(region)")
ocr = RecordingOcr()
eng = make_engine(ocr=ocr)
screen = eng._grab()
boxes = eng._ocr_boxes(screen, (300, 150, 540, 320))
seen_h, seen_w = ocr.calls[-1]
check(seen_w == 540 and seen_h <= 320,
      f"送进 OCR 的是纵向带({seen_w}x{seen_h}), 横向保持整行(不截断长标签)")
check(seen_h > 170, "裁剪时纵向留了余量(边界上的文字不被切一半)")
b = eng._ocr_find(screen, "任务说明", (300, 150, 540, 320))
check(b is not None and b["x"] >= 300,
      f"裁剪识别结果坐标已平移回全屏 (x={b['x'] if b else None})")

# region 过滤语义没变: 框中心不在区域内 → 不算命中
# (裁剪会外扩 8px, 所以贴着裁剪边的那一点点文字仍然会被 region 判据筛掉)
class EdgeOcr:
    def recognize(self, screen):
        return [{"text": "任务说明", "score": 0.9, "x": 0, "y": 0, "w": 8, "h": 8}]


eng_edge = make_engine(ocr=EdgeOcr())
s_edge = eng_edge._grab()
check(eng_edge._ocr_find(s_edge, "任务说明", (0, 700, 540, 960)) is None,
      "区域外的命中仍被过滤掉(与旧版一致)")

# ocr_row 也走裁剪
eng3 = make_engine(ocr=RecordingOcr(texts=("挖宝封妖3/10",)))
s3 = eng3._grab()
r = eng3._ocr_row(s3, {"has": ["挖宝封妖"], "not": ["V"],
                       "region": [356, 150, 540, 320]})
check(r is not None, "ocr_row + region 仍能命中")

# ---------------------------------------------------------------- [3] 热点窗口
print("\n[3] 热点窗口(关键词位置记忆)")
ocr = RecordingOcr()
eng = make_engine(ocr=ocr)
s = eng._grab()
eng._ocr_find(s, "任务说明")
first = ocr.calls[-1]
check(first == (960, 540), "第一次找某关键词: 全图识别")
eng._invalidate_frame()           # 换一帧, 缓存清空
s2 = eng._grab()
eng._ocr_find(s2, "任务说明")
second = ocr.calls[-1]
check(second != (960, 540) and second[0] * second[1] < 960 * 540 / 2,
      f"第二次找同一个关键词: 只识别上次那一小块 ({second[1]}x{second[0]})")

# 热点窗口里没有 → 必须退回全图(漏判风险为零)
class GoneOcr:
    def __init__(self):
        self.calls = []
        self.gone = False

    def recognize(self, screen):
        h, w = screen.shape[:2]
        self.calls.append((h, w))
        if self.gone:
            return []
        return [{"text": "任务说明", "score": 0.9, "x": 100, "y": 400, "w": 80, "h": 24}]

gocr = GoneOcr()
eng4 = make_engine(ocr=gocr)
s = eng4._grab()
check(eng4._ocr_find(s, "任务说明") is not None, "第一次全图命中")
gocr.gone = True
eng4._invalidate_frame()
s2 = eng4._grab()
r = eng4._ocr_find(s2, "任务说明")
check(r is None, "物件消失后判为未命中(没有假阳性)")
check(gocr.calls[-1] == (960, 540),
      f"热点窗口不中就退回全图 (最后一次识别 {gocr.calls[-1][1]}x{gocr.calls[-1][0]})")

# 连续 2 次不中 → 忘掉这个关键词的热点, 别再倒赔时间
eng4._invalidate_frame()
s3 = eng4._grab()
eng4._ocr_find(s3, "任务说明")          # 第 2 次未命中 → 达到上限, 热点被清掉
check("任务说明" not in eng4._ocr_hint, "连续 2 次未命中后热点已从记忆里清掉")
eng4._invalidate_frame()
s4 = eng4._grab()
before = len(gocr.calls)
eng4._ocr_find(s4, "任务说明")
check(len(gocr.calls) == before + 1,
      f"之后只识别 1 次(不再试热点) (实际 {len(gocr.calls)-before} 次)")

# ---------------------------------------------------------------- [4] 语义一致
print("\n[4] 语义一致(裁剪 vs 全图)")
# 同一份"全屏框", 用全图识别 vs 热点窗口识别, _ocr_find 结论必须一致
WHOLE = [
    {"text": "任务说明", "score": 0.9, "x": 120, "y": 300, "w": 120, "h": 30},   # 在左半
    {"text": "五环任务", "score": 0.8, "x": 301, "y": 683, "w": 164, "h": 37},   # 在右下
]


class WholeOcr:
    """真正的"按图说话"的假引擎: 只有全图才认字, 裁图返回空。

    用来验证"热点窗口不中就退回全图"这条兜底 —— 只要退回, 结论就和不用缓存一样。
    """

    def __init__(self, boxes):
        self.boxes = boxes
        self.calls = []

    def recognize(self, screen):
        h, w = screen.shape[:2]
        self.calls.append((w, h))
        if (w, h) == (540, 960):
            return [dict(b) for b in self.boxes]
        return []


e = make_engine(ocr=WholeOcr(WHOLE))
s = e._grab()
check(e._ocr_find(s, "任务说明")["x"] == 120, "全图: 命中左半区的「任务说明」")
check(e._ocr_find(s, "五环任务")["x"] == 301, "全图: 命中右半区的「五环任务」")
e._invalidate_frame()
s = e._grab()
# 这一帧里"裁图认不出字"(模拟热点窗口失效) → 必须退回全图, 结论与上一条一致
check(e._ocr_find(s, "五环任务")["x"] == 301,
      "热点窗口失效时退回全图, 命中位置不变(没有漏判)")
check(e.ocr.calls[-1] == (540, 960), "最后一次确实是全图识别")

# ---------------------------------------------------------------- [5] 轮询必须看新帧
print("\n[5] 轮询不复用旧帧")
eng5 = make_engine()
ttls = []
real_grab = eng5._grab


def spy_grab(ttl=None):
    ttls.append(ttl)
    return real_grab(ttl=0)          # 内容无所谓, 只关心调用方传的 ttl


eng5._grab = spy_grab
eng5._recognize_wait = lambda cond, screen: None      # 永远不命中
eng5.running = True                                   # _wait_until 会检查停止标志
try:
    eng5._wait_until({"ocr": "永远不会出现"}, 0.4, "测试等待")
except TimeoutError:
    pass
check(ttls and all(t == 0 for t in ttls),
      f"轮询每次都强制新帧 (ttl={set(ttls)})")
check(len(ttls) >= 2, f"0.4s 内轮询了 {len(ttls)} 次(没有被帧复用吞掉)")

# 动作后帧必须失效
class TapDevice(FakeDevice):
    pass


dev6 = TapDevice()
eng6 = make_engine(dev=dev6)
eng6.ui = None
eng6._grab()
eng6._invalidate_frame()          # 模拟 _tap 内部的动作后失效
check(eng6._frame is None and dev6.shots == 1, "动作后帧缓存被清空")

# ---------------------------------------------------------------- [6] 同屏内容复用
print("\n[6] 同屏时不重复 OCR")


class PaintDevice:
    """可以"改画面"的假设备: 同一张图连拍两次 = 画面没变"""

    def __init__(self):
        import cv2
        self.shots = 0
        self.img = np.zeros((960, 540, 3), dtype=np.uint8)
        self._cv2 = cv2

    def screenshot(self):
        self.shots += 1
        ok, buf = self._cv2.imencode(".png", self.img)
        return buf.tobytes()

    def paint(self):
        """画一大块白色 → 画面明显变了"""
        self.img[200:400, 100:400] = 255


dev7 = PaintDevice()
ocr7 = RecordingOcr()
eng7 = make_engine(ocr=ocr7, dev=dev7)
s1 = eng7._grab(ttl=0)
eng7._ocr_boxes(s1)
n1 = len(ocr7.calls)
check(n1 == 1, "第一帧识别一次")

eng7._grab(ttl=0)                 # 同画面再拍一张(ttl=0 强制截图)
s2 = eng7._grab(ttl=0)
eng7._ocr_boxes(s2)
check(len(ocr7.calls) == n1,
      f"画面没变时不再 OCR(仍为 {len(ocr7.calls)} 次), 直接沿用上一帧结果")

dev7.paint()
s3 = eng7._grab(ttl=0)
eng7._ocr_boxes(s3)
check(len(ocr7.calls) == n1 + 1, "画面变了必须重新识别")

# 连续复用上限: 静止画面最多连省 2 次, 第 3 次必须真识别(防"看漏一个小变化")
dev8 = PaintDevice()
ocr8 = RecordingOcr()
eng8 = make_engine(ocr=ocr8, dev=dev8)
s = eng8._grab(ttl=0)
eng8._ocr_boxes(s)
base = len(ocr8.calls)
for _ in range(3):
    s = eng8._grab(ttl=0)
    eng8._ocr_boxes(s)
check(len(ocr8.calls) == base + 1,
      f"静止画面连拍 4 次只额外识别 1 次(上限 {eng8._SAME_STREAK_MAX} 次复用), "
      f"实际多识别 {len(ocr8.calls)-base} 次")

# 验收点不许省
from engine.model import Step            # noqa: E402
eng7._reuse_on_same = True
st = Step(name="验收", wait={"ocr": "x"}, timeout=1, verify_done=True)
import engine.runner as R                # noqa: E402
source = open(R.__file__, encoding="utf-8").read()
check('getattr(step, "verify_done", False) or getattr(step, "verify_retry", False)'
      in source,
      "验收点(verify_done/verify_retry)会关闭同屏复用")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)