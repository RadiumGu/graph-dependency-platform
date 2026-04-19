"""
test_learning_golden.py — LearningAgent Golden Set CI test.

Engine matrix: parametrize over direct / strands.
Scenarios from tests/golden/learning/cases.yaml.

Usage:
  # Unit test (no real LLM calls):
  PYTHONPATH=rca:chaos/code pytest tests/test_learning_golden.py -v -k "not goldenreal"

  # Golden CI (real Bedrock + DynamoDB, has cost):
  PYTHONPATH=rca:chaos/code RUN_GOLDEN=1 LEARNING_ENGINE=direct pytest tests/test_learning_golden.py -v
  PYTHONPATH=rca:chaos/code RUN_GOLDEN=1 LEARNING_ENGINE=strands pytest tests/test_learning_golden.py -v
"""
from __future__ import annotations

import json
import os
import sys
import pytest
import yaml

# Path setup
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
_RCA = os.path.join(_PROJ, "rca")
_CHAOS = os.path.join(_PROJ, "chaos", "code")
for p in (_RCA, _CHAOS):
    if p not in sys.path:
        sys.path.insert(0, p)

FIXTURES_DIR = os.path.join(_PROJ, "experiments", "strands-poc", "fixtures")
CASES_PATH = os.path.join(_HERE, "golden", "learning", "cases.yaml")

RUN_GOLDEN = os.environ.get("RUN_GOLDEN", "").lower() in ("1", "true", "yes")
ENGINE = os.environ.get("LEARNING_ENGINE", "direct").lower()


def load_cases():
    with open(CASES_PATH) as f:
        return yaml.safe_load(f)


def load_fixture(fixture_file: str) -> list[dict]:
    path = os.path.join(FIXTURES_DIR, fixture_file)
    with open(path) as f:
        data = json.load(f)
    return data.get("experiments", [])


def get_engine():
    from engines.factory import make_learning_engine
    os.environ["LEARNING_ENGINE"] = ENGINE
    return make_learning_engine()


CASES = load_cases()
CASE_IDS = [c["id"] for c in CASES]


@pytest.fixture(scope="module")
def engine():
    return get_engine()


# ── analyze tests (no LLM, always run) ──────────────────────────────

@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_analyze(case, engine):
    """Test analyze() — pure Python, no LLM cost."""
    experiments = load_fixture(case["fixture"])
    result = engine.analyze(experiments)

    assert "engine" in result
    assert "coverage" in result
    assert "gaps" in result
    assert result["error"] is None

    if case.get("expect_empty_analysis"):
        assert result.get("report") is not None
        report = result["report"]
        assert report.total_experiments == 0
    else:
        assert len(experiments) > 0
        report = result["report"]
        assert report.total_experiments == len(experiments)


# ── generate_recommendations tests (LLM, only with RUN_GOLDEN) ─────

@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
def test_goldenreal_recommendations(case, engine):
    """Test generate_recommendations() against golden constraints."""
    if not RUN_GOLDEN:
        pytest.skip("RUN_GOLDEN not set")

    experiments = load_fixture(case["fixture"])
    analysis = engine.analyze(experiments)

    if case.get("expect_empty_analysis"):
        # Empty input should produce no recommendations
        # (generate_recommendations may not even be called for empty)
        return

    rec_result = engine.generate_recommendations(analysis)

    recs = rec_result.get("recommendations", [])
    rec_count = len(recs)

    # Count constraint
    assert rec_count >= case["min_recommendations"], (
        f"{case['id']}: got {rec_count} recs, expected >= {case['min_recommendations']}"
    )
    assert rec_count <= case["max_recommendations"], (
        f"{case['id']}: got {rec_count} recs, expected <= {case['max_recommendations']}"
    )

    # Dimension constraint
    dim_must = case.get("dimension_must_include_any", [])
    if dim_must and recs:
        categories = {r.get("category", "") for r in recs}
        assert categories & set(dim_must), (
            f"{case['id']}: categories {categories} must include one of {dim_must}"
        )

    # Must-not-include constraint
    must_not = case.get("recommendation_must_not_include", [])
    if must_not:
        all_text = " ".join(
            str(r.get("title", "")) + " " + str(r.get("description", ""))
            for r in recs
        ).lower()
        for forbidden in must_not:
            assert forbidden.lower() not in all_text, (
                f"{case['id']}: found forbidden term '{forbidden}' in recommendations"
            )

    # Token usage (should be present for real calls)
    if ENGINE == "strands":
        assert rec_result.get("token_usage") is not None, (
            f"{case['id']}: strands engine should report token_usage"
        )

    # Engine label
    assert rec_result["engine"] == ENGINE


# ── generate_report test ─────────────────────────────────────────────

def test_generate_report(engine):
    """Test report generation with a sample fixture."""
    experiments = load_fixture("coverage_snapshot_l001.json")
    analysis = engine.analyze(experiments)
    report_result = engine.generate_report(analysis)
    assert "report_md" in report_result
    assert "# 混沌工程学习报告" in report_result["report_md"]
    assert report_result["engine"] in ("direct", "strands")
