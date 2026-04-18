"""
Baseline — 现版 NLQueryEngine 跑同样 3 条 golden 问题，用于延迟对比。
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "rca"))
sys.path.insert(0, str(ROOT))
os.environ.setdefault("PROFILE_NAME", "petsite")

from neptune.nl_query import NLQueryEngine  # noqa: E402


def load_goldens() -> list[str]:
    p = Path(__file__).parent / "golden_questions.yaml"
    if p.exists():
        data = yaml.safe_load(p.read_text())
        if isinstance(data, dict) and "questions" in data:
            return [q if isinstance(q, str) else q.get("q", "") for q in data["questions"]]
    return [
        "petsite 依赖哪些数据库？",
        "Tier0 服务有哪些？",
        "petsite 的完整上下游依赖路径",
    ]


def main():
    eng = NLQueryEngine()
    runs = []
    for q in load_goldens():
        t0 = time.time()
        try:
            r = eng.query(q)
            err = r.get("error")
        except Exception as e:
            r, err = {}, repr(e)
        dt = time.time() - t0
        runs.append({
            "question": q,
            "latency_s": round(dt, 2),
            "cypher": (r.get("cypher") or "")[:200],
            "n_results": len(r.get("results") or []),
            "error": err,
            "summary_preview": (r.get("summary") or "")[:300],
        })
        print(f"- {q} -> {dt:.2f}s, rows={len(r.get('results') or [])}, err={err}")

    latencies = [x["latency_s"] for x in runs]
    out = {
        "runs": runs,
        "p50_latency_s": round(statistics.median(latencies), 2) if latencies else None,
    }
    Path(__file__).parent.joinpath("baseline_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2)
    )
    print("\nwrote baseline_result.json, p50=", out["p50_latency_s"])


if __name__ == "__main__":
    main()
