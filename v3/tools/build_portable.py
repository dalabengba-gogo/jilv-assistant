# -*- coding: utf-8 -*-
"""
打包绿色便携版 (portable)

用法 (双击 build.bat 即可, 或命令行):
    python tools/build_portable.py              生成 dist/鸡驴助手_vX.Y.Z/ + 同名 zip + 校验值
    python tools/build_portable.py --no-zip     只要目录, 不压 zip
    python tools/build_portable.py --clean      写入前先把同名旧产物移走(不删除)
    python tools/build_portable.py --version 2.1.0   手动指定版本号(默认读 common.py)
    python tools/build_portable.py --no-trim    不裁剪 runtime(默认裁剪, 详见 RUNTIME_TRIM_PREFIXES)

2026-10-20 新增 runtime 裁剪(默认开启):
    单包 332MB 里 runtime 占 324MB(97.6%), 其中约 41MB 是**运行时根本用不到**的东西
    (pip / 安装残留 / 编译头文件 / opencv 的视频编解码插件)。这些在**复制阶段就跳过**,
    不做"先拷再删" —— 与打包脚本一贯的"零删除"约定一致, 也不会触发批量删除守卫。
    裁剪后用产物里的 runtime 真跑一次冒烟(导入 cv2/numpy/onnxruntime/rapidocr/PySide6
    + 解一张模板 + 模板匹配), 失败即打包失败, 保证发出去的包一定能跑。


2026-10-18 约定: 每次版本变更后都会自动产出
    1. dist/鸡驴助手_vX.Y.Z/            绿色版目录
    2. dist/鸡驴助手_vX.Y.Z.zip         压缩包(方便拷去别的电脑)
    3. dist/鸡驴助手_vX.Y.Z.md5.txt     官方校验值(MD5 + SHA256, 让别人能验证包有没有被改过)

产物结构 —— 与历史绿色版完全一致, 解压即用:
    鸡驴助手_vX.Y.Z/
    ├─ app/xyfz_auto/        源码本体(assets/templates, assets/tasks, tools/adb)
    ├─ runtime/              Python 3.13 + 依赖(onnxruntime/opencv/PySide6/numpy...)
    ├─ 启动助手.bat
    ├─ 启动助手(调试).bat
    ├─ 验证完整性.bat        ← 校验下载的压缩包是否被改过
    ├─ 清理残留进程.bat
    ├─ 使用说明.txt
    └─ 跨电脑排查手册.txt

零绝对路径: 三个 bat 全部用 %~dp0 定位自身目录, 所以整个文件夹可以拷到任意盘符/任意电脑。
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
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
# 2026-10-18: 补 shots/ —— 它是"截图"按钮的输出目录, 属于用户自己的
#   运行产物, 不该进分发包(以前只在「外发前清理.bat」里漏掉它)。
EXCLUDE_DIRS = {"__pycache__", "logs", "_collect", "shots", ".git", ".workbuddy",
                # 2026-10-18 v4.7.0: 回归报告。scripts/run_tests.py 会把 JUnit XML
                # 写到 v3/_reports/(工程根, 打不到这里); 万一有人改了它那个
                # parents[3] 写成 parents[2], 报告就会落在 app/_reports 并被整棵
                # 拷进绿色版 —— CI 产物跟 programa 一起外发不合适, 这里兜一道。
                "_reports"}   # ← CI 报告目录, 详见上方说明
EXCLUDE_SUFFIX = {".pyc", ".pyo", ".log"}
# 2026-10-16 v3.0.0: 打包时排除的**个人运行时文件**。
# 以前绿色版里会混进 gui_settings.json(免责声明"不再显示"标记)和
# gui_timer.json(本机定时任务), 拷给别人等于把你的设置一起发出去。
# accounts.json(账号表)保留 —— 拷到自己别的电脑时账号能直接带过去;
# 要外发的话手动删掉它即可(首次运行会自动重建空表)。
EXCLUDE_FILES = {"gui_settings.json", "gui_timer.json"}

# 2026-10-20: runtime 裁剪白名单(相对 runtime/ 的路径前缀, 复制时直接跳过)。
# 实测这些在绿色版运行时**完全用不到**, 合计约 41MB(单包 332MB → ~290MB, -12.5%):
#   · Lib/site-packages/pip            5.3MB  pip 本体 + pip-*.dist-info, 运行时不装包
#   · Lib/site-packages/~nn*           1.9MB  onnxruntime 安装中断留下的残留目录
#   · cv2/opencv_videoio_ffmpeg*.dll  29.4MB 视频编解码插件; 本项目只用 imread 做模板匹配,
#                                           全项目无 VideoCapture/VideoWriter(grep 已确认)
#   · Lib/ensurepip + venv + pydoc_data 2.7MB 建虚拟环境/看 pydoc 才用
#   · include + libs                   2.2MB C 头文件与导入库, 只在编译 C 扩展时需要
# ⚠ 不能删(删了直接起不来/跑不了): cv2.pyd(82MB)、numpy、onnxruntime、PIL、rapidocr、
#   PySide6 的 resources/icudtl.dat(10MB, Qt6Core 必需)、plugins/platforms。
RUNTIME_TRIM_PREFIXES = (
    "Lib/site-packages/pip",           # pip 本体 + pip-*.dist-info
    "Lib/site-packages/~nn",           # onnxruntime 安装残留(目录名以 ~ 开头)
    "Lib/site-packages/cv2/opencv_videoio_ffmpeg500_64.dll",
    "Lib/ensurepip",
    "Lib/venv",
    "Lib/pydoc_data",
    "include",
    "libs",
)


def _skip_by_prefix(rel_posix: str, prefixes) -> bool:
    """rel_posix 是否命中裁剪前缀之一(目录前缀, 区分大小写)。"""
    if not prefixes:
        return False
    return any(rel_posix == p or rel_posix.startswith(p) for p in prefixes)


def read_version() -> str:
    """从 app/xyfz_auto/common.py 读 APP_VERSION, 避免版本号多处不一致。"""
    f = APP_DIR / "xyfz_auto" / "common.py"
    m = re.search(r'APP_VERSION\s*=\s*["\']([^"\']+)["\']', f.read_text(encoding="utf-8"))
    if not m:
        raise SystemExit("✗ 读不到 APP_VERSION: %s" % f)
    return m.group(1)


def copy_tree(src: Path, dst: Path, label: str, skip_prefixes=()):
    """按排除规则整树复制, 返回 (文件数, 字节数)。

    skip_prefixes: 额外的相对路径前缀(如 runtime 裁剪白名单), 命中即跳过不复制。
    """
    if not src.exists():
        raise SystemExit("✗ 缺少源目录: %s" % src)
    n = total = skipped = skip_n = 0
    dst.mkdir(parents=True, exist_ok=True)
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        rel = Path(root).relative_to(src)
        if skip_prefixes:
            kept = []
            for d in dirs:
                rp = (rel / d).as_posix()
                if _skip_by_prefix(rp, skip_prefixes):
                    skipped += _dir_bytes(Path(root) / d)
                    skip_n += 1
                else:
                    kept.append(d)
            dirs[:] = kept
        out = dst / rel if str(rel) != "." else dst
        out.mkdir(parents=True, exist_ok=True)
        for fn in files:
            if Path(fn).suffix.lower() in EXCLUDE_SUFFIX:
                continue
            if fn in EXCLUDE_FILES:
                continue
            if skip_prefixes and _skip_by_prefix((rel / fn).as_posix(), skip_prefixes):
                try:
                    skipped += (Path(root) / fn).stat().st_size
                except OSError:
                    pass
                skip_n += 1
                continue
            s = Path(root) / fn
            shutil.copy2(s, out / fn)
            n += 1
            total += s.stat().st_size
    tail = ""
    if skip_prefixes and skipped:
        tail = "  (裁剪跳过 %d 项 %.1f MB)" % (skip_n, skipped / 1048576)
    print("  ├─ %-10s %5d 个文件  %7.1f MB%s" % (label, n, total / 1048576, tail))
    return n, total


def _dir_bytes(p: Path) -> int:
    """目录内所有文件的合计字节(用于统计被裁剪掉的体积)。"""
    s = 0
    for root, _dirs, files in os.walk(p):
        for fn in files:
            try:
                s += (Path(root) / fn).stat().st_size
            except OSError:
                pass
    return s


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
        # 2026-10-18: 让别人能验证下载的压缩包有没有被改过
        out / "验证完整性.bat",
        out / "app" / "xyfz_auto" / "scripts" / "verify_zip.py",
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

    # 2026-10-17 加: 个人运行时文件。copy_tree 已排除它们, 但便携版**跑过一次**
    # 就会重新长出 gui_settings.json(免责声明"不再显示")/ accounts.json(本机端口表)
    # / logs(含游戏截图)。外发前若不清理, 等于把"我已同意声明"和自己的账号表
    # 一起发出去 —— 与声明第 7 条"对外分发须完整保留本声明"冲突。
    # 分两类: gui_settings/gui_timer 是**本就排除**的文件, 出现即打包逻辑出错 → 阻断;
    # accounts.json 是刻意保留的(拷到自己别的电脑时账号直接带过去), logs/ 是运行
    # 产物 —— 这两个只在**外发**时才需要清, 因此给警告不阻断, 免得每次打包都失败。
    app = out / "app" / "xyfz_auto"
    for name in ("gui_settings.json", "gui_timer.json"):
        if (app / name).exists():
            errs.append("混入个人配置 %s (copy_tree 应已排除, 检查排除规则)" % name)
    for name, why in (("accounts.json", "本机端口账号表"),
                      ("logs", "运行日志与游戏截图"),
                      ("_collect", "模板采集临时文件")):
        if (app / name).exists():
            print("    ! 提示: 产物含 %s(%s) —— 自己用无妨, 拷给别人前请先运行「外发前清理.bat」"
                  % (name, why))

    stray = [str(p.relative_to(out)) for p in out.rglob("__pycache__")]
    if stray:
        errs.append("残留 __pycache__: %d 处" % len(stray))

    # 版本号四处同步: 一直靠人肉同步, 漏一处用户就会打开旧版本还以为没改。
    # 以 common.py 的 APP_VERSION 为唯一权威, 校验产物里另外三处。
    # 2026-10-18 修(P1-8): 两个 README 的「当前版本」**文档里写着要同步、脚本却从没
    #   校验过** —— app/xyfz_auto/README.md 就悄悄停在 v3.9.0 滞后了两个大版本。
    #   现已补进清单, 从此漏一处打包直接失败。
    try:
        ver = read_version()
        checks = [
            (app / "__init__.py", '__version__ = "%s"' % ver),
            (out / "使用说明.txt", None),
            (app / "README.md", "**当前版本：v%s**" % ver),
        ]
        for path, needle in checks:
            if not path.exists():
                errs.append("版本校验缺少文件: %s" % path.name)
                continue
            txt = path.read_text(encoding="utf-8", errors="ignore")
            ok = (needle in txt) if needle else (("v%s " % ver) in txt or ("v%s (" % ver) in txt)
            if not ok:
                errs.append("版本号不同步: %s 里找不到 v%s" % (path.name, ver))
    except Exception as e:
        errs.append("版本校验失败: %s" % e)
    return errs


def smoke_test(out: Path) -> list:
    """裁剪后冒烟: 用**产物里的** runtime 真跑一遍核心库导入 + 解模板 + 模板匹配。

    只靠"文件看着齐"不够 —— 裁掉 ffmpeg 视频插件/编译头之后必须确认 cv2 仍能 import、
    模板仍能解码匹配, 否则发出去的包就是坏的。失败即视为打包失败。
    """
    errs = []
    py = out / "runtime" / "python.exe"
    if not py.exists():
        return ["冒烟失败: 产物缺少 runtime/python.exe"]
    tpl_dir = out / "app" / "xyfz_auto" / "assets" / "templates"
    tpl = next(iter(sorted(tpl_dir.glob("*.png"))), None) if tpl_dir.exists() else None
    if tpl is None:
        return ["冒烟失败: 找不到任何模板 png"]

    code = (
        "import sys\n"
        "import numpy as np\n"
        "import cv2, onnxruntime, rapidocr_onnxruntime\n"
        "from PySide6 import QtCore, QtGui, QtWidgets\n"
        "buf = np.fromfile(sys.argv[1], dtype=np.uint8)\n"
        "img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)\n"
        "assert img is not None and img.size > 0, 'template decode failed'\n"
        "r = cv2.matchTemplate(img, img, cv2.TM_CCOEFF_NORMED)\n"
        "assert float(r.max()) > 0.99, 'matchTemplate failed'\n"
        "print('SMOKE_OK', cv2.__version__)\n"
    )
    try:
        r = subprocess.run([str(py), "-c", code, str(tpl)],
                           capture_output=True, text=True, timeout=180)
    except Exception as e:
        return ["冒烟失败(无法启动产物 runtime): %s" % e]
    if r.returncode != 0 or "SMOKE_OK" not in (r.stdout or ""):
        msg = ((r.stderr or "") + (r.stdout or "")).strip()
        errs.append("冒烟失败(产物 runtime 跑不起来): %s" % msg[-500:])
    return errs


def _dir_size_mb(d: Path) -> float:
    """目录里所有文件的合计体积(MB)。"""
    try:
        return sum(p.stat().st_size for p in d.rglob("*") if p.is_file()) / 1048576
    except OSError:
        return 0.0


def sanitize_for_share(out: Path) -> list:
    """打包后、压缩前的「产物卫生」。

    打包规则本来就排除 logs/_collect/shots/gui_settings/gui_timer(见 EXCLUDE_*),
    但**直接在 dist 的产物目录里跑过一次**，这些文件就会重新长出来:
      · logs  —— 运行日志 + 游戏截图, 实测单个包能到 235MB;
      · gui_settings.json —— 免责声明"不再显示"标记;
      · gui_timer.json    —— 本机定时任务清单;
      · shots/_collect    —— 用户自己的截图与模板采集临时文件。
    zip 是要拷去别的电脑的, 这些个人运行产物必须留在本地、不带走。

    ⚠ 这里是**移走(rename)而不是删除**: 一来不需要删除权限(受限环境下批量删除会被
    拦截, 实测一次 4212 个文件就被拦得打包直接中止); 二来万一里面还有没导出的
    排障截图, 移走还能捞回来。垃圾统一落在 `dist/_trash_<包名>_<时间戳>/`,
    确认新包无误后整个删掉即可。

    返回 [(名称, 体积MB), ...]。
    """
    app = out / "app" / "xyfz_auto"
    stamp = time.strftime("%Y%m%d_%H%M%S")
    trash = out.parent / ("_trash_" + out.name + "_" + stamp)
    moved = []

    def _move(src: Path, label: str):
        if not src.exists():
            return
        mb = _dir_size_mb(src) if src.is_dir() else src.stat().st_size / 1048576
        try:
            trash.mkdir(parents=True, exist_ok=True)
            src.rename(trash / label)
            moved.append((label, mb))
        except OSError as e:
            print("    ! 跳过(移不走) %s: %s" % (label, e))

    for name in ("logs", "_collect", "shots"):
        _move(app / name, name)
    for name in ("gui_settings.json", "gui_timer.json"):
        _move(app / name, name)
    # 空目录要留着: 截图/采集功能直接往里写, 目录不在会报错
    (app / "shots").mkdir(parents=True, exist_ok=True)
    return moved


def file_digests(path: Path):
    """算文件的 MD5 + SHA256 + 字节数(分块读, 130MB 的包几秒就好)。"""
    md5 = hashlib.md5()
    sha = hashlib.sha256()
    size = 0
    with open(str(path), "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            md5.update(chunk)
            sha.update(chunk)
            size += len(chunk)
    return md5.hexdigest(), sha.hexdigest(), size


def write_checksum(zp: Path, ver: str) -> Path:
    """在压缩包旁边生成一份**官方校验值**, 让别人能验证包有没有被人改过。

    ⚠ 这条最容易被做错: 把校验值塞进压缩包**内部**等于让小偷自己改分数, 毫无意义。
    所以它必须是独立的一份文件; 更强一点的做法是发布方通过另一个渠道
    (发布页/群公告) 直接把 MD5 发给你, 那样连"包和校验文件一起被换掉"也能防住。
    """
    md5, sha, size = file_digests(zp)
    side = zp.parent / (zp.stem + ".md5.txt")
    txt = "\n".join([
        "# 鸡驴助手 绿色版 校验值",
        "# 版本: v%s" % ver,
        "# 打包时间: %s" % time.strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "文件: %s" % zp.name,
        "大小: %d 字节 (%.2f MB)" % (size, size / 1048576.0),
        "MD5:   %s" % md5,
        "SHA256: %s" % sha,
        "",
        "【怎么验证】",
        "方法1(推荐): 解压后双击里面的「验证完整性.bat」, 会自动与上面的值比对。",
        "方法2(不用解压): 在压缩包所在目录打开 PowerShell, 执行:",
        '    (Get-FileHash "%s" -Algorithm MD5).Hash' % zp.name,
        "  把结果与上面的 MD5 对比(不区分大小写)即可。",
        "",
        "【注意】",
        "- 把本文件和压缩包一起发给别人时, 建议 MD5 另外走一个渠道再发一次",
        "  (比如包放网盘、MD5 直接发消息)。两份东西在同一个地方的话,",
        "  被一起换掉就看不出问题了。",
        "- MD5/SHA256 只能证明「这个包和发布时的字节完全一致」, 也就是能发现",
        "  被改过或下载坏了; 它不是防伪证书, 防不住发布方自己作恶。",
        "",
    ])
    side.write_text(txt, encoding="utf-8")
    return side


def make_zip(out: Path, level: int = 6) -> Path:
    """把产物目录压成 zip(默认每次打包后自动执行)。

    用 zip 而不是 7z: 解压端零依赖 —— Windows 资源管理器原生支持,
    拷到没装任何压缩软件的电脑上也能直接解压运行, 这才是"方便去别的电脑测试"。
    """
    zp = out.parent / (out.name + ".zip")
    if zp.exists():
        zp.unlink()
    print("  压缩中 (约需 1~3 分钟, 请稍候) ...")
    t0 = time.time()
    files = [p for p in out.rglob("*") if p.is_file()]
    raw = sum(p.stat().st_size for p in files) / 1048576
    with zipfile.ZipFile(zp, "w", zipfile.ZIP_DEFLATED, compresslevel=level) as z:
        for i, p in enumerate(files, 1):
            z.write(p, p.relative_to(out.parent))
            if i % 400 == 0:
                print("    %d/%d ..." % (i, len(files)))
    size = zp.stat().st_size / 1048576
    print("  ├─ zip        %5d 个文件  %7.1f MB  (原 %.0f MB, 省 %.0f%%, %.0fs)"
          % (len(files), size, raw, (1 - size / raw) * 100 if raw else 0,
             time.time() - t0))
    return zp


def _pkg_mtime(p: Path) -> float:
    """包的时间序: 优先看它里面 app/xyfz_auto/common.py 的修改时间(版本号一动它必变),
    拿不到才退回目录自身的 mtime —— 只看目录时间容易被"只补了几个文件"弄乱。"""
    for rel in ("app/xyfz_auto/common.py", "app/xyfz_auto/__init__.py"):
        f = p / rel
        try:
            if f.is_file():
                return f.stat().st_mtime
        except OSError:
            continue
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def retire_old_packages(dist: Path, keep: int, protect: set[str] | None = None) -> list[str]:
    """把多余的旧版包挪出 dist(2026-10-19 v4.8.0, A-2)

    dist 是**母版仓库**, 长期运行的那个包在 `D:\\鸡驴助手_运行`(产物/运行分离),
    所以留最近 N 个就够回退了; 攒到 16 个纯属占盘。

    三条安全约束(与 --clean 同源, 都是为了"绝不悄悄删东西"):
      ① 只挪**正式包**(`鸡驴助手_vX.Y.Z`), 且必须像个真包(app / runtime 至少有
         一个在里面); `_old_` / `_trash_` / `_retire_` / `_runtime_shared` 一律不碰。
      ② **刚打完的那个包永不挪**(protect) —— 同一份源码反复打包时 common.py 的
         mtime 可能相同, 纯按时间排序有把自己判成最旧的风险。
      ③ 正在运行的包挪不动(Windows 会拒绝), catch 住跳过并提示, 不影响本次结果。
      ④ 零删除: 统一改名到 `dist/_retire_<时间戳>/`, 用户确认后手动删。
    """
    if keep < 1:
        return []
    protect = protect or set()
    try:
        pkgs = [p for p in dist.iterdir()
                if p.is_dir() and p.name.startswith("鸡驴助手_v")]
    except OSError:
        return []
    pkgs = [p for p in pkgs if (p / "app").is_dir() or (p / "runtime").is_dir()]
    # keep 说的是"总共留几个包" —— 受保护的(刚打的)那个占一个名额,
    # 否则 keep=1 会变成"当前包 + 1 个旧的", 与字面意思不符。
    n_protected = sum(1 for p in pkgs if p.name in protect)
    keep_others = max(0, keep - n_protected)
    others = [p for p in pkgs if p.name not in protect]
    if len(others) <= keep_others:
        return []
    others.sort(key=_pkg_mtime)
    old = others[: len(others) - keep_others]
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest_root = dist / ("_retire_" + stamp)
    moved, failed = [], []
    for p in old:
        try:
            dest_root.mkdir(parents=True, exist_ok=True)
            p.rename(dest_root / p.name)
            moved.append(p.name)
        except OSError as e:
            # 多半是被正在运行的助手占着 —— dist 里就地跑过一次就会这样,
            # 跳过它不影响本次打包, 退出那个实例后再跑一次 --keep 即可。
            failed.append((p.name, str(e)))
    print("  旧包回收(--keep %d):" % keep)
    for nm in moved:
        print("    ├─ 已挪出 %s" % nm)
    if failed:
        for nm, msg in failed:
            print("    ✗ %s 挪不动(多半正在运行, 已跳过): %s" % (nm, msg))
    if moved:
        print("    └─ 共 %d 个 → %s  (确认无误后可整个删除该目录回收空间)"
              % (len(moved), dest_root.name))
    return moved


def main():
    ap = argparse.ArgumentParser(description="打包鸡驴助手绿色便携版")
    # 2026-10-18 约定: 每个版本变更后都自动出一个 zip, 方便拷去别的电脑测试。
    #   所以压缩是**默认行为**, 想跳过才显式 --no-zip —— 免得每次都忘。
    ap.add_argument("--no-zip", action="store_true",
                    help="不生成 zip(默认每次打包后自动生成)")
    ap.add_argument("--zip-level", type=int, default=6,
                    help="zip 压缩级别 1~9(默认 6, 越大越慢)")
    ap.add_argument("--clean", action="store_true", help="写入前先清空同名目标")
    ap.add_argument("--version", help="手动指定版本号(默认读 common.py 的 APP_VERSION)")
    # 2026-10-19 v4.8.0 (A-2): dist 攒太多时用它回收。
    #   实测 dist 到 7.2GB / 16 个正式包(每份都带 337MB runtime), 而日常在跑的
    #   其实只有一个 `D:\鸡驴助手_运行`。保留最近 N 个够了。
    #   ⚠️ 仍是**零删除**: 超出的包被改名挪到 dist/_retire_<时间戳>/(和 --clean
    #   挪 _old_* 一个路数), 确认新包没问题后自己在资源管理器里删掉那个目录。
    #   默认 0 = 一个都不动, 与旧行为完全一致。
    ap.add_argument("--keep", type=int, default=0, metavar="N",
                    help="只保留最近 N 个正式包, 更早的挪到 dist/_retire_<时间戳>/(0=不动, 默认)")
    # 2026-10-20: 默认裁剪 runtime(省约 41MB/包, -12.5%)。需要完整 runtime 时显式 --no-trim。
    ap.add_argument("--no-trim", action="store_true",
                    help="不裁剪 runtime(默认裁剪 pip/视频插件/include 等运行时用不到的大件)")
    args = ap.parse_args()

    ver = args.version or read_version()
    out = DIST_DIR / ("鸡驴助手_v" + ver)

    print("=" * 60)
    print("  鸡驴助手 打包绿色便携版   版本 v%s" % ver)
    print("  源: %s" % PROJECT_ROOT)
    print("=" * 60)

    if out.exists() and args.clean:
        # 2026-10-18: 原来是 rmtree 直接删 —— 实测在受限环境里会被"批量删除保护"
        #   拦下(一次 4212 个文件 > 阈值), 打包直接胎死腹中; 而且就地跑过的旧包里
        #   可能有还没导出的排障截图, 删了找不回来。
        #   改成**改名移走**: 不删任何东西, 也不需要删除权限, 打包永远跑得动;
        #   确认新包没问题后, 用户自己在资源管理器里删掉 _old_* 即可。
        ts = time.strftime("%Y%m%d_%H%M%S")
        retired = out.parent / ("_old_" + out.name + "_" + ts)
        try:
            out.rename(retired)
            print("  旧产物已移出: %s  (确认新包无误后可整个删除)" % retired.name)
        except OSError as e:
            raise SystemExit(
                "✗ 无法移走旧产物 %s: %s\n  (多半是被正在运行的助手占用, 请先退出程序)"
                % (out.name, e))
    out.mkdir(parents=True, exist_ok=True)

    trim = not args.no_trim
    print("  组装中:" + ("" if trim else "  (--no-trim: 完整 runtime)"))
    results = [
        copy_tree(APP_DIR, out / "app", "app"),
        copy_tree(VENDOR_RUNTIME, out / "runtime", "runtime",
                  skip_prefixes=RUNTIME_TRIM_PREFIXES if trim else ()),
        copy_files(LAUNCHER_DIR, out, "*.bat", "bat"),
        copy_files(DOCS_DIR, out, "*.txt", "文档"),
    ]
    n = sum(r[0] for r in results)
    total = sum(r[1] for r in results)
    print("  └─ 合计      %5d 个文件  %7.1f MB" % (n, total / 1048576))

    # 压缩前先做产物卫生: 清掉就地运行长出来的个人文件(logs/截图/本机配置)。
    # 放在自检**之前**, 这样自检看到的也是干净产物, 不会误报"混入 logs"。
    cleaned = sanitize_for_share(out)
    if cleaned:
        print("  产物卫生(已移出, 未删除):")
        for name, mb in cleaned:
            print("    ├─ %-20s %.1f MB" % (name, mb))
        print("    └─ 合计 %.1f MB → dist/_trash_* (确认新包无误后可整个删除)"
              % sum(mb for _, mb in cleaned))

    print("  自检:")
    errs = validate(out)
    if trim:
        serrs = smoke_test(out)
        if serrs:
            errs += serrs
        else:
            print("    ✓ 裁剪冒烟: 产物 runtime 可导入 cv2/numpy/onnxruntime/rapidocr/PySide6"
                  " + 模板解码匹配正常")
    if errs:
        for e in errs:
            print("    ✗ " + e)
        raise SystemExit("\n打包失败, 请修正后重试。")
    print("    ✓ 关键文件齐全 / 无硬编码路径 / 无缓存残留")

    if args.keep:
        retire_old_packages(DIST_DIR, args.keep, protect={out.name})

    zp = None if args.no_zip else make_zip(out, level=args.zip_level)
    side = write_checksum(zp, ver) if zp else None
    print("=" * 60)
    print("  ✓ 完成: %s" % out)
    if zp:
        print("  ✓ 压缩包: %s  (拷到别的电脑解压即可, 无需安装任何东西)" % zp.name)
    if side:
        print("  ✓ 校验值: %s  (给别人时和 zip 一起发, MD5 最好再单独发一次)"
              % side.name)
    print("  双击里面的「启动助手.bat」即可运行。")
    # 长期跑的那个包建议拷出 dist: dist 只当干净母版, 免得运行日志把它撑大
    print("  ! 提示: 长期运行请先整个拷出 dist 目录, 别在 dist 里直接跑。")
    print("=" * 60)


if __name__ == "__main__":
    main()
