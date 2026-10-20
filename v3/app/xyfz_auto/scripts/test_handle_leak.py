# -*- coding: utf-8 -*-
"""桩测试: P1-4 句柄泄漏 (2026-10-17 v3.5.0)

不连模拟器。验证:
  1. LiveReader 有 close(), 且关掉后不再持有文件句柄
  2. 端口消失 + 写方已退出 → _readers 里该端口被摘掉并 close()
  3. 本地端口(_local_port)即使不活跃也**不**被回收(本机任务靠它)
  4. _full_shutdown 会关掉所有 reader
  5. crash_guard 的 faulthandler 句柄可被 close_crash_handle() 收掉
     (且是先 disable 再 close)
"""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, os.getcwd())

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


print("[1] LiveReader.close() 释放句柄")
from live import LiveReader

rd = LiveReader(16384)
check(hasattr(rd, "close"), "LiveReader 有公开 close()")
rd._fp = open(os.devnull, "rb")     # 手工塞一个句柄进去
rd.close()
check(rd._fp is None, f"close() 后 _fp 置空, 实测 {rd._fp}")

print("\n[2] 端口消失 + 写方已退出 → 摘掉并 close()")


class FakeReader:
    def __init__(self, port, active=False):
        self.port = port
        self._active = active
        self.closed = False

    def poll(self):
        return []

    def is_active(self):
        return self._active

    def read_frame(self):
        return None

    def close(self):
        self.closed = True


class FakeStore:
    def __init__(self):
        self.stopped = []
        self.active = {}

    def get(self, port):
        return None

    def mark_stopped(self, port, why):
        self.stopped.append(port)

    def set_active(self, port, v):
        self.active[port] = v


class FakeWin:
    """只跑 _poll_live 里与句柄回收相关的那几行"""
    def __init__(self, local_port, readers, live_ports):
        self._local_port = local_port
        self._readers = readers
        self._live_ports = set(live_ports)
        self.store = FakeStore()
        self._page_sig = None
        self._steps_dirty = False
        self._scan_tick = 0
        self._log_drawn = {}
        self._steps_sig = None

    def _acc_name(self, port):
        return f"端口{port}"

    def _discover_ports(self):
        return list(self._live_ports)

    def _poll_live(self):
        # 与 main_window._poll_live 的回收段保持同构
        self._scan_tick += 1
        if self._scan_tick % 5 == 1:
            self._live_ports = set(self._discover_ports())
            for port in self._live_ports:
                if port != self._local_port and port not in self._readers:
                    self._readers[port] = FakeReader(port)
        live_ports = getattr(self, "_live_ports", None) or set()
        for port, rd in list(self._readers.items()):
            if not rd.is_active():
                self.store.set_active(port, False)
                if port != self._local_port and port not in live_ports:
                    rd.close()
                    self._readers.pop(port, None)


dead = FakeReader(16416, active=False)
alive = FakeReader(16432, active=True)
w = FakeWin(local_port=16384,
            readers={16384: FakeReader(16384, active=False), 16416: dead},
            live_ports={16432})
w._poll_live()
check(16416 not in w._readers, "已消失且不活跃的端口被摘掉")
check(dead.closed is True, "被摘掉的 reader 已 close()")
check(16384 in w._readers, "本地端口保留")

print("\n[3] 本地端口不活跃也**不**被回收(本机任务靠它)")
w2 = FakeWin(local_port=16384, readers={16384: FakeReader(16384, active=False)},
             live_ports=set())
w2._poll_live()
check(16384 in w2._readers and not w2._readers[16384].closed,
      "本地端口即使不活跃也不 close")

print("\n[4] 仍在扫到的端口(只是这一拍不活跃)不回收")
w3 = FakeWin(local_port=16384, readers={16416: FakeReader(16416, active=False)},
             live_ports={16416})
w3._poll_live()
check(16416 in w3._readers, "端口目录还在 → 保留 reader(子进程可能马上再起)")

print("\n[5] crash_guard 句柄可被收掉, 且先 disable 再 close")
import crash_guard

order = []
real_disable = None
try:
    import faulthandler
    real_disable = faulthandler.disable
    faulthandler.disable = lambda: order.append("disable")
except Exception:
    faulthandler = None


class FakeFH:
    def close(self):
        order.append("close")


try:
    crash_guard._FH = FakeFH()
    crash_guard.close_crash_handle()
    check(crash_guard._FH is None, "close_crash_handle() 后句柄置空")
    check(order == ["disable", "close"], f"顺序必须是先 disable 再 close, 实测 {order}")
    before = order[:]
    crash_guard.close_crash_handle()      # 幂等
    check(order == before, "重复调用不报错也不重复动作")
finally:
    if faulthandler is not None and real_disable is not None:
        faulthandler.disable = real_disable
    crash_guard._FH = None

print("\n[6] multi_panel 启动后父进程那份日志句柄已关(Popen 之后 f.close())")
src = open("gui/multi_panel.py", encoding="utf-8").read()
i = src.find("f = open(log_dir")
seg = src[i:i + 1800]
check("f.close()" in seg, "启动流程里有 f.close()")
check(seg.find("Popen") < seg.find("f.close()"),
      "f.close() 必须在 Popen **之后**(先创建进程、再关父进程副本)")
check(seg.count("f.close()") >= 2, "失败分支也要关(不能只关成功路径)")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
