#!/usr/bin/env python3
"""
test_31_property_cardinality.py — 顶点属性基数的守门测试

## 为什么需要这个守门

Gremlin/Neptune 的顶点属性默认是 **SET 基数**：不带 `Cardinality.single` 的写入
会追加而不是替换。这个缺陷完全静默 —— 节点总数一直是对的（867），
只有读属性时才暴露：

    MATCH (n:LambdaFunction) RETURN labels(n)[0], count(*)     → 31    （不碰属性）
    MATCH (n:LambdaFunction) WHERE n.last_scanned IS NOT NULL  → 9 个节点 / **1,253 行**

实测最坏单节点 172 个 `last_scanned` 值（≈ etl_cfn 的运行次数，每轮 +1，无界增长）。
规约前全图共 21 个 (label, 属性) 组合多值、1,897 个冗余值。

**注意：边属性不受影响** —— Neptune/TinkerPop 的边属性天生单基数。
所以只测顶点。也因此不能把边的写法当作顶点的修复模板。

## 两条测试的分工

- L-01 打在**活图谱**上：任何 label 的属性投影行数必须等于真节点数。
  这是最终验收，能抓住任何来源（包括未来新增的 ETL）引入的多值。
- L-02 打在**代码发出的 Gremlin** 上：用桩捕获 ETL 实际生成的查询串，
  断言每轮刷新的标量走 `property(single,...)` 而不在 mergeV 的 option-map 里。
  这是行为断言（测代码「做了什么」），不是源码文本断言。
"""

import os
import sys

import pytest

from paths import PROJECT_ROOT


# ── L-01：活图谱上不允许有属性扇出 ────────────────────────────────────────

def test_l01_no_property_fanout_in_live_graph(neptune_rca):
    """L-01: 任何节点类型的属性投影行数必须等于真节点数。

    用 `last_updated` 与 `last_scanned` 两个探针 —— 前者覆盖节点最多，
    后者多值最严重。**这两个必须都测**：初次量化时只用了 `last_updated`，
    得到「31 → 43，多 12 行」这个看似温和的数字，
    而真正的 `last_scanned` 是「9 → 1,253」，低估了两个数量级。
    """
    for probe in ('last_updated', 'last_scanned'):
        rows = neptune_rca.results(
            f"MATCH (n) WHERE n.{probe} IS NOT NULL "
            f"WITH labels(n)[0] AS label, count(*) AS rows, count(DISTINCT id(n)) AS nodes "
            f"WHERE rows > nodes "
            f"RETURN label, nodes, rows"
        )
        assert rows == [], (
            f"探针 {probe} 发现属性扇出（多值累积）：{rows}。"
            f"写入侧必须用 property(single,k,v)；"
            f"存量用 infra/fix_property_cardinality.py --apply 规约。"
        )


# ── L-02：ETL 发出的 Gremlin 必须对刷新字段用 single 基数 ─────────────────

def _capture_gremlin(module_dir: str, module_name: str, call, prop_names):
    """把 module 的 neptune_query 换成桩，捕获它实际发出的 Gremlin 串。"""
    shared = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'shared', 'python')
    for p in (shared, module_dir):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib
    m = importlib.import_module(module_name)
    m = importlib.reload(m)

    captured = []

    def _stub(q, *a, **kw):
        captured.append(q)
        # 返回一个形状正确的空结果，让调用方不至于崩在解析上
        return {'result': {'data': {'@value': []}}}

    orig = m.neptune_query
    m.neptune_query = _stub
    try:
        call(m)
    finally:
        m.neptune_query = orig
    return captured


def test_l02_deepflow_node_upsert_uses_single_cardinality():
    """L-02a: etl_deepflow 的 Microservice upsert 必须对刷新字段用 single。

    `ip` 是会变的（Pod 重建就换 IP）—— 实测 Microservice.ip 上真的出现过 2 个值，
    这否证了「onMatch 里的值都稳定所以 SET 去重压得住」这个假设。
    """
    etl_dir = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'etl_deepflow')
    services = [{'name': 'svc-a', 'namespace': 'ns1', 'ip': '10.0.0.1', 'az': 'ap-northeast-1a'}]
    try:
        queries = _capture_gremlin(
            etl_dir, 'neptune_etl_deepflow',
            lambda m: m.batch_upsert_nodes(services), None)
    except Exception as e:                     # 缺依赖时跳过而不是假失败
        pytest.skip(f"无法加载 etl_deepflow：{type(e).__name__}: {e}")

    assert queries, "batch_upsert_nodes 没有发出任何查询"
    g = "\n".join(queries)

    # 每轮刷新的标量必须走 single
    for prop in ('ip', 'namespace', 'environment', 'recovery_priority',
                 'fault_boundary', 'az'):
        assert f"property(single,'{prop}'" in g, (
            f"刷新字段 {prop} 未用 property(single,...) 写入 —— "
            f"会以 SET 基数累积。实际发出的 Gremlin：\n{g[:600]}"
        )

    # 且这些字段不允许出现在 onMatch 的 option-map 里
    if 'Merge.onMatch' in g:
        seg = g.split('Merge.onMatch', 1)[1][:300]
        for prop in ('ip', 'namespace', 'az'):
            assert f"'{prop}'" not in seg, (
                f"{prop} 仍在 onMatch option-map 里（默认 SET 基数）：{seg}"
            )


def test_l02_cfn_vertex_upsert_uses_single_cardinality():
    """L-02b: etl_cfn 的 get_or_create_vertex 必须对 last_scanned 用 single。

    这是规约前**唯一还在流血**的源：`last_scanned` 是每轮变化的时间戳，
    放在 onMatch map 里就每轮新增一个 distinct 值，实测已累积到 172 个。
    """
    etl_dir = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'etl_cfn')
    try:
        queries = _capture_gremlin(
            etl_dir, 'neptune_etl_cfn',
            lambda m: m.get_or_create_vertex('LambdaFunction', 'fn-x', 'stack-y'), None)
    except Exception as e:
        pytest.skip(f"无法加载 etl_cfn：{type(e).__name__}: {e}")

    assert queries, "get_or_create_vertex 没有发出任何查询"
    g = "\n".join(queries)

    for prop in ('stack_name', 'source', 'last_scanned'):
        assert f"property(single,'{prop}'" in g, (
            f"刷新字段 {prop} 未用 property(single,...) 写入。实际：\n{g[:600]}"
        )

    assert 'Merge.onMatch' not in g, (
        f"get_or_create_vertex 仍带 onMatch option-map（默认 SET 基数）：\n{g[:600]}"
    )
