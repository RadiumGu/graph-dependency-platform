#!/usr/bin/env python3
"""
graph_mcp_server.py — 依赖图谱的 MCP 服务端(stdio / JSON-RPC 2.0)

让**任意语言、任意框架**的 agent 都能查询依赖图谱。此前图谱只能在 VPC 内、
用 Python、把 rca/ 挂进 sys.path 才访问得到(rca/.mcp.json 是空的
{"mcpServers": {}}，仓库里唯一含 MCP 的文件 chaos_mcp.py 做的是故障注入)。

## 架构定位(见 todo/goal-loop/tasks.md 的架构决策)

    外部 agent / 人类临时提问
            ↓  本文件(MCP)
    ┌───────────────────────────────┐
    │ 确定性查询层 rca/neptune/      │
    │   neptune_queries  Q1–Q18     │
    │   query_guard      只读校验    │
    │   neptune_client   SigV4+复用  │
    └───────────────────────────────┘

rca / chaos / dr-plan **三个模块自己不走本文件**,它们直接调用确定性查询层:
  - LLM 在事故热路径上带来延迟与不确定性(NL→Cypher 是两次 Bedrock 往返)
  - 写路径有 schema 契约,不能 LLM 中介(query_guard 本身就是只读设计)
  - dr-plan 的图算法(Kahn / DFS / 最长路径 DP)需要全量精确数据
  - MCP 端点若与被诊断对象同处一个 VPC,会共享故障域

## 为什么不用 mcp 官方 SDK

本机 python3 是 3.9.25,而 mcp 包要求 ≥3.10。MCP stdio 传输就是 JSON-RPC 2.0,
需要的表面很小(initialize / tools/list / tools/call),故用**纯标准库**实现:
零依赖、可直接落到任何环境、无供应链面。

## 用法

    python3 graph_mcp_server.py            # stdio 模式,供 MCP 客户端拉起
    python3 graph_mcp_server.py --selftest # 不连 Neptune,自检协议与工具注册

环境变量:NEPTUNE_ENDPOINT / NEPTUNE_PORT / REGION(与 rca 模块一致)
"""

import json
import os
import sys
import traceback

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "graph-dependency"
SERVER_VERSION = "1.0.0"

