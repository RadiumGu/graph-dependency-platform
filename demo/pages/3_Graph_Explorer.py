"""
3_Graph_Explorer.py — 依赖图谱浏览。

## 2026-09-05 第二轮重写：从「全图力导向」改为「锚点 + 分层」

第一轮修掉的是 bug（取数顺序错误导致 75% 的边被丢弃）。修完之后暴露出真问题：
**全图力导向布局本身就是错的展示方式。**

调研结论（6 路并行调研，证据见 todo/webui/05-图谱展示方案调研_20260905-0645.md）：

1. **这是稀疏图，不是密集图。** 88 节点 / 146 边 = 平均度 3.3、密度 0.038。
   它难看不是因为边多，而是因为力导向把数据里最强的两个信号扔了：
   **方向**与**天然分层**。

2. **认知预算有硬数字。** Yoghourdjian 等（IEEE TVCG 2020，EEG + 眼动对照）：
   节点超过约 50 个时被试答错或不确定的比例超过 50%。所以首屏目标
   **20–30 个可见节点**、默认 **1 跳**。

3. **10 个成熟产品无一渲染全图。** Bloom 与 AWS graph-explorer 开局是空画布 +
   搜索种子；Kiali 强制先选 namespace；Grafana 默认只画 200 个节点其余折叠；
   Datadog「左边靠近客户、右边更可能是根因」。Grafana 的默认布局是 Layered，
   并明确写「force 只建议 500+ 节点时用」。

4. **颜色可区分上限约 7 类、形状约 5 类**（arXiv 2103.06084）。我们有 39 种
   节点类型 —— **没有任何首屏能同时用颜色编码它们**。必须折成 ≤7 个色组
   （Okabe–Ito 色盲安全配色）+ 形状做冗余编码。

5. **最省力的一招在数据里已经有了**：契约的 `dependency: true` 把 29 种边分成
   6 种语义依赖边与 23 种结构/包含边。把包含边（RunsOn / LocatedIn / BelongsTo /
   Contains…）也画成箭头正是杂乱的主要来源。现在结构边默认关闭。

## 已知天花板

pyvis 是**单向**的 —— 选中的节点拿不回 Python，所以做不了「点节点展开」。
本页用 Streamlit 侧的选择器指定锚点来绕开这一点。要做真正的点击展开需要迁到
st-link-analysis（dagre + 选中回传），那是下一步。
"""
import colorsys
import hashlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _common as C  # noqa: E402

import streamlit as st  # noqa: E402

C.page_setup("图谱浏览", icon="🕸️")
C.sidebar()

# ── 分层：把节点类型钉到层级上 ────────────────────────────────────────────────
# 这是「让布局有结构」的核心。vis-network 的 sortMethod='directed' 没有
# feedback-arc-set 步骤，遇到环会把层级算乱；显式钉 level 可以绕开。
TYPE_LEVEL = {
    # 0 业务
    "BusinessCapability": 0,
    # 1 入口
    "LoadBalancer": 1, "ListenerRule": 1, "TargetGroup": 1,
    # 2 应用
    "Microservice": 2, "LambdaFunction": 2, "StepFunction": 2,
    "AgentRuntime": 2, "AgentGateway": 2,
    # 3 应用附属
    "AgentTool": 3, "K8sService": 3, "Deployment": 3, "HPA": 3,
    # 4 运行时
    "Pod": 4, "Namespace": 4,
    # 5 计算宿主
    "EC2Instance": 5, "EKSCluster": 5,
    # 6 数据与消息（依赖的终点）
    "RDSCluster": 6, "RDSInstance": 6, "Database": 6, "DynamoDBTable": 6,
    "S3Bucket": 6, "NeptuneCluster": 6, "NeptuneInstance": 6,
    "SQSQueue": 6, "SNSTopic": 6, "ECRRepository": 6,
    "AgentMemory": 6, "KnowledgeBase": 6, "AWSServiceEndpoint": 6,
    # 7 基础设施
    "VPC": 7, "Subnet": 7, "SecurityGroup": 7, "AvailabilityZone": 7, "Region": 7,
    # 8 运维事件
    "Incident": 8, "ChaosExperiment": 8, "TopologyChange": 8, "Guardrail": 8,
}

# ── 颜色：折成 7 组，Okabe–Ito 色盲安全配色 ──────────────────────────────────
# 39 种类型不可能各给一个可区分的颜色（上限约 7），所以颜色表示**大类**，
# 具体类型靠形状 + hover 提示做冗余编码。
#: 形状**必须**从 vis-network 里「按 size 缩放」的那一族里选：
#: dot / star / triangle / triangleDown / square / diamond / hexagon。
#: 另一族（database / box / ellipse / circle / text）是**按标签宽度自适应**的——
#: 用 database 画数据存储时，长名字会把柱子撑到几百像素、把标签画进形状内部，
#: 并压住邻居。这就是最初「节点重叠」的真因，不是间距不够。
#: star 留给锚点，故 7 个分组用 6 种形状，消息与数据存储共用 square（颜色差异极大）。
GROUPS = [
    ("业务能力", "#e07941", "triangleDown", {"BusinessCapability"}),
    ("入口与路由", "#688ae8", "triangle", {"LoadBalancer", "ListenerRule", "TargetGroup"}),
    ("应用与计算", "#3759ce", "dot", {
        "Microservice", "LambdaFunction", "StepFunction", "Deployment",
        "K8sService", "HPA", "Pod", "Namespace", "EKSCluster", "EC2Instance"}),
    ("数据存储", "#2ea597", "square", {
        "RDSCluster", "RDSInstance", "Database", "DynamoDBTable", "S3Bucket",
        "NeptuneCluster", "NeptuneInstance", "ECRRepository"}),
    ("消息", "#b2911c", "square", {"SQSQueue", "SNSTopic"}),
    ("Agent / GenAI", "#8456ce", "hexagon", {
        "AgentRuntime", "AgentTool", "AgentGateway", "AgentMemory",
        "KnowledgeBase", "Guardrail"}),
    ("基础设施与运维", "#8c8c94", "diamond", {
        "VPC", "Subnet", "SecurityGroup", "AvailabilityZone", "Region",
        "AWSServiceEndpoint", "Incident", "ChaosExperiment", "TopologyChange"}),
]


