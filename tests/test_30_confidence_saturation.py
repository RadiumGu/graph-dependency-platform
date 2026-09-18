"""
test_30_confidence_saturation.py — 置信度饱和与自动化闸门

覆盖测试清单:S-01 ~ S-07

## 生产数据先行:饱和不是理论问题

图谱里 126 个有置信度记录的 Incident:

    confidence=1.1   × 1     ← **超过 1.0**
    confidence=1.0   × 49
    confidence=0.75  × 8
    confidence=0.7   × 33
    confidence=0.6   × 8
    confidence=0.3   × 27

**50/126（40%）>= 1.0。** 而那个 1.1 说明某条路径完全绕过了上界。

## 三个各自独立的缺陷

### 1. `step4_score` 用**截断后**的分数排序

`score = min(score, 100)` 在 `results.sort()` **之前**执行，于是原始分 110 与
150 的两个候选都变成 100，排序退化为字典插入顺序 —— 即取决于 DeepFlow 返回的
服务次序，而非证据强度。而排第一位的候选就是 DecisionEngine 拿去决策、
action_executor 拿去执行动作的那一个。

修法刻意是「保留原始分用于排序」，而**不是**重新归一各维度权重:
band 阈值（high>=80 / medium>=50）是按现有分值校准的，重新加权会改变所有历史
评分的相对关系，进而改变 auto / semi_auto 判定。

### 2. LLM 返回的 confidence 无上界

提示词要求 confidence 等于 confidence_breakdown 四项之和（40+30+20+10=100），
但 LLM 不可靠地遵守。`graph_rag_reporter` 解析后不钳制，
`decision_engine` 又直接 `float(rag_conf) / 100.0` —— 110 就变成 1.1。

### 3. `confidence = max(规则分, LLM 分)` 对自动化闸门是错的方向

规则分一旦饱和到 1.0（40% 的 Incident 如此），LLM 自己的判断就被完全覆盖 ——
即使模型说「置信度 35，证据很弱」，`max(1.0, 0.35)` 仍是 1.0。
对一个用来授权**自动执行修复动作**的闸门，乐观偏置是错的。

处理方式刻意**只收紧 auto 这一条路**（加 LLM 低置信否决），不改展示用的
confidence 与 band —— auto 是唯一「无人确认就动生产」的分支。
semi_auto / manual 都有人在环，不受影响。

## 当前风险定级:潜伏，非在跑

`auto_remediation_enabled` 默认 False 且生产未覆盖，所以 auto 目前会被降级为
semi_auto。这些修复是**安全打开该开关的前置条件**，不是在处理正在发生的事故。
"""
from unittest.mock import patch

import pytest


# ── S-01 ~ S-02: 排序保留原始分 ─────────────────────────────────────────────


def _score(error_services, graph_candidates):
    from core import rca_engine as eng
    with patch('neptune.neptune_queries.q5_similar_incidents', return_value=[]), \
         patch.object(eng, '_get_causal_prior', return_value=None):
        return eng.step4_score(error_services, [], graph_candidates, 'petsite')


def test_s01_raw_score_is_exposed():
    """S-01: 结果里必须带未截断的 raw_score。"""
    es = [{'service': 'a', 'first_error': '2026-08-28T10:00:00Z',
           'error_count': 1, 'error_rate_pct': 1}]
    gc = [{'service': 'a', 'has_upstream_error': False}]
    r = _score(es, gc)
    assert 'raw_score' in r[0], "缺少 raw_score —— 排序无法区分饱和候选"


def test_s02_saturated_candidates_still_rank_by_evidence():
    """S-02: 两个都会饱和的候选，必须按**原始分**排序而非插入顺序。

    这是本卡的核心:排第一位的候选会被 DecisionEngine 拿去决策、
    被 action_executor 拿去执行动作。
    """
    # weak 放在**前面**，若按插入顺序排序它会胜出
    es = [
        {'service': 'weak', 'first_error': '2026-08-28T10:00:05Z',
         'error_count': 1, 'error_rate_pct': 1},
        {'service': 'strong', 'first_error': '2026-08-28T10:00:00Z',
         'error_count': 9, 'error_rate_pct': 5, '_l4_source': True,
         '_l4_detail': {'syn_retrans': 5, 'tcp_rst': 20, 'tcp_timeout': 30}},
    ]
    gc = [
        {'service': 'weak', 'has_upstream_error': False},
        {'service': 'strong', 'has_upstream_error': False, 'infra_fault': True,
         'ec2_id': 'i-x', 'ec2_state': 'stopped', 'az': 'apne1-az1',
         'affected_pods': ['p1'], 'affected_services': ['strong']},
    ]
    r = _score(es, gc)
    by = {x['service']: x for x in r}
    assert by['strong']['raw_score'] > 100, "strong 应当饱和（原始分 >100）"
    assert by['strong']['score'] == 100, "截断后应为 100"
    assert r[0]['service'] == 'strong', (
        f"排序应按原始分，实际第一位是 {r[0]['service']} —— "
        f"若按截断后分数排序，饱和候选顺序会退化为插入顺序"
    )


# ── S-03 ~ S-04: LLM 置信度钳制 ─────────────────────────────────────────────


