"""
test_81_dr_worker_contract.py — DR worker 的门禁。

这些断言守的不是「代码能跑」,而是几条**实测踩出来的**判据:

1. worker 的并发数不许退回 1 —— 那会造成队头阻塞(实测把真切换拖了 3 分钟)
2. 互斥必须靠 workflow ID,所以发起侧不许传 ALLOW_DUPLICATE
3. worker 连的是 gRPC 7233,不是 HTTP API 7243,也不是 UI 8080
4. 决策点不许有默认值 —— 超时要失败,不许自动选丢数据的那条路

worker 目录下的模块依赖 temporalio（只装在那台 EC2 上），本机没有，
所以这里**读源码文本**而不是 import。判据是「源码里写着什么」,
对这几条契约来说够了,而且不需要在 CI 里装 temporalio。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "dr-plan-generator" / "worker"
DR = ROOT / "dr-plan-generator"
if str(DR) not in sys.path:
    sys.path.insert(0, str(DR))


def _src(name: str) -> str:
    p = WORKER / name
    assert p.exists(), f"{p} 不存在"
    return p.read_text(encoding="utf-8")


class TestConcurrencyIsNotOne:
    """并发数不许退回 1。"""

    def test_workflow_task_concurrency_above_one(self):
        m = re.search(r"max_concurrent_workflow_tasks\s*=\s*(\d+)", _src("worker.py"))
        assert m, "找不到 max_concurrent_workflow_tasks 设置"
        n = int(m.group(1))
        assert n > 1, (
            f"max_concurrent_workflow_tasks={n} 会造成队头阻塞。"
            "实测：队列上有一个永久失败的 workflow 时（Temporal 无限重试它的"
            "workflow task），唯一槽位被占住，真切换被拖了 3 分钟并留下"
            "WORKFLOW_TASK_TIMED_OUT —— 即使把 workflowTaskTimeout 调到 60s "
            "也照样超时。「一次只做一个切换」要靠 workflow ID，不是靠这个数。"
        )

    def test_reason_is_documented_next_to_the_number(self):
        # 判据本身要留在代码里，否则下一个人会把它改回 1。
        src = _src("worker.py")
        assert "队头阻塞" in src
        assert "workflow ID" in src


class TestMutualExclusionViaWorkflowId:
    """互斥靠 workflow ID —— 发起侧不许拆掉它。"""

    def test_start_args_do_not_pass_allow_duplicate(self):
        from executor_temporal import TemporalExecutor

        args = TemporalExecutor(dry_run=True)._build_start_args(
            plan=None,  # type: ignore[arg-type]
            plan_ref="p1",
        )
        # 传 ALLOW_DUPLICATE 会让同一个 ID 可以并发启动，互斥就没了。
        assert "id_reuse_policy" not in args or "ALLOW_DUPLICATE" not in str(
            args.get("id_reuse_policy", "")
        ), "传 ALLOW_DUPLICATE 会拆掉「同一份计划不许重复触发」这层互斥"

    def test_workflow_id_derives_from_plan_ref(self):
        from executor_temporal import TemporalExecutor

        ex = TemporalExecutor(dry_run=True)
        a = ex._build_start_args(plan=None, plan_ref="alpha")  # type: ignore[arg-type]
        b = ex._build_start_args(plan=None, plan_ref="beta")  # type: ignore[arg-type]
        assert a["workflow_id"] != b["workflow_id"]
        assert "alpha" in a["workflow_id"]


class TestWorkerUsesGrpcPort:
    """worker 走 gRPC，不是 HTTP API。"""

    def test_default_target_is_7233(self):
        src = _src("worker.py")
        m = re.search(r'DR_TEMPORAL_TARGET["\']?\s*,\s*["\']([^"\']+)', src)
        assert m, "找不到 DR_TEMPORAL_TARGET 的默认值"
        target = m.group(1)
        assert target.endswith(":7233"), (
            f"worker 默认连 {target}。worker 用 gRPC(7233)；"
            "7243 是 HTTP API（temporal-mcp 用的），8080 是 Web UI。"
        )

    def test_systemd_unit_also_uses_7233(self):
        unit = _src("dr-worker.service")
        assert "DR_TEMPORAL_TARGET=localhost:7233" in unit
        assert ":7243" not in unit.split("Environment=DR_TEMPORAL_TARGET")[1][:40]


class TestDecisionPointHasNoDefault:
    """决策点不许有默认值：等不到人就停住，不替人选。

    2026-10-05 起决策点在 DrRunbookWorkflow 的 decision 步骤里（原来在已退役的
    DrFailoverWorkflow.database_decision）。行为由 test_129 在真实测试服务器上守；
    这里守代码里不能消失的那几处。
    """

    def test_timeout_stalls_instead_of_choosing(self):
        src = _src("runbook_workflow.py")
        i = src.index("except asyncio.TimeoutError:")
        seg = src[i : i + 700]
        assert '"stalled"' in seg, "等人超时必须停住"
        assert "没有替人做决定" in seg
        # 最危险的默认值：超时后自动选一个裁决。
        assert "allow" not in seg and "switchover" not in seg

    def test_choice_must_be_one_of_the_declared_options(self):
        src = _src("runbook_workflow.py")
        assert 'if args.get("choice") not in options:' in src

    def test_illegal_input_is_rejected_before_history(self):
        """非法裁决由 Update validator 拒掉 —— 不再有没 validator 的 signal 通道。"""
        src = _src("runbook_workflow.py")
        assert "@decide.validator" in src
        assert "@workflow.signal" not in src, (
            "signal 没有 validator，非法值照样落进 history —— 执行工作流只走 Update"
        )


class TestVerifiedIsThreeState:
    """verified / verdict 是三态：不许用 False/FAIL 冒充「没测到」。"""

    def test_step_result_verified_allows_none(self):
        src = _src("activities.py")
        assert "verified: bool | None" in src, "verified 必须允许 None"
        assert "inconclusive_reason" in src, "无法判断时要写明原因"

    def test_probe_exceptions_become_unknown_not_fail(self):
        src = _src("probes.py")
        i = src.index("def _run(")
        seg = src[i : i + 1600]
        assert "except Exception" in seg and "verdict=UNKNOWN" in seg
        assert "FAIL 必须是测到了一个不合格的值" in src
