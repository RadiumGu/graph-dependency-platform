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
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, '/opt/python')

from neptune_client_base import neptune_query, safe_str, extract_value, REGION  # noqa: E402
from graph_contract import (  # noqa: E402
    assert_edge_type,
    assert_node_type,
    assert_source,
    identity_prop_for,
    is_dependency_edge,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

SOURCE = 'agentcore-etl'
ENVIRONMENT = os.environ.get('ENVIRONMENT', 'prod')

# ── 「业务服务 → AgentRuntime」的声明来源（方法 1，2026-09-05）─────────────────
# PetSite 在 WaggleController.cs 里读这个 SSM 参数拿 runtime ARN，然后
# InvokeAgentRuntimeAsync。参数名 → 调用方微服务规范名的映射写在这里而不是猜：
# 参数前缀 /petstore/agent/ 是 PetSite 那套应用的配置根。
SSM_AGENT_PREFIX = os.environ.get('AGENT_SSM_PREFIX', '/petstore/agent')
SSM_RUNTIME_PARAM_CALLERS = {
    f'{SSM_AGENT_PREFIX}/waggleairuntimearn': 'petsite',
}
# CloudWatch 里 AgentCore 的 namespace。**是连字符不是斜杠** ——
# `AWS/BedrockAgentCore` 返回 0 个指标，正确的是 `AWS/Bedrock-AgentCore`（235 个）。
CW_AGENTCORE_NAMESPACE = os.environ.get('CW_AGENTCORE_NAMESPACE', 'AWS/Bedrock-AgentCore')
# runtime 活性回看窗口。取 24h 而非 6h：agent 调用是稀疏突发的
# （实测同一天 03:25 一批、07:47 一批，中间四小时空白），6h 窗口会频繁读到空。
ACTIVITY_WINDOW_SECONDS = int(os.environ.get('AGENTCORE_ACTIVITY_WINDOW_SECONDS', str(24 * 3600)))

# aws/spans 的回看窗口。与 AccessesData / Delegates / InvokesTool / Retrieves 的
# expires_seconds=21600（6h）**刻意取同一个值** —— 采集窗口小于 TTL 会让边在
# 「还没到期但本轮没看到」时被误判失活；大于 TTL 则写进来的边立刻就是过期的。
SPAN_LOOKBACK_SECONDS = int(os.environ.get('AGENTCORE_SPAN_LOOKBACK_SECONDS', str(6 * 3600)))
# AgentCore 的 span **不在** 共享的 aws/spans 里，而是按 runtime 分散在
# /aws/bedrock-agentcore/runtimes/<runtime_id>-<endpoint>/ 的 spans 流里。官方原文：
#   "spans go to the `spans` log stream in
#    /aws/bedrock-agentcore/runtimes/<agent_id>-<endpoint_name>,
#    **instead of the shared `aws/spans` log group**."
#
# ⚠️ 这里曾经默认 'aws/spans'，是个静默 bug（2026-09-05 实测发现）。把同一条查询
# 打到两个日志组上，结果相反：aws/spans **0 行**（它有数据，但是别的服务的 span，
# 实测最新两条是 SSM Get parameter），per-runtime 日志组 **3 行**
# （op=invoke_agent 234 / chat 312 / execute_tool tool=search_available_pets 78）。
#
# 一个默认值造成三个现象：每轮 collection_status.spans 都报 empty（不是 agent 没被
# 调用，是问错了地方）；runtime 侧的 AgentTool 节点永远拿不到属性（它们只能从 span
# 发现）；每轮 edges 都是空的（所有 agent 依赖边都来自 span 路）。
SPAN_LOG_GROUP_PREFIX = os.environ.get(
    'AGENTCORE_SPAN_LOG_GROUP_PREFIX', '/aws/bedrock-agentcore/runtimes/')
# 显式指定则不再按前缀发现（逗号分隔）。留这个口子是为了在只想查单个 runtime 时收窄范围。
SPAN_LOG_GROUPS_OVERRIDE = [
    g.strip() for g in (os.environ.get('AGENTCORE_SPAN_LOG_GROUPS') or '').split(',')
    if g.strip()
]
# Insights 单次查询的日志组上限是 50。
MAX_INSIGHTS_LOG_GROUPS = 50
# 当 span 没带 kb_id 时，用账号内唯一的营养 KB 作兜底（可用环境变量覆盖）。
AGENT_KB_FALLBACK_ID = os.environ.get('AGENTCORE_NUTRITION_KB_ID', '')
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
    # 「问对了地方、确实没有」与「问错了地方、所以没有」在计数上都是 0，
    # 而 EMPTY 只能表达前者。2026-09-05 实测踩到的正是后者：span 路默认查
    # aws/spans，每轮都诚实地报 empty，而真正的 agent span 在 per-runtime
    # 日志组里躺了一整天没人读。
    #
    # 所以要有第三种状态：**已知存在 N 个 agent runtime 日志组、且控制面确认有
    # N 个活跃 runtime，span 查询却零命中** —— 这不是「空」，这是自相矛盾，
    # 必须当失败上报。没有这条断言，同类 bug 会再次静默一整天。
    CONTRADICTORY = 'contradictory'


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


def _enrich_runtimes(acc, runtimes: list) -> list:
    """给每个 runtime 补上**注入目标属性**：执行角色与网络配置。

    ## 为什么必须在采集阶段做

    `list_agent_runtimes` 只返回 arn/id/name/version/status/lastUpdatedAt ——
    **不含** roleArn 与 networkConfiguration，必须逐个 `get_agent_runtime`。

    第一版我把这段写在 `write_control_plane()` 里，结果 `NameError: name 'acc'
    is not defined` —— 那是**写入**函数，拿不到采集阶段的客户端。修法不是在写入
    函数里另建一个客户端（那会把采集职责混进写入层，且每轮多建一次连接），
    而是在这里富化好再交给写入层。

    ## 这三个属性各对应一条已确认的注入路径

        role_arn            aws:fis:inject-api-* 的目标类型就是 aws:iam:role。
                            ⚠️ 但其 service 参数官方**只支持 ec2 与 kinesis**
                            （FIS Actions reference 原文），打不了 bedrock-agentcore。
                            留着它是为了 aws-samples/fis-template-library 的
                            agentcore-strands-agent-faults 模板 —— 那条路是
                            FIS -> SSM Automation -> /chaos/{runtime_id}/* 参数，
                            需要 runtime 执行角色有 ssm:GetParametersByPath。
        subnet_ids          aws:network:disrupt-connectivity 的目标是**子网**。
                            实测这些子网属于 vpc-010ab37a3f9f74725，与 EKS 同 VPC，
                            所以 agent 的出向连通性可被切断。
        security_group_ids  安全组级隔离的目标。

    没有它们，图谱能说「这条 agent 依赖存在」，却无法回答「要验证它该往哪注入」。

    失败不致命但必须 log：静默缺失会让选靶误以为「这个 runtime 本来就没有角色」。
    """
    out = []
    for r in runtimes or []:
        rid = r.get('agentRuntimeId')
        if not rid:
            out.append(r)
            continue
        try:
            det = acc.get_agent_runtime(agentRuntimeId=rid)
            net = det.get('networkConfiguration') or {}
            nmc = net.get('networkModeConfig') or {}
            r = dict(r)
            if det.get('roleArn'):
                r['_role_arn'] = det['roleArn']
            if net.get('networkMode'):
                r['_network_mode'] = net['networkMode']
            if nmc.get('subnets'):
                r['_subnet_ids'] = ','.join(nmc['subnets'])
            if nmc.get('securityGroups'):
                r['_security_group_ids'] = ','.join(nmc['securityGroups'])
            proto = (det.get('protocolConfiguration') or {}).get('serverProtocol')
            if proto:
                r['_server_protocol'] = proto
        except Exception as e:
            logger.warning(
                "get_agent_runtime(%s) 失败，该 runtime 缺注入目标属性"
                "（role_arn/subnet_ids/security_group_ids）—— 选靶将无法自动化: %r",
                rid, e)
        out.append(r)
    return out


def _enrich_gateways(acc, items: list) -> list:
    """给每个 gateway 补上 ARN。

    ⚠️ 实测：`list_gateways` **根本不返回 ARN** —— 它只给
    gatewayId / name / status / authorizerType / description / createdAt / updatedAt。
    ARN 只能从 `get_gateway` 的 `gatewayArn` 拿。

    而契约规定 AgentGateway 的身份键是 `arn`，所以不补这一步，
    节点会被门禁以「身份键 arn 取值为空」跳过 —— **静默少一个节点**，
    而且日志只是 WARNING，很容易被当成正常。
    这正是首次实跑时发生的事：5 个 Runtime 都写进去了，Gateway 一个没有。
    """
    out = []
    for it in items or []:
        gid = it.get('gatewayId')
        if not gid:
            out.append(it)
            continue
        try:
            detail = acc.get_gateway(gatewayIdentifier=gid)
        except Exception as exc:  # noqa: BLE001
            logger.warning('get_gateway(%s) 失败，该 gateway 将因缺 arn 被跳过: %s', gid, exc)
            out.append(it)
            continue
        merged = dict(it)
        # get_gateway 的字段更全，但以 list 的 status 为准（两者可能有滞后差异）
        for k, v in detail.items():
            if k not in ('status',):
                merged.setdefault(k, v)
        merged['arn'] = detail.get('gatewayArn') or merged.get('gatewayArn')
        out.append(merged)
    return out


def collect_control_plane() -> dict:
    """返回 {kind: (status, items)}。"""
    acc = boto3.client('bedrock-agentcore-control', region_name=REGION)
    br = boto3.client('bedrock', region_name=REGION)
    bra = boto3.client('bedrock-agent', region_name=REGION)

    return {
        'runtimes': _collect(
            lambda: _enrich_runtimes(acc, _paged(acc, 'list_agent_runtimes', 'agentRuntimes')),
            'AgentRuntime'),
        'gateways': _collect(
            lambda: _enrich_gateways(acc, _paged(acc, 'list_gateways', 'items')),
            'AgentGateway'),
        'memories': _collect(
            lambda: _paged(acc, 'list_memories', 'memories'), 'AgentMemory'),
        'guardrails': _collect(
            lambda: br.list_guardrails().get('guardrails') or [], 'Guardrail'),
        'kbs': _collect(
            lambda: bra.list_knowledge_bases().get('knowledgeBaseSummaries') or [],
            'KnowledgeBase'),
    }


def collect_service_to_runtime_declarations() -> dict:
    """从 SSM 参数发现「业务服务 → AgentRuntime」的**声明**边（方法 1，2026-09-05）。

    ## 补的是什么盲区

    实测：图谱里 agent 子图是**孤岛** —— 它只靠 `search_available_pets -> petsearch`
    一条边挂在业务系统上，而**真正的入口边 `petsite -> WaggleAIOrchestrator` 不存在**。
    后果是爆炸半径查询答不出「AgentCore 挂了会影响 PetSite 什么功能」，
    而这一跳恰恰是业务关键路径：断了 PetSite 的 AI 问答就不可用（真断过一次，
    IRSA 角色缺 `bedrock-agentcore:InvokeAgentRuntime`）。

    ## 为什么五个数据源都看不见它

    这一跳是 PetSite 容器里的一次 AWS SDK 调用：
      · DeepFlow 看不见 —— 跨 VPC 到 AWS 托管服务的 HTTPS
      · X-Ray 当时看不见 —— PetSite 的 .NET SDK 没给 AgentCore 客户端注册 handler
        （实测服务图里 `PetSite -> SimpleSystemsManagement` 存在、到 Waggle 的边不存在）
      · 控制面看不见 —— AgentCore 只知道自己被调了，不知道谁调的

    **但 SSM 参数是现成的声明证据**，只是此前没有任何 ETL 读它来建边。

    ## 证据等级要说实话

    这是 `static`：它证明「配置上 petsite 应该调它」，**不证明「真的调了」**。
    配置留着而功能下线的情况它分不出来。真正证明「调了」要靠调用方侧的 X-Ray 埋点
    （方法 2），那会给同一条边加一个独立观测源，置信度 0.7311 -> 0.8176。

    ## 为什么不用 CloudWatch 指标当边证据

    `AWS/Bedrock-AgentCore` 的 `Invocations` 确实在发布数据点（实测 24h 内 74 次），
    但维度只有 `Resource`/`Operation`/`Name`/`ComputeType` —— **没有调用方维度**。
    它只能说明「这个 runtime 被调了 74 次」，不能说明「petsite 调了它」；
    况且那 74 次里混着管理员直调。把它算成这条边的独立观测源是给置信度注水，
    与「throughput_only 通道不得判 hard」是同一条纪律。
    它改为写在 runtime **节点**上（见 `collect_runtime_activity`）。
    """
    ssm = boto3.client('ssm', region_name=REGION)
    out: dict = {}
    try:
        paginator = ssm.get_paginator('get_parameters_by_path')
        params = {}
        for page in paginator.paginate(Path=SSM_AGENT_PREFIX, Recursive=True):
            for p in page.get('Parameters') or []:
                params[p['Name']] = p.get('Value') or ''
    except Exception as e:
        # AccessDenied 单独识别：这条边是 PetSite→AgentCore 唯一的证据来源
        # （X-Ray/DeepFlow/CloudWatch 都看不见这一跳，见 docs/），少了权限
        # 就等于这个功能静默失效。所以要点名缺什么、怎么补，而不是只说"失败"。
        if 'AccessDenied' in repr(e) or 'UnauthorizedOperation' in repr(e):
            logger.error(
                '⚠️ 无权读 SSM %s —— service→runtime 声明边**不会被建出**，'
                'PetSite→AgentCore 这一跳会重新变成盲区（其他数据源都看不见它）。'
                '需要给执行角色加 ssm:GetParametersByPath + ssm:GetParameters，'
                '资源限定 arn:aws:ssm:<region>:<acct>:parameter%s/*。原始错误：%r',
                SSM_AGENT_PREFIX, SSM_AGENT_PREFIX, e)
        else:
            logger.warning('读 SSM %s 失败（非致命）：%r', SSM_AGENT_PREFIX, e)
        return out

    for name, caller in SSM_RUNTIME_PARAM_CALLERS.items():
        arn = params.get(name)
        if not arn:
            logger.info('SSM 参数 %s 不存在或为空，跳过', name)
            continue
        if not arn.startswith('arn:aws:bedrock-agentcore:'):
            # 参数存在但不是 runtime ARN —— 宁可跳过也不建一条端点错误的边
            logger.warning('SSM 参数 %s 的值不是 AgentCore runtime ARN，跳过：%s',
                           name, arn[:80])
            continue
        out[arn] = {'caller': caller, 'declared_in': name}
    return out


def collect_runtime_activity(runtimes: list) -> dict:
    """从 CloudWatch 取每个 runtime 的调用活性（方法 3，2026-09-05）。

    **刻意写在节点上而不是边上。** 指标维度里没有调用方，所以它回答不了
    「谁在调」，只回答「这个 runtime 有没有被调、调了多少、错多少」。
    这恰好补 `static` 声明边的局限：配置在但功能实际没人用，靠这个能看出来。

    维度必须给全 `Resource`+`Operation`+`Name` 三个 —— 实测只给前两个返回
    **0 个数据点**（CloudWatch 维度精确匹配）。少给一个会让人误判「指标没数据」。
    """
    cw = boto3.client('cloudwatch', region_name=REGION)
    now = int(time.time())
    start = now - ACTIVITY_WINDOW_SECONDS
    out: dict = {}
    for r in runtimes:
        arn = r.get('agentRuntimeArn')
        nm = r.get('agentRuntimeName')
        if not arn or not nm:
            continue
        dims = [
            {'Name': 'Resource', 'Value': arn},
            {'Name': 'Operation', 'Value': 'InvokeAgentRuntime'},
            {'Name': 'Name', 'Value': f'{nm}::DEFAULT'},
        ]
        rec = {}
        for metric, key in (('Invocations', 'invocations'),
                            ('Errors', 'errors'),
                            ('UserErrors', 'user_errors'),
                            ('SystemErrors', 'system_errors')):
            try:
                resp = cw.get_metric_statistics(
                    Namespace=CW_AGENTCORE_NAMESPACE, MetricName=metric,
                    Dimensions=dims,
                    StartTime=datetime.fromtimestamp(start, tz=timezone.utc),
                    EndTime=datetime.fromtimestamp(now, tz=timezone.utc),
                    Period=3600, Statistics=['Sum'])
                pts = resp.get('Datapoints') or []
                # 采集失败与「真的是 0」必须能区分 —— 没有数据点时写 None
                # 而不是 0，否则「没采到」会伪装成「没被调用过」。
                rec[key] = sum(p['Sum'] for p in pts) if pts else None
            except Exception as e:
                logger.warning('取 %s/%s 失败：%r', nm, metric, e)
                rec[key] = None
        if rec.get('invocations') is not None:
            out[arn] = rec
    return out


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


def _discover_span_log_groups(logs) -> list:
    """列出 /aws/bedrock-agentcore/runtimes/ 前缀下的全部日志组。

    每个 runtime 一个日志组，数量随 agent 数增长；Insights 单次查询最多 50 个。
    超过 50 要告警 —— 静默截断会让部分 agent 的依赖边凭空消失。
    """
    if SPAN_LOG_GROUPS_OVERRIDE:
        return SPAN_LOG_GROUPS_OVERRIDE[:MAX_INSIGHTS_LOG_GROUPS]
    out, token = [], None
    while True:
        kw = {'logGroupNamePrefix': SPAN_LOG_GROUP_PREFIX, 'limit': 50}
        if token:
            kw['nextToken'] = token
        resp = logs.describe_log_groups(**kw)
        out += [g['logGroupName'] for g in resp.get('logGroups') or []]
        token = resp.get('nextToken')
        if not token:
            break
    if len(out) > MAX_INSIGHTS_LOG_GROUPS:
        logger.warning(
            'agent runtime 日志组 %d 个，超过 Insights 上限 %d，本轮只查前 %d 个'
            ' —— 其余 runtime 的依赖边本轮不会被刷新',
            len(out), MAX_INSIGHTS_LOG_GROUPS, MAX_INSIGHTS_LOG_GROUPS)
    return sorted(out)[:MAX_INSIGHTS_LOG_GROUPS]


def _run_insights(logs, groups: list, now: int, query: str) -> list:
    """跑一次 Logs Insights 并等结果。超时不当成空 —— 抛出交给调用方记为失败。"""
    qid = logs.start_query(
        logGroupNames=groups,
        startTime=now - SPAN_LOOKBACK_SECONDS,
        endTime=now,
        queryString=query,
    )['queryId']
    deadline = time.time() + INSIGHTS_TIMEOUT_SECONDS
    while time.time() < deadline:
        r = logs.get_query_results(queryId=qid)
        status = r.get('status')
        if status == 'Complete':
            return [{c['field']: c['value'] for c in row} for row in r.get('results', [])]
        if status in ('Failed', 'Cancelled', 'Timeout'):
            raise RuntimeError(f'Logs Insights 查询 {status}')
        time.sleep(1)
    logs.stop_query(queryId=qid)
    raise TimeoutError(f'Logs Insights 超过 {INSIGHTS_TIMEOUT_SECONDS}s 未完成')


def collect_spans(runtime_count: int = 0) -> tuple:
    """从**各 runtime 自己的**日志组抽 agent 调用关系。

    ⚠️ 硬前置：Transaction Search 必须已开（destination=CloudWatchLogs）。
    开启记录见 todo/agentobv/05-etl_xray影响面量化_20260904-0835.md
    第六节（2026-09-04 08:49:33Z ACTIVE，索引采样 100%）。

    ⚠️ **不要改回 `aws/spans`** —— 那里没有 agent 的 span。实测对比与三个后果
    记在 SPAN_LOG_GROUP_PREFIX 上面那段注释里。

    `runtime_count` 用于矛盾检测：控制面说有 N 个 runtime、日志组也在，
    span 却零命中 —— 那不是「空」，是查询本身有问题。
    """
    logs = boto3.client('logs', region_name=REGION)
    now = int(time.time())
    groups = _discover_span_log_groups(logs)
    if not groups:
        logger.warning('前缀 %s 下没有任何日志组 —— agent 可能尚未部署，或前缀配错了',
                       SPAN_LOG_GROUP_PREFIX)
        return _probe_status.EMPTY, []
    logger.info('span 采集覆盖 %d 个 runtime 日志组', len(groups))

    def _run():
        return _run_insights(logs, groups, now, SPAN_QUERY)

    st, rows = _collect(_run, f'{len(groups)} 个 runtime 日志组的 agent span')

    # 矛盾检测：零命中到底是「没人调用 agent」还是「我的查询问错了」？
    #
    # ⚠️ 这个判据改了两次，两次都是因为**把「没有观测」当成「有问题」的证据** ——
    # 本项目反复记录的同一个错误，我在同一天里犯了两遍。
    #
    #   第一版：有 runtime + 零命中 → 矛盾。
    #     打脸：14:00 那轮窗口 08:00–14:00，最后一次 agent 调用在 07:48，
    #     零 span 是真实的空。agent 调用稀疏突发（同一天 03:25 一批、07:47 一批，
    #     中间四小时空白），「6 小时没人调用」是常态。
    #   第二版：日志组有任何日志 + 零命中 → 矛盾。
    #     还是错：那些日志是 `[runtime-logs]` 应用输出，不是 span。探针问错了对象。
    #
    # 第三版（当前）。实测数据让判据变得清楚：
    #   `spans` 流有 526 条记录，其中 526 条带 resource.attributes.cloud.resource_id，
    #   但**零条**带 attributes.gen_ai.operation.name —— 那些是 agent 进程的
    #   SSM/boto3 客户端 span（实测 aws.remote.service=AWS::SSM 出现 34 次）。
    #
    # 于是「没人调用」与「gen_ai 字段名变了」用 gen_ai 字段本身**无法区分**，都是 0。
    # 能区分的是 `cloud.resource_id` —— 它是**资源**属性，实测 400/400 条 span 都有，
    # 与操作类型无关。三态判据：
    #
    #   spans == 0                → EMPTY   没有 span，没人调用或没埋点
    #   spans > 0, with_rid == 0   → 矛盾    span 在，但**身份字段名变了**（真回归）
    #   spans > 0, with_rid > 0    → EMPTY   span 与身份字段都在，只是窗口内
    #                                        没有 gen_ai 操作 = 没人调用 agent
    #
    # 剩下一个诚实的局限：`gen_ai.operation.name` 本身若被改名，表现与「没人调用」
    # 完全一致，这里查不出来。那要靠 scripts/probe_agent_span_attrs.py 定期实测，
    # 而不是假装门禁能覆盖。
    if st == _probe_status.EMPTY and runtime_count > 0:
        try:
            probe = _run_insights(
                logs, groups, now,
                "fields @timestamp | filter @logStream = 'spans' "
                "| stats count(*) as spans, "
                "count(resource.attributes.cloud.resource_id) as with_rid")
        except Exception as exc:  # noqa: BLE001
            logger.warning('对照探针失败，无法区分「真空」与「查错」，保守记为 empty: %r', exc)
            return _probe_status.EMPTY, []
        n_spans = int((probe[0].get('spans') if probe else 0) or 0)
        n_rid = int((probe[0].get('with_rid') if probe else 0) or 0)
        if n_spans == 0:
            logger.info(
                'span 零命中，且 %d 个 runtime 日志组的 spans 流在同一 %ds 窗口内也零记录 —— '
                '没人调用 agent。agent 调用稀疏突发，空是常态而非故障。',
                len(groups), SPAN_LOOKBACK_SECONDS)
            return _probe_status.EMPTY, []
        if n_rid == 0:
            logger.error(
                '❌ 矛盾：窗口内有 %d 条 span，但**没有一条**带 '
                'resource.attributes.cloud.resource_id。该字段是资源属性、'
                '与操作类型无关，实测应 100%% 覆盖 —— 它全缺说明**字段名变了**'
                '（ADOT 版本升级会改属性名）。用 scripts/probe_agent_span_attrs.py '
                '重新实测属性名。**不要当成 empty 放过** —— 读错日志组那个 bug '
                '曾以 empty 的形式静默一整天。', n_spans)
            return _probe_status.CONTRADICTORY, []
        logger.info(
            'span 零命中，但窗口内有 %d 条 span 且 %d 条带身份字段 —— '
            '说明埋点与字段名都正常，只是窗口内没有 gen_ai 操作，即没人调用 agent。'
            '这是真实的空。', n_spans, n_rid)
        return _probe_status.EMPTY, []
    return st, rows


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
                 props: dict | None = None,
                 dependency_kind: str = 'inference') -> None:
    """按契约声明的端点约束 upsert 一条边。

    **source / dependency_kind / first_seen 只在新建时写** —— 它们是契约声明的
    写一次属性（edge_write_once_attrs），记录「谁首先发现了这条依赖」。
    被后写的源覆盖等于抹掉发现史；etl_aws 与 etl_cfn 都犯过这个错（无条件
    .property('source',...)），见 test_35::g08。

    ## dependency_kind 为什么默认 'inference' 而不是 'dynamic'（2026-09-05 修）

    本项目 `dependency_kind` 的语义：
        static    = 配置/模板声明了这条依赖，但不代表当前有流量
        dynamic   = **持续**观测到流量
        inference = LLM 在运行时按 query 决定的调用 —— 既不是配置写死的，
                    也不是持续存在的

    agent 的工具调用**不满足 dynamic 的定义**。实测调用形态是稀疏突发：
    同一天里 03:25 一批、07:47 一批，中间四个小时完全空白；8 个工具中有 7 个
    最后一次调用都在 03:26，到 09:26 之后就再也落不进任何 6h 采集窗口。

    把它们标成 `dynamic` 的代价是可观测的：`deactivate_stale_dynamic_edges`
    用 `has('dependency_kind','dynamic')` 挑边，于是 `Retrieves -> nutrition-kb`
    在 2026-09-05 被置 `active=false` —— 而那个知识库客观存在（控制面
    list_knowledge_bases 就返回它），nutrition agent 也确实依赖它，
    只是几个小时没人问营养问题。**图谱因此给出了一个错误陈述，而不是过期陈述。**

    这正是本项目最核心那条不变量禁止的事：
        零流量与健康在指标上无法区分 → 一律 inconclusive，绝不判 refuted
    对 `petsite -> petsearch`（300s 内 25,042 次）来说「30 分钟没调用」确实说明
    变了；对一天被调 35 次的 agent 工具，「6 小时没调用」什么也不说明。

    `inference` 这个取值是 `todo/agentobv/02-agent可观测性方案` 4.2 节提议的，
    理由与这里完全一致（「一条低频 query 才触发的边不该因为没出现就被判失效」）。
    我在 2026-09-05 的对账里判它「不需要落地，窗口=TTL 已解决」—— **那个判断是错的**，
    本轮 Retrieves 翻 false 就是它要防的那个故障。

    调用方需要显式传 `dependency_kind='static'` 的场景：**控制面**能独立证明
    存在的边（如 Gateway 的 target 列表），它们与流量无关。
    """
    assert_edge_type(label, src_label, dst_label)
    assert_source(SOURCE, f'_upsert_edge({src_label}-[{label}]->{dst_label})')

    # 非依赖边不得携带 dependency_kind —— 契约的 `dependency` 标志是权威。
    #
    # 2026-09-05 实测：本函数此前**无条件**写 dependency_kind，于是 5 条
    # AgentGateway-[RoutesTo]->AgentTool 带上了 `dependency_kind: static`，
    # 而 RoutesTo 声明为 `dependency: false`。etl_aws 与 etl_cfn 的同名函数
    # 一直有这道门禁，本函数是三个 ETL 里唯一漏掉的那个。
    #
    # 为什么 RoutesTo 不该改成依赖边：这个标签同时用在 ALB/TargetGroup 的转发上，
    # 改判会把那些结构边一起误标成依赖。若确实要表达「网关依赖工具」，
    # 应另立标签 —— 与 InvokesTool 当初刻意不复用 Invokes 是同一个判断
    # （见契约里 InvokesTool 的 note）。
    dep_kind_frag = (f".property('dependency_kind','{dependency_kind}')"
                     if is_dependency_edge(label) else '')

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
        f"{dep_kind_frag}"
        f"   .property('first_seen',{round_ts})"
        f")"
        f"{upd}"
        f".property('last_seen',{round_ts})"
        f".property('active',true)"
    )
    neptune_query(g)


