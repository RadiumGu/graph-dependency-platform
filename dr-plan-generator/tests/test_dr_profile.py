"""
tests/test_dr_profile.py — 自带 profile 读取器与解耦守卫

重点：
- 无默认 profile（不传就抛错，而不是静默用某个具体 workload）
- ``${ENV_VAR}`` 展开，且未设置时交出 default 而非占位符原文
- 换一份 profile 能真正改变生成的命令（验收标准 8）
- 仍能读父仓库的 profiles/petsite.yaml（格式兼容）
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dr_profile import (
    DRProfile,
    ProfileError,
    ProfileNotConfigured,
    get_active_profile,
    set_active_profile,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "test_profile.yaml")
#: 父仓库的真实 profile，用于验证格式兼容性（可能不存在，届时跳过）。
PARENT_PROFILE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "profiles", "petsite.yaml")
)


class TestNoDefaultProfile(unittest.TestCase):
    """没有默认 profile —— 这是刻意的行为，不是遗漏。"""

    def test_unset_profile_raises(self) -> None:
        set_active_profile(None)
        old = os.environ.pop("DR_PROFILE", None)
        try:
            with self.assertRaises(ProfileNotConfigured):
                get_active_profile()
        finally:
            if old is not None:
                os.environ["DR_PROFILE"] = old

    def test_env_var_is_honoured(self) -> None:
        set_active_profile(None)
        os.environ["DR_PROFILE"] = FIXTURE
        try:
            self.assertEqual(get_active_profile().name, "acme-shop")
        finally:
            os.environ.pop("DR_PROFILE", None)
            set_active_profile(None)

    def test_empty_path_raises(self) -> None:
        with self.assertRaises(ProfileNotConfigured):
            DRProfile("")

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(ProfileError):
            DRProfile("/nonexistent/nope.yaml")

    def test_non_mapping_root_raises(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
            fh.write("- just\n- a\n- list\n")
            path = fh.name
        try:
            with self.assertRaises(ProfileError):
                DRProfile(path)
        finally:
            os.unlink(path)


class TestProfileProperties(unittest.TestCase):
    def setUp(self) -> None:
        self.p = DRProfile(FIXTURE)

    def test_basic_properties(self) -> None:
        self.assertEqual(self.p.name, "acme-shop")
        self.assertEqual(self.p.region, "eu-west-1")
        self.assertEqual(self.p.domain, "shop.acme.test")
        self.assertEqual(self.p.health_endpoint, "/healthz")
        self.assertEqual(self.p.k8s_namespace, "acme")
        self.assertEqual(self.p.dns_ttl_normal, 300)
        self.assertEqual(self.p.dns_ttl_pre_switchover, 60)

    def test_alarm_prefix_prefers_monitoring_section(self) -> None:
        """两级回退：monitoring.cloudwatch_alarm_prefix 优先于 application.alarm_prefix。"""
        self.assertEqual(self.p.alarm_prefix, "acme-shop")

    def test_health_check_command_is_rendered(self) -> None:
        cmd = self.p.health_check_command
        self.assertIn("shop.acme.test/healthz", cmd)
        self.assertNotIn("{domain}", cmd)

    def test_ssm_key_from_profile(self) -> None:
        self.assertEqual(self.p.ssm_dynamodb_region_key, "/acme-shop/dynamodb-region")

    def test_deployment_name_translation(self) -> None:
        self.assertEqual(self.p.get_deployment_name("storefront"), "storefront-deployment")
        # deployment_map 缺失时回落到 services.<name>.k8s_deployment
        self.assertEqual(self.p.get_deployment_name("checkout"), "checkout-svc")
        # 完全没有映射时原样返回，而不是猜
        self.assertEqual(self.p.get_deployment_name("unknown-svc"), "unknown-svc")

    def test_missing_key_returns_default(self) -> None:
        self.assertEqual(self.p.get("nope.not.here", "fallback"), "fallback")


class TestPlaceholderExpansion(unittest.TestCase):
    def setUp(self) -> None:
        self.p = DRProfile(FIXTURE)
        os.environ.pop("ACME_ZONE_ID", None)

    def tearDown(self) -> None:
        os.environ.pop("ACME_ZONE_ID", None)

    def test_env_var_expanded_when_set(self) -> None:
        os.environ["ACME_ZONE_ID"] = "Z0123456789"
        self.assertEqual(self.p.dns_hosted_zone_id, "Z0123456789")

    def test_unset_placeholder_never_leaks_literal(self) -> None:
        """未设置时必须返回空串，不能把 ``${ACME_ZONE_ID}`` 原文交出去。

        原文会被当成真的 zone id 拿去调 Route 53，错误信息离根因很远。
        """
        value = self.p.dns_hosted_zone_id
        self.assertEqual(value, "")
        self.assertNotIn("${", value)


class TestProfileSwapChangesOutput(unittest.TestCase):
    """验收标准 8：换 profile 能真正改变产出，而不只是改变一个字段。"""

    def test_alarm_prefix_flows_into_generated_command(self) -> None:
        from graph.graph_analyzer import GraphAnalyzer
        from planner.plan_generator import PlanGenerator
        from planner.step_builder import StepBuilder

        nodes = [{"name": "storefront", "type": "Microservice", "tier": "Tier0"}]
        snapshot = {"nodes": nodes, "edges": []}

        class _Local(GraphAnalyzer):
            def extract_affected_subgraph(self, scope, source):  # pragma: no cover
                raise AssertionError("offline test must not query Neptune")

        gen = PlanGenerator(_Local(), StepBuilder())

        set_active_profile(FIXTURE)
        plan = gen.generate_plan(
            scope="region", source="eu-west-1", target="eu-central-1", snapshot=snapshot
        )
        commands = "\n".join(
            s.command + s.validation for ph in plan.phases for s in ph.steps
        )
        self.assertIn("--alarm-name-prefix acme-shop", commands)
        self.assertNotIn("--alarm-name-prefix petsite", commands)


class TestParentProfileCompatibility(unittest.TestCase):
    """自带读取器必须仍能消费父仓库那份 profile —— 它是数据，不是代码。"""

    def test_reads_parent_repo_profile(self) -> None:
        if not os.path.exists(PARENT_PROFILE):
            self.skipTest(f"parent profile not present: {PARENT_PROFILE}")
        p = DRProfile(PARENT_PROFILE)
        self.assertTrue(p.name)
        self.assertTrue(p.k8s_namespace)
        self.assertTrue(p.ssm_dynamodb_region_key.startswith("/"))
        # 该 profile 的 zone id 是占位符；未设环境变量时不得泄漏原文
        os.environ.pop("ROUTE53_ZONE_ID", None)
        self.assertNotIn("${", p.dns_hosted_zone_id)


class TestDecouplingGuard(unittest.TestCase):
    """守卫脚本本身要能跑，且当前代码库必须通过。"""

    def test_repo_passes_decoupling_check(self) -> None:
        sys.path.insert(
            0, os.path.join(os.path.dirname(__file__), "..", "scripts")
        )
        import check_decoupling

        count, messages = check_decoupling.check()
        self.assertEqual(count, 0, msg="\n".join(messages))

    def test_guard_detects_a_planted_literal(self) -> None:
        """反向验证：守卫必须真的能抓到运行时字符串里的 workload 名。"""
        sys.path.insert(
            0, os.path.join(os.path.dirname(__file__), "..", "scripts")
        )
        import check_decoupling

        src = 'x = "petsite-alarms"\n'
        hits = [
            v for _, v in check_decoupling._runtime_strings(src)
            if "petsite" in v.lower()
        ]
        self.assertEqual(hits, ["petsite-alarms"])

    def test_guard_ignores_docstrings_and_comments(self) -> None:
        """注释与 docstring 里出现 workload 名是合理的，不应误报。"""
        sys.path.insert(
            0, os.path.join(os.path.dirname(__file__), "..", "scripts")
        )
        import check_decoupling

        src = (
            '"""Module doc mentioning petsite as an example."""\n'
            "# petsite appears in a comment\n"
            "def f():\n"
            '    """petsite in a function docstring."""\n'
            "    return 1\n"
        )
        hits = [
            v for _, v in check_decoupling._runtime_strings(src)
            if "petsite" in v.lower()
        ]
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
