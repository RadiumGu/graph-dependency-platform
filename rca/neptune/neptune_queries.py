"""
neptune_queries.py - RCA 核心图谱查询（Q1/Q2/Q3）
"""
from neptune import neptune_client as nc

def q1_blast_radius(failed_node: str) -> dict:
    """
    Q1: 影响面评估
    给定故障节点，找受影响的下游服务和 BusinessCapability
    返回 {'services': [...], 'capabilities': [...]}
    """
    # 受影响的下游服务（最多5跳）
    svc_cypher = """
    MATCH (n {name: $node})-[:Calls|DependsOn*1..5]->(m)
    WHERE m.name IS NOT NULL
    RETURN DISTINCT m.name AS name, labels(m)[0] AS type,
           m.recovery_priority AS priority
    """
    # 受影响的 BusinessCapability
    bc_cypher = """
    MATCH (bc:BusinessCapability)-[:Serves|DependsOn*1..3]->(n {name: $node})
    RETURN DISTINCT bc.name AS name, bc.recovery_priority AS priority
    UNION
    MATCH (n {name: $node})-[:Calls|DependsOn*1..5]->(svc)
          <-[:Serves|DependsOn*1..3]-(bc:BusinessCapability)
    RETURN DISTINCT bc.name AS name, bc.recovery_priority AS priority
    """
    params = {"node": failed_node}
    services = nc.results(svc_cypher, params)
    capabilities = nc.results(bc_cypher, params)
    return {"services": services, "capabilities": capabilities}

def q2_tier0_status() -> list:
    """
    Q2: 查询所有 Tier0 服务的 fault_boundary 和 AZ 分布
    用于快速恢复路径推断
    """
    cypher = """
    MATCH (m:Microservice)
    WHERE m.recovery_priority = 'Tier0'
    RETURN m.name AS name, m.fault_boundary AS fault_boundary,
           m.az AS az, m.replicas AS replicas
    """
    return nc.results(cypher)

def q3_upstream_deps(failed_service: str) -> list:
    """
    Q3: 上游依赖查询（找根因候选）
    找直接依赖了故障服务的所有节点
    """
    cypher = """
    MATCH (upstream)-[:Calls|DependsOn]->(n {name: $svc})
    RETURN upstream.name AS name, labels(upstream)[0] AS type,
           upstream.recovery_priority AS priority
    """
    return nc.results(cypher, {"svc": failed_service})

def q4_service_info(service_name: str) -> dict:
    """获取单个服务的完整属性"""
    cypher = """
    MATCH (n {name: $name})
    RETURN n.name AS name, labels(n)[0] AS type,
           n.recovery_priority AS priority,
           n.fault_boundary AS fault_boundary,
           n.az AS az, n.replicas AS replicas
    LIMIT 1
    """
    rows = nc.results(cypher, {"name": service_name})
    return rows[0] if rows else {}

def q5_similar_incidents(service_name: str, limit: int = 3) -> list:
    """查找同一服务的历史故障（知识库）"""
    cypher = """
    MATCH (inc:Incident)-[:TriggeredBy]->(n {name: $svc})
    WHERE inc.status = 'resolved'
    RETURN inc.id AS id, inc.severity AS severity,
           inc.root_cause AS root_cause, inc.resolution AS resolution,
           inc.mttr AS mttr
    ORDER BY inc.start_time DESC
    LIMIT $limit
    """
    return nc.results(cypher, {"svc": service_name, "limit": limit})

def q6_pod_status(service_name: str) -> list:
    """
    Q6: 查询 Neptune 中服务关联的 Pod 状态（由 ETL 写入）
    返回 [{'pod_name':..., 'status':..., 'restarts':..., 'reason':...}]
    """
    cypher = """
    MATCH (svc {name: $svc})-[:RunsOn]->(pod:Pod)
    RETURN pod.name AS pod_name, pod.status AS status,
           pod.restarts AS restarts, pod.reason AS reason,
           pod.node AS node
    ORDER BY pod.restarts DESC
    """
    return nc.results(cypher, {"svc": service_name})


def q7_db_connections(service_name: str) -> list:
    """
    Q7: 查询服务关联的 Database 节点状态（由 ETL 写入）
    返回 [{'db_name':..., 'status':..., 'connections':..., 'cpu_pct':...}]
    """
    cypher = """
    MATCH (svc {name: $svc})-[:ConnectsTo]->(db:Database)
    RETURN db.name AS db_name, db.cluster_id AS cluster_id,
           db.status AS status, db.connections AS connections,
           db.cpu_pct AS cpu_pct, db.engine AS engine
    """
    return nc.results(cypher, {"svc": service_name})


