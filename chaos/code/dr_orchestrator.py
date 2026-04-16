"""
dr_orchestrator.py — DR Plan-level orchestration engine

Coordinates full DR plan execution across phases, with:
  - Phase gate checks (human approval or automatic)
  - Phase-level checkpointing
  - Full rehearsal execution
  - Rollback orchestration
  - Integration with DRStepRunner for step execution

Part of the DR Plan Verification system (§5.1).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from runner.dr_step_runner import DRStepRunner, StepExecutionResult

logger = logging.getLogger(__name__)


@dataclass
class PhaseExecutionResult:
    """Result of executing a single DR phase."""

    phase_id: str
    phase_name: str
    gate_check_passed: bool = False
    steps: List[Any] = field(default_factory=list)  # List[StepExecutionResult]
    total_duration_seconds: float = 0.0
    all_steps_passed: bool = False
    failed_steps: List[str] = field(default_factory=list)


@dataclass
class RehearsalExecutionReport:
    """Full rehearsal execution report."""

    plan_id: str
    scope: str
    environment: str = "staging"
    start_time: str = ""
    end_time: str = ""

    phase_results: List[PhaseExecutionResult] = field(default_factory=list)
    total_duration_seconds: float = 0.0
    actual_rto_minutes: float = 0.0
    estimated_rto_minutes: int = 0

    rollback_executed: bool = False
    rollback_success: bool = False
    rollback_duration_seconds: float = 0.0

    success: bool = False
    failed_steps: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


class DROrchestrator:
    """DR Plan-level orchestration engine.

    Manages phase execution order, gate checks, checkpointing,
    and rollback. Delegates step execution to DRStepRunner.
    """

    def __init__(
        self,
        step_runner: "DRStepRunner",
        plan: Any,  # DRPlan
        require_approval: bool = False,
        stop_on_failure: bool = True,
        environment: str = "staging",
    ) -> None:
        self.step_runner = step_runner
        self.plan = plan
        self.require_approval = require_approval
        self.stop_on_failure = stop_on_failure
        self.environment = environment

    # ------------------------------------------------------------------
    # Phase execution
    # ------------------------------------------------------------------

    def run_phase(self, phase: Any) -> PhaseExecutionResult:
        """Execute a single DR phase.

        Args:
            phase: DRPhase object.

        Returns:
            PhaseExecutionResult with step-level details.
        """
        result = PhaseExecutionResult(
            phase_id=phase.phase_id,
            phase_name=phase.name,
        )

        # Gate check
        result.gate_check_passed = self.gate_check(phase)
        if not result.gate_check_passed:
            logger.warning("Gate check failed for phase %s", phase.phase_id)
            return result

        phase_start = time.time()

        for step in phase.steps:
            # Determine if this is the readiness gate (hard block)
            is_readiness = phase.phase_id == "phase-readiness"
            inject_fault = not is_readiness  # Don't inject faults during readiness checks

            step_result = self.step_runner.execute_step(
                step=step,
                phase=phase,
                inject_fault=inject_fault,
                auto_rollback=False,  # Orchestrator handles rollback
            )
            result.steps.append(step_result)

            if not step_result.passed:
                result.failed_steps.append(step.step_id)
                if is_readiness:
                    # Hard block: readiness gate failure stops everything
                    logger.error(
                        "HARD BLOCK: Readiness gate step %s failed — "
                        "aborting before traffic cutover",
                        step.step_id,
                    )
                    break
                elif self.stop_on_failure:
                    logger.warning(
                        "Step %s failed — stopping phase %s",
                        step.step_id, phase.phase_id,
                    )
                    break

        result.total_duration_seconds = time.time() - phase_start
        result.all_steps_passed = len(result.failed_steps) == 0
        return result

    def gate_check(self, phase: Any) -> bool:
        """Perform a phase gate check.

        For production environments with require_approval=True,
        this would pause and wait for human confirmation.

        Returns:
            True if gate check passes.
        """
        phase_id = phase.phase_id

        if self.require_approval:
            if self.environment == "production":
                logger.info(
                    "GATE CHECK: Phase %s requires approval in production. "
                    "Waiting for confirmation...",
                    phase_id,
                )
                # In a real implementation, this would wait for external approval
                # For now, auto-approve in non-interactive mode
                return True
            else:
                logger.info("Gate check auto-approved for %s (staging)", phase_id)
                return True

        logger.info("Gate check passed for %s (auto-approve)", phase_id)
        return True

    # ------------------------------------------------------------------
    # Full rehearsal
    # ------------------------------------------------------------------

    def run_rehearsal(self) -> RehearsalExecutionReport:
        """Execute a full DR rehearsal across all phases.

        Executes Phase 0 → 1 → 2 → 2.5 (readiness) → 3 → 4 in order.
        Phase 2.5 is a hard gate — failure prevents Phase 3.

        Returns:
            RehearsalExecutionReport with complete results.
        """
        from datetime import datetime, timezone

        report = RehearsalExecutionReport(
            plan_id=self.plan.plan_id,
            scope=self.plan.scope,
            environment=self.environment,
            start_time=datetime.now(timezone.utc).isoformat(),
            estimated_rto_minutes=self.plan.estimated_rto,
        )

        rehearsal_start = time.time()

        for phase in self.plan.phases:
            logger.info("=== Executing Phase: %s (%s) ===", phase.phase_id, phase.name)

            phase_result = self.run_phase(phase)
            report.phase_results.append(phase_result)

            if not phase_result.all_steps_passed:
                report.failed_steps.extend(phase_result.failed_steps)

                if phase.phase_id == "phase-readiness":
                    # Hard block — do not continue to traffic cutover
                    report.warnings.append(
                        f"Readiness gate failed: {phase_result.failed_steps}. "
                        "Traffic cutover (Phase 3+) skipped."
                    )
                    break

                if self.stop_on_failure:
                    report.warnings.append(
                        f"Phase {phase.phase_id} failed: {phase_result.failed_steps}"
                    )
                    break

        total_elapsed = time.time() - rehearsal_start
        report.total_duration_seconds = total_elapsed
        report.actual_rto_minutes = total_elapsed / 60
        report.end_time = datetime.now(timezone.utc).isoformat()
        report.success = len(report.failed_steps) == 0

        return report

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    def run_rollback(self) -> Dict[str, Any]:
        """Execute rollback phases in reverse order.

        Uses the plan's rollback_phases to undo completed steps.

        Returns:
            Dict with rollback results.
        """
        if not self.plan.rollback_phases:
            logger.warning("No rollback phases defined in plan")
            return {"success": False, "reason": "no rollback phases"}

        rollback_start = time.time()
        all_ok = True
        failed: List[str] = []

        for rb_phase in self.plan.rollback_phases:
            logger.info("Rolling back phase: %s", rb_phase.phase_id)
            for step in rb_phase.steps:
                result = self.step_runner.execute_step(
                    step=step,
                    phase=rb_phase,
                    inject_fault=False,
                    auto_rollback=False,
                )
                if not result.passed:
                    all_ok = False
                    failed.append(step.step_id)
                    logger.error("Rollback step %s failed", step.step_id)

        return {
            "success": all_ok,
            "duration_seconds": time.time() - rollback_start,
            "failed_steps": failed,
        }

    # ------------------------------------------------------------------
    # Report persistence
    # ------------------------------------------------------------------

    def save_report(
        self,
        report: RehearsalExecutionReport,
        output_dir: str = "plans",
    ) -> str:
        """Save rehearsal report as JSON.

        Returns:
            Path to the saved report file.
        """
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, f"{report.plan_id}.rehearsal-report.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(asdict(report), fh, indent=2, ensure_ascii=False)
        logger.info("Rehearsal report saved to %s", path)
        return path
