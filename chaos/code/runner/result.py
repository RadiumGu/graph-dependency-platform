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

    # ─── 观测方数据（T-210）：key = 观测方 service 名 ───────────────────────
    # 验证边 A -[X]-> B 必须在 B 注入、观测 A。注入目标自己的指标回答的是
    # 「打断 B 之后 B 是否退化」，近乎恒真，不构成任何边的证据。
    observer_steady_before: dict = field(default_factory=dict)     # svc -> MetricsSnapshot
    observer_snapshots: dict = field(default_factory=dict)         # svc -> [MetricsSnapshot]
    observer_min_success_rate: dict = field(default_factory=dict)  # svc -> float

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

    # ─── 观测方（T-210）——验证一条边必须看调用侧，不是注入目标自己 ─────────────

    def record_observer_baseline(self, service: str, snapshot: "MetricsSnapshot"):
        """记录某观测方的基线快照。"""
        self.observer_steady_before[service] = snapshot

    def record_observer_snapshot(self, service: str, snapshot: "MetricsSnapshot"):
        """记录某观测方在注入期的一个采样点，并维护其最低成功率。"""
        self.observer_snapshots.setdefault(service, []).append(snapshot)
        cur = self.observer_min_success_rate.get(service, 100.0)
        if snapshot.success_rate is not None and snapshot.success_rate < cur:
            self.observer_min_success_rate[service] = snapshot.success_rate

    def observer_degradation_rate(self, service: str) -> Optional[float]:
        """
        观测方的退化幅度（百分点）。这才是一条边的证据 ——
        「在 B 注入后，调用方 A 是否退化」。

        返回 None 表示**无法判定**（缺基线或缺注入期采样），
        调用方必须据此判 inconclusive，绝不可当成 0（那等于判「边不存在」）。
        """
        base = self.observer_steady_before.get(service)
        if base is None or base.success_rate is None:
            return None
        if service not in self.observer_min_success_rate:
            return None
        return max(0.0, round(base.success_rate - self.observer_min_success_rate[service], 2))

    def observer_has_real_traffic(self, service: str, min_requests: int = 10) -> bool:
        """
        观测方在基线期是否有足够流量。

        这是假阴性防线：`metrics.collect()` 无数据时 fallback
        `success_rate=100.0 / total_requests=0` —— **零流量和健康在指标上完全一样**。
        不设下限，一条没流量的边会被判成「不存在」。
        """
        base = self.observer_steady_before.get(service)
        if base is None:
            return False
        return (base.total_requests or 0) >= min_requests

    def observer_evidence(self) -> dict:
        """
        汇总每个观测方的证据，供 edge_verification 判定。
        `usable=False` 的条目只能得到 inconclusive。
        """
        out = {}
        for svc, base in self.observer_steady_before.items():
            deg = self.observer_degradation_rate(svc)
            out[svc] = {
                "baseline_success_rate": base.success_rate,
                "baseline_total_requests": base.total_requests,
                "min_success_rate": self.observer_min_success_rate.get(svc),
                "degradation_rate": deg,
                "samples": len(self.observer_snapshots.get(svc, [])),
                "usable": bool(
                    self.observer_has_real_traffic(svc)
                    and deg is not None
                    and len(self.observer_snapshots.get(svc, [])) > 0
                ),
            }
        return out

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
