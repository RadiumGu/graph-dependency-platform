"""
incident_writer.py - 故障闭环：写 Incident 节点到 Neptune（Phase 3）

变更历史：
  2026-02-28  补 timestamp 字段；write_incident 后调用 _update_causal_weights()
              收集边因果权重（causal_weight = co_occurrence / total）供未来评分使用
  2026-04-01  Phase A: 新增实体提取 + MentionsResource 边写入
"""
import os, re, json, logging, time, uuid
import boto3

logger = logging.getLogger(__name__)
from shared import get_region
REGION = get_region()

# AWS 资源 ID 正则（实体提取用）
_EC2_PATTERN = re.compile(r'\bi-[0-9a-f]{8,17}\b')
_RDS_PATTERN = re.compile(r'\b(?:arn:aws:rds:[^:\s]+:[^:\s]+:(?:cluster|db):)?([a-zA-Z][a-zA-Z0-9-]{2,63})\b')


def _extract_entities(report_text: str) -> list[dict]:
    """从 RCA 报告文本中提取实体引用（服务名 + AWS 资源 ID）。

    Args:
        report_text: RCA 报告全文

    Returns:
        实体列表，每个元素含 type 和 name/id 字段
    """
    from config import CANONICAL
    entities: list[dict] = []
    seen: set[str] = set()

    # 精确匹配：Neptune 服务名（从 CANONICAL 反查）
    all_service_names = set(CANONICAL.values())
    for svc in all_service_names:
        if svc in report_text and svc not in seen:
            entities.append({'type': 'Microservice', 'name': svc})
            seen.add(svc)

    # 正则匹配：EC2 Instance ID
    for ec2_id in _EC2_PATTERN.findall(report_text):
        key = f'ec2:{ec2_id}'
        if key not in seen:
            entities.append({'type': 'EC2Instance', 'id': ec2_id})
            seen.add(key)

    return entities


def _link_entities_to_incident(incident_id: str, entities: list[dict]) -> None:
    """为每个提取到的实体创建 Incident -[:MentionsResource]-> Resource 边。

    Args:
        incident_id: Incident 节点 ID
        entities: _extract_entities() 返回的实体列表
    """
    from neptune import neptune_client as nc

    for ent in entities:
        try:
            if ent['type'] == 'Microservice':
                nc.results("""
                    MATCH (inc:Incident {id: $inc_id})
                    MATCH (svc:Microservice {name: $name})
                    MERGE (inc)-[:MentionsResource]->(svc)
                """, {'inc_id': incident_id, 'name': ent['name']})
            elif ent['type'] == 'EC2Instance':
                nc.results("""
                    MATCH (inc:Incident {id: $inc_id})
                    MATCH (ec2:EC2Instance {instance_id: $id})
                    MERGE (inc)-[:MentionsResource]->(ec2)
                """, {'inc_id': incident_id, 'id': ent['id']})
        except Exception as e:
            logger.warning(f"MentionsResource edge failed for {ent}: {e}")


def write_incident(
    classification: dict,
    rca_result: dict,
    resolution: str = '',
    report_text: str = '',
) -> str:
    """将故障记录写入 Neptune Incident 节点，并更新调用边的因果权重。

    Args:
        classification: 故障分类结果（含 affected_service, severity）
        rca_result: RCA 分析结果（含 top_candidate 等）
        resolution: 解决方案描述
        report_text: RCA 报告全文（用于实体提取，Phase A 新增）

    Returns:
        incident_id
    """
    from neptune import neptune_client as nc

    incident_id = f"inc-{time.strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:6]}"
    svc = classification['affected_service']
    severity = classification['severity']
    top = rca_result.get('top_candidate', {}) if rca_result else {}
    root_cause = top.get('service', 'unknown') if top else 'unknown'
    confidence = top.get('confidence', 0) if top else 0

    now_iso = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    # 写入 Incident 节点（补 timestamp 字段）
    cypher = """
    MERGE (inc:Incident {id: $id})
    ON CREATE SET
        inc.severity = $severity,
        inc.start_time = $start_time,
        inc.timestamp = $timestamp,
        inc.status = 'investigating',
        inc.root_cause = $root_cause,
        inc.root_cause_confidence = $confidence,
        inc.resolution = $resolution,
        inc.affected_service = $svc
    RETURN inc.id AS id
    """
    nc.results(cypher, {
        'id': incident_id,
        'severity': severity,
        'start_time': now_iso,
        'timestamp': now_iso,
        'root_cause': root_cause,
        'confidence': confidence,
        'resolution': resolution,
        'svc': svc,
    })

    # 建立关联边：Incident -[TriggeredBy]-> affected_service
    try:
        nc.results("""
        MATCH (inc:Incident {id: $inc_id})
        MATCH (svc {name: $svc_name})
        MERGE (inc)-[:TriggeredBy]->(svc)
        """, {'inc_id': incident_id, 'svc_name': svc})
    except Exception as e:
        logger.warning(f"Failed to create TriggeredBy edge: {e}")

    # 建立关联边：Incident -[Involves]-> root_cause service（用于 KB 历史相似匹配）
    if root_cause and root_cause != svc and root_cause != 'unknown':
        try:
            nc.results("""
            MATCH (inc:Incident {id: $inc_id})
            MATCH (rc {name: $rc_name})
            MERGE (inc)-[:Involves]->(rc)
            """, {'inc_id': incident_id, 'rc_name': root_cause})
        except Exception as e:
            logger.warning(f"Failed to create Involves edge: {e}")

    # 写入子图特征签名（为未来异常子图匹配采集数据）
    try:
        _write_subgraph_pattern(incident_id, svc, root_cause, rca_result)
    except Exception as e:
        logger.warning(f"subgraph_pattern write failed (non-fatal): {e}")

    # 更新调用边因果权重（失败不影响主流程）
    try:
        _update_causal_weights(svc, root_cause)
    except Exception as e:
        logger.warning(f"causal_weight update failed (non-fatal): {e}")

    # Phase A: 实体提取 + MentionsResource 边写入
    if report_text:
        try:
            entities = _extract_entities(report_text)
            if entities:
                _link_entities_to_incident(incident_id, entities)
                logger.info(f"MentionsResource: {incident_id} → {len(entities)} entities linked")
        except Exception as e:
            logger.warning(f"entity linking failed (non-fatal): {e}")

    # Phase B5: 向量索引（non-fatal）
    if report_text:
        try:
            from search.incident_vectordb import index_incident as vec_index
            vec_index(incident_id, report_text, {
                'severity': severity,
                'affected_service': svc,
                'root_cause': root_cause,
                'timestamp': now_iso,
            })
        except Exception as e:
            logger.warning(f"Vector indexing failed (non-fatal): {e}")

    logger.info(f"Incident written: {incident_id}, root_cause={root_cause}, confidence={confidence}")
    return incident_id



