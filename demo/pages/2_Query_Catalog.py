"""
2_Query_Catalog.py — 预置图查询库浏览器。

为什么单独做这一页：它是给访客**最安全、最容易上手**的互动入口——
确定性 Cypher，不经过任何 LLM，不会因为 Bedrock 不可用而失败，
参数契约是显式的（选一条、填参数、跑）。

条目数从 QUERY_CATALOG 现算。界面上任何「Q1–Q18」的说法都是错的：
q12–q16 在 dr-plan-generator 模块里，且 q19/q20/q21 是后来新增的。
"""
import os
import sys

# 页面被单独执行时 demo/ 不在 sys.path 上，显式补上。
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import _common as C  # noqa: E402

import streamlit as st  # noqa: E402

C.page_setup("查询库", icon="📚")
C.sidebar()

qc = C.query_catalog_info()

st.title("📚 预置查询库")
st.markdown(
    f"**{qc['count']} 条**确定性图查询——固定的 Cypher、显式的参数契约、"
    "**不经过任何 LLM**。选一条、填参数、直接跑。"
)

if not qc["available"]:
    st.error(f"查询库加载失败：{qc.get('error')}")
    st.stop()

try:
    from neptune.query_catalog import QUERY_CATALOG, run_query
except Exception as exc:  # noqa: BLE001
    st.error(f"查询库导入失败：{exc}")
    st.stop()

online = C.neptune_online()
samples = C.fixture("query_samples")

if online:
    st.success("🟢 **实时** —— 查询将直接在 Neptune 活图谱上执行", icon="🟢")
else:
    st.info(
        "🔵 **离线快照** —— 当前无法访问 Neptune。"
        f"下面标 📦 的查询有**真实执行结果的快照**可看"
        + (f"（抓取于 {samples.get('captured_at')}）" if samples.get("captured_at") else "")
        + "，其余查询可以看到它的参数契约与用途。"
    )

# ── 分组 ──────────────────────────────────────────────────────────────────────
def _kind(entry: dict) -> str:
    return "rca" if entry.get("mod") == "queries" else "dr"


rca_names = sorted(k for k, v in QUERY_CATALOG.items() if _kind(v) == "rca")
dr_names = sorted(k for k, v in QUERY_CATALOG.items() if _kind(v) != "rca")

m = st.columns(4)
m[0].metric("查询总数", qc["count"])
m[1].metric("RCA 类", len(rca_names))
m[2].metric("DR 类", len(dr_names))
m[3].metric("📦 有离线结果", sum(1 for k in QUERY_CATALOG if k in samples))

st.caption(
    "⚠️ 文档里流传的「Q1–Q18」两头都不对：`neptune_queries.py` 里**没有** q12–q16"
    "（它们在 `dr-plan-generator/graph/queries.py`），而 **q19 / q20 / q21 是新增的**"
    "——其中 q20、q21 正是依赖验证与观测源对账用的查询。"
)

# ── 推荐先试 ──────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("先试这几条")
st.caption("都不需要填参数，点一下就有结果。")

FEATURED = [
    ("q20_dependency_verification", "看依赖边的运行时验证状态——本项目的核心产出"),
    ("q16_single_point_of_failure", "图算法直接算出单点故障，不靠人推理"),
    ("q21_observation_source_coverage", "各观测源之间互相对账，看谁看到了什么"),
    ("q2_tier0_status", "所有 Tier0 服务的故障边界与副本数"),
]
fcols = st.columns(len(FEATURED))
for col, (name, why) in zip(fcols, FEATURED):
    if name not in QUERY_CATALOG:
        continue
    with col:
        with st.container(border=True):
            st.markdown(f"**`{name}`**")
            st.caption(why)
            if st.button("运行", key=f"feat_{name}", width="stretch"):
                # 必须直接写 selectbox **自己的 state key**（`qc_select_box`）。
                #
                # 原实现只设 `qc_selected`，再指望下面 `st.selectbox(..., index=...)`
                # 去读它 —— 但 **widget 带 key 且 session state 已有值时，
                # Streamlit 会忽略 `index=`**。于是这四个精选按钮推不动选择框，
                # 点任何一个都在跑当前选中的那条查询。
                #
                # 实测（AppTest）：四个「运行」全部返回 `q10_infra_root_cause`
                # 的结果，而它们标称的是 q20 / q16 / q21 / q2。
                st.session_state["qc_select_box"] = name
                st.session_state["qc_selected"] = name
                st.session_state["qc_autorun"] = True

