# -*- coding: utf-8 -*-
"""ocr_pair 几何门槛 + 师门找卡兜底 的确定性测试(不依赖模拟器)

批次七 v5.1.0 新增。事故背景(2026-10-19 真机):
  · 师门一整天零执行 —— 世界播报横幅盖住第一张卡的卡名(y108~145), OCR 读不到
    「师门任务」→ 点不了卡; 恢复块又重复同一次 OCR, 最后把"找不到卡"当成
    "已完成"直接收尾。
  · 五环/域外/地煞的验收点用 pair_min_score: 0.75, 而实测真卡名置信度中位数只有
    0.71~0.80 → 同一张卡不同帧忽过忽不过, 五环连判 4 帧全灭。

本测试验证:
  1. 几何门槛: 真卡名(右半区大号美术字)命中, 被压住的窄框误读被拒
  2. 分数/宽度/纵向距离各自生效
  3. 不写几何键时行为与旧版一致
  4. 脚本层: 所有 pair 判定都带 pair_min_x: 250 且 timeout >= 12
  5. 脚本层: 师门/五环的"找不到卡"不再 goto 已完成关面板, 改走明确收尾
  6. 脚本层: 师门有横幅遮挡兜底块(固定坐标 270,250), 且入口在非顺序流
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
from engine.loader import load_task       # noqa: E402

FAILS = []
TASK_DIR = os.path.join(APP, "assets", "tasks")


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


class FakeOcr:
    def __init__(self, boxes):
        self.boxes = boxes

    def recognize(self, screen):
        return self.boxes


def box(text, score, x, y, w, h):
    return {"text": text, "score": score, "x": x, "y": y, "w": w, "h": h}


def judge(boxes, cond):
    """用真 Engine 的 _recognize_wait 判一次(device/matcher 不参与 ocr_pair)"""
    eng = Engine(device=None, matcher=None, ocr=FakeOcr(boxes))
    screen = np.zeros((960, 540, 3), dtype=np.uint8)
    return eng._recognize_wait(cond, screen)


# 实测样本(取自 2026-10-19 失败现场截图离线 OCR)
BADGE_470 = box("已完成", 0.716, 43, 470, 64, 23)    # 上一张卡的徽标
BADGE_662 = box("已完成", 0.731, 43, 662, 63, 24)    # 五环卡的徽标
CARD_REAL = box("五环任务", 0.760, 301, 683, 164, 37)  # 真卡名: 右半区, 164 宽
# 误读样本: 实测"沉底卡被新消息气泡压住"时, 卡名会被读成目标词但框很窄
# (x=211 w=112, 落在面板左半区)。这里把 y 放到与徽标同一排, 模拟假配对成立的条件。
CARD_MISS = box("五环任务", 0.682, 211, 655, 112, 35)

COND = {"ocr_pair": ["已完成", "五环任务"], "pair_min_score": 0.62, "pair_min_x": 250}

print("--- 1. 几何门槛 ---")
hit = judge([BADGE_470, BADGE_662, CARD_REAL], COND)
check(hit is not None and abs(hit["y"] - 683) < 2,
      "真卡名(右半区)与同排徽标配对命中")
hit = judge([BADGE_470, BADGE_662, CARD_MISS], COND)
check(hit is None, "左边窄框误读(地煞星 x=211 w=112)被拒")
# 同一个误读, 去掉几何门槛 → 旧语义下会假命中(证明几何门槛真的在起作用)
legacy = {"ocr_pair": ["已完成", "五环任务"], "pair_min_score": 0.62}
check(judge([BADGE_470, BADGE_662, CARD_MISS], legacy) is not None,
      "对照组: 不写 pair_min_x 时同一误读会假命中(旧版风险)")

print("--- 2. 分数 / 宽度 / 纵向距离 ---")
low = box("五环任务", 0.55, 301, 683, 164, 37)
check(judge([BADGE_662, low], COND) is None, "置信度低于 pair_min_score 被拒")
narrow = box("五环任务", 0.90, 301, 683, 100, 37)
check(judge([BADGE_662, narrow], {"ocr_pair": ["已完成", "五环任务"],
                                  "pair_min_w": 140}) is None,
      "pair_min_w 生效(100 宽的框被拒)")
far = box("五环任务", 0.90, 301, 800, 164, 37)   # 与徽标 y 差 >45
check(judge([BADGE_662, far], COND) is None, "纵向距离超过 pair_dy 不配对")
close = box("五环任务", 0.90, 301, 700, 164, 37)
check(judge([BADGE_662, close], COND) is not None, "相邻 38px 仍算同一张卡")
check(judge([BADGE_662, far], {"ocr_pair": ["已完成", "五环任务"],
                               "pair_dy": 200}) is not None,
      "pair_dy 可放大(供副标题配对等场景)")

print("--- 3. 脚本层: 验收门槛 ---")
pair_lines = 0
for fn in sorted(os.listdir(os.path.join(TASK_DIR, "parts"))):
    if not fn.endswith(".yaml"):
        continue
    text = io.open(os.path.join(TASK_DIR, "parts", fn), encoding="utf-8").read()
    for m in re.finditer(r"wait: \{ocr_pair: \[已完成, [^\]]+\]([^}]*)\}", text):
        pair_lines += 1
        extra = m.group(1)
        if "pair_min_score" not in extra:
            continue          # 三选一探测(域外找卡)本就不设门槛
        tail = text[m.end():m.end() + 200]
        t = re.search(r"timeout:\s*(\d+(?:\.\d+)?)", tail)
        ok = ("pair_min_x: 250" in extra and "pair_min_score: 0.62" in extra
              and t and float(t.group(1)) >= 12)
        check(ok, f"{fn}: 验收门槛＝0.62 + 右半区 + timeout≥12")
check(pair_lines >= 17, f"共扫到 {pair_lines} 处 ocr_pair(≥17)")

print("--- 4. 脚本层: 师门/五环 不再把'找不到卡'当已完成 ---")
shimen = io.open(os.path.join(TASK_DIR, "parts", "daily_shimen.yaml"),
                 encoding="utf-8").read()
wuhuan = io.open(os.path.join(TASK_DIR, "parts", "daily_wuhuan.yaml"),
                 encoding="utf-8").read()
check("师门-找卡恢复-再点卡片\n    click: [270, 250]" in shimen,
      "师门恢复块改用卡面固定坐标点(横幅盖不住 y250)")
check("on_fail: {goto: 师门-脚本结束}" in shimen,
      "师门恢复块失败走明确收尾(不再 goto 已完成关面板)")
check("on_fail: {goto: 五环-脚本结束}" in wuhuan,
      "五环恢复块失败同样走明确收尾")
check("师门-找卡-遮挡兜底-点卡面" in shimen and "师门-找卡-遮挡兜底-等介绍页" in shimen,
      "师门有横幅遮挡兜底块")
check("队伍申请" in shimen and "队伍申请" in wuhuan,
      "师门/五环都有队伍申请弹窗守卫")

print("--- 5. 兜底块必须不在顺序流上 ---")
task = load_task(os.path.join(TASK_DIR, "parts", "daily_shimen.yaml"))
names = [s.name for s in task.steps]
idx_fallback = names.index("师门-找卡-遮挡兜底-面板在否")
idx_main_click = names.index("点师门任务卡片(OCR定位)")
idx_offline = names.index("师门-若弹离线完成提示则点继续进行")
check(not (idx_main_click < idx_fallback < idx_offline),
      "兜底块不在'点卡→等介绍页'的顺序路径中间(避免点成功后又被点一次)")
targets = set()
for s in task.steps:
    for attr in ("on_fail", "on_success"):
        v = getattr(s, attr, None)
        if isinstance(v, dict) and "goto" in v:
            targets.add(v["goto"])
check("师门-找卡-遮挡兜底-面板在否" in targets, "兜底块有明确入口(on_fail goto)")

print()
if FAILS:
    print(f"✘ {len(FAILS)} 项不通过:")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✔ ocr_pair 几何门槛 / 师门找卡兜底 全部通过")
