# -*- coding: utf-8 -*-
"""模板匹配识别模块

对屏幕截图做 OpenCV 模板匹配，判断画面中是否出现某个已知模板(按钮/图标/面板)，
并返回其中心坐标与相似度。这是整个「识别当前进度」的核心。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass
class MatchResult:
    """一次模板匹配的结果"""
    name: str          # 模板名（不含扩展名）
    score: float       # 相似度 0~1
    x: int             # 命中区域中心 x
    y: int             # 命中区域中心 y
    w: int
    h: int
    path: str          # 模板文件路径

    @property
    def ok(self) -> bool:
        return self.score >= 0


class TemplateMatcher:
    """模板图库管理器 + 匹配器

    assets/templates/ 下每张 png 一个模板，文件名即模板名。
    图片左上角像素透明(alpha=0)的区域视为掩膜——用于处理按钮周围会变化的背景。
    """

    def __init__(self, template_dir: str, threshold: float = 0.80):
        self.template_dir = Path(template_dir)
        self.threshold = threshold
        self.templates = {}   # name -> {"img":BGR, "mask":optional}
        self._load()

    def _load(self):
        if not self.template_dir.exists():
            return
        for p in sorted(self.template_dir.glob("*.png")):
            img = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
            if img is None:
                continue
            name = p.stem
            if img.ndim == 3 and img.shape[2] == 4:
                # 有 alpha 通道：alpha=0 区域为掩膜
                alpha = img[:, :, 3]
                bgr = img[:, :, :3]
                mask = np.where(alpha > 10, 255, 0).astype(np.uint8)
                self.templates[name] = {"img": bgr, "mask": mask}
            else:
                self.templates[name] = {"img": img, "mask": None}

    def reload(self):
        self.templates.clear()
        self._load()

    def names(self):
        return list(self.templates.keys())

    def match_one(self, screen_bgr, name: str, threshold: float | None = None) -> MatchResult | None:
        """在截图中找单个模板，返回最佳命中或 None"""
        t = self.templates.get(name)
        if t is None:
            return None
        th = self.threshold if threshold is None else threshold
        tmpl = t["img"]
        mask = t["mask"]
        h, w = tmpl.shape[:2]
        scr_h, scr_w = screen_bgr.shape[:2]
        if w > scr_w or h > scr_h:
            return None
        method = cv2.TM_CCOEFF_NORMED
        if mask is not None:
            res = cv2.matchTemplate(screen_bgr, tmpl, method, mask=mask)
        else:
            res = cv2.matchTemplate(screen_bgr, tmpl, method)
        _min_v, max_v, _min_l, max_loc = cv2.minMaxLoc(res)
        if max_v >= th:
            cx = max_loc[0] + w // 2
            cy = max_loc[1] + h // 2
            return MatchResult(name=name, score=float(max_v), x=cx, y=cy,
                               w=w, h=h, path=str(self.template_dir / f"{name}.png"))
        return None

    def match_all(self, screen_bgr, names=None, threshold: float | None = None) -> list[MatchResult]:
        """匹配多个模板，返回全部命中的结果（按分数降序）"""
        cands = names if names is not None else self.templates.keys()
        results = []
        for name in cands:
            m = self.match_one(screen_bgr, name, threshold)
            if m:
                results.append(m)
        results.sort(key=lambda r: r.score, reverse=True)
        return results

    def find_best(self, screen_bgr, names=None, threshold: float | None = None) -> MatchResult | None:
        """返回分数最高的命中"""
        results = self.match_all(screen_bgr, names, threshold)
        return results[0] if results else None
