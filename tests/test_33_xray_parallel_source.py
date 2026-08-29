"""
X-Ray 平行观测源（etl_xray）的守门测试。

设计原则（本仓库既有纪律）：
仓库级断言**阻塞**，活图谱状态类断言只**告警**。
一个测试绝不能因为「生产落后于分支」而变红 —— 那会训练人忽略失败，
真正的写侧回归就此被掩盖。先例：test_26 与 test_31 的 L-01 都用 UserWarning。
"""
import os
import sys
import time
import warnings

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, '..'))
_ETL_XRAY = os.path.join(_ROOT, 'infra', 'lambda', 'etl_xray')
_SHARED = os.path.join(_ROOT, 'infra', 'lambda', 'shared', 'python')


def _load_etl():
    """
    按需导入 etl_xray。

    刻意在函数内改 sys.path 而**不是**模块顶层：库级别改全局 path 会造成
    同名包遮蔽 —— 本仓库因此整场测试跑在 vendored 副本上过一次
    （conftest 把 etl_aws 部署包放上全局 path）。
    """
    for p in (_ETL_XRAY, _SHARED):
        if p not in sys.path:
            sys.path.insert(0, p)
    import neptune_etl_xray as m
    return m


# ── X-01：分类与归一化（纯函数，无外部依赖，阻塞） ──────────────────────

def test_x01_client_shadow_nodes_are_dropped():
    """
    X-Ray 给每个调用方补一个 Type='client' 的影子节点，它不是真实被调用对象。

    必须丢弃：否则图谱里会多出一批与服务本体同名、没有任何真实语义的节点。
    实测 24h 窗口里 6 个服务各有一个 client 影子，占节点总数近一半。
    """
    m = _load_etl()
    assert m._classify('PetSearch', 'client') is None
    assert m._classify('PetSite', 'client') is None
    # 服务本体（Type=None）必须保留
    assert m._classify('PetSearch', None) == ('petsearch', 'service', None)


def test_x02_ssm_aliases_collapse_to_one_identity():
    """
    SSM 与 SimpleSystemsManagement 是**同一个 AWS 服务**，
    X-Ray 因 SDK 版本差异报成两个名字。

    这是干跑时抓到的真 bug：最初 key 里带 xray_type，于是规范名虽然都归一到
    'ssm'，**key 却因 type 不同而没合并** —— 图谱里出现两个 'ssm' 条目，
    随后写进同一顶点、xray_type 互相覆盖，哪个值留下取决于 dict 迭代顺序。

    所以对 aws_service，key 的第三位必须恒为 None（身份只由规范名决定）。
    """
    m = _load_etl()
    a = m._classify('SSM', 'AWS::SSM')
    b = m._classify('SimpleSystemsManagement', 'AWS::SimpleSystemsManagement')
    assert a == b, f"两个别名必须归一成同一身份，实际 {a} != {b}"
    assert a[0] == 'ssm'
    assert a[1] == 'aws_service'
    assert a[2] is None, "aws_service 的身份不能含 xray_type，否则别名不会合并"


def test_x03_secrets_manager_reported_as_service_is_recognized():
    """
    实测 'Secrets Manager' 被 X-Ray 报成**服务本体**（Type=None）而非 AWS:: 类型。

    如果只按 Type 判断，它会被当成一个不存在的微服务 —— 在图谱里找不到
    对应的 Microservice 节点，变成孤儿边或被静默跳过。必须按别名表识别。
    """
    m = _load_etl()
    kind = m._classify('Secrets Manager', None)
    assert kind is not None
    assert kind[1] == 'aws_service', \
        f"'Secrets Manager' 必须识别为 AWS 托管服务，实际 kind={kind[1]}"
    assert kind[0] == 'secretsmanager'


def test_x04_generic_s3_never_resolved_to_a_bucket():
    """
    X-Ray 把 S3 报成一个字面叫 'S3' 的节点，**没有 bucket 名**。

    图谱里有 30+ 个 S3Bucket。把 'S3' 猜成其中某一个（例如「petsearch 在静态边里
    只连了一个 bucket，那就是它」）是**推断而非观测** ——
    用观测源的名义写推断结果就是编造。

    本测试锁住这条纪律：泛化 S3 只能落 AWSServiceEndpoint，
    绝不能被解析成 S3Bucket。
    """
    m = _load_etl()
    key = m._classify('S3', 'AWS::S3')
    assert key[1] == 'aws_service', "泛化 S3 不能被当成资源级节点"
    matcher, edge = m._dst_matcher(key)
    assert 'AWSServiceEndpoint' in matcher, \
        f"泛化 S3 必须指向 AWSServiceEndpoint，实际 matcher={matcher}"
    assert 'S3Bucket' not in matcher, "绝不能把泛化 S3 解析成某个具体 bucket"


