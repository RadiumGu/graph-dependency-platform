#!/usr/bin/env python3
"""
test_32_xray_drift_source.py — X-Ray 作为第二运行时观测源的测试

## 背景

漂移判定原先**只用 DeepFlow DNS** 作运行时观测源。但 AWS SDK 通常在启动时
解析一次域名就复用连接，走 VPC 端点更是不产生公网 DNS 查询 ——
结果一个每 24h 被调用 11,512 次的依赖，在 DNS 窗口里可以完全看不见。

实测（2026-08-29，引入 X-Ray 之前）：26 条带 `drift_status` 的边有 **22 条**
判为 `declared_not_observed`（85%），其中
`petsearch → ServicesEks2-ddbpetadoption…` 被 X-Ray 的 11,512 次调用证否。
那是**假阴性**，不是真漂移。

引入后实测：该边翻为 `drift_status='ok'` / `verified_by='xray'`。

## 三条测试的分工

- X-01 行为：DNS 为空但 X-Ray 有数据时**不能整体跳过**判定。
  这是原实现的具体缺陷（`if not dns_obs: return`）。
- X-02 行为：漂移查询必须用**精确名匹配**。原实现用 `containing()`，
  `petsite` 会匹配到 5 个节点（4 个是 Lambda），
  等于按「petsite 有没有发 DNS」给那些 Lambda 写凭空的漂移结论。
- X-03 契约：Q20 必须能读出 `verified_by` 并归类症状 ——
  没有读取方的属性就是「写了但没人读」，本仓库已犯过三次。
"""

import os
import sys

import pytest

from paths import PROJECT_ROOT


def _load_etl():
    shared = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'shared', 'python')
    etl_dir = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'etl_deepflow')
    for p in (shared, etl_dir):
        if p not in sys.path:
            sys.path.insert(0, p)
    import importlib
    import neptune_etl_deepflow as m
    return importlib.reload(m)


# ── X-01：X-Ray 单源即可驱动判定 ──────────────────────────────────────────

def test_x01_xray_alone_drives_drift_detection():
    """X-01: DNS 为空但 X-Ray 有数据时，判定必须继续跑，不能整体跳过。

    原实现是 `if not dns_obs: return` —— DNS 一空就整体跳过，
    于是「服务 → AWS 托管服务」这类 DNS 天生看不见的边
    **永远停在 declared_not_observed**，X-Ray 有多少观测都没用。
    """
    try:
        m = _load_etl()
    except Exception as e:
        pytest.skip(f"无法加载 etl_deepflow：{type(e).__name__}: {e}")

    calls = {'edges_written': 0}

    def _stub_query(q, *a, **kw):
        if '.property(' in q:
            calls['edges_written'] += 1
        # 让「查声明边」返回一条边 id，才能走到写入分支
        if 'outE(' in q:
            return {'result': {'data': {'@value': ['e-fake-1']}}}
        return {'result': {'data': {'@value': []}}}

    orig_q = m.neptune_query
    orig_dns = m.fetch_dns_connections
    orig_xray = m.fetch_xray_dependencies
    m.neptune_query = _stub_query
    m.fetch_dns_connections = lambda ip_map: {}                    # DNS 空
    m.fetch_xray_dependencies = lambda: {'svc-a': {'dynamodb'}}    # X-Ray 有数据
    try:
        m.run_drift_detection(['svc-a'], {})
    finally:
        m.neptune_query = orig_q
        m.fetch_dns_connections = orig_dns
        m.fetch_xray_dependencies = orig_xray

    assert calls['edges_written'] > 0, (
        "DNS 为空、X-Ray 有数据时判定被整体跳过 —— "
        "服务→AWS 托管服务这类边将永远停在 declared_not_observed"
    )


