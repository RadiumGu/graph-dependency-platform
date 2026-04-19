"""
verify_cache_learning.py — Prompt Caching verification for LearningAgent.

Uses verify_cache_common.py harness if available, otherwise standalone.

Usage:
  PYTHONPATH=rca:chaos/code python3 experiments/strands-poc/verify_cache_learning.py
"""
from __future__ import annotations

import json
import os
import sys
import time

_PROJ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for p in (os.path.join(_PROJ, "rca"), os.path.join(_PROJ, "chaos", "code")):
    if p not in sys.path:
        sys.path.insert(0, p)

FIXTURES_DIR = os.path.join(_PROJ, "experiments", "strands-poc", "fixtures")


def load_sample_analysis():
    """Load l001 fixture and analyze it."""
    fixture_path = os.path.join(FIXTURES_DIR, "coverage_snapshot_l001.json")
    with open(fixture_path) as f:
        data = json.load(f)
    experiments = data.get("experiments", [])

    os.environ["LEARNING_ENGINE"] = "strands"
    from engines.factory import make_learning_engine
    engine = make_learning_engine()
    analysis = engine.analyze(experiments)
    return engine, analysis


def main():
    print("=" * 60)
    print("LearningAgent Prompt Caching Verification")
    print("=" * 60)

    engine, analysis = load_sample_analysis()
    print(f"Engine: {engine.ENGINE_NAME}")
    print(f"System prompt: {len(engine.system_prompt)} chars")

    results = []
    for i in range(3):
        print(f"\n--- Run {i+1}/3 ---")
        t0 = time.time()
        rec_result = engine.generate_recommendations(analysis)
        elapsed = time.time() - t0

        tokens = rec_result.get("token_usage") or {}
        recs = rec_result.get("recommendations", [])
        cache_read = tokens.get("cache_read", 0)
        cache_write = tokens.get("cache_write", 0)

        print(f"  Recommendations: {len(recs)}")
        print(f"  Latency: {elapsed:.1f}s")
        print(f"  Token usage: {tokens}")
        print(f"  cache_write: {cache_write}")
        print(f"  cache_read:  {cache_read}")

        results.append({
            "run": i + 1,
            "recs": len(recs),
            "latency_s": round(elapsed, 1),
            "cache_write": cache_write,
            "cache_read": cache_read,
            "token_usage": tokens,
        })

    print("\n" + "=" * 60)
    print("VERIFICATION SUMMARY")
    print("=" * 60)

    # Check: run 1 should have cache_write > 0
    r1 = results[0]
    if r1["cache_write"] > 0:
        print(f"✅ Run 1: cache_write={r1['cache_write']} > 0")
    else:
        print(f"⚠️ Run 1: cache_write={r1['cache_write']} (expected > 0)")

    # Check: run 2/3 should have cache_read > 0
    for r in results[1:]:
        if r["cache_read"] > 0:
            print(f"✅ Run {r['run']}: cache_read={r['cache_read']} > 0")
        else:
            print(f"❌ Run {r['run']}: cache_read={r['cache_read']} (expected > 0)")

    # Save results
    output_path = os.path.join(os.path.dirname(__file__), "cache_verification_learning.json")
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {output_path}")


if __name__ == "__main__":
    main()
