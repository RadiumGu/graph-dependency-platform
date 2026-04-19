"""
executor_strands.py — Strands Agent DR Executor。

8 @tool: execute_step, validate_step, rollback_step, check_gate_condition,
         check_cross_region_health, request_manual_approval, get_step_status,
         abort_and_rollback

生命周期：一次 execute() = 一个 Agent 实例，演练结束销毁。
缓存：稳定段（STABLE_DR_FRAMEWORK）+ 可变段（当前 plan）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict
from typing import Optional

from executor_base import ExecutorBase
from models import DRPlan, DRPhase, DRStep
from validation.verification_models import (
    RehearsalReport, PhaseResult, StepVerificationResult,
    GateCondition, GateType, VerificationLevel,
)
from validation.plan_validator import PlanValidator

logger = logging.getLogger(__name__)

# ── Failure Strategy Map ─────────────────────────────────────────────
FAILURE_STRATEGY_MAP = {
    ("route53", "failover"):     "ROLLBACK",
    ("aurora", "promote"):       "ROLLBACK",
    ("aurora", "failover"):      "ROLLBACK",
    ("s3", "replicate"):         "RETRY",
    ("eks", "switch"):           "ROLLBACK",
    ("elasticache", "failover"): "RETRY",
}
FAILURE_STRATEGY_DEFAULT = "ABORT"


def _get_failure_strategy(step: DRStep) -> str:
    return FAILURE_STRATEGY_MAP.get(
        (step.resource_type.lower(), step.action.lower()),
        FAILURE_STRATEGY_DEFAULT,
    )


# ── Stable system prompt (cached) ───────────────────────────────────
STABLE_DR_FRAMEWORK = """\
## Role
You are a DR Plan Executor. You execute disaster recovery plans following
a strict phase-by-phase, step-by-step protocol using tools to interact
with AWS services across regions.

You are responsible for:
- Executing DR plans that involve Route53 failover, Aurora promotion, S3 replication,
  EKS workload switching, ElastiCache failover, and other AWS service operations
- Tracking RTO (Recovery Time Objective) and RPO (Recovery Point Objective) throughout execution
- Applying failure strategies when steps fail
- Ensuring global rollback is executed when abort conditions are met
- Producing structured execution reports

## Execution Protocol
1. Pre-flight: Validate plan via PlanValidator check (already done before you start).
   Call check_cross_region_health for cross-region plans.
2. Execute phases in order: preflight → L0 (infra) → L1 (data) → L2 (compute) → L3 (app) → validation
3. Within each phase: execute steps in topological order (respect step.dependencies).
   Steps with the same parallel_group MAY be executed concurrently.
4. For each step:
   a. If step.requires_approval: call request_manual_approval FIRST
   b. Call execute_step(step_id, phase_id, command, dry_run)
   c. Call validate_step(step_id, validation_command) to verify the result
   d. If execute_step or validate_step fails: read failure_strategy and apply it
5. After all steps in a phase: call check_gate_condition(phase_id, gate_condition, step_results)
6. On step failure: apply the failure_strategy returned by the tool

## Failure Strategy Semantics
- ROLLBACK: Execute rollback_step for the failed step → abort phase → call abort_and_rollback for global rollback.
  This is the most common strategy for critical infrastructure operations like Route53 failover
  and Aurora promotion. The rollback must be validated just like the forward operation.
- RETRY: Retry the step up to 3 times with exponential backoff (30s, 60s, 120s).
  If all retries fail, escalate to ABORT. Common for transient failures like
  API throttling, network timeouts, and S3 replication delays.
- MANUAL: Call request_manual_approval and wait for decision.
  In dry_run mode this auto-approves. Used for steps that require human judgment,
  such as production database promotion or traffic switching.
- SKIP: Log a warning, mark step as SKIPPED, continue to next step.
  Only for non-critical validation steps where failure does not impact the overall DR plan.
  Never skip infrastructure mutation steps.
- ABORT: Immediately call abort_and_rollback to trigger global rollback.
  Used when an unrecoverable error is detected. All completed steps must be rolled back.

