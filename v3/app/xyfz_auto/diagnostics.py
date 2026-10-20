# -*- coding: utf-8 -*-
"""诊断信息导出 —— 出问题时要"发给别人看"的那一份

2026-10-17 新增:
    以前排障全靠用户手动翻 logs、截屏、口述环境, 信息不齐来回问好几轮。
    现在一键导出 zip: 版本/环境/端口状态/配置/账号表/脚本清单/日志/
    最近失败现场截图, 外加一份**人读的排错指引**。

设计约定:
    · 本模块**不依赖 Qt**(纯 stdlib), 可单独跑测试; GUI 只负责把 extra 传进来。
    · 日志按行截断、截图按时间取最近 N 张 —— 一晚能产生上百 MB debug 截图,
      全量打包既慢又难传, 也容易把无关隐私带出去。
    · 每份文件都在指引里说明"它回答什么问题", 让看包的人不用猜。
"""
from __future__ import annotations

import json
import os
import platform
import sys
import time
import zipfile
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
LOGS_DIR = PROJECT_ROOT / "logs"

# 截断上限: 日志按"最后 N 行"取(尾部最有价值), 截图按最近 N 张
LOG_TAIL_LINES = 3000
CHILD_LOG_TAIL_LINES = 1500
SHOT_MAX = 15
SHOT_MAX_BYTES = 40 * 1024 * 1024      # 截图总量硬上限 40MB

GUIDE_TEXT = """【鸡驴助手 —— 诊断包排错指引】

一、这个包是什么
    由「帮助 → 导出诊断信息…」一键生成, 含版本/环境/端口状态/配置/账号表/
    脚本清单/运行日志/最近失败现场截图。把它整个发给排查的人即可。

二、先看这三份(90% 的问题在这里就能定位)
    00_排错指引.txt      就是本文件
    01_环境信息.txt      版本、系统、Python、adb 来源、屏幕基线
    02_设备与端口.txt    每个模拟器端口在不在、前台是不是游戏
    03_日志运行日志.txt  最近一次跑批的每一步(失败/跳过都记在这里)

三、常见故障自检顺序
    1) 端口不对 —— 02 里看对应端口是「离线」。模拟器重启后 MuMu 端口会从
       16384 变成 emulator-5556 之类, 去「多开管理」把端口改成实际的那个。
    2) adb 找不到 —— 01 里 adb 来源标着"未找到"。优先用包内自带
       app\\xyfz_auto\\tools\\adb\\adb.exe, 或把 config.yaml 的 adb_path 改成 "auto"。
    3) 前台不是游戏 —— 02 里前台一栏是桌面/别的包名。脚本等待界面会一直超时。
    4) 模板匹配不上 —— 03 里连续出现「超时或识别失败」, 配 07 里的现场截图看:
       画面换了背景/多了角标都会让分数掉到 0.80 以下。
    5) 整条任务直接没了 —— 看 03 日志末尾有没有 traceback, 有就说明是异常
       崩了(而不是步骤失败), 这类要报给开发者。若是**原生崩溃**(界面突然消失,
       03 里没有 traceback), 直接看 10_日志 下的四份:
         · 崩溃日志.txt         —— faulthandler 抓到的异常类型与线程
         · 崩溃前最后动作.txt   —— 死前在截图 / OCR / 点击哪一步
         · 崩溃前线程快照.txt   —— 每 30 秒一份全线程栈(含线程名)
         · 已加载模块.txt       —— DLL 基址表, 用来把崩溃地址归因到具体库
       崩溃日志里若出现「FATAL NATIVE EXCEPTION ... 归因: xxx.dll +0x...」,
       那就是故障模块, 把这一行发过来即可。

四、隐私提示
    账号表只有端口与脚本名(不含账号密码); 截图是你的游戏画面, 外发前请确认
    没有不想给人看的内容 —— 不需要的话删掉 07_失败现场截图 目录再发。

五、文件清单
    01_环境信息.txt        版本 / 系统 / Python / adb 来源 / 屏幕基线
    02_设备与端口.txt      每个端口在线与否、adb devices 原文、前台 Activity
    03_配置摘要.txt        config.yaml 全文 + 关键项说明
    04_账号表.json         多开管理里的端口与脚本
    05_日志健康度.txt      日志占了多少、滚动有没有开
    06_脚本清单.txt        全部脚本、步数、最后修改时间
    07_资产统计.txt        模板/按钮/界面数量, 脚本最后跑的是哪个
    08_定时任务.txt        配了哪些定时、今天跑过没有(v4.8.0 新增)
    09_磁盘与日志体积.txt  所在盘剩余空间 + logs 各目录明细(v4.8.0 新增)
    10_日志/               运行日志与崩溃日志(按行截断)
    11_失败现场截图/       最近若干张失败现场(按时间倒序)
    config.yaml            原始配置文件
"""


