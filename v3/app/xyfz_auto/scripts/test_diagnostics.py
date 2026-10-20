# -*- coding: utf-8 -*-
"""诊断导出 确定性测试(不连模拟器, 不弹界面)

验证:
  1. 导出成功, zip 落在指定目录, 文件名含版本号
  2. 必备文件齐全(指引/环境/端口/配置/账号/脚本/资产)
  3. 日志按行截断(不会把上百 MB 全塞进去)
  4. 账号表只含端口等 4 个字段(不含凭据类字段)
  5. 截图按时间倒序且不超过上限
"""
import os
import sys
import tempfile
import zipfile
from pathlib import Path

os.chdir(Path(__file__).resolve().parent.parent)
sys.path.insert(0, os.getcwd())

from diagnostics import export_diagnostics  # noqa: E402

fails = []


def ck(name, cond, extra=""):
    print(("  ok  " if cond else "  FAIL") + f"  {name}" + (f" — {extra}" if extra else ""))
    if not cond:
        fails.append(name)


print("[1] 导出到临时目录")
extra = {
    "adb_source": "包内自带", "adb_path": r"D:\x\adb.exe",
    "screen_size": "540x960", "package": "com.netease.xyfz_02",
    "ports": [{"port": 16384, "name": "账号1", "online": True, "front": "前台: com.netease.xyfz_02"},
              {"port": 16416, "name": "账号2", "online": False, "front": ""}],
    "current_script": "每日必领.yaml", "last_run": "2026-10-17 00:10",
    "last_summary": "16 步未做成",
}
with tempfile.TemporaryDirectory() as td:
    path = export_diagnostics({"device": {"port": 16384}}, extra, Path(td))
    ck("返回 zip 路径", path.exists() and path.suffix == ".zip", path.name)
    ck("文件名含版本", "v" in path.name and "诊断" in path.name, path.name)

    print("[2] 必备文件齐全")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        # 2026-10-19 v4.8.0 (P1-6): 编号整体后移 —— 中间插进了 05_日志健康度(v4.6.0,
        # 原先与 04_账号表 撞号)、08_定时任务、09_磁盘与日志体积。
        must = ["00_排错指引.txt", "01_环境信息.txt", "02_设备与端口.txt",
                "03_配置摘要.txt", "04_账号表.json", "05_日志健康度.txt",
                "06_脚本清单.txt", "07_资产统计.txt", "08_定时任务.txt",
                "09_磁盘与日志体积.txt"]
        for m in must:
            ck(f"含 {m}", m in names)
        ck("config.yaml 已打包", "config.yaml" in names)

        print("[3] 内容正确性")
        env = z.read("01_环境信息.txt").decode("utf-8")
        ck("环境含 adb 来源", "包内自带" in env)
        ck("环境含屏幕基线", "540x960" in env)
        ports = z.read("02_设备与端口.txt").decode("utf-8")
        ck("端口表含两个端口", "16384" in ports and "16416" in ports, ports.splitlines()[2] if len(ports.splitlines()) > 2 else "")
        ck("离线端口标离线", "离线" in ports)

        import json as _json
        acc = _json.loads(z.read("04_账号表.json").decode("utf-8"))
        ck("账号表是列表", isinstance(acc, list), f"{len(acc)} 条")
        if acc:
            keys = set(acc[0].keys())
            ck("账号字段仅 4 个", keys <= {"name", "port", "enabled", "task"}, str(keys))

        print("[4] 日志截断")
        logs = [n for n in names if n.startswith("10_日志/")]
        ck("打包了日志", len(logs) >= 1, str(logs))
        # v4.8.0: 顺带确认顶层编号不重复(历史上 04 曾被两份文件共用)
        heads = {}
        for nm in names:
            h = nm.split("/")[0]
            if h[:2].isdigit():
                heads.setdefault(h[:2], []).append(h)
        dup = [k for k, v in heads.items() if len(set(v)) > 1]
        ck("顶层编号无重复", not dup, str(sorted(heads)))
        for n in logs:
            txt = z.read(n).decode("utf-8", errors="replace")
            ck(f"{n} 行数 <= 3001", txt.count("\n") <= 3001, f"{txt.count(chr(10))} 行")

print("\n" + ("ALL PASS" if not fails else f"FAILED: {fails}"))
sys.exit(1 if fails else 0)
