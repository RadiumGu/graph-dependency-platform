"""
neptune_client.py - Neptune Gremlin query utilities for neptune-etl-from-aws.

Base networking functions (neptune_query, safe_str, extract_value) are
imported from the shared Lambda Layer (neptune_client_base).
"""

import time
import logging

from neptune_client_base import neptune_query, safe_str, extract_value  # noqa: F401 (re-exported)
from graph_contract import (
    TIMESTAMP_FIELD,
    assert_edge_type,
    assert_node_type,
    assert_source,
    dependency_edge_labels,
    filter_node_props,
    identity_prop_for,
    is_dependency_edge,
)
from config import (
    REGION,
    FAULT_BOUNDARY_MAP, ENVIRONMENT,
)

# scope 的 namespace 判据从**契约**读，不在 ETL 里抄一份 —— 与
# scripts/label_node_scope.py 共用同一份声明，否则两侧会悄悄分歧
# （这个项目已经因为「同一判据两份实现」踩过一次）。
try:
    from graph_contract_data import NODE_SCOPE as _NODE_SCOPE
    NODE_SCOPE_NS_MAP = dict(_NODE_SCOPE.get('namespace_map') or {})
except Exception:  # pragma: no cover
    # 契约不可用时退化为「不写 scope」而不是猜一个值 —— 猜错比留空更糟，
    # 留空会被守门测试抓到，猜错不会。
    NODE_SCOPE_NS_MAP = {}

logger = logging.getLogger()


_vid_cache = {}  # (label, name) → vertex_id


def get_vertex_id(label: str, name: str):
    key = (label, name)
    if key in _vid_cache:
        return _vid_cache[key]
    n = safe_str(name)
    result = neptune_query(f"g.V().has('{label}', 'name', '{n}').id()")
    ids = result.get('result', {}).get('data', {}).get('@value', [])
    if not ids:
        return None
    vid = ids[0]
    vid = extract_value(vid) if isinstance(vid, dict) else vid
    _vid_cache[key] = vid
    return vid


# 本 ETL 的缺省 source。调用方可以覆盖（K8s 路径用 eks-etl、静态声明用
# aws-etl-static），取值一律经 assert_source 校验，见 upsert_edge 的 docstring。
DEFAULT_SOURCE = 'aws-etl'

# 数值属性白名单 — 这些属性在 Neptune 中保持原始 int/float 类型
NUMERIC_PROPS = {
    'restarts', 'port', 'replica_count', 'error_rate', 'p50_latency',
    'p99_latency', 'rps', 'replicas', 'ready_replicas', 'updated_replicas',
    'available', 'min_replicas', 'max_replicas', 'current_replicas',
    'desired_replicas', 'subscriptions_confirmed', 'memory_size_mb',
    'concurrent_executions', 'priority', 'recovery_time_sec',
    'degradation_rate', 'last_updated',
}


def _format_prop_val(k: str, v) -> str:
    """Format a property value for Gremlin: numeric stays unquoted, else quoted string."""
    if k in NUMERIC_PROPS and isinstance(v, (int, float)):
        return str(v)
    return f"'{safe_str(v)}'"


