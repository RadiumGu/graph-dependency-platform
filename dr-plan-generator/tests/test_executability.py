"""
tests/test_executability.py — 产出必须可执行

三类「看起来完整但执行必失败/必空转」的形态，都在这里锚定：
1. Route 53 change-batch 缺必填字段 → 被 InvalidChangeBatch 整体拒绝
2. 未绑定的 shell 变量（$ZONE_ID）→ 展开成空串、参数错位
3. 只有注释的步骤 → 执行「成功」而什么都没做，演练全绿却漏了一步
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dr_profile import DRProfile, set_active_profile
from models import DRPhase, DRPlan, DRStep
from planner.dns_commands import (
    ZONE_ID_PLACEHOLDER,
    build_failover_change,
    build_ttl_change,
    resolve_zone_id,
)
from validation.plan_validator import PlanValidator

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "test_profile.yaml")


def _extract_change_batch(command: str) -> dict:
    """从命令里抽出 --change-batch 的 JSON 并解析。"""
    m = re.search(r"--change-batch '(\{.*?\})'", command, re.S)
    assert m, f"命令里找不到 change-batch: {command}"
    return json.loads(m.group(1))


class TestChangeBatchCompleteness(unittest.TestCase):
    """Route 53 的 change-batch 必填字段。"""

    def setUp(self) -> None:
        os.environ["ACME_ZONE_ID"] = "Z0EXAMPLE"
        self.profile = DRProfile(FIXTURE)

    def tearDown(self) -> None:
        os.environ.pop("ACME_ZONE_ID", None)

    def test_ttl_change_has_all_required_fields(self) -> None:
        """原实现只给 TTL，缺 Name/Type/ResourceRecords → InvalidChangeBatch。"""
        result = build_ttl_change(self.profile, 60, record_values=["1.2.3.4"])
        rrset = _extract_change_batch(result["command"])["Changes"][0]["ResourceRecordSet"]
        for field in ("Name", "Type", "TTL", "ResourceRecords"):
            self.assertIn(field, rrset, f"change-batch 缺必填字段 {field}")
        self.assertEqual(rrset["TTL"], 60)
        self.assertEqual(rrset["ResourceRecords"], [{"Value": "1.2.3.4"}])

    def test_record_name_is_fully_qualified(self) -> None:
        """Route 53 的规范形式带尾点。"""
        result = build_ttl_change(self.profile, 60, record_values=["1.2.3.4"])
        rrset = _extract_change_batch(result["command"])["Changes"][0]["ResourceRecordSet"]
        self.assertTrue(rrset["Name"].endswith("."))

    def test_failover_change_points_at_target(self) -> None:
        result = build_failover_change(self.profile, "dr-alb.example.com", "eu-central-1")
        rrset = _extract_change_batch(result["command"])["Changes"][0]["ResourceRecordSet"]
        self.assertEqual(rrset["ResourceRecords"], [{"Value": "dr-alb.example.com"}])
        self.assertEqual(result["expected"], "dr-alb.example.com")

    def test_ttl_command_warns_upsert_replaces_whole_record(self) -> None:
        """UPSERT 是整条替换：只写 TTL 会把解析目标清掉。"""
        result = build_ttl_change(self.profile, 60)
        self.assertIn("整条记录替换", result["command"])
        self.assertIn("list-resource-record-sets", result["command"])


class TestZoneIdHandling(unittest.TestCase):
    def test_configured_zone_is_used(self) -> None:
        os.environ["ACME_ZONE_ID"] = "Z0EXAMPLE"
        try:
            zone, configured = resolve_zone_id(DRProfile(FIXTURE))
            self.assertEqual(zone, "Z0EXAMPLE")
            self.assertTrue(configured)
        finally:
            os.environ.pop("ACME_ZONE_ID", None)

    def test_unconfigured_zone_uses_identifiable_placeholder(self) -> None:
        """不能输出裸 ``$ZONE_ID``：未设置时展开成空串，参数解析错位，
        报的错离根因很远。占位符要在执行时立刻失败。"""
        os.environ.pop("ACME_ZONE_ID", None)
        zone, configured = resolve_zone_id(DRProfile(FIXTURE))
        self.assertFalse(configured)
        self.assertEqual(zone, ZONE_ID_PLACEHOLDER)
        self.assertNotEqual(zone, "")
        self.assertNotIn("$", zone)

    def test_unconfigured_zone_adds_a_visible_warning(self) -> None:
        os.environ.pop("ACME_ZONE_ID", None)
        result = build_ttl_change(DRProfile(FIXTURE), 60, record_values=["1.2.3.4"])
        self.assertIn("未配置", result["command"])

    def test_no_profile_does_not_crash(self) -> None:
        zone, configured = resolve_zone_id(None)
        self.assertFalse(configured)
        self.assertTrue(zone)


class TestGeneratedPlanHasNoPlaceholders(unittest.TestCase):
    def setUp(self) -> None:
        set_active_profile(FIXTURE)

    def _plan(self):
        from graph.graph_analyzer import GraphAnalyzer
        from planner.plan_generator import PlanGenerator
        from planner.step_builder import StepBuilder

        gen = PlanGenerator(GraphAnalyzer(), StepBuilder(strategy="pilot_light"))
        return gen.generate_plan(
            scope="region", source="eu-west-1", target="eu-central-1",
            snapshot={
                "nodes": [
                    {"name": "storefront", "type": "Microservice", "tier": "Tier0"},
                    {"name": "acme-alb", "type": "LoadBalancer", "tier": None},
                    {"name": "mystery", "type": "SomeUnknownType", "tier": "Tier2"},
                ],
                "edges": [
                    {"from": "acme-alb", "to": "storefront", "type": "ForwardsTo"},
                    # mystery 必须与 workload 相连，否则会被 M4 的范围锚定正确地剔除
                    {"from": "storefront", "to": "mystery", "type": "DependsOn"},
                ],
            },
        )

    def test_every_step_has_an_executable_line(self) -> None:
        """判据是「有没有可执行行」而不是「有没有 TODO 这个词」。

        纯注释步骤执行会「成功」，于是演练全绿而那一步什么都没做。
        反过来，说明性注释里提到 TODO 并不构成问题——抓行为，不抓关键词。
        """
        plan = self._plan()
        for phase in plan.phases:
            for step in phase.steps:
                code = [
                    ln for ln in (step.command or "").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                ]
                self.assertTrue(
                    code,
                    f"{phase.phase_id}/{step.step_id} 只有注释，没有可执行内容",
                )

    def test_no_todo_inside_executable_lines(self) -> None:
        plan = self._plan()
        for phase in plan.phases:
            for step in phase.steps:
                for blob in (step.command, step.validation, step.rollback_command):
                    code = [
                        ln for ln in (blob or "").splitlines()
                        if ln.strip() and not ln.strip().startswith("#")
                    ]
                    for line in code:
                        self.assertNotIn(
                            "TODO", line,
                            f"{phase.phase_id}/{step.step_id} 可执行行里含 TODO",
                        )

    def test_no_bare_zone_id_variable(self) -> None:
        plan = self._plan()
        for phase in plan.phases:
            for step in phase.steps:
                blob = f"{step.command}\n{step.validation}\n{step.rollback_command}"
                self.assertNotIn("$ZONE_ID", blob, f"{step.step_id} 仍用裸 $ZONE_ID")

    def test_unsupported_type_blocks_instead_of_no_op(self) -> None:
        """未支持的类型要产出会失败的步骤，而不是一句注释。"""
        plan = self._plan()
        blocked = [
            s for ph in plan.phases for s in ph.steps
            if s.resource_name == "mystery"
        ]
        self.assertTrue(blocked, "未支持类型的步骤丢失了")
        self.assertIn("exit 1", blocked[0].command)

    def test_validator_reports_no_errors(self) -> None:
        """自产自销：工具生成的计划必须通过自己的校验器（无 ERROR）。"""
        plan = self._plan()
        report = PlanValidator().validate(plan)
        errors = [i for i in report.issues if i.severity in ("ERROR", "CRITICAL")]
        self.assertEqual(
            errors, [],
            "生成的计划触发了自己的 ERROR：\n"
            + "\n".join(f"  [{i.severity}] {i.message}" for i in errors),
        )


class TestValidatorCatchesPlaceholders(unittest.TestCase):
    """反向验证：校验器真的能抓到这三类问题。"""

    @staticmethod
    def _plan_with(step: DRStep) -> DRPlan:
        return DRPlan(
            plan_id="p", created_at="t", scope="region", source="a", target="b",
            affected_resources=[step.resource_name],
            phases=[DRPhase(phase_id="phase-1", name="n", layer="L0", steps=[step])],
        )

    def test_comment_only_command_is_error(self) -> None:
        step = DRStep(
            step_id="s1", order=1, resource_name="r", action="a",
            command="# TODO: Add the appropriate AWS CLI command here\n# nothing here",
            validation="echo ok",
        )
        report = PlanValidator().validate(self._plan_with(step))
        self.assertTrue(
            any(
                i.severity == "ERROR" and "no executable command" in i.message
                for i in report.issues
            ),
            "纯注释步骤未被判为 ERROR",
        )

    def test_explanatory_todo_in_comment_is_not_an_error(self) -> None:
        """说明性注释里提到 TODO 不构成问题——只要有可执行行。"""
        step = DRStep(
            step_id="s1b", order=1, resource_name="r", action="a",
            command="# 刻意不留 TODO 注释，因为注释执行会「成功」\n"
                    "aws sts get-caller-identity",
            validation="echo ok",
        )
        report = PlanValidator().validate(self._plan_with(step))
        self.assertFalse(
            any(i.severity == "ERROR" for i in report.issues),
            f"说明性注释被误判: {[i.message for i in report.issues]}",
        )

    def test_unbound_variable_is_flagged(self) -> None:
        step = DRStep(
            step_id="s2", order=1, resource_name="r", action="a",
            command="aws route53 change-resource-record-sets --hosted-zone-id $ZONE_ID",
            validation="echo ok",
        )
        report = PlanValidator().validate(self._plan_with(step))
        self.assertTrue(
            any("ZONE_ID" in i.message for i in report.issues),
            "未绑定 shell 变量未被标记",
        )

    def test_variable_assigned_in_same_step_is_not_flagged(self) -> None:
        """同一步骤内赋值过的变量不算未绑定。"""
        step = DRStep(
            step_id="s3", order=1, resource_name="r", action="a",
            command='TG_ARN=$(aws elbv2 describe-target-groups --query x)\n'
                    'aws elbv2 describe-target-health --target-group-arn "$TG_ARN"',
            validation="echo ok",
        )
        report = PlanValidator().validate(self._plan_with(step))
        self.assertFalse(
            any("TG_ARN" in i.message for i in report.issues),
            "同步骤内已赋值的变量被误判",
        )

    def test_variable_only_in_comment_is_not_flagged(self) -> None:
        """注释里的 $VAR 是说明文字，不是要执行的引用。"""
        step = DRStep(
            step_id="s4", order=1, resource_name="r", action="a",
            command="# 说明：可用 $SOME_VAR 覆盖\naws sts get-caller-identity",
            validation="echo ok",
        )
        report = PlanValidator().validate(self._plan_with(step))
        self.assertFalse(any("SOME_VAR" in i.message for i in report.issues))


if __name__ == "__main__":
    unittest.main()
