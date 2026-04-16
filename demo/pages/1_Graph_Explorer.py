"""
Graph Explorer — Neptune 拓扑可视化

交互式图谱浏览：选择节点类型 + 深度，用 pyvis 渲染。
"""
import json
import os
import sys
import tempfile

import streamlit as st
import streamlit.components.v1 as components

# ── 路径设置 ──────────────────────────────────────────────────────────────────
_DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_DEMO_DIR, "..", ".."))
_RCA_ROOT = os.path.join(_PROJECT_ROOT, "rca")

for _p in [_PROJECT_ROOT, _RCA_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault(
    "NEPTUNE_ENDPOINT",
    "petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com",
)
os.environ.setdefault("REGION", "ap-northeast-1")

st.set_page_config(page_title="Graph Explorer", page_icon="🕸️", layout="wide")

# ── 节点颜色方案 ──────────────────────────────────────────────────────────────
NODE_COLORS: dict[str, str] = {
    "Microservice":       "#4C9BE8",
    "Pod":                "#82C4F8",
    "EC2Instance":        "#F4A261",
    "EKSCluster":         "#E76F51",
    "AvailabilityZone":   "#2A9D8F",
    "RDSCluster":         "#E9C46A",
    "DynamoDB":           "#F4E285",
    "Lambda":             "#A8DADC",
    "SQS":                "#C77DFF",
    "SNS":                "#9D4EDD",
    "ALB":                "#06D6A0",
    "TargetGroup":        "#1B998B",
    "BusinessCapability": "#FF6B6B",
    "Incident":           "#EF233C",
    "ChaosExperiment":    "#B5179E",
    "S3":                 "#FFB703",
    "StepFunction":       "#8338EC",
    "VPC":                "#3A86FF",
    "Subnet":             "#90E0EF",
    "Neptune":            "#48CAE4",
    "SecurityGroup":      "#ADE8F4",
    "Region":             "#023E8A",
}
DEFAULT_COLOR = "#AAAAAA"


# ── Neptune 쿼리 helpers ──────────────────────────────────────────────────────
@st.cache_data(ttl=60, show_spinner=False)
def fetch_graph_data(node_types: tuple[str, ...], max_nodes: int = 150) -> dict:
    """Neptune에서 노드+엣지를 가져온다. 실패시 빈 dict 반환."""
    try:
        from neptune import neptune_client as nc

        type_filter = " OR ".join(f"n:{t}" for t in node_types)

        nodes_cypher = f"""
        MATCH (n)
        WHERE {type_filter}
        RETURN n.name AS name, labels(n)[0] AS label,
               n.recovery_priority AS priority, n.status AS status,
               n.state AS state, n.tier AS tier
        LIMIT {max_nodes}
        """
        raw_nodes = nc.results(nodes_cypher)

        # 收集名称集合
        names = {r["name"] for r in raw_nodes if r.get("name")}

        edges_cypher = """
        MATCH (a)-[r]->(b)
        WHERE a.name IN $names AND b.name IN $names
        RETURN a.name AS src, b.name AS dst, type(r) AS rel
        LIMIT 500
        """
        raw_edges = nc.results(edges_cypher, {"names": list(names)})

        return {"nodes": raw_nodes, "edges": raw_edges, "error": None}
    except Exception as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}


def build_pyvis_html(nodes: list, edges: list, height: int = 600) -> str:
    """将节点/边数据构建为 pyvis HTML 字符串。"""
    from pyvis.network import Network

    net = Network(height=f"{height}px", width="100%", directed=True, notebook=False)
    net.set_options(
        json.dumps(
            {
                "physics": {
                    "enabled": True,
                    "stabilization": {"iterations": 100},
                    "barnesHut": {
                        "gravitationalConstant": -8000,
                        "springLength": 120,
                        "springConstant": 0.04,
                    },
                },
                "nodes": {
                    "font": {"size": 12, "face": "Arial"},
                    "borderWidth": 2,
                    "shadow": True,
                },
                "edges": {
                    "arrows": {"to": {"enabled": True, "scaleFactor": 0.8}},
                    "font": {"size": 10, "align": "middle"},
                    "smooth": {"type": "curvedCW", "roundness": 0.2},
                },
                "interaction": {"hover": True, "tooltipDelay": 200},
            }
        )
    )

    added_nodes: set[str] = set()
    for n in nodes:
        name = n.get("name") or ""
        if not name or name in added_nodes:
            continue
        label = n.get("label", "Unknown")
        color = NODE_COLORS.get(label, DEFAULT_COLOR)
        priority = n.get("priority") or ""
        status = n.get("status") or n.get("state") or ""
        title = f"<b>{name}</b><br>类型: {label}"
        if priority:
            title += f"<br>优先级: {priority}"
        if status:
            title += f"<br>状态: {status}"
        net.add_node(name, label=name, color=color, title=title, size=20)
        added_nodes.add(name)

    for e in edges:
        src, dst, rel = e.get("src"), e.get("dst"), e.get("rel", "")
        if src in added_nodes and dst in added_nodes:
            net.add_edge(src, dst, label=rel, title=rel)

    # 写到临时文件再读回
    with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w") as f:
        net.save_graph(f.name)
        tmp_path = f.name

    with open(tmp_path, "r", encoding="utf-8") as f:
        html = f.read()
    os.unlink(tmp_path)
    return html


