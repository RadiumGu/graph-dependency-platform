"""契约元字段的守门测试 —— 让 immutable / scope_note / preferred 不是注释。

2026-09-04 的审计发现：33 个节点类型的身份键本身全绿（活图谱里 100% 存在且
唯一），但围绕身份键的三个元字段**全部零消费方**：

  · immutable   —— 唯一的读取函数 `identity_is_immutable` 全仓库零调用方，
                   而且它的名字问的是「跨重建是否不变」、函数体答的是
                   「能否安全 merge」，两个问题被一个名字混淆
  · scope_note  —— 纯注释，没有任何代码读它
  · preferred   —— 声明了「将来该切到 arn」这个待办，但没有任何机制检查
                   前提条件何时满足

第三条的代价是实测出来的：TargetGroup 的 note 写着「18 个节点全部没有 arn」
（2026-08-30），而 2026-09-04 实况是 14/18 已有 —— note 过期半个月无人发现，
而且它写的解锁条件「待存量都带上 arn 后再切」在结构上**不可满足**：
剩下 4 个节点分别是 AWS 侧已删除的（nginx-tg-1/2/3）和被
`SKIP_TG_PREFIXES` 刻意排除采集的（openclaw-tg-v2），ETL 永远不会再碰它们。

这一组测试的分工：
  m01–m03  两个身份键谓词的语义边界（锁住「在且仅在 lifetime 上分歧」）
  m04      每个 lifetime 类型必须申报作用域缺口
  m05      preferred 的静态自洽（必须与 identity 不同、必须是真属性名）
  m06      三个元字段都必须有 accessor —— 直接反「写了但没人读」那一类
  m07      **live**：preferred 的解锁条件对活图谱自动核验（显式 opt-in）

参见 todo/injection-found-defects_20260831-1705.md 第 24 节。
"""
from __future__ import annotations

import importlib.util
import os
import pathlib
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
def lifetime_types(contract) -> list[str]:
    """immutable: 'lifetime' 的类型 —— 重建即换名的那一批。

    这里刻意**从契约算出来**而不是硬写 [Pod, K8sService, Deployment, HPA]：
    硬写的话新增一个 lifetime 类型时这组测试会静默漏掉它。
    """
    out = [n for n, v in contract['node_types'].items() if v.get('immutable') == 'lifetime']
    assert out, "契约里没有任何 lifetime 类型 —— 要么契约被改坏了，要么这组测试的前提已失效"
    return out


# ── m01–m03 两个谓词的语义边界 ────────────────────────────────────────────

def test_m01_immutable_vocabulary_is_closed(contract):
    """immutable 只允许 True 与 'lifetime' 两个取值。

    与 test_35 的 g05 是同一条断言，这里重申是因为下面 m02/m03 的语义
    完全建立在「取值只有这两种」之上 —— 冒出第三种取值时应该在这里就炸，
    而不是让 m02/m03 给出一个看似通过的结论。
    """
    bad = {n: v.get('immutable') for n, v in contract['node_types'].items()
           if v.get('immutable') not in (True, 'lifetime')}
    assert not bad, f"这些类型的 immutable 取值不在词表内: {bad}"


def test_m02_lifetime_does_not_survive_recreation(gc, lifetime_types):
    """lifetime 类型的身份键**不能**跨对象重建成立。

    这是本轮修复锁住的回归点。原实现只有一个 `identity_is_immutable`，
    把 'lifetime' 判成不可变 —— 名字问 Q2（跨重建是否不变）、函数体答 Q1
    （能否安全 merge）。Pod 重建即换名，对 Q2 的正确答案是 False。
    """
    wrong = [t for t in lifetime_types if gc.identity_survives_recreation(t)]
    assert not wrong, (
        f"这些类型是 immutable: lifetime，身份键跨重建必然改变，"
        f"identity_survives_recreation 却报 True: {wrong}")


