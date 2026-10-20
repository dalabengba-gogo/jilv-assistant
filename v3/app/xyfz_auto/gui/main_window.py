# -*- coding: utf-8 -*-
"""主窗口：控制面板(左) + 实时监控(右: 画面/步骤/脚本)

2026-09-24 多账号改造:
    原先「实时画面」直接绑 self.device(单端口), 多开账号跑在子进程里,
    画面与步骤都没有回传通道 → 界面上只能看到第一个账号。
    现在所有任务(本机 + 每个多开子进程)都汇入 ChannelStore 的"通道",
    UI 按账号分别展示: 画面页可选账号 + 缩略图墙, 步骤页按账号筛选/标注。
"""
from __future__ import annotations
import datetime
import html
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
import cv2
import numpy as np
from PySide6.QtCore import Qt, QTime, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton,
    QSplitter, QSystemTrayIcon, QTabWidget, QTableWidget, QTableWidgetItem, QTimeEdit,
    QVBoxLayout, QWidget,
)
from common import (APP_TITLE, APP_VERSION, load_config, setup_logger,
                    close_loggers, acquire_device_lock, release_device_lock,
                    imwrite_unicode, rotate_text_logs, rotate_files)
from accounts import load_accounts as _load_accounts
from proc_guard import SpawnGuard, activate_window, kill_own_adb_server, \
    release_all_device_locks, single_instance_mutex
from device import AdbDevice
from device.adb_client import probe_ports
from engine import load_task, list_sub_scripts
from engine.ui_registry import UiRegistry
from live import LIVE_ROOT, LiveReader
from recognition import OcrEngine, TemplateMatcher
from gui.button_library import ButtonLibraryDialog
from gui.disclaimer import (DISCLAIMER_VERSION, DisclaimerDialog,
                            load_disclaimer_settings, save_disclaimer_settings,
                            should_show_disclaimer)
from gui.live_panel import AccountScreenPage
from gui.live_store import Channel, ChannelStore, StepRec
from gui.multi_panel import MultiPanel
from gui.new_script import KIND_PART, NewScriptDialog, insert_include_into_task
from gui.registry_panel import RegistryPanel
from gui.render_mixin import RenderMixin
from gui.timer_mixin import TimerMixin
from gui.logrotate_mixin import LogRotateMixin
from gui.taskeditor_mixin import TaskEditorMixin
from gui.lights_mixin import StatusLightsMixin
from gui.livepoll_mixin import LivePollMixin
from gui.ui_helpers import (_PortLight, _input_dialog, _safe_call,
                           _tail_int)
from gui.worker import ExportWorker, FuncWorker, TaskWorker
PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_ICON = PROJECT_ROOT / "assets" / "app_icon.ico"   # 窗口/任务栏图标

def _subtask_lines(subs: list) -> list[str]:
    """子任务统计 → 界面日志行(2026-10-17 v3.7.0)

    每个子任务一行: ✔/⚠/✘ + 名字 + 步数。任务跑完只显示这些, 取代以前的
    逐步明细(上百行流水账)。subs 为空(异常终止路径)返回空列表, 由调用方退回明细。
    """
    out = []
    for st in subs or []:
        name = str(st.get("name", "?"))
        total = int(st.get("total", 0) or 0)
        if not st.get("reached"):
            out.append(f"    —   {name}   未执行")
            continue
        # 2026-10-17 v3.7.0: 有验收点的子任务以游戏面板的完成状态为准
        # (而不是数"掉了几步" —— 很多"失败"其实是按钮已领取点不到这种正常情况)
        v = st.get("verified")
        n_req = int(st.get("required_failed", 0) or 0)
        n_fail = int(st.get("failed", 0) or 0)
        if v is True:
            out.append(f"    ✔   {name}   面板已「已完成」")
            continue
        if v is False:
            out.append(f"    ✘   {name}   面板未见「已完成」")
            continue
        if n_req:
            out.append(f"    ✘   {name}   {n_req}步关键失败")
        elif n_fail:
            out.append(f"    ⚠   {name}   {n_fail}步未做成")
        else:
            out.append(f"    ✔   {name}   完成")
    return out