def _tail_lines(path: Path, n: int) -> str:
    """取文件最后 n 行(流式尾读, 2026-10-19 v4.8.0 P1-6)

    以前是 ``f.readlines()[-n:]`` —— 为了留 3000 行, 得先把**整个文件读进内存**。
    run.log 涨到几百 MB 时这一步就能吃掉 GB 级内存(用户机器同时还在跑 MuMu),
    表现为"点一下导出诊断, 机器卡半分钟"。现在边遍历边丢: 全程内存只留最后 n 行。
    """
    try:
        from collections import deque
        buf: "deque[str]" = deque(maxlen=n)
        total = 0
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                total += 1
                buf.append(line)
        head = ""
        if total > n:
            head = f"…(前面 {total - n} 行已省略, 只保留最后 {n} 行)…\n"
        return head + "".join(buf)
    except Exception as e:
        return f"(读取失败: {e})"


def _safe_stat(p: Path) -> str:
    try:
        st = p.stat()
        return datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


def build_env_text(extra: dict) -> str:
    """01 环境信息"""
    try:
        from common import APP_NAME, APP_VERSION
    except Exception:
        APP_NAME, APP_VERSION = "鸡驴助手", "?"
    lines = [
        f"{APP_NAME} 版本: {APP_VERSION}",
        f"导出时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"操作系统: {platform.platform()}",
        f"Python: {sys.version.split()[0]}  ({sys.executable})",
        f"运行方式: {'便携版 runtime' if 'runtime' in sys.executable.lower() else '系统 Python'}",
        "",
        f"adb 来源: {extra.get('adb_source') or '(未知)'}",
        f"adb 路径: {extra.get('adb_path') or '(未解析)'}",
        f"屏幕基线: {extra.get('screen_size') or '(未知)'}",
        f"游戏包名: {extra.get('package') or '(未知)'}",
        "",
        f"项目目录: {PROJECT_ROOT}",
        f"日志目录: {LOGS_DIR}  {'[存在]' if LOGS_DIR.exists() else '[不存在]'}",
    ]
    return "\n".join(lines) + "\n"


def _adb_devices_raw(extra: dict) -> str:
    """``adb devices -l`` 原文(2026-10-19 v4.8.0 P1-6)

    为什么要把原文放进包里: 「02_设备与端口」是我们**加工过**的结论(在线/离线),
    加工就可能误判 —— 模拟器多开器各版本的端口写法不一样(127.0.0.1:16384 /
    emulator-5556 / offline / unauthorized), 我们只要有一环没认出来, 结论就是空的。
    原文让人能自己核对: 到底 adb 看见了几台设备、状态串是什么。

    本机 adb 可能连不上(更常见的情形恰恰是"排障时连不上"), 故一律带超时。
    """
    adb = (extra or {}).get("adb_path") or ""
    if not adb:
        return "(未提供 adb 路径, 跳过。若 adb_path 配的是 auto, 说明当时没解析出来 —— 这本身就是线索)"
    import subprocess
    # 2026-10-19 v5.1.1: pythonw 没有控制台, 这里不写 creationflags 会在桌面上
    # 弹出黑色命令行窗口(用户点「诊断」时闪一下)。全项目只有这处漏了。
    _no_win = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    last = "(未执行)"
    for cmd in ([adb, "devices", "-l"], [adb, "devices"]):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=8, encoding="utf-8", errors="replace",
                               creationflags=_no_win)
            out = (p.stdout or "") + (("\n[stderr] " + p.stderr) if p.stderr else "")
            return f"$ {' '.join(cmd)}\n(退出码 {p.returncode})\n{out or '(无输出)'}"
        except Exception as e:
            last = f"执行失败: {type(e).__name__}: {e}"
    return last


