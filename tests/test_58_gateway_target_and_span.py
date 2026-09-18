"""tests/test_58_gateway_target_and_span.py — 网关 target 解析与网关出站 span 派生

## 这组测试钉住的是什么

2026-09-07 用 `bedrock-agentcore-control` 控制面实测发现两件事：

1. **网关 target 的目标节点类型一直是错的。** `GetGatewayTarget` 显示 5 个
   target 的 `targetType` 全是 `AGENTCORE_RUNTIME`、配置指向 runtime ARN，
   而 ETL 一律建 `AgentTool` 节点 —— 图里因此多出 5 个不存在的「工具」。
2. **`ListGatewayTargets` 的 item 不含 `targetConfiguration`。**
   而 `_backend_kind()` / `_backend_ref()` 都在读它，于是对网关 target
   一直空转（实测：5 个 gateway 派生的 AgentTool 全部 `backend_kind='unknown'`）。

另外从 orchestrator 的 httpx CLIENT span 里发现网关这一跳有结构化记录，
可以据此派生 `RoutesVia`，并用 **控制面 target 索引** 代替
`_DELEGATION_TOOLS` 硬编码名字表来派生 `Delegates`。

这些解析逻辑都是纯函数，**离线可测**，不需要 AWS 凭据或 Neptune。
"""
from __future__ import annotations

import os
import sys

import pytest

from paths import PROJECT_ROOT

ETL_DIR = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'etl_agentcore')
LAYER = os.path.join(PROJECT_ROOT, 'infra', 'lambda', 'shared', 'python')
for p in (ETL_DIR, LAYER):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture(scope='module')
def etl():
    """导入 etl_agentcore 模块。conftest 已把 neptune_client_base 桩进 sys.modules。"""
    try:
        import neptune_etl_agentcore as m
    except ImportError as exc:                                # pragma: no cover
        pytest.skip(f'etl_agentcore 不可导入: {exc}')
    return m


# ── 控制面 target 的真实形状（2026-09-07 GetGatewayTarget 实测样本）──────────

RUNTIME_TARGET = {
    'targetId': 'BLEULLONR7',
    'name': 'nutrition',
    'status': 'READY',
    'targetType': 'AGENTCORE_RUNTIME',
    'targetConfiguration': {
        'http': {'agentcoreRuntime': {
            'arn': 'arn:aws:bedrock-agentcore:ap-northeast-1:926093770964:'
                   'runtime/WaggleAINutrition-2NEiCS7VJw'}}},
    'credentialProviderConfigurations': [
        {'credentialProviderType': 'GATEWAY_IAM_ROLE'}],
}

LAMBDA_TARGET = {
    'targetId': 'ZZZ1',
    'name': 'some_tool',
    'targetType': 'MCP',
    'targetConfiguration': {
        'mcp': {'lambda': {
            'lambdaArn': 'arn:aws:lambda:ap-northeast-1:926093770964:'
                         'function:my-tool-fn:PROD'}}},
}


def test_t58_01_runtime_target_resolves_to_runtime_arn(etl):
    """AGENTCORE_RUNTIME 型 target 必须解析出 runtime ARN。"""
    arn = etl._runtime_arn_from_target(RUNTIME_TARGET)
    assert arn == ('arn:aws:bedrock-agentcore:ap-northeast-1:926093770964:'
                   'runtime/WaggleAINutrition-2NEiCS7VJw')


def test_t58_02_non_runtime_target_returns_none(etl):
    """MCP/Lambda 型 target 不是 runtime —— 必须返回 None 走 AgentTool 分支。

    这条防的是「把所有 target 都当 runtime」的反向过头修复。
    """
    assert etl._runtime_arn_from_target(LAMBDA_TARGET) is None


def test_t58_03_runtime_type_without_config_returns_none(etl):
    """targetType 命中但拿不到 arn 时必须返回 None，不能建一条指向 None 的边。

    这是 `_paged_targets` 里 GetGatewayTarget 降级路径的对应场景 ——
    只看 targetType 就建边，会造出端点为空的脏边。
    """
    degraded = {'targetId': 'X', 'name': 'nutrition',
                'targetType': 'AGENTCORE_RUNTIME'}     # 无 targetConfiguration
    assert etl._runtime_arn_from_target(degraded) is None


def test_t58_04_endpoint_suffix_is_stripped(etl):
    """带 runtime-endpoint 后缀的 ARN 要被规约到裸 runtime ARN。

    span 里的 runtime_id 是带后缀的形式；控制面目前不带。两种形式混用会造出
    重复节点 —— 本项目踩过这个坑（见契约 identity 的 immutable 约束）。
    """
    t = dict(RUNTIME_TARGET)
    t['targetConfiguration'] = {'http': {'agentcoreRuntime': {
        'arn': 'arn:aws:bedrock-agentcore:ap-northeast-1:926093770964:'
               'runtime/WaggleAINutrition-2NEiCS7VJw/runtime-endpoint/DEFAULT:DEFAULT'}}}
    assert etl._runtime_arn_from_target(t).endswith(
        'runtime/WaggleAINutrition-2NEiCS7VJw')


