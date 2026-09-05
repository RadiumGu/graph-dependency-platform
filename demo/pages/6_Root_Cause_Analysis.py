"""
Root Cause Analysis — Graph RAG 根因分析

选择故障服务，触发 RCA 流程，展示 Graph RAG 报告。
"""
import os
import sys
import time

import streamlit as st

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
os.environ.setdefault("BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6")

st.set_page_config(page_title="根因分析 RCA", page_icon="🔍", layout="wide")

# ── PetSite 已知服务 ──────────────────────────────────────────────────────────
KNOWN_SERVICES = [
    "petsite",
    "petsearch",
    "payforadoption",
    "petlistadoptions",
    "petadoptionshistory",
    "petfood",
    "trafficgenerator",
]

SEVERITY_COLORS = {
    "P0": "🔴",
    "P1": "🟠",
    "P2": "🟡",
    "P3": "🟢",
}


def run_graph_rag_report(service: str, severity: str, alert_msg: str) -> str:
    """调用 graph_rag_reporter 生成报告。"""
    from core.graph_rag_reporter import generate_rca_report
    classification = {
        "severity": severity,
        "alert_message": alert_msg,
    }
    rca_result = {
        "error_services": [],
        "recent_changes": [],
        "root_cause_candidates": [],
        "aws_probe_results": [],
    }
    return generate_rca_report(
        affected_service=service,
        classification=classification,
        rca_result=rca_result,
    )


def _render_rca_report(report):
    """将 generate_rca_report 返回的 dict 渲染为可读的 Streamlit UI。"""
    if isinstance(report, str):
        st.markdown(report)
        return
    if not isinstance(report, dict):
        st.code(str(report))
        return

    root_cause = report.get("root_cause", "未知")
    confidence = report.get("confidence", 0)
    reasoning = report.get("reasoning", "")
    recommended = report.get("recommended_action", "")
    blast_radius = report.get("blast_radius", "")
    evidence = report.get("evidence", [])
    breakdown = report.get("confidence_breakdown", {})
    source = report.get("source", "")

    # ── 置信度进度条颜色 ──
    if confidence >= 70:
        conf_color = "🟢"
    elif confidence >= 40:
        conf_color = "🟡"
    else:
        conf_color = "🔴"

    # ── 根因 ──
    st.subheader("🎯 根因判断")
    st.info(root_cause)

    # ── 置信度 ──
    st.markdown(f"**置信度** {conf_color} {confidence}%")
    st.progress(int(confidence) / 100)

    if breakdown and isinstance(breakdown, dict):
        bd_cols = st.columns(len(breakdown))
        for col, (k, v) in zip(bd_cols, breakdown.items()):
            col.metric(k, f"{v}分")

    st.markdown("")

    # ── 推理过程 ──
    if reasoning:
        with st.expander("🧠 推理过程", expanded=False):
            st.markdown(reasoning)

    # ── 证据列表 ──
    if evidence:
        with st.expander("📎 支撑证据", expanded=True):
            for ev in evidence:
                st.markdown(f"- {ev}")

    # ── 推荐操作 ──
    if recommended:
        st.subheader("🔧 推荐操作")
        # 可能是带序号的多行文本，按句号/分号拆分
        steps = [s.strip() for s in recommended.replace("；", ";").replace("。", ".").split(";") if s.strip()]
        if len(steps) > 1:
            for i, step in enumerate(steps, 1):
                st.markdown(f"**{i}.** {step}")
        else:
            st.markdown(recommended)

    # ── 影响面 ──
    if blast_radius:
        st.subheader("💥 影响面")
        st.warning(blast_radius)

    # ── 来源 ──
    if source:
        st.caption(f"数据来源：{source}")


def fetch_blast_radius(service: str) -> dict:
    """Q1 查询影响面。"""
    try:
        from neptune import neptune_queries as nq
        return nq.q1_blast_radius(service)
    except Exception as exc:
        return {"services": [], "capabilities": [], "error": str(exc)}


