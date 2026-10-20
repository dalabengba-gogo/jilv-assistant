# -*- coding: utf-8 -*-
"""桩测试: P1-2 跨线程共享状态 + P1-3 worker 生命周期 (2026-10-17 v3.5.0)

不连模拟器 / 不真的起任务线程。验证:
  P1-2
  1. matcher.reload() 是「整体替换」而非「原地 clear 再逐条填」
     —— 装载期间另一个线程遍历不会撞 "dictionary changed size during iteration"
  2. 并发压测: 一个线程反复 reload, 另一个反复遍历/读取, 不抛异常且读到的
     永远是"完整的一套"(不会是半新半旧)
  3. worker 信号在主窗口里显式用 QueuedConnection 连接

  P1-3
  4. _on_worker_finished 会 disconnect + deleteLater + 引用置空
     (否则退出时 QThread: Destroyed while thread is still running)
  5. _retire_worker: 正常退出 → True; 等不到 → terminate 且记 crash.log
  6. _full_shutdown 走的是 _retire_worker(不再是裸 wait)
"""
import os
import sys
import threading
import time

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, os.getcwd())

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


# ---------------- P1-2 ----------------
print("[1] TemplateMatcher.reload() 是整体替换")
import inspect
from recognition.template import TemplateMatcher

src = inspect.getsource(TemplateMatcher._load)
# 只看**代码行**: 注释里为了说明旧写法会提到 clear(), 别被它误伤
code_lines = [l for l in src.splitlines() if not l.strip().startswith("#")]
check(not any(".clear()" in l for l in code_lines), "_load 的代码行里没有 clear()")
rsrc = inspect.getsource(TemplateMatcher.reload)
rcode = [l for l in rsrc.splitlines() if not l.strip().startswith("#")]
check(not any(".clear()" in l for l in rcode), "reload() 也不再原地 clear()")
check("loaded = {}" in src or "loaded={}" in src.replace(" ", ""),
      "先装进局部 dict")
check("self.templates = loaded" in src, "读完一次性整体赋值(原子切换)")

print("\n[2] 并发压测: 一个线程反复 reload, 另一个反复遍历")
import tempfile
import numpy as np
import cv2
from common import imwrite_unicode

d = tempfile.mkdtemp(prefix="tpl_")
N = 12
for i in range(N):
    img = np.full((20, 20, 3), (i * 20) % 256, dtype=np.uint8)
    imwrite_unicode(os.path.join(d, f"t{i}.png"), img)

m = TemplateMatcher(d)
errors = []
stop = threading.Event()


def reloader():
    try:
        while not stop.is_set():
            m.reload()
            time.sleep(0.001)
    except Exception as e:
        errors.append(f"reload 线程: {type(e).__name__}: {e}")


def reader():
    try:
        n = 0
        while not stop.is_set() and n < 300:
            names = m.names()                      # 遍历 keys
            if len(names) not in (0, N):           # 只可能看到 空 或 完整一套
                errors.append(f"读到半套模板: {len(names)} 个")
            for nm in names:
                _ = m.templates[nm]                # 取值
            n += 1
    except Exception as e:
        errors.append(f"读线程: {type(e).__name__}: {e}")


t1 = threading.Thread(target=reloader, daemon=True)
t2 = threading.Thread(target=reader, daemon=True)
t1.start()
t2.start()
time.sleep(1.2)
stop.set()
t1.join(3)
t2.join(3)
check(not errors, f"并发无异常/无半套, 错误: {errors[:3]}")
check(len(m.names()) == N, f"压测后模板数仍为 {N}, 实测 {len(m.names())}")

print("\n[3] 信号连接显式 QueuedConnection")
msrc = open("gui/main_window.py", encoding="utf-8").read()
i = msrc.find("self.worker = TaskWorker(")
seg = msrc[i:i + 1600]
for sig in ("log", "screenshot", "step_start", "step_end"):
    line = [l for l in seg.splitlines() if f"self.worker.{sig}.connect" in l]
    check(bool(line) and "QC" in line[0], f"{sig} 用了 QC(QueuedConnection)")
check("task_finished.connect" in seg and "QC" in seg, "task_finished 用了 QC")
check("self.worker.finished.connect" in seg, "额外接了 finished(用于回收)")

