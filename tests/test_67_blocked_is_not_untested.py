"""tests/test_67_blocked_is_not_untested.py — 「打不到」必须与「还没轮到」分开

## 这条门禁在解什么

2026-09-09 实测 115 条依赖边：

    confirmed          10
    inconclusive       14
    未测（无 status）   91

对那 91 条逐条跑 `chaos/code/runner/injectability.py`：

    injectable                 78 条  ← 工具是有的，纯粹没跑
    unreachable_by_any_backend 13 条  ← 用任何后端都打不到

那 13 条是 AgentCore 层（AgentRuntime → AgentTool / AgentRuntime /
KnowledgeBase）加 1 条 LambdaFunction → NeptuneCluster：
Chaos Mesh 只能打集群内 Pod 而 AgentCore runtime 是托管的，FIS 也没有
AgentCore 动作 —— **双向封死**。

**问题在于图谱上看不出这个区别。** 选靶逻辑早就会排除它们
（`tests/test_47::t305_06` 钉着「排除发生在打分之前」），
但那份知识只在代码里；查图的人看到的是一条没有 `verify_status` 的边，
与「还没轮到」完全无法区分。

后果是覆盖率数字给出**错误的努力方向**：看起来「再跑几轮就能覆盖」，
实际上永远轮不到。

## 为什么不给 verify_status 加第五个取值

`statuses` 只有 untested / confirmed / refuted / inconclusive，描述的是
**验证结果**；「能不能验」是正交维度 —— 一条边可以同时「被阻断」且「未测试」。
塞进 status 会让「不可验」看起来像一种验证结论，而它恰恰是「没有结论」，
并且会迫使每个消费方（置信度计算、覆盖率、DR 影响面）都跟着改。
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest

from paths import PROJECT_ROOT

ROOT = pathlib.Path(PROJECT_ROOT)
for p in (ROOT / 'chaos' / 'code' / 'runner',):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def test_t67_01_contract_declares_blocked_reason_attr():
    """契约必须声明 `verify_blocked_reason`，且**不得**把它做成第五个 status。"""
    import yaml

    gc = yaml.safe_load(
        (ROOT / 'profiles' / 'graph_contract.yaml').read_text(encoding='utf-8'))
    ev = gc.get('edge_verification') or {}
    assert 'verify_blocked_reason' in (ev.get('attrs') or []), (
        '契约的 edge_verification.attrs 里缺 verify_blocked_reason —— '
        '没有它，「后端打不到」只能混在 untested 里'
    )
    statuses = set(ev.get('statuses') or [])
    assert statuses == {'untested', 'confirmed', 'refuted', 'inconclusive'}, (
        f'verify_status 的取值集合变了: {sorted(statuses)}\n'
        f'「打不到」**刻意不做成第五个 status** —— 它是能力维度而非结果维度，'
        f'一条边可以同时「被阻断」且「未测试」。'
        f'加第五个取值会让「不可验」看起来像一种验证结论。'
    )


def test_t67_02_injectability_still_flags_agentcore_as_unreachable():
    """AgentCore 层仍被判为不可达 —— 但**理由变了**，而理由比结论重要。

    ## 2026-09-15：补了第四轴（IAM deny），结论没变，诊断变精确了

    9-13 另一个会话用 IAM deny 把 `petsearch -> DynamoDBTable` 验成
    confirmed / 退化 100%（`iam-deny-probe_20260913-155349`）。
    机制是 **SigV4 授权按每次 API 调用评估，不是按每个连接评估** ——
    DNS 缓存、连接池、端点 IP 轮换在这一层全部不成立。

    补轴之后我一度以为 3 条 `Delegates AgentRuntime -> AgentRuntime`
    的标注是错的（目标类型 `AgentRuntime` **确实在**能力表里），
    清掉过一次。但 `tests/test_47::t305b_03` 立刻抓出第四轴的实现 bug：
    **它只判目标、没判源**，于是连 `BusinessCapability -> SQSQueue`
    都被判成可注入。

    IAM deny 的做法是给**调用方的角色**加 deny 内联策略，所以两侧都要判。
    而源侧清单取**实现真能做到的**：
    `scripts/verify_via_iam_deny.py::_irsa_role_for` 只从 K8s ServiceAccount
    的注解取角色，AgentCore 执行角色的解析**没有实现**。

    所以那 3 条已按新理由重新标注（留痕
    `todo/marked-unreachable-edges_20260915-0526.json`）。
    新理由自带诊断：`目标在能力表内=True，源有可加策略的角色=False` ——
    它直接告出下一个人该补什么，而原来那句「无任何后端能打到」不告诉任何事。
    """
    import injectability as inj

    for src, dst in (('AgentRuntime', 'AgentTool'),
                     ('AgentRuntime', 'AgentRuntime'),
                     ('AgentRuntime', 'KnowledgeBase'),
                     ('LambdaFunction', 'NeptuneCluster')):
        verdict, why = inj.injectability(src, dst)
        assert verdict == inj.UNREACHABLE, (
            f'{src} -> {dst} 的可注入性判定变成了 {verdict}（{why}）。\n'
            f'若确实获得了新的注入能力，请：\n'
            f'  1. 用 scripts/reclassify_blocked_edges.py 清掉这些边的标注\n'
            f'  2. 让它们回到验证队列\n'
            f'  3. 更新本用例\n'
            f'不要只改本用例 —— 那会让这些边永久停在「打不到」而实际已可打。'
        )


def test_t67_02b_IAM_deny_轴必须两侧都判():
    """第四轴只判目标就会把没有 IAM 主体的源也判成可注入。

    实测：第一版只看 `dst_label in iam_deny_targets()`，
    于是 `BusinessCapability -> SQSQueue`（抽象节点，没有任何 IAM 角色）
    被判成 injectable。`tests/test_47::t305b_03` 抓到了它。
    """
    import injectability as inj

    # 目标在能力表内、但源没有可加策略的角色 —— 必须仍判不可达
    v, why = inj.injectability('BusinessCapability', 'SQSQueue')
    assert v == inj.UNREACHABLE, (
        f'BusinessCapability -> SQSQueue 判成了 {v} —— '
        f'IAM deny 要给调用方的角色加 deny 策略，'
        f'而 BusinessCapability 是抽象节点、没有 IAM 主体。')
    # 理由必须把两侧条件都摊开，否则诊断不出是哪一侧不满足
    assert '目标在能力表内' in why and '源有可加策略的角色' in why, (
        f'不可达的理由没有摊开两侧条件，无从诊断: {why}')


def test_t67_02c_IAM_deny_能力表必须真的读到():
    """能力表读不到时 `iam_deny_targets()` 静默返回空集。

    静默退化会让判定器悄悄回到"只有三轴"的旧行为，
    于是可验的托管服务边重新被判成永久不可达。
    """
    import injectability as inj

    targets = inj.iam_deny_targets()
    assert targets, (
        'iam_deny_targets() 返回空集 —— '
        '读不到 scripts/verify_via_iam_deny.py 的 SEVERANCE_METHODS。')
    for t in ('DynamoDBTable', 'S3Bucket', 'AgentRuntime'):
        assert t in targets, (
            f'IAM deny 能力表里缺 {t} —— 它是实测验证过的类型'
            f'（petsearch -> DynamoDBTable 在 iam-deny-probe_20260913-155349 '
            f'里拿到 100% 退化）')
    # 源侧清单不得悄悄扩大到实现做不到的类型
    assert 'AgentRuntime' not in inj.IAM_DENY_SOURCE_LABELS, (
        'AgentRuntime 被加进了 IAM deny 的源侧清单，但 '
        '_irsa_role_for 只解析 K8s ServiceAccount 注解 —— '
        '要加它先去补 AgentCore 执行角色的解析，否则判定会说能打、'
        '真要加策略那一刻才失败。')


def test_t67_03_pod_backed_labels_do_not_include_managed_runtimes():
    """托管运行时不得被登记为 Pod 支撑类型。

    `POD_BACKED_LABELS` 决定「能否在源侧切断出向流量」。
    把 `AgentRuntime` 混进去会让 13 条边被误判为可注入，
    于是选靶器会选中它们、注入必然打空，
    而打空的结果在判定链里表现为**退化为 0** —— 那正是 refuted 的形状。
    按 DoD-10 累计两次 refuted 就删边，等于用一个建模错误删掉真实依赖。
    """
    import injectability as inj

    forbidden = {'AgentRuntime', 'AgentTool', 'KnowledgeBase',
                 'LambdaFunction', 'StepFunction'}
    overlap = inj.POD_BACKED_LABELS & forbidden
    assert not overlap, (
        f'这些托管类型被登记为 Pod 支撑: {sorted(overlap)}\n'
        f'它们不在集群内，源侧切断对它们无效；误登记会让注入打空，'
        f'而打空在判定链里长得像 refuted（退化为 0）—— '
        f'累计两次就会删掉真实依赖。'
    )


@pytest.mark.neptune
def test_t67_04_blocked_edges_are_not_counted_as_untested(neptune_rca):
    """活图谱里「被阻断」与「未测试」必须是互斥的两桶。

    同时校验一条纪律：**被阻断的边不得同时带有终态结论**。
    若某条边既有 blocked_reason 又是 confirmed，说明它其实验过 ——
    标注是错的，该清掉。
    """
    rows = neptune_rca.results("""
MATCH (a)-[r:AccessesData|Calls|Delegates|DependsOn|Invokes|InvokesTool|PublishesTo|Retrieves|RoutesToRuntime|RoutesVia]->(b)
WHERE r.verify_blocked_reason IS NOT NULL
RETURN coalesce(r.verify_status,'(无)') AS status, count(*) AS cnt
""")
    bad = [r for r in rows
           if r.get('status') in ('confirmed', 'refuted', 'inconclusive')]
    assert not bad, (
        f'这些边同时带 verify_blocked_reason 和终态结论: {bad}\n'
        f'有结论说明实际上验过（或试过），标注「打不到」是错的，应清掉。'
    )
