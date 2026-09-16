"""稀释归一化的守门测试 —— 把聚合退化换算成「这条路径自己退化了多少」。

## 要解决的问题（2026-09-05 实测）

观测方 SLI 是**跨全部端点聚合**的。一条只被部分请求走到的依赖，即便**完全失效**，
也只能把聚合退化推到它自己的流量占比那么高 —— 拿这个数字去比固定的
`confirm_degradation_pct=20%` 是**比错了对象**。

实测（petsite，300s 窗口，入向 67,933 次）：

    petsite -> pethistory         7,685   占 11.31%   <- 稀释上限
    petsite -> list-adoptions     8,645   占 12.73%
    petsite -> petfood           16,611   占 24.45%
    petsite -> search-service    25,042   占 36.86%

打断 pethistory 观测到退化 **10.1pp**，落在中间带（5–20%）判不了。
但 10.1 / 11.89 = **84.6%** —— 走这条路径的请求近 85% 失败了，是强依赖。
归一化后该边从 inconclusive 翻成 **confirmed（置信度 1.000）**。

## 三重保守约束 + 一个必须暴露的信号

约束（缺任一个都会把噪声放大成信号）：
  n01 占比过小时不归一（放大倍数过高）
  n02 原始退化未超噪声地板时不归一
  n03 原始值与归一值**都要落盘**（只写归一值会让不同轮次不可比）

信号：
  n04 **归一值 > 100% 不得截断** —— 它意味着观测方退化超出这条边的理论上限，
      即归因不清（传导塌陷的特征）。第一版用 `min(..., 100)` 截断，
      那会把最该警惕的形态伪装成最强的证据。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
LAYER = REPO / 'infra' / 'lambda' / 'shared' / 'python'
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))


@pytest.fixture(scope='module')
def gcf():
    import graph_confidence
    return graph_confidence


def test_d01_normalizes_the_recorded_pethistory_shape(gcf):
    """实测形态：10.1pp 退化 / 11.89% 占比 → 归一 ~85%，判 confirmed。

    这是本机制存在的理由：原始值落在中间带、按固定阈值判不了，
    而归一后是明确的强依赖。
    """
    norm, why = gcf.normalize_by_dilution(10.1, 25375, 213411)
    assert norm is not None and 80.0 < norm < 90.0, f"归一值异常: {norm}"
    assert '理论上限' in why

    status, reason = gcf.classify_intervention(
        213411, 213411, 10.1, injection_confirmed=True,
        independent_observing_sources=3,
        edge_baseline_calls=25375, observer_total_calls=213411)
    assert status == gcf.STATUS_CONFIRMED
    assert '归一化' in reason and '中间带' in reason


def test_d02_over_ceiling_is_surfaced_not_capped(gcf):
    """归一值 > 100% 必须**原样返回**并告警，绝不 min() 截断。

    实测：打断 `petsite -> list-adoptions`（占比 7.04%）观测到 petsite 退化
    24.7pp —— 24.7/7.04 = **350%**。这说明退化不能全部归因于这条边
    （另有原因让观测方整体变差），是传导塌陷的特征。
    截断成 100% 再据此判 confirmed，等于把最该警惕的形态伪装成最强的证据。
    """
    norm, why = gcf.normalize_by_dilution(24.7, 704, 10000)
    assert norm is not None and norm > 100.0, f"超上限必须原样返回，实际 {norm}"
    assert '超出 100%' in why and '不能全部归因' in why


def test_d03_over_ceiling_must_not_confirm(gcf):
    """中间带 + 归一超上限 → 仍不得判 confirmed（归因不清）。"""
    status, _ = gcf.classify_intervention(
        10000, 10000, 15.0, injection_confirmed=True,
        independent_observing_sources=3,
        edge_baseline_calls=300, observer_total_calls=10000)
    assert status == gcf.STATUS_INCONCLUSIVE, \
        '归一值 500% 说明归因不清，不得据此确证'


def test_d04_tiny_share_is_not_normalized(gcf):
    """占比低于噪声地板时不归一 —— 否则放大倍数过高。"""
    norm, why = gcf.normalize_by_dilution(8.9, 100, 67933)   # 占比 0.15%
    assert norm is None
    assert '放大' in why


def test_d05_below_noise_floor_is_not_normalized(gcf):
    """原始退化未超噪声地板时不归一 —— 否则是在放大噪声。

    0.3pp 除以 3% 的占比会得出 10%，凭空造出一个信号。
    """
    norm, why = gcf.normalize_by_dilution(0.3, 7685, 67933)
    assert norm is None
    assert '噪声' in why


def test_d06_zero_observer_traffic_is_not_normalized(gcf):
    norm, why = gcf.normalize_by_dilution(10.0, 100, 0)
    assert norm is None
    assert '无法算' in why or '为 0' in why


def test_d07_normalization_never_overrides_the_channel_gate(gcf):
    """纯吞吐通道的判据优先于归一化 —— 顺序不能反。

    原始退化已超 confirm 线时先走通道分级：吞吐塌陷分不清「观测方自己失败」
    与「上游不再调它」，此时归一化解决不了归因问题，反而会掩盖它。
    实测 `petsite -> list-adoptions` 正是这个形态（24.7% 全来自吞吐）。
    """
    status, reason = gcf.classify_intervention(
        10000, 10000, 24.7, evidence_channel='throughput_only',
        injection_confirmed=True, independent_observing_sources=1,
        edge_baseline_calls=704, observer_total_calls=10000)
    assert status == gcf.STATUS_INCONCLUSIVE
    assert '吞吐塌陷' in reason, '应由通道分级给出结论，而不是归一化'


def test_d08_both_raw_and_normalized_are_persisted():
    """原始退化与归一值都必须落盘 —— 只写归一值会让不同轮次不可比。

    占比随流量画像变化（同一条边在不同压测配置下占比不同），
    只保留归一值会让「这轮 85% vs 上轮 60%」无法解释是依赖变了还是流量变了。
    """
    src = (REPO / 'chaos' / 'code' / 'runner' / 'edge_verification.py').read_text()
    assert 'edge_baseline_calls' in src and 'observer_total_calls' in src, \
        '判定结果里必须同时带上算稀释上限的两个原始输入'
    wsrc = src[src.index('def write_verdict'):]
    assert 'verify_degradation' in wsrc, '原始退化必须落盘'
