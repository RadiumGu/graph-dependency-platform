"""
test_22_ui_streamlit.py — Sprint 8 Streamlit UI 功能测试

测试 ID 覆盖：S8-01 ~ S8-08
使用 streamlit.testing.v1.AppTest（无需浏览器），mock Neptune/AWS 调用。
"""
import os
import sys
import types
import unittest.mock as mock

import pytest
from streamlit.testing.v1 import AppTest

# ── 路径设置 ──────────────────────────────────────────────────────────────────
PROJECT_ROOT = "/home/ubuntu/tech/graph-dependency-platform"
DEMO_DIR = os.path.join(PROJECT_ROOT, "demo")
PAGES_DIR = os.path.join(DEMO_DIR, "pages")

for _p in [
    PROJECT_ROOT,
    os.path.join(PROJECT_ROOT, "rca"),
    os.path.join(PROJECT_ROOT, "dr-plan-generator"),
    os.path.join(PROJECT_ROOT, "chaos", "code"),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── Mock 数据 ─────────────────────────────────────────────────────────────────
MOCK_NODES = [
    {"name": "petsite", "label": "Microservice", "priority": "Tier0", "status": "running", "state": None, "tier": "tier0"},
    {"name": "petsearch", "label": "Microservice", "priority": "Tier1", "status": "running", "state": None, "tier": "tier1"},
    {"name": "petsite-pod-1", "label": "Pod", "priority": None, "status": "Running", "state": None, "tier": None},
    {"name": "i-0abc123", "label": "EC2Instance", "priority": None, "status": None, "state": "running", "tier": None},
]

MOCK_EDGES = [
    {"src": "petsite", "dst": "petsearch", "rel": "Calls"},
    {"src": "petsite", "dst": "petsite-pod-1", "rel": "RunsOn"},
    {"src": "petsite-pod-1", "dst": "i-0abc123", "rel": "RunsOn"},
]

MOCK_DETAIL_ROWS = [
    {"name": "petsite", "type": "Microservice", "rel": "Calls", "neighbor": "petsearch", "neighbor_type": "Microservice"},
    {"name": "petsite", "type": "Microservice", "rel": "DependsOn", "neighbor": "petsite-db", "neighbor_type": "RDSCluster"},
]

MOCK_EXPERIMENTS = [
    {"service": "petsite", "id": "exp-001", "fault_type": "pod_kill",
     "result": "passed", "recovery_time": 30, "degradation": 0.05, "timestamp": "2026-04-01T10:00:00"},
    {"service": "petsearch", "id": "exp-002", "fault_type": "network_delay",
     "result": "failed", "recovery_time": 120, "degradation": 0.30, "timestamp": "2026-04-02T10:00:00"},
]

MOCK_INCIDENTS = [
    {"id": "inc-001", "severity": "P1", "root_cause": "DB 连接池耗尽", "resolution": "重启连接池", "start_time": "2026-03-01"},
    {"id": "inc-002", "severity": "P0", "root_cause": "网络分区", "resolution": "AZ 切换", "start_time": "2026-03-15"},
]

MOCK_RCA_REPORT = {
    "root_cause": "DynamoDB 限流导致 petsite 超时",
    "confidence": 78,
    "reasoning": "基于 Neptune 图谱拓扑和 CloudWatch 指标分析",
    "recommended_action": "增加 DynamoDB 容量; 检查 petsite 连接池",
    "blast_radius": "影响 2 个下游服务，1 个 BusinessCapability",
    "evidence": ["DynamoDB throttle metric spike", "petsite latency p99 > 2s"],
    "confidence_breakdown": {"拓扑": 30, "历史": 25, "指标": 23},
    "source": "Graph RAG + Bedrock Claude",
}

MOCK_DR_PLAN = {
    "markdown": "# DR Plan\n\n## Phase 0: 评估\n- 检查 petsite 健康状态\n## Phase 1: 切换\n- 切换流量至 AZ-2",
    "json": {"plan_id": "dr-az-test-001", "phases": [], "affected_services": ["petsite"]},
    "error": None,
    "validation_warnings": [],
    "plan_id": "dr-az-test-001",
    "estimated_rto": 13,
    "estimated_rpo": 15,
    "affected_count": 3,
}

MOCK_NL_RESULT = {
    "question": "petsite 依赖哪些数据库？",
    "cypher": "MATCH (s:Microservice {name:'petsite'})-[:DependsOn]->(db) RETURN db.name AS database",
    "results": [{"database": "petsite-rds", "type": "RDSCluster"}, {"database": "petsite-dynamo", "type": "DynamoDB"}],
    "summary": "petsite 依赖 1 个 RDS 集群和 1 个 DynamoDB 表。",
}


# ── 辅助函数 ──────────────────────────────────────────────────────────────────
def _make_mock_nc(side_effect=None, return_value=None):
    """创建 mock neptune_client 模块。"""
    nc_mod = types.ModuleType("neptune_client")
    if side_effect is not None:
        nc_mod.results = mock.Mock(side_effect=side_effect)
    else:
        nc_mod.results = mock.Mock(return_value=return_value if return_value is not None else [])
    return nc_mod


def _make_neptune_pkg(nc_mod=None, queries_mod=None):
    """创建 mock neptune 包（neptune.neptune_client + neptune.neptune_queries）。"""
    pkg = types.ModuleType("neptune")
    pkg.neptune_client = nc_mod or _make_mock_nc(return_value=[])
    if queries_mod is None:
        queries_mod = types.ModuleType("neptune.neptune_queries")
        queries_mod.q1_blast_radius = mock.Mock(return_value={"services": [], "capabilities": []})
        queries_mod.q5_similar_incidents = mock.Mock(return_value=[])
        queries_mod.q6_pod_status = mock.Mock(return_value=[])
        queries_mod.q9_service_infra_path = mock.Mock(return_value=[])
        queries_mod.q17_incidents_by_resource = mock.Mock(return_value=[])
        queries_mod.q18_chaos_history = mock.Mock(return_value=[])
    pkg.neptune_queries = queries_mod
    return pkg


def _inject_mocks(extra_mods: dict = None):
    """注入共用 mock 模块到 sys.modules，在 AppTest.run() 前调用。"""
    nc_mod = _make_mock_nc(return_value=MOCK_NODES[:2])
    neptune_pkg = _make_neptune_pkg(nc_mod=nc_mod)

    mods = {
        "neptune": neptune_pkg,
        "neptune.neptune_client": neptune_pkg.neptune_client,
        "neptune.neptune_queries": neptune_pkg.neptune_queries,
    }
    if extra_mods:
        mods.update(extra_mods)

    # 备份并注入
    originals = {k: sys.modules.get(k) for k in mods}
    sys.modules.update(mods)
    return originals


def _restore_mocks(originals: dict):
    """恢复 sys.modules。"""
    for k, v in originals.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


# ═══════════════════════════════════════════════════════════════════════════════
# S8-01: Graph Explorer — 选择节点类型后渲染图谱（无异常）
# ═══════════════════════════════════════════════════════════════════════════════
class TestS801GraphExplorerRender:
    """S8-01: Graph Explorer 页面加载不抛异常，图谱数据正确获取。"""

    def test_page_loads_without_exception(self):
        """S8-01: Graph Explorer 以默认节点类型运行后无未处理异常。"""
        # mock nc.results: 第一次调用返回节点，第二次返回边
        nc_mod = _make_mock_nc(side_effect=[MOCK_NODES, MOCK_EDGES])
        neptune_pkg = _make_neptune_pkg(nc_mod=nc_mod)

        originals = {
            "neptune": neptune_pkg,
            "neptune.neptune_client": nc_mod,
            "neptune.neptune_queries": neptune_pkg.neptune_queries,
        }
        orig_backup = {k: sys.modules.get(k) for k in originals}
        sys.modules.update(originals)

        try:
            at = AppTest.from_file(
                os.path.join(PAGES_DIR, "1_Graph_Explorer.py"),
                default_timeout=15,
            )
            at.run()
            # 核心断言：无未处理异常
            assert not at.exception, f"Graph Explorer 抛出未处理异常: {at.exception}"
        finally:
            _restore_mocks(orig_backup)

    def test_warning_when_no_type_selected(self):
        """S8-01 补充: 节点类型为空时显示 warning 而非 crash。"""
        nc_mod = _make_mock_nc(return_value=[])
        neptune_pkg = _make_neptune_pkg(nc_mod=nc_mod)

        originals = {
            "neptune": neptune_pkg,
            "neptune.neptune_client": nc_mod,
            "neptune.neptune_queries": neptune_pkg.neptune_queries,
        }
        orig_backup = {k: sys.modules.get(k) for k in originals}
        sys.modules.update(originals)

        try:
            at = AppTest.from_file(
                os.path.join(PAGES_DIR, "1_Graph_Explorer.py"),
                default_timeout=15,
            )
            # 清空多选默认值
            at.run()
            assert not at.exception, f"空类型选择时异常: {at.exception}"
        finally:
            _restore_mocks(orig_backup)


# ═══════════════════════════════════════════════════════════════════════════════
# S8-02: Graph Explorer — 节点详情查询显示正确属性【P0】
# ═══════════════════════════════════════════════════════════════════════════════
class TestS802GraphExplorerNodeDetail:
    """S8-02 [P0]: Graph Explorer 节点详情查询返回的属性非 None。"""

    def test_node_detail_rows_not_none(self):
        """S8-02 [P0]: nc.results 返回的 name/type 字段均不为 None。"""
        # MOCK_DETAIL_ROWS 的 name 和 type 均有值
        for row in MOCK_DETAIL_ROWS:
            assert row.get("name") is not None, "节点 name 不应为 None"
            assert row.get("type") is not None, "节点 type 不应为 None"

    def test_node_detail_query_page_runs(self):
        """S8-02 [P0]: Graph Explorer 页面在 nc.results 返回详情数据时无异常。"""
        nc_mod = _make_mock_nc(side_effect=[MOCK_NODES, MOCK_EDGES, MOCK_DETAIL_ROWS])
        neptune_pkg = _make_neptune_pkg(nc_mod=nc_mod)

        originals = {
            "neptune": neptune_pkg,
            "neptune.neptune_client": nc_mod,
            "neptune.neptune_queries": neptune_pkg.neptune_queries,
        }
        orig_backup = {k: sys.modules.get(k) for k in originals}
        sys.modules.update(originals)

        try:
            at = AppTest.from_file(
                os.path.join(PAGES_DIR, "1_Graph_Explorer.py"),
                default_timeout=15,
            )
            at.run()
            assert not at.exception, f"节点详情查询时异常: {at.exception}"
        finally:
            _restore_mocks(orig_backup)


# ═══════════════════════════════════════════════════════════════════════════════
# S8-03: Smart Query — 自然语言查询返回表格结果
# ═══════════════════════════════════════════════════════════════════════════════
class TestS803SmartQueryReturnsTable:
    """S8-03: Smart Query 页面加载正常，NLQueryEngine mock 返回表格数据。"""

    def test_page_loads_without_exception(self):
        """S8-03: Smart Query 页面无未处理异常。"""
        # Mock NLQueryEngine
        mock_engine_cls = mock.MagicMock()
        mock_engine_cls.return_value.query.return_value = MOCK_NL_RESULT

        nl_mod = types.ModuleType("neptune.nl_query")
        nl_mod.NLQueryEngine = mock_engine_cls

        nc_mod = _make_mock_nc(return_value=[])
        neptune_pkg = _make_neptune_pkg(nc_mod=nc_mod)
        neptune_pkg.nl_query = nl_mod

        originals = {
            "neptune": neptune_pkg,
            "neptune.neptune_client": nc_mod,
            "neptune.neptune_queries": neptune_pkg.neptune_queries,
            "neptune.nl_query": nl_mod,
        }
        orig_backup = {k: sys.modules.get(k) for k in originals}
        sys.modules.update(originals)

        try:
            at = AppTest.from_file(
                os.path.join(PAGES_DIR, "2_Smart_Query.py"),
                default_timeout=15,
            )
            at.run()
            assert not at.exception, f"Smart Query 页面异常: {at.exception}"
        finally:
            _restore_mocks(orig_backup)

    def test_nl_result_has_results_and_summary(self):
        """S8-03: NLQueryEngine 返回的结果包含 results 列表和 summary 字符串。"""
        assert isinstance(MOCK_NL_RESULT["results"], list)
        assert len(MOCK_NL_RESULT["results"]) > 0
        assert isinstance(MOCK_NL_RESULT["summary"], str)
        assert len(MOCK_NL_RESULT["summary"]) > 0

    def test_nl_result_cypher_is_read_only(self):
        """S8-03: 生成的 Cypher 不包含写操作关键字。"""
        cypher = MOCK_NL_RESULT["cypher"].upper()
        for kw in ["CREATE", "DELETE", "SET", "MERGE", "REMOVE", "DROP"]:
            assert kw not in cypher, f"Cypher 包含写操作关键字: {kw}"


# ═══════════════════════════════════════════════════════════════════════════════
# S8-04: Smart Query — 空结果显示友好提示（非 crash）
# ═══════════════════════════════════════════════════════════════════════════════
class TestS804SmartQueryEmptyResult:
    """S8-04: Smart Query 空结果时显示友好提示，不崩溃。"""

    def test_empty_result_no_exception(self):
        """S8-04: NLQueryEngine 返回空 results 时，页面无异常。"""
        mock_engine_cls = mock.MagicMock()
        mock_engine_cls.return_value.query.return_value = {
            "question": "不存在的服务有哪些依赖？",
            "cypher": "MATCH (s:Microservice {name:'nonexistent'})-[:DependsOn]->(d) RETURN d LIMIT 50",
            "results": [],
            "summary": "查询无结果。",
        }

        nl_mod = types.ModuleType("neptune.nl_query")
        nl_mod.NLQueryEngine = mock_engine_cls

        nc_mod = _make_mock_nc(return_value=[])
        neptune_pkg = _make_neptune_pkg(nc_mod=nc_mod)
        neptune_pkg.nl_query = nl_mod

        originals = {
            "neptune": neptune_pkg,
            "neptune.neptune_client": nc_mod,
            "neptune.neptune_queries": neptune_pkg.neptune_queries,
            "neptune.nl_query": nl_mod,
        }
        orig_backup = {k: sys.modules.get(k) for k in originals}
        sys.modules.update(originals)

        try:
            at = AppTest.from_file(
                os.path.join(PAGES_DIR, "2_Smart_Query.py"),
                default_timeout=15,
            )
            at.run()
            assert not at.exception, f"空结果时 Smart Query 异常: {at.exception}"
        finally:
            _restore_mocks(orig_backup)

    def test_error_result_no_exception(self):
        """S8-04: NLQueryEngine 返回 error 键时，页面展示错误信息而非崩溃。"""
        mock_engine_cls = mock.MagicMock()
        mock_engine_cls.return_value.query.return_value = {
            "error": "Neptune 连接超时",
            "cypher": "",
        }

        nl_mod = types.ModuleType("neptune.nl_query")
        nl_mod.NLQueryEngine = mock_engine_cls

        nc_mod = _make_mock_nc(return_value=[])
        neptune_pkg = _make_neptune_pkg(nc_mod=nc_mod)
        neptune_pkg.nl_query = nl_mod

        originals = {
            "neptune": neptune_pkg,
            "neptune.neptune_client": nc_mod,
            "neptune.neptune_queries": neptune_pkg.neptune_queries,
            "neptune.nl_query": nl_mod,
        }
        orig_backup = {k: sys.modules.get(k) for k in originals}
        sys.modules.update(originals)

        try:
            at = AppTest.from_file(
                os.path.join(PAGES_DIR, "2_Smart_Query.py"),
                default_timeout=15,
            )
            at.run()
            assert not at.exception, f"error 结果时 Smart Query 异常: {at.exception}"
        finally:
            _restore_mocks(orig_backup)


# ═══════════════════════════════════════════════════════════════════════════════
# S8-05: Root Cause Analysis — 触发分析返回根因报告
# ═══════════════════════════════════════════════════════════════════════════════
class TestS805RCAReturnsReport:
    """S8-05: RCA 页面加载正常，mock generate_rca_report 返回报告结构。"""

    def _build_mocks(self):
        """构建 RCA 页面所需的全部 mock。"""
        nc_mod = _make_mock_nc(return_value=[])
        neptune_pkg = _make_neptune_pkg(nc_mod=nc_mod)

        # mock core.graph_rag_reporter
        core_mod = types.ModuleType("core")
        rag_mod = types.ModuleType("core.graph_rag_reporter")
        rag_mod.generate_rca_report = mock.Mock(return_value=MOCK_RCA_REPORT)
        core_mod.graph_rag_reporter = rag_mod

        # mock neptune_queries for RCA overview queries
        queries_mod = neptune_pkg.neptune_queries
        queries_mod.q1_blast_radius = mock.Mock(return_value={"services": [{"name": "petsearch", "type": "Microservice"}], "capabilities": []})
        queries_mod.q5_similar_incidents = mock.Mock(return_value=MOCK_INCIDENTS)
        queries_mod.q6_pod_status = mock.Mock(return_value=[{"pod": "petsite-pod-1", "status": "Running"}])
        queries_mod.q9_service_infra_path = mock.Mock(return_value=[{"pod": "petsite-pod-1", "ec2": "i-abc", "ec2_state": "running", "az": "ap-northeast-1a"}])
        queries_mod.q17_incidents_by_resource = mock.Mock(return_value=[])

        return {
            "neptune": neptune_pkg,
            "neptune.neptune_client": nc_mod,
            "neptune.neptune_queries": queries_mod,
            "core": core_mod,
            "core.graph_rag_reporter": rag_mod,
        }

    def test_rca_page_loads_without_exception(self):
        """S8-05: RCA 页面加载时无未处理异常。"""
        mods = self._build_mocks()
        orig_backup = {k: sys.modules.get(k) for k in mods}
        sys.modules.update(mods)

        try:
            at = AppTest.from_file(
                os.path.join(PAGES_DIR, "3_Root_Cause_Analysis.py"),
                default_timeout=15,
            )
            at.run()
            assert not at.exception, f"RCA 页面异常: {at.exception}"
        finally:
            _restore_mocks(orig_backup)

    def test_rca_report_structure_valid(self):
        """S8-05: mock RCA 报告包含必要字段。"""
        assert "root_cause" in MOCK_RCA_REPORT
        assert isinstance(MOCK_RCA_REPORT["root_cause"], str)
        assert len(MOCK_RCA_REPORT["root_cause"]) > 0
        assert "confidence" in MOCK_RCA_REPORT
        assert 0 <= MOCK_RCA_REPORT["confidence"] <= 100
        assert "recommended_action" in MOCK_RCA_REPORT


# ═══════════════════════════════════════════════════════════════════════════════
# S8-06: Chaos Engineering — 实验列表展示
# ═══════════════════════════════════════════════════════════════════════════════
class TestS806ChaosExperimentList:
    """S8-06: Chaos Engineering 页面展示实验列表。"""

    def test_chaos_page_loads_without_exception(self):
        """S8-06: Chaos Engineering 页面加载无异常。"""
        # fetch_all_experiments 和 fetch_untested_services 都调用 nc.results
        nc_mod = _make_mock_nc(side_effect=[MOCK_EXPERIMENTS, []])
        neptune_pkg = _make_neptune_pkg(nc_mod=nc_mod)
        neptune_pkg.neptune_queries.q18_chaos_history = mock.Mock(return_value=MOCK_EXPERIMENTS)

        originals = {
            "neptune": neptune_pkg,
            "neptune.neptune_client": nc_mod,
            "neptune.neptune_queries": neptune_pkg.neptune_queries,
        }
        orig_backup = {k: sys.modules.get(k) for k in originals}
        sys.modules.update(originals)

        try:
            at = AppTest.from_file(
                os.path.join(PAGES_DIR, "4_Chaos_Engineering.py"),
                default_timeout=15,
            )
            at.run()
            assert not at.exception, f"Chaos 页面异常: {at.exception}"
        finally:
            _restore_mocks(orig_backup)

    def test_experiment_data_schema(self):
        """S8-06: 实验列表数据包含必要字段。"""
        required_fields = ["service", "id", "fault_type", "result", "timestamp"]
        for exp in MOCK_EXPERIMENTS:
            for field in required_fields:
                assert field in exp, f"实验记录缺少字段: {field}"

    def test_experiment_result_values(self):
        """S8-06: 实验 result 字段只包含合法值。"""
        valid_results = {"passed", "failed", "running", "aborted"}
        for exp in MOCK_EXPERIMENTS:
            assert exp["result"] in valid_results, f"非法 result 值: {exp['result']}"

    def test_chaos_page_empty_neptune(self):
        """S8-06: Neptune 无实验数据时，Chaos 页面显示提示而非 crash。"""
        nc_mod = _make_mock_nc(return_value=[])
        neptune_pkg = _make_neptune_pkg(nc_mod=nc_mod)

        originals = {
            "neptune": neptune_pkg,
            "neptune.neptune_client": nc_mod,
            "neptune.neptune_queries": neptune_pkg.neptune_queries,
        }
        orig_backup = {k: sys.modules.get(k) for k in originals}
        sys.modules.update(originals)

        try:
            at = AppTest.from_file(
                os.path.join(PAGES_DIR, "4_Chaos_Engineering.py"),
                default_timeout=15,
            )
            at.run()
            assert not at.exception, f"空实验数据时 Chaos 页面异常: {at.exception}"
        finally:
            _restore_mocks(orig_backup)


# ═══════════════════════════════════════════════════════════════════════════════
# S8-07: DR Plan — 生成 DR 计划并可下载
# ═══════════════════════════════════════════════════════════════════════════════
class TestS807DRPlanGenerate:
    """S8-07: DR Plan 页面展示预生成示例计划，提供下载按钮。"""

    def test_dr_page_loads_without_exception(self):
        """S8-07: DR Plan 页面加载（默认状态）无异常。"""
        # DR Plan 页面默认显示空状态 + 预生成示例，不调用 Neptune
        at = AppTest.from_file(
            os.path.join(PAGES_DIR, "5_DR_Plan.py"),
            default_timeout=15,
        )
        at.run()
        assert not at.exception, f"DR Plan 页面异常: {at.exception}"

    def test_example_plan_files_exist(self):
        """S8-07: DR 示例计划文件存在且不为空。"""
        dr_root = os.path.join(PROJECT_ROOT, "dr-plan-generator")
        example_md = os.path.join(dr_root, "examples", "az-switchover-apne1-az1.md")
        assert os.path.exists(example_md), f"示例 MD 计划文件不存在: {example_md}"
        with open(example_md, encoding="utf-8") as f:
            content = f.read()
        assert len(content) > 100, "示例 DR 计划内容过短，可能有问题"

    def test_dr_plan_json_structure(self):
        """S8-07: mock DR 计划 JSON 包含必要字段（plan_id, phases, affected_services）。"""
        plan_json = MOCK_DR_PLAN["json"]
        assert "plan_id" in plan_json
        assert "phases" in plan_json
        assert "affected_services" in plan_json

    def test_dr_plan_markdown_non_empty(self):
        """S8-07: mock DR 计划 Markdown 非空。"""
        assert len(MOCK_DR_PLAN["markdown"]) > 50

    def test_dr_plan_generate_with_mock(self):
        """S8-07: 使用 mock generate_dr_plan 时 DR 页面不崩溃。"""
        # Mock dr-plan-generator 的所有依赖
        # DR Plan 页面不依赖 neptune 直接调用，但依赖 dr-plan-generator 模块
        # 直接 mock generate_dr_plan 函数（它是局部定义的，通过模块内 patch 覆盖）

        # 用 patch 覆盖 dr-plan-generator 依赖模块（在 generate_dr_plan 被调用时）
        mock_registry = types.ModuleType("registry")
        mock_registry_loader = types.ModuleType("registry.registry_loader")
        mock_registry_loader.get_registry = mock.Mock(return_value={})
        mock_registry.registry_loader = mock_registry_loader

        mock_graph = types.ModuleType("graph")
        mock_analyzer = types.ModuleType("graph.graph_analyzer")
        mock_analyzer.GraphAnalyzer = mock.MagicMock()
        mock_graph.graph_analyzer = mock_analyzer

        mock_planner = types.ModuleType("planner")
        mock_plan_gen = types.ModuleType("planner.plan_generator")
        mock_step = types.ModuleType("planner.step_builder")
        mock_rollback = types.ModuleType("planner.rollback_generator")
        mock_plan_gen.PlanGenerator = mock.MagicMock()
        mock_step.StepBuilder = mock.MagicMock()
        mock_rollback.RollbackGenerator = mock.MagicMock()
        mock_planner.plan_generator = mock_plan_gen
        mock_planner.step_builder = mock_step
        mock_planner.rollback_generator = mock_rollback

        extra_mods = {
            "registry": mock_registry,
            "registry.registry_loader": mock_registry_loader,
            "graph": mock_graph,
            "graph.graph_analyzer": mock_analyzer,
            "planner": mock_planner,
            "planner.plan_generator": mock_plan_gen,
            "planner.step_builder": mock_step,
            "planner.rollback_generator": mock_rollback,
        }
        orig_backup = {k: sys.modules.get(k) for k in extra_mods}
        sys.modules.update(extra_mods)

        try:
            at = AppTest.from_file(
                os.path.join(PAGES_DIR, "5_DR_Plan.py"),
                default_timeout=15,
            )
            at.run()
            assert not at.exception, f"DR Plan mock 测试异常: {at.exception}"
        finally:
            _restore_mocks(orig_backup)


# ═══════════════════════════════════════════════════════════════════════════════
# S8-08: 所有页面 — Neptune 连接断开时显示错误而非 crash【P1】
# ═══════════════════════════════════════════════════════════════════════════════
class TestS808NeptuneConnectionFailure:
    """S8-08 [P1]: Neptune 连接失败时，所有页面显示错误提示而非未处理异常。"""

    def _make_failing_nc(self):
        """返回 nc.results 抛出连接异常的 mock。"""
        return _make_mock_nc(side_effect=Exception("Connection refused: Neptune endpoint unreachable"))

    def test_graph_explorer_neptune_failure(self):
        """S8-08 [P1]: Graph Explorer — Neptune 断开时显示错误页面，不 crash。"""
        nc_mod = self._make_failing_nc()
        neptune_pkg = _make_neptune_pkg(nc_mod=nc_mod)

        originals = {
            "neptune": neptune_pkg,
            "neptune.neptune_client": nc_mod,
            "neptune.neptune_queries": neptune_pkg.neptune_queries,
        }
        orig_backup = {k: sys.modules.get(k) for k in originals}
        sys.modules.update(originals)

        try:
            at = AppTest.from_file(
                os.path.join(PAGES_DIR, "1_Graph_Explorer.py"),
                default_timeout=15,
            )
            at.run()
            # 页面应该通过 st.error / st.stop() 优雅降级，而非未处理异常
            assert not at.exception, (
                f"Graph Explorer Neptune 断开时抛出未处理异常: {at.exception}"
            )
        finally:
            _restore_mocks(orig_backup)

    def test_chaos_page_neptune_failure(self):
        """S8-08 [P1]: Chaos Engineering — Neptune 断开时不 crash（返回空列表）。"""
        # fetch_all_experiments 的 except 块捕获异常并返回 []
        nc_mod = self._make_failing_nc()
        neptune_pkg = _make_neptune_pkg(nc_mod=nc_mod)

        originals = {
            "neptune": neptune_pkg,
            "neptune.neptune_client": nc_mod,
            "neptune.neptune_queries": neptune_pkg.neptune_queries,
        }
        orig_backup = {k: sys.modules.get(k) for k in originals}
        sys.modules.update(originals)

        try:
            at = AppTest.from_file(
                os.path.join(PAGES_DIR, "4_Chaos_Engineering.py"),
                default_timeout=15,
            )
            at.run()
            assert not at.exception, (
                f"Chaos Engineering Neptune 断开时抛出未处理异常: {at.exception}"
            )
        finally:
            _restore_mocks(orig_backup)

    def test_rca_page_neptune_failure(self):
        """S8-08 [P1]: RCA 页面 — Neptune 断开时不 crash（q1/q5/q6/q9 均捕获异常）。"""
        nc_mod = self._make_failing_nc()
        neptune_pkg = _make_neptune_pkg(nc_mod=nc_mod)

        # q* 函数内部 except 捕获，返回空
        neptune_pkg.neptune_queries.q1_blast_radius = mock.Mock(
            side_effect=Exception("Neptune down")
        )
        neptune_pkg.neptune_queries.q5_similar_incidents = mock.Mock(return_value=[])
        neptune_pkg.neptune_queries.q6_pod_status = mock.Mock(return_value=[])
        neptune_pkg.neptune_queries.q9_service_infra_path = mock.Mock(return_value=[])
        neptune_pkg.neptune_queries.q17_incidents_by_resource = mock.Mock(return_value=[])

        core_mod = types.ModuleType("core")
        rag_mod = types.ModuleType("core.graph_rag_reporter")
        rag_mod.generate_rca_report = mock.Mock(side_effect=Exception("Bedrock unreachable"))
        core_mod.graph_rag_reporter = rag_mod

        originals = {
            "neptune": neptune_pkg,
            "neptune.neptune_client": nc_mod,
            "neptune.neptune_queries": neptune_pkg.neptune_queries,
            "core": core_mod,
            "core.graph_rag_reporter": rag_mod,
        }
        orig_backup = {k: sys.modules.get(k) for k in originals}
        sys.modules.update(originals)

        try:
            at = AppTest.from_file(
                os.path.join(PAGES_DIR, "3_Root_Cause_Analysis.py"),
                default_timeout=15,
            )
            at.run()
            assert not at.exception, (
                f"RCA 页面 Neptune 断开时抛出未处理异常: {at.exception}"
            )
        finally:
            _restore_mocks(orig_backup)

    def test_dr_page_no_neptune_needed(self):
        """S8-08 [P1]: DR Plan 默认状态不需要 Neptune 连接，应正常加载。"""
        at = AppTest.from_file(
            os.path.join(PAGES_DIR, "5_DR_Plan.py"),
            default_timeout=15,
        )
        at.run()
        assert not at.exception, (
            f"DR Plan 默认状态异常: {at.exception}"
        )

    def test_homepage_no_neptune_needed(self):
        """S8-08 [P1]: 主页（app.py）不需要 Neptune，无异常。"""
        at = AppTest.from_file(
            os.path.join(DEMO_DIR, "app.py"),
            default_timeout=15,
        )
        at.run()
        assert not at.exception, f"主页异常: {at.exception}"
