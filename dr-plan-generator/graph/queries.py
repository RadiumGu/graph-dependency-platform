"""
graph/queries.py — DR-specific Neptune openCypher queries (Q12–Q16)

Each function corresponds to a named query in TDD Section 3.2.
All functions return lists of dicts (normalized Neptune result rows).
"""

import logging
from typing import Any, Dict, List

from graph import neptune_client

logger = logging.getLogger(__name__)


def q12_az_dependency_tree(az_name: str) -> List[Dict[str, Any]]:
    """Q12: All resources deployed in a given AZ plus their upstream data dependencies.

    Args:
        az_name: Availability zone name, e.g. ``ap-northeast-1a``.

    Returns:
        List of node dicts with keys: name, type, tier, state, az_exposure.

    ## 2026-09-07: this query used to report ZERO affected services

    ``Microservice`` is **never** directly ``LocatedIn`` an availability zone.
    Measured on the live graph: 1 hop from Microservice to AZ reaches 0 services.
    What sits on an AZ is ``Pod`` (808 edges), ``Subnet``, ``LoadBalancer``,
    ``EC2Instance``, ``RDSInstance``, ``NeptuneInstance``.

    The real path is two hops::

        AZ <-[:LocatedIn]- Pod <-[:RunsOn]- Microservice

    So the old query returned hundreds of anchors and
    ``plan_generator.affected_services`` — which filters
    ``type in ("Microservice", "K8sService")`` — got **nothing**.
    An AZ-failure DR plan that says "0 affected services" is worse than no plan:
    it actively tells the operator an AZ loss has no service impact.

    Measured after the fix: ap-northeast-1a → 6 services,
    ap-northeast-1c → 7, ap-northeast-1d → 0 (that AZ really does hold
    only 2 resources).

    ## ``az_exposure``: fully lost vs merely degraded

    A service is not *located in* an AZ, it is *partly* there. Losing one AZ
    takes a multi-AZ service to **degraded**, not **down** — and conflating the
    two is how a DR plan overstates an outage. Live measurement:

        petsite            96 pods in 1a, 160 in 1c   -> degraded
        pethistory         17 / 19                     -> degraded
        petsearch           6 /  7                     -> degraded
        petlistadoptions    6 /  6                     -> degraded
        payforadoption      5 /  5                     -> degraded
        petfood             1 /  1                     -> degraded
        trafficgenerator    0 /  1                     -> SINGLE-AZ, fully lost

    ``trafficgenerator`` is the one that actually disappears when 1c goes.
    That single row is the whole reason this query exists, and the old
    implementation could not surface it.

    Values: ``single-az`` (all pods in this AZ — fully lost),
    ``multi-az`` (pods elsewhere too — degraded),
    ``None`` (not a pod-hosted service; exposure not applicable).
    """
    cypher = """
MATCH (az:AvailabilityZone {name: $az_name})
      <-[:LocatedIn]-(resource)
RETURN resource.name AS name, labels(resource)[0] AS type,
       resource.recovery_priority AS tier,
       resource.state AS state
UNION
MATCH (az:AvailabilityZone {name: $az_name})
      <-[:LocatedIn]-(resource)-[:AccessesData|DependsOn|WritesTo]->(data_resource)
RETURN data_resource.name AS name, labels(data_resource)[0] AS type,
       data_resource.recovery_priority AS tier,
       data_resource.state AS state
"""
    rows = neptune_client.results(cypher, {"az_name": az_name})
    rows = list(rows) + _services_hosted_in_az(az_name)
    # De-duplicate by name
    seen: set = set()
    deduped = []
    for r in rows:
        if r.get("name") not in seen:
            seen.add(r.get("name"))
            deduped.append(r)
    return deduped