# ── 选择查询 ──────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("浏览全部查询")

group = st.radio(
    "分类", ["全部", f"RCA（{len(rca_names)}）", f"DR（{len(dr_names)}）"],
    horizontal=True, key="qc_group",
)
if group.startswith("RCA"):
    pool = rca_names
elif group.startswith("DR"):
    pool = dr_names
else:
    pool = rca_names + dr_names


def _label(name: str) -> str:
    e = QUERY_CATALOG[name]
    badge = "📦 " if name in samples else ""
    need = "（需参数）" if e.get("required") else "（无需参数）"
    return f"{badge}{name} {need} — {e.get('desc', '')[:44]}"


# 选中项**只**由 widget 自己的 state key `qc_select_box` 决定。
#
# 原实现同时用 `index=default_idx` 和 session state 两条路，Streamlit 会告警
# 「created with a default value but also had its value set via the Session
# State API」，而且 `index=` 在 key 已有值时根本不生效（精选按钮因此失效）。
# 只留一条路：精选按钮直接写 `qc_select_box`，这里不再传 index。
if st.session_state.get("qc_select_box") not in pool:
    st.session_state["qc_select_box"] = pool[0] if pool else None

selected = st.selectbox(
    "选择一条查询", pool, format_func=_label, key="qc_select_box"
)

entry = QUERY_CATALOG[selected]
required = entry.get("required", []) or []
params_spec = entry.get("params", {}) or {}

with st.container(border=True):
    st.markdown(f"### `{selected}`")
    st.markdown(f"**用途**　{entry.get('desc', '—')}")
    c1, c2 = st.columns(2)
    c1.caption(f"实现模块　`{entry.get('mod')}.{entry.get('fn')}`")
    c2.caption(f"分类　`{_kind(entry)}`　·　必填参数　{len(required)} 个")

    if params_spec:
        st.markdown("**参数契约**")
        st.dataframe(
            C.df([
                {
                    "参数": k,
                    "必填": "✅" if k in required else "可选",
                    "说明": v,
                }
                for k, v in params_spec.items()
            ]),
            width="stretch", hide_index=True,
        )
    else:
        st.caption("此查询无参数。")

# ── 参数输入 ──────────────────────────────────────────────────────────────────
#
# ## 必填参数不能默认空（2026-09-06 修）
#
# 原实现给服务类参数生成 `[""] + KNOWN_SERVICES` 的下拉框 —— **空串在第一位，
# 所以它是默认值**。而「执行查询」按钮是 `disabled=bool(missing) or not online`，
# 于是只要默认选中的查询带必填服务参数（默认就是 `q10_infra_root_cause`，
# 必填 `affected_service`），**页面一打开按钮就是禁用的，点了没有任何反应**。
# 4 个精选「运行」按钮也一样：它们的 `qc_autorun` 路径同样要求 `not missing`。
#
# 实测（AppTest）：`selectbox 值: ['q10_infra_root_cause', '']`、
# `按钮: [..., ('▶️ 执行查询', True)]` —— disabled=True。
#
# 改法：**必填参数不给空选项**，直接默认第一个真实取值；可选参数保留空选项
# （空 = 不传该参数，这是有意义的语义）。
#
# ## 服务清单从图谱取，不硬编码
#
# 原来的 `KNOWN_SERVICES` 里写着 `petadoptionshistory` —— 图谱里的名字是
# `pethistory`，这个硬编码清单和图谱漂移了。同一个毛病在 6_Root_Cause_Analysis
# 也修过。改用 `C.service_names()`（从活图谱现取，离线回退快照）。
_FALLBACK_SERVICES = [
    "petsite", "petsearch", "payforadoption", "petlistadoptions",
    "pethistory", "petfood", "trafficgenerator",
]
#: 默认参数优先落在这些服务上。图谱返回的顺序是字母序，第一个是 `artillery`
#: —— 那是压测工具（DeepFlow 采到它的流量后图里就多出一个「微服务」），
#: 它的陈旧边已被清理，所以拿它当默认参数几乎所有查询都返回空。
#: 讲依赖的页面开局给空结果是最差的第一印象，所以把有内容的服务排到前面。
_PREFERRED_FIRST = ["petsite", "petsearch", "payforadoption", "petlistadoptions",
                    "pethistory"]
