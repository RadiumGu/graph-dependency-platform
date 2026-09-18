"""
tests/test_rto_rpo.py — RTO 分策略估算与 RPO 按拓扑推导

最重要的一条是 test_rpo_returns_none_when_not_derivable：
**给不出数字比给错数字诚实。** 原实现在无跨区复制时照样输出「5 分钟」
（样例里的「15 分钟」来自 else 分支），审计问「怎么算的」答不上来。
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from assessment.rpo_estimator import RPOEstimator
from assessment.rto_estimator import (
    PILOT_LIGHT_TIMES,
    WARM_STANDBY_TIMES,
    RTOEstimator,
    load_measurements,
    record_measurement,
)
from dr_profile import DRProfile, set_active_profile
from models import DRPhase, DRStep

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "test_profile.yaml")


def _step(action: str, resource_type: str, seconds: int, group: str = None) -> DRStep:
    return DRStep(
        step_id=f"s-{action}", order=1, action=action,
        resource_type=resource_type, resource_name="r",
        estimated_time=seconds, parallel_group=group,
    )


def _phase(*steps) -> DRPhase:
    return DRPhase(phase_id="phase-2", name="p", layer="L2", steps=list(steps))


class TestStrategyTables(unittest.TestCase):
    def test_pilot_light_is_slower_on_compute(self) -> None:
        """pilot light 要造容量：节点冷启动 + 首次镜像拉取。

        原实现只有一张表，对两档用同一组数字，pilot light 被系统性低估。
        """
        for key in ("Microservice", "EKSNodeGroup", "EC2Instance", "Pod"):
            self.assertGreater(
                PILOT_LIGHT_TIMES[key], WARM_STANDBY_TIMES[key],
                f"{key} 在 pilot light 下应更慢",
            )

    def test_data_and_traffic_layers_unaffected_by_strategy(self) -> None:
        """数据层与流量层的耗时不因计算层策略而变。"""
        for key in ("RDSCluster", "DynamoDBTable", "LoadBalancer", "S3Bucket"):
            self.assertEqual(PILOT_LIGHT_TIMES[key], WARM_STANDBY_TIMES[key])

    def test_estimator_picks_table_by_strategy(self) -> None:
        pl = RTOEstimator(strategy="pilot_light", measurements={})
        ws = RTOEstimator(strategy="warm_standby", measurements={})
        self.assertIs(pl.table, PILOT_LIGHT_TIMES)
        self.assertIs(ws.table, WARM_STANDBY_TIMES)


class TestMeasurementOverride(unittest.TestCase):
    def test_measured_value_wins_over_declared(self) -> None:
        """实测值优先于步骤自带估值——这是「设计值→实测值」的关键。"""
        est = RTOEstimator(measurements={"scale_up_and_verify": [40.0, 50.0, 60.0]})
        seconds, measured = est.step_seconds(_step("scale_up_and_verify", "Microservice", 300))
        self.assertTrue(measured)
        self.assertEqual(seconds, 50)  # (40+50+60)/3

    def test_declared_used_when_no_samples(self) -> None:
        est = RTOEstimator(measurements={})
        seconds, measured = est.step_seconds(_step("scale_up_and_verify", "Microservice", 300))
        self.assertFalse(measured)
        self.assertEqual(seconds, 300)

    def test_basis_reports_confidence(self) -> None:
        """审计要的是「这个数字怎么来的」。"""
        est = RTOEstimator(measurements={})
        _, basis = est.estimate_with_basis([_phase(_step("a", "Microservice", 120))])
        self.assertEqual(basis["confidence"], "design_values_only")
        self.assertIn("a", basis["estimated_actions"])

        est2 = RTOEstimator(measurements={"a": [100.0]})
        _, basis2 = est2.estimate_with_basis([_phase(_step("a", "Microservice", 120))])
        self.assertEqual(basis2["confidence"], "measured")
        self.assertEqual(basis2["sample_counts"]["a"], 1)

    def test_partial_confidence_when_mixed(self) -> None:
        est = RTOEstimator(measurements={"a": [100.0]})
        _, basis = est.estimate_with_basis([
            _phase(_step("a", "Microservice", 120), _step("b", "Microservice", 120))
        ])
        self.assertEqual(basis["confidence"], "partial")

    def test_parallel_group_counts_only_the_longest(self) -> None:
        est = RTOEstimator(measurements={})
        phase = _phase(
            _step("x", "Microservice", 100, group="pg-1"),
            _step("y", "Microservice", 200, group="pg-1"),
            _step("z", "Microservice", 60),
        )
        seconds, _, _ = est._estimate_phase(phase)
        self.assertEqual(seconds, 260)  # max(100,200) + 60


class TestMeasurementsFile(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dr-meas-")
        self.path = os.path.join(self.tmp, "measurements.json")

    def test_roundtrip(self) -> None:
        record_measurement("scale_up", 42.0, path=self.path)
        record_measurement("scale_up", 44.0, path=self.path)
        data = load_measurements(self.path)
        self.assertEqual(data["scale_up"], [42.0, 44.0])

    def test_keeps_only_recent_samples(self) -> None:
        """环境会变（机型、镜像大小），很久以前的样本会把均值拖偏。"""
        for i in range(15):
            record_measurement("a", float(i), path=self.path, keep=5)
        self.assertEqual(load_measurements(self.path)["a"], [10.0, 11.0, 12.0, 13.0, 14.0])

    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(load_measurements(os.path.join(self.tmp, "nope.json")), {})

    def test_corrupt_file_degrades_gracefully(self) -> None:
        """统计文件损坏不该让计划生成失败——估算退回查表即可。"""
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(load_measurements(self.path), {})

    def test_non_numeric_samples_ignored(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"a": ["x", 5, None, 7]}, fh)
        self.assertEqual(load_measurements(self.path)["a"], [5.0, 7.0])


class TestRPODerivation(unittest.TestCase):
    def test_drill_with_global_database_is_zero_loss(self) -> None:
        """switchover 是零数据丢失（AWS 文档明确）。"""
        set_active_profile(FIXTURE)
        r = RPOEstimator(profile=DRProfile(FIXTURE), mode="drill").assess(
            [{"name": "db", "type": "RDSCluster"}]
        )
        self.assertEqual(r.minutes, 0)
        self.assertEqual(r.basis[0]["rpo"], "0")

    def test_failover_with_global_database_is_seconds_magnitude(self) -> None:
        r = RPOEstimator(profile=DRProfile(FIXTURE), mode="failover").assess(
            [{"name": "db", "type": "RDSCluster"}]
        )
        self.assertIsNotNone(r.minutes)
        self.assertIn("量级", r.basis[0]["rpo"])

    def test_rpo_returns_none_when_not_derivable(self) -> None:
        """**核心断言：给不出数字就返回 None，不编一个。**

        原实现在无跨区复制时照样给「5 分钟」，样例里的「15 分钟」来自 else 分支。
        审计问「怎么算的」答不上来。
        """
        path = _tmp_profile("    aurora:\n      topology: none\n")
        try:
            r = RPOEstimator(profile=DRProfile(path)).assess(
                [{"name": "db", "type": "RDSCluster"}]
            )
            self.assertIsNone(r.minutes)
            self.assertIn("aurora", r.unmeasurable)
            self.assertIn("小时级", r.basis[0]["reason"])
            self.assertFalse(r.is_stateable)
        finally:
            os.unlink(path)

    def test_sqs_always_unmeasurable(self) -> None:
        """SQS 丢失量只能实测：等于切换瞬间的消息数。"""
        r = RPOEstimator(profile=DRProfile(FIXTURE)).assess(
            [{"name": "q", "type": "SQSQueue"}]
        )
        self.assertIsNone(r.minutes)
        self.assertIn("sqs", r.unmeasurable)
        self.assertTrue(r.measurement_commands)

    def test_s3_crr_without_rtc_is_unmeasurable(self) -> None:
        """CRR 未开 RTC 时没有复制时间 SLA，给不出可举证的数字。"""
        path = _tmp_profile("    s3:\n      topology: crr\n")
        try:
            r = RPOEstimator(profile=DRProfile(path)).assess(
                [{"name": "b", "type": "S3Bucket"}]
            )
            self.assertIsNone(r.minutes)
            self.assertIn("SLA", r.basis[0]["reason"])
        finally:
            os.unlink(path)

    def test_stateless_scope_is_zero(self) -> None:
        r = RPOEstimator(profile=DRProfile(FIXTURE)).assess(
            [{"name": "svc", "type": "Microservice"}]
        )
        self.assertEqual(r.minutes, 0)

    def test_aurora_measurement_command_does_not_guess_metric_name(self) -> None:
        """不指定复制延迟指标名：写错时 get-metric-statistics 返回空数据不报错，
        会让人误以为延迟为 0。改为先用 list-metrics 查实际指标名。"""
        r = RPOEstimator(profile=DRProfile(FIXTURE), mode="failover").assess(
            [{"name": "q", "type": "SQSQueue"}, {"name": "db", "type": "RDSCluster"}]
        )
        cmds = {c["component"]: c["command"] for c in r.measurement_commands}
        if "aurora" in cmds:
            self.assertIn("list-metrics", cmds["aurora"])


class TestPlanCarriesBasis(unittest.TestCase):
    def test_plan_records_rpo_basis_and_none_rpo(self) -> None:
        from graph.graph_analyzer import GraphAnalyzer
        from planner.plan_generator import PlanGenerator
        from planner.step_builder import StepBuilder

        path = _tmp_profile(
            "    aurora:\n      topology: none\n", extra_services=True
        )
        try:
            set_active_profile(path)
            gen = PlanGenerator(GraphAnalyzer(), StepBuilder())
            plan = gen.generate_plan(
                scope="region", source="eu-west-1", target="eu-central-1",
                snapshot={
                    "nodes": [
                        {"name": "storefront", "type": "Microservice", "tier": "Tier0"},
                        {"name": "acme-db", "type": "RDSCluster", "tier": "Tier0"},
                    ],
                    "edges": [
                        {"from": "storefront", "to": "acme-db", "type": "AccessesData"}
                    ],
                },
            )
            self.assertIsNone(plan.estimated_rpo, "不可推定时 RPO 必须是 None")
            self.assertTrue(plan.rpo_basis)
            self.assertIn("aurora", plan.rpo_unmeasurable)
            self.assertTrue(plan.rto_basis.get("confidence"))
        finally:
            os.unlink(path)

    def test_renderer_shows_not_derivable_instead_of_zero(self) -> None:
        """None 不能渲染成 0——0 会被读成「零数据丢失」，与「说不清」相反。"""
        from output.markdown_renderer import MarkdownRenderer
        from models import DRPlan

        plan = DRPlan(
            plan_id="p", created_at="t", scope="region",
            source="a", target="b",
            estimated_rpo=None, rpo_unmeasurable=["aurora", "sqs"],
        )
        text = MarkdownRenderer()._format_rpo(plan)
        self.assertIn("不可从配置推定", text)
        self.assertIn("aurora", text)
        self.assertNotIn("0 minutes", text)


def _tmp_profile(dr_data: str, extra_services: bool = False) -> str:
    body = (
        "profile:\n  name: t\n"
        "kubernetes:\n  namespace: t\n"
    )
    if extra_services:
        body += "services:\n  storefront:\n    tier: Tier0\n"
    body += "dr:\n  strategy: warm_standby\n  data:\n" + dr_data
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(body)
        return fh.name


if __name__ == "__main__":
    unittest.main()
