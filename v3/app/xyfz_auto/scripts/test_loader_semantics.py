# -*- coding: utf-8 -*-
"""桩测试: loader 语义 —— include 展开/环检测 + goto 与 jump 的区别 (P2-2)

方案里点名的 5 个桩测试, 本文件补其中两个(另外三个已在
test_failure_accounting.py: 失败记账分类 / settle deadline / required 生效)。

验证:
  1. include 嵌套展开(父 → 子 → 孙), 步骤顺序正确、子步骤带上 __src__
  2. **成环**检测: A→B→A 报"include 成环"并点名文件(旧版只报"超过 5 层")
  3. 自环(A include 自己)同样被拦
  4. goto(界面导航) 与 jump(步骤跳转) 是两种不同的 action
  5. on_fail/on_success 的 goto 目标是**步骤名**, 与顶层 goto 不混淆
  6. _validate 能报出「跳转目标不存在」和「跳转目标重名」
"""
import os
import sys
import shutil
import tempfile

os.chdir(os.path.dirname(os.path.abspath(__file__)) + "/..")
sys.path.insert(0, os.getcwd())

import yaml

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


from engine.loader import _expand_includes, _shorthand_action, load_task
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="loader_"))


def write(rel, obj):
    p = TMP / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(obj, allow_unicode=True), encoding="utf-8")
    return p


print("[1] include 三层嵌套展开 + __src__ 标记")
write("grand.yaml", {"steps": [{"name": "孙步骤", "sleep": 1}]})
write("child.yaml", {"steps": [{"name": "子步骤A", "sleep": 1},
                              {"include": "grand.yaml"},
                              {"name": "子步骤B", "sleep": 1}]})
write("root.yaml", {"steps": [{"name": "父前", "sleep": 1},
                             {"include": "child.yaml"},
                             {"name": "父后", "sleep": 1}]})
steps = _expand_includes(yaml.safe_load((TMP / "root.yaml").read_text(encoding="utf-8"))["steps"],
                         TMP)
names = [s.get("name") for s in steps]
check(names == ["父前", "子步骤A", "孙步骤", "子步骤B", "父后"],
      f"展开顺序正确(深度优先、原地内联), 实测 {names}")
srcs = [s.get("__src__") for s in steps]
check(srcs[0] == "" and "child.yaml" in srcs[1] and "grand.yaml" in srcs[2],
      f"子步骤带上了声明文件的 __src__, 实测 {srcs}")

print("\n[2] 成环 A → B → A → 报『include 成环』并点名文件")
write("cyc_a.yaml", {"steps": [{"include": "cyc_b.yaml"}]})
write("cyc_b.yaml", {"steps": [{"include": "cyc_a.yaml"}]})
try:
    _expand_includes(yaml.safe_load((TMP / "cyc_a.yaml").read_text(encoding="utf-8"))["steps"],
                     TMP)
    check(False, "成环应当抛错, 却正常返回了")
except ValueError as e:
    msg = str(e)
    check("成环" in msg, f"错误信息点明是成环(不是'超过 5 层'), 实测: {msg}")
    check("cyc_a.yaml" in msg and "cyc_b.yaml" in msg, f"点名了涉及的文件: {msg}")

print("\n[3] 自环(A include 自己)")
write("self.yaml", {"steps": [{"include": "self.yaml"}]})
try:
    _expand_includes(yaml.safe_load((TMP / "self.yaml").read_text(encoding="utf-8"))["steps"],
                     TMP)
    check(False, "自环应当抛错")
except ValueError as e:
    check("成环" in str(e), f"自环也被拦下: {str(e)[:60]}")

print("\n[4] 合法的多处引用同一文件不算环(DAG)")
write("leaf.yaml", {"steps": [{"name": "公共收尾", "sleep": 1}]})
write("d1.yaml", {"steps": [{"include": "leaf.yaml"}, {"name": "d1独有"}]})
write("d2.yaml", {"steps": [{"name": "d2独有"}, {"include": "leaf.yaml"}]})
write("dag.yaml", {"steps": [{"include": "d1.yaml"}, {"include": "d2.yaml"}]})
try:
    s = _expand_includes(yaml.safe_load((TMP / "dag.yaml").read_text(encoding="utf-8"))["steps"], TMP)
    n = [x.get("name") for x in s]
    check(n == ["公共收尾", "d1独有", "d2独有", "公共收尾"],
          f"菱形引用正常展开两次(不是环), 实测 {n}")
except ValueError as e:
    check(False, f"DAG 被误判成环: {e}")

print("\n[5] goto(界面导航) 与 jump(步骤跳转) 是两种 action")
a1 = _shorthand_action({"goto": "主界面"})
a2 = _shorthand_action({"goto": {"ui": "主界面"}})
a3 = _shorthand_action({"jump": "收尾"})
check(a1 == {"type": "goto_ui", "screen": "主界面"}, f"goto: 名字 → {a1}")
check(a2 == {"type": "goto_ui", "screen": "主界面"}, f"goto: {{ui: 名字}} → {a2}")
check(a3 == {"type": "jump", "target": "收尾"}, f"jump: 名字 → {a3}")
check(a1["type"] != a3["type"], "两种语义 type 不同(界面导航 vs 步骤跳转)")

print("\n[6] _validate: 跳转目标不存在 / 目标重名")
import logging
from engine.loader import _validate
from engine.model import Step, Task

logging.disable(logging.WARNING)


def mk(name, **kw):
    return Step(name=name, action=kw.get("action"), wait=kw.get("wait"),
                on_fail=kw.get("on_fail"), on_success=kw.get("on_success"))


# 目标不存在
t = Task(name="T", steps=[mk("A", action={"type": "jump", "target": "不存在的步骤"})])
buf = []
real = sys.stderr
sys.stderr = type("W", (), {"write": lambda s, x: buf.append(x), "flush": lambda s: None})()
try:
    _validate(t)
finally:
    sys.stderr = real
out = "".join(buf)
check("不存在的步骤" in out, f"报出了不存在的跳转目标, 实测: {out.strip()[:70]}")

# 目标重名(会被跳到首个同名)
t2 = Task(name="T2", steps=[
    mk("收尾", sleep=1),
    mk("X", action={"type": "jump", "target": "收尾"}),
    mk("收尾", sleep=1),
])
buf2 = []
sys.stderr = type("W", (), {"write": lambda s, x: buf2.append(x), "flush": lambda s: None})()
try:
    _validate(t2)
finally:
    sys.stderr = real
out2 = "".join(buf2)
check("重名" in out2, f"报出了重名风险(只报'重名且被引用'的), 实测: {out2.strip()[:70]}")

# 重名但没被引用 → 不报(避免"停顿2秒×10"淹没真问题)
t3 = Task(name="T3", steps=[mk("停顿", sleep=1), mk("停顿", sleep=1)])
buf3 = []
sys.stderr = type("W", (), {"write": lambda s, x: buf3.append(x), "flush": lambda s: None})()
try:
    _validate(t3)
finally:
    sys.stderr = real
check("重名" not in "".join(buf3), "未被引用的重名不报(不刷屏)")

shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
