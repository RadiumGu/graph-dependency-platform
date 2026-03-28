"""
tests/test_graph_analyzer.py — Unit tests for GraphAnalyzer

Tests use fixtures/az1_subgraph.json mock data; no real Neptune connection.
"""

import json
import os
import sys
import unittest

# Ensure project root is on the path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from graph.graph_analyzer import GraphAnalyzer

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "az1_subgraph.json")


def _load_fixture() -> dict:
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


class TestLayerClassification(unittest.TestCase):
    """Tests for classify_by_layer()."""

    def setUp(self) -> None:
        self.analyzer = GraphAnalyzer()
        self.subgraph = _load_fixture()

    def test_rds_cluster_goes_to_l0(self) -> None:
        layers = self.analyzer.classify_by_layer(self.subgraph)
        l0_names = [n["name"] for n in layers["L0"]]
        self.assertIn("petsite-db", l0_names)

    def test_dynamodb_goes_to_l0(self) -> None:
        layers = self.analyzer.classify_by_layer(self.subgraph)
        l0_names = [n["name"] for n in layers["L0"]]
        self.assertIn("petsearch-db", l0_names)

    def test_microservice_goes_to_l2(self) -> None:
        layers = self.analyzer.classify_by_layer(self.subgraph)
        l2_names = [n["name"] for n in layers["L2"]]
        self.assertIn("petsite", l2_names)
        self.assertIn("petsearch", l2_names)

    def test_load_balancer_goes_to_l3(self) -> None:
        layers = self.analyzer.classify_by_layer(self.subgraph)
        l3_names = [n["name"] for n in layers["L3"]]
        self.assertIn("petsite-alb", l3_names)

    def test_lambda_function_goes_to_l2(self) -> None:
        layers = self.analyzer.classify_by_layer(self.subgraph)
        l2_names = [n["name"] for n in layers["L2"]]
        self.assertIn("petadoption-lambda", l2_names)

    def test_unknown_type_defaults_to_l2(self) -> None:
        subgraph = {
            "nodes": [{"name": "mystery-resource", "type": "UnknownType", "tier": None}],
            "edges": [],
        }
        layers = self.analyzer.classify_by_layer(subgraph)
        self.assertIn("mystery-resource", [n["name"] for n in layers["L2"]])

    def test_all_nodes_are_classified(self) -> None:
        layers = self.analyzer.classify_by_layer(self.subgraph)
        total_classified = sum(len(nodes) for nodes in layers.values())
        self.assertEqual(total_classified, len(self.subgraph["nodes"]))


class TestTopologicalSort(unittest.TestCase):
    """Tests for topological_sort_within_layer()."""

    def setUp(self) -> None:
        self.analyzer = GraphAnalyzer()

    def test_independent_nodes_all_returned(self) -> None:
        nodes = [
            {"name": "a", "type": "RDSCluster", "tier": "Tier0"},
            {"name": "b", "type": "DynamoDBTable", "tier": "Tier1"},
        ]
        result = self.analyzer.topological_sort_within_layer(nodes, [])
        self.assertEqual(set(result), {"a", "b"})

    def test_dependency_respects_order(self) -> None:
        # b depends on a (a→b edge), so a must come before b
        nodes = [
            {"name": "a", "type": "RDSCluster", "tier": "Tier1"},
            {"name": "b", "type": "RDSInstance", "tier": "Tier1"},
        ]
        edges = [{"from": "a", "to": "b", "type": "DependsOn"}]
        result = self.analyzer.topological_sort_within_layer(nodes, edges)
        self.assertLess(result.index("a"), result.index("b"))

    def test_tier0_sorted_before_tier1(self) -> None:
        nodes = [
            {"name": "tier1-resource", "type": "RDSCluster", "tier": "Tier1"},
            {"name": "tier0-resource", "type": "DynamoDBTable", "tier": "Tier0"},
        ]
        result = self.analyzer.topological_sort_within_layer(nodes, [])
        self.assertLess(result.index("tier0-resource"), result.index("tier1-resource"))

    def test_all_nodes_returned_even_with_cycle(self) -> None:
        nodes = [
            {"name": "x", "type": "RDSCluster", "tier": None},
            {"name": "y", "type": "RDSCluster", "tier": None},
        ]
        # Circular dependency
        edges = [
            {"from": "x", "to": "y", "type": "DependsOn"},
            {"from": "y", "to": "x", "type": "DependsOn"},
        ]
        result = self.analyzer.topological_sort_within_layer(nodes, edges)
        self.assertEqual(set(result), {"x", "y"})

    def test_cross_layer_edges_ignored(self) -> None:
        # Only L0 nodes; L2 edge should be ignored
        l0_nodes = [
            {"name": "db1", "type": "RDSCluster", "tier": "Tier0"},
            {"name": "db2", "type": "DynamoDBTable", "tier": "Tier0"},
        ]
        edges = [
            {"from": "svc1", "to": "db1", "type": "AccessesData"},  # svc1 not in layer
        ]
        result = self.analyzer.topological_sort_within_layer(l0_nodes, edges)
        self.assertEqual(set(result), {"db1", "db2"})


