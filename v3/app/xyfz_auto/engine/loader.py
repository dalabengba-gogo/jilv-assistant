# -*- coding: utf-8 -*-
"""YAML 任务脚本加载器"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from .model import Step, Task

# 2026-10-07 新增: 脚本里可用 {{cfg:节.键}} 引用 config.yaml 的值,
# 加载时做字符串替换(用于 GUI「脚本选项」驱动脚本分支, 如地煞星级)。
#   jump: "地煞-星级-{{cfg:dixia.star_row}}"   ->  jump: 地煞-星级-auto
# 缺键时替换为空串并打警告(会让 jump 目标解析失败, loader 警告可见, 不会静默跑偏)。
_CFG_PAT = re.compile(r"\{\{cfg:([A-Za-z0-9_.]+)\}\}")


def _cfg_get(cfg: dict, dotted: str):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _render_cfg_value(val) -> str:
    """占位符取值渲染成字符串: bool 统一小写(便于脚本里拼 jump 目标名)"""
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


def _substitute_cfg(raw_steps: list, cfg: dict,
                    defaults: dict | None = None) -> list:
    """递归替换步骤树里所有字符串值的 {{cfg:...}} 占位符

    defaults: 脚本 options: 段声明的缺省值({"节.键": 值}), config.yaml 里没有该键时
    用它兜底 —— 这样新脚本带新选项时, 用户没在界面上动过也能正常跑。
    """
    warn = False
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        stack = [raw]
        while stack:
            node = stack.pop()
            for k, v in list(node.items()):
                if isinstance(v, str) and "{{cfg:" in v:
                    def _rep(m):
                        nonlocal warn
                        val = _cfg_get(cfg, m.group(1))
                        if val is None and defaults:
                            val = defaults.get(m.group(1))
                        if val is None:
                            warn = True
                            print(f"[loader] 警告: {{cfg:{m.group(1)}}} 在 config.yaml "
                                  f"中不存在, 已替换为空串", file=sys.stderr)
                            return ""
                        return _render_cfg_value(val)
                    node[k] = _CFG_PAT.sub(_rep, v)
                elif isinstance(v, dict):
                    stack.append(v)
                elif isinstance(v, list):
                    stack.extend(x for x in v if isinstance(x, dict))
    return raw_steps


def _rel_src(path, root: Path) -> str:
    """步骤声明文件的相对路径(相对任务目录, 如 "parts/daily_dixia_3x.yaml")

    解析不到(不在任务目录下)就退回文件名 —— 只用于界面分组显示。
    """
    try:
        return str(Path(path).resolve().relative_to(root)).replace("\\", "/")
    except Exception:
        return Path(path).name


def _expand_includes(raw_steps: list, base_dir: Path, depth: int = 0,
                     src: str = "", root: Path | None = None,
                     chain: list | None = None) -> list:
    """把 include: xxx.yaml 的步骤内联展开(支持嵌套,最深5层)

    YAML 写法:
      steps:
        - name: 第一步 组队一条
          include: daily_team_oneloop.yaml
        - name: 第二步 地煞星
          include: daily_dixia_3x.yaml

    子文件里只取其 steps(相对自身文件目录解析,子文件也可再 include)。

    2026-10-09: 展开时给每条步骤打 `__src__` 标记(它写在哪份脚本里),
    load_task 会把它落到 Step.source —— GUI 按子脚本筛选步骤靠这个。

    2026-10-17 v3.5.0: 加**成环检测**。以前只有 `depth > 5` 的深度兜底, 于是
    A include B、B include A 这种真成环的情况, 报的是"include 嵌套超过 5 层"
    —— 一句完全指错方向的话(用户会去数层数, 而层数根本不深)。现在按文件
    链判定, 直接点名是哪几个文件绕成了圈。
    """
    if depth > 5:
        raise ValueError("include 嵌套超过 5 层")
    root = root or base_dir
    chain = list(chain or [])
    out = []
    for raw in raw_steps:
        if isinstance(raw, dict) and "include" in raw:
            sub = base_dir / str(raw["include"])
            try:
                key = str(sub.resolve())
            except Exception:
                key = str(sub)
            if key in chain:
                loop = [Path(c).name for c in chain[chain.index(key):]] + [sub.name]
                raise ValueError(
                    f"include 成环: {' → '.join(loop)}。"
                    f"脚本互相 include 会无限展开, 请把公共部分抽成第三个文件。")
            with open(sub, "r", encoding="utf-8") as f:
                sub_data = yaml.safe_load(f) or {}
            sub_steps = sub_data.get("steps", [])
            out.extend(_expand_includes(sub_steps, sub.parent, depth + 1,
                                        _rel_src(sub, root), root, chain + [key]))
        else:
            if isinstance(raw, dict):
                raw = dict(raw)                 # 浅拷贝: 不污染调用方的原始数据
                raw.setdefault("__src__", src)
            out.append(raw)
    return out


def _expand_loops(raw_steps: list) -> list:
    """把含 loop 的步骤块原地展开为普通步骤

    YAML 写法：
      - name: 点掉结算弹窗(循环)
        loop: 24                # 重复次数
        steps:
          - wait: {ocr: 奖励总览}
            timeout: 60
            on_fail: continue
          - click: [450, 700]
          - sleep: 2

    展开后等价于把 steps 里的子步骤顺序复制 loop 次(名字带序号)。
    子步骤里的 wait/action 结构与普通步骤完全一致。
    """
    out = []
    for raw in raw_steps:
        if not isinstance(raw, dict) or "loop" not in raw:
            out.append(raw)
            continue
        times = int(raw.get("loop", 1))
        children = raw.get("steps", [])
        # __src__ 是 loader 自己打的"所属脚本"标记, 不是写错位置的动作键
        extra = [k for k in raw if k not in ("name", "loop", "steps", "__src__")]
        if extra:
            import sys
            print(f"[loader] 警告: loop步骤[{raw.get('name')}] 的同行键 {extra} "
                  f"不会被执行(动作要写进 steps 子步骤里)", file=sys.stderr)
        if not children:
            continue
        base = raw.get("name", "循环")
        for i in range(1, times + 1):
            for sub in children:
                if not isinstance(sub, dict):
                    continue
                one = dict(sub)          # 浅拷贝，避免改名互相污染
                # 循环体子步骤没被 _expand_includes 打到标记, 继承循环块的
                one.setdefault("__src__", raw.get("__src__", ""))
                if sub.get("name") and times > 1:
                    one["name"] = f"{sub['name']}({i}/{times})"
                elif not one.get("name"):
                    one["name"] = f"{base} {i}/{times}"
                out.append(one)
    return out


def load_task(path: str | Path) -> Task:
    """从 YAML 文件加载任务"""
    p = Path(path)
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    task = Task(
        name=data.get("name", p.stem),
        description=data.get("description", ""),
    )
    settings = data.get("settings", {})
    default_timeout = settings.get("default_timeout", 30)
    default_retry = settings.get("default_retry", 3)

    # 任务目录 = 本文件的目录: 步骤 source 用相对它的路径(见 _rel_src)
    root = p.parent.resolve()
    raw_steps = _expand_includes(data.get("steps", []), p.parent, 0,
                                 _rel_src(p, root), root)
    raw_steps = _expand_loops(raw_steps)
    # 2026-10-07: include/loop 展开后再做配置占位符替换(此时步骤树已定型)
    try:
        from common import load_config
        cfg = load_config()
    except Exception:
        cfg = {}
    if any("{{cfg:" in str(s) for s in raw_steps):
        defaults: dict = {}
        try:
            # 脚本 options: 段声明的缺省值(config.yaml 缺键时兜底, 见 engine/options.py)
            from .options import task_option_keys
            defaults = task_option_keys(p)
        except Exception as e:
            print(f"[loader] 提示: 读取脚本选项缺省值失败({e}), 仅用 config.yaml", file=sys.stderr)
        raw_steps = _substitute_cfg(raw_steps, cfg, defaults)
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue

        action = raw.get("action")
        if not action:
            # 语法糖: 允许直接写 click:/sleep:/key:/swipe: 等顶层键
            action = _shorthand_action(raw)

        step = Step(
            name=raw.get("name", ""),
            wait=raw.get("wait"),
            timeout=raw.get("timeout", default_timeout),
            action=action or {},
            verify=raw.get("verify"),
            retry=raw.get("retry", default_retry),
            # 2026-10-16 v3.0.5: settle 稳检测秒数(默认 0 = 不启用, 零行为影响)
            settle=float(raw.get("settle", 0) or 0),
            on_fail=raw.get("on_fail", "retry"),
            on_success=raw.get("on_success"),
            source=str(raw.get("__src__") or ""),
            # 2026-10-16 v3.0.0: required: true = 这一步失败即整条任务判失败
            required=bool(raw.get("required", False)),
            # 2026-10-17 v3.5.0: optional: true = 纯条件探测, 未命中属正常(记
            # skipped 不记 failed)。不写则由 runner 启发式判定, 脚本不用改。
            optional=bool(raw.get("optional", False)),
            # 2026-10-17 v3.7.0: verify_done: true = 这一步是该子任务的达成验收点
            # (以游戏面板上的完成状态为准), 详见 model.Step 的注释。
            verify_done=bool(raw.get("verify_done", False)),
            # 2026-10-18 v4.4.0: verify_retry: true = 复核步(同卡二次确认),
            # 引擎按 OR 合并; 未标则不同验收点之间按 AND 合并。
            verify_retry=bool(raw.get("verify_retry", False)),
            # 2026-10-19 v4.8.0 (P1-3): max_seconds = 本步整步总预算, 覆盖全局
            # engine.max_step_seconds; 0 = 跟随全局。给「等一场战斗打完」这类
            # 天生要几十分钟的步骤单独开口子。
            max_seconds=float(raw.get("max_seconds", 0) or 0),
            # 2026-10-19 v4.8.0: max_visits = 本步最多被执行几次(循环/批次上限),
            # 0 = 不限。详见 model.Step 注释。
            max_visits=int(raw.get("max_visits", 0) or 0),
            # 2026-10-19 v4.9.0 (P2-5): 本步模板阈值, 0 = 用模板自己的默认值
            # (_meta.txt) 或全局 match_threshold。
            threshold=float(raw.get("threshold", 0) or 0),
        )
        task.steps.append(step)
    changed = _resolve_jump_targets(task)
    if changed:
        import logging
        logging.getLogger(__name__).info(
            "任务[%s] 跳转目标已按循环展开名解析: %s", task.name, "; ".join(changed))
    _validate(task)
    return task


# `_expand_loops` 给循环体子步骤生成的名字形如「点掉结算弹窗(3/24)」
_LOOP_NAME_RE = re.compile(r"^(?P<base>.+)\((?P<i>\d+)/(?P<n>\d+)\)$")


def _resolve_jump_targets(task: Task) -> list[str]:
    """把跳转目标对齐到**实际存在**的步骤名(2026-10-19 v4.9.0 P2-4)

    背景(真实耦合): 脚本里写 ``on_fail: {goto: 地煞-关面板-若在主界面则完成(1/6)}``,
    而 "(1/6)" 这一段是 loader 按 ``loop: 6`` **生成**的。今天把循环次数从 6 改成 8,
    步骤名全变成 "(1/8)", 所有写死 "(1/6)" 的跳转立刻失效 → 运行到那一步就报
    「goto 目标非法」整条任务终止 —— 而脚本里看起来"什么都没改"。
    仓库里这类引用有 80+ 处, 全手工改既累又容易漏。

    现在加载后统一解析一次, 三种情况:
      ① 目标已存在          → 原样不动(绝大多数, 零行为变化)
      ② 目标带 (i/N) 但 N 变了 → 换成同一轮次的真实名字(X(2/6) → X(2/8))
      ③ 目标只写了裸名, 而实际只有展开后的名字 → 解析到该循环的**第一轮**
         (同名循环出现两次时不做解析 —— 猜错地方比报错更难排查)
    解析不到的原样留着, 由 _validate / check_refs 报出来(不静默吞掉)。

    只改 goto / jump 的指向, 不动任何动作与等待语义。
    """
    known = {s.name for s in task.steps if s.name}
    # base -> [每一轮循环的 [(轮次, 真实名), ...], ...]
    # 按步骤顺序切"段": 轮次从 1 重新开始就算一段新循环。同名循环出现两次时
    # 会有两段 —— 这时宁可解析不出来(报错), 也不猜是哪一段。
    runs: dict[str, list[list[tuple[int, str]]]] = {}
    for s in task.steps:
        m = _LOOP_NAME_RE.match(s.name or "")
        if not m:
            continue
        base, it = m.group("base"), int(m.group("i"))
        segs = runs.setdefault(base, [])
        if it == 1 or not segs:
            segs.append([])
        segs[-1].append((it, s.name))

    def resolve(target: str) -> str | None:
        if target in known:
            return target
        m = _LOOP_NAME_RE.match(target)
        if m:
            want = int(m.group("i"))
            same = [nm for seg in runs.get(m.group("base"), [])
                    for it, nm in seg if it == want]
            if len(same) == 1:
                return same[0]
            # 轮次越界(loop 次数被改小)或两段同名循环 → 不动, 让 _validate 报错
            return None
        segs = runs.get(target) or []
        return segs[0][0][1] if len(segs) == 1 else None

    changed: list[str] = []
    for step in task.steps:
        for holder in ("on_fail", "on_success"):
            ref = getattr(step, holder, None)
            if not (isinstance(ref, dict) and isinstance(ref.get("goto"), str)):
                continue
            new = resolve(ref["goto"])
            if new and new != ref["goto"]:
                setattr(step, holder, {**ref, "goto": new})   # 不改原 dict(可能被复用)
                changed.append(f"{ref['goto']} → {new}")
        act = step.action if isinstance(step.action, dict) else None
        if act and act.get("type") == "jump" and isinstance(act.get("target"), str):
            new = resolve(act["target"])
            if new and new != act["target"]:
                step.action = {**act, "target": new}
                changed.append(f"{act['target']} → {new}")
    return changed


def _validate(task: Task):
    """加载后自检: 会静默出错的写法(2026-09-10 各踩过一次)

    1) 重名步骤被 goto/jump 引用 —— include 把多个脚本内联后, index_of() 只返回
       首个同名步骤, 跳转会串门(地煞的 goto 命中组队一条的同名步, 任务被劫持)。
       只报"重名 且 被引用"的, 避免一堆无害重名(停顿2秒×10)淹没真问题。
    2) 步骤缺 name —— 失败截图/日志定位困难。
    (loop 步骤的同行动作键在 _expand_loops 里单独告警)
    """
    import logging
    log = logging.getLogger(__name__)
    refs = set()
    for s in task.steps:
        act = getattr(s, "action", None)
        if isinstance(act, dict) and act.get("type") == "jump" and act.get("target"):
            refs.add(str(act["target"]))          # 无条件跳转(jump: 名字)
        of = getattr(s, "on_fail", None)
        if isinstance(of, dict) and of.get("goto"):
            refs.add(str(of["goto"]))
        os_ = getattr(s, "on_success", None)
        if isinstance(os_, dict) and os_.get("goto"):
            refs.add(str(os_["goto"]))
    names = {s.name for s in task.steps if s.name}
    missing = sorted(r for r in refs if r not in names)
    if missing:
        print(f"[loader] 警告: 任务[{task.name}] 跳转目标不存在, 运行时会报错: {missing}",
              file=sys.stderr)
    counts: dict[str, int] = {}
    first: dict[str, int] = {}
    for i, s in enumerate(task.steps):
        if s.name:
            counts[s.name] = counts.get(s.name, 0) + 1
            first.setdefault(s.name, i + 1)
    risky = {n: c for n, c in counts.items() if c > 1 and n in refs}
    if risky:
        items = ", ".join(f"{n}×{c}(首个在第{first[n]}步)" for n, c in risky.items())
        log.warning("任务[%s] goto/jump 目标重名, 会跳到首个同名步骤: %s", task.name, items)
        print(f"[loader] 警告: 任务[{task.name}] 跳转目标重名: {items}", file=sys.stderr)
    return task


def _shorthand_action(raw: dict) -> dict | None:
    """把简洁写法翻译成标准 action dict

    click: {button: 角色}   -> {"type":"click", "button":"角色"}
    click: [x, y]           -> {"type":"click", "at":[x,y]}
    click_center: true      -> {"type":"click_center"}
    sleep: 2                -> {"type":"sleep", "seconds":2}
    key: 4                  -> {"type":"key", "code":4}
    start_app: com.xx       -> {"type":"start_app", "package":"com.xx"}
    goto: {ui: 技能页}      -> {"type":"goto_ui", "screen":"技能页"}
    goto: 技能页            -> 同上(字符串简写)
    swipe: {from: [...], to: [...]} -> {"type":"swipe", ...}
    """
    if "goto" in raw:
        v = raw["goto"]
        if isinstance(v, dict):
            return {"type": "goto_ui", "screen": v.get("ui", "")}
        return {"type": "goto_ui", "screen": str(v)}
    if "jump" in raw:
        # 无条件跳转到指定步骤名(区别于 on_fail 的 goto,用于分支汇合)
        return {"type": "jump", "target": str(raw["jump"])}
    if "click" in raw:
        v = raw["click"]
        if isinstance(v, dict):
            return {"type": "click", **v}
        return {"type": "click", "at": list(v)}
    if "click_center" in raw:
        return {"type": "click_center"}
    if "sleep" in raw:
        return {"type": "sleep", "seconds": float(raw["sleep"])}
    if "key" in raw:
        return {"type": "key", "code": int(raw["key"])}
    if "start_app" in raw:
        return {"type": "start_app", "package": raw["start_app"]}
    if "swipe" in raw:
        v = dict(raw["swipe"] or {})
        return {"type": "swipe", **v}
    return None


# 2026-10-19 v4.9.0(P2-6): 删除了 dump_task()。
#   它曾是「脚本编辑器保存」的序列化入口, 但编辑器后来改为直接编辑 YAML 文本,
#   这个函数就再没有任何调用点(全项目 grep 只剩导入)。留着比删掉更危险:
#   它只写回 name/wait/timeout/action/verify/retry/on_fail 七个键, 会把
#   settle / required / optional / verify_done / verify_retry / max_seconds /
#   max_visits / threshold / source / on_success 以及整个 loop 结构**静默丢掉**
#   —— 谁哪天顺手拿它存一次盘, 脚本就被悄悄改残了, 还查不出是谁改的。
#   要序列化请直接改 YAML 文本(脚本页本来就是这么做的)。
