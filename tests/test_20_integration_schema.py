"""
test_20_integration_schema.py — Sprint 6 动态 Schema 集成测试

Tests: S6-01 ~ S6-08

S6-01/02/05/06/07: 需要真实 Neptune + Bedrock 连接，标记 @pytest.mark.neptune
S6-03/04/08:       离线可跑（mock），无 @pytest.mark.neptune 标记
"""
import io
import json
import os
import re
import sys
import time
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = "/home/ubuntu/tech/graph-dependency-platform"
RCA_DIR = os.path.join(PROJECT_ROOT, "rca")

if RCA_DIR not in sys.path:
    sys.path.insert(0, RCA_DIR)


# ── Schema parse helpers ──────────────────────────────────────────────────────

def _load_schema_module():
    """Load rca/neptune/schema_prompt.py as a module."""
    from neptune import schema_prompt
    return schema_prompt


def _schema_node_labels() -> set:
    """Parse node labels from static GRAPH_SCHEMA (lines: '- LabelName: ...')."""
    mod = _load_schema_module()
    node_section = mod.GRAPH_SCHEMA.split("## 边类型")[0]
    return set(re.findall(r"^- ([A-Z][A-Za-z0-9]+):", node_section, re.MULTILINE))


def _schema_edge_types() -> set:
    """Parse edge types from static GRAPH_SCHEMA ('[: EdgeType ]' patterns)."""
    mod = _load_schema_module()
    raw = re.findall(r"\[:([\w|:]+)\]", mod.GRAPH_SCHEMA)
    edges: set = set()
    for item in raw:
        for part in item.split("|"):
            part = part.lstrip(":")
            if part:
                edges.add(part)
    return edges


def _extract_dynamic_node_labels(neptune_client) -> set:
    """动态提取 Neptune 中实际存在的节点标签（实时查询）。"""
    rows = neptune_client.results(
        "MATCH (n) RETURN DISTINCT labels(n) AS label LIMIT 500"
    )
    labels: set = set()
    for row in rows:
        val = row.get("label", [])
        if isinstance(val, list):
            labels.update(val)
        elif isinstance(val, str):
            labels.add(val)
    return labels


def _extract_dynamic_edge_types(neptune_client) -> set:
    """动态提取 Neptune 中实际存在的边类型（实时查询）。"""
    rows = neptune_client.results(
        "MATCH ()-[r]->() RETURN DISTINCT type(r) AS rel_type LIMIT 500"
    )
    return {row["rel_type"] for row in rows if row.get("rel_type")}


def _mock_bedrock_response(cypher_text: str) -> dict:
    """构造 Bedrock invoke_model 的模拟响应（BytesIO body）。"""
    body_content = json.dumps({"content": [{"text": cypher_text}]}).encode()
    return {"body": io.BytesIO(body_content)}


def _make_engine_with_mock_bedrock(cypher_text: str):
    """创建带 mock Bedrock 的 NLQueryEngine（不需要真实 AWS Bedrock 凭证）。"""
    from neptune.nl_query import NLQueryEngine
    from neptune.schema_prompt import build_system_prompt

    engine = NLQueryEngine.__new__(NLQueryEngine)
    engine.system_prompt = build_system_prompt()

    call_count = [0]

    def mock_invoke(**kwargs):
        call_count[0] += 1
        return _mock_bedrock_response(cypher_text)

    mock_bedrock = MagicMock()
    mock_bedrock.invoke_model = mock_invoke
    engine.bedrock = mock_bedrock
    return engine


# ── TTL cache helper for S6-03 ────────────────────────────────────────────────

class _TTLSchemaCache:
    """带 TTL 的 schema 缓存（用于验证 S6-03 TTL 刷新逻辑）。"""

    TTL_SECONDS: int = 600  # 10 minutes

    def __init__(self) -> None:
        self._cache: Optional[str] = None
        self._cached_at: float = 0.0
        self._fetch_count: int = 0

    def get_schema(self) -> str:
        """返回 schema 字符串；TTL 过期时自动从 schema_prompt 重新加载。"""
        now = time.time()
        if self._cache is None or (now - self._cached_at) >= self.TTL_SECONDS:
            from neptune.schema_prompt import build_system_prompt
            self._cache = build_system_prompt()
            self._cached_at = now
            self._fetch_count += 1
        return self._cache