def group_of(label: str):
    for name, color, shape, members in GROUPS:
        if label in members:
            return name, color, shape
    # 契约里新增而这里还没归类的类型：给个稳定的兜底色，不静默变白
    h = int(hashlib.md5(label.encode()).hexdigest()[:8], 16)
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360.0, 0.6, 0.5)
    return "未归类", f"#{int(r*255):02X}{int(g*255):02X}{int(b*255):02X}", "dot"


ALL_NODE_TYPES = sorted((C.contract().get("node_types") or {}).keys())
DEP_EDGES = sorted(C.dependency_edge_labels())
STRUCT_EDGES = sorted(set((C.contract().get("edge_types") or {}).keys()) - set(DEP_EDGES))

# ── scope → 横轴簇 ───────────────────────────────────────────────────────────
# 维度不是新造的：契约 `node_scope` 就是为「区分被观测的业务系统 vs 平台自身
# 与可观测性设施」而建，全图已标注，选边器一直用它排除 platform/observability。
# 本页此前完全没用它，于是业务节点和监控设施混在同一条扁行里——这就是排版难看
# 且「业务与运维分不开」的根因。
#
# ⚠️ 节点上的属性名是 `scope`，不是 `node_scope`（契约 `node_scope.attr: scope`）。
#
# 顺序即从左到右的簇顺序：业务在最左（主角），越往右越偏基础设施。
SCOPE_BANDS: tuple = (
    ("observed",      "业务系统"),
    ("external",      "外部服务"),
    ("observability", "可观测性"),
    ("platform",      "平台自身"),
    ("cluster-infra", "集群底座"),
    ("scaffolding",   "脚手架"),
    ("unknown",       "未分类"),
)
_BAND_ORDER = {s: i for i, (s, _) in enumerate(SCOPE_BANDS)}
_BAND_TITLE = dict(SCOPE_BANDS)

# 图上还有一批节点没被 scope-labeler 覆盖到，其中 AWSServiceEndpoint
# （sns / sts / ssm / stepfunctions）恰恰是 petsite 依赖的 AWS 托管服务。
# 让它们掉进「未分类」簇会把真实的业务依赖藏进垃圾堆，所以这里按节点类型
# 做**仅展示层**的兜底推断。
#
# 这不写回图谱：scope 的权威写入方是 `scope-labeler`（契约 authority），
# 展示层猜的值不能冒充它的结论。真正的修复是让 labeler 覆盖这些类型。
_LABEL_SCOPE_FALLBACK = {
    "AWSServiceEndpoint": "external",
    "SaaSEndpoint": "external",
}


def band_of(scope: str, node_label: str) -> str:
    """节点归哪个横轴簇。scope 优先，缺失时按节点类型兜底，最后才是 unknown。"""
    sc = (scope or "").strip()
    if sc in _BAND_ORDER:
        return sc
    guess = _LABEL_SCOPE_FALLBACK.get(node_label)
    if guess in _BAND_ORDER:
        return guess
    return "unknown"


online = C.neptune_online()

st.title("🕸️ 依赖图谱")
st.markdown(
    "> 默认**不渲染全图**——从一个服务出发看它的 1 跳邻域。"
    "这是 Datadog / Neo4j Bloom / Kiali / Grafana 等成熟工具的共同做法："
    "全图渲染在认知上不可读（实测节点超过约 50 个，判断准确率就掉到一半以下）。"
)

# ── 视图模式 ──────────────────────────────────────────────────────────────────
SCENARIOS = {
    "🎯 依赖验证现状": {
        "why": "只看依赖边，按注入验证状态着色——本平台的核心产出",
        "edges": DEP_EDGES, "struct": False, "hops": 1,
    },
    "🗄️ 应用到数据层": {
        "why": "微服务 / Lambda 依赖了哪些数据存储与消息队列",
        "edges": ["AccessesData", "DependsOn", "PublishesTo", "WritesTo"],
        "struct": False, "hops": 1,
    },
    "🤖 Agent 依赖": {
        "why": "agent 委派、工具调用、知识库检索，以及连回主图的那条桥",
        "edges": ["Delegates", "InvokesTool", "Retrieves", "DependsOn"],
        "struct": False, "hops": 2,
    },
    "🌐 服务间调用": {
        "why": "只看 Calls —— 谁在调谁",
        "edges": ["Calls"], "struct": False, "hops": 2,
    },
}

mode = st.radio(
    "视图", ["预置场景", "选一个服务看邻域", "全图（不推荐）"],
    horizontal=True, index=0,
    help="预置场景各回答一个问题；全图会退化成散点，仅供对照",
)

anchor = None
hops = 1
edge_types = DEP_EDGES
show_struct = False
node_cap = 30

if mode == "预置场景":
    if "gx_scenario" not in st.session_state:
        st.session_state["gx_scenario"] = list(SCENARIOS)[0]
    cols = st.columns(len(SCENARIOS))
    for col, (name, cfg) in zip(cols, SCENARIOS.items()):
        with col:
            if st.button(name, width="stretch",
                         type="primary" if st.session_state["gx_scenario"] == name else "secondary"):
                st.session_state["gx_scenario"] = name
            st.caption(cfg["why"])
    sc = SCENARIOS[st.session_state["gx_scenario"]]
    edge_types, show_struct, hops = sc["edges"], sc["struct"], sc["hops"]
    node_cap = 30

    # 场景也必须有锚点。第一版没加锚点，结果「依赖验证现状」是全图查询，
    # 拿到 107 条边 / 66 个节点，分层布局把它们压成一条细带——完全不可读。
    # 「锚点 + 有界邻域」是调研里排第一的建议，不是可选项。
    svc = C.service_names()
    default_anchor = sc.get("anchor") or ("petsite" if "petsite" in svc else (svc[0] if svc else None))
    a1, a2 = st.columns([2, 3])
    anchor = a1.selectbox(
        "从哪个服务看起", svc,
        index=svc.index(default_anchor) if default_anchor in svc else 0,
        key="gx_sc_anchor",
    )
    a2.caption(
        f"当前场景：**{st.session_state['gx_scenario']}** —— {sc['why']}\n\n"
        f"关系类型 {len(edge_types)} 种 · {hops} 跳 · 节点上限 {node_cap}"
    )

