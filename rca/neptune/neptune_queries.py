"""
neptune_queries.py - RCA 核心图谱查询（Q1/Q2/Q3）
"""
from neptune import neptune_client as nc

def q1_blast_radius(failed_node: str, kind: str = None) -> dict:
    """
    Q1: 影响面评估
    给定故障节点，找受影响的下游服务和 BusinessCapability
    返回 {'services': [...], 'capabilities': [...]}
    """
    # 受影响的下游服务（最多5跳）
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
    svc_cypher = f"""
    MATCH (n {{name: $node}})-[_rels:Calls|DependsOn*1..5]->(m)
    WHERE m.name IS NOT NULL{_kind_filter}
    RETURN DISTINCT m.name AS name, labels(m)[0] AS type,
           m.recovery_priority AS priority
    """
    # 受影响的 BusinessCapability
    # 原先遍历 :Serves —— 但 etl_aws/handler.py 每轮主动 drop 该标签的边
    # （hasLabel('Serves').drop()），实测活图 Serves 边为 0 条，
    # BusinessCapability 只有 DependsOn 出边。故移除 Serves，避免死查询。
    bc_cypher = """
    MATCH (bc:BusinessCapability)-[:DependsOn*1..3]->(n {name: $node})
    RETURN DISTINCT bc.name AS name, bc.recovery_priority AS priority
    UNION
    MATCH (n {name: $node})-[:Calls|DependsOn*1..5]->(svc)
          <-[:DependsOn*1..3]-(bc:BusinessCapability)
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
    Q3: 上游依赖查询（找根因候选）
    找直接依赖了故障服务的所有节点

    Args:
        failed_service: 故障服务名
        kind: 依赖类型过滤
            None       不过滤（向后兼容）
            'static'   只看 AWS 配置 / CFN 模板「声明」的依赖
            'dynamic'  只看「观测到过」的依赖（含已失效的历史观测）
            'live'     只看「当前仍然存在」的依赖 = dynamic AND active=true
                       —— **根因定位应该用这个**

    为什么 'dynamic' 不够：dependency_kind 只区分「声明 vs 观测」，
    不区分「观测过 vs 现在还在」。实测例子：petsite 的三个 dynamic 上游里，
    gateway-service 与 order-service 属 awesomeshop 命名空间，
    其 6 个 Deployment 副本数已全为 0（服务下线），边已被对账置 active=false，
    但 dependency_kind 仍是 dynamic。若按 'dynamic' 过滤，
    这两个已下线的服务仍会被当作根因候选 —— 这正是此前
    causal_weight 出现 gateway-service→petsite 这类无意义条目的原因。
    """
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
    MATCH (upstream)-[r:Calls|DependsOn]->(n {{name: $svc}}){_f}
    RETURN upstream.name AS name, labels(upstream)[0] AS type,
           upstream.recovery_priority AS priority
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


