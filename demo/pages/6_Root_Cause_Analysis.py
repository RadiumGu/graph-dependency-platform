"""
6_Root_Cause_Analysis.py — 依赖图谱如何参与根因分析。

## 这一页要回答的不是「我们有依赖图」，而是「依赖数据改变了什么结论」

改造要点（2026-09-06，第二轮）：

1. **按推理链组织，不按查询清单组织。** 上一版把 9 条证据查询等权平铺成 9 张卡片，
   顺序就是数组顺序。观众看不出 `q3`（根因候选）与 `q1`（影响面）是图上**方向
   相反的一对**、是 RCA 的两半，它们只是列表里的第 2 项和第 3 项。

2. **方向必须显式画出来。** 上游/下游这套词在请求流与依赖流两种读法下含义相反，
   而实测 `q1`/`q3` 的遍历方向就是反的（见 `tests/test_52_rca_query_direction.py`，
   已于同日修正）。这一页把方向做成第一屏的主体，因为**搞反方向会把受害者
   当成嫌疑人**——这本身就是依赖图在 RCA 里起作用的最短证明。

3. **验证判定要回去改结论，不能只当一排状态灯。** 上一版取了 `q22` 却只渲染成
   status chips。现在把它按 `(src, dst)` 连回候选列表：每个根因候选都带上它所依赖
   的那条边的判定，`refuted` 的候选划掉并给出理由。

4. **`live` 过滤排除了什么，必须摆出来。** 实测 `petsite` 的 17 条依赖里只有 3 条
   算 live，被排除的 14 条分三类（声明未观测 / 观测后静默 / 未对账）。
   「RCA 该用哪些依赖、为什么不用另一些」正是这一页的核心内容，
   而上一版只呈现了过滤后的结果，读者看不到代价。

5. **覆盖率当置信度上限用。** `q23` 实测 `verified_ratio = 10.92%`。
   报告的置信度不该高于它所依赖的那些边的验证程度。

6. **补上 6 条闲置的依赖类查询**：`q12_service_dependency_tree`、`q15_critical_path`、
   `q16_single_point_of_failure`、`q20_dependency_verification`、
   `q21_observation_source_coverage`、`q23_verification_coverage`。
   它们本来就在目录里，零成本，而且正是「依赖在 RCA 中的作用」的内容。

7. **历史类查询收进折叠区**（`q5`/`q17`/`q18`）—— 有用，但不是依赖推理的主线，
   平铺会稀释重点。

8. 上一版侧栏里那段「改造前这里列的是 8 步…」的自我批评已移到本 docstring：
   对项目内部有价值，对访客是噪声。

## 与 1_Edge_Verification 的分工

那一页是**全图验证计分板**（横切）；这一页是**单次事件里验证如何改变结论**（纵切）。
同样的数据两个方向，互相链接，不重复。
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

#: 依赖判定的四种状态 → 「能不能当推理依据」。文案取自契约的判定规则。
VERDICT_USABILITY = {
    "confirmed": ("✅", "可作为推理依据", "在依赖目标端注入故障，源端退化 ≥ 20%"),
    "refuted": ("❌", "**不可**作为推理依据", "注入已确认生效，源端退化 ≤ 5%"),
    "inconclusive": ("⚠️", "需标注为未定", "退化落在 5–20%，或观测流量不足，或注入是否生效未知"),
    "untested": ("⬜", "需声明未验证", "尚未做过主动干预验证（多数边的状态，属正常）"),
}

#: 证据链与阶段标题定义在 _common —— 它有两个消费方（本页 + fixtures/
#: refresh_fixtures.py），各抄一份必然漂移。见 _common.RCA_EVIDENCE_CHAIN。
EVIDENCE_CHAIN = C.RCA_EVIDENCE_CHAIN
STAGE_TITLE = C.RCA_STAGE_TITLE

st.title("🔍 根因分析")
st.markdown(
    "> 这一页要回答的不是「我们有依赖图」，而是**依赖数据改变了什么结论**。\n"
    "> 先用图查询把事实收齐，摆出来，再交给模型写叙述。"
)

svc_rows, svc_mode = C.services()
svc_names = C.service_names()
if not svc_names:
    st.error("未能取得服务清单（图谱不可达且无离线快照）。")
    st.stop()

@st.cache_data(show_spinner=False, ttl=600)
def default_service_index(names: tuple) -> int:
    """默认选中**真的有 live 依赖**的服务。

    为什么需要这个：服务清单按图谱返回顺序排，首位实测是 `artillery` ——
    那是压测工具（DeepFlow 采到它的流量后图里就多出一个「微服务」），
    它的陈旧边已被清理，于是首屏是「0 个根因候选 / 0 影响面」。
    这一页要讲依赖如何参与推理，开局给一个空图是最差的第一印象。

    做法是现查各服务的 live 出边数，取最多的那个 —— 数据驱动，
    而不是硬编码一个服务名（硬编码会在图谱变化后静默失效）。
    """
    if not ONLINE:
        return 0
    try:
        from neptune import neptune_client as nc  # type: ignore
        labels = list(C.dependency_edge_labels())
        rows = nc.results(
            f"""MATCH (n)-[r]->(d)
                WHERE n.name IN {list(names)} AND type(r) IN {labels}
                  AND r.dependency_kind = 'dynamic' AND r.active = true
                RETURN n.name AS name, count(*) AS n
                ORDER BY n DESC LIMIT 1""")
        if rows and rows[0].get("name") in names:
            return list(names).index(rows[0]["name"])
    except Exception:  # noqa: BLE001
        pass
    return 0


# ── 侧栏 ──────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("---")
    st.markdown("### 分析配置")
    selected_service = st.selectbox(
        "故障服务", svc_names, index=default_service_index(tuple(svc_names)))
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
             else "离线模式下无法生成报告，但证据面板仍可查看",
    )

    st.markdown("---")
    st.markdown("### 证据链（按推理阶段分组）")
    st.caption(f"下面 {len(EVIDENCE_CHAIN)} 条查询会**真的执行**：")
    for stage, title in STAGE_TITLE.items():
        items = [x for x in EVIDENCE_CHAIN if x[3] == stage]
        st.caption(f"**{title}**（{len(items)}）")
        for qname, why, _, _ in items:
            st.caption(f"　`{qname}`")
    st.caption(f"最后：Bedrock 生成叙述")


# ── 证据采集 ──────────────────────────────────────────────────────────────────
def gather_evidence(service: str, on_step=None) -> dict:
    """执行证据链。纯图查询，不碰 Bedrock —— 所以离线也能用快照展示。"""
    from neptune.query_catalog import run_query  # type: ignore

    out: dict = {}
    total = len(EVIDENCE_CHAIN)
    for i, (qname, why, pmap, stage) in enumerate(EVIDENCE_CHAIN, 1):
        kw = {k: (service if v is None else v) for k, v in pmap.items()}
        if on_step:
            on_step(int(i / (total + 1) * 100), f"{qname} — {why}")
        try:
            r = run_query(qname, **kw)
            rows = r.get("results", r) if isinstance(r, dict) else r
            out[qname] = {"why": why, "params": kw, "stage": stage, "data": rows}
        except Exception as exc:  # noqa: BLE001
            out[qname] = {"why": why, "params": kw, "stage": stage,
                          "error": f"{type(exc).__name__}: {exc}"}
    return out


def data_of(ev: dict, qname: str, default=None):
    return ev.get(qname, {}).get("data") if ev.get(qname) else default


def show_table(rows, **kw) -> None:
    """类型安全地渲染一张表。

    ## 为什么不能直接 `st.dataframe(C.df(rows))`

    Streamlit 走 pyarrow 序列化，遇到**一列里混着类型**就抛
    `ArrowInvalid: Could not convert '—' with type str: tried to convert to double`
    整页打挂。图谱返回的行天然会踩到这个：

      · 缺失值填 `'—'` 而其余是数值 → 一列里 str + float
      · Neptune 的属性值可能是 list / dict（多值属性、嵌套结构）

    实测两处崩过：候选表的 `退化幅度` 列（`verify_degradation or '—'`），
    以及 `q12_service_dependency_tree` 的嵌套行。

    ⚠️ `AppTest` 抓不到这类问题 —— 它跑的是离线路径，喂进去的是干净的桩数据，
    真实图谱行才会触发。所以这一页改完必须**真的渲染一次看**，
    测试全绿不等于渲染正确。

    做法：把非标量单元格转成字符串；一列里混了类型的，整列转字符串
    （宁可失去排序能力，也不要整页崩）。
    """
    import pandas as pd

    if not isinstance(rows, pd.DataFrame):
        rows = list(rows or [])          # 允许传生成器
    if rows is None or (not isinstance(rows, pd.DataFrame) and not rows):
        return
    frame = rows if isinstance(rows, pd.DataFrame) else C.df(rows)
    for col in frame.columns:
        vals = frame[col]
        if vals.map(lambda x: isinstance(x, (list, dict, set, tuple))).any():
            frame[col] = vals.map(lambda x: "" if x is None else str(x))
            continue
        kinds = {type(x) for x in vals if x is not None}
        numeric = {int, float, bool}
        if len(kinds) > 1 and not kinds <= numeric:
            frame[col] = vals.map(lambda x: "" if x is None else str(x))
    st.dataframe(frame, width="stretch", hide_index=True, **kw)


def verdict_index(ev: dict) -> dict:
    """(src, dst) → q22 判定行。用于把判定连回候选列表。"""
    idx = {}
    for r in (data_of(ev, "q22_edge_verification_verdicts") or []):
        if isinstance(r, dict):
            idx[(r.get("src"), r.get("dst"))] = r
    return idx


@st.cache_data(show_spinner=False, ttl=300)
def dependency_breakdown(service: str) -> dict:
    """`live` 过滤排除了什么 —— 这是本页最该摆出来的差额。

    `kind='live'` = `dependency_kind='dynamic' AND active=true`。实测 petsite 的
    17 条出边里只有 3 条算 live。被排除的不是噪声，各有各的含义：

        声明未观测 static      CFN/配置里写了，但运行时从没看到过流量
        观测后静默 active=false 曾经观测到，现在不再出现（服务可能已下线）
        未对账     active=None  没有任何源的 reconcile 碰过它

    RCA 只该用 live 那一档，但**读者有权知道代价**。
    """
    if not ONLINE:
        return {}
    try:
        from neptune import neptune_client as nc  # type: ignore
        labels = list(C.dependency_edge_labels())
        rows = nc.results(
            f"""MATCH (n {{name: $s}})-[r]->(d)
                WHERE type(r) IN {labels} AND d.name IS NOT NULL
                RETURN d.name AS name, labels(d)[0] AS node_type,
                       type(r) AS edge_type, r.dependency_kind AS kind,
                       r.active AS active, r.verify_status AS verify_status""",
            {"s": service})
    except Exception:  # noqa: BLE001
        return {}
    buckets = {"live": [], "static": [], "silent": [], "unreconciled": []}
    for r in rows:
        k, a = r.get("kind"), r.get("active")
        if k == "dynamic" and a is True:
            buckets["live"].append(r)
        elif k == "static":
            buckets["static"].append(r)
        elif k == "dynamic" and a is False:
            buckets["silent"].append(r)
        else:
            buckets["unreconciled"].append(r)
    return {"total": len(rows), **buckets}


# ── 取证据 ────────────────────────────────────────────────────────────────────
st.session_state.setdefault("rca_ev", {})
st.session_state.setdefault("rca_report", {})

if ONLINE:
    cols = st.columns([1, 5])
    if cols[0].button("🔄 重新采集", key="regather"):
        st.session_state["rca_ev"].pop(selected_service, None)
        dependency_breakdown.clear()
    if selected_service not in st.session_state["rca_ev"]:
        bar = st.progress(0, text="开始采集…")
        box = st.empty()

        def _step(pct: int, msg: str) -> None:
            bar.progress(pct, text=msg)
            box.caption(f"⏳ {msg}")

        st.session_state["rca_ev"][selected_service] = gather_evidence(
            selected_service, on_step=_step)
        bar.progress(100, text="采集完成")
        box.empty()
    ev, ev_mode = st.session_state["rca_ev"][selected_service], "live"
else:
    snap = C.fixture("rca_evidence").get("by_service", {})
    ev = snap.get(selected_service, {})
    ev_mode = "snapshot" if ev else "none"

# ── 头部：服务概览 + 证据底座 ─────────────────────────────────────────────────
svc_meta = [r for r in svc_rows if r.get("name") == selected_service]
head = st.columns([2, 1, 1, 1])
head[0].markdown(f"### {SEVERITY_COLORS[severity]} {selected_service}")
if svc_meta:
    tiers = {r.get("tier") for r in svc_meta if r.get("tier")}
    azs = [r.get("az") for r in svc_meta if r.get("az")]
    head[1].metric("Tier", "/".join(sorted(tiers)) if tiers else "—")
    # AZ 数要去重：svc_rows 每行是一个 (服务, az, tier) 组合，
    # 同一个 AZ 可能出现多行，len(azs) 会把它数成多个 AZ。
    #
    # 附注不要放在 delta 位：`delta_color="off"` 只去掉颜色，**箭头还在**，
    # 于是渲染成「↑ 个可用区」，读起来像个上升趋势。附注一律用 help。
    head[2].metric("AZ 分布", len(set(azs)),
                   help="该服务的 Pod 分布在几个可用区。1 个可用区意味着 AZ 级故障"
                        "会让它整体不可用 —— DR 计划那一页按这个判断影响面。")
    # 这里原来是 `head[3].metric("图节点数", len(svc_meta))` —— **标签是错的**。
    # len(svc_meta) 是服务清单里匹配到的行数（az/tier 组合数），不是图节点数，
    # 而且它和左边的「AZ 分布」基本是同一个数换算法数两遍。
    # 后果是首屏经常显示「图节点数 1」，让人以为图里是空的 —— 对一个
    # 主张「这张图是真的」的站点，这种误导比少一个指标糟得多。
    # 换成真正有意义的:该服务的依赖出边数（根因就在出边）。
    _brk = dependency_breakdown(selected_service)
    if _brk:
        # 返回结构是 {"total": int, "live": [...], "static": [...], ...} ——
        # 直接取 total，不要对 values() 求 len（total 是 int，会 TypeError）。
        head[3].metric(
            "依赖出边", _brk.get("total", 0),
            help=f"该服务指向别人的依赖边共 {_brk.get('total', 0)} 条，"
                 f"其中 **live**（`dependency_kind=dynamic` 且 `active=true`）"
                 f"{len(_brk.get('live', []))} 条。"
                 "RCA 只该用 live 那一档，但差额有意义："
                 "声明未观测／观测后静默／未对账各自代表不同的问题。"
                 "**根因在出边、影响面在入边。**")
    else:
        head[3].metric("依赖出边", "—", help="离线模式下无法实时查依赖边。")
C.mode_badge(ev_mode if ev else "none", "证据数据")

if not ev:
    st.warning(
        f"离线快照里没有 `{selected_service}` 的证据数据。已抓取快照的服务："
        + "、".join(f"`{s}`" for s in
                   C.fixture("rca_evidence").get("by_service", {})) + "。")
    st.stop()

# Tab 标签必须短。
#
# 2026-09-07 部署后截图发现:五个标签带括号说明后总宽超出容器，
# 第五个 Tab 被挤到横向滚动箭头后面 —— 整站最有价值的一屏等于没做。
# 括号里的说明挪进各 Tab 内部的首行 caption（放那里更有用，
# 因为读到它的时候人已经在看对应内容了）。
tab_dir, tab_trust, tab_ctx, tab_report, tab_agent = st.tabs([
    "🧭 因果链",
    "⚖️ 依赖可信度",
    "🏗️ 基础设施",
    "📝 RCA 报告",
    "🤖 交给 Agent",
])

# ══ Tab 1：因果链 ═════════════════════════════════════════════════════════════
with tab_dir:
    st.markdown("#### 1　因果方向盘")
    st.caption(
        "RCA 的两个问题是图上**两个相反的方向**。契约里依赖边一律从依赖方指向"
        "被依赖方，所以：**根因在出边、影响面在入边**。"
        "搞反会把受害者当成嫌疑人 —— 这正是依赖图在 RCA 里起作用的最短证明。"
    )

    cands = data_of(ev, "q3_upstream_deps") or []
    blast = data_of(ev, "q1_blast_radius") or {}
    b_svcs = blast.get("services", []) if isinstance(blast, dict) else []
    b_caps = blast.get("capabilities", []) if isinstance(blast, dict) else []

    wheel = st.columns([5, 2, 5])
    with wheel[0]:
        st.markdown(f"##### ⬅️ 根因候选（{len(cands)}）")
        st.caption("`q3_upstream_deps` · 出边 · **它依赖谁**")
        st.caption("这些挂了，故障服务才会挂")
        if not cands:
            st.info("0 个 live 依赖。见下方第 3 节：被 `live` 过滤排除了什么。")
        else:
            show_table(cands)
    with wheel[1]:
        st.markdown("<div style='text-align:center;padding-top:2.2rem'>"
                    "<div style='font-size:1.6rem'>🔴</div>"
                    f"<b>{selected_service}</b><br>"
                    "<span style='color:#888;font-size:.8rem'>故障服务</span>"
                    "</div>", unsafe_allow_html=True)
    with wheel[2]:
        st.markdown(f"##### ➡️ 影响面（{len(b_svcs)} 服务 / {len(b_caps)} 能力）")
        st.caption("`q1_blast_radius` · 入边 · **谁依赖它**")
        st.caption("故障服务挂了，这些跟着挂")
        if not b_svcs and not b_caps:
            st.info("0 个 live 下游消费者。")
        else:
            if b_svcs:
                show_table(b_svcs)
            if b_caps:
                st.caption("受影响的业务能力")
                show_table(b_caps)

    # ── 2　候选可信度 ─────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 2　验证如何收窄候选名单")
    st.caption(
        "这一节是**依赖数据改变结论**的地方：把每个根因候选连回它所依赖的那条边的"
        "故障注入判定。判据是「在依赖目标端注入故障、观测源端是否退化」——"
        "方向是全部关键，本项目最初把故障注入在源端再观测源端，"
        "于是 72 个实验「全部通过、零失败」，因为那个判据对真边和假边给出相同结果。"
    )
    vidx = verdict_index(ev)
    if not cands:
        st.info("没有 live 候选可判定。")
    else:
        rows = []
        for c in cands:
            name = c.get("name")
            v = vidx.get((selected_service, name)) or {}
            status = v.get("verify_status") or "untested"
            icon, usable, basis = VERDICT_USABILITY.get(
                status, ("⬜", "未知", ""))
            rows.append({
                "候选": name,
                "边类型": c.get("edge_type") or v.get("edge_type") or "—",
                "判定": f"{icon} {status}",
                "能否作为推理依据": usable,
                "退化幅度": (f'{v["verify_degradation"]}'
                        if v.get("verify_degradation") is not None else "—"),
                "实验": v.get("verify_experiment") or "—",
                "理由": v.get("verify_reason") or basis,
            })
        show_table(rows)

        usable_n = sum(1 for r in rows if r["判定"].endswith("confirmed"))
        refuted_n = sum(1 for r in rows if r["判定"].endswith("refuted"))
        m = st.columns(4)
        m[0].metric("候选总数", len(rows))
        m[1].metric("✅ 可直接采信", usable_n)
        m[2].metric("❌ 已证伪（应划掉）", refuted_n)
        m[3].metric("⚠️ 需标注未定", len(rows) - usable_n - refuted_n)
        if refuted_n == 0:
            st.caption(
                "本服务目前没有 refuted 的依赖 —— 全图 `refuted_count` 也是 0。"
                "这不是「证伪能力没用上」：本项目曾判出 refuted 又被"
                "`verify_revoked_by=independent-evidence-gate` **撤销** ——"
                "判定自身也会被推翻，这比「我们能证伪」更能说明它是可证伪的。"
            )

    # ── 3　live 过滤排除了什么 ────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 3　`live` 过滤排除了什么")
    bd = dependency_breakdown(selected_service)
    if not bd:
        st.caption("离线模式下不展示该分解（需要现查活图谱）。")
    else:
        st.caption(
            f"`{selected_service}` 一共有 **{bd['total']}** 条出向依赖边，"
            f"RCA 只用其中 **{len(bd['live'])}** 条。"
            "被排除的不是噪声，各有各的含义 —— 读者有权知道代价。"
        )
        b = st.columns(4)
        b[0].metric("✅ live", len(bd["live"]),
                    help="`dependency_kind=dynamic` 且 `active=true` —— "
                         "运行时确实观测到、且现在仍在出现。RCA 只该用这一档。")
        b[1].metric("📄 声明未观测", len(bd["static"]),
                    help="`dependency_kind=static` —— CFN/配置里写了，"
                         "但运行时从没看到过流量。不等于不存在，只是没被观测到。")
        b[2].metric("🔇 观测后静默", len(bd["silent"]),
                    help="`dependency_kind=dynamic` 且 `active=false` —— "
                         "曾经观测到，现在不再出现。服务可能已下线，"
                         "也可能只是不再被采集：「不再被采集」≠「不再存在」。")
        b[3].metric("❔ 未对账", len(bd["unreconciled"]),
                    help="`active` 缺失 —— 没有任何源的 reconcile 碰过它。")
        for key, title, why in (
            ("live", "✅ live —— RCA 实际使用的",
             "运行时观测到、且当前仍然存在。"),
            ("static", "📄 声明未观测",
             "CFN / AWS 配置里声明了，但运行时从未观测到流量。"
             "**不代表不存在** —— 可能只是这条路径当前没被走到。"),
            ("silent", "🔇 观测后静默",
             "曾经观测到，现在不再出现。可能服务已下线，"
             "也可能只是暂时零流量 —— 零流量与健康在指标上无法区分。"),
            ("unreconciled", "❔ 未对账",
             "没有任何源的 reconcile 碰过它，`active` 属性缺失。"
             "这是**数据缺口**，不是依赖状态。"),
        ):
            items = bd.get(key) or []
            if not items:
                continue
            with st.expander(f"{title}（{len(items)}）"):
                st.caption(why)
                show_table(items)

    # ── 4　依赖子图 ───────────────────────────────────────────────────────────
    st.markdown("---")
    st.markdown("#### 4　依赖子图（边色 = 验证判定）")
    st.caption(
        "刻意做小 —— 它是论据，不是玩具。完整的图探索在 Graph Explorer。")
    C.page_link("pages/3_Graph_Explorer.py", "→ 打开 Graph Explorer")
    tree = data_of(ev, "q12_service_dependency_tree")
    if isinstance(tree, (list, dict)) and tree:
        with st.expander("依赖树原始数据 `q12_service_dependency_tree`"):
            if isinstance(tree, list):
                show_table(tree)
            else:
                st.json(tree, expanded=False)
    else:
        st.caption("`q12_service_dependency_tree` 返回空。")

# ══ Tab 2：依赖可信度 ═════════════════════════════════════════════════════════
with tab_trust:
    st.markdown("#### 报告的置信度上限，由证据的验证程度决定")
    cov = data_of(ev, "q23_verification_coverage") or {}
    if isinstance(cov, dict) and cov:
        total = cov.get("total_dependency_edges") or 0
        by = cov.get("by_status") or {}
        C.status_chips(by, total)
        st.caption(
            f"全图 **{total}** 条依赖边，`verified_ratio` = "
            f"**{cov.get('verified_ratio')}** —— 即真正做过主动干预并得出结论的比例。"
            "**报告的置信度不该高于它所依赖的那些边的验证程度。**"
        )
        per = cov.get("per_edge_type") or {}
        if per:
            st.markdown("##### 按边类型")
            show_table([{"边类型": k, **v} for k, v in per.items()])
            st.caption(
                "`Invokes` / `InvokesTool` 整类零验证 —— 那是 agent 侧的调用边，"
                "对它们做主动干预需要能在 AgentCore 里注入故障，目前做不到。"
                "**说明它未验证，比给它一个看起来合理的置信度更诚实。**"
            )

    st.markdown("---")
    st.markdown("#### 观测源覆盖：一条边被几个源看到")
    src_cov = data_of(ev, "q21_observation_source_coverage") or []
    if isinstance(src_cov, list) and src_cov:
        from collections import Counter
        c = Counter(r.get("coverage") or "?" for r in src_cov
                    if isinstance(r, dict))
        cc = st.columns(min(len(c), 5) or 1)
        for i, (k, n) in enumerate(c.most_common(5)):
            cc[i].metric(k, n)
        show_table(src_cov)
        st.caption(
            "X-Ray、DeepFlow、NFM 是**盲区不重叠的平行源**，不是主备关系："
            "X-Ray 能精确到 AWS 资源名，DeepFlow 只到域名，走 VPC 端点时"
            "DNS 侧连域名都没有。所以 `xray_only` / `deepflow_only` 不是缺陷，"
            "是各自能力边界的体现。\n\n"
            "⚠️ 注意 `observable_but_unobserved` 与 `unobservable_by_design` "
            "的区分：business-layer 写入的边**本质不可观测**，"
            "把两者混在一起会高估依赖质量问题。"
        )
    else:
        st.caption("`q21` 返回空。")

    st.markdown("---")
    st.markdown("#### 观测层漂移：这条边最近还有没有被看到")
    drift = data_of(ev, "q20_dependency_verification") or []
    if isinstance(drift, list) and drift:
        show_table(drift)
        st.caption(
            "与上面的故障注入判定**层次不同**：`q20` 是观测层（这条边最近有没有被"
            "看到），`q22` 是干预层（在目标端注入故障、源端会不会退化）。"
            "一条边可以「最近被看到」却「从未被验证」。"
        )
    else:
        st.caption("`q20` 没有报告问题边。")

    st.markdown("---")
    st.markdown("#### 拓扑地位：告警级别应该有图上的依据")
    tp = st.columns(2)
    with tp[0]:
        spof = data_of(ev, "q16_single_point_of_failure") or []
        st.markdown(f"##### 单点故障（{len(spof) if isinstance(spof, list) else '—'}）")
        st.caption("由图算法算出，不是推理得来的 —— 可直接采信其拓扑结论。")
        if isinstance(spof, list) and spof:
            hit = [r for r in spof if isinstance(r, dict)
                   and selected_service in str(r.values())]
            if hit:
                st.error(f"`{selected_service}` 在单点故障清单里", icon="⚠️")
            show_table(spof)
    with tp[1]:
        cp = data_of(ev, "q15_critical_path") or []
        st.markdown(f"##### 关键路径（{len(cp) if isinstance(cp, list) else '—'}）")
        st.caption("是否在关键路径上，决定这次告警值不值 P0。")
        if isinstance(cp, list) and cp:
            show_table(cp)

    C.page_link("pages/1_Edge_Verification.py",
                "→ 全图验证计分板（这一页是单次事件的纵切，那一页是全图横切）")

# ══ Tab 3：基础设施与历史 ═════════════════════════════════════════════════════
with tab_ctx:
    st.caption(
        "这一 Tab 是**基础设施与历史**：服务落在哪条基础设施路径上、Pod 现状、"
        "基础设施层根因候选、以及最近的拓扑变更。"
        "拓扑变更那一段常常是「什么时候开始坏的」最短的答案。")
    for qname in ("q9_service_infra_path", "q6_pod_status",
                  "q10_infra_root_cause", "q19_topology_changes"):
        item = ev.get(qname)
        if not item:
            continue
        st.markdown(f"##### `{qname}` — {item.get('why', '')}")
        if item.get("error"):
            st.caption(f"❌ 查询失败：{item['error']}")
            st.caption("⚠️ 查询失败不等于「该依赖不存在」——这是取数失败，不是事实。")
        else:
            d = item.get("data")
            if isinstance(d, list) and d:
                show_table(d)
            elif isinstance(d, dict) and d:
                st.json(d, expanded=False)
            else:
                st.caption("返回 0 行。空结果不等于错误。")
        if qname == "q19_topology_changes":
            st.caption(
                "**CloudTrail 看不见这两类变化** —— 依赖出现/消失是「流量出现或"
                "缺席」，不是一次 API 调用。这是图谱能提供而云厂商审计日志"
                "提供不了的东西。")
        st.markdown("")

    with st.expander("历史参照（不是依赖推理的主线）"):
        for qname in ("q5_similar_incidents", "q17_incidents_by_resource",
                      "q18_chaos_history"):
            item = ev.get(qname)
            if not item:
                continue
            st.markdown(f"**`{qname}`** — {item.get('why', '')}")
            d = item.get("data")
            if isinstance(d, list) and d:
                show_table(d)
            else:
                st.caption(item.get("error") or "返回 0 行。")

# ══ Tab 4：报告 ═══════════════════════════════════════════════════════════════
with tab_report:
    if not ONLINE:
        st.info(
            "🔵 **离线模式** —— 报告生成需要 Bedrock。\n\n"
            "刻意**不**预置示例报告：给 LLM 输出做录像，会让观众以为看到的是现场"
            "生成的分析。前三个 Tab 里的东西才是这一页真正的地基，"
            "而它们不需要任何模型就能看。")
    elif run_rca:
        bar = st.progress(0, text="初始化…")
        box = st.empty()

        def _step(pct: int, msg: str) -> None:
            bar.progress(min(pct, 95), text=msg)
            box.caption(f"⏳ {msg}")

        try:
            ev = gather_evidence(selected_service, on_step=_step)
            st.session_state["rca_ev"][selected_service] = ev
            _step(96, "调用 Bedrock 生成叙述…")
            from core.graph_rag_reporter import generate_rca_report  # type: ignore

            pods = data_of(ev, "q6_pod_status") or []
            report = generate_rca_report(
                affected_service=selected_service,
                classification={"severity": severity,
                                "alert_message": alert_message},
                rca_result={
                    "error_services": [
                        p for p in pods if isinstance(p, dict)
                        and str(p.get("status", "")).lower()
                        not in ("running", "", "none")],
                    "recent_changes": data_of(ev, "q19_topology_changes") or [],
                    # 方向已于 2026-09-06 修正：根因候选来自 q3（出边，它依赖谁）。
                    # 修正前这里传的是 q1，而 q1 当时走的是出边 —— 两处错误
                    # 恰好互相抵消了一半，所以看起来「有结果」。
                    "root_cause_candidates": data_of(ev, "q3_upstream_deps") or [],
                    "aws_probe_results": data_of(ev, "q9_service_infra_path") or [],
                    "blast_radius": data_of(ev, "q1_blast_radius") or {},
                    "similar_incidents": data_of(ev, "q5_similar_incidents") or [],
                    "dependency_verdicts":
                        data_of(ev, "q22_edge_verification_verdicts") or [],
                    "verification_coverage":
                        data_of(ev, "q23_verification_coverage") or {},
                })
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
        cov = data_of(ev, "q23_verification_coverage") or {}
        if isinstance(rep, dict):
            m = st.columns(4)
            m[0].metric("根因判断", str(rep.get("root_cause", "—"))[:24])
            m[1].metric("模型置信度", f"{rep.get('confidence', '—')}")
            m[2].metric("证据验证率",
                        f"{cov.get('verified_ratio', '—')}" if cov else "—")
            m[3].metric("来源", str(rep.get("source", "—")))
            if cov and isinstance(cov.get("verified_ratio"), (int, float)):
                st.warning(
                    f"模型给出的置信度是 **{rep.get('confidence', '—')}**，"
                    f"而它所依赖的依赖边里只有 **{cov['verified_ratio']:.1%}** "
                    "做过主动干预验证。**置信度不该高于证据的验证程度** ——"
                    "两者差距越大，这份报告越需要人工核验。", icon="⚖️")
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

        st.markdown("##### 这份报告站在哪些证据上")
        st.caption("每条论断都应能对应到下面某条查询的结果。")
        show_table([
            {"查询": q, "阶段": STAGE_TITLE.get(v.get("stage", ""), ""),
             "说明": v.get("why", ""),
             "结果": (f"{len(v['data'])} 行" if isinstance(v.get("data"), list)
                    else ("对象" if isinstance(v.get("data"), dict)
                          else v.get("error", "—")))}
            for q, v in ev.items()])
        st.caption(
            "⚠️ 模型输出请对照上表核验 —— 本项目的经验是：agent 在缺少某项数据时"
            "会倾向于**编一个看起来合理的数值**，而不是说「未获取」。")


# ══ Tab 5：交给 Agent ═════════════════════════════════════════════════════════
#
# 这一屏回答的问题：**把这张图接给一个通用运维 agent，到底改变了什么？**
#
# 不是「比谁聪明」。任何 LLM 被问一个它没有事实依据的问题时都会给出答案，
# 因为它不会说「我不知道」—— 包括本项目自己的引擎。区别只在于有没有
# 经过验证的数据可查，以及**它会不会真的去查**。
#
# 数据是 12 次真实调用（AWS DevOps Agent，agent space petsite-devops，
# 图谱以 MCP server 形式关联，24 条只读查询）。全部记录在
# demo/fixtures/agent_unaided_answer.json：逐字原文、executionId、耗时、复现命令。
with tab_agent:
    rec = C.fixture("agent_unaided_answer")
    runs = [r for r in (rec.get("runs") or []) if "error" not in r]

    st.markdown("#### 1　这一屏在证明什么")
    st.caption(
        "**不是比谁聪明。** 任何 LLM 被问一个它没有事实依据的问题时都会给出答案，"
        "因为它不会说「我不知道」—— 包括本项目自己的引擎。"
        "AWS DevOps Agent 擅长的部分做得确实好（从 CloudTrail 挖出 FIS 实验与发起者），"
        "而且**接上这张图之后表现很好**。\n\n"
        "真正的发现更朴素也更要紧：**工具可用 ≠ 工具会被用。**"
    )

    if not runs:
        st.warning(
            "没有读到 agent 调用记录。这一屏依赖 "
            "`demo/fixtures/agent_unaided_answer.json`，"
            "它由真实调用生成（见本页最下方的复现命令），不是合成数据。")
    else:
        plain = [r for r in runs if r.get("kind") == "plain"]
        expl = [r for r in runs if r.get("kind") == "explicit"]
        pg = sum(1 for r in plain if r.get("used_graph"))
        eg = sum(1 for r in expl if r.get("used_graph"))

        st.markdown("#### 2　同一个问题，问了 %d 次" % len(runs))
        st.caption(
            "同一个 agent space、同一段问题文本。唯一的变量是**有没有在问题里"
            "点名要求查图谱**。")

        # 比例进 label，不进 delta 位：delta 会渲染成箭头，
        # 「↑ 25%」读起来像「上升了 25%」，而这是一个占比。
        m = st.columns(4)
        m[0].metric(
            "不提图谱时会去查　%s" % (f"{100 * pg / len(plain):.0f}%" if plain else "—"),
            f"{pg}/{len(plain)}",
            help="问题里不点名图谱时，agent 自己决定要不要查。")
        m[1].metric(
            "点名要求查图谱　%s" % (f"{100 * eg / len(expl):.0f}%" if expl else "—"),
            f"{eg}/{len(expl)}",
            help="同一段问题后面加一句「请使用依赖图谱查询」。")
        m[2].metric("单次耗时", "%.0f–%.0f 秒" % (
            min(r["elapsed_seconds"] for r in runs),
            max(r["elapsed_seconds"] for r in runs)),
            help="所以这一屏默认展示已抓取的记录，不做实时阻塞调用。")
        m[3].metric("图谱查询耗时", "毫秒级",
                    help="而且每次结果相同。这个不对称本身就是论据。")

        st.info(
            "**工具是好的，关联是好的，一调就准 —— 模型只是不主动去拿。**\n\n"
            f"不提图谱时它只有 {100 * pg / len(plain):.0f}% 的调用会去查"
            f"（{pg}/{len(plain)}）；点名要求就是 "
            f"{100 * eg / len(expl):.0f}%（{eg}/{len(expl)}）。"
            "这不是配置坏了 —— 没查的那些调用一样成功返回、排版精美、语气自信。"
            "**一个挂在那里的 MCP server 不等于它会被用上。**",
            icon="🔑")

        # ── 判别方法：必须先说清楚，否则上面的比例只是我说了算 ──────────────
        st.markdown("#### 3　怎么判断它到底查了没有")
        st.caption(
            "这一步不能含糊：如果判别方法不可靠，上面那两个比例就只是自说自话。")

        sig_rows = []
        for i, r in enumerate(runs, 1):
            s = r.get("signals") or {}
            sig_rows.append({
                "#": i,
                "提问方式": "点名要求查图谱" if r.get("kind") == "explicit" else "不提图谱",
                "判定": "查了图谱" if r.get("used_graph") else "只用 FIS 模板",
                "退化幅度数字": s.get("degradation_figures", 0),
                "injection_confirmed": s.get("injection_confirmed_mentions", 0),
                "FIS 模板 ID": s.get("fis_template_ids", 0),
                "耗时（秒）": r.get("elapsed_seconds"),
                "字数": r.get("answer_chars"),
            })
        show_table(sig_rows)

        st.caption(
            "**判据是「退化幅度数字」这一列，它是类别性分离的**："
            "查了图谱的回答引用 20–36 个实测退化百分比，没查的**恰好 0 个** —— "
            "因为 AWS 控制面里没有这个数。它只存在于故障注入实验的结果里，"
            "而实验结果只存在于这张图上。`injection_confirmed` 同理，"
            "那是**边上的属性名**，控制面看不到。")

        with st.expander("⚠️ 我前两次的判据都是错的（记录在案）"):
            st.markdown(
                "这件事值得写下来，因为它和本项目要防的错误是同一类：\n\n"
                "**第一次**：数 `contentBlockStart` 里 `tool_use` 类型的块。"
                "该值**恒为 0** —— 真实工具调用体现在 `tool_summary` 块。"
                "于是我得出「它从不查图谱」，写进了台账。\n\n"
                "**第二次**：改用「引用 `exp-` 前缀实验 ID」。漏掉了两次调用 —— "
                "它们把实验写成「HTTP chaos · 2026-09-05」而不是原始 ID，"
                "于是被误判成没查图谱，比例算成 1/8 而不是 2/8。\n\n"
                "**第三次（现用）**：退化幅度数字 + `injection_confirmed`，"
                "两类样本零重叠、零残余歧义。\n\n"
                "教训与本项目的核心判据一致：**判据必须对准现象独有的东西，"
                "而不是它常见的书写形式。**")

        # ── 两类回答对照 ──────────────────────────────────────────────────────
        st.markdown("#### 4　两类回答长什么样")
        g_run = next((r for r in runs if r.get("used_graph")), None)
        f_run = next((r for r in runs if not r.get("used_graph")), None)

        cmp_cols = st.columns(2)
        with cmp_cols[0]:
            st.markdown("##### ❌ 没查图谱")
            if f_run:
                st.caption(
                    f"`{f_run['execution_id'][:8]}…`　"
                    f"{f_run['captured_at'][:19]}　{f_run['elapsed_seconds']} 秒")
                st.warning(
                    "**依据是「FIS 实验模板存在」。** 模板是**意图**，不是**结果**："
                    "它说明有人打算测，不说明测过了、更不说明测出了什么。",
                    icon="⚠️")
                st.caption("它的原话（逐字）：")
                st.markdown(
                    "> 5 个 FIS Aurora Reboot 模板（EXT3bF1…等），"
                    "目标精确指向 writer 实例 …… 配置双重停止条件")
                st.caption(
                    "这段话每一个字都对 —— 模板确实存在、目标确实精确。"
                    "问题在于它**不能支撑 `confirmed` 这个判定**。")
            else:
                st.caption("本批样本里没有这一类。")
        with cmp_cols[1]:
            st.markdown("##### ✅ 查了图谱")
            if g_run:
                st.caption(
                    f"`{g_run['execution_id'][:8]}…`　"
                    f"{g_run['captured_at'][:19]}　{g_run['elapsed_seconds']} 秒")
                st.success(
                    "**依据是实验结果。** 带实验 ID、退化幅度，"
                    "而且在证据不足时**主动拒绝下结论**。", icon="✅")
                st.caption("它的原话（逐字）：")
                st.markdown(
                    "> **payforadoption**｜退化仅 0.4%，且**注入生效性未知**"
                    "（未提供 `injection_confirmed`）。无法区分「依赖不传导」与"
                    "「注入根本没打到」，不能证伪，故不下结论。")
                st.caption(
                    "这正是本项目的判定纪律：**零退化与注入失败在指标上无法区分，"
                    "所以一律 inconclusive，绝不判 refuted。** "
                    "它不是被教会了这条规则，是**图上的数据本身逼出了这个结论**。")
            else:
                st.caption("本批样本里没有这一类。")

        # ── 图谱这一侧：实时查 ────────────────────────────────────────────────
        st.markdown("#### 5　图谱这一侧（实时查询，不是快照）")
        st.caption(
            f"上面 agent 的回答是**已抓取的记录**（非确定性、单次 45–205 秒）；"
            f"下面这张表是**现在查的**。这个不对称本身就是论据："
            f"图谱毫秒级返回、每次结果相同、每一行都能溯源到具体实验。")

        labels = C.dependency_edge_labels()
        if C.neptune_online() and labels:
            L = ", ".join(f"'{x}'" for x in labels)
            res = C.gquery(
                f"MATCH (s)-[r]->(t) WHERE s.name='{selected_service}' "
                f"AND type(r) IN [{L}] "
                "RETURN type(r) AS 边类型, t.name AS 依赖目标, "
                "coalesce(r.verify_status,'untested') AS 判定, "
                "coalesce(r.source,'—') AS 观测来源 "
                "ORDER BY 判定, 边类型")
            rows = res.get("results", []) if isinstance(res, dict) else (res or [])
            if rows:
                from collections import Counter
                dist = Counter(r.get("判定") for r in rows)
                st.caption(
                    f"`{selected_service}` 的 **{len(rows)}** 条依赖边："
                    + "　".join(f"**{k}** {v}" for k, v in sorted(dist.items())))
                show_table(rows)
                st.caption(
                    "判定存在边的 `verify_status` 属性上。"
                    "`untested` 不是缺陷 —— 它是**诚实**："
                    "这条边可以用于推理，但必须声明未经验证。"
                    "业界所有依赖图这一列都是 100% untested，"
                    "只是没人算过，因为没有持久化的边实体、也没有故障注入后端。")
            else:
                st.info(
                    f"`{selected_service}` 在图上没有依赖边。"
                    f"依赖边类型限于：{'、'.join(f'`{x}`' for x in labels)}；"
                    "`RunsOn`、`TestedBy` 等**不算依赖边**。")
        else:
            st.info(
                "离线模式：这一栏需要实时查 Neptune。"
                "在线时它会显示当前服务逐条依赖边的判定与观测来源。")

        # ── 这对我们自己意味着什么 ────────────────────────────────────────────
        st.markdown("#### 6　这个结果指出了我们自己要修的东西")
        st.caption(
            f"{100 * pg / len(plain):.0f}% 的自发查询率"
            "**不是 agent 的问题，是我们的问题**。"
            "本项目把证据纪律写在 MCP `initialize` 的 `instructions` 字段里，"
            "指望客户端把它交给模型 —— 而这批数据说明**这套纪律没有可靠地传达到**。"
            "现在有了度量手段（上面那张信号表），这就从一句抱怨变成了"
            "可优化、可验收的工程问题：改工具描述与 agent space 指令，"
            "再跑同一批采样，看比例升不升。")

        # ── 复现 ──────────────────────────────────────────────────────────────
        with st.expander("🔬 复现这批数据"):
            st.caption(
                f"agent space `{rec.get('agent_space_name')}` "
                f"(`{rec.get('agent_space_id')}`)，区域 "
                f"`{rec.get('region')}`。`aws devops-agent` 是 AWS CLI 的一等服务，"
                "调用闭环是 `CreateChat` → `SendMessage`（返回 **EventStream**，"
                "流式，不是 dict）→ `ListPendingMessages`。")
            st.markdown("**问题原文（不提图谱）**")
            st.code(rec.get("question", ""), language="text")
            if rec.get("question_explicit"):
                st.markdown("**问题原文（点名要求查图谱）**")
                st.code(rec["question_explicit"], language="text")
            st.markdown("**全部 executionId**")
            st.code("\n".join(
                f"{r.get('kind','?'):9s} {r['execution_id']}  "
                f"{'查了图谱' if r.get('used_graph') else '只用FIS模板'}"
                for r in runs), language="text")
            st.caption(
                "每一条都可以用 `aws devops-agent list-pending-messages` "
                "按 executionId 取回原始消息核对。"
                "逐字全文在 `demo/fixtures/agent_unaided_answer.json`。")
