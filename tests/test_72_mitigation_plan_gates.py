"""tests/test_72_mitigation_plan_gates.py

DevOps Agent 处置计划消费层的两道闸。

## 这一层为什么需要门禁

`rca/actions/mitigation_plan.py` 会调用 `action_executor` 改动生产工作负载
（`scale_deployment` / `rollout_undo` / `rollout_restart`）。它的全部价值在
两道闸上 —— 闸失效不会报错，只会让一个不该自动跑的计划自动跑了。

## 两道闸

**闸一：`confidence` 必须用图谱证据交叉验证。** 计划自称 HIGH 只是规划方的
自我评价，依据是遥测相关性 —— 而相关性不是依赖关系。`mcp/README.md` 实测记着
DevOps Agent 不查图谱时有 75% 的概率仅凭「FIS 实验模板存在」下结论，
而**模板是意图不是结果**。

**闸二：没有 `rollback` 的计划不自动执行。** 与「故障注入必须可自动恢复」
是同一条纪律的另一面：一次失败的处置就是一个新故障，且没人知道怎么退回去。

## 一条硬规矩：refuted 必须拒绝，不是降级

`refuted` 的含义是「图谱曾声称这条依赖存在，故障注入证明不成立」。
基于一条被证伪的依赖去处置，动的可能是完全无关的东西 ——
那不是「信心不足」，是「推理前提是假的」。所以抛异常而不是降级。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

from paths import PROJECT_ROOT

ROOT = pathlib.Path(PROJECT_ROOT)
for _p in (ROOT / 'rca',):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _plan(**over):
    p = {
        'planId': 'mp-test-1',
        'investigationId': 'inv-test-1',
        'status': 'READY',
        'confidence': 'HIGH',
        'actions': [{'sequence': 1, 'type': 'SCALE_OUT',
                     'target': 'petsite', 'parameters': {'desiredCount': 4},
                     'rationale': 'CPU 饱和'}],
        'rollback': {'type': 'SCALE_OUT', 'parameters': {'desiredCount': 2}},
    }
    p.update(over)
    return p


def _ev(status):
    return lambda _t: {'verify_status': status}


# ───────────────────── 结构校验 ─────────────────────

@pytest.mark.parametrize('over,frag', [
    ({'planId': ''}, 'planId'),
    ({'status': 'DRAFT'}, 'READY'),
    ({'actions': []}, 'actions'),
    ({'actions': [{'sequence': 1, 'target': 'x'}]}, 'type'),
    ({'actions': [{'sequence': 1, 'type': 'SCALE_OUT'}]}, 'target'),
])
def test_t72_01_结构错误必须被挡住(over, frag):
    from actions.mitigation_plan import validate_plan
    ok, why = validate_plan(_plan(**over))
    assert not ok, f'{over} 应当被判为结构错误'
    assert frag in why, f'诊断里应提到 {frag}，实际: {why}'


# ───────────────────── 闸一：证据 ─────────────────────

def test_t72_02_confirmed才允许保持HIGH():
    from actions.mitigation_plan import gate_plan
    d = gate_plan(_plan(), edge_evidence=_ev('confirmed'))
    assert d['effective_confidence'] == 'HIGH'
    assert d['allow_auto'] is True, f'不该被降级: {d["reasons"]}'


@pytest.mark.parametrize('status', ['untested', 'inconclusive', ''])
def test_t72_03_证据不足必须降级为需人工(status):
    """untested / inconclusive / 查不到，在风险上是同一档。"""
    from actions.mitigation_plan import gate_plan
    d = gate_plan(_plan(), edge_evidence=_ev(status))
    assert d['allow_auto'] is False, (
        f'verify_status={status!r} 时仍允许自动执行 —— '
        f'「动它能解决问题」这个推断没有实测支撑')
    assert d['effective_confidence'] == 'LOW'
    assert any('实测支撑' in r or '证据' in r for r in d['reasons'])


def test_t72_04_拿不到证据与未验证同等对待():
    """edge_evidence=None（查不到）不得比 untested 宽松。"""
    from actions.mitigation_plan import gate_plan
    d = gate_plan(_plan(), edge_evidence=None)
    assert d['allow_auto'] is False, (
        '拿不到证据时仍允许自动执行 —— '
        '「不知道」不比「已知未验证」更安全')


def test_t72_05_refuted必须拒绝而不是降级():
    """refuted 是推理前提为假，不是信心不足。"""
    from actions.mitigation_plan import PlanRejected, gate_plan
    with pytest.raises(PlanRejected) as e:
        gate_plan(_plan(), edge_evidence=_ev('refuted'))
    assert 'refuted' in str(e.value)
    assert '证伪' in str(e.value) or '不成立' in str(e.value)


# ───────────────────── 闸二：rollback ─────────────────────

def test_t72_06_缺rollback必须降级():
    from actions.mitigation_plan import gate_plan
    p = _plan()
    p.pop('rollback')
    d = gate_plan(p, edge_evidence=_ev('confirmed'))
    assert d['allow_auto'] is False, (
        '没有 rollback 却允许自动执行 —— '
        '施加变更前必须先有撤销路径')
    assert any('rollback' in r for r in d['reasons'])


# ───────────────────── 动作翻译 ─────────────────────

def test_t72_07_未知类型不得静默丢弃():
    """静默丢弃会让「执行了 N 个动作」看起来是全部完成。"""
    from actions.mitigation_plan import plan_to_actions
    p = _plan(actions=[
        {'sequence': 1, 'type': 'SCALE_OUT', 'target': 'a',
         'parameters': {'desiredCount': 3}},
        {'sequence': 2, 'type': 'SOMETHING_NEW', 'target': 'b'},
    ])
    steps = plan_to_actions(p)
    assert len(steps) == 2, '未知类型被丢掉了'
    assert steps[1]['executor'] is None
    assert steps[1].get('note'), '未知类型必须带说明，否则无从诊断'


def test_t72_08_ISOLATE刻意不映射():
    """ISOLATE 在 EKS 上的等价做法是切流量 —— 那是注入不是处置。"""
    from actions.mitigation_plan import plan_to_actions
    steps = plan_to_actions(_plan(actions=[
        {'sequence': 1, 'type': 'ISOLATE', 'target': 'petsite'}]))
    assert steps[0]['executor'] is None, (
        'ISOLATE 被映射到了某个执行器 —— '
        '它会把一个降级变成一个中断')
    assert '中断' in steps[0].get('note', '')


def test_t72_09_缺副本数时不得猜():
    """猜一个副本数可能把服务缩到 0。"""
    from actions.mitigation_plan import plan_to_actions
    steps = plan_to_actions(_plan(actions=[
        {'sequence': 1, 'type': 'SCALE_OUT', 'target': 'petsite',
         'parameters': {}}]))
    assert steps[0]['executor'] is None, 'SCALE_OUT 缺副本数却仍要执行'
    assert '猜' in steps[0].get('note', '')


def test_t72_10_按sequence排序():
    from actions.mitigation_plan import plan_to_actions
    steps = plan_to_actions(_plan(actions=[
        {'sequence': 3, 'type': 'RESTART', 'target': 'c'},
        {'sequence': 1, 'type': 'ROLLBACK', 'target': 'a'},
        {'sequence': 2, 'type': 'RESTART', 'target': 'b'},
    ]))
    assert [s['sequence'] for s in steps] == [1, 2, 3]


# ───────────────────── 执行默认值 ─────────────────────

def test_t72_11_默认不执行():
    """默认 require_approval=True 且 dry_run=True。

    这个函数会改动生产工作负载，调用方必须显式表达意图。
    """
    import inspect

    from actions.mitigation_plan import execute_plan
    sig = inspect.signature(execute_plan)
    assert sig.parameters['require_approval'].default is True
    assert sig.parameters['dry_run'].default is True
    assert sig.parameters['approved'].default is False

    r = execute_plan(_plan(), edge_evidence=_ev('confirmed'))
    assert r['executed'] is False
    assert 'Mode 2' in r['skipped_reason']


def test_t72_12_闸未过且无批准时不执行():
    from actions.mitigation_plan import execute_plan
    r = execute_plan(_plan(), edge_evidence=_ev('untested'),
                     require_approval=False)
    assert r['executed'] is False
    assert '闸未通过' in r['skipped_reason']
