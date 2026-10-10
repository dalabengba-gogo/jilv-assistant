# -*- coding: utf-8 -*-
"""最小闭环自测脚本

验证整条链路：adb 截图 → OpenCV 模板匹配 → 点击 → 再识别。
用法：
  python scripts/self_test.py            # 仅测连接+截图+模板加载
  python scripts/self_test.py --task assets/tasks/demo_login.yaml
                                       # 跑一个真实 YAML 任务
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import load_config, setup_logger
from device import AdbDevice
from engine import load_task, Engine
from engine.ui_registry import UiRegistry
from recognition import OcrEngine, TemplateMatcher


def test_basic(cfg, logger):
    """连接 + 截图 + 模板库自检"""
    dev = AdbDevice(cfg["device"]["adb_path"],
                    cfg["device"]["host"], cfg["device"]["port"])
    logger.info(f"设备在线: {dev.is_online()}")
    logger.info(f"前台: {dev.current_activity() or '(桌面)'}")

    shot_path = Path(__file__).resolve().parent.parent / "logs" / "selftest_screen.png"
    dev.screenshot(shot_path)
    logger.info(f"截图 OK → {shot_path}")

    matcher = TemplateMatcher(cfg["recognition"]["template_dir"],
                              cfg["recognition"]["match_threshold"])
    names = matcher.names()
    logger.info(f"模板库 {len(names)} 个: {names if names else '(空, 先用 capture.py 采集)'}")
    return dev, matcher


def main():
    logger = setup_logger()
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default=None, help="YAML 任务文件路径")
    args = ap.parse_args()

    dev, matcher = test_basic(cfg, logger)

    if args.task:
        task = load_task(args.task)
        logger.info(f"加载任务: {task.name} ({len(task.steps)} 步)")
        ocr = OcrEngine() if cfg["recognition"].get("ocr_engine") == "rapidocr" else None
        eng = Engine(dev, matcher, ocr=ocr,
                     poll_interval=cfg["engine"]["poll_interval"],
                     logger=logger, debug_dump_dir="logs/debug",
                     ui=UiRegistry())
        eng.subscribe(log=lambda lv, m: None)   # 日志已由 logger 输出
        ok = eng.run_task(task)
        logger.info("任务结果: " + ("成功" if ok else "失败/中断"))
        sys.exit(0 if ok else 1)
    else:
        print("\n提示: 加 --task 参数可执行 YAML 任务。示例:")
        print("  python scripts/self_test.py --task assets/tasks/demo_login.yaml")


if __name__ == "__main__":
    main()
