"""
neptune_etl_xray —— 把 AWS X-Ray 的服务图作为**与 DeepFlow 平行的观测源**写进 Neptune。

## 为什么是独立 ETL，而不是继续塞进 etl_deepflow

etl_deepflow 里已经有 fetch_xray_dependencies()，但那是**读用途**：
它把 X-Ray 当作漂移判定的第二证据源，只翻已存在边的 verified_by，
**不产生任何自己的边**。

本模块是**写用途**：X-Ray 观测到的拓扑本身进图谱。做成独立 Lambda 的理由：

1. 「两个独立观测源」这件事必须在架构上真独立。如果两者同一个 Lambda，
   一个 bug 同时打掉两边，图谱里「双源印证」的展示就是假的。
2. 失败模式与节奏都不同 —— X-Ray 的 GetServiceGraph 单次窗口上限 6 小时
   （超过报 `Time range cannot be longer than 6 hours`，必须分段合并），
   而 DeepFlow 的 DNS 观测是 30 分钟滑窗。
3. 权限面不同：本模块只需 xray:BatchGetTraces / GetServiceGraph，
   不碰 ClickHouse、不碰 EKS token。

## 与既有边的关系：补充证据，不抢 provenance

对每条 X-Ray 观测到的依赖：
  · 边**已存在**（无论谁发现的）→ 只补 X-Ray 的度量属性，
    **绝不覆盖 source/dependency_kind**。原 source 记录的是「谁первый发现了它」，
    覆盖掉就把发现史抹了。
  · 边**不存在** → 新建，source='xray'。这些才是 X-Ray 自己的贡献。

刻意**不引入** observed_by_xray 布尔属性：它完全可由 xray_last_seen IS NOT NULL
推导。一个能被另一个属性推导出来的布尔量，迟早会与它的来源不一致 ——
这正是本仓库反复踩过的坑（见 profiles/petsite.yaml 里 verified_by 的注释）。

## 粒度诚实性：X-Ray 的 S3 节点不是一个 bucket

X-Ray 的服务图把 S3 报成一个**字面叫 `S3` 的节点**，没有 bucket 名；
STS / SSM / Secrets Manager 同理。图谱里有 30 多个 S3Bucket，
把 `S3` 猜成其中某一个（比如「petsearch 在静态边里只连了一个 bucket」）
是**推断，不是观测** —— 用观测源的名义写推断结果就是编造。

所以这些粗粒度身份落到一个新节点类型 AWSServiceEndpoint，
granularity='service'，与资源级节点（granularity 概念上为 'resource'）明确区分。
DynamoDB 是唯一例外：X-Ray 给的是**完整表名**，逐字符命中图谱里已有的
DynamoDBTable 节点，所以那条边是资源级精确边。

顺带这也变成一个很好的 demo 对照：
  X-Ray 说  petsearch → S3（服务级）
  aws-etl 说 petsearch → serviceseks2-s3bucketpetadoptioncb20dce5-...（资源级）
同一依赖被两个源以不同粒度看到，正是「图谱作为多观测源对账中心」的价值。

## 名字归一化

实测（2026-08-29）X-Ray 的服务名经 lower() 即可命中图谱的 Microservice.name：
  PetSearch → petsearch    PetSite → petsite
  payforadoption / petlistadoptions 本就小写一致
无需任何逐名特例。

两处必须归一的别名：
  · SSM 与 SimpleSystemsManagement 是**同一个 AWS 服务**，
    X-Ray 因 SDK 版本差异报成两个名字。归一到 ssm，原始名记进 xray_aliases。
  · PetSite 与 petsite 是同一应用的两个上报名（后者是 collector 侧
    resource processor 设的 service.name）。lower() 天然合并。
"""

import os
import sys
import time
import logging
from collections import defaultdict

import boto3

