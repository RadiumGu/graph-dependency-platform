"""
1_Edge_Verification.py — 依赖边验证（本项目的核心产出）。

为什么这页最重要：整个平台的差异点不是「画了一张依赖图」，而是
「这张图上的每条依赖边都可以被主动证伪」。改造前的界面完全没有这一页，
而 profiles/graph_contract.yaml 里的 edge_verification 模型早已完整实现。

数据来源：依赖边的 verify_* 属性只存在于 Neptune 边属性上，实验报告里没有，
所以本页必须查图；不可达时回退到 fixtures/verification.json 的真实快照。
"""
import os
import sys

# 页面被单独执行时（streamlit run pages/X.py，或测试直接 exec 该文件），
# demo/ 不在 sys.path 上，import _common 会失败。显式补上。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _common as C  # noqa: E402

import streamlit as st  # noqa: E402

C.page_setup("边验证", icon="🎯")
C.sidebar()

st.title("🎯 依赖边验证")
st.markdown(
    "> 对边 `A → B`：**在 B 注入故障，观测 A**。"
    "A 退化 → 依赖成立；A 毫无反应且注入确实生效 → 依赖不成立。"
)

vdata, vmode = C.verification_data()
C.mode_badge(vmode, "验证数据")

if not vdata:
    st.warning("无验证数据可展示。")
    st.stop()

counts = vdata.get("totals_by_status", {})
total = vdata.get("dependency_edge_total", 0)
decided = vdata.get("decided_edges", []) or []

# ── 计分板 ────────────────────────────────────────────────────────────────────
C.status_chips(counts, total)

st.caption(
    f"共 **{total}** 条依赖边，来自契约里标记 `dependency: true` 的 "
    f"{len(C.dependency_edge_labels())} 种边类型：" +
    "、".join(f"`{x}`" for x in C.dependency_edge_labels())
)

# ── 为什么方向很重要 ──────────────────────────────────────────────────────────
with st.expander("⚠️ 为什么「在 B 注入、观测 A」这个方向是全部关键", expanded=False):
    st.markdown(
        """
本项目自己的代码最初把故障**注入在 A**，然后观测 A 自己。

后果：历史上 **72 个实验全部「通过」、零失败**。因为注入 A 再看 A 当然会退化——
这个判据**没有任何区分能力**，它对真边和假边给出完全相同的结果。

改成「注入 B、观测 A」之后，同一批边里立刻出现了 refuted。

> 一个永远不会失败的验证，等于没有验证。
"""
    )
    d1, d2 = st.columns(2)
    d1.error("**错误做法**\n\n注入 A → 观测 A\n\nA 必然退化 → 恒为「通过」", icon="❌")
    d2.success("**正确做法**\n\n注入 B → 观测 A\n\nA 退化才说明 A 真的依赖 B", icon="✅")

# ── 被证伪的边（最有说服力的部分）──────────────────────────────────────────────
refuted = [e for e in decided if e.get("status") == "refuted"]
st.markdown("---")
st.subheader(f"❌ 被证伪的边（{len(refuted)}）")

if refuted:
    st.error(
        "这些边**曾经存在于图上**——某个数据源声称它们成立。"
        "故障注入证明它们不成立：在目标端注入故障、且确认注入已生效，源端毫无反应。",
        icon="❌",
    )
    for e in refuted:
        with st.container(border=True):
            cc = st.columns([3, 1, 1, 2])
            cc[0].markdown(
                f"**`{e.get('source')}`** ─[ `{e.get('edge_type')}` ]→ **`{e.get('target')}`**"
            )
            cc[1].metric("观测方退化", f"{e.get('degradation', 0)}%")
            cc[2].metric("置信度分", e.get("confidence", "—"))
            cc[3].caption(
                f"实验 `{e.get('experiment') or '—'}`\n\n"
                f"边来源 `{e.get('edge_source') or '—'}`"
            )
            if e.get("reason"):
                st.caption(f"判定理由：{e['reason']}")
    st.caption(
        "置信度分为负（如 -4.0）来自证据权重表里 `intervention_refuted = -4.0`——"
        "一次主动干预的证伪，压过所有静态声明与被动观测。"
    )