def write_control_plane(cp: dict, gw_targets: dict, round_ts: int,
                        declarations: dict | None = None,
                        activity: dict | None = None) -> dict:
    """写资源节点 + 控制面能看到的静态边。"""
    n = defaultdict(int)

    for r in cp['runtimes'][1]:
        arn = r.get('agentRuntimeArn')
        rid = r.get('agentRuntimeId')
        # 注入目标属性在**采集阶段**由 _enrich_runtimes 富化好（带 _ 前缀），
        # 这里只做搬运 —— 写入函数拿不到采集阶段的 boto3 客户端。
        extra = {k.lstrip('_'): v for k, v in r.items() if k.startswith('_')}

        # 方法 3：调用活性写在**节点**上。维度里没有调用方，所以它答不了
        # 「谁在调」，只答「有没有被调」—— 当边证据会给置信度注水。
        act = (activity or {}).get(arn) or {}
        if act.get('invocations') is not None:
            extra['invocations_24h'] = act['invocations']
            # `Errors` 是 UserErrors + SystemErrors 的合并值，两者语义完全不同：
            #   UserErrors   = 调用方发了坏请求（4xx）——**runtime 是好的**
            #   SystemErrors = runtime 自己坏了（5xx）
            # 只写合并值会把前者误报成"这个 runtime 有故障"，带偏 RCA。
            # 2026-09-05 实测：WaggleAIAdoption 24h 内 34 次 Errors，
            # 拆开看是 **34 UserErrors / 0 SystemErrors**，且全部集中在一小时内
            # （那一小时 100 次调用，其余时段 30 次调用 0 错误）——
            # 所谓"26% 错误率"是把一小时突发平铺到 24 小时的假象，稳态是 0%。
            # 极可能全是同一个 runtimeSessionId < 33 字符的 ValidationException。
            extra['invocation_errors_24h'] = act.get('errors')       # 合并值，保留兼容
            extra['user_errors_24h'] = act.get('user_errors')        # 调用方的问题
            extra['system_errors_24h'] = act.get('system_errors')    # runtime 的问题
            extra['activity_source'] = CW_AGENTCORE_NAMESPACE

        _upsert_node('AgentRuntime', arn, {
            'runtime_id': rid,
            'name': r.get('agentRuntimeName'),
            'status': r.get('status'),
            'version': r.get('agentRuntimeVersion'),
            **extra,
        }, round_ts)
        n['AgentRuntime'] += 1

    # ── 方法 1：业务服务 → AgentRuntime 的声明边 ──────────────────────────
    # 放在 runtime 节点写完之后：边的两端都必须已存在，否则 upsert 会因为
    # 找不到端点而静默失败（本项目在 183 条错源边那次踩过端点查找的坑）。
    for arn, decl in (declarations or {}).items():
        caller = decl['caller']
        if arn not in {r.get('agentRuntimeArn') for r in cp['runtimes'][1]}:
            logger.warning('SSM 声明的 runtime %s 不在控制面列表里，跳过建边', arn[:80])
            continue
        _upsert_edge('DependsOn', 'Microservice', caller, 'AgentRuntime', arn, round_ts,
                     props={'declared_in': decl['declared_in']},
                     # static：SSM 参数是配置声明，不是流量观测。
                     dependency_kind='static')
        n['DependsOn:service->runtime'] += 1

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
                # name 与 tool_name 同值：身份键是 tool_key，但**项目约定要求
                # 每个节点都带 name 作为展示属性** —— upsert_vertex 的注释写得很明确：
                # 「以非 name 作身份时 name 必须进 onMatch，否则改名后图谱留旧名字」。
                # 实测代价：10/10 个 AgentTool 缺 name，于是在所有按 name 的查询里
                # 显示为 `?`/`<unnamed>` —— 图仿真报「端点节点没有 name 属性」、
                # 判定日志打成 `? -> petsearch`，这条桥接边看起来像脏数据。
                'name': tname,
                'tool_name': tname,
                'owner_arn': gw_arn,
                'owner_kind': 'gateway',
                'backend_kind': _backend_kind(t),
            }, round_ts)
            n['AgentTool'] += 1
            # 这两条边来自**控制面**（Gateway 的 target 列表），与流量无关 ——
            # 所以是 static（配置声明）而不是 inference（LLM 运行时决定的调用）。
            # 区分的实际后果：static 边不该因为没观测到就被判失效，那是
            # drift_status 的 declared_not_observed 要表达的信息。
            _upsert_edge('RoutesTo', 'AgentGateway', gw_arn,
                         'AgentTool', tool_key, round_ts,
                         dependency_kind='static')
            n['RoutesTo'] += 1

            back_label, back_id = _backend_ref(t)
            if back_label:
                _upsert_edge('DependsOn', 'AgentTool', tool_key,
                             back_label, back_id, round_ts,
                             dependency_kind='static')
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


