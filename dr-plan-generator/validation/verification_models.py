"""
validation/verification_models.py — DR Plan 验证结果数据模型

Covers all three verification levels:
  Level 1: Dry-Run (DryRunReport)
  Level 2: Step-by-Step (StepVerificationResult, PhaseResult)
  Level 3: Full Rehearsal (RehearsalReport)

Also defines VerificationScope and ExecutionContext for cross-region support.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# =====================================================
# Enums
# =====================================================


class VerificationLevel(str, Enum):
    DRY_RUN = "dry_run"
    STEP_BY_STEP = "step_by_step"
    FULL_REHEARSAL = "full_rehearsal"


class StepStrategy(str, Enum):
    ISOLATED = "isolated"
    CUMULATIVE = "cumulative"
    CHECKPOINT = "checkpoint"


class CheckStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


class GateType(str, Enum):
    HARD_BLOCK = "hard_block"
    SOFT_WARN = "soft_warn"
    INFO = "info"


# =====================================================
# Execution Context (cross-region support)
# =====================================================


@dataclass
class ExecutionContext:
    """Single Region/AZ execution context."""

    aws_region: str
    aws_profile: str = ""
    k8s_context: str = ""
    chaos_backend: str = "both"  # "chaos-mesh" | "fis" | "both"
    neptune_endpoint: str = ""
    role_arn: str = ""


# =====================================================
# Verification Scope
# =====================================================


@dataclass
class VerificationScope:
    """DR verification scope and execution settings."""

    # Inherited from DR Plan
    plan_scope: str  # "az" | "region" | "service"
    source: str
    target: str

    # Verification-specific
    environment: str = "staging"  # "production" | "staging" | "hybrid"
    level: VerificationLevel = VerificationLevel.DRY_RUN

    # Execution contexts (multi-region: key = region/az identifier)
    execution_contexts: Dict[str, ExecutionContext] = field(default_factory=dict)

    # Safety
    require_approval: bool = False
    auto_rollback: bool = True
    step_strategy: StepStrategy = StepStrategy.CHECKPOINT
    max_blast_radius: str = "single_az"  # "single_az" | "single_region" | "multi_region"

    # Timeouts
    phase_timeout_minutes: int = 30
    total_timeout_minutes: int = 120


# =====================================================
# Level 1: Dry-Run Models
# =====================================================


@dataclass
class DryRunCheck:
    """Single dry-run check result."""

    category: str  # "variable", "resource", "permission", "state", "network", "freshness", "context"
    name: str
    description: str
    status: CheckStatus = CheckStatus.PASS
    details: str = ""
    severity: str = "critical"  # "critical" | "warning" | "info"


@dataclass
class DryRunReport:
    """Level 1 dry-run report."""

    plan_id: str
    scope: str
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    checks: List[DryRunCheck] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """All CRITICAL checks passed?"""
        return all(
            c.status in (CheckStatus.PASS, CheckStatus.WARN, CheckStatus.SKIP)
            for c in self.checks
            if c.severity == "critical"
        )

    @property
    def warnings(self) -> List[str]:
        return [c.description for c in self.checks if c.status == CheckStatus.WARN]

    @property
    def blockers(self) -> List[str]:
        return [
            c.description
            for c in self.checks
            if c.status == CheckStatus.FAIL and c.severity == "critical"
        ]

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.PASS)

    @property
    def fail_count(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.FAIL)

    @property
    def warn_count(self) -> int:
        return sum(1 for c in self.checks if c.status == CheckStatus.WARN)


# =====================================================
# Level 2/3: Step & Phase Results
# =====================================================


@dataclass
class StepVerificationResult:
    """Single step verification result (Level 2/3)."""

    step_id: str
    phase_id: str
    resource_name: str

    # Execution
    command_success: bool = False
    command_output: str = ""
    command_exit_code: int = -1

    # Validation
    validation_success: bool = False
    validation_output: str = ""

    # Rollback (Level 2 isolated mode only)
    rollback_success: Optional[bool] = None
    rollback_output: Optional[str] = None

    # Timing
    actual_duration_seconds: float = 0.0
    estimated_duration_seconds: int = 60

    @property
    def rto_accuracy(self) -> float:
        """actual / estimated (1.0 = perfect)."""
        if self.estimated_duration_seconds <= 0:
            return 0.0
        return self.actual_duration_seconds / self.estimated_duration_seconds

    # Issues
    issues: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.command_success and self.validation_success


@dataclass
class GateCondition:
    """Phase gate condition definition."""

    type: GateType = GateType.SOFT_WARN
    require_all: bool = True
    auto_retry: int = 0
    retry_interval_seconds: int = 30
    timeout_minutes: int = 30
    on_timeout: str = "abort_and_hold"  # "abort_and_hold" | "abort_and_rollback" | "continue"
    on_failure: str = "diagnose"  # "diagnose" | "rollback" | "skip"


@dataclass
class PhaseResult:
    """Phase-level verification result."""

    phase_id: str
    phase_name: str

    steps: List[StepVerificationResult] = field(default_factory=list)
    gate_check_passed: bool = False
    gate_condition: Optional[GateCondition] = None

    total_duration_seconds: float = 0.0
    estimated_duration_seconds: int = 0

    @property
    def all_steps_passed(self) -> bool:
        return all(s.passed for s in self.steps)

    @property
    def failed_steps(self) -> List[str]:
        return [s.step_id for s in self.steps if not s.passed]


# =====================================================
# Level 3: Full Rehearsal Report
# =====================================================


@dataclass
class RehearsalReport:
    """Level 3 full rehearsal report."""

    plan_id: str
    scope: str  # "az" | "region" | "service"
    environment: str  # "production" | "staging"
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat())

    # Timing
    total_duration_seconds: int = 0
    actual_rto_minutes: int = 0
    estimated_rto_minutes: int = 0

    @property
    def rto_accuracy(self) -> float:
        if self.estimated_rto_minutes <= 0:
            return 0.0
        return self.actual_rto_minutes / self.estimated_rto_minutes

    # Results
    phase_results: List[PhaseResult] = field(default_factory=list)
    step_results: List[StepVerificationResult] = field(default_factory=list)

    # Rollback
    rollback_success: bool = False
    rollback_duration_seconds: int = 0

    # Summary
    failed_steps: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    plan_adjustments: List[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return all(pr.all_steps_passed for pr in self.phase_results)


# =====================================================
# Custom Rule Models (for Markdown NL rules)
# =====================================================


@dataclass
class TimeWindow:
    """Time-based constraint."""

    timezone: str = "UTC"
    blocked_windows: List[Dict[str, str]] = field(default_factory=list)
    # Each entry: {"start": "09:00", "end": "11:30"}


@dataclass
class RuleScope:
    """Rule applicability scope."""

    phase_ids: Optional[List[str]] = None
    resource_types: Optional[List[str]] = None
    resource_names: Optional[List[str]] = None
    scope_types: Optional[List[str]] = None  # "az" | "region" | "service"
    time_window: Optional[TimeWindow] = None


@dataclass
class RuleCondition:
    """Rule check condition."""

    type: str  # "metric" | "state" | "time" | "approval" | "custom"
    check: str  # Check expression
    threshold: Optional[Any] = None
    command: Optional[str] = None  # AWS CLI / kubectl check command


@dataclass
class CustomRule:
    """Structured rule parsed from natural language."""

    id: str
    source_text: str  # Original NL text
    category: str  # From ### heading

    trigger: str  # "before_step" | "after_step" | "before_phase" | "after_phase" | "before_plan" | "schedule"
    scope: RuleScope = field(default_factory=RuleScope)
    condition: RuleCondition = field(default_factory=lambda: RuleCondition(type="custom", check=""))
    action: str = "warn"  # "block" | "warn" | "require_approval" | "skip"
    action_message: str = ""
    check_command: Optional[str] = None

    confidence: float = 0.0
    human_verified: bool = False


@dataclass
class RuleResult:
    """Result of evaluating a single custom rule."""

    rule_id: str
    passed: bool
    action: str = "warn"
    message: str = ""
    details: str = ""