else:
    # ── 「0 条被证伪」是这一页最容易被误读的数字 ──────────────────────────────
    #
    # 原来这里只有一句 `st.info("当前没有被证伪的边。")`。那句话是**空洞的** ——
    # 它没告诉读者这是好事（图很干净）、坏事（判伪通道坏了），
    # 还是根本没测过。而这三种情形对「这张图能推翻自己」这个主张
    # 意味着完全不同的东西：
    #
    #   ① 图很干净          → 主张成立，只是没触发
    #   ② 判伪通道不可达    → **主张失去可证伪性**，等于一个永不说「不」的系统
    #   ③ 没测过            → 主张未被检验，是覆盖缺口
    #
    # 2026-09-09 逐条查过（活图谱实测，见下文），答案是 ③。
    # 把这个答案连同它的原始依据一起摆出来 —— 一个「0」旁边不给解释，
    # 读者只能猜，而猜出②的人会认为整个项目是自证的。
    st.info(
        "**当前没有被证伪的边。这个 0 需要解释 —— 它有三种完全不同的含义。**",
        icon="🔍")

    _labels = C.dependency_edge_labels()
    _gap_rows: list = []
    _dist: dict = {}
    if C.neptune_online() and _labels:
        _L = ", ".join(f"'{x}'" for x in _labels)
        # 判伪的必要条件之一是**没有任何独立观测源看到过这条边**。
        # 观测源按属性标记计数（与 chaos/code/runner/edge_verification.py
        # 的 _OBSERVER_MARKERS 同源）：xray_* / nfm_* / deepflow 的
        # calls|error_rate / k8s 镜像引用 image_ref。
        _no_obs = ("r.xray_call_count IS NULL AND r.xray_last_seen IS NULL "
                   "AND r.nfm_flow_count IS NULL AND r.nfm_last_seen IS NULL "
                   "AND r.calls IS NULL AND r.error_rate IS NULL "
                   "AND r.image_ref IS NULL")
        _res = C.gquery(
            f"MATCH (s)-[r]->(t) WHERE type(r) IN [{_L}] AND {_no_obs} "
            "AND coalesce(r.verify_status,'untested')='untested' "
            "RETURN t.name AS 注入靶标, labels(t)[0] AS 靶标类型, "
            "count(*) AS 可判伪边数, collect(DISTINCT s.name) AS _obs "
            "ORDER BY 可判伪边数 DESC")
        _gap_rows = _res.get("results", []) if isinstance(_res, dict) else (_res or [])

        # ── 排除「源已不存在」的边 ────────────────────────────────────────────
        #
        # 2026-09-09：队列里有 11 条边的源是 **awesomeshop 命名空间里已删除的
        # 7 个服务**（auth / order / points / product / gateway / artillery /
        # artillery-write）。外部核实过：命名空间在集群里still存在，
        # 但 **0 个 Pod、0 个 Deployment** —— 应用被删了，只剩空命名空间。
        #
        # 这些边不属于「未测的依赖」，而是「源已不存在的边」。区别是实质的：
        #
        #   未测的依赖    依赖可能成立，只是还没有人去注入验证 → 属于待办
        #   源已不存在    依赖方本身没了，依赖不可能是 active 的 → 属于清理
        #
        # 把后者混进待办队列，会让人以为要为一个已删应用去做故障注入。
        #
        # ⚠️ 判据必须窄。**不能用「零 Pod」** —— `petstatusupdater` 零 Pod 但活着，
        # 它的运行时是 Lambda（`ServicesEks2-statusupdaterservicelambdafn…`，
        # last_seen = 今天），只是图上没连到载体。用零 Pod 会误伤所有
        # Lambda / Fargate 支撑的服务。
        #
        # 所以这里按**命名空间白名单**排除，而且只列已经在集群里核实过的那一个。
        # 加新的命名空间之前必须同样核实一次（`list_k8s_resources` 查 Pod 与
        # Deployment 都为 0），不要靠图谱自身的 last_seen 推断 ——
        # 那正是并发会话论证过的错误：DNS 是不对称弱信号，
        # 「没观测到」推不出「不存在」。
        _DELETED_NS = ("awesomeshop",)
        _dead = C.gquery(
            f"MATCH (s)-[r]->(t) WHERE type(r) IN [{_L}] AND {_no_obs} "
            "AND coalesce(r.verify_status,'untested')='untested' "
            f"AND s.namespace IN {list(_DELETED_NS)} "
            "RETURN count(*) AS 边数, count(DISTINCT s.name) AS 源数, "
            "collect(DISTINCT s.name) AS _srcs")
        _d = (_dead.get("results") or [{}])[0] if isinstance(_dead, dict) else {}
        _dead_edges = int(_d.get("边数") or 0)
        if _dead_edges:
            _gap_rows = [r for r in _gap_rows if True]      # 靶标聚合不受影响
            st.warning(
                f"**队列里有 {_dead_edges} 条边的源已经不存在了** —— "
                f"{_d.get('源数')} 个服务在 "
                + "、".join(f"`{n}`" for n in _DELETED_NS)
                + " 命名空间里，集群实查 **0 Pod、0 Deployment**：应用已删，"
                "只剩空命名空间。\n\n"
                "它们属于**清理**，不属于待办 —— 对一个已删应用做故障注入没有意义。"
                "源：" + "、".join(f"`{n}`" for n in (_d.get("_srcs") or [])[:8]),
                icon="🧹")
            st.caption(
                "⚠️ 判据刻意用**命名空间白名单**而不是「零 Pod」："
                "`petstatusupdater` 零 Pod 但活着（运行时是 Lambda，last_seen 今天，"
                "只是图上没连到载体），用零 Pod 会误伤所有 Lambda / Fargate "
                "支撑的服务。也刻意不用图谱自身的 last_seen 推断 —— "
                "DNS 是不对称弱信号，「没观测到」推不出「不存在」。"
                "加新命名空间前必须先在集群里核实一次。")
        _tot = C.gquery(
            f"MATCH ()-[r]->() WHERE type(r) IN [{_L}] "
            f"RETURN count(*) AS 全部, "
            f"sum(CASE WHEN {_no_obs} THEN 1 ELSE 0 END) AS 零观测")
        _t = (_tot.get("results") or [{}])[0] if isinstance(_tot, dict) else {}
        _dist = {"全部": _t.get("全部") or 0, "零观测": _t.get("零观测") or 0}

    if _gap_rows and _dist.get("全部"):
        _gap_edges = sum(int(r.get("可判伪边数") or 0) for r in _gap_rows)
        g = st.columns(3)
        g[0].metric(
            "判伪通道可达的边", f"{_dist['零观测']}/{_dist['全部']}",
            help="判伪要同时满足三条：观测方退化低于阈值、**已确认注入生效**、"
                 "**且没有任何独立观测源看到过这条边**。"
                 "第三条是关键：一条被 X-Ray 或 NFM 看到过的边，"
                 "即使注入后调用方毫无反应，正确结论也是「这是 soft dependency」"
                 "而不是「边不存在」—— 两者在干预数据上完全同形。")
        g[1].metric(
            "其中从未做过注入实验", _gap_edges,
            help="这些边**可以**被证伪，只是还没有人去试。"
                 "它们同时是信息增益最高的靶标：零观测意味着"
                 "我们对它们真实性的证据最少。")
        g[2].metric(
            "需要的注入靶标", len(_gap_rows),
            help="按被注入的那一端聚合 —— 一次注入可以同时检验它的多条入边。")

        st.warning(
            f"**所以那个 0 的含义是「还没测」，不是「测不了」。**\n\n"
            f"判伪通道对 **{_dist['零观测']}/{_dist['全部']}** 条依赖边是可达的"
            f"（占 {_dist['零观测'] / _dist['全部'] * 100:.0f}%），"
            f"其中 **{_gap_edges} 条从未做过注入实验**，分布在 "
            f"{len(_gap_rows)} 个注入靶标上。下面这张表就是待办队列。",
            icon="⚠️")

        with st.expander(
                f"📋 可判伪但从未测过的边 —— 按注入靶标聚合（{len(_gap_rows)} 个）",
                expanded=True):
            st.caption(
                "一次注入检验它的**入边**（在 B 注入，看依赖 B 的那些 A 有没有反应），"
                "所以按靶标聚合就是按「要做几次实验」聚合。"
                "「观测方」列是这次注入能同时检验哪些调用方。")

            # ── 「可判伪」不等于「测得出结论」──────────────────────────────
            #
            # 判定还有一道运行时门禁：观测方基线请求数必须 >= 20，
            # 否则「零流量与健康在指标上无法区分」，只能回 inconclusive。
            # 实测 `pethistory → 数据库` 就是这样：退化 66.67% 却判不了，
            # 因为基线只有 12 个请求。
            #
            # 而这个队列里 26/57 条的观测方是 LambdaFunction、5 条是
            # StepFunction —— 它们触发稀疏，很可能凑不满 20 个请求。
            # 不说这一句，队列就在暗示 57 条都能测出结论。
            _by_src = C.gquery(
                f"MATCH (s)-[r]->(t) WHERE type(r) IN [{_L}] AND {_no_obs} "
                "AND coalesce(r.verify_status,'untested')='untested' "
                "RETURN labels(s)[0] AS 观测方类型, count(*) AS 边数 "
                "ORDER BY 边数 DESC")
            _src_rows = (_by_src.get("results") or []
                         if isinstance(_by_src, dict) else (_by_src or []))
            if _src_rows:
                _steady = sum(int(r.get("边数") or 0) for r in _src_rows
                              if r.get("观测方类型") in ("Microservice", "Deployment"))
                _sparse = sum(int(r.get("边数") or 0) for r in _src_rows) - _steady
                s1, s2 = st.columns(2)
                s1.metric(
                    "观测方有稳定流量", _steady,
                    help="观测方是 Microservice / Deployment —— 有持续请求流，"
                         "能满足「观测方基线请求 ≥ 20」这道门禁，注入后测得出结论。")
                s2.metric(
                    "观测方触发稀疏", _sparse,
                    help="观测方是 Lambda / StepFunction / SNS —— 触发稀疏，"
                         "很可能凑不满 20 个基线请求，于是即使注入了也只能回 "
                         "inconclusive（零流量与健康在指标上无法区分）。"
                         "这些边要先制造流量才验得动。")
                st.caption(
                    "⚠️ **「可判伪」不等于「测得出结论」。** 判定还有一道运行时门禁："
                    "观测方基线请求必须 ≥ 20 —— 实测 `pethistory → 数据库` 退化 "
                    "66.67% 却判不了，就是因为基线只有 12 个请求。"
                    f"上面 {_sparse} 条的观测方触发稀疏，"
                    "对它们要先造流量、再注入，否则跑了也是 inconclusive。")
                st.dataframe(
                    C.df([{"观测方类型": r.get("观测方类型"),
                           "边数": int(r.get("边数") or 0),
                           "流量": ("稳定" if r.get("观测方类型")
                                    in ("Microservice", "Deployment")
                                    else "稀疏 —— 需先造流量")}
                          for r in _src_rows]),
                    width="stretch", hide_index=True)

            st.dataframe(
                C.df([
                    {"注入靶标": r.get("注入靶标"),
                     "靶标类型": r.get("靶标类型"),
                     "可判伪边数": int(r.get("可判伪边数") or 0),
                     "观测方": "、".join(str(x) for x in (r.get("_obs") or [])[:4])}
                    for r in _gap_rows
                ]),
                width="stretch", hide_index=True)
            st.caption(
                "⚠️ 这张表**不是**说这些边是假的 —— 是说它们**还没被检验过**，"
                "而且它们是少数几类**能**被检验出假的边。"
                "一条边可以完全真实却零观测（例如 DNS 解析派生的边："
                "看到了解析，看不到流量）。")
    else:
        st.caption(
            "离线模式下无法算判伪覆盖 —— 这一段需要逐条查边的观测标记属性。"
            "在线时它会回答「那个 0 到底是没测过，还是测不了」。")