def build_ports_text(extra: dict) -> str:
    """02 设备与端口 —— 排障第一现场"""
    ports = extra.get("ports") or []
    if not ports:
        out = ["(没有读到端口状态 —— 多开管理里没配账号, 或探测还没跑完)\n"]
    else:
        out = ["账号        端口     状态    前台", "-" * 60]
        for p in ports:
            state = "在线" if p.get("online") else ("离线" if p.get("online") is False else "待探测")
            out.append(f"{str(p.get('name') or '')[:9]:<10}{p.get('port'):<8}{state:<8}"
                       f"{p.get('front') or ''}")
        out.append("")
        out.append("说明: 离线 = 这一刻连不上(常见于先开助手后开模拟器, 每 10 秒自动重试);")
        out.append("      前台不是游戏包名时, 所有「等待某界面」的步骤都会超时。")
    # 2026-10-19 v4.8.0 (P1-6): 附 adb 原文, 让人能核对上面的结论。
    out += ["", "-" * 60, "【adb devices 原文】", _adb_devices_raw(extra)]
    return "\n".join(out) + "\n"


def build_config_text(cfg: dict) -> str:
    """03 配置摘要"""
    head = [
        "【关键项解读】",
        "  device.host/port    —— 本机模拟器端口(多开账号的端口在「多开管理」里单独配)",
        "  device.adb_path     —— 写 auto 会多级回退(配置→包内自带→PATH→MuMu 常见位置)",
        "  recognition.match_threshold  —— 模板匹配阈值, 默认 0.80; 总差一点(如 0.79)",
        "                         多半是模板被动态角标/背景污染, 应当重裁模板而不是降阈值",
        "  engine.default_timeout / poll_interval —— 每步超时与轮询间隔",
        "  engine.goto_loop_limit —— 同一 goto 目标重复跳转上限(防死循环)",
        "",
        "【config.yaml 全文】",
    ]
    try:
        raw = (PROJECT_ROOT / "config.yaml").read_text(encoding="utf-8")
    except Exception as e:
        raw = f"(读取失败: {e})"
    return "\n".join(head) + "\n\n" + raw


def build_log_health_text(cfg: dict) -> str:
    """05 日志健康度(2026-10-18 v4.6.0 P0-4)

    加这一节的直接原因: 以前日志只在"跑完任务"清一次, 不跑任务的天只进不出,
    logs 悄悄涨到 159MB 才被发现 —— 而排障包里根本没有"占了多少"这一说,
    用户拿着诊断包问"是不是磁盘被它吃了", 我们答不上来。

    现在把三件事摊开: 各日志文件的体量、滚动配置是否开着、以及配额下大概的稳态。
    """
    eng = (cfg or {}).get("engine") or {}
    try:
        mb = int(eng.get("log_max_bytes") or 0)
        bc = int(eng.get("log_backup_count") or 0)
        days = int(eng.get("log_keep_days") or 0)
        keep = int(eng.get("debug_keep_max") or 0)
        shot_days = int(eng.get("debug_keep_days") or 0)
    except Exception:
        mb = bc = days = keep = shot_days = 0

    out = ["【滚动配置】"]
    if mb and bc:
        out.append(f"  文本日志: 单文件封顶 {mb / 1048576:.0f} MB, 保留 {bc} 份备份"
                   f" → 单个端口最多约 {mb * (1 + bc) / 1048576:.0f} MB")
    else:
        out.append("  文本日志: ⚠ 未配置 log_max_bytes/log_backup_count —— 日志会一直追加")
    out.append(f"  文本日志归档: 保留 {days} 天(0=不删过期归档)")
    out.append(f"  失败截图: 保留 {keep} 张 / {shot_days} 天"
               + ("  (v4.6.0 起存 JPEG, 单张实测约 43~159 KB)" if keep else ""))

    out += ["", "【实际占用】"]
    try:
        files = sorted([f for f in LOGS_DIR.glob("*.log") if f.is_file()],
                       key=lambda p: p.stat().st_size, reverse=True) \
            if LOGS_DIR.exists() else []
        ddirs = sorted([d for d in LOGS_DIR.glob("debug*") if d.is_dir()]) \
            if LOGS_DIR.exists() else []
    except Exception as e:
        files, ddirs = [], []
        out.append(f"  (读取日志目录失败: {e})")

    total = 0
    if not files:
        out.append("  (没有日志文件)")
    for p in files[:15]:
        sz = p.stat().st_size
        total += sz
        flag = "  ← 偏大" if mb and sz > mb else ""
        out.append(f"  {p.name:<34} {sz / 1048576:>7.1f} MB{flag}")
    shots_n = 0
    shots_b = 0
    for d in ddirs:
        try:
            fs = [f for f in d.iterdir() if f.is_file()]
        except Exception:
            fs = []
        shots_n += len(fs)
        sb = 0
        for f in fs:
            try:
                sb += f.stat().st_size
            except OSError:
                pass
        shots_b += sb
        total += sb
        out.append(f"  {d.name + '/':<34} {sb / 1048576:>7.1f} MB  ({len(fs)} 张)")
    out += ["", f"合计约 {total / 1048576:.1f} MB"
                 + (f", 其中失败截图 {shots_n} 张" if shots_n else "")]

    if (not mb or not bc) and files:
        out += ["", "⚠ 建议: 把 log_max_bytes / log_backup_count 配上, "
                    "否则上面的文件永远不会变小。"]
    return "\n".join(out) + "\n"


