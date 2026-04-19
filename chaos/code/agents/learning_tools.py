"""
learning_tools.py — Strands @tool definitions for LearningAgent.

硬约束（TASK § 5.3）:
  - Neptune 查询走 neptune_helpers.py，不依赖 Direct class 私有方法
  - DynamoDB 查询复用 ExperimentQueryClient
  - 模块级 trace list + Lock（同 hypothesis_tools.py 模式）
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

from runner.neptune_helpers import (  # type: ignore
    gremlin_query,
    query_topology as _query_topo,
    query_learning_nodes as _query_ln,
    query_infra_snapshot as _query_infra,
)

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_calls: list = []


def reset_trace() -> None:
    with _lock:
        _calls.clear()


def get_trace() -> list:
    with _lock:
        return list(_calls)


# =========================================================
# Tools
# =========================================================

@tool
def query_experiment_history(service: str = "", limit: int = 50) -> str:
    """Query DynamoDB for chaos experiment history records.

    Args:
        service: Optional service name filter. Empty = all services.
        limit: Max records to return (default 50).

    Returns:
        JSON array of experiment summaries (service / fault_type / status / recovery_seconds).
    """
    try:
        from runner.query import ExperimentQueryClient  # type: ignore
        client = ExperimentQueryClient()
        if service:
            items = client.list_by_service(service, days=180, limit=int(limit))
        else:
            items = []
            for status in ("PASSED", "FAILED", "ABORTED"):
                items.extend(client.list_by_status(status, days=180))
            items = items[:int(limit)]
    except Exception as e:
        with _lock:
            _calls.append({"tool": "query_experiment_history", "error": repr(e)[:200]})
        return f"ERROR: {e!r}"

    summary = [
        {
            "service": it.get("target_service", {}).get("S", ""),
            "fault_type": it.get("fault_type", {}).get("S", ""),
            "status": it.get("status", {}).get("S", ""),
            "recovery_seconds": float(it.get("recovery_seconds", {}).get("N", -1)),
        }
        for it in items
    ]
    with _lock:
        _calls.append({"tool": "query_experiment_history", "service": service, "rows": len(summary)})
    return json.dumps(summary, ensure_ascii=False, default=str)[:6000]


@tool
def query_coverage_snapshot(services_csv: str = "") -> str:
    """Query Neptune for existing learning/coverage data per service.

    Args:
        services_csv: Comma-separated service names. Empty = all.

    Returns:
        JSON array of {name, resilience_score, last_tested, coverage, weakness}.
    """
    service_filter = ""
    if services_csv:
        # just take the first for single filter
        names = [s.strip() for s in services_csv.split(",") if s.strip()]
        service_filter = names[0] if len(names) == 1 else ""

    try:
        rows = _query_ln(service_filter=service_filter)
        if services_csv and not service_filter:
            # multi-service filter
            name_set = {s.strip() for s in services_csv.split(",") if s.strip()}
            rows = [r for r in rows if r.get("name") in name_set]
    except Exception as e:
        with _lock:
            _calls.append({"tool": "query_coverage_snapshot", "error": repr(e)[:200]})
        return f"ERROR: {e!r}"

    with _lock:
        _calls.append({"tool": "query_coverage_snapshot", "rows": len(rows)})
    return json.dumps(rows, ensure_ascii=False, default=str)[:6000]


@tool
def query_graph_learning_nodes(service: str = "") -> str:
    """Query Neptune for service topology relevant to learning analysis.

    Args:
        service: Optional service name filter.

    Returns:
        JSON array of {name, tier, deps, callers, resources}.
    """
    try:
        rows = _query_topo(service_filter=service)
    except Exception as e:
        with _lock:
            _calls.append({"tool": "query_graph_learning_nodes", "error": repr(e)[:200]})
        return f"ERROR: {e!r}"

    with _lock:
        _calls.append({"tool": "query_graph_learning_nodes", "service": service, "rows": len(rows)})
    return json.dumps(rows, ensure_ascii=False, default=str)[:6000]


@tool
def invoke_hypothesis_engine(coverage_gaps_csv: str, max_hypotheses: int = 5) -> str:
    """Generate supplementary hypotheses based on coverage gaps.

    Uses make_hypothesis_engine() from Phase 3 Module 1.

    Args:
        coverage_gaps_csv: Comma-separated service names with coverage gaps.
        max_hypotheses: Max hypotheses per service (default 5).

    Returns:
        JSON array of generated hypothesis summaries.
    """
    services = [s.strip() for s in (coverage_gaps_csv or "").split(",") if s.strip()]
    if not services:
        return "[]"

    try:
        # Force direct engine to avoid nested Strands agent (memory explosion)
        import os as _os
        old_val = _os.environ.get("HYPOTHESIS_ENGINE", "")
        _os.environ["HYPOTHESIS_ENGINE"] = "direct"
        try:
            from engines.factory import make_hypothesis_engine  # type: ignore
            engine = make_hypothesis_engine()
        finally:
            if old_val:
                _os.environ["HYPOTHESIS_ENGINE"] = old_val
            else:
                _os.environ.pop("HYPOTHESIS_ENGINE", None)
        all_hypotheses = []
        for svc in services[:3]:  # limit to 3 services to control cost
            generated = engine.generate(max_hypotheses=int(max_hypotheses), service_filter=svc)
            for h in generated:
                all_hypotheses.append({
                    "id": getattr(h, "id", ""),
                    "title": getattr(h, "title", ""),
                    "failure_domain": getattr(h, "failure_domain", ""),
                    "service": svc,
                })
    except Exception as e:
        with _lock:
            _calls.append({"tool": "invoke_hypothesis_engine", "error": repr(e)[:200]})
        return f"ERROR: {e!r}"

    with _lock:
        _calls.append({"tool": "invoke_hypothesis_engine", "services": services, "hypotheses": len(all_hypotheses)})
    return json.dumps(all_hypotheses, ensure_ascii=False, default=str)[:6000]