# ── 已确认的边 ────────────────────────────────────────────────────────────────
confirmed = [e for e in decided if e.get("status") == "confirmed"]
st.markdown("---")
st.subheader(f"✅ 已确认的边（{len(confirmed)}）")
if confirmed:
    st.caption(
        "确认不只是「是/否」，还带**影响强度**（观测方退化百分比）——"
        "这是纯观测型依赖图给不出的信息：它们知道 A 调用了 B，但不知道 B 挂了 A 会坏到什么程度。"
    )
    st.dataframe(
        C.df([
            {
                "源": e.get("source"),
                "边": e.get("edge_type"),
                "目标": e.get("target"),
                "观测方退化%": e.get("degradation"),
                "置信度": e.get("confidence"),
                "证据通道": e.get("evidence_channel") or "—",
                "实验": e.get("experiment"),
            }
            for e in confirmed
        ]),
        width="stretch",
        hide_index=True,
    )
    with st.expander("「证据通道」是什么意思"):
        st.markdown(
            """
- **`both`** —— 成功率下降与吞吐塌陷都观测到，最强证据
- **`throughput_only`** —— 只有吞吐塌陷。`abort` 类故障不产生响应行，
  成功率会**稳定停在 100%**，此时吞吐是唯一信号。这类证据要求更高的门槛
  （必须超过 60%）才判确认，因为吞吐塌陷无法区分「A 坏了」和「A 的上游停止调用 A 了」。
- **`None`/空** —— 早期实验未记录该字段。
"""
        )
