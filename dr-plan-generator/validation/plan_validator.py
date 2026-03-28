"""
validation/plan_validator.py — Static DR plan validation

Checks for dependency cycles, completeness, step ordering consistency,
rollback command presence, and graph data freshness.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from models import DRPlan, Issue, ValidationReport

logger = logging.getLogger(__name__)

# Warn if graph snapshot is older than this (seconds)
_FRESHNESS_THRESHOLD = 3600


class PlanValidator:
    """Perform static validation of a DRPlan.

    Checks:
    1. Dependency cycle detection.
    2. Completeness (all affected resources have steps).
    3. Ordering consistency (dependencies before dependents).
    4. Rollback command presence.
    5. Graph snapshot freshness.
    """

    def validate(self, plan: DRPlan) -> ValidationReport:
        """Validate a DRPlan and return a ValidationReport.

        Args:
            plan: The DRPlan to validate.

        Returns:
            ValidationReport with ``valid`` flag and list of Issues.
        """
        issues: List[Issue] = []

        # 1. Cycle detection
        cycles = self._check_cycles(plan)
        if cycles:
            issues.append(Issue("CRITICAL", f"Dependency cycles detected: {cycles}"))

        # 2. Completeness
        missing = self._check_completeness(plan)
        if missing:
            issues.append(Issue("WARNING", f"Resources not covered in plan: {missing}"))

        # 3. Ordering consistency
        ordering_violations = self._check_ordering(plan)
        if ordering_violations:
            issues.append(Issue("CRITICAL", f"Ordering violations: {ordering_violations}"))

        # 4. Rollback command completeness
        no_rollback = [
            s.step_id
            for p in plan.phases
            for s in p.steps
            if not s.rollback_command
        ]
        if no_rollback:
            issues.append(
                Issue(
                    "WARNING",
                    f"{len(no_rollback)} steps missing rollback commands: {no_rollback}",
                )
            )

        # 5. Graph freshness
        if plan.graph_snapshot_time:
            try:
                snapshot_dt = datetime.fromisoformat(
                    plan.graph_snapshot_time.replace("Z", "+00:00")
                )
                if snapshot_dt.tzinfo is None:
                    snapshot_dt = snapshot_dt.replace(tzinfo=timezone.utc)
                age_secs = (
                    datetime.now(timezone.utc) - snapshot_dt
                ).total_seconds()
                if age_secs > _FRESHNESS_THRESHOLD:
                    issues.append(
                        Issue(
                            "WARNING",
                            f"Graph snapshot is {age_secs // 60:.0f}min old; "
                            "consider re-running ETL before executing this plan",
                        )
                    )
            except ValueError as exc:
                logger.warning("Could not parse graph_snapshot_time: %s", exc)

        return ValidationReport(
            valid=all(i.severity != "CRITICAL" for i in issues),
            issues=issues,
        )

    # ------------------------------------------------------------------
    # Individual checks
    # ------------------------------------------------------------------

    def _check_cycles(self, plan: DRPlan) -> List[List[str]]:
        """Detect dependency cycles across all plan steps.

        Builds a step-level dependency graph and runs DFS cycle detection.

        Args:
            plan: DRPlan to check.

        Returns:
            List of detected cycles (each cycle is a list of step IDs).
        """
        # Collect all steps
        all_steps = [s for p in plan.phases for s in p.steps]
        step_ids = {s.step_id for s in all_steps}

        adj: Dict[str, List[str]] = {s.step_id: [] for s in all_steps}
        for step in all_steps:
            for dep in step.dependencies:
                if dep in step_ids:
                    adj[dep].append(step.step_id)

        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {sid: WHITE for sid in step_ids}
        cycles: List[List[str]] = []

        def dfs(node: str, path: List[str]) -> None:
            color[node] = GRAY
            path.append(node)
            for neighbor in adj.get(node, []):
                if color[neighbor] == GRAY:
                    cycle_start = path.index(neighbor)
                    cycles.append(path[cycle_start:] + [neighbor])
                elif color[neighbor] == WHITE:
                    dfs(neighbor, path)
            path.pop()
            color[node] = BLACK

        for sid in list(step_ids):
            if color[sid] == WHITE:
                dfs(sid, [])

        return cycles

    def _check_completeness(self, plan: DRPlan) -> List[str]:
        """Check that all affected resources have at least one step.

        Args:
            plan: DRPlan to check.

        Returns:
            List of resource names not covered by any step.
        """
        covered = {s.resource_name for p in plan.phases for s in p.steps}
        # Exclude infra/synthetic steps that don't map to real resource names
        excluded_prefixes = ("preflight-", "validation-", "rollback-validation-")
        covered_resources = {
            n for n in covered
            if not any(n.startswith(pfx) for pfx in excluded_prefixes)
        }

        missing = [
            r for r in plan.affected_resources
            if r not in covered_resources
        ]
        return missing

    def _check_ordering(self, plan: DRPlan) -> List[str]:
        """Verify that no step is scheduled before its dependencies.

        Rule: if step A lists step ID B in its dependencies, B must
        appear before A in the flattened step execution order.

        Args:
            plan: DRPlan to check.

        Returns:
            List of violation description strings.
        """
        step_order: Dict[str, int] = {}
        global_order = 0
        for phase in plan.phases:
            for step in phase.steps:
                step_order[step.step_id] = global_order
                step_order[step.resource_name] = global_order
                global_order += 1

        violations: List[str] = []
        for phase in plan.phases:
            for step in phase.steps:
                for dep_id in step.dependencies:
                    dep_pos = step_order.get(dep_id)
                    step_pos = step_order.get(step.step_id)
                    if dep_pos is not None and step_pos is not None:
                        if dep_pos > step_pos:
                            violations.append(
                                f"{step.step_id} is scheduled before its "
                                f"dependency {dep_id}"
                            )
        return violations
