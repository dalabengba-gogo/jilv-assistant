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
import sys
import time
from pathlib import Path

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTime, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QPlainTextEdit,
    QPushButton,
    QSplitter, QSystemTrayIcon, QTabWidget, QTableWidget, QTableWidgetItem, QTimeEdit,
    QVBoxLayout, QWidget,
)

from common import (APP_TITLE, load_config, setup_logger, close_loggers,
                    acquire_device_lock, release_device_lock)
from accounts import load_accounts as _load_accounts
from proc_guard import SpawnGuard, activate_window, kill_own_adb_server, \
    release_all_device_locks, single_instance_mutex
from device import AdbDevice
from engine import dump_task, load_task, list_sub_scripts
from engine.ui_registry import UiRegistry
from live import LIVE_ROOT, LiveReader
from recognition import OcrEngine, TemplateMatcher

from gui.button_library import ButtonLibraryDialog
from gui.live_panel import AccountScreenPage
from gui.live_store import Channel, ChannelStore, StepRec
from gui.multi_panel import MultiPanel
from gui.new_script import KIND_PART, NewScriptDialog, insert_include_into_task
from gui.registry_panel import RegistryPanel
from gui.worker import TaskWorker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_ICON = PROJECT_ROOT / "assets" / "app_icon.ico"   # 窗口/任务栏图标


