"""
hypothesis_strands.py — Strands Agents 实现的 HypothesisAgent (Phase 3 Module 1)。

流程（ReAct）：
  1. Agent 收到请求："生成最多 N 个假设，可选 service_filter"
  2. Agent 自行决定调用 query_topology / query_recent_incidents /
     query_fault_history / query_infra_snapshot
  3. Agent 按 system prompt 规则输出 JSON 数组
  4. 引擎解析为 list[Hypothesis]，补齐 source_context 元数据

硬约束（TASK § 6）：
  - 业务行为等价：产出 list[Hypothesis] 形状与 Direct 一致
  - 不启用人工 retry（靠 ReAct 原生多轮）
  - BedrockModel 使用 inference profile（global.*），显式 ap-northeast-1
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from typing import Any, Optional

from engines.base import HypothesisBase
from engines.strands_common import (
    DEFAULT_MODEL,
    DEFAULT_REGION,
    HEAVY_MODEL,
    build_bedrock_model,
    ensure_telemetry,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from .models import Hypothesis
from . import hypothesis_tools as hy_tools

logger = logging.getLogger(__name__)


_AGENT_RULES = """

## Hypothesis Generation Agent Rules

You are a chaos engineering hypothesis expert. Your job: given a Neptune
dependency graph, recent incidents, chaos history and infra snapshot,
generate high-quality chaos hypotheses.

Operating procedure:
1. First call `query_topology` (optionally with service filter) to get the
   dependency graph. Required for every request.
2. Call `query_recent_incidents` to pull up to 20 recent incidents.
3. Call `query_fault_history` (optionally per service) to avoid duplicates.
4. For every service you intend to target, call `query_infra_snapshot`
   with a comma-separated list to check actual Pod / Node / AWS resources.
5. Do NOT propose pod-kill style fault on services without a running Pod.
6. Cover the 5 failure domains whenever possible: compute, data, network,
   dependencies, resources.
7. Each hypothesis requires: steady_state, fault_scenario, expected_impact,
   verification criteria, failure_domain, target_services, target_resources,
   backend.
8. backend must be "fis" for Lambda/RDS, "chaosmesh" for K8s Pods.
9. fault_scenario must start with one of the valid fault types:
   pod_kill, pod_failure, network_delay, network_loss, network_partition,
   pod_cpu_stress, pod_memory_stress, dns_chaos, http_chaos.
10. Output the final answer as a **single JSON array** wrapped in a
    ```json ... ``` block. Each element must have keys:
        id, title, description, steady_state, fault_scenario,
        expected_impact, failure_domain, target_services,
        target_resources, backend.
