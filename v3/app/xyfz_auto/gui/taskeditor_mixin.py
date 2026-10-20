# -*- coding: utf-8 -*-
"""脚本页编辑器: 任务列表 / 子脚本下拉 / 增删改 / 模板条

拆自 main_window.py(2026-10-18 v4.7.0 批次四·工程 P2-1)。
这一块是纯粹的"文件读写 + 控件重填", 与主流程耦合最低, 拆出来最安全。

依赖(由宿主 MainWindow 提供):
  · 控件: self.list_tasks / self.combo_edit_script / self.txt_edit /
          self.list_browse / self.combo_pick_script / self.tabs / self._tpl_box
  · 回调: self._start_task / self.refresh_account_names / self._set_busy
"""
from __future__ import annotations
import sys
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QGroupBox,
                               QHBoxLayout, QLabel, QListWidgetItem,
                               QMessageBox, QPushButton, QWidget)
from common import imwrite_unicode
from engine import list_sub_scripts, load_task
from engine.ui_registry import UiRegistry
from gui.button_library import ButtonLibraryDialog
from gui.new_script import KIND_PART, NewScriptDialog, insert_include_into_task
from gui.ui_helpers import _input_dialog
# ⚠ 2026-10-19 v5.0.0 修正: 必须与 main_window.py 的 PROJECT_ROOT 同一个根即
#   app/xyfz_auto/(本文件在 gui/ 下 → parents[1])。v4.7.0 拆分时误写成 parents[2]
#   (= app/), 于是 _task_dir() 指向不存在的 app/assets/tasks —— 表现为「脚本下拉
#   全空、脚本页一片空白、脚本步骤页空白」, 而且不报错(glob 不到文件就是空列表)。
PROJECT_ROOT = Path(__file__).resolve().parents[1]   # app/xyfz_auto/
class TaskEditorMixin:
    """见模块 docstring。方法体整体搬自 main_window.py, 未作改写。"""
    def _task_dir(self):
        return PROJECT_ROOT / "assets" / "tasks"
    def _refresh_task_list(self):
        cur = self.combo_task.currentText()
        self.combo_task.blockSignals(True)
        self.combo_task.clear()
        for p in sorted(self._task_dir().glob("*.yaml")):
            self.combo_task.addItem(p.name, str(p))
        if cur:
            i = self.combo_task.findText(cur)
            if i >= 0:
                self.combo_task.setCurrentIndex(i)
        self.combo_task.blockSignals(False)
        self._on_task_selected()
    def _on_task_selected(self):
        p = self.combo_task.currentData()
        if not p:
            self.edit_script.clear()
            return
        self.current_task_path = str(p)
        # 「脚本」/「脚本步骤」两页的"脚本"下拉框跟着换任务重建(2026-10-09)
        self._rebuild_script_pickers()
        # 重建后默认回到"整个任务", 把总脚本灌进编辑器
        self._load_file_into_editor(p)
        # 脚本选项页跟着换脚本(选项声明在各脚本自己的 yaml 里)
        if getattr(self, "page_settings", None) is not None:
            self.page_settings.set_task(p)
        # 「脚本步骤」页跟着换脚本(2026-10-09)
        if getattr(self, "list_browse", None) is not None:
            self._refresh_browse_steps()
    def _rebuild_script_pickers(self):
        """按当前任务刷新两个"脚本"下拉框(选中项一律回到"整个任务")"""
        p = self.combo_task.currentData() or self.current_task_path
        combos = [c for c in (getattr(self, "combo_edit_script", None),
                              getattr(self, "combo_browse_script", None))
                  if c is not None]
        if not combos:
            return
        subs: list[dict] = []
        if p:
            try:
                subs = list_sub_scripts(p, self._task_dir())
            except Exception as e:
                print(f"[browse] 扫描子脚本失败: {e}", file=sys.stderr)
        for combo in combos:
            combo.blockSignals(True)          # 重建期间别触发加载/刷新
            combo.clear()
            if p:
                combo.addItem(f"整个任务 —— {Path(p).name}", ("*", str(p)))
                for s in subs:
                    combo.addItem(f"{s['name']}  [{s['rel']}]",
                                  (s["rel"], s["path"]))
            combo.setCurrentIndex(0)
            combo.blockSignals(False)
    @staticmethod
    def _picker_selection(combo):
        """读"脚本"下拉框的选中项 → (rel 相对路径, 绝对路径); rel='*' 表示整个任务"""
        d = combo.currentData()
        if isinstance(d, (tuple, list)) and len(d) == 2:
            return str(d[0]), (str(d[1]) if d[1] else None)
        return "*", None
    def _load_file_into_editor(self, path):
        """把某个脚本文件读进编辑器, 并记下它是谁(保存 / 启动同步都按它)"""
        if getattr(self, "edit_script", None) is None or not path:
            return
        try:
            txt = Path(path).read_text(encoding="utf-8")
        except Exception as e:
            QMessageBox.warning(self, "打开脚本", f"读取失败:\n{path}\n{e}")
            return
        self._loading_edit = True
        try:
            self.edit_script.setPlainText(txt)
            self.edit_script_path = str(path)
            self._edit_dirty = False
        finally:
            self._loading_edit = False
        try:
            shown = str(Path(path).resolve().relative_to(
                self._task_dir().resolve())).replace("\\", "/")
        except Exception:
            shown = Path(path).name
        if getattr(self, "lbl_edit_path", None) is not None:
            self.lbl_edit_path.setText(f"正在编辑: {shown}")
    def _on_edit_script_changed(self, _idx=0):
        """「脚本」页切换目标文件(总脚本 <-> 子脚本)"""
        combo = getattr(self, "combo_edit_script", None)
        if combo is None or not combo.count():
            return
        _rel, path = self._picker_selection(combo)
        if path and str(path) != str(self.edit_script_path or ""):
            self._load_file_into_editor(path)
    def _on_edit_text_changed(self):
        """编辑器内容变化 → 记 dirty(程序化灌内容时不记)"""
        if not self._loading_edit:
            self._edit_dirty = True
    def _flush_editor(self):
        """启动任务前, 把编辑器里未保存的改动写回**它正在编辑的那个文件**

        2026-10-09: 「脚本」页现在能直接编辑子脚本, 不能再无条件写回任务文件
        (那会把子脚本内容覆盖进总脚本)。只写编辑器自己打开的那份。
        """
        tgt = self.edit_script_path
        if not tgt or not self._edit_dirty:
            return
        try:
            Path(tgt).write_text(self.edit_script.toPlainText(), encoding="utf-8")
            self._edit_dirty = False
            self.append_sys_log(f"[启动] 已同步未保存改动 → {Path(tgt).name}")
        except Exception as e:
            self.append_sys_log(f"[启动] 同步编辑器改动失败({Path(tgt).name}): {e}")
    def _refresh_browse_steps(self):
        """把当前脚本的步骤铺到「脚本步骤」页

        顶部可选"整个任务 / 某个子脚本": 选子脚本时只列它那一段, 但**序号仍是
        整个任务里的全局步号** —— 这样"从选中步骤开始跑"传的 start_index 永远正确。
        """
        if getattr(self, "list_browse", None) is None:
            return
        p = self.combo_task.currentData() or self.current_task_path
        self.list_browse.clear()
        if not p:
            self.lbl_browse_hint.setText("未选择任务脚本")
            return
        try:
            task = load_task(p)
        except Exception as e:
            self.lbl_browse_hint.setText(f"脚本解析失败: {e}")
            return
        rel = "*"
        cbs = getattr(self, "combo_browse_script", None)
        if cbs is not None and cbs.count():
            rel, _path = self._picker_selection(cbs)
        rows = [(i, s) for i, s in enumerate(task.steps)
                if rel == "*" or s.source == rel]
        for i, s in rows:
            it = QListWidgetItem(f"{i + 1}. {s.name or s.describe()}")
            it.setData(Qt.UserRole, i)          # 全局步号(0基) = start_index
            it.setToolTip(f"整个任务里的第 {i + 1} 步 · 来自 {s.source or Path(p).name}\n"
                          f"{s.describe()}")
            self.list_browse.addItem(it)
        base = Path(p).name
        if rel == "*":
            self.lbl_browse_hint.setText(
                f"{base} · 整个任务共 {len(task.steps)} 步 —— 选中一步后点"
                f"「从选中步骤开始跑」(双击同效)")
        elif rows:
            lo, hi = rows[0][0] + 1, rows[-1][0] + 1
            self.lbl_browse_hint.setText(
                f"{base} · 子脚本 {rel} · 共 {len(rows)} 步"
                f"(在整个任务里位于第 {lo}~{hi} 步) —— 序号就是整个任务的步号, "
                f"从选中步骤开跑位置不会错")
        else:
            self.lbl_browse_hint.setText(f"{base} · 子脚本 {rel} 没有步骤")
        btn = getattr(self, "btn_browse_run", None)
        if btn is not None:
            btn.setEnabled(bool(rows))
    def _start_from_selected_step(self):
        """从「脚本步骤」页当前选中的步骤开始执行(前面的步骤跳过)"""
        it = self.list_browse.currentItem()
        if it is None:
            QMessageBox.information(self, "提示", "先在列表里选中一个步骤")
            return
        idx = it.data(Qt.UserRole)
        if idx is None:
            return
        idx = int(idx)
        try:
            total = len(load_task(self.combo_task.currentData()
                                  or self.current_task_path).steps)
        except Exception:
            total = 0
        tail = (f"\n\n注意: 整个任务的第 1~{idx} 步会被跳过, "
                f"第 {idx + 2}~{total} 步照常继续。"
                if total and idx > 0 else "")
        if QMessageBox.question(
                self, "从该步骤开始跑",
                f"将从整个任务的第 {idx + 1} 步开始执行:\n{it.text()}{tail}\n\n"
                f"请先确认游戏当前画面与该步骤匹配, 否则可能点空。\n继续?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._start_task(start_index=idx)
    def _open_task_file(self):
        """打开已有的任务脚本(外部拷进来的也行); 想新建请走「＋新建脚本…」"""
        fn, _ = QFileDialog.getOpenFileName(
            self, "打开任务脚本(新建请用「＋ 新建脚本…」)",
            str(self._task_dir()), "YAML (*.yaml *.yml)")
        if not fn:
            return
        if not Path(fn).is_file():
            QMessageBox.information(
                self, "文件不存在",
                f"{Path(fn).name} 不存在。\n\n要建新脚本请点「＋ 新建脚本…」"
                f"(会生成带注释的骨架, 还能直接挂进当前任务)。")
            return
        self.combo_task.addItem(Path(fn).name, fn)
        self.combo_task.setCurrentIndex(self.combo_task.count() - 1)
    def _new_task_file(self):
        """新建脚本向导: 顶层任务 / parts 子脚本 + 骨架模板 + 按钮库 + 自动挂载

        旧版只有一个「打开/新建脚本…」按钮, 走的是**打开**文件对话框 ——
        敲个新名字会被当成"打开不存在的文件", 编辑器读不到内容直接报错,
        用户因此找不到新建入口。现在拆成独立的向导流程。
        """
        cur = self.combo_task.currentData() or self.current_task_path
        try:
            ui = UiRegistry()
        except Exception:
            ui = None
        dlg = NewScriptDialog(self._task_dir(), ui, self,
                             current_task=str(cur) if cur else None,
                             copy_text=self.edit_script.toPlainText())
        if dlg.exec() != QDialog.Accepted:
            return
        path = dlg.target_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.write_text(dlg.content(), encoding="utf-8")
        except Exception as e:
            QMessageBox.warning(self, "创建失败", f"{path}\n{e}")
            return
        rel = dlg.rel()
        self.append_sys_log(f"[新建] 已创建 {rel}")
        self.logger.info(f"已创建脚本: {path}")
        # 子脚本 → 可选把 include 追加进当前任务(否则两页「脚本」下拉看不到它)
        if dlg.kind() == KIND_PART and dlg.mount_requested() and cur:
            try:
                added = insert_include_into_task(cur, rel, dlg.edit_name.text().strip())
                self.append_sys_log(
                    f"[新建] 已{'' if added else '(本就存在, 跳过)'}把 include: {rel} "
                    f"挂进 {Path(cur).name}")
            except Exception as e:
                self.append_sys_log(f"[新建] 挂载失败: {e}")
            self._rebuild_script_pickers()
        if dlg.kind() == KIND_PART:
            if not self._select_script_in_picker(rel):
                # 没挂进当前任务 → 两页下拉里还没有它, 直接读进编辑器接着写
                self._load_file_into_editor(path)
                self.append_sys_log(
                    f"[新建] {rel} 未挂进当前任务, 已直接在编辑器打开"
                    f"(挂载后才会出现在「脚本」下拉里)")
        else:
            self._refresh_task_list()               # 顶层任务: 进任务下拉
            i = self.combo_task.findText(Path(path).name)
            if i >= 0:
                self.combo_task.setCurrentIndex(i)
    def _select_script_in_picker(self, rel: str) -> bool:
        """把「脚本」/「脚本步骤」两页的下拉切到某个子脚本(找不到返回 False)"""
        ok = False
        for combo in (getattr(self, "combo_edit_script", None),
                      getattr(self, "combo_browse_script", None)):
            if combo is None:
                continue
            for i in range(combo.count()):
                d = combo.itemData(i)
                if isinstance(d, (tuple, list)) and str(d[0]) == rel:
                    combo.setCurrentIndex(i)
                    ok = True
                    break
        return ok
    def _insert_button_snippet(self):
        """「插入按钮/界面…」: 从按钮库挑界面/按钮 → 生成片段插到光标处"""
        try:
            ui = UiRegistry()
        except Exception as e:
            QMessageBox.warning(self, "按钮库不可用", str(e))
            return
        tgt = self.edit_script_path or self.current_task_path or "(未选择)"
        dlg = ButtonLibraryDialog(ui, self, target=str(tgt))
        if dlg.exec() != QDialog.Accepted:
            return
        text = dlg.snippet()
        if text.strip():
            self._insert_into_editor(text)
    def _insert_into_editor(self, snippet: str):
        """把片段插到脚本编辑器光标处(自动补换行, 连续插入不会粘在一起)"""
        ed = self.edit_script
        cur = ed.textCursor()
        txt = snippet if snippet.endswith("\n") else snippet + "\n"
        if cur.position() > 0 and not ed.toPlainText()[: cur.position()].endswith("\n"):
            txt = "\n" + txt
        cur.insertText(txt)          # 会触发 textChanged → 自动标 dirty
        ed.setTextCursor(cur)
        ed.setFocus()
    def _save_task(self):
        """Ctrl+S: 保存**编辑器当前打开的那份文件**(总脚本或某个子脚本)"""
        target = self.edit_script_path or self.current_task_path
        if not target:
            target = str(self._task_dir() / "new_task.yaml")
            self.edit_script_path = target
        try:
            # 校验 YAML 可解析
            txt = self.edit_script.toPlainText()
            import yaml
            data = yaml.safe_load(txt)
            assert isinstance(data, dict) and "steps" in data, "缺少 steps 字段"
            Path(target).write_text(txt, encoding="utf-8")
            self._edit_dirty = False
            self.logger.info(f"已保存脚本: {target}")
            self.append_sys_log(f"[保存] {target}")
            if Path(target).parent.resolve() == self._task_dir().resolve():
                # 顶层任务文件: 刷新任务下拉(可能是个新文件)
                self._refresh_task_list()
            elif getattr(self, "list_browse", None) is not None:
                # 子脚本: 步骤可能变了, 刷新「脚本步骤」页(保留下拉选择)
                self._refresh_browse_steps()
        except Exception as e:
            QMessageBox.warning(self, "保存失败", f"脚本格式有误:\n{e}")
    def _build_template_bar(self) -> QWidget:
        """识别模板库工具条 —— 挂在「实时画面」页底部

        2026-10-09 v2.0.0 从左侧面板搬过来。采集模板的完整动作是两步:
          ① 在上面的实时画面点一下取坐标  → ② 点「从点击点采集模板」
        (以点击点为中心裁 80x80 存进 assets/templates/)。
        两步都发生在这个页面上, 放同一页才叫闭环; 左侧留给运行控制。
        """
        box = QGroupBox("识别模板库")
        self.box_templates = box          # 标题上会带上模板数量
        h = QHBoxLayout(box)
        h.setContentsMargins(8, 2, 8, 2)
        h.setSpacing(6)
        h.addWidget(QLabel("已有模板:"))
        self.combo_templates = QComboBox()
        self.combo_templates.setMinimumWidth(200)
        self.combo_templates.setToolTip(
            "assets/templates/ 里的模板文件(点开可看全部)。\n"
            "脚本里用 wait:{template: 名字} 引用, 或给注册表按钮配 template: 名字。\n"
            "这一栏只用于查看/核对, 选谁都不影响运行。")
        h.addWidget(self.combo_templates)
        self.btn_new_tpl = QPushButton("从点击点采集模板")
        self.btn_new_tpl.setToolTip(
            "在上方画面点一下取坐标 → 以该点为中心裁 80x80 存成新模板。\n"
            "多开时用的是当前所选账号自己的画面, 不会张冠李戴。\n"
            "采完可直接去「按钮库(注册表)」页把它登记到某个按钮上。")
        self.btn_new_tpl.clicked.connect(self._collect_template)
        h.addWidget(self.btn_new_tpl)
        self.lbl_hint = QLabel("先在上面的画面点一下取坐标, 再点采集(中心裁 80x80)")
        self.lbl_hint.setStyleSheet("color:#888; font-size:11px;")
        self.lbl_hint.setWordWrap(True)
        h.addWidget(self.lbl_hint, stretch=1)
        return box
    def _tpl_dir(self):
        return PROJECT_ROOT / "assets" / "templates"
    def _refresh_template_list(self):
        # 2026-10-09 v2.0.0: 列表控件从左侧的 QListWidget 换成「实时画面」页底部的
        # QComboBox(省高度); 两个控件都用 getattr 兜着, 谁没建好都不会把启动搞崩。
        names = [p.stem for p in sorted(self._tpl_dir().glob("*.png"))]
        combo = getattr(self, "combo_templates", None)
        if combo is not None:
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(names)
            combo.setCurrentIndex(-1)      # 不预设选中(这一栏只是查看)
            combo.blockSignals(False)
        box = getattr(self, "box_templates", None)
        if box is not None:
            box.setTitle(f"识别模板库 ({len(names)} 个)")
        # 2026-10-17 v3.5.0(P1-1): reload() 要重读 65 张 png, 主线程里数十~数百 ms。
        # 移后台; 与 worker 线程并发也安全 —— _load() 已改成整体替换(P1-2)。
        if getattr(self, "matcher", None) is not None:
            self._run_bg(self.matcher.reload, busy_text="正在重新载入模板库…")
    def _collect_template(self):
        if not self.last_click_xy:
            QMessageBox.information(self, "提示", "先在右侧实时画面点一下目标位置")
            return
        x, y = self.last_click_xy
        port = self._click_port if self._click_port is not None else self._local_port
        ch = self.store.get(port)
        img = ch.frame if ch else None
        if img is None and int(port) == self._local_port:
            # 本机可以临时补一张。2026-10-17 v3.5.0(P1-1): 截图已改后台线程,
            # 不能像以前那样"同步调完接着往下走" —— 这里改成截图完成后**回调重入**
            # 本方法(那时通道里已有画面)。_shooting 防连点重复起线程。
            if getattr(self, "_shooting", False):
                return
            self._shooting = True
            def _again(_r=None):
                self._shooting = False
                self._collect_template()
            if not self._manual_screenshot(_then=_again):
                self._shooting = False      # 压根没发起(设备未连接) → 立刻复位
            return
        if img is None:
            who = ch.label if ch else f"端口{port}"
            QMessageBox.warning(
                self, "失败",
                f"「{who}」还没有可用画面。\n"
                f"多开账号的画面由该账号的任务进程实时回传, 任务跑起来之后才有图。")
            return
        name, ok = _input_dialog(self, "模板名", "模板名(英文/数字):", f"tpl_{x}_{y}")
        if not ok or not name:
            return
        # 以点击点为中心裁 80x80 —— 用该账号自己的画面, 避免多开时张冠李戴
        hw = 40
        x1, y1 = max(0, x - hw), max(0, y - hw)
        x2, y2 = min(img.shape[1], x + hw), min(img.shape[0], y + hw)
        crop = img[y1:y2, x1:x2]
        out = self._tpl_dir() / f"{name}.png"
        # 2026-10-16 v3.0.0: 原本是裸 cv2.imwrite —— 项目路径含中文时它返回
        # False 却不报错, 于是界面提示"已保存"而文件根本没生成, 等到跑任务
        # 才发现模板不存在(表现为等待超时)。改用统一出口并检查返回值。
        if not imwrite_unicode(out, crop):
            QMessageBox.warning(
                self, "保存失败",
                f"模板没能写入磁盘:\n{out}\n\n"
                f"常见原因: 路径含特殊字符或目录不可写。请换个模板名重试。")
            self.append_sys_log(f"[模板] 保存失败: {out}")
            return
        self.logger.info(f"新模板已保存: {out} ({crop.shape[1]}x{crop.shape[0]})")
        self.append_sys_log(f"[模板] {out.name} 尺寸={crop.shape[1]}x{crop.shape[0]}"
                            f" 来源={(ch.label if ch else port)}")
        self._refresh_template_list()
