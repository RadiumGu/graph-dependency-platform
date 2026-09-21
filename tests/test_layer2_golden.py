"""
test_layer2_golden.py — Golden CI tests for Layer2 Probers (engine matrix).

Usage:
  # Unit tests (no LLM / AWS):
  cd rca && PYTHONPATH=.:.. pytest ../tests/test_layer2_golden.py -v -k "not goldenreal"

  # Golden CI (real Bedrock + AWS APIs):
  cd rca && PYTHONPATH=.:.. RUN_GOLDEN=1 LAYER2_ENGINE=direct  # ← 已失效，direct 已删除  pytest ../tests/test_layer2_golden.py -v
  cd rca && PYTHONPATH=.:.. RUN_GOLDEN=1 LAYER2_ENGINE=strands pytest ../tests/test_layer2_golden.py -v

⚠️ 2026-09-21：本文件头原先写的运行命令带 `LAYER2_ENGINE=direct`，
而 direct 实现已于当天全部删除 —— 照那条命令跑会以为在测 direct，
实际拿到的仍是 strands（factory 已去掉回退分支，只有一种实现）。

七套 golden 的跑法现在收在**唯一权威**的
`scripts/run_golden_suite.sh` 里（各自 cd 的目录与 PYTHONPATH 都不同，
七份各自维护的命令必然漂移，这次就漂了）：

    bash scripts/run_golden_suite.sh layer2   # 只跑本套
    bash scripts/run_golden_suite.sh --list    # 看全部套件
    bash scripts/run_golden_suite.sh           # 全跑（约 $3-5、10-15 分钟）
"""
from __future__ import annotations

import os
import sys
import time

import pytest
import yaml

# ── Path setup ─────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, ".."))
_RCA = os.path.join(_PROJECT, "rca")
for p in [_PROJECT, _RCA]:
    if p not in sys.path:
        sys.path.insert(0, p)

# ── Load golden cases ──────────────────────────────────
_GOLDEN_DIR = os.path.join(_HERE, "golden", "layer2")
with open(os.path.join(_GOLDEN_DIR, "cases.yaml")) as f:
    _CASES = yaml.safe_load(f)["cases"]


# ── Fixtures ───────────────────────────────────────────
@pytest.fixture(scope="module")
def engine():
    from engines.factory import make_layer2_engine
    return make_layer2_engine()


# ── Unit tests (analyze return format, no real calls) ──
@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_run_probes_format(case, engine):
    """Validate run_probes() return format without real AWS calls."""
    # We just check engine construction and format — skip real calls
    assert hasattr(engine, "run_probes")
    assert hasattr(engine, "run_single_probe")
    assert hasattr(engine, "format_probe_results")
    assert hasattr(engine, "total_score_delta")
    assert engine.ENGINE_NAME in ("direct", "strands")


# ── Golden CI (real AWS + optional Bedrock) ────────────
@pytest.mark.parametrize("case", _CASES, ids=[c["id"] for c in _CASES])
def test_goldenreal_probes(case, engine):
    """Run real probes against AWS APIs. Requires RUN_GOLDEN=1."""
    if not os.environ.get("RUN_GOLDEN"):
        pytest.skip("RUN_GOLDEN not set")

    t0 = time.time()
    result = engine.run_probes(
        signal=case["signal"],
        affected_service=case["affected_service"],
        timeout_sec=60 if engine.ENGINE_NAME == "strands" else 15,
    )
    elapsed = time.time() - t0

    # ── Structure assertions ──
    assert "probe_results" in result
    assert "summary" in result
    assert "score_delta" in result
    assert "engine" in result
    assert result["engine"] == engine.ENGINE_NAME

    probe_results = result["probe_results"]
    score_delta = result["score_delta"]

    # ── Behavioral assertions ──
    min_probes = case.get("min_probes_returning", 0)
    max_probes = case.get("max_probes_returning", 10)
    assert min_probes <= len(probe_results) <= max_probes, (
        f"Expected {min_probes}-{max_probes} probes, got {len(probe_results)}"
    )

    min_score = case.get("min_score_delta", 0)
    max_score = case.get("max_score_delta", 40)
    assert min_score <= score_delta <= max_score, (
        f"Expected score {min_score}-{max_score}, got {score_delta}"
    )

    if case.get("summary_not_empty"):
        assert result["summary"], "Summary should not be empty"

    if case.get("should_not_crash"):
        pass  # If we got here, it didn't crash

    # ── Print for visibility ──
    print(f"\n{'='*60}")
    print(f"Case: {case['id']} ({case['scenario']})")
    print(f"Engine: {result['engine']} | Probes: {len(probe_results)} | Score: {score_delta}")
    print(f"Latency: {elapsed:.1f}s | Summary: {result['summary'][:100]}")
    if result.get("token_usage"):
        tu = result["token_usage"]
        print(f"Tokens: in={tu.get('input',0)} out={tu.get('output',0)} "
              f"cache_read={tu.get('cache_read',0)} cache_write={tu.get('cache_write',0)}")
    print(f"{'='*60}")
