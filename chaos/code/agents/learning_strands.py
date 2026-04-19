"""
learning_strands.py — Strands Agents 实现的 LearningAgent (Phase 3 Module 2)。

流程（ReAct）：
  1. Agent 收到 coverage 分析数据
  2. Agent 可选调用 tools 补充上下文（实验历史、图谱节点、拓扑）
  3. Agent 生成改进建议（JSON 数组）
  4. 引擎解析为标准 dict 返回

硬约束（TASK § 7）：
  - 业务行为等价：推荐内容与 Direct 在行为约束上对齐
  - 不启用人工 retry（靠 ReAct 原生多轮）
  - BedrockModel 使用 CacheConfig(strategy="auto")，不用 deprecated cache_prompt
  - assert_cacheable 强制（system prompt ≥ 1024 tokens）
  - Global inference profile（global.*），显式 ap-northeast-1
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

_CHAOS_CODE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_RCA = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "rca"))
for p in (_CHAOS_CODE, _RCA):
    if p not in sys.path:
        sys.path.insert(0, p)

from engines.base import LearningBase  # type: ignore
from engines.strands_common import (  # type: ignore
    DEFAULT_MODEL,
    DEFAULT_REGION,
    ensure_telemetry,
)
from .models import (
    LearningReport, ServiceStats, FailurePattern,
    CoverageGap, Trend, Recommendation, GraphUpdate,
)
from . import learning_tools as lt
from .learning_direct import (
    _ddb_str, _ddb_num, ALL_FAULT_DOMAINS, FAULT_TYPE_DOMAIN,
    DirectBedrockLearning,
)
from runner.neptune_helpers import gremlin_query  # type: ignore

logger = logging.getLogger(__name__)


_LEARNING_SYSTEM_PROMPT = """
## Chaos Engineering Learning Agent

You are a chaos engineering learning and improvement advisor.
Your role: analyze experiment results and generate actionable improvement recommendations.

### Coverage Dimensions
The 5 failure domains to evaluate coverage:
1. **compute** — Pod/container/node failures (pod_kill, pod_failure, fis-eks-node-terminate)
   - Validates: auto-healing, pod disruption budgets, node group scaling
   - Key metrics: pod restart count, node replacement time, replica availability during failure
   - Common weaknesses: single-replica deployments, missing PDB, slow HPA response

2. **data** — Database/storage failures (fis-aurora-failover, fis-aurora-reboot)
   - Validates: read replica promotion, connection pool recovery, data consistency
   - Key metrics: failover time, connection error rate during failover, data loss (should be 0)
   - Common weaknesses: hardcoded writer endpoints, missing read replica, no connection retry

3. **network** — Network partitions, delays, loss (network_delay, network_loss, network_partition, dns_chaos)
   - Validates: circuit breakers, timeout configs, retry policies, DNS failover
   - Key metrics: p99 latency increase, error rate during partition, recovery time after partition heals
   - Common weaknesses: missing circuit breaker, aggressive timeouts, no retry backoff

4. **dependencies** — Upstream/downstream service failures (http_chaos, fis-lambda-delay, fis-lambda-error)
   - Validates: graceful degradation, fallback mechanisms, bulkhead isolation
   - Key metrics: cascade failure depth, degraded response quality, user-facing error rate
   - Common weaknesses: tight coupling, missing fallback, no bulkhead

5. **resources** — Resource exhaustion (pod_cpu_stress, pod_memory_stress, fis-ebs-io-latency)
   - Validates: resource limits, OOM handling, throttling behavior, EBS IOPS limits
   - Key metrics: OOM kill count, CPU throttle percentage, I/O wait time
   - Common weaknesses: missing resource limits, no vertical pod autoscaler, undersized EBS volumes

### Assessment Framework
For each service, evaluate:
- **Pass Rate**: PASSED / total experiments. Target ≥ 90% for Tier0, ≥ 80% for others
- **Recovery Time**: Average seconds to recover. Target ≤ 60s for Tier0, ≤ 120s for Tier1, ≤ 300s for Tier2
- **Domain Coverage**: How many of the 5 failure domains have been tested. Target: all 5 for Tier0
- **Repeated Failures**: Same service + fault_type failing ≥ 2 times → weakness pattern requiring immediate attention
- **Trends**: Compare first-half vs second-half recovery times:
  - improving: second half < 80% of first half
  - degrading: second half > 120% of first half
  - stable: within 80-120% band

