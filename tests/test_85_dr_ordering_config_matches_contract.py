"""test_85_dr_ordering_config_matches_contract.py — 守 DR 排序边的**配置**，不是兜底。

## 为什么已经有 test_53::m04 了还要这一份

`tests/test_53_drift_label_coverage.py::test_m04` 断言
`dr-plan-generator/graph/scope.py` 的 `_FALLBACK_ORDERING_EDGES` 等于契约的
`dependency: true` 边集合。它一直是绿的，实测那个兜底清单确实 10 种、与契约
一致。

但 `ScopeResolver.__init__` 的取值是：

    self.ordering_edge_types = set(ordering_edge_types or _FALLBACK_ORDERING_EDGES)

**配置优先。** 而 `registry/plan_policy.yaml` 里有配置，所以兜底那条分支
**永远不执行** —— test_53::m04 守的是一条死路。

实测后果（2026-09-21）：配置只列 6 项，契约的 dependency: true 已是 10 种，
实际生效的排序边漏掉 `Invokes` / `PublishesTo` / `RoutesToRuntime` / `RoutesVia`。
而 test_53 自己的 docstring 早在 2026-09 就写明了这个后果：

    恢复顺序按依赖边拓扑排，漏 Invokes（16 条）/ PublishesTo（2 条）
    就会把该先恢复的排到后面。

也就是说：**缺陷被预言了、门禁被写了、而门禁装在了不被执行的那条路径上**，
于是缺陷继续存在。这是本仓库反复出现的形状——判据本身对，取样位置错。

本文件补上真正的观测点：直接读 `plan_policy.yaml` 这份**实际生效的配置**。
"""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_POLICY = _ROOT / 'dr-plan-generator' / 'registry' / 'plan_policy.yaml'
sys.path.insert(0, str(_ROOT / 'infra' / 'lambda' / 'shared' / 'python'))


@pytest.fixture(scope='module')
def contract_deps() -> frozenset:
    """契约声明的依赖边集合 —— 唯一真值来源。"""
    from graph_contract import dependency_edge_labels  # type: ignore
    labels = frozenset(dependency_edge_labels())
    assert labels, '契约里取不到 dependency 边；测试自身失效'
    return labels


@pytest.fixture(scope='module')
def policy() -> dict:
    assert _POLICY.exists(), f'找不到 {_POLICY}'
    cfg = (yaml.safe_load(_POLICY.read_text(encoding='utf-8')) or {}).get('scope_policy')
    assert cfg, 'plan_policy.yaml 里读不到 scope_policy —— 结构变了就要同步改本门禁'
    return cfg


def test_t85_01_排序配置必须覆盖契约全部依赖边(policy, contract_deps):
    """实际生效的 ordering_edge_types 必须等于契约的 dependency: true 全集。

    不是「子集就行」：恢复顺序漏掉任何一种依赖边，就会把该先恢复的排到后面。
    """
    actual = frozenset(policy.get('ordering_edge_types') or [])
    assert actual, 'ordering_edge_types 为空'

    missing = contract_deps - actual
    extra = actual - contract_deps
    assert not missing, (
        f'排序边配置漏了 {len(missing)} 种契约依赖边: {sorted(missing)}。\n'
        f'后果是恢复顺序算错 —— 拓扑排序看不见这些边，该先恢复的会被排到后面。\n'
        f'改 {_POLICY.relative_to(_ROOT)} 的 ordering_edge_types。')
    assert not extra, (
        f'排序边配置多了 {len(extra)} 种契约未标为 dependency 的边: {sorted(extra)}。\n'
        f'要么契约标错（改契约），要么这里多列了（改配置）—— 但不要改本门禁。')


def test_t85_02_排序边必须是范围边的真子集(policy):
    """ordering ⊂ scope。与 dr-plan-generator/tests/test_scope.py:208 同一约束。

    在这里重复一遍是有意的：那道断言走 ScopeResolver 构造路径，
    而本文件直接读配置文件。两个观测点都要在，因为它们能发现的问题不同 ——
    构造路径那条会被兜底值掩盖，读配置这条不会。
    """
    order = frozenset(policy.get('ordering_edge_types') or [])
    scope = frozenset(policy.get('scope_edge_types') or [])
    assert order < scope, (
        f'ordering 不是 scope 的真子集。\n'
        f'ordering 独有: {sorted(order - scope)}\n'
        f'（范围边应比排序边宽：范围要含 RunsOn/BelongsTo 这类非依赖边，'
        f'而排序只认依赖边。）')


def test_t85_03_配置与兜底清单必须一致(contract_deps):
    """配置与代码兜底清单必须给出同一答案。

    两者不一致时，行为取决于「配置有没有被加载」这个偶然因素 ——
    这正是本轮缺陷的成因：兜底对了、配置错了，而实际走配置。
    """
    sys.path.insert(0, str(_ROOT / 'dr-plan-generator'))
    from graph.scope import _FALLBACK_ORDERING_EDGES  # type: ignore

    fallback = frozenset(_FALLBACK_ORDERING_EDGES)
    cfg = frozenset(
        (yaml.safe_load(_POLICY.read_text(encoding='utf-8'))
         or {}).get('scope_policy', {}).get('ordering_edge_types') or [])

    assert fallback == cfg, (
        f'兜底清单与配置不一致：\n'
        f'  仅兜底有: {sorted(fallback - cfg)}\n'
        f'  仅配置有: {sorted(cfg - fallback)}\n'
        f'ScopeResolver 取 `配置 or 兜底`，所以实际生效的是配置；'
        f'两者不一致意味着有一份是错的，而测试可能只守了另一份。')
    assert fallback == contract_deps, (
        '兜底清单与契约不一致 —— 这条本该由 tests/test_53::test_m04 抓到，'
        '在此重复断言是为了让本文件单独跑时也能自洽。')
