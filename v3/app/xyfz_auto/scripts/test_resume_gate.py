# -*- coding: utf-8 -*-
"""一条龙续跑判据 (ocr_row.any_of) 的确定性测试 —— 不依赖模拟器

v5.4.0(P0-2) 新增。事故背景(2026-10-19 真机日志 + 截图):
  · 15:24「点任务栏-组队一条开跑」OCR 找不到「组队一条」→ 切任务页签仍找不到
    → 落到「一条-任务栏仍有条目则续跑」;
  · 该步判据只有 has:["/"] not:[V/√/✓] —— 等于"任务栏里任何一条带计数的行",
    当天主界面上有「达成条件后继续主线」「招募路人|跑环任务[4/5]」等多行,
    于是假命中 → 点了 (454,227)(那是主线行) → 判"一条在跑" → 进监控 45 轮;
  · 45 轮 × 约 67 秒 ≈ 50 分钟纯等。那天恰好一条龙真的做完了(上一轮已完成),
    所以只损失了 4 分钟安静确认; 一旦是"任务栏被主线挤占", 就是白等 50 分钟。

本测试验证:
  1. any_of 语义: 含一条龙玩法名的行命中, 只有主线/跑环的行不命中
  2. has + any_of + not + region 组合仍然各自生效
  3. 不给 any_of 时行为与旧版一致(向后兼容)
  4. 脚本层: 两处续跑判据都带 any_of, 不再有裸 has:["/"] not:[V]
  5. 脚本层: 续跑后必须有一次"真的动了"的确认, 且失败走收尾而不是继续监控
"""
import io
import os
import re
import sys

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


class FakeOcr:
    """假识别引擎: 返回**裁图坐标系**下的框

    v5.5.0 起引擎会对 region 做纵向裁剪(只认那条带), 送进来的图是裁过的,
    识别结果由引擎加回偏移。所以这里必须把纵坐标减去裁掉的顶部高度 ——
    否则框会被平移出区域, 测出来的"漏判"其实是假象。
    """

    def __init__(self, boxes, offset=0):
        self.boxes = boxes
        self.offset = offset

    def recognize(self, screen):
        if not self.offset:
            return self.boxes
        return [dict(b, y=b["y"] - self.offset) for b in self.boxes]


def box(text, x, y, w=150, h=20):
    return {"text": text, "score": 0.9, "x": x, "y": y, "w": w, "h": h}


def row(boxes, spec):
    """用真 Engine 的 _ocr_row 判一次(不需要 device/matcher)"""
    # 复刻引擎的纵向裁带算法(_ocr_boxes), 供 FakeOcr 把坐标换算回裁图系
    reg = spec.get("region") or [0, 0, 540, 960]
    cy1, cy2 = max(0, reg[1] - 26), min(960, reg[3] + 26)
    if cy2 - cy1 >= 960 - 60:
        cy1 = 0
    eng = Engine(device=None, matcher=None, ocr=FakeOcr(boxes, offset=cy1))
    screen = np.zeros((960, 540, 3), dtype=np.uint8)
    return eng._recognize_wait({"ocr_row": spec}, screen)


# 2026-10-19 15:24 主界面实测的任务栏内容(区域 x 356~540, y 150~320)
LINES = [
    box("达成条件后继续主线", 356, 160, 180),     # 主线: 无计数
    box("招募路人 | 跑环任务 [4/5]", 356, 200),   # 有计数, 但不是一条龙
    box("通关第214章", 356, 250, 120),
]
YIHUAN = LINES + [box("域外风情0/10", 356, 300)]

SPEC = {
    "has": ["/"],
    "any_of": ["域外风情", "挖宝封妖", "烧法任务", "商旅护送", "帮派任务"],
    "not": ["V", "√", "✓"],
    "region": [356, 150, 540, 320],
}

print("--- 1. any_of 语义 ---")
check(row(YIHUAN, SPEC) is not None, "含「域外风情0/10」的行命中")
check(row(LINES, SPEC) is None,
      "只有主线/跑环(带 [4/5] 计数)时**不命中** —— 旧判据在这里会假命中")
check(row(LINES, {"has": ["/"], "not": ["V"]}) is not None,
      "对照组: 不写 any_of 时同一屏会假命中(旧版风险)")
check(row(LINES + [box("挖宝封妖3/10", 356, 300)], SPEC) is not None,
      "任一玩法名(挖宝封妖)都算")

print("--- 2. has / not / region 仍各自生效 ---")
check(row([box("域外风情 10/10 V", 356, 300, 170)], SPEC) is None,
      "已完成行(带 V)被 not 排掉")
check(row([box("帮派任务", 356, 300, 100)], SPEC) is None,
      "没有计数(不含 /)的行不命中")
check(row([box("域外风情5/10", 60, 880, 120)], SPEC) is None,
      "左下角世界聊天里的同名文字被 region 排掉")

print("--- 3. 向后兼容 ---")
check(row([box("某任务 1/3", 400, 200)], {"has": ["/"], "not": ["V"]}) is not None,
      "不写 any_of 时旧行为不变")
try:
    row([], {"region": [0, 0, 10, 10]})
    check(False, "has/any_of 都为空时应抛 ValueError")
except ValueError:
    check(True, "has/any_of 都为空时报写法错误")

print("--- 4. 脚本层: 判据已收紧 ---")
p = os.path.join(APP, "assets", "tasks", "parts", "daily_team_oneloop.yaml")
text = io.open(p, encoding="utf-8").read()
naked = re.findall(r'ocr_row: \{has: \["/"\], not: \[V\][^}]*\}', text)
check(not naked, f"没有裸 has:['/'] not:[V] 的判据(发现 {len(naked)} 处)")
specs = re.findall(r"ocr_row: \{[^}]*any_of: \[([^\]]+)\][^}]*\}", text)
check(len(specs) >= 2, f"两处续跑判据都带 any_of(实际 {len(specs)} 处)")
for s in specs:
    names = [x.strip() for x in s.split(",")]
    check(len(names) >= 5, f"any_of 覆盖 5 个玩法名(实际 {len(names)} 个)")

print("--- 5. 脚本层: 续跑后必须确认真的动了 ---")
m = re.search(r"- name: 一条-续跑后确认真的动了\n((?:    .*\n)+)", text)
check(bool(m), "存在「一条-续跑后确认真的动了」步骤")
if m:
    body = m.group(1)
    check("not: {ui: 主界面}" in body, "判据含'离开主界面'这一路")
    check("ocr: 回合" in body, "判据含战斗信号(回合)")
    check("goto: 一条今日已完成-跳过收尾" in body,
          "点空了直接按已完成收尾(不再进监控空转)")

# ------------------------------------------------------------------ 结果
print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(0 if not FAILS else 1)
