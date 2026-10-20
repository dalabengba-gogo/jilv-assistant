# -*- coding: utf-8 -*-
"""账号多开清单(accounts.json)读写层

最多支持 5 个账号并行, 每个账号绑定一个 MuMu 模拟器实例的 ADB 端口。
MuMu 多开实例默认 adb 端口: 实例1 = 16384, 之后每实例 +32
(即 16384 / 16416 / 16448 / 16480 / 16512)。
可在 GUI「多开管理」面板手动改成实际端口(以 MuMu 多开器/`adb devices` 显示为准)。

JSON 结构(固定 5 条, 与 GUI 表格一一对应):
[
  {"enabled": true,  "name": "账号1", "port": 16384, "task": "daily_all.yaml", "delay_min": 0},
  {"enabled": false, "name": "账号2", "port": 16416, "task": "daily_all.yaml", "delay_min": 20},
  ...
]
"""
from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
ACCOUNTS_PATH = PROJECT_ROOT / "accounts.json"
MAX_ACCOUNTS = 5
DEFAULT_PORTS = [16384 + i * 32 for i in range(MAX_ACCOUNTS)]
DEFAULT_TASK = "daily_all.yaml"


def _default_account(i: int) -> dict:
    return {
        "enabled": i == 0,        # 默认只启用第 1 个账号
        "name": f"账号{i + 1}",
        "port": DEFAULT_PORTS[i],
        "task": DEFAULT_TASK,
        "delay_min": 0,           # 相对「启动全部」时刻的错峰延迟(分钟)
    }


def load_accounts() -> list[dict]:
    """读取账号清单; 缺失/损坏时自动补齐默认(始终返回 MAX_ACCOUNTS 条, 保持 GUI 表格整齐)"""
    rows: list = []
    if ACCOUNTS_PATH.exists():
        try:
            raw = json.loads(ACCOUNTS_PATH.read_text(encoding="utf-8"))
            raw = raw if isinstance(raw, list) else raw.get("accounts", [])
            rows = [dict(r) for r in raw if isinstance(r, dict)]
        except Exception:
            rows = []

    out = []
    for i in range(MAX_ACCOUNTS):
        base = _default_account(i)
        if i < len(rows):
            base.update({k: v for k, v in rows[i].items() if v is not None})
        # 字段兜底清洗
        try:
            base["port"] = int(base["port"])
        except (TypeError, ValueError):
            base["port"] = DEFAULT_PORTS[i]
        try:
            base["delay_min"] = int(base["delay_min"])
        except (TypeError, ValueError):
            base["delay_min"] = 0
        base["enabled"] = bool(base.get("enabled"))
        base["name"] = str(base.get("name") or f"账号{i + 1}")
        base["task"] = str(base.get("task") or DEFAULT_TASK)
        out.append(base)
    return out


def save_accounts(rows: list[dict]) -> None:
    """保存账号清单(最多 MAX_ACCOUNTS 条, 只保留核心字段)"""
    clean = []
    for i, r in enumerate(rows[:MAX_ACCOUNTS]):
        try:
            port = int(r.get("port") or DEFAULT_PORTS[i])
        except (TypeError, ValueError):
            port = DEFAULT_PORTS[i]
        try:
            delay = int(r.get("delay_min") or 0)
        except (TypeError, ValueError):
            delay = 0
        clean.append({
            "enabled": bool(r.get("enabled")),
            "name": str(r.get("name") or f"账号{i + 1}"),
            "port": port,
            "task": str(r.get("task") or DEFAULT_TASK),
            "delay_min": delay,
        })
    ACCOUNTS_PATH.write_text(
        json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")


def enabled_accounts() -> list[dict]:
    """只返回启用中的账号"""
    return [r for r in load_accounts() if r.get("enabled")]
