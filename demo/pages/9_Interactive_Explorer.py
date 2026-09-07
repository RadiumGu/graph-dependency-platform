"""
9_Interactive_Explorer.py — 交互式依赖探索（B 档）。

## 为什么单独一页，而不是替换 3_Graph_Explorer

两者解决不同的事，调研里也是分开的两类工具：

| | 3_Graph_Explorer（A 档，pyvis） | 本页（B 档，Cytoscape） |
|---|---|---|
| 定位 | **静态分层总览** —— 一眼看清方向与层次 | **交互探索** —— 点着走 |
| 布局 | vis-network hierarchical | dagre（Sugiyama 分层，交叉更少） |
| 选中回传 Python | ❌ pyvis 单向 | ✅ 这是本页存在的理由 |
| 点节点展开邻居 | ❌ 做不到 | ✅ 内置 expand 动作 |
| 侧栏详情 | ❌ | ✅ |

pyvis 的硬天花板是**单向**：选中的节点拿不回 Python，所以「点节点 → 展开 /
出详情」这套成熟产品的标准模式在它上面根本做不了。本页用
`st-link-analysis`（目前唯一还在维护的 Cytoscape 封装，v0.4.0 / 2025-09）
补上这一段。

## 实现的交互模式（对应调研里 10 个产品的共同做法）

1. **搜索优先入口** —— 先选锚点，不渲染全图（Bloom / graph-explorer 都是空画布 + 搜索种子）
2. **渐进披露** —— 双击节点展开它的邻居，而不是一次铺开
3. **选中 → 侧栏详情** —— 节点属性 + 该节点依赖边的验证状态 + 可下钻查询
4. **面包屑** —— 记录展开路径，可回退到任一步
5. **高度数节点截断** —— 「显示更多 N 个」而不是一次拉进来几十个
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _common as C  # noqa: E402

import streamlit as st  # noqa: E402

C.page_setup("交互探索", icon="🧭")
C.sidebar()

try:
    from st_link_analysis import EdgeStyle, Event, NodeStyle, st_link_analysis
    HAVE_SLA = True

    # 抑制 st-link-analysis 0.4.0 的一条**无条件**弃用警告。
    #
    # 上游 styles.py 里写的是 `if labeled is not None: warn(...)`，
    # 而签名默认值是 `labeled: bool = False` —— `False is not None` 恒为真，
    # 所以每构造一个 EdgeStyle 就报一条，无论调用方传不传这个参数。
    # 实测：不传 / False / True 三种情形各报一条，caption 都不受影响。
    #
    # 这一页每次渲染要按关系类型构造 N 个 EdgeStyle，于是每次刷新往日志里
    # 灌 N 条噪音 —— 噪音会埋掉真信号，所以按**具体警告类**过滤，
    # 不是 `simplefilter("ignore")` 那种一刀切。
    # 上游修好或删掉这个参数之后，这个过滤自动变成空操作。
    import warnings  # noqa: E402

    try:
        from st_link_analysis.component.styles import (  # noqa: E402
            LinkAnalysisDeprecationWarning,
        )
        warnings.filterwarnings(
            "ignore", category=LinkAnalysisDeprecationWarning,
            message=r".*labeled.*deprecated.*")
    except Exception:  # noqa: BLE001
        pass       # 上游改了模块结构就算了，噪音不值得为它抛错
except Exception as _exc:  # noqa: BLE001
    HAVE_SLA = False
    _SLA_ERR = f"{type(_exc).__name__}: {_exc}"

st.title("🧭 交互探索")

if not HAVE_SLA:
    st.error(
        f"缺少 `st-link-analysis`（{_SLA_ERR}）。\n\n"
        "安装：`pip install st-link-analysis`\n\n"
        "这一页需要它来做「点节点 → 展开 / 出详情」——"
        "pyvis 是单向的，选中的节点拿不回 Python，那套交互在它上面做不了。",
        icon="📦",
    )
    C.page_link("pages/3_Graph_Explorer.py", "→ 先用分层总览页（不需要这个依赖）")
    st.stop()

st.markdown(
    "> 从一个服务出发，**双击节点展开它的邻居**，单击看详情。"
    "这是 Datadog / Neo4j Bloom / AWS graph-explorer / Kiali 的共同模式："
    "不渲染全图，让你点着走。"
)

DEP_EDGES = sorted(C.dependency_edge_labels())
STRUCT_EDGES = sorted(set((C.contract().get("edge_types") or {}).keys()) - set(DEP_EDGES))
online = C.neptune_online()

# ── 颜色：与 A 档保持一致的 7 组 Okabe–Ito ────────────────────────────────────
GROUPS = [
    ("业务能力", "#E69F00", {"BusinessCapability"}),
    ("入口与路由", "#56B4E9", {"LoadBalancer", "ListenerRule", "TargetGroup"}),
    ("应用与计算", "#0072B2", {
        "Microservice", "LambdaFunction", "StepFunction", "Deployment",
        "K8sService", "HPA", "Pod", "Namespace", "EKSCluster", "EC2Instance"}),
    ("数据存储", "#009E73", {
        "RDSCluster", "RDSInstance", "Database", "DynamoDBTable", "S3Bucket",
        "NeptuneCluster", "NeptuneInstance", "ECRRepository"}),
    ("消息", "#E5C100", {"SQSQueue", "SNSTopic"}),
    ("Agent / GenAI", "#CC79A7", {
        "AgentRuntime", "AgentTool", "AgentGateway", "AgentMemory",
        "KnowledgeBase", "Guardrail"}),
    ("基础设施与运维", "#8C8C8C", {
        "VPC", "Subnet", "SecurityGroup", "AvailabilityZone", "Region",
        "AWSServiceEndpoint", "Incident", "ChaosExperiment", "TopologyChange"}),
]


def group_name(label: str) -> str:
    for name, _c, members in GROUPS:
        if label in members:
            return name
    return "未归类"


GROUP_COLOR = {name: color for name, color, _m in GROUPS}
GROUP_COLOR["未归类"] = "#B39DDB"

# ── 会话状态：展开集合 + 面包屑 ───────────────────────────────────────────────
S_EXPANDED = "ie_expanded"     # 已展开过的节点名
S_CRUMB = "ie_crumbs"          # 面包屑
S_EDGES = "ie_edges"           # 累积的边
S_ANCHOR = "ie_anchor"

for k, v in ((S_EXPANDED, set()), (S_CRUMB, []), (S_EDGES, []), (S_ANCHOR, None)):
    st.session_state.setdefault(k, v)


@st.cache_data(ttl=60, show_spinner=False)
def neighbors(name: str, ets: tuple, limit: int) -> dict:
    """
    取一个节点的邻居（单跳，无向）。

    刻意用**迭代单跳**而不是变长路径：Neptune 对带谓词的变长路径 +
    relationships() 支持有限，A 档踩过 400 Bad Request。
    """
    if "'" in name:
        return {"results": []}
    ec = ", ".join(f"'{e}'" for e in ets)
    return C.gquery(
        f"MATCH (a)-[r]-(b) WHERE a.name = '{name}' AND type(r) IN [{ec}] "
        "RETURN coalesce(startNode(r).name, startNode(r).tool_key, startNode(r).arn) AS source, "
        "labels(startNode(r))[0] AS source_label, type(r) AS edge_type, "
        "coalesce(endNode(r).name, endNode(r).tool_key, endNode(r).arn) AS target, "
        "labels(endNode(r))[0] AS target_label, "
        "coalesce(r.verify_status,'') AS verify_status, coalesce(r.source,'') AS edge_source "
        f"LIMIT {int(limit)}"
    )


def neighbors_snapshot(name: str, ets: list, limit: int) -> list:
    snap = C.fixture("sample_topology")
    return [
        e for e in snap.get("edges", [])
        if e.get("edge_type") in ets
        and (e.get("source") == name or e.get("target") == name)
    ][:limit]


def expand(name: str, ets: list, limit: int) -> int:
    """把某个节点的邻居并进累积边集。返回新增边数。"""
    if online:
        res = neighbors(name, tuple(ets), limit)
        rows = res.get("results", []) if "error" not in res else []
        if "error" in res:
            st.warning(f"查询失败，改用离线快照：{res['error']}")
            rows = neighbors_snapshot(name, ets, limit)
    else:
        rows = neighbors_snapshot(name, ets, limit)

    have = {(e.get("source"), e.get("edge_type"), e.get("target"))
            for e in st.session_state[S_EDGES]}
    added = 0
    for r in rows:
        key = (r.get("source"), r.get("edge_type"), r.get("target"))
        if key not in have:
            st.session_state[S_EDGES].append(r)
            have.add(key)
            added += 1
    st.session_state[S_EXPANDED].add(name)
    return added


# ── 控制区 ────────────────────────────────────────────────────────────────────
svc = C.service_names()
if not svc:
    st.error("取不到服务清单（图谱不可达且无离线快照）。")
    st.stop()

c1, c2, c3 = st.columns([2, 3, 1])
anchor = c1.selectbox(
    "起点服务", svc,
    index=svc.index("petsite") if "petsite" in svc else 0,
    key="ie_anchor_pick",
)
edge_types = c2.multiselect(
    "关系类型", DEP_EDGES + STRUCT_EDGES, default=DEP_EDGES,
    help=f"默认只看 {len(DEP_EDGES)} 种依赖边；{len(STRUCT_EDGES)} 种结构边（包含/承载）需显式勾选",
)
per_expand = c3.number_input("每次展开上限", 3, 40, 12,
                             help="高度数节点一次拉几十个邻居会立刻变成一团")

if not edge_types:
    st.warning("请至少选择一种关系类型。")
    st.stop()

b1, b2, b3 = st.columns([1, 1, 3])
if b1.button("🎯 从这里开始", type="primary", width="stretch") or \
        st.session_state[S_ANCHOR] is None:
    if st.session_state[S_ANCHOR] != anchor or not st.session_state[S_EDGES]:
        st.session_state[S_EDGES] = []
        st.session_state[S_EXPANDED] = set()
        st.session_state[S_CRUMB] = [anchor]
        st.session_state[S_ANCHOR] = anchor
        expand(anchor, edge_types, per_expand)

if b2.button("🔄 重置", width="stretch"):
    st.session_state[S_EDGES] = []
    st.session_state[S_EXPANDED] = set()
    st.session_state[S_CRUMB] = [anchor]
    st.session_state[S_ANCHOR] = anchor
    expand(anchor, edge_types, per_expand)
    st.rerun()

C.mode_badge("live" if online else ("snapshot" if C.fixture("sample_topology") else "none"),
             "拓扑数据")

edges = st.session_state[S_EDGES]
if not edges:
    st.info("点「🎯 从这里开始」载入起点的邻居。")
    st.stop()

# ── 面包屑 ────────────────────────────────────────────────────────────────────
crumbs = st.session_state[S_CRUMB]
if len(crumbs) > 1:
    st.caption("展开路径（点任一步可回退到那时的状态）：")
    ccols = st.columns(min(len(crumbs), 8))
    for i, (col, name) in enumerate(zip(ccols, crumbs[-8:])):
        if col.button(f"{i + 1}. {name[:14]}", key=f"crumb_{i}_{name}",
                      width="stretch"):
            # 回退：只保留到该步为止展开过的节点
            keep = crumbs[: crumbs.index(name) + 1]
            st.session_state[S_CRUMB] = keep
            st.session_state[S_EDGES] = []
            st.session_state[S_EXPANDED] = set()
            for n in keep:
                expand(n, edge_types, per_expand)
            st.rerun()

# ── 组装 Cytoscape elements ───────────────────────────────────────────────────
node_map: dict = {}
for e in edges:
    for nm, lb in ((e.get("source"), e.get("source_label")),
                   (e.get("target"), e.get("target_label"))):
        if nm:
            node_map[nm] = lb

# 度数：用来给「显示更多」和节点大小提供依据
deg: dict = {}
for e in edges:
    deg[e.get("source")] = deg.get(e.get("source"), 0) + 1
    deg[e.get("target")] = deg.get(e.get("target"), 0) + 1

anchor_now = st.session_state[S_ANCHOR]
nodes_payload = []
for nm, lb in node_map.items():
    g = group_name(lb)
    expanded = nm in st.session_state[S_EXPANDED]
    nodes_payload.append({"data": {
        "id": nm,
        "label": g,                     # NodeStyle 按 label 匹配 → 用分组名着色
        "name": (("★ " if nm == anchor_now else "") + str(nm)[:26]),
        "type": lb,
        "degree": deg.get(nm, 0),
        "expanded": "已展开" if expanded else "双击展开",
    }})

VS_STYLE = {
    "confirmed": ("#2E7D32", "已确认"),
    "refuted": ("#C62828", "已证伪"),
    "inconclusive": ("#EF6C00", "未定"),
    "untested": ("#9E9E9E", "未验证"),
}
edges_payload = []
for i, e in enumerate(edges):
    vs = e.get("verify_status") or "untested"
    edges_payload.append({"data": {
        "id": f"e{i}",
        "source": e.get("source"),
        "target": e.get("target"),
        "label": vs if vs in VS_STYLE else "untested",   # 按验证状态着色
        "rel": e.get("edge_type"),
        "src_of_edge": e.get("edge_source") or "—",
    }})

elements = {"nodes": nodes_payload, "edges": edges_payload}

present_groups = sorted({group_name(lb) for lb in node_map.values()})
node_styles = [NodeStyle(g, GROUP_COLOR.get(g, "#B39DDB"), "name") for g in present_groups]
present_vs = sorted({(e["data"]["label"]) for e in edges_payload})
# 不要传 `labeled=` —— 它在这里是**死参数**。
# 上游 styles.py 的逻辑是 `if labeled and not caption: self.caption = "label"`，
# 而我们已经传了 caption="rel"，所以 labeled 不会改变任何行为。
#
# ⚠️ 而且它的弃用警告是**无条件触发**的：条件写成 `if labeled is not None`，
# 但签名默认值是 `labeled: bool = False` —— `False is not None` 恒为真。
# 实测三种情形（不传 / False / True）都各报一条警告，caption 都是 'rel'。
# 所以警告不是我们调用错了，删掉参数也消不掉；见下方 filterwarnings。
edge_styles = [
    EdgeStyle(vs, color=VS_STYLE.get(vs, ("#9E9E9E", ""))[0], caption="rel",
              directed=True, curve_style="bezier")
    for vs in present_vs
]

# ── 指标 ──────────────────────────────────────────────────────────────────────
over = len(node_map) > 50
m = st.columns(5)
m[0].metric("节点", len(node_map),
            help=("⚠️ **超预算** —— 实测节点超过约 50 个，判断准确率掉到一半以下。"
                  "收起几个已展开的节点。"
                  if over else "在认知预算内（约 50 个节点以下）。"))
m[1].metric("关系", len(edges))
m[2].metric("已展开", len(st.session_state[S_EXPANDED]))
m[3].metric("可继续展开", len([n for n in node_map if n not in st.session_state[S_EXPANDED]]))
vs_counts: dict = {}
for e in edges:
    k = e.get("verify_status") or "untested"
    vs_counts[k] = vs_counts.get(k, 0) + 1
m[4].metric("已验证关系", vs_counts.get("confirmed", 0) + vs_counts.get("refuted", 0),
            help=f"当前视图里共 {len(edges)} 条关系，"
                 "其中判定为 confirmed 或 refuted 的算「已验证」—— "
                 "即真正做过主动干预并得出结论的。"
                 "inconclusive 与 untested 都不算。")

if over:
    st.warning(
        f"{len(node_map)} 个节点已超过约 50 的可读上限（IEEE TVCG 2020）。"
        "建议点面包屑回退，或用「重置」重新开始。", icon="🧠",
    )

# ── 图 ────────────────────────────────────────────────────────────────────────
layout_name = st.radio(
    "布局", ["dagre（分层，推荐）", "fcose（力导向）", "breadthfirst（广度树）", "cola"],
    horizontal=True, index=0,
)
layout_key = {"dagre（分层，推荐）": "dagre", "fcose（力导向）": "fcose",
              "breadthfirst（广度树）": "breadthfirst", "cola": "cola"}[layout_name]

result = st_link_analysis(
    elements=elements,
    layout=layout_key,
    node_styles=node_styles,
    edge_styles=edge_styles,
    height=620,
    key="ie_graph",
    node_actions=["expand", "remove"],
    events=[Event("node_click", "click tap", "node")],
)

st.caption(
    "**单击**节点看详情（下方侧栏）　**双击 / 用节点上的 expand 动作**展开它的邻居。"
    "边颜色 = 故障注入验证状态：🟢 已确认　🔴 已证伪　🟠 未定　⚫ 未验证。"
)

# ── 选中回传：这是 pyvis 做不到的部分 ─────────────────────────────────────────
selected = None
if isinstance(result, dict):
    # 组件返回形态随版本略有差异，这里宽松解析，取不到就当无选中
    for key in ("data", "selection", "node_click"):
        blob = result.get(key)
        if isinstance(blob, dict):
            for k2 in ("id", "node", "target_id"):
                if blob.get(k2):
                    selected = blob[k2] if isinstance(blob[k2], str) else None
                    break
        elif isinstance(blob, list) and blob:
            first = blob[0]
            if isinstance(first, dict) and first.get("id"):
                selected = first["id"]
        if selected:
            break
    if not selected and isinstance(result.get("action"), str) and result.get("node_id"):
        selected = result["node_id"]

st.markdown("---")
detail_col, action_col = st.columns([3, 2])

with detail_col:
    st.subheader("节点详情")
    target = selected or anchor_now
    if selected:
        st.caption(f"来自图上的选中事件：`{selected}`")
    else:
        st.caption(f"（未捕获到选中事件，默认显示起点 `{anchor_now}`）")

    st.markdown(f"**`{target}`**　类型 `{node_map.get(target, '?')}`　"
                f"分组 {group_name(node_map.get(target, ''))}　度数 {deg.get(target, 0)}")

    inc = [e for e in edges if e.get("target") == target]
    out = [e for e in edges if e.get("source") == target]
    t1, t2 = st.tabs([f"⬅️ 被依赖 / 被调用（{len(inc)}）", f"➡️ 依赖 / 调用（{len(out)}）"])
    with t1:
        st.dataframe(C.df([
            {"来源": e["source"], "类型": e.get("source_label"), "关系": e["edge_type"],
             "验证": e.get("verify_status") or "untested"} for e in inc
        ]), width="stretch", hide_index=True)
    with t2:
        st.dataframe(C.df([
            {"目标": e["target"], "类型": e.get("target_label"), "关系": e["edge_type"],
             "验证": e.get("verify_status") or "untested"} for e in out
        ]), width="stretch", hide_index=True)

with action_col:
    st.subheader("下一步")
    if target not in st.session_state[S_EXPANDED]:
        if st.button(f"➕ 展开 `{target}` 的邻居", type="primary", width="stretch"):
            n = expand(target, edge_types, per_expand)
            if target not in st.session_state[S_CRUMB]:
                st.session_state[S_CRUMB].append(target)
            st.toast(f"新增 {n} 条关系")
            st.rerun()
    else:
        st.success(f"`{target}` 已展开", icon="✅")
        if st.button(f"🔎 再多拉 {per_expand} 条", width="stretch"):
            n = expand(target, edge_types, min(per_expand * 3, 60))
            st.toast(f"新增 {n} 条关系")
            st.rerun()

    st.markdown("**下钻查询**")
    st.caption("拿这个节点去跑预置查询——确定性 Cypher，不经过 AI。")
    for qname, why in (
        ("q22_edge_verification_verdicts", "这个服务的依赖边验证判定"),
        ("q1_blast_radius", "它挂了会影响什么"),
        ("q3_upstream_deps", "谁依赖它"),
    ):
        st.caption(f"　`{qname}` — {why}")
    C.page_link("pages/2_Query_Catalog.py", "→ 去查询库执行")

# ── 图例 ──────────────────────────────────────────────────────────────────────
with st.expander("图例与设计说明"):
    st.markdown("**节点颜色 = 分组**（7 组，Okabe–Ito 色盲安全）")
    for g in present_groups:
        types = sorted({lb for lb in node_map.values() if group_name(lb) == g})
        st.markdown(
            f"<span style='display:inline-block;width:13px;height:13px;border-radius:3px;"
            f"background:{GROUP_COLOR.get(g)};margin-right:8px'></span>**{g}**　"
            + "　".join(f"`{t}`" for t in types),
            unsafe_allow_html=True,
        )
    st.markdown("---")
    st.markdown(
        "**为什么颜色只到 7 组**：感知研究给出的可区分上限约 7 种颜色、5 种形状"
        "（arXiv:2103.06084），而契约里有 39 种节点类型——"
        "一类一色在首屏是不可能读出来的。具体类型放在节点详情里。\n\n"
        "**为什么边按验证状态着色而不是按关系类型**：关系类型有 29 种，同样超上限；"
        "而「这条依赖到底成立吗」才是本平台的核心信息。关系类型显示在边标签上。"
    )
