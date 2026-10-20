# -*- coding: utf-8 -*-
"""多开管理面板: 管理最多 5 个账号的模拟器实例 + 一键启动全部

每个账号对应一个 MuMu 模拟器实例(ADB 端口可手动改), 一个账号跑一个任务脚本。
「启动全部」为每个启用的账号拉起独立子进程 scripts/run_one.py,
日志各自写 logs/run_<port>.log, 一个号失败不影响其它号。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from accounts import (
    ACCOUNTS_PATH, MAX_ACCOUNTS, PROJECT_ROOT, load_accounts, save_accounts,
)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# 2026-10-18 v4.5.0 (P1-4): 协作停止的硬杀兜底时限。
# 用户点停止 → 写停止标记 → 子进程主线程收尾当前步骤(通常 ≤6s)后自行退出。
# 若标记写下超过这个秒数进程还不退出(主线程卡死), 由这里强制 TerminateProcess。
STOP_GRACE_SECONDS = 25


class MultiPanel(QWidget):
    """多开管理(账号 x 端口 x 脚本), 行数固定 MAX_ACCOUNTS"""

    def __init__(self, mainwin):
        super().__init__()
        self.mainwin = mainwin
        self._procs: dict[int, dict] = {}   # port -> {"p": Popen, "row": int}
        self._build_ui()
        self._reload()

        # 周期轮询子进程状态刷新表格
        self._poll_timer = QTimer(self)
        self._poll_timer.timeout.connect(self._poll_procs)
        self._poll_timer.start(3000)

    # ================= UI =================
    def _build_ui(self):
        v = QVBoxLayout(self)
        head = QLabel(
            "多开管理 —— 最多 5 个账号, 每个账号绑定一个 MuMu 模拟器实例的 ADB 端口。\n"
            "MuMu 多开默认端口: 16384 / 16416 / 16448 / 16480 / 16512 (实例1 起每实例 +32), "
            "请按 MuMu 多开器里的实际值手动修改。")
        head.setStyleSheet("color:#555; font-size:12px;")
        head.setWordWrap(True)
        v.addWidget(head)

        self.tbl = QTableWidget(0, 6, self)
        self.tbl.setHorizontalHeaderLabels(["启用", "名称", "ADB端口", "任务脚本", "延迟(分)", "状态"])
        self.tbl.setColumnWidth(0, 46)
        self.tbl.setColumnWidth(1, 90)
        self.tbl.setColumnWidth(2, 70)
        self.tbl.setColumnWidth(4, 64)
        self.tbl.setColumnWidth(5, 110)
        self.tbl.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.tbl.horizontalHeader().setSectionResizeMode(1, QHeaderView.Fixed)
        self.tbl.verticalHeader().setVisible(False)
        self.tbl.setEditTriggers(QTableWidget.DoubleClicked)   # 双击改 名称/端口/延迟
        self.tbl.horizontalHeaderItem(5).setToolTip(
            "运行N分=任务执行中 / ✅成功=跑完 / 失败=任务中断 / 设备错误=端口连不上 / 未启动=还没拉起")
        v.addWidget(self.tbl, stretch=1)

        btn_h = QHBoxLayout()
        self.btn_save = QPushButton("保存配置")
        self.btn_save.setToolTip("把表格里的 端口/脚本/延迟 写入 accounts.json")
        self.btn_save.clicked.connect(self._on_save)
        btn_h.addWidget(self.btn_save)
        self.btn_refresh_scripts = QPushButton("刷新脚本列表")
        self.btn_refresh_scripts.clicked.connect(self._refresh_scripts)
        btn_h.addWidget(self.btn_refresh_scripts)
        btn_h.addStretch(1)
        self.btn_view = QPushButton("📺 看选中账号画面")
        self.btn_view.setToolTip(
            "跳到「实时画面」并切到该账号 —— 多开时每个号都能单独看画面与步骤")
        self.btn_view.clicked.connect(self._on_view_screen)
        btn_h.addWidget(self.btn_view)
        self.btn_start = QPushButton("▶ 启动全部(多开)")
        self.btn_start.setStyleSheet("font-weight:bold;")
        self.btn_start.clicked.connect(self._on_start_all)
        btn_h.addWidget(self.btn_start)
        self.btn_stop = QPushButton("■ 停止全部")
        self.btn_stop.clicked.connect(self._on_stop_all)
        btn_h.addWidget(self.btn_stop)
        v.addLayout(btn_h)

        self.lbl_state = QLabel("未启动")
        self.lbl_state.setStyleSheet("color:#888; font-size:11px;")
        v.addWidget(self.lbl_state)

        leg = QLabel(
            "状态列含义: 运行N分=任务执行中 | ✅成功=已跑完 | 失败=任务中断(看 logs/run_<端口>.log) "
            "| 设备错误=adb 连不上该端口(模拟器没开/端口填错/没开调试) | 已停止=手动停止 "
            "| 延迟N分=等待错峰计时 | 跳过=端口与主GUI相同且主GUI正忙 | 未启用=该行没勾选")
        leg.setStyleSheet("color:#aaa; font-size:11px;")
        leg.setWordWrap(True)
        v.addWidget(leg)

        tip = QLabel(
            "提示: ① 先把每个模拟器实例里的游戏登录好对应账号(纯享/记住密码), 脚本只管日常。"
            "② 端口必须与该实例开启 adb 调试后的真实端口一致——可在 MuMu 多开器里查, "
            "或命令行 `adb devices` 核对。③ 延迟(分)用于错峰: 多个号建议 15~30 分钟错开, "
            "避免同时寻路战斗抢 CPU。④ 每个号日志在 logs/run_<端口>.log。")
        tip.setStyleSheet("color:#999; font-size:11px;")
        tip.setWordWrap(True)
        v.addWidget(tip)

    # ================= 表格读写 =================
    def _task_dir(self):
        return PROJECT_ROOT / "assets" / "tasks"

    def _refresh_scripts(self, keep_texts: bool | None = None):
        """重建每行脚本下拉(保留当前选中项, 供新增脚本后刷新)"""
        files = sorted(p.name for p in self._task_dir().glob("*.yaml"))
        for r in range(self.tbl.rowCount()):
            cur = None
            old = self.tbl.cellWidget(r, 3)
            if old is not None:
                cur = old.currentText()
            cb = QComboBox()
            cb.addItems(files)
            if cur in files:
                cb.setCurrentText(cur)
            cb.setToolTip("该账号要跑的任务脚本")
            self.tbl.setCellWidget(r, 3, cb)

    def _reload(self):
        """从 accounts.json 装载(固定 MAX_ACCOUNTS 行)"""
        rows = load_accounts()
        self.tbl.setRowCount(len(rows))
        for r, acc in enumerate(rows):
            ck = QCheckBox()
            ck.setChecked(bool(acc.get("enabled")))
            ck.setStyleSheet("margin-left:13px;")
            ck.setToolTip("勾选=参与「启动全部」")
            self.tbl.setCellWidget(r, 0, ck)
            it_name = QTableWidgetItem(str(acc.get("name", "")))
            self.tbl.setItem(r, 1, it_name)
            it_port = QTableWidgetItem(str(acc.get("port", "")))
            it_port.setTextAlignment(Qt.AlignCenter)
            self.tbl.setItem(r, 2, it_port)
            it_delay = QTableWidgetItem(str(acc.get("delay_min", 0)))
            it_delay.setTextAlignment(Qt.AlignCenter)
            self.tbl.setItem(r, 4, it_delay)
            self.tbl.setItem(r, 5, QTableWidgetItem("未启动"))
        self._refresh_scripts()

    def _collect_rows(self) -> list[dict]:
        """表格 → 账号清单(校验端口/名称/脚本)"""
        rows = []
        for r in range(self.tbl.rowCount()):
            ck = self.tbl.cellWidget(r, 0)
            cb = self.tbl.cellWidget(r, 3)
            name = (self.tbl.item(r, 1).text() if self.tbl.item(r, 1) else "").strip()
            ptext = (self.tbl.item(r, 2).text() if self.tbl.item(r, 2) else "").strip()
            dtext = (self.tbl.item(r, 4).text() if self.tbl.item(r, 4) else "").strip()
            try:
                port = int(ptext)
            except ValueError:
                raise ValueError(f"第{r+1}行 ADB端口 不是数字: {ptext!r}")
            if not (1000 <= port <= 65535):
                raise ValueError(f"第{r+1}行 ADB端口 超出范围: {port}")
            try:
                delay = int(dtext or 0)
            except ValueError:
                raise ValueError(f"第{r+1}行 延迟(分) 不是数字: {dtext!r}")
            task = cb.currentText() if cb else ""
            if not task:
                raise ValueError(f"第{r+1}行 未选任务脚本")
            rows.append({
                "enabled": bool(ck and ck.isChecked()),
                "name": name or f"账号{r+1}",
                "port": port,
                "task": task,
                "delay_min": max(0, delay),
            })
        # 2026-10-18 v4.5.0 (P1-5): 端口所有权校验 —— 同一 adb 端口只能给一个账号。
        # 两行配同一端口是最常见的"串号"根因(画面/步骤混到别的号), 在落库/启动前就拦掉。
        ports = [r["port"] for r in rows]
        dups = sorted({p for p in ports if ports.count(p) > 1})
        if dups:
            raise ValueError(
                f"端口重复: {', '.join(str(p) for p in dups)} 被多行账号占用 —— "
                f"同一端口只能给一个账号, 否则会串号(画面/步骤混到别的号)。"
                f"请先到 MuMu 多开器给重复实例分配不同 adb 端口, 再保存。")
        return rows

    def _on_save(self):
        try:
            save_accounts(self._collect_rows())
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        # 端口改名要立刻反映到「实时画面」下拉与步骤前缀, 否则两个地方两个叫法
        try:
            self.mainwin.refresh_account_names()
        except Exception:
            pass
        self.lbl_state.setText(f"已保存到 {ACCOUNTS_PATH.name}")
        self.mainwin.append_sys_log(f"[多开] 账号配置已保存({MAX_ACCOUNTS} 行)")

    def _on_view_screen(self):
        """跳到该账号的实时画面(表格当前行)"""
        r = self.tbl.currentRow()
        it = self.tbl.item(r, 2) if r >= 0 else None
        if it is None:
            QMessageBox.information(self, "提示", "先点一下表格里的账号行")
            return
        try:
            port = int(it.text().strip())
        except ValueError:
            QMessageBox.warning(self, "提示", f"端口不是数字: {it.text()!r}")
            return
        self.mainwin.show_account_screen(port)

    # ================= 启动 / 停止 =================
    def _row_of_port(self, port: int) -> int:
        """按端口找到表格行号(用于刷新状态列); 找不到返回 -1"""
        for r in range(self.tbl.rowCount()):
            it = self.tbl.item(r, 2)
            if it and it.text().strip() == str(port):
                return r
        return -1

    def running_ports(self) -> set[int]:
        return set(self._procs.keys())

    def launch(self, port: int, task: str, name: str = "", start_index: int = 0):
        """按端口拉起一个账号子进程(供「启动全部」与定时任务共用)

        start_index: 从脚本第 N 步(0基)开始跑(「脚本步骤」页"从这步开始"用)
        返回: True=已拉起 / False=该端口已在运行 / "busy"=端口与主GUI相同且主GUI正在跑任务
        """
        if port in self._procs:
            return False
        gui_port = int(self.mainwin.cfg["device"].get("port", 16384))
        if port == gui_port and self.mainwin.worker and self.mainwin.worker.isRunning():
            return "busy"
        row = self._row_of_port(port)
        self._spawn(port, task, name or f"账号(端口{port})", row, start_index=start_index)
        return True

    def _spawn(self, port: int, task: str, name: str, row: int, start_index: int = 0):
        # 绿色版 GUI 由 pythonw 启动时, 子进程改用同目录 python.exe(有控制台流, 日志正常)
        exe = Path(sys.executable)
        if exe.name.lower().startswith("pythonw"):
            alt = exe.with_name("python.exe")
            if alt.exists():
                exe = alt
        cmd = [str(exe), "scripts/run_one.py",
               "--port", str(port), "--task", task, "--tag", name]
        if int(start_index or 0) > 0:
            # 2026-10-09: 从脚本第 N 步开始跑(「脚本步骤」页"从这步开始"用)
            cmd += ["--start-index", str(int(start_index))]
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(exist_ok=True)
        # 2026-10-18 v4.6.0(P0-4 前置·单写方): 以前这里把子进程 stdout 指向
        # run_<port>.log, 和子进程自己的 FileHandler **抢同一个文件** —— 每条日志
        # 写两遍, 且任一方轮转都会和另一方打架。现在分工:
        #   · run_<port>.log  ← 子进程独写(RotatingFileHandler, 会滚动)
        #   · boot_<port>.log ← 这里接 stdout(早期崩溃栈/裸 print 的兜底)
        # 且用 "w" 每次启动覆盖 —— 只留最近一次启动的输出, 不会无限增长。
        f = open(log_dir / f"boot_{port}.log", "w", encoding="utf-8")
        guard = getattr(self.mainwin, "spawn_guard", None)
        try:
            if guard is not None:
                # 纳入作业对象: GUI 一退出, 系统连带结束该子进程, 不留孤儿
                # (旧版只靠关闭窗口时 terminate, 崩溃/强杀就会漏)
                # 2026-09-29: 必须带 CREATE_NO_WINDOW, 否则 pythonw GUI 拉起
                # python.exe 子进程时 Windows 会新分配控制台 → 弹黑色命令行窗口
                p = guard.popen(cmd, cwd=str(PROJECT_ROOT), stdout=f,
                                stderr=subprocess.STDOUT,
                                creationflags=CREATE_NO_WINDOW)
            else:
                p = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT), stdout=f,
                                     stderr=subprocess.STDOUT,
                                     creationflags=CREATE_NO_WINDOW)
        except Exception as e:
            f.close()
            self.lbl_state.setText(f"端口{port} 启动失败: {e}")
            self._set_row(row, "启动失败")
            return
        # 2026-10-17 v3.5.0(P1-4): 子进程创建时已**复制**了这份句柄, 父进程这份
        # 留着毫无用处却一直占着文件到进程退出(多开 5 个号就 5 个句柄, 且退出后
        # logs 目录删不掉)。这里立刻关掉 —— 不影响子进程继续写它那份日志。
        f.close()
        self._procs[port] = {"p": p, "row": row, "start": time.time(),
                             "name": name, "task": task}
        self._set_row(row, "运行中")
        self.lbl_state.setText(f"运行 {len(self._procs)} 个账号… (日志 logs/run_{port}.log)")
        # 通知主窗口把这个账号挂到实时通道上 —— 之后它的画面/步骤/日志
        # 会像本机任务一样出现在「实时画面」「实时步骤」里
        try:
            self.mainwin.on_child_launched(port, name, task)
        except Exception:
            pass
        self.mainwin.append_sys_log(
            f"[多开] 账号「{name}」端口{port} 已启动 (任务 {task})")

    def _on_start_all(self):
        # 先落库再启动
        try:
            rows = self._collect_rows()
        except Exception as e:
            QMessageBox.warning(self, "配置有误", str(e))
            return
        save_accounts(rows)
        # 2026-09-29: 启动全部也会落盘 accounts.json, 同样要刷新全局账号下拉
        try:
            self.mainwin.refresh_account_names()
        except Exception:
            pass
        gui_port = int(self.mainwin.cfg["device"].get("port", 16384))
        gui_busy = bool(self.mainwin.worker and self.mainwin.worker.isRunning())
        started = 0
        for i, acc in enumerate(rows):
            if not acc["enabled"]:
                self._set_row(i, "未启用")
                continue
            port = acc["port"]
            if self._procs.get(port):
                continue                        # 该端口已在跑
            if gui_busy and port == gui_port:
                self._set_row(i, "跳过(本机任务中)")
                continue                        # 主 GUI 正在跑同一个模拟器
            delay = acc["delay_min"]
            if delay > 0:
                self._set_row(i, f"延迟{delay}分")
                QTimer.singleShot(
                    delay * 60_000,
                    lambda p=port, t=acc["task"], n=acc["name"], r=i:
                        self._spawn(p, t, n, r))
            else:
                self._spawn(port, acc["task"], acc["name"], i)
            started += 1
        if started:
            self.mainwin.append_sys_log(
                f"[多开] 提交 {started} 个账号, 已并行运行 {len(self._procs)} 个")
        else:
            self.lbl_state.setText("没有可启动的账号(勾选启用行 / 端口重复 / 本机任务占用)")

    def _stop_flag_path(self, port: int) -> Path:
        """协作停止标记文件路径(logs/live/port_<port>/stop.flag)。"""
        return PROJECT_ROOT / "logs" / "live" / f"port_{int(port)}" / "stop.flag"

    def _request_stop(self, port: int) -> bool:
        """协作式停止: 写停止标记, 由子进程看门线程调 eng.stop() 安全收尾。

        2026-10-18 v4.5.0 (P1-4): 取代直接 p.terminate()(Windows 上即 TerminateProcess
        硬杀, 会把做到一半的步骤掐断、task_end 也来不及写 → 界面卡"运行中")。
        标记写下后由 _poll_procs 监测; 若超过 STOP_GRACE_SECONDS 仍不退出再硬杀兜底。
        返回 True=已发出停止信号; False=该端口没有运行的子进程。
        """
        info = self._procs.get(int(port))
        if not info:
            return False
        p = info["p"]
        if p.poll() is None:
            fp = self._stop_flag_path(port)
            try:
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(f"{time.time()}", encoding="utf-8")
            except OSError:
                pass
            info["stop_req"] = time.time()
            self._set_row(info["row"], "停止中")
        return True

    def stop_port(self, port: int) -> bool:
        """停掉指定端口的子进程(2026-09-28: 供主界面「停止」按钮按当前账号调用)。

        2026-10-18 v4.5.0 (P1-4): 改为协作式停止(写标记, 子进程自行收尾)。
        真正的进程退出与状态收口由 _poll_procs 统一处理。
        返回 True=已发出停止请求; False=该端口没有子进程在跑。
        """
        info = self._procs.get(int(port))
        if not info:
            return False
        if info["p"].poll() is None:
            self._request_stop(port)
        else:
            self._set_row(info["row"], "停止中")
        return True

    def terminate_port(self, port: int) -> bool:
        """硬杀指定端口子进程(仅用于孤儿回收兜底, 见 main_window._poll_live 的 P0-3 路径)。

        不立即除名 —— 让 _poll_procs 在进程真正退出后再收口状态, 避免状态闪烁。
        返回 True=已发出硬杀; False=该端口没有子进程在跑。
        """
        info = self._procs.get(int(port))
        if not info:
            return False
        p = info["p"]
        if p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass
        return True

    def _on_stop_all(self):
        # 2026-10-18 v4.5.0 (P1-4): 改为协作式停止 —— 逐个写停止标记, 子进程自行收尾,
        # 行先显示「停止中」; 真正的退出与状态收口由 _poll_procs 在进程结束后统一处理
        # (不再在这里立刻 terminate + 除名, 否则又回到硬杀卡"运行中"的老路)。
        n = 0
        for port, info in list(self._procs.items()):
            p = info["p"]
            if p.poll() is None:
                self._request_stop(port)
                n += 1
            else:
                self._set_row(info["row"], "已停止")
        if n:
            self.mainwin.append_sys_log(f"[多开] 已向 {n} 个账号发送停止信号(收尾中)")
        self.lbl_state.setText(f"已发送停止信号 {n} 个账号, 收尾中…")

    # ================= 状态维护 =================
    def _set_row(self, row: int, text: str):
        if 0 <= row < self.tbl.rowCount():
            it = self.tbl.item(row, 5)
            if it is None:
                it = QTableWidgetItem()
                self.tbl.setItem(row, 5, it)
            it.setText(text)

    def _poll_procs(self):
        """周期刷新运行中账号的状态列(成功/失败/设备错误/运行N分钟)"""
        done = []
        for port, info in list(self._procs.items()):
            p = info["p"]
            code = p.poll()
            if code is None:
                mins = int((time.time() - info["start"]) / 60)
                self._set_row(info["row"], f"运行 {mins} 分")
                # 2026-10-18 v4.5.0 (P1-4): 协作停止超时兜底。标记写下超过阈值
                # 进程仍不退出(主线程卡死)→ 强制硬杀, 不让界面卡在「停止中」。
                sr = info.get("stop_req")
                if sr and (time.time() - sr) > STOP_GRACE_SECONDS:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                    self._set_row(info["row"], "停止中(强制)")
                continue
            row = info["row"]
            # 清理停止标记, 避免下一次函数同一端口被秒停
            try:
                self._stop_flag_path(port).unlink()
            except OSError:
                pass
            # 2026-10-18 v4.5.0 (P1-4): 用户主动停止(含被兜底硬杀)统一显示「已停止」,
            # 不再误显示成「失败」; 退出码 3 = 已停止(区分于 失败 1 / 设备错误 2)。
            if info.get("stop_req"):
                self._set_row(row, "已停止")
            elif code == 0:
                self._set_row(row, "✅ 成功")
            elif code == 1:
                self._set_row(row, "失败")
            elif code == 2:
                self._set_row(row, "设备错误")
            else:
                self._set_row(row, f"退出({code})")
            done.append(port)
        for port in done:
            info = self._procs.pop(port)
            try:
                self.mainwin.on_child_finished(port)
            except Exception:
                pass
            self.mainwin.append_sys_log(
                f"[多开] 端口{port} 子进程结束, 详情见 logs/run_{port}.log")
        if self._procs:
            self.lbl_state.setText(f"运行中 {len(self._procs)} 个账号…")
        elif done:
            self.lbl_state.setText("全部账号任务已结束")

    def shutdown(self):
        """窗口关闭时回收子进程, 避免孤儿进程继续操作模拟器"""
        if self._procs:
            for port, info in list(self._procs.items()):
                p = info["p"]
                if p.poll() is None:
                    try:
                        p.terminate()
                    except Exception:
                        pass
            self._procs.clear()