else:
    st.info("当前没有已确认的边。")

# ── 未定的边 ──────────────────────────────────────────────────────────────────
inconclusive = [e for e in decided if e.get("status") == "inconclusive"]
if inconclusive:
    st.markdown("---")
    with st.expander(f"⚠️ 未定的边（{len(inconclusive)}）—— 为什么「不下结论」本身是设计"):
        st.markdown(
            """
落在 **5%–20%** 退化区间的边一律判 `inconclusive`，不判证伪。原因：
重试、熔断、缓存都会掩盖真实依赖——在这个区间证伪，会**删掉一条真实存在的边**。

另外两种强制 `inconclusive` 的情况：

1. **观测方流量不足**（< 20 请求）。指标采集在无数据时会回退成
   `success_rate=100 / total=0`，使「零流量」和「健康」无法区分。
2. **注入是否生效未知**。没有「注入确实生效」的证据时，绝不允许判证伪——
   否则「故障根本没打进去」会被误读成「依赖不存在」。
"""
        )
        st.dataframe(
            C.df([
                {
                    "源": e.get("source"),
                    "边": e.get("edge_type"),
                    "目标": e.get("target"),
                    "退化%": e.get("degradation"),
                    "理由": e.get("reason"),
                }
                for e in inconclusive
            ]),
            width="stretch",
            hide_index=True,
        )

