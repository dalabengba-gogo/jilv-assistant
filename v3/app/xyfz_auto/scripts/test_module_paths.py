# -*- coding: utf-8 -*-
"""模块根路径一致性 (2026-10-19 v5.0.0) —— 静态扫描, 不连模拟器

事故(用户可见): v4.7.0 把 main_window 拆成 Mixin 时, 抄过来的
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
把根算成了 app/, 而正确是 app/xyfz_auto/(文件在 gui/ 下 → parents[1])。
后果全是**静默**的(不报错、不崩溃, 只是什么都找不到):
  · 脚本下拉空 → 「脚本」页一片空白、「脚本步骤」页空白 → 用户"看不到脚本";
  · 日志滚动扫不存在的 app/logs → 启动/跨日/任务结束三次清理全是空转,
    日志照样涨到上百 MB。
同类风险: 以后再拆文件/搬目录, 少写一级或多写一级都能重现。

所以这里不测行为, 直接**对源码里的赋值表达式求值**, 断言每个模块算出来的
根目录都等于 app/xyfz_auto/ 本身, 且该根下真的有 assets/tasks、assets/templates、
config.yaml。表达式只允许 Path(...)/__file__/parents[i]/parent.parent, 不求值函数调用。
"""
import ast
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 下面 [5] 要建真窗口
os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, os.getcwd())                 # 让 [3][4] 能 import gui / engine
APP_ROOT = Path(os.getcwd()).resolve()          # app/xyfz_auto/

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


def eval_root_expr(node: ast.AST, file_path: Path):
    """把 `Path(__file__).resolve().parents[2]` 这类表达式换成真实值求出来

    只放行 Path / __file__ / .resolve() / .parent / .parents[i], 其它写法
    (变量、函数调用)一律视为"看不懂" → 返回 None 由调用方跳过(不去猜)。
    """
    class _V(ast.NodeVisitor):
        def __init__(self):
            self.ok = True

        def visit_Call(self, n):
            f = n.func
            if isinstance(f, ast.Name) and f.id == "Path":
                if n.args:
                    self.visit(n.args[0])
                return
            if isinstance(f, ast.Attribute) and f.attr in ("resolve",):
                self.visit(f.value)
                return
            self.ok = False

        def visit_Name(self, n):
            if n.id != "__file__":
                self.ok = False

        def visit_Attribute(self, n):
            if n.attr in ("parent", "parents"):
                self.visit(n.value)
                return
            self.ok = False

        def visit_Subscript(self, n):
            self.visit(n.value)
            if not (isinstance(n.slice, ast.Constant)
                    and isinstance(n.slice.value, int)):
                self.ok = False

        def visit_Constant(self, n):
            if not isinstance(n.value, int):
                self.ok = False

        def generic_visit(self, n):
            # 二元运算/比较/lambda 等一概不认
            self.ok = False

    v = _V()
    v.visit(node)
    if not v.ok:
        return None
    try:
        return eval(compile(ast.Expression(body=node), "<expr>", "eval"),
                    {"Path": Path, "__file__": str(file_path)})
    except Exception:
        return None


print("[0] 前置: 根目录自身完整")
check((APP_ROOT / "assets" / "tasks").is_dir(), f"assets/tasks 存在({APP_ROOT})")
check((APP_ROOT / "config.yaml").is_file(), "config.yaml 存在")
task_n = len(list((APP_ROOT / "assets" / "tasks").glob("*.yaml")))
check(task_n >= 1, f"assets/tasks 下能扫到任务脚本(实测 {task_n} 个)")

print("\n[1] 每个模块的 PROJECT_ROOT / APP_DIR 都必须落在 app/xyfz_auto/")
targets = ["PROJECT_ROOT", "APP_ROOT", "APP_DIR", "ROOT"]
scanned = 0
bad = []
for p in sorted((APP_ROOT / "gui").glob("*.py")):
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError as e:
        bad.append((p.name, f"语法错误 {e}"))
        continue
    for node in tree.body:                    # 只看模块级赋值
        if not isinstance(node, ast.Assign):
            continue
        for tgt in node.targets:
            if not (isinstance(tgt, ast.Name) and tgt.id in targets):
                continue
            scanned += 1
            got = eval_root_expr(node.value, p)
            if got is None:
                print(f"  · {p.name}:{node.lineno} {tgt.id} 非字面表达式, 跳过(人工确认)")
                continue
            if Path(got).resolve() != APP_ROOT:
                bad.append((p.name, f"{tgt.id} = {got} (应为 {APP_ROOT})"))
