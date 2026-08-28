"""
neptune_etl_deepflow.py - ClickHouse L7 flow_log → Neptune 服务调用图

Lambda: neptune-etl-from-deepflow
触发频率: 每5分钟
优化: 批量写入 + 合并查询，~700次请求降至~20-30次

─────────────────────────────────────────────────────────────────────────────
Neptune 边属性命名约定（Field Naming Convention）
─────────────────────────────────────────────────────────────────────────────
本 ETL 写入的所有字段均为"系统观测/静态配置"类属性，无特殊前缀：
  call_type, error_rate, p99_latency_ms, strength, ...

混沌工程实验（chaos-automation）写入的字段统一使用 chaos_ 前缀：
  chaos_dependency_type    ← 实验实测的依赖强弱（strong/weak/none/unverified）
  chaos_degradation_rate   ← 实验期间上游成功率下降幅度（%）
  chaos_recovery_time_seconds ← 下游恢复后上游恢复正常所需时间（秒）
  chaos_last_verified      ← 最近一次混沌验证时间 ISO8601
  chaos_verified_by        ← 验证该属性的实验 ID

规则：
  - ETL 只写非 chaos_ 字段，不覆盖 chaos_* 字段
  - chaos-automation 只写 chaos_* 字段，不覆盖 ETL 字段
  - 两套字段并存，允许对比"ETL 静态判断"vs"实验实测结论"
  - 若 DependsOn.strength=strong 但 chaos_dependency_type=weak/none，说明 ETL 判断偏保守，需人工复核

详细设计见: docs/chaos-tdd.md § 4.3（本地参考）
─────────────────────────────────────────────────────────────────────────────
"""

import os
import json
import time
import logging
import base64
import boto3

from neptune_client_base import neptune_query, extract_value, REGION  # noqa: F401

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# ===== 配置 =====
CH_HOST = os.environ.get('CLICKHOUSE_HOST', os.environ.get('CH_HOST', 'YOUR_CLICKHOUSE_HOST'))
CH_PORT = int(os.environ.get('CLICKHOUSE_PORT', os.environ.get('CH_PORT', '8123')))
INTERVAL_MIN = int(os.environ.get('INTERVAL_MIN', '6'))
EKS_CLUSTER_ARN = os.environ.get('EKS_CLUSTER_ARN',
    'arn:aws:eks:YOUR_REGION:YOUR_ACCOUNT_ID:cluster/YOUR_EKS_CLUSTER_NAME')
BATCH_SIZE = int(os.environ.get('BATCH_SIZE', '20'))
ENVIRONMENT = os.environ.get('ENVIRONMENT', 'prod')

# 只采集这些 namespace 的 pod（从 service_mappings.json 读 + 默认 namespace）
# deepflow、kube-system 等监控/基础设施 namespace 不纳入图，避免形成孤立分量
INCLUDED_NAMESPACES = {'default', 'awesomeshop'}

# ===== 服务映射（从 service_mappings.json 加载，由 profiles/petsite.yaml 生成） =====
# 部署前必须运行：python3 scripts/generate_service_mappings.py
_MAPPINGS_PATH = os.path.join(os.path.dirname(__file__), 'service_mappings.json')
if not os.path.exists(_MAPPINGS_PATH):
    raise RuntimeError(
        f"service_mappings.json not found at {_MAPPINGS_PATH}. "
        "Run 'python3 scripts/generate_service_mappings.py' before CDK deploy."
    )
with open(_MAPPINGS_PATH, encoding='utf-8') as _f:
    _MAPPINGS = json.loads(_f.read())
MICROSERVICE_RECOVERY_PRIORITY = _MAPPINGS['tier_map']
K8S_SERVICE_ALIAS = _MAPPINGS['k8s_alias']
SERVICE_TYPES = _MAPPINGS.get('service_types', {})
# 从 profile 读主业务 namespace，并并入 INCLUDED_NAMESPACES
_primary_ns = _MAPPINGS.get('namespace', 'default')
if _primary_ns:
    INCLUDED_NAMESPACES.add(_primary_ns)


def get_aws_session():
    return boto3.Session(region_name=REGION)

# ===== ClickHouse 查询 =====

def ch_query(sql: str) -> list:
    import requests
    try:
        r = requests.post(f"http://{CH_HOST}:{CH_PORT}/", data=sql, timeout=30)
        if r.status_code != 200:
            raise Exception(f"ClickHouse error {r.status_code}: {r.text[:200]}")
        return [line.split('\t') for line in r.text.strip().split('\n') if line.strip()]
    except Exception as e:
        logger.error(f"ClickHouse query failed: {e}")
        return []

def ch_query_json(sql: str) -> dict:
    import requests
    try:
        r = requests.post(f"http://{CH_HOST}:{CH_PORT}/", data=sql + ' FORMAT JSON', timeout=30)
        if r.status_code != 200:
            raise Exception(f"ClickHouse error {r.status_code}: {r.text[:200]}")
        return r.json()
    except Exception as e:
        logger.error(f"ClickHouse JSON query failed: {e}")
        return {}

# ===== L7 性能指标 =====

def fetch_l7_metrics() -> dict:
    # 注意：DeepFlow l7_flow_log 没有 server_ip 字段，服务端 IP 是 ip4_1
    # response_status 是枚举(0=正常,1=异常,2=不存在,3=服务端异常,4=客户端异常)，不是 HTTP 状态码
    sql = """
SELECT IPv4NumToString(ip4_1) AS server_ip,
    quantile(0.5)(response_duration)/1000 AS p50_latency_ms,
    quantile(0.99)(response_duration)/1000 AS p99_latency_ms,
    count()/300 AS rps,
    countIf(response_status >= 1) / count() AS error_rate
FROM flow_log.l7_flow_log
WHERE toUnixTimestamp(time) > toUnixTimestamp(now()) - 300
    AND response_duration > 0 AND ip4_1 != 0
GROUP BY server_ip
"""
    result = {}
    try:
        data = ch_query_json(sql)
        for row in data.get('data', []):
            ip = row.get('server_ip', '')
            if ip:
                result[ip] = {
                    'p50_latency_ms': float(row.get('p50_latency_ms', -1) or -1),
                    'p99_latency_ms': float(row.get('p99_latency_ms', -1) or -1),
                    'rps': float(row.get('rps', -1) or -1),
                    'error_rate': float(row.get('error_rate', -1) or -1),
                }
        logger.info(f"L7 metrics fetched for {len(result)} IPs")
    except Exception as e:
        logger.error(f"fetch_l7_metrics failed: {e}")
    return result


def fetch_nfm_throttling(ip_map: dict) -> dict:
    """
    查询 prometheus.samples 最近5分钟内各 pod 的 NFM 限速指标
    多副本同服务取 OR（任一 pod 被限速，服务即标记）
    返回 {svc_name: {'nfm_bw_throttled': bool, 'nfm_pps_throttled': bool, 'nfm_conntrack_throttled': bool}}
    """
    sql = """
SELECT
    IPv4NumToString(ip4) AS pod_ip,
    sumIf(value, m.name IN ('bw_in_allowance_exceeded', 'bw_out_allowance_exceeded')) AS bw_exceeded,
    sumIf(value, m.name = 'pps_allowance_exceeded') AS pps_exceeded,
    sumIf(value, m.name = 'conntrack_allowance_exceeded') AS conntrack_exceeded
FROM prometheus.samples s
JOIN flow_tag.prometheus_metric_name_map m ON s.metric_id = m.id
WHERE m.name IN ('bw_in_allowance_exceeded', 'bw_out_allowance_exceeded',
                 'pps_allowance_exceeded', 'conntrack_allowance_exceeded')
  AND s.time > now() - INTERVAL 5 MINUTE
  AND s.ip4 != 0
GROUP BY pod_ip
"""
    result = {}
    throttled_count = 0
    try:
        data = ch_query_json(sql)
        for row in data.get('data', []):
            pod_ip = row.get('pod_ip', '')
            info = ip_map.get(pod_ip)
            if not info:
                continue
            svc_name = info['name']
            bw  = float(row.get('bw_exceeded',       0) or 0) > 0
            pps = float(row.get('pps_exceeded',       0) or 0) > 0
            ct  = float(row.get('conntrack_exceeded', 0) or 0) > 0
            if svc_name not in result:
                result[svc_name] = {
                    'nfm_bw_throttled':       False,
                    'nfm_pps_throttled':      False,
                    'nfm_conntrack_throttled': False,
                }
            result[svc_name]['nfm_bw_throttled']       |= bw
            result[svc_name]['nfm_pps_throttled']      |= pps
            result[svc_name]['nfm_conntrack_throttled'] |= ct
            if bw or pps or ct:
                throttled_count += 1
        logger.info(f"NFM throttling: {throttled_count} throttled pod(s) across {len(result)} service(s)")
    except Exception as e:
        logger.error(f"fetch_nfm_throttling failed: {e}")
    return result


