"""
gc.py - Ghost-node garbage collection for neptune-etl-from-aws.

Compares each resource type in Neptune against the current AWS state
and drops nodes that no longer exist.
"""

import logging
import boto3

from neptune_client import neptune_query
from config import REGION

logger = logging.getLogger()


def _gc_vertices(label: str, id_prop: str, aws_ids: set) -> int:
    """Drop stale Neptune nodes of given label whose id_prop is not in aws_ids."""
    dropped = 0
    try:
        resp = neptune_query(
            f"g.V().hasLabel('{label}').project('vid','pid')"
            f".by(id()).by(coalesce(values('{id_prop}'),constant('__missing__'))).fold()"
        )['result']['data']['@value']
        if not resp:
            return 0
        graph_map = {}
        for item in resp[0].get('@value', []):
            m = item.get('@value', [])
            it = iter(m)
            kv = dict(zip(it, it))
            vid = kv.get('vid', {})
            pid = kv.get('pid', '')
            if isinstance(vid, dict): vid = vid.get('@value', str(vid))
            if isinstance(pid, dict): pid = pid.get('@value', str(pid))
            pid = str(pid)
            if pid != '__missing__':
                graph_map[pid] = str(vid)
        stale = set(graph_map.keys()) - aws_ids
        for pid in stale:
            logger.info(f"GC: dropping ghost node {label}[{id_prop}={pid}]")
            neptune_query(f"g.V('{graph_map[pid]}').drop()")
            dropped += 1
    except Exception as e:
        logger.warning(f"GC {label} failed: {e}")
    return dropped


def run_gc(session, ec2_client, eks_client, elb_client, lambda_client,
           sfn_client, ddb_client, rds_client, sqs_client, sns_client,
           s3_client, ecr_client) -> int:
    """Run full GC sweep. Returns total number of dropped nodes."""
    gc_total = 0
    try:
        # EC2
        aws_ec2 = set()
        for page in ec2_client.get_paginator('describe_instances').paginate(
                Filters=[{'Name': 'instance-state-name', 'Values': ['running']}]):
            for r in page['Reservations']:
                for i in r['Instances']:
                    aws_ec2.add(i['InstanceId'])
        gc_total += _gc_vertices('EC2Instance', 'instance_id', aws_ec2)

        # Lambda
        aws_lambda = set()
        for page in lambda_client.get_paginator('list_functions').paginate():
            for fn in page['Functions']:
                aws_lambda.add(fn['FunctionName'])
        gc_total += _gc_vertices('LambdaFunction', 'name', aws_lambda)

        # RDS/Neptune clusters
        aws_rds_clusters = set()
        for page in rds_client.get_paginator('describe_db_clusters').paginate():
            for c in page['DBClusters']:
                aws_rds_clusters.add(c['DBClusterIdentifier'])
        gc_total += _gc_vertices('RDSCluster', 'name', aws_rds_clusters)
        gc_total += _gc_vertices('NeptuneCluster', 'name', aws_rds_clusters)

        # RDS instances
        aws_rds_instances = set()
        for page in rds_client.get_paginator('describe_db_instances').paginate():
            for i in page['DBInstances']:
                aws_rds_instances.add(i['DBInstanceIdentifier'])
        gc_total += _gc_vertices('RDSInstance', 'name', aws_rds_instances)
        gc_total += _gc_vertices('NeptuneInstance', 'name', aws_rds_instances)

        # EKS
        aws_eks = set(eks_client.list_clusters().get('clusters', []))
        gc_total += _gc_vertices('EKSCluster', 'name', aws_eks)

        # ALB
        aws_alb = set()
        for page in elb_client.get_paginator('describe_load_balancers').paginate():
            for lb in page['LoadBalancers']:
                aws_alb.add(lb['LoadBalancerName'])
        gc_total += _gc_vertices('LoadBalancer', 'name', aws_alb)

        # SQS
        aws_sqs = set()
        for page in sqs_client.get_paginator('list_queues').paginate():
            for url in page.get('QueueUrls', []):
                aws_sqs.add(url.split('/')[-1])
        gc_total += _gc_vertices('SQSQueue', 'name', aws_sqs)

        # SNS
        aws_sns = set()
        for page in sns_client.get_paginator('list_topics').paginate():
            for t in page.get('Topics', []):
                aws_sns.add(t['TopicArn'].split(':')[-1])
        gc_total += _gc_vertices('SNSTopic', 'name', aws_sns)

        # DynamoDB
        aws_ddb = set()
        for page in ddb_client.get_paginator('list_tables').paginate():
            aws_ddb.update(page.get('TableNames', []))
        gc_total += _gc_vertices('DynamoDBTable', 'name', aws_ddb)

        # Step Functions
        aws_sfn = set()
        for page in sfn_client.get_paginator('list_state_machines').paginate():
            for sm in page['stateMachines']:
                aws_sfn.add(sm['name'])
        gc_total += _gc_vertices('StepFunction', 'name', aws_sfn)

        # S3 (region-local only)
        aws_s3 = set()
        for b in s3_client.list_buckets().get('Buckets', []):
            try:
                loc = s3_client.get_bucket_location(Bucket=b['Name'])
                bucket_region = loc.get('LocationConstraint') or 'us-east-1'
                if bucket_region == REGION:
                    aws_s3.add(b['Name'])
            except Exception:
                pass
        gc_total += _gc_vertices('S3Bucket', 'name', aws_s3)

        # ECR
        aws_ecr = set()
        for page in ecr_client.get_paginator('describe_repositories').paginate():
            for r in page['repositories']:
                aws_ecr.add(r['repositoryName'])
        gc_total += _gc_vertices('ECRRepository', 'name', aws_ecr)

        if gc_total:
            logger.info(f"GC complete: dropped {gc_total} ghost nodes")
        else:
            logger.info("GC complete: no ghost nodes found")
    except Exception as e:
        logger.error(f"GC sweep failed (non-fatal): {e}", exc_info=True)
    return gc_total
