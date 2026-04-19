"""
layer2_strands.py — Strands multi-agent Layer2 Prober 实现。

架构（方案 C — TASK §5.3 推荐）：
  - 1 个 Orchestrator Strands Agent，拥有 6 个 @tool
  - Agent 的 LLM 决定调哪些 tool、解读结果、生成关联分析
  - 不用 agent.as_tool() 嵌套（TASK §6.2 硬规则 #2）

增值点 vs Direct：
  - 多轮 ReAct 深入探查（tool 返回后 agent 可追问）
  - 跨 Prober 关联分析（SQS 积压 + Pod crash → 归因为"消费者故障"）
  - 异常归因（不只说"有异常"，还说"因为什么"）
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from engines.base import Layer2ProberBase

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# System Prompt
# ─────────────────────────────────────────────────────────

_ORCHESTRATOR_SYSTEM_PROMPT = """\
You are an AWS RCA (Root Cause Analysis) Layer2 Prober Orchestrator.

## Mission
Given an alert signal and affected service, you orchestrate 6 specialized probing tools
to detect anomalies across AWS infrastructure, then synthesize findings into a structured
root cause analysis report.

## Available Probes
1. **probe_cloudwatch** — SQS queue backlog/DLQ + DynamoDB throttling/errors
2. **probe_xray** — Step Functions execution failures/timeouts/throttles
3. **probe_neptune** — Service dependency topology from Neptune graph
4. **probe_logs** — Lambda function errors/throttles/near-timeout
5. **probe_deployment** — EKS node health (non-running instances)
6. **probe_network** — ALB 5xx errors, high latency, unhealthy targets

## Probing Strategy
- **Always call**: probe_cloudwatch, probe_logs, probe_network (broad coverage)
- **Call if relevant**: probe_xray (if service uses Step Functions), probe_neptune (for topology context)
- **Call conditionally**: probe_deployment (only if neptune_infra_fault=False in signal)

## Output Requirements
After collecting tool results, produce a JSON analysis with these fields:
```json
{
  "anomalies": [
    {
      "source": "<probe_name>",
      "service_name": "<AWS service>",
      "healthy": false,
      "score_delta": <0-40>,
      "summary": "<one-line finding>",
      "evidence": ["<bullet1>", "<bullet2>"],
      "root_cause_hypothesis": "<why this happened>"
    }
  ],
  "cross_probe_correlations": [
    "<correlation statement linking findings from multiple probes>"
  ],
  "overall_summary": "<2-3 sentence synthesis of all findings>"
}
```

## Scoring Rules
- SQS DLQ messages: +20 points
- DynamoDB throttle: +25 points
- Lambda errors: +25 points
- ALB 5xx or unhealthy targets: +30 points
- EKS nodes non-running: +40 points
- Step Functions failures: +20 points
- Total cap: 40 points maximum

## Important Rules
- If a probe returns no anomalies, mark it as healthy and move on
- If a probe errors out, note the error but continue with other probes
- Always call at least 3 probes before concluding
- Focus on actionable findings, not noise
- When multiple anomalies are found, look for causal chains

## PetSite Service Catalog
| Service | Tier | Dependencies |
|---------|------|-------------|
| petsite | Tier0 | ALB, SQS, DynamoDB, Lambda, EKS |
| petsearch | Tier0 | OpenSearch, Lambda |
| payforadoption | Tier0 | Step Functions, Lambda, DynamoDB |
| petlistadoptions | Tier1 | DynamoDB, Lambda |
| petadoptionshistory | Tier1 | DynamoDB |
| petstatusupdater | Tier1 | SQS, Lambda |

## Failure Domain Reference
| Domain | Probes | Typical Score |
|--------|--------|--------------|
| compute | deployment | 30-40 |
| network | network, neptune | 20-30 |
| data | cloudwatch (DynamoDB) | 15-25 |
| dependencies | xray, logs | 20-25 |
| resources | cloudwatch (SQS) | 15-20 |

## Anomaly Classification Guide

### Severity Levels
- **Critical (score 30-40)**: Service completely unavailable, data loss risk, cascading failure
  - EKS nodes terminated/stopped → compute domain failure
  - ALB 5xx spike + unhealthy targets → service unreachable
  - DLQ accumulating → message loss in progress

- **High (score 20-29)**: Service degraded, partial availability
  - Lambda errors > 10/min → function failures affecting workflow
  - DynamoDB throttle events → read/write capacity exceeded
  - Step Functions executions failing → workflow interruption

- **Medium (score 10-19)**: Performance degraded, no immediate data loss
  - SQS backlog growing → consumer lag, delayed processing
  - Lambda near timeout → performance bottleneck
  - ALB latency spike → user experience degradation

