"""tests/test_59_chokepoint_spof.py — 咽喉点（割点）判据与 transitive 边纪律

## 这组测试在补什么缺口

`q16_single_point_of_failure` 的判据是「**只在单个 AZ** 且被 ≥2 个服务依赖」。
它对 AZ 级故障是正确的，但**对区域级托管服务完全失明**：

    实测 2026-09-07：AgentGateway 与全部 6 个 AgentRuntime 的
    LocatedIn 边都是 0 —— q16 的 size([...LocatedIn...]) = 1 恒为假。

而 AgentCore 网关承载全部 agent 间流量（orchestrator 的 httpx CLIENT span 实测
09-04→09-07 四个子 agent 全覆盖、约每 5 分钟一次），它失效会让 orchestrator
到 4 个子 agent 全断。**在加入 q_articulation_chokepoints 之前，
图谱对这个故障模型没有任何表达能力。**

## 为什么 transitive 这个标志是本组测试的核心

`Delegates`（orchestrator → 子 agent）是两跳路径的汇总，物理上走
`RoutesVia → AgentGateway → RoutesToRuntime`。若把它算进可达性分析，
「绕开网关能否到 adoption」会命中这条 Delegates，于是**网关被判定为不是
单点故障** —— 恰好把要找的东西藏起来。

所以 `transitive` 漂移了、或有人把它从 Delegates 上删掉，
本查询会静默退化成「什么都找不到」。t59_02/03 就是钉这一点的。
"""
from __future__ import annotations

import os
import sys

import pytest

from paths import PROJECT_ROOT

