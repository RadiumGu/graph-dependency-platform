"""
app.py — Graph Dependency Platform Demo 主入口

架构概览 + 关键指标 + 导航
"""
import os
import sys

import streamlit as st

# ── 路径设置 ──────────────────────────────────────────────────────────────────
_DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_DEMO_DIR, ".."))
_RCA_ROOT = os.path.join(_PROJECT_ROOT, "rca")
_CHAOS_ROOT = os.path.join(_PROJECT_ROOT, "chaos", "code")
_DR_ROOT = os.path.join(_PROJECT_ROOT, "dr-plan-generator")

for _p in [_PROJECT_ROOT, _RCA_ROOT, _CHAOS_ROOT, _DR_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ── 环境变量默认值 ────────────────────────────────────────────────────────────
os.environ.setdefault(
    "NEPTUNE_ENDPOINT",
    "petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com",
)
os.environ.setdefault("REGION", "ap-northeast-1")
os.environ.setdefault("BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6")

# ── 页面配置（必须最先调用）──────────────────────────────────────────────────
st.set_page_config(
    page_title="Graph Dependency Platform",
    page_icon="🕸️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── 侧边栏 ────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.image(
        "https://img.shields.io/badge/Neptune-openCypher-blue?logo=amazon-aws",
        use_container_width=False,
    )
    st.markdown("## 导航")
    st.markdown(
        """
- 🏠 **首页** ← 当前
- [🕸️ Graph Explorer](Graph_Explorer)
- [💬 Smart Query](Smart_Query)
- [🔍 根因分析 RCA](Root_Cause_Analysis)
- [💥 混沌工程](Chaos_Engineering)
- [🛡️ DR 计划](DR_Plan)
"""
    )
    st.markdown("---")
    st.caption("PetSite · ap-northeast-1 · Neptune openCypher")

# ── 主标题 ────────────────────────────────────────────────────────────────────
st.title("🕸️ Graph Dependency Platform")
st.markdown(
    "**基于图谱的微服务依赖管理与智能运维平台** — "
    "将 Neptune 图谱、Bedrock AI 与混沌工程融合，实现全链路可观测与自动化恢复。"
)

# ── 关键指标 ──────────────────────────────────────────────────────────────────
st.markdown("---")
col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("图节点", "171+", "22 种类型")
col2.metric("边类型", "19 种", "拓扑/调用/依赖")
col3.metric("查询能力", "18 个", "Q1–Q18")
col4.metric("微服务", "7 个", "PetSite 应用")
col5.metric("AI 模型", "Claude Sonnet 4.6", "Bedrock")

# ── 平台功能矩阵 ──────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("平台功能")

left, right = st.columns([3, 2])

with left:
    st.markdown(
        """
| 模块 | 功能描述 | 技术 |
|------|----------|------|
| 🕸️ **Graph Explorer** | Neptune 拓扑可视化，交互式节点探索 | pyvis + openCypher |
| 💬 **Smart Query** | 自然语言 → openCypher，AI 生成查询 | Bedrock Claude + NL Engine |
| 🔍 **RCA 分析** | Graph RAG 根因分析，历史故障上下文 | Graph RAG + S3 Vectors |
| 💥 **混沌工程** | 实验历史浏览，服务韧性评估 | FIS + Neptune 同步 |
| 🛡️ **DR 计划** | 灾备切换计划生成，RTO/RPO 评估 | Graph Analyzer + Bedrock |
"""
    )

with right:
    st.info(
        """
**技术栈**

- **图数据库**: Amazon Neptune (openCypher)
- **AI**: Amazon Bedrock Claude Sonnet 4.6
- **语义搜索**: AWS S3 Vectors
- **可观测性**: DeepFlow + CloudWatch
- **微服务**: AWS EKS (PetSite)
- **混沌**: AWS FIS (Fault Injection Simulator)
"""
    )

# ── 架构图 ────────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("系统架构")

st.markdown(
    """
```
                    ┌─────────────────────────────────────────────────────┐
                    │              Graph Dependency Platform              │
                    └──────────────────────────┬──────────────────────────┘
                                               │
              ┌────────────────┬───────────────┼───────────────┬────────────────┐
              ▼                ▼               ▼               ▼                ▼
    ┌──────────────┐  ┌──────────────┐ ┌─────────────┐ ┌──────────────┐ ┌────────────┐
    │  Graph       │  │  Smart       │ │  RCA 分析   │ │  混沌工程    │ │  DR 计划   │
    │  Explorer    │  │  Query       │ │  Graph RAG  │ │  FIS + Graph │ │  Generator │
    └──────┬───────┘  └──────┬───────┘ └──────┬──────┘ └──────┬───────┘ └─────┬──────┘
           │                 │                │               │               │
           └─────────────────┴────────────────┴───────────────┴───────────────┘
                                               │
                    ┌──────────────────────────▼──────────────────────────┐
                    │               Amazon Neptune Graph DB               │
                    │        171+ 节点 · 19 种边 · openCypher API        │
                    └─────────────────────────────────────────────────────┘
                                               │
              ┌────────────────┬───────────────┼───────────────┬────────────────┐
              ▼                ▼               ▼               ▼                ▼
    ┌──────────────┐  ┌──────────────┐ ┌─────────────┐ ┌──────────────┐ ┌────────────┐
    │  EKS Cluster │  │  Bedrock     │ │  DeepFlow   │ │  S3 Vectors  │ │  DynamoDB  │
    │  (PetSite)   │  │  Claude 4.6  │ │  观测平台   │ │  语义搜索    │ │  / RDS     │
    └──────────────┘  └──────────────┘ └─────────────┘ └──────────────┘ └────────────┘
```
"""
)

# ── 图谱 Schema 概览 ──────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("图谱 Schema 概览")

tab1, tab2 = st.tabs(["节点类型（22 种）", "边类型（19 种）"])

with tab1:
    cols = st.columns(3)
    node_groups = {
        "基础设施层": ["Region", "AvailabilityZone", "VPC", "Subnet", "SecurityGroup"],
        "计算层": ["EC2Instance", "EKSCluster", "Pod", "Microservice"],
        "网络层": ["ALB", "TargetGroup"],
        "数据层": ["RDSCluster", "DynamoDB", "Neptune", "S3"],
        "消息层": ["SQS", "SNS"],
        "Serverless": ["Lambda", "StepFunction"],
        "业务/运维层": ["BusinessCapability", "Incident", "ChaosExperiment"],
    }
    items = list(node_groups.items())
    for i, (grp, nodes) in enumerate(items):
        with cols[i % 3]:
            st.markdown(f"**{grp}**")
            for n in nodes:
                st.markdown(f"- `{n}`")

with tab2:
    edges = [
        ("EC2Instance", "LocatedIn", "AvailabilityZone", "拓扑"),
        ("Subnet", "BelongsTo", "VPC", "拓扑"),
        ("EC2Instance", "BelongsTo", "EKSCluster", "拓扑"),
        ("Microservice", "RunsOn", "Pod", "运行"),
        ("Pod", "RunsOn", "EC2Instance", "运行"),
        ("ALB", "RoutesTo", "TargetGroup", "网络"),
        ("TargetGroup", "ForwardsTo", "Microservice", "网络"),
        ("Microservice", "Calls", "Microservice", "调用"),
        ("Microservice", "DependsOn", "DynamoDB/RDS/SQS/S3", "依赖"),
        ("Lambda", "Invokes", "Lambda/StepFunction", "调用"),
        ("Microservice", "WritesTo", "SQS/SNS/S3", "数据"),
        ("BusinessCapability", "Serves", "Microservice", "业务"),
        ("Microservice", "Implements", "BusinessCapability", "业务"),
        ("Incident", "TriggeredBy", "Microservice", "运维"),
        ("Incident", "MentionsResource", "任意节点", "运维 (Phase A)"),
        ("Microservice", "TestedBy", "ChaosExperiment", "运维 (Phase A)"),
    ]
    import pandas as pd

    st.dataframe(
        pd.DataFrame(edges, columns=["源节点", "边类型", "目标节点", "分类"]),
        use_container_width=True,
        hide_index=True,
    )

# ── 底部 ──────────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption(
    "Graph Dependency Platform · AWS ap-northeast-1 · "
    "Account 926093770964 · Neptune openCypher · Bedrock Claude Sonnet 4.6"
)
