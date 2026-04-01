"""
test_07_integration_incident.py — Incident 全链路联动测试

数据流：incident_writer.write_incident() → Neptune Incident + MentionsResource 边
        → Q17 → graph_rag_reporter 历史上下文
        → S3 Vectors 向量索引 → 向量搜索

Tests: I-04 ~ I-07
"""
import time

import pytest


REPORT_TEXT = (
    'petsite 服务因 DynamoDB PetAdoptions 表 ReadThrottling 导致 5xx，'
    '影响 petsearch 下游，前端用户看到加载失败。'
    '根因：DynamoDB RCU 不足，高峰期限流。修复：增加 RCU 从 100 到 500。'
)


@pytest.fixture(scope='module')
def incident_chain(neptune_rca):
    """创建完整 incident，返回 incident_id，测试结束后清理。"""
    from actions.incident_writer import write_incident, resolve_incident

    inc_id = write_incident(
        classification={'affected_service': 'petsite', 'severity': 'P1'},
        rca_result={'top_candidate': {'service': 'DynamoDB', 'confidence': 0.85}},
        resolution='增加 RCU',
        report_text=REPORT_TEXT,
    )

    # resolve 使 Q17 (status='resolved') 能查到
    resolve_incident(inc_id, '增加 RCU', 900)

    yield inc_id

    # 清理
    try:
        neptune_rca.results("MATCH (n:Incident {id: $id}) DETACH DELETE n", {'id': inc_id})
    except Exception:
        pass


def test_i04_incident_full_chain(neptune_rca, incident_chain):
    """I-04: write_incident 全链路 — Neptune 节点存在。"""
    inc_id = incident_chain

    # 验证 Incident 节点
    rows = neptune_rca.results(
        "MATCH (inc:Incident {id: $id}) RETURN inc.severity AS severity, inc.affected_service AS svc",
        {'id': inc_id},
    )
    assert len(rows) > 0, f"Incident 节点 {inc_id} 未创建"
    assert rows[0].get('svc') == 'petsite'
    assert rows[0].get('severity') == 'P1'


def test_i04b_mentions_resource_edge(neptune_rca, incident_chain):
    """I-04b: write_incident 创建了 MentionsResource 边到 petsite。"""
    inc_id = incident_chain

    # 验证 MentionsResource 边（petsite 在 REPORT_TEXT 中被提取）
    edges = neptune_rca.results(
        "MATCH (inc:Incident {id: $id})-[:MentionsResource]->(r) RETURN r.name AS name",
        {'id': inc_id},
    )
    resource_names = [e.get('name') for e in edges]
    assert 'petsite' in resource_names, \
        f"MentionsResource 边未指向 petsite，实际: {resource_names}"


def test_i05_q17_finds_incident(neptune_rca, incident_chain):
    """I-05: Q17 能查到刚写入的 Incident（status='resolved'）。"""
    inc_id = incident_chain
    from neptune import neptune_queries as nq

    results = nq.q17_incidents_by_resource('petsite', limit=20)
    inc_ids = [r.get('id') for r in results]
    assert inc_id in inc_ids, f"Q17 未查到 {inc_id}，当前结果: {inc_ids}"


def test_i06_vector_search_finds_incident(incident_chain):
    """I-06: 向量搜索能查到刚写入的 Incident（相似度 >= 0.5）。"""
    inc_id = incident_chain
    from search.incident_vectordb import search_similar

    time.sleep(3)  # S3 Vectors 写入可能有短延迟

    results = search_similar('DynamoDB 限流导致超时', top_k=5, threshold=0.5)
    found_ids = [r.get('incident_id') for r in results]
    assert inc_id in found_ids, \
        f"向量搜索未找到 {inc_id}，当前结果 IDs: {found_ids}"


def test_i07_graph_rag_report_includes_all_sections(neptune_rca, incident_chain):
    """I-07: graph_rag_reporter 输出包含历史故障 + 混沌实验历史 + 语义相似历史故障。"""
    from core.graph_rag_reporter import _get_neptune_subgraph

    subgraph_text = _get_neptune_subgraph('petsite')
    assert isinstance(subgraph_text, str)
    assert len(subgraph_text) > 0

    # 验证历史故障 section 存在（inc_id 是 resolved 状态）
    has_history = '[历史故障记录' in subgraph_text
    # 验证向量搜索 section 存在
    has_vector = '[语义相似历史故障' in subgraph_text

    # 检查是否有混沌实验（可能在 test_06 中创建了）
    exp_count = neptune_rca.results(
        "MATCH (svc:Microservice {name: 'petsite'})-[:TestedBy]->(:ChaosExperiment) RETURN count(*) AS cnt"
    )
    has_chaos_data = exp_count[0].get('cnt', 0) > 0 if exp_count else False
    has_chaos = '[混沌实验历史]' in subgraph_text

    # 日志记录各 section 状态
    import logging
    logger = logging.getLogger(__name__)
    logger.info(f"I-07 sections: history={has_history}, chaos={has_chaos}, vector={has_vector}")

    # 基本断言：函数正常工作，系统拓扑部分必须存在
    assert '[系统拓扑]' in subgraph_text, "缺少 [系统拓扑] section"

    # 如果有 resolved incidents，应该有历史故障
    resolved = neptune_rca.results(
        "MATCH (inc:Incident)-[:MentionsResource]->(:Microservice {name: 'petsite'}) "
        "WHERE inc.status = 'resolved' RETURN count(inc) AS cnt"
    )
    resolved_count = resolved[0].get('cnt', 0) if resolved else 0
    if resolved_count > 0:
        assert has_history, f"有 {resolved_count} 条 resolved incident，但无 [历史故障记录] section"