def upsert_vertex(label: str, name: str, extra_props: dict, managed_by: str = 'manual',
                  identity_prop: str = None):
    """upsert 节点，返回 vertex ID 并写入缓存

    ## identity_prop —— 为什么需要它

    默认以 `name` 作为身份键（`mergeV([label, name])`）。对大多数类型没问题，
    但对 **name 可变**的类型是个结构性缺陷：

    EC2 的 name 来自 Name 标签（`collect_ec2_instances` 里
    `next((t['Value'] for t in tags if t['Key']=='Name'), instance_id)`）。
    标签随时可以加、改、删 —— 一旦变化，mergeV 匹配不到旧节点就**新建一个**，
    旧节点带着当时的属性永远孤立在图里。

    实测后果（2026-08-29）：14 个 EC2Instance 节点里 **4 个是重复实体** ——
    4 台 EKS 工作节点各有两份，一份以实例 ID 命名（那些实例还没打 Name 标签时建的，
    88 天前停止更新），一份以 Name 标签命名。任何「有几台工作节点」
    「哪些实例 RTT 高」的查询都会数两次，且其中一份带 3 个月前的陈旧值。

    注意**修 name 的取值规则防不住这件事** —— 那条规则本来就是对的
    （Name 标签优先、缺失回落 ID）。问题在于拿一个可变属性当身份。

    传入 identity_prop 后，以该属性（EC2 用不可变的 instance_id）匹配，
    name 降级为普通可变属性，跟着标签变化更新而不再产生新节点。

    Args:
        identity_prop: 用作身份键的属性名，必须存在于 extra_props 中。
                       为 None 时行为与原先**完全一致**（以 name 匹配），
                       保证其余 30 多种节点类型不受影响。
    """
    n = safe_str(name)
    mb = safe_str(managed_by)

    # ── 契约门禁 ──────────────────────────────────────────────────────────
    # 未在 profiles/graph_contract.yaml 声明的类型不得写入。引入之前任何拼错的
    # 标签都会被静默写进 Neptune（缺口 1）。GRAPH_CONTRACT_MODE=warn 可灰度。
    assert_node_type(label)

    all_props = {'environment': ENVIRONMENT}

    # ── scope：算不算「被观测系统」的一部分（T-306）────────────────────────
    # 为什么在 upsert 时写、而不是全靠 scripts/label_node_scope.py 事后标注：
    # 那个脚本是**回填与对账**工具，不是稳态机制。ETL 每跑一次就产生一批没有
    # scope 的新节点（实测：两个 petsite-deployment Pod 在标注跑完 20 分钟后出现），
    # 于是守门测试每次都变红 —— 而一个反复闪红的门禁会被无视，比没有门禁更糟。
    #
    # 这里只做**零外部调用**的那一半：K8s 对象在 upsert 时手上就有 namespace，
    # 查一次契约的 namespace_map 即可。需要查 CloudFormation 栈归属的那一半
    # （AWS 资源）仍由脚本定期对账 —— 在 Lambda 里对每个资源调
    # describe-stacks 既慢又会撞限流。
    _ns = (extra_props or {}).get('namespace')
    if _ns:
        _sc = NODE_SCOPE_NS_MAP.get(_ns)
        if _sc:
            all_props['scope'] = _sc

    fb_entry = FAULT_BOUNDARY_MAP.get(label)
    if fb_entry:
        fb_type, fb_region = fb_entry
        all_props['fault_boundary'] = fb_type
        if fb_region:
            all_props['region'] = fb_region
    all_props.update(extra_props)

    # ── 属性权威过滤 ──────────────────────────────────────────────────────
    # 契约的 node_attr_authority 是**例外清单**（只登记已实测出冲突的属性），
    # 未登记的一律放行。被拒的属性**明示 log** 而不是静默丢弃 —— 对应
    # ServiceNow IRE 的 maskedAttributes：不明示，「谁赢」永远查不清。
    all_props, masked = filter_node_props(label, all_props, DEFAULT_SOURCE)
    if masked:
        logger.warning(
            "upsert_vertex(%s, %s): 属性 %s 的权威来源不是 aws-etl，已拒绝写入。"
            "权威声明见 profiles/graph_contract.yaml 的 node_attr_authority。",
            label, n, masked)

    ts_now = int(time.time())

    # 身份键的选择。优先级：显式传入的 identity_prop > 契约声明 > name。
    #
    # 从契约派生是这一层的关键改动 —— 它让 profiles/graph_contract.yaml 真正
    # **载荷**：新增一个身份键非 name 的类型时，只改契约即自动生效，
    # 不必再逐个调用点补参数（那正是此前 Subnet / VPC / SecurityGroup /
    # TargetGroup 四处漏掉的原因）。调用点仍保留显式传参作为文档，
    # 由 test_35 的 g11 强制两者一致。
    #
    # identity_prop 缺失或其值为空时**回落到 name** 而不是抛错：
    # 一个拿不到 instance_id 的实例仍然应该进图谱，只是退回到旧的
    # （有缺陷的）身份语义，比整轮 ETL 失败好。
    id_key, id_val = 'name', n
    chosen = identity_prop or identity_prop_for(label)
    if chosen and chosen != 'name':
        raw = all_props.get(chosen)
        if raw not in (None, ''):
            id_key, id_val = safe_str(chosen), safe_str(raw)
        else:
            logger.warning(
                "upsert_vertex(%s): 契约声明身份键 %r 但其值为空，回落到以 name 匹配。"
                "该节点仍可能因 name 变化而产生重复。", label, chosen)

    # 节点 source 也过一遍词表门禁：现在只有一个取值，但硬编码字符串是漂移的起点
    # （eks-etl 当初就是这么进来的），走门禁则改坏了当场就失败。
    assert_source(DEFAULT_SOURCE, f'upsert_vertex(label={label})')
    props_create = f"'name': '{n}', 'managedBy': '{mb}', 'source': '{DEFAULT_SOURCE}'"
    props_match = f"'managedBy': '{mb}', 'source': '{DEFAULT_SOURCE}'"
    # 以 instance_id 为身份时，name 必须进 onMatch —— 否则标签改名后
    # 图谱里仍留着旧名字，等于只是把重复换成了陈旧。
    if id_key != 'name':
        props_match += f", 'name': '{n}'"
    for k, v in all_props.items():
        ks = safe_str(k)
        fv = _format_prop_val(k, v)
        props_create += f", '{ks}': {fv}"
        props_match  += f", '{ks}': {fv}"
    prop_chain = f".property(single,'managedBy','{mb}').property(single,'source','{DEFAULT_SOURCE}')"
    if id_key != 'name':
        prop_chain += f".property(single,'name','{n}')"
    for k, v in all_props.items():
        ks = safe_str(k)
        fv = _format_prop_val(k, v)
        prop_chain += f".property(single,'{ks}',{fv})"
    prop_chain += f".property(single,'last_updated',{ts_now})"
    # 契约声明的统一时间戳字段（timestamp_field）。
    #
    # 2026-09-04 实测：etl_aws 此前**只写 last_updated**，从不写 TIMESTAMP_FIELD，
    # 于是 1077 个节点里只有 15 个（1.4%）带 last_seen —— 而那 15 个全是 etl_cfn
    # 写的。后果有两层：
    #   ① 任何按 TIMESTAMP_FIELD 判定的机制（节点过期收敛）**结构上永不触发**，
    #      查询返回 0 条会伪装成「没有陈旧节点」，与真的干净完全同形
    #   ② 反过来，若改用 last_updated 当判据，会**误杀 etl_cfn 独家写的节点** ——
    #      实测 7 个 LambdaFunction 活着且被 etl_cfn 每日刷新（last_seen 新鲜），
    #      但 etl_aws 不碰它们，last_updated 已陈旧 >7 天
    # 所以统一字段必须由**每个**节点写入方都写，判据才成立。
    # last_updated 保留：存量查询与报告仍在用它，属过渡态。
    prop_chain += f".property(single,'{TIMESTAMP_FIELD}',{ts_now})"
    gremlin = (
        f"g.mergeV([(T.label): '{label}', '{id_key}': '{id_val}'])"
        f".option(Merge.onCreate, [(T.label): '{label}', {props_create}])"
        f".option(Merge.onMatch, [{props_match}])"
        f"{prop_chain}"
        f".id()"
    )
    result = neptune_query(gremlin)
    ids = result.get('result', {}).get('data', {}).get('@value', [])
    if ids:
        vid = ids[0]
        vid = extract_value(vid) if isinstance(vid, dict) else vid
        _vid_cache[(label, name)] = vid
        return vid
    return None


