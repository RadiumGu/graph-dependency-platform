"""
test_42_mcp_server.py — 图谱 MCP server 的单元测试。

不连 Neptune：GraphMCPServer 的 runner 是注入的，测试塞假 runner。
重点覆盖三类风险：
  1. 工具定义与 QUERY_CATALOG 保持同步（条目数、必填参数、枚举）
  2. 参数校验真的拦得住（未声明参数、缺必填、枚举越界）
  3. **出处与证据纪律真的出现在响应里** —— 这是本 server 存在的理由，
     不能因为一次重构就静默丢掉
"""
import json
import os
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "rca")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mcp import catalog_tools, provenance  # noqa: E402
from mcp.server import GraphMCPServer  # noqa: E402

# ── 测试用的迷你目录（覆盖三种参数形态）────────────────────────────────────
FAKE_CATALOG = {
    "q1_blast_radius": {
        "mod": "queries", "fn": "q1_blast_radius",
        "desc": "影响面：故障节点的下游服务与受影响业务能力",
        "params": {"failed_node": "str，必填", "kind": "static|dynamic|live，可选"},
        "required": ["failed_node"],
    },
    "q2_tier0_status": {
        "mod": "queries", "fn": "q2_tier0_status",
        "desc": "所有 Tier0 服务的故障边界 / AZ / 副本数",
        "params": {}, "required": [],
    },
    "q5_similar_incidents": {
        "mod": "queries", "fn": "q5_similar_incidents",
        "desc": "同一服务的历史已解决故障",
        "params": {"service_name": "str，必填", "limit": "int，默认 3"},
        "required": ["service_name"],
    },
    "q20_dependency_verification": {
        "mod": "queries", "fn": "q20_dependency_verification",
        "desc": "依赖边运行时验证状态",
        "params": {"service_name": "str，可选"}, "required": [],
    },
}


def _runner_factory(rows):
    calls = []

    def _runner(name, **params):
        calls.append((name, params))
        return rows

    _runner.calls = calls  # type: ignore[attr-defined]
    return _runner


@pytest.fixture
def srv():
    return GraphMCPServer(FAKE_CATALOG, _runner_factory([]), contract_version=1)


