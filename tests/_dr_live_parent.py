"""_dr_live_parent.py — test_129 用的父执行。单独成模块是为了能进 workflow 沙箱。

Temporal 的沙箱会**重新导入**定义 workflow 的那个模块。test_129 顶层有
`Path(__file__).resolve()` 之类的调用，在沙箱里被禁止 —— 把父执行定义在测试
文件里会让 worker 校验失败（2026-10-06 实测：RestrictedWorkflowAccessError:
Cannot access pathlib.Path.resolve）。

不用 UnsandboxedWorkflowRunner 绕过：沙箱能在测试里抓出工作流的不确定性，
关掉它就丢掉了这层保护。
"""
from __future__ import annotations

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from runbook_workflow import DrRunbookWorkflow, RunbookRunInput


@workflow.defn(name="TestLiveParent")
class TestLiveParent:
    """模拟 DrPlanWorkflow.authorize_execution 起子执行的形状：<父 id>-exec-v<N>。"""

    @workflow.run
    async def run(self, inp: RunbookRunInput):
        return await workflow.execute_child_workflow(
            DrRunbookWorkflow.run,
            inp,
            id=f"{workflow.info().workflow_id}-exec-v{inp.version}",
            task_queue="tq",
        )
