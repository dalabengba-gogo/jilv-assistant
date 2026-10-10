# -*- coding: utf-8 -*-
"""UI 按钮注册表加载器

把 assets/ui_registry.yaml 里的按钮/界面定义加载成可查询对象，
供引擎把 {button: 角色} / {visible: 角色} / {ui: 主界面} 翻译成
坐标点击或模板匹配条件。
"""
from __future__ import annotations

from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "assets" / "ui_registry.yaml"


class _StrictLoader(yaml.SafeLoader):
    """禁止重复键的 SafeLoader。

    背景(2026-10-09 事故): 注册表里曾有两个同名「商城」按钮(主界面左列入口 vs
    主菜单面板卡片), yaml.safe_load 对重复键静默取最后一个 → click:{button:商城}
    解析到面板卡片坐标 (168,640), 在主界面上点地图让角色走动。这里把静默遮蔽
    变成显式报错, 防同类事故。
    """

    _seen_dup = None


def _construct_mapping_check_dup(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicated = key in mapping
        except TypeError:
            duplicated = False
        if duplicated:
            raise ValueError(
                f"ui_registry.yaml 存在重复键: {key!r} "
                f"(行 {key_node.start_mark.line + 1}) —— 后者会静默覆盖前者, 请改名")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping_check_dup)


