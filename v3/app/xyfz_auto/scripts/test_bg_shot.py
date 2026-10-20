# -*- coding: utf-8 -*-
"""桩测试: P1-1 截图/采集/重载模板 后台化 (2026-10-17 v3.5.0)

用 offscreen Qt + 桩设备(不连模拟器)。核心验证:
  1. 截图动作**不在主线程**执行(以前点一下按钮界面冻结 0.6~4.6 秒)
  2. 调用 _manual_screenshot() 后主线程立即返回, 不阻塞
  3. 结果回到主线程后画面被写进通道
  4. 失败也回主线程弹提示, 且不炸
  5. FuncWorker 的异常走 failed 而不是冒泡
  6. _run_bg 期间线程被持有, 结束后摘除 + deleteLater(防 GC 拆线程)
  7. 采集模板: 没画面时走"截图完成后回调重入", 而不是同步等
  8. matcher.reload() 走后台
  9. 缩略图用 FastTransformation; _poll_live 间隔 800ms
"""
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, os.getcwd())

from PySide6.QtCore import QTimer, QEventLoop
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from scripts._gui_stub import gui_sources, method_source

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


def spin(seconds=3.0, until=None):
    """转事件循环让跨线程信号有机会投递。

    不用 QEventLoop.exec() —— offscreen 下它会一直不返回(无窗口系统的
    QEventLoop 没有外部事件来打断), 用 processEvents 忙等更稳。
    `until` 是可选的可调用对象, 满足即提前结束。
    """
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        if until is not None and until():
            return
        time.sleep(0.02)
    app.processEvents()


MAIN_THREAD = app.thread()

# 启动 300ms 后会自动弹免责声明(QTimer.singleShot) —— offscreen 下没人点,
# 模态框会把 processEvents 永久堵住。测试里先屏蔽掉。
from gui.main_window import MainWindow as _MW
_MW._maybe_show_disclaimer = lambda self: None

# 同理: 失败路径会弹 QMessageBox, offscreen 下也是死堵。全部替换成记录。
from PySide6.QtWidgets import QMessageBox
MSGS = []
QMessageBox.warning = lambda *a, **k: MSGS.append(("warning", str(a[-1] if a else "")))
QMessageBox.information = lambda *a, **k: MSGS.append(("info", str(a[-1] if a else "")))

print("[1] 截图动作跑在后台线程(不在主线程)")
from gui.main_window import MainWindow
from gui.worker import FuncWorker

import threading
main_tid = threading.get_ident()

seen = {}


def slow_job():
    seen["tid"] = threading.get_ident()
    seen["thread"] = threading.current_thread().name
    time.sleep(0.4)          # 模拟端口没开时 adb 的等待
    return "OK"


w = MainWindow()
got = {}
w._run_bg(slow_job, on_done=lambda r: got.setdefault("r", r))
check("tid" not in seen, "调用后主线程没有被占住(后台还没跑完就已经返回)")
spin(2)
check(seen.get("tid") != main_tid,
      f"job 跑在非主线程, 实测 tid={seen.get('tid')} (主={main_tid})")
check(got.get("r") == "OK", f"返回值回到主线程, 实测 {got.get('r')}")

print("\n[2] 主线程不被阻塞(桩设备 sleep 0.5s, 调用后立刻能继续干活)")


class FakeDevice:
    def screenshot(self):
        seen["shot_tid"] = threading.get_ident()
        time.sleep(0.5)                      # 模拟端口没开时 adb 的等待
        import numpy as np
        import cv2
        img = np.zeros((960, 540, 3), dtype=np.uint8)
        ok, buf = cv2.imencode(".png", img)  # 真接口返回的是 png 字节
        return buf.tobytes()


w.device = FakeDevice()
w._local_port = 16384
try:
    w._combo_target = None
except Exception:
    pass
# 让 _target_port() 返回本机端口
w._target_port = lambda: 16384
w._acc_name = lambda p: "本机"
w.store.ensure(16384, name="本机", source="local")

t0 = time.time()
w._manual_screenshot()
elapsed = time.time() - t0
check(elapsed < 0.3, f"_manual_screenshot 立即返回(实测 {elapsed:.3f}s, 阻塞版会是 ~0.5s)")
spin(2)
check(seen.get("shot_tid") != main_tid,
      f"adb 截图在非主线程执行, 实测 tid={seen.get('shot_tid')}")
ch = w.store.get(16384)
check(ch is not None and ch.frame is not None, "画面已写进通道")

print("\n[3] 失败回主线程, 不炸")


class BadDevice:
    def screenshot(self):
        raise OSError("adb 掉线了")


MSGS.clear()
w.device = BadDevice()
w._manual_screenshot()
spin(2)
check(any(m[0] == "warning" for m in MSGS), f"失败提示被触发, 实测 {MSGS[:1]}")

print("\n[4] FuncWorker 异常走 failed, 不冒泡到线程外")


def boom():
    raise ValueError("故意炸")


res = {}
fw = FuncWorker(boom)
fw.failed.connect(lambda m: res.setdefault("m", m))
fw.start()
spin(2)
check("m" in res and "ValueError" in res["m"],
      f"failed 信号带上异常类型, 实测 {res.get('m')}")

