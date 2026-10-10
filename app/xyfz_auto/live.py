# -*- coding: utf-8 -*-
"""跨进程实时通道: 多开子进程 → 主 GUI 的画面/步骤/日志回传

背景 (2026-09-24):
    多开账号跑在 scripts/run_one.py 子进程里, 与主 GUI 是两个进程。
    Qt 信号不能跨进程传递, 所以此前主界面的「实时画面」只显示 config.yaml
    里那一个端口(config.device.port, 通常就是账号1)的画面, 其它账号的画面
    根本没有任何通道回到界面; 步骤也一样——子进程只写 logs/run_<port>.log
    纯文本, GUI 不读, 「实时步骤」里自然就分不清哪条属于哪个账号。

方案: 文件式实时通道(不引入 socket/HTTP, 子进程崩溃也不影响 GUI 显示)
    logs/live/port_<端口>/
        meta.json          每次运行写一次: 账号名/端口/任务/pid/起始时刻/总步数
        ev_<rid>.jsonl     追加式事件流: start / log / step_start / step_end / task_end
        fr_<rid>.jpg       最新一帧截图(先写 .tmp 再 os.replace → 读到的永远是完整帧)

    rid = "<启动时刻>_<pid>", 保证新一次运行的读位置不会与上一次混在一起。
    启动时清掉同目录内上一轮的文件, 所以每个端口目录只保留"最近一次运行"。

写方: LiveWriter (run_one.py 子进程)      读方: LiveReader (主 GUI 轮询增量读)
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import cv2

PROJECT_ROOT = Path(__file__).resolve().parent
LIVE_ROOT = PROJECT_ROOT / "logs" / "live"

# 帧落盘的限流间隔。引擎每轮识别都会回调一次截图(实测每轮 3~6s),
# 这里再兜一道, 防止某些动作密集的脚本把磁盘写爆。
FRAME_MIN_GAP = 0.4
JPEG_QUALITY = 72


def live_dir(port: int) -> Path:
    """某个端口的通道目录"""
    return LIVE_ROOT / f"port_{int(port)}"


def _atomic_write(path: Path, data: bytes):
    """先写临时文件再 replace, 避免读方看到写了一半的内容"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(data)
    try:
        os.replace(tmp, path)
    except OSError:
        # Windows 上极少数情况(读方恰好占用)会失败, 退化为直接覆盖
        try:
            path.write_bytes(data)
        except OSError:
            pass
        try:
            tmp.unlink()
        except OSError:
            pass


class LiveWriter:
    """子进程侧的写入端。非线程安全(引擎回调都在同一线程), 够用。"""

    def __init__(self, port: int, name: str = "", task: str = ""):
        self.port = int(port)
        self.name = name or f"端口{port}"
        self.task = task
        self.dir = live_dir(port)
        self.rid = f"{int(time.time())}_{os.getpid()}"
        self._ev_path = self.dir / f"ev_{self.rid}.jsonl"
        self._fr_path = self.dir / f"fr_{self.rid}.jpg"
        self._fp = None
        self._last_frame = 0.0
        self._begun = False
        self._closed = False

    # ---------- 生命周期 ----------
    def prepare(self, task: str = ""):
        """建目录 + 写 meta + 打开事件流(此时还不知道总步数, 先不写 start)"""
        if self._begun:
            return
        self._begun = True
        if task:
            self.task = task
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            self._clean_old_runs()
        except OSError:
            pass
        try:
            _atomic_write(self.dir / "meta.json", json.dumps({
                "rid": self.rid,
                "port": self.port,
                "name": self.name,
                "task": self.task,
                "pid": os.getpid(),
                "t0": time.time(),
                "ver": 1,
            }, ensure_ascii=False).encode("utf-8"))
            self._fp = open(self._ev_path, "a", encoding="utf-8", buffering=1)
        except OSError:
            self._fp = None

    def announce(self, descs: list[str] | None = None, task: str = ""):
        """写下 start 事件 + 全部步骤描述。

        descs 一次带全 —— GUI 就能像本机任务一样立刻把整份步骤列表铺出来,
        而不是等步骤一步步冒出来(某些脚本第一步是 sleep 60s, 那样界面会空白很久)。
        """
        self.prepare(task)
        if task:
            self.task = task
        self.event("start", name=self.name, task=self.task,
                   steps=list(descs or []), pid=os.getpid())

    def _clean_old_runs(self):
        """删掉上一轮运行留下的 ev_*.jsonl / fr_*.jpg(不影响正在读的旧 run)"""
        keep = {self._ev_path.name, self._fr_path.name}
        for f in self.dir.iterdir():
            try:
                if f.is_file() and f.name not in keep and (
                        f.name.startswith("ev_") or f.name.startswith("fr_")
                        or f.name.endswith(".tmp")):
                    f.unlink()
            except OSError:
                continue

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self._fp is not None:
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None

    # ---------- 事件 ----------
    def event(self, kind: str, **payload):
        if self._fp is None or self._closed:
            return
        rec = {"t": time.time(), "k": kind}
        rec.update(payload)
        try:
            self._fp.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        except (OSError, ValueError, TypeError):
            pass

    def log(self, level: str, msg: str):
        self.event("log", lv=str(level), msg=str(msg))

    def step_start(self, idx: int, desc: str):
        self.event("step_start", i=int(idx), desc=str(desc))

    def step_end(self, idx: int, name: str, ok: bool):
        self.event("step_end", i=int(idx), name=str(name), ok=bool(ok))

    def edge(self, info: str = ""):
        """刚启动、还没跑任务时的状态标记(如设备没连上)"""
        self.event("notice", msg=str(info))

    def end(self, ok: bool, info: str = ""):
        self.event("task_end", ok=bool(ok), info=str(info))
        self.close()

    # ---------- 画面 ----------
    def frame(self, bgr, force: bool = False):
        """写一帧预览图(限流)。bgr 为 OpenCV BGR numpy 数组"""
        if bgr is None or self._closed:
            return
        now = time.time()
        if not force and (now - self._last_frame) < FRAME_MIN_GAP:
            return
        self._last_frame = now
        try:
            ok, buf = cv2.imencode(".jpg", bgr,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY])
            if ok:
                _atomic_write(self._fr_path, buf.tobytes())
        except Exception:
            pass


