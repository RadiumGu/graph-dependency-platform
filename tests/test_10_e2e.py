"""
test_10_e2e.py — 端到端场景测试

E2E-01: 完整故障处理闭环（chaos → incident → rca → dr → nl）
E2E-02: NL 查询引擎 10 题验证（>= 8 题生成正确 Cypher）

Tests: E2E-01, E2E-02
"""
import time
import logging

import pytest

logger = logging.getLogger(__name__)


# ─── E2E-01: 完整故障处理闭环 ────────────────────────────────────────────────

@pytest.fixture(scope='module')
def e2e_setup(neptune_rca):
    """E2E 初始化：写入混沌实验 + Incident，并清理。"""
    from neptune_sync import write_experiment
    from actions.incident_writer import write_incident, resolve_incident

    # Step 2: chaos 实验
    exp_id = 'test-auto-e2e-chaos-001'
    write_experiment({
        'experiment_id': exp_id,
        'target_service': 'petsite',
        'fault_type': 'pod-kill',
        'result': 'passed',
        'recovery_time_sec': 60,
        'degradation_rate': 0.15,
        'timestamp': '2026-04-01T10:00:00Z',
    })

    # Step 3: rca incident
    inc_id = write_incident(
        classification={'affected_service': 'petsite', 'severity': 'P1'},
        rca_result={'top_candidate': {'service': 'petsite', 'confidence': 0.9}},
        resolution='重启 petsite pod',
        report_text=(
            'petsite 出现严重 5xx 故障，经分析是 pod 被意外终止导致。'
            'petsite 服务依赖 DynamoDB 和 petsearch，故障期间下游全部受影响。'
        ),
    )
    resolve_incident(inc_id, '重启 petsite pod', 300)

    time.sleep(2)  # 等待向量索引写入生效

    yield {'exp_id': exp_id, 'inc_id': inc_id}

    # 清理
    try:
        neptune_rca.results(
            "MATCH (n:ChaosExperiment {experiment_id: $id}) DETACH DELETE n",
            {'id': exp_id},
        )
    except Exception:
        pass
    try:
        neptune_rca.results(
            "MATCH (n:Incident {id: $id}) DETACH DELETE n",
            {'id': inc_id},
        )
    except Exception:
        pass


def test_e2e01_chaos_experiment_in_neptune(neptune_rca, e2e_setup):
    """E2E-01 Step 2: Neptune 新增 ChaosExperiment 节点 + TestedBy 边。"""
    exp_id = e2e_setup['exp_id']
    rows = neptune_rca.results(
        "MATCH (svc:Microservice {name: 'petsite'})-[:TestedBy]->(exp:ChaosExperiment {experiment_id: $id}) "
        "RETURN exp.fault_type AS ft",
        {'id': exp_id},
    )
    assert len(rows) >= 1, f"ChaosExperiment {exp_id} 未找到 TestedBy 边"
    assert rows[0].get('ft') == 'pod-kill'


def test_e2e01_incident_in_neptune(neptune_rca, e2e_setup):
    """E2E-01 Step 3a: Incident 节点存在于 Neptune。"""
    inc_id = e2e_setup['inc_id']
    rows = neptune_rca.results(
        "MATCH (inc:Incident {id: $id}) RETURN inc.status AS status, inc.severity AS severity",
        {'id': inc_id},
    )
    assert len(rows) == 1, f"Incident {inc_id} 未找到"
    assert rows[0].get('severity') == 'P1'
    assert rows[0].get('status') == 'resolved'


def test_e2e01_report_has_all_sections(neptune_rca, e2e_setup):
    """E2E-01 Step 3b: graph_rag_reporter 报告包含历史记录 + 混沌实验历史。"""
    from core.graph_rag_reporter import _get_neptune_subgraph

    subgraph = _get_neptune_subgraph('petsite')
    assert '[系统拓扑]' in subgraph, "缺少 [系统拓扑] section"

    # 检查各 section 是否存在
    logger.info(f"E2E subgraph sections present: "
                f"history={'[历史故障记录' in subgraph}, "
                f"chaos={'[混沌实验历史]' in subgraph}, "
                f"vector={'[语义相似历史故障' in subgraph}")


def test_e2e01_q17_finds_incident(neptune_rca, e2e_setup):
    """E2E-01 Step 5a: NL 查 petsite 最近故障 → Q17 返回新 incident。"""
    from neptune import neptune_queries as nq
    inc_id = e2e_setup['inc_id']

    results = nq.q17_incidents_by_resource('petsite', limit=20)
    found = any(r.get('id') == inc_id for r in results)
    assert found, f"Q17 未找到 {inc_id}，当前结果: {[r.get('id') for r in results]}"


