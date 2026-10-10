# -*- coding: utf-8 -*-
"""按钮库管理面板：查看/编辑/新增/删除 ui_registry.yaml 中的按钮

入库工作流（配合左侧"实时画面"标签）：
  1. 在实时画面点目标按钮 → 得到最近取点
  2. 回到本面板 → 填按钮名/界面/说明 → 「采模板并入库」一键完成
     (以取点为中心裁模板 + 写入注册表)
  3. 或只填坐标不采模板（模板留空，用 at 坐标点击）
"""
from __future__ import annotations

import cv2
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from engine import UiRegistry


class RegistryPanel(QWidget):
    COLS = ["按钮名", "坐标", "模板", "打开", "别名", "动态", "说明"]

    def __init__(self, mainwin):
        super().__init__()
        self.main = mainwin          # 主窗口: 提供 device/last_click_xy/last_screen_bgr
        self.registry: UiRegistry = mainwin.ui_registry
        self._build_ui()
        self.refresh()

    # ---------------- UI ----------------
    def _build_ui(self):
        v = QVBoxLayout(self)

        # -- 筛选行 --
        h = QHBoxLayout()
        h.addWidget(QLabel("界面:"))
        self.combo_filter = QComboBox()
        self.combo_filter.currentIndexChanged.connect(self.refresh)
        h.addWidget(self.combo_filter, stretch=1)
        h.addWidget(QLabel("搜索:"))
        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("名称/别名/说明关键字")
        self.txt_search.textChanged.connect(self.refresh)
        h.addWidget(self.txt_search, stretch=1)
        v.addLayout(h)

        # -- 界面判定(screens)管理 --
        hs = QHBoxLayout()
        hs.addWidget(QLabel("界面判定:"))
        self.combo_screens = QComboBox()
        hs.addWidget(self.combo_screens, stretch=1)
        self.btn_edit_screen = QPushButton("添加/更新判定…")
        self.btn_edit_screen.setToolTip("新建界面或修改其判定模板(用于 wait:{ui:界面名})")
        self.btn_edit_screen.clicked.connect(self._edit_screen)
        hs.addWidget(self.btn_edit_screen)
        self.btn_del_screen = QPushButton("删除判定")
        self.btn_del_screen.clicked.connect(self._delete_screen)
        hs.addWidget(self.btn_del_screen)
        v.addLayout(hs)

        # -- 按钮列表 --
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(self.COLS))
        self.tree.setHeaderLabels(self.COLS)
        self.tree.setRootIsDecorated(True)
        self.tree.setSelectionMode(QTreeWidget.SingleSelection)
        self.tree.itemSelectionChanged.connect(self._on_select)
        for i, w in enumerate([110, 70, 150, 90, 100, 40, 230]):
            self.tree.setColumnWidth(i, w)
        v.addWidget(self.tree, stretch=3)

        # -- 编辑表单 --
        form_box = QGroupBox("按钮 编辑 / 新增")
        form = QFormLayout(form_box)
        row_h = QHBoxLayout()
        self.ed_name = QLineEdit()
        self.ed_name.setPlaceholderText("中文名(如: 修罗任务)")
        row_h.addWidget(self.ed_name, stretch=1)
        self.chk_dyn = QCheckBox("动态内容(状态会变)")
        row_h.addWidget(self.chk_dyn)
        form.addRow("按钮名:", row_h)

        row_s = QHBoxLayout()
        self.ed_screen = QComboBox()
        self.ed_screen.setEditable(True)
        self.ed_screen.addItems(["主界面", "玩法面板", "角色面板", "背包面板",
                                 "坐骑面板", "召唤兽面板", "主菜单面板"])
        row_s.addWidget(self.ed_screen, stretch=1)
        self.ed_x = QLineEdit(); self.ed_x.setPlaceholderText("x")
        self.ed_y = QLineEdit(); self.ed_y.setPlaceholderText("y")
        row_s.addWidget(QLabel("坐标:")); row_s.addWidget(self.ed_x)
        row_s.addWidget(self.ed_y)
        self.btn_use_point = QPushButton("←用最近取点")
        self.btn_use_point.setToolTip("填入左侧实时画面最近点过的坐标")
        self.btn_use_point.clicked.connect(self._use_last_point)
        row_s.addWidget(self.btn_use_point)
        form.addRow("归属界面:", row_s)

        self.combo_tpl = QComboBox()
        self.combo_tpl.setEditable(True)
        form.addRow("模板(可选):", self.combo_tpl)
        row_o = QHBoxLayout()
        self.ed_opens = QComboBox()
        self.ed_opens.setEditable(True)
        self.ed_opens.setPlaceholderText("不打开新界面则留空")
        row_o.addWidget(self.ed_opens, stretch=1)
        form.addRow("打开界面:", row_o)
        self.ed_alias = QLineEdit()
        self.ed_alias.setPlaceholderText("逗号分隔, 如: 宠物,召唤兽入口")
        form.addRow("别名(可选):", self.ed_alias)
        self.ed_desc = QLineEdit()
        self.ed_desc.setPlaceholderText("功能说明(desc 即文档)")
        form.addRow("说明:", self.ed_desc)
        v.addWidget(form_box)

        # -- 操作按钮 --
        hb = QHBoxLayout()
        self.btn_quick = QPushButton("★ 采模板并入库")
        self.btn_quick.setStyleSheet("font-weight:bold; color:#060;")
        self.btn_quick.setToolTip("用最近取点为中心从当前画面裁模板, 连同坐标一起写入注册表")
        self.btn_quick.clicked.connect(self._quick_add)
        hb.addWidget(self.btn_quick)
        self.btn_add = QPushButton("仅坐标入库")
        self.btn_add.clicked.connect(self._add)
        hb.addWidget(self.btn_add)
        self.btn_update = QPushButton("更新选中")
        self.btn_update.clicked.connect(self._update_selected)
        hb.addWidget(self.btn_update)
        self.btn_del = QPushButton("删除选中")
        self.btn_del.clicked.connect(self._delete_selected)
        hb.addWidget(self.btn_del)
        self.btn_reload = QPushButton("重新加载")
        self.btn_reload.clicked.connect(self.reload_registry)
        hb.addWidget(self.btn_reload)
        v.addLayout(hb)

        self.lbl_info = QLabel("")
        self.lbl_info.setStyleSheet("color:#888; font-size:11px;")
        v.addWidget(self.lbl_info)

    # ---------------- 数据 ----------------
    def reload_registry(self):
        """从磁盘重新加载注册表(外部改动生效)"""
        self.registry = UiRegistry(self.registry.path)
        self.main.ui_registry = self.registry
        self.refresh()
        self.lbl_info.setText("已从磁盘重新加载")

    def refresh(self):
        # 界面筛选下拉
        cur = self.combo_filter.currentText()
        self.combo_filter.blockSignals(True)
        self.combo_filter.clear()
        self.combo_filter.addItem("全部")
        self.combo_filter.addItems(list(self.registry.screens))
        self.combo_filter.addItems(
            sorted({b.get("screen", "") for b in self.registry.buttons.values()}
                   - {""}))
        if cur:
            i = self.combo_filter.findText(cur)
            if i >= 0:
                self.combo_filter.setCurrentIndex(i)
        self.combo_filter.blockSignals(False)

        # 模板下拉
        ct = self.combo_tpl.currentText()
        self.combo_tpl.blockSignals(True)
        self.combo_tpl.clear()
        self.combo_tpl.addItem("")            # 允许不配模板
        if self.main.matcher:
            self.combo_tpl.addItems(sorted(self.main.matcher.names()))
        if ct:
            i = self.combo_tpl.findText(ct)
            if i >= 0:
                self.combo_tpl.setCurrentIndex(i)
        self.combo_tpl.blockSignals(False)

        # 打开界面下拉(可选目标 = 所有已判定的界面)
        co = self.ed_opens.currentText()
        self.ed_opens.blockSignals(True)
        self.ed_opens.clear()
        self.ed_opens.addItem("")
        self.ed_opens.addItems(list(self.registry.screens))
        if co:
            i = self.ed_opens.findText(co)
            self.ed_opens.setEditText(co if i < 0 else co)
        self.ed_opens.blockSignals(False)

        # 界面判定下拉
        self.combo_screens.blockSignals(True)
        self.combo_screens.clear()
        for s in self.registry.screens:
            self.combo_screens.addItem(s)
        self.combo_screens.blockSignals(False)

        # 列表(按界面分组, 组名带 opens 导航路径; 三级按钮的入口按钮带→标记)
        kw = self.txt_search.text().strip()
        filt = self.combo_filter.currentText()
        self.tree.clear()
        groups: dict[str, list] = {}
        for name, b in self.registry.buttons.items():
            scr = b.get("screen", "(未分组)")
            if filt != "全部" and scr != filt and name != filt:
                continue
            if kw and kw not in name and not any(
                    kw in a for a in b.get("alias", [])) and kw not in b.get("desc", ""):
                continue
            groups.setdefault(scr, []).append((name, b))
        for scr in sorted(groups, key=lambda s: (self.registry.screen_path(s) is None, s)):
            path = self.registry.screen_path(scr)
            label = " › ".join(path) if path else scr
            root = QTreeWidgetItem([label])
            self.tree.addTopLevelItem(root)
            for name, b in groups[scr]:
                at = b.get("at")
                item = QTreeWidgetItem([
                    name,
                    f"({at[0]},{at[1]})" if at else "-",
                    b.get("template", "-"),
                    f"→ {b['opens']}" if b.get("opens") else "-",
                    ", ".join(b.get("alias", [])) or "-",
                    "是" if b.get("dynamic") else "",
                    b.get("desc", ""),
                ])
                item.setData(0, Qt.UserRole, name)
                root.addChild(item)
            root.setExpanded(True)
        self.lbl_info.setText(
            f"共 {len(self.registry.buttons)} 个按钮 / {len(self.registry.screens)} 个界面"
            f"    库文件: {self.registry.path.name}")

    def _selected_name(self) -> str | None:
        its = self.tree.selectedItems()
        if not its:
            return None
        it = its[0]
        return it.data(0, Qt.UserRole) or None   # 分组行无 UserRole

    def _on_select(self):
        name = self._selected_name()
        if not name:
            return
        b = self.registry.get(name)
        if not b:
            return
        self.ed_name.setText(name)
        i = self.ed_screen.findText(b.get("screen", ""))
        self.ed_screen.setEditText(b.get("screen", "") if i < 0 else b.get("screen", ""))
        at = b.get("at") or [None, None]
        self.ed_x.setText(str(at[0]) if at[0] is not None else "")
        self.ed_y.setText(str(at[1]) if at[1] is not None else "")
        self.combo_tpl.setEditText(b.get("template", ""))
        self.ed_opens.setEditText(b.get("opens", ""))
        self.ed_alias.setText(", ".join(b.get("alias", [])))
        self.chk_dyn.setChecked(bool(b.get("dynamic")))
        self.ed_desc.setText(b.get("desc", ""))

    # ---------------- 动作 ----------------
    def _use_last_point(self):
        if not self.main.last_click_xy:
            QMessageBox.information(self, "提示", "先去「实时画面」标签点一下目标按钮")
            return
        x, y = self.main.last_click_xy
        self.ed_x.setText(str(x))
        self.ed_y.setText(str(y))

    def _collect_form(self) -> dict:
        name = self.ed_name.text().strip()
        if not name:
            raise ValueError("请填按钮名")
        at = None
        if self.ed_x.text().strip() and self.ed_y.text().strip():
            at = [int(self.ed_x.text()), int(self.ed_y.text())]
        tpl = self.combo_tpl.currentText().strip() or None
        alias = [a for a in self.ed_alias.text().replace("，", ",").split(",")] \
            if self.ed_alias.text().strip() else None
        return dict(name=name, desc=self.ed_desc.text().strip(),
                    screen=self.ed_screen.currentText().strip(),
                    template=tpl, at=at, alias=alias,
                    dynamic=self.chk_dyn.isChecked(),
                    opens=self.ed_opens.currentText().strip())

    def _save_and_refresh(self, action: str):
        try:
            self.registry.save()
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.refresh()
        self.lbl_info.setText(f"{action} 成功, 已写入 {self.registry.path.name}")
        if self.main.matcher:
            self.main.matcher.reload()

    def _quick_add(self):
        """一键入库: 最近取点 → 裁模板 → 写注册表

        2026-09-24: 画面按"取点所属账号"取 —— 多开时在账号2的画面上点了按钮,
        却拿本机画面去裁模板会张冠李戴, 入库的模板直接用不了。
        """
        if not self.main.last_click_xy:
            QMessageBox.information(self, "提示", "先去「实时画面」点一下目标按钮")
            return
        port = self.main.pick_port()
        img = self.main.frame_of(port)
        if img is None and int(port) == int(self.main._local_port):
            self.main._manual_screenshot()      # 只有本机设备才能现场补一张
            img = self.main.frame_of(port)
        if img is None:
            who = port
            ch = self.main.store.get(port)
            if ch is not None:
                who = ch.label
            QMessageBox.warning(
                self, "失败",
                f"「{who}」还没有可用画面。\n多开账号的画面要等它的任务跑起来才有。")
            return
        try:
            f = self._collect_form()
        except ValueError as e:
            QMessageBox.warning(self, "提示", str(e))
            return
        x, y = self.main.last_click_xy
        tpl = f["template"] or f"btn_{x}_{y}"
        # 采集模板(以取点为中心 80x80)
        hw = 40
        x1, y1 = max(0, x - hw), max(0, y - hw)
        x2, y2 = min(img.shape[1], x + hw), min(img.shape[0], y + hw)
        crop = img[y1:y2, x1:x2]
        out = self.main._tpl_dir() / f"{tpl}.png"
        cv2.imwrite(str(out), crop)
        try:
            self.registry.upsert(
                f["name"], desc=f["desc"], screen=f["screen"],
                template=tpl, at=[x, y], alias=f["alias"], dynamic=f["dynamic"],
                opens=f["opens"])
        except ValueError as e:
            QMessageBox.warning(self, "入库失败", str(e))
            return
        self._save_and_refresh(f"入库 [{f['name']}] 模板={tpl} ({x},{y})")

    def _add(self):
        try:
            f = self._collect_form()
            self.registry.upsert(
                f["name"], desc=f["desc"], screen=f["screen"],
                template=f["template"], at=f["at"], alias=f["alias"],
                dynamic=f["dynamic"], opens=f["opens"])
        except ValueError as e:
            QMessageBox.warning(self, "入库失败", str(e))
            return
        self._save_and_refresh(f"新增 [{f['name']}]")

    def _update_selected(self):
        name = self._selected_name()
        if not name:
            QMessageBox.information(self, "提示", "先在列表中选中一个按钮")
            return
        try:
            f = self._collect_form()
            if f["name"] != name and f["name"] in self.registry.buttons:
                raise ValueError(f"新名字[{f['name']}]已存在")
            self.registry.delete(name)
            self.registry.upsert(
                f["name"], desc=f["desc"], screen=f["screen"],
                template=f["template"], at=f["at"], alias=f["alias"],
                dynamic=f["dynamic"], opens=f["opens"])
        except ValueError as e:
            QMessageBox.warning(self, "更新失败", str(e))
            self.reload_registry()
            return
        self._save_and_refresh(f"更新 [{name} → {f['name']}]")

    def _delete_selected(self):
        name = self._selected_name()
        if not name:
            return
        r = QMessageBox.question(self, "确认删除", f"删除按钮 [{name}] ?")
        if r != QMessageBox.Yes:
            return
        try:
            self.registry.delete(name)
        except ValueError as e:
            QMessageBox.warning(self, "失败", str(e))
            return
        self._save_and_refresh(f"删除 [{name}]")

    # ---------------- 界面判定管理 ----------------
    def _edit_screen(self):
        """新建/更新界面判定对话框"""
        cur = self.combo_screens.currentText() if self.combo_screens.count() else ""
        d = QDialog(self)
        d.setWindowTitle("界面判定 (wait:{ui:界面名})")
        form = QFormLayout(d)
        ed_name = QLineEdit(cur)
        ed_name.setPlaceholderText("界面名, 如: 技能页")
        form.addRow("界面名:", ed_name)
        ed_any = QLineEdit()
        ed_any.setPlaceholderText("判定模板, 逗号分隔(任一命中即在页面)\n如: panel_skill_title 或 技能页标题,菜单返回")
        form.addRow("判定模板(any_of):", ed_any)
        ed_desc = QLineEdit()
        form.addRow("说明:", ed_desc)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        form.addRow(bb)
        if d.exec() != QDialog.Accepted:
            return
        try:
            self.registry.upsert_screen(
                ed_name.text(), ed_any.text().replace("，", ",").split(","),
                ed_desc.text().strip())
        except ValueError as e:
            QMessageBox.warning(self, "失败", str(e))
            return
        self._save_and_refresh(f"界面判定 [{ed_name.text().strip()}] 已保存")

    def _delete_screen(self):
        cur = self.combo_screens.currentText()
        if not cur:
            return
        r = QMessageBox.question(self, "确认删除", f"删除界面判定 [{cur}] ?\n(按钮不受影响, 但 wait:{{ui:{cur}}}) 将不可用")
        if r != QMessageBox.Yes:
            return
        try:
            self.registry.delete_screen(cur)
        except ValueError as e:
            QMessageBox.warning(self, "失败", str(e))
            return
        self._save_and_refresh(f"界面判定 [{cur}] 已删除")
