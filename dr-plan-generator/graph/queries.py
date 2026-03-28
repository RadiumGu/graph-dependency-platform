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
        az_name: Availability zone name, e.g. ``apne1-az1``.

    Returns:
        List of node dicts with keys: name, type, tier, state.
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
    # De-duplicate by name
    seen: set = set()
    deduped = []
    for r in rows:
        if r.get("name") not in seen:
            seen.add(r.get("name"))
            deduped.append(r)
    return deduped


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
    seen: set = set()
    deduped = []
    for r in rows:
        if r.get("name") not in seen:
            seen.add(r.get("name"))
            deduped.append(r)
    return deduped


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
