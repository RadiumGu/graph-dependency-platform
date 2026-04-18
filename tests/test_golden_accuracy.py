"""
test_golden_accuracy.py - Smart Query 准确率 Golden CI (Wave 3, 2026-04-18)

真调 Bedrock + Neptune，对 golden set 里的每条 case 检查：
  1. 生成的 cypher 是否满足 must_contain / must_not_contain 特征
  2. 执行结果是否达到 min_rows + 含必要字段

默认 skip（成本考虑）。启用：
    cd rca && PYTHONPATH=.:.. RUN_GOLDEN=1 pytest ../tests/test_golden_accuracy.py -v

输出：pass 率、feature_match_rate、result_correctness 汇总到 BASELINE.md。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
# conftest.py at tests/ root sets up sys.path for rca/profiles imports.

GOLDEN_PATH = ROOT / "tests" / "golden" / "petsite.yaml"
BASELINE_PATH = ROOT / "tests" / "golden" / "BASELINE.md"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_GOLDEN") != "1",
    reason="Golden accuracy tests skipped by default (set RUN_GOLDEN=1)",
)


def _load_cases() -> list[dict]:
    with GOLDEN_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)["cases"]


@pytest.fixture(scope="module")
def engine():
    from neptune.nl_query import NLQueryEngine
    return NLQueryEngine()


@pytest.fixture(scope="module")
def results_accumulator():
    """累积单测结果，module 结束时写 BASELINE.md。"""
    acc: list[dict] = []
    yield acc

    # 生成 baseline 报告
    if not acc:
        return
    total = len(acc)
    passed = sum(1 for r in acc if r["status"] == "pass")
    feature_hits = sum(1 for r in acc if r["feature_ok"])
    result_hits = sum(1 for r in acc if r["result_ok"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        f"# Smart Query Golden Baseline",
        f"",
        f"_Last run: {now}_",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Total cases | {total} |",
        f"| Pass (all checks) | {passed}/{total} = {passed / total:.1%} |",
        f"| Feature match | {feature_hits}/{total} = {feature_hits / total:.1%} |",
        f"| Result correctness | {result_hits}/{total} = {result_hits / total:.1%} |",
        f"",
        f"## Failures",
        f"",
    ]
    fails = [r for r in acc if r["status"] != "pass"]
    if not fails:
        lines.append("_All cases passed._")
    else:
        for r in fails:
            lines.append(f"### {r['id']}: {r['question']}")
            lines.append(f"- generated cypher: `{r['cypher']}`")
            for reason in r["reasons"]:
                lines.append(f"  - ❌ {reason}")
            lines.append("")
    BASELINE_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n📊 Baseline written to {BASELINE_PATH}")


@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_golden_case(engine, results_accumulator, case):
    q = case["question"]
    out = engine.query(q)
    cypher = out.get("cypher", "")
    results = out.get("results") or []
    error = out.get("error")

    reasons: list[str] = []
    if error:
        reasons.append(f"engine error: {error}")

    # feature checks on cypher
    feature_ok = True
    for needle in case.get("cypher_must_contain", []) or []:
        if needle not in cypher:
            feature_ok = False
            reasons.append(f"cypher missing substring: {needle!r}")
    for needle in case.get("cypher_must_not_contain", []) or []:
        if needle in cypher:
            feature_ok = False
            reasons.append(f"cypher contains forbidden substring: {needle!r}")

    # result checks
    result_ok = True
    min_rows = int(case.get("result_min_rows", 0))
    if len(results) < min_rows:
        result_ok = False
        reasons.append(f"result has {len(results)} rows, expected ≥ {min_rows}")
    must_any = case.get("result_must_contain_any") or []
    if must_any:
        blob = json.dumps(results, ensure_ascii=False, default=str)
        if not any(tok in blob for tok in must_any):
            result_ok = False
            reasons.append(f"result missing any of: {must_any}")

    status = "pass" if feature_ok and result_ok and not error else "fail"
    results_accumulator.append({
        "id": case["id"],
        "question": q,
        "cypher": cypher,
        "feature_ok": feature_ok,
        "result_ok": result_ok,
        "status": status,
        "reasons": reasons,
    })

    if status != "pass":
        pytest.fail("; ".join(reasons))
