"""
dr_profile.py — dr-plan-generator 自带的 workload profile 读取器

为什么不复用父仓库的 profiles/profile_loader.py
-----------------------------------------------
父仓库的 ``profiles/`` 包有 16 处消费者（chaos/ rca/ infra/ scripts/ …），
把它搬进本模块会打断那些消费者；而继续 ``sys.path.insert(0, '..')`` 反向
import 又让本模块无法作为独立制品交付。

本模块只用到 4 个 profile 属性（domain / health_endpoint /
ssm_dynamodb_region_key / 两个 k8s context 键），因此正确的解法是：
**依赖数据契约，不依赖兄弟包**——自带一个小读取器，消费同一种 YAML 格式。
``profiles/petsite.yaml`` 依旧可以直接喂进来，它是数据不是代码。

刻意没有默认 profile
--------------------
原 ``profile_loader._DEFAULT_PROFILE`` 直接指向 ``petsite.yaml``：不传参数
就静默加载某个具体 workload 的配置。对一个声称通用的容灾工具，这意味着
「忘记指定」和「指定了 petsite」在行为上无法区分，而错误的 profile 会生成
指向错误域名、错误 SSM 键、错误命名空间的**看起来正常**的计划。
因此这里改为：显式设置 > ``DR_PROFILE`` 环境变量 > 抛错。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List, Optional, Union

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Strategy / topology vocabularies
# ---------------------------------------------------------------------------

#: 本工具只支持这两档。backup & restore 的 RTO（24h 内）对本场景无意义；
#: active-active 需要解决跨区写冲突，复杂度不成比例。
SUPPORTED_STRATEGIES = ("pilot_light", "warm_standby")

#: 各组件中“数据已复制到恢复区”成立的拓扑取值。
REPLICATED_AURORA = frozenset({"global_database", "cross_region_replica"})
REPLICATED_DYNAMODB = frozenset({"global_tables"})
REPLICATED_S3 = frozenset({"crr", "crr_rtc"})


class ProfileError(Exception):
    """Profile 文件缺失或格式非法。"""


class ProfileNotConfigured(ProfileError):
    """未指定 profile。刻意不回退到任何具体 workload——见模块 docstring。"""


class DRProfile:
    """一个 workload 的 DR 相关配置。

    Args:
        path: profile YAML 路径。**必填**，没有默认值。

    Raises:
        ProfileError: 文件不存在或不是 YAML 映射。
    """

    _PLACEHOLDER_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")

    def __init__(self, path: str) -> None:
        if not path:
            raise ProfileNotConfigured("Profile path is required (no default profile).")
        if not os.path.exists(path):
            raise ProfileError(f"Profile file not found: {path}")
        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            raise ProfileError(
                f"Profile root must be a YAML mapping, got {type(data).__name__}: {path}"
            )
        self.path = path
        self._data: Dict[str, Any] = data

    # ------------------------------------------------------------------
    # Dotted access with ${ENV_VAR} expansion
    # ------------------------------------------------------------------

    def _resolve_placeholder(self, value: Any, default: Any, dotted_key: str) -> Any:
        """把 ``${ENV_VAR}`` 换成环境变量值。

        环境变量未设置时返回调用方的 ``default``，**不把占位符原文交出去**：
        原文一定会在下游造成更难定位的故障（拿 ``${ROUTE53_ZONE_ID}`` 当
        zone id 去调 Route 53、拿 ``${K8S_CONTEXT_TARGET}`` 当 kubectl context），
        而 default 至少是调用方预期过的值。
        """
        m = self._PLACEHOLDER_RE.match(value) if isinstance(value, str) else None
        if not m:
            return value
        env_name = m.group(1)
        env_val = os.environ.get(env_name)
        if env_val:
            return env_val
        logger.warning(
            "Profile key %s is an unexpanded placeholder ${%s} and that env var is "
            "not set; falling back to default=%r. Set %s or write a literal value.",
            dotted_key, env_name, default, env_name,
        )
        return default

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """点分路径访问，值为 ``${ENV_VAR}`` 时用环境变量展开。

        Args:
            dotted_key: 如 ``dns.ttl_normal``。
            default: 未找到或占位符无法展开时返回的值。

        Returns:
            配置值或 ``default``。
        """
        node: Any = self._data
        for key in dotted_key.split("."):
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        if node is None:
            return default
        return self._resolve_placeholder(node, default, dotted_key)

    # ------------------------------------------------------------------
    # Identity / application
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Profile 名称。"""
        return self.get("profile.name", "unknown")

    @property
    def region(self) -> str:
        """Workload 主区。"""
        return self.get("aws_resources.primary_region", "") or ""

    @property
    def domain(self) -> str:
        """应用主域名。"""
        return self.get("application.domain", "") or ""

    @property
    def health_endpoint(self) -> str:
        """健康检查端点路径。"""
        return self.get("application.health_endpoint", "/health")

    @property
    def health_check_command(self) -> str:
        """渲染后的健康检查命令。"""
        template = self.get(
            "application.health_check_command",
            "curl -sf https://{domain}{health_endpoint} | jq '.status'",
        )
        return str(template).strip().format(
            domain=self.domain, health_endpoint=self.health_endpoint
        )

    @property
    def alarm_prefix(self) -> str:
        """CloudWatch 告警前缀（两级回退，与父仓库语义一致）。"""
        return (
            self.get("monitoring.cloudwatch_alarm_prefix")
            or self.get("application.alarm_prefix")
            or "app"
        )

    # ------------------------------------------------------------------
    # Parameter Store / DNS / Kubernetes
    # ------------------------------------------------------------------

    @property
    def ssm_dynamodb_region_key(self) -> str:
        """DynamoDB region 切换使用的 SSM 参数路径。"""
        return (
            self.get("parameter_store.keys.dynamodb_region")
            or self.get("parameter_store.dynamodb_region_key")
            or f"/{self.name}/dynamodb-region"
        )

    @property
    def dns_hosted_zone_id(self) -> str:
        """Route 53 Hosted Zone ID。未配置时返回空串，便于调用方 falsy 判断。"""
        return self.get("dns.hosted_zone_id", "") or ""

    @property
    def dns_primary_record(self) -> str:
        """DNS 主记录名，缺省回落到 domain。"""
        return self.get("dns.primary_record") or self.domain

    @property
    def dns_ttl_normal(self) -> int:
        """常态 DNS TTL（秒）。"""
        return int(self.get("dns.ttl_normal", 300))

    @property
    def dns_ttl_pre_switchover(self) -> int:
        """切换前调低的 DNS TTL（秒）。"""
        return int(self.get("dns.ttl_pre_switchover", 60))

    @property
    def k8s_namespace(self) -> str:
        """工作负载所在的 K8s 命名空间。也是 M4 范围锚点。"""
        return self.get("kubernetes.namespace", "default")

    def get_deployment_name(self, service_name: str) -> str:
        """把图谱里的服务名翻译成 K8s Deployment 名。

        Profile 的职责就是这类**名字翻译**（图谱名 ↔ Deployment 名 ↔
        CloudWatch 维度），不承载 tier 之类的判断依据——那些只认图谱，
        避免出现双源真相。

        Args:
            service_name: 图谱中的服务名。

        Returns:
            Deployment 名；未配置映射时原样返回。
        """
        deploy_map = self.get("kubernetes.deployment_map", {}) or {}
        if service_name in deploy_map:
            return deploy_map[service_name]
        per_service = self.get(f"services.{service_name}.k8s_deployment")
        return per_service or service_name

    # ------------------------------------------------------------------
    # DR section
    # ------------------------------------------------------------------

    @property
    def dr_strategy(self) -> str:
        """容灾策略：``pilot_light`` 或 ``warm_standby``。

        Raises:
            ProfileError: 值不在支持范围内。刻意不静默回落——策略决定生成
                哪些步骤，猜错会产出一份缺步骤但看起来完整的计划。
        """
        value = self.get("dr.strategy", "")
        if value not in SUPPORTED_STRATEGIES:
            raise ProfileError(
                f"dr.strategy must be one of {list(SUPPORTED_STRATEGIES)}, got {value!r}. "
                "backup_restore and multi_site_active_active are out of scope."
            )
        return value

    @property
    def dr_target_region(self) -> str:
        """恢复区。"""
        return self.get("dr.target_region", "") or ""

    def dr_topology(self, component: str) -> str:
        """返回某个数据组件声明的跨区复制拓扑。

        Args:
            component: ``aurora`` / ``dynamodb`` / ``s3``。

        Returns:
            拓扑名，未声明时为 ``none``。
        """
        return self.get(f"dr.data.{component}.topology", "none") or "none"

    @property
    def dr_excluded(self) -> list:
        """明确排除在切换范围外的资源，每项应含 ``reason``。

        排除必须是**显式且带原因**的：审计要能看出「这个东西是被有意排除的，
        不是漏了」。
        """
        return self.get("dr.excluded", []) or []

    def dr_excluded_reasons(self) -> Dict[str, str]:
        """把排除项摊平成 ``{name_or_pattern: reason}``。"""
        out: Dict[str, str] = {}
        for item in self.dr_excluded:
            if not isinstance(item, dict):
                continue
            key = item.get("name") or item.get("pattern")
            if key:
                out[str(key)] = str(item.get("reason", "unspecified"))
        return out

    def strategy_feasibility(self) -> List[Dict[str, str]]:
        """检查所声明的策略，其数据层前提是否真的成立。

        为什么必须有这一步
        ------------------
        AWS 对两档策略的定义都**要求数据已复制到恢复区**：

        - pilot light：“Replicate your data into the recovery Region … Resources
          required to support data replication and backup … are always on.”
        - warm standby：“Data is replicated and live in the recovery Region.”

        若数据层实际没有任何跨区复制，那么无论计算层做得多好，真实档位
        就是 backup & restore（RPO 数小时）。此时仍然输出一份标称
        “RPO 秒级”的计划，是把**未达标**包装成**已达标**——这类计划在演练里
        可能侥幸通过（因为演练前常常手工补过数据），在真灾里必然失守。

        Returns:
            未满足项列表，每项含 ``component`` / ``requirement`` /
            ``actual`` / ``implication``。空列表表示前提齐备。
        """
        unmet: List[Dict[str, str]] = []

        checks = (
            ("aurora", REPLICATED_AURORA, "Aurora Global Database 或跨区只读副本"),
            ("dynamodb", REPLICATED_DYNAMODB, "DynamoDB Global Tables"),
            ("s3", REPLICATED_S3, "S3 跨区复制（CRR）"),
        )
        for component, acceptable, requirement in checks:
            if not self.get(f"dr.data.{component}"):
                continue  # 该组件不在本 workload 中
            actual = self.dr_topology(component)
            if actual not in acceptable:
                unmet.append({
                    "component": component,
                    "requirement": requirement,
                    "actual": actual,
                    "implication": (
                        f"{component} 无跨区复制，恢复只能依赖备份还原，"
                        f"RPO 退化到小时级；声明的 {self.get('dr.strategy', '?')} "
                        "在数据层并不成立。"
                    ),
                })

        # SQS 不可复制是结构性事实，不是配置疏漏——但数据丢失量必须被量化。
        if self.get("dr.data.sqs"):
            unmet.append({
                "component": "sqs",
                "requirement": "无（SQS 不支持跨区复制）",
                "actual": "not_replicable",
                "implication": (
                    "切换瞬间源队列中 visible + in-flight 的消息即为业务数据丢失，"
                    "必须在切换前抓取消息数并让业务方知情。"
                ),
            })

        return unmet

    # ------------------------------------------------------------------
    # DR — EKS (compute layer)
    # ------------------------------------------------------------------

    @property
    def dr_eks_target_cluster(self) -> str:
        """恢复区的 EKS 集群名。"""
        return self.get("dr.eks.target_cluster", "") or ""

    @property
    def dr_eks_nodegroups(self) -> List[Dict[str, Any]]:
        """恢复区需要扩容的节点组列表。

        pilot light 下这些节点组常态 ``desired=0``，切换时必须先扩容并
        **等节点 Ready**，否则 scale Deployment 只会让 Pod 永久 Pending。
        """
        return self.get("dr.eks.nodegroups", []) or []

    @property
    def dr_eks_requires_ecr_replication(self) -> bool:
        """恢复区是否需要预先复制容器镜像。

        ECR 是**区域级**服务。镜像没复制过去，Pod 一律 ImagePullBackOff，
        而这只有在扩容之后才暴露——属于必须放进 phase-0 前置校验的项。
        """
        return bool(self.get("dr.eks.ecr_replication_required", True))


