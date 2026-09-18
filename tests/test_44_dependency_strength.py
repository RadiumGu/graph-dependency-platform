"""依赖强度三级分类（Google SRE）的守门测试。

## 为什么加第三个轴

原模型只有**存在性**一个轴（confirmed / refuted / inconclusive），回答「这条边是不是
真的」，但回答不了「它有多要紧」—— 而后者才是影响面分析、容量规划、故障预算真正
要用的信息。Google SRE 的《Defining SLOs for services with dependencies》给出三档：

    hard      其宕机 = 你也宕机
    degraded  介于两者之间（如缓存失效只降级延迟，不失败）
    soft      设计得当则其故障对你无影响（如尽力而为的日志/追踪）

强度与存在性**正交**。引入它顺带修掉了一个此前没被识别的判定错误：
**一条 soft dependency 在干预数据上与「边不存在」完全同形**，原模型会把它判成
refuted，而按 DoD-10 累计两次 refuted 就删边 —— 于是一条真实存在、且**设计良好**的
依赖会被当成图谱错误删掉，在影响面分析里留下盲区。

## 本组守的四件事

  s01–s03  hard / degraded / soft 三档的边界
  s04      **throughput_only 通道永远不得判 hard**（封顶 degraded）
  s05–s07  soft 的两个前提各自挡掉一种混淆，缺任一个都不分级
  s08–s09  有独立观测证据的边**永远不得判 refuted**（存在性侧的连带修复）
  s10      阈值来自契约而非硬写
"""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
LAYER = REPO / 'infra' / 'lambda' / 'shared' / 'python'
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))

ENOUGH = 100      # 高于契约的 min_observation_requests，避免撞流量下限门


@pytest.fixture(scope='module')
def gcf():
    import graph_confidence
    return graph_confidence


@pytest.fixture(scope='module')
def thresholds():
    c = yaml.safe_load((REPO / 'profiles' / 'graph_contract.yaml').read_text())
    return c['edge_verification']['thresholds']


def strength(gcf, deg, **kw):
    kw.setdefault('observer_baseline_requests', ENOUGH)
    kw.setdefault('observer_injected_requests', ENOUGH)
    return gcf.classify_dependency_strength(deg, **kw)


# ── s01–s03 三档边界 ──────────────────────────────────────────────────────

def test_s01_hard_requires_success_rate_channel_and_high_degradation(gcf, thresholds):
    """退化跌破调用方自身「坏了」的门线且成功率通道有信号 → hard。

    hard 阈值 70pp 不是凭空定的：项目护栏把观测方 success_rate < 30% 当作
    「已经坏了」（见各实验规格 stop_conditions），退化 >= 70pp 即调用方
    跌破它自己定义的那条线 —— 与既有判据同源。
    """
    cls, why = strength(gcf, 95.0, evidence_channel='both')
    assert cls == gcf.DEP_CLASS_HARD
    assert '70' in why or 'hard' in why.lower()
    # 恰好在线上也算 hard
    assert strength(gcf, thresholds['hard_degradation_pct'],
                    evidence_channel='success_rate')[0] == gcf.DEP_CLASS_HARD


def test_s02_degraded_is_between_confirm_and_hard(gcf, thresholds):
    """影响真实但未使调用方失效 → degraded。"""
    mid = (thresholds['confirm_degradation_pct'] + thresholds['hard_degradation_pct']) / 2
    cls, why = strength(gcf, mid, evidence_channel='both')
    assert cls == gcf.DEP_CLASS_DEGRADED
    assert '降级' in why
    # 恰好在 confirm 线上是 degraded 的下界
    assert strength(gcf, thresholds['confirm_degradation_pct'],
                    evidence_channel='both')[0] == gcf.DEP_CLASS_DEGRADED


def test_s03_soft_needs_confirmed_injection_and_independent_evidence(gcf):
    """注入确认生效、观测方无退化、且有独立观测源看到过这条边 → soft。

    这一档是本次引入的核心：它把「打断了也没事」从**图谱错误**重新定性为
    **设计良好的证据**。
    """
    cls, why = strength(gcf, 1.2, evidence_channel='both',
                        injection_confirmed=True, independent_observing_sources=2)
    assert cls == gcf.DEP_CLASS_SOFT
    assert '设计' in why and '不是' in why


# ── s04 纯吞吐通道不得判 hard ─────────────────────────────────────────────

def test_s04_throughput_only_can_never_be_hard(gcf):
    """证据全来自吞吐塌陷时，即便退化 100% 也只能到 degraded。

    hard 的定义是「调用方自己不行了」，而这正是**成功率通道**表达的东西。
    纯吞吐塌陷分不清「观测方自己失败到不产生 response」与「上游不再调它」——
    实测踩过：断 DynamoDB 后 pay-for-adoption 成功率退化 **0.00pp**、吞吐塌陷
    100%，真实成因是 petsite 因 petsearch 失败已不再提交领养。若允许纯吞吐判
    hard，那一轮会得出「pay-for-adoption 硬依赖 DynamoDB」这个由传导伪造的强结论。
    """
    cls, why = strength(gcf, 100.0, evidence_channel='throughput_only')
    assert cls == gcf.DEP_CLASS_DEGRADED, '纯吞吐证据不得判 hard'
    assert '吞吐' in why and '封顶' in why
    # 而同样的退化率走成功率通道就是 hard —— 差别只在通道
    assert strength(gcf, 100.0, evidence_channel='success_rate')[0] == gcf.DEP_CLASS_HARD