def test_m03_every_declared_type_is_safe_to_merge(gc, contract):
    """所有声明的类型都必须可安全 merge —— 这是 Q1，lifetime 也算通过。

    可变身份键的真实危害是「同一实体裂成两份」（EC2Instance 实测 4/14）。
    Pod 换名产生的是新实体，不构成这种危害，所以 lifetime 在 Q1 上是合格的。
    这条与 m02 一起把两个谓词的分歧面锁死。
    """
    unsafe = [n for n in contract['node_types'] if not gc.identity_is_stable_for_merge(n)]
    assert not unsafe, f"这些类型的身份键不适合作为 merge 匹配键: {unsafe}"


def test_m04_two_predicates_differ_exactly_on_lifetime(gc, contract, lifetime_types):
    """两个谓词必须**在且仅在** lifetime 类型上给出不同答案。

    这条是拆分函数的意义所在：如果两者永远一致，拆分就是纯噪声；
    如果它们在 lifetime 之外也分歧，说明某个谓词的实现漏了取值分支。
    """
    differ = {n for n in contract['node_types']
              if gc.identity_is_stable_for_merge(n) != gc.identity_survives_recreation(n)}
    assert differ == set(lifetime_types), (
        f"两个谓词的分歧面应恰好是 lifetime 类型 {sorted(lifetime_types)}，"
        f"实际是 {sorted(differ)}")


def test_m05_lifetime_types_declare_their_scope_gap(gc, lifetime_types):
    """重建即换名的类型必须申报作用域缺口，不能沉默。

    Pod / K8sService / Deployment / HPA 的身份键是 namespace 内唯一的 name，
    图谱没按 namespace 限定，所以跨 namespace 同名会碰撞 —— 这是已知且未修的
    缺陷，契约有义务把它写出来而不是让读者自己发现。
    """
    silent = [t for t in lifetime_types if not gc.scope_gap_for(t)]
    assert not silent, (
        f"这些 lifetime 类型没有声明 scope_note —— 身份键的作用域缺口必须申报: {silent}")


# ── m06 preferred 的静态自洽 ──────────────────────────────────────────────

def test_m06_preferred_is_a_real_upgrade(gc, contract):
    """preferred 必须是一个与当前身份键**不同**的候选键。

    preferred == identity 意味着「建议切到已经在用的键」，是无意义声明；
    实测 ListenerRule 是个反例参照：它的 name 存的就是 rule_arn，所以它
    正确地**没有**声明 preferred（而不是声明 preferred: arn 来表达同一件事）。
    """
    bad = {}
    for n, v in contract['node_types'].items():
        p = v.get('preferred')
        if p is None:
            continue
        if p == v.get('identity'):
            bad[n] = f"preferred={p} 与 identity 相同，是无意义声明"
        elif not isinstance(p, str) or not p:
            bad[n] = f"preferred={p!r} 不是合法属性名"
    assert not bad, f"preferred 声明不自洽: {bad}"


def test_m07_all_three_meta_fields_have_a_reader(gc, contract):
    """三个元字段都必须有 accessor —— 直接反「写了但没人读」那一类缺陷。

    本系统查出的缺陷有 5 例落在这一类（avg_duration_ms、profile.in_process、
    L7 span 列、probe_xray、ENA 限速），元字段这一轮是第 6 例。判据是
    「契约里出现的每个元字段，graph_contract 都得有函数能把它取出来」——
    没有 accessor 的字段等于注释，注释会过期而且过期时没人知道。
    """
    readers = {
        'identity':             gc.identity_prop_for,
        'immutable':            gc.identity_is_stable_for_merge,
        'scope_note':           gc.scope_gap_for,
        'preferred':            gc.preferred_identity_for,
        'preferred_blocked_by': gc.preferred_blocked_by,
        'expires_seconds':      gc.node_expires_seconds_for,
        'expiry_note':          gc.node_expiry_note_for,
    }
    declared = {k for v in contract['node_types'].values() for k in v}
    # note / writer 是自由文本与归属标注，不参与判定
    declared -= {'note', 'writer'}
    missing = declared - set(readers)
    assert not missing, (
        f"这些元字段在契约里有声明但 graph_contract 没有读取函数: {sorted(missing)}。"
        f"没有 accessor 的字段会变成过期而无人知晓的注释 —— 见 TargetGroup 那条 "
        f"过期半个月的 note。")
    # accessor 必须真的能返回值，不能是空壳
    for field, fn in readers.items():
        owners = [n for n, v in contract['node_types'].items() if v.get(field) is not None]
        if not owners:
            continue
        assert any(fn(n) is not None for n in owners), (
            f"{field} 有 {len(owners)} 个类型声明了它，但其 accessor 对所有类型都返回 None")


