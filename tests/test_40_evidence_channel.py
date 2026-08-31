"""test_40_evidence_channel.py —— 证据通道分级与吞吐塌陷的中位数判据。

## 这组测试守什么

2026-08-31 15:43–15:56 用 FIS 断子网到 DynamoDB / S3 的连通性验证
AWSServiceEndpoint 边时，连续踩到两个让判定失真的缺陷：

### 缺陷 A：min 与基线不同量纲，抖动伪造塌陷

原实现拿「注入期 18 个采样的 **min**」比「基线的**单个**采样」。
S3 实验实测：

    实验报告        基线 2407 -> 谷值 819，吞吐塌陷 65.97%，判 **confirmed**
    同窗口聚合复核  search-service 3 分钟总量 7796 -> **11630（涨 49%）**
                    list-adoptions / petsite / pay-for-adoption 同样上涨

整体流量根本没降，只是个别 60s 窗口低。据此判 confirmed 是错的。
改用**中位数**：对单点低谷不敏感，真塌陷时多数采样都低仍能检出。

### 缺陷 B：吞吐通道无法归因

两条通道的证据强度不对等：
  · 成功率下降 = 观测方**自己**返回了失败 —— 归因明确
  · 吞吐塌陷   = 观测方的请求量少了 —— 三种成因分不清：
                 ① 它自己失败到不产生 response 行（真依赖）
                 ② 它的上游不再调它（传导，不是它自己的依赖）
                 ③ 测量管道本身受影响

实测踩到 ②：断 DynamoDB 后 `pay-for-adoption` 成功率退化 **0.00pp**、
吞吐塌陷 100%，判成 confirmed。但它的入流量来自 petsite，而 petsite 因
petsearch 失败已不再提交领养 —— 「它不再被调用」被当成了「它依赖 DynamoDB」。

修法：纯吞吐证据判 confirmed 的门槛显著抬高，达不到判 inconclusive。
方向与「零流量不判 refuted」一致：**宁可判不了，不可判错。**

两条判定已按 DoD-10 从活图谱撤销回 untested。
"""
from __future__ import annotations

import sys
import pathlib

import pytest

from paths import PROJECT_ROOT  # noqa: F401

_LAYER = str(pathlib.Path(PROJECT_ROOT) / 'infra' / 'lambda' / 'shared' / 'python')
if _LAYER not in sys.path:
    sys.path.insert(0, _LAYER)

from graph_confidence import (  # noqa: E402
    STATUS_CONFIRMED, STATUS_INCONCLUSIVE, STATUS_REFUTED, classify_intervention,
)
from runner.experiment import MetricsSnapshot  # noqa: E402


def _result(baseline_requests: int, samples: list[tuple[float, int]]):
    """samples: [(success_rate, total_requests), ...]，全部 ok=True。"""
    from runner.experiment import (
        Experiment, FaultSpec, ObservationTarget,
    )
    from runner.result import ExperimentResult
    exp = Experiment(
        name="stub", description="", target_service="t", target_namespace="ns",
        target_tier="Tier1",
        fault=FaultSpec(type="http_chaos", mode="all", value="", duration="1m"),
        steady_state_before=[], steady_state_after=[], stop_conditions=[],
        observation_targets=[ObservationTarget.parse(
            {"service": "obs", "namespace": "ns", "edge_label": "Calls"})],
    )
    r = ExperimentResult(experiment=exp)
    r.record_observer_baseline('obs', MetricsSnapshot(
        timestamp=0, success_rate=100.0, latency_p99_ms=1.0,
        total_requests=baseline_requests))
    for i, (sr, tot) in enumerate(samples, start=1):
        r.record_observer_snapshot('obs', MetricsSnapshot(
            timestamp=i, success_rate=sr, latency_p99_ms=1.0, total_requests=tot))
    return r


# ── 缺陷 A：中位数替代 min ──────────────────────────────────────────────────

def test_e01_single_dip_does_not_fake_a_collapse():
    """一个低谷采样不得伪造出吞吐塌陷 —— 这正是 S3 实验的失真场景。

    基线 2407，18 个采样里 17 个在基线附近、1 个跌到 819。
    min 判据会报 65.97% 塌陷；中位数判据应接近 0。
    """
    samples = [(100.0, 2400)] * 17 + [(100.0, 819)]
    r = _result(2407, samples)
    drop = r.observer_throughput_drop_pct('obs')
    assert drop is not None
    assert drop < 5.0, f"单点低谷不应产生塌陷，实得 {drop}%"


