"""
strands_tools.py - Strands @tool definitions for Smart Query agent.

硬约束（TASK-L1-smart-query § 6）:
  - execute_cypher 内部必先调 query_guard.is_safe()；不安全直接返回错误字符串，
    绝不让 Agent 有机会绕过。

实现参考 experiments/strands-poc/spike.py。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from strands import tool  # type: ignore

from neptune import neptune_client as nc
from neptune import query_guard

logger = logging.getLogger(__name__)


# ---- Per-thread tool-call trace (StrandsNLQueryEngine 会在每次 query() 前 reset) ----
# Strands 内部可能在其他线程/async 中调用 tool，threading.local 拿不到；
# 所以用模块级列表 + 锁。单实例 + 单线程调用者下安全；并发场景可在调用前自己加锁。
import threading as _threading

_lock = _threading.Lock()
_calls: list = []
_profile_holder: dict = {"profile": None}


def _trace_list() -> list:
    return _calls


def reset_trace() -> None:
    with _lock:
        _calls.clear()


def get_trace() -> list:
    with _lock:
        return list(_calls)


# ---- profile override (StrandsNLQueryEngine 注入 profile) ----
def _current_profile() -> Any:
    prof = _profile_holder["profile"]
    if prof is None:
        from profiles.profile_loader import EnvironmentProfile
        prof = EnvironmentProfile()
        _profile_holder["profile"] = prof
    return prof


def set_profile(profile: Any) -> None:
    _profile_holder["profile"] = profile


# =========================================================
# Tools
# =========================================================

@tool
def get_schema_section(section: str = "all") -> str:
    """Return the Neptune graph schema text for grounding the cypher generator.

    Args:
        section: "all" | "nodes" | "edges". 任意非法值按 "all" 处理。

    Returns:
        Schema 文本片段（最多 6000 字符，避免吃光上下文窗口）。
    """
    text = _current_profile().neptune_graph_schema_text or ""
    with _lock:
        _calls.append({"tool": "get_schema_section", "section": section, "chars": len(text)})
    return text[:6000]


@tool
def validate_cypher(cypher: str) -> str:
    """Validate an openCypher query for safety (read-only + hop depth).

    Returns:
        "OK" if safe, else "UNSAFE: <reason>".
    """
    safe, reason = query_guard.is_safe(cypher)
    with _lock:
        _calls.append({"tool": "validate_cypher", "safe": safe, "reason": reason})
    return "OK" if safe else f"UNSAFE: {reason}"


@tool
def execute_cypher(cypher: str) -> str:
    """Execute a READ-ONLY openCypher query against Neptune and return JSON rows.

    内部强制 query_guard.is_safe() 校验；不安全的查询直接返回 ERROR 字符串
    而不是执行。LIMIT 不足时自动补到默认值。

    Returns:
        JSON array string of rows (truncated to 4000 chars) or "ERROR: ...".
    """
    safe, reason = query_guard.is_safe(cypher)
    if not safe:
        with _lock:
            _calls.append({"tool": "execute_cypher", "blocked": True, "reason": reason})
        return f"ERROR: guard blocked unsafe cypher — {reason}"
    cypher2 = query_guard.ensure_limit(cypher)
    try:
        rows = nc.results(cypher2)
    except Exception as e:
        with _lock:
            _calls.append({"tool": "execute_cypher", "error": repr(e)[:200]})
        return f"ERROR: execution failed — {e!r}"
    with _lock:
        _calls.append({"tool": "execute_cypher", "cypher": cypher2[:200], "rows": len(rows)})
    return json.dumps(rows, ensure_ascii=False, default=str)[:4000]


# ---- 仅供引擎读取最后一次执行的 cypher + 结果 ----
def last_execution() -> dict:
    """返回最近一次 execute_cypher 的 {cypher, rows} 信息（engine 用来填 results 字段）。"""
    with _lock:
        snapshot = list(_calls)
    for c in reversed(snapshot):
        if c.get("tool") == "execute_cypher" and not c.get("blocked") and not c.get("error"):
            return c
    return {}