check(scanned >= 4, f"扫到 {scanned} 处根目录定义(gui/*.py)")
check(not bad, f"全部指向 app/xyfz_auto/{'' if not bad else ' —— ' + str(bad)}")

print("\n[2] 每个根下真能找到运行所需目录(空目录 = 那个功能静默失效)")
missing = []
for p in sorted((APP_ROOT / "gui").glob("*.py")):
    try:
        tree = ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError:
        continue
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in targets for t in node.targets)):
            continue
        got = eval_root_expr(node.value, p)
        if got is None:
            continue
        for sub in ("assets/tasks", "assets/templates"):
            if not (Path(got) / sub).is_dir():
                missing.append(f"{p.name}: {Path(got) / sub}")
check(not missing, f"根下 assets/tasks + assets/templates 均存在"
                   f"{'' if not missing else ' —— ' + str(missing)}")

print("\n[3] Mixin 不得自己指向 app/(少一级)以外的层级假设")
# 具体回归: 这两个文件曾经就是 parents[2] 的受害者
from gui import logrotate_mixin, taskeditor_mixin
check(taskeditor_mixin.PROJECT_ROOT == APP_ROOT,
      f"taskeditor_mixin.PROJECT_ROOT = {taskeditor_mixin.PROJECT_ROOT}")
check(logrotate_mixin.PROJECT_ROOT == APP_ROOT,
      f"logrotate_mixin.PROJECT_ROOT = {logrotate_mixin.PROJECT_ROOT}")
check(len(list((taskeditor_mixin.PROJECT_ROOT / "assets" / "tasks").glob("*.yaml"))) == task_n,
      "脚本页扫到的任务数与根目录一致(不再是 0)")

print("\n[4] GUI 侧那条链真的能列出脚本(而不是空列表)")
from engine import list_sub_scripts

one = sorted((taskeditor_mixin.PROJECT_ROOT / "assets" / "tasks").glob("*.yaml"))[0]
subs = list_sub_scripts(one, taskeditor_mixin.PROJECT_ROOT / "assets" / "tasks")
check(isinstance(subs, list), f"list_sub_scripts 正常返回({one.name} → {len(subs)} 个子脚本)")
check(all(s.get("path") and Path(s["path"]).is_file() for s in subs),
      "返回的每个子脚本路径都真实存在")

print("\n[5] 真把主窗口建起来: 脚本下拉/编辑器/步骤列表里到底有没有东西")
# 这一段是"用户视角"的复现: 用户报的就是「看不到脚本」。
# 之前 [1]~[4] 都过而界面依然空白是可能的(比如控件没被填充), 所以这里直接读控件。
from PySide6.QtWidgets import QApplication

_qt_app = QApplication.instance() or QApplication([])
from gui.main_window import MainWindow
from scripts._gui_stub import no_disclaimer, sync_background

no_disclaimer(MainWindow)      # 别弹免责声明(offscreen 下模态框会卡住)
sync_background(MainWindow)    # 后台动作改同步, 防止 QThread 悬空 → 0xC0000409
win = MainWindow()
check(win.combo_task.count() == task_n,
      f"任务下拉有 {win.combo_task.count()} 项(应为 {task_n})")
check(win.combo_task.currentText().endswith(".yaml"),
      f"默认选中了任务: {win.combo_task.currentText()!r}")
check(win.combo_edit_script.count() >= 2,
      f"「脚本」页下拉 {win.combo_edit_script.count()} 项(整个任务 + 各子脚本)")
check(win.combo_edit_script.itemText(0).startswith("整个任务"),
      f"第一项是「整个任务」: {win.combo_edit_script.itemText(0)!r}")
check(win.edit_script.toPlainText().strip() != "",
      f"脚本编辑器有内容({len(win.edit_script.toPlainText())} 字符)")
check(win.edit_script_path and Path(win.edit_script_path).is_file(),
      f"编辑器指向真实文件: {win.edit_script_path}")
check(win.list_browse.count() > 0,
      f"「脚本步骤」页列出 {win.list_browse.count()} 步")

print("\n[6] 日志滚动: 本进程自己的 run.log 必须在 skip 里(改名必失败)")
# Windows 不允许 rename 被打开的文件; GUI 的 RotatingFileHandler 常驻开着 run.log,
# 不 skip 的话那次归档每次都抛 PermissionError 被静默吞掉 —— 看起来"清了但没动静"。
act = win._active_log_names()
check("run.log" in act, f"_active_log_names 含本进程主日志, 实测 {sorted(act)}")
check(all(n.endswith(".log") for n in act), "skip 列表里都是日志文件名(不是路径)")

print()
if FAILS:
    print(f"❌ 失败 {len(FAILS)} 项:")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 模块根路径一致, 脚本可列出")