def _dir_size_bytes(root: Path, cap_files: int = 20000) -> tuple[int, int]:
    """目录体积与文件数(遍历有上限, 别为一个诊断包扫半天磁盘)"""
    total = files = 0
    try:
        for p in root.rglob("*"):
            if files >= cap_files:
                break
            try:
                if p.is_file():
                    total += p.stat().st_size
                    files += 1
            except OSError:
                continue
    except Exception:
        pass
    return total, files


def build_timer_text(extra: dict) -> str:
    """08 定时任务清单(2026-10-19 v4.8.0 P1-6)

    加这一节的直接原因: 用户反馈过"定时任务没跑"而我们拿到的包里**根本看不见
    定时配了什么、今天跑过没有** —— 只能靠一串口头问答去猜(配了?到点了吗?
    GUI 当时开着吗?是不是失败了退了份额?)。这些界面早就知道(v4.6.0 P0-5 之后
    连"今天已跑/重试次数"都落盘了), 只是从来没进过诊断包。
    """
    timers = (extra or {}).get("timers")
    if not timers:
        return "(没有读到定时任务 —— 没配过定时, 或那条定时列表还没刷出来)\n"
    out = ["启用  时间    脚本                              账号              今天状态",
           "-" * 82]
    for t in timers:
        out.append(
            f"{'是' if t.get('enabled') else '否':<6}"
            f"{str(t.get('time') or ''):<8}{str(t.get('task') or '')[:30]:<32}"
            f"{str(t.get('account') or '')[:16]:<18}{str(t.get('today') or '')}")
    out += ["", "今天状态口径: 今天已跑=到点执行过; 今天未跑=还没到点, 或当天 GUI 没开",
            "              (超过宽限时间未执行的会在界面明确报案「已错过」);",
            "              失败重试 N 次=失败后退了份额、等冷却重投, 次数用尽才会占掉当天份额。"]
    grace = (extra or {}).get("timer_grace_minutes")
    if grace:
        out.append(f"错过判定宽限: {grace} 分钟(超过就算错过, 界面会明确提示)")
    return "\n".join(out) + "\n"


def build_disk_text(extra: dict) -> str:
    """09 磁盘与日志体积(2026-10-19 v4.8.0 P1-6)

    回答两个排障时反复问、却从来没人能一次答上来的问题:
      ① 盘是不是满了(截图写不进去 → 步骤全部"超时", 看起来像识别挂了);
      ② logs 到底占了多少、是被哪个目录吃掉的。
    """
    out = ["【磁盘剩余】"]
    import shutil
    try:
        total, used, free = shutil.disk_usage(str(PROJECT_ROOT))
        out.append(f"  项目所在盘: 总 {total / 1073741824:.1f} GB, "
                   f"已用 {used / 1073741824:.1f} GB, 剩余 {free / 1073741824:.1f} GB")
        if free < 2 * 1073741824:
            out.append("  ⚠ 剩余不足 2 GB: 失败截图与日志都可能写不进去, "
                       "表现为大量步骤突然一起超时。")
    except Exception as e:
        out.append(f"  (读取失败: {e})")

    out += ["", "【日志目录明细】"]
    if not LOGS_DIR.exists():
        out.append("  (日志目录不存在)")
        return "\n".join(out) + "\n"
    rows = []
    grand = 0
    for d in sorted([p for p in LOGS_DIR.iterdir() if p.is_dir()]):
        b, n = _dir_size_bytes(d)
        grand += b
        rows.append(f"  {d.name + '/':<28} {b / 1048576:>8.1f} MB  ({n} 个文件)")
    for f in sorted([p for p in LOGS_DIR.glob('*') if p.is_file()],
                    key=lambda p: p.stat().st_size, reverse=True)[:10]:
        b = f.stat().st_size
        grand += b
        rows.append(f"  {f.name:<28} {b / 1048576:>8.1f} MB")
    out += rows if rows else ["  (空)"]
    out.append(f"  {'合计':<28} {grand / 1048576:>8.1f} MB")
    out += ["", "说明: logs/debug* 是失败现场截图(v4.6.0 起存 JPEG, 单张约 43~159 KB), "
                 "保留份数见 03 配置摘要里的 debug_keep_max / debug_keep_days。"]
    return "\n".join(out) + "\n"