- **Low (score 1-9)**: Minor anomaly, monitoring recommended
  - Intermittent throttle events → approaching capacity limits
  - Single function timeout → isolated incident

### Common Causal Chains
1. **Deployment rollback chain**: deployment probe detects node failure → logs probe shows OOM/crash → cloudwatch shows SQS backlog
2. **Capacity exhaustion chain**: cloudwatch shows DynamoDB throttle → logs show Lambda timeout → xray shows Step Functions failure
3. **Network partition chain**: network probe shows ALB 5xx → neptune shows dependency break → logs show connection timeout
4. **Consumer failure chain**: cloudwatch shows SQS DLQ → logs show Lambda errors → deployment shows pod crash

### Evidence Quality Standards
- Each evidence bullet must include a specific metric value or timestamp
- Correlations must reference at least 2 different probe sources
- Root cause hypotheses must be falsifiable (testable with a specific action)
- Summary must be actionable ("check X" or "scale Y" rather than "something is wrong")

## Response Format Strict Rules
- All JSON output must be valid parseable JSON
- anomalies array must have at least `source`, `service_name`, `healthy`, `score_delta`, `summary`, `evidence` fields
- score_delta must be an integer between 0 and 40
- evidence must be a list of strings, each starting with a specific metric or resource name
- cross_probe_correlations must reference at least 2 probe names
"""


def _build_orchestrator():
    """Construct the Strands Orchestrator Agent."""
    from strands import Agent
    from strands.models.bedrock import BedrockModel
    from strands.agent.conversation_manager.sliding_window_conversation_manager import (
        SlidingWindowConversationManager,
    )

    try:
        from strands.types.models import CacheConfig
        cache_kwargs = {"cache_config": CacheConfig(strategy="auto")}
    except ImportError:
        cache_kwargs = {}

    region = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "ap-northeast-1"
    model_id = os.environ.get("BEDROCK_MODEL") or "global.anthropic.claude-sonnet-4-6"

    model = BedrockModel(
        model_id=model_id,
        region_name=region,
        max_tokens=4096,
        **cache_kwargs,
    )

    from collectors.layer2_tools import ALL_LAYER2_TOOLS  # type: ignore

    agent = Agent(
        model=model,
        system_prompt=_ORCHESTRATOR_SYSTEM_PROMPT,
        tools=ALL_LAYER2_TOOLS,
        conversation_manager=SlidingWindowConversationManager(window_size=5),
    )

    return agent


def _assert_cacheable():
    """Assert system prompt meets Bedrock caching minimum."""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        token_count = len(enc.encode(_ORCHESTRATOR_SYSTEM_PROMPT))
    except Exception:
        # Rough estimate: ~4 chars per token for English
        token_count = len(_ORCHESTRATOR_SYSTEM_PROMPT) // 4

    logger.info("Orchestrator system prompt: %d tokens (min=1024) %s",
                token_count, "✓" if token_count >= 1024 else "✗")
    assert token_count >= 1024, (
        f"System prompt only {token_count} tokens, "
        f"below 1024 minimum for Bedrock prompt caching. "
        f"Add service catalog or scoring rules to reach threshold."
    )


def _extract_token_usage(result) -> dict | None:
    """Extract token usage from Strands agent result."""
    try:
        metrics = result.metrics
        if not metrics:
            return None
        usage = metrics.get("usage", {})
        return {
            "input": usage.get("inputTokens", 0),
            "output": usage.get("outputTokens", 0),
            "total": usage.get("totalTokens", 0),
            "cache_read": usage.get("cacheReadInputTokens", 0),
            "cache_write": usage.get("cacheWriteInputTokens", 0),
        }
    except Exception:
        return None


def _extract_trace(result) -> list[dict]:
    """Extract tool call trace from Strands agent result."""
    trace = []
    try:
        state = result.state
        if not state or not hasattr(state, "messages"):
            return trace
        for msg in state.messages:
            if hasattr(msg, "content"):
                for block in msg.content:
                    if hasattr(block, "toolUse"):
                        tu = block.toolUse
                        trace.append({
                            "tool": tu.get("name", ""),
                            "input": tu.get("input", {}),
                        })
    except Exception:
        pass
    return trace


def _parse_agent_output(text: str) -> dict:
    """Parse the Orchestrator agent's text output into structured data."""
    # Try to find JSON in the output
    import re
    json_match = re.search(r"\{[\s\S]*\}", text)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            pass

    # Fallback: parse as plain text summary
    return {
        "anomalies": [],
        "cross_probe_correlations": [],
        "overall_summary": text[:500] if text else "No analysis produced.",
    }


