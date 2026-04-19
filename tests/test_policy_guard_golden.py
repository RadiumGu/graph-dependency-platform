"""
test_policy_guard_golden.py — Golden CI tests for PolicyGuard (engine matrix).

Usage:
  # Unit tests (no LLM):
  cd chaos/code && PYTHONPATH=. pytest ../../tests/test_policy_guard_golden.py -v -k "not goldenreal"

  # Golden CI (real Bedrock):
  cd chaos/code && PYTHONPATH=. RUN_GOLDEN=1 POLICY_GUARD_ENGINE=direct  pytest ../../tests/test_policy_guard_golden.py -v -s
  cd chaos/code && PYTHONPATH=. RUN_GOLDEN=1 POLICY_GUARD_ENGINE=strands pytest ../../tests/test_policy_guard_golden.py -v -s
"""
from __future__ import annotations

import os
import sys

import pytest
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, ".."))
_CHAOS = os.path.join(_PROJECT, "chaos", "code")
for p in [_PROJECT, _CHAOS]:
    if p not in sys.path:
        sys.path.insert(0, p)

_GOLDEN_DIR = os.path.join(_HERE, "golden", "policy_guard")
with open(os.path.join(_GOLDEN_DIR, "cases.yaml")) as f:
    _CASES = yaml.safe_load(f)["cases"]


@pytest.fixture(scope="module")
def engine():
    from policy.factory import make_policy_guard
    return make_policy_guard()


# ── Unit tests ──
@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_evaluate_format(case, engine):
    """Validate engine construction and interface."""
    assert hasattr(engine, "evaluate")
    assert engine.ENGINE_NAME in ("direct", "strands")


# ── Golden CI ──
@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_goldenreal_evaluate(case, engine):
    """Run real LLM evaluation. Requires RUN_GOLDEN=1."""
    if not os.environ.get("RUN_GOLDEN"):
        pytest.skip("RUN_GOLDEN not set")

    result = engine.evaluate(
        experiment=case["experiment"],
        context=case.get("context"),
    )

    # ── Structure ──
    assert "decision" in result
    assert "reasoning" in result
    assert "engine" in result
    assert result["engine"] == engine.ENGINE_NAME
    assert result["decision"] in ("allow", "deny")

    # ── Decision ──
    expected = case["expected_decision"]
    assert result["decision"] == expected, (
        f"Expected {expected}, got {result['decision']}. "
        f"Reasoning: {result['reasoning'][:200]}"
    )

    # ── Matched rules (if specified) ──
    if "must_match_rules" in case:
        matched = result.get("matched_rules", [])
        for rule_id in case["must_match_rules"]:
            assert rule_id in matched, (
                f"Expected rule {rule_id} in matched_rules, got {matched}. "
                f"Reasoning: {result['reasoning'][:200]}"
            )

    # ── Print ──
    print(f"\n{'='*60}")
    print(f"Case: {case['id']} | Decision: {result['decision']} | Expected: {expected}")
    print(f"Matched rules: {result.get('matched_rules', [])}")
    print(f"Reasoning: {result['reasoning'][:150]}")
    if result.get("token_usage"):
        tu = result["token_usage"]
        print(f"Tokens: in={tu.get('input',0)} out={tu.get('output',0)} "
              f"cache_read={tu.get('cache_read',0)} cache_write={tu.get('cache_write',0)}")
    print(f"Latency: {result.get('latency_ms', 0)}ms")
    print(f"{'='*60}")