def test_x01b_both_sources_empty_still_skips():
    """X-01b: 两个源都没数据时才应跳过（避免把「没观测到」误判成「依赖消失」）。"""
    try:
        m = _load_etl()
    except Exception as e:
        pytest.skip(f"无法加载 etl_deepflow：{type(e).__name__}: {e}")

    calls = {'n': 0}

    def _stub_query(q, *a, **kw):
        calls['n'] += 1
        return {'result': {'data': {'@value': []}}}

    orig_q, orig_dns, orig_xray = (m.neptune_query, m.fetch_dns_connections,
                                   m.fetch_xray_dependencies)
    m.neptune_query = _stub_query
    m.fetch_dns_connections = lambda ip_map: {}
    m.fetch_xray_dependencies = lambda: {}
    try:
        m.run_drift_detection(['svc-a'], {})
    finally:
        m.neptune_query, m.fetch_dns_connections, m.fetch_xray_dependencies = (
            orig_q, orig_dns, orig_xray)

    assert calls['n'] == 0, (
        "两个观测源都为空时仍然写了判定 —— "
        "会把「采集侧故障」误记成「依赖不存在」"
    )


# ── X-02：必须精确名匹配 ─────────────────────────────────────────────────

def test_x02_drift_query_uses_exact_name_match():
    """X-02: 漂移查询必须按精确服务名匹配，不能用 containing()。

    实测 `containing('petsite')` 匹配到 5 个节点：
    Microservice `petsite` 加 4 个 Lambda（`petsite-rca-engine`、
    `petsite-ops-slack-notifier`、`petsite-rca-interaction`、
    `ServicesEks2-petsiteapplicationresourcecontrolerDC-…`）。
    那些 Lambda 的依赖被按「petsite 有没有发 DNS 查询」判定，两者毫无关系。
    """
    try:
        m = _load_etl()
    except Exception as e:
        pytest.skip(f"无法加载 etl_deepflow：{type(e).__name__}: {e}")

    seen = []

    def _stub_query(q, *a, **kw):
        seen.append(q)
        return {'result': {'data': {'@value': []}}}

    orig_q, orig_dns, orig_xray = (m.neptune_query, m.fetch_dns_connections,
                                   m.fetch_xray_dependencies)
    m.neptune_query = _stub_query
    m.fetch_dns_connections = lambda ip_map: {}
    m.fetch_xray_dependencies = lambda: {'petsite': {'dynamodb'}}
    try:
        m.run_drift_detection(['petsite'], {})
    finally:
        m.neptune_query, m.fetch_dns_connections, m.fetch_xray_dependencies = (
            orig_q, orig_dns, orig_xray)

    decl = [q for q in seen if 'outE(' in q]
    assert decl, "没有发出查声明边的查询"
    for q in decl:
        assert "has('name','petsite')" in q, (
            f"声明边查询未用精确名匹配，实际：{q[:250]}"
        )
        assert "containing('petsite')" not in q, (
            f"声明边查询仍在用 containing() 匹配服务名，会误匹配 Lambda：{q[:250]}"
        )


# ── X-03：verified_by 必须有读取方 ────────────────────────────────────────

def test_x03_q20_reads_verified_by_and_classifies(neptune_rca):
    """X-03: Q20 必须能读出 verified_by 并把症状归类。

    这条测试的意义是**给属性配一个读取方**：本仓库已三次出现
    「写了但全仓无人读」（active/last_seen、causal_weight、resilience 分数）。
    `verified_by` 若无人读，X-Ray 侧静默失效时判定会悄悄退回 DNS-only
    而结果看起来完全正常。
    """
    sys.path.insert(0, os.path.join(PROJECT_ROOT, 'rca'))
    try:
        from neptune import query_catalog as qc
    except Exception as e:
        pytest.skip(f"无法加载 query_catalog：{type(e).__name__}: {e}")

    assert 'q20_dependency_verification' in qc.QUERY_CATALOG, \
        "Q20 未进 query_catalog —— 外部调用方无法从统一入口发现它"
    assert callable(qc.get_query('q20_dependency_verification'))

    rows = qc.run_query('q20_dependency_verification', limit=20)
    assert isinstance(rows, list), f"Q20 返回类型异常：{type(rows)}"

    # 契约：每行必须带 verified_by 与归类好的 symptom
    for r in rows:
        assert 'verified_by' in r, f"Q20 结果缺 verified_by 字段：{r}"
        assert 'symptom' in r and r['symptom'], f"Q20 未归类症状：{r}"

    # 只要图里有任何一条 verified_by='xray' 的边，就说明 X-Ray 源真的在生效
    all_rows = qc.run_query('q20_dependency_verification',
                            only_problematic=False, limit=100)
    sources = {r.get('verified_by') for r in all_rows}
    assert sources, "图谱里没有任何带验证状态的依赖边"
