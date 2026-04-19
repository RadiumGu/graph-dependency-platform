"""
test_layer2_memory.py — Memory pressure test for Layer2 Probers.

Validates that Strands Orchestrator + tools stays under 2 GB memory.

Usage:
  cd rca && PYTHONPATH=.:.. pytest ../tests/test_layer2_memory.py -v -s
"""
from __future__ import annotations

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, ".."))
_RCA = os.path.join(_PROJECT, "rca")
for p in [_PROJECT, _RCA]:
    if p not in sys.path:
        sys.path.insert(0, p)


def _get_rss_mb() -> float:
    """Get current process RSS in MB."""
    import resource
    # getrusage returns KB on Linux
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_maxrss / 1024  # KB -> MB


def test_strands_memory_under_2gb():
    """Constructing Strands Layer2 engine should stay under 2 GB."""
    import gc
    gc.collect()
    baseline_mb = _get_rss_mb()
    print(f"\nBaseline RSS: {baseline_mb:.0f} MB")

    os.environ["LAYER2_ENGINE"] = "strands"
    from engines.factory import make_layer2_engine

    engine = make_layer2_engine()
    assert engine.ENGINE_NAME == "strands"

    gc.collect()
    after_mb = _get_rss_mb()
    delta_mb = after_mb - baseline_mb
    print(f"After Strands construction: {after_mb:.0f} MB (delta: {delta_mb:.0f} MB)")

    # The engine + tools should add < 500 MB
    assert delta_mb < 500, f"Strands engine added {delta_mb:.0f} MB, exceeding 500 MB budget"
    assert after_mb < 2048, f"Total RSS {after_mb:.0f} MB exceeds 2 GB limit"


def test_direct_memory_baseline():
    """Direct engine should be very lightweight."""
    import gc
    gc.collect()
    baseline_mb = _get_rss_mb()

    os.environ["LAYER2_ENGINE"] = "direct"
    from engines.factory import make_layer2_engine

    engine = make_layer2_engine()
    assert engine.ENGINE_NAME == "direct"

    gc.collect()
    after_mb = _get_rss_mb()
    delta_mb = after_mb - baseline_mb
    print(f"\nDirect engine: {after_mb:.0f} MB (delta: {delta_mb:.0f} MB)")

    # Direct should add almost nothing
    assert delta_mb < 100, f"Direct engine added {delta_mb:.0f} MB, unexpected"
