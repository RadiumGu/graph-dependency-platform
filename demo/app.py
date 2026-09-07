"""
app.py — 首页。

改造要点（2026-09-05）：原首页是一份功能目录（5 个指标 + 功能表 + 技术栈 +
ASCII 架构图），把本项目最强的东西——依赖边可被故障注入证伪——完全埋掉了，
且三个核心数字全错（22/19/18，实际 39/29/22）。

现在的结构按 todo/project-intro-outline_20260831-0750.md 的叙事骨架：
先立主张 → 抛两个观众答不上来的问题 → 亮实时计分板 → 对照四种业界范式 →
点出缺口 → 上证据 → 引导互动。

所有数字均从 profiles/graph_contract.yaml 与活图谱现算，代码中不含计数字面量。
"""
import _common as C

import streamlit as st

C.page_setup("这张图是真的吗", icon="🎯")
C.sidebar()

# ── 主张 ──────────────────────────────────────────────────────────────────────
st.title("🎯 这张依赖图，是真的吗？")
st.markdown(
    "> **市面上的依赖图都在回答「我看到了什么」。这个项目回答的是「我看到的是真的吗」。**"
)

q1, q2 = st.columns(2)
q1.warning("**问题一**　你怎么知道图上那条边是真的？")
q2.warning("**问题二**　如果图上少了一条边，你有任何机制会发现吗？")
st.caption(
    "绝大多数依赖拓扑工具对这两个问题都没有答案——不是做得不好，是**架构上无法回答**。"
    "下面的计分板是这个项目对第一个问题的回答。"
)

# ── 实时计分板（核心）────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("依赖边验证计分板")

vdata, vmode = C.verification_data()
C.mode_badge(vmode, "验证数据")

if vdata:
    counts = vdata.get("totals_by_status", {})
    total = vdata.get("dependency_edge_total", 0)
    C.status_chips(counts, total)

    refuted = counts.get("refuted", 0)
    if refuted:
        st.error(
            f"**其中 {refuted} 条边被证伪** —— 图上声称存在、但故障注入证明它不成立。"
            "这是任何竞品都拿不出来的东西：一个能推翻自己的依赖图。",
            icon="❌",
        )
    st.caption(
        "「已验证」= confirmed + refuted，即真正做过主动干预并得出结论的边。"
        "untested 不是缺陷，而是诚实——业界所有依赖图的这个数字都是 100%，只是没人算过。"
    )
else:
    st.warning("暂无验证数据。")

# ── 图谱规模（动态）──────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("图谱规模")

gstats, gmode = C.graph_stats()
sc = C.schema_counts()
qc = C.query_catalog_info()
fc = C.fault_catalog_counts()

# 附注一律进 help=，不进第三个位置参数 —— 那个位置是 delta，会渲染成
# 带箭头的涨跌（还带绿/红配色）。「预置查询 24 ↑ 确定性 Cypher」是在
# 首页最显眼处凭空造一个趋势，而这一页的主张恰恰是「摆出来的数字能被核对」。
# 门禁 tests/test_58_metric_delta_not_annotation.py。
m = st.columns(6)
m[0].metric("图节点", f"{gstats.get('node_total', 0):,}",
            help=f"活图谱里的节点总数，其中 "
                 f"{gstats.get('node_label_count', 0)} 种标签在用。")
m[1].metric("图边", f"{gstats.get('edge_total', 0):,}",
            help=f"活图谱里的边总数，其中 "
                 f"{gstats.get('edge_type_count', 0)} 种关系类型在用。")
m[2].metric("契约节点类型", sc["node_types"],
            help="契约 `profiles/graph_contract.yaml` 里**声明**的节点类型数。"
                 "与左边「图节点」的在用种数对账 —— 两者不一致说明有声明未落地"
                 "或有类型未声明。")
m[3].metric("契约边类型", sc["edge_types"],
            help=f"契约声明的关系类型数，其中 {sc['dependency_edge_types']} 种是"
                 "**依赖边**（只有依赖边才带故障注入验证判定）。")
m[4].metric("预置查询", qc["count"],
            help="固定 openCypher，无 LLM 参与，同参数同结果。见「查询库」页。")
m[5].metric("故障目录", fc.get("total", 0),
            help=f"Chaos Mesh {fc.get('chaosmesh', 0)} 个 K8s CRD ＋ "
                 f"AWS FIS {fc.get('fis', 0)} 个单动作 ＋ "
                 f"{fc.get('fis_scenarios', 0)} 个复合场景。")

if gstats.get("node_label_count") and sc["node_types"]:
    if gstats["node_label_count"] == sc["node_types"]:
        st.caption(
            f"✅ 契约声明的 {sc['node_types']} 种节点类型**全部**在活图谱里有实例——"
            "声明与现实没有分叉。"
        )
    else:
        st.caption(
            f"契约声明 {sc['node_types']} 种节点类型，活图谱出现 {gstats['node_label_count']} 种。"
            "差值通常是仅追加的事件日志类型或尚未产生实例的新类型。"
        )

