"""
collectors/data_stores.py - DynamoDB, SQS, SNS, S3, ECR collectors.
"""

import json
import logging
from neptune_client import resolve_managed_by_dict, resolve_resource_tags
from config import REGION

logger = logging.getLogger()


def collect_dynamodb_tables(ddb_client) -> list:
    tables = []
    paginator = ddb_client.get_paginator('list_tables')
    for page in paginator.paginate():
        for table_name in page['TableNames']:
            try:
                desc = ddb_client.describe_table(TableName=table_name)['Table']
                table_arn = desc.get('TableArn', '')
                try:
                    tags_resp = ddb_client.list_tags_of_resource(ResourceArn=table_arn)
                    tag_dict = {t['Key']: t['Value'] for t in tags_resp.get('Tags', [])}
                    managed_by = resolve_managed_by_dict(tag_dict)
                except Exception:
                    managed_by = 'manual'
                tables.append({
                    'name': table_name,
                    'arn': table_arn,
                    'status': desc.get('TableStatus', ''),
                    'managed_by': managed_by,
                })
            except Exception as e:
                logger.warning(f"DynamoDB table {table_name} describe failed: {e}")
    logger.info(f"DynamoDB tables: {len(tables)}")
    return tables


def collect_sqs_queues(sqs_client) -> list:
    queues = []
    try:
        paginator = sqs_client.get_paginator('list_queues')
        for page in paginator.paginate():
            for url in page.get('QueueUrls', []):
                queue_name = url.split('/')[-1]
                try:
                    attrs = sqs_client.get_queue_attributes(
                        QueueUrl=url,
                        AttributeNames=['QueueArn', 'ApproximateNumberOfMessages',
                                        'VisibilityTimeout', 'MessageRetentionPeriod',
                                        'RedrivePolicy', 'RedriveAllowPolicy']
                    ).get('Attributes', {})
                    try:
                        tags_resp = sqs_client.list_queue_tags(QueueUrl=url)
                        tags_dict = tags_resp.get('Tags', {})
                        rtags = resolve_resource_tags(tags_dict)
                        managed_by = rtags.get('managed_by_tag') or resolve_managed_by_dict(tags_dict)
                    except Exception:
                        managed_by = 'manual'
                        rtags = {'environment': None, 'system': None, 'team': None, 'tier': None}
                    is_dlq = 'dlq' in queue_name.lower() or 'dead' in queue_name.lower()
                    redrive_target_arn = None
                    redrive_raw = attrs.get('RedrivePolicy', '')
                    if redrive_raw:
                        try:
                            rp = json.loads(redrive_raw)
                            redrive_target_arn = rp.get('deadLetterTargetArn')
                        except Exception:
                            pass
                    queues.append({
                        'name': queue_name,
                        'url': url,
                        'arn': attrs.get('QueueArn', ''),
                        'is_dlq': str(is_dlq),
                        'managed_by': managed_by,
                        'redrive_target_arn': redrive_target_arn,
                        'environment': rtags['environment'],
                        'system': rtags['system'],
                        'team': rtags['team'],
                        'tag_tier': rtags['tier'],
                    })
                except Exception as e:
                    logger.warning(f"SQS queue {queue_name} attrs failed: {e}")
    except Exception as e:
        logger.error(f"SQS list failed: {e}")
    logger.info(f"SQS queues: {len(queues)}")
    return queues


def collect_sns_topics(sns_client) -> list:
    topics = []
    try:
        paginator = sns_client.get_paginator('list_topics')
        for page in paginator.paginate():
            for topic in page.get('Topics', []):
                arn = topic['TopicArn']
                name = arn.split(':')[-1]
                try:
                    tags_resp = sns_client.list_tags_for_resource(ResourceArn=arn)
                    tag_dict = {t['Key']: t['Value'] for t in tags_resp.get('Tags', [])}
                    managed_by = resolve_managed_by_dict(tag_dict)
                except Exception:
                    managed_by = 'manual'
                try:
                    attrs = sns_client.get_topic_attributes(TopicArn=arn).get('Attributes', {})
                    subs_confirmed = attrs.get('SubscriptionsConfirmed', '0')
                except Exception:
                    subs_confirmed = '0'
                topics.append({
                    'name': name,
                    'arn': arn,
                    'subscriptions_confirmed': subs_confirmed,
                    'managed_by': managed_by,
                })
    except Exception as e:
        logger.error(f"SNS list failed: {e}")
    logger.info(f"SNS topics: {len(topics)}")
    return topics


def collect_s3_buckets_in_region(s3_client) -> list:
    buckets = []
    try:
        resp = s3_client.list_buckets()
        for bucket in resp.get('Buckets', []):
            bname = bucket['Name']
            try:
                loc = s3_client.get_bucket_location(Bucket=bname)
                bucket_region = loc.get('LocationConstraint') or 'us-east-1'
                if bucket_region != REGION:
                    continue
                try:
                    tags_resp = s3_client.get_bucket_tagging(Bucket=bname)
                    tag_dict = {t['Key']: t['Value'] for t in tags_resp.get('TagSet', [])}
                    managed_by = resolve_managed_by_dict(tag_dict)
                except Exception:
                    managed_by = 'manual'
                buckets.append({
                    'name': bname,
                    'region': bucket_region,
                    'managed_by': managed_by,
                })
            except Exception as e:
                logger.debug(f"S3 bucket {bname} skip: {e}")
    except Exception as e:
        logger.error(f"S3 list_buckets failed: {e}")
    logger.info(f"S3 buckets (region={REGION}): {len(buckets)}")
    return buckets


def collect_ecr_repositories(ecr_client) -> list:
    repos = []
    try:
        paginator = ecr_client.get_paginator('describe_repositories')
        for page in paginator.paginate():
            for repo in page.get('repositories', []):
                try:
                    tags_resp = ecr_client.list_tags_for_resource(resourceArn=repo['repositoryArn'])
                    tag_dict = {t['Key']: t['Value'] for t in tags_resp.get('tags', [])}
                    managed_by = resolve_managed_by_dict(tag_dict)
                except Exception:
                    managed_by = 'manual'
                repos.append({
                    'name': repo['repositoryName'],
                    'arn': repo['repositoryArn'],
                    'uri': repo['repositoryUri'],
                    'managed_by': managed_by,
                })
    except Exception as e:
        logger.error(f"ECR list failed: {e}")
    logger.info(f"ECR repositories: {len(repos)}")
    return repos
