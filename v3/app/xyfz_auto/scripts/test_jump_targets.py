# -*- coding: utf-8 -*-
"""goto / jump 目标与"循环生成名"解耦的测试(不依赖模拟器)

批次六 v4.9.0 P2-4 新增。背景: loop 展开时步骤名会带上 `(i/N)`, 脚本里写死的
跳转目标一旦遇到"把 loop 次数从 6 改成 8"就全部失效(运行到那步直接报
「goto 目标非法」整条任务终止)。loader 现在加载后统一解析一次, 验证:

  1. 目标已存在 → 原样不动(零行为变化)
  2. `X(2/5)` 而实际是 `X(2/3)` → 解析到同一轮次
  3. 只写裸名 `X` 而实际只有 `X(1/3)` → 解析到第一轮
  4. 轮次越界(写 `(7/7)` 而循环只有 3 轮) → **不动**(宁可报错也别跳到别处)
  5. on_success / action:jump 两种写法同样生效
  6. 解析不出来的目标保持原样(_validate 会报出来, 不静默吞)
  7. 真实 13 个任务: 所有跳转目标都能解析, 且没有"重名导致跳首个"的引用
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

FAILS = []


def check(cond, msg):
    print(f"  {'✔' if cond else '✘'} {msg}")
    if not cond:
        FAILS.append(msg)


TMP = tempfile.mkdtemp(prefix="jump_")
from engine.loader import load_task
import glob


def load_case(name: str, body: str):
    p = os.path.join(TMP, name + ".yaml")
    with open(p, "w", encoding="utf-8") as f:
        f.write("name: %s\nsettings: {default_timeout: 2, default_retry: 0}\nsteps:\n"
                % name + body)
    return load_task(p)


print("[1] 目标已存在 → 原样不动")
t = load_case("exact", """\
  - name: A
    wait: {ocr: x}
    on_fail: {goto: B}
  - name: B
    wait: {ocr: y}
""")
check(t.index_of("A") == 0 and (t.steps[0].on_fail or {}).get("goto") == "B",
      "精确目标没被改写")

print("\n[2] 循环次数变了: X(2/5) → X(2/3)")
t = load_case("renumber", """\
  - name: 循环
    loop: 3
    steps:
      - name: X
        wait: {ocr: x}
  - name: 收尾
    wait: {ocr: y}
    on_fail: {goto: X(2/5)}
""")
names = [s.name for s in t.steps]
target = t.steps[-1].on_fail.get("goto")
check("X(2/3)" in names, f"loop 展开名带轮次, 实测 {names}")
check(target == "X(2/3)", f"跳到同一轮次, 实测 {target}")
check(t.index_of(target) >= 0, "index_of 能找到")

print("\n[3] 只写裸名 → 循环第一轮")
t = load_case("bare", """\
  - name: 收尾
    wait: {ocr: y}
    on_fail: {goto: 某步}
  - name: 循环
    loop: 3
    steps:
      - name: 某步
        wait: {ocr: x}
""")
check(t.steps[0].on_fail.get("goto") == "某步(1/3)",
      f"裸名解析到第一轮, 实测 {t.steps[0].on_fail.get('goto')}")

print("\n[4] 轮次越界 → 不动(不猜)")
t = load_case("outrange", """\
  - name: 循环
    loop: 3
    steps:
      - name: X
        wait: {ocr: x}
  - name: 收尾
    wait: {ocr: y}
    on_fail: {goto: X(7/7)}
""")
check(t.steps[-1].on_fail.get("goto") == "X(7/7)",
      f"越界目标保持原样, 实测 {t.steps[-1].on_fail.get('goto')}")
check(t.index_of("X(7/7)") == -1, "index_of 返回 -1 → 运行时会明确报错(不静默跑偏)")

print("\n[5] on_success / action:jump 同样解析")
t = load_case("forms", """\
  - name: 监控
    wait: {ocr: x}
    on_success: {goto: 点掉(1/9)}
  - name: 跳转
    action: {type: jump, target: 点掉}
  - name: 循环
    loop: 2
    steps:
      - name: 点掉
        click: [10, 20]
""")
check(t.steps[0].on_success.get("goto") == "点掉(1/2)",
      f"on_success 被解析, 实测 {t.steps[0].on_success.get('goto')}")
check(t.steps[1].action.get("target") == "点掉(1/2)",
      f"action:jump 被解析, 实测 {t.steps[1].action.get('target')}")

print("\n[6] 解析不出来的目标保持原样(交给 _validate 报错)")
t = load_case("bogus", """\
  - name: A
    wait: {ocr: x}
    on_fail: {goto: 根本没有这一步}
""")
check(t.steps[0].on_fail.get("goto") == "根本没有这一步", "错字目标不会被乱猜")

print("\n[7] 真实任务: 跳转目标全部可解析, 无重名歧义")
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(APP)
bad, ambig, n = [], [], 0
for f in sorted(glob.glob("assets/tasks/*.yaml")):
    task = load_task(f)
    names = [s.name for s in task.steps]
    counts = {}
    for nm in names:
        counts[nm] = counts.get(nm, 0) + 1
    refs = set()
    for s in task.steps:
        for holder in ("on_fail", "on_success"):
            v = getattr(s, holder, None)
            if isinstance(v, dict) and isinstance(v.get("goto"), str):
                refs.add(v["goto"])
        a = s.action if isinstance(s.action, dict) else {}
        if a.get("type") == "jump" and isinstance(a.get("target"), str):
            refs.add(a["target"])
    n += len(refs)
    bad += [(os.path.basename(f), r) for r in refs if r not in counts]
    ambig += [(os.path.basename(f), r) for r in refs if counts.get(r, 0) > 1]
check(not bad, f"{n} 处跳转目标全部存在(缺失: {bad[:5]})")
check(not ambig, f"没有指向重名步骤的跳转(歧义: {ambig[:5]})")

import shutil
shutil.rmtree(TMP, ignore_errors=True)
print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
