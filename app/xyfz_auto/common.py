# -*- coding: utf-8 -*-
"""公共工具：配置加载、图片解码、日志"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

import cv2
import numpy as np
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent  # xyfz_auto/

APP_NAME = "鸡驴助手"

# 版本号规则 (2026-10-09 约定): 主.次.修 —— **次版本号到 9 就进位**(主 +1, 次归 0),
#   例: 1.9.x → 2.0.0 ; 2.9.x → 3.0.0
#   修(第三位)只管同一批次内的小修补, 不设上限也不进位。
# 改版本号时同步这三处: common.py 本行 / __init__.py 的 __version__ / 使用说明.txt 版本历史
# (历史上这三处长期不一致: common.py 已是 1.10.3 时 __init__.py 还停在 1.6.2, 以 common.py 为准)
APP_VERSION = "2.0.6"        # v2.0.0 —— ① 每日必领段⑤ 盈福大厅签到: 实机采集 5 个模板 + 注册
                             #   2 界面/3 按钮, 修掉「关奖励弹窗」白等一次超时、返回次数多写一次;
                             # ② engine/runner 补写法/注册表错误兜底(ValueError 等), 单个步骤
                             #   写错不再炸掉整条任务, on_fail 终于生效;
                             # ③ 新增 scripts/check_refs.py —— 事前扫出"引用了不存在的按钮/
                             #   locate 没配模板"这类问题(本次事故正是这一类);
                             # ④ 界面归位: 识别模板库挪进「实时画面」页(取点→采集同页闭环),
                             #   脚本的打开/新建/保存挪进「脚本(YAML 可编辑)」页
                             # v2.0.1 —— ① 新建开发源码工程 D:\鸡驴助手_dev(源码与 runtime 分离,
                             #   vendor\runtime 331MB 不进 git), tools\build_portable.py 一键打包绿色版
                             #   (版本号自动读本行, 带 10 项关键件 + bat 编码自检);
                             # ② 修 bat 启动乱码: 我曾把 bat 写成 UTF-8+LF, cmd 只认 CRLF, 且 chcp
                             #   会打乱字节偏移 → 报「鎵撳寘 不是内部或外部命令」「系统无法接受输入的时间」。
                             #   统一为 GBK+CRLF+不切代码页, 并写进打包自检防复发;
                             # ③ 多开管理: 撤掉页签, 改左侧「多开管理…」按钮弹独立窗口
                             #   (实例保留 —— 停止按钮状态/定时任务按账号启动/非本机端口启停/退出回收
                             #   子进程共 6 处仍依赖它)
                             # v2.0.2 —— 日常玩法拆出 4 个可单独运行的顶层脚本(组队一条 / 五环+师门 /
                             #   地煞星 / 域外风情), 都自带纯享模式前置。用 include 复用 parts/ 子脚本,
                             #   与 daily_all.yaml 共用同一份, 不复制内容
                             # v2.0.3 —— 每日必领新增 段⑥ 帮派捐献(主界面右下「帮派」→ 帮派页(议政厅)
                             #   → 底部「聚义厅」页签 → 捐献)。聚义厅捐献区"免费态(单个捐献)/
                             #   常规态(材料捐献+银两捐献)"两态自动识别(上一步找不到就 goto 下一步);
                             #   捐完弹「恭喜获得」点顶部空白关, 再点左下返回(=菜单返回同模板)回主界面。
                             #   新增 2 界面(帮派页/帮派聚义厅页) + 3 按钮 + 5 模板; 真机实跑验证通过
                             # v2.0.4 —— 使用说明.txt 新增【免责声明】章节(文末): 明确软件为个人学习研究
                             #   用途的 ADB 截图+模拟点击外部辅助(不修改游戏/不逆向协议/不读内存/不绕过付费
                             #   与安全机制); 使用者自负账号限制等后果, 软件按"现状"提供, 作者免责; 禁商业牟利
                             # v2.0.5 —— ① 修 每日必领段⑥「点帮派」定位失败: 原 right_btn_gangpai 模板
                             #   把按钮周边背景也截了进去, 角色站在不同场景(背景变化)时匹配分从 0.949 掉到
                             #   0.779(<0.80 阈值), 判定失败导致整个段⑥被 on_fail 跳过(捐献没做成但报"成功")。
                             #   重采模板只保留按钮本体(深盘+徽记+文字, 32×34), 实测各主界面截图 0.99+、
                             #   议政厅/聚义厅 <0.5;
                             # ② 修 绿色版 GBK 控制台下日志刷屏 UnicodeEncodeError(✔/▶/⚠ 无法编码,
                             #   把 run_<port>.log 灌满 traceback): common._harden_console() 把
                             #   stdout/stderr 的 error 策略设为 replace, 符号降级为 ? 不再中断
                             # v2.0.6 —— 新增 gui/disclaimer.py: 启动后弹一次【免责声明】(内容与使用说明
                             #   文末一致, 结尾推荐主播「大话西游归来·缘起服 —— 达拉崩吧」), 可勾选
                             #   「不再显示此提示」永久跳过 → 写入 gui_settings.json(不入库)。
                             #   声明正文版本 DISCLAIMER_VERSION 变更时会重新弹一次;
                             #   菜单栏「帮助」与托盘菜单随时可重开, 也能取消勾选恢复启动弹窗
APP_TITLE = f"{APP_NAME} v{APP_VERSION}"


def load_config(path: str | Path | None = None) -> dict:
    """加载项目 config.yaml"""
    p = Path(path) if path else PROJECT_ROOT / "config.yaml"
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


# ---------------- adb 路径解析 (2026-09-24 新增) ----------------
# 事故背景: config.yaml 的 device.adb_path 曾写死 MuMu 的 C 盘安装路径,
# 换机器后 MuMu 装在 D 盘(或根本没装) → adb 找不到 → 界面只报"设备未连接",
# 用户完全看不出是路径问题。
# 现在改为多级回退, 任何一级命中即可用:
#   1) config.yaml 显式指定且文件真实存在
#   2) 包内自带 tools/adb/adb.exe (便携版首选, 换机器零配置)
#   3) 系统 PATH 里的 adb
#   4) MuMu 模拟器常见安装位置(跨盘符扫描)

BUNDLED_ADB = PROJECT_ROOT / "tools" / "adb" / "adb.exe"

# 这些值视为"未配置", 不当作路径
_ADB_PLACEHOLDERS = {"", "auto", "none", "null", "-", "adb", "adb.exe"}


def _drives() -> list[Path]:
    """列出本机盘符。优先用 os.listdrives(3.12+), 避免逐个探测不存在的盘。"""
    fn = getattr(os, "listdrives", None)
    if callable(fn):
        try:
            return [Path(d) for d in fn()]
        except OSError:
            pass
    return [Path(f"{c}:/") for c in "CDEFGH"]


def _mumu_adb_candidates() -> list[Path]:
    """枚举 MuMu 模拟器可能的 adb.exe 位置(含 D 盘等非系统盘的自定义安装)"""
    relative = [
        r"Netease\MuMu\nx_main\adb.exe",
        r"Program Files\Netease\MuMu\nx_main\adb.exe",
        r"Program Files (x86)\Netease\MuMu\nx_main\adb.exe",
        r"MuMu\nx_main\adb.exe",
        r"MuMuPlayer-12.0\nx_main\adb.exe",
        r"Program Files\Netease\MuMuPlayer-12.0\nx_main\adb.exe",
        r"Program Files\MuMu\nx_main\adb.exe",
    ]
    out = []
    for root in _drives():
        for rel in relative:
            out.append(root / rel)
    return out


def _same_file(a, b) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


def find_adb_ex(configured: str | Path | None = None) -> tuple[str | None, str]:
    """解析 adb 并同时返回来源标签, 返回 (路径或None, 来源说明)。

    只有真实存在的文件才算命中——配置里那条失效的老路径会被自动跳过,
    这正是"换了电脑就连接失败"的修复点。
    """
    # 1) 显式配置(真实的文件才认)
    if configured is not None:
        s = str(configured).strip().strip('"').strip("'")
        if s.lower() not in _ADB_PLACEHOLDERS:
            try:
                if Path(s).is_file():
                    return str(Path(s)), "config.yaml 指定"
            except OSError:
                pass

    # 2) 包内自带(便携版首选, 换机器零配置)
    try:
        if BUNDLED_ADB.is_file():
            return str(BUNDLED_ADB), "包内自带"
    except OSError:
        pass

    # 3) 系统 PATH
    which = shutil.which("adb")
    if which:
        return which, "系统 PATH"

    # 4) MuMu 常见安装位置(跨盘符)
    for p in _mumu_adb_candidates():
        try:
            if p.is_file():
                return str(p), "MuMu 安装目录"
        except OSError:
            continue

    return None, "未找到"


def find_adb(configured: str | Path | None = None) -> str | None:
    """按优先级解析出可用的 adb.exe 绝对路径; 全部失败返回 None。"""
    path, _ = find_adb_ex(configured)
    return path


ADB_NOT_FOUND_MSG = (
    "未找到 adb.exe，无法连接模拟器。请任选一种方式修复：\n"
    "  1) 用便携版自带的 adb：确认 app\\xyfz_auto\\tools\\adb\\adb.exe 存在；\n"
    "  2) 打开 config.yaml，把 device.adb_path 改成你机器上的真实路径"
    "（注意盘符，MuMu 可能装在 D 盘）；\n"
    "  3) 把 adb.exe 所在目录加入系统 PATH 环境变量。"
)


def png_bytes_to_bgr(data: bytes) -> np.ndarray:
    """把 adb screencap 的 PNG 字节解码为 OpenCV BGR 图像"""
    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        raise ValueError("PNG 解码失败")
    return img


def _harden_console() -> None:
    """GBK 控制台下, 日志里的 ✔/▶/⚠/✗ 等符号无法编码, StreamHandler 会抛
    UnicodeEncodeError 并刷屏(2026-10-11 绿色版实测: run_<port>.log 被 traceback 灌满)。
    把 stdout/stderr 的错误策略改为 replace(符号降级为 ?, 中文不受影响), 不再中断。
    """
    for s in (sys.stdout, sys.stderr):
        if s is None:
            continue
        try:
            s.reconfigure(errors="replace")      # Python 3.7+
        except Exception:
            pass


def setup_logger(name: str = "xyfz", level=logging.INFO) -> logging.Logger:
    """控制台 + 文件双写日志"""
    _harden_console()
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    if sys.stdout is not None:          # pythonw.exe 下 stdout 为 None(绿色版双击启动)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    target = log_dir / "run.log"
    try:
        fh = logging.FileHandler(target, encoding="utf-8")
    except PermissionError:
        # 2026-09-28: run.log 被另一个实例或残留进程占着时, 旧版直接抛异常导致
        # 程序根本起不来(冒烟测试/双开都会撞上)。改为退到带 PID 的日志文件。
        fh = logging.FileHandler(log_dir / f"run_{os.getpid()}.log", encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


def close_loggers() -> None:
    """关闭日志文件句柄。

    不关的话 logs\\run.log 会一直被本进程占着 —— 这正是"退出后删不掉文件夹"
    的常见原因之一(见 proc_guard.py 的说明)。
    """
    for name in list(logging.Logger.manager.loggerDict):
        lg = logging.getLogger(name)
        for h in list(getattr(lg, "handlers", [])):
            try:
                h.flush()
            except Exception:
                pass
            try:
                h.close()
            except Exception:
                pass
            try:
                lg.removeHandler(h)
            except Exception:
                pass


# ---------------- 跨进程设备互斥锁 (2026-09-11 新增) ----------------
# 事故背景: 凌晨定时任务运行时, 另一个 GUI 实例(绿色包旧版)同时触发了定时,
# 两个进程并发点击同一个模拟器 → 界面被搅乱, 双双失败。
# 锁文件放系统临时目录(跨项目目录有效, 主项目/绿色包共用同一把锁)。

def _lock_path(port: int) -> str:
    import os, tempfile
    d = os.path.join(tempfile.gettempdir(), "xyfz_auto_locks")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"lock_{port}.lock")


def acquire_device_lock(port: int) -> bool:
    """尝试获取指定模拟器端口的任务锁; 已有活进程持锁时返回 False"""
    import os
    p = _lock_path(port)
    if os.path.exists(p):
        try:
            pid = int(open(p, encoding="utf-8").read().strip() or 0)
        except Exception:
            pid = 0
        if pid and pid != os.getpid():
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                # 2026-09-23 加固: 旧版只看"PID 是否存活"——PID 会被系统复用,
                # 死进程的 PID 落到无关进程(如 explorer)头上时 OpenProcess 照样
                # 成功 → 残留锁被误判为"有实例在跑", 用户点开始被拒。
                # 现在再查进程映像名, 不是 python 解释器视为持锁进程已死 → 抢占。
                buf = ctypes.create_unicode_buffer(512)
                size = ctypes.c_ulong(512)
                name_ok = False
                try:
                    name_ok = bool(
                        k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)))
                except Exception:
                    name_ok = False
                k.CloseHandle(h)
                image = (buf.value or "").lower()
                if name_ok and ("python" in image or "xyfz" in image):
                    return False    # 确实是本工具的进程持锁 → 拒绝
                # PID 复用给了无关进程 / 查询失败 → 按死锁处理, 抢占覆盖
        # 持锁进程已死 → 抢占并覆盖
    with open(p, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    return True


def release_device_lock(port: int):
    import os
    try:
        os.remove(_lock_path(port))
    except OSError:
        pass