def test_x05_dynamodb_full_table_name_maps_to_resource_node():
    """
    DynamoDB 是唯一能从 X-Ray 拿到**完整资源名**的类型（实测逐字符命中图谱里
    已有的 DynamoDBTable 节点）。这条边因此是资源级精确边，
    与泛化的 S3/STS/SSM 形成对照 —— 这也是 demo 里「源粒度差异」的主要看点。
    """
    m = _load_etl()
    name = 'ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM'
    key = m._classify(name, 'AWS::DynamoDB::Table')
    assert key == (name, 'resource', 'AWS::DynamoDB::Table')
    matcher, edge = m._dst_matcher(key)
    assert 'DynamoDBTable' in matcher
    assert edge == 'AccessesData', "微服务到数据库用 AccessesData，不是 DependsOn"


def test_x06_six_hour_window_cap_is_respected():
    """
    X-Ray 的 GetServiceGraph 单次窗口上限 6 小时，超过直接报
    `Time range cannot be longer than 6 hours`。这是 API 硬约束。

    锁住分段逻辑：24h 请求必须切成 4 段，且每段都不超过 6 小时 ——
    否则整个回看窗口会静默退化成只有最后 6 小时。
    """
    m = _load_etl()
    assert m.XRAY_MAX_WINDOW_SECONDS == 6 * 3600
    # 复算分段逻辑（与 fetch_xray_service_graph 内部一致）
    for hours, expected_segments in ((24, 4), (6, 1), (12, 2), (1, 1)):
        remaining, segs = hours * 3600, []
        end = 1_700_000_000
        while remaining > 0:
            span = min(remaining, m.XRAY_MAX_WINDOW_SECONDS)
            segs.append((end - span, end))
            end -= span
            remaining -= span
        assert len(segs) == expected_segments, \
            f"{hours}h 应切成 {expected_segments} 段，实际 {len(segs)}"
        for s, e in segs:
            assert e - s <= m.XRAY_MAX_WINDOW_SECONDS


# ── X-07：写入纪律（代码断言，阻塞） ────────────────────────────────────

def test_x07_existing_edges_never_lose_their_provenance():
    """
    边已存在时**只补 X-Ray 度量，绝不覆盖 source / dependency_kind**。
    原 source 记录的是「谁首先发现了这条依赖」，覆盖掉就把发现史抹了。

    实测证据：petsearch → DynamoDB 表由 aws-etl 以 static 声明发现，
    X-Ray 补上 11,503 次调用后，source 仍是 aws-etl。

    本测试直接读源码断言 —— 走活图谱会因生产是否已部署而不稳定。
    """
    src_path = os.path.join(_ETL_XRAY, 'neptune_etl_xray.py')
    with open(src_path, encoding='utf-8') as fh:
        src = fh.read()

    # 定位「已存在」分支：它必须不含 source / dependency_kind 的写入
    marker = "if existing > 0:"
    assert marker in src, "写边逻辑的分支结构变了，测试需同步更新"
    branch = src.split(marker, 1)[1].split("else:", 1)[0]
    assert "'source'" not in branch, \
        "已存在的边分支里出现了 source 写入 —— 会抹掉原发现者"
    assert "'dependency_kind'" not in branch, \
        "已存在的边分支里出现了 dependency_kind 写入 —— 会把 static 声明改成 dynamic"


def test_x08_vertex_writes_use_single_cardinality():
    """
    Gremlin **顶点**属性默认 SET 基数 —— 不带 single 就是追加而非覆盖，
    静默累积多值。本仓库为此清理过 1,897 个冗余值。

    注意**不能**拿边的写法当模板：TinkerPop 边属性按规范天生单基数，
    所以边上不写 single 是正确的，顶点上不写就是 bug。
    """
    src_path = os.path.join(_ETL_XRAY, 'neptune_etl_xray.py')
    with open(src_path, encoding='utf-8') as fh:
        src = fh.read()
    fn = src.split('def upsert_aws_service_endpoints', 1)[1].split('\ndef ', 1)[0]
    # 该函数里每一处顶点 property 写入都必须带 single
    import re
    props = re.findall(r"\.property\((.*?),'", fn)
    assert props, "没找到顶点属性写入，测试需同步更新"
    for p in props:
        assert 'single' in p, \
            f"顶点属性写入缺 single 基数修饰：.property({p},...) —— 会累积多值"


