"""
tests/test_plan_validator.py — Unit tests for PlanValidator

Tests ordering validation, completeness check, cycle detection,
rollback completeness, and graph freshness checks.
"""

import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import DRPhase, DRPlan, DRStep, ImpactReport
from validation.plan_validator import PlanValidator


def _make_step(
    step_id: str,
    resource_name: str,
    resource_type: str = "Microservice",
    dependencies: list = None,
    rollback_command: str = "rollback cmd",
    order: int = 1,
) -> DRStep:
    return DRStep(
        step_id=step_id,
        order=order,
        resource_type=resource_type,
        resource_id="",
        resource_name=resource_name,
        action="test_action",
        command="test cmd",
        validation="test validation",
        expected_result="ok",
        rollback_command=rollback_command,
        estimated_time=60,
        requires_approval=False,
        tier="Tier1",
        dependencies=dependencies or [],
    )


def _make_plan(
    phases: list = None,
    affected_resources: list = None,
    graph_snapshot_time: str = None,
) -> DRPlan:
    now = datetime.now(timezone.utc).isoformat()
    return DRPlan(
        plan_id="test-plan",
        created_at=now,
        scope="az",
        source="apne1-az1",
        target="apne1-az2",
        affected_services=[],
        affected_resources=affected_resources or [],
        phases=phases or [],
        rollback_phases=[],
        impact_assessment=ImpactReport(scope="az", source="apne1-az1"),
        estimated_rto=10,
        estimated_rpo=5,
        validation_status="pending",
        graph_snapshot_time=graph_snapshot_time or now,
    )


class TestCycleDetection(unittest.TestCase):

    def setUp(self) -> None:
        self.validator = PlanValidator()

    def test_no_cycle_passes(self) -> None:
        step_a = _make_step("step-a", "resource-a", dependencies=[])
        step_b = _make_step("step-b", "resource-b", dependencies=["step-a"])
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step_a, step_b], estimated_duration=2, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        cycle_issues = [i for i in report.issues if "cycle" in i.message.lower()]
        self.assertEqual(cycle_issues, [])

    def test_cycle_detected(self) -> None:
        step_a = _make_step("step-a", "resource-a", dependencies=["step-b"])
        step_b = _make_step("step-b", "resource-b", dependencies=["step-a"])
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step_a, step_b], estimated_duration=2, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        cycle_issues = [i for i in report.issues if "cycle" in i.message.lower()]
        self.assertGreater(len(cycle_issues), 0)
        self.assertFalse(report.valid)


class TestCompletenessCheck(unittest.TestCase):

    def setUp(self) -> None:
        self.validator = PlanValidator()

    def test_all_resources_covered_passes(self) -> None:
        step = _make_step("step-a", "petsite")
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step], estimated_duration=1, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase], affected_resources=["petsite"])
        report = self.validator.validate(plan)
        completeness_issues = [i for i in report.issues if "not covered" in i.message]
        self.assertEqual(completeness_issues, [])

    def test_missing_resource_creates_warning(self) -> None:
        step = _make_step("step-a", "petsite")
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step], estimated_duration=1, gate_condition="ok",
        )
        plan = _make_plan(
            phases=[phase],
            affected_resources=["petsite", "petsite-db"],  # petsite-db has no step
        )
        report = self.validator.validate(plan)
        completeness_issues = [i for i in report.issues if "not covered" in i.message]
        self.assertGreater(len(completeness_issues), 0)
        self.assertEqual(completeness_issues[0].severity, "WARNING")

    def test_empty_plan_with_no_resources_passes(self) -> None:
        plan = _make_plan(phases=[], affected_resources=[])
        report = self.validator.validate(plan)
        completeness_issues = [i for i in report.issues if "not covered" in i.message]
        self.assertEqual(completeness_issues, [])


class TestOrderingCheck(unittest.TestCase):

    def setUp(self) -> None:
        self.validator = PlanValidator()

    def test_correct_order_passes(self) -> None:
        step_a = _make_step("step-a", "service-a", order=1, dependencies=[])
        step_b = _make_step("step-b", "service-b", order=2, dependencies=["step-a"])
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step_a, step_b], estimated_duration=2, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        ordering_issues = [i for i in report.issues if "ordering" in i.message.lower() or "scheduled before" in i.message.lower()]
        self.assertEqual(ordering_issues, [])

    def test_ordering_violation_detected(self) -> None:
        # step-b appears first but depends on step-a which appears second
        step_b = _make_step("step-b", "service-b", order=1, dependencies=["step-a"])
        step_a = _make_step("step-a", "service-a", order=2, dependencies=[])
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step_b, step_a],  # b before a, but b depends on a
            estimated_duration=2, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        ordering_issues = [
            i for i in report.issues
            if "ordering" in i.message.lower() or "scheduled before" in i.message.lower()
        ]
        self.assertGreater(len(ordering_issues), 0)
        self.assertFalse(report.valid)