def _services_hosted_in_az(az_name: str) -> List[Dict[str, Any]]:
    """Services whose pods sit in ``az_name``, tagged with their AZ exposure.

    Split out of :func:`q12_az_dependency_tree` because it needs the pod counts
    per AZ to tell ``single-az`` from ``multi-az`` — that cannot be expressed in
    the same UNION without changing the column shape of the other branches.

    A failure here must not take the whole plan down: the AZ anchors from the
    main query are still valid on their own, so a broken second hop degrades the
    plan to its old (service-blind) behaviour rather than raising. But it is
    logged at WARNING, because silently returning no services is exactly the
    bug this function exists to fix.
    """
    cypher = """
MATCH (s:Microservice)-[:RunsOn]->(p:Pod)-[:LocatedIn]->(z:AvailabilityZone)
RETURN s.name AS name, s.recovery_priority AS tier, s.state AS state,
       z.name AS az, count(DISTINCT p) AS pods
"""
    try:
        rows = neptune_client.results(cypher, {})
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not resolve services hosted in AZ %s (second hop "
            "Pod<-RunsOn-Microservice failed). The plan keeps its AZ-local "
            "resource anchors but will list no affected services.", az_name)
        return []

    spread: Dict[str, Dict[str, int]] = {}
    meta: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        nm = r.get("name")
        if not nm:
            continue
        spread.setdefault(nm, {})[r.get("az")] = r.get("pods") or 0
        meta[nm] = {"tier": r.get("tier"), "state": r.get("state")}

    out: List[Dict[str, Any]] = []
    for nm, per_az in spread.items():
        if not per_az.get(az_name):
            continue                      # no pods of this service in that AZ
        live_azs = [a for a, n in per_az.items() if n]
        out.append({
            "name": nm,
            "type": "Microservice",
            "tier": meta[nm]["tier"],
            "state": meta[nm]["state"],
            "az_exposure": "single-az" if len(live_azs) == 1 else "multi-az",
            "az_pod_counts": per_az,
        })
    return out


def q12_az_dependency_tree_by_region(region_name: str) -> List[Dict[str, Any]]:
    """Q12 variant: All resources in a region (across all AZs) plus data dependencies.

    Args:
        region_name: AWS region name, e.g. ``ap-northeast-1``.

    Returns:
        List of node dicts with keys: name, type, tier, state.
    """
    cypher = """
MATCH (r:Region {name: $region_name})-[:Contains]->(az:AvailabilityZone)
      <-[:LocatedIn]-(resource)
RETURN resource.name AS name, labels(resource)[0] AS type,
       resource.recovery_priority AS tier,
       resource.state AS state
UNION
MATCH (r:Region {name: $region_name})-[:Contains]->(az:AvailabilityZone)
      <-[:LocatedIn]-(resource)-[:AccessesData|DependsOn|WritesTo]->(data_resource)
RETURN data_resource.name AS name, labels(data_resource)[0] AS type,
       data_resource.recovery_priority AS tier,
       data_resource.state AS state
"""
    rows = neptune_client.results(cypher, {"region_name": region_name})
    # 同 q12_az_dependency_tree：Microservice 不直接 LocatedIn AZ，
    # 路径是 AZ <-LocatedIn- Pod <-RunsOn- Microservice。少这一跳会让
    # affected_services（按 type in Microservice/K8sService 过滤）恒为空。
    #
    # region 范围与 AZ 范围有一处本质区别：整个 region 失守时，
    # **所有** pod 都在受影响范围内，所以不存在「跨 AZ 所以只是降级」——
    # 这里一律标 single-az 语义不对，故不给 az_exposure，
    # 让下游不要按 AZ 逃逸做判断。
    rows = list(rows) + _services_hosted_in_region(region_name)
    seen: set = set()
    deduped = []
    for r in rows:
        if r.get("name") not in seen:
            seen.add(r.get("name"))
            deduped.append(r)
    return deduped


def _services_hosted_in_region(region_name: str) -> List[Dict[str, Any]]:
    """Services whose pods sit anywhere in ``region_name``.

    No ``az_exposure`` here: when the whole region is the failure scope,
    every pod is inside it, so "spread across AZs" buys nothing and tagging a
    service ``multi-az`` would wrongly read as "degraded, not down".
    """
    cypher = """
MATCH (r:Region {name: $region_name})-[:Contains]->(:AvailabilityZone)
      <-[:LocatedIn]-(p:Pod)<-[:RunsOn]-(s:Microservice)
RETURN DISTINCT s.name AS name, s.recovery_priority AS tier, s.state AS state
"""
    try:
        rows = neptune_client.results(cypher, {"region_name": region_name})
    except Exception:  # noqa: BLE001
        logger.warning(
            "Could not resolve services hosted in region %s (second hop "
            "Pod<-RunsOn-Microservice failed). The plan keeps its region-local "
            "resource anchors but will list no affected services.", region_name)
        return []
    return [{"name": r.get("name"), "type": "Microservice",
             "tier": r.get("tier"), "state": r.get("state")}
            for r in rows if r.get("name")]


