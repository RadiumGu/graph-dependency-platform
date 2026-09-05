"""
5_Agent_Dependencies.py — GenAI Agent 的依赖也进同一张图。

背景：2026-09-04 契约扩展到 agent 域（节点 33→39、边 26→29、数据源 11→12），
新增 etl_agentcore 从两条独立链路写入：
  ① 控制平面 list API → 资源节点（失败表现为「缺节点」）
  ② aws/spans 结构化日志 → 调用边（失败表现为「有节点没依赖」）

这一页刻意讲清「孤岛问题」：agent 子图与主图之间只有一条桥——
AgentTool -[DependsOn]-> Microservice / LambdaFunction。
桥断了，两边的节点和边**各自都合法**，没有任何断言会失败。
"""
import os
import sys

# 页面被单独执行时 demo/ 不在 sys.path 上，显式补上。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _common as C  # noqa: E402

import streamlit as st  # noqa: E402

C.page_setup("Agent 依赖", icon="🤖")
C.sidebar()

AGENT_LABELS = [
    "AgentRuntime", "AgentTool", "AgentGateway",
    "AgentMemory", "KnowledgeBase", "Guardrail",
]

st.title("🤖 Agent 依赖")
st.markdown(
    "> LLM 在**运行时**决定调用哪个工具、委派给哪个子 agent。"
    "这类依赖没法靠静态配置推出来，只能观测——但观测到之后，"
    "它和微服务依赖进的是**同一张图、同一套契约、同一套验证机制**。"
)

# ── 数据 ──────────────────────────────────────────────────────────────────────
online = C.neptune_online()
mode = "none"
nodes: list = []
edges: list = []

if online:
    lst = ", ".join(f"'{x}'" for x in AGENT_LABELS)
    rn = C.gquery(
        f"MATCH (n) WHERE labels(n)[0] IN [{lst}] "
        "RETURN labels(n)[0] AS label, coalesce(n.name, n.tool_key, n.arn) AS name, "
        "n.arn AS arn ORDER BY label, name LIMIT 100"
    )
    re_ = C.gquery(
        f"MATCH (a)-[e]->(b) WHERE labels(a)[0] IN [{lst}] OR labels(b)[0] IN [{lst}] "
        "RETURN labels(a)[0] AS source_label, coalesce(a.name,a.tool_key,a.arn) AS source, "
        "type(e) AS edge_type, labels(b)[0] AS target_label, "
        "coalesce(b.name,b.tool_key,b.arn) AS target, "
        "coalesce(e.verify_status,'untested') AS verify_status, e.source AS edge_source "
        "ORDER BY edge_type LIMIT 200"
    )
    if "error" not in rn and "error" not in re_:
        nodes, edges, mode = rn["results"], re_["results"], "live"

if mode != "live":
    snap = C.fixture("agent_graph")
    if snap:
        nodes, edges, mode = snap.get("nodes", []), snap.get("edges", []), "snapshot"

C.mode_badge(mode, "Agent 子图")

if not nodes and not edges:
    st.warning("无 Agent 域数据。")
    st.stop()

# ── 概览 ──────────────────────────────────────────────────────────────────────
gc = C.contract()
agent_node_types = [n for n in AGENT_LABELS if n in (gc.get("node_types") or {})]
agent_edge_types = sorted({e["edge_type"] for e in edges})

m = st.columns(4)
m[0].metric("Agent 域节点", len(nodes), f"{len(agent_node_types)} 种类型")
m[1].metric("相关边", len(edges), f"{len(agent_edge_types)} 种类型")
m[2].metric("契约声明节点类型", C.schema_counts()["node_types"], "含 agent 域 6 种")
m[3].metric("数据源", "agentcore-etl", "第 12 个合法源")

# ── 节点分布 ──────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("Agent 域实体")

by_label: dict = {}
for n in nodes:
    by_label.setdefault(n["label"], []).append(n)

LABEL_DESC = {
    "AgentRuntime": "AgentCore 上运行的 agent 实例（身份键 `arn`）",
    "AgentTool": "agent 可调用的工具（身份键 `tool_key`，即 `<owner_arn>#<tool_name>` 组合键）",
    "AgentGateway": "工具网关，把工具暴露给 agent（身份键 `arn`）",
    "AgentMemory": "agent 的记忆存储（身份键 `arn`）",
    "KnowledgeBase": "被检索的知识库（身份键 `arn`）",
    "Guardrail": "护栏，约束 agent 行为（身份键 `arn`）",
}

cols = st.columns(3)
for i, label in enumerate(sorted(by_label, key=lambda x: -len(by_label[x]))):
    items = by_label[label]
    with cols[i % 3]:
        with st.container(border=True):
            st.markdown(f"**{label}** · {len(items)}")
            st.caption(LABEL_DESC.get(label, ""))
            for it in items[:6]:
                st.caption(f"　`{it.get('name')}`")
            if len(items) > 6:
                st.caption(f"　… 还有 {len(items) - 6} 个")

# ── 孤岛问题与那条唯一的桥 ────────────────────────────────────────────────────
st.markdown("---")
st.subheader("🌉 孤岛问题：只有一条桥连回主图")

bridge = [
    e for e in edges
    if e.get("edge_type") == "DependsOn"
    and e.get("source_label") == "AgentTool"
    and e.get("target_label") in ("Microservice", "LambdaFunction")
]

