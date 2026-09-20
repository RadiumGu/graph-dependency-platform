"""
nl_query_strands.py - Strands Agents 实现的 Smart Query 引擎。

流程（ReAct，2026-09-20 起 2 轮）:
  1. Agent 收到自然语言问题（schema 已完整在 system prompt 里）
  2. cycle 1: 生成 Cypher → 直接调 execute_cypher（内部强制 query_guard.is_safe()）
  3. cycle 2: 收到结果 → 生成 2-4 句中文摘要
  4. 引擎从 st_tools.last_rows() 取**完整**结果（不再重跑 Neptune）

  优化前是 3 轮：中间多一轮 validate_cypher，而安全校验在 execute 内部
  本就无条件执行，那一轮纯属浪费。详见 _AGENT_RULES 下方注释。

硬约束（TASK § 6）:
  - 不启用 Wave 4 的 _should_retry_on_empty 逻辑（依赖 Strands 原生 ReAct 多轮）
  - Wave 5 的 Opus 升级通过 complex_keywords 关键词匹配 + _select_model 在构造时选定
  - BedrockModel 必须用 inference profile id + 显式 region
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import boto3

from engines.base import NLQueryBase
from engines.strands_common import DEFAULT_MODEL, DEFAULT_REGION, HEAVY_MODEL, build_bedrock_model, ensure_telemetry
from neptune import strands_tools as st_tools
from neptune.schema_prompt import build_system_prompt

logger = logging.getLogger(__name__)


_AGENT_RULES = (
    "\n\n## Agent 调用规则\n"
    "1. 上面已给出**完整** schema，直接据此生成 Cypher。\n"
    "2. 生成 Cypher 后**直接调用 execute_cypher**。它内部强制安全校验，"
    "不安全会返回 \"ERROR: guard blocked...\"，届时你再修正重试即可。\n"
    "3. 不必在执行前先调 validate_cypher —— 那会多花一轮。"
    "只在你对某个可疑写法想预检时才用它。\n"
    "4. 必须完整保留问题中的所有过滤条件（如 severity='P0'、tier='Tier0'、name='petsite' 等），不得泛化。"
    "例：问“所有 P0 故障”必须生成 WHERE inc.severity = 'P0'，不得返回全部 Incident。\n"
    "5. 如果结果为空且你怀疑关系名写错了（尤其 AccessesData vs DependsOn），换一个常见关系名重试最多 1 次。\n"
    "6. 微服务访问数据库（RDS / DynamoDB / S3）用 AccessesData，不是 DependsOn。\n"
    "7. 最后用 2-4 句中文总结结果，直接给结论。"
)

# ── 2026-09-20 规则调优：3 轮 ReAct 压到 2 轮 ────────────────────────────
#
# 实测（6 条 golden 查询）优化前每次查询固定 3 个 cycle：
#
#   cycle 1  LLM 生成 cypher      → 调 validate_cypher
#   cycle 2  LLM 收到 "OK"        → 调 execute_cypher
#   cycle 3  LLM 收到结果         → 生成中文总结
#
# 中间那轮**纯属浪费**：`execute_cypher` 内部无条件执行
# `query_guard.is_safe()`，安全性从来不依赖 Agent 先调 validate。
# 原规则 2/3（"先 validate 再 execute"、"不得直接拼接未经 validate 的查询"）
# 是在**请求 Agent 配合**做一件代码已经强制了的事。
#
# ⚠️ 这不降低安全性，理由必须说清楚，否则以后会被误读成削弱防线：
#   · 真正的防线是 `execute_cypher` 里那句无条件的 `is_safe()`，
#     它挡的是 Agent **已经决定要执行**的语句，Agent 配合与否都挡得住；
#   · 对不安全查询，两条路径的轮数完全相同 —— 直接 execute 被 guard 挡回
#     的反馈，和 validate 返回 UNSAFE 的反馈，都是一轮；
#   · 对安全查询（绝大多数），先 validate 是净亏一轮。
#   · `validate_cypher` 工具本身保留，Agent 想预检仍可调。
#
# 同时删掉 `get_schema_section`（规则 1 原本引导 Agent 调它）：
# 那份 schema 已完整在 system prompt 里，实测被调用 0 次。
# 详见 `neptune/strands_tools.py` 里删除处的注释。


class StrandsNLQueryEngine(NLQueryBase):
    """Strands Agent-based NL Query engine."""

    ENGINE_NAME = "strands"

    def __init__(self, profile: Any = None) -> None:
        super().__init__(profile=profile)
        if self.profile is None:
            from profiles.profile_loader import EnvironmentProfile
            self.profile = EnvironmentProfile()
        ensure_telemetry()  # STRANDS_TELEMETRY=console|otlp 时激活
        self.bedrock = boto3.client("bedrock-runtime", region_name=DEFAULT_REGION)
        self.system_prompt = build_system_prompt(self.profile) + _AGENT_RULES
        # L2 Prompt Caching: system prompt 太短 → Bedrock 静默跳过缓存。
        # Sonnet/Opus 最低缓存 ~1024 tokens ≈ 3000 chars。
        assert len(self.system_prompt) > 3000, (
            f"system prompt too short for prompt caching: {len(self.system_prompt)} chars"
        )

    # ------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------

    def query(self, question: str) -> dict:
        t0 = time.time()
        model_id = self._select_model(question)
        # 让 @tool 拿到当前 profile + 重置 trace
        st_tools.set_profile(self.profile)
        st_tools.reset_trace()

        try:
            agent = self._build_agent(model_id)
        except Exception as e:
            logger.warning("Strands agent build failed: %s", e)
            return self._pack(question, cypher="", results=[], summary="", model=model_id,
                              t0=t0, error=f"agent build failed: {e!r}")

        try:
            resp = agent(question)
        except Exception as e:
            logger.warning("Strands agent invocation failed: %s", e)
            return self._pack(question, cypher="", results=[], summary="", model=model_id,
                              t0=t0, error=repr(e))

        trace = st_tools.get_trace()
        # 完整结果直接从 tool 取 —— 它执行时就存下了未截断的 rows。
        #
        # 2026-09-20 之前这里是**重新跑一次 Neptune 查询**：因为给 LLM 的
        # tool 返回被截断到 4000 字符，引擎拿不到完整 results。于是每次查询
        # 都白跑两趟 Neptune（Agent 一趟、引擎一趟）查同一条 cypher。
        full = st_tools.last_rows()
        cypher = full.get("cypher") or ""
        results: list = full.get("rows") if full.get("rows") is not None else []

        summary = self._extract_agent_text(resp) or self._fallback_summary(results)
        tokens = self._extract_token_usage(resp)
        strands_cycles = self._extract_cycles(resp)

        return self._pack(
            question, cypher=cypher, results=results, summary=summary,
            model=model_id, t0=t0, trace=trace, tokens=tokens,
            extra={"strands_cycles": strands_cycles,
                   "latency_ms_agent": self._extract_agent_latency_ms(resp)},
        )

    # ------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------

    def _build_agent(self, model_id: str):
        from strands import Agent  # type: ignore
        model = build_bedrock_model(model_id=model_id, region=DEFAULT_REGION)
        return Agent(
            model=model,
            # get_schema_section 已删（schema 完整在 system prompt，实测调用 0 次）。
            # validate_cypher 保留但不再强制前置，见 _AGENT_RULES 下方注释。
            tools=[st_tools.validate_cypher, st_tools.execute_cypher],
            system_prompt=self.system_prompt,
        )

    def _select_model(self, question: str) -> str:
        """Wave 5 等价实现：命中 complex_keywords → Opus。"""
        ck = self.profile.neptune_complex_keywords if self.profile else {}
        needles = list((ck or {}).get("zh") or []) + list((ck or {}).get("en") or [])
        if not needles:
            return DEFAULT_MODEL
        ql = question.lower()
        for kw in needles:
            kw_l = (kw or "").lower()
            if kw_l and kw_l in ql:
                logger.info("Strands engine upgrading model to %s (kw=%s)", HEAVY_MODEL, kw)
                return HEAVY_MODEL
        return DEFAULT_MODEL

    def _extract_cycles(self, resp: Any) -> int | None:
        try:
            m = getattr(resp, "metrics", None)
            return int(getattr(m, "cycle_count", 0) or 0) or None
        except Exception:
            return None

    def _extract_agent_text(self, resp: Any) -> str:
        """从 Strands AgentResult 抽取文本摘要。"""
        try:
            msg = getattr(resp, "message", None)
            if isinstance(msg, dict):
                content = msg.get("content") or []
                if content and isinstance(content[0], dict):
                    return str(content[0].get("text") or "").strip()
            if msg:
                return str(msg).strip()
        except Exception:
            pass
        return str(resp).strip() if resp else ""

    def _extract_token_usage(self, resp: Any) -> dict | None:
        """从 Strands AgentResult 中获取 token usage。

        Strands 1.36+: resp.metrics.get_summary()['accumulated_usage'] 提供
        精确的 inputTokens/outputTokens/totalTokens。
        """
        try:
            metrics = getattr(resp, "metrics", None)
            if metrics is None:
                return None
            summary = metrics.get_summary()
            usage = summary.get("accumulated_usage") or {}
            it = int(usage.get("inputTokens", 0) or 0)
            ot = int(usage.get("outputTokens", 0) or 0)
            cr = int(usage.get("cacheReadInputTokens", 0) or 0)
            cw = int(usage.get("cacheWriteInputTokens", 0) or 0)
            tt = int(usage.get("totalTokens", it + ot + cr + cw) or 0)
            if it == 0 and ot == 0 and cr == 0 and cw == 0:
                return None
            return {
                "input": it,
                "output": ot,
                "total": tt,
                "cache_read": cr,
                "cache_write": cw,
            }
        except Exception as e:
            logger.debug("token usage extraction failed: %r", e)
            return None

    def _extract_agent_latency_ms(self, resp: Any) -> int | None:
        """Strands 内部报告的 accumulated latency（可与我们脚手架的 wall-clock 对比）。"""
        try:
            metrics = getattr(resp, "metrics", None)
            if metrics is None:
                return None
            summary = metrics.get_summary()
            return int((summary.get("accumulated_metrics") or {}).get("latencyMs", 0) or 0) or None
        except Exception:
            return None

    def _fallback_summary(self, results: list) -> str:
        if not results:
            return "查询无结果。"
        return f"查询返回 {len(results)} 行结果。"

    def _pack(self, question: str, *, cypher: str, results: list, summary: str,
              model: str, t0: float, trace: list | None = None,
              tokens: dict | None = None, error: str | None = None,
              extra: dict | None = None) -> dict:
        out = {
            "question": question,
            "cypher": cypher,
            "results": results,
            "summary": summary,
            "retried": False,  # Strands 版不用 Wave 4 显式重试
            "engine": self.ENGINE_NAME,
            "model_used": model,
            "latency_ms": int((time.time() - t0) * 1000),
            "token_usage": tokens,
            "trace": list(trace or []),
            "error": error,
        }
        if extra:
            out.update({k: v for k, v in extra.items() if v is not None})
        return out
