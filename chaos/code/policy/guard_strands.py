"""
guard_strands.py — Strands Agent PolicyGuard: 单次 LLM 调用判断 allow/deny。

关键设计（TASK §6）：每次 evaluate() 新建 Agent 实例，不复用。
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from policy.base import PolicyGuardBase  # type: ignore
from policy.guard_direct import _load_rules, _build_system_prompt  # type: ignore

logger = logging.getLogger(__name__)


def _assert_cacheable(system_prompt: str, min_tokens: int = 1024):
    """验证 system prompt 达到缓存下限。"""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        token_count = len(enc.encode(system_prompt))
    except Exception:
        token_count = len(system_prompt) // 4  # rough estimate

    logger.info("PolicyGuard system prompt: %d tokens (min=%d) %s",
                token_count, min_tokens, "✓" if token_count >= min_tokens else "✗")

    if token_count < min_tokens:
        raise AssertionError(
            f"System prompt only {token_count} tokens, "
            f"below {min_tokens} minimum for Bedrock prompt caching."
        )


class StrandsPolicyGuard(PolicyGuardBase):
    """Strands PolicyGuard — 每次调用新建 Agent 实例。"""

    ENGINE_NAME = "strands"

    def __init__(self, rules_path: str | None = None) -> None:
        super().__init__(rules_path=rules_path)
        self._rules = _load_rules(rules_path)
        self._system_prompt = _build_system_prompt(self._rules)
        _assert_cacheable(self._system_prompt)
        self._region = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "ap-northeast-1"
        self._model_id = os.environ.get("BEDROCK_MODEL") or "global.anthropic.claude-sonnet-4-6"

    def _build_agent(self):
        """每次调用新建 Agent。"""
        from strands import Agent  # type: ignore
        from strands.models import BedrockModel, CacheConfig  # type: ignore

        model = BedrockModel(
            model_id=self._model_id,
            region_name=self._region,
            max_tokens=1024,
            cache_config=CacheConfig(strategy="auto"),
        )
        return Agent(
            model=model,
            system_prompt=self._system_prompt,
            # 不设 conversation_manager — 单次调用不需要
        )

    def evaluate(self, experiment: dict, context: dict | None = None) -> dict:
        t0 = time.time()
        context = context or {}

        user_msg = (
            "Evaluate the following chaos experiment:\n\n"
            f"**Experiment:**\n```json\n{json.dumps(experiment, indent=2, default=str)}\n```\n\n"
            f"**Context:**\n```json\n{json.dumps(context, indent=2, default=str)}\n```\n\n"
            "Apply all relevant rules and respond with the JSON decision."
        )

        try:
            agent = self._build_agent()
            result = agent(user_msg)
            text = str(result) if result else ""

            # Extract token usage
            token_usage = self._extract_token_usage(result)
            decision = self._parse_response(text)
            elapsed_ms = int((time.time() - t0) * 1000)

            return {
                **decision,
                "engine": "strands",
                "model_used": self._model_id,
                "latency_ms": elapsed_ms,
                "token_usage": token_usage,
                "error": None,
            }

        except Exception as e:
            logger.error("PolicyGuard strands call failed: %s", e)
            elapsed_ms = int((time.time() - t0) * 1000)
            return {
                "decision": "deny",
                "reasoning": f"PolicyGuard error (fail-closed): {e}",
                "matched_rules": [],
                "confidence": 0.0,
                "engine": "strands",
                "model_used": self._model_id,
                "latency_ms": elapsed_ms,
                "token_usage": None,
                "error": str(e),
            }

    @staticmethod
    def _extract_token_usage(result) -> dict | None:
        """Extract token usage via metrics.get_summary()."""
        try:
            metrics = result.metrics
            if not metrics:
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
            return {"input": it, "output": ot, "total": tt,
                    "cache_read": cr, "cache_write": cw}
        except Exception:
            return None

    @staticmethod
    def _parse_response(text: str) -> dict:
        """Parse LLM JSON response. Fail-closed on parse error."""
        import re
        json_match = re.search(r"\{[\s\S]*\}", text)
        if json_match:
            try:
                data = json.loads(json_match.group())
                return {
                    "decision": data.get("decision", "deny"),
                    "reasoning": data.get("reasoning", ""),
                    "matched_rules": data.get("matched_rules", []),
                    "confidence": float(data.get("confidence", 0.0)),
                }
            except (json.JSONDecodeError, ValueError):
                pass
        return {
            "decision": "deny",
            "reasoning": f"Failed to parse LLM response (fail-closed). Raw: {text[:200]}",
            "matched_rules": [],
            "confidence": 0.0,
        }