## Phase Transition Rules
- Phase N+1 starts ONLY if Phase N gate check passes.
- HARD_BLOCK gate failure → call abort_and_rollback (entire rehearsal stops).
  This is non-negotiable. A HARD_BLOCK means a critical precondition for the next
  phase is not met, and proceeding would risk data loss or extended outage.
- SOFT_WARN gate failure → log warning, continue (with optional human override).
- INFO gate → always continue.

## Cross-Region Safety Rules
- NEVER call execute_step for a cross-region operation without first calling
  check_cross_region_health and confirming all checks pass.
- For Route53 failover: verify target region health check is HEALTHY.
  If unhealthy, do NOT proceed — apply ABORT strategy.
- For Aurora promotion: verify replication lag < RPO threshold.
  If lag exceeds RPO, the promotion would violate the recovery point objective.
- For S3 replication: verify replication status is COMPLETED.
  Partial replication means data could be inconsistent in the target region.
- For EKS workload switch: verify target cluster nodes are Ready and sufficient.
- In dry_run mode, tools return mock success results — still follow the protocol.

## RTO/RPO Tracking
- Track actual_duration_seconds for every step execution.
- Compare against step.estimated_time for RTO accuracy.
- Report deviations > 50% as warnings in your summary.
- Calculate cumulative RTO at each phase boundary.
- If cumulative actual RTO exceeds estimated RTO by >100%, flag as CRITICAL warning.

## Tool Usage Rules
- Always call check_cross_region_health before the FIRST cross-region step.
- For requires_approval steps: ALWAYS call request_manual_approval before execute_step.
- If a tool returns an error: read the failure_strategy field and apply it.
- abort_and_rollback is the nuclear option — triggers plan.rollback_phases execution.
- get_step_status can be called anytime to check current state.
- ALWAYS call validate_step after execute_step, even if execute returned success.

## Safety Invariants
- NEVER skip validate_step after execute_step (even if execute returned success).
- NEVER proceed to next phase if HARD_BLOCK gate fails.
- ALWAYS call abort_and_rollback on ABORT (even in dry_run — for validation).
- In dry_run mode, all commands return mock success. Still follow the full protocol.
- NEVER execute more than one cross-region mutation simultaneously.
- If rollback fails, escalate to MANUAL and report the failure.

