"""
assessment/rto_estimator.py — RTO estimation based on switchover steps

Handles serial step accumulation and parallel group optimization.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class RTOEstimator:
    """Estimate Recovery Time Objective (RTO) from DR plan phases.

    Serial steps accumulate; steps within the same parallel_group
    contribute only the maximum duration of the group.
    """

    # Default switchover times per resource type (seconds)
    DEFAULT_TIMES: Dict[str, int] = {
        "RDSCluster": 300,       # Aurora failover ~5 min
        "RDSInstance": 600,      # RDS reboot ~10 min
        "DynamoDBTable": 60,     # Global Table — second-level switch
        "S3Bucket": 30,          # Replication verification
        "SQSQueue": 30,          # Endpoint switch
        "SNSTopic": 30,
        "Microservice": 120,     # Rollout + health check
        "LambdaFunction": 30,    # Function verification
        "LoadBalancer": 180,     # Health check + DNS propagation
        "K8sService": 60,        # Service endpoint update
        "EC2Instance": 180,
        "EKSCluster": 300,
        "Pod": 60,
    }

    def estimate(self, phases: List[Any]) -> int:
        """Calculate estimated RTO in minutes across all phases.

        Accounts for serial steps (sum), parallel groups (max), and
        a 60-second inter-phase gate/verification overhead.

        Args:
            phases: List of DRPhase objects.

        Returns:
            Estimated RTO in minutes (minimum 1).
        """
        total_seconds = 0
        for phase in phases:
            phase_seconds = self._estimate_phase(phase)
            total_seconds += phase_seconds
            total_seconds += 60  # inter-phase gate time

        return max(1, total_seconds // 60)

    def estimate_from_subgraph(self, subgraph: Dict[str, Any]) -> int:
        """Rough RTO estimate from a subgraph node list (no phase structure).

        Assumes all nodes are serial (conservative upper bound).

        Args:
            subgraph: Dict with ``nodes`` key.

        Returns:
            Estimated RTO in minutes.
        """
        total_seconds = sum(
            self.DEFAULT_TIMES.get(n.get("type", ""), 60)
            for n in subgraph.get("nodes", [])
        )
        return max(1, total_seconds // 60)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _estimate_phase(self, phase: Any) -> int:
        """Calculate phase duration considering parallel groups.

        Args:
            phase: DRPhase object.

        Returns:
            Phase duration in seconds.
        """
        groups: Dict[str, List[int]] = {}
        serial_time = 0

        for step in phase.steps:
            if step.parallel_group:
                groups.setdefault(step.parallel_group, []).append(step.estimated_time)
            else:
                serial_time += step.estimated_time

        parallel_time = sum(max(times) for times in groups.values())
        return serial_time + parallel_time
