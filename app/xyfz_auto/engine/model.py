# -*- coding: utf-8 -*-
"""步骤/任务数据模型

任务脚本 = 一组有序 Step。每个 Step 描述：
  wait   等待画面出现某特征(模板或OCR关键词)，超时则失败
  action 执行操作(点击固定坐标 / 点击识别中心 / 滑动 / 按键 / 等待)
  verify (可选)操作后再确认某特征出现，才算成功
"""
from __future__ import annotations

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
    # on_fail: "retry" | "stop" | "continue" | {"goto": 序号或名字}
    on_fail: object = "retry"
    # on_success: {"goto": 名字} —— wait 命中(成功)时跳转, 用于循环内条件退出
    #             (2026-09-10 新增: 监控循环检测"链条已结束"提前收尾)
    on_success: object = None
    # 2026-10-09 新增: 这一步写在哪份脚本里(相对任务目录, 如 "parts/daily_dixia_3x.yaml").
    # include/loop 展开时由 loader 打标, 供 GUI「脚本」/「脚本步骤」页按子脚本筛选 ——
    # 纯元信息, 执行逻辑不看它, dump_task 也不会写回 YAML。
    source: str = ""

    # 运行期状态
    matched: object = None   # wait 命中的 MatchResult / OCR 框

    def describe(self) -> str:
        parts = []
        if self.wait:
            parts.append(f"等待{self.wait}")
        if self.action:
            parts.append(f"动作{self.action}")
        if self.verify:
            parts.append(f"验证{self.verify}")
        return f"[{self.name}] {' → '.join(parts)}" if self.name else " → ".join(parts)


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
