"""
tests/test_13_unit_etl_deepflow.py — Sprint 2: DeepFlow ETL 单元测试

S2-01: DeepFlow ETL — 调用链数据解析为 Calls 边（mock DeepFlow API）
S2-02: DeepFlow ETL — AccessesData 边从 DNS 调用链推断
S2-03: DeepFlow ETL — 空数据/格式异常处理
"""

import os
import sys
import types
import json
import time
from unittest.mock import MagicMock, patch, call

import pytest

# ── 0. 文件存在检查 ──────────────────────────────────────────────────────────
DEEPFLOW_PATH = '/home/ubuntu/tech/graph-dependency-platform/infra/lambda/etl_deepflow'
_HANDLER_FILE = os.path.join(DEEPFLOW_PATH, 'neptune_etl_deepflow.py')
if not os.path.exists(_HANDLER_FILE):
    pytest.skip('etl_deepflow handler not found', allow_module_level=True)

# ── 1. 环境变量（import 前设置）─────────────────────────────────────────────
os.environ.setdefault('REGION', 'ap-northeast-1')
os.environ.setdefault('NEPTUNE_ENDPOINT', 'test-endpoint.example.com')
os.environ.setdefault('NEPTUNE_PORT', '8182')
os.environ.setdefault('CH_HOST', 'localhost')
os.environ.setdefault('CH_PORT', '8123')
os.environ.setdefault('EKS_CLUSTER_ARN',
    'arn:aws:eks:ap-northeast-1:123456789012:cluster/petsite-eks')
os.environ.setdefault('ENVIRONMENT', 'test')

# ── 2. Mock neptune_client_base（Lambda Layer，测试环境不存在）────────────────
_nc_mock = types.ModuleType('neptune_client_base')
_nc_mock.neptune_query = MagicMock(
    return_value={'result': {'data': {'@value': []}}}
)
_nc_mock.safe_str = lambda s: str(s).replace("'", "\\'").replace('"', '\\"')[:128]
_nc_mock.extract_value = lambda v: v.get('@value', v) if isinstance(v, dict) else v
_nc_mock.REGION = 'ap-northeast-1'

if 'neptune_client_base' not in sys.modules:
    sys.modules['neptune_client_base'] = _nc_mock

# ── 3. 导入 deepflow ETL 模块 ─────────────────────────────────────────────────
if DEEPFLOW_PATH not in sys.path:
    sys.path.insert(0, DEEPFLOW_PATH)

import neptune_etl_deepflow as etl_df  # noqa: E402


# ── Fixture: 覆盖 conftest 的 neptune_rca（本文件全为单测，不需真实 Neptune）──
@pytest.fixture(scope='session')
def neptune_rca():
    """Override conftest fixture: return MagicMock to prevent real Neptune call."""
    mock_nc = MagicMock()
    mock_nc.results.return_value = []
    return mock_nc


# ─────────────────────────────────────────────────────────────────────────────
# S2-01: 调用链数据解析为 Calls 边
# ─────────────────────────────────────────────────────────────────────────────

def test_s2_01_calls_edge_from_flow_data():
    """S2-01: DeepFlow ETL — 调用链数据解析为 Calls 边（mock DeepFlow API）

    验证 batch_upsert_edges 将流量数据正确转换为 Neptune Calls 边，
    Gremlin 包含正确的 src/dst 服务名、error_rate、protocol、coalesce 模式。
    """
    edges = [
        {
            'src': 'petsite',
            'dst': 'payforadoption',
            'protocol': 'HTTP',
            'port': 8080,
            'calls': 200,
            'avg_latency': 3500.0,
            'errors': 10,
            'p99_latency_ms': 18.5,
        },
        {
            'src': 'petsite',
            'dst': 'petsearch',
            'protocol': 'HTTP',
            'port': 8080,
            'calls': 50,
            'avg_latency': 1200.0,
            'errors': 0,
            'p99_latency_ms': 5.0,
        },
    ]

    with patch.object(etl_df, 'neptune_query') as mock_nq:
        mock_nq.return_value = {'result': {'data': {'@value': []}}}
        etl_df.batch_upsert_edges(edges)

    # One neptune_query call per edge
    assert mock_nq.call_count == 2, "Expected one neptune_query call per edge"

    # Inspect first edge (petsite → payforadoption)
    gremlin_0 = mock_nq.call_args_list[0][0][0]
    assert "petsite" in gremlin_0
    assert "payforadoption" in gremlin_0
    assert "Calls" in gremlin_0
    assert "addE('Calls')" in gremlin_0
    assert "coalesce" in gremlin_0
    # error_rate = 10/200 = 0.05
    assert "error_rate" in gremlin_0
    assert "protocol" in gremlin_0
    assert "HTTP" in gremlin_0

    # Inspect second edge (petsite → petsearch, 0 errors)
    gremlin_1 = mock_nq.call_args_list[1][0][0]
    assert "petsearch" in gremlin_1
    # error_rate should be 0.0 when calls > 0 and errors == 0
    assert "error_rate',0.0" in gremlin_1 or "error_rate,0.0" in gremlin_1


# ─────────────────────────────────────────────────────────────────────────────
# S2-02: AccessesData 边从 DNS 调用链推断
# ─────────────────────────────────────────────────────────────────────────────