elif mode == "选一个服务看邻域":
    svc = C.service_names()
    a1, a2, a3 = st.columns([2, 1, 2])
    anchor = a1.selectbox("锚点服务", svc, index=svc.index("petsite") if "petsite" in svc else 0)
    hops = a2.slider("跳数", 1, 2, 1, help="调研结论：默认 1 跳；2 跳只在低度节点上可读")
    edge_types = a3.multiselect("关系类型", DEP_EDGES + STRUCT_EDGES, default=DEP_EDGES)
    show_struct = any(e in STRUCT_EDGES for e in edge_types)
    node_cap = 40

else:
    st.warning(
        "全图模式仅供对照——它会退化成一片散点。"
        "**这正是问题所在**：力导向布局丢掉了方向与分层，"
        "而节点一多，人就读不出结构了。",
        icon="⚠️",
    )
    e1, e2 = st.columns([3, 1])
    edge_types = e1.multiselect("关系类型", DEP_EDGES + STRUCT_EDGES, default=DEP_EDGES)
    node_cap = e2.slider("节点上限", 30, 300, 120, 10)
    show_struct = any(e in STRUCT_EDGES for e in edge_types)

if not edge_types:
    st.warning("请至少选择一种关系类型。")
    st.stop()

with st.sidebar:
    st.markdown("---")
    st.markdown("### 显示")
    layered = st.toggle("分层布局", value=True,
                        help="关掉就退回力导向——可以直接对比两者差别")
    show_labels = st.toggle("显示节点名", value=True)

    # ── scope 筛选：把业务视图和运维设施分开看 ────────────────────────────
    # 默认只留「业务系统 + 外部服务」：这两类构成一次业务影响面分析真正要看的
    # 东西。可观测性 / 平台自身 / 集群底座加起来 761 个节点，混进来会把 40 个
    # 节点的预算吃光，业务依赖反而被挤掉。
    #
    # 外部服务默认**开着**：sns / sqs / dynamodb / ssm 这些 AWSServiceEndpoint
    # 是 petsite 真实的下游依赖，藏掉它们等于让业务视图缺一块。
    st.markdown("### 范围（scope）")
    _DEFAULT_BANDS = ["observed", "external"]
    visible_bands = st.multiselect(
        "显示哪些簇",
        options=[b for b, _ in SCOPE_BANDS],
        default=_DEFAULT_BANDS,
        format_func=lambda b: f"{_BAND_TITLE.get(b, b)}（{b}）",
        help="业务系统=被观测的 petsite 及其依赖；外部服务=AWS 托管服务端点"
             "（sns/sqs/dynamodb…）；其余为可观测性与平台自身设施。",
    )
    if set(visible_bands) != set(_DEFAULT_BANDS):
        st.caption("⚠️ 已偏离默认范围，图上的节点不再等于「业务影响面」。")
    height = st.slider("画布高度 (px)", 400, 1000, 640, 20)


# ── 取数 ──────────────────────────────────────────────────────────────────────
def _edge_clause(ets: list) -> str:
    return ", ".join(f"'{e}'" for e in ets)


@st.cache_data(ttl=60, show_spinner=False)
def fetch_neighborhood(anchor_name: str, ets: tuple, n_hops: int, cap: int) -> dict:
    """
    锚点 + N 跳。

    实现要点：**用 Python 侧迭代做单跳扩展，不用变长路径**。
    第一版写的是 `MATCH p = (a)-[r*1..N]-(b) ... UNWIND relationships(p)`，
    Neptune 直接回 400 Bad Request——它对带谓词的变长路径 + relationships()
    支持有限。迭代单跳是纯基础语法，一定能跑，而且顺便让跳数边界更可控。
    """
    ec = _edge_clause(list(ets))
    seen_names = {anchor_name}
    all_edges: list = []
    frontier = {anchor_name}

    for _ in range(max(1, int(n_hops))):
        if not frontier:
            break
        # 名字里的单引号要挡掉，避免拼接出坏查询
        names = ", ".join(f"'{n}'" for n in sorted(frontier) if "'" not in n)
        if not names:
            break
        res = C.gquery(
            f"MATCH (a)-[r]-(b) WHERE a.name IN [{names}] AND type(r) IN [{ec}] "
            "RETURN coalesce(startNode(r).name, startNode(r).tool_key, startNode(r).arn) AS source, "
            "labels(startNode(r))[0] AS source_label, type(r) AS edge_type, "
            "coalesce(endNode(r).name, endNode(r).tool_key, endNode(r).arn) AS target, "
            "labels(endNode(r))[0] AS target_label, "
            "coalesce(startNode(r).scope,'') AS source_scope, "
            "coalesce(endNode(r).scope,'') AS target_scope, "
            "coalesce(r.verify_status,'') AS verify_status, "
            "coalesce(r.source,'') AS edge_source "
            f"LIMIT {int(cap) * 6}"
        )
        if "error" in res:
            return res
        nxt = set()
        for row in res["results"]:
            key = (row.get("source"), row.get("edge_type"), row.get("target"))
            if key not in {(e.get("source"), e.get("edge_type"), e.get("target"))
                           for e in all_edges}:
                all_edges.append(row)
            for nm in (row.get("source"), row.get("target")):
                if nm and nm not in seen_names:
                    seen_names.add(nm)
                    nxt.add(nm)
        frontier = nxt
        if len(seen_names) >= cap * 2:
            break

    return {"results": all_edges}


