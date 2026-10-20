# -*- coding: utf-8 -*-
"""渲染层 Mixin (2026-10-17 v3.5.0, 优化方案 P2-1)

从 main_window.py 切出来的「画面 / 步骤 / 日志」渲染部分(约 215 行)。

为什么用 Mixin 而不是独立类:
  这块代码几乎全是「读 store → 写 widget」, 但它要读写主窗口上的一堆状态
  (_steps_dirty / _page_sig / _steps_sig / _frame_drawn / _log_drawn /
  _sys_logs / _scroll_to), 这些状态在主窗口里被十几处赋值。做成独立类就得
  把它们全部搬走并改写所有赋值点, 风险与收益不成比例。
  Mixin 让方法原样搬过来继续跑在 self 上 —— 零行为变化, 纯粹是把文件变短。

注意: 本模块**不 import QMainWindow**, 也不参与 Qt 元对象; 它只是一组普通
方法, 由 MainWindow 通过多重继承拿到。改动这里的方法签名时, 记得 main_window
里还有调用点(用 grep 找)。
"""
from __future__ import annotations

import html
import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QListWidgetItem

from gui.live_store import Channel, StepRec


class RenderMixin:
    """实时画面 / 实时步骤 / 日志 三个页签的渲染

    依赖(由宿主 MainWindow 提供):
      · self.store            ChannelStore, 只读
      · self.page_screen / self.combo_step_acc / self.list_steps / self.txt_log
      · self.lbl_step_hint / self.tabs
      · 状态位: _page_sig _steps_sig _steps_dirty _frame_drawn _log_drawn
                _steps_items _sys_logs _scroll_to
      · 回调: _set_current_port(步骤页选账号时联动全局)
    """


    def append_sys_log(self, text: str):
        """系统级消息(多开/定时/保存…): 与账号日志合并后按时间顺序渲染"""
        self._sys_logs.append((time.time(), str(text)))

    def _render_tick(self):
        self._drain_probe()          # 取回后台端口探测结果更新状态灯(非阻塞)
        self._refresh_port_states()  # 每账号的任务状态(状态灯右侧那列)
        chans = self.store.channels()   # 一个 tick 只取一次(以前取了 3~4 次)
        self._render_logs(chans)
        self._render_screen_page(chans)
        self._render_steps(chans)

    def _render_all(self, force: bool = False):
        if force:
            self._page_sig = None
            self._steps_sig = None
        chans = self.store.channels()
        self._render_logs(chans)
        self._render_screen_page(chans)
        self._render_steps(chans)

    def _screen_tab_visible(self) -> bool:
        """「实时画面」是不是当前页签 —— 不在这一页就别转码(2026-10-19 v4.9.0 P1-2)

        页签 0 就是实时画面(_build_ui 里第一个 addTab)。以前不管当前在哪一页,
        每 250ms 都把 5 个号的新帧转成 QPixmap 再缩放一遍, 用户在看「脚本」页
        时这些工作全白做 —— 实测主线程 10~20% 花在这种看不见的像素搬运上。
        """
        try:
            return self.tabs.currentIndex() == 0
        except Exception:
            return True

    def _on_main_tab_changed(self, idx: int):
        """切回实时画面 → 整页补一次(隐藏期间攒下的帧只画最新一张)"""
        if idx != 0:
            return
        self._page_sig = None
        self._frame_drawn.clear()
        self._render_screen_page()

    def _render_screen_page(self, chans=None):
        chans = chans if chans is not None else self.store.channels()
        # 下拉/账号名很便宜, 无论在哪一页都保持同步(别让用户切回来看到旧名字)
        self._sync_step_account_combo(chans)
        if not self._screen_tab_visible():
            self._page_sig = None      # 下次切回来整页重建
            return
        sig = tuple((c.port, c.label, c.state, c.task, c.info) for c in chans)
        if sig != self._page_sig:
            self._page_sig = sig
            self.page_screen.refresh(chans)
        for c in chans:            # 新帧才喂给画面页(避免每 250ms 重建一遍 QPixmap)
            if c.frame_seq > self._frame_drawn.get(c.port, 0):
                self._frame_drawn[c.port] = c.frame_seq
                if c.frame is not None:
                    self.page_screen.set_frame(c.port, c.frame, c.frame_seq)

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

    def _render_steps(self, chans=None):
        if not self._steps_dirty:
            return
        self._steps_dirty = False
        sel = self.combo_step_acc.currentData()
        chans = chans if chans is not None else self.store.channels()
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

    def _render_logs(self, chans=None):
        """把各通道新增日志 + 系统消息按时间顺序写进日志框"""
        items: list[tuple[float, str]] = []
        chans = chans if chans is not None else self.store.channels()
        multi = len(chans) > 1
        for c in chans:
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
