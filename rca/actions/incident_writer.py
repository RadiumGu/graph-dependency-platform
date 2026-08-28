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

# ── 因果先验（prior_root_cause_*）的调参 ────────────────────────────────────
# 半衰期:年龄 t 天的历史事件权重 = 0.5^(t / HALF_LIFE)。
# 30 天意味着上月的故障算半份、三个月前算 1/8 —— 既保留趋势又不让陈旧共现
# 永久压制新证据。原实现取全历史计数，旧证据与新证据同权、永不衰减。
CAUSAL_HALF_LIFE_DAYS = float(os.environ.get('CAUSAL_HALF_LIFE_DAYS', '30'))

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
    """更新 Calls 边上的因果权重属性。

    ## 2026-08-28 重写。原实现有四个问题，其中最严重的是语义错位

    ### 1. 语义与名字不符（最严重）

    原 docstring 写「B **同时出现在同一 Incident** 的次数」——共现语义，
    字段也叫 `co_occurrence`。但 co_count 查的是 `Involves` 边，而
    `Involves` 在 write_incident 里**只在 root_cause != affected_service 时
    为根因服务写一条**（本文件 :141-150）。

    所以它实际测量的是「该上游**曾被判定为根因**的次数」，不是共现。
    这也解释了稀疏度：全图 141 个 Incident 只有 8 条 Involves 边。

    结论：**保留数据语义、改正名字与文档**。「曾是根因的频率」对 RCA 是
    比共现更强的先验（共现只是相关，曾是根因带因果判定），
    所以该修的是名字不是数据。字段改为 prior_root_cause_rate /
    prior_root_cause_count，旧字段保留一轮以免破坏既有读取方。

    ### 2. 无时间衰减

    原 total 取该服务**全历史** Incident 计数，旧共现与新共现同权、永不衰减 ——
    一个曾频繁致故但已修复的依赖会永远保持高权重。
    改为指数衰减：年龄 t 天的事件权重 = 0.5^(t / HALF_LIFE_DAYS)。

    ### 3. 上游集合未过滤已下线服务

    原 `MATCH (upstream)-[e:Calls]->(n)` 无 active 过滤，于是给已缩容到零的
    服务也算权重 —— 生产日志里的 `causal_weight: gateway-service→petsite = 0.0`
    就是这么来的（该服务所属命名空间 6 个 Deployment 全为 0 副本）。
    改为只看 active=true 的边（cycle-6 引入的 live 语义）。

    ### 4. 基线率混杂

    P(A 是根因 | B 故障) 忽略了 A 的整体根因率 —— 一个在所有故障里都被判为
    根因的服务会在每条边上都拿到高权重，却不含任何针对性信息。
    改为 lift = P(A|B) / P(A)，lift > 1 才表示「A 对 B 有特异性」。
    """
    from neptune import neptune_client as nc

    # 只看仍然活跃的上游边 —— 已下线服务的历史调用不能用来解释现在的故障
    upstream_edges = nc.results("""
    MATCH (upstream:Microservice)-[e:Calls]->(n:Microservice {name: $svc})
    WHERE e.active = true OR e.active IS NULL
    RETURN upstream.name AS upstream_name
    """, {'svc': affected_service})

    if not upstream_edges:
        logger.info(
            f"causal_weight: {affected_service} 无活跃上游边，跳过"
        )
        return

    now = time.time()

    def _decayed(rows: list, key: str = 'st') -> float:
        """按 start_time 做指数衰减求和。解析不了的当作最老（权重最小）。"""
        total = 0.0
        for r in rows:
            st = r.get(key)
            try:
                t = time.mktime(time.strptime(str(st)[:19], '%Y-%m-%dT%H:%M:%S'))
                age_days = max(0.0, (now - t) / 86400.0)
            except (ValueError, TypeError):
                age_days = CAUSAL_HALF_LIFE_DAYS * 4  # 无法定年 → 权重压到 1/16
            total += 0.5 ** (age_days / CAUSAL_HALF_LIFE_DAYS)
        return total

    # 分母：该服务的 Incident（衰减后）
    svc_incidents = nc.results("""
    MATCH (i:Incident {affected_service: $svc})
    RETURN i.start_time AS st
    """, {'svc': affected_service})
    denom = _decayed(svc_incidents)
    if denom <= 0:
        return

    # 基线：全图 Incident 总量（衰减后），用于算 lift
    all_incidents = nc.results("""
    MATCH (i:Incident) RETURN i.start_time AS st
    """, {})
    all_denom = _decayed(all_incidents)

    for row in upstream_edges:
        upstream_name = row.get('upstream_name')
        if not upstream_name:
            continue

        co_rows = nc.results("""
        MATCH (i:Incident {affected_service: $svc})-[:Involves]->(u {name: $upstream})
        RETURN i.start_time AS st
        """, {'svc': affected_service, 'upstream': upstream_name})
        numer = _decayed(co_rows)

        # P(A 是根因 | B 故障)
        p_cond = numer / denom if denom > 0 else 0.0

        # 基线 P(A 是根因 | 任意故障)，用于 lift 校正
        base_rows = nc.results("""
        MATCH (i:Incident)-[:Involves]->(u {name: $upstream})
        RETURN i.start_time AS st
        """, {'upstream': upstream_name})
        p_base = (_decayed(base_rows) / all_denom) if all_denom > 0 else 0.0

        # lift > 1 表示 A 对 B 有特异性；p_base 为 0 时 lift 无定义，记 None
        lift = round(p_cond / p_base, 3) if p_base > 0 else None

        try:
            nc.results("""
            MATCH (upstream:Microservice {name: $upstream})-[e:Calls]->(n:Microservice {name: $svc})
            SET e.prior_root_cause_rate = $rate,
                e.prior_root_cause_count = $numer,
                e.prior_sample_weight = $denom,
                e.prior_lift = $lift,
                e.prior_half_life_days = $hl,
                e.causal_weight = $rate,
                e.co_occurrence = $numer,
                e.sample_count = $denom,
                e.updated_at = $ts
            """, {
                'upstream': upstream_name,
                'svc': affected_service,
                'rate': round(p_cond, 3),
                'numer': round(numer, 3),
                'denom': round(denom, 3),
                'lift': lift if lift is not None else -1.0,
                'hl': CAUSAL_HALF_LIFE_DAYS,
                'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
            })
            logger.info(
                f"prior_root_cause: {upstream_name}→{affected_service} "
                f"rate={p_cond:.3f} ({numer:.2f}/{denom:.2f}, 半衰期 "
                f"{CAUSAL_HALF_LIFE_DAYS}d) lift="
                f"{lift if lift is not None else 'n/a'}"
            )
        except Exception as e:
            logger.warning(
                f"Failed to set prior_root_cause {upstream_name}→{affected_service}: {e}"
            )


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
