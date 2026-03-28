"""
models.py — DR Plan Generator 数据模型定义

All core dataclasses for the DR planning system.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Dict, List, Optional


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
    estimated_rpo_minutes: int = 0
    risk_matrix: dict = field(default_factory=dict)


@dataclass
class DRPlan:
    """完整的容灾切换计划。"""

    plan_id: str
    created_at: str
    scope: str
    source: str
    target: str
    affected_services: List[str] = field(default_factory=list)
    affected_resources: List[str] = field(default_factory=list)
    phases: List[DRPhase] = field(default_factory=list)
    rollback_phases: List[DRPhase] = field(default_factory=list)
    impact_assessment: Optional[ImpactReport] = None
    estimated_rto: int = 0            # minutes
    estimated_rpo: int = 0            # minutes
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