# Lambda 运行时里 shared 层挂在 /opt/python，本地运行时由 --selftest 自行安排。
# 与 query_catalog.py 同样的纪律：**import 时不改全局 sys.path**。
from neptune_client_base import neptune_query, extract_value, REGION  # noqa: F401

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ── X-Ray 自埋点 ────────────────────────────────────────────────────────
# 为什么需要它：给 Lambda 开 `TracingConfig.Mode=Active` **只让 Lambda 服务端
# 记录调用**，实测结果是 X-Ray 服务图里出现了 Lambda 节点，但**一条出边都没有**
# —— 函数对下游（Neptune / X-Ray API / DynamoDB）的调用不产生子段。
#
# patch_all() 会包装 botocore 与 requests，让每次下游调用变成一个子段，
# 于是 `neptune-etl-from-xray → Neptune` 这类依赖才会出现在服务图里。
# 这是把 declared_only 里那批「声明了但没有任何观测源看得到」的 Lambda 依赖边
# 变成可印证的唯一低成本途径（对比：引入 Pyroscope 完全不解决依赖可见性）。
#
# context_missing='LOG_ERROR' 是必须的：模块 import 发生在 handler 之外，
# 此时还没有活动 segment，默认行为会抛 SegmentNotFoundException 直接打挂函数。
_XRAY_PATCHED = False
try:
    from aws_xray_sdk.core import xray_recorder, patch_all  # noqa: E402

    xray_recorder.configure(context_missing='LOG_ERROR')
    patch_all()
    _XRAY_PATCHED = True
except Exception as _exc:  # noqa: BLE001
    # 缺 SDK（层未升级）不该让 ETL 失败 —— 退化为只有 Lambda 节点没有出边，
    # 与开启自埋点之前的行为一致。但要打日志，否则「以为埋点生效了」比没埋更糟。
    logging.getLogger().warning(
        "X-Ray 自埋点未启用（%s）—— 本函数对下游的调用不会出现在 X-Ray 服务图里。"
        "需要 neptune-client-base:5 或更高版本的层提供 aws_xray_sdk。", _exc)

# ── 配置 ────────────────────────────────────────────────────────────────
# X-Ray 的 GetServiceGraph 单次调用窗口上限 6 小时，超过直接报错。
# 想要 24 小时视图必须分段调用再合并，这是 API 的硬约束，不是可调参数。
XRAY_MAX_WINDOW_SECONDS = 6 * 3600
XRAY_LOOKBACK_HOURS = int(os.environ.get('XRAY_LOOKBACK_HOURS', '24'))

# 边被认为「仍然活跃」的阈值。X-Ray 的窗口本身就是回看窗口，
# 所以这里只用于把久未观测的边置 active=false，与 etl_deepflow 的语义保持一致。
XRAY_STALE_SECONDS = int(os.environ.get('XRAY_STALE_SECONDS', str(6 * 3600)))

# ── 名字归一化 ──────────────────────────────────────────────────────────
# K8s 部署名 → 图谱 Microservice 名的映射，**复用 etl_deepflow 已有的
# service_mappings.json 的 k8s_alias**，不另造第二套。
#
# 为什么必须映射：X-Ray 会把**同一条依赖**用两种身份报两次 ——
#   PetSite → PetSearch                                    （两端都插桩，按服务名）
#   petsite → search-service.petadoptions.svc.cluster.local（Type=remote，按主机名）
# 而图谱里那个服务叫 `petsearch`，**没有** `search-service` 这个节点。
# 不映射的话第二条边永远写不进去，而且 edges_created 会每轮谎报一次成功。
#
# 两套映射是本仓库反复出问题的模式（「两个实现掩盖同一个缺陷」），
# 所以这里读同一个文件；文件缺失时退化为空表并告警，而不是内联一份副本。
def _load_k8s_alias() -> dict:
    import json as _json
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, 'service_mappings.json'),
        os.path.join(here, '..', 'etl_deepflow', 'service_mappings.json'),
    ):
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding='utf-8') as fh:
                    return _json.load(fh).get('k8s_alias', {}) or {}
            except Exception as exc:  # noqa: BLE001
                logger.warning("读 service_mappings.json 失败: %s", exc)
                return {}
    logger.warning(
        "找不到 service_mappings.json —— K8s 部署名不会被映射成图谱服务名，"
        "Type=remote 的主机名边会落不进图谱。部署时需把该文件打进包里。")
    return {}


K8S_ALIAS = _load_k8s_alias()

# X-Ray 的粗粒度 AWS 服务身份 → 规范名。
# key 用 lower() 后的原始名，避免大小写分支。
XRAY_SERVICE_ALIASES = {
    's3': 's3',
    'sts': 'sts',
    'ssm': 'ssm',
    'simplesystemsmanagement': 'ssm',   # 同一服务的另一种 SDK 命名
    'secrets manager': 'secretsmanager',
    'secretsmanager': 'secretsmanager',
    'dynamodb': 'dynamodb',
    'sqs': 'sqs',
    'sns': 'sns',
    'kinesis': 'kinesis',
    'lambda': 'lambda',
    # 开启 Lambda 自埋点后实测出现的 AWS::xray —— ETL 自己调 X-Ray API。
    # 这是真实依赖（etl_xray 读服务图），不是噪音，所以显式登记而不是靠兜底。
    'xray': 'xray',
    'cloudwatch': 'cloudwatch',
    'monitoring': 'cloudwatch',      # CloudWatch 的另一种上报名
    'logs': 'cloudwatchlogs',
}

