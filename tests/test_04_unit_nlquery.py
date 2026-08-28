"""
test_04_unit_nlquery.py — NL 查询引擎单元测试

Tests: U-B2-01 ~ U-B2-04
"""
import io
import json
from unittest.mock import MagicMock, patch

import pytest


def _mock_bedrock_response(cypher_text: str) -> dict:
    """构造 Bedrock invoke_model 的模拟响应（dict + BytesIO body）。"""
    body_content = json.dumps({
        "content": [{"text": cypher_text}]
    }).encode()
    return {'body': io.BytesIO(body_content)}


def _make_engine_with_mock_bedrock(cypher_for_query: str, summary_text: str = "查询完成"):
    """创建 NLQueryEngine，注入 mock Bedrock (BytesIO body)。"""
    from neptune.nl_query import NLQueryEngine

    engine = NLQueryEngine.__new__(NLQueryEngine)
    from neptune.schema_prompt import build_system_prompt
    engine.system_prompt = build_system_prompt()

    call_count = [0]

    def mock_invoke(**kwargs):
        call_count[0] += 1
        if call_count[0] == 1:
            return _mock_bedrock_response(cypher_for_query)
        else:
            return _mock_bedrock_response(summary_text)

    mock_bedrock = MagicMock()
    mock_bedrock.invoke_model = mock_invoke
    engine.bedrock = mock_bedrock
    return engine


def test_ub2_01_basic_query_returns_structure(neptune_rca):
    """U-B2-01: 基本自然语言查询返回正确结构，cypher 包含预期关键词。"""
    cypher = "MATCH (s:Microservice {name:'petsite'})-[:DependsOn]->(db) WHERE db:RDSCluster OR db:DynamoDB RETURN db.name AS database, labels(db)[0] AS type LIMIT 50"
    engine = _make_engine_with_mock_bedrock(cypher, "petsite 依赖若干数据库。")

    result = engine.query("petsite 依赖哪些数据库？")

    assert 'question' in result
    assert 'cypher' in result
    assert 'results' in result
    assert 'summary' in result
    assert result['question'] == "petsite 依赖哪些数据库？"
    assert 'DependsOn' in result['cypher']


def test_ub2_02_empty_results(neptune_rca):
    """U-B2-02: 查询结果为空时，summary 包含"无结果"语义。"""
    # 查询一个肯定无结果的时间范围
    cypher = "MATCH (inc:Incident) WHERE inc.start_time >= '2019-01-01' AND inc.start_time <= '2019-12-31' RETURN inc.id AS id LIMIT 50"
    engine = _make_engine_with_mock_bedrock(cypher)

    result = engine.query("2019年发生了什么故障？")
    assert isinstance(result.get('results'), list)
    # 当 results 为空时，_summarize 直接返回 "查询无结果。"
    if len(result.get('results', [])) == 0:
        assert '无结果' in result.get('summary', '') or result.get('summary') == '查询无结果。'


def test_ub2_03_bedrock_timeout_returns_error(caplog):
    """U-B2-03: Bedrock 超时时,query() 返回带 error 的结果并记日志(不静默)。

    2026-08-28 更正:本测试原名 ``..._raises``,断言异常会穿过 query() 传播,
    注释还写着「_generate_cypher has no try/except」—— 那是 PR2(Strands L1 POC)
    迁移**之前**的实现细节。迁移到 nl_query_direct.DirectBedrockNLQuery 之后,
    query() 刻意做了错误收敛:

        try:
            cypher = self._generate_cypher(question)
        except Exception as e:
            logger.warning(f"NLQuery cypher generation failed: {e}")
            return _base_return({"error": str(e)})

    这**不是**静默吞异常 —— 既 logger.warning,又把 error 放进返回值。
    且实测两个真实调用方都遵守该契约:
      rca/scripts/graph-ask.py     if 'error' in result: print + sys.exit(1)
      demo/pages/2_Smart_Query.py  if result.get("error"): st.error(...)

    NLQuery 会被 RCA 热路径调用,一次 Bedrock 抖动不该让整轮 RCA 崩掉,
    所以收敛成结构化错误是正确设计。改为断言**契约**(返回 error + 记日志)
    而非**实现**(异常传播)—— 契约才是调用方依赖的东西。
    """
    import logging

    from neptune.nl_query import NLQueryEngine

    engine = NLQueryEngine.__new__(NLQueryEngine)
    from neptune.schema_prompt import build_system_prompt
    engine.system_prompt = build_system_prompt()

    mock_bedrock = MagicMock()
    mock_bedrock.invoke_model = MagicMock(side_effect=Exception("Connection timeout"))
    engine.bedrock = mock_bedrock

    with caplog.at_level(logging.WARNING):
        result = engine.query("petsite 依赖哪些数据库？")

    # 契约 1:错误出现在返回值里,调用方能判断
    assert 'error' in result, f"query() 应返回 error 字段，实际: {sorted(result)}"
    assert 'Connection timeout' in result['error'], result['error']
    # 契约 2:没有伪装成成功 —— 不能返回一个看起来正常的空结果
    assert not result.get('results'), f"失败时不应返回结果集: {result.get('results')}"
    # 契约 3:失败可见于日志,不是静默
    assert any('Connection timeout' in r.message or 'Connection timeout' in str(r.msg)
               for r in caplog.records), "失败应记 warning 日志，实际日志为空"


def test_ub2_04_unsafe_cypher_blocked(neptune_rca):
    """U-B2-04: LLM 生成不安全的查询时，query_guard 拦截并返回 error。"""
    # Mock Bedrock 返回一个写操作查询
    unsafe_cypher = "MATCH (n:Microservice {name: 'petsite'}) DELETE n"
    engine = _make_engine_with_mock_bedrock(unsafe_cypher)

    result = engine.query("删除所有 petsite 节点")
    assert 'error' in result
    assert 'cypher' in result
    # 错误原因应包含写操作拦截信息
    assert result['error']


def test_ub2_04_safe_cypher_passes(neptune_rca):
    """U-B2-04 补充: 安全 cypher 正常通过并执行。"""
    cypher = "MATCH (s:Microservice {name:'petsite'}) RETURN s.name AS name LIMIT 10"
    engine = _make_engine_with_mock_bedrock(cypher, "petsite 微服务存在于图谱中。")

    result = engine.query("petsite 是什么服务？")
    assert 'error' not in result
    assert isinstance(result.get('results'), list)


def test_generate_cypher_strips_markdown():
    """_generate_cypher 能正确去除 markdown 代码块包裹。"""
    from neptune.nl_query import NLQueryEngine

    engine = NLQueryEngine.__new__(NLQueryEngine)
    from neptune.schema_prompt import build_system_prompt
    engine.system_prompt = build_system_prompt()

    # Mock 返回带 markdown 代码块的 Cypher
    cypher_with_md = "```cypher\nMATCH (n) RETURN n LIMIT 10\n```"
    mock_bedrock = MagicMock()
    mock_bedrock.invoke_model = lambda **kwargs: _mock_bedrock_response(cypher_with_md)
    engine.bedrock = mock_bedrock

    result = engine._generate_cypher("测试查询")
    assert not result.startswith('```')
    assert 'MATCH' in result