def q20_dependency_verification(service_name: str = None,
                                stale_after_seconds: int = 3600,
                                only_problematic: bool = True,
                                limit: int = 50) -> list:
    """Q20: 查询依赖边的运行时验证状态 —— 找出「未被观测」「只被单源验证」「验证已过期」的边。

    ## 为什么需要它

    漂移判定原先**只用 DeepFlow DNS** 作运行时观测源。但 AWS SDK 通常在启动时
    解析一次域名就复用连接，走 VPC 端点更是不产生公网 DNS 查询 —— 结果一个
    每 24h 被调用 11,512 次的依赖，在 DNS 窗口里可以完全看不见。

    实测（2026-08-29，引入 X-Ray 源之前）：26 条带 `drift_status` 的边有
    **22 条**判为 `declared_not_observed`（85%），其中
    `petsearch → ServicesEks2-ddbpetadoption…` 被 X-Ray 的 11,512 次调用证否
    —— 那是**假阴性**，不是真漂移。

    现在有两个观测源（DNS 与 X-Ray，OR 关系，盲区不重叠），边上记 `verified_by`。
    本查询是 `verified_by` 的**读取方**：双源验证下，如果 X-Ray 侧静默失效
    （权限丢失、服务未插桩、窗口内无流量），判定会悄悄退回 DNS-only 而
    结果看起来完全正常。只有把「单源验证」和「验证过期」查出来才能发现这件事。

    ## 三类要关注的边

    | 症状 | 含义 |
    |---|---|
    | `drift_status='declared_not_observed'` | 声明了但两个源都没看到 —— 可能是死代码，也可能仍是假阴性 |
    | `verified_by` 只有 `dns` 或只有 `xray` | 单源验证，另一个源的盲区未被覆盖 |
    | `last_drift_check` 超过 stale_after_seconds | 验证已过期，判定不再代表当前状态 |

    Args:
        service_name: 只看该服务出发的依赖边。None 表示全图。
        stale_after_seconds: 超过这么久未验证即算过期，默认 1h
                             （对账每 5 分钟一轮，1h = 12 轮未更新才算异常）
        only_problematic: True 只返回上表三类之一；False 返回全部带验证状态的边
        limit: 返回条数上限

    Returns:
        [{'src':..., 'dst':..., 'dst_type':..., 'edge_type':...,
          'dependency_kind':..., 'source':..., 'drift_status':...,
          'runtime_verified':..., 'verified_by':..., 'last_drift_check':...,
          'stale_seconds':..., 'symptom':...}]
    """
    import time as _t
    now = int(_t.time())
    cutoff = now - int(stale_after_seconds)

    where = ["r.drift_status IS NOT NULL"]
    params = {"now": now, "cutoff": cutoff, "limit": limit}
    if service_name:
        where.append("a.name = $svc")
        params["svc"] = service_name
    if only_problematic:
        # 三类症状之一：未观测 / 单源验证 / 验证过期
        where.append(
            "(r.drift_status = 'declared_not_observed'"
            " OR r.verified_by IS NULL"
            " OR r.verified_by IN ['dns', 'xray', 'none']"
            " OR r.last_drift_check IS NULL"
            " OR r.last_drift_check < $cutoff)"
        )

    cypher = f"""
    MATCH (a)-[r]->(b)
    WHERE {' AND '.join(where)}
    RETURN a.name AS src, b.name AS dst, labels(b)[0] AS dst_type,
           type(r) AS edge_type, r.dependency_kind AS dependency_kind,
           r.source AS source, r.drift_status AS drift_status,
           r.runtime_verified AS runtime_verified, r.verified_by AS verified_by,
           r.last_drift_check AS last_drift_check,
           $now - coalesce(r.last_drift_check, 0) AS stale_seconds
    ORDER BY r.last_drift_check ASC
    LIMIT $limit
    """
    rows = nc.results(cypher, params)

    # 把症状归类挑明，调用方（含 agent）不必自己重推判定逻辑
    for row in rows:
        symptoms = []
        if row.get('drift_status') == 'declared_not_observed':
            symptoms.append('not_observed')
        vb = row.get('verified_by')
        if vb in (None, 'none'):
            symptoms.append('no_verification_source')
        elif vb in ('dns', 'xray'):
            symptoms.append(f'single_source:{vb}')
        ldc = row.get('last_drift_check')
        if ldc is None:
            symptoms.append('never_checked')
        elif int(ldc) < cutoff:
            symptoms.append('stale_verification')
        row['symptom'] = ','.join(symptoms) if symptoms else 'ok'
    return rows