def build_scripts_text(extra: dict) -> str:
    """06 脚本清单"""
    tdir = PROJECT_ROOT / "assets" / "tasks"
    out = [f"脚本目录: {tdir}", ""]
    if not tdir.exists():
        return "(脚本目录不存在)\n"
    for p in sorted(tdir.rglob("*.yaml")):
        try:
            steps = sum(1 for ln in p.read_text(encoding="utf-8").splitlines()
                        if ln.strip().startswith("- name:"))
        except Exception:
            steps = -1
        out.append(f"  {p.relative_to(tdir).as_posix():<42} {steps:>4} 步   "
                   f"最后修改 {_safe_stat(p)}")
    if extra.get("current_script"):
        out += ["", f"界面上当前选中的脚本: {extra['current_script']}"]
    return "\n".join(out) + "\n"


def build_assets_text(extra: dict) -> str:
    """07 资产统计"""
    tpl_dir = PROJECT_ROOT / "assets" / "templates"
    n_tpl = len([f for f in tpl_dir.glob("*.png")]) if tpl_dir.exists() else -1
    n_btn = n_scr = -1
    try:
        import yaml
        reg = yaml.safe_load(
            (PROJECT_ROOT / "assets" / "ui_registry.yaml").read_text(encoding="utf-8")) or {}
        n_btn = len(reg.get("buttons") or {})
        n_scr = len(reg.get("screens") or {})
    except Exception:
        pass
    return "\n".join([
        f"识别模板: {n_tpl} 个",
        f"注册表按钮: {n_btn} 个",
        f"注册表界面: {n_scr} 个",
        "",
        f"最后一次跑批: {extra.get('last_run') or '(本次会话没跑过)'}",
        f"最后一次失败汇总: {extra.get('last_summary') or '(无)'}",
    ]) + "\n"


def default_dest_dir() -> Path:
    """导出到哪: 优先桌面(好找), 退回项目目录"""
    for cand in (Path.home() / "Desktop", Path.home() / "桌面"):
        if cand.is_dir():
            return cand
    return PROJECT_ROOT


