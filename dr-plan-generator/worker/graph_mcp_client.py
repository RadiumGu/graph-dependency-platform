"""graph_mcp_client.py — 经受审的查询目录读图谱，不直连 Neptune。

## 为什么不直连 Neptune

2026-09-26 定案，理由是**查询质量**：直连意味着 worker 自己写 openCypher，
那样快照记录的就不是「图谱事实」，而是「某次临时查询的偶然结果」——
一份灾备计划所倚赖的事实集合不该是这种东西。

东京的 `graph_dependency_mcp`（AgentCore runtime，MCP 协议）暴露一份固定的
`QUERY_CATALOG`，并随结果返回溯源元数据。它自己的 provenance.py 原话：

    determinism: 固定 openCypher，无 LLM 参与，同参数同结果

## 为什么用现有的 Cognito m2m client，而不是另建一个 SigV4 runtime

两条都实测确认过：
  · SigV4 模式在本账号有先例（韩国 temporal_mcp 就是 authorizerConfiguration=null）
  · Cognito 侧已完全建好 m2m：domain graphdp-mcp-1788589178、
    resource server graphdp-mcp/invoke、app client **graphdp-mcp-m2m**
    （flows=["client_credentials"]，有 secret）

所以两条都可行。选现有这条的决定性理由是：**另建一个 runtime 会造出
同一个事实源的两份副本**，而这正是 2026-09-26 在 petsearch 上咬过的故障——
两份 SearchController.java，改了不被构建的那一份，修复静默无效。
两个 MCP runtime 同时提供「图谱事实」，后果相同：有人更新了查询目录，
灾备快照继续读旧的那个，**而一切看起来正常**。对一个只在灾难时才被消费
的组件，「看起来正常」是最坏的失败。

附带好处：JWT 路径由 Bearer token 授权，不走 SigV4 —— worker 完全不需要
`bedrock-agentcore:InvokeAgentRuntime`，唯一新增权限是读一个 secret。
比另建 runtime 的授权面更小。

## 刻意不缓存 token

client_credentials 的 token 有 TTL（Cognito 默认 1 小时）。快照是 6 小时
一次的作业，每次现取一个就够 —— 不缓存意味着没有「缓存里那个刚好过期」
这条路径，也没有状态要跨 activity 重试保持。多一次 HTTP 调用换掉一整类
故障，是合算的。

## 已验证与未验证

实测（2026-09-26）：
  · token 端点无凭证 -> HTTP 400（不是 404，URL 形式正确）
  · MCP 端点无 token -> HTTP 401（不是 404，URL 形式正确）
  · boto3 invoke_agent_runtime（SigV4）-> AccessDeniedException:
    Authorization method mismatch —— 这就是为什么必须走 JWT

**未验证**：拿到真 token 之后的完整往返。secret 还没建，所以
`tools/list` 与 `tools/call` 的真实响应形状尚未见过。
本模块因此对响应做严格校验并在形状不符时**响亮失败**，
不做「取不到就用默认值」那种兜底 —— 那会让一份残缺快照看起来正常。
"""
from __future__ import annotations

import base64
import json
import os
import random
import time
import urllib.parse
import urllib.request

# ── 配置（全部走环境变量，便于换环境）────────────────────────────────────
MCP_RUNTIME_ARN = os.environ.get("DR_GRAPH_MCP_RUNTIME_ARN", "")
MCP_REGION = os.environ.get("DR_GRAPH_MCP_REGION", "ap-northeast-1")
COGNITO_DOMAIN = os.environ.get("DR_GRAPH_MCP_COGNITO_DOMAIN", "")
COGNITO_REGION = os.environ.get("DR_GRAPH_MCP_COGNITO_REGION", "ap-northeast-1")
OAUTH_SCOPE = os.environ.get("DR_GRAPH_MCP_SCOPE", "graphdp-mcp/invoke")
SECRET_ID = os.environ.get("DR_GRAPH_MCP_SECRET_ID", "")
SECRET_REGION = os.environ.get("DR_GRAPH_MCP_SECRET_REGION", "ap-northeast-2")

# 本 workflow 期望的图谱契约版本。
# ⚠️ 这不是装饰。那个 runtime 已经到版本 3，它在演进。
# 契约变了而快照照样存，你会得到一份**形状不同却看起来正常**的快照 ——
# 而这正是直连 Neptune 时根本察觉不到的那类故障。
EXPECTED_CONTRACT_VERSION = os.environ.get("DR_GRAPH_CONTRACT_VERSION", "1")

