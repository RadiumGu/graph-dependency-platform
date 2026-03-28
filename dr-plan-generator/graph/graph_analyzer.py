"""
graph/graph_analyzer.py — Dependency graph analysis engine

Handles subgraph extraction, layer classification, topological sort
(Kahn's algorithm), parallel group detection, cycle detection (DFS coloring),
and critical path analysis.
"""

import logging
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from graph import queries

logger = logging.getLogger(__name__)


class GraphAnalyzer:
    """Dependency graph analysis engine.

    Extracts affected subgraphs from Neptune, classifies nodes into layers,
    performs topological sort within each layer, detects parallel groups,
    finds critical paths, and detects dependency cycles.
    """

    # Resource type → switchover layer mapping
    LAYER_MAP: Dict[str, str] = {
        # L0 — data layer (switch first)
        "RDSCluster": "L0",
        "RDSInstance": "L0",
        "DynamoDBTable": "L0",
        "NeptuneCluster": "L0",
        "NeptuneInstance": "L0",
        "S3Bucket": "L0",
        "SQSQueue": "L0",
        "SNSTopic": "L0",
        # L1 — infrastructure layer
        "EC2Instance": "L1",
        "EKSCluster": "L1",
        "Pod": "L1",
        "SecurityGroup": "L1",
        # L2 — application layer
        "K8sService": "L2",
        "Microservice": "L2",
        "LambdaFunction": "L2",
        "StepFunction": "L2",
        "BusinessCapability": "L2",
        # L3 — traffic layer (switch last)
        "LoadBalancer": "L3",
        "TargetGroup": "L3",
        "ListenerRule": "L3",
    }

    # Tier priority for stable sort within Kahn's algorithm
    _TIER_ORDER = {"Tier0": 0, "Tier1": 1, "Tier2": 2}

    def extract_affected_subgraph(self, scope: str, source: str) -> Dict[str, Any]:
        """Extract the subgraph of nodes affected by a failure.

        Args:
            scope: One of ``region``, ``az``, or ``service``.
            source: The failure source identifier (region/az/service name).

        Returns:
            Dict with keys ``nodes`` (list of node dicts) and
            ``edges`` (list of edge dicts with ``from``, ``to``, ``type``).
        """
        if scope == "az":
            nodes = queries.q12_az_dependency_tree(source)
        elif scope == "region":
            nodes = queries.q12_az_dependency_tree_by_region(source)
        elif scope == "service":
            nodes = queries.q12_service_dependency_tree(source)
        else:
            raise ValueError(f"Unknown scope: {scope!r}. Must be region, az, or service.")

        return self._enrich_with_edges(nodes)

    def extract_affected_subgraph_from_data(
        self, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Build a subgraph dict from pre-loaded nodes and edges (for testing).

        Args:
            nodes: List of node dicts.
            edges: List of edge dicts.

        Returns:
            Subgraph dict with ``nodes`` and ``edges`` keys.
        """
        return {"nodes": nodes, "edges": edges}

    def _enrich_with_edges(self, nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Fetch edges between nodes and attach them to the subgraph.

        Args:
            nodes: List of node dicts (must have a ``name`` key).

        Returns:
            Dict with ``nodes`` and ``edges``.
        """
        node_names = [n["name"] for n in nodes if n.get("name")]
        try:
            edges = queries.q_edges_for_subgraph(node_names)
        except Exception as exc:
            logger.warning("Failed to fetch edges from Neptune: %s", exc)
            edges = []
        return {"nodes": nodes, "edges": edges}

    def classify_by_layer(self, subgraph: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
        """Classify nodes in a subgraph into switchover layers.

        Args:
            subgraph: Dict with ``nodes`` key.

        Returns:
            Dict mapping layer keys (``L0``–``L3``) to lists of node dicts.
        """
        layers: Dict[str, List[Dict[str, Any]]] = {"L0": [], "L1": [], "L2": [], "L3": []}
        for node in subgraph["nodes"]:
            layer = self.LAYER_MAP.get(node.get("type", ""), "L2")
            layers[layer].append(node)
        return layers

    def topological_sort_within_layer(
        self,
        layer_nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
    ) -> List[str]:
        """Sort nodes within a layer using Kahn's algorithm.

        Nodes with no intra-layer dependencies come first (they should be
        switched first). Ties are broken by Tier (Tier0 before Tier1/2).

        Args:
            layer_nodes: Nodes belonging to this layer.
            edges: All edges in the subgraph (only same-layer edges are used).

        Returns:
            Ordered list of node names.
        """
        node_map = {n["name"]: n for n in layer_nodes}
        node_names = set(node_map.keys())

        in_degree: Dict[str, int] = {n: 0 for n in node_names}
        adj: Dict[str, List[str]] = {n: [] for n in node_names}

        for edge in edges:
            src = edge.get("from", "")
            dst = edge.get("to", "")
            if src in node_names and dst in node_names:
                adj[src].append(dst)
                in_degree[dst] += 1

        queue: List[str] = [n for n, deg in in_degree.items() if deg == 0]
        result: List[str] = []

        while queue:
            queue.sort(key=lambda x: self._tier_priority(node_map.get(x, {}).get("tier")))
            node = queue.pop(0)
            result.append(node)
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # If result length < node count, there's a cycle — append remaining
        if len(result) < len(node_names):
            logger.warning(
                "Cycle detected in layer during topological sort; "
                "%d nodes not reachable.",
                len(node_names) - len(result),
            )
            remaining = node_names - set(result)
            result.extend(sorted(remaining))

        return result

    def detect_parallel_groups(
        self,
        sorted_nodes: List[str],
        edges: List[Dict[str, Any]],
    ) -> List[Tuple[str, List[str]]]:
        """Identify groups of nodes that can be executed in parallel.

        Nodes with no dependency between them (within the same layer) are
        placed in the same parallel group.

        Args:
            sorted_nodes: Topologically sorted node names for a layer.
            edges: All subgraph edges.

        Returns:
            List of ``(group_id, [node_names])`` tuples.
        """
        # Build a set of (from→to) dependency pairs among sorted_nodes
        node_set = set(sorted_nodes)
        dep_pairs: set = set()
        for edge in edges:
            src = edge.get("from", "")
            dst = edge.get("to", "")
            if src in node_set and dst in node_set:
                dep_pairs.add((src, dst))

        # Walk in topological order; each node starts a new group only if
        # it depends on a node in the current group.
        groups: List[List[str]] = []
        current_group: List[str] = []
        current_set: set = set()

        for node in sorted_nodes:
            # Check if this node has a dependency on anything in current_group
            has_dep_in_group = any((dep, node) in dep_pairs for dep in current_set)
            if has_dep_in_group or not current_group:
                if has_dep_in_group:
                    groups.append(current_group)
                    current_group = [node]
                    current_set = {node}
                else:
                    current_group.append(node)
                    current_set.add(node)
            else:
                current_group.append(node)
                current_set.add(node)

        if current_group:
            groups.append(current_group)

        return [(f"pg-{i+1}", grp) for i, grp in enumerate(groups)]

    def find_critical_path(
        self,
        layers: Dict[str, List[Dict[str, Any]]],
        edges: List[Dict[str, Any]],
        default_times: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        """Find the longest path through all layers (determines minimum RTO).

        Args:
            layers: Layer-classified nodes dict (L0–L3).
            edges: All subgraph edges.
            default_times: Optional mapping of resource type → seconds.

        Returns:
            Dict with keys ``path`` (list of node names) and
            ``estimated_minutes`` (int).
        """
        from assessment.rto_estimator import RTOEstimator

        times = RTOEstimator.DEFAULT_TIMES if default_times is None else default_times

        # Gather all nodes
        all_nodes: List[Dict[str, Any]] = []
        for layer_nodes in layers.values():
            all_nodes.extend(layer_nodes)

        node_map = {n["name"]: n for n in all_nodes}
        node_names = set(node_map.keys())

        # Build adjacency with weights (estimated_time)
        adj: Dict[str, List[Tuple[str, int]]] = {n: [] for n in node_names}
        for edge in edges:
            src = edge.get("from", "")
            dst = edge.get("to", "")
            if src in node_names and dst in node_names:
                weight = times.get(node_map[dst].get("type", ""), 60)
                adj[src].append((dst, weight))

        # Longest path via DP on a DAG (topological order)
        topo_order = self._topo_sort_all(list(node_names), edges)
        dist: Dict[str, int] = {n: 0 for n in node_names}
        prev: Dict[str, Optional[str]] = {n: None for n in node_names}

        for n in topo_order:
            node_time = times.get(node_map[n].get("type", ""), 60)
            dist[n] = max(dist.get(n, 0), node_time)
            for neighbor, weight in adj.get(n, []):
                if dist[n] + weight > dist.get(neighbor, 0):
                    dist[neighbor] = dist[n] + weight
                    prev[neighbor] = n

        if not dist:
            return {"path": [], "estimated_minutes": 0}

        end_node = max(dist, key=lambda x: dist[x])
        path: List[str] = []
        cur: Optional[str] = end_node
        while cur is not None:
            path.append(cur)
            cur = prev.get(cur)
        path.reverse()

        return {
            "path": path,
            "estimated_minutes": max(1, dist[end_node] // 60),
        }

    def detect_cycles(
        self,
        nodes: List[Dict[str, Any]],
        edges: List[Dict[str, Any]],
    ) -> List[List[str]]:
        """Detect dependency cycles using DFS coloring.

        Args:
            nodes: List of node dicts with ``name`` key.
            edges: List of edge dicts with ``from`` and ``to`` keys.

        Returns:
            List of cycles; each cycle is a list of node names.
            Empty list means no cycles.
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {n["name"]: WHITE for n in nodes}
        node_names = set(color.keys())

        adj: Dict[str, List[str]] = {n: [] for n in node_names}
        for edge in edges:
            src = edge.get("from", "")
            dst = edge.get("to", "")
            if src in node_names and dst in node_names:
                adj[src].append(dst)

        cycles: List[List[str]] = []

        def dfs(node: str, path: List[str]) -> None:
            color[node] = GRAY
            path.append(node)
            for neighbor in adj.get(node, []):
                if color[neighbor] == GRAY:
                    cycle_start = path.index(neighbor)
                    cycles.append(path[cycle_start:] + [neighbor])
                elif color[neighbor] == WHITE:
                    dfs(neighbor, path)
            path.pop()
            color[node] = BLACK

        for node_name in list(color.keys()):
            if color[node_name] == WHITE:
                dfs(node_name, [])

        return cycles

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _tier_priority(self, tier: Optional[str]) -> int:
        """Return sort key for a tier string (lower = higher priority)."""
        return self._TIER_ORDER.get(tier or "", 99)

    def _topo_sort_all(
        self, node_names: List[str], edges: List[Dict[str, Any]]
    ) -> List[str]:
        """Topological sort across all nodes (Kahn's algorithm).

        Args:
            node_names: All node names.
            edges: All edges.

        Returns:
            Topologically sorted list of node names.
        """
        name_set = set(node_names)
        in_degree: Dict[str, int] = {n: 0 for n in name_set}
        adj: Dict[str, List[str]] = {n: [] for n in name_set}

        for edge in edges:
            src = edge.get("from", "")
            dst = edge.get("to", "")
            if src in name_set and dst in name_set:
                adj[src].append(dst)
                in_degree[dst] += 1

        q: deque = deque(n for n in name_set if in_degree[n] == 0)
        result: List[str] = []
        while q:
            node = q.popleft()
            result.append(node)
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    q.append(neighbor)

        # Append any remaining nodes (cycle members)
        remaining = name_set - set(result)
        result.extend(sorted(remaining))
        return result
