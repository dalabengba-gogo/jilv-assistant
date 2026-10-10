# -*- coding: utf-8 -*-
"""多开调度器: 按 accounts.json 一次把多个账号(各自模拟器实例)的任务都跑起来

用法:
  python scripts/multirun.py             # 启动全部「启用」的账号(独立子进程, 各自日志)
  python scripts/multirun.py --dry-run   # 只打印将要启动的命令, 不真启动
  python scripts/multirun.py --wait      # 前台等待全部结束(适合命令行观察汇总)

账号配置在 accounts.json(可在 GUI「多开管理」面板修改):
  每账号 = 启用 / 名称 / adb端口 / 任务脚本 / 错峰延迟分钟(delay_min,
  相对启动时刻错开, 避免多个模拟器同时寻路/战斗抢资源)。

每个账号一个 run_one.py 子进程: 端口隔离、日志隔离(logs/run_<port>.log),
一个号失败不影响其它号; 子进程退出码 0=成功 1=失败 2=设备/配置错误。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from accounts import PROJECT_ROOT, enabled_accounts  # noqa: E402


def _spawn(acc: dict) -> subprocess.Popen | None:
    """启动一个账号的子进程; stdout 追加写 logs/run_<port>.log"""
    port = int(acc["port"])
    cmd = [sys.executable, "scripts/run_one.py",
           "--port", str(port), "--task", str(acc["task"]), "--tag", str(acc["name"])]
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(exist_ok=True)
    f = open(log_dir / f"run_{port}.log", "a", encoding="utf-8")
    try:
        p = subprocess.Popen(cmd, cwd=str(PROJECT_ROOT),
                             stdout=f, stderr=subprocess.STDOUT)
    except Exception as e:
        print(f"  ✗ [{acc['name']}] 端口{port} 启动失败: {e}")
        return None
    print(f"  ✓ [{acc['name']}] 端口{port} 已启动 (PID {p.pid}), 任务 {acc['task']}")
    return p


def main():
    ap = argparse.ArgumentParser(description="多开调度器: 并行跑多个账号的日常任务")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划, 不真启动")
    ap.add_argument("--wait", action="store_true", help="等待全部账号任务结束后退出")
    args = ap.parse_args()

    rows = enabled_accounts()
    if not rows:
        print("没有启用的账号。请先在 GUI「多开管理」勾选账号, 或编辑 accounts.json。")
        return
    print(f"计划启动 {len(rows)} 个账号:")
    for r in rows:
        print(f"  - {r['name']}: adb 端口 {r['port']}, 任务 {r['task']}, "
              f"延迟 {r['delay_min']} 分钟")

    if args.dry_run:
        print("\n[--dry-run] 仅打印, 未真正启动。")
        return

    procs: list[tuple[dict, subprocess.Popen]] = []

    def _launch(acc: dict):
        p = _spawn(acc)
        if p:
            procs.append((acc, p))

    for acc in rows:
        delay = int(acc.get("delay_min") or 0)
        if delay > 0:
            print(f"  · [{acc['name']}] 端口{acc['port']} 延迟 {delay} 分钟后启动")
            threading.Timer(delay * 60.0, lambda a=acc: _launch(a)).start()
        else:
            _launch(acc)

    if not args.wait:
        print("\n已提交, 主进程退出。各账号日志: logs/run_<port>.log")
        return

    # --wait: 等所有已启动进程结束(延迟的由 Timer 推进, 这里轮询)
    print("\n等待全部账号结束…")
    deadline = time.time() + 6 * 3600   # 最长等 6 小时
    try:
        while time.time() < deadline:
            running = [(a, p) for a, p in procs if p.poll() is None]
            if not running and all(not t.is_alive()
                                   for t in threading.enumerate() if t is not threading.main_thread()):
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n收到中断, 终止所有子进程…")
        for _, p in procs:
            if p.poll() is None:
                p.terminate()
        sys.exit(130)

    print("\n===== 汇总 =====")
    failed = 0
    for acc, p in procs:
        code = p.poll()
        st = "成功" if code == 0 else ("失败" if code == 1 else f"设备/配置错误({code})")
        if code != 0:
            failed += 1
        print(f"  [{acc['name']}] 端口{acc['port']} → {st}")
    print(f"共 {len(procs)} 个账号, 失败 {failed} 个。日志见 logs/run_<port>.log")


if __name__ == "__main__":
    main()
