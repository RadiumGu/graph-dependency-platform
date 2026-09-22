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
e2.metric("契约 few-shot", len(FEW_SHOT),
          help="每组是「一个自然语言问题 + 它对应的正确 openCypher」，"
               "来自 `profiles/petsite.yaml`。左侧折叠区可逐条查看。")
e3.caption(
    f"引擎由 `rca/engines/factory.py` 构造，**只有 strands** 一种实现。\n\n"
    f"2026-09-20 起 direct 实现已删除、factory 也去掉了回退分支 —— "
    f"strands 不可用时**直接抛异常**，不再静默降级。\n\n"
    f"去掉回退是有意的：那个回退曾让线上在包里没装 strands 的情况下"
    f"一直静默跑 direct，而 golden 基线测的是装了 strands 的环境 —— "
    f"两者不一致了五个月没人发现。\n\n"
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
            "**Strands Agent（ReAct 2 轮）**\n\n"
            "1. 按关键词选模型：Sonnet，命中「完整/影响/路径」等复杂词升 Opus\n"
            "2. cycle 1：据 system prompt 里的完整 schema 生成 Cypher → "
            "调 `execute_cypher`（内部强制 `query_guard.is_safe()`）\n"
            "3. cycle 2：据结果生成中文摘要\n\n"
            "2026-09-20 调优：原为 3 轮（中间一轮 `validate_cypher`）。"
            "安全校验在 `execute_cypher` 内部无条件执行、不依赖 Agent 先调 validate，"
            "那一轮是净亏 → p99 降 27.6%、token 降 33.4%，准确率仍 20/20。\n\n"
            "每个回答下方可展开完整的工具调用链。"
        )
    else:
        # 2026-09-21：原先这里有 `elif engine_name == "direct"` 的说明分支。
        # direct 实现已于 2026-09-20 删除，engine_name 不可能再取到 "direct"，
        # 那段说明成了描述不存在实现的死代码。
        st.caption("引擎不可用，无法展示工作原理。")

    st.markdown("---")
    # ⚠️ 2026-09-21 删掉「⚖️ 并排对比两个引擎」开关。
    #
    # 它的两列是 `("direct", "strands")`，各自调 `C.build_engine(name)`。而
    # 2026-09-20 起 factory 无条件返回 StrandsNLQueryEngine、完全忽略
    # NLQUERY_ENGINE —— 于是两列构造出的是**同一个引擎**，「direct」那列只是
    # 被贴错标签的 strands 输出，「Cypher 是否相同 ✅ 相同」恒真、token 对比
    # 毫无意义。
    #
    # 而同一页页头已经写明 direct 已删除：一边声明它不存在，一边提供「和它
    # 对比」的入口，是本项目反复清理的那类自相矛盾。
    #
    # 刻意不改成「strands 两种配置对比」：那需要 factory 支持按参数构造
    # （如 2 轮 vs 3 轮 ReAct），属功能新增而非缺陷修复，不在此处夹带。

    st.markdown("---")
    st.caption(
        "**安全边界**　`query_guard` 拦截 "
        "`CREATE / DELETE / DETACH / SET / MERGE / REMOVE / DROP / CALL`，"
        "限制最大跳数 6、默认 LIMIT 200。写操作在这里就被挡掉，不依赖 IAM。"
    )

    # ── 契约 few-shot 语料 ────────────────────────────────────────────────────
    # 原来这一块渲染在**主区、对话之后**，一个过滤输入框加 N 个 expander，
    # 把答案顶到很上面、输入框压到很下面，中间隔几十行 —— 这正是用户报的
    # 「对话框在最下面，被很多内容隔开，答案在最上面」。
    # 挪到侧栏折叠区：离线时它仍是理解本页的主材料，所以不删，只是收起来。
    with st.expander(f"📚 契约 few-shot 语料（{len(FEW_SHOT)} 组）",
                     expanded=not ONLINE):
        if FEW_SHOT:
            st.caption(
                "NL 引擎实际使用的示例语料，来自 `profiles/petsite.yaml`。"
                "每组是「一个自然语言问题 + 它对应的正确 openCypher」。"
            )
            kw = st.text_input("过滤问题", placeholder="如 依赖、AZ、Tier0",
                               key="fs_filter")
            shown = [e for e in FEW_SHOT if not kw or kw in e.get("q", "")]
            st.caption(f"显示 {len(shown)} / {len(FEW_SHOT)} 组")
            for i, ex in enumerate(shown, 1):
                st.markdown(f"**{i}. {ex['q']}**")
                st.code(ex.get("cypher", "（无）"), language="cypher")
        else:
            st.warning(
                "未能从契约读到 few-shot 语料。该功能依赖 "
                "`profiles/profile_loader.py`，需要安装 `pydantic`。"
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
                # 该 tool 已于 2026-09-20 删除（schema 完整在 system prompt，实测调用 0 次）。
                # 这个分支保留用于渲染删除前存档的历史 trace。
                st.markdown(
                    f"**{i}. {tool}** — section=`{t.get('section', 'all')}`，{t.get('chars', 0)} 字符"
                    "  ·  _该工具已于 2026-09-20 移除_"
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


# ── 空状态引导：放在对话**上方** ──────────────────────────────────────────────
# 原来它在页面最下面（few-shot 之后），第一次打开的人根本看不到。
if not st.session_state["chat_history"]:
    if ONLINE:
        st.info(
            "在**页面底部**的输入框提问，或点左侧「示例问题」直接填入。\n\n"
            "💡 想要**确定性**的查询、不经过 AI？查询库有 "
            f"{C.query_catalog_info()['count']} 条固定 Cypher，"
            "参数契约显式、同参同果。",
            icon="💬",
        )
        C.page_link("pages/2_Query_Catalog.py", "→ 打开查询库")
    else:
        st.warning(
            "**离线模式** —— 自然语言查询需要 Neptune 与 Bedrock，"
            "所以底部输入框是禁用状态（不是坏了）。\n\n"
            "左侧「📚 契约 few-shot 语料」已展开，那是理解这一页在做什么的最佳材料："
            "每组都是一个真实问题和它对应的正确 openCypher。",
            icon="🔵",
        )

for msg in st.session_state["chat_history"]:
    with st.chat_message(msg["role"]):
        if msg["role"] == "user":
            st.markdown(msg["content"])
        elif msg.get("compare"):
            # 兼容分支：2026-09-21 之前的会话可能在 chat_history 里留下 compare
            # 记录。新会话不会再产生（并排对比功能已删，见上方注释），但既有
            # session_state 里的旧记录仍要能渲染而不是抛 KeyError。
            st.caption("⚠️ 这是双引擎并排对比的历史记录。该功能已于 2026-09-21 "
                       "删除——它的两列实际是同一个 strands 引擎。")
            for key, res in (msg.get("compare") or {}).items():
                st.markdown(f"#### `{key}`")
                _render_answer(res, compact=True)
        else:
            _render_answer(msg.get("data", {}))

# ── 输入 ──────────────────────────────────────────────────────────────────────
#
# ## 布局:主区只放「对话」,别的都挪走(2026-09-07 修)
#
# 用户报的是「对话框在最下面,被很多内容隔开,答案在最上面,看着很不方便」。
# 根因不是 chat_input 的位置 —— `st.chat_input` 在顶层**总是固定在页面底部**,
# 这是 Streamlit 的行为,也是聊天界面的常规。真正的问题是**它和对话之间塞了东西**:
#
#     标题 / 引擎状态
#     对话历史          ← 答案在这里
#     并排对比开关
#     [chat_input 固定在底部]
#     新一轮问答渲染
#     契约 few-shot 语料（一个过滤输入框 + N 个 expander）← 又长又占地
#     空状态引导
#
# few-shot 那一大块渲染在对话之后,于是答案被顶到很上面,输入框在很下面,
# 中间隔着几十行。修法是把**所有非对话内容挪出主区**:
#   · 并排对比开关 → 侧栏（它是配置,不是内容）
#   · few-shot 语料 → 侧栏折叠区（离线时它是主内容,所以不能删,但可以收起）
#   · 空状态引导   → 只在没有对话时显示,且放在对话上方
#
# 这样主区自上而下只有「引擎状态 → 对话 → 输入框」,答案永远紧贴输入框上方。
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
        # 2026-09-21：原先这里有 `if compare_mode:` 的双引擎并排分支，
        # 已随「并排对比两个引擎」开关一起删除（理由见上方开关处的注释：
        # 两列构造的是同一个 strands 引擎，direct 那列是贴错标签的输出）。
        with st.spinner("生成查询并执行…"):
            try:
                result = C.nlquery_engine().query(user_input)
            except Exception as exc:  # noqa: BLE001
                result = {"error": f"{type(exc).__name__}: {exc}", "cypher": ""}
        _render_answer(result)
        st.session_state["chat_history"].append({"role": "assistant", "data": result})

    st.rerun()

# ── few-shot 语料与空状态引导已移出主区 ──────────────────────────────────────
#
# few-shot 对照表移到了侧栏折叠区，空状态引导移到了对话**上方**（见下面
# `_empty_state_hint()` 的调用点）。主区自上而下现在只有：
#
#     标题 / 引擎状态 → （空状态引导）→ 对话历史 → [chat_input 固定底部]
#
# 这样答案永远紧贴输入框上方，不会再被几十行语料隔开。
