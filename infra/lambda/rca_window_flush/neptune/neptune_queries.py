"""
neptune_queries.py - RCA 核心图谱查询（Q1/Q2/Q3）
"""
from neptune import neptune_client as nc


def _dependency_edge_labels() -> list:
    """
    契约里标 dependency: true 的边类型。

    ## 为什么这份副本也需要它（2026-09-06）

    本文件在仓库里有两份：`rca/neptune/` 与本处（打进 gp-window-flush Lambda）。
    q1/q3 原先硬编码 `Calls|DependsOn`，只覆盖契约 7 类依赖边中的 2 类，
    漏掉的 `AccessesData`（58 条，服务 → RDS/DynamoDB/S3/SSM/StepFunctions）
    正是真实根因最常在的一层。两份都得改，否则线上 Lambda 与本地行为不一致。

    ## 两个来源

    `rca/neptune/` 能按相对路径读到 `profiles/graph_contract.yaml`；
    本副本的三级父目录里没有 `profiles/`，所以优先用 `neptune-client-base` 层里的
    `graph_contract.EDGE_TYPES`（契约已烧成 Python 模块）。
    顺序 = 层模块 → YAML 文件 → 兜底。
    """
    try:
        from graph_contract import EDGE_TYPES  # type: ignore
        labels = [k for k, v in EDGE_TYPES.items()
                  if isinstance(v, dict) and v.get("dependency")]
        if labels:
            return sorted(labels)
    except Exception:  # noqa: BLE001
        pass
    try:
        import os
        import yaml
        _root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with open(os.path.join(_root, "profiles", "graph_contract.yaml"), encoding="utf-8") as fh:
            gc = yaml.safe_load(fh) or {}
        labels = [
            k for k, v in (gc.get("edge_types") or {}).items()
            if isinstance(v, dict) and v.get("dependency")
        ]
        if labels:
            return sorted(labels)
    except Exception:  # noqa: BLE001
        pass
    # 2026-09-07 同步：新增 RoutesToRuntime / RoutesVia（agent 层的网关路由边）。
    # 这份兜底只在「层模块与 YAML 都读不到」时生效，漂移由 test_52 的门禁钉住 ——
    # 上一次漂移的代价是 Lambda 里漏掉 16 条 Invokes 边。
    return ["AccessesData", "Calls", "Delegates", "DependsOn", "Invokes",
            "InvokesTool", "PublishesTo", "Retrieves", "RoutesToRuntime",
            "RoutesVia"]


def q1_blast_radius(failed_node: str, kind: str = None) -> dict:
    """
    Q1: 影响面评估 —— **谁依赖 failed_node**，它挂了谁跟着挂。

    返回 {'services': [...], 'capabilities': [...]}

    ## 方向（2026-09-06 修）

    契约里依赖边一律**从依赖方指向被依赖方**（`AgentTool -[DependsOn]-> Lambda`、
    `BusinessCapability -[DependsOn]-> RDSCluster`、
    `LambdaFunction -[AccessesData]-> DynamoDBTable`）。因此：

        影响面   = **入边**：(u)-[dep]->(n)   谁依赖 n → n 挂了 u 受影响
        根因候选 = **出边**：(n)-[dep]->(d)   n 依赖谁 → d 挂了 n 才会挂（见 q3）

    原实现的 `services` 部分走的是**出边**，却叫「受影响的下游服务」——
    方向反了。同一个函数里 `capabilities` 部分走的是入边，两半互相矛盾。

    实测证据是一对对称的结果：图里唯一一条 live 服务间边是 `petsite → petsearch`，
    改之前 `q1('petsite')` 返回 `petsearch`（标为 petsite 的影响面），
    `q3('petsearch')` 返回 `petsite`（标为 petsearch 的根因候选）。
    两种读法不可能同时成立，而且都是错的 —— petsite 挂了 petsearch 不受影响
    （petsearch 不依赖 petsite）；petsearch 挂了 petsite 才是受害者而非根因。

    调用方本来就按名字的语义在用（`core/topology_correlator.py` 写明
    「若告警 A 的服务出现在告警 B 的 blast_radius 中，则 A 可能是 B 的根因」，
    这要求 blast_radius 是入边），所以修遍历方向即可，不需要改名或改任何调用点。

    ## 边类型：从契约派生，不硬编码（2026-09-06 修）

    原实现只遍历 `Calls|DependsOn` —— 契约有 7 种 `dependency: true` 的边，
    它看得见 2 种。实测漏掉的 5 种装着绝大多数真实依赖：
    `AccessesData` 58 条（服务 → RDS / DynamoDB / S3 / SSM / StepFunctions，
    **真实根因基本都在这一层**）、`Invokes` 16、`InvokesTool` 8、
    `Delegates` 3、`Retrieves` 1。改用 `_dependency_edge_labels()` 后，
    `petsite` 的根因候选从 1 个变成 17 个。
    """
    # 受影响的服务（最多 5 跳）
    # dependency_kind 过滤：见 q3_upstream_deps 的 docstring。
    # 'live' = dynamic AND active=true，即「当前仍然存在的依赖」，
    # 影响面分析应该用它 —— 已下线服务的历史调用边不构成现在的影响面。
    if kind == 'live':
        _kind_filter = (" AND all(_r IN _rels WHERE _r.dependency_kind = 'dynamic'"
                        " AND _r.active = true)")
    elif kind:
        _kind_filter = " AND all(_r IN _rels WHERE _r.dependency_kind = $kind)"
    else:
        _kind_filter = ""
    _labels = "|".join(_dependency_edge_labels())
    # 方向是**入边**：谁依赖 n，n 挂了谁受影响。
    svc_cypher = f"""
    MATCH (u)-[_rels:{_labels}*1..5]->(n {{name: $node}})
    WHERE u.name IS NOT NULL{_kind_filter}
    RETURN DISTINCT u.name AS name, labels(u)[0] AS type,
           u.recovery_priority AS priority
    """
    # 受影响的 BusinessCapability：同样是**依赖 n 的** capability。
    #
    # 原先遍历 :Serves —— 但 etl_aws/handler.py 每轮主动 drop 该标签的边
    # （hasLabel('Serves').drop()），实测活图 Serves 边为 0 条，
    # BusinessCapability 只有 DependsOn 出边。故移除 Serves，避免死查询。
    #
    # 原先还有一段 UNION：
    #     MATCH (n)-[:Calls|DependsOn*1..5]->(svc)<-[:DependsOn*1..3]-(bc)
    # 已删除 —— 那查的是「和 n 依赖同一个 svc 的 capability」，即 n 的**兄弟**。
    # n 挂了它们不受影响（它们不依赖 n），把它们算进影响面会虚报。
    bc_cypher = """
    MATCH (bc:BusinessCapability)-[:DependsOn*1..3]->(n {name: $node})
    RETURN DISTINCT bc.name AS name, bc.recovery_priority AS priority
    """
    params = {"node": failed_node}
    if kind:
        params["kind"] = kind
    services = nc.results(svc_cypher, params)
    capabilities = nc.results(bc_cypher, {"node": failed_node})
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

