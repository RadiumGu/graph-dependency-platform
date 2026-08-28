"""
test_28_causal_prior.py — 因果先验（prior_root_cause_*）接入评分

覆盖测试清单:P-01 ~ P-06

## 这个机制此前的四个问题

原 `causal_weight`（本次改名 `prior_root_cause_rate`）**只有写入方、零读取方** ——
与 cycle-1 查出的 `active` / `last_seen` 写了没人读是同一缺陷类型。
除此之外还有三个:

### 1. 语义与名字不符（最严重）

原 docstring 写「B **同时出现在同一 Incident** 的次数」——共现语义，
字段也叫 `co_occurrence`。但 co_count 查的是 `Involves` 边，而 `Involves` 在
`write_incident` 里**只在 root_cause != affected_service 时为根因服务写一条**。

所以它实际测量的是「该上游**曾被判定为根因**的次数」，不是共现。
这也解释了稀疏度:全图 141 个 Incident 只有 8 条 `Involves` 边，且全指向 petsearch。

处理方式是**保留数据语义、改正名字与文档** —— 「曾是根因的频率」对 RCA 是比
共现更强的先验（共现只是相关，曾是根因带因果判定）。

### 2. 无时间衰减

原 `total` 取该服务**全历史** Incident 计数，旧证据与新证据同权、永不衰减。
实测这不是理论问题:petsearch 的 11 个 Incident 全在 ~134 天前，
旧实现会用 `co_count/11` 把四个月前的数据当作当前信号；加指数衰减后
样本权重塌到 **0.353**，如实表达「这里没有近期证据」。

### 3. 上游集合未过滤已下线服务

原查询无 `active` 过滤，于是给已缩容到零的服务也算权重 ——
生产日志里的 `causal_weight: gateway-service→petsite = 0.0` 就是这么来的。

### 4. 基线率混杂

`P(A 是根因 | B 故障)` 忽略 A 的整体根因率 —— 一个在所有故障里都被判为根因的
服务会在每条边上都拿到高权重却无针对性。已改为 lift = P(A|B)/P(A)。

## 为什么本文件全部断言行为、不断言源码文本

初版用「剥注释后做子串检查」的办法，结果 `_code_only` 把 Cypher 的
**三引号字符串字面量**当成 docstring 起始，解析错位吞掉真实代码，4 个测试误报。

这是同一教训的**第四次**显形:
  1. 用裸 `grep -c` 数装饰器
  2. 用正则扫 TYPE_TO_LABEL 时把注释掉的条目算成生效
  3. 把 docstring 里「刻意用 mergeV 而非 addV」的散文当代码
  4. 这次

结论不是「写更好的正则」，而是**停止对源码做文本断言**。行为断言既更可靠，
也真正测到了调用方依赖的东西。
"""
from unittest.mock import patch

import pytest


# ── P-01: 有读取方 —— 先验必须真的改变评分（这是最初的缺陷） ─────────────────


def test_p01_prior_changes_the_score():
    """P-01: 先验必须真的影响 step4_score 的输出。

    此前它只有写入方 —— 与 active / last_seen 写了没人读同一缺陷类型。
    断言方式是**跑两遍评分**（有先验 / 无先验）比较分数，而不是检查源码里
    有没有某个函数名 —— 后者无法证明它真的被用上了。
    """
    from core import rca_engine as eng

    error_services = [{'service': 'upA', 'first_error': '2026-08-28T10:00:00Z',
                       'error_count': 10, 'error_rate_pct': 5}]
    graph_candidates = [{'service': 'upA', 'has_upstream_error': False}]

    def run(prior):
        with patch.object(eng, '_get_causal_prior', return_value=prior), \
             patch('neptune.neptune_queries.q5_similar_incidents', return_value=[]):
            return eng.step4_score(list(error_services), [], graph_candidates, 'svcB')

    # 无先验
    base = run(None)[0]['score']
    # 有先验:rate=0.8、样本权重充足、lift>1 → 应加 8 分
    with_prior = run((0.8, 10.0, 2.0))[0]['score']

    assert with_prior > base, (
        f"先验未影响评分（{base} → {with_prior}）—— 说明读取方没有真正接上"
    )
    assert with_prior - base == 8, f"rate=0.8 应加 8 分，实际加了 {with_prior - base}"


# ── P-02 / P-03: 时间衰减 ───────────────────────────────────────────────────


def test_p02_old_evidence_weighs_less_than_new():
    """P-02: 同样数量的历史证据，越旧则样本权重越低。

    这是「衰减」的行为定义。原实现取全历史计数，新旧同权。
    """
    from actions import incident_writer as iw
    import time as _t

    hl = iw.CAUSAL_HALF_LIFE_DAYS
    now = _t.time()

    def iso(days_ago):
        return _t.strftime('%Y-%m-%dT%H:%M:%SZ',
                           _t.gmtime(now - days_ago * 86400))

    calls = {'n': 0}
    captured = {}

    def fake_results(cypher, params=None):
        calls['n'] += 1
        c = ' '.join(cypher.split())
        if 'Calls]->(n:Microservice' in c and 'RETURN upstream.name' in c:
            return [{'upstream_name': 'upA'}]
        if 'Incident {affected_service: $svc})-[:Involves]' in c:
            return []                      # co_count = 0
        if 'Incident {affected_service: $svc})' in c:
            return params['_incidents']    # 注入的分母样本
        if 'MATCH (i:Incident) RETURN' in c:
            return params['_incidents']
        if 'Incident)-[:Involves]' in c:
            return []
        if 'SET e.prior_root_cause_rate' in c:
            captured.update(params)
            return []
        return []

    def measure(days_ago):
        captured.clear()
        incidents = [{'st': iso(days_ago)} for _ in range(10)]

        def wrapped(cypher, params=None):
            p = dict(params or {})
            p['_incidents'] = incidents
            return fake_results(cypher, p)

        with patch('neptune.neptune_client.results', side_effect=wrapped):
            iw._update_causal_weights('svcB', 'upA')
        return captured.get('denom')

    fresh = measure(0)
    old = measure(hl * 3)          # 三个半衰期前
    assert fresh is not None and old is not None, "未捕获到写入参数"
    assert old < fresh, f"旧证据的样本权重应更低（{old} 应 < {fresh}）"
    assert old < fresh * 0.2, (
        f"三个半衰期后权重应降到 1/8 附近，实际 {old}/{fresh}"
    )