# ---------------- P1-3 ----------------
print("\n[4] _on_worker_finished: disconnect + deleteLater + 置空")


class FakeSig:
    def __init__(self):
        self.disconnected = 0

    def disconnect(self, *a):        # 真实 Qt 信号可带参 disconnect, 桩要兼容
        self.disconnected += 1


class FakeWorker:
    def __init__(self):
        self.log = FakeSig()
        self.screenshot = FakeSig()
        self.step_start = FakeSig()
        self.step_end = FakeSig()
        self.task_finished = FakeSig()
        self.finished = FakeSig()
        self.deleted = False

    def deleteLater(self):
        self.deleted = True


class FakeWin:
    """只挂 _on_worker_finished, 其余属性用不上的都给个空壳"""
    def __init__(self):
        from gui.main_window import MainWindow
        self.__class__ = type("W", (FakeWin,), {"__init__": lambda s: None})
        self.worker = FakeWorker()

    def _update_run_buttons(self):
        self.buttons_updated = True


from gui.main_window import MainWindow

w = FakeWin()
w.buttons_updated = False
MainWindow._on_worker_finished(w)
check(w.worker is None, "self.worker 已置空(避免 C++ 对象已删还去调 isRunning)")
fw = FakeWorker()
w2 = FakeWin()
w2.worker = fw
w2.buttons_updated = False
saved = fw
MainWindow._on_worker_finished(w2)
check(saved.deleted is True, "deleteLater() 被调用")
check(sum(s.disconnected for s in (saved.log, saved.screenshot, saved.step_start,
                                  saved.step_end, saved.task_finished, saved.finished)) >= 6,
      "6 路信号全部 disconnect")
check(w2.buttons_updated, "回收后刷新了按钮状态")
# 幂等: 重复调用不炸
MainWindow._on_worker_finished(w2)

print("\n[5] _retire_worker: 正常退出 / 卡死两条路")


class W2(FakeWin):
    pass


class StoppableWorker(FakeWorker):
    def __init__(self, wait_result=True, stop_calls=None):
        super().__init__()
        self._wait = wait_result
        self.stopped = False
        self.terminated = False
        self.waits = []

    def stop(self):
        self.stopped = True

    def wait(self, ms):
        self.waits.append(ms)
        return self._wait

    def terminate(self):
        self.terminated = True


crash_written = []
import crash_guard
real_write = crash_guard.write_crash
crash_guard.write_crash = lambda t, x="": crash_written.append(t)


class Host:
    def __init__(self, worker, wait_ret):
        self.worker = worker
        self._wait_ret = wait_ret
        self.updated = False

    def _update_run_buttons(self):
        self.updated = True


try:
    # 5a 正常退出
    wk = StoppableWorker(wait_result=True)
    h = FakeWin()
    h.worker = wk
    h.buttons_updated = False
    ok = MainWindow._retire_worker(h, 5000)
    check(ok is True, "wait 成功 → 返回 True")
    check(wk.stopped, "先 stop() 请求停止")
    check(not wk.terminated, "正常退出不 terminate")
    check(not crash_written, "正常退出不写 crash.log")

    # 5b 卡死
    wk2 = StoppableWorker(wait_result=False)
    h2 = FakeWin()
    h2.worker = wk2
    h2.buttons_updated = False
    ok2 = MainWindow._retire_worker(h2, 5000)
    check(ok2 is False, "wait 失败 → 返回 False")
    check(wk2.terminated, "超时后强制 terminate")
    check(len(wk2.waits) == 2, f"共 wait 两次(等 + terminate 后再等), 实测 {wk2.waits}")
    check(any("WORKER NOT STOPPING" in t for t in crash_written),
          f"卡死情况写进 crash.log 留证据, 实测 {crash_written}")

    # 5c 没有 worker
    h3 = FakeWin()
    h3.worker = None
    check(MainWindow._retire_worker(h3, 100) is True, "没有 worker → 直接 True")
finally:
    crash_guard.write_crash = real_write

print("\n[6] _full_shutdown 走 _retire_worker")
fsrc = msrc[msrc.find("def _full_shutdown"): msrc.find("def _full_shutdown") + 2200]
check("_retire_worker" in fsrc, "_full_shutdown 里调用了 _retire_worker")
check("self.worker.wait(5000)" not in fsrc, "不再是裸 wait(超时也照样往下走)")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
