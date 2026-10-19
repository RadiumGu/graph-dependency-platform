"""test_136_atomic_steps.py — 一步一个动作、每个变更单独确认、按裁决二选一由工作流选定（2026-10-18）。

负责人要求：「这些被执行的具体步骤要详细体现出来，而不是挤在一个里」，且「每个原子步骤都要人确认」。
守四件事：
1. 格式：`when` 只能引用前面的 decision、选项必须存在、不能以 abort 为条件
2. 批准：一个 manual 步骤有多条命令就不能批准（解析时不拒，保证旧正文回放不变）
3. 生成器：每个只读断言一步、每个清单一步、Aurora 两种方式是两个条件步骤
4. 行为（真实 Temporal）：选 switchover 时 failover 那一步被跳过且不等人确认；
   每个变更步骤都单独等一次 confirm_step
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
W = ROOT / "dr-plan-generator" / "worker"
for p in (str(W), str(ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from runbook import (  # noqa: E402
    FORMAT, RunbookError, approval_problems, command_count, parse_runbook, render_block, summarize,
)
from runbook_draft import DraftFacts, draft_tokyo_to_seoul, render_body  # noqa: E402

DEC = {"id": "mode", "kind": "decision", "title": "怎么切", "options": ["a", "b", "abort"]}


def _rb(steps):
    return parse_runbook({"format": FORMAT, "source_region": "ap-northeast-1",
                          "target_region": "ap-northeast-2", "steps": steps})


def _manual(sid, cmd="echo x", **kw):
    return {"id": sid, "kind": "manual", "title": sid, "command": cmd, "reversible": True,
            "rollback": "echo undo", "post": [{"probe": "eks_nodegroup_status"}], **kw}


class TestWhen:
    def test_valid_condition_parses(self):
        rb = _rb([DEC, _manual("do-a", when={"step": "mode", "in": ["a"]})])
        assert rb.steps[1].when == {"step": "mode", "in": ["a"]}

    @pytest.mark.parametrize("when,msg", [
        ({"step": "nope", "in": ["a"]}, "排在它前面的 decision"),
        ({"step": "mode", "in": ["c"]}, "不在 mode 的选项"),
        ({"step": "mode", "in": ["abort"]}, "不能以 abort 为条件"),
        ({"step": "mode"}, "when 必须是"),
    ])
    def test_bad_conditions_are_rejected(self, when, msg):
        with pytest.raises(RunbookError) as e:
            _rb([DEC, _manual("do-a", when=when)])
        assert any(msg in p for p in e.value.problems), e.value.problems

    def test_condition_must_point_backwards(self):
        with pytest.raises(RunbookError):
            _rb([_manual("do-a", when={"step": "mode", "in": ["a"]}), DEC])

    def test_old_bodies_without_when_parse_identically(self):
        rb = _rb([_manual("x")])
        assert rb.steps[0].when is None and "when" not in rb.to_dict()["steps"][0]


class TestOneActionPerStep:
    def test_command_count(self):
        assert command_count("aws a \\\n  --b c") == 1
        assert command_count("# 注释\naws a") == 1
        assert command_count("按裁决执行其中一条：\n  a\n  b") == 3

    def test_multi_command_step_cannot_be_approved(self):
        body = "x\n" + render_block(_rb([_manual("two", cmd="aws a\naws b")])) + "\n"
        ps = approval_problems(body)
        assert any("一步只能有一个动作" in p for p in ps), ps

    def test_but_it_still_parses(self):
        """解析结果决定 workflow 的 command 序列 —— 旧正文必须照样能解析（回放）。"""
        _rb([_manual("two", cmd="aws a\naws b")])


def _facts(**kw):
    f = DraftFacts(
        secondary_cluster_arn="arn:aws:rds:ap-northeast-2:1:cluster:sec", dynamodb_table="t",
        images_by_digest=[("r1", "sha256:" + "a" * 64)], images_by_tag=[("r2", "v1")],
        lambdas=["dr-korea-f1", "dr-korea-f2"], target_groups=["dr-korea-petsite-tg", "dr-korea-x-tg"],
        health_urls=["http://h/health"],
        workload_manifests=["infra/dr-korea/petsite-korea-drill.yaml", "infra/dr-korea/15-korea-workloads.yaml"],
        workloads=[("infra/dr-korea/workloads/00-shared.yaml", "共享", [], []),
                   ("infra/dr-korea/petsite-korea-drill.yaml", "petsite", ["dr-korea-petsite-tg"], ["petsite-deployment"]),
                   ("infra/dr-korea/workloads/petfood.yaml", "petfood", [], ["petfood"])],
    )
    for k, v in kw.items():
        setattr(f, k, v)
    return f


class TestGeneratorIsAtomic:
    def test_every_check_step_asks_exactly_one_thing(self):
        for s in draft_tokyo_to_seoul(_facts()).steps:
            if s.kind == "check":
                assert len(s.probes) == 1, (s.id, [p.probe for p in s.probes])

    def test_every_manual_step_is_one_command(self):
        for s in draft_tokyo_to_seoul(_facts()).steps:
            if s.kind == "manual" and s.command:
                assert command_count(s.command) == 1, s.id

    def test_one_step_per_service_each_checking_its_own_deployment(self):
        steps = [s for s in draft_tokyo_to_seoul(_facts()).steps if s.id.startswith("apply-workload-")]
        assert len(steps) == 3
        assert steps[0].command.endswith("00-shared.yaml"), "共享对象必须最先部署"
        assert [(p.probe, p.params.get("name") or p.params.get("target_group")) for p in steps[1].post] == [
            ("k8s_deployment_available", "petsite-deployment"), ("alb_target_health", "dr-korea-petsite-tg")]
        assert [(p.probe, p.params.get("name")) for p in steps[2].post] == [("k8s_deployment_available", "petfood")]

    def test_aurora_branches_are_two_conditional_steps(self):
        rb = draft_tokyo_to_seoul(_facts())
        sw = next(s for s in rb.steps if s.id == "promote-aurora-switchover")
        fo = next(s for s in rb.steps if s.id == "promote-aurora-failover")
        assert sw.when == {"step": "aurora-mode", "in": ["switchover"]} and sw.command.endswith("--switchover")
        assert fo.when == {"step": "aurora-mode", "in": ["failover-allow-data-loss"]}
        assert fo.command.endswith("--allow-data-loss")
        assert "--switchover" not in fo.command and "--allow-data-loss" not in sw.command

    def test_every_step_has_a_phase(self):
        assert all(s.phase for s in draft_tokyo_to_seoul(_facts()).steps)

    def test_only_front_door_blocks_approval(self):
        f = _facts()
        body = render_body(f, draft_tokyo_to_seoul(f), generated_at="t", generator="g")
        ps = approval_problems(body)
        assert len(ps) == 1 and "switch-front-door" in ps[0], ps

    def test_summary_lists_each_probe_on_its_own_line_with_its_question(self):
        rb = draft_tokyo_to_seoul(_facts())
        lines = summarize(rb)
        assert any("清单引用的那个镜像 digest 在首尔 ECR 里是否存在" in l for l in lines)
        assert sum(1 for l in lines if l.startswith("   - ") and "`" in l and l.lstrip().startswith("- ") and "：" in l) \
            >= sum(len(s.all_probes()) for s in rb.steps)


# ── 行为：真实 Temporal 本地服务器 ─────────────────────────────────────────

if os.environ.get("DR_REQUIRE_TEMPORAL") == "1":
    import temporalio  # noqa: F401
    import pytest_asyncio
else:
    temporalio = pytest.importorskip("temporalio")
    pytest_asyncio = pytest.importorskip("pytest_asyncio")

from temporalio import activity  # noqa: E402
from temporalio.common import SearchAttributeKey  # noqa: E402
from temporalio.testing import WorkflowEnvironment  # noqa: E402
from temporalio.worker import Worker  # noqa: E402

from _dr_live_parent import TestLiveParent  # noqa: E402
from activities import ExecutionRecordInput, LoadedPlan, LoadPlanInput, StepResult  # noqa: E402
from probe_catalog import PASS, PROBES, activity_name  # noqa: E402
from probes import ProbeInput, ProbeResult  # noqa: E402
from runbook_workflow import DrRunbookWorkflow, RunbookRunInput  # noqa: E402

_SA = [
    SearchAttributeKey.for_keyword_list("DRStepsExecuted"), SearchAttributeKey.for_keyword("DRPlanRef"),
    SearchAttributeKey.for_bool("DRDryRun"), SearchAttributeKey.for_keyword("DRStepName"),
    SearchAttributeKey.for_keyword("DRDecision"), SearchAttributeKey.for_keyword("DRPlanId"),
    SearchAttributeKey.for_int("DRPlanVersion"), SearchAttributeKey.for_keyword("DRPlanState"),
]
BODIES: dict = {}


@activity.defn(name="load_plan_version")
async def _load(inp: LoadPlanInput) -> LoadedPlan:
    return LoadedPlan(plan_id=inp.plan_id, version=inp.version, sha256=inp.sha256,
                      body=BODIES[inp.plan_id], s3_key="k")


@activity.defn(name="put_execution_record")
async def _rec(inp: ExecutionRecordInput) -> StepResult:
    return StepResult(step="put_execution_record", executed=True, verified=True)


def _probe(name):
    @activity.defn(name=activity_name(name))
    async def _p(inp: ProbeInput) -> ProbeResult:
        return ProbeResult(probe=name, verdict=PASS, question="q", means="m")
    return _p


@pytest_asyncio.fixture
async def env():
    e = await WorkflowEnvironment.start_local(search_attributes=_SA)
    async with Worker(e.client, task_queue="tq", workflows=[DrRunbookWorkflow, TestLiveParent],
                      activities=[_load, _rec, *[_probe(n) for n in PROBES]]):
        yield e
    await e.shutdown()


@pytest.mark.asyncio
async def test_unchosen_branch_is_skipped_and_each_change_is_confirmed_separately(env):
    steps = [
        _manual("prep-1"), _manual("prep-2"),
        DEC,
        _manual("do-a", when={"step": "mode", "in": ["a"]}),
        _manual("do-b", when={"step": "mode", "in": ["b"]}),
    ]
    pid = f"p-{uuid.uuid4().hex[:6]}"
    BODIES[pid] = "# t\n\n" + render_block(_rb(steps)) + "\n"
    inp = RunbookRunInput(plan_id=pid, version=1, sha256="x", mode="live", requested_by="t",
                          human_wait_hours=0.01)
    await env.client.start_workflow(TestLiveParent.run, inp, id=f"plan-{pid}", task_queue="tq")
    h = env.client.get_workflow_handle_for(DrRunbookWorkflow.run, f"plan-{pid}-exec-v1")

    async def wait_for(kind, step):
        for _ in range(300):
            try:
                a = (await h.query("runbook_state")).get("awaiting")
            except Exception:  # noqa: BLE001 —— 子执行还没起出来
                a = None
            if a and a["type"] == kind and a["step_id"] == step:
                return
            await asyncio.sleep(0.05)
        raise AssertionError(f"没等到 {kind}@{step}")

    confirmed = []
    for sid in ("prep-1", "prep-2"):
        await wait_for("confirm", sid)                 # 每个变更都单独等一次人
        await h.execute_update("confirm_step", {"step_id": sid, "operator": "alice"})
        confirmed.append(sid)
    await wait_for("decision", "mode")
    await h.execute_update("decide", {"step_id": "mode", "choice": "b", "operator": "alice", "reason": "r"})
    await wait_for("confirm", "do-b")                  # 直接跳到 do-b，没有在 do-a 停
    await h.execute_update("confirm_step", {"step_id": "do-b", "operator": "alice"})
    r = await h.result()
    st = await h.query("runbook_state")
    assert r.status == "completed"
    assert st["skipped"] == ["do-a"] and st["decisions"] == {"mode": "b"}
    assert st["executed"] == ["prep-1", "prep-2", "do-b"]
