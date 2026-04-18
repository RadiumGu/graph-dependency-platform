#!/usr/bin/env python3
"""Streamlit smoke — 复制页面的 import 路径 + engine 构造，headless 跑 3 条问题。"""
import json, os, sys, time

_DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_DEMO_DIR, ".."))
_RCA_ROOT = os.path.join(_PROJECT_ROOT, "rca")
for _p in [_PROJECT_ROOT, _RCA_ROOT]:
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("NEPTUNE_ENDPOINT", "petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com")
os.environ.setdefault("REGION", "ap-northeast-1")

from engines.factory import make_nlquery_engine  # noqa: E402

QUESTIONS = [
    "petsite 依赖哪些数据库？",
    "Tier0 服务有哪些？",
    "petsite 的完整上下游依赖路径",
]


def run_for(engine_name: str) -> dict:
    if engine_name:
        os.environ["NLQUERY_ENGINE"] = engine_name
    else:
        os.environ.pop("NLQUERY_ENGINE", None)
    eng = make_nlquery_engine()
    rows = []
    for q in QUESTIONS:
        t0 = time.time()
        out = eng.query(q)
        dt = int((time.time() - t0) * 1000)
        rows.append({
            "q": q,
            "engine": out.get("engine"),
            "latency_ms_observed": dt,
            "latency_ms_internal": out.get("latency_ms"),
            "model_used": out.get("model_used"),
            "rows": len(out.get("results") or []),
            "cypher_head": (out.get("cypher") or "")[:140],
            "trace_tools": [t.get("tool") for t in (out.get("trace") or [])],
            "retried": out.get("retried"),
            "strands_cycles": out.get("strands_cycles"),
            "token_usage": out.get("token_usage"),
            "latency_ms_agent": out.get("latency_ms_agent"),
            "error": out.get("error"),
        })
    return {"engine_class": type(eng).__name__, "runs": rows}


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "direct"
    print(json.dumps(run_for(target), ensure_ascii=False, indent=2, default=str))
