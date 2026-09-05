"""
6_Root_Cause_Analysis.py — Graph RAG 根因分析。

改造要点（2026-09-05）：

1. **原实现是空跑。** `run_graph_rag_report` 把 `rca_result` 的四个字段全传
   空列表（error_services / recent_changes / root_cause_candidates /
   aws_probe_results），也就是说侧栏宣传的「8 步分析流程」**根本没执行**，
   报告是在零证据下生成的。进度条则是 6 个 `time.sleep(0.3)` 的表演。
   现在真的执行 8 条图查询收集证据，进度由真实执行回调驱动。

2. **证据本身要给观众看。** 采集到的证据（影响面、上游依赖、Pod 状态、
   注入验证判定）全是**纯图查询**、不需要 Bedrock——这一页的多数价值
   反而是可以离线呈现的。原来它们只被塞进 prompt，界面上看不到。

3. **服务清单从图谱现取。** 原来硬编码 7 个，且其中 `petadoptionshistory`
   与图谱/混沌目录用的 `pethistory` 不是同一个名字。实测图谱里有 20 个
   Microservice 节点、15 个不同名字。

4. **侧栏流程与实际执行对齐**，不再宣传没跑的步骤。

5. 报告生成需要 Bedrock，离线时明确说明，**不预置假报告**。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _common as C  # noqa: E402

import streamlit as st  # noqa: E402

os.environ.setdefault("BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6")

C.page_setup("根因分析", icon="🔍")
C.sidebar()

ONLINE = C.neptune_online()
SEVERITY_COLORS = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "P3": "🟢"}

# ── 证据链定义：这是**实际会执行**的清单，侧栏据此渲染，不再手写 ────────────
# 每项 = (查询名, 说明, 参数映射)。参数值为 None 表示填入所选服务名。
EVIDENCE_CHAIN = [
    ("q9_service_infra_path", "基础设施路径 Service→Pod→EC2→AZ",
     {"service_name": None}),
    ("q1_blast_radius", "下游影响面（谁会被连带影响）",
     {"failed_node": None, "kind": "live"}),
    ("q3_upstream_deps", "上游调用者 —— 根因候选",
     {"failed_service": None, "kind": "live"}),
    ("q6_pod_status", "Pod 状态与重启次数",
     {"service_name": None}),
    ("q10_infra_root_cause", "基础设施侧反查（非 running 的 EC2）",
     {"affected_service": None}),
    ("q19_topology_changes", "近期依赖变更事件",
     {"service_name": None}),
    ("q17_incidents_by_resource", "同资源历史 Incident",
     {"resource_name": None}),
    ("q5_similar_incidents", "同服务历史已解决故障",
     {"service_name": None, "limit": 3}),
    ("q22_edge_verification_verdicts", "该服务依赖边的注入验证判定",
     {"service_name": None, "limit": 40}),
]

st.title("🔍 根因分析")
st.markdown(
    "> 先用图查询把**事实**收齐，再交给模型写叙述。"
    "这一页刻意把收到的证据摆出来——报告可信不可信，取决于它建立在什么之上。"
)

# ── 服务清单（从图谱现取）────────────────────────────────────────────────────
svc_rows, svc_mode = C.services()
svc_names = C.service_names()

if not svc_names:
    st.error("未能取得服务清单（图谱不可达且无离线快照）。")
    st.stop()

# ── 侧栏配置 ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("---")
    st.markdown("### 分析配置")
    selected_service = st.selectbox("故障服务", svc_names, index=0)
    severity = st.selectbox("告警级别", list(SEVERITY_COLORS), index=1)
    alert_message = st.text_area(
        "告警描述",
        value=f"{selected_service} 服务出现异常，延迟升高，错误率超阈值。",
        height=80,
    )
    run_rca = st.button(
        "🚀 生成 RCA 报告", width="stretch", type="primary",
        disabled=not ONLINE,
        help="需要 Neptune（取证据）与 Bedrock（写叙述）" if ONLINE
             else "离线模式下无法生成报告，但下方证据面板仍可查看",
    )

    st.markdown("---")
    st.markdown("### 实际执行的证据链")
    st.caption(
        f"下面 {len(EVIDENCE_CHAIN)} 条查询会**真的执行**，"
        "然后连同结果一起交给 Bedrock："
    )
    for i, (qname, why, _) in enumerate(EVIDENCE_CHAIN, 1):
        st.caption(f"{i}. `{qname}` — {why}")
    st.caption(f"{len(EVIDENCE_CHAIN) + 1}. Bedrock 生成叙述")
    st.caption(
        "⚠️ 改造前这里列的是 8 步（含 Q10/Q17/S3 Vectors 语义搜索），"
        "但代码传给报告生成器的证据字段全是空列表——宣传的流程没有执行。"
    )

svc_meta = [r for r in svc_rows if r.get("name") == selected_service]

# ── 服务概览 ──────────────────────────────────────────────────────────────────
st.markdown("---")
head = st.columns([2, 1, 1, 1])
head[0].markdown(f"### {SEVERITY_COLORS[severity]} {selected_service}")
if svc_meta:
    tiers = {r.get("tier") for r in svc_meta if r.get("tier")}
    azs = [r.get("az") for r in svc_meta if r.get("az")]
    head[1].metric("Tier", "/".join(sorted(tiers)) if tiers else "—")
    head[2].metric("AZ 分布", len(azs), "、".join(azs) if len(azs) <= 2 else None)
    head[3].metric("图节点数", len(svc_meta), "同名跨 AZ")
C.mode_badge(svc_mode, "服务清单")


# ── 证据采集 ──────────────────────────────────────────────────────────────────
def gather_evidence(service: str, on_step=None) -> dict:
    """
    执行证据链。返回 {查询名: {why, params, data|error}}。
    纯图查询，不碰 Bedrock —— 所以这部分离线也能用快照展示。
    """
    from neptune.query_catalog import run_query  # type: ignore

    out: dict = {}
    total = len(EVIDENCE_CHAIN)
    for i, (qname, why, pmap) in enumerate(EVIDENCE_CHAIN, 1):
        kw = {k: (service if v is None else v) for k, v in pmap.items()}
        if on_step:
            on_step(int(i / (total + 1) * 100), f"{qname} — {why}")
        try:
            r = run_query(qname, **kw)
            rows = r.get("results", r) if isinstance(r, dict) else r
            out[qname] = {"why": why, "params": kw, "data": rows}
        except Exception as exc:  # noqa: BLE001
            out[qname] = {"why": why, "params": kw, "error": f"{type(exc).__name__}: {exc}"}
    return out


def evidence_for_display(service: str) -> tuple[dict, str]:
    """在线现查；离线取快照（只有部分服务有快照）。"""
    if ONLINE:
        return gather_evidence(service), "live"
    snap = C.fixture("rca_evidence").get("by_service", {})
    if service in snap:
        return snap[service], "snapshot"
    return {}, "none"


def _render_one_evidence(qname: str, item: dict) -> None:
    data = item.get("data")
    with st.container(border=True):
        top = st.columns([3, 1])
        top[0].markdown(f"**`{qname}`** — {item.get('why', '')}")

        if item.get("error"):
            top[1].markdown("❌ 查询失败")
            st.caption(f"错误：{item['error']}")
            st.caption("⚠️ 查询失败不等于「该依赖不存在」——这是取数失败，不是事实。")
            return

        if isinstance(data, list):
            top[1].markdown(f"**{len(data)}** 行")
            if not data:
                st.caption("返回 0 行。空结果不等于错误——例如前端服务本来就没有上游调用者。")
            else:
                st.dataframe(C.df(data), width="stretch", hide_index=True)
                # 注入验证判定单独给个状态摘要，它是最该被看到的
                if qname == "q22_edge_verification_verdicts":
                    counts: dict = {}
                    for r in data:
                        k = r.get("verify_status") or "untested"
                        counts[k] = counts.get(k, 0) + 1
                    C.status_chips(counts, sum(counts.values()))
        elif isinstance(data, dict):
            top[1].markdown("对象")
            st.json(data, expanded=False)
        else:
            top[1].markdown("单值")
            st.code(str(data))
        st.caption(f"参数　`{item.get('params')}`")


# ── 主体 ──────────────────────────────────────────────────────────────────────
tab_evidence, tab_report = st.tabs(["📋 证据面板（不需要 AI）", "📝 RCA 报告（需要 Bedrock）"])

with tab_evidence:
    st.caption(
        "这些全是确定性图查询的结果——**没有任何模型参与**。"
        "报告的可信度取决于它建立在这些事实之上。"
    )
    if "rca_evidence_cache" not in st.session_state:
        st.session_state["rca_evidence_cache"] = {}

    if ONLINE:
        if st.button("🔄 重新采集证据", key="regather"):
            st.session_state["rca_evidence_cache"].pop(selected_service, None)
        if selected_service not in st.session_state["rca_evidence_cache"]:
            bar = st.progress(0, text="开始采集…")
            box = st.empty()

            def _step(pct: int, msg: str) -> None:
                bar.progress(pct, text=msg)
                box.caption(f"⏳ {msg}")

            st.session_state["rca_evidence_cache"][selected_service] = gather_evidence(
                selected_service, on_step=_step
            )
            bar.progress(100, text="采集完成")
            box.empty()
        ev, ev_mode = st.session_state["rca_evidence_cache"][selected_service], "live"
    else:
        ev, ev_mode = evidence_for_display(selected_service)

    C.mode_badge(ev_mode if ev else "none", "证据数据")

    if not ev:
        st.warning(
            f"离线快照里没有 `{selected_service}` 的证据数据。"
            "已抓取快照的服务：" + "、".join(
                f"`{s}`" for s in C.fixture("rca_evidence").get("by_service", {})
            ) + "。"
        )
    else:
        non_empty = sum(
            1 for v in ev.values()
            if isinstance(v.get("data"), list) and v["data"]
            or isinstance(v.get("data"), dict) and v["data"]
        )
        st.success(f"共 {len(ev)} 条证据查询，其中 **{non_empty}** 条返回了非空结果", icon="📋")
        for qname, item in ev.items():
            _render_one_evidence(qname, item)

with tab_report:
    if "rca_report" not in st.session_state:
        st.session_state["rca_report"] = {}

    if not ONLINE:
        st.info(
            "🔵 **离线模式** —— 报告生成需要 Bedrock。\n\n"
            "刻意**不**预置示例报告：给 LLM 输出做录像，会让观众以为看到的是"
            "现场生成的分析。左侧「证据面板」里的东西才是这一页真正的地基，"
            "而它不需要任何模型就能看。"
        )
    elif run_rca:
        bar = st.progress(0, text="初始化…")
        box = st.empty()

        def _step(pct: int, msg: str) -> None:
            bar.progress(min(pct, 95), text=msg)
            box.caption(f"⏳ {msg}")

        try:
            ev = gather_evidence(selected_service, on_step=_step)
            st.session_state["rca_evidence_cache"][selected_service] = ev

            _step(96, "调用 Bedrock 生成叙述…")
            from core.graph_rag_reporter import generate_rca_report  # type: ignore

            pods = ev.get("q6_pod_status", {}).get("data") or []
            report = generate_rca_report(
                affected_service=selected_service,
                classification={"severity": severity, "alert_message": alert_message},
                rca_result={
                    "error_services": [
                        p for p in pods
                        if isinstance(p, dict)
                        and str(p.get("status", "")).lower() not in ("running", "", "none")
                    ],
                    "recent_changes": ev.get("q19_topology_changes", {}).get("data") or [],
                    "root_cause_candidates": ev.get("q3_upstream_deps", {}).get("data") or [],
                    "aws_probe_results": ev.get("q9_service_infra_path", {}).get("data") or [],
                    "blast_radius": ev.get("q1_blast_radius", {}).get("data") or {},
                    "similar_incidents": ev.get("q5_similar_incidents", {}).get("data") or [],
                    "dependency_verdicts": ev.get("q22_edge_verification_verdicts", {}).get("data") or [],
                },
            )
            bar.progress(100, text="完成")
            box.empty()
            st.session_state["rca_report"][selected_service] = {"report": report}
        except Exception as exc:  # noqa: BLE001
            bar.progress(100, text="失败")
            box.empty()
            st.session_state["rca_report"][selected_service] = {"error": str(exc)}

    entry = st.session_state["rca_report"].get(selected_service)
    if entry is None and ONLINE:
        st.caption("点击左侧「🚀 生成 RCA 报告」开始。证据会先被真实采集，再交给模型。")
    elif entry and entry.get("error"):
        st.error(f"报告生成失败：{entry['error']}")
    elif entry and entry.get("report") is not None:
        rep = entry["report"]
        if isinstance(rep, dict):
            m = st.columns(3)
            m[0].metric("根因判断", str(rep.get("root_cause", "—"))[:24])
            conf = rep.get("confidence")
            m[1].metric("置信度", f"{conf}" if conf is not None else "—")
            m[2].metric("来源", str(rep.get("source", "—")))
            if rep.get("reasoning"):
                st.markdown("**推理过程**")
                st.markdown(rep["reasoning"])
            if rep.get("recommended_action"):
                st.markdown("**建议动作**")
                st.markdown(rep["recommended_action"])
            with st.expander("完整报告 JSON"):
                st.json(rep)
        else:
            st.markdown(str(rep))
        st.caption(
            "⚠️ 模型输出请对照「证据面板」核验——本项目的经验是："
            "agent 在缺少某项数据时会倾向于**编一个看起来合理的数值**，"
            "而不是说「未获取」。"
        )
