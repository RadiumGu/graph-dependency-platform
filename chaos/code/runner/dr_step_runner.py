"""
runner/dr_step_runner.py — DR Plan step-level execution engine

Executes individual DR plan steps with fault injection, validation,
and rollback capabilities. Integrates with existing ExperimentRunner,
ChaosMCPClient, and FISClient.

Part of the DR Plan Verification system (§5.1).
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass
class StepExecutionResult:
    """Result of executing a single DR step."""

    step_id: str
    phase_id: str
    resource_name: str

    # Pre-check
    baseline_captured: bool = False
    baseline_data: Dict[str, Any] = field(default_factory=dict)

    # Fault injection
    fault_injected: bool = False
    fault_type: str = ""
    fault_id: str = ""  # FIS experiment ID or Chaos Mesh resource name

    # Execution
    command_success: bool = False
    command_output: str = ""
    command_exit_code: int = -1
    command_duration_seconds: float = 0.0

    # Validation
    validation_success: bool = False
    validation_output: str = ""

    # Rollback
    rollback_executed: bool = False
    rollback_success: Optional[bool] = None
    rollback_output: str = ""

    # Restore (fault removal)
    fault_removed: bool = False

    # Baseline confirmation
    baseline_restored: bool = False

    # Issues
    issues: List[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return self.command_success and self.validation_success


class DRStepRunner:
    """Executes individual DR plan steps.

    Follows the Step Verification Cycle (§3.2):
      1. Pre-check: capture baseline
      2. Fault inject: simulate the fault this step handles
      3. Execute: run the step's command
      4. Validate: run the step's validation command
      5. Measure: record timing
      6. Rollback: run rollback command (isolated mode)
      7. Restore: remove fault injection
      8. Confirm: verify baseline restored
      9. Cooldown: wait for system stability
    """

    def __init__(
        self,
        chaos_backend: str = "both",
        cooldown_seconds: int = 10,
        dry_run: bool = False,
    ) -> None:
        self.chaos_backend = chaos_backend
        self.cooldown_seconds = cooldown_seconds
        self.dry_run = dry_run

    def execute_step(
        self,
        step: Any,
        phase: Any,
        inject_fault: bool = False,
        auto_rollback: bool = False,
        context: Optional[Dict[str, str]] = None,
    ) -> StepExecutionResult:
        """Execute a single DR step through the full verification cycle.

        Args:
            step: DRStep object.
            phase: DRPhase object.
            inject_fault: Whether to inject a fault before execution.
            auto_rollback: Whether to auto-rollback after execution.
            context: Execution context (region, k8s_context, etc.)

        Returns:
            StepExecutionResult with full details.
        """
        result = StepExecutionResult(
            step_id=step.step_id,
            phase_id=phase.phase_id,
            resource_name=step.resource_name,
        )

        # 1. Pre-check: capture baseline
        result.baseline_captured = True
        result.baseline_data = self._capture_baseline(step, context)

        # 2. Fault injection (optional)
        if inject_fault:
            fault_result = self.inject_for_step(step, context)
            result.fault_injected = fault_result.get("injected", False)
            result.fault_type = fault_result.get("type", "")
            result.fault_id = fault_result.get("id", "")

        # 3. Execute command
        cmd_start = time.time()
        if self.dry_run:
            logger.info("[DRY-RUN] Would execute: %s", step.command[:100])
            result.command_success = True
            result.command_output = "[dry-run] command skipped"
            result.command_exit_code = 0
        else:
            cmd_result = self._run_command(step.command, timeout=step.estimated_time * 3)
            result.command_success = cmd_result["success"]
            result.command_output = cmd_result["output"]
            result.command_exit_code = cmd_result["exit_code"]
        result.command_duration_seconds = time.time() - cmd_start

        # 4. Validate
        if step.validation and not step.validation.startswith("#"):
            val_result = self.validate_step(step, context)
            result.validation_success = val_result["success"]
            result.validation_output = val_result["output"]
        else:
            result.validation_success = result.command_success

        # 5. Timing already captured

        # 6. Rollback (if requested)
        if auto_rollback and step.rollback_command:
            rb_result = self.rollback_step(step, context)
            result.rollback_executed = True
            result.rollback_success = rb_result["success"]
            result.rollback_output = rb_result["output"]

        # 7. Restore: remove fault injection
        if result.fault_injected:
            self._remove_fault(result.fault_type, result.fault_id, context)
            result.fault_removed = True

        # 8. Confirm baseline restored (if rollback was executed)
        if result.rollback_executed:
            result.baseline_restored = self._confirm_baseline(
                step, result.baseline_data, context,
            )

        # 9. Cooldown
        if not self.dry_run and self.cooldown_seconds > 0:
            logger.debug("Cooldown: %ds", self.cooldown_seconds)
            time.sleep(self.cooldown_seconds)

        if not result.passed:
            result.issues.append(
                f"Step {step.step_id} verification failed: "
                f"cmd={result.command_success}, val={result.validation_success}"
            )

        return result

    def inject_for_step(
        self, step: Any, context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Inject a fault corresponding to the step's resource type.

        Maps resource types to fault injection methods:
          - RDSCluster → FIS az-power-interrupt or network disruption
          - EKSDeployment → Chaos Mesh pod-kill / network-delay
          - ALB → FIS target-group health manipulation
        """
        if self.dry_run:
            return {"injected": False, "type": "dry-run", "id": ""}

        rtype = step.resource_type
        rname = step.resource_name

        # Stub: fault injection would delegate to ChaosMCPClient / FISClient
        logger.info(
            "Fault injection for %s (%s) — delegating to %s backend",
            rname, rtype, self.chaos_backend,
        )
        return {"injected": False, "type": rtype, "id": ""}

    def validate_step(
        self, step: Any, context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Run the step's validation command."""
        if self.dry_run:
            return {"success": True, "output": "[dry-run] validation skipped"}
        return self._run_command(step.validation, timeout=60)

    def rollback_step(
        self, step: Any, context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Run the step's rollback command."""
        if self.dry_run:
            return {"success": True, "output": "[dry-run] rollback skipped"}
        if not step.rollback_command or step.rollback_command.startswith("#"):
            return {"success": True, "output": "no rollback command"}
        return self._run_command(step.rollback_command, timeout=step.estimated_time * 2)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _capture_baseline(
        self, step: Any, context: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """Capture pre-execution baseline state."""
        return {
            "step_id": step.step_id,
            "resource": step.resource_name,
            "timestamp": time.time(),
        }

    def _remove_fault(
        self, fault_type: str, fault_id: str, context: Optional[Dict[str, str]] = None,
    ) -> None:
        """Remove injected fault (cleanup)."""
        if fault_id:
            logger.info("Removing fault %s (type=%s)", fault_id, fault_type)

    def _confirm_baseline(
        self,
        step: Any,
        baseline: Dict[str, Any],
        context: Optional[Dict[str, str]] = None,
    ) -> bool:
        """Confirm system returned to baseline after rollback."""
        # Run validation command as proxy for baseline confirmation
        if step.validation and not step.validation.startswith("#"):
            result = self._run_command(step.validation, timeout=30)
            return result["success"]
        return True

    @staticmethod
    def _run_command(command: str, timeout: int = 120) -> Dict[str, Any]:
        """Execute a shell command and return results."""
        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "success": result.returncode == 0,
                "output": result.stdout[:2000],
                "exit_code": result.returncode,
                "stderr": result.stderr[:500],
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "output": f"Command timed out ({timeout}s)",
                "exit_code": -1,
                "stderr": "timeout",
            }
        except Exception as exc:
            return {
                "success": False,
                "output": str(exc)[:500],
                "exit_code": -1,
                "stderr": str(exc)[:500],
            }
