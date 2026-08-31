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
    # 注入期最低请求量。abort 类故障不产生 response 行，成功率对它是盲的 ——
    # 实测 http_chaos abort 下注入目标成功率全程 100%，但请求量 2240 -> 56（-97%）。
    observer_min_requests: dict = field(default_factory=dict)      # svc -> int

    # ─── 注入目标 Pod 健康（T-214h）───────────────────────────────────────────
    # 为什么记基线而不是只在 Phase 5 查一次：`abort` 注入打断 liveness 探针 →
    # 容器被 kubelet 重启 → 而 tproxy 残留在 **Pod netns**（属 sandbox 不属容器），
    # 重启清不掉 → Pod 持续 1/2 Ready。实测这个退化在 Phase 4 报 `2/2 running`
    # 之后**还要 2.5 分钟**才显形，所以点时刻的 running/total 会漏判。
    # restartCount 没有滞后（重启就发生在注入期间），故判据是**差值**。
    target_pods_before: dict = field(default_factory=dict)   # Phase 0 的 check_pods 快照
    target_pods_after: dict = field(default_factory=dict)    # Phase 5 的 check_pods 快照
    pod_damage: list = field(default_factory=list)           # 人可读的损伤描述，进报告

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
        """记录某观测方在注入期的一个采样点，并维护其最低成功率与最低请求量。

        ⚠️ 首个采样点必须无条件初始化 min（2026-08-31 实测 bug）：
        原实现写成 `cur = get(service, 100.0)` 再 `if rate < cur`，
        观测方**全程保持 100.0** 时 min 永远不被写入 → 退化率返回 None
        → usable=False → 拿不到任何判定。健康的观测方应得 **0.0 退化**，
        而不是「判不了」——后者是给「完全没有采样点」保留的语义。

        ⚠️ 采集失败的采样点（`ok=False`）**只入列表、不参与 min**（2026-08-31 二次实测）：
        `metrics.collect()` 查询异常时 fallback 成 (100%, 0 requests)。
        把它算进 `observer_min_requests`，一次 ClickHouse 抖动就让谷值变 0，
        实测把 petsite 的 322 → 0 算成「吞吐塌陷 100%」，合成退化率 100pp，
        足以把一条边**误判成 confirmed**。宁可少一个采样点，不可伪造一个谷值。
        """
        self.observer_snapshots.setdefault(service, []).append(snapshot)
        if not getattr(snapshot, 'ok', True):
            return
        if snapshot.success_rate is not None:
            cur = self.observer_min_success_rate.get(service)
            if cur is None or snapshot.success_rate < cur:
                self.observer_min_success_rate[service] = snapshot.success_rate
        # 吞吐量最低值：abort 类故障不产生 response 行，成功率看不见它，
        # 唯一可见信号是请求量塌陷（实测注入目标 2240 -> 56，-97%）。
        req = snapshot.total_requests or 0
        cur_r = self.observer_min_requests.get(service)
        if cur_r is None or req < cur_r:
            self.observer_min_requests[service] = req

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

    def observer_throughput_drop_pct(self, service: str) -> Optional[float]:
        """
        观测方请求量塌陷幅度（百分比）。

        为什么必需：`http_chaos action: abort` 直接短路连接，被中断的请求
        **根本不产生 response 行**，而 SLI 查询带 `response_duration > 0`，
        所以成功率对它完全是盲的。2026-08-31 实测：注入目标成功率全程 100%，
        但请求量 2240 -> 56（-97%）。只看成功率会把生效的注入判成「没影响」。

        ⚠️ 用**中位数**而不是最小值（2026-08-31 15:56 实测缺陷）：

        原实现拿「注入期 18 个采样的 min」比「基线的单个采样」——两侧不同量纲。
        突发流量下一个低谷采样就能伪造出塌陷。实测铁证（S3 注入实验）：

            实验报告        基线请求 2407 -> 谷值 819，吞吐塌陷 65.97%，判 confirmed
            同窗口聚合复核  search-service 3 分钟总量 7796 -> **11630（涨 49%）**
                            list-adoptions / petsite / pay-for-adoption 同样上涨

        也就是说整体流量根本没降，只是有个别 60s 窗口低。据此判 confirmed 是错的。

        中位数对单点低谷不敏感，又保留了「持续性塌陷」的检出能力（真塌陷时
        多数采样都低）。仍只统计 `ok=True` 的采样点，见 record_observer_snapshot。
        """
        base = self.observer_steady_before.get(service)
        if base is None or not (base.total_requests or 0):
            return None
        vals = sorted(s.total_requests or 0
                      for s in self.observer_snapshots.get(service, [])
                      if getattr(s, 'ok', True))
        if not vals:
            return None
        n = len(vals)
        median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        b = base.total_requests
        return max(0.0, round((b - median) / b * 100.0, 2))

    def observer_effective_degradation(self, service: str) -> Optional[float]:
        """
        供判定使用的**合成**退化率：成功率下降与吞吐塌陷取较大者。

        两个信号覆盖互补的故障形态：
          - `delay` / 5xx 类 → 成功率下降可见，吞吐基本不变
          - `abort` / 连接层类 → 成功率盲（无 response 行），吞吐塌陷可见
        取 max 而不是相加：两者度量的是同一件事「调用方受了多大影响」，
        相加会重复计数。
        """
        d1 = self.observer_degradation_rate(service)
        d2 = self.observer_throughput_drop_pct(service)
        vals = [v for v in (d1, d2) if v is not None]
        return max(vals) if vals else None

    # 纯吞吐证据的判定下限。两条通道的证据强度**不对等**：
    #   成功率下降  = 观测方**自己**返回了失败 —— 归因明确
    #   吞吐塌陷    = 观测方的请求量少了 —— 有三种成因，单靠它分不清：
    #                 ① 它自己失败到不产生 response 行（真依赖）
    #                 ② 它的上游不再调它（传导，非它自己的依赖）
    #                 ③ 测量管道本身受影响
    # 2026-08-31 实测踩到 ②：断 DynamoDB 后 pay-for-adoption 成功率退化 0.00pp、
    # 吞吐塌陷 100%，但它的入流量来自 petsite，而 petsite 因 petsearch 失败
    # 已经不再提交领养 —— 「它不再被调用」被当成了「它依赖 DynamoDB」。
    THROUGHPUT_ONLY_CONFIRM_PCT = 60.0

    def observer_evidence_channel(self, service: str) -> str:
        """本次证据来自哪条通道：'success_rate' / 'throughput_only' / 'both' / 'none'。

        判定层据此决定证据强度 —— 纯吞吐证据不足以单独判 confirmed。
        """
        d1 = self.observer_degradation_rate(service)
        d2 = self.observer_throughput_drop_pct(service)
        sr = d1 is not None and d1 >= 5.0        # 成功率通道有实质信号
        tp = d2 is not None and d2 >= 5.0
        if sr and tp:
            return 'both'
        if sr:
            return 'success_rate'
        if tp:
            return 'throughput_only'
        return 'none'

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
            thr = self.observer_throughput_drop_pct(svc)
            eff = self.observer_effective_degradation(svc)
            out[svc] = {
                "baseline_success_rate": base.success_rate,
                "baseline_total_requests": base.total_requests,
                "min_success_rate": self.observer_min_success_rate.get(svc),
                "min_requests": self.observer_min_requests.get(svc),
                "degradation_rate": deg,            # 成功率下降
                "throughput_drop_pct": thr,         # 吞吐塌陷（abort 类唯一可见信号）
                "effective_degradation": eff,       # 两者取 max，供判定使用
                "samples": len(self.observer_snapshots.get(svc, [])),
                "usable": bool(
                    self.observer_has_real_traffic(svc)
                    and eff is not None
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
