"""
test_layer2_shadow.py — Direct vs Strands shadow comparison for Layer2 Probers.

Usage:
  cd rca && PYTHONPATH=.:.. RUN_GOLDEN=1 pytest ../tests/test_layer2_shadow.py -v -s
"""
from __future__ import annotations

import os
import sys
import time

import pytest
import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, ".."))
_RCA = os.path.join(_PROJECT, "rca")
for p in [_PROJECT, _RCA]:
    if p not in sys.path:
        sys.path.insert(0, p)

_GOLDEN_DIR = os.path.join(_HERE, "golden", "layer2")
with open(os.path.join(_GOLDEN_DIR, "cases.yaml")) as f:
    _CASES = yaml.safe_load(f)["cases"]


def _make_engine(engine_name: str):
    os.environ["LAYER2_ENGINE"] = engine_name
    from engines.factory import make_layer2_engine
    return make_layer2_engine()


@pytest.mark.parametrize("case", _CASES[:4], ids=[c["id"] for c in _CASES[:4]])
def test_shadow_comparison(case):
    """Compare direct vs strands on the same input (first 4 cases)."""
    if not os.environ.get("RUN_GOLDEN"):
        pytest.skip("RUN_GOLDEN not set")

    direct = _make_engine("direct")
    strands = _make_engine("strands")

    signal = case["signal"]
    service = case["affected_service"]

    t0 = time.time()
    d_result = direct.run_probes(signal, service, timeout_sec=15)
    d_time = time.time() - t0

    t0 = time.time()
    s_result = strands.run_probes(signal, service, timeout_sec=60)
    s_time = time.time() - t0

    print(f"\n{'='*70}")
    print(f"Case: {case['id']} | Service: {service}")
    print(f"{'─'*70}")
    print(f"Direct:  probes={len(d_result['probe_results'])} score={d_result['score_delta']} "
          f"latency={d_time:.1f}s")
    print(f"Strands: probes={len(s_result['probe_results'])} score={s_result['score_delta']} "
          f"latency={s_time:.1f}s")

    if s_result.get("token_usage"):
        tu = s_result["token_usage"]
        print(f"Strands tokens: in={tu.get('input',0)} out={tu.get('output',0)} "
              f"cache_read={tu.get('cache_read',0)} cache_write={tu.get('cache_write',0)}")

    latency_ratio = s_time / max(d_time, 0.001)
    print(f"Latency ratio: {latency_ratio:.1f}x")
    print(f"{'='*70}")

    # Strands should not be more than 5x slower (generous for multi-agent)
    assert latency_ratio < 5.0, f"Strands too slow: {latency_ratio:.1f}x"
