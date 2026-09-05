#!/usr/bin/env python3
"""一次性 bootstrap：从 profiles/petsite.yaml 的 graph_schema_text 生成
profiles/graph_contract.yaml 的骨架，并合并本文件内的人工标注。

为什么要有这个脚本而不是手写 YAML：
    33 个节点类型 + 26 个边类型 + 每条边的端点约束，手工转写必然出错。
    先程序化抽取（唯一真实来源是同一份 schema 文本），再合并标注，
    生成后由 tests/test_35_graph_contract.py 反向校验类型名集合一致。

生成之后 profiles/graph_contract.yaml **转为手工维护**：
    它与 graph_schema_text 是两份平级文档，互不生成 ——
    graph_contract.yaml 是机器权威（ETL 门禁读它），
    graph_schema_text 是人与 LLM 读的叙述。
    两者的类型名集合必须相同，由 test_35 强制；漂移即测试失败。

用法（只应在初次引入契约时运行一次）：
    python3 scripts/bootstrap_graph_contract.py --write
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
PROFILE = REPO / 'profiles' / 'petsite.yaml'
OUT = REPO / 'profiles' / 'graph_contract.yaml'

# ── 人工标注 ───────────────────────────────────────────────────────────────
#
# identity: 身份键属性名。选取原则是 **必须不可变** ——
#   身份键一变，mergeV 匹配不到旧节点就新建一个，同一实体在图里裂成两份。
#   实测后果见 infra/lambda/etl_aws/neptune_client.py:62-77（14 个 EC2Instance
#   里 4 个是重复实体）。
#
# immutable:
#   true            —— 该属性在资源生命周期内不可改（AWS 侧不支持改名，
#                      或它本身就是资源标识符）
#   lifetime        —— 在该对象的生命周期内不变，但对象重建后换新值
#                      （K8s Pod / Deployment 这类）
#   false           —— 可变。出现即为缺陷，必须给出 identity 覆盖。
#
# scope_note: 已知的身份限定不足（例如未按 namespace/cluster 限定，
#            跨命名空间同名对象会碰撞）。记录而不静默。
#
# preferred: 更稳健的身份键候选（通常是 ARN）。当前 identity 已不可变时
#           属于健壮性升级而非缺陷修复，故不强制。
NODE_ANNOTATIONS: dict[str, dict] = {
    # ── 基础设施层 ────────────────────────────────────────────────────
    'Region':            {'identity': 'name',        'immutable': True,
                          'note': 'region code，AWS 侧不可变'},
    'AvailabilityZone':  {'identity': 'name',        'immutable': True,
                          'note': 'az name，AWS 侧不可变'},
    'VPC':               {'identity': 'vpc_id',      'immutable': True,
                          'note': 'name 取自 Name 标签（collectors/ec2.py:76 '
                                  "tags.get('Name', v['VpcId'])）是可变的，"
                                  '必须以 vpc_id 为身份'},
    'Subnet':            {'identity': 'subnet_id',   'immutable': True,
                          'note': 'name 取自 Name 标签（collectors/ec2.py:56）'
                                  '是可变的，必须以 subnet_id 为身份'},
    'SecurityGroup':     {'identity': 'sg_id',       'immutable': True,
                          'note': 'GroupName 创建后不可改，但 sg_id 更稳且已在 dict 里'},

    # ── 计算层 ────────────────────────────────────────────────────────
    'EC2Instance':       {'identity': 'instance_id', 'immutable': True,
                          'note': 'name 取自 Name 标签，已于 2026-08-29 改以 instance_id 为身份'},
    'EKSCluster':        {'identity': 'name',        'immutable': True},
    'Namespace':         {'identity': 'name',        'immutable': True,
                          'scope_note': '未按 cluster 限定；多集群场景同名 namespace 会碰撞'},
    'Pod':               {'identity': 'name',        'immutable': 'lifetime',
                          'scope_note': '未按 namespace 限定；Pod 重建即换名，属预期'},
    'Microservice':      {'identity': 'name',        'immutable': True,
                          'note': '规范服务名，来自 service_mappings.json，是声明而非观测值'},
    'K8sService':        {'identity': 'name',        'immutable': 'lifetime',
                          'scope_note': '未按 namespace 限定'},
    'Deployment':        {'identity': 'name',        'immutable': 'lifetime',
                          'scope_note': '未按 namespace 限定'},
    'HPA':               {'identity': 'name',        'immutable': 'lifetime',
                          'scope_note': '未按 namespace 限定'},

    # ── 网络层 ────────────────────────────────────────────────────────
    'LoadBalancer':      {'identity': 'name',        'immutable': True,
                          'preferred': 'arn',
                          'note': 'ALB 创建后不可改名；dict 里已有 arn，可升级'},
    'TargetGroup':       {'identity': 'name',        'immutable': True,
                          'preferred': 'arn',
                          'note': 'TargetGroupName 在 AWS 侧创建后不可改，所以 name 是合法身份键。'
                                  'arn 更稳（跨账号/区域唯一），但**切换需要先回填** —— '
                                  '2026-08-30 活图谱审计：18 个现存 TargetGroup 节点'
                                  '全部没有 arn 属性，直接切会在首轮 ETL 造 18 个重复节点。'
                                  '现已把 arn 作为普通属性写入（此前图谱里根本没有该字段），'
                                  '待存量都带上 arn 后可用 infra/migrate_identity_keys.py 复核再切'},
    'ListenerRule':      {'identity': 'name',        'immutable': True,
                          'note': 'name 传入的就是 rule_arn（handler.py:273）'},

    # ── 数据层 ────────────────────────────────────────────────────────
    'RDSCluster':        {'identity': 'name',        'immutable': True,
                          'note': 'DBClusterIdentifier'},
    'RDSInstance':       {'identity': 'name',        'immutable': True,
                          'note': 'DBInstanceIdentifier'},
    'Database':          {'identity': 'name',        'immutable': True,
                          'note': '逻辑库名'},
    'DynamoDBTable':     {'identity': 'name',        'immutable': True, 'preferred': 'arn'},
    'NeptuneCluster':    {'identity': 'name',        'immutable': True},
    'NeptuneInstance':   {'identity': 'name',        'immutable': True},
    'S3Bucket':          {'identity': 'name',        'immutable': True,
                          'note': '桶名全局唯一且不可变'},

    # ── 消息层 ────────────────────────────────────────────────────────
    'SQSQueue':          {'identity': 'name',        'immutable': True, 'preferred': 'arn'},
    'SNSTopic':          {'identity': 'name',        'immutable': True, 'preferred': 'arn'},

    # ── Serverless ───────────────────────────────────────────────────
    'LambdaFunction':    {'identity': 'name',        'immutable': True, 'preferred': 'arn'},
    'StepFunction':      {'identity': 'name',        'immutable': True, 'preferred': 'arn'},

    # ── 镜像 / 业务 / 外部 ────────────────────────────────────────────
    'ECRRepository':     {'identity': 'name',        'immutable': True, 'preferred': 'arn'},
    'BusinessCapability': {'identity': 'name',       'immutable': True,
                           'note': '业务声明，来自 business_config.json'},
    'AWSServiceEndpoint': {'identity': 'name',       'immutable': True,
                           'note': '归一化后的服务名（ssm 与 SimpleSystemsManagement 已归一）'},

    # ── 运维层（由 rca / chaos / deepflow 写入，非四个采集 ETL）────────
    'Incident':          {'identity': 'id',            'immutable': True,
                          'writer': 'rca_window_flush'},
    'ChaosExperiment':   {'identity': 'experiment_id', 'immutable': True,
                          'writer': 'chaos'},
    'TopologyChange':    {'identity': 'change_id',     'immutable': True,
                          'writer': 'etl_deepflow',
                          'note': '追加式事件日志，稳态可 0 实例'},
}

# expires_seconds:
#   整数 —— 该类型的边在这么久未被任何源刷新后应被置 active=false（软删除）。
#           取值为「所有写入源中最长的观测窗口」，否则窗口较长的源会被误判失效。
#   null  —— 结构边，生命周期跟随两端节点（对应 Dynatrace 的 static edge
#           继承 node lifetime 语义），不独立过期。
#
# 为什么 TTL 必须按边类型声明而不是一个全局阈值：
#   「Pod 属于哪个 Node」与「服务 A 调用服务 B」的合理过期时间差两个数量级。
#   用一个全局判据必然一头过激一头迟钝。这是 New Relic 关系 expires
#   （默认 PT75M，允许 10min–72h，每类关系各自声明）的同一思路。
EDGE_ANNOTATIONS: dict[str, dict] = {
    'Calls':          {'dependency': True,  'expires_seconds': 1800,
                       'retention_seconds': 604800,
                       'note': '唯一写入源是 deepflow，阈值同 CALLS_INACTIVE_AFTER_SECONDS'},
    'AccessesData':   {'dependency': True,  'expires_seconds': 21600,
                       'note': '四个源都写（aws/cfn 静态，deepflow 动态，xray 补度量）；'
                               '取最长源窗口 = xray 的 6h，否则会误杀 xray 发现的边'},
    'DependsOn':      {'dependency': True,  'expires_seconds': 21600,
                       'note': 'aws + cfn 静态 + deepflow 动态'},
    'PublishesTo':    {'dependency': False, 'expires_seconds': 21600,
                       'note': '受 drift 对账覆盖'},
    'InvokesVia':     {'dependency': False, 'expires_seconds': 21600,
                       'note': '受 drift 对账覆盖'},
    'ConnectsTo':     {'dependency': False, 'expires_seconds': None},
    'Contains':       {'dependency': False, 'expires_seconds': None},
    'LocatedIn':      {'dependency': False, 'expires_seconds': None},
    'BelongsTo':      {'dependency': False, 'expires_seconds': None},
    'OwnedBy':        {'dependency': False, 'expires_seconds': None},
    'HasSG':          {'dependency': False, 'expires_seconds': None},
    'ProtectsAccess': {'dependency': False, 'expires_seconds': None},
    'RunsOn':         {'dependency': False, 'expires_seconds': None},
    'Implements':     {'dependency': False, 'expires_seconds': None},
    'Routes':         {'dependency': False, 'expires_seconds': None},
    'Manages':        {'dependency': False, 'expires_seconds': None},
    'HasRule':        {'dependency': False, 'expires_seconds': None},
    'ForwardsTo':     {'dependency': False, 'expires_seconds': None},
    'RoutesTo':       {'dependency': False, 'expires_seconds': None},
    'WritesTo':       {'dependency': False, 'expires_seconds': None},
    'Invokes':        {'dependency': False, 'expires_seconds': None},
    'TriggeredBy':    {'dependency': False, 'expires_seconds': None},
    'TestedBy':       {'dependency': False, 'expires_seconds': None},
    'AffectedService': {'dependency': False, 'expires_seconds': None},
    'Involves':       {'dependency': False, 'expires_seconds': None},
    'MentionsResource': {'dependency': False, 'expires_seconds': None},
}

# graph_schema_text 里 WritesTo 的 dst 写成了 S3 / SNS / SQS —— 这三个不是
# 节点类型名。真实类型是 S3Bucket / SNSTopic / SQSQueue。契约里存正确的，
# 并由 test_35 断言契约端点全部是已声明的节点类型（该断言会持续拦住这类笔误）。
ENDPOINT_FIXUPS = {
    'WritesTo': {'dst': {'S3': 'S3Bucket', 'SNS': 'SNSTopic', 'SQS': 'SQSQueue'}},
}

# 2026-08-30：对活图谱做三元组普查（1740 条边 / 85 种形态）后，确认下列形态
# **真实存在且语义正确**，只是 graph_schema_text 从未声明它们。逐条给出依据。
#
# 这是 Mystery Machine（OSDI'14）的做法反过来用：先假设观测到的边都成立，
# 再逐条证伪；剩下证伪不掉的补进声明。凭空补声明会把真缺陷一起合法化 ——
# 同一次普查里另外 183 条就是真缺陷（find_vertex_by_name 不带标签所致），
# 那些**不补**，改代码 + 清数据。
PAIR_ADDITIONS = {
    'Manages': [
        # handler.py:906 T8g 明确写 HPA → Deployment，语义正确，schema 漏声明
        ('HPA', 'Deployment'),
    ],
    'AccessesData': [
        # RDSInstance 是已声明节点类型；schema 只写了 RDSCluster
        ('Microservice', 'RDSInstance'),
        # Step Functions 调 Lambda，cfn 模板里的真实依赖
        ('StepFunction', 'LambdaFunction'),
        # 本项目自己的 ETL Lambda 写 Neptune —— 图谱在描述自己
        ('LambdaFunction', 'NeptuneCluster'),
        # Lambda 读写 S3。平铺白名单下这条是「合规」的（LambdaFunction 在 src、
        # S3Bucket 在 dst），改成配对校验后才暴露它从未被显式声明 ——
        # 这正是收紧端点约束的预期代价：被笛卡尔积掩盖的合法组合会浮出来。
        ('LambdaFunction', 'S3Bucket'),
    ],
    'Calls': [
        # Lambda 直接调 Lambda；schema 只写了 Microservice→Microservice
        ('LambdaFunction', 'LambdaFunction'),
    ],
}


def parse_schema_text(txt: str) -> tuple[list[str], dict]:
    """从 graph_schema_text 抽出节点类型与边类型（含端点约束）。

    注意字符类必须含 0-9 —— EC2Instance / K8sService / S3Bucket 都带数字，
    漏掉会让整行不匹配（实测会把 26 种边少解析成 23 种）。

    ## 为什么记 pairs 而不只记 src/dst 两个集合

    schema 文本是 `(:A)-[:X]->(:B|:C)` 形式，**配对信息在解析时天然就有**。
    原实现把它拆成 src={A} / dst={B,C} 两个平铺集合，于是校验退化成笛卡尔积：
      - `Manages` src={Deployment} dst={Microservice,Pod}，加进真实存在的
        HPA→Deployment 后就变成 src={Deployment,HPA} dst={Deployment,...}，
        连 **Deployment→Deployment**（本次查出的真缺陷之一）都会被放行。
      - `RunsOn` src={Microservice,Pod} dst={EC2Instance,Pod}，平铺白名单
        已经在放行从未声明的 Microservice→EC2Instance 和 Pod→Pod。
    保留 pairs 才能让端点约束真正有分辨力。src/dst 仍然生成，作为兼容字段
    与「只知道一端」时的宽松校验。
    """
    lines = txt.splitlines()
    ni = next(i for i, l in enumerate(lines) if l.strip().startswith('## 节点类型'))
    ei = next(i for i, l in enumerate(lines) if l.strip().startswith('## 边类型'))

    nodes: list[str] = []
    for l in lines[ni:ei]:
        m = re.match(r'^\s{0,6}-\s+([A-Z][A-Za-z0-9]*)\s*:', l)
        if m and m.group(1) not in nodes:
            nodes.append(m.group(1))

    pat = re.compile(r'\(:([A-Za-z0-9|:]+)\)-\[:([A-Za-z0-9|:]+)\]->\(:([A-Za-z0-9|:]+)\)')
    edges: dict[str, dict] = collections.defaultdict(
        lambda: {'src': set(), 'dst': set(), 'pairs': set()})
    for m in pat.finditer(txt):
        srcs = [s for s in m.group(1).split('|:') if s]
        labs = [s for s in m.group(2).split('|:') if s]
        dsts = [s for s in m.group(3).split('|:') if s]
        for lb in labs:
            edges[lb]['src'].update(srcs)
            edges[lb]['dst'].update(dsts)
            edges[lb]['pairs'].update((s, d) for s in srcs for d in dsts)
    return nodes, edges


def build(nodes: list[str], edges: dict) -> dict:
    missing_n = [n for n in nodes if n not in NODE_ANNOTATIONS]
    missing_e = [e for e in edges if e not in EDGE_ANNOTATIONS]
    if missing_n or missing_e:
        sys.exit(f"标注缺失 —— 节点: {missing_n}  边: {missing_e}")

    node_types = {}
    for n in nodes:
        a = NODE_ANNOTATIONS[n]
        entry = {'identity': a['identity'], 'immutable': a['immutable']}
        for k in ('preferred', 'scope_note', 'note', 'writer'):
            if a.get(k):
                entry[k] = a[k]
        node_types[n] = entry

    edge_types = {}
    for e in sorted(edges):
        a = EDGE_ANNOTATIONS[e]
        fix = ENDPOINT_FIXUPS.get(e, {})
        _fs = lambda s: fix.get('src', {}).get(s, s)   # noqa: E731
        _fd = lambda s: fix.get('dst', {}).get(s, s)   # noqa: E731
        pairs = {(_fs(s), _fd(d)) for s, d in edges[e]['pairs']}
        pairs |= set(PAIR_ADDITIONS.get(e, ()))
        src = sorted({s for s, _ in pairs})
        dst = sorted({d for _, d in pairs})
        entry = {
            'src': src,
            'dst': dst,
            'pairs': [list(p) for p in sorted(pairs)],
            'dependency': a['dependency'],
            'expires_seconds': a['expires_seconds'],
        }
        if a.get('retention_seconds'):
            entry['retention_seconds'] = a['retention_seconds']
        if a.get('note'):
            entry['note'] = a['note']
        edge_types[e] = entry

    return {
        'version': 1,
        'timestamp_field': 'last_seen',
        'timestamp_legacy_aliases': ['last_updated', 'last_scanned'],
        'sources': ['aws-etl', 'cfn-etl', 'deepflow-etl', 'deepflow-l4', 'deepflow-dns',
                    'nfm', 'xray', 'business-layer', 'manual-fix'],
        'edge_write_once_attrs': ['source', 'dependency_kind', 'first_seen'],
        'node_attr_authority': {
            'Microservice': {
                'az': ['aws-etl'],
                'fault_boundary': ['aws-etl'],
                'recovery_priority': ['aws-etl', 'business-layer'],
            },
        },
        'node_types': node_types,
        'edge_types': edge_types,
        'edge_verification': EDGE_VERIFICATION,
    }


# ── 依赖边验证与置信度 ────────────────────────────────────────────────────
#
# 为什么放进契约而不是写死在 chaos 模块里：判据必须与写入门禁共用同一份声明，
# 否则「谁能写 verify_* 属性」「confirmed 的阈值是多少」会各处一份、悄悄漂移。
#
# ## 证据分层的依据
#
# 贝叶斯结构学习里，把每次故障注入当作一次**干预（intervention）**，对边做后验
# 更新（Hybrid Bayesian network discovery with latent variables by scoring
# multiple interventions, DMKD 2022, arXiv:2112.10574）。三层证据权重不同：
#
#   静态声明（aws-etl / cfn-etl）  = 先验。强，但可能过期。
#   观测（deepflow / xray）        = 似然。**必须封顶** —— How Does Bayesian
#                                    Causal Discovery Fail?(arXiv:2607.09449)
#                                    推出临界阈值：样本越多越容易被虚假相关性
#                                    诱导出假边。所以纯观测频次不能无限累加置信度，
#                                    否则跑得越久假边越"可信"。
#   干预（chaos 注入）            = 后验更新，权重最高，**可以翻转先验**。
#                                    这是唯一能证伪一条边的证据。
#
# ## 为什么 refuted 的阈值不是 confirmed 阈值的补集
#
# 中间带（5%~20% 退化）刻意判 inconclusive 而不是二分。原因是重试 / 熔断 /
# 缓存 / 连接池会掩盖真实依赖（Circuit Breaker Pattern, Azure Architecture
# Center；ICSA'20 对 Retry/Fail-Fast/Circuit-Breaker 如何改变可观测失败传播的
# 系统讨论）—— 一条真实存在的边完全可能只表现出轻微退化。把轻微退化判成
# "边不存在"会**删掉真实依赖**，比留着未验证的边有害得多。
#
# ## 为什么要 min_observation_requests
#
# chaos/code/runner/metrics.py:collect() 在无数据时 fallback 返回
# success_rate=100.0 / total_requests=0 —— **零流量与健康长得完全一样**。
# 不设请求量下限，一条没有流量的边会被判成 refuted（假阴性）。
EDGE_VERIFICATION = {
    'attrs': [
        'verify_status',        # untested | confirmed | refuted | inconclusive
        'verify_confidence',    # 0.0 ~ 1.0，证据 log-odds 经 sigmoid
        'verify_last',          # ISO8601，最近一次验证时刻
        'verify_by',            # 做出该判定的实验来源
        'verify_experiment',    # experiment_id，可追溯到具体实验
        'verify_degradation',   # 该次验证观测到的观测方退化率(%)
    ],
    # 只有混沌运行器可写这些属性。ETL 不得写 —— 否则静态采集会覆盖干预证据，
    # 等于用先验抹掉后验。
    'authority': ['chaos-runner'],
    'statuses': ['untested', 'confirmed', 'refuted', 'inconclusive'],
    'evidence_weights': {
        'static_declaration': 1.0,
        'observed_per_source': 0.5,
        'observed_cap': 1.5,          # 观测证据总量封顶，防假边随数据量膨胀
        # 不变量：|intervention| 必须 **大于可达到的最大先验**
        #   max_prior = static_declaration*2 + observed_cap = 2.0 + 1.5 = 3.5
        # 否则就存在「任何单次实验都无法证伪」的边 —— 而「唯一能证伪自己那张图」
        # 正是这套机制的立足点。设 3.0 时双静态+双观测的边一次证伪后恰好落在
        # sigmoid(0)=0.5，翻不过去（tests/test_37 的 v02 抓到过）。
        'intervention_confirmed': 4.0,
        'intervention_refuted': -4.0,
    },
    'thresholds': {
        'min_observation_requests': 20,     # 观测方基线与注入期各自的最小请求数
        'confirm_degradation_pct': 20.0,    # 观测方退化 >= 此值 -> confirmed
        'refute_degradation_pct': 5.0,      # 观测方退化 <= 此值 -> refuted
        # 观测方退化 >= 此值 -> hard dependency（Google SRE 三级分类）。
        # 70 不是凭空定的：项目护栏把 success_rate < 30% 当作「已经坏了」
        # （见各实验规格 stop_conditions），退化 >= 70pp 即调用方跌破自身那条线。
        'hard_degradation_pct': 70.0,
        'stale_verification_seconds': 2592000,  # 30 天未复验即视为过期
    },
}


HEADER = """# 图谱契约 —— 机器可读的权威声明
#
# 这份文件是 **ETL 写入门禁读取的权威**。profiles/petsite.yaml 的
# neptune.graph_schema_text 是给人和 LLM 读的叙述，两者平级、互不生成。
# 类型名集合必须完全相同，由 tests/test_35_graph_contract.py 强制；
# 漂移即测试失败。这样就不会再出现「声明说 33 种、代码里写进去 34 种」。
#
# 为什么需要它：在引入本文件之前，四个写入 ETL 无一 import profiles，
# 运行时对节点/边类型零校验 —— 任何拼错或新造的标签都会被静默写进 Neptune。
# 参见 todo/graph-correctness-audit-and-gaps_20260830-1435.md 缺口 1。
#
# 由 scripts/bootstrap_graph_contract.py 初次生成，此后手工维护。
# Lambda 层产物 infra/lambda/shared/python/graph_contract.py
# 由 scripts/gen_graph_contract.py 从本文件生成（test_35 校验未过期）。
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true')
    args = ap.parse_args()

    prof = yaml.safe_load(PROFILE.read_text())
    nodes, edges = parse_schema_text(prof['neptune']['graph_schema_text'])
    print(f"解析: {len(nodes)} 节点类型, {len(edges)} 边类型")

    contract = build(nodes, edges)
    body = HEADER + yaml.safe_dump(contract, allow_unicode=True, sort_keys=False,
                                   default_flow_style=False, width=100)
    if args.write:
        OUT.write_text(body)
        print(f"已写 {OUT}  ({len(body.splitlines())} 行)")
    else:
        print(body[:1500])
        print(f"... (dry-run，加 --write 落盘；共 {len(body.splitlines())} 行)")


if __name__ == '__main__':
    main()
