"""
test_23_regression_bugfix.py — Sprint 9 已知 Bug 修复验证 + 回归防护

Tests:
  S9-01 (P0) BUG-01 修复验证 — S3 Vectors metadata 长中文内容不超 1500 bytes
  S9-02 (P0) BUG-01 回归    — 短文本经 bytes 截断逻辑后仍正常写入
  S9-03 (P0) BUG-02 修复验证 — Bedrock 超时返回 {"error": ...} 而非抛出异常
  S9-04 (P0) BUG-02 回归    — 正常 Bedrock 响应时 query() 完整返回
  S9-05 (P0) Schema 漂移回归 — ETL 新增标签必须同步 schema_prompt.py（K8sService 预期 FAIL）

全部用 mock，无需真实 AWS / Neptune 连接。S9-05 纯静态扫描。
"""
import io
import json
import re
import types as pytypes
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = Path('/home/ubuntu/tech/graph-dependency-platform')
ETL_DIR = PROJECT_ROOT / 'infra' / 'lambda' / 'etl_aws'


# ---------------------------------------------------------------------------
# 共享 Mock 工具
# ---------------------------------------------------------------------------

def _build_s3v_mock_client(put_vectors_calls: list) -> MagicMock:
    """构造可用的 S3 Vectors mock client（bucket/index 已存在，put_vectors 记录调用）。"""
    mock_client = MagicMock()
    # ensure_bucket_and_index() 捕获 client.exceptions.NotFoundException
    mock_client.exceptions.NotFoundException = Exception
    mock_client.get_vector_bucket.return_value = {}  # 不抛异常 → bucket 已存在
    mock_client.get_index.return_value = {}           # 不抛异常 → index 已存在

    def _capture_put(**kwargs):
        put_vectors_calls.append(kwargs)
        return {}

    mock_client.put_vectors.side_effect = _capture_put
    return mock_client


def _mock_bedrock_invoke(cypher_text: str, summary_text: str = "查询完成。") -> callable:
    """返回一个 Bedrock invoke_model mock 函数（第 1 次返回 cypher，第 2 次返回 summary）。"""
    call_count = [0]

    def invoke(**kwargs):  # NLQueryEngine 全部用 keyword args 调用
        call_count[0] += 1
        text = cypher_text if call_count[0] == 1 else summary_text
        body = json.dumps({"content": [{"text": text}]}).encode()
        return {"body": io.BytesIO(body)}

    return invoke


# ---------------------------------------------------------------------------
# S9-01: BUG-01 修复验证 — metadata 长中文按 bytes 截断到 ≤ 1500
# ---------------------------------------------------------------------------

