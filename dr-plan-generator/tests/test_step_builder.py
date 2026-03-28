"""
tests/test_step_builder.py — Unit tests for StepBuilder

Verifies that each resource type produces a properly populated DRStep
with non-empty command, validation, and rollback_command fields.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models import DRStep
from planner.step_builder import StepBuilder


SOURCE = "ap-northeast-1"
TARGET = "us-west-2"


def _make_node(name: str, rtype: str, tier: str = "Tier1") -> dict:
    return {"name": name, "type": rtype, "tier": tier, "id": f"id-{name}"}


class TestRDSClusterStep(unittest.TestCase):

    def setUp(self) -> None:
        self.builder = StepBuilder()

    def test_produces_drstep(self) -> None:
        step = self.builder.build_step(_make_node("pet-db", "RDSCluster", "Tier0"), SOURCE, TARGET)
        self.assertIsInstance(step, DRStep)

    def test_step_id_contains_resource_name(self) -> None:
        step = self.builder.build_step(_make_node("pet-db", "RDSCluster"), SOURCE, TARGET)
        self.assertIn("pet-db", step.step_id)

    def test_resource_type_is_rdscluster(self) -> None:
        step = self.builder.build_step(_make_node("pet-db", "RDSCluster"), SOURCE, TARGET)
        self.assertEqual(step.resource_type, "RDSCluster")

    def test_command_contains_target_region(self) -> None:
        step = self.builder.build_step(_make_node("pet-db", "RDSCluster"), SOURCE, TARGET)
        self.assertIn(TARGET, step.command)

    def test_rollback_command_contains_source_region(self) -> None:
        step = self.builder.build_step(_make_node("pet-db", "RDSCluster"), SOURCE, TARGET)
        self.assertIn(SOURCE, step.rollback_command)

    def test_rollback_command_not_empty(self) -> None:
        step = self.builder.build_step(_make_node("pet-db", "RDSCluster"), SOURCE, TARGET)
        self.assertTrue(step.rollback_command)

    def test_requires_approval_for_rds(self) -> None:
        step = self.builder.build_step(_make_node("pet-db", "RDSCluster"), SOURCE, TARGET)
        self.assertTrue(step.requires_approval)

    def test_estimated_time_is_positive(self) -> None:
        step = self.builder.build_step(_make_node("pet-db", "RDSCluster"), SOURCE, TARGET)
        self.assertGreater(step.estimated_time, 0)

    def test_action_is_promote_read_replica(self) -> None:
        step = self.builder.build_step(_make_node("pet-db", "RDSCluster"), SOURCE, TARGET)
        self.assertEqual(step.action, "promote_read_replica")


class TestDynamoDBTableStep(unittest.TestCase):

    def setUp(self) -> None:
        self.builder = StepBuilder()

    def test_produces_drstep(self) -> None:
        step = self.builder.build_step(_make_node("pets-table", "DynamoDBTable"), SOURCE, TARGET)
        self.assertIsInstance(step, DRStep)

    def test_command_references_target(self) -> None:
        step = self.builder.build_step(_make_node("pets-table", "DynamoDBTable"), SOURCE, TARGET)
        self.assertIn(TARGET, step.command)

    def test_rollback_command_references_source(self) -> None:
        step = self.builder.build_step(_make_node("pets-table", "DynamoDBTable"), SOURCE, TARGET)
        self.assertIn(SOURCE, step.rollback_command)

    def test_expected_result_is_active(self) -> None:
        step = self.builder.build_step(_make_node("pets-table", "DynamoDBTable"), SOURCE, TARGET)
        self.assertEqual(step.expected_result, "ACTIVE")

    def test_action_is_switch_global_table(self) -> None:
        step = self.builder.build_step(_make_node("pets-table", "DynamoDBTable"), SOURCE, TARGET)
        self.assertEqual(step.action, "switch_global_table_region")


class TestMicroserviceStep(unittest.TestCase):

    def setUp(self) -> None:
        self.builder = StepBuilder()

    def test_produces_drstep(self) -> None:
        step = self.builder.build_step(_make_node("petsite", "Microservice", "Tier0"), SOURCE, TARGET)
        self.assertIsInstance(step, DRStep)

    def test_tier0_requires_approval(self) -> None:
        step = self.builder.build_step(_make_node("petsite", "Microservice", "Tier0"), SOURCE, TARGET)
        self.assertTrue(step.requires_approval)

    def test_tier2_no_approval_required(self) -> None:
        step = self.builder.build_step(_make_node("petsite", "Microservice", "Tier2"), SOURCE, TARGET)
        self.assertFalse(step.requires_approval)

    def test_command_contains_kubectl(self) -> None:
        step = self.builder.build_step(_make_node("petsite", "Microservice"), SOURCE, TARGET)
        self.assertIn("kubectl", step.command)

    def test_rollback_command_scales_to_zero(self) -> None:
        step = self.builder.build_step(_make_node("petsite", "Microservice"), SOURCE, TARGET)
        self.assertIn("--replicas=0", step.rollback_command)

    def test_tier_stored_on_step(self) -> None:
        step = self.builder.build_step(_make_node("petsite", "Microservice", "Tier0"), SOURCE, TARGET)
        self.assertEqual(step.tier, "Tier0")


class TestLoadBalancerStep(unittest.TestCase):

    def setUp(self) -> None:
        self.builder = StepBuilder()

    def test_produces_drstep(self) -> None:
        step = self.builder.build_step(_make_node("petsite-alb", "LoadBalancer"), SOURCE, TARGET)
        self.assertIsInstance(step, DRStep)

    def test_command_references_route53(self) -> None:
        step = self.builder.build_step(_make_node("petsite-alb", "LoadBalancer"), SOURCE, TARGET)
        self.assertIn("route53", step.command.lower())

    def test_requires_approval(self) -> None:
        step = self.builder.build_step(_make_node("petsite-alb", "LoadBalancer"), SOURCE, TARGET)
        self.assertTrue(step.requires_approval)

    def test_rollback_command_not_empty(self) -> None:
        step = self.builder.build_step(_make_node("petsite-alb", "LoadBalancer"), SOURCE, TARGET)
        self.assertTrue(step.rollback_command)


class TestLambdaFunctionStep(unittest.TestCase):

    def setUp(self) -> None:
        self.builder = StepBuilder()

    def test_produces_drstep(self) -> None:
        step = self.builder.build_step(_make_node("my-fn", "LambdaFunction"), SOURCE, TARGET)
        self.assertIsInstance(step, DRStep)

    def test_command_contains_lambda_invoke(self) -> None:
        step = self.builder.build_step(_make_node("my-fn", "LambdaFunction"), SOURCE, TARGET)
        self.assertIn("lambda invoke", step.command)

    def test_expected_result_is_active(self) -> None:
        step = self.builder.build_step(_make_node("my-fn", "LambdaFunction"), SOURCE, TARGET)
        self.assertEqual(step.expected_result, "Active")

    def test_no_approval_required_by_default(self) -> None:
        step = self.builder.build_step(_make_node("my-fn", "LambdaFunction"), SOURCE, TARGET)
        self.assertFalse(step.requires_approval)


class TestK8sServiceStep(unittest.TestCase):

    def setUp(self) -> None:
        self.builder = StepBuilder()

    def test_produces_drstep(self) -> None:
        step = self.builder.build_step(_make_node("petsite-svc", "K8sService"), SOURCE, TARGET)
        self.assertIsInstance(step, DRStep)

    def test_command_contains_kubectl_get_endpoints(self) -> None:
        step = self.builder.build_step(_make_node("petsite-svc", "K8sService"), SOURCE, TARGET)
        self.assertIn("kubectl", step.command)
        self.assertIn("endpoints", step.command)


class TestGenericFallback(unittest.TestCase):

    def setUp(self) -> None:
        self.builder = StepBuilder()

    def test_unknown_type_gets_generic_step(self) -> None:
        node = {"name": "mystery", "type": "Frobnicator", "tier": None}
        step = self.builder.build_step(node, SOURCE, TARGET)
        self.assertIsInstance(step, DRStep)

    def test_generic_action_is_manual(self) -> None:
        node = {"name": "mystery", "type": "Frobnicator", "tier": None}
        step = self.builder.build_step(node, SOURCE, TARGET)
        self.assertEqual(step.action, "manual_switchover")

    def test_generic_requires_approval(self) -> None:
        node = {"name": "mystery", "type": "Frobnicator", "tier": None}
        step = self.builder.build_step(node, SOURCE, TARGET)
        self.assertTrue(step.requires_approval)

    def test_generic_rollback_command_not_empty(self) -> None:
        node = {"name": "mystery", "type": "Frobnicator", "tier": None}
        step = self.builder.build_step(node, SOURCE, TARGET)
        self.assertTrue(step.rollback_command)


class TestContextOrder(unittest.TestCase):

    def setUp(self) -> None:
        self.builder = StepBuilder()

    def test_order_is_set_from_context(self) -> None:
        node = _make_node("petsite", "Microservice")
        step = self.builder.build_step(node, SOURCE, TARGET, context={"order": 5})
        self.assertEqual(step.order, 5)


if __name__ == "__main__":
    unittest.main()
