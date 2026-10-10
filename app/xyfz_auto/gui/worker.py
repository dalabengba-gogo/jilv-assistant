# -*- coding: utf-8 -*-
"""GUI 任务工作线程

Engine 是阻塞式循环，不能跑在 UI 主线程。这里用 QThread 包装：
在子线程里执行任务，通过 Qt 信号把 日志/截图/步骤状态 发回主线程刷新界面。
"""
from __future__ import annotations

from PySide6.QtCore import QThread, Signal

from engine import Engine, load_task
from engine.ui_registry import UiRegistry


class TaskWorker(QThread):
    """执行一次任务的工作线程"""

    # 信号参数类型说明:
    log = Signal(str, str)            # (level, message)
    screenshot = Signal(object)       # numpy BGR 截图
    step_start = Signal(int, str)     # (序号, 步骤描述)
    step_end = Signal(int, str, bool) # (序号, 步骤名, 是否成功)
    task_finished = Signal(bool, str) # (成功?, 信息)

    def __init__(self, device, matcher, ocr, task_path, poll_interval=1.0,
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
            self.task_finished.emit(ok, task.name)
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.task_finished.emit(False, f"异常: {e}")
