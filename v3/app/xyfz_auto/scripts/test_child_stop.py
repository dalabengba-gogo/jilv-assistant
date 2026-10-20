# -*- coding: utf-8 -*-
"""桩/真机混合测试: 手动停止后状态不再卡在「运行中」 (2026-10-17 v4.0.1)

用户实测: 19:15 手动停止账号2(端口16416) → 状态灯右边一直显示「运行中」。
根因: LiveReader.is_active() 只用 OpenProcess 判活, 而主进程自己 Popen 着
这个子进程、握着它的句柄 → 进程退出后 OpenProcess 照样成功 → 永远"活着"
→ _poll_live 里那段"子进程已退出"兜底从未生效过。

验证:
  1. is_active() 对**已退出**的子进程返回 False(关键回归)
  2. on_child_finished 会把 running 通道收口成 stopped(不依赖 pid 判活)
  3. 正常跑完的子进程(已写 task_end)不会被误改成 stopped
  4. 点「停止」但该端口已无子进程 → 孤儿"运行中"状态被清掉
"""
import os
import subprocess
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, os.getcwd())

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


from gui.main_window import MainWindow
# 2026-10-18 v4.7.0: 同 _gui_stub —— 屏蔽免责声明弹窗, 并把后台线程改同步执行。
#   否则 MainWindow 构造时起的 QThread 在脚本退出时被回收 → Qt qFatal →
#   进程以 0xC0000409 硬崩(表面 ALL PASS, 退出码却非 0)。
from scripts._gui_stub import no_disclaimer, sync_background
no_disclaimer(MainWindow)
sync_background(MainWindow)

print("[1] is_active(): 已退出的子进程不能再算活着")
p = subprocess.Popen([sys.executable, "-c", "pass"])
dead_pid = p.pid
p.wait()                      # 已退出, 但主进程仍握着它的句柄(正同线上场景)
import ctypes
k = ctypes.windll.kernel32
h = k.OpenProcess(0x1000, False, dead_pid)
check(bool(h), f"前置条件: OpenProcess 对已死 pid {dead_pid} 仍然成功(老实现就栽在这)")
if h:
    k.CloseHandle(h)

from live import LiveReader
rd = LiveReader(16416)
rd.meta = {"pid": dead_pid}
check(rd.is_active() is False,
      f"is_active() 判为已死(老实现返回 True → 状态永远卡住), 实测 {rd.is_active()}")
rd.meta = {"pid": os.getpid()}
check(rd.is_active() is True, "自己这个活着的进程仍判为活着")
rd.meta = {"pid": 0}
check(rd.is_active() is False, "没有 pid 视作已结束")

print("\n[2] on_child_finished 把 running 通道收口(用户场景)")
w = MainWindow()
PORT = 16416
w.store.ensure(PORT, name="账号2", source="child")
w.store.reset(PORT, task="域外风情.yaml", source="child")
w._set_port_state(PORT, "停止中…", "#c80", "已请求终止子进程")
w._refresh_port_states()
check(w._port_state_labels[PORT].text() == "停止中…", "点停止后先显示「停止中…」")
w.on_child_finished(PORT)
w._refresh_port_states()
txt = w._port_state_labels[PORT].text()
check(w.store.get(PORT).state == "stopped", f"通道收口成 stopped, 实测 {w.store.get(PORT).state}")
check(txt == "已停止", f"★ 不再卡在「运行中」, 实测 {txt!r}")

print("\n[3] 正常跑完的子进程不被误改(先写 task_end 再回调)")
w.store.reset(PORT, task="域外风情.yaml", source="child")
w.store.finish(PORT, True, "成功")
w.on_child_finished(PORT)
w._refresh_port_states()
check(w.store.get(PORT).state == "ok", f"仍是 ok, 实测 {w.store.get(PORT).state}")
check(w._port_state_labels[PORT].text() == "完成 ✔",
      f"仍显示完成, 实测 {w._port_state_labels[PORT].text()!r}")

print("\n[4] 点「停止」但该端口已无子进程 → 清掉孤儿「运行中」")
w.store.reset(PORT, task="域外风情.yaml", source="child")
w._refresh_port_states()
check(w._port_state_labels[PORT].text() == "运行中", "前置: 孤儿状态显示「运行中」")
w.panel_multi.stop_port = lambda port: False      # 没有子进程可停
w._target_port = lambda: PORT
w._stop_task()
w._refresh_port_states()
check(w.store.get(PORT).state == "stopped", f"被收口, 实测 {w.store.get(PORT).state}")
check(w._port_state_labels[PORT].text() == "已停止",
      f"★ 显示已停止, 实测 {w._port_state_labels[PORT].text()!r}")

print("\n[5] 本机手动停止不再被显示成「失败」")
src = open("gui/main_window.py", encoding="utf-8").read()
seg = src[src.find("def _on_finished"):]
seg = seg[:seg.find("def _poll_live")]
# stopped 分支必须排在失败(else) 分支之前 —— 否则用户自己点的停止会被当成失败
check('elif stopped:' in seg, "_on_finished 里有 stopped 分支")
check(seg.find('elif stopped:') < seg.find('else:'),
      "stopped 分支排在 失败(else) 分支之前")
check('mark_stopped(self._local_port)' in seg,
      "stopped 走 mark_stopped(落 stopped 通道, 非 finish(False))")
# 端到端: stopped 通道在显示层派生为「已停止」(窗口内), 而非「失败 ✘」
w.store.reset(PORT, task="域外风情.yaml", source="child")
w.store.mark_stopped(PORT)
w._refresh_port_states()
txt5 = w._port_state_labels[PORT].text()
check(txt5 == "已停止", f"stopped 通道显示「已停止」(非「失败 ✘」), 实测 {txt5!r}")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