# ---------------------------------------------------------------------------
# Active profile (module-level, lazily resolved)
# ---------------------------------------------------------------------------

_active: Optional[DRProfile] = None

#: 环境变量名。CLI 用 ``--profile``；库/测试可用此变量。
PROFILE_ENV_VAR = "DR_PROFILE"


def set_active_profile(profile: Union[str, DRProfile, None]) -> Optional[DRProfile]:
    """设置进程级 active profile。

    Args:
        profile: YAML 路径、已构造的 DRProfile，或 None（清空，便于测试隔离）。

    Returns:
        设置后的 DRProfile，或 None。
    """
    global _active
    if profile is None:
        _active = None
    elif isinstance(profile, DRProfile):
        _active = profile
    else:
        _active = DRProfile(profile)
    return _active


def get_active_profile() -> DRProfile:
    """返回 active profile，未设置时尝试 ``DR_PROFILE`` 环境变量，否则抛错。

    Returns:
        DRProfile 实例。

    Raises:
        ProfileNotConfigured: 既未显式设置也没有 ``DR_PROFILE``。
    """
    global _active
    if _active is not None:
        return _active
    env_path = os.environ.get(PROFILE_ENV_VAR)
    if env_path:
        _active = DRProfile(env_path)
        return _active
    raise ProfileNotConfigured(
        "No workload profile configured. Pass --profile <profile.yaml> or set "
        f"{PROFILE_ENV_VAR}. There is deliberately no default: a wrong profile "
        "silently produces a plan pointing at the wrong domain, SSM keys and "
        "namespace, which looks correct until it is executed."
    )
