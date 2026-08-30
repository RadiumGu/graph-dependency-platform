"""
neptune_etl_cfn.py - CloudFormation 模板 → Neptune DependsOn 边

Lambda 3: neptune-etl-from-cfn
触发方式:
  1. EventBridge: CloudFormation StackEvent（UPDATE_COMPLETE/CREATE_COMPLETE）
  2. EventBridge: 每日 2:00 AM CST（UTC 18:00）

功能:
  1. 获取 CFN 模板（GetTemplate）
  2. 解析三类跨服务声明依赖：
     - Lambda env Ref → DynamoDB/SQS
     - StepFunction DefinitionString GetAtt → Lambda
     - ALB ListenerRule Ref → TargetGroup
  3. 将 logical_id 解析为 physical_id（ListStackResources）
  4. 在 Neptune 中 upsert 边（declared_in="cfn", stack_name=X）
"""

import os
import json
import time
import logging
import boto3
from typing import Optional

from neptune_client_base import neptune_query, safe_str, extract_value, REGION  # noqa: F401
from graph_contract import (  # 同属 neptune-client-base Layer
    TIMESTAMP_FIELD,
    assert_edge_type,
    assert_node_type,
    is_dependency_edge,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ===== 配置 =====
CFN_STACK_NAMES = os.environ.get('CFN_STACK_NAMES', 'ServicesEks2,Applications').split(',')

# 只处理这些类型之间的语义依赖（过滤部署顺序约束）
SEMANTIC_TYPES = {
    'AWS::Lambda::Function',
    'AWS::StepFunctions::StateMachine',
    'AWS::DynamoDB::Table',
    'AWS::SQS::Queue',
    'AWS::ElasticLoadBalancingV2::LoadBalancer',
    'AWS::ElasticLoadBalancingV2::TargetGroup',
    'AWS::ElasticLoadBalancingV2::Listener',
    'AWS::ElasticLoadBalancingV2::ListenerRule',
    'AWS::ApiGateway::RestApi',
    'AWS::SNS::Topic',
    'AWS::Kinesis::Stream',
}

# Neptune 标签（哪些资源类型对应图中哪个 label）
TYPE_TO_LABEL = {
    'AWS::Lambda::Function': 'LambdaFunction',
    'AWS::StepFunctions::StateMachine': 'StepFunction',
    'AWS::DynamoDB::Table': 'DynamoDBTable',
    'AWS::SQS::Queue': 'SQSQueue',
    'AWS::ElasticLoadBalancingV2::LoadBalancer': 'LoadBalancer',
    # TargetGroup 不单独建节点（是 ALB 内部路由细节，不是业务拓扑节点）
    # 'AWS::ElasticLoadBalancingV2::TargetGroup': 'Microservice',  # 已移除：TG ≠ Microservice
    # 下面两条本账号目前零实例，故刻意**不**写进 profiles/petsite.yaml 的 schema：
    # schema 会喂给 LLM 做自然语言查询，声明零实例类型等于邀请它推理不存在的东西。
    # 映射本身保留 —— 若资源将来出现，走 fallback split('::')[-1] 会得到
    # 'RestApi'/'Stream'，比现在这两个名字更差。真出现时须同步补 schema。
    'AWS::ApiGateway::RestApi': 'APIGateway',
    'AWS::SNS::Topic': 'SNSTopic',
    'AWS::Kinesis::Stream': 'KinesisStream',
}

# ===== Neptune 工具函数 =====

_vid_cache = {}  # (label, name) → vertex_id

def get_or_create_vertex(label: str, physical_id: str, stack_name: str):
    """mergeV upsert 顶点，返回 vertex ID，结果写入 _vid_cache

    基数语义（2026-08-29 修复）：
    mergeV 的 option-map **用 Gremlin 默认 SET 基数**，值会追加而不替换。
    `last_scanned` 是每轮变化的时间戳，放在 onMatch map 里会导致每次运行
    在同一节点上新增一个 distinct 值 —— 实测已累积到单节点 172 个值，
    使 `MATCH (n:LambdaFunction) WHERE n.last_scanned IS NOT NULL` 从
    9 个真节点扇出成 1,253 行（139 倍）。

    因此 option-map 只保留**身份与仅创建时写一次**的字段，
    所有需要每轮刷新的标量改用尾部 `.property(single, k, v)` 链重写。
    这与 etl_aws/neptune_client.py:upsert_vertex 的兜底模式一致。

    注意：顶点属性才有基数问题；**边属性在 Neptune/TinkerPop 里天生单值**，
    所以 upsert_cfn_edge 里不带 single 的 `.property()` 是安全的，
    但不要把边的写法照搬到顶点。
    """
    key = (label, physical_id)
    if key in _vid_cache:
        return _vid_cache[key]
    pid = safe_str(physical_id)
    lb = safe_str(label)
    sn = safe_str(stack_name)
    ts = int(time.time())

    # 契约门禁：未在 profiles/graph_contract.yaml 声明的类型不得写入。
    assert_node_type(lb)

    gremlin = (
        f"g.mergeV([(T.label): '{lb}', 'name': '{pid}'])"
        f".option(Merge.onCreate, [(T.label): '{lb}', 'name': '{pid}', "
        f"'created_at': {ts}])"
        # 每轮刷新的标量一律 single 基数，避免 SET 追加
        f".property(single,'stack_name','{sn}')"
        f".property(single,'source','cfn-etl')"
        f".property(single,'last_scanned',{ts})"
        # 统一时间戳字段（契约 timestamp_field）。last_scanned 保留是过渡态 ——
        # 跨源查询「这条记录最后何时被看到」此前无统一字段可用：
        # aws 写 last_updated、cfn 写 last_scanned、deepflow/xray 写 last_seen。
        f".property(single,'{TIMESTAMP_FIELD}',{ts})"
        f".id()"
    )
    result = neptune_query(gremlin)
    ids = result.get('result', {}).get('data', {}).get('@value', [])
    if ids:
        vid = ids[0]
        vid = extract_value(vid) if isinstance(vid, dict) else vid
        _vid_cache[key] = vid
        return vid
    return None

def upsert_cfn_edge(src_vid, dst_vid, rel_type: str, stack_name: str, evidence: str):
    """upsert 声明依赖边（declared_in=cfn）
    
    Uses Gremlin coalesce pattern to avoid mergeE vertex ID escaping issues with ARNs.
    """
    if not src_vid or not dst_vid:
        return False
    ts = int(time.time())
    sn = safe_str(stack_name)
    ev = safe_str(evidence)
    rt = safe_str(rel_type)

    # 契约门禁：未声明的边类型不得写入。
    assert_edge_type(rt)

    # ── 溯源写一次（write-once）──────────────────────────────────────────
    # 原实现是无条件 `.property('source','cfn-etl')` —— coalesce 命中一条**已存在**
    # 的边之后照写，会把 deepflow / xray 先写的 source 静默改成 cfn-etl，
    # 等于抹掉「谁首先发现了这条依赖」。而 xray / deepflow-L4 / NFM 三个源本来就
    # 刻意保护这些属性，只有 aws 与 cfn 两处覆盖 —— 行为自相矛盾。
    # 改法同 etl_aws：coalesce(values(k), constant(v)) —— 已有值保留，没有才写。
    #
    # dependency 判定改由契约提供，不再本地硬编码 ('Calls','DependsOn','AccessesData')。
    # 那份常量原先在三个 ETL 里各存一份，作者已在 etl_aws 的注释里标为待收敛项。
    write_once = {'source': 'cfn-etl'}
    if is_dependency_edge(rt):
        # dependency_kind='static'：CFN 模板「声明」的依赖，未必被实际调用过。
        write_once['dependency_kind'] = 'static'
    once_chain = ''.join(
        f".property('{k}', __.coalesce(__.values('{k}'), __.constant('{safe_str(v)}')))"
        for k, v in write_once.items())

    # Use V(id) lookups to get vertex refs, then coalesce to find or create edge
    gremlin = (
        f"g.V('{src_vid}').as('s').V('{dst_vid}').as('d')"
        f".select('s')"
        f".coalesce("
        f"  __.outE('{rt}').where(__.inV().hasId('{dst_vid}')),"
        f"  __.addE('{rt}').to(__.V('{dst_vid}'))"
        f")"
        # declared_in 是 cfn 独有的证据字段，保留不动，避免破坏既有查询
        f".property('declared_in', 'cfn')"
        + once_chain
        + f".property('stack_name', '{sn}')"
        f".property('evidence', '{ev}')"
        f".property('last_scanned', {ts})"
        f".property('{TIMESTAMP_FIELD}', {ts})"
    )
    neptune_query(gremlin)
    return True

# ===== CFN 模板分析 =====

def get_cfn_template(cfn_client, stack_name: str) -> dict:
    """获取 CloudFormation 模板"""
    try:
        resp = cfn_client.get_template(StackName=stack_name, TemplateStage='Original')
        tb = resp['TemplateBody']
        if isinstance(tb, str):
            try:
                tb = json.loads(tb)
            except json.JSONDecodeError:
                import yaml
                tb = yaml.safe_load(tb)
        return tb
    except Exception as e:
        logger.error(f"get_template for {stack_name} failed: {e}")
        return {}

def get_physical_id_map(cfn_client, stack_name: str) -> dict:
    """获取 logical_id → {physical_id, type} 映射"""
    mapping = {}
    try:
        paginator = cfn_client.get_paginator('list_stack_resources')
        for page in paginator.paginate(StackName=stack_name):
            for res in page['StackResourceSummaries']:
                mapping[res['LogicalResourceId']] = {
                    'physical_id': res.get('PhysicalResourceId', ''),
                    'type': res.get('ResourceType', ''),
                }
    except Exception as e:
        logger.error(f"list_stack_resources for {stack_name} failed: {e}")
    return mapping

def extract_ref_or_getatt(val) -> Optional[str]:
    """从 CFN 值中提取 Ref 或 Fn::GetAtt 的 logical_id"""
    if not isinstance(val, dict):
        return None
    if 'Ref' in val:
        return val['Ref']
    if 'Fn::GetAtt' in val:
        ref = val['Fn::GetAtt']
        if isinstance(ref, list):
            return ref[0]
        return str(ref)
    # Fn::Sub 可能包含 ${LogicalId}，简单提取第一个引用
    if 'Fn::Sub' in val:
        sub_val = val['Fn::Sub']
        if isinstance(sub_val, str):
            import re
            matches = re.findall(r'\$\{([^.}]+)', sub_val)
            if matches:
                return matches[0]
        elif isinstance(sub_val, list) and len(sub_val) > 1:
            # [template_string, substitution_map]
            for k in sub_val[1].values():
                result = extract_ref_or_getatt(k)
                if result:
                    return result
    return None

def deep_find_lambda_refs(obj, lambda_logicals: set) -> set:
    """递归扫描对象，找到所有对 Lambda 的引用"""
    found = set()
    if isinstance(obj, dict):
        logical = extract_ref_or_getatt(obj)
        if logical and logical in lambda_logicals:
            found.add(logical)
        for v in obj.values():
            found.update(deep_find_lambda_refs(v, lambda_logicals))
    elif isinstance(obj, list):
        for item in obj:
            found.update(deep_find_lambda_refs(item, lambda_logicals))
    elif isinstance(obj, str):
        # 处理内嵌 ARN 字符串（如 "arn:aws:lambda:..."）
        pass
    return found

def extract_declared_deps(template: dict, physical_map: dict) -> list:
    """
    从 CFN 模板提取语义依赖关系
    返回: [{'src_physical', 'src_type', 'dst_physical', 'dst_type', 'rel_type', 'evidence'}]
    """
    resources = template.get('Resources', {})
    deps = []

    # 按类型预建索引
    lambda_logicals = {
        lid for lid, r in resources.items()
        if r.get('Type') == 'AWS::Lambda::Function'
    }
    ddb_logicals = {
        lid for lid, r in resources.items()
        if r.get('Type') == 'AWS::DynamoDB::Table'
    }
    sqs_logicals = {
        lid for lid, r in resources.items()
        if r.get('Type') == 'AWS::SQS::Queue'
    }
    tg_logicals = {
        lid for lid, r in resources.items()
        if r.get('Type') == 'AWS::ElasticLoadBalancingV2::TargetGroup'
    }

    for logical_id, resource in resources.items():
        rtype = resource.get('Type', '')
        props = resource.get('Properties', {})
        phys = physical_map.get(logical_id, {})
        src_physical = phys.get('physical_id', logical_id)

        if not src_physical:
            continue

        # ── Lambda → DynamoDB/SQS（通过 env var Ref/GetAtt）──
        if rtype == 'AWS::Lambda::Function':
            env_vars = props.get('Environment', {}).get('Variables', {})
            for var_name, var_val in env_vars.items():
                target_logical = extract_ref_or_getatt(var_val)
                if target_logical:
                    dst_phys = physical_map.get(target_logical, {})
                    dst_type = dst_phys.get('type', '')
                    if dst_type in SEMANTIC_TYPES and dst_phys.get('physical_id'):
                        deps.append({
                            'src_physical': src_physical,
                            'src_type': rtype,
                            'dst_physical': dst_phys['physical_id'],
                            'dst_type': dst_type,
                            'rel_type': 'AccessesData',
                            'evidence': f'env:{var_name}',
                        })

            # Lambda DependsOn 其他语义资源（显式 DependsOn）
            depends_on = resource.get('DependsOn', [])
            if isinstance(depends_on, str):
                depends_on = [depends_on]
            for dep_logical in depends_on:
                dst_phys = physical_map.get(dep_logical, {})
                dst_type = dst_phys.get('type', '')
                if dst_type in SEMANTIC_TYPES and dst_phys.get('physical_id'):
                    deps.append({
                        'src_physical': src_physical,
                        'src_type': rtype,
                        'dst_physical': dst_phys['physical_id'],
                        'dst_type': dst_type,
                        'rel_type': 'DependsOn',
                        'evidence': 'DependsOn',
                    })

        # ── StepFunction → Lambda（DefinitionString GetAtt/Ref）──
        if rtype == 'AWS::StepFunctions::StateMachine':
            defn_prop = props.get('DefinitionString', props.get('Definition', {}))
            found_lambdas = deep_find_lambda_refs(defn_prop, lambda_logicals)
            for lambda_logical in found_lambdas:
                dst_phys = physical_map.get(lambda_logical, {})
                if dst_phys.get('physical_id'):
                    deps.append({
                        'src_physical': src_physical,
                        'src_type': rtype,
                        'dst_physical': dst_phys['physical_id'],
                        'dst_type': 'AWS::Lambda::Function',
                        'rel_type': 'Invokes',
                        'evidence': 'sfn:definition:lambda-ref',
                    })

        # ── ALB ListenerRule → TargetGroup ──
        # TargetGroup 是 ALB 内部路由细节，不作为独立节点纳入拓扑图，跳过此边
        # 如需追踪 ListenerRule→微服务 的关系，需要从 EKS TargetGroupBinding 推导（待实现）
        if rtype == 'AWS::ElasticLoadBalancingV2::ListenerRule':
            pass  # skip TargetGroup edges

    return deps

def write_deps_to_neptune(deps: list, stack_name: str) -> int:
    """将提取的声明依赖写入 Neptune，返回写入边数"""
    count = 0
    for dep in deps:
        src_type = dep['src_type']
        dst_type = dep['dst_type']
        src_label = TYPE_TO_LABEL.get(src_type, src_type.split('::')[-1])
        dst_label = TYPE_TO_LABEL.get(dst_type, dst_type.split('::')[-1])

        def normalize_name(rtype: str, physical_id: str) -> str:
            """将 CFN 物理 ID 转换为与 etl_aws 一致的节点名字
            StepFunction ARN → 状态机短名（和 etl_aws list_state_machines 返回值一致）
            其他类型（Lambda 等）物理 ID 本身就是节点名，直接返回
            """
            if rtype == 'AWS::StepFunctions::StateMachine' and ':stateMachine:' in physical_id:
                return physical_id.split(':stateMachine:')[-1]
            return physical_id

        src_name = normalize_name(src_type, dep['src_physical'])
        dst_name = normalize_name(dst_type, dep['dst_physical'])

        try:
            # get-or-create 顶点（使用 normalize 后的短名，与 etl_aws 节点自然合并）
            src_vid = get_or_create_vertex(src_label, src_name, stack_name)
            dst_vid = get_or_create_vertex(dst_label, dst_name, stack_name)

            # upsert 边
            if upsert_cfn_edge(src_vid, dst_vid, dep['rel_type'], stack_name, dep['evidence']):
                count += 1
                logger.debug(
                    f"  [{stack_name}] {src_name} -[{dep['rel_type']}]-> "
                    f"{dst_name} (evidence={dep['evidence']})"
                )
        except Exception as e:
            logger.error(f"Failed to write dep {dep['src_physical']}->{dep['dst_physical']}: {e}")

    return count

# ===== 主 ETL 逻辑 =====

def run_etl(stack_names: list = None):
    """运行 CFN ETL"""
    if stack_names is None:
        stack_names = CFN_STACK_NAMES

    logger.info(f"=== neptune-etl-from-cfn 开始, stacks={stack_names} ===")
    cfn_client = boto3.client('cloudformation', region_name=REGION)
    total_deps = 0

    for stack_name in stack_names:
        stack_name = stack_name.strip()
        if not stack_name:
            continue
        logger.info(f"处理 CloudFormation 模板: {stack_name}")
        try:
            template = get_cfn_template(cfn_client, stack_name)
            if not template:
                logger.warning(f"Empty template for {stack_name}, skipping")
                continue
            physical_map = get_physical_id_map(cfn_client, stack_name)
            logger.info(f"  模板资源数: {len(template.get('Resources', {}))}, "
                       f"物理 ID 映射: {len(physical_map)}")
            deps = extract_declared_deps(template, physical_map)
            logger.info(f"  提取声明依赖: {len(deps)} 条")
            written = write_deps_to_neptune(deps, stack_name)
            total_deps += written
            logger.info(f"  [{stack_name}] 写入 {written} 条 DependsOn 边到 Neptune")

            # SNS Subscription → SQS: PublishesTo（sns:protocol=sqs 订阅）
            resources = template.get('Resources', {})
            for logical_id, resource in resources.items():
                if resource.get('Type') != 'AWS::SNS::Subscription':
                    continue
                props_r = resource.get('Properties', {})
                if props_r.get('Protocol', '') != 'sqs':
                    continue
                topic_arn_val = props_r.get('TopicArn', {})
                topic_logical = None
                if isinstance(topic_arn_val, dict):
                    topic_logical = topic_arn_val.get('Ref')
                    if not topic_logical:
                        ga = topic_arn_val.get('Fn::GetAtt', [])
                        if isinstance(ga, list) and ga:
                            topic_logical = ga[0]
                endpoint_val = props_r.get('Endpoint', {})
                queue_logical = None
                if isinstance(endpoint_val, dict):
                    ga = endpoint_val.get('Fn::GetAtt', [])
                    if isinstance(ga, list) and ga:
                        queue_logical = ga[0]
                    elif 'Ref' in endpoint_val:
                        queue_logical = endpoint_val['Ref']
                if not topic_logical or not queue_logical:
                    continue
                topic_phys = physical_map.get(topic_logical, {})
                queue_phys = physical_map.get(queue_logical, {})
                if not topic_phys.get('physical_id') or not queue_phys.get('physical_id'):
                    continue
                t_vid = get_or_create_vertex('SNSTopic', topic_phys['physical_id'], stack_name)
                q_vid = get_or_create_vertex('SQSQueue', queue_phys['physical_id'], stack_name)
                if t_vid and q_vid:
                    upsert_cfn_edge(t_vid, q_vid, 'PublishesTo', stack_name, f'Subscription:{logical_id}')
                    logger.info(f"PublishesTo: {topic_phys['physical_id']} → {queue_phys['physical_id']}")
                    total_deps += 1
        except Exception as e:
            logger.error(f"Stack {stack_name} processing failed: {e}", exc_info=True)

    logger.info(f"=== neptune-etl-from-cfn 完成: total_deps={total_deps} ===")
    return {'total_deps': total_deps}


def handler(event, context):
    """Lambda 入口"""
    logger.info(f"Received event: {json.dumps(event, default=str)[:500]}")

    # 支持 EventBridge CFN stack update 事件
    stack_names = list(CFN_STACK_NAMES)
    if event.get('source') == 'aws.cloudformation':
        detail = event.get('detail', {})
        stack_id = detail.get('stack-id', '')
        if stack_id:
            # 从 stack ARN 中提取 stack name
            # arn:aws:cloudformation:region:account:stack/StackName/uuid
            parts = stack_id.split('/')
            if len(parts) >= 2:
                triggered_stack = parts[1]
                logger.info(f"Triggered by CFN stack event: {triggered_stack}")
                if triggered_stack in stack_names:
                    stack_names = [triggered_stack]
                else:
                    logger.warning(f"Stack {triggered_stack} not in configured list, running all")

    try:
        result = run_etl(stack_names)
        return {"statusCode": 200, "body": result}
    except Exception as e:
        logger.error(f"ETL failed: {e}", exc_info=True)
        raise
