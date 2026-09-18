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
    # 退化极小 —— 仍走 refuted 分支。
    # 注意必须带 injection_confirmed=True：2026-08-31 16:30 新增了**注入生效门禁**
    # （见 test_e20），未确认注入生效时该分支返回 inconclusive。
    # 本用例守的是「通道分级」不影响 refuted 判据，所以这里显式确认生效，
    # 把生效门禁这个变量固定住 —— 否则两条护栏混在一个用例里，失败时分不清是谁的锅。
    status, _ = classify_intervention(100, 100, 1.0, evidence_channel='success_rate',
                                     injection_confirmed=True)
    assert status == STATUS_REFUTED


def test_e15_default_channel_keeps_backward_compat():
    """不传 evidence_channel 时行为与改动前一致（默认 both）。"""
    status, _ = classify_intervention(100, 100, 25.0)
    assert status == STATUS_CONFIRMED


# ── 注入生效门禁（T-297）────────────────────────────────────────────────────

def test_e20_refuted_requires_confirmed_injection():
    """观测方没退化 + 注入生效性未知 → 判 inconclusive，**不得**判 refuted。

    实测背景：`petsearch -[AccessesData]-> s3` 被 FIS
    `disrupt-connectivity scope=s3` 判 refuted（退化 1.21%）。
    但拿 X-Ray 严格按故障窗口复核，PetSearch 在两次窗口内各做了 10 次与 13 次
    **成功**的 S3 调用、0 错误 —— NACL 没切断这条路径。
    而这条边有两个独立源的硬证据（X-Ray 24h 17,190 次调用、NFM 50 条流 1.9MB），
    判 refuted 等于凭空证伪一条真实依赖。
    """
    # 默认 None（未知）
    status, reason = classify_intervention(100, 100, 1.2)
    assert status == STATUS_INCONCLUSIVE
    assert '注入生效性未知' in reason
    # 明确「注入未生效」也不判 refuted
    status, reason = classify_intervention(100, 100, 1.2, injection_confirmed=False)
    assert status == STATUS_INCONCLUSIVE
    assert '已确认注入未生效' in reason


def test_e21_refuted_allowed_when_injection_confirmed():
    """确认注入生效后，观测方仍无退化 → 这才是真 refuted。"""
    status, reason = classify_intervention(100, 100, 1.2, injection_confirmed=True)
    assert status == STATUS_REFUTED
    assert '已确认注入生效' in reason


def test_e22_gate_does_not_affect_confirmed_or_inconclusive_bands():
    """生效门禁只作用在 refuted 分支，不得影响 confirmed 与中间带。"""
    # confirmed 不需要 injection_confirmed —— 影响传导本身就是注入生效的证据
    status, _ = classify_intervention(100, 100, 30.0)
    assert status == STATUS_CONFIRMED
    # 中间带仍是 inconclusive，理由应是中间带而不是生效门禁
    status, reason = classify_intervention(100, 100, 12.0)
    assert status == STATUS_INCONCLUSIVE
    assert '中间带' in reason


def test_e23_runner_effect_detection_returns_none_without_target_traffic():
    """注入目标没有可用流量基线时（AWS 托管资源目标）必须返回 None 而非 False。

    返回 False 会被判定层当成「已确认未生效」，语义上更强；
    而真实情况是「测不了」。两者都阻止 refuted，但理由必须准确。
    """
    from runner.runner import ExperimentRunner
    from runner.result import ExperimentResult
    from runner.experiment import Experiment, FaultSpec

    exp = Experiment(
        name="s", description="", target_service="dynamodb", target_namespace="petadoptions",
        target_tier="Tier0",
        fault=FaultSpec(type="fis_network_disrupt", mode="all", value="", duration="3m"),
        steady_state_before=[], steady_state_after=[], stop_conditions=[],
        observation_targets=[])
    r = ExperimentResult(experiment=exp)
    # 基线 total_requests=0 —— AWS 托管资源目标在 DeepFlow 里查不到
    r.steady_state_before = MetricsSnapshot(
        timestamp=0, success_rate=100.0, latency_p99_ms=0.0, total_requests=0)
    r.snapshots = [MetricsSnapshot(timestamp=1, success_rate=100.0,
                                   latency_p99_ms=0.0, total_requests=0)]
    runner = ExperimentRunner.__new__(ExperimentRunner)
    assert runner._injection_took_effect(exp, r) is None


def test_e24_runner_effect_detection_confirms_on_target_degradation():
    """注入目标自己退化了 → 确认生效。门槛刻意低（5%），回答是非问题。"""
    from runner.runner import ExperimentRunner
    from runner.result import ExperimentResult
    from runner.experiment import Experiment, FaultSpec

    exp = Experiment(
        name="s", description="", target_service="search-service",
        target_namespace="petadoptions", target_tier="Tier1",
        fault=FaultSpec(type="http_chaos", mode="all", value="", duration="3m"),
        steady_state_before=[], steady_state_after=[], stop_conditions=[],
        observation_targets=[])
    r = ExperimentResult(experiment=exp)
    r.steady_state_before = MetricsSnapshot(
        timestamp=0, success_rate=100.0, latency_p99_ms=1.0, total_requests=1000)
    # 吞吐掉到 100（-90%），成功率不动 —— abort 类的典型形态
    r.snapshots = [MetricsSnapshot(timestamp=i, success_rate=100.0,
                                   latency_p99_ms=1.0, total_requests=100)
                   for i in range(1, 6)]
    runner = ExperimentRunner.__new__(ExperimentRunner)
    assert runner._injection_took_effect(exp, r) is True