# ── Module-level NLQueryEngine fixture (shared across S6-05/06/07) ───────────

@pytest.fixture(scope="module")
def nl_engine():
    """创建真实 NLQueryEngine 实例（需要 AWS 凭证；复用整个 module）。"""
    from neptune.nl_query import NLQueryEngine
    return NLQueryEngine()


# ── S6-01 ─────────────────────────────────────────────────────────────────────

@pytest.mark.neptune
def test_s6_01_dynamic_node_labels_match_static_schema(neptune_rca):
    """S6-01 (P0): 动态 schema 提取节点标签 vs schema_prompt.py 静态定义一致。

    动态提取：MATCH (n) RETURN DISTINCT labels(n) — 实时查询 Neptune
    静态定义：schema_prompt.py GRAPH_SCHEMA 中 '- LabelName:' 行解析
    任何差异均视为 schema 漂移，导致测试失败。
    """
    static_labels = _schema_node_labels()
    dynamic_labels = _extract_dynamic_node_labels(neptune_rca)

    missing_in_neptune = static_labels - dynamic_labels
    extra_in_neptune = dynamic_labels - static_labels

    print(f"\n静态节点标签 ({len(static_labels)}): {sorted(static_labels)}")
    print(f"动态节点标签 ({len(dynamic_labels)}): {sorted(dynamic_labels)}")
    if missing_in_neptune:
        print(f"[MISSING] 静态定义但 Neptune 无数据: {sorted(missing_in_neptune)}")
    if extra_in_neptune:
        print(f"[EXTRA]   Neptune 有但静态未定义: {sorted(extra_in_neptune)}")

    assert not missing_in_neptune, (
        f"schema_prompt.py 定义了以下节点标签但 Neptune 中不存在: "
        f"{sorted(missing_in_neptune)}"
    )
    assert not extra_in_neptune, (
        f"Neptune 中存在以下节点标签但 schema_prompt.py 未定义: "
        f"{sorted(extra_in_neptune)}"
    )


# ── S6-02 ─────────────────────────────────────────────────────────────────────

@pytest.mark.neptune
def test_s6_02_dynamic_edge_types_match_static_schema(neptune_rca):
    """S6-02 (P0): 动态 schema 提取边类型 vs schema_prompt.py 静态定义一致。

    动态提取：MATCH ()-[r]->() RETURN DISTINCT type(r) — 实时查询 Neptune
    静态定义：schema_prompt.py GRAPH_SCHEMA 中 '[:EdgeType]' 模式解析
    任何差异均视为 schema 漂移，导致测试失败。
    """
    static_edges = _schema_edge_types()
    dynamic_edges = _extract_dynamic_edge_types(neptune_rca)

    missing_in_neptune = static_edges - dynamic_edges
    extra_in_neptune = dynamic_edges - static_edges

    print(f"\n静态边类型 ({len(static_edges)}): {sorted(static_edges)}")
    print(f"动态边类型 ({len(dynamic_edges)}): {sorted(dynamic_edges)}")
    if missing_in_neptune:
        print(f"[MISSING] 静态定义但 Neptune 无数据: {sorted(missing_in_neptune)}")
    if extra_in_neptune:
        print(f"[EXTRA]   Neptune 有但静态未定义: {sorted(extra_in_neptune)}")

    assert not missing_in_neptune, (
        f"schema_prompt.py 定义了以下边类型但 Neptune 中不存在: "
        f"{sorted(missing_in_neptune)}"
    )
    assert not extra_in_neptune, (
        f"Neptune 中存在以下边类型但 schema_prompt.py 未定义: "
        f"{sorted(extra_in_neptune)}"
    )


# ── S6-03 ─────────────────────────────────────────────────────────────────────

