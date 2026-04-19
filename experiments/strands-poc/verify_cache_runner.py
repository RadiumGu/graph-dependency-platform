#!/usr/bin/env python3
"""
verify_cache_runner.py — Strands Runner 缓存验证脚本。
连续 3 次运行同一实验（dry_run），观察 cache_read 是否在 run2/run3 增长。
"""
import json, os, sys, tempfile, time, yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../chaos/code"))

os.environ.setdefault("CHAOS_RUNNER_ENGINE", "strands")
os.environ.setdefault("CHAOS_RUNNER_DRY_RUN", "true")

from runner.factory import make_runner_engine
from runner.experiment import load_experiment

# Simple experiment YAML
EXP = {
    "name": "cache-verify-pod-delete",
    "target": {"service": "petsite", "namespace": "petsite-staging"},
    "fault": {"type": "pod-delete", "duration": "60s"},
    "backend": "chaosmesh",
    "steady_state": {"before": [], "after": []},
    "stop_conditions": [],
}

def main():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        yaml.dump(EXP, f)
        tmp = f.name

    exp = load_experiment(tmp)
    results = []

    for i in range(1, 4):
        print(f"\n{'='*60}")
        print(f"Run {i}/3")
        print(f"{'='*60}")
        runner = make_runner_engine(dry_run=True)
        t0 = time.time()
        result = runner.run(exp)
        elapsed = time.time() - t0

        meta = getattr(result, "engine_meta", {}) or {}
        tu = meta.get("token_usage", {})
        row = {
            "run": i,
            "status": result.status,
            "latency_s": round(elapsed, 1),
            "input_tokens": tu.get("input", 0),
            "output_tokens": tu.get("output", 0),
            "cache_read": tu.get("cache_read", 0),
            "cache_write": tu.get("cache_write", 0),
        }
        results.append(row)
        print(json.dumps(row, indent=2))
        time.sleep(2)  # Small gap between runs

    os.unlink(tmp)

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    for r in results:
        print(f"  Run {r['run']}: cache_read={r['cache_read']:,} cache_write={r['cache_write']:,} "
              f"latency={r['latency_s']}s status={r['status']}")

    # Check cache growth
    if len(results) >= 2 and results[1]["cache_read"] > results[0]["cache_read"]:
        print("\n✅ Cache ACTIVE — cache_read grew from run1 to run2")
    elif len(results) >= 3 and results[2]["cache_read"] > results[0]["cache_read"]:
        print("\n✅ Cache ACTIVE — cache_read grew from run1 to run3")
    else:
        print("\n⚠️ Cache may not be active — cache_read did not grow")

if __name__ == "__main__":
    main()
