"""
tests/test_preflight.py — phase-0 就绪检查

这些检查的共同价值是：**把「切到一半才暴露」的失败提前到动手之前**。
因此测试重点不是命令字符串好不好看，而是：
- 缺配置时是否记缺口（而不是静默生成半个检查）
- 检查对象是否优先来自图谱（profile 手写清单会漂移）
- pilot light 的假设是否被显式确认
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dr_profile import DRProfile, set_active_profile
from planner.preflight import DEFAULT_VCPU_QUOTA_CODE, PreflightBuilder

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "test_profile.yaml")
SOURCE, TARGET = "eu-west-1", "eu-central-1"

_NODES = [
    {"name": "storefront", "type": "Microservice", "tier": "Tier0"},
    {"name": "checkout", "type": "Microservice", "tier": "Tier1"},
]


def _builder(strategy: str = "warm_standby", profile_path: str = FIXTURE) -> PreflightBuilder:
    return PreflightBuilder(strategy=strategy, profile=DRProfile(profile_path))


def _actions(steps) -> list:
    return [s.action for s in steps]


class TestEcrChecks(unittest.TestCase):
    def test_prefers_graph_derived_repositories(self) -> None:
        """图谱随服务增减自动同步，profile 里手写的仓库清单会漂移。"""
        nodes = _NODES + [
            {"name": "acme/from-graph", "type": "ECRRepository"},
        ]
        steps = _builder().build(SOURCE, TARGET, nodes)
        ecr = [s for s in steps if s.action == "check_image_present_in_target"]
        names = {s.resource_name for s in ecr}
        self.assertEqual(names, {"acme/from-graph"})

    def test_falls_back_to_profile_repositories(self) -> None:
        steps = _builder().build(SOURCE, TARGET, _NODES)
        names = {
            s.resource_name for s in steps
            if s.action == "check_image_present_in_target"
        }
        self.assertEqual(names, {"acme/storefront", "acme/checkout"})

    def test_uses_configured_tag(self) -> None:
        steps = _builder().build(SOURCE, TARGET, _NODES)
        ecr = [s for s in steps if s.action == "check_image_present_in_target"][0]
        self.assertIn("imageTag=v1.2.3", ecr.command)
        self.assertIn(f"--region {TARGET}", ecr.command)

    def test_command_warns_about_architecture(self) -> None:
        """镜像存在但架构不符同样起不来（本环境 arm64）。"""
        steps = _builder().build(SOURCE, TARGET, _NODES)
        ecr = [s for s in steps if s.action == "check_image_present_in_target"][0]
        self.assertIn("arm64", ecr.command)

    def test_no_repositories_records_a_gap(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(
                "profile:\n  name: t\n"
                "dr:\n  strategy: warm_standby\n"
                "  eks:\n    ecr_replication_required: true\n"
            )
            path = fh.name
        try:
            b = _builder(profile_path=path)
            b.build(SOURCE, TARGET, _NODES)
            self.assertTrue(any(g["component"] == "ecr" for g in b.gaps))
        finally:
            os.unlink(path)

    def test_skipped_when_replication_not_required(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(
                "profile:\n  name: t\n"
                "dr:\n  strategy: warm_standby\n"
                "  eks:\n    ecr_replication_required: false\n"
            )
            path = fh.name
        try:
            b = _builder(profile_path=path)
            steps = b.build(SOURCE, TARGET, _NODES)
            self.assertNotIn("check_image_present_in_target", _actions(steps))
            self.assertFalse(any(g["component"] == "ecr" for g in b.gaps))
        finally:
            os.unlink(path)


class TestCapacityChecks(unittest.TestCase):
    def test_instance_type_availability_checked(self) -> None:
        """arm64/Graviton 的区域覆盖不齐；机型不可用时扩容会「提交成功」但起不来。"""
        steps = _builder().build(SOURCE, TARGET, _NODES)
        step = [s for s in steps if s.action == "check_instance_types_available"][0]
        self.assertIn("describe-instance-type-offerings", step.command)
        self.assertIn("m7g.large", step.command)
        self.assertIn(f"--region {TARGET}", step.command)

    def test_vcpu_quota_checked_with_estimate(self) -> None:
        steps = _builder().build(SOURCE, TARGET, _NODES)
        step = [s for s in steps if s.action == "check_vcpu_quota"][0]
        self.assertIn("service-quotas get-service-quota", step.command)
        self.assertIn(DEFAULT_VCPU_QUOTA_CODE, step.command)
        # desired=2 × vcpus_per_node=2 = 4
        self.assertIn("4", step.expected_result)

    def test_quota_command_tells_reader_to_verify_the_code(self) -> None:
        """配额代码写错会查到一个不相关的配额然后「通过」——比不查更危险。"""
        steps = _builder().build(SOURCE, TARGET, _NODES)
        step = [s for s in steps if s.action == "check_vcpu_quota"][0]
        self.assertIn("list-service-quotas", step.command)

    def test_quota_mentions_arc_closure(self) -> None:
        """ARC readiness check 已于 2026-04-30 对新客户关闭，故须自建。"""
        steps = _builder().build(SOURCE, TARGET, _NODES)
        step = [s for s in steps if s.action == "check_vcpu_quota"][0]
        self.assertIn("2026-04-30", step.command)


class TestKmsChecks(unittest.TestCase):
    def test_configured_aliases_are_checked(self) -> None:
        steps = _builder().build(SOURCE, TARGET, _NODES)
        kms = [s for s in steps if s.action == "check_kms_key_usable"]
        self.assertEqual([s.resource_name for s in kms], ["alias/acme-data"])
        self.assertEqual(kms[0].expected_result, "Enabled")
        self.assertIn(f"--region {TARGET}", kms[0].command)

    def test_missing_aliases_records_a_gap(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("profile:\n  name: t\ndr:\n  strategy: warm_standby\n")
            path = fh.name
        try:
            b = _builder(profile_path=path)
            b.build(SOURCE, TARGET, _NODES)
            gap = [g for g in b.gaps if g["component"] == "kms"][0]
            self.assertIn("区域级", gap["implication"])
        finally:
            os.unlink(path)


class TestAppConfigCheck(unittest.TestCase):
    def test_requires_human_review(self) -> None:
        """配置指向错区域不会报错，只会 500——数量对了不代表值对了。"""
        steps = _builder().build(SOURCE, TARGET, _NODES)
        step = [s for s in steps if s.action == "check_target_app_config"][0]
        self.assertTrue(step.requires_approval)
        self.assertIn("--output table", step.command)
        self.assertIn("人工", step.expected_result)

    def test_uses_profile_prefix_and_target_region(self) -> None:
        steps = _builder().build(SOURCE, TARGET, _NODES)
        step = [s for s in steps if s.action == "check_target_app_config"][0]
        self.assertIn("--path /acme-shop", step.command)
        self.assertIn(f"--region {TARGET}", step.command)


class TestAuroraSyncCheck(unittest.TestCase):
    def test_uses_synchronization_status_not_a_guessed_metric(self) -> None:
        """主判据用文档明确列出取值的 SynchronizationStatus。

        没有用某个 CloudWatch 复制延迟指标：指标名若写错会查到空数据然后
        静默「通过」，那比不检查更危险。
        """
        steps = _builder().build(SOURCE, TARGET, _NODES)
        step = [s for s in steps if s.action == "check_global_cluster_synchronized"][0]
        self.assertIn("describe-global-clusters", step.command)
        self.assertIn("SynchronizationStatus", step.command)
        self.assertIn("pending-resync", step.expected_result)

    def test_skipped_when_no_global_database(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(
                "profile:\n  name: t\n"
                "dr:\n  strategy: warm_standby\n"
                "  data:\n    aurora:\n      topology: none\n"
            )
            path = fh.name
        try:
            steps = _builder(profile_path=path).build(SOURCE, TARGET, _NODES)
            self.assertNotIn("check_global_cluster_synchronized", _actions(steps))
        finally:
            os.unlink(path)


class TestManifestsAppliedCheck(unittest.TestCase):
    def test_only_for_pilot_light(self) -> None:
        ws = _builder("warm_standby").build(SOURCE, TARGET, _NODES)
        self.assertNotIn("check_manifests_applied", _actions(ws))
        pl = _builder("pilot_light").build(SOURCE, TARGET, _NODES)
        self.assertIn("check_manifests_applied", _actions(pl))

    def test_uses_translated_deployment_names(self) -> None:
        steps = _builder("pilot_light").build(SOURCE, TARGET, _NODES)
        step = [s for s in steps if s.action == "check_manifests_applied"][0]
        self.assertIn("deployment/storefront-deployment", step.command)
        self.assertIn("deployment/checkout-svc", step.command)

    def test_scoped_to_namespace(self) -> None:
        steps = _builder("pilot_light").build(SOURCE, TARGET, _NODES)
        step = [s for s in steps if s.action == "check_manifests_applied"][0]
        self.assertIn("-n acme", step.command)


class TestOrderingAndIntegration(unittest.TestCase):
    def test_orders_are_sequential_from_start(self) -> None:
        steps = _builder("pilot_light").build(SOURCE, TARGET, _NODES, start_order=7)
        self.assertEqual([s.order for s in steps], list(range(7, 7 + len(steps))))

    def test_no_profile_degrades_without_crashing(self) -> None:
        """无 profile 时不应崩——只跑得出不依赖配置的检查。"""
        b = PreflightBuilder(profile=None)
        steps = b.build(SOURCE, TARGET, _NODES)
        self.assertIsInstance(steps, list)

    def test_gaps_reach_the_plan(self) -> None:
        from graph.graph_analyzer import GraphAnalyzer
        from planner.plan_generator import PlanGenerator
        from planner.step_builder import StepBuilder

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(
                "profile:\n  name: t\n"
                "kubernetes:\n  namespace: t\n"
                "services:\n  storefront:\n    tier: Tier0\n"
                "dr:\n  strategy: warm_standby\n"
                "  eks:\n    ecr_replication_required: true\n"
            )
            path = fh.name
        try:
            set_active_profile(path)
            gen = PlanGenerator(GraphAnalyzer(), StepBuilder())
            plan = gen.generate_plan(
                scope="region", source=SOURCE, target=TARGET,
                snapshot={"nodes": _NODES, "edges": []},
            )
            components = {g["component"] for g in plan.compute_layer_gaps}
            self.assertIn("ecr", components)
            self.assertIn("kms", components)
        finally:
            os.unlink(path)

    def test_all_steps_have_expected_results(self) -> None:
        """没有期望值的「检查」等于没检查。"""
        for strategy in ("pilot_light", "warm_standby"):
            for step in _builder(strategy).build(SOURCE, TARGET, _NODES):
                self.assertTrue(
                    step.expected_result,
                    f"{step.step_id} 缺 expected_result",
                )
                self.assertTrue(step.validation, f"{step.step_id} 缺 validation")

    def test_phase0_orders_are_unique(self) -> None:
        """phase-0 内 order 不得撞号。

        回归锚点：接入就绪检查时曾用手工维护的 order 计数器作起点，而原代码在
        DNS TTL 步骤后忘了递增，导致 lower_dns_ttl 与第一个 ECR 检查都拿到 3。
        撞号会让「按 order 执行」的执行器顺序不确定。
        """
        from graph.graph_analyzer import GraphAnalyzer
        from planner.plan_generator import PlanGenerator
        from planner.step_builder import StepBuilder

        set_active_profile(FIXTURE)
        gen = PlanGenerator(GraphAnalyzer(), StepBuilder(strategy="pilot_light"))
        plan = gen.generate_plan(
            scope="region", source=SOURCE, target=TARGET,
            snapshot={
                "nodes": _NODES + [{"name": "acme-db", "type": "RDSCluster", "tier": "Tier0"}],
                "edges": [{"from": "checkout", "to": "acme-db", "type": "AccessesData"}],
            },
        )
        phase0 = [p for p in plan.phases if p.phase_id == "phase-0"][0]
        orders = [s.order for s in phase0.steps]
        self.assertEqual(
            len(orders), len(set(orders)),
            f"phase-0 order 撞号: {sorted(orders)}",
        )


if __name__ == "__main__":
    unittest.main()
