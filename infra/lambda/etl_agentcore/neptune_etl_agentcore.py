"""neptune_etl_agentcore —— 把 AgentCore / GenAI 资源与调用关系写进依赖图谱。

两条数据链路，**刻意分开**（失败模式不同，混在一起会互相掩盖）：

  ① 控制面 API（bedrock-agentcore-control / bedrock / bedrock-agent）
       → 资源节点：AgentRuntime / AgentGateway / AgentMemory / KnowledgeBase / Guardrail
       → 静态关系边：AgentGateway-[RoutesTo]->AgentTool、Guardrail-[ProtectsAccess]->AgentRuntime
       失败表现：**节点缺失**

  ② aws/spans 结构化日志（Transaction Search 开启后 100% span 落这里）
       → 调用边：Delegates / InvokesTool / Retrieves / AccessesData
       失败表现：**边缺失**（节点还在，图看起来「有 agent 但没有依赖」）

为什么调用边只能来自 ②：控制面 API 看不到「谁调了谁」。
Runtime 列表告诉你有哪些 agent，**不告诉你 orchestrator 路由到了哪个子 agent** ——
那只存在于运行时 span 里。这与 etl_xray 只能从 GetServiceGraph 拿拓扑是同一个道理。

⚠️ 本 ETL 全程走契约门禁（assert_node_type / assert_edge_type / assert_source）。
   写这条注释是因为审计发现 **etl_xray 与共享层 neptune_client_base.py 完全没接门禁**，
   契约声称「写入时强制」但那条路径是无门禁的。新 ETL 不重复这个缺陷。

⚠️ 本 ETL 必须能在「AgentCore 尚未部署」时正常空跑（Stage 4 之前就是这个状态）。
   空结果不是错误 —— 但**空结果与「拿不到结果」必须区分**，否则权限丢失会被
   当成「本来就没有 agent」而静默通过。见 _probe_status。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from collections import defaultdict

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, '/opt/python')

from neptune_client_base import neptune_query, safe_str, extract_value, REGION  # noqa: E402
from graph_contract import (  # noqa: E402
    assert_edge_type,
    assert_node_type,
    assert_source,
    identity_prop_for,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SOURCE = 'agentcore-etl'
ENVIRONMENT = os.environ.get('ENVIRONMENT', 'prod')

# aws/spans 的回看窗口。与 AccessesData / Delegates / InvokesTool / Retrieves 的
# expires_seconds=21600（6h）**刻意取同一个值** —— 采集窗口小于 TTL 会让边在
# 「还没到期但本轮没看到」时被误判失活；大于 TTL 则写进来的边立刻就是过期的。
SPAN_LOOKBACK_SECONDS = int(os.environ.get('AGENTCORE_SPAN_LOOKBACK_SECONDS', str(6 * 3600)))
SPAN_LOG_GROUP = os.environ.get('AGENTCORE_SPAN_LOG_GROUP', 'aws/spans')
INSIGHTS_TIMEOUT_SECONDS = int(os.environ.get('AGENTCORE_INSIGHTS_TIMEOUT', '60'))


# ── 采集状态：区分「空」与「拿不到」──────────────────────────────────────────
class _probe_status:
    """一次子采集的结果状态。

    为什么不用 None/[] 表示失败：`[]` 与「调用失败」在下游看起来一样，
    而它们的含义相反 —— 前者是「确实没有 agent」，后者是「我不知道有没有」。
    把后者当成前者会让权限丢失表现为「图里 agent 消失了」而无人报警，
    这与本项目在 X-Ray 双源验证里踩过的坑同形（verified_by 属性就是为它加的）。
    """

    OK = 'ok'
    EMPTY = 'empty'
    FAILED = 'failed'


def _collect(fn, what: str):
    """跑一次采集，返回 (status, items)。异常不外抛 —— 单项失败不该让整轮 ETL 挂。"""
    try:
        items = fn()
    except ClientError as exc:
        code = exc.response.get('Error', {}).get('Code', '?')
        logger.warning("采集 %s 失败 [%s]: %s", what, code, exc)
        return _probe_status.FAILED, []
    except Exception as exc:  # noqa: BLE001
        logger.warning("采集 %s 失败: %s", what, exc)
        return _probe_status.FAILED, []
    if not items:
        logger.info("采集 %s：0 条（确认为空，非失败）", what)
        return _probe_status.EMPTY, []
    logger.info("采集 %s：%d 条", what, len(items))
    return _probe_status.OK, items


# ── ① 控制面采集 ─────────────────────────────────────────────────────────────
def _paged(client, op: str, key: str) -> list:
    """分页取全量。不用 get_paginator —— bedrock-agentcore-control 的部分 op
    在当前 botocore 里没有 paginator 模型，硬用会抛 OperationNotPageableError。"""
    out, token = [], None
    while True:
        kwargs = {'nextToken': token} if token else {}
        resp = getattr(client, op)(**kwargs)
        out.extend(resp.get(key) or [])
        token = resp.get('nextToken')
        if not token:
            return out


def collect_control_plane() -> dict:
    """返回 {kind: (status, items)}。"""
    acc = boto3.client('bedrock-agentcore-control', region_name=REGION)
    br = boto3.client('bedrock', region_name=REGION)
    bra = boto3.client('bedrock-agent', region_name=REGION)

    return {
        'runtimes': _collect(
            lambda: _paged(acc, 'list_agent_runtimes', 'agentRuntimes'), 'AgentRuntime'),
        'gateways': _collect(
            lambda: _paged(acc, 'list_gateways', 'items'), 'AgentGateway'),
        'memories': _collect(
            lambda: _paged(acc, 'list_memories', 'memories'), 'AgentMemory'),
        'guardrails': _collect(
            lambda: br.list_guardrails().get('guardrails') or [], 'Guardrail'),
        'kbs': _collect(
            lambda: bra.list_knowledge_bases().get('knowledgeBaseSummaries') or [],
            'KnowledgeBase'),
    }


def collect_gateway_targets(gateways: list) -> dict:
    """Gateway → 它前置的 tool。这是 AgentTool 节点的**唯一控制面来源**。

    进程内注册的 tool 控制面看不到，只能从 span 里发现 —— 两个来源写同一类节点，
    靠 tool_key 的 owner 部分区分（Gateway 前置的 owner 是 gateway arn，
    进程内的 owner 是 runtime arn）。
    """
    acc = boto3.client('bedrock-agentcore-control', region_name=REGION)
    out = {}
    for gw in gateways:
        gid = gw.get('gatewayId') or gw.get('gatewayIdentifier')
        arn = gw.get('gatewayArn') or gw.get('arn')
        if not (gid and arn):
            continue
        st, targets = _collect(
            lambda g=gid: _paged_targets(acc, g), f'Gateway {gid} 的 target')
        if st == _probe_status.OK:
            out[arn] = targets
    return out


def _paged_targets(acc, gateway_id: str) -> list:
    out, token = [], None
    while True:
        kwargs = {'gatewayIdentifier': gateway_id}
        if token:
            kwargs['nextToken'] = token
        resp = acc.list_gateway_targets(**kwargs)
        out.extend(resp.get('items') or [])
        token = resp.get('nextToken')
        if not token:
            return out


# ── ② span 采集 ──────────────────────────────────────────────────────────────
# aws/spans 里 agent 调用的判据。**不用 gen_ai.* 属性做身份键**（stability 全是
# development），但可以用它们做**筛选**—— 筛错了顶多少几条边，不会污染节点身份。
SPAN_QUERY = """
fields @timestamp, attributes.gen_ai.operation.name as op,
       resource.attributes.cloud.resource_id as runtime_id,
       attributes.gen_ai.tool.name as tool_name,
       attributes.gen_ai.agent.name as peer_agent,
       attributes.gen_ai.request.model as model_id,
       attributes.aws.bedrock.knowledge_base.id as kb_id