class MainWindow(QMainWindow):
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
        self._timer_runs: dict[str, str] = {}  # key="HH:MM|脚本名" -> 已执行日期
        self._really_quit = False             # 托盘"退出"置 True, 否则关窗=最小化到托盘
        self._tray_tip_shown = False          # 首次最小化的气泡提示只弹一次

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

        self._build_ui()
        self._rebuild_target_combo()   # 全局账号下拉(本机 + 多开账号)
        self._connect_device()
        self._restore_timer()
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
        self._timer.timeout.connect(self._tick_timer)
        self._timer.start(5000)

        # 多开子进程通道轮询: 增量读 logs/live/port_*/, 把画面/步骤/日志捞进 store
        self._live_timer = QTimer(self)
        self._live_timer.timeout.connect(self._poll_live)
        self._live_timer.start(600)
        # 界面刷新: 合并高频事件的渲染(步骤/画面/日志), 避免每来一个事件就重绘
        self._ui_timer = QTimer(self)
        self._ui_timer.timeout.connect(self._render_tick)
        self._ui_timer.start(250)

        # 2026-09-16: "停止中"秒表。停止是协作式的(要等当前截图/等待轮询结束),
        # 之前点完停止只有一句话不变, 用户不知道到底停没停、还要多久。
        self._stop_t0 = None
        self._stop_tick = QTimer(self)
        self._stop_tick.timeout.connect(self._tick_stop)
        self._stop_tick.start(200)

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
        ports_now = {p for p, _ in self._target_accounts()}
        if old_port not in ports_now:
            # 当前选中的账号被改了端口 → 按名字找到它的新端口
            hit = next((p for p, txt in self._target_accounts()
                        if txt == f"{old_name} (端口{p})"), None)
            self._set_current_port(hit if hit is not None else self._local_port)

    def _tick_stop(self):
        """刷新"停止中… Ns"提示"""
        if self._stop_t0 is None:
            return
        if self.worker and self.worker.isRunning():
            self.lbl_run_state.setText(
                f"停止中… 已等待 {time.time() - self._stop_t0:.0f}s")
        else:
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
        try:
            if self.worker and self.worker.isRunning():
                self.worker.stop()
                self.worker.wait(5000)
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

        lv.addWidget(QLabel("设备"))
        self.lbl_status = QLabel("连接中…")
        lv.addWidget(self.lbl_status)

        # 2026-09-28: 全局「当前账号」切换器 —— 截图/启动/停止/实时画面/实时步骤
        # 全部跟随这里, 换一处=处处换, 不用在每个页面单独切。
        lv.addWidget(QLabel("当前账号(全局)"))
        self.combo_target = QComboBox()
        self.combo_target.setToolTip(
            "全局账号开关: 截图、启动任务、停止, 以及右侧实时画面/实时步骤都跟随此处")
        self.combo_target.currentIndexChanged.connect(self._on_target_changed)
        lv.addWidget(self.combo_target)

        self.btn_shot = QPushButton("截图(当前账号)")
        self.btn_shot.clicked.connect(self._manual_screenshot)
        lv.addWidget(self.btn_shot)

        # 2026-09-28: 换机器后 via:win 步骤可能因找不到 MuMu 窗口而失效,
        # 这个按钮把"所有窗口清单"摆出来, 直接照抄就能修配置。
        self.btn_windiag = QPushButton("窗口诊断 (win 通道)")
        self.btn_windiag.setToolTip(
            "列出全部窗口并标出 MuMu 候选。任务里 via: win 的步骤报\n"
            "「未找到 MuMu 窗口」时点这里, 把候选行的 hwnd 填进 config.yaml")
        self.btn_windiag.clicked.connect(self._win_diag)
        lv.addWidget(self.btn_windiag)

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
        self.lbl_run_state = QLabel("空闲")
        lv.addWidget(self.lbl_run_state)

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
        right.addTab(self.page_settings, "脚本选项")
        self.page_settings.set_task(self.combo_task.currentData())

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
        """
        if getattr(self, "tray", None) and self.tray.isVisible() and not self._really_quit:
            ev.ignore()
            self.hide()
            if not self._tray_tip_shown:
                self._tray_tip_shown = True
                running = self._running_task_summary()
                msg = ("已最小化到右下角托盘 —— 注意: 程序进程仍在后台运行"
                       f"{'，' + running + ' 的任务继续跑' if running else ''}。\n"
                       "要彻底退出并释放文件占用: 右键托盘图标 → 退出。")
                self.tray.showMessage(APP_TITLE, msg,
                                      QSystemTrayIcon.Information, 6000)
            return

        # 真正退出: 有任务在跑就先确认(避免误关掉正在跑的任务)
        running = self._running_task_summary()
        if running and not self._quitting_confirmed:
            self._quitting_confirmed = True
            r = QMessageBox.question(
                self, "确认退出",
                f"以下任务还在运行:\n\n    {running}\n\n"
                "退出会立即停止它们, 并结束模拟器上的自动操作。确定退出吗?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            self._quitting_confirmed = False
            if r != QMessageBox.Yes:
                self._really_quit = False
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

    # ================= 设备 =================
    def _connect_device(self):
        try:
            d = self.cfg["device"]
            self.device = AdbDevice(d["adb_path"], d["host"], d["port"])
            online = self.device.is_online()
            front = self.device.current_activity() or "(桌面)"
            # 显示 adb 来源, 换机后一眼能看出用的是哪份 adb
            self.lbl_status.setText(
                f"● 在线\n前台: {front[:40]}\nadb: {self.device.adb_source}")
            self.lbl_status.setStyleSheet("color:#2a6;")
        except Exception as e:
            self.lbl_status.setText(f"✗ 连接失败: {e}")
            self.lbl_status.setStyleSheet("color:#c33;")

        # 识别器(惰性; OCR 首次调用会加载模型，放后台线程)
        self.matcher = TemplateMatcher(
            self.cfg["recognition"]["template_dir"],
            self.cfg["recognition"]["match_threshold"])

    # ================= 任务脚本 =================
    def _task_dir(self):
        return PROJECT_ROOT / "assets" / "tasks"

    def _refresh_task_list(self):
        cur = self.combo_task.currentText()
        self.combo_task.blockSignals(True)
        self.combo_task.clear()
        for p in sorted(self._task_dir().glob("*.yaml")):
            self.combo_task.addItem(p.name, str(p))
        if cur:
            i = self.combo_task.findText(cur)
            if i >= 0:
                self.combo_task.setCurrentIndex(i)
        self.combo_task.blockSignals(False)
        self._on_task_selected()

    def _on_task_selected(self):
        p = self.combo_task.currentData()
        if not p:
            self.edit_script.clear()
            return
        self.current_task_path = str(p)
        # 「脚本」/「脚本步骤」两页的"脚本"下拉框跟着换任务重建(2026-10-09)
        self._rebuild_script_pickers()
        # 重建后默认回到"整个任务", 把总脚本灌进编辑器
        self._load_file_into_editor(p)
        # 脚本选项页跟着换脚本(选项声明在各脚本自己的 yaml 里)
        if getattr(self, "page_settings", None) is not None:
            self.page_settings.set_task(p)
        # 「脚本步骤」页跟着换脚本(2026-10-09)
        if getattr(self, "list_browse", None) is not None:
            self._refresh_browse_steps()

    # ================= 脚本选择(总脚本 / 子脚本) =================
    # 2026-10-09 新增: 「脚本」与「脚本步骤」两页都能单独挑一个子脚本。
    # 下拉项 = 整个任务 + 该任务 include 进来的每个子脚本(engine.list_sub_scripts),
    # 以后加玩法脚本时只要 include 进来, 这两处会自动多一项, 不用改 GUI 代码。
    def _rebuild_script_pickers(self):
        """按当前任务刷新两个"脚本"下拉框(选中项一律回到"整个任务")"""
        p = self.combo_task.currentData() or self.current_task_path
        combos = [c for c in (getattr(self, "combo_edit_script", None),
                              getattr(self, "combo_browse_script", None))
                  if c is not None]
        if not combos:
            return
        subs: list[dict] = []
        if p:
            try:
                subs = list_sub_scripts(p, self._task_dir())
            except Exception as e:
                print(f"[browse] 扫描子脚本失败: {e}", file=sys.stderr)
        for combo in combos:
            combo.blockSignals(True)          # 重建期间别触发加载/刷新
            combo.clear()
            if p:
                combo.addItem(f"整个任务 —— {Path(p).name}", ("*", str(p)))
                for s in subs:
                    combo.addItem(f"{s['name']}  [{s['rel']}]",
                                  (s["rel"], s["path"]))
            combo.setCurrentIndex(0)
            combo.blockSignals(False)

    @staticmethod
    def _picker_selection(combo):
        """读"脚本"下拉框的选中项 → (rel 相对路径, 绝对路径); rel='*' 表示整个任务"""
        d = combo.currentData()
        if isinstance(d, (tuple, list)) and len(d) == 2:
            return str(d[0]), (str(d[1]) if d[1] else None)
        return "*", None

    def _load_file_into_editor(self, path):
        """把某个脚本文件读进编辑器, 并记下它是谁(保存 / 启动同步都按它)"""
        if getattr(self, "edit_script", None) is None or not path:
            return
        try:
            txt = Path(path).read_text(encoding="utf-8")
        except Exception as e:
            QMessageBox.warning(self, "打开脚本", f"读取失败:\n{path}\n{e}")
            return
        self._loading_edit = True
        try:
            self.edit_script.setPlainText(txt)
            self.edit_script_path = str(path)
            self._edit_dirty = False
        finally:
            self._loading_edit = False
        try:
            shown = str(Path(path).resolve().relative_to(
                self._task_dir().resolve())).replace("\\", "/")
        except Exception:
            shown = Path(path).name
        if getattr(self, "lbl_edit_path", None) is not None:
            self.lbl_edit_path.setText(f"正在编辑: {shown}")

    def _on_edit_script_changed(self, _idx=0):
        """「脚本」页切换目标文件(总脚本 <-> 子脚本)"""
        combo = getattr(self, "combo_edit_script", None)
        if combo is None or not combo.count():
            return
        _rel, path = self._picker_selection(combo)
        if path and str(path) != str(self.edit_script_path or ""):
            self._load_file_into_editor(path)

    def _on_edit_text_changed(self):
        """编辑器内容变化 → 记 dirty(程序化灌内容时不记)"""
        if not self._loading_edit:
            self._edit_dirty = True

    def _flush_editor(self):
        """启动任务前, 把编辑器里未保存的改动写回**它正在编辑的那个文件**

        2026-10-09: 「脚本」页现在能直接编辑子脚本, 不能再无条件写回任务文件
        (那会把子脚本内容覆盖进总脚本)。只写编辑器自己打开的那份。
        """
        tgt = self.edit_script_path
        if not tgt or not self._edit_dirty:
            return
        try:
            Path(tgt).write_text(self.edit_script.toPlainText(), encoding="utf-8")
            self._edit_dirty = False
            self.append_sys_log(f"[启动] 已同步未保存改动 → {Path(tgt).name}")
        except Exception as e:
            self.append_sys_log(f"[启动] 同步编辑器改动失败({Path(tgt).name}): {e}")

    # ================= 脚本步骤(从选中步骤开始跑) =================
    def _refresh_browse_steps(self):
        """把当前脚本的步骤铺到「脚本步骤」页

        顶部可选"整个任务 / 某个子脚本": 选子脚本时只列它那一段, 但**序号仍是
        整个任务里的全局步号** —— 这样"从选中步骤开始跑"传的 start_index 永远正确。
        """
        if getattr(self, "list_browse", None) is None:
            return
        p = self.combo_task.currentData() or self.current_task_path
        self.list_browse.clear()
        if not p:
            self.lbl_browse_hint.setText("未选择任务脚本")
            return
        try:
            task = load_task(p)
        except Exception as e:
            self.lbl_browse_hint.setText(f"脚本解析失败: {e}")
            return

        rel = "*"
        cbs = getattr(self, "combo_browse_script", None)
        if cbs is not None and cbs.count():
            rel, _path = self._picker_selection(cbs)

        rows = [(i, s) for i, s in enumerate(task.steps)
                if rel == "*" or s.source == rel]
        for i, s in rows:
            it = QListWidgetItem(f"{i + 1}. {s.name or s.describe()}")
            it.setData(Qt.UserRole, i)          # 全局步号(0基) = start_index
            it.setToolTip(f"整个任务里的第 {i + 1} 步 · 来自 {s.source or Path(p).name}\n"
                          f"{s.describe()}")
            self.list_browse.addItem(it)

        base = Path(p).name
        if rel == "*":
            self.lbl_browse_hint.setText(
                f"{base} · 整个任务共 {len(task.steps)} 步 —— 选中一步后点"
                f"「从选中步骤开始跑」(双击同效)")
        elif rows:
            lo, hi = rows[0][0] + 1, rows[-1][0] + 1
            self.lbl_browse_hint.setText(
                f"{base} · 子脚本 {rel} · 共 {len(rows)} 步"
                f"(在整个任务里位于第 {lo}~{hi} 步) —— 序号就是整个任务的步号, "
                f"从选中步骤开跑位置不会错")
        else:
            self.lbl_browse_hint.setText(f"{base} · 子脚本 {rel} 没有步骤")
        btn = getattr(self, "btn_browse_run", None)
        if btn is not None:
            btn.setEnabled(bool(rows))

    def _start_from_selected_step(self):
        """从「脚本步骤」页当前选中的步骤开始执行(前面的步骤跳过)"""
        it = self.list_browse.currentItem()
        if it is None:
            QMessageBox.information(self, "提示", "先在列表里选中一个步骤")
            return
        idx = it.data(Qt.UserRole)
        if idx is None:
            return
        idx = int(idx)
        try:
            total = len(load_task(self.combo_task.currentData()
                                  or self.current_task_path).steps)
        except Exception:
            total = 0
        tail = (f"\n\n注意: 整个任务的第 1~{idx} 步会被跳过, "
                f"第 {idx + 2}~{total} 步照常继续。"
                if total and idx > 0 else "")
        if QMessageBox.question(
                self, "从该步骤开始跑",
                f"将从整个任务的第 {idx + 1} 步开始执行:\n{it.text()}{tail}\n\n"
                f"请先确认游戏当前画面与该步骤匹配, 否则可能点空。\n继续?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._start_task(start_index=idx)

    def _open_task_file(self):
        """打开已有的任务脚本(外部拷进来的也行); 想新建请走「＋新建脚本…」"""
        fn, _ = QFileDialog.getOpenFileName(
            self, "打开任务脚本(新建请用「＋ 新建脚本…」)",
            str(self._task_dir()), "YAML (*.yaml *.yml)")
        if not fn:
            return
        if not Path(fn).is_file():
            QMessageBox.information(
                self, "文件不存在",
                f"{Path(fn).name} 不存在。\n\n要建新脚本请点「＋ 新建脚本…」"
                f"(会生成带注释的骨架, 还能直接挂进当前任务)。")
            return
        self.combo_task.addItem(Path(fn).name, fn)
        self.combo_task.setCurrentIndex(self.combo_task.count() - 1)

    # ================= 新建脚本(2026-10-09 v1.9.0) =================
    def _new_task_file(self):
        """新建脚本向导: 顶层任务 / parts 子脚本 + 骨架模板 + 按钮库 + 自动挂载

        旧版只有一个「打开/新建脚本…」按钮, 走的是**打开**文件对话框 ——
        敲个新名字会被当成"打开不存在的文件", 编辑器读不到内容直接报错,
        用户因此找不到新建入口。现在拆成独立的向导流程。
        """
        cur = self.combo_task.currentData() or self.current_task_path
        try:
            ui = UiRegistry()
        except Exception:
            ui = None
        dlg = NewScriptDialog(self._task_dir(), ui, self,
                             current_task=str(cur) if cur else None,
                             copy_text=self.edit_script.toPlainText())
        if dlg.exec() != QDialog.Accepted:
            return
        path = dlg.target_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(dlg.content(), encoding="utf-8")
        except Exception as e:
            QMessageBox.warning(self, "创建失败", f"{path}\n{e}")
            return
        rel = dlg.rel()
        self.append_sys_log(f"[新建] 已创建 {rel}")
        self.logger.info(f"已创建脚本: {path}")

        # 子脚本 → 可选把 include 追加进当前任务(否则两页「脚本」下拉看不到它)
        if dlg.kind() == KIND_PART and dlg.mount_requested() and cur:
            try:
                added = insert_include_into_task(cur, rel, dlg.edit_name.text().strip())
                self.append_sys_log(
                    f"[新建] 已{'' if added else '(本就存在, 跳过)'}把 include: {rel} "
                    f"挂进 {Path(cur).name}")
            except Exception as e:
                self.append_sys_log(f"[新建] 挂载失败: {e}")
            self._rebuild_script_pickers()

        if dlg.kind() == KIND_PART:
            if not self._select_script_in_picker(rel):
                # 没挂进当前任务 → 两页下拉里还没有它, 直接读进编辑器接着写
                self._load_file_into_editor(path)
                self.append_sys_log(
                    f"[新建] {rel} 未挂进当前任务, 已直接在编辑器打开"
                    f"(挂载后才会出现在「脚本」下拉里)")
        else:
            self._refresh_task_list()               # 顶层任务: 进任务下拉
            i = self.combo_task.findText(Path(path).name)
            if i >= 0:
                self.combo_task.setCurrentIndex(i)

    def _select_script_in_picker(self, rel: str) -> bool:
        """把「脚本」/「脚本步骤」两页的下拉切到某个子脚本(找不到返回 False)"""
        ok = False
        for combo in (getattr(self, "combo_edit_script", None),
                      getattr(self, "combo_browse_script", None)):
            if combo is None:
                continue
            for i in range(combo.count()):
                d = combo.itemData(i)
                if isinstance(d, (tuple, list)) and str(d[0]) == rel:
                    combo.setCurrentIndex(i)
                    ok = True
                    break
        return ok

    def _insert_button_snippet(self):
        """「插入按钮/界面…」: 从按钮库挑界面/按钮 → 生成片段插到光标处"""
        try:
            ui = UiRegistry()
        except Exception as e:
            QMessageBox.warning(self, "按钮库不可用", str(e))
            return
        tgt = self.edit_script_path or self.current_task_path or "(未选择)"
        dlg = ButtonLibraryDialog(ui, self, target=str(tgt))
        if dlg.exec() != QDialog.Accepted:
            return
        text = dlg.snippet()
        if text.strip():
            self._insert_into_editor(text)

    def _insert_into_editor(self, snippet: str):
        """把片段插到脚本编辑器光标处(自动补换行, 连续插入不会粘在一起)"""
        ed = self.edit_script
        cur = ed.textCursor()
        txt = snippet if snippet.endswith("\n") else snippet + "\n"
        if cur.position() > 0 and not ed.toPlainText()[: cur.position()].endswith("\n"):
            txt = "\n" + txt
        cur.insertText(txt)          # 会触发 textChanged → 自动标 dirty
        ed.setTextCursor(cur)
        ed.setFocus()

    def _save_task(self):
        """Ctrl+S: 保存**编辑器当前打开的那份文件**(总脚本或某个子脚本)"""
        target = self.edit_script_path or self.current_task_path
        if not target:
            target = str(self._task_dir() / "new_task.yaml")
            self.edit_script_path = target
        try:
            # 校验 YAML 可解析
            txt = self.edit_script.toPlainText()
            import yaml
            data = yaml.safe_load(txt)
            assert isinstance(data, dict) and "steps" in data, "缺少 steps 字段"
            Path(target).write_text(txt, encoding="utf-8")
            self._edit_dirty = False
            self.logger.info(f"已保存脚本: {target}")
            self.append_sys_log(f"[保存] {target}")
            if Path(target).parent.resolve() == self._task_dir().resolve():
                # 顶层任务文件: 刷新任务下拉(可能是个新文件)
                self._refresh_task_list()
            elif getattr(self, "list_browse", None) is not None:
                # 子脚本: 步骤可能变了, 刷新「脚本步骤」页(保留下拉选择)
                self._refresh_browse_steps()
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"脚本格式有误:\n{e}")

    # ================= 模板 =================
    def _build_template_bar(self) -> QWidget:
        """识别模板库工具条 —— 挂在「实时画面」页底部

        2026-10-09 v2.0.0 从左侧面板搬过来。采集模板的完整动作是两步:
          ① 在上面的实时画面点一下取坐标  → ② 点「从点击点采集模板」
        (以点击点为中心裁 80x80 存进 assets/templates/)。
        两步都发生在这个页面上, 放同一页才叫闭环; 左侧留给运行控制。
        """
        box = QGroupBox("识别模板库")
        self.box_templates = box          # 标题上会带上模板数量
        h = QHBoxLayout(box)
        h.setContentsMargins(8, 2, 8, 2)
        h.setSpacing(6)

        h.addWidget(QLabel("已有模板:"))
        self.combo_templates = QComboBox()
        self.combo_templates.setMinimumWidth(200)
        self.combo_templates.setToolTip(
            "assets/templates/ 里的模板文件(点开可看全部)。\n"
            "脚本里用 wait:{template: 名字} 引用, 或给注册表按钮配 template: 名字。\n"
            "这一栏只用于查看/核对, 选谁都不影响运行。")
        h.addWidget(self.combo_templates)

        self.btn_new_tpl = QPushButton("从点击点采集模板")
        self.btn_new_tpl.setToolTip(
            "在上方画面点一下取坐标 → 以该点为中心裁 80x80 存成新模板。\n"
            "多开时用的是当前所选账号自己的画面, 不会张冠李戴。\n"
            "采完可直接去「按钮库(注册表)」页把它登记到某个按钮上。")
        self.btn_new_tpl.clicked.connect(self._collect_template)
        h.addWidget(self.btn_new_tpl)

        self.lbl_hint = QLabel("先在上面的画面点一下取坐标, 再点采集(中心裁 80x80)")
        self.lbl_hint.setStyleSheet("color:#888; font-size:11px;")
        self.lbl_hint.setWordWrap(True)
        h.addWidget(self.lbl_hint, stretch=1)
        return box

    def _tpl_dir(self):
        return PROJECT_ROOT / "assets" / "templates"

    def _refresh_template_list(self):
        # 2026-10-09 v2.0.0: 列表控件从左侧的 QListWidget 换成「实时画面」页底部的
        # QComboBox(省高度); 两个控件都用 getattr 兜着, 谁没建好都不会把启动搞崩。
        names = [p.stem for p in sorted(self._tpl_dir().glob("*.png"))]
        combo = getattr(self, "combo_templates", None)
        if combo is not None:
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(names)
            combo.setCurrentIndex(-1)      # 不预设选中(这一栏只是查看)
            combo.blockSignals(False)
        box = getattr(self, "box_templates", None)
        if box is not None:
            box.setTitle(f"识别模板库 ({len(names)} 个)")
        if getattr(self, "matcher", None) is not None:
            self.matcher.reload()

    def _collect_template(self):
        if not self.last_click_xy:
            QMessageBox.information(self, "提示", "先在右侧实时画面点一下目标位置")
            return
        x, y = self.last_click_xy
        port = self._click_port if self._click_port is not None else self._local_port
        ch = self.store.get(port)
        img = ch.frame if ch else None
        if img is None and int(port) == self._local_port:
            self._manual_screenshot()          # 本机可以临时补一张
            ch = self.store.get(port)
            img = ch.frame if ch else None
        if img is None:
            who = ch.label if ch else f"端口{port}"
            QMessageBox.warning(
                self, "失败",
                f"「{who}」还没有可用画面。\n"
                f"多开账号的画面由该账号的任务进程实时回传, 任务跑起来之后才有图。")
            return
        name, ok = _input_dialog(self, "模板名", "模板名(英文/数字):", f"tpl_{x}_{y}")
        if not ok or not name:
            return
        # 以点击点为中心裁 80x80 —— 用该账号自己的画面, 避免多开时张冠李戴
        hw = 40
        x1, y1 = max(0, x - hw), max(0, y - hw)
        x2, y2 = min(img.shape[1], x + hw), min(img.shape[0], y + hw)
        crop = img[y1:y2, x1:x2]
        out = self._tpl_dir() / f"{name}.png"
        cv2.imwrite(str(out), crop)
        self.logger.info(f"新模板已保存: {out} ({crop.shape[1]}x{crop.shape[0]})")
        self.append_sys_log(f"[模板] {out.name} 尺寸={crop.shape[1]}x{crop.shape[0]}"
                            f" 来源={(ch.label if ch else port)}")
        self._refresh_template_list()

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
    def _manual_screenshot(self):
        """截「当前账号」一张。

        本机走常驻 device; 多开端口临时建 ADB 连接 —— 截图是只读操作,
        不影响该账号正在跑的任务, 也不与它的实时回传冲突(只是补一帧)。
        """
        port = self._target_port()
        try:
            from common import png_bytes_to_bgr
            if port == self._local_port:
                if not self.device:
                    QMessageBox.information(self, "提示", "设备未连接")
                    return
                png = self.device.screenshot()
            else:
                d = self.cfg["device"]
                tmp = AdbDevice(d["adb_path"], d["host"], port)
                png = tmp.screenshot()
            self.store.set_frame(port, png_bytes_to_bgr(png))
            who = self._acc_name(port) or f"端口{port}"
            self.append_sys_log(f"[截图] {who} 手动截图完成")
            self.logger.info(f"手动截图完成: {who}")
        except Exception as e:
            self.logger.error(f"截图失败: {e}")
            QMessageBox.warning(self, "截图失败", str(e))

    def _win_diag(self):
        """窗口诊断: 列出顶层窗口并标出 MuMu 候选, 供换机器后修 win_input 配置"""
        if sys.platform != "win32":
            QMessageBox.information(self, "提示", "Windows 鼠标输入通道仅在 Windows 上可用")
            return
        try:
            from device.win_input import diagnose_text, resolve_window
            tbl = diagnose_text()
            wi = self.cfg.get("win_input") or {}
            try:
                hwnd, info = resolve_window(wi)
                head = (f"[OK] 已找到 MuMu 窗口: hwnd=0x{hwnd:X}\n"
                        f"     标题「{info.get('title')}」进程={info.get('exe_name')}\n"
                        f"     位置={info.get('rect')}\n"
                        f"[可粘贴] config.yaml → win_input.hwnd: {hwnd if wi.get('hwnd') else 0}"
                        f"   (当前为自动查找)\n\n")
            except Exception as e:
                head = f"[!!] 未找到 MuMu 窗口\n{e}\n\n"
            body = head + tbl
        except Exception as e:
            body = f"诊断失败: {e}"
        dlg = QDialog(self)
        dlg.setWindowTitle("窗口诊断 —— Windows 鼠标输入通道 (via: win)")
        dlg.resize(880, 560)
        v = QVBoxLayout(dlg)
        tip = QLabel(
            "任务里 <b>via: win</b> 的步骤靠 Windows 鼠标点击绕开反注入, 需要找到 MuMu 窗口。\n"
            "报「未找到 MuMu 窗口」时: 在下面清单里找到标着 MuMu候选 的那行 —— "
            "把它的 hwnd 填进 config.yaml 的 <b>win_input.hwnd</b>，"
            "或把标题里的特征词填进 <b>win_input.title_contains</b>。")
        tip.setWordWrap(True)
        v.addWidget(tip)
        te = QPlainTextEdit(body)
        te.setReadOnly(True)
        te.setFont(self._mono_font())
        v.addWidget(te, stretch=1)
        bb = QDialogButtonBox(QDialogButtonBox.Close)
        btn_copy = bb.addButton("复制全部", QDialogButtonBox.ActionRole)

        def _copy():
            QApplication.clipboard().setText(body)
            self.append_sys_log("[诊断] 窗口清单已复制到剪贴板")
        btn_copy.clicked.connect(_copy)
        bb.rejected.connect(dlg.reject)
        v.addWidget(bb)
        self.append_sys_log("[诊断] 已生成窗口清单(见弹窗)")
        dlg.exec()

    def _show_screen(self, bgr):
        """本机设备的新画面 → 本机通道"""
        if bgr is None:
            return
        self.last_screen_bgr = bgr
        self.store.set_frame(self._local_port, bgr)

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
    def _account_combo(self, cur_port=None) -> QComboBox:
        """账号下拉: 本机(0) + 多开账号(按端口)"""
        cb = QComboBox()
        cb.addItem("本机(当前GUI设备)", 0)
        for acc in _load_accounts():
            cb.addItem(f"{acc['name']} (端口{acc['port']})", acc["port"])
        if cur_port:
            i = cb.findData(int(cur_port))
            if i > 0:
                cb.setCurrentIndex(i)
        cb.currentIndexChanged.connect(lambda _: self._save_timer_state())
        cb.setToolTip("本机=用主窗口跑; 选某账号=用子进程跑该模拟器端口(多开)")
        return cb

    def _append_timer_row(self, time_str, task_name, acc_port=None, enabled=True):
        """表格追加一行: 启用勾选 | 时间 | 脚本名 | 账号(0=本机)"""
        r = self.tbl_timer.rowCount()
        self.tbl_timer.insertRow(r)
        ck = QCheckBox()
        ck.setChecked(bool(enabled))
        ck.setToolTip("勾选=启用这条定时")
        ck.setStyleSheet("margin-left:13px;")
        ck.stateChanged.connect(lambda _: self._save_timer_state())
        self.tbl_timer.setCellWidget(r, 0, ck)
        ti = QTableWidgetItem(str(time_str))
        ti.setTextAlignment(Qt.AlignCenter)
        ti.setToolTip("双击修改时间(HH:mm)")
        self.tbl_timer.setItem(r, 1, ti)
        self.tbl_timer.setItem(r, 2, QTableWidgetItem(str(task_name)))
        self.tbl_timer.setCellWidget(r, 3, self._account_combo(acc_port))

    def _add_timer_dialog(self):
        """弹出对话框: 时间 + 脚本 + 账号 下拉 → 加一行"""
        d = QDialog(self)
        d.setWindowTitle("添加定时任务")
        lay = QFormLayout(d)
        tm = QTimeEdit(QTime(0, 10))
        tm.setDisplayFormat("HH:mm")
        cb = QComboBox()
        for p in sorted(self._task_dir().glob("*.yaml")):
            cb.addItem(p.name, str(p))
        if cb.count() == 0:
            QMessageBox.warning(self, "提示", "任务目录没有可用的 yaml 脚本")
            return
        cur = self.combo_task.currentData()
        if cur:
            i = cb.findData(cur)
            if i >= 0:
                cb.setCurrentIndex(i)
        lay.addRow("时间(每天)", tm)
        lay.addRow("脚本", cb)
        lay.addRow("账号", self._account_combo())
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        lay.addRow(bb)
        if d.exec() == QDialog.Accepted:
            self._append_timer_row(tm.time().toString("HH:mm"), cb.currentText())
            self._save_timer_state()

    def _del_timer_rows(self):
        rows = sorted({i.row() for i in self.tbl_timer.selectedItems()}, reverse=True)
        if not rows:
            QMessageBox.information(self, "提示", "先在表格里选中要删除的行")
            return
        for r in rows:
            self.tbl_timer.removeRow(r)
        self._save_timer_state()

    def _restore_timer(self):
        """启动时读回上次的定时列表(gui_timer.json), 重启后不丢失"""
        try:
            st = json.loads(Path(self._timer_cfg_path).read_text(encoding="utf-8"))
            for e in st.get("entries", []):
                self._append_timer_row(
                    str(e.get("time", "00:10")),
                    str(e.get("task", "")),
                    e.get("acc_port"),
                    bool(e.get("enabled", True)))
        except Exception:
            pass

    def _save_timer_state(self):
        try:
            entries = []
            for r in range(self.tbl_timer.rowCount()):
                ck = self.tbl_timer.cellWidget(r, 0)
                it_t = self.tbl_timer.item(r, 1)
                it_s = self.tbl_timer.item(r, 2)
                if not it_s or not it_s.text():
                    continue
                cb_acc = self.tbl_timer.cellWidget(r, 3)
                entries.append({
                    "enabled": bool(ck and ck.isChecked()),
                    "time": it_t.text() if it_t else "00:10",
                    "task": it_s.text(),
                    "acc_port": int(cb_acc.currentData() or 0) if cb_acc else 0,
                })
            Path(self._timer_cfg_path).write_text(json.dumps(
                {"entries": entries}, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _tick_timer(self):
        """每5秒由 QTimer 调用: 遍历定时列表, 到点且今天未跑过则自动启动对应脚本/账号"""
        now = QTime.currentTime()
        now_ms = now.msecsSinceStartOfDay()
        today = datetime.date.today().isoformat()
        n_enabled = 0
        for r in range(self.tbl_timer.rowCount()):
            ck = self.tbl_timer.cellWidget(r, 0)
            if not (ck and ck.isChecked()):
                continue
            n_enabled += 1
            it_t = self.tbl_timer.item(r, 1)
            it_s = self.tbl_timer.item(r, 2)
            tm = QTime.fromString(it_t.text() if it_t else "", "HH:mm")
            if not tm.isValid():
                continue
            task_name = it_s.text() if it_s else ""
            if not task_name:
                continue
            cb_acc = self.tbl_timer.cellWidget(r, 3)
            acc_port = int(cb_acc.currentData() or 0) if cb_acc else 0
            t_ms = tm.msecsSinceStartOfDay()
            if now_ms < t_ms:          # 还没到点
                continue
            key = f"{tm.toString('HH:mm')}|{task_name}|{acc_port}"
            if self._timer_runs.get(key) == today:
                continue
            if now_ms - t_ms > 3 * 60 * 1000:
                # 超过到点时间3分钟仍未启动(如GUI刚开/错过), 今天不补跑
                self._timer_runs[key] = today
                continue
            if acc_port:
                # ---- 多开账号: 用子进程跑对应模拟器端口 ----
                res = self.panel_multi.launch(acc_port, task_name, "")
                if res is True:
                    self._timer_runs[key] = today
                    self.append_sys_log(
                        f"[定时] {now.toString('HH:mm')} 到点, 账号端口{acc_port} "
                        f"自动启动「{task_name}」(子进程)")
                elif res is False:
                    self._timer_runs[key] = today
                    self.append_sys_log(
                        f"[定时] 端口{acc_port} 已有任务在跑, 今日不重复启动")
                else:   # "busy"
                    self.append_sys_log(
                        f"[定时] 端口{acc_port}=主GUI设备且主任务在跑, 跳过本次")
                continue
            # ---- 本机: 用主窗口 worker 跑 ----
            if self.worker and self.worker.isRunning():
                self.lbl_timer_state.setText(
                    f"定时: {tm.toString('HH:mm')}「{task_name}」已到点, 等当前任务结束…")
                return
            self._timer_runs[key] = today
            self.append_sys_log(
                f"[定时] {now.toString('HH:mm')} 到点, 自动启动「{task_name}」")
            self._auto_start_task(task_name)
        self.lbl_timer_state.setText(f"定时: {n_enabled} 条待命 (每天到点自动执行)")

    def _auto_start_task(self, task_name):
        """定时触发: 按脚本文件名定位并启动(不覆盖编辑器未保存内容)"""
        p = next((c for c in sorted(self._task_dir().glob("*.yaml"))
                  if c.name == task_name), None)
        if p is None:
            self.append_sys_log(f"[定时] 找不到任务脚本 {task_name}, 已跳过")
            return
        self.current_task_path = str(p)
        self._start_task(auto=True, path=str(p))

    # ================= 任务控制 =================
    def _start_task(self, auto=False, path=None, start_index=0):
        """启动任务; auto=True 为定时触发: 弹窗改日志、不写回编辑器, 避免无人值守卡住。

        start_index>0: 从脚本展开后的第 N 步(0基)开始执行 —— 供「脚本步骤」页
        的"从选中步骤开始跑"使用(跳过前面步骤, 直接补跑某一段)。

        2026-09-28: 按「当前账号」路由 —— 本机走 GUI 内 TaskWorker,
        多开账号交给 run_one.py 子进程(复用多开面板的启动/状态跟踪)。
        """
        start_index = int(start_index or 0)
        port = self._target_port()
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
                self.lbl_run_state.setText("定时启动失败: 设备未连接")
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
        self.lbl_run_state.setText("运行中…")

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
        self.worker.log.connect(self._on_log)
        self.worker.screenshot.connect(self._on_shot)
        self.worker.step_start.connect(self._on_step_start)
        self.worker.step_end.connect(self._on_step_end)
        self.worker.task_finished.connect(self._on_finished)
        self.worker.start()
        # 2026-09-29 双保险: 前面 _select_step_account 的信号联动曾把停止按钮
        # 置灰(worker 当时还是 None/旧线程), 这里在 worker 真正启动后再点亮一次
        self.btn_stop.setEnabled(True)

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
            self.lbl_run_state.setText(f"运行中: {name} · {task_name}")
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
                self.lbl_run_state.setText("停止中…")
            return
        if self.worker and self.worker.isRunning():
            # 先回显"已收到", 避免点了没反应(引擎真正退出要等当前等待轮结束)
            self._stop_t0 = time.time()
            self.append_sys_log("⏹ 已发送停止请求，正在等当前步骤安全退出…")
            self.lbl_run_state.setText("停止中… 已等待 0s")
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

    def _on_finished(self, ok, info):
        stopped = self._stop_t0 is not None
        self._stop_t0 = None
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.lbl_run_state.setText("完成" if ok else f"失败: {info}")
        if ok:
            self.store.finish(self._local_port, True, "成功")
        elif stopped:
            self.store.mark_stopped(self._local_port)
        else:
            self.store.finish(self._local_port, False, info)
        self._steps_dirty = True
        self.append_sys_log("=" * 40)
        self.append_sys_log("任务" + ("成功 ✔" if ok else f"未完成: {info}"))
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
        self.store.set_active(int(port), False)
        self._page_sig = None
        self._update_run_buttons()      # 当前账号的子进程结束了, 停止按钮要复位

    def _discover_ports(self) -> list[int]:
        """扫描 logs/live/port_*/: 让"GUI 重启后仍在跑"的子进程也被认回来"""
        out: list[int] = []
        try:
            if not LIVE_ROOT.exists():
                return out
            for d in LIVE_ROOT.iterdir():
                if d.is_dir() and d.name.startswith("port_"):
                    try:
                        out.append(int(d.name[5:]))
                    except ValueError:
                        continue
        except OSError:
            pass
        return out

    def _poll_live(self):
        """把多开子进程的 画面/步骤/日志 增量捞进通道(不阻塞界面)"""
        self._scan_tick += 1
        if self._scan_tick % 5 == 1:        # 目录扫描降频到 ~3 秒一次
            for port in self._discover_ports():
                if port != self._local_port and port not in self._readers:
                    self._readers[port] = LiveReader(port)
                    self._page_sig = None

        for port, rd in list(self._readers.items()):
            events = rd.poll()
            if rd.new_run:
                # 新一次运行 → 丢掉上一轮进度, 名称/脚本以 meta.json 为准
                self._log_drawn[port] = 0
                self.store.reset(port,
                                 name=str(rd.meta.get("name") or "") or self._acc_name(port),
                                 source="child",
                                 task=str(rd.meta.get("task") or ""))
                self._steps_sig = None
                self._page_sig = None
            if events:
                self.store.apply_events(port, events)
                self._steps_dirty = True
                self._page_sig = None
            if not rd.is_active():
                ch = self.store.get(port)
                if ch is not None and ch.state == "running":
                    # 子进程被强杀/崩溃时不会回写 task_end, 别让界面永远停在"运行中"
                    self.store.mark_stopped(port, "子进程已退出(未见结束事件)")
                    self._page_sig = None
                    self._steps_dirty = True
                self.store.set_active(port, False)
            img = rd.read_frame()
            if img is not None:
                self.store.set_frame(port, img)

    # ================= 渲染 =================
    def append_sys_log(self, text: str):
        """系统级消息(多开/定时/保存…): 与账号日志合并后按时间顺序渲染"""
        self._sys_logs.append((time.time(), str(text)))

    def _render_tick(self):
        self._render_logs()
        self._render_screen_page()
        self._render_steps()

    def _render_all(self, force: bool = False):
        if force:
            self._page_sig = None
            self._steps_sig = None
        self._render_logs()
        self._render_screen_page()
        self._render_steps()

    def _render_screen_page(self):
        chans = self.store.channels()
        sig = tuple((c.port, c.label, c.state, c.task, c.info) for c in chans)
        if sig != self._page_sig:
            self._page_sig = sig
            self.page_screen.refresh(chans)
        self._sync_step_account_combo(chans)
        for c in chans:            # 新帧才喂给画面页(避免每 250ms 重建一遍 QPixmap)
            if c.frame_seq > self._frame_drawn.get(c.port, 0):
                self._frame_drawn[c.port] = c.frame_seq
                if c.frame is not None:
                    self.page_screen.set_frame(c.port, c.frame)

    def _sync_step_account_combo(self, chans=None):
        """步骤页账号筛选下拉: 全部 + 每个通道(保留当前选择)"""
        chans = chans if chans is not None else self.store.channels()
        want = [(0, "全部账号")] + [(c.port, f"{c.label} · {c.port}") for c in chans]
        have = [self.combo_step_acc.itemData(i)
                for i in range(self.combo_step_acc.count())]
        if [d for d, _ in want] == have:
            for i, (_d, txt) in enumerate(want):
                if self.combo_step_acc.itemText(i) != txt:
                    self.combo_step_acc.setItemText(i, txt)
            return
        cur = self.combo_step_acc.currentData()
        self.combo_step_acc.blockSignals(True)
        self.combo_step_acc.clear()
        for d, txt in want:
            self.combo_step_acc.addItem(txt, d)
        if cur is not None:
            i = self.combo_step_acc.findData(cur)
            if i >= 0:
                self.combo_step_acc.setCurrentIndex(i)
        self.combo_step_acc.blockSignals(False)
        self._steps_sig = None
        self._steps_dirty = True

    def _on_step_filter_changed(self, _i):
        # 用户在步骤页选了具体账号 → 全局跟随(选"全部账号"不动全局)
        if not self._syncing_target:
            d = self.combo_step_acc.currentData()
            if d and int(d) != 0:
                self._set_current_port(int(d))
                return
        self._steps_sig = None
        self._steps_dirty = True
        self._render_steps()

    def _select_step_account(self, port: int):
        """程序内部同步步骤页账号下拉(不改全局状态)。

        2026-09-29 修复: 原来直接 setCurrentIndex 会触发 currentIndexChanged →
        _on_step_filter_changed → _set_current_port → _update_run_buttons,
        而启动任务时(woker 尚未创建/还是旧线程)会把刚启用的「停止」按钮
        重新置灰 —— 表现为任务在跑但停止按钮整场灰色点不了。此方法是纯
        UI 同步, 屏蔽信号, 不应反向驱动全局状态。
        """
        i = self.combo_step_acc.findData(int(port))
        if i >= 0:
            self.combo_step_acc.blockSignals(True)
            self.combo_step_acc.setCurrentIndex(i)
            self.combo_step_acc.blockSignals(False)

    def show_account_screen(self, port: int):
        """从「多开管理」跳到某账号的实时画面(并全局同步)"""
        port = int(port)
        self._set_current_port(port)
        self.tabs.setCurrentWidget(self.page_screen)

    def _render_steps(self):
        if not self._steps_dirty:
            return
        self._steps_dirty = False
        sel = self.combo_step_acc.currentData()
        chans = self.store.channels()
        if sel:
            chans = [c for c in chans if c.port == int(sel)]
        multi = len(chans) > 1
        sig = (multi, tuple((c.port, len(c.order)) for c in chans))
        if sig != self._steps_sig:
            self._steps_sig = sig
            self._rebuild_step_items(chans, multi)
        else:
            self._update_step_items(chans, multi)
        if not chans:
            self.lbl_step_hint.setText("暂无步骤 —— 启动任务后这里会按账号分别显示进度")
        else:
            self.lbl_step_hint.setText("  |  ".join(
                f"{c.label}: {c.state_text()}" for c in chans))

    def _rebuild_step_items(self, chans, multi: bool):
        sb = self.list_steps.verticalScrollBar()
        pos = sb.value()
        self.list_steps.clear()
        self._steps_items.clear()
        for c in chans:
            if multi:
                # 分组标题: 多账号并行时, 一眼看清每段属于谁
                head = QListWidgetItem(
                    f"── {c.label} · 端口{c.port} · {c.state_text()} ──")
                head.setFlags(Qt.NoItemFlags)
                head.setBackground(QColor("#eef2f7"))
                head.setForeground(QColor("#456"))
                self.list_steps.addItem(head)
            for idx in c.order:
                sr = c.steps.get(idx)
                if sr is None:
                    continue
                it = QListWidgetItem(self._step_text(c, sr, multi))
                it.setData(Qt.UserRole, (c.port, idx))
                self._step_style(it, sr)
                self.list_steps.addItem(it)
                self._steps_items[(c.port, idx)] = it
        sb.setValue(min(pos, sb.maximum()))
        self._scroll_to_item()

    def _update_step_items(self, chans, multi: bool):
        for c in chans:
            for idx in c.order:
                it = self._steps_items.get((c.port, idx))
                sr = c.steps.get(idx)
                if it is None or sr is None:
                    continue
                txt = self._step_text(c, sr, multi)
                if it.text() != txt:
                    it.setText(txt)
                self._step_style(it, sr)
        self._scroll_to_item()

    def _step_text(self, ch: Channel, sr: StepRec, multi: bool) -> str:
        who = f"[{ch.label}] " if multi else ""
        base = f"{sr.idx + 1}. {sr.desc or sr.name}"
        hm = lambda t: time.strftime("%H:%M:%S", time.localtime(t)) if t else ""
        if sr.state == "running":
            return f"{who}[{hm(sr.t0)}]  {base}"
        if sr.state in ("ok", "fail"):
            cost = sr.cost
            cs = f"  {cost:.1f}s" if cost is not None else ""
            mark = "✔" if sr.state == "ok" else "✘"
            return f"{who}[{hm(sr.t0)}]  {base}  {mark}{cs} [{hm(sr.t1)}]"
        return f"{who}{base}"

    @staticmethod
    def _step_style(it: QListWidgetItem, sr: StepRec):
        if sr.state == "fail":
            it.setForeground(QColor("#c00"))
            it.setBackground(QColor(255, 235, 235))
        elif sr.state == "running":
            it.setForeground(QColor("#06c"))
            it.setBackground(QColor(238, 244, 255))
        elif sr.state == "ok":
            it.setForeground(QColor("#000"))
            it.setBackground(QColor(255, 255, 255))
        else:
            it.setForeground(QColor("#999"))
            it.setBackground(QColor(255, 255, 255))

    def _scroll_to_item(self):
        if not self._scroll_to:
            return
        it = self._steps_items.get(self._scroll_to)
        self._scroll_to = None
        if it is not None:
            self.list_steps.scrollToItem(it)
            it.setSelected(True)

    def _render_logs(self):
        """把各通道新增日志 + 系统消息按时间顺序写进日志框"""
        items: list[tuple[float, str]] = []
        multi = len(self.store.channels()) > 1
        for c in self.store.channels():
            drawn = self._log_drawn.get(c.port, 0)
            offset = c.log_total - len(c.logs)      # 头部被裁剪掉的条数
            start = max(drawn - offset, 0)
            for (t, lv, msg) in c.logs[start:]:
                items.append((t, self._log_html(c.label if multi else "", t, lv, msg)))
            self._log_drawn[c.port] = c.log_total
        if self._sys_logs:
            for (t, txt) in self._sys_logs:
                items.append((t, self._log_html("", t, "sys", txt)))
            self._sys_logs.clear()
        if not items:
            return
        items.sort(key=lambda x: x[0])
        for _, h in items:
            self.txt_log.appendHtml(h)

    @staticmethod
    def _log_html(who: str, t: float, level: str, msg: str) -> str:
        color = {"info": "#000", "debug": "#666", "warning": "#c70",
                 "error": "#c00", "sys": "#456"}.get(level, "#000")
        # 2026-09-20: 时间戳与 logs/run.log 对齐, 排查时可直接对时间
        ts = time.strftime("%H:%M:%S", time.localtime(t))
        head = (f'<span style="color:#0a7; font-weight:bold">'
                f'[{html.escape(who)}]</span> ' if who else "")
        return (f'<span style="color:#888">[{ts}]</span> {head}'
                f'<span style="color:{color}">{html.escape(msg)}</span>')


def _input_dialog(parent, title, label, default=""):
    """简化输入框"""
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout
    d = QDialog(parent)
    d.setWindowTitle(title)
    lay = QFormLayout(d)
    ed = QLineEdit(default)
    lay.addRow(label, ed)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.accepted.connect(d.accept)
    bb.rejected.connect(d.reject)
    lay.addRow(bb)
    if d.exec() == QDialog.Accepted:
        return ed.text(), True
    return "", False


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
    _mutex = None
    try:
        cfg = load_config()
        if (cfg.get("app", {}) or {}).get("single_instance", True):
            _mutex = single_instance_mutex("xyfz_auto_gui_single")
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
