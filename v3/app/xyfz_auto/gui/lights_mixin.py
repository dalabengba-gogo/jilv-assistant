# -*- coding: utf-8 -*-
"""左侧账号状态灯 + 每端口任务状态那一列

拆自 main_window.py(2026-10-18 v4.7.0 批次四·工程 P2-1)。

依赖(由宿主 MainWindow 提供):
  · self._port_lights / self._port_state_labels / self._lights_box
  · self.store(ChannelStore, 只读) / self._acc_name
"""
from __future__ import annotations
import time
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget
from accounts import load_accounts as _load_accounts
from gui.ui_helpers import _PortLight, _safe_call, _tail_int

# 2026-10-19 fix: 任务结论(完成/失败/已停止/设备错误)在状态灯右侧先展示几秒,
# 让用户看到结果, 然后自动归位到「空闲」——否则账号状态会永远停在上一轮结论,
# 看着像卡在那。设备离线时不受此宽限限制, 立即归位空闲。
PORT_STATE_REVERT_SECONDS = 10.0
class StatusLightsMixin:
    """见模块 docstring。方法体整体搬自 main_window.py, 未作改写。"""
    def _rebuild_status_lights(self):
        """按「多开管理」(accounts.json)的端口重建状态灯 —— 端口/名称变动即同步。

        在初始化与本机「多开管理」保存后都会调用, 因此状态灯永远跟着多开管理的
        端口走, 不再只盯 config 里那一个固定端口。
        """
        if not hasattr(self, "_status_lights_layout"):
            return
        # 清掉旧灯(布局 + dict 两处)
        self._port_lights = {}
        self._port_state_labels = {}
        while self._status_lights_layout.count():
            item = self._status_lights_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        try:
            accounts = _load_accounts()
        except Exception:
            accounts = []
        for acc in accounts:
            try:
                port = int(acc.get("port", 0))
            except (TypeError, ValueError):
                continue
            if port <= 0:
                continue
            name = str(acc.get("name") or f"端口{port}")
            light = _PortLight(port, name)
            light.set_state(None, "探测中…")
            light.clicked.connect(
                lambda p=port: _safe_call(self._start_probe, p))
            # 2026-10-17 v3.10.0: 一行 = [状态灯] + [该账号的任务状态]
            row = QWidget()
            rh = QHBoxLayout(row)
            rh.setContentsMargins(0, 0, 0, 0)
            rh.setSpacing(4)
            rh.addWidget(light)
            st = QLabel("")
            st.setAlignment(Qt.AlignmentFlag.AlignRight
                            | Qt.AlignmentFlag.AlignVCenter)
            st.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
            # 保底宽度: 账号名再长也别把状态挤没了(宁可灯那侧被压窄)
            st.setMinimumWidth(56)
            rh.addWidget(st, stretch=1)
            self._status_lights_layout.addWidget(row)
            self._port_lights[port] = light
            self._port_state_labels[port] = st
        self._refresh_port_states()
        # 端口集合/名称变了, 不等下一轮 10s, 立即探测一次(延后到当前调用栈结束后)
        if self._port_lights:
            QTimer.singleShot(50, lambda: _safe_call(self._start_probe))
    def _refresh_port_states(self):
        """把每个账号的任务状态刷到它那盏灯右边(由 _render_tick 每 250ms 调)。

        状态**以通道为准**(ChannelStore), 本机任务与多开子进程走同一套:
        多开账号由 run_one 写回的 task_end 事件驱动, 主界面不用额外记账。
        只有"停止中"这类瞬时态才由 _set_port_state() 临时接管。
        """
        for port, lbl in (getattr(self, "_port_state_labels", None) or {}).items():
            txt, color, tip = self._port_state_of(port)
            if lbl.text() != txt:
                lbl.setText(txt)
            css = f"color:{color}; font-size:11px;"
            if lbl.styleSheet() != css:
                lbl.setStyleSheet(css)
            # 2026-10-19 v4.9.0(P1-2): tooltip 也要先比对 —— 无条件 setToolTip 每
            # 250ms × 每盏灯都会让 Qt 重建 tooltip 对象, 内容却 99% 没变。
            if lbl.toolTip() != tip:
                lbl.setToolTip(tip)
    def _port_state_of(self, port: int) -> tuple[str, str, str]:
        """返回 (文字, 颜色, tooltip) —— 左侧面板窄, 文字一律短, 详情进 tooltip"""
        port = int(port)
        ov = (getattr(self, "_port_state_override", None) or {}).get(port)
        if ov:
            return ov
        ch = self.store.get(port)
        state = (ch.state if ch else "") or "idle"
        info = (ch.info or "") if ch else ""
        # 2026-10-19 fix: 设备**确认离线**(_online 为 False, 即探测过且确认掉线)
        # → 任何"历史结论"都已无意义, 直接回空闲, 不让上一次跑完的完成/失败挂在
        # 离线账号上。注意: 没探测到(_online 为 None)或压根没这个灯, 一律穿透到
        # 下面的正常派生 —— 不能凭"没探测"就误判离线(测试里 MainWindow 不跑探测,
        # 以及本机端口若不在 accounts.json 里就没灯, 都不该被误判成永远空闲)。
        light = (getattr(self, "_port_lights", None) or {}).get(port)
        online = getattr(light, "_online", None) if light is not None else None
        if online is False:
            return ("空闲", "#888", "")
        if state == "running":
            # 本机点了停止但 worker 还没退出 → 显示秒表, 用户能看到"确实在收"
            # (_stop_t0 在 _build_ui 之后才初始化, 首帧用 getattr 兜住)
            if port == self._local_port and getattr(self, "_stop_t0", None) is not None:
                return (f"停止中 {time.time() - self._stop_t0:.0f}s", "#c80",
                        "已发送停止请求, 等当前这一步安全退出")
            return ("运行中", "#1668dc",
                    f"任务: {ch.task}" if (ch and ch.task) else "")
        # 2026-10-19 fix: 终态结论(完成/失败/已停止/设备错误)先展示几秒, 再自动
        # 归位空闲, 否则账号状态永远停在上一轮结论。运行中不受影响(上面已返回)。
        if state in ("ok", "fail", "stopped", "error"):
            t1 = getattr(ch, "t1", None)
            if t1 is None or (time.time() - t1) > PORT_STATE_REVERT_SECONDS:
                return ("空闲", "#888", "")
        if state == "ok":
            # ok 但有步骤没做成: 引擎把原因写在 info 里(如"完成但有13步失败")
            n = _tail_int(info)
            if n:
                return (f"⚠{n}步失败", "#c80", info)
            return ("完成 ✔", "#2a6", info or "全部步骤已做成")
        if state == "fail":
            # 左侧只剩 ~140px, 失败原因(如"步骤[地煞-进入] 超时")塞不下还容易
            # 把灯挤变形 —— 标签只给结论, 原因进 tooltip(系统日志里也有全文)
            return ("失败 ✘", "#c33", f"失败: {info}" if info else "")
        if state == "stopped":
            return ("已停止", "#888", info or "已手动停止")
        if state == "error":
            return ("设备错误", "#c33", info)
        return ("空闲", "#888", "")
    def _set_port_state(self, port: int, text: str, color: str = "#333",
                        tip: str = ""):
        """临时接管某账号的状态文字(下次启动该账号时自动清掉)

        只用于"停止中"这类通道状态里没有的瞬时态, 以及任务结束时的结论
        (完成/失败要带具体数字, 比自动推导的更准)。
        """
        port = int(port)
        self._port_state_override[port] = (text, color, tip)
        lbl = (getattr(self, "_port_state_labels", None) or {}).get(port)
        if lbl is not None:
            lbl.setText(text)
            lbl.setStyleSheet(f"color:{color}; font-size:11px;")
            lbl.setToolTip(tip)
    def _clear_port_state(self, port: int):
        """交还给自动推导(启动新任务时调, 免得上一轮的"完成/失败"一直挂着)"""
        self._port_state_override.pop(int(port), None)