def q21_observation_source_coverage(service_name: str = None,
                                    coverage: str = None,
                                    limit: int = 50) -> list:
    """Q21: 按**观测源**对账依赖边 —— 谁看到了这条依赖，谁没看到，各自的粒度如何。

    ## 为什么需要它

    图谱里同一条依赖可能被多个来源写入，但它们**不是主备关系，而是盲区不重叠的
    平行观测源**：

    | 源 | 前提 | 覆盖面 | 对 AWS 托管服务的粒度 |
    |---|---|---|---|
    | X-Ray (`xray_*` 属性) | 应用必须埋点 | 只有已插桩的服务 | **精确到资源名** |
    | DeepFlow (`deepflow-etl`/`deepflow-dns`) | eBPF，零埋点 | **所有 Pod** | 只到域名 |
    | NFM (`nfm_*` 属性) | AWS 网络遥测，零部署 | **VPC 内全部流** | 只到服务类别 |
    | AWS/CFN 声明 (`aws-etl`/`cfn-etl`) | 读资源配置 | 全部已声明资源 | 精确，但**不代表被调用过** |

    实测差异（2026-08-29，ap-northeast-1）：
      · X-Ray 只在应用埋点后可见；DeepFlow 看到全部 Pod；NFM 看到 VPC 内每条流
      · X-Ray 给出那张 DynamoDB 表的**完整名字**；DeepFlow 只能从
        DNS 域名反推，而走 VPC 端点时连域名都没有；NFM 只给
        `destinationCategory=AMAZON_DYNAMODB`，**连表名都没有**
      · 只有 NFM 给出 ENI 级网络路径（`traversedConstructs`）和
        `INTRA_AZ`/`INTER_AZ` 判定 —— 「这条依赖跨不跨 AZ」只有它能直接回答

    所以正确的用法不是「相信其中一个」，而是把图谱当**对账中心**：
    一条边被几个源看到，本身就是这条依赖可信度的度量。

    ## coverage 分类

    | 值 | 含义 | 运维解读 |
    |---|---|---|
    | `triple_corroborated` | 三个源都观测到 | 最可信 —— 应用埋点 / 内核 eBPF / AWS 网络遥测三种机制互证 |
    | `double_corroborated` | 两个源观测到 | 可信 —— `seen_by` 说明是哪两个 |
    | `xray_only` | 只有 X-Ray 看到 | 通常是**到 AWS 托管服务**的调用：连接复用或 VPC 端点让 DNS 侧看不见 |
    | `deepflow_only` | 只有 DeepFlow 看到 | 通常是**未插桩服务**发起的调用，X-Ray 里根本不存在 |
    | `nfm_only` | 只有 NFM 看到 | 走 VPC 端点、且发起方未插桩 —— 另两个源同时有盲点 |
    | `unobservable_by_design` | 业务层逻辑声明，**本质不可观测** | 不该被期待有运行时印证，不是缺陷 |
    | `observable_but_unobserved` | 该被观测到却没有 | **真盲区** —— 死代码，或所有源同时有盲点 |

    `observer_count` 直接给出观测源数量，便于排序和统计。

    > ⚠️ 原先只有 X-Ray / DeepFlow 两源时这里叫 `both`。
    > 引入 NFM 后该命名不再成立，且**曾产生一次真实误报**：
    > NFM 独家观测到的 `petsearch → dynamodb` 因为 coverage 判定不认识 NFM，
    > 被归入 `observable_but_unobserved`（真盲区）—— 一条正在被观测的边
    > 被报成了没人看见。教训是**加观测源必须同步改对账口径**，
    > 否则新源写进去的数据在报告里等于不存在。

    ## 为什么要把最后两类分开

    原先它们合并为一个 `declared_only`，实测 20 条里混了三种完全不同的东西：

    | 类别 | 条数 | 性质 |
    |---|---|---|
    | Lambda 的依赖 | 7 | 真盲区 —— eBPF 在 EKS 节点上抓不到 Lambda，X-Ray 当时也没覆盖 |
    | **BusinessCapability 的边** | **6** | **本质不可观测** —— 「支付流程依赖告警主题」是业务语义，不是一次网络调用 |
    | 微服务 → 数据存储 | 7 | 真盲区 —— SDK 连接复用 + VPC 端点让 DNS 看不见 |

    把第二类算进「盲区」会**高估依赖质量问题**：那 6 条边由 `business-layer`
    写入，描述的是业务能力对资源的逻辑依赖，运行时永远不会有一条网络包对应它。
    报告里把它们和「petsearch 每天访问 S3 却没被观测到」并列，等于让真问题被稀释。

    判据是 **source**：`business-layer` 写入的边归为 `unobservable_by_design`。
    这不是靠边类型或节点类型推断 —— provenance 本身就记录了它的性质。

    ## 粒度诚实性

    `dst_granularity='service'` 表示这条边的目标是 **AWSServiceEndpoint** ——
    X-Ray 只给了粗粒度服务名（字面的 `S3`），拿不到具体是哪个 bucket。
    图谱里有 30+ 个 S3Bucket，猜其中一个是**推断而非观测**，所以刻意不猜。
    demo 时这正好是个好对照：同一依赖 aws-etl 给资源级、X-Ray 给服务级。

    Args:
        service_name: 只看该服务出发的边。None 表示全图。
        coverage: 只返回某一类，取值 both|xray_only|deepflow_only|
                  unobservable_by_design|observable_but_unobserved。
                  None 表示全部。
        limit: 返回条数上限

    Returns:
        [{'src':..., 'edge_type':..., 'dst':..., 'dst_type':...,
          'dst_granularity':..., 'discovered_by':..., 'dependency_kind':...,
          'coverage':..., 'seen_by':[...],
          'xray_calls':..., 'xray_rt_seconds':..., 'xray_errors':...,
          'deepflow_calls':..., 'verified_by':...}]
    """
    where = ["r.dependency_kind IS NOT NULL"]
    params = {}
    if service_name:
        where.append("a.name = $svc")
        params["svc"] = service_name

    # ⚠️ 刻意**不在 Cypher 里 LIMIT**。
    #
    # coverage 的判定需要读 xray_call_count / calls / source，只能在 Python 侧做，
    # 所以如果 Cypher 先 LIMIT，过滤就发生在截断之后 —— 而盲区边恰恰是**调用量为 0**、
    # 在 ORDER BY 里排最后的那批，会被整批截掉。
    # 实测症状：coverage='observable_but_unobserved' limit=50 只返回 5 条，
    # 真实数量是 13 —— 一个专门用来找盲区的查询把盲区漏报了 62%。
    #
    # 依赖边总数是**图谱量级**（实测 81 条，与 dependency_kind 挂钩，
    # 不随遥测量增长），全量取回再在 Python 侧截断是安全的。
    cypher = f"""
    MATCH (a)-[r]->(b)
    WHERE {' AND '.join(where)}
    RETURN a.name AS src, type(r) AS edge_type, b.name AS dst,
           labels(b)[0] AS dst_type, b.granularity AS dst_granularity,
           r.source AS discovered_by, r.dependency_kind AS dependency_kind,
           r.xray_call_count AS xray_calls,
           r.xray_total_response_time_s AS xray_rt_seconds,
           r.xray_error_count AS xray_errors,
           r.calls AS deepflow_calls,
           r.nfm_last_seen AS nfm_last_seen,
           r.nfm_bytes AS nfm_bytes,
           r.nfm_cross_az AS nfm_cross_az,
           r.l4_last_seen AS l4_last_seen,
           r.l4_flow_count AS l4_flow_count,
           r.l4_via_instance AS l4_via_instance,
           r.verified_by AS verified_by,
           r.active AS active
    ORDER BY coalesce(r.xray_call_count, r.calls, r.nfm_bytes, 0) DESC
    """
    rows = nc.results(cypher, params)

    out = []
    for row in rows:
        # X-Ray 观测的判据是 xray_call_count 存在，而**不是**某个布尔标记。
        # 刻意不引入 observed_by_xray 布尔属性：它可由本字段推导，
        # 而能被推导出来的布尔量迟早与来源不一致。
        seen_xray = row.get('xray_calls') is not None
        src_name = row.get('discovered_by') or ''
        # DeepFlow 的判据有三条，任一成立即算它看到了 ——
        # DeepFlow 是**一个源、多个通道**，不是三个源：
        #   · 由 deepflow-* 发现（source，含 deepflow-etl / deepflow-dns / deepflow-l4）
        #   · 带 L7 度量 calls（对账时补写在别人发现的边上）
        #   · 带 L4 流度量 l4_last_seen（从 l4_flow_log 按 endpoint 解析出的
        #     IP 反查得到，覆盖「SDK 连接复用 / 走 VPC 端点导致 L7 与 DNS
        #     都看不见」的那批微服务→数据存储依赖）
        #
        # ⚠️ 加 l4_last_seen 这一条是**补上一次疏漏**：L4 通道上线后
        # 3 条 `微服务 → Aurora` 边已经被实测印证（l4_flow_count 152/237/164），
        # 但本判据当时只认 calls，于是它们继续被报成 `observable_but_unobserved`。
        # 这是「新增观测源却没同步对账口径」的第三次 —— 前两次是 NFM
        # 和这里的 L4。教训写在文件顶部：**新增写入通道必须同步改 Q21**。
        seen_deepflow = (src_name.startswith('deepflow')
                         or row.get('deepflow_calls') is not None
                         or row.get('l4_last_seen') is not None)
        # NFM 的判据同样是**度量存在**而非布尔标记，与 X-Ray 一致。
        # 只看 source=='nfm' 是不够的：NFM 在别人先发现的边上只补度量、
        # 刻意不覆盖 source，所以那些边的 source 仍是 deepflow-etl / xray。
        seen_nfm = row.get('nfm_last_seen') is not None or src_name == 'nfm'

        seen = []
        if seen_xray:
            seen.append('xray')
        if seen_deepflow:
            seen.append('deepflow')
        if seen_nfm:
            seen.append('nfm')

        if len(seen) >= 3:
            cov = 'triple_corroborated'
        elif len(seen) == 2:
            cov = 'double_corroborated'
        elif len(seen) == 1:
            cov = {'xray': 'xray_only',
                   'deepflow': 'deepflow_only',
                   'nfm': 'nfm_only'}[seen[0]]
        elif src_name == 'business-layer':
            # 业务层的逻辑声明**本质不可观测**：「支付流程依赖告警主题」
            # 描述的是业务能力对资源的依赖，运行时永远不会有一条网络包对应它。
            # 把它算进「盲区」会高估依赖质量问题、稀释真问题。
            # 判据用 provenance（source）而不是边类型或节点类型推断。
            cov = 'unobservable_by_design'
        else:
            cov = 'observable_but_unobserved'

        row['seen_by'] = seen
        row['observer_count'] = len(seen)
        row['coverage'] = cov
        if coverage and cov != coverage:
            continue
        out.append(row)
    # limit 在**过滤之后**生效 —— 见上方 Cypher 处的注释：
    # 先截断会把调用量为 0 的盲区边整批丢掉，让本查询漏报它本该找的东西。
    return out[:limit] if limit else out


