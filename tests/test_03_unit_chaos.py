"""
test_03_unit_chaos.py — chaos 模块单元测试

Tests: U-A2-01 ~ U-A2-06
"""
import sys
import os
from unittest.mock import patch, MagicMock

import pytest

PROJECT_ROOT = '/home/ubuntu/tech/graph-dependency-platform'


@pytest.fixture(scope='module')
def chaos_experiment_base():
    """标准测试实验 dict。"""
    return {
        'experiment_id': 'test-auto-chaos-unit-001',
        'target_service': 'petsite',
        'fault_type': 'pod-kill',
        'result': 'passed',
        'recovery_time_sec': 30,
        'degradation_rate': 0.05,
        'timestamp': '2026-04-01T00:00:00Z',
    }


@pytest.fixture(scope='module', autouse=True)
def cleanup_unit_chaos(neptune_rca):
    """清理 test_03 中创建的测试节点。"""
    yield
    try:
        neptune_rca.results(
            "MATCH (n:ChaosExperiment) WHERE n.experiment_id STARTS WITH 'test-auto-chaos-unit-' "
            "DETACH DELETE n"
        )
    except Exception:
        pass


def test_ua2_01_write_new_experiment(neptune_rca, chaos_experiment_base):
    """U-A2-01: 写入新实验节点 → Neptune 可查到 ChaosExperiment + TestedBy 边。"""
    from neptune_sync import write_experiment

    write_experiment(chaos_experiment_base)

    # 验证节点存在
    rows = neptune_rca.results(
        "MATCH (exp:ChaosExperiment {experiment_id: $id}) RETURN exp.fault_type AS ft",
        {'id': chaos_experiment_base['experiment_id']},
    )
    assert len(rows) == 1
    assert rows[0].get('ft') == 'pod-kill'

    # 验证 TestedBy 边
    edges = neptune_rca.results(
        "MATCH (svc:Microservice {name: $svc})-[:TestedBy]->(exp:ChaosExperiment {experiment_id: $id}) "
        "RETURN exp.experiment_id AS eid",
        {'svc': 'petsite', 'id': chaos_experiment_base['experiment_id']},
    )
    assert len(edges) >= 1


def test_ua2_02_idempotent_write(neptune_rca, chaos_experiment_base):
    """U-A2-02: 同一 experiment_id 写入 2 次 → Neptune 只有 1 个节点，属性取最新值。"""
    from neptune_sync import write_experiment

    # 第二次写入，更新 result
    updated = dict(chaos_experiment_base)
    updated['result'] = 'failed'
    updated['recovery_time_sec'] = 90
    write_experiment(updated)

    rows = neptune_rca.results(
        "MATCH (exp:ChaosExperiment {experiment_id: $id}) RETURN exp.result AS r, exp.recovery_time_sec AS rt",
        {'id': chaos_experiment_base['experiment_id']},
    )
    assert len(rows) == 1, f"Expected 1 node, got {len(rows)}"
    assert rows[0].get('r') == 'failed'
    assert rows[0].get('rt') == 90


def test_ua2_03_nonexistent_service(neptune_rca):
    """U-A2-03: target_service 不存在时，ChaosExperiment 节点创建成功，但 TestedBy 边无法建立，不抛异常。"""
    from neptune_sync import write_experiment

    exp = {
        'experiment_id': 'test-auto-chaos-unit-noservice',
        'target_service': 'nonexistent-service-xyz',
        'fault_type': 'network-delay',
        'result': 'passed',
        'recovery_time_sec': 10,
        'degradation_rate': 0.0,
        'timestamp': '2026-04-01T00:00:00Z',
    }
    # 不应抛异常
    try:
        write_experiment(exp)
    except Exception as e:
        pytest.fail(f"write_experiment raised unexpectedly: {e}")

    # 节点应该存在
    rows = neptune_rca.results(
        "MATCH (exp:ChaosExperiment {experiment_id: $id}) RETURN exp",
        {'id': exp['experiment_id']},
    )
    assert len(rows) == 1

    # 但 TestedBy 边应该不存在
    edges = neptune_rca.results(
        "MATCH (svc:Microservice {name: 'nonexistent-service-xyz'})-[:TestedBy]->(exp:ChaosExperiment {experiment_id: $id}) "
        "RETURN exp",
        {'id': exp['experiment_id']},
    )
    assert len(edges) == 0


def test_ua2_04_neptune_write_failure():
    """U-A2-04: Neptune 写入失败时 write_experiment 抛出异常（上层 runner.py 有 try/except）。"""
    from neptune_sync import write_experiment

    exp = {
        'experiment_id': 'test-auto-chaos-unit-fail',
        'target_service': 'petsite',
        'fault_type': 'cpu-stress',
        'result': 'passed',
        'recovery_time_sec': 15,
        'degradation_rate': 0.1,
        'timestamp': '2026-04-01T00:00:00Z',
    }

    with patch('runner.neptune_client.query_opencypher', side_effect=Exception("Neptune unavailable")):
        with pytest.raises(Exception, match="Neptune unavailable"):
            write_experiment(exp)


def test_ua2_05_missing_required_fields():
    """U-A2-05: experiment_id 或 target_service 缺失时，write_experiment 跳过写入（不抛异常）。"""
    from neptune_sync import write_experiment

    # 缺少 experiment_id
    try:
        write_experiment({'target_service': 'petsite'})
    except Exception as e:
        pytest.fail(f"write_experiment raised unexpectedly with missing experiment_id: {e}")

    # 缺少 target_service
    try:
        write_experiment({'experiment_id': 'test-auto-chaos-unit-noid'})
    except Exception as e:
        pytest.fail(f"write_experiment raised unexpectedly with missing target_service: {e}")


def test_ua2_06_neptune_sync_failure_non_fatal():
    """U-A2-06: Neptune 同步失败不影响调用者（模拟 runner.py 中的 try/except 保护）。"""
    # 验证 write_experiment 异常能被 try/except 捕获不传播
    from neptune_sync import write_experiment

    exp = {
        'experiment_id': 'test-auto-chaos-unit-nonfatal',
        'target_service': 'petsite',
        'fault_type': 'pod-kill',
        'result': 'passed',
        'recovery_time_sec': 20,
        'degradation_rate': 0.0,
        'timestamp': '2026-04-01T00:00:00Z',
    }

    neptune_failure = False
    try:
        with patch('runner.neptune_client.query_opencypher', side_effect=Exception("timeout")):
            write_experiment(exp)
    except Exception:
        neptune_failure = True

    # 验证：runner.py 中应包 try/except，这里验证异常可被捕获
    # (实际 runner.py 使用 try/except 包住 write_experiment 调用)
    assert neptune_failure, "Exception should be raised from write_experiment (caught in runner.py)"
