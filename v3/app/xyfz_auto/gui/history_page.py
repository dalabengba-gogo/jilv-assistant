# -*- coding: utf-8 -*-
"""运行历史页签 (v5.6.0)

读取 logs/reports/*_p<port>.json(由 run_one.py 在每次任务跑完时落盘), 按账号/日期
列出每轮运行的成败、子任务明细、失败步骤、性能统计, 点选某轮可看详情。

设计要点:
  · 纯只读展示, 不依赖实时通道(live), 关掉任务也能回看历史。
  · 切到本页或点「刷新」时从磁盘重新读取, 所以刚跑完的一轮立刻能看到。
  · 详情区用纯文本渲染(子任务级汇总 + 失败/条件未命中逐步 + 性能), 不堆控件。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QHeaderView, QHBoxLayout, QLabel, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
)

from run_report import load_run_reports


def _fmt_duration(sec) -> str:
    try:
        s = float(sec or 0)
    except Exception:
        return "-"
    if s < 60:
        return f"{s:.0f}s"
    m = int(s // 60)
    if m < 60:
        return f"{m}m{s % 60:.0f}s"
    return f"{m // 60}h{m % 60}m"


def _result_mark(r: dict) -> str:
    if r.get("stopped"):
        return "⏹ 已停止"
    if r.get("ok"):
        return "✔ 完成"
    return "✘ 失败"


class HistoryPage(QWidget):
    """运行历史页: 上=账号筛选+列表, 下=详情(或左列表右详情)。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._records: list[dict] = []
        self._build_ui()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("账号:"))
        self.combo_acc = QComboBox()
        self.combo_acc.setMinimumWidth(220)
        self.combo_acc.addItem("全部账号", "")
        self.combo_acc.currentIndexChanged.connect(lambda *_: self._apply_filter())
        bar.addWidget(self.combo_acc)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.clicked.connect(self.refresh)
        bar.addWidget(self.btn_refresh)
        bar.addStretch(1)
        self.lbl_stat = QLabel("")
        self.lbl_stat.setStyleSheet("color:#888; font-size:11px;")
        bar.addWidget(self.lbl_stat)
        root.addLayout(bar)

        split = QSplitter(Qt.Horizontal)

        # 左: 运行列表
        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(
            ["结束时间", "端口/账号", "任务", "结果", "失败", "条件未命中", "耗时"])
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.itemSelectionChanged.connect(self._on_select)
        split.addWidget(self.table)

        # 右: 详情
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setFont(_mono_font())
        split.addWidget(self.detail)

        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, stretch=1)

    def refresh(self):
        """从磁盘重新读取运行报告并填充列表(切到本页/点刷新时调用)。"""
        try:
            self._records = load_run_reports(days=30)
        except Exception:
            self._records = []
        self._rebuild_account_filter()
        self._apply_filter()

    def _rebuild_account_filter(self):
        cur = self.combo_acc.currentData() or ""
        self.combo_acc.blockSignals(True)
        self.combo_acc.clear()
        self.combo_acc.addItem("全部账号", "")
        seen = set()
        for r in self._records:
            key = f"{r.get('port', '?')}"
            label = f"{r.get('account', '')} (端口{r.get('port', '?')})".strip()
            if key not in seen and r.get("port") is not None:
                seen.add(key)
                self.combo_acc.addItem(label, str(r.get("port")))
        # 尽量保留之前的筛选
        idx = self.combo_acc.findData(cur)
        if idx >= 0:
            self.combo_acc.setCurrentIndex(idx)
        self.combo_acc.blockSignals(False)

    def _apply_filter(self):
        port = self.combo_acc.currentData() or ""
        recs = self._records
        if port:
            recs = [r for r in recs if str(r.get("port")) == str(port)]
        self._filtered = recs
        self._fill_table(recs)
        self.lbl_stat.setText(f"共 {len(recs)} 轮运行")
        self.detail.clear()

    def _fill_table(self, recs: list[dict]):
        self.table.setRowCount(len(recs))
        for row, r in enumerate(recs):
            self.table.setItem(row, 0, QTableWidgetItem(r.get("finished_at", "")))
            self.table.setItem(
                row, 1, QTableWidgetItem(
                    f"{r.get('account', '')} · {r.get('port', '?')}"))
            self.table.setItem(row, 2, QTableWidgetItem(r.get("task_name") or r.get("task") or ""))
            mark = _result_mark(r)
            item = QTableWidgetItem(mark)
            if r.get("stopped"):
                item.setForeground(_qt_color("#c47f00"))
            elif r.get("ok"):
                item.setForeground(_qt_color("#1a8a3c"))
            else:
                item.setForeground(_qt_color("#c0392b"))
            self.table.setItem(row, 3, item)
            c = r.get("counts") or {}
            self.table.setItem(row, 4, QTableWidgetItem(str(c.get("failed", 0) or 0)))
            self.table.setItem(row, 5, QTableWidgetItem(str(c.get("skipped", 0) or 0)))
            self.table.setItem(row, 6, QTableWidgetItem(_fmt_duration(r.get("duration_seconds", 0))))
        self.table.resizeColumnsToContents()
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)

    def _on_select(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        idx = rows[0].row()
        recs = getattr(self, "_filtered", self._records)
        if 0 <= idx < len(recs):
            self._render_detail(recs[idx])

    def _render_detail(self, r: dict):
        lines = []
        lines.append(f"任务: {r.get('task_name') or r.get('task')}  ({r.get('task')})")
        lines.append(f"账号: {r.get('account')}  端口: {r.get('port')}")
        lines.append(f"结束: {r.get('finished_at')}   耗时: "
                     f"{_fmt_duration(r.get('duration_seconds', 0))}")
        lines.append(f"结果: {_result_mark(r)}   最后步骤: "
                     f"{((r.get('counts') or {}).get('last_step') or '-')}")
        if r.get("aborted_reason"):
            lines.append(f"原因: {r.get('aborted_reason')}")
        lines.append("")

        # 子任务级汇总
        subs = r.get("subtasks") or []
        if subs:
            lines.append("【子任务】")
            for s in subs:
                if isinstance(s, dict):
                    name = s.get("name", "?")
                    total = s.get("total", 0)
                    failed = s.get("failed", 0)
                    ver = s.get("verified")
                    if ver is True:
                        vmark = " ✔面板已完成"
                    elif ver is False:
                        vmark = " ✘面板未显示"
                    else:
                        vmark = ""
                    mark = "⚠" if failed else "✔"
                    lines.append(f"  {mark} {name}  ({total}步"
                                 + (f", {failed}步未做成" if failed else "")
                                 + f"){vmark}")
            lines.append("")

        # 失败步骤
        fails = r.get("failed_steps") or []
        if fails:
            lines.append(f"【失败步骤 {len(fails)}】")
            for f in fails[:40]:
                if isinstance(f, dict):
                    lines.append(f"  ✘ #{f.get('index','?')} {f.get('name','?')}"
                                 f"  ({f.get('reason','')})")
            if len(fails) > 40:
                lines.append(f"  … 其余 {len(fails)-40} 步省略")
            lines.append("")

        # 条件未命中(已领完/没得领这类属正常)
        skips = r.get("skipped_steps") or []
        if skips:
            lines.append(f"【条件未命中 {len(skips)}（属正常）】")
            for s in skips[:25]:
                if isinstance(s, dict):
                    lines.append(f"  ○ #{s.get('index','?')} {s.get('name','?')}")
            if len(skips) > 25:
                lines.append(f"  … 其余 {len(skips)-25} 步省略")
            lines.append("")

        # 性能
        p = r.get("performance") or {}
        if p:
            lines.append("【性能】")
            lines.append(
                f"  截图 {p.get('shots',0)} 次(复用 {p.get('frame_reuse',0)}) · "
                f"OCR 全图 {p.get('ocr_full',0)} / 裁剪带 {p.get('ocr_band',0)} / "
                f"热点命中 {p.get('ocr_hint_hit',0)} / 同帧复用 {p.get('ocr_cached',0)} / "
                f"同屏跳过 {p.get('ocr_same_screen',0)}")
            lines.append(
                f"  累计截图 {p.get('shot_seconds',0)}s + OCR {p.get('ocr_seconds',0)}s")

        self.detail.setPlainText("\n".join(lines))

    # showEvent: 切到本页时自动刷新
    def showEvent(self, ev):
        try:
            self.refresh()
        except Exception:
            pass
        super().showEvent(ev)


def _has_consolas() -> bool:
    try:
        from PySide6.QtGui import QFontDatabase
        return "Consolas" in QFontDatabase.families()
    except Exception:
        return False


def _qt_color(hex_str: str):
    from PySide6.QtGui import QColor
    return QColor(hex_str)


def _mono_font():
    from PySide6.QtGui import QFont
    if _has_consolas():
        return QFont("Consolas", 10)
    return QFont("Monospace", 9)
