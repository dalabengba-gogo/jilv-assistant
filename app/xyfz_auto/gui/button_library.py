# -*- coding: utf-8 -*-
"""按钮库(ui_registry) → 脚本片段 的插取器  (2026-10-09 v1.9.0)

写脚本最费劲的一步是"这个按钮在注册表里到底叫什么、坐标在哪"。这里把
assets/ui_registry.yaml 里的界面(screens)与按钮(buttons)摊成一棵可搜索的树,
选一个 → 生成能直接粘进 steps 的 YAML 片段 → 双击插入光标处。

被两处复用:
  · 「新建脚本」向导(gui/new_script.py) —— 左按钮库 / 右脚本内容, 边新建边插
  · 主界面「脚本(YAML 可编辑)」页的「插入按钮/界面…」按钮

生成的片段一律按 steps 列表的缩进(默认 2 空格)写好, 并自带 on_fail 兜底 ——
脚本裸奔失败会终止整个日常任务(历史事故), 所以默认给的是"能直接跑"的写法。
"""
from __future__ import annotations

import json

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSplitter, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from engine import UiRegistry

# ---------------- 片段形式 ----------------
# 注意: "只动作"那种片段必须缩进 4 空格 —— YAML 里 `- name: X` 的映射从第 5 列
# 开始, 后续键若只缩进 2 会被当成"另一个映射"直接解析报错。所以它只能"接到某个
# 已有步骤里", 标签也写清楚了; 其余形式都是完整步骤(缩进 2), 可独立成一步。
FORMS_BUTTON = [
    ("完整步骤   + timeout/retry/on_fail 兜底 (推荐)", "step"),
    ("完整步骤 + 先等所在界面出现", "step_wait"),
    ("只动作   click: {button: X}  (接到已有步骤里, 缩进4空格)", "click"),
]
FORMS_SCREEN = [
    ("界面判定   wait: {ui: X}", "wait_ui"),
    ("自动导航   goto: {ui: X} (按 opens 推导路径)", "goto_ui"),
    ("等界面出现 + 停顿 1 秒 (衔接用)", "wait_pause"),
]

_SPECIAL = set(',:{}[]"\'#&*!|>%@` \t')


def _y(s: str) -> str:
    """YAML 标量安全输出: 含特殊字符/以特殊字符开头时加双引号"""
    if not s:
        return '""'
    if s[0] in "-?:,[]{}#&*!|>'\"%@`" or any(ch in _SPECIAL for ch in s):
        return json.dumps(s, ensure_ascii=False)
    return s


def make_snippet(form: str, name: str, screen: str = "",
                 ui: UiRegistry | None = None, indent: str = "  ") -> str:
    """按选中项生成脚本片段(YAML 文本, 已含列表缩进)"""
    q = _y(name)
    scr = _y(screen) if screen else ""
    if form == "click":
        # 接到已有步骤里 → 要多缩进一级(见 FORMS_BUTTON 上方说明)
        return f"{indent}  click: {{button: {q}}}\n"
    if form == "step":
        return (f"{indent}- name: 点击-{name}\n"
                f"{indent}  click: {{button: {q}}}\n"
                f"{indent}  timeout: 5\n"
                f"{indent}  retry: 1\n"
                f"{indent}  on_fail: continue\n")
    if form == "step_wait":
        head = ""
        if screen:
            head = (f"{indent}- name: 等{screen}出现\n"
                    f"{indent}  wait: {{ui: {scr}}}\n"
                    f"{indent}  timeout: 10\n"
                    f"{indent}  retry: 1\n"
                    f"{indent}  on_fail: continue\n\n")
        return head + (f"{indent}- name: 点击-{name}\n"
                       f"{indent}  click: {{button: {q}}}\n"
                       f"{indent}  timeout: 5\n"
                       f"{indent}  retry: 1\n"
                       f"{indent}  on_fail: continue\n")
    if form == "wait_ui":
        return (f"{indent}- name: 等{screen}出现\n"
                f"{indent}  wait: {{ui: {scr}}}\n"
                f"{indent}  timeout: 10\n"
                f"{indent}  retry: 1\n"
                f"{indent}  on_fail: continue\n")
    if form == "wait_pause":
        return ((f"{indent}- name: 等{screen}出现\n"
                 f"{indent}  wait: {{ui: {scr}}}\n"
                 f"{indent}  timeout: 10\n"
                 f"{indent}  retry: 1\n"
                 f"{indent}  on_fail: continue\n\n"
                 f"{indent}- name: 停顿1秒\n"
                 f"{indent}  sleep: 1\n") if screen else
                f"{indent}- name: 停顿1秒\n{indent}  sleep: 1\n")
    if form == "goto_ui":
        note = ""
        if ui is not None:
            try:
                plan = ui.navigation_plan(screen)
                note = (f"{indent}# 自动导航: " + " -> ".join(f"[{b}]" for b in plan)
                        if plan else f"{indent}# 当前位置就是[{screen}], goto 不需要点击")
            except Exception as e:
                note = f"{indent}# ⚠ 注册表里到[{screen}]没有 opens 路径, goto 会报错: {e}"
        return (note + "\n" if note else "") + \
            f"{indent}- name: 导航到{screen}\n{indent}  goto: {{ui: {scr}}}\n"
    return ""


