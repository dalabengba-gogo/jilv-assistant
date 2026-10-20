# -*- coding: utf-8 -*-
"""状态灯端口探测: 离线端口不许再起 adb 进程(确定性, 不依赖模拟器)

v5.1.1 新增。事故背景(2026-10-19 用户回报):
  「刚才脚本弹了好几次黑色的命令行窗口」
排查结论: 所有 adb 调用都带 CREATE_NO_WINDOW(实测不弹窗), 真正的噪音是
**状态灯每 10 秒探 5 个端口**, 其中没开模拟器的 4 个每次都要跑一条
``adb connect``(自身重试 ~1.5s 才放弃) → 每分钟 ~24 个 adb 进程 + 同样数量
的 conhost.exe。控制台窗口一闪一闪就是它们带出来的。
修法: 补 connect 之前先用 socket 探本机端口, 没人监听就一条 adb 都不起。

本测试验证:
  1. _port_listening: 死端口 False / 监听端口 True
  2. probe_ports: 全是死端口时, 一条 connect 都不发
  3. probe_ports: 有端口在监听时, 仍然会给它发 connect(不能把功能砍掉)
"""
import os
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)
sys.path.insert(0, APP)

import device.adb_client as ac          # noqa: E402

ADB = os.path.join(APP, "tools", "adb", "adb.exe")

CALLS = []


def fake_run(adb, args, timeout):
    """替掉真实 adb: 只记账, 一律返回空(模拟"一台都没连上")"""
    CALLS.append(list(args))
    return ""


def reset():
    CALLS.clear()


def connects():
    return [c for c in CALLS if c and c[0] == "connect"]


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def main():
    ac._run_adb_quiet = fake_run

    # ---- 1. _port_listening ----
    dead = 16999
    check(ac._port_listening("127.0.0.1", dead) is False, "死端口应判 False")

    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    live_port = srv.getsockname()[1]
    try:
        check(ac._port_listening("127.0.0.1", live_port) is True, "监听端口应判 True")

        # ---- 2. 全死端口: 不许发 connect ----
        reset()
        states, _src = ac.probe_ports(ADB, ports=[dead, dead + 1])
        check(states == {dead: False, dead + 1: False}, f"死端口状态应为 False: {states}")
        check(not connects(), f"死端口不应发 adb connect, 实际发了: {connects()}")
        print(f"✔ 死端口 {dead}/{dead + 1}: 0 条 connect, 状态全 False")

        # ---- 3. 有监听端口: 必须发 connect ----
        reset()
        states, _src = ac.probe_ports(ADB, ports=[live_port])
        check(connects(), "监听端口应该补一条 adb connect, 实际没发")
        check(states.get(live_port) is False, "adb devices 没列它 → False")
        print(f"✔ 监听端口 {live_port}: 补了 connect {connects()}")
    finally:
        srv.close()

    # start-server / devices 不能被砍掉(否则 adb server 死了没人拉起)
    reset()
    ac.probe_ports(ADB, ports=[dead])
    args = [c[0] if c else "" for c in CALLS]
    check("start-server" in args, f"start-server 不能被跳过: {CALLS}")
    check("devices" in args, f"devices 不能被跳过: {CALLS}")
    print("✔ start-server / devices 仍在")

    print("\n全部通过 (probe_ports 不再为空端口起 adb)")


if __name__ == "__main__":
    main()
