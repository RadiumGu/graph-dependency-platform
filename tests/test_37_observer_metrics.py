"""
test_37_observer_metrics.py —— T-210：runner 必须采集**观测方**（调用侧）指标。

为什么这组测试存在：
验证边 `A -[X]-> B` 必须在 **B** 注入、观测 **A**。runner 原先只采集
`exp.target_service`（注入目标）自己的指标 —— 「打断 B 之后 B 是否退化」
近乎恒真，**根本没有检验任何边**。这也解释了历史上 72 个实验全部 `passed`、
零失败：判定门槛没有分辨力。

最要紧的一条是 o05/o06：**零流量必须判 inconclusive，绝不能判 refuted。**
`metrics.collect()` 无数据时 fallback `success_rate=100.0 / total_requests=0`
—— 零流量和健康在指标上完全一样。判 refuted 会删掉真实存在的边，
比留着未验证的边有害得多。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "chaos" / "code", ROOT / "infra" / "lambda" / "shared" / "python"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)


def _snap(success_rate: float, total_requests: int, ts: int = 0, p99: float = 100.0):
    from runner.experiment import MetricsSnapshot
    return MetricsSnapshot(
        timestamp=ts, success_rate=success_rate,
        latency_p99_ms=p99, total_requests=total_requests,
    )


def _stub_experiment(observers=()):
    """最小 Experiment，仅供 ExperimentResult.__post_init__ 取 target_service。"""
    from runner.experiment import Experiment, FaultSpec, ObservationTarget
    return Experiment(
        name="stub", description="", target_service="target-svc",
        target_namespace="ns", target_tier="Tier1",
        fault=FaultSpec(type="pod_kill", mode="all", value="100", duration="1m"),
        steady_state_before=[], steady_state_after=[], stop_conditions=[],
        observation_targets=[ObservationTarget.parse(o) for o in observers],
    )


def _result():
    """构造一个最小 ExperimentResult，不触发任何 IO。"""
    from runner.result import ExperimentResult
    return ExperimentResult(experiment=_stub_experiment())


# ─── 规格解析 ────────────────────────────────────────────────────────────────

def test_o01_observation_target_parses_three_forms():
    """'svc' / 'ns/svc' / dict 三种写法都要能解析。"""
    from runner.experiment import ObservationTarget

    a = ObservationTarget.parse("petsite")
    assert a.service == "petsite" and a.namespace is None

    b = ObservationTarget.parse("petadoptions/gateway-service")
    assert b.service == "gateway-service" and b.namespace == "petadoptions"

    c = ObservationTarget.parse(
        {"service": "order-service", "namespace": "ns1",
         "edge_label": "DependsOn", "min_baseline_requests": 42})
    assert (c.service, c.namespace, c.edge_label, c.min_baseline_requests) == \
        ("order-service", "ns1", "DependsOn", 42)


def test_o02_default_edge_label_is_calls():
    from runner.experiment import ObservationTarget
    assert ObservationTarget.parse("x").edge_label == "Calls"
    assert ObservationTarget.parse("x", "AccessesData").edge_label == "AccessesData"


def test_o03_experiment_defaults_to_no_observers():
    """未声明观测方时必须是空列表 —— 即退化为旧行为，而不是报错。"""
    from runner.experiment import Experiment, FaultSpec
    exp = Experiment(
        name="n", description="d", target_service="svc", target_namespace="ns",
        target_tier="Tier1",
        fault=FaultSpec(type="pod_kill", mode="all", value="100", duration="1m"),
        steady_state_before=[], steady_state_after=[], stop_conditions=[],
    )
    assert exp.observation_targets == []


# ─── 观测方证据 ──────────────────────────────────────────────────────────────

def test_o04_degradation_is_computed_from_observer_not_target():
    """退化率必须来自观测方自己的基线与最低值。"""
    r = _result()
    r.record_observer_baseline("A", _snap(99.0, 500))
    r.record_observer_snapshot("A", _snap(60.0, 480, 1))
    r.record_observer_snapshot("A", _snap(45.0, 470, 2))
    assert r.observer_min_success_rate["A"] == 45.0
    assert r.observer_degradation_rate("A") == pytest.approx(54.0)


def test_o05_zero_traffic_observer_is_not_usable():
    """零流量观测方必须 usable=False —— 这是不得判 refuted 的前置。"""
    r = _result()
    # DeepFlow 不可达时的 fallback：成功率 100 但请求数 0
    r.record_observer_baseline("A", _snap(100.0, 0))
    r.record_observer_snapshot("A", _snap(100.0, 0, 1))
    assert r.observer_has_real_traffic("A") is False
    assert r.observer_evidence()["A"]["usable"] is False


def test_o06_zero_traffic_classifies_inconclusive_never_refuted():
    """
    最关键的一条：零流量下判定必须是 inconclusive。
    若这条失败，说明系统会把没有流量的真实边判成「不存在」。
    """
    from graph_confidence import classify_intervention, STATUS_INCONCLUSIVE, STATUS_REFUTED
    status, reason = classify_intervention(0, 0, 0.0)
    assert status == STATUS_INCONCLUSIVE
    assert status != STATUS_REFUTED
    assert "流量" in reason


def test_o07_missing_baseline_returns_none_not_zero():
    """
    缺基线时退化率必须是 None 而不是 0.0 ——
    0.0 会被下游读成「完全没退化」从而判 refuted。
    """
    r = _result()
    r.record_observer_snapshot("A", _snap(50.0, 100, 1))
    assert r.observer_degradation_rate("A") is None


def test_o08_missing_injected_samples_returns_none():
    """有基线但注入期无采样，同样不能给出数字。"""
    r = _result()
    r.record_observer_baseline("A", _snap(99.0, 500))
    assert r.observer_degradation_rate("A") is None


def test_o09_observer_evidence_is_per_service():
    """多个观测方各自独立，不能互相污染。"""
    r = _result()
    r.record_observer_baseline("A", _snap(99.0, 500))
    r.record_observer_snapshot("A", _snap(40.0, 480, 1))
    r.record_observer_baseline("B", _snap(99.0, 600))
    r.record_observer_snapshot("B", _snap(98.5, 590, 1))
    ev = r.observer_evidence()
    assert ev["A"]["degradation_rate"] == pytest.approx(59.0)
    assert ev["B"]["degradation_rate"] == pytest.approx(0.5)
    assert ev["A"]["usable"] and ev["B"]["usable"]


# ─── runner 接线 ─────────────────────────────────────────────────────────────

def test_o10_runner_references_edge_verification():
    """
    runner 必须真的引用 edge_verification —— 这是 DoD-3 的第一项检查。
    模块存在但没人调用，就是 T-210 之前的状态。
    """
    src = (ROOT / "chaos" / "code" / "runner" / "runner.py").read_text(encoding="utf-8")
    assert "edge_verification" in src, "runner.py 未引用 edge_verification"
    assert "_verify_edges" in src, "runner.py 缺少 _verify_edges 接线"
    assert "observation_targets" in src, "runner.py 未读取 observation_targets"


def test_o11_runner_collects_observer_in_both_phases():
    """Phase1 要有观测方基线，Phase3 要有观测方采样 —— 少任一半都算不出退化率。"""
    src = (ROOT / "chaos" / "code" / "runner" / "runner.py").read_text(encoding="utf-8")
    assert "_collect_observer_baselines" in src
    assert "_collect_observer_snapshots" in src
    # 基线必须在 Phase1 里调用
    p1 = src.index("_phase1_steady_state_before")
    p2 = src.index("def _phase2_inject")
    assert "_collect_observer_baselines" in src[p1:p2], "Phase1 未采集观测方基线"


def test_o12_no_cardinality_on_edge_properties():
    """
    边属性禁用 property(single, ...) —— Neptune 返回
    400 UnsupportedOperationException。这条写回历史上 100% 失败过 21 次。
    只扫 g.E() 上下文，不误伤顶点写入，也不误伤文档字符串。
    """
    import re
    bad = []
    for p in (ROOT / "chaos" / "code").rglob("*.py"):
        t = p.read_text(encoding="utf-8", errors="ignore")
        for m in re.finditer(r"""g\.E\(\)[^"']*?property\(\s*single""", t, re.S):
            bad.append(f"{p}:{t[:m.start()].count(chr(10)) + 1}")
    assert not bad, f"边属性上仍有 property(single, ...): {bad}"
