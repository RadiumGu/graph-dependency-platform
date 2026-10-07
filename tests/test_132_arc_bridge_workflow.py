"""test_132_arc_bridge_workflow.py — 在真实的 Temporal 本地服务器上跑 ArcPlanExecutionWorkflow。

ARC、CloudTrail、探针、S3 都用同名假 activity 替换。守的是这几条行为：

- 前置探针有一项不是 PASS → **不起** ARC 执行
- 没有后置探针 → 拒绝（只信 ARC 自己的 completed 正是要避免的）
- ARC 停在 pendingManualApproval 时，查询里能看到在等哪一步；Temporal 不替人批
- 等人超过上限 → stalled，**不取消** ARC 执行
- ARC completed 但后置探针 FAIL → 结论是 arc-completed-but-postcheck-failed，不是成功
- 进入过审批 → 从 CloudTrail 查审批人并写进记录
- 起执行用 workflow id 做幂等键
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

if os.environ.get("DR_REQUIRE_TEMPORAL") == "1":
    import temporalio  # noqa: F401 —— CI 里缺依赖必须是红的，不能是 skip
else:
    temporalio = pytest.importorskip("temporalio")
pytest_asyncio = pytest.importorskip("pytest_asyncio")

from temporalio import activity  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

W = Path(__file__).resolve().parents[1] / "dr-plan-generator" / "worker"
if str(W) not in sys.path:
    sys.path.insert(0, str(W))

from activities import ExecutionRecordInput, StepResult  # noqa: E402
from arc_bridge import (  # noqa: E402
    ApproverLookupInput,
    ArcExecutionView,
    ArcGetInput,
    ArcPlanExecutionWorkflow,
    ArcRunInput,
    ArcStartInput,
    ArcStartResult,
)
from probe_catalog import FAIL, PASS, PROBES, activity_name  # noqa: E402
from probes import ProbeInput, ProbeResult  # noqa: E402

pytestmark = [pytest.mark.asyncio, pytest.mark.filterwarnings("ignore::DeprecationWarning")]

PLAN = "arn:aws:arc-region-switch::123456789012:plan/petsite-dr-poc:abc"

STARTS: list[ArcStartInput] = []
#: 每次 get_plan_execution 依次返回的状态；最后一个会一直重复
ARC_STATES: list[str] = []
RECORDS: list[dict[str, Any]] = []
VERDICTS: dict[str, list[str]] = {}
APPROVERS: list[dict[str, Any]] = []


def _steps(state: str) -> list[dict[str, Any]]:
    if state == "pendingManualApproval":
        return [{"name": "approve-prepare", "status": "pendingApproval"},
                {"name": "prepare-activating-region", "status": "notStarted"}]
    if state in ("completed", "inProgress"):
        return [{"name": "approve-prepare", "status": "completed"},
                {"name": "prepare-activating-region",
                 "status": "completed" if state == "completed" else "running"}]
    return [{"name": "approve-prepare", "status": "canceled"}]


START_FAILS: list[str] = []


@activity.defn(name="arc.start_plan_execution")
async def fake_start(inp: ArcStartInput) -> ArcStartResult:
    if START_FAILS:
        from temporalio.exceptions import ApplicationError
        raise ApplicationError(START_FAILS[0], non_retryable=True)
    STARTS.append(inp)
    return ArcStartResult(execution_id="exec-1", plan_version="v1",
                          activate_region=inp.target_region, deactivate_region="ap-northeast-1")


@activity.defn(name="arc.get_plan_execution")
async def fake_get(inp: ArcGetInput) -> ArcExecutionView:
    s = ARC_STATES.pop(0) if len(ARC_STATES) > 1 else ARC_STATES[0]
    return ArcExecutionView(state=s, steps=_steps(s))


@activity.defn(name="arc.list_execution_events")
async def fake_events(inp: ArcGetInput) -> list[dict[str, Any]]:
    return [{"type": "stepPendingApproval", "step": "approve-prepare"}]


@activity.defn(name="arc.lookup_approver")
async def fake_approver(inp: ApproverLookupInput) -> list[dict[str, Any]]:
    return list(APPROVERS)


@activity.defn(name="put_execution_record")
async def fake_record(inp: ExecutionRecordInput) -> StepResult:
    RECORDS.append(inp.record)
    return StepResult(step="put_execution_record", executed=True, verified=True)


def _fake_probe(name: str):
    @activity.defn(name=activity_name(name))
    async def _p(inp: ProbeInput) -> ProbeResult:
        seq = VERDICTS.get(name) or [PASS]
        v = seq.pop(0) if len(seq) > 1 else seq[0]
        return ProbeResult(probe=name, verdict=v, question="q", means="m",
                           reason="" if v == PASS else f"假的 {v}")
    return _p


@pytest_asyncio.fixture
async def env():
    for x in (STARTS, ARC_STATES, RECORDS, APPROVERS, START_FAILS):
        x.clear()
    VERDICTS.clear()
    e = await WorkflowEnvironment.start_local()
    async with Worker(
        e.client, task_queue="tq", workflows=[ArcPlanExecutionWorkflow],
        activities=[fake_start, fake_get, fake_events, fake_approver, fake_record,
                    *[_fake_probe(n) for n in PROBES]],
    ):
        yield e
    await e.shutdown()


PRE = [{"probe": "eks_nodegroup_status", "params": {"expect_desired": 0}}]
POST = [{"probe": "eks_ready_nodes", "params": {"min_ready": 1}}]


def _inp(**kw) -> ArcRunInput:
    base = dict(plan_arn=PLAN, target_region="ap-northeast-2", prechecks=PRE, postchecks=POST,
                poll_seconds=1, postcheck_attempts=2, postcheck_interval_seconds=1,
                lookup_approver_minutes=1)
    base.update(kw)
    return ArcRunInput(**base)


async def _run(env, inp: ArcRunInput, wid: str | None = None):
    wid = wid or f"arc-t-{uuid.uuid4().hex[:6]}"
    h = await env.client.start_workflow(ArcPlanExecutionWorkflow.run, inp, id=wid, task_queue="tq")
    return wid, h


class TestBridge:
    async def test_precheck_not_pass_means_arc_is_never_started(self, env):
        VERDICTS["eks_nodegroup_status"] = [FAIL]
        ARC_STATES.append("completed")
        _, h = await _run(env, _inp())
        r = await h.result()
        assert r["verdict"] == "precheck-not-pass"
        assert STARTS == [], "前置没过就起了 ARC 执行"

    async def test_no_postchecks_is_refused(self, env):
        ARC_STATES.append("completed")
        _, h = await _run(env, _inp(postchecks=[]))
        r = await h.result()
        assert r["verdict"] == "invalid-input" and STARTS == []

    async def test_happy_path_verified_with_approver_from_cloudtrail(self, env):
        ARC_STATES.extend(["pendingManualApproval", "pendingManualApproval", "inProgress", "completed"])
        APPROVERS.append({"principal": "arn:aws:sts::1:assumed-role/petsite-dr-poc-approver/alice",
                          "mfa": "true", "approval": "approve"})
        wid, h = await _run(env, _inp())
        r = await h.result()
        assert r["verdict"] == "verified"
        assert r["approvers"][0]["mfa"] == "true"
        assert STARTS[0].client_token == wid, "幂等键必须是 workflow id"
        assert any(t.get("state") == "pendingManualApproval" for t in r["timeline"])
        assert RECORDS and RECORDS[-1]["verdict"] == "verified"

    async def test_awaiting_step_is_visible_while_arc_waits_for_a_human(self, env):
        ARC_STATES.append("pendingManualApproval")
        _, h = await _run(env, _inp(max_wait_hours=1.0))
        for _ in range(200):
            st = await h.query("arc_state")
            if st["awaiting_approval_step"]:
                break
            await asyncio.sleep(0.05)
        assert st["awaiting_approval_step"] == "approve-prepare"
        assert st["phase"] == "waiting-arc"
        await h.cancel()

    async def test_no_human_means_stall_not_cancel(self, env):
        ARC_STATES.append("pendingManualApproval")
        _, h = await _run(env, _inp(max_wait_hours=0.0005))   # ≈ 2 秒
        r = await h.result()
        assert r["phase"] == "stalled" and r["verdict"] == "arc-not-finished-in-time"
        assert r["awaiting_approval_step"] == "approve-prepare"

    async def test_arc_success_but_postcheck_fail_is_not_success(self, env):
        ARC_STATES.append("completed")
        VERDICTS["eks_ready_nodes"] = [FAIL]
        _, h = await _run(env, _inp())
        r = await h.result()
        assert r["verdict"] == "arc-completed-but-postcheck-failed"

    async def test_declined_is_reported_with_who_declined(self, env):
        ARC_STATES.extend(["pendingManualApproval", "canceled"])
        APPROVERS.append({"principal": "arn:aws:sts::1:assumed-role/x/bob", "approval": "decline"})
        _, h = await _run(env, _inp())
        r = await h.result()
        assert r["verdict"] == "arc-canceled"
        assert r["approvers"][0]["approval"] == "decline"
        assert r["postchecks"] == [], "ARC 没成功就不该跑后置探针"

    async def test_start_failure_is_a_verdict_and_still_exports(self, env):
        START_FAILS.append('Unknown parameter in input: "clientToken"')
        ARC_STATES.append("completed")
        _, h = await _run(env, _inp())
        r = await h.result()
        assert r["verdict"] == "arc-start-failed"
        assert RECORDS and RECORDS[-1]["verdict"] == "arc-start-failed"


# ── activity 本身：在「旧 SDK 没有 clientToken」的条件下 ──────────────────


class _Op:
    def __init__(self, members):
        self.input_shape = type("S", (), {"members": {m: None for m in members}})()


class _StubArc:
    """模拟 worker 上 boto3==1.40.47 的 ARC 客户端：StartPlanExecution 不认 clientToken。"""

    def __init__(self, existing=None, with_token=False):
        self.existing = existing or []
        self.calls: list[dict] = []
        ms = ["planArn", "targetRegion", "action", "mode", "comment", "latestVersion"]
        if with_token:
            ms.append("clientToken")
        model = type("M", (), {"operation_model": lambda _s, op: _Op(ms)})()
        self.meta = type("Meta", (), {"service_model": model})()
        self._ms = set(ms)

    def list_plan_executions(self, **kw):
        return {"items": self.existing}

    def start_plan_execution(self, **kw):
        bad = set(kw) - self._ms
        if bad:
            raise ValueError(f"Unknown parameter in input: {sorted(bad)}")
        self.calls.append(kw)
        return {"executionId": "new-1", "planVersion": "v1"}


class TestStartActivityOnOldSdk:
    def _inp(self):
        return ArcStartInput(plan_arn=PLAN, target_region="ap-northeast-2", action="activate",
                             mode="graceful", comment="[temporal wf-9] x", client_token="wf-9")

    async def test_old_sdk_does_not_get_client_token(self, monkeypatch):
        import arc_bridge
        stub = _StubArc(with_token=False)
        monkeypatch.setattr(arc_bridge, "_arc", lambda: stub)
        r = await arc_bridge.arc_start_plan_execution(self._inp())
        assert r.execution_id == "new-1" and "clientToken" not in stub.calls[0]

    async def test_new_sdk_gets_client_token(self, monkeypatch):
        import arc_bridge
        stub = _StubArc(with_token=True)
        monkeypatch.setattr(arc_bridge, "_arc", lambda: stub)
        await arc_bridge.arc_start_plan_execution(self._inp())
        assert stub.calls[0]["clientToken"] == "wf-9"

    async def test_retry_finds_the_existing_execution(self, monkeypatch):
        import arc_bridge
        stub = _StubArc(existing=[{"executionId": "old-1", "comment": "[temporal wf-9] x"}])
        monkeypatch.setattr(arc_bridge, "_arc", lambda: stub)
        r = await arc_bridge.arc_start_plan_execution(self._inp())
        assert r.execution_id == "old-1" and stub.calls == [], "重试起了第二个执行"
