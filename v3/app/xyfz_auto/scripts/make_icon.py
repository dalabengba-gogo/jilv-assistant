# -*- coding: utf-8 -*-
"""生成应用图标(窗口/任务栏图标)

用法:
  python scripts/make_icon.py

输出:
  assets/app_icon.png   —— 256x256 预览用
  assets/app_icon.ico   —— 多尺寸(16/24/32/48/64/128/256)任务栏/窗口图标

设计: 圆角深色底 + 金色循环箭头环 + 金色闪电(象征"自动循环执行")
不依赖任何外部美术资源, 纯 Pillow 绘制, 可随时重新生成。
"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "assets"

S = 1024                      # 绘制画布(最后降采样, 边缘更平滑)
GOLD = (245, 196, 81, 255)    # 主色: 金
GOLD_DARK = (214, 158, 46, 255)


def _bg() -> Image.Image:
    """圆角矩形 + 竖直渐变底(深靛蓝), 返回 RGBA 图。"""
    grad = Image.new("RGBA", (S, S))
    gd = ImageDraw.Draw(grad)
    top, bottom = (44, 56, 92), (18, 24, 42)
    for y in range(S):
        t = y / (S - 1)
        gd.line(
            [(0, y), (S, y)],
            fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)) + (255,),
        )
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=255)
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def _ring(img: Image.Image) -> None:
    """金色循环环: 缺口圆弧 + 端点箭头(表达"循环/自动")。"""
    d = ImageDraw.Draw(img)
    pad = int(S * 0.17)
    box = [pad, pad, S - 1 - pad, S - 1 - pad]
    w = int(S * 0.062)
    # 从 -60° 起画 300° 弧, 留 60° 缺口
    d.arc(box, start=-60, end=240, fill=GOLD, width=w)
    # 缺口下端(240°)的箭头: 三角, 指向切线方向(逆时针闭合)
    import math

    cx = cy = (S - 1) / 2
    r = (S - 1) / 2 - pad
    a = math.radians(240)
    ax, ay = cx + r * math.cos(a), cy + r * math.sin(a)
    size = w * 1.35
    # 切线方向(逆时针): 角度 +90°
    ta = a + math.pi / 2
    tip = (ax + math.cos(ta) * size, ay + math.sin(ta) * size)
    p1 = (ax + math.cos(a + math.pi / 2 + math.pi / 2) * size * 0.75,
          ay + math.sin(a + math.pi / 2 + math.pi / 2) * size * 0.75)
    p2 = (ax - math.cos(a + math.pi / 2 + math.pi / 2) * size * 0.75,
          ay - math.sin(a + math.pi / 2 + math.pi / 2) * size * 0.75)
    d.polygon([tip, p1, p2], fill=GOLD)


def _bolt(img: Image.Image) -> None:
    """中央金色闪电(自动/加速)。"""
    pts = [(0.58, 0.04), (0.30, 0.56), (0.47, 0.56), (0.41, 0.96), (0.71, 0.42), (0.53, 0.42)]
    x0, x1, y0, y1 = 0.30 * S, 0.71 * S, 0.10 * S, 0.90 * S
    poly = [
        (x0 + (px - 0.30) / (0.71 - 0.30) * (x1 - x0),
         y0 + (py - 0.04) / (0.96 - 0.04) * (y1 - y0))
        for px, py in pts
    ]
    d = ImageDraw.Draw(img)
    d.polygon(poly, fill=GOLD)
    # 极细深色描边让闪电在缩小后仍清晰
    d.line(poly + [poly[0]], fill=GOLD_DARK, width=max(2, int(S * 0.004)))


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    img = _bg()
    _ring(img)
    _bolt(img)

    png = OUT_DIR / "app_icon.png"
    img.resize((256, 256), Image.LANCZOS).save(png)

    ico = OUT_DIR / "app_icon.ico"
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(ico, format="ICO", sizes=sizes)
    print(f"已生成: {png}")
    print(f"已生成: {ico} (尺寸 {', '.join(f'{w}x{h}' for w, h in sizes)})")


if __name__ == "__main__":
    main()
