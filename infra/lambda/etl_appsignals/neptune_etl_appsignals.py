"""CloudWatch Application Signals → 依赖边采集（**第一阶段：只采集不写库**）。

## 为什么加这个源

同一个 `PetSite`，两个 API 给出的下游数量差 8 倍（2026-09-05 实测）：

| 数据源 | PetSite 的下游 |
|---|---|
| X-Ray `GetServiceGraph`（`etl_xray` 现用） | **1 个**（只有 SSM） |
| Application Signals `ListServiceDependencies` | **10 个（去重）**，且带操作名 |

`amazon-cloudwatch-observability` add-on 在集群上早已 ACTIVE（v6.5.0），
Application Signals 里已注册 310 个服务 —— 这套数据一直都在，
只是依赖图谱还在用粒度更粗的 `GetServiceGraph`。

## 为什么这一版刻意不写图谱

Application Signals 为**每个 ReplicaSet 单独注册服务**：

    petsite-deployment-8599bcbfc7 / -75db64db96 / -76bc5b99cd / -99ff8c99f ...
    pethistory-deployment-58f84c6b8f / -5c64bcc4cc / -5d74c785db ...

而 `etl_xray` 按服务名做节点身份。直接接入 = 每次发布长出一批新节点，
图谱被 deploy 噪声刷爆。所以先跑"采集 + 归一化 + 打印"，
**用真实数据肉眼确认收敛正确，再接写入**。

## 归一化只新增了一步，其余全是复用

规范名的单一真源是 `profiles/petsite.yaml`：

    petsite:
      k8s_deployment: "petsite-deployment"
      k8s_label:      "petsite"
      neptune_name:   "petsite"

经 `EnvironmentProfile` + `ServiceRegistry.resolve()` 消费。所以：

1. **新增**：剥 ReplicaSet 哈希后缀 → `petsite-deployment-8599bcbfc7` 成 `petsite-deployment`
2. **复用** `ServiceRegistry.resolve()` → `petsite-deployment` 成 `petsite`
3. 双身份自动收敛：`PetSite`(env=generic:default) 经 `lower()` 也是 `petsite`
   → 两个身份落到同一节点，**不需要额外的合并规则**

> ⚠️ 实测 5 个 ETL 对 `ServiceRegistry` 的引用数是 **0** —— 各家自己造名字映射
> （`etl_xray` 有本地 `_strip_k8s_fqdn`，`etl_aws` 走 `app_label`）。
> 本模块**用真源**，不再添一份分歧实现。把"让 5 个 ETL 都收敛到真源"
> 记为技术债，不在此处顺手重构。

## 粒度落差：AWS 托管服务只到服务级

Application Signals 对 AWS 托管服务只给 `AWS::BedrockAgentCore` 这样的**服务名**，
拿不到"调的是哪个 runtime"。所以它与已上线的 SSM 声明边是**互补**关系：

- 声明边（`petsite → WaggleAIOrchestrator`）提供**身份**
- 本源提供**活性**（这条路径真的在跑）

写入阶段必须把 observed 证据**并到那条已存在的 static 边上**，
而不是新建 `petsite → AWS::BedrockAgentCore` 平行边 ——
否则图上两条语义重复、粒度不同的边，爆炸半径查询会漏掉服务级那条。
"""

from __future__ import annotations

import collections
import datetime
import logging
import os
import pathlib
import re
import sys

import boto3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 运行时包根：与 rca_window_flush 同样的布局（profiles/ 与 shared/ 是同级目录）
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

