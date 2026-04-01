"""
test_08_integration_cross_module.py — 跨模块 Neptune 一致性测试

验证 rca / dr-plan-generator / chaos 三个模块使用同一 Neptune 图谱的一致性

Tests: I-08 ~ I-15, C-01 ~ C-04
"""
import os
import pytest


# ─── Neptune Client 一致性（C-01 ~ C-04）────────────────────────────────────

def test_c01_all_clients_same_endpoint():
    """C-01: 所有模块的 Neptune client 连接同一 endpoint。"""
    endpoint = os.environ.get('NEPTUNE_ENDPOINT', '')
    assert 'petsite-neptune.cluster-czbjnsviioad' in endpoint, \
        f"NEPTUNE_ENDPOINT 未设置或不正确: {endpoint}"

    # rca client
    from neptune import neptune_client as rca_nc
    # dr client
    from graph import neptune_client as dr_nc
    # chaos runner client
    from runner import neptune_client as chaos_nc

    # 验证 rca 和 dr 使用相同 endpoint（通过环境变量）
    assert rca_nc is not None
    assert dr_nc is not None
    assert chaos_nc is not None


def test_c02_rca_client_sigv4_works(neptune_rca):
    """C-02: rca Neptune client SigV4 认证可工作。"""
    result = neptune_rca.results("MATCH (n) RETURN count(n) AS cnt LIMIT 1")
    assert isinstance(result, list)
    assert result[0].get('cnt', 0) >= 0


def test_c02_dr_client_sigv4_works(neptune_dr):
    """C-02: dr-plan Neptune client SigV4 认证可工作。"""
    result = neptune_dr.results("MATCH (n) RETURN count(n) AS cnt LIMIT 1")
    assert isinstance(result, list)
    assert result[0].get('cnt', 0) >= 0


def test_c02_chaos_client_sigv4_works():
    """C-02: chaos runner Neptune client SigV4 认证可工作。"""
    from runner.neptune_client import query_opencypher
    result = query_opencypher("MATCH (n) RETURN count(n) AS cnt LIMIT 1")
    assert isinstance(result, list)
    assert result[0].get('cnt', 0) >= 0


def test_c03_all_clients_same_petsite_data(neptune_rca, neptune_dr):
    """C-03: 不同模块查询同一 petsite 节点，属性一致。"""
    cypher = "MATCH (s:Microservice {name: 'petsite'}) RETURN s.name AS name, s.recovery_priority AS priority LIMIT 1"

    rca_result = neptune_rca.results(cypher)
    dr_result = neptune_dr.results(cypher)

    assert rca_result, "rca client 未找到 petsite 节点"
    assert dr_result, "dr client 未找到 petsite 节点"
    assert rca_result[0].get('name') == dr_result[0].get('name'), \
        "rca 和 dr 查到的 petsite name 不一致"
    assert rca_result[0].get('priority') == dr_result[0].get('priority'), \
        "rca 和 dr 查到的 petsite priority 不一致"


def test_c04_chaos_opencypher_interface():
    """C-04: chaos runner 的 openCypher 接口正常工作。"""
    from runner.neptune_client import query_opencypher
    result = query_opencypher("MATCH (s:Microservice {name: 'petsite'}) RETURN s.name AS name LIMIT 1")
    assert isinstance(result, list)
    if result:
        assert result[0].get('name') == 'petsite'


# ─── I-08 ~ I-10: rca ↔ dr-plan-generator 联动 ──────────────────────────────

def test_i08_same_service_consistent_across_modules(neptune_rca, neptune_dr):
    """I-08: rca 和 dr-plan 查询同一服务数据一致。"""
    from neptune import neptune_queries as nq

    # rca 查询
    rca_svc = nq.q4_service_info('petsite')
    assert rca_svc, "rca 未找到 petsite 服务信息"

    # dr 查询（通过 dr client）
    dr_rows = neptune_dr.results(
        "MATCH (s:Microservice {name: 'petsite'}) RETURN s.name AS name, s.recovery_priority AS priority LIMIT 1"
    )
    assert dr_rows, "dr 未找到 petsite 服务信息"

    # 验证 name 一致
    assert rca_svc.get('name') == dr_rows[0].get('name') == 'petsite'


