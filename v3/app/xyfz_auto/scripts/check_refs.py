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
    5. goto / jump(含 on_success)指向的步骤名是否存在、是否重名歧义
    6. assets/templates/_meta.txt 里的模板名是否存在、阈值是否在 0~1
    7. 脚本里的 threshold 是否在 (0,1] 区间
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
    jump_miss, jump_ambig, bad_th = [], [], []
    for f in task_files:
        try:
            t = loader.load_task(f)
            names = [s.name for s in t.steps]
            counts = {}
            for n in names:
                counts[n] = counts.get(n, 0) + 1
            refs = set()
            for s in t.steps:
                fa = getattr(s, "on_fail", None)
                if isinstance(fa, dict) and isinstance(fa.get("goto"), str):
                    refs.add(fa["goto"])
                sa = getattr(s, "on_success", None)
                if isinstance(sa, dict) and isinstance(sa.get("goto"), str):
                    refs.add(sa["goto"])
                act = getattr(s, "action", None)
                if isinstance(act, dict) and act.get("type") == "jump" \
                        and isinstance(act.get("target"), str):
                    refs.add(act["target"])
                th = float(getattr(s, "threshold", 0) or 0)
                if th and not (0 < th <= 1):
                    bad_th.append((os.path.basename(f), s.name, th))
            miss = sorted(r for r in refs if r not in counts)
            ambig = sorted(r for r in refs if counts.get(r, 0) > 1)
            jump_miss += [(os.path.basename(f), m) for m in miss]
            jump_ambig += [(os.path.basename(f), a, counts[a]) for a in ambig]
            print("   %s %-34s %4d 步 %s"
                  % ("✅" if not (miss or ambig) else "⚠️", os.path.basename(f),
                     len(names),
                     ("goto 目标缺失 " + str(miss)) if miss else
                     (f"goto 目标重名(跳首个) {ambig}" if ambig else "")))
            ok += 1
        except Exception as e:
            print("   ❌ %-34s 加载失败: %s" % (os.path.basename(f), e))
    print("   成功 %d / %d" % (ok, len(task_files)))

    # ---------- 3. 模板阈值表 ----------
    print("\n--- 模板阈值 (_meta.txt) ---")
    tpl_dir = os.path.join("assets", "templates")
    meta = os.path.join(tpl_dir, "_meta.txt")
    bad_meta = []
    if os.path.exists(meta):
        from recognition.template import TemplateMatcher
        m = TemplateMatcher(tpl_dir)
        have = set(m.templates)
        for name, th in sorted(m.thresholds.items()):
            if name not in have:
                bad_meta.append((name, th, "模板不存在"))
            elif not (0 < th <= 1):
                bad_meta.append((name, th, "阈值越界"))
        print("   %s 解析到 %d 条模板阈值, 模板库共 %d 张"
              % ("✅" if not bad_meta else "❌", len(m.thresholds), len(have)))
        for name, th, why in bad_meta:
            print("      %s = %g  -> %s" % (name, th, why))
        if m.load_errors:
            print("   ⚠️ 有 %d 张模板读不进来(中文路径/文件损坏): %s"
                  % (len(m.load_errors), m.load_errors[:5]))
    else:
        print("   · 没有 _meta.txt, 全部模板走全局 match_threshold")

    if bad_th:
        print("\n❌ 脚本里的 threshold 不在 (0,1]:")
        for fn, sn, th in bad_th[:10]:
            print("      %s -> [%s] threshold=%g" % (fn, sn, th))
    else:
        print("✅ 脚本里的 threshold 全部合法(或未设置)")

    # ---------- 4. 条件探测步审计 (2026-10-19 v5.4.0 / P0-1) ----------
    # 「若…则…」这类"游戏自己决定出不出现"的步骤, 没标 optional 就会在每轮
    # 结束时白报一条「⚠ 步骤未做成」(实测一轮 daily_all 有 6 条以上, 把真失败
    # 淹掉了)。这里只提示、不计入 problems —— 清单为 0 才是常态, 有候选时
    # 作者逐条确认后补 optional 或加进 check_optional.KNOWN_OK。
    print("\n--- 条件探测步 (「若…则…」有动作但未标 optional) ---")
    try:
        import check_optional
        cand = check_optional.collect(quiet=True)
        if not cand:
            print("   ✅ 没有未标注的候选")
        else:
            print("   ⚠️  %d 个候选(不阻断, 建议补 optional: true):" % len(cand))
            for fn, idx, name in cand[:12]:
                print("      %s %s  %s" % (fn, idx, name))
            if len(cand) > 12:
                print("      … 另有 %d 个, 跑 scripts\\check_optional.py 看全表"
                      % (len(cand) - 12))
    except Exception as e:                    # 审计自身出错不能拖垮 check_refs
        print("   · 审计跳过: %s" % e)

    problems = len(bad_btn) + len(bad_ui) + len(bad_tpl) + len(jump_miss) \
        + len(jump_ambig) + len(bad_meta) + len(bad_th)
    print("\n" + ("✅ 全部通过" if problems == 0 and ok == len(task_files)
                  else "⚠️  有 %d 处问题, 见上" % problems))
    return 0 if problems == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