try:
    _svcs = list(C.service_names()) or _FALLBACK_SERVICES
except Exception:  # noqa: BLE001
    _svcs = _FALLBACK_SERVICES
KNOWN_SERVICES = ([s for s in _PREFERRED_FIRST if s in _svcs]
                  + [s for s in _svcs if s not in _PREFERRED_FIRST])

user_params: dict = {}
if params_spec:
    st.markdown("**填参数**")
    pcols = st.columns(min(3, len(params_spec)))
    for i, (pname, phint) in enumerate(params_spec.items()):
        col = pcols[i % len(pcols)]
        hint = str(phint)
        req = pname in required
        with col:
            if "service" in pname or "node" in pname:
                # 必填 → 不给空选项，否则按钮永远是禁用的
                opts = KNOWN_SERVICES if req else [""] + KNOWN_SERVICES
                val = col.selectbox(
                    f"{pname}{' *' if req else ''}",
                    opts, help=hint, key=f"p_{selected}_{pname}",
                )
            elif "int" in hint:
                default = 5
                for tok in hint.replace("默认", " ").split():
                    if tok.isdigit():
                        default = int(tok)
                        break
                val = col.number_input(
                    f"{pname}{' *' if req else ''}",
                    min_value=1, value=default, help=hint, key=f"p_{selected}_{pname}",
                )
            elif "|" in hint:
                choices = [o.strip() for o in hint.split("，")[0].split("|")]
                opts = choices if req else [""] + choices
                val = col.selectbox(
                    f"{pname}{' *' if req else ''}",
                    opts, help=hint, key=f"p_{selected}_{pname}",
                )
            else:
                # 自由文本必填项：给一个可用的默认值，理由同上
                val = col.text_input(
                    f"{pname}{' *' if req else ''}",
                    value=(KNOWN_SERVICES[0] if req and KNOWN_SERVICES else ""),
                    help=hint, key=f"p_{selected}_{pname}",
                )
        if val not in ("", None):
            user_params[pname] = val

missing = [p for p in required if p not in user_params]

run_col, note_col = st.columns([1, 3])
do_run = run_col.button(
    "▶️ 执行查询", type="primary", width="stretch",
    disabled=bool(missing) or not online,
)
if missing:
    note_col.warning(f"还缺必填参数：{'、'.join(f'`{p}`' for p in missing)}")
elif not online:
    note_col.info("当前离线，无法执行。下面展示该查询的离线结果快照（若有）。")

if st.session_state.pop("qc_autorun", False) and online and not missing:
    do_run = True

# ── 结果 ──────────────────────────────────────────────────────────────────────
st.markdown("---")
st.subheader("结果")

