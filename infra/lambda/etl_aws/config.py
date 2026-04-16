"""
config.py - Configuration constants and business mappings for neptune-etl-from-aws.

All environment-specific values are read from Lambda environment variables.
PetSite business topology is loaded from business_config.json at cold-start.
"""

import os
import json

# ===== 核心配置 =====
NEPTUNE_ENDPOINT = os.environ.get('NEPTUNE_ENDPOINT', 'YOUR_NEPTUNE_ENDPOINT')
NEPTUNE_PORT = int(os.environ.get('NEPTUNE_PORT', '8182'))
REGION = os.environ.get('REGION', 'YOUR_AWS_REGION')
EKS_CLUSTER_NAME = os.environ.get('EKS_CLUSTER_NAME', 'YOUR_EKS_CLUSTER_NAME')
ENVIRONMENT = os.environ.get('ENVIRONMENT', 'prod')

# ===== AWS 服务故障边界（Fault Boundary）=====
FAULT_BOUNDARY_MAP = {
    'EC2Instance':      ('az',     None),
    'RDSInstance':      ('az',     None),
    'Subnet':           ('az',     None),
    'AvailabilityZone': ('az',     None),
    'LambdaFunction':   ('region', REGION),
    'EKSCluster':       ('region', REGION),
    'LoadBalancer':     ('region', REGION),
    'DynamoDBTable':    ('region', REGION),
    'SQSQueue':         ('region', REGION),
    'SNSTopic':         ('region', REGION),
    'S3Bucket':         ('region', REGION),
    'ECRRepository':    ('region', REGION),
    'StepFunction':     ('region', REGION),
    'RDSCluster':       ('region', REGION),
    'NeptuneCluster':   ('region', REGION),
    'NeptuneInstance':  ('az',     None),
    'Region':           ('region', REGION),
}

# ===== 采集过滤規則（infrastructure-level, not business-specific）=====
SKIP_SG_VPCS = frozenset(os.environ.get('SKIP_SG_VPCS', '').split(',')) if os.environ.get('SKIP_SG_VPCS') else frozenset()
SKIP_TG_PREFIXES = ('openclaw-',)

CDK_LAMBDA_SKIP_PREFIXES = (
    'Applications-',
    'cwsyn-',
    'openclaw-',
    'neptune-etl-from-cfn',
)
CDK_LAMBDA_SKIP_KEYWORDS = (
    'awscdkawseks',
    'AWSCDKCfnUtils',
    'CustomAWSCDKOpenIdConnect',
    'CustomCDKBucketDeployment',
    'CustomS3AutoDeleteObjects',
    'ProviderframeworkonEvent',
    'ProviderframeworkisCompl',
    'ProviderframeworkonTimeo',
    'IsCompleteHandler',
    'AWS679f53fac002430cb',
)

# ===== Business config (loaded from YAML) =====
_here = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_here, 'business_config.json'), 'r') as _f:
    _bc = json.load(_f)

MICROSERVICE_RECOVERY_PRIORITY: dict = _bc.get('microservice_recovery_priority', {})
LAMBDA_RECOVERY_PRIORITY: dict       = _bc.get('lambda_recovery_priority', {})
EC2_RECOVERY_PRIORITY: dict          = _bc.get('ec2_recovery_priority', {})
K8S_SERVICE_ALIAS: dict              = _bc.get('k8s_service_alias', {})
BUSINESS_CAPABILITIES: list          = _bc.get('business_capabilities', [])
MICROSERVICE_INFRA_DEPS: dict        = _bc.get('microservice_infra_deps', {})
# 服务 namespace 映射：从 service_mappings.json 加载（必须存在）
# 部署前必须运行：python3 scripts/generate_service_mappings.py
_sm_path = os.path.join(os.path.dirname(__file__), 'service_mappings.json')
if not os.path.exists(_sm_path):
    raise RuntimeError(
        f"service_mappings.json not found at {_sm_path}. "
        "Run 'python3 scripts/generate_service_mappings.py' before CDK deploy."
    )
with open(_sm_path, encoding='utf-8') as _smf:
    _sm_data = json.load(_smf)
_sm_ns_val = _sm_data['namespace']
# 为 tier_map 中所有服务生成 namespace 映射
_sm_ns = {_sn: _sm_ns_val for _sn in _sm_data['tier_map']}
# awesomeshop 服务仍保留硬编码（不在 profile 中）
_sm_ns.update({
    'auth-service': 'awesomeshop', 'gateway-service': 'awesomeshop',
    'order-service': 'awesomeshop', 'points-service': 'awesomeshop',
    'product-service': 'awesomeshop', 'frontend': 'awesomeshop',
})
MICROSERVICE_NAMESPACE: dict         = _bc.get('microservice_namespace', _sm_ns)
SERVICE_DB_MAPPING: list             = _bc.get('service_db_mapping', [])
TG_APP_LABEL_STATIC: dict            = _bc.get('tg_app_label_static', {})
INFRA_DRIFT_RULES: dict              = _bc.get('infra_drift_rules', {})

# OPS_TOOL_EDGES: list of tuples [(src_label, src_name, edge, dst_label, dst_name)]
OPS_TOOL_EDGES: list = [tuple(e) for e in _bc.get('ops_tool_edges', [])]
