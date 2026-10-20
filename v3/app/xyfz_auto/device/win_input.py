# -*- coding: utf-8 -*-
"""Windows 鼠标注入输入通道 (device/win_input.py)

背景: 大话西游对 adb `input tap` 有反注入检测 —— 部分弹窗按钮
(如师门介绍页的师傅牌匾)对 adb 点击完全无响应, 而真实鼠标正常。
本模块用 Windows API (SetCursorPos + mouse_event) 向 MuMu 窗口
注入真实鼠标事件, 实测可绕过该屏蔽。

代价与限制:
- 需要把 MuMu 窗口置前, 且会移动真实鼠标 → 点击瞬间不能动鼠标
- 窗口被其它窗口遮挡的区域会点错(点击落在遮挡窗口上)
- 坐标映射需要校准: 屏幕坐标 = 客户区原点 + 安卓坐标*scale + 偏移
  (MuMu 客户区顶部有自绘标题栏, 偏移 y≈39px)

2026-09-28 (v1.4.0) 关键修复 —— 换机器后"未找到 MuMu 窗口":
  1. 不再要求窗口标题【完全相等】。旧版写死 title == "MuMu安卓设备",
     换个 MuMu 版本/多开器后标题会变(如 "MuMu模拟器12"、"MuMu Player 12"、
     带实例后缀等), 于是直接找不到窗口 → 任务中断。
     现在改为【标题关键字 + 进程名】双路模糊匹配, 覆盖各版本命名。
  2. 找不到时不再只抛一句异常: 附上【诊断清单】(所有顶层窗口的
     标题/类名/进程/矩形), 可直接照抄进 config.yaml。
  3. 引擎侧默认【降级为 adb 点击继续跑】, 不再因为一个 win 步骤
     中断整条任务(见 engine/runner.py 的 _ensure_win_input)。
  4. 全部参数可从 config.yaml 的 win_input 段配置(hwnd/关键字/多开序号/校准值)。

用法:
    from device.win_input import WinMouse
    win = WinMouse.from_config(cfg)   # 从 config.yaml 的 win_input 段构造
    win = WinMouse()                  # 或纯自动查找
    win.tap(124, 796)                 # 安卓坐标点击
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.SetProcessDPIAware()

# MuMu 12 (540x960) 默认校准参数 —— 2026-09-12 calib_win.py 重校
DEFAULT_SCALE_X = 0.6772
DEFAULT_SCALE_Y = 0.6754
DEFAULT_Y_OFFSET = 40.5          # 自绘标题栏高度(客户区顶部)

# 旧版写死的标题, 仅作为"精确命中"的最高优先级候选
LEGACY_TITLE = "MuMu安卓设备"
WINDOW_TITLE = LEGACY_TITLE      # 兼容旧引用

# 默认匹配关键字(模糊, 不区分大小写)
DEFAULT_TITLE_KEYWORDS = (
    "MuMu安卓设备", "MuMu模拟器", "MuMu Player", "MuMuPlayer", "MuMu",
)
DEFAULT_EXE_KEYWORDS = (
    "MuMuPlayer", "MuMuNxMain", "NemuPlayer", "MuMuVMM", "MuMu",
)

_PROC_CACHE: dict[int, str] = {}
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class WinWindowNotFound(RuntimeError):
    """找不到可用的 MuMu 窗口(消息里带诊断清单)"""


# ================= 进程/窗口枚举 =================
def _proc_exe(pid: int) -> str:
    """按 PID 取可执行文件全路径(失败返回空串)。结果带缓存。"""
    if pid in _PROC_CACHE:
        return _PROC_CACHE[pid]
    path = ""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                path = buf.value
        except Exception:
            path = ""
        finally:
            kernel32.CloseHandle(h)
    _PROC_CACHE[pid] = path
    return path


def list_windows(*, only_visible: bool = True) -> list[dict]:
    """枚举顶层窗口 → [{hwnd,title,cls,pid,exe,exe_name,rect,visible,iconic}]

    rect = (left, top, right, bottom); 按 (top, left) 排序, 多开时顺序稳定。
    """
    out: list[dict] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        visible = bool(user32.IsWindowVisible(hwnd))
        if only_visible and not visible:
            return True
        t = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, t, 512)
        c = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, c, 256)
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        exe = _proc_exe(int(pid.value)) if pid.value else ""
        r = wintypes.RECT()
        try:
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            rect = (r.left, r.top, r.right, r.bottom)
        except Exception:
            rect = (0, 0, 0, 0)
        out.append({
            "hwnd": int(hwnd),
            "title": t.value,
            "cls": c.value,
            "pid": int(pid.value),
            "exe": exe,
            "exe_name": exe.rsplit("\\", 1)[-1] if exe else "",
            "rect": rect,
            "visible": visible,
            "iconic": bool(user32.IsIconic(hwnd)),
        })
        return True

    user32.EnumWindows(proto(cb), 0)
    out.sort(key=lambda w: (w["rect"][1], w["rect"][0], w["hwnd"]))
    return out


def _keywords(cfg_key: str, defaults: tuple[str, ...], cfg: dict | None) -> tuple[str, ...]:
    """合并默认关键字与 config.yaml 里追加的关键字"""
    extra: list[str] = []
    if isinstance(cfg, dict):
        v = cfg.get(cfg_key)
        if isinstance(v, str) and v.strip():
            extra = [v.strip()]
        elif isinstance(v, (list, tuple)):
            extra = [str(x).strip() for x in v if str(x).strip()]
    low = {d.lower() for d in defaults}
    merged = list(defaults) + [k for k in extra if k.lower() not in low]
    return tuple(merged)


def _match_windows(title_kw, exe_kw, *, only_visible=True) -> list[dict]:
    hits: list[dict] = []
    for w in list_windows(only_visible=only_visible):
        if not w["title"]:
            continue
        t, e = w["title"].lower(), w["exe_name"].lower()
        if any(k.lower() in e for k in exe_kw) or any(k.lower() in t for k in title_kw):
            hits.append(w)

    def rank(w):
        exact = 0 if w["title"] == LEGACY_TITLE else 1
        return (exact, 1 if w["iconic"] else 0, w["rect"][1], w["rect"][0])
    hits.sort(key=rank)
    return hits


def diagnose_text(limit: int = 40) -> str:
    """诊断清单: 列出所有顶层窗口, 标注 MuMu 命中项。"""
    wins = list_windows(only_visible=True)
    kw_t = DEFAULT_TITLE_KEYWORDS
    kw_e = DEFAULT_EXE_KEYWORDS
    lines = [f"当前可见顶层窗口 {len(wins)} 个 (标题 / 类名 / 进程 / 矩形):"]
    n_hit = 0
    for i, w in enumerate(wins[:limit], 1):
        is_hit = (any(k.lower() in w["exe_name"].lower() for k in kw_e)
                  or any(k.lower() in w["title"].lower() for k in kw_t))
        if is_hit:
            n_hit += 1
        lines.append(
            f"  {i:2d}. hwnd=0x{w['hwnd']:X} [{w['title'] or '(无标题)'}] "
            f"类={w['cls']} 进程={w['exe_name'] or '?'} "
            f"位置={w['rect']}{' [最小化]' if w['iconic'] else ''}"
            f"{' ← MuMu候选' if is_hit else ''}")
    if len(wins) > limit:
        lines.append(f"  ...(仅显示前 {limit} 个)")
    lines.append(f"其中 MuMu 候选 {n_hit} 个。")
    return "\n".join(lines)


def resolve_window(cfg: dict | None = None) -> tuple[int, dict]:
    """按配置解析出目标 MuMu 窗口 → (hwnd, 窗口信息)。

    解析顺序:
      1) cfg["hwnd"] 指定且有效
      2) 标题关键字 / 进程名 模糊匹配, 取 cfg["index"] 指定的那个(多开)
    失败抛 WinWindowNotFound, 消息内含诊断清单。
    """
    cfg = cfg if isinstance(cfg, dict) else {}

    try:
        forced = int(cfg.get("hwnd") or 0)
    except (TypeError, ValueError):
        forced = 0
    if forced and user32.IsWindow(forced):
        for w in list_windows(only_visible=False):
            if w["hwnd"] == forced:
                return forced, w
        return forced, {"hwnd": forced, "title": "(配置指定)", "exe_name": "",
                        "rect": (0, 0, 0, 0), "iconic": False}

    title_kw = _keywords("title_contains", DEFAULT_TITLE_KEYWORDS, cfg)
    exe_kw = _keywords("exe_contains", DEFAULT_EXE_KEYWORDS, cfg)
    hits = _match_windows(title_kw, exe_kw)
    if not hits:
        # 最小化窗口也认 —— 引擎侧会先还原再点击
        hits = [w for w in _match_windows(title_kw, exe_kw, only_visible=False)
                if w["iconic"]]
    if not hits:
        raise WinWindowNotFound(
            "未找到 MuMu 窗口, Windows 鼠标输入通道不可用。\n"
            f"  已尝试标题关键字: {list(title_kw)}\n"
            f"  已尝试进程关键字: {list(exe_kw)}\n"
            + diagnose_text()
            + "\n修复: 在 config.yaml 的 win_input 段补 title_contains / exe_contains, "
              "或把上面某行的 hwnd 直接填进 win_input.hwnd。")

    try:
        idx = int(cfg.get("index") or 0)
    except (TypeError, ValueError):
        idx = 0
    idx = max(0, min(idx, len(hits) - 1))
    w = hits[idx]
    return w["hwnd"], w


def find_mumu_windows(title_contains=None, exe_contains=None, index: int = 0,
                      hwnd: int = 0) -> list[int]:
    """兼容旧接口: 返回匹配到的 hwnd 列表(按多开顺序)。

    2026-09-28: 由"标题完全相等"改为模糊匹配; 找不到时返回空列表(不抛异常)。
    """
    cfg: dict = {"hwnd": hwnd, "index": index}
    if title_contains:
        cfg["title_contains"] = title_contains
    if exe_contains:
        cfg["exe_contains"] = exe_contains
    kw_t = _keywords("title_contains", DEFAULT_TITLE_KEYWORDS, cfg)
    kw_e = _keywords("exe_contains", DEFAULT_EXE_KEYWORDS, cfg)
    return [w["hwnd"] for w in _match_windows(kw_t, kw_e, only_visible=False)]


# ================= 鼠标注入 =================
class WinMouse:
    """通过 Windows 鼠标事件点击 MuMu 安卓坐标"""

    SW_RESTORE = 9
    SW_SHOW = 5
    HWND_TOP = 0
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_SHOWWINDOW = 0x0040

    def __init__(self, hwnd: int | None = None, *,
                 cfg: dict | None = None,
                 scale_x: float | None = None,
                 scale_y: float | None = None,
                 y_offset: float | None = None,
                 foreground_delay: float | None = None,
                 hold_ms: int | None = None):
        c = cfg if isinstance(cfg, dict) else {}
        self.info: dict = {}
        if hwnd is None:
            hwnd, self.info = resolve_window(c)
        self.hwnd = int(hwnd)
        self.scale_x = float(scale_x if scale_x is not None
                             else c.get("scale_x", DEFAULT_SCALE_X))
        self.scale_y = float(scale_y if scale_y is not None
                             else c.get("scale_y", DEFAULT_SCALE_Y))
        self.y_offset = float(y_offset if y_offset is not None
                              else c.get("y_offset", DEFAULT_Y_OFFSET))
        self.foreground_delay = float(foreground_delay if foreground_delay is not None
                                      else c.get("foreground_delay", 0.35))
        self.hold_ms = int(hold_ms if hold_ms is not None
                           else c.get("hold_ms", 60))

    @classmethod
    def from_config(cls, cfg: dict | None = None) -> "WinMouse":
        """从整份 config 里读 win_input 段构造"""
        wi = {}
        if isinstance(cfg, dict):
            wi = cfg.get("win_input", {}) or {}
        return cls(cfg=wi)

    # ---- 窗口几何 ----
    def client_origin(self) -> tuple[int, int]:
        pt = wintypes.POINT(0, 0)
        user32.ClientToScreen(self.hwnd, ctypes.byref(pt))
        return pt.x, pt.y

    def to_screen(self, ax: float, ay: float) -> tuple[int, int]:
        ox, oy = self.client_origin()
        return (int(round(ox + ax * self.scale_x)),
                int(round(oy + self.y_offset + ay * self.scale_y)))

    # ---- 置前 ----
    def force_foreground(self) -> bool:
        """把窗口还原/置前。返回是否成功成为前台窗口。

        2026-09-28: 旧版只调 SetForegroundWindow —— 窗口最小化时无效,
        且 Windows 对"非前台进程调用"会静默失败, 于是点击落在别处。
        现在先 ShowWindow 还原, 再 SetWindowPos 提前, 最后校验并补救。
        """
        try:
            if user32.IsIconic(self.hwnd):
                user32.ShowWindow(self.hwnd, self.SW_RESTORE)
            else:
                user32.ShowWindow(self.hwnd, self.SW_SHOW)
            user32.SetWindowPos(self.hwnd, self.HWND_TOP, 0, 0, 0, 0,
                                self.SWP_NOSIZE | self.SWP_NOMOVE | self.SWP_SHOWWINDOW)
            user32.SetForegroundWindow(self.hwnd)
        except Exception:
            return False
        time.sleep(0.05)
        if user32.GetForegroundWindow() == self.hwnd:
            return True
        try:
            fg = user32.GetForegroundWindow()
            tid_fg = user32.GetWindowThreadProcessId(fg, None)
            tid_me = kernel32.GetCurrentThreadId()
            if tid_fg and tid_fg != tid_me:
                user32.AttachThreadInput(tid_me, tid_fg, True)
                user32.SetForegroundWindow(self.hwnd)
                user32.AttachThreadInput(tid_me, tid_fg, False)
        except Exception:
            pass
        return user32.GetForegroundWindow() == self.hwnd

    # ---- 输入 ----
    def tap(self, x: int, y: int, hold_ms: int | None = None):
        """在安卓坐标 (x, y) 处模拟鼠标点击(先置前窗口)"""
        sx, sy = self.to_screen(x, y)
        self.force_foreground()
        time.sleep(self.foreground_delay)
        user32.SetCursorPos(sx, sy)
        time.sleep(0.12)
        user32.mouse_event(0x0002, 0, 0, 0, 0)            # LEFTDOWN
        time.sleep((hold_ms or self.hold_ms) / 1000.0)
        user32.mouse_event(0x0004, 0, 0, 0, 0)            # LEFTUP
        time.sleep(0.3)

    def describe(self) -> str:
        t = self.info.get("title") or "(未知标题)"
        e = self.info.get("exe_name") or "?"
        return (f"hwnd=0x{self.hwnd:X} 标题「{t}」进程={e} "
                f"scale=({self.scale_x},{self.scale_y}) y_off={self.y_offset}")

    def __repr__(self):
        return (f"<WinMouse hwnd={self.hwnd:#x} scale=({self.scale_x},{self.scale_y}) "
                f"y_off={self.y_offset}>")