def q12_service_dependency_tree(service_name: str) -> List[Dict[str, Any]]:
    """Q12 variant: Given a service, return it plus its full dependency chain.

    Args:
        service_name: Service name as stored in Neptune.

    Returns:
        List of node dicts with keys: name, type, tier, state.
    """
    cypher = """
MATCH path = (svc {name: $service_name})-[:Calls|DependsOn|AccessesData|WritesTo*0..8]->(dep)
UNWIND nodes(path) AS n
RETURN n.name AS name, labels(n)[0] AS type,
       n.recovery_priority AS tier,
       n.state AS state
"""
    rows = neptune_client.results(cypher, {"service_name": service_name})
    seen: set = set()
    deduped = []
    for r in rows:
        if r.get("name") not in seen:
            seen.add(r.get("name"))
            deduped.append(r)
    return deduped


def q13_data_layer_topology() -> List[Dict[str, Any]]:
    """Q13: All data stores and the services that depend on them.

    Returns:
        List of dicts with keys: data_store, ds_type, ds_az,
        dependent_services, tier.
    """
    cypher = """
MATCH (svc)-[:AccessesData|DependsOn|WritesTo]->(ds)
WHERE labels(ds)[0] IN ['RDSCluster', 'RDSInstance', 'DynamoDBTable',
                         'S3Bucket', 'SQSQueue', 'NeptuneCluster']
RETURN ds.name AS data_store, labels(ds)[0] AS ds_type,
       ds.az AS ds_az,
       collect(DISTINCT svc.name) AS dependent_services,
       ds.recovery_priority AS tier
"""
    return neptune_client.results(cypher)


def q14_cross_region_resources() -> List[Dict[str, Any]]:
    """Q14: Resources that have cross-region replication configured.

    Returns:
        List of dicts with keys: source_name, type, source_az,
        replica_name, replica_az.
    """
    cypher = """
MATCH (r)-[:ReplicatedTo]->(replica)
RETURN r.name AS source_name, labels(r)[0] AS type,
       r.az AS source_az,
       replica.name AS replica_name, replica.az AS replica_az
UNION
MATCH (dt:DynamoDBTable)
WHERE dt.global_table = true
RETURN dt.name AS source_name, 'DynamoDBTable' AS type,
       dt.az AS source_az,
       dt.replica_regions AS replica_name, '' AS replica_az
"""
    return neptune_client.results(cypher)


def q15_critical_path() -> List[Dict[str, Any]]:
    """Q15: Tier0 services ordered by dependency chain depth (longest first).

    Returns:
        List of dicts with keys: service, depth, chain, types.
    """
    cypher = """
MATCH path = (svc:Microservice)-[:Calls|DependsOn*1..10]->(dep)
WHERE svc.recovery_priority = 'Tier0'
RETURN svc.name AS service,
       length(path) AS depth,
       [n IN nodes(path) | n.name] AS chain,
       [n IN nodes(path) | labels(n)[0]] AS types
ORDER BY depth DESC
"""
    return neptune_client.results(cypher)


def q16_single_point_of_failure() -> List[Dict[str, Any]]:
    """Q16: Resources deployed in only one AZ but depended on by multiple services.

    Returns:
        List of dicts with keys: resource_name, type, single_az,
        services, svc_count.
    """
    cypher = """
MATCH (resource)<-[:AccessesData|DependsOn|WritesTo|RunsOn]-(svc)
WITH resource, collect(DISTINCT svc.name) AS services, count(DISTINCT svc) AS svc_count
WHERE svc_count >= 2
  AND size([(resource)-[:LocatedIn]->(az:AvailabilityZone) | az.name]) = 1
RETURN resource.name AS resource_name, labels(resource)[0] AS type,
       [(resource)-[:LocatedIn]->(az) | az.name][0] AS single_az,
       services, svc_count
ORDER BY svc_count DESC
"""
    return neptune_client.results(cypher)