# ── span 到边的映射（实测校准，勿凭直觉改）─────────────────────────────────────

def _runtime_id_from_span(raw: str) -> str:
    """从 span 的 runtime_id 里取出裸 runtime id。

    ⚠️ 实测：`aws/spans` 里的 runtime_id 是**带 endpoint 后缀的完整 ARN**：
        arn:aws:bedrock-agentcore:<r>:<acct>:runtime/WaggleAIOrchestrator-K85tG867Xt/runtime-endpoint/DEFAULT:DEFAULT
    而 `_runtime_index()` 的键是裸 id（WaggleAIOrchestrator-K85tG867Xt）和名字。
    直接 `idx.get(runtime_id)` **永远匹配不上** —— 首次实跑就是这样：
    span 收到 12 条、节点全部写入，但 edges 是 `{}`，而且**不报任何错**。
    """
    if not raw:
        return ''
    if 'runtime/' in raw:
        tail = raw.split('runtime/', 1)[1]
        # 去掉 /runtime-endpoint/... 与 :DEFAULT 后缀
        return tail.split('/', 1)[0].split(':', 1)[0]
    return raw.split(':', 1)[0]


# orchestrator 用来委派子 agent 的 tool 名 -> 子 agent 的 runtime 名。
#
# ⚠️ **不能用 span 的 `peer_agent` 字段判断委派** —— 实测它的值是**框架名**
#    （'Strands Agents' / 'LangGraph' / 'Agent'），不是 agent 名字，
#    拿它去查 runtime 索引必然落空。
#    真正的委派信号是 orchestrator 的 `execute_tool` + tool_name。
_DELEGATION_TOOLS: dict = {
    'nutrition_advisor': 'WaggleAINutrition',
    'nutrition': 'WaggleAINutrition',
    'ordering': 'WaggleAIOrdering',
    'order_specialist': 'WaggleAIOrdering',
    'adoption': 'WaggleAIAdoption',
    'adoption_specialist': 'WaggleAIAdoption',
    'concierge': 'WaggleAIConcierge',
    # 2026-09-06 补：Orchestrator 实际注册的 tool 名是 `concierge_chat` /
    # `food_ordering`（图上 InvokesTool 边指向的就是这两个节点），
    # 而表里原先只有 `concierge` / `ordering` —— span 里出现真实 tool 名时
    # 查表落空，`Delegates` 边永远建不出来。
    #
    # 实测后果：Orchestrator 只有 2 条 Delegates（→ Nutrition / Adoption），
    # Concierge 与 Ordering 在图上**从 Orchestrator 不可达** ——
    # 爆炸半径查询答不出"这两个 agent 挂了影响谁"。
    #
    # ⚠️ 刻意只补映射，**不直接往图里插边**：`Delegates` 是观测驱动的
    # （只有 span 里真出现 execute_tool 才建），凭"对称性应该有"硬插边
    # 会把未观测到的关系写成观测事实。补映射后，等真实调用发生自然建出。
    'concierge_chat': 'WaggleAIConcierge',
    'food_ordering': 'WaggleAIOrdering',
}