LAYER = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'shared', 'python')
DR_DIR = os.path.join(PROJECT_ROOT, 'dr-plan-generator')
for p in (LAYER, DR_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

#: 声明为 transitive（多跳路径的汇总）的边类型。
#
# **改这个集合就得改本测试** —— 与 test_35::g18 同一套纪律。
# transitive 的判据是「两端之间是否还存在别的、代表同一次调用的节点」，
# 不是「弱依赖」或「间接依赖」。
TRANSITIVE_EDGES = {'Delegates'}


@pytest.fixture(scope='module')
def gc():
    import graph_contract
    return graph_contract


def test_t59_01_transitive_set_is_locked(gc):
    """transitive 边集合必须与本文件的显式清单一致。

    这条会红的典型场景：有人给某条边加了 transitive 来「让割点分析别报它」——
    那是用一个语义标志去调噪声，会让可达性分析静默漏掉真实路径。
    """
    declared = {lb for lb in gc.EDGE_TYPES if gc.is_transitive_edge(lb)}
    assert declared == TRANSITIVE_EDGES, (
        f"transitive 边集合变了。\n"
        f"  契约多出: {sorted(declared - TRANSITIVE_EDGES)}\n"
        f"  契约缺少: {sorted(TRANSITIVE_EDGES - declared)}\n"
        f"transitive 会让该边被**排除**在可达性/割点分析之外 —— "
        f"加错一条就等于在图上凭空断开一条真实路径，"
        f"减错一条就等于制造一条不存在的旁路。"
    )


def test_t59_02_delegates_is_transitive(gc):
    """`Delegates` 必须是 transitive —— 这条边是网关那两跳的汇总。

    若它不是 transitive，割点分析会把它当成 orchestrator → 子 agent 的
    物理直连，于是「绕开网关仍能到达」成立，**网关不再被判为咽喉点**。
    这正是 q_articulation_chokepoints 存在的目的被抹掉的方式。
    """
    assert gc.is_transitive_edge('Delegates'), (
        'Delegates 是 RoutesVia → AgentGateway → RoutesToRuntime 两跳的汇总，'
        '必须标 transitive，否则它会在可达性分析里充当一条物理上不存在的旁路。'
    )


def test_t59_03_physical_set_excludes_transitive_but_keeps_dependency(gc):
    """物理依赖边 = 依赖边 − transitive 边，且**不得为空**。"""
    dep = set(gc.dependency_edge_labels())
    phys = set(gc.physical_dependency_edge_labels())
    assert phys == dep - TRANSITIVE_EDGES
    assert phys, '物理依赖边集合为空会让割点分析退化成什么都查不到'
    # 网关那两条必须在物理集合里 —— 它们正是让网关可被检出的通路
    assert {'RoutesVia', 'RoutesToRuntime'} <= phys, (
        'RoutesVia / RoutesToRuntime 是唯一能让割点分析看见网关的两条边'
    )


def test_t59_04_query_excludes_transitive_labels():
    """`q_articulation_chokepoints` 的边集合里**不得**出现 transitive 边。

    纯静态检查：**直接读源文件**，不 import。
    原因：全量跑时 conftest 的 `_isolate_shadowed_packages` 会清理顶层包名，
    `from graph import queries` 可能解析到别的 `graph` 包 ——
    单独跑通过、全量跑失败。静态检查本来也不需要导入。

    防的是有人把边集合改回 `dependency_edge_labels()`（含 Delegates）。
    """
    src_path = os.path.join(DR_DIR, 'graph', 'queries.py')
    src = open(src_path, encoding='utf-8').read()
    start = src.index('def q_articulation_chokepoints')
    # 到下一个顶层 def 为止
    nxt = src.find('\ndef ', start + 1)
    body = src[start:nxt if nxt > 0 else len(src)]

    for bad in ('"Delegates"', "'Delegates'"):
        assert bad not in body, (
            'q_articulation_chokepoints 的边集合（含兜底）不得包含 Delegates —— '
            '它是 transitive，会制造绕开网关的假路径，'
            '恰好把本查询要找的咽喉点藏起来。'
        )
    assert 'physical_dependency_edge_labels' in body, (
        '应从契约取物理依赖边集合，而不是自己列 —— 硬编码清单漂移过'
        '（曾少 Invokes，导致 Lambda 里漏 16 条边）'
    )


def test_t59_05_chokepoint_query_runs_and_is_not_noisy(neptune_dr):
    """实跑一次：查询能执行，且结果规模在可人工消费的范围内。

    **不断言具体命中哪些节点** —— 那会随图谱变化而脆。
    只断言两件事：查询语法有效、结果不是噪声洪水。

    ⚠️ `AgentGateway` 现在**不会**出现在结果里：RoutesVia / RoutesToRuntime
    的 ETL 改动已提交但尚未部署，活图谱里还没有这两种边的实例
    （见 test_11 的 PENDING_FIRST_EDGE）。部署后它应当出现，
    那是 M7 的验收判据。
    """
    from graph import queries
    import graph.neptune_client as _gnc

    # dr-plan-generator/config.py 在 **import 时**求值 NEPTUNE_ENDPOINT，
    # 而 conftest 是在构建统一配置之后才设环境变量，所以模块级变量可能是 ""。
    # 这个坑 tests/test_21_e2e_pipeline.py::s7_06 已经踩过并留了同样的处置。
    _gnc.NEPTUNE_ENDPOINT = os.environ.get(
        'NEPTUNE_ENDPOINT',
        'petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com')

    rows = queries.q_articulation_chokepoints(min_blocked=2)
    assert isinstance(rows, list)
    for r in rows:
        assert {'chokepoint', 'type', 'blocked', 'upstream'} <= set(r), r
        assert r['blocked'] >= 2

    # 噪声上限：一份要给人看的咽喉点清单，几十条就没人看了。
    # 实测 2026-09-07 是 10 条（min_blocked=2）。留足余量到 40。
    assert len(rows) <= 40, (
        f'咽喉点结果 {len(rows)} 条，过多会让这份清单被当成噪声忽略。'
        f'考虑提高 min_blocked，或检查是否有 transitive 边没标。'
    )
    print(f'\n咽喉点（min_blocked=2）共 {len(rows)} 条:')
    for r in rows[:12]:
        print(f"  {r['type']:<16} {r['chokepoint']:<52} "
              f"阻断 {r['blocked']:<3} 上游 {r['upstream']}")
