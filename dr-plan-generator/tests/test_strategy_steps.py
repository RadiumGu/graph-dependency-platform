"""
tests/test_strategy_steps.py — pilot light 与 warm standby 的步骤差异

核心断言（验收标准 2）：两档策略的 phase-2 步骤数必须不同——pilot light
多出「扩节点组」与「等节点 Ready」。若两者一样，说明策略参数没生效，
而那种计划在 pilot light 环境下执行会让 Pod 永久 Pending。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dr_profile import set_active_profile
from graph.graph_analyzer import GraphAnalyzer
from models import DRStep
from planner.plan_generator import PlanGenerator
from planner.step_builder import (
    NODE_READY_TIMEOUT_SECONDS,
    STRATEGY_PILOT_LIGHT,
    STRATEGY_WARM_STANDBY,
    StepBuilder,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "test_profile.yaml")

_NODES = [
    {"name": "storefront", "type": "Microservice", "tier": "Tier0"},
    {"name": "checkout", "type": "Microservice", "tier": "Tier1"},
]
_EDGES = [{"from": "storefront", "to": "checkout", "type": "Calls"}]
_SNAPSHOT = {"nodes": _NODES, "edges": _EDGES}


class _OfflineAnalyzer(GraphAnalyzer):
    def extract_affected_subgraph(self, scope, source):  # pragma: no cover
        raise AssertionError("offline test must not query Neptune")


def _plan(strategy: str):
    gen = PlanGenerator(_OfflineAnalyzer(), StepBuilder(strategy=strategy))
    return gen.generate_plan(
        scope="region", source="eu-west-1", target="eu-central-1", snapshot=_SNAPSHOT
    )


def _phase(plan, phase_id: str):
    for ph in plan.phases:
        if ph.phase_id == phase_id:
            return ph
    raise AssertionError(f"phase {phase_id} not found")


class TestNodegroupSteps(unittest.TestCase):
    def setUp(self) -> None:
        set_active_profile(FIXTURE)

    def test_pilot_light_generates_nodegroup_steps(self) -> None:
        steps = StepBuilder(strategy=STRATEGY_PILOT_LIGHT).build_nodegroup_steps("eu-central-1")
        actions = [s.action for s in steps]
        self.assertEqual(actions, ["scale_nodegroup_up", "wait_nodes_ready"])

    def test_warm_standby_generates_none(self) -> None:
        """warm standby 的节点容量常态就在——AWS 定义里 everything is already running。"""
        steps = StepBuilder(strategy=STRATEGY_WARM_STANDBY).build_nodegroup_steps("eu-central-1")
        self.assertEqual(steps, [])

    def test_scale_step_uses_profile_cluster_and_sizes(self) -> None:
        step = StepBuilder(strategy=STRATEGY_PILOT_LIGHT).build_nodegroup_steps("eu-central-1")[0]
        self.assertIn("--cluster-name acme-dr", step.command)
        self.assertIn("--nodegroup-name acme-arm64", step.command)
        self.assertIn("desiredSize=2", step.command)
        self.assertIn("--region eu-central-1", step.command)
        self.assertEqual(step.expected_result, "2")

    def test_scale_step_rollback_returns_to_zero(self) -> None:
        step = StepBuilder(strategy=STRATEGY_PILOT_LIGHT).build_nodegroup_steps("eu-central-1")[0]
        self.assertIn("desiredSize=0", step.rollback_command)

    def test_wait_step_blocks_on_node_readiness(self) -> None:
        """必须真的阻塞等待，而不是查一下就过。"""
        wait = StepBuilder(strategy=STRATEGY_PILOT_LIGHT).build_nodegroup_steps("eu-central-1")[1]
        self.assertIn("kubectl wait --for=condition=Ready node", wait.command)
        self.assertIn(f"--timeout={NODE_READY_TIMEOUT_SECONDS}s", wait.command)
        self.assertIn("eks.amazonaws.com/nodegroup=acme-arm64", wait.command)

    def test_wait_step_has_no_namespace_flag(self) -> None:
        """节点是集群级资源：带 -n <ns> 会让 kubectl wait 失败。"""
        wait = StepBuilder(strategy=STRATEGY_PILOT_LIGHT).build_nodegroup_steps("eu-central-1")[1]
        self.assertNotIn(" -n ", wait.command)
        self.assertNotIn(" -n ", wait.validation)

    def test_wait_depends_on_scale(self) -> None:
        steps = StepBuilder(strategy=STRATEGY_PILOT_LIGHT).build_nodegroup_steps("eu-central-1")
        self.assertIn(steps[0].step_id, steps[1].dependencies)


class TestMicroserviceStep(unittest.TestCase):
    def setUp(self) -> None:
        set_active_profile(FIXTURE)
        self.builder = StepBuilder(strategy=STRATEGY_WARM_STANDBY)

    def _step(self, name: str, tier: str = "Tier0") -> DRStep:
        return self.builder.build_step(
            {"name": name, "type": "Microservice", "tier": tier},
            "eu-west-1", "eu-central-1",
        )

    def test_deployment_name_is_translated_via_profile(self) -> None:
        """图谱名 ≠ Deployment 名。直接用图谱名会 scale 一个不存在的 Deployment。"""
        step = self._step("storefront")
        self.assertIn("deployment/storefront-deployment", step.command)
        self.assertNotIn("deployment/storefront ", step.command)

    def test_namespace_is_applied(self) -> None:
        step = self._step("storefront")
        self.assertIn("-n acme", step.command)

    def test_context_is_not_string_concatenated_from_region(self) -> None:
        """原实现拼 ``--context {target}-cluster``，那个 context 几乎必然不存在，
        而 kubectl 遇到不存在的 context 直接报错退出，恢复链就断在这里。"""
        step = self._step("storefront")
        self.assertNotIn("eu-central-1-cluster", step.command)

    def test_replicas_come_from_tier_config(self) -> None:
        self.assertIn("--replicas=3", self._step("storefront", "Tier0").command)
        self.assertIn("--replicas=2", self._step("checkout", "Tier1").command)
        self.assertIn("--replicas=1", self._step("checkout", "Tier2").command)

    def test_rollback_scales_to_zero(self) -> None:
        self.assertIn("--replicas=0", self._step("storefront").rollback_command)

    def test_pilot_light_allows_longer_rollout_for_image_pull(self) -> None:
        """pilot light 的目标区节点是全新的，本地无镜像缓存，首拉更久。"""
        pl = StepBuilder(strategy=STRATEGY_PILOT_LIGHT).build_step(
            {"name": "storefront", "type": "Microservice", "tier": "Tier0"},
            "eu-west-1", "eu-central-1",
        )
        ws = self._step("storefront")
        self.assertGreater(pl.estimated_time, ws.estimated_time)
        self.assertIn("--timeout=300s", pl.command)


class TestPhaseTwoDiffersByStrategy(unittest.TestCase):
    """验收标准 2：两档策略的 phase-2 必须不同。"""

    def setUp(self) -> None:
        set_active_profile(FIXTURE)

    def test_step_counts_differ(self) -> None:
        pl = _phase(_plan(STRATEGY_PILOT_LIGHT), "phase-2")
        ws = _phase(_plan(STRATEGY_WARM_STANDBY), "phase-2")
        self.assertGreater(
            len(pl.steps), len(ws.steps),
            "pilot light 必须多出节点组扩容与等待步骤；相同意味着策略未生效",
        )
        self.assertEqual(len(pl.steps) - len(ws.steps), 2)

    def test_node_capacity_comes_before_deployment_scaling(self) -> None:
        """顺序不可颠倒：节点未 Ready 就 scale Deployment → Pod 永久 Pending。"""
        steps = _phase(_plan(STRATEGY_PILOT_LIGHT), "phase-2").steps
        actions = [s.action for s in steps]
        self.assertEqual(actions[0], "scale_nodegroup_up")
        self.assertEqual(actions[1], "wait_nodes_ready")
        self.assertIn("scale_up_and_verify", actions[2:])

    def test_gate_condition_mentions_pending_pods(self) -> None:
        pl = _phase(_plan(STRATEGY_PILOT_LIGHT), "phase-2")
        self.assertIn("Pending", pl.gate_condition)

    def test_warm_standby_gate_is_plain(self) -> None:
        ws = _phase(_plan(STRATEGY_WARM_STANDBY), "phase-2")
        self.assertNotIn("Pending", ws.gate_condition)

    def test_plan_records_effective_strategy(self) -> None:
        """CLI 可覆盖 profile，计划必须记录**实际生效**的那个。

        fixture 的 dr.strategy 是 warm_standby；用 pilot_light 构造 StepBuilder
        后，计划里应记 pilot_light。
        """
        plan = _plan(STRATEGY_PILOT_LIGHT)
        self.assertEqual(plan.strategy, STRATEGY_PILOT_LIGHT)

    def test_phase_name_carries_strategy(self) -> None:
        pl = _phase(_plan(STRATEGY_PILOT_LIGHT), "phase-2")
        self.assertIn("pilot_light", pl.name)

    def test_dependency_order_preserved_within_phase(self) -> None:
        """storefront 调 checkout，故 checkout 必须排在 storefront 之前。"""
        steps = _phase(_plan(STRATEGY_WARM_STANDBY), "phase-2").steps
        names = [s.resource_name for s in steps]
        self.assertLess(names.index("checkout"), names.index("storefront"))


class TestMissingNodegroupConfig(unittest.TestCase):
    def test_pilot_light_without_nodegroups_warns_and_yields_none(self) -> None:
        """配置缺失时不静默生成半套步骤——留白比错误的完整感安全。"""
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(
                "profile:\n  name: bare\n"
                "kubernetes:\n  namespace: bare\n"
                "dr:\n  strategy: pilot_light\n"
                "  eks:\n    target_cluster: bare-dr\n    nodegroups: []\n"
            )
            path = fh.name
        try:
            set_active_profile(path)
            steps = StepBuilder(strategy=STRATEGY_PILOT_LIGHT).build_nodegroup_steps("eu-central-1")
            self.assertEqual(steps, [])
        finally:
            os.unlink(path)

    def test_missing_nodegroups_becomes_a_plan_level_gap(self) -> None:
        """只记日志不够——运维拿到的仍是一份看起来完整的计划。

        缺口必须进产物，否则「命令返回成功、Pod 永久 Pending」这个失败模式
        在执行前完全不可见。
        """
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write(
                "profile:\n  name: bare\n"
                "kubernetes:\n  namespace: bare\n"
                "services:\n  storefront:\n    tier: Tier0\n"
                "dr:\n  strategy: pilot_light\n"
                "  eks:\n    target_cluster: bare-dr\n    nodegroups: []\n"
            )
            path = fh.name
        try:
            set_active_profile(path)
            plan = _plan(STRATEGY_PILOT_LIGHT)
            self.assertTrue(plan.compute_layer_gaps, "缺节点组配置却未产生计划级缺口")
            gap = plan.compute_layer_gaps[0]
            self.assertEqual(gap["component"], "eks_nodegroups")
            self.assertIn("Pending", gap["implication"])
        finally:
            os.unlink(path)

    def test_configured_nodegroups_produce_no_gap(self) -> None:
        """断言精确到**节点组**缺口。

        `compute_layer_gaps` 是个汇总列表，M7 起还会收 phase-0 就绪检查的缺口，
        所以不能断言它整体为空——那样只要新增任何一类检查，这个用例就会误红。
        """
        set_active_profile(FIXTURE)
        plan = _plan(STRATEGY_PILOT_LIGHT)
        nodegroup_gaps = [
            g for g in plan.compute_layer_gaps if g.get("component") == "eks_nodegroups"
        ]
        self.assertEqual(nodegroup_gaps, [])


if __name__ == "__main__":
    unittest.main()