class ButtonLibrary(QWidget):
    """按钮库面板: 搜索 + 界面/按钮树 + 片段形式 + snippet()

    双击一条 → 发 insertRequested(片段文本), 由外层插到光标处。
    """

    insertRequested = Signal(str)

    def __init__(self, ui: UiRegistry, parent=None):
        super().__init__(parent)
        self.ui = ui
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self.edit_search = QLineEdit()
        self.edit_search.setPlaceholderText("搜索界面/按钮(名字或说明)…")
        self.edit_search.textChanged.connect(self._apply_filter)
        lay.addWidget(self.edit_search)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["界面 / 按钮", "位置"])
        self.tree.setColumnWidth(0, 210)
        self.tree.setToolTip("双击一条 = 把对应脚本片段插到右边光标处")
        self.tree.itemSelectionChanged.connect(self._on_sel)
        self.tree.itemDoubleClicked.connect(self._on_dbl)
        lay.addWidget(self.tree, stretch=1)

        self.lbl_info = QLabel("")
        self.lbl_info.setStyleSheet("color:#888; font-size:11px;")
        self.lbl_info.setWordWrap(True)
        lay.addWidget(self.lbl_info)

        row = QHBoxLayout()
        row.addWidget(QLabel("插入形式:"))
        self.combo_form = QComboBox()
        self.combo_form.setMinimumWidth(260)
        row.addWidget(self.combo_form, stretch=1)
        lay.addLayout(row)

        self.btn_insert = QPushButton("插入片段 →")
        self.btn_insert.setToolTip("把当前选中的片段插到脚本内容的光标处(双击树里条目同效)")
        self.btn_insert.clicked.connect(
            lambda *_: self.insertRequested.emit(self.snippet()))
        lay.addWidget(self.btn_insert)

        self._build_tree()
        self._on_sel()

    # ---------- 树 ----------
    def _build_tree(self):
        self.tree.clear()
        btn_screens = {n: (b.get("screen") or "") for n, b in self.ui.buttons.items()}
        known = set(self.ui.screens)
        orphan = QTreeWidgetItem(["（未归属已注册界面的按钮）", ""])
        n_orphan = 0
        for scr in self.ui.screens:
            node = QTreeWidgetItem([scr, f"{(self.ui.screens[scr] or {}).get('desc','')[:24]}"])
            node.setData(0, Qt.UserRole, {"kind": "screen", "name": scr, "screen": scr})
            node.setToolTip(0, f"界面: {scr}\n"
                               f"{(self.ui.screens.get(scr) or {}).get('desc','')}\n"
                               f"判定: {(self.ui.screens.get(scr) or {}).get('any_of')}")
            for n in self.ui.names(scr):
                node.addChild(self._btn_item(n, scr))
            self.tree.addTopLevelItem(node)
        for n, scr in btn_screens.items():
            if scr and scr in known:
                continue
            orphan.addChild(self._btn_item(n, scr))
            n_orphan += 1
        if n_orphan:
            self.tree.addTopLevelItem(orphan)
        for i in range(self.tree.topLevelItemCount()):
            self.tree.topLevelItem(i).setExpanded(True)

    def _btn_item(self, name: str, screen: str) -> QTreeWidgetItem:
        b = self.ui.get(name) or {}
        pos = f"({b['at'][0]},{b['at'][1]})" if b.get("at") else \
              (f"模板:{b['template']}" if b.get("template") else "-")
        it = QTreeWidgetItem([name, pos])
        it.setData(0, Qt.UserRole, {"kind": "button", "name": name, "screen": screen})
        tip = [f"按钮: {name}", f"界面: {screen or '(未填)'}",
               f"说明: {b.get('desc','')}", f"定位: {pos}"]
        if b.get("opens"):
            tip.append(f"点击后进入: {b['opens']}")
        if b.get("dynamic"):
            tip.append("注意: 动态内容, 模板判断不可靠")
        it.setToolTip(0, "\n".join(tip))
        return it

    def _payload(self) -> dict | None:
        it = self.tree.currentItem()
        return it.data(0, Qt.UserRole) if it is not None else None

    def _on_sel(self):
        p = self._payload()
        self.combo_form.clear()
        if not p:
            self.lbl_info.setText("在上面选一个界面或按钮")
            self.btn_insert.setEnabled(False)
            return
        forms = FORMS_BUTTON if p["kind"] == "button" else FORMS_SCREEN
        for label, val in forms:
            self.combo_form.addItem(label, val)
        self.btn_insert.setEnabled(True)
        b = self.ui.get(p["name"]) if p["kind"] == "button" else None
        if p["kind"] == "button":
            scr_name = self._safe_screen(p["screen"])
            self.lbl_info.setText(
                f"{p['name']} · 界面 {scr_name or '(未归属已注册界面)'}"
                f"{' · opens ' + b['opens'] if b and b.get('opens') else ''}"
                f"\n{(b or {}).get('desc','')}")
        else:
            n = len(self.ui.names(p["name"]))
            scr = self.ui.screens.get(p["name"]) or {}
            self.lbl_info.setText(f"界面 {p['name']} · {n} 个按钮\n{scr.get('desc','')}")

    def _on_dbl(self, _item, _col=0):
        if self.btn_insert.isEnabled():
            self.insertRequested.emit(self.snippet())

    def _apply_filter(self, text: str):
        kw = (text or "").strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            hit_self = kw in node.text(0).lower() or kw in node.text(1).lower()
            shown = 0
            for j in range(node.childCount()):
                ch = node.child(j)
                tip = (ch.toolTip(0) or "")
                ok = (not kw) or hit_self or kw in ch.text(0).lower() \
                    or kw in ch.text(1).lower() or kw in tip.lower()
                ch.setHidden(not ok)
                shown += 1 if ok else 0
            node.setHidden(bool(kw) and not hit_self and shown == 0)
            if kw and shown:
                node.setExpanded(True)

    def _safe_screen(self, name: str) -> str:
        """只把**已注册界面**当界面用 —— 杂项(如 screen: 通用)不能生成 wait:{ui:}"""
        return name if name in self.ui.screens else ""

    def snippet(self) -> str:
        p = self._payload()
        if not p:
            return ""
        return make_snippet(self.combo_form.currentData(), p["name"],
                            self._safe_screen(p.get("screen", "")), self.ui)