def test_m09_preferred_must_be_actionable_or_declare_its_blocker(gc, contract):
    """声明了 `preferred` 的类型，必须要么现在就能切，要么申报阻塞原因。

    这条断言的存在本身就是为了不重造它上游的那个缺陷：TargetGroup 曾经带着一条
    「待存量都带上 arn 后再切」的 note 过期半个月，因为那句话既没有读取方、
    也没有说清到底卡在哪。一个既不可切换又不说明原因的 `preferred`
    就是同一个东西换了个字段名。

    2026-09-04 实测的阻塞是**第二类**前提，此前完全没被记录：
    6 个类型（LoadBalancer / DynamoDBTable / SQSQueue / SNSTopic /
    LambdaFunction / StepFunction）同时被 etl_cfn 写入，而 etl_cfn 的
    get_or_create_vertex 硬编码按 name 匹配、且它手上只有被规范化成短名的
    physical_id（Lambda 的 PhysicalResourceId 就是函数名，本地拿不到 ARN）。
    单方面切 arn 会让两个 ETL 用不同身份键写同一类节点 —— 正是
    test_35::g13 预告的形态。

    判据：凡是被 etl_cfn 的 TYPE_TO_LABEL 覆盖的类型，若声明了 `preferred`
    就必须同时声明 `preferred_blocked_by`。
    """
    cfn = REPO / 'infra' / 'lambda' / 'etl_cfn' / 'neptune_etl_cfn.py'
    import re
    m = re.search(r'TYPE_TO_LABEL\s*=\s*\{(.*?)\n\}', cfn.read_text(), re.S)
    assert m, "找不到 TYPE_TO_LABEL 映射，本用例的判据失效了"
    cfn_labels = set(re.findall(r":\s*'([A-Za-z0-9]+)'", m.group(1)))

    silent = []
    for n, v in contract['node_types'].items():
        if not v.get('preferred'):
            continue
        if n in cfn_labels and not gc.preferred_blocked_by(n):
            silent.append(n)
    assert not silent, (
        f"这些类型声明了 preferred，且被 etl_cfn（按 name 匹配）同时写入，"
        f"却没有申报 preferred_blocked_by: {silent}。"
        f"不申报就会变成又一条过期而无人知晓的承诺。")

    # 反向：申报了阻塞的类型不该已经切过去了（切完就该把两个字段都撤掉）
    stale = [n for n, v in contract['node_types'].items()
             if v.get('preferred_blocked_by') and v.get('identity') == v.get('preferred')]
    assert not stale, f"这些类型已经切到 preferred 了，阻塞声明应当撤掉: {stale}"


def test_m10_switched_types_are_not_written_by_a_name_matching_etl(gc, contract):
    """已经切到非 name 身份键的类型，不得被 etl_cfn 同时写入。

    这是 m09 的另一半，也是 test_35::g13 的加强版：g13 只看 etl_cfn 写的类型
    在契约里是否为 name 身份，本条从相反方向锁 —— 任何非 name 身份的类型都
    不能出现在 etl_cfn 的写入面里。两条合起来让「切身份键」这个动作无法
    绕开 etl_cfn 这个约束。
    """
    cfn = REPO / 'infra' / 'lambda' / 'etl_cfn' / 'neptune_etl_cfn.py'
    import re
    m = re.search(r'TYPE_TO_LABEL\s*=\s*\{(.*?)\n\}', cfn.read_text(), re.S)
    cfn_labels = set(re.findall(r":\s*'([A-Za-z0-9]+)'", m.group(1)))
    bad = {n: v['identity'] for n, v in contract['node_types'].items()
           if v.get('identity') != 'name' and n in cfn_labels}
    assert not bad, (
        f"这些类型的身份键不是 name，但 etl_cfn 仍按 name 匹配着写它们，"
        f"图里会裂成两份: {bad}")


# ── m08 live：preferred 的解锁条件自动核验 ────────────────────────────────

