# -*- coding: utf-8 -*-
"""后台 live 通道轮询(画面 / 步骤 / 子进程存亡)

拆自 main_window.py(2026-10-18 v4.7.0 批次四·工程 P2-1)。
每条对自己的 QTimer, 只读 live 目录, 不碰 adb(adb 是另一个后台线程)。

依赖(由宿主 MainWindow 提供):
  · self.store / self.panel_multi / self.append_sys_log
  · 回调: self.mark_stopped / self.set_active(由 store 提供)
"""
from __future__ import annotations
from live import LIVE_ROOT, LiveReader
class LivePollMixin:
    """见模块 docstring。方法体整体搬自 main_window.py, 未作改写。"""
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
    def _heartbeat_timeout(self) -> float:
        """孤儿判定阈值(秒) —— config.yaml → engine.heartbeat_timeout, 默认 120。

        2026-10-18 v4.5.0 (P0-3): 必须 ≥ 子进程正常启动耗时(adb 连接/大任务加载/
        OCR 模型加载)且远大于心跳间隔(默认 5s), 否则会把慢启动误杀成孤儿。
        健康进程每 ≤5s 一拍, 永远远小于此值; 只有真正卡死(主线程在心跳点之外阻塞
        超过此值)才会触发强制回收。
        """
        try:
            cfg = getattr(self, "cfg", None) or {}
            eng = cfg.get("engine", {}) if isinstance(cfg, dict) else {}
            return float(eng.get("heartbeat_timeout", 120) or 120)
        except (TypeError, ValueError):
            return 120.0
    def _poll_live(self):
        """把多开子进程的 画面/步骤/日志 增量捞进通道(不阻塞界面)"""
        self._scan_tick += 1
        if self._scan_tick % 5 == 1:        # 目录扫描降频到 ~3 秒一次
            self._live_ports = set(self._discover_ports())
            for port in self._live_ports:
                if port != self._local_port and port not in self._readers:
                    self._readers[port] = LiveReader(port)
                    self._page_sig = None
        # 2026-10-17 v3.5.0: 缓存本轮扫到的端口(句柄回收要用, 见下方 rd.close())
        live_ports = getattr(self, "_live_ports", None) or set()
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
                # 2026-10-18 v4.5.0 (P1-5): 端口所有权 sanity。通道回报的端口必须与
                # 目录端口一致; 不一致说明通道目录被串用/被其它会话遗留, 不应把数据
                # 套到这个账号上(串号)。正常情况下 run_one 写的 meta 端口 == 目录端口,
                # 此分支不会触发, 仅作防御。
                if int(rd.meta.get("port") or -1) != port:
                    try:
                        self.append_sys_log(
                            f"[多开] 端口{port} 通道回报端口异常"
                            f"(meta={rd.meta.get('port')}), 疑似串号")
                    except Exception:
                        pass
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
                # 2026-10-17 v3.5.0(P1-4): 句柄回收。
                # 以前 _readers 只增不减, 每发现一个端口就开一个事件流句柄并永久
                # 持有(连退出都不关) —— 攒够了就表现为「退出后文件夹删不掉」。
                # 写方已退出**且**该端口的通道目录也不再被扫到 → 关句柄并除名。
                # 本地端口(_local_port)由本机任务自己驱动, 保留不动。
                if port != self._local_port and port not in live_ports:
                    try:
                        rd.close()
                    except Exception:
                        pass
                    self._readers.pop(port, None)
                    self._page_sig = None
            elif rd.is_orphan(self._heartbeat_timeout()):
                # 2026-10-18 v4.5.0 (P0-3 + P1-1): 进程还活着(pid 没退出)却长时间
                # 无任何推进证据(心跳/启动时刻都超阈值) → 僵尸进程(假运行中):
                # adb 冻结 / 主线程死锁 / 被其它会话遗留在后台的子进程。
                # 协作停止已无意义(主线程不响应标记), 直接让多开面板硬杀它; 进程死后
                # 下一轮 is_active() 路径会正常收口并回收读句柄。这一支让"以权威信号
                # 兜底"真正闭环 —— 事件漏读/通道卡死都不会再让界面停在「运行中」。
                ch = self.store.get(port)
                transition = (ch is not None and ch.state == "running")
                if transition:
                    # 仅在"运行中 → 停止"这一刻记一条系统日志(去重): 后台检测不必
                    # 每轮轮询(800ms)都刷屏, 一次足够。后续默默兜底杀直至进程真退出。
                    self.store.mark_stopped(port, "孤儿进程(心跳超时,已强制回收)")
                    self._steps_dirty = True
                    self._page_sig = None
                    try:
                        self.append_sys_log(
                            f"[多开] 端口{port} 子进程心跳超时(疑似卡死), 已强制回收")
                    except Exception:
                        pass
                self.store.set_active(port, False)
                # 本 GUI 自己 spawn 的子进程: 走 _procs 句柄硬杀
                try:
                    self.panel_multi.terminate_port(port)
                except Exception:
                    pass
                # 跨会话孤儿(上一代 GUI 被杀后残留、靠 live 目录被本 GUI 认回来的子
                # 进程)不在 _procs 里, terminate_port 拿不到句柄是无用功 —— 直接用
                # meta.json 里的 pid 兜底硬杀, 让"强制回收"名副其实, 也避免它永久
                # 挂在「运行中」。(仅在 is_orphan 已确认"活着却超时"后调用, 不误杀)
                try:
                    self._kill_orphan_by_pid(rd)
                except Exception:
                    pass
            img = rd.read_frame()
            if img is not None:
                self.store.set_frame(port, img)

    def _kill_orphan_by_pid(self, rd):
        """跨会话孤儿兜底硬杀: terminate_port 只认本 GUI 的 _procs 子进程; 对上一代
        GUI 被杀后残留、靠 live 目录被本 GUI 认回来的子进程(不在 _procs)无能为力。
        这里用 meta.json 里的 pid 直接 TerminateProcess, 让孤儿判据真正闭环。

        （仅在 is_orphan 已确认"进程活着却长时间无推进证据"后调用, 健康/已退出进程
        根本到不了这里, 不会误杀; pid 取自本端口通道目录的 meta, 非凭空猜测。）
        """
        try:
            pid = int(rd.meta.get("pid") or 0)
        except (TypeError, ValueError):
            return
        if pid <= 0:
            return
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x0001, False, pid)   # PROCESS_TERMINATE
            if not h:
                return
            try:
                k.TerminateProcess(h, 1)
            finally:
                k.CloseHandle(h)
        except Exception:
            # 非 Windows / 拿不到句柄: 退化为 os.kill
            try:
                import os
                os.kill(pid, 9)
            except Exception:
                pass
