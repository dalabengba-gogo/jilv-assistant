# -*- coding: utf-8 -*-
"""桩测试: 失败现场截图的滚动清理 (P1-5, 2026-10-17 v3.5.0)

不连模拟器。验证:
  1. 超过 keep_max 的旧图被删, 保留的是**最新的** N 张
  2. 超过 keep_days 的旧图被删(即使总数没超)
  3. 两条规则取并集(只留"又新又在天数内"的)
  4. 0 = 该规则不生效; 两条都 0 = 一张不删(退回旧行为)
  5. 目录不存在 / 非空目录里有子目录 → 不报错
  6. 引擎侧: 每落 ROTATE_EVERY 张才扫一次目录(不是每步都 listdir)
"""
import os
import sys
import time
import shutil
import tempfile

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, os.getcwd())

from common import rotate_files

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


def make_dir(n, age_days=0):
    """造 n 张 png; age_days>0 的把 mtime 往前推"""
    d = tempfile.mkdtemp(prefix="dbg_")
    old = time.time() - age_days * 86400
    for i in range(n):
        p = os.path.join(d, f"step{i}.png")
        with open(p, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + bytes([i % 256]) * 32)
        if age_days:
            os.utime(p, (old, old))
    return d


def names(d):
    return sorted(os.listdir(d))


print("[1] 超数删除 —— 保留最新的 N 张")
d = make_dir(10)
for i in range(10):     # 让 mtime 有先后: step9 最新
    os.utime(os.path.join(d, f"step{i}.png"), (time.time() + i, time.time() + i))
n = rotate_files(d, keep_max=4, keep_days=0)
check(n == 6, f"删掉 6 张(10-4), 实测 {n}")
check(names(d) == ["step6.png", "step7.png", "step8.png", "step9.png"],
      f"留下的应是最新的 4 张, 实测 {names(d)}")
shutil.rmtree(d, ignore_errors=True)

print("\n[2] 超期删除 —— 总数没超也要删")
d = make_dir(3, age_days=10)
n = rotate_files(d, keep_max=0, keep_days=7)
check(n == 3, f"10 天前的 3 张全删, 实测 {n}")
check(names(d) == [], f"目录应空, 实测 {names(d)}")
shutil.rmtree(d, ignore_errors=True)

print("\n[3] 两条规则取并集(又新又在天数内的才留)")
d = make_dir(5)
for i in range(5):
    os.utime(os.path.join(d, f"step{i}.png"), (time.time() + i, time.time() + i))
old = time.time() - 10 * 86400
os.utime(os.path.join(d, "step4.png"), (old, old))   # 最新那张改成 10 天前
n = rotate_files(d, keep_max=3, keep_days=7)
check("step4.png" not in names(d), f"最新但过期的那张也要删, 实测 {names(d)}")
check(len(names(d)) <= 3, f"剩余不超过 3 张, 实测 {names(d)}")
shutil.rmtree(d, ignore_errors=True)

print("\n[4] 0 = 规则不生效; 两条都 0 = 一张不删(退回旧行为)")
d = make_dir(5)
n = rotate_files(d, keep_max=0, keep_days=0)
check(n == 0 and len(names(d)) == 5, f"两条都 0 应一张不删, 实测删了 {n}")
n = rotate_files(d, keep_max=2, keep_days=0)
check(n == 3, f"只开条数规则仍生效, 实测 {n}")
shutil.rmtree(d, ignore_errors=True)

print("\n[5] 异常输入不炸")
check(rotate_files(None) == 0, "目录为 None → 0")
check(rotate_files("不存在的目录/xyz") == 0, "目录不存在 → 0")
d = tempfile.mkdtemp(prefix="dbg_")
os.mkdir(os.path.join(d, "子目录"))
with open(os.path.join(d, "a.png"), "wb") as f:
    f.write(b"x")
check(rotate_files(d, keep_max=1, keep_days=1) == 0, "子目录不算文件, 不删不炸")
shutil.rmtree(d, ignore_errors=True)

print("\n[6] 引擎侧: 每 ROTATE_EVERY 张才扫一次目录(不是每步 listdir)")
from engine.runner import Engine

eng = Engine(device=object(), matcher=object(), debug_dump_dir="logs/debug_test")
eng._cfg_cache = {"engine": {"debug_keep_max": 3, "debug_keep_days": 0}}
eng._ROTATE_EVERY = 5
calls = {"n": 0}
real_rotate = rotate_files


def spy(directory, keep_max=200, keep_days=7):
    calls["n"] += 1
    return real_rotate(directory, keep_max=keep_max, keep_days=keep_days)


import engine.runner as R
R.rotate_files = spy
try:
    for i in range(4):
        eng._rotate_debug()
    check(calls["n"] == 0, f"落 4 张还没到 5 張 → 不扫目录, 实测扫了 {calls['n']} 次")
    eng._rotate_debug()
    check(calls["n"] == 1, f"第 5 张触发一次扫描, 实测 {calls['n']}")
    eng._rotate_debug(force=True)
    check(calls["n"] == 2, f"force(任务开始时)必定扫, 实测 {calls['n']}")
    # 没配 debug_dump_dir → 完全不干活
    eng2 = Engine(device=object(), matcher=object())
    eng2._cfg_cache = {"engine": {"debug_keep_max": 3}}
    before = calls["n"]
    eng2._rotate_debug(force=True)
    check(calls["n"] == before, "没配 debug_dump_dir → 不做任何事")
finally:
    R.rotate_files = real_rotate

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