def test_s9_01_bug01_long_chinese_metadata_not_too_large():
    """S9-01 (P0): BUG-01 修复验证 — metadata content 字段长中文不超 1500 bytes。

    BUG-01 (tests/FAILURES.md):
      incident_vectordb.py 原先用 chunk.content[:2000] 存入 metadata。
      中文每字 3 bytes，2000 字 ≈ 6000 bytes，超过 S3 Vectors 2048 bytes 总上限。
      错误: ValidationException "Filterable metadata must have at most 2048 bytes"

    FIX (rca/search/incident_vectordb.py ~L86):
      'content': chunk.content.encode('utf-8')[:1500].decode('utf-8', errors='ignore')
      按 UTF-8 bytes 截断，确保 content 字段本身 ≤ 1500 bytes。
    """
    # 超长中文文本：约 1600 字 × 3 bytes/字 ≈ 4800 bytes（远超 1500）
    long_chinese = "这是一段超长的中文 RCA 事故报告文本，包含大量详细的故障分析和根因说明。" * 50
    assert len(long_chinese.encode("utf-8")) > 1500, "前提条件：测试文本应超过 1500 bytes"

    mock_chunk = pytypes.SimpleNamespace(content=long_chinese, tokens=450)
    put_calls: list = []
    mock_client = _build_s3v_mock_client(put_calls)
    dummy_vector = [0.1] * 1024

    with patch("search.incident_vectordb.chunk_text", return_value=[mock_chunk]), \
         patch("search.incident_vectordb.embed_text", return_value=dummy_vector), \
         patch("search.incident_vectordb.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client

        from search import incident_vectordb
        # BUG-01 修复后应正常完成，不抛出 ValidationException
        incident_vectordb.index_incident(
            incident_id="test-s9-01-long-chinese",
            report_text=long_chinese,
            metadata={
                "severity": "P1",
                "affected_service": "petsite",
                "root_cause": "长中文根因描述用于测试 bytes 截断",
                "timestamp": "2026-04-16T00:00:00Z",
            },
        )

    assert put_calls, "put_vectors 应被调用（向量已写入）"

    for vector in put_calls[0]["vectors"]:
        content_field: str = vector["metadata"]["content"]
        byte_len = len(content_field.encode("utf-8"))
        assert byte_len <= 1500, (
            f"metadata content 字段超出 1500 bytes 限制: {byte_len} bytes。"
            f"BUG-01 修复可能未生效（检查 rca/search/incident_vectordb.py ~L86）。"
        )


# ---------------------------------------------------------------------------
# S9-02: BUG-01 回归 — 短文本经 bytes 截断逻辑后仍正常写入
# ---------------------------------------------------------------------------

def test_s9_02_bug01_regression_short_text_still_works():
    """S9-02 (P0): BUG-01 回归防护 — 短文本经 bytes 截断逻辑后内容完整保留。

    BUG-01 修复引入了按 bytes 截断逻辑。
    本测试确保短文本（bytes < 1500）在截断后内容无损，写入路径正常。
    Ref: tests/FAILURES.md BUG-01
    """
    short_text = "petsite 服务 DynamoDB PetAdoptions 表读取限流，导致 5xx 错误响应率上升至 12%。"
    assert len(short_text.encode("utf-8")) < 1500, "前提条件：短文本应小于 1500 bytes"

    mock_chunk = pytypes.SimpleNamespace(content=short_text, tokens=30)
    put_calls: list = []
    mock_client = _build_s3v_mock_client(put_calls)
    dummy_vector = [0.1] * 1024

    with patch("search.incident_vectordb.chunk_text", return_value=[mock_chunk]), \
         patch("search.incident_vectordb.embed_text", return_value=dummy_vector), \
         patch("search.incident_vectordb.boto3") as mock_boto3:
        mock_boto3.client.return_value = mock_client

        from search import incident_vectordb
        incident_vectordb.index_incident(
            incident_id="test-s9-02-short-text",
            report_text=short_text,
            metadata={
                "severity": "P2",
                "affected_service": "petsite",
                "root_cause": "DynamoDB ReadThrottling",
                "timestamp": "2026-04-16T01:00:00Z",
            },
        )

    assert put_calls, "短文本也应正常调用 put_vectors"
    vectors = put_calls[0]["vectors"]
    assert len(vectors) == 1, "1 个 chunk 应产生 1 个 vector"

    content = vectors[0]["metadata"]["content"]
    assert content, "content 字段不应为空"
    # 截断不应破坏短文本内容（全部字符都能保留）
    assert "DynamoDB" in content, "短文本关键词 'DynamoDB' 应在 content 中保留"
    assert "petsite" in content, "短文本关键词 'petsite' 应在 content 中保留"

    byte_len = len(content.encode("utf-8"))
    assert byte_len <= 1500, f"截断后 content 仍不超 1500 bytes: {byte_len}"


# ---------------------------------------------------------------------------
# S9-03: BUG-02 修复验证 — Bedrock 超时返回 {"error": ...} 而非抛出异常
# ---------------------------------------------------------------------------

def test_s9_03_bug02_bedrock_timeout_returns_error_dict():
    """S9-03 (P0): BUG-02 修复验证 — query() 捕获 Bedrock 异常，返回 {"error": ...}。

    BUG-02 (tests/FAILURES.md):
      NLQueryEngine.query() 未捕获 _generate_cypher() 的异常，
      Bedrock 超时/不可用时异常向上传播，调用者拿不到友好的 {"error": ...} 格式。

    FIX (rca/neptune/nl_query.py query() 方法):
      try:
          cypher = self._generate_cypher(question)
      except Exception as e:
          return {"error": str(e), "cypher": ""}

    注意: test_04_unit_nlquery.py::test_ub2_03_bedrock_timeout_raises 测试的是旧行为
    （期望抛出异常），该测试在修复后应 FAIL（记录为已知回归）。
    Ref: tests/FAILURES.md BUG-02
    """
    from neptune.nl_query import NLQueryEngine
    from neptune.schema_prompt import build_system_prompt

    engine = NLQueryEngine.__new__(NLQueryEngine)
    engine.system_prompt = build_system_prompt()

    mock_bedrock = MagicMock()
    mock_bedrock.invoke_model = MagicMock(
        side_effect=Exception("ReadTimeoutError: Bedrock endpoint not responding after 30s")
    )
    engine.bedrock = mock_bedrock

    # BUG-02 修复后：不应抛出异常，应返回 {"error": ...}
    result = engine.query("petsite 依赖哪些数据库？")

    assert isinstance(result, dict), (
        f"query() 应返回 dict，实际类型: {type(result)}。"
        "BUG-02 修复可能未生效——检查 rca/neptune/nl_query.py query() 方法是否有 try/except。"
    )
    assert "error" in result, (
        f"result 应包含 'error' key，实际 keys: {list(result.keys())}。"
        "调用者期望收到 error dict 而非原始异常。"
    )
    assert "ReadTimeoutError" in result["error"] or result["error"], (
        f"error 字段应包含异常消息，实际值: {result['error']!r}"
    )
    assert result.get("cypher") == "", (
        f"超时时 cypher 应为空字符串，实际: {result.get('cypher')!r}"
    )


# ---------------------------------------------------------------------------
# S9-04: BUG-02 回归 — 正常 Bedrock 响应时 query() 完整返回
# ---------------------------------------------------------------------------

def test_s9_04_bug02_regression_normal_query_works():
    """S9-04 (P0): BUG-02 回归防护 — 正常 Bedrock 响应时 query() 返回完整结构。

    确保 BUG-02 引入的 try/except 不影响正常查询路径：
    结果应包含 question / cypher / results / summary 四个字段，不含 error。
    Ref: tests/FAILURES.md BUG-02
    """
    from neptune.nl_query import NLQueryEngine
    from neptune.schema_prompt import build_system_prompt

    cypher = (
        "MATCH (s:Microservice {name:'petsite'})-[:AccessesData]->(db) "
        "WHERE db:RDSCluster OR db:DynamoDBTable "
        "RETURN db.name AS database, labels(db)[0] AS type LIMIT 50"
    )
    engine = NLQueryEngine.__new__(NLQueryEngine)
    engine.system_prompt = build_system_prompt()

    mock_bedrock = MagicMock()
    mock_bedrock.invoke_model = _mock_bedrock_invoke(cypher, "petsite 依赖 RDS 和 DynamoDB 两类数据库。")
    engine.bedrock = mock_bedrock

    mock_results = [{"database": "petsite-rds", "type": "RDSCluster"}]

    with patch("neptune.neptune_client.results", return_value=mock_results):
        result = engine.query("petsite 依赖哪些数据库？")

    assert "error" not in result, (
        f"正常查询不应包含 error，实际: {result}"
    )
    assert result.get("question") == "petsite 依赖哪些数据库？", (
        f"question 字段应原样保留，实际: {result.get('question')!r}"
    )
    assert "Microservice" in result.get("cypher", ""), (
        f"cypher 字段应包含生成的查询，实际: {result.get('cypher')!r}"
    )
    assert result.get("results") == mock_results, (
        f"results 应为 Neptune 查询返回值，实际: {result.get('results')}"
    )
    assert result.get("summary"), "summary 字段不应为空"


# ---------------------------------------------------------------------------
# S9-05: Schema 漂移回归 — ETL 节点标签必须同步 schema_prompt.py
# ---------------------------------------------------------------------------

def _extract_etl_node_labels() -> set:
    """静态扫描 ETL 源文件，提取 upsert_vertex 调用中的字符串字面量节点标签。"""
    pattern = re.compile(r"""upsert_vertex\(\s*['"](\w+)['"]""")
    labels: set = set()
    for py_file in ETL_DIR.glob("*.py"):
        src = py_file.read_text(encoding="utf-8")
        labels.update(pattern.findall(src))
    return labels


def _extract_etl_edge_types() -> set:
    """静态扫描 ETL 源文件，提取 upsert_edge / addE 调用中的字符串字面量边类型。"""
    # upsert_edge(src, dst, 'EdgeType', ...)
    upsert_pat = re.compile(r"""upsert_edge\([^,]+,[^,]+,\s*['"](\w+)['"]""")
    # addE('EdgeType')
    add_e_pat = re.compile(r"""addE\(['"](\w+)['"]\)""")
    edge_types: set = set()
    for py_file in ETL_DIR.glob("*.py"):
        src = py_file.read_text(encoding="utf-8")
        edge_types.update(upsert_pat.findall(src))
        edge_types.update(add_e_pat.findall(src))
    return edge_types


def _extract_schema_node_labels() -> set:
    """从 schema_prompt.py GRAPH_SCHEMA 提取节点标签（形如 '- LabelName:' 的行）。"""
    from neptune.schema_prompt import GRAPH_SCHEMA
    # 匹配 "- LabelName: ..." 或 "- LabelName (..." 的行首节点定义
    pattern = re.compile(r"^\s*-\s+(\w+)\s*[:(]", re.MULTILINE)
    return set(pattern.findall(GRAPH_SCHEMA))


def _extract_schema_edge_types() -> set:
    """从 schema_prompt.py GRAPH_SCHEMA 提取边类型（含 [:Type1|:Type2] 多类型写法）。"""
    from neptune.schema_prompt import GRAPH_SCHEMA
    # 先提取 [...] 内的完整内容，再按 | 分割处理多类型如 [:ForwardsTo|:RoutesTo]
    bracket_pattern = re.compile(r"\[([:\w|]+)\]")
    edge_types: set = set()
    for bracket_content in bracket_pattern.findall(GRAPH_SCHEMA):
        for part in bracket_content.split("|"):
            label = part.lstrip(":")
            if label:
                edge_types.add(label)
    return edge_types


def test_s9_05_schema_drift_etl_labels_in_schema():
    """S9-05 (P0): Schema 漂移回归 — ETL upsert_vertex 节点标签必须出现在 schema_prompt.py。

    静态扫描 infra/lambda/etl_aws/*.py，提取 upsert_vertex('Label', ...) 的标签，
    与 rca/neptune/schema_prompt.py GRAPH_SCHEMA 定义对比，找出漂移。

    已知漂移 (Sprint 9，预期 FAIL):
      ❌ K8sService — handler.py:695 新增了 K8sService 节点，但 schema_prompt.py 未更新。
         修复方法: 在 GRAPH_SCHEMA 的"容器层"中添加:
           - K8sService: name(str), cluster_ip(str), port(int), selector(str)
         并在边类型部分添加:
           - (:K8sService)-[:Implements]->(:Microservice)
           - (:Pod)-[:BelongsTo]->(:K8sService)

    此测试将持续 FAIL 直到 schema_prompt.py 补充 K8sService 定义。
    """
    etl_labels = _extract_etl_node_labels()
    schema_labels = _extract_schema_node_labels()
    etl_edge_types = _extract_etl_edge_types()
    schema_edge_types = _extract_schema_edge_types()

    missing_labels = etl_labels - schema_labels
    missing_edge_types = etl_edge_types - schema_edge_types

    failure_lines = []

    if missing_labels:
        failure_lines.append("【节点标签漂移】ETL 使用但 schema_prompt.py 未定义：")
        for label in sorted(missing_labels):
            failure_lines.append(f"  ❌ {label}")

    if missing_edge_types:
        failure_lines.append("【边类型漂移】ETL 使用但 schema_prompt.py 未定义：")
        for etype in sorted(missing_edge_types):
            failure_lines.append(f"  ❌ {etype}")

    if failure_lines:
        failure_lines.append(
            "\n修复方法: 更新 rca/neptune/schema_prompt.py GRAPH_SCHEMA，"
            "添加以上缺失的节点/边类型定义。"
        )
        pytest.fail("\n".join(failure_lines))
