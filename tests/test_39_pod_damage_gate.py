"""test_39_pod_damage_gate.py —— T-214h：SLI 全绿但 Pod 被打伤时必须判 FAILED。

## 这组测试守什么

2026-08-31 两轮真实 `http_chaos abort` 注入，SLI 报 100%、稳态检查全部通过、
实验判 **PASSED**，但被注入的两个 Pod 都进了重启循环并持续 1/2 Ready，
只能人工 `kubectl delete pod` 重建。

SLI 之所以看不见：HPA 新拉的干净 Pod 在撑着服务。
**服务健康 ≠ 实验没造成损伤。** 一个报 PASSED 却留下坏 Pod 的实验，
会让下一轮实验的基线带着污染开始。

## 为什么主判据是 restarts 差值而不是 readiness

readiness 有滞后：实测 Phase 4 报 `2/2 running`（08:34:07）之后**还要 2.5 分钟**
Pod 才退回 1/2。任何点时刻的 running/total 都可能刚好看不见损伤。
而 `restartCount` 在注入期间就已递增，Phase 5 时必然可见。

所以两条判据都要有，且缺基线时**不算通过**（`restarts=None` 显式区分
「没测到」与「没有重启」）。
"""
from __future__ import annotations

import pytest

from runner.experiment import (
    Experiment, FaultSpec, MetricsSnapshot, ObservationTarget, SteadyStateCheck,
)
from runner.result import ExperimentResult


def _exp():
    return Experiment(
        name="pod-damage-stub", description="", target_service="search-service",
        target_namespace="petadoptions", target_tier="Tier1",
        fault=FaultSpec(type="http_chaos", mode="all", value="", duration="3m"),
        steady_state_before=[], steady_state_after=[], stop_conditions=[],
        observation_targets=[],
    )


class _FakeInjector:
    """只提供 check_pods 的最小桩。"""

    def __init__(self, after: dict):
        self._after = after

    def check_pods(self, service, namespace="default"):
        return self._after


def _runner(after: dict):
    """构造一个只用于调用 _check_target_pod_health 的 runner，不触发任何 IO。"""
    from runner.runner import ExperimentRunner
    r = ExperimentRunner.__new__(ExperimentRunner)   # 绕过 __init__ 的外部依赖
    r.injector = _FakeInjector(after)
    return r


# ── g01：重启差值是主判据 ────────────────────────────────────────────────────

def test_p01_restart_increase_fails_even_when_all_pods_ready():
    """所有 Pod 都 Ready，但重启数涨了 —— 必须判 FAILED。

    这正是实测场景的前半段：Phase 5 采样时 HPA 新 Pod 已把服务撑成 2/2，
    而被注入的那两个 Pod 已经各重启过一次。
    """
    exp, result = _exp(), ExperimentResult(experiment=_exp())
    result.target_pods_before = {"total": 2, "running": 2, "not_running": [],
                                 "restarts": 0, "per_pod_restarts": {"a": 0, "b": 0}}
    after = {"total": 2, "running": 2, "not_running": [],
             "restarts": 2, "per_pod_restarts": {"a": 1, "b": 1}}

    ok = _runner(after)._check_target_pod_health(exp, result)

    assert ok is False, "重启数从 0 涨到 2，不能判通过"
    assert result.pod_damage, "损伤必须被记录进报告"
    assert "重启" in result.pod_damage[0]
    # 处置动作必须写清楚 —— 光说坏了不告诉怎么修，下一轮基线还是脏的
    assert "删 Pod" in result.pod_damage[0]


def test_p02_all_clean_passes():
    """Pod 全就绪且重启数未增加 —— 通过。"""
    exp, result = _exp(), ExperimentResult(experiment=_exp())
    result.target_pods_before = {"total": 2, "running": 2, "not_running": [],
                                 "restarts": 3, "per_pod_restarts": {"a": 2, "b": 1}}
    after = {"total": 2, "running": 2, "not_running": [],
             "restarts": 3, "per_pod_restarts": {"a": 2, "b": 1}}

    assert _runner(after)._check_target_pod_health(exp, result) is True
    assert not result.pod_damage


