# -*- coding: utf-8 -*-
"""GUI 任务工作线程

Engine 是阻塞式循环，不能跑在 UI 主线程。这里用 QThread 包装：
在子线程里执行任务，通过 Qt 信号把 日志/截图/步骤状态 发回主线程刷新界面。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from engine import Engine, load_task
from engine.ui_registry import UiRegistry


class ExportWorker(QThread):
    """导出诊断包的后台线程

    2026-10-17 新增: 打包要遍历日志(可能上百 MB)与截图, 放主线程会卡界面。
    只回一个 finished(路径或错误) —— 不做进度条, 通常两三秒完事。
    """

    finished = Signal(str, str)   # (zip 路径, 错误信息); 成功时错误信息为空

    def __init__(self, cfg, extra, dest_dir=None, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._extra = extra
        self._dest = dest_dir

    def run(self):
        try:
            from diagnostics import export_diagnostics
            path = export_diagnostics(self._cfg, self._extra, self._dest)
            self.finished.emit(str(path), "")
        except Exception as e:
            import traceback as _tb
            try:
                from crash_guard import write_crash
                write_crash("EXPORT DIAGNOSTICS FAILED", _tb.format_exc())
            except Exception:
                pass
            self.finished.emit("", str(e))


class FuncWorker(QThread):
    """在后台线程跑一个函数, 把返回值 / 异常发回主线程 (2026-10-17 v3.5.0 P1-1)

    用途: 「截图」「采集模板」「重载模板库」这类**会同步等 adb 或读几十上百张
    png** 的动作。以前都在主线程直跑, 实测:
      · 端口在线时截图约 0.6s, **端口没开时 4.6s**(adb connect 自己做 TCP 重试)
      · matcher.reload() 重读 65 张模板, 数十~数百 ms
    界面就是这么"卡一下"的。现在统一走这个类。

    ⚠️ 调用方必须**持有**这个线程的引用直到它结束 —— 局部变量被 GC 掉会触发
    `QThread: Destroyed while thread is still running` 硬崩。main_window 里统一
    用 `self._run_bg(...)`(内部挂在 _bg_workers 上并在 finished 时摘除)。
    """

    done = Signal(object)      # 函数返回值
    failed = Signal(str)       # "异常类型: 消息"

    def __init__(self, fn, *args, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._args = args

    def run(self):
        try:
            self.done.emit(self._fn(*self._args))
        except Exception as e:
            try:
                from crash_guard import write_crash
                import traceback as _tb
                write_crash("BG WORKER FAILED", _tb.format_exc())
            except Exception:
                pass
            self.failed.emit(f"{type(e).__name__}: {e}")


class TaskWorker(QThread):
    """执行一次任务的工作线程"""

    # 信号参数类型说明:
    log = Signal(str, str)            # (level, message)
    screenshot = Signal(object)       # numpy BGR 截图
    step_start = Signal(int, str)     # (序号, 步骤描述)
    step_end = Signal(int, str, bool) # (序号, 步骤名, 是否成功)
    # 2026-10-16 v3.0.0: 第三个参数是"失败明细汇总"(无失败则为空串)。
    # 以前只有 ok/任务名, 而 continue 兜底会让 ok=True —— 帮派捐献没做成却显示
    # "完成", 用户无从察觉。现在界面能拿到"哪几步没做成"。
    # 2026-10-17 v3.5.0: 追加第四个参数 —— 结构化统计 dict
    # {"failed":n, "skipped":n, "required_failed":n, "stopped":bool}。
    # 以前界面用 int(summary.split(" 步未做成")[0]) 从文案切数字, 文案一变就
    # 静默得到 0(失败被显示成"完成")。现在一律读这个 dict。
    task_finished = Signal(bool, str, str, dict)  # (成功?, 信息, 明细, 统计)

    def __init__(self, device, matcher, ocr, task_path, poll_interval=0.3,
                 debug_dir="logs/debug", ui=None, start_index=0, parent=None):
        super().__init__(parent)
        self.device = device
        self.matcher = matcher
        self.ocr = ocr
        self.task_path = task_path
        self.poll_interval = poll_interval
        self.debug_dir = debug_dir
        self.ui = ui
        # 2026-10-09: 从脚本第 start_index 步(0基)开始跑 —— 供「脚本步骤」页
        # 实现"点击某步骤→从该步开始执行"(调试/补跑单段时省去前面的等待)
        self.start_index = int(start_index or 0)
        self._engine = None

    def stop(self):
        if self._engine:
            self._engine.stop()

    def run(self):
        try:
            self.matcher.reload()          # 重新加载模板库(含 GUI 新增模板)
            task = load_task(self.task_path)
            eng = Engine(self.device, self.matcher, ocr=self.ocr,
                         poll_interval=self.poll_interval,
                         debug_dump_dir=self.debug_dir,
                         ui=self.ui or UiRegistry())
            self._engine = eng

            eng.subscribe(
                log=lambda lv, m: self.log.emit(lv, m),
                screenshot=lambda s: self.screenshot.emit(s),
                step_start=lambda i, s: self.step_start.emit(i, s.describe()),
                step_end=lambda i, s, ok: self.step_end.emit(i, s.name or str(i), ok),
            )
            ok = eng.run_task(task, self.start_index)
            self.task_finished.emit(ok, task.name, eng.failure_summary(), eng.counts())
        except Exception as e:
            import traceback as _tb
            # 2026-10-16: pythonw 下 stderr 被丢弃, print_exc() 等于白打。
            # 改写到 logs/crash.log, 这样 worker 里的异常也能被看到。
            try:
                from crash_guard import write_crash
                write_crash("WORKER EXCEPTION (task thread)", _tb.format_exc())
            except Exception:
                pass
            self.task_finished.emit(False, f"异常: {e}", "", {})
