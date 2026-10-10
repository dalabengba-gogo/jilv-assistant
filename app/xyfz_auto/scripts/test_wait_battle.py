# -*- coding: utf-8 -*-
"""实测：等待战斗结束（闭环验证）

逻辑：轮询截图，识别右下角"自动中"标志(battle_flag_auto)。
  标志存在 → 仍在战斗，继续等
  标志消失 → 战斗结束，截图留证退出
这验证了 Engine 的 轮询识别 + not 条件 + 超时控制 全链路。
"""
import sys
from pathlib import Path
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import load_config, setup_logger, png_bytes_to_bgr
from device import AdbDevice
from recognition import TemplateMatcher

logger = setup_logger()
cfg = load_config()

dev = AdbDevice(cfg["device"]["adb_path"], cfg["device"]["host"], cfg["device"]["port"])
matcher = TemplateMatcher(cfg["recognition"]["template_dir"], 0.80)

TIMEOUT = 180
deadline = time.time() + TIMEOUT
logger.info(f"开始等待战斗结束(识别「自动中」标志消失)，超时 {TIMEOUT}s")

in_battle_count = 0
while time.time() < deadline:
    screen = png_bytes_to_bgr(dev.screenshot())
    hit = matcher.match_one(screen, "battle_flag_auto")
    if hit:
        in_battle_count += 1
        if in_battle_count % 5 == 1:
            logger.info(f"仍在战斗中 (score={hit.score:.3f})，继续等待...")
    else:
        logger.info("「自动中」标志消失 → 战斗已结束！")
        out = Path("logs/battle_end.png")
        import cv2
        cv2.imwrite(str(out), screen)
        logger.info(f"战斗结束后截图: {out}")
        sys.exit(0)
    time.sleep(2)

logger.warning("超时，战斗仍未结束")
sys.exit(1)
