#!/usr/bin/env python3
"""verify_cache_dr_executor.py — DR Executor Strands 缓存验证。"""
import json, os, sys, time, yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../dr-plan-generator"))
os.environ.setdefault("DR_EXECUTOR_ENGINE", "strands")
os.environ.setdefault("DR_EXECUTOR_DRY_RUN", "true")

from executor_factory import make_dr_executor
from models import DRPlan
from validation.verification_models import VerificationLevel

PLAN = {
    "plan_id": "cache-verify-az",
    "created_at": "2026-04-20T00:00:00Z",
    "scope": "az", "source": "ap-northeast-1a", "target": "ap-northeast-1c",
    "affected_services": ["petsite"], "affected_resources": ["aurora-1"],
    "estimated_rto": 10, "estimated_rpo": 5,
    "validation_status": "valid", "graph_snapshot_time": "2026-04-20T00:00:00Z",
    "phases": [{
        "phase_id": "phase-1", "name": "Pre-flight", "layer": "preflight",
        "estimated_duration": 2, "gate_condition": "all_pass",
        "steps": [{
            "step_id": "s-001", "order": 1, "resource_type": "health",
            "resource_id": "check", "resource_name": "Health",
            "action": "verify", "command": "echo ok", "validation": "echo ok",
            "expected_result": "ok", "rollback_command": "echo rollback",
            "estimated_time": 10, "requires_approval": False, "dependencies": []
        }]
    }],
    "rollback_phases": []
}

def main():
    plan = DRPlan.from_dict(PLAN)
    results = []
    for i in range(1, 4):
        print(f"\n{'='*60}\nRun {i}/3\n{'='*60}")
        executor = make_dr_executor(dry_run=True)
        t0 = time.time()
        report = executor.execute(plan, level=VerificationLevel.DRY_RUN)
        elapsed = time.time() - t0
        meta = getattr(report, "engine_meta", {}) or {}
        tu = meta.get("token_usage", {})
        row = {"run": i, "latency_s": round(elapsed, 1),
               "cache_read": tu.get("cache_read", 0), "cache_write": tu.get("cache_write", 0)}
        results.append(row)
        print(json.dumps(row, indent=2))
        time.sleep(2)

    print(f"\n{'='*60}\nSUMMARY\n{'='*60}")
    for r in results:
        print(f"  Run {r['run']}: cache_read={r['cache_read']:,} cache_write={r['cache_write']:,} latency={r['latency_s']}s")

if __name__ == "__main__":
    main()