def q_articulation_chokepoints(min_blocked: int = 2,
                              scope: str = "observed") -> List[Dict[str, Any]]:
    """移除该节点后，会让多少个下游从其上游**彻底不可达**（割点 + 阻断规模）。

    ## 为什么 q16 不够，必须另立一条

    `q16_single_point_of_failure` 的判据是「**只在单个 AZ** 且被 ≥2 个服务依赖」。
    那回答的是「一次 AZ 故障会打掉哪些被多方依赖的资源」，对 AZ 级故障是正确的。

    但它对**区域级托管服务完全失明**。实测 2026-09-07：
    `AgentGateway` 与全部 6 个 `AgentRuntime` 的 `LocatedIn` 边都是 **0** ——
    q16 的 `size([(resource)-[:LocatedIn]->(az)]) = 1` 恒为假，
    这些节点**永远不会**被它命中。而 AgentCore 网关承载全部 agent 间流量，
    它失效会让 orchestrator 到 4 个子 agent 全断。

    两者是**两种不同的故障模型**，不该塞进一个查询：
    q16 问「AZ 没了谁受影响」，本查询问「这个组件没了，谁就到不了它后面的东西」。
    把两种模型合并会得出没人能解释的答案 —— 与 `RoutesTo` 一个标签两种语义
    是同一类错误。

    ## 判据

    对每个 (上游 u, 候选 n, 下游 d) 三元组，若**不经 n** 就没有任何 u→d 的路径
    （长度 1..3，只走物理依赖边），则 d 被 n 阻断。按被阻断的 d 数量排序。

    ## 为什么必须排除 transitive 边

    `Delegates`（orchestrator → 子 agent）是两跳路径的汇总，物理上走
    `RoutesVia → AgentGateway → RoutesToRuntime`。若把它算进可达性，
    检查「绕开网关能否到 adoption」会命中这条 Delegates，
    于是**网关被判定为不是单点故障** —— 恰好把本查询要找的东西藏起来。

    边集合来自契约的 `physical_dependency_edge_labels()`，
    不在此处硬编码（硬编码清单漂移过：曾少 `Invokes`，Lambda 里漏 16 条边）。

    ## 已知局限（不要当成完整的 SPOF 判定）

    - **不判冗余**：一个有多副本的组件，只要拓扑上是唯一通路，仍会被列出。
      「是不是真的只有一个」要看 `LocatedIn` / 副本数，本查询不回答。
    - **路径长度截断在 3 跳**：更长的绕行路径不会被发现，因此可能**高估**阻断。
      放宽到 4+ 跳在本图规模（约 130 条依赖边）下也能跑，但收益未验证。
    - **`upstream = 1` 的条目未被过滤**：它们是链条中的一环而非扇入型咽喉点。
      刻意不过滤 —— 网关部署后 `fan_in` 恰好只有 1（仅 orchestrator），
      按扇入过滤会把本查询最该找到的那个节点漏掉。调用方按 `upstream`
      自行区分「多方依赖的枢纽」与「单链上的瓶颈」。

    Args:
        min_blocked: 至少阻断多少个下游才报告。
        scope: 只看该 node_scope 的候选（默认 observed，排除平台自身与脚手架）。

    Returns:
        List of dicts: chokepoint, type, blocked, upstream, blocked_sample。
        `blocked_sample` 最多 5 个被阻断节点名 —— 只给样本不给全量是刻意的：
        这份结果要给人看，全量列表在扇出大的节点上会长到没人读。
    """
    try:
        from graph_contract import physical_dependency_edge_labels  # type: ignore
        labels = sorted(physical_dependency_edge_labels())
    except Exception:  # noqa: BLE001
        # 兜底与 rca/neptune/neptune_queries.py::_dependency_edge_labels 同源，
        # 去掉 transitive 的 Delegates。漂移由 tests/test_59::t59_06 钉住。
        labels = ["AccessesData", "Calls", "DependsOn", "Invokes",
                  "InvokesTool", "PublishesTo", "Retrieves",
                  "RoutesToRuntime", "RoutesVia"]
    rel = "|".join(labels)
    cypher = f"""
MATCH (u)-[:{rel}]->(n)-[:{rel}]->(d)
WHERE u <> d AND u <> n AND n <> d AND n.scope = $scope
WITH DISTINCT u, n, d
OPTIONAL MATCH p = (u)-[:{rel}*1..3]->(d)
WHERE NOT n IN nodes(p)
WITH n, u, d, count(p) AS alt
WHERE alt = 0
WITH n, count(DISTINCT d) AS blocked, count(DISTINCT u) AS upstream,
     collect(DISTINCT d.name)[0..5] AS blocked_sample
WHERE blocked >= $min_blocked
RETURN n.name AS chokepoint, labels(n)[0] AS type, blocked, upstream,
       blocked_sample
ORDER BY blocked DESC, upstream DESC
"""
    return neptune_client.results(cypher, {"scope": scope,
                                           "min_blocked": min_blocked})


def q_edges_for_subgraph(node_names: List[str]) -> List[Dict[str, Any]]:
    """Fetch all dependency edges between a given set of node names.

    Args:
        node_names: List of node names to query edges for.

    Returns:
        List of dicts with keys: from, to, type.
    """
    if not node_names:
        return []
    cypher = """
MATCH (a)-[e:Calls|DependsOn|AccessesData|WritesTo|RunsOn|LocatedIn]->(b)
WHERE a.name IN $names AND b.name IN $names
RETURN a.name AS from, b.name AS to, type(e) AS type
"""
    return neptune_client.results(cypher, {"names": node_names})