@pytest.mark.parametrize("llm_conf", [110, 150, 1000])
def test_s03_llm_confidence_clamped_at_reporter(llm_conf):
    """S-03: graph_rag_reporter 必须把 LLM 的 confidence 钳制到 [0,100]。

    生产图谱里实测存在 root_cause_confidence = 1.1 的 Incident。
    """
    import json
    from core import graph_rag_reporter as grr

    payload = json.dumps({
        'root_cause': 'x', 'confidence': llm_conf,
        'confidence_breakdown': {'deepflow': 40, 'cloudtrail': 30,
                                 'graph': 20, 'history': 10},
        'evidence': [], 'recommended_action': 'restart_pod',
        'reasoning': 'r', 'blast_radius': 'b',
    })

    class FakeBedrock:
        def invoke_model(self, **kw):
            body = json.dumps({'content': [{'text': payload}]}).encode()
            return {'body': type('B', (), {'read': lambda s: body})()}

    with patch('boto3.client', return_value=FakeBedrock()), \
         patch.object(grr, '_get_neptune_subgraph', return_value=''), \
         patch.object(grr, '_get_cloudwatch_metrics', return_value=''):
        out = grr.generate_rca_report('petsite', {'severity': 'P1'}, {})

    assert out['confidence'] <= 100, (
        f"LLM 返回 {llm_conf} 未被钳制，实际 {out['confidence']}"
    )


@pytest.mark.parametrize("rag_conf,expect_max", [(110, 1.0), (150, 1.0), (99999, 1.0)])
def test_s04_decision_engine_clamps_too(rag_conf, expect_max):
    """S-04: decision_engine 也必须钳制（第二道防线）。

    该值可能来自其它写入方，不能只依赖 reporter 侧。
    """
    from core.decision_engine import DecisionEngine
    de = DecisionEngine()
    r = de.evaluate('P2', {
        'root_cause_candidates': [{'service': 'petsite', 'confidence': 0.3}],
        'rag_report': {'confidence': rag_conf, 'recommended_action': 'restart_pod'},
    })
    assert r['confidence'] <= expect_max, f"confidence {r['confidence']} 越界"


# ── S-05 ~ S-07: auto 的 LLM 低置信否决 ─────────────────────────────────────


def _evaluate_with_auto_enabled(rule_conf, llm_conf):
    """在 auto_remediation_enabled=True 下求决策。

    必须显式打开 flag —— 否则 auto 会被 flag 统一降级为 semi_auto，
    测试就无法区分「否决生效」与「flag 拦下」，等于什么都没验证。
    """
    import config
    from core.decision_engine import DecisionEngine
    saved = config.FEATURE_FLAGS.get('auto_remediation_enabled')
    config.FEATURE_FLAGS['auto_remediation_enabled'] = True
    try:
        rag = {'recommended_action': 'restart_pod'}
        if llm_conf is not None:
            rag['confidence'] = llm_conf
        return DecisionEngine().evaluate('P2', {
            'root_cause_candidates': [{'service': 'petsite', 'confidence': rule_conf}],
            'rag_report': rag,
        })
    finally:
        config.FEATURE_FLAGS['auto_remediation_enabled'] = saved


@pytest.mark.parametrize("llm_conf,expect", [
    (35, 'semi_auto'),   # LLM 说弱 → 否决
    (45, 'semi_auto'),   # 仍低于阈值 → 否决
    (50, 'auto'),        # 恰好到阈值 → 放行
    (70, 'auto'),        # 可信 → 放行
])
def test_s05_llm_low_confidence_vetoes_auto(llm_conf, expect):
    """S-05: 规则分饱和到 1.0 时，LLM 低置信必须否决 auto。

    这是本卡最重要的一条:此前 max(1.0, 0.35) = 1.0 会让规则分饱和
    **单独授权自动执行修复动作**，即使模型明确表示证据很弱。
    """
    r = _evaluate_with_auto_enabled(1.0, llm_conf)
    assert r['action_level'] == expect, (
        f"LLM confidence={llm_conf} 时应为 {expect}，实际 {r['action_level']}"
    )


def test_s06_no_llm_confidence_does_not_veto():
    """S-06: 没有 LLM 分时不否决 —— 否则会让 auto 永久不可达。

    缺失不等于低置信。把「没有数据」当作「证据不足」会让整个特性休眠，
    与 T-023 里「待积累 100+ 告警」那个永远达不到的门槛是同一类错误。
    """
    r = _evaluate_with_auto_enabled(0.9, None)
    assert r['action_level'] == 'auto', (
        f"无 LLM 分不应否决，实际 {r['action_level']}"
    )


def test_s07_veto_does_not_touch_semi_auto_or_manual():
    """S-07: 否决只作用于 auto，不影响 semi_auto / manual。

    收紧一个安全闸门时，不该顺带改变有人在环的分支 ——
    那会在没有安全收益的前提下改变既有行为。
    """
    # P0 无论置信度都最多 semi_auto（策略矩阵规定）
    r = _evaluate_with_auto_enabled(1.0, 10)
    assert r['action_level'] in ('semi_auto', 'manual')
    import config
    from core.decision_engine import DecisionEngine
    r0 = DecisionEngine().evaluate('P0', {
        'root_cause_candidates': [{'service': 'petsite', 'confidence': 1.0}],
        'rag_report': {'confidence': 10, 'recommended_action': 'restart_pod'},
    })
    assert r0['action_level'] != 'auto', "P0 永不应为 auto"