def fetch_similar_incidents(service: str) -> list:
    """Q5 历史故障。"""
    try:
        from neptune import neptune_queries as nq
        return nq.q5_similar_incidents(service, limit=5)
    except Exception as exc:
        return []


def fetch_pod_status(service: str) -> list:
    """Q6 Pod 状态。"""
    try:
        from neptune import neptune_queries as nq
        return nq.q6_pod_status(service)
    except Exception as exc:
        return []


def fetch_infra_path(service: str) -> list:
    """Q9 基础设施路径。"""
    try:
        from neptune import neptune_queries as nq
        return nq.q9_service_infra_path(service)
    except Exception as exc:
        return []


# ── UI ────────────────────────────────────────────────────────────────────────
st.title("🔍 根因分析 (RCA)")
st.markdown(
    "基于图谱的 Graph RAG 根因分析 — 融合 Neptune 拓扑、DeepFlow 调用链与 CloudWatch 指标，"
    "由 Bedrock Claude 生成结构化故障报告。"
)

with st.sidebar:
    st.header("RCA 配置")
    selected_service = st.selectbox(
        "故障服务", KNOWN_SERVICES, index=0
    )
    severity = st.selectbox("告警级别", ["P0", "P1", "P2", "P3"], index=1)
    alert_message = st.text_area(
        "告警描述",
        value=f"{selected_service} 服务出现异常，延迟升高，错误率超阈值。",
        height=80,
    )
    run_rca = st.button("🚀 发起 RCA 分析", use_container_width=True, type="primary")

    st.markdown("---")
    st.markdown("**分析流程**")
    st.markdown(
        """
1. Q9 基础设施路径（Pod→EC2→AZ）
2. Q10 基础设施根因探测
3. Q1 影响面评估（Blast Radius）
4. Q3 上游依赖查询
5. Q5 历史故障检索
6. Q17 同资源历史 Incident
7. 语义搜索（S3 Vectors）
8. Bedrock Claude 生成报告
"""
    )

# ── 主内容：快速指标 ──────────────────────────────────────────────────────────
tab_overview, tab_rca, tab_history = st.tabs(["服务概览", "RCA 报告", "历史故障"])

with tab_overview:
    st.subheader(f"服务概览：{selected_service}")
    col1, col2 = st.columns(2)

    with col1:
        st.markdown("**影响面评估 (Q1)**")
        with st.spinner("查询中…"):
            blast = fetch_blast_radius(selected_service)
        if blast.get("error"):
            st.error(f"查询失败: {blast['error']}")
        else:
            services = blast.get("services", [])
            caps = blast.get("capabilities", [])
            st.metric("受影响下游服务", len(services))
            st.metric("受影响业务能力", len(caps))
            if services:
                import pandas as pd
                st.dataframe(
                    pd.DataFrame(services)[["name", "type", "priority"] if "priority" in services[0] else ["name", "type"]],
                    use_container_width=True,
                    hide_index=True,
                )

    with col2:
        st.markdown("**基础设施路径 (Q9)**")
        with st.spinner("查询中…"):
            infra = fetch_infra_path(selected_service)
        if not infra:
            st.info("无基础设施路径数据（Neptune 可能未写入）")
        else:
            import pandas as pd
            df_infra = pd.DataFrame(infra)
            # 标记异常 EC2
            if "ec2_state" in df_infra.columns:
                df_infra["健康"] = df_infra["ec2_state"].apply(
                    lambda s: "✅ 正常" if s == "running" else f"⚠️ {s}"
                )
            st.dataframe(df_infra, use_container_width=True, hide_index=True)

    st.markdown("**Pod 状态 (Q6)**")
    with st.spinner("查询中…"):
        pods = fetch_pod_status(selected_service)
    if not pods:
        st.info("无 Pod 状态数据")
    else:
        import pandas as pd
        df_pods = pd.DataFrame(pods)
        st.dataframe(df_pods, use_container_width=True, hide_index=True)