# 依赖语义边的标签集合。**从契约派生**，不再各 ETL 各存一份 ——
# 原先三个 ETL 各自复制一份同样的 frozenset，作者在注释里已标为待收敛项
# （「后续 graph SDK 收敛时统一」）。现在唯一来源是
# profiles/graph_contract.yaml 里每个边类型的 dependency 标记。
DEPENDENCY_EDGE_LABELS = dependency_edge_labels()


def upsert_edge(src_id, dst_id, label: str, props: dict = None):
    """upsert 边（by vertex ID）。

    ## 溯源属性写一次（write-once）

    `source` / `dependency_kind` / `first_seen` 只由**首个发现者**写入。
    原实现是无条件 `.property('source','aws-etl')` —— coalesce 命中一条已存在的
    边之后照写，会把 deepflow / xray 先写的 source 静默改成 aws-etl，
    等于抹掉「谁首先发现了这条依赖」。而 xray / deepflow-L4 / NFM 三个源本来就
    刻意保护这些属性，只有 aws 与 cfn 两处覆盖 —— 行为自相矛盾（缺口 3）。

    改法是 Gremlin 侧的 `coalesce(values(k), constant(v))`：属性已存在则保留原值，
    不存在才写入。新建边走 addE 时必然不存在，所以首写者照常写上。

    ## 调用方声明的 source 必须被采纳（2026-08-31 修）

    上面那版把「写一次」实现成了**丢弃调用方的取值**：

        write_once = {'source': 'aws-etl'}      # 硬编码
        for k, v in props.items():
            if ks in write_once: continue      # 调用方的 source 被跳过

    于是 handler.py 里 13 处 `{'source': 'eks-etl'}`、1 处 `'aws-etl-static'`
    **全部静默失效**，新建的边一律写成 `aws-etl`。实测确认：
    `upsert_edge(..., {'source': 'eks-etl'})` 生成的 Gremlin 里只有 `'aws-etl'`。

    活图谱之所以还能看到 1228 条 `eks-etl`，是因为 `coalesce` 保护了**存量**边 ——
    这个 bug 只影响此后新建的边，因此不会立刻暴露，只会让 provenance 缓慢腐坏。

    正确语义是两件事分开：
      · 写一次 = **已存在的边不覆盖**（由 coalesce 保证）
      · 取什么值 = **首写者说了算**，也就是调用方传进来的那个
    """
    if src_id is None or dst_id is None:
        return None
    lb = safe_str(label)

    # 契约门禁：未声明的边类型不得写入。端点类型这里拿不到（只有 vertex id），
    # 故不做端点核对 —— 端点约束由 test_35 的 g06 在契约层面保证。
    assert_edge_type(lb)

    props = dict(props or {})
    # 调用方可以声明这条边的发现者（K8s 采集路径用 eks-etl、静态声明用 aws-etl-static）。
    # 缺省才回落到 aws-etl。取值必须在契约词表内 —— 否则就是当初 eks-etl 那类
    # 「代码在写、契约没声明」的漂移。
    edge_source = props.pop('source', None) or DEFAULT_SOURCE
    assert_source(edge_source, f"upsert_edge(label={lb})")

    ts = int(time.time())
    write_once = {'source': edge_source}
    if is_dependency_edge(lb):
        # dependency_kind='static'：本 ETL 的边来自 AWS 资源配置「声明」的关系，
        # 而非运行时观测。与 deepflow 的 dynamic 相对，供 q1/q3 按类型过滤。
        write_once['dependency_kind'] = 'static'

    prop_str = ''
    for k, v in write_once.items():
        prop_str += (f".property('{k}', __.coalesce(__.values('{k}'),"
                     f" __.constant('{safe_str(v)}')))")
    # 统一时间戳字段（契约的 timestamp_field）。仍同时写 last_updated：
    # 读取侧还有查询在用它，两者并存是过渡态，摘除 last_updated 需要先迁读取方。
    prop_str += f".property('{TIMESTAMP_FIELD}', {ts}).property('last_updated', {ts})"

    if props:
        for k, v in props.items():
            ks = safe_str(k)
            # `source` 已在上面 pop 出来并作为首写值采纳，这里挡的是 `dependency_kind`
            # 与 `first_seen`：这两个由本函数按边类型判定，调用方不得直接指定，
            # 否则 static/dynamic 的语义会随调用点各说各话。
            if ks in write_once:
                continue
            vs = safe_str(v)
            prop_str += f".property('{ks}', '{vs}')"
    gremlin = (
        f"g.V('{src_id}').as('s').V('{dst_id}')"
        f".coalesce("
        f"  __.inE('{lb}').where(__.outV().hasId('{src_id}')),"
        f"  __.addE('{lb}').from('s')"
        f")"
        + prop_str
    )
    return neptune_query(gremlin)


