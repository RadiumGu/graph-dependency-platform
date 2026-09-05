"""
catalog_tools.py — 把 QUERY_CATALOG 的 22 条预置查询转成 MCP 工具定义。

设计取舍：
1. **不暴露裸 Cypher。** 另一条路是直接给 agent 一个 run_cypher 工具（awslabs
   Neptune MCP server 就是这么做的），但那等于邀请 agent 自己写查询——写错了
   它不知道，写出笛卡尔积它也不知道。预置查询的参数契约是显式的、Cypher 是
   审过的，这才是能给托管 agent 用的形态。
2. **工具名沿用目录键**（如 `q1_blast_radius`）。语义化命名（`blast_radius`）
   对 LLM 更友好，但目录键可追溯到 `rca/neptune/query_catalog.py` 里的具体实现，
   出问题时能对账。语义由 description 承载。
3. **工具数量 = len(QUERY_CATALOG)**，不写死。目录里加一条查询，工具自动多一个。
"""
from __future__ import annotations

from typing import Any

# 这些查询直接回答依赖关系问题，响应需要富化验证状态（见 provenance.py）。
# 不是靠猜——依据是它们的 Cypher 会返回依赖边或依赖路径。
DEPENDENCY_BEARING = {
    "q1_blast_radius",
    "q3_upstream_deps",
    "q11_broader_impact",
    "q12_az_dependency_tree",
    "q12_service_dependency_tree",
    "q15_critical_path",
    "q16_single_point_of_failure",
    "q20_dependency_verification",
    "q21_observation_source_coverage",
    "q22_edge_verification_verdicts",
    "q23_verification_coverage",
}

#: 目录里 desc 太短、不足以让 agent 判断「什么时候该用这个工具」的，补充用法说明。
USAGE_HINTS = {
    "q1_blast_radius": (
        "用于回答「X 挂了会连带影响什么」。kind='live' 取运行时观测到的依赖，"
        "'static' 取声明的依赖，两者不一致本身就是有价值的信号。"
        "返回的是一个对象（含 services 与 capabilities 两个集合），不是行列表。"
    ),
    "q3_upstream_deps": (
        "根因排查的主力工具：谁直接依赖这个故障服务。建议 kind='live'。"
        "注意方向——这是找上游调用者，不是找下游受害者（那是 q1）。"
    ),
    "q16_single_point_of_failure": (
        "单点故障由图算法算出，不是推理得来的。可直接采信其拓扑结论。"
    ),
    # ⚠️ q20 与 q22 名字里都有 verification，但**层次不同**，不可混用。
    "q20_dependency_verification": (
        "**观测层**验证：这条边最近有没有被观测到（字段 drift_status / "
        "runtime_verified / verified_by）。用于发现「声明了但没被观测到」、"
        "「只被单一观测源看到」、「验证已过期」三类问题。\n"
        "它**不是**故障注入判定——要那个请用 q22_edge_verification_verdicts。"
    ),
    "q22_edge_verification_verdicts": (
        "**干预层**验证，本平台的核心产出：在依赖目标端注入故障、观测源端是否退化。\n"
        "**讨论任何依赖关系之前先调它。** verify_status 四态：\n"
        "  confirmed —— 已确认，verify_degradation 给出影响强度，可采信\n"
        "  refuted —— 已证伪，**不得作为推理依据**\n"
        "  inconclusive —— 证据不足（退化落在 5%–20%／观测流量不足／注入是否生效未知）\n"
        "  untested —— 未验证（多数边的状态，属正常），引用时必须声明"
    ),
    "q23_verification_coverage": (
        "验证覆盖率总览：verified_ratio = (confirmed + refuted) / 全部依赖边。"
        "先看这个再决定要不要逐边下钻。比例低不代表数据差——"
        "业界依赖图这个数字都是 100% 未验证，只是无从计算。返回对象，不是行列表。"
    ),
    "q21_observation_source_coverage": (
        "各观测源（xray / deepflow / nfm）之间的对账结果。"
        "只被单一源看到的依赖可信度低——历史上单一观测源造成过 85% 的假阴性。"
    ),
    "q19_topology_changes": (
        "依赖关系的出现/消失事件流。用于回答「最近拓扑变过吗」，"
        "这是多数依赖图无法回答的问题（它们没有持久化的边实体）。"
    ),
    "q10_infra_root_cause": (
        "从基础设施侧反查：哪些 EC2 非 running、进而影响了哪些服务。"
        "与 q3 互补——q3 走服务依赖，这个走承载关系。"
        "返回对象（含 unhealthy_ec2 / az_impact / has_infra_fault），不是行列表。"
    ),
    "q4_service_info": "单个服务的完整属性。返回对象，不是行列表。",
    "q8_log_source": "服务的日志源配置。返回单个字符串，不是行列表。",
}


