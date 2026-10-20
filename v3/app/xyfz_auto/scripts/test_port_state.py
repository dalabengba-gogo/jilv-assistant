# -*- coding: utf-8 -*-
"""桩测试: 状态灯右侧「每账号任务状态」列 (2026-10-17 v3.10.0)

用 offscreen Qt + 桩设备(不连模拟器)。核心验证:
  1. 启动按钮下面那行红字已经没了(lbl_run_state 被移除)
  2. 每个端口都有一盏灯 + 一个状态标签, 且状态在灯的**右侧同一行**
  3. 状态以通道为准自动推导: 空闲/运行中/完成✔/⚠N步失败/失败/已停止
  4. 多开子进程与本机走同一套推导(不靠主界面额外记账)
  5. 本机点停止 → 显示"停止中 Ns"秒表, worker 退出后自动收掉
  6. _set_port_state 临时接管 → _clear_port_state 后回到自动推导
  7. 文字够短(左侧面板只有 260px), 详情进 tooltip
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, os.getcwd())

from gui.lights_mixin import PORT_STATE_REVERT_SECONDS

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


# 300ms 后的免责声明在 offscreen 下没人点 → 模态框堵死 processEvents, 先屏蔽
from gui.main_window import MainWindow
# 2026-10-18 v4.7.0: 免责声明弹窗 + 后台线程, 统一走公共桩。
#   后台那条尤其关键 —— MainWindow 构造时会起 QThread 重载模板, 脚本跑完时它
#   若未结束就被回收, Qt 直接 qFatal → 进程以 0xC0000409 硬崩: 屏幕上一片
#   「ALL PASS」而退出码非 0, CI 判失败还查不出所以然。详见 _gui_stub。
from scripts._gui_stub import no_disclaimer, sync_background
no_disclaimer(MainWindow)
sync_background(MainWindow)

src = open("gui/main_window.py", encoding="utf-8").read()
import accounts as _acc
ports = sorted(int(a["port"]) for a in _acc.load_accounts())
P = ports[0]
Q = ports[1] if len(ports) > 1 else ports[0]

print("[1] 启动按钮下面那行状态文字已移除")
check("lbl_run_state" not in src, "源码里已无 lbl_run_state")
w = MainWindow()
check(not hasattr(w, "lbl_run_state"), "MainWindow 实例上也没有 lbl_run_state")

# 2026-10-19 fix: 测试中状态灯默认 _online=None(没真探测)。派生逻辑里
# "离线即空闲"会因此把一切判成空闲, 与"在线跑任务"的常态不符。这里把灯都标成
# 在线, 还原常态; 离线场景在下面的 [9] 单独测。
for _p, _lt in w._port_lights.items():
    _lt._online = True

print("\n[2] 每端口一盏灯 + 一个状态标签, 状态在灯右侧同一行")
check(set(w._port_state_labels.keys()) == set(w._port_lights.keys()),
      f"灯与状态标签一一对应, 实测 {sorted(w._port_state_labels.keys())}")
row = w._port_state_labels[P].parentWidget()
lay = row.layout()
check(lay.indexOf(w._port_lights[P]) < lay.indexOf(w._port_state_labels[P]),
      "状态标签排在灯的右边(同一行)")
check(w._port_state_labels[P].alignment() & 0x0002 != 0, "状态文字右对齐")

print("\n[3] 状态按通道自动推导")
w.store.ensure(P, name="本机", source="local")
w.store.ensure(Q, name="账号2", source="child")
w._refresh_port_states()
check(w._port_state_labels[P].text() == "空闲",
      f"没跑任务 → 空闲, 实测 {w._port_state_labels[P].text()!r}")

w.store.reset(P, task="日常总览")
w._refresh_port_states()
check(w._port_state_labels[P].text() == "运行中",
      f"跑起来 → 运行中, 实测 {w._port_state_labels[P].text()!r}")
check("日常总览" in w._port_state_labels[P].toolTip(),
      "tooltip 里带脚本名(左侧窄, 正文放不下)")

w.store.finish(P, True, "成功")
w._refresh_port_states()
check(w._port_state_labels[P].text() == "完成 ✔",
      f"零失败 → 完成 ✔, 实测 {w._port_state_labels[P].text()!r}")

w.store.finish(P, True, "完成但有13步失败")
w._refresh_port_states()
check(w._port_state_labels[P].text() == "⚠13步失败",
      f"有失败 → ⚠N步失败, 实测 {w._port_state_labels[P].text()!r}")
check("#c80" in w._port_state_labels[P].styleSheet(), "⚠ 用橙色(不是红色, 任务本身跑完了)")

w.store.finish(P, False, "步骤[地煞-进入] 超时")
w._refresh_port_states()
check(w._port_state_labels[P].text() == "失败 ✘",
      f"真失败 → 失败 ✘, 实测 {w._port_state_labels[P].text()!r}")
check("#c33" in w._port_state_labels[P].styleSheet(), "失败用红色")
check("超时" in w._port_state_labels[P].toolTip(), "完整原因进 tooltip")

w.store.reset(P, task="日常总览")     # 手动停止只发生在"正在跑"的通道上
w.store.mark_stopped(P)
w._refresh_port_states()
check(w._port_state_labels[P].text() == "已停止",
      f"手动停 → 已停止, 实测 {w._port_state_labels[P].text()!r}")

print("\n[4] 多开子进程走同一套推导(不靠主界面额外记账)")
w.store.reset(Q, descs=["a", "b"], task="每日必领", name="账号2", source="child")
w._refresh_port_states()
check(w._port_state_labels[Q].text() == "运行中" and
      w._port_state_labels[P].text() == "已停止",
      f"两个号各显示各的, 实测 P={w._port_state_labels[P].text()!r} "
      f"Q={w._port_state_labels[Q].text()!r}")

print("\n[5] 本机点停止 → 秒表, worker 退出后自动收掉")
w.store.reset(P, task="日常总览")
w._stop_t0 = time.time() - 3
w._refresh_port_states()
txt = w._port_state_labels[P].text()
check(txt.startswith("停止中") and "3" in txt, f"显示秒表, 实测 {txt!r}")
w._stop_t0 = None
w._refresh_port_states()
check(w._port_state_labels[P].text() == "运行中", "收掉标志位后回到「运行中」")

print("\n[6] 临时接管 / 交回自动推导")
w._set_port_state(Q, "停止中…", "#c80", "已请求终止子进程")
check(w._port_state_labels[Q].text() == "停止中…", "接管生效")
w._refresh_port_states()
check(w._port_state_labels[Q].text() == "停止中…", "刷新不会被自动推导盖掉")
w.on_child_finished(Q)
w.store.finish(Q, False, "子进程异常退出")
w._refresh_port_states()
check(w._port_state_labels[Q].text() == "失败 ✘",
      f"子进程结束后交回推导, 实测 {w._port_state_labels[Q].text()!r}")
check("子进程异常退出" in w._port_state_labels[Q].toolTip(), "tooltip 带子进程给出的原因")

print("\n[7] 文字够短(左侧面板 260px), 详情在 tooltip")
for p, lbl in w._port_state_labels.items():
    check(len(lbl.text()) <= 10, f":{p} 状态文字 {lbl.text()!r} 不超过 10 字符")
check(w._port_state_labels[P].minimumWidth() >= 40,
      "状态列有保底宽度(账号名再长也不会被挤没)")
check(f":{P}" in w._port_lights[P].toolTip(), "灯的 tooltip 补上了完整名+端口")

print("\n[8] 启动新任务会清掉上一轮的结论")
w._set_port_state(P, "完成 ✔", "#2a6", "")
w._clear_port_state(P)
w.store.reset(P, task="日常总览")
w._refresh_port_states()
check(w._port_state_labels[P].text() == "运行中",
      f"不会挂着上一次的「完成」, 实测 {w._port_state_labels[P].text()!r}")

print("\n[9] 2026-10-19 fix: 结论展示数秒后自动归位空闲 / 离线立即归位空闲")
# 9a 终态结论在宽限窗口内仍显示, 窗口外自动回空闲
w.store.reset(P, task="日常总览")
w.store.finish(P, True, "成功")
w._refresh_port_states()
check(w._port_state_labels[P].text() == "完成 ✔",
      f"刚结束(窗口内) → 完成 ✔, 实测 {w._port_state_labels[P].text()!r}")
w.store.get(P).t1 = time.time() - (PORT_STATE_REVERT_SECONDS + 5)
w._refresh_port_states()
check(w._port_state_labels[P].text() == "空闲",
      f"过了宽限窗口 → 自动归位空闲, 实测 {w._port_state_labels[P].text()!r}")

# 9b 设备离线时, 任何历史结论立即回空闲(即使刚结束)
w._port_lights[P]._online = False
w.store.reset(P, task="日常总览")
w.store.finish(P, True, "成功")      # t1=now, 窗口内, 但设备已离线
w._refresh_port_states()
check(w._port_state_labels[P].text() == "空闲",
      f"离线 → 立即空闲(不挂上一轮结论), 实测 {w._port_state_labels[P].text()!r}")
w._port_lights[P]._online = True     # 复原, 不影响后续

# 9c 离线+运行中 也应回空闲(设备都不在, 谈不上在跑)
w.store.reset(P, task="日常总览")
w._port_lights[P]._online = False
w._refresh_port_states()
check(w._port_state_labels[P].text() == "空闲",
      f"离线+运行中 → 立即空闲, 实测 {w._port_state_labels[P].text()!r}")
w._port_lights[P]._online = True

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