def export_diagnostics(cfg: dict, extra: dict, dest_dir: Path | None = None) -> Path:
    """打包诊断信息, 返回 zip 路径

    extra 由 GUI 填: adb_source / adb_path / screen_size / package /
                     ports[list] / current_script / last_run / last_summary
    """
    try:
        from common import APP_VERSION
    except Exception:
        APP_VERSION = "?"
    dest_dir = Path(dest_dir) if dest_dir else default_dest_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = dest_dir / f"鸡驴助手_诊断_v{APP_VERSION}_{stamp}.zip"

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("00_排错指引.txt", GUIDE_TEXT)
        z.writestr("01_环境信息.txt", build_env_text(extra))
        z.writestr("02_设备与端口.txt", build_ports_text(extra))
        z.writestr("03_配置摘要.txt", build_config_text(cfg))
        # v4.6.0(P0-4): 让"日志到底占了多少、有没有在滚动"在排障包里看得见
        z.writestr("05_日志健康度.txt", build_log_health_text(cfg))
        z.writestr("06_脚本清单.txt", build_scripts_text(extra))
        z.writestr("07_资产统计.txt", build_assets_text(extra))
        # v4.8.0(P1-6): 定时清单、磁盘与日志体积 —— 以前这两样排障时要靠口头问答去猜
        z.writestr("08_定时任务.txt", build_timer_text(extra))
        z.writestr("09_磁盘与日志体积.txt", build_disk_text(extra))

        # 04 账号表: 只留端口与脚本, 不含任何凭据
        acc = extra.get("accounts")
        if acc is None:
            try:
                acc = json.loads((PROJECT_ROOT / "accounts.json").read_text(encoding="utf-8"))
            except Exception:
                acc = []
        try:
            slim = [{"name": a.get("name"), "port": a.get("port"),
                     "enabled": a.get("enabled"), "task": a.get("task")}
                    for a in (acc or [])]
        except Exception:
            slim = []
        z.writestr("04_账号表.json",
                   json.dumps(slim, ensure_ascii=False, indent=2))

        # 10 日志
        if LOGS_DIR.exists():
            main_log = LOGS_DIR / "run.log"
            if main_log.exists():
                z.writestr("10_日志/运行日志.txt", _tail_lines(main_log, LOG_TAIL_LINES))
            for p in sorted(LOGS_DIR.glob("run_*.log")):
                z.writestr(f"10_日志/{p.stem}.txt",
                           _tail_lines(p, CHILD_LOG_TAIL_LINES))
            crash = LOGS_DIR / "crash.log"
            if crash.exists() and crash.stat().st_size > 0:
                z.writestr("10_日志/崩溃日志.txt", _tail_lines(crash, 2000))
            # 2026-10-19 v5.2.0: 崩溃三件套 —— 之前只有 crash.log, 而原生崩溃
            # 在里面往往只有一行 "<no Python frame>", 等于没线索。
            #   last_action.log  死前正在做什么(截图/OCR/点击/第几步)
            #   threads.log      每 30 秒一份全线程快照(含线程名)
            #   modules.log      已加载 DLL 基址表 —— 崩在这种库里的地址靠它归因
            for name, arc in (("last_action.log", "10_日志/崩溃前最后动作.txt"),
                              ("threads.log", "10_日志/崩溃前线程快照.txt"),
                              ("modules.log", "10_日志/已加载模块.txt")):
                p = LOGS_DIR / name
                if p.exists() and p.stat().st_size > 0:
                    z.writestr(arc, _tail_lines(p, 1200))
            # 多开子进程各自的诊断文件(文件名带 _p<端口> 后缀)
            for p in sorted(LOGS_DIR.glob("last_action_p*.log")):
                z.writestr(f"10_日志/{p.stem}.txt", _tail_lines(p, 400))
            # minidump(若真的写成功过)只报清单, 不打包 —— 单份可能数 MB
            dumps = sorted(LOGS_DIR.glob("crash*_*.dmp")) if LOGS_DIR.exists() else []
            if dumps:
                z.writestr("10_日志/dump清单.txt",
                           "\n".join(f"{p.name}  {p.stat().st_size} 字节" for p in dumps)
                           + "\n\n解析: runtime\\python.exe scripts\\analyze_dump.py <dmp>\n")

        # 11 失败现场截图: 最近 N 张, 总量不超 SHOT_MAX_BYTES
        shots = []
        for d in sorted(LOGS_DIR.glob("debug*")) if LOGS_DIR.exists() else []:
            if d.is_dir():
                # 2026-10-18 v4.6.0(P0-4): 失败截图改存 JPEG 了(v4.5 及更早是 PNG)。
                # 两种都认 —— 否则升级后这里会统计成 0 张、导出诊断包也不带图,
                # 倒不是崩, 而是排障时"明明有截图却一张都没有"最难查。
                shots += [f for f in d.glob("*.jpg")]
                shots += [f for f in d.glob("*.jpeg")]
                shots += [f for f in d.glob("*.png")]
        shots.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        used = 0
        for i, p in enumerate(shots[:SHOT_MAX]):
            if used + p.stat().st_size > SHOT_MAX_BYTES:
                break
            z.write(p, f"11_失败现场截图/{i + 1:02d}_{p.name}")
            used += p.stat().st_size
        if len(shots) > SHOT_MAX:
            z.writestr("11_失败现场截图/说明.txt",
                       f"只打包最近 {SHOT_MAX} 张(共 {len(shots)} 张), 按时间倒序。\n")

        cfg_path = PROJECT_ROOT / "config.yaml"
        if cfg_path.exists():
            z.write(cfg_path, "config.yaml")
    return out
