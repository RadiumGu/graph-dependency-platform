"""
tests/test_data_layer_steps.py — 数据层步骤按 mode 与拓扑分流

最重要的两条：
- test_failover_mode_must_pass_allow_data_loss：省略该 flag 会被 AWS 默认当成
  switchover，而 switchover 要求主区健康——真灾时那条命令会失败。
- test_never_emits_in_cluster_failover_api：``failover-db-cluster`` 是集群内
  AZ 级切换，用于跨区是原实现的核心错误，必须永不再出现。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dr_profile import set_active_profile
from planner.step_builder import (
    MODE_DRILL,
    MODE_FAILOVER,
    StepBuilder,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "test_profile.yaml")
SOURCE, TARGET = "eu-west-1", "eu-central-1"


def _node(name: str, node_type: str, tier: str = "Tier0") -> dict:
    return {"name": name, "type": node_type, "tier": tier}


def _profile_with(dr_data: str) -> str:
    """写一份只含指定 dr.data 的临时 profile，返回路径（调用方负责删除）。"""
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        fh.write(
            "profile:\n  name: t\n"
            "kubernetes:\n  namespace: t\n"
            "dr:\n  strategy: warm_standby\n"
            "  data:\n" + dr_data
        )
        return fh.name


class TestAuroraModeBranching(unittest.TestCase):
    def setUp(self) -> None:
        set_active_profile(FIXTURE)  # aurora topology = global_database

    def test_drill_uses_switchover(self) -> None:
        """演练要零数据丢失，且保持复制拓扑。"""
        step = StepBuilder(mode=MODE_DRILL).build_step(
            _node("acme-db", "RDSCluster"), SOURCE, TARGET
        )
        self.assertEqual(step.action, "switchover_global_cluster")
        self.assertIn("aws rds switchover-global-cluster", step.command)
        self.assertNotIn("--allow-data-loss", step.command)

    def test_failover_uses_failover_api(self) -> None:
        step = StepBuilder(mode=MODE_FAILOVER).build_step(
            _node("acme-db", "RDSCluster"), SOURCE, TARGET
        )
        self.assertEqual(step.action, "failover_global_cluster")
        self.assertIn("aws rds failover-global-cluster", step.command)

    def test_failover_mode_must_pass_allow_data_loss(self) -> None:
        """AWS 文档原文：不指定 AllowDataLoss 时操作**默认降级为 switchover**。

        而 switchover 要求 global cluster 健康。真灾时主区已不可达，
        缺这个 flag 的命令会失败——这是最容易漏、后果最重的一处。
        """
        step = StepBuilder(mode=MODE_FAILOVER).build_step(
            _node("acme-db", "RDSCluster"), SOURCE, TARGET
        )
        self.assertIn("--allow-data-loss", step.command)
        # 与 --switchover 互斥，不能同时出现
        self.assertNotIn("--switchover ", step.command)

    def test_never_emits_in_cluster_failover_api(self) -> None:
        """``failover-db-cluster`` 是集群内 AZ 级切换，跨区无效。回归锚点。"""
        for mode in (MODE_DRILL, MODE_FAILOVER):
            step = StepBuilder(mode=mode).build_step(
                _node("acme-db", "RDSCluster"), SOURCE, TARGET
            )
            self.assertNotIn("failover-db-cluster", step.command, f"mode={mode}")
            self.assertNotIn("failover-db-cluster", step.rollback_command, f"mode={mode}")

    def test_uses_global_cluster_identifier_from_profile(self) -> None:
        step = StepBuilder(mode=MODE_DRILL).build_step(
            _node("acme-db", "RDSCluster"), SOURCE, TARGET
        )
        self.assertIn("--global-cluster-identifier acme-global", step.command)

    def test_rollback_prefers_switchover_not_failover(self) -> None:
        """回切是计划内操作，应零丢失。"""
        step = StepBuilder(mode=MODE_FAILOVER).build_step(
            _node("acme-db", "RDSCluster"), SOURCE, TARGET
        )
        self.assertIn("switchover-global-cluster", step.rollback_command)

    def test_missing_target_arn_records_a_gap(self) -> None:
        """跨区提升要求 ARN，裸 identifier 定位不到其它 Region 的集群。

        fixture 未提供 target_cluster_arn，故应记缺口并给出可辨识的占位，
        而不是拼一个看着像 ARN 的字符串让人直接执行。
        """
        builder = StepBuilder(mode=MODE_DRILL)
        step = builder.build_step(_node("acme-db", "RDSCluster"), SOURCE, TARGET)
        self.assertTrue(any(g["component"] == "aurora" for g in builder.data_step_gaps))
        self.assertIn("<arn:aws:rds:", step.command)


class TestAuroraWithoutReplica(unittest.TestCase):
    def test_no_replica_produces_blocking_step_not_silence(self) -> None:
        """静默跳过会让计划看起来完整而数据层其实没切。

        生成一个 ``exit 1`` 的步骤，能保证演练时立刻暴露，而不是等到真灾。
        """
        path = _profile_with("    aurora:\n      topology: none\n")
        try:
            set_active_profile(path)
            builder = StepBuilder(mode=MODE_FAILOVER)
            step = builder.build_step(_node("plain-db", "RDSCluster"), SOURCE, TARGET)
            self.assertEqual(step.action, "blocked_no_cross_region_replica")
            self.assertIn("exit 1", step.command)
            self.assertIn("copy-db-cluster-snapshot", step.command)
            self.assertTrue(builder.data_step_gaps)
        finally:
            os.unlink(path)


class TestDynamoDBSteps(unittest.TestCase):
    def test_global_table_step_is_verification_only(self) -> None:
        set_active_profile(FIXTURE)  # dynamodb topology = global_tables
        step = StepBuilder().build_step(_node("acme-orders", "DynamoDBTable"), SOURCE, TARGET)
        self.assertEqual(step.action, "verify_global_table_replica")
        self.assertNotIn("put-parameter", step.command)
        self.assertEqual(step.expected_result, "ACTIVE")
        self.assertFalse(step.requires_approval, "纯校验不应要求审批")

    def test_without_global_tables_blocks(self) -> None:
        path = _profile_with("    dynamodb:\n      topology: none\n")
        try:
            set_active_profile(path)
            builder = StepBuilder()
            step = builder.build_step(_node("plain-table", "DynamoDBTable"), SOURCE, TARGET)
            self.assertEqual(step.action, "blocked_no_global_table")
            self.assertIn("exit 1", step.command)
            self.assertTrue(any(g["component"] == "dynamodb" for g in builder.data_step_gaps))
        finally:
            os.unlink(path)


class TestS3Steps(unittest.TestCase):
    def test_crr_step_checks_pending_replication(self) -> None:
        set_active_profile(FIXTURE)  # s3 topology = crr
        step = StepBuilder().build_step(_node("acme-receipts", "S3Bucket"), SOURCE, TARGET)
        self.assertEqual(step.action, "verify_crr_caught_up")
        self.assertIn("OperationsPendingReplication", step.command)
        self.assertIn(f"--region {SOURCE}", step.command)

    def test_without_crr_blocks_and_records_gap(self) -> None:
        path = _profile_with("    s3:\n      topology: none\n")
        try:
            set_active_profile(path)
            builder = StepBuilder()
            step = builder.build_step(_node("plain-bucket", "S3Bucket"), SOURCE, TARGET)
            self.assertEqual(step.action, "blocked_no_crr")
            self.assertIn("exit 1", step.command)
            gap = [g for g in builder.data_step_gaps if g["component"] == "s3"][0]
            self.assertIn("404", gap["implication"])
        finally:
            os.unlink(path)


class TestSQSSteps(unittest.TestCase):
    def setUp(self) -> None:
        set_active_profile(FIXTURE)

    def test_quantifies_message_loss_from_source_region(self) -> None:
        """必须查**源区**的消息数——那才是丢失量。"""
        step = StepBuilder().build_step(_node("acme-queue", "SQSQueue"), SOURCE, TARGET)
        self.assertEqual(step.action, "quantify_message_loss")
        self.assertIn("ApproximateNumberOfMessages", step.command)
        self.assertIn("ApproximateNumberOfMessagesNotVisible", step.command)
        self.assertIn(f"--region {SOURCE}", step.command)

    def test_counts_in_flight_messages_too(self) -> None:
        """只数 visible 会低估丢失量：in-flight 的消息同样丢。"""
        step = StepBuilder().build_step(_node("acme-queue", "SQSQueue"), SOURCE, TARGET)
        self.assertIn("NotVisible", step.command)

    def test_rollback_states_loss_is_irreversible(self) -> None:
        step = StepBuilder().build_step(_node("acme-queue", "SQSQueue"), SOURCE, TARGET)
        self.assertIn("无法回滚", step.rollback_command)

    def test_requires_approval_because_data_is_lost(self) -> None:
        step = StepBuilder().build_step(_node("acme-queue", "SQSQueue"), SOURCE, TARGET)
        self.assertTrue(step.requires_approval)


class TestGapsReachThePlan(unittest.TestCase):
    def test_step_gaps_merge_into_plan_data_layer_gaps(self) -> None:
        """步骤级缺口必须进产物，否则「计划里有一步注定失败」在执行前不可见。"""
        from graph.graph_analyzer import GraphAnalyzer
        from planner.plan_generator import PlanGenerator

        path = _profile_with(
            "    aurora:\n      topology: none\n"
            "    dynamodb:\n      topology: none\n"
        )
        try:
            set_active_profile(path)
            gen = PlanGenerator(GraphAnalyzer(), StepBuilder(mode=MODE_FAILOVER))
            plan = gen.generate_plan(
                scope="region", source=SOURCE, target=TARGET,
                snapshot={
                    "nodes": [_node("plain-db", "RDSCluster")],
                    "edges": [],
                },
            )
            components = {g["component"] for g in plan.data_layer_gaps}
            self.assertIn("aurora", components)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
