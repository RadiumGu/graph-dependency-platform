"""10_Compliance_Report.py — 合规依赖关系报告导出。

## 为什么这一页值得单独存在

前面几页回答「我们知道什么」（拓扑、验证、根因）。这一页回答**「怎么交出去」**——
把图谱变成能放到监管/审计面前的东西，并且**把边界一起交出去**。

## 这一页刻意不做的事

**不做 DORA Art. 28 信息登记册。** 那份由 ITS (EU) 2024/2956 规定 15 张互联模板、
以 xBRL-CSV 年度报送，字段是合同编号、通知期、适用法律、20 位 LEI、退出策略 ——
本质是合同清单不是图，本平台没有合同实体。把「做不到」明说出来，比含糊地
暗示什么都能做更可信。

## 页面与导出层的关系

全部口径来自 `compliance_export` 包，本页只做渲染。**不在这里重算任何数字** ——
否则页面与导出的 CSV 会各说一套，而那正是跨表交叉校验要防的事。
"""
import os
import sys

# 页面被单独执行时 demo/ 不在 sys.path 上，显式补上。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _common as C  # noqa: E402

import streamlit as st  # noqa: E402

C.page_setup("合规报告", icon="📋")
C.sidebar()

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

st.title("📋 合规依赖关系报告")
st.markdown(
    "把依赖图谱导出成可交给监管/审计的报告。**只读**，不写图谱。"
    "全部口径来自 `compliance_export` 包 —— 本页只渲染，不重算。"
)

# ── 监管要求分三类 ───────────────────────────────────────────────────────────
st.markdown("### 监管对依赖关系的要求不是一类，是三类")
st.caption("把三类混为一谈是这个领域最常见的错误。")

st.markdown(
    """
| 类别 | 代表条文 | 本质 | 本平台 |
|---|---|---|---|
| **A 穿透式依赖映射** | DORA Art. 8(4)、BCBS POR 原则四、SYSC 15A.4.1R、关基条例第九条 | **图问题** —— 条文动词是 `map`，要求穿透到「资产之间的链接与互赖」 | ✅ 本页做这个 |
| B 合同型登记册 | DORA Art. 28/29/31、OCC 2023-17 / FFIEC App. J | 合同数据集，零流量也必须登记 | ❌ 结构上做不到，也不该做 |
| C 容忍度阈值 | SYSC 15A.2.5R / 2.9R | 业务判断 + 阈值检测 | ⚠️ 承载能力有，阈值待业务方填 |
"""
)

with st.expander("BCBS 自己点名的行业难点，正是 A 类和 C 类", expanded=False):
    st.markdown(
        "> *The mapping of interconnections and interdependencies for critical "
        "operations, and the definition of tolerances for disruption to these "
        "operations **are the most common challenges that banks face** when "
        "adopting the Principles.*\n>\n"
        "> —— BIS newsletter 34"
    )
    st.markdown(
        "**不是** B 类的合同登记册。填表工具能满足 B，满足不了 A —— "
        "这是本平台的差异化位置。"
    )
    st.markdown("最对位的一条原文是 **DORA Art. 8(4)**：")
    st.markdown(
        "> *Financial entities shall identify all information assets and ICT "
        "assets... and shall map those considered critical. **They shall map the "
        "configuration of the information assets and ICT assets and the links and "
        "interdependencies between the different information assets and ICT "
        "assets.***"
    )
    st.markdown(
        "三个动词递进：`identify` → `map those considered critical` → "
        "`map the links and interdependencies`。"
        "**第三步在数据结构上就是一张图的边集，清单做不出来。**"
    )

st.markdown("---")

# ── 取快照 ───────────────────────────────────────────────────────────────────
if not C.neptune_online():
    st.warning(
        "Neptune 不可达 —— 合规报告**刻意不提供 fixture 降级**。\n\n"
        "其它页面用离线快照演示是合理的（看的是结构与交互），"
        "但一份合规报告的价值全部建立在「数字来自活图谱、且锚定某个基准时刻」上。"
        "拿固定快照冒充实时导出，是这类工具最不该犯的错。"
    )
    st.info(
        "样例产出已入版本库，可直接查看：\n\n"
        "`compliance_export/samples/SAMPLE-compliance-dependency-report.md`\n\n"
        "`compliance_export/samples/SAMPLE-compliance-dependency-report.csv`"
    )
    st.stop()


