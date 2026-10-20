# -*- coding: utf-8 -*-
"""P0-3 心跳超时孤儿判据 的确定性测试(不依赖模拟器/真机)

验证:
  1. 引擎主线程周期性发心跳: _beat() 受间隔限流; _sleep() 睡眠切片里也发心跳
  2. LiveReader 解析 hb 事件并跟踪 last_hb(不把它当普通事件回传给 store)
  3. LiveReader.is_orphan(): 活着却长时间无推进证据 → True; 健康/已退出 → False
  4. 启动期空窗(last_hb 为空、meta.t0 新鲜)不被误判为孤儿(防慢启动被误杀)
"""
import os
import sys
import time
import json
import shutil

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from engine import Engine
from live import LiveReader, LIVE_ROOT

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


print("[1] 引擎 _beat() 受心跳间隔限流")
eng = Engine(None, None)
calls = []
eng.set_heartbeat(lambda: calls.append(1))
eng._last_hb = 0.0                       # 强制"间隔已过"
eng._beat()
check(len(calls) == 1, "_beat() 首次立即发一拍")
eng._beat()                              # 刚发过, 间隔内不应再发
check(len(calls) == 1, "间隔内(5s)不再发")
eng._last_hb = 0.0
eng._beat()
check(len(calls) == 2, "间隔过了再发第二拍")
# 没接回调时 _beat 是空操作, 不报错
eng2 = Engine(None, None)
eng2._beat()
check(True, "无心跳回调时 _beat 安全空转")

print("\n[2] _sleep() 睡眠切片里也发心跳(长 sleep 不会变沉默)")
eng3 = Engine(None, None)
calls3 = []
eng3.set_heartbeat(lambda: calls3.append(1))
eng3.running = True
eng3._last_hb = 0.0
eng3._sleep(0.25, check_stop=False)      # ~3 个 0.1s 切片
check(len(calls3) == 1, "0.25s 睡眠里发且仅发 1 拍(限流生效)")

print("\n[3] LiveReader 解析 hb 事件并跟踪 last_hb")
port = 99998
ld = LIVE_ROOT / f"port_{port}"
try:
    ld.mkdir(parents=True, exist_ok=True)
    rid = "100_1"
    (ld / "meta.json").write_text(json.dumps(
        {"rid": rid, "port": port, "pid": os.getpid(), "t0": time.time()}))
    (ld / f"ev_{rid}.jsonl").write_text(
        json.dumps({"t": time.time(), "k": "hb"}) + "\n"
        + json.dumps({"t": time.time(), "k": "step_start", "i": 0, "desc": "x"}) + "\n")
    rd = LiveReader(port)
    evs = rd.poll()
    check(rd.last_hb > 0, "hb 事件被解析并写入 last_hb")
    check("hb" not in [e.get("k") for e in evs], "hb 不进增量事件流(界面不需要)")
    check(any(e.get("k") == "step_start" for e in evs), "普通事件照常回传")
finally:
    shutil.rmtree(ld, ignore_errors=True)

print("\n[4] LiveReader.is_orphan(): 活着却沉默 = 孤儿; 健康/已退出 = 否")
rd2 = LiveReader(99997)
rd2.meta = {"pid": os.getpid()}          # 真实活着的进程
rd2.last_hb = time.time() - 200          # 200s 没心跳
check(rd2.is_orphan(120) is True, "活着但 200s 无心跳 → 判孤儿")
rd2.last_hb = time.time()                # 刚心跳过
check(rd2.is_orphan(120) is False, "刚心跳过 → 不是孤儿")
rd2.meta = {"pid": 0}                    # 没有 pid
check(rd2.is_orphan(120) is False, "无 pid(已结束) → 不是孤儿(走 is_active 收口)")

print("\n[5] 启动期空窗不被误判(防慢启动被误杀)")
rd3 = LiveReader(99996)
rd3.meta = {"pid": os.getpid(), "t0": time.time()}   # 刚启动, 还没发过心跳
rd3.last_hb = 0.0
check(rd3.is_orphan(120) is False, "无心跳但 meta.t0 新鲜 → 不是孤儿(退回 t0 基线)")

print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
