# -*- coding: utf-8 -*-
"""模板采集工具

用途：从模拟器/截图中采集「模板图」存入 assets/templates/，供识别模块使用。

用法：
  python scripts/capture.py shot [--out shots/live.png]
        截一张当前屏幕图

  python scripts/capture.py template <源图> <模板名> <x1> <y1> <x2> <y2> [--alpha]
        从源截图裁剪 (x1,y1)-(x2,y2) 区域存为模板 png。
        --alpha: 把四周挖空(变透明)作为掩膜，适合背景会变的按钮
                 (自动保留中间 80% 不透明，四周透明)

示例：
  python scripts/capture.py shot --out shots/login.png
  python scripts/capture.py template shots/login.png login_btn 210 545 330 595
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from common import load_config
from device import AdbDevice


def cmd_shot(cfg, out: str):
    dev = AdbDevice(cfg["device"]["adb_path"],
                    cfg["device"]["host"], cfg["device"]["port"])
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    dev.screenshot(out)
    print(f"截图已保存: {out}")


def cmd_template(src: str, name: str, x1, y1, x2, y2, alpha: bool):
    tpl_dir = Path(__file__).resolve().parent.parent / "assets" / "templates"
    tpl_dir.mkdir(parents=True, exist_ok=True)
    img = Image.open(src).convert("RGB")
    box = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    crop = img.crop(box)
    if alpha:
        # 构造 RGBA：四周一圈变透明作为掩膜(避开会变化的背景)
        w, h = crop.size
        rgba = crop.convert("RGBA")
        inset_x, inset_y = max(1, w // 10), max(1, h // 10)
        mask = Image.new("L", (w, h), 0)
        px = mask.load()
        for yy in range(h):
            for xx in range(w):
                if inset_x <= xx < w - inset_x and inset_y <= yy < h - inset_y:
                    px[xx, yy] = 255
        rgba.putalpha(mask)
        crop = rgba
    out = tpl_dir / f"{name}.png"
    crop.save(out)
    print(f"模板已保存: {out}  尺寸={crop.size}")


def main():
    cfg = load_config()
    ap = argparse.ArgumentParser(description="模板采集工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_shot = sub.add_parser("shot", help="截取当前屏幕")
    p_shot.add_argument("--out", default="shots/live.png")

    p_tpl = sub.add_parser("template", help="从截图裁剪模板")
    p_tpl.add_argument("src")
    p_tpl.add_argument("name")
    p_tpl.add_argument("x1", type=int)
    p_tpl.add_argument("y1", type=int)
    p_tpl.add_argument("x2", type=int)
    p_tpl.add_argument("y2", type=int)
    p_tpl.add_argument("--alpha", action="store_true")

    args = ap.parse_args()
    if args.cmd == "shot":
        cmd_shot(cfg, args.out)
    elif args.cmd == "template":
        cmd_template(args.src, args.name, args.x1, args.y1, args.x2, args.y2,
                     args.alpha)


if __name__ == "__main__":
    main()
