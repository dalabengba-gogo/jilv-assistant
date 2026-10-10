# -*- coding: utf-8 -*-
"""
打包绿色便携版 (portable)

用法 (双击 build.bat 即可, 或命令行):
    python tools/build_portable.py              生成 dist/鸡驴助手_vX.Y.Z/
    python tools/build_portable.py --zip        额外压成 dist/鸡驴助手_vX.Y.Z.zip
    python tools/build_portable.py --clean      写入前先清空同名目标目录
    python tools/build_portable.py --version 2.1.0   手动指定版本号(默认读 common.py)

产物结构 —— 与历史绿色版完全一致, 解压即用:
    鸡驴助手_vX.Y.Z/
    ├─ app/xyfz_auto/        源码本体(assets/templates, assets/tasks, tools/adb)
    ├─ runtime/              Python 3.13 + 依赖(onnxruntime/opencv/PySide6/numpy...)
    ├─ 启动助手.bat
    ├─ 启动助手(调试).bat
    ├─ 清理残留进程.bat
    ├─ 使用说明.txt
    └─ 跨电脑排查手册.txt

零绝对路径: 三个 bat 全部用 %~dp0 定位自身目录, 所以整个文件夹可以拷到任意盘符/任意电脑。
"""

import argparse
import os
import re
import shutil
import sys
import time
import zipfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "app"
VENDOR_RUNTIME = PROJECT_ROOT / "vendor" / "runtime"
LAUNCHER_DIR = PROJECT_ROOT / "assets" / "launcher"
DOCS_DIR = PROJECT_ROOT / "assets" / "docs"
DIST_DIR = PROJECT_ROOT / "dist"

# 打包时排除的目录(运行期产物 / 缓存 / 开发工具)
EXCLUDE_DIRS = {"__pycache__", "logs", "_collect", ".git", ".workbuddy"}
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log"}


def read_version() -> str:
    """从 app/xyfz_auto/common.py 读 APP_VERSION, 避免版本号多处不一致。"""
    f = APP_DIR / "xyfz_auto" / "common.py"
    m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', f.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("✗ 读不到 APP_VERSION: %s" % f)
    return m.group(1)


def copy_tree(src: Path, dst: Path, label: str):
    """按排除规则整树复制, 返回 (文件数, 字节数)。"""
    if not src.exists():
        raise SystemExit("✗ 缺少源目录: %s" % src)
    n = total = 0
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        rel = Path(root).relative_to(src)
        out = dst / rel if str(rel) != "." else dst
        out.mkdir(parents=True, exist_ok=True)
        for fn in files:
            if Path(fn).suffix.lower() in EXCLUDE_SUFFIX:
                continue
            s = Path(root) / fn
            shutil.copy2(s, out / fn)
            n += 1
            total += s.stat().st_size
    print("  ├─ %-10s %5d 个文件  %7.1f MB" % (label, n, total / 1048576))
    return n, total


def copy_files(src_dir: Path, dst_dir: Path, pattern: str, label: str):
    """复制顶层匹配文件(如 *.bat / *.txt)。"""
    dst_dir.mkdir(parents=True, exist_ok=True)
    n = total = 0
    for p in sorted(src_dir.glob(pattern)):
        shutil.copy2(p, dst_dir / p.name)
        n += 1
        total += p.stat().st_size
    print("  ├─ %-10s %5d 个文件  %7.1f MB" % (label, n, total / 1048576))
    return n, total