# X-Ray 的 Type → 图谱里已存在的**资源级**节点类型。
# 只有能从 X-Ray 拿到完整资源名的类型才配列在这里 ——
# 拿不到资源名就只能落 AWSServiceEndpoint，不许猜。
XRAY_TYPE_TO_RESOURCE_NODE = {
    'AWS::DynamoDB::Table': 'DynamoDBTable',
    'AWS::SQS::Queue': 'SQSQueue',
    'AWS::SNS::Topic': 'SNSTopic',
    'AWS::S3::Bucket': 'S3Bucket',       # 注意：X-Ray 通常报泛化的 'S3'，
                                          # 只有少数 SDK 会给出这个带 bucket 的 Type
    'AWS::Lambda::Function': 'LambdaFunction',
    # X-Ray 用**两个条目**表示同一个 Lambda：AWS::Lambda（服务容器，代表
    # Lambda 服务收到的调用）+ AWS::Lambda::Function（函数本身的执行）。
    # 两者的 Name 都是**具体函数名**，所以都按资源级映射到同一个
    # LambdaFunction 节点 —— 边写入是幂等 upsert，两个条目合并成一条边。
    # 2026-08-29 给 10 个 Lambda 开启 Active 追踪后才出现这个 type。
    'AWS::Lambda': 'LambdaFunction',
}

# 边类型选择：目标是数据/存储类 → AccessesData；目标是服务 → Calls。
# 与 schema 里既有的约定一致（微服务到数据库用 AccessesData，不是 DependsOn）。
RESOURCE_NODE_TO_EDGE = {
    'DynamoDBTable': 'AccessesData',
    'S3Bucket': 'AccessesData',
    'RDSCluster': 'AccessesData',
    'SQSQueue': 'DependsOn',
    'SNSTopic': 'PublishesTo',
    'LambdaFunction': 'AccessesData',
}


def safe_str(s) -> str:
    """Gremlin 字符串字面量转义。与 etl_deepflow.safe_str 同语义。"""
    if s is None:
        return ''
    return str(s).replace('\\', '\\\\').replace("'", "\\'")


