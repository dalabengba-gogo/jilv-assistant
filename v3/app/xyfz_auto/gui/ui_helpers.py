# -*- coding: utf-8 -*-
"""主窗口共享小工具 (2026-10-18 v4.7.0, 批次四·工程 P2-1)

为什么要单独一个文件:
  拆 main_window.py 时, `_safe_call` / `_tail_int` / `_input_dialog` /
  `_PortLight` 这几个**模块级**工具会被主窗口自己和新拆出的若干 Mixin
  同时用到。若留在 main_window.py 里, Mixin 就得 `from gui.main_window
  import ...` —— 而 main_window 又要 `from gui.xxx_mixin import XxxMixin`,
  立刻形成循环导入(A→B→A), 导入即炸。

  抽到这里之后, main_window 与所有 Mixin 都是「叶子依赖」, 谁都不必
  import 谁, 拆分才立得住。

⚠️ main_window.py 里仍然 `from gui.ui_helpers import _safe_call, ...`,
  所以 `main_window._safe_call` 这种写法依旧可用 —— 回归脚本和历史调用
  点一个都不用改。这是抽离而非搬家: 变的是定义位置, 不是名字。
"""
from __future__ import annotations
import re as _re
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QLineEdit

def safe_call(fn, *args, **kwargs):
    """防御性包裹: 定时器回调 / 跨线程信号槽里的未捕获异常, 不再能拖垮整个 GUI。

    2026-10-16 加: 之前一旦某帧渲染或某次 OCR 结果处理抛异常, 在 pythonw
    下 stderr 被丢弃、且无 excepthook, 进程直接静默消失(本次 v3 闪退即此)。
    现在异常被记到 logs/crash.log, GUI 继续活。
    """
    try:
        return fn(*args, **kwargs)
    except Exception:
        try:
            import traceback as _tb
            from crash_guard import write_crash
            write_crash(f"UI CALL GUARDED: {getattr(fn, '__name__', fn)}",
                        _tb.format_exc())
        except Exception:
            pass

def tail_int(text: str) -> int:
    """从「完成但有13步失败」这类文案里取那个数字(取不到返回 0)

    2026-10-17 v3.10.0: 状态灯右侧只有 ~120px, 放不下整句, 只能取数字。
    """
    m = _re.findall(r"(\d+)\s*步", str(text or ""))
    return int(m[-1]) if m else 0

def input_dialog(parent, title, label, default=""):
    """简化输入框"""
    from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFormLayout
    d = QDialog(parent)
    d.setWindowTitle(title)
    lay = QFormLayout(d)
    ed = QLineEdit(default)
    lay.addRow(label, ed)
    bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    bb.accepted.connect(d.accept)
    bb.rejected.connect(d.reject)
    lay.addRow(bb)
    return (ed.text().strip(), True) if d.exec() else ("", False)

class PortLight(QLabel):
    """单个端口的状态灯(「多开管理」里每个账号一个)。点一下立即重连该端口。"""
    clicked = Signal(int)
    def __init__(self, port: int, name: str):
        super().__init__()
        self._port = int(port)
        self._name = name
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(18)
    def mousePressEvent(self, ev):
        self.clicked.emit(self._port)
        super().mousePressEvent(ev)
    def set_state(self, online: bool | None, detail: str = ""):
        """online=None 表示还没探测完(灰); True/False 表示结果

        状态存在实例上 —— 「导出诊断信息」要读当前端口状态写进诊断包。
        """
        self._online = online
        self._detail = detail or ""
        if online is True:
            dot, color = "●", "#2a6"          # 绿
        elif online is False:
            dot, color = "○", "#888"          # 灰
        else:
            dot, color = "◌", "#aa8"          # 待探测
        self.setText(f"{dot} {self._name} (:{self._port})")
        self.setStyleSheet(f"color:{color};")
        # 2026-10-17 v3.10.0: 右侧那列状态可能把灯挤窄, 完整名放进 tooltip
        tip = f"{self._name} (:{self._port})"
        if detail:
            tip = f"{tip}\n{detail}"
        self.setToolTip(tip)

# ---- 旧名兼容: main_window / 回归脚本里一直用的是带下划线的名字 ----
# 拆分时不想改动几十处调用点, 这里给出同义映射即可。**不要删**, 删了就
# 要把 main_window 里所有调用点都改一遍 —— 纯赚疲劳, 零收益。
_safe_call = safe_call
_tail_int = tail_int
_input_dialog = input_dialog
_PortLight = PortLight