def q3_upstream_deps(failed_service: str, kind: str = None) -> list:
    """
    Q3: 根因候选 —— **failed_service 依赖谁**。它们挂了，failed_service 才会挂。

    Args:
        failed_service: 故障服务名
        kind: 依赖类型过滤
            None       不过滤（向后兼容）
            'static'   只看 AWS 配置 / CFN 模板「声明」的依赖
            'dynamic'  只看「观测到过」的依赖（含已失效的历史观测）
            'live'     只看「当前仍然存在」的依赖 = dynamic AND active=true
                       —— **根因定位应该用这个**

    ## 方向（2026-09-06 修）

    原实现是 `MATCH (upstream)-[r]->(n {name: $svc})`，即**入边** ——
    那查的是「谁调用了故障服务」，也就是**受害者**，不是根因。
    契约里依赖边从依赖方指向被依赖方，所以根因候选在**出边**方向。

    这个反向曾经自己暴露过而没被认出来：本函数的原 docstring 举例说
    `petsite` 的 dynamic 上游里有 `gateway-service` 与 `order-service`，
    并称它们在 causal_weight 里是「无意义条目」，归因于 `active=false` 没过滤。
    那只是第二层原因 —— 第一层是这些服务是**调用 petsite 的**，
    它们在任何过滤下都不该出现在 petsite 的根因候选里。
    （`trafficgenerator → petsite` 更明显：一个压测流量源不可能是 petsite 的根因。）

    `kind='live'` 仍然必要，理由不变：`dependency_kind` 只区分「声明 vs 观测」，
    不区分「观测过 vs 现在还在」。已下线服务的历史边不构成现在的依赖。

    ## ⚠️ 函数名里的 `upstream` 与本仓库 NL 层的术语相反

    NL 层的 few-shot 示例（现由 `profiles/petsite.yaml` 的 `nl_examples`
    驱动，经 `rca/neptune/schema_prompt.build_system_prompt()` 渲染 ——
    本包里那份 `neptune/schema_prompt.py` 是上一代硬编码版本，已于
    2026-09-09 删除，因为 core/ 与 actions/ 对它零引用）教给模型的约定是：

        「下游依赖」  = 出边  (s)-[:Calls|AccessesData|...]->(d)   它依赖谁
        「上游调用者」= 入边  (caller)-[:Calls]->(s)               谁依赖它

    也就是说本函数返回的东西，NL 层叫**下游依赖**，而函数名叫 `upstream`。
    两套读法都存在于工程实践里 —— 依赖流看，你的依赖在你「上游」；
    请求流看，调用你的人在你「上游」。**这种歧义正是本函数方向搞反的根源。**

    没有改名，是因为调用面包括 `mcp/devops-agent-association.json`
    （已部署 agent 的工具白名单）、4 个测试文件与 fixtures，改名影响面大而无
    语义收益。取而代之的是：docstring 与目录 desc 显式写明出边/入边，
    机器消费方由 `tests/test_52_rca_query_direction.py` 钉住。
    **判断方向请只看「出边/入边」，不要看 upstream/downstream 这两个词。**

    ## 边类型：从契约派生，不硬编码（2026-09-06 修）

    见 `q1_blast_radius` 的同名段落。只看 `Calls|DependsOn` 会让根因候选里
    **永远不出现数据库、队列和 Lambda 目标** —— 而那是真实根因最常在的地方。
    """
    _labels = "|".join(_dependency_edge_labels())
    if kind == 'live':
        _f = " WHERE r.dependency_kind = 'dynamic' AND r.active = true"
        params = {"svc": failed_service}
    elif kind:
        _f = " WHERE r.dependency_kind = $kind"
        params = {"svc": failed_service, "kind": kind}
    else:
        _f = ""
        params = {"svc": failed_service}
    cypher = f"""
    MATCH (n {{name: $svc}})-[r:{_labels}]->(dep){_f}
    RETURN dep.name AS name, labels(dep)[0] AS type,
           dep.recovery_priority AS priority,
           type(r) AS edge_type, r.dependency_kind AS dependency_kind,
           r.verify_status AS verify_status
    """
    return nc.results(cypher, params)

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


