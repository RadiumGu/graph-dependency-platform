"""
test_06_integration_chaos_rca.py — chaos ↔ rca 联动测试

数据流：chaos/neptune_sync → Neptune ChaosExperiment 节点 → rca/neptune_queries Q18

Tests: I-01 ~ I-03
"""
import pytest


EXP_ID = 'test-auto-chaos-int-01'
EXP_ID_UPDATE = 'test-auto-chaos-int-02'


@pytest.fixture(scope='module', autouse=True)
def cleanup_chaos_integration(neptune_rca):
    """清理集成测试数据。"""
    yield
    for eid in [EXP_ID, EXP_ID_UPDATE]:
        try:
            neptune_rca.results(
                "MATCH (n:ChaosExperiment {experiment_id: $id}) DETACH DELETE n",
                {'id': eid},
            )
        except Exception:
            pass


def test_i01_chaos_write_then_rca_q18(neptune_rca):
    """I-01: chaos write_experiment → rca Q18 可查到写入的记录。"""
    from neptune_sync import write_experiment
    from neptune import neptune_queries as nq

    # Step 1: chaos 写入实验
    write_experiment({
        'experiment_id': EXP_ID,
        'target_service': 'petsite',
        'fault_type': 'pod-kill',
        'result': 'passed',
        'recovery_time_sec': 45,
        'degradation_rate': 0.12,
        'timestamp': '2026-04-01T00:00:00Z',
    })

    # Step 2: rca Q18 查询
    results = nq.q18_chaos_history('petsite', limit=20)
    exp_ids = [r.get('id') for r in results]
    assert EXP_ID in exp_ids, f"Q18 未查到 {EXP_ID}，当前结果: {exp_ids}"


def test_i02_chaos_update_then_rca_reads_latest(neptune_rca):
    """I-02: 先写入 result='failed'，再更新为 result='passed'，Q18 返回最新值。"""
    from neptune_sync import write_experiment
    from neptune import neptune_queries as nq

    # 写入 failed
    write_experiment({
        'experiment_id': EXP_ID_UPDATE,
        'target_service': 'petsite',
        'fault_type': 'network-delay',
        'result': 'failed',
        'recovery_time_sec': 120,
        'degradation_rate': 0.30,
        'timestamp': '2026-04-01T01:00:00Z',
    })

    # 更新为 passed
    write_experiment({
        'experiment_id': EXP_ID_UPDATE,
        'target_service': 'petsite',
        'fault_type': 'network-delay',
        'result': 'passed',
        'recovery_time_sec': 60,
        'degradation_rate': 0.10,
        'timestamp': '2026-04-01T01:30:00Z',
    })

    # Q18 应返回最新值
    results = nq.q18_chaos_history('petsite', limit=20)
    matching = [r for r in results if r.get('id') == EXP_ID_UPDATE]
    assert len(matching) == 1, f"Q18 结果中应有且仅有 1 条 {EXP_ID_UPDATE}"
    assert matching[0].get('result') == 'passed', \
        f"Q18 应返回最新 result='passed'，实际: {matching[0].get('result')}"


def test_i03_graph_rag_includes_chaos_history(neptune_rca):
    """I-03: graph_rag_reporter._get_neptune_subgraph 输出包含 [混沌实验历史] section。"""
    # 确保 petsite 有 TestedBy 边（来自 test_i01）
    from core.graph_rag_reporter import _get_neptune_subgraph

    subgraph_text = _get_neptune_subgraph('petsite')
    assert isinstance(subgraph_text, str)
    # 有混沌实验历史时应包含该 section
    # （如果 Q18 有结果，_get_neptune_subgraph 会添加此 section）
    # 验证函数能正常返回而不抛异常
    assert len(subgraph_text) > 0

    # 检查混沌实验历史 section（依赖 petsite 有实验记录）
    rows = neptune_rca.results(
        "MATCH (svc:Microservice {name: 'petsite'})-[:TestedBy]->(exp:ChaosExperiment) RETURN count(exp) AS cnt"
    )
    exp_count = rows[0].get('cnt', 0) if rows else 0
    if exp_count > 0:
        assert '[混沌实验历史]' in subgraph_text, \
            f"有 {exp_count} 条混沌实验，但报告中没有 [混沌实验历史] section"
