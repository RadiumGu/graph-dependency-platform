"""
tests/test_snapshot.py — 图快照导出/加载与离线计划生成

重点不是「能跑通」，而是证明离线路径**确实不依赖 Neptune**：
- test_offline_path_does_not_import_neptune_client 断言导入面
- test_generate_plan_offline_never_calls_neptune 用会抛异常的替身守住调用面
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from graph.snapshot import (
    SNAPSHOT_VERSION,
    SnapshotError,
    build_snapshot,
    export_snapshot,
    load_snapshot,
    snapshot_age_seconds,
)

_NODES = [
    {"name": "petsite", "type": "Microservice", "tier": "Tier0"},
    {"name": "payforadoption", "type": "Microservice", "tier": "Tier1"},
    {"name": "petsite-db", "type": "RDSCluster", "tier": "Tier0"},
]
_EDGES = [
    {"from": "petsite", "to": "payforadoption", "type": "Calls"},
    {"from": "payforadoption", "to": "petsite-db", "type": "AccessesData"},
]


class _FakeAnalyzer:
    """Analyzer 替身：记录调用，永不联网。"""

    def __init__(self, nodes=None, edges=None):
        self.nodes = nodes if nodes is not None else _NODES
        self.edges = edges if edges is not None else _EDGES
        self.online_calls = 0

    def extract_affected_subgraph(self, scope, source):
        self.online_calls += 1
        return {"nodes": self.nodes, "edges": self.edges}


class _ExplodingAnalyzer:
    """任何联机抽取都直接失败——用来证明离线路径不会走到那里。"""

    def extract_affected_subgraph(self, scope, source):  # pragma: no cover
        raise AssertionError(
            "extract_affected_subgraph must never be called on the offline path"
        )

    def extract_affected_subgraph_from_data(self, nodes, edges):
        return {"nodes": nodes, "edges": edges}

    def classify_by_layer(self, subgraph):
        from graph.graph_analyzer import GraphAnalyzer

        return GraphAnalyzer().classify_by_layer(subgraph)

    def topological_sort_within_layer(self, nodes, edges):
        from graph.graph_analyzer import GraphAnalyzer

        return GraphAnalyzer().topological_sort_within_layer(nodes, edges)

    def detect_parallel_groups(self, sorted_nodes, edges):
        from graph.graph_analyzer import GraphAnalyzer

        return GraphAnalyzer().detect_parallel_groups(sorted_nodes, edges)


class TestBuildSnapshot(unittest.TestCase):
    def test_metadata_and_counts(self) -> None:
        snap = build_snapshot("region", "ap-northeast-1", _NODES, _EDGES, region="ap-northeast-1")
        self.assertEqual(snap["snapshot_version"], SNAPSHOT_VERSION)
        self.assertEqual(snap["scope"], "region")
        self.assertEqual(snap["source"], "ap-northeast-1")
        self.assertEqual(snap["node_count"], 3)
        self.assertEqual(snap["edge_count"], 2)
        self.assertTrue(snap["created_at"])

    def test_empty_graph_is_representable(self) -> None:
        snap = build_snapshot("az", "apne1-az1", [], [])
        self.assertEqual(snap["node_count"], 0)
        self.assertEqual(snap["edge_count"], 0)


class TestSnapshotAge(unittest.TestCase):
    def test_age_of_known_timestamp(self) -> None:
        now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
        snap = {"created_at": (now - timedelta(hours=3)).isoformat()}
        age = snapshot_age_seconds(snap, now=now)
        self.assertAlmostEqual(age, 3 * 3600, delta=1)

    def test_missing_created_at_returns_none(self) -> None:
        self.assertIsNone(snapshot_age_seconds({}))

    def test_unparseable_created_at_returns_none(self) -> None:
        self.assertIsNone(snapshot_age_seconds({"created_at": "not-a-date"}))

    def test_naive_timestamp_treated_as_utc(self) -> None:
        now = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)
        snap = {"created_at": "2026-09-05T11:00:00"}
        self.assertAlmostEqual(snapshot_age_seconds(snap, now=now), 3600, delta=1)


class TestExportLoadRoundtrip(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="dr-snap-")

    def test_roundtrip_preserves_graph(self) -> None:
        path = os.path.join(self.tmp, "nested", "snap.json")
        analyzer = _FakeAnalyzer()
        export_snapshot("region", "ap-northeast-1", path, analyzer=analyzer)
        self.assertEqual(analyzer.online_calls, 1)
        self.assertTrue(os.path.exists(path), "parent dirs must be created")

        loaded = load_snapshot(path)
        self.assertEqual(loaded["nodes"], _NODES)
        self.assertEqual(loaded["edges"], _EDGES)
        self.assertFalse(loaded["stale"])

    def test_missing_file_raises(self) -> None:
        with self.assertRaises(SnapshotError):
            load_snapshot(os.path.join(self.tmp, "nope.json"))

    def test_invalid_json_raises(self) -> None:
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        with self.assertRaises(SnapshotError):
            load_snapshot(path)

    def test_wrong_version_raises(self) -> None:
        path = os.path.join(self.tmp, "v99.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"snapshot_version": 99, "nodes": [], "edges": []}, fh)
        with self.assertRaises(SnapshotError):
            load_snapshot(path)

    def test_nodes_must_be_a_list(self) -> None:
        path = os.path.join(self.tmp, "badnodes.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"snapshot_version": SNAPSHOT_VERSION, "nodes": {}, "edges": []}, fh)
        with self.assertRaises(SnapshotError):
            load_snapshot(path)

    def test_stale_snapshot_warns_but_loads(self) -> None:
        """陈旧快照必须仍能加载：真灾时拒绝生成计划等于工具失效。"""
        path = os.path.join(self.tmp, "old.json")
        old = build_snapshot("region", "ap-northeast-1", _NODES, _EDGES)
        old["created_at"] = (
            datetime.now(timezone.utc) - timedelta(days=3)
        ).isoformat()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(old, fh)

        loaded = load_snapshot(path, max_age_seconds=3600)
        self.assertTrue(loaded["stale"])
        self.assertEqual(loaded["nodes"], _NODES)


class TestOfflinePlanGeneration(unittest.TestCase):
    def setUp(self) -> None:
        from planner.plan_generator import PlanGenerator
        from planner.step_builder import StepBuilder

        self.generator = PlanGenerator(_ExplodingAnalyzer(), StepBuilder())
        self.snapshot = build_snapshot("region", "ap-northeast-1", _NODES, _EDGES)

    def test_generate_plan_offline_never_calls_neptune(self) -> None:
        plan = self.generator.generate_plan(
            scope="region",
            source="ap-northeast-1",
            target="us-west-2",
            snapshot=self.snapshot,
        )
        self.assertEqual(plan.plan_source, "snapshot")
        self.assertTrue(plan.phases)

    def test_plan_records_capture_time_not_render_time(self) -> None:
        """审计需要知道计划依据的图数据是何时抓的，而非何时渲染的。"""
        captured = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        snap = dict(self.snapshot)
        snap["created_at"] = captured
        snap["age_seconds"] = 5 * 3600
        snap["stale"] = True

        plan = self.generator.generate_plan(
            scope="region", source="ap-northeast-1", target="us-west-2", snapshot=snap
        )
        self.assertEqual(plan.graph_snapshot_time, captured)
        self.assertNotEqual(plan.graph_snapshot_time, plan.created_at)
        self.assertTrue(plan.graph_snapshot_stale)
        self.assertAlmostEqual(plan.graph_snapshot_age_seconds, 5 * 3600, delta=1)

    def test_mode_defaults_to_drill_and_is_recorded(self) -> None:
        plan = self.generator.generate_plan(
            scope="region", source="ap-northeast-1", target="us-west-2",
            snapshot=self.snapshot,
        )
        self.assertEqual(plan.mode, "drill")

        plan_fo = self.generator.generate_plan(
            scope="region", source="ap-northeast-1", target="us-west-2",
            snapshot=self.snapshot, mode="failover",
        )
        self.assertEqual(plan_fo.mode, "failover")

    def test_spof_detection_offline_does_not_query_neptune(self) -> None:
        """离线时 SPOF 必须直接走本地分析，不能靠 except 兜底。

        真灾时目标 Region 不可达 ≠ 快速失败：请求会阻塞到超时，
        把 RTO 悄悄拉长。所以必须显式跳过，而不是依赖异常路径。
        """
        from assessment.spof_detector import SPOFDetector

        called = []

        class _Boom:
            @staticmethod
            def q16_single_point_of_failure():  # pragma: no cover
                called.append(1)
                raise AssertionError("Neptune Q16 must not be queried when offline")

        import graph.queries as real_queries

        original = real_queries.q16_single_point_of_failure
        real_queries.q16_single_point_of_failure = _Boom.q16_single_point_of_failure
        try:
            subgraph = {"nodes": _NODES, "edges": _EDGES}
            SPOFDetector().detect(subgraph, offline=True)
        finally:
            real_queries.q16_single_point_of_failure = original

        self.assertEqual(called, [], "offline SPOF detection reached Neptune")

    def test_plan_survives_json_roundtrip(self) -> None:
        from models import DRPlan
        from output.json_renderer import JSONRenderer

        plan = self.generator.generate_plan(
            scope="region", source="ap-northeast-1", target="us-west-2",
            snapshot=self.snapshot, mode="failover",
        )
        revived = DRPlan.from_dict(json.loads(JSONRenderer().render(plan)))
        self.assertEqual(revived.mode, "failover")
        self.assertEqual(revived.plan_source, "snapshot")


class TestOfflineImportSurface(unittest.TestCase):
    """离线路径不得导入 Neptune 客户端。

    在**子进程**里检查：本测试文件其它用例已经间接导入过 neptune_client，
    同进程内 sys.modules 会被污染，断言就没有意义了。
    """

    def test_offline_path_does_not_import_neptune_client(self) -> None:
        pkg_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        repo_root = os.path.abspath(os.path.join(pkg_root, ".."))
        code = (
            "import sys\n"
            "from graph.snapshot import load_snapshot, build_snapshot\n"
            "from graph.graph_analyzer import GraphAnalyzer\n"
            "from planner.plan_generator import PlanGenerator\n"
            "leaked = [m for m in sys.modules "
            "if 'neptune_client' in m or m == 'graph.queries']\n"
            "print('LEAKED=' + ','.join(sorted(leaked)))\n"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([pkg_root, repo_root])
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, cwd=pkg_root, env=env,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("LEAKED=", result.stdout)
        leaked = result.stdout.strip().split("LEAKED=", 1)[1]
        self.assertEqual(
            leaked, "",
            msg=f"offline path pulled in Neptune modules: {leaked}",
        )


if __name__ == "__main__":
    unittest.main()
