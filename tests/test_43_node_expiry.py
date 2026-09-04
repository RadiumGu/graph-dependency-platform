"""节点过期收敛的守门测试 —— 填掉「生命周期跟随节点」这句话的实现空缺。

## 这个缺口是怎么暴露的

契约里结构边（LocatedIn / Contains / BelongsTo…）的 `expires_seconds: None`
注解写的是「生命周期跟随两端节点、不独立过期」，对应 Dynatrace 的
static edge 继承 node lifetime 语义。但节点侧**根本没有生命周期机制**，
所以那句话在实现上是悬空的：边不过期，节点也不过期，谁都不会消失。

2026-09-04 实测代价：

    Pod        图谱 581 个节点   集群实际 Running 70 个
               511 个（88%）超过 1 天未刷新，313 个超过 7 天
    SecurityGroup  56 个里 8 个超过 7 天未刷新
    Subnet         16 个里 1 个超过 30 天未刷新

任何「这个服务跑在哪些 Pod 上」「这个实例挂了哪些安全组」的查询都会拖出
一堆早已消失的对象。

## 本组测试守的三件事

  n01–n03  每个节点类型必须有**显式**的过期策略（数值或带理由的 null），
           不允许沉默省略 —— 省略与「忘了写」无法区分
  n04–n06  判据字段必须真的被写入方写。这是本轮最要紧的一条：
           TIMESTAMP_FIELD 在节点上的覆盖率实测只有 1.4%，
           建在它上面的机制会**结构上永不触发**
  n07–n09  执行器语义：默认 dry-run、软过期不删除、
           **「不可判定」必须与「已判定为新鲜」分开上报**

## 一个必须避开的陷阱

判据用 TIMESTAMP_FIELD（last_seen）而**不能**用 last_updated，即便后者
覆盖率高得多（78.8% vs 1.4%）。理由是实测出来的：7 个 LambdaFunction
活得很好、由 etl_cfn 每日刷新（last_seen 新鲜），但 etl_aws 不碰它们，
它们的 last_updated 已陈旧 7 天以上。**按 last_updated 判会误杀活节点。**
统一字段的意义正在这里 —— 它必须由每个写入方都写，判据才成立。
"""
from __future__ import annotations

import pathlib
import re
import sys

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[1]
LAYER = REPO / 'infra' / 'lambda' / 'shared' / 'python'
CONTRACT_YAML = REPO / 'profiles' / 'graph_contract.yaml'

if str(LAYER) not in sys.path:
    sys.path.insert(0, str(LAYER))


@pytest.fixture(scope='module')
def contract() -> dict:
    return yaml.safe_load(CONTRACT_YAML.read_text())


@pytest.fixture(scope='module')
def gc():
    import graph_contract
    return graph_contract


@pytest.fixture(scope='module')
def cleanup():
    import graph_cleanup
    return graph_cleanup


# ── n01–n03 过期策略必须显式声明 ──────────────────────────────────────────

def test_n01_every_node_type_declares_an_expiry_policy(contract):
    """每个节点类型都必须显式声明 expires_seconds（可以是 null）。

    沉默省略与「忘了写」在数据上无法区分，而两者的后果相反：前者是刻意的
    永不过期，后者是一个本该被回收的类型悄悄留在图里。
    """
    missing = [n for n, v in contract['node_types'].items()
               if 'expires_seconds' not in v]
    assert not missing, f"这些节点类型没有声明 expires_seconds: {missing}"


def test_n02_never_expiring_types_must_justify_it(contract, gc):
    """expires_seconds: null 必须同时给出 expiry_note 说明理由。

    合法的理由只有两类：追加式事件日志（Incident / ChaosExperiment /
    TopologyChange —— 历史证据不该消失），以及来自 json 的**声明而非观测值**
    （Microservice / BusinessCapability —— 删声明才该消失，不刷新不代表消失）。
    """
    silent = [n for n, v in contract['node_types'].items()
              if v.get('expires_seconds') is None and not gc.node_expiry_note_for(n)]
    assert not silent, (
        f"这些类型声明了永不过期但没写理由: {silent}。"
        f"没有理由的 null 与「忘了填」无法区分。")


