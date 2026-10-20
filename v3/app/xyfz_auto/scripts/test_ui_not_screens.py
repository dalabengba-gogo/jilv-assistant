# -*- coding: utf-8 -*-
# 回归测试: {ui:主界面} 的 not_screens 必须排除所有"共享底栏"的子面板。
#
# 背景(2026-10-19): 背包/角色/坐骑/召唤兽/仓库 这些子面板也显示底部菜单栏,
# 而 主界面 只靠 any_of[底栏按钮] 判定 → 在子面板里会被误判成主界面, 任务从子页面
# 起跑时整轮空转却不报错。修复方式是给 主界面 的 not_screens 加上这些面板。
#
# 本测试锁定: screen_condition('主界面') 生成的条件树里, 必须包含对这 5 个面板 +
# 战斗 的否定, 且 any_of 仍是底栏三按钮。结构正确即保证判定正确(引擎的 not 求值已
# 由"战斗中"用例在生产环境佐证)。
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.ui_registry import UiRegistry


def _collect_not_templates(cond):
    """递归收集 all_of 里所有 not 子条件引用的模板名(去重)。"""
    out = set()
    if not isinstance(cond, dict):
        return out
    if "not" in cond:
        sub = cond["not"]
        if "template" in sub:
            out.add(sub["template"])
        if "any" in sub:
            for a in sub["any"]:
                if isinstance(a, dict) and "template" in a:
                    out.add(a["template"])
    if "all_of" in cond:
        for c in cond["all_of"]:
            out |= _collect_not_templates(c)
    return out


def main():
    ui = UiRegistry()
    cond = ui.screen_condition("主界面")

    # 1) 顶层必须是 all_of
    assert isinstance(cond, dict) and "all_of" in cond, \
        f"主界面 条件应为 all_of, 实际: {json.dumps(cond, ensure_ascii=False)}"

    # 2) any_of 仍是底栏三按钮
    any_part = next((c for c in cond["all_of"]
                     if isinstance(c, dict) and "any" in c), None)
    assert any_part is not None, "主界面 缺少 any_of(底栏按钮)"
    any_templates = set()
    for a in any_part["any"]:
        if isinstance(a, dict) and "template" in a:
            any_templates.add(a["template"])
    for need in ("bottom_btn_role", "bottom_btn_bag", "bottom_btn_mount"):
        assert need in any_templates, f"主界面 any_of 缺少 {need}"

    # 3) not_screens 必须排除 5 个面板 + 战斗
    not_tpls = _collect_not_templates(cond)
    required = {
        "panel_role_title",      # 角色面板
        "panel_bag_title",       # 背包面板
        "panel_mount_title",     # 坐骑面板
        "panel_pet_title",       # 召唤兽面板
        "panel_warehouse_title",  # 仓库页
        "battle_flag_auto",      # 战斗中(战斗旗标)
        "battle_btn_flee",       # 战斗中(逃跑钮)
    }
    missing = required - not_tpls
    assert not missing, f"主界面 not_screens 缺少否定: {missing}"

    # 4) 反向: 这些面板本身不应出现在 any_of(避免自我否定失效)
    assert not (required & any_templates), "主界面 any_of 不应包含面板/战斗模板"

    print("✅ 主界面 not_screens 正确排除了 角色/背包/坐骑/召唤兽/仓库 面板 + 战斗")
    print(f"   any_of(底栏): {sorted(any_templates)}")
    print(f"   not(排除)模板数: {len(not_tpls)}")


if __name__ == "__main__":
    main()
