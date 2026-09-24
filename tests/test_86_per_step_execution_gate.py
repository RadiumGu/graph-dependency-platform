"""
test_86_per_step_execution_gate.py — 「按步骤放行」的门禁。

这是整个 DR worker 里**最不能退化**的一条:一次节点组演练不该有能力
切换生产数据库。

2026-09-24 演练前加的机制:只有名字出现在 `execute_steps` 里的步骤才真执行,
其余一律 dry_run,**即使 `dry_run=False`**。两道闸门(全局开关 **且**
名字在清单里)是刻意的 —— 单独任何一个被误设都不足以让危险步骤真跑。

实测验证过:一次 `dry_run=False` 且 `execute_steps=["scale_up_nodegroup"]`
的运行里,`fetch_plan_body` 的结果是 `executed=False`。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

W = Path(__file__).resolve().parents[1] / "dr-plan-generator" / "worker"
WF = W / "workflows.py"
ACT = W / "activities.py"


@pytest.fixture(scope="module")
def wf() -> str:
    return WF.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def act() -> str:
    return ACT.read_text(encoding="utf-8")


class TestTwoGatesNotOne:
    def test_gate_requires_both_conditions(self, wf: str):
        i = wf.find("def _step_is_live(")
        assert i > 0, "找不到 _step_is_live"
        # ⚠️ 不要用 `(.*?)\n\n` 去截函数体 —— 这个函数有带空行的 docstring，
        # 那个正则会在 docstring 里就停住，取不到 return 那一行。
        # 第一版就是这么写的，于是在代码完全正确的情况下判成失败。
        # 按位置取固定长度的切片更实在。
        body = wf[i : i + 900]
        # 两个条件都要:全局 dry_run 为假，**并且** 步骤名在 execute_steps 里。
        assert "not args.dry_run" in body, "缺少全局 dry_run 闸门"
        assert "args.execute_steps" in body, "缺少按步骤放行的闸门"
        assert "(not args.dry_run) and (step in args.execute_steps)" in body, (
            "两个条件必须用 and 连接。改成 or 会让任一被误设就放行危险步骤。"
        )

    def test_default_execute_steps_is_empty(self, wf: str):
        # 默认空清单 = 默认什么都不真跑。漏写的后果必须是安全的那一侧。
        assert re.search(
            r"execute_steps:\s*list\[str\]\s*=\s*field\(default_factory=list\)", wf
        ), "execute_steps 的默认值必须是空清单"


class TestEveryStepGoesThroughTheGate:
    """四个步骤都必须按名字取 dry_run，不许直接用全局开关。"""

    STEPS = ["fetch_plan_body", "scale_up_nodegroup", "promote_database", "verify_step"]

    @pytest.mark.parametrize("step", STEPS)
    def test_step_uses_gate(self, wf: str, step: str):
        assert f'_step_is_live(args, "{step}")' in wf, (
            f"{step} 没有走按步骤放行的闸门"
        )

    def test_no_step_passes_global_dry_run_directly(self, wf: str):
        # 直接把 args.dry_run 传给 ActivityInput 就绕过了按步骤放行。
        bad = re.findall(r"ActivityInput\([^)]*dry_run=args\.dry_run", wf, re.S)
        assert not bad, (
            "有步骤直接用 args.dry_run，绕过了按步骤放行。"
            "那意味着一次节点组演练会把数据库提升也真执行。"
        )

    def test_promote_database_gate_is_documented(self, wf: str):
        # 最危险的那一步，理由要留在代码里。
        i = wf.index('_step_is_live(args, "promote_database")')
        near = wf[max(0, i - 400) : i]
        assert "演练" in near or "生产数据库" in near


class TestResultRecordsWhatWasAllowed:
    def test_executed_steps_in_result(self, wf: str):
        # 事后复盘要能看出「这是一次什么演练」。
        assert "executed_steps" in wf


class TestVerificationLimitationIsStated:
    """`verified=True` 不等于「这一步达到目的了」。"""

    def test_step_result_has_detail_note(self, act: str):
        assert "detail_note" in act, (
            "需要一个字段说明「核实测到的到底是什么」"
        )

    def test_nodegroup_limitation_is_recorded(self, act: str):
        i = act.index('step="scale_up_nodegroup",\n        executed=True')
        seg = act[i : i + 1400]
        # 数 EC2 实例不等于集群有可调度容量。
        assert "不是 Ready" in seg or "可调度容量" in seg, (
            "必须写明 running_nodes 数的是 EC2 而不是 Ready 的 k8s 节点 —— "
            "两台 EC2 起来了仍可能没有可调度容量"
        )
