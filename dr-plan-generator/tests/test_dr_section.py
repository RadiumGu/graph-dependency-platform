"""
tests/test_dr_section.py — profile 的 dr 节与策略可行性校验

最重要的用例是 test_petsite_profile_reports_unmet_data_preconditions：
它固定住一个事实——PetAdoptions 当前**没有任何跨区数据复制**，
因此工具必须报出「所声明的策略在数据层不成立」，而不是照样输出
一份标称 RPO 秒级的计划。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dr_profile import (
    REPLICATED_AURORA,
    SUPPORTED_STRATEGIES,
    DRProfile,
    ProfileError,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "test_profile.yaml")
PARENT_PROFILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "profiles", "petsite.yaml")
)


class TestStrategyVocabulary(unittest.TestCase):
    def test_only_two_strategies_supported(self) -> None:
        """范围就是这两档——backup&restore 与 active-active 明确不做。"""
        self.assertEqual(set(SUPPORTED_STRATEGIES), {"pilot_light", "warm_standby"})

    def test_valid_strategy_is_returned(self) -> None:
        self.assertEqual(DRProfile(FIXTURE).dr_strategy, "warm_standby")

    def test_unsupported_strategy_raises(self) -> None:
        """不静默回落：策略决定生成哪些步骤，猜错会产出缺步骤但看着完整的计划。"""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("profile:\n  name: x\ndr:\n  strategy: backup_restore\n")
            path = fh.name
        try:
            with self.assertRaises(ProfileError):
                DRProfile(path).dr_strategy
        finally:
            os.unlink(path)

    def test_missing_strategy_raises(self) -> None:
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("profile:\n  name: x\n")
            path = fh.name
        try:
            with self.assertRaises(ProfileError):
                DRProfile(path).dr_strategy
        finally:
            os.unlink(path)


class TestDRSectionAccessors(unittest.TestCase):
    def setUp(self) -> None:
        self.p = DRProfile(FIXTURE)

    def test_target_region(self) -> None:
        self.assertEqual(self.p.dr_target_region, "eu-central-1")

    def test_topologies(self) -> None:
        self.assertEqual(self.p.dr_topology("aurora"), "global_database")
        self.assertEqual(self.p.dr_topology("dynamodb"), "global_tables")
        self.assertEqual(self.p.dr_topology("s3"), "crr")

    def test_unknown_component_defaults_to_none(self) -> None:
        self.assertEqual(self.p.dr_topology("redshift"), "none")

    def test_eks_accessors(self) -> None:
        self.assertEqual(self.p.dr_eks_target_cluster, "acme-dr")
        self.assertEqual(len(self.p.dr_eks_nodegroups), 1)
        self.assertEqual(self.p.dr_eks_nodegroups[0]["name"], "acme-arm64")
        self.assertTrue(self.p.dr_eks_requires_ecr_replication)

    def test_excluded_carries_reasons(self) -> None:
        reasons = self.p.dr_excluded_reasons()
        self.assertIn("etl_*", reasons)
        self.assertIn("platform_infrastructure", reasons["etl_*"])

    def test_aurora_replicated_vocabulary(self) -> None:
        self.assertIn("global_database", REPLICATED_AURORA)
        self.assertIn("cross_region_replica", REPLICATED_AURORA)
        self.assertNotIn("none", REPLICATED_AURORA)
        self.assertNotIn("snapshot_copy", REPLICATED_AURORA)


class TestStrategyFeasibility(unittest.TestCase):
    def test_fully_replicated_profile_only_flags_sqs(self) -> None:
        """数据层齐备时，唯一剩下的应是 SQS——它结构上就不能复制。"""
        unmet = DRProfile(FIXTURE).strategy_feasibility()
        components = {u["component"] for u in unmet}
        self.assertEqual(components, {"sqs"})
        sqs = [u for u in unmet if u["component"] == "sqs"][0]
        self.assertEqual(sqs["actual"], "not_replicable")
        self.assertIn("in-flight", sqs["implication"])

    def test_petsite_profile_reports_unmet_data_preconditions(self) -> None:
        """回归锚点：PetAdoptions 当前零跨区复制，必须报出前提不成立。

        依据（2026-09-05 核对 CDK）：
          services-eks.ts:150  rds.DatabaseCluster  — 无 GlobalCluster
          services-eks.ts:795/849  ddb.Table        — 无 replicationRegions
          services-eks.ts:68   s3.Bucket            — 无复制配置

        若哪天这个用例开始失败，说明 CDK 里补上了跨区复制（好事），
        届时应同步把 petsite.yaml 的 topology 改掉，而不是删掉这个断言。
        """
        if not os.path.exists(PARENT_PROFILE):
            self.skipTest(f"parent profile not present: {PARENT_PROFILE}")
        p = DRProfile(PARENT_PROFILE)
        unmet = p.strategy_feasibility()
        components = {u["component"] for u in unmet}

        self.assertIn("aurora", components, "Aurora 无跨区复制却未被报出")
        self.assertIn("dynamodb", components, "DynamoDB 无 Global Tables 却未被报出")
        self.assertIn("s3", components, "S3 无 CRR 却未被报出")
        self.assertIn("sqs", components)

        aurora = [u for u in unmet if u["component"] == "aurora"][0]
        self.assertEqual(aurora["actual"], "none")
        self.assertIn("小时级", aurora["implication"])

    def test_absent_component_is_not_flagged(self) -> None:
        """workload 里没有的组件不该被当成缺陷。"""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(
                "profile:\n  name: x\n"
                "dr:\n  strategy: pilot_light\n"
                "  data:\n"
                "    aurora:\n      topology: global_database\n"
            )
            path = fh.name
        try:
            unmet = DRProfile(path).strategy_feasibility()
            self.assertEqual(unmet, [], "只声明了达标的 aurora，不应有未满足项")
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