class TestRollbackCompleteness(unittest.TestCase):

    def setUp(self) -> None:
        self.validator = PlanValidator()

    def test_missing_rollback_creates_warning(self) -> None:
        step = _make_step("step-a", "petsite", rollback_command="")
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step], estimated_duration=1, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        rollback_issues = [i for i in report.issues if "rollback" in i.message.lower()]
        self.assertGreater(len(rollback_issues), 0)
        self.assertEqual(rollback_issues[0].severity, "WARNING")

    def test_all_rollbacks_present_no_issue(self) -> None:
        step = _make_step("step-a", "petsite", rollback_command="aws rollback cmd")
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step], estimated_duration=1, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        rollback_issues = [i for i in report.issues if "rollback" in i.message.lower()]
        self.assertEqual(rollback_issues, [])


class TestGraphFreshness(unittest.TestCase):

    def setUp(self) -> None:
        self.validator = PlanValidator()

    def test_fresh_snapshot_no_warning(self) -> None:
        now = datetime.now(timezone.utc).isoformat()
        plan = _make_plan(graph_snapshot_time=now)
        report = self.validator.validate(plan)
        freshness_issues = [i for i in report.issues if "snapshot" in i.message.lower()]
        self.assertEqual(freshness_issues, [])

    def test_stale_snapshot_creates_warning(self) -> None:
        old_time = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        plan = _make_plan(graph_snapshot_time=old_time)
        report = self.validator.validate(plan)
        freshness_issues = [i for i in report.issues if "snapshot" in i.message.lower()]
        self.assertGreater(len(freshness_issues), 0)
        self.assertEqual(freshness_issues[0].severity, "WARNING")
        # Stale snapshot is a warning, not critical → plan can still be valid
        self.assertTrue(report.valid)


class TestValidationReport(unittest.TestCase):

    def setUp(self) -> None:
        self.validator = PlanValidator()

    def test_clean_plan_is_valid(self) -> None:
        step = _make_step("step-a", "petsite")
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step], estimated_duration=1, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase], affected_resources=["petsite"])
        report = self.validator.validate(plan)
        self.assertTrue(report.valid)

    def test_critical_issue_makes_invalid(self) -> None:
        step_a = _make_step("step-a", "a", dependencies=["step-b"])
        step_b = _make_step("step-b", "b", dependencies=["step-a"])
        phase = DRPhase(
            phase_id="phase-1", name="Test", layer="L2",
            steps=[step_a, step_b], estimated_duration=1, gate_condition="ok",
        )
        plan = _make_plan(phases=[phase])
        report = self.validator.validate(plan)
        self.assertFalse(report.valid)


if __name__ == "__main__":
    unittest.main()


class TestValidationQuality:
    """Tests for validation command quality checks."""

    def _make_plan_with_validation(self, validation_str):
        """Helper: create a minimal plan with one step having given validation."""
        from models import DRPlan, DRPhase, DRStep
        step = DRStep(
            step_id="test-step",
            order=1,
            resource_type="RDSCluster",
            resource_id="id-1",
            resource_name="test-db",
            action="promote_read_replica",
            command="aws rds promote ...",
            validation=validation_str,
            expected_result="available",
            rollback_command="aws rds ...",
            estimated_time=60,
            requires_approval=True,
            tier="Tier0",
            dependencies=[],
        )
        phase = DRPhase(
            phase_id="phase-1",
            name="Data",
            layer="L0",
            steps=[step],
            estimated_duration=1,
            gate_condition="ok",
        )
        return DRPlan(
            plan_id="test",
            created_at="2026-01-01T00:00:00Z",
            scope="az",
            source="apne1-az1",
            target="apne1-az2",
            phases=[phase],
            graph_snapshot_time="2026-01-01T00:00:00Z",
        )

    def test_echo_dollar_flagged(self):
        plan = self._make_plan_with_validation("echo $?")
        report = PlanValidator().validate(plan)
        warnings = [i for i in report.issues if "echo" in i.message.lower()]
        assert len(warnings) == 1
        assert "not meaningful" in warnings[0].message

    def test_comment_only_flagged(self):
        plan = self._make_plan_with_validation("# Just a comment")
        report = PlanValidator().validate(plan)
        warnings = [i for i in report.issues if "comment" in i.message.lower()]
        assert len(warnings) == 1

    def test_empty_validation_flagged(self):
        plan = self._make_plan_with_validation("")
        report = PlanValidator().validate(plan)
        errors = [i for i in report.issues if "empty" in i.message.lower()]
        assert len(errors) == 1
        assert errors[0].severity == "ERROR"

    def test_real_command_no_flag(self):
        plan = self._make_plan_with_validation(
            "aws rds describe-db-clusters --db-cluster-identifier test-db"
        )
        report = PlanValidator().validate(plan)
        quality_issues = [i for i in report.issues
                         if "echo" in i.message.lower()
                         or "comment" in i.message.lower()
                         or "empty" in i.message.lower()]
        assert len(quality_issues) == 0