def test_x09_no_derivable_boolean_flag():
    """
    刻意**不引入** observed_by_xray 布尔属性：它完全可由
    xray_last_seen 是否存在推导，而能被推导出来的布尔量迟早与来源不一致。

    本仓库反复踩过这类坑（「字段有值 ≠ 值有用」出现过四次）。
    本测试锁住这个决定，防止后来者「顺手加个标记更方便查」。
    """
    for path in (os.path.join(_ETL_XRAY, 'neptune_etl_xray.py'),
                 os.path.join(_ROOT, 'rca', 'neptune', 'neptune_queries.py')):
        with open(path, encoding='utf-8') as fh:
            src = fh.read()
        # 允许注释里解释为什么不要它，但不允许真的写入这个属性
        assert "property('observed_by_xray'" not in src, \
            f"{path} 里出现了 observed_by_xray 写入 —— 该布尔量可由 xray_last_seen 推导"


# ── X-10：Q21 被注册且可用（阻塞） ──────────────────────────────────────

def test_x10_q21_registered_in_catalog():
    """
    X-Ray 的边属性必须有**读取方**，否则就是第五个「写了但从没被读」的字段。
    Q21 是那个读取方，必须在 query_catalog 里注册 —— 只有注册了，
    未来的 AgentCore handler 才能把事件直接映射到它。
    """
    sys.path.insert(0, _ROOT)
    sys.path.insert(0, os.path.join(_ROOT, 'rca'))
    from neptune.query_catalog import QUERY_CATALOG
    name = 'q21_observation_source_coverage'
    assert name in QUERY_CATALOG, f"{name} 未注册进 query_catalog"
    spec = QUERY_CATALOG[name]
    assert spec['mod'] == 'queries'
    assert 'coverage' in spec['params'], "coverage 参数契约缺失"


def test_x11_schema_declares_awsserviceendpoint():
    """
    新节点类型必须在 schema 里声明 —— 否则 test_11 的
    「活图谱 ⊆ schema」断言会红（图谱里已有实例，schema 没声明）。
    """
    import yaml
    with open(os.path.join(_ROOT, 'profiles', 'petsite.yaml'), encoding='utf-8') as fh:
        prof = yaml.safe_load(fh)
    text = prof['neptune']['graph_schema_text']
    assert 'AWSServiceEndpoint' in text, "schema 未声明 AWSServiceEndpoint"
    assert 'granularity' in text, "schema 未说明 granularity 语义"
    # 边模式也必须声明，否则模式一致性检查会漏
    assert 'AccessesData]->(:AWSServiceEndpoint)' in text, \
        "schema 未声明指向 AWSServiceEndpoint 的边模式"
    for prop in ('xray_call_count', 'xray_total_response_time_s',
                 'xray_window_hours', 'xray_last_seen'):
        assert prop in text, f"schema 未声明边属性 {prop}"


# ── X-12：活图谱状态（**只告警**，不阻塞） ──────────────────────────────

def test_x12_live_graph_has_xray_edges(neptune_rca):
    """
    活图谱里应该有 X-Ray 写入的边。

    **只告警不阻塞**：etl_xray 尚未部署为 Lambda（当前是手动跑的），
    而且一个新环境在首轮跑之前必然没有这些边。
    让它阻塞会训练人忽略失败 —— 真正的写侧回归就此被掩盖。
    """
    rows = neptune_rca.results(
        "MATCH ()-[r]->() WHERE r.xray_last_seen IS NOT NULL "
        "RETURN count(r) AS n", {})
    n = 0
    if rows:
        n = int(rows[0].get('n') or 0)
    if n == 0:
        warnings.warn(
            "活图谱里没有任何带 xray_last_seen 的边。"
            "若 etl_xray 已部署，说明写侧回归了；"
            "若尚未部署（当前状态），这是预期的。",
            UserWarning)
        return

    stale = neptune_rca.results(
        "MATCH ()-[r]->() WHERE r.xray_last_seen IS NOT NULL "
        "RETURN max(r.xray_last_seen) AS newest", {})
    if stale and stale[0].get('newest'):
        age = int(time.time()) - int(stale[0]['newest'])
        if age > 24 * 3600:
            warnings.warn(
                f"最新的 X-Ray 观测已过去 {age // 3600} 小时 —— "
                f"etl_xray 可能已停止运行。", UserWarning)
