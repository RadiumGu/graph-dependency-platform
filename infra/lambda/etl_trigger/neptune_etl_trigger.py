"""
neptune_etl_trigger.py
事件驱动 ETL 触发器

接收 SQS 消息（来自 EventBridge AWS 基础设施变更事件），
等待 30 秒（让 AWS 数据稳定），然后异步触发 neptune-etl-from-aws。

触发链：
  AWS 基础设施变更 → EventBridge → SQS → 本 Lambda → neptune-etl-from-aws

设计原则：
  - reservedConcurrentExecutions=1：同一时间只有一个实例运行，防止并发写入 Neptune
  - 30s 延迟：确保 AWS API 在 ETL 运行时已反映最新状态（如 RDS failover 后新 IP 已就绪）
  - 幂等：多次触发无副作用（ETL 本身基于 mergeV/mergeE）
"""

import os
import json
import time
import boto3
import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

DELAY_SECONDS = int(os.environ.get('TRIGGER_DELAY_SECONDS', '30'))
ETL_FUNCTION_NAME = os.environ.get('ETL_FUNCTION_NAME', 'neptune-etl-from-aws')
REGION = os.environ.get('REGION', 'ap-northeast-1')

lambda_client = boto3.client('lambda', region_name=REGION)


def handler(event, context):
    records = event.get('Records', [])
    logger.info(f"Received {len(records)} SQS record(s)")

    # 解析所有事件，收集来源信息（用于日志 + 传递给 ETL）
    event_sources = []
    for record in records:
        try:
            body = json.loads(record.get('body', '{}'))
            source = body.get('source', 'unknown')
            detail_type = body.get('detail-type', 'unknown')
            detail = body.get('detail', {})

            event_info = {
                'source': source,
                'detail_type': detail_type,
                'resource': _extract_resource_id(source, detail),
            }
            event_sources.append(event_info)
            logger.info(
                f"Event: source={source}, detail-type={detail_type}, "
                f"resource={event_info['resource']}"
            )
        except Exception as e:
            logger.warning(f"Failed to parse SQS record body: {e}")

    if not event_sources:
        logger.warning("No valid events parsed, skipping ETL trigger")
        return {'statusCode': 200, 'body': 'No valid events, skipped'}

    # 等待 AWS 数据稳定
    logger.info(f"Waiting {DELAY_SECONDS}s for AWS data to settle before ETL...")
    time.sleep(DELAY_SECONDS)

    # 异步触发 neptune-etl-from-aws
    payload = {
        'trigger_source': 'event_driven',
        'event_sources': event_sources,
    }
    logger.info(f"Invoking {ETL_FUNCTION_NAME} (async)...")
    try:
        response = lambda_client.invoke(
            FunctionName=ETL_FUNCTION_NAME,
            InvocationType='Event',  # 异步调用，不等结果
            Payload=json.dumps(payload),
        )
        status_code = response.get('StatusCode', 0)
        logger.info(f"Invoke response: StatusCode={status_code}")

        if status_code != 202:
            raise Exception(f"Unexpected invoke status code: {status_code} (expected 202)")

        logger.info(f"ETL triggered successfully. Events: {[e['source'] + '/' + e['detail_type'] for e in event_sources]}")
        return {
            'statusCode': 200,
            'body': f"ETL triggered, processed {len(event_sources)} event(s)",
        }
    except Exception as e:
        logger.error(f"Failed to invoke ETL Lambda: {e}")
        raise  # 让 SQS 重试


def _extract_resource_id(source: str, detail: dict) -> str:
    """从事件 detail 中提取资源标识符，用于日志记录。"""
    try:
        if source == 'aws.rds':
            return detail.get('SourceIdentifier', detail.get('DBInstanceIdentifier', 'unknown'))
        elif source == 'aws.ec2':
            return detail.get('instance-id', 'unknown')
        elif source == 'aws.eks':
            return detail.get('nodegroupName', detail.get('clusterName', 'unknown'))
        elif source == 'aws.elasticache':
            return detail.get('ReplicationGroupId', detail.get('CacheClusterId', 'unknown'))
        elif source == 'aws.elasticloadbalancing':
            return detail.get('requestParameters', {}).get('targetGroupArn', 'unknown')
        else:
            return 'unknown'
    except Exception:
        return 'unknown'
