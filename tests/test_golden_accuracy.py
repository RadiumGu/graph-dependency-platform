"""
test_golden_accuracy.py - Smart Query 准确率 Golden CI

真调 Bedrock + Neptune，对 golden set 每条 case 检查：
  1. 生成的 cypher 是否满足 must_contain / must_not_contain
  2. 执行结果是否达到 min_rows + 含必要字段

默认 skip（成本考虑）。启用：
  cd rca && PYTHONPATH=.:.. RUN_GOLDEN=1 NLQUERY_ENGINE=direct  pytest ../tests/test_golden_accuracy.py -v
  cd rca && PYTHONPATH=.:.. RUN_GOLDEN=1 NLQUERY_ENGINE=strands pytest ../tests/test_golden_accuracy.py -v

引擎矩阵（PR4）: 通过 env NLQUERY_ENGINE 选 direct|strands，
BASELINE 按 engine 分别写入 BASELINE-direct.md / BASELINE-strands.md。
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
BASELINE_DIR = ROOT / "tests" / "golden"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_GOLDEN") != "1",
    reason="Golden accuracy tests skipped by default (set RUN_GOLDEN=1)",
)


def _load_cases() -> list[dict]:
    with GOLDEN_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)["cases"]


def _engine_names() -> list[str]:
    """允许通过 NLQUERY_ENGINE env 收窄 matrix。未设置 → 两个都跑。"""
    req = (os.environ.get("NLQUERY_ENGINE") or "").strip().lower()
    if req in ("direct", "strands"):
        return [req]
    return ["direct", "strands"]


@pytest.fixture(scope="module")
def engines():
    """构造所有参与 matrix 的 engine 实例（复用以减少 boto3 client 建设开销）。"""
    from engines.factory import make_nlquery_engine
    built: dict = {}
    for name in _engine_names():
        os.environ["NLQUERY_ENGINE"] = name
        try:
            built[name] = make_nlquery_engine()
        except Exception as e:  # engine 构造失败 → 把它从 matrix 剔除
            built[name] = e
    os.environ.pop("NLQUERY_ENGINE", None)
    return built


@pytest.fixture(scope="module")
def results_accumulator():
    """累积所有 case × engine 的结果，module teardown 时按 engine 写 BASELINE-<name>.md。"""
    acc: list[dict] = []
    yield acc

    if not acc:
        return

    engines_in_run = sorted({r["engine"] for r in acc})
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    for engine_name in engines_in_run:
        rows = [r for r in acc if r["engine"] == engine_name]
        total = len(rows)
        passed = sum(1 for r in rows if r["status"] == "pass")
        feature_hits = sum(1 for r in rows if r["feature_ok"])
        result_hits = sum(1 for r in rows if r["result_ok"])
        latencies = sorted(r["latency_ms"] for r in rows if r["latency_ms"] is not None)
        p50 = latencies[len(latencies) // 2] if latencies else 0
        p99 = latencies[int(len(latencies) * 0.99)] if latencies else 0
        tokens_total = sum((r.get("token_usage") or {}).get("total", 0) or 0 for r in rows)

        lines = [
            f"# Smart Query Golden Baseline — engine: {engine_name}",
            "",
            f"_Last run: {now}_",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total cases | {total} |",
            f"| Pass (all checks) | {passed}/{total} = {passed / total:.1%} |",
            f"| Feature match | {feature_hits}/{total} = {feature_hits / total:.1%} |",
            f"| Result correctness | {result_hits}/{total} = {result_hits / total:.1%} |",
            f"| Latency p50 | {p50} ms |",
            f"| Latency p99 | {p99} ms |",
            f"| Total tokens (approx) | {tokens_total} |",
            "",
            "## Failures",
            "",
        ]
        fails = [r for r in rows if r["status"] != "pass"]
        if not fails:
            lines.append("_All cases passed._")
        else:
            for r in fails:
                lines.append(f"### {r['id']}: {r['question']}")
                lines.append(f"- generated cypher: `{r['cypher']}`")
                for reason in r["reasons"]:
                    lines.append(f"  - ❌ {reason}")
                lines.append("")

        path = BASELINE_DIR / f"BASELINE-{engine_name}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n📊 Baseline[{engine_name}] written to {path}")


@pytest.mark.parametrize("engine_name", _engine_names())
@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_golden_case(engines, results_accumulator, engine_name, case):
    engine = engines.get(engine_name)
    if isinstance(engine, Exception):
        pytest.skip(f"engine '{engine_name}' unavailable: {engine!r}")

    q = case["question"]
    out = engine.query(q)
    cypher = out.get("cypher", "") or ""
    results = out.get("results") or []
    error = out.get("error")

    reasons: list[str] = []
    if error:
        reasons.append(f"engine error: {error}")

    feature_ok = True
    for needle in case.get("cypher_must_contain", []) or []:
        if needle not in cypher:
            feature_ok = False
            reasons.append(f"cypher missing substring: {needle!r}")
    for needle in case.get("cypher_must_not_contain", []) or []:
        if needle in cypher:
            feature_ok = False
            reasons.append(f"cypher contains forbidden substring: {needle!r}")

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
        "engine": engine_name,
        "question": q,
        "cypher": cypher,
        "feature_ok": feature_ok,
        "result_ok": result_ok,
        "status": status,
        "reasons": reasons,
        "latency_ms": out.get("latency_ms"),
        "token_usage": out.get("token_usage"),
    })

    if status != "pass":
        pytest.fail(f"[{engine_name}] " + "; ".join(reasons))
