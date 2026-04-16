"""
planner/rollback_generator.py — Rollback plan generator

Generates a reversed DR plan by flipping phase order (traffic → compute → data)
and swapping each step's command with its rollback_command.
"""

import logging
import os
import sys
from typing import List

_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..')
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, os.path.abspath(_PROJECT_ROOT))

from profiles.profile_loader import EnvironmentProfile
_profile = EnvironmentProfile()

from models import DRPhase, DRPlan, DRStep

logger = logging.getLogger(__name__)


class RollbackGenerator:
    """Generate rollback phases from an existing DR switchover plan.

    Key differences from the switchover plan:
    - Phase order is reversed (traffic → compute → data).
    - Each step uses rollback_command as its primary command.
    - All steps require approval (rollback is higher risk).
    - A rollback validation phase is appended.
    """

    def generate_rollback(self, plan: DRPlan) -> List[DRPhase]:
        """Build rollback phases from a DRPlan.

        Args:
            plan: The original switchover DRPlan.

        Returns:
            List of rollback DRPhase objects (in reversed order).
        """
        # Skip preflight and validation phases for the reversal
        switchover_phases = [
            p for p in plan.phases
            if p.layer not in ("preflight", "validation")
        ]
        switchover_phases = list(reversed(switchover_phases))

        rollback_phases: List[DRPhase] = []
        for phase in switchover_phases:
            rollback_steps: List[DRStep] = []
            for step in reversed(phase.steps):
                rollback_step = DRStep(
                    step_id=f"rollback-{step.step_id}",
                    order=0,
                    parallel_group=step.parallel_group,
                    resource_type=step.resource_type,
                    resource_id=step.resource_id,
                    resource_name=step.resource_name,
                    action=f"rollback_{step.action}",
                    command=step.rollback_command,
                    validation=step.validation,
                    expected_result=f"Original state of {step.resource_name}",
                    rollback_command="# Manual intervention required",
                    estimated_time=step.estimated_time,
                    requires_approval=True,
                    tier=step.tier,
                    dependencies=[],
                )
                rollback_steps.append(rollback_step)

            # Re-number steps in order
            for i, s in enumerate(rollback_steps, 1):
                s.order = i

            rollback_phases.append(
                DRPhase(
                    phase_id=f"rollback-{phase.phase_id}",
                    name=f"Rollback: {phase.name}",
                    layer=phase.layer,
                    steps=rollback_steps,
                    estimated_duration=phase.estimated_duration,
                    gate_condition=f"All {phase.name} rollback steps verified",
                )
            )

        rollback_phases.append(self._build_rollback_validation_phase(plan))
        return rollback_phases

    def _build_rollback_validation_phase(self, plan: DRPlan) -> DRPhase:
        """Build a post-rollback validation phase.

        Args:
            plan: The original DRPlan for context.

        Returns:
            DRPhase for post-rollback validation.
        """
        validation_step = DRStep(
            step_id="rollback-validation-e2e",
            order=1,
            resource_type="Synthetic",
            resource_id="",
            resource_name="rollback-smoke-test",
            action="verify_rollback_complete",
            command=(
                "# Verify traffic is back on original source\n"
                f"curl -sf https://{_profile.domain}{_profile.health_endpoint} | jq '.status'\n"
                f"# Verify source region is serving requests\n"
                f"aws sts get-caller-identity --region {plan.source}"
            ),
            validation=f"curl -sf https://{_profile.domain}{_profile.health_endpoint} | jq '.status'",
            expected_result="ok",
            rollback_command="# No further rollback available — escalate to incident commander",
            estimated_time=120,
            requires_approval=True,
            tier=None,
            dependencies=[],
        )
        return DRPhase(
            phase_id="rollback-phase-validation",
            name="Rollback Validation",
            layer="validation",
            steps=[validation_step],
            estimated_duration=2,
            gate_condition="Source region restored; all services healthy",
        )