def fetch_xray_service_graph(lookback_hours: int = None) -> dict:
    """
    拉取 X-Ray 服务图并合并多段窗口。

    返回 {'nodes': {(canonical_name, kind, xray_type): {...}},
          'edges': {(src_canonical, dst_canonical): {...}}}

    kind ∈ {'service', 'resource', 'aws_service'}：
      service     —— 被插桩的应用（对应图谱 Microservice）
      resource    —— 能拿到完整资源名的 AWS 资源（对应已有资源节点类型）
      aws_service —— 只有粗粒度服务名（落 AWSServiceEndpoint）
    """
    hours = lookback_hours if lookback_hours is not None else XRAY_LOOKBACK_HOURS
    xray = boto3.client('xray', region_name=REGION)

    now = int(time.time())
    segments = []
    remaining = hours * 3600
    end = now
    while remaining > 0:
        span = min(remaining, XRAY_MAX_WINDOW_SECONDS)
        segments.append((end - span, end))
        end -= span
        remaining -= span

    nodes = {}
    edges = {}
    # 被 _classify 跳过的 (type, name)。**刻意统计而不是静默丢弃** ——
    # 前两次分类缺陷都是因为兜底猜了一个节点类型；现在改成跳过，
    # 但如果不把跳过的东西暴露出来，就变成了另一种静默失败。
    unmapped = set()

    for start_ts, end_ts in segments:
        try:
            raw_services = []
            paginator = xray.get_paginator('get_service_graph')
            for page in paginator.paginate(
                StartTime=start_ts, EndTime=end_ts
            ):
                raw_services.extend(page.get('Services', []) or [])
        except Exception as exc:  # noqa: BLE001
            # 单段失败不应让整轮 ETL 失败 —— 合并语义下少一段只是覆盖面变窄，
            # 而抛出去会让**已经拿到的**其他段也白跑。
            logger.warning("X-Ray GetServiceGraph 段 [%s,%s] 失败: %s",
                           start_ts, end_ts, exc)
            continue

        by_ref = {s.get('ReferenceId'): s for s in raw_services}

        for svc in raw_services:
            raw_name = svc.get('Name')
            xray_type = svc.get('Type')
            if not raw_name:
                continue

            # Type='client' 的条目是 X-Ray 给调用方补的影子节点，
            # 不是一个真实存在的被调用对象。跳过 —— 否则图谱里会多出
            # 一批与服务本体同名、没有任何真实语义的节点。
            if xray_type == 'client':
                continue

            key = _classify(raw_name, xray_type)
            if key is None:
                if xray_type != 'client':
                    unmapped.add((str(xray_type), raw_name))
                continue

            stats = svc.get('SummaryStatistics') or {}
            entry = nodes.setdefault(key, {
                'raw_names': set(),
                'xray_types': set(),
                'call_count': 0,
                'error_count': 0,
                'fault_count': 0,
                'total_response_time': 0.0,
            })
            entry['raw_names'].add(raw_name)
            if xray_type:
                entry['xray_types'].add(xray_type)
            entry['call_count'] += stats.get('OkCount') or 0
            entry['error_count'] += (stats.get('ErrorStatistics') or {}).get('TotalCount') or 0
            entry['fault_count'] += (stats.get('FaultStatistics') or {}).get('TotalCount') or 0
            entry['total_response_time'] += stats.get('TotalResponseTime') or 0.0

            # 收集出边。
            #
            # 原先这里有 `if xray_type is not None: continue`，注释写着
            # 「只有服务本体会有出边」—— 那个假设在只有 EKS 服务插桩时成立，
            # **加入 Lambda 后就错了**：Lambda 的出边挂在 `AWS::Lambda::Function`
            # 节点上（Type 不为 None），于是所有 Lambda 的下游依赖被整批跳过。
            # 实测症状是 xray_edges_seen 始终是 7，Lambda 明明在服务图里有出边
            # （neptune-etl-trigger → neptune-etl-from-aws、
            #   neptune-etl-from-xray → Neptune endpoint）却一条都没进图谱。
            for edge in svc.get('Edges', []) or []:
                target = by_ref.get(edge.get('ReferenceId'))
                if not target:
                    continue
                t_key = _classify(target.get('Name'), target.get('Type'))
                if t_key is None:
                    tt = str(target.get('Type'))
                    if tt != 'client':
                        unmapped.add((tt, target.get('Name')))
                    continue
                # 过滤自环。X-Ray 用 AWS::Lambda（服务容器）→
                # AWS::Lambda::Function（函数执行）表示同一个 Lambda 的内部结构，
                # 那不是依赖关系。键归一化后两端落到同一个 key，正好识别为自环。
                if t_key == key:
                    continue
                ek = (key, t_key)
                estats = edge.get('SummaryStatistics') or {}
                ee = edges.setdefault(ek, {
                    'call_count': 0, 'error_count': 0,
                    'fault_count': 0, 'total_response_time': 0.0,
                })
                ee['call_count'] += estats.get('OkCount') or 0
                ee['error_count'] += (estats.get('ErrorStatistics') or {}).get('TotalCount') or 0
                ee['fault_count'] += (estats.get('FaultStatistics') or {}).get('TotalCount') or 0
                ee['total_response_time'] += estats.get('TotalResponseTime') or 0.0

    if unmapped:
        logger.warning(
            "X-Ray 出现 %d 个未映射的 (type, name)，已跳过未写入图谱："
            "%s —— 若其中有真实依赖，需在 XRAY_TYPE_TO_RESOURCE_NODE 或 "
            "XRAY_SERVICE_ALIASES 里显式登记，**不要靠兜底猜类型**",
            len(unmapped), sorted(unmapped)[:10])
    return {'nodes': nodes, 'edges': edges, 'window_hours': hours,
            'unmapped': sorted(unmapped)}


