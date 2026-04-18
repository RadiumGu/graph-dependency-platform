#!/usr/bin/env python3
"""verify_cache_strands.py — Strands 引擎 Prompt Caching 生效验证。

同 verify_cache_direct.py，但针对 StrandsNLQueryEngine。
"""
import json, os, sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "rca"))
os.environ.setdefault("NEPTUNE_ENDPOINT", "petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com")

from neptune.nl_query_strands import StrandsNLQueryEngine  # noqa: E402

Q = "petsite 依赖哪些数据库？"
engine = StrandsNLQueryEngine()

rows = []
for i in range(1, 4):
    out = engine.query(Q)
    tu = out.get("token_usage") or {}
    rows.append({
        "run": i,
        "rows": len(out.get("results") or []),
        "latency_ms": out.get("latency_ms"),
        "cycles": out.get("strands_cycles"),
        "input": tu.get("input"),
        "output": tu.get("output"),
        "cache_read": tu.get("cache_read"),
        "cache_write": tu.get("cache_write"),
        "total": tu.get("total"),
    })

print(json.dumps(rows, ensure_ascii=False, indent=2))

r1, r2, r3 = rows
ok = (r1.get("cache_write", 0) > 0
      and r2.get("cache_read", 0) > 0
      and r3.get("cache_read", 0) > 0)
print("\nverdict:", "PASS ✅ cache working" if ok else "FAIL ❌ see numbers above")
sys.exit(0 if ok else 1)
