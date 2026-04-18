"""
Smart Query — 自然语言图查询

NL → openCypher → 执行 → AI 摘要，聊天式 UI。
"""
import json
import os
import sys

import streamlit as st

# ── 路径设置 ──────────────────────────────────────────────────────────────────
_DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_DEMO_DIR, "..", ".."))
_RCA_ROOT = os.path.join(_PROJECT_ROOT, "rca")

for _p in [_PROJECT_ROOT, _RCA_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault(
    "NEPTUNE_ENDPOINT",
    "petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com",
)
os.environ.setdefault("REGION", "ap-northeast-1")
os.environ.setdefault("BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6")

st.set_page_config(page_title="Smart Query", page_icon="💬", layout="wide")

# ── 示例问题 ──────────────────────────────────────────────────────────────────
EXAMPLE_QUESTIONS = [
    "petsite 依赖哪些数据库？",
    "AZ ap-northeast-1a 有哪些 Tier0 服务？",
    "payforadoption 的上游调用者有哪些？",
    "哪些服务从未做过混沌实验？",
    "petsite 完整的基础设施路径（到 AZ）",
    "最近发生过哪些 P0 故障？",
    "列出所有 Microservice 节点及其 tier",
    "petsearch 的 Pod 运行在哪些 EC2 节点上？",
]

# ── UI ────────────────────────────────────────────────────────────────────────
st.title("💬 Smart Query")
st.markdown(
    "用自然语言查询 Neptune 图谱。AI 自动将问题转为 openCypher 查询并执行，返回结构化结果和中文摘要。"
)

with st.sidebar:
    st.header("示例问题")
    st.markdown("点击快速填入：")
    for q in EXAMPLE_QUESTIONS:
        if st.button(q, key=f"ex_{q}", use_container_width=True):
            st.session_state["pending_question"] = q

    st.markdown("---")
    with st.expander("📖 查询示例参考（Few-shot）", expanded=False):
        from neptune.schema_prompt import FEW_SHOT_EXAMPLES
        for i, ex in enumerate(FEW_SHOT_EXAMPLES, 1):
            st.markdown(f"**{i}. {ex['q']}**")
            st.code(ex['cypher'], language='cypher')

    st.markdown("**工作原理**")
    st.markdown(
        """
1. 自然语言问题
2. Claude Sonnet 生成 openCypher
3. 安全校验（拦截写操作）
4. Neptune 执行查询
5. Claude 生成中文摘要
"""
    )
    if st.button("🗑️ 清空对话", use_container_width=True):
        st.session_state["chat_history"] = []
        st.rerun()

# ── 对话历史 ──────────────────────────────────────────────────────────────────
if "chat_history" not in st.session_state:
    st.session_state["chat_history"] = []

# 展示历史消息
for msg in st.session_state["chat_history"]:
    with st.chat_message(msg["role"]):
        if msg["role"] == "user":
            st.markdown(msg["content"])
        else:
            # assistant message has structured content
            data = msg.get("data", {})
            if data.get("error"):
                st.error(f"查询失败：{data['error']}")
                if data.get("cypher"):
                    with st.expander("生成的 Cypher（失败）"):
                        st.code(data["cypher"], language="cypher")
            else:
                st.markdown(f"**摘要**: {data.get('summary', '')}")
                with st.expander("查看生成的 openCypher 查询"):
                    st.code(data.get("cypher", ""), language="cypher")
                results = data.get("results", [])
                if results:
                    with st.expander(f"查询结果（{len(results)} 条）"):
                        import pandas as pd
                        try:
                            df = pd.DataFrame(results)
                            st.dataframe(df, use_container_width=True, hide_index=True)
                        except Exception:
                            st.json(results)
                else:
                    st.info("查询返回空结果。")

# ── 处理 pending question（来自侧边栏快速填入）────────────────────────────────
if "pending_question" in st.session_state:
    pending = st.session_state.pop("pending_question")
    # 自动写入 chat input 区域（通过 session_state）
    st.session_state["_autofill"] = pending

# ── 输入框 ────────────────────────────────────────────────────────────────────
user_input = st.chat_input("输入你的问题，如：petsite 依赖哪些服务？")

# 也处理自动填入
if not user_input and st.session_state.get("_autofill"):
    user_input = st.session_state.pop("_autofill")

if user_input:
    # 显示用户消息
    with st.chat_message("user"):
        st.markdown(user_input)
    st.session_state["chat_history"].append({"role": "user", "content": user_input})

    # 执行查询
    with st.chat_message("assistant"):
        with st.spinner("AI 正在生成查询并执行…"):
            try:
                from engines.factory import make_nlquery_engine
                engine = make_nlquery_engine()
                result = engine.query(user_input)
            except Exception as exc:
                result = {"error": str(exc), "cypher": ""}

        if result.get("error"):
            st.error(f"查询失败：{result['error']}")
            if result.get("cypher"):
                with st.expander("生成的 Cypher（失败）"):
                    st.code(result["cypher"], language="cypher")
        else:
            st.markdown(f"**摘要**: {result.get('summary', '（无摘要）')}")
            with st.expander("查看生成的 openCypher 查询"):
                st.code(result.get("cypher", ""), language="cypher")
            results = result.get("results", [])
            if results:
                with st.expander(f"查询结果（{len(results)} 条）"):
                    import pandas as pd
                    try:
                        df = pd.DataFrame(results)
                        st.dataframe(df, use_container_width=True, hide_index=True)
                    except Exception:
                        st.json(results)
            else:
                st.info("查询返回空结果。")

            # Engine 元数据 + Tool Trace（Strands L1 POC）
            meta_bits = []
            if result.get("engine"):
                meta_bits.append(f"engine=`{result['engine']}`")
            if result.get("model_used"):
                meta_bits.append(f"model=`{result['model_used']}`")
            if result.get("latency_ms") is not None:
                meta_bits.append(f"latency=`{result['latency_ms']}ms`")
            if result.get("retried"):
                meta_bits.append("retried=`true`")
            tu = result.get("token_usage") or {}
            if tu.get("total"):
                meta_bits.append(f"tokens=`{tu.get('total')}` (in={tu.get('input', 0)}, out={tu.get('output', 0)})")
            if meta_bits:
                st.caption(" · ".join(meta_bits))

            trace = result.get("trace") or []
            if trace:
                with st.expander(f"🔍 Tool Trace（{len(trace)} 次调用）"):
                    for i, t in enumerate(trace, 1):
                        tool_name = t.get("tool", "?")
                        if tool_name == "execute_cypher":
                            if t.get("blocked"):
                                st.markdown(f"**{i}. {tool_name}** 🚫 blocked — {t.get('reason', '')}")
                            elif t.get("error"):
                                st.markdown(f"**{i}. {tool_name}** ❌ error — `{t.get('error', '')}`")
                            else:
                                st.markdown(f"**{i}. {tool_name}** — rows=`{t.get('rows', 0)}`")
                                cypher = t.get("cypher") or t.get("cypher_preview", "")
                                if cypher:
                                    st.code(cypher, language="cypher")
                        elif tool_name == "validate_cypher":
                            safe = t.get("safe")
                            icon = "✅" if safe else "⚠️"
                            st.markdown(f"**{i}. {tool_name}** {icon} — {t.get('reason', '')}")
                        elif tool_name == "get_schema_section":
                            st.markdown(
                                f"**{i}. {tool_name}** — section=`{t.get('section', 'all')}`, chars={t.get('chars', 0)}"
                            )
                        else:
                            st.markdown(f"**{i}. {tool_name}**")
                            st.json(t)

    st.session_state["chat_history"].append(
        {"role": "assistant", "data": result}
    )
    st.rerun()

# ── 空状态提示 ────────────────────────────────────────────────────────────────
if not st.session_state["chat_history"]:
    st.info(
        "👆 在输入框中输入问题，或点击左侧示例问题开始查询。\n\n"
        "示例：`petsite 依赖哪些数据库？`"
    )
    st.markdown("---")
    st.subheader("支持的查询类型")
    col1, col2 = st.columns(2)
    with col1:
        st.markdown(
            """
**拓扑查询**
- 服务依赖关系
- 上下游调用链
- 基础设施路径（Pod→EC2→AZ）
- ALB → TargetGroup → Service

**资源查询**
- 特定 AZ 的服务列表
- Tier0/Tier1 关键服务
- EC2 实例状态
"""
        )
    with col2:
        st.markdown(
            """
**运维查询**
- 历史故障记录（Incident）
- 混沌实验历史
- 服务恢复优先级

**业务查询**
- BusinessCapability 影响面
- 服务 SLA 级别
- 从未测试的服务
"""
        )