HTTP_TIMEOUT = int(os.environ.get("DR_GRAPH_MCP_TIMEOUT", "60"))


class GraphMcpError(RuntimeError):
    """图谱 MCP 调用失败。带上足以分辨故障类别的信息。"""


class ContractVersionMismatch(GraphMcpError):
    """契约版本与期望不符 —— 刻意做成独立异常类型，便于在 workflow 里
    与「网络抖动」区分开：前者重试一百次也不会变好。"""


def _require(name: str, value: str) -> str:
    if not value:
        raise GraphMcpError(
            f"缺少配置 {name}。本模块零硬编码账号/端点，全部走环境变量 —— "
            f"见 dr-worker.service"
        )
    return value


# ── 1. 取 client secret ──────────────────────────────────────────────────
def _client_credentials() -> tuple[str, str]:
    """从 Secrets Manager 取 Cognito app client 的 id 与 secret。

    secret 放在**韩国**（worker 所在区域），不在东京：这样读它不依赖东京。
    Cognito 与 MCP runtime 本身在东京，但快照只在东京健康时跑，
    那层依赖是固有的；而把凭证也放东京是白添一层。
    """
    import boto3  # 延迟导入：单测塞假 fetcher 时不需要

    sid = _require("DR_GRAPH_MCP_SECRET_ID", SECRET_ID)
    sm = boto3.client("secretsmanager", region_name=SECRET_REGION)
    raw = sm.get_secret_value(SecretId=sid)["SecretString"]
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        raise GraphMcpError(
            f"secret {sid} 不是 JSON。期望 {{\"client_id\":…,\"client_secret\":…}}"
        ) from e
    cid, csec = d.get("client_id"), d.get("client_secret")
    if not cid or not csec:
        raise GraphMcpError(
            f"secret {sid} 缺 client_id 或 client_secret（键名必须正好是这两个）"
        )
    return cid, csec


# ── 2. 换 token ──────────────────────────────────────────────────────────
def fetch_token() -> str:
    """client_credentials 换 access token。不缓存 —— 见模块文档。"""
    domain = _require("DR_GRAPH_MCP_COGNITO_DOMAIN", COGNITO_DOMAIN)
    cid, csec = _client_credentials()
    url = f"https://{domain}.auth.{COGNITO_REGION}.amazoncognito.com/oauth2/token"
    body = urllib.parse.urlencode(
        {"grant_type": "client_credentials", "scope": OAUTH_SCOPE}
    ).encode()
    basic = base64.b64encode(f"{cid}:{csec}".encode()).decode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        # 400 invalid_client 与 400 invalid_scope 是两种完全不同的配置错误，
        # 都会被 requests 报成同一个 400 —— 所以把响应体带出来。
        raise GraphMcpError(
            f"取 token 失败 HTTP {e.code}：{detail}\n"
            f"  401/invalid_client -> secret 不对或已被轮换\n"
            f"  400/invalid_scope  -> scope 与 resource server 不符（当前 {OAUTH_SCOPE}）"
        ) from e
    tok = d.get("access_token")
    if not tok:
        raise GraphMcpError(f"token 响应里没有 access_token：{list(d)}")
    return tok


# ── 3. 调 MCP ────────────────────────────────────────────────────────────
def _endpoint() -> str:
    arn = _require("DR_GRAPH_MCP_RUNTIME_ARN", MCP_RUNTIME_ARN)
    enc = urllib.parse.quote(arn, safe="")
    return (
        f"https://bedrock-agentcore.{MCP_REGION}.amazonaws.com"
        f"/runtimes/{enc}/invocations?qualifier=DEFAULT"
    )


def _session_id() -> str:
    # 实测注记（见 scripts/loadgen_full.py）：runtimeSessionId 有长度下限，
    # 需 >= 33 字符，太短会被拒。
    return f"dr-snapshot-{int(time.time()*1000)}-{random.randint(10**12, 10**13)}"


