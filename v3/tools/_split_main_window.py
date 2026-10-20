# -*- coding: utf-8 -*-
"""main_window.py 拆分执行脚本 (2026-10-18 v4.7.0 批次四·工程 P2-1)

为什么写成一次性脚本而不是手工 Edit:
  要搬 45 个方法 / 994 行。手工剪切粘贴必然会漏字符、错缩进、吃错别字,
  而且一旦出错, 2890 行的文件里找差异是大海捞针。
  这里用 ast 精确算出每个方法的行范围, **按原行整体搬运**(Mixin 里的方法
  缩进和主窗口类内方法一样是 4 空格, 所以内容一个字都不用改), 主窗口则是
  把对应行整段删掉。搬完用 compile + 全量回归验证行为等价。

  用完就留在 tools/ 当记录 —— 它不是构建的一部分, 只是这次重构的执行器。
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MW = ROOT / "app" / "xyfz_auto" / "gui" / "main_window.py"

# ---------------------------------------------------------------- 分组定义
GROUPS = [
    ("A_timer", "gui/timer_mixin.py", "TimerMixin", [
        "_account_combo", "_append_timer_row", "_add_timer_dialog",
        "_del_timer_rows", "_restore_timer", "_save_timer_state",
        "_tick_timer", "_fire_timer_item", "_timer_cfg_int",
        "_persist_timer_runs", "_timer_done", "_auto_start_task",
    ]),
    ("B_logrotate", "gui/logrotate_mixin.py", "LogRotateMixin", [
        "_log_keep_days", "_active_log_names", "_rotate_logs",
        "_maybe_rotate_daily",
    ]),
    ("C_taskeditor", "gui/taskeditor_mixin.py", "TaskEditorMixin", [
        "_task_dir", "_refresh_task_list", "_on_task_selected",
        "_rebuild_script_pickers", "_picker_selection",
        "_load_file_into_editor", "_on_edit_script_changed",
        "_on_edit_text_changed", "_flush_editor", "_refresh_browse_steps",
        "_start_from_selected_step", "_open_task_file", "_new_task_file",
        "_select_script_in_picker", "_insert_button_snippet",
        "_insert_into_editor", "_save_task", "_build_template_bar",
        "_tpl_dir", "_refresh_template_list", "_collect_template",
    ]),
    ("D_lights", "gui/lights_mixin.py", "StatusLightsMixin", [
        "_rebuild_status_lights", "_refresh_port_states", "_port_state_of",
        "_set_port_state", "_clear_port_state",
    ]),
    ("E_livepoll", "gui/livepoll_mixin.py", "LivePollMixin", [
        "_discover_ports", "_heartbeat_timeout", "_poll_live",
    ]),
]

# 各 Mixin 的 imports —— 由依赖分析得出(见当时脚本: 用 ast 求每个方法的自由变量,
# 再按"属于 main_window 顶部 import / 模块级定义 / builtins"分类)。
IMPORTS = {
    "A_timer": '''\
from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

from PySide6.QtCore import Qt, QTime, QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFormLayout, QMessageBox, QTableWidgetItem,
                               QTimeEdit)

from accounts import load_accounts as _load_accounts
from gui.ui_helpers import _safe_call
''',
    "B_logrotate": '''\
from __future__ import annotations

import datetime
from pathlib import Path

from common import rotate_files, rotate_text_logs
''',
    "C_taskeditor": '''\
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
''',
    "D_lights": '''\
from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from accounts import load_accounts as _load_accounts
from gui.ui_helpers import _PortLight, _safe_call, _tail_int
''',
    "E_livepoll": '''\
from __future__ import annotations

from live import LIVE_ROOT, LiveReader
''',
}

# 各 Mixin 的模块级常量重建。PROJECT_ROOT 在 main_window 里是
# Path(__file__).parent.parent(app), gui/ 下要多退一层。
CONSTS = {
    "B_logrotate": '\nPROJECT_ROOT = Path(__file__).resolve().parents[2]   # app/\n',
    "C_taskeditor": '\nPROJECT_ROOT = Path(__file__).resolve().parents[2]   # app/\n',
}

DOCSTRINGS = {
    "A_timer": '''\
"""定时执行相关逻辑