# ── Streamlit UI ──────────────────────────────────────────────────────────────
st.title("🕸️ Graph Explorer")
st.markdown("交互式 Neptune 图谱浏览 — 选择节点类型，探索微服务拓扑。")

# 侧边栏控件
with st.sidebar:
    st.header("筛选条件")
    selected_types = st.multiselect(
        "节点类型",
        options=list(NODE_COLORS.keys()),
        default=["Microservice", "Pod", "EC2Instance", "RDSCluster", "DynamoDB", "SQS", "ALB"],
    )
    max_nodes = st.slider("最多节点数", 20, 300, 150, 10)
    graph_height = st.slider("图高度 (px)", 400, 900, 620, 50)
    refresh = st.button("🔄 刷新图谱", use_container_width=True)
    if refresh:
        st.cache_data.clear()

    st.markdown("---")
    st.markdown("**节点颜色图例**")
    for node_type, color in list(NODE_COLORS.items())[:12]:
        st.markdown(
            f'<span style="background:{color};border-radius:4px;'
            f'padding:2px 8px;color:#fff;font-size:11px">{node_type}</span>',
            unsafe_allow_html=True,
        )

if not selected_types:
    st.warning("请至少选择一种节点类型。")
    st.stop()

# 拉取数据
with st.spinner("正在从 Neptune 获取图谱数据…"):
    data = fetch_graph_data(tuple(sorted(selected_types)), max_nodes)

if data["error"]:
    st.error(f"Neptune 连接失败：{data['error']}")
    st.info(
        "请确认：\n"
        "1. AWS 凭证已配置（`aws configure` 或 IAM Role）\n"
        "2. 环境变量 `NEPTUNE_ENDPOINT` 正确\n"
        "3. 当前网络可访问 Neptune（需要在 VPC 内或通过隧道）"
    )
    st.stop()

nodes, edges = data["nodes"], data["edges"]

if not nodes:
    st.warning("查询返回空结果，请检查节点类型选择或 Neptune 数据。")
    st.stop()

# 统计信息
c1, c2, c3 = st.columns(3)
c1.metric("节点数", len(nodes))
c2.metric("边数", len(edges))
label_counts: dict[str, int] = {}
for n in nodes:
    lbl = n.get("label", "Unknown")
    label_counts[lbl] = label_counts.get(lbl, 0) + 1
c3.metric("节点类型", len(label_counts))

# 渲染图
with st.spinner("渲染交互图谱…"):
    html_content = build_pyvis_html(nodes, edges, height=graph_height)

components.html(html_content, height=graph_height + 20, scrolling=False)

# 节点详情表
with st.expander("节点详情列表", expanded=False):
    import pandas as pd

    df = pd.DataFrame(nodes)
    if not df.empty:
        st.dataframe(df, use_container_width=True, hide_index=True)

# 边列表
with st.expander("边列表", expanded=False):
    import pandas as pd

    df_e = pd.DataFrame(edges)
    if not df_e.empty:
        st.dataframe(df_e, use_container_width=True, hide_index=True)

# 服务详情查询
st.markdown("---")
st.subheader("节点详情查询")
svc_name = st.text_input("输入节点名称（精确）", placeholder="如: petsite")
if svc_name:
    try:
        from neptune import neptune_client as nc

        detail_cypher = """
        MATCH (n {name: $name})
        OPTIONAL MATCH (n)-[r]->(neighbor)
        RETURN n.name AS name, labels(n)[0] AS type,
               type(r) AS rel,
               COALESCE(neighbor.name, neighbor.experiment_id, neighbor.service_name, id(neighbor)) AS neighbor,
               labels(neighbor)[0] AS neighbor_type
        LIMIT 50
        """
        rows = nc.results(detail_cypher, {"name": svc_name})
        if rows:
            props_row = rows[0]
            st.markdown(f"**节点**: `{props_row.get('name')}` — 类型: `{props_row.get('type')}`")
            import pandas as pd

            df_d = pd.DataFrame(
                [{"关系": r.get("rel"), "目标节点": r.get("neighbor"), "目标类型": r.get("neighbor_type")}
                 for r in rows if r.get("rel")]
            )
            if not df_d.empty:
                st.dataframe(df_d, use_container_width=True, hide_index=True)
            else:
                st.info("该节点无出边。")
        else:
            st.warning(f"未找到节点: {svc_name}")
    except Exception as exc:
        st.error(f"查询失败: {exc}")
