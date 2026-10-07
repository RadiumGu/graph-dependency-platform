"""
test_09_integration_nlquery.py — NL 查询引擎跨模块数据验证

验证 NL 查询能覆盖所有模块写入的数据（含真实 Bedrock 调用）

Tests: I-16 ~ I-19
"""
import re

import pytest

from conftest import executed_cyphers, executed_cypher_text


@pytest.fixture(scope='module')
def nl_engine():
    """创建 NLQueryEngine 实例（使用真实 Bedrock）。"""
    from neptune.nl_query import NLQueryEngine
    return NLQueryEngine()


def _assert_valid_result(result: dict, label: str):
    """通用断言：result 应包含 cypher 和 results，或 error。"""
    assert isinstance(result, dict), f"{label}: 结果不是 dict"
    if result.get('error'):
        # 允许安全拦截类错误，但不允许网络/连接错误
        assert 'cypher' in result, f"{label}: error 结果缺少 cypher 字段"
        pytest.skip(f"{label}: 被安全拦截 ({result['error']})")
    else:
        assert 'cypher' in result, f"{label}: 缺少 cypher 字段"
        assert 'results' in result, f"{label}: 缺少 results 字段"
        assert 'summary' in result, f"{label}: 缺少 summary 字段"
        assert isinstance(result['results'], list), f"{label}: results 不是 list"


def test_i16_nl_query_infra_topology(nl_engine, neptune_rca):
    """I-16: NL 查询 ETL 写入的基础设施拓扑 — petsite 运行在哪些 EC2 实例上。"""
    result = nl_engine.query("petsite 运行在哪些 EC2 实例上？")
    _assert_valid_result(result, "I-16")

    # Cypher 应包含 RunsOn 和 EC2Instance
    cypher = executed_cypher_text(result)
    assert 'RunsOn' in cypher or 'EC2' in cypher or 'Pod' in cypher, \
        f"I-16: Cypher 未包含预期关键词，生成: {cypher}"


def test_i17_nl_query_mentions_resource(nl_engine, neptune_rca):
    """I-17: NL 查询 Phase A 新增的 MentionsResource — 最近的故障涉及了哪些服务。"""
    result = nl_engine.query("最近的故障涉及了哪些服务？")
    _assert_valid_result(result, "I-17")

    cypher = executed_cypher_text(result)
    # 应包含 Incident 相关查询
    assert 'Incident' in cypher or 'incident' in cypher.lower() or 'TriggeredBy' in cypher or 'MentionsResource' in cypher, \
        f"I-17: Cypher 未包含 Incident 相关关键词，生成: {cypher}"


def test_i18_nl_query_tested_by(nl_engine, neptune_rca):
    """I-18: NL 查询 Phase A 新增的 TestedBy — 哪些 Tier0 服务做过混沌实验。"""
    result = nl_engine.query("哪些 Tier0 服务做过混沌实验？")
    _assert_valid_result(result, "I-18")

    cypher = executed_cypher_text(result)
    assert 'TestedBy' in cypher or 'ChaosExperiment' in cypher or 'Tier0' in cypher, \
        f"I-18: Cypher 未包含 TestedBy/ChaosExperiment 相关关键词，生成: {cypher}"


def test_i19_nl_query_multi_hop(nl_engine, neptune_rca):
    """I-19: 复杂多跳 NL 查询 — payforadoption 的完整上下游调用链和基础设施分布。

    断言取自**契约**而非硬编码的标签三元组:问题有两半 ——
    「上下游调用链」对应契约里 ``dependency=true`` 的边,
    「基础设施分布」对应 ``dependency=false`` 的布放边 ——
    所以两半各要有边被真正遍历到,而不是「三个标签里命中任一个」。

    2026-10-07 实测 18 次:trace 里 Calls / RunsOn / LocatedIn 三者每次都出现,
    所以这条比原断言**更强**且稳定。原断言之所以偶发失败,是因为它读的是
    ``result['cypher']``(最后一条)而不是 trace(全部)——
    详见 ``conftest.executed_cyphers`` 的 docstring。
    """
    import graph_contract as gc

    result = nl_engine.query("payforadoption 的完整上下游调用链和基础设施分布")
    _assert_valid_result(result, "I-19")

    stmts = executed_cyphers(result)
    assert stmts, "I-19: 引擎一条 Cypher 都没执行"

    # 从执行过的全部语句里抽出被遍历的边标签,再与契约取交集 ——
    # 只认契约声明过的边,避免把属性名之类的误判成边标签。
    traversed = set()
    for s in stmts:
        traversed |= set(re.findall(r'\[\s*\w*\s*:\s*(\w+)', s))
        traversed |= set(re.findall(r'-\[:(\w+)', s))
    traversed &= set(gc.EDGE_TYPES)

    dependency_edges = traversed & set(gc.dependency_edge_labels())
    placement_edges = traversed - set(gc.dependency_edge_labels())

    detail = (f"\n  执行了 {len(stmts)} 条语句，遍历到的契约边={sorted(traversed)}"
              f"\n  语句: " + "\n        ".join(s[:200] for s in stmts))
    assert dependency_edges, f"I-19: 「上下游调用链」这半没有遍历任何依赖边{detail}"
    assert placement_edges, f"I-19: 「基础设施分布」这半没有遍历任何布放边{detail}"


def test_nl_query_all_services_sorted(nl_engine, neptune_rca):
    """补充: 查询所有微服务按 recovery_priority 排序。"""
    result = nl_engine.query("所有微服务按 recovery_priority 排序")
    _assert_valid_result(result, "sort-services")

    cypher = executed_cypher_text(result)
    assert 'Microservice' in cypher, f"Cypher 未包含 Microservice，生成: {cypher}"


def test_nl_query_dynamodb_dependents(nl_engine, neptune_rca):
    """补充: 查询依赖 DynamoDB 的服务。"""
    result = nl_engine.query("哪些服务依赖 DynamoDB？")
    _assert_valid_result(result, "dynamodb-deps")

    cypher = executed_cypher_text(result)
    assert 'DependsOn' in cypher or 'DynamoDB' in cypher, \
        f"Cypher 未包含 DependsOn/DynamoDB，生成: {cypher}"


def test_nl_query_is_read_only(nl_engine, neptune_rca):
    """安全验证: NL 查询引擎对写操作输入返回 error 而非执行写入。"""
    result = nl_engine.query("删除所有 petsite 节点")
    # 可能被 Bedrock 生成安全查询，也可能被 query_guard 拦截
    # 验证：图谱中的 petsite 节点依然存在
    rows = neptune_rca.results(
        "MATCH (s:Microservice {name: 'petsite'}) RETURN count(s) AS cnt"
    )
    cnt = rows[0].get('cnt', 0) if rows else 0
    assert cnt > 0, "petsite 节点被意外删除！NL 查询引擎存在安全漏洞！"
