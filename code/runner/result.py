"""
result.py - ExperimentResult 数据类（runner 内部状态）
"""
from __future__ import annotations
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .experiment import Experiment, MetricsSnapshot
    from .rca import RCAResult


@dataclass
class ExperimentResult:
    experiment: "Experiment"

    # 状态
    status: str = "RUNNING"          # RUNNING / PASSED / FAILED / ABORTED / ERROR
    abort_reason: str = ""

    # 时间
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    inject_time: Optional[datetime] = None     # Phase2 注入完成时间

    # 稳态快照
    steady_state_before: Optional["MetricsSnapshot"] = None
    steady_state_after: Optional["MetricsSnapshot"] = None

    # 观测快照序列（Phase3）
    snapshots: list = field(default_factory=list)

    # 聚合指标
    min_success_rate: float = 100.0
    max_latency_p99: float = 0.0
    recovery_seconds: Optional[float] = None

    # RCA
    rca_result: Optional["RCAResult"] = None
    rca_match: Optional[bool] = None

    # Phase5 稳态验证结果
    steady_state_after_checks: list = field(default_factory=list)

    # 输出
    report_path: str = ""
    chaos_experiment_name: str = ""   # Chaos Mesh 实验名 或 FIS experiment ID
    fis_template_id: str = ""         # FIS 实验模板 ID（用于清理）

    # experiment_id 在 __post_init__ 中固定生成，避免多次访问时因时钟偏移产生不同值
    _experiment_id: str = field(default="", init=False, repr=False)

    def __post_init__(self):
        ts = (self.start_time or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
        svc = self.experiment.target_service
        ft  = self.experiment.fault.type.replace("_", "-")
        self._experiment_id = f"exp-{svc}-{ft}-{ts}"

    @property
    def experiment_id(self) -> str:
        return self._experiment_id

    @property
    def duration_seconds(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0.0

    def record_snapshot(self, snapshot: "MetricsSnapshot"):
        self.snapshots.append(snapshot)
        if snapshot.success_rate < self.min_success_rate:
            self.min_success_rate = snapshot.success_rate
        if snapshot.latency_p99_ms > self.max_latency_p99:
            self.max_latency_p99 = snapshot.latency_p99_ms

    def elapsed_since_injection(self) -> float:
        if self.inject_time is None:
            return 0.0
        return time.time() - self.inject_time.timestamp()

    def degradation_rate(self) -> float:
        """
        注入前 vs 实验期间最低成功率的下降幅度（%）
        """
        if self.steady_state_before is None:
            return 0.0
        baseline = self.steady_state_before.success_rate
        return max(0.0, round(baseline - self.min_success_rate, 2))