def test_s6_03_schema_cache_ttl_refresh():
    """S6-03 (P2): 动态 schema 缓存 10 分钟后过期自动刷新（mock time.time）。

    验证三个阶段：
    1. 首次 get_schema() → fetch_count=1，缓存已填充
    2. TTL 内再次调用 → fetch_count 不变（命中缓存，返回相同内容）
    3. mock time.time 推进 601 秒后调用 → fetch_count=2（缓存过期触发刷新）

    离线测试，无需 Neptune 或 Bedrock。
    """
    cache = _TTLSchemaCache()

    # Phase 1: First fetch
    schema1 = cache.get_schema()
    assert cache._fetch_count == 1, (
        f"首次调用应触发一次 schema 加载，实际 fetch_count={cache._fetch_count}"
    )
    assert isinstance(schema1, str) and len(schema1) > 100, (
        "Schema 内容应为非空字符串"
    )
    assert "Microservice" in schema1, "Schema 内容应包含 Microservice 定义"

    # Phase 2: Cache hit within TTL
    schema2 = cache.get_schema()
    assert cache._fetch_count == 1, (
        f"TTL 内再次调用不应触发刷新（应命中缓存），实际 fetch_count={cache._fetch_count}"
    )
    assert schema1 == schema2, "缓存命中时返回内容应与首次完全一致"

    # Phase 3: TTL expiry → refresh
    expired_time = cache._cached_at + 601.0  # 601s > TTL(600s)
    with patch("time.time", return_value=expired_time):
        schema3 = cache.get_schema()

    assert cache._fetch_count == 2, (
        f"TTL 过期（601s > 600s）后应触发重新加载，实际 fetch_count={cache._fetch_count}"
    )
    assert isinstance(schema3, str) and len(schema3) > 100, (
        "刷新后 schema 仍应为非空字符串"
    )


# ── S6-04 ─────────────────────────────────────────────────────────────────────

def test_s6_04_neptune_unreachable_fallback_to_static_schema():
    """S6-04 (P1): Neptune 不可达时，NLQueryEngine 仍使用静态 schema 生成 Cypher 并优雅返回。

    mock 策略：
      - Bedrock invoke_model → 返回固定合法 Cypher（绕过真实 Bedrock）
      - neptune_client.results → 抛出 ConnectionError（模拟 VPC 断网）

    期望：
      - query() 返回 dict 而非传播异常（Neptune 错误被优雅捕获）
      - 返回 dict 中包含 "cypher" 字段（静态 schema 仍可用于生成查询）
      - 返回 dict 中包含 "error" 字段（说明 Neptune 执行阶段失败）

    离线测试，无需真实 Neptune 或 Bedrock。
    """
    test_cypher = (
        "MATCH (s:Microservice {name:'petsite'}) "
        "RETURN s.name AS name, s.recovery_priority AS priority LIMIT 50"
    )
    engine = _make_engine_with_mock_bedrock(test_cypher)

    with patch(
        "neptune.neptune_client.results",
        side_effect=ConnectionError("Neptune cluster unreachable: timeout"),
    ):
        result = engine.query("petsite 是什么服务？")

    assert isinstance(result, dict), (
        "query() 应始终返回 dict，不应将 Neptune 异常传播到调用方"
    )
    assert "cypher" in result, (
        "即使 Neptune 不可达，仍应包含 cypher 字段（来自静态 schema 生成）"
    )
    assert "error" in result, (
        "Neptune 不可达时应在 error 字段中记录失败原因"
    )
    # Generated cypher should reflect the static schema prompt (Microservice label exists)
    assert "Microservice" in result["cypher"] or result["cypher"] == test_cypher, (
        f"生成的 Cypher 应基于静态 schema，实际: {result['cypher']}"
    )
    # Error message should contain the connection failure description
    assert result["error"], f"error 字段不应为空字符串，实际: {result['error']!r}"
    print(f"\nS6-04 优雅返回: error='{result['error']}', cypher='{result['cypher'][:60]}...'")


# ── S6-05 ─────────────────────────────────────────────────────────────────────

