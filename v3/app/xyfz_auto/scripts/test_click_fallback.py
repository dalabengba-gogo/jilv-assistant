# -*- coding: utf-8 -*-
"""click 的「模板优先 / 坐标兜底」语义测试 (v5.4.0 P1-4)

事故背景(2026-10-19 每日必领实跑):
    15:17「每日必领-②点魔王窟礼包」报
        「按钮[魔王窟礼包]模板未命中，无法定位点击」
    —— 那个按钮是"周常页签右下浮动礼包", 可领取时右上角会多一个"!"角标,
    正好压住模板的一角。模板匹配从 0.95 掉到 0.7x → 命中失败 → 整段②直接收尾,
    当日礼包没领。而注册表里 at:[465,825] 是早就实测过的坐标。
    (同一类坑: 模板采集铁律里"动态角标陷阱" —— 角标对 TM_CCOEFF_NORMED 无效)

本测试验证:
  1. mode: locate_or_at → 模板命中时用模板坐标(不依赖固定坐标)
  2. mode: locate_or_at → 模板未命中时退回注册坐标, 不再报错
  3. mode: locate   → 模板未命中仍报错(向后兼容, 不能被兜底洗白)
  4. 注册表没有 at 时 locate_or_at 也不会瞎点(按 locate 处理)
  5. 每日必领.yaml 里魔王窟礼包那一步确实是 locate_or_at
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

from engine.runner import Engine          # noqa: E402
from engine.ui_registry import UiRegistry  # noqa: E402

FAILS = []


def check(cond, msg, extra=""):
    print(f"  {'✔' if cond else '✘'} {msg}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(msg)


class FakeDevice:
    def __init__(self):
        self.taps = []

    def tap(self, x, y):
        self.taps.append((x, y))

    def screenshot(self):
        return b""


class FakeHit:
    def __init__(self, x, y):
        self.x, self.y = x, y


class FakeMatcher:
    """match_one 返回 hit_x/hit_y; 为 None 表示模板未命中。
    loose: 传了低阈值(<=0.6)时返回的对象 —— 模拟"按钮被角标遮挡, 严格阈值不中
           但宽松阈值能认出来"的情况。"""
    def __init__(self, hit=None, loose=None, loose_score=0.68):
        self.hit = hit
        self.loose = loose
        self.calls = []
        if loose is not None and not hasattr(loose, "score"):
            loose = FakeHit(loose[0], loose[1])
        if loose is not None:
            loose.score = loose_score
        self._loose = loose

    def match_one(self, screen, name, threshold=None):
        self.calls.append(threshold)
        if threshold is not None and threshold <= 0.6:
            return self._loose
        return self.hit


def make_engine(registry, hit, loose=None):
    eng = Engine(device=FakeDevice(), matcher=FakeMatcher(hit, loose))
    eng.ui = registry
    eng._grab = lambda: object()
    return eng


REG = UiRegistry(os.path.join(APP, "assets", "ui_registry.yaml"))
eng = make_engine(REG, FakeHit(468, 820))
check("注册表里 魔王窟礼包 同时有 template 与 at",
      bool(REG.get("魔王窟礼包").get("template")) and bool(REG.get("魔王窟礼包").get("at")),
      str(REG.get("魔王窟礼包")))

print("--- 1. 模板命中时用模板坐标 ---")


def click(eng, button, mode):
    """走真 _do_action 的 click 分支(用一个最小 Step 对象)"""
    from engine.model import Step
    step = Step(name="点按钮", action={"type": "click", "button": button, "mode": mode})
    eng._do_action(step, None)
    return list(eng.device.taps)


check(click(eng, "魔王窟礼包", "locate_or_at") == [(468, 820)],
      "模板命中 → 点模板位置(不依赖固定坐标)")

print("--- 2. 模板未命中但被遮挡(宽松阈值能认) → 退回注册坐标 ---")
eng2 = make_engine(REG, None, loose=(468, 820))
taps = click(eng2, "魔王窟礼包", "locate_or_at")
check(taps == [(465, 825)], f"宽松阈值命中 → 点注册坐标(实际 {taps})")
check(0.55 in eng2.matcher.calls, "确实做了低阈值复核")

print("--- 2b. 按钮根本不在这 → 不点(真机 16:23 的坑) ---")
# 那天周常页签右下角是空的(今日无可领), 模板严格/宽松都不中。
# 旧实现会"对着空气点一下"(点到了魔王窟卡片), 现在必须直接判失败、不乱点。
eng2b = make_engine(REG, None, loose=None)
try:
    taps = click(eng2b, "魔王窟礼包", "locate_or_at")
    check(False, f"按钮不存在时不该点击(实际点了 {taps})")
except TimeoutError as e:
    check("宽松阈值" in str(e), "按钮不存在 → 报超时(交给 on_fail 跳过), 不盲点")
check(not eng2b.device.taps, "确实一次都没点")

print("--- 3. locate 仍然严格(向后兼容) ---")
eng3 = make_engine(REG, None, loose=(468, 820))
try:
    click(eng3, "魔王窟礼包", "locate")
    check(False, "mode: locate 模板未命中时应抛 TimeoutError")
except TimeoutError as e:
    check("模板未命中" in str(e), "mode: locate 模板未命中仍报错(不会被兜底洗白)")
    check("宽松阈值" not in str(e),
          "locate 模式不做低阈值复核(文案不被搅乱 —— 16:36 踩过)")
check(not eng3.device.taps, "locate 未命中时一次都没点")

print("--- 4. 按钮名写错时不静默点击 ---")
try:
    REG.resolve_click("这个名字不存在", "locate_or_at")
    check(False, "不存在的按钮应报 ValueError")
except ValueError as e:
    check("注册表中没有按钮" in str(e), "不存在的按钮报 ValueError(不会点空)")

print("--- 5. 脚本层 ---")
p = os.path.join(APP, "assets", "tasks", "每日必领.yaml")
text = io.open(p, encoding="utf-8").read()
check("button: 魔王窟礼包, mode: locate_or_at" in text,
      "每日必领 魔王窟礼包 已改 locate_or_at")
check("button: 魔王窟礼包, mode: locate}" not in text,
      "没有残留的严格 locate 写法")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(0 if not FAILS else 1)
