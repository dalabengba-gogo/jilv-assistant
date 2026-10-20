# -*- coding: utf-8 -*-
"""识别模块快速验证：对当前屏幕跑全部模板匹配"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common import load_config, setup_logger, png_bytes_to_bgr
from device import AdbDevice
from recognition import TemplateMatcher

cfg = load_config()
logger = setup_logger()

dev = AdbDevice(cfg["device"]["adb_path"], cfg["device"]["host"], cfg["device"]["port"])
matcher = TemplateMatcher(cfg["recognition"]["template_dir"],
                          cfg["recognition"]["match_threshold"])
print(f"模板库: {matcher.names()}")

screen = png_bytes_to_bgr(dev.screenshot())
hits = matcher.match_all(screen)
if hits:
    for m in hits:
        print(f"命中 [{m.name}] score={m.score:.3f} center=({m.x},{m.y}) size={m.w}x{m.h}")
else:
    print("未命中任何模板")