@st.cache_data(ttl=60, show_spinner=False)
def fetch_all(ets: tuple, cap: int) -> dict:
    ec = _edge_clause(list(ets))
    return C.gquery(
        f"MATCH (a)-[r]->(b) WHERE type(r) IN [{ec}] "
        "RETURN coalesce(a.name,a.tool_key,a.arn) AS source, labels(a)[0] AS source_label, "
        "type(r) AS edge_type, coalesce(b.name,b.tool_key,b.arn) AS target, "
        "labels(b)[0] AS target_label, coalesce(r.verify_status,'') AS verify_status, "
        "coalesce(a.scope,'') AS source_scope, coalesce(b.scope,'') AS target_scope, "
        "coalesce(r.source,'') AS edge_source "
        f"LIMIT {int(cap) * 3}"
    )


def from_snapshot(ets: list, anchor_name, n_hops: int, cap: int) -> list:
    snap = C.fixture("sample_topology")
    edges = [e for e in snap.get("edges", []) if e.get("edge_type") in ets]
    if not anchor_name:
        return edges[: cap * 3]
    # 快照里做 N 跳广度搜索
    frontier, seen_nodes, keep = {anchor_name}, {anchor_name}, []
    for _ in range(n_hops):
        nxt = set()
        for e in edges:
            if e.get("source") in frontier or e.get("target") in frontier:
                if e not in keep:
                    keep.append(e)
                for nm in (e.get("source"), e.get("target")):
                    if nm and nm not in seen_nodes:
                        seen_nodes.add(nm)
                        nxt.add(nm)
        frontier = nxt
    return keep


data_mode = "none"
edges: list = []
if online:
    res = (fetch_neighborhood(anchor, tuple(edge_types), hops, node_cap)
           if anchor else fetch_all(tuple(edge_types), node_cap))
    if "error" not in res:
        edges, data_mode = res["results"], "live"
    else:
        st.warning(f"实时查询失败，回退快照：{res['error']}")
if data_mode != "live":
    edges = from_snapshot(edge_types, anchor, hops, node_cap)
    data_mode = "snapshot" if edges else "none"

C.mode_badge(data_mode, "拓扑数据")

# 节点由边推导（第一轮修复：保证每条边都画得出来）
nodes: dict = {}
for e in edges:
    for nm, lb, sc in ((e.get("source"), e.get("source_label"), e.get("source_scope")),
                       (e.get("target"), e.get("target_label"), e.get("target_scope"))):
        if nm:
            prev = nodes.get((lb, nm))
            # 同一节点可能在多条边里出现；scope 取第一个非空值，避免被
            # 后来一条没带 scope 的边（快照数据就没有这个字段）擦成空。
            keep_sc = (prev or {}).get("scope") or sc or ""
            nodes[(lb, nm)] = {"label": lb, "name": nm, "scope": keep_sc,
                               "band": band_of(keep_sc, lb)}

# ── 按 scope 筛掉不看的簇 ─────────────────────────────────────────────────────
# **必须在节点上限裁剪之前**做：否则可观测性/平台那 761 个节点会先把 node_cap
# 的预算吃掉，真正要看的业务依赖反而被裁掉 —— 那正是这个筛选器要解决的问题。
# 边也要一起筛：留下指向已隐藏节点的边，vis 会为它凭空建出一个无属性的孤点。
hidden_bands = 0
if visible_bands:
    _allow = set(visible_bands)
    keep_keys = {
        k for k, m in nodes.items()
        # 锚点永不隐藏：它的簇被关掉时整张图就没有意义了
        if m.get("band") in _allow or (anchor is not None and m["name"] == anchor)
    }
    hidden_bands = len(nodes) - len(keep_keys)
    if hidden_bands:
        keep_names = {nm for (_lb, nm) in keep_keys}
        nodes = {k: v for k, v in nodes.items() if k in keep_keys}
        edges = [e for e in edges
                 if e.get("source") in keep_names and e.get("target") in keep_names]

if not edges:
    st.warning(
        "当前条件下没有关系。"
        + (f"锚点 `{anchor}` 在所选关系类型下可能没有邻居——试试增加关系类型或跳数。"
           if anchor else "试试多选几种关系类型。")
    )
    st.stop()

# ── 超预算时按「离锚点近」裁剪，而不是任意截断 ────────────────────────────────
# 任意截断会砍掉锚点的直接邻居而留下无关的远端节点，那比不裁更糟。
trimmed = 0
if anchor and len(nodes) > node_cap:
    # BFS 分层，逐层收进来直到触到上限
    adj: dict = {}
    for e in edges:
        s, t = e.get("source"), e.get("target")
        adj.setdefault(s, set()).add(t)
        adj.setdefault(t, set()).add(s)
    keep_names, frontier = {anchor}, {anchor}
    while frontier and len(keep_names) < node_cap:
        nxt = set()
        for n in frontier:
            for m in sorted(adj.get(n, ())):
                if m not in keep_names:
                    if len(keep_names) >= node_cap:
                        break
                    keep_names.add(m)
                    nxt.add(m)
        frontier = nxt
    before = len(edges)
    edges = [e for e in edges
             if e.get("source") in keep_names and e.get("target") in keep_names]
    trimmed = before - len(edges)
    nodes = {}
    for e in edges:
        for nm, lb in ((e.get("source"), e.get("source_label")),
                       (e.get("target"), e.get("target_label"))):
            if nm:
                nodes[(lb, nm)] = {"label": lb, "name": nm}

# 分层布局下，最宽的那一层决定需要多高的画布。
# 固定高度 + 多节点同层 = nodeSpacing 放不开，会被压成一条细带（第一版就是这样）。
#
# 层级怎么定，取决于有没有锚点：
#   有锚点 → 按**相对锚点的有向跳距**：调用方在左、锚点居中、依赖在右。
#            这比按类型分层更贴合 ego view —— 上游调用者往往和锚点同类型
#            （都是 Microservice），按类型分会把它们和锚点堆在同一列。
#            这也是 Datadog 的做法：「左边的服务靠近客户，右边的更可能是根因」。
#   无锚点 → 退回按类型分层（TYPE_LEVEL）。
LEVEL_BASE = 10  # 偏移量，避免负数层级