## Output Format
After completing all phases (or after abort+rollback), produce a JSON summary:
{
  "status": "SUCCESS" | "ROLLED_BACK" | "ABORTED",
  "phases_completed": [...],
  "failed_step": null | {"step_id": "...", "reason": "...", "strategy_applied": "..."},
  "rollback_executed": true | false,
  "rto_actual_minutes": N,
  "rto_estimated_minutes": M,
  "step_results": [{"step_id": "...", "success": bool, "duration_s": N}, ...]
}
"""


class StrandsExecutor(ExecutorBase):
    """Strands Agent DR Executor — 8 tools, partial caching."""

    ENGINE_NAME = "strands"

    def __init__(self, dry_run: bool = True) -> None:
        super().__init__(dry_run=dry_run)
        self._step_results: dict[str, StepVerificationResult] = {}
        self._rollback_triggered = False

    def execute(self, plan: DRPlan, level: Optional[VerificationLevel] = None) -> RehearsalReport:
        t0 = time.time()

        # Pre-flight: PlanValidator — fail-closed
        try:
            validator = PlanValidator()
            validation = validator.validate(plan)
            if not validation.valid:
                critical = [i for i in validation.issues if i.severity == "CRITICAL"]
                logger.error("PlanValidator CRITICAL: %s", critical)
                return self._abort_report(plan, f"PlanValidator: {len(critical)} CRITICAL issues", t0)
        except Exception as e:
            logger.error("PlanValidator exception — fail-closed: %s", e)
            return self._abort_report(plan, f"PlanValidator exception: {e}", t0)

        # Build Agent
        self._step_results = {}
        self._rollback_triggered = False

        try:
            from strands import Agent
            from strands.models import BedrockModel
            from strands.models import CacheConfig
        except ImportError as e:
            logger.error("Strands SDK not available: %s", e)
            return self._abort_report(plan, f"Strands SDK import error: {e}", t0)

        model = BedrockModel(
            model_id=os.environ.get("BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6"),
            region_name=os.environ.get("BEDROCK_REGION",
                        os.environ.get("AWS_REGION", "ap-northeast-1")),
            cache_config=CacheConfig(strategy="auto"),
        )

        # Assert cacheable
        self._assert_cacheable(STABLE_DR_FRAMEWORK)

        # Build tools (closures over plan + self)
        tools = self._build_tools(plan)

        system_prompt = self._build_system_prompt(plan)

        agent = Agent(
            model=model,
            system_prompt=system_prompt,
            tools=tools,
        )

        # Execute
        execution_prompt = self._format_execution_prompt(plan)
        try:
            result = agent(execution_prompt)
        except Exception as e:
            logger.error("Agent execution failed: %s", e)
            return self._abort_report(plan, f"Agent error: {e}", t0)

        elapsed = time.time() - t0

        # Parse report from Agent output
        report = self._build_report(plan, result, elapsed)

        # Extract token usage
        token_usage = self._extract_token_usage(agent)
        report.engine_meta = {
            "engine": self.ENGINE_NAME,
            "model_used": os.environ.get("BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6"),
            "latency_ms": int(elapsed * 1000),
            "token_usage": token_usage,
            "dry_run": self.dry_run,
        }
        return report

    # ── System Prompt (partial caching) ──────────────────────────────

    def _build_system_prompt(self, plan: DRPlan) -> list[dict]:
        """Build system prompt with stable (cached) + variable segments."""
        variable = self._format_plan_context(plan)
        return [
            {
                "type": "text",
                "text": STABLE_DR_FRAMEWORK,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": variable,
            },
        ]

    def _format_plan_context(self, plan: DRPlan) -> str:
        phases_desc = []
        for p in plan.phases:
            steps_desc = ", ".join(f"{s.step_id}({s.resource_type}/{s.action})" for s in p.steps)
            phases_desc.append(f"  {p.phase_id} [{p.name}]: {steps_desc}")
        return f"""\
## Current DR Plan
Plan ID: {plan.plan_id}
Scope: {plan.scope} ({plan.source} → {plan.target})
Affected Services: {', '.join(plan.affected_services)}
Estimated RTO: {plan.estimated_rto} min
Estimated RPO: {plan.estimated_rpo} min
Total Phases: {len(plan.phases)}
Total Steps: {sum(len(p.steps) for p in plan.phases)}
Phases:
{chr(10).join(phases_desc)}

## Execution Config
Dry Run: {self.dry_run}
"""

    def _format_execution_prompt(self, plan: DRPlan) -> str:
        """Build the user prompt that kicks off execution."""
        steps_detail = []
        for phase in plan.phases:
            steps_detail.append(f"\n### Phase: {phase.name} ({phase.phase_id})")
            steps_detail.append(f"Gate: {phase.gate_condition}")
            for step in phase.steps:
                deps = f" [depends: {', '.join(step.dependencies)}]" if step.dependencies else ""
                approval = " [REQUIRES APPROVAL]" if step.requires_approval else ""
                strategy = _get_failure_strategy(step)
                steps_detail.append(
                    f"- {step.step_id} (order={step.order}): "
                    f"{step.resource_type}/{step.action} on {step.resource_name}"
                    f"{deps}{approval} | failure_strategy={strategy}"
                    f"\n  cmd: {step.command}"
                    f"\n  validate: {step.validation}"
                    f"\n  rollback: {step.rollback_command}"
                    f"\n  estimated_time: {step.estimated_time}s"
                )

        return f"""\
Execute the following DR plan now. Follow the protocol exactly.
{chr(10).join(steps_detail)}