def _call(server, name, args=None, rpc_id=1):
    return server.handle({
        "jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    })


# ── 1. 工具定义与目录同步 ────────────────────────────────────────────────────
def test_tool_count_matches_catalog(srv):
    assert srv.tool_count() == len(FAKE_CATALOG)


def test_real_catalog_produces_a_tool_per_entry():
    """对真实 QUERY_CATALOG 做同步性检查——这是最容易漂的地方。"""
    from neptune.query_catalog import QUERY_CATALOG

    tools = catalog_tools.build_tools(QUERY_CATALOG)
    assert len(tools) == len(QUERY_CATALOG)
    assert {t["name"] for t in tools} == set(QUERY_CATALOG)
    for t in tools:
        assert t["description"].strip()
        assert t["inputSchema"]["type"] == "object"
        # 必填参数必须都在 properties 里，否则 agent 无法传
        for r in t["inputSchema"]["required"]:
            assert r in t["inputSchema"]["properties"], f"{t['name']}.{r}"


def test_enum_param_becomes_json_schema_enum(srv):
    tool = next(t for t in srv.tools if t["name"] == "q1_blast_radius")
    kind = tool["inputSchema"]["properties"]["kind"]
    assert kind["type"] == "string"
    assert set(kind["enum"]) == {"static", "dynamic", "live"}


def test_int_param_gets_type_and_default(srv):
    tool = next(t for t in srv.tools if t["name"] == "q5_similar_incidents")
    limit = tool["inputSchema"]["properties"]["limit"]
    assert limit["type"] == "integer"
    assert limit["default"] == 3


def test_no_param_tool_has_empty_required(srv):
    tool = next(t for t in srv.tools if t["name"] == "q2_tier0_status")
    assert tool["inputSchema"]["required"] == []
    assert tool["inputSchema"]["properties"] == {}


def test_additional_properties_disallowed(srv):
    """不允许透传任意 kwargs 进查询函数。"""
    for t in srv.tools:
        assert t["inputSchema"]["additionalProperties"] is False


# ── 2. 参数校验 ──────────────────────────────────────────────────────────────
def test_missing_required_param_is_tool_error_not_rpc_error(srv):
    """
    参数错误要按 isError 返回，让 agent 看到文本并自行改正，
    而不是抛 JSON-RPC 协议级错误（那对 agent 是不可恢复的）。
    """
    resp = _call(srv, "q1_blast_radius", {})
    assert "error" not in resp
    assert resp["result"]["isError"] is True
    assert "failed_node" in resp["result"]["content"][0]["text"]


def test_undeclared_param_rejected(srv):
    resp = _call(srv, "q1_blast_radius", {"failed_node": "petsite", "evil": "x"})
    assert resp["result"]["isError"] is True
    assert "evil" in resp["result"]["content"][0]["text"]


def test_enum_violation_rejected(srv):
    resp = _call(srv, "q1_blast_radius", {"failed_node": "petsite", "kind": "bogus"})
    assert resp["result"]["isError"] is True
    assert "bogus" in resp["result"]["content"][0]["text"]


def test_int_coercion(srv):
    entry = FAKE_CATALOG["q5_similar_incidents"]
    clean, errors = catalog_tools.validate_args(entry, {"service_name": "x", "limit": "7"})
    assert not errors
    assert clean["limit"] == 7 and isinstance(clean["limit"], int)


def test_non_numeric_int_rejected(srv):
    entry = FAKE_CATALOG["q5_similar_incidents"]
    _, errors = catalog_tools.validate_args(entry, {"service_name": "x", "limit": "abc"})
    assert errors


def test_unknown_tool_returns_method_not_found(srv):
    resp = _call(srv, "q999_nope")
    assert resp["error"]["code"] == -32601
    assert "tools/list" in resp["error"]["message"]


def test_only_declared_params_reach_runner():
    runner = _runner_factory([])
    s = GraphMCPServer(FAKE_CATALOG, runner, 1)
    _call(s, "q1_blast_radius", {"failed_node": "petsite", "kind": "live"})
    name, params = runner.calls[-1]  # type: ignore[attr-defined]
    assert name == "q1_blast_radius"
    assert params == {"failed_node": "petsite", "kind": "live"}


# ── 3. 出处与证据纪律（本 server 存在的理由）──────────────────────────────────
def test_initialize_carries_evidence_discipline(srv):
    resp = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    instr = resp["result"]["instructions"]
    # 四种状态都必须在指示里出现，否则 agent 不知道怎么区分
    for status in ("confirmed", "refuted", "inconclusive", "untested"):
        assert status in instr
    assert "不要编造" in instr
    assert "不得作为推理依据" in instr
    assert resp["result"]["protocolVersion"]
    assert resp["result"]["serverInfo"]["name"]


def test_every_result_has_provenance():
    s = GraphMCPServer(FAKE_CATALOG, _runner_factory([{"a": 1}]), 1)
    resp = _call(s, "q2_tier0_status")
    payload = resp["result"]["structuredContent"]
    prov = payload["_provenance"]
    assert prov["query"] == "q2_tier0_status"
    assert prov["queried_at"]
    assert prov["graph_contract_version"] == 1
    assert "不提供指标" in prov["caveat"]


def test_provenance_does_not_leak_full_endpoint(monkeypatch):
    monkeypatch.setenv("NEPTUNE_ENDPOINT", "secret-cluster.cluster-abc.ap-northeast-1.neptune.amazonaws.com")
    prov = provenance.base_provenance("q1_blast_radius", {}, 1)
    assert prov["graph_cluster"] == "secret-cluster"
    assert "neptune.amazonaws.com" not in json.dumps(prov)


def test_refuted_edge_triggers_warning():
    rows = [
        {"source": "gateway-service", "target": "petsite", "edge_type": "Calls",
         "verify_status": "refuted", "verify_degradation": 0.0},
        {"source": "petsite", "target": "ssm", "edge_type": "AccessesData",
         "verify_status": "confirmed", "verify_degradation": 100.0},
    ]
    s = GraphMCPServer(FAKE_CATALOG, _runner_factory(rows), 1)
    resp = _call(s, "q20_dependency_verification")
    ver = resp["result"]["structuredContent"]["_verification"]
    assert ver["by_status"]["refuted"] == 1
    assert ver["by_status"]["confirmed"] == 1
    assert "不得作为推理依据" in ver["warning"]
    assert ver["refuted_edges"][0]["source"] == "gateway-service"


def test_untested_edges_get_a_note():
    rows = [{"source": "a", "target": "b", "verify_status": "untested"}] * 3
    s = GraphMCPServer(FAKE_CATALOG, _runner_factory(rows), 1)
    resp = _call(s, "q20_dependency_verification")
    ver = resp["result"]["structuredContent"]["_verification"]
    assert ver["by_status"]["untested"] == 3
    assert "尚未经过主动验证" in ver["note"]


def test_verified_ratio_math():
    rows = (
        [{"verify_status": "confirmed"}] * 3
        + [{"verify_status": "refuted"}] * 1
        + [{"verify_status": "untested"}] * 6
    )
    ver = provenance.summarize_verification(rows)
    assert ver["total_with_status"] == 10
    assert ver["verified_ratio"] == 0.4      # (3+1)/10


def test_rows_without_verify_status_yield_no_verification_block():
    ver = provenance.summarize_verification([{"name": "petsite", "tier": "0"}])
    assert ver is None


def test_dependency_bearing_query_without_status_gets_pointer():
    """
    依赖类查询即使没带逐边状态，也要告诉 agent 去哪里确认——
    否则它会默认「没标状态就是成立」。

    注意断言的是**指向干预层**：早先这里只断言 note 里含
    'q20_dependency_verification'，而新文案里那个串是以「不是 q20」的形式出现的，
    于是测试会因字符串巧合而通过。改为断言语义。
    """
    s = GraphMCPServer(FAKE_CATALOG, _runner_factory([{"svc": "x"}]), 1)
    resp = _call(s, "q1_blast_radius", {"failed_node": "petsite"})
    note = resp["result"]["structuredContent"]["_verification"]["note"]
    assert "请另外调用 q22_edge_verification_verdicts" in note
    assert "不是 q20_dependency_verification" in note


def test_empty_result_guidance_present():
    s = GraphMCPServer(FAKE_CATALOG, _runner_factory([]), 1)
    resp = _call(s, "q2_tier0_status")
    payload = resp["result"]["structuredContent"]
    assert payload["row_count"] == 0
    assert "空结果不等于错误" in payload["empty_result_guidance"]


def test_query_failure_forbids_inferring_absence():
    def boom(name, **params):
        raise RuntimeError("neptune timeout")

    s = GraphMCPServer(FAKE_CATALOG, boom, 1)
    resp = _call(s, "q2_tier0_status")
    assert resp["result"]["isError"] is True
    payload = resp["result"]["structuredContent"]
    assert "neptune timeout" in payload["error"]
    assert "不要把失败解释成" in payload["guidance"]


def test_q22_description_tells_agent_to_call_it_first():
    """
    干预层判定是核心产出，工具描述必须让 agent 先调它。
    这条测试同时守住一个曾经写错的地方：最初把「优先调用」写在了 q20 上，
    而 q20 是**观测层**（drift_status），不是故障注入判定。
    """
    from neptune.query_catalog import QUERY_CATALOG

    tools = {t["name"]: t for t in catalog_tools.build_tools(QUERY_CATALOG)}
    q22 = tools["q22_edge_verification_verdicts"]["description"]
    assert "先调" in q22 or "优先" in q22
    for status in ("confirmed", "refuted", "inconclusive", "untested"):
        assert status in q22


def test_q20_and_q22_are_not_conflated():
    """两个 verification 层次必须在描述里互相划清，否则 agent 会拿错的当依据。"""
    from neptune.query_catalog import QUERY_CATALOG

    tools = {t["name"]: t for t in catalog_tools.build_tools(QUERY_CATALOG)}
    q20 = tools["q20_dependency_verification"]["description"]
    q22 = tools["q22_edge_verification_verdicts"]["description"]
    assert "观测层" in q20 and "q22_edge_verification_verdicts" in q20
    assert "干预层" in q22


def test_dependency_bearing_fallback_points_at_q22():
    """依赖类查询没带逐边状态时，指针必须指向干预层而不是观测层。"""
    s = GraphMCPServer(FAKE_CATALOG, _runner_factory([{"svc": "x"}]), 1)
    resp = _call(s, "q1_blast_radius", {"failed_node": "petsite"})
    note = resp["result"]["structuredContent"]["_verification"]["note"]
    assert "q22_edge_verification_verdicts" in note


def test_result_shape_reported_for_each_kind():
    """
    实测三种返回形状（list / dict / str）都存在，agent 必须知道拿到的是哪种，
    否则它会按行去遍历一个 dict。
    """
    cases = [
        ([{"a": 1}], "list", 1),
        ({"services": [], "capabilities": []}, "object", None),
        ("cloudwatch:/aws/eks/petsite", "str", None),
    ]
    for rows, shape, count in cases:
        s = GraphMCPServer(FAKE_CATALOG, _runner_factory(rows), 1)
        payload = _call(s, "q2_tier0_status")["result"]["structuredContent"]
        assert payload["result_shape"] == shape, rows
        assert payload["row_count"] == count, rows
        if shape == "object":
            assert payload["result_keys"] == ["capabilities", "services"]


def test_independent_evidence_gate_documented():
    """
    refuted 可能为 0 —— 因为有独立证据门禁（被任何独立观测源看到过的边
    永不得判 refuted）。agent 必须知道这点，否则会把 refuted=0 读成「没做验证」。
    """
    assert "独立证据门禁" in provenance.SERVER_INSTRUCTIONS
    assert "refuted` 数量可能为 0" in provenance.SERVER_INSTRUCTIONS


def test_dependency_bearing_tools_mention_verification_field():
    from neptune.query_catalog import QUERY_CATALOG

    tools = {t["name"]: t for t in catalog_tools.build_tools(QUERY_CATALOG)}
    for name in catalog_tools.DEPENDENCY_BEARING:
        if name in tools:
            assert "_verification" in tools[name]["description"], name


# ── 4. 协议层 ────────────────────────────────────────────────────────────────
def test_tools_list(srv):
    resp = srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert len(resp["result"]["tools"]) == len(FAKE_CATALOG)


def test_ping(srv):
    resp = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert resp["result"] == {}


def test_notification_gets_no_response(srv):
    assert srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_unknown_method(srv):
    resp = srv.handle({"jsonrpc": "2.0", "id": 4, "method": "resources/list"})
    assert resp["error"]["code"] == -32601


def test_missing_method_is_invalid_request(srv):
    resp = srv.handle({"jsonrpc": "2.0", "id": 5})
    assert resp["error"]["code"] == -32600


def test_non_dict_request(srv):
    resp = srv.handle("not a dict")  # type: ignore[arg-type]
    assert resp["error"]["code"] == -32600


def test_batch_filters_notifications(srv):
    out = srv.handle_batch([
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ])
    assert isinstance(out, list) and len(out) == 2
    assert {r["id"] for r in out} == {1, 2}


def test_batch_of_only_notifications_returns_none(srv):
    assert srv.handle_batch([{"jsonrpc": "2.0", "method": "notifications/initialized"}]) is None


def test_empty_batch_is_invalid(srv):
    resp = srv.handle_batch([])
    assert resp["error"]["code"] == -32600


def test_rpc_id_is_echoed(srv):
    resp = srv.handle({"jsonrpc": "2.0", "id": "abc-123", "method": "ping"})
    assert resp["id"] == "abc-123"


def test_structured_and_text_content_agree(srv):
    s = GraphMCPServer(FAKE_CATALOG, _runner_factory([{"x": 1}]), 1)
    resp = _call(s, "q2_tier0_status")
    text = json.loads(resp["result"]["content"][0]["text"])
    assert text == resp["result"]["structuredContent"]