# ──────────────────────────────────────────────────────────────────────────────
# Q22 / Q23：故障注入验证判定
#
# 补这两条的原因（2026-09-05）：查询库里原本**没有任何一条**查询能读到边上的
# 混沌验证判定，而那是本项目的头号产出。Q20 名字里也有 "verification"，但它读的
# 是**漂移层面**的验证（声明 vs 观测，DNS/X-Ray 两个观测源，字段 drift_status /
# runtime_verified / verified_by），与「故障注入证伪」完全是两件事：
#
#   Q20  观测层  这条边最近有没有被观测到？        → drift_status
#   Q22  干预层  在目标端注入故障，源端会不会退化？ → verify_status
#
# 两者可以同时成立又互相矛盾（一条边天天被观测到，却在注入实验里被证伪），
# 那种矛盾正是本平台想暴露的东西。
#
# 判定写入方只有 chaos-runner（见 profiles/graph_contract.yaml 的
# edge_verification.authority），本查询是只读的读取方。
# ──────────────────────────────────────────────────────────────────────────────

def _dependency_edge_labels() -> list:
    """
    契约里标 dependency: true 的边类型。

    优先从契约现取；取不到才用兜底清单——兜底清单与契约漂移会被
    tests/test_42_mcp_server.py 的同步性测试抓住。
    """
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
    return ["AccessesData", "Calls", "Delegates", "DependsOn", "InvokesTool", "Retrieves"]