def q9_service_infra_path(service_name: str) -> list:
    """
    Q9: 多层图遍历 — Service → Pod → EC2Instance
    利用已有的 RunsOn 边，一次查询拿到完整基础设施链路。
    返回 [{'pod_name':..., 'pod_status':..., 'ec2_name':..., 'ec2_id':..., 'ec2_state':..., 'az':...}]
    """
    cypher = """
    MATCH (svc {name: $svc})-[:RunsOn]->(pod:Pod)-[:RunsOn]->(ec2:EC2Instance)
    OPTIONAL MATCH (ec2)-[:LocatedIn]->(az:AvailabilityZone)
    RETURN pod.name AS pod_name, pod.status AS pod_status,
           pod.node_name AS node_name,
           ec2.name AS ec2_name, ec2.instance_id AS ec2_id,
           ec2.state AS ec2_state, ec2.health_status AS ec2_health,
           az.name AS az
    """
    return nc.results(cypher, {"svc": service_name})


def q10_infra_root_cause(affected_service: str) -> dict:
    """
    Q10: 基础设施层根因探测 — 查所有非 running 的 EC2 节点（EKS 或非 EKS），
    再反向通过图遍历找受影响的所有服务。

    不依赖 BelongsTo→EKSCluster（因为 ASG 会在 EC2 停止后踢出，导致边丢失），
    而是直接查 EC2Instance label 中 state != 'running' 的节点。

    返回 {
        'unhealthy_ec2': [{'ec2_id':..., 'state':..., 'az':..., 'affected_pods': [...], 'affected_services': [...]}],
        'az_impact': {az: {'total_pods': N, 'affected_pods': N}},
        'has_infra_fault': bool
    }
    """
    # 1) 所有非 running 的 EC2 节点，反向找 Pod 和 Service
    cypher_unhealthy = """
    MATCH (ec2:EC2Instance)
    WHERE ec2.state IS NOT NULL AND ec2.state <> 'running'
    OPTIONAL MATCH (ec2)-[:LocatedIn]->(az:AvailabilityZone)
    OPTIONAL MATCH (pod:Pod)-[:RunsOn]->(ec2)
    OPTIONAL MATCH (svc:Microservice)-[:RunsOn]->(pod)
    RETURN ec2.instance_id AS ec2_id, ec2.name AS ec2_name,
           ec2.state AS state, az.name AS az,
           collect(DISTINCT pod.name) AS affected_pods,
           collect(DISTINCT svc.name) AS affected_services
    """
    unhealthy_rows = nc.results(cypher_unhealthy)

    # 2) AZ 维度：同一 AZ 下所有 Pod 数 vs 受影响 Pod 数
    cypher_az = """
    MATCH (svc {name: $svc})-[:RunsOn]->(pod:Pod)-[:LocatedIn]->(az:AvailabilityZone)
    RETURN az.name AS az, count(pod) AS total_pods
    """
    az_rows = nc.results(cypher_az, {"svc": affected_service})
    az_total = {r['az']: r['total_pods'] for r in az_rows if r.get('az')}

    # 受影响 AZ 的 Pod 数
    az_affected = {}
    for row in unhealthy_rows:
        az = row.get('az', '')
        if az:
            az_affected[az] = az_affected.get(az, 0) + len(row.get('affected_pods', []))

    az_impact = {}
    for az in set(list(az_total.keys()) + list(az_affected.keys())):
        az_impact[az] = {
            'total_pods': az_total.get(az, 0),
            'affected_pods': az_affected.get(az, 0),
        }

    return {
        'unhealthy_ec2': unhealthy_rows,
        'az_impact': az_impact,
        'has_infra_fault': len(unhealthy_rows) > 0,
    }


def q11_broader_impact(ec2_ids: list) -> list:
    """
    Q11: 给定故障 EC2 节点，反向查所有受影响的服务（不限于 affected_service）。
    发现 blast radius 比 affected_service 更大的情况。

    返回 [{'service':..., 'pod':..., 'ec2_id':...}]
    """
    if not ec2_ids:
        return []
    cypher = """
    MATCH (svc:Microservice)-[:RunsOn]->(pod:Pod)-[:RunsOn]->(ec2:EC2Instance)
    WHERE ec2.instance_id IN $ids
    RETURN DISTINCT svc.name AS service, pod.name AS pod, ec2.instance_id AS ec2_id
    """
    return nc.results(cypher, {"ids": ec2_ids})


def q8_log_source(service_name: str) -> str:
    """
    Q8: 查询节点的 log_source 属性
    优先查 Microservice，再查关联的 EC2Instance、RDSCluster
    返回 log_source 字符串（可能为空）
    """
    cypher = """
    MATCH (n {name: $svc})
    WHERE n.log_source IS NOT NULL AND n.log_source <> ''
    RETURN n.log_source AS log_source, labels(n)[0] AS node_type
    LIMIT 1
    """
    rows = nc.results(cypher, {"svc": service_name})
    if rows:
        return rows[0].get('log_source', '')
    return ''