REGION = os.environ.get('REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'ap-northeast-1'
# 默认 24 小时。**不要缩短到 6 小时** —— 2026-09-05 实测 6h 窗口只捞到
# petsite 的 2 条下游（SSM + AgentCore），漏掉 petsearch/pethistory/
# payforadoption/petfood/petlistadoptions 全部 5 条应用边，因为这些调用频率低于
# 合成流量。窗口短 = 稀疏依赖被判成"不存在"，是这类源最容易踩的假阴性。
LOOKBACK_SECONDS = int(os.environ.get('APPSIGNALS_LOOKBACK_SECONDS', str(24 * 3600)))

# ReplicaSet 哈希后缀。K8s 生成的是 pod-template-hash，字符集是
# base-32-ish（去掉易混字符），长度实测 8~10。
# 刻意**锚定在结尾**且要求前面有 `-`，避免命中 `pay-for-adoption` 这类真实名字。
_RS_HASH = re.compile(r'-[0-9a-f]{8,10}$')
# Pod 级：ReplicaSet 哈希 + 5 位 pod 后缀
_POD_SUFFIX = re.compile(r'-[0-9a-f]{8,10}-[a-z0-9]{5}$')
# K8s 集群内 FQDN 后缀
_K8S_FQDN = re.compile(r'\.[a-z0-9-]+\.svc\.cluster\.local(:\d+)?$', re.I)


def _services_cfg() -> dict:
    """读 profile 的 services 段（与 `_load_registry` 同源，只读一次文件）。"""
    import yaml
    here = pathlib.Path(_HERE)
    for cand in (here / 'profiles' / 'petsite.yaml',
                 here.parent.parent.parent / 'profiles' / 'petsite.yaml'):
        if cand.exists():
            return (yaml.safe_load(cand.read_text(encoding='utf-8')) or {}).get('services', {})
    raise FileNotFoundError('找不到 profiles/petsite.yaml（规范名真源）')


def _load_registry():
    """加载规范名真源。失败就抛 —— 没有真源时不应该退化成自造映射。

    ## 为什么不走 `EnvironmentProfile`

    `profiles.profile_loader.EnvironmentProfile` 依赖 **pydantic**（带 Rust 扩展、
    十几 MB），而本模块只需要 YAML 里的 `services` 段喂给 `ServiceRegistry`。
    在 Lambda 里为一次字典读取背上 pydantic 不值得
    （2026-09-05 实测：先缺 yaml、补上后又缺 pydantic，依赖在蔓延）。

    所以直接读 YAML 的 `services` 段。**注意这跳过了 profile 的校验与
    `${VAR}` 插值** —— `services` 段里没有插值（插值出现在 `neptune.endpoint`
    这类字段），所以对本用途是安全的。若将来要读 `services` 之外的段，
    必须重新评估，不要顺手扩展这个函数。

    `ServiceRegistry` 本身没有重依赖，是真正承载名字解析逻辑的地方，照常复用。
    """
    import yaml
    from shared.service_registry import ServiceRegistry

    here = pathlib.Path(_HERE)
    for cand in (here / 'profiles' / 'petsite.yaml',
                 here.parent.parent.parent / 'profiles' / 'petsite.yaml'):
        if cand.exists():
            data = yaml.safe_load(cand.read_text(encoding='utf-8')) or {}
            return ServiceRegistry(data.get('services', {}))
    raise FileNotFoundError('找不到 profiles/petsite.yaml（规范名真源）')


def strip_replicaset_suffix(name: str) -> str:
    """剥掉 ReplicaSet / Pod 哈希后缀，还原到 Deployment 名。

    `petsite-deployment-8599bcbfc7`        -> `petsite-deployment`
    `pethistory-deployment-674788d7bd-gd8b4` -> `pethistory-deployment`

    先试 Pod 级（更长的模式）再试 ReplicaSet 级，否则 Pod 名只会被剥掉一半。
    """
    if not name:
        return name
    n = _POD_SUFFIX.sub('', name)
    if n != name:
        return n
    return _RS_HASH.sub('', name)


def strip_k8s_fqdn(name: str) -> str:
    """`petfood.petadoptions.svc.cluster.local:8080` -> `petfood`"""
    if not name:
        return name
    return _K8S_FQDN.sub('', name)


def canonical(raw: str, registry) -> str:
    """把 Application Signals 的服务名收敛到图谱的 Microservice 名。

    顺序有讲究：先剥 FQDN、再剥 ReplicaSet 哈希、再小写、最后过真源。
    反过来（先过真源）会因为名字还带后缀而查不中，白白退化成原名。

    ⚠️ `ServiceRegistry.resolve()` **自己不做小写化**
    （实测 `resolve('PetSite')` 原样返回 `'PetSite'`）。所以这里
    **「先 lower 再 resolve」的顺序是承重的**，调换会让所有大写形态解析失败。
    """
    n = strip_replicaset_suffix(strip_k8s_fqdn(raw or '')).lower()
    return registry.resolve(n)


# ── 目标解析：把归一化后的名字落到图谱上具体的 (label, name) ──────────────
#
# 不复制 etl_xray 的 XRAY_SERVICE_ALIASES 别名表 —— 那会成为第二份分歧实现。
# 改为**直接查图谱**：`AWSServiceEndpoint` 节点上的 `xray_aliases` 属性
# 就是现成的真源（实测 7 个节点，`ssm` 的别名里含 `SimpleSystemsManagement`）。
#
# ⚠️ 刻意**不做兜底**。`etl_xray` 第 477-484 行记着一个旧 bug：
# 「任何不认识的 type 都算 aws_service」的兜底让 `Type='remote'` 的条目
# 建出与已有 `LambdaFunction` 重名的 `AWSServiceEndpoint` 节点。
# 所以这里解析不出就是 unresolved，**不猜、不新建节点**。

# 明确丢弃的垃圾条目（不是依赖，也不该记成 unresolved）
_JUNK_TARGETS = frozenset({
    'unknownremoteservice',  # Application Signals 对解不出的远端的占位符
    'amazon.com',            # 出站到公网，不是本系统的依赖
})

# ── 抽象层伪依赖：服务指向"实现自己的那个 Lambda" ──────────────────────
#
# 2026-09-06 查证：图上同一个物理 Lambda 有**两个抽象层的节点**
#   Microservice{petstatusupdater}                       ← 业务视角（profile 的 type: lambda）
#   LambdaFunction{ServicesEks2-statusupdaterservice...} ← 资源视角（aws-etl 从 CFN 采）
# 两者之间**没有任何边**，图谱没建这层身份关系。
#
# Application Signals 报的 `PetAdoptionStatusUpdater(api-gateway) → ...lambdafn`
# 是"API Gateway 调它自己的后端 Lambda"，归一化后就成了
# `Microservice{petstatusupdater} → LambdaFunction{同一个东西}` ——
# 在图谱的抽象层上等于**业务服务依赖自己**。
#
# 契约里没有边类型允许 `Microservice → LambdaFunction`，**这是对的不是缺口**：
# 扩 pairs 会往图里注入一条假的自依赖。真正缺的是
# `Microservice -实现于-> LambdaFunction` 这类**身份边**，
# 而那该由持有 CFN 映射的 `aws-etl` / `etl_cfn` 建，不是观测源的职责。
#
# 判据刻意保守：源服务在 profile 里声明 `type: lambda`，且目标是 LambdaFunction。
# 这样只命中"逻辑服务本身就是个 Lambda"的情形，不会误杀真实的
# "服务调别人家的 Lambda"。
def _lambda_backed_services(services_cfg: dict) -> frozenset:
    return frozenset(
        cfg.get('neptune_name', name)
        for name, cfg in (services_cfg or {}).items()
        if str(cfg.get('type', '')).lower() == 'lambda')


# 允许本源新建的服务级 AWS 端点（白名单，**不做兜底**）。
#
# `AWSServiceEndpoint` 的设计意图就是承载"只知道服务名、拿不到资源名"的依赖
# （契约原文：把 `S3` 猜成 30+ 个 bucket 中某一个是推断而非观测，所以另立此类型）。
# 图上原有 7 个（dynamodb/ssm/s3/sqs/sts/secretsmanager/xray）全由 etl_xray 建；
# Application Signals 额外观测到 sns 与 stepfunctions，按同一模式补。
#
# ⚠️ 白名单而非兜底：`etl_xray` 第 477-484 行记着旧 bug ——
# "任何不认识的 type 都算 aws_service"的兜底曾建出与 LambdaFunction 重名的节点。
_ALLOWED_NEW_ENDPOINTS = frozenset({'sns', 'stepfunctions'})

# `RemoteService` 报的数据库引擎名 → 图谱 RDS 节点的 engine 前缀。
# 靠 **engine 匹配**定身份，不靠名字猜：
#   Application Signals 报 `postgres`；图上两个 RDSCluster 分别是
#   aurora-postgresql（应用库）与 aurora-mysql（Grafana 自己的），engine 唯一命中。
# SSM `/petstore/rdsendpoint` 与图上已有的
# `payforadoption -AccessesData-> serviceseks2-database...` 边均佐证。
_DB_ENGINE_HINTS = {
    'postgres': 'aurora-postgresql',
    'postgresql': 'aurora-postgresql',
    'mysql': 'aurora-mysql',
}

# Application Signals 对 AWS 服务的命名 → 图谱 AWSServiceEndpoint 的 name。
#
# ⚠️ 这**不是** `etl_xray` 的 `XRAY_SERVICE_ALIASES` 的重复实现，
# 而是**另一个源的词汇表**。契约里 `xray_aliases` 的定义是
# "X-Ray 报过的原始名"，由 etl_xray 写入 —— 往那个属性里塞
# Application Signals 的名字会污染它的语义。
#
# 实测两个源对同一服务的报法不同：
#   STS：       X-Ray 报 `STS`            / Application Signals 报 `AWS::SecurityToken`
#   SSM：       X-Ray 报 `SimpleSystemsManagement`（已在 xray_aliases 里，故能落地）
#              / Application Signals 报 `AWS::SimpleSystemsManagement`（同名，碰巧命中）
#
# 所以这里只收 **Application Signals 特有的报法**；能靠图谱
# `xray_aliases` 命中的不重复列出（`_load_graph_index` 已覆盖）。
#
# TODO(债): 契约里给 AWSServiceEndpoint 加 `appsignals_aliases` 属性，
# 让这张表最终也落到图上，与 xray_aliases 并列而非藏在代码里。
_APPSIGNALS_AWS_ALIASES = {
    'securitytoken': 'sts',
}


def _load_graph_index(neptune_query) -> tuple[set, dict, dict, dict]:
    """从图谱读四张索引。

    `lambdas` 刻意是 **小写 → 图上原始名** 的字典而不是集合：
    Lambda 的 CFN 物理名**大小写混杂**（实测 30 个节点里 17 个含大写，
    如 `ServicesEks2-statusupdaterservicelambdafn37242E00-0SHsIkrhwJ32`），
    而 Application Signals 报的是全小写形态。所以必须**大小写不敏感匹配、
    返回图上的原始名** —— 否则要么匹配不上，要么写出一个小写的重名节点。

    ⚠️ 2026-09-06 已核查：图上 13 个全小写 Lambda **不是**小写化造成的重复副本
    （小写化后与 17 个含大写名字**零命中**），它们本来就叫小写名
    （`neptune-etl-from-aws` 这类 CDK 命名），全部 source=aws-etl。
    """
    def _names(label: str) -> set:
        r = neptune_query(f"g.V().hasLabel('{label}').values('name').fold()")
        vals = r['result']['data']['@value'][0]['@value']
        return {v['@value'] if isinstance(v, dict) else v for v in vals}

    micro = _names('Microservice')
    lambdas = {n.lower(): n for n in _names('LambdaFunction')}

    # RDS：engine → 节点名。用于把 `postgres` 这类引擎名定到具体集群。
    q_rds = ("g.V().hasLabel('RDSCluster').project('n','e')"
             ".by(__.values('name'))"
             ".by(__.coalesce(__.values('engine'), __.constant(''))).fold()")
    rows = neptune_query(q_rds)['result']['data']['@value'][0]['@value']
    engine_to_rds: dict[str, str] = {}
    for row in rows:
        it = iter(row['@value'])
        d = dict(zip(it, it))
        gv = lambda k: d[k]['@value'] if isinstance(d[k], dict) else d[k]  # noqa: E731
        eng = str(gv('e')).lower()
        if eng:
            # 同一 engine 有多个集群时不猜 —— 记为冲突，交由调用方判 unresolved
            engine_to_rds[eng] = '\x00CONFLICT' if eng in engine_to_rds else gv('n')

    q = ("g.V().hasLabel('AWSServiceEndpoint').project('n','a')"
         ".by(__.values('name'))"
         ".by(__.coalesce(__.values('xray_aliases'), __.constant(''))).fold()")
    rows = neptune_query(q)['result']['data']['@value'][0]['@value']
    alias_to_ep: dict[str, str] = {}
    for row in rows:
        it = iter(row['@value'])
        d = dict(zip(it, it))
        gv = lambda k: d[k]['@value'] if isinstance(d[k], dict) else d[k]  # noqa: E731
        ep = gv('n')
        alias_to_ep[ep.lower()] = ep
        # xray_aliases 是 '; ' 连接的原始名列表
        for a in str(gv('a')).split(';'):
            a = a.strip().lower()
            if a:
                alias_to_ep[a] = ep
    # 叠加本源特有的报法。只在图上已有该端点时才生效 ——
    # 不为一个不存在的端点凭空造映射。
    for raw, ep in _APPSIGNALS_AWS_ALIASES.items():
        if ep.lower() in alias_to_ep:
            alias_to_ep.setdefault(raw, alias_to_ep[ep.lower()])
    return micro, lambdas, alias_to_ep, engine_to_rds


def resolve_target(canon_name: str, dep_type: str, micro: set,
                   lambdas: dict, alias_to_ep: dict,
                   engine_to_rds: dict | None = None) -> tuple[str, str] | None:
    """把下游名解析成 (label, name)；解析不出返回 None（= unresolved）。

    `canon_name` 已经过 `canonical()` 归一化（含小写化），形如 `petsearch`、
    `aws::simplesystemsmanagement`、`postgres`。
    """
    if not canon_name or canon_name in _JUNK_TARGETS:
        return None

    # 1) 应用服务
    if canon_name in micro:
        return ('Microservice', canon_name)

    # 2) Lambda 函数：Application Signals 报的是 Lambda **物理名的小写形态**，
    #    图上存的是 CFN 原始大小写。用小写索引查，**返回图上的原始名**。
    real = lambdas.get(canon_name)
    if real:
        return ('LambdaFunction', real)

    # 3) 数据库引擎名 → 具体 RDS 集群（靠 engine 匹配，不靠名字猜）。
    #    同一 engine 有多个集群时判 unresolved，绝不挑一个。
    eng = (_DB_ENGINE_HINTS.get(canon_name) if engine_to_rds else None)
    if eng:
        node = engine_to_rds.get(eng)
        if node and node != '\x00CONFLICT':
            return ('RDSCluster', node)
        return None

    # 4) AWS 托管服务：剥掉 `aws::` 前缀后查别名索引
    bare = canon_name[5:] if canon_name.startswith('aws::') else canon_name
    ep = alias_to_ep.get(bare)
    if ep:
        return ('AWSServiceEndpoint', ep)

    # 5) 白名单内的服务级端点：图上还没有，但按 AWSServiceEndpoint 的设计意图
    #    应该建（只知道服务名、拿不到资源名，正是该类型存在的理由）。
    #    返回目标身份，由 `ensure_service_endpoints()` 负责建节点。
    if bare in _ALLOWED_NEW_ENDPOINTS:
        return ('AWSServiceEndpoint', bare)

    # 6) 解析不出 —— 显式返回 None，交由调用方记录为 unresolved
    return None


def ensure_service_endpoints(writable: list, neptune_query,
                             apply: bool = False) -> dict:
    """为白名单内、图上还缺的服务级 AWS 端点建节点。

    只建 `name` + `granularity='service'` + `source`，**不写 `xray_*` 属性** ——
    那些的语义是"X-Ray 报过的原始名/类型"，由 `etl_xray` 拥有，
    本源塞进去会污染它们（同 cycle-7 那次的判断）。
    """
    from graph_contract import assert_node_type  # type: ignore
    stats = collections.Counter()
    want = {dn for (_sl, _sn), (dl, dn), _t, _c in writable
            if dl == 'AWSServiceEndpoint' and dn in _ALLOWED_NEW_ENDPOINTS}
    if not want:
        return {'nothing_to_create': 1}
    assert_node_type('AWSServiceEndpoint')
    round_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    for name in sorted(want):
        n = name.replace("'", "\\'")
        g = (f"g.V().has('AWSServiceEndpoint','name','{n}').fold()"
             f".coalesce(__.unfold(),"
             f" __.addV('AWSServiceEndpoint')"
             f"   .property(single,'name','{n}')"
             f"   .property(single,'granularity','service')"
             f"   .property(single,'source','{SOURCE}')"
             f"   .property(single,'first_seen',{round_ts}))"
             f".property(single,'last_seen',{round_ts})")
        if not apply:
            print(f'  [dry] 确保服务级端点节点 AWSServiceEndpoint:{name}')
            stats['dry'] += 1
            continue
        try:
            neptune_query(g)
            stats['ensured'] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning('建/更新端点 %s 失败：%r', name, e)
            stats['failed'] += 1
    return dict(stats)




def collect(region: str = REGION, lookback: int = LOOKBACK_SECONDS) -> dict:
    """采集 Application Signals 的服务与依赖边，返回归一化后的结果。"""
    registry = _load_registry()
    c = boto3.client('application-signals', region_name=region)
    end = datetime.datetime.now(datetime.timezone.utc)
    start = end - datetime.timedelta(seconds=lookback)

    services, tok = [], None
    while True:
        kw = {'StartTime': start, 'EndTime': end, 'MaxResults': 100}
        if tok:
            kw['NextToken'] = tok
        r = c.list_services(**kw)
        services += r.get('ServiceSummaries', [])
        tok = r.get('NextToken')
        if not tok:
            break

    # 只取本环境认识的服务作为**源端** —— AWS 托管服务、ALB、EC2 节点等
    # 不该当作源端展开（它们的"下游"不是应用依赖）。
    known = set(registry.all_service_names())
    edges = collections.Counter()
    raw_to_canon: dict[str, str] = {}
    src_considered = 0

    for s in services:
        ka = s.get('KeyAttributes') or {}
        if ka.get('Type') != 'Service':
            continue
        raw_name = ka.get('Name') or ''
        canon = canonical(raw_name, registry)
        raw_to_canon[raw_name] = canon
        if canon not in known:
            continue
        src_considered += 1
        try:
            deps, dtok = [], None
            while True:
                kw = {'StartTime': start, 'EndTime': end,
                      'KeyAttributes': ka, 'MaxResults': 100}
                if dtok:
                    kw['NextToken'] = dtok
                rr = c.list_service_dependencies(**kw)
                deps += rr.get('ServiceDependencies', [])
                dtok = rr.get('NextToken')
                if not dtok:
                    break
        except Exception as e:
            logger.warning('取 %s 的依赖失败：%r', raw_name, e)
            continue
        for d in deps:
            dk = d.get('DependencyKeyAttributes') or {}
            dname = dk.get('Name')
            if not dname:
                continue
            dtype = dk.get('Type') or '?'
            edges[(canon, canonical(dname, registry), dtype)] += 1

    return {'services_total': len(services),
            'sources_considered': src_considered,
            'raw_to_canon': raw_to_canon,
            'edges': edges,
            # 一并带出"哪些逻辑服务本身就是 Lambda"，供抽象层伪依赖判定。
            # 由 collect 提供而非调用方自己读 profile —— 同一份真源只读一次。
            'lambda_backed': _lambda_backed_services(_services_cfg())}


SOURCE = 'appsignals-etl'

# 端点组合 → 边类型。**查契约得出，不是猜的**（cycle-9）：
#   Microservice -> Microservice        只有 Calls        允许（TTL 1800s）
#   Microservice -> AWSServiceEndpoint 只有 AccessesData 允许（TTL 21600s）
#   Microservice -> LambdaFunction     **没有任何边类型允许** —— 契约缺口，待决
_EDGE_TYPE_FOR = {
    ('Microservice', 'Microservice'): 'Calls',
    ('Microservice', 'AWSServiceEndpoint'): 'AccessesData',
    # 数据库是数据访问而非 L7 调用，故 AccessesData 而非 Calls。
    # 契约已允许该组合（etl_aws 建的既有边就是这个类型）。
    ('Microservice', 'RDSCluster'): 'AccessesData',
}

# 本 ETL 会写哪些边类型。**唯一开关，Lambda 与本地 CLI 共用** ——
# 不要在两个入口各写一份 only_labels，那正是本仓库反复吃亏的
# "两份分歧实现"（verify_confidence ±4.0/0.0 是同一根因）。
#
# cycle-10 首次写入时只放开 AccessesData，暂缓 Calls，理由是
# "Calls 的 TTL 只有 1800s 而采集窗口 24h，落差最大，怕反复删又建"。
# cycle-15 放开 Calls，三条顾虑已逐条被证据消解：
#   1. graph_cleanup 只软删（active=false），**从不硬删** —— cycle-11/12 读实现确认
#   2. EventBridge 调度 15 分钟 < 1800s TTL，刷新永远赶在过期前 —— cycle-14
#   3. 24h 窗口的陈旧风险已由每条边上的 observation_window_seconds 显式标注 —— cycle-10
WRITE_EDGE_TYPES = frozenset({'AccessesData', 'Calls'})


def write_edges(writable: list, neptune_query, lookback: int,
                only_labels: set | None = None, apply: bool = False) -> dict:
    """把可写边落到图谱。默认 **dry-run**，只打印 Gremlin。

    ## 为什么每条边都要带 observation_window_seconds

    Application Signals 的依赖是**窗口内聚合**，拿不到每次调用的时间戳。
    一条 20 小时前发生、之后再没发生过的调用，仍会出现在 24h 窗口里，
    被写成 `last_seen = now` 的"新鲜"边，而且**每轮 ETL 都会再刷新一次** ——
    `Calls` 的 30 分钟 TTL 因此根本没机会生效，依赖消失的检测要滞后整个窗口。

    所以边上必须显式写出窗口长度，让消费方知道这是
    **"窗口内至少发生过一次"而不是"刚刚发生"**。

    刻意**不**把采集窗口缩短到与 TTL 对齐：cycle-3 实测 6 小时窗口就已经漏掉
    petsite 的 5 条应用边，30 分钟几乎采不到东西。
    **宁可标注粒度，不可制造假阴性。**

    ## 已存在的边只补度量，绝不覆盖 source / dependency_kind

    沿用 `etl_xray` 的既有约定：`source` 记的是"谁首先发现了它"，
    被后来的源覆盖就丢失了发现历史。
    """
    from graph_contract import assert_edge_type, assert_source  # type: ignore
    assert_source(SOURCE, 'write_edges')
    round_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    stats = collections.Counter()

    for (sl, sn), (dl, dn), dep_type, cnt in writable:
        et = _EDGE_TYPE_FOR.get((sl, dl))
        if not et:
            stats['no_edge_type'] += 1
            logger.warning('契约无边类型承载 %s -> %s，跳过（%s -> %s）', sl, dl, sn, dn)
            continue
        if only_labels and et not in only_labels:
            stats['filtered_out'] += 1
            continue
        assert_edge_type(et, sl, dl)

        sn_q, dn_q = sn.replace("'", "\\'"), dn.replace("'", "\\'")
        base = (f"g.V().hasLabel('{sl}').has('name','{sn_q}').as('s')"
                f".V().hasLabel('{dl}').has('name','{dn_q}')")
        # 只在新建时写 source / dependency_kind / first_seen；已存在的边不动它们
        upsert = (
            f"{base}.coalesce("
            f"  __.inE('{et}').where(__.outV().has('name','{sn_q}')),"
            f"  __.addE('{et}').from('s')"
            f"    .property('source','{SOURCE}')"
            f"    .property('dependency_kind','dynamic')"
            f"    .property('first_seen',{round_ts})"
            f")"
            f".property('active',true)"
            f".property('last_seen',{round_ts})"
            f".property('observation_window_seconds',{lookback})"
            f".property('appsignals_entries',{cnt})"
        )
        if not apply:
            print(f"  [dry] {sl}:{sn} -[{et}]-> {dl}:{dn}")
            stats['dry'] += 1
            continue
        try:
            neptune_query(upsert)
            stats['written'] += 1
        except Exception as e:  # noqa: BLE001
            logger.warning('写边失败 %s -[%s]-> %s：%r', sn, et, dn, e)
            stats['failed'] += 1
    return dict(stats)


def classify(edges: dict, micro, lambdas, alias_to_ep, engine_to_rds,
             lambda_backed: frozenset) -> tuple[list, list, list, list]:
    """把采集到的边分成 可写 / 未解析 / 垃圾 / 抽象层伪依赖。

    抽取成共用函数是刻意的：Lambda 与本地 CLI 两个入口都调它，
    否则分类规则会分叉（本仓库因"两份分歧实现"吃过亏）。
    """
    writable, unresolved, junk, self_impl = [], [], [], []
    for (s_, d_, t_), cnt in edges.items():
        src = resolve_target(s_, 'Service', micro, lambdas, alias_to_ep, engine_to_rds)
        dst = resolve_target(d_, t_, micro, lambdas, alias_to_ep, engine_to_rds)
        if d_ in _JUNK_TARGETS:
            junk.append((s_, d_, t_))
        elif (src and dst and src[0] == 'Microservice'
              and dst[0] == 'LambdaFunction' and src[1] in lambda_backed):
            # 服务本身就是个 Lambda，目标是它自己的物理 Lambda 节点 ——
            # 抽象层伪依赖，不是真实依赖（见 _lambda_backed_services 上方说明）
            self_impl.append((s_, d_, t_))
        elif src and dst:
            writable.append((src, dst, t_, cnt))
        else:
            unresolved.append((s_, d_, t_))
    return writable, unresolved, junk, self_impl


def merge_agentcore_observation(unresolved: list, neptune_query, lookback: int,
                                apply: bool = False) -> dict:
    """把观测到的 `→ aws::bedrockagentcore` 依赖并到已存在的声明边上。

    ## 为什么不建新边

    Application Signals 对 AWS 托管服务只到**服务级**：它只说"petsite 调了
    AgentCore"，说不出调的是哪个 runtime。而 `agentcore-etl` 从 SSM 声明
    建出的 `petsite -DependsOn-> WaggleAIOrchestrator` 是**runtime 级**。

    两者是**互补**关系：
      · 声明边提供**身份**（是 WaggleAIOrchestrator 而不是别的 runtime）
      · 本源提供**活性**（这条路径真的在跑，不只是配置上应该跑）

    若另建一条 `petsite -AccessesData-> AWSServiceEndpoint{bedrockagentcore}`
    平行边，图上就有两条语义重复、粒度不同的边，
    **爆炸半径查询会漏掉其中一条**。所以把活性证据并到声明边上。

    ## 绝不覆盖 source / dependency_kind

    那条边的身份来自声明，`source='agentcore-etl'` /
    `dependency_kind='static'` 必须保持。本源只追加
    `observation_window_seconds` / `appsignals_entries` / `last_seen`。

    ## 边不存在时不创建

    建那条边是 `agentcore-etl` 的职责（它持有 SSM 声明）。
    若边不存在，说明声明侧没跑或没权限 —— 本源**不越权代建**，
    只报 `edge_missing`，让问题留在它该出现的地方。
    """
    import boto3
    stats = collections.Counter()
    targets = [(s, d, t) for (s, d, t) in unresolved
               if 'bedrockagentcore' in str(d).lower()]
    if not targets:
        return {'skipped_no_agentcore_dep': 1}

    # 用与 agentcore-etl 相同的 SSM 参数定 runtime 身份（单一真源）
    try:
        ssm = boto3.client('ssm', region_name=REGION)
        arn = ssm.get_parameter(
            Name='/petstore/agent/waggleairuntimearn')['Parameter']['Value']
        runtime_name = arn.rsplit('/', 1)[-1].rsplit('-', 1)[0]
    except Exception as e:  # noqa: BLE001
        logger.error('无法从 SSM 定 runtime 身份，AgentCore 活性证据无法合并：%r', e)
        return {'ssm_failed': 1}

    round_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
    for src_name, _dst, _t in targets:
        sn = src_name.replace("'", "\\'")
        rn = runtime_name.replace("'", "\\'")
        probe = (f"g.V().hasLabel('Microservice').has('name','{sn}')"
                 f".outE('DependsOn').where("
                 f"  __.inV().hasLabel('AgentRuntime').has('name','{rn}'))"
                 f".count()")
        try:
            resp = neptune_query(probe)
            vals = (resp or {}).get('result', {}).get('data', {}).get('@value', [])
            raw = vals[0] if vals else 0
            n = int((raw.get('@value', raw) if isinstance(raw, dict) else raw) or 0)
        except Exception as e:  # noqa: BLE001
            logger.warning('探测声明边失败：%r', e)
            stats['probe_failed'] += 1
            continue
        if n == 0:
            logger.warning(
                '声明边 %s -DependsOn-> %s 不存在，**不代建** —— '
                '那是 agentcore-etl 的职责（它持有 SSM 声明）。'
                '先查 neptune-etl-from-agentcore 是否正常且有 SSM 读权限。',
                src_name, runtime_name)
            stats['edge_missing'] += 1
            continue
        if not apply:
            print(f'  [dry] 并入活性证据: {src_name} -DependsOn-> {runtime_name}')
            stats['dry'] += 1
            continue
        # 只追加活性属性；source / dependency_kind / declared_in 一律不动
        upd = (f"g.V().hasLabel('Microservice').has('name','{sn}')"
               f".outE('DependsOn').where("
               f"  __.inV().hasLabel('AgentRuntime').has('name','{rn}'))"
               f".property('observation_window_seconds',{lookback})"
               f".property('appsignals_observed',true)"
               f".property('last_seen',{round_ts})")
        try:
            neptune_query(upd)
            stats['merged'] += 1
            logger.info('已把 AgentCore 活性证据并入 %s -> %s（未动 source/kind）',
                        src_name, runtime_name)
        except Exception as e:  # noqa: BLE001
            logger.warning('合并失败：%r', e)
            stats['failed'] += 1
    return dict(stats)


def main() -> int:
    r = collect()
    print(f"Application Signals 服务总数: {r['services_total']}"
          f" | 被认作源端的: {r['sources_considered']}")

    print('\n=== 归一化收敛检查（多个原名 → 同一规范名）===')
    inv = collections.defaultdict(list)
    for raw, canon in r['raw_to_canon'].items():
        inv[canon].append(raw)
    for canon, raws in sorted(inv.items(), key=lambda kv: -len(kv[1])):
        if len(raws) > 1:
            print(f'  {canon:<20} ← {len(raws)} 个原名: {sorted(raws)[:5]}')

    print('\n=== 归一化后的依赖边 ===')
    for (s, d, t), cnt in sorted(r['edges'].items(), key=lambda kv: (kv[0][0], -kv[1])):
        star = '  ★AGENTCORE' if 'agentcore' in d.lower() or 'bedrock' in d.lower() else ''
        print(f'  {s:<16} -> {d:<44} [{t}] x{cnt}{star}')

    # ── 目标解析（cycle-6）：确认两端都能落到图谱上具体节点 ──
    sys.path.insert(0, os.path.join(_HERE, '..', 'shared', 'python'))
    try:
        from neptune_client_base import neptune_query  # type: ignore
    except Exception as e:
        print(f'\n⚠️ 无法连图谱做解析验证（{e!r}）；上面的采集结果仍有效。')
        return 0

    micro, lambdas, alias_to_ep, engine_to_rds = _load_graph_index(neptune_query)
    print(f'\n图谱索引：Microservice {len(micro)} | LambdaFunction {len(lambdas)} | '
          f'AWSServiceEndpoint 别名 {len(alias_to_ep)} | RDS engine {len(engine_to_rds)}')
    writable, unresolved, junk, self_impl = classify(
        r['edges'], micro, lambdas, alias_to_ep, engine_to_rds, r['lambda_backed'])
    if self_impl:
        print(f'\n=== 抽象层伪依赖 {len(self_impl)} 条（服务指向实现自己的 Lambda，不写）===')
        for a, b, t in self_impl:
            print(f'  {a} -> {b} [{t}]')
    print(f'\n=== 可写边 {len(writable)} 条（两端都落地）===')
    for (sl, sn), (dl, dn), t, cnt in sorted(writable, key=lambda x: (x[0][1], -x[3])):
        print(f'  {sl}:{sn:<18} -> {dl}:{dn:<28} [{t}] x{cnt}')

    print(f'\n=== 未解析 {len(unresolved)} 条（**不猜、不新建节点**）===')
    for s, d, t in unresolved:
        print(f'  {s:<16} -> {d:<40} [{t}]')

    if junk:
        print(f'\n=== 明确丢弃 {len(junk)} 条（垃圾条目，非依赖）===')
        for s, d, t in junk:
            print(f'  {s} -> {d} [{t}]')

    apply = '--apply' in sys.argv
    # ⚠️ 顺序是承重的：**先建端点节点，再写边**。
    # 写 `-> AWSServiceEndpoint:sns` 的边靠
    # `g.V().hasLabel('AWSServiceEndpoint').has('name','sns')` 匹配目标，
    # 节点不存在时匹配为空 —— 边**静默写不出**且 write_edges 仍报 written。
    # 2026-09-06 dry-run 就暴露过这个顺序反了（Lambda 入口当时是对的）。
    es = ensure_service_endpoints(writable, neptune_query, apply=apply)
    print(f'  端点节点: {es}')
    print(f"\n=== 写入（{'APPLY' if apply else 'DRY-RUN'}）"
          f"边类型 {sorted(WRITE_EDGE_TYPES)} ===")
    st = write_edges(writable, neptune_query, LOOKBACK_SECONDS,
                     only_labels=WRITE_EDGE_TYPES, apply=apply)
    print(f'  {st}')
    # AgentCore 的活性证据并入声明边（粒度落差，见 merge_agentcore_observation）
    ms = merge_agentcore_observation(unresolved, neptune_query,
                                    LOOKBACK_SECONDS, apply=apply)
    print(f'  AgentCore 合并: {ms}')
    if not apply:
        print('\n加 --apply 才真正写入。')
    return 0


if __name__ == '__main__':
    sys.exit(main())


def lambda_handler(event, context):  # noqa: ARG001
    """Lambda 入口。**默认真写入** —— 与本地 CLI 的 dry-run 默认相反。

    理由：定时任务的意义就是持续刷新 last_seen。若这里也默认 dry-run，
    边会在 TTL（AccessesData 21600s）到期后被 neptune-etl-from-aws 置
    active=false，这条源等于没接上（cycle-12 已实测这个后果）。
    传 {"dry_run": true} 可临时只采不写。
    """
    from graph_contract import assert_source  # noqa: F401  提前触发契约校验
    dry = bool((event or {}).get('dry_run'))
    r = collect()
    nq = neptune_query_lambda()
    micro, lambdas, alias_to_ep, engine_to_rds = _load_graph_index(nq)
    writable, unresolved, junk, self_impl = classify(
        r['edges'], micro, lambdas, alias_to_ep, engine_to_rds,
        r['lambda_backed'])
    es = ensure_service_endpoints(writable, nq, apply=not dry)
    st = write_edges(writable, neptune_query_lambda(), LOOKBACK_SECONDS,
                     only_labels=WRITE_EDGE_TYPES, apply=not dry)
    ms = merge_agentcore_observation(unresolved, neptune_query_lambda(),
                                    LOOKBACK_SECONDS, apply=not dry)
    return {'agentcore_merge': ms, 'endpoints': es,
            'services_total': r['services_total'],
            'writable': len(writable), 'unresolved': len(unresolved),
            'junk': len(junk), 'self_implementation': len(self_impl),
            'write_stats': st,
            'lookback_seconds': LOOKBACK_SECONDS, 'dry_run': dry}


def neptune_query_lambda():
    """Layer 里的 neptune_client_base.neptune_query。"""
    from neptune_client_base import neptune_query  # type: ignore
    return neptune_query