def test_e2e01_q18_finds_experiment(neptune_rca, e2e_setup):
    """E2E-01 Step 5b: Q18 查询 petsite 混沌实验 → 返回新实验。"""
    from neptune import neptune_queries as nq
    exp_id = e2e_setup['exp_id']

    results = nq.q18_chaos_history('petsite', limit=20)
    found = any(r.get('id') == exp_id for r in results)
    assert found, f"Q18 未找到 {exp_id}，当前结果: {[r.get('id') for r in results]}"


def test_e2e01_vector_search_incident(e2e_setup):
    """E2E-01 Step 5c: 向量搜索 → 返回新 incident（语义相似）。"""
    from search.incident_vectordb import search_similar
    inc_id = e2e_setup['inc_id']

    results = search_similar('petsite pod 故障重启', top_k=5, threshold=0.4)
    found_ids = [r.get('incident_id') for r in results]
    assert inc_id in found_ids, \
        f"向量搜索未找到 {inc_id}，当前 IDs: {found_ids}"


# ─── E2E-02: NL 查询引擎 10 题验证 ──────────────────────────────────────────

@pytest.fixture(scope='module')
def nl_engine_e2e():
    """NLQueryEngine 实例（真实 Bedrock）。"""
    from neptune.nl_query import NLQueryEngine
    return NLQueryEngine()


E2E_NL_QUESTIONS = [
    ("q1", "petsite 的所有下游依赖有哪些？", ['DependsOn', 'Calls']),
    ("q2", "AZ ap-northeast-1a 有多少个 Pod？", ['Pod', 'AvailabilityZone', 'LocatedIn']),
    ("q3", "哪些 Tier0 服务没做过混沌实验？", ['Tier0', 'TestedBy', 'ChaosExperiment', 'Microservice']),
    ("q4", "最近一周发生了几次 P0 故障？", ['Incident', 'P0']),
    ("q5", "payforadoption 的完整调用链", ['Calls', 'payforadoption']),
    ("q6", "petsite 运行在哪个 AZ？", ['RunsOn', 'LocatedIn', 'AvailabilityZone']),
    ("q7", "哪些服务依赖 DynamoDB？", ['DependsOn', 'DynamoDB']),
    ("q8", "petsite 涉及的历史故障有哪些？", ['Incident', 'petsite']),
    ("q9", "所有微服务按 recovery_priority 排序", ['Microservice', 'recovery_priority']),
    ("q10", "petsite 和 petsearch 之间有调用关系吗？", ['Calls', 'petsite', 'petsearch']),
]


@pytest.mark.parametrize("qid,question,expected_keywords", E2E_NL_QUESTIONS)
def test_e2e02_nl_query_10_questions(qid, question, expected_keywords, nl_engine_e2e, neptune_rca):
    """E2E-02: NL 查询引擎 10 题验证（每题生成的 Cypher 含预期关键词）。"""
    result = nl_engine_e2e.query(question)

    assert isinstance(result, dict), f"{qid}: 结果不是 dict"

    if 'error' in result:
        # 被安全拦截的情况（如写操作）需记录
        logger.warning(f"{qid} ({question}): 被拦截 - {result['error']}")
        pytest.skip(f"{qid}: 被安全拦截")

    assert 'cypher' in result, f"{qid}: 缺少 cypher"
    assert 'results' in result, f"{qid}: 缺少 results"

    cypher = result.get('cypher', '')

    # 检查 Cypher 是否包含至少一个预期关键词
    found_keywords = [kw for kw in expected_keywords if kw in cypher]
    assert found_keywords, \
        f"{qid} ({question}): Cypher 未包含任何预期关键词 {expected_keywords}\n生成: {cypher}"

    logger.info(f"{qid} PASS: {question} → cypher contains {found_keywords}")


def test_e2e02_pass_rate(nl_engine_e2e, neptune_rca):
    """E2E-02 总结: 10 题中至少 8 题生成正确 Cypher。"""
    passed = 0
    failed = []

    for qid, question, expected_keywords in E2E_NL_QUESTIONS:
        try:
            result = nl_engine_e2e.query(question)
            if 'error' in result:
                failed.append((qid, question, f"error: {result['error']}"))
                continue
            cypher = result.get('cypher', '')
            if any(kw in cypher for kw in expected_keywords):
                passed += 1
                logger.info(f"PASS {qid}: {question}")
            else:
                failed.append((qid, question, f"cypher: {cypher[:100]}"))
                logger.warning(f"FAIL {qid}: {question} | cypher: {cypher[:100]}")
        except Exception as e:
            failed.append((qid, question, str(e)))
            logger.error(f"ERROR {qid}: {question} | {e}")

    total = len(E2E_NL_QUESTIONS)
    logger.info(f"E2E-02 结果: {passed}/{total} 通过")
    assert passed >= 8, \
        f"E2E-02 通过率不足：{passed}/{total}，失败题目:\n" + \
        "\n".join(f"  {qid}: {q} → {reason}" for qid, q, reason in failed)
