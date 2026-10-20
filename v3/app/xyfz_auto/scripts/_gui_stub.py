# -*- coding: utf-8 -*-
"""offscreen GUI 回归测试的公共桩 (2026-10-18 v4.7.0 批次四·工程)

三个脚本都要构造真的 MainWindow, 而它构造时会起后台线程 —— 抽出公用桩,
免得每个脚本各打一份补丁、各踩一遍坑。

文件名 `_` 开头: run_tests.py 只收集 `test_*.py`, 不会被当成测试跑。
"""
from __future__ import annotations

import os
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]      # app/xyfz_auto/
GUI_DIR = APP_ROOT / "gui"


def no_disclaimer(MainWindow) -> None:
    """300ms 后的免责声明弹窗: offscreen 下没人点 → 模态框堵死 processEvents。"""
    MainWindow._maybe_show_disclaimer = lambda self: None


def sync_background(MainWindow) -> None:
    """把 `_run_bg` 改成**同步直跑**, 不再起 QThread。

    为什么必须这样做(踩过的坑):
      MainWindow 构造时会 `_run_bg(matcher.reload)` 去后台重读 65 张模板。
      脚本跑完时那个 FuncWorker 若还没结束, 紧接着 Python 回收它 → C++ 析构
      → Qt 判定"线程还在跑就拆对象" → qFatal("QThread: Destroyed while thread
      is still running") → 进程以 0xC0000409 硬崩。
      表现极具迷惑性: 屏幕上是 `ALL PASS`, 但退出码非 0, CI 照样判失败。

      真实 GUI 退出走 `_full_shutdown`(里面会 wait 收尾), 所以线上路径是安全
      的; 测试脚本压根不走那条路, 撞这个是活该 —— 但它测的东西(UI 结构、
      状态推导)跟后台线程毫无关系, 让前台同步跑反而更快、更确定、无计时依赖。

    ⚠️ 只适用于"被测逻辑与后台线程无关"的脚本。要测 `_run_bg` 本身行为的脚本
    (如 test_bg_shot) **不能**用这个。
    """
    def _sync_run_bg(self, fn, on_done=None, on_fail=None, busy_text=None):
        try:
            r = fn()
        except Exception as e:                                   # noqa: BLE001
            if on_fail:
                on_fail(f"{type(e).__name__}: {e}")
            return
        if on_done:
            on_done(r)

    MainWindow._run_bg = _sync_run_bg


def gui_sources() -> str:
    """整个 gui/ 目录下所有 .py 拼起来。

    2026-10-18: main_window.py 拆出了若干 Mixin 之后, 凡是 `open("gui/main_window.py")
    .read()` 再去 find("def xxx") 的静态断言都会失灵 —— 方法已经不在那个文件里了。
    静态断言要跟着**方法**走, 不能钉死在某个文件上, 否则拆一次文件就误报一次。
    """
    out = []
    for p in sorted(GUI_DIR.glob("*.py")):
        try:
            out.append(p.read_text(encoding="utf-8"))
        except OSError:
            pass
    return "\n".join(out)


def method_source(name: str, win: int = 1400) -> tuple[str, str]:
    """在 gui/ 全部源码里找 `def <name>` 的实现, 返回 (文件名, 片段)

    找不到返回 ("", "") —— 调用方应当 check 文件名非空, 那样日后方法被改名/删除
    时报的是"找不到 _xxx"而不是"断言不通过", 一眼就知道是断言过期而非功能没了。
    """
    for p in sorted(GUI_DIR.glob("*.py")):
        try:
            t = p.read_text(encoding="utf-8")
        except OSError:
            continue
        i = t.find("def " + name)
        if i >= 0:
            return p.name, t[i:i + win]
    return "", ""