拆自 main_window.py(2026-10-18 v4.7.0 批次四·工程 P2-1)。到点判定 / 启动 /
成败回写 / 重试 / 错峰 都在这里, 主窗口只留一个 QTimer 每 5 秒调 _tick_timer。

依赖(由宿主 MainWindow 提供):
  · 控件: self.table_timer / self.lbl_timer_state / self.tabs
  · 状态: self._timer_runs / self._timer_retry / self._timer_pending_ports /
          self._timer_cfg_path / self.cfg / self._active_log_names
  · 回调: self.append_sys_log / self._start_task / self.panel_multi
"""
''',
    "B_logrotate": '''\
"""文本日志 / 失败截图 的滚动清理

拆自 main_window.py(2026-10-18 v4.7.0 批次四·工程 P2-1)。
触发点三条: GUI 启动 / 跨日 / 任务结束 —— 见调用方。

依赖(由宿主 MainWindow 提供):
  · self.cfg(读 engine.log_keep_days 等)
  · self.append_sys_log
"""
''',
    "C_taskeditor": '''\
"""脚本页编辑器: 任务列表 / 子脚本下拉 / 增删改 / 模板条

拆自 main_window.py(2026-10-18 v4.7.0 批次四·工程 P2-1)。
这一块是纯粹的"文件读写 + 控件重填", 与主流程耦合最低, 拆出来最安全。

依赖(由宿主 MainWindow 提供):
  · 控件: self.list_tasks / self.combo_edit_script / self.txt_edit /
          self.list_browse / self.combo_pick_script / self.tabs / self._tpl_box
  · 回调: self._start_task / self.refresh_account_names / self._set_busy
"""
''',
    "D_lights": '''\
"""左侧账号状态灯 + 每端口任务状态那一列

拆自 main_window.py(2026-10-18 v4.7.0 批次四·工程 P2-1)。

依赖(由宿主 MainWindow 提供):
  · self._port_lights / self._port_state_labels / self._lights_box
  · self.store(ChannelStore, 只读) / self._acc_name
"""
''',
    "E_livepoll": '''\
"""后台 live 通道轮询(画面 / 步骤 / 子进程存亡)

拆自 main_window.py(2026-10-18 v4.7.0 批次四·工程 P2-1)。
每条对自己的 QTimer, 只读 live 目录, 不碰 adb(adb 是另一个后台线程)。

依赖(由宿主 MainWindow 提供):
  · self.store / self.panel_multi / self.append_sys_log
  · 回调: self.mark_stopped / self.set_active(由 store 提供)
