# -*- coding: utf-8 -*-
"""崩溃诊断 (2026-10-16, v5.2.0 大幅增强 2026-10-19)

背景: v3 用 pythonw.exe 启动, stderr 被丢弃, 且原先没有 faulthandler /
sys.excepthook / threading.excepthook。结果: 任何原生崩溃(segfault)或主线程
未捕获异常都会"静默消失" —— 本次 v3 闪退正是这样: run.log 戛然而止、无 traceback、
Windows 事件日志也无对应崩溃条目。

v5.2.0 增强(2026-10-19 11:48 事故: access violation, crash.log 只有
"Thread 0x... (most recent call first)" + "<no Python frame>" —— 崩在
Qt/onnxruntime/cv2 这类**没有 Python 帧的原生线程**里, 完全无法定位):

  note(text)                 面包屑: 把"当前正在做什么"实时追加到
                             logs/last_action.log。崩溃后最后一行就是死前动作。
  dump_threads()             把**所有线程**的名字 + Python 栈写进 logs/threads.log
                             (faulthandler 自己的转储里线程只有 0x... 地址, 没有名字)
  start_thread_watchdog()    每 N 秒自动 dump 一次线程快照(滚动保留最近几份) ——
                             原生崩溃时 faulthandler 打不出栈, 靠崩溃**前**最后一份
                             快照反推"那会儿谁在跑"。
  _install_minidump_hook()   进程内 MiniDumpWriteDump: 原生崩溃时自动落
                             logs/crash_<时间>.dmp。**不需要管理员权限**,
                             也不需要配 WER LocalDumps(那要写 HKLM)。
"""
from __future__ import annotations

import os
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path

_LOG_PATH = None
_FH = None          # faulthandler 的常驻文件句柄(退出时要关, 见 close_crash_handle)
_NOTE_FH = None     # 面包屑文件句柄(行缓冲, 崩溃时内容已在磁盘上)
_NOTE_WRITES = 0
_NOTE_MAX = 768 * 1024      # 面包屑单文件上限, 超了就地清空重来
_DUMP_KEEP = 2              # 保留最近几份 .dmp(每份约 1~5MB)
_MINIDUMP_OK = False        # SetUnhandledExceptionFilter 是否挂上了
_DUMP_WRITTEN = False       # 已经写过 dmp?(崩溃回调可能被调多次)
_SUFFIX = ""                # 文件名后缀 —— 多开时子进程用 `_p16384`, 免得互相踩


def set_suffix(suffix: str):
    """给所有诊断文件加后缀(必须在 install_crash_guard 之前调)

    多开管理里的每个 run_one 都是独立进程、都往同一个 logs/ 写, 不加后缀
    就是几个进程交叉写同一个 crash.log, 出事后完全分不清是谁崩的。
    """
    global _SUFFIX, _LOG_PATH
    _SUFFIX = str(suffix or "")
    _LOG_PATH = None


def _log_dir() -> Path:
    try:
        p = Path(__file__).resolve().parent / "logs"
    except Exception:
        p = Path("logs")
    try:
        p.mkdir(exist_ok=True)
    except Exception:
        pass
    return p


def _log_path() -> Path:
    global _LOG_PATH
    if _LOG_PATH is None:
        _LOG_PATH = _log_dir() / f"crash{_SUFFIX}.log"
    return _LOG_PATH


def _note_path() -> Path:
    return _log_dir() / f"last_action{_SUFFIX}.log"


