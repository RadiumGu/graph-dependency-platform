#!/usr/bin/env python3
"""
scan-service-db-mapping.py
扫描 K8s ConfigMap/Secrets Manager，提取 服务名 → DB名 映射，
可选自动给 RDS 集群打 tag。

用法：
  python3 scan-service-db-mapping.py          # dry-run，只扫描
  python3 scan-service-db-mapping.py --tag    # 扫描后自动打 tag

结果保存至: ./service-db-mapping.json
"""

import subprocess, json, re, sys, boto3

REGION = 'ap-northeast-1'
DRY_RUN = '--tag' not in sys.argv
OUTPUT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'service-db-mapping.json')


def get_deployments():
    """获取所有 deployment 的 env 变量"""
    r = subprocess.run(['kubectl', 'get', 'deployment', '-A', '-o', 'json'],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return []
    items = json.loads(r.stdout).get('items', [])
    results = []
    for item in items:
        ns = item['metadata']['namespace']
        name = item['metadata']['name']
        containers = item.get('spec', {}).get('template', {}).get('spec', {}).get('containers', [])
        for c in containers:
            for env in c.get('env', []):
                k = env.get('name', '')
                v = env.get('value', '')
                if 'SECRET_ARN' in k.upper() and 'rds' not in k.lower():
                    continue
                if any(x in k.upper() for x in ['RDS_SECRET', 'DATABASE_SECRET', 'DB_SECRET']):
                    results.append({'service': name, 'namespace': ns, 'secret_arn': v})
                elif any(x in k.upper() for x in ['DB_HOST', 'DATABASE_URL', 'RDS_HOST', 'MYSQL_HOST', 'POSTGRES_HOST']):
                    results.append({'service': name, 'namespace': ns, 'db_endpoint': v})
    return results


def resolve_secret(secret_arn):
    """从 Secrets Manager 解析 DB endpoint"""
    try:
        sm = boto3.client('secretsmanager', region_name=REGION)
        val = sm.get_secret_value(SecretId=secret_arn)['SecretString']
        d = json.loads(val)
        return {
            'host': d.get('host', ''),
            'dbname': d.get('dbname', ''),
            'engine': d.get('engine', ''),
            'cluster_id': d.get('dbClusterIdentifier', ''),
        }
    except Exception as e:
        return None


def get_rds_clusters():
    """获取所有 RDS 集群"""
    rds = boto3.client('rds', region_name=REGION)
    clusters = {}
    for c in rds.describe_db_clusters()['DBClusters']:
        clusters[c['DBClusterIdentifier']] = {
            'arn': c['DBClusterArn'],
            'endpoint': c['Endpoint'],
            'engine': c['Engine'],
            'status': c['Status'],
        }
    return clusters


def tag_rds_cluster(cluster_arn, service_name):
    """给 RDS 集群打 tag"""
    rds = boto3.client('rds', region_name=REGION)
    tags = [
        {'Key': 'app', 'Value': service_name},
        {'Key': 'managed-by', 'Value': 'petsite-etl'},
        {'Key': 'connected-service', 'Value': service_name},
    ]
    rds.add_tags_to_resource(ResourceName=cluster_arn, Tags=tags)


def main():
    print("=== PetSite 服务-数据库映射扫描 ===\n")

    # 1. 扫描 Deployment env
    print("[1] 扫描 Deployment 环境变量...")
    deployments = get_deployments()
    for d in deployments:
        print(f"  {d['namespace']}/{d['service']}: secret_arn={d.get('secret_arn','')[:60]}")

    # 2. 解析 Secrets Manager
    print("\n[2] 解析 Secrets Manager...")
    mappings = []
    for d in deployments:
        if d.get('secret_arn'):
            info = resolve_secret(d['secret_arn'])
            if info:
                mappings.append({
                    'service': d['service'],
                    'namespace': d['namespace'],
                    'db_cluster_id': info['cluster_id'],
                    'db_endpoint': info['host'],
                    'dbname': info['dbname'],
                    'engine': info['engine'],
                    'source': 'secrets_manager',
                    'secret_arn': d['secret_arn'],
                })
                print(f"  ✅ {d['service']} → {info['cluster_id']} (dbname={info['dbname']})")

    # 3. RDS 集群列表
    print("\n[3] RDS 集群列表...")
    clusters = get_rds_clusters()
    for cid, info in clusters.items():
        print(f"  {cid}: {info['engine']} {info['status']}")

    # 4. 打 tag
    if mappings:
        print(f"\n[4] 找到 {len(mappings)} 条服务-DB映射")
        for m in mappings:
            cid = m['db_cluster_id']
            if cid in clusters:
                m['db_cluster_arn'] = clusters[cid]['arn']
                if not DRY_RUN:
                    tag_rds_cluster(clusters[cid]['arn'], m['service'])
                    print(f"  Tagged: {cid} ← app={m['service']}")
                else:
                    print(f"  [DRY-RUN] Would tag {cid} ← app={m['service']}")
        if DRY_RUN:
            print("\n加 --tag 参数执行实际打 tag")
    else:
        print("\n未找到映射关系")

    # 5. 保存结果
    with open(OUTPUT_FILE, 'w') as f:
        json.dump(mappings, f, indent=2, ensure_ascii=False)
    print(f"\n映射结果已保存: {OUTPUT_FILE}")
    return mappings


if __name__ == '__main__':
    mappings = main()
    print("\n=== 最终映射结果 ===")
    for m in mappings:
        print(f"  Service: {m['service']}")
        print(f"    DB Cluster: {m['db_cluster_id']}")
        print(f"    Database: {m['dbname']}")
        print(f"    Engine: {m['engine']}")
