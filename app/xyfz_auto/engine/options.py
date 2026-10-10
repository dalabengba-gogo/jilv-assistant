# -*- coding: utf-8 -*-
"""脚本选项 schema 发现 (向 GUI「脚本选项」页与 loader 提供)

2026-10-07: 选项**按脚本归属**, 声明写在各脚本自己的 yaml 里:

    # assets/tasks/parts/daily_dixia_3x.yaml
    options:
      script: parts/daily_dixia_3x.yaml   # ★ 归属脚本(可省, 省=声明所在的这个文件)
      title: 地煞星                        # 界面上显示的名字
      desc: 挑战星级选择                    # 可选, 副标题
      items:
        - key: star_row                    # config.yaml 里的键
          section: dixia                   # config.yaml 里的节
          label: 挑战星级                   # 表单行标题
          type: choice                     # choice | bool | int
          default: "auto"                  # 缺省值(config 缺键时 GUI/loader 都用它)
          choices:
            - {text: "自动(默认)", value: "auto"}
            - {text: "第1行", value: "row1"}
          help: 说明文字(灰色小字)

脚本里用 {{cfg:dixia.star_row}} 引用(见 engine.loader._substitute_cfg)。

界面表现: 「脚本选项」页左侧列出**当前任务里的所有脚本**(按 include 顺序),
选中哪个脚本, 右边就只显示那个脚本的可调选项 —— 新增脚本时把它 include 进来
(或单独运行), 界面自动多出一项, **不用改 GUI 代码**。

script: 字段的用处: 选项块可以放在任意文件里(比如集中在 daily_all.yaml 里写),
用 script: 指向它真正服务的脚本; 缺省时就是"本文件"。
"""
from __future__ import annotations

from pathlib import Path

import yaml

MAX_DEPTH = 5
VALID_TYPES = ("choice", "bool", "int")


def _load_yaml(p: Path) -> dict:
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _normalize_item(raw: dict) -> dict | None:
    """把 yaml 里的一条选项规范成内部结构; 缺关键字段则丢弃(返回 None)"""
    if not isinstance(raw, dict):
        return None
    key = raw.get("key")
    section = raw.get("section")
    if not key or not section:
        return None
    typ = str(raw.get("type", "choice")).lower()
    if typ not in VALID_TYPES:
        typ = "choice"
    item = {
        "key": str(key),
        "section": str(section),
        "label": str(raw.get("label") or key),
        "type": typ,
        "default": raw.get("default"),
        "help": str(raw.get("help") or ""),
    }
    if typ == "choice":
        choices = []
        for c in (raw.get("choices") or []):
            if isinstance(c, dict) and "value" in c:
                choices.append({"text": str(c.get("text", c["value"])),
                                "value": c["value"]})
            elif c is not None:
                choices.append({"text": str(c), "value": c})
        if not choices:
            return None                      # 没有候选的下拉框没意义
        item["choices"] = choices
        if item["default"] is None:
            item["default"] = choices[0]["value"]
    elif typ == "bool":
        item["default"] = bool(item["default"]) if item["default"] is not None else False
    else:                                    # int
        for k, dv in (("min", 0), ("max", 9999), ("step", 1)):
            try:
                item[k] = int(raw.get(k, dv))
            except Exception:
                item[k] = dv
        if item["step"] < 1:
            item["step"] = 1
        try:
            item["default"] = int(item["default"]) if item["default"] is not None else item["min"]
        except Exception:
            item["default"] = item["min"]
        item["unit"] = str(raw.get("unit") or "")
    return item


def _resolve_script(declared: str, decl_file: Path, root: Path) -> str:
    """把 options.script 解析成相对任务目录的脚本路径(解析不到就原样保留)"""
    v = str(declared).strip().replace("\\", "/")
    cands = [decl_file.parent / v, Path(root) / v]
    for c in cands:
        try:
            if c.is_file():
                return str(c.resolve().relative_to(Path(root).resolve())).replace("\\", "/")
        except Exception:
            continue
    return v


