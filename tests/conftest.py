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

# ⚠️ 刻意**不**把 infra/lambda/etl_aws 放进全局 sys.path。
# 该目录是 Lambda 部署包,里面 vendored 了 5 个第三方包:
#     certifi/  charset_normalizer/  idna/  requests/  urllib3/
# 再加上 collectors/、config.py、handler.py 等同名可能性。
# 原先 conftest 在此无条件 insert 它,后果是**整个测试 session 用的是
# etl_aws 里那套 vendored 副本而不是已安装的版本** —— 实测警告来自
# infra/lambda/etl_aws/urllib3/connectionpool.py 而非 site-packages。
# 于是测试结果取决于部署包里冻结的版本,且 collectors 等同名模块被连带遮蔽。
#
# 需要 etl_aws 的只有 tests/test_12_unit_etl_aws.py,它自己会
# `sys.path.insert(0, ETL_PATH)`,所以去掉这里不影响它。
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


# === collectors 包遮蔽隔离 ===
# 仓库里有**两个**顶层名同为 `collectors` 的包:
#   rca/collectors/                    aws_probers / infra_collector / eks_auth / layer2_*
#   infra/lambda/etl_aws/collectors/   ec2 / eks / rds / alb / data_stores / lambda_sfn
#
# 生产上二者从不冲突 —— etl_aws 与 rca 跑在**不同的 Lambda** 里,不共处一个进程。
# 冲突只出现在测试套件这一个进程内:test_12_unit_etl_aws.py 在模块级(收集期)
# 把 etl_aws 顶到 sys.path[0] 并清掉 collectors* 缓存,之后整个 session 的
# sys.modules['collectors'] 都是 etl_aws 那个包。
#
# 于是所有需要 rca/collectors 的测试都失败,实测 16 个 error 加 test_11 的
# **假**"循环导入"告警。而 test_layer2_*.py 自己那句
#   for p in [_PROJECT, _RCA]:
#       if p not in sys.path: sys.path.insert(0, p)
# 救不了:守卫只检查**存在性**不检查**优先级** —— conftest 已经放过 rca,
# 条件为假,于是不会把它重新提前。
#
# 刻意**不**改生产代码的包名:为一个测试期问题去重命名 Lambda 部署包目录
# (还要重新部署三个 ETL)代价不对等。改为在此提供通用的 per-test 隔离:
# 每个测试前把 rca 提到 sys.path[0] 并清掉 collectors* 缓存,测试后还原。
#
# 对 test_12 安全:它在模块级就完成了 `from collectors.ec2 import ...`,
# 测试体内用的是已绑定的函数引用,不再走 import 机制。
_RCA_DIR = os.path.join(PROJECT_ROOT, 'rca')


# scope='module' 而非 'function':pytest **先实例化高作用域 fixture**,
# 而 test_layer2_golden.py 的 `engine` 是 scope='module' 的 —— function 作用域的
# 隔离会在它之后才跑,救不了它(实测 12 个 error 由此而来)。
# 同作用域内 autouse fixture 先于被显式请求的 fixture,故 module 作用域可行。
#
# 豁免名单:test_12_unit_etl_aws 测的**就是** etl_aws 那个 collectors 包,
# 它在测试体内还有惰性的 `collectors.eks` 等导入。强制它走 rca 会让 4 个测试
# 报 ModuleNotFoundError —— 隔离的目的是让两个包各得其所,不是让 rca 通吃。
_ETL_COLLECTORS_MODULES = {'test_12_unit_etl_aws'}


@pytest.fixture(autouse=True, scope='module')
def _isolate_collectors_package(request):
    """让 `collectors` 在每个测试**模块**中稳定解析到该模块需要的那一个。"""
    mod_name = getattr(request.module, '__name__', '')
    if mod_name.split('.')[-1] in _ETL_COLLECTORS_MODULES:
        # 该模块要 etl_aws 的 collectors,它自己在模块级已配好 sys.path,不干预
        yield
        return
    saved_path = list(sys.path)
    saved_mods = {k: v for k, v in sys.modules.items()
                  if k == 'collectors' or k.startswith('collectors.')}
    for k in saved_mods:
        del sys.modules[k]
    if _RCA_DIR in sys.path:
        sys.path.remove(_RCA_DIR)
    sys.path.insert(0, _RCA_DIR)
    try:
        yield
    finally:
        for k in [k for k in sys.modules
                  if k == 'collectors' or k.startswith('collectors.')]:
            del sys.modules[k]
        sys.modules.update(saved_mods)
        sys.path[:] = saved_path

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
