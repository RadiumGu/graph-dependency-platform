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
    st.info("当前没有被证伪的边。")

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
