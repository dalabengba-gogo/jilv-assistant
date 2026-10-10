# -*- coding: utf-8 -*-
"""「脚本选项」设置页 —— 左侧选脚本, 右侧只显示该脚本的选项 (ALAS 风格)

2026-10-07 v1.6.6 改造: 选项与**具体脚本**绑定。
  · 左侧列表 = 当前任务里的所有脚本(按 include 顺序) + 「运行(全局)」;
    有可调选项的脚本前标 ●, 没有的标 ○。
  · 选中某个脚本, 右侧**只显示这个脚本自己的选项** —— 以后新增脚本,
    在它的 yaml 里加 options: 段(可显式写 script: 指向归属脚本), 界面自动
    多出一项, 不用改 GUI 代码。详见 engine/options.py。
  · 「运行(全局)」是代码里固定的(引擎/通道这类与脚本无关的设置)。
  · 改动即时写入 config.yaml(按节定位原位替换, 保留注释); 下轮任务生效。
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.yaml"
TASKS_DIR = PROJECT_ROOT / "assets" / "tasks"

# 与具体脚本无关的全局设置(代码里固定; 脚本自己的选项请写进脚本的 options: 段)
GLOBAL_GROUP = {
    "script": "(全局)",
    "title": "运行(全局)",
    "desc": "引擎与点击通道设置, 与具体脚本无关",
    "items": [
        {
            "key": "max_sleep_seconds", "section": "engine", "type": "int",
            "label": "固定停顿上限(秒)", "default": 0, "min": 0, "max": 600,
            "help": "脚本里 sleep 的全局上限, 0 = 不限制。\n"
                    "想整体压缩干等就填个数, 不用改每个脚本。\n"
                    "别压安全窗口: 组队「安静确认」4×60s、地煞「等12秒」。",
        },
        {
            "key": "enabled", "section": "win_input", "type": "bool",
            "label": "启用 Windows 鼠标通道", "default": True,
            "help": "脚本里 via: win 的步骤用它绕开游戏反注入(师门选师傅兜底等)。\n"
                    "关掉 = 全部改用 adb 点击; 换电脑后点不动就先来这关一下试试。",
        },
    ],
}

_SECTION_RE = None


def _load_config_text() -> str:
    return CONFIG_PATH.read_text(encoding="utf-8")


def _fmt_value(value) -> str:
    """按 YAML 类型写法输出: bool 小写裸值 / 数字裸值 / 其它加引号"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"%s"' % str(value).replace('"', '\\"')


def _split_comment(old: str) -> tuple[str, str]:
    """拆出 "值 # 注释" —— 保留行尾注释(重写键值时不丢注释)"""
    in_q = False
    for i, ch in enumerate(old):
        if ch == '"':
            in_q = not in_q
        elif ch == "#" and not in_q:
            return old[:i].strip(), "  " + old[i:].strip()
    return old.strip(), ""


def _set_config_key(text: str, key: str, value, section: str) -> str:
    """改 config.yaml 指定**节**里的键(原位替换保注释; 节/键不存在则追加)

    注意: 必须按节定位 —— 只按键名全局搜会改错节(如 win_input.enabled 与
    将来别的节的 enabled)。
    """
    import re
    global _SECTION_RE
    if _SECTION_RE is None:
        _SECTION_RE = re.compile(r"^([A-Za-z0-9_]+):[ \t]*(?:#.*)?$")

    lines = text.splitlines(keepends=True)

    # 1) 定位目标节(顶层键, 行首无缩进)
    head = None
    for i, ln in enumerate(lines):
        m = _SECTION_RE.match(ln.rstrip("\n"))
        if m and m.group(1) == section:
            head = i
            break
    if head is None:                                  # 整节都没有 → 追加
        tail = "" if (text == "" or text.endswith("\n\n")) else (
            "\n" if text.endswith("\n") else "\n\n")
        return text + f"{tail}{section}:\n  {key}: {_fmt_value(value)}\n"

    # 2) 节的结束位置 = 下一个顶层键
    end = len(lines)
    for i in range(head + 1, len(lines)):
        if _SECTION_RE.match(lines[i].rstrip("\n")):
            end = i
            break

    # 3) 节内找键(必须带缩进)
    kpat = re.compile(rf"^([ \t]+){re.escape(key)}:[ \t]*(.*)$")
    for i in range(head + 1, end):
        m = kpat.match(lines[i].rstrip("\n"))
        if m:
            _old, comment = _split_comment(m.group(2))
            lines[i] = f"{m.group(1)}{key}: {_fmt_value(value)}{comment}\n"
            return "".join(lines)

    # 4) 键不存在 → 插到节尾(跳过尾部空行)
    ins = end
    while ins - 1 > head and lines[ins - 1].strip() == "":
        ins -= 1
    lines.insert(ins, f"  {key}: {_fmt_value(value)}\n")
    return "".join(lines)


