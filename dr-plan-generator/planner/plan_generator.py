"""
planner/plan_generator.py — DR switchover plan generation engine

Orchestrates graph analysis, layer classification, topological sorting,
step building, and phase assembly into a complete DRPlan.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from dr_profile import get_active_profile as _p

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
        #: 最近一次范围解析所用的 resolver（供排序边过滤复用）。
        self._scope_resolver: Any = None
        #: phase-0 就绪检查因配置缺失而未能生成的项，汇入计划产物。
        self._preflight_gaps: List[Dict[str, str]] = []

    def generate_plan(
        self,
        scope: str,
        source: str,
        target: str,
        exclude: Optional[List[str]] = None,
        options: Optional[Dict[str, Any]] = None,
        snapshot: Optional[Dict[str, Any]] = None,
        mode: str = "drill",
    ) -> DRPlan:
        """Generate a complete DR switchover plan.

        Steps:
        1. Obtain the affected subgraph — from ``snapshot`` when given,
           otherwise by querying Neptune.
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
            snapshot: Optional pre-loaded graph snapshot (see ``graph.snapshot``).
                When supplied, Neptune is **not** contacted at all — this is the
                path that must work while the primary Region is down.
            mode: ``drill`` (planned, data-loss-free operations preferred) or
                ``failover`` (unplanned; the primary is already gone).

        Returns:
            Fully populated DRPlan.
        """
        from assessment.impact_analyzer import ImpactAnalyzer
        from assessment.rto_estimator import RTOEstimator

        # 每次生成都从干净状态开始：同一个 PlanGenerator 实例生成多份计划时
        # （测试与批量生成都会这么用），残留的缺口会串到下一份计划里。
        self._preflight_gaps = []

        if snapshot is not None:
            # Offline path: never touches Neptune. The primary Region may be gone.
            subgraph = self.analyzer.extract_affected_subgraph_from_data(
                snapshot.get("nodes", []), snapshot.get("edges", [])
            )
        else:
            subgraph = self.analyzer.extract_affected_subgraph(scope, source)

        # Whitelist scoping. Runs on BOTH paths on purpose: the offline path is
        # the disaster-time one, so scope trimming must apply there too — a
        # snapshot is a capture of the whole region, not of this workload.
        scoped = self._resolve_scope(subgraph)
        exclusion_table: List[Dict[str, str]] = []
        ordering_edges = subgraph["edges"]
        if scoped is not None:
            subgraph = scoped.as_subgraph()
            exclusion_table = scoped.exclusion_table()
            ordering_edges = self._scope_resolver.filter_ordering_edges(subgraph["edges"])
            logger.info(
                "Scope resolved: %d node(s) in scope, %d excluded "
                "(anchors matched %d, missing %d).",
                len(subgraph["nodes"]), len(exclusion_table),
                len(scoped.anchors_matched), len(scoped.anchors_missing),
            )

        # Legacy name-list exclusion stays available as a manual override on top
        # of anchoring (e.g. "skip petfood in this drill"), but it is no longer
        # how platform infrastructure is kept out — see graph/scope.py.
        if exclude:
            subgraph = self._filter_excluded(subgraph, exclude)
            kept = {n.get("name") for n in subgraph["nodes"]}
            ordering_edges = [
                e for e in ordering_edges
                if e.get("from") in kept and e.get("to") in kept
            ]

        layers = self.analyzer.classify_by_layer(subgraph)
        sorted_layers: Dict[str, List[str]] = {}
        for layer_name, nodes in layers.items():
            sorted_layers[layer_name] = self.analyzer.topological_sort_within_layer(
                nodes, ordering_edges
            )

        phases = self._build_phases(sorted_layers, layers, subgraph, source, target, options, scope=scope)

        impact = ImpactAnalyzer().assess_impact(
            subgraph, scope, source, offline=snapshot is not None
        )

        # RTO：按策略查表，并让演练实测值覆盖查表值（见 rto_estimator）。
        rto, rto_basis = RTOEstimator(
            strategy=getattr(self.step_builder, "strategy", "warm_standby")
        ).estimate_with_basis(phases)

        # RPO：按**实际复制拓扑**推导，无法推定时返回 None 而不是编一个数字。
        from assessment.rpo_estimator import RPOEstimator

        try:
            rpo_profile = _p()
        except Exception:  # noqa: BLE001
            rpo_profile = None
        rpo_assessment = RPOEstimator(
            profile=rpo_profile, mode=mode
        ).assess(subgraph["nodes"])
        rpo = rpo_assessment.minutes
        if rpo is None:
            logger.warning(
                "RPO cannot be derived from configuration (%s). The plan will say so "
                "rather than print a number that cannot be justified.",
                ", ".join(rpo_assessment.unmeasurable) or "unknown",
            )

        plan_id = f"dr-{scope}-{int(time.time())}"
        now = datetime.now(timezone.utc).isoformat()

        # Audit-relevant: record when the graph data was *captured*, not when the
        # plan was rendered. Offline plans are built from a snapshot that may be
        # hours old, and an auditor must be able to see that.
        if snapshot is not None:
            graph_time = snapshot.get("created_at") or now
            plan_source = "snapshot"
            snapshot_age = snapshot.get("age_seconds")
            snapshot_stale = bool(snapshot.get("stale", False))
        else:
            graph_time = now
            plan_source = "neptune"
            snapshot_age = None
            snapshot_stale = False

        # Strategy + data-layer feasibility come from the profile. Both are
        # tolerated as absent here: a profile without a `dr` section is still a
        # usable profile for AZ-level work, and M5 adds an explicit --strategy.
        # Strategy: whatever the StepBuilder was actually constructed with is the
        # authoritative value — the CLI can override the profile, and the plan
        # must record what was really used to generate these steps.
        strategy = getattr(self.step_builder, "strategy", "") or ""
        data_layer_gaps: List[Dict[str, str]] = []
        try:
            profile = _p()
            if not strategy:
                strategy = profile.dr_strategy
            data_layer_gaps = profile.strategy_feasibility()
        except Exception as exc:  # noqa: BLE001
            logger.info("Strategy/feasibility not available from profile: %s", exc)

        # 生成步骤时发现的数据层缺口（例如「该集群没有跨区 secondary」）与
        # profile 的静态可行性校验互补：前者是「实际生不出可执行步骤」，
        # 后者是「拓扑声明就不达标」。两者都要进产物。
        step_gaps = list(getattr(self.step_builder, "data_step_gaps", []) or [])
        seen = {(g.get("component"), g.get("actual")) for g in data_layer_gaps}
        for gap in step_gaps:
            if (gap.get("component"), gap.get("actual")) not in seen:
                data_layer_gaps.append(gap)

        if data_layer_gaps:
            logger.warning(
                "Declared strategy %r has %d unmet data-layer precondition(s); "
                "the effective recovery point is worse than the strategy implies.",
                strategy or "(unset)", len(data_layer_gaps),
            )

        return DRPlan(
            plan_id=plan_id,
            created_at=now,
            scope=scope,
            source=source,
            target=target,
            mode=mode,
            strategy=strategy,
            data_layer_gaps=data_layer_gaps,
            compute_layer_gaps=(
                list(getattr(self.step_builder, "compute_layer_gaps", []) or [])
                + list(self._preflight_gaps)
            ),
            scope_exclusions=exclusion_table,
            plan_source=plan_source,
            graph_snapshot_age_seconds=snapshot_age,
            graph_snapshot_stale=snapshot_stale,
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
            rpo_basis=rpo_assessment.basis,
            rpo_unmeasurable=rpo_assessment.unmeasurable,
            rpo_measurement_commands=rpo_assessment.measurement_commands,
            rto_basis=rto_basis,
            validation_status="pending",
            graph_snapshot_time=graph_time,
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
                validation=f"aws sts get-caller-identity --region {target} --query 'Account' --output text",
                expected_result="Account ID returned (not empty)",
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
                        validation=(
                            f"aws cloudwatch get-metric-statistics "
                            f"--namespace AWS/RDS --metric-name ReplicaLag "
                            f"--dimensions Name=DBClusterIdentifier,Value={node['name']} "
                            f"--start-time $(date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%S) "
                            f"--end-time $(date -u +%Y-%m-%dT%H:%M:%S) "
                            f"--period 60 --statistics Average --region {source} "
                            f"--query 'Datapoints[0].Average' --output text"
                        ),
                        expected_result="< 1000 (milliseconds)",
                        rollback_command="# No rollback needed for lag check",
                        estimated_time=15,
                        requires_approval=False,
                        tier=node.get("tier"),
                        dependencies=[],
                    )
                )
                order += 1

        # Step 0.3: Lower DNS TTL
        # DNS TTL 降低。命令由 planner/dns_commands 构造：原实现的 change-batch
        # 缺 Name/Type/ResourceRecords 三个必填字段（Route 53 会以
        # InvalidChangeBatch 整体拒绝），且 --hosted-zone-id $ZONE_ID 是未绑定的
        # shell 变量，展开成空串后参数解析错位。
        from planner.dns_commands import build_failover_change, build_ttl_change

        try:
            dns_profile = _p()
        except Exception:  # noqa: BLE001
            dns_profile = None

        ttl_low = build_ttl_change(
            dns_profile,
            dns_profile.dns_ttl_pre_switchover if dns_profile else 60,
        )
        ttl_restore = build_ttl_change(
            dns_profile,
            dns_profile.dns_ttl_normal if dns_profile else 300,
        )
        if ttl_low.get("zone_configured") == "no":
            self._preflight_gaps.append({
                "component": "route53",
                "requirement": "profile 的 dns.hosted_zone_id（或 ROUTE53_ZONE_ID 环境变量）",
                "actual": "未配置",
                "implication": (
                    "DNS 步骤中的 hosted zone id 是占位符，执行前必须替换。"
                    "改 DNS 是切换里最难纠正的一步——TTL 缓存意味着改错要等缓存过期。"
                ),
            })

        steps.append(
            DRStep(
                step_id="preflight-dns-ttl",
                order=order,
                resource_type="Route53",
                resource_id="",
                resource_name=ttl_low.get("record_name", "dns-ttl"),
                action="lower_dns_ttl",
                command=ttl_low["command"],
                validation=ttl_low["validation"],
                expected_result=ttl_low["expected"],
                rollback_command=ttl_restore["command"],
                estimated_time=30,
                requires_approval=False,
                tier=None,
                dependencies=[],
            )
        )

        # Readiness checks (M7). These come last in phase-0 on purpose: the cheap
        # connectivity/lag probes above should fail fast, and only then is it worth
        # spending time on the fuller readiness sweep.
        #
        # These are the checks whose absence produces failures **mid-switchover**
        # with symptoms far from the cause — missing images, unavailable instance
        # types, quota walls, config pointing at the wrong Region, unusable KMS
        # keys. ARC's readiness check used to cover much of this, but it closed to
        # new customers on 2026-04-30, so it has to be built here.
        from planner.preflight import PreflightBuilder

        all_nodes: List[Dict[str, Any]] = []
        for layer_nodes in layers.values():
            all_nodes.extend(layer_nodes)

        try:
            profile = _p()
        except Exception:  # noqa: BLE001
            profile = None

        preflight = PreflightBuilder(
            strategy=getattr(self.step_builder, "strategy", "warm_standby"),
            mode=getattr(self.step_builder, "mode", "drill"),
            profile=profile,
        )
        # start_order 由**实际步数**推导，不用上面那个手工维护的 order 计数器：
        # 原代码在 DNS TTL 步骤后忘了递增，直接沿用会让 order 撞号
        # （实测 lower_dns_ttl 与第一个 ECR 检查都拿到 3）。
        readiness = preflight.build(
            source, target, all_nodes, start_order=len(steps) + 1
        )
        steps.extend(readiness)
        # 追加而非赋值：`_resolve_scope` 可能已经记过缺口（例如锚点全不命中的
        # 空范围回退），直接赋值会把它冲掉。
        self._preflight_gaps.extend(preflight.gaps)

        total_secs = sum(s.estimated_time for s in steps)
        return DRPhase(
            phase_id="phase-0",
            name="Pre-flight Check",
            layer="preflight",
            steps=steps,
            estimated_duration=max(1, total_secs // 60),
            gate_condition=(
                "All preflight checks passed: replication in sync, images present "
                "in target, instance types and quota available, KMS keys usable, "
                "and target app config verified to point at the recovery Region"
            ),
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

        pilot light 下先插入节点组扩容与**等节点 Ready**，再做 Deployment 扩容。
        顺序不可颠倒：节点组 desiredSize=0 时 ``kubectl scale`` 会「成功」而
        Pod 永久 Pending，后续 rollout 一直等到超时，运维却看到扩容命令成功了。

        Args:
            nodes: L1 + L2 compute nodes (sorted).
            source: Source identifier.
            target: Target identifier.
            scope: Switchover scope (region/az/service).

        Returns:
            DRPhase for compute layer.
        """
        prereq = self.step_builder.build_nodegroup_steps(target)
        steps = self._nodes_to_steps(
            nodes, source, target, base_order=len(prereq) + 1, scope=scope
        )
        steps = prereq + steps
        total_secs = sum(s.estimated_time for s in steps)
        strategy = getattr(self.step_builder, "strategy", "")
        gate = "All Tier0 services healthy in target"
        if prereq:
            gate = (
                "Node capacity Ready in target AND all Tier0 services healthy "
                "(Pods must not be left Pending)"
            )
        return DRPhase(
            phase_id="phase-2",
            name=(
                "Compute Layer Activation"
                + (f" ({strategy})" if strategy else "")
            ),
            layer="L2",
            steps=steps,
            estimated_duration=max(1, total_secs // 60),
            gate_condition=gate,
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
                    f"curl -sf https://{_p().domain}{_p().health_endpoint} | jq '.status'"
                ),
                validation=f"curl -sf https://{_p().domain}{_p().health_endpoint} | jq '.status'",
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
                    f"--alarm-name-prefix {_p().alarm_prefix} --output table"
                ),
                validation=(
                    "aws cloudwatch describe-alarms --state-value ALARM "
                    f"--alarm-name-prefix {_p().alarm_prefix} "
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

    def _resolve_scope(self, subgraph: Dict[str, Any]) -> Any:
        """按 profile 锚点裁剪范围；profile 无 services 声明时跳过。

        跳过而不是报错：AZ 级演练、或用最小 profile 做单点排查时，
        锚点集合可能是空的，此时不裁剪比裁成空集有用。

        Args:
            subgraph: 含 ``nodes`` / ``edges``。

        Returns:
            ScopedSubgraph，或 None（未裁剪）。
        """
        from graph.scope import ScopeResolver

        try:
            profile = _p()
        except Exception as exc:  # noqa: BLE001
            logger.info("No profile available; skipping scope anchoring: %s", exc)
            return None

        try:
            from registry.policy_loader import PlanPolicy

            policy = PlanPolicy()
        except Exception:  # noqa: BLE001
            policy = None

        resolver = ScopeResolver.from_profile(profile, policy)
        if not resolver.anchors:
            logger.info(
                "Profile declares no services; skipping scope anchoring "
                "(the whole subgraph is treated as in scope)."
            )
            return None

        self._scope_resolver = resolver
        scoped = resolver.resolve(subgraph["nodes"], subgraph["edges"])
        if not scoped.nodes:
            logger.warning(
                "Scope anchoring produced an EMPTY scope; falling back to the "
                "unfiltered subgraph. Check that profile service names match "
                "graph node names (anchors missing: %s).",
                ", ".join(scoped.anchors_missing[:8]) or "none",
            )
            # 只记 WARNING 不够：回退后的计划**没有做过任何范围裁剪**，
            # 平台自身设施与污染节点都会留在里面，而计划看起来是完整的。
            # 这必须出现在产物里，否则运维不会知道自己拿到的是未裁剪的全图。
            self._preflight_gaps.append({
                "component": "scope_anchoring",
                "requirement": "profile 的 services 名字需与图谱节点名对得上",
                "actual": (
                    f"锚点全部未命中（缺失：{', '.join(scoped.anchors_missing[:6]) or '未知'}）"
                ),
                "implication": (
                    "已回退为**未裁剪的全图**：平台自身设施（Neptune、etl_*）与"
                    "污染节点可能留在计划里。执行前必须核对 profile 的服务名"
                    "与图谱节点名是否一致。"
                ),
            })
            return None
        return scoped

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

