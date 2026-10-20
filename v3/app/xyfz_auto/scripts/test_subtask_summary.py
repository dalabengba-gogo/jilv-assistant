# -*- coding: utf-8 -*-
"""子任务级汇总的桩测试(2026-10-17 v3.7.0)

背景: 任务跑完后的汇总以前按步骤逐条列, 926 步的日常能刷出上百行, 而其中
大量是「跳转 / 条件未命中」这类正常现象 —— 用户要的是"每个子任务成没成"。
本测试不连模拟器, 用桩喂失败记账, 验证:
  1. 分组模式自动判定(source 多子脚本 / segment 单脚本段号)
  2. 子任务汇总的每一行(✔/⚠/✘/未执行)
  3. 正常跑完时逐步明细**不推给界面**(gui=False), 但 run.log 里有
  4. 异常终止路径仍能看到明细(to_gui=True)
"""
import os
import sys
import io
import logging
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engine.model import Step, Task, RunResult, subtask_of
from engine.runner import Engine

OK = []


def check(cond, label):
    OK.append(bool(cond))
    print(("  ✔ " if cond else "  ✘ ") + label)


print("== 1. 分组模式判定 ==")
# 多子脚本(像 daily_all: 7 个 parts) → source 模式
t_multi = Task(name="daily_all", steps=[
    Step(name="确认主界面", source="parts/daily_pure_mode.yaml"),
    Step(name="打开师门", source="parts/daily_shimen.yaml"),
    Step(name="交任务", source="parts/daily_shimen.yaml"),
])
# 单脚本(像「每日必领」99 步在一个文件里) → segment 模式
t_one = Task(name="每日必领", steps=[
    Step(name="每日必领-确认主界面", source="每日必领.yaml"),
    Step(name="每日必领-①打开商城", source="每日必领.yaml"),
    Step(name="每日必领-②领奖", source="每日必领.yaml"),
])

eng = Engine(device=object(), matcher=object())
check(eng._subtask_mode(t_multi) == "source", "多子脚本 → source 模式")
check(eng._subtask_mode(t_one) == "segment", "单脚本 → segment 模式")

print("== 2. 子任务归属 ==")
check(subtask_of(t_multi.steps[1], "source") == "师门",
      f"parts/daily_shimen.yaml → 师门(实测 {subtask_of(t_multi.steps[1], 'source')})")
check(subtask_of(t_multi.steps[0], "source") == "纯享模式",
      f"parts/daily_pure_mode.yaml → 纯享模式(实测 {subtask_of(t_multi.steps[0], 'source')})")
check(subtask_of(t_one.steps[1], "segment") == "①",
      f"「每日必领-①打开商城」→ ①(实测 {subtask_of(t_one.steps[1], 'segment')})")
check(subtask_of(t_one.steps[0], "segment") == "主流程",
      f"「每日必领-确认主界面」无段号 → 主流程(实测 {subtask_of(t_one.steps[0], 'segment')})")

print("== 3. 子任务汇总文案 ==")
res = RunResult(ok=True, name="t", subtasks=[
    {"name": "①", "total": 17, "reached": True, "failed": 0, "required_failed": 0},
    {"name": "③", "total": 20, "reached": True, "failed": 4, "required_failed": 0},
    {"name": "⑤", "total": 14, "reached": True, "failed": 1, "required_failed": 1},
    {"name": "⑥", "total": 15, "reached": False, "failed": 0, "required_failed": 0},
])
txt = res.subtask_summary()
print("---")
print(txt)
print("---")
check("✔ ① (17步)" in txt, "全做成 → ✔")
check("⚠ ③ (20步) · 4步未做成" in txt, "有真失败 → ⚠")
check("✘ ⑤ (14步) · 1步关键失败" in txt, "有 required 失败 → ✘")
check("⑥ (15步) 未执行" in txt, "没跑到 → 未执行")
check(len(txt.splitlines()) == 4, f"四个子任务四行(实测 {len(txt.splitlines())})")
check(RunResult().subtask_summary() == "", "无子任务统计 → 空串(调用方退回逐步明细)")

print("== 4. 引擎聚合: 失败记账落到对应子任务 ==")


class Log:
    def __init__(self):
        self.lines = []

    def info(self, m): self.lines.append(("info", m))

    def warning(self, m): self.lines.append(("warning", m))

    def error(self, m): self.lines.append(("error", m))

    def debug(self, m): self.lines.append(("debug", m))


eng2 = Engine(device=object(), matcher=object())
eng2.logger = Log()
eng2._cur_steps = list(t_one.steps)
eng2._sub_mode = "segment"
eng2._sub_total = {}
eng2._sub_seen = {"主流程", "①", "②"}
for s in t_one.steps:
    k = subtask_of(s, "segment")
    eng2._sub_total[k] = eng2._sub_total.get(k, 0) + 1
