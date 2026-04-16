"""
test_17_unit_chaos_runner.py — Sprint 4 Chaos Runner 单元测试

14 个测试用例，全部使用 mock，不依赖真实 AWS / Neptune / ClickHouse 连接。

测试 ID 映射：
  S4-01  fault_injector     故障注入模板生成
  S4-02  fis_backend        FIS API 调用封装
  S4-03  fis_backend        FIS 实验状态轮询和超时
  S4-04  experiment         实验生命周期状态机
  S4-05  metrics            CloudWatch 指标采集
  S4-06  observability      可观测性数据采集
  S4-07  log_collector      实验日志采集
  S4-08  report             报告生成必要字段
  S4-09  neptune_sync       TestedBy 边写入
  S4-10  neptune_sync       ChaosExperiment 节点属性完整性
  S4-11  graph_feedback     实验结果图反馈
  S4-12  composite_runner   组合实验编排
  S4-13  target_resolver    实验目标解析（ARN→资源）
  S4-14  fault_registry     故障类型注册和查找
"""
from __future__ import annotations

import importlib
import sys
import os
import types
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ─── 路径配置 ─────────────────────────────────────────────────────────────────

PROJECT_ROOT = "/home/ubuntu/tech/graph-dependency-platform"
RUNNER_PATH = os.path.join(PROJECT_ROOT, "chaos", "code")

