"""tests/test_68_xray_effectiveness_and_blocked_class.py

X-Ray 生效性回落 + 阻断类别的门禁。

## 背景：这两件事是同一次实测的产物

判定链要判 refuted 之前必须先证明「我真的打断了它」
（`graph_confidence.py` 的注入生效门禁）。而生效性 `edge_took_effect`
**只从 DeepFlow 的边级流量算出来**，DeepFlow 只看得见集群内 Pod ——
于是 78 条「工具能打但没跑」的边里有 41 条源不在集群内，必然测不出生效性，
跑了也只会得 inconclusive。

本以为把生效性检测扩到 X-Ray 就能解锁这 41 条。**实测证明不能**：
36 条盲区边里 X-Ray 只看得见 1 条（6h 窗口下同样只有 1 条）。
再查源 Lambda 的调用数，根因清楚了 —— **15 个源里 14 个 24h 零调用**。

所以那些边验不了的真正原因不是观测后端、也不是注入工具，而是
**链路本身是休眠的**：混沌验证在零流量链路上不可能成立，
打不断一个没在跑的东西。

X-Ray 回落仍然保留（它对有流量的非集群源有效，且实测修掉了一个真 bug），
但收益的正确表述是「补上一类观测盲区」，不是「解锁 41 条」。
"""
from __future__ import annotations

import pathlib
import sys
import time

import pytest

from paths import PROJECT_ROOT

ROOT = pathlib.Path(PROJECT_ROOT)
for _p in (ROOT / 'chaos' / 'code' / 'runner',):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ───────────────────────── X-Ray 采集器 ─────────────────────────

def test_t68_01_窗口必须按6小时分段():
    """`GetServiceGraph` 硬上限 6h，超了是 InvalidRequestException。

    实测踩到：传 24h 窗口时每条边都静默拿到 None ——
    而 None 在模块语义里是「测不出生效性」，
    于是一个纯粹的参数越界被读成「X-Ray 看不到这条边」。
    """
    from xray_metrics import XRAY_MAX_WINDOW_SECONDS, _segments

    assert XRAY_MAX_WINDOW_SECONDS == 6 * 3600

    segs = _segments(0, 24 * 3600)
    assert len(segs) == 4, f'24h 应切成 4 段，实际 {len(segs)} 段'
    for s, e in segs:
        assert e - s <= XRAY_MAX_WINDOW_SECONDS, f'段 [{s},{e}] 超过 6h'
    # 段必须无缝覆盖整个窗口，否则会漏掉中间的调用
    assert min(s for s, _ in segs) == 0
    assert max(e for _, e in segs) == 24 * 3600
    ordered = sorted(segs)
    for (_, e1), (s2, _) in zip(ordered, ordered[1:]):
        assert e1 == s2, f'段之间有缝隙: {e1} != {s2}'


def test_t68_02_同名双节点必须聚合而不是取第一个():
    """X-Ray 把一个 Lambda 表示成两个同名节点，真实出边挂在后者上。

    实测形态：

        neptune-etl-trigger (AWS::Lambda)           -> neptune-etl-trigger  [Function]
        neptune-etl-trigger (AWS::Lambda::Function) -> neptune-etl-from-aws [AWS::Lambda]

    第一版实现遇到第一个同名节点就 return，挑中 `AWS::Lambda` 那个、
    在它的 Edges 里找不到目标、返回 None —— 一条**明明测得出**的边
    被误判成测不出，而这正是本模块要消灭的失败模式。
    """
    from xray_metrics import XRayEdgeMetrics

    services = [
        {'ReferenceId': 1, 'Name': 'trigger', 'Type': 'AWS::Lambda',
         'Edges': [{'ReferenceId': 2,
                    'SummaryStatistics': {'TotalCount': 31, 'OkCount': 31,
                                          'TotalResponseTime': 3.1}}]},
        {'ReferenceId': 2, 'Name': 'trigger', 'Type': 'AWS::Lambda::Function',
         'Edges': [{'ReferenceId': 3,
                    'SummaryStatistics': {'TotalCount': 20, 'OkCount': 18,
                                          'TotalResponseTime': 2.0}}]},
        {'ReferenceId': 3, 'Name': 'downstream', 'Type': 'AWS::Lambda'},
    ]

    class _FakePaginator:
        def paginate(self, **_kw):
            return [{'Services': services}]

    class _FakeXRay:
        def get_paginator(self, _name):
            return _FakePaginator()

    xr = XRayEdgeMetrics(client=_FakeXRay())
    got = xr._edge_stats('trigger', 'downstream', 0, 60)
    assert got is not None, (
        'trigger -> downstream 应当测得出 —— 出边挂在 AWS::Lambda::Function '
        '那个同名节点上，只看第一个同名节点会漏掉它。')
    total, ok_n, _ = got
    assert (total, ok_n) == (20, 18), f'统计取错: {got}'