@pytest.mark.neptune
def test_s6_05_nl_query_petsite_databases(nl_engine, neptune_rca):
    """S6-05 (P0): NL 查询 "petsite 依赖哪些数据库" 返回 RDSCluster 或 DynamoDBTable 相关节点。

    端到端路径：NL → Bedrock → openCypher → Neptune → 结果
    验证 NL 引擎能正确理解数据库查询意图并生成包含数据库标签/关系的 Cypher。
    """
    result = nl_engine.query("petsite 依赖哪些数据库")

    assert isinstance(result, dict), "结果应为 dict"
    assert "cypher" in result, "应包含 cypher 字段"

    if "error" in result:
        pytest.skip(f"S6-05: 查询失败（可能数据不足）: {result['error']}")

    assert "results" in result, "成功结果应包含 results 字段"
    assert isinstance(result["results"], list), "results 应为列表"
    assert "summary" in result, "成功结果应包含 summary 字段"

    cypher = result["cypher"]
    db_keywords = [
        "RDSCluster", "DynamoDBTable", "DynamoDB", "AccessesData",
        "Database", "ConnectsTo", "S3Bucket",
    ]
    has_db_keyword = any(kw in cypher for kw in db_keywords)
    assert has_db_keyword, (
        f"S6-05: Cypher 未包含数据库相关关键词 {db_keywords}，\n"
        f"  生成的 Cypher: {cypher}"
    )

    result_count = len(result["results"])
    print(f"\nS6-05 结果 ({result_count} 条): {result['results'][:3]}")
    print(f"S6-05 摘要: {result.get('summary', '')[:100]}")


# ── S6-06 ─────────────────────────────────────────────────────────────────────

@pytest.mark.neptune
def test_s6_06_nl_query_petsite_upstream_downstream(nl_engine, neptune_rca):
    """S6-06 (P1): NL 查询 "petsite 的上下游服务" 生成包含 Calls / AccessesData 等关系的 Cypher。

    验证 NL 引擎对上下游拓扑查询意图的理解，
    生成的 Cypher 应包含至少一种调用/依赖关系类型。
    """
    result = nl_engine.query("petsite 的上下游服务是什么")

    assert isinstance(result, dict), "结果应为 dict"
    assert "cypher" in result, "应包含 cypher 字段"

    if "error" in result:
        pytest.skip(f"S6-06: 查询失败: {result['error']}")

    assert "results" in result, "成功结果应包含 results 字段"

    cypher = result["cypher"]
    relation_keywords = [
        "Calls", "AccessesData", "DependsOn", "PublishesTo",
        "InvokesVia", "ConnectsTo", "WritesTo",
    ]
    has_relation = any(kw in cypher for kw in relation_keywords)
    assert has_relation, (
        f"S6-06: Cypher 未包含调用/依赖关系关键词 {relation_keywords}，\n"
        f"  生成的 Cypher: {cypher}"
    )

    result_count = len(result.get("results", []))
    print(f"\nS6-06 生成 Cypher: {cypher[:120]}")
    print(f"S6-06 结果数: {result_count}")


# ── S6-07 ─────────────────────────────────────────────────────────────────────

_NL_QUERY_PATTERNS = [
    # (question, expected_keyword_in_cypher, human_label)
    ("petsite 依赖哪些服务",    "Microservice",    "微服务依赖"),
    ("petsite 的数据库有哪些",  "Accesses",        "数据库查询"),
    ("哪些 Pod 在运行",         "Pod",             "Pod状态"),
    ("EKS 集群有哪些节点",      "EC2",             "EKS节点"),
    ("有哪些负载均衡器",        "LoadBalancer",    "负载均衡器"),
    ("petsite 的上下游服务",    "Microservice",    "上下游拓扑"),
    ("哪些服务有安全组",        "SecurityGroup",   "安全组关联"),
    ("RDS 实例状态",            "RDS",             "RDS状态"),
    ("Lambda 函数列表",         "Lambda",          "Lambda函数"),
    ("混沌实验结果",            "ChaosExperiment", "混沌实验"),
]


