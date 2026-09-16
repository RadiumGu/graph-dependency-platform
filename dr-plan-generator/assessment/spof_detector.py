"""
assessment/spof_detector.py — Single Point of Failure (SPOF) detection

Uses Neptune Q16 to identify resources deployed in only one AZ
but depended on by multiple services.
"""

import logging
from typing import Any, Dict, List, Optional

from registry.registry_loader import ServiceTypeRegistry, get_registry

logger = logging.getLogger(__name__)


class SPOFDetector:
    """Detect single points of failure in the dependency graph.

    Sources:
    - Neptune Q16: resources with single-AZ deployment and multiple dependents.
    - Local subgraph analysis: data stores without replication metadata.
    """

    def __init__(self, registry: Optional[ServiceTypeRegistry] = None) -> None:
        """Initialize SPOFDetector.

        Args:
            registry: Optional ServiceTypeRegistry instance. If None, the
                module-level singleton is used.
        """
        self._registry: ServiceTypeRegistry = registry if registry is not None else get_registry()

    def detect(
        self, subgraph: Dict[str, Any], offline: bool = False
    ) -> List[Dict[str, Any]]:
        """Detect SPOF risks from a subgraph and (when online) Neptune Q16.

        Args:
            subgraph: Subgraph dict with ``nodes`` and ``edges`` keys.
            offline: When True, skip the Neptune Q16 query entirely and analyse
                only the supplied subgraph. Set this on the disaster-time path:
                relying on the ``except`` fallback is not good enough, because a
                Region that is *down* does not refuse connections quickly — the
                request blocks until timeout, silently inflating RTO at the worst
                possible moment.

        Returns:
            List of SPOF dicts, each with keys:
            ``resource``, ``type``, ``risk``, ``az``, ``impact``,
            ``recommendation``.
        """
        if offline:
            logger.info("Offline mode: skipping Neptune Q16, using local subgraph analysis.")
            return self._detect_from_subgraph(subgraph)

        from graph import queries

        spof_list: List[Dict[str, Any]] = []

        # Neptune Q16 results
        try:
            q16_results = queries.q16_single_point_of_failure()
            for r in q16_results:
                spof_list.append({
                    "resource": r.get("resource_name", ""),
                    "type": r.get("type", ""),
                    "risk": "single_az",
                    "az": r.get("single_az", ""),
                    "impact": r.get("services", []),
                    "recommendation": "Deploy to multiple AZs or add cross-region replica",
                })
        except Exception as exc:
            logger.warning("Neptune Q16 query failed, falling back to local analysis: %s", exc)
            spof_list.extend(self._detect_from_subgraph(subgraph))

        # ── 第二种故障模型：拓扑咽喉点 ────────────────────────────────────
        #
        # Q16 的判据是「**只在单个 AZ** 且被 >=2 个服务依赖」，它对区域级托管服务
        # **完全失明**：实测 AgentGateway 与全部 6 个 AgentRuntime 的 LocatedIn 边
        # 都是 0，Q16 的 size([...LocatedIn...]) = 1 恒为假。
        #
        # 而 AgentCore 网关承载全部 agent 间流量，它失效会让 orchestrator 到
        # 4 个子 agent 全断 —— 一个 Q16 永远看不见的单点故障。
        #
        # **刻意用独立的 risk 取值**（topology_chokepoint）而不是混进 single_az：
        # 两者的处置完全不同 —— single_az 的答案是「跨 AZ 部署」，
        # 咽喉点的答案是「加旁路或让它冗余」。合成一类会让恢复建议无法生成。
        #
        # **独立 try/except**：这条查询比 Q16 重（变长路径），失败不得影响
        # 已经拿到的 Q16 结果 —— 也不回落到子图分析（那只看 AZ，答不了咽喉点）。
        try:
            for r in queries.q_articulation_chokepoints(min_blocked=2):
                blocked = r.get("blocked", 0)
                spof_list.append({
                    "resource": r.get("chokepoint", ""),
                    "type": r.get("type", ""),
                    "risk": "topology_chokepoint",
                    "az": "",          # 与 AZ 无关，刻意留空而不是填 unknown
                    "impact": r.get("blocked_sample", []),
                    "blocked_count": blocked,
                    "upstream_count": r.get("upstream", 0),
                    "recommendation": (
                        f"该节点是 {blocked} 个下游的唯一通路。"
                        "加旁路、或让它自身冗余；跨 AZ 部署对这类风险无效。"
                    ),
                })
        except Exception as exc:      # noqa: BLE001
            logger.warning("咽喉点查询失败（不影响 Q16 结果）: %s", exc)

        return spof_list

    def _detect_from_subgraph(self, subgraph: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Fallback SPOF detection from local subgraph data.

        Identifies data-layer resources with a single AZ assignment and
        at least two dependent services (from edge analysis).

        Args:
            subgraph: Subgraph dict.

        Returns:
            List of SPOF dicts.
        """
        nodes = subgraph.get("nodes", [])
        edges = subgraph.get("edges", [])

        # Map: resource_name → list of dependent service names
        dependents: Dict[str, List[str]] = {}
        for edge in edges:
            dst = edge.get("to", "")
            src = edge.get("from", "")
            if dst and src:
                dependents.setdefault(dst, []).append(src)

        spof_list: List[Dict[str, Any]] = []

        for node in nodes:
            name = node.get("name", "")
            rtype = node.get("type", "")
            az = node.get("az", "")
            deps = dependents.get(name, [])

            if self._registry.is_spof_candidate(rtype) and az and len(deps) >= 2:
                spof_list.append({
                    "resource": name,
                    "type": rtype,
                    "risk": "single_az",
                    "az": az,
                    "impact": deps,
                    "recommendation": "Deploy to multiple AZs or add cross-region replica",
                })

        return spof_list