def fetch_active_connections() -> dict:
    """从 network_map.1m 查 TCP 连接数（用 syn_count 代理活跃连接）"""
    sql = """
SELECT IPv4NumToString(ip4_1) AS server_ip,
    sum(syn_count) AS active_connections
FROM `flow_metrics`.`network_map.1m`
WHERE toUnixTimestamp(time) > toUnixTimestamp(now()) - 300
    AND ip4_1 != 0 AND protocol = 6
GROUP BY server_ip
"""
    result = {}
    try:
        data = ch_query_json(sql)
        for row in data.get('data', []):
            ip = row.get('server_ip', '')
            if ip:
                result[ip] = int(float(row.get('active_connections', 0) or 0))
        logger.info(f"active_connections fetched for {len(result)} IPs")
    except Exception as e:
        logger.error(f"fetch_active_connections failed: {e}")
    return result


# ===== T13a: DNS/TCP 连接级漂移检测 =====
# 原理：EKS pod 访问 AWS 托管服务前必然有 DNS 查询 (*.amazonaws.com)
# DeepFlow l7_flow_log 中 l7_protocol_str='DNS' 记录了这些查询
# 对比 Neptune 中代码声明的 AccessesData/PublishesTo/InvokesVia 边，写入 drift_status

# infra 类型 → (Neptune 标签, name_contains, [dns_keywords_any_match])
# 一组 dns_keywords 中任意一个出现在 observed DNS 里，即认为 runtime_verified
INFRA_DRIFT_RULES = {
    'dynamodb': {
        'label': 'DynamoDBTable',
        'nc': 'ddbpetadoption',
        'dns_keywords': ['dynamodb', '.ddb.'],  # {account}.ddb.{region}.amazonaws.com 或 dynamodb.*
    },
    'sqs': {
        'label': 'SQSQueue',
        'nc': 'sqspetadoption',
        'dns_keywords': ['.sqs.'],
    },
    'sns': {
        'label': 'SNSTopic',
        'nc': 'topicpetadoption',
        'dns_keywords': ['.sns.'],
    },
    'rds': {
        'label': 'RDSCluster',
        'nc': 'databaseb269d8bb',
        'dns_keywords': ['rds.', 'aurora'],
    },
    's3': {
        'label': 'S3Bucket',
        'nc': 's3bucketpetadoption',
        'dns_keywords': ['.s3.', 's3.amazon'],
    },
    'stepfunction': {
        'label': 'StepFunction',
        'nc': 'StepFnStateMachine',
        'dns_keywords': ['states.'],
    },
}

# 旧结构保留兼容（fetch_dns_connections 用这个做 keyword→infra_type 映射）
DNS_TO_INFRA = {}
for _itype, _rule in INFRA_DRIFT_RULES.items():
    for _kw in _rule['dns_keywords']:
        DNS_TO_INFRA[_kw] = (_rule['label'], [_rule['nc']], _itype)


def fetch_dns_connections(ip_map: dict) -> dict:
    """
    查询 DeepFlow DNS 流量，返回各服务实际连接的 AWS 服务类型集合
    返回：{svc_name: {'dynamodb', 'sqs', 'rds', ...}}
    """
    # ip_map 格式: {pod_ip: {'name': svc_name, 'namespace': ..., ...}}
    # 注意：name 可能是 K8s 原名（pay-for-adoption），需过 alias 转换为 Neptune 名（payforadoption）
    ip_to_svc = {
        ip: K8S_SERVICE_ALIAS.get(info.get('name',''), info.get('name',''))
        for ip, info in ip_map.items() if info.get('name')
    }

    if not ip_to_svc:
        logger.info("DNS drift: empty ip_map, skipping")
        return {}

    # 查询最近 5 分钟内来自 EKS pod 的 DNS 请求（l7_protocol_str=DNS）
    # request_domain 字段包含 DNS 查询的域名
    # 用 position() 代替 LIKE（避免 ClickHouse 的 % 转义问题）
    sql = """
SELECT IPv4NumToString(ip4_0) AS src_ip,
       request_domain,
       count() AS query_count
FROM flow_log.l7_flow_log
WHERE toUnixTimestamp(time) > toUnixTimestamp(now()) - 1800
  AND l7_protocol_str = 'DNS'
  AND ip4_0 != 0
  AND (position(request_domain, 'dynamodb') > 0
    OR position(request_domain, '.ddb.') > 0
    OR position(request_domain, '.sqs.') > 0
    OR position(request_domain, '.sns.') > 0
    OR position(request_domain, '.s3.') > 0
    OR position(request_domain, 'rds.') > 0
    OR position(request_domain, 'aurora') > 0
    OR position(request_domain, 'states.') > 0)
GROUP BY src_ip, request_domain
HAVING query_count >= 1
"""
    result = {}
    try:
        import json as _json
        data = ch_query_json(sql)
        rows = data.get('data', [])
        if not rows:
            logger.info("DNS drift: no AWS DNS flows in last 5min")
            return {}
        for row in rows:
            src_ip = row.get('src_ip', '')
            domain = row.get('request_domain', '').lower()
            svc = ip_to_svc.get(src_ip)
            if not svc or not domain:
                continue
            if svc not in result:
                result[svc] = set()
            for _kw, (_lbl, _nc_list, _itype) in DNS_TO_INFRA.items():
                if _kw in domain:
                    result[svc].add(_kw)   # 保留原始 keyword（drift loop 用 any()）
                    break
        logger.info(f"DNS drift: {len(result)} services with AWS DNS queries: {dict((k,list(v)) for k,v in result.items())}")
    except Exception as e:
        logger.warning(f"fetch_dns_connections failed (non-fatal): {e}")
    return result