class UiRegistry:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)
        with open(self.path, "r", encoding="utf-8") as f:
            data = yaml.load(f, Loader=_StrictLoader) or {}
        self.screens: dict = data.get("screens", {}) or {}

        self.buttons: dict = {}      # 真名 -> 配置
        self._alias: dict = {}       # 别名/真名 -> 真名
        for name, cfg in (data.get("buttons", {}) or {}).items():
            cfg = dict(cfg or {})
            cfg["name"] = name
            self.buttons[name] = cfg
            self._alias[name] = name
            for a in cfg.get("alias") or []:
                self._alias[a] = name

    # ---------- 按钮查询 ----------
    def get(self, name: str) -> dict | None:
        """按真名或别名查按钮配置；未知返回 None"""
        real = self._alias.get(name)
        return self.buttons.get(real) if real else None

    def names(self, screen: str | None = None) -> list[str]:
        """所有按钮名；传 screen 则只列该界面的"""
        return [n for n, c in self.buttons.items()
                if screen is None or c.get("screen") == screen]

    def resolve_click(self, name: str, mode: str = "at") -> dict:
        """解析 click 动作参数：返回 {"at":[x,y]} 或 {"template": name}
        mode: at=用注册坐标 | locate=用模板实时定位
        """
        b = self.get(name)
        if b is None:
            raise ValueError(f"注册表中没有按钮: {name} (可用: {', '.join(self.names())})")
        tpl = b.get("template")
        if mode == "locate" or (not b.get("at") and tpl):
            if not tpl:
                raise ValueError(f"按钮[{b['name']}]未配置模板，无法 locate 定位")
            return {"template": tpl}
        if not b.get("at"):
            raise ValueError(f"按钮[{b['name']}]既无 at 坐标也无模板")
        return {"at": list(b["at"])}

    def visible_condition(self, name: str) -> dict:
        """把 {visible: 按钮} 翻译成模板匹配条件"""
        b = self.get(name)
        if b is None:
            raise ValueError(f"注册表中没有按钮: {name}")
        if b.get("dynamic"):
            raise ValueError(f"按钮[{b['name']}]是动态内容(dynamic)，模板判断不可靠")
        if not b.get("template"):
            raise ValueError(f"按钮[{b['name']}]未配置模板，无法做可见性判断")
        return {"template": b["template"]}

    # ---------- 编辑(GUI 入库用) ----------
    def upsert(self, name: str, *, desc: str = "", screen: str = "",
               template: str | None = None, at: list | tuple | None = None,
               alias: list | None = None, dynamic: bool = False,
               opens: str | None = None):
        """新增或更新按钮(同名覆盖)
        opens: 该按钮点击后进入的界面名(用于 goto 自动导航/层级树)
        """
        name = name.strip()
        if not name:
            raise ValueError("按钮名不能为空")
        if opens is not None and opens.strip() and opens.strip() not in self.screens:
            raise ValueError(f"opens 指向的界面[{opens.strip()}]还没有界面判定, "
                             f"请先在「添加/更新判定…」里创建")
        old = self._alias.get(name)
        if old and old != name:
            raise ValueError(f"[{name}] 已是按钮[{old}]的别名，换一个名字")
        cfg = {"desc": desc, "screen": screen}
        if template:
            cfg["template"] = template
        if at:
            cfg["at"] = [int(at[0]), int(at[1])]
        if alias:
            cfg["alias"] = [a.strip() for a in alias if a.strip()]
        if dynamic:
            cfg["dynamic"] = True
        if opens is not None and opens.strip():
            cfg["opens"] = opens.strip()
        # 清掉旧条目的别名映射再重建
        for a in [k for k, v in self._alias.items() if v == name and k != name]:
            del self._alias[a]
        self.buttons[name] = cfg
        self._alias[name] = name
        for a in cfg.get("alias", []):
            if a in self.buttons and self.buttons[a] is not cfg:
                raise ValueError(f"别名[{a}]与已有按钮重名")
            self._alias[a] = name

    def delete(self, name: str):
        if name not in self.buttons:
            raise ValueError(f"按钮不存在: {name}")
        del self.buttons[name]
        self._alias.pop(name, None)
        for a in [k for k, v in self._alias.items() if v == name]:
            del self._alias[a]

    def save(self, path: str | Path | None = None):
        """写回 YAML(注释不保留, desc 即文档)"""
        out_path = Path(path) if path else self.path
        data = {
            "screens": self.screens,
            "buttons": {n: {k: v for k, v in cfg.items() if k != "name"}
                        for n, cfg in self.buttons.items()},
        }
        with open(out_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    def upsert_screen(self, name: str, any_of: list[str], desc: str = ""):
        """新增/更新界面判定: any_of = 模板名或按钮名列表(任一命中即在页面)"""
        name = name.strip()
        if not name:
            raise ValueError("界面名不能为空")
        refs = [r.strip() for r in any_of if r.strip()]
        if not refs:
            raise ValueError("界面至少需要一个判定模板(any_of)")
        self.screens[name] = {"desc": desc, "any_of": refs}

    def delete_screen(self, name: str):
        if name not in self.screens:
            raise ValueError(f"界面判定不存在: {name}")
        del self.screens[name]

    # ---------- 界面查询 ----------
    ROOT_SCREEN = "主界面"

    def screen_path(self, screen_name: str) -> list[str]:
        """从主界面到某界面的 opens 路径(界面名列表, 含起止); 无路径返回 None"""
        if screen_name == self.ROOT_SCREEN:
            return [screen_name]
        # BFS: 边 = 按钮.screen -> 按钮.opens
        prev: dict[str, tuple[str, str]] = {}   # 界面 -> (来源界面, 经由按钮)
        queue = [self.ROOT_SCREEN]
        seen = {self.ROOT_SCREEN}
        while queue:
            cur = queue.pop(0)
            for name, b in self.buttons.items():
                nxt = b.get("opens")
                if not nxt or nxt in seen or b.get("screen") != cur:
                    continue
                seen.add(nxt)
                prev[nxt] = (cur, name)
                if nxt == screen_name:
                    path = [nxt]
                    while path[-1] != self.ROOT_SCREEN:
                        frm, via = prev[path[-1]]
                        path.append(frm)
                    return list(reversed(path))
                queue.append(nxt)
        return None

    def navigation_plan(self, target_screen: str) -> list[str]:
        """计算 goto:{ui:target} 要依次点击的按钮名列表(从主界面出发)

        依据: 每个入口按钮的 opens 字段 + 目标界面的 screen_path。
        目标就是主界面 → 返回空表(假定当前已在主界面)。
        找不到路径 → 抛 ValueError。
        """
        if target_screen not in self.screens:
            raise ValueError(f"注册表中没有界面: {target_screen} "
                             f"(可用: {', '.join(self.screens)})")
        if target_screen == self.ROOT_SCREEN:
            return []
        path = self.screen_path(target_screen)
        if path is None:
            raise ValueError(
                f"从[{self.ROOT_SCREEN}]到[{target_screen}]没有 opens 导航路径, "
                f"请给入口按钮补 opens 字段")
        plan = []
        for i in range(len(path) - 1):
            frm, to = path[i], path[i + 1]
            btn = next((n for n, b in self.buttons.items()
                        if b.get("screen") == frm and b.get("opens") == to), None)
            if btn is None:
                raise ValueError(f"[{frm}]→[{to}] 之间没有配置 opens 入口按钮")
            plan.append(btn)
        return plan

    def screen_condition(self, screen_name: str) -> dict:
        """把 {ui: 界面} 翻译成识别条件

        any_of: 模板任一命中即算该界面
        not_screens: (2026-09-23 新增) 同时这些界面的特征都不得命中。
            背景: 「主界面」只查底栏按钮, 战斗过渡期底栏短暂可见被误判,
            引发域外批次死循环 8h。主界面现配 not_screens:[战斗中],
            生成 all_of:[any(底栏), not(战斗特征)]。
        """
        scr = self.screens.get(screen_name)
        if not scr:
            raise ValueError(f"注册表中没有界面: {screen_name} "
                             f"(可用: {', '.join(self.screens)})")
        conds = []
        for ref in scr.get("any_of", []):
            b = self.get(ref)
            if b and b.get("template"):
                conds.append({"template": b["template"]})
            else:
                # 也允许直接写模板文件名(不经过按钮注册)
                conds.append({"template": ref})
        if not conds:
            raise ValueError(f"界面[{screen_name}]没有可判定的模板特征")
        cond = {"any": conds}
        not_conds = []
        for ref in scr.get("not_screens", []):
            sub = self.screens.get(ref)
            if not sub:
                raise ValueError(f"界面[{screen_name}]的 not_screens 引用了"
                                 f"未定义界面: {ref}")
            not_conds.append(self.screen_condition(ref))
        if not_conds:
            return {"all_of": [cond] + [{"not": n} for n in not_conds]}
        return cond