# 表示「去 KB 取知识」的 tool 名 —— 用来建 Retrieves 边。
_KB_TOOLS = {'retrieve_nutrition_guidance', 'retrieve_nutrition', 'nutrition_kb'}

# tool 名 -> 它实际打到的后端 Microservice（既有图里的 name）。
#
# ⚠️ **这是把 agent 子图接回既有图的唯一通路**（Stage 2 契约扩展时就是这么定的：
#    AgentTool -[DependsOn]-> {LambdaFunction, Microservice}）。
#    不写这一段的后果我实测过：agent 节点的出入边全部只在子图内部循环
#    （RoutesTo 5 / Delegates 2 / InvokesTool 5 / Retrieves 1），
#    到 Microservice / LambdaFunction 的边数是 **0** ——
#    图里就成了两座**孤岛**：1107 个既有节点和 15 个 agent 节点毫无关联。
#    此时「依赖关系与实际系统一致」只成立一半，而且**没有任何断言会失败**，
#    因为节点和边各自都在、类型也都合法。
#
# 映射依据（不是猜的）：agent 的 common/config.py 里 _BACKEND_SSM_NAMES 把
# 逻辑键映到 SSM 短名，我们又把短名的值指向 internal ALB 的具体端口，
# 而每个端口的目标组绑的是哪个 k8s service 是确定的：
#   search_available_pets  -> searchapiurl        -> :8081 -> search-service    -> petsearch
#   get_available_foods    -> petfoodapiurl       -> （petfood 未部署，先留空）
#   list_adoptions 类      -> petlistadoptionsurl -> :8082 -> list-adoptions     -> petlistadoptions
#   complete_adoption 类   -> paymentapiurl       -> :8083 -> pay-for-adoption   -> payforadoption
_TOOL_BACKEND: dict = {
    'search_available_pets': 'petsearch',
    'search_pets': 'petsearch',
    'get_pet_details': 'petsearch',
    'list_adoptions': 'petlistadoptions',
    'get_adoption_list': 'petlistadoptions',
    'complete_adoption': 'payforadoption',
    'pay_for_adoption': 'payforadoption',
}