def test_n03_ttl_exceeds_the_slowest_writer_cadence(contract):
    """TTL 必须显著大于最慢写入方的调度周期。

    实测周期：etl_cfn 是 **每天一次**（cron(0 18 * * ? *)），另有 15 分钟 /
    5 分钟 / 每小时三条。而 etl_xray 曾因层缺 requests **连死两天**——
    所以窗口必须能扛住多日中断，否则一次 ETL 故障会把整图判成过期。

    下限取 3 天（etl_cfn 的 3 个周期、并留出两天中断的余量）。
    """
    MIN_TTL = 3 * 86400
    too_short = {n: v['expires_seconds'] for n, v in contract['node_types'].items()
                 if v.get('expires_seconds') and v['expires_seconds'] < MIN_TTL}
    assert not too_short, (
        f"这些类型的 TTL 短于 {MIN_TTL}s（3 天），一次多日 ETL 中断就会误判: "
        f"{too_short}")


# ── n04–n06 判据字段必须真的被写入 ────────────────────────────────────────

def test_n04_expiry_judges_by_the_declared_timestamp_field(cleanup, contract):
    """执行器必须以契约声明的 timestamp_field 为判据，不得改用 last_updated。

    这条是实测教训的固化：last_updated 覆盖率 78.8%、last_seen 只有 1.4%，
    看起来该用前者 —— 但 7 个 LambdaFunction 由 etl_cfn 独家刷新，
    last_seen 新鲜而 last_updated 已陈旧 7 天以上，按 last_updated 判会**误杀活节点**。
    """
    ts_field = contract['timestamp_field']
    q = cleanup._node_count_query('Pod', 12345)
    assert f"'{ts_field}'" in q, f"过期判定查询没有用 timestamp_field={ts_field}: {q}"
    assert 'last_updated' not in q, (
        "过期判定不得用 last_updated —— 它不是统一字段，"
        "会误杀由其它 ETL 刷新的节点")


def test_n05_every_node_writer_writes_the_timestamp_field():
    """每个写节点的 ETL 都必须写 TIMESTAMP_FIELD，否则判据对它写的节点恒不成立。

    2026-09-04 实测：etl_aws 的 upsert_vertex **只写 last_updated**，
    于是 1077 个节点里只有 15 个带 last_seen（1.4%），全是 etl_cfn 写的。
    在那个状态下节点过期收敛会对 98.6% 的节点什么都不判，而 count 返回 0
    会被读成「图谱很干净」。
    """
    writers = {
        'etl_aws': REPO / 'infra' / 'lambda' / 'etl_aws' / 'neptune_client.py',
        'etl_cfn': REPO / 'infra' / 'lambda' / 'etl_cfn' / 'neptune_etl_cfn.py',
    }
    bad = []
    for name, path in writers.items():
        src = path.read_text()
        # 顶点写入路径里必须出现 TIMESTAMP_FIELD 的 property 写入
        if 'TIMESTAMP_FIELD' not in src:
            bad.append(f"{name}: 源码里没有 TIMESTAMP_FIELD")
    assert not bad, (
        f"这些节点写入方没有写统一时间戳字段: {bad}。"
        f"节点过期收敛对它们写的节点会恒不成立。")


