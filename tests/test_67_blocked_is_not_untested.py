"""tests/test_67_blocked_is_not_untested.py — 「打不到」必须与「还没轮到」分开

## 这条门禁在解什么

2026-09-09 实测 115 条依赖边：

    confirmed          10
    inconclusive       14
    未测（无 status）   91

对那 91 条逐条跑 `chaos/code/runner/injectability.py`：

    injectable                 78 条  ← 工具是有的，纯粹没跑
    unreachable_by_any_backend 13 条  ← 用任何后端都打不到

那 13 条是 AgentCore 层（AgentRuntime → AgentTool / AgentRuntime /
KnowledgeBase）加 1 条 LambdaFunction → NeptuneCluster：
Chaos Mesh 只能打集群内 Pod 而 AgentCore runtime 是托管的，FIS 也没有
AgentCore 动作 —— **双向封死**。

**问题在于图谱上看不出这个区别。** 选靶逻辑早就会排除它们
（`tests/test_47::t305_06` 钉着「排除发生在打分之前」），
但那份知识只在代码里；查图的人看到的是一条没有 `verify_status` 的边，
与「还没轮到」完全无法区分。

后果是覆盖率数字给出**错误的努力方向**：看起来「再跑几轮就能覆盖」，
实际上永远轮不到。

## 为什么不给 verify_status 加第五个取值

`statuses` 只有 untested / confirmed / refuted / inconclusive，描述的是
**验证结果**；「能不能验」是正交维度 —— 一条边可以同时「被阻断」且「未测试」。
塞进 status 会让「不可验」看起来像一种验证结论，而它恰恰是「没有结论」，
并且会迫使每个消费方（置信度计算、覆盖率、DR 影响面）都跟着改。
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

from paths import PROJECT_ROOT

ROOT = pathlib.Path(PROJECT_ROOT)
for p in (ROOT / 'chaos' / 'code' / 'runner',):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def test_t67_01_contract_declares_blocked_reason_attr():
    """契约必须声明 `verify_blocked_reason`，且**不得**把它做成第五个 status。"""
    import yaml

    gc = yaml.safe_load(
        (ROOT / 'profiles' / 'graph_contract.yaml').read_text(encoding='utf-8'))
    ev = gc.get('edge_verification') or {}
    assert 'verify_blocked_reason' in (ev.get('attrs') or []), (
        '契约的 edge_verification.attrs 里缺 verify_blocked_reason —— '
        '没有它，「后端打不到」只能混在 untested 里'
    )
    statuses = set(ev.get('statuses') or [])
    assert statuses == {'untested', 'confirmed', 'refuted', 'inconclusive'}, (
        f'verify_status 的取值集合变了: {sorted(statuses)}\n'
        f'「打不到」**刻意不做成第五个 status** —— 它是能力维度而非结果维度，'
        f'一条边可以同时「被阻断」且「未测试」。'
        f'加第五个取值会让「不可验」看起来像一种验证结论。'
    )


def test_t67_02_injectability_still_flags_agentcore_as_unreachable():
    """AgentCore 层必须仍被判为后端不可达 —— 这是那 13 条标注的依据。

    这条会红的场景：有人给 `fault_catalog.yaml` 加了 AgentCore 动作，
    或者把 AgentRuntime 误加进 `POD_BACKED_LABELS`。
    **两种都是好事**，但都要求重新评估那 13 条标注（去掉 blocked_reason 再跑验证），
    所以必须有人看见。
    """
    import injectability as inj

    for src, dst in (('AgentRuntime', 'AgentTool'),
                     ('AgentRuntime', 'AgentRuntime'),
                     ('AgentRuntime', 'KnowledgeBase')):
        verdict, why = inj.injectability(src, dst)
        assert verdict == inj.UNREACHABLE, (
            f'{src} -> {dst} 的可注入性判定变成了 {verdict}（{why}）。\n'
            f'若确实获得了新的注入能力，请：\n'
            f'  1. 清掉这些边的 verify_blocked_reason\n'
            f'  2. 把它们放回验证队列\n'
            f'  3. 更新本用例\n'
            f'不要只改本用例 —— 那会让 13 条边永久停在「打不到」而实际已可打。'
        )


def test_t67_03_pod_backed_labels_do_not_include_managed_runtimes():
    """托管运行时不得被登记为 Pod 支撑类型。

    `POD_BACKED_LABELS` 决定「能否在源侧切断出向流量」。
    把 `AgentRuntime` 混进去会让 13 条边被误判为可注入，
    于是选靶器会选中它们、注入必然打空，
    而打空的结果在判定链里表现为**退化为 0** —— 那正是 refuted 的形状。
    按 DoD-10 累计两次 refuted 就删边，等于用一个建模错误删掉真实依赖。
    """
    import injectability as inj

    forbidden = {'AgentRuntime', 'AgentTool', 'KnowledgeBase',
                 'LambdaFunction', 'StepFunction'}
    overlap = inj.POD_BACKED_LABELS & forbidden
    assert not overlap, (
        f'这些托管类型被登记为 Pod 支撑: {sorted(overlap)}\n'
        f'它们不在集群内，源侧切断对它们无效；误登记会让注入打空，'
        f'而打空在判定链里长得像 refuted（退化为 0）—— '
        f'累计两次就会删掉真实依赖。'
    )


@pytest.mark.neptune
def test_t67_04_blocked_edges_are_not_counted_as_untested(neptune_rca):
    """活图谱里「被阻断」与「未测试」必须是互斥的两桶。

    同时校验一条纪律：**被阻断的边不得同时带有终态结论**。
    若某条边既有 blocked_reason 又是 confirmed，说明它其实验过 ——
    标注是错的，该清掉。
    """
    rows = neptune_rca.results("""
MATCH (a)-[r:AccessesData|Calls|Delegates|DependsOn|Invokes|InvokesTool|PublishesTo|Retrieves|RoutesToRuntime|RoutesVia]->(b)
WHERE r.verify_blocked_reason IS NOT NULL
RETURN coalesce(r.verify_status,'(无)') AS status, count(*) AS cnt
""")
    bad = [r for r in rows
           if r.get('status') in ('confirmed', 'refuted', 'inconclusive')]
    assert not bad, (
        f'这些边同时带 verify_blocked_reason 和终态结论: {bad}\n'
        f'有结论说明实际上验过（或试过），标注「打不到」是错的，应清掉。'
    )
