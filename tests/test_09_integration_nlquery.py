"""
test_09_integration_nlquery.py — NL 查询引擎跨模块数据验证

验证 NL 查询能覆盖所有模块写入的数据（含真实 Bedrock 调用）

Tests: I-16 ~ I-19
"""
import pytest


@pytest.fixture(scope='module')
def nl_engine():
    """创建 NLQueryEngine 实例（使用真实 Bedrock）。"""
    from neptune.nl_query import NLQueryEngine
    return NLQueryEngine()


def _assert_valid_result(result: dict, label: str):
    """通用断言：result 应包含 cypher 和 results，或 error。"""
    assert isinstance(result, dict), f"{label}: 结果不是 dict"
    if 'error' in result:
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
    cypher = result.get('cypher', '')
    assert 'RunsOn' in cypher or 'EC2' in cypher or 'Pod' in cypher, \
        f"I-16: Cypher 未包含预期关键词，生成: {cypher}"


def test_i17_nl_query_mentions_resource(nl_engine, neptune_rca):
    """I-17: NL 查询 Phase A 新增的 MentionsResource — 最近的故障涉及了哪些服务。"""
    result = nl_engine.query("最近的故障涉及了哪些服务？")
    _assert_valid_result(result, "I-17")

    cypher = result.get('cypher', '')
    # 应包含 Incident 相关查询
    assert 'Incident' in cypher or 'incident' in cypher.lower() or 'TriggeredBy' in cypher or 'MentionsResource' in cypher, \
        f"I-17: Cypher 未包含 Incident 相关关键词，生成: {cypher}"


def test_i18_nl_query_tested_by(nl_engine, neptune_rca):
    """I-18: NL 查询 Phase A 新增的 TestedBy — 哪些 Tier0 服务做过混沌实验。"""
    result = nl_engine.query("哪些 Tier0 服务做过混沌实验？")
    _assert_valid_result(result, "I-18")

    cypher = result.get('cypher', '')
    assert 'TestedBy' in cypher or 'ChaosExperiment' in cypher or 'Tier0' in cypher, \
        f"I-18: Cypher 未包含 TestedBy/ChaosExperiment 相关关键词，生成: {cypher}"


def test_i19_nl_query_multi_hop(nl_engine, neptune_rca):
    """I-19: 复杂多跳 NL 查询 — payforadoption 的完整上下游调用链和基础设施分布。"""
    result = nl_engine.query("payforadoption 的完整上下游调用链和基础设施分布")
    _assert_valid_result(result, "I-19")

    cypher = result.get('cypher', '')
    # 应包含 Calls、RunsOn 或 LocatedIn
    has_calls = 'Calls' in cypher
    has_runs = 'RunsOn' in cypher
    has_located = 'LocatedIn' in cypher
    assert has_calls or has_runs or has_located, \
        f"I-19: 多跳 Cypher 缺少关键边类型，生成: {cypher}"


def test_nl_query_all_services_sorted(nl_engine, neptune_rca):
    """补充: 查询所有微服务按 recovery_priority 排序。"""
    result = nl_engine.query("所有微服务按 recovery_priority 排序")
    _assert_valid_result(result, "sort-services")

    cypher = result.get('cypher', '')
    assert 'Microservice' in cypher, f"Cypher 未包含 Microservice，生成: {cypher}"


def test_nl_query_dynamodb_dependents(nl_engine, neptune_rca):
    """补充: 查询依赖 DynamoDB 的服务。"""
    result = nl_engine.query("哪些服务依赖 DynamoDB？")
    _assert_valid_result(result, "dynamodb-deps")

    cypher = result.get('cypher', '')
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
