"""
tests/test_registry.py — Unit tests for the Service Type Registry

Tests:
- YAML loading
- Custom types override
- Unknown type defaults + warning
- All fixture types resolve correctly
- Registry integration with graph_analyzer and spof_detector
"""

import json
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from registry.registry_loader import ServiceTypeInfo, ServiceTypeRegistry

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "az1_subgraph.json")

_CUSTOM_YAML = """\
service_types:
  DocumentDBCluster:
    layer: L0
    fault_domain: zonal
    spof_candidate: true
    has_step_builder: false
    switchover_type: promote_replica
    description: "DocumentDB 集群（兼容 MongoDB，单 AZ Writer）"
  RDSCluster:
    layer: L0
    fault_domain: zonal
    spof_candidate: true
    has_step_builder: true
    switchover_type: promote_replica
    description: "Custom override for RDSCluster"
"""


class TestYAMLLoading(unittest.TestCase):
    """Tests for loading the bundled service_types.yaml."""

    def setUp(self) -> None:
        self.registry = ServiceTypeRegistry()

    def test_rds_cluster_loaded(self) -> None:
        info = self.registry.get_type("RDSCluster")
        self.assertFalse(info.is_unknown)
        self.assertEqual(info.layer, "L0")

    def test_dynamodb_loaded(self) -> None:
        info = self.registry.get_type("DynamoDBTable")
        self.assertFalse(info.is_unknown)
        self.assertEqual(info.layer, "L0")
        self.assertEqual(info.fault_domain, "regional")
        self.assertFalse(info.spof_candidate)

    def test_cloudfront_loaded(self) -> None:
        info = self.registry.get_type("CloudFrontDistribution")
        self.assertEqual(info.layer, "L3")
        self.assertEqual(info.fault_domain, "global")

    def test_total_types_at_least_30(self) -> None:
        all_types = (
            self.registry.list_types_by_layer("L0")
            + self.registry.list_types_by_layer("L1")
            + self.registry.list_types_by_layer("L2")
            + self.registry.list_types_by_layer("L3")
        )
        self.assertGreaterEqual(len(all_types), 30)

    def test_returns_service_type_info_instance(self) -> None:
        info = self.registry.get_type("LambdaFunction")
        self.assertIsInstance(info, ServiceTypeInfo)


class TestCustomTypesOverride(unittest.TestCase):
    """Tests for custom_types.yaml merging."""

    def setUp(self) -> None:
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        self._tmp.write(_CUSTOM_YAML)
        self._tmp.close()

    def tearDown(self) -> None:
        os.unlink(self._tmp.name)

    def test_custom_type_added(self) -> None:
        registry = ServiceTypeRegistry(custom_path=self._tmp.name)
        info = registry.get_type("DocumentDBCluster")
        self.assertFalse(info.is_unknown)
        self.assertEqual(info.layer, "L0")
        self.assertEqual(info.switchover_type, "promote_replica")

    def test_custom_overrides_default(self) -> None:
        registry = ServiceTypeRegistry(custom_path=self._tmp.name)
        info = registry.get_type("RDSCluster")
        # The custom YAML overrides the description
        self.assertEqual(info.description, "Custom override for RDSCluster")

    def test_non_overridden_types_still_present(self) -> None:
        registry = ServiceTypeRegistry(custom_path=self._tmp.name)
        info = registry.get_type("LambdaFunction")
        self.assertFalse(info.is_unknown)
        self.assertEqual(info.layer, "L2")

    def test_missing_custom_path_raises(self) -> None:
        with self.assertRaises(FileNotFoundError):
            ServiceTypeRegistry(custom_path="/nonexistent/path/custom.yaml")