def rpc(method: str, params: dict | None = None, token: str | None = None) -> dict:
    """发一条 MCP JSON-RPC 请求，返回 result。

    刻意不把 JSON-RPC error 转成 None/{} —— 一个静默的空结果会变成
    一份残缺却看起来正常的快照。
    """
    tok = token or fetch_token()
    body: dict = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        body["params"] = params
    req = urllib.request.Request(
        _endpoint(),
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {tok}",
            "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": _session_id(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        # 这几个状态码含义不同，混成一句「调用失败」会让排查从头再来。
        hint = {
            401: "token 无效或过期（本模块不缓存 token，所以更可能是 secret 被轮换）",
            403: "token 有效但 scope 不含 graphdp-mcp/invoke，或该 client 不在 allowedClients 里",
            404: "runtime ARN 或 qualifier 不对（URL 形式已于 2026-09-26 验证过，先怀疑 ARN）",
        }.get(e.code, "")
        raise GraphMcpError(f"MCP 调用失败 HTTP {e.code}：{detail}\n  {hint}") from e

    if "error" in payload:
        err = payload["error"]
        raise GraphMcpError(
            f"MCP 返回 JSON-RPC error {err.get('code')}: {err.get('message')}"
        )
    if "result" not in payload:
        raise GraphMcpError(f"MCP 响应既无 result 也无 error：{list(payload)}")
    return payload["result"]


def list_tools(token: str | None = None) -> list[dict]:
    res = rpc("tools/list", {}, token=token)
    tools = res.get("tools")
    if not isinstance(tools, list) or not tools:
        raise GraphMcpError(f"tools/list 没返回工具清单：{res}")
    return tools


def query(name: str, token: str | None = None, **params) -> dict:
    """调一条目录查询，校验溯源与契约版本后返回。

    返回 {"rows": …, "provenance": …} —— 两者都进快照。
    只存 rows 不存 provenance 是错的：那样以后没法回答
    「这条事实是哪个查询、哪个契约版本、什么时候取的」。
    """
    res = rpc("tools/call", {"name": name, "arguments": params}, token=token)

    if res.get("isError"):
        txt = _text_of(res)
        raise GraphMcpError(f"查询 {name} 返回 isError：{txt[:300]}")

    doc = _parse_content(res, name)
    prov = doc.get("provenance")
    if not isinstance(prov, dict):
        raise GraphMcpError(
            f"查询 {name} 的响应里没有 provenance。"
            f"本 server 的设计是每条结果都带溯源 —— 没有它说明 server 版本变了，"
            f"而不是「这次恰好没有」。"
        )

    got = str(prov.get("graph_contract_version", "unknown"))
    if got != str(EXPECTED_CONTRACT_VERSION):
        raise ContractVersionMismatch(
            f"图谱契约版本不符：期望 {EXPECTED_CONTRACT_VERSION}，实际 {got}（查询 {name}）。\n"
            f"  刻意做成失败而不是警告：契约变了还照样存快照，"
            f"会得到一份形状不同却看起来正常的快照。\n"
            f"  确认新契约兼容后，改 DR_GRAPH_CONTRACT_VERSION 并重跑。"
        )
    return doc


def _text_of(res: dict) -> str:
    parts = res.get("content") or []
    return "\n".join(p.get("text", "") for p in parts if isinstance(p, dict))


def _parse_content(res: dict, name: str) -> dict:
    """MCP 的 tools/call 把结果放在 content[].text 里，本 server 放的是 JSON。"""
    txt = _text_of(res)
    if not txt:
        raise GraphMcpError(f"查询 {name} 返回空 content：{res}")
    try:
        doc = json.loads(txt)
    except json.JSONDecodeError as e:
        raise GraphMcpError(
            f"查询 {name} 的 content 不是 JSON（前 200 字符）：{txt[:200]}"
        ) from e
    if not isinstance(doc, dict):
        raise GraphMcpError(f"查询 {name} 的 content 不是对象：{type(doc).__name__}")
    return doc


# ── 灾备快照要用的那几条 ─────────────────────────────────────────────────
# 都来自受审的 QUERY_CATALOG。缺什么事实**应当往目录里加一条**
# （一次对版本化契约的受审改动），而不是在这里写临时查询 ——
# 那会把「不直连 Neptune」这个决定的意义抹掉。
SNAPSHOT_QUERIES = (
    "q14_cross_region_resources",
    "q13_data_layer_topology",
    "q12_service_dependency_tree",
    "q15_critical_path",
    "q2_tier0_status",
)
