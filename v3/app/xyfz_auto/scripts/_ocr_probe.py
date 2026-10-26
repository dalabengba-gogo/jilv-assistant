# -*- coding: utf-8 -*-
"""临时：对一张截图跑 OCR，打印文本框与中心坐标（排查按钮真实位置用）。

用法: python scripts/_ocr_probe.py <图片路径> [关键词]
"""
import os
import sys

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from recognition.ocr import OcrEngine


def imread_u(p):
    return cv2.imdecode(np.fromfile(p, dtype=np.uint8), cv2.IMREAD_COLOR)


def main():
    p = sys.argv[1]
    kw = sys.argv[2] if len(sys.argv) > 2 else None
    img = imread_u(p)
    print("图片 %s  %s" % (p, img.shape))
    boxes = OcrEngine().recognize(img)
    print("共 %d 个文本框" % len(boxes))
    for b in boxes:
        if kw and kw not in b["text"]:
            continue
        print("  %-16s score=%.2f  中心=(%d,%d)  框=(%d,%d %dx%d)"
              % (b["text"], b["score"], b["x"] + b["w"] // 2, b["y"] + b["h"] // 2,
                 b["x"], b["y"], b["w"], b["h"]))


if __name__ == "__main__":
    main()
