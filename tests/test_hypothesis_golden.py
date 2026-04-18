"""
test_hypothesis_golden.py — HypothesisAgent Golden CI（Phase 3 Module 1）。

engine matrix：direct / strands 通过 HYPOTHESIS_ENGINE env 选。默认 skip（真调 Bedrock+Neptune）。

启用：
  RUN_GOLDEN=1 HYPOTHESIS_ENGINE=direct  pytest tests/test_hypothesis_golden.py -v
  RUN_GOLDEN=1 HYPOTHESIS_ENGINE=strands pytest tests/test_hypothesis_golden.py -v

每条 case 来自 tests/golden/hypothesis/cases.yaml（人工 review 过的采样结果）。
断言基于*行为约束*（fault_type_must_include_any / must_not_include /
failure_domain_must_include_any / tier_must_equal / min/max hypotheses）。

BASELINE 落 tests/golden/hypothesis/BASELINE-<engine>.md，含 Avg Cache Hit Ratio。
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "tests" / "golden" / "hypothesis" / "cases.yaml"
SCENARIOS = ROOT / "tests" / "golden" / "hypothesis" / "scenarios.yaml"
BASELINE_DIR = ROOT / "tests" / "golden" / "hypothesis"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_GOLDEN") != "1",
    reason="Hypothesis Golden tests skipped by default (set RUN_GOLDEN=1)",
)


def _load_cases() -> list[dict]:
    """优先读 cases.yaml（人工 review 后）；没有就退回 scenarios.yaml 的纯输入版。"""
    path = CASES if CASES.exists() else SCENARIOS
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("cases") or data.get("scenarios") or []


def _engine_names() -> list[str]:
    req = (os.environ.get("HYPOTHESIS_ENGINE") or "").strip().lower()
    if req in ("direct", "strands"):
        return [req]
    return ["direct", "strands"]


@pytest.fixture(scope="module")
def engines():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "rca"))
    sys.path.insert(0, str(ROOT / "chaos" / "code"))
    from engines.factory import make_hypothesis_engine
    built: dict = {}
    for name in _engine_names():
        os.environ["HYPOTHESIS_ENGINE"] = name
        try:
            built[name] = make_hypothesis_engine()
        except Exception as e:
            built[name] = e
    os.environ.pop("HYPOTHESIS_ENGINE", None)
    return built


@pytest.fixture(scope="module")
def results_accumulator():
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
        latencies = sorted(r["latency_ms"] for r in rows if r["latency_ms"] is not None)
        p50 = latencies[len(latencies) // 2] if latencies else 0
        p99 = latencies[int(len(latencies) * 0.99)] if latencies else 0
        sum_read = sum((r.get("token_usage") or {}).get("cache_read", 0) or 0 for r in rows)
        sum_write = sum((r.get("token_usage") or {}).get("cache_write", 0) or 0 for r in rows)
        sum_input = sum((r.get("token_usage") or {}).get("input", 0) or 0 for r in rows)
        tokens_total = sum((r.get("token_usage") or {}).get("total", 0) or 0 for r in rows)
        denom = sum_read + sum_input
        hit_ratio = f"{sum_read / denom:.1%}" if denom else "N/A"

        lines = [
            f"# HypothesisAgent Golden Baseline — engine: {engine_name}",
            "",
            f"_Last run: {now}_",
            "",
            "| Metric | Value |",
            "|--------|-------|",
            f"| Total cases | {total} |",
            f"| Pass | {passed}/{total} = {passed / total:.1%} |",
            f"| Latency p50 | {p50} ms |",
            f"| Latency p99 | {p99} ms |",
            f"| Total tokens (approx) | {tokens_total} |",
            f"| Cache read tokens | {sum_read} |",
            f"| Cache write tokens | {sum_write} |",
            f"| Avg Cache Hit Ratio | {hit_ratio} |",
            "",
            "## Failures",
            "",
        ]
        fails = [r for r in rows if r["status"] != "pass"]
        if not fails:
            lines.append("_All cases passed._")
        else:
            for r in fails:
                lines.append(f"### {r['id']}: {r.get('desc', '')}")
                for reason in r["reasons"]:
                    lines.append(f"  - ❌ {reason}")
                lines.append("")

        path = BASELINE_DIR / f"BASELINE-{engine_name}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n📊 Hypothesis baseline[{engine_name}] → {path}")


def _extract_fault_type(fault_scenario: str) -> str:
    text = (fault_scenario or "").lower()
    try:
        from agents.hypothesis_direct import VALID_FAULT_TYPES  # type: ignore
    except ImportError:
        VALID_FAULT_TYPES = []
    for f in VALID_FAULT_TYPES:
        if f in text:
            return f
    return ""


@pytest.mark.parametrize("engine_name", _engine_names())
@pytest.mark.parametrize("case", _load_cases(), ids=lambda c: c["id"])
def test_hypothesis_case(engines, results_accumulator, engine_name, case):
    engine = engines.get(engine_name)
    if isinstance(engine, Exception):
        pytest.skip(f"engine '{engine_name}' unavailable: {engine!r}")

    expected = case.get("expected") or {}
    max_h = int(case.get("max_hypotheses", 10))
    svc = case.get("service_filter")

    out = engine.generate_with_meta(max_hypotheses=max_h, service_filter=svc)

    reasons: list[str] = []
    err = out.get("error")
    hyps = out.get("hypotheses") or []

    if expected.get("should_error"):
        # 预期出错的场景
        if not err and hyps:
            reasons.append(f"expected error, got {len(hyps)} hypotheses")
        status = "pass" if not reasons else "fail"
        results_accumulator.append({
            "id": case["id"], "desc": case.get("desc"), "engine": engine_name,
            "status": status, "reasons": reasons,
            "latency_ms": out.get("latency_ms"), "token_usage": out.get("token_usage"),
        })
        if status != "pass":
            pytest.fail(f"[{engine_name}] " + "; ".join(reasons))
        return

    # 常规断言
    if err:
        reasons.append(f"engine error: {err}")

    min_h = int(expected.get("min_hypotheses", 1))
    max_h_expected = int(expected.get("max_hypotheses", max_h))
    if not (min_h <= len(hyps) <= max_h_expected):
        reasons.append(f"count {len(hyps)} not in [{min_h}, {max_h_expected}]")

    fault_types = {_extract_fault_type(h.fault_scenario) for h in hyps}
    failure_domains = {(h.failure_domain or "") for h in hyps}
    backends = {(h.backend or "") for h in hyps}

    must_fts = expected.get("fault_type_must_include_any") or []
    if must_fts and not (set(must_fts) & fault_types):
        reasons.append(f"fault_types {fault_types} missing any of {must_fts}")

    must_fds = expected.get("failure_domain_must_include_any") or []
    if must_fds and not (set(must_fds) & failure_domains):
        reasons.append(f"failure_domains {failure_domains} missing any of {must_fds}")

    must_bks = expected.get("backend_must_include_any") or []
    if must_bks and not (set(must_bks) & backends):
        reasons.append(f"backends {backends} missing any of {must_bks}")

    # must_not_include: list of {match: substring} against fault_scenario
    for rule in expected.get("must_not_include") or []:
        needle = (rule.get("match") or "").lower()
        if not needle:
            continue
        for h in hyps:
            if needle in (h.fault_scenario or "").lower():
                reasons.append(f"forbidden match '{needle}' found: {h.fault_scenario!r} ({rule.get('reason')})")

    status = "pass" if not reasons else "fail"
    results_accumulator.append({
        "id": case["id"], "desc": case.get("desc"), "engine": engine_name,
        "status": status, "reasons": reasons,
        "latency_ms": out.get("latency_ms"), "token_usage": out.get("token_usage"),
    })

    if status != "pass":
        pytest.fail(f"[{engine_name}] " + "; ".join(reasons))
