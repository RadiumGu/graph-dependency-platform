"""
server.py — MCP 协议分派（JSON-RPC 2.0），与传输层解耦。

刻意不依赖任何 MCP SDK：这个 server 只需要实现 initialize / tools/list /
tools/call / ping 四个方法，自己写一百来行比引一个包更好控——尤其它要打进
Lambda 包，而依赖越少冷启动越快。

传输层（Lambda Function URL / API Gateway）在 handler.py，本模块只吃 dict 出 dict，
因此可以直接单元测试，不需要起 HTTP。
"""
from __future__ import annotations

import logging
from typing import Any

# 两种导入形态都要支持：
#   - 作为包（本地开发 / 测试）：mcp.server
#   - 扁平（Lambda 包把模块平铺在根）：server
try:  # pragma: no cover - 取决于部署形态
    from . import catalog_tools, provenance
except ImportError:  # pragma: no cover
    import catalog_tools  # type: ignore
    import provenance  # type: ignore

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "graph-dependency-platform"
SERVER_VERSION = "1.0.0"

# JSON-RPC 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class GraphMCPServer:
    """
    依赖注入式设计：catalog / runner / contract_version 都从外面传进来，
    这样单元测试可以塞假的 runner，不需要连 Neptune。
    """

    def __init__(self, catalog: dict, runner, contract_version: Any = "unknown"):
        self._catalog = catalog
        self._runner = runner          # callable(name, **params) -> rows
        self._contract_version = contract_version
        self._tools = catalog_tools.build_tools(catalog)

    # ── 元信息 ────────────────────────────────────────────────────────────
    @property
    def tools(self) -> list[dict]:
        return self._tools

    def tool_count(self) -> int:
        return len(self._tools)

    # ── 方法实现 ──────────────────────────────────────────────────────────
    def _initialize(self, params: dict) -> dict:
        client_proto = (params or {}).get("protocolVersion")
        if client_proto and client_proto != PROTOCOL_VERSION:
            # 不因协议版本不同而拒绝——回自己支持的版本，让客户端决定
            logger.info("client protocolVersion=%s, server=%s", client_proto, PROTOCOL_VERSION)
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            # 这个字段是本 server 的核心：证据纪律要进模型上下文
            "instructions": provenance.SERVER_INSTRUCTIONS,
        }

    def _tools_list(self, params: dict) -> dict:
        return {"tools": self._tools}

    def _tools_call(self, params: dict) -> dict:
        params = params or {}
        name = params.get("name")
        args = params.get("arguments") or {}

        if not name:
            raise _RpcError(INVALID_PARAMS, "缺少 name")
        entry = self._catalog.get(name)
        if entry is None:
            raise _RpcError(
                METHOD_NOT_FOUND,
                f"未知工具 '{name}'。可用工具共 {len(self._tools)} 个，"
                f"调用 tools/list 获取清单。",
            )

        clean, errors = catalog_tools.validate_args(entry, args)
        if errors:
            # 参数错误按 isError 返回而不是 JSON-RPC error：
            # 这样 agent 能看到错误文本并自行改正，而不是拿到一个协议级失败
            return self._text_result(
                "参数校验失败：\n- " + "\n- ".join(errors), is_error=True
            )

        try:
            rows = self._runner(name, **clean)
            if isinstance(rows, dict):
                rows = rows.get("results", rows)
        except Exception as exc:  # noqa: BLE001
            logger.exception("query %s failed", name)
            return self._json_result(
                provenance.error_result(name, clean, exc), is_error=True
            )

        payload = provenance.wrap_result(
            name, clean, rows, self._contract_version,
            dependency_bearing=name in catalog_tools.DEPENDENCY_BEARING,
        )
        return self._json_result(payload)

    # ── 结果封装 ──────────────────────────────────────────────────────────
    @staticmethod
    def _text_result(text: str, is_error: bool = False) -> dict:
        return {"content": [{"type": "text", "text": text}], "isError": is_error}

    @staticmethod
    def _json_result(payload: dict, is_error: bool = False) -> dict:
        import json

        return {
            "content": [{
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            }],
            "isError": is_error,
            # structuredContent 让支持的客户端拿到结构化数据而不用解析文本
            "structuredContent": payload,
        }

    # ── 分派 ──────────────────────────────────────────────────────────────
    def handle(self, request: dict) -> dict | None:
        """
        处理单条 JSON-RPC 请求。通知（无 id）返回 None —— 按规范不应回响应。
        """
        if not isinstance(request, dict):
            return _err_response(None, INVALID_REQUEST, "请求必须是 JSON 对象")

        rpc_id = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}
        is_notification = "id" not in request

        if not method:
            return None if is_notification else _err_response(
                rpc_id, INVALID_REQUEST, "缺少 method"
            )

        # 通知类：initialized / cancelled 等，确认即可，不回响应
        if is_notification:
            logger.info("notification: %s", method)
            return None

        handlers = {
            "initialize": self._initialize,
            "tools/list": self._tools_list,
            "tools/call": self._tools_call,
            "ping": lambda p: {},
        }
        fn = handlers.get(method)
        if fn is None:
            return _err_response(
                rpc_id, METHOD_NOT_FOUND,
                f"未实现的方法 '{method}'。本 server 支持："
                + ", ".join(sorted(handlers)),
            )

        try:
            result = fn(params)
        except _RpcError as exc:
            return _err_response(rpc_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001
            logger.exception("method %s failed", method)
            return _err_response(rpc_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    def handle_batch(self, payload: Any) -> Any:
        """支持 JSON-RPC 批量请求（数组）。全是通知时返回 None。"""
        if isinstance(payload, list):
            if not payload:
                return _err_response(None, INVALID_REQUEST, "批量请求不能为空数组")
            out = [r for r in (self.handle(x) for x in payload) if r is not None]
            return out or None
        return self.handle(payload)


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _err_response(rpc_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": code, "message": message}}


# ── 生产构造入口 ──────────────────────────────────────────────────────────────
def build_default_server() -> GraphMCPServer:
    """
    用仓内真实的 QUERY_CATALOG 与 run_query 构造 server。
    导入放在函数内：让单元测试能在不具备 Neptune / 依赖的环境里 import 本模块。
    """
    from neptune.query_catalog import QUERY_CATALOG, run_query  # type: ignore

    version: Any = "unknown"
    try:
        import os

        import yaml

        here = os.path.dirname(os.path.abspath(__file__))
        # 两种部署形态的契约位置不同，都要试：
        #   包内（本地开发）  <repo>/profiles/graph_contract.yaml  → 上一级
        #   扁平（AgentCore 直接代码部署 / Lambda）  ./profiles/...  → 同级
        # 只试上一级会让扁平部署下 version 静默变成 "unknown"，
        # 而 provenance 里的契约版本正是给 agent 判断数据新旧用的。
        candidates = [
            os.path.join(os.path.dirname(here), "profiles", "graph_contract.yaml"),
            os.path.join(here, "profiles", "graph_contract.yaml"),
        ]
        for cpath in candidates:
            if os.path.exists(cpath):
                with open(cpath, encoding="utf-8") as fh:
                    version = (yaml.safe_load(fh) or {}).get("version", "unknown")
                break
        else:
            logger.warning("未找到 graph_contract.yaml，尝试过：%s", candidates)
    except Exception:  # noqa: BLE001
        logger.exception("读取契约版本失败")

    return GraphMCPServer(QUERY_CATALOG, run_query, version)
