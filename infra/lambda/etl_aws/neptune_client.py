"""
neptune_client.py - Neptune Gremlin query utilities for neptune-etl-from-aws.

Base networking functions (neptune_query, safe_str, extract_value) are
imported from the shared Lambda Layer (neptune_client_base).
"""

import time
import logging

from neptune_client_base import neptune_query, safe_str, extract_value  # noqa: F401 (re-exported)
from config import (
    REGION,
    FAULT_BOUNDARY_MAP, ENVIRONMENT,
)

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
    all_props = {'environment': ENVIRONMENT}
    fb_entry = FAULT_BOUNDARY_MAP.get(label)
    if fb_entry:
        fb_type, fb_region = fb_entry
        all_props['fault_boundary'] = fb_type
        if fb_region:
            all_props['region'] = fb_region
    all_props.update(extra_props)
    ts_now = int(time.time())

    # 身份键的选择。identity_prop 缺失或其值为空时**回落到 name**，
    # 而不是抛错：一个拿不到 instance_id 的实例仍然应该进图谱，
    # 只是退回到旧的（有缺陷的）身份语义，比整轮 ETL 失败好。
    id_key, id_val = 'name', n
    if identity_prop:
        raw = all_props.get(identity_prop)
        if raw not in (None, ''):
            id_key, id_val = safe_str(identity_prop), safe_str(raw)
        else:
            logger.warning(
                "upsert_vertex(%s): identity_prop=%r 的值为空，回落到以 name 匹配。"
                "该节点仍可能因 name 变化而产生重复。", label, identity_prop)

    props_create = f"'name': '{n}', 'managedBy': '{mb}', 'source': 'aws-etl'"
    props_match = f"'managedBy': '{mb}', 'source': 'aws-etl'"
    # 以 instance_id 为身份时，name 必须进 onMatch —— 否则标签改名后
    # 图谱里仍留着旧名字，等于只是把重复换成了陈旧。
    if id_key != 'name':
        props_match += f", 'name': '{n}'"
    for k, v in all_props.items():
        ks = safe_str(k)
        fv = _format_prop_val(k, v)
        props_create += f", '{ks}': {fv}"
        props_match  += f", '{ks}': {fv}"
    prop_chain = f".property(single,'managedBy','{mb}').property(single,'source','aws-etl')"
    if id_key != 'name':
        prop_chain += f".property(single,'name','{n}')"
    for k, v in all_props.items():
        ks = safe_str(k)
        fv = _format_prop_val(k, v)
        prop_chain += f".property(single,'{ks}',{fv})"
    prop_chain += f".property(single,'last_updated',{ts_now})"
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


# 依赖语义边的标签集合。只有这些边代表「A 依赖 B」，才需要 dependency_kind；
# LocatedIn / Contains / BelongsTo 等结构边不是依赖，不打该标记。
# 规范定义见 profiles/petsite.yaml 的边类型声明。
# 三个 ETL 各自持有一份同样的常量：跨 Lambda 共享需要改 neptune-client-base layer
# 并同步升级 3 个函数，代价高于复制一个 3 元素集合；后续 graph SDK 收敛时统一。
DEPENDENCY_EDGE_LABELS = frozenset({'Calls', 'DependsOn', 'AccessesData'})


def upsert_edge(src_id, dst_id, label: str, props: dict = None):
    """upsert 边（by vertex ID）"""
    if src_id is None or dst_id is None:
        return None
    ts = int(time.time())
    lb = safe_str(label)
    prop_str = f".property('source', 'aws-etl').property('last_updated', {ts})"
    # dependency_kind='static'：本 ETL 的边来自 AWS 资源配置「声明」的关系，
    # 而非运行时观测。与 deepflow 的 dynamic 相对，供 q1/q3 按类型过滤，
    # 避免「模板里声明但从未调用的依赖」和「每秒数百次的真实调用」在影响面分析里等权。
    if lb in DEPENDENCY_EDGE_LABELS:
        prop_str += ".property('dependency_kind', 'static')"
    if props:
        for k, v in props.items():
            ks = safe_str(k)
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


def find_vertex_by_name(name: str):
    for (label, cached_name), vid in _vid_cache.items():
        if cached_name == name:
            return vid
    try:
        result = neptune_query(f"g.V().has('name', '{name}').id().limit(1)")
        ids = result.get('result', {}).get('data', {}).get('@value', [])
        if ids:
            vid = ids[0]
            return extract_value(vid) if isinstance(vid, dict) else vid
    except Exception:
        pass
    return None