### Recommendation Categories
Each recommendation must have one category:
- **coverage**: Fill untested failure domains. Priority for Tier0 services with < 3 domains covered
- **resilience**: Fix repeated failure patterns, improve recovery times exceeding targets
- **process**: Improve testing cadence (recommend monthly for Tier0, quarterly for others), automation, monitoring integration

### Recommendation Quality Criteria
1. Each recommendation must target specific services (not generic advice)
2. Include concrete next steps (e.g., "add network_delay experiment for petsite" not "improve network resilience")
3. Reference actual data from the analysis (pass rates, gap counts, failure patterns)
4. Priority 1 = most urgent (repeated failures on Tier0), Priority 5 = nice-to-have

### Output Format
Produce a JSON array of 3-5 recommendations:
```json
[
  {
    "priority": 1,
    "category": "coverage|resilience|process",
    "title": "标题（中文）",
    "description": "详细描述（中文），包含具体服务名和数据引用",
    "target_services": ["service1"]
  }
]
```

### Rules
1. Use tools to gather context if the provided analysis is insufficient.
2. Keep recommendations actionable and specific.
3. Priority 1 = most urgent.
4. Always address coverage gaps first (category=coverage) for services with < 3 domains.
5. Write in Chinese (中文).
6. Do NOT recommend deleting or shutting down services.
7. If the analysis shows 0 experiments, return an empty array [].
8. Maximum 5 recommendations per request.