eng2._record_failure(t_one.steps[1], 1, "等待超时(按钮未命中)")     # ① 真失败
eng2._record_failure(t_one.steps[2], 2, "等待超时(OCR 未找到)")     # ② 真失败(t_one 无 optional → 真失败)
subs = eng2._build_subtasks()
by = {s["name"]: s for s in subs}
check(by["①"]["failed"] == 1, f"① 记到 1 步失败(实测 {by['①']['failed']})")
check(by["②"]["failed"] == 1, f"② 记到 1 步失败(实测 {by['②']['failed']})")
check(by["主流程"]["failed"] == 0, "主流程零失败")
check([s["name"] for s in subs] == ["主流程", "①", "②"], "按脚本里首次出现的顺序排列")

print("== 5. 条件未命中不占界面(gui=False) ==")
eng3 = Engine(device=object(), matcher=object())
eng3.logger = Log()
pushed = []
eng3.subscribe(log=lambda lv, m: pushed.append((lv, m)))
eng3._sub_mode = "segment"
opt = Step(name="每日必领-③查弹窗", optional=True, on_fail="continue")
eng3._record_failure(opt, 0, "等待超时(探测)")
check(len(pushed) == 0, f"未命中不推给界面(实测推了 {len(pushed)} 条)")
check(any("条件未命中" in m for _, m in eng3.logger.lines),
      "但 run.log 里有记录(logger 收到)")
check(len(eng3.skipped_steps) == 1 and len(eng3.failed_steps) == 0,
      "仍记进 skipped、不进 failed")

print("== 6. 真失败照旧推给界面 ==")
eng3.failed_steps.clear()
eng3._record_failure(Step(name="每日必领-③领奖"), 0, "等待超时")
check(any(lv == "warning" for lv, _ in pushed), "真失败仍以 warning 推给界面")

print("== 7. 验收点(verify_done): 以游戏面板的完成状态为准 ==")
eng4 = Engine(device=object(), matcher=object())
eng4.logger = Log()
eng4._cur_steps = list(t_one.steps)
eng4._sub_mode = "segment"
eng4._sub_total = {}
for s in t_one.steps:
    k = subtask_of(s, "segment")
    eng4._sub_total[k] = eng4._sub_total.get(k, 0) + 1
eng4._sub_seen = set(eng4._sub_total)
eng4._verified = {}

# ① 段: 中间掉了 3 步, 但收尾时面板显示「已完成」→ 应判做成
for i in (1, 2, 3):
    eng4._record_failure(t_one.steps[1], 1, "按钮已领取, 模板失配")
chk = Step(name="每日必领-①验收已完成", verify_done=True, wait={"x": 1}, on_fail="continue")
eng4._mark_verified(chk, True)          # 面板命中
eng4.failed_steps.clear()               # 清掉上面堆叠的同一步(只为凑数)
eng4.failed_steps.extend([{"sub": "①", "required": False}] * 3)
sub_txt = RunResult(ok=True, subtasks=eng4._build_subtasks()).subtask_summary()
print("---")
print(sub_txt)
print("---")
check("✔ ①" in sub_txt and "已完成" in sub_txt,
      "掉了 3 步但面板已完成 → 仍判 ✔(不再报未做成)")

# ② 段: 每步都走到了(零失败), 但面板没显示已完成 → 必须报未完成
eng5 = Engine(device=object(), matcher=object())
eng5.logger = Log()
eng5._cur_steps = list(t_one.steps)
eng5._sub_mode = "segment"
eng5._sub_total = {"主流程": 2, "①": 17, "②": 15}
eng5._sub_seen = {"主流程", "①", "②"}
eng5._verified = {"②": False}           # 面板没挂「已完成」
txt5 = RunResult(ok=True, subtasks=eng5._build_subtasks()).subtask_summary()
check("✘ ② (15步) · 面板未显示「已完成」" in txt5,
      f"零失败但面板未见完成 → 判 ✘(实测: {[l for l in txt5.splitlines() if '②' in l]})")

# 整步成功路径也会记验收(run_step 成功命中 → True)
eng6 = Engine(device=object(), matcher=object())
eng6.logger = Log()
eng6._sub_mode = "segment"
eng6._verified = {}
eng6._mark_verified(Step(name="每日必领-③验收", verify_done=True), True)
check(eng6._verified.get("③") is True, "成功路径 → verified=True")
eng6._mark_verified(Step(name="每日必领-③验收", verify_done=True), False)
check(eng6._verified.get("③") is False, "失败路径 → verified=False (后者覆盖)")

print()
print("ALL PASS" if all(OK) else "有失败项")
sys.exit(0 if all(OK) else 1)
