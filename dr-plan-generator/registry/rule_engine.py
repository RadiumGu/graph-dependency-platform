"""
registry/rule_engine.py — Custom rule execution engine

Evaluates CustomRule objects against DR plan steps at runtime.
Used during plan generation, verification, and rehearsal to enforce
business rules defined in natural language (PRD §10.3).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional

from validation.verification_models import CustomRule, RuleResult, RuleScope

if TYPE_CHECKING:
    from models import DRPhase, DRStep

logger = logging.getLogger(__name__)


class RuleEngine:
    """Custom rule execution engine.

    Evaluates rules against steps based on trigger type and scope.
    Supports condition types: time, metric, state, approval, custom.
    """

    def __init__(self, rules: List[CustomRule]) -> None:
        self.rules = rules
        # Index by trigger type for efficient lookup
        self._before_step_rules = [r for r in rules if r.trigger == "before_step"]
        self._after_step_rules = [r for r in rules if r.trigger == "after_step"]
        self._before_phase_rules = [r for r in rules if r.trigger == "before_phase"]
        self._after_phase_rules = [r for r in rules if r.trigger == "after_phase"]
        self._before_plan_rules = [r for r in rules if r.trigger == "before_plan"]

    def check_before_step(
        self,
        step: "DRStep",
        phase: "DRPhase",
        context: Any = None,
    ) -> List[RuleResult]:
        """Check all applicable before_step rules.

        Args:
            step: The DR step about to execute.
            phase: The phase containing the step.
            context: ExecutionContext (optional).

        Returns:
            List of RuleResult, one per applicable rule.
        """
        results: List[RuleResult] = []
        for rule in self._before_step_rules:
            if self._rule_applies(rule, step, phase):
                result = self._evaluate(rule, step, context)
                results.append(result)
                if result.action == "block" and not result.passed:
                    break  # Block rule failed → stop checking
        return results

    def check_after_step(
        self,
        step: "DRStep",
        phase: "DRPhase",
        context: Any = None,
    ) -> List[RuleResult]:
        """Check all applicable after_step rules."""
        results: List[RuleResult] = []
        for rule in self._after_step_rules:
            if self._rule_applies(rule, step, phase):
                results.append(self._evaluate(rule, step, context))
        return results

    def check_before_phase(
        self,
        phase: "DRPhase",
        context: Any = None,
    ) -> List[RuleResult]:
        """Check all applicable before_phase rules."""
        results: List[RuleResult] = []
        for rule in self._before_phase_rules:
            if self._phase_applies(rule, phase):
                results.append(self._evaluate_phase_rule(rule, phase, context))
        return results

    def check_before_plan(self, context: Any = None) -> List[RuleResult]:
        """Check all before_plan rules."""
        return [
            self._evaluate_plan_rule(rule, context)
            for rule in self._before_plan_rules
        ]

    # ------------------------------------------------------------------
    # Applicability checks
    # ------------------------------------------------------------------

    @staticmethod
    def _rule_applies(rule: CustomRule, step: "DRStep", phase: "DRPhase") -> bool:
        """Check if a rule applies to the given step and phase."""
        scope = rule.scope

        if scope.phase_ids and phase.phase_id not in scope.phase_ids:
            return False

        if scope.resource_types and step.resource_type not in scope.resource_types:
            return False

        if scope.resource_names and step.resource_name not in scope.resource_names:
            return False

        return True

    @staticmethod
    def _phase_applies(rule: CustomRule, phase: "DRPhase") -> bool:
        """Check if a phase-level rule applies."""
        scope = rule.scope
        if scope.phase_ids and phase.phase_id not in scope.phase_ids:
            return False
        return True

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def _evaluate(
        self,
        rule: CustomRule,
        step: "DRStep",
        context: Any = None,
    ) -> RuleResult:
        """Evaluate a single rule against a step."""
        condition = rule.condition

        if condition.type == "time":
            return self._check_time_window(rule)
        elif condition.type == "custom" and condition.command:
            return self._check_command(rule, step, context)
        elif condition.type == "metric":
            return self._check_metric(rule, step, context)
        elif condition.type == "state":
            return self._check_state(rule, step, context)
        elif condition.type == "approval":
            return RuleResult(
                rule_id=rule.id,
                passed=False,
                action=rule.action,
                message=f"Requires manual approval: {rule.action_message}",
            )
        else:
            # Unknown or generic custom rule — warn only
            return RuleResult(
                rule_id=rule.id,
                passed=True,
                action="warn",
                message=f"Rule type '{condition.type}' not auto-evaluated: {rule.source_text}",
            )

    def _evaluate_phase_rule(
        self,
        rule: CustomRule,
        phase: "DRPhase",
        context: Any = None,
    ) -> RuleResult:
        """Evaluate a phase-level rule."""
        return self._evaluate(rule, phase.steps[0] if phase.steps else None, context)

    def _evaluate_plan_rule(
        self,
        rule: CustomRule,
        context: Any = None,
    ) -> RuleResult:
        """Evaluate a plan-level rule."""
        condition = rule.condition
        if condition.type == "time":
            return self._check_time_window(rule)
        return RuleResult(
            rule_id=rule.id,
            passed=True,
            action="warn",
            message=f"Plan-level rule not auto-evaluated: {rule.source_text}",
        )

    # ------------------------------------------------------------------
    # Condition checkers
    # ------------------------------------------------------------------

    def _check_time_window(self, rule: CustomRule) -> RuleResult:
        """Check if current time is within a blocked window."""
        condition = rule.condition
        threshold = condition.threshold

        if not isinstance(threshold, dict):
            return RuleResult(
                rule_id=rule.id,
                passed=True,
                action=rule.action,
                message="No time window threshold defined",
            )

        tz_name = threshold.get("timezone", "UTC")
        blocked_windows = threshold.get("blocked_windows", [])

        try:
            from zoneinfo import ZoneInfo
            now = datetime.now(ZoneInfo(tz_name))
        except (ImportError, KeyError):
            now = datetime.utcnow()

        current_time = now.strftime("%H:%M")

        for window in blocked_windows:
            start = window.get("start", "00:00")
            end = window.get("end", "23:59")
            if start <= current_time <= end:
                return RuleResult(
                    rule_id=rule.id,
                    passed=False,
                    action=rule.action,
                    message=rule.action_message or f"Current time {current_time} is in blocked window {start}-{end}",
                    details=f"timezone={tz_name}, window={start}-{end}",
                )

        return RuleResult(
            rule_id=rule.id,
            passed=True,
            action=rule.action,
            message=f"Current time {current_time} is outside all blocked windows",
        )

    def _check_command(
        self,
        rule: CustomRule,
        step: Optional["DRStep"],
        context: Any = None,
    ) -> RuleResult:
        """Execute a check command and evaluate the result."""
        command = rule.condition.command or rule.check_command
        if not command:
            return RuleResult(
                rule_id=rule.id,
                passed=True,
                action=rule.action,
                message="No check command defined",
            )

        # Substitute step variables
        if step:
            command = command.replace("{name}", step.resource_name or "")
            command = command.replace("{resource_type}", step.resource_type or "")

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            passed = result.returncode == 0
            return RuleResult(
                rule_id=rule.id,
                passed=passed,
                action=rule.action,
                message=rule.action_message if not passed else f"Check passed: {rule.source_text}",
                details=result.stdout[:500],
            )
        except Exception as exc:
            return RuleResult(
                rule_id=rule.id,
                passed=False,
                action=rule.action,
                message=f"Check command failed: {exc}",
                details=str(exc)[:500],
            )

    def _check_metric(
        self,
        rule: CustomRule,
        step: Optional["DRStep"],
        context: Any = None,
    ) -> RuleResult:
        """Check a CloudWatch metric condition."""
        # Delegate to command check if a command is available
        if rule.condition.command:
            return self._check_command(rule, step, context)

        return RuleResult(
            rule_id=rule.id,
            passed=True,
            action="warn",
            message=f"Metric check not implemented: {rule.condition.check}",
        )

    def _check_state(
        self,
        rule: CustomRule,
        step: Optional["DRStep"],
        context: Any = None,
    ) -> RuleResult:
        """Check a resource state condition."""
        if rule.condition.command:
            return self._check_command(rule, step, context)

        return RuleResult(
            rule_id=rule.id,
            passed=True,
            action="warn",
            message=f"State check not implemented: {rule.condition.check}",
        )
