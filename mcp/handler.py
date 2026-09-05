"""
handler.py — Lambda 传输层（Function URL / API Gateway payload v2）。

鉴权说明：DevOps Agent 注册自定义 MCP server 时，认证方式只有
apiKey / bearerToken / oAuthClientCredentials / oAuth3LO / authorizationDiscovery
五种（实测 devops-agent API 版本 2026-01-01），**没有 SigV4**。所以这里做
api-key 校验，与该 agent space 上已有的 `api-cn` server 一致（头 X-API-Key）。

api-key 从 Secrets Manager 取，不进环境变量、不进代码。
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import sys

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Lambda 包里 rca/ 与本目录同级
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (_HERE, os.path.dirname(_HERE), os.path.join(os.path.dirname(_HERE), "rca")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

API_KEY_HEADER = os.environ.get("MCP_API_KEY_HEADER", "x-api-key").lower()
API_KEY_SECRET_ID = os.environ.get("MCP_API_KEY_SECRET_ID", "")

_server = None
_api_key: str | None = None


def _get_server():
    """server 在容器内复用——避免每次调用重建 22 个工具定义。"""
    global _server
    if _server is None:
        from server import build_default_server  # type: ignore

        _server = build_default_server()
        logger.info("MCP server initialised with %d tools", _server.tool_count())
    return _server


def _get_api_key() -> str | None:
    """从 Secrets Manager 取 api-key。取不到返回 None（视为未配置鉴权）。"""
    global _api_key
    if _api_key is not None:
        return _api_key
    if not API_KEY_SECRET_ID:
        return None
    try:
        import boto3

        sm = boto3.client("secretsmanager")
        val = sm.get_secret_value(SecretId=API_KEY_SECRET_ID)
        raw = val.get("SecretString") or ""
        try:
            parsed = json.loads(raw)
            _api_key = parsed.get("api_key") or parsed.get("apiKey") or raw
        except json.JSONDecodeError:
            _api_key = raw
        return _api_key
    except Exception:  # noqa: BLE001
        logger.exception("无法读取 api-key secret")
        return None


def _headers(event: dict) -> dict:
    return {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}


def _authorized(event: dict) -> tuple[bool, str]:
    expected = _get_api_key()
    if not expected:
        # 未配置密钥时**拒绝**而不是放行——一个能读全图依赖关系的端点
        # 不应该因为忘配密钥就变成匿名可读。
        return False, "服务端未配置 api-key（MCP_API_KEY_SECRET_ID），拒绝请求"
    got = _headers(event).get(API_KEY_HEADER, "")
    if not got:
        return False, f"缺少 {API_KEY_HEADER} 头"
    # 定长比较，避免计时侧信道
    if not hmac.compare_digest(str(got), str(expected)):
        return False, "api-key 不匹配"
    return True, ""


def _body(event: dict):
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    if not raw:
        return None
    return json.loads(raw)


def _resp(status: int, payload) -> dict:
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json",
            "Cache-Control": "no-store",
        },
        "body": "" if payload is None else json.dumps(payload, ensure_ascii=False, default=str),
    }


def lambda_handler(event, context):  # noqa: ANN001
    method = (
        (event.get("requestContext", {}).get("http", {}) or {}).get("method")
        or event.get("httpMethod")
        or "POST"
    ).upper()

    if method == "GET":
        # 健康检查：不泄露工具细节，只报活
        return _resp(200, {"status": "ok", "server": "graph-dependency-platform-mcp"})

    if method != "POST":
        return _resp(405, {"error": f"不支持的方法 {method}"})

    ok, reason = _authorized(event)
    if not ok:
        logger.warning("unauthorized: %s", reason)
        return _resp(401, {"error": "unauthorized"})

    try:
        payload = _body(event)
    except json.JSONDecodeError as exc:
        return _resp(200, {
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32700, "message": f"JSON 解析失败：{exc}"},
        })

    if payload is None:
        return _resp(200, {
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32600, "message": "请求体为空"},
        })

    try:
        result = _get_server().handle_batch(payload)
    except Exception as exc:  # noqa: BLE001
        logger.exception("dispatch failed")
        return _resp(200, {
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"},
        })

    # 全是通知时无响应体，按 HTTP 202 回
    if result is None:
        return _resp(202, None)
    return _resp(200, result)
