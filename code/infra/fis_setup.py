"""
fis_setup.py - FIS 实验基础设施一次性创建脚本

幂等（check-before-create）：安全重复执行，已存在的资源跳过。

创建内容：
  1. FIS Experiment IAM Role（chaos-fis-experiment-role）
     - 包含 Lambda / RDS / EKS / EC2 / EBS / 网络 / API 注入权限
  2. CloudWatch Alarms（FIS Stop Conditions）
     - chaos-eks-sr-critical
     - chaos-petstatusupdater-sr-critical
     - chaos-petlistadoptions-sr-critical
  3. FIS Lambda Extension S3 Bucket（chaos-fis-config-{account_id}）
     - Lambda FIS Extension 通信所需的 S3 bucket

用法：
  python3 fis_setup.py
  python3 fis_setup.py --dry-run    # 仅检查，不创建
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

REGION     = "ap-northeast-1"
ACCOUNT_ID = "926093770964"

# ─── FIS IAM Role ─────────────────────────────────────────────────────────────

FIS_ROLE_NAME = "chaos-fis-experiment-role"

FIS_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "fis.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }],
}

FIS_PERMISSIONS = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "FISLambdaActions",
            "Effect": "Allow",
            "Action": [
                "lambda:GetFunction",
                "lambda:GetFunctionConfiguration",
            ],
            "Resource": f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:*",
        },
        {
            "Sid": "FISRDSActions",
            "Effect": "Allow",
            "Action": [
                "rds:FailoverDBCluster",
                "rds:RebootDBInstance",
                "rds:DescribeDBClusters",
                "rds:DescribeDBInstances",
            ],
            "Resource": f"arn:aws:rds:{REGION}:{ACCOUNT_ID}:cluster:*",
        },
        {
            "Sid": "FISEKSActions",
            "Effect": "Allow",
            "Action": [
                "eks:DescribeNodegroup",
                "ec2:TerminateInstances",
                "ec2:DescribeInstances",
                "autoscaling:DescribeAutoScalingGroups",
            ],
            "Resource": "*",
            "Condition": {
                "StringEquals": {
                    "aws:ResourceTag/eks:cluster-name": "PetSite",
                },
            },
        },
        {
            "Sid": "FISNetworkActions",
            "Effect": "Allow",
            "Action": [
                "ec2:CreateNetworkAcl",
                "ec2:CreateNetworkAclEntry",
                "ec2:DeleteNetworkAcl",
                "ec2:DeleteNetworkAclEntry",
                "ec2:DescribeNetworkAcls",
                "ec2:ReplaceNetworkAclAssociation",
                "ec2:ReplaceNetworkAclEntry",
                "ec2:DescribeSubnets",
                "ec2:DescribeVpcs",
            ],
            "Resource": "*",
        },
        {
            "Sid": "FISEBSActions",
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeVolumes",
                "ec2:PauseVolumeIO",
            ],
            "Resource": "*",
        },
        {
            "Sid": "FISAPIInjection",
            "Effect": "Allow",
            "Action": [
                "fis:InjectApiInternalError",
                "fis:InjectApiThrottleError",
                "fis:InjectApiUnavailableError",
            ],
            "Resource": "*",
        },
        {
            "Sid": "FISCloudWatch",
            "Effect": "Allow",
            "Action": [
                "cloudwatch:DescribeAlarms",
            ],
            "Resource": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT_ID}:alarm:chaos-*",
        },
        {
            "Sid": "FISLogging",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogDelivery",
                "logs:PutLogEvents",
            ],
            "Resource": "*",
        },
    ],
}

FIS_INLINE_POLICY_NAME = "chaos-fis-experiment-policy"

# ─── CloudWatch Alarms（FIS Stop Conditions）──────────────────────────────────

# 每个实验场景对应一个 CloudWatch Alarm，作为 FIS 原生 Stop Condition
STOP_CONDITION_ALARMS = [
    {
        "AlarmName": "chaos-eks-sr-critical",
        "AlarmDescription": "FIS Stop Condition: EKS 实验期间 ALB 5XX 超阈值",
        "MetricName": "HTTPCode_Target_5XX_Count",
        "Namespace": "AWS/ApplicationELB",
        "Statistic": "Sum",
        "Period": 60,
        "EvaluationPeriods": 2,
        "Threshold": 100.0,
        "ComparisonOperator": "GreaterThanThreshold",
        "TreatMissingData": "notBreaching",
    },
    {
        "AlarmName": "chaos-petstatusupdater-sr-critical",
        "AlarmDescription": "FIS Stop Condition: petstatusupdater Lambda 错误数超阈值",
        "MetricName": "Errors",
        "Namespace": "AWS/Lambda",
        "Statistic": "Sum",
        "Period": 60,
        "EvaluationPeriods": 2,
        "Threshold": 50.0,
        "ComparisonOperator": "GreaterThanThreshold",
        "TreatMissingData": "notBreaching",
        "Dimensions": [{"Name": "FunctionName", "Value": "ServicesEks2-statusupdaterservicelambdafn37242E00-0SHsIkrhwJ32"}],
    },
    {
        "AlarmName": "chaos-petadoptionshistory-sr-critical",
        "AlarmDescription": "FIS Stop Condition: petadoptionshistory Lambda 错误数超阈值",
        "MetricName": "Errors",
        "Namespace": "AWS/Lambda",
        "Statistic": "Sum",
        "Period": 60,
        "EvaluationPeriods": 2,
        "Threshold": 50.0,
        "ComparisonOperator": "GreaterThanThreshold",
        "TreatMissingData": "notBreaching",
        "Dimensions": [{"Name": "FunctionName", "Value": "petadoptionshistory"}],
    },
    {
        "AlarmName": "chaos-petlistadoptions-sr-critical",
        "AlarmDescription": "FIS Stop Condition: petlistadoptions ALB 5XX 超阈值",
        "MetricName": "HTTPCode_Target_5XX_Count",
        "Namespace": "AWS/ApplicationELB",
        "Statistic": "Sum",
        "Period": 60,
        "EvaluationPeriods": 2,
        "Threshold": 50.0,
        "ComparisonOperator": "GreaterThanThreshold",
        "TreatMissingData": "notBreaching",
    },
    {
        "AlarmName": "chaos-rds-sr-critical",
        "AlarmDescription": "FIS Stop Condition: Aurora RDS 连接失败超阈值",
        "MetricName": "DatabaseConnections",
        "Namespace": "AWS/RDS",
        "Statistic": "Sum",
        "Period": 60,
        "EvaluationPeriods": 2,
        "Threshold": 0.0,
        "ComparisonOperator": "LessThanOrEqualToThreshold",
        "TreatMissingData": "breaching",
    },
]

# ─── FIS Lambda Extension S3 Bucket ──────────────────────────────────────────

FIS_S3_BUCKET = f"chaos-fis-config-{ACCOUNT_ID}"


# ─── 核心函数 ─────────────────────────────────────────────────────────────────

def setup_fis_role(iam: object, dry_run: bool = False) -> str:
    """
    创建 FIS Experiment IAM Role（幂等）。

    Returns:
        Role ARN
    """
    # 检查是否已存在
    try:
        resp = iam.get_role(RoleName=FIS_ROLE_NAME)
        arn = resp["Role"]["Arn"]
        logger.info(f"✅ IAM Role 已存在，跳过创建: {arn}")
        return arn
    except ClientError as e:
        if e.response["Error"]["Code"] != "NoSuchEntity":
            raise

    if dry_run:
        logger.info(f"[dry-run] 将创建 IAM Role: {FIS_ROLE_NAME}")
        return f"arn:aws:iam::{ACCOUNT_ID}:role/{FIS_ROLE_NAME}"

    # 创建 Role
    logger.info(f"创建 FIS IAM Role: {FIS_ROLE_NAME}")
    resp = iam.create_role(
        RoleName=FIS_ROLE_NAME,
        AssumeRolePolicyDocument=json.dumps(FIS_TRUST_POLICY),
        Description="FIS experiment execution role for chaos-automation (isolated from PetSite business roles)",
        Tags=[
            {"Key": "chaos-automation", "Value": "true"},
            {"Key": "managed-by", "Value": "fis_setup.py"},
        ],
    )
    role_arn = resp["Role"]["Arn"]
    logger.info(f"✅ IAM Role 创建成功: {role_arn}")

    # 附加内联策略
    logger.info(f"附加内联策略: {FIS_INLINE_POLICY_NAME}")
    iam.put_role_policy(
        RoleName=FIS_ROLE_NAME,
        PolicyName=FIS_INLINE_POLICY_NAME,
        PolicyDocument=json.dumps(FIS_PERMISSIONS),
    )
    logger.info(f"✅ IAM 内联策略已附加")

    return role_arn


def setup_cloudwatch_alarms(cw: object, dry_run: bool = False) -> list[str]:
    """
    创建 CloudWatch Alarms 用作 FIS Stop Conditions（幂等）。

    Returns:
        list of Alarm ARNs
    """
    alarm_arns = []

    for alarm_cfg in STOP_CONDITION_ALARMS:
        alarm_name = alarm_cfg["AlarmName"]

        # 检查是否已存在
        try:
            resp = cw.describe_alarms(AlarmNames=[alarm_name])
            existing = resp.get("MetricAlarms", [])
            if existing:
                arn = existing[0]["AlarmArn"]
                logger.info(f"✅ CloudWatch Alarm 已存在，跳过: {alarm_name}")
                alarm_arns.append(arn)
                continue
        except ClientError as e:
            logger.warning(f"检查 Alarm 失败: {e}")

        if dry_run:
            fake_arn = (
                f"arn:aws:cloudwatch:{REGION}:{ACCOUNT_ID}:alarm:{alarm_name}"
            )
            logger.info(f"[dry-run] 将创建 CloudWatch Alarm: {alarm_name}")
            alarm_arns.append(fake_arn)
            continue

        # 构建 put_metric_alarm 参数（剔除内部字段）
        params: dict = {
            "AlarmName":          alarm_cfg["AlarmName"],
            "AlarmDescription":   alarm_cfg.get("AlarmDescription", ""),
            "MetricName":         alarm_cfg["MetricName"],
            "Namespace":          alarm_cfg["Namespace"],
            "Statistic":          alarm_cfg["Statistic"],
            "Period":             alarm_cfg["Period"],
            "EvaluationPeriods":  alarm_cfg["EvaluationPeriods"],
            "Threshold":          alarm_cfg["Threshold"],
            "ComparisonOperator": alarm_cfg["ComparisonOperator"],
            "TreatMissingData":   alarm_cfg.get("TreatMissingData", "notBreaching"),
            "ActionsEnabled":     False,   # Stop Conditions 由 FIS 原生处理，无需 SNS
            "Tags": [
                {"Key": "chaos-automation", "Value": "true"},
                {"Key": "managed-by", "Value": "fis_setup.py"},
            ],
        }
        if "Dimensions" in alarm_cfg:
            params["Dimensions"] = alarm_cfg["Dimensions"]

        logger.info(f"创建 CloudWatch Alarm: {alarm_name}")
        cw.put_metric_alarm(**params)

        # 获取 ARN
        resp = cw.describe_alarms(AlarmNames=[alarm_name])
        arn  = resp["MetricAlarms"][0]["AlarmArn"]
        logger.info(f"✅ CloudWatch Alarm 创建成功: {arn}")
        alarm_arns.append(arn)

    return alarm_arns


def setup_fis_s3_bucket(s3: object, dry_run: bool = False) -> str:
    """
    创建 FIS Lambda Extension 通信所需的 S3 Bucket（幂等）。

    Returns:
        S3 Bucket 名称
    """
    try:
        s3.head_bucket(Bucket=FIS_S3_BUCKET)
        logger.info(f"✅ S3 Bucket 已存在，跳过: {FIS_S3_BUCKET}")
        return FIS_S3_BUCKET
    except ClientError as e:
        if e.response["Error"]["Code"] not in ("404", "NoSuchBucket"):
            raise

    if dry_run:
        logger.info(f"[dry-run] 将创建 S3 Bucket: {FIS_S3_BUCKET}")
        return FIS_S3_BUCKET

    logger.info(f"创建 S3 Bucket: {FIS_S3_BUCKET}")
    if REGION == "us-east-1":
        s3.create_bucket(Bucket=FIS_S3_BUCKET)
    else:
        s3.create_bucket(
            Bucket=FIS_S3_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )

    # 阻止公共访问
    s3.put_public_access_block(
        Bucket=FIS_S3_BUCKET,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls":       True,
            "IgnorePublicAcls":      True,
            "BlockPublicPolicy":     True,
            "RestrictPublicBuckets": True,
        },
    )
    logger.info(f"✅ S3 Bucket 创建成功（已阻止公共访问）: {FIS_S3_BUCKET}")
    return FIS_S3_BUCKET


def run_setup(dry_run: bool = False):
    """
    执行完整的 FIS 基础设施配置。
    幂等：可安全重复执行。
    """
    print(f"\n{'='*60}")
    print(f"FIS 基础设施配置{'（dry-run 模式，不会实际创建资源）' if dry_run else ''}")
    print(f"Region: {REGION} | Account: {ACCOUNT_ID}")
    print(f"{'='*60}\n")

    iam = boto3.client("iam",            region_name=REGION)
    cw  = boto3.client("cloudwatch",     region_name=REGION)
    s3  = boto3.client("s3",             region_name=REGION)

    # 1. FIS IAM Role
    print("1️⃣  配置 FIS IAM Role...")
    role_arn = setup_fis_role(iam, dry_run=dry_run)

    # 2. CloudWatch Alarms
    print("\n2️⃣  配置 CloudWatch Alarms（FIS Stop Conditions）...")
    alarm_arns = setup_cloudwatch_alarms(cw, dry_run=dry_run)

    # 3. S3 Bucket（Lambda Extension 通信）
    print("\n3️⃣  配置 FIS Lambda Extension S3 Bucket...")
    bucket = setup_fis_s3_bucket(s3, dry_run=dry_run)

    # 输出汇总
    print(f"\n{'='*60}")
    print("✅ FIS 基础设施配置完成")
    print(f"\nIAM Role ARN:  {role_arn}")
    print(f"S3 Bucket:     {bucket}")
    print(f"\nCloudWatch Alarm ARNs（FIS Stop Conditions 配置用）：")
    for arn in alarm_arns:
        alarm_name = arn.split(":")[-1]
        print(f"  {alarm_name}:")
        print(f"    {arn}")

    print(f"\n{'='*60}")
    print("⚠️  下一步（手动）：")
    print("1. Lambda 函数需添加 FIS Extension Layer（见 TDD 3.3d）")
    print("2. Lambda 函数配置环境变量 AWS_FIS_CONFIGURATION_LOCATION=s3://{bucket}/")
    print(f"   s3://{bucket}/")
    print("3. 将上方 Alarm ARN 填入对应实验 YAML 的 stop_conditions[].cloudwatch_alarm_arn")
    print(f"{'='*60}\n")


# ─── CLI 入口 ─────────────────────────────────────────────────────────────────

def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="FIS 基础设施配置脚本（一次性执行，幂等）"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="仅检查现有资源，不创建任何新资源",
    )
    args = parser.parse_args()

    _setup_logging()

    try:
        run_setup(dry_run=args.dry_run)
    except Exception as e:
        logger.error(f"配置失败: {e}")
        sys.exit(1)
