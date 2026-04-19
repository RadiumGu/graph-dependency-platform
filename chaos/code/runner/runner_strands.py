"""
runner_strands.py — Strands Runner: Agent-driven 5-phase experiment execution.

Agent 生命周期 = 一次实验（run() 内新建，run() 结束销毁）。
7 个 @tool 封装 K8s/FIS 操作，Agent 驱动 phase 流程。
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Any

from runner.base import RunnerBase  # type: ignore

logger = logging.getLogger(__name__)

# ── 稳定段 system prompt（缓存对象）─────────────────────────────────────────

STABLE_CHAOS_FRAMEWORK = """\
You are a Chaos Engineering Experiment Runner. You execute chaos experiments following
a strict 7-phase protocol, using tools to interact with Kubernetes and AWS FIS.

## 7-Phase Protocol (MUST follow in order — do NOT skip or reorder phases)

### Phase 0: Pre-flight Check
1. Call `policy_guard_check` — if denied, STOP immediately (do NOT proceed to Phase 1)
2. Validate target namespace is not protected
3. Log preflight results

### Phase 1: Steady State Before
1. Call `check_steady_state` to collect baseline metrics (default 3 samples)
2. Verify all steady-state conditions are satisfied
3. If any condition fails → STOP (service not healthy enough for experiment)

### Phase 2: Fault Injection
1. Call `inject_fault` with experiment parameters
2. Record injection time and experiment name
3. In dry_run mode, the tool returns mock results without actual mutations

### Phase 3: Observation
1. Call `observe_metrics` at regular intervals during the fault duration
2. After each observation, call `check_stop_conditions` with current metrics
3. If stop condition breached → immediately call `recover_fault` → ABORT
4. Continue until fault duration expires

### Phase 4: Fault Recovery
1. Call `recover_fault` to clean up injected faults
2. Wait for pods to recover (call `check_steady_state` to verify)
3. Record recovery time

### Phase 5: Steady State After
1. Call `check_steady_state` to verify service returned to baseline
2. Compare before/after metrics
3. Call `collect_logs` for post-experiment analysis
4. Determine final status: PASSED (recovered) or FAILED (not recovered)

## Safety Invariants (NEVER violate)
- NEVER inject fault if PolicyGuard denied
- NEVER inject fault in protected namespaces (default, kube-system, kube-public, kube-node-lease)
- ALWAYS clean up injected faults on abort/error — call `recover_fault` before stopping
- In dry_run mode, report expected outcomes without actual mutations
- If any tool returns an error, you may retry ONCE; if it fails again → ABORT with cleanup

## Stop Condition Rules
- Evaluate after EVERY `observe_metrics` call during Phase 3
- If ANY stop condition is breached → ABORT immediately:
  1. Call `recover_fault` to remove injected fault
  2. Call `collect_logs` for diagnosis
  3. Report status = ABORTED with breach details

## Tool Error Handling
- Tool returns `{"error": "..."}` → retry once
- Second failure → ABORT with cleanup
- NEVER ignore tool errors silently

## Decision Logging
For EVERY phase transition, state your reasoning:
- What you observed
- What decision you're making
- Why (reference specific metrics or conditions)

## Fault Type Reference Table
| Fault Type | Category | Backend | Typical Duration | Risk Level |
|-----------|----------|---------|-----------------|------------|
| pod-delete | compute | chaosmesh | 30-120s | Medium |
| pod-kill | compute | chaosmesh | 30-120s | Medium |
| pod-cpu-stress | resources | chaosmesh | 60-300s | Medium |
| pod-memory-stress | resources | chaosmesh | 60-300s | Medium |
| network-latency | network | chaosmesh | 60-300s | Low-Medium |
| network-partition | network | chaosmesh | 30-180s | High |
| network-loss | network | chaosmesh | 60-300s | Medium |
| dns-chaos | network | chaosmesh | 60-120s | Medium |
| http-chaos | dependencies | chaosmesh | 60-300s | Medium |
| node-drain | compute | chaosmesh | 120-600s | High |
| fis-aurora-failover | data | fis | 60-300s | High |
| fis-aurora-reboot | data | fis | 60-180s | High |
| fis-lambda-delay | dependencies | fis | 60-300s | Medium |
| fis-lambda-error | dependencies | fis | 60-300s | Medium |
| fis-subnet-disrupt | infrastructure | fis | 60-600s | High |
| fis-ebs-pause | storage | fis | 60-300s | High |

## Blast Radius Safety Limits
| Environment | Allowed Blast Radius |
|------------|---------------------|
| chaos-sandbox | single-pod, service |
| staging | single-pod, service, namespace |
| production | single-pod ONLY |