def validate(out: Path) -> list:
    """打包后自检: 关键件是否齐全 / 是否混入绝对路径 / 是否残留缓存。"""
    errs = []
    must = [
        out / "app" / "xyfz_auto" / "gui_main.py",
        out / "app" / "xyfz_auto" / "assets" / "ui_registry.yaml",
        out / "app" / "xyfz_auto" / "tools" / "adb" / "adb.exe",
        out / "runtime" / "python.exe",
        out / "runtime" / "pythonw.exe",
        out / "启动助手.bat",
        out / "启动助手(调试).bat",
        out / "清理残留进程.bat",
        out / "使用说明.txt",
        out / "跨电脑排查手册.txt",
    ]
    for p in must:
        if not p.exists():
            errs.append("缺少关键文件: %s" % p.relative_to(out))

    tpl = list((out / "app" / "xyfz_auto" / "assets" / "templates").glob("*.png"))
    if len(tpl) < 50:
        errs.append("模板数量异常: 只有 %d 个 (应 >=50)" % len(tpl))

    # bat 的编码/换行是硬伤: LF 换行会让 cmd 把 rem 注释里的中文词当命令执行,
    # UTF-8 + chcp 65001 会打乱 cmd 读取文件时的字节偏移 —— 两者都会让 bat 直接跑不起来。
    # 唯一安全组合: GBK(或纯 ASCII) + CRLF + 不切代码页。
    for bat in out.glob("*.bat"):
        txt = bat.read_text(encoding="utf-8", errors="ignore")
        if re.search(r"[A-Za-z]:\\(?!%~dp0)", txt):
            errs.append("%s 含硬编码绝对路径(跨电脑会失效)" % bat.name)
        raw = bat.read_bytes()
        stray_lf = raw.replace(b"\r\n", b"").count(b"\n")
        if not raw.count(b"\r\n") or stray_lf:
            errs.append("%s 换行符必须是 CRLF(有 %d 处裸 LF, cmd 会解析错乱)" % (bat.name, stray_lf))
        if b"chcp" in raw.lower():
            errs.append("%s 含 chcp 切代码页(会打乱 cmd 读取偏移)" % bat.name)
        try:
            raw.decode("gbk")
        except UnicodeDecodeError:
            errs.append("%s 非 GBK 编码(中文注释会变乱码)" % bat.name)

    stray = [str(p.relative_to(out)) for p in out.rglob("__pycache__")]
    if stray:
        errs.append("残留 __pycache__: %d 处" % len(stray))
    return errs


def make_zip(out: Path) -> Path:
    zp = out.parent / (out.name + ".zip")
    print("  压缩中 (约需 1~3 分钟, 130MB+ → 请稍候) ...")
    t0 = time.time()
    files = [p for p in out.rglob("*") if p.is_file()]
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for i, p in enumerate(files, 1):
            z.write(p, p.relative_to(out.parent))
            if i % 400 == 0:
                print("    %d/%d ..." % (i, len(files)))
    print("  ├─ zip        %5d 个文件  %7.1f MB  (%.0fs)"
          % (len(files), zp.stat().st_size / 1048576, time.time() - t0))
    return zp


def main():
    ap = argparse.ArgumentParser(description="打包鸡驴助手绿色便携版")
    ap.add_argument("--zip", action="store_true", help="额外压成 zip")
    ap.add_argument("--clean", action="store_true", help="写入前先清空同名目标")
    ap.add_argument("--version", help="手动指定版本号(默认读 common.py 的 APP_VERSION)")
    args = ap.parse_args()

    ver = args.version or read_version()
    out = DIST_DIR / ("鸡驴助手_v" + ver)

    print("=" * 60)
    print("  鸡驴助手 打包绿色便携版   版本 v%s" % ver)
    print("  源: %s" % PROJECT_ROOT)
    print("=" * 60)

    if out.exists() and args.clean:
        shutil.rmtree(out)
        print("  已清空旧产物: %s" % out.name)
    out.mkdir(parents=True, exist_ok=True)

    print("  组装中:")
    results = [
        copy_tree(APP_DIR, out / "app", "app"),
        copy_tree(VENDOR_RUNTIME, out / "runtime", "runtime"),
        copy_files(LAUNCHER_DIR, out, "*.bat", "bat"),
        copy_files(DOCS_DIR, out, "*.txt", "文档"),
    ]
    n = sum(r[0] for r in results)
    total = sum(r[1] for r in results)
    print("  └─ 合计      %5d 个文件  %7.1f MB" % (n, total / 1048576))

    print("  自检:")
    errs = validate(out)
    if errs:
        for e in errs:
            print("    ✗ " + e)
        raise SystemExit("\n打包失败, 请修正后重试。")
    print("    ✓ 关键文件齐全 / 无硬编码路径 / 无缓存残留")

    zp = make_zip(out) if args.zip else None
    print("=" * 60)
    print("  ✓ 完成: %s" % out)
    if zp:
        print("  ✓ 压缩包: %s" % zp)
    print("  双击里面的「启动助手.bat」即可运行。")
    print("=" * 60)


if __name__ == "__main__":
    main()