class TestUnknownTypeDefaults(unittest.TestCase):
    """Tests for unknown type conservative fallback behaviour."""

    def setUp(self) -> None:
        self.registry = ServiceTypeRegistry()

    def test_unknown_type_is_marked(self) -> None:
        info = self.registry.get_type("FrobnicatorX99")
        self.assertTrue(info.is_unknown)

    def test_unknown_type_defaults_layer_l2(self) -> None:
        info = self.registry.get_type("FrobnicatorX99")
        self.assertEqual(info.layer, "L2")

    def test_unknown_type_defaults_fault_domain_zonal(self) -> None:
        info = self.registry.get_type("FrobnicatorX99")
        self.assertEqual(info.fault_domain, "zonal")

    def test_unknown_type_defaults_spof_candidate_true(self) -> None:
        self.assertTrue(self.registry.is_spof_candidate("FrobnicatorX99"))

    def test_unknown_type_emits_warning(self) -> None:
        with self.assertLogs("registry.registry_loader", level="WARNING") as cm:
            self.registry.get_type("MysteryService999")
        self.assertTrue(
            any("MysteryService999" in line for line in cm.output)
        )

    def test_get_layer_unknown_returns_l2(self) -> None:
        self.assertEqual(self.registry.get_layer("NonExistentType"), "L2")

    def test_get_fault_domain_unknown_returns_zonal(self) -> None:
        self.assertEqual(self.registry.get_fault_domain("NonExistentType"), "zonal")


class TestLookupMethods(unittest.TestCase):
    """Tests for registry lookup API methods."""

    def setUp(self) -> None:
        self.registry = ServiceTypeRegistry()

    def test_list_types_by_layer_l0(self) -> None:
        l0 = self.registry.list_types_by_layer("L0")
        self.assertIn("RDSCluster", l0)
        self.assertIn("DynamoDBTable", l0)
        self.assertIn("S3Bucket", l0)

    def test_list_types_by_layer_l3(self) -> None:
        l3 = self.registry.list_types_by_layer("L3")
        self.assertIn("LoadBalancer", l3)
        self.assertIn("CloudFrontDistribution", l3)
        self.assertIn("Route53Record", l3)

    def test_list_types_by_layer_returns_sorted(self) -> None:
        l2 = self.registry.list_types_by_layer("L2")
        self.assertEqual(l2, sorted(l2))

    def test_list_spof_candidates_includes_rds(self) -> None:
        candidates = self.registry.list_spof_candidates()
        self.assertIn("RDSCluster", candidates)
        self.assertIn("EC2Instance", candidates)
        self.assertIn("EKSCluster", candidates)

    def test_list_spof_candidates_excludes_dynamodb(self) -> None:
        candidates = self.registry.list_spof_candidates()
        self.assertNotIn("DynamoDBTable", candidates)

    def test_list_spof_candidates_excludes_lambda(self) -> None:
        candidates = self.registry.list_spof_candidates()
        self.assertNotIn("LambdaFunction", candidates)

    def test_is_spof_candidate_rds_true(self) -> None:
        self.assertTrue(self.registry.is_spof_candidate("RDSCluster"))

    def test_is_spof_candidate_dynamodb_false(self) -> None:
        self.assertFalse(self.registry.is_spof_candidate("DynamoDBTable"))

    def test_get_fault_domain_global(self) -> None:
        self.assertEqual(self.registry.get_fault_domain("Route53Record"), "global")

    def test_switchover_type_rds(self) -> None:
        info = self.registry.get_type("RDSCluster")
        self.assertEqual(info.switchover_type, "promote_replica")


class TestAllFixtureTypesResolve(unittest.TestCase):
    """All resource types from the fixture subgraph must resolve without is_unknown."""

    def setUp(self) -> None:
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            self.fixture = json.load(fh)
        self.registry = ServiceTypeRegistry()

    def test_all_fixture_types_known(self) -> None:
        for node in self.fixture["nodes"]:
            rtype = node.get("type", "")
            with self.subTest(type=rtype):
                info = self.registry.get_type(rtype)
                self.assertFalse(
                    info.is_unknown,
                    msg=f"Type {rtype!r} from fixture is not in service_types.yaml",
                )


class TestGraphAnalyzerIntegration(unittest.TestCase):
    """Registry integration tests with GraphAnalyzer."""

    def setUp(self) -> None:
        from graph.graph_analyzer import GraphAnalyzer
        self.registry = ServiceTypeRegistry()
        self.analyzer = GraphAnalyzer(registry=self.registry)
        with open(FIXTURE_PATH, encoding="utf-8") as fh:
            self.fixture = json.load(fh)

    def test_classify_by_layer_uses_registry(self) -> None:
        layers = self.analyzer.classify_by_layer(self.fixture)
        l0_names = [n["name"] for n in layers["L0"]]
        self.assertIn("petsite-db", l0_names)  # RDSCluster → L0
        self.assertIn("petsearch-db", l0_names)  # DynamoDBTable → L0

    def test_unknown_type_goes_to_l2_via_registry(self) -> None:
        subgraph = {
            "nodes": [{"name": "mystery", "type": "AlienDB", "tier": None}],
            "edges": [],
        }
        layers = self.analyzer.classify_by_layer(subgraph)
        self.assertIn("mystery", [n["name"] for n in layers["L2"]])

    def test_registry_injected_correctly(self) -> None:
        self.assertIs(self.analyzer._registry, self.registry)