### Fault Type Reference Table
| Fault Type | Domain | Backend | Typical Recovery |
|---|---|---|---|
| pod_kill | compute | chaosmesh | 10-30s (auto-restart) |
| pod_failure | compute | chaosmesh | 10-60s (depends on PDB) |
| network_delay | network | chaosmesh | immediate after removal |
| network_loss | network | chaosmesh | immediate after removal |
| network_partition | network | chaosmesh | 5-30s (circuit breaker) |
| pod_cpu_stress | resources | chaosmesh | immediate after removal |
| pod_memory_stress | resources | chaosmesh | 10-60s (OOM restart) |
| dns_chaos | network | chaosmesh | 30-120s (DNS cache TTL) |
| http_chaos | dependencies | chaosmesh | immediate after removal |
| fis-aurora-failover | data | fis | 20-60s (promotion) |
| fis-aurora-reboot | data | fis | 60-180s (full restart) |
| fis-lambda-delay | dependencies | fis | immediate after removal |
| fis-lambda-error | dependencies | fis | immediate after removal |
| fis-ebs-io-latency | resources | fis | immediate after removal |
| fis-eks-node-terminate | compute | fis | 60-300s (node replacement) |
"""


def _assert_cacheable(system_prompt: str, min_tokens: int = 1024):
    """验证 system prompt 达到缓存下限（按 tokens 算，不按 chars）。"""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        token_count = len(enc.encode(system_prompt))
    except ImportError:
        # fallback: rough estimate 1 token ≈ 4 chars for English, 2 for Chinese
        token_count = len(system_prompt) // 3
        logger.warning("tiktoken not available, using rough estimate: %d tokens", token_count)

    assert token_count >= min_tokens, (
        f"System prompt only {token_count} tokens, "
        f"below {min_tokens} minimum for Bedrock prompt caching. "
        f"Add coverage dimension definitions to reach threshold."
    )
    logger.info("System prompt: %d tokens (min=%d) ✓", token_count, min_tokens)


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _extract_json_array(text: str) -> list:
    for m in _JSON_BLOCK_RE.finditer(text):
        candidate = m.group(1).strip()
        try:
            data = json.loads(candidate)
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            continue
    lo = text.find("[")
    hi = text.rfind("]")
    if lo >= 0 and hi > lo:
        try:
            data = json.loads(text[lo: hi + 1])
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
    return []


class StrandsLearningAgent(LearningBase):
    """Strands Agent-based Learning engine."""

    ENGINE_NAME = "strands"

    def __init__(self, hypothesis_engine: Any = None, profile: Any = None) -> None:
        super().__init__(hypothesis_engine=hypothesis_engine, profile=profile)
        ensure_telemetry()

        # Build system prompt with profile-specific coverage schema if available
        extra_schema = ""
        try:
            from profiles.profile_loader import EnvironmentProfile  # type: ignore
            if self.profile is None:
                self.profile = EnvironmentProfile()
            if self.profile:
                extra_schema = getattr(self.profile, "coverage_schema_text", "") or ""
        except Exception:
            pass

        self.system_prompt = (
            "## Environment Coverage Schema\n"
            f"{extra_schema}\n"
            f"{_LEARNING_SYSTEM_PROMPT}"
        )

        _assert_cacheable(self.system_prompt, min_tokens=1024)

        # Reuse Direct engine for pure-Python methods (analyze, graph, report)
        self._direct = DirectBedrockLearning(
            hypothesis_engine=hypothesis_engine, profile=profile,
        )

    def _build_agent(self):
        from strands import Agent  # type: ignore
        from strands.models import BedrockModel, CacheConfig  # type: ignore

        model = BedrockModel(
            model_id=DEFAULT_MODEL,
            region_name=DEFAULT_REGION,
            cache_config=CacheConfig(strategy="auto"),
        )
        return Agent(
            model=model,
            tools=[
                lt.query_experiment_history,
                lt.query_coverage_snapshot,
                lt.query_graph_learning_nodes,
                lt.invoke_hypothesis_engine,
            ],
            system_prompt=self.system_prompt,
        )

    def _extract_text(self, resp: Any) -> str:
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
            return {"input": it, "output": ot, "total": tt,
                    "cache_read": cr, "cache_write": cw}
        except Exception:
            return None

    # ── analyze (delegate to Direct — pure Python, no LLM) ──────────

    def analyze(self, experiment_results: list[dict]) -> dict:
        result = self._direct.analyze(experiment_results)
        result["engine"] = self.ENGINE_NAME
        return result

    # ── generate_recommendations (Strands ReAct) ────────────────────

    def generate_recommendations(self, analysis: dict) -> dict:
        t0 = time.time()
        lt.reset_trace()

        # Build summary for the agent
        report = analysis.get("report")
        summary_data = {
            "coverage": analysis.get("coverage", {}),
            "gaps": analysis.get("gaps", []),
            "repeated_failures": analysis.get("repeated_failures", []),
            "improvement_trends": analysis.get("improvement_trends", []),
        }
        if report:
            summary_data["total_experiments"] = getattr(report, "total_experiments", 0)
            summary_data["pass_rate"] = getattr(report, "pass_rate", 0)
            summary_data["avg_recovery_seconds"] = getattr(report, "avg_recovery_seconds", 0)

        prompt = (
            "基于以下混沌工程实验分析结果，生成 3-5 条改进建议。\n\n"
            f"## 分析数据\n```json\n{json.dumps(summary_data, ensure_ascii=False, indent=2, default=str)}\n```\n\n"
            "如需更多上下文，可调用 tools 查询。\n"
            "最后以 ```json ... ``` 包裹 JSON 数组作为最终答案。"
        )

        try:
            agent = self._build_agent()
            resp = agent(prompt)
        except Exception as e:
            logger.warning("Strands learning agent failed: %s", e)
            # Fallback to direct
            return self._direct.generate_recommendations(analysis)

        text = self._extract_text(resp)
        raw_list = _extract_json_array(text)

        recommendations = [
            {
                "priority": r.get("priority", 5),
                "category": r.get("category", "resilience"),
                "title": r.get("title", ""),
                "description": r.get("description", ""),
                "target_services": r.get("target_services", []),
            }
            for r in raw_list
        ]

        token_usage = self._extract_token_usage(resp)
        trace = lt.get_trace()

        return {
            "recommendations": recommendations,
            "engine": self.ENGINE_NAME,
            "model_used": DEFAULT_MODEL,
            "latency_ms": int((time.time() - t0) * 1000),
            "token_usage": token_usage,
            "trace": trace,
            "error": None,
        }

    # ── iterate_hypotheses (delegate to Direct) ─────────────────────

    def iterate_hypotheses(self, coverage: dict, existing_hypotheses: list) -> dict:
        result = self._direct.iterate_hypotheses(coverage, existing_hypotheses)
        result["engine"] = self.ENGINE_NAME
        return result

    # ── update_graph (delegate to Direct — pure Gremlin writes) ─────

    def update_graph(self, learning_data: dict) -> dict:
        result = self._direct.update_graph(learning_data)
        result["engine"] = self.ENGINE_NAME
        return result

    # ── generate_report (delegate to Direct — pure string formatting) ─

    def generate_report(self, analysis: dict) -> dict:
        result = self._direct.generate_report(analysis)
        result["engine"] = self.ENGINE_NAME
        return result