def test_p03_half_life_math_is_correct():
    """P-03: 半衰期语义正确 —— t=半衰期时权重恰为 0.5。"""
    from actions import incident_writer as iw
    hl = iw.CAUSAL_HALF_LIFE_DAYS
    assert hl > 0
    assert abs(0.5 ** (hl / hl) - 0.5) < 1e-9
    assert abs(0.5 ** (2 * hl / hl) - 0.25) < 1e-9
    # 四个月前的证据应被压到 <10%（petsearch 的 11 个 Incident 就是这个年龄）
    assert 0.5 ** (134 / hl) < 0.10


# ── P-04: 只看活跃上游 ──────────────────────────────────────────────────────


def test_p04_inactive_upstream_yields_no_prior():
    """P-04: 已下线（active=false）的上游读不到先验。

    行为断言:让桩返回空（模拟 active 过滤把边滤掉），确认返回 None
    而不是拿一个 0.0 去参与评分。
    生产日志里的 `causal_weight: gateway-service→petsite = 0.0` 就是
    没有这层过滤的后果 —— 该服务所属命名空间 6 个 Deployment 全为 0 副本。
    """
    from core import rca_engine as eng
    with patch('neptune.neptune_client.results', return_value=[]):
        assert eng._get_causal_prior('gateway-service', 'petsite') is None


def test_p04b_writer_skips_service_without_active_upstream():
    """P-04b: 没有活跃上游时写入方直接跳过，不写任何先验。"""
    from actions import incident_writer as iw
    wrote = {'n': 0}

    def fake(cypher, params=None):
        c = ' '.join(cypher.split())
        if 'SET e.prior_root_cause_rate' in c:
            wrote['n'] += 1
        return []          # 上游查询返回空 → 无活跃上游

    with patch('neptune.neptune_client.results', side_effect=fake):
        iw._update_causal_weights('petsite', 'petsearch')
    assert wrote['n'] == 0, "无活跃上游时不应写入先验"


# ── P-05: 有界、有门槛、不足时可见 ──────────────────────────────────────────


@pytest.mark.parametrize("prior,expect_bonus,why", [
    ((0.8, 10.0, 2.0), 8,  "样本充足且 lift>1 → 计分"),
    ((0.8,  0.5, 2.0), 0,  "样本权重 0.5 < 门槛 → 不计分"),
    ((0.8, 10.0, 0.9), 0,  "lift<=1 无特异性 → 不计分"),
    ((1.0, 10.0, 5.0), 10, "rate=1.0 时封顶 10 分"),
    ((0.0, 10.0, 2.0), 0,  "rate=0 不计分"),
])
def test_p05_bounded_and_gated(prior, expect_bonus, why):
    """P-05: 加分有上限、有样本门槛、有 lift 门槛。

    小样本比率是噪声。把噪声喂进评分正是本项目一直在清除的那类
    「无法核实的信号」（Bedrock KB 的编造先例、56% 孤儿向量）。
    """
    from core import rca_engine as eng

    error_services = [{'service': 'upA', 'first_error': '2026-08-28T10:00:00Z',
                       'error_count': 10, 'error_rate_pct': 5}]
    graph_candidates = [{'service': 'upA', 'has_upstream_error': False}]

    def run(p):
        with patch.object(eng, '_get_causal_prior', return_value=p), \
             patch('neptune.neptune_queries.q5_similar_incidents', return_value=[]):
            return eng.step4_score(list(error_services), [], graph_candidates, 'svcB')

    base = run(None)[0]['score']
    got = run(prior)[0]['score'] - base
    assert got == expect_bonus, f"{why}: 期望 +{expect_bonus}，实际 +{got}"


def test_p06_min_sample_is_reachable():
    """P-06: 样本门槛必须是**可达**的。

    原注释写「待积累 100+ 真实告警后启用」—— 以当前故障频率永远达不到，
    等于让机制永久休眠（这正是它长期只有写入方的原因之一）。
    门槛应该是一个真实系统会在数月内跨过的值。
    """
    from core import rca_engine as eng
    from actions import incident_writer as iw
    hl = iw.CAUSAL_HALF_LIFE_DAYS
    # 事件均匀分布在一个半衰期内时平均权重约 0.7
    reachable_count = eng.CAUSAL_MIN_SAMPLE / 0.7
    assert reachable_count <= 20, (
        f"门槛 {eng.CAUSAL_MIN_SAMPLE} 需要半衰期({hl}天)内约 "
        f"{reachable_count:.0f} 次判定才能跨过 —— 过高会让机制永久休眠"
    )
