"""
planner/preflight.py — phase-0 前置校验

为什么这些检查值得单独一个模块
------------------------------
它们的共同特征是：**不查就切，失败点会出现在切换中途，而且症状离根因很远。**

- ECR 是**区域级**服务。镜像没复制到恢复区，Pod 一律 ImagePullBackOff——
  而这只在节点扩容、Pod 开始调度之后才暴露，此时数据层可能已经切过去了。
- 目标区 arm64 实例类型不可用或 vCPU 配额不足，节点组扩容会「成功提交」
  然后永远到不了 desired 数量。
- 应用配置指向错区域：**不报错**，只是页面 500。今天踩的 `petfoodapiurl`
  少一段路径就是这一类。
- 跨区加密数据没有目标区可用的 KMS 密钥——金融场景最常见的隐性阻塞。

这些本该由 Amazon Application Recovery Controller 的 readiness check 承担，
但该功能已于 **2026-04-30 起对新客户关闭**，所以必须自建。

设计原则
--------
每个检查都必须是**可执行且有明确期望值**的命令。配置缺失时不生成半个检查，
而是记入 ``gaps`` 让调用方汇入计划——留白比错误的完整感安全。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from models import DRStep

logger = logging.getLogger(__name__)

#: EC2 On-Demand Standard 实例族的 vCPU 配额代码（A/C/D/H/I/M/R/T/Z，含 Graviton）。
#: 可用 profile 的 ``dr.eks.vcpu_quota_code`` 覆盖。
#: ⚠️ 该代码请在自己账号用 `aws service-quotas list-service-quotas --service-code ec2`
#: 核对一次——配额代码随 AWS 调整过，写错会让检查查到一个不相关的配额。
DEFAULT_VCPU_QUOTA_CODE = "L-1216C47A"


class PreflightBuilder:
    """生成 phase-0 的就绪检查步骤。

    Args:
        strategy: ``pilot_light`` / ``warm_standby``。
        mode: ``drill`` / ``failover``。
        profile: DRProfile 实例；None 时多数检查会降级为缺口记录。
    """

    def __init__(
        self,
        strategy: str = "warm_standby",
        mode: str = "drill",
        profile: Optional[Any] = None,
    ) -> None:
        self.strategy = strategy
        self.mode = mode
        self.profile = profile
        #: 因配置缺失而无法生成的检查，由调用方汇入计划产物。
        self.gaps: List[Dict[str, str]] = []

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def build(
        self,
        source: str,
        target: str,
        nodes: Sequence[Dict[str, Any]],
        start_order: int = 1,
    ) -> List[DRStep]:
        """产出全部就绪检查。

        Args:
            source: 源 region。
            target: 恢复区 region。
            nodes: 范围内的节点（用于按类型派生检查对象）。
            start_order: 起始 order 序号。

        Returns:
            DRStep 列表。
        """
        steps: List[DRStep] = []
        order = start_order

        for factory in (
            self._ecr_images,
            self._instance_type_availability,
            self._vcpu_quota,
            self._kms_keys,
            self._app_config,
            self._aurora_sync_status,
            self._manifests_applied,
        ):
            for step in factory(source, target, nodes):
                step.order = order
                steps.append(step)
                order += 1
        return steps

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _ecr_images(
        self, source: str, target: str, nodes: Sequence[Dict[str, Any]]
    ) -> List[DRStep]:
        """恢复区是否已有容器镜像。

        ECR 是区域级服务。镜像不在恢复区，Pod 一律 ImagePullBackOff——而这只在
        节点扩容、Pod 开始调度之后才暴露，那时数据层可能已经切过去了，
        回滚成本远高于切换前多跑一条 describe。
        """
        if not self._get("dr.eks.ecr_replication_required", True):
            return []

        repos = self._ecr_repositories(nodes)
        if not repos:
            self.gaps.append({
                "component": "ecr",
                "requirement": "恢复区的 ECR 仓库清单（图谱 ECRRepository 节点或 "
                               "dr.eks.ecr_repositories）",
                "actual": "未知",
                "implication": (
                    "无法校验镜像是否已跨区复制。镜像缺失的表现是 Pod "
                    "ImagePullBackOff，且只有在扩容之后才暴露。"
                ),
            })
            return []

        tag = self._get("dr.eks.image_tag", "") or ""
        steps: List[DRStep] = []
        for repo in repos:
            if tag:
                cmd = (
                    f"aws ecr describe-images --repository-name {repo} "
                    f"--image-ids imageTag={tag} --region {target} "
                    f"--query 'imageDetails[0].imageDigest' --output text"
                )
                expected = f"镜像 {repo}:{tag} 在 {target} 存在（返回 digest）"
            else:
                cmd = (
                    f"aws ecr describe-images --repository-name {repo} "
                    f"--region {target} --query 'length(imageDetails)' --output text"
                )
                expected = "> 0（仓库中至少有一个镜像）"
            steps.append(DRStep(
                step_id=f"preflight-ecr-{repo}",
                order=0,
                parallel_group="pg-preflight-ecr",
                resource_type="ECRRepository",
                resource_name=repo,
                action="check_image_present_in_target",
                command=(
                    "# ECR 是区域级服务：镜像不在恢复区则 Pod 一律 ImagePullBackOff。\n"
                    "# 注意架构也要匹配（本环境是 arm64），镜像存在但架构不符同样起不来。\n"
                    + cmd
                ),
                validation=cmd,
                expected_result=expected,
                rollback_command="# 无副作用",
                estimated_time=10,
                requires_approval=False,
            ))
        return steps

    def _instance_type_availability(
        self, source: str, target: str, nodes: Sequence[Dict[str, Any]]
    ) -> List[DRStep]:
        """恢复区是否提供所需实例类型。

        并非所有 Region 都提供所有机型。arm64（Graviton）机型的区域覆盖尤其不齐，
        而节点组扩容在机型不可用时会「成功提交」然后永远到不了 desired。
        """
        types = self._instance_types()
        if not types:
            return []
        joined = ",".join(types)
        cmd = (
            f"aws ec2 describe-instance-type-offerings --location-type region "
            f"--filters Name=instance-type,Values={joined} "
            f"--region {target} --query 'length(InstanceTypeOfferings)' --output text"
        )
        return [DRStep(
            step_id="preflight-instance-types",
            order=0,
            resource_type="EC2",
            resource_name=joined,
            action="check_instance_types_available",
            command=(
                "# 并非所有 Region 提供所有机型，arm64/Graviton 覆盖尤其不齐。\n"
                "# 机型不可用时节点组扩容会「提交成功」但永远到不了 desired。\n"
                + cmd
            ),
            validation=cmd,
            expected_result=str(len(types)),
            rollback_command="# 无副作用",
            estimated_time=10,
            requires_approval=False,
        )]

    def _vcpu_quota(
        self, source: str, target: str, nodes: Sequence[Dict[str, Any]]
    ) -> List[DRStep]:
        """恢复区 vCPU 配额是否够扩到生产容量。

        这本是 ARC readiness check 的职责之一，而该功能已于 2026-04-30 对新客户关闭，
        必须自建。配额不足的表现同样是「扩容提交成功、节点起不来」。
        """
        quota_code = self._get("dr.eks.vcpu_quota_code", DEFAULT_VCPU_QUOTA_CODE)
        needed = self._required_vcpus()
        cmd = (
            f"aws service-quotas get-service-quota --service-code ec2 "
            f"--quota-code {quota_code} --region {target} "
            f"--query 'Quota.Value' --output text"
        )
        expected = (
            f">= {needed}（按节点组 desired × 每节点 vCPU 估）"
            if needed else "足够扩到生产容量"
        )
        return [DRStep(
            step_id="preflight-vcpu-quota",
            order=0,
            resource_type="ServiceQuota",
            resource_name=quota_code,
            action="check_vcpu_quota",
            command=(
                "# ARC 的 readiness check 已于 2026-04-30 起对新客户关闭，配额校验须自建。\n"
                f"# 配额代码 {quota_code} 请在本账号用 "
                "`aws service-quotas list-service-quotas --service-code ec2` 核对一次。\n"
                + cmd
            ),
            validation=cmd,
            expected_result=expected,
            rollback_command="# 无副作用",
            estimated_time=10,
            requires_approval=False,
        )]

    def _kms_keys(
        self, source: str, target: str, nodes: Sequence[Dict[str, Any]]
    ) -> List[DRStep]:
        """恢复区的 KMS 密钥是否可用。

        跨区加密数据必须有目标区可用的密钥。KMS 密钥是**区域级**的，
        源区的密钥无法解密目标区的副本。这是金融场景最常见的隐性阻塞：
        存储与数据库都「复制成功」了，恢复时却打不开。
        """
        aliases = self._get("dr.kms_key_aliases", []) or []
        if not aliases:
            self.gaps.append({
                "component": "kms",
                "requirement": "dr.kms_key_aliases（恢复区需可用的密钥别名）",
                "actual": "未配置",
                "implication": (
                    "KMS 密钥是区域级的，源区密钥无法解密恢复区副本。未校验时的表现是"
                    "「数据都复制成功了，恢复时打不开」——金融场景最常见的隐性阻塞。"
                ),
            })
            return []
        steps: List[DRStep] = []
        for alias in aliases:
            cmd = (
                f"aws kms describe-key --key-id {alias} --region {target} "
                f"--query 'KeyMetadata.KeyState' --output text"
            )
            steps.append(DRStep(
                step_id=f"preflight-kms-{str(alias).replace('/', '-')}",
                order=0,
                parallel_group="pg-preflight-kms",
                resource_type="KMSKey",
                resource_name=str(alias),
                action="check_kms_key_usable",
                command=(
                    "# KMS 密钥是区域级的：源区密钥不能解密恢复区副本。\n" + cmd
                ),
                validation=cmd,
                expected_result="Enabled",
                rollback_command="# 无副作用",
                estimated_time=5,
                requires_approval=False,
            ))
        return steps

    def _app_config(
        self, source: str, target: str, nodes: Sequence[Dict[str, Any]]
    ) -> List[DRStep]:
        """恢复区的应用配置是否已指向本区。

        这是最阴的一类：配置指向错误区域**不会报错**，服务正常启动、健康检查通过，
        只有真实请求走到下游时才 500。今天踩的 `petfoodapiurl` 少一段路径同属此类
        ——参数存在、值看着合理、应用读到了，但指向的东西不对。
        """
        prefix = self._get("parameter_store.prefix", "") or ""
        if not prefix:
            return []
        cmd = (
            f"aws ssm get-parameters-by-path --path {prefix} --recursive "
            f"--region {target} --query 'length(Parameters)' --output text"
        )
        review = (
            f"aws ssm get-parameters-by-path --path {prefix} --recursive "
            f"--region {target} --query 'Parameters[].{{Name:Name,Value:Value}}' "
            f"--output table"
        )
        return [DRStep(
            step_id="preflight-app-config",
            order=0,
            resource_type="SSM",
            resource_name=prefix,
            action="check_target_app_config",
            command=(
                "# 配置指向错误区域**不会报错**：服务正常启动、健康检查通过，\n"
                "#   只有真实请求走到下游时才 500。所以这一步要人工过一遍取值，\n"
                "#   确认每个下游 URL / 区域名都是恢复区的。\n"
                f"{cmd}\n"
                f"# 逐项复核（需人工确认，不能只看数量）：\n"
                f"{review}"
            ),
            validation=cmd,
            expected_result=f"> 0，且所有下游端点均指向 {target}（需人工确认）",
            rollback_command="# 无副作用",
            estimated_time=60,
            requires_approval=True,
        )]

    def _aurora_sync_status(
        self, source: str, target: str, nodes: Sequence[Dict[str, Any]]
    ) -> List[DRStep]:
        """Aurora global cluster 的成员同步状态。

        刻意用 ``describe-global-clusters`` 的 ``SynchronizationStatus``
        （文档明确列出取值 ``connected`` / ``pending-resync``）作为主判据，
        而不是某个 CloudWatch 复制延迟指标——后者的准确指标名我没有联机核实过，
        写错会让检查静默查到空数据然后「通过」。
        """
        topology = self._get("dr.data.aurora.topology", "none")
        if topology != "global_database":
            return []
        gc = self._get("dr.data.aurora.global_cluster_identifier", "") or ""
        if not gc:
            return []
        cmd = (
            f"aws rds describe-global-clusters "
            f"--global-cluster-identifier {gc} --region {target} "
            f"--query 'GlobalClusters[0].GlobalClusterMembers[].SynchronizationStatus' "
            f"--output text"
        )
        return [DRStep(
            step_id="preflight-aurora-sync",
            order=0,
            resource_type="RDSCluster",
            resource_name=gc,
            action="check_global_cluster_synchronized",
            command=(
                "# 用 SynchronizationStatus 作主判据（文档明确取值 connected / \n"
                "#   pending-resync）。未选某个 CloudWatch 延迟指标，是因为指标名\n"
                "#   若写错会查到空数据然后静默「通过」——那比不检查更危险。\n"
                + cmd
            ),
            validation=cmd,
            expected_result="所有成员均为 connected（出现 pending-resync 则不得切换）",
            rollback_command="# 无副作用",
            estimated_time=15,
            requires_approval=False,
        )]

    def _manifests_applied(
        self, source: str, target: str, nodes: Sequence[Dict[str, Any]]
    ) -> List[DRStep]:
        """pilot light 假设的显式确认：恢复区的 Deployment 清单**已 apply**。

        本工具的 pilot light 模型是「清单已 apply，replicas=0，节点组 desired=0」。
        这是个假设，不能默默依赖：若清单其实没 apply，``kubectl scale`` 会因
        Deployment 不存在而失败——好在这会立刻报错，不像配置类问题那样静默。
        但放进 phase-0 意味着在动数据层**之前**就发现，而不是切到一半才发现。
        """
        if self.strategy != "pilot_light":
            return []
        services = [
            n["name"] for n in nodes
            if n.get("type") == "Microservice" and n.get("name")
        ]
        if not services:
            return []
        context = self._get("kubernetes.context_target", "") or ""
        namespace = self._get("kubernetes.namespace", "default")
        ctx_arg = f" --context {context}" if context else ""
        deployments = []
        for svc in services:
            if self.profile is not None:
                try:
                    deployments.append(self.profile.get_deployment_name(svc))
                    continue
                except Exception:  # noqa: BLE001
                    pass
            deployments.append(svc)
        joined = " ".join(f"deployment/{d}" for d in sorted(set(deployments)))
        cmd = f"kubectl get {joined} -n {namespace}{ctx_arg}"
        return [DRStep(
            step_id="preflight-manifests-applied",
            order=0,
            resource_type="K8sDeployment",
            resource_name=namespace,
            action="check_manifests_applied",
            command=(
                "# 本工具的 pilot light 模型是「清单已 apply、replicas=0、节点组 desired=0」。\n"
                "# 这是假设而非事实，所以在动数据层之前先确认，而不是切到一半才发现。\n"
                + cmd
            ),
            validation=cmd,
            expected_result=f"全部 {len(set(deployments))} 个 Deployment 存在（副本数可为 0）",
            rollback_command="# 无副作用",
            estimated_time=15,
            requires_approval=False,
        )]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get(self, key: str, default: Any = None) -> Any:
        """从 profile 取值；无 profile 时返回 default。"""
        if self.profile is None:
            return default
        try:
            return self.profile.get(key, default)
        except Exception:  # noqa: BLE001
            return default

    def _ecr_repositories(self, nodes: Sequence[Dict[str, Any]]) -> List[str]:
        """优先从图谱的 ECRRepository 节点派生，其次读 profile。

        优先图谱是有意的：仓库清单会随服务增减而变，图谱是自动同步的，
        profile 里手写的清单会漂移。
        """
        from_graph = sorted({
            str(n["name"]) for n in nodes
            if n.get("type") == "ECRRepository" and n.get("name")
        })
        if from_graph:
            return from_graph
        configured = self._get("dr.eks.ecr_repositories", []) or []
        return [str(r) for r in configured]

    def _instance_types(self) -> List[str]:
        """节点组声明的实例类型。"""
        types: List[str] = []
        for group in self._get("dr.eks.nodegroups", []) or []:
            if isinstance(group, dict):
                value = group.get("instance_type")
                if value:
                    types.append(str(value))
        return sorted(set(types))

    def _required_vcpus(self) -> int:
        """粗估需要的 vCPU 总数（节点数 × 每节点 vCPU，缺一即返回 0）。"""
        total = 0
        for group in self._get("dr.eks.nodegroups", []) or []:
            if not isinstance(group, dict):
                continue
            desired = group.get("desired")
            per_node = group.get("vcpus_per_node")
            if desired is None or per_node is None:
                return 0
            total += int(desired) * int(per_node)
        return total
