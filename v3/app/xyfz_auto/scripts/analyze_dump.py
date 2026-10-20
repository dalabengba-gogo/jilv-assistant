# -*- coding: utf-8 -*-
"""离线解析 minidump —— 定位原生崩溃的故障模块(2026-10-19 v5.2.0)

为什么需要它: 2026-10-19 11:48 GUI 崩在 access violation, crash.log 只有
  Windows fatal exception: access violation
  Thread 0x0000737c (most recent call first)
  <no Python frame>
—— 崩在 Qt/onnxruntime/cv2 这类**原生线程**里, faulthandler 打不出栈, 连是哪个
模块都没法说。crash_guard 现在会在原生崩溃时用 MiniDumpWriteDump 落
logs/crash_<时间>.dmp, 本脚本读它: 异常码 + 崩溃地址落在**哪个 DLL** 的哪个偏移。

用法:
  python scripts/analyze_dump.py                 # 自动找 logs/ 下最新的 dmp
  python scripts/analyze_dump.py <a.dmp> [b.dmp]  # 指定文件
  python scripts/analyze_dump.py --all            # 列出全部 dmp 的摘要

只依赖标准库(struct), 不需要 WinDbg / Windows SDK。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

# ---- minidump 常量 -------------------------------------------------------
MDMP_SIGNATURE = 0x504D444D          # b"MDMP"
STREAM_THREAD_LIST = 3
STREAM_MODULE_LIST = 4
STREAM_EXCEPTION = 6
STREAM_SYSTEM_INFO = 7

EXCEPTION_NAMES = {
    0xC0000005: "ACCESS_VIOLATION (访问违例)",
    0xC0000006: "IN_PAGE_ERROR",
    0xC000001D: "ILLEGAL_INSTRUCTION",
    0xC0000094: "INTEGER_DIVIDE_BY_ZERO",
    0xC0000096: "PRIV_INSTRUCTION",
    0xC00000FD: "STACK_OVERFLOW (栈溢出)",
    0xC0000135: "DLL_NOT_FOUND",
    0xC0000139: "ENTRYPOINT_NOT_FOUND",
    0xC0000374: "HEAP_CORRUPTION (堆损坏)",
    0xC0000409: "STACK_BUFFER_OVERRUN (快速失败)",
    0x80000003: "BREAKPOINT",
    0xE06D7363: "C++ EXCEPTION",
}


def _u32(buf, off):
    return struct.unpack_from("<I", buf, off)[0]


def _u64(buf, off):
    return struct.unpack_from("<Q", buf, off)[0]


def _mdstring(buf, rva):
    """MINIDUMP_STRING: ULONG32 字节长度 + UTF-16LE 字符"""
    if not rva or rva + 4 > len(buf):
        return ""
    n = _u32(buf, rva)
    raw = buf[rva + 4: rva + 4 + n]
    try:
        return raw.decode("utf-16-le", "replace").rstrip("\x00")
    except Exception:
        return ""


def parse_dump(path: Path) -> dict:
    buf = path.read_bytes()
    if len(buf) < 32 or _u32(buf, 0) != MDMP_SIGNATURE:
        raise ValueError(f"{path} 不是 minidump(MDMP 头不匹配)")
    n_streams = _u32(buf, 8)
    dir_rva = _u32(buf, 12)

    streams = {}
    for i in range(n_streams):
        off = dir_rva + i * 12
        if off + 12 > len(buf):
            break
        stype = _u32(buf, off)
        size = _u32(buf, off + 4)
        rva = _u32(buf, off + 8)
        streams[stype] = (rva, size)

    out = {"path": str(path), "size": len(buf)}

    # ---- 异常 ----
    exc = None
    if STREAM_EXCEPTION in streams:
        rva = streams[STREAM_EXCEPTION][0]
        # ThreadId(4) + alignment(4) + MINIDUMP_EXCEPTION(152) + ThreadContext(8)
        code = _u32(buf, rva + 8)
        eflags = _u32(buf, rva + 12)
        addr = _u64(buf, rva + 24)
        nparam = _u32(buf, rva + 32)
        params = [_u64(buf, rva + 40 + 8 * k) for k in range(min(nparam, 15))]
        exc = {"code": code, "flags": eflags, "address": addr, "params": params,
               "thread_id": _u32(buf, rva)}
        out["exception"] = exc

    # ---- 模块 ----
    modules = []
    if STREAM_MODULE_LIST in streams:
        rva = streams[STREAM_MODULE_LIST][0]
        n = _u32(buf, rva)
        base_off = rva + 4
        for i in range(n):
            off = base_off + i * 108
            if off + 108 > len(buf):
                break
            base = _u64(buf, off)
            size = _u32(buf, off + 8)
            name_rva = _u32(buf, off + 20)
            modules.append({"base": base, "size": size,
                            "name": _mdstring(buf, name_rva)})
    out["modules"] = modules

    # ---- 归因: 崩溃地址落在哪个模块 ----
    culprit = None
    if exc:
        a = exc["address"]
        for m in modules:
            if m["base"] and m["base"] <= a < m["base"] + max(m["size"], 1):
                culprit = dict(m)
                culprit["offset"] = a - m["base"]
                break
        if culprit is None:
            # 落在模块之间的空隙 → 可能是已卸载模块/JIT/堆; 给最近的几个
            near = sorted(modules, key=lambda m: abs(m["base"] - a))[:5]
            out["nearest"] = near
    out["culprit"] = culprit

    # ---- 线程数(粗略: 只报数量, 名字在 minidump 里没有) ----
    if STREAM_THREAD_LIST in streams:
        out["thread_count"] = _u32(buf, streams[STREAM_THREAD_LIST][0])
    return out


def describe(info: dict) -> str:
    lines = [f"文件: {info['path']}  ({info['size'] / 1024 / 1024:.1f} MB)"]
    exc = info.get("exception")
    if not exc:
        lines.append("⚠ 没有异常流 —— 这个 dump 不是崩溃时写的(可能是手动/挂起转储)")
    else:
        name = EXCEPTION_NAMES.get(exc["code"], f"未知异常 0x{exc['code']:08X}")
        lines.append(f"异常: {name}")
        lines.append(f"  代码 0x{exc['code']:08X}   地址 0x{exc['address']:016X}   "
                     f"线程 id {exc['thread_id']}   flags 0x{exc['flags']:X}")
        if exc["code"] == 0xC0000005 and len(exc["params"]) >= 2:
            kind = {0: "读", 1: "写", 8: "执行(DEP)"}.get(exc["params"][0], str(exc["params"][0]))
            lines.append(f"  访问类型: {kind}   目标地址 0x{exc['params'][1]:016X}")
        elif exc["params"]:
            lines.append("  参数: " + ", ".join(f"0x{p:X}" for p in exc["params"]))

    c = info.get("culprit")
    if c:
        lines.append(f"🎯 故障模块: {c['name']}")
        lines.append(f"   基址 0x{c['base']:016X}  大小 0x{c['size']:X}  "
                     f"偏移 +0x{c['offset']:X}")
    elif exc:
        lines.append("🎯 故障模块: 未落在任何已加载模块内(可能是已卸载 DLL / JIT / 堆内存)")
        for m in info.get("nearest") or []:
            lines.append(f"   附近: {m['name']}  基址 0x{m['base']:016X}")
    if info.get("thread_count"):
        lines.append(f"线程数: {info['thread_count']}")
    return "\n".join(lines)


def _latest_dump() -> Path | None:
    root = Path(__file__).resolve().parents[1]
    cands = sorted((root / "logs").glob("crash_*.dmp"), key=lambda p: p.stat().st_mtime)
    return cands[-1] if cands else None


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("-")]
    if "--all" in argv:
        root = Path(__file__).resolve().parents[1]
        dumps = sorted((root / "logs").glob("crash_*.dmp"), key=lambda p: p.stat().st_mtime)
        if not dumps:
            print("logs/ 下没有 crash_*.dmp")
            return 1
        for p in dumps:
            try:
                print(describe(parse_dump(p)))
            except Exception as e:  # noqa: BLE001
                print(f"{p}: 解析失败 {e}")
            print("-" * 60)
        return 0

    targets = [Path(a) for a in args] or ([_latest_dump()] if _latest_dump() else [])
    if not targets:
        print("没有找到 dump 文件。\n"
              "· 若刚崩溃过: 看 logs/crash_*.dmp 是否生成(没生成说明 minidump 钩子没挂上)\n"
              "· 手动指定: python scripts/analyze_dump.py D:\\path\\a.dmp")
        return 1
    rc = 0
    for p in targets:
        try:
            print(describe(parse_dump(p)))
        except Exception as e:  # noqa: BLE001
            print(f"{p}: 解析失败 {e}")
            rc = 2
        print("-" * 60)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