print("\n[5] _run_bg 持有线程引用, 结束后摘除并 deleteLater")
w2 = MainWindow()
spin(1.5)                      # 先等启动时那个 reload 线程收掉, 免得干扰计数
holder = {}
bw = w2._run_bg(lambda: (time.sleep(0.2), "done")[1],
                on_done=lambda r: holder.setdefault("r", r))
check(bw in w2._bg_workers, "运行期间该线程被 _bg_workers 持有")
spin(2)
check(bw not in w2._bg_workers, "结束后已从 _bg_workers 摘除(否则引用永不释放)")
check(holder.get("r") == "done", "回调正常执行")

print("\n[6] 采集模板: 没画面 → 异步截图后重入, 不阻塞")
calls = {"n": 0}
real_collect = MainWindow._collect_template


def spy_collect(self):
    calls["n"] += 1
    # 第一次: 装作"没有画面且是本机端口" → 应走异步补拍
    # 第二次(回调重入): 装作有画面 → 应正常往下
    if calls["n"] == 1:
        self.last_click_xy = (100, 200)
        self._click_port = self._local_port
        self._shooting = False
        # 用桩: 通道里没画面 → 触发补拍分支
        ch = self.store.get(self._local_port)
        if ch is not None:
            ch.frame = None
        real_collect(self)
    return


MainWindow._collect_template = spy_collect
try:
    w3 = MainWindow()
    w3.device = FakeDevice()
    w3._target_port = lambda: w3._local_port
    w3._acc_name = lambda p: "本机"
    w3.store.ensure(w3._local_port, name="本机", source="local")
    ch = w3.store.get(w3._local_port)
    if ch is not None:
        ch.frame = None
    calls["n"] = 0
    t0 = time.time()
    spy_collect(w3)
    el = time.time() - t0
    check(el < 0.3, f"第一次调用立即返回并转入后台(实测 {el:.3f}s)")
    check(getattr(w3, "_shooting", False) is True, "_shooting 置起(防连点重复起线程)")
    spin(2)
    check(calls["n"] >= 2, f"截图完成后回调重入 _collect_template, 实测调用 {calls['n']} 次")
    check(getattr(w3, "_shooting", False) is False, "重入后 _shooting 复位")
finally:
    MainWindow._collect_template = real_collect

print("\n[7] matcher.reload() 走后台(不再在主线程读 65 张 png)")
# 2026-10-18 v4.7.0: main_window.py 拆出若干 Mixin 后, 不能再写死读
#   "gui/main_window.py" —— 方法已经搬到 gui/taskeditor_mixin.py 了, 而
#   src.find("def _refresh_template_list") 找不到时返回 -1, 切片就变成文件开头
#   那一小段, 于是"没有同步 reload"这种断言会**假通过**。
#   现在改为在 gui/ 全目录里按方法名定位, 并显式校验能定位到。
src = gui_sources()
where, seg = method_source("_refresh_template_list")
check(where != "", f"_refresh_template_list 能定位到(实测在 {where})")
check("_run_bg(self.matcher.reload" in seg, "_refresh_template_list 里 reload 走 _run_bg")
check("self.matcher.reload()" not in seg, "不再有同步的 self.matcher.reload()")
rseg = open("gui/registry_panel.py", encoding="utf-8").read()
ri = rseg.find("def _save_and_refresh")
check("_run_bg" in rseg[ri:ri + 900], "注册表保存后的 reload 也走后台(带同步兜底)")

print("\n[8] 缩略图 FastTransformation / _poll_live 800ms")
lseg = open("gui/live_panel.py", encoding="utf-8").read()
check("Qt.FastTransformation" in lseg, "缩略图用 FastTransformation")
check("_live_timer.start(800)" in src, "_poll_live 间隔已提到 800ms")

print("\n[9] 退出收尾会等/终止后台线程")
fwhere, fsrc = method_source("_full_shutdown", 3000)
check(fwhere != "", f"_full_shutdown 能定位到(实测在 {fwhere})")
check("_bg_workers" in fsrc, "_full_shutdown 里处理了 _bg_workers")

print("\n[10] 截图按钮信号别把 Qt 的 checked(bool) 当 _then")
# 2026-10-20 v5.10.2 事故: btn_shot.clicked.connect(self._manual_screenshot) —— Qt 的
#   clicked 信号带一个 checked(bool) 参数, 会被当成 _then 传进去; 截图成功后
#   _then() 抛 TypeError: 'bool' object is not callable, 每点一次按钮 crash.log 记一条
#   (D:\鸡驴助手_运行\logs\crash.log 有实录)。
mwhere, mseg = method_source("_manual_screenshot", 4000)
check(mwhere != "", f"_manual_screenshot 能定位到(实测在 {mwhere})")
check("callable(_then)" in mseg, "_ok 里用 callable(_then) 守卫, 误传 bool 也不会抛")
bt = [l for l in src.splitlines() if "btn_shot.clicked.connect" in l]
check(len(bt) == 1, f"定位到 btn_shot 的连接语句(实测 {len(bt)} 行)")
check(bool(bt) and "lambda" in bt[0],
      "btn_shot.clicked 用 lambda 吞掉 checked 参数, 实测: %s"
      % (bt[0].strip() if bt else "N/A"))

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