# ── 四种业界范式 ──────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("业界怎么做的：四种范式")
st.caption(
    "多个数据源写同一张图，**冲突时谁赢、什么时候算一条边消失了**——"
    "这一个问题分出了四种范式。"
)

st.markdown(
    """
| | 范式 | 代表 | 立场 |
|---|---|---|---|
| **A** | 属性级规则仲裁 | ServiceNow IRE | 预先声明每个字段谁能写，低优先级的写入直接挡掉 |
| **B** | 声明式单一权威 + 派生关系 | Backstage | 从结构上避免冲突：一个实体，一个 owner |
| **C** | 幂等 upsert + 全量扫描时间戳收敛 | Cartography (CNCF) | 不仲裁——本轮看到就打时间戳，没打上的即视为消失，删除 |
| **D** | 持久化实体 + 显式 TTL | New Relic / Dynatrace | 边是有生命周期的对象，每种类型自己声明能活多久 |

**A + B 解决「防止冲突」，C + D 解决「管理消失」。**
"""
)
st.info(
    "**值得单独点出的事实**：Datadog、OTel service graph、Elastic APM **根本没有持久化的边实体**——"
    "一条边「是否存在」等价于「查询窗口内是否观测到」。所以「这条边消失了吗」这个问题，"
    "在那些系统里不是答不好，而是**无法表达**。只有 New Relic 与 Dynatrace 把边建模成生命周期对象。",
    icon="💡",
)

# ── 缺口 ──────────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("四种范式都没解的那个缺口")
st.error(
    "**这四种范式都在裁决「不同数据源之间谁对」，没有一个在验证「这些源合起来对不对」。**",
    icon="🕳️",
)
g1, g2 = st.columns(2)
g1.markdown(
    """
**学术侧的两个硬停止**

- eBPF 依赖发现（arXiv:2608.04413）只复现了一个**已知的** 20 服务测试床拓扑
  —— 证明的是「能复现」，不是「能发现未知边」
- ICPE'24 Casper：阿里 2021 trace 在严格口径下只能重建到 **58.32%**
"""
)
g2.markdown(
    """
**我们的选择**

> 我们没有去做第五个数据源——我们去做了**证伪**。

对边 `A → B`：**在 B 注入故障，观测 A**。
A 退化 → 边成立（且得到影响强度）；A 毫无反应 → 边可疑。
"""
)
st.caption(
    "本项目自己的代码最初把故障注入在 A —— 于是历史上 72 个实验「全部通过、零失败」，"
    "因为那个判据根本没有区分能力。这个错误本身就是最好的说明：判据错了，全绿也毫无意义。"
)

# ── 证据卡 ────────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("这套方法查出了什么别人查不出的东西")
st.caption("每条都带出处标签。这些缺陷的共同点：读代码、跑单元测试都发现不了。")

EVIDENCE = [
    ("85%", "单一观测源导致的假阴性",
     "接入 X-Ray 之前，漂移判定只用 DNS。一个 24 小时内被调用 **11,512 次** 的依赖在 DNS 窗口里完全不可见，"
     "导致 26 条边里 22 条（85%）被误标为 `declared_not_observed`。加一个观测源，85% 的判定被推翻。",
     "代码注释记录的实测"),
    ("43%", "唯一没有门禁的字段上的漂移",
     "`source` 字段在契约里声明了，但直到 2026-08-31 都没有校验。全图普查查出 **1240 条** 取值不合法的边与节点"
     "（`eks-etl` 1228 / `aws-etl-static` 3 / `deepflow` 8 / `manual` 1）。同一份 YAML 里，"
     "**有门禁的部分漂移为零**。",
     "实测"),
    ("183", "条边因为一次不带标签的查询而指错源端点",
     "`find_vertex_by_name()` 不带标签，而名字在不同标签间会重复（**12 组**，例如 `gateway-service` "
     "同时是 Deployment、K8sService、Microservice）。正确的 `Microservice-[RunsOn]->Pod` 只有 36 条，错源 173 条。",
     "实测"),
    ("40×", "count 正确，join 却放大 40 倍",
     "边属性上的 SET 累积：`LambdaFunction` 数出来是 31，正确；但加上 `WHERE last_scanned IS NOT NULL` "
     "就炸成 **9 个真实节点 / 1,253 行**，最坏的单个节点有 172 个不同的 `last_scanned` 值。"
     "任何「先 count 看看对不对」的自检都发现不了它。",
     "代码注释记录的实测"),
    ("100% → 盲", "`abort` 类故障下成功率是瞎的",
     "注入 abort 后被注入方成功率**稳定停在 100%**，而请求数从 2240 掉到 56（**-97%**）——"
     "因为 abort 不产生响应行，成功率无从下降。判据改为「成功率下降」与「吞吐塌陷」取最大值后才看得见。",
     "代码"),
    ("1 年", "一条写入路径失败了近一年，而功能「一直存在」",
     "Neptune 拒绝在边属性上使用 cardinality（`400 UnsupportedOperationException`），"
     "而这个异常被吞成了一行日志。结果 **21 个实验跑完，19 条 `Calls` 边的混沌属性全是 0**。",
     "代码注释记录的实测"),
]

