# -*- coding: utf-8 -*-
"""单账号(单 MuMu 实例)headless 任务执行入口

供「多开管理」面板/多开调度器调用, 也便于单独调试某端口账号:
  python scripts/run_one.py --port 16384 --task daily_all.yaml
  python scripts/run_one.py --port 16384 --probe        # 只测 adb 连接+截图, 不动屏幕

每个 run_one 是独立进程(引擎无全局状态), 多账号并行互不干扰;
日志写 logs/run_<port>.log, 退出码: 0=任务成功 1=失败/中断 2=设备/配置错误。

2026-09-24 新增实时回传: 通过 live.LiveWriter 把 画面/步骤/日志 写进
logs/live/port_<port>/, 主 GUI 据此在「实时画面」「实时步骤」里按账号分别展示
(改造前子进程完全不出图, 界面只能看到本机那一个账号)。
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import _harden_console                   # noqa: E402
from common import load_config                       # noqa: E402
from common import acquire_device_lock, release_device_lock  # noqa: E402
from common import png_bytes_to_bgr                  # noqa: E402
from device import AdbDevice                         # noqa: E402
from engine import load_task, Engine                 # noqa: E402
from engine.ui_registry import UiRegistry            # noqa: E402
from live import LiveWriter                          # noqa: E402
from recognition import OcrEngine, TemplateMatcher   # noqa: E402
from run_report import write_run_report              # noqa: E402  v5.6.0 运行历史落盘

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 2026-10-19 v5.2.0: 子进程也装崩溃守卫(文件名带端口后缀, 多开时互不覆盖)。
# 主界面在多开里只看到"子进程退出码非 0", 没有 crash.log 就查不到原因 ——
# 和主进程一样会静默。at exit 时由 crash_guard 自己关句柄。
_CRASH_GUARD = None
try:
    import crash_guard as _CRASH_GUARD
except Exception:
    _CRASH_GUARD = None


def _make_logger(port: int) -> logging.Logger:
    """日志: 控制台(仅交互式终端) + logs/run_<port>.log(RotatingFileHandler 独写)

    2026-10-18 v4.6.0(P0-4 前置·单写方): 这份 run_<port>.log **只由本子进程写**。
    以前 GUI 侧 multi_panel 还会把子进程 stdout 重定向到同一个文件, 于是
    「stdout 流」和「FileHandler」两个句柄写同一处 —— 后果有两个:
      ① 每条日志写两遍;
      ② 任一方想轮转都会和另一方打架(一个切了, 另一个还握着旧文件)。
    现在分工明确: 本文件归 RotatingFileHandler 独写; GUI 侧改写到
    logs/boot_<port>.log(见 gui/multi_panel._spawn), 专门接早期崩溃栈。

    控制台句柄只在**真终端**(isatty)时挂载: GUI 拉起时 stdout 是被重定向的文件,
    不是交互式终端, 再挂上去就又变回上面说的"写两遍"。效果是——双击调试 bat
    照样看得到输出, 而 GUI 多开时不会重复。
    """
    _harden_console()          # GBK 控制台下防止 ✔/▶ 触发 UnicodeEncodeError
    logger = logging.getLogger(f"xyfz_{port}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    stream = getattr(sys, "stdout", None)
    if stream is not None:          # pythonw 兼容(绿色版)
        try:
            interactive = bool(stream.isatty())
        except Exception:
            interactive = False
        if interactive:
            sh = logging.StreamHandler(stream)
            sh.setFormatter(fmt)
            logger.addHandler(sh)
    # 单文件大小上限来自 config, 拿不到就用默认 2MB × 3 份
    try:
        eng = load_config().get("engine") or {}
    except Exception:
        eng = {}
    try:
        max_bytes = int(eng.get("log_max_bytes") or 2 * 1024 * 1024)
        backups = int(eng.get("log_backup_count") or 3)
    except Exception:
        max_bytes, backups = 2 * 1024 * 1024, 3
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    fh = RotatingFileHandler(log_dir / f"run_{port}.log",
                             maxBytes=max((max_bytes, 64 * 1024)),
                             backupCount=max((backups, 1)),
                             encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def main():
    ap = argparse.ArgumentParser(description="单账号任务执行(多开子进程入口)")
    ap.add_argument("--port", type=int, default=16384, help="该账号 MuMu 实例的 adb 端口")
    ap.add_argument("--task", default="daily_all.yaml",
                    help="任务脚本名(assets/tasks/ 下)或绝对/相对路径")
    ap.add_argument("--tag", default="", help="账号标签(仅用于日志显示)")
    ap.add_argument("--start-index", type=int, default=0,
                    help="从脚本第 N 步(0基, 展开后)开始执行; 0=从头。供 GUI「脚本步骤」页使用")
    ap.add_argument("--probe", action="store_true", help="只测 adb 连接+截图, 不跑任务")
    args = ap.parse_args()

    # 崩溃守卫要在任何可能崩的模块被使用前装好(文件名带端口后缀)
    if _CRASH_GUARD is not None:
        try:
            _CRASH_GUARD.set_suffix(f"_p{args.port}")
            _CRASH_GUARD.install_crash_guard()
            _CRASH_GUARD.start_thread_watchdog(interval=30.0)
            _CRASH_GUARD.note(f"=== run_one 启动 port={args.port} task={args.task} ===")
        except Exception:
            pass

    logger = _make_logger(args.port)
    tag = f"[{args.tag}] " if args.tag else ""
    logger.info(f"===== {tag}端口 {args.port} 启动, 任务: {args.task} =====")

    # 2026-09-24: 实时回传通道 —— 主 GUI 靠它看到这个账号的画面/步骤。
    # 建在设备检查之前, 这样"连不上"也能在界面上留下原因, 而不是一条哑通道。
    live = LiveWriter(args.port, name=args.tag or f"端口{args.port}", task=args.task)
    live.prepare()

    def _abort(msg: str, info: str = ""):
        """设备/配置类错误: 留痕后以退出码 2 结束"""
        logger.error(msg)
        live.event("task_end", ok=False, info=info or msg)
        live.close()
        sys.exit(2)

    cfg = load_config()
    d = cfg["device"]
    try:
        dev = AdbDevice(d["adb_path"], d["host"], args.port)
    except Exception as e:
        _abort(f"adb 初始化失败(端口 {args.port}): {e}", "adb/设备初始化失败")
    if not dev.is_online():
        _abort(f"设备不在线(端口 {args.port})。请确认该模拟器实例已开机、"
               f"ADB 调试已开、端口填的是实例真实端口(MuMu 多开一般 16384+i*32)",
               "设备不在线(端口没连上)")
    logger.info(f"设备在线: {dev.serial}")

    if args.probe:
        shot = PROJECT_ROOT / "logs" / f"probe_{args.port}.png"
        dev.screenshot(shot)
        logger.info(f"截图 OK → {shot}, 前台: {dev.current_activity() or '(桌面)'}")
        try:
            live.frame(png_bytes_to_bgr(dev.screenshot()), force=True)
        except Exception as e:
            logger.warning(f"预览帧失败: {e}")
        live.event("task_end", ok=True, info="probe 完成")
        live.close()
        sys.exit(0)

    # 2026-09-11 新增: 跨进程设备锁(与 GUI 主任务/其他实例互斥)
    if not acquire_device_lock(args.port):
        _abort(f"端口 {args.port} 已有其他任务实例在跑, 本次不启动", "端口被其他实例占用")

    # 任务路径解析: 直接路径优先, 否则按 assets/tasks/<文件名>
    p = Path(args.task)
    if not p.is_absolute() and not p.exists():
        p = PROJECT_ROOT / "assets" / "tasks" / args.task
    if not p.exists():
        _abort(f"任务文件不存在: {p}", "任务文件不存在")
    try:
        task = load_task(p)
    except Exception as e:
        _abort(f"任务解析失败 {p}: {e}", "任务解析失败")
    logger.info(f"任务加载: {task.name} ({len(task.steps)} 步)")
    start_index = max(0, min(int(args.start_index or 0), max(0, len(task.steps) - 1)))
    if start_index:
        logger.info(f"⚠ 从第 {start_index + 1} 步开始执行(跳过前面 {start_index} 步): "
                    f"[{task.steps[start_index].name}]")

    # 步骤清单一次性回传 —— GUI 立刻能铺出完整列表; 否则脚本第一步若是
    # sleep 60s, 界面上会长时间只有一行, 看不出这个号到底在跑什么。
    live.announce([s.describe() for s in task.steps], task=task.name)

    matcher = TemplateMatcher(cfg["recognition"]["template_dir"],
                              cfg["recognition"]["match_threshold"])
    ocr = OcrEngine()
    eng = Engine(dev, matcher, ocr=ocr,
                 poll_interval=cfg["engine"].get("poll_interval", 0.3),
                 logger=logger,
                 debug_dump_dir=f"logs/debug_{args.port}",
                 ui=UiRegistry())
    # 事件分流: logger 负责 logs/run_<port>.log(整份日志), live 负责回传给 GUI
    eng.subscribe(
        log=lambda lv, m: live.log(lv, m),
        screenshot=lambda s: live.frame(s),
        step_start=lambda i, s: live.step_start(i, s.describe()),
        step_end=lambda i, s, o: live.step_end(i, s.name or str(i), o),
    )
    # 2026-10-18 v4.5.0 (P0-3): 心跳回传 —— 主线程周期性发"还在推进"的证据,
    # GUI 据此区分正常长跑与僵尸进程(假运行中)。
    eng.set_heartbeat(lambda: live.heartbeat())

    # 2026-10-18 v4.5.0 (P1-4): 协作式停止。GUI 不再硬杀子进程(TerminateProcess 会
    # 让当前步骤做到一半就被掐断、task_end 也来不及写 → 界面卡"运行中")。改为写一个
    # 停止标记文件, 这里的主线程看门线程看到后就调 eng.stop() —— 引擎在 0.1s 内察觉、
    # 安全收尾当前步骤再退出。超时(标记写下后进程仍不退出)由 GUI 兜底硬杀。
    stop_flag = PROJECT_ROOT / "logs" / "live" / f"port_{args.port}" / "stop.flag"
    try:
        stop_flag.unlink()             # 清掉上一轮可能残留的标记
    except OSError:
        pass

    def _watch_stop():
        """看门线程: 每秒检查停止标记, 命中则协作停止引擎。"""
        while True:
            time.sleep(1)
            if stop_flag.exists():
                try:
                    eng.stop()
                except Exception:
                    pass
                break

    threading.Thread(target=_watch_stop, daemon=True).start()

    # 首帧: 先给界面一张图, 免得开局空等
    try:
        live.frame(png_bytes_to_bgr(dev.screenshot()), force=True)
    except Exception as e:
        logger.warning(f"首帧预览失败(不影响任务): {e}")

    started_at = time.time()          # v5.6.0: 运行历史用的计时起点
    try:
        ok = eng.run_task(task, start_index)
    except KeyboardInterrupt:
        logger.warning("收到中断信号, 停止任务")
        ok = False
    except Exception as e:
        import traceback
        traceback.print_exc()
        logger.error(f"任务异常终止: {e}")
        ok = False
    # 2026-10-19 v5.6.0: 运行历史落盘 —— 无论正常完成/停止/异常, 把结构化结果写进
    # logs/reports/YYYY-MM-DD_p<port>.json, GUI「运行历史」页签据此展示历史。
    try:
        _exit_code = (3 if getattr(eng, "_stop_at", None) is not None
                      else (0 if ok else 1))
        write_run_report(args.port, args.tag or f"端口{args.port}",
                         args.task, task.name, eng, ok, started_at, _exit_code)
    except Exception:
        pass
    # 2026-10-18 v4.5.0 (P1-4): 用户协作停止优先判定。eng.stop() 被调用(看门线程看到
    # 停止标记)才会置 _stop_at —— 此时 run_task 已安全收尾当前步骤, 应区别于"失败(1)"。
    # 退出码 3 = 已停止, 多开面板据此显示「已停止」而非「失败」。
    if eng._stop_at is not None:
        logger.info("任务已被用户停止(协作收尾完成)")
        live.end(False, "已停止(用户)")
        try:
            stop_flag.unlink()         # 清标记, 避免下一次函数同一端口被秒停
        except OSError:
            pass
        release_device_lock(args.port)
        sys.exit(3)
    # 2026-10-16 v3.0.0: 多开/命令行场景同样要能看到"哪些步骤没做成"。
    # 以前 continue 兜底让 ok=True, 子进程退出码 0, 多开面板一律显示成功。
    summary = eng.failure_summary()
    # 2026-10-17 v3.5.0 (P2-4): 读结构化统计, 不再从文案里切数字
    # (旧写法 split(" 步未做成")[0] 在文案变化时会静默得到 0)。
    st = eng.counts()
    n_fail = int(st.get("failed", 0) or 0)
    n_skip = int(st.get("skipped", 0) or 0)
    # 2026-10-17 v3.9.0: 同 GUI —— 成功只报最后一个步骤, 失败才给子任务汇总。
    last_step = str(st.get("last_step", "") or "")
    if ok and n_fail:
        logger.warning(f"任务跑完但有 {n_fail} 步失败")
        sub = eng.subtask_summary()
        if sub:
            logger.warning(sub)
        live.end(True, f"完成但有{n_fail}步失败")
    elif ok:
        logger.info(f"任务成功 — 最后执行 {last_step or '?'}")
        live.end(True, "成功")
    else:
        logger.info(f"任务失败/中断")
        sub = eng.subtask_summary()
        if sub:
            logger.info(sub)
        elif summary:
            logger.info(summary)        # 异常终止(拿不到子任务统计)才退回明细
        live.end(False, "失败/中断")
    release_device_lock(args.port)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