def _classify(raw_name: str, xray_type):
    """
    把一个 X-Ray 节点归一成 (canonical_name, kind, type_for_identity)。
    返回 None 表示刻意不建模（如 client 影子节点）。

    ## 为什么 aws_service 的 type_for_identity 恒为 None

    干跑时踩到的真 bug：最初 key 直接带 xray_type，于是 SSM 与
    SimpleSystemsManagement 虽然规范名都归一到 'ssm'，**key 却因 type 不同而没合并**，
    图谱里会出现两个 'ssm' 条目。它们随后被写进同一个顶点，
    xray_type 互相覆盖 —— 哪个值最终留下取决于 dict 迭代顺序，
    而 xray_aliases 每次只带得到一半的原始名。

    对粗粒度 AWS 服务，**规范名就是身份**；X-Ray 报的 type 只是观测到的别名之一，
    属于数据（累积进 xray_types），不属于键。
    resource 类型则相反 —— 它的 xray_type 决定落到哪个节点标签，必须进键。
    """
    if not raw_name:
        return None
    if xray_type == 'client':
        return None

    # 服务本体：X-Ray 的 Type 为 None
    if xray_type is None:
        low = raw_name.lower()
        # 有些 AWS 托管服务会被 X-Ray 报成「服务本体」而非 AWS:: 类型
        # （实测 'Secrets Manager' 就是这样）。按别名表识别，
        # 否则它会被当成一个不存在的微服务，在图谱里变成孤儿节点。
        if low in XRAY_SERVICE_ALIASES:
            return (XRAY_SERVICE_ALIASES[low], 'aws_service', None)
        return (low, 'service', None)

    # 有精确资源名的 AWS 资源类型。
    # 身份键的第三位用**映射后的节点类型**而不是原始 xray_type ——
    # X-Ray 会用多个 type 表示同一个资源（AWS::Lambda 与
    # AWS::Lambda::Function 都指同一个函数），带原始 type 会拆成两个 key，
    # 度量被分摊到两份、写边时后者覆盖前者而不是累加。
    # 这与 ssm 因 AWS::SSM / AWS::SimpleSystemsManagement 拆键是同一个坑。
    node_type = XRAY_TYPE_TO_RESOURCE_NODE.get(xray_type)
    if node_type:
        return (raw_name, 'resource', node_type)

    low = raw_name.lower()

    # 只有**真的是 AWS 托管服务的粗粒度标签**才落 AWSServiceEndpoint。
    #
    # 判据是 **Name 本身是不是一个服务标签**，而不是 type 有没有 'AWS::' 前缀。
    # 这一点踩过两次才想清楚：
    #   第一次 —— 兜底「任何不认识的 type 都算 aws_service」，被 Type='remote' 的
    #     search-service.petadoptions.svc.cluster.local 吞掉，一个集群内 K8s 服务
    #     被建成了 AWS 托管服务节点。
    #   第二次 —— 收紧成「type 以 AWS:: 开头才算」之后仍然不对：给 Lambda 开启
    #     Active 追踪后，X-Ray 用 **AWS::Lambda**（容器）+ AWS::Lambda::Function
    #     两个条目表示同一个函数，而前者的 Name 是 `neptune-etl-from-aws`
    #     —— 一个**真实资源名**。按 type 前缀判定会凭空造出一个
    #     与已有 LambdaFunction 节点重名的 AWSServiceEndpoint。
    #
    # 真正的区别：AWS::S3 的 Name 字面就是 `S3`（服务标签），
    # AWS::Lambda 的 Name 是具体函数名（资源标识）。所以只认别名表。
    if low in XRAY_SERVICE_ALIASES:
        return (XRAY_SERVICE_ALIASES[low], 'aws_service', None)

    # Type='remote'：X-Ray 无法识别的进程外被调方，通常是一个主机名。
    if xray_type == 'remote':
        # K8s 集群内 FQDN → 服务名（再经 k8s_alias 映射到图谱服务名）
        if '.svc.cluster.' in low:
            return (_strip_k8s_fqdn(low), 'service', None)
        # 数据库/集群 endpoint → 按图谱里的 endpoint 属性**精确反查**。
        #
        # 开启自埋点后实测出现
        # `petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com`
        # 这类主机名。图谱里 NeptuneCluster / RDSCluster 节点带 `endpoint` 属性，
        # 直接按属性匹配就能命中（实测精确命中 NeptuneCluster/petsite-neptune），
        # **不需要靠字符串截断去猜集群名** —— 这正是本仓库反复强调的
        # 「不用推断冒充观测」。解析在 _dst_matcher 里用 Gremlin 完成，
        # 这里只标记为 endpoint 类型并原样带上主机名。
        if '.amazonaws.com' in low and any(
                seg in low for seg in ('.rds.', '.neptune.', '.cache.', '.es.')):
            return (raw_name, 'endpoint', None)
        return (_strip_k8s_fqdn(low), 'service', None)

    # 其余 AWS:: 类型：既不在资源类型映射里，Name 也不是已知服务标签。
    # **刻意跳过而不是猜一个节点类型** —— 前两次都是猜出来的错。
    # 调用方会把它计入 unmapped_types 并打日志，让它可见而不是静默丢弃。
    return None


