"""
verify_cache_layer2.py — 验证 Layer2 Strands Orchestrator 的 prompt caching。
"""
from __future__ import annotations

import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJECT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_RCA = os.path.join(_PROJECT, "rca")
for p in [_PROJECT, _RCA]:
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ.setdefault("NEPTUNE_HOST",
    "petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com")
os.environ.setdefault("REGION", "ap-northeast-1")
os.environ.setdefault("BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6")
os.environ["LAYER2_ENGINE"] = "strands"


def main():
    from engines.factory import make_layer2_engine

    print("=" * 60)
    print("Layer2 Prober Prompt Caching Verification")
    print("=" * 60)

    engine = make_layer2_engine()
    print(f"Engine: {engine.ENGINE_NAME}")

    signal = {
        "alarm_name": "cache-test",
        "alarm_type": "Manual",
        "neptune_infra_fault": True,
    }

    results = []
    for i in range(3):
        print(f"\n--- Run {i+1}/3 ---")
        t0 = time.time()
        result = engine.run_probes(signal, "petsite", timeout_sec=90)
        elapsed = time.time() - t0

        tu = result.get("token_usage") or {}
        cw = tu.get("cache_write", 0)
        cr = tu.get("cache_read", 0)

        print(f"  Latency: {elapsed:.1f}s")
        print(f"  Token usage: {tu}")
        print(f"  cache_write: {cw}")
        print(f"  cache_read:  {cr}")
        print(f"  Probes: {len(result.get('probe_results', []))}")
        print(f"  Score: {result.get('score_delta', 0)}")

        results.append({
            "run": i + 1,
            "latency_s": round(elapsed, 1),
            "token_usage": tu,
            "probe_count": len(result.get("probe_results", [])),
            "score_delta": result.get("score_delta", 0),
        })

    print("\n" + "=" * 60)
    print("VERIFICATION SUMMARY")
    print("=" * 60)

    r1 = results[0]["token_usage"]
    r2 = results[1]["token_usage"]
    r3 = results[2]["token_usage"]

    checks = []
    if r1.get("cache_write", 0) > 0:
        print(f"✅ Run 1: cache_write={r1['cache_write']} > 0")
        checks.append(True)
    else:
        print(f"❌ Run 1: cache_write={r1.get('cache_write', 0)} == 0")
        checks.append(False)

    if r2.get("cache_read", 0) > 0:
        print(f"✅ Run 2: cache_read={r2['cache_read']} > 0")
        checks.append(True)
    else:
        print(f"❌ Run 2: cache_read={r2.get('cache_read', 0)} == 0")
        checks.append(False)

    if r3.get("cache_read", 0) > 0:
        print(f"✅ Run 3: cache_read={r3['cache_read']} > 0")
        checks.append(True)
    else:
        print(f"❌ Run 3: cache_read={r3.get('cache_read', 0)} == 0")
        checks.append(False)

    out_path = os.path.join(_HERE, "cache_verification_layer2.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nResults saved to {out_path}")

    if not all(checks):
        print("\n⚠️ Some cache checks failed. Review token_usage output.")
        sys.exit(1)


if __name__ == "__main__":
    main()
