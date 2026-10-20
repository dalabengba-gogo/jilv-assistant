# -*- coding: utf-8 -*-
"""发版一条龙 (2026-10-18 v4.7.0 批次四·工程 P2-3)

    python tools/release.py                 # 版本自检 → 回归 → 打包 → 产物自校验
    python tools/release.py --skip-tests    # 跳过回归(已跑过 / 赶时间)
    python tools/release.py --no-zip        # 不出 zip
    python tools/release.py --clean         # 旧产物先移出(透传给 build_portable)

为什么单独一层, 而不是把校验塞进 build_portable.py:
  build_portable 的职责是"把文件组装成产物", 已经够长了; 发版时真正容易出错的
  是**产出之后**那几步: 版本号漏改一处、zip 坏了一半、md5 对不上、忘了跑回归。
  这些事的共同点是"不影响打包成功, 但会导致别人拿到手的包是错的" —— 单独一层
  专门管这些 —— 坏掉的那一刻就当场拦下, 而不是等别人解压时才发现。

为什么只校验不清理:
  沿用 build_portable 定下的铁律 —— 构建脚本绝不删除已有的产物。dist 越攒越大
  这件事由报告末尾的占用清单告诉你(多少个正式包、多少个待清理旧包), 删不删由
  人决定。
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # v3/
APP = ROOT / "app" / "xyfz_auto"
DIST = ROOT / "dist"
BUILD = ROOT / "tools" / "build_portable.py"
DOCS = ROOT / "assets" / "docs" / "使用说明.txt"

VERSION_SPOTS = [
    (APP / "common.py",       r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', "common.py"),
    (APP / "__init__.py",     r'__version__\s*=\s*["\']([^"\']+)["\']', "__init__.py"),
    (APP / "README.md",       r'当前版本：v([\d.]+)',                    "README.md"),
]

CROSS = "—"
LINE = "=" * 66


def read_ver(path: Path, pat: str) -> str | None:
    try:
        m = re.search(pat, path.read_text(encoding="utf-8"))
        return m.group(1) if m else None
    except OSError:
        return None


def check_version() -> str:
    """四处版本号必须一致(第五处是 zip 里的 Readme, 由第一处派生)。"""
    print("[1/4] 版本号四处一致性")
    vals = {}
    for path, pat, label in VERSION_SPOTS:
        v = read_ver(path, pat)
        vals[label] = v
        print("      %-16s %s" % (label, v or "✘ 读不到"))
    try:
        txt = DOCS.read_text(encoding="utf-8")
        latest = re.findall(r"^v([\d.]+) \(\d{4}-", txt, re.M)
        vals["使用说明.txt"] = latest[0] if latest else None
        print("      %-16s %s (版本历史最新条目)"
              % ("使用说明.txt", vals["使用说明.txt"] or "✘ 读不到"))
    except OSError as e:
        vals["使用说明.txt"] = None
        print("      %-16s ✘ %s" % ("使用说明.txt", e))

    bad = [k for k, v in vals.items() if not v]
    if bad:
        raise SystemExit("✗ 读不到版本号: %s" % ", ".join(bad))
    uniq = set(vals.values())
    if len(uniq) != 1:
        detail = "  ".join(f"{k}={v}" for k, v in vals.items())
        raise SystemExit("✗ 版本号不一致！按约定必须同步四处:\n      " + detail)
    ver = uniq.pop()
    print("      ✔ 四处一致: v%s" % ver)
    return ver


def run_tests() -> bool:
    print("\n[2/4] 回归测试")
    t0 = time.time()
    cp = subprocess.run(
        [sys.executable, str(APP / "scripts" / "run_tests.py")],
        cwd=str(APP))
    ok = cp.returncode == 0
    print("      %s  (%.0fs)" % ("✔ 全部通过" if ok else "✘ 有失败", time.time() - t0))
    return ok


def build(extra: list) -> Path:
    print("\n[3/4] 打包绿色便携版")
    t0 = time.time()
    cp = subprocess.run([sys.executable, str(BUILD)] + extra, cwd=str(ROOT))
    if cp.returncode != 0:
        raise SystemExit("✗ 打包失败(退出码 %s)" % cp.returncode)
    print("      ✔ 打包完成 (%.0fs)" % (time.time() - t0))
    return DIST


def md5_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def verify(ver: str, want_zip: bool) -> list:
    """产物自校验 —— 打包成功不等于别人能正常使用"""
    print("\n[4/4] 产物自校验")
    errs = []
    out = DIST / ("鸡驴助手_v" + ver)
    if not out.is_dir():
        return ["产物目录不存在: %s" % out]

    must = ["app/xyfz_auto/gui_main.py", "runtime/python.exe",
            "runtime/pythonw.exe", "启动助手.bat", "使用说明.txt"]
    for rel in must:
        if not (out / rel).exists():
            errs.append("缺关键件: %s" % rel)
    miss = [e for e in errs if e.startswith("缺")]
    print("      %s 关键件 %d/%d 项齐全"
          % ("✔" if not miss else "✘", len(must) - len(miss), len(must)))

    # 账号表与本机设置: accounts.json 该有(方便拷到自己别的电脑),
    # gui_settings/gui_timer 不该有(个人状态, 发出去等于泄露)
    acc = out / "app" / "xyfz_auto" / "accounts.json"
    print("      %s accounts.json %s"
          % ("✔" if acc.exists() else "⚠", "随包携带(拷到自己电脑直接可用)"
             if acc.exists() else "没随包, 首次运行会重建空表"))
    for leak in ("gui_settings.json", "gui_timer.json"):
        p = out / "app" / "xyfz_auto" / leak
        if p.exists():
            errs.append("产物里混进了个人文件 %s(本机设置, 不该外发)" % leak)

    if not want_zip:
        return errs

    zp = DIST / ("鸡驴助手_v" + ver + ".zip")
    if not zp.is_file():
        errs.append("没产出 zip: %s" % zp.name)
        return errs

    # ① zip 本身完好吗 —— 很多人是拷到别的电脑才发现解压到一半报错
    if not zipfile.is_zipfile(zp):
        errs.append("zip 文件不可用: %s" % zp.name)
    else:
        with zipfile.ZipFile(zp) as z:
            bad = z.testzip()
            if bad:
                errs.append("zip 内有损坏条目: %s" % bad)
            names = z.namelist()
        top = {n.split("/")[0] for n in names}
        if out.name not in top:
            errs.append("zip 顶层目录不是 %s, 实测 %s —— 解压后结构会套错层"
                        % (out.name, top))

    # ② md5 对得上吗 —— 校验文件写错等于没校验
    md5p = DIST / ("鸡驴助手_v" + ver + ".md5.txt")
    actual = md5_of(zp)
    if not md5p.is_file():
        errs.append("缺 md5 校验文件")
    else:
        txt = md5p.read_text(encoding="utf-8", errors="replace")
        if actual not in txt.lower():
            errs.append("md5 校验值与 zip 不符(文件可能被改动过)")
        else:
            print("      ✔ md5 与 zip 一致")

    raw = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print("      ✔ zip 完好  %.1f MB / 原始 %.1f MB (省 %.0f%%)"
          % (zp.stat().st_size / 1048576, raw / 1048576,
             (1 - zp.stat().st_size / raw) * 100))
    return errs


def size_report():
    if not DIST.is_dir():
        return
    total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    pkgs = sorted(p for p in DIST.iterdir()
                  if p.is_dir() and p.name.startswith("鸡驴助手_v"))
    olds = sorted(p for p in DIST.iterdir() if p.name.startswith("_old_"))
    print("\n      dist 合计 %.1f GB  正式包 %d 个  待清理旧包(_old_) %d 个"
          % (total / 1073741824, len(pkgs), len(olds)))
    if len(pkgs) > 3:
        print("      ⚠ 攒了 %d 个正式包 —— 确认不用后可在资源管理器里手动删除,"
              "本脚本不擅自动手。" % len(pkgs))


def main():
    ap = argparse.ArgumentParser(description="版本自检 → 回归 → 打包 → 产物自校验")
    ap.add_argument("--skip-tests", action="store_true", help="跳过回归")
    ap.add_argument("--no-zip", action="store_true", help="不出 zip")
    ap.add_argument("--clean", action="store_true", help="打包前先移走旧产物")
    # 2026-10-19 v4.8.0: 透传 build_portable 的 --keep(零删除: 旧包挪到
    # dist/_retire_<时间戳>/, 不删任何东西)。默认 0 = 不动 dist, 与旧行为一致。
    ap.add_argument("--keep", type=int, default=0, metavar="N",
                    help="只保留最近 N 个正式包(0=不动, 旧包挪到 dist/_retire_* 而非删除)")
    args = ap.parse_args()

    print(LINE)
    t0 = time.time()
    ver = check_version()

    if args.skip_tests:
        print("\n[2/4] 回归测试       —— 已按 --skip-tests 跳过")
    elif not run_tests():
        raise SystemExit(
            "\n✗ 回归没过, 不发版。\n"
            "  要么修到绿, 要么确认是本机没有模拟器(skipped 不算失败)后加 --skip-tests。")

    extra = []
    if args.no_zip:
        extra.append("--no-zip")
    if args.clean:
        extra.append("--clean")
    if args.keep:
        extra += ["--keep", str(args.keep)]
    dist = build(extra)

    errs = verify(ver, not args.no_zip)
    size_report()

    print("\n" + LINE)
    if errs:
        print("  ✘ 产物有问题, 别发出去:")
        for e in errs:
            print("     - " + e)
        print(LINE)
        return 1
    print("  ✔ v%s 发版就绪   %s" % (ver, dist / ("鸡驴助手_v" + ver)))
    print("    总耗时 %.0fs" % (time.time() - t0))
    print(LINE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