def _param_schema(pname: str, hint: str) -> dict:
    """
    把目录里的中文参数提示转成 JSON Schema。
    提示形如 'str，必填' / 'int，默认 3' / 'static|dynamic|live，可选'。
    """
    h = str(hint)
    schema: dict[str, Any] = {"description": h}

    if "|" in h:
        # 枚举型：取第一个逗号/顿号前的部分按 | 切
        head = h.split("，")[0].split(",")[0]
        opts = [o.strip() for o in head.split("|") if o.strip()]
        if len(opts) > 1:
            schema["type"] = "string"
            schema["enum"] = opts
            return schema

    if "int" in h:
        schema["type"] = "integer"
        for tok in h.replace("默认", " ").replace("，", " ").split():
            if tok.isdigit():
                schema["default"] = int(tok)
                break
        return schema

    if "list" in h.lower():
        schema["type"] = "array"
        schema["items"] = {"type": "string"}
        return schema

    if "bool" in h.lower():
        schema["type"] = "boolean"
        return schema

    schema["type"] = "string"
    return schema


def _describe(name: str, entry: dict) -> str:
    desc = entry.get("desc") or name
    kind = "根因分析（RCA）" if entry.get("mod") == "queries" else "灾备（DR）"
    parts = [f"{desc}", "", f"分类：{kind}　实现：{entry.get('mod')}.{entry.get('fn')}"]
    if USAGE_HINTS.get(name):
        parts += ["", USAGE_HINTS[name]]
    if name in DEPENDENCY_BEARING:
        parts += [
            "",
            "响应含 `_verification` 字段：该结果涉及的依赖边有多少经过故障注入验证。",
        ]
    parts += [
        "",
        "确定性查询——固定 openCypher，无 LLM 参与，同参数同结果。",
    ]
    return "\n".join(parts)


def build_tools(catalog: dict) -> list[dict]:
    """从 QUERY_CATALOG 生成 MCP tools/list 的 tools 数组。"""
    tools = []
    for name in sorted(catalog):
        entry = catalog[name] or {}
        params_spec = entry.get("params", {}) or {}
        required = list(entry.get("required", []) or [])

        props = {p: _param_schema(p, hint) for p, hint in params_spec.items()}
        tools.append({
            "name": name,
            "description": _describe(name, entry),
            "inputSchema": {
                "type": "object",
                "properties": props,
                "required": required,
                "additionalProperties": False,
            },
        })
    return tools


def validate_args(entry: dict, args: dict) -> tuple[dict, list[str]]:
    """
    校验并规整入参。返回 (clean_args, errors)。
    只放行目录声明过的参数——避免把任意 kwargs 透传进查询函数。
    """
    params_spec = entry.get("params", {}) or {}
    required = list(entry.get("required", []) or [])
    errors: list[str] = []

    unknown = [k for k in args if k not in params_spec]
    if unknown:
        errors.append(
            f"未声明的参数：{', '.join(sorted(unknown))}。"
            f"该查询只接受：{', '.join(sorted(params_spec)) or '（无参数）'}"
        )

    clean = {k: v for k, v in args.items() if k in params_spec and v not in ("", None)}

    missing = [p for p in required if p not in clean]
    if missing:
        errors.append(f"缺少必填参数：{', '.join(missing)}")

    # 枚举值校验
    for k, v in list(clean.items()):
        sch = _param_schema(k, params_spec[k])
        if sch.get("enum") and str(v) not in sch["enum"]:
            errors.append(
                f"参数 {k} 取值 '{v}' 不合法，允许：{'|'.join(sch['enum'])}"
            )
        elif sch.get("type") == "integer":
            try:
                clean[k] = int(v)
            except (TypeError, ValueError):
                errors.append(f"参数 {k} 需要整数，收到 '{v}'")

    return clean, errors