class LiveReader:
    """主 GUI 侧的读取端: 增量读事件、按需读最新帧。"""

    def __init__(self, port: int):
        self.port = int(port)
        self.dir = live_dir(port)
        self.meta: dict = {}
        self.rid: str = ""
        self.frame_seq = 0            # 已读到的帧版本号
        self._fp = None
        self._pos = 0
        self._carry = b""
        self._frame_mtime = None
        self._ev_path: Path | None = None
        self._fr_path: Path | None = None
        self.new_run = False          # 本次 poll 发现是新一次运行

    # ---------- 打开/切换运行 ----------
    def _load_meta(self) -> bool:
        try:
            m = json.loads((self.dir / "meta.json").read_text(encoding="utf-8"))
        except Exception:
            return False
        rid = str(m.get("rid") or "")
        if not rid:
            return False
        self.meta = m
        if rid == self.rid:
            return True
        # 新的一次运行(或首次读到) → 重开事件流与帧
        self.rid = rid
        self.new_run = True
        self._close_fp()
        self._ev_path = self.dir / f"ev_{rid}.jsonl"
        self._fr_path = self.dir / f"fr_{rid}.jpg"
        self._pos = 0
        self._carry = b""
        self._frame_mtime = None
        return True

    def _close_fp(self):
        if self._fp is not None:
            try:
                self._fp.close()
            except OSError:
                pass
            self._fp = None

    def is_active(self) -> bool:
        """写方进程是否还活着。

        正常结束时子进程会回写 task_end, 状态自会更新; 但子进程被强杀/崩溃时
        不会留下任何收尾事件, 通道会永远停在"运行中" —— 那就成了假象。
        """
        pid = int(self.meta.get("pid") or 0)
        if pid <= 0:
            return False
        if os.name != "nt":
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            k.CloseHandle(h)
            return True
        except Exception:
            return True     # 查不了就别乱判成"已死"

    # ---------- 轮询 ----------
    def poll(self) -> list[dict]:
        """返回自上次 poll 以来的新事件(已按行解析)。

        _load_meta 发现 rid 变化(新一次运行)时会把 new_run 置 True,
        上层据此清空该通道的旧步骤/日志再套用新事件。
        """
        self.new_run = False
        if not self._load_meta():
            return []
        if self._ev_path is not None:
            return self._read_new_lines()
        return []

    def _read_new_lines(self) -> list[dict]:
        try:
            st = os.stat(self._ev_path)
        except OSError:
            return []
        # 文件被截断/替换 → 从头再读
        if self._fp is None or st.st_size < self._pos:
            self._close_fp()
            try:
                self._fp = open(self._ev_path, "rb")
            except OSError:
                return []
            self._pos = 0
            self._carry = b""
        if st.st_size == self._pos:
            return []
        try:
            self._fp.seek(self._pos)
            buf = self._fp.read()
            self._pos = self._fp.tell()
        except OSError:
            self._close_fp()
            return []
        out: list[dict] = []
        self._carry += buf
        parts = self._carry.split(b"\n")
        self._carry = parts.pop()          # 末段可能是不完整的行, 留到下次
        for raw in parts:
            if not raw.strip():
                continue
            try:
                out.append(json.loads(raw.decode("utf-8", "ignore")))
            except Exception:
                continue
        return out

    def read_frame(self):
        """有新帧则解码返回 BGR, 否则返回 None(靠 mtime_ns + 大小判重)"""
        if self._fr_path is None:
            return None
        try:
            st = os.stat(self._fr_path)
        except OSError:
            return None
        sig = (st.st_mtime_ns, st.st_size)
        if self._frame_mtime == sig:
            return None
        self._frame_mtime = sig
        try:
            data = self._fr_path.read_bytes()
        except OSError:
            self._frame_mtime = None
            return None
        import numpy as np
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            return None
        self.frame_seq += 1
        return img

    def close(self):
        self._close_fp()
