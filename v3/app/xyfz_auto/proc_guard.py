# -*- coding: utf-8 -*-
"""进程守护: 子进程随主进程回收 + 残留进程排查/清理 (proc_guard.py)

解决的实际问题 (2026-09-28 用户反馈):
    绿色版退出后仍留有 python / pythonw / adb 进程, 占着文件夹里的文件,
    删除目录时提示"必须先结束残留进程"。

三道保险:
  1. Windows 作业对象(Job Object): 多开子进程全部绑到 GUI 进程的作业上, 并设置
     JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE —— GUI 一退出(正常关闭/崩溃/被强杀),
     系统自动结束作业内全部子进程, 不留孤儿。
  2. 退出收尾(见 gui/main_window.py 的 _full_shutdown): 停任务线程 → 关作业 →
     释放设备锁 → 收掉【本包自己的】adb server → 关闭日志文件句柄。
  3. 兜底工具 scripts/cleanup_residual.py: 已经残留了也能一条命令清干净。

注意: adb server 只在确认它是【本包 tools\\adb\\adb.exe】拉起来的时才回收,
      避免误杀 MuMu 自己那个 adb(两者都监听 5037, 但各有各的 server 进程)。
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

IS_WIN = sys.platform == "win32"

if IS_WIN:
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi
else:                                    # 非 Windows: 全部降级为 no-op
    kernel32 = None
    psapi = None

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_TERMINATE = 0x0001
ERROR_ALREADY_EXISTS = 183

# ---------------- 作业对象结构体 ----------------
if IS_WIN:
    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong)]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _EXT_LIMIT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC_LIMIT),
                    ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


def _proc_exe(pid: int) -> str:
    """按 PID 取可执行文件全路径(失败返回空串)"""
    if not IS_WIN:
        return ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
    except Exception:
        pass
    finally:
        kernel32.CloseHandle(h)
    return ""


def iter_processes() -> list[dict]:
    """枚举进程 → [{"pid", "exe", "name"}]"""
    if not IS_WIN:
        return []
    arr = (wintypes.DWORD * 8192)()
    cb = wintypes.DWORD()
    if not psapi.EnumProcesses(ctypes.byref(arr), ctypes.sizeof(arr), ctypes.byref(cb)):
        return []
    out = []
    my_pid = os.getpid()
    for i in range(cb.value // ctypes.sizeof(wintypes.DWORD)):
        pid = int(arr[i])
        if pid in (0, my_pid):
            continue
        exe = _proc_exe(pid)
        if not exe:
            continue
        out.append({"pid": pid, "exe": exe, "name": os.path.basename(exe)})
    return out


def processes_under(*roots) -> list[dict]:
    """列出可执行文件位于给定目录之下的进程(即"本包自己拉起来的")"""
    keys = [str(Path(r)).rstrip("\\/").lower() for r in roots if r]
    if not keys:
        return []
    hits = []
    for p in iter_processes():
        low = p["exe"].lower()
        if any(low.startswith(k + "\\") or low.startswith(k + "/") for k in keys):
            hits.append(p)
    return hits


def kill_pids(pids, *, wait: float = 2.0) -> int:
    """结束进程(TerminateProcess)。返回成功数。"""
    if not IS_WIN:
        return 0
    n = 0
    for pid in pids:
        h = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not h:
            continue
        try:
            if kernel32.TerminateProcess(h, 1):
                n += 1
        except Exception:
            pass
        finally:
            kernel32.CloseHandle(h)
    if wait:
        time.sleep(min(wait, 3.0))
    return n


# ---------------- 作业对象 ----------------
class SpawnGuard:
    """把子进程绑到作业对象上 —— 主进程退出时由系统自动回收, 杜绝孤儿进程"""

    def __init__(self, name: str | None = None):
        self.job = None
        self._procs: list = []
        self.available = False
        if not IS_WIN:
            return
        try:
            self.job = kernel32.CreateJobObjectW(None, name)
            if not self.job:
                return
            info = _EXT_LIMIT()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = kernel32.SetInformationJobObject(
                self.job, JobObjectExtendedLimitInformation,
                ctypes.byref(info), ctypes.sizeof(info))
            self.available = bool(ok)
        except Exception:
            self.job = None
            self.available = False

    def popen(self, cmd, cwd=None, stdout=None, stderr=None, creationflags=0):
        """启动子进程并纳入作业(作业不可用时退化为普通 Popen, 不影响功能)"""
        p = subprocess.Popen(cmd, cwd=cwd, stdout=stdout, stderr=stderr,
                             creationflags=creationflags)
        self._procs.append(p)
        if self.available and self.job:
            try:
                kernel32.AssignProcessToJobObject(self.job, int(p._handle))
            except Exception:
                pass
        return p

    def alive(self) -> list:
        return [p for p in self._procs if p.poll() is None]

    def terminate_all(self, *, timeout: float = 3.0) -> int:
        """先 terminate, 超时未退再 kill"""
        procs = self.alive()
        for p in procs:
            try:
                p.terminate()
            except Exception:
                pass
        t0 = time.time()
        while time.time() - t0 < timeout and any(p.poll() is None for p in procs):
            time.sleep(0.1)
        n = 0
        for p in procs:
            if p.poll() is None:
                try:
                    p.kill()
                except Exception:
                    pass
            else:
                n += 1
        return n

    def close(self):
        """关闭作业句柄 → 系统按 KILL_ON_JOB_CLOSE 补杀作业内全部进程"""
        try:
            self.terminate_all(timeout=2.0)
        except Exception:
            pass
        if self.job:
            try:
                kernel32.CloseHandle(self.job)
            except Exception:
                pass
            self.job = None

    def __repr__(self):
        return f"<SpawnGuard job={'on' if self.available else 'off'} procs={len(self.alive())}>"


# ---------------- 单实例互斥 ----------------
def single_instance_mutex(name: str = "xyfz_auto_gui_single"):
    """尝试创建命名互斥体。返回句柄; 已有实例在跑时返回 None。"""
    if not IS_WIN:
        return 1
    h = kernel32.CreateMutexW(None, False, name)
    if not h:
        return 1
    if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        try:
            kernel32.CloseHandle(h)
        except Exception:
            pass
        return None
    return h


def activate_window(title: str) -> bool:
    """把标题匹配的已有窗口带到前台(单实例时提示用户)"""
    if not IS_WIN:
        return False
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, title)
        if not hwnd:
            return False
        ctypes.windll.user32.ShowWindow(hwnd, 9)      # SW_RESTORE
        ctypes.windll.user32.SetForegroundWindow(hwnd)
        return True
    except Exception:
        return False


# ---------------- adb server ----------------
def own_adb_processes(adb_path: str | Path | None) -> list[dict]:
    """找出【本包 adb 二进制】对应的 server 进程(按 exe 全路径精确比对)"""
    if not adb_path:
        return []
    want = str(Path(adb_path)).lower()
    return [p for p in iter_processes()
            if p["name"].lower() == "adb.exe" and p["exe"].lower() == want]


def kill_own_adb_server(adb_path: str | Path | None, *, timeout: float = 6.0) -> str:
    """收掉本包 adb 拉起的 server —— 它是"删不掉文件夹"最常见的占用者。

    只在本包 adb.exe 确实有 server 进程时才动手, 不碰 MuMu 自己的 adb。
    返回人类可读的执行结果。
    """
    if not adb_path:
        return "未指定 adb, 跳过"
    p = Path(adb_path)
    if not p.is_file():
        # 配置里常写 "auto" —— 用 common.find_adb 解析成真实路径(包内自带会命中)
        try:
            from common import find_adb
            resolved = find_adb(adb_path)
        except Exception:
            resolved = None
        if not resolved:
            return f"adb 未找到({adb_path}), 跳过"
        p = Path(resolved)
    mine = own_adb_processes(p)
    if not mine:
        return "本包 adb server 未在运行, 无需处理"
    try:
        subprocess.run([str(p), "kill-server"], capture_output=True,
                       timeout=timeout, creationflags=0x08000000)
    except Exception as e:
        left = kill_pids([x["pid"] for x in own_adb_processes(p)])
        return f"kill-server 失败({e}), 已强制结束 {left} 个 adb 进程"
    time.sleep(0.5)
    left = own_adb_processes(p)
    if left:
        n = kill_pids([x["pid"] for x in left])
        return f"kill-server 后仍残留, 已强制结束 {n} 个 adb 进程"
    return "已停止本包 adb server"


def release_all_device_locks(ports=None) -> int:
    """删掉设备互斥锁文件(正常退出应主动释放, 兜底防"锁残留导致下次不启动")"""
    import tempfile
    d = Path(tempfile.gettempdir()) / "xyfz_auto_locks"
    if not d.is_dir():
        return 0
    n = 0
    for f in d.glob("lock_*.lock"):
        if ports is not None:
            try:
                if int(f.stem.split("_")[1]) not in ports:
                    continue
            except (IndexError, ValueError):
                pass
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n
