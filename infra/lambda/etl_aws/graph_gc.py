"""
gc.py - Ghost-node garbage collection for neptune-etl-from-aws.

Compares each resource type in Neptune against the current AWS state
and drops nodes that no longer exist.

## 本文件的唯一安全原则：不确定就不删

`_gc_vertices` 做的是集合差 —— 图里有、而本轮 AWS 清单里没有的节点，直接
`g.V(...).drop()`。所以**任何让 AWS 清单不完整的情况都等价于删真实资源**：

  · 分页少收（未分页的 list_* 超过单页上限）
  · 一次瞬时限流被 `except: pass` 吞掉
  · 达到分页跑飞护栏

这三者在图里的表现与「资源真的被删了」完全相同，无法区分。

因此本文件里每个 `_gc_vertices` 调用的前置清单，要么完整，要么**整段放弃
这一类的 GC** —— 放弃 GC 只会留下陈旧节点（下一轮会收），而误删是不可逆的。
"""

import logging
import boto3

# paginate_all: 带跑飞护栏的分页收集。上限或截止时间触发时**抛异常**而非
# 返回部分结果 —— 见上面的安全原则。
# 防御式 import：Layer 未更新时降级为不分页（与改动前行为一致），
# 使函数代码的部署不依赖 Layer 的部署顺序。
try:
    from aws_resilience import paginate_all
except ImportError:  # pragma: no cover - Layer 未更新时的降级路径
    import logging as _lg
    _lg.getLogger(__name__).warning(
        "AWS_RESILIENCE_UNAVAILABLE —— Layer 里没有 aws_resilience，"
        "paginate_all 降级为不分页（与改动前行为一致）。"
        "清单可能少收，而少收会让 _gc_vertices 删掉真实存在的资源。"
    )

    def paginate_all(client, operation_name, key, **kwargs):  # type: ignore[misc]
        return getattr(client, operation_name)(**kwargs).get(key, []) or []

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
        # list_clusters 每页 100。原实现 `eks_client.list_clusters()` 不分页，
        # 超过 100 个集群就静默少收 —— 而少收会让下面这行把真实存在的集群
        # 判为 ghost 节点删除。当前只有 1 个集群所以没触发过。
        aws_eks = set(paginate_all(eks_client, 'list_clusters', 'clusters'))
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
        #
        # 两处误删路径，一起修：
        #  1. list_buckets 原本不分页。ListBuckets 自 2024 起支持分页
        #     （ContinuationToken / MaxBuckets），少收就会误删。
        #  2. 原实现对 get_bucket_location 的失败是 `except Exception: pass`
        #     —— 桶不进 aws_s3，于是被判为 ghost 节点删掉。也就是说
        #     **一次瞬时限流就能让一个真实存在的桶从图里消失**。
        #
        # 改成：桶清单只要有任何不确定，就整段放弃 S3Bucket 的 GC。
        # 放弃只留下陈旧节点（下一轮会收），误删不可逆。
        try:
            aws_s3 = set()
            for b in paginate_all(s3_client, 'list_buckets', 'Buckets'):
                loc = s3_client.get_bucket_location(Bucket=b['Name'])
                bucket_region = loc.get('LocationConstraint') or 'us-east-1'
                if bucket_region == REGION:
                    aws_s3.add(b['Name'])
        except Exception as _e:
            logger.warning(
                "GC S3Bucket 放弃本轮：桶清单不完整（%s: %s）。"
                "不完整的清单会把真实存在的桶判为 ghost 节点删除。",
                type(_e).__name__, _e,
            )
        else:
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
