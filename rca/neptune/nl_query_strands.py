"""
nl_query_strands.py - Strands Agents 实现的 Smart Query 引擎。

流程（ReAct）:
  1. Agent 收到自然语言问题
  2. Agent 自行决定调用 get_schema_section / validate_cypher / execute_cypher
  3. execute_cypher 内部强制 query_guard.is_safe()
  4. 引擎从 trace 中提取最终 cypher + results
  5. 调用 _summarize 生成中文摘要（与 direct 版共用逻辑，但不复用代码以保持解耦）

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
from engines.strands_common import DEFAULT_MODEL, DEFAULT_REGION, HEAVY_MODEL, build_bedrock_model
from neptune import strands_tools as st_tools
from neptune.schema_prompt import build_system_prompt

logger = logging.getLogger(__name__)


_AGENT_RULES = (
    "\n\n## Agent 调用规则\n"
    "1. 先思考问题涉及哪些节点/关系。如不确定，调用 get_schema_section。\n"
    "2. 生成 Cypher 后先调用 validate_cypher 校验；通过后再调用 execute_cypher。\n"
    "3. 不得直接拼接未经 validate_cypher 的查询。\n"
    "4. 必须完整保留问题中的所有过滤条件（如 severity='P0'、tier='Tier0'、name='petsite' 等），不得泛化。"
    "例：问“所有 P0 故障”必须生成 WHERE inc.severity = 'P0'，不得返回全部 Incident。\n"
    "5. 如果结果为空且你怀疑关系名写错了（尤其 AccessesData vs DependsOn），换一个常见关系名重试最多 1 次。\n"
    "6. 微服务访问数据库（RDS / DynamoDB / S3）用 AccessesData，不是 DependsOn。\n"
    "7. 最后用 2-4 句中文总结结果，直接给结论。"
)


class StrandsNLQueryEngine(NLQueryBase):
    """Strands Agent-based NL Query engine."""

    ENGINE_NAME = "strands"

    def __init__(self, profile: Any = None) -> None:
        super().__init__(profile=profile)
        if self.profile is None:
            from profiles.profile_loader import EnvironmentProfile
            self.profile = EnvironmentProfile()
        self.bedrock = boto3.client("bedrock-runtime", region_name=DEFAULT_REGION)
        self.system_prompt = build_system_prompt(self.profile) + _AGENT_RULES

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
        last = st_tools.last_execution()
        cypher = last.get("cypher", "")
        # 真正的结果不在 trace（被截断 4000 字符）；重新执行确认拿完整 results
        results: list = []
        if cypher:
            from neptune import neptune_client as nc
            from neptune import query_guard
            safe, _ = query_guard.is_safe(cypher)
            if safe:
                try:
                    results = nc.results(query_guard.ensure_limit(cypher))
                except Exception as e:
                    logger.warning("Strands engine re-exec failed: %s", e)

        summary = self._extract_agent_text(resp) or self._fallback_summary(results)
        tokens = self._extract_token_usage(resp)

        return self._pack(
            question, cypher=cypher, results=results, summary=summary,
            model=model_id, t0=t0, trace=trace, tokens=tokens,
        )

    # ------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------

    def _build_agent(self, model_id: str):
        from strands import Agent  # type: ignore
        model = build_bedrock_model(model_id=model_id, region=DEFAULT_REGION)
        return Agent(
            model=model,
            tools=[st_tools.get_schema_section, st_tools.validate_cypher, st_tools.execute_cypher],
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
        """尽力抽 token usage；拿不到就返回 None（NLQueryBase 契约允许）。"""
        try:
            meta = getattr(resp, "metrics", None) or getattr(resp, "metadata", None)
            if not meta:
                return None
            it = int(meta.get("input_tokens", 0) or meta.get("inputTokens", 0) or 0)
            ot = int(meta.get("output_tokens", 0) or meta.get("outputTokens", 0) or 0)
            if it == 0 and ot == 0:
                return None
            return {"input": it, "output": ot, "total": it + ot}
        except Exception:
            return None

    def _fallback_summary(self, results: list) -> str:
        if not results:
            return "查询无结果。"
        return f"查询返回 {len(results)} 行结果。"

    def _pack(self, question: str, *, cypher: str, results: list, summary: str,
              model: str, t0: float, trace: list | None = None,
              tokens: dict | None = None, error: str | None = None) -> dict:
        return {
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
