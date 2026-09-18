"""
planner/step_builder.py — Per-resource-type DR step command builder

Each builder method returns a fully populated DRStep with command,
validation, expected_result, and rollback_command.
"""

import logging
from typing import Any, Dict, List, Optional

from dr_profile import get_active_profile as _p

from models import DRStep

logger = logging.getLogger(__name__)

#: pilot light 与 warm standby 的分界（AWS Well-Architected REL13-BP02 原文）：
#:   "pilot light cannot process requests without additional action taken first,
#:    while warm standby can handle traffic (at reduced capacity levels)
#:    immediately. Pilot light will require you to turn on servers, possibly
#:    deploy additional (non-core) infrastructure, and scale up, while warm
#:    standby only requires you to scale up (everything is already deployed
#:    and running)."
#:
#: 落到 EKS 上，本工具采用的 pilot light 模型是：
#:   **清单已 apply，但 Deployment replicas=0 且节点组 desiredSize=0。**
#: 这是 GitOps 友好且可用 kubectl 表达的解读。另一种解读（清单完全没 apply）
#: 需要工具持有 manifest，超出图谱能提供的信息范围。
#: 该假设会作为 phase-0 前置校验项（M7）显式确认，而不是默默依赖。
STRATEGY_PILOT_LIGHT = "pilot_light"
STRATEGY_WARM_STANDBY = "warm_standby"

#: 演练（计划内）与真灾（非计划）。数据层动作按此分流——这不是措辞差别，
#: 而是**调不同 API**：
#:   drill    → aws rds switchover-global-cluster   （零数据丢失，要求 global cluster 健康）
#:   failover → aws rds failover-global-cluster --allow-data-loss （RPO 秒级，可在主区已失时用）
#: ⚠️ 陷阱：failover-global-cluster **不带** --allow-data-loss 时会默认降级为
#: switchover（AWS 文档原文："If you don't specify AllowDataLoss, the global
#: database cluster operation defaults to a switchover"），而 switchover 要求
#: 主区健康——真灾时那条命令会失败。所以 failover 模式必须显式带该 flag。
MODE_DRILL = "drill"
MODE_FAILOVER = "failover"

#: 节点从扩容到 Ready 的等待上限（秒）。EC2 节点加入 + kubelet 就绪实测
#: 约 180–300s，留一倍余量；宁可等待也不能让后续步骤在 Pod Pending 上空转。
NODE_READY_TIMEOUT_SECONDS = 600

#: Aurora 拓扑中「跨区提升」可行的取值。none / snapshot_copy 不能用
#: global-cluster 那套 API——前者压根没有 secondary，后者要走快照恢复。
_AURORA_PROMOTABLE = frozenset({"global_database", "cross_region_replica"})



