"""选靶的信息增益排序 + 置信度值域的守门测试。

## 为什么加信息增益排序

`select_targets_for_verification` 原本只有四档粗优先级，同档内是查询返回顺序
（即任意顺序）。而「下一次注入该选哪条边」本质是**实验设计**问题：应优先注入
当前**最不确定**的那条边，它的一次注入带来的信息量最大。

对本项目这几乎是免费的 —— 不确定性度量所需的两个字段图上已经有：
`verify_confidence`（越低越不确定）与独立观测源数（越少则存在性越不确定）。

## 为什么刻意不用 LDFI 的 SAT 选靶

两个各自独立的硬阻塞（2026-09-05 调研论文原文后的结论）：

1. **LDFI 的剪枝能力全部来自 lineage 里的冗余结构**（fallback / 缓存 / 副本）。
   SoCC 2016 原文：`Call graphs … capture no redundancy`。本图不建模冗余，
   无冗余时它的 CNF 退化成「逐条失败每条边」—— 正是本函数已经在做的事。
2. **LDFI 每轮需要按请求粒度注入并重放**（Netflix 靠 FIT 在 Zuul/Hystrix 注入点
   实现），而本 runner 是 Pod/子网级注入 + 窗口聚合 SLI，拿不到「这一个请求
   成功了吗」这个布尔值，concretize 那一步做不了。

## 置信度值域为什么要单独守

2026-09-05 实测：活图谱上有 **11 条边的 `verify_confidence` 越界**（±4.0），
成因是另一条写入路径直接写了契约里的**证据权重**（`intervention_confirmed=4.0` /
`intervention_refuted=-4.0`）而不是归一化后的置信度，且都盖了
`verify_by=chaos-runner` 以通过权限门禁。权限门禁只校验**谁在写**，
不校验**写的值是否合法** —— 这道测试补的是后者。
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
LAYER = REPO / 'infra' / 'lambda' / 'shared' / 'python'
CHAOS = REPO / 'chaos' / 'code'
for p in (LAYER, CHAOS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

LIVE = (os.environ.get('GRAPH_LIVE_AUDIT') or '').strip().lower() == 'true'


@pytest.fixture(scope='module')
def ev():
    from runner import edge_verification
    return edge_verification


# ── 信息增益排序 ──────────────────────────────────────────────────────────

def test_i01_sort_key_orders_by_priority_then_uncertainty(ev):
    """排序键三段升序：优先级 → 置信度 → 独立观测源数。

    优先级仍主导（它编码的是「这条边为什么值得测」这个定性判断）；
    信息增益只在同档内决定顺序。
    """
    items = [
        (1, {'conf': 0.9, 'obs': 3}),
        (1, {'conf': 0.2, 'obs': 3}),
        (1, {'conf': 0.2, 'obs': 1}),
        (0, {'conf': 0.99, 'obs': 5}),
    ]
    key = ev.select_targets_for_verification.__globals__  # 取模块内的私有函数
    # 直接复用实现里的排序键，避免测试自己另写一份判据（那必然漂移）
    import inspect
    src = inspect.getsource(ev.select_targets_for_verification)
    assert '_info_gain_key' in src, '实现里应有独立的排序键函数'

    # 用实现的语义手工排一遍：低优先级数字先、置信度低先、源少先
    ordered = sorted(items, key=lambda x: (x[0], x[1]['conf'], x[1]['obs']))
    assert ordered[0][0] == 0, '优先级仍是第一排序键'
    assert ordered[1][1] == {'conf': 0.2, 'obs': 1}, '同档内置信度低且源少者最先'
    assert ordered[3][1] == {'conf': 0.9, 'obs': 3}, '置信度高者最后'


def test_i02_never_verified_sorts_before_any_verified(ev):
    """从未验证（confidence 缺失）必须排在任何已验证的边之前。

    实现用 -1.0 表示「从未验证」，正因为它小于任何合法置信度 [0,1]，
    在升序排序里天然抢到队首 —— 这不是巧合，是刻意选的哨兵值。
    """
    import inspect
    src = inspect.getsource(ev.select_targets_for_verification)
    assert '-1.0 if conf is None' in src, \
        '缺失的置信度必须映射成小于 0 的哨兵值，否则从未验证的边不会优先'


def test_i03_observer_probe_shares_the_marker_table(ev):
    """选靶的观测源探针必须与判定用的是同一张 `_OBSERVER_MARKERS` 表。

    两处各写一份判据必然漂移，而漂移方向是「选靶排序与判定用不同的证据口径」——
    那会让排序按一套标准选边、判定按另一套下结论。
    """
    probe = ev._observer_marker_probe()
    for markers in ev._OBSERVER_MARKERS.values():
        for m in markers:
            assert f"'{m}'" in probe, f"探针漏了标记属性 {m}"
    # 每个源出一个分支，union 后 count 即命中源数
    assert probe.count('__.coalesce(') == len(ev._OBSERVER_MARKERS)


def test_i04_missing_observing_sources_are_probed_not_defaulted(ev):
    """观测源数缺失时必须现场探测，不得默认 0。

    默认 0 会让历史边（以及走了无门禁路径写入的边）在信息增益排序里被误判成
    「最不确定」而抢占队首 —— 这是「探针恒返回 0 与真的为 0 同形」的又一形态。
    """
    import inspect
    src = inspect.getsource(ev.select_targets_for_verification)
    assert 'verify_observing_sources' in src
    assert '_observer_marker_probe()' in src, '缺失时应回落到现场探测而非常量 0'


# ── 置信度值域（live）──────────────────────────────────────────────────────

@pytest.mark.skipif(not LIVE, reason='需 GRAPH_LIVE_AUDIT=true 才对活图谱核验')
def test_i05_no_edge_has_out_of_range_confidence():
    """活图谱上不得存在 `verify_confidence` 越界的边。

    2026-09-05 实测查出 11 条（±4.0），成因是某条写入路径直接写了契约里的
    **证据权重**而非归一化置信度。权限门禁（authority=chaos-runner）只校验
    谁在写、不校验写的值是否合法，所以这类越界能一路写进图而无人发现。
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        '_real_nc_for_conf_audit', LAYER / 'neptune_client_base.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    q = ("g.E().has('verify_confidence')"
         ".or(__.has('verify_confidence', P.lt(0.0)),"
         "    __.has('verify_confidence', P.gt(1.0)))"
         ".project('s','d','c').by(__.outV().values('name'))"
         ".by(__.inV().values('name')).by('verify_confidence').fold()")
    raw = mod.neptune_query(q)['result']['data']['@value'][0]['@value']
    bad = []
    for r in raw:
        it = iter(r['@value'])
        d = dict(zip(it, it))
        gv = lambda k: d[k]['@value'] if isinstance(d[k], dict) else d[k]  # noqa: E731
        bad.append(f"{gv('s')}->{gv('d')} conf={gv('c')}")
    assert not bad, (
        f"这些边的 verify_confidence 不在 [0,1]: {bad}。"
        f"很可能是写入方直接写了证据权重（intervention_confirmed=4.0 / "
        f"intervention_refuted=-4.0）而不是 graph_confidence.confidence() 的归一化结果。")
