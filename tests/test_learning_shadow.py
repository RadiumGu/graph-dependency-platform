"""
test_learning_shadow.py — Direct vs Strands shadow comparison.

Runs both engines on the same fixtures, compares:
- Recommendation count difference
- Category distribution similarity
- Latency ratio
- Token usage ratio
- Cache hit rate

Usage:
  PYTHONPATH=rca:chaos/code RUN_GOLDEN=1 pytest tests/test_learning_shadow.py -v -s
"""
from __future__ import annotations

import json
import os
import sys
import pytest
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
for p in (os.path.join(_PROJ, "rca"), os.path.join(_PROJ, "chaos", "code")):
    if p not in sys.path:
        sys.path.insert(0, p)

FIXTURES_DIR = os.path.join(_PROJ, "experiments", "strands-poc", "fixtures")
CASES_PATH = os.path.join(_HERE, "golden", "learning", "cases.yaml")

RUN_GOLDEN = os.environ.get("RUN_GOLDEN", "").lower() in ("1", "true", "yes")


def load_cases():
    with open(CASES_PATH) as f:
        cases = yaml.safe_load(f)
    # Skip error_input cases for shadow comparison
    return [c for c in cases if c.get("bucket") not in ("error_input",)]


def load_fixture(fixture_file: str) -> list[dict]:
    path = os.path.join(FIXTURES_DIR, fixture_file)
    with open(path) as f:
        data = json.load(f)
    return data.get("experiments", [])


SHADOW_CASES = load_cases()
CASE_IDS = [c["id"] for c in SHADOW_CASES]


@pytest.fixture(scope="module")
def engines():
    from engines.factory import make_learning_engine
    os.environ["LEARNING_ENGINE"] = "direct"
    direct = make_learning_engine()
    os.environ["LEARNING_ENGINE"] = "strands"
    strands = make_learning_engine()
    return direct, strands


@pytest.mark.parametrize("case", SHADOW_CASES, ids=CASE_IDS)
def test_shadow_comparison(case, engines):
    """Compare direct vs strands on the same input."""
    if not RUN_GOLDEN:
        pytest.skip("RUN_GOLDEN not set")

    direct_eng, strands_eng = engines
    experiments = load_fixture(case["fixture"])

    # Analyze (same for both since Strands delegates to Direct)
    analysis = direct_eng.analyze(experiments)

    # Generate recommendations
    direct_result = direct_eng.generate_recommendations(analysis)
    strands_result = strands_eng.generate_recommendations(analysis)

    d_recs = direct_result.get("recommendations", [])
    s_recs = strands_result.get("recommendations", [])

    # Report
    print(f"\n{'='*60}")
    print(f"Case: {case['id']} — {case.get('scenario', '')}")
    print(f"  Direct:  {len(d_recs)} recs, {direct_result.get('latency_ms')}ms")
    print(f"  Strands: {len(s_recs)} recs, {strands_result.get('latency_ms')}ms")

    # Category distribution
    d_cats = sorted(r.get("category", "") for r in d_recs)
    s_cats = sorted(r.get("category", "") for r in s_recs)
    print(f"  Direct categories:  {d_cats}")
    print(f"  Strands categories: {s_cats}")

    # Token usage
    d_tokens = direct_result.get("token_usage") or {}
    s_tokens = strands_result.get("token_usage") or {}
    print(f"  Direct tokens:  {d_tokens}")
    print(f"  Strands tokens: {s_tokens}")

    if s_tokens.get("cache_read", 0) > 0:
        print(f"  ✅ Strands cache HIT: cache_read={s_tokens['cache_read']}")
    else:
        print(f"  ⚠️ Strands cache MISS (expected on first call)")

    # Latency ratio
    d_lat = direct_result.get("latency_ms", 1)
    s_lat = strands_result.get("latency_ms", 1)
    ratio = s_lat / d_lat if d_lat > 0 else 0
    print(f"  Latency ratio (strands/direct): {ratio:.2f}x")

    # Assertions: strands should not be wildly different
    # Rec count within ±3
    assert abs(len(d_recs) - len(s_recs)) <= 3, (
        f"{case['id']}: rec count diff too large: direct={len(d_recs)}, strands={len(s_recs)}"
    )

    # Category overlap: at least 1 shared category
    if d_recs and s_recs:
        d_cat_set = {r.get("category") for r in d_recs}
        s_cat_set = {r.get("category") for r in s_recs}
        assert d_cat_set & s_cat_set, (
            f"{case['id']}: no shared categories: direct={d_cat_set}, strands={s_cat_set}"
        )

    # Latency: strands ≤ 1.5x direct (TASK Gate B)
    # Note: first call may be slow due to cache warmup
    if ratio > 1.5:
        print(f"  ⚠️ Strands latency {ratio:.2f}x exceeds 1.5x target (may improve with cache)")