def _strip_k8s_fqdn(name: str) -> str:
    """
    把 K8s 集群内 FQDN 还原成服务名，再过一遍 k8s_alias 映射到图谱的服务名：
      search-service.petadoptions.svc.cluster.local → search-service → petsearch

    只剥 `.svc.cluster.` 这种明确的集群内形态。**不**对任意含点的名字截断 ——
    那会把 `logs.ap-northeast-1.amazonaws.com` 之类的公网域名误伤成 `logs`，
    凭空造出一个不存在的服务。

    别名映射不可省：图谱里的服务叫 `petsearch`，K8s 部署叫 `search-service`，
    没有名为 `search-service` 的 Microservice 节点。
    """
    if '.svc.cluster.' in name:
        name = name.split('.', 1)[0]
    return K8S_ALIAS.get(name, name)


def upsert_aws_service_endpoints(nodes: dict, round_ts: int) -> int:
    """
    为粗粒度 AWS 服务身份建 AWSServiceEndpoint 节点，并挂到 Region 下。

    顶点属性一律用 property(single, ...) —— Gremlin 顶点属性默认 SET 基数，
    不带 single 就是**追加而非覆盖**，会静默累积多值。
    本仓库为此清理过 1,897 个冗余值，见 infra/fix_property_cardinality.py。
    """
    written = 0
    for (name, kind, _identity_type), data in nodes.items():
        if kind != 'aws_service':
            continue
        aliases = '; '.join(sorted(data['raw_names']))
        # xray_types 是**集合**：同一个规范服务可能被 X-Ray 以多个 type 报出
        # （SSM 就同时以 AWS::SSM 与 AWS::SimpleSystemsManagement 出现）。
        # 存成合并串而不是任取一个，否则记录的是「碰巧最后写入的那个」。
        types = '; '.join(sorted(data['xray_types'])) or 'AWS::Unknown'
        g = (
            f"g.V().has('AWSServiceEndpoint','name','{safe_str(name)}')"
            f".fold()"
            f".coalesce(__.unfold(),"
            f" __.addV('AWSServiceEndpoint').property(single,'name','{safe_str(name)}'))"
            f".property(single,'granularity','service')"
            f".property(single,'xray_type','{safe_str(types)}')"
            f".property(single,'xray_aliases','{safe_str(aliases)}')"
            f".property(single,'last_seen',{round_ts})"
        )
        neptune_query(g)
        # 挂到 Region，避免成为孤儿节点 —— 与其他 AWS 资源节点一致的建模。
        gl = (
            f"g.V().has('AWSServiceEndpoint','name','{safe_str(name)}').as('s')"
            f".V().hasLabel('Region').has('name','{safe_str(REGION)}')"
            f".coalesce("
            f"  __.inE('LocatedIn').where(__.outV().has('name','{safe_str(name)}')),"
            f"  __.addE('LocatedIn').from('s')"
            f")"
        )
        try:
            neptune_query(gl)
        except Exception as exc:  # noqa: BLE001
            # Region 节点可能不存在（图谱未跑过 etl_aws）。
            # 缺 LocatedIn 只是少一条结构边，不该让节点写入失败。
            logger.warning("AWSServiceEndpoint %s 挂 Region 失败: %s", name, exc)
        written += 1
    return written


def _src_label_clause(name: str) -> str:
    """
    源节点可能是 Microservice 也可能是 LambdaFunction。
    X-Ray 只给名字不给类型，所以两种都试 —— 用 or 而非猜测。
    """
    n = safe_str(name)
    return (f"g.V().or(__.hasLabel('Microservice').has('name','{n}'),"
            f" __.hasLabel('LambdaFunction').has('name','{n}'))")


