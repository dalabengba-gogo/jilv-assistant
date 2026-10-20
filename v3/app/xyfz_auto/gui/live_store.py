# -*- coding: utf-8 -*-
"""实时通道缓存: 把「本机任务」与「多开子进程」统一成按账号分开的通道

为什么要这一层:
    改造前, 主界面只有一条数据流——来自本机 TaskWorker 的 Qt 信号。
    多开账号跑在子进程里, 画面和步骤根本没有回传通道, 于是:
      · 实时画面永远只有 config.device.port 那一个账号;
      · 实时步骤里混在一起也分不清属于谁;
      · 连"这条日志是哪个号打的"都要靠猜。
    现在两边都往 ChannelStore 里灌同构的事件, UI 只认通道(port), 不认来源,
    本机任务也就是 ports 里的普通一员(标记 source="local")。

Channel 生命周期:
    idle → running → ok / fail / stopped
    子进程重跑(新 rid)会 reset 该通道, 主 GUI 重启后靠 live.LiveReader
    读 meta.json 把在跑的通道重新认回来。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

# 每个通道最多保留的日志行数(子进程日志可能很密, 防止内存无限增长)
MAX_LOGS_PER_CHANNEL = 600

_STATE_TEXT = {
    "idle": "空闲",
    "running": "运行中",
    "ok": "已完成",
    "fail": "失败",
    "stopped": "已停止",
    "error": "设备错误",
}


@dataclass
class StepRec:
    """一个步骤的实时状态"""
    idx: int
    desc: str = ""
    name: str = ""
    state: str = "pending"          # pending / running / ok / fail
    t0: float | None = None
    t1: float | None = None

    @property
    def cost(self) -> float | None:
        if self.t0 and self.t1:
            return max(0.0, self.t1 - self.t0)
        return None


@dataclass
class Channel:
    """一个账号(或本机设备)的实时数据"""
    port: int
    name: str
    source: str = "child"            # child=多开子进程 / local=主GUI自己跑
    task: str = ""
    state: str = "idle"
    rid: str = ""
    steps: dict[int, StepRec] = field(default_factory=dict)
    order: list[int] = field(default_factory=list)
    logs: list[tuple[float, str, str]] = field(default_factory=list)
    log_total: int = 0               # 累计日志条数(头部裁剪后 UI 仍能算增量)
    frame: object = None             # numpy BGR 最新一帧
    frame_seq: int = 0               # 帧版本号, UI 用它判断有没有新图
    t0: float | None = None
    t1: float | None = None
    info: str = ""                   # 结束原因/提示
    seq: int = 0                     # 数据版本号(任何变更都自增)
    active: bool = False             # 子进程是否还在写

    @property
    def label(self) -> str:
        return self.name or f"端口{self.port}"

    @property
    def title(self) -> str:
        """带端口的完整名, 用于下拉/缩略图, 便于和 MuMu 多开器对号"""
        return f"{self.label} · {self.port}"

    def state_text(self) -> str:
        return _STATE_TEXT.get(self.state, self.state)

    def touch(self):
        self.seq += 1

    def reset_progress(self):
        self.steps.clear()
        self.order.clear()
        self.logs.clear()
        self.log_total = 0
        self.frame = None
        self.frame_seq = 0
        self.t0 = time.time()
        self.t1 = None
        self.info = ""
        self.state = "running"


class ChannelStore:
    """所有通道的容器。

    name_of: 端口 → 账号名 的解析回调(通常查 accounts.json),
             这样界面显示的是用户自己起的"账号2", 而不是冷冰冰的"端口16416"。
    """

    def __init__(self, name_of: Callable[[int], str] | None = None):
        self._ch: dict[int, Channel] = {}
        self._order: list[int] = []
        self._name_of = name_of

    # ---------- 基础访问 ----------
    def channels(self) -> list[Channel]:
        """按端口稳定排序。

        刻意不按"运行中优先"排 —— 账号一启动/结束就重排的话, 画面页下拉
        和缩略图墙会顺序跳动, 反而容易点错号。状态靠颜色和文字体现。
        """
        return [self._ch[p] for p in sorted(self._ch)]

    def get(self, port: int) -> Channel | None:
        return self._ch.get(int(port))

    def ensure(self, port: int, name: str = "", source: str = "",
               task: str = "") -> Channel:
        port = int(port)
        ch = self._ch.get(port)
        if ch is None:
            nm = name or (self._name_of(port) if self._name_of else "") or f"端口{port}"
            ch = Channel(port=port, name=nm, source=source or "child", task=task)
            self._ch[port] = ch
            self._order.append(port)
        else:
            if name:
                ch.name = name
            if source:
                ch.source = source
            if task:
                ch.task = task
        return ch

    def reset(self, port: int, *, descs: list[str] | None = None,
              task: str = "", name: str = "", source: str = "",
              rid: str = "") -> Channel:
        """开始新一次运行: 清空旧进度并按 descs 预填充步骤列表"""
        ch = self.ensure(port, name=name, source=source, task=task)
        if task:
            ch.task = task
        if rid:
            ch.rid = rid
        if name:
            ch.name = name
        ch.reset_progress()
        if descs:
            for i, d in enumerate(descs):
                ch.steps[i] = StepRec(idx=i, desc=str(d))
                ch.order.append(i)
        ch.touch()
        return ch

    # ---------- 事件应用 ----------
    def apply_events(self, port: int, events: list[dict]):
        for ev in events:
            self.apply_event(port, ev)

    def apply_event(self, port: int, ev: dict):
        """把一条通道事件落到对应 Channel 上"""
        if not isinstance(ev, dict):
            return
        k = str(ev.get("k") or "")
        try:
            t = float(ev.get("t") or time.time())
        except (TypeError, ValueError):
            t = time.time()

        if k == "start":
            self.reset(port,
                       descs=ev.get("steps"),
                       task=str(ev.get("task") or ""),
                       name=str(ev.get("name") or ""),
                       source="child")
            return

        ch = self.ensure(port)
        if k == "log":
            self._push_log(ch, t, str(ev.get("lv") or "info"), str(ev.get("msg") or ""))
        elif k == "step_start":
            idx = self._as_int(ev.get("i"))
            if idx is None:
                return
            sr = ch.steps.get(idx) or StepRec(idx=idx)
            if ev.get("desc"):
                sr.desc = str(ev["desc"])
            sr.state = "running"
            sr.t0 = t
            ch.steps[idx] = sr
            if idx not in ch.order:
                ch.order.append(idx)
                ch.order.sort()
            ch.state = "running"
            ch.touch()
        elif k == "step_end":
            idx = self._as_int(ev.get("i"))
            if idx is None:
                return
            sr = ch.steps.get(idx) or StepRec(idx=idx)
            if ev.get("name"):
                sr.name = str(ev["name"])
            sr.state = "ok" if ev.get("ok") else "fail"
            sr.t1 = t
            ch.steps[idx] = sr
            if idx not in ch.order:
                ch.order.append(idx)
                ch.order.sort()
            ch.touch()
        elif k == "task_end":
            ok = bool(ev.get("ok"))
            ch.state = "ok" if ok else "fail"
            ch.t1 = t
            ch.info = str(ev.get("info") or "")
            ch.touch()
        elif k == "notice":
            self._push_log(ch, t, "warning", str(ev.get("msg") or ""))
        else:
            return

    def _push_log(self, ch: Channel, t: float, level: str, msg: str):
        ch.logs.append((t, level, msg))
        ch.log_total += 1
        if len(ch.logs) > MAX_LOGS_PER_CHANNEL:
            del ch.logs[:len(ch.logs) - MAX_LOGS_PER_CHANNEL]
        ch.touch()

    # ---------- 本机通道(主GUI 自己跑的任务) ----------
    def local_start(self, port: int, descs: list[str], task: str = ""):
        ch = self.reset(port, descs=descs, task=task, source="local")
        ch.state = "running"
        ch.touch()
        return ch

    def push_log(self, port: int, level: str, msg: str):
        ch = self.ensure(port, source="local")
        self._push_log(ch, time.time(), level, msg)

    def step_start(self, port: int, idx: int, desc: str):
        self.apply_event(port, {"k": "step_start", "i": idx, "desc": desc})

    def step_end(self, port: int, idx: int, name: str, ok: bool):
        self.apply_event(port, {"k": "step_end", "i": idx, "name": name, "ok": ok})

    def finish(self, port: int, ok: bool, info: str = ""):
        self.apply_event(port, {"k": "task_end", "ok": ok, "info": info})

    def set_frame(self, port: int, bgr):
        if bgr is None:
            return
        ch = self.ensure(port)
        ch.frame = bgr
        ch.frame_seq += 1
        ch.touch()

    def mark_stopped(self, port: int, info: str = "已手动停止"):
        ch = self.get(port)
        if ch is None:
            return
        if ch.state == "running":
            ch.state = "stopped"
        ch.info = info
        ch.t1 = time.time()
        ch.touch()

    def set_active(self, port: int, active: bool):
        ch = self.get(port)
        if ch is not None and ch.active != active:
            ch.active = active
            ch.touch()

    def remove(self, port: int):
        port = int(port)
        self._ch.pop(port, None)
        if port in self._order:
            self._order.remove(port)

    # ---------- 工具 ----------
    @staticmethod
    def _as_int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
