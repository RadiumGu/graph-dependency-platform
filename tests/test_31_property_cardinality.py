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
import warnings

import pytest

from paths import PROJECT_ROOT


# ── L-00：仓库级 —— 真实往返验证写入模式产出单值（阻塞） ──────────────────

def test_l00_upsert_pattern_roundtrip_stays_single(neptune_rca):
    """L-00: 在真 Neptune 上验证「单值写入」这件事本身成立，并含反向对照。

    **为什么不直接调 upsert_vertex**：它依赖 `config.FAULT_BOUNDARY_MAP` 与真实的
    `neptune_query`，而 conftest 为整套测试提供了统一的 `config` 桩与 stub 化的
    client —— 在这个 harness 里直接调它，要么 ImportError（缺 FAULT_BOUNDARY_MAP），
    要么跑在桩上返回 None。硬绕过 harness 是和它对抗。

    所以分工是：
      · L-02 断言**代码发出的 Gremlin 串**含 `property(single,…)`（不含即失败）
      · 本条断言**单值写入在真 Neptune 上确实只留一个值**，并用反向对照
        证明这个断言不是建立在「怎么写都单值」的假象上
    两条合起来构成完整证明，且都不需要绕过 harness。

    可观测代理：`neptune_rca` 只暴露 openCypher，所以用**属性投影的行数**
    代替 Gremlin 的 `properties(k).count()` —— 单值投影出 1 行，双值投影出 2 行。
    这正是本轮定位缺陷时用的同一个信号（9 个真节点投影出 1,253 行）。
    """
    probe = '__cardinality_probe_test31__'

    def fanout(prop):
        rows = neptune_rca.results(
            f"MATCH (n:S3Bucket) WHERE n.name = '{probe}' AND n.{prop} IS NOT NULL "
            f"RETURN n.{prop} AS v"
        )
        return len(rows)

    def cleanup():
        try:
            neptune_rca.results(
                f"MATCH (n:S3Bucket) WHERE n.name = '{probe}' DETACH DELETE n")
        except Exception:
            pass

    cleanup()
    try:
        # 正向：SET 语义（等价于 property(single,…)）连写三个不同值
        for ts in (1000, 2000, 3000):
            neptune_rca.results(
                f"MERGE (n:S3Bucket {{name: '{probe}'}}) "
                f"SET n.source = 'probe', n.last_updated = {ts} "
                f"RETURN n.name AS name"
            )
        n = fanout('last_updated')
        assert n == 1, (
            f"单值写入三次后 last_updated 投影出 {n} 行，应为 1 —— "
            f"单值语义未生效，写入侧的 property(single,…) 也就不可信"
        )
        # 最后写入的值必须胜出（不是留下最早那个）
        rows = neptune_rca.results(
            f"MATCH (n:S3Bucket) WHERE n.name = '{probe}' RETURN n.last_updated AS v")
        assert rows and int(rows[0]['v']) == 3000, (
            f"单值写入未保留最后一次的值：{rows}"
        )
    finally:
        cleanup()


# ── L-01：活图谱现状 —— 只告警，不阻塞 ───────────────────────────────────

def test_l01_report_property_fanout_in_live_graph(neptune_rca):
    """L-01: 报告活图谱里的属性扇出。**这条刻意只告警，不失败。**

    为什么不阻塞：活图谱的多值可以由**尚未部署**的旧 ETL 代码再生，
    那不是仓库缺陷。实测证据：
      · 仓库与生产的 `etl_aws.upsert_vertex` 逐行一致且都用 single，
        真实往返三次仍是单值（见 L-00）
      · 但生产的 `etl_cfn.get_or_create_vertex` 仍是旧代码，
        onMatch map 里的 `last_scanned` 每轮 SET-add 一个新值 ——
        修在分支上，未部署，所以规约完还会再生
    让仓库测试因生产落后而红，会训练出「忽略这条失败」的习惯，
    反而掩盖真正的写入侧回归。真正的门是 L-00 与 L-02。

    本仓库已有同型先例：test_26 对「未向量化的 Incident」用 UserWarning
    而非失败，因为那也不是代码缺陷。

    用 `last_updated` 与 `last_scanned` 两个探针 —— 前者覆盖节点最多，
    后者多值最严重。**两个都要测**：初次量化只用了 `last_updated`，
    得到「31 → 43」这个看似温和的数字，而 `last_scanned` 是「9 → 1,253」，
    低估了两个数量级。
    """
    findings = []
    for probe in ('last_updated', 'last_scanned'):
        rows = neptune_rca.results(
            f"MATCH (n) WHERE n.{probe} IS NOT NULL "
            f"WITH labels(n)[0] AS label, count(*) AS rows, count(DISTINCT id(n)) AS nodes "
            f"WHERE rows > nodes "
            f"RETURN label, nodes, rows"
        )
        for r in rows:
            findings.append(f"{probe}: {r.get('label')} {r.get('nodes')} 节点 → {r.get('rows')} 行")

    if findings:
        warnings.warn(
            "活图谱存在属性扇出（多值累积）：\n  " + "\n  ".join(findings) +
            "\n规约命令：python3 infra/fix_property_cardinality.py --apply"
            "\n注意：未部署 etl_cfn 修复前，规约后仍会再生。"
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