def test_n06_unjudgeable_nodes_are_counted_separately(cleanup):
    """缺判据字段的节点必须单独计数，不能混进「新鲜」里。

    这是全项目反复踩的那类假健康 fallback 在本模块的对应物：
    查询返回 0 条与「真的没有陈旧节点」完全同形。执行器必须把
    「本轮对多少个节点什么都没判」显式报出来。
    """
    q = cleanup._node_unjudgeable_query('Pod')
    assert 'not(' in q.replace(' ', '') or '.not(' in q, \
        f"不可判定查询应当筛出**缺**该字段的节点: {q}"

    calls = []

    def fake_query(gremlin):
        calls.append(gremlin)
        # 陈旧 0 个、不可判定 500 个 —— 正是 2026-09-04 的真实形态
        n = 500 if 'not(' in gremlin and 'active' not in gremlin else 0
        return {'result': {'data': {'@value': [n]}}}

    out = cleanup.expire_stale_nodes(fake_query, round_ts=10**9, only_labels={'Pod'})
    assert out['per_label']['Pod']['stale'] == 0
    assert out['per_label']['Pod']['unjudgeable'] == 500, \
        "不可判定的节点数必须被单独报出，否则 stale=0 会伪装成图谱干净"
    assert out['unjudgeable_total'] == 500


# ── n07–n09 执行器语义 ────────────────────────────────────────────────────

def test_n07_disabled_by_default(cleanup, monkeypatch):
    """默认不改写 —— 与边过期收敛一致，且两个开关必须分开。"""
    monkeypatch.delenv('GRAPH_NODE_EXPIRY_ENABLED', raising=False)
    assert cleanup.node_expiry_enabled() is False
    # 边的开关不得连带打开节点侧：节点置 active=false 会影响一切遍历，
    # 风险面比边大，必须能独立灰度
    monkeypatch.setenv('GRAPH_EDGE_EXPIRY_ENABLED', 'true')
    assert cleanup.node_expiry_enabled() is False, \
        "节点过期不得由边的开关连带打开 —— 两者风险面不同"


def test_n08_dry_run_reports_but_does_not_write(cleanup, monkeypatch):
    monkeypatch.delenv('GRAPH_NODE_EXPIRY_ENABLED', raising=False)
    seen = []

    def fake_query(gremlin):
        seen.append(gremlin)
        return {'result': {'data': {'@value': [7]}}}

    out = cleanup.expire_stale_nodes(fake_query, round_ts=10**9, only_labels={'Pod'})
    assert out['enabled'] is False
    assert out['per_label']['Pod']['stale'] == 7
    assert out['per_label']['Pod']['expired'] == 0, 'dry-run 不得改写'
    assert not any('property' in g for g in seen), \
        f"dry-run 发出了写入查询: {[g for g in seen if 'property' in g]}"


def test_n09_soft_expire_never_drops(cleanup):
    """过期是软标记，绝不物理删除 —— 删节点会连带删边，一次误判不可逆。

    真要物理删除走 infra/reap_stale_nodes.py，那条路要求
    「向源端实查一遍清单」这个更强的前提（实测 4 个「看起来都死了」的
    TargetGroup 里有 1 个活着，只按陈旧度删会销毁活资源的节点）。
    """
    q = cleanup._node_deactivate_query('Pod', 12345)
    assert 'drop()' not in q, f"过期查询不得包含 drop(): {q}"
    assert "'active', false" in q or "'active',false" in q
    assert 'expired_at' in q, '过期必须留下时间戳，否则无法回溯是哪一轮判的'


def test_n10_reaper_reads_skip_prefixes_from_etl_config():
    """物理回收脚本的 skip 前缀必须从 etl_aws/config.py 读，不得硬写。

    硬写会与真实策略漂移，而漂移方向必然是脚本删掉策略其实想保留的东西。
    """
    src = (REPO / 'infra' / 'reap_stale_nodes.py').read_text()
    assert 'import config' in src and 'SKIP_TG_PREFIXES' in src, \
        '回收脚本应从 etl_aws 的 config 读取 skip 前缀'
    assert not re.search(r"SKIP_TG_PREFIXES\s*=\s*\(", src), \
        '回收脚本里不得自己定义 SKIP_TG_PREFIXES —— 那就是漂移的起点'
