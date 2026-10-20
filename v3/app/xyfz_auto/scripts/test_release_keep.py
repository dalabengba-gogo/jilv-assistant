# -*- coding: utf-8 -*-
"""打包 --keep N(旧包回收) 的安全语义测试(2026-10-19 v4.8.0)

为什么要单独测: 这条命令会**动 dist 里的目录**, 一旦写错就是"误删产物"。
全工程的铁律是"构建脚本绝不删除已有的东西", 所以这里必须钉死三件事:
  1. 只认正式包(鸡驴助手_vX.Y.Z), `_old_` / `_retire_` / 非目录一律不碰
  2. keep 说的是"总共留几个包" —— 刚打完的那个占一个名额且**永不挪走**
  3. keep=0(默认) / keep 大于现有包数 → 什么都不做

测试全部在临时目录里造假的包结构, 不接触真实 dist。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(APP_DIR, "..", "..", "tools"))

try:
    import build_portable as bp
except Exception as e:                                  # pragma: no cover
    print(f"  ✘ 无法导入 tools/build_portable.py: {e}")
    sys.exit(1)

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


def make_dist():
    root = Path(tempfile.mkdtemp(prefix="keep_test_"))

    def mk(name, ts):
        p = root / name
        (p / "app" / "xyfz_auto").mkdir(parents=True)
        f = p / "app" / "xyfz_auto" / "common.py"
        f.write_text("x", encoding="utf-8")
        os.utime(f, (ts, ts))

    for name, ts in [("鸡驴助手_v4.5.0", 1000), ("鸡驴助手_v4.6.0", 2000),
                     ("鸡驴助手_v4.7.0", 3000), ("鸡驴助手_v4.8.0", 4000)]:
        mk(name, ts)
    (root / "_old_鸡驴助手_v4.4.0_20260101").mkdir()
    (root / "鸡驴助手_v4.8.0.zip").write_text("zip", encoding="utf-8")
    (root / "_runtime_shared").mkdir()
    return root


def run(keep, protect=None):
    root = make_dist()
    moved = bp.retire_old_packages(root, keep, protect=protect)
    pkgs = sorted(p.name for p in root.iterdir()
                  if p.is_dir() and p.name.startswith("鸡驴助手_v"))
    extras = sorted(p.name for p in root.iterdir()
                    if p.is_dir() and not p.name.startswith("鸡驴助手_v"))
    shutil.rmtree(root, ignore_errors=True)
    return sorted(moved), pkgs, extras


print("[1] keep=2 + 保护当前包 → 只留最近两个")
moved, pkgs, extras = run(2, {"鸡驴助手_v4.8.0"})
check(moved == ["鸡驴助手_v4.5.0", "鸡驴助手_v4.6.0"], f"挪走最早的 2 个: {moved}")
check(pkgs == ["鸡驴助手_v4.7.0", "鸡驴助手_v4.8.0"], f"留下最近 2 个: {pkgs}")
check(all(x in extras for x in ["_old_鸡驴助手_v4.4.0_20260101", "_runtime_shared"]),
      f"`_old_` / `_runtime_shared` 原样保留: {extras}")

print("\n[2] keep=1 → 只留刚打的那个(当前包永不被挪走)")
moved, pkgs, extras = run(1, {"鸡驴助手_v4.8.0"})
check(pkgs == ["鸡驴助手_v4.8.0"], f"只剩当前包: {pkgs}")
check("鸡驴助手_v4.8.0" not in moved, "当前包不在挪走名单里")

print("\n[3] keep=0(默认) → 一个都不动")
moved, pkgs, extras = run(0)
check(moved == [] and len(pkgs) == 4, f"不动任何包(仍有 {len(pkgs)} 个)")

print("\n[4] keep 大于现有包数 → 不动")
moved, pkgs, extras = run(9, {"鸡驴助手_v4.8.0"})
check(moved == [] and len(pkgs) == 4, "包数不够就不挪")

print("\n[5] 挪走的目录落在 _retire_ 下, 且不删除内容(零删除语义)")
root = make_dist()
bp.retire_old_packages(root, 2, protect={"鸡驴助手_v4.8.0"})
retire_dirs = [p for p in root.iterdir() if p.name.startswith("_retire_")]
check(len(retire_dirs) == 1, f"生成一个回收目录: {[p.name for p in retire_dirs]}")
if retire_dirs:
    inside = sorted(p.name for p in retire_dirs[0].iterdir())
    check(inside == ["鸡驴助手_v4.5.0", "鸡驴助手_v4.6.0"], f"被挪的包完整躺在里面: {inside}")
    kept_file = retire_dirs[0] / "鸡驴助手_v4.5.0" / "app" / "xyfz_auto" / "common.py"
    check(kept_file.is_file(), "包内文件也一起搬过去了(没有丢东西)")
shutil.rmtree(root, ignore_errors=True)

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