_HERE = os.path.dirname(os.path.abspath(__file__))
_RCA_DIR = os.path.dirname(_HERE)                 # .../rca
_PROJECT_ROOT = os.path.dirname(_RCA_DIR)         # 仓库根
for _p in (_RCA_DIR, _PROJECT_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 懒加载：--selftest 不应因为缺 boto3 / 连不上 Neptune 而失败
_mods = {}


def _load(name: str):
    """按需导入并缓存，导入失败时抛出带上下文的错误。"""
    if name in _mods:
        return _mods[name]
    if name == "queries":
        from neptune import neptune_queries as m
    elif name == "guard":
        from neptune import query_guard as m
    elif name == "client":
        from neptune import neptune_client as m
    elif name == "dr":
        # dr-plan-generator 目录名带连字符，不能当包导入，按文件路径加载
        import importlib.util
        path = os.path.join(_PROJECT_ROOT, "dr-plan-generator", "graph", "queries.py")
        dr_dir = os.path.join(_PROJECT_ROOT, "dr-plan-generator")
        if dr_dir not in sys.path:
            sys.path.insert(0, dr_dir)
        spec = importlib.util.spec_from_file_location("dr_graph_queries", path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    else:
        raise KeyError(name)
    _mods[name] = m
    return m


# ─── 查询库注册表 ──────────────────────────────────────────────────────────
# 把此前割裂在两个模块的查询库(rca 持 Q1–Q11/Q17/Q18、dr-plan 持 Q12–Q16)
# 统一从一个入口暴露。每项声明所属模块、函数名、参数与用途。

QUERY_REGISTRY = {
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
    "q3_upstream_deps": {
        "mod": "queries", "fn": "q3_upstream_deps",
        "desc": "根因候选：直接依赖故障服务的上游节点。建议 kind='live'",
        "params": {"failed_service": "str，必填", "kind": "static|dynamic|live，可选"},
        "required": ["failed_service"],
    },
    "q4_service_info": {
        "mod": "queries", "fn": "q4_service_info",
        "desc": "单个服务的完整属性", "params": {"service_name": "str，必填"},
        "required": ["service_name"],
    },
    "q5_similar_incidents": {
        "mod": "queries", "fn": "q5_similar_incidents",
        "desc": "同一服务的历史已解决故障。注意它过滤 status='resolved'",
        "params": {"service_name": "str，必填", "limit": "int，默认 3"},
        "required": ["service_name"],
    },
    "q6_pod_status": {
        "mod": "queries", "fn": "q6_pod_status",
        "desc": "服务关联的 Pod 状态与重启次数",
        "params": {"service_name": "str，必填"}, "required": ["service_name"],
    },
    "q7_db_connections": {
        "mod": "queries", "fn": "q7_db_connections",
        "desc": "服务关联的数据库节点状态",
        "params": {"service_name": "str，必填"}, "required": ["service_name"],
    },
    "q8_log_source": {
        "mod": "queries", "fn": "q8_log_source",
        "desc": "服务的日志源配置",
        "params": {"service_name": "str，必填"}, "required": ["service_name"],
    },
    "q9_service_infra_path": {
        "mod": "queries", "fn": "q9_service_infra_path",
        "desc": "Service→Pod→EC2→AZ 完整基础设施链路",
        "params": {"service_name": "str，必填"}, "required": ["service_name"],
    },
    "q10_infra_root_cause": {
        "mod": "queries", "fn": "q10_infra_root_cause",
        "desc": "非 running 的 EC2 反查受影响服务与 AZ 影响面",
        "params": {"affected_service": "str，必填"}, "required": ["affected_service"],
    },
    "q11_broader_impact": {
        "mod": "queries", "fn": "q11_broader_impact",
        "desc": "给定故障 EC2 列表反查所有受影响服务",
        "params": {"ec2_ids": "list[str]，必填"}, "required": ["ec2_ids"],
    },
    "q17_incidents_by_resource": {
        "mod": "queries", "fn": "q17_incidents_by_resource",
        "desc": "经 MentionsResource 边查涉及同一资源的历史 Incident",
        "params": {"resource_name": "str，必填", "limit": "int，默认 5"},
        "required": ["resource_name"],
    },
    "q18_chaos_history": {
        "mod": "queries", "fn": "q18_chaos_history",
        "desc": "经 TestedBy 边查服务的混沌实验历史",
        "params": {"service_name": "str，必填", "limit": "int，默认 5"},
        "required": ["service_name"],
    },
    # ── 以下来自 dr-plan-generator ──
    "q12_az_dependency_tree": {
        "mod": "dr", "fn": "q12_az_dependency_tree",
        "desc": "AZ 维度依赖树", "params": {"az_name": "str，必填"},
        "required": ["az_name"],
    },
    "q12_service_dependency_tree": {
        "mod": "dr", "fn": "q12_service_dependency_tree",
        "desc": "服务维度依赖树", "params": {"service_name": "str，必填"},
        "required": ["service_name"],
    },
    "q13_data_layer_topology": {
        "mod": "dr", "fn": "q13_data_layer_topology",
        "desc": "数据层拓扑", "params": {}, "required": [],
    },
    "q14_cross_region_resources": {
        "mod": "dr", "fn": "q14_cross_region_resources",
        "desc": "跨区复制资源", "params": {}, "required": [],
    },
    "q15_critical_path": {
        "mod": "dr", "fn": "q15_critical_path",
        "desc": "关键路径", "params": {}, "required": [],
    },
    "q16_single_point_of_failure": {
        "mod": "dr", "fn": "q16_single_point_of_failure",
        "desc": "单点故障检测", "params": {}, "required": [],
    },
}


# ─── 工具实现 ──────────────────────────────────────────────────────────────

def _tool_list_queries(_args):
    lines = ["图谱固化查询库（确定性，推荐优先使用）：", ""]
    for name, spec in QUERY_REGISTRY.items():
        src = "rca" if spec["mod"] == "queries" else "dr-plan"
        ps = ", ".join(f"{k}({v})" for k, v in spec["params"].items()) or "无参数"
        lines.append(f"- {name}  [{src}]")
        lines.append(f"    {spec['desc']}")
        lines.append(f"    参数: {ps}")
    return "\n".join(lines)


def _tool_run_query(args):
    name = args.get("query_name")
    if not name:
        raise ValueError("缺少 query_name")
    spec = QUERY_REGISTRY.get(name)
    if not spec:
        raise ValueError(
            f"未知查询 '{name}'。可用: {', '.join(sorted(QUERY_REGISTRY))}"
        )
    params = args.get("params") or {}
    missing = [r for r in spec["required"] if r not in params]
    if missing:
        raise ValueError(f"{name} 缺少必填参数: {missing}")
    mod = _load(spec["mod"])
    fn = getattr(mod, spec["fn"])
    result = fn(**params)
    return json.dumps(result, ensure_ascii=False, indent=2, default=str)


def _tool_get_schema(args):
    section = args.get("section", "all")
    from neptune import schema_prompt
    if hasattr(schema_prompt, "get_schema_section"):
        return schema_prompt.get_schema_section(section)
    # 回退：直接取 profile 里的 schema 文本
    from config import profile
    return (profile.get("neptune", {}) or {}).get("graph_schema_text", "") or "(schema 为空)"


def _tool_run_cypher(args):
    cypher = args.get("cypher")
    if not cypher:
        raise ValueError("缺少 cypher")
    guard = _load("guard")
    ok, reason = guard.is_safe(cypher)
    if not ok:
        # 明确拒绝而非降级执行：本端点对外开放，写操作必须走类型化的模块代码
        raise ValueError(f"查询被 query_guard 拒绝（只读+跳数限制）: {reason}")
    safe = guard.ensure_limit(cypher)
    client = _load("client")
    rows = client.results(safe)
    return json.dumps({"cypher": safe, "row_count": len(rows), "rows": rows},
                      ensure_ascii=False, indent=2, default=str)


TOOLS = [
    {
        "name": "list_queries",
        "description": ("列出图谱固化查询库（Q1–Q18，含 rca 与 dr-plan 两个模块）。"
                        "先调这个看有哪些确定性查询可用，再用 run_query 执行。"),
        "inputSchema": {"type": "object", "properties": {}},
        "handler": _tool_list_queries,
    },
    {
        "name": "run_query",
        "description": ("按名称执行固化查询并返回 JSON。**优先用这个而不是 run_cypher** —— "
                        "固化查询是确定性的、经过验证的。影响面/根因类查询建议传 "
                        "params.kind='live'，只看当前仍然存在的依赖。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query_name": {"type": "string", "description": "查询名，见 list_queries"},
                "params": {"type": "object", "description": "查询参数字典"},
            },
            "required": ["query_name"],
        },
        "handler": _tool_run_query,
    },
    {
        "name": "get_schema",
        "description": ("返回图谱 schema（31 种节点 / 26 种边及依赖边的通用属性）。"
                        "写 Cypher 前先看这个。"),
        "inputSchema": {
            "type": "object",
            "properties": {"section": {"type": "string",
                                       "description": "all | nodes | edges，默认 all"}},
        },
        "handler": _tool_get_schema,
    },
    {
        "name": "run_cypher",
        "description": ("执行**只读** openCypher。经 query_guard 校验："
                        "拒绝 CREATE/DELETE/SET/MERGE/REMOVE/DROP/CALL，限制跳数，"
                        "自动补 LIMIT。固化查询覆盖不到的临时问题才用它。"),
        "inputSchema": {
            "type": "object",
            "properties": {"cypher": {"type": "string", "description": "只读 openCypher"}},
            "required": ["cypher"],
        },
        "handler": _tool_run_cypher,
    },
]

_HANDLERS = {t["name"]: t["handler"] for t in TOOLS}
_TOOL_SPECS = [{k: v for k, v in t.items() if k != "handler"} for t in TOOLS]


# ─── JSON-RPC 2.0 / stdio ─────────────────────────────────────────────────

def _reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def handle(msg: dict):
    """处理一条 JSON-RPC 消息。通知（无 id）不回复。"""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        _reply(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    elif method in ("notifications/initialized", "initialized"):
        pass  # 通知，无需回复
    elif method == "tools/list":
        _reply(msg_id, {"tools": _TOOL_SPECS})
    elif method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = _HANDLERS.get(name)
        if fn is None:
            _reply(msg_id, error={"code": -32601, "message": f"未知工具: {name}"})
            return
        try:
            text = fn(args)
            _reply(msg_id, {"content": [{"type": "text", "text": text}]})
        except Exception as e:
            # 按 MCP 约定，工具执行错误作为 isError 结果返回，而非协议级 error，
            # 这样调用方 agent 能看到错误内容并自行纠正
            _reply(msg_id, {
                "content": [{"type": "text",
                             "text": f"{type(e).__name__}: {e}"}],
                "isError": True,
            })
    elif method == "ping":
        _reply(msg_id, {})
    elif msg_id is not None:
        _reply(msg_id, error={"code": -32601, "message": f"未实现的方法: {method}"})


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _reply(None, error={"code": -32700, "message": "JSON 解析失败"})
            continue
        try:
            handle(msg)
        except Exception:
            sys.stderr.write(traceback.format_exc())
            if msg.get("id") is not None:
                _reply(msg["id"], error={"code": -32603, "message": "服务端内部错误"})


def selftest() -> int:
    """不连 Neptune，自检协议握手、工具注册与参数校验。"""
    print(f"MCP 自检 — {SERVER_NAME} v{SERVER_VERSION} (protocol {PROTOCOL_VERSION})")
    print(f"  已注册工具: {[t['name'] for t in _TOOL_SPECS]}")
    print(f"  查询库条目: {len(QUERY_REGISTRY)} "
          f"(rca {sum(1 for s in QUERY_REGISTRY.values() if s['mod']=='queries')} + "
          f"dr-plan {sum(1 for s in QUERY_REGISTRY.values() if s['mod']=='dr')})")
    ok = True
    listing = _tool_list_queries({})
    assert "q1_blast_radius" in listing and "q16_single_point_of_failure" in listing
    print("  list_queries: ✅")
    for bad, why in ((None, "缺 query_name"), ("q999", "未知查询")):
        try:
            _tool_run_query({"query_name": bad} if bad else {})
            print(f"  参数校验({why}): ❌ 未拒绝")
            ok = False
        except ValueError:
            print(f"  参数校验({why}): ✅ 已拒绝")
    try:
        _tool_run_query({"query_name": "q1_blast_radius", "params": {}})
        print("  必填参数校验: ❌ 未拒绝")
        ok = False
    except ValueError:
        print("  必填参数校验: ✅ 已拒绝")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    serve()