for i in range(0, len(EVIDENCE), 3):
    cols = st.columns(3)
    for col, (num, headline, body, tag) in zip(cols, EVIDENCE[i:i + 3]):
        with col:
            with st.container(border=True):
                st.markdown(f"### {num}")
                st.markdown(f"**{headline}**")
                st.caption(body)
                st.markdown(f"`[{tag}]`")

st.success(
    "**贯穿全项目的一条结论**：至今查出的缺陷，**没有一个**是「少采了数据」。"
    "全部落在三类——粒度错配、写了但没人读、身份不唯一。"
    "**瓶颈在数据契约，不在采集覆盖面。**",
    icon="🧭",
)

# ── 引导互动 ──────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("动手试试")
st.caption("下面三个入口都**不需要你有任何 AWS 凭证**——环境不可达时会自动展示活图谱的真实快照。")

t1, t2, t3 = st.columns(3)
with t1:
    with st.container(border=True):
        st.markdown("#### 🎯 边验证")
        st.caption("看那 3 条被证伪的边具体是什么，以及判定它们用的证据权重与阈值。")
        C.page_link("pages/1_Edge_Verification.py", "→ 打开边验证", width="stretch")
with t2:
    with st.container(border=True):
        st.markdown("#### 📚 查询库")
        st.caption(f"{qc['count']} 条预置图查询，选一条填参数就能跑。确定性 Cypher，**不用 AI**。")
        C.page_link("pages/2_Query_Catalog.py", "→ 打开查询库", width="stretch")
with t3:
    with st.container(border=True):
        st.markdown("#### 🤖 Agent 依赖")
        st.caption("GenAI agent 的依赖也进了同一张图：谁委派谁、调了哪个工具、检索了哪个知识库。")
        C.page_link("pages/5_Agent_Dependencies.py", "→ 打开 Agent 依赖", width="stretch")

# ── 契约摘要 ──────────────────────────────────────────────────────────────────
st.markdown("---")
with st.expander("图谱契约：这张图凭什么可信（点开看门禁细节）"):
    st.markdown(
        f"""
契约文件 `profiles/graph_contract.yaml`（版本 `{sc['contract_version']}`）是 **ETL 写入时实际读取**
的那一份，不是文档。它规定：
"""
    )
    cc1, cc2 = st.columns(2)
    with cc1:
        st.markdown(f"**合法数据源白名单（{sc['sources']} 个）**")
        st.caption(
            "写入时校验 `source` 取值；这条门禁上线前，有 4 种未声明取值、1240 条数据漂移。"
        )
        st.code("\n".join(C.source_vocabulary()), language="text")
    with cc2:
        rub = C.verification_rubric()
        st.markdown("**证据权重**")
        st.caption("主动干预一次，比被动观测多少次都值钱——观测有上限，干预没有。")
        w = rub.get("evidence_weights", {})
        st.markdown(
            f"""
| 证据类型 | 权重 |
|---|---|
| 静态声明（每个源） | `{w.get('static_declaration')}` |
| 被动观测（每个源） | `{w.get('observed_per_source')}` |
| 被动观测**上限** | `{w.get('observed_cap')}` |
| **主动干预确认** | `{w.get('intervention_confirmed')}`（无上限） |
| **主动干预证伪** | `{w.get('intervention_refuted')}` |
"""
        )

    st.markdown("**节点类型 · 边类型明细**")
    tab_n, tab_e, tab_w = st.tabs(
        [f"节点类型（{sc['node_types']}）", f"边类型（{sc['edge_types']}）", "按 writer 分组"]
    )
    with tab_n:
        st.dataframe(C.df(C.node_type_table()), width="stretch", hide_index=True)
    with tab_e:
        st.dataframe(C.df(C.edge_type_table()), width="stretch", hide_index=True)
        st.caption("「依赖边」列打勾的才参与验证——它们是「A 依赖 B」这种有方向的断言。")
    with tab_w:
        for writer, names in C.node_types_by_writer().items():
            st.markdown(f"**{writer}** — {len(names)} 种")
            st.caption("　".join(f"`{n}`" for n in names))

st.markdown("---")
st.caption(
    f"Graph Dependency Platform · {C.REGION} · Neptune openCypher · "
    f"契约版本 {sc['contract_version']} · 数字均由契约与活图谱现算"
)