LIVE = (os.environ.get('GRAPH_LIVE_AUDIT') or '').strip().lower() == 'true'


def _real_neptune_query():
    """绕过 conftest 的全局桩，拿到真正会打网络的 neptune_query。

    conftest.py 把 sys.modules['neptune_client_base'] 换成了一个桩，
    neptune_query 恒返回空列表。如果本用例用了那个桩，每个 label 都会查到
    0 个节点，然后「没有节点缺 preferred」这个结论会**自动成立** ——
    一次空查询伪装成条件满足。这正是本项目反复踩的假健康 fallback，
    所以这里必须按文件路径显式加载真模块。
    """
    path = LAYER / 'neptune_client_base.py'
    spec = importlib.util.spec_from_file_location('_real_nc_base_for_audit', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.neptune_query


@pytest.mark.skipif(not LIVE, reason='需 GRAPH_LIVE_AUDIT=true 才对活图谱核验')
def test_m08_preferred_unlock_condition_against_live_graph(contract):
    """preferred 的切换前提条件：全部节点都有该属性且取值唯一。

    三种状态，只有中间那种判失败：

      PENDING   0 个节点有该属性  —— 写入方还没开始写，等待是合理的
      RESIDUE   0 < k < N 有      —— **写入方在写，剩下的是刷不到的残留**，
                                     等下去永远不会自愈，必须回收才能解锁
      UNLOCKED  N 个全有且唯一    —— 可以切了，测试会把它报出来

    区分 PENDING 与 RESIDUE 是这条判据的关键：两者都表现为「条件未满足」，
    但前者该等、后者该动手。TargetGroup 曾经是 14/18（RESIDUE），note 却
    写着「全部没有 arn」（PENDING 的描述），据此得出的「等回填完成再切」
    是个永远不会到来的前提。
    """
    q = _real_neptune_query()

    def one(gremlin):
        v = q(gremlin)['result']['data']['@value'][0]
        return v['@value'] if isinstance(v, dict) else v

    pref = {n: v['preferred'] for n, v in contract['node_types'].items() if v.get('preferred')}
    assert pref, "契约里没有任何 preferred 声明 —— 这条用例的前提已失效"

    residue, unlocked, pending, empty, blocked = {}, [], [], [], []
    for label, p in pref.items():
        total = one(f"g.V().hasLabel('{label}').count()")
        if total == 0:
            empty.append(label)
            continue
        withp = one(f"g.V().hasLabel('{label}').has('{p}').count()")
        if withp == 0:
            pending.append(f"{label}.{p}")
        elif withp < total:
            residue[f"{label}.{p}"] = f"{total - withp}/{total} 个节点没有 {p}"
        else:
            uniq = one(f"g.V().hasLabel('{label}').values('{p}').dedup().count()")
            if uniq < withp:
                residue[f"{label}.{p}"] = f"{withp - uniq} 个 {p} 取值重复"
            elif contract['node_types'][label].get('preferred_blocked_by'):
                # 回填已完成，但还有**第二个**前提没满足（2026-09-04 实测：
                # etl_cfn 也按 name 写这些类型）。报成 UNLOCKED 会误导人去切，
                # 切下去就是两个 ETL 用不同身份键写同一类节点。
                blocked.append(f"{label}.{p}")
            else:
                unlocked.append(f"{label}.{p}")

    print(f"\npreferred 解锁状态："
          f"UNLOCKED={sorted(unlocked)} BLOCKED={sorted(blocked)} "
          f"PENDING={sorted(pending)} 空类型={sorted(empty)}")
    if unlocked:
        print(f"⬆ 以下类型的候选身份键已回填完整且唯一、且无其它阻塞，可以切换："
              f"{sorted(unlocked)} —— 用 infra/migrate_identity_keys.py 复核后改契约")
    if blocked:
        print(f"⛔ 以下类型回填已完成但仍被阻塞（见契约 preferred_blocked_by）："
              f"{sorted(blocked)}")

    assert not residue, (
        f"这些类型的候选身份键处于 RESIDUE 状态 —— 写入方已在写该属性，"
        f"缺失的节点是刷不到的残留，等待不会自愈，必须先回收：{residue}")
