# -*- coding: utf-8 -*-
"""批次二桩测试: 失败记账分类 / 统一 deadline / 异常兜底 / goto 只算失败回跳

不连模拟器、不需要 Qt(纯引擎层), 用桩替换截图与识别。

覆盖 v3.5.0 的四项改动:
  1. P0-1 optional 探测的"未命中"与真失败分开计数 (failed / skipped)
  2. P0-1 required:true 的步骤失败 → 整条任务判失败
  3. P0-2 意外异常(DeviceError/OSError/cv2.error…)不再冒泡穿出 run_task
  4. P1-6 ① 整步共享时间预算(max_step_seconds) ② goto 计数只统计失败回跳

运行: vendor\\runtime\\python.exe scripts\\test_failure_accounting.py
"""
from __future__ import annotations

import os
import sys
import time

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), ".."))

from engine.model import Step, Task          # noqa: E402
from engine.runner import Engine             # noqa: E402

FAILS: list[str] = []


def check(name: str, cond: bool, extra: str = ""):
    print(("  ✔ " if cond else "  ✘ ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        FAILS.append(name)


def make_engine(cfg=None) -> Engine:
    """桩引擎: 不截图、不识别, 由各用例自行 patch"""
    eng = Engine(device=object(), matcher=object(), poll_interval=0.02)
    if cfg is not None:
        eng._cfg_cache = cfg
    return eng


def no_hit(eng: Engine):
    """截图永不命中"""
    eng._grab = lambda ttl=None: object()
    eng._recognize_wait = lambda cond, screen: None


# ---------------------------------------------------------------- 1. 分类
print("\n[1] 失败分类: 探测未命中 vs 真失败 vs required")
eng = make_engine()
no_hit(eng)
task = Task(name="分类", steps=[
    # 纯探测: 只有 wait、没有 action、on_fail=continue → 未命中属正常
    Step(name="查是否已领", wait={"template": "x"}, timeout=0.05, retry=0,
         on_fail="continue"),
    # 有 action 的步骤失败 = 真失败
    Step(name="点领取", wait={"template": "x"}, timeout=0.05, retry=0,
         action={"type": "sleep", "seconds": 0}, on_fail="continue"),
    # 显式 optional
    Step(name="查徽标", wait={"template": "x"}, timeout=0.05, retry=0,
         optional=True, on_fail="continue"),
    # 探测特征但写了 stop → 尊重脚本意图, 记真失败(且终止整条, 见下)
    Step(name="重要探测", wait={"template": "x"}, timeout=0.05, retry=0,
         on_fail="stop"),
    Step(name="永远跑不到", action={"type": "sleep", "seconds": 0}),
])
ok = eng.run_task(task)
st = eng.counts()
check("含 on_fail:stop → 整条终止(stop 本就该停)", ok is False)
check("真失败 = 2 步(有action + stop)", st["failed"] == 2, f"实际 {st['failed']}")
check("条件未命中 = 2 步(探测 + 显式optional)", st["skipped"] == 2, f"实际 {st['skipped']}")
check("counts 字段齐全",
      set(st) >= {"failed", "skipped", "required_failed", "stopped"})
s = eng.failure_summary()
check("汇总区分[失败]与[未命中]", "[失败]" in s and "[未命中]" in s)
check("汇总不再出现旧的[跳过]标记", "[跳过]" not in s)

print("\n[2] required:true 失败 → 整条任务判失败")
eng = make_engine()
no_hit(eng)
task = Task(name="关键", steps=[
    Step(name="导航-回主界面", wait={"template": "x"}, timeout=0.05, retry=0,
         action={"type": "sleep", "seconds": 0}, required=True,
         on_fail="continue"),
])
ok = eng.run_task(task)
check("整条判失败", ok is False)
check("required_failed = 1", eng.counts()["required_failed"] == 1)
check("终止原因被记录",
      "关键步骤" in (eng.last_result.aborted_reason or ""),
      repr(eng.last_result.aborted_reason))

# ------------------------------------------------------------ 3. 异常兜底
print("\n[3] 意外异常不再冒泡, 走 on_fail 并记账")


class Boom(RuntimeError):
    pass


eng = make_engine()
eng._wait_until = lambda *a, **k: (_ for _ in ()).throw(Boom("adb 掉线"))
task = Task(name="异常", steps=[
    # 有 action → 属"该做成的步骤", 异常记为真失败
    Step(name="会炸的步骤", wait={"template": "x"}, timeout=0.05, retry=0,
         action={"type": "sleep", "seconds": 0}, on_fail="continue"),
    # 纯探测 → 异常同样不冒泡, 但按未命中记(不污染失败数)
    Step(name="会炸的探测", wait={"template": "x"}, timeout=0.05, retry=0,
         on_fail="continue"),
    Step(name="后一步", action={"type": "sleep", "seconds": 0}),
])
try:
    ok = eng.run_task(task)
    bubbled = False
except Exception as e:
    bubbled = True
    print(f"    异常冒泡: {e}")
check("异常未冒泡出 run_task", not bubbled)
check("异常被记为失败(有 action 的)", eng.counts()["failed"] == 1,
      str(eng.counts()))
check("探测类异常记为未命中", eng.counts()["skipped"] == 1, str(eng.counts()))
check("后续步骤照常执行(整条未报废)", ok is True)
check("失败原因含异常类型",
      "Boom" in (eng.failed_steps[0]["reason"] if eng.failed_steps else ""),
      repr(eng.failed_steps[:1]))

# --------------------------------------------------------- 4. 统一 deadline
print("\n[4] 整步共享时间预算 (max_step_seconds)")
eng = make_engine({"engine": {"max_step_seconds": 1.0}})
no_hit(eng)
task = Task(name="预算", steps=[
    Step(name="长等", wait={"template": "x"}, timeout=5, retry=1,  # 名义 10s
         on_fail="continue"),
])
t0 = time.time()
eng.run_task(task)
cost = time.time() - t0
check(f"单步耗时被压到 ≈1s (名义 5s×2=10s), 实测 {cost:.2f}s", cost < 3.0)

print("\n[5] settle 稳检测循环有总时限(不再 while True 空转)")
eng = make_engine({"engine": {"max_step_seconds": 1.5}})
eng._grab = lambda ttl=None: object()
_state = {"n": 0}


def first_hit_then_miss(cond, screen):
    """第一次命中(触发 settle 复核), 之后永远不命中 → 老实现会无限空转"""
    _state["n"] += 1
    return object() if _state["n"] == 1 else None


eng._recognize_wait = first_hit_then_miss
task = Task(name="settle", steps=[
    Step(name="等回主界面", wait={"template": "x"}, timeout=1, retry=0,
         settle=0.1, on_fail="continue"),
])
t0 = time.time()
eng.run_task(task)
cost = time.time() - t0
check(f"复核空转被总时限终止, 实测 {cost:.2f}s (<3s)", cost < 3.0)
check("该步最终判超时失败", eng.counts()["failed"] + eng.counts()["skipped"] >= 1)

# ------------------------------------------------------ 6. goto 只算失败回跳
print("\n[6] goto 死循环保护只统计失败回跳(合法回边不误杀)")
eng = make_engine({"engine": {"goto_loop_limit": 5}})
eng._grab = lambda ttl=None: object()
_hits = {"n": 0}


def hit_first_10(cond, screen):
    _hits["n"] += 1
    return object() if _hits["n"] <= 10 else None


eng._recognize_wait = hit_first_10
task = Task(name="合法循环", steps=[
    # A: 命中 → on_success.goto C(合法跳转, 不计入死循环)
    Step(name="A", wait={"template": "x"}, timeout=0.05, retry=0,
         on_success={"goto": "C"}, on_fail="continue"),
    # B: jump 回边(合法, 不计入)
    Step(name="B", action={"type": "jump", "target": "A"}),
    Step(name="C", action={"type": "sleep", "seconds": 0}),
])
ok = eng.run_task(task)
check("10 次合法回边未被误判死循环(旧实现会在第 6 次终止)", ok is True)
check("_goto_counts 全程为空(success/jump 不计数)",
      eng._goto_counts == {}, str(eng._goto_counts))

print("\n[7] on_fail 回跳超限仍会被终止(保护没被削弱)")
eng = make_engine({"engine": {"goto_loop_limit": 3}})
no_hit(eng)
task = Task(name="真死循环", steps=[
    # 永不命中 → on_fail:{goto: B} → B 又 jump 回 A, 形成失败回跳环
    Step(name="A", wait={"template": "x"}, timeout=0.05, retry=0,
         on_fail={"goto": "B"}),
    Step(name="B", action={"type": "jump", "target": "A"}),
])
ok = eng.run_task(task)
check("失败回跳超限 → 整条终止", ok is False)
check("终止原因标明死循环", "死循环" in (eng.last_result.aborted_reason or ""),
      repr(eng.last_result.aborted_reason))
check("失败被记账", eng.counts()["failed"] + eng.counts()["skipped"] >= 3)

# ------------------------------------------------- 8. 纯等待超时不算真失败
# 2026-10-19 v5.3.0 (用户定调): 「等 15 秒没等到」属于探测没命中, 不该计入
# 结束统计的「真失败 N 步」。但验收点的面板结论(verified)必须照旧。
print("\n[8] 纯等待超时 → 条件未命中; 验收结论仍记 False; 有动作的步仍算真失败")
eng = make_engine()
no_hit(eng)
task = Task(name="等待超时归类", steps=[
    # 验收点(纯 wait, 没有 action): 面板没刷出徽标 → verified=False, 但不进真失败
    Step(name="域外验收-看已完成徽标", wait={"ocr_pair": ["已完成", "域外风情"]},
         timeout=0.05, retry=0, verify_done=True, on_fail="continue"),
    # 复核步(同款): 检查已未命中, 复核再超时一次也只是又一条未命中
    Step(name="域外验收-复核徽标", wait={"ocr_pair": ["已完成", "域外风情"]},
         timeout=0.05, retry=0, verify_done=True, verify_retry=True,
         on_fail="continue"),
    # 有动作的步超时 = 该点的没点上 → 仍算真失败(不能被一起抹掉)
    Step(name="点前往", wait={"ocr": "任务说明"}, timeout=0.05, retry=0,
         action={"type": "sleep", "seconds": 0}, on_fail="continue"),
])
eng.run_task(task)
st = eng.counts()
check("真失败只算有动作那 1 步", st["failed"] == 1, f"实际 {st['failed']}")
check("两条验收超时记为条件未命中", st["skipped"] == 2, f"实际 {st['skipped']}")
subs = {s["name"]: s for s in eng._build_subtasks()}
check("验收结论仍为 False(面板没显示已完成)", subs.get("主流程", {}).get("verified") is False,
      str(subs.get("主流程")))

# ------------------------------------------------- 9. 停止期间的异常不记账
# 2026-10-19 v5.4.0 (P1-1) 真机实证: 15:17 用户点停止 → 正在跑的步骤抛
# DeviceError(截图失败) → 结束统计留下「真失败 1 步」。停止是正常操作, 不该
# 留疤。另外发现 except InterruptedError 被写在 except Exception 之后(永远
# 走不到), 顺手纠正了顺序 —— 本用例把两条一起盯住。
print("\n[9] 手动停止期间的异常 → 不计入真失败")


class DeviceBoom(RuntimeError):
    """冒充 DeviceError(不 import device 层, 免得测试依赖 adb 模块)"""


eng = make_engine()


def _grab_then_stop(ttl=None):
    """模拟"截图过程中收到停止请求, adb 调用中断抛错"的顺序"""
    eng.running = False
    eng._stop_at = time.monotonic()
    raise DeviceBoom("截图失败")


eng._grab = _grab_then_stop
eng._recognize_wait = lambda cond, screen: None
task = Task(name="停止中", steps=[
    Step(name="正在截图的步骤", wait={"template": "x"}, timeout=0.05, retry=0,
         action={"type": "sleep", "seconds": 0}, on_fail="continue"),
    Step(name="本该继续的下一步", action={"type": "sleep", "seconds": 0}),
])
eng.run_task(task)
st = eng.counts()
check("停止期间的异常不计入真失败", st["failed"] == 0, f"实际 {st['failed']}")
check("也没有被记成条件未命中", st["skipped"] == 0, f"实际 {st['skipped']}")
check("结束统计里没有失败明细", not eng.failed_steps, str(eng.failed_steps[:1]))

# InterruptedError 必须走"已停止"而不是"意外异常": 用真异常类型再验一次
eng2 = make_engine()
eng2._wait_until = lambda *a, **k: (_ for _ in ()).throw(InterruptedError("已停止"))
task2 = Task(name="停止2", steps=[
    Step(name="等待中被停止", wait={"template": "x"}, timeout=0.05, retry=0,
         action={"type": "sleep", "seconds": 0}, on_fail="continue"),
])
ok2 = eng2.run_task(task2)
check("InterruptedError 不算失败", eng2.counts()["failed"] == 0,
      str(eng2.counts()))
check("InterruptedError 不冒泡", ok2 is False)

# ------------------------------------------------------------------ 结果
print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