def q22_edge_verification_verdicts(service_name: str = None,
                                   status: str = None,
                                   limit: int = 100) -> list:
    """Q22: 依赖边的**故障注入**验证判定（confirmed / refuted / inconclusive / untested）。

    ## 判定是怎么来的

    对边 `A → B`：**在 B 注入故障，观测 A**。A 退化 → 依赖成立；A 毫无反应
    且注入确认生效 → 依赖不成立。方向是全部关键——本项目最初把故障注入在 A
    再观测 A，于是历史上 72 个实验「全部通过、零失败」，因为那个判据对真边和
    假边给出完全相同的结果。

    ## 四种状态的含义

    | 状态 | 含义 | 能否作为推理依据 |
    |---|---|---|
    | `confirmed` | 观测方退化 ≥ 20%，依赖成立，`verify_degradation` 给出影响强度 | 可以 |
    | `refuted` | 观测方退化 ≤ 5% 且**注入已确认生效** | **不可以** |
    | `inconclusive` | 退化落在 5%–20%，或观测流量 < 20 请求，或注入是否生效未知 | 需标注为未定 |
    | `untested` | 尚未做过主动验证（多数边的状态，属正常） | 需声明未验证 |

    5%–20% 区间一律判未定而非证伪：重试、熔断、缓存都会掩盖真实依赖，
    在这个区间证伪会**删掉一条真实存在的边**。

    Args:
        service_name: 只看该服务作为源端的边。None 表示全图。
        status: 只看某一种状态（confirmed/refuted/inconclusive/untested）。None 表示全部。
        limit: 返回条数上限。

    Returns:
        [{'src':..., 'dst':..., 'edge_type':..., 'verify_status':...,
          'verify_confidence':..., 'verify_degradation':...,
          'verify_evidence_channel':..., 'verify_experiment':...,
          'verify_last':..., 'verify_reason':..., 'source':...}]
    """
    labels = ", ".join(f"'{x}'" for x in _dependency_edge_labels())
    where = [f"type(e) IN [{labels}]"]
    if service_name:
        where.append(f"(a.name = '{service_name}' OR b.name = '{service_name}')")
    if status:
        if status == "untested":
            where.append("(e.verify_status IS NULL OR e.verify_status = 'untested')")
        else:
            where.append(f"e.verify_status = '{status}'")

    cypher = (
        "MATCH (a)-[e]->(b) WHERE " + " AND ".join(where) + " "
        "RETURN coalesce(a.name, a.arn, 'unknown') AS src, "
        "coalesce(b.name, b.arn, 'unknown') AS dst, "
        "labels(b)[0] AS dst_type, type(e) AS edge_type, "
        "coalesce(e.verify_status, 'untested') AS verify_status, "
        "e.verify_confidence AS verify_confidence, "
        "e.verify_degradation AS verify_degradation, "
        "e.verify_evidence_channel AS verify_evidence_channel, "
        "e.verify_experiment AS verify_experiment, "
        "e.verify_last AS verify_last, "
        "e.verify_reason AS verify_reason, "
        "e.source AS source "
        "ORDER BY verify_status, edge_type, src "
        f"LIMIT {int(limit)}"
    )
    res = nc.query(cypher)
    return res.get("results", []) if isinstance(res, dict) else (res or [])


