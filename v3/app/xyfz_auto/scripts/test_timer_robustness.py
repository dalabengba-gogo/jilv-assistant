# -*- coding: utf-8 -*-
"""P0-5 定时任务健壮性 的确定性测试(不依赖模拟器、不真跑任务)

针对 2026-10-18 v4.6.0 的四条修复(以前全是"默默吃掉当天份额"的老毛病):
  1. 任务做成 → 占掉当天份额(今天不再重复跑)
  2. 任务失败 → **退还**当天份额, 冷却后可重试(旧行为: 失败也永久占用份额)
  3. 重试次数用完 → 认了, 占掉份额并明确报案
  4. 重启继承: gui_timer.json 里只保留"今天"的 runs/tries, 隔天的自动丢弃
     (这是"崩溃重启会把当天任务再跑一遍"的直接后果)
  5. 配置兜底: timer_* 缺失或填乱 → 退回默认值, 不让定时彻底失效

用桩对象承载 MainWindow 的这几个方法(它们只用到 _timer_* 几个属性 +
append_sys_log), 不构造整个主窗口 —— 这样跑得快也不会碰硬件。
"""
import os
import sys
import json
import tempfile
from datetime import date, datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from PySide6.QtWidgets import QApplication           # noqa: E402,F401

from gui.main_window import MainWindow               # noqa: E402

FAILS = []
TODAY = date.today().isoformat()
YESTERDAY = (date.today() - timedelta(days=1)).isoformat()


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


class TimerStub:
    """只实现 _timer_done / _restore_timer / _timer_cfg_int 需要的那点东西"""
    def __init__(self, cfg=None, cfg_path=None):
        self.cfg = cfg or {}
        self._timer_runs = {}
        self._timer_try = {}
        self._timer_local_key = None
        self._timer_cfg_path = cfg_path
        self._rows = []
        self.log = []
        self.saved = 0

    def append_sys_log(self, msg):
        self.log.append(msg)

    def _persist_timer_runs(self):
        self.saved += 1

    def _append_timer_row(self, *a):
        self._rows.append(a)

    # 直接借 MainWindow 的真实实现, 保证测的就是线上那份逻辑
    _timer_done = MainWindow._timer_done
    _timer_cfg_int = MainWindow._timer_cfg_int
    _restore_timer = MainWindow._restore_timer


print("[1] 做成 → 占掉当天份额")
s = TimerStub({"engine": {"timer_max_retry": 2}})
key = "09:00|每日必领.yaml|16416"
s._timer_local_key = key
s._timer_done(key, True)
check(s._timer_runs.get(key) == TODAY, "成功 → 记今天已执行")
check(key not in s._timer_try, "成功 → 重试计数清掉")

print("\n[2] 失败 → 退还份额, 可以重试(旧行为在这里错得最离谱)")
s = TimerStub({"engine": {"timer_max_retry": 2}})
s._timer_runs[key] = TODAY
s._timer_done(key, False)
check(key not in s._timer_runs, "第1次失败 → 份额退还(今天还能再跑)")
check(s._timer_try.get(key) == [TODAY, 1], f"重试计数=1(实测 {s._timer_try.get(key)})")
check(any("重试" in m for m in s.log), "系统日志里说明了要重试")

print("\n[3] 重试次数用完 → 认了, 占掉份额")
s._timer_done(key, False)          # 第2次失败 (<=max_retry=2 → 还退)
check(key not in s._timer_runs, f"第2次失败仍在额度内 → 还能重试")
s._timer_done(key, False)          # 第3次 → 超限
check(s._timer_runs.get(key) == TODAY, "超过 timer_max_retry → 占掉当天份额")
check(s._timer_try[key][1] == 3, f"重试计数累计到 3(实测 {s._timer_try[key]})")
check(any("不再重试" in m for m in s.log), "并明确告知今日不再重试")

print("\n[4] 重启继承: 只认今天的记录")
d = tempfile.mkdtemp(prefix="timer_")
p = os.path.join(d, "gui_timer.json")
with open(p, "w", encoding="utf-8") as f:
    json.dump({"entries": [{"time": "09:00", "task": "每日必领.yaml",
                            "acc_port": 16416, "enabled": True}],
               "runs": {key: TODAY, "08:00|五环.yaml|0": YESTERDAY},
               "tries": {key: [TODAY, 2], "07:00|师门.yaml|0": [YESTERDAY, 5]}}, f)
s = TimerStub(cfg_path=p)
s._restore_timer()
check(len(s._rows) == 1, f"定时条目照常读回(实测 {len(s._rows)} 条)")
check(key in s._timer_runs, "今天的执行记录被继承 → 重启后不会重复跑")
check("08:00|五环.yaml|0" not in s._timer_runs, "昨天的执行记录丢弃")
check(key in s._timer_try, "今天的重试计数被继承")
check("07:00|师门.yaml|0" not in s._timer_try, "昨天的重试计数丢弃")

print("\n[5] 配置兜底")
s = TimerStub({})
check(s._timer_cfg_int("timer_max_retry", 2) == 2, "没配 → 用默认")
s = TimerStub({"engine": {"timer_max_retry": 0}})
check(s._timer_cfg_int("timer_max_retry", 2) == 2, "填 0 → 用默认(不能变成永动重试)")
s = TimerStub({"engine": {"timer_grace_minutes": "abc"}})
check(s._timer_cfg_int("timer_grace_minutes", 3) == 3, "填乱码 → 用默认, 不抛异常")

print()
if FAILS:
    print(f"✘ 有 {len(FAILS)} 项未通过:")
    for m in FAILS:
        print("   -", m)
    sys.exit(1)
print("✔ 全部通过")
