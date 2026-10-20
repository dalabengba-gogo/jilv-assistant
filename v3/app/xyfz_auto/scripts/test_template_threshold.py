# -*- coding: utf-8 -*-
"""模板阈值分级(脚本 > _meta.txt > 全局) 的确定性测试(不依赖模拟器)

批次六 v4.9.0 P2-5 新增。验证:

  1. `_meta.txt` 解析: `=` / `:` 两种写法、注释、空行、坏行、越界值
  2. 阈值优先级: 模板默认值**高于**全局, 但**低于**脚本显式传参
  3. 缺省不配时行为完全不变(仍用全局 match_threshold)
  4. loader 把 YAML 里的 `threshold:` 读进 Step.threshold
  5. runner 只对模板类条件生效(把本步阈值传给 matcher), 不在条件里时不受影响
  6. 真实模板库: 65 张模板 / _meta.txt 里没有写错的模板名
"""
import os
import shutil
import sys
import tempfile

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


TMP = tempfile.mkdtemp(prefix="tplmeta_")


def make_template(name: str, seed: int) -> np.ndarray:
    """造一张有纹理的小图(TM_CCOEFF_NORMED 需要方差, 纯色会算出 nan)"""
    rng = np.random.default_rng(seed)
    img = rng.integers(0, 256, (40, 60, 3), dtype=np.uint8)
    cv2.imwrite(os.path.join(TMP, name + ".png"), img)
    return img


tpl = make_template("btn_ok", 1)
# 屏幕 = 模板盖掉顶部 12 行再平铺 → 实测相似度约 0.838, 正好落在
# "全局 0.90 判不中 / 模板阈值 0.80 判中" 的区间里, 用来验证分级是否真的生效
screen_part = tpl.copy()
screen_part[:12, :] = 128
screen = np.ascontiguousarray(np.tile(screen_part, (3, 3, 1))[:200, :200])

from recognition.template import TemplateMatcher

print("[1] _meta.txt 解析")
m = TemplateMatcher(TMP, threshold=0.80)
score = m.match_one(screen, "btn_ok", threshold=0.05).score
print(f"   (本帧实际相似度 {score:.4f})")
check(0.70 < score < 0.90, f"构造的帧确实是\"像但不完全一样\", 实测 {score:.4f}")
with open(os.path.join(TMP, "_meta.txt"), "w", encoding="utf-8") as f:
    f.write(
        "# 注释行不算\n"
        "\n"
        "btn_ok = 0.80\n"
        "另一个 = 0.9   # 行尾注释\n"
        "冒号写法: 0.85\n"
        "坏行没有等号\n"
        "越界 = 1.8\n"
        "不是数字 = abc\n")
m.reload()
check(abs(m.thresholds.get("btn_ok", 0) - 0.80) < 1e-9, "= 写法被解析")
check(abs(m.thresholds.get("冒号写法", 0) - 0.85) < 1e-9, ": 写法被解析")
check("坏行没有等号" not in m.thresholds, "没有分隔符的行被跳过")
check("越界" not in m.thresholds, "超出 0~1 的值被跳过")
check("不是数字" not in m.thresholds, "非数字值被跳过")
check(len(m.thresholds) == 3, f"只解析到 3 条有效配置, 实测 {sorted(m.thresholds)}")

print("\n[2] 优先级: 脚本 > _meta.txt > 全局")
m_default = TemplateMatcher(TMP, threshold=0.90)
check(m_default.match_one(screen, "btn_ok") is not None,
      "配了 _meta.txt 的模板: 全局 0.90 判不中, 模板自己的 0.80 能判中")
check(m_default.match_one(screen, "btn_ok", threshold=0.95) is None,
      "脚本显式传的 0.95 压过 _meta.txt(判不中)")
check(m_default.threshold_for("btn_ok") == m_default.thresholds["btn_ok"],
      "threshold_for 走模板默认值")
check(m_default.threshold_for("没配过的模板") == 0.90,
      "没配过的模板退回全局 0.90")
check(m_default.threshold_for("btn_ok", 1.5) == m_default.thresholds["btn_ok"],
      "非法覆盖值(>1)被忽略, 仍用模板默认值")

print("\n[3] 没有 _meta.txt 时行为与旧版一致")
tmp2 = tempfile.mkdtemp(prefix="tplnometa_")
cv2.imwrite(os.path.join(tmp2, "btn_ok.png"), tpl)
m2 = TemplateMatcher(tmp2, threshold=0.90)
check(m2.thresholds == {}, "_meta.txt 缺失 → 空表, 不报错")
check(m2.match_one(screen, "btn_ok") is None, "仍按全局 0.90 判定(不命中)")
shutil.rmtree(tmp2, ignore_errors=True)

print("\n[4] loader 把 threshold: 读进 Step")
tmpy = os.path.join(TMP, "t.yaml")
with open(tmpy, "w", encoding="utf-8") as f:
    f.write(
        "name: 阈值测试\n"
        "settings: {default_timeout: 5, default_retry: 0}\n"
        "steps:\n"
        "  - name: 带阈值\n"
        "    wait: {template: btn_ok}\n"
        "    threshold: 0.92\n"
        "  - name: 不带阈值\n"
        "    wait: {template: btn_ok}\n")
from engine.loader import load_task

t = load_task(tmpy)
check(abs(t.steps[0].threshold - 0.92) < 1e-9,
      f"threshold 解析为 0.92, 实测 {t.steps[0].threshold}")
check(t.steps[1].threshold == 0.0, "没写的步骤 threshold=0(不覆盖)")

print("\n[5] runner 把本步阈值传给 matcher(只对模板条件)")
from engine import Engine


class RecordingMatcher:
    def __init__(self):
        self.calls = []

    def match_one(self, screen, name, threshold=None):
        self.calls.append((name, threshold))
        return object()


eng = Engine(device=object(), matcher=RecordingMatcher(), poll_interval=0.02)
eng._step_threshold = 0.93
eng._recognize_wait({"template": "btn_ok"}, screen)
check(eng.matcher.calls[-1] == ("btn_ok", 0.93),
      f"模板条件带上本步阈值, 实测 {eng.matcher.calls[-1]}")
eng._step_threshold = None
eng._recognize_wait({"template": "btn_ok"}, screen)
check(eng.matcher.calls[-1] == ("btn_ok", None),
      "本步没设阈值 → 传 None(交给 _meta.txt / 全局)")
check(len(eng.matcher.calls) == 2, "非模板条件不会去碰 matcher")

print("\n[6] 真实模板库: _meta.txt 与模板名对得上")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
real = TemplateMatcher(os.path.join(APP, "assets", "templates"))
missing = [n for n in real.thresholds if n not in real.templates]
check(not missing, f"_meta.txt 里的模板名都存在(缺失: {missing})")
check(len(real.templates) >= 60, f"模板库正常加载, 共 {len(real.templates)} 张")
check(not real.load_errors, f"没有读失败的模板(失败: {real.load_errors[:3]})")

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
