# -*- coding: utf-8 -*-
"""实时画面(按账号): 账号下拉 + 大画面 + 缩略图条

改造前「实时画面」绑死 config.device.port 那一个设备(通常就是账号1),
多开账号的画面没有任何入口 —— 这就是"只能截图第一个账号"的直接原因。
现在:

    ┌─ 账号 [账号2 · 16416 ▾]   ● 运行中 · daily_all.yaml ──────┐
    │                                                          │
    │              大画面(点击可拾取坐标)                        │
    │                                                          │
    └─ [缩略图][缩略图][缩略图][缩略图]  ← 所有在跑的号一眼可见    ┘

点缩略图切换大画面; 大画面点击仍可拾取坐标(采集模板/生成步骤)。
坐标是模拟器逻辑分辨率(540x960)下的值, 与是哪个账号无关, 所以任何
账号的画面都能直接用来取点。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from gui.live_store import Channel
from gui.screen_view import ClickableScreen, bgr_to_qimage

# 缩略图尺寸(按 540x960 的竖屏比例: 88 / 156 ≈ 0.564)
THUMB_W, THUMB_H = 88, 156

_STATE_COLOR = {
    "running": "#0a6",      # 运行中
    "ok": "#2a6",           # 已完成
    "fail": "#c33",         # 失败
    "error": "#c33",        # 设备错误
    "stopped": "#c70",      # 已停止
    "idle": "#888",         # 未启动
}


class ThumbCard(QFrame):
    """一个账号的缩略图卡片(点一下切成大画面)"""

    picked = Signal(int)

    def __init__(self, port: int, parent=None):
        super().__init__(parent)
        self.port = int(port)
        self._current = False
        self.setFrameShape(QFrame.StyledPanel)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        v = QVBoxLayout(self)
        v.setContentsMargins(5, 5, 5, 5)
        v.setSpacing(2)

        self.lbl_img = QLabel("无画面")
        self.lbl_img.setFixedSize(THUMB_W, THUMB_H)
        self.lbl_img.setAlignment(Qt.AlignCenter)
        self.lbl_img.setStyleSheet(
            "background:#f2f2f2; color:#aaa; border:1px solid #ddd; font-size:11px;")
        v.addWidget(self.lbl_img)

        self.lbl_name = QLabel()
        self.lbl_name.setAlignment(Qt.AlignCenter)
        self.lbl_name.setFixedWidth(THUMB_W)
        self.lbl_name.setStyleSheet("font-size:11px; font-weight:bold; color:#333;")
        v.addWidget(self.lbl_name)

        self.lbl_state = QLabel()
        self.lbl_state.setAlignment(Qt.AlignCenter)
        self.lbl_state.setFixedWidth(THUMB_W)
        self.lbl_state.setStyleSheet("font-size:10px; color:#888;")
        v.addWidget(self.lbl_state)

        self.setStyleSheet(self._style())
        self.setCursor(Qt.PointingHandCursor)

    def _style(self) -> str:
        border = "2px solid #06c" if self._current else "1px solid #ddd"
        bg = "#eef4ff" if self._current else "#fafafa"
        return f"ThumbCard {{ border:{border}; background:{bg}; border-radius:3px; }}"

    def set_current(self, on: bool):
        if self._current != on:
            self._current = on
            self.setStyleSheet(self._style())

    def set_info(self, name: str, state_text: str, state: str):
        self.lbl_name.setText(name)
        self.lbl_state.setText(state_text)
        self.lbl_state.setStyleSheet(
            f"font-size:10px; color:{_STATE_COLOR.get(state, '#888')};")
        self.setToolTip(f"{name} · 端口{self.port}\n状态: {state_text}")

    def set_frame(self, bgr):
        """更新缩略图(等比缩放到卡片里)"""
        if bgr is None:
            return
        try:
            pix = QPixmap.fromImage(bgr_to_qimage(bgr))
        except Exception:
            return
        self.lbl_img.setPixmap(pix.scaled(THUMB_W, THUMB_H, Qt.KeepAspectRatio,
                                          Qt.SmoothTransformation))
        self.lbl_img.setStyleSheet("background:#000; border:1px solid #ddd;")

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self.picked.emit(self.port)
        super().mousePressEvent(ev)


class AccountScreenPage(QWidget):
    """实时画面页: 账号选择 + 大画面 + 缩略图条"""

    picked = Signal(int, int, int)      # (端口, x, y) 原始分辨率坐标
    account_selected = Signal(int)      # 用户在这个页切了账号 → 通知主窗口做全局同步

    def __init__(self, frame_of=None, parent=None):
        super().__init__(parent)
        self._frame_of = frame_of        # port -> 最新一帧(切账号时立刻回显)
        self._thumbs: dict[int, ThumbCard] = {}
        self._order: list[int] = []
        self._channels: dict[int, Channel] = {}

        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(6)

        # ---- 账号选择栏 ----
        bar = QHBoxLayout()
        bar.addWidget(QLabel("账号:"))
        self.combo = QComboBox()
        self.combo.setMinimumWidth(240)
        self.combo.currentIndexChanged.connect(self._on_combo_changed)
        bar.addWidget(self.combo)
        self.lbl_meta = QLabel("")
        self.lbl_meta.setStyleSheet("color:#666; font-size:12px;")
        bar.addWidget(self.lbl_meta, stretch=1)
        v.addLayout(bar)

        # ---- 大画面 ----
        self.view = ClickableScreen()
        self.view.clicked_at.connect(self._on_view_clicked)
        v.addWidget(self.view, stretch=1)

        # ---- 缩略图条(横向可滚动, 账号多时不挤) ----
        thumb_host = QWidget()
        self.thumb_layout = QHBoxLayout(thumb_host)
        self.thumb_layout.setContentsMargins(0, 0, 0, 0)
        self.thumb_layout.setSpacing(8)
        self.thumb_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedHeight(THUMB_H + 62)
        scroll.setWidget(thumb_host)
        scroll.setFrameShape(QFrame.NoFrame)
        v.addWidget(scroll)

        self._sync_empty()

    # ================= 通道刷新 =================
    def refresh(self, channels: list[Channel]):
        """按当前通道列表刷新下拉与缩略图(保留用户选中的账号)"""
        self._channels = {c.port: c for c in channels}
        ports = [c.port for c in channels]

        # ---- 缩略图: 先删掉已消失的通道 ----
        for port in list(self._thumbs):
            if port not in self._channels:
                card = self._thumbs.pop(port)
                self.thumb_layout.removeWidget(card)
                card.deleteLater()
                if port in self._order:
                    self._order.remove(port)

        # ---- 缩略图: 新增/更新 ----
        for port in ports:
            ch = self._channels[port]
            card = self._thumbs.get(port)
            if card is None:
                card = ThumbCard(port)
                card.picked.connect(self.select_port)
                self._thumbs[port] = card
                self._order.append(port)
                # 插到 stretch 之前, 保持左对齐
                self.thumb_layout.insertWidget(self.thumb_layout.count() - 1, card)
            card.set_info(ch.label, ch.state_text(), ch.state)
            if ch.frame is not None:
                card.set_frame(ch.frame)

        # ---- 下拉 ----
        cur = self.current_port()
        self.combo.blockSignals(True)
        self.combo.clear()
        for port in ports:
            ch = self._channels[port]
            tag = "本机" if ch.source == "local" else "多开"
            self.combo.addItem(f"{ch.title}  [{tag}]  {ch.state_text()}", port)
        if cur is not None and cur in ports:
            i = self.combo.findData(cur)
            if i >= 0:
                self.combo.setCurrentIndex(i)
        self.combo.blockSignals(False)

        self._mark_current()
        self._sync_empty()
        self._refresh_meta()
        self._show_current_frame()

    def _sync_empty(self):
        empty = self.combo.count() == 0
        self.lbl_meta.setText("暂无账号 —— 勾选「多开管理」里的账号并启动, "
                              "或点左侧「▶ 启动任务」跑本机任务" if empty else "")
        if empty:
            self.view.set_screen_placeholder("暂无画面")

    def select_port(self, port: int):
        i = self.combo.findData(int(port))
        if i >= 0:
            self.combo.setCurrentIndex(i)

    def current_port(self) -> int | None:
        d = self.combo.currentData()
        return int(d) if d is not None else None

    def _on_combo_changed(self, _i):
        self._mark_current()
        self._refresh_meta()
        self._show_current_frame()
        cur = self.current_port()
        if cur is not None:
            self.account_selected.emit(cur)

    def _mark_current(self):
        cur = self.current_port()
        for port, card in self._thumbs.items():
            card.set_current(port == cur)

    def _refresh_meta(self):
        cur = self.current_port()
        ch = self._channels.get(cur) if cur is not None else None
        if ch is None:
            return
        task = ch.task or "(未指定脚本)"
        extra = f" · {ch.info}" if ch.info else ""
        self.lbl_meta.setText(
            f"● {ch.state_text()} · {task} · 端口{ch.port}{extra}")
        self.lbl_meta.setStyleSheet(
            f"color:{_STATE_COLOR.get(ch.state, '#666')}; font-size:12px;")

    def _show_current_frame(self):
        cur = self.current_port()
        if cur is None:
            return
        ch = self._channels.get(cur)
        bgr = ch.frame if (ch and ch.frame is not None) else None
        if bgr is None and self._frame_of:
            bgr = self._frame_of(cur)
        if bgr is not None:
            self.view.set_screen(bgr)
        else:
            self.view.set_screen_placeholder(
                f"{self._channels[cur].label} 暂无画面\n(任务运行时才有实时截图)")

    # ================= 新帧 =================
    def set_frame(self, port: int, bgr):
        """有新帧: 更新缩略图; 若是当前选中的账号, 同时更新大画面"""
        card = self._thumbs.get(port)
        if card is not None:
            card.set_frame(bgr)
        if port == self.current_port():
            self.view.set_screen(bgr)

    def _on_view_clicked(self, x, y):
        port = self.current_port()
        if port is not None:
            self.picked.emit(int(port), int(x), int(y))