def write_span_edges(rows: list, round_ts: int) -> dict:
    """从 span 写调用边。runtime_id → AgentRuntime 的 arn 需要先建索引。"""
    n = defaultdict(int)
    idx = _runtime_index()
    if not idx:
        logger.info('图里还没有 AgentRuntime 节点，跳过 span 边写入')
        return dict(n)

    for r in rows:
        arn = idx.get(_runtime_id_from_span(r.get('runtime_id') or ''))
        if not arn:
            continue
        calls = int(float(r.get('calls') or 0))
        props = {'calls': calls}

        tool = r.get('tool_name')
        if tool:
            tool_key = f'{arn}#{tool}'
            # name 同 tool_name，理由见上面 gateway 侧那处注释（项目约定：
            # 非 name 身份键的节点仍必须带 name 作展示属性）
            _upsert_node('AgentTool', tool_key, {
                'name': tool,
                'tool_name': tool, 'owner_arn': arn, 'owner_kind': 'runtime',
            }, round_ts)
            _upsert_edge('InvokesTool', 'AgentRuntime', arn,
                         'AgentTool', tool_key, round_ts, props)
            n['InvokesTool'] += 1

            # 把 tool 接到它真正打的后端服务上 —— 这是 agent 子图与既有图的唯一连接点。
            backend = _TOOL_BACKEND.get(tool.lower())
            if backend:
                try:
                    _upsert_edge('DependsOn', 'AgentTool', tool_key,
                                 'Microservice', backend, round_ts, props)
                    n['DependsOn'] += 1
                except Exception as exc:  # noqa: BLE001
                    # 后端节点可能还不存在（服务未部署 / etl_deepflow 还没跑到），
                    # 这属于正常情况，不该让整个 ETL 失败。
                    logger.warning('AgentTool %s -> Microservice %s 建边失败: %s',
                                   tool, backend, exc)

        # Delegates：由 execute_tool 的 tool_name 判定，不是 peer_agent（那是框架名）
        if tool:
            peer_name = _DELEGATION_TOOLS.get(tool.lower())
            peer_arn = idx.get(peer_name) if peer_name else None
            if peer_arn and peer_arn != arn:
                _upsert_edge('Delegates', 'AgentRuntime', arn,
                             'AgentRuntime', peer_arn, round_ts, props)
                n['Delegates'] += 1

        kb = r.get('kb_id')
        # span 里通常没有 kb_id 字段（实测 12 行里一条都没有），
        # 所以还要认「取知识」这类 tool 名，否则 Retrieves 永远建不出来。
        if not kb and tool and tool.lower() in _KB_TOOLS:
            kb = AGENT_KB_FALLBACK_ID or ''
        if kb:
            kb_arn = _kb_arn_for(kb)
            if kb_arn:
                _upsert_edge('Retrieves', 'AgentRuntime', arn,
                             'KnowledgeBase', kb_arn, round_ts, props)
                n['Retrieves'] += 1
    return dict(n)