def _dst_matcher(dst_key) -> tuple:
    """返回 (gremlin 片段, 边类型)。找不到可建模的目标时返回 (None, None)。

    注意第三位对 kind='resource' 是**映射后的节点类型**（见 _classify 的注释），
    不是原始 xray_type —— 这样 AWS::Lambda 与 AWS::Lambda::Function 归一成
    同一个键，不会把同一条边的度量拆成两份。
    """
    name, kind, node_type = dst_key
    n = safe_str(name)
    if kind == 'service':
        return (f"__.or(__.hasLabel('Microservice').has('name','{n}'),"
                f" __.hasLabel('LambdaFunction').has('name','{n}'))", 'Calls')
    if kind == 'aws_service':
        return (f"__.hasLabel('AWSServiceEndpoint').has('name','{n}')", 'AccessesData')
    if kind == 'resource':
        if not node_type:
            return (None, None)
        edge = RESOURCE_NODE_TO_EDGE.get(node_type, 'AccessesData')
        return (f"__.hasLabel('{node_type}').has('name','{n}')", edge)
    if kind == 'endpoint':
        # 按 `endpoint` 属性精确反查 —— 图谱里 NeptuneCluster / RDSCluster
        # 都带这个属性，直接匹配即可命中，不靠主机名截断去猜集群名。
        # 实测 petsite-neptune.cluster-...neptune.amazonaws.com 精确命中
        # NeptuneCluster/petsite-neptune。
        # 匹配不到时边写入会计入 skipped_no_node（如实记录，不新建节点）。
        return (f"__.has('endpoint','{n}')", 'AccessesData')
    return (None, None)


