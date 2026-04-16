"""
tests/test_19_unit_dr_supplement.py — Sprint 5 DR Plan Supplement Unit Tests

Test IDs:
  S5-06: spof_detector — SPOF detection (graph topology, mock Neptune)
  S5-07: rto_estimator — RTO/RPO estimation logic
  S5-08: impact_analyzer — business impact analysis
  S5-09: plan_validator — DR plan validation rules
  S5-10: rollback_generator — rollback steps generation
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

PROJECT_ROOT = "/home/ubuntu/tech/graph-dependency-platform"
DR_PLAN_DIR = os.path.join(PROJECT_ROOT, "dr-plan-generator")
if DR_PLAN_DIR not in sys.path:
    sys.path.insert(0, DR_PLAN_DIR)

# --- Import DR plan modules ---
try:
    from assessment.rto_estimator import RTOEstimator
    from assessment.spof_detector import SPOFDetector
    from assessment.impact_analyzer import ImpactAnalyzer
    from validation.plan_validator import PlanValidator
    from planner.rollback_generator import RollbackGenerator
    from models import DRPlan, DRPhase, DRStep, Issue, ValidationReport

    _DR_AVAILABLE = True
    _DR_IMPORT_ERROR = ""
except ImportError as _e:
    _DR_AVAILABLE = False
    _DR_IMPORT_ERROR = str(_e)


# ─── Helpers ─────────────────────────────────────────────────────────────────


def _make_step(
    step_id: str,
    order: int = 1,
    rollback_cmd: str = "echo rollback",
    validation: str = "aws sts get-caller-identity",
    dependencies: list = None,
    parallel_group: str = None,
    estimated_time: int = 60,
    resource_name: str = None,
) -> "DRStep":
    return DRStep(
        step_id=step_id,
        order=order,
        resource_type="Microservice",
        resource_id=step_id,
        resource_name=resource_name or step_id,
        action="switchover",
        command=f"echo {step_id}",
        validation=validation,
        expected_result="ok",
        rollback_command=rollback_cmd,
        estimated_time=estimated_time,
        dependencies=dependencies or [],
        parallel_group=parallel_group,
    )


def _make_plan(phases: list = None, affected_resources: list = None) -> "DRPlan":
    return DRPlan(
        plan_id="test-plan-001",
        created_at="2026-01-01T00:00:00Z",
        scope="az",
        source="ap-northeast-1a",
        target="ap-northeast-1c",
        affected_resources=affected_resources or [],
        phases=phases or [],
    )


# ─── S5-06 ───────────────────────────────────────────────────────────────────


def test_s5_06_spof_detector_from_subgraph():
    """S5-06: spof_detector — SPOF detection from subgraph topology (mock Neptune)."""
    if not _DR_AVAILABLE:
        pytest.skip(f"DR plan modules not available: {_DR_IMPORT_ERROR}")

    # Use a mock registry: DynamoDBTable and RDSCluster are SPOF candidates
    mock_registry = MagicMock()
    mock_registry.is_spof_candidate.side_effect = lambda rtype: rtype in (
        "DynamoDBTable",
        "RDSCluster",
    )
    detector = SPOFDetector(registry=mock_registry)

    subgraph = {
        "nodes": [
            {
                "name": "petadoption-db",
                "type": "DynamoDBTable",
                "az": "ap-northeast-1a",
            },
            {"name": "petsite", "type": "Microservice", "az": ""},
            {"name": "payforadoption", "type": "Microservice", "az": ""},
        ],
        "edges": [
            {"from": "petsite", "to": "petadoption-db"},
            {"from": "payforadoption", "to": "petadoption-db"},
        ],
    }

    # Call _detect_from_subgraph directly — no Neptune needed
    spofs = detector._detect_from_subgraph(subgraph)

    assert len(spofs) >= 1
    resources = [s["resource"] for s in spofs]
    assert "petadoption-db" in resources
    spof = next(s for s in spofs if s["resource"] == "petadoption-db")
    assert spof["risk"] == "single_az"
    assert spof["az"] == "ap-northeast-1a"
    assert set(spof["impact"]) == {"petsite", "payforadoption"}
    assert "recommendation" in spof

    # Non-SPOF node (Microservice) must not appear even if it has dependents
    assert "petsite" not in resources

    # Node with no az set must be excluded even if type matches
    subgraph_no_az = {
        "nodes": [{"name": "no-az-db", "type": "DynamoDBTable", "az": ""}],
        "edges": [
            {"from": "svc-a", "to": "no-az-db"},
            {"from": "svc-b", "to": "no-az-db"},
        ],
    }
    assert detector._detect_from_subgraph(subgraph_no_az) == []

    # Node with only one dependent must be excluded (not single-point-of-failure)
    subgraph_one_dep = {
        "nodes": [{"name": "solo-db", "type": "DynamoDBTable", "az": "ap-northeast-1a"}],
        "edges": [{"from": "only-svc", "to": "solo-db"}],
    }
    assert detector._detect_from_subgraph(subgraph_one_dep) == []


# ─── S5-07 ───────────────────────────────────────────────────────────────────


def test_s5_07_rto_estimator():
    """S5-07: rto_estimator — RTO and RPO estimation logic."""
    if not _DR_AVAILABLE:
        pytest.skip(f"DR plan modules not available: {_DR_IMPORT_ERROR}")

    estimator = RTOEstimator()

    # estimate_from_subgraph: sum of DEFAULT_TIMES per node type
    subgraph = {
        "nodes": [
            {"type": "RDSCluster"},     # 300 s
            {"type": "Microservice"},   # 120 s
            {"type": "DynamoDBTable"},  # 60 s
        ]
    }
    rto = estimator.estimate_from_subgraph(subgraph)
    assert rto == 8  # (300+120+60)=480 s // 60 = 8 min

    # Empty subgraph → minimum 1
    assert estimator.estimate_from_subgraph({"nodes": []}) == 1

    # Unknown type falls back to 60 s default
    rto_unknown = estimator.estimate_from_subgraph({"nodes": [{"type": "ExoticService"}]})
    assert rto_unknown == 1  # 60 s // 60 = 1 min

    # estimate() with phases: serial + parallel groups + inter-phase gate (60 s)
    phase = DRPhase(
        phase_id="phase-1",
        name="Compute",
        layer="L1",
        steps=[
            _make_step("step-serial", estimated_time=120),
            _make_step("step-par-a1", estimated_time=60, parallel_group="grp-a"),
            _make_step("step-par-a2", estimated_time=90, parallel_group="grp-a"),  # max
        ],
    )
    # serial=120, parallel_max=90, gate=60 → 270 s → 4 min (round down)
    rto_phases = estimator.estimate([phase])
    assert rto_phases == 4

    # Two phases: phase1=(120+90)+60gate + phase2=300+60gate = 210+60+300+60=630 s → 10 min
    phase2 = DRPhase(
        phase_id="phase-2",
        name="Data",
        layer="L0",
        steps=[_make_step("step-data", estimated_time=300)],
    )
    rto_two = estimator.estimate([phase, phase2])
    assert rto_two == 10  # (120+90+60 + 300+60) = 630 s → 10 min

    # Empty phases → 1
    assert estimator.estimate([]) == 1


# ─── S5-08 ───────────────────────────────────────────────────────────────────


def test_s5_08_impact_analyzer():
    """S5-08: impact_analyzer — business impact analysis."""
    if not _DR_AVAILABLE:
        pytest.skip(f"DR plan modules not available: {_DR_IMPORT_ERROR}")

    subgraph = {
        "nodes": [
            {"name": "petsite", "type": "Microservice", "tier": "Tier0"},
            {"name": "petsearch", "type": "Microservice", "tier": "Tier0"},
            {"name": "pethistory", "type": "Microservice", "tier": "Tier1"},
            {
                "name": "adoption-db",
                "type": "RDSCluster",
                "tier": None,
                "az": "ap-northeast-1a",
            },
            {"name": "PetAdoption", "type": "BusinessCapability"},
        ],
        "edges": [
            {"from": "petsite", "to": "adoption-db"},
            {"from": "petsearch", "to": "adoption-db"},
        ],
    }

    analyzer = ImpactAnalyzer()

    # Mock SPOFDetector so we don't hit Neptune
    mock_spof_instance = MagicMock()
    mock_spof_instance.detect.return_value = [
        {
            "resource": "adoption-db",
            "type": "RDSCluster",
            "risk": "single_az",
            "az": "ap-northeast-1a",
            "impact": ["petsite", "petsearch"],
            "recommendation": "Deploy multi-AZ",
        }
    ]

    with patch(
        "assessment.spof_detector.SPOFDetector", return_value=mock_spof_instance
    ):
        report = analyzer.assess_impact(
            subgraph, scope="az", source="ap-northeast-1a"
        )

    # Scope and source
    assert report.scope == "az"
    assert report.source == "ap-northeast-1a"

    # Total affected nodes
    assert report.total_affected == 5

    # Tier grouping
    assert len(report.by_tier.get("Tier0", [])) == 2
    assert len(report.by_tier.get("Tier1", [])) == 1

    # Business capabilities
    assert len(report.affected_capabilities) == 1
    assert report.affected_capabilities[0]["name"] == "PetAdoption"

    # SPOF list forwarded from mock
    assert len(report.single_points_of_failure) == 1

    # RPO: RDSCluster → 5 min
    assert report.estimated_rpo_minutes == 5

    # Risk matrix: Tier0 + SPOF → HIGH
    assert report.risk_matrix["severity"] == "HIGH"
    assert report.risk_matrix["tier0_services_affected"] == 2
    assert report.risk_matrix["single_points_of_failure"] == 1

    # LOW severity: no Tier0, no SPOF
    subgraph_low = {
        "nodes": [{"name": "petfood", "type": "Microservice", "tier": "Tier2"}],
        "edges": [],
    }
    mock_no_spof = MagicMock()
    mock_no_spof.detect.return_value = []
    with patch(
        "assessment.spof_detector.SPOFDetector", return_value=mock_no_spof
    ):
        report_low = analyzer.assess_impact(
            subgraph_low, scope="service", source="petfood"
        )
    assert report_low.risk_matrix["severity"] == "LOW"


# ─── S5-09 ───────────────────────────────────────────────────────────────────


def test_s5_09_plan_validator():
    """S5-09: plan_validator — DR plan validation rules."""
    if not _DR_AVAILABLE:
        pytest.skip(f"DR plan modules not available: {_DR_IMPORT_ERROR}")

    validator = PlanValidator()

    # ── Valid plan (old snapshot only triggers freshness WARNING, not CRITICAL) ──
    step = _make_step(
        "step-petsite-1",
        rollback_cmd="echo rollback-petsite",
        validation="aws sts get-caller-identity",
        resource_name="petsite",
    )
    phase = DRPhase(phase_id="phase-1", name="Compute", layer="L1", steps=[step])
    plan = _make_plan(phases=[phase], affected_resources=["petsite"])
    plan.graph_snapshot_time = "2026-01-01T00:00:00Z"  # very old → freshness warning

    report = validator.validate(plan)
    assert isinstance(report, ValidationReport)
    assert report.valid  # no CRITICAL issues
    assert all(i.severity != "CRITICAL" for i in report.issues)

    # ── Cycle detection: A → B → A ──
    step_a = _make_step("step-a", dependencies=["step-b"])
    step_b = _make_step("step-b", dependencies=["step-a"])
    phase_cycle = DRPhase(
        phase_id="phase-cycle", name="Cycle", layer="L1", steps=[step_a, step_b]
    )
    plan_cycle = _make_plan(phases=[phase_cycle])
    report_cycle = validator.validate(plan_cycle)
    assert not report_cycle.valid
    assert any(i.severity == "CRITICAL" for i in report_cycle.issues)
    assert any("cycle" in i.message.lower() for i in report_cycle.issues)

    # ── Missing rollback → WARNING ──
    step_no_rb = _make_step("step-norb", rollback_cmd="")
    phase_norb = DRPhase(
        phase_id="phase-norb", name="NoRollback", layer="L1", steps=[step_no_rb]
    )
    plan_norb = _make_plan(phases=[phase_norb])
    report_norb = validator.validate(plan_norb)
    assert any(i.severity == "WARNING" for i in report_norb.issues)
    assert any("rollback" in i.message.lower() for i in report_norb.issues)

    # ── Empty validation → ERROR ──
    step_empty = _make_step("step-empty-val", validation="")
    phase_empty = DRPhase(
        phase_id="phase-emptyval", name="EmptyVal", layer="L1", steps=[step_empty]
    )
    plan_empty = _make_plan(phases=[phase_empty])
    report_empty = validator.validate(plan_empty)
    assert any(i.severity == "ERROR" for i in report_empty.issues)

    # ── Ordering violation: dep appears after dependent ──
    step_first = _make_step("step-first", order=1)
    step_second = _make_step("step-second", order=2, dependencies=["step-first"])
    # Put second before first in the phase steps list
    phase_order = DRPhase(
        phase_id="phase-order",
        name="BadOrder",
        layer="L1",
        steps=[step_second, step_first],  # wrong order
    )
    plan_order = _make_plan(phases=[phase_order])
    report_order = validator.validate(plan_order)
    assert not report_order.valid
    assert any("ordering" in i.message.lower() or "scheduled" in i.message.lower()
               for i in report_order.issues)

    # ── Completeness: affected_resources not covered by any step ──
    step_covered = _make_step("step-covered", resource_name="petsite")
    phase_incomplete = DRPhase(
        phase_id="phase-incomplete",
        name="Incomplete",
        layer="L1",
        steps=[step_covered],
    )
    plan_incomplete = _make_plan(
        phases=[phase_incomplete],
        affected_resources=["petsite", "petsearch"],  # petsearch not covered
    )
    report_incomplete = validator.validate(plan_incomplete)
    assert any(
        "petsearch" in i.message and i.severity == "WARNING"
        for i in report_incomplete.issues
    )


# ─── S5-10 ───────────────────────────────────────────────────────────────────


def test_s5_10_rollback_generator():
    """S5-10: rollback_generator — rollback steps generation."""
    if not _DR_AVAILABLE:
        pytest.skip(f"DR plan modules not available: {_DR_IMPORT_ERROR}")

    step_data = _make_step(
        "step-data-1", rollback_cmd="echo rollback-data", estimated_time=300
    )
    step_compute = _make_step(
        "step-compute-1", rollback_cmd="echo rollback-compute", estimated_time=120
    )
    phase_data = DRPhase(
        phase_id="phase-data", name="Data Layer", layer="L0", steps=[step_data]
    )
    phase_compute = DRPhase(
        phase_id="phase-compute", name="Compute Layer", layer="L1", steps=[step_compute]
    )

    plan = _make_plan(phases=[phase_data, phase_compute])
    plan.source = "ap-northeast-1a"

    generator = RollbackGenerator()
    rollback_phases = generator.generate_rollback(plan)

    # At least reversed phases + validation phase
    assert len(rollback_phases) >= 3  # data→compute reversed + validation

    # Last phase is always rollback-validation
    last_phase = rollback_phases[-1]
    assert last_phase.phase_id == "rollback-phase-validation"
    assert last_phase.layer == "validation"
    assert len(last_phase.steps) == 1
    assert last_phase.steps[0].requires_approval is True

    # All rollback steps are prefixed with "rollback-" and require approval
    for phase in rollback_phases[:-1]:
        for step in phase.steps:
            assert step.step_id.startswith("rollback-")
            assert step.requires_approval is True

    # Reversed order: compute (L1) should come before data (L0) in rollback
    non_validation = [
        p for p in rollback_phases if p.phase_id != "rollback-phase-validation"
    ]
    assert len(non_validation) == 2
    # Original: [data, compute] → reversed: [compute, data]
    assert "compute" in non_validation[0].phase_id
    assert "data" in non_validation[1].phase_id

    # Rollback step command comes from original step's rollback_command
    compute_rollback_phase = non_validation[0]
    assert compute_rollback_phase.steps[0].command == "echo rollback-compute"

    # Preflight and validation phases are excluded from reversal
    step_preflight = _make_step("step-pre")
    phase_preflight = DRPhase(
        phase_id="phase-preflight", name="Preflight", layer="preflight",
        steps=[step_preflight]
    )
    plan_with_preflight = _make_plan(
        phases=[phase_preflight, phase_data, phase_compute]
    )
    plan_with_preflight.source = "ap-northeast-1a"
    rollback_with_preflight = generator.generate_rollback(plan_with_preflight)
    phase_ids = [p.phase_id for p in rollback_with_preflight]
    assert "rollback-phase-preflight" not in phase_ids
