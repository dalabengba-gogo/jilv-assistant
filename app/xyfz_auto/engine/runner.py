# -*- coding: utf-8 -*-
"""任务执行引擎(核心)

执行循环：对每个步骤
  1) wait    —— 反复截图识别，直到画面出现期望特征（模板/OCR关键词/任一）
  2) action  —— 执行点击/滑动/按键等操作
  3) verify  —— (可选)再识别，确认操作生效
  超时/失败  —— 按 on_fail 策略处理 (retry/stop/continue/goto)

对外提供事件回调，GUI/实时面板订阅即可获得：
  on_step_start(idx, step)     步骤开始
  on_log(level, msg)           日志行
  on_screenshot(screen_bgr)    每轮识别的画面(供实时预览)
  on_step_end(idx, step, ok)   步骤结束
"""
from __future__ import annotations

import time
from typing import Callable

import cv2

from common import png_bytes_to_bgr
from engine.model import Step, Task
from engine.ui_registry import UiRegistry, DEFAULT_PATH as UI_REGISTRY_PATH

# 事件回调类型
StepCallback = Callable[[int, Step], None]
LogCallback = Callable[[str, str], None]   # (level, message)
ScreenCallback = Callable[[object], None]


class Engine:
    def __init__(self, device, matcher, ocr=None, poll_interval=1.0,
                 logger=None, debug_dump_dir=None, ui=None, win_input=None):
        self.device = device
        self.matcher = matcher
        self.ocr = ocr                    # OCR 引擎
        self.ui: UiRegistry | None = ui   # 按钮注册表(支持中文按钮名)
        self.win_input = win_input        # Windows 鼠标输入通道(绕反注入)
        self._win_failed = False          # win 通道不可用(已告警过, 不再重复尝试)
        self._cfg_cache = None            # 惰性加载的 config(读 win_input 段)
        self.poll_interval = poll_interval
        self.logger = logger
        self.debug_dump_dir = debug_dump_dir  # 失败时存截图供排查
        self.running = False
        self._stop_at = None             # 停止请求时间戳(算停止响应耗时)
        self._last_screen = None
        # 2026-09-11 性能优化: 同一张截图的 OCR 结果缓存。
        # 旧版 any[...]/not:{any:[...]} 会对同一 screen 逐个子条件调用 _ocr_find,
        # 每次重新跑一遍 OCR(单次约2.4s), 7个目标 = 每轮约17s;
        # 监控循环里"任务栏无任何条目"正是7个OCR目标, 单次检查实测耗时40~80s。
        # 现改为每张截图只识别一次, 所有子条件复用。
        self._ocr_cache_screen = None
        self._ocr_cache_boxes = None

        # 事件订阅
        self._on_step_start: list[StepCallback] = []
        self._on_log: list[LogCallback] = []
        self._on_screenshot: list[ScreenCallback] = []
        self._on_step_end: list = []

    # ---------- 订阅 ----------
    def subscribe(self, *, step_start=None, log=None, screenshot=None, step_end=None):
        if step_start:
            self._on_step_start.append(step_start)
        if log:
            self._on_log.append(log)
        if screenshot:
            self._on_screenshot.append(screenshot)
        if step_end:
            self._on_step_end.append(step_end)

    def _emit_log(self, level: str, msg: str):
        if self.logger:
            getattr(self.logger, level)(msg)
        for cb in self._on_log:
            try:
                cb(level, msg)
            except Exception:
                pass

    def _emit_screenshot(self, screen):
        self._last_screen = screen
        for cb in self._on_screenshot:
            try:
                cb(screen)
            except Exception:
                pass

    # ---------- 识别原语 ----------
    def _grab(self):
        """截屏并解码"""
        png = self.device.screenshot()
        screen = png_bytes_to_bgr(png)
        # 新截图 → 作废上一张的 OCR 缓存
        self._ocr_cache_screen = None
        self._ocr_cache_boxes = None
        self._emit_screenshot(screen)
        return screen

    def _ocr_boxes(self, screen):
        """对截图跑 OCR, 同一张截图复用结果(性能关键)"""
        if self.ocr is None:
            raise NotImplementedError("OCR 引擎尚未接入，OCR 条件不可用")
        if self._ocr_cache_screen is not screen:
            self._ocr_cache_boxes = self.ocr.recognize(screen)
            self._ocr_cache_screen = screen
        return self._ocr_cache_boxes

    def _recognize_wait(self, cond: dict, screen):
        """判断截图是否满足 wait/verify 条件，命中返回结果对象，未命中返回 None

        cond: {"template": name} | {"ocr": "关键词"} | {"any":[ ... ]}
             | {"visible": 按钮名} | {"ui": 界面名} | {"not": {...}}
             | {"ocr_pair": [徽标文本, 目标文本]}  —— 两段文字纵向相邻(同一卡片行)才算命中,
               用于玩法面板"已完成"徽标与任务名的配对判定(2026-09-11 新增)
             | {"ocr_row": {has:[...], not:[...], region:[x1,y1,x2,y2]}}
               —— 按"行"查找: 文本框需含 has 全部关键词、且不含 not 任一关键词,
               可限定中心落在 region 内(2026-09-28 新增, 见 _ocr_row 说明)
        """
        if not cond:
            return object()  # 无条件视为命中

        if "ocr_row" in cond:
            return self._ocr_row(screen, cond["ocr_row"])

        if "ocr_pair" in cond:
            # ocr_pair: [锚文本, 目标文本] —— 玩法面板卡片上"已完成"徽标在卡名
            # 左上方约 21px 处, 中心 y 相差 <45px 视为同一张卡。
            # 单独 OCR"已完成"会误伤其他卡片的徽标, 必须与卡名配对。
            # 2026-10-08 新增可选 pair_min_score: 目标框(卡名)置信度门槛。
            # 事故: 另一台电脑账号2 的面板底部已完成沉底卡区, OCR 把被"新消息"
            # 气泡压住的卡名误读成「师门任务」(score 0.652) → 与真实"已完成"
            # 徽标假配对 → 师门被误跳过。真卡名是大号美术字, 置信度通常 ≥0.8,
            # 门槛默认关(兼容旧行为), 用到的地方在 yaml 里显式给 pair_min_score。
            anchor_kw, target_kw = cond["ocr_pair"]
            min_score = float(cond.get("pair_min_score") or 0)
            boxes = self._ocr_boxes(screen)
            targets = [b for b in boxes if target_kw in b["text"]
                       and (not min_score or b.get("score", 0.0) >= min_score)]
            if not targets:
                return None
            for a in boxes:
                if anchor_kw not in a["text"]:
                    continue
                ay = a["y"] + a["h"] / 2
                for t in targets:
                    ty = t["y"] + t["h"] / 2
                    if abs(ay - ty) < 45:
                        return t
            return None

        if "ui" in cond:
            if self.ui is None:
                raise ValueError("引擎未挂载 UI 注册表，wait:{ui:...} 不可用")
            return self._recognize_wait(self.ui.screen_condition(cond["ui"]), screen)
        if "visible" in cond:
            if self.ui is None:
                raise ValueError("引擎未挂载 UI 注册表，wait:{visible:...} 不可用")
            return self._recognize_wait(self.ui.visible_condition(cond["visible"]), screen)
        if "template" in cond:
            return self.matcher.match_one(screen, cond["template"])
        if "ocr" in cond:
            return self._ocr_find(screen, cond["ocr"])
        if "any" in cond:
            for sub in cond["any"]:
                # 2026-09-13 防呆: 裸字符串在旧版会被静默跳过(永假), 曾导致
                # 守卫 wait:{any:[创建队伍,邀请入队]} 永远失败、师门/五环被误判已完成。
                # 必须写完整条件字典, 写错就大声报错而不是无声失败。
                if not isinstance(sub, dict):
                    raise ValueError(
                        f"any 列表项必须是条件字典(如 {{'ocr': 'xx'}} / {{'template': 'xx'}})，"
                        f"收到裸值: {sub!r}")
                r = self._recognize_wait(sub, screen)
                if r:
                    return r
            return None
        if "all_of" in cond:
            # 2026-09-23 新增: 全部子条件都成立才算命中。
            # 背景: 域外批次死循环 8h 事故 —— 「主界面」只查底栏按钮, 而战斗
            # 过渡期底栏短暂可见 → 战斗中被误判"已回主界面" → 后续复核链在
            # 战斗间隙全部假通过 → 盲点重开面板把面板点开 → 死循环无出口。
            # 主界面现定义为 all_of:[底栏在, 不在战斗], 全局消灭这类误判。
            for sub in cond["all_of"]:
                if not isinstance(sub, dict):
                    raise ValueError(
                        f"all_of 列表项必须是条件字典，收到裸值: {sub!r}")
                if not self._recognize_wait(sub, screen):
                    return None
            return object()
        if "not" in cond:
            # not: 用于"等待某物消失"(例如加载完成/弹窗关闭)
            sub = cond["not"]
            if not isinstance(sub, dict):
                raise ValueError(
                    f"not 条件必须是条件字典，收到裸值: {sub!r}")
            r = self._recognize_wait(sub, screen)
            return None if r else object()
        return None

    def _ocr_find(self, screen, keyword: str, region=None):
        """OCR 找含关键词的文字框; region=[x1,y1,x2,y2] 时只接受
        框中心落在该区域内的命中(2026-09-29 新增, 用于页签等局部定位,
        避免同屏其他位置的相同文字干扰)。"""
        boxes = self._ocr_boxes(screen)
        if region:
            x1, y1, x2, y2 = region
            boxes = [b for b in boxes
                     if x1 <= b["x"] + b["w"] // 2 <= x2
                     and y1 <= b["y"] + b["h"] // 2 <= y2]
        for b in boxes:
            if keyword in b["text"]:
                return b
        return None

    def _ocr_row(self, screen, spec):
        """按"行"查找文字框: 需含 has 全部关键词、且不含 not 任一关键词。

        2026-09-28 新增。用于同屏多行、且行内带状态标记的场景 —— 典型是主界面
        右侧任务栏: 未完成的一条龙子任务行为「挖宝封妖0/10」, 同族的已完成行为
        「域外风情10/10V」。只用 {"ocr": "挖宝封妖"} 无法区分状态; 而任务栏左下角
        的世界聊天气泡里出现同名文字时还会误命中(2026-09-15 打开聊天界面事故)。
        因此 spec 支持:
          has:    关键词 或 [关键词...], 文本框必须全部包含
          not:    关键词 或 [关键词...], 文本框一个都不能含(默认空)
          region: [x1,y1,x2,y2], 只接受"框中心"落在该区域内的框(默认全屏)
        多个命中取最靠上的一行(与"先做靠前的任务"一致, 且不受 OCR 返回顺序影响)。
        """
        if not isinstance(spec, dict):
            raise ValueError(
                f"ocr_row 必须是条件字典 {{has:[...], not:[...], region:[...]}}，"
                f"收到裸值: {spec!r}")
        has = spec.get("has") or []
        has = [has] if isinstance(has, str) else [str(k) for k in has]
        neg = spec.get("not") or []
        neg = [neg] if isinstance(neg, str) else [str(k) for k in neg]
        if not has:
            raise ValueError("ocr_row 缺少 has 关键词")
        region = spec.get("region")

        hits = []
        for b in self._ocr_boxes(screen):
            text = b.get("text", "")
            if not all(k in text for k in has):
                continue
            if any(k in text for k in neg):
                continue
            if region:
                cx = b["x"] + b["w"] / 2
                cy = b["y"] + b["h"] / 2
                x1, y1, x2, y2 = region
                if not (x1 <= cx <= x2 and y1 <= cy <= y2):
                    continue
            hits.append(b)
        if not hits:
            return None
        hits.sort(key=lambda b: (b["y"], b["x"]))
        return hits[0]

    # ---------- 停止响应 ----------
    # 2026-09-16 事故修复: 用户反馈"停止任务耗时很长时间"。
    # 根因: 停止是协作式的, 只在固定检查点生效 ——
    #   ① sleep 动作直接用 time.sleep() 睡到底(脚本里有 60s/150s), 完全不可中断;
    #   ② _wait_until 每轮轮询检查一次, 而一轮 = 截图(实测 adb screencap 约 2.6s)
    #      + 识别(OCR 约 2.4s) + 轮询间隔 sleep(1s) ≈ 6s, 停止最多要等一轮;
    #   ③ 重试间隔/界面切换动画的 sleep 同样不可中断。
    # 现统一走 _sleep(): 以 0.1s 为步长切片, 每片检查停止标志 → 停止最迟 0.1s 生效。
    def _check_stop(self):
        """停止标志检查; 已请求停止则抛 InterruptedError"""
        if not self.running:
            raise InterruptedError("已停止")

    def _sleep(self, seconds, check_stop: bool = True) -> bool:
        """可中断等待。返回 True=睡满; False=睡眠途中被停止"""
        end = time.time() + max(0.0, float(seconds))
        while True:
            if check_stop and not self.running:
                return False
            remain = end - time.time()
            if remain <= 0:
                return True
            time.sleep(min(0.1, remain))

    def _wait_until(self, cond: dict, timeout: float, desc: str):
        """轮询直到条件满足；返回命中对象；超时抛 TimeoutError；被停止抛 InterruptedError"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self._check_stop()
            screen = self._grab()
            self._check_stop()          # 截图耗时约 2.6s, 期间按了停止也别白等下一轮
            hit = self._recognize_wait(cond, screen)
            if hit:
                self._emit_log("debug", f"{desc} 命中: {hit}")
                return hit
            # 轮询间隔改为可中断, 停止请求不再被 1s 的 sleep 拖延
            if not self._sleep(self.poll_interval):
                raise InterruptedError("已停止")
        raise TimeoutError(f"{desc} 超时({timeout}s)")

    # ---------- Windows 输入通道(带降级) ----------
    def _win_cfg(self) -> dict:
        """win_input 段配置(整份 config 只需读一次)"""
        if self._cfg_cache is None:
            try:
                from common import load_config
                self._cfg_cache = load_config()
            except Exception:
                self._cfg_cache = {}
        wi = self._cfg_cache.get("win_input", {}) if isinstance(self._cfg_cache, dict) else {}
        return wi if isinstance(wi, dict) else {}

    def max_sleep_seconds(self) -> float:
        """脚本固定停顿(sleep)的全局上限 —— config.yaml → engine.max_sleep_seconds。

        2026-09-29 新增: 想整体压缩脚本里的干等, 不必去改每个任务脚本,
        改这一处即可(0 或未配置 = 不限制, 按脚本里写的秒数睡)。

        ⚠️ 注意: 有几个停顿是"安全窗口", 压狠了会出事故 ——
          · 组队一条「监控-安静确认」4×60 秒 = 连续 4 分钟安静, 用来跨过实测
            最长约 3 分钟的赶路间隙(09-11 事故: 提前收尾与一条龙抢画面报错)
          · 地煞「等12秒」= 游戏强制按钮 10 秒后才可点
        这些建议保持原值; 域外/师门/五环的收尾停顿压到 10~15 秒是安全的
        (后面都有「复核仍主界面/无战斗」把关, 不通过会跳回长等)。
        """
        if self._cfg_cache is None:
            try:
                from common import load_config
                self._cfg_cache = load_config()
            except Exception:
                self._cfg_cache = {}
        eng = self._cfg_cache.get("engine", {}) if isinstance(self._cfg_cache, dict) else {}
        try:
            return float(eng.get("max_sleep_seconds", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def win_channel_status(self) -> str:
        """win 通道当前状态(供界面/日志显示)"""
        if self.win_input is not None:
            return f"可用 · {self.win_input}"
        if self._win_failed:
            return "不可用(已降级为 adb 点击)"
        return "未初始化"

    def _ensure_win_input(self) -> bool:
        """初始化 Windows 鼠标通道。失败时按配置降级, 避免中断整条任务。

        2026-09-28 (v1.4.0): 旧版在 via:win 步骤直接 WinMouse() —— 换台机器
        窗口标题不同就抛异常, 481/718 步的任务从这一步整条崩掉。现在:
          · 找不到窗口 → 告警一次 + 自动改用 adb 点击, 任务继续往后跑;
          · 想让它必须成功, 把 config.yaml 的 win_input.fallback_adb 设为 false。
        """
        if self.win_input is not None:
            return True
        if self._win_failed:
            return False
        wi = self._win_cfg()
        if wi.get("enabled", True) is False:
            self._win_failed = True
            self._emit_log("warning", "win 通道已在 config.yaml 中禁用(win_input.enabled=false), "
                                      "本步及后续 via:win 步骤改用 adb 点击")
            return False
        try:
            from device.win_input import WinMouse
            self.win_input = WinMouse.from_config(self._cfg_cache)
            self._emit_log("info", f"已初始化 Windows 鼠标输入通道(绕反注入): "
                                   f"{self.win_input.describe()}")
            return True
        except Exception as e:
            self._win_failed = True
            self._emit_log("warning", f"win 通道不可用: {e}")
            if wi.get("fallback_adb", True):
                self._emit_log("warning",
                               "已自动降级为 adb 点击继续执行(该按钮若对 adb 点击无响应, "
                               "可能本步验证失败, 但任务不会中断)。"
                               "可在 config.yaml 的 win_input 段指定 hwnd/关键字修复。")
                return False
            raise

    # ---------- 动作 ----------
    def _do_action(self, step: Step, hit, task: Task | None = None):
        act = step.action
        t = act.get("type", "click") if isinstance(act, dict) else "click"

        if t == "click":
            # 懒加载: 首次用到才初始化 Windows 鼠标通道; 不可用则降级
            via_win = act.get("via") == "win" and self._ensure_win_input()

            def _tap(x, y, label=""):
                if via_win:
                    self.win_input.tap(x, y)
                    self._emit_log("info", f"点击{label} ({x},{y}) [win通道]")
                else:
                    self.device.tap(x, y)
                    if label:
                        self._emit_log("info", f"点击{label} ({x},{y})")

            if "ocr" in act:
                # OCR 定位点击: 找到含关键词的文字框,点其中心(适合位置会变的卡片/按钮)
                # 2026-09-29: 支持 region=[x1,y1,x2,y2] 限定框中心范围 —— 用于
                # 「任务/队伍」页签这类局部定位, 防止同屏其他位置的相同文字干扰
                screen = self._grab()
                box = self._ocr_find(screen, str(act["ocr"]), act.get("region"))
                if box is None:
                    hint = f"(区域 {act['region']} 内)" if act.get("region") else ""
                    raise TimeoutError(f"OCR 未找到文字[{act['ocr']}]{hint}，无法点击")
                cx = box["x"] + box["w"] // 2
                cy = box["y"] + box["h"] // 2
                _tap(cx, cy, f"OCR[{act['ocr']}]")
                if not via_win:
                    self._emit_log("info",
                        f"OCR点击[{act['ocr']}] 文字框({box['x']},{box['y']} {box['w']}x{box['h']}) → 中心({cx},{cy})")
                return
            if "ocr_row" in act:
                # 2026-09-28: 按行定位点击 —— 用于任务栏「未完成条目」这类
                # 需要"含计数、不含完成勾"才能点中的行(见 _ocr_row)
                screen = self._grab()
                box = self._ocr_row(screen, act["ocr_row"])
                if box is None:
                    raise TimeoutError(
                        f"OCR 未找到符合条件的行[{act['ocr_row']}]，无法点击")
                cx = box["x"] + box["w"] // 2
                cy = box["y"] + box["h"] // 2
                _tap(cx, cy, f"行[{box['text']}]")
                if not via_win:
                    self._emit_log("info",
                        f"OCR行点击[{box['text']}] 文字框({box['x']},{box['y']} "
                        f"{box['w']}x{box['h']}) → 中心({cx},{cy})")
                return
            if "button" in act:
                if self.ui is None:
                    raise ValueError("引擎未挂载 UI 注册表，click:{button:...} 不可用")
                ref = self.ui.resolve_click(act["button"], act.get("mode", "at"))
                if "template" in ref:
                    # 模板实时定位后点击中心
                    screen = self._grab()
                    hit = self.matcher.match_one(screen, ref["template"])
                    if hit is None:
                        raise TimeoutError(
                            f"按钮[{act['button']}]模板未命中，无法定位点击")
                    _tap(hit.x, hit.y, f"按钮[{act['button']}](模板定位)")
                    return
                at = ref["at"]
            else:
                at = act.get("at")
            if not at:
                raise ValueError(f"步骤[{step.name}] click 缺少 at/button")
            label = act.get("button") or str(at)
            _tap(*at, label=f"按钮[{label}]" if "button" in act else str(at))
        elif t == "goto_ui":
            # 自动导航: 按 opens 字段推导从主界面到目标界面的点击路径
            if self.ui is None:
                raise ValueError("引擎未挂载 UI 注册表，goto 不可用")
            target = act.get("screen", "")
            plan = self.ui.navigation_plan(target)
            self._emit_log("info", f"导航 → [{target}] 路径: "
                           + (" -> ".join(f"[{b}]" for b in plan) or "(已在主界面)"))
            for btn in plan:
                ref = self.ui.resolve_click(btn, "locate" if self.ui.get(btn).get("template") else "at")
                if "template" in ref:
                    screen = self._grab()
                    hit2 = self.matcher.match_one(screen, ref["template"])
                    if hit2 is None:
                        raise TimeoutError(f"导航按钮[{btn}]模板未命中，无法定位")
                    self.device.tap(hit2.x, hit2.y)
                else:
                    self.device.tap(*ref["at"])
                self._emit_log("info", f"导航点击 [{btn}]")
                # 2026-09-16: 界面切换等待改为可中断(原 time.sleep(1.5) 停止要等满)
                if not self._sleep(1.5):
                    raise InterruptedError("已停止")
            # 最终确认到达目标界面
            self._wait_until(self.ui.screen_condition(target), step.timeout,
                             f"导航验证[{target}]")
            self._emit_log("info", f"✔ 已到达 [{target}]")
        elif t == "click_center":
            if hit is None:
                raise ValueError(f"步骤[{step.name}] click_center 无可用命中点")
            if hasattr(hit, "x"):
                self.device.tap(hit.x, hit.y)
                self._emit_log("info", f"点击识别中心 ({hit.x},{hit.y})")
            else:
                # OCR 框: 用框中心
                cx = hit["x"] + hit["w"] // 2
                cy = hit["y"] + hit["h"] // 2
                self.device.tap(cx, cy)
                self._emit_log("info", f"点击文字中心 ({cx},{cy})")
        elif t == "jump":
            # 无条件跳转到指定步骤名(分支汇合用)
            if task is None:
                raise ValueError("jump 动作需要 task 上下文")
            g = task.index_of(str(act.get("target", "")))
            self._emit_log("info", f"跳转 → 步骤[{act.get('target')}] #{g+1}")
            raise _Goto(g)
        elif t == "swipe":
            frm, to = act.get("from"), act.get("to")
            dur = act.get("duration", 300)
            self.device.swipe(*frm, *to, dur)
            self._emit_log("info", f"滑动 {frm}→{to}")
        elif t == "key":
            self.device.key(act.get("code", 4))
        elif t == "sleep":
            # 2026-09-16 修复: 脚本里有 sleep 60s/150s 的停顿, 原 time.sleep 睡到底
            # → 用户点停止最坏要等满 150 秒。改可中断睡眠。
            secs = act.get("seconds", 1)
            cap = self.max_sleep_seconds()
            if cap and secs > cap:
                self._emit_log(
                    "info",
                    f"停顿 {secs:g}s → 压缩为 {cap:g}s (config: engine.max_sleep_seconds)")
                secs = cap
            if not self._sleep(secs):
                raise InterruptedError("已停止")
        elif t == "start_app":
            self.device.start_app(act.get("package"))
        else:
            raise ValueError(f"未知动作类型: {t}")

    # ---------- 步骤执行 ----------
    def run_step(self, idx: int, task: Task) -> bool:
        step = task.steps[idx]
        for cb in self._on_step_start:
            cb(idx, step)
        self._emit_log("info", f"▶ 步骤 {idx+1}/{len(task.steps)}: {step.describe()}")

        tries = max(1, int(step.retry) + 1)
        for attempt in range(1, tries + 1):
            if not self.running:
                return False
            try:
                hit = None
                if step.wait:
                    hit = self._wait_until(step.wait, step.timeout,
                                           f"步骤[{step.name}] 等待条件")
                if step.action:
                    self._do_action(step, hit, task)
                if step.verify:
                    self._wait_until(step.verify, step.timeout,
                                     f"步骤[{step.name}] 验证条件")
                for cb in self._on_step_end:
                    cb(idx, step, True)
                self._emit_log("info", f"✔ 步骤完成: {step.name or idx}")
                # 2026-09-10 新增: wait 命中(成功)时的条件跳转, 用于监控循环提前退出
                if isinstance(step.on_success, dict) and "goto" in step.on_success:
                    g = task.index_of(step.on_success["goto"])
                    self._emit_log("info",
                                   f"条件成立跳转 → 步骤[{step.on_success['goto']}] #{g+1}")
                    raise _Goto(g)
                return True
            except TimeoutError as e:
                self._emit_log("warning", f"第{attempt}次尝试失败: {e}")
                if attempt < tries:
                    # 2026-09-16: 重试间隔也可中断(原先 time.sleep(1) 期间按停止要等满)
                    if not self._sleep(1):
                        self._emit_log("warning", "⏹ 任务已停止(重试间隔中被中断)")
                        return False
                else:
                    self._dump_debug(step, f"step{idx}_{step.name}")
                    for cb in self._on_step_end:
                        cb(idx, step, False)
                    return self._handle_fail(step, idx, task)
            except (ValueError, KeyError, TypeError, NotImplementedError) as e:
                # 2026-10-09 事故修复: 脚本/注册表的**写法错误**原先直接冒泡出
                # run_task(只捕获 TimeoutError/InterruptedError) → 整条任务被
                # "任务未完成: 异常" 终止, 该步骤的 on_fail 兜底完全失效。
                # 实录: 每日必领段⑤ 引用了 ui_registry 里还没登记的按钮/界面
                # (盈福大厅 无模板却写 mode:locate → ValueError), ①~④ 已领完
                # 的奖励也跟着被整链判失败, 后面的「最终确认」根本没跑到。
                # 这类错误是"这一步做不了"(且是确定性的, 重试也没用), 不是
                # "整个任务完蛋" —— 按失败处理: 留 error 日志 + 现场截图,
                # 交给 on_fail 决定(continue / goto 收尾), 其余段照常跑。
                self._emit_log(
                    "error",
                    f"步骤[{step.name}] 写法/注册表错误, 已按失败处理(请检查 "
                    f"ui_registry 里该按钮是否有 template、界面是否已登记): {e}")
                self._dump_debug(step, f"step{idx}_{step.name}")
                for cb in self._on_step_end:
                    cb(idx, step, False)
                return self._handle_fail(step, idx, task)
            except InterruptedError:
                # 2026-09-16: 打印停止响应耗时, 便于用户/排查确认停止到底卡在哪一步
                cost = (time.time() - self._stop_at) if self._stop_at else 0.0
                self._emit_log(
                    "warning",
                    f"⏹ 任务已被手动停止 (发出停止请求 {cost:.1f}s 后生效)")
                return False
        return False

    def _handle_fail(self, step: Step, idx: int, task: Task) -> bool:
        """on_fail 策略，返回 True=可继续执行后续，False=终止任务"""
        policy = step.on_fail
        if policy == "retry":
            return False                      # 已重试完，终止
        if policy == "continue":
            self._emit_log("warning", "跳过失败步骤继续")
            return True
        if policy == "stop":
            self._emit_log("error", "任务终止")
            return False
        if isinstance(policy, dict) and "goto" in policy:
            g = task.index_of(policy["goto"])
            self._emit_log("warning", f"跳转到步骤 {g+1}")
            # 通过异常跳出当前循环，由 run() 处理跳转
            raise _Goto(g)
        return False

    def _dump_debug(self, step: Step, tag: str):
        if self.debug_dump_dir and self._last_screen is not None:
            import os
            import re
            os.makedirs(self.debug_dump_dir, exist_ok=True)
            # 2026-09-10 修复: 步骤名含 "/" ":" "(" 等非法字符时 cv2.imwrite 静默失败,
            # 失败现场截图全部丢失(循环步骤名"xxx(1/20)"必踩)。统一替换为下划线
            safe = re.sub(r'[\\/:*?"<>|\s]+', "_", tag).strip("_") or "step"
            if len(safe) > 80:
                safe = safe[:80]
            p = os.path.join(self.debug_dump_dir, f"{safe}_{int(time.time())}.png")
            # 2026-09-11 修复: cv2.imwrite 对含中文的路径在 Windows 上偶发静默失败,
            # 改用 imencode + 二进制写文件, 彻底兼容中文步骤名
            try:
                ok, buf = cv2.imencode(".png", self._last_screen)
                if ok:
                    with open(p, "wb") as f:
                        f.write(buf.tobytes())
                else:
                    ok = False
            except Exception:
                ok = False
            if ok:
                self._emit_log("info", f"失败现场截图: {p}")
            else:
                self._emit_log("warning", f"失败现场截图保存失败(路径非法?): {p}")

    # ---------- 任务整体 ----------
    def run_task(self, task: Task, start_index: int = 0) -> bool:
        """从 start_index 顺序执行全部步骤；返回是否全部成功"""
        self.running = True
        self._stop_at = None
        idx = start_index
        try:
            while idx < len(task.steps):
                try:
                    ok = self.run_step(idx, task)
                    # 2026-09-16: 用户主动停止不算"失败", 不打误导性的"重试耗尽"终止日志
                    if not self.running:
                        self._emit_log("warning", "⏹ 任务已停止")
                        return False
                    if not ok and not (isinstance(task.steps[idx].on_fail, dict)):
                        # 2026-09-14 补救: 硬失败原先静默 return, run.log 戛然而止
                        # 无从分析。终止时必须留下终止步骤与原因。
                        self._emit_log(
                            "error",
                            f"✘ 任务终止: 步骤[{task.steps[idx].name}] "
                            f"重试耗尽且未配置 on_fail 兜底")
                        return False
                except _Goto as g:
                    idx = g.target
                    if idx < 0 or idx >= len(task.steps):
                        self._emit_log("error", f"goto 目标非法: {idx+1}")
                        return False
                    continue
                if not self.running:
                    return False
                idx += 1
            self._emit_log("info", f"✔ 任务完成: {task.name}")
            return True
        finally:
            self.running = False

    def stop(self):
        """请求停止(协作式): 置标志即可, 各等待/睡眠点会在 0.1s 内察觉到

        2026-09-16: 增加停止请求日志与时间戳。用户点"停止"后此前无任何
        输出, 日志上看不出停止请求何时发出, 无法判断是"没响应"还是"卡在某步"。
        """
        if self.running and not self._stop_at:
            self._stop_at = time.time()
            self._emit_log(
                "warning",
                "⏹ 收到停止请求, 正在安全停止(正在进行的截图/点击会先结束)…")
        self.running = False


class _Goto(Exception):
    """内部跳转信号"""
    def __init__(self, target: int):
        super().__init__(f"goto {target}")
        self.target = target
