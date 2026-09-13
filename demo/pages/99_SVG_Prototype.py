"""99_SVG_Prototype.py —— dagre + 自绘 SVG 的原型（不替换现有页面）。

单独一页，为了能和 3_Graph_Explorer（pyvis）、9_Interactive_Explorer
（st-link-analysis）并排对比，而不是先拆掉能用的东西。

要验证的三件事，对应现有两页各自的短板：
1. **节点名一定画得出来** —— 9_Interactive_Explorer 的标签被组件硬编码的
   min-zoomed-font-size 整体隐藏，自绘之后字号由 graph.css 决定，没有阈值。
2. **有层次** —— dagre 分层，横向流向（左=起点，右=它依赖的）。
3. **点击回传 Python 且不经 iframe** —— Components v2 的 setStateValue /
   setTriggerValue，免 npm 构建。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _common as C  # noqa: E402

import streamlit as st  # noqa: E402

from components import graph_svg  # noqa: E402

C.page_setup("SVG 原型", icon="🧪")
C.sidebar()

st.title("🧪 SVG 原型")
st.caption(
    "dagre 分层 + 自绘 SVG，跑在 Streamlit Components v2（免 npm 构建、双向）。"
    "与 **分层总览**（pyvis）和 **交互探索**（Cytoscape 封装）并排对比用，不替换它们。"
)

# ── 同一套分组配色：与 9_Interactive_Explorer 保持一致，便于对比 ──────────────
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


def group_of(label: str) -> tuple[str, str]:
    for name, color, members in GROUPS:
        if label in members:
            return name, color
    return "未归类", "#B39DDB"


# 验证状态 → 边色，与既有两页同一套语义
VS_COLOR = {
    "confirmed": ("#2E7D32", "已确认"),
    "refuted": ("#C62828", "已证伪"),
    "inconclusive": ("#EF6C00", "未定"),
    "untested": ("#9E9E9E", "未验证"),
}

DEP_EDGES = sorted(C.dependency_edge_labels())
online = C.neptune_online()


@st.cache_data(ttl=60, show_spinner=False)
def _neighbors_live(name: str, ets: tuple, limit: int) -> dict:
    """单跳邻居（无向）。查询照抄 9_Interactive_Explorer：Neptune 对带谓词的
    变长路径 + relationships() 支持有限，A 档踩过 400，所以是**迭代单跳**。"""
    if "'" in name:
        return {"results": []}
    ec = ", ".join(f"'{e}'" for e in ets)
    return C.gquery(
        f"MATCH (a)-[r]-(b) WHERE a.name = '{name}' AND type(r) IN [{ec}] "
        "RETURN coalesce(startNode(r).name, startNode(r).tool_key, startNode(r).arn) AS source, "
        "labels(startNode(r))[0] AS source_label, type(r) AS edge_type, "
        "coalesce(endNode(r).name, endNode(r).tool_key, endNode(r).arn) AS target, "
        "labels(endNode(r))[0] AS target_label, r.verify_status AS verify_status "
        f"LIMIT {int(limit)}"
    )


def _neighbors_snapshot(name: str, ets: list, limit: int) -> list:
    snap = C.fixture("sample_topology")
    lab = {n.get("name"): n.get("label") for n in snap.get("nodes", [])}
    rows = [
        dict(e, source_label=lab.get(e.get("source"), ""),
             target_label=lab.get(e.get("target"), ""))
        for e in snap.get("edges", [])
        if e.get("edge_type") in ets
        and (e.get("source") == name or e.get("target") == name)
    ]
    return rows[:limit]


def neighbors_of(name: str, ets: tuple, limit: int) -> list:
    """在线走 Neptune，取不到就退到离线快照 —— 与既有两页同一策略。"""
    if online:
        res = _neighbors_live(name, ets, limit)
        if "error" not in res:
            return res.get("results", [])
    return _neighbors_snapshot(name, list(ets), limit)


# ── 只沿**一个方向**走 ────────────────────────────────────────────────────────
#
# 上面的查询是无向的（`(a)-[r]-(b)`，与既有两页一致），BFS 时如果两个方向都收，
# 锚点就同时有上游和下游，dagre 会把它排到中间层 —— 用户选了 petsite，开局却
# 看到图的中段，找不到自己选的那个服务。试过在前端按锚点坐标 scrollLeft 对准，
# 但那是在补救一个本不该出现的布局：把锚点排在中间本身就不是我们想要的图。
#
# 改成按方向筛边：downstream 只留 source==当前节点的边，于是锚点是**唯一的源**，
# dagre 必然把它放在第 0 层、也就是 rankdir=LR 下的最左端。方向由用户选，
# 因为「它依赖谁」和「谁依赖它」是两个不同的问题（爆炸半径 vs 根因）。
def directed_rows(name: str, ets: tuple, limit: int, downstream: bool) -> list:
    rows = neighbors_of(name, ets, limit)
    key = "source" if downstream else "target"
    return [r for r in rows if r.get(key) == name]


c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
with c1:
    services = C.service_names()
    if not services:
        st.error("取不到服务清单（图谱不可达且无离线快照）。")
        st.stop()
    anchor = st.selectbox("起点服务", services,
                          index=services.index("petsite") if "petsite" in services else 0)
with c2:
    hops = st.number_input("跳数", min_value=1, max_value=3, value=2,
                           help="现有两页默认 1 跳，只有两层；层次要靠跳数，不靠渲染器。")
with c3:
    limit = st.number_input("每层上限", min_value=4, max_value=40, value=14)
with c4:
    direction = st.radio("方向", ["它依赖谁", "谁依赖它"], index=0,
                         help="两个不同的问题：前者是爆炸半径，后者是根因方向。"
                              "沿单一方向走，锚点才会落在最左端。")
downstream = direction == "它依赖谁"

# ── 手动展开的节点集合 ───────────────────────────────────────────────────────
#
# 双击一个节点 = 把它加进这个集合，取数时对集合里的每个节点**额外再走一跳**。
# 这才是「渐进披露」：先看锚点附近，顺着依赖一步步走下去，而不是一次铺开
# 所有跳数。10 个成熟依赖图产品的共同做法都是锚点 + 有界邻域 + 按需展开。
#
# 换锚点 / 换方向 / 换跳数时必须清空：那三个参数一变，图就是另一张图了，
# 留着上一张图的展开集合会把无关节点带进来，而用户看不出它们为什么在那儿。
S_EXPANDED = "_svgproto_expanded"
S_SHAPE = "_svgproto_shape"
shape = (anchor, downstream, int(hops))
if st.session_state.get(S_SHAPE) != shape:
    st.session_state[S_SHAPE] = shape
    st.session_state[S_EXPANDED] = set()
expanded: set = st.session_state.setdefault(S_EXPANDED, set())

# ── 取数：锚点 BFS + 展开集合各自再走一跳 ────────────────────────────────────
seen_nodes: dict[str, str] = {}
seen_edges: list[dict] = []
_edge_keys: set = set()


def absorb(rows: list) -> list:
    """把一批边并进累积集合，返回这批边通向的下一层节点。"""
    out = []
    for r in rows:
        s, t = r.get("source"), r.get("target")
        if not s or not t:
            continue
        seen_nodes.setdefault(s, r.get("source_label") or "")
        seen_nodes.setdefault(t, r.get("target_label") or "")
        key = (s, r.get("edge_type"), t)
        if key not in _edge_keys:
            _edge_keys.add(key)
            vs = r.get("verify_status") or "untested"
            color, vs_text = VS_COLOR.get(vs, VS_COLOR["untested"])
            seen_edges.append({
                "source": s, "target": t, "color": color,
                "width": 2.0 if vs == "confirmed" else 1.5,
                "title": f"{r.get('edge_type')} · {vs_text}",
            })
        out.append(t if downstream else s)
    return out


frontier = [anchor]
visited = {anchor}
for _ in range(int(hops)):
    nxt = []
    for name in frontier:
        nxt += absorb(directed_rows(name, tuple(DEP_EDGES), int(limit), downstream))
    frontier = [n for n in nxt if n not in visited]
    visited.update(frontier)

# 手动展开：每个只走一跳。放在基础 BFS 之后，这样它们带出来的边是**增量**，
# 而不是把跳数整体调大 —— 后者会让所有分支一起膨胀，正是要避免的。
for name in sorted(expanded):
    absorb(directed_rows(name, tuple(DEP_EDGES), int(limit), downstream))

deg: dict[str, int] = {}
for e in seen_edges:
    deg[e["source"]] = deg.get(e["source"], 0) + 1
    deg[e["target"]] = deg.get(e["target"], 0) + 1

nodes = []
for nid, lb in seen_nodes.items():
    gname, gcolor = group_of(lb)
    n = {
        "id": nid,
        "label": (("★ " if nid == anchor else "") + nid)[:34],
        "type": lb, "group": gname, "degree": deg.get(nid, 0),
        "accent": gcolor,
        "stroke": "#3759ce" if nid == anchor else "#c8d1de",
    }
    # 已展开的标个记号：不然用户双击过之后看不出哪些走过了，
    # 会重复双击同一个节点并以为没生效。
    if nid in expanded:
        n["badge"] = "✓"
        n["badge_fill"] = "#2E7D32"
        n["fill"] = "#f4fbf5"
    nodes.append(n)

m = st.columns(5)
m[0].metric("节点", len(nodes))
m[1].metric("依赖边", len(seen_edges))
m[2].metric("基础跳数", int(hops))
m[3].metric("手动展开", len(expanded))
m[4].metric("分组", len({n["group"] for n in nodes}))

if expanded:
    if st.button("↺ 清空手动展开", help="回到只有基础跳数的那张图"):
        st.session_state[S_EXPANDED] = set()
        st.rerun()

res = graph_svg.render(nodes, seen_edges, height=640, anchor=anchor, key="proto")

# 双击展开：把节点并进集合并重画。
# 只在它**还不在集合里**时才 rerun —— setTriggerValue 是一次性的，但同一个
# 值在 rerun 后若仍被读到就会形成无限循环，这个判断同时也是那道防线。
hit = res.get("expand")
if hit and hit not in expanded:
    expanded.add(hit)
    st.session_state[S_EXPANDED] = expanded
    st.rerun()

st.caption(
    "**单击**节点看下方详情，**双击**展开它的下一跳（展开过的节点带 ✓ 记号）。"
    "左侧色条 = 分组，边色 = 故障注入验证状态。节点名恒定 13px —— "
    "不做「缩到装下」，图比容器大时滚动浏览。"
)

st.markdown("---")
d1, d2 = st.columns([3, 2])
with d1:
    st.subheader("选中节点")
    sel = res.get("selected")
    if sel:
        n = next((x for x in nodes if x["id"] == sel), None)
        st.write(f"**{sel}**")
        if n:
            st.caption(f"类型 `{n['type']}` · 分组 {n['group']} · 度数 {n['degree']}")
        st.caption("↑ 这段来自前端 setStateValue 的回传，证明双向通道通了。")
    else:
        st.caption("还没点过节点。单击图上任意节点，这里会显示它的信息。")
with d2:
    st.subheader("展开路径")
    if expanded:
        for name in sorted(expanded):
            st.markdown(f"- `{name}`")
        st.caption(
            "双击这些节点各自多走了一跳。它们是**增量**，不是把基础跳数调大 —— "
            "后者会让所有分支一起膨胀。"
        )
    else:
        st.caption(
            "还没手动展开过。双击一个节点会把它的下一跳并进这张图，"
            "而不是重画一张 —— 这是「顺着依赖走」和「换一张快照」的区别。"
        )
