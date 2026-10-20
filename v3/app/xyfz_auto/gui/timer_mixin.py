# -*- coding: utf-8 -*-
"""定时执行相关逻辑

拆自 main_window.py(2026-10-18 v4.7.0 批次四·工程 P2-1)。到点判定 / 启动 /
成败回写 / 重试 / 错峰 都在这里, 主窗口只留一个 QTimer 每 5 秒调 _tick_timer。

依赖(由宿主 MainWindow 提供):
  · 控件: self.table_timer / self.lbl_timer_state / self.tabs
  · 状态: self._timer_runs / self._timer_retry / self._timer_pending_ports /
          self._timer_cfg_path / self.cfg / self._active_log_names
  · 回调: self.append_sys_log / self._start_task / self.panel_multi
"""
from __future__ import annotations
import datetime
import json
import time
from pathlib import Path
from PySide6.QtCore import Qt, QTime, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QMessageBox, QTableWidgetItem,
                               QTimeEdit)
from accounts import load_accounts as _load_accounts
from gui.ui_helpers import _safe_call
class TimerMixin:
    """见模块 docstring。方法体整体搬自 main_window.py, 未作改写。"""
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
        """启动时读回上次的定时列表(gui_timer.json), 重启后不丢失

        2026-10-18 v4.6.0(P0-5): 连"今天已经跑过哪些"一起读回来。
        以前 _timer_runs 只活在内存里 —— GUI 崩一次重启, 同一天的定时任务
        就会被**再跑一遍**(用户反馈: 重启后又开始刷任务)。现在落盘, 并按日期
        自动丢弃过期记录, 不留垃圾。
        """
        today = datetime.date.today().isoformat()
        try:
            st = json.loads(Path(self._timer_cfg_path).read_text(encoding="utf-8"))
        except Exception:
            return
        for e in st.get("entries", []):
            self._append_timer_row(
                str(e.get("time", "00:10")),
                str(e.get("task", "")),
                e.get("acc_port"),
                bool(e.get("enabled", True)))
        runs = st.get("runs") or {}
        if isinstance(runs, dict):
            self._timer_runs = {k: v for k, v in runs.items() if v == today}
        tries = st.get("tries") or {}
        if isinstance(tries, dict):
            self._timer_try = {k: v for k, v in tries.items()
                               if isinstance(v, list) and v and v[0] == today}
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
        self._maybe_rotate_daily()      # v4.6.0(P0-4) 日志滚动三触发点 ②
        now = QTime.currentTime()
        now_ms = now.msecsSinceStartOfDay()
        today = datetime.date.today().isoformat()
        # v4.6.0(P0-5): 宽限/重试/错峰都改成可配置 —— 3 分钟宽限对不同人差别很大,
        # 写死的时候"错过即丢"这条骂名背了很久。
        grace_min = self._timer_cfg_int("timer_grace_minutes", 3)
        grace_ms = grace_min * 60 * 1000
        retry_cd = self._timer_cfg_int("timer_retry_cooldown_seconds", 300)
        stagger_s = self._timer_cfg_int("timer_stagger_seconds", 20)
        due: list[tuple] = []           # 本轮到点该跑的, 最后统一错峰放行
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
            # ---- v4.6.0(P0-5) ① 错过要"看得见" ----
            # 旧行为: 悄悄记成"今天跑过了"就完事, 用户完全不知道今天漏了一班
            # (GUI 没开/卡了都会走到这里)。现在明确报案。
            if now_ms - t_ms > grace_ms:
                if self._timer_runs.get(key) != today:
                    self._timer_runs[key] = today
                    self._persist_timer_runs()
                    self.append_sys_log(
                        f"[定时] 今日 {tm.toString('HH:mm')}「{task_name}」已错过"
                        f"(超过 {grace_min} 分钟未执行, 多半是当时 GUI 没开或被卡住), "
                        f"今天不再补跑")
                continue
            # ---- v4.6.0(P0-5) ② 重试冷却 ----
            # 失败会清掉 _timer_runs 让它重试, 但不能立刻重试 —— 否则脚本一失败
            # 就被每 5 秒一次的轮询反复鞭尸。
            last = self._timer_last_try.get(key) or 0.0
            if last and (time.time() - last) < retry_cd:
                continue
            due.append((key, task_name, acc_port, tm))
        # ---- v4.6.0(P0-5) ③ 错峰 ----
        # 以前同一 tick 里到点的账号会被一股脑同时拉起 —— 5 个号同时打 adb,
        # 截图串行排队, 互相拖到超时。现在排好队, 每隔 stagger 秒放一个。
        for i, (key, task_name, acc_port, tm) in enumerate(due):
            self._timer_last_try[key] = time.time()
            if i == 0:
                self._fire_timer_item(key, task_name, acc_port, tm, now)
            else:
                QTimer.singleShot(
                    int(i * stagger_s * 1000),
                    lambda k=key, t=task_name, a=acc_port, m=tm:
                        _safe_call(self._fire_timer_item, k, t, a, m, QTime.currentTime()))
        suffix = (f", 已错峰排入 {len(due)} 条(每 {stagger_s:g} 秒放一个)"
                  if len(due) > 1 else "")
        self.lbl_timer_state.setText(
            f"定时: {n_enabled} 条待命 (每天到点自动执行){suffix}")
    def _fire_timer_item(self, key, task_name, acc_port, tm, now):
        today = datetime.date.today().isoformat()
        self._timer_runs[key] = today
        self._persist_timer_runs()
        if acc_port:
            res = self.panel_multi.launch(acc_port, task_name, "")
            if res is True:
                self._timer_pending_ports[int(acc_port)] = key
                self.append_sys_log(
                    f"[定时] {tm.toString('HH:mm')} 到点, 账号端口{acc_port} "
                    f"自动启动「{task_name}」(子进程)")
            elif res is False:
                self.append_sys_log(
                    f"[定时] 端口{acc_port} 已有任务在跑, 今日不重复启动")
            else:   # "busy"
                # 主任务在跑 ≠ 这次定时"做过了" —— 让下个 tick 还能再试,
                # 否则一次撞车就永久占用当天份额(旧行为)。
                self._timer_runs.pop(key, None)
                self._persist_timer_runs()
                self.append_sys_log(
                    f"[定时] 端口{acc_port}=主GUI设备且主任务在跑, 稍后重试本次")
            return
        if self.worker and self.worker.isRunning():
            # 同理: 本机被占, 不算"跑过了", 退回去下轮再试
            self._timer_runs.pop(key, None)
            self._persist_timer_runs()
            self.lbl_timer_state.setText(
                f"定时: {tm.toString('HH:mm')}「{task_name}」已到点, 等当前任务结束…")
            return
        self._timer_local_key = key
        self.append_sys_log(
            f"[定时] {tm.toString('HH:mm')} 到点, 自动启动「{task_name}」")
        self._auto_start_task(task_name)
    def _timer_cfg_int(self, name: str, default: int) -> int:
        try:
            v = int((self.cfg.get("engine") or {}).get(name, default))
            return v if v > 0 else default
        except Exception:
            return default
    def _persist_timer_runs(self):
        """把"今天跑过哪些"落盘(P0-5): 崩溃重启不至于重复跑"""
        try:
            self._save_timer_state()
        except Exception:
            pass
    def _timer_done(self, key: str, ok: bool):
        """定时任务跑完了 —— 成败回写当日份额

        旧行为: 出发那一刻就把当天份额吃掉, 后面跑崩了也不管 —— 于是"明明失败了
        今天却再也不跑了"(用户反馈)。现在: 失败且重试次数没用完 → 把份额退回去,
        让它在冷却期后再试一次; 用完了才真正记为今天已执行。
        """
        if not key:
            return
        today = datetime.date.today().isoformat()
        self._timer_local_key = None
        try:
            if ok:
                self._timer_try.pop(key, None)
                self._timer_runs[key] = today
                self._persist_timer_runs()
                return
            tries = self._timer_try.get(key) or [today, 0]
            if str(tries[0]) != today:
                tries = [today, 0]
            tries[1] = int(tries[1]) + 1
            self._timer_try[key] = tries
            max_retry = self._timer_cfg_int("timer_max_retry", 2)
            if tries[1] <= max_retry:
                self._timer_runs.pop(key, None)   # 退还份额 → 冷却后可重试
                self.append_sys_log(
                    f"[定时] 「{key.split('|')[1] if '|' in key else key}」本次未做成, "
                    f"将在冷却后重试(第 {tries[1]}/{max_retry} 次)")
            else:
                self._timer_runs[key] = today     # 重试也用完了, 认了
                self.append_sys_log(
                    f"[定时] 「{key.split('|')[1] if '|' in key else key}」重试 "
                    f"{tries[1]} 次仍未做成, 今日不再重试")
            self._persist_timer_runs()
        except Exception:
            pass
    def _auto_start_task(self, task_name):
        """定时触发: 按脚本文件名定位并启动(不覆盖编辑器未保存内容)"""
        p = next((c for c in sorted(self._task_dir().glob("*.yaml"))
                  if c.name == task_name), None)
        if p is None:
            self.append_sys_log(f"[定时] 找不到任务脚本 {task_name}, 已跳过")
            return
        self.current_task_path = str(p)
        self._start_task(auto=True, path=str(p))
