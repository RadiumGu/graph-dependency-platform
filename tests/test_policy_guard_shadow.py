"""
test_policy_guard_shadow.py — Shadow comparison: Direct vs Strands decision consistency.

Usage:
  cd chaos/code && PYTHONPATH=. RUN_GOLDEN=1 pytest ../../tests/test_policy_guard_shadow.py -v -s
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
def engines():
    if not os.environ.get("RUN_GOLDEN"):
        pytest.skip("RUN_GOLDEN not set")
    os.environ["POLICY_GUARD_ENGINE"] = "direct"
    from policy.factory import make_policy_guard as _make
    direct = _make()
    os.environ["POLICY_GUARD_ENGINE"] = "strands"
    # Force reimport
    import importlib
    import policy.factory as pf
    importlib.reload(pf)
    strands = pf.make_policy_guard()
    return {"direct": direct, "strands": strands}


@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_shadow_decision_consistency(case, engines):
    """Direct and Strands must produce same allow/deny decision."""
    if not os.environ.get("RUN_GOLDEN"):
        pytest.skip("RUN_GOLDEN not set")

    exp = case["experiment"]
    ctx = case.get("context")

    d_result = engines["direct"].evaluate(exp, ctx)
    s_result = engines["strands"].evaluate(exp, ctx)

    print(f"\n{'='*60}")
    print(f"Case {case['id']}: {case['scenario']}")
    print(f"  Direct:  {d_result['decision']} ({d_result.get('latency_ms',0)}ms)")
    print(f"  Strands: {s_result['decision']} ({s_result.get('latency_ms',0)}ms)")

    if d_result.get("token_usage") and s_result.get("token_usage"):
        dt = d_result["token_usage"]
        st = s_result["token_usage"]
        print(f"  Direct  tokens: in={dt.get('input',0)} out={dt.get('output',0)} "
              f"cache_r={dt.get('cache_read',0)} cache_w={dt.get('cache_write',0)}")
        print(f"  Strands tokens: in={st.get('input',0)} out={st.get('output',0)} "
              f"cache_r={st.get('cache_read',0)} cache_w={st.get('cache_write',0)}")
    print(f"{'='*60}")

    assert d_result["decision"] == s_result["decision"], (
        f"Decision mismatch! Direct={d_result['decision']}, Strands={s_result['decision']}. "
        f"Direct reasoning: {d_result['reasoning'][:150]}... "
        f"Strands reasoning: {s_result['reasoning'][:150]}"
    )
