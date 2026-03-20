"""
config.py - 中心化配置

所有模块应从此处 import 常量，禁止在业务代码中硬编码 Region / Account ID / ARN。
环境变量优先，内置合理默认值。
"""
import os

# ─── AWS 基础 ─────────────────────────────────────────────────────────────────

REGION     = os.environ.get("AWS_DEFAULT_REGION", "ap-northeast-1")
ACCOUNT_ID = os.environ.get("AWS_ACCOUNT_ID", "926093770964")

# ─── Neptune ──────────────────────────────────────────────────────────────────

NEPTUNE_HOST     = os.environ.get(
    "NEPTUNE_HOST",
    "petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com",
)
NEPTUNE_PORT     = int(os.environ.get("NEPTUNE_PORT", "8182"))
NEPTUNE_ENDPOINT = f"https://{NEPTUNE_HOST}:{NEPTUNE_PORT}"

# ─── FIS ──────────────────────────────────────────────────────────────────────

FIS_ROLE_ARN  = os.environ.get(
    "FIS_ROLE_ARN",
    f"arn:aws:iam::{ACCOUNT_ID}:role/chaos-fis-experiment-role",
)
FIS_S3_BUCKET = os.environ.get("FIS_S3_BUCKET", f"chaos-fis-config-{ACCOUNT_ID}")

# ─── Bedrock ──────────────────────────────────────────────────────────────────

BEDROCK_REGION = os.environ.get("BEDROCK_REGION", "us-east-1")
BEDROCK_MODEL  = os.environ.get(
    "BEDROCK_MODEL", "us.anthropic.claude-sonnet-4-20250514"
)

# ─── DynamoDB ─────────────────────────────────────────────────────────────────

DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "chaos-experiments")