# ── s05–s07 soft 的两个前提 ───────────────────────────────────────────────

def test_s05_no_soft_without_confirmed_injection(gcf):
    """注入生效性未确认 → 分不清 soft 与「注入根本没生效」，不分级。"""
    for ic in (None, False):
        cls, why = strength(gcf, 1.2, injection_confirmed=ic,
                            independent_observing_sources=2)
        assert cls is None, f'injection_confirmed={ic} 时不应分级'
        assert '注入' in why


def test_s06_no_soft_without_independent_evidence(gcf):
    """无独立观测源 → 分不清 soft 与「这条边不存在」，不分级。

    存在性没立起来，强度就无从谈起 —— 这两个前提各挡一种混淆，缺一不可。
    """
    cls, why = strength(gcf, 1.2, injection_confirmed=True,
                        independent_observing_sources=0)
    assert cls is None
    assert '独立观测源' in why and '存在性' in why


def test_s07_middle_band_cannot_be_classified(gcf, thresholds):
    """中间带无法分级 —— 重试/熔断/缓存会让 hard 只表现出轻微退化。"""
    mid = (thresholds['refute_degradation_pct'] + thresholds['confirm_degradation_pct']) / 2
    cls, why = strength(gcf, mid, injection_confirmed=True,
                        independent_observing_sources=2)
    assert cls is None
    assert '中间带' in why


def test_s08_low_traffic_blocks_any_classification(gcf, thresholds):
    """流量不足时任何分级都不成立 —— 与存在性侧同一条护栏。"""
    cls, why = strength(gcf, 95.0, observer_baseline_requests=5,
                        observer_injected_requests=5)
    assert cls is None
    assert '流量不足' in why


# ── s09–s10 存在性侧的连带修复 ────────────────────────────────────────────

def test_s09_edge_with_independent_evidence_is_never_refuted(gcf):
    """有独立观测源看到过的边，永远不得判 refuted。

    注入确认生效、观测方没退化，仍有两种完全不同的真相：
      ① 这条边是假的（图错了）        → 真 refuted
      ② 边是真的但是 soft dependency  → 打断它本就不该有影响
    区分二者的唯一依据是**独立观测源是否看到过它**。

    实测代价已经付过一次：`petsearch -[AccessesData]-> s3` 有两个独立源的硬证据
    （X-Ray 24h 内 17,190 次调用、NFM 50 条流 1.9MB），却被判 refuted。
    按 DoD-10 累计两次 refuted 就删边 —— 删掉一条真实的 soft dependency
    会在影响面分析里造成盲区，而盲区比冗余危险。
    """
    status, why = gcf.classify_intervention(
        ENOUGH, ENOUGH, 1.2, injection_confirmed=True,
        independent_observing_sources=2)
    assert status == gcf.STATUS_INCONCLUSIVE, '有独立证据时不得判 refuted'
    assert 'soft' in why
    # 而无独立证据时，这才是真 refuted
    status2, why2 = gcf.classify_intervention(
        ENOUGH, ENOUGH, 1.2, injection_confirmed=True,
        independent_observing_sources=0)
    assert status2 == gcf.STATUS_REFUTED
    assert '无任何独立观测源' in why2


def test_s10_soft_dependency_does_not_increment_refute_count():
    """soft dependency 不得让 refute_count 递增 —— 否则 DoD-10 仍会删掉它。

    这是端到端的一条：s09 保证状态不是 refuted，本条保证计数器也没被加。
    两者缺一，删边的路径就还在。
    """
    sys.path.insert(0, str(REPO / 'chaos' / 'code'))
    from runner.edge_verification import verify_edge

    edge = {'eid': 'e1', 'label': 'AccessesData', 'observer': 'petsearch',
            # 两个独立观测源，正是 petsearch->s3 的真实形态
            'props': {'xray_call_count': 17190, 'nfm_flow_count': 50,
                      'source': 'aws-etl'}}
    v = verify_edge(edge, ENOUGH, ENOUGH, 1.2, 'exp-test',
                    evidence_channel='both', injection_confirmed=True)
    assert v['status'] == 'inconclusive'
    assert v['refute_count'] == 0, 'soft dependency 不得计入 refute_count'
    assert v['dependency_class'] == 'soft'
    assert v['observing_sources'] == 2


def test_s11_hard_threshold_comes_from_the_contract(gcf, thresholds):
    """阈值必须来自契约，不得在代码里硬写。

    硬写会与契约漂移，而漂移时没有任何机制会发现 —— 这是本项目反复出现的形态。
    """
    assert 'hard_degradation_pct' in thresholds, '契约必须声明 hard_degradation_pct'
    assert (thresholds['confirm_degradation_pct']
            < thresholds['hard_degradation_pct'] <= 100.0), \
        'hard 线必须严格高于 confirm 线'
    src = (LAYER / 'graph_confidence.py').read_text()
    assert "_T.get('hard_degradation_pct'" in src or \
           "_T['hard_degradation_pct']" in src, '必须从契约读取而非硬写数字'