def test_t68_03_同名内部调用边不得被当成依赖():
    """`AWS::Lambda -> AWS::Lambda::Function` 同名那条是 X-Ray 的内部结构。

    它表示「调用进入函数执行」，不是一条依赖。把它算进去会让
    「源到自己」凭空出现流量，从而让一条自环边看起来可测。
    """
    from xray_metrics import XRayEdgeMetrics

    services = [
        {'ReferenceId': 1, 'Name': 'fn', 'Type': 'AWS::Lambda',
         'Edges': [{'ReferenceId': 2,
                    'SummaryStatistics': {'TotalCount': 99, 'OkCount': 99,
                                          'TotalResponseTime': 9.9}}]},
        {'ReferenceId': 2, 'Name': 'fn', 'Type': 'AWS::Lambda::Function',
         'Edges': []},
        {'ReferenceId': 3, 'Name': 'other', 'Type': 'AWS::Lambda'},
    ]

    class _FakePaginator:
        def paginate(self, **_kw):
            return [{'Services': services}]

    class _FakeXRay:
        def get_paginator(self, _name):
            return _FakePaginator()

    xr = XRayEdgeMetrics(client=_FakeXRay())
    # 找 fn -> other：不存在，且不得被那条同名内部边冒充
    assert xr._edge_stats('fn', 'other', 0, 60) is None


def test_t68_04_采集失败必须ok为False():
    """与 DeepFlow 同一约定：采集不到不得伪装成「健康且零流量」。"""
    from xray_metrics import XRayEdgeMetrics

    class _Boom:
        def get_paginator(self, _n):
            raise RuntimeError('boom')

    snap = XRayEdgeMetrics(client=_Boom()).collect_edge_flow('a', 'b')
    assert snap.ok is False, (
        '采集失败必须 ok=False —— (100%, 0 requests, ok=True) '
        '会被下游读成「路径健康但零流量」，那是一个不同的结论。')


def test_t68_05_零基线不得判生效():
    """基线窗内 0 次调用时生效性必须是 None，不能是 False。

    False 的含义是「注入没作用到它」，会被门禁当作
    「已确认注入未生效」而放行到 refuted 判定之外；
    但零流量的真相是**什么都没证明**。
    """
    from xray_metrics import XRayEdgeMetrics

    class _FakePaginator:
        def paginate(self, **_kw):
            return [{'Services': [
                {'ReferenceId': 1, 'Name': 'a', 'Type': 'AWS::Lambda',
                 'Edges': [{'ReferenceId': 2,
                            'SummaryStatistics': {'TotalCount': 0,
                                                  'OkCount': 0,
                                                  'TotalResponseTime': 0.0}}]},
                {'ReferenceId': 2, 'Name': 'b', 'Type': 'AWS::Lambda'},
            ]}]

    class _FakeXRay:
        def get_paginator(self, _n):
            return _FakePaginator()

    eff, why = XRayEdgeMetrics(client=_FakeXRay()).took_effect('a', 'b')
    assert eff is None, f'零基线应返回 None，实际 {eff}（{why}）'
    assert '0 次调用' in why or '无从打断' in why


def test_t68_06_runner必须在DeepFlow测不出时回落():
    """接线门禁：runner 必须真的调用 X-Ray 回落，且**只在** None 时调。

    DeepFlow 有结论时优先用它 —— 它是 L7 实测流量，
    而 X-Ray 受采样率影响。
    """
    src = (ROOT / 'chaos' / 'code' / 'runner' / 'runner.py').read_text(
        encoding='utf-8')
    assert 'XRayEdgeMetrics' in src, 'runner 没有接入 X-Ray 回落'
    i_guard = src.find('if edge_took_effect is None:')
    i_call = src.find('XRayEdgeMetrics')
    assert i_guard != -1, '找不到 `if edge_took_effect is None:` 守卫'
    assert i_guard < i_call, (
        'X-Ray 回落没有被 `edge_took_effect is None` 守卫住 —— '
        '会在 DeepFlow 已有结论时覆盖掉更可靠的 L7 数字。')


# ───────────────────────── 阻断类别 ─────────────────────────

def test_t68_07_契约声明阻断类别():
    import yaml

    gc = yaml.safe_load(
        (ROOT / 'profiles' / 'graph_contract.yaml').read_text(encoding='utf-8'))
    attrs = ((gc.get('edge_verification') or {}).get('attrs') or [])
    assert 'verify_blocked_class' in attrs, (
        '契约缺 verify_blocked_class —— 只有 reason 文本的话，'
        '「永久工具天花板」与「环境前提不满足」会混在一个桶里，'
        '而前者不该进待办、后者该进（且待办是造流量不是跑注入）。')


def test_t68_08_覆盖率必须把两类阻断分开报():
    """混着报会再犯一次「错误的努力方向」的错。"""
    script = (ROOT / 'scripts' / 'emit_graph_coverage_metrics.py').read_text(
        encoding='utf-8')
    for m in ('blocked_unreachable', 'blocked_precondition',
              'verify_blocked_class'):
        assert m in script, f'覆盖率脚本缺 {m}'


@pytest.mark.neptune
def test_t68_09_阻断类别取值必须是injectability的判定常量(neptune_rca):
    """类别不得是自由文本 —— 它是给机器读的。"""
    import injectability as inj

    allowed = {inj.NO_OBSERVER, inj.NEEDS_COMPOUND, inj.PRECONDITION_UNMET,
               inj.UNREACHABLE, inj.INJECTABLE}
    rows = neptune_rca.results("""
MATCH ()-[r]->() WHERE r.verify_blocked_class IS NOT NULL
RETURN DISTINCT r.verify_blocked_class AS k
""")
    got = {r['k'] for r in rows}
    bad = got - allowed
    assert not bad, (
        f'图上出现了非法的阻断类别 {sorted(bad)}，合法取值: {sorted(allowed)}')
    assert inj.INJECTABLE not in got, (
        'injectable 不该被写成阻断类别 —— 它的含义是「能打」，'
        '标成阻断会把可做的事排除出待办。')
