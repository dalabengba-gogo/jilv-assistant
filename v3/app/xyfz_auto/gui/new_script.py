# -*- coding: utf-8 -*-
"""新建脚本向导  (2026-10-09 v1.9.0)

背景(为什么要有这个文件):
    旧版 GUI 只有一个「打开/新建脚本…」按钮, 走的是 QFileDialog 的**打开**
    对话框 —— 在里面敲一个新文件名会被当成"打开一个不存在的文件", 编辑器读不到
    内容直接弹"读取失败", 所以用户根本找不到"新建脚本"这条路。

现在的流程:
    脚本名 → 选位置(顶层任务 / parts 子脚本) → 选起点模板 → 左按钮库右编辑器
    边写边插 → 勾选"挂进当前任务"后自动把 include 追加到总脚本(不追加的话,
    新建的 parts 脚本不会出现在两页的「脚本」下拉里 —— 下拉列的是**被当前任务
    include 进来的**子脚本)。

`append_include()` 只做**文本追加**, 不做 yaml round-trip —— 那会把总脚本里的
全部注释冲掉, 而这些注释是踩坑记录。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QSplitter, QVBoxLayout,
    QWidget,
)

from engine import UiRegistry

from gui.button_library import ButtonLibrary

KIND_TOP = "top"
KIND_PART = "part"
_BAD_CHARS = '\\/:*?"<>|'


# ---------------- 纯逻辑(可单测, 不依赖 Qt) ----------------
def validate_script_name(name: str) -> str | None:
    """检查脚本文件名: 返回错误说明, 合法返回 None"""
    s = (name or "").strip()
    if not s:
        return "脚本名不能为空"
    bad = [c for c in s if c in _BAD_CHARS]
    if bad:
        return f"文件名不能含 {' '.join(bad)}"
    if s in (".", ".."):
        return "脚本名不合法"
    if s.endswith("."):
        return "脚本名不能以点号结尾"
    return None


def script_file_name(name: str) -> str:
    """统一成 xxx.yaml(用户写了 .yml/.yaml 也不重复加)"""
    s = (name or "").strip()
    low = s.lower()
    for suf in (".yaml", ".yml"):
        if low.endswith(suf):
            return s[: -len(suf)] + ".yaml"
    return s + ".yaml"


def _screens_hint(ui: UiRegistry | None) -> str:
    if ui is None:
        return ""
    try:
        return "、".join(ui.screens.keys())
    except Exception:
        return ""


def build_skeleton(name: str, kind: str = KIND_TOP,
                   ui: UiRegistry | None = None) -> str:
    """起点模板: 顶层任务 / parts 子脚本(带注释、示例与兜底写法)"""
    scr = _screens_hint(ui)
    head = (f"# -*- coding: utf-8 -*-\n"
            f"# ============================================================\n"
            f"# {name}\n"
            f"# ------------------------------------------------------------\n")
    if kind == KIND_PART:
        head += (
            "# 子脚本(assets/tasks/parts/): 被总脚本 include 进来执行。\n"
            "# 约定(和现有 parts/*.yaml 一致, 才能安全拼接):\n"
            "#   · 开头「确认在主界面」, 结尾「等回主界面」—— 与前后脚本衔接不串画面\n"
            "#   · 每个可能失败的步骤都要 on_fail 兜底(重开面板/重试/收尾),\n"
            "#     子脚本里裸奔失败会终止整个日常任务\n"
            "#   · 玩法面板进入套路: 点(506,539)开面板 → (95,934)切「日常」页签 →\n"
            "#     列表下滑两次回顶 → OCR 点卡片 → 等「任务说明」介绍页 → 点前往(241,782)\n")
    else:
        head += (
            "# 顶层任务(assets/tasks/): 左边「任务脚本」下拉里选中它即可运行。\n"
            "# steps 里既能直接写步骤, 也能 include 子脚本:\n"
            "#   - name: \"①我的玩法\"\n"
            "#     include: parts/daily_xxx.yaml\n")
    head += (
        f"# ------------------------------------------------------------\n"
        f"# 常用写法(每步只写一个动作键, 不能混写):\n"
        f"#   - name: 确认在主界面\n"
        f"#     wait: {{ui: 主界面}}            # 界面判定\n"
        f"#   - name: 点按钮\n"
        f"#     click: {{button: 玩法}}         # 注册表按钮名(推荐, 换分辨率不用改)\n"
        f"#   - name: 点固定坐标\n"
        f"#     click: [506, 539]              # 540x960 绝对坐标\n"
        f"#   - name: OCR 找字点它\n"
        f"#     click: {{ocr: 确认领取}}\n"
        f"#   - name: 等某文字\n"
        f"#     wait: {{ocr: 恭喜完成}}\n"
        f"#     timeout: 60\n"
        f"#   - name: 停顿 / 滑动\n"
        f"#     sleep: 2\n"
        f"#     swipe: {{from: [270, 700], to: [270, 250], duration: 300}}\n"
        f"#   - name: 分支跳转\n"
        f"#     on_fail: {{goto: 我的恢复块}}   # 名字带逗号要加引号\n")
    if scr:
        head += f"#\n# 可用的界面判定名: {scr}\n"
    head += "# ============================================================\n"

    body = f"name: {name}\n"
    if kind == KIND_PART:
        steps = (
            "steps:\n"
            "  - name: 确认在主界面\n"
            "    wait: {ui: 主界面}\n"
            "    timeout: 15\n"
            "\n"
            "  - name: 打开玩法面板\n"
            "    click: [506, 539]\n"
            "    on_fail: continue\n"
            "\n"
            "  - name: 等玩法面板出现\n"
            "    wait: {ui: 玩法面板}\n"
            "    timeout: 10\n"
            "    retry: 1\n"
            "    on_fail: continue\n"
            "\n"
            "  - name: 点日常页签(确保在正确页签)\n"
            "    click: [95, 934]\n"
            "    on_fail: continue\n"
            "\n"
            "  - name: 玩法列表下拉回顶(1/2)\n"
            "    swipe: {from: [270, 320], to: [270, 860], duration: 200}\n"
            "\n"
            "  - name: 停顿1秒\n"
            "    sleep: 1\n"
            "\n"
            "  - name: 玩法列表下拉回顶(2/2)\n"
            "    swipe: {from: [270, 320], to: [270, 860], duration: 200}\n"
            "\n"
            "  - name: 停顿1秒\n"
            "    sleep: 1\n"
            "\n"
            "  # ↓↓↓ 在这里写你的玩法步骤(上面「按钮库」双击可直接插片段)↓↓↓\n"
            "  - name: 示例-OCR 点卡片(改成你要点的)\n"
            "    click: {ocr: 卡片上的字}\n"
            "    timeout: 8\n"
            "    retry: 1\n"
            "    on_fail: {goto: 我的脚本结束}\n"
            "\n"
            "  - name: 我的脚本结束\n"
            "    sleep: 1\n"
            "\n"
            "  - name: 等回主界面\n"
            "    wait: {ui: 主界面}\n"
            "    timeout: 15\n")
    else:
        steps = (
            "settings:\n"
            "  default_timeout: 30\n"
            "  default_retry: 2\n"
            "\n"
            "steps:\n"
            "  - name: 确认在主界面\n"
            "    wait: {ui: 主界面}\n"
            "    timeout: 15\n"
            "\n"
            "  # ↓↓↓ 在这里写步骤(上面「按钮库」双击可直接插片段)↓↓↓\n"
            "  - name: 示例-打开玩法面板\n"
            "    click: [506, 539]\n"
            "    on_fail: continue\n"
            "\n"
            "  - name: 示例-等玩法面板出现\n"
            "    wait: {ui: 玩法面板}\n"
            "    timeout: 10\n"
            "    retry: 1\n"
            "    on_fail: continue\n"
            "\n"
            "  - name: 脚本结束\n"
            "    sleep: 1\n")
    return head + "\n" + body + "\n" + steps


def build_blank(name: str) -> str:
    """空白脚本: 只有能跑起来的最小骨架"""
    return (f"# -*- coding: utf-8 -*-\n"
            f"name: {name}\n"
            f"steps:\n"
            f"  - name: 确认在主界面\n"
            f"    wait: {{ui: 主界面}}\n"
            f"    timeout: 15\n"
            f"\n"
            f"  - name: 第一步\n"
            f"    sleep: 1\n")


def append_include(text: str, rel: str, label: str = "") -> str:
    """把 `- name: Xxx` + `include: parts/xxx.yaml` 追加到任务的 steps 末尾

    只用文本插入(不用 yaml round-trip —— 那会把总脚本注释全冲掉):
      找到顶层 `steps:` 行 → 找 steps 块的结尾(之后第一行顶格内容) →
      按块内首个条目的缩进补一条, 末尾保留一个空行。
    steps 不是最后一个键、文件末尾带空行等情况都能正确处理。
    """
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.strip() == "steps:" or ln.startswith("steps:")), None)
    if start is None:
        return text.rstrip("\n") + \
            f"\n\nsteps:\n  - name: {label or Path(rel).stem}\n    include: {rel}\n"

    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].strip() and not lines[j][:1].isspace():
            end = j                      # steps 块到下一个顶格内容前结束
            break
    indent = "  "
    for j in range(start + 1, end):
        s = lines[j]
        if s.lstrip().startswith("-"):
            indent = s[: len(s) - len(s.lstrip())]
            break
    while end - 1 > start and not lines[end - 1].strip():
        end -= 1                          # 块末空白行先去掉, 再补一个空行
    block = [f"{indent}- name: {label or Path(rel).stem}",
             f"{indent}  include: {rel}"]
    out = lines[:end] + [""] + block + lines[end:]
    return "\n".join(out).rstrip("\n") + "\n"


def insert_include_into_task(task_path: str | Path, rel: str, label: str = "") -> bool:
    """把 include 追加进任务文件(调 append_include 落盘); 已在则不重复加"""
    p = Path(task_path)
    text = p.read_text(encoding="utf-8")
    if f"include: {rel}" in text:
        return False
    p.write_text(append_include(text, rel, label), encoding="utf-8")
    return True


# ---------------- 对话框 ----------------
class NewScriptDialog(QDialog):
    """新建脚本: 名字 + 位置 + 起点模板 + 按钮库(左) / 脚本内容(右)"""

    def __init__(self, task_dir: Path, ui: UiRegistry | None = None, parent=None,
                 current_task: str | None = None, copy_text: str = ""):
        super().__init__(parent)
        self.setWindowTitle("新建脚本")
        self.resize(940, 640)
        self._task_dir = Path(task_dir)
        self.ui_reg = ui
        self._current_task = current_task
        self._copy_text = copy_text or ""
        self._dirty = False               # 用户手改过内容 → 不再自动重填模板

        lay = QVBoxLayout(self)

        form = QFormLayout()
        self.edit_name = QLineEdit("我的新玩法")
        self.edit_name.setPlaceholderText("例如: 我的新玩法(将生成 my_xxx.yaml)")
        self.edit_name.textChanged.connect(self._maybe_refresh)
        form.addRow("脚本名:", self.edit_name)

        self.combo_kind = QComboBox()
        self.combo_kind.addItem("顶层任务   assets/tasks/ (可单独运行/被定时任务选用)", KIND_TOP)
        self.combo_kind.addItem("子脚本     assets/tasks/parts/ (被总脚本 include)", KIND_PART)
        self.combo_kind.currentIndexChanged.connect(self._on_kind_changed)
        form.addRow("存放位置:", self.combo_kind)

        self.combo_tpl = QComboBox()
        self.combo_tpl.addItem("骨架模板(带注释与示例, 推荐)", "skeleton")
        self.combo_tpl.addItem("空白脚本(最小可跑)", "blank")
        self.combo_tpl.addItem("复制当前编辑器里的脚本", "copy")
        self.combo_tpl.currentIndexChanged.connect(self._on_tpl_changed)
        form.addRow("起点模板:", self.combo_tpl)

        self.chk_mount = QCheckBox("同时把 include 追加到当前任务(这样两页「脚本」下拉里立刻能看到它)")
        self.chk_mount.setChecked(True)
        self.chk_mount.setEnabled(bool(current_task))
        if not current_task:
            self.chk_mount.setText("(没有选中的任务脚本, 暂不能自动挂载)")
        form.addRow("", self.chk_mount)
        lay.addLayout(form)

        split = QSplitter(Qt.Horizontal)
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        title = QLabel("按钮库(双击 → 插到右边光标处)")
        title.setStyleSheet("font-size:11px; color:#666;")
        lv.addWidget(title)
        self.lib = None
        if ui is not None:
            self.lib = ButtonLibrary(ui)
            self.lib.insertRequested.connect(self._insert_snippet)
            lv.addWidget(self.lib, stretch=1)
        else:
            lv.addWidget(QLabel("(按钮库不可用: ui_registry.yaml 未加载)"))
        split.addWidget(left)

        right = QWidget()
        rv = QVBoxLayout(right)
        rv.setContentsMargins(0, 0, 0, 0)
        self.lbl_target = QLabel("")
        self.lbl_target.setStyleSheet("font-size:11px; color:#666;")
        self.lbl_target.setWordWrap(True)
        rv.addWidget(self.lbl_target)
        self.editor = QPlainTextEdit()
        self.editor.textChanged.connect(self._on_edit_changed)
        rv.addWidget(self.editor, stretch=1)
        split.addWidget(right)
        split.setSizes([380, 540])
        lay.addWidget(split, stretch=1)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("创建脚本")
        bb.accepted.connect(self._on_ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self._loading = True
        self._refresh()
        self._loading = False
        self._dirty = False

    # ---------- 状态 ----------
    def kind(self) -> str:
        return str(self.combo_kind.currentData())

    def target_dir(self) -> Path:
        return self._task_dir if self.kind() == KIND_TOP else (self._task_dir / "parts")

    def target_path(self) -> Path | None:
        err = validate_script_name(self.edit_name.text())
        if err:
            return None
        return self.target_dir() / script_file_name(self.edit_name.text())

    def rel(self) -> str:
        """相对任务目录的路径(子脚本形如 parts/xxx.yaml)"""
        p = self.target_path()
        if p is None:
            return ""
        try:
            return str(p.relative_to(self._task_dir)).replace("\\", "/")
        except Exception:
            return p.name

    def content(self) -> str:
        return self.editor.toPlainText()

    def mount_requested(self) -> bool:
        return bool(self.chk_mount.isChecked() and self._current_task)

    # ---------- 内容生成 ----------
    def _build(self) -> str:
        name = (self.edit_name.text() or "").strip() or "新脚本"
        tpl = str(self.combo_tpl.currentData())
        if tpl == "copy" and self._copy_text.strip():
            return self._copy_text
        if tpl == "blank":
            return build_blank(name)
        return build_skeleton(name, self.kind(), self.ui_reg)

    def _refresh(self):
        self.editor.setPlainText(self._build())
        self._dirty = False
        self._update_target_label()

    def _maybe_refresh(self):
        """名字/位置变了: 只有内容没被手改过才重填(免得冲掉用户编辑)"""
        if not getattr(self, "_loading", False) and not self._dirty:
            self._loading = True
            try:
                self._refresh()
            finally:
                self._loading = False
        else:
            self._update_target_label()

    def _on_kind_changed(self):
        self._maybe_refresh()

    def _on_tpl_changed(self):
        # 换模板 = 用户明确要重填
        self._loading = True
        try:
            self._refresh()
        finally:
            self._loading = False

    def _on_edit_changed(self):
        if not getattr(self, "_loading", False):
            self._dirty = True

    def _insert_snippet(self, text: str):
        if not text:
            return
        cur = self.editor.toPlainText()
        sep = "" if (not cur or cur.endswith("\n")) else "\n"
        self.editor.setPlainText(cur + sep + text)
        self.editor.moveCursor(self.editor.textCursor().End)
        self.editor.setFocus()

    def _update_target_label(self):
        p = self.target_path()
        if p is None:
            self.lbl_target.setText("⚠ " + (validate_script_name(self.edit_name.text()) or ""))
            self.lbl_target.setStyleSheet("font-size:11px; color:#c00;")
            return
        try:
            shown = str(p.relative_to(self._task_dir)).replace("\\", "/")
        except Exception:
            shown = str(p)
        exists = p.exists()
        self.lbl_target.setText(
            f"将创建: {shown}" + ("   ⚠ 同名文件已存在, 创建会覆盖它"
                                  if exists else "")
            + (f"\n挂载到: {Path(self._current_task).name}" if self.mount_requested() else ""))
        self.lbl_target.setStyleSheet(
            "font-size:11px; color:%s;" % ("#c00" if exists else "#666"))

    # ---------- 确定 ----------
    def _on_ok(self):
        err = validate_script_name(self.edit_name.text())
        if err:
            QMessageBox.warning(self, "脚本名不对", err)
            return
        p = self.target_path()
        if p.exists() and QMessageBox.question(
                self, "已存在",
                f"{p.name} 已存在, 要覆盖它吗?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            import yaml
            data = yaml.safe_load(self.editor.toPlainText())
            assert isinstance(data, dict) and "steps" in data, "缺少 steps 字段"
        except Exception as e:
            if QMessageBox.question(
                    self, "脚本还没写好",
                    f"内容现在解析不了(YAML 报错):\n{e}\n\n仍然创建(之后在编辑器里接着改)?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes) != QMessageBox.Yes:
                return
        self.accept()