def run_drift_detection(service_names: list, ip_map: dict):
    """
    T13a: 对比代码声明边 vs DNS/TCP 运行时观测，写入 drift_status
    - 代码声明了但没有 DNS → drift_status=declared_not_observed
    - DNS 有但代码没声明 → drift_status=observed_not_declared（写新边）
    - 两者都有 → runtime_verified=true, drift_status=ok
    """
    ts = int(time.time())
    dns_obs = fetch_dns_connections(ip_map)

    if not dns_obs:
        logger.info("Drift detection: no DNS data, skipping")
        return

    drift_summary = {'ok': 0, 'declared_not_observed': 0, 'observed_not_declared': 0}

    for svc_name in service_names:
        observed_keywords = dns_obs.get(svc_name, set())

        for infra_type, rule in INFRA_DRIFT_RULES.items():
            infra_label = rule['label']
            nc          = rule['nc']
            # 任一关键词匹配即为 observed（多 DNS endpoint 格式兼容）
            has_dns = any(kw in observed_keywords for kw in rule['dns_keywords'])

            # 查 Neptune 中是否有代码声明边
            try:
                r = neptune_query(
                    f"g.V().hasLabel('Microservice','LambdaFunction').has('name',containing('{svc_name}'))"
                    f".outE('AccessesData','PublishesTo','InvokesVia','ConsumesFrom')"
                    f".where(inV().hasLabel('{infra_label}').has('name',containing('{nc}')))"
                    f".id().toList()"
                )
                edge_ids = r.get('result', {}).get('data', {}).get('@value', [])
                has_declared = len(edge_ids) > 0
            except Exception as e:
                logger.warning(f"drift query {svc_name}->{infra_label}: {e}")
                continue

            if has_declared and has_dns:
                # 两者一致：标记 runtime_verified=true, drift_status=ok
                drift_status = 'ok'
                for eid_raw in edge_ids:
                    eid = eid_raw.get('@value', eid_raw) if isinstance(eid_raw, dict) else eid_raw
                    try:
                        neptune_query(
                            f"g.E('{eid}')"
                            f".property('runtime_verified',true)"
                            f".property('drift_status','ok')"
                            f".property('last_drift_check',{ts})"
                        )
                    except Exception as e:
                        logger.warning(f"drift update edge {eid}: {e}")
                drift_summary['ok'] += 1

            elif has_declared and not has_dns:
                # 代码声明了但运行时没有 DNS → 可能死代码/环境问题
                drift_status = 'declared_not_observed'
                for eid_raw in edge_ids:
                    eid = eid_raw.get('@value', eid_raw) if isinstance(eid_raw, dict) else eid_raw
                    try:
                        neptune_query(
                            f"g.E('{eid}')"
                            f".property('runtime_verified',false)"
                            f".property('drift_status','declared_not_observed')"
                            f".property('last_drift_check',{ts})"
                        )
                    except Exception as e:
                        logger.warning(f"drift mark {eid}: {e}")
                drift_summary['declared_not_observed'] += 1
                logger.warning(f"DRIFT: {svc_name} -declared-> {infra_label}({nc}) but no DNS observed")

            elif not has_declared and has_dns:
                # 运行时有 DNS 但代码没声明 → 影子依赖，写新边
                drift_status = 'observed_not_declared'
                try:
                    r2 = neptune_query(
                        f"g.V().hasLabel('Microservice','LambdaFunction').has('name',containing('{svc_name}')).as('src')"
                        f".V().hasLabel('{infra_label}').has('name',containing('{nc}'))"
                        f".coalesce("
                        f"  __.inE('AccessesData').where(outV().has('name',containing('{svc_name}'))),"
                        f"  __.addE('AccessesData').from('src')"
                        f").property('source','deepflow-dns')"
                        # DNS 观测得来 → dynamic（与 etl_aws/etl_cfn 声明的 static 相对）
                        f".property('dependency_kind','dynamic')"
                        f".property('runtime_verified',true)"
                        f".property('drift_status','observed_not_declared')"
                        f".property('last_drift_check',{ts})"
                    )
                    drift_summary['observed_not_declared'] += 1
                    logger.warning(f"DRIFT: {svc_name} -DNS-> {infra_label}({nc}) but NOT declared in code")
                except Exception as e:
                    logger.warning(f"drift new edge {svc_name}->{infra_label}: {e}")

    logger.info(f"Drift detection complete: {drift_summary}")

# ===== EKS Token & IP 映射 =====

def get_eks_token(cluster_name: str) -> str:
    try:
        import botocore
        from botocore.signers import RequestSigner
        session = get_aws_session()
        signer = RequestSigner(
            botocore.model.ServiceId('sts'), REGION, 'sts', 'v4',
            session.get_credentials(), session.events,
        )
        params = {
            'method': 'GET',
            'url': f'https://sts.{REGION}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15',
            'body': {}, 'headers': {'x-k8s-aws-id': cluster_name}, 'context': {},
        }
        signed = signer.generate_presigned_url(params, region_name=REGION, expires_in=60, operation_name='')
        return 'k8s-aws-v1.' + base64.urlsafe_b64encode(signed.encode()).decode().rstrip('=')
    except Exception as e:
        logger.warning(f"Failed to get EKS token: {e}")
        return ''

def _get_eks_k8s_session() -> tuple:
    """建立 EKS/K8s 连接，返回 (k8s_endpoint, token, ca_file)，失败返回 (None, None, None)"""
    try:
        session = get_aws_session()
        eks_client = session.client('eks', region_name=REGION)
        cluster_name = EKS_CLUSTER_ARN.split('/')[-1]
        cluster_info = eks_client.describe_cluster(name=cluster_name)
        k8s_endpoint = cluster_info['cluster']['endpoint']
        ca_data = cluster_info['cluster']['certificateAuthority']['data']
        token = get_eks_token(cluster_name)
        if not token:
            return None, None, None
        import tempfile
        ca_bytes = base64.b64decode(ca_data)
        with tempfile.NamedTemporaryFile(suffix='.crt', delete=False) as f:
            f.write(ca_bytes)
            ca_file = f.name
        return k8s_endpoint, token, ca_file
    except Exception as e:
        logger.warning(f"EKS session setup failed: {e}")
        return None, None, None

def build_ip_service_map() -> tuple:
    """返回 (ip_map, ecr_dep_map, restart_map)
    ip_map:      {pod_ip: {'name':..., 'namespace':..., 'type':..., 'az':...}}
    ecr_dep_map: {svc_name: set(ecr_repo_names)}  ← 启动依赖
    restart_map: {svc_name: max_restart_count}     ← 重启次数（各容器最大值）
    """
    ip_map = {}
    ecr_dep_map = {}
    restart_map = {}
    import requests
    k8s_endpoint, token, ca_file = _get_eks_k8s_session()
    if not k8s_endpoint:
        return ip_map, ecr_dep_map, restart_map
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    try:
        # Step 1: node_name → AZ 映射
        node_az_map = {}
        try:
            nodes_resp = requests.get(
                f"{k8s_endpoint}/api/v1/nodes",
                headers=headers, verify=ca_file, timeout=15
            )
            if nodes_resp.status_code == 200:
                for node in nodes_resp.json().get('items', []):
                    node_name = node.get('metadata', {}).get('name', '')
                    labels = node.get('metadata', {}).get('labels', {})
                    az = (
                        labels.get('topology.kubernetes.io/zone') or
                        labels.get('failure-domain.beta.kubernetes.io/zone', '')
                    )
                    if node_name and az:
                        node_az_map[node_name] = az
            logger.info(f"K8s node→AZ map: {len(node_az_map)} nodes, AZs: {set(node_az_map.values())}")
        except Exception as e:
            logger.warning(f"K8s nodes AZ map failed: {e}")

        # Step 2: 查 pods，带 nodeName 解析 AZ
        resp = requests.get(
            f"{k8s_endpoint}/api/v1/pods",
            headers=headers, verify=ca_file, timeout=15
        )
        if resp.status_code == 200:
            for item in resp.json().get('items', []):
                pod_ip = item.get('status', {}).get('podIP', '')
                ns = item.get('metadata', {}).get('namespace', 'default')
                if ns not in INCLUDED_NAMESPACES:
                    continue  # 跳过 deepflow / kube-system 等非业务 namespace
                labels = item.get('metadata', {}).get('labels', {})
                pod_name = item.get('metadata', {}).get('name', '')
                node_name = item.get('spec', {}).get('nodeName', '')
                app_label = (
                    labels.get('app') or
                    labels.get('app.kubernetes.io/name') or
                    labels.get('name') or ''
                )
                svc_name = app_label if app_label else pod_name.rsplit('-', 2)[0]
                # 应用 K8s pod label → Neptune 微服务名别名映射
                svc_name = K8S_SERVICE_ALIAS.get(svc_name, svc_name)
                az = node_az_map.get(node_name, '')
                if pod_ip and svc_name:
                    ip_map[pod_ip] = {
                        'name': svc_name,
                        'namespace': ns,
                        'type': 'Microservice',
                        'az': az,
                    }
                # ECR 启动依赖：提取 private ECR 镜像（不含 public.ecr.aws）
                ecr_suffix = '.dkr.ecr.' + REGION + '.amazonaws.com/'
                if svc_name:
                    for ctr in (item.get('spec', {}).get('containers', []) +
                                item.get('spec', {}).get('initContainers', [])):
                        image = ctr.get('image', '')
                        if ecr_suffix in image:
                            repo = image.split(ecr_suffix)[-1].split(':')[0].split('@')[0]
                            ecr_dep_map.setdefault(svc_name, set()).add(repo)
                # pod 重启次数：取所有容器 restartCount 的最大值
                if svc_name:
                    ctr_statuses = item.get('status', {}).get('containerStatuses', [])
                    max_restart = max((cs.get('restartCount', 0) for cs in ctr_statuses), default=0)
                    restart_map[svc_name] = max(restart_map.get(svc_name, 0), max_restart)
        logger.info(f"IP→Service map: {len(ip_map)} entries (with AZ info)")
    finally:
        try:
            os.unlink(ca_file)
        except Exception:
            pass
    return ip_map, ecr_dep_map, restart_map

