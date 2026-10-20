# -*- coding: utf-8 -*-
"""步骤/任务数据模型

任务脚本 = 一组有序 Step。每个 Step 描述：
  wait   等待画面出现某特征(模板或OCR关键词)，超时则失败
  action 执行操作(点击固定坐标 / 点击识别中心 / 滑动 / 按键 / 等待)
  verify (可选)操作后再确认某特征出现，才算成功
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Step:
    name: str = ""
    # wait: {"template": "tpl_name"} | {"ocr": "关键词"} | {"any": ["a","b"]} | None
    wait: dict | None = None
    timeout: float = 30.0
    # action: {"type": "click", "at":[x,y]}
    #         {"type": "click_center"}  点击 wait 命中的中心
    #         {"type": "swipe", "from":[x1,y1], "to":[x2,y2], "duration":300}
    #         {"type": "key", "code": 4}
    #         {"type": "sleep", "seconds": 1}
    #         {"type": "start_app"}
    action: dict = field(default_factory=dict)
    # verify: 同 wait 结构，点击后还需验证出现，否则失败
    verify: dict | None = None
    retry: int = 3
    # settle: 稳检测秒数(2026-10-16 v3.0.5 新增)。wait 命中后若设了 settle>0,
    #   会再等 settle 秒并复核条件是否仍成立, 才判定成功; 否则视为过渡期假阳性
    #   (典型: 战斗→主界面切换时底栏短暂闪现, 主界面=底栏任一+not战斗中 瞬间成立),
    #   继续轮询。用于根治「等回主界面」在战斗间隙被误判、进而与下游复核形成
    #   goto 死循环的旧事故(2026-10-16 实战复现: 域外批次后 875↔878 空转)。
    #   默认 0 = 不启用, 对所有已有脚本行为零影响。
    settle: float = 0.0
    # on_fail: "retry" | "stop" | "continue" | {"goto": 序号或名字}
    on_fail: object = "retry"
    # on_success: {"goto": 名字} —— wait 命中(成功)时跳转, 用于循环内条件退出
    #             (2026-09-10 新增: 监控循环检测"链条已结束"提前收尾)
    on_success: object = None
    # 2026-10-16 v3.0.0 新增: required=True 表示"这一步必须做成"。
    #   背景(事故): 全项目 on_fail: continue 有 185 处, 而 continue 在原实现里
    #   返回 True → run_task 判定成功 → 界面打 ✅、退出码 0。实录: 帮派捐献没做成
    #   却整条任务报"成功", 用户完全无从察觉(2026-10-11)。
    #   默认 False = 沿用现状(跳过不影响整条任务的成败判定), 保证 185 处脚本
    #   行为不变、不会天天报红; 只想让**关键步骤**(领奖励/交任务这类)失败即整条
    #   失败时, 在 YAML 里显式写 required: true。
    #   注意: 即使 required=False, 失败也会被统计进 Engine.failed_steps 并在
    #   任务结束时汇总告警 —— 静默失败的问题靠这个解决, 与 required 无关。
    required: bool = False
    # 2026-10-17 v3.5.0: optional=True 表示"这一步是条件探测, 没命中属正常"。
    #   背景(事故): 昨夜实战 926 步里 157 步「跳过」, 汇总只说「16 步未做成」,
    #   用户完全分不清哪些是真失败、哪些只是"这个奖励今天没有/已领过"的正常未命中。
    #   语义: 命中=继续走成功分支; 没命中=记入 skipped(条件未命中), **不算失败**,
    #   不进 failed、不影响成败判定, 但仍会在汇总里单列一行供核对。
    #   未显式写时由 runner 按启发式自动判定(见 Engine._is_optional), 无需改脚本。
    optional: bool = False
    # 2026-10-17 v3.7.0: verify_done=True 表示"这一步是验收点" —— 它的**成功与否
    #   直接决定所在子任务做成没有**, 不再看该段里掉了几步。
    #   背景(用户主张): 玩法是否做完, 权威答案在游戏自己的面板上 —— 卡片变灰 /
    #   左上角挂「已完成」徽标 = 做完了; 没挂 = 没做完。而我们数"有几步没走到"
    #   只是二手猜测: 「已领取的按钮点不到」「这次没弹确认框」都会记成失败, 实际
    #   却已经做成了(实录: 每日必领 13 步"失败", 玩法其实全做成)。
    #   用法: 在子任务收尾回到玩法面板后加一步达成状态判定并标 verify_done: true。
    #   命中 → 该子任务 ✔「已完成」; 未命中 → ✘ 未完成(即使中间没掉过一步)。
    #   没标这个字段的脚本行为完全不变(仍按"掉了几步"判断)。
    verify_done: bool = False
    # 2026-10-18 v4.4.0: verify_retry=True 标记"复核步"(对**同一张卡**的二次确认)。
    #   背景: 一个子任务可以有多个验收点(如一条龙验 挖宝封妖/烧法/商旅/帮派 4 张卡),
    #   不同验收点之间必须 AND 合并(任一卡未见徽标 → 整段 ✘); 但「检查+复核」两步
    #   针对同一张卡, 旧版靠"后写覆盖"实现复核恢复(检查未命中→复核命中=已完成)。
    #   引入 AND 合并后覆盖语义被破坏, 故复核步显式标 verify_retry: true,
    #   引擎对它做 OR 合并(与该卡首次检查结果取或); 不标则按 AND 合并。
    verify_retry: bool = False
    # 2026-10-19 v4.8.0 (P1-3): 本步自己的**总时间预算**(秒), 覆盖全局
    #   engine.max_step_seconds。0 = 跟随全局。
    #   背景: 全局预算只能定一个"谁都别超过"的天花板, 而「等一场战斗打完」这类
    #   步骤本身就要几十分钟(域外自述最长 90 分钟), 一刀压到 30 分钟会把它掐死。
    #   用法: 在长等待步骤上显式标 max_seconds, 语义是"这一步我认它要这么久";
    #   没标则吃全局上限。注意单位是**整步总预算**(含所有重试), 不是单次 timeout。
    max_seconds: float = 0.0
    # 2026-10-19 v4.8.0: 本步在整个任务里最多被执行几次(0 = 不限)。
    #   背景(用户诉求, 自 v4.4.0 起三度单列): 域外风情这类玩法是循环刷的,
    #   脚本一圈圈转, 没有"今天该收手"的天然终点 —— 实测过跑到天亮。
    #   加了它, 循环到上限就不再继续执行, 按该步的 on_fail 走收尾(通常 goto 验收块),
    #   后面的玩法照常跑。只在需要限流的**循环体首步**上标, 其余步留 0(行为不变)。
    max_visits: int = 0
    # 2026-10-19 v4.9.0 (P2-5): 这一步的**模板匹配阈值**(0 = 不覆盖)。
    #   背景: 全局 match_threshold 是折中值, 而模板之间差别很大 —— 纯文字/单色
    #   按钮要 0.9 才算真命中, 带动态角标/半透明的模板 0.8 就够; 一刀切的结果是
    #   "调高了某些步骤不认, 调低了某些步骤乱认"。逐模板的默认值写
    #   assets/templates/_meta.txt, 只有**某一步**要特殊处理时才写这个字段。
    #   优先级: 本字段 > _meta.txt > config.yaml 的 match_threshold。
    threshold: float = 0.0
    # 2026-10-09 新增: 这一步写在哪份脚本里(相对任务目录, 如 "parts/daily_dixia_3x.yaml").
    # include/loop 展开时由 loader 打标, 供 GUI「脚本」/「脚本步骤」页按子脚本筛选 ——
    # 纯元信息, 执行逻辑不看它(GUI 的「脚本步骤」页按它筛选子脚本)。
    source: str = ""

    # 运行期状态
    # 2026-10-17: 删掉 matched 字段 —— 全项目只声明不写不读(grep "\.matched" 零命中),
    # 留着只会让人以为 wait 命中结果会被缓存复用。
    # 2026-10-16 v3.0.0: 本步是否失败过(被跳过/终止/跳转收尾)。由 runner 填写,
    # 供界面「脚本步骤」页把跳过的步骤标出来, 而不是只看最终"成功/失败"两个字。
    failed: bool = False

    def describe(self) -> str:
        parts = []
        if self.wait:
            parts.append(f"等待{self.wait}")
        if self.action:
            parts.append(f"动作{self.action}")
        if self.verify:
            parts.append(f"验证{self.verify}")
        return f"[{self.name}] {' → '.join(parts)}" if self.name else " → ".join(parts)


# ---------------------------------------------------------------------------
# 子任务分组(2026-10-17 v3.7.0)
#
# 需求: 任务跑完后的汇总按步骤逐条列, 926 步的日常能刷出上百行, 而其中大量是
# 「跳转/条件未命中」这类正常现象 —— 用户要的是"每个子任务成没成", 不是流水账。
#
# 分两种模式, 由 runner 按脚本实际结构自动选(脚本不用改):
#   · "source"  —— 任务由多个子脚本 include 组成(如 daily_all 的 7 个 parts),
#                  按 Step.source 分组, 显示子脚本的可读名。
#   · "segment" —— 单脚本任务(如「每日必领」99 步在一个文件里), source 只有一种,
#                  按步骤名的段标记分组(「每日必领-①打开商城」→ ①)。
# ---------------------------------------------------------------------------

# 子脚本 → 可读名。没登记的退回文件名(去掉目录与扩展名), 新增 parts 不用改代码。
SUBTASK_SOURCE_NAMES = {
    "daily_pure_mode": "纯享模式",
    "daily_claim": "每日必领",
    "daily_team_oneloop": "组队一条龙",
    "daily_dixia_3x": "地煞星×3",
    "daily_shimen": "师门",
    "daily_wuhuan": "五环",
    "daily_yuwai": "域外风情",
}

# 「每日必领-①打开商城」→ ① ; 也认 -1 / -2 这类数字段号
_SEG_RE = re.compile(r"^.*?[-—]([①-⑩]|\d{1,2}|[A-Za-z])(?![0-9])")


def subtask_of(step: "Step", mode: str) -> str:
    """这一步属于哪个子任务

    纯函数(不碰引擎状态), 方便单测。分组粒度见上面模块注释。
    """
    if mode == "source":
        src = (getattr(step, "source", "") or "").replace("\\", "/")
        if not src:
            return "主流程"
        stem = src.rsplit("/", 1)[-1]
        for ext in (".yaml", ".yml"):
            if stem.endswith(ext):
                stem = stem[: -len(ext)]
                break
        return SUBTASK_SOURCE_NAMES.get(stem, stem)
    m = _SEG_RE.match(step.name or "")
    return m.group(1) if m else "主流程"


@dataclass
class RunResult:
    """一次任务运行的结构化结果(2026-10-17 v3.5.0)

    以前 GUI / 命令行靠 ``int(summary.split(" 步未做成")[0])`` 从文案里切数字
    —— 引擎文案一改就静默得到 0(P2-4)。现在直接给结构化字段, 文案只用于展示。

    failed  = 真失败(该做成的没做成; required 的会升级为整条任务失败)
    skipped = 条件未命中(optional 探测, 正常现象, 不算失败)
    """

    ok: bool = False
    name: str = ""
    failed: list = field(default_factory=list)     # [{index,name,reason,policy,required}]
    skipped: list = field(default_factory=list)    # [{index,name,reason,policy}]
    stopped: bool = False                          # 是否用户手动停止
    aborted_reason: str = ""                       # 终止原因(死循环/兜底缺失/异常等)
    # 2026-10-17 v3.7.0: 子任务级统计 —— [{name,total,reached,failed,required_failed}]
    #   任务跑完界面只看这个粒度(每个子任务一行), 逐步明细只进 run.log。
    #   reached=False 表示这一步所在的子任务压根没跑到(任务提前终止/停止)。
    subtasks: list = field(default_factory=list)

    @property
    def n_failed(self) -> int:
        return len(self.failed)

    @property
    def n_skipped(self) -> int:
        return len(self.skipped)

    @property
    def n_required_failed(self) -> int:
        return sum(1 for f in self.failed if f.get("required"))

    def counts(self) -> dict:
        """给 Qt 信号用的纯 dict(跨线程传 dict 比传自定义对象安全)"""
        return {
            "failed": self.n_failed,
            "skipped": self.n_skipped,
            "required_failed": self.n_required_failed,
            "stopped": self.stopped,
            "subtasks": list(self.subtasks),
        }

    def subtask_summary(self) -> str:
        """子任务级汇总: 每个子任务一行(2026-10-17 v3.7.0)

        界面只看这个粒度。逐步明细(第几步、为什么)仍由 summary() 生成并只写
        run.log —— 926 步的日常在界面上刷出上百行流水账, 用户根本不看, 反而
        把"到底成没成"这个最该看见的信息淹了。
        """
        if not self.subtasks:
            return ""
        lines = []
        for st in self.subtasks:
            name = st.get("name", "?")
            total = int(st.get("total", 0) or 0)
            if not st.get("reached"):
                lines.append(f"—  {name} ({total}步) 未执行")
                continue
            # 2026-10-17 v3.7.0: 有验收点的子任务, **以游戏面板的完成状态为准**,
            # 不看掉了几步 —— 「按钮已领取点不到」「这次没弹确认框」都曾被记成失败,
            # 而玩法其实做成了(用户主张: 卡片变灰 / 挂「已完成」徽标才是权威答案)。
            v = st.get("verified")
            n_req = int(st.get("required_failed", 0) or 0)
            n_fail = int(st.get("failed", 0) or 0)
            if v is True:
                lines.append(f"✔ {name} ({total}步) · 面板已显示「已完成」")
                continue
            if v is False:
                lines.append(f"✘ {name} ({total}步) · 面板未显示「已完成」")
                continue
            if n_req:
                mark, extra = "✘", f"{n_req}步关键失败"
            elif n_fail:
                mark, extra = "⚠", f"{n_fail}步未做成"
            else:
                mark, extra = "✔", ""
            lines.append(f"{mark} {name} ({total}步)" + (f" · {extra}" if extra else ""))
        return "\n".join(lines)

    def summary(self) -> str:
        """可读汇总；无失败且无跳过返回空串(界面据此判定"全做成")"""
        if not self.failed and not self.skipped:
            return ""
        lines = []
        if self.failed:
            req = self.n_required_failed
            lines.append(
                f"真失败 {self.n_failed} 步"
                + (f"，其中 {req} 步是关键步骤(required)" if req else ""))
            for f in self.failed:
                mark = "[关键]" if f.get("required") else "[失败]"
                lines.append(
                    f"  {mark} 第{f['index']}步「{f['name']}」 — {f['reason']}")
        if self.skipped:
            lines.append(f"条件未命中 {self.n_skipped} 步（探测类, 属正常, 不算失败）")
            for f in self.skipped[:20]:
                lines.append(f"  [未命中] 第{f['index']}步「{f['name']}」 — {f['reason']}")
            if self.n_skipped > 20:
                lines.append(f"  … 另有 {self.n_skipped - 20} 步未命中(详见 run.log)")
        return "\n".join(lines)


@dataclass
class Task:
    name: str = "未命名任务"
    description: str = ""
    steps: list = field(default_factory=list)

    def index_of(self, target) -> int:
        """按名字或序号找步骤下标，找不到返回 -1"""
        if isinstance(target, int):
            return target if 0 <= target < len(self.steps) else -1
        for i, s in enumerate(self.steps):
            if s.name == target:
                return i
        return -1