@st.cache_data(ttl=120, show_spinner="正在取快照…")
def _snapshot():
    """取一次快照并算好分列统计。

    TTL 只有 120 秒 —— 活图谱在被 ETL 持续改写（实测数分钟内 LocatedIn
    从 1060 变 1054），缓存太久会让页面显示的基准时刻与实际数据脱节。
    """
    from compliance_export import Breakdown, render_markdown, render_csv, take_snapshot

    snap = take_snapshot()
    bd = Breakdown(snap.function_mapping)
    return {
        "taken_at": snap.taken_at,
        "capability_count": snap.capability_count,
        "dependency_count": snap.dependency_count,
        "dep_labels": snap.dependency_edge_labels,
        "function_mapping": snap.function_mapping,
        "concentration": snap.concentration,
        "hosting": snap.hosting_layer,
        "capability_meta": snap.capability_meta,
        "reachability": getattr(snap, "reachability", []),
        "dimensions": [(t, w, [(b.value, b.count, b.share_pct) for b in bs])
                       for _f, t, w, bs in bd.dimensions()],
        "self_check": bd.self_check(),
        "markdown": render_markdown(snap, bd),
        "csv": render_csv(snap),
    }


try:
    D = _snapshot()
except Exception as exc:  # noqa: BLE001
    st.error(f"取快照失败：{exc}")
    st.stop()

if D["self_check"]:
    # 比例算错的报告比没有报告更糟 —— 不展示，直接报错。
    st.error("分列统计自检失败，报告不可采信：")
    for p in D["self_check"]:
        st.code(p)
    st.stop()

# ── 快照时刻 ─────────────────────────────────────────────────────────────────
st.markdown("### 快照时刻（基准日）")
c1, c2, c3, c4 = st.columns(4)
c1.metric("快照时刻 (UTC)", D["taken_at"].replace("T", " ").rstrip("Z"))
c2.metric("业务能力", D["capability_count"])
c3.metric("一跳依赖边", D["dependency_count"])
c4.metric("依赖边类型", f"{len(D['dep_labels'])} 种")
st.caption(
    "本页所有表格取自**同一次会话、同一快照时刻** —— 跨表数字可交叉校验。"
    "DORA 的登记册用 12/31 作为统一基准日正是为此，"
    "ESAs 点名的失败模式里就有「跨模板基准日不一致」。"
)
st.caption(
    "依赖边清单来源：`graph_contract.dependency_edge_labels()`（契约，单一来源）—— "
    + " / ".join(D["dep_labels"])
)

st.markdown("---")

# ── 三栏分列 ─────────────────────────────────────────────────────────────────
st.markdown("### 三栏分列统计")
st.caption(
    "三个维度回答三个不同问题，**刻意不合并成单一覆盖率** —— "
    "平均会让「已声明但从未观测」与「已观测但从未验证」互相抵消，"
    "而这是监管审查最容易挑的点。"
)
cols = st.columns(3)
for col, (title, why, buckets) in zip(cols, D["dimensions"]):
    with col:
        st.markdown(f"**{title}**")
        st.caption(why)
        st.dataframe(
            [{"取值": v, "条数": n, "占比": p} for v, n, p in buckets],
            hide_index=True, width="stretch",
        )

st.markdown("---")

# ── 功能映射表 ───────────────────────────────────────────────────────────────
st.markdown("### 功能映射表")
st.caption(
    "对应 DORA Art. 8(1)（business functions ← 支撑资产 ← 其 dependencies）与 "
    "8(4)，以及 SYSC 15A.4.1R 的 technology 维度。"
    "`verify` 与 `kind` 是**强制列**：前者回答「凭什么说这条依赖成立」"
    "（SYSC 15A.5.3R 的测试证据），后者回答「配置声明的还是运行时观测的」。"
)
reach = {r["capability"]: r for r in D["reachability"]}
by_cap = {}
for r in D["function_mapping"]:
    by_cap.setdefault(r["capability"], []).append(r)

tabs = st.tabs([f"{cap}（{len(rows)} 条）" for cap, rows in sorted(by_cap.items())])
for tab, (cap, rows) in zip(tabs, sorted(by_cap.items())):
    with tab:
        rc = reach.get(cap, {})
        st.caption(
            f"tier={rows[0].get('tier') or '–'} · "
            f"一跳依赖 {len(rows)} 条 · "
            f"多跳可达 {rc.get('reachable_objects', '?')} 个对象"
            f"（{rc.get('paths', '?')} 条路径）"
        )
        st.dataframe(
            [{"服务": r["service"], "边类型": r["edge_type"],
              "被依赖对象": r["target"], "对象类型": r["target_label"],
              "scope": r.get("target_scope"), "kind": r.get("dependency_kind"),
              "verify": r.get("verify_status"), "conf": r.get("confidence"),
              "source": r.get("source"), "drift": r.get("drift_status")}
             for r in rows],
            hide_index=True, width="stretch",
        )

st.markdown("---")

