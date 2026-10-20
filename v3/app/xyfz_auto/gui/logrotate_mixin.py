# -*- coding: utf-8 -*-
"""文本日志 / 失败截图 的滚动清理

拆自 main_window.py(2026-10-18 v4.7.0 批次四·工程 P2-1)。
触发点三条: GUI 启动 / 跨日 / 任务结束 —— 见调用方。

依赖(由宿主 MainWindow 提供):
  · self.cfg(读 engine.log_keep_days 等)
  · self.append_sys_log
"""
from __future__ import annotations
import datetime
from pathlib import Path
from common import rotate_files, rotate_text_logs
# ⚠ 2026-10-19 v5.0.0 修正: 同 taskeditor_mixin —— 本文件在 gui/ 下, 根是
#   app/xyfz_auto/(parents[1])。v4.7.0 误写 parents[2](= app/), 于是三处日志滚动
#   全在扫不存在的 app/logs, 静默什么也没做(跨日/启动/任务结束的清理都是空转)。
PROJECT_ROOT = Path(__file__).resolve().parents[1]   # app/xyfz_auto/
class LogRotateMixin:
    """见模块 docstring。方法体整体搬自 main_window.py, 未作改写。"""
    def _log_keep_days(self) -> int:
        try:
            return int((self.cfg.get("engine") or {}).get("log_keep_days", 7) or 7)
        except Exception:
            return 7
    def _active_log_names(self) -> set:
        """正在跑的账号, 其 run_<port>.log 不许归档。

        归档的本质是 rename —— 把它从"正握着它的写方"手里抽走, 那个写方会继续
        写进改名后的 inode(后续日志就再也进不了新文件)。所以跨日那一刀必须避开
        活着的端口。判断依据: 多开子进程的字典 + 本机 worker 是否在跑。

        2026-10-19 v5.0.0: 还要加上 **run.log —— 它是本进程自己的主日志**。
        GUI 启动时 setup_logger 就用 RotatingFileHandler 常驻打开它, 而 Windows
        不允许 rename 一个被打开的文件: rotate_text_logs 里那次 os.rename 会抛
        PermissionError, 被 `except OSError: pass` 吞掉 —— 于是"启动/跨日/任务结束"
        三次滚动对 run.log 全都在做无用功, 且完全不可见。它自己有 RotatingFileHandler
        (log_max_bytes × log_backup_count)兜底, 本来也不需要按天归档。
        显式放进 skip 是为了让代码说真话, 而不是每次去撞一个必然失败的 rename。
        """
        names = {"run.log"}
        try:
            for port in (getattr(self.panel_multi, "_procs", None) or {}):
                names.add(f"run_{port}.log")
        except Exception:
            pass
        try:
            if getattr(self, "worker", None) is not None and self.worker.isRunning():
                names.add(f"run_{self._local_port}.log")
        except Exception:
            pass
        return names
    def _rotate_logs(self, reason: str):
        """文本日志滚动(接到 append_sys_log, 让用户在系统日志里看见清理动作)

        以前只在"跑完任务"清一次 —— 不跑任务的天只进不出, 实测 logs 撑到 159MB。
        现在三个触发点: ① 启动(这里) ② 跨日(_maybe_rotate_daily) ③ 任务结束。
        """
        try:
            res = rotate_text_logs(PROJECT_ROOT / "logs",
                                   keep_days=self._log_keep_days(),
                                   skip_names=self._active_log_names())
        except Exception:
            return          # 家务活, 绝不能因为清理把 GUI 搞崩
        # 失败截图目录(logs/debug*)也一并清 —— 这一半以前同样只在"跑任务时"才清,
        # 于是 90~100 张 PNG 能静静躺到爆盘(实测单目录 75~83MB)。
        n_shot = 0
        try:
            eng = self.cfg.get("engine") or {}
            keep_max = int(eng.get("debug_keep_max", 120) or 0)
            keep_days = int(eng.get("debug_keep_days", 7) or 0)
            if keep_max or keep_days:
                logs = PROJECT_ROOT / "logs"
                if logs.exists():
                    for d in sorted(logs.glob("debug*")):
                        if d.is_dir():
                            n_shot += rotate_files(str(d), keep_max=keep_max,
                                                   keep_days=keep_days)
        except Exception:
            pass
        try:
            if res.get("archived") or res.get("removed") or n_shot:
                self.append_sys_log(
                    f"[日志] {reason}滚动: 归档 {res.get('archived', 0)} 个, "
                    f"删除过期 {res.get('removed', 0)} 个, 清理失败截图 {n_shot} 张")
        except Exception:
            pass
    def _maybe_rotate_daily(self):
        """跨日触发点: 只在"日期真的变了"时清一次(每5秒轮询里蹭一下)"""
        try:
            today = datetime.date.today().isoformat()
            was = getattr(self, "_last_rotate_day", None)
            if was == today:
                return
            self._last_rotate_day = today
            if was is not None:          # 启动那次已经转过, 别刚开局又转一遍
                self._rotate_logs("跨日")
        except Exception:
            pass
