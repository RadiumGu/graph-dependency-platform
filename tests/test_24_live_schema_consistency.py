"""
test_24_live_schema_consistency.py — profiles/petsite.yaml ↔ **活图** 一致性校验

## 为什么需要这个测试

已有的 tests/test_11_schema_consistency.py 校验的是**代码之间**的一致性,
不校验 YAML 与 Neptune 活图。而本项目的核心设计目标是
「图数据库作为依赖关系的唯一源头」,现实是形成了**多源头**:

  - profiles/petsite.yaml            services.*.tier
  - infra/lambda/etl_aws/business_config.json  microservice_recovery_priority
  - AWS 资源 tag                     Tier=tierN
  - 活图                             Microservice.recovery_priority

本测试是把「多源头」变回「单源头」的**最低成本方案** —— 不强行合并这几份配置,
但保证它们不静默漂移。2026-08-28 首次运行即抓到 petstatusupdater 的
YAML=Tier2 / 其余三源=Tier1 漂移。

## 断言方向是刻意不对称的

  活图有、YAML 未声明  → **硬失败**
      schema_prompt 只把 YAML 的 graph_schema_text 喂给 LLM，
      未声明的类型对自然语言查询是隐形的，属真实缺陷。
  YAML 声明、活图暂无  → **只告警**
      某些类型可能只在 DR 演练或特定场景下才被实例化，缺席未必是错。

## 运行条件

需要 VPC 内网络 + neptune-db SigV4 权限。取不到活图时 **skip 而非 fail**，
否则没有 VPC 访问权的开发者本地跑测试会全红。
"""

import os
import re
import sys

import pytest
import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILE_PATH = os.path.join(PROJECT_ROOT, 'profiles', 'petsite.yaml')
BUSINESS_CONFIG = os.path.join(
    PROJECT_ROOT, 'infra', 'lambda', 'etl_aws', 'business_config.json'
)

if os.path.join(PROJECT_ROOT, 'rca') not in sys.path:
    sys.path.insert(0, os.path.join(PROJECT_ROOT, 'rca'))


# ─── 夹具 ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def profile_data():
    with open(PROFILE_PATH, encoding='utf-8') as f:
        return yaml.safe_load(f)


@pytest.fixture(scope='module')
def declared_types(profile_data):
    """从 graph_schema_text 解析声明的节点与边类型。"""
    txt = (profile_data.get('neptune', {}) or {}).get('graph_schema_text', '') or ''
    if not txt:
        pytest.skip('profiles/petsite.yaml 未提供 neptune.graph_schema_text')
    # 节点：形如 "- NodeType: field(type), ..."
    nodes = set(re.findall(r'^\s*-\s+([A-Z][A-Za-z0-9]*)\s*:', txt, re.M))
    # 边：形如 "-[:EdgeType]->"，需拆开 A|B 与 :A|:B 两种写法
    edges = set()
    for raw in re.findall(r'-\[:([A-Za-z][A-Za-z0-9|:]*)\]', txt):
        for part in raw.replace(':', '|').split('|'):
            if part:
                edges.add(part)
    return {'nodes': nodes, 'edges': edges}


@pytest.fixture(scope='module')
def live_graph():
    """查活图的类型集合与服务 tier。连不上则 skip。"""
    try:
        from neptune import neptune_client as nc
    except Exception as e:
        pytest.skip(f'无法导入 neptune_client（缺依赖？）: {e}')

    try:
        node_rows = nc.results('MATCH (n) UNWIND labels(n) AS l RETURN DISTINCT l')
        edge_rows = nc.results('MATCH ()-[r]->() RETURN DISTINCT type(r) AS t')
        svc_rows = nc.results(
            'MATCH (n:Microservice) WHERE exists(n.recovery_priority) '
            'RETURN n.name AS name, n.recovery_priority AS tier'
        )
    except Exception as e:
        pytest.skip(f'活图不可达（需 VPC 内网络 + neptune-db 权限）: {e}')

    if not node_rows:
        pytest.skip('活图为空，无可校验内容')

    return {
        'nodes': {r['l'] for r in node_rows},
        'edges': {r['t'] for r in edge_rows},
        'services': {r['name']: r['tier'] for r in svc_rows},
    }


# ─── 类型集合一致性 ────────────────────────────────────────────────────────

