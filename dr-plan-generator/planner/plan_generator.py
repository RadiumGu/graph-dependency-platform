"""
planner/plan_generator.py — DR switchover plan generation engine

Orchestrates graph analysis, layer classification, topological sorting,
step building, and phase assembly into a complete DRPlan.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from models import DRPhase, DRPlan, DRStep

logger = logging.getLogger(__name__)


class PlanGenerator:
    """Main DR switchover plan generation engine.

    Coordinates GraphAnalyzer (subgraph extraction + sorting) with
    StepBuilder (command generation) to produce a structured DRPlan.
    """

    def __init__(self, analyzer: Any, step_builder: Any) -> None:
        """Initialise with injected analyzer and step builder.

        Args:
            analyzer: GraphAnalyzer instance.
            step_builder: StepBuilder instance.
        """
        self.analyzer = analyzer
        self.step_builder = step_builder

    def generate_plan(
        self,
        scope: str,
        source: str,
        target: str,
        exclude: Optional[List[str]] = None,
        options: Optional[Dict[str, Any]] = None,
    ) -> DRPlan:
        """Generate a complete DR switchover plan.

        Steps:
        1. Extract affected subgraph from Neptune.
        2. Optionally filter excluded services.
        3. Classify nodes into layers (L0–L3).
        4. Topologically sort within each layer.
        5. Build Phase 0–4.
        6. Assess impact and estimate RTO/RPO.

        Args:
            scope: One of ``region``, ``az``, ``service``.
            source: Failure source identifier.
            target: DR target identifier.
            exclude: Optional list of service names to exclude.
            options: Optional extra options dict.

        Returns:
            Fully populated DRPlan.
        """
        from assessment.impact_analyzer import ImpactAnalyzer
        from assessment.rto_estimator import RTOEstimator

        subgraph = self.analyzer.extract_affected_subgraph(scope, source)

        if exclude:
            subgraph = self._filter_excluded(subgraph, exclude)

        layers = self.analyzer.classify_by_layer(subgraph)
        sorted_layers: Dict[str, List[str]] = {}
        for layer_name, nodes in layers.items():
            sorted_layers[layer_name] = self.analyzer.topological_sort_within_layer(
                nodes, subgraph["edges"]
            )

        phases = self._build_phases(sorted_layers, layers, subgraph, source, target, options, scope=scope)

        impact = ImpactAnalyzer().assess_impact(subgraph, scope, source)
        rto = RTOEstimator().estimate(phases)
        rpo = self._estimate_rpo(layers.get("L0", []))

        plan_id = f"dr-{scope}-{int(time.time())}"
        now = datetime.now(timezone.utc).isoformat()

        return DRPlan(
            plan_id=plan_id,
            created_at=now,
            scope=scope,
            source=source,
            target=target,
            affected_services=[
                n["name"]
                for n in subgraph["nodes"]
                if n.get("type") in ("Microservice", "K8sService")
            ],
            affected_resources=[n["name"] for n in subgraph["nodes"]],
            phases=phases,
            rollback_phases=[],
            impact_assessment=impact,
            estimated_rto=rto,
            estimated_rpo=rpo,
            validation_status="pending",
            graph_snapshot_time=now,
        )

    # ------------------------------------------------------------------
    # Phase builders
    # ------------------------------------------------------------------

    def _build_phases(
        self,
        sorted_layers: Dict[str, List[str]],
        layers: Dict[str, List[Dict[str, Any]]],
        subgraph: Dict[str, Any],
        source: str,
        target: str,
        options: Optional[Dict[str, Any]],
        scope: str = "region",
    ) -> List[DRPhase]:
        """Assemble the five standard DR phases (0–4).

        Args:
            sorted_layers: Layer name → sorted list of node names.
            layers: Layer name → list of node dicts.
            subgraph: Full subgraph dict.
            source: Source identifier.
            target: Target identifier.
            options: Extra options.

        Returns:
            List of DRPhase objects.
        """
        node_map = {n["name"]: n for n in subgraph["nodes"]}
        phases: List[DRPhase] = []

        # Phase 0: Pre-flight
        phases.append(self._build_preflight_phase(source, target, layers))

        # Phase 1: Data Layer (L0)
        l0_nodes = [node_map[name] for name in sorted_layers.get("L0", []) if name in node_map]
        if l0_nodes:
            phases.append(self._build_data_phase(l0_nodes, source, target, scope=scope))

        # Phase 2: Compute Layer (L1 + L2)
        l1_names = sorted_layers.get("L1", [])
        l2_names = sorted_layers.get("L2", [])
        compute_nodes = [
            node_map[name]
            for name in (l1_names + l2_names)
            if name in node_map
        ]
        if compute_nodes:
            phases.append(self._build_compute_phase(compute_nodes, source, target, scope=scope))

        # Phase 3: Network / Traffic Layer (L3)
        l3_nodes = [node_map[name] for name in sorted_layers.get("L3", []) if name in node_map]
        if l3_nodes:
            phases.append(self._build_network_phase(l3_nodes, source, target, scope=scope))

        # Phase 4: Post-switchover Validation
        phases.append(self._build_validation_phase(sorted_layers, layers))

        return phases

    def _build_preflight_phase(
        self,
        source: str,
        target: str,
        layers: Dict[str, List[Dict[str, Any]]],
    ) -> DRPhase:
        """Build Phase 0: Pre-flight checks.

        Args:
            source: Source identifier.
            target: Target identifier.
            layers: Layer-classified node dict.

        Returns:
            DRPhase for pre-flight.
        """
        steps: List[DRStep] = []
        order = 1

        # Step 0.1: Target connectivity
        steps.append(
            DRStep(
                step_id="preflight-connectivity",
                order=order,
                resource_type="AWS",
                resource_id="",
                resource_name=target,
                action="check_target_connectivity",
                command=f"aws sts get-caller-identity --region {target}",
                validation="echo $?",
                expected_result="0",
                rollback_command="# No rollback needed for connectivity check",
                estimated_time=10,
                requires_approval=False,
                tier=None,
                dependencies=[],
            )
        )
        order += 1

        # Step 0.2: RDS replication lag checks
        for node in layers.get("L0", []):
            if node.get("type") == "RDSCluster":
                steps.append(
                    DRStep(
                        step_id=f"preflight-repl-{node['name']}",
                        order=order,
                        resource_type="RDSCluster",
                        resource_id=node.get("id", ""),
                        resource_name=node["name"],
                        action="check_replication_lag",
                        command=(
                            f"aws rds describe-db-clusters "
                            f"--db-cluster-identifier {node['name']} "
                            f"--region {source} "
                            f"--query 'DBClusters[0].ReplicationSourceIdentifier'"
                        ),
                        validation="# ReplicaLag should be < 1000ms",
                        expected_result="ReplicaLag < 1000ms",
                        rollback_command="# No rollback needed for lag check",
                        estimated_time=15,
                        requires_approval=False,
                        tier=node.get("tier"),
                        dependencies=[],
                    )
                )
                order += 1

        # Step 0.3: Lower DNS TTL
        steps.append(
            DRStep(
                step_id="preflight-dns-ttl",
                order=order,
                resource_type="Route53",
                resource_id="",
                resource_name="dns-ttl",
                action="lower_dns_ttl",
                command=(
                    "aws route53 change-resource-record-sets "
                    "--hosted-zone-id $ZONE_ID "
                    "--change-batch '{\"Changes\":[{\"Action\":\"UPSERT\","
                    "\"ResourceRecordSet\":{\"TTL\":60}}]}'"
                ),
                validation=(
                    "aws route53 list-resource-record-sets "
                    "--hosted-zone-id $ZONE_ID "
                    "--query 'ResourceRecordSets[?Name==`petsite.example.com.`].TTL'"
                ),
                expected_result="60",
                rollback_command=(
                    "aws route53 change-resource-record-sets "
                    "--hosted-zone-id $ZONE_ID "
                    "--change-batch '{\"Changes\":[{\"Action\":\"UPSERT\","
                    "\"ResourceRecordSet\":{\"TTL\":300}}]}'"
                ),
                estimated_time=30,
                requires_approval=False,
                tier=None,
                dependencies=[],
            )
        )

        total_secs = sum(s.estimated_time for s in steps)
        return DRPhase(
            phase_id="phase-0",
            name="Pre-flight Check",
            layer="preflight",
            steps=steps,
            estimated_duration=max(1, total_secs // 60),
            gate_condition="All preflight checks passed, replication lag within threshold",
        )

    def _build_data_phase(
        self,
        nodes: List[Dict[str, Any]],
        source: str,
        target: str,
        scope: str = "region",
    ) -> DRPhase:
        """Build Phase 1: Data layer switchover.

        Args:
            nodes: L0 data-layer nodes (sorted).
            source: Source identifier.
            target: Target identifier.
            scope: Switchover scope (region/az/service).

        Returns:
            DRPhase for data layer.
        """
        steps = self._nodes_to_steps(nodes, source, target, base_order=1, scope=scope)
        total_secs = sum(s.estimated_time for s in steps)
        return DRPhase(
            phase_id="phase-1",
            name="Data Layer Switchover",
            layer="L0",
            steps=steps,
            estimated_duration=max(1, total_secs // 60),
            gate_condition="All data stores reachable and writable in target region",
        )

    def _build_compute_phase(
        self,
        nodes: List[Dict[str, Any]],
        source: str,
        target: str,
        scope: str = "region",
    ) -> DRPhase:
        """Build Phase 2: Compute layer activation.

        Args:
            nodes: L1 + L2 compute nodes (sorted).
            source: Source identifier.
            target: Target identifier.
            scope: Switchover scope (region/az/service).

        Returns:
            DRPhase for compute layer.
        """
        steps = self._nodes_to_steps(nodes, source, target, base_order=1, scope=scope)
        total_secs = sum(s.estimated_time for s in steps)
        return DRPhase(
            phase_id="phase-2",
            name="Compute Layer Activation",
            layer="L2",
            steps=steps,
            estimated_duration=max(1, total_secs // 60),
            gate_condition="All Tier0 services healthy in target",
        )

    def _build_network_phase(
        self,
        nodes: List[Dict[str, Any]],
        source: str,
        target: str,
        scope: str = "region",
    ) -> DRPhase:
        """Build Phase 3: Network/traffic layer cutover.

        Args:
            nodes: L3 traffic-layer nodes (sorted).
            source: Source identifier.
            target: Target identifier.
            scope: Switchover scope (region/az/service).

        Returns:
            DRPhase for network layer.
        """
        steps = self._nodes_to_steps(nodes, source, target, base_order=1, scope=scope)
        total_secs = sum(s.estimated_time for s in steps)
        return DRPhase(
            phase_id="phase-3",
            name="Network / Traffic Layer Cutover",
            layer="L3",
            steps=steps,
            estimated_duration=max(1, total_secs // 60),
            gate_condition="End-user traffic routed to target; DNS propagated",
        )

    def _build_validation_phase(
        self,
        sorted_layers: Dict[str, List[str]],
        layers: Dict[str, List[Dict[str, Any]]],
    ) -> DRPhase:
        """Build Phase 4: Post-switchover validation.

        Args:
            sorted_layers: Layer name → sorted node names.
            layers: Layer name → node dicts.

        Returns:
            DRPhase for validation.
        """
        steps: List[DRStep] = [
            DRStep(
                step_id="validation-e2e",
                order=1,
                resource_type="Synthetic",
                resource_id="",
                resource_name="e2e-smoke-test",
                action="run_end_to_end_smoke_test",
                command=(
                    "# Run end-to-end smoke test against target endpoint\n"
                    "curl -sf https://petsite.example.com/health | jq '.status'"
                ),
                validation="curl -sf https://petsite.example.com/health | jq '.status'",
                expected_result="ok",
                rollback_command="# Initiate rollback plan if validation fails",
                estimated_time=120,
                requires_approval=False,
                tier=None,
                dependencies=[],
            ),
            DRStep(
                step_id="validation-monitoring",
                order=2,
                resource_type="CloudWatch",
                resource_id="",
                resource_name="alarms-check",
                action="verify_no_critical_alarms",
                command=(
                    "aws cloudwatch describe-alarms --state-value ALARM "
                    "--alarm-name-prefix petsite --output table"
                ),
                validation=(
                    "aws cloudwatch describe-alarms --state-value ALARM "
                    "--alarm-name-prefix petsite "
                    "--query 'length(MetricAlarms)' --output text"
                ),
                expected_result="0",
                rollback_command="# Investigate alarms before proceeding",
                estimated_time=60,
                requires_approval=False,
                tier=None,
                dependencies=["validation-e2e"],
            ),
        ]
        return DRPhase(
            phase_id="phase-4",
            name="Post-switchover Validation",
            layer="validation",
            steps=steps,
            estimated_duration=3,
            gate_condition="All smoke tests pass and no critical alarms firing",
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _nodes_to_steps(
        self,
        nodes: List[Dict[str, Any]],
        source: str,
        target: str,
        base_order: int = 1,
        scope: str = "region",
    ) -> List[DRStep]:
        """Convert a list of nodes to DRStep objects.

        For AZ-scope switchovers, regional/global services are
        automatically skipped by the step builder.

        Args:
            nodes: Node dicts.
            source: Source identifier.
            target: Target identifier.
            base_order: Starting order counter.
            scope: Switchover scope (region/az/service).

        Returns:
            List of DRStep objects (skipped nodes excluded).
        """
        steps: List[DRStep] = []
        order = base_order
        for node in nodes:
            step = self.step_builder.build_step(
                node, source, target, context={"order": order, "scope": scope}
            )
            if step is None:
                # Skipped (e.g. regional service during AZ switchover)
                continue
            step.order = order
            steps.append(step)
            order += 1
        return steps

    def _filter_excluded(
        self, subgraph: Dict[str, Any], exclude: List[str]
    ) -> Dict[str, Any]:
        """Remove excluded services from the subgraph.

        Args:
            subgraph: Original subgraph dict.
            exclude: List of service names to remove.

        Returns:
            Filtered subgraph dict.
        """
        exclude_set = set(exclude)
        filtered_nodes = [n for n in subgraph["nodes"] if n.get("name") not in exclude_set]
        filtered_names = {n["name"] for n in filtered_nodes}
        filtered_edges = [
            e for e in subgraph["edges"]
            if e.get("from") in filtered_names and e.get("to") in filtered_names
        ]
        return {"nodes": filtered_nodes, "edges": filtered_edges}

    def _estimate_rpo(self, data_layer_nodes: List[Dict[str, Any]]) -> int:
        """Estimate RPO in minutes based on data layer replication configs.

        Args:
            data_layer_nodes: L0 node dicts.

        Returns:
            Estimated RPO in minutes.
        """
        if not data_layer_nodes:
            return 0
        # Conservative estimate: 5 min default for RDS, 0 for DynamoDB Global Table
        max_rpo = 0
        for node in data_layer_nodes:
            rtype = node.get("type", "")
            if rtype in ("RDSCluster", "RDSInstance"):
                max_rpo = max(max_rpo, 5)
            elif rtype == "DynamoDBTable":
                max_rpo = max(max_rpo, 0)
            else:
                max_rpo = max(max_rpo, 15)
        return max_rpo
