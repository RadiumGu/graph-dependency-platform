"""
assessment/impact_analyzer.py — DR impact assessment

Produces an ImpactReport from a Neptune subgraph, including tier
breakdown, business capability impact, SPOF list, and RTO/RPO estimates.
"""

import logging
from typing import Any, Dict, List, Optional

from models import ImpactReport

logger = logging.getLogger(__name__)


class ImpactAnalyzer:
    """Generate impact assessment reports from affected subgraphs.

    Combines tier-based grouping, SPOF detection, and RTO/RPO estimation
    into a structured ImpactReport.
    """

    def assess_impact(
        self,
        subgraph: Dict[str, Any],
        scope: str,
        source: str,
        offline: bool = False,
    ) -> ImpactReport:
        """Generate an ImpactReport for a given failure scenario.

        Args:
            subgraph: Dict with ``nodes`` and ``edges`` keys.
            scope: One of ``region``, ``az``, ``service``.
            source: Failure source identifier.
            offline: When True, no Neptune query is issued (disaster-time path).

        Returns:
            Populated ImpactReport.
        """
        from assessment.rto_estimator import RTOEstimator
        from assessment.spof_detector import SPOFDetector

        nodes = subgraph.get("nodes", [])

        # Group nodes by tier
        by_tier: Dict[str, List[Dict[str, Any]]] = {
            "Tier0": [], "Tier1": [], "Tier2": [], "Unknown": []
        }
        for node in nodes:
            tier = node.get("tier") or "Unknown"
            by_tier.setdefault(tier, []).append(node)

        # Business capability nodes
        capabilities = [n for n in nodes if n.get("type") == "BusinessCapability"]

        # SPOF detection
        spof = SPOFDetector().detect(subgraph, offline=offline)

        # RTO/RPO estimation
        rto = RTOEstimator().estimate_from_subgraph(subgraph)
        rpo = self._estimate_rpo(nodes)

        return ImpactReport(
            scope=scope,
            source=source,
            total_affected=len(nodes),
            by_tier=by_tier,
            affected_capabilities=capabilities,
            single_points_of_failure=spof,
            estimated_rto_minutes=rto,
            estimated_rpo_minutes=rpo,
            risk_matrix=self._build_risk_matrix(by_tier, spof),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _estimate_rpo(self, nodes: List[Dict[str, Any]]) -> Optional[int]:
        """按实际复制拓扑推导 RPO（分钟）。

        原实现是与 ``plan_generator._estimate_rpo`` 同源的硬编码表
        （RDS=5 / DynamoDB=0 / S3=60），与实际配置无关，审计答不上来。
        现改为委托 ``RPOEstimator``。

        **不可推定时返回 ``None``，绝不返回 0。** 早期版本因为
        ``ImpectReport.estimated_rpo_minutes`` 是 ``int`` 而在推不出时返回 0，
        于是报告里渲染成「Estimated RPO | 0 min」—— 容灾语境下 0 是
        **零数据丢失**，最令人安心的那个值，真实含义却是「不知道」。
        字段已改为 ``Optional[int]``，渲染侧对 None 显式输出「不可推定」。

        Args:
            nodes: 受影响节点。

        Returns:
            RPO 分钟数；不可推定时为 ``None``。
        """
        from assessment.rpo_estimator import RPOEstimator

        try:
            from dr_profile import get_active_profile

            profile = get_active_profile()
        except Exception:  # noqa: BLE001
            profile = None
        return RPOEstimator(profile=profile).assess(nodes).minutes

    def _build_risk_matrix(
        self,
        by_tier: Dict[str, List[Any]],
        spof: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Build a simple risk matrix.

        Args:
            by_tier: Nodes grouped by tier.
            spof: List of SPOF dicts.

        Returns:
            Risk matrix dict with severity and key risks.
        """
        tier0_count = len(by_tier.get("Tier0", []))
        spof_count = len(spof)

        if tier0_count > 0 and spof_count > 0:
            severity = "HIGH"
        elif tier0_count > 0 or spof_count > 0:
            severity = "MEDIUM"
        else:
            severity = "LOW"

        return {
            "severity": severity,
            "tier0_services_affected": tier0_count,
            "single_points_of_failure": spof_count,
            "key_risks": [s["resource"] for s in spof[:3]],
        }
