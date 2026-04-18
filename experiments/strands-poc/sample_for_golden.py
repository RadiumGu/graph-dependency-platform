#!/usr/bin/env python3
"""sample_for_golden.py — 用 direct 引擎跑 scenarios.yaml 采样，输出 cases.yaml 草稿。

每个场景默认跑 3 次，统计 fault_type / failure_domain / backend 分布，
产出 "行为约束式" golden cases.yaml：
  - fault_type_must_include_any: 每次都出现的 fault_type（intersection）
  - failure_domain_must_include_any: 所有 run 的 failure_domain 并集中的高频项

人工 review 后 commit 到 tests/golden/hypothesis/cases.yaml。

用法：
  BEDROCK_REGION=ap-northeast-1 \\
  NEPTUNE_HOST=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com \\
  python3 experiments/strands-poc/sample_for_golden.py [runs_per_scenario=3]
"""
import json, os, sys, time
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "rca"))
sys.path.insert(0, str(ROOT / "chaos" / "code"))

SCENARIOS = ROOT / "tests" / "golden" / "hypothesis" / "scenarios.yaml"
CASES_DRAFT = ROOT / "tests" / "golden" / "hypothesis" / "cases.draft.yaml"


def _extract_fault_type(fault_scenario: str) -> str:
    """复用 direct 引擎的规则：从 fault_scenario 文本抽 fault_type。"""
    text = (fault_scenario or "").lower()
    from agents.hypothesis_direct import VALID_FAULT_TYPES  # type: ignore
    for f in VALID_FAULT_TYPES:
        if f in text:
            return f
    # 降级：first word
    return text.split()[0] if text.split() else ""


def sample_scenario(scenario: dict, runs: int) -> dict:
    from agents.hypothesis_direct import DirectBedrockHypothesis
    eng = DirectBedrockHypothesis()
    svc = scenario.get("service_filter")
    mx = int(scenario.get("max_hypotheses", 10))

    runs_data = []
    for _ in range(runs):
        try:
            out = eng.generate_with_meta(max_hypotheses=mx, service_filter=svc)
            hyps = out.get("hypotheses") or []
        except Exception as e:
            runs_data.append({"error": repr(e), "hypotheses": []})
            continue
        runs_data.append({
            "error": out.get("error"),
            "hypotheses": [
                {
                    "fault_type": _extract_fault_type(h.fault_scenario),
                    "failure_domain": h.failure_domain,
                    "backend": h.backend,
                    "target_services": h.target_services,
                }
                for h in hyps
            ],
        })

    # 聚合
    all_fault_types = [
        h["fault_type"] for r in runs_data for h in r["hypotheses"] if h["fault_type"]
    ]
    all_failure_domains = [
        h["failure_domain"] for r in runs_data for h in r["hypotheses"] if h["failure_domain"]
    ]
    all_backends = [
        h["backend"] for r in runs_data for h in r["hypotheses"] if h["backend"]
    ]
    counts = {
        "min_hypotheses_seen": min(len(r["hypotheses"]) for r in runs_data),
        "max_hypotheses_seen": max(len(r["hypotheses"]) for r in runs_data),
        "fault_types": dict(Counter(all_fault_types).most_common()),
        "failure_domains": dict(Counter(all_failure_domains).most_common()),
        "backends": dict(Counter(all_backends).most_common()),
    }
    # intersection fault_types：每 run 至少出现一次的
    per_run_types = [set(h["fault_type"] for h in r["hypotheses"]) for r in runs_data]
    inter = set.intersection(*per_run_types) if per_run_types else set()
    return {"counts": counts, "intersection_fault_types": sorted(inter), "runs_raw": runs_data}


def main():
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    with SCENARIOS.open() as f:
        scenarios = yaml.safe_load(f)["scenarios"]

    cases = []
    for sc in scenarios:
        if sc.get("expected", {}).get("should_error"):
            # 直接写回 case，不 sample
            cases.append({
                "id": sc["id"],
                "desc": sc["desc"],
                "service_filter": sc.get("service_filter"),
                "max_hypotheses": sc["max_hypotheses"],
                "expected": sc["expected"],
            })
            continue
        print(f"[sample] {sc['id']} {sc['desc']} (runs={runs})", file=sys.stderr)
        t0 = time.time()
        result = sample_scenario(sc, runs)
        dt = time.time() - t0
        print(f"  {dt:.1f}s  counts={result['counts']}", file=sys.stderr)

        expected = dict(sc.get("expected") or {})
        expected.setdefault("min_hypotheses", max(1, result["counts"]["min_hypotheses_seen"] - 1))
        expected.setdefault("max_hypotheses", result["counts"]["max_hypotheses_seen"])
        if result["intersection_fault_types"]:
            expected["fault_type_must_include_any"] = result["intersection_fault_types"]
        # failure_domain 取出现 ≥ 2 次的
        fd_counts = result["counts"]["failure_domains"]
        frequent_fd = [k for k, v in fd_counts.items() if v >= 2]
        if frequent_fd:
            expected.setdefault("failure_domain_must_include_any", frequent_fd)
        cases.append({
            "id": sc["id"],
            "desc": sc["desc"],
            "service_filter": sc.get("service_filter"),
            "max_hypotheses": sc["max_hypotheses"],
            "expected": expected,
            "_sample_stats": result["counts"],   # 参考用，review 时删掉
        })

    draft = {"cases": cases, "_metadata": {
        "sampled_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
        "runs_per_scenario": runs,
        "engine": "direct",
    }}
    CASES_DRAFT.write_text(yaml.safe_dump(draft, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"\n✅ wrote {CASES_DRAFT}", file=sys.stderr)


if __name__ == "__main__":
    main()
