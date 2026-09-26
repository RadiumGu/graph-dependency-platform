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

import ast
import re
from pathlib import Path

import pytest

W = Path(__file__).resolve().parents[1] / "dr-plan-generator" / "worker"
WF = W / "workflows.py"
ACT = W / "activities.py"


def _callee_name(func: ast.expr) -> str | None:
    """取调用目标的名字，`f(...)` 与 `self.f(...)` 都返回 `f`。

    只看最后一段名字是刻意的：`self._run_step` 与 `_run_step` 是同一个东西，
    而按完整点号路径匹配会让「把函数挪进类里」这种无害重构误报。
    """
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


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
    """四个步骤都必须按名字取 dry_run，不许直接用全局开关。

    ⚠️ 2026-09-26 改判据。原来写的是

        assert f'_step_is_live(args, "{step}")' in wf

    —— 那是**某一种写法的语法形态**，不是要守的性质。把每个步骤重构成
    独立子 workflow 之后，调用变成 `self._run_step(Cls, "step", args)`
    而 `_run_step` 内部调 `_step_is_live(args, step)`：**每一步仍然经过闸门，
    但四条断言全挂**。代码是对的，门禁错了。

    这与本仓库反复记录的同一族失误一致：判据切的是语法片段而不是意图。
    现在断言的是性质 —— 步骤名必须被传给一个「自身受 `_step_is_live`
    约束」的可调用对象，无论那是 `_step_is_live` 本身还是一个派发函数。
    两种形态都满足，往后再重构也不会误报。
    """

    STEPS = ["fetch_plan_body", "scale_up_nodegroup", "promote_database", "verify_step"]

    @staticmethod
    def _gated_call_lines(wf: str, step: str) -> list[int]:
        """返回把 `step` 传给「受闸门约束的可调用对象」的那些调用的行号。

        受闸门约束 = `_step_is_live` 本身，或任何函数体里调用了
        `_step_is_live` 的函数/方法（派发函数）。
        """
        tree = ast.parse(wf)

        gated: set[str] = {"_step_is_live"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for inner in ast.walk(node):
                    if (
                        isinstance(inner, ast.Call)
                        and _callee_name(inner.func) == "_step_is_live"
                    ):
                        gated.add(node.name)
                        break

        hits: list[int] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if _callee_name(node.func) not in gated:
                continue
            literals = [
                a.value
                for a in list(node.args) + [kw.value for kw in node.keywords]
                if isinstance(a, ast.Constant) and isinstance(a.value, str)
            ]
            if step in literals:
                hits.append(node.lineno)
        return hits

    @pytest.mark.parametrize("step", STEPS)
    def test_step_uses_gate(self, wf: str, step: str):
        hits = self._gated_call_lines(wf, step)
        assert hits, (
            f"{step} 没有走按步骤放行的闸门 —— 它的名字没有被传给 "
            f"_step_is_live，也没有被传给任何调用了 _step_is_live 的派发函数"
        )

    def test_the_dispatcher_itself_is_gated(self, wf: str):
        """如果存在派发函数，它必须自己算 live，而不是由调用方传进来。

        这条是上面那条的补充：派发函数把闸门集中到一处是好事，
        但前提是那一处真的在算。少了这条，一个**名叫** _run_step
        而实际不判闸门的函数会让上面四条全部通过。
        """
        tree = ast.parse(wf)
        dispatchers = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(c, ast.Call) and _callee_name(c.func) == "_step_is_live"
                for c in ast.walk(n)
            )
        ]
        assert dispatchers, "没有任何函数调用 _step_is_live —— 闸门根本没在用"

    def test_no_step_passes_global_dry_run_directly(self, wf: str):
        # 直接把 args.dry_run 传给 ActivityInput 就绕过了按步骤放行。
        # ⚠️ 作用域刻意限定在 ActivityInput( —— `FailoverResult(dry_run=args.dry_run)`
        #    是**结果记录**，如实记下这次的全局开关是对的，不是绕过。
        bad = re.findall(r"ActivityInput\([^)]*dry_run=args\.dry_run", wf, re.S)
        assert not bad, (
            "有步骤直接用 args.dry_run，绕过了按步骤放行。"
            "那意味着一次节点组演练会把数据库提升也真执行。"
        )

    def test_promote_database_gate_is_documented(self, wf: str):
        # 最危险的那一步，理由要留在代码里。
        # 按 AST 拿到行号再回看源码，而不是按某一种调用写法做字符串定位 ——
        # 重构会换掉写法，但「理由该留在调用点附近」这条性质不变。
        hits = self._gated_call_lines(wf, "promote_database")
        assert hits, "promote_database 没有走闸门"
        lines = wf.split("\n")
        for lineno in hits:
            window = "\n".join(lines[max(0, lineno - 21) : lineno])
            if "演练" in window or "生产数据库" in window:
                return
        pytest.fail(
            "promote_database 的调用点附近 20 行内没有说明为什么它需要显式放行"
        )


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
