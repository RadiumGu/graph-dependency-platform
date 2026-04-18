"""
test_hypothesis_shadow.py — HypothesisAgent Direct vs Strands shadow 对比。

对 scenarios.yaml 里每条输入都跑两引擎，输出：
  - 每 case 的 hypothesis 数量差异
  - fault_type 分布差异（jaccard）
  - 延迟倍数 / token 倍数 / 缓存命中率

*不 block PR*，仅打印到 stdout（pytest -s）。

启用：
  RUN_GOLDEN=1 pytest tests/test_hypothesis_shadow.py -v -s
"""
from __future__ import annotations

import os
import statistics
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "tests" / "golden" / "hypothesis" / "scenarios.yaml"

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_GOLDEN") != "1",
    reason="Shadow test skipped by default (set RUN_GOLDEN=1)",
)


def _load_scenarios() -> list[dict]:
    with SCENARIOS.open(encoding="utf-8") as f:
        return yaml.safe_load(f)["scenarios"]


@pytest.fixture(scope="module")
def both_engines():
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(ROOT / "rca"))
    sys.path.insert(0, str(ROOT / "chaos" / "code"))
    from agents.hypothesis_direct import DirectBedrockHypothesis
    try:
        from agents.hypothesis_strands import StrandsHypothesisAgent
    except ImportError as e:
        pytest.skip(f"Strands not available: {e}")
    return {"direct": DirectBedrockHypothesis(), "strands": StrandsHypothesisAgent()}


def _extract_fault_type(fs: str) -> str:
    text = (fs or "").lower()
    try:
        from agents.hypothesis_direct import VALID_FAULT_TYPES  # type: ignore
    except ImportError:
        VALID_FAULT_TYPES = []
    for f in VALID_FAULT_TYPES:
        if f in text:
            return f
    return ""


def _fault_jaccard(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / max(1, len(sa | sb))


def test_hypothesis_shadow(both_engines, capsys):
    scenarios = _load_scenarios()
    rows = []
    lat_d, lat_s = [], []
    cache = {"direct": {"read": 0, "write": 0, "input": 0},
             "strands": {"read": 0, "write": 0, "input": 0}}

    print("\n=== Hypothesis shadow: direct vs strands ===")
    print(f"{'id':<5} {'d_ct':>4} {'s_ct':>4} {'jac':>5} {'d(ms)':>7} {'s(ms)':>7} {'mult':>5}")

    for sc in scenarios:
        if (sc.get("expected") or {}).get("should_error"):
            continue
        sv = sc.get("service_filter")
        mh = sc["max_hypotheses"]
        out_d = both_engines["direct"].generate_with_meta(max_hypotheses=mh, service_filter=sv)
        out_s = both_engines["strands"].generate_with_meta(max_hypotheses=mh, service_filter=sv)
        d_list = out_d.get("hypotheses") or []
        s_list = out_s.get("hypotheses") or []
        d_fts = [_extract_fault_type(h.fault_scenario) for h in d_list]
        s_fts = [_extract_fault_type(h.fault_scenario) for h in s_list]
        jac = _fault_jaccard(d_fts, s_fts)
        ld = int(out_d.get("latency_ms") or 0)
        ls = int(out_s.get("latency_ms") or 0)
        mult = (ls / ld) if ld else 0.0

        print(f"{sc['id']:<5} {len(d_list):>4} {len(s_list):>4} {jac:>5.2f} {ld:>7} {ls:>7} {mult:>5.2f}")

        lat_d.append(ld)
        lat_s.append(ls)
        for engine_name, out in (("direct", out_d), ("strands", out_s)):
            tu = out.get("token_usage") or {}
            cache[engine_name]["read"] += int(tu.get("cache_read") or 0)
            cache[engine_name]["write"] += int(tu.get("cache_write") or 0)
            cache[engine_name]["input"] += int(tu.get("input") or 0)
        rows.append({"id": sc["id"], "jac": jac, "d_ct": len(d_list), "s_ct": len(s_list),
                     "d_fts": d_fts, "s_fts": s_fts})

    if lat_d and lat_s:
        p50_d = statistics.median(lat_d)
        p50_s = statistics.median(lat_s)
        p99_d = sorted(lat_d)[int(len(lat_d) * 0.99)]
        p99_s = sorted(lat_s)[int(len(lat_s) * 0.99)]
        print(f"\np50 direct={p50_d:.0f}ms  strands={p50_s:.0f}ms  mult={p50_s / p50_d:.2f}x")
        print(f"p99 direct={p99_d:.0f}ms  strands={p99_s:.0f}ms  mult={p99_s / p99_d:.2f}x")

    jacs = [r["jac"] for r in rows]
    if jacs:
        print(f"\nfault_type jaccard avg={statistics.mean(jacs):.2f} min={min(jacs):.2f}")

    for name, cs in cache.items():
        denom = cs["read"] + cs["input"]
        ratio = f"{cs['read'] / denom:.1%}" if denom else "N/A"
        print(f"{name}: cache_read={cs['read']} write={cs['write']} non-cache_input={cs['input']} hit={ratio}")

    # 不 block，informational only
    assert True
