# -*- coding: utf-8 -*-
"""可点击的截图画面控件

实时显示模拟器截图(缩放适配)，鼠标点击时换算回原始分辨率坐标并发送信号，
供「点画面取坐标→生成步骤」使用。
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QSizePolicy, QWidget


def bgr_to_qimage(bgr: np.ndarray) -> QImage:
    """OpenCV BGR → QImage(RGB888)，拷贝像素防悬空"""
    rgb = np.ascontiguousarray(bgr[:, :, ::-1])
    h, w = rgb.shape[:2]
    return QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()


class ClickableScreen(QWidget):
    """截图预览 + 点击取坐标"""

    clicked_at = Signal(int, int)   # 原始分辨率坐标 (x, y)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._pixmap: QPixmap | None = None
        self._scale = 1.0           # 显示缩放比
        self._offset_x = 0.0        # 内容区相对控件偏移
        self._offset_y = 0.0
        self._crosshair = None      # 十字光标坐标(原始分辨率)
        self._placeholder: str | None = None   # 无画面时的占位文字
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(240, 400)
        self.setMouseTracking(True)

    def set_screen(self, bgr: np.ndarray):
        """更新画面(OpenCV BGR numpy)"""
        img = bgr_to_qimage(bgr)
        self._pixmap = QPixmap.fromImage(img)
        self._placeholder = None
        self.update()

    def set_screen_placeholder(self, text: str):
        """没有画面可显示时(某账号还没出图)给出说明文字, 避免一片空白"""
        self._pixmap = None
        self._placeholder = str(text)
        self.update()

    def set_crosshair(self, xy):
        self._crosshair = xy
        self.update()

    def clear_crosshair(self):
        self._crosshair = None
        self.update()

    def _layout_info(self):
        """计算 pixmap 在控件内的显示区域(居中缩放适配)"""
        if self._pixmap is None:
            return 0, 0, 0, 0, 1.0
        pw, ph = self._pixmap.width(), self._pixmap.height()
        scale = min(self.width() / pw, self.height() / ph)
        dw, dh = pw * scale, ph * scale
        ox, oy = (self.width() - dw) / 2, (self.height() - dh) / 2
        return ox, oy, dw, dh, scale

    def _to_screen(self, wx, wy):
        ox, oy, dw, dh, scale = self._layout_info()
        if scale <= 0 or self._pixmap is None:
            return None
        x = int((wx - ox) / scale)
        y = int((wy - oy) / scale)
        pw, ph = self._pixmap.width(), self._pixmap.height()
        if 0 <= x < pw and 0 <= y < ph:
            return x, y
        return None

    def paintEvent(self, ev):
        super().paintEvent(ev)
        if self._pixmap is None:
            if self._placeholder:
                # 占位说明(多开时某个账号还没出图, 明确告诉用户为什么是空的)
                p = QPainter(self)
                p.setPen(QPen(QColor("#999")))
                p.drawText(self.rect(), Qt.AlignCenter | Qt.TextWordWrap,
                           self._placeholder)
                p.end()
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        ox, oy, dw, dh, _ = self._layout_info()
        p.drawPixmap(int(ox), int(oy), int(dw), int(dh), self._pixmap)
        # 边框
        p.setPen(QPen(Qt.gray, 1))
        p.drawRect(int(ox), int(oy), int(dw), int(dh))
        # 十字光标
        if self._crosshair:
            cx = ox + self._crosshair[0] * dw / self._pixmap.width()
            cy = oy + self._crosshair[1] * dh / self._pixmap.height()
            pen = QPen(Qt.red, 1, Qt.DashLine)
            p.setPen(pen)
            p.drawLine(int(cx), int(oy), int(cx), int(oy + dh))
            p.drawLine(int(ox), int(cy), int(ox + dw), int(cy))
        p.end()

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            xy = self._to_screen(ev.position().x(), ev.position().y())
            if xy:
                self.set_crosshair(xy)
                self.clicked_at.emit(*xy)
        super().mousePressEvent(ev)