def upsert_az_region(az: str, region: str = None) -> tuple:
    """upsert AvailabilityZone and Region nodes, and Region→AZ Contains edge."""
    if not az:
        return None, None
    r = region or REGION
    upsert_vertex('Region', r, {'region_name': r, 'provider': 'aws'}, 'aws')
    region_vid = get_vertex_id('Region', r)
    upsert_vertex('AvailabilityZone', az, {'az_name': az, 'region': r}, 'aws')
    az_vid = get_vertex_id('AvailabilityZone', az)
    if region_vid and az_vid:
        upsert_edge(region_vid, az_vid, 'Contains', {'source': 'aws-etl'})
    return az_vid, region_vid


def link_to_az(resource_vid, az: str):
    """Connect a resource node to its AZ node (LocatedIn edge)."""
    if not resource_vid or not az:
        return
    az_vid = get_vertex_id('AvailabilityZone', az)
    if az_vid:
        upsert_edge(resource_vid, az_vid, 'LocatedIn', {'source': 'aws-etl'})


def resolve_managed_by(tags: list) -> str:
    tag_dict = {t.get('Key', ''): t.get('Value', '') for t in (tags or [])}
    if tag_dict.get('aws:cloudformation:stack-name'):
        return 'cloudformation'
    if tag_dict.get('aws:eks:cluster-name') or tag_dict.get('eks:cluster-name'):
        return 'eks-managed'
    return 'manual'


