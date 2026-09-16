"""
test_43_mcp_agentcore_transport.py — AgentCore Runtime HTTP 传输层测试。

起真实的 ThreadingHTTPServer 发真实 HTTP 请求，但把 MCP server 换成注入了假
runner 的实例，因此**不需要 Neptune**。

覆盖 AgentCore 的协议契约（官方文档的硬要求，违反任何一条 runtime 就不可用）：
  - POST /mcp 接收 RPC
  - 不得拒绝平台注入的 Mcp-Session-Id
  - 健康检查不能依赖 Neptune（否则图谱抖动会让 runtime 被判不健康重启）
  - 通知无响应体
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "rca"), os.path.join(_ROOT, "mcp")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mcp import agentcore_app  # noqa: E402
from mcp.server import GraphMCPServer  # noqa: E402

FAKE_CATALOG = {
    "q2_tier0_status": {
        "mod": "queries", "fn": "q2_tier0_status",
        "desc": "所有 Tier0 服务", "params": {}, "required": [],
    },
    "q22_edge_verification_verdicts": {
        "mod": "queries", "fn": "q22_edge_verification_verdicts",
        "desc": "故障注入验证判定",
        "params": {"status": "confirmed|refuted|inconclusive|untested，可选"},
        "required": [],
    },
}

ROWS = [
    {"src": "petsite", "dst": "ssm", "edge_type": "AccessesData",
     "verify_status": "confirmed", "verify_degradation": 100.0},
]


@pytest.fixture
def base_url(monkeypatch):
    """起一个真实 HTTP server，MCP 层注入假 runner（不连 Neptune）。"""
    fake = GraphMCPServer(FAKE_CATALOG, lambda name, **p: ROWS, contract_version=1)
    monkeypatch.setattr(agentcore_app, "get_mcp", lambda: fake)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), agentcore_app.Handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


def _post(url, payload, headers=None, raw=None):
    body = raw if raw is not None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        text = resp.read().decode() or ""
        return resp.status, (json.loads(text) if text else None)


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as resp:
        text = resp.read().decode() or ""
        return resp.status, (json.loads(text) if text else None)


# ── AgentCore 契约 ───────────────────────────────────────────────────────────
def test_health_check_does_not_require_neptune(base_url, monkeypatch):
    """
    健康检查必须在图谱不可达时也返回 200。
    否则 Neptune 短暂抖动会让 AgentCore 判定 runtime 不健康并重启它——
    把一个查询问题放大成服务中断。
    """
    def boom():
        raise RuntimeError("neptune down")

    monkeypatch.setattr(agentcore_app, "get_mcp", boom)
    status, body = _get(base_url + "/ping")
    assert status == 200
    assert body["status"] == "ok"


def test_platform_injected_session_id_is_not_rejected(base_url):
    """
    AgentCore 会给每个不带 session id 的请求自动加 Mcp-Session-Id。
    stateless 服务必须忽略它——不能因为「我没发过这个 session」而拒绝。
    """
    status, body = _post(
        base_url + "/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"Mcp-Session-Id": "platform-generated-never-seen-before"},
    )
    assert status == 200
    assert len(body["result"]["tools"]) == len(FAKE_CATALOG)


def test_two_calls_with_different_session_ids_both_work(base_url):
    """stateless：不同 session id 之间不共享任何状态，都应正常。"""
    for sid in ("sess-a", "sess-b"):
        status, body = _post(
            base_url + "/mcp",
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Mcp-Session-Id": sid},
        )
        assert status == 200 and body["result"] == {}


def test_initialize_over_http_carries_instructions(base_url):
    status, body = _post(
        base_url + "/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18"}},
    )
    assert status == 200
    instr = body["result"]["instructions"]
    assert "不要编造" in instr
    assert "q22_edge_verification_verdicts" in instr


def test_tools_call_over_http_returns_provenance(base_url):
    status, body = _post(
        base_url + "/mcp",
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "q22_edge_verification_verdicts", "arguments": {}}},
    )
    assert status == 200
    p = body["result"]["structuredContent"]
    assert p["_provenance"]["query"] == "q22_edge_verification_verdicts"
    assert p["_verification"]["by_status"]["confirmed"] == 1


def test_notification_returns_202_with_no_body(base_url):
    status, body = _post(
        base_url + "/mcp", {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )
    assert status == 202
    assert body is None


def test_wrong_path_is_404(base_url):
    with pytest.raises(urllib.error.HTTPError) as ei:
        _post(base_url + "/not-mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert ei.value.code == 404


def test_query_string_on_mcp_path_still_routes(base_url):
    status, body = _post(base_url + "/mcp?foo=bar", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert status == 200 and body["result"] == {}


def test_malformed_json_returns_parse_error(base_url):
    status, body = _post(base_url + "/mcp", None, raw=b"{not json")
    assert status == 200
    assert body["error"]["code"] == -32700


def test_empty_body_rejected(base_url):
    status, body = _post(base_url + "/mcp", None, raw=b"")
    assert status == 200
    assert body["error"]["code"] == -32600


def test_oversized_body_rejected(base_url, monkeypatch):
    monkeypatch.setattr(agentcore_app, "MAX_BODY", 64)
    status, body = _post(base_url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping",
                                             "params": {"pad": "x" * 500}})
    assert status == 200
    assert body["error"]["code"] == -32600
    assert "过大" in body["error"]["message"]


def test_batch_over_http(base_url):
    status, body = _post(base_url + "/mcp", [
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ])
    assert status == 200
    assert isinstance(body, list) and {r["id"] for r in body} == {1, 2}


def test_dispatch_failure_becomes_internal_error(base_url, monkeypatch):
    class Boom:
        def handle_batch(self, payload):
            raise RuntimeError("kaboom")

    monkeypatch.setattr(agentcore_app, "get_mcp", lambda: Boom())
    status, body = _post(base_url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert status == 200
    assert body["error"]["code"] == -32603
    assert "kaboom" in body["error"]["message"]


# ── 配置默认值符合 AgentCore 要求 ────────────────────────────────────────────
def test_defaults_match_agentcore_contract():
    """
    端口 8000、绑 0.0.0.0、路径 /mcp —— 这三个是 AgentCore 的硬要求，
    改了 runtime 就连不上。用测试钉住，避免以后「顺手改个默认值」。
    """
    import importlib

    for var in ("PORT", "BIND_HOST", "MCP_PATH"):
        os.environ.pop(var, None)
    mod = importlib.reload(agentcore_app)
    assert mod.PORT == 8000
    assert mod.BIND_HOST == "0.0.0.0"
    assert mod.MCP_PATH == "/mcp"
