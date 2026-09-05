"""
4_Smart_Query.py — 自然语言图查询（NL → openCypher → 执行 → AI 摘要）。

改造要点（2026-09-05）：

1. **修一个真 bug**：`rca/engines/factory.py:28` 的默认引擎是 `strands`，
   而本页原来写 `os.environ.get("NLQUERY_ENGINE") or "direct"`。不设环境变量时
   跑的是 strands、显示的是 direct，还据此选错了「工作原理」说明文字。
   现在从**实际构造出来的引擎**读 ENGINE_NAME。

2. **引擎并排对比**：原来写「切换需重启」，观众无从体会两个引擎的差别。
   工厂是调用时读 env 的，所以可以同一个问题分别用 direct 与 strands 跑，
   把 token / ReAct 轮数 / 工具调用链摆在一起——这是展示 Strands 集成
   最有说服力的方式。

3. **示例问题从契约取**：原来硬编码 8 个问题；契约里有 30 组 few-shot
   `{q, cypher}`，那是引擎真正用的语料，问题一定有对应的正确 Cypher。

4. **离线降级**：无 Bedrock 时不再是「每次提问都报错」，而是展示 few-shot 的
   问题→Cypher 对照。刻意**不**预置假答案——那是给 LLM 输出做录像，
   会瓦解本项目关于「数据可信」的主张。

5. `@st.cache_resource` 缓存引擎（原来每次交互都重建）。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _common as C  # noqa: E402

import streamlit as st  # noqa: E402

os.environ.setdefault("BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6")

C.page_setup("自然语言查询", icon="💬")
C.sidebar()

FEW_SHOT = C.few_shot_examples()
ONLINE = C.neptune_online()

st.title("💬 自然语言查询")
st.markdown(
    "把问题写成中文，AI 生成 openCypher 并执行。"
    "**生成的查询会完整展示出来**——这一步不该是黑盒。"
)

# ── 引擎状态（原来这里显示的是错的）──────────────────────────────────────────
engine_name = C.active_engine_name() if ONLINE else "（离线）"

e1, e2, e3 = st.columns([1, 1, 2])
e1.metric("当前引擎", engine_name)
e2.metric("契约 few-shot", len(FEW_SHOT), "组问题→Cypher")
e3.caption(
    f"引擎由 `rca/engines/factory.py` 按环境变量 `NLQUERY_ENGINE` 选择，"
    f"**默认 `strands`**；strands 不可用时会记一条 warning 并静默回落 `direct`。\n\n"
    f"上面显示的是**实际构造出来的**引擎名，不是猜的环境变量值。"
)

if not ONLINE:
    st.info(
        "🔵 **离线模式** —— 当前无法访问 Neptune / Bedrock，无法实时生成查询。\n\n"
        "下面展示契约里的 **问题 → Cypher 对照**，那是 NL 引擎真正使用的 few-shot "
        "语料，也就是「AI 应该生成什么」的标准答案。\n\n"
        "刻意**不**预置假的 AI 回答——给 LLM 输出做录像会瓦解这个项目"
        "关于「数据可信」的主张。"
    )

# ── 侧栏 ──────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("---")
    st.markdown("### 示例问题")
    st.caption(f"来自契约的 {len(FEW_SHOT)} 组 few-shot，点击填入")
    for i, ex in enumerate(FEW_SHOT[:12]):
        if st.button(ex["q"], key=f"ex_{i}", width="stretch"):
            st.session_state["pending_question"] = ex["q"]
    if len(FEW_SHOT) > 12:
        st.caption(f"另有 {len(FEW_SHOT) - 12} 组，见页面下方对照表")

    st.markdown("---")
    st.markdown("### 工作原理")
    if engine_name == "strands":
        st.caption(
            "**Strands Agent（ReAct 多轮）**\n\n"
            "1. 按关键词选模型：Sonnet，命中「完整/影响/路径」等复杂词升 Opus\n"
            "2. Agent 调 `get_schema_section` 按需取 schema\n"
            "3. Agent 调 `validate_cypher` 做安全校验\n"
            "4. Agent 调 `execute_cypher`——内部强制 `query_guard.is_safe()`\n"
            "5. Agent 生成中文摘要\n\n"
            "每个回答下方可展开完整的工具调用链。"
        )
    elif engine_name == "direct":
        st.caption(
            "**Direct Bedrock（单轮 + 重试）**\n\n"
            "1. Claude Sonnet 生成 openCypher（复杂问题升 Opus）\n"
            "2. `query_guard.is_safe()` 拦写操作与超深遍历\n"
            "3. Neptune 执行（自动补 LIMIT）\n"
            "4. 空结果自动重试一次（带关系名提示；否定型问题跳过）\n"
            "5. Claude 生成中文摘要\n\n"
            "不产生工具调用链（`trace` 恒为空）。"
        )
    else:
        st.caption("引擎不可用，无法展示工作原理。")

    st.markdown("---")
    st.caption(
        "**安全边界**　`query_guard` 拦截 "
        "`CREATE / DELETE / DETACH / SET / MERGE / REMOVE / DROP / CALL`，"
        "限制最大跳数 6、默认 LIMIT 200。写操作在这里就被挡掉，不依赖 IAM。"
    )
    if st.button("🗑️ 清空对话", width="stretch"):
        st.session_state["chat_history"] = []
        st.rerun()

if "chat_history" not in st.session_state:
    st.session_state["chat_history"] = []


# ── 渲染 ──────────────────────────────────────────────────────────────────────
def _meta_line(data: dict) -> str:
    bits = []
    for key, label in (
        ("engine", "engine"), ("model_used", "model"),
        ("latency_ms", "latency_ms"), ("strands_cycles", "cycles"),
    ):
        if data.get(key) not in (None, ""):
            bits.append(f"{label}=`{data[key]}`")
    if data.get("retried"):
        bits.append("retried=`true`")
    tu = data.get("token_usage") or {}
    if tu.get("total"):
        bits.append(
            f"tokens=`{tu.get('total')}`(in={tu.get('input', 0)},out={tu.get('output', 0)}"
            + (f",cache_r={tu['cache_read']}" if tu.get("cache_read") else "")
            + ")"
        )
    return " · ".join(bits)


def _render_trace(trace: list) -> None:
    if not trace:
        return
    with st.expander(f"🔍 工具调用链（{len(trace)} 次）"):
        for i, t in enumerate(trace, 1):
            tool = t.get("tool", "?")
            if tool == "execute_cypher":
                if t.get("blocked"):
                    st.markdown(f"**{i}. {tool}** 🚫 被 query_guard 拦下 — {t.get('reason', '')}")
                elif t.get("error"):
                    st.markdown(f"**{i}. {tool}** ❌ — `{t.get('error')}`")
                else:
                    st.markdown(f"**{i}. {tool}** — 返回 `{t.get('rows', 0)}` 行")
                cy = t.get("cypher") or t.get("cypher_preview")
                if cy:
                    st.code(cy, language="cypher")
            elif tool == "validate_cypher":
                st.markdown(f"**{i}. {tool}** {'✅' if t.get('safe') else '⚠️'} — {t.get('reason', '')}")
            elif tool == "get_schema_section":
                st.markdown(
                    f"**{i}. {tool}** — section=`{t.get('section', 'all')}`，{t.get('chars', 0)} 字符"
                )
            else:
                st.markdown(f"**{i}. {tool}**")
                st.json(t)


def _render_answer(data: dict, compact: bool = False) -> None:
    if data.get("error"):
        st.error(f"查询失败：{data['error']}")
        if data.get("cypher"):
            with st.expander("生成的 Cypher（执行失败）"):
                st.code(data["cypher"], language="cypher")
        return

    st.markdown(f"**摘要**　{data.get('summary') or '（引擎未返回摘要）'}")
    with st.expander("生成的 openCypher", expanded=not compact):
        st.code(data.get("cypher") or "（无）", language="cypher")

    rows = data.get("results") or []
    if rows:
        with st.expander(f"结果（{len(rows)} 行）", expanded=not compact):
            try:
                st.dataframe(C.df(rows), width="stretch", hide_index=True)
            except Exception:  # noqa: BLE001
                st.json(rows)
    else:
        st.info("查询返回空结果。空结果不等于错误——问题本身可能就该没有匹配。")

    meta = _meta_line(data)
    if meta:
        st.caption(meta)
    _render_trace(data.get("trace") or [])


for msg in st.session_state["chat_history"]:
    with st.chat_message(msg["role"]):
        if msg["role"] == "user":
            st.markdown(msg["content"])
        elif msg.get("compare"):
            st.markdown(f"**并排对比**：`direct` vs `strands`")
            cols = st.columns(2)
            for col, key in zip(cols, ("direct", "strands")):
                with col:
                    st.markdown(f"#### `{key}`")
                    _render_answer(msg["compare"].get(key, {}), compact=True)
        else:
            _render_answer(msg.get("data", {}))

# ── 输入 ──────────────────────────────────────────────────────────────────────
compare_mode = st.toggle(
    "⚖️ 并排对比两个引擎（同一问题分别用 direct 与 strands 跑）",
    value=False,
    disabled=not ONLINE,
    help="Strands 走 ReAct 多轮、会产生工具调用链；direct 是单轮生成加空结果重试。"
         "对比能直观看到两者的 token 用量与推理过程差异。",
)

if "pending_question" in st.session_state:
    st.session_state["_autofill"] = st.session_state.pop("pending_question")

user_input = st.chat_input(
    "输入问题，如：petsite 依赖哪些数据库？" if ONLINE else "离线模式下无法实时查询",
    disabled=not ONLINE,
)
if not user_input and st.session_state.get("_autofill"):
    user_input = st.session_state.pop("_autofill")

if user_input and ONLINE:
    with st.chat_message("user"):
        st.markdown(user_input)
    st.session_state["chat_history"].append({"role": "user", "content": user_input})

    with st.chat_message("assistant"):
        if compare_mode:
            st.markdown("**并排对比**：`direct` vs `strands`")
            results: dict = {}
            cols = st.columns(2)
            for col, eng_name in zip(cols, ("direct", "strands")):
                with col:
                    st.markdown(f"#### `{eng_name}`")
                    with st.spinner(f"{eng_name} 执行中…"):
                        t0 = time.time()
                        try:
                            eng = C.build_engine(eng_name)
                            res = eng.query(user_input)
                        except Exception as exc:  # noqa: BLE001
                            res = {"error": f"{type(exc).__name__}: {exc}", "cypher": ""}
                        res.setdefault("latency_ms", int((time.time() - t0) * 1000))
                        res.setdefault("engine", eng_name)
                    results[eng_name] = res
                    _render_answer(res, compact=True)

            # 差异摘要：观众未必会逐个展开看
            d, s = results.get("direct", {}), results.get("strands", {})
            same_cypher = (d.get("cypher") or "").strip() == (s.get("cypher") or "").strip()
            st.markdown("---")
            st.caption(
                f"**Cypher 是否相同**：{'✅ 相同' if same_cypher else '❌ 不同'}　·　"
                f"**strands 工具调用**：{len(s.get('trace') or [])} 次　·　"
                f"**token**：direct {(d.get('token_usage') or {}).get('total', '—')} / "
                f"strands {(s.get('token_usage') or {}).get('total', '—')}"
            )
            st.session_state["chat_history"].append({"role": "assistant", "compare": results})
        else:
            with st.spinner("生成查询并执行…"):
                try:
                    result = C.nlquery_engine().query(user_input)
                except Exception as exc:  # noqa: BLE001
                    result = {"error": f"{type(exc).__name__}: {exc}", "cypher": ""}
            _render_answer(result)
            st.session_state["chat_history"].append({"role": "assistant", "data": result})

    st.rerun()

# ── few-shot 对照表（离线主内容 / 在线参考）──────────────────────────────────
if FEW_SHOT:
    st.markdown("---")
    st.subheader(f"契约 few-shot 语料（{len(FEW_SHOT)} 组）")
    st.caption(
        "这是 NL 引擎实际使用的示例语料，来自 `profiles/petsite.yaml`。"
        "每组都是「一个自然语言问题 + 它对应的正确 openCypher」。"
        + ("离线模式下，它是理解这一页在做什么的最佳材料。" if not ONLINE else "")
    )
    kw = st.text_input("过滤问题", placeholder="如 依赖、AZ、Tier0", key="fs_filter")
    shown = [e for e in FEW_SHOT if not kw or kw in e.get("q", "")]
    st.caption(f"显示 {len(shown)} / {len(FEW_SHOT)} 组")
    for i, ex in enumerate(shown, 1):
        with st.expander(f"{i}. {ex['q']}"):
            st.code(ex.get("cypher", "（无）"), language="cypher")
else:
    st.warning(
        "未能从契约读到 few-shot 语料。该功能依赖 `profiles/profile_loader.py`，"
        "需要安装 `pydantic`。"
    )

# ── 空状态引导 ────────────────────────────────────────────────────────────────
if not st.session_state["chat_history"] and ONLINE:
    st.markdown("---")
    st.caption(
        "💡 想要**确定性**的查询、不经过 AI？查询库有 "
        f"{C.query_catalog_info()['count']} 条固定 Cypher，参数契约显式、同参同果。"
    )
    C.page_link("pages/2_Query_Catalog.py", "→ 打开查询库")
