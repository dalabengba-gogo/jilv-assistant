# -*- coding: utf-8 -*-
"""P1-3 单步预算 / 判定下限 / 批次上限 / 单调时钟 的确定性测试(不依赖模拟器)

批次五 v4.8.0 新增。验证:

  P1-3 单步预算:
    1. 全局 max_step_seconds 生效: budget = timeout × tries 被压到全局上限
    2. per-step max_seconds **优先于** 全局(给长战斗步开口子)
    3. 长战斗步(域外)不受全局 1800 掐死 —— 用真实 YAML 实读
  P1-3b 判定下限:
    4. verify_done / verify_retry 步骤的 timeout / retry 被抬到下限(防只看一帧)
    5. 普通步骤**不受**下限影响(不拖慢整体剧本)
  max_visits 批次上限:
    6. 达到上限后不再执行该步, 走 on_fail 并记账; 未达上限正常通过
    7. run_task 重置计数(同一个 Task 对象跑两遍不串味)
  时钟一致性:
    8. 把墙钟拨到"未来", _sleep / _wait_until 的倒计时**不受影响**(单调时钟)
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from engine import Engine
from engine.model import Step, Task
import engine.runner as runner

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


def make_engine(cfg=None) -> Engine:
    eng = Engine(device=object(), matcher=object(), poll_interval=0.02)
    if cfg is not None:
        eng._cfg_cache = cfg
    return eng


def cfg_of(**eng):
    return {"engine": eng}


# --------------------------------------------------------------- 1. 全局预算
print("[1] 全局 max_step_seconds: 整步预算被压到上限")
eng = make_engine(cfg_of(max_step_seconds=1800))
long_step = Step(name="长等待", timeout=5400, retry=1)     # 预算 = 10800s
check(eng.step_budget(long_step) == 1800.0, "step_budget = 全局 1800s")
short_step = Step(name="短等待", timeout=30, retry=3)      # 预算 = 120s
check(eng.step_budget(short_step) == 1800.0, "step_budget 只给上限, 不限短步")

print("\n[2] per-step max_seconds 优先于全局(给长战斗步开口子)")
eng = make_engine(cfg_of(max_step_seconds=1800))
own = Step(name="域外-等打完回主界面", timeout=5400, retry=1, max_seconds=5400)
check(eng.step_budget(own) == 5400.0, "标了 max_seconds 的步按自己的值(不被 1800 掐死)")
check(eng.step_budget(Step(name="别的步", timeout=10, max_seconds=0)) == 1800.0,
      "没标的仍吃全局")

print("\n[3] 真实 YAML: 三个长等待步都有自己的预算口子")
try:
    from engine.loader import load_task
    from common import load_config
    cfg_engine = (load_config() or {}).get("engine") or {}
    check(float(cfg_engine.get("max_step_seconds") or 0) > 0,
          f"config 已启用 max_step_seconds = {cfg_engine.get('max_step_seconds')}")
    covered = 0
    for script in ("assets/tasks/域外风情.yaml", "assets/tasks/组队一条.yaml"):
        p = os.path.join(os.path.dirname(os.path.abspath(__file__)) + "/..", script)
        if not os.path.exists(p):
            continue
        task = load_task(p)
        for s in task.steps:
            if float(s.timeout) >= 600:
                covered += 1
                cap_from_step = float(getattr(s, "max_seconds", 0) or 0)
                global_cap = float(cfg_engine.get("max_step_seconds") or 0)
                need = float(s.timeout) * (max(1, int(s.retry) + 1))
                effective = min(need, cap_from_step or global_cap)
                check(effective >= float(s.timeout),
                      f"{script.split('/')[-1]}: 「{s.name}」单次 timeout {s.timeout:g}s 放得下"
                      f" (整步上限 {effective:g}s)")
    check(covered >= 3, f"覆盖到 {covered} 个长等待步骤(全项目共 4 个)")
except Exception as e:
    check(False, f"加载 YAML 失败: {e}")

# --------------------------------------------------------------- 4. 判定下限
print("\n[4] 判定下限: 验收点不再只看一帧就下结论")
eng = make_engine(cfg_of(verify_min_seconds=15, verify_min_retry=1))
verify_step = Step(name="地煞-查验已完成徽标", timeout=3, retry=0,
                   wait={"ocr": "已完成"}, verify_done=True)
eng._apply_judge_floor(verify_step, 0)
check(verify_step.timeout == 15, f"timeout 3 → {verify_step.timeout:g}s(抬到下限)")
check(verify_step.retry == 1, f"retry 0 → {verify_step.retry}(允许一次复核)")

retry_step = Step(name="复核", timeout=20, retry=2, verify_retry=True)
eng._apply_judge_floor(retry_step, 1)
check(retry_step.timeout == 20 and retry_step.retry == 2,
      "本来就够的步骤不动(幂等, 反复调用不会越抬越高)")

# 真实配置必须"至少够两轮" —— 实测一轮判定约 6.02s(截图 0.8 + 全图 OCR 5.2),
# 下限若低于 12s 等于仍然只看一两帧, 这条断言防止有人把值调回去。
real = make_engine()
real._ensure_cfg()
floor = real.verify_min_seconds()
check(floor >= 12, f"真实 config 的下限 {floor:g}s ≥ 两轮实测耗时(6.02s×2)")

print("\n[5] 普通步骤不受下限影响(不拖慢剧本)")
plain = Step(name="普通-等待主界面", timeout=3, retry=0, wait={"ui": "主界面"})
eng._apply_judge_floor(plain, 2)
check(plain.timeout == 3 and plain.retry == 0, "非验收点维持脚本原值")

off = make_engine(cfg_of(verify_min_seconds=0, verify_min_retry=0))
vd = Step(name="验收", timeout=3, retry=0, verify_done=True)
off._apply_judge_floor(vd, 0)
check(vd.timeout == 3 and vd.retry == 0, "下限配 0 可整体关掉")

# --------------------------------------------------------------- 6. 批次上限
print("\n[6] max_visits 批次上限")
eng = make_engine(cfg_of())
task = Task(name="伪域外", steps=[
    Step(name="循环头", wait={"ui": "主界面"}, timeout=0.5, retry=0,
         max_visits=3, on_fail="continue"),
])
eng.running = True
eng._grab = lambda ttl=None: object()
eng._recognize_wait = lambda cond, screen: True      # 永远命中 → 会反复进该步
for i in range(3):
    eng.run_step(0, task)
check(eng._visits.get(0) == 3, f"前三次正常执行(计数 {eng._visits.get(0)})")
check(not eng.failed_steps, "未达上限时不记账为失败")
eng.run_step(0, task)
# 超限不是"做砸了", 因此按常见的「条件未命中」语义记账: 走 on_fail(通常是
# goto 验收块), 后续步骤照跑, 任务不会因为按额度和气收手而报红。
notes = eng.failed_steps + eng.skipped_steps
check(len(notes) == 1, "第 4 次 → 记账 1 条(走了 on_fail, 不再执行该步)")
check("批次上限" in (notes[0].get("reason") if notes else ""),
      f"原因写明批次上限: {notes[0].get('reason') if notes else '(无)'}")

print("\n[7] 每次运行重置计数(同一 Task 连跑不串味)")
eng2 = make_engine(cfg_of())
task2 = Task(name="伪", steps=[Step(name="步", timeout=0.5, retry=0, max_visits=2,
                                    on_fail="continue")])
eng2._grab = lambda ttl=None: object()
eng2._recognize_wait = lambda cond, screen: True
eng2.run_task(task2)
check(not eng2.failed_steps, "第一轮两趟都在额度内, 无失败")
eng2.run_task(task2)          # 第二轮不该继承上一轮的计数
check(not eng2.failed_steps, "第二轮重新计数, 仍无失败(没被旧计数立刻判超)")

# --------------------------------------------------------------- 8. 单调时钟
print("\n[8] 单调时钟: 墙钟被拨动不影响倒计时")
eng3 = make_engine(cfg_of())
real_time = time.time
try:
    # 把墙钟拨到 10 年后 —— 若计时用 time.time(), deadline 会瞬间"已过"
    runner.time.time = lambda: real_time() + 10 * 365 * 86400
    t0 = runner.time.monotonic()
    eng3.running = True
    eng3._sleep(0.3)
    dt = runner.time.monotonic() - t0
    check(dt >= 0.25, f"_sleep(0.3) 实际仍睡了 {dt:.2f}s(墙钟回拨不再让它秒返回)")

    import datetime
    calls = {"n": 0}
    def slow_recognize(cond, screen):
        calls["n"] += 1
        return None if calls["n"] < 3 else "hit"
    eng3._grab = lambda ttl=None: object()
    eng3._recognize_wait = slow_recognize
    t0 = runner.time.monotonic()
    hit = eng3._wait_until({"ui": "主界面"}, 5.0, "测试等待")
    dt = runner.time.monotonic() - t0
    check(hit == "hit" and calls["n"] == 3,
          f"_wait_until 轮询了 {calls['n']} 轮才判成功(没被提前判超时)")
    check(dt < 4.0, f"轮询耗时 {dt:.2f}s, 未在墙钟被拨后异常拉长")
finally:
    runner.time.time = real_time

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