class CommandQualityAcrossAllFieldsTest(unittest.TestCase):
    """2026-09-22：判据必须对 command / validation / rollback_command 三者都生效。

    此前只有 command 被完整检查：validation 只查空/echo/纯注释，
    rollback_command 只在别处查存在性、内容完全不查。后果是一类缺陷结构性地
    看不见 —— Lambda 回滚步骤引用未绑定的 $EVENT_SOURCE_UUID，它在
    rollback_command 里，校验器从来没有机会报它。

    回滚命令写错比正常命令写错更危险：它只在**出事之后**才被执行，
    那时没人有余裕调试一条展开成空串的参数。
    """

    def _one_step_plan(self, **kw) -> DRPlan:
        """只填必填字段 —— DRStep / DRPhase / DRPlan 的其余字段都有默认值。"""
        step = DRStep(
            step_id="s1",
            order=1,
            resource_type="LambdaFunction",
            resource_name="fn",
            action="a",
            command=kw.get("command", "aws lambda get-function --function-name fn"),
            validation=kw.get("validation", "aws lambda get-function --function-name fn"),
            rollback_command=kw.get(
                "rollback_command", "aws lambda update-function-code --function-name fn"),
            tier="Tier1",
        )
        phase = DRPhase(phase_id="phase-2", name="n", layer="compute", steps=[step])
        return DRPlan(
            plan_id="p",
            created_at="2026-09-22T00:00:00Z",
            scope="az",
            source="apne1-az1",
            target="apne1-az2",
            phases=[phase],
        )

    def _msgs(self, plan):
        return [i.message for i in PlanValidator()._check_validation_quality(plan)]

    def test_rollback_未绑定变量必须被检出(self):
        """审查第 2 条：$EVENT_SOURCE_UUID 在 rollback_command 里，旧判据扫不到。"""
        plan = self._one_step_plan(
            rollback_command="aws lambda update-event-source-mapping "
                             "--uuid $EVENT_SOURCE_UUID --enabled")
        hits = [m for m in self._msgs(plan)
                if "rollback_command" in m and "EVENT_SOURCE_UUID" in m]
        self.assertTrue(hits, "rollback_command 里的未绑定变量没被检出")

    def test_validation_未绑定变量必须被检出(self):
        plan = self._one_step_plan(
            validation="aws route53 get-hosted-zone --id $ZONE_ID")
        hits = [m for m in self._msgs(plan)
                if "validation" in m and "ZONE_ID" in m]
        self.assertTrue(hits, "validation 里的未绑定变量没被检出")

    def test_同步骤内赋值过的变量不算未绑定(self):
        plan = self._one_step_plan(
            command="ZONE_ID=$(aws route53 list-hosted-zones --query x --output text)\n"
                    "aws route53 get-hosted-zone --id $ZONE_ID")
        hits = [m for m in self._msgs(plan) if "ZONE_ID" in m]
        self.assertEqual(hits, [], f"同步骤赋值过的变量被误报: {hits}")

    def test_未替换的模板占位符必须被检出(self):
        """审查第 1 条那类形态：命令由字符串格式化拼出，参数没填上。"""
        plan = self._one_step_plan(
            command="kubectl get pods --context {target}-cluster")
        hits = [m for m in self._msgs(plan) if "placeholder" in m and "{target}" in m]
        self.assertTrue(hits, "未替换的 {target} 占位符没被检出")

    def test_shell合法的大括号不得误报(self):
        """误判的代价比漏判高：一次误报会让人把整个校验器关掉。"""
        plan = self._one_step_plan(
            command="NS=default\n"
                    "kubectl get pods -n ${NS} -o json | jq -r '.items[].metadata.name'\n"
                    "ps aux | awk '{print $1}'")
        hits = [m for m in self._msgs(plan) if "placeholder" in m]
        self.assertEqual(hits, [], f"shell 合法的大括号被误报成占位符: {hits}")

    def test_rollback为纯注释不得报ERROR(self):
        """固化一个我自己踩过的误报。

        第一版把 rollback_command 纯注释也判 ERROR，对真实计划一跑报出 41 条，
        绝大多数是 preflight-connectivity / preflight-vcpu-quota 这类**只读检查**
        步骤 —— 它们没有修改任何状态，本就不需要回滚，写一条「无需回滚」的注释
        是正确做法。

        判据的分界是「这个字段为空会不会导致演练假通过」：
            command / validation 为空 → 会（步骤空转却报成功）
            rollback 为空            → 不会（只在回滚时才执行）
        """
        plan = self._one_step_plan(
            rollback_command="# Read-only preflight check — no rollback needed")
        errs = [m for m in self._msgs(plan) if "rollback_command" in m]
        self.assertEqual(errs, [], f"preflight 的「无需回滚」注释被误报: {errs}")

    def test_command为纯注释仍必须报ERROR(self):
        """这条是真缺陷：注释「执行成功」，演练全绿而那一步什么都没做。"""
        plan = self._one_step_plan(
            command="# TODO: Manual switchover required for NeptuneCluster\n"
                    "# Add the appropriate AWS CLI command here.")
        hits = [m for m in self._msgs(plan)
                if "command" in m and "no executable line" in m]
        self.assertTrue(hits, "command 纯注释没被报 ERROR —— 演练会假通过")




