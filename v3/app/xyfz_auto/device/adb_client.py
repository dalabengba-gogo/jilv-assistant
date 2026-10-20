# -*- coding: utf-8 -*-
"""ADB 设备控制模块

封装对 MuMu 模拟器的 adb 操作：连接、截图、点击、滑动、按键、启停 App。
借鉴工作区根目录 adb_mumu.sh 的既有经验：
  - 每次操作前 start-server + connect（保证连接有效）
  - 端口 127.0.0.1:16384
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from common import ADB_NOT_FOUND_MSG, find_adb_ex

# Windows 下隐藏 adb 的黑色控制台窗口(0x08000000 = CREATE_NO_WINDOW)
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class DeviceError(RuntimeError):
    """设备操作失败"""


def decode_screencap_raw(data: bytes):
    """解析 `adb exec-out screencap` 的原始像素流, 返回 BGR ndarray(None=不认识)

    2026-10-19 v5.5.0: 独立成函数是为了能脱离设备单测 —— 这段解析错一个字节,
    整屏像素就**整体平移 1 个像素**(实测踩过: 与 PNG 版比对时 dx 恒为 +1.00),
    而画面肉眼看不出差别, 只有固定坐标点击和模板匹配会悄悄偏。
    格式: 头(宽/高/像素格式[/colorspace]) + 像素数据, 头长度按(总长 - 像素数据)反推
    —— MuMu 实测 16 字节, 老设备 12 字节, 不要写死。
    """
    try:
        import struct

        import numpy as np
        if not data or len(data) < 16:
            return None
        w, h, fmt = struct.unpack_from("<III", data, 0)
        if not (0 < w <= 4096 and 0 < h <= 4096):
            return None
        bpp = 4 if fmt in (1, 2) else (3 if fmt == 3 else 0)
        if not bpp:
            return None
        need = w * h * bpp
        head = len(data) - need
        # 已知两种头长: 12(宽/高/格式) 与 16(多一个 colorspace, MuMu 实测)。
        # 一律用"白名单"而不是范围判断 —— 数据缺一截时算出来的头长会落在
        # 8/20 这种"看着也合理"的值上, 范围判断会把残帧当成好帧解析。
        if head not in (12, 16):
            return None
        arr = np.frombuffer(data, np.uint8, count=need,
                            offset=head).reshape(h, w, bpp)
        return np.ascontiguousarray(arr[:, :, [2, 1, 0]])   # RGB(A) → BGR
    except Exception:
        return None


class AdbDevice:
    """通过 adb 控制一个安卓设备(MuMu 模拟器)"""

    def __init__(self, adb_path: str | None = None, host: str = "127.0.0.1",
                 port: int = 16384, screen_size=(540, 960), connect: bool = True):
        # 2026-09-24: 不再直接信任 config 里那条路径——它可能指向别的机器/
        # 别的盘符。交给 find_adb_ex 做多级回退(配置→包内自带→PATH→MuMu常见位置)。
        resolved, source = find_adb_ex(adb_path)
        if not resolved:
            raise DeviceError(ADB_NOT_FOUND_MSG)
        self.adb = resolved
        self.adb_source = source        # 供界面显示, 便于排查换机后的路径问题
        self.host = host
        self.port = port
        self.serial = f"{host}:{port}"
        self.screen_w, self.screen_h = screen_size
        # 2026-10-16: connect=False 时"只建对象不连设备"。构造里那句 _ensure_connected()
        # 会跑 start-server + connect 两条子进程; 端口没人监听时 adb connect 自己要
        # 重试几秒才回来 → 在 GUI 主线程构造就等于启动卡住几秒。界面只拿它当"本机
        # 通道句柄"用时不需要立刻连, shell/screenshot 本来就会按需自愈(见下面各方法)。
        if connect:
            self._ensure_connected()

    # ---------- 基础命令 ----------
    def _run(self, args, timeout=15) -> str:
        """执行 adb 命令，返回 stdout"""
        cmd = [self.adb] + args
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=timeout, encoding="utf-8", errors="ignore",
                               creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise DeviceError(f"adb 命令超时: {cmd}")
        except FileNotFoundError:
            raise DeviceError(f"adb 可执行文件不存在: {self.adb}\n{ADB_NOT_FOUND_MSG}")
        except OSError as e:
            raise DeviceError(f"无法启动 adb ({self.adb}): {e}")
        if r.returncode != 0 and "device 'offline'" not in (r.stderr or ""):
            # 设备离线等错误直接抛出；offline 时上层可重连
            err = (r.stderr or "").strip() or (r.stdout or "").strip()
            raise DeviceError(f"adb 失败: {cmd}\n{err}")
        return r.stdout or ""

    def _ensure_connected(self):
        """启动 server 并确保连接到目标设备"""
        self._run(["start-server"], timeout=15)
        # 已连接则忽略 connect 报错
        subprocess.run([self.adb, "connect", self.serial],
                       capture_output=True, text=True, timeout=15,
                       creationflags=_NO_WINDOW)

    def is_online(self) -> bool:
        """设备是否在线"""
        out = self._run(["devices"])
        for line in out.splitlines():
            if line.startswith(self.serial) and "device" in line:
                return True
        return False

    def reconnect(self) -> bool:
        """显式重连一次并返回是否在线(界面状态探测/自愈用)

        模拟器比助手晚启动时用得上: adb 通道对象还在, 只是设备当时不在列表里。
        """
        self._ensure_connected()
        return self.is_online()

    def shell(self, *cmd, timeout=20) -> str:
        """执行 adb shell 命令

        2026-10-19 v5.5.0(性能): 去掉了**每次调用前**的 ``is_online()`` 预检。
        实测 `adb devices` 单次 136ms, 一轮日常 350 次动作 + 417 次截图 ≈ 770 次
        预检 = **1 分 45 秒纯开销**, 而且设备真掉线时预检也救不回来(下一条命令
        照样失败)。改成"先跑, 失败再自愈": 只有真的失败才重连 + 重试一次 ——
        既省掉常态开销, 又比"预检通过后照样失败"更可靠。
        """
        try:
            return self._run(["-s", self.serial, "shell"] + list(cmd), timeout=timeout)
        except DeviceError:
            # 掉线自愈: 重连一次再试。仍失败才把原始错误抛给上层(引擎会记账)
            self._ensure_connected()
            return self._run(["-s", self.serial, "shell"] + list(cmd), timeout=timeout)

    # ---------- 屏幕 ----------
    def screenshot(self, save_path: str | Path | None = None) -> bytes:
        """截屏，返回 PNG 字节；可同时保存到文件

        2026-10-19 v5.5.0(性能): 同样的预检改动 —— 见 shell() 的说明。
        截图是一轮任务里调用最频繁的 adb 操作(实测 417 次/轮), 省掉每次
        136ms 的 `adb devices` 相当于白捡一分多钟。
        """
        args = [self.adb, "-s", self.serial, "exec-out", "screencap", "-p"]
        r = subprocess.run(args, capture_output=True, timeout=20,
                           creationflags=_NO_WINDOW)
        if r.returncode != 0 or not r.stdout:
            # 先判是不是掉线: 重连一次再拍, 仍失败才算真失败
            self._ensure_connected()
            r = subprocess.run(args, capture_output=True, timeout=20,
                               creationflags=_NO_WINDOW)
        if r.returncode != 0 or not r.stdout:
            raise DeviceError("截图失败")
        if save_path:
            Path(save_path).write_bytes(r.stdout)
        return r.stdout

    def screenshot_bgr(self):
        """截屏并直接返回 BGR ndarray —— 比 PNG 快约一倍

        2026-10-19 v5.5.0(性能): 实测同一个模拟器
            exec-out screencap -p (PNG)   684ms / 860KB
            exec-out screencap    (RAW)   359ms / 2025KB
        慢的是**设备端**的 PNG 编码(不是传输), 所以宁可多传 1MB 也要省这 325ms。
        一轮日常 150~400 次截图 = 省 50~130 秒。
        RAW 格式是 头(宽/高/像素格式[/colorspace]) + 像素数据, 常见 RGBA_8888。
        任何一步对不上(老设备/带行填充/格式未知)就**整条退回 PNG**, 保证不会因为
        追求速度把画面搞错(解析见 decode_screencap_raw)。
        """
        r = subprocess.run([self.adb, "-s", self.serial, "exec-out", "screencap"],
                           capture_output=True, timeout=20,
                           creationflags=_NO_WINDOW)
        if r.returncode != 0 or len(r.stdout) < 16:
            self._ensure_connected()
            r = subprocess.run(
                [self.adb, "-s", self.serial, "exec-out", "screencap"],
                capture_output=True, timeout=20, creationflags=_NO_WINDOW)
        img = decode_screencap_raw(r.stdout)
        if img is not None:
            return img
        # 回退: 老路(PNG 解码)
        from common import png_bytes_to_bgr
        return png_bytes_to_bgr(self.screenshot())

    # ---------- 输入 ----------
    def tap(self, x: int, y: int):
        """点击屏幕坐标

        2026-10-19 v5.5.0(性能): 内置停顿 0.3 → 0.12 秒。实测一次 tap 总耗时
        435ms = shell 往返 135ms + sleep 300ms, 也就是说"防抖"占了七成 ——
        一轮日常 350 次点击白花 1 分 45 秒。`input tap` 在设备端是同步派发的,
        135ms 已经包含派发时间; 留 0.12 秒足够游戏把这一下收进队列。
        """
        self.shell("input", "tap", str(int(x)), str(int(y)))
        time.sleep(0.12)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300):
        """滑动"""
        self.shell("input", "swipe", str(int(x1)), str(int(y1)),
                   str(int(x2)), str(int(y2)), str(int(duration_ms)))

    def key(self, keycode: int):
        """按键，如 3=HOME 4=BACK 26=POWER"""
        self.shell("input", "keyevent", str(keycode))

    def text(self, s: str):
        """输入文本（仅 ASCII 可靠）"""
        self.shell("input", "text", s)

    def home(self):
        self.key(3)

    def back(self):
        self.key(4)

    # ---------- App ----------
    def start_app(self, package: str, activity: str | None = None):
        """启动 App（默认用 monkey 方式唤起主入口）"""
        if activity:
            self.shell("am", "start", "-n", f"{package}/{activity}")
        else:
            self.shell("monkey", "-p", package,
                       "-c", "android.intent.category.LAUNCHER", "1")
        time.sleep(1)

    def current_activity(self) -> str:
        """当前前台 Activity(包名/类名); 取不到返回空串

        2026-10-15 修: 原写法取 split()[0] 拿到的是 "ActivityRecord{a07ba5c" 这种
        外壳而不是包名 —— 界面那行一直显示 ActivityRecord{...}, 且 is_app_front()
        因此永远判 False。真实行长这样:
          topResumedActivity=ActivityRecord{a07ba5c u0 com.netease.xyfz_02/... t363}
        """
        out = self.shell("dumpsys", "activity", "activities", timeout=15)
        for line in out.splitlines():
            if "topResumedActivity" not in line:
                continue
            tail = line.split("topResumedActivity=", 1)[-1].strip()
            if tail in ("", "null"):
                return ""
            m = re.search(r"\bu\d+\s+(\S+)", tail)      # u0 后面那个 token 就是包名/类名
            if m:
                return m.group(1)
            return tail.split()[0]                       # 退化兜底
        return ""

    def is_app_front(self, package: str) -> bool:
        """指定包名是否在前台"""
        return self.current_activity().startswith(package)


# ---------- 多端口批量探测(界面状态灯专用) ----------
def _run_adb_quiet(adb: str, args, timeout: float) -> str:
    """跑一条 adb 命令; 超时/起不来一律吞掉返回 ""(批量探测用, 不想抛异常)"""
    try:
        r = subprocess.run([adb] + list(args), capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="ignore",
                           creationflags=_NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return r.stdout or ""


def _online_serials(adb: str, timeout: float = 8.0,
                    host: str = "127.0.0.1", ports=()) -> set[str]:
    """adb devices 里处于 device 状态的 serial 集合

    2026-10-16: 只认这些端口自己的 serial(host:port)。以前"看到任意一台设备在线"
    就当作目标在线是不对的 —— 多开时 5 个 MuMu 实例各是独立 serial。
    """
    out = _run_adb_quiet(adb, ["devices"], timeout)
    got: set[str] = set()
    for line in out.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            got.add(parts[0])
    return got


def _port_listening(host: str, port: int, timeout: float = 0.12) -> bool:
    """本机端口是否有程序在监听(纯 TCP 连接, 不起任何子进程)

    2026-10-19 v5.1.1: 状态灯每 10 秒探一次 5 个端口, 其中没开模拟器的那些
    每次都要跑一条 ``adb connect``(自身重试 ~1.5s 才放弃)。实测在只有一个
    模拟器在跑时, 4 个离线端口 × 每 10 秒 = 每分钟 ~24 个 adb 进程 + 同样
    数量的 conhost.exe —— CPU/句柄都在抖, 用户也能看到控制台窗口一闪一闪。
    先用系统 socket 探一下端口, 没人监听就直接判"不在线", 一条 adb 都不用起。
    """
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def probe_ports(adb_path: str | None = None, host: str = "127.0.0.1", ports=(),
                connect_timeout: float = 1.5, start_timeout: float = 12.0):
    """批量探测多个 ADB 端口是否在线 —— 界面状态灯专用(便宜 + 适合放后台线程)。

    为什么不用逐个 AdbDevice.reconnect(): 那是**每端口**3 条 adb 子进程
    (start-server + connect + devices)。2026-10-16 实测: 在线端口约 0.6s,
    而**没人监听的端口要 ~4.6s**(adb connect 自己会做几轮 TCP 重试才放弃)。
    5 个端口串起来 = 3~19 秒, 每 10 秒来一次, 放在 GUI 主线程里就是"卡死/无响应"。
    现在改成: 1 次 start-server(仅一次) + 1 次 devices, 只有**没出现在列表里**的
    端口才补一条 connect(短超时, 到点杀进程), 最后再 devices 复核一遍。

    2026-10-19 v5.1.1 再加一道闸: 补 connect 之前先用 socket 探本机端口,
    **没人监听的端口一条 adb 都不起**(见 _port_listening)。
    """
    resolved, source = find_adb_ex(adb_path)
    if not resolved:
        raise DeviceError(ADB_NOT_FOUND_MSG)

    want = [int(p) for p in ports]
    if not want:
        return {}, source

    _run_adb_quiet(resolved, ["start-server"], start_timeout)
    online = _online_serials(resolved, host=host)
    # ★ 先筛"端口真有人监听"的: 没开模拟器的端口 connect 必然失败,
    #   白白起一个进程 + 一个 conhost, 还要等满超时。
    missing = [p for p in want
               if f"{host}:{p}" not in online and _port_listening(host, p)]
    if missing:
        # 已连上的秒回; 端口没人监听时 adb 要重试好几秒 → 短超时把它掐掉,
        # 结果由后面那次 devices 复核决定, 掐掉不影响正确性。
        # 2026-10-16: 并行发起 —— 死端口那条必然吃满超时, 串行就是 N×超时;
        # 并行后总耗时≈一个超时。反正整只函数已跑在后台线程, 没必要再慢吞吞。
        with ThreadPoolExecutor(max_workers=min(len(missing), 8)) as ex:
            list(ex.map(
                lambda p: _run_adb_quiet(
                    resolved, ["connect", f"{host}:{p}"], connect_timeout),
                missing))
        online = _online_serials(resolved, host=host)
    return {p: (f"{host}:{p}" in online) for p in want}, source