| filter ispresent(runtime_id)
| filter ispresent(op)
| stats count(*) as calls, max(@timestamp) as last_ts
       by runtime_id, op, tool_name, peer_agent, model_id, kb_id
| limit 2000
"""


def collect_spans() -> tuple:
    """从 aws/spans 抽 agent 调用关系。

    ⚠️ 硬前置：Transaction Search 必须已开（destination=CloudWatchLogs），
    否则 aws/spans 日志组根本不存在。开启记录见
    todo/agentobv/05-etl_xray影响面量化_20260904-0835.md 第六节（2026-09-04 08:49:33Z ACTIVE）。
    """
    logs = boto3.client('logs', region_name=REGION)
    now = int(time.time())

    def _run():
        q = logs.start_query(
            logGroupName=SPAN_LOG_GROUP,
            startTime=now - SPAN_LOOKBACK_SECONDS,
            endTime=now,
            queryString=SPAN_QUERY,
        )
        qid = q['queryId']
        deadline = time.time() + INSIGHTS_TIMEOUT_SECONDS
        while time.time() < deadline:
            r = logs.get_query_results(queryId=qid)
            status = r.get('status')
            if status == 'Complete':
                return [{c['field']: c['value'] for c in row} for row in r.get('results', [])]
            if status in ('Failed', 'Cancelled', 'Timeout'):
                raise RuntimeError(f'Logs Insights 查询 {status}')
            time.sleep(1)
        # 超时不当成空 —— 交给 _collect 记为 FAILED
        logs.stop_query(queryId=qid)
        raise TimeoutError(f'Logs Insights 超过 {INSIGHTS_TIMEOUT_SECONDS}s 未完成')

    return _collect(_run, 'aws/spans agent 调用')


# ── 写入 ─────────────────────────────────────────────────────────────────────
def _upsert_node(label: str, identity_value: str, props: dict, round_ts: int) -> None:
    """按契约声明的身份键 upsert 一个节点。

    顶点属性一律 property(single, ...) —— Gremlin 顶点属性默认 SET 基数，
    不带 single 是**追加而非覆盖**，会静默累积多值。本仓库为此清理过 1,897 个
    冗余值（infra/fix_property_cardinality.py）。
    """
    assert_node_type(label)
    assert_source(SOURCE, f'_upsert_node(label={label})')

    id_key = identity_prop_for(label)
    if not id_key:
        raise RuntimeError(f'契约未声明 {label} 的身份键')
    if not identity_value:
        logger.warning('跳过 %s：身份键 %s 取值为空', label, id_key)
        return

    sets = ''.join(
        f".property(single,'{k}',{_lit(v)})"
        for k, v in sorted(props.items()) if v is not None and k != id_key
    )
    g = (
        f"g.V().has('{label}','{id_key}','{safe_str(identity_value)}').fold()"
        f".coalesce(__.unfold(),"
        f" __.addV('{label}').property(single,'{id_key}','{safe_str(identity_value)}')"
        f".property(single,'source','{SOURCE}')"
        f".property(single,'first_seen',{round_ts}))"
        f".property(single,'environment','{ENVIRONMENT}')"
        f"{sets}"
        f".property(single,'last_seen',{round_ts})"
    )
    neptune_query(g)


def _lit(v) -> str:
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, (int, float)):
        return str(v)
    return f"'{safe_str(str(v))}'"


def _upsert_edge(label: str, src_label: str, src_id: str,
                 dst_label: str, dst_id: str, round_ts: int,
                 props: dict | None = None) -> None:
    """按契约声明的端点约束 upsert 一条边。

    **source / dependency_kind / first_seen 只在新建时写** —— 它们是契约声明的
    写一次属性（edge_write_once_attrs），记录「谁首先发现了这条依赖」。
    被后写的源覆盖等于抹掉发现史；etl_aws 与 etl_cfn 都犯过这个错（无条件
    .property('source',...)），见 test_35::g08。
    """
    assert_edge_type(label, src_label, dst_label)
    assert_source(SOURCE, f'_upsert_edge({src_label}-[{label}]->{dst_label})')

    s_key = identity_prop_for(src_label)
    d_key = identity_prop_for(dst_label)
    upd = ''.join(
        f".property('{k}',{_lit(v)})" for k, v in sorted((props or {}).items())
        if v is not None
    )
    g = (
        f"g.V().has('{src_label}','{s_key}','{safe_str(src_id)}').as('s')"
        f".V().has('{dst_label}','{d_key}','{safe_str(dst_id)}')"
        f".coalesce("
        f"  __.inE('{label}').where(__.outV().has('{s_key}','{safe_str(src_id)}')),"
        f"  __.addE('{label}').from('s')"
        f"   .property('source','{SOURCE}')"
        f"   .property('dependency_kind','dynamic')"
        f"   .property('first_seen',{round_ts})"
        f")"
        f"{upd}"
        f".property('last_seen',{round_ts})"
        f".property('active',true)"
    )
    neptune_query(g)


def write_control_plane(cp: dict, gw_targets: dict, round_ts: int) -> dict:
    """写资源节点 + 控制面能看到的静态边。"""
    n = defaultdict(int)

    for r in cp['runtimes'][1]:
        arn = r.get('agentRuntimeArn')
        _upsert_node('AgentRuntime', arn, {
            'runtime_id': r.get('agentRuntimeId'),
            'name': r.get('agentRuntimeName'),
            'status': r.get('status'),
            'version': r.get('agentRuntimeVersion'),
        }, round_ts)
        n['AgentRuntime'] += 1

    for gw in cp['gateways'][1]:
        arn = gw.get('gatewayArn') or gw.get('arn')
        _upsert_node('AgentGateway', arn, {
            'gateway_id': gw.get('gatewayId'),
            'name': gw.get('name'),
            'status': gw.get('status'),
            'protocol': gw.get('protocolType'),
        }, round_ts)
        n['AgentGateway'] += 1

    for m in cp['memories'][1]:
        _upsert_node('AgentMemory', m.get('arn'), {
            'memory_id': m.get('id'),
            'name': m.get('name'),
            'status': m.get('status'),
        }, round_ts)
        n['AgentMemory'] += 1

    for kb in cp['kbs'][1]:
        _upsert_node('KnowledgeBase', kb.get('knowledgeBaseArn') or kb.get('knowledgeBaseId'), {
            'kb_id': kb.get('knowledgeBaseId'),
            'name': kb.get('name'),
            'status': kb.get('status'),
        }, round_ts)
        n['KnowledgeBase'] += 1

    for gr in cp['guardrails'][1]:
        _upsert_node('Guardrail', gr.get('arn'), {
            'guardrail_id': gr.get('id'),
            'name': gr.get('name'),
            'status': gr.get('status'),
            'version': gr.get('version'),
        }, round_ts)
        n['Guardrail'] += 1

    # Gateway -[RoutesTo]-> AgentTool，以及 AgentTool -[DependsOn]-> 真实后端。
    # 后者是把 agent 子图接回既有 PetSite 图的**唯一**通路 —— 缺了它，
    # agent 节点在图里是一座孤岛，能查内部结构却回答不了
    # 「petsite 挂了会影响哪个 agent」。
    for gw_arn, targets in gw_targets.items():
        for t in targets:
            tname = t.get('name') or t.get('targetId')
            if not tname:
                continue
            tool_key = f'{gw_arn}#{tname}'
            _upsert_node('AgentTool', tool_key, {
                'tool_name': tname,
                'owner_arn': gw_arn,
                'owner_kind': 'gateway',
                'backend_kind': _backend_kind(t),
            }, round_ts)
            n['AgentTool'] += 1
            _upsert_edge('RoutesTo', 'AgentGateway', gw_arn,
                         'AgentTool', tool_key, round_ts)
            n['RoutesTo'] += 1

            back_label, back_id = _backend_ref(t)
            if back_label:
                _upsert_edge('DependsOn', 'AgentTool', tool_key,
                             back_label, back_id, round_ts)
                n['DependsOn'] += 1
    return dict(n)


def _backend_kind(target: dict) -> str:
    cfg = target.get('targetConfiguration') or {}
    mcp = cfg.get('mcp') or {}
    for k in ('lambda', 'openApiSchema', 'smithyModel'):
        if k in mcp:
            return k
    return 'unknown'


def _backend_ref(target: dict):
    """把 Gateway target 解析成图里已有的节点引用。

    只处理 Lambda —— OpenAPI / 外部 MCP server 的后端是一个 URL，
    映射到哪个 Microservice 需要 URL→服务的对照表，那属于 profile 的职责，
    这里**刻意不猜**（猜错会造出一条假依赖边，比缺一条边更糟）。
    """
    cfg = ((target.get('targetConfiguration') or {}).get('mcp') or {}).get('lambda') or {}
    arn = cfg.get('lambdaArn')
    if not arn:
        return None, None
    # LambdaFunction 的契约身份键是 name（被 preferred_blocked_by 卡着，见 g13），
    # 所以这里要从 ARN 取函数名而不是直接用 ARN。
    return 'LambdaFunction', arn.rsplit(':function:', 1)[-1].split(':')[0]


def write_span_edges(rows: list, round_ts: int) -> dict:
    """从 span 写调用边。runtime_id → AgentRuntime 的 arn 需要先建索引。"""
    n = defaultdict(int)
    idx = _runtime_index()
    if not idx:
        logger.info('图里还没有 AgentRuntime 节点，跳过 span 边写入')
        return dict(n)

    for r in rows:
        arn = idx.get(r.get('runtime_id') or '')
        if not arn:
            continue
        calls = int(float(r.get('calls') or 0))
        props = {'calls': calls}

        tool = r.get('tool_name')
        if tool:
            tool_key = f'{arn}#{tool}'
            _upsert_node('AgentTool', tool_key, {
                'tool_name': tool, 'owner_arn': arn, 'owner_kind': 'runtime',
            }, round_ts)
            _upsert_edge('InvokesTool', 'AgentRuntime', arn,
                         'AgentTool', tool_key, round_ts, props)
            n['InvokesTool'] += 1

        peer = r.get('peer_agent')
        if peer and idx.get(peer) and idx[peer] != arn:
            _upsert_edge('Delegates', 'AgentRuntime', arn,
                         'AgentRuntime', idx[peer], round_ts, props)
            n['Delegates'] += 1

        kb = r.get('kb_id')
        if kb:
            kb_arn = _kb_arn_for(kb)
            if kb_arn:
                _upsert_edge('Retrieves', 'AgentRuntime', arn,
                             'KnowledgeBase', kb_arn, round_ts, props)
                n['Retrieves'] += 1
    return dict(n)


def _runtime_index() -> dict:
    """{runtime_id: arn, name: arn} —— span 里的 cloud.resource_id 是 runtime id，
    而契约身份键是 arn，必须有这层映射。同时收 name 以便解析 peer_agent。"""
    try:
        rows = neptune_query(
            "g.V().hasLabel('AgentRuntime')"
            ".project('arn','rid','name')"
            ".by(values('arn').fold()).by(values('runtime_id').fold())"
            ".by(values('name').fold())"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning('取 AgentRuntime 索引失败: %s', exc)
        return {}
    idx = {}
    for item in (rows.get('result', {}).get('data', {}).get('@value', []) or []):
        d = extract_value(item) or {}
        arn = (d.get('arn') or [None])[0] if isinstance(d.get('arn'), list) else d.get('arn')
        if not arn:
            continue
        for k in ('rid', 'name'):
            v = d.get(k)
            v = v[0] if isinstance(v, list) and v else v
            if v:
                idx[str(v)] = arn
    return idx


def _kb_arn_for(kb_id: str):
    try:
        rows = neptune_query(
            f"g.V().hasLabel('KnowledgeBase').has('kb_id','{safe_str(kb_id)}')"
            f".values('arn').limit(1)"
        )
        vals = rows.get('result', {}).get('data', {}).get('@value', []) or []
        return extract_value(vals[0]) if vals else None
    except Exception:  # noqa: BLE001
        return None


# ── 入口 ─────────────────────────────────────────────────────────────────────
def lambda_handler(event=None, context=None):
    round_ts = int(time.time())
    cp = collect_control_plane()
    gw_targets = collect_gateway_targets(cp['gateways'][1])
    node_stats = write_control_plane(cp, gw_targets, round_ts)

    span_status, span_rows = collect_spans()
    edge_stats = write_span_edges(span_rows, round_ts)

    # 采集状态逐项上报 —— 「全空」与「全失败」在计数上都是 0，
    # 不把状态带出来就无法区分「还没部署 agent」和「权限丢了」。
    statuses = {k: v[0] for k, v in cp.items()}
    statuses['spans'] = span_status
    failed = [k for k, v in statuses.items() if v == _probe_status.FAILED]

    result = {
        'round_ts': round_ts,
        'nodes': node_stats,
        'edges': edge_stats,
        'collection_status': statuses,
        'failed_collections': failed,
        'source': SOURCE,
        'span_lookback_seconds': SPAN_LOOKBACK_SECONDS,
    }
    if failed:
        logger.error('有采集项失败，本轮结果不完整: %s', failed)
    logger.info('etl_agentcore 完成: %s', json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    print(json.dumps(lambda_handler(), ensure_ascii=False, indent=2))
