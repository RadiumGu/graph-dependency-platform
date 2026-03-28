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
    ) -> ImpactReport:
        """Generate an ImpactReport for a given failure scenario.

        Args:
            subgraph: Dict with ``nodes`` and ``edges`` keys.
            scope: One of ``region``, ``az``, ``service``.
            source: Failure source identifier.

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
        spof = SPOFDetector().detect(subgraph)

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

    def _estimate_rpo(self, nodes: List[Dict[str, Any]]) -> int:
        """Estimate RPO in minutes based on data layer node types.

        Args:
            nodes: All affected nodes.

        Returns:
            Estimated RPO in minutes.
        """
        max_rpo = 0
        for node in nodes:
            rtype = node.get("type", "")
            if rtype in ("RDSCluster", "RDSInstance"):
                max_rpo = max(max_rpo, 5)
            elif rtype == "DynamoDBTable":
                max_rpo = max(max_rpo, 0)
            elif rtype in ("S3Bucket",):
                max_rpo = max(max_rpo, 60)
        return max_rpo

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
