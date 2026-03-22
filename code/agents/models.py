"""
models.py — Hypothesis + Learning 数据模型

Phase 1: Hypothesis（假设生成）
Phase 2: LearningReport（闭环学习）
严格按照 improvement-plan.md 定义的字段。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


# ─── Phase 1: Hypothesis ─────────────────────────────────────────────────────

@dataclass
class Hypothesis:
    id: str                                # 唯一 ID，如 H001
    title: str                             # 简短标题
    description: str                       # 详细描述
    steady_state: str                      # 稳态假设（"成功率 >= 99%"）
    fault_scenario: str                    # 故障场景描述
    expected_impact: str                   # 预期影响
    failure_domain: str                    # compute | data | network | dependencies | resources
    target_services: list[str]             # 目标服务（逻辑名）
    target_resources: list[str]            # 目标资源类型
    backend: str                           # fis | chaosmesh
    priority: int = 999                    # 1 = 最高优先级
    priority_scores: dict = field(default_factory=dict)  # {business_impact, blast_radius, feasibility, learning_value}
    status: str = "draft"                  # draft | tested | validated | invalidated
    linked_experiments: list[str] = field(default_factory=list)
    generated_by: str = "hypothesis-agent-v1"
    generated_at: str = ""
    source_context: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Hypothesis:
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ─── Phase 2: Learning Report 辅助模型 ───────────────────────────────────────

@dataclass
class ServiceStats:
    """单个服务的实验统计"""
    service: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    aborted: int = 0
    pass_rate: float = 0.0
    avg_recovery_seconds: float = 0.0
    tested_domains: list[str] = field(default_factory=list)  # 已覆盖的故障域


@dataclass
class FailurePattern:
    """重复失败模式"""
    service: str
    fault_type: str
    failure_count: int
    last_failure: str          # ISO timestamp
    description: str = ""      # LLM 生成的模式描述


@dataclass
class CoverageGap:
    """未覆盖的故障域"""
    service: str
    missing_domains: list[str]  # 未测试的故障域
    suggestion: str = ""        # 建议


@dataclass
class Trend:
    """改善/恶化趋势"""
    service: str
    metric: str                 # recovery_time | pass_rate
    direction: str              # improving | degrading | stable
    values: list[float] = field(default_factory=list)  # 时间序列值
    description: str = ""


@dataclass
class Recommendation:
    """改进建议"""
    priority: int               # 1 = 最高
    category: str               # coverage | resilience | process
    title: str
    description: str
    target_services: list[str] = field(default_factory=list)


@dataclass
class GraphUpdate:
    """需要写回 Neptune 的更新"""
    service: str
    property_name: str          # resilience_score | weakness_pattern | last_tested_at | test_coverage
    value: str | float


@dataclass
class LearningReport:
    """Phase 2 闭环学习报告"""
    # 统计
    total_experiments: int = 0
    pass_rate: float = 0.0
    avg_recovery_seconds: float = 0.0

    # 按服务分析
    service_stats: dict[str, ServiceStats] = field(default_factory=dict)

    # 模式识别
    repeated_failures: list[FailurePattern] = field(default_factory=list)
    coverage_gaps: list[CoverageGap] = field(default_factory=list)
    improvement_trends: list[Trend] = field(default_factory=list)

    # 建议
    recommendations: list[Recommendation] = field(default_factory=list)
    new_hypotheses: list[Hypothesis] = field(default_factory=list)

    # 图谱更新
    graph_updates: list[GraphUpdate] = field(default_factory=list)

    # 元数据
    generated_at: str = ""
    analysis_window_days: int = 90

    def __post_init__(self):
        if not self.generated_at:
            self.generated_at = datetime.now(timezone.utc).isoformat()
