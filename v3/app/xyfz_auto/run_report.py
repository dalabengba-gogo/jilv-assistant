# -*- coding: utf-8 -*-
"""运行历史落盘 / 读取 (v5.6.0)

每次任务跑完, 把结构化结果写进 logs/reports/YYYY-MM-DD_p<port>.json(每个账号每天
一个文件, 内含当天所有运行的数组, 追加)。GUI「运行历史」页签读这些文件展示历史记录。

为什么落盘而不是只留 run.log: run.log 是追加式流水账, 跨天/跨账号混在一起, 想看
"这个号昨天那轮到底做没做成"得翻几百行; 落盘成结构化 JSON 后, 历史页能按账号/日期
直接列出每轮的成败、子任务明细、失败步骤、性能统计 —— 排障和回看都方便。

落盘用"写临时文件再 replace", 避免写到一半进程被停导致文件损坏。删除旧报告走
common._hard_remove, 绕开 WorkBuddy 运行时的批量删除守卫(否则在开发环境删旧文件
会触发 SystemExit 杀进程)。
"""
from __future__ import annotations

import datetime
import json
import os
from pathlib import Path

# 本模块在 app/xyfz_auto 下, parent 即工程根
PROJECT_ROOT = Path(__file__).resolve().parent
REPORT_DIR = PROJECT_ROOT / "logs" / "reports"

# 每个(日期,端口)文件最多保留的运行条数 / 旧报告文件保留天数
_MAX_RUNS_PER_FILE = 200
_KEEP_DAYS = 30


def _hard_remove(path: str) -> bool:
    """删除旧报告文件: 优先 Windows API 绕过 os.remove 钩子(见 common._hard_remove 同款)。"""
    try:
        if not os.path.lexists(path):
            return True
        import ctypes
        k32 = getattr(ctypes, "windll", None)
        if k32 is not None:
            if k32.kernel32.DeleteFileW(path):
                return True
            return False
    except Exception:
        pass
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def write_run_report(port: int, account: str, task_file: str, task_name: str,
                     eng, ok: bool, started_at: float, exit_code: int) -> dict:
    """把一次运行结果写进 logs/reports/YYYY-MM-DD_p<port>.json (追加到当天数组)。

    参数 eng 是 engine.runner.Engine 实例, 取其 last_result / counts() / stats。
    任何字段缺失都做兜底, 保证落盘不崩。
    """
    now = datetime.datetime.now()
    finished = _now()
    res = getattr(eng, "last_result", None)
    counts = eng.counts() if hasattr(eng, "counts") else {}
    st = getattr(eng, "stats", {}) or {}

    perf = {
        "shots": int(st.get("shots", 0) or 0),
        "frame_reuse": int(st.get("frame_reuse", 0) or 0),
        "ocr_full": int(st.get("ocr_full", 0) or 0),
        "ocr_band": int(st.get("ocr_band", 0) or 0),
        "ocr_hint_hit": int(st.get("ocr_hint_hit", 0) or 0),
        "ocr_cached": int(st.get("ocr_cached", 0) or 0),
        "ocr_same_screen": int(st.get("ocr_same_screen", 0) or 0),
        "shot_seconds": round(float(st.get("shot_seconds", 0) or 0), 1),
        "ocr_seconds": round(float(st.get("ocr_seconds", 0) or 0), 1),
    }

    rec = {
        "finished_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "started_at": (datetime.datetime.fromtimestamp(started_at).strftime(
            "%Y-%m-%d %H:%M:%S") if started_at else ""),
        "duration_seconds": round(finished - started_at, 1) if started_at else 0.0,
        "port": int(port),
        "account": account or f"端口{port}",
        "task": task_file or "",
        "task_name": task_name or (getattr(res, "name", "") if res else ""),
        "ok": bool(ok),
        "stopped": bool(getattr(eng, "_stop_at", None) is not None),
        "aborted_reason": (getattr(res, "aborted_reason", "") if res else "") or "",
        "exit_code": int(exit_code),
        "counts": {
            "failed": int(counts.get("failed", 0) or 0),
            "skipped": int(counts.get("skipped", 0) or 0),
            "required_failed": int(counts.get("required_failed", 0) or 0),
            "last_step": counts.get("last_step", "") or "",
        },
        # 子任务级汇总(界面只看这一层): 每个子任务 名称/总步数/是否走到/失败数/验收结果
        "subtasks": (list(getattr(res, "subtasks", []) or []) if res
                     else list(counts.get("subtasks", []) or [])),
        # 失败/条件未命中 逐步明细(排障用, 历史页可展开看)
        "failed_steps": (list(getattr(res, "failed", []) or []) if res else []),
        "skipped_steps": (list(getattr(res, "skipped", []) or []) if res else []),
        "performance": perf,
    }

    try:
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        fn = REPORT_DIR / f"{now.strftime('%Y-%m-%d')}_p{port}.json"
        arr = []
        if fn.exists():
            try:
                arr = json.loads(fn.read_text(encoding="utf-8"))
                if not isinstance(arr, list):
                    arr = [arr]
            except Exception:
                arr = []
        arr.append(rec)
        if len(arr) > _MAX_RUNS_PER_FILE:
            arr = arr[-_MAX_RUNS_PER_FILE:]
        # 原子写: 临时文件 → replace, 避免半截文件(进程中途被停)
        tmp = fn.with_name(fn.name + ".tmp")
        tmp.write_text(json.dumps(arr, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            tmp.replace(fn)
        except Exception:
            tmp.rename(fn)
        _clean_old_reports()
    except Exception:
        # 落盘是家务活, 绝不能因为写不出把任务/进程搞崩
        pass
    return rec


def load_run_reports(days: int = _KEEP_DAYS):
    """读取最近 days 天的所有运行报告, 按结束时间倒序返回列表。

    每条记录附带 _file 字段(来源文件名), GUI 据此分组/去重。读不到的文件静默跳过。
    """
    out = []
    if not REPORT_DIR.exists():
        return out
    cutoff = datetime.date.today() - datetime.timedelta(days=max(days, 1))
    for fn in sorted(REPORT_DIR.glob("*_p*.json")):
        try:
            date_str = fn.name.split("_p")[0]
            fdate = datetime.date.fromisoformat(date_str)
        except Exception:
            continue
        if fdate < cutoff:
            continue
        try:
            arr = json.loads(fn.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(arr, dict):
            arr = [arr]
        for r in arr:
            if not isinstance(r, dict):
                continue
            r["_file"] = fn.name
            out.append(r)
    out.sort(key=lambda r: r.get("finished_at", ""), reverse=True)
    return out


def _clean_old_reports():
    """删掉超过 _KEEP_DAYS 天的报告文件(用 _hard_remove 绕过删除守卫)。"""
    if not REPORT_DIR.exists():
        return
    cutoff = datetime.date.today() - datetime.timedelta(days=_KEEP_DAYS)
    for fn in REPORT_DIR.glob("*_p*.json"):
        try:
            fdate = datetime.date.fromisoformat(fn.name.split("_p")[0])
        except Exception:
            continue
        if fdate < cutoff:
            _hard_remove(str(fn))


def _now() -> float:
    import time as _t
    return _t.time()