# ── 集中度 ───────────────────────────────────────────────────────────────────
st.markdown("### 技术集中度")
st.caption(
    "对应 SYSC 15A.2.7G(10) 的原文要求：评估 *the potential aggregate impact of "
    "disruptions to multiple important business services, in particular where such "
    "services rely on **common operational resources** as identified by the firm's "
    "mapping exercise*。也是 DORA Art. 29/31 的技术输入。"
)
total_caps = D["capability_count"]
st.dataframe(
    [{"被依赖对象": r["target"], "类型": r["target_label"],
      "scope": r.get("target_scope"),
      "支撑业务功能": f"{r['capability_count']} / {total_caps}",
      "边数": r["edge_count"]}
     for r in D["concentration"]],
    hide_index=True, width="stretch",
)
st.info(
    "⚠️ 这是**技术**集中度，不是供应商集中度。DORA Art. 29/31 要的是供应商层面的"
    "集中度与分包链，需要 `Vendor` 法人实体节点才能回答 —— 本平台当前只有技术对象。"
    "第四方分包链在埋点边界外，**结构上做不到**。"
)

st.markdown("---")

# ── 承载层 ───────────────────────────────────────────────────────────────────
st.markdown("### 基础设施承载层（单列，非服务消费关系）")
st.caption(
    "这些边**不是**依赖。它们普遍为真、对几乎每个资源都成立，因而不携带判别信息 —— "
    "算进依赖会让 Region 成为所有东西的咽喉点，淹没可操作发现。"
    "但 EKS 集群与负载均衡器在 DORA 视角下确实是关键 ICT 服务，所以在此单列。"
)
st.dataframe(
    [{"边类型": r["edge_type"], "条数": r["count"], "性质": r["nature"]}
     for r in D["hosting"]],
    hide_index=True, width="stretch",
)

# ── 容忍度 ───────────────────────────────────────────────────────────────────
st.markdown("### 业务能力与容忍度阈值")
st.dataframe(
    [{"业务能力": r["capability"], "tier": r.get("tier"),
      "impact_tolerance_seconds": r.get("impact_tolerance_seconds"),
      "rto_target_seconds": r.get("rto_target_seconds")}
     for r in D["capability_meta"]],
    hide_index=True, width="stretch",
)
_no_tol = [r for r in D["capability_meta"]
           if r.get("impact_tolerance_seconds") is None]
if _no_tol:
    st.warning(
        f"**impact tolerance 未设定（{len(_no_tol)}/{len(D['capability_meta'])} 个为空）。**"
        " SYSC 15A.2.5R 要求 firm *must* set an impact tolerance for each important "
        "business service。**本报告不得出现任何「未越界」表述** —— 没有阈值就是"
        "没有阈值，不能用「没检测到越界」掩盖「压根没有阈值可比」。"
    )
st.caption(
    "`impact_tolerance` 与 `rto_target` **必须分列**：RTO 是恢复某个流程的目标时间"
    "（内部视角），impact tolerance 是 IBS 的最大可容忍中断（外部危害视角），"
    "两者可以差数倍。混用是监管审查重点。"
)

st.markdown("---")

# ── 下载 ─────────────────────────────────────────────────────────────────────
st.markdown("### 导出")
stamp = D["taken_at"].replace(":", "").replace("-", "")
c1, c2 = st.columns(2)
with c1:
    st.download_button(
        "⬇️ 下载完整报告（Markdown）",
        data=D["markdown"].encode("utf-8"),
        file_name=f"compliance-dependency-report_{stamp}.md",
        mime="text/markdown", width="stretch",
    )
    st.caption("含全部四份表格 + 由数据算出的局限披露。")
with c2:
    st.download_button(
        "⬇️ 下载功能映射表（CSV）",
        data=D["csv"].encode("utf-8"),
        file_name=f"compliance-dependency-mapping_{stamp}.csv",
        mime="text/csv", width="stretch",
    )
    st.caption("页首注释行记录快照时刻与依赖边清单来源，便于审计取证。")

st.caption(
    "文件名带快照时刻 —— SYSC 15A.6.2R 要求保存 **each version** 的记录满 6 年，"
    "同名覆盖会让历史版本消失。"
)

with st.expander("📌 必须随报告一同披露的局限（报告里已自动生成）", expanded=False):
    st.markdown(
        "披露内容**由快照数据算出，不手写** —— 手写的披露会过期。"
        "确证率现算、缺失维度现查、impact tolerance 是否为空现判。"
    )
    st.markdown("下面是本次快照对应的实际披露文本：")
    _marker = "## 必须随报告一同披露的局限"
    _md = D["markdown"]
    st.markdown(_md[_md.index(_marker):] if _marker in _md else "（未生成）")
