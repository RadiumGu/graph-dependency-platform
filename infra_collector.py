"""
infra_collector.py - 基础设施层实时采集

在 RCA 分析时调用，实时查询：
  1. EKS Pod 状态（CrashLoop / OOM / Pending 等）
  2. CloudWatch RDS 指标（连接数、CPU、内存）
  3. service-db-mapping.json 服务-DB关联关系
"""

import os, json, logging, base64, datetime, ssl, urllib.request
import boto3

logger = logging.getLogger()
REGION = os.environ.get('REGION', 'ap-northeast-1')
EKS_CLUSTER = os.environ.get('EKS_CLUSTER_NAME', 'PetSite')

# ---- DB Mapping ----

# 静态映射（由扫描脚本确认，写入代码避免文件路径问题）
STATIC_DB_MAPPING = [
    {
        'service': 'pethistory-deployment',
        'namespace': 'default',
        'db_cluster_id': 'serviceseks2-databaseb269d8bb-efjeyzicx2ak',
        'dbname': 'adoptions',
        'engine': 'postgres',
    }
]

def get_service_db(service_name):
    return [m for m in STATIC_DB_MAPPING if service_name in m.get('service', '')]


# ---- EKS Pod 状态 ----

def _get_k8s_token():
    try:
        eks = boto3.client('eks', region_name=REGION)
        cluster = eks.describe_cluster(name=EKS_CLUSTER)['cluster']
        endpoint = cluster['endpoint']
        from botocore.auth import SigV4QueryAuth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials
        creds = boto3.Session().get_credentials().get_frozen_credentials()
        signer = SigV4QueryAuth(
            credentials=Credentials(creds.access_key, creds.secret_key, creds.token),
            service_name='sts', region_name=REGION, expires=60
        )
        url = f'https://sts.{REGION}.amazonaws.com/?Action=GetCallerIdentity&Version=2011-06-15'
        req = AWSRequest(method='GET', url=url, headers={'x-k8s-aws-id': EKS_CLUSTER})
        signer.add_auth(req)
        token = 'k8s-aws-v1.' + base64.urlsafe_b64encode(req.url.encode()).decode().rstrip('=')
        return endpoint, token
    except Exception as e:
        logger.warning(f'EKS token error: {e}')
        return None, None


def _get_node_az_map(node_names: list) -> dict:
    """通过 EC2 API 根据 Node private DNS name 查询 AZ，返回 {node_name: az}"""
    if not node_names:
        return {}
    try:
        ec2 = boto3.client('ec2', region_name=REGION)
        resp = ec2.describe_instances(
            Filters=[{'Name': 'private-dns-name', 'Values': node_names}]
        )
        node_az = {}
        for reservation in resp['Reservations']:
            for inst in reservation['Instances']:
                dns = inst.get('PrivateDnsName', '')
                az = inst['Placement']['AvailabilityZone']
                node_az[dns] = az
        return node_az
    except Exception as e:
        logger.warning(f'EC2 Node AZ query failed: {e}')
        return {}


def get_pods_for_service(service_name, namespace='default'):
    endpoint, token = _get_k8s_token()
    if not endpoint:
        return []
    # 尝试两种 label: app=<service> 和 app=<service去掉-deployment>
    labels = [service_name, service_name.replace('-deployment', '')]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    # 先收集所有 pod，再批量查 Node AZ（避免多次 API 调用）
    all_pods_raw = []
    for label in labels:
        url = f'{endpoint}/api/v1/namespaces/{namespace}/pods?labelSelector=app%3D{label}'
        try:
            req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
            with urllib.request.urlopen(req, context=ctx, timeout=5) as resp:
                data = json.loads(resp.read())
            items = data.get('items', [])
            if items:
                for item in items:
                    name = item['metadata']['name']
                    phase = item['status'].get('phase', 'Unknown')
                    node = item['spec'].get('nodeName', '')
                    restarts, reason = 0, ''
                    for cs in item['status'].get('containerStatuses', []):
                        restarts = max(restarts, cs.get('restartCount', 0))
                        waiting = cs.get('state', {}).get('waiting', {})
                        if waiting.get('reason'):
                            reason = waiting['reason']
                    all_pods_raw.append({'name': name, 'status': phase,
                                         'restarts': restarts, 'node': node, 'reason': reason})
                break  # 找到了就不用试其他 label
        except Exception as e:
            logger.warning(f'K8s pod query (label={label}): {e}')

    if not all_pods_raw:
        return []

    # 批量查 Node→AZ（EC2 API）
    node_names = list(set(p['node'] for p in all_pods_raw if p['node']))
    node_az_map = _get_node_az_map(node_names)
    for p in all_pods_raw:
        p['az'] = node_az_map.get(p['node'], '')
    return all_pods_raw