class TestCycleDetection(unittest.TestCase):
    """Tests for detect_cycles()."""

    def setUp(self) -> None:
        self.analyzer = GraphAnalyzer()

    def test_no_cycle_dag(self) -> None:
        nodes = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
        edges = [
            {"from": "a", "to": "b", "type": "Calls"},
            {"from": "b", "to": "c", "type": "Calls"},
        ]
        cycles = self.analyzer.detect_cycles(nodes, edges)
        self.assertEqual(cycles, [])

    def test_simple_cycle(self) -> None:
        nodes = [{"name": "a"}, {"name": "b"}]
        edges = [
            {"from": "a", "to": "b", "type": "Calls"},
            {"from": "b", "to": "a", "type": "Calls"},
        ]
        cycles = self.analyzer.detect_cycles(nodes, edges)
        self.assertGreater(len(cycles), 0)

    def test_self_loop(self) -> None:
        nodes = [{"name": "a"}, {"name": "b"}]
        edges = [{"from": "a", "to": "a", "type": "Calls"}]
        cycles = self.analyzer.detect_cycles(nodes, edges)
        self.assertGreater(len(cycles), 0)

    def test_longer_cycle(self) -> None:
        nodes = [{"name": n} for n in ["a", "b", "c", "d"]]
        edges = [
            {"from": "a", "to": "b", "type": "Calls"},
            {"from": "b", "to": "c", "type": "Calls"},
            {"from": "c", "to": "d", "type": "Calls"},
            {"from": "d", "to": "a", "type": "Calls"},
        ]
        cycles = self.analyzer.detect_cycles(nodes, edges)
        self.assertGreater(len(cycles), 0)

    def test_disconnected_graph_no_cycle(self) -> None:
        nodes = [{"name": "a"}, {"name": "b"}, {"name": "c"}, {"name": "d"}]
        edges = [
            {"from": "a", "to": "b", "type": "Calls"},
            {"from": "c", "to": "d", "type": "Calls"},
        ]
        cycles = self.analyzer.detect_cycles(nodes, edges)
        self.assertEqual(cycles, [])


class TestParallelGroups(unittest.TestCase):
    """Tests for detect_parallel_groups()."""

    def setUp(self) -> None:
        self.analyzer = GraphAnalyzer()

    def test_independent_nodes_in_one_group(self) -> None:
        sorted_nodes = ["a", "b", "c"]
        edges: list = []
        groups = self.analyzer.detect_parallel_groups(sorted_nodes, edges)
        # All independent → should be in a single group
        total_in_groups = sum(len(grp) for _, grp in groups)
        self.assertEqual(total_in_groups, 3)

    def test_dependent_nodes_in_separate_groups(self) -> None:
        sorted_nodes = ["a", "b"]
        edges = [{"from": "a", "to": "b", "type": "DependsOn"}]
        groups = self.analyzer.detect_parallel_groups(sorted_nodes, edges)
        # b depends on a → separate groups
        all_nodes = [n for _, grp in groups for n in grp]
        self.assertIn("a", all_nodes)
        self.assertIn("b", all_nodes)

    def test_returns_group_ids(self) -> None:
        sorted_nodes = ["x", "y"]
        edges: list = []
        groups = self.analyzer.detect_parallel_groups(sorted_nodes, edges)
        for group_id, _ in groups:
            self.assertTrue(group_id.startswith("pg-"))

    def test_empty_input(self) -> None:
        groups = self.analyzer.detect_parallel_groups([], [])
        self.assertEqual(groups, [])


class TestExtractFromData(unittest.TestCase):
    """Tests for extract_affected_subgraph_from_data()."""

    def setUp(self) -> None:
        self.analyzer = GraphAnalyzer()

    def test_roundtrip_fixture(self) -> None:
        fixture = _load_fixture()
        subgraph = self.analyzer.extract_affected_subgraph_from_data(
            fixture["nodes"], fixture["edges"]
        )
        self.assertEqual(len(subgraph["nodes"]), len(fixture["nodes"]))
        self.assertEqual(len(subgraph["edges"]), len(fixture["edges"]))


if __name__ == "__main__":
    unittest.main()
