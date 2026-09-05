"""
assessment/rpo_estimator.py — RPO 推导

**这个模块存在的唯一理由是：不编数字。**

原实现（``plan_generator._estimate_rpo``）是一张硬编码表::

    RDSCluster / RDSInstance → 5 分钟
    DynamoDBTable            → 0
    其他一切                  → 15 分钟

样例产物里那个「Estimated RPO: 15 minutes」就来自 ``else`` 分支。三重问题：

1. **数值方向就错。** Aurora Global Database 的 RPO，AWS 文档写的是
   "typically a non-zero value measured in **seconds**"，不是 5 分钟。
2. **与实际配置无关。** 没有跨区复制时 RPO 是「上次备份到故障点」的间隔——
   小时级——而这张表照样给 5 分钟。
3. **审计答不上来。** 「这个 15 分钟怎么算的」没有答案。

改为按**实际复制拓扑**推导，并且在无法从配置推定时**返回 None 而不是编一个数**。
渲染层把 None 显示为「不可从配置推定（需实测）」。给不出数字比给错数字诚实，
也更符合监管对举证的要求。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: 各拓扑的 RPO 量级（秒）。仅用于**量级**判断，不作为可举证的实测值。
#: 依据 AWS 官方描述：
#:   · Aurora Global Database 失败切换 "RPO … typically a non-zero value
#:     measured in seconds"（r53recovery 文档）
#:   · switchover 为零数据丢失
#:   · DynamoDB Global Tables 复制延迟通常亚秒到秒级
#:   · S3 CRR 未开 RTC 时**无 SLA**，故不给量级
_TOPOLOGY_ORDER_OF_MAGNITUDE: Dict[str, Optional[int]] = {
    "global_database": 60,        # 秒级；取 60s 作保守上界
    "cross_region_replica": 300,  # 异步只读副本，分钟级
    "global_tables": 60,
    "crr_rtc": 900,               # RTC 有 SLA（15 分钟内 99.99%）
    "crr": None,                  # 无 RTC → 无 SLA → 不给数字
    "none": None,
    "snapshot_copy": None,        # 取决于备份间隔，需实测
}


@dataclass
class RPOAssessment:
    """RPO 评估结果。

    Attributes:
        minutes: 可举证的 RPO 上界（分钟）。**None 表示无法从配置推定**——
            此时必须实测或按备份间隔另行论证，绝不填一个占位数字。
        basis: 每个组件的推导依据，供审计追溯。
        unmeasurable: 无法从配置推定的组件列表。
        measurement_commands: 用于取得真实值的命令（演练时执行并回填）。
    """

    minutes: Optional[int] = None
    basis: List[Dict[str, str]] = field(default_factory=list)
    unmeasurable: List[str] = field(default_factory=list)
    measurement_commands: List[Dict[str, str]] = field(default_factory=list)

    @property
    def is_stateable(self) -> bool:
        """能否给出一个可举证的数字。"""
        return self.minutes is not None and not self.unmeasurable


class RPOEstimator:
    """按实际复制拓扑推导 RPO。

    Args:
        profile: DRProfile 实例；None 时一律判为不可推定。
        mode: ``drill``（switchover，零丢失）或 ``failover``（有丢失）。
    """

    #: 数据层资源类型 → profile 里的组件名
    _TYPE_TO_COMPONENT = {
        "RDSCluster": "aurora",
        "RDSInstance": "aurora",
        "DynamoDBTable": "dynamodb",
        "S3Bucket": "s3",
        "SQSQueue": "sqs",
    }

    def __init__(self, profile: Optional[Any] = None, mode: str = "drill") -> None:
        self.profile = profile
        self.mode = mode

    def assess(self, nodes: Sequence[Dict[str, Any]]) -> RPOAssessment:
        """推导范围内数据层资源的 RPO。

        Args:
            nodes: 范围内的节点（只看数据层类型）。

        Returns:
            RPOAssessment。
        """
        result = RPOAssessment()
        components = {
            self._TYPE_TO_COMPONENT[n["type"]]
            for n in nodes
            if n.get("type") in self._TYPE_TO_COMPONENT
        }
        if not components:
            result.basis.append({
                "component": "(none)",
                "topology": "-",
                "rpo": "0",
                "reason": "范围内没有有状态资源，无数据丢失风险。",
            })
            result.minutes = 0
            return result

        worst: Optional[int] = 0
        for component in sorted(components):
            if component == "sqs":
                # SQS 结构性不可复制：丢失量是切换瞬间的消息数，只能实测。
                result.unmeasurable.append("sqs")
                result.basis.append({
                    "component": "sqs",
                    "topology": "not_replicable",
                    "rpo": "需实测",
                    "reason": (
                        "SQS 无跨区复制能力。丢失量 = 切换瞬间 visible + in-flight "
                        "的消息数，只能在切换前实测，无法从配置推定。"
                    ),
                })
                result.measurement_commands.append({
                    "component": "sqs",
                    "purpose": "切换瞬间的消息丢失量",
                    "command": (
                        "aws sqs get-queue-attributes --queue-url <QUEUE_URL> "
                        "--attribute-names ApproximateNumberOfMessages "
                        "ApproximateNumberOfMessagesNotVisible"
                    ),
                })
                continue

            topology = self._topology(component)

            # drill 走 switchover：Aurora Global Database 是零数据丢失。
            if (
                component == "aurora"
                and topology == "global_database"
                and self.mode == "drill"
            ):
                result.basis.append({
                    "component": "aurora",
                    "topology": topology,
                    "rpo": "0",
                    "reason": (
                        "计划内 switchover：AWS 文档明确「所有 secondary 在开始时与 "
                        "primary 同步，新 primary 继续服务而不丢任何数据」。"
                    ),
                })
                continue

            magnitude = _TOPOLOGY_ORDER_OF_MAGNITUDE.get(topology)
            if magnitude is None:
                result.unmeasurable.append(component)
                result.basis.append({
                    "component": component,
                    "topology": topology,
                    "rpo": "不可从配置推定",
                    "reason": self._unmeasurable_reason(component, topology),
                })
                result.measurement_commands.append(
                    self._measurement_command(component)
                )
                continue

            result.basis.append({
                "component": component,
                "topology": topology,
                "rpo": f"≤ {magnitude}s（量级，非实测）",
                "reason": (
                    f"按 {topology} 的官方描述取保守上界。**这是量级估计，"
                    f"不能作为监管举证值**——举证需演练实测。"
                ),
            })
            if worst is not None:
                worst = max(worst, magnitude)

        if result.unmeasurable:
            # 只要有一个组件说不清，整体 RPO 就不能给数字。
            result.minutes = None
        else:
            result.minutes = 0 if not worst else max(1, worst // 60)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _topology(self, component: str) -> str:
        if self.profile is None:
            return "none"
        try:
            return self.profile.dr_topology(component)
        except Exception:  # noqa: BLE001
            return "none"

    @staticmethod
    def _unmeasurable_reason(component: str, topology: str) -> str:
        if topology in ("none", ""):
            return (
                f"{component} 未配置跨区复制。RPO = 「上次备份到故障点」的间隔，"
                "属小时级，且取决于备份策略——必须按实际备份间隔论证，不能凭配置推定。"
            )
        if component == "s3" and topology == "crr":
            return (
                "S3 CRR 未启用 Replication Time Control 时**没有复制时间 SLA**，"
                "延迟随对象大小与流量波动。要给可举证的数字须开启 RTC 或实测。"
            )
        if topology == "snapshot_copy":
            return (
                "依赖跨区快照复制，RPO 等于快照间隔加复制耗时，须按实际调度论证。"
            )
        return f"{component} 的拓扑 {topology} 无法推出可举证的 RPO。"

    @staticmethod
    def _measurement_command(component: str) -> Dict[str, str]:
        """给出取真实值的命令。

        Aurora 一项刻意**不指定** CloudWatch 复制延迟指标名：该指标名未经本项目
        联机核实，而指标名写错时 ``get-metric-statistics`` 返回空数据集**不报错**，
        会让人误以为延迟为 0。这里改为提示用 ``list-metrics`` 先查实际指标名。
        """
        if component == "aurora":
            return {
                "component": "aurora",
                "purpose": "实测复制延迟（先确认本账号实际暴露的指标名）",
                "command": (
                    "aws cloudwatch list-metrics --namespace AWS/RDS "
                    "--query \"Metrics[?contains(MetricName,'RPO') || "
                    "contains(MetricName,'Lag')].MetricName\" --output text"
                ),
            }
        if component == "dynamodb":
            return {
                "component": "dynamodb",
                "purpose": "实测 Global Table 复制延迟",
                "command": (
                    "aws cloudwatch get-metric-statistics --namespace AWS/DynamoDB "
                    "--metric-name ReplicationLatency "
                    "--dimensions Name=TableName,Value=<TABLE> "
                    "Name=ReceivingRegion,Value=<TARGET_REGION> "
                    "--start-time $(date -u -d '1 hour ago' +%Y-%m-%dT%H:%M:%S) "
                    "--end-time $(date -u +%Y-%m-%dT%H:%M:%S) "
                    "--period 300 --statistics Maximum"
                ),
            }
        if component == "s3":
            return {
                "component": "s3",
                "purpose": "实测待复制对象数（应为 0）",
                "command": (
                    "aws cloudwatch get-metric-statistics --namespace AWS/S3 "
                    "--metric-name OperationsPendingReplication "
                    "--dimensions Name=SourceBucket,Value=<BUCKET> "
                    "--start-time $(date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%S) "
                    "--end-time $(date -u +%Y-%m-%dT%H:%M:%S) "
                    "--period 60 --statistics Maximum"
                ),
            }
        return {
            "component": component,
            "purpose": "实测复制延迟",
            "command": f"# 需按 {component} 的实际复制机制补充实测命令",
        }
