"""
validation/chaos_exporter.py — Export DR plan assumptions as chaos experiments

Generates FIS-compatible chaos experiment YAML files for key DR steps
(RDS failover, AZ failure simulation, service recovery verification).
"""

import logging
import os
from typing import Any, Dict, List

import yaml

from models import DRPlan, DRStep

logger = logging.getLogger(__name__)


class ChaosExporter:
    """Export DR plan's key assumptions as chaos experiment YAML files.

    Produces:
    - RDS failover validation experiment per failover step.
    - AZ failure simulation experiment (for AZ-scoped plans).
    - Service recovery validation experiment.
    """

    def export(self, plan: DRPlan, output_dir: str) -> List[Dict[str, Any]]:
        """Generate chaos experiment YAMLs for a DR plan.

        Args:
            plan: The DRPlan to export experiments for.
            output_dir: Directory where YAML files are written.

        Returns:
            List of experiment dicts that were exported.
        """
        os.makedirs(output_dir, exist_ok=True)
        experiments: List[Dict[str, Any]] = []

        for phase in plan.phases:
            for step in phase.steps:
                if step.action == "promote_read_replica":
                    exp = self._build_rds_failover_experiment(step, plan)
                    experiments.append(exp)

        if plan.scope == "az":
            exp = self._build_az_failure_experiment(plan)
            experiments.append(exp)

        # Service recovery experiment for Tier0 services
        tier0_steps = [
            s for p in plan.phases for s in p.steps
            if s.tier == "Tier0" and s.resource_type == "Microservice"
        ]
        if tier0_steps:
            exp = self._build_service_recovery_experiment(tier0_steps, plan)
            experiments.append(exp)

        for exp in experiments:
            path = os.path.join(output_dir, f"{exp['name']}.yaml")
            with open(path, "w", encoding="utf-8") as f:
                yaml.dump(exp, f, default_flow_style=False, allow_unicode=True)
            logger.info("Exported chaos experiment: %s", path)

        return experiments

    # ------------------------------------------------------------------
    # Experiment builders
    # ------------------------------------------------------------------

    def _build_rds_failover_experiment(
        self, step: DRStep, plan: DRPlan
    ) -> Dict[str, Any]:
        """Build an FIS RDS cluster failover validation experiment.

        Args:
            step: The DRStep representing the RDS failover.
            plan: Parent DRPlan for metadata.

        Returns:
            Experiment dict.
        """
        return {
            "name": f"dr-validate-{step.resource_name}-failover",
            "description": (
                f"Validate RDS failover time for {step.resource_name}. "
                f"Expected completion within {step.estimated_time}s."
            ),
            "backend": "fis",
            "target": {
                "service": step.resource_name,
                "resource_type": "rds:cluster",
            },
            "fault": {
                "type": "aws:rds:failover-db-cluster",
                "duration": f"{step.estimated_time}s",
            },
            "steady_state": {
                "success_rate_threshold": 95,
            },
            "stop_conditions": [
                {
                    "metric": "success_rate",
                    "threshold": 80,
                }
            ],
            "rca": {"enabled": True},
            "tags": {
                "source": "dr-plan-generator",
                "plan_id": plan.plan_id,
                "phase": "data-layer-validation",
            },
        }

    def _build_az_failure_experiment(self, plan: DRPlan) -> Dict[str, Any]:
        """Build an AZ failure simulation experiment.

        Args:
            plan: DRPlan with scope ``az``.

        Returns:
            Experiment dict.
        """
        return {
            "name": f"dr-validate-az-failure-{plan.source}",
            "description": (
                f"Simulate AZ failure for {plan.source} and validate "
                f"traffic migration to {plan.target}."
            ),
            "backend": "fis",
            "target": {
                "az": plan.source,
                "resource_type": "aws:ec2:subnet",
            },
            "fault": {
                "type": "aws:network:disrupt-connectivity",
                "duration": "10m",
                "scope": plan.source,
            },
            "steady_state": {
                "success_rate_threshold": 90,
            },
            "stop_conditions": [
                {
                    "metric": "success_rate",
                    "threshold": 70,
                }
            ],
            "rca": {"enabled": True},
            "tags": {
                "source": "dr-plan-generator",
                "plan_id": plan.plan_id,
                "phase": "az-failure-validation",
            },
        }

    def _build_service_recovery_experiment(
        self, steps: List[DRStep], plan: DRPlan
    ) -> Dict[str, Any]:
        """Build a Tier0 service recovery time validation experiment.

        Args:
            steps: List of Tier0 Microservice DRSteps.
            plan: Parent DRPlan.

        Returns:
            Experiment dict.
        """
        services = [s.resource_name for s in steps]
        max_recovery_time = max(s.estimated_time for s in steps)

        return {
            "name": f"dr-validate-service-recovery-{plan.plan_id}",
            "description": (
                f"Validate that Tier0 services ({', '.join(services)}) "
                f"recover within {max_recovery_time}s during DR."
            ),
            "backend": "k8s",
            "targets": [
                {"deployment": svc, "namespace": "default"}
                for svc in services
            ],
            "fault": {
                "type": "pod-failure",
                "duration": "5m",
                "selector": {"tier": "Tier0"},
            },
            "steady_state": {
                "readiness_threshold": 2,
                "timeout_seconds": max_recovery_time,
            },
            "stop_conditions": [
                {
                    "metric": "ready_replicas",
                    "threshold": 1,
                }
            ],
            "rca": {"enabled": True},
            "tags": {
                "source": "dr-plan-generator",
                "plan_id": plan.plan_id,
                "phase": "compute-layer-validation",
            },
        }
