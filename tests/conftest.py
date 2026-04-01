"""
conftest.py - 共享 fixtures 和路径配置

覆盖测试清单：共享基础设施，不对应具体用例编号
"""
import logging
import os
import sys

import pytest

logger = logging.getLogger(__name__)

# === Path setup ===
# Insert in reverse priority order: rca inserted last → ends up at sys.path[0] (highest priority).
PROJECT_ROOT = '/home/ubuntu/tech/graph-dependency-platform'
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'etl_aws'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'dr-plan-generator'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'chaos', 'code'))
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'rca'))  # highest priority

# === Unified config module ===
# Both rca/config.py and dr-plan-generator/config.py share the module name 'config'
# but have different attributes.  Build a merged module that satisfies both so that
# whichever package imports it first gets all attributes it needs.
import importlib.util
import types

_unified_config = types.ModuleType('config')
for _cfg_path in [
    os.path.join(PROJECT_ROOT, 'dr-plan-generator', 'config.py'),
    os.path.join(PROJECT_ROOT, 'rca', 'config.py'),
]:
    _spec = importlib.util.spec_from_file_location('_tmp_cfg', _cfg_path)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    for _attr in dir(_mod):
        if not _attr.startswith('_'):
            setattr(_unified_config, _attr, getattr(_mod, _attr))

sys.modules['config'] = _unified_config

# === Environment defaults ===
os.environ.setdefault('NEPTUNE_ENDPOINT', 'petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com')
os.environ.setdefault('REGION', 'ap-northeast-1')
os.environ.setdefault('NEPTUNE_PORT', '8182')

TEST_PREFIX = 'test-auto-'


@pytest.fixture(scope='session')
def neptune_rca():
    """rca 模块的 Neptune client (openCypher)。"""
    from neptune import neptune_client as nc
    result = nc.results("MATCH (n) RETURN count(n) AS cnt LIMIT 1")
    assert isinstance(result, list), "Neptune connection failed"
    return nc


@pytest.fixture(scope='session')
def neptune_dr():
    """dr-plan-generator 模块的 Neptune client。"""
    from graph import neptune_client
    return neptune_client


@pytest.fixture(scope='session')
def test_incident_id():
    return f'{TEST_PREFIX}incident-001'


@pytest.fixture(scope='session')
def test_experiment_id():
    return f'{TEST_PREFIX}chaos-001'


@pytest.fixture(scope='session', autouse=True)
def cleanup_test_data(neptune_rca):
    """测试结束后清理所有 test-auto- 前缀的测试数据。"""
    yield
    # Cleanup Neptune: test-auto- prefixed nodes
    try:
        neptune_rca.results(
            f"MATCH (n) WHERE n.id STARTS WITH '{TEST_PREFIX}' "
            f"OR n.experiment_id STARTS WITH '{TEST_PREFIX}' "
            f"DETACH DELETE n"
        )
        logger.info("Neptune test data (test-auto-) cleaned")
    except Exception as e:
        logger.warning(f"Neptune cleanup failed: {e}")

    # Cleanup S3 Vectors (best-effort)
    try:
        import boto3
        client = boto3.client('s3vectors', region_name='ap-northeast-1')
        resp = client.list_vectors(
            vectorBucketName='gp-incident-kb',
            indexName='incidents-v1',
        )
        test_keys = [
            v['key'] for v in resp.get('vectors', [])
            if v['key'].startswith(TEST_PREFIX)
        ]
        if test_keys:
            client.delete_vectors(
                vectorBucketName='gp-incident-kb',
                indexName='incidents-v1',
                keys=test_keys,
            )
            logger.info(f"S3 Vectors: cleaned {len(test_keys)} test vectors")
    except Exception as e:
        logger.warning(f"S3 Vectors cleanup failed: {e}")
