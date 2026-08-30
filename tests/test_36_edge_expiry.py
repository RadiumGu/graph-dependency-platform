"""契约驱动的边过期执行器测试。

关键断言是**语义边界**而不只是能跑：

- c01/c02  TTL 来自契约、按边类型各自不同，不是全局阈值
- c03      只作用于 dependency_kind='dynamic' —— static 边表示架构声明，
           不该因为没被观测到就置 false（那是 drift_status 的职责）。
           这条断言是防止两种失效语义被混淆的守门。
- c04      默认 dry-run：部署代码不等于立刻开始改图
- c05      开关打开后才真正写
- c06      结构边不参与（expires_seconds=None 表示跟随节点生命周期）
- c07      单个类型查询失败不中断整轮
- c08      同一轮所有判定用调用方传入的同一个 round_ts 基准

参见 todo/graph-correctness-audit-and-gaps_20260830-1435.md 缺口 4。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
LAYER = REPO / 'infra' / 'lambda' / 'shared' / 'python'
if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))


@pytest.fixture()
def gcl(monkeypatch):
    monkeypatch.delenv('GRAPH_EDGE_EXPIRY_ENABLED', raising=False)
    import graph_cleanup
    return graph_cleanup


def _fake_query(counts: dict, log: list):
    """伪 neptune_query：按边类型返回 count，并记录所有下发的 Gremlin。"""
    def q(gremlin: str):
        log.append(gremlin)
        m = re.search(r"hasLabel\('([A-Za-z0-9]+)'\)", gremlin)
        label = m.group(1) if m else ''
        if gremlin.rstrip().endswith('.count()'):
            return {'result': {'data': {'@value': [counts.get(label, 0)]}}}
        return {'result': {'data': {'@value': []}}}
    return q


# ── c01/c02 TTL 来自契约且按类型不同 ─────────────────────────────────────

def test_c01_ttls_come_from_contract(gcl):
    labels = dict(gcl.expiring_edge_labels())
    assert labels, "契约里没有任何声明了 TTL 的边类型"
    # Calls 只有 deepflow 一个源，阈值同 CALLS_INACTIVE_AFTER_SECONDS
    assert labels['Calls'] == 1800
    # AccessesData 四个源都写，取最长源窗口 = xray 的 6h，
    # 否则会把 xray 发现的边误杀（这正是「TTL 必须按类型声明」的理由）
    assert labels['AccessesData'] == 21600


def test_c02_ttls_are_not_uniform(gcl):
    vals = {v for _, v in gcl.expiring_edge_labels()}
    assert len(vals) > 1, (
        "所有边类型的 TTL 相同，等于退回全局阈值 —— "
        "「Pod 属于哪个 Node」与「A 调用 B」的合理过期时间差两个数量级")


# ── c03 只作用于 dynamic 边（最要紧的一条）───────────────────────────────

def test_c03_only_targets_dynamic_edges(gcl):
    """static 边表示 AWS 配置 / CFN 模板的**架构声明**。

    它不该因为 DeepFlow 没观测到就被置 false —— 那是 drift_status 的
    declared_not_observed 要表达的信息。两种失效语义混在一起会静默删掉
    真实的架构声明。
    """
    log = []
    gcl.deactivate_stale_dynamic_edges(_fake_query({}, log), round_ts=1_000_000)
    assert log, "没有下发任何查询"
    for g in log:
        assert "has('dependency_kind','dynamic')" in g, (
            f"查询没有限定 dynamic，会波及 static 声明边:\n{g}")


# ── c04/c05 默认 dry-run，开关才写 ───────────────────────────────────────

def test_c04_dry_run_by_default(gcl, monkeypatch):
    log = []
    r = gcl.deactivate_stale_dynamic_edges(
        _fake_query({'Calls': 7}, log), round_ts=1_000_000)
    assert r['enabled'] is False
    assert r['per_label']['Calls']['stale'] == 7
    assert r['per_label']['Calls']['deactivated'] == 0, "dry-run 不应改写"
    assert not any('.property(' in g for g in log), \
        "dry-run 下不应下发任何写属性的语句"


def test_c05_writes_when_enabled(gcl, monkeypatch):
    monkeypatch.setenv('GRAPH_EDGE_EXPIRY_ENABLED', 'true')
    log = []
    r = gcl.deactivate_stale_dynamic_edges(
        _fake_query({'Calls': 3}, log), round_ts=1_000_000)
    assert r['enabled'] is True
    assert r['per_label']['Calls']['deactivated'] == 3
    writes = [g for g in log if "property('active', false)" in g]
    assert writes, "开关打开后应下发置 active=false 的语句"
    assert all('.iterate()' in g for g in writes), "批量改写必须 iterate()"


# ── c06 结构边不参与 ─────────────────────────────────────────────────────

def test_c06_structural_edges_excluded(gcl):
    """expires_seconds=None 的结构边生命周期跟随两端节点，不独立过期。"""
    labels = dict(gcl.expiring_edge_labels())
    for structural in ('LocatedIn', 'Contains', 'BelongsTo', 'RunsOn', 'HasSG'):
        assert structural not in labels, (
            f"{structural} 是结构边，不该有独立 TTL")


# ── c07 单类型失败不中断整轮 ─────────────────────────────────────────────

def test_c07_per_label_failure_is_non_fatal(gcl):
    calls = {'n': 0}

    def flaky(gremlin: str):
        calls['n'] += 1
        if calls['n'] == 1:
            raise RuntimeError("simulated Neptune failure")
        return {'result': {'data': {'@value': [0]}}}

    r = gcl.deactivate_stale_dynamic_edges(flaky, round_ts=1_000_000)
    # 第一个类型失败被跳过，其余仍被处理
    assert len(r['per_label']) == len(gcl.expiring_edge_labels()) - 1


# ── c08 同一轮共用一个时间基准 ───────────────────────────────────────────

def test_c08_single_round_ts_basis(gcl):
    """cutoff 必须是「调用方传入的 round_ts − 该类型 TTL」。

    如果各类型各自取 time.time()，同一批边会因执行先后落在不同 cutoff 上。
    """
    log = []
    round_ts = 1_700_000_000
    gcl.deactivate_stale_dynamic_edges(_fake_query({}, log), round_ts=round_ts)
    ttls = dict(gcl.expiring_edge_labels())
    seen = {}
    for g in log:
        lb = re.search(r"hasLabel\('([A-Za-z0-9]+)'\)", g).group(1)
        cut = int(re.search(r"lt\((\d+)\)", g).group(1))
        seen[lb] = cut
    for lb, cut in seen.items():
        assert cut == round_ts - ttls[lb], (
            f"{lb} 的 cutoff 是 {cut}，应为 {round_ts} - {ttls[lb]}")