# ---- CloudWatch RDS 指标 ----

def get_db_metrics(cluster_id):
    cw = boto3.client('cloudwatch', region_name=REGION)
    rds = boto3.client('rds', region_name=REGION)
    db_status = 'unknown'
    try:
        # 集群状态（available/backing-up/failing-over 等）
        clusters = rds.describe_db_clusters(DBClusterIdentifier=cluster_id)['DBClusters']
        if clusters:
            db_status = clusters[0].get('Status', 'unknown')
            # 如果集群 available 但有实例异常，再检查实例层
            if db_status == 'available':
                instances = rds.describe_db_instances(
                    Filters=[{'Name': 'db-cluster-id', 'Values': [cluster_id]}]
                )['DBInstances']
                inst_statuses = [i['DBInstanceStatus'] for i in instances]
                non_ok = [s for s in inst_statuses if s != 'available']
                if non_ok:
                    db_status = f'cluster=available,instances={",".join(non_ok)}'
    except Exception:
        pass

    end = datetime.datetime.utcnow()
    start = end - datetime.timedelta(minutes=10)

    def get_metric(metric_name, stat='Average'):
        try:
            resp = cw.get_metric_statistics(
                Namespace='AWS/RDS', MetricName=metric_name,
                Dimensions=[{'Name': 'DBClusterIdentifier', 'Value': cluster_id}],
                StartTime=start, EndTime=end, Period=300, Statistics=[stat]
            )
            pts = resp.get('Datapoints', [])
            return pts[-1].get(stat, 0) if pts else None
        except Exception:
            return None

    connections = get_metric('DatabaseConnections')
    cpu = get_metric('CPUUtilization')
    free_mem = get_metric('FreeableMemory')
    return {
        'cluster_id': cluster_id,
        'status': db_status,
        'connections': int(connections) if connections is not None else None,
        'cpu_pct': round(cpu, 1) if cpu is not None else None,
        'freeable_memory_mb': int(free_mem / 1024 / 1024) if free_mem else None,
    }


# ---- 主入口 ----

def collect(service_name):
    result = {'pods': [], 'databases': [], 'mapping': []}
    try:
        pods = get_pods_for_service(service_name)
        result['pods'] = pods
        if pods:
            az_dist = {}
            for p in pods:
                az_dist[p.get('az','?')] = az_dist.get(p.get('az','?'), 0) + 1
            logger.info(f'Infra pods: {len(pods)} pods, max_restarts={max(p["restarts"] for p in pods)}, az_dist={az_dist}')
    except Exception as e:
        logger.warning(f'Pod collection failed: {e}')

    try:
        db_mapping = get_service_db(service_name)
        result['mapping'] = db_mapping
        for m in db_mapping:
            metrics = get_db_metrics(m['db_cluster_id'])
            metrics['dbname'] = m['dbname']
            metrics['engine'] = m['engine']
            result['databases'].append(metrics)
            logger.info(f'Infra DB: {m["dbname"]} status={metrics["status"]} connections={metrics["connections"]} cpu={metrics["cpu_pct"]}%')
    except Exception as e:
        logger.warning(f'DB collection failed: {e}')
    return result


def format_for_prompt(infra):
    lines = ['[基础设施状态]']
    pods = infra.get('pods', [])
    if pods:
        lines.append(f'Pods ({len(pods)} 个):')
        for p in pods:
            flag = ' ⚠️ CrashLoop/OOM' if p['reason'] in ('CrashLoopBackOff','OOMKilled','ImagePullBackOff') else (
                   ' ⚠️ 高重启' if p['restarts'] >= 5 else '')
            az_str = f' az={p["az"]}' if p.get('az') else ''
            lines.append(f'  - {p["name"]}: {p["status"]} restarts={p["restarts"]}{az_str}{flag}')
    else:
        lines.append('Pods: 未查询到（label不匹配或无权限）')

    dbs = infra.get('databases', [])
    if dbs:
        lines.append('数据库:')
        for db in dbs:
            conn_note = ' ⚠️ 连接数过高' if db.get('connections') and db['connections'] > 400 else ''
            cpu_note = ' ⚠️ CPU过高' if db.get('cpu_pct') and db['cpu_pct'] > 80 else ''
            lines.append(
                f'  - {db["dbname"]} ({db["engine"]}): {db["status"]}'
                f' conns={db.get("connections","N/A")}{conn_note}'
                f' cpu={db.get("cpu_pct","N/A")}%{cpu_note}'
                f' free_mem={db.get("freeable_memory_mb","N/A")}MB'
            )
    else:
        lines.append('数据库: 该服务无关联DB映射')
    return '\n'.join(lines)