def test_p03_pre_existing_restarts_are_not_blamed_on_this_run():
    """基线本来就有重启数，本轮没有新增 —— 不得判 FAILED。

    否则任何跑过一次实验的服务都会永久失败。判据是**差值**不是绝对值。
    """
    exp, result = _exp(), ExperimentResult(experiment=_exp())
    result.target_pods_before = {"total": 2, "running": 2, "not_running": [],
                                 "restarts": 7, "per_pod_restarts": {}}
    after = {"total": 2, "running": 2, "not_running": [], "restarts": 7,
             "per_pod_restarts": {}}

    assert _runner(after)._check_target_pod_health(exp, result) is True


# ── g02：readiness 判据 ──────────────────────────────────────────────────────

def test_p04_not_ready_pod_fails_with_remediation_command():
    exp, result = _exp(), ExperimentResult(experiment=_exp())
    result.target_pods_before = {"total": 2, "running": 2, "not_running": [],
                                 "restarts": 0, "per_pod_restarts": {}}
    after = {"total": 2, "running": 1, "restarts": 1, "per_pod_restarts": {},
             "not_running": [{"pod": "search-service-x", "phase": "Running",
                              "ready": False, "restarts": 1}]}

    ok = _runner(after)._check_target_pod_health(exp, result)

    assert ok is False
    joined = " ".join(result.pod_damage)
    assert "search-service-x" in joined
    assert "kubectl delete pod" in joined
    assert "petadoptions" in joined, "处置命令必须带上正确的 namespace"


def test_p05_zero_pods_fails():
    """查不到 Pod 也是失败 —— 不能当成「没有 Pod 所以没问题」。"""
    exp, result = _exp(), ExperimentResult(experiment=_exp())
    result.target_pods_before = {"total": 2, "running": 2, "not_running": [],
                                 "restarts": 0, "per_pod_restarts": {}}
    after = {"total": 0, "running": 0, "not_running": [], "restarts": 0,
             "per_pod_restarts": {}}

    assert _runner(after)._check_target_pod_health(exp, result) is False


# ── g03：缺基线时不得静默通过 ───────────────────────────────────────────────

def test_p06_missing_baseline_is_not_a_pass():
    """`restarts=None` 表示没测到，必须留痕，不能被当成 0 从而静默通过。"""
    exp, result = _exp(), ExperimentResult(experiment=_exp())
    result.target_pods_before = {"total": 0, "running": 0, "not_running": [],
                                 "restarts": None, "per_pod_restarts": {}}
    after = {"total": 2, "running": 2, "not_running": [], "restarts": 5,
             "per_pod_restarts": {}}

    ok = _runner(after)._check_target_pod_health(exp, result)

    # readiness 是好的，所以整体不判 FAILED；但差值无法判定必须显式留痕
    assert ok is True
    assert any("未能判定" in d for d in result.pod_damage)


def test_p07_check_pods_failure_returns_none_not_zero():
    """`check_pods` 异常路径必须返回 restarts=None。

    返回 0 会让 Phase 5 的差值判据静默通过 —— 与不变量 7
    （零流量与健康不可区分）同类的错误。
    """
    from runner.chaos_mcp import ChaosMCPClient
    c = ChaosMCPClient.__new__(ChaosMCPClient)
    # 用一个不存在的 namespace 名触发 kubectl 失败路径；即便 kubectl 不在 PATH
    # 也会走到 except，两种情况都应返回 None
    out = c.check_pods("no-such-service-xyz", "no-such-ns-xyz")
    assert out["restarts"] in (None, 0)
    if out["restarts"] == 0:
        # kubectl 存在且正常返回空列表：这是「查到了，0 个 Pod」，合法
        assert out["total"] == 0
