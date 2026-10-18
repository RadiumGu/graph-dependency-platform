"""test_134_temporal_ui_readability.py — Temporal UI 上能看到每一步要执行的动作（2026-10-18）。

背景：plan-20261006-tokyo-to-seoul 在 UI 上只有两根贯穿 12 天的长柱（workflow 本身和
「1 (30d)」计时器），每步要做什么只能去 Input 里翻 12KB 原文。调研与 API 核实见
docs/lessons/temporal-execution-readability-2026-10.md（10-07 另一次会话的调研，截至 10-18 仍未提交入库；本次照它落地）。

守三类东西：
1. 渲染（离线）：步骤表一步一行、命令完整可复制、summary 按字节不超 200、details 不超 20K
2. 接线（离线）：等人的计时器、探针 activity、子执行都带了人话标签
3. 行为（真实 Temporal）：计划与执行的 Current Details 里真能读到步骤与「现在等你做什么」
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest

W = Path(__file__).resolve().parents[1] / "dr-plan-generator" / "worker"
if str(W) not in sys.path:
    sys.path.insert(0, str(W))

from runbook import FORMAT, parse_runbook, render_block  # noqa: E402
import ui_text as u  # noqa: E402

STEPS = [
    {"id": "pre", "kind": "check", "title": "前置核查",
     "probes": [{"probe": "aurora_global_membership"}]},
    {"id": "scale", "kind": "manual", "title": "把首尔节点组拉到 2",
     "command": "aws eks update-nodegroup-config --scaling-config desiredSize=2 | tee x",
     "reversible": True, "rollback": "aws eks update-nodegroup-config --scaling-config desiredSize=0",
     "pre": [{"probe": "eks_nodegroup_status"}],
     "post": [{"probe": "eks_ready_nodes", "params": {"min_ready": 2}}]},
    {"id": "mode", "kind": "decision", "title": "Aurora 怎么切",
     "options": ["switchover", "failover-allow-data-loss", "abort"]},
    {"id": "promote", "kind": "manual", "title": "提升首尔为写节点", "command": "aws rds failover-global-cluster …",
     "reversible": False, "irreversible_note": "回不去",
     "post": [{"probe": "aurora_writer_region", "params": {"expect_region": "ap-northeast-2"}}]},
]
RB = parse_runbook({"format": FORMAT, "source_region": "ap-northeast-1",
                    "target_region": "ap-northeast-2", "steps": STEPS})
BODY = "# 测试计划\n\n" + render_block(RB) + "\n"


class TestRendering:
    def test_one_row_per_step_with_the_action(self):
        rows = [l for l in u.steps_table(RB).splitlines() if l.startswith("| ") and not l.startswith("| #")]
        assert len(rows) == len(STEPS)
        assert "aws eks update-nodegroup-config" in rows[1]
        assert "switchover / failover-allow-data-loss / abort" in rows[2]
        assert "⛔ **不可逆**" in rows[3]

    def test_pipe_in_command_does_not_break_the_table(self):
        row = u.steps_table(RB).splitlines()[3]
        assert "\\| tee x" in row
        assert row.count(" | ") == u.steps_table(RB).splitlines()[0].count(" | ")

    def test_full_commands_are_copyable(self):
        md = u.full_commands(RB)
        assert "```bash\naws eks update-nodegroup-config --scaling-config desiredSize=2 | tee x\n```" in md
        assert "desiredSize=0" in md, "回滚命令必须完整给出"

    def test_status_column_has_matching_header(self):
        t = u.steps_table(RB, {0: "✅", 1: "🙋 等确认"}).splitlines()
        assert t[0].count("|") == t[1].count("|") == t[2].count("|")
        assert "| 状态 |" in t[0]

    def test_summaries_fit_200_bytes(self):
        rb = parse_runbook({"format": FORMAT, "source_region": "ap-northeast-1",
                            "target_region": "ap-northeast-2",
                            "steps": [{**STEPS[1], "title": "标题" * 100, "command": "中文" * 300}]})
        for s in (u.plan_wait_summary("draft", 720),
                  u.human_wait_summary(rb, {"type": "confirm", "step_id": "scale"}, 24),
                  u.probe_summary(rb, "scale", "post", rb.steps[0].post[0]),
                  u.plan_static("p" * 300, rb)[0]):
            assert len(s.encode("utf-8")) <= 200, (len(s.encode()), s)
            assert "**" not in s and "`" not in s, "Timeline 上按纯文本显示"

    def test_details_fit_20k_bytes(self):
        big = parse_runbook({"format": FORMAT, "source_region": "a-b-1", "target_region": "a-b-2",
                             "steps": [{**STEPS[1], "id": f"s{i}", "command": "命令" * 2000}
                                       for i in range(20)]})
        md = u.plan_details(plan_id="p", version=1, state="draft", sha256="x" * 64, rb=big,
                            parse_error=None, approval_blockers=[], active_child=None)
        assert len(md.encode("utf-8")) <= 20_000

    def test_awaiting_confirm_shows_the_full_command_and_how_to_confirm(self):
        md = u.runbook_details(wid="w-exec-v1", mode="live", plan_id="p", version=1, rb=RB,
                               status="running", cursor=1,
                               awaiting={"type": "confirm", "step_id": "scale"},
                               done_ids=set(), findings=[])
        assert "现在等你" in md and "desiredSize=2 | tee x" in md
        assert "confirm_step" in md and '"step_id":"scale"' in md

    def test_draft_lists_what_blocks_approval(self):
        md = u.plan_details(plan_id="p", version=1, state="draft", sha256="x" * 64, rb=RB,
                            parse_error=None, approval_blockers=["步骤 x 仍标着 needs_human_input"],
                            active_child=None)
        assert "批准前必须解决" in md and "needs_human_input" in md


def _src(name: str) -> str:
    return (W / name).read_text(encoding="utf-8")


def _calls_missing_kw(src: str, func_attr: str, kw: str) -> list[int]:
    out = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == func_attr \
                and getattr(n.func.value, "id", None) == "workflow":
            if kw not in {k.arg for k in n.keywords}:
                out.append(n.lineno)
    return out


class TestWiring:
    @pytest.mark.parametrize("f", ["plan_workflow.py", "runbook_workflow.py", "arc_bridge.py"])
    def test_every_human_wait_has_a_label(self, f):
        # 带 timeout 的 wait_condition 在 Timeline 上就是一根计时器
        src = _src(f)
        real = []
        for n in ast.walk(ast.parse(src)):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "wait_condition":
                kws = {k.arg for k in n.keywords}
                if "timeout" in kws and "timeout_summary" not in kws:
                    real.append(n.lineno)
        assert not real, f"{f} 第 {real} 行的计时器没有标签 —— Timeline 上又会是「1 (30d)」"

    @pytest.mark.parametrize("f", ["runbook_workflow.py", "arc_bridge.py"])
    def test_every_sleep_and_probe_has_a_label(self, f):
        src = _src(f)
        assert not _calls_missing_kw(src, "sleep", "summary"), f"{f} 有不带 summary 的 workflow.sleep"

    def test_probe_activities_are_labelled(self):
        assert "summary=probe_summary(" in _src("runbook_workflow.py")

    def test_child_run_has_a_summary(self):
        assert "static_summary=" in _src("plan_workflow.py")

    def test_cli_start_plan_sets_static_metadata(self):
        src = _src("probe_cli.py")
        assert "static_summary=summary" in src and "static_details=details" in src

    def test_details_refresh_is_wired_into_state_changes(self):
        assert "self._refresh_details()\n        workflow.upsert_search_attributes(" in _src("plan_workflow.py")


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

from activities import PlanVersionInput, StepResult  # noqa: E402
from plan_workflow import DrPlanWorkflow  # noqa: E402
from runbook_workflow import DrRunbookWorkflow  # noqa: E402

_SA = [
    SearchAttributeKey.for_keyword_list("DRStepsExecuted"), SearchAttributeKey.for_keyword("DRPlanRef"),
    SearchAttributeKey.for_bool("DRDryRun"), SearchAttributeKey.for_keyword("DRStepName"),
    SearchAttributeKey.for_keyword("DRDecision"), SearchAttributeKey.for_keyword("DRPlanId"),
    SearchAttributeKey.for_int("DRPlanVersion"), SearchAttributeKey.for_keyword("DRPlanState"),
]


@activity.defn(name="put_plan_version")
async def _fake_put(inp: PlanVersionInput) -> StepResult:
    return StepResult(step="put_plan_version", executed=True, verified=True, detail={"s3_key": "k"})


async def _current_details(h) -> str:
    m = await h.query("__temporal_workflow_metadata")
    # SDK 返回 proto（temporal.api.sdk.v1.WorkflowMetadata）
    return m.current_details


@pytest_asyncio.fixture
async def env():
    e = await WorkflowEnvironment.start_local(search_attributes=_SA)
    async with Worker(e.client, task_queue="tq", workflows=[DrPlanWorkflow, DrRunbookWorkflow],
                      activities=[_fake_put]):
        yield e
    await e.shutdown()


@pytest.mark.asyncio
async def test_plan_current_details_show_every_step(env):
    h = await env.client.start_workflow(
        DrPlanWorkflow.run,
        {"plan_id": "ui", "body": BODY, "author": "t", "reason": "t"},
        id=f"plan-ui-{uuid.uuid4().hex[:6]}", task_queue="tq",
    )
    md = ""
    for _ in range(100):
        md = await _current_details(h)
        if md:
            break
        await asyncio.sleep(0.05)
    for s in STEPS:
        assert s["title"] in md, s["title"]
    assert "aws eks update-nodegroup-config" in md
    assert "**下一步**" in md
    await h.terminate()
