"""
tests/test_scope.py — 白名单锚定的范围裁剪

关键用例：
- test_polluted_nodes_drop_out_without_being_named：DeepFlow 污染节点
  不点名也应出局（这是白名单相对黑名单的核心优势）
- test_refuted_edge_is_not_used_for_ordering：已证伪的边不得进入恢复顺序
- test_alb_and_queue_stay_in_scope：范围边 ≠ 排序边，ALB/队列属于 workload
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dr_profile import set_active_profile
from graph.scope import ScopeResolver

# 一个贴近实况的小图：真实 workload + 平台设施 + 污染节点
_WORKLOAD_NODES = [
    {"name": "storefront", "type": "Microservice", "tier": "Tier0"},
    {"name": "checkout", "type": "Microservice", "tier": "Tier1"},
    {"name": "acme-db", "type": "RDSCluster", "tier": "Tier0"},
    {"name": "acme-queue", "type": "SQSQueue", "tier": "Tier1"},
    {"name": "acme-alb", "type": "LoadBalancer", "tier": None},
]
_PLATFORM_NODES = [
    {"name": "graph-neptune", "type": "NeptuneCluster", "tier": "Tier1"},
    {"name": "etl_aws", "type": "LambdaFunction", "tier": "Tier2"},
    {"name": "etl_deepflow", "type": "LambdaFunction", "tier": "Tier2"},
]
_POLLUTED_NODES = [
    {"name": "gateway-service", "type": "Microservice", "namespace": "awesomeshop"},
    {"name": "order-service", "type": "Microservice", "namespace": "awesomeshop"},
    {"name": "artillery", "type": "Microservice", "namespace": "awesomeshop"},
]

_EDGES = [
    {"from": "storefront", "to": "checkout", "type": "Calls"},
    {"from": "checkout", "to": "acme-db", "type": "AccessesData"},
    {"from": "checkout", "to": "acme-queue", "type": "WritesTo"},
    {"from": "acme-alb", "to": "storefront", "type": "ForwardsTo"},
    # 污染节点之间自成一片，与 workload 无连接
    {"from": "gateway-service", "to": "order-service", "type": "Calls"},
]

_EXCLUDE_RULES = [
    {"pattern": "etl_*", "reason": "platform_infrastructure：图谱 ETL"},
    {"type": "NeptuneCluster", "reason": "platform_infrastructure：图谱自身存储"},
    {"namespace": "awesomeshop", "reason": "data_pollution：DeepFlow 虚构服务"},
]


def _resolver(**kwargs) -> ScopeResolver:
    params = dict(
        anchors=["storefront", "checkout"],
        excluded_rules=_EXCLUDE_RULES,
    )
    params.update(kwargs)
    return ScopeResolver(**params)


class TestReachabilityScoping(unittest.TestCase):
    def setUp(self) -> None:
        self.nodes = _WORKLOAD_NODES + _PLATFORM_NODES + _POLLUTED_NODES
        self.scoped = _resolver().resolve(self.nodes, _EDGES)

    def test_workload_nodes_are_kept(self) -> None:
        kept = {n["name"] for n in self.scoped.nodes}
        self.assertEqual(kept, {"storefront", "checkout", "acme-db",
                                "acme-queue", "acme-alb"})

    def test_alb_and_queue_stay_in_scope(self) -> None:
        """范围边 ≠ 排序边：ALB 与队列不是「依赖」，但属于这个 workload。

        若用 dependency=True 的 6 种边界定范围，ForwardsTo / WritesTo 会被漏掉，
        ALB 和队列就不进计划——那是切换时一定会出问题的遗漏。
        """
        kept = {n["name"] for n in self.scoped.nodes}
        self.assertIn("acme-alb", kept)
        self.assertIn("acme-queue", kept)

    def test_platform_infrastructure_excluded_with_reason(self) -> None:
        by_name = {e.name: e for e in self.scoped.excluded}
        self.assertIn("graph-neptune", by_name)
        self.assertIn("platform_infrastructure", by_name["graph-neptune"].reason)
        self.assertEqual(by_name["graph-neptune"].rule, "type=NeptuneCluster")
        self.assertIn("etl_aws", by_name)
        self.assertEqual(by_name["etl_aws"].rule, "pattern=etl_*")

    def test_polluted_nodes_drop_out_without_being_named(self) -> None:
        """白名单的核心优势：污染节点不必逐个点名。

        这里刻意**不**给 gateway-service / order-service / artillery 写任何
        按名字的规则。它们从 workload 锚点不可达，因此自动出局；
        黑名单模式下漏掉任何一个都会让虚构服务进入 DR 计划。
        """
        resolver = _resolver(excluded_rules=[])  # 完全不设排除规则
        scoped = resolver.resolve(
            _WORKLOAD_NODES + _POLLUTED_NODES, _EDGES
        )
        kept = {n["name"] for n in scoped.nodes}
        for polluted in ("gateway-service", "order-service", "artillery"):
            self.assertNotIn(polluted, kept, f"{polluted} 竟然进了范围")

        reasons = {e.name: e.reason for e in scoped.excluded}
        self.assertIn("out_of_scope", reasons["gateway-service"])

    def test_every_exclusion_carries_a_reason(self) -> None:
        """审计要求：不能有「无原因」的排除。"""
        for record in self.scoped.excluded:
            self.assertTrue(record.reason, f"{record.name} 缺少 reason")
            self.assertTrue(record.rule, f"{record.name} 缺少 rule")

    def test_anchor_diagnostics(self) -> None:
        resolver = _resolver(anchors=["storefront", "does-not-exist"])
        scoped = resolver.resolve(self.nodes, _EDGES)
        self.assertIn("storefront", scoped.anchors_matched)
        self.assertIn("does-not-exist", scoped.anchors_missing)

    def test_edges_are_trimmed_to_kept_nodes(self) -> None:
        names = {n["name"] for n in self.scoped.nodes}
        for e in self.scoped.edges:
            self.assertIn(e["from"], names)
            self.assertIn(e["to"], names)


class TestRefutedEdges(unittest.TestCase):
    def test_refuted_edge_is_not_used_for_ordering(self) -> None:
        """已证伪的边不得进入恢复顺序。

        采信一条被故障注入证伪的依赖，会让计划为一个**不存在的约束**串行等待，
        白白拉长 RTO。
        """
        edges = [
            {"from": "storefront", "to": "checkout", "type": "Calls",
             "verify_status": "refuted"},
            {"from": "checkout", "to": "acme-db", "type": "AccessesData",
             "verify_status": "confirmed"},
        ]
        ordering = _resolver().filter_ordering_edges(edges)
        types = [(e["from"], e["to"]) for e in ordering]
        self.assertNotIn(("storefront", "checkout"), types)
        self.assertIn(("checkout", "acme-db"), types)

    def test_untested_and_inconclusive_are_kept(self) -> None:
        """证据不足 ≠ 不存在。容灾场景宁可多算一条依赖。"""
        edges = [
            {"from": "a", "to": "b", "type": "Calls", "verify_status": "untested"},
            {"from": "b", "to": "c", "type": "Calls", "verify_status": "inconclusive"},
            {"from": "c", "to": "d", "type": "Calls"},  # 无该属性
        ]
        ordering = _resolver().filter_ordering_edges(edges)
        self.assertEqual(len(ordering), 3)

    def test_non_dependency_edges_excluded_from_ordering(self) -> None:
        """位置/归属关系不表达先后，不能进排序。

        Contains(Region→AZ)、LocatedIn、ForwardsTo 在契约里 dependency=False，
        用它们排序会产生无意义的串行约束。
        """
        edges = [
            {"from": "acme-alb", "to": "storefront", "type": "ForwardsTo"},
            {"from": "apne1", "to": "apne1-az1", "type": "Contains"},
            {"from": "storefront", "to": "checkout", "type": "Calls"},
        ]
        ordering = _resolver().filter_ordering_edges(edges)
        self.assertEqual(len(ordering), 1)
        self.assertEqual(ordering[0]["type"], "Calls")

    def test_refuted_edge_also_excluded_from_scope(self) -> None:
        """证伪的边也不能作为「归属」依据，否则会把无关资源拉进范围。"""
        nodes = _WORKLOAD_NODES + [
            {"name": "unrelated-bucket", "type": "S3Bucket"}
        ]
        edges = _EDGES + [
            {"from": "checkout", "to": "unrelated-bucket", "type": "WritesTo",
             "verify_status": "refuted"},
        ]
        scoped = _resolver().resolve(nodes, edges)
        kept = {n["name"] for n in scoped.nodes}
        self.assertNotIn("unrelated-bucket", kept)


class TestFromProfile(unittest.TestCase):
    def test_anchors_come_from_profile_services(self) -> None:
        from dr_profile import DRProfile

        fixture = os.path.join(
            os.path.dirname(__file__), "fixtures", "test_profile.yaml"
        )
        resolver = ScopeResolver.from_profile(DRProfile(fixture))
        self.assertIn("storefront", resolver.anchors)
        self.assertIn("checkout", resolver.anchors)

    def test_policy_supplies_edge_sets(self) -> None:
        from dr_profile import DRProfile
        from registry.policy_loader import PlanPolicy

        fixture = os.path.join(
            os.path.dirname(__file__), "fixtures", "test_profile.yaml"
        )
        resolver = ScopeResolver.from_profile(DRProfile(fixture), PlanPolicy())
        # 排序边必须严格窄于范围边——这是 M4 的核心区分
        self.assertTrue(resolver.ordering_edge_types)
        self.assertTrue(
            resolver.ordering_edge_types < resolver.scope_edge_types,
            "ordering_edge_types 必须是 scope_edge_types 的真子集",
        )
        self.assertNotIn("ForwardsTo", resolver.ordering_edge_types)
        self.assertIn("ForwardsTo", resolver.scope_edge_types)
        self.assertIn("refuted", resolver.excluded_verify_statuses)


class TestEmptyScopeFallback(unittest.TestCase):
    """锚点全不命中时的回退必须在产物里留痕。"""

    def test_empty_scope_records_a_gap(self) -> None:
        """回退后的计划**未经任何范围裁剪**，而它看起来是完整的。

        只记 WARNING 不够：运维不会知道自己拿到的是全图，平台设施与污染节点
        都还在里面。
        """
        import tempfile

        from graph.graph_analyzer import GraphAnalyzer
        from planner.plan_generator import PlanGenerator
        from planner.step_builder import StepBuilder

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(
                "profile:\n  name: mismatch\n"
                "kubernetes:\n  namespace: nope\n"
                "services:\n  not-in-graph:\n    tier: Tier0\n"
                "dr:\n  strategy: warm_standby\n"
            )
            path = fh.name
        try:
            set_active_profile(path)
            gen = PlanGenerator(GraphAnalyzer(), StepBuilder())
            plan = gen.generate_plan(
                scope="region", source="a", target="b",
                snapshot={
                    "nodes": [{"name": "unrelated", "type": "Microservice", "tier": "Tier0"}],
                    "edges": [],
                },
            )
            gaps = [
                g for g in plan.compute_layer_gaps
                if g.get("component") == "scope_anchoring"
            ]
            self.assertTrue(gaps, "空范围回退未记入计划级缺口")
            self.assertIn("未裁剪", gaps[0]["implication"])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
