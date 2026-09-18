"""为 AgentCore 的 InvokeAgentRuntime 开启 CloudTrail 数据事件。

## 为什么需要它

Application Signals 已经能给出 `PetSite -> AWS::BedrockAgentCore` 的观测边
（2026-09-05 实测，合成流量上线 <1h 就出现），但它对 AWS 托管服务只到
**服务级** —— 拿不到"调的是哪个 runtime"。

CloudTrail 数据事件补的正是这块：官方文档明确数据面事件的
`resources.ARN` 装的是 **runtime ARN**，`userIdentity` 装调用方。
所以它是唯一能给出 **runtime 级 observed 调用方** 的源。

## 为什么新建 trail 而不是改现有的

账号里唯一的 trail 是 `IsengardTrail-DO-NOT-DELETE` —— 账号管理的托管 trail。
改它的事件选择器会影响账号审计，不能碰。

## 成本

数据事件 $0.10 / 100,000 个。AgentCore 实测量级约 500 次/天 ≈ 15k/月，
成本可忽略。**刻意只开 AgentCore 这一类** —— 不开 S3/Lambda 数据事件，
那两类才是会烧钱的（一个繁忙的 S3 桶一天就能几百万事件）。
"""

import json
import sys

import boto3

REGION = 'ap-northeast-1'
sts = boto3.client('sts', region_name=REGION)
ACCT = sts.get_caller_identity()['Account']
BUCKET = f'agentcore-dataevents-trail-{ACCT}-{REGION}'
TRAIL = 'agentcore-invoke-dataevents'

s3 = boto3.client('s3', region_name=REGION)
ct = boto3.client('cloudtrail', region_name=REGION)


def ensure_bucket() -> None:
    try:
        s3.head_bucket(Bucket=BUCKET)
        print(f'  桶已存在: {BUCKET}')
        return
    except Exception:
        pass
    s3.create_bucket(Bucket=BUCKET,
                     CreateBucketConfiguration={'LocationConstraint': REGION})
    # 阻断公开访问 —— trail 日志含调用方身份，绝不能公开
    s3.put_public_access_block(
        Bucket=BUCKET,
        PublicAccessBlockConfiguration={
            'BlockPublicAcls': True, 'IgnorePublicAcls': True,
            'BlockPublicPolicy': True, 'RestrictPublicBuckets': True})
    s3.put_bucket_encryption(
        Bucket=BUCKET,
        ServerSideEncryptionConfiguration={'Rules': [
            {'ApplyServerSideEncryptionByDefault': {'SSEAlgorithm': 'AES256'}}]})
    # 90 天过期：这是证据源不是归档，图谱只看最近窗口
    s3.put_bucket_lifecycle_configuration(
        Bucket=BUCKET,
        LifecycleConfiguration={'Rules': [{
            'ID': 'expire-90d', 'Status': 'Enabled',
            'Filter': {'Prefix': ''}, 'Expiration': {'Days': 90}}]})
    print(f'  已建桶: {BUCKET}（已阻断公开访问 + AES256 + 90 天过期）')


def ensure_policy() -> None:
    trail_arn = f'arn:aws:cloudtrail:{REGION}:{ACCT}:trail/{TRAIL}'
    pol = {
        'Version': '2012-10-17',
        'Statement': [
            {'Sid': 'AWSCloudTrailAclCheck', 'Effect': 'Allow',
             'Principal': {'Service': 'cloudtrail.amazonaws.com'},
             'Action': 's3:GetBucketAcl', 'Resource': f'arn:aws:s3:::{BUCKET}',
             'Condition': {'StringEquals': {'aws:SourceArn': trail_arn}}},
            {'Sid': 'AWSCloudTrailWrite', 'Effect': 'Allow',
             'Principal': {'Service': 'cloudtrail.amazonaws.com'},
             'Action': 's3:PutObject',
             'Resource': f'arn:aws:s3:::{BUCKET}/AWSLogs/{ACCT}/*',
             'Condition': {'StringEquals': {
                 's3:x-amz-acl': 'bucket-owner-full-control',
                 'aws:SourceArn': trail_arn}}},
        ]}
    s3.put_bucket_policy(Bucket=BUCKET, Policy=json.dumps(pol))
    print('  桶策略已设（限定 aws:SourceArn 到本 trail，防混淆代理）')


def ensure_trail() -> str:
    try:
        d = ct.get_trail(Name=TRAIL)['Trail']
        print(f"  trail 已存在: {d['Name']}")
        return d['TrailARN']
    except ct.exceptions.TrailNotFoundException:
        pass
    d = ct.create_trail(Name=TRAIL, S3BucketName=BUCKET,
                        IsMultiRegionTrail=False,
                        IncludeGlobalServiceEvents=False,
                        EnableLogFileValidation=True)
    print(f"  已建 trail: {d['Name']}")
    return d['TrailARN']


def ensure_selectors() -> None:
    # 只要 AgentCore runtime 的数据事件。刻意**不含管理事件** ——
    # IsengardTrail 已经在记管理事件，重复记只是多花钱。
    ct.put_event_selectors(
        TrailName=TRAIL,
        AdvancedEventSelectors=[{
            'Name': 'AgentCore runtime data events only',
            'FieldSelectors': [
                {'Field': 'eventCategory', 'Equals': ['Data']},
                {'Field': 'resources.type',
                 'Equals': ['AWS::BedrockAgentCore::Runtime']},
            ]}])
    print('  事件选择器已设：仅 AWS::BedrockAgentCore::Runtime 的 Data 事件')


def main() -> int:
    print(f'账号 {ACCT} / 区域 {REGION}')
    ensure_bucket()
    # 顺序不可颠倒：CreateTrail 会校验桶策略，策略不到位直接报
    # InsufficientS3BucketPolicyException。而策略里引用的 trail ARN 是
    # 确定性的（区域+账号+名字推出），不需要 trail 先存在 —— 所以先设策略。
    ensure_policy()
    ensure_trail()
    ensure_selectors()
    ct.start_logging(Name=TRAIL)
    st = ct.get_trail_status(Name=TRAIL)
    print(f"\n最终状态: IsLogging={st.get('IsLogging')}")
    sel = ct.get_event_selectors(TrailName=TRAIL)
    print('生效的选择器:',
          json.dumps(sel.get('AdvancedEventSelectors'), ensure_ascii=False))
    print('\n注意：数据事件从现在起才记录，历史调用不会回溯。'
          '首批日志投递到 S3 通常需 5–15 分钟。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