with tab_rca:
    st.subheader("Graph RAG RCA 报告")

    if "rca_report" not in st.session_state:
        st.session_state["rca_report"] = {}

    if run_rca:
        progress = st.progress(0, text="初始化 RCA 流程…")
        status_box = st.empty()

        steps = [
            (10, "从 Neptune 提取服务拓扑子图…"),
            (25, "查询基础设施层（Pod→EC2→AZ）…"),
            (40, "评估影响面（Blast Radius）…"),
            (55, "检索历史故障知识库…"),
            (70, "调用 Bedrock Claude 生成分析…"),
            (90, "整理报告结构…"),
        ]

        for pct, msg in steps:
            progress.progress(pct, text=msg)
            status_box.info(f"⏳ {msg}")
            time.sleep(0.3)

        try:
            report = run_graph_rag_report(selected_service, severity, alert_message)
            progress.progress(100, text="RCA 完成！")
            status_box.success("✅ RCA 报告生成完成")
            st.session_state["rca_report"][selected_service] = {
                "report": report,
                "severity": severity,
                "service": selected_service,
            }
        except Exception as exc:
            progress.progress(100, text="RCA 失败")
            status_box.error(f"RCA 执行失败：{exc}")
            st.session_state["rca_report"][selected_service] = {
                "report": None,
                "error": str(exc),
                "service": selected_service,
            }

    cached = st.session_state["rca_report"].get(selected_service)
    if cached:
        if cached.get("error"):
            st.error(f"RCA 执行失败：{cached['error']}")
            st.info(
                "常见原因：\n"
                "- Neptune / Bedrock 访问权限不足\n"
                "- VPC 内网访问限制\n"
                "- AWS 凭证未配置"
            )
        elif cached.get("report"):
            sev_icon = SEVERITY_COLORS.get(cached.get("severity", "P1"), "⚪")
            st.markdown(f"**服务**: `{cached['service']}` | **级别**: {sev_icon} {cached.get('severity')}")
            st.markdown("---")
            _render_rca_report(cached["report"])
    else:
        st.info(
            "点击左侧「🚀 发起 RCA 分析」按钮开始分析。\n\n"
            "RCA 流程将：\n"
            "- 从 Neptune 提取服务拓扑\n"
            "- 融合基础设施状态\n"
            "- 检索历史故障（图查询 + 语义搜索）\n"
            "- 由 Bedrock Claude 生成根因报告"
        )

with tab_history:
    st.subheader(f"历史故障记录：{selected_service}")
    with st.spinner("查询历史故障…"):
        incidents = fetch_similar_incidents(selected_service)

    if not incidents:
        st.info("暂无历史故障记录（Neptune 中无 Incident 节点或无相关边）")
    else:
        import pandas as pd

        for inc in incidents:
            sev = inc.get("severity", "?")
            icon = SEVERITY_COLORS.get(sev, "⚪")
            with st.expander(
                f"{icon} {inc.get('id', '?')} — {sev} — {inc.get('start_time', '')}",
                expanded=False,
            ):
                col_a, col_b = st.columns(2)
                with col_a:
                    st.markdown(f"**根因**: {inc.get('root_cause', '未知')}")
                with col_b:
                    st.markdown(f"**解决方案**: {inc.get('resolution', '未知')}")
                if inc.get("mttr"):
                    st.markdown(f"**MTTR**: {inc.get('mttr')} 分钟")

    # Q17：通过 MentionsResource 边查相关 Incident
    st.markdown("---")
    st.markdown("**同资源历史 Incident (Q17)**")
    try:
        from neptune import neptune_queries as nq
        q17_results = nq.q17_incidents_by_resource(selected_service, limit=5)
        if q17_results:
            import pandas as pd
            st.dataframe(pd.DataFrame(q17_results), use_container_width=True, hide_index=True)
        else:
            st.info("无同资源历史 Incident。")
    except Exception as exc:
        st.error(f"Q17 查询失败: {exc}")