## Output Format
After completing all phases (or aborting), provide a final summary as JSON:
```json
{
    "status": "PASSED|FAILED|ABORTED|ERROR",
    "phases_completed": ["phase0", "phase1", ...],
    "abort_reason": null or "description",
    "decision_log": [
        {"phase": "phase0", "decision": "proceed", "reasoning": "PolicyGuard allowed"},
        ...
    ],
    "metrics_summary": {
        "steady_before": {"success_rate": 99.5, "p99_ms": 120},
        "steady_after": {"success_rate": 99.2, "p99_ms": 135},
        "min_success_rate_during_fault": 85.0,
        "recovery_seconds": 45
    }
}
```
"""


def _assert_cacheable(prompt: str, min_tokens: int = 1024):
    """验证稳定段达到缓存下限。"""
    try:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        n = len(enc.encode(prompt))
    except Exception:
        n = len(prompt) // 4
    logger.info("Runner stable prompt: %d tokens (min=%d) %s", n, min_tokens, "✓" if n >= min_tokens else "✗")
    if n < min_tokens:
        raise AssertionError(f"Stable prompt only {n} tokens, below {min_tokens} minimum.")


class StrandsRunner(RunnerBase):
    """Strands Agent-driven 5-phase experiment runner.

    Agent 生命周期 = 一次 run() 调用。
    """

    ENGINE_NAME = "strands"

    def __init__(self, dry_run: bool = True, tags: dict | None = None) -> None:
        super().__init__(dry_run=dry_run, tags=tags)
        _assert_cacheable(STABLE_CHAOS_FRAMEWORK)
        self._region = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "ap-northeast-1"
        self._model_id = os.environ.get("BEDROCK_MODEL") or "global.anthropic.claude-sonnet-4-6"

        # Infrastructure clients (shared across runs, stateless)
        from runner.metrics import DeepFlowMetrics
        from runner.chaos_mcp import ChaosMCPClient
        from runner.fis_backend import FISClient
        self._metrics = DeepFlowMetrics()
        self._injector = ChaosMCPClient()
        self._fis = FISClient()

    def _build_model(self):
        from strands.models import BedrockModel, CacheConfig  # type: ignore
        return BedrockModel(
            model_id=self._model_id,
            region_name=self._region,
            max_tokens=4096,
            cache_config=CacheConfig(strategy="auto"),
        )

    def _build_system_prompt(self, experiment) -> str:
        """稳定段 + 可变段拼接。"""
        env = os.environ.get("ENVIRONMENT", "staging")
        now = datetime.now(timezone(timedelta(hours=8))).isoformat()

        variable_part = f"""
## Current Experiment
- Name: {experiment.name}
- Target: {experiment.target_service} in {experiment.target_namespace}
- Fault Type: {experiment.fault.type}
- Duration: {experiment.fault.duration}
- Blast Radius: {getattr(experiment, 'blast_radius', 'service')}
- Backend: {experiment.backend}
- Dry Run: {self.dry_run}

