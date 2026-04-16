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

    def has_real_metrics(self) -> bool:
        """
        判断是否有真实流量数据（DeepFlow 可达且有请求）。
        total_requests=0 且 success_rate=100.0 是 DeepFlow 不可达时的 fallback 值，
        此时指标不可信，不应据此判断依赖关系。
        """
        snap = self.steady_state_before
        if snap is None:
            return False
        return snap.total_requests > 0

    def is_conclusive(self) -> bool:
        """
        判断实验结果是否有充分数据支撑。
        以下任一情况视为 inconclusive：
        1. steady_state_after 为空或指标全 null（Phase 5 没采集到数据）
        2. 观测快照为 0（Phase 3 没有有效观测点）
        3. steady_state_before 无真实流量
        """
        after = self.steady_state_after
        if after is None:
            return False
        if after.success_rate is None and after.latency_p99_ms is None:
            return False
        if after.total_requests == 0 and after.success_rate == 100.0:
            return False  # DeepFlow fallback 值
        if len(self.snapshots) == 0:
            return False
        if not self.has_real_metrics():
            return False
        return True

    @property
    def data_quality(self) -> str:
        """
        数据质量分级：
        - complete: 所有阶段数据完整
        - partial: 有数据但部分缺失（观测点少或恢复时间可疑）
        - inconclusive: 关键数据缺失，结论不可信
        """
        if not self.is_conclusive():
            return "inconclusive"
        if (self.recovery_seconds is not None
                and self.recovery_seconds <= 1.0
                and len(self.snapshots) < 3):
            return "partial"
        if len(self.snapshots) < 3:
            return "partial"
        return "complete"