def test_t58_05_backend_kind_works_once_config_present(etl):
    """`targetConfiguration` 补齐后 _backend_kind 必须不再是 unknown。

    实测这个函数对网关 target 一直返回 'unknown'，因为 ListGatewayTargets
    不返回 targetConfiguration。本条钉住「补了 GetGatewayTarget 之后它能工作」。
    """
    assert etl._backend_kind(LAMBDA_TARGET) == 'lambda'
    assert etl._backend_kind({'targetConfiguration': {}}) == 'unknown'


def test_t58_06_credential_provider_extracted(etl):
    assert etl._credential_provider(RUNTIME_TARGET) == 'GATEWAY_IAM_ROLE'
    assert etl._credential_provider({}) == 'unknown'


# ── 网关出站 span 的解析 ─────────────────────────────────────────────────────

GW_HOST = ('waggleaigateway-th4m2rp46p.gateway.bedrock-agentcore'
           '.ap-northeast-1.amazonaws.com')
GW_ARN = ('arn:aws:bedrock-agentcore:ap-northeast-1:926093770964:'
          'gateway/waggleaigateway-th4m2rp46p')


def test_t58_07_gateway_host_maps_to_arn_by_id(etl):
    """主机名第一段是 gatewayId，用它查索引 —— 不用名字。

    用 id 而非 name 是刻意的：id 在 ARN 里、是控制面给的不变量，名字可改。
    与契约「身份键必须由不变量派生」一致。
    """
    idx = {'waggleaigateway-th4m2rp46p': GW_ARN}
    assert etl._gateway_arn_from_remote_service(GW_HOST, idx) == GW_ARN


def test_t58_08_unknown_gateway_host_returns_none(etl):
    """索引里没有的网关必须返回 None —— 宁缺一条边，不建端点错的边。"""
    assert etl._gateway_arn_from_remote_service(GW_HOST, {}) is None
    assert etl._gateway_arn_from_remote_service('', {'x': 'y'}) is None


@pytest.mark.parametrize('remote_op,expected', [
    ('POST /adoption', 'adoption'),
    ('POST /nutrition', 'nutrition'),
    ('POST /concierge', 'concierge'),
    ('GET /ordering?x=1', 'ordering'),
    ('', ''),
])
def test_t58_09_target_name_from_remote_op(etl, remote_op, expected):
    """`aws.remote.operation` 形如 `POST /adoption` —— 取出 target 名。

    这个 target 名是替代 `_DELEGATION_TOOLS` 硬编码表的关键：
    它经控制面 target 索引就能解析出目标 runtime，不需要人肉维护名字映射。
    """
    assert etl._target_name_from_remote_op(remote_op) == expected


def test_t58_10_delegation_no_longer_needs_hardcoded_tool_names(etl):
    """回归防线：曾经漏掉的两个 tool 名，现在走 target 索引必须能解析。

    2026-09-06 之前 `_DELEGATION_TOOLS` 只有 concierge / ordering，
    而 orchestrator 实际注册的是 concierge_chat / food_ordering，
    查表落空导致 Delegates 边**永远建不出来** —— Concierge 与 Ordering
    在图上从 orchestrator 不可达，爆炸半径查询答不出「这两个 agent 挂了影响谁」。

    网关 span 给的是**网关 target 名**（concierge / ordering），
    与 orchestrator 注册的 tool 名无关，所以那类漏项在这条路径上不会发生。
    """
    target_index = {
        (GW_ARN, 'concierge'): 'arn:…:runtime/WaggleAIConcierge-Yi6Ub97Ylw',
        (GW_ARN, 'ordering'): 'arn:…:runtime/WaggleAIOrdering-Mnr1HiASuX',
    }
    for op, want_key in (('POST /concierge', 'concierge'),
                         ('POST /ordering', 'ordering')):
        tname = etl._target_name_from_remote_op(op)
        assert tname == want_key
        assert target_index.get((GW_ARN, tname)) is not None, (
            f'target 名 {tname!r} 应能经控制面索引解析出 runtime，'
            f'不再依赖 _DELEGATION_TOOLS 名字表'
        )


def test_t58_11_gateway_span_query_does_not_filter_on_gen_ai(etl):
    """网关 span 查询**不得**带 gen_ai 过滤器 —— 那正是它此前被漏采的原因。

    SPAN_QUERY 的 `filter ispresent(op)`（op=attributes.gen_ai.operation.name）
    会把 httpx CLIENT span 整批排除。这条断言防止有人「统一」两条查询。
    """
    q = etl.GATEWAY_SPAN_QUERY
    assert 'gen_ai' not in q, (
        '网关这一跳是 HTTP CLIENT span，没有任何 gen_ai.* 属性；'
        '加 gen_ai 过滤器会让它一条都采不到。'
    )
    assert 'aws.remote.service' in q and 'aws.remote.operation' in q
    # 不写死网关主机名：网关 id 是部署产物，重建后写死的匹配会静默失效。
    assert 'th4m2rp46p' not in q, '不要把具体网关 id 写进查询'