def resolve_managed_by_dict(tags: dict) -> str:
    if not tags:
        return 'manual'
    if tags.get('aws:cloudformation:stack-name'):
        return 'cloudformation'
    if tags.get('aws:eks:cluster-name') or tags.get('eks:cluster-name'):
        return 'eks-managed'
    return 'manual'


def resolve_resource_tags(tags) -> dict:
    if isinstance(tags, list):
        td = {t.get('Key', ''): t.get('Value', '') for t in (tags or [])}
    elif isinstance(tags, dict):
        td = tags
    else:
        td = {}
    tier_raw = td.get('Tier', '') or td.get('tier', '')
    tier = tier_raw.capitalize() if tier_raw else None
    managed_by_tag = (td.get('ManagedBy') or td.get('managedby') or '').lower() or None
    return {
        'environment':    td.get('Environment') or None,
        'system':         td.get('System')       or None,
        'team':           td.get('Team')          or None,
        'tier':           tier,
        'managed_by_tag': managed_by_tag,
    }


def find_vertex_by_name(name: str, label: str):
    """按 **(label, name)** 查顶点 ID。`label` 是必需参数，刻意不给默认值。

    ## 为什么必须带 label（2026-08-30 修复）

    原实现两层都不带标签：

        for (label, cached_name), vid in _vid_cache.items():   # ← 丢弃 label
            if cached_name == name: return vid
        neptune_query(f"g.V().has('name','{name}').id().limit(1)")  # ← 也不带

    而本图里名字**跨标签重复**是常态 —— 实测 12 组，例如 `gateway-service`
    同时是 Deployment、K8sService 和 Microservice 三个节点。于是返回哪一个
    取决于 `_vid_cache` 的插入顺序，也就是 ETL 步骤的先后，**不确定**。

    实测后果（活图谱 2026-08-30）：
      - `Microservice-[RunsOn]->Pod` 正确的只有 36 条，
        而源端点错成 Namespace / K8sService / Deployment 的有 **173 条**。
        影响面分析从 Microservice 出发遍历 RunsOn 会漏掉大部分 Pod。
      - `Deployment-[Manages]->Microservice` 正确 6 条，错 10 条。
      - handler.py Step 8b-post 把本该写给 Microservice 的 `ip` 属性
        写到了 Deployment / K8sService 节点上；而它前一句只 drop
        `hasLabel('Microservice')` 的 ip，所以那些错写的值**永远清不掉**。

    把 label 设为必需参数（而不是可选带默认值）是刻意的：可选参数会让现有
    调用点继续静默走错误路径，缺陷照旧存在，只是多了一个没人用的正确入口。

    顺带把缓存查找从线性扫描改成 O(1) 直接取键。
    """
    assert_node_type(label)
    vid = _vid_cache.get((label, name))
    if vid:
        return vid
    try:
        result = neptune_query(
            f"g.V().hasLabel('{label}').has('name', '{safe_str(name)}').id().limit(1)"
        )
        ids = result.get('result', {}).get('data', {}).get('@value', [])
        if ids:
            vid = ids[0]
            return extract_value(vid) if isinstance(vid, dict) else vid
    except Exception:
        pass
    return None