def q23_verification_coverage() -> dict:
    """Q23: 依赖边验证覆盖率汇总。

    `verified_ratio = (confirmed + refuted) / 全部依赖边`，即真正做过主动干预
    并得出结论的比例。

    这个数字对外时容易被误读为「完成度低」，其实相反：业界所有依赖图的这个
    数字都是 100% untested，只是没人算过——因为没有持久化的边实体、也没有
    故障注入后端，这个比例**无从计算**。

    Returns:
        {'total_dependency_edges': int,
         'by_status': {status: count},
         'per_edge_type': {edge_type: {status: count}},
         'verified_ratio': float,
         'refuted_count': int}
    """
    labels = ", ".join(f"'{x}'" for x in _dependency_edge_labels())
    cypher = (
        f"MATCH ()-[e]->() WHERE type(e) IN [{labels}] "
        "RETURN type(e) AS edge_type, "
        "coalesce(e.verify_status, 'untested') AS verify_status, "
        "count(*) AS c ORDER BY edge_type, verify_status"
    )
    res = nc.query(cypher)
    rows = res.get("results", []) if isinstance(res, dict) else (res or [])

    by_status: dict = {}
    per_type: dict = {}
    for r in rows:
        st = r.get("verify_status") or "untested"
        et = r.get("edge_type") or "unknown"
        c = int(r.get("c") or 0)
        by_status[st] = by_status.get(st, 0) + c
        per_type.setdefault(et, {})[st] = per_type.get(et, {}).get(st, 0) + c

    total = sum(by_status.values())
    decided = by_status.get("confirmed", 0) + by_status.get("refuted", 0)
    return {
        "total_dependency_edges": total,
        "by_status": by_status,
        "per_edge_type": per_type,
        "verified_ratio": round(decided / total, 4) if total else 0.0,
        "refuted_count": by_status.get("refuted", 0),
        "dependency_edge_types": _dependency_edge_labels(),
    }
