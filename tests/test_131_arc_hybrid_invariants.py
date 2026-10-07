"""test_131_arc_hybrid_invariants.py — ARC + Temporal 混合方案的不变量（离线、纯文本/结构断言）。

worker 被允许调用 StartPlanExecution（test_128 唯一按文件放行的写），前提是
「起执行不等于做变更」。这个前提由下面几条撑着，任何一条被改掉，放行就不再成立：

1. 计划里每个工作流的第一步都是 ManualApproval
2. 审批角色的信任策略要求 MFA，且信任的不是 worker / agent 的角色
3. worker 角色只有 Start/Get/List，没有 Approve/Cancel/Update
4. 扩缩函数只能动这一个节点组；两份函数代码一致
5. 回切方向依赖停用区域的那一步，在 ungraceful 模式下跳过
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from cfn_yaml import load_cfn  # noqa: E402

PLAN_T = ROOT / "infra" / "dr-korea" / "20-arc-region-switch-poc.yaml"
NOOP_T = ROOT / "infra" / "tokyo" / "07-arc-region-switch-noop.yaml"
WORKER_T = ROOT / "infra" / "dr-korea" / "07-worker-permissions.yaml"
BRIDGE = ROOT / "dr-plan-generator" / "worker" / "arc_bridge.py"
APPROVE = ROOT / "infra" / "dr-korea" / "arc-approve.sh"
DESIGN = ROOT / "docs" / "runbooks" / "dr-arc-temporal-hybrid-design.md"


@pytest.fixture(scope="module")
def plan() -> dict:
    return load_cfn(PLAN_T)


def _statements(policy_doc: dict) -> list[dict]:
    s = policy_doc["Statement"]
    return s if isinstance(s, list) else [s]


class TestEveryWorkflowStartsWithAHuman:
    def test_first_step_is_manual_approval(self, plan):
        wfs = plan["Resources"]["Plan"]["Properties"]["Workflows"]
        assert wfs
        for wf in wfs:
            assert wf["Steps"][0]["ExecutionBlockType"] == "ManualApproval", wf.get("WorkflowDescription")

    def test_approval_uses_the_mfa_role(self, plan):
        for wf in plan["Resources"]["Plan"]["Properties"]["Workflows"]:
            cfg = wf["Steps"][0]["ExecutionBlockConfiguration"]["ExecutionApprovalConfig"]
            assert cfg["ApprovalRole"] == "ApproverRole.Arn"   # cfn_yaml 把 !GetAtt 化成字符串

    def test_only_reversible_blocks_in_the_poc(self, plan):
        """小验证只放可逆步骤：不许出现 Aurora / 路由 / 健康检查切流。"""
        kinds = {s["ExecutionBlockType"] for wf in plan["Resources"]["Plan"]["Properties"]["Workflows"]
                 for s in wf["Steps"]}
        assert kinds <= {"ManualApproval", "CustomActionLambda"}, kinds


class TestApproverRequiresMfa:
    def test_trust_requires_mfa(self, plan):
        st = _statements(plan["Resources"]["ApproverRole"]["Properties"]["AssumeRolePolicyDocument"])
        assert len(st) == 1
        assert st[0]["Condition"] == {"Bool": {"aws:MultiFactorAuthPresent": "true"}}

    def test_trust_is_a_parameter_not_the_worker_role(self, plan):
        st = _statements(plan["Resources"]["ApproverRole"]["Properties"]["AssumeRolePolicyDocument"])
        assert st[0]["Principal"] == {"AWS": "ApproverPrincipalArn"}   # !Ref 参数
        assert "TemporalRole" not in PLAN_T.read_text(encoding="utf-8")

    def test_helper_asks_for_an_mfa_code(self):
        src = APPROVE.read_text(encoding="utf-8")
        assert "--serial-number" in src and "--token-code" in src


class TestWorkerCanStartButNotDecide:
    def _arc_actions(self) -> set[str]:
        d = load_cfn(WORKER_T)
        st = _statements(d["Resources"]["WorkerReadPlans"]["Properties"]["PolicyDocument"])
        out: set[str] = set()
        for s in st:
            acts = s["Action"] if isinstance(s["Action"], list) else [s["Action"]]
            out |= {a for a in acts if a.startswith("arc-region-switch:")}
        return out

    def test_start_and_read_only(self):
        acts = self._arc_actions()
        assert "arc-region-switch:StartPlanExecution" in acts
        forbidden = {"arc-region-switch:ApprovePlanExecutionStep", "arc-region-switch:CancelPlanExecution",
                     "arc-region-switch:UpdatePlanExecution", "arc-region-switch:UpdatePlanExecutionStep",
                     "arc-region-switch:UpdatePlan", "arc-region-switch:CreatePlan",
                     "arc-region-switch:DeletePlan", "arc-region-switch:*"}
        assert not (acts & forbidden), acts & forbidden

    def test_scoped_to_the_poc_plan(self):
        src = WORKER_T.read_text(encoding="utf-8")
        assert "plan/petsite-dr-poc*" in src

    def test_bridge_never_calls_approve_or_cancel(self):
        attrs = {n.attr for n in ast.walk(ast.parse(BRIDGE.read_text(encoding="utf-8")))
                 if isinstance(n, ast.Attribute)}
        assert not attrs & {"approve_plan_execution_step", "cancel_plan_execution",
                            "update_plan_execution", "update_plan_execution_step"}


class TestScaleFunctions:
    def test_only_one_nodegroup(self, plan):
        pol = plan["Resources"]["ScaleFnRole"]["Properties"]["Policies"][0]["PolicyDocument"]
        sts = {s["Sid"]: s for s in _statements(pol)}
        assert sts["OnlyThisNodegroup"]["Resource"] == (
            "arn:aws:eks:${AWS::Region}:${AWS::AccountId}:nodegroup/${ClusterName}/${NodegroupName}/*"
        )
        assert set(sts["OnlyThisNodegroup"]["Action"]) == {"eks:DescribeNodegroup", "eks:UpdateNodegroupConfig"}

    def test_two_copies_of_code_are_identical(self, plan):
        r = plan["Resources"]
        up = r["ScaleUpFn"]["Properties"]["Code"]["ZipFile"]
        down = r["ScaleDownFn"]["Properties"]["Code"]["ZipFile"]
        assert up == down, "CFN 不支持 YAML 别名，两份代码靠这条保持一致"
        compile(up, "scale", "exec")

    def test_success_means_asg_settled_not_api_200(self, plan):
        code = plan["Resources"]["ScaleUpFn"]["Properties"]["Code"]["ZipFile"]
        assert 'in_service == TARGET and len(states) == TARGET' in code

    def test_records_the_arc_event(self, plan):
        assert "arc_event=event" in plan["Resources"]["ScaleUpFn"]["Properties"]["Code"]["ZipFile"]


class TestDirectionality:
    def test_deactivating_region_step_skips_when_ungraceful(self, plan):
        for wf in plan["Resources"]["Plan"]["Properties"]["Workflows"]:
            for s in wf["Steps"]:
                cfg = s["ExecutionBlockConfiguration"].get("CustomActionLambdaConfig")
                if cfg and cfg["RegionToRun"] == "deactivatingRegion":
                    assert cfg.get("Ungraceful") == {"Behavior": "skip"}, s["Name"]

    def test_each_lambda_step_has_one_function_per_region(self, plan):
        for wf in plan["Resources"]["Plan"]["Properties"]["Workflows"]:
            for s in wf["Steps"]:
                cfg = s["ExecutionBlockConfiguration"].get("CustomActionLambdaConfig")
                if cfg:
                    assert len(cfg["Lambdas"]) == 2, s["Name"]

    def test_tokyo_noop_really_does_nothing(self):
        code = load_cfn(NOOP_T)["Resources"]["NoopFn"]["Properties"]["Code"]["ZipFile"]
        tree = ast.parse(code)
        calls = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert calls <= {"dumps"}, calls
        assert "boto3" not in code


class TestBridgeDoesNotTrustArcAlone:
    def test_postchecks_required(self):
        assert "没有后置探针" in BRIDGE.read_text(encoding="utf-8")

    def test_two_verdicts_are_separate(self):
        assert '"arc-completed-but-postcheck-failed"' in BRIDGE.read_text(encoding="utf-8")

    def test_stall_does_not_cancel(self):
        src = BRIDGE.read_text(encoding="utf-8")
        assert "停在原地，不取消 ARC 执行" in src


class TestStartIsIdempotentOnTheWorkerSdk:
    """2026-10-07 首次真跑：worker 钉的 boto3==1.40.47 没有 clientToken，起执行直接失败。
    本地 SDK 新，测试全绿 —— 幂等不能只靠一个运行时可能不存在的参数。"""

    def test_client_token_only_if_the_runtime_sdk_has_it(self):
        src = BRIDGE.read_text(encoding="utf-8")
        assert 'if "clientToken" in _input_members(c, "StartPlanExecution"):' in src

    def test_looks_for_an_existing_execution_first(self):
        src = BRIDGE.read_text(encoding="utf-8")
        i = src.index("async def arc_start_plan_execution")
        seg = src[i: i + 1800]
        assert seg.index("find_existing(") < seg.index("c.start_plan_execution"), "必须先找再起"

    def test_comment_carries_the_tag_at_the_front(self):
        assert 'comment=f"{idem_tag(wid)} ' in BRIDGE.read_text(encoding="utf-8")

    def test_find_existing(self):
        sys.path.insert(0, str(BRIDGE.parent))
        tag_src = BRIDGE.read_text(encoding="utf-8")
        ns: dict = {}
        exec("from typing import Any\n" + tag_src[tag_src.index("def idem_tag"): tag_src.index("def _input_members")], ns)
        items = [{"executionId": "a", "comment": "[temporal wf-1] x"}, {"executionId": "b", "comment": "[temporal wf-12] y"}]
        assert ns["find_existing"](items, "wf-1")["executionId"] == "a"
        assert ns["find_existing"](items, "wf-12")["executionId"] == "b"
        assert ns["find_existing"](items, "wf-2") is None

    def test_start_failure_is_a_verdict(self):
        assert '"arc-start-failed"' in BRIDGE.read_text(encoding="utf-8")
