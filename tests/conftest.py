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
#
# PROJECT_ROOT 的推导集中在 tests/paths.py（单一来源），本文件与 13 个测试文件
# 都从那里取 —— 不再各自硬编码,也不各自复制推导逻辑。
# 背景见 tests/paths.py 的模块 docstring。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 让 paths 可导入
from paths import PROJECT_ROOT, assert_layout  # noqa: E402

assert_layout()  # 推导错了立刻失败，而不是让后续测试报一堆误导性的"文件不存在"

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

# === Unified neptune_client_base stub ===
# neptune_client_base 在生产中来自 Lambda Layer（挂在 /opt/python），测试环境没有,
# 所以每个 ETL 单测都得自己塞一个桩。原先是**三份各不相同的桩**:
#
#   test_12_unit_etl_aws.py   无条件覆盖   3 个属性（缺 REGION）
#   test_13_unit_etl_deepflow.py  if not in sys.modules   4 个属性
#   test_14_unit_etl_cfn.py       if not in sys.modules   4 个属性
#
# 后果:字母序 test_12 先执行,它无条件塞进缺 REGION 的桩;test_13/14 的条件判断
# 因此跳过,导入真实 ETL 模块时报
#   ImportError: cannot import name 'REGION' from 'neptune_client_base'
#
# 这个冲突长期被**掩盖**:test_12 在 `import moto` 处就失败(moto 未安装),
# 根本走不到塞桩那行。2026-08-28 按 requirements-dev.txt 补齐 moto 之后立即暴露
# —— 即"缺依赖"意外地维持了套件表面正常。属跨测试模块的全局状态泄漏。
#
# 修法:在此建立**唯一**的桩,按真实模块的完整公开面来写
# （infra/lambda/shared/python/neptune_client_base.py:22-65）。
# conftest 先于所有测试模块加载,故各测试文件里的 `if not in sys.modules` 判断
# 会一致地跳过,不再取决于收集顺序。
_nc_base = types.ModuleType('neptune_client_base')
_nc_base.NEPTUNE_ENDPOINT = os.environ.get('NEPTUNE_ENDPOINT', 'test-endpoint')
_nc_base.NEPTUNE_PORT = int(os.environ.get('NEPTUNE_PORT', '8182'))
_nc_base.REGION = os.environ.get('REGION', os.environ.get('AWS_DEFAULT_REGION', 'ap-northeast-1'))
_nc_base.neptune_query = lambda gremlin: {'result': {'data': {'@value': []}}}
_nc_base.safe_str = lambda s: str(s).replace("'", "\\'").replace('"', '\\"')[:128] if s is not None else ''
_nc_base.extract_value = lambda v: v.get('@value', v) if isinstance(v, dict) else v
sys.modules['neptune_client_base'] = _nc_base

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