class StepBuilder:
    """Build DRStep objects for each supported AWS resource type.

    For unsupported types, a generic placeholder step is generated.

    Args:
        strategy: ``pilot_light`` or ``warm_standby``. Determines whether the
            compute layer needs node capacity brought up first.
        mode: ``drill`` (planned) or ``failover`` (unplanned). Determines which
            data-layer API is emitted — see MODE_* constants.
    """

    def __init__(
        self,
        strategy: str = STRATEGY_WARM_STANDBY,
        mode: str = MODE_DRILL,
    ) -> None:
        self.strategy = strategy
        self.mode = mode
        #: 生成过程中发现的计算层前提缺口，由 PlanGenerator 收进计划产物。
        #: 只记日志是不够的——运维拿到的仍是一份看起来完整的计划。
        self.compute_layer_gaps: List[Dict[str, str]] = []
        #: 同理，数据层在**生成步骤时**发现的缺口（与 profile 的静态可行性
        #: 校验互补：这里记的是「这个资源实际生不出可执行步骤」）。
        self.data_step_gaps: List[Dict[str, str]] = []

    # ------------------------------------------------------------------
    # EKS helpers — resolved from the profile, never string-concatenated
    # ------------------------------------------------------------------

    def _kubectl_target(self) -> str:
        """返回指向恢复区集群的 kubectl 参数（context + namespace）。

        原实现是 ``--context {target}-cluster`` ——把 region 名拼上 "-cluster"
        当作 kube context。真实 context 名由使用者的 kubeconfig 决定，拼出来的
        几乎必然不存在，而 kubectl 对不存在的 context 是**直接报错退出**，
        整条恢复链就断在这里。改从 profile 的 ``kubernetes.context_target`` 读。
        """
        try:
            profile = _p()
            context = profile.get("kubernetes.context_target", "") or ""
            namespace = profile.k8s_namespace
        except Exception:  # noqa: BLE001
            context, namespace = "", "default"
        parts = []
        if context:
            parts.append(f"--context {context}")
        if namespace:
            parts.append(f"-n {namespace}")
        return " ".join(parts)

    def _deployment_name(self, service_name: str) -> str:
        """把图谱服务名翻译成 K8s Deployment 名。

        必须走 profile：图谱名与 Deployment 名常常不同（例如 profile 里
        ``petsearch`` 的 ``k8s_deployment`` 是 ``search-service``）。
        直接拿图谱名去 ``kubectl scale`` 会命中一个不存在的 Deployment。
        """
        try:
            return _p().get_deployment_name(service_name)
        except Exception:  # noqa: BLE001
            return service_name

    def _target_replicas(self, tier: Optional[str]) -> int:
        """恢复区目标副本数，按 tier 从 profile 读，缺省保守取 2。"""
        try:
            profile = _p()
            per_tier = profile.get("dr.eks.replicas_by_tier", {}) or {}
            if tier and tier in per_tier:
                return int(per_tier[tier])
            return int(profile.get("dr.eks.default_replicas", 2))
        except Exception:  # noqa: BLE001
            return 2

    def build_nodegroup_steps(self, target: str) -> List[DRStep]:
        """pilot light 专属：先把节点容量拉起来，并**等节点 Ready**。

        为什么这两步不能省、且顺序不能反
        --------------------------------
        pilot light 下节点组常态 ``desiredSize=0``。此时直接
        ``kubectl scale deployment`` 不会报错——它会成功，然后 Pod 永远
        ``Pending``（没有可调度节点）。后续 ``rollout status`` 会一直等到超时，
        而运维看到的是「扩容命令执行成功」。这是 pilot light 在 EKS 上最常见
        的失败模式，也是原实现完全缺失的一环。

        因此每个节点组产出两步：扩容，然后**阻塞等待节点 Ready**。

        warm standby 不需要这些：节点容量常态就在（AWS 定义里
        "everything is already deployed and running"），只需 scale 副本。

        Args:
            target: 恢复区 region。

        Returns:
            DRStep 列表；warm standby 下为空。
        """
        if self.strategy != STRATEGY_PILOT_LIGHT:
            return []

        try:
            profile = _p()
            cluster = profile.dr_eks_target_cluster
            nodegroups = profile.dr_eks_nodegroups
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cannot read dr.eks from profile: %s", exc)
            return []

        if not nodegroups:
            logger.warning(
                "strategy=pilot_light but dr.eks.nodegroups is empty. No node "
                "capacity step will be generated, and scaling Deployments will "
                "leave Pods Pending forever if the nodegroups really are at 0."
            )
            self.compute_layer_gaps.append({
                "component": "eks_nodegroups",
                "requirement": "dr.eks.nodegroups 需列出恢复区待扩容的节点组",
                "actual": "未配置（空列表）",
                "implication": (
                    "pilot light 下节点组常态 desiredSize=0。缺少扩容与等待步骤时，"
                    "kubectl scale 会返回成功而 Pod 永久 Pending，rollout 一直等到超时——"
                    "运维看到的却是「扩容执行成功」。执行本计划前必须补齐此配置。"
                ),
            })
            return []

        steps: List[DRStep] = []
        for index, group in enumerate(nodegroups, start=1):
            name = str(group.get("name", ""))
            desired = int(group.get("desired", 2))
            minimum = int(group.get("min", desired))
            maximum = int(group.get("max", max(desired, minimum)))

            steps.append(DRStep(
                step_id=f"ng-scale-{name}",
                order=index * 2 - 1,
                parallel_group="pg-nodegroup-scale",
                resource_type="EKSNodeGroup",
                resource_name=name,
                action="scale_nodegroup_up",
                command=(
                    f"aws eks update-nodegroup-config --cluster-name {cluster} "
                    f"--nodegroup-name {name} --scaling-config "
                    f"minSize={minimum},maxSize={maximum},desiredSize={desired} "
                    f"--region {target}"
                ),
                validation=(
                    f"aws eks describe-nodegroup --cluster-name {cluster} "
                    f"--nodegroup-name {name} --region {target} "
                    f"--query 'nodegroup.scalingConfig.desiredSize' --output text"
                ),
                expected_result=str(desired),
                rollback_command=(
                    f"aws eks update-nodegroup-config --cluster-name {cluster} "
                    f"--nodegroup-name {name} --scaling-config "
                    f"minSize=0,maxSize={maximum},desiredSize=0 --region {target}"
                ),
                estimated_time=60,
                requires_approval=False,
                tier="Tier0",
            ))

            # 节点是**集群级**资源，命令里不能带 -n <namespace>：
            # `kubectl wait node -n foo` 会因命名空间与集群级资源不匹配而失败。
            ctx_arg = f" {self._context_only()}" if self._context_only() else ""
            steps.append(DRStep(
                step_id=f"ng-wait-{name}",
                order=index * 2,
                resource_type="EKSNodeGroup",
                resource_name=name,
                action="wait_nodes_ready",
                command=(
                    f"kubectl wait --for=condition=Ready node "
                    f"-l eks.amazonaws.com/nodegroup={name} "
                    f"--timeout={NODE_READY_TIMEOUT_SECONDS}s{ctx_arg}"
                ),
                validation=(
                    f"kubectl get nodes -l eks.amazonaws.com/nodegroup={name} "
                    f"--no-headers{ctx_arg} | grep -c ' Ready '"
                ),
                expected_result=f">= {desired}",
                rollback_command="# No rollback: waiting for nodes has no side effect",
                estimated_time=NODE_READY_TIMEOUT_SECONDS // 2,
                requires_approval=False,
                tier="Tier0",
                dependencies=[f"ng-scale-{name}"],
            ))

        return steps

    def _context_only(self) -> str:
        """只返回 ``--context …``。节点是集群级资源，不能带 ``-n``。"""
        try:
            context = _p().get("kubernetes.context_target", "") or ""
        except Exception:  # noqa: BLE001
            context = ""
        return f"--context {context}" if context else ""

    def _namespace_or_empty(self) -> str:
        """当前命名空间名（供诊断与测试断言用）。"""
        try:
            return _p().k8s_namespace
        except Exception:  # noqa: BLE001
            return ""

    def build_step(
        self,
        node: Dict[str, Any],
        source: str,
        target: str,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[DRStep]:
        """Dispatch to the correct per-type builder based on node type.

        For AZ-scope switchovers, regional/global services are skipped
        because they are inherently multi-AZ and unaffected by a single
        AZ failure (per AWS Fault Isolation Boundaries whitepaper).

        Args:
            node: Node dict with at least ``name`` and ``type`` keys.
            source: Source region/AZ being failed over from.
            target: Target region/AZ being failed over to.
            context: Optional extra context (e.g. step order counter).

        Returns:
            Populated DRStep, or ``None`` if the resource should be
            skipped for this switchover scope.
        """
        ctx = context or {}
        scope = ctx.get("scope", "region")
        resource_type_raw = node.get("type", "")

        # --- AZ switchover: skip regional/global services ---
        # Regional services (DynamoDB, SQS, S3, Lambda, etc.) are
        # inherently multi-AZ; they don't need AZ-level failover.
        if scope == "az":
            from registry import registry_loader
            reg = registry_loader.get_registry()
            fault_domain = reg.get_fault_domain(resource_type_raw)
            if fault_domain in ("regional", "global"):
                logger.info(
                    "Skipping %s '%s' for AZ switchover — %s service "
                    "(multi-AZ, no AZ-level failover needed).",
                    resource_type_raw, node.get("name", ""), fault_domain,
                )
                return None

        resource_type = resource_type_raw.lower()
        builder = getattr(self, f"_build_{resource_type}_step", None)
        if builder:
            return builder(node, source, target, ctx)
        logger.warning(
            "No dedicated step builder for resource type %r — falling back to generic step. "
            "Consider adding a _build_%s_step method or updating registry/custom_types.yaml.",
            resource_type_raw,
            resource_type,
        )
        return self._build_generic_step(node, source, target)

    # ------------------------------------------------------------------
    # Per-type builders
    # ------------------------------------------------------------------

    def _build_rdscluster_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """Aurora 跨区提升步骤，按 ``mode`` 与拓扑分流。

        **原实现是错的**（这是 M6 的必修项）::

            aws rds failover-db-cluster --db-cluster-identifier X --region <target>

        ``failover-db-cluster`` 是**集群内**把 reader 提升为 writer（AZ 级）。
        它不认识跨区拓扑：拿它配一个目标区的 identifier，要么因该区没有同名集群
        而报错，要么在目标区做了一次无意义的 AZ 内切换——两种都不是跨区容灾。
        而步骤名还叫 ``promote_read_replica``，名实不符。

        正确的 API（已核对 AWS CLI 参考）::

            drill    → aws rds switchover-global-cluster
                         --global-cluster-identifier <gc>
                         --target-db-cluster-identifier <secondary ARN>
            failover → aws rds failover-global-cluster
                         --global-cluster-identifier <gc>
                         --target-db-cluster-identifier <secondary ARN>
                         --allow-data-loss

        两个要点：
        1. ``--target-db-cluster-identifier`` **必须用 ARN**。文档原文：
           "Use the Amazon Resource Name (ARN) ... so that Aurora can locate the
           cluster in its Amazon Web Services Region." 裸 identifier 定位不到跨区集群。
        2. ``failover-global-cluster`` **不带** ``--allow-data-loss`` 会默认降级成
           switchover，而 switchover 要求 global cluster 健康——真灾时主区已不可达，
           那条命令会失败。所以 failover 模式必须显式带该 flag，且它与
           ``--switchover`` 互斥。

        Args:
            node: Node dict.
            source: Source region.
            target: Target region.
            ctx: Context dict.

        Returns:
            DRStep；拓扑不支持跨区提升时返回一个「阻塞并说明原因」的步骤。
        """
        cluster_id = node["name"]
        tier = node.get("tier")
        topology, global_cluster = self._aurora_topology()

        if topology not in _AURORA_PROMOTABLE:
            return self._aurora_unavailable_step(node, source, target, ctx, topology)

        # secondary 集群的 ARN。profile 未提供时给出可辨识的占位并记缺口——
        # 绝不拼一个看起来像 ARN 的字符串让人直接执行。
        target_arn = self._aurora_target_arn(cluster_id, target)

        if self.mode == MODE_FAILOVER:
            action = "failover_global_cluster"
            command = (
                f"aws rds failover-global-cluster "
                f"--global-cluster-identifier {global_cluster} "
                f"--target-db-cluster-identifier {target_arn} "
                f"--allow-data-loss --region {target}"
            )
            note = (
                "# 非计划切换：--allow-data-loss 必须显式给出。省略它会被 AWS "
                "默认当作 switchover，而 switchover 要求主区健康，真灾时会失败。\n"
            )
        else:
            action = "switchover_global_cluster"
            command = (
                f"aws rds switchover-global-cluster "
                f"--global-cluster-identifier {global_cluster} "
                f"--target-db-cluster-identifier {target_arn} "
                f"--region {target}"
            )
            note = (
                "# 计划内切换：零数据丢失，且保持原有复制拓扑（原主降为 secondary）。\n"
                "# 要求 global cluster 健康——主区不可达时改用 --mode failover。\n"
            )

        return DRStep(
            step_id=f"rds-{cluster_id}",
            order=ctx.get("order", 0),
            resource_type="RDSCluster",
            resource_id=node.get("id", ""),
            resource_name=cluster_id,
            action=action,
            command=note + command,
            validation=(
                f"aws rds describe-global-clusters "
                f"--global-cluster-identifier {global_cluster} "
                f"--region {target} "
                f"--query \"GlobalClusters[0].GlobalClusterMembers"
                f"[?IsWriter==\\`true\\`].DBClusterArn\" --output text"
            ),
            expected_result=f"writer ARN 位于 {target}",
            rollback_command=(
                "# 回切用 switchover（零丢失），不要用 failover：\n"
                f"aws rds switchover-global-cluster "
                f"--global-cluster-identifier {global_cluster} "
                f"--target-db-cluster-identifier <{source} 集群的 ARN> "
                f"--region {source}"
            ),
            estimated_time=300,
            requires_approval=True,
            tier=tier,
            dependencies=[],
        )

    def _aurora_topology(self) -> tuple:
        """返回 ``(topology, global_cluster_identifier)``。"""
        try:
            profile = _p()
            return (
                profile.dr_topology("aurora"),
                profile.get("dr.data.aurora.global_cluster_identifier", "") or "",
            )
        except Exception:  # noqa: BLE001
            return "none", ""

    def _aurora_target_arn(self, cluster_id: str, target: str) -> str:
        """恢复区 secondary 集群的 ARN。缺失时给显式占位并记缺口。"""
        try:
            arn = _p().get("dr.data.aurora.target_cluster_arn", "") or ""
        except Exception:  # noqa: BLE001
            arn = ""
        if arn:
            return arn
        self.data_step_gaps.append({
            "component": "aurora",
            "requirement": "dr.data.aurora.target_cluster_arn（恢复区 secondary 集群 ARN）",
            "actual": "未配置",
            "implication": (
                "跨区提升要求 --target-db-cluster-identifier 是 ARN，裸 identifier "
                "定位不到其它 Region 的集群。此步骤中的占位符必须在执行前替换为真实 ARN。"
            ),
        })
        return f"<arn:aws:rds:{target}:<account>:cluster:{cluster_id}>"

    def _aurora_unavailable_step(
        self,
        node: Dict[str, Any],
        source: str,
        target: str,
        ctx: Dict[str, Any],
        topology: str,
    ) -> DRStep:
        """拓扑不支持跨区提升时，产出一个**会失败**的阻塞步骤而非静默跳过。

        静默跳过会让计划看起来完整而数据层其实没切；生成一个
        ``exit 1`` 的步骤，能保证演练时立刻暴露，而不是等到真灾。
        """
        cluster_id = node["name"]
        self.data_step_gaps.append({
            "component": "aurora",
            "requirement": "Aurora Global Database 或跨区只读副本",
            "actual": topology,
            "implication": (
                f"集群 {cluster_id} 没有跨区 secondary，无法提升。恢复只能走"
                "「跨区快照恢复」——RTO 从分钟级退化到小时级，且需要单独的恢复流程。"
            ),
        })
        return DRStep(
            step_id=f"rds-{cluster_id}",
            order=ctx.get("order", 0),
            resource_type="RDSCluster",
            resource_id=node.get("id", ""),
            resource_name=cluster_id,
            action="blocked_no_cross_region_replica",
            command=(
                f"# ✗ 无法生成 {cluster_id} 的跨区提升步骤：aurora topology = {topology}\n"
                f"# 该集群在 {target} 没有 secondary，failover/switchover 都不适用。\n"
                f"# 可行路径只有跨区快照恢复（RTO 小时级），需另行编排：\n"
                f"#   aws rds describe-db-cluster-snapshots --db-cluster-identifier {cluster_id} "
                f"--region {source}\n"
                f"#   aws rds copy-db-cluster-snapshot ... --region {target}\n"
                f"#   aws rds restore-db-cluster-from-snapshot ... --region {target}\n"
                f"echo 'BLOCKED: no cross-region replica for {cluster_id}' >&2 && exit 1"
            ),
            validation=f"echo 'not applicable — {cluster_id} has no cross-region secondary' && exit 1",
            expected_result="（无法满足：需先建立跨区复制）",
            rollback_command="# 无需回滚：本步骤不会产生变更",
            estimated_time=0,
            requires_approval=True,
            tier=node.get("tier"),
            dependencies=[],
        )

    def _build_rdsinstance_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """RDS instance reboot/failover step.

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.
            ctx: Context dict.

        Returns:
            DRStep for RDS instance failover.
        """
        instance_id = node["name"]
        return DRStep(
            step_id=f"rdsinstance-{instance_id}",
            order=ctx.get("order", 0),
            resource_type="RDSInstance",
            resource_id=node.get("id", ""),
            resource_name=instance_id,
            action="reboot_with_failover",
            command=(
                f"aws rds reboot-db-instance "
                f"--db-instance-identifier {instance_id} "
                f"--force-failover --region {target}"
            ),
            validation=(
                f"aws rds describe-db-instances "
                f"--db-instance-identifier {instance_id} "
                f"--region {target} "
                f"--query 'DBInstances[0].DBInstanceStatus' --output text"
            ),
            expected_result="available",
            rollback_command=(
                f"aws rds reboot-db-instance "
                f"--db-instance-identifier {instance_id} "
                f"--force-failover --region {source}"
            ),
            estimated_time=600,
            requires_approval=True,
            tier=node.get("tier"),
            dependencies=[],
        )

    def _build_dynamodbtable_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """DynamoDB 跨区步骤：降级为**校验**，不再改应用配置。

        **原实现把应用契约当成了命令**::

            aws ssm put-parameter --name '/petsite/dynamodb-region' --value '<target>'

        它假设应用会从这个 SSM 参数读区域——而图谱不可能知道这件事。参数名对了
        但应用不读它，命令照样成功、切换却没生效；参数名写错同样静默通过。

        Global Tables 的正确用法是**应用连本区端点**：DR 区的 Deployment 部署时
        就该指向本区，切换时数据层不需要任何变更动作。所以这一步只校验目标区
        副本可用。

        Args:
            node: Node dict.
            source: Source region.
            target: Target region.
            ctx: Context dict.

        Returns:
            DRStep（校验型；拓扑不支持时为阻塞步骤）。
        """
        table_name = node["name"]
        tier = node.get("tier")
        try:
            topology = _p().dr_topology("dynamodb")
        except Exception:  # noqa: BLE001
            topology = "none"

        if topology != "global_tables":
            self.data_step_gaps.append({
                "component": "dynamodb",
                "requirement": "DynamoDB Global Tables",
                "actual": topology,
                "implication": (
                    f"表 {table_name} 在 {target} 没有副本。恢复须从备份还原"
                    "（PITR 或 on-demand backup），RPO/RTO 均退化，且要单独编排。"
                ),
            })
            return DRStep(
                step_id=f"ddb-{table_name}",
                order=ctx.get("order", 0),
                resource_type="DynamoDBTable",
                resource_id=node.get("id", ""),
                resource_name=table_name,
                action="blocked_no_global_table",
                command=(
                    f"# ✗ {table_name} 不是 Global Table（topology={topology}），"
                    f"{target} 无副本。\n"
                    f"# 恢复须走备份还原，需另行编排：\n"
                    f"#   aws dynamodb describe-continuous-backups "
                    f"--table-name {table_name} --region {source}\n"
                    f"echo 'BLOCKED: {table_name} has no replica in {target}' >&2 && exit 1"
                ),
                validation=(
                    f"echo 'not applicable — {table_name} is not a Global Table' && exit 1"
                ),
                expected_result="（无法满足：需先启用 Global Tables）",
                rollback_command="# 无需回滚：本步骤不产生变更",
                estimated_time=0,
                requires_approval=True,
                tier=tier,
                dependencies=[],
            )

        return DRStep(
            step_id=f"ddb-{table_name}",
            order=ctx.get("order", 0),
            resource_type="DynamoDBTable",
            resource_id=node.get("id", ""),
            resource_name=table_name,
            action="verify_global_table_replica",
            command=(
                f"# Global Table 无需切换动作：应用连本区端点即可。\n"
                f"# DR 区 Deployment 在部署时就该指向 {target}，此处只校验副本可用。\n"
                f"aws dynamodb describe-table --table-name {table_name} "
                f"--region {target} --query 'Table.TableStatus' --output text"
            ),
            validation=(
                f"aws dynamodb describe-table "
                f"--table-name {table_name} --region {target} "
                f"--query 'Table.TableStatus' --output text"
            ),
            expected_result="ACTIVE",
            rollback_command="# 无需回滚：校验步骤无副作用",
            estimated_time=30,
            requires_approval=False,
            tier=tier,
            dependencies=[],
        )

    def _build_s3bucket_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """S3 跨区复制校验步骤。

        S3 CRR 是异步的，未开启 RTC（Replication Time Control）时**没有 SLA**。
        切换前必须确认「待复制对象数已归零」，否则目标区数据是残缺的——
        而这不会报错，应用只是读不到部分对象（表现为「图片全丢」这类难定位故障）。

        Args:
            node: Node dict.
            source: Source region.
            target: Target region.
            ctx: Context dict.

        Returns:
            DRStep。
        """
        bucket = node["name"]
        try:
            topology = _p().dr_topology("s3")
        except Exception:  # noqa: BLE001
            topology = "none"
        replicated = topology in ("crr", "crr_rtc")

        if not replicated:
            self.data_step_gaps.append({
                "component": "s3",
                "requirement": "S3 跨区复制（CRR）",
                "actual": topology,
                "implication": (
                    f"桶 {bucket} 未配置跨区复制，{target} 没有对象副本。"
                    "应用读取会静默拿到 404 而非报错。"
                ),
            })

        return DRStep(
            step_id=f"s3-{bucket}",
            order=ctx.get("order", 0),
            resource_type="S3Bucket",
            resource_id=node.get("id", ""),
            resource_name=bucket,
            action=("verify_crr_caught_up" if replicated else "blocked_no_crr"),
            command=(
                (
                    f"# 确认待复制对象已归零。CRR 是异步的，未开 RTC 时无 SLA。\n"
                    f"aws cloudwatch get-metric-statistics --namespace AWS/S3 "
                    f"--metric-name OperationsPendingReplication "
                    f"--dimensions Name=SourceBucket,Value={bucket} "
                    f"--start-time $(date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%S) "
                    f"--end-time $(date -u +%Y-%m-%dT%H:%M:%S) --period 60 "
                    f"--statistics Maximum --region {source} "
                    f"--query 'Datapoints[-1].Maximum' --output text"
                ) if replicated else (
                    f"# ✗ {bucket} 未配置跨区复制（topology={topology}），{target} 无副本。\n"
                    f"echo 'BLOCKED: {bucket} has no cross-region replication' >&2 && exit 1"
                )
            ),
            validation=(
                f"aws s3api get-bucket-replication --bucket {bucket} "
                f"--query 'ReplicationConfiguration.Rules[0].Status' --output text"
                if replicated
                else f"echo 'not applicable — {bucket} has no CRR' && exit 1"
            ),
            expected_result=("0（无待复制对象）" if replicated
                             else "（无法满足：需先配置 CRR）"),
            rollback_command="# 无需回滚：校验步骤无副作用",
            estimated_time=(60 if replicated else 0),
            requires_approval=False,
            tier=node.get("tier"),
            dependencies=[],
        )

    def _build_sqsqueue_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """SQS 队列步骤：**量化数据丢失**，而不是假装能切过去。

        SQS 没有跨区复制能力——结构性事实，不是配置疏漏。切换瞬间源队列里
        visible + in-flight 的消息就是丢失的业务数据。

        对领养/支付这类流程，这意味着「已提交但未处理的事件会丢」，属于业务方
        必须知情并签字的事项，不能藏在技术细节里。所以这一步抓取消息数作为
        丢失量证据，并确认目标区队列存在。

        Args:
            node: Node dict.
            source: Source region.
            target: Target region.
            ctx: Context dict.

        Returns:
            DRStep。
        """
        queue = node["name"]
        return DRStep(
            step_id=f"sqs-{queue}",
            order=ctx.get("order", 0),
            resource_type="SQSQueue",
            resource_id=node.get("id", ""),
            resource_name=queue,
            action="quantify_message_loss",
            command=(
                f"# ⚠️ SQS 无跨区复制能力。以下数字就是本次切换的**业务数据丢失量**，\n"
                f"#    必须记入演练/事故报告并由业务方确认。\n"
                f"QURL=$(aws sqs get-queue-url --queue-name {queue} "
                f"--region {source} --query QueueUrl --output text)\n"
                f"aws sqs get-queue-attributes --queue-url \"$QURL\" --region {source} "
                f"--attribute-names ApproximateNumberOfMessages "
                f"ApproximateNumberOfMessagesNotVisible --query 'Attributes' --output json"
            ),
            validation=(
                f"aws sqs get-queue-url --queue-name {queue} --region {target} "
                f"--query QueueUrl --output text"
            ),
            expected_result=f"{target} 中存在同名队列（消息不会被复制过去）",
            rollback_command=(
                "# 无法回滚：已丢失的消息不可恢复，回切后需业务侧补偿。"
            ),
            estimated_time=30,
            requires_approval=True,
            tier=node.get("tier"),
            dependencies=[],
        )

    def _build_microservice_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """EKS microservice scale-up and verify step.

        两档策略在这一步的命令**相同**（都是 scale + rollout status）——差别不在
        这里，而在 pilot light 需要先跑 ``build_nodegroup_steps()`` 把节点容量
        拉起来。AWS 的定义正是如此：warm standby "only requires you to scale up"。

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.
            ctx: Context dict.

        Returns:
            DRStep for microservice DR activation.
        """
        svc_name = node["name"]
        tier = node.get("tier", "Tier2")
        deployment = self._deployment_name(svc_name)
        kube = self._kubectl_target()
        replicas = self._target_replicas(tier)

        # rollout 超时要覆盖「镜像首次拉取」——pilot light 下目标区节点是全新的，
        # 本地无镜像缓存，arm64 镜像首拉实测可达 60–120s。给 300s。
        rollout_timeout = 300 if self.strategy == STRATEGY_PILOT_LIGHT else 120

        return DRStep(
            step_id=f"svc-{svc_name}",
            order=ctx.get("order", 0),
            parallel_group=ctx.get("parallel_group"),
            resource_type="Microservice",
            resource_id=node.get("id", ""),
            resource_name=svc_name,
            action="scale_up_and_verify",
            command=(
                f"kubectl scale deployment/{deployment} --replicas={replicas} {kube}\n"
                f"kubectl rollout status deployment/{deployment} "
                f"--timeout={rollout_timeout}s {kube}"
            ),
            validation=(
                f"kubectl get deployment/{deployment} {kube} "
                f"-o jsonpath='{{.status.readyReplicas}}'"
            ),
            expected_result=str(replicas),
            rollback_command=(
                f"kubectl scale deployment/{deployment} --replicas=0 {kube}"
            ),
            estimated_time=rollout_timeout,
            requires_approval=(tier == "Tier0"),
            tier=tier,
            dependencies=[],
        )

    def _build_loadbalancer_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """ALB/NLB 健康确认 + DNS 切换步骤。

        DNS 命令由 ``planner/dns_commands`` 构造。原实现有两处不可执行：
        ``--change-batch file://dns-failover.json`` 引用一个**工具从不生成**的文件，
        且 ``--hosted-zone-id $ZONE_ID`` 是未绑定的 shell 变量。

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.
            ctx: Context dict.

        Returns:
            DRStep for load balancer DNS switch.
        """
        from planner.dns_commands import build_failover_change

        lb_name = node["name"]
        try:
            profile = _p()
        except Exception:  # noqa: BLE001
            profile = None

        # 恢复区入口的 DNS 名。profile 未提供时给可辨识占位——不拼一个假的 ALB 域名。
        target_dns = (
            (profile.get("dr.target_entry_dns", "") if profile else "")
            or f"<{target} 入口的 DNS 名，例如 ALB 的 DNSName>"
        )
        source_dns = (
            (profile.get("dr.source_entry_dns", "") if profile else "")
            or f"<{source} 入口的 DNS 名>"
        )
        dns_switch = build_failover_change(profile, target_dns, target)
        dns_rollback = build_failover_change(profile, source_dns, source)

        return DRStep(
            step_id=f"lb-{lb_name}",
            order=ctx.get("order", 0),
            resource_type="LoadBalancer",
            resource_id=node.get("id", ""),
            resource_name=lb_name,
            action="verify_health_and_switch_dns",
            command=(
                f"# 1. 先确认恢复区 ALB 的目标健康——DNS 切过去之前必须确认后端可用，\n"
                f"#    否则流量切到一个不健康的入口，而 TTL 缓存让回退不是立刻生效的。\n"
                f"TG_ARN=$(aws elbv2 describe-target-groups --region {target} "
                f"--query \"TargetGroups[?contains(TargetGroupName,'{lb_name}')]"
                f".TargetGroupArn | [0]\" --output text)\n"
                f"aws elbv2 describe-target-health --target-group-arn \"$TG_ARN\" "
                f"--region {target} "
                f"--query 'TargetHealthDescriptions[].TargetHealth.State' --output text\n"
                f"# 2. 切 Route 53 主记录\n"
                f"{dns_switch['command']}"
            ),
            validation=dns_switch["validation"],
            expected_result=dns_switch["expected"],
            rollback_command=(
                f"# 回切到源区入口。注意 TTL 缓存：生效时间取决于当前 TTL。\n"
                f"{dns_rollback['command']}"
            ),
            estimated_time=180,
            requires_approval=True,
            tier=None,
            dependencies=[],
        )

    def _build_lambdafunction_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """Lambda function validation and region switch step.

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.
            ctx: Context dict.

        Returns:
            DRStep for Lambda function DR activation.
        """
        fn_name = node["name"]
        return DRStep(
            step_id=f"lambda-{fn_name}",
            order=ctx.get("order", 0),
            resource_type="LambdaFunction",
            resource_id=node.get("id", ""),
            resource_name=fn_name,
            action="verify_lambda_function",
            command=(
                f"aws lambda invoke --function-name {fn_name} "
                f"--region {target} "
                f"--payload '{{\"source\": \"dr-healthcheck\"}}' "
                f"/tmp/{fn_name}-response.json\n"
                f"cat /tmp/{fn_name}-response.json"
            ),
            validation=(
                f"aws lambda get-function-configuration "
                f"--function-name {fn_name} --region {target} "
                f"--query 'State' --output text"
            ),
            expected_result="Active",
            rollback_command=(
                f"# Lambda functions are stateless; update event source mapping\n"
                f"aws lambda update-event-source-mapping "
                f"--region {source} "
                f"--uuid $EVENT_SOURCE_UUID --enabled"
            ),
            estimated_time=30,
            requires_approval=False,
            tier=node.get("tier"),
            dependencies=[],
        )

    def _build_k8sservice_step(
        self, node: Dict[str, Any], source: str, target: str, ctx: Dict[str, Any]
    ) -> DRStep:
        """Kubernetes Service endpoint update step.

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.
            ctx: Context dict.

        Returns:
            DRStep for K8s service DR activation.
        """
        svc_name = node["name"]
        return DRStep(
            step_id=f"k8ssvc-{svc_name}",
            order=ctx.get("order", 0),
            resource_type="K8sService",
            resource_id=node.get("id", ""),
            resource_name=svc_name,
            action="verify_k8s_service_endpoints",
            command=(
                f"kubectl get endpoints {svc_name} "
                f"--context {target}-cluster\n"
                f"kubectl describe service {svc_name} "
                f"--context {target}-cluster"
            ),
            validation=(
                f"kubectl get endpoints {svc_name} "
                f"--context {target}-cluster "
                f"-o jsonpath='{{.subsets[0].addresses[0].ip}}'"
            ),
            expected_result="<non-empty IP>",
            rollback_command=(
                f"kubectl delete endpoints {svc_name} "
                f"--context {target}-cluster\n"
                f"# Service endpoints will repopulate from source cluster"
            ),
            estimated_time=60,
            requires_approval=False,
            tier=node.get("tier"),
            dependencies=[],
        )

    def _build_generic_step(
        self, node: Dict[str, Any], source: str, target: str
    ) -> DRStep:
        """Generic fallback step for unsupported resource types.

        Args:
            node: Node dict.
            source: Source region/AZ.
            target: Target region/AZ.

        Returns:
            Placeholder DRStep that requires manual intervention.
        """
        resource_name = node.get("name", "unknown")
        resource_type = node.get("type", "Unknown")

        # Use validation template from registry
        from registry import registry_loader
        reg = registry_loader.get_registry()
        info = reg.get_type(resource_type)
        try:
            validation_cmd = info.validation_template.format(
                name=resource_name, target=target, type=resource_type
            ) if info.validation_template else (
                f"echo 'NO VALIDATION DEFINED for {resource_type} "
                f"{resource_name}' >&2 && exit 1"
            )
        except (KeyError, IndexError):
            # Template contains unescaped braces (e.g. jsonpath expressions).
            # Fall back to a safe placeholder rather than crashing.
            validation_cmd = (
                f"echo 'NO VALIDATION DEFINED for {resource_type} "
                f"{resource_name}' >&2 && exit 1"
            )

        return DRStep(
            step_id=f"generic-{resource_name}",
            order=0,
            resource_type=resource_type,
            resource_id=node.get("id", ""),
            resource_name=resource_name,
            action="manual_switchover",
            command=(
                f"# ✗ {resource_type} '{resource_name}' 没有对应的步骤生成器，"
                f"本工具无法为它产出可执行的切换命令。\n"
                f"# Source: {source} → Target: {target}\n"
                f"# 处理方式（二选一）：\n"
                f"#   a) 为该类型实现 _build_{resource_type.lower()}_step；\n"
                f"#   b) 在 registry/custom_types.yaml 里声明它，或确认它不需要切换。\n"
                f"# 刻意用 exit 1 而不是留一句 TODO 注释：注释是合法的空命令，\n"
                f"#   执行会「成功」，于是演练全绿而这一步什么都没做。\n"
                f"echo 'BLOCKED: no step builder for {resource_type} "
                f"{resource_name}' >&2 && exit 1"
            ),
            validation=validation_cmd,
            expected_result="Resource healthy in target",
            rollback_command=(
                f"# 该类型无回滚命令生成器。若本步骤产生了变更，回滚须人工编排。\n"
                f"echo 'NO ROLLBACK DEFINED for {resource_name}' >&2 && exit 1"
            ),
            estimated_time=120,
            requires_approval=True,
            tier=node.get("tier"),
            dependencies=[],
        )
