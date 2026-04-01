"""
test_02_unit_rca.py — rca 模块单元测试

Tests: U-A1-01 ~ U-A1-07, U-A3-01 ~ U-A3-04, U-B1-01 ~ U-B1-02
"""
import sys
import os
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = '/home/ubuntu/tech/graph-dependency-platform'


# ─── U-A1: _extract_entities（纯函数）────────────────────────────────────────

def test_ua1_01_extract_known_services():
    """U-A1-01: 从 RCA 报告提取已知服务名。"""
    from actions.incident_writer import _extract_entities
    text = 'petsite 服务因 DynamoDB 限流导致 5xx，影响 petsearch 下游'
    entities = _extract_entities(text)
    names = [e['name'] for e in entities if e['type'] == 'Microservice']
    assert 'petsite' in names
    assert 'petsearch' in names


def test_ua1_02_extract_ec2_instance_id():
    """U-A1-02: 从 RCA 报告提取 EC2 instance ID。"""
    from actions.incident_writer import _extract_entities
    text = '节点 i-0abc1234def56789 出现 CPU 飙高'
    entities = _extract_entities(text)
    ec2_entities = [e for e in entities if e['type'] == 'EC2Instance']
    ec2_ids = [e['id'] for e in ec2_entities]
    assert 'i-0abc1234def56789' in ec2_ids


def test_ua1_03_empty_report_text():
    """U-A1-03: 空报告文本返回 []，不抛异常。"""
    from actions.incident_writer import _extract_entities
    result = _extract_entities("")
    assert result == []


def test_ua1_04_no_recognizable_entity():
    """U-A1-04: 无可识别实体时返回 []。"""
    from actions.incident_writer import _extract_entities
    result = _extract_entities("系统运行正常，无异常")
    assert result == []


def test_ua1_05_case_sensitivity():
    """U-A1-05: 实体提取区分大小写（CANONICAL 中为小写）。"""
    from actions.incident_writer import _extract_entities
    # 'PetSite'（大写P和S）不匹配 CANONICAL 中的 'petsite'
    result_upper = _extract_entities("PetSite 发生故障")
    names_upper = [e['name'] for e in result_upper if e['type'] == 'Microservice']
    assert 'petsite' not in names_upper  # 大小写不匹配

    # 小写精确匹配
    result_lower = _extract_entities("petsite 发生故障")
    names_lower = [e['name'] for e in result_lower if e['type'] == 'Microservice']
    assert 'petsite' in names_lower


def test_ua1_06_link_entities_neptune_failure():
    """U-A1-06: _link_entities_to_incident Neptune 写入失败，不抛异常（logger.warning）。"""
    from actions.incident_writer import _link_entities_to_incident
    entities = [{'type': 'Microservice', 'name': 'petsite'}]

    with patch('neptune.neptune_client.results', side_effect=Exception("Neptune timeout")):
        # 不应抛异常
        try:
            _link_entities_to_incident('test-inc-001', entities)
        except Exception as e:
            pytest.fail(f"_link_entities_to_incident raised unexpectedly: {e}")


def test_ua1_07_vector_index_failure_non_fatal(neptune_rca):
    """U-A1-07: 向量索引写入失败时，write_incident() 正常完成（non-fatal）。"""
    from actions.incident_writer import write_incident

    classification = {'affected_service': 'petsite', 'severity': 'P2'}
    rca_result = {'top_candidate': {'service': 'petsite', 'confidence': 0.5}}

    with patch('search.incident_vectordb.index_incident', side_effect=Exception("S3 Vectors unavailable")):
        inc_id = write_incident(
            classification=classification,
            rca_result=rca_result,
            resolution='test resolution',
            report_text='petsite 测试报告，向量索引模拟失败',
        )
    assert inc_id is not None and inc_id.startswith('inc-')

    # 清理测试节点
    neptune_rca.results("MATCH (n:Incident {id: $id}) DETACH DELETE n", {'id': inc_id})


# ─── U-A3: Q17/Q18 Neptune 查询 ──────────────────────────────────────────────

def test_ua3_01_q17_existing_service(neptune_rca):
    """U-A3-01: Q17 查询存在的服务，返回正确字段结构。"""
    from neptune import neptune_queries as nq
    result = nq.q17_incidents_by_resource('petsite')
    assert isinstance(result, list)
    # 如果有结果，验证字段结构
    for row in result:
        assert isinstance(row, dict)
        # 以下字段应存在（值可能为 None）
        assert 'id' in row
        assert 'severity' in row
        assert 'root_cause' in row
        assert 'resolution' in row
        assert 'start_time' in row


def test_ua3_02_q17_nonexistent_service(neptune_rca):
    """U-A3-02: Q17 查询不存在的服务，返回 []，不抛异常。"""
    from neptune import neptune_queries as nq
    result = nq.q17_incidents_by_resource('nonexistent-service-xyz')
    assert result == []


def test_ua3_03_q18_chaos_history(neptune_rca):
    """U-A3-03: Q18 查询服务混沌实验历史，返回正确字段结构。"""
    from neptune import neptune_queries as nq
    result = nq.q18_chaos_history('petsite')
    assert isinstance(result, list)
    for row in result:
        assert isinstance(row, dict)
        assert 'id' in row
        assert 'fault_type' in row
        assert 'result' in row
        assert 'recovery_time' in row
        assert 'degradation' in row
        assert 'timestamp' in row


def test_ua3_04_q17_limit_respected(neptune_rca):
    """U-A3-04: Q17 limit 参数生效。"""
    from neptune import neptune_queries as nq
    result = nq.q17_incidents_by_resource('petsite', limit=2)
    assert isinstance(result, list)
    assert len(result) <= 2


# ─── U-B1: Schema Prompt ─────────────────────────────────────────────────────

def test_ub1_01_build_system_prompt():
    """U-B1-01: build_system_prompt() 返回完整 prompt，包含节点/边/示例。"""
    from neptune.schema_prompt import build_system_prompt, GRAPH_SCHEMA, FEW_SHOT_EXAMPLES
    prompt = build_system_prompt()
    assert isinstance(prompt, str)
    assert len(prompt) > 500
    # 包含节点类型
    assert 'Microservice' in prompt
    assert 'ChaosExperiment' in prompt
    assert 'Incident' in prompt
    # 包含边类型
    assert 'TestedBy' in prompt
    assert 'MentionsResource' in prompt
    # 包含 few-shot 示例
    assert len(FEW_SHOT_EXAMPLES) >= 6
    for ex in FEW_SHOT_EXAMPLES:
        assert ex['q'] in prompt or ex['cypher'] in prompt


def test_ub1_02_few_shot_cypher_safe():
    """U-B1-02: FEW_SHOT_EXAMPLES 中的所有 Cypher 都通过 query_guard.is_safe()。"""
    from neptune.schema_prompt import FEW_SHOT_EXAMPLES
    from neptune.query_guard import is_safe
    for ex in FEW_SHOT_EXAMPLES:
        safe, reason = is_safe(ex['cypher'])
        assert safe, f"Unsafe example cypher for '{ex['q']}': {reason}\nCypher: {ex['cypher']}"
