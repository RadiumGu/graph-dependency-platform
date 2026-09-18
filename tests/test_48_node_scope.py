"""T-306：节点 scope 六档（第五个正交维度）。

scope 回答的是前四维都不回答的那个问题：**这个节点算不算「被观测系统」的一部分？**

缺它的后果实测到了：16/16 全部 `Invokes` 边被靶点选择器当依赖边选中，其中 13 条
其实是 CDK 部署脚手架与本平台自己的工具链。靶点、爆炸半径、DR 计划都只该看
被观测系统，但图里此前没有任何字段能表达这件事。
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
LAYER = REPO / 'infra' / 'lambda' / 'shared' / 'python'
SCRIPTS = REPO / 'scripts'
CHAOS = REPO / 'chaos' / 'code'
for p in (LAYER, SCRIPTS, CHAOS):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

LIVE = (os.environ.get('GRAPH_LIVE_AUDIT') or '').strip().lower() == 'true'


@pytest.fixture(scope='module')
def ns():
    from graph_contract_data import NODE_SCOPE
    return NODE_SCOPE


# ── 契约声明 ─────────────────────────────────────────────────────────────────

def test_t306_01_six_tiers_plus_unknown(ns):
    """六档 + 一个 unknown，不多不少。"""
    assert ns['values'] == ['observed', 'observability', 'platform', 'scaffolding',
                            'cluster-infra', 'external', 'unknown']
    assert ns['unresolved_value'] == 'unknown'


def test_t306_02_observability_is_separate_from_platform(ns):
    """`observability` 必须与 `platform` 分开，不得合并。

    前者是被观测系统的**观测者**（采集栈），后者是**本依赖图谱平台**。
    合并就分不清「谁在观测」与「谁在管依赖图」，而观测自噪声治理
    （曾从 73.2% 压到 4.6%）针对的正是前者。
    """
    m = ns['namespace_map']
    obs_ns = {k for k, v in m.items() if v == 'observability'}
    plat_ns = {k for k, v in m.items() if v == 'platform'}
    assert obs_ns and plat_ns and not (obs_ns & plat_ns)
    assert 'deepflow' in obs_ns and 'amazon-cloudwatch' in obs_ns
    assert 'chaos-mesh' in plat_ns, 'chaos-mesh 是本平台的注入工具，不是观测栈'


def test_t306_03_cluster_infra_not_folded_into_external(ns):
    """`cluster-infra` 不得并入 `external`。

    否则 CoreDNS / kube-proxy / aws-node 这类**真依赖**会被误判成外部系统，
    爆炸半径分析会漏掉它们。
    """
    assert ns['namespace_map']['kube-system'] == 'cluster-infra'
    assert 'cluster-infra' in ns['values']


def test_t306_04_nested_stack_rule_is_name_independent(ns):
    """嵌套栈判据必须与栈名无关。

    CDK 把 provider framework 放进独立嵌套栈是它的架构事实，靠 ParentId 判定；
    靠名字（匹配 'NestedStack' 或 'awscdkaws'）会误判 ——
    实测 `ServicesEks2-GuardDutyCleanupLambda` 名字像脚手架，
    但它在**主栈**里、属被观测系统。
    """
    assert ns['nested_stack_scope'] == 'scaffolding'
    assert 'cloudformation-stack-membership' in ns['resolvers']
    # 判据里不得出现名字模式
    for r in ns['resolvers']:
        assert 'name-prefix' not in r and 'name-pattern' not in r


def test_t306_05_resolver_order_puts_strong_criteria_first(ns):
    """解析顺序：namespace → 栈归属 → 节点类型 → profile 声明（最弱，只兜底）。

    profile 是**片段匹配**，比栈归属弱。让它抢先会覆盖掉精确的栈归属判定。
    """
    r = ns['resolvers']
    assert r.index('k8s-namespace') < r.index('cloudformation-stack-membership')
    assert r.index('cloudformation-stack-membership') < r.index('profile-declaration')
    assert r[-1] == 'profile-declaration', 'profile 片段匹配必须排最后'


def test_t306_06_primary_query_scope_is_observed(ns):
    """靶点选择 / 爆炸半径 / DR 计划只该看 `observed`。"""
    assert ns['primary_query_scope'] == 'observed'
    assert ns['primary_query_scope'] in ns['values']


# ── 标注脚本 ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope='module')
def labeler():
    import label_node_scope
    return label_node_scope


def test_t306_07_script_reads_vocabulary_from_contract(labeler, ns):
    """脚本不得自己抄一份词表。

    这个项目已经因为「同一判据两份互相分歧的实现」踩过一次
    （verify_confidence 的 ±4.0 与 0.0 都出自那个根因），不重犯。
    """
    assert tuple(ns['values']) == labeler.SCOPE_VALUES
    assert labeler.NS_SCOPE == ns['namespace_map']
    assert labeler.STACK_SCOPE == ns['stack_map']
    assert labeler.UNRESOLVED == ns['unresolved_value']


def test_t306_08_no_arn_backfill_required(labeler):
    """判据不得依赖补 `arn`。

    `PhysicalResourceId` 对这些资源用的就是节点已有的标识
    （SecurityGroup=sg-xxx / VPC=vpc-xxx / Subnet=subnet-xxx / S3Bucket=桶名）。
    补 arn 会制造第二个身份键，正是本项目踩过多次的「身份不唯一」那一类。
    """
    keys = labeler.PHYSICAL_ID_KEYS
    for k in ('name', 'sg_id', 'subnet_id', 'vpc_id', 'instance_id'):
        assert k in keys, f'缺少已有身份键 {k}，会退化成必须补 arn'


def test_t306_09_unresolved_is_explicit_never_defaulted(labeler):
    """解析不出必须显式返回 unknown，不得默认成任何一档。

    与「不分级时写 `unclassified` 而非留空」同一条教训：属性缺失与
    「判过但判不出」在查询上无法区分。
    """
    node = {'label': 'SomeTypeNobodyRegistered',
            'props': {'name': 'zzz-nothing-matches-this-name'}}
    sc, why = labeler.resolve_scope(node, {})
    assert sc == labeler.UNRESOLVED and why


def test_t306_10_namespace_beats_stack_membership(labeler):
    """namespace 优先于栈归属。

    反过来会让 kube-system 里的东西被栈归属误判 —— 一个 kube-system 的 Pod
    可能跑在被观测系统的栈创建的节点上。
    """
    node = {'label': 'Pod', 'props': {'namespace': 'kube-system', 'name': 'kube-proxy-x'}}
    sc, why = labeler.resolve_scope(node, {'kube-proxy-x': 'observed'})
    assert sc == 'cluster-infra' and 'namespace' in why


# ── 选边器接入（T-306）───────────────────────────────────────────────────────

def test_t306_12_selector_excludes_non_observed_scopes(ns):
    """选边器必须排除触及 platform / scaffolding / observability / cluster-infra 的边。

    `platform` 那一档是**安全问题**不只是噪声：12 条 platform→platform 边是本平台
    自己的 ETL/RCA 链，往那里注入可能打断记录本次实验判定的那条管道 ——
    neptune-etl-* 挂了，这次实验的结论就写不回图。
    """
    from runner import edge_verification as ev
    assert ev.EXCLUDED_SCOPES == {'platform', 'scaffolding', 'observability', 'cluster-infra'}
    # observed 与 unknown 都不在排除集里
    assert 'observed' not in ev.EXCLUDED_SCOPES
    assert ns['unresolved_value'] not in ev.EXCLUDED_SCOPES


def test_t306_13_unknown_scope_is_not_excluded(ns):
    """`unknown` 不得被排除 —— 「没标注」不等于「不该打」。

    活图谱 110 条依赖边里只有 50 条两端都是 observed，另有 34 条一端是 unknown
    （端点解析不出，其中有真的被观测依赖）。要求两端 observed 会连带丢掉这 34 条，
    那是把「解析不出」当成「不该打」。
    """
    from runner import edge_verification as ev
    import inspect
    src = inspect.getsource(ev.select_targets_for_verification)
    assert 'EXCLUDED_SCOPES' in src, '选边函数没有接 scope 过滤'
    assert "__.constant('unknown')" in src, 'scope 缺失应回落 unknown 而不是排除'
    # 判据必须是「触及排除档」而不是「要求两端 observed」
    assert "== 'observed'" not in src, '不得要求两端都是 observed'


def test_t306_14_scope_filter_runs_before_capability_matrix(ns):
    """scope 过滤必须在能力矩阵之前。

    先问「该不该打」再问「能不能打」：一条 platform→platform 边即使完全可注入，
    也不该打。顺序反了只是白算一遍矩阵，但语义上「不该打」应当优先。
    """
    from runner import edge_verification as ev
    import inspect
    src = inspect.getsource(ev.select_targets_for_verification)
    i_scope = src.find('EXCLUDED_SCOPES')
    i_matrix = src.find('should_skip_target')
    assert i_scope != -1 and i_matrix != -1
    assert i_scope < i_matrix, 'scope 过滤应在能力矩阵之前'


@pytest.mark.skipif(not LIVE, reason='需 GRAPH_LIVE_AUDIT=true 才对活图谱核验')
def test_t306_15_no_excluded_scope_edge_is_ever_selected():
    """活图谱实跑：选出的靶点里不得出现任何被排除档的端点。"""
    from runner import edge_verification as ev
    targets = ev.select_targets_for_verification(limit=500)
    bad = [f"{t.get('src')}->{t.get('dst')}"
           for t in targets
           if {str(t.get('src_scope')), str(t.get('dst_scope'))} & ev.EXCLUDED_SCOPES]
    assert not bad, f"这些被排除档的边仍被选为靶点：{bad[:10]}"


# ── 活图谱 ───────────────────────────────────────────────────────────────────

@pytest.mark.skipif(not LIVE, reason='需 GRAPH_LIVE_AUDIT=true 才对活图谱核验')
def test_t306_11_every_node_has_a_legal_scope(ns):
    """全图每个节点都必须有 scope，且取值在词表内 —— **带宽限窗口**。

    ## 为什么要宽限窗口

    scope 有两个来源，能力不同：
      · **写入方就地写**：K8s 对象在 upsert 时手上就有 namespace，查一次
        契约的 namespace_map 即可（零外部调用）。
      · **对账脚本定期写**：AWS 资源要查 CloudFormation 栈归属，在 Lambda 里
        对每个资源调 describe-stacks 既慢又会撞限流，只能由
        `scripts/label_node_scope.py` 周期性补。

    所以「ETL 刚写进来、对账还没跑」是**正常中间态**，不是缺陷。第一版没有宽限
    窗口，实测每跑一轮 ETL（15 分钟一次）就有新节点让这条断言变红 ——
    而**一个反复闪红的门禁会被无视，比没有门禁更糟**。

    宽限窗口取 `SCOPE_GRACE_SECONDS`（默认 30 分钟 = 两个 ETL 周期）：
    比这更老还缺 scope 的节点，说明对账真的没覆盖到它，那才是缺陷。
    """
    import time
    spec = importlib.util.spec_from_file_location(
        '_nc_for_scope_audit', LAYER / 'neptune_client_base.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    grace = int(os.environ.get('SCOPE_GRACE_SECONDS', str(30 * 60)))
    cutoff = int(time.time()) - grace

    legal = ",".join(f"'{v}'" for v in ns['values'])
    # 只查「比宽限窗口更老」的节点。两个时间戳字段都取，因为本仓库存在
    # 时间戳字段并存的过渡态（见契约 timestamp_legacy_aliases）。
    q = (f"g.V().or(__.hasNot('scope'), __.not(__.has('scope', P.within({legal}))))"
         f".not(__.or(__.has('last_updated', P.gt({cutoff})),"
         f"           __.has('last_seen', P.gt({cutoff}))))"
         f".project('l','n','ts').by(__.label())"
         f".by(__.coalesce(__.values('name'), __.constant('<无名>')))"
         f".by(__.coalesce(__.values('last_updated'), __.values('last_seen'),"
         f"    __.constant(0))).fold()")
    raw = mod.neptune_query(q)['result']['data']['@value'][0]['@value']
    bad = []
    for r in raw:
        it = iter(r['@value'])
        d = dict(zip(it, it))
        gv = lambda k: d[k]['@value'] if isinstance(d[k], dict) else d[k]  # noqa: E731
        bad.append(f"{gv('l')}/{gv('n')}(ts={gv('ts')})")
    assert not bad, (
        f"这些节点缺 scope 或取值非法，且已超过 {grace}s 宽限窗口："
        f"{bad[:20]}（共 {len(bad)}）。"
        f"跑 scripts/label_node_scope.py --apply 补齐；若某类节点反复出现，"
        f"说明它的写入方该在 upsert 时就写 scope。")
