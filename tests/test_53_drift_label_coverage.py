"""tests/test_53_drift_label_coverage.py — 依赖边清单不得脱离契约

## 这条测试要防什么

「依赖边有哪些」这个判据在仓库里有多处副本。契约提供了单一入口
`graph_contract.dependency_edge_labels()`，但有些位置读不到契约（Lambda 部署
副本的父目录里没有 `profiles/`）或需要字面量（Gremlin 字符串、prompt 文本），
所以**兜底清单本身是正确设计** —— 错的是兜底与契约脱钩且无人看守。

2026-09-08 实测到两处已经漂移的兜底，各自的后果都不是「少一点覆盖」：

### 一、etl_deepflow 的 drift 对账清单 → 产生假的监管信号

原本手写成 `('AccessesData', 'PublishesTo', 'InvokesVia', 'ConsumesFrom')`：

  · `ConsumesFrom` 在契约与图谱里**都不存在**（0 条）。查一个不存在的标签，
    Gremlin 不报错只返回空，所以这一项对账从来没生效过。
  · `InvokesVia` 只有一条没有写入方的孤儿边。
  · 漏掉 8 种依赖边，其中 `DependsOn` 正是 etl_xray 表示
    `Microservice → SQSQueue` 用的标签。

后果是**误报**而非漏报：判 `has_declared` 时看不见 `DependsOn`，于是
「声明存在且运行时已观测到」被判成 `declared_not_observed`。
实测 9 条 `declared_not_observed` 里 2 条是这样的假告警：

    petsite -> SQS           PublishesTo 判「没观测到」，而 DependsOn(xray) 就在旁边
    petsite -> StepFunction  InvokesVia  判「没观测到」，而 AccessesData(xray) 就在旁边

`declared_not_observed` 是本平台相对合同型登记册的差异化所在
（「你申报了但我们观测不到」这类审计发现）。带 22% 误报比没有它更糟。

### 二、dr-plan-generator 的 ordering 清单 → 恢复顺序算错

注释写「契约里 dependency: true 的 6 种」、实际列 6 项，而契约已是 10 种。
恢复顺序按依赖边拓扑排，漏 `Invokes`（16 条）/ `PublishesTo`（2 条）
就会把该先恢复的排到后面。

## 为什么用静态扫描而不是运行时断言

漏写的代码路径压根不会调用任何校验函数 —— 运行时断言对「忘了同步」这件事
结构性地无能为力。这与 test_51::m01 用静态扫描抓漏写 source 同理。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / 'infra' / 'lambda' / 'shared' / 'python'))


@pytest.fixture(scope='module')
def dep_labels() -> frozenset:
    """契约声明的依赖边集合 —— 本文件唯一的真值来源。"""
    from graph_contract import dependency_edge_labels  # type: ignore
    labels = dependency_edge_labels()
    assert labels, '契约里应有 dependency 边；取不到说明测试自身失效'
    return frozenset(labels)


def _tuple_literal_after(src: str, name: str) -> set:
    """取形如 `NAME = ( 'a', 'b', ... )` 的字面量元素集合。"""
    m = re.search(rf'^{re.escape(name)}\s*=\s*\((.*?)\)', src, re.S | re.M)
    assert m, f'没找到 {name} 的元组字面量'
    return set(re.findall(r'''['"]([A-Za-z]+)['"]''', m.group(1)))


def test_m01_deepflow_drift_兜底清单必须等于契约依赖边集合(dep_labels):
    """etl_deepflow 的 drift 对账兜底清单不得与契约漂移。"""
    p = _ROOT / 'infra' / 'lambda' / 'etl_deepflow' / 'neptune_etl_deepflow.py'
    src = p.read_text(encoding='utf-8')
    fallback = _tuple_literal_after(src, '_DRIFT_LABELS_FALLBACK')
    assert fallback == dep_labels, (
        f'drift 兜底清单与契约不一致。\n'
        f'契约有兜底没有: {sorted(dep_labels - fallback)}\n'
        f'兜底有契约没有: {sorted(fallback - dep_labels)}\n'
        f'漏掉的标签会让 has_declared 判错，把「已声明且已观测」误报成 '
        f'declared_not_observed。')


def test_m02_deepflow_drift_查询不得硬编码标签(dep_labels):
    """drift 查询必须走 `_drift_edge_labels()`，不得内联标签字面量。

    这条防的是「加了函数但调用点没改」——那种改动看起来完成了，
    实际一行都没生效。
    """
    p = _ROOT / 'infra' / 'lambda' / 'etl_deepflow' / 'neptune_etl_deepflow.py'
    src = p.read_text(encoding='utf-8')
    # 只看 has_declared 那段查询：它以 hasLabel('Microservice','LambdaFunction') 起头
    m = re.search(r"hasLabel\('Microservice','LambdaFunction'\)\.has\('name'.*?\.outE\(([^)]*)\)",
                  src, re.S)
    assert m, '没找到 drift 的 has_declared 查询'
    outE_arg = m.group(1)
    assert '_dl' in outE_arg or '_drift_edge_labels' in outE_arg, (
        f"drift 查询的 outE() 仍在内联标签字面量：{outE_arg[:120]}\n"
        f'应改为由 _drift_edge_labels() 生成。')


def test_m03_不得再引用不存在的边类型(dep_labels):
    """`ConsumesFrom` 这类幽灵标签不得出现在任何 ETL 的**边**查询里。

    幽灵标签的危害在于**静默**：Gremlin 查一个不存在的标签不报错、只返回空，
    所以带着它的对账逻辑会长期看起来在工作。

    ## 判据只扫边位置，不扫 hasLabel

    第一版把 `hasLabel(...)` 也算进来，结果报出 30+ 处「引用了 Microservice /
    Region / LambdaFunction」—— 全是**节点**类型。`hasLabel()` 在 Gremlin 里
    同时用于顶点和边，光看字面量无法判断当前遍历位置是点还是边，
    所以不能一视同仁。只有 `outE()` / `inE()` / `bothE()` 的实参可以确定是边标签。

    `hasLabel` 上的边标签因此扫不到 —— 那是本判据已知的覆盖边界，
    宁可漏报也不要制造假阳性：假阳性会训练人忽略告警，比没有测试更糟
    （与 test_51::m01 第一版把 9 处合规调用报成违规是同一教训）。
    """
    from graph_contract import EDGE_TYPES  # type: ignore
    declared = set(EDGE_TYPES)
    etl_dir = _ROOT / 'infra' / 'lambda'
    offenders = []
    for p in etl_dir.rglob('*.py'):
        if 'shared' in p.parts:          # 契约模块本身会列出全部标签
            continue
        src = p.read_text(encoding='utf-8', errors='replace')
        # 只取 outE/inE/bothE —— 它们的实参一定是边标签
        for m in re.finditer(r"\.(?:outE|inE|bothE)\(([^)]*)\)", src):
            for lb in re.findall(r"'([A-Z][A-Za-z]+)'", m.group(1)):
                if lb not in declared:
                    line = src[:m.start()].count('\n') + 1
                    offenders.append(f'{p.relative_to(_ROOT)}:{line} 引用了 {lb}')
    assert not offenders, (
        '以下位置在边查询里引用了契约不存在的标签 —— 查询会静默返回空而不报错：\n  '
        + '\n  '.join(sorted(set(offenders))))


def test_m04_dr_plan_ordering_兜底清单必须等于契约依赖边集合(dep_labels):
    """dr-plan-generator 的恢复顺序边集合不得与契约漂移。

    `_FALLBACK_SCOPE_EDGES` 刻意**不**在本断言范围内 —— 那是子图抽取范围，
    包含 `RunsOn` / `BelongsTo` 等承载边是正确的（做 DR 计划要把承载关系
    拉进来）。两个常量是两件事，别合并。
    """
    p = _ROOT / 'dr-plan-generator' / 'graph' / 'scope.py'
    if not p.exists():
        pytest.skip('dr-plan-generator 不在本工作树')
    src = p.read_text(encoding='utf-8')
    fallback = _tuple_literal_after(src, '_FALLBACK_ORDERING_EDGES')
    assert fallback == dep_labels, (
        f'dr-plan ordering 兜底清单与契约不一致。\n'
        f'契约有兜底没有: {sorted(dep_labels - fallback)}\n'
        f'兜底有契约没有: {sorted(fallback - dep_labels)}\n'
        f'恢复顺序按依赖边拓扑排，漏边会把该先恢复的排到后面。')