def _write_subgraph_pattern(incident_id: str, affected_service: str,
                             root_cause: str, rca_result: dict):
    """
    记录故障子图特征，为异常子图匹配积累训练数据。

    存储三类信息：
    1. pattern_signature  — 所有出错服务名的有序拼接（快速 Jaccard 相似度计算）
    2. error_services     — 逗号分隔的出错服务列表
    3. propagation_path   — 推断的传播链（root_cause → ... → affected_service）

    注意：当前仅采集，不用于评分。待积累 30+ 真实 Incident 后启用 step3c_subgraph_match()。
    """
    from neptune import neptune_client as nc

    # 从 rca_result 提取出错服务列表
    candidates = rca_result.get('all_candidates', []) if rca_result else []
    error_svc_list = sorted({c.get('service', '') for c in candidates if c.get('service')})
    if affected_service not in error_svc_list:
        error_svc_list.append(affected_service)
    error_svc_list = sorted(set(error_svc_list))

    # pattern_signature: "affected_service|svc1,svc2,svc3"
    pattern_signature = f"{affected_service}|{','.join(error_svc_list)}"

    # 推断传播路径: root_cause → affected_service（简化版）
    if root_cause and root_cause != affected_service and root_cause != 'unknown':
        propagation_path = f"{root_cause}→{affected_service}"
    else:
        propagation_path = affected_service

    try:
        nc.results("""
        MATCH (inc:Incident {id: $id})
        SET inc.pattern_signature = $sig,
            inc.error_services = $error_svcs,
            inc.propagation_path = $path,
            inc.involved_count = $count
        """, {
            'id': incident_id,
            'sig': pattern_signature,
            'error_svcs': ','.join(error_svc_list),
            'path': propagation_path,
            'count': len(error_svc_list),
        })
        logger.info(f"subgraph_pattern written: {incident_id} sig={pattern_signature}")
    except Exception as e:
        logger.warning(f"Failed to write subgraph_pattern: {e}")


def _update_causal_weights(affected_service: str, root_cause: str):
    """
    更新 Calls 边上的因果权重属性。

    因果权重 = 历史上 A 出问题时 B 同时出现在同一 Incident 的次数 / A 出问题的总次数

    注意：当前 Incident 数量较少（<100），权重仅作为数据采集用途，
         尚未纳入 step4_score() 评分，待积累 100+ 真实告警后启用。
    """
    from neptune import neptune_client as nc

    upstream_edges = nc.results("""
    MATCH (upstream:Microservice)-[e:Calls]->(n:Microservice {name: $svc})
    RETURN upstream.name AS upstream_name
    """, {'svc': affected_service})

    if not upstream_edges:
        return

    total_result = nc.results("""
    MATCH (i:Incident {affected_service: $svc})
    RETURN count(i) AS total
    """, {'svc': affected_service})
    total = total_result[0].get('total', 0) if total_result else 0

    if total == 0:
        return

    for row in upstream_edges:
        upstream_name = row.get('upstream_name')
        if not upstream_name:
            continue

        co_result = nc.results("""
        MATCH (i:Incident {affected_service: $svc})-[:Involves]->(u {name: $upstream})
        RETURN count(i) AS co_count
        """, {'svc': affected_service, 'upstream': upstream_name})
        co_count = co_result[0].get('co_count', 0) if co_result else 0

        causal_weight = round(co_count / total, 3)

        try:
            nc.results("""
            MATCH (upstream:Microservice {name: $upstream})-[e:Calls]->(n:Microservice {name: $svc})
            SET e.causal_weight = $weight,
                e.co_occurrence = $co_count,
                e.sample_count = $total,
                e.updated_at = $ts
            """, {
                'upstream': upstream_name,
                'svc': affected_service,
                'weight': causal_weight,
                'co_count': co_count,
                'total': total,
                'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            })
            logger.info(f"causal_weight: {upstream_name}→{affected_service} = {causal_weight} ({co_count}/{total})")
        except Exception as e:
            logger.warning(f"Failed to set causal_weight {upstream_name}→{affected_service}: {e}")


def resolve_incident(incident_id: str, resolution: str, mttr_seconds: int):
    """更新 Incident 节点为 resolved"""
    from neptune import neptune_client as nc
    nc.results("""
    MATCH (inc:Incident {id: $id})
    SET inc.status = 'resolved',
        inc.resolution = $resolution,
        inc.mttr = $mttr,
        inc.end_time = $end_time
    RETURN inc.id
    """, {
        'id': incident_id,
        'resolution': resolution,
        'mttr': mttr_seconds,
        'end_time': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
    })
    logger.info(f"Incident resolved: {incident_id}, mttr={mttr_seconds}s")