def compute_levels(edge_list: list, anchor_name) -> dict:
    """返回 {(label, name): level}。"""
    if not anchor_name:
        return {k: TYPE_LEVEL.get(v["label"], 9) for k, v in nodes.items()}

    out_adj: dict = {}
    in_adj: dict = {}
    for e in edge_list:
        s, t = e.get("source"), e.get("target")
        out_adj.setdefault(s, set()).add(t)
        in_adj.setdefault(t, set()).add(s)

    depth = {anchor_name: 0}
    # 顺着出边走：依赖方向，层级递增
    frontier = {anchor_name}
    d = 0
    while frontier and d < 6:
        d += 1
        nxt = set()
        for n in frontier:
            for m in out_adj.get(n, ()):
                if m not in depth:
                    depth[m] = d
                    nxt.add(m)
        frontier = nxt
    # 逆着入边走：调用方向，层级递减
    frontier = {anchor_name}
    d = 0
    while frontier and d < 6:
        d += 1
        nxt = set()
        for n in frontier:
            for m in in_adj.get(n, ()):
                if m not in depth:
                    depth[m] = -d
                    nxt.add(m)
        frontier = nxt

    # 从锚点走不到的节点（只与被裁掉的边相连，或处在别的连通片里）
    # 不能落回 depth 默认值 0 —— 那等于把它们全塞进锚点自己那一列，
    # 于是一列里挤十几个节点、图形互相压住（截图里 ssm/sts 压在绿柱上就是这个）。
    # 给它们一个比最深下游更远的独立层，自成一列。
    orphan_level = (max(depth.values()) if depth else 0) + 2
    return {
        (lb, nm): LEVEL_BASE + (depth[nm] if nm in depth else orphan_level)
        for (lb, nm) in nodes
    }


levels = compute_levels(edges, anchor)