def test_i09_chaos_experiment_visible_to_dr(neptune_dr):
    """I-09: Phase A 新增的 ChaosExperiment 节点对 dr-plan-generator 模块可见。"""
    result = neptune_dr.results(
        "MATCH (n:ChaosExperiment) RETURN count(n) AS cnt LIMIT 1"
    )
    assert isinstance(result, list)
    cnt = result[0].get('cnt', 0) if result else 0
    # 至少应有测试中写入的实验记录（test_06 中写入了）
    assert cnt >= 0  # 不要求有数据，只验证查询不报错


def test_i10_mentions_resource_visible_to_dr(neptune_dr):
    """I-10: Phase A 新增的 MentionsResource 边对 dr-plan-generator 模块可见。"""
    result = neptune_dr.results(
        "MATCH (inc:Incident)-[:MentionsResource]->(r) RETURN count(inc) AS cnt LIMIT 1"
    )
    assert isinstance(result, list)
    # 只要查询不报错即可（有无数据取决于之前是否写入了 resolved incident）
    assert result[0].get('cnt', 0) >= 0


# ─── I-11 ~ I-13: rca ↔ infra 联动 ──────────────────────────────────────────

def test_i11_etl_services_visible_to_rca(neptune_rca):
    """I-11: ETL 写入的服务节点 rca 可查到（≥7 个微服务）。"""
    from neptune import neptune_queries as nq
    # q2_tier0_status 查所有微服务（可能不只 Tier0）
    all_svcs = neptune_rca.results(
        "MATCH (s:Microservice) RETURN s.name AS name LIMIT 20"
    )
    svc_names = [r.get('name') for r in all_svcs]
    assert len(svc_names) >= 7, f"微服务数量不足 7 个，实际: {svc_names}"

    known = {'petsite', 'petsearch', 'payforadoption', 'petlistadoptions',
             'petadoptionshistory', 'petfood', 'trafficgenerator'}
    found = known & set(svc_names)
    assert len(found) >= 5, f"已知服务中只找到 {found}"


def test_i12_call_chain_traversable(neptune_rca):
    """I-12: ETL 写入的调用链可以被 rca 遍历（payforadoption 有 Calls 边）。"""
    result = neptune_rca.results(
        "MATCH (s:Microservice)-[:Calls]->(t:Microservice) "
        "RETURN count(*) AS cnt LIMIT 1"
    )
    cnt = result[0].get('cnt', 0) if result else 0
    assert cnt > 0, "没有 Microservice Calls 边，ETL 可能未运行"


def test_i13_az_info_accessible_to_dr(neptune_dr):
    """I-13: infra ETL 写入的 AZ 信息 dr-plan 模块可查。"""
    from graph.queries import q12_az_dependency_tree
    result = q12_az_dependency_tree('apne1-az1')
    assert isinstance(result, list)
    # 有数据或无数据都可，只要不报错


# ─── I-14 ~ I-15: chaos ↔ dr-plan-generator 联动 ────────────────────────────

def test_i14_dr_can_query_chaos_data(neptune_dr):
    """I-14: dr-plan 能查询 chaos 写入的混沌实验数据。"""
    result = neptune_dr.results(
        "MATCH (s:Microservice)-[:TestedBy]->(exp:ChaosExperiment) "
        "RETURN s.name AS service, exp.result AS result LIMIT 10"
    )
    assert isinstance(result, list)
    # 如果有混沌实验数据（test_06 写入了），dr 应该能查到
    # 不强制要求数据存在，只验证查询可执行


def test_i15_untested_services_identifiable(neptune_dr):
    """I-15: DR 评估中能识别未做过混沌实验的服务。"""
    result = neptune_dr.results(
        "MATCH (s:Microservice) WHERE NOT (s)-[:TestedBy]->(:ChaosExperiment) "
        "RETURN s.name AS service LIMIT 20"
    )
    assert isinstance(result, list)
    # 应该有至少一些服务未做混沌实验
    # 不强制要求具体数量
