# -*- coding: utf-8 -*-
"""OCR 中文识别模块

封装 RapidOCR (onnxruntime)，对屏幕截图做中文文字检测+识别。
返回每行文字的文本框与内容，供引擎 wait:{ocr:"关键词"} 使用。

RapidOCR 输出格式: result = [ [box4点, text, score], ... ]
box = [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]  (左上/右上/右下/左下)
"""
from __future__ import annotations

import numpy as np


class OcrEngine:
    """惰性加载的 OCR 引擎（首次 recognize 时才初始化，加快启动）"""

    def __init__(self, lang: str = "ch"):
        self.lang = lang
        self._engine = None

    def _ensure(self):
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR
            self._engine = RapidOCR()
        return self._engine

    def recognize(self, screen_bgr: np.ndarray) -> list[dict]:
        """识别图片中的全部文字。

        返回: [{"text": str, "score": float,
                "x": int, "y": int, "w": int, "h": int}, ...]
        x/y/w/h 是外接矩形(左上角+宽高)，点击用中心即可。
        """
        eng = self._ensure()
        result, _elapse = eng(screen_bgr)
        boxes = []
        if not result:
            return boxes
        for item in result:
            # RapidOCR 新版可能返回 dict 或 list
            if isinstance(item, dict):
                pts = item.get("box") or item.get("boxes")
                text = item.get("text", "")
                score = item.get("score", 0.0)
            else:
                pts, text, score = item[0], item[1], item[2]
            pts = np.asarray(pts, dtype=np.float32)
            x = int(pts[:, 0].min())
            y = int(pts[:, 1].min())
            x2 = int(pts[:, 0].max())
            y2 = int(pts[:, 1].max())
            boxes.append({
                "text": str(text),
                "score": float(score),
                "x": x, "y": y,
                "w": x2 - x, "h": y2 - y,
            })
        return boxes

    # 2026-10-16 v3.0.0: 删掉了 find()/find_all() 两个便利封装 —— 全项目零调用,
    # 且 runner.Engine._ocr_find() 已有一份等价实现(还带区域过滤), 属于重复代码。
    # 需要"按关键词找文字框"的场合统一走 Engine._ocr_find。
