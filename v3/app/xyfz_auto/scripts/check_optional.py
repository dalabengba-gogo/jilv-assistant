# -*- coding: utf-8 -*-
"""条件探测步骤审计  (2026-10-19 v5.4.0 新增, P0-1)

背景:
    v5.3.0 定下规矩: **等待超时不算真失败** —— 但那条规则只覆盖"纯等待(无动作)"
    的步骤。现实里大量探测步是「若弹了弹窗就点掉」这种**带 click 动作**的写法:
        - name: 若还有结算弹窗再点一次
          wait: {any: [{ocr: 奖励总览}, {ocr: 获得奖励}]}
          click: [450, 700]
          timeout: 8
    没弹窗 → 等待超时 → 有动作 → 走真失败通道 → 每轮白报一条
    「⚠ 步骤未做成」。实测一轮 daily_all 里这类假失败有 6 条以上, 把真正的
    失败淹掉了; 用户看到"有 N 步未做成"第一反应就是"是不是漏了"。

    正确写法是给这类步加 `optional: true`(条件探测: 没这弹窗属正常)。
    但全项目近百个步骤靠人肉记不住 —— 本脚本把**疑似候选**扫出来, 打印清单,
    由作者逐条确认。**它不报错、不阻断打包**(误报会让人关掉它), 只提示。

用法:
    runtime\\python.exe scripts\\check_optional.py          # 列出候选
    runtime\\python.exe scripts\\check_optional.py --all    # 连已标 optional 的也列

判据(全部满足才算候选):
    1. 步骤名含 若 / 如果 / 如果有
    2. 有 wait 条件(真在判断某个东西在不在)
    3. 有动作(click/click_center/swipe)  —— 纯 wait 的已经被 v5.3.0 软通道覆盖
    4. 没标 optional, 也没标 required

白名单: 见下面 KNOWN_OK —— 确认过"这一步失败了确实说明有问题"的步骤名,
        写进来就不再提示(例如"若弹队伍申请则拒绝"漏了就真会卡住流程, 不标)。
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import glob                                   # noqa: E402
from engine import loader                     # noqa: E402

# 步骤名前缀命中这些词的, 视为"失败了也是真问题", 不走条件探测白名单
# (留空表示暂无; 真遇到再往这里加)
KNOWN_OK: set[str] = set()

NAME_HINTS = ("若", "如果")
ACTION_KEYS = ("click", "click_center", "swipe", "tap")


def has_action(step) -> bool:
    """这一步除了 wait 之外, 是否还会动手(点/滑)"""
    for k in ACTION_KEYS:
        v = getattr(step, k, None)
        if v:
            return True
    # 简写动作(click: {ocr: X} / click: [x,y]) 在 loader 里已归一化成 step.action
    return bool(getattr(step, "action", None))


def collect(show_all: bool = False, quiet: bool = False) -> list[tuple[str, str, str]]:
    """扫描所有任务, 返回 (文件名, #序号, 步骤名) 候选列表。

    抽成独立函数是为了让 check_refs 能复用(只取数量, 不打印整张表)。
    """
    base = os.path.join("assets", "tasks")
    files = sorted(glob.glob(os.path.join(base, "*.yaml")))
    cands: list[tuple[str, str, str]] = []
    for f in files:
        try:
            task = loader.load_task(f)
        except Exception as e:                      # 加载失败交给 check_refs 报
            if not quiet:
                print(f"  (跳过 {os.path.basename(f)}: {e})")
            continue
        for i, step in enumerate(task.steps, 1):
            name = step.name or ""
            if not any(h in name for h in NAME_HINTS):
                continue
            if not getattr(step, "wait", None):
                continue
            if not has_action(step):
                continue                            # 纯等待: v5.3.0 已覆盖
            opt = bool(getattr(step, "optional", False))
            if opt and not show_all:
                continue
            if name.strip() in KNOWN_OK:
                continue
            cands.append((os.path.basename(f), f"#{i}", name.strip()))
    return cands


def main() -> int:
    show_all = "--all" in sys.argv
    cands = collect(show_all=show_all)
    print("=" * 68)
    print("条件探测步骤审计 (「若…则…」类)")
    print("=" * 68)
    if not cands:
        print("✔ 没有未标注的候选 —— 所有「若…则…」类步骤都已声明 optional")
        return 0
    total = len(cands)
    print(f"\n发现 {total} 个疑似条件探测步, 未标 optional:\n")
    cur = None
    for fn, idx, name in cands:
        if fn != cur:
            print(f"  [{fn}]")
            cur = fn
        print(f"    {idx:>6}  {name}")
    print(f"""
怎么处理:
  · 确认是"有就做、没有就跳过"  → 在该步加一行  optional: true
  · 确认"这一步失败说明真有问题" → 把步骤名加进本脚本的 KNOWN_OK
  · 判定标准: 弹窗/浮层/横幅这类**游戏自己决定出不出现**的东西 → optional
              需要玩家主动点才出现的界面 → 不是 optional(没点上就是真失败)

共 {total} 条待确认(不影响打包, 仅提示)
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