def fetch_resource_limits(ip_map: dict) -> dict:
    """从 K8s Deployments API 获取 resource limits，返回 {svc_name: {cpu, memory}}"""
    resource_limits = {}
    import requests
    k8s_endpoint, token, ca_file = _get_eks_k8s_session()
    if not k8s_endpoint:
        return resource_limits
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    try:
        resp = requests.get(f"{k8s_endpoint}/apis/apps/v1/deployments",
                            headers=headers, verify=ca_file, timeout=15)
        if resp.status_code == 200:
            for item in resp.json().get('items', []):
                labels = item.get('metadata', {}).get('labels', {})
                svc_name = (labels.get('app') or labels.get('app.kubernetes.io/name')
                            or labels.get('name') or item.get('metadata', {}).get('name', ''))
                svc_name = K8S_SERVICE_ALIAS.get(svc_name, svc_name)  # 统一别名
                containers = (item.get('spec', {}).get('template', {})
                              .get('spec', {}).get('containers', []))
                if containers and svc_name:
                    limits = containers[0].get('resources', {}).get('limits', {})
                    # 取 Progressing condition 的 lastUpdateTime 作为 last_deploy_time
                    conditions = item.get('status', {}).get('conditions', [])
                    last_update = next(
                        (c.get('lastUpdateTime', '') for c in conditions if c.get('type') == 'Progressing'),
                        ''
                    )
                    resource_limits[svc_name] = {
                        'cpu': limits.get('cpu', ''),
                        'memory': limits.get('memory', ''),
                        'last_deploy_time': last_update,
                    }
        logger.info(f"resource_limits fetched for {len(resource_limits)} services")
    except Exception as e:
        logger.warning(f"fetch_resource_limits failed: {e}")
    finally:
        try:
            if ca_file:
                os.unlink(ca_file)
        except Exception:
            pass
    return resource_limits


def fetch_replica_counts(ip_map: dict) -> dict:
    replica_map = {}
    import requests
    k8s_endpoint, token, ca_file = _get_eks_k8s_session()
    if not k8s_endpoint:
        return replica_map
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/json'}
    try:
        resp = requests.get(f"{k8s_endpoint}/apis/apps/v1/deployments", headers=headers, verify=ca_file, timeout=15)
        if resp.status_code == 200:
            for item in resp.json().get('items', []):
                labels = item.get('metadata', {}).get('labels', {})
                svc_name = (labels.get('app') or labels.get('app.kubernetes.io/name')
                            or labels.get('name') or item.get('metadata', {}).get('name', ''))
                ready = item.get('status', {}).get('readyReplicas', 0) or 0
                if svc_name:
                    replica_map[svc_name] = ready
    finally:
        try:
            os.unlink(ca_file)
        except Exception:
            pass
    return replica_map

# ===== 安全字符串 =====

def safe_str(s: str) -> str:
    return str(s).replace("'", "\\'").replace('"', '\\"')[:128]

# ===== 批量 Neptune 操作 =====

def batch_upsert_nodes(services: list):
    """批量 upsert Microservice 节点，每批 BATCH_SIZE 个"""
    for i in range(0, len(services), BATCH_SIZE):
        batch = services[i:i + BATCH_SIZE]
        # 用链式 mergeV 一次请求写多个节点
        parts = []
        for svc in batch:
            n, ns, ip = safe_str(svc['name']), safe_str(svc['namespace']), safe_str(svc['ip'])
            az = safe_str(svc.get('az', ''))
            priority = MICROSERVICE_RECOVERY_PRIORITY.get(n, 'Tier2')
            create_props = (
                f"'namespace': '{ns}', 'ip': '{ip}', 'source': 'deepflow', "
                f"'environment': '{ENVIRONMENT}', 'recovery_priority': '{priority}', "
                f"'fault_boundary': 'az', 'region': '{REGION}'"
            )
            match_props = (
                f"'ip': '{ip}', 'namespace': '{ns}', "
                f"'environment': '{ENVIRONMENT}', 'recovery_priority': '{priority}', "
                f"'fault_boundary': 'az'"
            )
            if az:
                create_props += f", 'az': '{az}'"
                # NOTE: az 不放入 match_props（list cardinality），改为后置 property(single) 更新
            parts.append(
                f"mergeV([(T.label): 'Microservice', 'name': '{n}'])"
                f".option(Merge.onCreate, [(T.label): 'Microservice', 'name': '{n}', "
                f"{create_props}])"
                f".option(Merge.onMatch, [{match_props}])"
            )
        gremlin = "g." + ".".join(parts)
        try:
            neptune_query(gremlin)
        except Exception as e:
            logger.error(f"batch_upsert_nodes failed (batch {i}): {e}")
            # 回退到逐条写入
            for svc in batch:
                try:
                    n, ns, ip = safe_str(svc['name']), safe_str(svc['namespace']), safe_str(svc['ip'])
                    priority = MICROSERVICE_RECOVERY_PRIORITY.get(n, 'Tier2')
                    neptune_query(
                        f"g.mergeV([(T.label): 'Microservice', 'name': '{n}'])"
                        f".option(Merge.onCreate, [(T.label): 'Microservice', 'name': '{n}', "
                        f"'namespace': '{ns}', 'ip': '{ip}', 'source': 'deepflow', "
                        f"'environment': '{ENVIRONMENT}', 'recovery_priority': '{priority}', "
                        f"'fault_boundary': 'az', 'region': '{REGION}'])"
                        f".option(Merge.onMatch, ['ip': '{ip}', 'namespace': '{ns}', "
                        f"'environment': '{ENVIRONMENT}', 'recovery_priority': '{priority}', "
                        f"'fault_boundary': 'az'])"
                    )
                except Exception as e2:
                    logger.error(f"single upsert node {svc['name']} failed: {e2}")

