"""
test_nlquery_shadow.py - Smart Query shadow 对比（PR4/6）

对同一批问题用 direct 和 strands 各跑一遍，输出 cypher diff、结果行数 diff、
延迟倍数。*不 block PR*，结果打印到 stdout（用 pytest -s 看）。

启用：
  cd rca && PYTHONPATH=.:.. RUN_GOLDEN=1 pytest ../tests/test_nlquery_shadow.py -v -s
"""
from __future__ import annotations

import os
import statistics
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "tests" / "golden" / "petsite.yaml"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_GOLDEN") != "1",
    reason="Shadow test skipped by default (set RUN_GOLDEN=1)",
)


def _load_cases() -> list[dict]:
    with GOLDEN_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)["cases"]


@pytest.fixture(scope="module")
def direct_engine():
    from neptune.nl_query_direct import DirectBedrockNLQuery
    return DirectBedrockNLQuery()


@pytest.fixture(scope="module")
def strands_engine():
    try:
        from neptune.nl_query_strands import StrandsNLQueryEngine
    except ImportError as e:
        pytest.skip(f"Strands not available: {e}")
    return StrandsNLQueryEngine()


def test_shadow_compare(direct_engine, strands_engine, capsys):
    """对所有 golden cases 做 direct vs strands 对比（informational only）。"""
    cases = _load_cases()
    diffs: list[dict] = []
    latency_direct: list[int] = []
    latency_strands: list[int] = []

    print("\n=== Shadow comparison: direct vs strands ===")
    print(f"{'id':<6} {'direct(ms)':>10} {'strands(ms)':>12} {'mult':>6} {'rows_d/rows_s':>15}  {'verdict'}")

    for case in cases:
        q = case["question"]
        out_d = direct_engine.query(q)
        out_s = strands_engine.query(q)

        ld = int(out_d.get("latency_ms") or 0)
        ls = int(out_s.get("latency_ms") or 0)
        rd = len(out_d.get("results") or [])
        rs = len(out_s.get("results") or [])
        cyd = (out_d.get("cypher") or "").strip()
        cys = (out_s.get("cypher") or "").strip()
        mult = (ls / ld) if ld else float("inf")

        cypher_same = cyd == cys
        rows_same = rd == rs
        verdict_bits = []
        if not cypher_same:
            verdict_bits.append("cypher≠")
        if not rows_same:
            verdict_bits.append(f"rows{rd}/{rs}")
        if not verdict_bits:
            verdict_bits.append("match")

        print(f"{case['id']:<6} {ld:>10} {ls:>12} {mult:>6.2f} {f'{rd}/{rs}':>15}  {' '.join(verdict_bits)}")

        latency_direct.append(ld)
        latency_strands.append(ls)
        diffs.append({"id": case["id"], "cypher_same": cypher_same, "rows_same": rows_same,
                       "cyd": cyd, "cys": cys, "rd": rd, "rs": rs, "ld": ld, "ls": ls})

    # 汇总
    if latency_direct and latency_strands:
        p50_d = statistics.median(latency_direct)
        p50_s = statistics.median(latency_strands)
        p99_d = sorted(latency_direct)[int(len(latency_direct) * 0.99)]
        p99_s = sorted(latency_strands)[int(len(latency_strands) * 0.99)]
        print(f"\np50 direct={p50_d:.0f}ms  strands={p50_s:.0f}ms  mult={p50_s / p50_d:.2f}x")
        print(f"p99 direct={p99_d:.0f}ms  strands={p99_s:.0f}ms  mult={p99_s / p99_d:.2f}x")

    same_cypher = sum(1 for d in diffs if d["cypher_same"])
    same_rows = sum(1 for d in diffs if d["rows_same"])
    print(f"\ncypher identical: {same_cypher}/{len(diffs)}")
    print(f"row-count identical: {same_rows}/{len(diffs)}")

    # 打印详细 diff（仅 cypher 不一致的）
    print("\n--- cypher diffs ---")
    for d in diffs:
        if not d["cypher_same"]:
            print(f"\n[{d['id']}] direct  rows={d['rd']} ms={d['ld']}:\n  {d['cyd']}")
            print(f"[{d['id']}] strands rows={d['rs']} ms={d['ls']}:\n  {d['cys']}")

    # 此测试不 assert 失败，只打印（按任务要求）
    assert True
