# -*- coding: utf-8 -*-
"""ADB 设备控制模块

封装对 MuMu 模拟器的 adb 操作：连接、截图、点击、滑动、按键、启停 App。
借鉴工作区根目录 adb_mumu.sh 的既有经验：
  - 每次操作前 start-server + connect（保证连接有效）
  - 端口 127.0.0.1:16384
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from common import ADB_NOT_FOUND_MSG, find_adb_ex

# Windows 下隐藏 adb 的黑色控制台窗口(0x08000000 = CREATE_NO_WINDOW)
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class DeviceError(RuntimeError):
    """设备操作失败"""


class AdbDevice:
    """通过 adb 控制一个安卓设备(MuMu 模拟器)"""

    def __init__(self, adb_path: str | None = None, host: str = "127.0.0.1",
                 port: int = 16384, screen_size=(540, 960)):
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

    def shell(self, *cmd, timeout=20) -> str:
        """执行 adb shell 命令"""
        # 若设备离线先重连一次
        if not self.is_online():
            self._ensure_connected()
        return self._run(["-s", self.serial, "shell"] + list(cmd), timeout=timeout)

    # ---------- 屏幕 ----------
    def screenshot(self, save_path: str | Path | None = None) -> bytes:
        """截屏，返回 PNG 字节；可同时保存到文件"""
        if not self.is_online():
            self._ensure_connected()
        r = subprocess.run([self.adb, "-s", self.serial, "exec-out", "screencap", "-p"],
                           capture_output=True, timeout=20,
                           creationflags=_NO_WINDOW)
        if r.returncode != 0 or not r.stdout:
            raise DeviceError("截图失败")
        if save_path:
            Path(save_path).write_bytes(r.stdout)
        return r.stdout

    # ---------- 输入 ----------
    def tap(self, x: int, y: int):
        """点击屏幕坐标"""
        self.shell("input", "tap", str(int(x)), str(int(y)))
        time.sleep(0.3)

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

    def wake_and_unlock(self):
        """点亮屏幕并去掉锁屏"""
        self.key(26)            # POWER
        self.shell("wm", "dismiss-keyguard")
        time.sleep(0.5)

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
        """当前前台 Activity"""
        out = self.shell("dumpsys", "activity", "activities", timeout=15)
        for line in out.splitlines():
            if "topResumedActivity" in line:
                return line.split("topResumedActivity=")[-1].split()[0]
        return ""

    def is_app_front(self, package: str) -> bool:
        """指定包名是否在前台"""
        return self.current_activity().startswith(package)