st.error(
    "**这是本次扩展里最容易静默失败的地方。** agent 子图与 PetSite 主图之间，"
    "唯一的连接是 `AgentTool -[DependsOn]-> Microservice / LambdaFunction`。"
    "这条桥断掉时，agent 侧的节点和边**各自都完全合法**——"
    "没有任何 schema 断言、没有任何契约门禁会失败，图看起来只是「agent 没有依赖」。",
    icon="🌉",
)

b1, b2 = st.columns([2, 3])
b1.metric("当前桥接边数量", len(bridge))
if bridge:
    b2.success("桥接正常——agent 子图已连回主图", icon="✅")
    st.dataframe(
        C.df([
            {
                "工具": e.get("source"),
                "边": e.get("edge_type"),
                "后端": e.get("target"),
                "后端类型": e.get("target_label"),
                "数据源": e.get("edge_source") or "—",
            }
            for e in bridge
        ]),
        width="stretch", hide_index=True,
    )
    st.caption(
        "这些边由 `etl_agentcore` 的工具→后端映射表建立"
        "（如 `search_available_pets → petsearch`、`list_adoptions → petlistadoptions`、"
        "`complete_adoption → payforadoption`）。这张映射表就是整座桥。"
    )
else:
    b2.error("桥接缺失——agent 子图当前是孤岛", icon="🚨")

# ── 边明细 ────────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("Agent 依赖边")

EDGE_DESC = {
    "Delegates": "agent 委派给另一个 agent（编排器 → 子 agent）。判据取 `execute_tool` 的工具名，"
                 "**不是** `peer_agent` 字段——后者存的是框架名。",
    "InvokesTool": "agent 调用工具。刻意**没有**复用已有的 `Invokes`——那条是结构边（`dependency: false`），"
                   "而工具调用是真实的依赖，必须能参与验证。",
    "Retrieves": "agent 检索知识库。注意它的失败形态是「成功返回但结果为空」，不是错误率上升。",
    "RoutesTo": "网关把请求路由到工具（结构边）。",
    "ProtectsAccess": "护栏约束 agent（结构边）。",
    "AccessesData": "agent 访问记忆或 AWS 服务端点。",
    "DependsOn": "工具依赖后端服务——**连回主图的那条桥**。",
}

dep_labels = set(C.dependency_edge_labels())
et_pick = st.multiselect(
    "筛选边类型", agent_edge_types, default=agent_edge_types, key="agent_et"
)
shown = [e for e in edges if e["edge_type"] in et_pick]

for et in sorted({e["edge_type"] for e in shown}):
    group = [e for e in shown if e["edge_type"] == et]
    is_dep = et in dep_labels
    with st.container(border=True):
        head = st.columns([2, 1, 1])
        head[0].markdown(f"#### `{et}` · {len(group)} 条")
        head[1].markdown("**依赖边** ✅" if is_dep else "结构边")
        vs = {}
        for e in group:
            vs[e.get("verify_status", "untested")] = vs.get(e.get("verify_status", "untested"), 0) + 1
        if is_dep:
            head[2].caption(
                "验证状态　" + "　".join(
                    f"{C.STATUS_META.get(k, ('', k, ''))[0]}{v}" for k, v in sorted(vs.items())
                )
            )
        if EDGE_DESC.get(et):
            st.caption(EDGE_DESC[et])
        st.dataframe(
            C.df([
                {
                    "源": e.get("source"),
                    "源类型": e.get("source_label"),
                    "目标": e.get("target"),
                    "目标类型": e.get("target_label"),
                    **({"验证": e.get("verify_status")} if is_dep else {}),
                    "数据源": e.get("edge_source") or "—",
                }
                for e in group
            ]),
            width="stretch", hide_index=True,
        )

# ── 两条采集链路 ──────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("为什么 etl_agentcore 刻意分成两条链路")

l1, l2 = st.columns(2)
with l1:
    with st.container(border=True):
        st.markdown("#### ① 控制平面 → 资源节点")
        st.caption(
            "走 `bedrock-agentcore-control` / `bedrock` / `bedrock-agent` 的 list 与 get API："
            "`list_agent_runtimes`、`list_gateways`+`get_gateway`、`list_memories`、"
            "`list_gateway_targets`、`list_guardrails`、`list_knowledge_bases`。"
        )
        st.error("**失败形态：缺节点**", icon="🚫")
with l2:
    with st.container(border=True):
        st.markdown("#### ② aws/spans 日志 → 调用边")
        st.caption(
            "走 CloudWatch Logs Insights 查 `aws/spans` 日志组（6 小时回看窗口）。"
            "**硬前置**：必须开启 CloudWatch Transaction Search，"
            "否则该日志组根本不存在。"
        )
        st.error("**失败形态：有节点、没依赖**", icon="🔗")

st.info(
    "两条链路的失败表现完全不同，所以刻意不合并——"
    "合并之后「节点缺了」和「边缺了」会呈现成同一种症状，无法定位。",
    icon="🔀",
)

# ── Strands 实时元数据入口 ────────────────────────────────────────────────────
st.markdown("---")
st.subheader("想看 agent 实际跑起来的样子？")
st.caption(
    "自然语言查询页会展示 Strands 引擎每次调用的真实元数据——"
    "token 用量（含缓存读写）、ReAct 循环次数、逐步的工具调用链。"
)
C.page_link("pages/4_Smart_Query.py", "→ 打开自然语言查询")