## Context
- Environment: {env}
- Current Time: {now}
- Observe Interval: 10s
- Recovery Timeout: 300s
- Steady Samples: 3
"""
        return STABLE_CHAOS_FRAMEWORK + "\n" + variable_part

    def _build_tools(self, experiment):
        """Build @tool functions bound to this experiment's infra clients."""
        from strands import tool  # type: ignore

        runner_self = self  # closure capture

        @tool
        def policy_guard_check(experiment_dict: str, context_dict: str) -> str:
            """Phase 0: Run PolicyGuard pre-execution check. Returns allow/deny decision.
            Args:
                experiment_dict: JSON string of experiment metadata
                context_dict: JSON string of execution context
            """
            try:
                # 硬约束 §13: tool 内调 PolicyGuard 必须 direct
                os.environ["POLICY_GUARD_ENGINE"] = "direct"
                from policy.factory import make_policy_guard  # type: ignore
                guard = make_policy_guard()
                exp_d = json.loads(experiment_dict)
                ctx_d = json.loads(context_dict)
                result = guard.evaluate(exp_d, ctx_d)
                return json.dumps(result, default=str)
            except Exception as e:
                # fail-closed
                return json.dumps({"decision": "deny", "reasoning": f"PolicyGuard error: {e}",
                                   "matched_rules": [], "confidence": 0.0, "error": str(e)})

        @tool
        def inject_fault(fault_type: str, target_service: str, namespace: str,
                         duration: str, backend: str) -> str:
            """Phase 2: Inject fault via Chaos Mesh or FIS. In dry_run mode returns mock result.
            Args:
                fault_type: Type of fault (e.g. pod-delete, network-latency)
                target_service: Target K8s service name
                namespace: Target K8s namespace
                duration: Fault duration (e.g. "60s", "2m")
                backend: Backend engine (chaosmesh, fis, fis-scenario)
            """
            if runner_self.dry_run:
                return json.dumps({"status": "injected", "dry_run": True,
                                   "experiment_name": "dry-run-placeholder",
                                   "fault_type": fault_type, "duration": duration})
            try:
                if backend in ("fis", "fis-scenario"):
                    fis_result = runner_self._fis.inject(experiment)
                    return json.dumps({"status": "injected", "experiment_id": fis_result["experiment_id"],
                                       "backend": backend})
                else:
                    ft = experiment.fault
                    mcp_result = runner_self._injector.inject(
                        fault_type=ft.type, service=target_service, namespace=namespace,
                        duration=ft.duration, mode=ft.mode, value=ft.value,
                        latency=ft.latency, loss=ft.loss, corrupt=ft.corrupt,
                        container_names=ft.container_names, workers=ft.workers,
                        load=ft.load, size=ft.size, time_offset=ft.time_offset,
                        direction=ft.direction, external_targets=ft.external_targets,
                    )
                    exp_name = runner_self._injector.extract_experiment_name(mcp_result, ft.type)
                    return json.dumps({"status": "injected", "experiment_name": exp_name,
                                       "fault_type": fault_type, "backend": "chaosmesh"})
            except Exception as e:
                return json.dumps({"error": str(e), "fault_type": fault_type})

        @tool
        def check_steady_state(service: str, namespace: str, samples: int = 3) -> str:
            """Phase 1/5: Collect steady-state metrics (success rate, p99 latency).
            Args:
                service: Target service name
                namespace: Target namespace
                samples: Number of metric samples to collect
            """
            try:
                if runner_self.dry_run:
                    return json.dumps({"success_rate": 99.9, "latency_p99_ms": 50.0,
                                       "total_requests": 1000, "dry_run": True})
                snap = runner_self._metrics.collect_steady(
                    service=service, namespace=namespace,
                    window_seconds=60, samples=samples, interval=10,
                )
                return json.dumps({"success_rate": snap.success_rate,
                                   "latency_p99_ms": snap.latency_p99_ms,
                                   "total_requests": snap.total_requests})
            except Exception as e:
                return json.dumps({"error": str(e)})

        @tool
        def observe_metrics(service: str, namespace: str) -> str:
            """Phase 3: Collect current metrics during fault observation period.
            Args:
                service: Target service name
                namespace: Target namespace
            """
            try:
                if runner_self.dry_run:
                    return json.dumps({"success_rate": 85.0, "latency_p99_ms": 500.0,
                                       "total_requests": 800, "dry_run": True})
                snap = runner_self._metrics.collect(
                    service=service, namespace=namespace, window_seconds=60,
                )
                return json.dumps({"success_rate": snap.success_rate,
                                   "latency_p99_ms": snap.latency_p99_ms,
                                   "total_requests": snap.total_requests})
            except Exception as e:
                return json.dumps({"error": str(e)})

        @tool
        def check_stop_conditions(current_success_rate: float, current_p99_ms: float) -> str:
            """Phase 3: Check if any stop condition is breached based on current metrics.
            Args:
                current_success_rate: Current success rate percentage (0-100)
                current_p99_ms: Current p99 latency in milliseconds
            """
            from runner.experiment import MetricsSnapshot
            # Build a fake snapshot for the StopCondition.is_triggered() method
            snap = MetricsSnapshot(
                timestamp=int(time.time()),
                success_rate=current_success_rate,
                latency_p99_ms=current_p99_ms,
                total_requests=0,
            )
            breaches = []
            for cond in experiment.stop_conditions:
                if cond.is_triggered(snap):
                    breaches.append(cond.describe(snap))
            return json.dumps({"breached": len(breaches) > 0, "breaches": breaches})

        @tool
        def recover_fault(experiment_name: str, backend: str) -> str:
            """Phase 4: Remove injected fault (delete ChaosExperiment CR or stop FIS experiment).
            Args:
                experiment_name: Name of the chaos experiment to remove
                backend: Backend engine (chaosmesh, fis, fis-scenario)
            """
            if runner_self.dry_run:
                return json.dumps({"status": "recovered", "dry_run": True})
            try:
                if backend in ("fis", "fis-scenario"):
                    runner_self._fis.stop(experiment_name)
                else:
                    fault_type = experiment.fault.type
                    chaos_type = runner_self._injector.FAULT_TO_DELETE_TYPE.get(fault_type, fault_type)
                    runner_self._injector.delete(experiment_name, chaos_type=chaos_type,
                                                 namespace=experiment.target_namespace)
                return json.dumps({"status": "recovered", "experiment_name": experiment_name})
            except Exception as e:
                return json.dumps({"error": str(e), "experiment_name": experiment_name})

        @tool
        def collect_logs(service: str, namespace: str, since: str = "5m") -> str:
            """Collect pod logs for post-experiment analysis (best-effort, non-fatal).
            Args:
                service: Target service name
                namespace: Target namespace
                since: Time window for log collection (e.g. "5m", "10m")
            """
            if runner_self.dry_run:
                return json.dumps({"status": "collected", "dry_run": True, "log_lines": 0})
            try:
                from runner.log_collector import PodLogCollector
                collector = PodLogCollector(service=service, namespace=namespace, since=since)
                logs = collector.collect_sync()
                return json.dumps({"status": "collected",
                                   "log_lines": len(logs) if logs else 0,
                                   "summary": str(logs)[:500] if logs else "no logs"})
            except Exception as e:
                return json.dumps({"status": "failed", "error": str(e)})

        return [policy_guard_check, inject_fault, check_steady_state,
                observe_metrics, check_stop_conditions, recover_fault, collect_logs]

    def run(self, experiment) -> Any:
        from strands import Agent  # type: ignore
        from runner.runner import ExperimentRunner, PrefightFailure, AbortException
        from runner.result import ExperimentResult

        t0 = time.time()
        self._validate_namespace(experiment.target_namespace)

        result = ExperimentResult(experiment=experiment)
        result.start_time = datetime.now(timezone.utc)

        try:
            agent = Agent(
                model=self._build_model(),
                system_prompt=self._build_system_prompt(experiment),
                tools=self._build_tools(experiment),
            )

            # Build the run prompt
            env = os.environ.get("ENVIRONMENT", "staging")
            now = datetime.now(timezone(timedelta(hours=8))).isoformat()
            exp_dict = {
                "name": experiment.name,
                "fault_type": experiment.fault.type,
                "target_namespace": experiment.target_namespace,
                "target_service": experiment.target_service,
                "duration_sec": getattr(experiment, 'duration', 0),
                "blast_radius": getattr(experiment, 'blast_radius', 'service'),
            }
            ctx_dict = {
                "current_time": now,
                "environment": env,
                "recent_incidents": [],
                "recent_experiments": [],
            }

            run_prompt = (
                f"Execute the chaos experiment following the 7-phase protocol.\n\n"
                f"**Experiment config:**\n```json\n{json.dumps(exp_dict, indent=2)}\n```\n\n"
                f"**Context:**\n```json\n{json.dumps(ctx_dict, indent=2)}\n```\n\n"
                f"Start with Phase 0 (policy_guard_check), then proceed through all phases in order. "
                f"Remember: dry_run={self.dry_run}."
            )

            agent_result = agent(run_prompt)
            text = str(agent_result) if agent_result else ""

            # Extract token usage
            token_usage = self._extract_token_usage(agent_result)
            parsed = self._parse_result(text)

            result.status = parsed.get("status", "COMPLETED")
            if parsed.get("abort_reason"):
                result.abort_reason = parsed["abort_reason"]

        except Exception as e:
            logger.error("StrandsRunner failed: %s", e)
            result.status = "ERROR"
            result.abort_reason = str(e)
            token_usage = None

            # Emergency cleanup
            if hasattr(result, 'chaos_experiment_name') and result.chaos_experiment_name:
                try:
                    ExperimentRunner(dry_run=self.dry_run)._emergency_cleanup(
                        result.chaos_experiment_name,
                        fault_type=experiment.fault.type,
                        backend=experiment.backend,
                    )
                except Exception as ce:
                    logger.error("Emergency cleanup failed: %s", ce)

        elapsed_ms = int((time.time() - t0) * 1000)
        result.end_time = datetime.now(timezone.utc)

        if not hasattr(result, "engine_meta"):
            result.engine_meta = {}
        result.engine_meta.update({
            "engine": "strands",
            "model_used": self._model_id,
            "latency_ms": elapsed_ms,
            "token_usage": token_usage or {
                "input": 0, "output": 0, "total": 0,
                "cache_read": 0, "cache_write": 0,
            },
        })

        return result

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
            if it == 0 and ot == 0:
                return None
            return {"input": it, "output": ot, "total": tt,
                    "cache_read": cr, "cache_write": cw}
        except Exception:
            return None

    @staticmethod
    def _parse_result(text: str) -> dict:
        """Parse Agent's final JSON summary."""
        import re
        json_match = re.search(r'\{[\s\S]*"status"[\s\S]*\}', text)
        if json_match:
            try:
                return json.loads(json_match.group())
            except (json.JSONDecodeError, ValueError):
                pass
        # Heuristic: check for keywords
        if "ABORT" in text.upper():
            return {"status": "ABORTED", "abort_reason": text[:200]}
        if "FAILED" in text.upper():
            return {"status": "FAILED"}
        return {"status": "COMPLETED"}