def write_crash(title: str, text: str = ""):
    """把一段崩溃/异常信息追加到 logs/crash.log (pythonw 下 stderr 不可见时用)"""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {title}\n")
            if text:
                f.write(text if text.endswith("\n") else text + "\n")
            f.write("-" * 60 + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 面包屑
def note(text: str):
    """记录"当前正在做什么" —— 崩溃后 last_action.log 的最后一行就是死前动作

    设计要点(2026-10-19):
      · 行缓冲 + 每行带毫秒时间戳, 崩溃时不需要额外 flush 就能落盘;
      · 调用点只在**有意义的动作**上(截图/OCR/点击/步骤切换), 每步几次,
        实测单次 < 0.1ms, 不构成性能问题;
      · 单文件超 768KB 就清空重来 —— 它只是"最近干了什么", 历史价值低但
        crash.log / run.log 有, 不必留档。
    """
    global _NOTE_FH, _NOTE_WRITES
    try:
        if _NOTE_FH is None:
            _NOTE_FH = open(_note_path(), "a", encoding="utf-8", buffering=1)
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        _NOTE_FH.write(f"[{ts}] {str(text)[:300]}\n")
        _NOTE_WRITES += 1
        if _NOTE_WRITES % 200 == 0:
            try:
                if _NOTE_FH.tell() > _NOTE_MAX:
                    _NOTE_FH.close()
                    _NOTE_FH = open(_note_path(), "w", encoding="utf-8", buffering=1)
                    _NOTE_FH.write(f"[{ts}] --- (文件达上限, 已滚动) ---\n")
            except Exception:
                pass
    except Exception:
        pass


def last_actions(n: int = 40) -> list[str]:
    """读最近 n 条面包屑(诊断包用)"""
    try:
        with open(_note_path(), "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [ln.rstrip("\n") for ln in lines[-n:]]
    except Exception:
        return []


# ---------------------------------------------------------------- 线程快照
def dump_threads(tag: str = "") -> bool:
    """把全部线程的名字 + Python 栈写进 logs/threads.log

    为什么需要它: faulthandler 崩溃转储里线程只显示 `Thread 0x0000737c`,
    没有任何业务含义; 而且崩在**原生线程**时是 `<no Python frame>`, 等于什么都没说。
    这里额外落"线程名 + 每个线程的栈", 配合定期快照就能看出崩溃那会儿在跑什么。
    """
    try:
        import faulthandler
        path = _log_dir() / f"threads{_SUFFIX}.log"
        try:
            if path.exists() and path.stat().st_size > 2 * 1024 * 1024:
                path = _log_dir() / f"threads{_SUFFIX}.log"
                path.unlink(missing_ok=True)
        except Exception:
            pass
        with open(path, "a", encoding="utf-8", buffering=1) as f:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            f.write(f"\n===== [{ts}] {tag or 'snapshot'} =====\n")
            # 线程清单(名字/daemon/是否存活) —— faulthandler 不会给这些
            for t in threading.enumerate():
                f.write(f"  - {t.name!r} daemon={t.daemon} alive={t.is_alive()}\n")
            f.write("---- Python stacks ----\n")
            faulthandler.dump_traceback(file=f, all_threads=True)
        return True
    except Exception:
        return False


def _write_modules_log():
    """把模块快照落一份到 logs/modules.log(覆盖写, 保留最近一次)"""
    try:
        path = _log_dir() / f"modules{_SUFFIX}.log"
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        path.write_text(f"# {ts}  pid={os.getpid()}  模块数={len(_MODULES)}\n"
                        f"# base             size      path\n"
                        f"{module_snapshot_text()}\n", encoding="utf-8")
    except Exception:
        pass


_watchdog_started = False


def start_thread_watchdog(interval: float = 30.0, keep_seconds: float = 600.0):
    """起一个守护线程, 每 interval 秒 dump 一次线程快照(默认 30s)

    原生崩溃(access violation)时 faulthandler 打不出 Python 栈, 唯一能用的证据
    就是**崩之前最后一份快照**。保留最近 keep_seconds 秒的量, 更多的不必留。
    """
    global _watchdog_started
    if _watchdog_started:
        return
    _watchdog_started = True

    def _loop():
        import time
        rounds = 0
        while True:
            try:
                time.sleep(max(5.0, float(interval)))
                rounds += 1
                # 模块表必须定期刷新: Qt 插件 / cv2 / onnxruntime 的 dll 是运行中
                # 才加载的, 启动时那份快照里没有它们 —— 而崩溃往往就在这些库里。
                refresh_modules()
                dump_threads("watchdog")
                _trim_threads_log(keep_seconds, interval)
                if rounds % max(1, int(300 / max(interval, 1))) == 0:
                    _write_modules_log()
            except Exception:
                pass

    t = threading.Thread(target=_loop, name="crash-watchdog", daemon=True)
    t.start()


def _trim_threads_log(keep_seconds: float, interval: float):
    """把 threads.log 裁到最近 keep_seconds 秒(按 '===== [时间] ' 分段)"""
    try:
        path = _log_dir() / f"threads{_SUFFIX}.log"
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8", errors="replace")
        marks = [i for i in range(len(text)) if text.startswith("===== [", i)]
        limit = max(2, int(keep_seconds / max(interval, 1)) + 2)
        if len(marks) <= limit:
            return
        cut = marks[len(marks) - limit]
        path.write_text(text[cut:], encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 原生崩溃 minidump
# 值得落 dmp 的"真崩溃"异常码。其余异常(如 Python 里常见的探测性访问)一律不管,
# 免得每次都被写一份 dump。0xC0000005 也是 ctypes 边界上会出现的那一个 ——
# 本项目生产路径不用"预期 AV", 所以这里可以接受偶发误报(写一次就够, 见 _DUMP_WRITTEN)。
_FATAL_CODES = {
    0xC0000005,   # ACCESS_VIOLATION
    0xC000001D,   # ILLEGAL_INSTRUCTION
    0xC00000FD,   # STACK_OVERFLOW
    0xC0000374,   # HEAP_CORRUPTION
    0xC0000409,   # STACK_BUFFER_OVERRUN / fast-fail
    0xC0000006,   # IN_PAGE_ERROR
}


def _install_minidump_hook() -> bool:
    """进程内落 .dmp —— 不需要管理员, 不需要配 WER LocalDumps

    两层:
      · AddVectoredExceptionHandler(First=1): 第一机会, **插到异常链最前面**。
        必须这样 —— 实测(2026-10-19) faulthandler 在 Windows 上是 vectored
        handler 且处理完直接终止进程, SetUnhandledExceptionFilter 根本不会被调用,
        所以"只挂 unhandled filter"的方案在真崩时一个 dmp 都拿不到。
      · SetUnhandledExceptionFilter: 兜底(万一上面那层没被调/提前 return)。

    回调只做三件事: 读异常码 → 写 crash.log 一行 → MiniDumpWriteDump。
    写完返回 EXCEPTION_CONTINUE_SEARCH, 不改变后续处理(进程该死还是死)。
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        try:
            dbghelp = ctypes.WinDLL("dbghelp", use_last_error=True)
        except OSError:
            return False

        class MINIDUMP_EXCEPTION_INFORMATION(ctypes.Structure):
            _fields_ = [("ThreadId", wintypes.DWORD),
                        ("ExceptionPointers", ctypes.c_void_p),
                        ("ClientPointers", wintypes.BOOL)]

        # 轻量组合: 基础 + 数据段 + 句柄 + 已卸载模块 + 线程信息 + 内存布局
        # 故意不用 MiniDumpWithFullMemory(2) —— 那会写出几百 MB, 用户不会传回来。
        DUMP_TYPE = 0x1 | 0x4 | 0x20 | 0x100 | 0x1000 | 0x2000

        dbghelp.MiniDumpWriteDump.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        dbghelp.MiniDumpWriteDump.restype = wintypes.BOOL
        k32.SetUnhandledExceptionFilter.argtypes = [ctypes.c_void_p]
        k32.SetUnhandledExceptionFilter.restype = ctypes.c_void_p
        k32.AddVectoredExceptionHandler.argtypes = [wintypes.ULONG, ctypes.c_void_p]
        k32.AddVectoredExceptionHandler.restype = ctypes.c_void_p

        def _on_exception(exc_ptrs):
            global _DUMP_WRITTEN
            try:
                code, addr = _read_exc(exc_ptrs)
                if code not in _FATAL_CODES:
                    return 0        # EXCEPTION_CONTINUE_SEARCH, 不插手
                if _DUMP_WRITTEN:
                    return 0
                _DUMP_WRITTEN = True
                # ★ 关键: 用**崩溃前缓存的模块表**把地址归因到具体 DLL。
                # 这一步只做减法, 不在异常上下文里调任何枚举 API —— 稳。
                where = ""
                try:
                    hit = _attribute_addr(addr)
                    if hit:
                        where = f"\n归因: {hit[0]} +0x{hit[2]:X} (基址 0x{hit[1]:016X})"
                    else:
                        where = "\n归因: 不在任何已加载模块内(已卸载 DLL / JIT / 堆内存)"
                except Exception:
                    pass
                try:
                    write_crash(
                        "FATAL NATIVE EXCEPTION",
                        f"code=0x{code:08X} address=0x{addr:016X} "
                        f"(崩溃线程 id={_current_tid()}){where}")
                except Exception:
                    pass
                _write_dump(ctypes, wintypes, k32, dbghelp,
                            MINIDUMP_EXCEPTION_INFORMATION, exc_ptrs, DUMP_TYPE)
            except Exception:
                pass
            return 0                # 交回后续处理(faulthandler/系统照样收尾)

        # 回调必须被模块级变量持住, 否则被 GC 后 Windows 会跳进野指针
        global _MINIDUMP_CB
        _MINIDUMP_CB = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)(_on_exception)
        cb_ptr = ctypes.cast(_MINIDUMP_CB, ctypes.c_void_p)
        k32.AddVectoredExceptionHandler(1, cb_ptr)
        k32.SetUnhandledExceptionFilter(cb_ptr)
        global _MINIDUMP_OK
        _MINIDUMP_OK = True
        return True
    except Exception:
        return False


_MINIDUMP_CB = None

# 已加载模块的地址范围快照 (base, size, path)。
# ★ 为什么在**崩溃前**就缓存: 崩溃时再去 EnumProcessModules/MiniDumpWriteDump
#   属于"在异常上下文里做重活", 实测会重入并失败(2026-10-19: dmp 只有 0 字节,
#   GetLastError 0x26)。而"崩溃地址落在哪个 DLL"这件事, 只要有一份模块基址表
#   就能算出来 —— 这份表由 watchdog 定期刷新, 崩溃回调里只做一次减法。
_MODULES: list[tuple[int, int, str]] = []


def refresh_modules() -> int:
    """刷新已加载模块快照(pid 内所有 dll/exe)。返回模块数。"""
    global _MODULES
    if sys.platform != "win32":
        return 0
    try:
        import ctypes
        from ctypes import wintypes

        class MODULEINFO(ctypes.Structure):
            _fields_ = [("lpBaseOfDll", ctypes.c_void_p),
                        ("SizeOfImage", wintypes.DWORD),
                        ("EntryPoint", ctypes.c_void_p)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        hproc = k32.GetCurrentProcess()
        MAXN = 512
        arr = (ctypes.c_void_p * MAXN)()
        needed = wintypes.DWORD()
        # LIST_MODULES_ALL = 3
        ok = psapi.EnumProcessModulesEx(ctypes.c_void_p(hproc), arr, ctypes.sizeof(arr),
                                        ctypes.byref(needed), 3)
        if not ok:
            return 0
        n = min(MAXN, int(needed.value) // ctypes.sizeof(ctypes.c_void_p))
        out = []
        buf = ctypes.create_unicode_buffer(512)
        for i in range(n):
            hmod = arr[i]
            mi = MODULEINFO()
            if not psapi.GetModuleInformation(ctypes.c_void_p(hproc), ctypes.c_void_p(hmod),
                                              ctypes.byref(mi), ctypes.sizeof(mi)):
                continue
            buf.value = ""
            psapi.GetModuleFileNameExW(ctypes.c_void_p(hproc), ctypes.c_void_p(hmod), buf, 512)
            base = int(mi.lpBaseOfDll or 0)
            out.append((base, int(mi.SizeOfImage), buf.value or "?"))
        _MODULES = out
        return len(out)
    except Exception:
        return 0


def _attribute_addr(addr: int):
    """把崩溃地址归因到模块 → (模块名, 基址, 偏移) 或 None"""
    try:
        for base, size, name in _MODULES:
            if base and base <= addr < base + max(size, 1):
                return name, base, addr - base
    except Exception:
        pass
    return None


def module_snapshot_text(limit: int = 400) -> str:
    """模块快照文本(诊断包 / modules.log 用)"""
    lines = [f"{base:016X}  {size:08X}  {name}"
             for base, size, name in _MODULES[:limit]]
    return "\n".join(lines)


def _current_tid() -> int:
    """当前线程的系统 tid —— 崩溃回调里不能用 threading.current_thread()"""
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetCurrentThreadId())
    except Exception:
        return -1


def _read_exc(exc_ptrs):
    """从 EXCEPTION_POINTERS 里读 (异常码, 异常地址) —— 只用 ctypes 读内存, 不依赖 Python"""
    import ctypes
    from ctypes import wintypes

    class EXCEPTION_RECORD(ctypes.Structure):
        _fields_ = [("ExceptionCode", wintypes.DWORD),
                    ("ExceptionFlags", wintypes.DWORD),
                    ("ExceptionRecord", ctypes.c_void_p),
                    ("ExceptionAddress", ctypes.c_void_p),
                    ("NumberParameters", wintypes.DWORD),
                    ("ExceptionInformation", ctypes.c_void_p * 15)]

    class EXCEPTION_POINTERS(ctypes.Structure):
        _fields_ = [("ExceptionRecord", ctypes.POINTER(EXCEPTION_RECORD)),
                    ("ContextRecord", ctypes.c_void_p)]

    if not exc_ptrs:
        return 0, 0
    p = ctypes.cast(exc_ptrs, ctypes.POINTER(EXCEPTION_POINTERS)).contents
    if not p.ExceptionRecord:
        return 0, 0
    rec = p.ExceptionRecord.contents
    return int(rec.ExceptionCode), int(rec.ExceptionAddress or 0)


def _write_dump(ctypes, wintypes, k32, dbghelp, exc_info_cls, exc_ptrs, dump_type):
    _cleanup_old_dumps(_DUMP_KEEP - 1)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = _log_dir() / f"crash{_SUFFIX}_{ts}.dmp"

    # ⚠ GetCurrentProcess() 返回伪句柄 (HANDLE)-1。若把 restype 设成 c_void_p,
    #   ctypes 会把它转成 None 传给 MiniDumpWriteDump → 参数无效 → 只留下一个
    #   **0 字节的 .dmp**(2026-10-19 实测踩到)。这里保持默认 c_int(-1) 并让
    #   argtypes 以 c_void_p 接收(-1 会被当成 0xFFFF...FF, 正是伪句柄)。
    k32.GetCurrentProcess.restype = ctypes.c_void_p
    k32.GetCurrentProcessId.restype = wintypes.DWORD
    hproc = k32.GetCurrentProcess()
    pid = int(k32.GetCurrentProcessId())

    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.CreateFileW.restype = ctypes.c_void_p
    GENERIC_WRITE = 0x40000000
    CREATE_ALWAYS = 2
    FILE_ATTRIBUTE_NORMAL = 0x80
    hfile = k32.CreateFileW(str(path), GENERIC_WRITE, 0, None,
                            CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
    if not hfile or hfile == 0xFFFFFFFFFFFFFFFF:
        return
    info = exc_info_cls()
    info.ThreadId = int(k32.GetCurrentThreadId())
    info.ExceptionPointers = exc_ptrs
    info.ClientPointers = False
    ok = dbghelp.MiniDumpWriteDump(ctypes.c_void_p(hproc), pid, ctypes.c_void_p(hfile),
                                   dump_type, ctypes.byref(info), None, None)
    err = 0
    if not ok:
        try:
            err = int(ctypes.get_last_error())
        except Exception:
            err = 0
    try:
        k32.CloseHandle(ctypes.c_void_p(hfile))
    except Exception:
        pass
    if ok:
        write_crash("MINIDUMP WRITTEN", f"{path}\n"
                    "用 runtime\\python.exe scripts\\analyze_dump.py <dmp> 可解析异常码与故障模块")
    else:
        # 0 字节文件只是垃圾, 删掉 —— 归因信息已经在 crash.log 里了
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass
        write_crash("MINIDUMP FAILED",
                    f"MiniDumpWriteDump 返回 0, GetLastError={err} "
                    f"(0x{err & 0xFFFFFFFF:X})\n"
                    "不影响诊断: 上面那行『归因』已经指出故障 DLL 与偏移。"
                    "dmp 在异常上下文里写会重入失败, 属已知限制。")


def _cleanup_old_dumps(keep: int):
    try:
        dumps = sorted(_log_dir().glob(f"crash{_SUFFIX}_*.dmp"), key=lambda p: p.stat().st_mtime)
        for p in dumps[:max(0, len(dumps) - keep)]:
            try:
                p.unlink()
            except Exception:
                pass
    except Exception:
        pass


def minidump_enabled() -> bool:
    return bool(_MINIDUMP_OK)


# ---------------------------------------------------------------- 安装
def close_crash_handle():
    """关闭 faulthandler / 面包屑常驻持有的句柄(退出收尾用)

    2026-10-17 v3.5.0(P1-4): `faulthandler.enable(file=fh)` 会一直握着这个句柄
    到进程结束。平时无害, 但本程序退出时要保证 logs 目录能被删掉/移动
    (用户反馈过"文件夹删不掉"), 所以显式收一下。
    ⚠️ 必须先 `faulthandler.disable()` 再 close, 否则之后万一真崩了,
       faulthandler 会往一个已关闭的文件里写(它自己会吞异常, 但转储就没了 ——
       反正此刻已经在退出, 崩溃转储也没人看了)。
    """
    global _FH, _NOTE_FH
    if _FH is not None:
        try:
            import faulthandler
            faulthandler.disable()
        except Exception:
            pass
        try:
            _FH.close()
        except Exception:
            pass
        _FH = None
    if _NOTE_FH is not None:
        try:
            _NOTE_FH.close()
        except Exception:
            pass
        _NOTE_FH = None


def install_crash_guard():
    # 1) 原生崩溃 (SIGSEGV/SIGFPE/SIGABRT/SIGBUS) → 落盘到 crash.log
    #    ⚠️ 顺序很重要: faulthandler 在 Windows 上自己也会占用异常处理链,
    #    必须**先**让它装好, 再由我们挂 SetUnhandledExceptionFilter —— 否则
    #    我们的过滤器会被它覆盖, 实测(2026-10-19)就是"崩了但没有 dmp"。
    try:
        import faulthandler
        try:
            global _FH
            _FH = open(_log_path(), "a", encoding="utf-8", buffering=1)
            faulthandler.enable(file=_FH)
        except Exception:
            faulthandler.enable()   # 退回 stderr (pythonw 下不可见, 但无害)
    except Exception:
        pass

    # 2) 原生崩溃 minidump(在 faulthandler 之后挂, 保证是最后一层)
    try:
        _install_minidump_hook()
    except Exception:
        pass

    # 2) 主线程未捕获异常 → 落盘
    def _exc(etype, exc, tb):
        try:
            write_crash("UNCAUGHT EXCEPTION (main thread)",
                        "".join(traceback.format_exception(etype, exc, tb)))
            dump_threads("uncaught(main)")
        except Exception:
            pass

    if hasattr(sys, "excepthook"):
        sys.excepthook = _exc

    # 3) 子线程未捕获异常 → 落盘 (worker 线程虽已自捕获, 仍作兜底)
    def _thread_exc(args):
        try:
            write_crash(f"UNCAUGHT EXCEPTION (thread {getattr(args, 'thread', None)})",
                        "".join(traceback.format_exception(
                            args.exc_type, args.exc_value, args.exc_traceback)))
        except Exception:
            pass

    try:
        threading.excepthook = _thread_exc
    except Exception:
        pass

    # 4) 面包屑起点(崩溃后至少知道进程什么时候起的、跑的是哪个包)
    try:
        note(f"=== 进程启动 pid={os.getpid()} argv={sys.argv} ===")
    except Exception:
        pass

    # 5) 模块表首次快照(崩溃归因要用; watchdog 之后会定期刷新)
    try:
        refresh_modules()
        _write_modules_log()
    except Exception:
        pass
