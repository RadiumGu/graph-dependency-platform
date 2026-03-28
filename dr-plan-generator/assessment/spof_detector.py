"""
assessment/spof_detector.py — Single Point of Failure (SPOF) detection

Uses Neptune Q16 to identify resources deployed in only one AZ
but depended on by multiple services.
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class SPOFDetector:
    """Detect single points of failure in the dependency graph.

    Sources:
    - Neptune Q16: resources with single-AZ deployment and multiple dependents.
    - Local subgraph analysis: data stores without replication metadata.
    """

    def detect(self, subgraph: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Detect SPOF risks from a subgraph and Neptune Q16 results.

        Args:
            subgraph: Subgraph dict with ``nodes`` and ``edges`` keys.

        Returns:
            List of SPOF dicts, each with keys:
            ``resource``, ``type``, ``risk``, ``az``, ``impact``,
            ``recommendation``.
        """
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

        # Resource types that are truly AZ-bound (SPOF candidates)
        # DynamoDB, SQS, SNS, S3 are regional managed services — inherently
        # multi-AZ, so they should NOT be flagged as single-AZ SPOF.
        az_bound_types = {
            "RDSCluster", "RDSInstance",
            "NeptuneCluster", "NeptuneInstance",
            "EC2Instance",
            "EKSCluster",
        }

        for node in nodes:
            name = node.get("name", "")
            rtype = node.get("type", "")
            az = node.get("az", "")
            deps = dependents.get(name, [])

            if rtype in az_bound_types and az and len(deps) >= 2:
                spof_list.append({
                    "resource": name,
                    "type": rtype,
                    "risk": "single_az",
                    "az": az,
                    "impact": deps,
                    "recommendation": "Deploy to multiple AZs or add cross-region replica",
                })

        return spof_list
