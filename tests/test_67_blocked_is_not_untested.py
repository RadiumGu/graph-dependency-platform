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
# 导入约定照 `tests/test_47` 的注释（它早就写清了）：
# **必须以 `runner.xxx` 形式导入** —— 该包内模块用相对 import，
# 把 `runner/` 本身加进 sys.path 再 `import xxx` 会报
# 「attempted relative import with no known parent package」。
#
# 本文件原先插 `chaos/code/runner` 并用顶层 `import injectability`。
# 那让 `runner` 优先解析成**模块**（`runner/runner.py`）而不是包，于是
# `tests/test_47` 的 `from runner import injectability` 拿到那个模块、
# 它的 `from .experiment import ...` 炸掉 —— 18 个 fixture setup 全 error。
# 症状极阴：单独跑 test_67 绿、单独跑 test_47 绿，`67+47` 一起跑才红。
# 2026-09-15 实测定位。
for p in (ROOT / 'infra' / 'lambda' / 'shared' / 'python',
          ROOT / 'chaos' / 'code'):
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
    """AgentCore 层里**仍然**打不到的那几类必须仍判不可达。

    ## 演进史（三次改动，每次都被门禁或另一条门禁纠正）

    **9-09** 原始版本把 AgentCore 全部三类钉成 UNREACHABLE，依据是
    「Chaos Mesh 只能打集群内 Pod，FIS 没有 AgentCore 动作」。

    **9-15 第一次**：补了 IAM deny 第四轴（机制是 **SigV4 授权按每次 API
    调用评估、不按连接评估**），`AgentRuntime` 在能力表里，于是本用例变红。
    我据此清掉 3 条 `Delegates` 标注 —— **清早了**。

    **9-15 第二次**：`tests/test_47::t305b_03` 抓出第四轴只判目标不判源，
    连 `BusinessCapability -> SQSQueue` 都判成可注入。补上源侧判定后
    `AgentRuntime` 作源不在清单里（`_irsa_role_for` 只解析 K8s SA 注解），
    那 3 条又回到不可达，我把它们重新标上。

    **9-15 第三次（本次）**：补了
    `_agentcore_role_for`（`bedrock-agentcore-control get-agent-runtime`
    的 `roleArn`）与 `_lambda_role_for`。实测 5 个 WaggleAI 运行时
    **角色两两不共用**，满足"半径恰好一个服务"的纪律。
    于是 `AgentRuntime` 进了源侧清单，那 3 条边**真的**可注入了，已清标注
    （留痕 `todo/reclassified-blocked-edges_20260915-0924.json`）。

    留下来的两类是**目标侧**打不到：`AgentTool` / `KnowledgeBase`
    都不在 `SEVERANCE_METHODS` 里。要解开得往那张表加条目，
    不是往源侧清单加。

    **9-17 第四次（本次修正，两个方向都动了）**：

    · `NeptuneCluster` **已加入** `SEVERANCE_METHODS` ——
      前置条件实测确认（`petsite-neptune` 的
      `IAMDatabaseAuthenticationEnabled = True`，源
      `neptune-etl-from-xray` 是 Lambda、冷启动重取凭证）。
      所以它从本清单移出，改钉在下面"必须可注入"那一组。

    · `AgentRuntime` 作**目标**曾被撤回，**9-17 又加回来了**。
      撤回的理由（"IAM deny 切不断"）是错的，加回的证据强度也不同：

      读源码确认委派用**标准 SigV4**
      （`SigV4Auth(..., "bedrock-agentcore", region)` + httpx POST），
      CloudTrail 按 access key 反查确认签名身份就是被 deny 的那个角色，
      而施加 deny 后源侧日志 **403 出现 379 次**
      （6.89 次/分，基线 0.02 次/分 → 413 倍）。
      **注入一直是生效的。**

      三次误判的原因是两条观测通道同时瞎：边级流量测不出目标粒度，
      而业务探针被 LLM 欺骗 —— `_via_gateway` 把 403 包成 JSON 错误
      喂给 LLM，LLM 用对话历史编出看起来正常的回答。
      我拿"业务未退化"反推"注入未生效"，中间那一步从没验证。

      所以 `AgentRuntime -> AgentRuntime` 现在钉在"必须可注入"那一组，
      且这类边的生效性判据必须用 `_source_denial_count`（源侧拒绝速率）。
    """
    from runner import injectability as inj

    for src, dst in (('AgentRuntime', 'AgentTool'),
                     ('AgentRuntime', 'KnowledgeBase')):
        verdict, why = inj.injectability(src, dst)
        assert verdict == inj.UNREACHABLE, (
            f'{src} -> {dst} 的可注入性判定变成了 {verdict}（{why}）。\n'
            f'通常是 SEVERANCE_METHODS 补了 {dst}。若确实如此，请：\n'
            f'  1. 用 scripts/reclassify_blocked_edges.py 清掉这些边的标注\n'
            f'  2. 让它们回到验证队列\n'
            f'  3. 更新本用例\n'
            f'不要只改本用例 —— 那会让这些边永久停在「打不到」而实际已可打。'
        )

    # 反向钉住：这两类现在**必须**可注入。
    for src, dst, guard in (
            ('LambdaFunction', 'NeptuneCluster',
             'NeptuneCluster 条目与 _lambda_role_for'),
            # 9-17 加回：源侧日志 379 次 403 证明 IAM deny 确实切断了委派。
            ('AgentRuntime', 'AgentRuntime',
             'AgentRuntime 条目与 _agentcore_role_for')):
        verdict, why = inj.injectability(src, dst)
        assert verdict == inj.INJECTABLE, (
            f'{src} -> {dst} 退回成 {verdict}（{why}）。\n'
            f'可能是 {guard} 被拆掉。\n'
            f'⚠️ 若你是因为"跑了一次业务没退化"而想把它标回不可达：\n'
            f'   先看源侧日志的拒绝速率（_source_denial_count）。\n'
            f'   agent 系统上业务探针会系统性漏判 —— LLM 会用旧数据\n'
            f'   编出像样的回答，那是静默错误而不是"依赖不成立"。\n'
            f'   这个坑本项目已经踩了三次。'
        )


    # 注：这里曾有一条"`AgentRuntime -> AgentRuntime` 必须 INJECTABLE"的
    # 反向断言，用来守 `_agentcore_role_for` 不被拆掉。9-17 删除 ——
    # 它钉的是一个**被实测推翻**的结论（见上面 docstring 第四次）。
    # `_agentcore_role_for` 本身仍有 tests/test_74 守着源侧解析，
    # 不需要靠一条错误的可注入性断言来保护。