if do_run and online:
    with st.spinner(f"在活图谱上执行 {selected} …"):
        try:
            res = run_query(selected, **user_params)
            rows = res.get("results", res) if isinstance(res, dict) else res
            if isinstance(rows, list):
                st.success(f"🟢 实时执行成功，返回 **{len(rows)}** 行")
                if rows:
                    st.dataframe(C.df(rows), width="stretch", hide_index=True)
                    st.download_button(
                        "下载 JSON", data=C.json.dumps(rows, ensure_ascii=False, indent=2, default=str),
                        file_name=f"{selected}.json", mime="application/json",
                    )
                else:
                    st.info(
                        "返回 0 行。这不一定是错误——例如 `q14_cross_region_resources` "
                        "在单区域部署下本来就应该是空的。"
                    )
            elif isinstance(rows, dict):
                # 有几条查询返回的是**多个命名集合**而不是行列表
                # （`q1_blast_radius` → services/capabilities，
                #   `q10_infra_root_cause` → unhealthy_ec2/az_impact/has_infra_fault，
                #   `q4_service_info` → 单个对象）。
                #
                # 原实现一律走 `st.write(rows)`，渲染成一团原始字典：既没有成功提示、
                # 也没有表格，看起来跟「点了没反应」没有区别 —— 实测 Query Catalog
                # 默认选中的正是 `q10_infra_root_cause`，所以首屏体验就是这个。
                #
                # 改成按键分开渲染：列表 → 表格，标量 → 指标。
                lists = {k: v for k, v in rows.items() if isinstance(v, list)}
                scalars = {k: v for k, v in rows.items() if not isinstance(v, (list, dict))}
                total = sum(len(v) for v in lists.values())
                st.success(
                    f"🟢 实时执行成功，返回 **{len(rows)}** 个集合"
                    + (f"、共 **{total}** 行" if lists else ""))
                if scalars:
                    scols = st.columns(min(len(scalars), 4))
                    for i, (k, v) in enumerate(scalars.items()):
                        scols[i % len(scols)].metric(k, str(v))
                for k, v in lists.items():
                    st.markdown(f"**{k}** — {len(v)} 行")
                    if v:
                        st.dataframe(C.df(v), width="stretch", hide_index=True)
                    else:
                        st.caption("0 行。空集合不等于错误 —— 例如没有故障 EC2 时"
                                   "`unhealthy_ec2` 本来就该是空的。")
                nested = {k: v for k, v in rows.items() if isinstance(v, dict)}
                if nested:
                    with st.expander("嵌套结构"):
                        st.json(nested, expanded=False)
                st.download_button(
                    "下载 JSON",
                    data=C.json.dumps(rows, ensure_ascii=False, indent=2, default=str),
                    file_name=f"{selected}.json", mime="application/json",
                )
            else:
                st.success("🟢 实时执行成功（单值）")
                st.code(str(rows))
        except Exception as exc:  # noqa: BLE001
            st.error(f"执行失败：{type(exc).__name__}: {exc}")
elif selected in samples:
    s = samples[selected]
    st.info(
        f"📦 **离线结果快照** —— 这是该查询在活图谱上的真实执行结果"
        + (f"，抓取于 {samples.get('captured_at')}" if samples.get("captured_at") else "")
        + f"。共 {s.get('row_count')} 行"
        + ("（此处显示前 40 行）" if (s.get("row_count") or 0) > 40 else "")
        + "。"
    )
    rows = s.get("rows") or []
    if rows:
        st.dataframe(C.df(rows), width="stretch", hide_index=True)
    else:
        st.caption(
            "该查询返回 0 行。以 `q14_cross_region_resources` 为例，"
            "单区域部署下空结果是**正确**的。"
        )
else:
    st.caption("尚未执行。选择一条查询并点「执行查询」，或挑一条带 📦 的看离线结果。")

# ── 全量清单 ──────────────────────────────────────────────────────────────────
st.markdown("---")
with st.expander(f"全部 {qc['count']} 条查询一览"):
    st.dataframe(
        C.df([
            {
                "查询": k,
                "分类": _kind(QUERY_CATALOG[k]),
                "必填参数": ", ".join(QUERY_CATALOG[k].get("required", []) or []) or "—",
                "离线结果": "📦" if k in samples else "",
                "用途": QUERY_CATALOG[k].get("desc", ""),
            }
            for k in sorted(QUERY_CATALOG)
        ]),
        width="stretch", hide_index=True,
    )
