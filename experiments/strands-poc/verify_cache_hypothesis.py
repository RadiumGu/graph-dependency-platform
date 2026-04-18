#!/usr/bin/env python3
"""verify_cache_hypothesis.py — HypothesisAgent 缓存生效验证。

同 Smart Query L2 的 verify_cache_direct.py，但针对 DirectBedrockHypothesis
和 StrandsHypothesisAgent。每个 engine 连跑 3 次相同请求：
  run 1: cache_write > 0
  run 2/3: cache_read > 0

用法：
  NEPTUNE_ENDPOINT=... python3 experiments/strands-poc/verify_cache_hypothesis.py [direct|strands]
"""
import json, os, sys, time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "rca"))
sys.path.insert(0, os.path.join(ROOT, "chaos", "code"))
os.environ.setdefault("NEPTUNE_ENDPOINT", "petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com")


def _run(engine_name: str, max_h: int = 5, service: str = "petsite") -> list[dict]:
    if engine_name == "direct":
        from agents.hypothesis_direct import DirectBedrockHypothesis
        eng = DirectBedrockHypothesis()
    elif engine_name == "strands":
        from agents.hypothesis_strands import StrandsHypothesisAgent
        eng = StrandsHypothesisAgent()
    else:
        raise ValueError(engine_name)

    rows = []
    for i in range(1, 4):
        out = eng.generate_with_meta(max_hypotheses=max_h, service_filter=service)
        tu = out.get("token_usage") or {}
        rows.append({
            "engine": engine_name,
            "run": i,
            "hypotheses": len(out.get("hypotheses") or []),
            "latency_ms": out.get("latency_ms"),
            "input": tu.get("input"),
            "output": tu.get("output"),
            "cache_read": tu.get("cache_read"),
            "cache_write": tu.get("cache_write"),
            "total": tu.get("total"),
        })
    return rows


def main():
    targets = sys.argv[1:] or ["direct", "strands"]
    all_rows = []
    for t in targets:
        all_rows.extend(_run(t))
    print(json.dumps(all_rows, ensure_ascii=False, indent=2))

    ok_overall = True
    for t in targets:
        rows = [r for r in all_rows if r["engine"] == t]
        if len(rows) < 3:
            continue
        r1, r2, r3 = rows
        ok = (r1.get("cache_write", 0) or 0) > 0 \
             and (r2.get("cache_read", 0) or 0) > 0 \
             and (r3.get("cache_read", 0) or 0) > 0
        print(f"\n{t}: {'PASS ✅' if ok else 'FAIL ❌'}")
        ok_overall &= ok

    sys.exit(0 if ok_overall else 1)


if __name__ == "__main__":
    main()
