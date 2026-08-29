#!/usr/bin/env python3
"""
query_catalog.py — 固化查询库的统一目录（普通 Python 模块，非 MCP 端点）

## 它解决什么

查询库此前割裂在两个模块：rca 持 Q1–Q11 / Q17–Q19（14 条），
dr-plan-generator 持 Q12–Q16（6 条）。后者的目录名带连字符，**不能当包
导入**，调用方必须自己写 importlib 按文件路径加载——这是真实摩擦。
本模块把两边统一到一个入口，并为每条查询声明参数契约。

## 它不做什么：不再提供 MCP 端点

前身是 rca/neptune/graph_mcp_server.py（410 行，纯标准库手写 JSON-RPC 2.0）。
删除的依据是实测，不是偏好：

1. **零消费方。** 全仓库只有两处引用——它自己的 rca/.mcp.json，以及一条
   断言「注册表里有我放进去的条目」的循环测试。没有任何模块 import 它，
   也从未被任何 agent 注册过。这正是本仓库反复出现的「写了但没人读」
   （active/last_seen、causal_weight、resilience 分数都是同一模式）。
2. **官方 server 覆盖了传输层。** awslabs.amazon-neptune-mcp-server 提供
   get_graph_status / get_graph_schema / run_opencypher_query /
   run_gremlin_query，本机实测全通。
3. **只读护栏应由 IAM 承担。** query_guard 是客户端校验，绕过该进程直连
   Neptune 即失效；只给 neptune-db:ReadDataViaQuery、不给 WriteDataViaQuery
   与 DeleteDataViaQuery，由 Neptune 服务端拒绝写操作，权威且绕不过。
   （依据：Neptune 授予的是一条查询「可能执行的动作」的并集，所以对已有
   属性做 SET 都需要 Delete 权限。）
4. **官方的 schema 更优。** 它从活图谱实测反推；原 get_schema 只是回显
   profiles/petsite.yaml 的声明文本。两者的差异本身是漂移信号，
   由 test_24 在 CI 里对账。
5. **手写的传输层在目标形态里用不上。** 对外服务的既定方向是把 MCP server
   放到 **Bedrock AgentCore** 上（见下）。AgentCore Gateway 自己承担 MCP
   协议与托管鉴权，需要下游提供的是**一个可调用的分发面**，不是又一份
   stdio JSON-RPC 实现——那 410 行无论如何都会被丢掉。

## 对外服务的目标形态（AgentCore）

未来需要对外提供服务时，路径是 AgentCore，而不是把本模块重新包成 stdio：

    VPC 外的异构 agent
          ↓ MCP over HTTP + 托管鉴权
    ┌──────────────────────────────┐
    │ Bedrock AgentCore Gateway    │  ← 提供 MCP 协议层与鉴权
    └──────────────────────────────┘
          ↓ target（Lambda）
    ┌──────────────────────────────┐
    │ query_catalog.run_query()    │  ← 本模块：参数契约 + 分发
    │   ↓                          │
    │ 确定性查询层 rca/neptune/     │
    └──────────────────────────────┘
          ↓ SigV4（VPC 内）
              Neptune

因此本模块刻意只暴露 **describe() / get_query() / run_query()** 三个纯 Python
入口：Lambda handler 把事件参数直接转成 run_query(name, **params) 即可，
无需再实现任何协议。这也顺带补上了原端点没解决的缺口——原端点仍需
「在 VPC 内 + 调用方自备 SigV4」，VPC 外的异构 agent 接不上。

## 谁该用本模块

rca / chaos / dr-plan 三个模块**直接调用确定性查询层**，本模块只是它们的
统一索引，不插在调用路径上：
  - LLM 在事故热路径上带来延迟与不可复现（NL→Cypher 是两次 Bedrock 往返）
  - 写路径有 schema 契约，不能 LLM 中介
  - dr-plan 的图算法（Kahn / DFS / 最长路径 DP）需要全量精确结果集

外部 agent 走官方 MCP server + get_graph_schema 自行写 Cypher；
本模块的 describe() 输出可作为「有哪些问题是已被固化回答的」清单交给它。

## 用法

    from neptune import query_catalog as qc

    print(qc.describe())                      # 列出全部 20 条及参数
    rows = qc.run_query('q3_upstream_deps',    # 带参数校验的分发
                        failed_service='petsite', kind='live')
    fn = qc.get_query('q16_single_point_of_failure')   # 拿到原函数自己调

    python3 query_catalog.py --selftest       # 不连 Neptune，自检目录完整性
"""

import importlib.util
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_RCA_DIR = os.path.dirname(_HERE)                 # .../rca
_PROJECT_ROOT = os.path.dirname(_RCA_DIR)         # 仓库根

# 刻意不在 import 时改 sys.path：本模块位于 rca/neptune/ 下，能被 import
# 就说明 rca 已在路径上。dr-plan 的路径只在真正加载它时才临时加入，
# 避免把额外目录塞进全局 sys.path（那是同名包遮蔽缺陷的来源）。

_mods = {}


def load_module(kind: str):
    """按需导入查询模块并缓存。kind: 'queries'（rca）或 'dr'（dr-plan）。"""
    if kind in _mods:
        return _mods[kind]
    if kind == 'queries':
        from neptune import neptune_queries as m
    elif kind == 'dr':
        # dr-plan-generator 目录名带连字符，不能当包导入，按文件路径加载
        path = os.path.join(_PROJECT_ROOT, 'dr-plan-generator', 'graph', 'queries.py')
        dr_dir = os.path.join(_PROJECT_ROOT, 'dr-plan-generator')
        if dr_dir not in sys.path:
            sys.path.insert(0, dr_dir)
        spec = importlib.util.spec_from_file_location('dr_graph_queries', path)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
    else:
        raise KeyError(f"未知模块类别: {kind}（可用: queries, dr）")
    _mods[kind] = m
    return m


