"""
dynamodb_setup.py - chaos-experiments 表建表脚本（一次性执行）
"""
import boto3
import json
import os
import sys

_PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..', '..', '..')
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, os.path.abspath(_PROJECT_ROOT))

from shared import get_region

REGION = get_region()
TABLE  = "chaos-experiments"


def create_table():
    ddb = boto3.client("dynamodb", region_name=REGION)

    # 检查是否已存在
    try:
        ddb.describe_table(TableName=TABLE)
        print(f"✅ 表 {TABLE} 已存在，跳过创建")
        return
    except ddb.exceptions.ResourceNotFoundException:
        pass

    resp = ddb.create_table(
        TableName=TABLE,
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[
            {"AttributeName": "experiment_id", "KeyType": "HASH"},
            {"AttributeName": "start_time",    "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "experiment_id",   "AttributeType": "S"},
            {"AttributeName": "start_time",      "AttributeType": "S"},
            {"AttributeName": "target_service",  "AttributeType": "S"},
            {"AttributeName": "status",          "AttributeType": "S"},
            {"AttributeName": "experiment_name", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "target_service-start_time-index",
                "KeySchema": [
                    {"AttributeName": "target_service", "KeyType": "HASH"},
                    {"AttributeName": "start_time",     "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "status-start_time-index",
                "KeySchema": [
                    {"AttributeName": "status",     "KeyType": "HASH"},
                    {"AttributeName": "start_time", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "experiment_name-start_time-index",
                "KeySchema": [
                    {"AttributeName": "experiment_name", "KeyType": "HASH"},
                    {"AttributeName": "start_time",      "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
    )
    print(f"✅ 表 {TABLE} 创建成功，状态: {resp['TableDescription']['TableStatus']}")

    # 等待创建完成
    waiter = ddb.get_waiter("table_exists")
    print("⏳ 等待表就绪...")
    waiter.wait(TableName=TABLE)
    print(f"✅ 表 {TABLE} 就绪")


if __name__ == "__main__":
    create_table()
