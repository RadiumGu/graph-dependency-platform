"""
agentcore_app.py — Amazon Bedrock AgentCore Runtime 的 MCP 传输层。

## AgentCore Runtime 的协议契约（官方文档要求，不是我们的选择）

| 要求 | 本实现 |
|---|---|
| 监听 `0.0.0.0` | `BIND_HOST`，默认 `0.0.0.0` |
| 端口 `8000` | `PORT`，默认 `8000` |
| `POST /mcp` 接收 MCP RPC | 有 |
| stateless streamable-HTTP | 无会话状态，每个请求自包含 |
| 不得拒绝平台注入的 `Mcp-Session-Id` | 直接忽略该头 |
| 支持 `tools/list`、`tools/call` | 由 server.py 提供 |

## 关于认证：这里刻意不做认证

AgentCore Runtime 在请求到达本容器**之前**就完成了 JWT 校验
（验签、校验 issuer/audience/过期，拒绝无有效 OAuth 2.0 bearer token 的请求）。
所以本服务信任「调用方已被 AgentCore 认证过」，与 API Gateway 后面的内部
微服务同一个模式。

⚠️ **这个前提只在 AgentCore Runtime 里成立。** 如果把本文件直接跑在 EC2 或
独立容器上，它就是一个无认证、能读全图依赖关系的端点。那种场景请改用
`handler.py`（Lambda + api-key）。

## 为什么用标准库而不是 uvicorn/starlette

真正的耗时在 Neptune 查询上，HTTP 层不是瓶颈；而依赖越少，冷启动越快、
打包越小。ThreadingHTTPServer 足够，且不引入需要跟进安全更新的第三方栈。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.join(os.path.dirname(_HERE), "rca")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("graph-mcp")

PORT = int(os.environ.get("PORT", "8000"))
BIND_HOST = os.environ.get("BIND_HOST", "0.0.0.0")  # noqa: S104 - AgentCore 要求
MCP_PATH = os.environ.get("MCP_PATH", "/mcp")
MAX_BODY = int(os.environ.get("MAX_BODY_BYTES", str(2 * 1024 * 1024)))

_server_lock = threading.Lock()
_mcp = None


def get_mcp():
    """
    MCP server 单例。第一次请求时构造——刻意不在模块导入时构造：
    容器启动到第一个请求之间 AgentCore 会做健康检查，导入期失败会让整个
    runtime 起不来，而第一次请求时失败至少能把错误回给调用方。
    """
    global _mcp
    if _mcp is None:
        with _server_lock:
            if _mcp is None:
                from server import build_default_server  # type: ignore

                _mcp = build_default_server()
                logger.info("MCP server ready, %d tools", _mcp.tool_count())
    return _mcp


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "graph-dependency-platform-mcp/1.0"

    # ── 工具 ──────────────────────────────────────────────────────────────
    def _send(self, status: int, payload, ctype: str = "application/json") -> None:
        body = b"" if payload is None else json.dumps(
            payload, ensure_ascii=False, default=str
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _rpc_error(self, code: int, message: str, rpc_id=None) -> None:
        # JSON-RPC 的错误走 HTTP 200 + error 体，这样客户端能拿到结构化原因
        self._send(200, {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}})

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        logger.info("%s - %s", self.address_string(), fmt % args)

    # ── 路由 ──────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        # 健康检查。刻意不回工具清单——那需要连 Neptune，
        # 健康检查不该因为图谱短暂不可达而失败，导致 runtime 被判不健康重启。
        if self.path.rstrip("/") in ("", "/ping", "/health", MCP_PATH.rstrip("/")):
            self._send(200, {"status": "ok", "server": "graph-dependency-platform-mcp"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?")[0].rstrip("/") != MCP_PATH.rstrip("/"):
            self._send(404, {"error": f"未知路径 {self.path}，MCP 端点是 {MCP_PATH}"})
            return

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._rpc_error(-32600, "Content-Length 非法")
            return

        if length <= 0:
            self._rpc_error(-32600, "请求体为空")
            return
        if length > MAX_BODY:
            self._rpc_error(-32600, f"请求体过大（{length} > {MAX_BODY}）")
            return

        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            self._rpc_error(-32700, f"JSON 解析失败：{exc}")
            return
        except Exception as exc:  # noqa: BLE001
            self._rpc_error(-32700, f"读取请求体失败：{type(exc).__name__}")
            return

        # 平台会自动注入 Mcp-Session-Id。stateless 模式下必须**忽略**它，
        # 不能因为「我没发过这个 session」而拒绝——那会让整个 runtime 不可用。
        try:
            result = get_mcp().handle_batch(payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("dispatch failed")
            self._rpc_error(-32603, f"{type(exc).__name__}: {exc}")
            return

        if result is None:
            # 全是通知：按 JSON-RPC 规范无响应体
            self._send(202, None)
        else:
            self._send(200, result)


def main() -> None:
    httpd = ThreadingHTTPServer((BIND_HOST, PORT), Handler)
    httpd.daemon_threads = True
    logger.info("listening on %s:%d%s", BIND_HOST, PORT, MCP_PATH)

    # 预热：把 22+ 个工具定义和查询模块先加载好，让第一个真实请求不吃冷启动。
    # 失败不阻止启动——健康检查要能通过，错误留给第一个请求返回。
    try:
        get_mcp()
    except Exception:  # noqa: BLE001
        logger.exception("预热失败，服务仍启动；首个请求会返回具体错误")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("shutting down")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