def q17_incidents_by_resource(resource_name: str, limit: int = 5) -> list:
    """Q17: 查找涉及相同资源的历史 Incident（通过 MentionsResource 边）。

    Args:
        resource_name: 资源节点 name 属性（服务名或 EC2 instance name）
        limit: 返回条数上限

    Returns:
        [{'id':..., 'severity':..., 'root_cause':..., 'resolution':..., 'start_time':...}]
    """
    cypher = """
    MATCH (inc:Incident)-[:MentionsResource]->(r {name: $name})
    WHERE inc.status = 'resolved'
    RETURN inc.id AS id, inc.severity AS severity,
           inc.root_cause AS root_cause, inc.resolution AS resolution,
           inc.start_time AS start_time
    ORDER BY inc.start_time DESC
    LIMIT $limit
    """
    return nc.results(cypher, {"name": resource_name, "limit": limit})


def q18_chaos_history(service_name: str, limit: int = 5) -> list:
    """Q18: 查询服务的混沌实验历史（通过 TestedBy 边）。

    Args:
        service_name: Neptune Microservice 节点 name 属性
        limit: 返回条数上限

    Returns:
        [{'id':..., 'fault_type':..., 'result':..., 'recovery_time':...,
          'degradation':..., 'timestamp':...}]
    """
    cypher = """
    MATCH (svc:Microservice {name: $svc})-[:TestedBy]->(exp:ChaosExperiment)
    RETURN exp.experiment_id AS id, exp.fault_type AS fault_type,
           exp.result AS result, exp.recovery_time_sec AS recovery_time,
           exp.degradation_rate AS degradation, exp.timestamp AS timestamp
    ORDER BY exp.timestamp DESC
    LIMIT $limit
    """
    return nc.results(cypher, {"svc": service_name, "limit": limit})


def q19_topology_changes(service_name: str = None, since_seconds: int = 86400,
                         limit: int = 20) -> list:
    """Q19: 查询拓扑变更事件（依赖出现/消失），可按服务过滤。

    为什么需要它:CloudTrail 记录的是 AWS API 级变更（部署、实例停止、
    RDS 修改、扩缩容），它按构造**看不见**两类对依赖图谱最相关的变化 ——
      · 依赖消失:A 不再调用 B。这不产生任何 AWS API 调用，是「流量缺席」
      · 依赖出现:应用内配置/开关导致 A 开始调 B
    事件由 etl_deepflow 的对账在 active true→false 的**状态转变**时写入。

    Args:
        service_name: 只看与该服务相关的变更（作为 source 或 target）。
                      None 表示全图。
        since_seconds: 只看最近这么多秒内的变更，默认 24h
        limit: 返回条数上限

    Returns:
        [{'ts':..., 'kind':..., 'subject':..., 'source':..., 'target':...,
          'edge_type':..., 'detail':...}]，按时间倒序
    """
    import time as _t
    cutoff = int(_t.time()) - int(since_seconds)
    if service_name:
        cypher = """
        MATCH (c:TopologyChange)
        WHERE c.ts >= $cutoff
          AND (c.source_name = $svc OR c.target_name = $svc)
        RETURN c.ts AS ts, c.kind AS kind, c.subject AS subject,
               c.source_name AS source, c.target_name AS target,
               c.edge_type AS edge_type, c.detail AS detail
        ORDER BY c.ts DESC
        LIMIT $limit
        """
        params = {"cutoff": cutoff, "svc": service_name, "limit": limit}
    else:
        cypher = """
        MATCH (c:TopologyChange)
        WHERE c.ts >= $cutoff
        RETURN c.ts AS ts, c.kind AS kind, c.subject AS subject,
               c.source_name AS source, c.target_name AS target,
               c.edge_type AS edge_type, c.detail AS detail
        ORDER BY c.ts DESC
        LIMIT $limit
        """
        params = {"cutoff": cutoff, "limit": limit}
    return nc.results(cypher, params)


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