# ── 按边类型下钻 ──────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("按边类型下钻")

by_type = vdata.get("by_edge_type_status", []) or []
if by_type:
    types = sorted({r["edge_type"] for r in by_type})
    pick = st.multiselect("筛选边类型", types, default=types)
    rows = [r for r in by_type if r["edge_type"] in pick]

    # 转成矩阵：行=边类型，列=状态
    matrix: dict = {}
    for r in rows:
        matrix.setdefault(r["edge_type"], {})[r["status"]] = r["c"]
    table = []
    for et in sorted(matrix):
        d = matrix[et]
        tot = sum(d.values())
        dec = d.get("confirmed", 0) + d.get("refuted", 0)
        table.append({
            "边类型": et,
            "✅ 确认": d.get("confirmed", 0),
            "❌ 证伪": d.get("refuted", 0),
            "⚠️ 未定": d.get("inconclusive", 0),
            "⬜ 未测": d.get("untested", 0),
            "合计": tot,
            "已验证率": f"{dec / tot * 100:.1f}%" if tot else "—",
        })
    st.dataframe(C.df(table), width="stretch", hide_index=True)

    chart = {r["边类型"]: r["合计"] for r in table}
    if chart:
        st.bar_chart(chart, height=220)

# ── 判定规则 ──────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("判定规则（全部来自契约，不是文档里的说法）")

rub = C.verification_rubric()
th = rub.get("thresholds", {})
w = rub.get("evidence_weights", {})

r1, r2 = st.columns(2)
with r1:
    st.markdown("**阈值**")
    st.markdown(
        f"""
| 项 | 值 | 含义 |
|---|---|---|
| `confirm_degradation_pct` | **{th.get('confirm_degradation_pct')}%** | 观测方退化 ≥ 此值 → 确认 |
| `refute_degradation_pct` | **{th.get('refute_degradation_pct')}%** | 退化 ≤ 此值**且注入已确认生效** → 证伪 |
| `hard_degradation_pct` | **{th.get('hard_degradation_pct')}%** | 仅吞吐通道时需超过的更高门槛 |
| `min_observation_requests` | **{th.get('min_observation_requests')}** | 观测方流量低于此值 → 强制未定 |
| `stale_verification_seconds` | **{th.get('stale_verification_seconds')}** | 超过此秒数的验证视为过期需重测 |
"""
    )
with r2:
    st.markdown("**证据权重（对数几率累加后过 sigmoid）**")
    st.markdown(
        f"""
| 证据类型 | 权重 |
|---|---|
| 静态声明（每个源） | `{w.get('static_declaration')}` |
| 被动观测（每个源） | `{w.get('observed_per_source')}` |
| 被动观测**上限** | `{w.get('observed_cap')}` |
| **主动干预确认** | `{w.get('intervention_confirmed')}`（**无上限**） |
| **主动干预证伪** | `{w.get('intervention_refuted')}` |
"""
    )
    st.info(
        "被动观测有上限、主动干预没有——因为多个被动源之间**并不独立**"
        "（同一份流量被三个采集器各看一遍，不等于三份独立证据），"
        "而主动干预是可重复的实验。",
        icon="⚖️",
    )

st.markdown("**写入权限**")
st.caption(
    f"这些 `verify_*` 属性只有 `{'`, `'.join(rub.get('authority', []) or ['—'])}` 有权写入，"
    f"其余写入方一律被契约挡掉。属性清单："
    + "、".join(f"`{a}`" for a in (rub.get("attrs", []) or []))
)

st.markdown("---")
st.caption(
    "⚠️ 关于实现的一处坑：这些属性必须用普通 `.property()` 写入，"
    "不能用 `property(single, ...)` —— Neptune 拒绝在边属性上使用 cardinality，"
    "而早期实现把这个异常吞成了一行日志，导致 21 个实验跑完、19 条边的属性全是 0。"
)