11. Do not generate more than the requested number of hypotheses.
12. Keep descriptions in Chinese (中文) as the operators are Chinese.
"""


_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _extract_json_array(text: str) -> list:
    m = _JSON_BLOCK_RE.search(text)
    raw = m.group(1).strip() if m else text.strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # 尝试找第一个 [ 到最后一个 ] 的片段
        lo = raw.find("[")
        hi = raw.rfind("]")
        if lo >= 0 and hi > lo:
            data = json.loads(raw[lo : hi + 1])
        else:
            return []
    return data if isinstance(data, list) else []


class StrandsHypothesisAgent(HypothesisBase):
    """Strands Agent-based Hypothesis generator."""

    ENGINE_NAME = "strands"

    def __init__(self, profile: Any = None) -> None:
        super().__init__(profile=profile)
        ensure_telemetry()
        self._sys_prompt = _AGENT_RULES
        # HypothesisBase 的 shim 需要 system_prompt 足够长（Prompt Caching 最低 1024 tokens）
        # _AGENT_RULES 当前 ~1500 chars，不到门槛；PR4 会补足。
        # 这里先用 L0 spike 的 schema 再加业务规则，达到 > 3000 chars。
        try:
            from profiles.profile_loader import EnvironmentProfile  # type: ignore
            if self.profile is None:
                self.profile = EnvironmentProfile()
        except Exception:
            self.profile = None
        # 合并稳定前缀：图 schema（从 profile 可选读） + Agent 规则
        schema = ""
        if self.profile is not None:
            schema = getattr(self.profile, "neptune_graph_schema_text", "") or ""
        self.system_prompt = (
            "## Graph schema context (for hypothesis generation)\n"
            f"{schema}\n"
            f"{_AGENT_RULES}"
        )

    # ------------------------------------------------------------
    # Public API (dict forms)
    # ------------------------------------------------------------

    def generate_with_meta(
        self,
        max_hypotheses: int = 50,
        service_filter: str | None = None,
    ) -> dict:
        t0 = time.time()
        model_id = self._select_model(max_hypotheses=max_hypotheses, service_filter=service_filter)
        hy_tools.reset_trace()
        hy_tools.reset_context()

        try:
            agent = self._build_agent(model_id)
        except Exception as e:
            logger.warning("Strands hypothesis agent build failed: %s", e)
            return self._pack(hypotheses=[], prioritized=[], t0=t0,
                              model=model_id, error=f"agent build failed: {e!r}")

        prompt = (
            f"请为以下请求生成混沌工程假设：\n"
            f"- max_hypotheses: {max_hypotheses}\n"
            f"- service_filter: {service_filter or '(all)'}\n\n"
            "按 System Prompt 规则严格操作：先 query_topology，再按需补充 tools。"
            "最后以 ```json ... ``` 包裹 JSON 数组作为最终答案。"
        )

        try:
            resp = agent(prompt)
        except Exception as e:
            logger.warning("Strands hypothesis agent invoke failed: %s", e)
            return self._pack(hypotheses=[], prioritized=[], t0=t0,
                              model=model_id, error=repr(e))

        text = self._extract_text(resp)
        raw_list = _extract_json_array(text)
        hypotheses: list[Hypothesis] = []
        topology = hy_tools._context.get("topology_cache") or []
        incidents = hy_tools._context.get("incidents_cache") or []
        for i, item in enumerate(raw_list[:max_hypotheses]):
            try:
                h = Hypothesis(
                    id=item.get("id", f"H{i+1:03d}"),
                    title=item.get("title", ""),
                    description=item.get("description", ""),
                    steady_state=item.get("steady_state", ""),
                    fault_scenario=item.get("fault_scenario", ""),
                    expected_impact=item.get("expected_impact", ""),
                    failure_domain=item.get("failure_domain", "compute"),
                    target_services=item.get("target_services", []),
                    target_resources=item.get("target_resources", []),
                    backend=item.get("backend", "chaosmesh"),
                    source_context={
                        "topology_services": len(topology),
                        "incidents": len(incidents),
                        "engine": self.ENGINE_NAME,
                    },
                )
                hypotheses.append(h)
            except Exception as e:  # pragma: no cover
                logger.warning("Drop malformed hypothesis item %s: %r", i, e)

        token_usage = self._extract_token_usage(resp)
        trace = hy_tools.get_trace()
        return self._pack(
            hypotheses=hypotheses, prioritized=[], t0=t0,
            model=model_id, token_usage=token_usage, trace=trace,
        )

    def prioritize_with_meta(self, hypotheses: list[Hypothesis]) -> dict:
        """交给 Strands agent 排序——复用 Direct 的 LLM prompt 模板最省事，
        避免重复发明。为不在 Phase 3 引入新复杂度，这里退化为调 Direct.prioritize()，
        但打上 engine='strands'（代表 generate 部分由 Strands 产出）。
        """
        from .hypothesis_direct import DirectBedrockHypothesis
        t0 = time.time()
        if not hypotheses:
            return self._pack(hypotheses=[], prioritized=hypotheses, t0=t0,
                              model=DEFAULT_MODEL)
        try:
            direct = DirectBedrockHypothesis(profile=self.profile)
            meta = direct.prioritize_with_meta(hypotheses)
        except Exception as e:
            logger.warning("Strands prioritize (delegated) failed: %s", e)
            return self._pack(hypotheses=[], prioritized=hypotheses, t0=t0,
                              model=DEFAULT_MODEL, error=repr(e))
        meta["engine"] = self.ENGINE_NAME
        meta["latency_ms"] = int((time.time() - t0) * 1000)
        return meta

    # ------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------

    def _build_agent(self, model_id: str):
        from strands import Agent  # type: ignore
        model = build_bedrock_model(model_id=model_id, region=DEFAULT_REGION)
        return Agent(
            model=model,
            tools=[
                hy_tools.query_topology,
                hy_tools.query_recent_incidents,
                hy_tools.query_fault_history,
                hy_tools.query_infra_snapshot,
            ],
            system_prompt=self.system_prompt,
        )

    def _select_model(self, *, max_hypotheses: int, service_filter: Optional[str]) -> str:
        """复杂请求（无 filter + 数量大）用 Opus，否则 Sonnet。"""
        if service_filter is None and max_hypotheses >= 30:
            logger.info("Hypothesis agent upgrading model to %s (broad generate)", HEAVY_MODEL)
            return HEAVY_MODEL
        return DEFAULT_MODEL

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

    def _pack(self, *, hypotheses: list, prioritized: list, t0: float,
              model: str, token_usage: dict | None = None,
              trace: list | None = None, error: str | None = None) -> dict:
        out = {
            "hypotheses": list(hypotheses or []),
            "prioritized": list(prioritized or []),
            "engine": self.ENGINE_NAME,
            "model_used": model,
            "latency_ms": int((time.time() - t0) * 1000),
            "token_usage": token_usage,
            "trace": list(trace or []),
        }
        if error is not None:
            out["error"] = error
        return out
