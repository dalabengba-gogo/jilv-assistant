# -*- coding: utf-8 -*-
"""注册表 / 脚本引用一致性检查  (2026-10-09 采集时新增)

背景:
    「每日必领」段⑤ 曾引用未注册的按钮和界面, 且明明用了 mode: locate,
    对应按钮却没有 template —— 运行到该步直接抛 ValueError, 整条日常报废。
    这个脚本在真正跑之前, 把这类"名字写错 / 借了没登记的模板"提前扫出来。

用法:
    runtime\\python.exe scripts\\check_refs.py

检查项:
    1. ui_registry.yaml 能否加载(同名键会被 StrictLoader 直接拦下)
    2. 脚本里 click/visible 的 button 名、wait/goto 的 ui 名能否解析
    3. 凡是被 mode: locate 引用的按钮, 是否真的配了 template
    4. 每个任务脚本能否被 loader 正常展开加载(include/loop/goto 全过一遍)
    5. goto / jump 指向的步骤名是否存在
"""
import os
import sys
import glob

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import yaml                                  # noqa: E402
from engine.ui_registry import UiRegistry    # noqa: E402
from engine import loader                    # noqa: E402


def walk(o):
    """递归产出所有 (key, value)"""
    if isinstance(o, dict):
        for k, v in o.items():
            yield k, v
            yield from walk(v)
    elif isinstance(o, list):
        for i in o:
            yield from walk(i)


def main():
    print("=" * 62)
    print(" 鸡驴助手 注册表 / 脚本引用检查")
    print("=" * 62)

    # ---------- 1. 注册表 ----------
    try:
        reg = UiRegistry()
    except Exception as e:
        print("❌ 注册表加载失败:", e)
        return 1
    print("✅ ui_registry.yaml 加载成功: 按钮 %d 个, 界面 %d 个"
          % (len(reg.buttons), len(reg.screens)))

    files = sorted(set(glob.glob("assets/tasks/*.yaml")
                       + glob.glob("assets/tasks/**/*.yaml", recursive=True)))
    # tests/ 是自检样例, 允许写占位名, 但仍参与扫描
    task_files = [f for f in files if os.sep + "tests" + os.sep not in f]

    bad_btn, bad_ui, bad_tpl, n_btn = [], [], set(), 0
    for f in files:
        try:
            data = yaml.safe_load(open(f, encoding="utf-8"))
        except Exception as e:
            bad_btn.append((f, "YAML 解析失败: %s" % e))
            continue
        if not isinstance(data, dict) or not isinstance(data.get("steps"), list):
            continue
        for k, v in walk(data["steps"]):
            if k == "button" and isinstance(v, str):
                n_btn += 1
                if reg.get(v) is None:
                    bad_btn.append((f, v))
            if k == "ui" and isinstance(v, str) and v not in reg.screens:
                bad_ui.append((f, v))
            # locate 模式专项: {button: X, mode: locate} 必须有 template
            if isinstance(v, dict) and v.get("mode") == "locate" \
                    and isinstance(v.get("button"), str):
                cfg = reg.get(v["button"])
                if cfg and not cfg.get("template"):
                    bad_tpl.add((f, v["button"]))

    print("\n--- 引用扫描: %d 个脚本, %d 处 button 引用 ---" % (len(files), n_btn))
    if bad_btn:
        print("❌ 引用了注册表里不存在的按钮:")
        for f, n in bad_btn:
            print("     %s -> button: %s" % (os.path.basename(f), n))
    else:
        print("✅ button 引用全部可解析")
    if bad_ui:
        print("❌ 引用了未定义的界面:")
        for f, n in bad_ui:
            print("     %s -> ui: %s" % (os.path.basename(f), n))
    else:
        print("✅ ui 引用全部可解析")
    if bad_tpl:
        print("❌ mode: locate 但按钮没配 template (运行时会直接报错):")
        for f, n in sorted(bad_tpl):
            print("     %s -> %s" % (os.path.basename(f), n))
    else:
        print("✅ 所有 locate 引用的按钮都已配 template")

    # ---------- 2. 端到端加载 ----------
    print("\n--- 端到端加载 ---")
    ok = 0
    for f in task_files:
        try:
            t = loader.load_task(f)
            names = [s.name for s in t.steps]
            over = set()
            for s in t.steps:
                fa = getattr(s, "on_fail", None)
                if isinstance(fa, dict) and isinstance(fa.get("goto"), str):
                    over.add(fa["goto"])
            miss = [x for x in over if x not in names]
            print("   %s %-34s %4d 步 %s"
                  % ("✅" if not miss else "⚠️", os.path.basename(f), len(names),
                     ("goto 目标缺失 " + str(miss)) if miss else ""))
            ok += 1
        except Exception as e:
            print("   ❌ %-34s 加载失败: %s" % (os.path.basename(f), e))
    print("   成功 %d / %d" % (ok, len(task_files)))

    problems = len(bad_btn) + len(bad_ui) + len(bad_tpl)
    print("\n" + ("✅ 全部通过" if problems == 0 and ok == len(task_files)
                  else "⚠️  有 %d 处问题, 见上" % problems))
    return 0 if problems == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
