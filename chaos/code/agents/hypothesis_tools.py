"""
hypothesis_tools.py — Strands @tool definitions for HypothesisAgent.

硬约束（TASK § 6）:
  - 复用 runner/neptune_client.py 的 Gremlin 客户端，不重写查询
  - 业务逻辑等价：读拓扑 / 历史 incident / 实验历史 / 基础设施快照

参考 rca/neptune/strands_tools.py（L1 Smart Query）— 模块级 trace list + Lock
以绕过 Strands 内部的 async/thread pool。
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any

from strands import tool  # type: ignore

import os as _os
import sys as _sys
_CHAOS_CODE = _os.path.abspath(_os.path.join(_os.path.dirname(__file__), ".."))
if _CHAOS_CODE not in _sys.path:
    _sys.path.insert(0, _CHAOS_CODE)

from runner.neptune_client import query_gremlin_parsed  # type: ignore  # noqa: E402

logger = logging.getLogger(__name__)


_lock = threading.Lock()
_calls: list = []
_context: dict = {"topology_cache": None, "incidents_cache": None}


def reset_trace() -> None:
    with _lock:
        _calls.clear()


def get_trace() -> list:
    with _lock:
        return list(_calls)


def reset_context() -> None:
    _context["topology_cache"] = None
    _context["incidents_cache"] = None


# =========================================================
# Tools
# =========================================================

@tool
def query_topology(service_filter: str = "") -> str:
    """Query Neptune for the Microservice dependency topology.

    Args:
        service_filter: Optional service name. Empty string = all services.

    Returns:
        JSON array of services with their tier, deps, callers, resources.
    """
    gremlin = """
    g.V().hasLabel('Microservice')
      .project('name','tier','deps','callers','resources')
      .by('name')
      .by('recovery_priority')
      .by(out('DependsOn').values('name').fold())
      .by(in('DependsOn').values('name').fold())
      .by(out('RunsOn','DependsOn').hasLabel('LambdaFunction','RDSCluster','DynamoDBTable','SQSQueue').values('name').fold())
    """
    try:
        rows = query_gremlin_parsed(gremlin)
    except Exception as e:
        with _lock:
            _calls.append({"tool": "query_topology", "error": repr(e)[:200]})
        return f"ERROR: {e!r}"
    if service_filter:
        rows = [r for r in rows if r.get("name") == service_filter]
    _context["topology_cache"] = rows
    with _lock:
        _calls.append({"tool": "query_topology", "service_filter": service_filter, "rows": len(rows)})
    return json.dumps(rows, ensure_ascii=False, default=str)[:6000]


@tool
def query_recent_incidents(limit: int = 20) -> str:
    """Query recent Incident events from Neptune graph.

    Args:
        limit: Max incidents to return (default 20).

    Returns:
        JSON array of {service, type, duration, impact}.
    """
    gremlin = """
    g.V().hasLabel('Incident')
      .project('service','type','duration','impact')
      .by(out('AffectedService').values('name'))
      .by('incident_type')
      .by('duration_minutes')
      .by('impact_level')
    """
    try:
        rows = query_gremlin_parsed(gremlin)
    except Exception as e:
        with _lock:
            _calls.append({"tool": "query_recent_incidents", "error": repr(e)[:200]})
        return f"ERROR: {e!r}"
    rows = rows[: int(limit)]
    _context["incidents_cache"] = rows
    with _lock:
        _calls.append({"tool": "query_recent_incidents", "rows": len(rows)})
    return json.dumps(rows, ensure_ascii=False, default=str)[:4000]


@tool
def query_fault_history(service: str = "") -> str:
    """Query DynamoDB for historic chaos experiment results.

    Args:
        service: Optional service name filter.

    Returns:
        JSON array summarizing past experiments (service / fault type / status).
    """
    try:
        from runner.query import ExperimentQueryClient  # type: ignore
        client = ExperimentQueryClient()
        if service:
            items = client.list_by_service(service, days=180, limit=50)
        else:
            # 所有服务（数据量控制在 30 条以内，避免 tool output 超限）
            items = client.list_all(days=180, limit=30)
    except Exception as e:
        with _lock:
            _calls.append({"tool": "query_fault_history", "error": repr(e)[:200]})
        return f"ERROR: {e!r}"
    summary = [
        {
            "service": it.get("target_service", {}).get("S", ""),
            "fault": it.get("fault_type", {}).get("S", ""),
            "status": it.get("status", {}).get("S", ""),
        }
        for it in items
    ]
    with _lock:
        _calls.append({"tool": "query_fault_history", "rows": len(summary)})
    return json.dumps(summary, ensure_ascii=False, default=str)[:4000]


@tool
def query_infra_snapshot(services_csv: str) -> str:
    """Query current infra snapshot (Pod count / node distribution / AWS resources)
    for the given services.

    Args:
        services_csv: Comma-separated service names, e.g. "petsite,pethistory".

    Returns:
        JSON object keyed by service name with pod count / node spread / AWS resources.
    """
    names = [s.strip() for s in (services_csv or "").split(",") if s.strip()]
    if not names:
        return "{}"
    try:
        from runner.neptune_helpers import query_infra_snapshot as _query_snap  # type: ignore
        snap = _query_snap(names)
    except Exception as e:
        with _lock:
            _calls.append({"tool": "query_infra_snapshot", "error": repr(e)[:200]})
        return f"ERROR: {e!r}"
    with _lock:
        _calls.append({"tool": "query_infra_snapshot", "services": names, "keys": list(snap.keys())})
    return json.dumps(snap, ensure_ascii=False, default=str)[:4000]