def _get_cfg_value(cfg: dict, section: str, key: str, default=None):
    sec = cfg.get(section)
    if isinstance(sec, dict) and key in sec:
        return sec[key]
    return default


def _clear_layout(layout):
    while layout.count():
        it = layout.takeAt(0)
        w = it.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()


class SettingsPage(QFrame):
    """脚本选项: 左侧选脚本 → 右侧该脚本的选项表单(改动即时写 config.yaml)"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self._loading = False
        self._items: list[dict] = []      # 当前显示的选项 [{spec, get, set, widget}]
        self._groups: list[dict] = []
        self._task_path: str | None = None

        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)

        # ---- 顶栏: 扫描对象 + 重新扫描 ----
        top = QHBoxLayout()
        self.lbl_task = QLabel("脚本选项")
        self.lbl_task.setStyleSheet("font-weight:bold;")
        top.addWidget(self.lbl_task, stretch=1)
        btn_rescan = QPushButton("重新扫描脚本")
        btn_rescan.setToolTip("外部改动脚本(新增脚本/改选项声明)后点这里, 不用重启 GUI")
        btn_rescan.clicked.connect(self._rescan)
        top.addWidget(btn_rescan)
        v.addLayout(top)

        # ---- 主体: 左选脚本 / 右看选项 ----
        body = QHBoxLayout()
        body.setSpacing(8)

        left = QVBoxLayout()
        left.addWidget(QLabel("脚本:"))
        self.list_scripts = QListWidget()
        self.list_scripts.setMaximumWidth(190)
        self.list_scripts.setMinimumWidth(120)
        self.list_scripts.currentRowChanged.connect(self._on_script_changed)
        left.addWidget(self.list_scripts, stretch=1)
        body.addLayout(left)

        right = QVBoxLayout()
        self.lbl_group = QLabel("")
        self.lbl_group.setStyleSheet("font-weight:bold;")
        self.lbl_group.setWordWrap(True)
        right.addWidget(self.lbl_group)
        self.lbl_src = QLabel("")
        self.lbl_src.setStyleSheet("color:#888; font-size:11px;")
        self.lbl_src.setWordWrap(True)
        right.addWidget(self.lbl_src)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        right.addWidget(self.scroll, stretch=1)
        body.addLayout(right, stretch=1)
        v.addLayout(body, stretch=1)

        self.hint = QLabel(
            "改动立即写入 config.yaml, 下轮任务生效。\n"
            "选项由各脚本自己的 options: 段声明(可写 script: 指明归属脚本) —— "
            "新增脚本时在它的 yaml 里加一段, 左侧列表就会多出这一项。")
        self.hint.setStyleSheet("color:#888; font-size:11px;")
        self.hint.setWordWrap(True)
        v.addWidget(self.hint)

        self.set_task(None)

    # ================= 扫描 & 列表 =================
    def set_task(self, path):
        """切换任务时调用: 重扫该任务(含 include 的子脚本)的选项声明并重建列表"""
        self._task_path = str(path) if path else None
        self._rebuild()

    def _rescan(self):
        self._rebuild()

    def _collect_groups(self) -> list[dict]:
        groups: list[dict] = [GLOBAL_GROUP]
        if self._task_path:
            try:
                from engine.options import collect_options
                groups.extend(collect_options(self._task_path, TASKS_DIR))
            except Exception as e:
                print(f"[settings] 扫描脚本选项失败: {e}", file=sys.stderr)
        return groups

    def _current_key(self) -> str | None:
        row = self.list_scripts.currentRow()
        if 0 <= row < len(self._groups):
            return self._groups[row]["script"]
        return None

    def _rebuild(self):
        prev = self._current_key()
        self._groups = self._collect_groups()

        self._loading = True
        try:
            self.list_scripts.clear()
            for g in self._groups:
                n = len(g["items"])
                item = QListWidgetItem(("● " if n else "○ ") + str(g["title"]))
                tip = f"{g['title']}\n脚本: {g['script']}\n可调选项: {n} 项"
                item.setToolTip(tip)
                if n:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                self.list_scripts.addItem(item)
            self.lbl_task.setText(
                f"脚本选项 —— {Path(self._task_path).name}" if self._task_path
                else "脚本选项 (未选任务)")
        finally:
            self._loading = False

        # 恢复上次选中的脚本; 上次停在「全局」或找不到时, 选第一个"有选项"的脚本,
        # 再退到第一项 —— 打开这一页默认看到的是脚本选项, 不是全局设置。
        prev = prev if prev != GLOBAL_GROUP["script"] else None
        row = next((i for i, g in enumerate(self._groups) if g["script"] == prev), None)
        if row is None:
            row = next((i for i, g in enumerate(self._groups) if g["items"] and i > 0),
                       next((i for i, g in enumerate(self._groups) if g["items"]), 0))
        self.list_scripts.setCurrentRow(row)      # 触发 _on_script_changed
        if not self._groups:
            self._show_group(None)

    # ================= 右侧表单 =================
    def _on_script_changed(self, row: int):
        if self._loading:
            return
        if not (0 <= row < len(self._groups)):
            self._show_group(None)
            return
        self._show_group(row)

    def _show_group(self, row: int | None):
        self._loading = True
        try:
            self._items = []
            if row is None:
                self.lbl_group.setText("")
                self.lbl_src.setText("没有可显示的内容")
                self.scroll.setWidget(QWidget())
                return
            g = self._groups[row]
            is_global = g["script"] == "(全局)"
            self.lbl_group.setText(f"{g['title']}" if is_global
                                   else f"{g['title']}   [{g['script']}]")
            parts = []
            if g.get("desc"):
                parts.append(g["desc"])
            parts.append("本页设置与具体脚本无关" if is_global
                         else f"来自脚本文件: {g.get('path') or g['script']}")
            self.lbl_src.setText("\n".join(parts))

            body = QWidget()
            bl = QVBoxLayout(body)
            bl.setContentsMargins(0, 0, 0, 0)
            if g["items"]:
                box = QGroupBox(g["title"])
                form = QFormLayout(box)
                for spec in g["items"]:
                    form.addRow(QLabel(f"{spec['label']}:"), self._build_widget(spec))
                    if spec.get("help"):
                        h = QLabel(spec["help"])
                        h.setStyleSheet("color:#888; font-size:11px;")
                        h.setWordWrap(True)
                        form.addRow(h)
                bl.addWidget(box)
            else:
                tip = QLabel(
                    f"「{g['title']}」暂无可调选项。\n"
                    "需要加选项就在该脚本(.yaml)里加一段 options:, 例如:\n"
                    "  options:\n"
                    "    title: 我的玩法\n"
                    "    items:\n"
                    "      - {key: mode, section: my_task, type: choice,\n"
                    "         default: \"fast\", choices: [{text: 快速, value: fast}]}")
                tip.setStyleSheet("color:#888; font-size:11px;")
                tip.setWordWrap(True)
                bl.addWidget(tip)
            bl.addStretch(1)
            self.scroll.setWidget(body)
            self.scroll.verticalScrollBar().setValue(0)
        finally:
            self._loading = False
        self.reload()

    def _build_widget(self, spec: dict) -> QWidget:
        typ = spec["type"]
        rec = {"spec": spec}
        if typ == "choice":
            w = QComboBox()
            for c in spec["choices"]:
                w.addItem(str(c["text"]), c["value"])
            rec["get"] = lambda w=w: w.currentData()
            rec["set"] = lambda v, w=w: w.setCurrentIndex(
                max(0, next((i for i in range(w.count())
                             if str(w.itemData(i)) == str(v)), 0)))
            w.currentIndexChanged.connect(lambda _i, s=spec: self._on_changed(s))
        elif typ == "bool":
            w = QCheckBox("启用")
            rec["get"] = lambda w=w: w.isChecked()
            rec["set"] = lambda v, w=w: w.setChecked(bool(v))
            w.toggled.connect(lambda _c, s=spec: self._on_changed(s))
        else:  # int
            w = QSpinBox()
            w.setRange(int(spec.get("min", 0)), int(spec.get("max", 9999)))
            w.setSingleStep(int(spec.get("step", 1)))
            if spec.get("unit"):
                w.setSuffix(" " + spec["unit"])
            rec["get"] = lambda w=w: w.value()
            rec["set"] = lambda v, w=w: w.setValue(int(v))
            w.valueChanged.connect(lambda _v, s=spec: self._on_changed(s))
        rec["widget"] = w
        self._items.append(rec)
        return w

    # ================= 读写 =================
    def reload(self):
        """从 config.yaml 读当前值刷到控件(缺键用脚本声明的 default)"""
        self._loading = True
        try:
            cfg = yaml.safe_load(_load_config_text()) or {}
            for rec in self._items:
                s = rec["spec"]
                val = _get_cfg_value(cfg, s["section"], s["key"], s.get("default"))
                if s["type"] == "choice" and val is None:
                    val = s.get("default")
                rec["set"](val)
        finally:
            self._loading = False

    def _on_changed(self, spec: dict):
        if self._loading:
            return
        rec = next((r for r in self._items if r["spec"] is spec), None)
        if rec is None:
            return
        value = rec["get"]()
        try:
            text = _set_config_key(_load_config_text(), spec["key"], value,
                                   spec["section"])
            CONFIG_PATH.write_text(text, encoding="utf-8")
        except Exception as e:               # 写失败弹窗提示, 不静默
            QMessageBox.warning(self, "脚本选项", f"写入 config.yaml 失败:\n{e}")
            return
        print(f"[settings] {spec['section']}.{spec['key']} -> {value!r}",
              file=sys.stderr)
