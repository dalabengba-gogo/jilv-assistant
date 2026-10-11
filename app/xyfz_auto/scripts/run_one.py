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

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _make_logger(port: int) -> logging.Logger:
    """控制台 + logs/run_<port>.log 双写(独立 logger, 不与 GUI 进程共享)"""
    _harden_console()          # GBK 控制台下防止 ✔/▶ 触发 UnicodeEncodeError
    logger = logging.getLogger(f"xyfz_{port}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    if sys.stdout is not None:          # pythonw 兼容(绿色版)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    fh = logging.FileHandler(log_dir / f"run_{port}.log", encoding="utf-8")
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
                 poll_interval=cfg["engine"].get("poll_interval", 1.0),
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

    # 首帧: 先给界面一张图, 免得开局空等
    try:
        live.frame(png_bytes_to_bgr(dev.screenshot()), force=True)
    except Exception as e:
        logger.warning(f"首帧预览失败(不影响任务): {e}")

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
    logger.info(f"任务结果: {'成功' if ok else '失败/中断'}")
    live.end(bool(ok), "成功" if ok else "失败/中断")
    release_device_lock(args.port)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