def test_s2_02_accessesdata_edge_from_dns_inference():
    """S2-02: DeepFlow ETL — AccessesData 边从 DNS 调用链推断

    验证 run_drift_detection 在 DNS 观测到 DynamoDB 访问但 Neptune 中无声明边时，
    自动创建 AccessesData 边（observed_not_declared 路径）。
    """
    # payforadoption 的 pod 向 DynamoDB 发起了 DNS 查询
    dns_obs = {'payforadoption': {'dynamodb'}}

    with patch.object(etl_df, 'fetch_dns_connections', return_value=dns_obs), \
         patch.object(etl_df, 'neptune_query') as mock_nq:

        # 所有 Neptune 查询返回空（没有声明边）
        mock_nq.return_value = {'result': {'data': {'@value': []}}}

        etl_df.run_drift_detection(['payforadoption'], {})

    # 应该有 neptune_query 调用（检查每个 infra_type 的声明边 + 写入新边）
    assert mock_nq.call_count >= 2, (
        f"Expected at least 2 neptune_query calls, got {mock_nq.call_count}"
    )

    gremlin_calls = [c[0][0] for c in mock_nq.call_args_list]

    # 至少一个调用应创建 AccessesData 边（observed_not_declared 路径）
    access_data_writes = [
        g for g in gremlin_calls
        if "addE('AccessesData')" in g and 'payforadoption' in g
    ]
    assert len(access_data_writes) >= 1, (
        "Expected at least one AccessesData edge creation for DNS-observed DynamoDB"
    )

    # 该写入应标记为 deepflow-dns 来源
    assert "deepflow-dns" in access_data_writes[0]
    assert "observed_not_declared" in access_data_writes[0]


# ─────────────────────────────────────────────────────────────────────────────
# S2-03: 空数据/格式异常处理
# ─────────────────────────────────────────────────────────────────────────────

def test_s2_03_empty_clickhouse_data_returns_early():
    """S2-03: DeepFlow ETL — 空数据时提前返回，不写入任何节点/边

    当 ClickHouse 返回空行时，run_etl 应返回 nodes=0, edges=0，
    且不触发 drift detection 和 Neptune 写操作。
    """
    with patch.object(etl_df, 'build_ip_service_map',
                      return_value=({}, {}, {})) as mock_ip, \
         patch.object(etl_df, 'ch_query', return_value=[]) as mock_ch, \
         patch.object(etl_df, 'fetch_l7_metrics', return_value={}) as mock_l7, \
         patch.object(etl_df, 'fetch_active_connections', return_value={}) as mock_ac, \
         patch.object(etl_df, 'batch_upsert_nodes') as mock_nodes, \
         patch.object(etl_df, 'batch_upsert_edges') as mock_edges, \
         patch.object(etl_df, 'run_drift_detection') as mock_drift, \
         patch.object(etl_df, 'neptune_query') as mock_nq:

        mock_nq.return_value = {'result': {'data': {'@value': [{'@value': 0}]}}}

        result = etl_df.run_etl()

    assert result['nodes'] == 0
    assert result['edges'] == 0
    mock_nodes.assert_not_called()
    mock_edges.assert_not_called()
    mock_drift.assert_not_called()


def test_s2_03_malformed_rows_are_skipped():
    """S2-03: DeepFlow ETL — 格式异常行（列数不足/类型错误）被跳过，不导致崩溃

    当 ClickHouse 返回混合数据（部分行格式正确，部分行缺少列）时，
    run_etl 只处理合法行，异常行被安静跳过。
    """
    ip_map = {
        '10.0.0.1': {'name': 'petsite',         'namespace': 'default', 'az': ''},
        '10.0.0.2': {'name': 'payforadoption',   'namespace': 'default', 'az': ''},
    }
    # Row 0: 仅 3 列（缺少 calls/latency/errors）→ 应被跳过
    # Row 1: 8 列合法数据 → 应被处理
    mixed_rows = [
        ['10.0.0.1', '10.0.0.2', '8080'],
        ['10.0.0.1', '10.0.0.2', '8080', 'HTTP', '100', '4000.0', '5', '12.3'],
    ]

    with patch.object(etl_df, 'build_ip_service_map',
                      return_value=(ip_map, {}, {})), \
         patch.object(etl_df, 'ch_query', return_value=mixed_rows), \
         patch.object(etl_df, 'fetch_l7_metrics', return_value={}), \
         patch.object(etl_df, 'fetch_active_connections', return_value={}), \
         patch.object(etl_df, 'fetch_replica_counts', return_value={}), \
         patch.object(etl_df, 'fetch_resource_limits', return_value={}), \
         patch.object(etl_df, 'fetch_nfm_throttling', return_value={}), \
         patch.object(etl_df, 'batch_fetch_dependency_and_update'), \
         patch.object(etl_df, 'run_drift_detection'), \
         patch.object(etl_df, 'neptune_query') as mock_nq:

        mock_nq.return_value = {'result': {'data': {'@value': [{'@value': 0}]}}}

        # Should not raise
        result = etl_df.run_etl()

    # Only the valid row should produce an edge
    assert result['edges'] == 1, (
        f"Expected 1 edge from 1 valid row, got {result['edges']}"
    )
    assert result['nodes'] == 2