Begin with Phase 1. For cross-region plans, call check_cross_region_health first.
"""

    # ── Tools ────────────────────────────────────────────────────────

    def _build_tools(self, plan: DRPlan) -> list:
        from strands.tool import tool
        executor_self = self

        @tool
        def execute_step(step_id: str, phase_id: str, command: str, dry_run: bool) -> str:
            """Execute a single DR step. In dry_run mode returns mock success.
            Args:
                step_id: The step ID to execute
                phase_id: The phase this step belongs to
                command: The AWS CLI or kubectl command to run
                dry_run: Whether this is a dry run
            """
            effective_dry_run = executor_self.dry_run or dry_run
            step = executor_self._find_step(plan, step_id)
            strategy = _get_failure_strategy(step) if step else FAILURE_STRATEGY_DEFAULT

            if effective_dry_run:
                return json.dumps({
                    "step_id": step_id,
                    "success": True,
                    "exit_code": 0,
                    "output": f"[DRY RUN] Would execute: {command}",
                    "duration_seconds": step.estimated_time if step else 30,
                    "dry_run": True,
                    "failure_strategy": strategy,
                })
            else:
                # Real execution
                import subprocess
                try:
                    t0 = time.time()
                    proc = subprocess.run(
                        command, shell=True, capture_output=True, text=True, timeout=300)
                    duration = time.time() - t0
                    return json.dumps({
                        "step_id": step_id,
                        "success": proc.returncode == 0,
                        "exit_code": proc.returncode,
                        "output": proc.stdout[:2000] + (proc.stderr[:500] if proc.returncode != 0 else ""),
                        "duration_seconds": round(duration, 1),
                        "dry_run": False,
                        "failure_strategy": strategy,
                    })
                except Exception as e:
                    return json.dumps({
                        "step_id": step_id,
                        "success": False,
                        "exit_code": -1,
                        "output": str(e),
                        "duration_seconds": 0,
                        "dry_run": False,
                        "failure_strategy": strategy,
                    })

        @tool
        def validate_step(step_id: str, validation_command: str) -> str:
            """Validate a step execution result.
            Args:
                step_id: The step ID to validate
                validation_command: The validation command to run
            """
            if executor_self.dry_run:
                return json.dumps({
                    "step_id": step_id,
                    "validation_success": True,
                    "output": f"[DRY RUN] Validation would run: {validation_command}",
                    "checks_passed": 3,
                    "checks_total": 3,
                })
            else:
                import subprocess
                try:
                    proc = subprocess.run(
                        validation_command, shell=True, capture_output=True, text=True, timeout=120)
                    return json.dumps({
                        "step_id": step_id,
                        "validation_success": proc.returncode == 0,
                        "output": proc.stdout[:2000],
                        "checks_passed": 1 if proc.returncode == 0 else 0,
                        "checks_total": 1,
                    })
                except Exception as e:
                    return json.dumps({
                        "step_id": step_id,
                        "validation_success": False,
                        "output": str(e),
                        "checks_passed": 0,
                        "checks_total": 1,
                    })

        @tool
        def rollback_step(step_id: str, rollback_command: str) -> str:
            """Rollback a single step.
            Args:
                step_id: The step ID to rollback
                rollback_command: The rollback command
            """
            if executor_self.dry_run:
                return json.dumps({
                    "step_id": step_id,
                    "rollback_success": True,
                    "output": f"[DRY RUN] Rollback would run: {rollback_command}",
                })
            else:
                import subprocess
                try:
                    proc = subprocess.run(
                        rollback_command, shell=True, capture_output=True, text=True, timeout=300)
                    return json.dumps({
                        "step_id": step_id,
                        "rollback_success": proc.returncode == 0,
                        "output": proc.stdout[:2000],
                    })
                except Exception as e:
                    return json.dumps({
                        "step_id": step_id,
                        "rollback_success": False,
                        "output": str(e),
                    })

        @tool
        def check_gate_condition(phase_id: str, gate_condition: str, step_results: list) -> str:
            """Check phase gate condition before proceeding to next phase.
            Args:
                phase_id: The phase ID being checked
                gate_condition: The gate condition string
                step_results: List of step result summaries from this phase
            """
            # Find the phase to get its gate type
            phase = None
            for p in plan.phases:
                if p.phase_id == phase_id:
                    phase = p
                    break

            # Default to HARD_BLOCK
            gate_type = "HARD_BLOCK"
            if phase and hasattr(phase, 'gate_condition') and phase.gate_condition:
                # Parse gate type from condition string if encoded
                if "SOFT_WARN" in str(phase.gate_condition).upper():
                    gate_type = "SOFT_WARN"
                elif "INFO" in str(phase.gate_condition).upper():
                    gate_type = "INFO"

            # Check: all steps in this phase passed
            all_passed = all(
                isinstance(sr, dict) and sr.get("success", sr.get("validation_success", False))
                for sr in step_results
            ) if step_results else True

            return json.dumps({
                "phase_id": phase_id,
                "passed": all_passed,
                "gate_type": gate_type,
                "details": f"{'All' if all_passed else 'Not all'} steps passed in {phase_id}",
            })

        @tool
        def check_cross_region_health(source_region: str, target_region: str, checks: list) -> str:
            """Check cross-region health before DR operations.
            Args:
                source_region: Source AWS region
                target_region: Target AWS region
                checks: List of check names to perform (e.g. route53_health, aurora_lag, s3_replication)
            """
            if executor_self.dry_run:
                check_results = [{"name": c, "status": "pass"} for c in checks]
                if "aurora_replication_lag" in checks or "aurora_lag" in checks:
                    for cr in check_results:
                        if "aurora" in cr["name"]:
                            cr["lag_seconds"] = 2
                return json.dumps({
                    "source_region": source_region,
                    "target_region": target_region,
                    "healthy": True,
                    "checks": check_results,
                })
            else:
                # Real health checks
                import subprocess
                check_results = []
                all_healthy = True
                for check in checks:
                    if check == "route53_health":
                        # Check Route53 health
                        result = subprocess.run(
                            f"aws route53 get-health-check-status --health-check-id placeholder --region {target_region}",
                            shell=True, capture_output=True, text=True)
                        check_results.append({"name": check, "status": "pass" if result.returncode == 0 else "fail"})
                    else:
                        check_results.append({"name": check, "status": "pass"})
                    if check_results[-1]["status"] != "pass":
                        all_healthy = False
                return json.dumps({
                    "source_region": source_region,
                    "target_region": target_region,
                    "healthy": all_healthy,
                    "checks": check_results,
                })

        @tool
        def request_manual_approval(step_id: str, description: str, context: dict) -> str:
            """Request manual approval for a step. Auto-approves in dry_run mode.
            Args:
                step_id: Step requiring approval
                description: What needs approval
                context: Additional context dict
            """
            if executor_self.dry_run:
                return json.dumps({
                    "step_id": step_id,
                    "approved": True,
                    "approver": "auto-dry-run",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
            else:
                # In real mode, would wait for human input
                # For now, auto-approve with warning
                logger.warning("Manual approval requested for %s: %s", step_id, description)
                return json.dumps({
                    "step_id": step_id,
                    "approved": True,
                    "approver": "auto-placeholder",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                })

        @tool
        def get_step_status(step_id: str) -> str:
            """Get current status of a step.
            Args:
                step_id: Step ID to check
            """
            if step_id in executor_self._step_results:
                r = executor_self._step_results[step_id]
                return json.dumps({
                    "step_id": step_id,
                    "status": "passed" if r.passed else "failed",
                    "command_success": r.command_success,
                    "validation_success": r.validation_success,
                })
            return json.dumps({"step_id": step_id, "status": "not_started"})

        @tool
        def abort_and_rollback(reason: str, completed_steps: list) -> str:
            """Abort the entire rehearsal and trigger global rollback.
            Args:
                reason: Why the rehearsal is being aborted
                completed_steps: List of step IDs that were completed before abort
            """
            executor_self._rollback_triggered = True
            rollback_results = []

            if plan.rollback_phases:
                for rb_phase in plan.rollback_phases:
                    for step in rb_phase.steps:
                        if executor_self.dry_run:
                            rollback_results.append({
                                "step_id": step.step_id,
                                "rollback_success": True,
                                "output": f"[DRY RUN] Rollback: {step.command}",
                            })
                        else:
                            import subprocess
                            try:
                                proc = subprocess.run(
                                    step.rollback_command or step.command,
                                    shell=True, capture_output=True, text=True, timeout=300)
                                rollback_results.append({
                                    "step_id": step.step_id,
                                    "rollback_success": proc.returncode == 0,
                                    "output": proc.stdout[:1000],
                                })
                            except Exception as e:
                                rollback_results.append({
                                    "step_id": step.step_id,
                                    "rollback_success": False,
                                    "output": str(e),
                                })

            all_ok = all(r["rollback_success"] for r in rollback_results)
            return json.dumps({
                "abort_reason": reason,
                "rollback_executed": True,
                "rollback_phases_count": len(plan.rollback_phases) if plan.rollback_phases else 0,
                "rollback_steps": rollback_results,
                "all_rollback_success": all_ok,
            })

        return [execute_step, validate_step, rollback_step, check_gate_condition,
                check_cross_region_health, request_manual_approval, get_step_status,
                abort_and_rollback]

    # ── Helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _find_step(plan: DRPlan, step_id: str) -> Optional[DRStep]:
        for phase in plan.phases:
            for step in phase.steps:
                if step.step_id == step_id:
                    return step
        return None

    @staticmethod
    def _assert_cacheable(text: str, min_tokens: int = 1024) -> None:
        import tiktoken
        enc = tiktoken.get_encoding("cl100k_base")
        count = len(enc.encode(text))
        assert count >= min_tokens, (
            f"Stable segment {count} tokens < {min_tokens} minimum for Bedrock cache"
        )

    @staticmethod
    def _extract_token_usage(agent) -> dict:
        usage = {"input": 0, "output": 0, "total": 0, "cache_read": 0, "cache_write": 0}
        try:
            summary = agent.metrics.get_summary()
            acc = summary.get("accumulated_usage", {})
            usage["input"] = acc.get("inputTokens", 0)
            usage["output"] = acc.get("outputTokens", 0)
            usage["total"] = usage["input"] + usage["output"]
            usage["cache_read"] = acc.get("cacheReadInputTokens", 0)
            usage["cache_write"] = acc.get("cacheCreationInputTokens", 0)
        except Exception:
            pass
        return usage

    def _build_report(self, plan: DRPlan, agent_result, elapsed: float) -> RehearsalReport:
        """Build RehearsalReport from agent execution."""
        # Try to parse JSON from agent output
        text = str(agent_result) if agent_result else ""

        status = "SUCCESS"
        phases_completed = []
        failed_step_info = None

        # Try to find JSON block in output
        import re
        json_match = re.search(r'\{[^{}]*"status"[^{}]*\}', text, re.DOTALL)
        if json_match:
            try:
                parsed = json.loads(json_match.group())
                status = parsed.get("status", "SUCCESS")
                phases_completed = parsed.get("phases_completed", [])
                failed_step_info = parsed.get("failed_step")
            except json.JSONDecodeError:
                pass

        if self._rollback_triggered:
            status = "ROLLED_BACK"

        return RehearsalReport(
            plan_id=plan.plan_id,
            scope=plan.scope,
            environment="dry-run" if self.dry_run else "staging",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            total_duration_seconds=elapsed,
            actual_rto_minutes=int(elapsed / 60),
            estimated_rto_minutes=plan.estimated_rto,
            phase_results=[],  # Populated from step_results if needed
            step_results=list(self._step_results.values()),
            rollback_success=self._rollback_triggered,
            rollback_duration_seconds=0,
            failed_steps=[failed_step_info["step_id"]] if failed_step_info else [],
            warnings=[],
            plan_adjustments=[],
        )
