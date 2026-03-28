"""
tests/test_plan_validator.py — Unit tests for PlanValidator

Tests ordering validation, completeness check, cycle detection,
rollback completeness, and graph freshness checks.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import DRPhase, DRPlan, DRStep, ImpactReport
from validation.plan_validator import PlanValidator


def _make_step(
    step_id: str,
    resource_name: str,
    resource_type: str = "Microservice",
    dependencies: list = None,
    rollback_command: str = "rollback cmd",
    order: int = 1,
) -> DRStep:
    return DRStep(
        step_id=step_id,
        order=order,
        resource_type=resource_type,
        resource_id="",
        resource_name=resource_name,
        action="test_action",
        command="test cmd",
        validation="test validation",
        expected_result="ok",
        rollback_command=rollback_command,
        estimated_time=60,
        requires_approval=False,
        tier="Tier1",
        dependencies=dependencies or [],
    )


def _make_plan(
    phases: list = None,
    affected_resources: list = None,
    graph_snapshot_time: str = None,
) -> DRPlan:
    now = datetime.now(timezone.utc).isoformat()
    return DRPlan(
        plan_id="test-plan",
        created_at=now,
        scope="az",
        source="apne1-az1",
        target="apne1-az2",
        affected_services=[],
        affected_resources=affected_resources or [],
        phases=phases or [],
        rollback_phases=[],
        impact_assessment=ImpactReport(scope="az", source="apne1-az1"),
        estimated_rto=10,
        estimated_rpo=5,
        validation_status="pending",
        graph_snapshot_time=graph_snapshot_time or now,
    )


class TestCycleDetection(unittest.TestCase):

    def setUp(self) -> None:
        self.validator = PlanValidator()

    def test_no_cycle_passes(self) -> None:
        step_a = _make_step("step-a", "resource-a", dependencies=[])
        step_b = _make_step("step-b", "resource-b", dependencies=["step-a"])
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step_a, step_b], estimated_duration=2, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        cycle_issues = [i for i in report.issues if "cycle" in i.message.lower()]
        self.assertEqual(cycle_issues, [])

    def test_cycle_detected(self) -> None:
        step_a = _make_step("step-a", "resource-a", dependencies=["step-b"])
        step_b = _make_step("step-b", "resource-b", dependencies=["step-a"])
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step_a, step_b], estimated_duration=2, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        cycle_issues = [i for i in report.issues if "cycle" in i.message.lower()]
        self.assertGreater(len(cycle_issues), 0)
        self.assertFalse(report.valid)


class TestCompletenessCheck(unittest.TestCase):

    def setUp(self) -> None:
        self.validator = PlanValidator()

    def test_all_resources_covered_passes(self) -> None:
        step = _make_step("step-a", "petsite")
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step], estimated_duration=1, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase], affected_resources=["petsite"])
        report = self.validator.validate(plan)
        completeness_issues = [i for i in report.issues if "not covered" in i.message]
        self.assertEqual(completeness_issues, [])

    def test_missing_resource_creates_warning(self) -> None:
        step = _make_step("step-a", "petsite")
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step], estimated_duration=1, gate_condition="ok",
        )
        plan = _make_plan(
            phases=[phase],
            affected_resources=["petsite", "petsite-db"],  # petsite-db has no step
        )
        report = self.validator.validate(plan)
        completeness_issues = [i for i in report.issues if "not covered" in i.message]
        self.assertGreater(len(completeness_issues), 0)
        self.assertEqual(completeness_issues[0].severity, "WARNING")

    def test_empty_plan_with_no_resources_passes(self) -> None:
        plan = _make_plan(phases=[], affected_resources=[])
        report = self.validator.validate(plan)
        completeness_issues = [i for i in report.issues if "not covered" in i.message]
        self.assertEqual(completeness_issues, [])


class TestOrderingCheck(unittest.TestCase):

    def setUp(self) -> None:
        self.validator = PlanValidator()

    def test_correct_order_passes(self) -> None:
        step_a = _make_step("step-a", "service-a", order=1, dependencies=[])
        step_b = _make_step("step-b", "service-b", order=2, dependencies=["step-a"])
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step_a, step_b], estimated_duration=2, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        ordering_issues = [i for i in report.issues if "ordering" in i.message.lower() or "scheduled before" in i.message.lower()]
        self.assertEqual(ordering_issues, [])

    def test_ordering_violation_detected(self) -> None:
        # step-b appears first but depends on step-a which appears second
        step_b = _make_step("step-b", "service-b", order=1, dependencies=["step-a"])
        step_a = _make_step("step-a", "service-a", order=2, dependencies=[])
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step_b, step_a],  # b before a, but b depends on a
            estimated_duration=2, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        ordering_issues = [
            i for i in report.issues
            if "ordering" in i.message.lower() or "scheduled before" in i.message.lower()
        ]
        self.assertGreater(len(ordering_issues), 0)
        self.assertFalse(report.valid)


class TestRollbackCompleteness(unittest.TestCase):

    def setUp(self) -> None:
        self.validator = PlanValidator()

    def test_missing_rollback_creates_warning(self) -> None:
        step = _make_step("step-a", "petsite", rollback_command="")
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step], estimated_duration=1, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        rollback_issues = [i for i in report.issues if "rollback" in i.message.lower()]
        self.assertGreater(len(rollback_issues), 0)
        self.assertEqual(rollback_issues[0].severity, "WARNING")

    def test_all_rollbacks_present_no_issue(self) -> None:
        step = _make_step("step-a", "petsite", rollback_command="aws rollback cmd")
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step], estimated_duration=1, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        rollback_issues = [i for i in report.issues if "rollback" in i.message.lower()]
        self.assertEqual(rollback_issues, [])


class TestGraphFreshness(unittest.TestCase):

    def setUp(self) -> None:
        self.validator = PlanValidator()

    def test_fresh_snapshot_no_warning(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        plan = _make_plan(graph_snapshot_time=now)
        report = self.validator.validate(plan)
        freshness_issues = [i for i in report.issues if "snapshot" in i.message.lower()]
        self.assertEqual(freshness_issues, [])

    def test_stale_snapshot_creates_warning(self) -> None:
        old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        plan = _make_plan(graph_snapshot_time=old_time)
        report = self.validator.validate(plan)
        freshness_issues = [i for i in report.issues if "snapshot" in i.message.lower()]
        self.assertGreater(len(freshness_issues), 0)
        self.assertEqual(freshness_issues[0].severity, "WARNING")
        # Stale snapshot is a warning, not critical → plan can still be valid
        self.assertTrue(report.valid)


class TestValidationReport(unittest.TestCase):

    def setUp(self) -> None:
        self.validator = PlanValidator()

    def test_clean_plan_is_valid(self) -> None:
        step = _make_step("step-a", "petsite")
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step], estimated_duration=1, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase], affected_resources=["petsite"])
        report = self.validator.validate(plan)
        self.assertTrue(report.valid)

    def test_critical_issue_makes_invalid(self) -> None:
        step_a = _make_step("step-a", "a", dependencies=["step-b"])
        step_b = _make_step("step-b", "b", dependencies=["step-a"])
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step_a, step_b], estimated_duration=1, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        self.assertFalse(report.valid)


if __name__ == "__main__":
    unittest.main()


class TestValidationQuality:
    """Tests for validation command quality checks."""

    def _make_plan_with_validation(self, validation_str):
        """Helper: create a minimal plan with one step having given validation."""
        from models import DRPlan, DRPhase, DRStep
        step = DRStep(
            step_id="test-step",
            order=1,
            resource_type="RDSCluster",
            resource_id="id-1",
            resource_name="test-db",
            action="promote_read_replica",
            command="aws rds promote ...",
            validation=validation_str,
            expected_result="available",
            rollback_command="aws rds ...",
            estimated_time=60,
            requires_approval=True,
            tier="Tier0",
            dependencies=[],
        )
        phase = DRPhase(
            phase_id="phase-1",
            name="Data",
            layer="L0",
            steps=[step],
            estimated_duration=1,
            gate_condition="ok",
        )
        return DRPlan(
            plan_id="test",
            created_at="2026-01-01T00:00:00Z",
            scope="az",
            source="apne1-az1",
            target="apne1-az2",
            phases=[phase],
            graph_snapshot_time="2026-01-01T00:00:00Z",
        )

    def test_echo_dollar_flagged(self):
        plan = self._make_plan_with_validation("echo $?")
        report = PlanValidator().validate(plan)
        warnings = [i for i in report.issues if "echo" in i.message.lower()]
        assert len(warnings) == 1
        assert "not meaningful" in warnings[0].message

    def test_comment_only_flagged(self):
        plan = self._make_plan_with_validation("# Just a comment")
        report = PlanValidator().validate(plan)
        warnings = [i for i in report.issues if "comment" in i.message.lower()]
        assert len(warnings) == 1

    def test_empty_validation_flagged(self):
        plan = self._make_plan_with_validation("")
        report = PlanValidator().validate(plan)
        errors = [i for i in report.issues if "empty" in i.message.lower()]
        assert len(errors) == 1
        assert errors[0].severity == "ERROR"

    def test_real_command_no_flag(self):
        plan = self._make_plan_with_validation(
            "aws rds describe-db-clusters --db-cluster-identifier test-db"
        )
        report = PlanValidator().validate(plan)
        quality_issues = [i for i in report.issues
                         if "echo" in i.message.lower()
                         or "comment" in i.message.lower()
                         or "empty" in i.message.lower()]
        assert len(quality_issues) == 0