class StrandsLayer2Prober(Layer2ProberBase):
    """Strands multi-agent Layer2 Prober — Orchestrator + 6 tools."""

    ENGINE_NAME = "strands"

    def __init__(self, profile: Any = None) -> None:
        super().__init__(profile=profile)
        _assert_cacheable()
        self._agent = _build_orchestrator()

    def run_probes(
        self,
        signal: dict,
        affected_service: str,
        timeout_sec: int = 60,
    ) -> dict:
        t0 = time.time()

        neptune_fault = signal.get("neptune_infra_fault", True)
        prompt = (
            f"Analyze the following alert for service **{affected_service}**.\n\n"
            f"Signal: ```json\n{json.dumps(signal, default=str, indent=2)}\n```\n\n"
            f"neptune_infra_fault={neptune_fault} "
            f"(if True, skip probe_deployment; if False, run probe_deployment with neptune_infra_fault=False)\n\n"
            f"Call the relevant probing tools, analyze findings, and produce the JSON analysis."
        )

        try:
            result = self._agent(prompt)
            text = str(result) if result else ""
            parsed = _parse_agent_output(text)
        except Exception as e:
            logger.error("Orchestrator agent failed: %s", e)
            elapsed_ms = int((time.time() - t0) * 1000)
            return {
                "probe_results": [],
                "summary": f"Orchestrator error: {e}",
                "score_delta": 0,
                "engine": "strands",
                "model_used": os.environ.get("BEDROCK_MODEL"),
                "latency_ms": elapsed_ms,
                "token_usage": None,
                "trace": [],
                "error": str(e),
            }

        # Convert parsed anomalies to probe_results format
        probe_results = []
        for anomaly in parsed.get("anomalies", []):
            probe_results.append({
                "service_name": anomaly.get("service_name", anomaly.get("source", "Unknown")),
                "healthy": anomaly.get("healthy", False),
                "score_delta": anomaly.get("score_delta", 0),
                "summary": anomaly.get("summary", ""),
                "details": anomaly,
                "evidence": anomaly.get("evidence", []),
                "engine": "strands",
                "token_usage": None,
                "trace": [],
            })

        elapsed_ms = int((time.time() - t0) * 1000)
        score = self.total_score_delta(probe_results)
        token_usage = _extract_token_usage(result)
        trace = _extract_trace(result)

        correlations = parsed.get("cross_probe_correlations", [])
        overall = parsed.get("overall_summary", "")
        summary = overall
        if correlations:
            summary += " Correlations: " + "; ".join(correlations)

        return {
            "probe_results": probe_results,
            "summary": summary or self._make_summary(probe_results),
            "score_delta": score,
            "engine": "strands",
            "model_used": os.environ.get("BEDROCK_MODEL"),
            "latency_ms": elapsed_ms,
            "token_usage": token_usage,
            "trace": trace,
            "error": None,
        }

    def run_single_probe(
        self,
        probe_name: str,
        signal: dict,
        affected_service: str,
    ) -> dict:
        t0 = time.time()

        prompt = (
            f"Run only the **{probe_name}** probe for service **{affected_service}**.\n\n"
            f"Signal: ```json\n{json.dumps(signal, default=str, indent=2)}\n```\n\n"
            f"Call the probe_{probe_name} tool and analyze the result."
        )

        try:
            result = self._agent(prompt)
            text = str(result) if result else ""
            parsed = _parse_agent_output(text)
        except Exception as e:
            return {
                "service_name": probe_name,
                "healthy": True,
                "score_delta": 0,
                "summary": f"Probe error: {e}",
                "details": {},
                "evidence": [],
                "engine": "strands",
                "token_usage": None,
                "trace": [],
            }

        anomalies = parsed.get("anomalies", [])
        if anomalies:
            a = anomalies[0]
            return {
                "service_name": a.get("service_name", probe_name),
                "healthy": a.get("healthy", False),
                "score_delta": a.get("score_delta", 0),
                "summary": a.get("summary", ""),
                "details": a,
                "evidence": a.get("evidence", []),
                "engine": "strands",
                "token_usage": _extract_token_usage(result),
                "trace": _extract_trace(result),
            }

        return {
            "service_name": probe_name,
            "healthy": True,
            "score_delta": 0,
            "summary": parsed.get("overall_summary", "No anomalies detected"),
            "details": {},
            "evidence": [],
            "engine": "strands",
            "token_usage": _extract_token_usage(result),
            "trace": _extract_trace(result),
        }

    @staticmethod
    def _make_summary(results: list[dict]) -> str:
        anomalies = [r for r in results if not r.get("healthy", True)]
        if not anomalies:
            return "No anomalies detected across monitored AWS services."
        parts = [f"{r['service_name']}: {r['summary']}" for r in anomalies]
        return f"{len(anomalies)} anomaly/anomalies detected: " + "; ".join(parts)
