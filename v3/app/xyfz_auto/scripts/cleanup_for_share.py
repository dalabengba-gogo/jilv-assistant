# -*- coding: utf-8 -*-
"""外发前清理 —— 把便携版里的个人痕迹清干净再拷给别人

2026-10-17 新增:
    绿色版跑过一次就会长出这些东西, 拷给别人等于把自己的设置和游戏画面一起送走:
      · app/xyfz_auto/gui_settings.json  免责声明"不再显示"标记(别人看不到声明,
        与声明第 7 条"对外分发须完整保留本声明"冲突)
      · app/xyfz_auto/gui_timer.json     本机定时任务
      · app/xyfz_auto/accounts.json      我的模拟器端口表
      · app/xyfz_auto/logs/              运行日志 + 游戏截图(可能上百 MB)
      · app/xyfz_auto/shots/             界面临时截图(v4.8.0 补进清单)
      · 各处 __pycache__
    accounts.json 删掉后首次运行会自动重建空表, 不影响使用。

用法: 双击根目录的「外发前清理.bat」(或 python scripts/cleanup_for_share.py)
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

# scripts/ 的上级 = xyfz_auto/
APP_DIR = Path(__file__).resolve().parent.parent


def _rm_path(p: Path) -> str:
    try:
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists():
            p.unlink()
        return "已清理" if not p.exists() else "仍存在(被占用?)"
    except Exception as e:
        return f"失败: {e}"


def main() -> int:
    print("鸡驴助手 —— 外发前清理")
    print("=" * 56)
    print("清理个人配置与运行产物, 让这份便携版可以干净地拷给别人。\n")

    targets: list[tuple[str, Path]] = [
        ("免责声明设置", APP_DIR / "gui_settings.json"),
        ("本机定时任务", APP_DIR / "gui_timer.json"),
        ("端口账号表", APP_DIR / "accounts.json"),
        ("日志与截图", APP_DIR / "logs"),
        ("模板采集临时目录", APP_DIR / "_collect"),
        # 2026-10-19 v4.8.0: 补 shots/(界面里的临时截图落地目录)。打包侧
        # EXCLUDE_DIRS 早就排了它, 但外发清理这份清单一直漏着 —— 两个口径不一致,
        # 就会"打包出来的包干净、手工整理的包带着游戏画面发出去"。
        ("临时截图目录", APP_DIR / "shots"),
    ]
    n_ok = 0
    for label, p in targets:
        if not p.exists():
            print(f"  [跳过] {label}: 不存在")
            continue
        res = _rm_path(p)
        print(f"  [{'完成' if res == '已清理' else '!!'}] {label}: {res}")
        n_ok += 1

    n_cache = 0
    for d in APP_DIR.rglob("__pycache__"):
        if _rm_path(d) == "已清理":
            n_cache += 1
    print(f"  [完成] 字节码缓存 __pycache__: {n_cache} 处")

    print("\n" + "=" * 56)
    print("清理完成。现在可以把整个文件夹(或压缩成 zip)拷给别人了。")
    print("注意: 若助手还在运行, 日志可能被占用删不掉 —— 先退出程序再来一次。")
    print("      端口账号表已删除, 对方首次运行会自动重建空表。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
