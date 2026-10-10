# -*- coding: utf-8 -*-
"""回归测试: 停止任务响应延迟

2026-09-16 用户反馈"停止任务耗时很长时间"。
根因: sleep 动作/轮询间隔/重试间隔用裸 time.sleep() 睡到底,
脚本里最长有 sleep 150s, 点停止最坏要等满再退出。

本测试用桩设备(不连模拟器)验证三类阻塞点被停止请求打断的耗时,
期望全部 < 0.5s。修复前 sleep 动作期望值 = 睡眠总时长(秒级)。

用法(项目根目录):
    venv_python scripts/test_stop_latency.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
import numpy as np

from engine.model import Step, Task
from engine.runner import Engine


class _StubMatcher:
    """永不命中的匹配器: 让 wait 步骤一直轮询到超时"""

    def match_one(self, screen, name):
        return None

    def reload(self):
        pass


class _StubDevice:
    """桩设备: screenshot 返回纯黑 PNG, 模拟一次截图耗时"""

    def __init__(self, shot_cost=0.05):
        self.shot_cost = shot_cost
        img = np.zeros((960, 540, 3), dtype=np.uint8)
        ok, buf = cv2.imencode(".png", img)
        self._png = buf.tobytes()

    def screenshot(self, *a, **k):
        time.sleep(self.shot_cost)
        return self._png

    def tap(self, *a, **k):
        pass

    def key(self, *a, **k):
        pass


def _run_and_stop_at(step: Step, stop_after: float, shot_cost=0.05):
    """后台跑单步, 到点调 stop(), 返回(停止请求→run_step 返回)的耗时秒数"""
    eng = Engine(_StubDevice(shot_cost), _StubMatcher(), poll_interval=1.0)
    task = Task(name="stub", steps=[step])
    out = {}

    def _worker():
        t0 = time.time()
        eng.run_task(task)
        out["total"] = time.time() - t0

    th = threading.Thread(target=_worker, daemon=True)
    th.start()
    time.sleep(stop_after)
    t_req = time.time()
    eng.stop()
    th.join(timeout=30)
    if th.is_alive():
        return None, out.get("total")
    return time.time() - t_req, out.get("total")


def check_sleep_cap():
    """验证 config.yaml → engine.max_sleep_seconds 能压缩脚本里的固定停顿

    2026-09-29 新增: 用户想把脚本里过长的干等压到 5~10 秒, 又不想逐个改脚本。
    引擎读了该配置后, 任何 sleep 步骤都会被截到不超过该值。
    """
    eng = Engine(_StubDevice(0.0), _StubMatcher(), poll_interval=1.0)
    eng._cfg_cache = {"engine": {"max_sleep_seconds": 1.0}}   # 注入: 上限 1 秒
    task = Task(name="stub", steps=[
        Step(name="长停顿", action={"type": "sleep", "seconds": 60})])
    t0 = time.time()
    eng.run_task(task)
    cost = time.time() - t0
    ok = cost <= 3.0
    print(f"[{'OK  ' if ok else 'FAIL'}] sleep:60 被 max_sleep_seconds=1 压缩"
          f"\n       实际耗时 {cost:.2f}s (不压则 60s)")
    return 0 if ok else 1


def main():
    cases = [
        # (说明, 步骤, 停止前先跑多久, 允许的最大响应耗时)
        ("sleep 动作 sleep:150 (旧版要等满 150s)",
         Step(name="长停顿", action={"type": "sleep", "seconds": 150}), 0.5, 0.5),
        ("sleep 动作 sleep:60 (旧版要等满 60s)",
         Step(name="停顿60", action={"type": "sleep", "seconds": 60}), 0.3, 0.5),
        ("wait 轮询中 (template 条件, 每轮约 1s)",
         Step(name="等待", wait={"template": "never"}, timeout=600), 0.2, 2.5),
        ("wait 超时后的重试间隔",
         Step(name="重试", wait={"template": "never"}, timeout=1, retry=5), 1.2, 2.5),
    ]
    bad = 0
    print("=" * 74)
    for desc, step, wait_before, limit in cases:
        lat, total = _run_and_stop_at(step, wait_before)
        if lat is None:
            print(f"[FAIL] {desc}\n       线程 30s 内未退出(停止没生效!)")
            bad += 1
            continue
        flag = "OK  " if lat <= limit else "FAIL"
        if lat > limit:
            bad += 1
        print(f"[{flag}] {desc}\n       停止响应 {lat:.2f}s (上限 {limit}s)")
    print("-" * 74)
    bad += check_sleep_cap()
    print("=" * 74)
    print("全部通过 ✔" if not bad else f"有 {bad} 项未通过 ✘")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