class TestSPOFDetectorIntegration(unittest.TestCase):
    """Registry integration tests with SPOFDetector."""

    def setUp(self) -> None:
        from assessment.spof_detector import SPOFDetector
        self.registry = ServiceTypeRegistry()
        self.detector = SPOFDetector(registry=self.registry)

    def test_rds_with_two_dependents_flagged(self) -> None:
        subgraph = {
            "nodes": [
                {"name": "pet-db", "type": "RDSCluster", "az": "apne1-az1"},
                {"name": "svc-a", "type": "Microservice", "az": "apne1-az1"},
                {"name": "svc-b", "type": "Microservice", "az": "apne1-az1"},
            ],
            "edges": [
                {"from": "svc-a", "to": "pet-db", "type": "AccessesData"},
                {"from": "svc-b", "to": "pet-db", "type": "AccessesData"},
            ],
        }
        spofs = self.detector._detect_from_subgraph(subgraph)
        self.assertEqual(len(spofs), 1)
        self.assertEqual(spofs[0]["resource"], "pet-db")

    def test_dynamodb_not_flagged_as_spof(self) -> None:
        subgraph = {
            "nodes": [
                {"name": "pets-table", "type": "DynamoDBTable", "az": "apne1-az1"},
                {"name": "svc-a", "type": "Microservice", "az": "apne1-az1"},
                {"name": "svc-b", "type": "Microservice", "az": "apne1-az1"},
            ],
            "edges": [
                {"from": "svc-a", "to": "pets-table", "type": "AccessesData"},
                {"from": "svc-b", "to": "pets-table", "type": "AccessesData"},
            ],
        }
        spofs = self.detector._detect_from_subgraph(subgraph)
        self.assertEqual(len(spofs), 0)

    def test_ec2_with_two_dependents_flagged(self) -> None:
        subgraph = {
            "nodes": [
                {"name": "my-ec2", "type": "EC2Instance", "az": "apne1-az1"},
                {"name": "svc-a", "type": "Microservice", "az": "apne1-az1"},
                {"name": "svc-b", "type": "Microservice", "az": "apne1-az1"},
            ],
            "edges": [
                {"from": "svc-a", "to": "my-ec2", "type": "RunsOn"},
                {"from": "svc-b", "to": "my-ec2", "type": "RunsOn"},
            ],
        }
        spofs = self.detector._detect_from_subgraph(subgraph)
        self.assertEqual(len(spofs), 1)

    def test_rds_single_dependent_not_flagged(self) -> None:
        subgraph = {
            "nodes": [
                {"name": "pet-db", "type": "RDSCluster", "az": "apne1-az1"},
                {"name": "svc-a", "type": "Microservice", "az": "apne1-az1"},
            ],
            "edges": [
                {"from": "svc-a", "to": "pet-db", "type": "AccessesData"},
            ],
        }
        spofs = self.detector._detect_from_subgraph(subgraph)
        self.assertEqual(len(spofs), 0)

    def test_unknown_type_treated_as_spof_candidate(self) -> None:
        # Unknown types default to spof_candidate=True (conservative)
        subgraph = {
            "nodes": [
                {"name": "alien-db", "type": "AlienDBCluster", "az": "apne1-az1"},
                {"name": "svc-a", "type": "Microservice", "az": "apne1-az1"},
                {"name": "svc-b", "type": "Microservice", "az": "apne1-az1"},
            ],
            "edges": [
                {"from": "svc-a", "to": "alien-db", "type": "AccessesData"},
                {"from": "svc-b", "to": "alien-db", "type": "AccessesData"},
            ],
        }
        spofs = self.detector._detect_from_subgraph(subgraph)
        self.assertEqual(len(spofs), 1)


if __name__ == "__main__":
    unittest.main()
