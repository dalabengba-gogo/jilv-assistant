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

from common import imread_unicode


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

    阈值分三级(2026-10-19 v4.9.0 P2-5)，优先级从高到低:
      ① 调用方显式传参  —— 脚本里某一步写 ``threshold: 0.9``(Step.threshold)
      ② `_meta.txt`     —— 该模板自己的默认值(见 _load_meta 的格式说明)
      ③ 构造时的 threshold —— config.yaml 的 match_threshold(全局兜底)
    """

    META_NAME = "_meta.txt"

    def __init__(self, template_dir: str, threshold: float = 0.80):
        self.template_dir = Path(template_dir)
        self.threshold = threshold
        self.templates = {}   # name -> {"img":BGR, "mask":optional}
        self.thresholds = {}  # name -> 该模板的默认阈值(_meta.txt)
        self.load_errors = []  # 读失败的模板名(中文路径坑的取证口)
        self._load()

    def _load(self):
        if not self.template_dir.exists():
            return
        # 2026-10-16 v3.0.0: 原本是裸 cv2.imread —— 模板目录路径含中文时
        # (绿色版装在 D:\鸡驴助手_xxx 下就是) 它对所有模板都返回 None 且不报错,
        # 于是 self.templates 为空, 表现出来却是"每个步骤都等待超时", 极难定位。
        # 改用 imread_unicode(np.fromfile + imdecode), 并统计读失败的模板。
        #
        # 2026-10-17 v3.5.0(P1-2): 先装进**局部** dict, 全部读完后一次性整体赋值。
        # 旧写法是 self.templates.clear() 再逐条 self.templates[name] = ...,
        # 装载期间别的线程(worker 跑任务 / 主线程刷新模板下拉)正遍历同一个 dict,
        # 会撞上 RuntimeError: dictionary changed size during iteration, 或者
        # 更隐蔽地读到"半套模板"(前 30 张是新目录的、后 35 张还是旧的)。
        # Python 下「给属性赋一个新 dict」是单条字节码、原子完成, 读者只会看到
        # 旧的全套或新的全套, 不会看到中间态。
        loaded = {}
        errors = []            # 读不进来的模板文件名, 供 GUI/日志告警
        for p in sorted(self.template_dir.glob("*.png")):
            img = imread_unicode(p, cv2.IMREAD_UNCHANGED)
            if img is None:
                errors.append(p.name)
                continue
            name = p.stem
            if img.ndim == 3 and img.shape[2] == 4:
                # 有 alpha 通道：alpha=0 区域为掩膜
                alpha = img[:, :, 3]
                bgr = img[:, :, :3]
                mask = np.where(alpha > 10, 255, 0).astype(np.uint8)
                loaded[name] = {"img": bgr, "mask": mask}
            else:
                loaded[name] = {"img": img, "mask": None}
        self.templates = loaded      # ← 原子切换
        self.load_errors = errors
        self.thresholds = self._load_meta()   # 与模板一起原子生效

    def _load_meta(self) -> dict[str, float]:
        """读 `_meta.txt`: 给**单个模板**配默认阈值(2026-10-19 v4.9.0 P2-5)

        为什么需要它: 全局 match_threshold(0.80)是个折中值 —— 有的模板(纯文字、
        颜色单调)在 0.92 才算真命中, 调到 0.9 却会把别的模板一起勒死; 反过来
        有的模板(带动态角标/半透明底)天生只能到 0.72, 0.80 时永远匹配不上。
        逐模板一条设置, 比改代码里的常量或整体调阈值都安全。

        文件格式(每行一条, `#` 之后为注释, 写错了就跳过该行, 不会让模板库加载失败):

            # 模板名 = 阈值(0~1)
            帮派-捐献按钮 = 0.88
            menu_panel_back = 0.86

        注意: 脚本里某一步显式写了 `threshold:` 时, 以脚本为准(见 threshold_for)。
        """
        path = self.template_dir / self.META_NAME
        out: dict[str, float] = {}
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            return out
        for line in text.splitlines():
            s = line.split("#", 1)[0].strip()
            if not s:
                continue
            key = val = None
            for sep in ("=", ":", "\t"):
                if sep in s:
                    key, val = s.split(sep, 1)
                    break
            if key is None:
                continue
            key, val = key.strip(), val.strip()
            try:
                th = float(val)
            except ValueError:
                continue
            if key and 0.0 < th <= 1.0:
                out[key] = th
        return out

    def threshold_for(self, name: str, override: float | None = None) -> float:
        """该模板本次匹配用哪个阈值 —— 脚本显式 > _meta.txt > 全局"""
        if override is not None:
            try:
                v = float(override)
                if 0.0 < v <= 1.0:
                    return v
            except (TypeError, ValueError):
                pass
        th = self.thresholds.get(name)
        return th if th is not None else self.threshold

    def reload(self):
        """重读模板目录(GUI 里新增/改了模板后调用)

        整体替换而非原地清空重填 —— 见 _load() 里 P1-2 的说明。
        """
        self._load()

    def names(self):
        return list(self.templates.keys())

    def match_one(self, screen_bgr, name: str, threshold: float | None = None) -> MatchResult | None:
        """在截图中找单个模板，返回最佳命中或 None

        threshold 为 None 时走 threshold_for 的分级解析(脚本 > _meta.txt > 全局)。
        """
        t = self.templates.get(name)
        if t is None:
            return None
        th = self.threshold_for(name, threshold)
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