def collect_options(task_path: str | Path,
                    tasks_dir: str | Path | None = None,
                    include_empty: bool = True) -> list[dict]:
    """按 include 顺序收集任务里**每个脚本**的选项分组

    include_empty=True 时, 没有 options: 段的脚本也会返回(items=[]),
    界面据此列出"当前任务的所有脚本"。

    返回 [{script, path, title, desc, items:[...]}, ...]
    """
    task_path = Path(task_path)
    root = Path(tasks_dir) if tasks_dir else task_path.parent
    groups: list[dict] = []
    index: dict[str, int] = {}               # script -> groups 下标(同一脚本多次出现则合并)
    seen: set[Path] = set()

    def add(script_rel: str, path: Path, name: str, opt_title: str, desc: str,
            items: list[dict]):
        """opt_title 非空表示这次带的是 options 段声明的标题(优先于脚本 name)"""
        if script_rel in index:              # 合并(同一脚本被 include 两次)
            g = groups[index[script_rel]]
            if opt_title and not g["_opt_title"]:
                g["title"], g["desc"], g["_opt_title"] = opt_title, desc, True
            have = {(i["section"], i["key"]) for i in g["items"]}
            g["items"].extend(i for i in items if (i["section"], i["key"]) not in have)
            if items and not g["path"]:
                g["path"] = str(path)
            return
        index[script_rel] = len(groups)
        groups.append({"script": script_rel, "path": str(path),
                       "title": opt_title or name, "desc": desc,
                       "items": list(items), "_opt_title": bool(opt_title)})

    def walk(p: Path, depth: int):
        if depth > MAX_DEPTH:
            return
        real = p.resolve()
        if real in seen:
            return                            # 防 include 环
        seen.add(real)
        data = _load_yaml(p)
        try:
            rel = str(real.relative_to(Path(root).resolve())).replace("\\", "/")
        except Exception:
            rel = p.name
        name = str(data.get("name") or p.stem)

        raw_opts = data.get("options")
        items: list[dict] = []
        opt_title, desc, script_rel = "", "", rel
        if isinstance(raw_opts, dict):
            opt_title = str(raw_opts.get("title") or "")
            desc = str(raw_opts.get("desc") or "")
            if raw_opts.get("script"):
                script_rel = _resolve_script(str(raw_opts["script"]), real, root)
            items = [it for it in (_normalize_item(r)
                                   for r in (raw_opts.get("items") or [])) if it]

        # 主脚本(任务文件本身)没有选项段时不出现在列表里; 子脚本一律列出
        # (界面要能看到"这个任务有哪些脚本"), 有选项的带 ● 标记。
        if items or (include_empty and depth > 0):
            add(script_rel, real, name, opt_title, desc, items)

        steps = data.get("steps") or []
        if not isinstance(steps, list):
            return
        for raw in steps:
            if isinstance(raw, dict) and "include" in raw:
                walk(p.parent / str(raw["include"]), depth + 1)

    walk(task_path, 0)
    return groups


def option_keys(groups: list[dict]) -> dict[str, object]:
    """{"section.key": default} —— 供 loader 在 config.yaml 缺键时兜底"""
    out: dict[str, object] = {}
    for g in groups:
        for it in g["items"]:
            full = f"{it['section']}.{it['key']}"
            if full not in out:
                out[full] = it["default"]
    return out


def list_sub_scripts(task_path: str | Path,
                     tasks_dir: str | Path | None = None) -> list[dict]:
    """按 include 顺序列出这个任务用到**哪些子脚本**(不含任务文件本身)

    2026-10-09 新增: 供 GUI「脚本」/「脚本步骤」页的下拉框使用 —— 选中某个子脚本
    就能单独打开/编辑它, 或只看它那一段步骤(补跑/调试时不用在大脚本里翻)。

    返回 [{rel, path, name}]:
      rel  = 相对任务目录的路径(与 Step.source 同口径, 可直接比对)
      name = 脚本自己的 name: 字段(没有则用文件名)
    """
    task_path = Path(task_path)
    root = (Path(tasks_dir) if tasks_dir else task_path.parent).resolve()
    out: list[dict] = []
    seen: set[Path] = set()

    def walk(p: Path, depth: int):
        if depth > MAX_DEPTH:
            return
        real = p.resolve()
        if real in seen:                       # 防 include 环 / 同一脚本出现两次
            return
        seen.add(real)
        data = _load_yaml(p)
        try:
            rel = str(real.relative_to(root)).replace("\\", "/")
        except Exception:
            rel = p.name
        if depth > 0:                          # depth 0 = 任务文件本身, 不列为子脚本
            out.append({"rel": rel, "path": str(real),
                        "name": str(data.get("name") or p.stem)})
        steps = data.get("steps") or []
        if not isinstance(steps, list):
            return
        for raw in steps:
            if isinstance(raw, dict) and "include" in raw:
                walk(p.parent / str(raw["include"]), depth + 1)

    walk(task_path, 0)
    return out


def task_option_keys(task_path: str | Path,
                     tasks_dir: str | Path | None = None) -> dict[str, object]:
    return option_keys(collect_options(task_path, tasks_dir))