def _gmap(item) -> dict:
    """把 Gremlin 的 `g:Map` 解成 Python dict。

    ⚠️ 这一步不能省，也不能用 `extract_value` 代替。
    `g:Map` 在 GraphSON 里是**扁平的键值交替列表**，不是对象：

        {"@type": "g:Map",
         "@value": ["arn", {"@type":"g:List","@value":[...]},
                    "rid", {"@type":"g:List","@value":[...]}]}

    `extract_value(item)` 拿到的是 `@value` 的**第一个元素**，也就是字符串
    `"arn"`（键名本身），于是后面 `d.get('arn')` 报
    `AttributeError: 'str' object has no attribute 'get'`。

    我第一次「修」这个 bug 时加的是 `if not isinstance(d, dict): continue` ——
    那把崩溃换成了**静默跳过所有行**，索引恒为空，ETL 于是报
    「图里还没有 AgentRuntime 节点，跳过 span 边写入」，
    而此时 5 个节点明明已经写进去了。**比崩溃更糟：它不报错。**
    """
    if not isinstance(item, dict) or item.get('@type') != 'g:Map':
        return {}
    flat = item.get('@value') or []
    out = {}
    # 键值交替，成对取
    for i in range(0, len(flat) - 1, 2):
        key = flat[i]
        val = flat[i + 1]
        if isinstance(val, dict) and val.get('@type') in ('g:List', 'g:Set'):
            val = val.get('@value') or []
        out[str(key)] = val
    return out


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
        d = _gmap(item)
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
    # 方法 1（SSM 声明边）与方法 3（runtime 活性）都在控制面之后采：
    # 前者要用 runtime 列表校验 ARN 存在，后者要按 runtime 逐个取指标。
    declarations = collect_service_to_runtime_declarations()
    activity = collect_runtime_activity(cp['runtimes'][1])
    node_stats = write_control_plane(cp, gw_targets, round_ts,
                                     declarations=declarations, activity=activity)

    span_status, span_rows = collect_spans(runtime_count=len(cp['runtimes'][1]))
    edge_stats = write_span_edges(span_rows, round_ts)

    # 采集状态逐项上报 —— 「全空」与「全失败」在计数上都是 0，
    # 不把状态带出来就无法区分「还没部署 agent」和「权限丢了」。
    statuses = {k: v[0] for k, v in cp.items()}
    statuses['spans'] = span_status
    # CONTRADICTORY 与 FAILED 同等对待：两者都意味着本轮结果不可信。
    # 区别只在诊断信息 —— FAILED 是调用没成功，CONTRADICTORY 是调用成功但结果自相矛盾。
    failed = [k for k, v in statuses.items()
              if v in (_probe_status.FAILED, _probe_status.CONTRADICTORY)]

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
