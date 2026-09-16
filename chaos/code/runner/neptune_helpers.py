"""
neptune_helpers.py — 公共 Neptune/Gremlin 查询 helper。

从 hypothesis_direct.py 的私有方法抽取，供 learning_tools.py、hypothesis_tools.py 等共用。
解决 retro § 3.5 指出的问题：Strands @tool 不应依赖 Direct class 私有方法。

依赖：runner/neptune_client.py（底层 Gremlin 客户端）
"""
from __future__ import annotations

import logging
from typing import Any

from runner.neptune_client import query_gremlin_parsed  # type: ignore

logger = logging.getLogger(__name__)


def gremlin_query(gremlin: str) -> list:
    """执行 Neptune Gremlin 查询，返回解析后的 Python 对象列表。

    等价于 hypothesis_direct._gremlin_query()，但作为公共 API 暴露。
    """
    return query_gremlin_parsed(gremlin)


def query_topology(service_filter: str = "") -> list[dict]:
    """查询微服务拓扑（name, tier, deps, callers, resources）。"""
    gremlin = """
    g.V().hasLabel('Microservice')
      .project('name','tier','deps','callers','resources')
      .by('name')
      .by('recovery_priority')
      .by(out('DependsOn').values('name').fold())
      .by(in('DependsOn').values('name').fold())
      .by(out('RunsOn','DependsOn').hasLabel('LambdaFunction','RDSCluster','DynamoDBTable','SQSQueue').values('name').fold())
    """
    rows = query_gremlin_parsed(gremlin)
    if service_filter:
        rows = [r for r in rows if r.get("name") == service_filter]
    return rows


def query_infra_snapshot(services: list[str]) -> dict:
    """通过 TargetResolver 获取实时基础设施快照。

    抽取自 DirectBedrockHypothesis._query_infra_snapshot()。
    返回 dict keyed by service name: {pod_count, nodes, aws_resources, ...}
    """
    if not services:
        return {}
    try:
        from runner.target_resolver import TargetResolver  # type: ignore
        resolver = TargetResolver()
        snapshot = resolver.get_infra_snapshot(services)
        logger.info(f"基础设施快照: {len(snapshot)} 个服务")
        return snapshot
    except Exception as e:
        logger.warning(f"基础设施快照获取失败（非致命）: {e}")
        return {}


def query_learning_nodes(service_filter: str = "") -> list[dict]:
    """查询 Neptune 中已有的学习节点和边（LearningAgent 专用）。

    属性名说明（2026-08-28 修正）：本函数原先读 chaos_resilience_score /
    last_tested_at，但活图里这两个属性 **0 个节点** 拥有 —— 真正落数据的是
    runner/graph_feedback.py 写的 resilience_score / last_chaos_test（6 个节点）。
    四个投影当时全部命中兜底值，LearningAgent 一直在拿空数据决策。

    现以 resilience_score / last_chaos_test 为主，并保留对旧名的 coalesce 回退
    一个版本周期，避免历史数据（若某处确实写过旧名）在切换瞬间读不到。
    注意旧名 chaos_resilience_score 是 0-1 量纲，新名是 0-100，回退分支已乘 100 归一。
    """
    gremlin = """
    g.V().hasLabel('Microservice')
      .project('name','resilience_score','last_tested','coverage','weakness')
      .by('name')
      .by(coalesce(
            values('resilience_score'),
            __.values('chaos_resilience_score').math('_ * 100'),
            constant(-1)))
      .by(coalesce(values('last_chaos_test'), values('last_tested_at'), constant('never')))
      .by(coalesce(values('test_coverage'), constant('')))
      .by(coalesce(values('weakness_pattern'), constant('')))
    """
    rows = query_gremlin_parsed(gremlin)
    if service_filter:
        rows = [r for r in rows if r.get("name") == service_filter]
    return rows