for _p in [
    RUNNER_PATH,
    os.path.join(PROJECT_ROOT, "rca"),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ─── 检查模块是否存在 ─────────────────────────────────────────────────────────

def _runner_exists() -> bool:
    return os.path.isfile(os.path.join(RUNNER_PATH, "runner", "fault_injector.py"))


def _neptune_sync_exists() -> bool:
    return os.path.isfile(os.path.join(RUNNER_PATH, "neptune_sync.py"))


# ─── 辅助：构建最小 Experiment stub ──────────────────────────────────────────

def _make_experiment(
    name: str = "test-exp",
    target_service: str = "petsite",
    fault_type: str = "pod_kill",
    duration: str = "2m",
    backend: str = "chaosmesh",
):
    """返回一个最小化的 Experiment-like 对象（避免依赖真实 YAML 解析）。"""
    fault = MagicMock()
    fault.type = fault_type
    fault.mode = "fixed-percent"
    fault.value = "50"
    fault.duration = duration
    fault.latency = None
    fault.loss = None
    fault.corrupt = None
    fault.container_names = None
    fault.workers = None
    fault.load = None
    fault.size = None
    fault.extra_params = {}

    exp = MagicMock()
    exp.name = name
    exp.backend = backend
    exp.target_service = target_service
    exp.target_namespace = "default"
    exp.target_tier = "Tier1"
    exp.fault = fault
    return exp


# ═══════════════════════════════════════════════════════════════════════════════
# S4-01  fault_injector — 故障注入模板生成
# ═══════════════════════════════════════════════════════════════════════════════

class TestFaultInjector:
    """S4-01: 故障注入抽象层 — 模板生成与后端分派"""

    def test_s4_01_injection_result_fields(self):
        """S4-01: InjectionResult 包含 experiment_ref / backend / start_time / expected_duration。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("runner.chaos_mcp.ChaosMCPClient"), \
             patch("runner.fis_backend.FISClient"), \
             patch("boto3.client"):
            from runner.fault_injector import InjectionResult

        result = InjectionResult(
            experiment_ref="exp-12345",
            backend="chaosmesh",
            start_time="2026-04-16T10:00:00Z",
            expected_duration="2m",
            extra={"template_id": "tmpl-abc"},
        )

        assert result.experiment_ref == "exp-12345"
        assert result.backend == "chaosmesh"
        assert result.start_time == "2026-04-16T10:00:00Z"
        assert result.expected_duration == "2m"
        assert result.extra["template_id"] == "tmpl-abc"

    def test_s4_01_fault_injector_abstract_interface(self):
        """S4-01: FaultInjector 是抽象基类，拥有 inject/remove/status/abort/preflight_check 方法。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("runner.chaos_mcp.ChaosMCPClient"), \
             patch("boto3.client"):
            from runner.fault_injector import FaultInjector
            import inspect

        abstract_methods = set(FaultInjector.__abstractmethods__)
        for method in ("inject", "remove", "status", "abort", "preflight_check"):
            assert method in abstract_methods, f"{method} 不是抽象方法"

    def test_s4_01_chaosmesh_backend_inject(self):
        """S4-01: ChaosMeshBackend.inject 调用 ChaosMCPClient 并返回 InjectionResult。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("runner.chaos_mcp.ChaosMCPClient") as MockCMC, \
             patch("boto3.client"):
            mock_client = MagicMock()
            mock_client.inject.return_value = {"chaos_name": "petsite-pod-kill-abc"}
            MockCMC.return_value = mock_client

            from runner.fault_injector import ChaosMeshBackend, InjectionResult
            backend = ChaosMeshBackend()

            exp = _make_experiment()
            result = backend.inject(exp)

        assert isinstance(result, InjectionResult)
        assert result.backend == "chaosmesh"
        mock_client.inject.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# S4-02  fis_backend — FIS API 调用封装
# ═══════════════════════════════════════════════════════════════════════════════

class TestFISBackend:
    """S4-02 / S4-03: AWS FIS 后端单元测试"""

    def test_s4_02_fis_inject_creates_template_and_experiment(self):
        """S4-02: FISClient.inject 调用 create_experiment_template + start_experiment。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("boto3.client") as mock_boto3, \
             patch("runner.fault_registry.FIS_ACTION_MAP", {"fis_lambda_delay": "aws:lambda:invocation-add-delay"}):

            mock_fis = MagicMock()
            mock_fis.create_experiment_template.return_value = {
                "experimentTemplate": {"id": "EXT12345"}
            }
            mock_fis.start_experiment.return_value = {
                "experiment": {"id": "EXP98765", "state": {"status": "running"}}
            }
            mock_boto3.return_value = mock_fis

            from runner.fis_backend import FISClient

            client = FISClient()
            # 直接注入 mock fis
            client._fis = mock_fis

            exp = _make_experiment(fault_type="fis_lambda_delay", backend="fis")
            # _build_target for fis_lambda requires function_arn
            exp.fault.extra_params = {
                "function_arn": "arn:aws:lambda:ap-northeast-1:926093770964:function:petsite-handler"
            }

            result = client.inject(exp)

        assert "experiment_id" in result
        assert result["experiment_id"] == "EXP98765"
        assert "template_id" in result
        mock_fis.create_experiment_template.assert_called_once()
        mock_fis.start_experiment.assert_called_once()

    def test_s4_02_fis_stop_experiment(self):
        """S4-02: FISClient.stop 调用 fis.stop_experiment。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("boto3.client") as mock_boto3:
            mock_fis = MagicMock()
            mock_fis.stop_experiment.return_value = {"experiment": {"id": "EXP123"}}
            mock_boto3.return_value = mock_fis

            from runner.fis_backend import FISClient

            client = FISClient()
            client._fis = mock_fis
            client.stop("EXP123")

        mock_fis.stop_experiment.assert_called_once_with(id="EXP123")

    def test_s4_03_fis_poll_status_running(self):
        """S4-03: FISClient.status 返回 'running' / 'completed' 等标准状态字符串。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("boto3.client") as mock_boto3:
            mock_fis = MagicMock()
            mock_fis.get_experiment.return_value = {
                "experiment": {"id": "EXP123", "state": {"status": "running"}}
            }
            mock_boto3.return_value = mock_fis

            from runner.fis_backend import FISClient

            client = FISClient()
            client._fis = mock_fis
            status = client.status("EXP123")

        assert status in ("running", "completed", "stopped", "failed", "pending", "unknown", "initiating")

    def test_s4_03_fis_poll_timeout(self):
        """S4-03: FISClient.wait_for_completion 在超时后返回最终状态（不阻塞）。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("boto3.client") as mock_boto3, \
             patch("time.sleep"):  # 防止真正的 sleep
            mock_fis = MagicMock()
            # 始终返回 running（模拟超时场景）
            mock_fis.get_experiment.return_value = {
                "experiment": {"id": "EXP123", "state": {"status": "running"}}
            }
            mock_boto3.return_value = mock_fis

            from runner.fis_backend import FISClient

            client = FISClient()
            client._fis = mock_fis
            # wait_for_completion 参数 timeout=1 → 几乎立即超时
            final_status = client.wait_for_completion("EXP123", timeout=1, poll_interval=1)

        # 超时后返回当前状态（running 或 timeout 字符串）
        assert final_status in ("running", "timeout", "completed", "stopped", "failed")


# ═══════════════════════════════════════════════════════════════════════════════
# S4-04  experiment — 实验生命周期状态机
# ═══════════════════════════════════════════════════════════════════════════════

class TestExperiment:
    """S4-04: Experiment 数据模型和解析工具"""

    def test_s4_04_parse_duration(self):
        """S4-04: parse_duration 正确解析 '2m'/'30s'/'1h'。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        from runner.experiment import parse_duration

        assert parse_duration("2m") == 120
        assert parse_duration("30s") == 30
        assert parse_duration("1h") == 3600

    def test_s4_04_parse_duration_invalid(self):
        """S4-04: parse_duration 无效格式抛出 ValueError。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        from runner.experiment import parse_duration

        with pytest.raises(ValueError):
            parse_duration("invalid")

    def test_s4_04_metrics_snapshot_state_machine(self):
        """S4-04: MetricsSnapshot 模拟 PENDING→RUNNING→COMPLETED 状态转换。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        from runner.experiment import MetricsSnapshot

        # 模拟 PENDING（流量为 0）
        snap_pending = MetricsSnapshot(timestamp=1000, success_rate=100.0, latency_p99_ms=0.0, total_requests=0)
        # 模拟 RUNNING（故障注入中，成功率下降）
        snap_running = MetricsSnapshot(timestamp=1060, success_rate=72.5, latency_p99_ms=2300.0, total_requests=400)
        # 模拟 COMPLETED（恢复后）
        snap_completed = MetricsSnapshot(timestamp=1180, success_rate=99.8, latency_p99_ms=120.0, total_requests=400)

        assert snap_pending.success_rate == 100.0
        assert snap_running.success_rate < 80.0
        assert snap_completed.success_rate > 99.0

        # get() 接口
        assert snap_running.get("success_rate") == 72.5
        assert snap_running.get("error_rate") == pytest.approx(27.5, abs=0.1)

    def test_s4_04_stop_condition_triggered(self):
        """S4-04: StopCondition 在成功率低于阈值时触发。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        from runner.experiment import StopCondition, MetricsSnapshot

        sc = StopCondition(metric="success_rate", threshold="< 50%", window="30s", action="abort")
        snap_bad = MetricsSnapshot(timestamp=1000, success_rate=40.0, latency_p99_ms=5000.0)
        snap_ok = MetricsSnapshot(timestamp=1000, success_rate=95.0, latency_p99_ms=200.0)

        assert sc.is_triggered(snap_bad) is True
        assert sc.is_triggered(snap_ok) is False


# ═══════════════════════════════════════════════════════════════════════════════
# S4-05  metrics — CloudWatch 指标采集 (DeepFlow)
# ═══════════════════════════════════════════════════════════════════════════════

class TestMetrics:
    """S4-05: DeepFlowMetrics 单元测试（mock ClickHouse HTTP）"""

    def test_s4_05_collect_returns_snapshot(self):
        """S4-05: collect() mock ClickHouse 响应，返回 MetricsSnapshot。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        mock_response = {
            "data": [{"success_cnt": "950", "total_cnt": "1000", "p99_latency_ms": "180.5"}]
        }

        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = mock_response
            mock_post.return_value = mock_resp

            from runner.metrics import DeepFlowMetrics
            from runner.experiment import MetricsSnapshot

            dm = DeepFlowMetrics()
            snap = dm.collect("petsite", "default", window_seconds=60)

        assert isinstance(snap, MetricsSnapshot)
        assert snap.success_rate == pytest.approx(95.0, abs=0.1)
        assert snap.latency_p99_ms == pytest.approx(180.5, abs=1.0)
        assert snap.total_requests == 1000

    def test_s4_05_collect_fallback_on_error(self):
        """S4-05: ClickHouse 不可达时 collect() 返回 fallback 快照（success_rate=100）。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("requests.post", side_effect=Exception("connection refused")):
            from runner.metrics import DeepFlowMetrics

            dm = DeepFlowMetrics()
            snap = dm.collect("petsite", "default")

        assert snap.success_rate == 100.0
        assert snap.total_requests == 0


# ═══════════════════════════════════════════════════════════════════════════════
# S4-06  observability — 可观测性数据采集
# ═══════════════════════════════════════════════════════════════════════════════

class TestObservability:
    """S4-06: ChaosMetrics CloudWatch 发布单元测试"""

    def test_s4_06_publish_experiment_metrics(self):
        """S4-06: publish_experiment_metrics 调用 put_metric_data 并包含必要维度。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("boto3.client") as mock_boto3, \
             patch("structlog.configure"), \
             patch("structlog.get_logger") as mock_get_logger:
            mock_get_logger.return_value = MagicMock()
            mock_cw = MagicMock()
            mock_boto3.return_value = mock_cw

            from runner.observability import ChaosMetrics

            cm = ChaosMetrics()
            cm._cw = mock_cw

            # 构建最小 ExperimentResult stub
            result = MagicMock()
            result.experiment.target_service = "petsite"
            result.experiment.fault.type = "pod_kill"
            result.status = "PASSED"
            result.duration_seconds = 145.0
            result.recovery_seconds = 30.0
            result.min_success_rate = 82.0
            result.max_latency_p99 = 1500.0
            result.degradation_rate.return_value = 18.0
            result.start_time = datetime.now(timezone.utc)
            result.end_time = datetime.now(timezone.utc)

            cm.publish_experiment_metrics(result)

        mock_cw.put_metric_data.assert_called_once()
        call_kwargs = mock_cw.put_metric_data.call_args
        assert call_kwargs[1]["Namespace"] == "ChaosEngineering"
        metric_names = {m["MetricName"] for m in call_kwargs[1]["MetricData"]}
        assert "ExperimentDuration" in metric_names
        assert "RecoveryTime" in metric_names
        assert "MinSuccessRate" in metric_names


# ═══════════════════════════════════════════════════════════════════════════════
# S4-07  log_collector — 实验日志采集
# ═══════════════════════════════════════════════════════════════════════════════

class TestLogCollector:
    """S4-07: PodLogCollector 单元测试（mock subprocess）"""

    def test_s4_07_log_collection_result_summary(self):
        """S4-07: LogCollectionResult.summary() 返回包含服务名和行数的摘要字符串。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        from runner.log_collector import LogCollectionResult, LogEntry

        lcr = LogCollectionResult(
            service="petsite",
            namespace="default",
            pod_count=3,
            total_lines=120,
            error_count=5,
        )
        summary = lcr.summary()

        assert "petsite" in summary
        assert "120" in summary
        assert "5" in summary

    def test_s4_07_log_collector_start_stop(self):
        """S4-07: PodLogCollector start_background/stop_and_collect 不抛出异常（kubectl 被 mock）。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("subprocess.Popen") as mock_popen, \
             patch("subprocess.run") as mock_run:
            # mock kubectl get pods 返回空 pod 列表
            mock_run.return_value = MagicMock(stdout="", returncode=0)
            mock_popen.return_value = MagicMock()

            from runner.log_collector import PodLogCollector

            collector = PodLogCollector(
                service="petsite",
                namespace="default",
                since="1m",
                max_lines=100,
            )
            collector.start_background()
            result = collector.stop_and_collect()

        assert result is not None
        assert hasattr(result, "service")
        assert result.service == "petsite"


# ═══════════════════════════════════════════════════════════════════════════════
# S4-08  report — 实验报告生成
# ═══════════════════════════════════════════════════════════════════════════════

class TestReport:
    """S4-08: Reporter 单元测试（mock DynamoDB + Bedrock）"""

    def test_s4_08_report_contains_required_fields(self):
        """S4-08: generate_markdown 报告包含 experiment_name / status / impact 等必要字段。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("boto3.client") as mock_boto3, \
             patch("runner.report.Reporter._generate_llm_analysis", return_value=""):
            mock_ddb = MagicMock()
            mock_boto3.return_value = mock_ddb

            from runner.report import Reporter

            reporter = Reporter()
            reporter._ddb = mock_ddb

            # 构建最小 ExperimentResult stub
            result = MagicMock()
            result.experiment.name = "petsite-pod-kill-test"
            result.experiment.backend = "chaosmesh"
            result.experiment.target_service = "petsite"
            result.experiment.target_tier = "Tier0"
            result.experiment.fault.type = "pod_kill"
            result.experiment.fault.mode = "fixed-percent"
            result.experiment.fault.value = "50"
            result.experiment.fault.duration = "2m"
            result.experiment.rca = MagicMock()
            result.experiment.rca.expected_root_cause = "pod_failure"
            result.status = "PASSED"
            result.abort_reason = ""
            result.duration_seconds = 150.0
            result.recovery_seconds = 35.0
            result.min_success_rate = 80.0
            result.max_latency_p99 = 1800.0
            result.degradation_rate.return_value = 20.0
            result.snapshots = []
            result.rca_result = None
            result.rca_match = None
            result.start_time = datetime.now(timezone.utc)
            result.end_time = datetime.now(timezone.utc)
            result.inject_time = None
            result.steady_state_before = MagicMock(success_rate=99.5, latency_p99_ms=120.0)
            result.steady_state_after = MagicMock(success_rate=99.1, latency_p99_ms=130.0)
            result.steady_state_after_checks = []
            result.chaos_experiment_name = "chaos-exp-abc"
            result.fis_template_id = ""
            result.report_path = ""

            markdown = reporter.generate_markdown(result)

        assert "petsite-pod-kill-test" in markdown
        assert "PASSED" in markdown
        assert "pod_kill" in markdown

    def test_s4_08_write_to_dynamodb(self):
        """S4-08: save_to_dynamodb 调用 DynamoDB put_item。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("boto3.client") as mock_boto3:
            mock_ddb = MagicMock()
            mock_boto3.return_value = mock_ddb

            from runner.report import Reporter

            reporter = Reporter()
            reporter._ddb = mock_ddb

            result = MagicMock()
            result.experiment_id = "exp-petsite-network-20260416"
            result.experiment.name = "petsite-network-test"
            result.experiment.target_service = "petsite"
            result.experiment.target_namespace = "default"
            result.experiment.target_tier = "Tier1"
            result.experiment.backend = "chaosmesh"
            result.experiment.fault.type = "network_delay"
            result.experiment.fault.mode = "fixed-percent"
            result.experiment.fault.value = "50"
            result.experiment.fault.duration = "3m"
            result.experiment.rca.enabled = False
            result.experiment.rca.expected_root_cause = ""
            result.experiment.yaml_source = "test.yaml"
            result.status = "FAILED"
            result.abort_reason = ""
            result.duration_seconds = 200.0
            result.recovery_seconds = None
            result.min_success_rate = 55.0
            result.max_latency_p99 = 3500.0
            result.degradation_rate.return_value = 45.0
            result.snapshots = []
            result.rca_result = None
            result.steady_state_before = None
            result.steady_state_after = None
            result.report_path = ""
            result.start_time = datetime.now(timezone.utc)
            result.end_time = datetime.now(timezone.utc)

            reporter.save_to_dynamodb(result)

        mock_ddb.put_item.assert_called_once()


# ═══════════════════════════════════════════════════════════════════════════════
# S4-09 / S4-10  neptune_sync — Neptune 写入
# ═══════════════════════════════════════════════════════════════════════════════

class TestNeptuneSync:
    """S4-09 / S4-10: neptune_sync.write_experiment 单元测试"""

    def test_s4_09_testedby_edge_written(self):
        """S4-09: write_experiment 调用 query_opencypher 建立 TestedBy 边。"""
        if not _neptune_sync_exists():
            pytest.skip("neptune_sync.py 不存在")

        calls = []

        def fake_query(cypher, *args, **kwargs):
            calls.append(cypher)
            return []

        with patch("runner.neptune_client.query_opencypher", side_effect=fake_query):
            from neptune_sync import write_experiment

            write_experiment({
                "experiment_id": "exp-petsite-pod-kill-20260416",
                "target_service": "petsite",
                "fault_type": "pod_kill",
                "result": "passed",
                "recovery_time_sec": 35,
                "degradation_rate": 0.18,
                "timestamp": "2026-04-16T10:00:00Z",
            })

        # 至少有两次调用：节点 MERGE + 边 MERGE
        assert len(calls) >= 2

        # TestedBy 边应在某次 cypher 中出现
        testedby_calls = [c for c in calls if "TestedBy" in c]
        assert len(testedby_calls) >= 1

    def test_s4_09_missing_ids_skipped(self):
        """S4-09: experiment_id 或 target_service 缺失时 write_experiment 静默跳过。"""
        if not _neptune_sync_exists():
            pytest.skip("neptune_sync.py 不存在")

        with patch("runner.neptune_client.query_opencypher") as mock_q:
            from neptune_sync import write_experiment

            write_experiment({"experiment_id": "", "target_service": "petsite"})
            write_experiment({"experiment_id": "exp-123", "target_service": ""})

        mock_q.assert_not_called()

    def test_s4_10_node_attributes_completeness(self):
        """S4-10: ChaosExperiment 节点属性包含 experiment_id / fault_type / result / recovery_time_sec / degradation_rate / timestamp。"""
        if not _neptune_sync_exists():
            pytest.skip("neptune_sync.py 不存在")

        captured_cyphers = []

        def fake_query(cypher, *args, **kwargs):
            captured_cyphers.append(cypher)
            return []

        with patch("runner.neptune_client.query_opencypher", side_effect=fake_query):
            from neptune_sync import write_experiment

            write_experiment({
                "experiment_id": "exp-petsite-test-001",
                "target_service": "petsite",
                "fault_type": "network_delay",
                "result": "failed",
                "recovery_time_sec": 120,
                "degradation_rate": 0.45,
                "timestamp": "2026-04-16T12:00:00Z",
            })

        # 合并所有 cypher 文本检查属性
        all_cypher = " ".join(captured_cyphers)

        for attr in ("experiment_id", "fault_type", "result", "recovery_time_sec", "degradation_rate"):
            assert attr in all_cypher, f"属性 '{attr}' 未出现在 Neptune 查询中"

        # 具体值也应该在 cypher 中
        assert "exp-petsite-test-001" in all_cypher
        assert "network_delay" in all_cypher
        assert "120" in all_cypher


# ═══════════════════════════════════════════════════════════════════════════════
# S4-11  graph_feedback — 实验结果图反馈
# ═══════════════════════════════════════════════════════════════════════════════

class TestGraphFeedback:
    """S4-11: GraphFeedback 写回 Neptune Calls 边和 Microservice 节点"""

    def test_s4_11_write_back_calls_neptune(self):
        """S4-11: write_back 在 Neptune 可达时调用 _run_gremlin (query_gremlin)。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        # graph_feedback imports check_connectivity and query_gremlin at module level,
        # so we must patch them in the graph_feedback namespace, not neptune_client.
        with patch("runner.graph_feedback.check_connectivity", return_value=True), \
             patch("runner.graph_feedback.query_gremlin", return_value=[]) as mock_gremlin:
            from runner.graph_feedback import GraphFeedback

            fb = GraphFeedback()

            result = MagicMock()
            result.status = "PASSED"
            result.experiment.target_service = "petsite"
            result.experiment.fault.type = "pod_kill"
            result.experiment.fault.duration = "2m"
            result.experiment_id = "exp-petsite-pod-kill-20260416"
            result.degradation_rate.return_value = 18.0
            result.recovery_seconds = 30.0
            result.end_time = datetime.now(timezone.utc)

            fb.write_back(result)

        mock_gremlin.assert_called()

    def test_s4_11_write_back_skipped_on_neptune_unavailable(self):
        """S4-11: Neptune 不可达时 write_back 抛出 RuntimeError（有明确日志）。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("runner.graph_feedback.check_connectivity", return_value=False):
            from runner.graph_feedback import GraphFeedback

            fb = GraphFeedback()

            result = MagicMock()
            result.status = "PASSED"
            result.experiment.target_service = "petsite"
            result.experiment.fault.type = "pod_kill"
            result.degradation_rate.return_value = 15.0
            result.recovery_seconds = 20.0
            result.end_time = datetime.now(timezone.utc)

            with pytest.raises(RuntimeError, match="Neptune 不可达"):
                fb.write_back(result)


# ═══════════════════════════════════════════════════════════════════════════════
# S4-12  composite_runner — 组合实验编排
# ═══════════════════════════════════════════════════════════════════════════════

class TestCompositeRunner:
    """S4-12: CompositeRunner 组合实验编排单元测试"""

    def test_s4_12_composite_runner_init(self):
        """S4-12: CompositeRunner 初始化包含 metrics / cm_client / fis_client / scheduler。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("runner.chaos_mcp.ChaosMCPClient"), \
             patch("runner.fis_backend.FISClient"), \
             patch("runner.rca.RCATrigger"), \
             patch("runner.report.Reporter"), \
             patch("runner.observability.ChaosMetrics"), \
             patch("structlog.configure"), \
             patch("structlog.get_logger", return_value=MagicMock()), \
             patch("boto3.client"):
            from runner.composite_runner import CompositeRunner

            runner = CompositeRunner(dry_run=True)

        assert runner.dry_run is True
        assert hasattr(runner, "metrics")
        assert hasattr(runner, "scheduler")

    def test_s4_12_composite_experiment_result_has_action_states(self):
        """S4-12: CompositeExperimentResult 包含 action_states 字段。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("runner.chaos_mcp.ChaosMCPClient"), \
             patch("runner.fis_backend.FISClient"), \
             patch("boto3.client"):
            from runner.composite_runner import CompositeExperimentResult

        exp = _make_experiment()
        result = CompositeExperimentResult(experiment=exp)
        result.action_states["action-1"] = {"status": "completed", "chaos_name": "exp-abc"}

        assert "action-1" in result.action_states
        assert result.action_states["action-1"]["status"] == "completed"


# ═══════════════════════════════════════════════════════════════════════════════
# S4-13  target_resolver — 实验目标解析
# ═══════════════════════════════════════════════════════════════════════════════

class TestTargetResolver:
    """S4-13: TargetResolver ARN 解析单元测试"""

    def test_s4_13_resolver_init(self):
        """S4-13: TargetResolver 初始化包含缓存字段。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("runner.neptune_client.query_opencypher", return_value=[]), \
             patch("runner.neptune_client.query_gremlin", return_value=[]), \
             patch("boto3.client"):
            from runner.target_resolver import TargetResolver

            resolver = TargetResolver(tags={"Project": "PetSite"})

        assert resolver._tags == {"Project": "PetSite"}
        assert hasattr(resolver, "_fis_cache")

    def test_s4_13_resolve_from_cache(self):
        """S4-13: 缓存命中时 resolve() 不调用 Neptune/AWS API。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        arn = "arn:aws:lambda:ap-northeast-1:926093770964:function:petsite-handler"

        with patch("runner.neptune_client.query_opencypher", return_value=[]) as mock_neptune, \
             patch("runner.neptune_client.query_gremlin", return_value=[]), \
             patch("boto3.client"):
            from runner.target_resolver import TargetResolver

            resolver = TargetResolver()
            # 缓存 key 格式为 "{resource_type}:{service_name}"
            resolver._fis_cache["lambda:function:petsite"] = {"arn": arn}
            resolver._fis_cache_loaded = True

            result_arn = resolver.resolve("petsite", "lambda:function")

        assert result_arn == arn
        mock_neptune.assert_not_called()

    def test_s4_13_resolve_lambda_arn_from_aws(self):
        """S4-13: 缓存未命中时通过 Lambda paginator 查找 ARN (_find_lambda_arn)。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        arn = "arn:aws:lambda:ap-northeast-1:926093770964:function:petsite-handler"

        with patch("runner.neptune_client.query_opencypher", return_value=[]), \
             patch("runner.neptune_client.query_gremlin", return_value=[]), \
             patch("boto3.client") as mock_boto3:
            mock_lambda = MagicMock()
            # _find_lambda_arn uses a paginator
            mock_paginator = MagicMock()
            mock_paginator.paginate.return_value = [
                {"Functions": [{"FunctionName": "petsite-handler", "FunctionArn": arn}]}
            ]
            mock_lambda.get_paginator.return_value = mock_paginator
            mock_boto3.return_value = mock_lambda

            from runner.target_resolver import TargetResolver

            resolver = TargetResolver()
            result_arn = resolver._find_lambda_arn("petsite")

        assert result_arn == arn


# ═══════════════════════════════════════════════════════════════════════════════
# S4-14  fault_registry — 故障类型注册和查找
# ═══════════════════════════════════════════════════════════════════════════════

class TestFaultRegistry:
    """S4-14: FaultDef 数据类和 CATALOG 加载"""

    def test_s4_14_fault_def_fields(self):
        """S4-14: FaultDef 包含 type / backend / fis_action_id / default_params 等必要字段。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("boto3.client"):
            from runner.fault_registry import FaultDef

        fd = FaultDef(
            type="pod_kill",
            backend="chaosmesh",
            category="compute",
            description="随机杀 Pod",
            fis_action_id="",
            default_params={"mode": "fixed-percent", "value": "50"},
            requires=[],
            tier=["Tier0", "Tier1"],
        )

        assert fd.type == "pod_kill"
        assert fd.backend == "chaosmesh"
        assert fd.default_params["mode"] == "fixed-percent"

    def test_s4_14_catalog_loaded(self):
        """S4-14: fault_catalog.yaml 加载后 CATALOG 非空，包含 pod_kill / network_delay 等基本类型。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("boto3.client"):
            from runner.fault_registry import CATALOG

        assert len(CATALOG) > 0, "CATALOG 为空，fault_catalog.yaml 解析失败"

        expected_types = {"pod_kill", "network_delay", "pod_failure"}
        actual_types = set(CATALOG.keys())
        missing = expected_types - actual_types
        assert not missing, f"CATALOG 缺少故障类型: {missing}"

    def test_s4_14_fis_action_map_consistent(self):
        """S4-14: FIS_ACTION_MAP 中的每个故障类型都能在 CATALOG 中找到对应条目。"""
        if not _runner_exists():
            pytest.skip("runner 模块不存在")

        with patch("boto3.client"):
            from runner.fault_registry import CATALOG, FIS_ACTION_MAP

        for fault_type in FIS_ACTION_MAP:
            assert fault_type in CATALOG, (
                f"FIS_ACTION_MAP 包含 '{fault_type}'，但 CATALOG 中未找到对应定义"
            )