# ─── 查询目录 ──────────────────────────────────────────────────────────────
# 每项声明所属模块、函数名、参数与用途。mod='queries' 来自 rca，
# mod='dr' 来自 dr-plan-generator。

QUERY_CATALOG = {
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
    "q19_topology_changes": {
        "mod": "queries", "fn": "q19_topology_changes",
        "desc": ("拓扑变更事件（依赖出现/消失）。CloudTrail 看不见这两类变化 —— "
                 "依赖消失是「流量缺席」而非 API 调用"),
        "params": {"service_name": "str，可选（省略=全图）",
                   "since_seconds": "int，默认 86400",
                   "limit": "int，默认 20"},
        "required": [],
    },
    "q20_dependency_verification": {
        "mod": "queries", "fn": "q20_dependency_verification",
        "desc": ("依赖边的运行时验证状态：找出「未被观测」「只被单源验证」"
                 "「验证已过期」的边。漂移判定有 DNS 与 X-Ray 两个观测源，"
                 "本查询是 verified_by 的读取方 —— X-Ray 若静默失效，"
                 "判定会悄悄退回 DNS-only 而结果看起来完全正常"),
        "params": {"service_name": "str，可选（省略=全图）",
                   "stale_after_seconds": "int，默认 3600",
                   "only_problematic": "bool，默认 True",
                   "limit": "int，默认 50"},
        "required": [],
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


def describe(kind: str = None) -> str:
    """人类/agent 可读的查询清单。kind 可选 'queries' 或 'dr' 做过滤。"""
    lines = ["图谱固化查询库（确定性，参数契约见下）：", ""]
    for name, spec in QUERY_CATALOG.items():
        if kind and spec["mod"] != kind:
            continue
        src = "rca" if spec["mod"] == "queries" else "dr-plan"
        ps = ", ".join(f"{k}({v})" for k, v in spec["params"].items()) or "无参数"
        lines.append(f"- {name}  [{src}]")
        lines.append(f"    {spec['desc']}")
        lines.append(f"    参数: {ps}")
    return "\n".join(lines)


def get_query(name: str):
    """返回查询的原函数，调用方自行传参。"""
    spec = QUERY_CATALOG.get(name)
    if not spec:
        raise ValueError(
            f"未知查询 '{name}'。可用: {', '.join(sorted(QUERY_CATALOG))}"
        )
    return getattr(load_module(spec["mod"]), spec["fn"])


def run_query(name: str, **params):
    """带必填参数校验的分发。返回查询原始结果（通常是 list[dict]）。"""
    spec = QUERY_CATALOG.get(name)
    if not spec:
        raise ValueError(
            f"未知查询 '{name}'。可用: {', '.join(sorted(QUERY_CATALOG))}"
        )
    missing = [r for r in spec["required"] if r not in params]
    if missing:
        raise ValueError(f"{name} 缺少必填参数: {missing}")
    return get_query(name)(**params)


def selftest() -> int:
    """不连 Neptune，自检目录完整性与参数校验。

    独立运行时自行把 rca/ 与仓库根加入 sys.path：`shared` 包在仓库根，
    而 neptune_queries → neptune_client → `from shared import get_region`。
    这个安排**只在这里做**，不在 import 时做 —— 库模块改全局 sys.path 正是
    同名包遮蔽缺陷的来源（conftest 曾把 etl_aws 部署包塞进全局路径，
    导致整个会话跑在 vendored 副本上）。真实调用方无需这一步：Lambda 部署包
    把 shared/ 放在根目录，测试则由 conftest 安排。
    """
    for p in (_RCA_DIR, _PROJECT_ROOT):
        if p not in sys.path:
            sys.path.insert(0, p)
            print(f"  [selftest] 已临时加入 sys.path: {p}")

    print(f"查询目录自检 — 共 {len(QUERY_CATALOG)} 条 "
          f"(rca {sum(1 for s in QUERY_CATALOG.values() if s['mod'] == 'queries')} + "
          f"dr-plan {sum(1 for s in QUERY_CATALOG.values() if s['mod'] == 'dr')})")
    ok = True

    listing = describe()
    for must in ("q1_blast_radius", "q16_single_point_of_failure",
                 "q19_topology_changes"):
        if must not in listing:
            print(f"  describe 缺少 {must}: ❌")
            ok = False
    if ok:
        print("  describe: ✅")

    for bad in ("q999", ""):
        try:
            run_query(bad)
            print(f"  未知查询校验({bad!r}): ❌ 未拒绝")
            ok = False
        except ValueError:
            print(f"  未知查询校验({bad!r}): ✅ 已拒绝")

    try:
        run_query("q1_blast_radius")
        print("  必填参数校验: ❌ 未拒绝")
        ok = False
    except ValueError:
        print("  必填参数校验: ✅ 已拒绝")

    # 每条目录项的目标函数都必须真实存在（不连 Neptune，只做 getattr）
    broken = []
    for name, spec in QUERY_CATALOG.items():
        try:
            if not callable(getattr(load_module(spec["mod"]), spec["fn"], None)):
                broken.append(name)
        except Exception as e:            # 导入失败（缺 boto3 等）单独报告
            print(f"  {name} 所属模块 {spec['mod']} 导入失败: {type(e).__name__}: {e}")
            broken.append(name)
    if broken:
        print(f"  目标函数缺失: ❌ {broken}")
        ok = False
    else:
        print("  20 条目标函数均存在: ✅")

    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    print(describe())
