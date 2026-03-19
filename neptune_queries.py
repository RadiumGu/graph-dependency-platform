"""
neptune_queries.py - RCA 核心图谱查询（Q1/Q2/Q3）
"""
from . import neptune_client as nc

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