def test_no_undeclared_node_types(declared_types, live_graph):
    """活图里出现但 schema 未声明的节点类型 → 对 NL 查询隐形，硬失败。"""
    undeclared = sorted(live_graph['nodes'] - declared_types['nodes'])
    assert not undeclared, (
        f'活图存在 {len(undeclared)} 种未在 profiles/petsite.yaml 声明的节点类型: '
        f'{undeclared}。schema_prompt 只把已声明的类型喂给 LLM，'
        f'未声明的类型无法被自然语言查询触达。'
    )


def test_no_undeclared_edge_types(declared_types, live_graph):
    """活图里出现但 schema 未声明的边类型 → 同上，硬失败。"""
    undeclared = sorted(live_graph['edges'] - declared_types['edges'])
    assert not undeclared, (
        f'活图存在 {len(undeclared)} 种未在 profiles/petsite.yaml 声明的边类型: '
        f'{undeclared}。'
    )


def test_declared_but_absent_types_are_reported(declared_types, live_graph, recwarn):
    """schema 声明但活图暂无的类型：只告警，不失败。

    某些类型可能只在 DR 演练或特定故障场景下才被实例化。
    """
    absent_nodes = sorted(declared_types['nodes'] - live_graph['nodes'])
    absent_edges = sorted(declared_types['edges'] - live_graph['edges'])
    if absent_nodes or absent_edges:
        import warnings
        warnings.warn(
            f'schema 声明但活图暂无 —— 节点 {absent_nodes}，边 {absent_edges}。'
            f'若长期缺席，考虑从 schema 移除以免误导 NL 查询。',
            UserWarning,
        )
    # 该测试本身永远通过：它的作用是留痕，不是把关
    assert True


# ─── 服务 tier 一致性（这是抓到真实漂移的那条）────────────────────────────

def test_service_tier_matches_live_graph(profile_data, live_graph):
    """YAML 的 services.*.tier 必须与活图 Microservice.recovery_priority 一致。

    2026-08-28 首次运行抓到:petstatusupdater YAML=Tier2 而
    AWS 资源 tag / business_config.json / 活图 三者均为 Tier1。已修正 YAML。
    """
    declared = {k: v.get('tier') for k, v in (profile_data.get('services', {}) or {}).items()}
    live = live_graph['services']

    mismatches = []
    for name, tier in sorted(declared.items()):
        live_tier = live.get(name)
        if live_tier is None:
            continue  # 活图暂无该节点，由下一个测试单独报告
        if str(tier) != str(live_tier):
            mismatches.append(f'{name}: YAML={tier} 活图={live_tier}')

    assert not mismatches, (
        '服务 tier 在 YAML 与活图之间漂移:\n  ' + '\n  '.join(mismatches) +
        '\n判定依据优先级:AWS 资源 tag > business_config.json > 活图 > 本 YAML。'
    )


def test_declared_services_exist_in_live_graph(profile_data, live_graph):
    """YAML 声明的服务应当在活图中存在（否则 profile 引用了不存在的服务）。"""
    declared = set((profile_data.get('services', {}) or {}).keys())
    missing = sorted(declared - set(live_graph['services'].keys()))
    assert not missing, (
        f'profiles/petsite.yaml 声明的服务在活图中不存在: {missing}。'
        f'可能是服务名漂移（如把 alias 当规范名），或该服务已下线但 profile 未更新。'
    )


def test_tier_consistent_with_business_config(profile_data):
    """YAML 的 tier 必须与 etl_aws/business_config.json 一致。

    后者是 etl_aws 写入活图 recovery_priority 的直接来源，
    两份配置不一致会导致「改了 YAML 但图没变」这类困惑。
    """
    import json
    if not os.path.exists(BUSINESS_CONFIG):
        pytest.skip(f'{BUSINESS_CONFIG} 不存在')
    with open(BUSINESS_CONFIG, encoding='utf-8') as f:
        bc = json.load(f)
    bc_tiers = bc.get('microservice_recovery_priority', {}) or {}
    if not bc_tiers:
        pytest.skip('business_config.json 未提供 microservice_recovery_priority')

    declared = {k: v.get('tier') for k, v in (profile_data.get('services', {}) or {}).items()}
    mismatches = [
        f'{name}: petsite.yaml={tier} business_config.json={bc_tiers[name]}'
        for name, tier in sorted(declared.items())
        if name in bc_tiers and str(tier) != str(bc_tiers[name])
    ]
    assert not mismatches, (
        'tier 在两份配置之间漂移:\n  ' + '\n  '.join(mismatches)
    )
