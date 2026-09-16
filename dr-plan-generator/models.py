"""
models.py — DR Plan Generator 数据模型定义

All core dataclasses for the DR planning system.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DRStep:
    """单个切换/回滚操作步骤。"""

    step_id: str
    order: int
    parallel_group: Optional[str] = None
    resource_type: str = ""
    resource_id: str = ""
    resource_name: str = ""
    action: str = ""
    command: str = ""
    validation: str = ""
    expected_result: str = ""
    rollback_command: str = ""
    estimated_time: int = 60          # seconds
    requires_approval: bool = False
    tier: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "DRStep":
        """从字典反序列化 DRStep。"""
        return cls(**{k: v for k, v in d.items() if k in {f.name for f in dataclasses.fields(cls)}})


@dataclass
class DRPhase:
    """切换计划中的一个阶段（Phase）。"""

    phase_id: str
    name: str
    layer: str                        # preflight / L0 / L1 / L2 / L3 / validation
    steps: List[DRStep] = field(default_factory=list)
    estimated_duration: int = 0       # minutes
    gate_condition: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "DRPhase":
        """从字典反序列化 DRPhase（含嵌套 steps）。"""
        d = dict(d)
        steps = [DRStep.from_dict(s) for s in d.pop("steps", [])]
        return cls(**d, steps=steps)


@dataclass
class ImpactReport:
    """影响评估报告。"""

    scope: str
    source: str
    total_affected: int = 0
    by_tier: Dict[str, list] = field(default_factory=dict)
    affected_capabilities: list = field(default_factory=list)
    single_points_of_failure: list = field(default_factory=list)
    estimated_rto_minutes: int = 0
    #: RPO 分钟数。**None 表示「不可推定」，不是 0。**
    #
    # 曾经这里是 `int = 0`，`_estimate_rpo` 在推不出时返回 0。代价是渲染出
    # 「Estimated RPO | 0 min」—— 在容灾语境里 0 意味着**零数据丢失**，
    # 是最令人安心的值，而真实含义是「我们不知道」。
    # 这与本项目最核心那条不变量同形：无法区分时必须显式说无法区分，
    # 不能借一个看起来正常的数字蒙过去。
    estimated_rpo_minutes: Optional[int] = None
    risk_matrix: dict = field(default_factory=dict)


@dataclass
class DRPlan:
    """完整的容灾切换计划。"""

    plan_id: str
    created_at: str
    scope: str
    source: str
    target: str
    #: ``drill``（计划内演练，优先零数据丢失操作）或 ``failover``（非计划，主区已失）。
    #: 数据层动作按此分流：例如 Aurora 演练走 switchover、真灾走 failover。
    mode: str = "drill"
    #: 容灾策略：``pilot_light`` 或 ``warm_standby``（来自 profile 的 dr.strategy）。
    strategy: str = ""
    #: 所声明策略在**数据层未满足**的前提。非空意味着实际档位低于标称档位，
    #: 必须在计划里显著呈现——把未达标包装成已达标的计划，演练可能侥幸通过，
    #: 真灾必然失守。
    data_layer_gaps: List[Dict[str, str]] = field(default_factory=list)
    #: 计算层未满足的前提。例如声明 pilot_light 但没配 ``dr.eks.nodegroups``——
    #: 那样生成的计划会 scale 一批永久 Pending 的 Pod，而命令本身返回成功。
    compute_layer_gaps: List[Dict[str, str]] = field(default_factory=list)
    #: 被排除在切换范围外的资源，每项含 ``reason``。审计要能看出某个资源是
    #: **有意排除**（平台自身设施、污染数据）而不是漏了。
    scope_exclusions: List[Dict[str, str]] = field(default_factory=list)
    #: 图数据来源：``neptune``（实时查）或 ``snapshot``（离线快照）。
    plan_source: str = "neptune"
    #: 快照年龄（秒）。离线生成时填，供审计判断计划依据的新鲜度。
    graph_snapshot_age_seconds: Optional[float] = None
    #: 快照是否已超过新鲜度阈值。陈旧不阻断生成，但必须在产物里留痕。
    graph_snapshot_stale: bool = False
    affected_services: List[str] = field(default_factory=list)
    #: **只在这一个 AZ 上有 pod 的服务** —— AZ 失守时它们整体不可用，
    #: 而 affected_services 里其余的只是降级（pod 还在别的 AZ 上跑着）。
    #:
    #: 这个区分不是修饰。2026-09-07 实测 petsite 有 96 个 pod 在 1a、160 个在
    #: 1c，1a 失守它是降级；trafficgenerator 只有 1 个 pod 且只在 1c，1c 失守
    #: 它就没了。把两者混在一份「受影响服务」清单里，等于让运维在
    #: 「7 个服务受影响」和「1 个服务彻底没了」之间自己猜。
    #:
    #: region 范围下**恒为空**：整个 region 失守时所有 pod 都在范围内，
    #: 「跨 AZ 所以只是降级」这个概念不成立，标了反而误导。
    fully_lost_services: List[str] = field(default_factory=list)
    #: 逐服务的 AZ pod 分布：``{服务名: {AZ 名: pod 数}}``。
    #: 上面那个判断的原始依据，留着让人能自己核对而不必信结论。
    service_az_pods: Dict[str, Dict[str, int]] = field(default_factory=dict)
    affected_resources: List[str] = field(default_factory=list)
    phases: List[DRPhase] = field(default_factory=list)
    rollback_phases: List[DRPhase] = field(default_factory=list)
    impact_assessment: Optional[ImpactReport] = None
    estimated_rto: int = 0            # minutes
    #: RPO（分钟）。**None 表示无法从配置推定**——此时必须实测或按备份间隔论证。
    #: 给不出数字比给错数字诚实，也更符合监管举证要求。原实现在这里硬编码
    #: RDS=5/DynamoDB=0/其他=15，样例里那个「15 分钟」就是编的。
    estimated_rpo: Optional[int] = 0
    #: RPO 的逐组件推导依据（component / topology / rpo / reason）。
    rpo_basis: List[Dict[str, str]] = field(default_factory=list)
    #: 无法从配置推定 RPO 的组件。
    rpo_unmeasurable: List[str] = field(default_factory=list)
    #: 取得真实 RPO 所需的实测命令。
    rpo_measurement_commands: List[Dict[str, str]] = field(default_factory=list)
    #: RTO 估算依据（哪些步骤用了实测值、confidence 等级）。
    rto_basis: Dict[str, Any] = field(default_factory=dict)
    validation_status: str = "pending"
    graph_snapshot_time: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "DRPlan":
        """从 JSON dict 反序列化 DRPlan（含嵌套 phases/steps/impact）。"""
        d = dict(d)
        phases = [DRPhase.from_dict(p) for p in d.pop("phases", [])]
        rollback_phases = [DRPhase.from_dict(p) for p in d.pop("rollback_phases", [])]
        impact_data = d.pop("impact_assessment", None)
        impact = ImpactReport(**impact_data) if impact_data else None
        return cls(**d, phases=phases, rollback_phases=rollback_phases, impact_assessment=impact)


@dataclass
class Issue:
    """计划验证发现的问题。"""

    severity: str    # CRITICAL / WARNING / INFO
    message: str


@dataclass
class ValidationReport:
    """计划验证报告。"""

    valid: bool
    issues: List[Issue] = field(default_factory=list)
