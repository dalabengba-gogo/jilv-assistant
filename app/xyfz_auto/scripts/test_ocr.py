# -*- coding: utf-8 -*-
"""OCR 识别验证：对截图跑中文识别，输出全部文字行"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cv2
import numpy as np
from recognition import OcrEngine

img_path = sys.argv[1] if len(sys.argv) > 1 else "shots/main_ui_now.png"
# 2026-09-11 修复: cv2.imread 读不了中文路径(Windows), 改 numpy.fromfile+imdecode
img = cv2.imdecode(np.fromfile(img_path, dtype=np.uint8), cv2.IMREAD_COLOR)
if img is None:
    print(f"读图失败(路径不存在或非图片): {img_path}")
    sys.exit(1)
print(f"识别图片: {img_path} ({img.shape[1]}x{img.shape[0]})")

ocr = OcrEngine()
boxes = ocr.recognize(img)
print(f"\n共识别 {len(boxes)} 行文字:\n")
for b in sorted(boxes, key=lambda b: (b["y"] // 40, b["x"])):
    print(f"  ({b['x']:>3},{b['y']:>3} {b['w']:>3}x{b['h']:>3}) {b['text']}")
