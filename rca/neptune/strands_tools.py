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

# execute_cypher 的**完整**结果，供引擎直接取用，不经过 LLM。
#
# 为什么单独放而不塞进 _calls：_calls 会作为 trace 进返回值、被 demo 页面渲染，
# 完整 rows 可能很大。所以 trace 里只记行数，完整数据走这里。
#
# 2026-09-20 加。在这之前引擎是**重新执行一次 Neptune 查询**来拿完整结果的
# （因为给 LLM 的返回被截断到 4000 字符），等于每次查询白跑两趟 Neptune。
_last_rows: dict = {"cypher": "", "rows": None}


def _trace_list() -> list:
    return _calls


def reset_trace() -> None:
    with _lock:
        _calls.clear()
        _last_rows["cypher"] = ""
        _last_rows["rows"] = None


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

# ── get_schema_section 于 2026-09-20 删除 ──────────────────────────────
#
# 它返回 `profile.neptune_graph_schema_text[:6000]`，而**同一份 schema
# （13520 字符，完整未截断）早已由 `build_system_prompt()` 嵌进 system prompt**
# （19857 字符）。也就是说这个 tool 能提供的信息，Agent 在第一个 token
# 之前就已经全部拿到了，而且比它更完整。
#
# 实测 6 条 golden 查询，它被调用 **0 次** —— Agent 自己就没需要过。
# 但它的代价是实打实的：tool schema 要进每一次 Bedrock 请求的 payload，
# 而 `_AGENT_RULES` 原规则 1 还在主动引导「如不确定，调用 get_schema_section」，
# 一旦 Agent 听话就白烧一轮 ReAct（约 3-5 秒）并把 6000 字符重复内容
# 灌进 messages，反而**压低 prompt cache 命中率**。
#
# 若将来 schema 大到不宜整份进 system prompt，正确做法是按 section 切分
# system prompt 本身（或换检索），而不是恢复这个 tool —— 让 Agent 多跑
# 一轮去取它本来就有的东西，是净亏。


@tool
def validate_cypher(cypher: str) -> str:
    """Validate an openCypher query for safety (read-only + hop depth).

    ⚠️ 这是**可选的预检**工具，不是安全防线。真正的防线在 `execute_cypher`
    内部的 `query_guard.is_safe()` —— 它无条件执行，不依赖 Agent 是否先调本函数。

    `_AGENT_RULES` 自 2026-09-20 起**不再要求**执行前必须先调它：
    对安全查询（绝大多数）那是纯多一轮 ReAct；对不安全查询，直接
    `execute_cypher` 被 guard 挡回的反馈轮数与先 validate 完全相同。
    保留它是为了 Agent 想预检某个可疑写法时有个便宜的选择。

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
        # 完整 rows 交给引擎（不经 LLM）；trace 里只留行数，避免大结果污染 trace。
        _last_rows["cypher"] = cypher2
        _last_rows["rows"] = rows
        _calls.append({"tool": "execute_cypher", "cypher": cypher2, "cypher_preview": cypher2[:200], "rows": len(rows)})
    # 给 LLM 的仍是截断版：4000 字符够它总结，又不吃光上下文窗口。
    return json.dumps(rows, ensure_ascii=False, default=str)[:4000]


def last_rows() -> dict:
    """返回最近一次成功 execute_cypher 的 {cypher, rows}——**完整**结果，未截断。

    引擎用它填 `results` 字段。在这之前引擎是重新跑一次 Neptune 查询来拿
    完整结果的（因为 tool 返回给 LLM 的被截断到 4000 字符），
    等于每次查询都白跑两趟 Neptune。

    `rows` 为 None 表示本轮没有任何一次成功执行。
    """
    with _lock:
        return {"cypher": _last_rows["cypher"], "rows": _last_rows["rows"]}


# ---- 仅供引擎读取最后一次执行的 cypher + 结果 ----
def last_execution() -> dict:
    """返回最近一次 execute_cypher 的 {cypher, rows} 信息（engine 用来填 results 字段）。"""
    with _lock:
        snapshot = list(_calls)
    for c in reversed(snapshot):
        if c.get("tool") == "execute_cypher" and not c.get("blocked") and not c.get("error"):
            return c
    return {}