def upsert_xray_edges(edges: dict, window_hours: int, round_ts: int) -> dict:
    """
    写 X-Ray 观测到的边。

    已存在的边只补 X-Ray 度量，**不动 source / dependency_kind** ——
    保留「谁первый发现了这条依赖」的 provenance。
    新建的边才带 source='xray'，那是 X-Ray 自己的贡献。
    """
    stats = {'created': 0, 'corroborated': 0, 'skipped_no_node': 0, 'write_failed': 0}

    for (src_key, dst_key), data in edges.items():
        src_name = src_key[0]
        dst_matcher, edge_type = _dst_matcher(dst_key)
        if dst_matcher is None:
            stats['skipped_no_node'] += 1
            continue

        rt = round(float(data['total_response_time']), 3)
        metrics = (
            f".property('xray_call_count',{int(data['call_count'])})"
            f".property('xray_error_count',{int(data['error_count'])})"
            f".property('xray_fault_count',{int(data['fault_count'])})"
            f".property('xray_total_response_time_s',{rt})"
            f".property('xray_window_hours',{int(window_hours)})"
            f".property('xray_last_seen',{round_ts})"
        )

        # 先探测边是否已存在 —— 决定是「补证据」还是「新建」。
        # 分两步而不是一条 coalesce：因为两个分支要写的属性集不同，
        # coalesce 里没法只在 addE 分支上写 source。
        probe = (
            f"{_src_label_clause(src_name)}.as('s')"
            f".V().where({dst_matcher})"
            f".inE('{edge_type}').where(__.outV().has('name','{safe_str(src_name)}'))"
            f".count()"
        )
        try:
            resp = neptune_query(probe)
            existing = int(extract_value(resp['result']['data']) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning("探测边 %s -[%s]-> %s 失败: %s",
                           src_name, edge_type, dst_key[0], exc)
            stats['skipped_no_node'] += 1
            continue

        if existing > 0:
            g = (
                f"{_src_label_clause(src_name)}.as('s')"
                f".V().where({dst_matcher})"
                f".inE('{edge_type}').where(__.outV().has('name','{safe_str(src_name)}'))"
                f"{metrics}"
                # active 回写 true：X-Ray 刚观测到它，无论之前被谁置为 false。
                f".property('active',true)"
                f".property('last_seen',{round_ts})"
            )
            key = 'corroborated'
        else:
            g = (
                f"{_src_label_clause(src_name)}.as('s')"
                f".V().where({dst_matcher})"
                f".coalesce("
                f"  __.inE('{edge_type}').where(__.outV().has('name','{safe_str(src_name)}')),"
                f"  __.addE('{edge_type}').from('s')"
                f"    .property('source','xray')"
                f"    .property('dependency_kind','dynamic')"
                f"    .property('first_seen',{round_ts})"
                f")"
                f"{metrics}"
                f".property('active',true)"
                f".property('last_seen',{round_ts})"
            )
            key = 'created'

        try:
            neptune_query(g)
        except Exception as exc:  # noqa: BLE001
            logger.warning("写边 %s -[%s]-> %s 失败: %s",
                           src_name, edge_type, dst_key[0], exc)
            stats['write_failed'] += 1
            continue

        if key == 'corroborated':
            stats['corroborated'] += 1
            continue

        # 新建分支必须**复查是否真的落地**。
        #
        # 这是测 demo 脚本时抓到的 bug：如果目标节点不存在，
        # `.V().where(<matcher>)` 匹配不到任何东西，整条 traversal 静默产出空集 ——
        # **不抛异常、不写边**，而原先这里无条件 `created += 1`，
        # 于是每一轮都谎报一次「新建成功」，而图谱边总数始终不变。
        # （实测：连跑三次都报 created=1，带 xray 度量的边却一直是 7 条而非 8 条。）
        #
        # 「写了就算成功」这种假设正是本仓库反复踩的坑，所以这里回读确认。
        verify = (
            f"{_src_label_clause(src_name)}.as('s')"
            f".V().where({dst_matcher})"
            f".inE('{edge_type}').where(__.outV().has('name','{safe_str(src_name)}'))"
            f".count()"
        )
        try:
            landed = int(extract_value(neptune_query(verify)['result']['data']) or 0)
        except Exception:  # noqa: BLE001
            landed = 0
        if landed > 0:
            stats['created'] += 1
        else:
            # 目标节点不存在 —— 观测到了但图谱里没有对应实体。
            # 记进 skipped 而不是 created，让计数如实反映图谱状态。
            stats['skipped_no_node'] += 1
            logger.info(
                "观测到 %s -[%s]-> %s，但图谱中无匹配目标节点，跳过（未写入）",
                src_name, edge_type, dst_key[0])

    return stats


def deactivate_stale_xray_edges(round_ts: int) -> int:
    """
    把久未被 X-Ray 观测到的 xray 源边置 active=false。

    只处理 source='xray' 的边 —— 别的源发现的边由它自己的 ETL 负责失效判定，
    在这里动它们等于两个 ETL 抢同一个属性的写权（本仓库踩过这个坑）。
    """
    cutoff = round_ts - XRAY_STALE_SECONDS
    g = (
        f"g.E().has('source','xray').has('xray_last_seen',lt({cutoff}))"
        f".has('active',true).property('active',false).count()"
    )
    try:
        resp = neptune_query(g)
        return int(extract_value(resp['result']['data']) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("失效判定失败: %s", exc)
        return 0


def run_etl(lookback_hours: int = None) -> dict:
    round_ts = int(time.time())
    graph = fetch_xray_service_graph(lookback_hours)

    kinds = defaultdict(int)
    for (_, kind, _) in graph['nodes']:
        kinds[kind] += 1

    endpoints = upsert_aws_service_endpoints(graph['nodes'], round_ts)
    edge_stats = upsert_xray_edges(graph['edges'], graph['window_hours'], round_ts)
    deactivated = deactivate_stale_xray_edges(round_ts)

    result = {
        'round_ts': round_ts,
        'window_hours': graph['window_hours'],
        'xray_nodes_seen': len(graph['nodes']),
        'xray_nodes_by_kind': dict(kinds),
        'xray_edges_seen': len(graph['edges']),
        'xray_unmapped_types': graph.get('unmapped', []),
        'aws_service_endpoints_written': endpoints,
        'edges_created': edge_stats['created'],
        'edges_corroborated': edge_stats['corroborated'],
        'edges_skipped_no_node': edge_stats['skipped_no_node'],
        'edges_write_failed': edge_stats['write_failed'],
        'edges_deactivated': deactivated,
    }
    logger.info("etl_xray 完成: %s", result)
    return result


def handler(event, context):  # noqa: ARG001
    hours = None
    if isinstance(event, dict) and event.get('lookback_hours'):
        hours = int(event['lookback_hours'])
    return {'statusCode': 200, 'body': run_etl(hours)}


if __name__ == '__main__':
    # 这里**不**安排 sys.path —— 那是无效的：模块顶部的
    # `from neptune_client_base import ...` 在 __main__ 之前就已执行并失败。
    # 之前写成在这里 insert path，是一个看起来合理但根本跑不通的写法。
    #
    # 库级别改全局 sys.path 也不行：本仓库因此整场测试跑在 vendored 副本上过一次
    # （conftest 把 etl_aws 部署包放上全局 path，同名包遮蔽）。
    #
    # 所以依赖由**调用方**提供，这也与真实运行环境一致：
    #   Lambda   —— shared 层挂在 /opt/python，import 天然可用
    #   本地运行 —— 由调用者设 PYTHONPATH：
    #     PYTHONPATH=../shared/python python3.11 neptune_etl_xray.py 24
    logging.basicConfig(level=logging.INFO)
    import json as _json
    _hours = int(sys.argv[1]) if len(sys.argv) > 1 else None
    print(_json.dumps(run_etl(_hours), ensure_ascii=False, indent=2))
