# -*- coding: utf-8 -*-
"""P1-4 优雅停止 + P1-5 端口所有权 的确定性测试(不依赖模拟器)

验证:
  P1-4 优雅停止:
    1. stop_port() 走协作式: 写停止标记文件 + 行显示「停止中」, 进程活着时不硬杀
    2. terminate_port() 才硬杀(孤儿回收用), 且不直接除名
    3. _poll_procs(): 退出码 3(用户停止) → 行显示「已停止」; 退出码 0 → ✅成功
    4. _poll_procs(): 协作停止超时(GRACE)仍不退出 → 强制硬杀
  P1-5 端口所有权:
    5. _collect_rows() 拦截同一端口被多行账号占用(串号根因)
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from PySide6.QtWidgets import QApplication

from gui.multi_panel import MultiPanel, STOP_GRACE_SECONDS

app = QApplication.instance() or QApplication([])

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


class FakeMainwin:
    """MultiPanel 需要的 mainwin 桩(本测试不真正启动子进程)"""
    def on_child_finished(self, port):
        pass
    def append_sys_log(self, msg):
        pass


class FakePopen:
    def __init__(self, code=None):
        self._code = code          # None=活着; int=已退出且退出码
        self.terminated = False
    def poll(self):
        return self._code
    def terminate(self):
        self.terminated = True


mp = MultiPanel(FakeMainwin())
PORT = 12345


print("[1] stop_port() 协作式: 写标记 + 显示「停止中」, 不硬杀")
mp._procs = {PORT: {"p": FakePopen(None), "row": 0, "start": time.time()}}
ok = mp.stop_port(PORT)
check(ok is True, "stop_port 返回 True(已发停止信号)")
flag = mp._stop_flag_path(PORT)
check(flag.exists(), "停止标记文件已写入 logs/live/port_<port>/stop.flag")
check(mp._procs[PORT].get("stop_req") is not None, "记录 stop_req 时间戳")
check(mp.tbl.item(0, 5).text() == "停止中", "状态列先显示「停止中」")
check(mp._procs[PORT]["p"].terminated is False, "进程活着时不硬杀(TerminateProcess)")
# 清理标记文件
try:
    flag.unlink()
except OSError:
    pass
try:
    flag.parent.rmdir()
except OSError:
    pass

print("\n[2] terminate_port() 才硬杀, 且不直接除名")
mp._procs = {PORT: {"p": FakePopen(None), "row": 0, "start": time.time()}}
mp.terminate_port(PORT)
check(mp._procs[PORT]["p"].terminated is True, "terminate_port 调了 p.terminate()")
check(PORT in mp._procs, "terminate_port 不直接除名(留给 _poll_procs 收口)")

print("\n[3] _poll_procs(): 退出码 3 → 「已停止」; 0 → ✅成功")
mp._procs = {PORT: {"p": FakePopen(3), "row": 0, "start": time.time(),
                    "stop_req": time.time()}}
mp._poll_procs()
check(mp.tbl.item(0, 5).text() == "已停止", "退出码 3(用户停止) → 「已停止」")
check(PORT not in mp._procs, "已退出的端口被收口除名")
mp._procs = {PORT: {"p": FakePopen(0), "row": 1, "start": time.time()}}
mp._poll_procs()
check(mp.tbl.item(1, 5).text() == "✅ 成功", "退出码 0 → 「✅成功」")

print("\n[4] _poll_procs(): 协作停止超时仍不退出 → 强制硬杀")
fp = FakePopen(None)
mp._procs = {PORT: {"p": fp, "row": 0, "start": time.time(),
                    "stop_req": time.time() - (STOP_GRACE_SECONDS + 10)}}
mp._poll_procs()
check(fp.terminated is True, "超过 STOP_GRACE_SECONDS 仍存活 → 强制 terminate")
check(mp.tbl.item(0, 5).text() == "停止中(强制)", "行显示「停止中(强制)」")
check(PORT in mp._procs, "强制杀之前仍记为运行中(等真正退出再收口)")

print("\n[5] P1-5: _collect_rows() 拦截重复端口(串号根因)")
# 复用 mp 的表格: 把前两行的端口都改成同一个值
mp.tbl.setItem(0, 2, __import__("PySide6").QtWidgets.QTableWidgetItem("16600"))
mp.tbl.setItem(1, 2, __import__("PySide6").QtWidgets.QTableWidgetItem("16600"))
raised = None
try:
    mp._collect_rows()
except Exception as e:
    raised = e
check(raised is not None and "端口重复" in str(raised),
      f"两行同端口被拦截: {raised}")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
