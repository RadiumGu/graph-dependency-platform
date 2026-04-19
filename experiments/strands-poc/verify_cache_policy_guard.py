#!/usr/bin/env python3
"""
verify_cache_policy_guard.py — Verify prompt caching works for PolicyGuard.

Run 3 evaluations sequentially; cache_read should grow from run 2 onward.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "chaos", "code"))
os.environ.setdefault("POLICY_GUARD_ENGINE", "strands")

from policy.factory import make_policy_guard

SAMPLE_EXPERIMENT = {
    "name": "cache-verify-test",
    "fault_type": "pod-delete",
    "target_namespace": "petsite-staging",
    "target_service": "petsite",
    "duration_sec": 60,
    "blast_radius": "single-pod",
}
SAMPLE_CONTEXT = {
    "current_time": "2026-04-20T22:00:00+08:00",
    "environment": "staging",
    "recent_incidents": [],
    "recent_experiments": [],
}

RUNS = 3


def main():
    print("=" * 60)
    print("PolicyGuard Prompt Caching Verification")
    print(f"Engine: {os.environ.get('POLICY_GUARD_ENGINE', 'direct')}")
    print("=" * 60)

    guard = make_policy_guard()

    results = []
    for i in range(1, RUNS + 1):
        print(f"\n--- Run {i}/{RUNS} ---")
        result = guard.evaluate(SAMPLE_EXPERIMENT, SAMPLE_CONTEXT)
        results.append(result)

        tu = result.get("token_usage") or {}
        print(f"  Decision: {result['decision']}")
        print(f"  Latency: {result['latency_ms']}ms")
        print(f"  Tokens: in={tu.get('input',0)} out={tu.get('output',0)} "
              f"cache_read={tu.get('cache_read',0)} cache_write={tu.get('cache_write',0)}")

    # Summary
    print(f"\n{'='*60}")
    print("Summary:")
    any_cache = False
    for i, r in enumerate(results, 1):
        tu = r.get("token_usage") or {}
        cr = tu.get("cache_read", 0)
        cw = tu.get("cache_write", 0)
        if cr > 0 or cw > 0:
            any_cache = True
        print(f"  Run {i}: decision={r['decision']} latency={r['latency_ms']}ms "
              f"cache_read={cr} cache_write={cw}")

    if any_cache:
        print("\n✅ Prompt caching is ACTIVE")
    else:
        print("\n⚠️  No cache activity detected — may need investigation")
    print("=" * 60)


if __name__ == "__main__":
    main()
