"""
test_hypothesis_golden.py — HypothesisAgent Golden CI（Phase 3 Module 1）。

engine matrix：direct / strands 通过 HYPOTHESIS_ENGINE env 选。默认 skip（真调 Bedrock+Neptune）。

启用：
  RUN_GOLDEN=1 HYPOTHESIS_ENGINE=direct  # ← 已失效，direct 已删除  pytest tests/test_hypothesis_golden.py -v
  RUN_GOLDEN=1 HYPOTHESIS_ENGINE=strands pytest tests/test_hypothesis_golden.py -v

每条 case 来自 tests/golden/hypothesis/cases.yaml（人工 review 过的采样结果）。
断言基于*行为约束*（fault_type_must_include_any / must_not_include /
failure_domain_must_include_any / tier_must_equal / min/max hypotheses）。

BASELINE 落 tests/golden/hypothesis/BASELINE-<engine>.md，含 Avg Cache Hit Ratio。

⚠️ 2026-09-21：本文件头原先写的运行命令带 `HYPOTHESIS_ENGINE=direct`，
而 direct 实现已于当天全部删除 —— 照那条命令跑会以为在测 direct，
实际拿到的仍是 strands（factory 已去掉回退分支，只有一种实现）。

七套 golden 的跑法现在收在**唯一权威**的
`scripts/run_golden_suite.sh` 里（各自 cd 的目录与 PYTHONPATH 都不同，
七份各自维护的命令必然漂移，这次就漂了）：

    bash scripts/run_golden_suite.sh hypothesis   # 只跑本套
    bash scripts/run_golden_suite.sh --list    # 看全部套件
    bash scripts/run_golden_suite.sh           # 全跑（约 $3-5、10-15 分钟）
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
    if req == "strands":
        return [req]
    if req == "direct":
        raise RuntimeError(
            f"{__name__}: 设了 HYPOTHESIS_ENGINE=direct，但 direct 实现已于 "
            "2026-09-20 删除，factory 会返回 strands —— 跑下去会得到一份"
            "标着 direct 的 strands 数据。请去掉这个 env。")
    return ["strands"]


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
    # 不再 try/except —— 原先 `except ImportError: VALID_FAULT_TYPES = []`
    # 会让这个函数在导入失败时**永远返回空字符串**，而测试照绿：
    # 任何基于 fault_type 的断言都静默失效。宁可 import 直接炸。
    from agents.hypothesis_common import VALID_FAULT_TYPES  # type: ignore
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

    # ── 合法的空结果不受 must_include_any 约束 ────────────────────────────
    #
    # 一条用例可以同时声明 `min_hypotheses: 0`（允许产出 0 个假设）和
    # `*_must_include_any`（产出的假设必须覆盖某些维度）。这两者在结果为空时
    # **逻辑上不可同时满足** —— 0 个假设必然给出空的 fault_types /
    # failure_domains / backends，而空集不包含任何值。
    #
    # 2026-09-21 实测这正是 S014 的失败原因：
    #
    #     Failed: [strands] failure_domains set() missing any of ['network', 'compute']
    #
    # 而该用例自己的 notes 早就写明了意图：
    # 「sparse topology / broad scan — may legitimately produce 0」。
    # 也就是说**用例作者允许空结果，是校验逻辑没实现这个意图**。
    #
    # 查下去发现引擎的行为是对的：`gateway-service` 属于 awesomeshop，
    # 2026-09-04 已把它指向 petsite 的边证伪清除（该 namespace 六个 Deployment
    # 副本数全为 0），实测它在图里 0 节点 0 条边。Agent 的 trace 写着
    # 「query_topology 对 gateway-service 返回了空列表 []，根据规则：」——
    # 它**拒绝为不存在的服务编造假设**，这恰恰是最该保住的行为。
    #
    # 所以空结果时跳过 must_include_any，而不是放宽 must_include_any 本身：
    #   · 用例要求至少 1 个假设（min_hypotheses >= 1）时，空结果仍由上面的
    #     数量断言判失败 —— 那条没被绕过；
    #   · `must_not_include`（禁止项）**照旧校验**，因为空结果天然满足它，
    #     不存在矛盾。
    _empty_is_allowed = not hyps and min_h == 0
    if _empty_is_allowed:
        reasons.append(
            "__note__: 产出 0 个假设，而用例声明 min_hypotheses=0 允许这种情况，"
            "故跳过 *_must_include_any 校验（空集无法包含任何值）")

    if not _empty_is_allowed:
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

    # `__note__:` 前缀的条目是**说明而非问题** —— 它要进 BASELINE 供人看见
    # （「这条为什么算过」和「这条为什么算不过」同样重要），但不参与判定。
    # 不做这个区分的话，上面那条跳过说明会让用例直接判失败。
    real_reasons = [r for r in reasons if not r.startswith("__note__:")]
    status = "pass" if not real_reasons else "fail"
    results_accumulator.append({
        "id": case["id"], "desc": case.get("desc"), "engine": engine_name,
        "status": status, "reasons": reasons,
        "latency_ms": out.get("latency_ms"), "token_usage": out.get("token_usage"),
    })

    if status != "pass":
        pytest.fail(f"[{engine_name}] " + "; ".join(real_reasons))