class K8sServiceContextTest(unittest.TestCase):
    """2026-09-22：K8sService 步骤的 kube context 不得由字符串拼接得出。

    原实现是 `--context {target}-cluster`，把 region/AZ 名拼上 "-cluster" 当作
    kube context。真实 context 名由使用者的 kubeconfig 决定，拼出来的几乎必然
    不存在，而 kubectl 对不存在的 context 是**直接报错退出** —— 整条恢复链断在
    这里。

    而本文件所在模块的 `_kubectl_target()` docstring 早就写明了这一点，
    只是这个方法没改用它。又一次「正确实现早就有，某处没用它」。
    """

    def _builder(self):
        from planner.step_builder import StepBuilder
        return StepBuilder()

    def _step(self, sb, namespace="petadoptions"):
        node = {"name": "petsite-svc", "namespace": namespace,
                "id": "v1", "tier": "Tier0"}
        return sb._build_k8sservice_step(node, "apne1-az1", "apne1-az2", {"order": 1})

    def test_不得出现拼接的context名(self):
        sb = self._builder()
        step = self._step(sb)
        for field in (step.command, step.validation, step.rollback_command):
            self.assertNotIn(
                "-cluster", field,
                f"命令里仍有拼接出来的 context 名: {field!r}",
            )
            self.assertNotIn("apne1-az2-cluster", field)

    def test_namespace取自节点而非profile默认值(self):
        """K8sService 的 namespace 必须用节点自己的属性。

        用 profile 默认 namespace 会去错误的 namespace 查 endpoints，
        查不到却看不出是找错了地方。
        """
        sb = self._builder()
        step = self._step(sb, namespace="awesomeshop")
        self.assertIn("-n awesomeshop", step.command)

    def test_context缺失必须记入计划产物而不只是日志(self):
        """没有 --context 会落到 kubeconfig 的当前 context 上 —— 命令能跑，
        但可能跑在源区集群，输出看起来完全正常。

        这种「静默在错误目标上执行成功」比报错危险，所以必须出现在计划产物里。
        只记日志不够：运维拿到的仍是一份看起来完整的计划。
        """
        sb = self._builder()
        self._step(sb)
        gaps = [g for g in sb.compute_layer_gaps
                if g.get("component") == "kubernetes_context_target"]
        # profile 里 context_target 是 ${K8S_CONTEXT_TARGET} 占位符，
        # 未设环境变量时应当记 gap。
        if "--context" not in sb._context_only():
            self.assertTrue(
                gaps,
                "context_target 未配置却没有记入 compute_layer_gaps",
            )
            self.assertIn("源区", gaps[0]["implication"],
                          "缺口说明里应当讲清「可能跑在源区集群」这个后果")