class ButtonLibraryDialog(QDialog):
    """弹窗版: 选一条 → 「插入」返回片段文本(给主界面「脚本」页用)"""

    def __init__(self, ui: UiRegistry, parent=None, target: str = ""):
        super().__init__(parent)
        self.setWindowTitle("插入按钮/界面片段")
        self.resize(880, 560)
        lay = QVBoxLayout(self)
        tip = QLabel("双击左侧条目 → 片段插入到中间(可再改); 点「插入到脚本」把它带回编辑器光标处。\n"
                     f"目标文件: {target}")
        tip.setStyleSheet("color:#666; font-size:11px;")
        lay.addWidget(tip)

        split = QSplitter(Qt.Horizontal)
        self.lib = ButtonLibrary(ui)
        self.lib.insertRequested.connect(self._append_preview)
        split.addWidget(self.lib)
        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        rv.addWidget(QLabel("片段预览(可编辑):"))
        self.preview = QPlainTextEdit()
        self.preview.setPlaceholderText("左边双击一条, 片段会出现在这里…")
        rv.addWidget(self.preview, stretch=1)
        split.addWidget(right)
        split.setSizes([420, 440])
        lay.addWidget(split, stretch=1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("插入到脚本")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def _append_preview(self, text: str):
        cur = self.preview.toPlainText()
        sep = "" if (not cur or cur.endswith("\n")) else "\n"
        self.preview.setPlainText(cur + sep + text)
        self.preview.moveCursor(self.preview.textCursor().End)

    def snippet(self) -> str:
        return self.preview.toPlainText()
