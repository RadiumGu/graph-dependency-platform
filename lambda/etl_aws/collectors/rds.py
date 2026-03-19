"""
collectors/rds.py - RDS and Aurora cluster/instance collectors.
"""

import logging
from neptune_client import resolve_managed_by_dict, resolve_resource_tags

logger = logging.getLogger()


def collect_rds_clusters(rds_client) -> list:
    clusters = []
    paginator = rds_client.get_paginator('describe_db_clusters')
    for page in paginator.paginate():
        for cluster in page['DBClusters']:
            tag_dict = {t['Key']: t['Value'] for t in cluster.get('TagList', [])}
            rtags = resolve_resource_tags(tag_dict)
            managed_by = rtags.get('managed_by_tag') or resolve_managed_by_dict(tag_dict)
            clusters.append({
                'id': cluster['DBClusterIdentifier'],
                'arn': cluster.get('DBClusterArn', ''),
                'engine': cluster.get('Engine', ''),
                'engine_version': cluster.get('EngineVersion', ''),
                'status': cluster.get('Status', ''),
                'endpoint': cluster.get('Endpoint', ''),
                'reader_endpoint': cluster.get('ReaderEndpoint', ''),
                'azs': cluster.get('AvailabilityZones', []),
                'managed_by': managed_by,
                'environment': rtags['environment'],
                'system': rtags['system'],
                'team': rtags['team'],
                'tag_tier': rtags['tier'],
                'member_roles': {
                    m['DBInstanceIdentifier']: m.get('IsClusterWriter', False)
                    for m in cluster.get('DBClusterMembers', [])
                },
            })
    logger.info(f"RDS clusters: {len(clusters)}")
    return clusters


def collect_rds_instances(rds_client) -> list:
    instances = []
    paginator = rds_client.get_paginator('describe_db_instances')
    for page in paginator.paginate():
        for inst in page['DBInstances']:
            tag_dict = {t['Key']: t['Value'] for t in inst.get('TagList', [])}
            managed_by = resolve_managed_by_dict(tag_dict)
            instances.append({
                'id': inst['DBInstanceIdentifier'],
                'arn': inst.get('DBInstanceArn', ''),
                'engine': inst.get('Engine', ''),
                'instance_class': inst.get('DBInstanceClass', ''),
                'az': inst.get('AvailabilityZone', ''),
                'status': inst.get('DBInstanceStatus', ''),
                'cluster_id': inst.get('DBClusterIdentifier', ''),
                'endpoint': inst.get('Endpoint', {}).get('Address', ''),
                'port': str(inst.get('Endpoint', {}).get('Port', '')),
                'is_writer': not inst.get('ReadReplicaSourceDBInstanceIdentifier'),
                'managed_by': managed_by,
            })
    logger.info(f"RDS instances: {len(instances)}")
    return instances