def batch_upsert_edges(edges: list):
    """批量 upsert Calls 边 — 直接用属性匹配，不再查 vertex ID"""
    ts = int(time.time())
    for e in edges:
        src, dst = safe_str(e['src']), safe_str(e['dst'])
        proto = safe_str(e['protocol'])
        calls = e['calls']
        errors = e['errors']
        error_rate = round(errors / calls, 4) if calls > 0 else 0.0
        p99 = float(e.get('p99_latency_ms', -1))
        gremlin = (
            f"g.V().has('Microservice','name','{src}').as('s')"
            f".V().has('Microservice','name','{dst}')"
            f".coalesce("
            f"  __.inE('Calls').where(__.outV().has('name','{src}')),"
            f"  __.addE('Calls').from('s')"
            f")"
            # first_seen 用幂等写法：已有值保留，缺失才写入当前 ts。
            # 不能放在 addE 分支之外裸写 .property('first_seen', ts) —— 那会被每轮
            # 刷新，「首次出现时间」退化成「最近一次时间」。
            # 也不用 last_seen 回填存量边：first_seen <= last_seen 恒成立，
            # 把 last_seen 当 first_seen 等于断言依赖"那时才出现"，比留空更糟。
            # 存量边的语义是「自埋点起首次观测到」，非真实首现时间，见 north_star DoD-1。
            f".property('first_seen', __.coalesce(__.values('first_seen'), __.constant({ts})))"
            f".property('protocol','{proto}')"
            f".property('port',{e['port']})"
            f".property('calls',{calls})"
            f".property('avg_latency_us',{e['avg_latency']:.0f})"
            f".property('p99_latency_ms',{p99:.4f})"
            f".property('error_count',{errors})"
            f".property('error_rate',{error_rate})"
            f".property('call_type','sync')"
            # source / dependency_kind：本 ETL 的 Calls 边来自 DeepFlow L7 **运行时观测**，
            # 与 etl_aws / etl_cfn 「声明」出来的静态依赖相对。
            # 此前 Calls 边**没有任何溯源标记**（实测 18 条边带 source 的 0 条），
            # 只能靠端点节点的 source='deepflow' 间接猜，无法在图上直接判定其性质。
            f".property('source','deepflow-etl')"
            f".property('dependency_kind','dynamic')"
            f".property('active',true)"
            f".property('last_seen',{ts})"
        )
        try:
            neptune_query(gremlin)
        except Exception as ex:
            logger.error(f"upsert edge {e['src']}->{e['dst']} failed: {ex}")

    return ts


# ─── Calls 边失效对账 ───────────────────────────────────────────────────────
#
# 背景：本 ETL 原先只 upsert，从不失效。`active` / `last_seen` 两个字段写了但
# 全仓库没有任何消费方，导致已消失的依赖永久留在图里且始终 active=true。
# 2026-08-28 实测：18 条 Calls 边里 17 条陈旧 2-5 个月却全部 active=true，
# 其中 gateway-service→auth-service 对应的 awesomeshop 命名空间 6 个 Deployment
# 副本数全为 0（服务已下线），图谱仍声称该依赖活跃并带 376 次调用量。
# 后果：RCA 把缩容到零的服务当作上游做根因推理。

# 多久未被观测到就标记 active=false。ETL 每 5 分钟一轮，默认 30 分钟 ≈ 6 轮，
# 留出容错窗口 —— 单轮 L7 查询偶发漏采不应立刻把活依赖判死。
INACTIVE_AFTER_SECONDS = int(os.environ.get('CALLS_INACTIVE_AFTER_SECONDS', '1800'))

# 硬删除阈值。**默认关闭**：删除是不可逆的生产数据变更，而软删除
# （active=false）已足以让图谱停止断言不存在的依赖，且保留历史可供追溯。
# 需要真正清理时显式设 CALLS_EDGE_DROP_ENABLED=true。
DROP_AFTER_SECONDS = int(os.environ.get('CALLS_DROP_AFTER_SECONDS', '604800'))
DROP_ENABLED = os.environ.get('CALLS_EDGE_DROP_ENABLED', 'false').lower() == 'true'

# ── 拓扑变更日志 ──────────────────────────────────────────────────────────────
# 「上周拓扑长什么样」此前完全无法回答:所有写入都是就地覆盖,无版本、无快照。
#
# 方案选择(三选一,详见 todo/goal-loop/tasks.md 的 T-030):
#   A 定期全量快照到 S3   —— 只答「T 时刻全貌」,答不了「变了什么」
#   B 双时态边 valid_from/valid_to —— **否决**:要改 6 个写入方,且现有 19 条
#     查询会静默返回被取代的旧版本,除非每条都加时间过滤。回归面太大,
#     且图规模按「边 × 变更频率」无界增长。
#   C 图内追加式变更事件日志 —— **采用**:纯追加,现有查询不动,
#     增长与**变更次数**成正比而非边数×时间。
#
# 为什么图谱侧的变更日志不可替代 CloudTrail:CloudTrail 记录的是 AWS API 级
# 变更(部署、实例停止、RDS 修改、扩缩容),它按构造**看不见**
#   · 依赖消失 —— A 不再调用 B,这不产生任何 AWS API 调用,是「流量缺席」
#   · 依赖出现 —— 应用内配置/开关导致 A 开始调 B
# 而对依赖图谱 RCA 因果上最相关的,恰恰是这两类。
#
# 保留期:否决方案 B 的理由之一是无界增长,所以自建的日志必须有上限。
CHANGE_LOG_ENABLED = os.environ.get('TOPOLOGY_CHANGE_LOG_ENABLED', 'true').lower() == 'true'
CHANGE_LOG_RETENTION_SECONDS = int(
    os.environ.get('TOPOLOGY_CHANGE_LOG_RETENTION_SECONDS', str(90 * 86400))
)


def _emit_topology_changes(events: list) -> int:
    """把拓扑变更事件写入图谱,返回成功写入条数。

    幂等键 change_id = f"{kind}:{subject}:{ts}" —— 同一轮重复调用不会重复建点。

    刻意用 `mergeV` 而非 `addV`:ETL 可能因重试被同一轮触发两次
    (SQS/EventBridge 均为 at-least-once),裸 addV 会留下重复事件,
    而变更日志一旦重复就没法用来做时序推断。

    Args:
        events: [{'kind','subject','source','target','edge_type','detail'}]

    Returns:
        写入条数(失败不抛,返回已成功的条数)
    """
    if not CHANGE_LOG_ENABLED or not events:
        return 0
    ts = int(time.time())
    written = 0
    for ev in events:
        kind = safe_str(ev.get('kind', 'unknown'))
        subject = safe_str(ev.get('subject', ''))
        cid = safe_str(f"{kind}:{subject}:{ts}")
        try:
            neptune_query(
                f"g.mergeV([(T.label):'TopologyChange','change_id':'{cid}'])"
                f".option(Merge.onCreate, ["
                f"  'ts': {ts},"
                f"  'kind': '{kind}',"
                f"  'subject': '{subject}',"
                f"  'edge_type': '{safe_str(ev.get('edge_type', ''))}',"
                f"  'source_name': '{safe_str(ev.get('source', ''))}',"
                f"  'target_name': '{safe_str(ev.get('target', ''))}',"
                f"  'detail': '{safe_str(ev.get('detail', ''))}',"
                f"  'provenance': 'deepflow-etl-reconcile'"
                f"]).iterate()"
            )
            written += 1
        except Exception as ex:
            # 变更日志是旁路,写失败不该影响本轮 upsert 的成果。
            # 但必须可见 —— 本项目历史上多数缺陷都是异常被吞导致的静默失败。
            logger.warning(f"拓扑变更事件写入失败 {cid}: {ex}")
    if written:
        logger.info(f"拓扑变更日志:写入 {written} 条事件")
    return written


def _prune_change_log(now_ts: int) -> int:
    """清理超过保留期的变更事件,返回删除条数。

    追加式日志必须有上限 —— 否则就重复了我否决方案 B 时批评的无界增长。
    """
    if not CHANGE_LOG_ENABLED:
        return 0
    cutoff = now_ts - CHANGE_LOG_RETENTION_SECONDS
    try:
        cnt = neptune_query(
            f"g.V().hasLabel('TopologyChange').has('ts', lt({cutoff})).count()"
        )
        n = _first_scalar(cnt)
        if n:
            neptune_query(
                f"g.V().hasLabel('TopologyChange').has('ts', lt({cutoff}))"
                f".drop().iterate()"
            )
            logger.info(f"拓扑变更日志:清理 {n} 条超过保留期的事件")
        return n
    except Exception as ex:
        logger.warning(f"拓扑变更日志清理失败: {ex}")
        return 0