def test_e02_sustained_collapse_is_still_detected():
    """真塌陷（多数采样都低）仍必须检出 —— 修 A 不能把检出能力一起修掉。"""
    samples = [(100.0, 60)] * 15 + [(100.0, 2400)] * 3
    r = _result(2400, samples)
    drop = r.observer_throughput_drop_pct('obs')
    assert drop is not None and drop >= 90.0, f"持续塌陷应被检出，实得 {drop}%"


def test_e03_failed_samples_still_excluded():
    """缺陷 #8 的守门不能被本次改动破坏：ok=False 的采样点不参与统计。"""
    from runner.experiment import MetricsSnapshot as MS
    r = _result(1000, [(100.0, 990)] * 5)
    for i in range(5):
        r.record_observer_snapshot('obs', MS(
            timestamp=100 + i, success_rate=100.0, latency_p99_ms=0.0,
            total_requests=0, ok=False))
    drop = r.observer_throughput_drop_pct('obs')
    assert drop is not None and drop < 5.0, f"采集失败点不得压低中位数，实得 {drop}%"


# ── 缺陷 B：证据通道分级 ────────────────────────────────────────────────────

def test_e10_channel_classification():
    # 成功率与吞吐都有信号
    r = _result(1000, [(40.0, 300)] * 5)
    assert r.observer_evidence_channel('obs') == 'both'
    # 只有成功率下降，吞吐不变（delay / 5xx 类故障的典型形态）
    r = _result(1000, [(40.0, 1000)] * 5)
    assert r.observer_evidence_channel('obs') == 'success_rate'
    # 只有吞吐塌陷，成功率不动（abort 类，或上游不再调它）
    r = _result(1000, [(100.0, 100)] * 5)
    assert r.observer_evidence_channel('obs') == 'throughput_only'
    # 两条都没信号
    r = _result(1000, [(100.0, 1000)] * 5)
    assert r.observer_evidence_channel('obs') == 'none'


def test_e11_throughput_only_below_high_bar_is_inconclusive():
    """纯吞吐证据、退化率没到高门槛 —— 判 inconclusive 而不是 confirmed。

    这正是 pay-for-adoption -> dynamodb 那条被错判的形态的一般化：
    成功率 0.00pp、只有吞吐掉。
    """
    status, reason = classify_intervention(
        100, 100, 36.0, evidence_channel='throughput_only',
        throughput_only_confirm_pct=60.0)
    assert status == STATUS_INCONCLUSIVE
    assert '吞吐塌陷' in reason
    # 理由必须点明为什么不算数，否则读的人无法复核
    assert '上游不再调它' in reason


def test_e12_throughput_only_above_high_bar_still_confirms():
    """纯吞吐但退化极深（>=60%）仍判 confirmed —— 否则 abort 类故障全判不了。

    abort 不产生 response 行，成功率通道**结构性**为盲，这是缺陷 #4 的既有结论，
    不能被 B 的修法一起否掉。
    """
    status, reason = classify_intervention(
        100, 100, 74.9, evidence_channel='throughput_only',
        throughput_only_confirm_pct=60.0)
    assert status == STATUS_CONFIRMED
    assert '仅吞吐通道' in reason, "判定理由必须标明证据只来自吞吐通道"


def test_e13_success_rate_channel_confirms_at_normal_bar():
    """成功率通道有信号时按原门槛（20%）判 confirmed，不受高门槛影响。"""
    for ch in ('success_rate', 'both'):
        status, reason = classify_intervention(100, 100, 25.0, evidence_channel=ch)
        assert status == STATUS_CONFIRMED, ch
        assert '通道' in reason


def test_e14_channel_does_not_affect_refute_or_low_traffic_gates():
    """通道分级不得改变既有的两条护栏：流量下限与 refuted 门槛。"""
    # 流量不足 —— 无论哪个通道都判 inconclusive
    status, _ = classify_intervention(5, 5, 90.0, evidence_channel='both')
    assert status == STATUS_INCONCLUSIVE
    # 退化极小 —— 仍判 refuted
    status, _ = classify_intervention(100, 100, 1.0, evidence_channel='success_rate')
    assert status == STATUS_REFUTED


def test_e15_default_channel_keeps_backward_compat():
    """不传 evidence_channel 时行为与改动前一致（默认 both）。"""
    status, _ = classify_intervention(100, 100, 25.0)
    assert status == STATUS_CONFIRMED
