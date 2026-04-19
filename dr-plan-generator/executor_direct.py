"""
executor_direct.py — Direct DR Executor（包装现有 DRPlanVerifier）。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from executor_base import ExecutorBase
from models import DRPlan
from validation.verification_models import RehearsalReport, VerificationLevel
from validation.plan_validator import PlanValidator

logger = logging.getLogger(__name__)


class DirectExecutor(ExecutorBase):
    """Direct engine: delegates to existing DRPlanVerifier."""

    ENGINE_NAME = "direct"

    def execute(self, plan: DRPlan, level: Optional[VerificationLevel] = None) -> RehearsalReport:
        """Execute DR plan via DRPlanVerifier.

        1. PlanValidator.validate() — CRITICAL → abort
        2. DRPlanVerifier.full_rehearsal() or dry_run() based on level
        """
        t0 = time.time()

        # Step 0: PlanValidator — fail-closed
        try:
            validator = PlanValidator()
            validation = validator.validate(plan)
            if not validation.valid:
                critical = [i for i in validation.issues if i.severity == "CRITICAL"]
                logger.error("PlanValidator CRITICAL issues: %s", critical)
                return self._abort_report(plan, f"PlanValidator failed: {len(critical)} CRITICAL issues")
        except Exception as e:
            logger.error("PlanValidator exception — fail-closed: %s", e)
            return self._abort_report(plan, f"PlanValidator exception: {e}")

        # Step 1: Execute via DRPlanVerifier
        from validation.plan_verifier import DRPlanVerifier
        verifier = DRPlanVerifier(plan=plan)

        effective_level = level
        if effective_level is None:
            effective_level = VerificationLevel.DRY_RUN if self.dry_run else VerificationLevel.FULL_REHEARSAL

        try:
            if effective_level == VerificationLevel.DRY_RUN:
                # dry_run returns DryRunReport, not RehearsalReport
                dry_result = verifier.dry_run()
                report = RehearsalReport(
                    plan_id=plan.plan_id,
                    scope=plan.scope,
                    environment="dry-run",
                    timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    total_duration_seconds=time.time() - t0,
                    actual_rto_minutes=0,
                    estimated_rto_minutes=plan.estimated_rto,
                    phase_results=[],
                    step_results=[],
                    rollback_success=None,
                    rollback_duration_seconds=0,
                    failed_steps=[s.name for s in dry_result.checks if s.status.name != "PASS"] if hasattr(dry_result, 'checks') else [],
                    warnings=[],
                    plan_adjustments=[],
                )
            elif effective_level == VerificationLevel.FULL_REHEARSAL:
                report = verifier.full_rehearsal(auto_rollback=True)
            else:
                report = verifier.step_verify()

        except Exception as e:
            logger.error("DRPlanVerifier execution failed: %s", e)
            report = self._abort_report(plan, f"Execution failed: {e}")

        elapsed = time.time() - t0

        # Attach engine metadata
        report.engine_meta = {
            "engine": self.ENGINE_NAME,
            "model_used": None,
            "latency_ms": int(elapsed * 1000),
            "token_usage": {"input": 0, "output": 0, "total": 0, "cache_read": 0, "cache_write": 0},
            "dry_run": self.dry_run,
        }
        return report

    @staticmethod
    def _abort_report(plan: DRPlan, reason: str) -> RehearsalReport:
        """Create an aborted RehearsalReport."""
        return RehearsalReport(
            plan_id=plan.plan_id,
            scope=plan.scope,
            environment="aborted",
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            total_duration_seconds=0,
            actual_rto_minutes=0,
            estimated_rto_minutes=plan.estimated_rto,
            phase_results=[],
            step_results=[],
            rollback_success=None,
            rollback_duration_seconds=0,
            failed_steps=[],
            warnings=[reason],
            plan_adjustments=[],
        )