def reconcile_calls_edges(round_ts: int) -> dict:
    """把本轮未被观测到的 Calls 边标记为失效。

    与 etl_aws/graph_gc.py 的 _gc_vertices 是同一思路（求差集），
    但那里只覆盖节点，这里补上边级对账。

    自愈：边一旦重新被观测到，batch_upsert_edges 会把 active 写回 true，
    因此误判是自动恢复的，不需要人工干预。

    Args:
        round_ts: 本轮 upsert 使用的时间戳。本轮刷新过的边 last_seen == round_ts，
                  用严格小于把它们排除在对账范围外。

    Returns:
        {'marked_inactive': int, 'dropped': int}
    """
    stats = {'marked_inactive': 0, 'dropped': 0, 'changes_logged': 0, 'changes_pruned': 0}

    inactive_before = round_ts - INACTIVE_AFTER_SECONDS
    try:
        # 先取出**将被影响的边的身份**（不只是计数）—— 变更日志需要知道是哪条依赖
        # 消失了。过滤里带 .has('active', true)，所以只会命中 true→false 的
        # **状态转变**，稳态失效边不会每轮重复产生事件（事件量因此天然有界）。
        pairs = []
        try:
            resp = neptune_query(
                f"g.E().hasLabel('Calls')"
                f".has('last_seen', lt({inactive_before}))"
                f".has('active', true)"
                f".project('src','dst')"
                f".by(__.outV().values('name'))"
                f".by(__.inV().values('name'))"
                f".toList()"
            )
            pairs = _extract_pairs(resp)
        except Exception as ex:
            # 取不到身份不该阻止对账本身 —— 标记失效比记日志重要
            logger.warning(f"对账取边身份失败（仍会继续标记失效）: {ex}")

        n = len(pairs)
        if n == 0:
            # 身份取不到时退回计数，保持原有行为
            cnt = neptune_query(
                f"g.E().hasLabel('Calls')"
                f".has('last_seen', lt({inactive_before}))"
                f".has('active', true).count()"
            )
            n = _first_scalar(cnt)
        if n:
            neptune_query(
                f"g.E().hasLabel('Calls')"
                f".has('last_seen', lt({inactive_before}))"
                f".has('active', true)"
                f".property('active', false)"
                f".iterate()"
            )
            stats['marked_inactive'] = n
            logger.info(
                f"Calls reconcile: {n} 条边超过 {INACTIVE_AFTER_SECONDS}s 未被观测 → active=false"
            )
            stats['changes_logged'] = _emit_topology_changes([
                {
                    'kind': 'dependency_deactivated',
                    'subject': f"{p['src']}->{p['dst']}",
                    'source': p['src'],
                    'target': p['dst'],
                    'edge_type': 'Calls',
                    'detail': f"超过 {INACTIVE_AFTER_SECONDS}s 未被 DeepFlow 观测到",
                }
                for p in pairs
            ])
    except Exception as ex:
        # 对账失败不能影响本轮 upsert 的成果，但必须可见（本项目历史上
        # 9 个缺陷都是异常被吞导致的静默失败）
        logger.warning(f"Calls reconcile (mark inactive) failed: {ex}")

    stats['changes_pruned'] = _prune_change_log(round_ts)

    if not DROP_ENABLED:
        return stats

    drop_before = round_ts - DROP_AFTER_SECONDS
    try:
        cnt = neptune_query(
            f"g.E().hasLabel('Calls').has('last_seen', lt({drop_before})).count()"
        )
        n = _first_scalar(cnt)
        if n:
            neptune_query(
                f"g.E().hasLabel('Calls')"
                f".has('last_seen', lt({drop_before}))"
                f".drop().iterate()"
            )
            stats['dropped'] = n
            logger.info(
                f"Calls reconcile: {n} 条边超过 {DROP_AFTER_SECONDS}s 未被观测 → 已删除"
            )
    except Exception as ex:
        logger.warning(f"Calls reconcile (drop) failed: {ex}")

    return stats


def _extract_pairs(resp) -> list:
    """从 Gremlin project('src','dst') 的响应里取出 [{'src':..,'dst':..}]。

    GraphSON 的包装层数因 Neptune 版本而异（1.4.x 会多一层 @value/@type），
    这里做宽松解析:解析不出来返回空列表,让调用方退回计数路径 ——
    变更日志是旁路,不该因为解析细节炸掉整轮对账。
    """
    def _unwrap(x):
        if isinstance(x, dict):
            if '@value' in x:
                return _unwrap(x['@value'])
            # GraphSON Map 是 ['k1',v1,'k2',v2] 的扁平列表
            return x
        if isinstance(x, list):
            return x
        return x

    out = []
    try:
        data = resp
        if isinstance(data, dict):
            data = data.get('result', {}).get('data', data)
        data = _unwrap(data)
        if not isinstance(data, list):
            return []
        for item in data:
            m = _unwrap(item)
            if isinstance(m, list):
                # 扁平 kv 列表 → dict
                m = {m[i]: _unwrap(m[i + 1]) for i in range(0, len(m) - 1, 2)}
            if not isinstance(m, dict):
                continue
            src, dst = m.get('src'), m.get('dst')
            src = _unwrap(src)
            dst = _unwrap(dst)
            if isinstance(src, list) and src:
                src = _unwrap(src[0])
            if isinstance(dst, list) and dst:
                dst = _unwrap(dst[0])
            if src and dst:
                out.append({'src': str(src), 'dst': str(dst)})
    except Exception:
        return []
    return out


def _first_scalar(resp) -> int:
    """从 Gremlin count() 响应里取出第一个整数，取不到返回 0。

    GraphSON 的包装层数因 Neptune 版本而异，这里做宽松解析，
    解析不出来就当 0 —— 对账是尽力而为，不该因为解析细节炸掉整轮 ETL。
    """
    try:
        if isinstance(resp, dict):
            data = resp.get('result', {}).get('data', resp)
            if isinstance(data, dict):
                data = data.get('@value', data)
            if isinstance(data, list) and data:
                v = data[0]
                if isinstance(v, dict):
                    v = v.get('@value', 0)
                return int(v)
        if isinstance(resp, list) and resp:
            v = resp[0]
            if isinstance(v, dict):
                v = v.get('@value', 0)
            return int(v)
        if isinstance(resp, (int, float)):
            return int(resp)
    except (TypeError, ValueError, AttributeError):
        pass
    return 0