def test_t67_02b_IAM_deny_轴必须两侧都判():
    """第四轴只判目标就会把没有 IAM 主体的源也判成可注入。

    实测：第一版只看 `dst_label in iam_deny_targets()`，
    于是 `BusinessCapability -> SQSQueue`（抽象节点，没有任何 IAM 角色）
    被判成 injectable。`tests/test_47::t305b_03` 抓到了它。
    """
    from runner import injectability as inj

    # 目标在能力表内、但源没有可加策略的角色 —— 必须仍判不可达
    v, why = inj.injectability('BusinessCapability', 'SQSQueue')
    assert v == inj.UNREACHABLE, (
        f'BusinessCapability -> SQSQueue 判成了 {v} —— '
        f'IAM deny 要给调用方的角色加 deny 策略，'
        f'而 BusinessCapability 是抽象节点、没有 IAM 主体。')
    # 理由必须把两侧条件都摊开，否则诊断不出是哪一侧不满足
    assert '目标在能力表内' in why and '源有可加策略的角色' in why, (
        f'不可达的理由没有摊开两侧条件，无从诊断: {why}')



def test_t67_03_pod_backed_labels_do_not_include_managed_runtimes():
    """托管运行时不得被登记为 Pod 支撑类型。

    `POD_BACKED_LABELS` 决定「能否在源侧切断出向流量」。
    把 `AgentRuntime` 混进去会让 13 条边被误判为可注入，
    于是选靶器会选中它们、注入必然打空，
    而打空的结果在判定链里表现为**退化为 0** —— 那正是 refuted 的形状。
    按 DoD-10 累计两次 refuted 就删边，等于用一个建模错误删掉真实依赖。
    """
    from runner import injectability as inj

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
