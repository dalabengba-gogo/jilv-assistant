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

from common import png_bytes_to_bgr, rotate_files
from engine.model import RunResult, Step, Task, subtask_of
from engine.ui_registry import UiRegistry, DEFAULT_PATH as UI_REGISTRY_PATH


def _crumb(text: str):
    """崩溃面包屑(2026-10-19 v5.2.0) —— 把"此刻在做什么"写进 logs/last_action.log

    为什么在引擎里也要插: 11:48 那次 access violation 崩在**原生线程**里,
    faulthandler 只有 "<no Python frame>", 完全不知道当时是截图、OCR 还是点击
    (三者分别压在 adb / onnxruntime / Qt 上, 定位方向完全不同)。
    单次调用实测 < 0.1ms, 而一次 OCR 是 5.2 秒, 这点开销可以忽略。
    crash_guard 未安装(如 run_one 脚本)时静默跳过。
    """
    try:
        import crash_guard
        crash_guard.note(text)
    except Exception:
        pass


def _now() -> float:
    """单调时钟 —— 所有"还剩多久/过了多少秒"一律用它(2026-10-19 v4.8.0)

    以前全用 ``time.time()``(墙钟), 只要钟被拨动, 所有 deadline 一起失真:
      · 凌晨 NTP 校时回拨 → ``time.time() + budget`` 算出的截止时间被推后,
        单步预算形同虚设(本该 30 分钟超时的步骤能跑更久);
      · 系统休眠/待机唤醒 → 睡眠期间墙钟照走, 醒来发现 deadline 已过,
        等待类步骤一上来就直接判超时, 而游戏其实只是被冻结了。
    ``time.monotonic()`` 不受校时与休眠影响, 只保证单调递增。

    ⚠️ **只用于计时**: 日志行时间、截图文件名里的 ``int(time.time())`` 这类
    给人看的时间戳仍要用墙钟(否则导出诊断包时无法对齐真实时刻)。
    """
    return time.monotonic()

# 事件回调类型
StepCallback = Callable[[int, Step], None]
LogCallback = Callable[[str, str], None]   # (level, message)
ScreenCallback = Callable[[object], None]

# 2026-10-16 v3.0.4: goto 死循环保护上限。
# 某步骤 on_fail: {goto: X} 反复跳回同一目标、且目标依赖的条件始终不成立时,
# 引擎会无限循环(实录: 域外日常 875↔878 卡死, 进程被强杀才停)。超过该次数即
# 判定为疑似死循环并终止任务, 而不是永远空转。可用 config.yaml → engine.goto_loop_limit 调整。
GOTO_LOOP_LIMIT = 30


def _as_float(val, default: float) -> float:
    """读配置项成 float —— **0 是合法值**(2026-10-19 v4.8.0)

    项目里多处写成 ``float(cfg.get(k, d) or d)``, 一旦有人把某上限配成 0
    (语义通常是"关掉这条限制"), 0 会被 ``or`` 当成假值 → 悄悄退回默认值,
    配置看起来改了却完全没生效。这里显式区分"没配"与"配了 0"。
    """
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _as_int(val, default: int) -> int:
    """同上, int 版"""
    if val is None:
        return default
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


