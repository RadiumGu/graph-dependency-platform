"""
test_80_temporal_executor_skeleton.py — Temporal 执行引擎骨架的门禁。

这个骨架**刻意没实现 execute()**,所以门禁守的不是功能,而是三件
「实现时不许走回头路」的事:

1. 选了 temporal 引擎就不许静默降级到 strands
   (本仓库 direct 回退悄悄当了五个月实际路径,同一个坑不踩第二次)
2. 未实现就要抛,不许返回一个看起来成功的空报告
3. 「任务队列上有没有 worker」在服务端没报时必须是 inconclusive,
   不许当成「没有」
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

DR = Path(__file__).resolve().parents[1] / "dr-plan-generator"
if str(DR) not in sys.path:
    sys.path.insert(0, str(DR))


def test_temporal_engine_is_selectable_and_never_silently_falls_back(monkeypatch):
    """DR_EXECUTOR_ENGINE=temporal 必须真的拿到 TemporalExecutor。"""
    monkeypatch.setenv("DR_EXECUTOR_ENGINE", "temporal")
    from executor_factory import make_dr_executor

    ex = make_dr_executor(dry_run=True)
    # 关键:拿到的必须是 temporal,不是 strands。
    assert ex.ENGINE_NAME == "temporal", (
        f"选了 temporal 却拿到 {ex.ENGINE_NAME} —— 这就是静默降级，"
        "本仓库已经吃过五个月的苦头"
    )


def test_dry_run_gate_still_double_locked(monkeypatch):
    """双重闸门不能因为新引擎而松掉。"""
    monkeypatch.setenv("DR_EXECUTOR_ENGINE", "temporal")
    from executor_factory import make_dr_executor

    # env 说 true → 无论代码参数怎么传都必须 dry_run
    monkeypatch.setenv("DR_EXECUTOR_DRY_RUN", "true")
    assert make_dr_executor(dry_run=False).dry_run is True

    # 两个都 false 才真执行
    monkeypatch.setenv("DR_EXECUTOR_DRY_RUN", "false")
    assert make_dr_executor(dry_run=False).dry_run is False
    # 代码参数单独为 true 也足够拦住
    assert make_dr_executor(dry_run=True).dry_run is True


def test_execute_raises_rather_than_returning_empty_success(monkeypatch):
    """骨架未实现时必须抛,不许返回一个看起来成功的报告。"""
    monkeypatch.setenv("DR_EXECUTOR_ENGINE", "temporal")
    from executor_temporal import TemporalExecutor

    ex = TemporalExecutor(dry_run=True)
    with pytest.raises(NotImplementedError) as e:
        ex.execute(plan=None)  # type: ignore[arg-type]
    # 异常里要说明卡在哪两个决定上,否则接手的人不知道缺什么。
    msg = str(e.value)
    assert "写权限" in msg or "FailoverGlobalCluster" in msg
    assert "保留期" in msg or "86400" in msg


class TestPollerInterpretation:
    """服务端没报 poller 时必须是 inconclusive,不是「没有」。"""

    def test_pollers_field_absent_is_inconclusive(self):
        from executor_temporal import TemporalExecutor

        # 实测形状:没有 worker 时 pollers 字段整个不存在。
        absent = {"effectiveRateLimit": {"requestsPerSecond": 4000},
                  "versioningInfo": {"currentVersion": "__unversioned__"}}
        got = TemporalExecutor.interpret_task_queue_pollers(absent)
        assert got is None, (
            "服务端没报 pollers 就必须是 None(无法判断)。"
            "返回 False 等于断言「没有 worker」,而字段缺失时"
            "「队列不存在」「无 worker」「有 workflow 在等但无 worker」"
            "三种状态无法区分 —— 第三种是切换卡死"
        )
        # 尤其不能是 False。
        assert got is not False

    def test_pollers_present_and_nonempty_is_true(self):
        from executor_temporal import TemporalExecutor

        present = {"pollers": [{"identity": "worker@host"}]}
        assert TemporalExecutor.interpret_task_queue_pollers(present) is True

    def test_empty_array_is_a_real_measurement_of_zero(self):
        from executor_temporal import TemporalExecutor

        # 空数组是有效测量:服务端确实说了「0 个」。
        assert TemporalExecutor.interpret_task_queue_pollers({"pollers": []}) is False


def test_start_args_carry_the_fields_a_failover_cannot_omit():
    """切换 workflow 的启动参数不许缺超时 / identity / 幂等键。"""
    from executor_temporal import TemporalExecutor

    ex = TemporalExecutor(dry_run=True)
    args = ex._build_start_args(plan=None, plan_ref="plan-123")  # type: ignore[arg-type]

    # 没有超时,切换卡住就永远挂着。
    assert args["execution_timeout"], "缺 execution_timeout"
    assert args["run_timeout"], "缺 run_timeout"
    # 事后要查出是谁发起的。
    assert args["identity"]
    # 幂等:重试不能起出第二个切换流程。
    assert args["request_id"]

    # ⚠️ 计划正文不许进 input —— 保留期 1 天,且 payload 上限拿不到。
    assert "plan_ref" in args["input"]
    body_like = [v for v in args["input"].values() if isinstance(v, str) and len(v) > 500]
    assert not body_like, "计划正文不该塞进 input,只放引用"