def batch_fetch_dependency_and_update(service_names: list, l7_metrics: dict,
                                       ip_map_by_name: dict, replica_counts: dict,
                                       resource_limits: dict, active_connections_map: dict,
                                       restart_map: dict, nfm_throttling: dict = None):
    """合并 dependency 查询：每个服务 1 次请求（原来 4 次）+ 1 次 metrics 更新"""
    ts = int(time.time())
    for name in service_names:
        n = safe_str(name)
        # 1 次 project 查询替代原来 4 次独立查询
        gremlin = (
            f"g.V().has('Microservice','name','{n}')"
            f".project('up','down','db','cache')"
            f".by(out('Calls').count())"
            f".by(in('Calls').count())"
            f".by(out('Calls').hasLabel('RDSCluster','DynamoDBTable').count())"
            f".by(out('Calls').hasLabel('ElastiCache','Redis','CacheCluster').count())"
        )
        dep = {'upstream_count': 0, 'downstream_count': 0,
               'is_entry_point': False, 'has_db_dependency': False, 'has_cache_dependency': False}
        try:
            res = neptune_query(gremlin)
            data = res.get('result', {}).get('data', {}).get('@value', [])
            if data:
                item = data[0] if isinstance(data, list) else data
                if isinstance(item, dict) and '@value' in item:
                    vals = item['@value']
                    # GraphSON map format: [key, value, key, value, ...]
                    if isinstance(vals, list) and len(vals) >= 8:
                        dep['upstream_count'] = int(extract_value(vals[1]))
                        dep['downstream_count'] = int(extract_value(vals[3]))
                        dep['has_db_dependency'] = int(extract_value(vals[5])) > 0
                        dep['has_cache_dependency'] = int(extract_value(vals[7])) > 0
                        dep['is_entry_point'] = dep['downstream_count'] == 0
        except Exception as e:
            logger.error(f"dependency query {name}: {e}")

        # 构建 metrics
        svc_ip = ip_map_by_name.get(name, '')
        l7 = l7_metrics.get(svc_ip, {})
        priority = MICROSERVICE_RECOVERY_PRIORITY.get(name, 'Tier2')
        svc_type = SERVICE_TYPES.get(name, 'k8s')
        fb = 'region' if svc_type == 'lambda' else 'az'
        props = [f".property(single,'metrics_updated_at',{ts})",
                 f".property(single,'environment','{ENVIRONMENT}')",
                 f".property(single,'recovery_priority','{priority}')",
                 f".property(single,'fault_boundary','{fb}')",
                 f".property(single,'service_type','{svc_type}')",
                 f".property(single,'region','{REGION}')"]
        for key in ['p50_latency_ms', 'p99_latency_ms', 'rps', 'error_rate']:
            v = l7.get(key, -1)
            props.append(f".property(single,'{key}',{float(v):.4f})")
        # active_connections（来自 network_map L4 数据）
        ac = active_connections_map.get(svc_ip, -1)
        props.append(f".property(single,'active_connections',{int(ac)})")
        props.append(f".property(single,'replica_count',{int(replica_counts.get(name, -1))})")
        # K8s resource limits
        limits = resource_limits.get(name, {})
        if limits.get('cpu'):
            cpu_s = safe_str(limits['cpu'])
            props.append(f".property(single,'resource_limit_cpu','{cpu_s}')")
        if limits.get('memory'):
            mem_s = safe_str(limits['memory'])
            props.append(f".property(single,'resource_limit_memory','{mem_s}')")
        # 变更锚点：最近部署时间 + pod 重启次数
        if limits.get('last_deploy_time'):
            props.append(f".property(single,'last_deploy_time','{safe_str(limits['last_deploy_time'])}')")
        restart_count = restart_map.get(name, 0)
        props.append(f".property(single,'pod_restart_count',{restart_count})")
        for key in ['upstream_count', 'downstream_count']:
            props.append(f".property(single,'{key}',{int(dep.get(key, -1))})")
        for key in ['is_entry_point', 'has_db_dependency', 'has_cache_dependency']:
            props.append(f".property(single,'{key}',{'true' if dep.get(key) else 'false'})")
        # NFM 限速标志（来自 prometheus.samples，三个 bool 属性）
        throttling = (nfm_throttling or {}).get(name, {})
        bw  = throttling.get('nfm_bw_throttled',       False)
        pps = throttling.get('nfm_pps_throttled',      False)
        ct  = throttling.get('nfm_conntrack_throttled', False)
        props.append(f".property(single,'nfm_bw_throttled',{'true' if bw else 'false'})")
        props.append(f".property(single,'nfm_pps_throttled',{'true' if pps else 'false'})")
        props.append(f".property(single,'nfm_conntrack_throttled',{'true' if ct else 'false'})")

        try:
            neptune_query(f"g.V().has('Microservice','name','{n}'){''.join(props)}")
        except Exception as e:
            logger.error(f"update metrics {name}: {e}")

# ===== 主处理逻辑 =====