# ── 二维坐标：纵轴 = 依赖方向，横轴 = scope 簇 ────────────────────────────────
def compute_positions(node_map: dict, level_map: dict) -> tuple:
    """
    返回 ({(label,name): (x, y)}, [(簇名, 簇标题, 簇中心x, 表头y)])。

    为什么自己算坐标、不用 vis 的 hierarchical：
      1. hierarchical 只有一个排序维度，塞不进「方向 × scope」两个维度；
      2. 它会在每个节点上写死 fixed.y（UD 时）来防止节点离开自己那层，
         导致节点只能左右拖不能上下拖 —— 自己算坐标就没有这个锁，
         四个方向天然都能拖。
    """
    X_GAP = 165          # 同层节点间距：要容得下标签宽度，否则标签互相糊
    BAND_PAD = 130       # 簇间留白：这段空白就是「业务 / 运维」的视觉分界
    MAX_PER_ROW = 6      # 一行最多几个，超了折行
    SUB_Y_GAP = 80       # 折行后子行的行距（小于层距，读起来仍是同一层）
                         # 不能再小：标签渲染在图标正下方，锚点半径就有 32，
                         # 60 出头会让标签与下一子行的图标糊在一起。
    LEVEL_PAD = 150      # 层与层之间额外留白

    # 先按 (簇, 层) 归组
    grid: dict = {}
    for key, meta in node_map.items():
        band = meta.get("band", "unknown")
        lv = level_map.get(key, 9)
        grid.setdefault(band, {}).setdefault(lv, []).append(key)

    # 簇内同层的节点排序要稳定，否则每次刷新节点位置乱跳，看着像图变了
    for rows in grid.values():
        for keys in rows.values():
            keys.sort(key=lambda k: (str(k[0]), str(k[1])))

    def sub_rows(n: int) -> int:
        return max(1, -(-n // MAX_PER_ROW))     # 上取整

    # ── 纵轴：逐层累加，层高按该层最多需要几个子行决定 ──
    # 不能给所有层同一个固定层距：某一层折成 3 行时会撞进下一层。
    all_lv = sorted({lv for rows in grid.values() for lv in rows})
    level_y: dict = {}
    run = 0.0
    for lv in all_lv:
        level_y[lv] = run
        need = max(sub_rows(len(grid[b][lv])) for b in grid if lv in grid[b])
        run += (need - 1) * SUB_Y_GAP + LEVEL_PAD + SUB_Y_GAP

    # ── 横轴：簇宽由该簇最宽的一行决定（已折行，所以不超过 MAX_PER_ROW）──
    present = [b for b, _ in SCOPE_BANDS if b in grid]
    widths = {
        b: max(min(len(v), MAX_PER_ROW) for v in grid[b].values()) * X_GAP
        for b in present
    }

    pos: dict = {}
    centers: dict = {}
    cursor = 0.0
    for b in present:
        w = widths[b]
        center = cursor + w / 2.0
        centers[b] = center
        for lv, keys in grid[b].items():
            base_y = level_y[lv]
            for r in range(sub_rows(len(keys))):
                chunk = keys[r * MAX_PER_ROW:(r + 1) * MAX_PER_ROW]
                n = len(chunk)
                start = center - (n - 1) * X_GAP / 2.0
                for i, key in enumerate(chunk):
                    pos[key] = (start + i * X_GAP, base_y + r * SUB_Y_GAP)
        cursor += w + BAND_PAD

    # 表头统一在全图最顶端之上同一高度 —— 各簇用自己的顶层会让表头高低不齐。
    header_y = (min(level_y.values()) if level_y else 0.0) - LEVEL_PAD * 0.9
    bands_meta = [(b, _BAND_TITLE.get(b, b), centers[b], header_y) for b in present]
    return pos, bands_meta


positions, bands_meta = compute_positions(nodes, levels)

level_width: dict = {}
for key in nodes:
    lv = levels.get(key, 9)
    level_width[lv] = level_width.get(lv, 0) + 1
widest = max(level_width.values()) if level_width else 1
# 画布高度：**不能**按最宽层无限加高。
# 之前按 widest*46 长到 1600px，iframe 比视口还高，于是纵向拖动被页面滚动吃掉、
# 横向又没有可滚的方向 —— 表现就是「只能上下拖，不能左右拖」。
# 正确做法与 Grafana / Datadog 一致：画布固定在视口内，图靠 fit + 平移缩放浏览。
auto_height = min(900, max(height, 560))

over = len(nodes) > 50
m = st.columns(4)
m[0].metric("关系", len(edges))
m[1].metric("节点", len(nodes),
            help=("⚠️ **超认知预算** —— 实测节点超过约 50 个，判断准确率就掉到一半"
                  "以下。换个更小的场景或减少跳数。"
                  if over else
                  "在认知预算内（约 50 个节点以下）。"))
m[2].metric("节点类型", len({n["label"] for n in nodes.values()}))
m[3].metric("关系类型", len({e["edge_type"] for e in edges}))

if hidden_bands:
    _hidden_titles = "、".join(
        _BAND_TITLE.get(b, b) for b, _ in SCOPE_BANDS if b not in set(visible_bands)
    )
    st.caption(
        f"🔍 按 scope 隐藏了 {hidden_bands} 个节点（{_hidden_titles}）。"
        "筛选发生在节点上限裁剪**之前**，所以这 "
        f"{node_cap} 个节点的预算全花在你选中的簇上。"
        "在左侧「范围（scope）」里可以把它们放回来。"
    )

if trimmed:
    st.caption(
        f"ℹ️ 为控制在 {node_cap} 个节点内，按「离锚点距离」裁掉了 {trimmed} 条较远的关系"
        "（不是任意截断——那会砍掉锚点的直接邻居）。想看更多请提高跳数或换场景。"
    )

if over:
    st.warning(
        f"当前 {len(nodes)} 个节点已超过约 50 的可读上限"
        "（IEEE TVCG 2020 实测：超过后判断准确率掉到一半以下）。"
        "建议减少关系类型、降到 1 跳，或改用预置场景。",
        icon="🧠",
    )
# ── 画布会不会超出视口：用真实 x 跨度判断，不用「最宽一层几个节点」代理 ──────
#
# 原来这里写的是：
#
#     if layered and widest > 12:
#         st.caption("画布已自动缩放到能装下全图，可用滚轮缩放…")
#
# **那句话是错的。** `fitReadable()`（见下方注入的 JS）先调 `network.fit()`，
# 但如果结果缩放低于 `MIN_SCALE = 0.75`，它会**放弃装下全图** ——
# 改为保持可读比例并对准锚点，剩下靠平移。理由是 fit() 为了装下全图会无限缩小，
# 节点标签变成糊点，那样图就白画了。这个取舍是对的，但提示说了反话。
#
# 而且触发条件也不对：`widest > 12` 是个代理量。实测一个 20 节点的 petsite
# 视图最宽层只有 10 个节点（提示不出现），x 跨度却有 1615px —— 主容器约 900px，
# fit 需要 0.56 倍、低于下限，于是钳到 0.75，约 26% 的内容在视口外。
# **需要拖动的时候提示反而不显示。**
#
# 改用真实 x 跨度：内容宽度 × MIN_SCALE 超过一个保守的容器宽度就提示。
_MIN_SCALE = 0.75          # 与下方 JS 里的 MIN_SCALE 必须一致
_ASSUMED_CANVAS_PX = 900   # 保守估计：1280 视口减去侧栏与留白
if layered and positions:
    _xs = [p[0] for p in positions.values()]
    _span = max(_xs) - min(_xs)
    if _span * _MIN_SCALE > _ASSUMED_CANVAS_PX:
        _visible = _ASSUMED_CANVAS_PX / _MIN_SCALE
        st.caption(
            f"ℹ️ 图的横向跨度约 {_span:.0f}px，比画布宽 —— "
            f"**约 {max(0, 1 - _visible / _span) * 100:.0f}% 的内容在视口外，"
            "按住空白处拖动即可看到。**\n\n"
            f"这是刻意的取舍：自动缩放到装下全图会让缩放降到 "
            f"{_ASSUMED_CANVAS_PX / _span:.2f} 倍，节点标签变成糊点、图就白画了。"
            f"所以缩放保持在可读下限 {_MIN_SCALE} 并对准锚点，"
            "剩下靠平移。也可用滚轮缩放或右下角导航按钮。"
        )
    elif widest > 12:
        st.caption(
            f"ℹ️ 最宽的一层有 {widest} 个节点，画布已缩放到能装下全图。"
            "可用滚轮缩放、按住空白处拖动平移，或用右下角导航按钮。"
        )


# ── 渲染 ──────────────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def build_html(nodes_t: tuple, edges_t: tuple, h: int, use_layered: bool,
               labels: bool, anchor_name, bands_t: tuple = ()) -> str:
    import json

    from pyvis.network import Network

    net = Network(height=f"{h}px", width="100%", bgcolor="#ffffff",
                  font_color="#222222", directed=True)

    # 簇标题：用 shape='text' 的哑节点当作泳道表头。
    # 没有它，横向的留白只是留白；有了它，留白才读得出「左边是业务、右边是设施」。
    if use_layered:
        for band, title, cx, cy in bands_t:
            net.add_node(
                f"__band__::{band}", label=f"{title}",
                shape="text", x=float(cx), y=float(cy), physics=False,
                font={"size": 20, "face": "sans-serif", "color": "#8a94a6",
                      "strokeWidth": 4, "strokeColor": "#ffffff"},
                title=f"scope = {band}",
            )

    ids = set()
    for label, name, lvl, band, px, py in nodes_t:
        nid = f"{label}::{name}"
        ids.add(nid)
        gname, color, shape = group_of(label)
        is_anchor = anchor_name is not None and name == anchor_name
        extra = {}
        if use_layered:
            # 显式坐标 + physics 关：节点停在算好的位置，但没有 fixed 锁，
            # 所以上下左右都能拖（拖完也不会被物理引擎弹回去）。
            extra = {"x": float(px), "y": float(py), "physics": False}
        net.add_node(
            nid,
            label=(str(name)[:18] if labels else " "),
            title=f"{label}\n{name}\n分组：{gname}"
                  f"\nscope：{band}"
                  + ("\n（锚点）" if is_anchor else ""),
            # 锚点用 AWS 品牌橙 + Squid Ink 描边：明确是「焦点」，
            # 不用红色——红在本页已经表示「已证伪」，两种含义不能撞。
            color={"background": "#ff9900" if is_anchor else color,
                   "border": "#232f3e" if is_anchor else "#ffffff",
                   "highlight": {"background": "#ff9900" if is_anchor else color,
                                 "border": "#232f3e"}},
            shape="star" if is_anchor else shape,
            size=32 if is_anchor else 16,
            borderWidth=3 if is_anchor else 1,
            level=int(lvl),
            **extra,
        )

    # 边色 = AWS Cloudscape 图表状态色（status-positive / high / medium / neutral）
    for src, sl, tgt, tl, et, vs in edges_t:
        a, b = f"{sl}::{src}", f"{tl}::{tgt}"
        if a not in ids or b not in ids:
            continue
        if vs == "confirmed":
            net.add_edge(a, b, color="#67a353", width=3,
                         title=f"{et} · 故障注入已确认")
        elif vs == "refuted":
            net.add_edge(a, b, color="#ba2e0f", width=3, dashes=True,
                         title=f"{et} · 已证伪，不成立")
        elif vs == "inconclusive":
            net.add_edge(a, b, color="#cc5f21", width=2,
                         title=f"{et} · 未定")
        else:
            net.add_edge(a, b, color="#b4b4bb", width=1,
                         title=f"{et} · 未验证", label="")

    if use_layered:
        # 坐标已在 Python 侧算好（纵轴=依赖方向，横轴=scope 簇），
        # 所以**不启用 vis 的 hierarchical**：
        #   1. 它只有一个排序维度，装不下「方向 × scope」；
        #   2. 它会在节点上写死 fixed.y，害得节点只能左右拖 —— 不用它，
        #      这个毛病就从根上不存在了。
        opts = {
            "layout": {"hierarchical": {"enabled": False},
                       # 关掉随机种子扰动，保证同样的数据画出同样的图
                       "randomSeed": 7},
            "physics": {"enabled": False},
            # 竖直贝塞尔：依赖方向是纵向的，边也顺着纵向走，读起来才连贯。
            "edges": {"smooth": {"enabled": True, "type": "cubicBezier",
                                 "forceDirection": "vertical", "roundness": 0.5},
                      "arrows": {"to": {"enabled": True, "scaleFactor": 0.6}},
                      "font": {"size": 11, "face": "sans-serif", "color": "#5f6b7a",
                               "strokeWidth": 3, "strokeColor": "#ffffff",
                               "align": "middle"}},
            # dragView / zoomView 显式打开：默认虽为 true，但写明可避免被别处覆盖。
            # ⚠️ 「节点拖不动」不要从这里查 —— dragNodes 只管交互层的总开关，
            # 而分层布局是在每个节点上写 fixed.y/fixed.x 来锁轴的，这里配什么都
            # 覆盖不了。本页已改为自算坐标，不再启用 hierarchical，故无此问题。
            "interaction": {"hover": True, "tooltipDelay": 200,
                            "navigationButtons": True, "keyboard": False,
                            "dragView": True, "zoomView": True,
                            "dragNodes": True, "multiselect": False},
            "nodes": {"font": {"size": 13, "face": "sans-serif", "color": "#16191f",
                               "strokeWidth": 3, "strokeColor": "#ffffff"}},
        }
    else:
        opts = {
            "physics": {"barnesHut": {"gravitationalConstant": -9000,
                                      "centralGravity": 0.35, "springLength": 150},
                        "enabled": True},
            "interaction": {"hover": True, "navigationButtons": True,
                            "dragView": True, "zoomView": True},
            "edges": {"font": {"size": 11, "color": "#5f6b7a",
                               "strokeWidth": 3, "strokeColor": "#ffffff"}},
            "nodes": {"font": {"size": 13, "color": "#16191f",
                               "strokeWidth": 3, "strokeColor": "#ffffff"}},
        }
    net.set_options(json.dumps(opts))

    tmp = tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8")
    try:
        net.save_graph(tmp.name)
        with open(tmp.name, encoding="utf-8") as fh:
            html = fh.read()
    finally:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    # pyvis 不带 .vis-tooltip 的样式，浏览器于是用默认字号渲染 title——
    # 表现是一个巨大的白框（截图里那个盖住半张图的 "AccessesData"）。
    # 这里显式补样式，并在渲染完成后 fit() 一次，保证全图入画。
    patch = """
<style>
  html, body { margin:0; padding:0; overflow:hidden; }
  div.vis-tooltip {
    position:absolute; visibility:hidden; padding:6px 9px;
    white-space:pre; font-family:-apple-system,"Segoe UI",Roboto,sans-serif;
    font-size:12px; line-height:1.45; color:#16191f;
    background:#ffffff; border:1px solid #c6c6cd; border-radius:6px;
    box-shadow:0 2px 8px rgba(0,0,0,.16); pointer-events:none; z-index:9;
  }
  #mynetwork { border:1px solid #e9ebed !important; border-radius:8px; }
</style>
<script>window.__anchorId__ = __ANCHOR_ID__;</script>
<script>
  window.addEventListener('load', function () {
    if (typeof network === 'undefined') return;
    // fit() 会为了装下全图无限缩小，节点标签随之变成糊点。
    // 这里给缩放设可读下限：装不下就保持可读比例并对准锚点，
    // 剩下的靠平移看（dragView 已显式打开）。
    var MIN_SCALE = 0.75;
    function fitReadable() {
      try {
        network.fit({animation: false});
        if (network.getScale() < MIN_SCALE) {
          var focus = window.__anchorId__;
          if (focus && network.body.nodes[focus]) {
            network.moveTo({scale: MIN_SCALE,
                            position: network.getPositions([focus])[focus],
                            animation: false});
          } else {
            network.moveTo({scale: MIN_SCALE, animation: false});
          }
        }
      } catch (e) {}
    }
    // ── 解开分层布局在节点上写死的轴锁 ─────────────────────────────
    // vis-network 的 LayoutEngine 启用 hierarchical 时，会直接在每个节点上
    // 写 fixed.y=true（direction UD/DU）或 fixed.x=true（LR/RL），
    // 好让节点不脱离自己那一层。表现就是「只能左右拖，不能上下拖」。
    //
    // 这个锁 interaction.dragNodes 管不到 —— 它是布局引擎下在节点属性上的，
    // 不是交互开关，所以调 interaction 那一段永远无效（曾在此排查过）。
    //
    // 做法：等布局把坐标算完，取下坐标 → 关掉 hierarchical（否则它会持续
    // 重新加锁）→ 解开两个轴 → 把坐标显式写回。physics 本来就是关的，
    // 所以节点不会飘：分层外观完整保留，而四个方向都能拖了。
    function unlockNodeDragging() {
      try {
        if (typeof network === 'undefined') return;
        var lay = (network.options && network.options.layout) || {};
        var h = lay.hierarchical;
        var layered = (h === true) || (h && h.enabled === true);
        // 自由布局（physics 档）本来就四向可拖，别去碰它的 physics。
        if (!layered) return;

        var pos = network.getPositions();          // 分层算出的坐标
        network.setOptions({layout: {hierarchical: {enabled: false}},
                            physics: {enabled: false}});
        // 用 network.body.data.nodes 而不是全局 nodes：后者的变量名随
        // pyvis 模板版本变化，前者是 vis 自己持有的 DataSet，稳定。
        var ds = network.body.data.nodes;
        var upd = Object.keys(pos).map(function (id) {
          return {id: id, x: pos[id].x, y: pos[id].y,
                  fixed: {x: false, y: false}};
        });
        if (ds && upd.length) ds.update(upd);
      } catch (e) {}
    }

    network.once('afterDrawing', function () {
      unlockNodeDragging();   // 先解锁，再 fit：解锁不动坐标，fit 只调视野
      fitReadable();
    });
    // 分层布局下 physics 关闭，不触发 stabilized，故再兜底一次
    setTimeout(function () { unlockNodeDragging(); fitReadable(); }, 350);
  });
</script>
"""
    anchor_id = ""
    if anchor_name is not None:
        for label, name, *_rest in nodes_t:
            if name == anchor_name:
                anchor_id = f"{label}::{name}"
                break
    # json.dumps 负责转义，避免节点名里的引号破坏脚本
    patch = patch.replace("__ANCHOR_ID__", json.dumps(anchor_id or None))
    return html.replace("</head>", patch + "</head>", 1) if "</head>" in html else html + patch


html = build_html(
    tuple((lb, nm, levels.get((lb, nm), 9),
           nodes[(lb, nm)].get("band", "unknown"),
           positions.get((lb, nm), (0.0, 0.0))[0],
           positions.get((lb, nm), (0.0, 0.0))[1]) for (lb, nm) in nodes),
    tuple((e["source"], e.get("source_label"), e["target"], e.get("target_label"),
           e["edge_type"], e.get("verify_status", "")) for e in edges),
    auto_height, layered, show_labels, anchor,
    tuple(bands_meta),
)
C.embed_html(html, auto_height + 20)

lc1, lc2 = st.columns([3, 2])
lc1.caption(
    "🟢 绿粗线 = 故障注入**已确认**　🔴 红虚线 = 已**证伪**　🟠 橙线 = 未定　"
    "⚫ 灰细线 = 未验证。★ 橙色星 = 锚点。"
    "**上→下 = 依赖方向**（上游调用方在上、下游被依赖方在下）；"
    "**横向分簇 = `scope`**（左起：业务系统 → 外部服务 → 可观测性 → 平台自身 → "
    "集群底座），簇间留白就是业务与运维设施的分界。"
    "配色取自 AWS Cloudscape 图表令牌。滚轮缩放、空白处拖动平移、节点可上下左右任意拖动。"
)
lc2.caption("拖拽可重排；右下角有缩放按钮；悬停看类型与分组。")

# ── 图例：7 组 + 形状 ─────────────────────────────────────────────────────────
present_groups = {}
for n in nodes.values():
    gname, color, shape = group_of(n["label"])
    present_groups.setdefault(gname, {"color": color, "shape": shape, "types": set()})
    present_groups[gname]["types"].add(n["label"])

with st.expander(f"图例：{len(present_groups)} 个分组 / {len(nodes)} 个节点", expanded=False):
    st.caption(
        "颜色只编码**大类**（可区分上限约 7 类），形状与 hover 提示承载具体类型——"
        "39 种节点类型不可能各给一个能分辨的颜色。配色用 Okabe–Ito，色盲安全。"
    )
    for gname, info in present_groups.items():
        st.markdown(
            f"<span style='display:inline-block;width:13px;height:13px;border-radius:3px;"
            f"background:{info['color']};margin-right:8px'></span>"
            f"**{gname}**（{info['shape']}）　"
            + "　".join(f"`{t}`" for t in sorted(info["types"])),
            unsafe_allow_html=True,
        )

# ── 边明细 ────────────────────────────────────────────────────────────────────
with st.expander(f"关系明细（{len(edges)}）"):
    st.caption(
        f"契约把 {len(DEP_EDGES) + len(STRUCT_EDGES)} 种边分成 "
        f"**{len(DEP_EDGES)} 种依赖边**（`dependency: true`，参与验证）与 "
        f"**{len(STRUCT_EDGES)} 种结构边**（包含/承载关系）。"
        "默认只画依赖边——把包含边也画成箭头是杂乱的主要来源。"
    )
    st.dataframe(
        C.df([
            {
                "源": e["source"], "源类型": e.get("source_label"),
                "关系": e["edge_type"],
                "依赖边": "✅" if e["edge_type"] in DEP_EDGES else "结构",
                "目标": e["target"], "目标类型": e.get("target_label"),
                "验证": e.get("verify_status") or "untested",
                "数据源": e.get("edge_source") or "—",
            }
            for e in edges
        ]),
        width="stretch", hide_index=True,
    )

st.markdown("---")
st.caption(
    "**为什么默认不给全图**：这不是偷懒。88 节点 / 146 边看似不多，但平均度只有 3.3——"
    "力导向布局在这种稀疏典型图上会把方向与分层信息全丢掉，"
    "结果就是一片散点。10 个成熟依赖图产品（Datadog / Dynatrace / Bloom / "
    "graph-explorer / Kiali / Grafana …）没有一个默认渲染全图。"
    "详见 `todo/webui/05-图谱展示方案调研_20260905-0645.md`。"
)