class Engine:
    def __init__(self, device, matcher, ocr=None, poll_interval=0.3,
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
        # 2026-10-17 v3.5.0: debug 截图滚动清理。一晚 157 张/134MB 且永久常驻。
        # 每落 ROTATE_EVERY 张才扫一次目录(避免每步 listdir), 任务开始时也清一次。
        self._debug_since_rotate = 0
        self._ROTATE_EVERY = 20
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
        # 2026-10-19 v5.5.0(性能): 按 region 分桶的 OCR 缓存 + 当前帧 + 帧复用窗口。
        # 见 _grab()/_ocr_boxes() 的说明。frame_ttl=0 可一键退回"每次都重新截图"。
        self._ocr_cache: dict = {}
        self._frame = None
        self._frame_ts = 0.0
        self.frame_ttl = 2.0
        self._last_sig = None          # 上一张画面的缩略指纹(同屏判定)
        self._same_streak = 0          # 连续"同屏"次数(见 _SAME_STREAK_MAX)
        self._reuse_on_same = True     # 同屏复用 OCR; 验收点会临时关掉
        # 2026-10-19 v5.5.0(性能·热点窗口): 关键词上次出现的位置。
        # 下次找同一个关键词时先只识别那一小块(约 0.4~0.6s, 全图 1.97s);
        # 小块里没有就照常退回全图 —— 只会更快, 不会漏判。
        self._ocr_hint: dict = {}
        self._ocr_hint_miss: dict = {}
        self.ocr_hint_enabled = True
        # 2026-10-19 v5.5.0: 运行级性能计数(任务结束时打一行汇总, 用来验证优化是否
        # 真的生效 —— 不看数字的优化等于没做)。
        self.stats = {"shots": 0, "frame_reuse": 0,
                      "ocr_full": 0, "ocr_band": 0, "ocr_cached": 0,
                      "ocr_hint_hit": 0, "ocr_same_screen": 0,
                      "shot_seconds": 0.0, "ocr_seconds": 0.0,
                      "t0": None}
        # 2026-10-16 v3.0.0: 失败步骤明细。
        # 原先 on_fail: continue 返回 True 被当成"成功", 全链路没有任何一处
        # 统计失败步数 —— 帮派捐献没做成却整条报"成功"就是这么来的。
        # 现在每步失败都记账, 任务结束时汇总告警, required=True 的还会升级为
        # 整条失败。每项: {"index","name","reason","policy","required"}
        self.failed_steps: list[dict] = []
        # 2026-10-17 v3.5.0: 与 failed 分开记的「条件未命中」(optional 探测没命中)。
        # 昨夜 157 步"跳过"里绝大多数是这类(今天没有的奖励/已领过的), 混在失败里
        # 报会让用户误以为任务没做成。真失败才进 failed_steps。
        self.skipped_steps: list[dict] = []
        self._ocr_warned = False          # OCR 不可用的总告警只打一次(防刷屏)
        # 2026-10-17 v3.5.0: 结构化运行结果的最后一次快照(GUI/命令行直接读,
        # 不再从文案里切数字)。run_task 返回 bool 保持兼容。
        self.last_result: RunResult | None = None
        # 2026-10-16 v3.0.4: goto 死循环保护计数
        self._goto_counts: dict[int, int] = {}   # target index -> 被跳转次数
        self._visits: dict[int, int] = {}        # 2026-10-19 v4.8.0: 每步执行次数(批次上限)
        self._step_threshold: float | None = None   # v4.9.0: 本步模板阈值(见 run_step)
        self._exec_count = 0                       # 全局步骤执行计数(兜底)
        self._last_step = ""                       # 最后执行的步骤名(v3.9.0 完成通报用)

        # 2026-10-18 v4.5.0 (P0-3): 心跳回调。引擎主线程周期性发心跳,
        # 供 GUI 区分"正常长跑"与"僵尸进程(假运行中)"。默认为 None(本机任务
        # 不走 live 通道, 不心跳); 多开子进程 run_one 会接上 live.heartbeat。
        self._hb_cb = None
        self._last_hb = 0.0

        # 事件订阅
        self._on_step_start: list[StepCallback] = []
        self._on_log: list[LogCallback] = []
        self._on_screenshot: list[ScreenCallback] = []
        self._on_step_end: list = []

    # ---------- 订阅 ----------
    def set_heartbeat(self, cb):
        """接上心跳回调(多开子进程用来写 live 心跳事件)。"""
        self._hb_cb = cb

    def _heartbeat_interval(self) -> float:
        """心跳发射间隔(秒) —— config.yaml → engine.heartbeat_interval, 默认 5s。"""
        if self._cfg_cache is None:
            try:
                from common import load_config
                self._cfg_cache = load_config()
            except Exception:
                self._cfg_cache = {}
        eng = self._cfg_cache.get("engine", {}) if isinstance(self._cfg_cache, dict) else {}
        try:
            return float(eng.get("heartbeat_interval", 5.0) or 5.0)
        except (TypeError, ValueError):
            return 5.0

    def _beat(self):
        """发一次心跳(受间隔限流)。必须只在主线程调用 —— 见 live.py heartbeat 说明。"""
        if self._hb_cb is None:
            return
        now = _now()                       # v4.8.0: 间隔限流用单调时钟(墙钟回拨会让心跳停发)
        if now - self._last_hb < self._heartbeat_interval():
            return
        self._last_hb = now
        try:
            self._hb_cb()
        except Exception:
            pass

    def subscribe(self, *, step_start=None, log=None, screenshot=None, step_end=None):
        if step_start:
            self._on_step_start.append(step_start)
        if log:
            self._on_log.append(log)
        if screenshot:
            self._on_screenshot.append(screenshot)
        if step_end:
            self._on_step_end.append(step_end)

    def _emit_log(self, level: str, msg: str, gui: bool = True):
        """打一行日志

        2026-10-17 v3.7.0: gui=False = **只写 run.log, 不推给界面**。
        用于"条件未命中"这类每步都来一行的正常现象 —— 926 步的日常能刷出上百行,
        把真正要看的失败信息淹了(用户反馈)。排障要查时 run.log 里一条不少。
        """
        if self.logger:
            getattr(self.logger, level)(msg)
        if not gui:
            return
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
    def _grab(self, ttl: float | None = None):
        """截屏并解码

        2026-10-19 v5.5.0(性能·帧复用): ``ttl`` 秒内允许**复用上一帧**。
        实测 adb 截图 860ms + 全图 OCR 1970ms, 一轮日常 417 次截图 / 341 次 OCR,
        而其中相当一部分是"同一瞬间看第二眼":
          · ``wait`` 刚命中 → ``click: {ocr: X}`` 立刻又要一份画面来定位;
          · 纯检查步连在一起(上一步没做任何动作, 画面根本没变)。
        这些重复帧白花 2.8 秒/次。
        默认 ttl=``self.frame_ttl``(2.0s)。**轮询(_wait_until/稳检测复核)一律传 ttl=0**
        —— 等待要的是新画面, 复用旧帧等于把轮询变瞎; 而动作(tap/swipe/key)会显式
        作废缓存, 所以"点完必须重新看"的语义不受影响。ttl=0 = 强制重新截图。
        """
        now = _now()
        ttl = self.frame_ttl if ttl is None else ttl
        if ttl > 0 and self._frame is not None and (now - self._frame_ts) <= ttl:
            self._beat()
            _crumb("截图(复用上一帧)")
            self.stats["frame_reuse"] += 1
            return self._frame
        self._beat()                 # 每轮识别都发心跳(证明主线程在推进, P0-3)
        _crumb("截图")
        t_shot = _now()
        # 2026-10-19 v5.5.0: 优先走 RAW 抓屏(359ms), 不支持时退回 PNG(684ms)
        if hasattr(self.device, "screenshot_bgr"):
            screen = self.device.screenshot_bgr()
        else:
            screen = png_bytes_to_bgr(self.device.screenshot())
        self.stats["shots"] += 1
        self.stats["shot_seconds"] += _now() - t_shot
        self._frame = screen
        self._frame_ts = _now()
        # 2026-10-19 v5.5.0(性能·同屏复用): 画面和上一张**几乎一模一样**时,
        # 保留上一帧的 OCR 结果。等待类步骤(等主界面/等面板/等徽标)每一轮都要
        # 重新 OCR 1.5 秒, 而画面根本没变 —— 同一份像素重新算一遍不会得出别的结论。
        # 判据: 缩到 108x192 灰度图逐像素比, 变化像素占比 < 0.15% 视为同屏。
        # 验收点(verify_done/verify_retry)不走这条, 见 run_step —— 那是"以面板为准"
        # 的权威结论, 宁可多花 1.5 秒也不省。
        same = False
        if (self._reuse_on_same and self._last_sig is not None
                and self._same_streak < self._SAME_STREAK_MAX):
            same = self._same_picture(screen)
        if same:
            self.stats["ocr_same_screen"] += 1
            self._same_streak += 1
        else:
            self._same_streak = 0
            # 画面变了 → 作废上一张的 OCR 缓存
            self._ocr_cache.clear()
            self._ocr_cache_screen = None
            self._ocr_cache_boxes = None
        self._last_sig = self._signature(screen)
        self._emit_screenshot(screen)
        return screen

    # 同屏判定: 缩到 216x384 灰度图(1/2.5), 逐格差 > 8 记为一个变化格。
    # 阈值 0.04% 是拿 119 对真实现场截图标定的:
    #   · 相邻两帧的中位数是 9.11%(完全不同的画面), 真正"没动"的在 0.002~0.02%;
    #   · 而一个 20x18 的**数字变化**就占 0.077% —— 阈值必须压在它下面,
    #     否则"进度 0/10 → 1/10"会被当成没变, OCR 结果一直沿用旧的。
    _SIG_W, _SIG_H = 216, 384
    _SIG_DIFF = 8
    _SIG_RATIO = 0.0004
    # 连续复用上限: 就算阈值判错了, 最多连省 2 次就必须真识别一次,
    # 把"看漏一个小变化"的滞后限制在 2 轮轮询(约 4~6 秒)之内。
    _SAME_STREAK_MAX = 2

    def _signature(self, screen):
        try:
            import cv2
            g = cv2.cvtColor(screen, cv2.COLOR_BGR2GRAY)
            return cv2.resize(g, (self._SIG_W, self._SIG_H),
                              interpolation=cv2.INTER_AREA)
        except Exception:
            return None

    def _same_picture(self, screen):
        sig = self._signature(screen)
        if sig is None or self._last_sig is None:
            return False
        try:
            import numpy as np
            d = np.abs(sig.astype(np.int16) - self._last_sig.astype(np.int16))
            changed = int((d > self._SIG_DIFF).sum())
            return changed / d.size < self._SIG_RATIO
        except Exception:
            return False

    def _invalidate_frame(self):
        """动作(tap/swipe/key/启停 App)之后画面一定变了 → 立刻作废缓存的帧

        2026-10-19 v5.5.0: 帧复用能省 2.8 秒/次, 但**必须在动作后失效** ——
        否则"点完立刻复核"会看到点击前的旧画面, 这正是各种假阳性的温床。
        """
        self._frame = None
        self._frame_ts = 0.0
        self._last_sig = None
        self._same_streak = 0
        self._ocr_cache.clear()

    def _ocr_boxes(self, screen, region=None):
        """对截图跑 OCR; region=[x1,y1,x2,y2] 时**只识别该纵向带**(v5.5.0)

        2026-10-19 v5.5.0(性能·裁剪识别): 以前 region 只是"对全图 OCR 结果做过滤",
        每帧都付全图成本(实测 1.85~1.97s / 43 个文本框)。RapidOCR 的耗时几乎与
        **文本框数量**成正比, 裁掉无关区域能直接省掉大部分 ——
        实测「任务栏 540x222 纵向带」1081ms、「小块热点窗口 178x97」619ms,
        全图 1849ms。

        ⚠️ **只按纵向裁, 横向保持整行**。第一版按矩形裁, 24 张真实现场图里有 10 张
        把长标签从中间截断(「今日多倍次数：10/10」→「倍次数：10/10」,
        「域外风情2/10」→「常域外风情2/10」) —— 判据(any_of/has)会因此漏判,
        属于"省了时间但把结论搞错了", 不能接受。文字行是横排的, 纵向裁带既能
        砍掉大部分框, 又不会切断任何一行。

        ⚠️ 缩放**没有用**: 0.75/0.6/0.5 倍实测 2.0~2.2s, 反而更慢 —— 引擎内部
        会先把输入拉到自己的尺寸。所以路线只走裁剪, 不走降采样。

        同一张画面只识别一次; 不同 region 各自缓存(同一帧可能既要全图又要局部)。
        """
        if self.ocr is None:
            raise NotImplementedError("OCR 引擎尚未接入，OCR 条件不可用")
        is_current = screen is self._frame
        key = None if not region else tuple(int(v) for v in region)
        if is_current and key in self._ocr_cache:
            self.stats["ocr_cached"] += 1
            return self._ocr_cache[key]
        t_ocr = _now()

        if region:
            h, w = screen.shape[:2]
            x1, y1, x2, y2 = key
            # 纵向留 26px 余量: 文字框高实测 22~48, "中心在区域内"的框最多外伸 24px,
            # 留 26 保证它完整落在带内(否则贴着边界的行会被切掉一半读不出来)。
            cy1, cy2 = max(0, y1 - 26), min(h, y2 + 26)
            if cy2 - cy1 >= h - 60:      # 带子几乎等于全屏 → 别白折腾, 直接全图
                cy1, cy2 = 0, h
            _crumb(f"OCR(纵向带 y{cy1}~{cy2} 识别)")
            boxes = self.ocr.recognize(screen[cy1:cy2, :])
            for b in boxes:                  # 裁图坐标 → 全屏坐标
                b["y"] += cy1
            _crumb("OCR 完成")
        else:
            _crumb("OCR(全图识别, 约 2s)")
            boxes = self.ocr.recognize(screen)
            _crumb("OCR 完成")

        self.stats["ocr_band" if region else "ocr_full"] += 1
        self.stats["ocr_seconds"] += _now() - t_ocr
        if is_current:
            self._ocr_cache[key] = boxes
        self._ocr_cache_screen = screen
        self._ocr_cache_boxes = self._ocr_cache.get(None)
        return boxes

    def _recognize_wait(self, cond: dict, screen):
        """判断截图是否满足 wait/verify 条件，命中返回结果对象，未命中返回 None

        cond: {"template": name} | {"ocr": "关键词"} | {"any":[ ... ]}
             | {"visible": 按钮名} | {"ui": 界面名} | {"not": {...}}
             | {"ocr_pair": [徽标文本, 目标文本]}  —— 两段文字纵向相邻(同一卡片行)才算命中,
               用于玩法面板"已完成"徽标与任务名的配对判定(2026-09-11 新增)
               可选几何门槛(2026-10-19 v5.1.0):
                 pair_min_score 目标框置信度下限    pair_min_x  目标框左边 x 下限
                 pair_min_w     目标框最小宽度      pair_dy     两框中心 y 差上限(默认45)
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
            #
            # 2026-10-19 v5.1.0 ⚠ 上面这套"靠分数卡"实测是错的, 必须配几何门槛:
            #   43 张失败现场截图实测 —— 真卡名置信度中位数只有 0.71~0.80
            #   (五环任务 0.755 / 地煞星 0.744 / 域外风情 0.795), 均值就在 0.75
            #   门槛线上 → 同一张卡不同帧忽过忽不过, 五环曾连判 4 帧全灭。
            #   而 0.652 那次误读的框是 **x=211 w=112**, 真卡名恒为 **x≈298~317,
            #   w≈129~169**(大号美术字, 右半区) —— 用位置/宽度分, 比分数干净得多。
            #   所以脚本统一改成 pair_min_score 0.62 + pair_min_x 250(卡名在右半区),
            #   分数只用来挡掉离谱的低分, 由几何条件兜底。
            anchor_kw, target_kw = cond["ocr_pair"]
            min_score = float(cond.get("pair_min_score") or 0)
            min_x = float(cond.get("pair_min_x") or 0)
            min_w = float(cond.get("pair_min_w") or 0)
            dy_max = float(cond.get("pair_dy") or 45)
            boxes = self._ocr_boxes(screen)
            targets = [b for b in boxes if target_kw in b["text"]
                       and (not min_score or b.get("score", 0.0) >= min_score)
                       and (not min_x or b["x"] >= min_x)
                       and (not min_w or b["w"] >= min_w)]
            if not targets:
                return None
            for a in boxes:
                if anchor_kw not in a["text"]:
                    continue
                ay = a["y"] + a["h"] / 2
                for t in targets:
                    ty = t["y"] + t["h"] / 2
                    if abs(ay - ty) < dy_max:
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
            # 2026-10-19 v4.9.0(P2-5): 带上本步的阈值覆盖(若有)
            return self.matcher.match_one(screen, cond["template"],
                                          self._step_threshold)
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
        避免同屏其他位置的相同文字干扰)。

        2026-10-19 v5.5.0(性能):
          · 给了 region → **只识别该区域**(不再全图识别后过滤), 见 _ocr_boxes;
          · 没给 region → 先试"热点窗口"(这个关键词上次在哪儿出现过), 小图识别
            约 0.4~0.6s; 没找到才退回全图 1.97s。**漏判风险为零** —— 全图那步
            照旧执行, 只是把"常见关键词的常态"变便宜了。
        """
        if region:
            x1, y1, x2, y2 = (int(v) for v in region)
            boxes = self._ocr_boxes(screen, region)
            boxes = [b for b in boxes
                     if x1 <= b["x"] + b["w"] // 2 <= x2
                     and y1 <= b["y"] + b["h"] // 2 <= y2]
            for b in boxes:
                if keyword in b["text"]:
                    return b
            return None

        # 热点窗口: 上次在哪儿见过, 就先只看那一小块
        if self.ocr_hint_enabled:
            hints = self._ocr_hint.get(keyword)
            if hints:
                found = None
                for reg in list(hints):
                    for b in self._ocr_boxes(screen, reg):
                        if keyword in b["text"]:
                            found = b
                            break
                    if found is not None:
                        break
                if found is not None:
                    self._ocr_hint_miss.pop(keyword, None)
                    self.stats["ocr_hint_hit"] += 1
                    return found
                # 热点不中 → 这次白花了 0.6s。连续 2 次不中就**忘掉**这个关键词的
                # 热点: 说明它开始频繁"不在"(典型是战斗里的「回合」, 出了战斗每次
                # 检查都会试点失败) —— 忘了它就回到"直接全图", 不再倒赔时间。
                miss = self._ocr_hint_miss.get(keyword, 0) + 1
                if miss >= self._HINT_MISS_LIMIT:
                    self._ocr_hint.pop(keyword, None)
                    self._ocr_hint_miss.pop(keyword, None)
                    self._emit_log("debug",
                                   f"热点窗口连续 {miss} 次未命中, 停止使用: [{keyword}]")
                else:
                    self._ocr_hint_miss[keyword] = miss

        boxes = self._ocr_boxes(screen)
        for b in boxes:
            if keyword in b["text"]:
                self._remember_hint(keyword, b)
                return b
        return None

    # 热点窗口的裁剪边距: 卡片/按钮位置会在小范围内浮动(实测 ±20px),
    # 留 70px 横向、45px 纵向余量, 单个窗口约 200x120, 识别耗时 0.6s(全图 1.85s)。
    _HINT_PAD_X = 70
    _HINT_PAD_Y = 45
    _HINT_KEEP = 2
    _HINT_MISS_LIMIT = 2

    def _remember_hint(self, keyword: str, box: dict):
        """记住这个关键词这次出现在哪儿(供下次走热点窗口)"""
        if not self.ocr_hint_enabled:
            return
        h, w = (self._frame.shape[:2] if self._frame is not None else (960, 540))
        reg = (max(0, box["x"] - self._HINT_PAD_X),
               max(0, box["y"] - self._HINT_PAD_Y),
               min(w, box["x"] + box["w"] + self._HINT_PAD_X),
               min(h, box["y"] + box["h"] + self._HINT_PAD_Y))
        lst = [r for r in self._ocr_hint.get(keyword, ()) if r != reg]
        lst.insert(0, reg)
        self._ocr_hint[keyword] = lst[:self._HINT_KEEP]

    def _ocr_row(self, screen, spec):
        """按"行"查找文字框: 需含 has 全部关键词、且不含 not 任一关键词。

        2026-09-28 新增。用于同屏多行、且行内带状态标记的场景 —— 典型是主界面
        右侧任务栏: 未完成的一条龙子任务行为「挖宝封妖0/10」, 同族的已完成行为
        「域外风情10/10V」。只用 {"ocr": "挖宝封妖"} 无法区分状态; 而任务栏左下角
        的世界聊天气泡里出现同名文字时还会误命中(2026-09-15 打开聊天界面事故)。
        因此 spec 支持:
          has:    关键词 或 [关键词...], 文本框必须全部包含
          any_of: 关键词 或 [关键词...], 文本框至少包含其中一个(2026-10-19 新增)
          not:    关键词 或 [关键词...], 文本框一个都不能含(默认空)
          region: [x1,y1,x2,y2], 只接受"框中心"落在该区域内的框(默认全屏)
        多个命中取最靠上的一行(与"先做靠前的任务"一致, 且不受 OCR 返回顺序影响)。
        has 与 any_of 可同时出现(全部满足); 两者都为空是写法错误。
        """
        if not isinstance(spec, dict):
            raise ValueError(
                f"ocr_row 必须是条件字典 {{has:[...], not:[...], region:[...]}}，"
                f"收到裸值: {spec!r}")
        has = spec.get("has") or []
        has = [has] if isinstance(has, str) else [str(k) for k in has]
        neg = spec.get("not") or []
        neg = [neg] if isinstance(neg, str) else [str(k) for k in neg]
        any_of = spec.get("any_of") or []
        any_of = [any_of] if isinstance(any_of, str) else [str(k) for k in any_of]
        if not has and not any_of:
            raise ValueError("ocr_row 缺少 has / any_of 关键词")
        region = spec.get("region")

        hits = []
        # 2026-10-19 v5.5.0: 带 region 时只识别该区域(不再全图识别后过滤)。
        for b in self._ocr_boxes(screen, region):
            text = b.get("text", "")
            if has and not all(k in text for k in has):
                continue
            # 2026-10-19 v5.4.0: any_of —— "含其中任一即可"。
            # 起因是一条龙的续跑判据只写了 has:['/'] not:['V'], 等于"任何一条
            # 带斜杠的任务行"都算一条龙在跑 → 误判工作量/白等 45 分钟。
            # 现在必须命中一条龙专属玩法名(域外风情/挖宝封妖/烧法任务/…)。
            if any_of and not any(k in text for k in any_of):
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
        end = _now() + max(0.0, float(seconds))
        while True:
            if check_stop and not self.running:
                return False
            self._beat()             # 睡眠切片里也发心跳, 长 sleep 不会变"沉默"(P0-3)
            remain = end - _now()
            if remain <= 0:
                return True
            time.sleep(min(0.1, remain))

    def _wait_until(self, cond: dict, timeout: float, desc: str,
                    hard_deadline: float | None = None):
        """轮询直到条件满足；返回命中对象；超时抛 TimeoutError；被停止抛 InterruptedError

        hard_deadline(2026-10-17 v3.5.0): 绝对时间戳(perf 时钟)。给了就以
        ``min(本次 timeout, hard_deadline)`` 为准 —— 重试不再"每次都重跑完整
        timeout", 整步共享一份时间预算。实测事故: daily_yuwai.yaml 有
        ``timeout: 5400`` + ``retry: 1`` → 单步最坏 3 小时, 一夜跑 4 小时。
        """
        deadline = _now() + timeout
        if hard_deadline:
            deadline = min(deadline, hard_deadline)
        while _now() < deadline:
            self._check_stop()
            screen = self._grab(ttl=0)   # 轮询必须看新画面, 不能复用旧帧(v5.5.0)
            self._check_stop()          # 截图耗时约 2.6s, 期间按了停止也别白等下一轮
            hit = self._recognize_wait(cond, screen)
            if hit:
                self._emit_log("debug", f"{desc} 命中: {hit}")
                return hit
            # 轮询间隔改为可中断, 停止请求不再被 1s 的 sleep 拖延
            if not self._sleep(self.poll_interval):
                raise InterruptedError("已停止")
        raise TimeoutError(f"{desc} 超时({timeout}s)")

    def _wait_with_settle(self, step: "Step", hard_deadline: float | None = None):
        """带稳检测的等待(2026-10-16 v3.0.5, 2026-10-17 补总时限)。

        wait 命中后若 step.settle>0, 再等 settle 秒并复核条件是否仍成立, 才判定成功;
        否则视为过渡期假阳性(如战斗→主界面切换时底栏短暂闪现, 主界面判定瞬间成立)
        继续轮询。根治「等回主界面」在战斗间隙被误判、进而与下游复核形成 goto
        死循环的旧事故(2026-10-16 实战复现: 域外批次后 875↔878 空转)。

        返回命中对象; 超时抛 TimeoutError(与 _wait_until 保持一致, 复用既有
        except 分支); 被停止抛 InterruptedError。

        2026-10-17 v3.5.0: 外层「命中→复核→再等」原先是**没有总 deadline 的
        while True** —— 条件一直假阳性时(如战斗间隙底栏反复闪现)会永远转下去,
        step.timeout 形同虚设。现在受 hard_deadline 约束; 未显式给时也有一个
        兜底上限(2×timeout), 保证任何调用路径都不会失控。
        """
        desc = f"步骤[{step.name}] 等待条件"
        settle = float(getattr(step, "settle", 0) or 0)
        if settle <= 0:
            return self._wait_until(step.wait, step.timeout, desc, hard_deadline)
        # 复核循环的总时限: 优先用整步预算, 否则兜底 2×timeout(至少 60s)
        loop_deadline = hard_deadline or (
            _now() + max(float(step.timeout), 60.0) * 2)
        while True:
            self._check_stop()
            if _now() >= loop_deadline:
                raise TimeoutError(f"{desc} 稳检测复核未收敛(超出总时限)")
            hit = self._wait_until(step.wait, step.timeout, desc, loop_deadline)
            # 命中后稳检测: 等 settle 秒, 复核条件是否仍成立
            if not self._sleep(settle):
                raise InterruptedError("已停止")
            re_hit = self._recognize_wait(step.wait, self._grab(ttl=0))
            if re_hit:
                return hit
            self._emit_log(
                "debug",
                f"{desc} 命中后 {settle}s 复核失败, 判定为过渡期假阳性, 继续等待")

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

    def max_step_seconds(self) -> float:
        """单个步骤的总时间预算上限 —— config.yaml → engine.max_step_seconds。

        2026-10-17 v3.5.0 新增: 一步的总耗时不再等于 ``timeout × (retry+1)``
        的无上限乘积(实测 daily_yuwai.yaml 有 timeout:5400 + retry:1 → 单步最坏
        **3 小时**, 一夜跑 4 小时的元凶之一)。设了它, 整步(含重试、含 settle
        复核循环)的等待总时长被压到该上限; 0 或未配置 = 只用 timeout×次数 预算。
        """
        if self._cfg_cache is None:
            try:
                from common import load_config
                self._cfg_cache = load_config()
            except Exception:
                self._cfg_cache = {}
        eng = self._cfg_cache.get("engine", {}) if isinstance(self._cfg_cache, dict) else {}
        try:
            return float(eng.get("max_step_seconds", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def step_budget(self, step: "Step") -> float:
        """这一步的整步时间预算上限(秒) —— 步骤自带的 max_seconds 优先(2026-10-19 v4.8.0)

        为什么要有 per-step 口子: 全局 max_step_seconds 只能定一个"谁也别超过"
        的天花板, 而域外「等打完回主界面」自述最长 90 分钟、一条龙结束要等 60 分钟,
        —— 一刀压到全局值会把这类**天生就该久等**的步骤掐死。标了 max_seconds
        的步骤按自己的值走, 没标的吃全局。0 = 不额外限制。
        """
        own = float(getattr(step, "max_seconds", 0) or 0)
        if own > 0:
            return own
        return self.max_step_seconds()

    def _cfg_int(self, key: str, default: int) -> int:
        eng = self._cfg_cache.get("engine", {}) if isinstance(self._cfg_cache, dict) else {}
        return _as_int(eng.get(key), default)

    def _cfg_float(self, key: str, default: float) -> float:
        eng = self._cfg_cache.get("engine", {}) if isinstance(self._cfg_cache, dict) else {}
        return _as_float(eng.get(key), default)

    def verify_min_seconds(self) -> float:
        """验收点/复核步的最小判定时长(秒) —— engine.verify_min_seconds, 默认 15。

        15 不是拍脑袋: 2026-10-19 账号2 真机实测**一轮判定 6.02 秒**
        (adb 截图 0.80s + 全图 OCR 5.22s/45 个文本块), 配 retry 1 最坏能看 5 帧。
        写 6 秒等于仍然只够一帧, 治不了"刚渲染完就被判没有"。
        """
        self._ensure_cfg()
        return self._cfg_float("verify_min_seconds", 15.0)

    def verify_min_retry(self) -> int:
        """验收点/复核步的最小重试次数 —— engine.verify_min_retry, 默认 1。"""
        self._ensure_cfg()
        return self._cfg_int("verify_min_retry", 1)

    def _ensure_cfg(self):
        """惰性加载 config(供 _cfg_int/_cfg_float 共用)"""
        if self._cfg_cache is None:
            try:
                from common import load_config
                self._cfg_cache = load_config()
            except Exception:
                self._cfg_cache = {}

    def _apply_judge_floor(self, step: "Step", idx: int):
        """给验收点抬"判定下限" —— 别只看一帧就下结论(2026-10-19 v4.8.0, P1-3)

        事故背景: 脚本里验收点写的是 ``timeout: 3~5 + retry: 0``, 而**实测一轮判定
        就要 6 秒**(adb 截图 0.8s + 全图 OCR 5.2s), 结果这类"玩法到底做完没有"的
        判定**只看了 1 帧**就放弃 —— 面板列表还在滑动/刚渲染完、徽标还没画出来
        都会读成"没有", 直接把做成的玩法判成未完成。

        而这一判定是用户明确指定要**以游戏面板为准**的权威口径(v3.9.0), 宁可多等
        几秒也不能草率。所以对 verify_done / verify_retry 步骤做下限保护:
          · timeout < verify_min_seconds → 抬到该值(默认 15s ≈ 2.5 轮完整识别);
          · retry  < verify_min_retry    → 抬到该值(允许二次确认, 抵消偶发假阴性)。
        验收点全项目只有十来处, 多出的这点等待不影响整体时长。0 = 关掉该下限。
        """
        if not (getattr(step, "verify_done", False) or getattr(step, "verify_retry", False)):
            return
        floor_s = self.verify_min_seconds()
        floor_r = self.verify_min_retry()
        if floor_s <= 0 and floor_r <= 0:
            return
        old_t, old_r = float(step.timeout), int(step.retry)
        new_t, new_r = old_t, old_r
        if floor_s > 0 and old_t < floor_s:
            new_t = floor_s
        if old_r < floor_r:
            new_r = floor_r
        if (new_t, new_r) != (old_t, old_r):
            step.timeout, step.retry = new_t, new_r
            self._emit_log(
                "info",
                f"步骤[{step.name or idx}] 是验收点, 判定时间 {old_t:g}s/"
                f"retry {old_r} 只够看一两帧 → 抬到 {new_t:g}s/retry {new_r}")

    def _goto_loop_limit(self) -> int:
        """goto 死循环保护的重复跳转上限(config.yaml → engine.goto_loop_limit)。

        2026-10-16 v3.0.4 新增: 某步骤反复 goto 跳回同一目标超过该次数即判定疑似死循环。
        默认 GOTO_LOOP_LIMIT(30); 若 legit 脚本需要更长等待, 在 config 里调大。
        """
        if self._cfg_cache is None:
            try:
                from common import load_config
                self._cfg_cache = load_config()
            except Exception:
                self._cfg_cache = {}
        eng = self._cfg_cache.get("engine", {}) if isinstance(self._cfg_cache, dict) else {}
        try:
            return int(eng.get("goto_loop_limit", GOTO_LOOP_LIMIT) or GOTO_LOOP_LIMIT)
        except (TypeError, ValueError):
            return GOTO_LOOP_LIMIT

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
                # 2026-10-19 v5.5.0: 点击后画面就变了, 立刻作废帧缓存
                self._invalidate_frame()

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
                    hit = self.matcher.match_one(screen, ref["template"],
                                                 self._step_threshold)
                    if hit is None:
                        # 2026-10-19 v5.4.0(P1-4): 只有 mode: locate_or_at 才做兜底。
                        # 其它模式(locate)保持原样直接判失败 —— 否则会把
                        # "商城/签到这类按钮今日已领、本来就不在"的步骤文案搅乱。
                        #
                        # locate_or_at 的两种情况必须分开(真机 16:23/16:36 各踩一次):
                        #   · 按钮在, 但"!"角标盖住一角 → 严格阈值 0.7x 不中,
                        #     **宽松阈值(0.55)能认出** → 用实测坐标点;
                        #   · 按钮根本不在(今日无可领/已领过) → 宽松也不中 →
                        #     不点(盲点会误触卡片), 报超时让 on_fail 正常跳过。
                        if act.get("mode") == "locate_or_at" and ref.get("at"):
                            loose = None
                            try:
                                loose = self.matcher.match_one(
                                    screen, ref["template"], 0.55)
                            except Exception:
                                loose = None
                            if loose is not None:
                                _tap(*ref["at"],
                                     label=f"按钮[{act['button']}](被遮挡→坐标兜底)")
                                self._emit_log(
                                    "warning",
                                    f"按钮[{act['button']}]模板未命中但宽松阈值命中 "
                                    f"({getattr(loose, 'score', 0):.2f}) → 判为遮挡, "
                                    f"退回注册坐标 {tuple(ref['at'])} 点击")
                                return
                            raise TimeoutError(
                                f"按钮[{act['button']}]模板未命中"
                                f"(宽松阈值复核也不中 → 按钮当前不在这, 今日无可领/已消失)")
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
                self._invalidate_frame()
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
            self._invalidate_frame()
        elif t == "jump":
            # 无条件跳转到指定步骤名(分支汇合用)
            if task is None:
                raise ValueError("jump 动作需要 task 上下文")
            g = task.index_of(str(act.get("target", "")))
            self._emit_log("info", f"跳转 → 步骤[{act.get('target')}] #{g+1}")
            raise _Goto(g, "jump")
        elif t == "swipe":
            frm, to = act.get("from"), act.get("to")
            dur = act.get("duration", 300)
            self.device.swipe(*frm, *to, dur)
            self._emit_log("info", f"滑动 {frm}→{to}")
            self._invalidate_frame()
        elif t == "key":
            self.device.key(act.get("code", 4))
            self._invalidate_frame()
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
            self._invalidate_frame()
        else:
            raise ValueError(f"未知动作类型: {t}")

    # ---------- 步骤执行 ----------
    def run_step(self, idx: int, task: Task) -> bool:
        step = task.steps[idx]
        # 2026-10-17 v3.7.0: 标记"这个子任务被跑到过"(用于区分 未执行 vs 全做成)
        try:
            self._sub_seen.add(subtask_of(step, self._sub_mode))
        except Exception:
            pass
        self._last_step = f"第{idx+1}步「{step.name or step.describe()}」"
        _crumb(self._last_step)
        # 2026-10-19 v4.9.0(P2-5): 本步的模板阈值(0/None = 走模板自己的默认值)。
        # 放在识别原语之外传递, 免得给 _recognize_wait / _do_action 逐层加参数 ——
        # 它们都是"当前这一步"的上下文, 用实例字段表达最直接。
        try:
            self._step_threshold = float(getattr(step, "threshold", 0) or 0) or None
        except (TypeError, ValueError):
            self._step_threshold = None
        # 2026-10-19 v5.5.0: "同屏复用 OCR"对**验收点**关闭。
        # 验收点是"以游戏面板为准"的权威结论(verify_done / verify_retry),
        # 它必须看真的新画面 —— 哪怕省 1.5 秒也不冒这个险。
        self._reuse_on_same = not (
            getattr(step, "verify_done", False) or getattr(step, "verify_retry", False))

        # 2026-10-19 v4.8.0: 批次上限(step.max_visits)。计数放在钟点之外,
        # 因为它是"这一步被跑了几轮", 不是耗时。
        mv = int(getattr(step, "max_visits", 0) or 0)
        if mv > 0:
            n = self._visits.get(idx, 0) + 1
            self._visits[idx] = n
            if n > mv:
                self._emit_log(
                    "warning",
                    f"⏹ 步骤[{step.name or idx}] 已执行 {n} 次, 达到批次上限 {mv} 次"
                    f"—— 不再继续循环, 按该步的 on_fail 收尾")
                return self._handle_fail(
                    step, idx, task, f"达到批次上限(已执行 {n} 次, 上限 {mv})")
        # 2026-10-19 v4.8.0: 验收点判定下限(见 _apply_judge_floor 的注释)
        self._apply_judge_floor(step, idx)

        for cb in self._on_step_start:
            cb(idx, step)
        self._emit_log("info", f"▶ 步骤 {idx+1}/{len(task.steps)}: {step.describe()}")

        tries = max(1, int(step.retry) + 1)
        # 2026-10-17 v3.5.0 统一 deadline: 整步(含每次重试、含 settle 复核循环)
        # 共享一份时间预算, 而不是"每次重试都重新跑满 timeout"。
        #   预算 = timeout × 重试次数, 再被步预算上限压一道。
        # 2026-10-19 v4.8.0: 上限改为 **先看本步 max_seconds, 再看全局** ——
        #   全局值只能当"谁也别超过"的天花板, 长战斗步要有自己的口子。
        budget = float(step.timeout) * tries
        cap = self.step_budget(step)
        if cap and budget > cap:
            self._emit_log(
                "info",
                f"步骤[{step.name}] 等待预算 {budget:g}s → 压缩为 {cap:g}s "
                f"({'本步 max_seconds' if getattr(step, 'max_seconds', 0) else 'config: engine.max_step_seconds'})")
            budget = cap
        hard_deadline = _now() + budget
        for attempt in range(1, tries + 1):
            if not self.running:
                return False
            try:
                hit = None
                if step.wait:
                    hit = self._wait_with_settle(step, hard_deadline)
                if step.action:
                    self._do_action(step, hit, task)
                if step.verify:
                    self._wait_until(step.verify, step.timeout,
                                     f"步骤[{step.name}] 验证条件", hard_deadline)
                for cb in self._on_step_end:
                    cb(idx, step, True)
                self._emit_log("info", f"✔ 步骤完成: {step.name or idx}")
                # 2026-10-17 v3.7.0: 验收点命中 —— 玩法面板说它已完成
                if step.verify_done:
                    self._mark_verified(step, True)
                # 2026-09-10 新增: wait 命中(成功)时的条件跳转, 用于监控循环提前退出
                if isinstance(step.on_success, dict) and "goto" in step.on_success:
                    g = task.index_of(step.on_success["goto"])
                    self._emit_log("info",
                                   f"条件成立跳转 → 步骤[{step.on_success['goto']}] #{g+1}")
                    raise _Goto(g, "success")
                return True
            except _Goto:
                # ⚠️ 必须在 except Exception **之前**: _Goto 是 Exception 子类,
                # 2026-10-17 加通用异常兜底时曾把它一并吃掉 —— jump / on_success
                # 跳转全被当成"步骤失败"记账, 循环脚本被判"重试耗尽"直接终止。
                # 跳转是正常控制流, 不是失败, 原样抛给 run_task 处理。
                raise
            except TimeoutError as e:
                self._emit_log("warning", f"第{attempt}次尝试失败: {e}")
                # 2026-10-17: 预算用尽就不再空转重试(重试也只是立刻再超时)
                if attempt < tries and _now() >= hard_deadline - 1:
                    self._emit_log(
                        "warning",
                        f"整步时间预算 {budget:g}s 已用尽, 不再重试")
                    attempt = tries
                if attempt < tries:
                    # 2026-09-16: 重试间隔也可中断(原先 time.sleep(1) 期间按停止要等满)
                    if not self._sleep(1):
                        self._emit_log("warning", "⏹ 任务已停止(重试间隔中被中断)")
                        return False
                    # 2026-10-19 v5.5.0: 重试要重新看画面 —— 否则帧还在复用窗口里,
                    # "再试一次"实际看的是刚才那张图, 等于没重试。
                    self._invalidate_frame()
                else:
                    self._dump_debug(step, f"step{idx}_{step.name}")
                    for cb in self._on_step_end:
                        cb(idx, step, False)
                    return self._handle_fail(
                        step, idx, task,
                        f"等待超时({(str(e).splitlines() or [''])[0][:60]})",
                        kind="timeout")
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
                return self._handle_fail(
                    step, idx, task,
                    f"脚本/注册表错误: {(str(e).splitlines() or [''])[0][:60]}")
            except InterruptedError:
                # 2026-10-19 v5.4.0(P1-1): **这段必须排在 except Exception 之前**。
                # InterruptedError 是 OSError → Exception 的子类, 而 Python 按
                # except 子句的书写顺序匹配 —— 旧版把它写在 except Exception 后面,
                # 于是"手动停止"永远走不到这里, 全被当成"意外异常按失败处理":
                # 用户点停止 → 结束统计里留一条「真失败」。
                # (与 2026-10-17「except _Goto 必须写在 except Exception 前」同型)
                cost = (_now() - self._stop_at) if self._stop_at else 0.0
                self._emit_log(
                    "warning",
                    f"⏹ 任务已被手动停止 (发出停止请求 {cost:.1f}s 后生效)")
                return False
            except Exception as e:
                # 2026-10-17 v3.5.0 (P0-2): 上面那组只覆盖"脚本写法错误", 而
                # DeviceError(adb 掉线/模拟器崩) / OSError / cv2.error / ImportError
                # (rapidocr 没装) 全都漏出 run_step → 一路冒泡穿出 run_task,
                # 结果: 没有失败汇总、没有现场截图、整条任务"戛然而止", 前面已做成
                # 的段全部白记。现在统一兜底 —— 记账 + 落图 + 交给 on_fail 决定,
                # 其余段照常跑; 只在日志里明确标出这是意外异常(区别于写法错误)。
                # 2026-10-19 v5.4.0(P1-1): 停止期间的基础设施异常不算"这一步做错了"。
                # 实测 15:17 —— 用户点停止 → 正在跑的步骤抛 DeviceError(截图失败)
                # → 结束统计里留下「真失败 1 步」。停止是正常操作, 不该留疤。
                # 判据用 self.running / self._stop_at 而不是异常类型: 停止时
                # adb screencap 可能抛 DeviceError/OSError/ConnectionError 任何一种。
                if (not self.running) or self._stop_at:
                    self._emit_log(
                        "warning",
                        f"⏹ 停止期间的异常不计入失败: {type(e).__name__}: {e}"
                        f"(步骤[{step.name}])")
                    return False
                reason = f"{type(e).__name__}: {e}"
                if isinstance(e, ImportError) and "ocr" in str(e).lower():
                    reason = (f"OCR 不可用(缺少 rapidocr): {e} "
                              f"— 所有 wait:{{ocr:}} 步骤都会失败")
                    if not self._ocr_warned:
                        self._ocr_warned = True
                        self._emit_log(
                            "error",
                            "✘ OCR 引擎不可用(rapidocr 未安装/加载失败), "
                            "所有依赖文字识别的步骤都会失败 —— 请检查 runtime 依赖")
                self._emit_log(
                    "error",
                    f"步骤[{step.name}] 发生意外异常, 已按失败处理(不影响后续步骤): {reason}")
                self._dump_debug(step, f"step{idx}_{step.name}")
                for cb in self._on_step_end:
                    cb(idx, step, False)
                return self._handle_fail(step, idx, task, reason)
        return False

    def _is_optional(self, step: Step) -> bool:
        """这一步是不是"条件探测" —— 没命中属正常, 不算失败

        2026-10-17 v3.5.0: 昨夜实战 157 步"跳过"无法分辨真假, 根子在于脚本里
        「若XX已完成则跳过」「查是否已领」「有没有这个弹窗」这类**纯探测步骤**
        和真失败共用一套计数。判定优先级:
          1) YAML 显式写 ``optional: true/false`` → 以它为准(脚本作者最清楚);
          2) 否则用启发式: **只有 wait、没有任何 action、且 on_fail 是 continue**
             → 它不可能"做"任何事, 没命中只是"这次没有", 属条件未命中。
             (典型: 「域外-批次后查已完成徽标」+ on_success:{goto:收尾})
        有 action 的步骤失败 = 该点的没点上/该等的没等到 = 真失败。

        例外: 作者显式写了 on_fail: stop / retry 的, 说明"没命中就该停下来",
        那是重要步骤 —— 即使符合探测特征也按真失败记, 尊重脚本意图。

        2026-10-18 v4.3.0 (P0-1): **验收点步骤(verify_done)绝不当"条件探测"**。
        面板说没做完(verify_done 没命中)是权威答案, 必须进 failed_steps 并标
        verified=False, 不能因为它写成 wait+continue 就被静默丢进 skipped_steps
        —— 否则任务仍被报成「成功 ✔」, 与用户确立的"以游戏面板为准"直接矛盾。
        """
        # 验收点步骤强制非 optional: 命中→已做成(记 verified=True, 不算失败);
        # 未命中→记 verified=False 并计入失败通道(至少是子任务级 ✘)。
        if getattr(step, "verify_done", False):
            return False
        if step.required:
            return False
        policy = step.on_fail
        if policy in ("stop", "retry") or policy is None:
            return False
        if step.optional:
            return True
        return (not step.action) and step.wait and policy == "continue"

    def _subtask_mode(self, task: Task) -> str:
        """这个任务该按什么粒度分组(2026-10-17 v3.7.0)

        · 由多个子脚本 include 组成(如 daily_all 的 7 个 parts) → "source",
          一个子脚本就是一个子任务, 名字用它的可读名。
        · 单脚本任务(如「每日必领」99 步全在一个文件里) → "segment",
          source 只有一种、分不出东西, 改按步骤名的段标记(① ② ③)分组。
        """
        srcs = {s.source for s in task.steps if getattr(s, "source", "")}
        return "source" if len(srcs) > 1 else "segment"

    def _mark_verified(self, step: Step, val: bool):
        """记下"这个子任务的达成验收结果"(2026-10-17 v3.7.0)

        val=True  = 面板显示已完成(卡片变灰/挂了「已完成」徽标)
        val=False = 面板没显示 —— 即使中间每步都走到了, 也以游戏给的答案为准。

        2026-10-18 v4.4.0: 同一子任务存在**多个验收点**时的合并规则:
          · 不同验收点(verify_retry 未标, 如一条龙验 4 张卡) → AND:
            任一卡未见徽标, 整段子任务即 ✘(面板是权威答案)。
          · 复核步(verify_retry: true, 同一张卡的二次确认) → OR:
            检查未命中后复核命中 = 面板动画/OCR 抖动恢复, 仍算已完成
            (保持旧版"后写覆盖"时代 检查✘→复核✔=✔ 的行为不变)。
        """
        try:
            sub = subtask_of(step, getattr(self, "_sub_mode", "segment"))
            val = bool(val)
            prev = self._verified.get(sub)
            if getattr(step, "verify_retry", False):
                self._verified[sub] = val if prev is None else (prev or val)
            else:
                self._verified[sub] = val if prev is None else (prev and val)
        except Exception:
            pass

    def _build_subtasks(self) -> list:
        """把失败记账聚合到子任务粒度(界面汇总只看这个)"""
        mode = getattr(self, "_sub_mode", "segment")
        total = getattr(self, "_sub_total", {}) or {}
        seen = getattr(self, "_sub_seen", set()) or set()
        # 按任务里首次出现的顺序排列(跟脚本结构一致, 便于对照)
        order: list[str] = []
        for s in getattr(self, "_cur_steps", []) or []:
            k = subtask_of(s, mode)
            if k not in order:
                order.append(k)
        fail_by: dict[str, int] = {}
        req_by: dict[str, int] = {}
        for f in self.failed_steps:
            k = f.get("sub") or "主流程"
            fail_by[k] = fail_by.get(k, 0) + 1
            if f.get("required"):
                req_by[k] = req_by.get(k, 0) + 1
        verified = getattr(self, "_verified", {}) or {}
        return [{"name": k,
                 "total": int(total.get(k, 0) or 0),
                 "reached": k in seen,
                 "failed": int(fail_by.get(k, 0) or 0),
                 "required_failed": int(req_by.get(k, 0) or 0),
                 # True=面板显示已完成 / False=未显示 / None=这段没有验收点(退回数步数)
                 "verified": verified.get(k)}
                for k in order]

    def _record_failure(self, step: Step, idx: int, reason: str,
                        kind: str = "fail"):
        """2026-10-16 v3.0.0: 记一笔失败, 并给步骤打标

        这是"失败不再被报成成功"的基础设施。以前 continue 分支直接 return True,
        上层无从知道这一步其实没做成。现在无论哪种兜底策略都先记账:
          · required=True  → 汇总时升级为整条任务失败
          · required=False → 任务仍算跑完, 但结束时汇总告警, 列出被跳过的步骤

        2026-10-17 v3.5.0: 再细分一层 —— optional(条件探测)的未命中记进
        skipped_steps, 不进 failed_steps, 汇总里单列"条件未命中 N 步"。
        这样"真失败 3 步 / 条件未命中 154 步"一眼可辨, 不再是一锅粥。

        2026-10-19 v5.3.0(用户定调): **纯等待步骤的等待超时一律算"条件未命中"**。
        用户的判断: "等 15 秒没等到" 本身是一次探测没命中(战斗还没打完/面板还没
        刷出来/动画还没停), 不是"这一步做错了", 把它列进结束时的「真失败 N 步」
        只是噪音 —— 实测一轮日常里 28 步"未做成"有一大半是这种等待超时, 其中
        「域外验收-看已完成徽标」+「复核徽标」一个玩法就顶了两步。
        判定: kind == "timeout" **且这一步没有动作**(step.action 为空) → skipped。
        有动作的步骤(点前往/点领取/点师傅…)超时仍记真失败 —— "该点的没点成"
        是真问题, 不能一起抹掉。
        **验收点(verify_done)的面板结论不受影响**: _mark_verified 照旧记
        True/False, 子任务汇总里该 ✔ / ✘ 还是 ✔ / ✘, 权威答案没被削弱。
        required 步骤仍然算真失败(关键路径没做成, 任务该停还是停)。
        """
        step.failed = True
        rec = {
            "index": idx + 1,                       # 1基, 跟界面显示一致
            "name": step.name or f"#{idx+1}",
            "reason": reason,
            "policy": step.on_fail if isinstance(step.on_fail, str) else "goto",
            "required": bool(step.required),
            # 2026-10-17 v3.7.0: 归属子任务(界面按它聚合, 不再逐条列)
            "sub": subtask_of(step, getattr(self, "_sub_mode", "segment")),
        }
        if step.verify_done:
            # 验收点没命中 —— 游戏面板没显示"已完成", 这就是权威答案
            self._mark_verified(step, False)
        # 「纯等待超时」= 这一步没有动作可做、也没被作者标成"没等到就该停/重试"
        # (on_fail: stop / retry) —— 只是这次没等到, 记未命中即可。
        # verify_done 的验收步也走这条: 面板结论已由 _mark_verified 记下, 不必
        # 再在"真失败步数"里重复算一次。
        intentional = step.on_fail in ("stop", "retry")
        soft = (self._is_optional(step)
                or (kind == "timeout" and not step.action and not intentional))
        if soft and not step.required:
            self.skipped_steps.append(rec)
            # 2026-10-17 v3.7.0: 正常的未命中不占界面(每步一行太吵), 只进 run.log
            self._emit_log(
                "info",
                f"○ 条件未命中(属正常): [{rec['name']}] ({reason}) — on_fail={rec['policy']}",
                gui=False)
            return
        self.failed_steps.append(rec)
        lvl = "error" if step.required else "warning"
        self._emit_log(
            lvl,
            f"{'✘ 关键步骤未做成' if step.required else '⚠ 步骤未做成'}: "
            f"[{rec['name']}] ({reason}) — on_fail={rec['policy']}"
            f"{'; 已标记 required, 任务将判失败' if step.required else ''}")

    def _handle_fail(self, step: Step, idx: int, task: Task,
                     reason: str = "超时或识别失败", kind: str = "fail") -> bool:
        """on_fail 策略，返回 True=可继续执行后续，False=终止任务

        2026-10-16 v3.0.0: 各分支先 _record_failure() 再决定走向。
        注意 continue 仍返回 True(保持 185 处脚本的既有行为, 任务照常往下跑),
        但失败已被记账 —— 任务结束时会在汇总里点名, 不再静默。

        2026-10-17 v3.5.0: reason 由调用方传入**真实原因**。以前四个分支都写死
        "超时或识别失败", 于是"adb 掉线""脚本引用了没登记的按钮""OCR 没装"
        在汇总里长得一模一样, 根本没法排错 —— 现在逐条都能看出到底为什么。

        2026-10-19 v5.3.0: kind="timeout" 表示这是**等待条件超时**(不是异常、
        不是写错脚本、不是点击失败)。这类只记"条件未命中", 不计入结束统计,
        详见 _record_failure 的说明。
        """
        policy = step.on_fail
        if policy == "retry":
            self._record_failure(step, idx, f"重试耗尽({reason})", kind)
            return False                      # 已重试完，终止
        if policy == "continue":
            self._record_failure(step, idx, reason, kind)
            self._emit_log("warning", "跳过失败步骤继续")
            return True
        if policy == "stop":
            self._record_failure(step, idx, reason, kind)
            self._emit_log("error", "任务终止")
            return False
        if isinstance(policy, dict) and "goto" in policy:
            self._record_failure(step, idx, f"{reason}(跳转收尾)", kind)
            g = task.index_of(policy["goto"])
            self._emit_log("warning", f"跳转到步骤 {g+1}")
            # 通过异常跳出当前循环，由 run() 处理跳转
            raise _Goto(g)
        self._record_failure(step, idx, "未配置 on_fail 兜底", kind)
        return False

    def _rotate_debug(self, force: bool = False):
        """滚动清理失败现场截图目录(logs/debug*)。

        2026-10-17 v3.5.0(P1-5): 以前只增不减 —— 实测一晚 157 张 / 134MB 永久
        常驻, 跑几晚就把便携版撑到几百 MB, 而「导出诊断信息」和外发时还会把这
        些游戏截图一起带出去。现在按 config 的
          engine.debug_keep_max  (最多保留几张, 0=不限)
          engine.debug_keep_days (最多保留几天, 0=不限)
        两条规则取并集清理。每落 ROTATE_EVERY 张才扫一次目录(一步一 listdir
        在 926 步的任务里是纯浪费), 任务开始强制扫一次。
        """
        if not self.debug_dump_dir:
            return
        if not force:
            self._debug_since_rotate += 1
            if self._debug_since_rotate < self._ROTATE_EVERY:
                return
        self._debug_since_rotate = 0
        if self._cfg_cache is None:
            try:
                from common import load_config
                self._cfg_cache = load_config()
            except Exception:
                self._cfg_cache = {}
        eng = self._cfg_cache.get("engine", {}) if isinstance(self._cfg_cache, dict) else {}
        keep_max = int(eng.get("debug_keep_max", 200) or 0)
        keep_days = int(eng.get("debug_keep_days", 7) or 0)
        if not keep_max and not keep_days:
            return
        n = rotate_files(self.debug_dump_dir, keep_max=keep_max, keep_days=keep_days)
        if n:
            self._emit_log("debug", f"失败现场截图滚动清理: 删除 {n} 张(上限 {keep_max} 张 / {keep_days} 天)")

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
            # 2026-10-18 v4.6.0(P0-4): PNG → JPEG。
            # 三条真实截图实测: PNG 431/682/1061 KB → JPEG 43/99/159 KB(省 86%)。
            # 质量取 80 而不是更低: 失败截图有时要用来复现 OCR 为什么认错,
            # 压太狠小字糊掉就失去排查价值了。
            # 按 120 张配额算: 单端口目录约 85 MB → 约 12 MB。
            p = os.path.join(self.debug_dump_dir, f"{safe}_{int(time.time())}.jpg")
            # 2026-09-11 修复: cv2.imwrite 对含中文的路径在 Windows 上偶发静默失败,
            # 改用 imencode + 二进制写文件, 彻底兼容中文步骤名
            try:
                ok, buf = cv2.imencode(
                    ".jpg", self._last_screen,
                    [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                if ok:
                    with open(p, "wb") as f:
                        f.write(buf.tobytes())
                else:
                    ok = False
            except Exception:
                ok = False
            if ok:
                self._emit_log("info", f"失败现场截图: {p}")
                self._rotate_debug()
            else:
                self._emit_log("warning", f"失败现场截图保存失败(路径非法?): {p}")

    # ---------- 失败汇总 (2026-10-16 v3.0.0) ----------
    def failure_summary(self) -> str:
        """失败/未命中步骤的可读汇总；两者都为空返回空串

        这是给"任务到底做成没有"一个诚实的答案。以前只有 ✅/❌ 两个字，
        且 ❌ 几乎不会出现(continue 全被当成功)，帮派捐献那种"整段跳过却报
        成功"完全看不出来。现在界面/命令行都能拿到逐条明细。

        2026-10-17 v3.5.0: 文案改由 RunResult.summary() 生成, 并**分两档**
        (真失败 / 条件未命中)。⚠ 旧版调用方曾用 ``split(" 步未做成")[0]`` 从
        首行切数字 —— 新格式首行是"真失败 N 步", 那些旧解析已全部改为读
        counts(), 不要再靠文案切数字。
        """
        res = self.last_result
        if res is not None:
            return res.summary()
        return RunResult(
            ok=not self.has_required_failure(),
            failed=list(self.failed_steps),
            skipped=list(self.skipped_steps),
        ).summary()

    def subtask_summary(self) -> str:
        """子任务级汇总(2026-10-17 v3.7.0)

        界面只看这个: 每个子任务一行(✔/⚠/✘ + 步数), 不再逐条列步骤。
        逐步明细仍由 failure_summary() 生成, 但只写 run.log —— 926 步的日常
        在界面刷上百行流水账, 反而把"到底成没成"淹了(用户反馈)。
        """
        res = self.last_result
        if res is not None and res.subtasks:
            return res.subtask_summary()
        return RunResult(
            ok=not self.has_required_failure(),
            failed=list(self.failed_steps),
            skipped=list(self.skipped_steps),
            subtasks=self._build_subtasks(),
        ).subtask_summary()

    def _emit_failure_summary(self, level: str, prefix: str, to_gui: bool = True):
        """把已积累的失败明细打到日志(供任务结束/终止时调用)

        2026-10-17 v3.7.0: to_gui=False 时**只写 run.log, 不推给界面**。
        正常跑完的汇总用这个 —— 界面已经有子任务级汇总了, 再推逐步明细就是刷屏。
        异常终止(死循环/required 失败/用户停止)仍走 to_gui=True, 那时候必须看细节。
        """
        s = self.failure_summary()
        if not s:
            return
        if to_gui:
            self._emit_log(level, f"{prefix}\n{s}")
        elif self.logger:
            try:
                getattr(self.logger, level)(f"{prefix}\n{s}")
            except Exception:
                pass

    def has_required_failure(self) -> bool:
        """是否有标记 required 的步骤失败了 —— 有则整条任务判失败"""
        return any(f["required"] for f in self.failed_steps)

    def counts(self) -> dict:
        """结构化统计(给 GUI/命令行用, 别再从文案里切数字)

        2026-10-17 v3.5.0 (P2-4): 旧实现 ``int(summary.split(" 步未做成")[0])``
        在文案变化时会**静默得到 0**, 界面随即显示"完成"——失败被彻底掩盖。
        """
        return {
            "failed": len(self.failed_steps),
            "skipped": len(self.skipped_steps),
            "required_failed": sum(1 for f in self.failed_steps if f["required"]),
            "stopped": self._stop_at is not None,
            # 2026-10-17 v3.7.0: 子任务级统计(界面按它汇总, 不再逐条列步骤)
            "subtasks": self._build_subtasks(),
            # 2026-10-17 v3.9.0: 完成通报只显示最后一个步骤(用户要求)
            "last_step": getattr(self, "_last_step", ""),
        }

    # ---------- 任务整体 ----------
    def run_task(self, task: Task, start_index: int = 0) -> bool:
        """从 start_index 顺序执行全部步骤；返回是否全部成功

        2026-10-16 v3.0.0 调整成败判定(详见 _record_failure / failure_summary):
          · 旧行为: on_fail: continue → 返回 True → 判成功, 失败被完全掩盖
          · 新行为: 所有失败都记账; 结束时
              - 有 required=True 的步骤失败 → 返回 False(整条失败)
              - 有非关键步骤失败         → 仍返回 True, 但汇总告警点名
              - 全成功                   → 返回 True
            (默认 required=False, 故 185 处 continue 脚本行为不变, 不会天天报红)
        """
        self.running = True
        self.stats["t0"] = _now()          # v5.5.0: 性能统计的计时起点
        self._stop_at = None
        self._beat()                    # 启动即发一拍, 让 GUI 尽快拿到推进证据(P0-3)
        self.failed_steps = []          # 每次运行重置, 避免多开/连跑串味
        self.skipped_steps = []         # 2026-10-17: 条件未命中单独一档
        self._ocr_warned = False
        self.last_result = None
        self._goto_counts = {}          # goto 死循环保护: 每次运行重置
        # 2026-10-17: step.failed 以前**跨运行不重置** —— 同一 Task 对象跑第二遍时
        # 上一轮的"失败"标记还留在步骤上, 界面「脚本步骤」页会把已经做成的步骤
        # 继续标成跳过。这里跟着 failed_steps 一起清。
        for _s in task.steps:
            _s.failed = False
        self._exec_count = 0            # 全局步骤执行计数(兜底)重置
        self._last_step = ""            # 最后执行的步骤(v3.9.0)
        # 2026-10-19 v4.8.0: 每步执行次数(step.max_visits 批次上限)。每次运行重置,
        # 同一 Task 对象连跑两遍不会互相继承计数。
        self._visits: dict[int, int] = {}
        # 2026-10-17 v3.7.0: 子任务分组(界面汇总只看这个粒度)
        self._cur_steps = list(task.steps)
        self._sub_mode = self._subtask_mode(task)
        self._sub_seen: set[str] = set()
        self._sub_total: dict[str, int] = {}
        self._visits = {}               # 2026-10-19 v4.8.0: step.max_visits 计数
        self._verified: dict[str, bool] = {}   # 验收点结果(True/False), 权威答案
        for _s in task.steps:
            _k = subtask_of(_s, self._sub_mode)
            self._sub_total[_k] = self._sub_total.get(_k, 0) + 1
        # 2026-10-17 v3.5.0: 开跑前先滚一次(上一轮/上一晚留下的图不该无限堆积)
        self._rotate_debug(force=True)
        idx = start_index
        try:
            while idx < len(task.steps):
                # 2026-10-16 v3.0.4: 全局步骤执行上限(兜底, 防任何形式的不收敛循环)
                self._exec_count += 1
                _backstop = max(len(task.steps) * 20, 3000)
                if self._exec_count > _backstop:
                    self._emit_log(
                        "error",
                        f"✘ 任务执行步数超过上限 {_backstop}(疑似死循环), 任务终止。"
                        f"请检查脚本里是否有不收敛的 goto/等待循环。")
                    self._emit_failure_summary("error", "终止前已未做成的步骤:")
                    return self._finish(False, task.name, "执行步数超上限(疑似死循环)")
                try:
                    ok = self.run_step(idx, task)
                    # 2026-09-16: 用户主动停止不算"失败", 不打误导性的"重试耗尽"终止日志
                    if not self.running:
                        self._emit_log("warning", "⏹ 任务已停止")
                        self._emit_failure_summary("warning", "停止前已有步骤未做成:")
                        return self._finish(False, task.name, "用户手动停止")
                    if not ok and not (isinstance(task.steps[idx].on_fail, dict)):
                        # 2026-09-14 补救: 硬失败原先静默 return, run.log 戛然而止
                        # 无从分析。终止时必须留下终止步骤与原因。
                        self._emit_log(
                            "error",
                            f"✘ 任务终止: 步骤[{task.steps[idx].name}] "
                            f"重试耗尽且未配置 on_fail 兜底")
                        self._emit_failure_summary("error", "终止前已未做成的步骤:")
                        return self._finish(
                            False, task.name,
                            f"步骤[{task.steps[idx].name}] 重试耗尽且无 on_fail 兜底")
                except _Goto as g:
                    idx = g.target
                    if idx < 0 or idx >= len(task.steps):
                        self._emit_log("error", f"goto 目标非法: {idx+1}")
                        self._emit_failure_summary("error", "终止前已未做成的步骤:")
                        return self._finish(False, task.name, f"goto 目标非法: {idx+1}")
                    # 2026-10-16 v3.0.4: 死循环保护 —— 同一目标被反复跳转即判疑似死循环。
                    # 实录: 域外日常 on_fail:{goto: 等打完回主界面} 在"主界面"始终不成立时,
                    # 反复跳回该步形成 875↔878 死循环, 进程被强杀才停。加次数上限后自动终止。
                    # 2026-10-17 v3.5.0: **只统计失败回跳(cause="fail")** ——
                    #   以前 on_success 跳转与 action:jump 的合法回边也被计数,
                    #   长循环(如每日必领 15 次宝箱)会被上限误杀。
                    if g.cause == "fail":
                        self._goto_counts[idx] = self._goto_counts.get(idx, 0) + 1
                        limit = self._goto_loop_limit()
                        if self._goto_counts[idx] > limit:
                            tname = task.steps[idx].name if 0 <= idx < len(task.steps) else f"#{idx+1}"
                            self._emit_log(
                                "error",
                                f"✘ 检测到 goto 死循环: 步骤[{tname}] 已被重复跳转 "
                                f"{self._goto_counts[idx]} 次(超过上限 {limit})。"
                                f"疑似回跳目标依赖的条件始终不成立(例如一直不在主界面), 任务终止。"
                                f"建议检查该 goto 回跳路径上的等待条件。")
                            self._emit_failure_summary("error", "终止前已未做成的步骤:")
                            return self._finish(
                                False, task.name,
                                f"goto 死循环: 步骤[{tname}] 回跳 {self._goto_counts[idx]} 次")
                    continue
                if not self.running:
                    return self._finish(False, task.name, "用户手动停止")
                idx += 1

            # ---- 跑完全部步骤, 按失败明细判定 ----
            if self.has_required_failure():
                self._emit_log(
                    "error",
                    f"✘ 任务失败: {task.name} —— 有关键步骤(required: true)未做成")
                self._emit_failure_summary("error", "未做成的步骤:")
                return self._finish(False, task.name,
                                    f"{self.counts()['required_failed']} 个关键步骤未做成")
            n_skip = len(self.skipped_steps)
            # 2026-10-17 v3.9.0 用户确立: 成功通报只报最后一个步骤, 子任务汇总/
            # 条件未命中计数全部撤掉 —— "目标都能完成, 总结没什么用"。
            #   失败/终止路径保留汇总(那时才需要排障信息); 逐步明细照旧进 run.log。
            if self.failed_steps:
                sub = self.subtask_summary()
                self._emit_log(
                    "warning",
                    f"⚠ 任务跑完但有 {len(self.failed_steps)} 步未做成: {task.name}"
                    + (f"（另有 {n_skip} 步条件未命中, 属正常）" if n_skip else ""))
                if sub:
                    self._emit_log("warning", sub)
                self._emit_failure_summary(
                    "warning", "未做成的步骤(已按 on_fail 跳过, 明细仅供排障):",
                    to_gui=False)
            else:
                self._emit_log(
                    "info",
                    f"✔ 任务完成: {task.name} — 最后执行 {self._last_step or '?'}")
            return self._finish(True, task.name)
        finally:
            self.running = False
            # 2026-10-17 v3.5.0 (P0-2): 走到这里而 last_result 仍为空 = 有异常冒泡
            # 出了 run_task(不是走任何一条正常收尾分支)。补一份失败快照, 让外层的
            # except 仍能拿到完整的失败明细与汇总 —— 以前这种路径下 run.log 直接
            # 戛然而止, 前面已做成的段落完全无从判断。
            if self.last_result is None:
                self._finish(False, task.name, "异常终止(未走正常收尾分支)")

    def _finish(self, ok: bool, name: str, reason: str = "") -> bool:
        """收尾: 快照结构化结果并返回 ok(2026-10-17 v3.5.0)

        GUI / run_one 现在读 ``engine.counts()`` 而不是解析文案, 见 P2-4。
        """
        self._emit_stats()
        self.last_result = RunResult(
            ok=ok,
            name=name,
            failed=list(self.failed_steps),
            skipped=list(self.skipped_steps),
            stopped=self._stop_at is not None,
            aborted_reason=reason,
            subtasks=self._build_subtasks(),
        )
        return ok

    def _emit_stats(self):
        """打一行性能统计(v5.5.0)

        不看数字的优化等于没做 —— 这一行让每轮跑完都能自证:
        截图/OCR 各多少次、有多少是被缓存和热点窗口省下来的、一共花了多少秒。
        与 2026-10-19 的基线对比(OCR 341 帧 8.8 分钟 / 截图 417 次 6 分钟)即可判断
        下一版该往哪儿下手。
        """
        st = self.stats
        ocr_n = st["ocr_full"] + st["ocr_band"]
        if not st["shots"] and not ocr_n:
            return
        span = (_now() - st["t0"]) if st["t0"] else 0.0
        saved = (st["frame_reuse"] * 0.85 + st["ocr_hint_hit"] * 1.2
                 + st["ocr_same_screen"] * 1.4)
        msg = (f"⚡ 性能: 截图 {st['shots']} 次(复用 {st['frame_reuse']} 次) · "
               f"OCR {ocr_n} 次(全图 {st['ocr_full']} / 裁剪带 {st['ocr_band']} / "
               f"热点命中 {st['ocr_hint_hit']} / 同帧复用 {st['ocr_cached']} / "
               f"同屏跳过 {st['ocr_same_screen']}) · "
               f"累计截图 {st['shot_seconds']:.0f}s + OCR {st['ocr_seconds']:.0f}s")
        if span > 1:
            msg += (f" · 占本轮 {span/60:.1f} 分钟的 "
                    f"{(st['shot_seconds']+st['ocr_seconds'])/span*100:.0f}%"
                    f"(估算已省 {saved/60:.1f} 分钟)")
        self._emit_log("info", msg)

    def stop(self):
        """请求停止(协作式): 置标志即可, 各等待/睡眠点会在 0.1s 内察觉到

        2026-09-16: 增加停止请求日志与时间戳。用户点"停止"后此前无任何
        输出, 日志上看不出停止请求何时发出, 无法判断是"没响应"还是"卡在某步"。
        """
        if self.running and not self._stop_at:
            self._stop_at = _now()     # v4.8.0: 只用来算耗时 → 单调时钟
            self._emit_log(
                "warning",
                "⏹ 收到停止请求, 正在安全停止(正在进行的截图/点击会先结束)…")
        self.running = False


class _Goto(Exception):
    """内部跳转信号

    2026-10-17 v3.5.0: 增加 cause 区分跳转来源 ——
      · "fail"    on_fail:{goto} 引发的**失败回跳** → 计入死循环保护
      · "success" on_success:{goto} 条件成立跳转(监控循环提前收尾) → 不计
      · "jump"    action: jump 合法回边(如每日必领 15 次宝箱循环) → 不计

    以前三种一视同仁地计数, 长循环(合法)会被 GOTO_LOOP_LIMIT 误杀 ——
    实测 daily_claim.yaml:95 的宝箱循环就有 15 次合法回边。
    """
    def __init__(self, target: int, cause: str = "fail"):
        super().__init__(f"goto {target} ({cause})")
        self.target = target
        self.cause = cause