def run_etl():
    logger.info("=== neptune-etl-from-deepflow 开始 (optimized) ===")
    t0 = time.time()

    # 1. 构建 IP→服务名映射
    ip_map, ecr_dep_map, restart_map = build_ip_service_map()
    if not ip_map:
        logger.warning("Empty IP map, will use IP-based names as fallback")

    # 2. 查询 ClickHouse - L7 流量关系
    # 注意：去掉 type=0 过滤（DeepFlow 中 type=2 是响应日志，占绝大多数有效数据）
    # server_ip 用 ip4_1，error 用 response_status=3（服务端异常）
    sql = f"""
SELECT IPv4NumToString(ip4_0) as src_ip, IPv4NumToString(ip4_1) as dst_ip,
    server_port, l7_protocol_str, count() as calls,
    avg(response_duration) as avg_latency_us,
    countIf(response_status = 3) as error_count,
    quantile(0.99)(response_duration)/1000 AS p99_latency_ms
FROM flow_log.l7_flow_log
WHERE ip4_0 != 0 AND ip4_1 != 0
    AND toUnixTimestamp(time) > toUnixTimestamp(now()) - {INTERVAL_MIN}*60
    AND ip4_0 != ip4_1
GROUP BY src_ip, dst_ip, server_port, l7_protocol_str
HAVING calls >= 2
ORDER BY calls DESC LIMIT 100 FORMAT TSV
"""
    rows = ch_query(sql)
    logger.info(f"发现 {len(rows)} 条调用关系")

    # 3. L7 性能指标
    l7_metrics = fetch_l7_metrics()

    # 3b. 活跃连接数（from network_map L4）
    active_connections_map = fetch_active_connections()

    if not rows:
        logger.info("No flow data, skipping")
        return {"nodes": 0, "edges": 0, "duration_ms": int((time.time() - t0) * 1000)}

    # 4. 收集所有需要 upsert 的节点和边
    nodes_set = {}  # name -> {name, namespace, ip}
    edges_list = []
    ip_map_by_name = {}  # name -> ip

    for row in rows:
        if len(row) < 7:
            continue
        src_ip, dst_ip, port, protocol, calls_s, avg_lat_s, errors_s = row[:7]
        p99_ms = float(row[7]) if len(row) > 7 else -1.0
        try:
            calls, avg_lat, errors = int(calls_s), float(avg_lat_s), int(errors_s)
        except ValueError:
            continue

        src_info = ip_map.get(src_ip)
        dst_info = ip_map.get(dst_ip)
        # 只处理 ip_map 中能解析的 IP（K8s 已知 pod/service）
        # 未解析的 IP（旧 pod、外部服务等）直接跳过，避免产生 svc-x.x.x.x 幽灵节点
        if not src_info or not dst_info:
            continue
        src_name, dst_name = src_info['name'], dst_info['name']
        if src_name == dst_name:
            continue

        # 过滤基础设施 sidecar 和测试流量节点（避免假环路）
        # xray-daemon / trafficgenerator 等产生的网络流量不代表真实业务依赖
        EXCLUDED_SERVICES = {
            'xray-daemon',           # AWS X-Ray trace sidecar
            'trafficgenerator',      # 负载测试流量生成器
            'traffic-generator',     # 别名
            'aws-otel-collector',    # OTEL 遥测 sidecar
            'fluentd',               # 日志采集 sidecar
            'fluent-bit',            # 日志采集 sidecar
            'datadog-agent',         # 监控 agent
            'prometheus',            # 指标采集
            'jaeger-agent',          # trace sidecar
        }
        if src_name in EXCLUDED_SERVICES or dst_name in EXCLUDED_SERVICES:
            logger.debug(f"跳过基础设施/测试节点: {src_name} -> {dst_name}")
            continue

        # 拦截 ARN 格式和 AWS TargetGroup 命名（belt-and-suspenders）
        def _is_valid_svc_name(n: str) -> bool:
            if n.startswith('arn:'):        return False  # ARN
            if '/' in n:                    return False  # ARN path 或非法名称
            if len(n) > 64:                 return False  # K8s svc name 最长 63
            return True
        if not _is_valid_svc_name(src_name) or not _is_valid_svc_name(dst_name):
            logger.debug(f"跳过非法服务名: {src_name} -> {dst_name}")
            continue

        nodes_set[src_name] = {'name': src_name, 'namespace': src_info['namespace'], 'ip': src_ip, 'az': src_info.get('az', '')}
        nodes_set[dst_name] = {'name': dst_name, 'namespace': dst_info['namespace'], 'ip': dst_ip, 'az': dst_info.get('az', '')}
        ip_map_by_name[src_name] = src_ip
        ip_map_by_name[dst_name] = dst_ip
        edges_list.append({
            'src': src_name, 'dst': dst_name, 'protocol': protocol,
            'port': port, 'calls': calls, 'avg_latency': avg_lat,
            'errors': errors, 'p99_latency_ms': p99_ms,
        })

    # 5. 批量写入节点
    nodes_list = list(nodes_set.values())
    logger.info(f"批量 upsert {len(nodes_list)} 个节点...")
    batch_upsert_nodes(nodes_list)

    # 5b. 更新 az 属性（set cardinality，保留多 AZ 信息）
    # 设计原则：Microservice 可能跨多 AZ 部署（多副本），az 应存所有实际 AZ 的集合
    # 用 set cardinality：property(set,'az','az-1a') / property(set,'az','az-1c')
    # 每次 ETL 先 drop 旧 az（反映当前真实部署），再写入本次 ip_map 中的 AZ
    #
    # 查询示例：az-1a 故障影响哪些服务？
    #   g.V().hasLabel('Microservice').has('az','ap-northeast-1a').values('name')
    # → 返回有 pod 在 1a 的所有服务（含多 AZ 部署的服务，如 petsite）

    # 构建 service → {az1, az2, ...} 映射（从所有 pod 的 node_az 信息）
    svc_azs_map: dict = {}  # service_name → set of azs
    for pod_ip, info in ip_map.items():
        sn = info['name']
        az_val = info.get('az', '')
        if az_val:
            if sn not in svc_azs_map:
                svc_azs_map[sn] = set()
            svc_azs_map[sn].add(az_val)

    az_updated = 0
    for sn, az_set in svc_azs_map.items():
        n = safe_str(sn)
        try:
            # Step 1: drop 旧 az 属性（清除过期的 AZ 信息，如 pod 被重新调度到其他 AZ）
            neptune_query(
                f"g.V().hasLabel('Microservice').has('name','{n}').properties('az').drop()"
            )
            # Step 2: 逐一添加当前所有 AZ（set cardinality = 自动去重）
            for az_val in az_set:
                az_s = safe_str(az_val)
                neptune_query(
                    f"g.V().hasLabel('Microservice').has('name','{n}')"
                    f".property(set,'az','{az_s}')"
                )
            az_updated += 1
        except Exception as e:
            logger.error(f"Update az {sn}: {e}")
    logger.info(f"az 属性更新完成: {az_updated} 个服务，az_sets={{{', '.join(f'{k}:{v}' for k,v in list(svc_azs_map.items())[:5])}}}")

    # 6. 写入边（复用连接，无需 get_vertex_id）
    logger.info(f"upsert {len(edges_list)} 条边...")
    round_ts = batch_upsert_edges(edges_list)

    # 6.5 Calls 边失效对账 —— 必须紧跟 upsert，用同一个 round_ts 做分界：
    # 本轮刷新过的边 last_seen == round_ts，未刷新的严格小于它。
    reconcile_stats = reconcile_calls_edges(round_ts)

    # 7. 副本数 + resource limits
    replica_counts = fetch_replica_counts(ip_map)
    resource_limits = fetch_resource_limits(ip_map)

    # 7b. NFM 限速指标（bw / pps / conntrack throttling）
    nfm_throttling = fetch_nfm_throttling(ip_map)

    # 8. 合并 dependency 查询 + metrics 更新（每服务 2 次请求，原来 5 次）
    # T03 防复发: 过滤 xray-daemon 等噪音节点
    DEEPFLOW_SKIP = {'xray-daemon', 'xray-service'}
    service_names = [s for s in nodes_set.keys() if s not in DEEPFLOW_SKIP]
    logger.info(f"更新 {len(service_names)} 个服务的指标...")
    batch_fetch_dependency_and_update(service_names, l7_metrics, ip_map_by_name,
                                       replica_counts, resource_limits, active_connections_map,
                                       restart_map, nfm_throttling)

    # 8b. T13a: DNS/TCP 漂移检测
    try:
        run_drift_detection(service_names, ip_map)
    except Exception as e:
        logger.warning(f"Drift detection failed (non-fatal): {e}")

    # 9. 验证
    try:
        v_res = neptune_query("g.V().count()")
        e_res = neptune_query("g.E().count()")
        v_count = extract_value(v_res['result']['data']['@value'][0])
        e_count = extract_value(e_res['result']['data']['@value'][0])
        logger.info(f"Neptune 图状态: 顶点={v_count}, 边={e_count}")
    except Exception as e:
        logger.warning(f"Verification failed: {e}")

    # GC：用单条 Gremlin 直接删除旧 K8s 原名节点（alias 存在前遗留的幽灵节点）
    # 典型：pay-for-adoption→payforadoption、list-adoptions→petlistadoptions
    try:
        stale_keys = list(K8S_SERVICE_ALIAS.keys())
        dropped = neptune_query(
            f"g.V().hasLabel('Microservice').has('source','deepflow')"
            f".where(values('name').is(within({','.join(repr(k) for k in stale_keys)})))"
            f".sideEffect(drop()).count()"
        )['result']['data']['@value'][0]
        if isinstance(dropped, dict): dropped = dropped.get('@value', 0)
        if int(dropped) > 0:
            logger.info(f"Microservice GC: 删除 {dropped} 个旧 K8s 原名节点")
    except Exception as e:
        logger.warning(f"Microservice GC failed (non-fatal): {e}")

    # ECR 启动依赖写入（来自 build_ip_service_map 提取的镜像信息）
    ecr_startup_count = 0
    try:
        ts_ecr = int(time.time())
        ecr_sfx = '.dkr.ecr.' + REGION + '.amazonaws.com/'
        for svc_name, ecr_repos in ecr_dep_map.items():
            svc_vids = neptune_query(
                f"g.V().hasLabel('Microservice').has('name','{safe_str(svc_name)}').id()"
            ).get('result', {}).get('data', {}).get('@value', [])
            if not svc_vids:
                continue
            svc_vid = svc_vids[0]
            svc_vid = svc_vid.get('@value', svc_vid) if isinstance(svc_vid, dict) else svc_vid
            for repo in ecr_repos:
                ecr_vids = neptune_query(
                    f"g.V().hasLabel('ECRRepository').has('name','{safe_str(repo)}').id()"
                ).get('result', {}).get('data', {}).get('@value', [])
                if not ecr_vids:
                    continue
                ecr_vid = ecr_vids[0]
                ecr_vid = ecr_vid.get('@value', ecr_vid) if isinstance(ecr_vid, dict) else ecr_vid
                neptune_query(
                    f"g.V('{svc_vid}').as('s').V('{ecr_vid}')"
                    f".coalesce("
                    f"  __.inE('DependsOn').where(__.outV().hasId('{svc_vid}')),"
                    f"  __.addE('DependsOn').from('s')"
                    f").property('source','deepflow-etl')"
                    # ECR 启动依赖同样源自运行时观测（实际拉取的镜像）→ dynamic
                    f".property('dependency_kind','dynamic')"
                    f".property('phase','startup')"
                    f".property('strength','strong')"
                    f".property('last_updated',{ts_ecr})"
                )
                ecr_startup_count += 1
        if ecr_startup_count:
            logger.info(f"ECR startup deps: {ecr_startup_count} 条边写入")
    except Exception as e:
        logger.warning(f"ECR startup deps failed (non-fatal): {e}")

    duration = int((time.time() - t0) * 1000)
    logger.info(f"=== ETL 完成: nodes={len(nodes_list)}, edges={len(edges_list)}, {duration}ms ===")
    return {"nodes": len(nodes_list), "edges": len(edges_list),
            "marked_inactive": reconcile_stats.get("marked_inactive", 0),
            "dropped": reconcile_stats.get("dropped", 0),
            "duration_ms": duration}


def handler(event, context):
    try:
        return {"statusCode": 200, "body": run_etl()}
    except Exception as e:
        logger.error(f"ETL failed: {e}", exc_info=True)
        raise
