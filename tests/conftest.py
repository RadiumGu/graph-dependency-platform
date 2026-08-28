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


def cleanup_incident(neptune_client, incident_id: str) -> None:
    """删除测试产生的 Incident:**同时**清理 Neptune 节点与 S3 Vectors 条目。

    2026-08-28 新增。此前各测试只 `DETACH DELETE` Neptune 节点,不删向量 ——
    实测一天下来向量索引从基线 18 条涨到 **56 条**,38 条测试残留。
    而 search_similar 取 top_k,索引里塞满内容高度相似的测试 Incident 之后,
    刚写入的那条排不进 top_k,表现为"向量搜索找不到刚写的 Incident"。
    起初以为是最终一致性,实际是**索引污染**。

    tests/test_21_e2e_pipeline.py 更是**完全没有清理** —— 它 write_incident
    之后什么都不删,是节点残留的主要来源。

    两侧都用 try/except 吞掉异常:清理失败不该让测试本身变红,
    但会记 warning 以免无声堆积。
    """
    try:
        neptune_client.results(
            "MATCH (n:Incident {id: $id}) DETACH DELETE n", {'id': incident_id},
        )
    except Exception as e:
        logger.warning(f"清理 Incident 节点失败 {incident_id}: {e}")
    try:
        from search.incident_vectordb import delete_incident_vectors
        delete_incident_vectors(incident_id)
    except Exception as e:
        logger.warning(f"清理 Incident 向量失败 {incident_id}: {e}")


def now_iso(offset_sec: int = 0) -> str:
    """当前 UTC 时间的 ISO 串,供集成测试写 timestamp 用。

    集成测试原先把 ChaosExperiment 的 timestamp 写死为 '2026-04-01T...'。
    Q18 是 ``ORDER BY exp.timestamp DESC LIMIT $limit``,而 petsite 在活图里
    已累积 **24 个** ChaosExperiment,时间戳全部 ≥ 2026-04-02 —— 写死的旧
    时间戳排在第 25 位,被 LIMIT 20 切掉,测试恒红。

    实测确认过节点与 TestedBy 边都成功落库(`LIMIT 30` 即可查到),
    所以这不是写入缺陷,是**测试脆弱性**:它在图谱数据还少的时候写的,
    随着数据累积就静默失效。

    刚跑完的实验本来就该是最新的 —— 用当前时间才是语义正确的写法,
    且不受活图累积多少历史实验影响。
    """
    import datetime
    t = (datetime.datetime.now(datetime.timezone.utc)
         + datetime.timedelta(seconds=offset_sec))
    return t.strftime('%Y-%m-%dT%H:%M:%S.%f+00:00')


# scope='module' 而非 'function':pytest **先实例化高作用域 fixture**,
# 而 test_layer2_golden.py 的 `engine` 是 scope='module' 的 —— function 作用域的
# 隔离会在它之后才跑,救不了它(实测 12 个 error 由此而来)。
# 同作用域内 autouse fixture 先于被显式请求的 fixture,故 module 作用域可行。
#
_ETL_AWS_DIR = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'etl_aws')

# etl_aws 目录里 vendored 的第三方包。**不能**把它们纳入隔离集:
# 清掉 sys.modules 里的 requests/urllib3 只会让它们从 etl_aws 的 vendored 副本
# 重新导入,正是要避免的事(见上方移除全局 sys.path 注入的说明)。
_VENDORED = {'certifi', 'charset_normalizer', 'idna', 'requests', 'urllib3', 'bin'}


def _top_level_names(root: str) -> set:
    """列出某个组件根目录暴露的顶层模块名(第一方,排除 vendored 与 config)。"""
    if not os.path.isdir(root):
        return set()
    out = set()
    for e in os.listdir(root):
        if e.startswith('.') or e in ('__pycache__', '__init__.py'):
            continue
        if e.endswith('.dist-info'):
            continue
        name = e[:-3] if e.endswith('.py') else e
        if not e.endswith('.py') and not os.path.isdir(os.path.join(root, e)):
            continue  # 非 .py 文件(json/txt)
        if name in _VENDORED or name == 'config':
            # config 由 conftest 刻意合并成一个统一模块,清掉会破坏它
            continue
        out.add(name)
    return out


# 真正会互相遮蔽的名字 = 两个组件根都暴露的第一方顶层名。
# **算出来而不是手写**:实测碰撞不止 collectors —— 还有 handler
# (rca/handler.py vs infra/lambda/etl_aws/handler.py)。
# test_12 用 patch.object(h, 'upsert_vertex') 时拿到的是 rca 的 handler,
# 报 AttributeError: module 'handler' ... does not have the attribute 'upsert_vertex'。
# 先前两次只隔离 collectors 都没修好,就是因为漏了 handler。
_SHADOWED = _top_level_names(_RCA_DIR) & _top_level_names(_ETL_AWS_DIR)

# 需要 etl_aws 那一侧的测试模块 → 该模块应优先的组件根
_COLLECTORS_OWNER = {
    'test_12_unit_etl_aws': _ETL_AWS_DIR,
}


def _purge(names: set) -> dict:
    """把 names 及其子模块从 sys.modules 摘出来并返回,便于事后还原。"""
    saved = {}
    for k in list(sys.modules):
        head = k.split('.')[0]
        if head in names:
            saved[k] = sys.modules.pop(k)
    return saved


@pytest.fixture(autouse=True, scope='module')
def _isolate_shadowed_packages(request):
    """让互相遮蔽的顶层模块在每个测试**模块**中稳定解析到它需要的那一侧。

    必须 scope='module':pytest 先实例化高作用域 fixture,而
    test_layer2_golden.py 的 `engine` 就是 module 作用域 —— function 作用域的
    隔离在它之后才跑,救不了它(实测 12 个 error 由此而来)。
    """
    mod_name = getattr(request.module, '__name__', '').split('.')[-1]
    want = _COLLECTORS_OWNER.get(mod_name, _RCA_DIR)

    saved_path = list(sys.path)
    saved_mods = _purge(_SHADOWED)
    if want in sys.path:
        sys.path.remove(want)
    sys.path.insert(0, want)
    try:
        yield
    finally:
        _purge(_SHADOWED)
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