class MainWindow(RenderMixin, TimerMixin, LogRotateMixin, TaskEditorMixin, StatusLightsMixin, LivePollMixin, QMainWindow):
    """主窗口

    2026-10-17 v3.5.0(P2-1): 渲染部分(画面/步骤/日志, 约 215 行)已切到
    `gui/render_mixin.py` 的 RenderMixin, 这里通过多重继承拿回来。
    方法签名与调用点全部不变 —— 拆分是纯搬移, 不改行为。
    顺序写 RenderMixin 在前, 是为了让它的方法可以被主类里的同名方法覆盖
    (目前没有同名方法, 但保持这个可扩展的方向)。
    """
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        if APP_ICON.exists():
            self.setWindowIcon(QIcon(str(APP_ICON)))
        self.resize(1180, 760)
        self.cfg = load_config()
        self.logger = setup_logger()
        self.device = None
        self.matcher = None
        self.ocr = None
        self.worker = None
        self.ui_registry = UiRegistry()   # 按钮注册表(按钮库面板/任务引擎共用)
        self.current_task_path = None
        # 2026-10-09: 「脚本」页现在可以在"总脚本 / 各子脚本"之间切换 ——
        # edit_script_path 记的是编辑器当前打开的那份文件(保存/启动同步都按它)。
        self.edit_script_path = None
        self._edit_dirty = False        # 编辑器有未保存改动
        self._loading_edit = False      # 程序化灌内容时抑制 dirty 标记
        self.last_screen_bgr = None
        self.last_click_xy = None     # 拾取的坐标
        self._manual_xy_queue = []    # 手动点取坐标的步骤(供生成脚本)
        self._timer_cfg_path = PROJECT_ROOT / "gui_timer.json"  # 定时设置持久化
        # key = "HH:MM|脚本名|端口" -> 当天已执行(见 _tick_timer)
        self._timer_runs: dict[str, str] = {}
        # ---- v4.6.0(P0-5) 定时健壮性 ----
        # 尝试次数: key -> [日期, 次数]。失败清掉 _timer_runs 让它能重试,
        # 但次数封顶 —— 否则一个必败的脚本能被重试到天亮。
        self._timer_try: dict[str, list] = {}
        # 重试冷却: key -> 上次真正出发的时间戳, 避免失败后每 5 秒重跑一次
        self._timer_last_try: dict[str, float] = {}
        # 本次是"定时任务拉起的"标记, 用来在结束时把成败回写给对应定时项:
        #   多开子进程用"端口 -> key", 本机任务用单个 _timer_local_key
        self._timer_pending_ports: dict[int, str] = {}
        self._timer_local_key: str | None = None
        self._really_quit = False             # 托盘/菜单"退出"置 True, 否则关窗=最小化到托盘
        # ---- 多账号实时通道(2026-09-24) ----
        # 本机任务与每个多开子进程都是通道里平等的一员, UI 按账号分别展示
        self._local_port = int(self.cfg["device"].get("port", 16384))
        self.store = ChannelStore(name_of=self._acc_name)
        self._readers: dict[int, LiveReader] = {}       # 端口 -> 子进程通道读取器
        self._log_drawn: dict[int, int] = {}            # 端口 -> 已渲染到的日志序号
        self._frame_drawn: dict[int, int] = {}          # 端口 -> 已渲染到的帧版本
        self._sys_logs: list[tuple[float, str]] = []    # 系统消息(与账号日志合并排序)
        self._steps_items: dict[tuple[int, int], QListWidgetItem] = {}
        self._steps_sig = None                          # 步骤列表结构签名
        self._steps_dirty = True
        self._page_sig = None                           # 画面页结构签名
        self._scroll_to = None                          # 待滚动到可见的步骤
        self._click_port = None                         # 最后取坐标所属通道
        self._syncing_target = False                    # 全局账号切换的防递归标志
        # 2026-09-28: 子进程随主进程回收 + 退出收尾(解决"退出后仍有残留进程占文件")
        self.spawn_guard = SpawnGuard()
        self._shutdown_done = False
        self._quitting_confirmed = False                # 退出确认对话框防重入
        self._scan_tick = 0                             # 通道目录扫描降频用
        self._live_ports: set[int] = set()              # 本轮扫到的子进程端口(回收句柄用)
        # 2026-10-17 v3.5.0(P1-1): 后台短任务线程持有列表。
        # 截图/采集/重载模板的线程必须被引用着, 否则 Python GC 一回收就
        # "QThread: Destroyed while thread is still running"。
        self._bg_workers: list = []
        # 2026-10-17 v3.10.0: 每账号一行任务状态(挂在状态灯右侧)。
        # _port_state_labels = 端口 -> 那一行右侧的 QLabel;
        # _port_state_override = 端口 -> 人工接管的状态文字(下次启动该号时清掉),
        #   没接管就由通道状态自动推导, 因此多开子进程的状态不用主界面操心。
        self._port_state_labels: dict[int, QLabel] = {}
        self._port_state_override: dict[int, tuple[str, str, str]] = {}
        self._build_ui()
        self._build_menubar()          # 「帮助」菜单: 可随时重看免责声明
        self._rebuild_target_combo()   # 全局账号下拉(本机 + 多开账号)
        self._connect_device()
        self._restore_timer()
        self._rotate_logs("启动")     # v4.6.0(P0-4) 日志滚动三触发点 ①
        self._refresh_task_list()
        self._refresh_template_list()
        self._setup_tray()
        # 本机通道常驻: 即使没跑任务, 画面页里也能看到它(显示"暂无画面")
        self.store.ensure(self._local_port, source="local",
                          name=self._acc_name(self._local_port) or "本机",
                          task=self.combo_task.currentText())
        self._render_all(force=True)
        # 定时任务轮询(每5秒检查一次是否到点)
        self._timer = QTimer(self)
        self._timer.timeout.connect(lambda: _safe_call(self._tick_timer))
        self._timer.start(5000)
        # 多开子进程通道轮询: 增量读 logs/live/port_*/, 把画面/步骤/日志捞进 store
        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(lambda: _safe_call(self._poll_live))
        # 2026-10-17 v3.5.0(P1-1): 600 → 800ms。5 个账号每周期各做一次
        # imdecode(540x960), 实测叠加 30~75ms; 1 秒 1.6 次降到 1.25 次,
        # 省下的主线程时间让界面明显更跟手, 画面流畅度无感差异。
        self._live_timer.start(800)
        # 界面刷新: 合并高频事件的渲染(步骤/画面/日志), 避免每来一个事件就重绘
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(lambda: _safe_call(self._render_tick))
        self._ui_timer.start(250)
        # 2026-09-16: "停止中"秒表。停止是协作式的(要等当前截图/等待轮询结束),
        # 之前点完停止只有一句话不变, 用户不知道到底停没停、还要多久。
        self._stop_t0 = None
        self._stop_tick = QTimer(self)
        self._stop_tick.timeout.connect(lambda: _safe_call(self._tick_stop))
        self._stop_tick.start(200)
        # 2026-10-15: 启动免责声明。延后到主窗口已经显示出来再弹 —— 否则窗口还没
        # 出来先弹个框, 看起来像程序卡住。是否要弹由 gui_settings.json 决定。
        QTimer.singleShot(300, self._maybe_show_disclaimer)
    # ================= 免责声明 =================
    def _build_menubar(self):
        """菜单栏: 「文件」给退出入口, 「帮助」可随时重看免责声明

        2026-10-16: 以前**只有托盘右键菜单能真正退出** —— 点窗口右上角 ✕ 只是
        最小化到托盘(定时任务要在后台跑, 所以刻意保留进程), 而托盘图标常被
        Windows 折叠进隐藏区, 用户找不到图标, 就成了"程序消失了但进程还在"。
        实测: 用户以为自己退出了 v2, 进程其实从上午一直挂到下午。
        现在主界面直接给了出口, 不依赖托盘是否可见。
        """
        try:
            mb = self.menuBar()
            mf = mb.addMenu("文件(&F)")
            act_min = mf.addAction("最小化到托盘(&M)")
            act_min.setToolTip("隐藏窗口, 程序继续在后台运行(定时任务需要)")
            act_min.triggered.connect(self._minimize_to_tray)
            mf.addSeparator()
            act_quit = mf.addAction("退出(&X)")
            act_quit.setShortcut("Ctrl+Q")
            act_quit.setToolTip("停止全部任务并结束程序(释放 adb / 设备锁 / 日志句柄)")
            act_quit.triggered.connect(self._quit_app)
            mh = mb.addMenu("帮助(&H)")
            act = mh.addAction("免责声明 / 主播推荐…")
            act.triggered.connect(self._show_disclaimer_manual)
            # 2026-10-17: 排障时第一件事就是"把日志发过来"。以前要手动翻 logs/
            # 截屏、口述环境, 来回好几轮; 现在一键打包成 zip(含排错指引)。
            act_exp = mh.addAction("导出诊断信息…")
            act_exp.setToolTip(
                "把版本/环境/端口状态/配置/日志/最近失败现场截图打包成一个 zip, "
                "方便发给别人排查。")
            act_exp.triggered.connect(self._export_diagnostics)
        except Exception:
            pass
    # ================= 诊断信息导出 =================
    def _export_diagnostics(self):
        """一键导出诊断包(后台线程打包, 完成后弹提示并打开所在文件夹)

        2026-10-17 新增。低频动作, 入口有两处: 「帮助」菜单 + 「脚本选项 →
        运行(全局)」页的按钮(排障类入口按约定放那一页)。
        """
        if getattr(self, "_export_worker", None) and self._export_worker.isRunning():
            QMessageBox.information(self, "导出诊断信息", "上一次导出还没完成, 请稍等。")
            return
        try:
            extra = self._collect_diag_extra()
        except Exception as e:
            QMessageBox.warning(self, "导出诊断信息", f"收集信息失败: {e}")
            return
        w = ExportWorker(self.cfg, extra, None, self)
        self._export_worker = w
        # 显式 QueuedConnection: 槽在主线程执行, 不依赖 Qt 对无上下文 lambda 的解析
        w.finished.connect(self._on_export_done, Qt.ConnectionType.QueuedConnection)
        w.start()
        self.statusBar().showMessage("正在导出诊断信息…", 5000)
    def _collect_diag_extra(self) -> dict:
        """收集「界面知道、diagnostics 模块不知道」的那部分信息"""
        d = (self.cfg or {}).get("device") or {}
        ports = []
        for port, light in (getattr(self, "_port_lights", None) or {}).items():
            ports.append({
                "port": int(port),
                "name": self._acc_name(int(port)) or "",
                "online": getattr(light, "_online", None),
                "front": (getattr(light, "_detail", "") or "").replace("\n", " ")[:80],
            })
        ports.sort(key=lambda x: x["port"])
        # 2026-10-19 v4.8.0 (P1-6): 定时任务清单进诊断包。以前只打包"端口/日志/脚本",
        # 用户报"定时没跑"时我们手里根本没有配了什么、今天跑过没有 —— 只能靠一串口头
        # 问答去猜。这里从定时表格直接读(timer_items 见 TimerMixin)。
        timers = []
        try:
            today = __import__("datetime").date.today().isoformat()
            tbl = getattr(self, "tbl_timer", None)
            if tbl is not None:
                for r in range(tbl.rowCount()):
                    ck = tbl.cellWidget(r, 0)
                    it_t = tbl.item(r, 1)
                    it_s = tbl.item(r, 2)
                    if not it_s or not it_s.text():
                        continue
                    cb_acc = tbl.cellWidget(r, 3)
                    port = int(cb_acc.currentData() or 0) if cb_acc else 0
                    act = it_s.text()
                    time_s = it_t.text() if it_t else ""
                    # key 的构造方式与 _tick_timer 完全一致, 才能查到今天的状态
                    key = f"{time_s}|{act}|{port}"
                    state = "今天未跑"
                    if (getattr(self, "_timer_runs", None) or {}).get(key) == today:
                        state = "今天已跑"
                    tr = (getattr(self, "_timer_try", None) or {}).get(key)
                    if isinstance(tr, list) and tr and tr[0] == today and len(tr) > 1:
                        state = f"失败重试 {tr[1]} 次"
                    timers.append({
                        "enabled": bool(ck and ck.isChecked()),
                        "time": time_s,
                        "task": act,
                        "account": ("本机" if port == 0
                                    else f"{self._acc_name(port) or ''}(端口{port})"),
                        "today": state,
                    })
        except Exception:
            timers = []
        return {
            "adb_source": getattr(self.device, "adb_source", "") if self.device else "",
            "adb_path": str(getattr(self.device, "adb", "")) if self.device else str(d.get("adb_path") or ""),
            "screen_size": f"{d.get('screen_w', '?')}x{d.get('screen_h', '?')}",
            "package": d.get("package", ""),
            "ports": ports,
            "timers": timers,
            "timer_grace_minutes": self._timer_cfg_int("timer_grace_minutes", 3),
            "current_script": self.combo_task.currentText() if getattr(self, "combo_task", None) else "",
            "last_run": getattr(self, "_last_run_at", ""),
            "last_summary": getattr(self, "_last_summary", ""),
        }
    def _on_export_done(self, path: str, err: str):
        """导出结束: 成功则告知路径并打开所在文件夹"""
        if err or not path:
            QMessageBox.warning(self, "导出诊断信息", f"导出失败: {err or '未知原因'}")
            return
        box = QMessageBox(self)
        box.setWindowTitle("导出诊断信息")
        box.setIcon(QMessageBox.Information)
        box.setText(f"已导出:\n{path}")
        box.setInformativeText("把它发给排查的人即可。里面第一份《00_排错指引》"
                               "说明了每个文件回答什么问题。")
        btn_open = box.addButton("打开所在文件夹", QMessageBox.AcceptRole)
        box.addButton("关闭", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is btn_open:
            try:
                os.startfile(os.path.dirname(path))   # noqa: S606 用户主动要求打开
            except Exception:
                pass
    def _minimize_to_tray(self):
        """菜单「最小化到托盘」: 程序继续在后台跑(定时任务/多开需要)"""
        if getattr(self, "tray", None) and self.tray.isVisible():
            self.hide()
            self.tray.showMessage(
                APP_TITLE, "已最小化到托盘, 程序仍在后台运行。\n"
                           "要彻底退出: 菜单「文件 → 退出」。",
                QSystemTrayIcon.Information, 5000)
        else:
            self.showMinimized()
    def _maybe_show_disclaimer(self):
        """启动时调用: 已勾选"不再显示"且声明版本未变 → 直接跳过"""
        try:
            if not should_show_disclaimer():
                return
            self._show_disclaimer()
        except Exception:
            pass
    def _show_disclaimer_manual(self):
        """菜单/托盘手动打开(不看"不再显示", 总能打开)"""
        try:
            self._show_disclaimer()
        except Exception:
            pass
    def _show_disclaimer(self):
        st = load_disclaimer_settings()
        dlg = DisclaimerDialog(self, dont_show=bool(st.get("dont_show", False)))
        dlg.exec()
        # 手动打开时也能取消勾选 → 下次启动恢复弹窗
        st["dont_show"] = dlg.dont_show_checked()
        st["version"] = DISCLAIMER_VERSION
        save_disclaimer_settings(st)
    # ================= 账号命名 =================
    def _acc_name(self, port: int) -> str:
        """端口 → 用户在「多开管理」里起的账号名(查不到返回空)"""
        try:
            for a in _load_accounts():
                if int(a.get("port", -1)) == int(port):
                    return str(a.get("name") or "")
        except Exception:
            pass
        return ""
    def _local_label(self) -> str:
        """本机通道的显示名(与账号清单对齐, 免得同一个模拟器两个叫法)"""
        return self._acc_name(self._local_port) or "本机"
    def refresh_account_names(self):
        """「多开管理」保存后调用: 端口/名称改动要同步到所有引用处

        2026-09-29 修复: 旧版只改通道显示名, 左上角「当前账号(全局)」下拉
        不重建 —— 表格里改了端口, 下拉里还是旧端口且新端口不在选项里。
        现在保存后重建下拉; 若当前选中的账号正好被改了端口, 按账号名
        自动跟随到新端口, 找不到再回落本机。
        """
        old_port = self._target_port()
        old_text = self.combo_target.currentText()
        # 旧选项文本形如 "账号5 (端口16512)"; 本机项是 "本机(GUI设备)"
        old_name = old_text.rsplit(" (端口", 1)[0] if " (端口" in old_text else old_text
        for ch in self.store.channels():
            if ch.source == "local":
                nm = self._local_label()
            else:
                nm = self._acc_name(ch.port) or ch.label
            if nm and nm != ch.name:
                ch.name = nm
                ch.touch()
        self._steps_sig = None
        self._page_sig = None
        self._rebuild_target_combo()
        # 2026-10-16: 多开管理的端口/名称变了, 左上角状态灯也要跟着同步
        self._rebuild_status_lights()
        ports_now = {p for p, _ in self._target_accounts()}
        if old_port not in ports_now:
            # 当前选中的账号被改了端口 → 按名字找到它的新端口
            hit = next((p for p, txt in self._target_accounts()
                        if txt == f"{old_name} (端口{p})"), None)
            self._set_current_port(hit if hit is not None else self._local_port)
    def _tick_stop(self):
        """刷新"停止中… Ns"提示

        2026-10-17 v3.10.0: 不再自己写标签 —— 秒表由 _port_state_of() 按
        _stop_t0 现算(挂在状态灯右侧该账号那一行), 这里只管到点收掉标志位。
        """
        if self._stop_t0 is None:
            return
        if not (self.worker and self.worker.isRunning()):
            self._stop_t0 = None
    # ================= 系统托盘(右下角) =================
    def _setup_tray(self):
        """右下角通知区托盘图标: 双击显示主窗口, 右键菜单可显示/退出"""
        if not APP_ICON.exists() or not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QSystemTrayIcon(QIcon(str(APP_ICON)), self)
        self.tray.setToolTip(APP_TITLE)
        menu = QMenu()
        act_show = menu.addAction("显示主窗口")
        act_show.triggered.connect(self._tray_show)
        menu.addSeparator()
        # 2026-10-15: 免得勾了"不再显示"就再也找不着声明了
        act_disc = menu.addAction("免责声明 / 主播推荐…")
        act_disc.triggered.connect(self._show_disclaimer_manual)
        menu.addSeparator()
        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self._quit_app)   # 走 closeEvent 回收资源
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()
    def _quit_app(self):
        """托盘菜单"退出": 标记后真关窗(回收子进程/任务线程)"""
        self._really_quit = True
        self.close()
    def _running_task_summary(self) -> str:
        """当前还在跑的任务简述(退出确认/状态显示用)"""
        items = []
        if self.worker and self.worker.isRunning():
            items.append(f"本机「{self._acc_name(self._local_port) or '本机'}」")
        pm = getattr(self, "panel_multi", None)
        if pm:
            for port in sorted(pm.running_ports()):
                items.append(f"「{self._acc_name(port) or port}」(端口{port})")
        return "、".join(items)
    def _full_shutdown(self):
        """彻底退出前的收尾 —— 把占着文件夹的每一个持有者都收干净。

        用户反馈(2026-09-28): 绿色版退出后仍有残留进程占着文件, 必须
        先结束进程才能删除文件夹。占用来源共四个, 这里逐一处理:
          1) GUI 内的任务线程(本机任务)  → worker.stop() + 等它退
          2) 多开子进程 run_one.py       → panel_multi.shutdown() + 作业对象兜底
          3) 本包 adb server(常用占用者) → 仅当它确由本包 adb.exe 拉起时才停
          4) logs\\run.log 文件句柄        → close_loggers()
        另外顺带释放设备互斥锁, 避免"锁残留导致下次不启动"。
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        # 1) 本机任务线程
        #    2026-10-17 v3.5.0(P1-3): 改走 _retire_worker() —— 等不到就 terminate
        #    并记 crash.log, 不再"超时也照样往下走"把线程活活拆掉。
        try:
            self._retire_worker(5000)
        except Exception:
            pass
        # 2) 多开子进程
        try:
            if getattr(self, "panel_multi", None):
                self.panel_multi.shutdown()
                # 2026-10-09: 多开管理现在是独立窗口, 主窗口退出要一并关掉,
                # 否则它会以一个空壳窗口留在屏幕上(子进程已没, 但窗口还在)
                self.panel_multi.close()
        except Exception:
            pass
        try:
            if getattr(self, "spawn_guard", None):
                self.spawn_guard.close()      # 关作业句柄 → 系统补杀作业内全部进程
        except Exception:
            pass
        # 3) 设备锁: 主动清掉本程序用到的端口
        try:
            ports = {self._local_port}
            for acc in _load_accounts():
                try:
                    ports.add(int(acc["port"]))
                except (TypeError, ValueError):
                    continue
            release_all_device_locks(ports)
        except Exception:
            pass
        # 4) 本包 adb server(只回收本包 adb.exe 拉起的那个)
        try:
            if (self.cfg.get("app", {}) or {}).get("kill_adb_on_exit", True):
                msg = kill_own_adb_server(self.cfg["device"].get("adb_path"))
                self.logger.info(f"[退出] {msg}")
        except Exception:
            pass
        # 5) 关日志句柄(否则 run.log 一直被占)
        try:
            close_loggers()
        except Exception:
            pass
        # 6) 关多开通道的事件流句柄(2026-10-17 v3.5.0 P1-4)
        #    以前 _readers 只增不减且从无 close() —— 每个跑过的端口都留一个句柄
        #    到进程结束, 是"退出后 logs 文件夹删不掉"的占用源之一。
        try:
            for _port, _rd in list(getattr(self, "_readers", {}).items()):
                try:
                    _rd.close()
                except Exception:
                    pass
            self._readers.clear()
        except Exception:
            pass
        # 7) 关崩溃转储句柄(faulthandler 常驻打开 logs/crash.log, 同上)
        try:
            from crash_guard import close_crash_handle
            close_crash_handle()
        except Exception:
            pass
        # 8) 收掉后台短任务线程(截图/采集/重载模板, 2026-10-17 v3.5.0 P1-1)
        #    它们最多等 adb 几秒; 超时就 terminate, 免得退出时被 Qt 拆线程硬崩。
        try:
            for _w in list(getattr(self, "_bg_workers", [])):
                try:
                    if not _w.wait(2000):
                        _w.terminate()
                        _w.wait(1000)
                except Exception:
                    pass
            self._bg_workers.clear()
        except Exception:
            pass
    def _tray_show(self):
        """从托盘恢复窗口(最小化时先还原)"""
        self.showNormal()
        self.raise_()
        self.activateWindow()
    def _on_tray_activated(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._tray_show()
    # ================= UI 构建 =================
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        split = QSplitter(Qt.Horizontal, central)
        # ---- 左: 控制面板 ----
        left = QFrame()
        left.setFrameShape(QFrame.StyledPanel)
        left.setMaximumWidth(260)
        lv = QVBoxLayout(left)
        lv.addWidget(QLabel("设备状态(每端口一灯, 与多开管理同步)"))
        # 2026-10-16: 旧版只监测 config 里那一个端口, 换了别的模拟器实例
        # 端口就显示「未连接」, 误以为没连上。现改为按「多开管理」(accounts.json)
        # 的端口逐个亮灯, 哪个端口真在线哪个亮绿, 与多开管理端口实时同步。
        # 2026-10-17 v3.10.0: 灯的右侧再挂一列"该账号的任务状态"
        # (空闲/运行中/完成/失败)。以前只有启动按钮下面一行红字, 且只反映
        # **当前账号** —— 多开时别的号跑成什么样完全看不见。
        self._status_lights_wrap = QWidget()
        self._status_lights_layout = QVBoxLayout(self._status_lights_wrap)
        self._status_lights_layout.setContentsMargins(0, 0, 0, 0)
        self._status_lights_layout.setSpacing(2)
        lv.addWidget(self._status_lights_wrap)
        self._port_lights: dict[int, _PortLight] = {}
        self._rebuild_status_lights()
        # 2026-09-28: 全局「当前账号」切换器 —— 截图/启动/停止/实时画面/实时步骤
        # 全部跟随这里, 换一处=处处换, 不用在每个页面单独切。
        lv.addWidget(QLabel("当前账号(全局)"))
        self.combo_target = QComboBox()
        self.combo_target.setToolTip(
            "全局账号开关: 截图、启动任务、停止, 以及右侧实时画面/实时步骤都跟随此处")
        self.combo_target.currentIndexChanged.connect(self._on_target_changed)
        lv.addWidget(self.combo_target)
        self.btn_shot = QPushButton("截图(当前账号)")
        # 2026-10-20 v5.10.2: 不能直接 connect(self._manual_screenshot)。
        # Qt 的 clicked 信号带一个 checked(bool) 参数, 会被当成 _then 传进去,
        # 截图成功后 _then() 抛 TypeError('bool' object is not callable)
        # (每天点一次按钮就往 crash.log 记一条, 已实测复现)。
        self.btn_shot.clicked.connect(lambda _checked=False: self._manual_screenshot())
        lv.addWidget(self.btn_shot)
        # 2026-10-15: 「窗口诊断」搬到右侧「脚本选项 → 运行(全局)」页, 与它要修的
        # win_input 配置项同页(查出 hwnd 当场就能填)。左侧只留纯运行控制。
        # 实现见 gui/settings_panel.py 的 build_win_diag_body() / _open_win_diag()。
        lv.addSpacing(6)
        lv.addWidget(QLabel("任务脚本"))
        th = QHBoxLayout()
        self.combo_task = QComboBox()
        self.combo_task.currentIndexChanged.connect(self._on_task_selected)
        th.addWidget(self.combo_task, stretch=1)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setToolTip("重新扫描任务目录(外部新增/修改脚本后点这里,不用重启)")
        self.btn_refresh.clicked.connect(self._refresh_task_list)
        th.addWidget(self.btn_refresh)
        lv.addLayout(th)
        # 2026-10-09 v2.0.0: 脚本的「打开 / 新建 / 保存」已搬去右侧「脚本(YAML 可编辑)」
        # 页头部 —— 那三个动作操作的就是编辑器里的这份文件, 跟编辑器放一起才直观。
        # 左侧只留"要跑哪个任务"这个运行主控, 不再混着文件操作。
        lv.addSpacing(8)
        run_h = QHBoxLayout()
        self.btn_start = QPushButton("▶ 启动任务")
        self.btn_start.setStyleSheet("font-weight:bold;")
        self.btn_start.clicked.connect(self._start_task)
        self.btn_stop = QPushButton("■ 停止")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_task)
        run_h.addWidget(self.btn_start)
        run_h.addWidget(self.btn_stop)
        lv.addLayout(run_h)
        # 2026-10-17 v3.10.0: 这一行状态文字(空闲/运行中/完成/失败)已挪到
        # 上面状态灯的右侧, 每账号各一行 —— 见 _rebuild_status_lights()。
        # 原来只有一行、且只跟当前账号, 多开时别的号跑成什么样看不见。
        # 2026-10-09 v2.0.0: 「多开管理」原是右侧页签, 单开时用不到却一直占位置,
        # 改成这里的按钮按需弹出独立窗口 —— 与启动/停止同组(都是"怎么跑任务")。
        self.btn_multi = QPushButton("多开管理…")
        self.btn_multi.setToolTip(
            "管理多个 MuMu 模拟器实例(账号 x ADB端口 x 脚本), 可一键并行启动。\n"
            "只用当前这一个号的话用不到, 留着不碍事。")
        self.btn_multi.clicked.connect(self._open_multi_panel)
        lv.addWidget(self.btn_multi)
        # ---- 定时任务列表: 每天到点自动执行所选脚本(时间 x 脚本) ----
        self.lbl_timer = QLabel("定时任务(每天到点自动执行):")
        self.lbl_timer.setStyleSheet("color:#888; font-size:11px;")
        lv.addWidget(self.lbl_timer)
        self.tbl_timer = QTableWidget(0, 4, self)
        self.tbl_timer.setHorizontalHeaderLabels(["启用", "时间", "脚本", "账号"])
        self.tbl_timer.setColumnWidth(0, 46)
        self.tbl_timer.setColumnWidth(1, 58)
        self.tbl_timer.setColumnWidth(3, 138)
        self.tbl_timer.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.tbl_timer.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.tbl_timer.horizontalHeader().setSectionResizeMode(3, QHeaderView.Fixed)
        self.tbl_timer.horizontalHeaderItem(3).setToolTip(
            "本机=当前GUI设备(主窗口跑) / 某账号=以子进程方式跑该模拟器端口(多开)")
        self.tbl_timer.verticalHeader().setVisible(False)
        self.tbl_timer.setMaximumHeight(150)
        self.tbl_timer.setEditTriggers(QTableWidget.DoubleClicked)  # 双击单元格改时间/脚本
        lv.addWidget(self.tbl_timer)
        tbtn_h = QHBoxLayout()
        btn_add_timer = QPushButton("＋添加定时")
        btn_add_timer.clicked.connect(self._add_timer_dialog)
        tbtn_h.addWidget(btn_add_timer)
        btn_del_timer = QPushButton("删除选中")
        btn_del_timer.clicked.connect(self._del_timer_rows)
        tbtn_h.addWidget(btn_del_timer)
        lv.addLayout(tbtn_h)
        self.lbl_timer_state = QLabel("定时: 0 条待命")
        self.lbl_timer_state.setStyleSheet("color:#888; font-size:11px;")
        lv.addWidget(self.lbl_timer_state)
        lv.addStretch(1)
        # ---- 右: 实时监控(多标签) ----
        right = self.tabs = QTabWidget()
        # 实时画面: 账号下拉 + 大画面 + 缩略图墙
        # —— 多开的每个账号都在这里, 缩略图一眼看到全部, 点一下切成大图
        self.page_screen = AccountScreenPage(frame_of=self.frame_of)
        self.page_screen.picked.connect(self._on_screen_clicked)
        self.page_screen.account_selected.connect(self._on_page_account_selected)
        self.view_screen = self.page_screen.view     # 取坐标/采集模板沿用这个引用
        # 2026-10-09 v2.0.0: 「识别模板库」从左侧搬到这里。采集模板的完整动作是
        # 「在画面上点一下取坐标 → 点采集」, 两步都发生在这个页面上, 放一起才闭环;
        # 左侧留给运行控制(任务/启停/定时), 不再塞平时用不到的采集工具。
        screen_wrap = QWidget()
        sw = QVBoxLayout(screen_wrap)
        sw.setContentsMargins(0, 0, 0, 0)
        sw.setSpacing(4)
        sw.addWidget(self.page_screen, stretch=1)
        sw.addWidget(self._build_template_bar())
        right.addTab(screen_wrap, "实时画面")
        # 2026-10-19 v4.9.0(P1-2): 切页签要感知 —— 不在画面页时不做任何帧转码,
        # 切回来时整页补一帧(见 render_mixin._on_main_tab_changed)。
        self.tabs.currentChanged.connect(
            lambda i: _safe_call(self._on_main_tab_changed, i))
        # 实时步骤: 账号筛选 + 步骤列表 + 日志(每行带账号标记)
        steps_w = QWidget()
        sv = QVBoxLayout(steps_w)
        filt = QHBoxLayout()
        filt.addWidget(QLabel("账号:"))
        self.combo_step_acc = QComboBox()
        self.combo_step_acc.setMinimumWidth(220)
        self.combo_step_acc.currentIndexChanged.connect(self._on_step_filter_changed)
        filt.addWidget(self.combo_step_acc)
        self.lbl_step_hint = QLabel("")
        self.lbl_step_hint.setStyleSheet("color:#888; font-size:11px;")
        self.lbl_step_hint.setWordWrap(True)
        filt.addWidget(self.lbl_step_hint, stretch=1)
        sv.addLayout(filt)
        self.list_steps = QListWidget()       # 步骤列表(当前高亮)
        sv.addWidget(self.list_steps, stretch=1)
        self.txt_log = QPlainTextEdit()       # 日志
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(2000)
        sv.addWidget(self.txt_log, stretch=1)
        right.addTab(steps_w, "实时步骤")
        # 2026-10-09: 「脚本」页加"脚本"下拉 —— 总脚本 / 各子脚本(parts/*.yaml),
        # 选中哪个就打开哪个来改, Ctrl+S 写回它自己(以后脚本多了不用再翻总文件)。
        edit_w = QWidget()
        ev = QVBoxLayout(edit_w)
        ev.setSpacing(4)
        ehead = QHBoxLayout()
        ehead.addWidget(QLabel("脚本:"))
        self.combo_edit_script = QComboBox()
        self.combo_edit_script.setMinimumWidth(320)
        self.combo_edit_script.setToolTip(
            "「整个任务」= 总脚本(把各子脚本串起来的那个)\n"
            "选中某个子脚本 = 直接打开它(parts/*.yaml)来改\n"
            "Ctrl+S 保存写回当前打开的这个文件")
        self.combo_edit_script.currentIndexChanged.connect(self._on_edit_script_changed)
        ehead.addWidget(self.combo_edit_script)
        # 2026-10-09 v2.0.0: 这三个文件操作从左侧搬来(它们操作的就是本编辑器里的
        # 这份文件, 放一起不用左右找)。Ctrl+N / Ctrl+S 全局快捷键照常可用。
        self.btn_open2 = QPushButton("打开脚本…")
        self.btn_open2.setToolTip(
            "打开任务目录里已有的 .yaml(外部拷进来的也能这样加进下拉)\n"
            "要建新脚本请用旁边的「＋ 新建脚本…」")
        self.btn_open2.clicked.connect(self._open_task_file)
        ehead.addWidget(self.btn_open2)
        self.btn_new2 = QPushButton("＋ 新建脚本…")
        self.btn_new2.setToolTip(
            "新建脚本向导: 选顶层任务 / parts 子脚本, 带骨架模板与按钮库,\n"
            "可直接把 include 挂进当前任务 (Ctrl+N)")
        self.btn_new2.clicked.connect(self._new_task_file)
        ehead.addWidget(self.btn_new2)
        self.btn_save = QPushButton("保存 (Ctrl+S)")
        self.btn_save.setToolTip(
            "保存**编辑器当前打开的那份文件**(可能是总脚本, 也可能是某个子脚本)\n"
            "启动任务时也会自动把未保存改动写回同一个文件")
        self.btn_save.clicked.connect(self._save_task)
        ehead.addWidget(self.btn_save)
        ehead.addStretch(1)
        ev.addLayout(ehead)
        # 行2: 从按钮库插片段 + 当前正在编辑的文件
        ehead2 = QHBoxLayout()
        btn_ins = QPushButton("插入按钮/界面…")
        btn_ins.setToolTip("从按钮库(ui_registry.yaml)挑一个界面/按钮,\n"
                           "自动生成脚本片段插到光标处 (Ctrl+B)\n"
                           "—— 不用再记按钮叫什么、坐标是多少")
        btn_ins.clicked.connect(self._insert_button_snippet)
        ehead2.addWidget(btn_ins)
        self.lbl_edit_path = QLabel("")
        self.lbl_edit_path.setStyleSheet("color:#888; font-size:11px;")
        ehead2.addWidget(self.lbl_edit_path, stretch=1)
        ev.addLayout(ehead2)
        self.edit_script = QPlainTextEdit()   # 脚本编辑器
        self.edit_script.setFont(self._mono_font())
        self.edit_script.textChanged.connect(self._on_edit_text_changed)
        ev.addWidget(self.edit_script, stretch=1)
        right.addTab(edit_w, "脚本(YAML 可编辑)")
        # 2026-10-09 新增: 「脚本步骤」页 —— 列出当前脚本(展开后)全部步骤,
        # 支持"点/双击某一步 → 从该步开始跑"(补跑单段/调试, 省去前面的等待)
        browse_w = QWidget()
        bv = QVBoxLayout(browse_w)
        brow = QHBoxLayout()
        brow.addWidget(QLabel("脚本:"))
        self.combo_browse_script = QComboBox()
        self.combo_browse_script.setMinimumWidth(320)
        self.combo_browse_script.setToolTip(
            "只看某一个子脚本的步骤, 方便调试特定玩法。\n"
            "注意: 列表里的序号始终是『整个任务里的第几步』,\n"
            "所以「从选中步骤开始跑」的位置不会错。")
        self.combo_browse_script.currentIndexChanged.connect(
            lambda *_: self._refresh_browse_steps())
        brow.addWidget(self.combo_browse_script)
        brow.addStretch(1)
        bv.addLayout(brow)
        self.lbl_browse_hint = QLabel("")
        self.lbl_browse_hint.setStyleSheet("color:#888; font-size:11px;")
        self.lbl_browse_hint.setWordWrap(True)
        bv.addWidget(self.lbl_browse_hint)
        self.list_browse = QListWidget()
        self.list_browse.setToolTip(
            "当前脚本展开后的步骤(含循环/包含展开; 上面的下拉框可只看某个子脚本)。\n"
            "序号 = 整个任务里的第几步。双击某一步 = 从该步开始执行(前面跳过)")
        self.list_browse.itemDoubleClicked.connect(
            lambda *_: self._start_from_selected_step())
        bv.addWidget(self.list_browse, stretch=1)
        bfoot = QHBoxLayout()
        btn_browse_refresh = QPushButton("刷新步骤")
        btn_browse_refresh.setToolTip("重新解析当前脚本, 刷新步骤列表(改了脚本后点这里)")
        btn_browse_refresh.clicked.connect(self._refresh_browse_steps)
        bfoot.addWidget(btn_browse_refresh)
        self.btn_browse_run = QPushButton("▶ 从选中步骤开始跑")
        self.btn_browse_run.setStyleSheet("font-weight:bold;")
        self.btn_browse_run.setToolTip("从列表里选中的那一步开始执行(不会重跑前面的步骤)")
        self.btn_browse_run.clicked.connect(lambda *_: self._start_from_selected_step())
        bfoot.addWidget(self.btn_browse_run)
        bv.addLayout(bfoot)
        right.addTab(browse_w, "脚本步骤")
        # 按钮库(注册表查看/编辑/入库)
        self.panel_registry = RegistryPanel(self)
        right.addTab(self.panel_registry, "按钮库(注册表)")
        # 多开管理(账号 x ADB端口 x 脚本, 一键并行)
        # 2026-10-09 v2.0.0: 页签撤掉, 改成左侧「多开管理…」按钮按需弹出的独立窗口 ——
        # 单开时白占一个页签, 但功能不能删, 所以换个不挡路的位置。
        # **实例必须保留**: 下面这些地方都依赖它, 删掉会直接崩:
        #   227/258  窗口关闭时回收子进程(防孤儿进程继续操作模拟器)
        #   1163     停止按钮的可用状态(port in pm.running_ports())
        #   1408     定时任务按指定账号启动
        #   1544/1564/1580  非本机端口账号的 启动/查重/停止
        self.panel_multi = MultiPanel(self)
        self.panel_multi.setWindowTitle("多开管理")
        self.panel_multi.resize(780, 540)
        # 2026-10-07: 脚本选项(ALAS 风格表单, 落 config.yaml, 下轮任务生效)
        from .settings_panel import SettingsPage
        self.page_settings = SettingsPage()
        # 本页的「导出诊断信息…」按钮需要主窗口的设备状态, 注入回调
        self.page_settings.on_export = self._export_diagnostics
        right.addTab(self.page_settings, "脚本选项")
        self.page_settings.set_task(self.combo_task.currentData())
        # 2026-10-19 v5.6.0: 运行历史(从 logs/reports/*_p<port>.json 读历史运行记录)
        from .history_page import HistoryPage
        self.page_history = HistoryPage()
        right.addTab(self.page_history, "运行历史")
        # 点击画面后的坐标工具条(浮在右侧顶部)
        self.coord_bar = QLabel("最近取点: 无")
        self.coord_bar.setStyleSheet("background:#f5f5f5; padding:2px 6px;")
        right.setCornerWidget(self.coord_bar, Qt.TopRightCorner)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([250, 900])
        lay = QVBoxLayout(central)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.addWidget(split)
        # 快捷键
        act = QAction(self)
        act.setShortcut(QKeySequence.Save)
        act.triggered.connect(self._save_task)
        self.addAction(act)
        act_new = QAction(self)
        act_new.setShortcut(QKeySequence.New)           # Ctrl+N
        act_new.triggered.connect(self._new_task_file)
        self.addAction(act_new)
        act_ins = QAction(self)
        act_ins.setShortcut(QKeySequence("Ctrl+B"))
        act_ins.triggered.connect(self._insert_button_snippet)
        self.addAction(act_ins)
    @staticmethod
    def _mono_font():
        from PySide6.QtGui import QFont
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        return f
    def closeEvent(self, ev):
        """关窗: 有托盘则最小化到托盘(定时任务继续在后台跑),
        托盘菜单"退出"才真正关闭。

        2026-09-28 调整: 用户曾误以为"关掉窗口=程序退出了", 实际进程还在后台,
        于是出现"退出后仍有残留进程占着文件夹"。现在:
          · 最小化到托盘时, 提示里明确写出【进程仍在后台】和【怎么彻底退出】;
          · 有任务在跑时, 真正退出要二次确认, 避免误杀正在跑的任务;
          · 真退出走 _full_shutdown(), 把子进程/adb/日志句柄/设备锁全部收干净。
        2026-10-16 补充(一): 光靠托盘不够 —— 托盘图标被系统折叠时用户根本找不到
        出口。已加菜单栏「文件」: 最小化到托盘 / 退出(Ctrl+Q), 且每次隐藏都提示。

        2026-10-16 补充(二) ★真 bug: 上面的"出口"其实还是退不掉!
        用户走的是正确路径(托盘右键 → 退出), v2 进程照样挂了一整天 —— 因为
        Qt 只在"最后一个**可见**窗口被关闭"时结束事件循环, 而窗口早已被 hide()
        到托盘了, 此时再 close() 不产生可见性变化 → Qt 不触发退出 →
        app.exec() 永不返回。已写最小复现验证:
          场景A 窗口可见时退出   → app.exec() 立刻返回(正常)
          场景B 先隐藏再退出     → 事件循环 3 秒后仍在跑(bug 复现)
        修法: 真退出分支末尾显式 QApplication.quit(), 不再依赖 Qt 的隐式判定。
        """
        if getattr(self, "tray", None) and self.tray.isVisible() and not self._really_quit:
            ev.ignore()
            self.hide()
            # 2026-10-16: 原来"只提示一次"——用户第一次没看见(气泡 6 秒就消失,
            # 托盘图标又常被系统折叠), 之后每次关窗都无声无息, 于是长期以为
            # 自己已经退出了。现在每次都提示, 并给出菜单这条不依赖托盘的路径。
            running = self._running_task_summary()
            msg = ("程序没有退出, 已隐藏到右下角托盘"
                   f"{'，' + running + ' 的任务继续在后台跑' if running else ''}。\n"
                   "要彻底退出: 菜单「文件 → 退出」或右键托盘图标 → 退出。")
            self.tray.showMessage(APP_TITLE, msg,
                                  QSystemTrayIcon.Information, 6000)
            return
        # 真正退出: 有任务在跑就先确认(避免误关掉正在跑的任务)
        running = self._running_task_summary()
        if running and not self._quitting_confirmed:
            self._quitting_confirmed = True
            # 窗口若已隐藏到托盘, 以它为父的确认框可能不可见 —— 用户看不见框,
            # 程序就永久卡在这个模态循环里(进程活着却叫不应)。先把它拉回来。
            was_visible = self.isVisible()
            if not was_visible:
                self.show()
                self.raise_()
                self.activateWindow()
            r = QMessageBox.question(
                self, "确认退出",
                f"以下任务还在运行:\n\n    {running}\n\n"
                "退出会立即停止它们, 并结束模拟器上的自动操作。确定退出吗?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            self._quitting_confirmed = False
            if r != QMessageBox.Yes:
                self._really_quit = False
                if not was_visible:      # 用户取消 → 恢复"藏在托盘"的原样
                    self.hide()
                ev.ignore()
                return
        self._full_shutdown()
        # 关窗时一并移除托盘图标, 否则图标会残留到鼠标划过才消失
        try:
            if getattr(self, "tray", None):
                self.tray.hide()
        except Exception:
            pass
        super().closeEvent(ev)
        # ★ 2026-10-16: 走到这里说明用户确实要退出(已过二次确认)。必须显式结束
        # 事件循环 —— 窗口若早已 hide() 到托盘, Qt 的 lastWindowClosed 不会触发,
        # app.exec() 永不返回, 进程就一直挂着(实测: v2 这样挂了一整天)。
        app = QApplication.instance()
        if app is not None:
            app.quit()
    # ================= 设备 =================
    def _connect_device(self):
        """建本机 adb 通道(用于「当前账号=本机」时的直接截图) + 起状态自愈轮询

        2026-10-15: 旧版建通道失败就永久红字「✗ 连接失败」, 可任务该跑还是能跑
        (引擎每次操作前自己会重连), 看着很吓人。现在改成: 建通道失败也照常往下走,
        由后台探测线程每 10 秒批量探一次(见 _start_probe), 连上了自己转绿。
        2026-10-16: 状态灯从「单端口」改为「多端口」(见 _rebuild_status_lights),
        与「多开管理」(accounts.json)的端口一一对应。
        """
        self._adb_global_error = None
        try:
            d = self.cfg["device"]
            # 2026-10-16: connect=False —— 只建句柄不连设备。构造里那次 connect 在
            # 端口没人监听时要卡好几秒, 会把启动/主线程拖住(见 adb_client 注释)。
            # 真正的连通性由后台探测线程负责, shell/screenshot 也会按需自愈。
            self.device = AdbDevice(d["adb_path"], d["host"], d["port"],
                                    connect=False)
        except Exception as e:
            self.device = None
            # adb 整体不可用: 所有端口灯都落这个错(更具体的全局原因)
            self._adb_global_error = (str(e).splitlines() or [""])[0][:60]
        # 识别器(惰性; OCR 首次调用会加载模型，放后台线程)
        self.matcher = TemplateMatcher(
            self.cfg["recognition"]["template_dir"],
            self.cfg["recognition"]["match_threshold"])
        # 状态自愈: 每 10 秒探测一次(模拟器后开也能自动转绿)。
        # ★2026-10-16 性能修复★ 探测改到后台线程 + 批量 adb, 绝不再占用 GUI 线程:
        #   旧写法在主线程里逐个端口 reconnect(), 每端口 3 条 adb 子进程;
        #   实测在线端口 ~0.6s、离线端口 ~4.6s, 5 个端口 = 3~19 秒一次,
        #   每 10 秒来一发 → 界面就是"卡一下卡一下"、甚至长时间无响应。
        self._probe_q = queue.Queue()
        self._probe_busy = False
        self._probe_pending = None
        self._front_dev = None                      # 只为取本机前台 Activity
        self._dev_timer = QTimer(self)
        self._dev_timer.timeout.connect(lambda: _safe_call(self._start_probe))
        self._dev_timer.start(10000)
        self._start_probe()
    # ---------- 端口状态探测(全部在后台线程, 绝不阻塞 GUI) ----------
    def _start_probe(self, only: int | None = None):
        """发起一次端口探测(后台线程)。同一时刻只跑一轮, 避免探测叠加。

        only=None    → 探测所有状态灯端口(定时轮询 / 多开管理变更后)
        only=<port>  → 只探测这一盏(用户点灯时的立即重连)
        """
        if getattr(self, "_probe_q", None) is None:
            return
        lights = getattr(self, "_port_lights", None) or {}
        if only is not None:
            # 点了灯就先给即时反馈 —— 后台可能正有一轮在跑(最长几秒), 不能等到
            # 它结束才让用户看到"点了有反应"。
            only = int(only)
            light = lights.get(only)
            if light is not None:
                light.set_state(None, "正在重连…")
        if self._probe_busy:
            if only is not None:
                self._probe_pending = only          # 本轮结束后补一次, 不丢点击
            return
        ports = list(lights.keys())
        if only is not None:
            ports = [only] if only in lights else []
        if not ports:
            return
        self._probe_busy = True
        d = self.cfg["device"]
        # 本机端口在线时顺手取一次前台 Activity(dumpsys ~0.9s, 在后台无所谓)
        want_front = self._local_port if self._local_port in ports else None
        threading.Thread(
            target=self._probe_worker,
            args=(d["adb_path"], d["host"], ports, want_front),
            daemon=True).start()
    def _probe_worker(self, adb_path, host, ports, want_front):
        """后台线程体: 批量探测端口是否在线, 结果丢进队列交给 GUI 线程。

        这里只碰 adb 子进程和普通属性, **不碰任何 Qt 控件** —— 界面更新一律
        回到主线程做(见 _drain_probe)。
        """
        res = {"ok": False, "err": "", "states": {}, "source": "", "front": ""}
        try:
            states, source = probe_ports(adb_path, host, ports)
            res["ok"] = True
            res["states"] = states
            res["source"] = source
            if want_front and states.get(int(want_front)):
                try:
                    if self._front_dev is None:
                        self._front_dev = AdbDevice(adb_path, host,
                                                    int(want_front), connect=False)
                    res["front"] = self._front_dev.current_activity() or "(桌面)"
                except Exception:
                    res["front"] = ""
        except Exception as e:
            res["err"] = (str(e).splitlines() or [""])[0][:60]
        self._probe_q.put(res)
    def _drain_probe(self):
        """GUI 线程: 取回后台探测结果并更新状态灯(由 _render_tick 每 250ms 调)"""
        if getattr(self, "_probe_q", None) is None:
            return
        got = False
        while True:
            try:
                res = self._probe_q.get_nowait()
            except queue.Empty:
                break
            got = True
            self._probe_busy = False
            self._apply_probe(res)
        if got:
            pend = self._probe_pending
            if pend is not None:
                self._probe_pending = None
                self._start_probe(only=pend)
    def _apply_probe(self, res: dict):
        """把探测结果落到状态灯上(只在 GUI 线程执行)"""
        lights = getattr(self, "_port_lights", None) or {}
        if not res.get("ok"):
            err = res.get("err") or self._adb_global_error or "adb 不可用"
            for light in lights.values():
                light.set_state(False, err)
            return
        source = res.get("source") or ""
        front = res.get("front") or ""
        for port, light in lights.items():
            port = int(port)
            if port not in res["states"]:       # 本轮没探它(手动只点一盏), 保持原样
                continue
            if not res["states"][port]:
                light.set_state(False, f"未检测到模拟器 :{port}")
                continue
            detail = f"adb: {source}"
            if front and port == self._local_port:
                detail = f"前台: {front[:40]}\n{detail}"
            light.set_state(True, detail)

    # ---------- 每账号的任务状态(状态灯右侧那一列) ----------

    # ================= 任务脚本 =================

    # ================= 脚本选择(总脚本 / 子脚本) =================
    # 2026-10-09 新增: 「脚本」与「脚本步骤」两页都能单独挑一个子脚本。
    # 下拉项 = 整个任务 + 该任务 include 进来的每个子脚本(engine.list_sub_scripts),
    # 以后加玩法脚本时只要 include 进来, 这两处会自动多一项, 不用改 GUI 代码。

    # ================= 脚本步骤(从选中步骤开始跑) =================

    # ================= 新建脚本(2026-10-09 v1.9.0) =================

    # ================= 模板 =================

    # ================= 全局账号切换 =================
    def _target_accounts(self) -> list[tuple[int, str]]:
        """「当前账号」下拉的选项: 本机 + 多开账号(端口重复的本机优先)"""
        out = [(self._local_port, "本机(GUI设备)")]
        for acc in _load_accounts():
            try:
                p = int(acc["port"])
            except (TypeError, ValueError):
                continue
            if p == self._local_port:
                continue
            out.append((p, f"{acc['name']} (端口{p})"))
        return out
    def _rebuild_target_combo(self):
        self._syncing_target = True
        try:
            cur = self.combo_target.currentData() if hasattr(self, "combo_target") else None
            self.combo_target.clear()
            for p, txt in self._target_accounts():
                self.combo_target.addItem(txt, p)
            if cur is not None:
                i = self.combo_target.findData(int(cur))
                if i >= 0:
                    self.combo_target.setCurrentIndex(i)
        finally:
            self._syncing_target = False
    def _target_port(self) -> int:
        d = self.combo_target.currentData()
        return int(d) if d is not None else self._local_port
    def _set_current_port(self, port: int):
        """全局切账号的唯一入口: 主界面下拉 / 画面页 / 步骤页三处统一走这里,
        一次切换, 截图·启动·停止·画面·步骤全部跟随。"""
        port = int(port)
        if self._syncing_target:
            return
        self._syncing_target = True
        try:
            i = self.combo_target.findData(port)
            if i >= 0 and self.combo_target.currentData() != port:
                self.combo_target.setCurrentIndex(i)
            if self.store.get(port) is None:
                self.store.ensure(port, name=self._acc_name(port), source="child")
            # 先重建画面页/步骤页的账号下拉(新账号的选项要先进列表才选得上)
            self._page_sig = None
            self._render_screen_page()
            self.page_screen.select_port(port)
            self._select_step_account(port)
        finally:
            self._syncing_target = False
        self._steps_dirty = True
        self._render_steps()
        self._update_run_buttons()
    def _on_target_changed(self, _i):
        if not self._syncing_target:
            self._set_current_port(self._target_port())
    def _on_page_account_selected(self, port):
        """实时画面页切了账号 → 全局跟随"""
        if port is not None and not self._syncing_target:
            self._set_current_port(int(port))
    def _update_run_buttons(self):
        """按当前账号的任务状态更新停止按钮(本机=worker, 多开=子进程)"""
        pm = getattr(self, "panel_multi", None)
        if pm is None:
            return
        port = self._target_port()
        if port == self._local_port:
            self.btn_stop.setEnabled(bool(self.worker and self.worker.isRunning()))
        else:
            self.btn_stop.setEnabled(port in pm.running_ports())
    # ================= 截图/点击 =================
    # ---------------- 后台任务统一入口 (2026-10-17 v3.5.0 P1-1) ----------------
    def _run_bg(self, fn, on_done=None, on_fail=None, busy_text=None):
        """在后台线程跑 fn, 结果回主线程; 期间按钮置灰并显示 busy_text。

        这是「截图 / 采集模板 / 重载模板库」的统一走法。它们共同点: 要同步等 adb
        或读几十张 png, 放主线程就是实打实的界面冻结(端口没开时一次 4.6 秒)。
        """
        w = FuncWorker(fn)
        self._bg_workers.append(w)          # 持有引用, 防 GC 时线程还在跑
        QC = Qt.ConnectionType.QueuedConnection
        # 槽函数必须留引用: 事后要按槽精确 disconnect。
        # 无参 disconnect() 在 PySide6 下会报
        # "Failed to disconnect (None) from signal"(它不知道要断哪个)。
        pairs = []      # (signal, slot)
        def _cleanup(*_a):
            try:
                self._bg_workers.remove(w)
            except ValueError:
                pass
            for sig, slot in pairs:
                try:
                    sig.disconnect(slot)
                except Exception:
                    pass
            try:
                w.deleteLater()
            except Exception:
                pass
            self._set_busy(False)
        def _on_done(r):
            _safe_call(on_done, r)
        def _on_fail(m):
            _safe_call(on_fail, m)
        if on_done is not None:
            w.done.connect(_on_done, QC)
            pairs.append((w.done, _on_done))
        if on_fail is not None:
            w.failed.connect(_on_fail, QC)
            pairs.append((w.failed, _on_fail))
        w.finished.connect(_cleanup, QC)
        pairs.append((w.finished, _cleanup))
        if busy_text is not None:
            self._set_busy(True, busy_text)
        w.start()
        return w
    def _set_busy(self, busy: bool, text: str = ""):
        """后台忙碌态: 关掉会重复触发的按钮, 免得连点多起几条 adb"""
        for b in ("btn_shot", "btn_new_tpl"):
            btn = getattr(self, b, None)
            if btn is not None:
                btn.setEnabled(not busy)
        if busy:
            self.lbl_hint.setText(text or "处理中…")
            self.lbl_hint.setStyleSheet("color:#c70; font-size:11px;")
        else:
            self.lbl_hint.setText("先在上面的画面点一下取坐标, 再点采集(中心裁 80x80)")
            self.lbl_hint.setStyleSheet("color:#888; font-size:11px;")
    def _manual_screenshot(self, _then=None):
        """截「当前账号」一张。

        本机走常驻 device; 多开端口临时建 ADB 连接 —— 截图是只读操作,
        不影响该账号正在跑的任务, 也不与它的实时回传冲突(只是补一帧)。

        2026-10-17 v3.5.0(P1-1): adb 调用移出主线程。以前点一下「截图」按钮,
        端口没开时要**冻结 4.6 秒**(adb connect 自身 TCP 重试), 在线也约 0.6 秒。
        现在后台跑, 期间按钮置灰, 完事回主线程更新画面。
        `_then` 是可选的完成回调(采集模板要用: 没画面时先截图再继续裁)。
        """
        port = self._target_port()
        if port == self._local_port and not self.device:
            QMessageBox.information(self, "提示", "设备未连接")
            return False        # 告知调用方"没真的发起截图"(便于它复位自己的忙碌标志)
        def _job():
            from common import png_bytes_to_bgr
            if port == self._local_port:
                png = self.device.screenshot()
            else:
                d = self.cfg["device"]
                tmp = AdbDevice(d["adb_path"], d["host"], port)
                png = tmp.screenshot()
            return png_bytes_to_bgr(png)
        def _ok(bgr):
            self.store.set_frame(port, bgr)
            who = self._acc_name(port) or f"端口{port}"
            self.append_sys_log(f"[截图] {who} 手动截图完成")
            self.logger.info(f"手动截图完成: {who}")
            if callable(_then):
                _then()
            elif _then is not None:
                # 防御: 任何误传(如 Qt 的 clicked(bool)) 都不该让截图白干
                self.logger.warning(f"截图回调不是可调用对象, 已忽略: {_then!r}")
        def _err(msg):
            self.logger.error(f"截图失败: {msg}")
            QMessageBox.warning(self, "截图失败", msg)
        self._run_bg(_job, _ok, _err,
                     busy_text=f"正在截取端口{port}的画面…(端口没开时会慢几秒)")
        return True
    def frame_of(self, port: int):
        """某账号通道里缓存的最新画面(切账号/采集模板都按各自通道取, 避免张冠李戴)"""
        ch = self.store.get(int(port))
        return ch.frame if ch else None
    def pick_port(self) -> int:
        """最近一次取坐标所属的账号端口"""
        return int(self._click_port) if self._click_port is not None else self._local_port
    def _on_screen_clicked(self, port, x, y):
        self.last_click_xy = (x, y)
        self._click_port = int(port)
        ch = self.store.get(int(port))
        who = ch.label if ch else f"端口{port}"
        self.coord_bar.setText(
            f"最近取点: ({x},{y}) @ {who}  → 可采集模板/生成步骤")
    # ================= 定时任务 =================

    # ---------- v4.6.0(P0-4) 文本日志滚动 ----------

    # ---- v4.6.0(P0-5) 定时: 把"拉起"这件事单独拆出来, 错峰延迟也要用它 ----

    # ================= 任务控制 =================
    def _is_port_offline(self, port: int) -> bool:
        """所选账号端口是否已**确认离线**(后台状态灯持续探测的结果)

        只认 `_online is False`(明确探测到模拟器没起来)才判离线;
        True/None(在线 / 还没探到) 一律放行 —— 不误杀、不阻塞正常启动,
        离线的最终兜底交给子进程 run_one 的 is_online() 检查。
        """
        light = self._port_lights.get(int(port))
        if light is None:
            return False
        return getattr(light, "_online", None) is False

    def _start_task(self, auto=False, path=None, start_index=0):
        """启动任务; auto=True 为定时触发: 弹窗改日志、不写回编辑器, 避免无人值守卡住。

        start_index>0: 从脚本展开后的第 N 步(0基)开始执行 —— 供「脚本步骤」页
        的"从选中步骤开始跑"使用(跳过前面步骤, 直接补跑某一段)。

        2026-09-28: 按「当前账号」路由 —— 本机走 GUI 内 TaskWorker,
        多开账号交给 run_one.py 子进程(复用多开面板的启动/状态跟踪)。
        """
        start_index = int(start_index or 0)
        port = self._target_port()
        # 2026-10-20 v5.9.0: 启动前校验所选账号是否离线 —— 离线就记错误日志并阻止启动
        if self._is_port_offline(port):
            name = self._acc_name(port) or f"端口{port}"
            self.append_sys_log(
                f"[错误] 账号「{name}」(端口{port}) 当前离线，已阻止启动任务（请先启动模拟器）")
            if not auto:
                QMessageBox.warning(
                    self, "无法启动",
                    f"账号「{name}」(端口{port}) 当前离线，\n请先启动对应模拟器再运行任务。")
            return
        if port != self._local_port:
            self._start_remote_task(port, auto=auto, path=path, start_index=start_index)
            return
        if self.worker and self.worker.isRunning():
            if auto:
                self.append_sys_log("[定时] 任务已在运行, 跳过本次")
                return
            QMessageBox.information(self, "提示", "任务运行中，先停止")
            return
        if not self.device:
            if auto:
                self.append_sys_log("[定时] 设备未连接, 自动启动失败")
                self._set_port_state(port, "启动失败", "#c33", "定时启动: 设备未连接")
                return
            QMessageBox.warning(self, "提示", "设备未连接")
            return
        path = path or (self.combo_task.currentData() or self.current_task_path)
        if not path:
            if auto:
                self.append_sys_log("[定时] 未选择任务脚本, 自动启动失败")
                return
            QMessageBox.warning(self, "提示", "请先选择任务脚本")
            return
        # 2026-09-11 新增: 跨进程设备锁, 防止另一个 GUI/绿色包实例并发操作同一模拟器
        port = int(self.cfg["device"].get("port", 16384))
        if not acquire_device_lock(port):
            msg = f"端口{port} 已有其他任务实例在跑(可能是另一个GUI窗口), 本次不启动"
            self.append_sys_log(f"[锁] {msg}")
            if not auto:
                QMessageBox.warning(self, "提示", msg)
            return
        if not auto:
            # 手动启动: 先把编辑器里未保存的改动写回**它正打开的那份文件**。
            # 2026-10-09: 「脚本」页可切到子脚本, 不能再无条件写回 path(会覆盖总脚本)。
            self._flush_editor()
        if self.ocr is None:
            # OCR 延迟初始化(加载模型慢)，放后台
            pass
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self._stop_t0 = None
        self._clear_port_state(self._local_port)   # 交回自动推导 → 显示"运行中"
        # 本机任务同样是一条"通道", 与多开账号同构 —— 这样画面页/步骤页
        # 不必区分"本机"与"子进程", 只多一个来源标记
        task = load_task(path)
        self.store.local_start(self._local_port,
                               [s.describe() for s in task.steps],
                               task=task.name)
        if start_index > 0:
            self.append_sys_log(
                f"[启动] 从第 {start_index + 1} 步开始执行(跳过前 {start_index} 步): "
                f"[{task.steps[start_index].name if start_index < len(task.steps) else '?'}]")
        self._steps_sig = None
        self._steps_dirty = True
        self._render_all(force=True)
        self._select_step_account(self._local_port)   # 自动聚焦本机, 免得看错账号
        self.worker = TaskWorker(
            self.device, self.matcher,
            OcrEngine() if self.cfg["recognition"].get("ocr_engine") == "rapidocr" else None,
            path, self.cfg["engine"]["poll_interval"], ui=self.ui_registry,
            start_index=start_index)
        # 2026-10-17 v3.5.0(P1-2): 全部显式 QueuedConnection。
        # 以前靠 Qt 的 Auto 判定: 这里碰巧总是排队的(信号从 worker 线程发出、
        # 接收方在主线程), 但这个"碰巧"建立在「connect 一定发生在主线程」之上
        # —— 一旦哪天从别的线程 connect, Auto 会退化成 DirectConnection,
        # 槽函数就直接在 worker 线程里操作 QWidget(未定义行为 / 随机崩溃)。
        # 跨线程一律写死 Queued, 不给它猜的机会。
        QC = Qt.ConnectionType.QueuedConnection
        self.worker.log.connect(lambda lv, m: _safe_call(self._on_log, lv, m), QC)
        self.worker.screenshot.connect(lambda bgr: _safe_call(self._on_shot, bgr), QC)
        self.worker.step_start.connect(lambda i, d: _safe_call(self._on_step_start, i, d), QC)
        self.worker.step_end.connect(lambda i, n, ok: _safe_call(self._on_step_end, i, n, ok), QC)
        self.worker.task_finished.connect(
            lambda ok, info, s, st: _safe_call(self._on_finished, ok, info, s, st), QC)
        # 2026-10-17 v3.5.0(P1-3): 线程结束后回收。
        # 旧代码从不 disconnect / deleteLater, 旧 TaskWorker 一路堆积;
        # 典型症状是退出时 QThread: Destroyed while thread is still running 硬崩。
        self.worker.finished.connect(self._on_worker_finished, QC)
        self.worker.start()
        # 2026-09-29 双保险: 前面 _select_step_account 的信号联动曾把停止按钮
        # 置灰(worker 当时还是 None/旧线程), 这里在 worker 真正启动后再点亮一次
        self.btn_stop.setEnabled(True)
    def _on_worker_finished(self):
        """任务线程 run() 已返回 —— 回收它(P1-3)

        2026-10-17 v3.5.0: 以前旧 worker 从不 disconnect 也不 deleteLater,
        跑几次任务就攒下几个僵尸 QThread; 退出时若还有线程没退干净, Qt 会抛
        `QThread: Destroyed while thread is still running` 并硬崩。
        这里: 断开全部信号 → deleteLater() → 把主窗口的引用置空
        (置空很重要: C++ 对象被延迟删除后, 再调 isRunning() 会
        RuntimeError: Internal C++ object (TaskWorker) already deleted)。
        """
        w = self.worker
        if w is None:
            return
        for sig in ("log", "screenshot", "step_start", "step_end", "task_finished"):
            try:
                getattr(w, sig).disconnect()
            except Exception:
                pass
        try:
            # 无参 disconnect = 断开 finished 上的全部连接(这里只有本方法一个)。
            # 不写 disconnect(self._on_worker_finished) 是因为那要求 self 上还能取到
            # 这个方法, 一旦本方法被改成静态/挪走就静默失败(被 except 吞掉)。
            w.finished.disconnect()
        except Exception:
            pass
        try:
            w.deleteLater()
        except Exception:
            pass
        self.worker = None
        self._update_run_buttons()
    def _retire_worker(self, timeout_ms: int = 5000) -> bool:
        """退出收尾: 请求停止并等任务线程退干净, 返回是否正常退出

        2026-10-17 v3.5.0(P1-3): 旧代码 `wait(5000)` **超时后照样往下走**并最终
        quit() —— 线程还在跑就被拆, 于是 `QThread: Destroyed while thread is
        still running`。现在超时后: 记 crash.log(留证据) → terminate() → 再等。
        terminate() 是最后手段(可能留下引擎半截状态), 但比崩在退出路上强。
        """
        w = self.worker
        if w is None:
            return True
        try:
            w.stop()
        except Exception:
            pass
        try:
            if w.wait(timeout_ms):
                return True
        except Exception:
            return False
        # —— 走到这里说明 5 秒没退出来 ——
        try:
            from crash_guard import write_crash
            write_crash("WORKER NOT STOPPING (forced terminate at shutdown)",
                        f"等待 {timeout_ms}ms 后任务线程仍未退出, 已强制 terminate。\n"
                        "通常意味着引擎卡在一次 adb 调用或长 sleep 里没有检查停止标志。\n")
        except Exception:
            pass
        try:
            w.terminate()
        except Exception:
            pass
        try:
            return bool(w.wait(3000))
        except Exception:
            return False
    def _open_multi_panel(self):
        """打开「多开管理」独立窗口(2026-10-09: 不再占右侧页签, 改为按需弹出)"""
        pm = getattr(self, "panel_multi", None)
        if pm is None:
            QMessageBox.information(self, "提示", "多开面板未初始化")
            return
        if pm.isHidden():
            pm.show()
        pm.raise_()
        pm.activateWindow()
    def _start_remote_task(self, port: int, auto=False, path=None, start_index=0):
        """对多开账号端口启动任务: 拉起 run_one.py 子进程(日志/画面经实时通道回流)"""
        start_index = int(start_index or 0)
        path = path or (self.combo_task.currentData() or self.current_task_path)
        if not path:
            if auto:
                self.append_sys_log("[定时] 未选择任务脚本, 自动启动失败")
                return
            QMessageBox.warning(self, "提示", "请先选择任务脚本")
            return
        name = self._acc_name(port) or f"端口{port}"
        task_name = Path(path).name
        if port in self.panel_multi.running_ports():
            msg = f"「{name}」已有任务在跑, 先停止再启动"
            self.append_sys_log(f"[启动] {msg}")
            if not auto:
                QMessageBox.information(self, "提示", msg)
            return
        # 设备锁预检(仅 UX 提示; 真正持锁的是子进程 run_one)
        if not acquire_device_lock(port):
            msg = f"端口{port} 已有其他任务实例在跑, 本次不启动"
            self.append_sys_log(f"[锁] {msg}")
            if not auto:
                QMessageBox.warning(self, "提示", msg)
            return
        release_device_lock(port)
        if not auto:
            # 手动启动: 编辑器里未保存的改动写回**它正打开的那份文件**
            # (2026-10-09: 「脚本」页可切到子脚本, 只写它自己, 不碰总脚本)
            self._flush_editor()
        res = self.panel_multi.launch(port, task_name, name, start_index=start_index)
        if res is True:
            self.append_sys_log(f"[启动] 「{name}」任务 {task_name} 已提交(子进程, 日志 logs/run_{port}.log)")
            self._clear_port_state(port)      # 交回自动推导 → 该账号那行显示"运行中"
            self._set_current_port(port)      # 界面聚焦到这个账号
        else:
            msg = {False: "该账号已在运行", "busy": "端口与本机相同且本机任务在跑"}.get(
                res, f"启动失败({res})")
            self.append_sys_log(f"[启动] 「{name}」{msg}")
            if not auto:
                QMessageBox.information(self, "提示", msg)
    def _stop_task(self):
        port = self._target_port()
        if port != self._local_port:
            # 多开账号: 终止它的子进程(run_one 收尾会写结束事件, 通道状态自动收口)
            if self.panel_multi.stop_port(port):
                self.append_sys_log(f"[停止] 已请求终止「{self._acc_name(port) or port}」的任务子进程")
                self._set_port_state(port, "停止中…", "#c80", "已请求终止子进程")
                return
            # 2026-10-17 v4.0.1: 没有子进程在跑, 但通道还挂着"运行中"(子进程早退了
            # 却没人收口) —— 点停止就要把它清掉, 以前只能重启程序才恢复。
            ch = self.store.get(port)
            if ch is not None and ch.state == "running":
                self.store.mark_stopped(port, "已手动停止(子进程已退出)")
                self._clear_port_state(port)
                self.append_sys_log(
                    f"[停止] 端口{port} 已无子进程, 状态已收口(未见结束事件)")
            else:
                self.append_sys_log("[停止] 该账号当前没有在跑的任务")
            return
        if self.worker and self.worker.isRunning():
            # 先回显"已收到", 避免点了没反应(引擎真正退出要等当前等待轮结束)
            self._stop_t0 = time.time()
            self.append_sys_log("⏹ 已发送停止请求，正在等当前步骤安全退出…")
            self._clear_port_state(self._local_port)   # 由秒表接管显示
            self.worker.stop()
    # ================= 本机任务事件 → 通道 =================
    def _on_log(self, level, msg):
        # 统一进通道: 与多开账号的日志用同一套渲染, 每行都带账号标记
        self.store.push_log(self._local_port, level, msg)
        try:
            getattr(self.logger, level)(msg)   # 同时写 logs/run.log
        except Exception:
            self.logger.info(msg)
    def _on_shot(self, bgr):
        self.store.set_frame(self._local_port, bgr)
    def _on_step_start(self, idx, desc):
        self.store.step_start(self._local_port, idx, desc)
        self._steps_dirty = True
        self._scroll_to = (self._local_port, idx)
    def _on_step_end(self, idx, name, ok):
        self.store.step_end(self._local_port, idx, name, ok)
        self._steps_dirty = True
    def _on_finished(self, ok, info, summary="", stats=None):
        """任务结束

        2026-10-16 v3.0.0: summary = 失败明细汇总(空串表示零失败)。
        旧版只有 ok 一个布尔, 而 on_fail: continue 会让 ok=True —— 于是
        "整段跳过"和"全部做成"在界面上长得一模一样(帮派捐献事故)。
        现在: 全成功显示"完成 ✔"; 有步骤没做成显示"⚠ 完成(N步未做成)"并
        把逐条明细打进系统日志; 关键步骤(required)失败才是真的"失败"。

        2026-10-17 v3.5.0: stats = 结构化统计 {"failed","skipped",
        "required_failed","stopped"}。以前从 summary 文案里
        ``int(summary.split(" 步未做成")[0])`` 切数字 —— 引擎文案一改就静默
        得到 0, 界面随即显示"完成", 失败被彻底掩盖(P2-4)。现在一律读 stats。
        并把"真失败"与"条件未命中(探测类, 正常)"分开显示, 不再混为一谈。
        """
        stats = stats or {}
        stopped = self._stop_t0 is not None
        self._stop_t0 = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        # v4.6.0(P0-5) 把成败回写给拉起它的那个定时项(本机任务路径)
        self._timer_done(self._timer_local_key, bool(ok))
        self._rotate_logs("任务结束")   # v4.6.0(P0-4) 日志滚动三触发点 ③
        # 兜底: 极老的调用方可能没传 stats, 退回从明细结构里数(不再切文案)
        n_fail = int(stats.get("failed", 0) or 0)
        n_skip = int(stats.get("skipped", 0) or 0)
        if not stats and summary:
            n_fail = summary.count("[失败]") + summary.count("[关键]")
            n_skip = summary.count("[未命中]")
        # 2026-10-17 v3.10.0: 结论写到**本机账号那一行**(状态灯右侧), 不再
        #   单独占按钮下面一行 —— 多开时每个号各显示各的。
        # 2026-10-17 v3.7.0: 不再把"条件未命中"计数塞进状态标签。
        #   未命中是探测类步骤的正常结果(今天没这个奖励/已领过), 用户明确说
        #   这类不必显示 —— 它只在 run.log 里留档。
        # 2026-10-19 fix: 本机结论**不再用持久覆盖**, 改为交给下方 store.finish /
        #   mark_stopped 写进通道, 由 _port_state_of 按通道状态派生显示 —— 与多开
        #   子进程走同一套, 且结论展示数秒后会自动归位「空闲」(离线立即归位),
        #   不再永远挂在上一轮"完成/失败/已停止"上。
        if ok and n_fail:
            self.store.finish(self._local_port, True, f"完成但有{n_fail}步失败")
        elif ok:
            self.store.finish(self._local_port, True, "成功")
        elif stopped:
            self.store.mark_stopped(self._local_port)
        else:
            self.store.finish(self._local_port, False, info)
        self._steps_dirty = True
        self.append_sys_log("=" * 40)
        # 2026-10-17 v3.9.0 用户确立: 成功时**连子任务汇总也不要了**, 界面只给
        #   一行"最后执行到哪一步"。理由: "目前看设定的目标都能完成, 总结没用"。
        #   失败/终止仍给子任务行(排障需要); 逐步明细照旧只进 run.log。
        subs = stats.get("subtasks") or []
        last_step = str(stats.get("last_step", "") or "")
        if ok and n_fail:
            self.append_sys_log(f"任务跑完但有 {n_fail} 步失败 ⚠")
            for ln in _subtask_lines(subs):
                self.append_sys_log(ln)
        elif ok:
            self.append_sys_log("任务成功 ✔")
            # 2026-10-18 v4.3.0 (P0-1): 成功路径也要渲染带验收点的子任务行,
            # 让用户一眼看到 ✔/✘(面板说做没做成)。"最后执行到哪步"降为副标题。
            _verified_subs = [s for s in subs if s.get("verified") is not None]
            if _verified_subs:
                for ln in _subtask_lines(_verified_subs):
                    self.append_sys_log(ln)
            if last_step:
                self.append_sys_log(f"   ↳ 最后执行 {last_step}")
        else:
            self.append_sys_log(f"任务未完成: {info}")
            for ln in _subtask_lines(subs):
                self.append_sys_log(ln)
            if summary:
                self.append_sys_log(summary)
        if summary:
            try:
                self.logger.info("完成明细(逐步, 仅供排障):\n" + summary)
            except Exception:
                pass
        # 释放跨进程设备锁
        try:
            release_device_lock(int(self.cfg["device"].get("port", 16384)))
        except Exception:
            pass
        self._update_run_buttons()      # 若当前账号已切到别的端口, 按它的状态复位
    # ================= 多开子进程通道 =================
    def on_child_launched(self, port: int, name: str, task: str = ""):
        """「多开管理」拉起子进程时调用: 立刻把该账号挂到界面上, 不用等它写出文件"""
        port = int(port)
        self._readers.setdefault(port, LiveReader(port))
        self.store.reset(port,
                         name=name or self._acc_name(port) or f"端口{port}",
                         source="child", task=task)
        self.store.set_active(port, True)
        self._log_drawn[port] = 0
        self._steps_sig = None
        self._page_sig = None
        self._steps_dirty = True
        self._render_all(force=True)
    def on_child_finished(self, port: int):
        """子进程已退出(由 multi_panel 的 Popen.poll() 判定, 比 pid 判活权威)

        2026-10-17 v4.0.1: 这里必须**先把通道收口**再交回自动推导 ——
        被 terminate 的子进程不会回写 task_end, 通道 state 会一直停在 running,
        于是状态灯右边永远显示「运行中」(用户实测: 停止账号2 后卡住不走)。
        正常跑完的子进程已经写过 task_end(ok/fail), mark_stopped 不会覆盖它。
        """
        port = int(port)
        # v4.6.0(P0-5) 把成败回写给拉起它的那个定时项(多开子进程路径)
        tkey = self._timer_pending_ports.pop(port, None)
        self.store.set_active(port, False)
        ch = self.store.get(port)
        if ch is not None and ch.state == "running":
            self.store.mark_stopped(port, "已手动停止(子进程已退出)")
            self._steps_dirty = True
        if tkey and ch is not None:
            # 只有 fail/error 才退还当天份额让它重试 —— 用户手动停止的
            # (stopped)不算失败, 不重试, 否则"停了又自己跑起来"更吓人。
            self._timer_done(tkey, ch.state == "ok")
            if ch.state == "stopped":
                self._timer_runs[tkey] = datetime.date.today().isoformat()
                self._persist_timer_runs()
        # v3.10.0: 交回自动推导 —— 否则"停止中…"这类临时文字会一直挂着
        self._clear_port_state(port)
        self._page_sig = None
        self._update_run_buttons()      # 当前账号的子进程结束了, 停止按钮要复位
        self._rotate_logs("任务结束")   # v4.6.0(P0-4) 日志滚动三触发点 ③

def run_gui():
    from PySide6.QtWidgets import QApplication
    # Windows 任务栏归属: 设置 AppUserModelID, 避免任务栏图标被归到
    # pythonw.exe 进程而显示 Python 默认图标
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("xyfz.auto.assistant")
    except Exception:
        pass  # 非 Windows 环境忽略
    app = QApplication(sys.argv)
    if APP_ICON.exists():
        app.setWindowIcon(QIcon(str(APP_ICON)))
    # 2026-09-28: 单实例保护 —— 误双击两次(或 .bat + 快捷方式同时点)会出现两个
    # 窗口两个进程抢同一模拟器, 还会互相占用 logs\run.log。这里直接拦掉。
    #
    # 2026-10-16 v3.0.0: 互斥体名带上**主版本号**。
    # 原先是固定字符串 "xyfz_auto_gui_single", v2 与 v3 共用同一个名字 ——
    # 实测: v2.1.0 绿色版正在跑时启动 v3, 会被判定为"重复启动"直接退出,
    # 而这两个其实是并存的两个工程。带版本号后各自防重复、互不干扰,
    # 将来出 v4 也不用再改这里。
    _mutex = None
    try:
        cfg = load_config()
        if (cfg.get("app", {}) or {}).get("single_instance", True):
            _major = str(APP_VERSION).split(".")[0] if APP_VERSION else "0"
            _mutex = single_instance_mutex(f"xyfz_auto_gui_v{_major}")
            if _mutex is None:
                ok = activate_window(APP_TITLE)
                (QMessageBox.information if not ok else (lambda *a: None))(
                    None, APP_TITLE,
                    "程序已经在运行了 —— 请看右下角托盘的图标。\n"
                    "（本窗口是重复启动，已自动关闭；退出请用托盘右键 → 退出）")
                if ok:
                    print("程序已在运行, 已切换到已有窗口")
                sys.exit(0)
    except SystemExit:
        raise
    except Exception:
        pass
    w = MainWindow()
    # 兜底: 无论是托盘退出、窗口关闭还是异常退出, 都走一次彻底收尾
    app.aboutToQuit.connect(w._full_shutdown)
    w.show()
    sys.exit(app.exec())