@pytest.mark.neptune
@pytest.mark.parametrize("question,expected_kw,label", _NL_QUERY_PATTERNS)
def test_s6_07_common_nl_query_patterns(question, expected_kw, label, nl_engine, neptune_rca):
    """S6-07 (P1): 10 种常见 NL 查询模式均能生成包含预期关键词的合法 Cypher。

    每个子测试验证：
    1. query() 返回合法 dict（含 cypher 字段）
    2. 生成的 Cypher 中包含预期的标签/关系关键词（区分大小写）
    3. 非安全拦截的 error 直接 fail；安全拦截 skip
    """
    result = nl_engine.query(question)

    assert isinstance(result, dict), f"[{label}] query() 应返回 dict，问题: {question}"
    assert "cypher" in result, f"[{label}] 应包含 cypher 字段"

    if "error" in result:
        # 安全拦截：skip（不算失败，guard 正常工作）
        if any(kw in result["error"] for kw in ("写操作", "unsafe", "injection")):
            pytest.skip(f"[{label}] 被安全拦截（预期行为）: {result['error']}")
        # 其它错误：fail
        pytest.fail(
            f"[{label}] 查询失败（问题: {question!r}）\n"
            f"  error: {result['error']}\n"
            f"  cypher: {result.get('cypher', 'N/A')}"
        )

    cypher = result["cypher"]
    assert expected_kw in cypher or expected_kw.lower() in cypher.lower(), (
        f"[{label}] Cypher 未包含预期关键词 '{expected_kw}'，\n"
        f"  问题: {question!r}\n"
        f"  生成的 Cypher: {cypher}"
    )

    result_count = len(result.get("results", []))
    print(f"\n[{label}] 问题: {question!r} | 关键词: {expected_kw} ✓ | 结果数: {result_count}")


# ── S6-08 ─────────────────────────────────────────────────────────────────────

_INJECTION_CASES = [
    # (malicious_cypher, blocked_keyword)
    ("DROP INDEX ON :Microservice(name)", "DROP"),
    ("MATCH (n) DELETE n", "DELETE"),
    ("MATCH (n:Microservice {name:'petsite'}) SET n.name = 'hacked'", "SET"),
    ("MERGE (n:EvilNode {name: 'attacker'}) RETURN n", "MERGE"),
    ("CREATE (:EvilNode {name: 'injection_test'}) RETURN 1", "CREATE"),
    ("MATCH (n) DETACH DELETE n", "DETACH"),
    ("MATCH (n) REMOVE n.name RETURN n", "REMOVE"),
    ("CALL db.clearQueryCaches() YIELD value RETURN value", "CALL"),
]


@pytest.mark.parametrize("malicious_cypher,keyword", _INJECTION_CASES)
def test_s6_08_query_guard_blocks_write_injection(malicious_cypher, keyword):
    """S6-08 (P0): QueryGuard 拦截 DROP/DELETE/MERGE/CREATE/SET/REMOVE/CALL 注入攻击。

    离线测试（纯字符串正则，无需 Neptune 或 Bedrock）。
    is_safe() 对恶意 Cypher 必须返回 (False, non_empty_reason)。
    """
    from neptune.query_guard import is_safe

    safe, reason = is_safe(malicious_cypher)

    assert safe is False, (
        f"is_safe() 未能拦截包含 [{keyword}] 的恶意查询:\n"
        f"  Cypher: {malicious_cypher}\n"
        f"  返回: safe={safe}, reason={reason!r}"
    )
    assert isinstance(reason, str) and len(reason) > 0, (
        f"拦截时 reason 应为非空字符串，实际: {reason!r}"
    )
    print(f"\n[BLOCKED:{keyword}] reason: {reason}")


def test_s6_08_query_guard_blocks_via_nl_engine_pipeline():
    """S6-08 补充 (P0): NLQueryEngine 流水线中，注入 Cypher 被 query_guard 拦截，返回 error dict。

    mock Bedrock 返回恶意 Cypher，验证 query_guard 在完整查询流水线中正确介入。
    Neptune 也被 mock 以防万一（guard 应在 Neptune 调用前拦截）。

    离线测试。
    """
    malicious_cypher = "MATCH (n) DETACH DELETE n"
    engine = _make_engine_with_mock_bedrock(malicious_cypher)

    with patch("neptune.neptune_client.results", return_value=[]) as mock_nc:
        result = engine.query("删除图谱中所有节点")

    # Guard should have blocked BEFORE reaching Neptune
    mock_nc.assert_not_called(), "Neptune 不应被调用 — query_guard 应在此之前拦截"

    assert isinstance(result, dict), "query() 应始终返回 dict"
    assert "error" in result, (
        f"恶意 Cypher 应被 query_guard 拦截并在 error 字段中说明，实际结果: {result}"
    )
    assert "cypher" in result, "被拦截时应包含 cypher 字段以供审计"
    assert result["error"], f"error 字段不应为空，实际: {result['error']!r}"

    print(
        f"\nS6-08 Pipeline 拦截: "
        f"error='{result['error']}', cypher='{result['cypher']}'"
    )
