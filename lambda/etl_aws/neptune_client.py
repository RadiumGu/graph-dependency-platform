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


def upsert_vertex(label: str, name: str, extra_props: dict, managed_by: str = 'manual'):
    """upsert 节点，返回 vertex ID 并写入缓存"""
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
    props_create = f"'name': '{n}', 'managedBy': '{mb}', 'source': 'aws-etl'"
    props_match = f"'managedBy': '{mb}', 'source': 'aws-etl'"
    for k, v in all_props.items():
        ks = safe_str(k); vs = safe_str(v)
        props_create += f", '{ks}': '{vs}'"
        props_match  += f", '{ks}': '{vs}'"
    prop_chain = f".property(single,'managedBy','{mb}').property(single,'source','aws-etl')"
    for k, v in all_props.items():
        ks = safe_str(k); vs = safe_str(v)
        prop_chain += f".property(single,'{ks}','{vs}')"
    prop_chain += f".property(single,'last_updated',{ts_now})"
    gremlin = (
        f"g.mergeV([(T.label): '{label}', 'name': '{n}'])"
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


def upsert_edge(src_id, dst_id, label: str, props: dict = None):
    """upsert 边（by vertex ID）"""
    if src_id is None or dst_id is None:
        return None
    ts = int(time.time())
    lb = safe_str(label)
    prop_str = f".property('source', 'aws-etl').property('last_updated', {ts})"
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
