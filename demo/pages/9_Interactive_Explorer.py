"""9_Interactive_Explorer.py —— 从一个服务出发，顺着依赖走。

## 这一页回答的问题

「petsite 挂了会影响谁」/「它依赖谁」—— 锚点 + 有界邻域 + 按需展开。
和 3_Graph_Explorer 的分工：那一页是**系统全貌**（二维编码：依赖层级 × scope
簇），这一页是**从一个点出发的牵连**。两页共用 `components/graph_svg` 渲染，
所以样式与交互一致，区别只在信息组织。

## 为什么换掉了 st-link-analysis

上一版用它（Cytoscape 封装）做双向交互，因为 pyvis 单向、选中拿不回 Python。
但它把 Cytoscape 的 `min-zoomed-font-size` 硬编码为 10 且不暴露 font-size、
不透传 cy 实例：fit 后缩放落在 0.625 以下时**节点标签被整体隐藏**，一张依赖图
连节点叫什么都读不出来，而封装内没有任何入口能绕开（收紧布局跨不过阈值，
关掉 fit 则视图不再对准内容）。

Streamlit 1.51.0 起的 Components v2 让「选中回传」成为原生能力——免 npm 构建、
frameless、双向——所以当初选那个封装的前提已经不成立。现在的渲染是自绘 SVG，
字号就是 graph.css 里的一条声明，没有别人能改它。

## 实现的交互模式（对应调研里 10 个成熟产品的共同做法）

1. **搜索优先入口** —— 先选锚点，不渲染全图
2. **渐进披露** —— 双击展开下一跳；高度数节点可「展开全部邻居」绕开每层上限
3. **选中 → 侧栏详情** —— 节点属性 + 还有多少邻居没显示 + 可下钻查询
4. **展开路径** —— 记录走过哪些节点，可一键清空回到基础图
5. **相邻高亮** —— hover / 键盘聚焦时点亮相连的边与邻居，压暗其余
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _common as C  # noqa: E402

import streamlit as st  # noqa: E402

from components import graph_svg  # noqa: E402

C.page_setup("交互探索", icon="🧭")
C.sidebar()

st.title("🧭 交互探索")
st.markdown(
    "从一个服务出发，**单击**看详情、**双击**展开它的下一跳。这是 Datadog / "
    "Neo4j Bloom / AWS graph-explorer / Kiali 的共同模式：不渲染全图，让你点着走。"
)

# ── 同一套分组配色：与 9_Interactive_Explorer 保持一致，便于对比 ──────────────
GROUPS = [
    ("业务能力", "#E69F00", {"BusinessCapability"}),
    ("入口与路由", "#56B4E9", {"LoadBalancer", "ListenerRule", "TargetGroup"}),
    ("应用与计算", "#0072B2", {
        "Microservice", "LambdaFunction", "StepFunction", "Deployment",
        "K8sService", "HPA", "Pod", "Namespace", "EKSCluster", "EC2Instance"}),
    ("数据存储", "#009E73", {
        "RDSCluster", "RDSInstance", "Database", "DynamoDBTable", "S3Bucket",
        "NeptuneCluster", "NeptuneInstance", "ECRRepository"}),
    ("消息", "#E5C100", {"SQSQueue", "SNSTopic"}),
    ("Agent / GenAI", "#CC79A7", {
        "AgentRuntime", "AgentTool", "AgentGateway", "AgentMemory",
        "KnowledgeBase", "Guardrail"}),
    ("基础设施与运维", "#8C8C8C", {
        "VPC", "Subnet", "SecurityGroup", "AvailabilityZone", "Region",
        "AWSServiceEndpoint", "Incident", "ChaosExperiment", "TopologyChange"}),
]


#: 分组名 → 颜色，供图例用（GROUPS 是按类型查的，图例按分组名查）
GROUP_COLOR_BY_NAME = {name: color for name, color, _m in GROUPS}
GROUP_COLOR_BY_NAME["未归类"] = "#B39DDB"


def group_of(label: str) -> tuple[str, str]:
    for name, color, members in GROUPS:
        if label in members:
            return name, color
    return "未归类", "#B39DDB"


# 验证状态 → 边色，与既有两页同一套语义
VS_COLOR = {
    "confirmed": ("#2E7D32", "已确认"),
    "refuted": ("#C62828", "已证伪"),
    "inconclusive": ("#EF6C00", "未定"),
    "untested": ("#9E9E9E", "未验证"),
}

DEP_EDGES = sorted(C.dependency_edge_labels())
online = C.neptune_online()


@st.cache_data(ttl=60, show_spinner=False)
def _neighbors_live(name: str, ets: tuple, limit: int) -> dict:
    """单跳邻居（无向）。查询照抄 9_Interactive_Explorer：Neptune 对带谓词的
    变长路径 + relationships() 支持有限，A 档踩过 400，所以是**迭代单跳**。"""
    if "'" in name:
        return {"results": []}
    ec = ", ".join(f"'{e}'" for e in ets)
    return C.gquery(
        f"MATCH (a)-[r]-(b) WHERE a.name = '{name}' AND type(r) IN [{ec}] "
        "RETURN coalesce(startNode(r).name, startNode(r).tool_key, startNode(r).arn) AS source, "
        "labels(startNode(r))[0] AS source_label, type(r) AS edge_type, "
        "coalesce(endNode(r).name, endNode(r).tool_key, endNode(r).arn) AS target, "
        "labels(endNode(r))[0] AS target_label, r.verify_status AS verify_status, "
        "r.verify_degradation AS deg, r.verify_last AS verify_last, "
        "r.verify_severance AS severance, r.verify_evidence_channel AS ev_channel, "
        "r.nfm_cross_az AS nfm_cross_az, r.drift_status AS drift, "
        "r.unobserved_since AS unobserved_since, r.last_seen AS last_seen, "
        "r.p99_latency_ms AS p99, r.error_rate AS error_rate, r.calls AS calls "
        f"LIMIT {int(limit)}"
    )


def _neighbors_snapshot(name: str, ets: list, limit: int) -> list:
    snap = C.fixture("sample_topology")
    lab = {n.get("name"): n.get("label") for n in snap.get("nodes", [])}
    rows = [
        dict(e, source_label=lab.get(e.get("source"), ""),
             target_label=lab.get(e.get("target"), ""))
        for e in snap.get("edges", [])
        if e.get("edge_type") in ets
        and (e.get("source") == name or e.get("target") == name)
    ]
    return rows[:limit]


def neighbors_of(name: str, ets: tuple, limit: int) -> list:
    """在线走 Neptune，取不到就退到离线快照 —— 与既有两页同一策略。"""
    if online:
        res = _neighbors_live(name, ets, limit)
        if "error" not in res:
            return res.get("results", [])
    return _neighbors_snapshot(name, list(ets), limit)


# ── 只沿**一个方向**走 ────────────────────────────────────────────────────────
#
# 上面的查询是无向的（`(a)-[r]-(b)`，与既有两页一致），BFS 时如果两个方向都收，
# 锚点就同时有上游和下游，dagre 会把它排到中间层 —— 用户选了 petsite，开局却
# 看到图的中段，找不到自己选的那个服务。试过在前端按锚点坐标 scrollLeft 对准，
# 但那是在补救一个本不该出现的布局：把锚点排在中间本身就不是我们想要的图。
#
# 改成按方向筛边：downstream 只留 source==当前节点的边，于是锚点是**唯一的源**，
# dagre 必然把它放在第 0 层、也就是 rankdir=LR 下的最左端。方向由用户选，
# 因为「它依赖谁」和「谁依赖它」是两个不同的问题（爆炸半径 vs 根因）。
def directed_rows(name: str, ets: tuple, limit: int, downstream: bool) -> list:
    rows = neighbors_of(name, ets, limit)
    key = "source" if downstream else "target"
    return [r for r in rows if r.get(key) == name]


# 关系类型筛选：契约把 29 种边分成 6 种依赖边与 23 种结构/包含边
# （RunsOn / LocatedIn / BelongsTo / Contains…）。只画依赖边是本页的默认，
# 因为把包含关系也画成箭头正是杂乱的主要来源；但允许操作者按类型再收窄，
# 「只看 Calls」和「只看 AccessesData」是两个不同的排查问题。
edge_types = st.multiselect(
    "关系类型", DEP_EDGES, default=DEP_EDGES,
    help="只列依赖边（契约里 dependency: true 的那 6 种）。结构/包含边不画： "
         "把它们也画成箭头会让图的杂乱度翻倍，而它们回答的不是「谁依赖谁」。")
if not edge_types:
    st.warning("至少选一种关系类型，否则没有边可画。")
    st.stop()

c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
with c1:
    services = C.service_names()
    if not services:
        st.error("取不到服务清单（图谱不可达且无离线快照）。")
        st.stop()
    anchor = st.selectbox("起点服务", services,
                          index=services.index("petsite") if "petsite" in services else 0)
with c2:
    hops = st.number_input(
        "跳数", min_value=1, max_value=5, value=2,
        help="层次靠跳数，不靠渲染器。注意依赖图的深度本来就浅：到 AWS 服务端点"
             "这类叶子就没有下一跳了，所以加大跳数到某一步之后不会再多出节点"
             "（离线快照上从 petsite 沿「它依赖谁」走，3 跳即到底）。"
             "真正影响规模的通常是下面的「每层上限」。")
with c3:
    limit = st.number_input(
        "每层上限", min_value=4, max_value=60, value=14,
        help="真正卡住规模的是这个，不是跳数：度数高的节点在低上限下不会全展开。"
             "选中它之后，侧栏会告出它还有多少个邻居没显示，并给一个"
             "「展开全部邻居」绕开这个上限。"
             "代价是取数走**迭代单跳**（Neptune 对带谓词的变长路径支持有限，"
             "A 档踩过 400），查询次数随前沿节点数增长，而线上是跨 VPC 访问。")
with c4:
    direction = st.radio("方向", ["它依赖谁", "谁依赖它"], index=0,
                         help="两个不同的问题：前者是爆炸半径，后者是根因方向。"
                              "沿单一方向走，锚点才会落在最左端。")
downstream = direction == "它依赖谁"

# ── 手动展开的节点集合 ───────────────────────────────────────────────────────
#
# 双击一个节点 = 把它加进这个集合，取数时对集合里的每个节点**额外再走一跳**。
# 这才是「渐进披露」：先看锚点附近，顺着依赖一步步走下去，而不是一次铺开
# 所有跳数。10 个成熟依赖图产品的共同做法都是锚点 + 有界邻域 + 按需展开。
#
# 换锚点 / 换方向 / 换跳数时必须清空：那三个参数一变，图就是另一张图了，
# 留着上一张图的展开集合会把无关节点带进来，而用户看不出它们为什么在那儿。
S_EXPANDED = "_svgproto_expanded"
S_FULL = "_svgproto_full"
S_SHAPE = "_svgproto_shape"
shape = (anchor, downstream, int(hops), tuple(sorted(edge_types)))
if st.session_state.get(S_SHAPE) != shape:
    st.session_state[S_SHAPE] = shape
    st.session_state[S_EXPANDED] = set()
    st.session_state[S_FULL] = set()
expanded: set = st.session_state.setdefault(S_EXPANDED, set())
# 「展开全部邻居」的节点单独记一个集合：它和双击展开的区别只在**上限**——
# 双击受「每层上限」约束（那是用来防止高度数节点一次灌进几十个的），
# 而这个按钮是用户明确要求「全都要」，所以绕开那个上限。
# 分成两个集合而不是一个带标记的字典，是因为取数时它们用的 limit 不同。
full_expanded: set = st.session_state.setdefault(S_FULL, set())

# 全量展开的安全上限：绕开「每层上限」不等于无上限。单个节点的邻居数在这个
# 图里最多几十个，200 足够覆盖，同时挡住某个意外的超高度数节点把图撑爆。
_FULL_CAP = 200

# ── 取数：锚点 BFS + 展开集合各自再走一跳 ────────────────────────────────────
seen_nodes: dict[str, str] = {}
seen_edges: list[dict] = []
_edge_keys: set = set()


def absorb(rows: list) -> list:
    """把一批边并进累积集合，返回这批边通向的下一层节点。"""
    out = []
    for r in rows:
        s, t = r.get("source"), r.get("target")
        if not s or not t:
            continue
        seen_nodes.setdefault(s, r.get("source_label") or "")
        seen_nodes.setdefault(t, r.get("target_label") or "")
        key = (s, r.get("edge_type"), t)
        if key not in _edge_keys:
            _edge_keys.add(key)
            vs = r.get("verify_status") or "untested"
            color, vs_text = VS_COLOR.get(vs, VS_COLOR["untested"])
            # 边的粗细编码**实测退化率**，不是「确认与否」。
            # verify_degradation 是故障注入后观测方的退化百分比 —— 也就是这条依赖
            # 断掉时下游坏到什么程度。实测分布很宽（10% / 62% / 64%），
            # 而「确认与否」只有两档：把粗细给二值化的信息，就浪费了真正的爆炸半径。
            # 1.4–5.0px 之间线性映射，未确认或无退化数据的保持最细。
            #
            # ⚠️ 2026-09-15：`verify_degradation` 承载了**两种相反的语义**，
            #    照字面用会把最严重的边画成最细。实测三条：
            #
            #      petsite -[PublishesTo]-> ServicesEks2-topicpetadoption   deg=0.0
            #      petsite -[PublishesTo]-> ServicesEks2-sqspetadoption     deg=0.0
            #      petsite -[DependsOn]->   ServicesEks2-sqspetadoption     deg=0.0
            #
            #    它们全是 confirmed，`verify_reason` 写的是「**完全切断**：基线 32 次
            #    → 故障期从服务图消失 → 回滚后 29 次（前后夹住，排除聚合延迟），
            #    且业务归零」。也就是说这是**最大**爆炸半径，不是零。
            #
            #    根因在探针侧：degradation 算的是 `baseline_sr - during_sr`
            #    （成功率百分点差）。这几条 PublishesTo 边**没有成功率通道**，
            #    基线与故障期都取到 0，于是 `0 - 0 = 0.0` —— 一个从「无数据」
            #    算出来的值，恰好长得像「毫无退化」。
            #    真实证据在 `verify_evidence_channel = xray-edge+business-probe`。
            #
            #    所以这里**不能只看 deg**：`verify_severance` 有值就说明干预方式是
            #    「切断」且判定成立，那是满格宽度。已把探针侧的语义问题写进
            #    todo/CROSS-SESSION-NOTE，修在那边之前，这里的读法必须自己兜住。
            _deg = r.get("deg")
            _sev = r.get("severance")
            _severed = vs == "confirmed" and bool(_sev)
            if _severed:
                width = 5.0                      # 完全切断 = 满格，不是最细
            elif vs == "confirmed" and isinstance(_deg, (int, float)) and _deg > 0:
                width = 1.6 + min(float(_deg), 100.0) / 100.0 * 3.4
            else:
                width = 1.4
            _bits = [f"{r.get('edge_type')} · {vs_text}"]
            if _severed:
                # 刻意不写「退化 0.0%」—— 那个 0 不是测量值。
                _bits.append(f"⛔ 完全切断（{_sev}）：故障期该边从服务图消失")
                if r.get("ev_channel"):
                    _bits.append(f"证据通道 {r.get('ev_channel')}")
            elif isinstance(_deg, (int, float)) and _deg > 0:
                _bits.append(f"下游退化 {_deg:.1f}%")
            # 验证时效：一条边「确认过」不等于「现在还成立」。
            # 把距今天数摆出来，让读者自己判断这个结论有多新。
            _vl = r.get("verify_last")
            if isinstance(_vl, (int, float)) and _vl > 0:
                _days = (time.time() - float(_vl)) / 86400.0
                _bits.append(f"{_days:.1f} 天前验证" if _days >= 1 else "今天验证过")

            # ── 陈旧度 → 透明度 ────────────────────────────────────────────────
            #
            # 现在图上颜色给了验证状态、粗细给了退化率、虚线给了跨 AZ，
            # 陈旧度需要第四个通道，用**透明度**：越久没观测到越淡。
            # 这个映射有天然语义（褪色 = 正在消失），而且不与前三个冲突。
            #
            # 只让 `observed_then_silent` 和 `unobserved_since` 走透明度：
            # 它们是「曾经存在、现在沉默」。`declared_not_observed` 不走 ——
            # 那是「声明了但从没见过」，属于未证实而非褪色，语义上更接近
            # 已经由颜色表达的 untested，用变淡表示会把两件事混成一件。
            _drift = r.get("drift")
            _unobs = r.get("unobserved_since")
            _opacity = None
            _silent_days = None
            if isinstance(_unobs, (int, float)) and _unobs > 0:
                _silent_days = (time.time() - float(_unobs)) / 86400.0
                # 7 天以上压到最淡，线性过渡；不压到 0 以下，否则边就看不见了
                _opacity = round(max(0.32, 1.0 - min(_silent_days, 7.0) / 7.0 * 0.68), 2)
            elif _drift == "observed_then_silent":
                _opacity = 0.45

            if _drift == "observed_then_silent":
                _bits.append("曾观测到、现已沉默")
            elif _drift == "declared_not_observed":
                _bits.append("配置里声明但从未观测到")
            elif _drift == "ok":
                _bits.append("声明与观测一致")
            if _silent_days is not None:
                _bits.append(f"已沉默 {_silent_days:.1f} 天")

            # 性能数据只在有值时进提示：error_rate 在这个环境恒为 0，
            # 把恒定值印出来是噪音，不是信息。
            _p99 = r.get("p99")
            if isinstance(_p99, (int, float)) and _p99 > 0:
                _bits.append(f"p99 {_p99:.0f}ms")
            _err = r.get("error_rate")
            if isinstance(_err, (int, float)) and _err > 0:
                _bits.append(f"错误率 {_err:.2%}")
            _calls = r.get("calls")
            if isinstance(_calls, (int, float)) and _calls > 0:
                _bits.append(f"{int(_calls)} 次调用")

            _edge = {
                "source": s, "target": t, "color": color, "width": round(width, 2),
                "title": " · ".join(_bits),
                "_nfm_cross_az": r.get("nfm_cross_az"),
                "_drift": _drift,
                "_silent_days": _silent_days,
            }
            if _opacity is not None:
                _edge["opacity"] = _opacity
            seen_edges.append(_edge)
        out.append(t if downstream else s)
    return out


frontier = [anchor]
visited = {anchor}
for _ in range(int(hops)):
    nxt = []
    for name in frontier:
        nxt += absorb(directed_rows(name, tuple(edge_types), int(limit), downstream))
    frontier = [n for n in nxt if n not in visited]
    visited.update(frontier)

# 手动展开：每个只走一跳。放在基础 BFS 之后，这样它们带出来的边是**增量**，
# 而不是把跳数整体调大 —— 后者会让所有分支一起膨胀，正是要避免的。
for name in sorted(expanded):
    absorb(directed_rows(name, tuple(edge_types), int(limit), downstream))

# 全量展开：同样只走一跳，但不受「每层上限」约束。放在最后，所以它补齐的正是
# 前面被上限截掉的那些邻居。
for name in sorted(full_expanded):
    absorb(directed_rows(name, tuple(edge_types), _FULL_CAP, downstream))

deg: dict[str, int] = {}
for e in seen_edges:
    deg[e["source"]] = deg.get(e["source"], 0) + 1
    deg[e["target"]] = deg.get(e["target"], 0) + 1

nodes = []
for nid, lb in seen_nodes.items():
    gname, gcolor = group_of(lb)
    n = {
        "id": nid,
        "label": (("★ " if nid == anchor else "") + nid)[:34],
        "type": lb, "group": gname, "degree": deg.get(nid, 0),
        "accent": gcolor,
        "stroke": "#3759ce" if nid == anchor else "#c8d1de",
    }
    # 已展开的标个记号：不然用户双击过之后看不出哪些走过了，
    # 会重复双击同一个节点并以为没生效。两种展开分开标，因为它们的含义不同：
    # ✓ = 走过一跳（仍受每层上限），✦ = 该节点的邻居已全部拉进来。
    if nid in full_expanded:
        n["badge"] = "✦"
        n["badge_fill"] = "#3759ce"
        n["fill"] = "#f2f5fd"
    elif nid in expanded:
        n["badge"] = "✓"
        n["badge_fill"] = "#2E7D32"
        n["fill"] = "#f4fbf5"
    nodes.append(n)

_place = C.placements(tuple(sorted(seen_nodes)))

# 跨 AZ 的依赖边：**先信图上的观测字段，再退到属地推算**。
#
# 边上有 `nfm_cross_az`（Network Flow Monitor 的实测结果），但只覆盖约 11% 的依赖边
# （实测 91 条依赖边里 10 条有这个字段）—— NFM 只看得到它采样到的网络流。
# 所以两个来源是互补的：有观测就用观测，没有就用两端属地推算。
#
# 顺序不能反：属地推算是「两端 AZ 不同所以调用跨区」，而 NFM 是真的看到了流量走向。
# 推算会把「同城不同 AZ 但走了 VPC 端点」这类情况算错，观测不会。
_crossed = _crossed_obs = 0
for _e in seen_edges:
    _obs = _e.pop("_nfm_cross_az", None)
    if _obs is True:
        _e["dashed"] = True
        _e["title"] = (_e.get("title") or "") + " · 跨 AZ（NFM 实测）"
        _crossed += 1
        _crossed_obs += 1
    elif _obs is False:
        pass  # 实测同区，不必再拿属地去推翻观测
    else:
        _sa = (_place.get(_e["source"]) or {}).get("az")
        _ta = (_place.get(_e["target"]) or {}).get("az")
        if _sa and _ta and _sa != _ta:
            _e["dashed"] = True
            _e["title"] = (_e.get("title") or "") + f" · 跨 AZ {_sa}→{_ta}（按属地推算）"
            _crossed += 1

m = st.columns(6)
m[0].metric("节点", len(nodes))
m[1].metric("依赖边", len(seen_edges))
m[2].metric("基础跳数", int(hops))
m[3].metric("手动展开", len(expanded) + len(full_expanded))
m[4].metric("分组", len({n["group"] for n in nodes}))

# AZ 分布：值班时真正要判断的不是「这个 pod 在哪台机器」，而是「这堆东西有没有
# 跨 AZ 冗余」。全落在一个 AZ 就是个藏起来的单点，所以这里显示**几个 AZ**
# 而不是列出机器，并在只有一个 AZ 时明确警示。
_az_count: dict = {}
for _nm in seen_nodes:
    _az = (_place.get(_nm) or {}).get("az")
    if _az:
        _az_count[_az] = _az_count.get(_az, 0) + 1
_placed = sum(_az_count.values())
m[5].metric(
    "AZ", len(_az_count) if _az_count else "—",
    help=("图上有属地信息的 " + str(_placed) + " 个节点分布在："
          + "、".join(f"{a}({c})" for a, c in sorted(_az_count.items()))
          if _az_count else
          "取不到属地信息。位置走的是结构边（RunsOn / LocatedIn），"
          "离线快照里没有这部分，需要连上 Neptune 活图谱。"),
)
if len(_az_count) == 1 and _placed >= 3:
    _only = next(iter(_az_count))
    st.warning(
        f"⚠️ 图上 {_placed} 个有属地的节点**全部**在 `{_only}`，"
        "这一层没有跨 AZ 冗余，该 AZ 故障会让它们一起失效。"
        "（位置来自 `RunsOn` / `LocatedIn` 结构边，不画在图上：那是包含关系，"
        "画成箭头会被读成依赖。）",
        icon="🏗️",
    )
elif _crossed:
    _src = (f"其中 {_crossed_obs} 条来自 NFM 实测，其余按两端属地推算"
            if _crossed_obs else "按两端属地推算")
    st.info(
        f"🔀 有 **{_crossed}** 条依赖边跨了可用区（图上画成**虚线**，{_src}）。"
        "跨 AZ 调用多一跳网络延迟、产生跨区数据传输费用，"
        "换来的是单 AZ 故障时不会一起失效。这是取舍，不是缺陷。"
        "悬停虚线边可以看到具体是哪两个 AZ、以及这一条是实测还是推算。"
    )

# ── 沉默边：这张图里有几条依赖可能已经不存在了 ────────────────────────────────
#
# 值班时最误导人的不是缺一条边，而是**多一条早就没了的边** —— 它会把爆炸半径算大，
# 把根因候选拉长。所以这个数单独摆出来，而不是只靠图上变淡让人自己发现。
_silent = [e for e in seen_edges
           if e.get("_drift") == "observed_then_silent" or e.get("_silent_days")]
_declared_only = [e for e in seen_edges if e.get("_drift") == "declared_not_observed"]
if _silent or _declared_only:
    _parts = []
    if _silent:
        _worst = max((e.get("_silent_days") or 0) for e in _silent)
        _parts.append(
            f"**{len(_silent)}** 条曾观测到、现已沉默（图上**变淡**，"
            + (f"最久 {_worst:.1f} 天" if _worst else "无沉默时长数据") + "）"
        )
    if _declared_only:
        _parts.append(f"**{len(_declared_only)}** 条只在配置里声明、从未观测到")
    st.warning(
        "🕰️ " + "；".join(_parts) + "。\n\n"
        "沉默的边不代表一定失效，低频路径也会长时间没有流量；但把它们当成活跃依赖会"
        "**把爆炸半径算大、把根因候选拉长**。变淡是提醒去核对，不是判定它已消失。"
        "「只声明未观测」则是另一回事：它没有褪色，因为从来没有亮过 —— "
        "可能是死配置，也可能是尚未触发的路径。",
        icon="🕰️",
    )

if expanded or full_expanded:
    if st.button("↺ 清空手动展开", help="回到只有基础跳数的那张图"):
        st.session_state[S_EXPANDED] = set()
        st.session_state[S_FULL] = set()
        st.rerun()

# 传给组件前摘掉内部字段（`_` 前缀）：组件只认
# source/target/color/width/title/dashed，其余键只会白占序列化体积。
_render_edges = [{k: v for k, v in e.items() if not k.startswith("_")}
                 for e in seen_edges]
res = graph_svg.render(nodes, _render_edges, height=640, anchor=anchor, key="proto")

# 双击展开：把节点并进集合并重画。
# 只在它**还不在集合里**时才 rerun —— setTriggerValue 是一次性的，但同一个
# 值在 rerun 后若仍被读到就会形成无限循环，这个判断同时也是那道防线。
hit = res.get("expand")
if hit and hit not in expanded:
    expanded.add(hit)
    st.session_state[S_EXPANDED] = expanded
    st.rerun()

st.caption(
    "**单击**节点看下方详情与可做的操作，**双击**展开它的下一跳。"
    "记号：**✓** 走过一跳（仍受每层上限）／**✦** 邻居已全部拉进来。"
    "键盘：`Tab` 逐个聚焦节点、`Enter` 选中，聚焦时同样点亮相邻。"
    "左侧色条 = 分组，边色 = 故障注入验证状态。节点名恒定 13px："
    "不做「缩到装下」，图比容器大时滚动浏览。"
)

st.markdown("---")
d1, d2 = st.columns([3, 2])
with d1:
    st.subheader("选中节点")
    sel = res.get("selected")
    if sel:
        n = next((x for x in nodes if x["id"] == sel), None)
        st.write(f"**{sel}**")
        if n:
            st.caption(f"类型 `{n['type']}` · 分组 {n['group']} · 图上已连 {n['degree']} 条边")

        # 属地：从结构边（RunsOn / LocatedIn）取，不画在图上。
        # 顺序按「从近到远」排：宿主 → 子网 → AZ → Region，这也是排查时的收敛顺序。
        _p = _place.get(sel) or {}
        _chain = [(lab, _p.get(k)) for lab, k in
                  (("宿主", "host"), ("子网", "subnet"), ("AZ", "az"), ("Region", "region"))]
        _chain = [(lab, v) for lab, v in _chain if v]
        if _chain:
            st.caption("**属地**　" + "　→　".join(f"{lab} `{v}`" for lab, v in _chain))
        elif online:
            st.caption(
                "属地：图里没有这个节点的 `RunsOn` / `LocatedIn` 边，"
                "它可能是逻辑实体（业务能力、Agent 工具）而非部署实体。"
            )

        # 「展开全部邻居」：查一次不受上限的邻居，和图上已有的比对，
        # 把差值放在按钮旁边 —— 一个不说明会发生什么的按钮，用户只能盲点。
        all_rows = directed_rows(sel, tuple(edge_types), _FULL_CAP, downstream)
        peers = {(r.get("target") if downstream else r.get("source")) for r in all_rows}
        peers.discard(None)
        on_graph = peers & set(seen_nodes)
        hidden = len(peers) - len(on_graph)

        if sel in full_expanded:
            st.caption(f"已展开全部邻居（共 {len(peers)} 个）。")
        elif hidden > 0:
            if st.button(f"⤢ 展开全部邻居（还有 {hidden} 个没显示）", key="btn_full"):
                full_expanded.add(sel)
                st.session_state[S_FULL] = full_expanded
                st.rerun()
            st.caption(
                f"这个节点沿当前方向共有 {len(peers)} 个邻居，图上已有 {len(on_graph)} 个，"
                f"差的 {hidden} 个被「每层上限」({int(limit)}) 截掉了。"
                "这个按钮绕开那个上限，只对这一个节点。"
            )
        else:
            st.caption(f"它的邻居（{len(peers)} 个）都已经在图上了。")

        st.markdown("**下钻查询**")
        st.caption("拿这个节点去跑预置查询：确定性 Cypher，不经过 AI。")
        for qname, why in (
            ("q22_edge_verification_verdicts", "这个服务的依赖边验证判定"),
            ("q1_blast_radius", "它挂了会影响什么"),
            ("q3_upstream_deps", "谁依赖它"),
        ):
            st.caption(f"　`{qname}` — {why}")
        C.page_link("pages/2_Query_Catalog.py", "→ 去查询库执行")
    else:
        st.caption("还没点过节点。单击图上任意节点，这里会显示它的信息与可做的操作。")
with d2:
    st.subheader("展开路径")
    if expanded or full_expanded:
        for name in sorted(full_expanded):
            st.markdown(f"- ✦ `{name}`：全部邻居")
        for name in sorted(expanded - full_expanded):
            st.markdown(f"- ✓ `{name}`：下一跳")
        st.caption(
            "这些是**增量**。把基础跳数调大会让所有分支同时膨胀，那不是同一件事。"
            "✦ 绕开了「每层上限」，✓ 仍受它约束。"
        )
    else:
        st.caption(
            "还没手动展开过。双击一个节点会把它的下一跳并进这张图，"
            "而不是重画一张。这是「顺着依赖走」和「换一张快照」的区别。"
            "要一次看完某个节点的全部邻居，单击它、用左边的「展开全部邻居」。"
        )


# ── 图例 ──────────────────────────────────────────────────────────────────────
with st.expander("图例与设计说明"):
    st.markdown("**左侧色条 = 分组**（7 组，Okabe–Ito 色盲安全配色）")
    present = sorted({n["group"] for n in nodes})
    for gname in present:
        types = sorted({n["type"] for n in nodes if n["group"] == gname and n["type"]})
        st.markdown(
            f"<span style='display:inline-block;width:13px;height:13px;border-radius:3px;"
            f"background:{GROUP_COLOR_BY_NAME.get(gname, '#B39DDB')};margin-right:8px'></span>"
            f"**{gname}**　" + "　".join(f"`{t}`" for t in types),
            unsafe_allow_html=True,
        )
    st.markdown("---")
    st.markdown(
        "**边颜色 = 故障注入验证状态**，不是关系类型：\n\n"
        "- 🟢 已确认：注入故障后下游确实受影响\n"
        "- 🔴 已证伪：注入了但下游没反应，这条边存疑\n"
        "- 🟠 未定：注入过但结论不明确\n"
        "- ⚫ 未验证：还没注入过\n\n"
        "**边的粗细 = 实测退化率**（`verify_degradation`）：注入故障后观测方退化的"
        "百分比，也就是这条依赖断掉时下游坏到什么程度。实测分布很宽（10% 到 64%），"
        "所以粗细给了它而不是给「确认与否」那两档。未确认的边一律最细。\n\n"
        "**满格粗 = 完全切断**：有些边没有成功率通道可测（比如发往 SNS/SQS 的"
        "`PublishesTo`），注入时它直接**从服务图消失**、业务归零。这类边的"
        "`verify_degradation` 会算成 `0.0`（基线与故障期都没有样本，相减得零），"
        "**那个 0 不是测量值**。所以这里按 `verify_severance` 判定，画满格宽度 —— "
        "照字面读 0.0 会把爆炸半径最大的边画成最细，正好反了。\n\n"
        "关系类型、退化率数值、**距今多久验证过**都在**悬停边**时显示。"
        "时效值得单独看一眼：一条边确认过，不等于它现在还成立。\n\n"
        "**虚线 = 跨可用区**。优先用边上 `nfm_cross_az`（Network Flow Monitor 实测，"
        "但只覆盖约一成的依赖边），没有观测时退回按两端属地推算，悬停可看到是哪一种。"
        "这一维用线型而不是颜色，是因为颜色已经表示验证状态；"
        "两种含义挤进同一个通道，读者就分不清自己看到的是哪一件事。\n\n"
        "**变淡 = 越久没观测到**（`unobserved_since` / `drift_status`）。"
        "曾经观测到、现在沉默的边会褪色，7 天以上压到最淡。"
        "「只在配置里声明、从未观测到」的边**不褪色** —— 它不是正在消失，"
        "而是从来没亮过，那更接近「未验证」，已经由颜色表达了。\n\n"
        "四个通道到此为止：颜色（验证状态）、粗细（退化率）、线型（跨区）、"
        "透明度（陈旧度）。再往上加编码会超出一眼能辨的数量，"
        "其余字段（p99 延迟、调用次数、错误率）都放在**悬停提示**里。"
    )
    st.markdown("---")
    st.markdown(
        "**为什么不渲染全图**：本仓 `docs/lessons/05-图谱展示方案调研.md` "
        "记录的调研里，10 个成熟依赖图产品（Datadog / Dynatrace / Bloom / "
        "graph-explorer / Kiali / Grafana …）没有一个默认渲染全图。"
        "认知上也有硬数字：Yoghourdjian 等（IEEE TVCG 2020，EEG + 眼动对照）测得"
        "**节点超过约 50 个时，被试答错或不确定的比例超过一半**。"
        "所以默认是锚点 + 有界邻域，要看更远靠**双击展开**顺着一条路径走，"
        "把跳数或每层上限拉满则会让所有分支同时膨胀，那是另一回事。"
    )
