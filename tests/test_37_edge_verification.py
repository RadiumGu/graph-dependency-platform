"""依赖边验证与置信度模型测试。

断言的是**语义边界**，不是"能跑"：

  v01  观测证据必须封顶 —— 否则假边只要 ETL 跑得久就会变"高置信"
       （arXiv:2607.09449：样本越多越容易被虚假相关性诱导出假边）
  v02  干预证据必须能翻转先验 —— 这是"能证伪自己那张图"的前提
  v03  零流量必须判 inconclusive，绝不能判 refuted（假阴性会删真实边）
  v04  中间带退化判 inconclusive —— 重试/熔断/缓存会让真实依赖只轻微退化
  v05  多源证据从边的现有属性推导（xray_* / nfm_* / deepflow）
  v06  verify_* 属性只有混沌运行器可写，ETL 不得覆盖干预证据
  v07  写回的 Gremlin **不含 property(single** —— Neptune 对边属性拒绝基数说明，
       这是原实现 100% 失败的根因，必须有回归守门
  v08  写回按 edge id 精确定位，不得用「所有出入边」的宽匹配
  v09  候选边只取入边 —— 在 B 注入不能检验 B 依赖谁
  v10  契约声明的判据齐全
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
LAYER = REPO / 'infra' / 'lambda' / 'shared' / 'python'
for p in (str(LAYER), str(REPO / 'chaos' / 'code')):
    if p not in sys.path:
        sys.path.insert(0, p)

EV_SRC = (REPO / 'chaos' / 'code' / 'runner' / 'edge_verification.py').read_text()
GF_SRC = (REPO / 'chaos' / 'code' / 'runner' / 'graph_feedback.py').read_text()


@pytest.fixture()
def gcf():
    import graph_confidence
    return graph_confidence


# ── v01/v02 证据权重语义 ──────────────────────────────────────────────────

def test_v01_observation_evidence_is_capped(gcf):
    """观测源从 3 个涨到 100 个，置信度不得再增长。"""
    a = gcf.confidence(static_sources=1, observing_sources=3)
    b = gcf.confidence(static_sources=1, observing_sources=100)
    assert a == b, (
        f"观测证据未封顶（3 源={a}，100 源={b}）—— "
        f"假边只要 ETL 跑得够久就会变成高置信")
    # 但两个观测源应当比一个高（封顶之前仍要有区分度）
    assert gcf.confidence(observing_sources=1) < gcf.confidence(observing_sources=3)


def test_v02_intervention_can_flip_prior(gcf):
    """双静态源声明的边（高先验），一次证伪后置信度必须掉到 0.5 以下。"""
    prior = gcf.confidence(static_sources=2, observing_sources=2)
    assert prior > 0.8, f"前提变了：双静态+双观测应是高先验，实为 {prior}"
    after = gcf.confidence(static_sources=2, observing_sources=2,
                           interventions_refuted=1)
    assert after < 0.5, (
        f"一次干预证伪没能翻转先验（{prior} -> {after}）—— "
        f"那就等于图谱无法被自己的实验证伪")
    assert gcf.confidence(static_sources=2, observing_sources=2,
                          interventions_confirmed=1) > prior


# ── v03/v04 判定的假阴性防护 ──────────────────────────────────────────────

@pytest.mark.parametrize('base,inj', [(0, 0), (5, 5), (100, 3), (3, 100)])
def test_v03_insufficient_traffic_never_refutes(gcf, base, inj):
    """流量不足时无论退化率多少都不能判 refuted。

    metrics.collect() 无数据时 fallback success_rate=100.0 / total=0，
    零流量与健康在指标上完全无法区分。判 refuted 会删掉真实存在的边。
    """
    for degradation in (0.0, 50.0, 100.0):
        status, reason = gcf.classify_intervention(base, inj, degradation)
        assert status == gcf.STATUS_INCONCLUSIVE, (
            f"基线 {base}/注入 {inj} 退化 {degradation}% 判成了 {status}")
        assert '流量' in reason


def test_v04_middle_band_is_inconclusive(gcf):
    """5%~20% 的中间带判 inconclusive，不判 refuted。"""
    assert gcf.classify_intervention(100, 100, 12.0)[0] == gcf.STATUS_INCONCLUSIVE
    assert gcf.classify_intervention(100, 100, 1.0)[0] == gcf.STATUS_REFUTED
    assert gcf.classify_intervention(100, 100, 35.0)[0] == gcf.STATUS_CONFIRMED
    # 中间带的理由必须说明为什么不下结论
    _, why = gcf.classify_intervention(100, 100, 12.0)
    assert '熔断' in why or '重试' in why


# ── v05 多源证据推导 ──────────────────────────────────────────────────────

def test_v05_evidence_derived_from_existing_props():
    """证据从边已有属性推导，不需要新增字段。

    活图谱实测：一条 Calls 边同时带 xray_call_count / nfm_flow_count /
    calls+error_rate 三组标记。
    """
    from runner import edge_verification as ev
    real_edge_props = {
        'source': 'deepflow-etl', 'dependency_kind': 'dynamic',
        'calls': 20, 'error_rate': 0.0,
        'xray_call_count': 2314, 'xray_last_seen': 1788105649,
        'nfm_flow_count': 1, 'nfm_last_seen': 1788106640,
    }
    static, observing, c, r = ev.evidence_from_props(real_edge_props)
    assert static == 0, "deepflow-etl 是动态源，不该算静态声明"
    assert observing == 3, f"应识别出 deepflow/xray/nfm 三个观测源，实得 {observing}"
    assert (c, r) == (0, 0), "首次验证时干预计数应为 0"

    static2, _, _, _ = ev.evidence_from_props(
        {'source': 'cfn-etl', 'dependency_kind': 'static'})
    assert static2 == 2, "cfn 声明 + static 类型应算两个静态证据"


# ── v06 属性权威 ──────────────────────────────────────────────────────────

def test_v06_only_chaos_runner_may_write_verify_attrs(gcf):
    """ETL 不得写 verify_* —— 静态采集覆盖干预证据等于用先验抹掉后验。"""
    assert gcf.may_write_verify_attr('chaos-runner')
    for etl in ('aws-etl', 'cfn-etl', 'deepflow-etl', 'xray', 'nfm'):
        assert not gcf.may_write_verify_attr(etl), f"{etl} 不该能写 verify_*"


# ── v07/v08 写回的回归守门（原实现 100% 失败的根因）───────────────────────

def test_v07_no_cardinality_on_edge_properties():
    """边属性写入不得带基数说明。

    Neptune 实测：`g.E(id).property(single,'k','v')` 返回
    400 UnsupportedOperationException
    "Cardinality specification may not be used with Edge properties."
    原 graph_feedback 因此 100% 失败且被 except 静默吞掉 ——
    活图谱 19 条 Calls 边上 chaos_* 属性全为 0。
    """
    import ast

    # 只检查**真正的 Gremlin 字符串字面量**，不检查文档字符串 ——
    # 这两个文件的 docstring 里刻意引用了 `property(single, ...)` 来解释
    # 为什么不能那样写，把说明文字当代码扫会误报（本测试第一版就误报了）。
    for name, src in (('edge_verification.py', EV_SRC), ('graph_feedback.py', GF_SRC)):
        tree = ast.parse(src)
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
                d = ast.get_docstring(node, clean=False)
                if d:
                    docstrings.add(d)
        gremlin_literals = [
            n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and n.value not in docstrings
            and ('g.E(' in n.value or "hasLabel('Calls')" in n.value)
        ]
        assert gremlin_literals, f"{name} 里找不到写边的 Gremlin 字面量"
        for lit in gremlin_literals:
            assert 'property(single' not in lit, (
                f"{name} 的边属性写入带了基数说明，Neptune 会返回 400 "
                f"并被 except 静默吞掉:\n{lit[:200]}")


def test_v08_verdict_written_by_edge_id_not_broad_match():
    """判定必须按 edge id 精确写，不得用「所有出入边」的宽匹配。

    原实现 `where(outV().has(name,svc).or_(inV().has(name,svc)))` 把同一判定
    写给注入目标的所有出边和入边 —— 在 svc 注入无法检验 svc 依赖谁，
    写上去是伪造证据。
    """
    m = re.search(r"def write_verdict.*?(?=\ndef |\Z)", EV_SRC, re.S)
    assert m, "找不到 write_verdict"
    body = m.group(0)
    assert "g.E('%s')" in body or "g.E('{" in body, "write_verdict 未按 edge id 定位"
    assert '.or_(' not in body, "write_verdict 用了 or_ 宽匹配"
    # graph_feedback 的入边写法也不该再出现 or_ 双向匹配
    m2 = re.search(r"g\.E\(\)\.hasLabel\('Calls'\).*?\"\"\"", GF_SRC, re.S)
    assert '.or_(' not in m2.group(0), "Calls 边写入仍是双向宽匹配"
    assert 'inV()' in m2.group(0), "Calls 边写入应只匹配入边"


# ── v09 候选边方向 ────────────────────────────────────────────────────────

def test_v09_candidates_are_inbound_only():
    """在 B 注入只能检验「谁依赖 B」，所以候选边只能是入边。"""
    m = re.search(r"def candidate_edges.*?(?=\ndef |\Z)", EV_SRC, re.S)
    assert m, "找不到 candidate_edges"
    body = m.group(0)
    assert '.inE(' in body, "candidate_edges 应查入边"
    assert '.outE(' not in body, "candidate_edges 不该查出边"


# ── v10 契约判据齐全 ──────────────────────────────────────────────────────

def test_v10_contract_declares_all_thresholds():
    from graph_contract_data import EDGE_VERIFICATION as EV
    for k in ('attrs', 'authority', 'statuses', 'evidence_weights', 'thresholds'):
        assert k in EV, f"契约缺 edge_verification.{k}"
    for w in ('static_declaration', 'observed_per_source', 'observed_cap',
              'intervention_confirmed', 'intervention_refuted'):
        assert w in EV['evidence_weights'], f"缺权重 {w}"
    for t in ('min_observation_requests', 'confirm_degradation_pct',
              'refute_degradation_pct', 'stale_verification_seconds'):
        assert t in EV['thresholds'], f"缺阈值 {t}"
    assert EV['evidence_weights']['intervention_refuted'] < 0, "证伪权重必须为负"

    # 关键不变量：干预权重必须大于可达到的最大先验，否则存在「任何单次实验都
    # 无法证伪」的边 —— 那就等于图谱无法被自己的实验推翻。
    w = EV['evidence_weights']
    max_prior = w['static_declaration'] * 2 + w['observed_cap']
    assert abs(w['intervention_refuted']) > max_prior, (
        f"证伪权重 {w['intervention_refuted']} 未超过最大先验 {max_prior} —— "
        f"存在单次实验无法证伪的边")
    assert w['intervention_confirmed'] > max_prior
    assert (EV['thresholds']['refute_degradation_pct']
            < EV['thresholds']['confirm_degradation_pct']), \
        "refute 阈值必须低于 confirm 阈值，中间带才存在"
