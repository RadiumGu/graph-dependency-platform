"""test_129_dr_runbook_workflow.py — 在真实的 Temporal 测试服务器上跑 DrRunbookWorkflow。

需要 temporalio。CI 里设了 `DR_REQUIRE_TEMPORAL=1`：缺依赖时**直接失败而不是跳过** ——
2026-10-06 之前它在 CI 里一直是静默 skip，看起来是绿的，实际一个都没跑。
本地：

    <venv>/bin/python -m pytest tests/test_129_dr_runbook_workflow.py

探针与 S3 用假实现替换 —— 名字与真实 activity 相同，worker 按名字派单。

## 守的是「人执行、Temporal 核实」的行为，不是代码文本

- 演练：任何 UNKNOWN → 结论「无法判断」；前置 check 不过 → 结论「不通过」
- 执行：越序的确认在进入 history 之前被拒；override 不写理由被拒；
  非法的裁决选项被拒；选 abort 就停；等不到人就停在原地（不替人做决定）
- 被取消也会导出记录
- 计划评审链：带 needs_human_input 的计划不能被批准；execute_steps 已退役、响亮拒绝
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
from temporalio.client import WorkflowUpdateFailedError  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

W = Path(__file__).resolve().parents[1] / "dr-plan-generator" / "worker"
if str(W) not in sys.path:
    sys.path.insert(0, str(W))

from activities import (  # noqa: E402
    ExecutionRecordInput,
    LoadedPlan,
    LoadPlanInput,
    PlanVersionInput,
    StepResult,
)
from plan_workflow import DrPlanWorkflow  # noqa: E402
from probe_catalog import PASS, PROBES, UNKNOWN, FAIL, activity_name  # noqa: E402
from probes import ProbeInput, ProbeResult  # noqa: E402
from runbook import FORMAT, parse_runbook, render_block  # noqa: E402
from runbook_workflow import DrRunbookWorkflow, RunbookRunInput  # noqa: E402

_T = Path(__file__).resolve().parent
if str(_T) not in sys.path:
    sys.path.insert(0, str(_T))
from _dr_live_parent import TestLiveParent  # noqa: E402  —— 单独成模块才能进沙箱，见该文件

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.filterwarnings("ignore::DeprecationWarning"),
]

from temporalio.common import SearchAttributeKey  # noqa: E402

#: 与 worker._search_attribute_types() 一致。
_SEARCH_ATTRIBUTES = [
    SearchAttributeKey.for_keyword_list("DRStepsExecuted"),
    SearchAttributeKey.for_keyword("DRPlanRef"),
    SearchAttributeKey.for_bool("DRDryRun"),
    SearchAttributeKey.for_keyword("DRStepName"),
    SearchAttributeKey.for_keyword("DRDecision"),
    SearchAttributeKey.for_keyword("DRPlanId"),
    SearchAttributeKey.for_int("DRPlanVersion"),
    SearchAttributeKey.for_keyword("DRPlanState"),
]

# ── 假的 S3 与探针 ─────────────────────────────────────────────────────────

BODIES: dict[tuple[str, int], str] = {}
RECORDS: list[dict[str, Any]] = []
#: 探针名 -> 依次返回的结论（用完后重复最后一个）。
VERDICTS: dict[str, list[str]] = {}
CALLS: list[str] = []


@activity.defn(name="put_plan_version")
async def fake_put(inp: PlanVersionInput) -> StepResult:
    BODIES[(inp.plan_id, inp.version)] = inp.body
    return StepResult(step="put_plan_version", executed=True, verified=True,
                      detail={"s3_key": f"plans/{inp.plan_id}/v{inp.version}.md"})


@activity.defn(name="load_plan_version")
async def fake_load(inp: LoadPlanInput) -> LoadedPlan:
    body = BODIES[(inp.plan_id, inp.version)]
    return LoadedPlan(plan_id=inp.plan_id, version=inp.version, sha256=inp.sha256,
                      body=body, s3_key="k")


@activity.defn(name="put_execution_record")
async def fake_record(inp: ExecutionRecordInput) -> StepResult:
    RECORDS.append(inp.record)
    return StepResult(step="put_execution_record", executed=True, verified=True)


def _fake_probe(name: str):
    @activity.defn(name=activity_name(name))
    async def _p(inp: ProbeInput) -> ProbeResult:
        CALLS.append(name)
        seq = VERDICTS.get(name) or [PASS]
        v = seq.pop(0) if len(seq) > 1 else seq[0]
        return ProbeResult(probe=name, verdict=v, question="q", means="m",
                           reason="" if v == PASS else f"假的 {v}")
    return _p


FAKE_PROBES = [_fake_probe(n) for n in PROBES]


def _body(steps: list[dict]) -> str:
    rb = parse_runbook({"format": FORMAT, "source_region": "ap-northeast-1",
                        "target_region": "ap-northeast-2", "steps": steps})
    return "# 测试计划\n\n" + render_block(rb) + "\n"


CHECK = {"id": "pre", "kind": "check", "title": "前置",
         "probes": [{"probe": "aurora_global_membership"}]}
SCALE = {"id": "scale", "kind": "manual", "title": "拉节点", "command": "aws eks …",
         "reversible": True, "rollback": "aws eks … 0",
         "pre": [{"probe": "eks_nodegroup_status"}],
         "post": [{"probe": "eks_ready_nodes", "params": {"min_ready": 2}}]}
DECIDE = {"id": "mode", "kind": "decision", "title": "裁决",
          "options": ["switchover", "failover-allow-data-loss", "abort"]}
PROMOTE = {"id": "promote", "kind": "manual", "title": "提升", "command": "aws rds …",
           "reversible": False, "irreversible_note": "回不去",
           "post": [{"probe": "aurora_writer_region", "params": {"expect_region": "ap-northeast-2"}}]}


@pytest_asyncio.fixture
async def env():
    BODIES.clear(); RECORDS.clear(); VERDICTS.clear(); CALLS.clear()
    # ⚠️ 不用 start_time_skipping()：那个精简服务器不实现 OperatorService，
    #    注册不了自定义 search attribute —— 而 upsert 一个未注册的属性会让
    #    activation **无限重试**（RUNNING 但永不前进）。本测试第一版就卡在这里
    #    600 秒没有任何输出，和 worker.py 里记的那次线上事故是同一个形状。
    e = await WorkflowEnvironment.start_local(search_attributes=_SEARCH_ATTRIBUTES)
    async with Worker(
        e.client, task_queue="tq",
        workflows=[DrRunbookWorkflow, DrPlanWorkflow, TestLiveParent],
        activities=[fake_put, fake_load, fake_record, *FAKE_PROBES],
    ):
        yield e
    await e.shutdown()


async def _start(env, steps, mode, **kw):
    """rehearsal 直接起；live 经一个父执行起（live 要求有父执行）。返回子执行的 handle。"""
    pid = f"p-{uuid.uuid4().hex[:6]}"
    BODIES[(pid, 1)] = _body(steps)
    inp = RunbookRunInput(plan_id=pid, version=1, sha256="x", mode=mode, requested_by="t", **kw)
    if mode != "live":
        return await env.client.start_workflow(
            DrRunbookWorkflow.run, inp, id=f"rb-{pid}", task_queue="tq")
    await env.client.start_workflow(TestLiveParent.run, inp, id=f"plan-{pid}", task_queue="tq")
    child = env.client.get_workflow_handle_for(DrRunbookWorkflow.run, f"plan-{pid}-exec-v1")
    for _ in range(100):
        try:
            await child.describe()
            return child
        except Exception:  # noqa: BLE001 —— 子执行还没被父执行起出来
            await asyncio.sleep(0.05)
    raise AssertionError("父执行没有起出子执行")


async def _until(h, kind: str, step: str, tries: int = 200) -> dict:
    for _ in range(tries):
        st = await h.query("runbook_state")
        a = st.get("awaiting")
        if a and a["type"] == kind and a["step_id"] == step:
            return st
        await asyncio.sleep(0.05)
    raise AssertionError(f"没等到 {kind}@{step}，最后状态：{st}")


# ── 演练 ──────────────────────────────────────────────────────────────────


class TestRehearsal:
    async def test_all_measurable_and_preflight_passes(self, env):
        VERDICTS["eks_ready_nodes"] = [FAIL]   # 后置探针此刻不过是预期的
        h = await _start(env, [CHECK, SCALE, PROMOTE], "rehearsal")
        r = await h.result()
        assert r.ok is True and r.status == "completed"
        assert RECORDS and RECORDS[-1]["mode"] == "rehearsal"

    async def test_unknown_anywhere_means_cannot_judge(self, env):
        VERDICTS["aurora_writer_region"] = [UNKNOWN]
        r = await (await _start(env, [CHECK, SCALE, PROMOTE], "rehearsal")).result()
        assert r.ok is None
        assert any("测不到" in f for f in r.findings)

    async def test_preflight_fail_means_not_ready(self, env):
        VERDICTS["aurora_global_membership"] = [FAIL]
        r = await (await _start(env, [CHECK, SCALE], "rehearsal")).result()
        assert r.ok is False
        assert any("前置条件不成立" in f for f in r.findings)

    async def test_rehearsal_never_waits_for_a_human(self, env):
        r = await (await _start(env, [CHECK, SCALE, DECIDE, PROMOTE], "rehearsal")).result()
        assert r.status == "completed"


# ── 执行 ──────────────────────────────────────────────────────────────────


class TestLive:
    async def test_happy_path_requires_a_human_at_every_change(self, env):
        h = await _start(env, [CHECK, SCALE, DECIDE, PROMOTE], "live")
        await _until(h, "confirm", "scale")
        assert "eks_ready_nodes" not in CALLS, "后置探针不该在人确认之前跑"

        with pytest.raises(WorkflowUpdateFailedError):
            # 越序：看着旧页面确认了别的步骤
            await h.execute_update("confirm_step", {"step_id": "promote", "operator": "a"})
        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update("confirm_step", {"step_id": "scale", "operator": " "})
        await h.execute_update("confirm_step", {"step_id": "scale", "operator": "alice"})

        await _until(h, "decision", "mode")
        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update("decide", {"step_id": "mode", "choice": "yolo",
                                              "operator": "a", "reason": "r"})
        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update("decide", {"step_id": "mode", "choice": "switchover",
                                              "operator": "a"})   # 缺理由
        await h.execute_update("decide", {"step_id": "mode", "choice": "switchover",
                                          "operator": "bob", "reason": "东京仍在线"})

        await _until(h, "confirm", "promote")
        await h.execute_update("confirm_step", {"step_id": "promote", "operator": "bob"})
        r = await h.result()
        assert r.ok is True and r.status == "completed"
        assert [s["step"] for s in r.steps if s.get("confirmed")] == ["scale", "promote"]
        assert RECORDS[-1]["executed_manual_steps"] == ["scale", "promote"]

    async def test_failed_post_probe_stops_and_override_needs_reason(self, env):
        VERDICTS["eks_ready_nodes"] = [FAIL]
        h = await _start(env, [SCALE, PROMOTE], "live")
        await _until(h, "confirm", "scale")
        await h.execute_update("confirm_step", {"step_id": "scale", "operator": "a"})
        st = await _until(h, "gate", "scale")
        assert st["awaiting"]["phase"] == "post"
        assert st["awaiting"]["failing"][0]["probe"] == "eks_ready_nodes"

        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update("resolve_gate", {"step_id": "scale", "phase": "post",
                                                    "action": "override", "operator": "a"})
        with pytest.raises(WorkflowUpdateFailedError):
            await h.execute_update("resolve_gate", {"step_id": "scale", "phase": "pre",
                                                    "action": "retry", "operator": "a"})
        await h.execute_update("resolve_gate", {"step_id": "scale", "phase": "post",
                                                "action": "override", "operator": "a",
                                                "reason": "节点刚起，k8s 还没上报"})
        await _until(h, "confirm", "promote")
        await h.execute_update("confirm_step", {"step_id": "promote", "operator": "a"})
        r = await h.result()
        # 有人 override 过 = 至少一个闸门没被核实 → 结论只能是「无法判断」。
        assert r.ok is None and r.overrides and r.overrides[0]["reason"]

    async def test_retry_reruns_the_probes(self, env):
        VERDICTS["eks_ready_nodes"] = [FAIL, PASS]
        h = await _start(env, [SCALE], "live")
        await _until(h, "confirm", "scale")
        await h.execute_update("confirm_step", {"step_id": "scale", "operator": "a"})
        await _until(h, "gate", "scale")
        await h.execute_update("resolve_gate", {"step_id": "scale", "phase": "post",
                                                "action": "retry", "operator": "a"})
        r = await h.result()
        assert r.ok is True and CALLS.count("eks_ready_nodes") == 2

    async def test_abort_decision_stops_before_the_irreversible_step(self, env):
        h = await _start(env, [DECIDE, PROMOTE], "live")
        await _until(h, "decision", "mode")
        await h.execute_update("decide", {"step_id": "mode", "choice": "abort",
                                          "operator": "a", "reason": "东京恢复了"})
        r = await h.result()
        assert r.status == "aborted" and r.ok is False
        assert not any(s.get("confirmed") for s in r.steps)
        assert "aurora_writer_region" not in CALLS

    async def test_no_reply_means_stall_not_proceed(self, env):
        h = await _start(env, [SCALE, PROMOTE], "live", human_wait_hours=0.001)   # 3.6 秒
        r = await h.result()
        assert r.status == "stalled" and r.ok is None
        assert not any(s.get("confirmed") for s in r.steps), "超时绝不能被当成「已确认」"

    async def test_live_without_parent_is_refused(self, env):
        """直接起 live 会绕过「批准 + 演练」的硬闸门 —— 必须被拒，且一个探针都不跑。"""
        pid = f"p-{uuid.uuid4().hex[:6]}"
        BODIES[(pid, 1)] = _body([CHECK, SCALE])
        h = await env.client.start_workflow(
            DrRunbookWorkflow.run,
            # human_wait_hours 设得很短：校验一旦失效，执行会走下去等人确认 ——
            # 默认 24 小时会让这条用例**卡住而不是失败**（2026-10-06 反向验证时实测），
            # 在 CI 里就是一个跑到超时的 job。设短之后失效时几秒内就 stalled → 断言失败。
            RunbookRunInput(plan_id=pid, version=1, sha256="x", mode="live", requested_by="t",
                            human_wait_hours=0.001),
            id=f"plan-{pid}-exec-v1", task_queue="tq",   # 名字像，但没有父执行
        )
        r = await h.result()
        assert r.status == "invalid" and r.ok is False
        assert any("只能由 DrPlanWorkflow" in f for f in r.findings)
        assert CALLS == [], "被拒的 live 不该跑任何探针"

    async def test_cancelled_run_still_exports_its_record(self, env):
        h = await _start(env, [SCALE, PROMOTE], "live")
        await _until(h, "confirm", "scale")
        await h.cancel()
        with pytest.raises(Exception):
            await h.result()
        assert RECORDS and RECORDS[-1]["status"] == "cancelled"


# ── 计划评审链 ────────────────────────────────────────────────────────────


class TestPlanLifecycle:
    async def test_placeholder_blocks_approval_and_drill_runs_rehearsal(self, env):
        draft = dict(SCALE, id="front", needs_human_input=True, command=None)
        h = await env.client.start_workflow(
            DrPlanWorkflow.run,
            # ⚠️ failover_task_queue 必须与 worker 监听的队列一致。第一版没传，
            #    子执行被派到默认的 dr-plan-queue —— 那里没有 worker，于是它
            #    **RUNNING 但永不前进**，连 query 都超时。这正是 worker.py 里
            #    记着的「启动成功不等于在执行」，在测试里又遇到了一次。
            {"plan_id": "lc", "body": _body([CHECK, draft]), "author": "gen",
             "failover_task_queue": "tq"},
            id="plan-lc", task_queue="tq",
        )
        await asyncio.sleep(0.3)
        with pytest.raises(WorkflowUpdateFailedError) as e:
            await h.execute_update("approve_plan", {"version": 1, "approver": "a"})
        assert "needs_human_input" in str(e.value.cause)

        with pytest.raises(WorkflowUpdateFailedError):
            # 块不合法的修订在进入 history 之前就被拒
            await h.execute_update("revise_plan", {"body": "```dr-runbook\n{}\n```",
                                                   "author": "a", "reason": "r"})
        await h.execute_update("revise_plan", {"body": _body([CHECK, SCALE]),
                                               "author": "alice", "reason": "填上前门"})
        await h.execute_update("approve_plan", {"version": 2, "approver": "bob"})
        await h.execute_update("start_drill", {"requested_by": "bob"})
        for _ in range(200):
            st = await h.query("plan_state")
            if st["state"] == "drilled":
                break
            await asyncio.sleep(0.05)
        assert st["state"] == "drilled", st

        with pytest.raises(WorkflowUpdateFailedError) as e:
            await h.execute_update("authorize_execution", {"authorized_by": "bob",
                                                           "execute_steps": ["scale"]})
        assert "已于 2026-10-05 退役" in str(e.value.cause)
        r = await h.execute_update("authorize_execution", {"authorized_by": "bob"})
        assert r["child_workflow_id"].endswith("-exec-v2")