"""
''',
}

# 模块级工具(_safe_call/_tail_int/_input_dialog/_PortLight)已抽到
# gui/ui_helpers.py, 这里连同它们上方的注释一起从 main_window 删掉。
HELPER_LINES = None   # 运行时按名字算


def analyze(text: str):
    tree = ast.parse(text)
    cls = [n for n in tree.body
           if isinstance(n, ast.ClassDef) and n.name == "MainWindow"][0]
    meths = {}
    for n in cls.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])
            meths[n.name] = (start, n.end_lineno)
    tops = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = min([n.lineno] + [d.lineno for d in n.decorator_list])
            tops[n.name] = (start, n.end_lineno)
        elif isinstance(n, ast.ClassDef):
            tops[n.name] = (n.lineno, n.end_lineno)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                for x in ast.walk(t):
                    if isinstance(x, ast.Name):
                        tops[x.id] = (n.lineno, n.end_lineno)
    return tree, cls, meths, tops


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的写入(默认只预演)")
    args = ap.parse_args()

    raw = MW.read_bytes().decode("utf-8")
    text = raw.replace("\r\n", "\n")
    lines = text.split("\n")

    tree, cls, meths, tops = analyze(text)
    print("main_window.py  %d 行, MainWindow %d 个方法"
          % (len(lines), len(meths)))

    all_occupied = sorted(set(list(meths.values()) + list(tops.values())))

    plan = []       # [(group, [(s,e,name),...])]
    moved_total = 0
    for gname, relpath, mixcls, names in GROUPS:
        rngs = []
        for nm in names:
            s, e = meths[nm]
            # 向上吸收紧邻的注释 / 空行, 但不能越过上一个顶层节点的结尾
            cur = s
            while cur > 1:
                prev_txt = lines[cur - 2]
                blocked = any(a <= cur - 1 <= b for a, b in all_occupied)
                if blocked:
                    break
                if prev_txt.strip() and not prev_txt.strip().startswith("#"):
                    break
                if not prev_txt.strip() and any(a <= cur - 1 <= b
                                                for a, b in all_occupied):
                    break
                cur -= 1
            rngs.append((cur, e, nm))
        plan.append((gname, relpath, mixcls, rngs, names))
        moved_total += sum(e - s + 1 for s, e, _ in rngs)

    # 模块级工具的行范围(搬到 gui/ui_helpers.py 后从主窗口删除)
    helper_names = ["_safe_call", "_tail_int", "_PortLight", "_input_dialog"]
    helper_rngs = []
    for nm in helper_names:
        helper_rngs.append((tops[nm][0], tops[nm][1], nm))

    print("拟搬出 %d 行(含模块级工具 %d 行)"
          % (moved_total, sum(e - s + 1 for s, e, _ in helper_rngs)))

    # ---------------- 生成各 Mixin ----------------
    for gname, relpath, mixcls, rngs, names in plan:
        body = []
        for s, e, nm in rngs:
            body.extend(lines[s - 1:e])
            body.append("")
        while body and not body[-1]:
            body.pop()
        content = (
            "# -*- coding: utf-8 -*-\n"
            + DOCSTRINGS[gname]
            + IMPORTS[gname].rstrip() + "\n"
            + CONSTS.get(gname, "")
            + "\nclass " + mixcls + ":\n"
            + '    """见模块 docstring。方法体整体搬自 main_window.py, 未作改写。"""\n\n'
            + "\n".join(body) + "\n"
        )
        if not args.apply:
            print("\n" + "=" * 60)
            print("  预演 %s -> %s  (%d 行)" % (gname, relpath, content.count("\n")))
            print("=" * 60)
            print("首段开头:")
            for l in content.split("\n")[:40]:
                print("   | " + l)
            print("   ...(%d 行省略)" % max(0, content.count("\n") - 40))
        else:
            p = ROOT / "app" / "xyfz_auto" / relpath
            p.write_bytes(content.replace("\n", "\r\n").encode("utf-8"))
            print("  已写 %-28s %4d 行" % (relpath, content.count("\n")))

    # ---------------- 重写 main_window ----------------
    drop = set()
    for s, e, _ in helper_rngs:
        drop.update(range(s, e + 1))
    for _, _, _, rngs, _ in plan:
        for s, e, _ in rngs:
            drop.update(range(s, e + 1))
    out = [l for i, l in enumerate(lines, 1) if i not in drop]

    # 插 import + 改 class 声明
    new_text = "\n".join(out)
    old_cls_line = "class MainWindow(RenderMixin, QMainWindow):"
    assert old_cls_line in new_text, "找不到 MainWindow 类声明行"
    mixin_names = [m for _, _, m, _, _ in plan]
    new_cls_line = "class MainWindow(RenderMixin, %s, QMainWindow):" % ", ".join(mixin_names)
    new_text = new_text.replace(old_cls_line, new_cls_line, 1)

    imports = "\n".join(
        "from %s import %s"
        % (relpath[:-3].replace("/", "."), m)
        for _, relpath, m, _, _ in plan)
    anchor = "from gui.render_mixin import RenderMixin\n"
    assert anchor in new_text
    new_text = new_text.replace(
        anchor,
        anchor + imports + "\n" +
        "from gui.ui_helpers import (_PortLight, _input_dialog, _safe_call,\n"
        "                           _tail_int)\n",
        1)

    if not args.apply:
        print("\n[预演] main_window.py 将由 %d 行降到 %d 行"
              % (len(lines), new_text.count("\n") + 1))
        print("[预演] 新类声明: " + new_cls_line)
        print("[预演] 新增导入:\n" + imports)
        return

    MW.write_bytes(new_text.replace("\n", "\r\n").encode("utf-8"))
    print("\n  已重写 main_window.py: %d 行 -> %d 行"
          % (len(lines), new_text.count("\n") + 1))


if __name__ == "__main__":
    main()
