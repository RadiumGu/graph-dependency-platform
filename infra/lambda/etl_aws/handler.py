"""
handler.py - Lambda entry point and run_etl orchestration for neptune-etl-from-aws.

Lambda 2: neptune-etl-from-aws
Trigger: every 15 minutes (EventBridge) + event-driven via neptune-etl-trigger
"""

import os
import logging
import time
import boto3

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

from config import (
    REGION, EKS_CLUSTER_NAME,
    EC2_RECOVERY_PRIORITY, LAMBDA_RECOVERY_PRIORITY,
    MICROSERVICE_RECOVERY_PRIORITY, MICROSERVICE_INFRA_DEPS,
    MICROSERVICE_NAMESPACE,
    SERVICE_DB_MAPPING, OPS_TOOL_EDGES, TG_APP_LABEL_STATIC,
)
from neptune_client import (
    neptune_query, upsert_vertex, upsert_edge,
    upsert_az_region, safe_str, extract_value,
    _vid_cache, get_vertex_id, find_vertex_by_name,
)
from collectors.ec2 import (
    collect_ec2_instances, collect_subnets, collect_vpcs, collect_security_groups,
)
from collectors.eks import (
    collect_eks_cluster, collect_eks_nodegroup_instances,
    collect_k8s_services, get_pod_ip_to_app_label, collect_eks_pods,
    collect_k8s_deployments, collect_k8s_hpas,
    _K8S_SVC_ALIAS,
)
from collectors.alb import (
    collect_load_balancers, collect_listener_rules, collect_alb_target_groups,
)
from collectors.rds import collect_rds_clusters, collect_rds_instances
from collectors.lambda_sfn import (
    collect_lambda_functions, collect_step_functions, extract_sfn_lambda_refs,
)
from collectors.data_stores import (
    collect_dynamodb_tables, collect_sqs_queues, collect_sns_topics,
    collect_s3_buckets_in_region, collect_ecr_repositories,
)
from cloudwatch import (
    fetch_ec2_cloudwatch_metrics_batch, fetch_lambda_cloudwatch_metrics_batch,
    fetch_nfm_ec2_metrics, map_nfm_metrics_to_ec2,
    update_ec2_metrics, update_ec2_nfm_metrics, update_lambda_metrics,
)
from business_layer import upsert_business_capabilities, scan_ecr_startup_deps
from graph_gc import run_gc


def run_etl():
    logger.info("=== neptune-etl-from-aws start ===")

    session = boto3.Session(region_name=REGION)
    ec2_client = session.client('ec2', region_name=REGION)
    eks_client = session.client('eks', region_name=REGION)
    elb_client = session.client('elbv2', region_name=REGION)
    lambda_client = session.client('lambda', region_name=REGION)
    sfn_client = session.client('stepfunctions', region_name=REGION)
    ddb_client = session.client('dynamodb', region_name=REGION)
    cw_client = session.client('cloudwatch', region_name=REGION)
    rds_client = session.client('rds', region_name=REGION)
    sqs_client = session.client('sqs', region_name=REGION)
    sns_client = session.client('sns', region_name=REGION)
    s3_client = session.client('s3', region_name=REGION)
    ecr_client = session.client('ecr', region_name=REGION)

    stats = {'vertices': 0, 'edges': 0, 'cw_ec2': 0, 'cw_lambda': 0}

    # ── Step 0: Region node ──────────────────────────────────────────────────
    upsert_vertex('Region', REGION, {'region_name': REGION, 'provider': 'aws'}, 'aws')
    region_vid = _vid_cache.get(('Region', REGION))
    if not region_vid:
        region_vid = get_vertex_id('Region', REGION)
        if region_vid:
            _vid_cache[('Region', REGION)] = region_vid
    stats['vertices'] += 1

    # ── Step 1: Subnets ──────────────────────────────────────────────────────
    subnets = collect_subnets(ec2_client)
    subnet_map = {}
    subnet_vid_map = {}
    for sn in subnets:
        sn_vid = upsert_vertex('Subnet', sn['name'], {
            'subnet_id': sn['subnet_id'],
            'cidr': sn['cidr'],
            'az': sn['az'],
            'vpc_id': sn['vpc_id'],
        }, 'cloudformation')
        subnet_map[sn['subnet_id']] = sn['name']
        subnet_vid_map[sn['subnet_id']] = sn_vid
        stats['vertices'] += 1
        az_vid, _ = upsert_az_region(sn['az'])
        if sn_vid and az_vid:
            upsert_edge(sn_vid, az_vid, 'LocatedIn', {'source': 'aws-etl'})
            stats['edges'] += 1

    # ── Step 2: EC2 instances ────────────────────────────────────────────────
    ec2_instances = collect_ec2_instances(ec2_client)
    inst_vid_map = {}
    for inst in ec2_instances:
        priority = 'Tier2'
        for key, p in EC2_RECOVERY_PRIORITY.items():
            if key.lower() in inst['name'].lower():
                priority = p
                break
        if inst.get('tag_tier'):
            priority = inst['tag_tier']
        extra = {k: v for k, v in {
            'environment': inst.get('environment'),
            'system':      inst.get('system'),
            'team':        inst.get('team'),
        }.items() if v is not None}
        _is_eks_node = (inst.get('system') or '').lower() in ('petsite', 'eks') or \
                       inst['name'].startswith('petsite-eks-node')
        _log_source_ec2 = (
            f"cwlogs:///aws/containerinsights/{EKS_CLUSTER_NAME}/host?node={inst['name']}"
            if _is_eks_node else ''
        )
        inst_vid = upsert_vertex('EC2Instance', inst['name'], {
            'instance_id': inst['id'],
            'instance_type': inst['instance_type'],
            'state': inst.get('state', 'unknown'),
            'az': inst['az'],
            'private_ip': inst['private_ip'],
            'recovery_priority': priority,
            'health_status': 'healthy' if inst.get('state') == 'running' else 'unhealthy',
            'log_source': _log_source_ec2,
            **extra,
        }, inst['managed_by'])
        inst_vid_map[inst['id']] = inst_vid
        stats['vertices'] += 1
        upsert_az_region(inst['az'])
        if inst['az']:
            az_vid = upsert_vertex('AvailabilityZone', inst['az'], {}, 'aws')
            if inst_vid and az_vid:
                upsert_edge(inst_vid, az_vid, 'LocatedIn', {'source': 'aws-etl'})
                stats['edges'] += 1
        if inst['subnet_id'] in subnet_vid_map:
            sn_vid = subnet_vid_map[inst['subnet_id']]
            if inst_vid and sn_vid:
                upsert_edge(inst_vid, sn_vid, 'LocatedIn', {'source': 'aws-etl'})
                stats['edges'] += 1

    ec2_cw = fetch_ec2_cloudwatch_metrics_batch(cw_client, ec2_instances)
    for inst in ec2_instances:
        try:
            if update_ec2_metrics(inst['name'], ec2_cw.get(inst['id'], {})):
                stats['cw_ec2'] += 1
        except Exception as e:
            logger.warning(f"EC2 CW metrics {inst['name']}: {e}")

    nfm_cw = fetch_nfm_ec2_metrics(cw_client)
    if nfm_cw:
        ec2_nfm_map = map_nfm_metrics_to_ec2(nfm_cw, ec2_instances)
        for ec2_name, nfm_metrics in ec2_nfm_map.items():
            update_ec2_nfm_metrics(ec2_name, nfm_metrics)
        logger.info(f"NFM metrics written to {len(ec2_nfm_map)} EC2 nodes")

    # ── Step 3: EKS cluster ──────────────────────────────────────────────────
    eks_cluster = collect_eks_cluster(eks_client)
    if eks_cluster:
        cluster_vid = upsert_vertex('EKSCluster', eks_cluster['name'], {
            'version': eks_cluster['version'],
            'status': eks_cluster['status'],
            'arn': eks_cluster['arn'],
        }, 'cloudformation')
        stats['vertices'] += 1
        eks_instance_ids = collect_eks_nodegroup_instances(eks_client, ec2_client)
        for inst_id in eks_instance_ids:
            inst_vid = inst_vid_map.get(inst_id)
            if cluster_vid and inst_vid:
                upsert_edge(inst_vid, cluster_vid, 'BelongsTo', {'source': 'aws-etl'})
                stats['edges'] += 1

    # ── Step 4: ALB + TargetGroups ───────────────────────────────────────────
    load_balancers = collect_load_balancers(elb_client)
    target_groups = collect_alb_target_groups(elb_client)
    lb_arn_map = {lb['arn']: lb['name'] for lb in load_balancers}

    for lb in load_balancers:
        lb_extra = {k: v for k, v in {
            'environment': lb.get('environment'),
            'system':      lb.get('system'),
            'team':        lb.get('team'),
        }.items() if v is not None}
        lb_vid = upsert_vertex('LoadBalancer', lb['name'], {
            'arn': lb['arn'],
            'dns': lb['dns'],
            'scheme': lb['scheme'],
            'lb_type': lb['type'],
            'health_status': 'healthy',
            **lb_extra,
        }, lb['managed_by'])
        stats['vertices'] += 1
        for az in lb.get('azs', []):
            az_vid, _ = upsert_az_region(az)
            if lb_vid and az_vid:
                upsert_edge(lb_vid, az_vid, 'LocatedIn', {'source': 'aws-etl'})
                stats['edges'] += 1

    lb_vid_map = {lb['name']: _vid_cache.get(('LoadBalancer', lb['name'])) for lb in load_balancers}
    for tg in target_groups:
        for lb_arn in tg.get('lb_arns', []):
            lb_name = lb_arn_map.get(lb_arn)
            if not lb_name:
                continue
            lb_vid = lb_vid_map.get(lb_name)
            if not lb_vid:
                continue
            tg_vid = upsert_vertex('TargetGroup', tg['name'], {
                'port': str(tg['port']),
                'protocol': tg['protocol'],
                'role': 'target-group',
            }, 'cloudformation')
            stats['vertices'] += 1
            if tg_vid:
                upsert_edge(lb_vid, tg_vid, 'RoutesTo', {'source': 'aws-etl'})
                stats['edges'] += 1

    # ── Step 4b: ListenerRule nodes ──────────────────────────────────────────
    try:
        listener_rules = collect_listener_rules(elb_client)
        tg_arn_to_vid = {tg['arn']: None for tg in target_groups}
        for tg in target_groups:
            tg_name_v = upsert_vertex('TargetGroup', tg['name'], {
                'port': str(tg['port']), 'protocol': tg['protocol'], 'role': 'target-group',
            }, 'cloudformation')
            tg_arn_to_vid[tg['arn']] = tg_name_v

        for rule in listener_rules:
            if rule.get('is_default') or not rule.get('tg_arns'):
                continue
            lb_arn = rule['lb_arn']
            lb_name = lb_arn_map.get(lb_arn)
            lb_vid  = lb_vid_map.get(lb_name) if lb_name else None
            if not lb_vid:
                continue
            rule_arn = rule['rule_arn']
            rule_name = rule_arn.split('/')[-1]
            rule_vid = upsert_vertex('ListenerRule', rule_arn, {
                'priority': str(rule.get('priority', '999')),
                'listener_arn': rule.get('listener_arn', ''),
                'short_name': rule_name,
            }, 'cloudformation')
            stats['vertices'] += 1
            if rule_vid:
                upsert_edge(lb_vid, rule_vid, 'HasRule', {'source': 'aws-etl'})
                stats['edges'] += 1
                for tg_arn in rule.get('tg_arns', []):
                    tg_vid = tg_arn_to_vid.get(tg_arn)
                    if tg_vid:
                        upsert_edge(rule_vid, tg_vid, 'ForwardsTo', {'source': 'aws-etl'})
                        stats['edges'] += 1
        logger.info("T05: ListenerRule nodes+edges written")
    except Exception as e:
        logger.warning(f"T05 ListenerRule step failed (non-fatal): {e}")

    # ── Step P0: TargetGroup → Microservice ─────────────────────────────────
    try:
        pod_ip_map = get_pod_ip_to_app_label(eks_client)
        # Build ms_vid_map from config (fix: was referencing undefined `microservices` variable)
        ms_vid_map = {name: _vid_cache.get(('Microservice', name))
                      for name in MICROSERVICE_RECOVERY_PRIORITY.keys()}
        for tg in target_groups:
            tg_vid = _vid_cache.get(('TargetGroup', tg['name']))
            if not tg_vid:
                continue
            ms_name = None
            for ip in tg.get('healthy_targets', []):
                app_label = pod_ip_map.get(ip, '')
                candidate = _K8S_SVC_ALIAS.get(app_label, app_label)
                if candidate in ms_vid_map:
                    ms_name = candidate
                    break
            if not ms_name:
                app_label = TG_APP_LABEL_STATIC.get(tg['name'], '')
                ms_name = _K8S_SVC_ALIAS.get(app_label, app_label)
            if ms_name and ms_name in ms_vid_map and ms_vid_map[ms_name]:
                upsert_edge(tg_vid, ms_vid_map[ms_name], 'ForwardsTo',
                            {'source': 'aws-etl', 'evidence': 'pod-ip-lookup'})
                stats['edges'] += 1
                logger.info(f"P0: TG({tg['name']}) → ForwardsTo → Microservice({ms_name})")
    except Exception as e:
        logger.warning(f"P0 TG→Microservice step failed (non-fatal): {e}")

    # ── Step 5: Lambda functions ──────────────────────────────────────────────
    lambda_fns = collect_lambda_functions(lambda_client)
    fn_vid_map = {}
    for fn in lambda_fns:
        priority = 'Tier2'
        for key, p in LAMBDA_RECOVERY_PRIORITY.items():
            if key in fn['name'].lower():
                priority = p
                break
        if fn.get('tag_tier'):
            priority = fn['tag_tier']
        extra = {k: v for k, v in {
            'environment': fn.get('environment'),
            'system':      fn.get('system'),
            'team':        fn.get('team'),
        }.items() if v is not None}
        fn_vid = upsert_vertex('LambdaFunction', fn['name'], {
            'arn': fn['arn'],
            'runtime': fn['runtime'],
            'recovery_priority': priority,
            'health_status': 'healthy',
            'log_source': f"cwlogs:///aws/lambda/{fn['name']}",
            **extra,
        }, fn['managed_by'])
        fn_vid_map[fn['name']] = fn_vid
        stats['vertices'] += 1

    lambda_cw = fetch_lambda_cloudwatch_metrics_batch(cw_client, lambda_fns)
    for fn in lambda_fns:
        try:
            if update_lambda_metrics(fn['name'], lambda_cw.get(fn['name'], {})):
                stats['cw_lambda'] += 1
        except Exception as e:
            logger.warning(f"Lambda CW metrics {fn['name']}: {e}")

    # ── P1: Ops-tool Lambda static edges ────────────────────────────────────
    try:
        for (src_label, src_name, edge_lbl, dst_label, dst_name) in OPS_TOOL_EDGES:
            src_vid = _vid_cache.get((src_label, src_name))
            dst_vid = _vid_cache.get((dst_label, dst_name))
            if not src_vid:
                src_vid = fn_vid_map.get(src_name) if src_label == 'LambdaFunction' else None
            if not dst_vid:
                dst_vid = fn_vid_map.get(dst_name) if dst_label == 'LambdaFunction' else None
            if src_vid and dst_vid:
                upsert_edge(src_vid, dst_vid, edge_lbl, {'source': 'aws-etl-static'})
                stats['edges'] += 1
    except Exception as e:
        logger.warning(f"P1 OPS_TOOL_EDGES failed (non-fatal): {e}")

    # ── Step 6: DynamoDB ─────────────────────────────────────────────────────
    ddb_tables = collect_dynamodb_tables(ddb_client)
    ddb_vid_map = {}
    for tbl in ddb_tables:
        tbl_vid = upsert_vertex('DynamoDBTable', tbl['name'], {
            'arn': tbl['arn'],
            'status': tbl['status'],
        }, tbl['managed_by'])
        stats['vertices'] += 1
        ddb_vid_map[tbl['name']] = tbl_vid

    for fn in lambda_fns:
        fn_vid = fn_vid_map.get(fn['name'])
        if not fn_vid:
            continue
        for var_name, var_val in fn.get('env_vars', {}).items():
            if isinstance(var_val, str) and var_val in ddb_vid_map:
                tbl_vid = ddb_vid_map[var_val]
                if tbl_vid:
                    upsert_edge(fn_vid, tbl_vid, 'AccessesData', {
                        'source': 'aws-etl', 'evidence': f'env:{var_name}'
                    })
                    stats['edges'] += 1

    fn_name_set = {fn['name'] for fn in lambda_fns}
    fn_arn_to_name = {fn['arn']: fn['name'] for fn in lambda_fns}
    for fn in lambda_fns:
        fn_vid = fn_vid_map.get(fn['name'])
        if not fn_vid:
            continue
        for var_name, var_val in fn.get('env_vars', {}).items():
            if not isinstance(var_val, str):
                continue
            target_name = None
            if ':function:' in var_val:
                clean_arn = var_val.rstrip(':*').rstrip(':')
                target_name = fn_arn_to_name.get(clean_arn) or clean_arn.split(':')[-1]
            elif var_val in fn_name_set and var_val != fn['name']:
                target_name = var_val
            if target_name and target_name in fn_vid_map and target_name != fn['name']:
                upsert_edge(fn_vid, fn_vid_map[target_name], 'Invokes', {
                    'source': 'aws-etl', 'evidence': f'env:{var_name}'
                })
                stats['edges'] += 1

    # ── Step 7: Step Functions ────────────────────────────────────────────────
    sfn_machines = collect_step_functions(sfn_client)
    sfn_vid_map = {}
    for sm in sfn_machines:
        sm_vid = upsert_vertex('StepFunction', sm['name'], {'arn': sm['arn']}, sm['managed_by'])
        sfn_vid_map[sm['name']] = sm_vid
        stats['vertices'] += 1

    for sm in sfn_machines:
        sm_vid = sfn_vid_map.get(sm['name'])
        if not sm_vid:
            continue
        for fn_name in extract_sfn_lambda_refs(sm['definition'], lambda_fns):
            fn_vid = fn_vid_map.get(fn_name)
            if fn_vid:
                upsert_edge(sm_vid, fn_vid, 'Invokes', {'source': 'aws-etl'})
                stats['edges'] += 1

    # ── Step 8: RDS / Aurora + Neptune ───────────────────────────────────────
    try:
        rds_clusters = collect_rds_clusters(rds_client)
    except Exception as _e:
        logger.warning(f"collect_rds_clusters failed (permission?): {_e}")
        rds_clusters = []
    try:
        rds_instances = collect_rds_instances(rds_client)
    except Exception as _e:
        logger.warning(f"collect_rds_instances failed (permission?): {_e}")
        rds_instances = []
    rds_cluster_vid_map = {}

    for cluster in rds_clusters:
        is_neptune = cluster['engine'] == 'neptune'
        vertex_label = 'NeptuneCluster' if is_neptune else 'RDSCluster'
        rds_extra = {k: v for k, v in {
            'environment': cluster.get('environment'),
            'system':      cluster.get('system'),
            'team':        cluster.get('team'),
        }.items() if v is not None}
        _rds_log_source = (
            f"cwlogs:///aws/rds/cluster/{cluster['id']}/error" if not is_neptune else ''
        )
        c_vid = upsert_vertex(vertex_label, cluster['id'], {
            'arn': cluster['arn'],
            'engine': cluster['engine'],
            'engine_version': cluster['engine_version'],
            'status': cluster['status'],
            'endpoint': cluster['endpoint'],
            'reader_endpoint': cluster['reader_endpoint'],
            'log_source': _rds_log_source,
            **rds_extra,
        }, cluster['managed_by'])
        rds_cluster_vid_map[cluster['id']] = c_vid
        stats['vertices'] += 1
        if c_vid and region_vid:
            try:
                upsert_edge(c_vid, region_vid, 'LocatedIn', {'source': 'aws-etl'})
                stats['edges'] += 1
            except Exception as _re:
                logger.debug(f"region_vid upsert_edge: {_re}")

    rds_instance_writer_map = {}
    for cluster in rds_clusters:
        for inst_id, is_writer in cluster.get('member_roles', {}).items():
            rds_instance_writer_map[inst_id] = is_writer

    for inst in rds_instances:
        is_neptune = inst['engine'] == 'neptune'
        vertex_label = 'NeptuneInstance' if is_neptune else 'RDSInstance'
        if inst['id'] in rds_instance_writer_map:
            role = 'writer' if rds_instance_writer_map[inst['id']] else 'reader'
        else:
            role = 'writer' if inst['is_writer'] else 'reader'
        i_vid = upsert_vertex(vertex_label, inst['id'], {
            'arn': inst['arn'],
            'engine': inst['engine'],
            'instance_class': inst['instance_class'],
            'az': inst['az'],
            'status': inst['status'],
            'endpoint': inst['endpoint'],
            'port': inst['port'],
            'role': role,
        }, inst['managed_by'])
        stats['vertices'] += 1
        az_vid, _ = upsert_az_region(inst['az'])
        if i_vid and az_vid:
            upsert_edge(i_vid, az_vid, 'LocatedIn', {'source': 'aws-etl'})
            stats['edges'] += 1
        if inst['cluster_id'] and inst['cluster_id'] in rds_cluster_vid_map:
            c_vid = rds_cluster_vid_map[inst['cluster_id']]
            if i_vid and c_vid:
                upsert_edge(i_vid, c_vid, 'BelongsTo', {'source': 'aws-etl'})
                stats['edges'] += 1

    # ── Step 8b: EKS Pods + Service→Pod + Database→Service ──────────────────
    eks_pods = collect_eks_pods(eks_client, ec2_client)
    pod_vid_map = {}
    for pod in eks_pods:
        p_vid = upsert_vertex('Pod', pod['name'], {
            'namespace':  pod['namespace'],
            'status':     pod['status'],
            'restarts':   pod['restarts'],
            'reason':     pod['reason'],
            'node_name':  pod['node_name'],
            'az':         pod['az'],
        }, 'eks-etl')
        pod_vid_map[pod['name']] = p_vid
        stats['vertices'] += 1

        if pod['az']:
            az_vid, _ = upsert_az_region(pod['az'])
            if p_vid and az_vid:
                upsert_edge(p_vid, az_vid, 'LocatedIn', {'source': 'eks-etl'})
                stats['edges'] += 1

        node_name = pod.get('node_name', '')
        if p_vid and node_name and node_name.startswith('ip-'):
            try:
                ip_parts = node_name.split('.')[0]
                private_ip = ip_parts[3:].replace('-', '.')
                r_ec2 = neptune_query(
                    f"g.V().hasLabel('EC2Instance').has('private_ip','{private_ip}').id().next()"
                )
                ec2_vid_raw = r_ec2.get('result', {}).get('data', {}).get('@value', [])
                if ec2_vid_raw:
                    ec2_vid = ec2_vid_raw[0].get('@value', ec2_vid_raw[0]) if isinstance(ec2_vid_raw[0], dict) else ec2_vid_raw[0]
                    upsert_edge(p_vid, str(ec2_vid), 'RunsOn', {'source': 'eks-etl'})
                    stats['edges'] += 1
            except Exception as e:
                logger.debug(f"T12 Pod→EC2 {node_name}: {e}")

        if pod['service_name']:
            svc_vid = find_vertex_by_name(pod['service_name'])
            if svc_vid and p_vid:
                upsert_edge(svc_vid, p_vid, 'RunsOn', {'source': 'eks-etl'})
                stats['edges'] += 1

    for mapping in SERVICE_DB_MAPPING:
        db_vid = upsert_vertex('Database', mapping['dbname'], {
            'cluster_id': mapping['db_cluster_id'],
            'engine':     mapping['engine'],
        }, 'eks-etl')
        stats['vertices'] += 1
        rds_vid = rds_cluster_vid_map.get(mapping['db_cluster_id'])
        if db_vid and rds_vid:
            upsert_edge(db_vid, rds_vid, 'BelongsTo', {'source': 'eks-etl'})
            stats['edges'] += 1
        svc_vid = find_vertex_by_name(mapping['service'])
        if svc_vid and db_vid:
            upsert_edge(svc_vid, db_vid, 'ConnectsTo', {'source': 'eks-etl'})
            stats['edges'] += 1

    logger.info(f"Step 8b: {len(eks_pods)} pods, {len(SERVICE_DB_MAPPING)} DB mappings")

    # ── Step 8b-post: 覆盖写入 Microservice.ip（只保留当前 Running Pod IP）────────
    try:
        # 1) Drop ALL existing ip properties on Microservice (清除历史堆积)
        neptune_query("g.V().hasLabel('Microservice').properties('ip').drop().iterate()")

        # 2) 重新写入当前 Running Pod IP
        ms_current_ips: dict[str, set] = {}
        for pod in eks_pods:
            if pod['status'] == 'Running' and pod.get('service_name') and pod.get('pod_ip'):
                ms_name = _K8S_SVC_ALIAS.get(pod['service_name'], pod['service_name'])
                ms_current_ips.setdefault(ms_name, set()).add(pod['pod_ip'])
        for ms_name, ips in ms_current_ips.items():
            ms_vid = find_vertex_by_name(ms_name)
            if ms_vid:
                ip_str = safe_str(','.join(sorted(ips)))
                neptune_query(f"g.V('{ms_vid}').property(single,'ip','{ip_str}')")
        logger.info(f"Step 8b-post: {len(ms_current_ips)} Microservice IP overwritten")
    except Exception as e:
        logger.warning(f"Step 8b-post Microservice.ip overwrite failed (non-fatal): {e}")

    # ── Step 8a: Namespace nodes ────────────────────────────────────────────
    ns_vid_map = {}
    try:
        seen_ns = {pod.get('namespace', 'default') for pod in eks_pods}
        seen_ns.update({'default', 'petadoptions', 'awesomeshop'})
        for ns_name in sorted(seen_ns):
            ns_vid = upsert_vertex('Namespace', ns_name, {
                'cluster': EKS_CLUSTER_NAME,
            }, 'eks-etl')
            ns_vid_map[ns_name] = ns_vid
            stats['vertices'] += 1
            if ns_vid and cluster_vid:
                upsert_edge(ns_vid, cluster_vid, 'BelongsTo', {'source': 'eks-etl'})
                stats['edges'] += 1
        logger.info(f"Step 8a: {len(ns_vid_map)} Namespace nodes done")
    except Exception as e:
        logger.warning(f"Step 8a Namespace failed (non-fatal): {e}")

    # ── Step 8a-post: Microservice → Namespace (OwnedBy) ────────────────────
    try:
        if ns_vid_map:
            ms_rows = neptune_query(
                "g.V().hasLabel('Microservice').project('vid','ns').by(id).by(values('namespace'))"
            ).get('result', {}).get('data', {}).get('@value', [])
            owned_count = 0
            for row in ms_rows:
                vals = row.get('@value', []) if isinstance(row, dict) else []
                if len(vals) >= 4:
                    ms_vid_raw = vals[1]
                    ms_ns_raw  = vals[3]
                    ms_vid_str = ms_vid_raw.get('@value', ms_vid_raw) if isinstance(ms_vid_raw, dict) else str(ms_vid_raw)
                    ms_ns_str  = ms_ns_raw.get('@value', ms_ns_raw) if isinstance(ms_ns_raw, dict) else str(ms_ns_raw)
                    ns_vid = ns_vid_map.get(ms_ns_str)
                    if ns_vid and ms_vid_str:
                        upsert_edge(str(ms_vid_str), str(ns_vid), 'OwnedBy', {'source': 'eks-etl'})
                        owned_count += 1
                        stats['edges'] += 1
            logger.info(f"Step 8a-post: {owned_count} Microservice→Namespace OwnedBy edges")
    except Exception as e:
        logger.warning(f"Step 8a-post OwnedBy failed (non-fatal): {e}")



    # ── Step 8c: VPCs ────────────────────────────────────────────────────────
    try:
        vpcs = collect_vpcs(ec2_client)
        vpc_vid_map = {}
        for vpc in vpcs:
            v_vid = upsert_vertex('VPC', vpc['name'], {
                'vpc_id': vpc['vpc_id'],
                'cidr':   vpc['cidr'],
            }, 'cloudformation')
            vpc_vid_map[vpc['vpc_id']] = v_vid
            stats['vertices'] += 1
            region_vid_q = neptune_query(
                f"g.V().hasLabel('Region').has('name','{REGION}').id().next()"
            ).get('result', {}).get('data', {}).get('@value')
            if region_vid_q and v_vid:
                region_id = region_vid_q[0].get('@value', region_vid_q[0]) if isinstance(region_vid_q[0], dict) else region_vid_q[0]
                upsert_edge(v_vid, str(region_id), 'LocatedIn', {'source': 'aws-etl'})
                stats['edges'] += 1

        subnet_nodes = neptune_query(
            "g.V().hasLabel('Subnet').project('id','vpc_id').by(id()).by(coalesce(values('vpc_id'),constant(''))).toList()"
        ).get('result', {}).get('data', {}).get('@value', [])
        for item in subnet_nodes:
            vals = item.get('@value', [])
            kv = dict(zip(vals[::2], vals[1::2])) if len(vals) >= 4 else {}
            subnet_id_raw = kv.get('id', {})
            subnet_vid = subnet_id_raw.get('@value', subnet_id_raw) if isinstance(subnet_id_raw, dict) else subnet_id_raw
            vpc_id_raw  = kv.get('vpc_id', {})
            vpc_id_str  = vpc_id_raw.get('@value', vpc_id_raw) if isinstance(vpc_id_raw, dict) else vpc_id_raw
            vpc_vid = vpc_vid_map.get(str(vpc_id_str))
            if subnet_vid and vpc_vid:
                upsert_edge(str(subnet_vid), vpc_vid, 'LocatedIn', {'source': 'aws-etl'})
                stats['edges'] += 1
        logger.info(f"T09: {len(vpcs)} VPCs + Subnet→VPC edges done")
    except Exception as e:
        logger.warning(f"T09 VPC step failed (non-fatal): {e}")

    # ── Step 8d: SecurityGroups ──────────────────────────────────────────────
    try:
        sgs = collect_security_groups(ec2_client)
        sg_id_to_vid = {}
        for sg in sgs:
            sg_vid = upsert_vertex('SecurityGroup', sg['name'], {
                'sg_id':       sg['sg_id'],
                'description': sg['description'][:200],
                'vpc_id':      sg['vpc_id'],
            }, 'cloudformation')
            sg_id_to_vid[sg['sg_id']] = sg_vid
            stats['vertices'] += 1

        ec2_sg_resp = ec2_client.describe_instances()
        for resv in ec2_sg_resp.get('Reservations', []):
            for inst in resv.get('Instances', []):
                private_ip = inst.get('PrivateIpAddress', '')
                inst_sgs   = [sg['GroupId'] for sg in inst.get('SecurityGroups', [])]
                ec2_r = neptune_query(
                    f"g.V().hasLabel('EC2Instance').has('private_ip','{private_ip}').id().next()"
                ).get('result', {}).get('data', {}).get('@value')
                if not ec2_r:
                    continue
                ec2_vid = ec2_r[0].get('@value', ec2_r[0]) if isinstance(ec2_r[0], dict) else ec2_r[0]
                for sg_id in inst_sgs:
                    sg_vid = sg_id_to_vid.get(sg_id)
                    if sg_vid:
                        upsert_edge(str(ec2_vid), sg_vid, 'HasSG', {'source': 'aws-etl'})
                        stats['edges'] += 1

        try:
            eks_cl = eks_client.describe_cluster(name=EKS_CLUSTER_NAME)['cluster']
            cluster_sg_id = eks_cl.get('resourcesVpcConfig', {}).get('clusterSecurityGroupId', '')
            eks_sg_vid = sg_id_to_vid.get(cluster_sg_id)
            eks_cl_vid_r = neptune_query(
                f"g.V().hasLabel('EKSCluster').has('name','{EKS_CLUSTER_NAME}').id().next()"
            ).get('result', {}).get('data', {}).get('@value')
            if eks_cl_vid_r and eks_sg_vid:
                eks_vid = eks_cl_vid_r[0].get('@value', eks_cl_vid_r[0]) if isinstance(eks_cl_vid_r[0], dict) else eks_cl_vid_r[0]
                upsert_edge(str(eks_vid), eks_sg_vid, 'HasSG', {'source': 'aws-etl'})
                stats['edges'] += 1
        except Exception as e2:
            logger.debug(f"T10 EKS HasSG: {e2}")

        try:
            alb_resp = elb_client.describe_load_balancers()
            for lb in alb_resp.get('LoadBalancers', []):
                lb_name = lb['LoadBalancerName']
                lb_sgs  = lb.get('SecurityGroups', [])
                lb_r = neptune_query(
                    f"g.V().hasLabel('LoadBalancer').has('name','{lb_name}').id().next()"
                ).get('result', {}).get('data', {}).get('@value')
                if not lb_r:
                    continue
                lb_vid = lb_r[0].get('@value', lb_r[0]) if isinstance(lb_r[0], dict) else lb_r[0]
                for sg_id in lb_sgs:
                    sg_vid = sg_id_to_vid.get(sg_id)
                    if sg_vid:
                        upsert_edge(str(lb_vid), sg_vid, 'HasSG', {'source': 'aws-etl'})
                        stats['edges'] += 1
        except Exception as e2:
            logger.debug(f"T10 ALB HasSG: {e2}")

        try:
            neptune_b = boto3.client('neptune', region_name=REGION)
            nc_resp = neptune_b.describe_db_clusters()
            for cluster in nc_resp.get('DBClusters', []):
                if cluster.get('Engine') != 'neptune':
                    continue
                nc_name = cluster['DBClusterIdentifier']
                nc_r = neptune_query(
                    f"g.V().hasLabel('NeptuneCluster').has('name','{nc_name}').id()"
                ).get('result', {}).get('data', {}).get('@value')
                if not nc_r:
                    continue
                nc_vid = nc_r[0].get('@value', nc_r[0]) if isinstance(nc_r[0], dict) else nc_r[0]
                for sg_info in cluster.get('VpcSecurityGroups', []):
                    sg_vid = sg_id_to_vid.get(sg_info.get('VpcSecurityGroupId'))
                    if sg_vid:
                        upsert_edge(str(nc_vid), sg_vid, 'HasSG', {'source': 'aws-etl'})
                        stats['edges'] += 1
        except Exception as _ne:
            logger.debug(f"T10 NeptuneCluster HasSG: {_ne}")

        try:
            rds_b = boto3.client('rds', region_name=REGION)
            for page in rds_b.get_paginator('describe_db_clusters').paginate():
                for cluster in page['DBClusters']:
                    rds_name = cluster['DBClusterIdentifier']
                    rds_r = neptune_query(
                        f"g.V().hasLabel('RDSCluster').has('name','{rds_name}').id()"
                    ).get('result', {}).get('data', {}).get('@value')
                    if not rds_r:
                        continue
                    rds_vid = rds_r[0].get('@value', rds_r[0]) if isinstance(rds_r[0], dict) else rds_r[0]
                    for sg_info in cluster.get('VpcSecurityGroups', []):
                        sg_vid = sg_id_to_vid.get(sg_info.get('VpcSecurityGroupId'))
                        if sg_vid:
                            upsert_edge(str(rds_vid), sg_vid, 'HasSG', {'source': 'aws-etl'})
                            stats['edges'] += 1
        except Exception as _re:
            logger.debug(f"T10 RDSCluster HasSG: {_re}")

        logger.info(f"T10: {len(sgs)} SecurityGroups + HasSG edges done")
    except Exception as e:
        logger.warning(f"T10 SecurityGroup step failed (non-fatal): {e}")

    # ── Step 8e: K8sService nodes ─────────────────────────────────────────────
    try:
        k8s_svcs = collect_k8s_services(eks_client)
        k8s_svc_vid_map = {}
        SKIP_K8S_SVCS = {'kubernetes', 'xray-service'}
        SKIP_K8S_NS = {'kube-system', 'kube-public', 'kube-node-lease',
                       'amazon-cloudwatch', 'amazon-guardduty', 'cert-manager',
                       'chaos-mesh', 'deepflow'}
        for svc in k8s_svcs:
            if svc['name'] in SKIP_K8S_SVCS or svc.get('namespace') in SKIP_K8S_NS:
                continue
            k_vid = upsert_vertex('K8sService', svc['name'], {
                'namespace':  svc['namespace'],
                'svc_type':   svc['type'],
                'cluster_ip': svc['cluster_ip'] or '',
                'app_label':  svc['app_label'],
            }, 'eks-etl')
            k8s_svc_vid_map[svc['name']] = k_vid
            stats['vertices'] += 1

            ms_alias = svc.get('ms_alias', '')
            if ms_alias:
                try:
                    ms_r = neptune_query(
                        f"g.V().hasLabel('Microservice','LambdaFunction').has('name','{ms_alias}').id().fold()"
                    ).get('result', {}).get('data', {}).get('@value', [])
                    # fold() returns [[values]] — unwrap
                    if ms_r and isinstance(ms_r[0], dict):
                        inner = ms_r[0].get('@value', [])
                        if inner:
                            ms_vid = inner[0].get('@value', inner[0]) if isinstance(inner[0], dict) else inner[0]
                            upsert_edge(k_vid, str(ms_vid), 'Implements', {'source': 'eks-etl'})
                            stats['edges'] += 1
                except Exception as e:
                    logger.debug(f"K8sService {svc['name']} Implements edge skip: {e}")

        app_label_to_svc_vid = {svc['app_label']: k8s_svc_vid_map.get(svc['name'])
                                 for svc in k8s_svcs
                                 if svc['name'] not in SKIP_K8S_SVCS
                                 and svc.get('namespace') not in SKIP_K8S_NS}
        for pod in eks_pods:
            svc_vid = app_label_to_svc_vid.get(pod.get('service_name', ''))
            p_vid   = pod_vid_map.get(pod['name'])
            if p_vid and svc_vid:
                upsert_edge(p_vid, svc_vid, 'BelongsTo', {'source': 'eks-etl'})
                upsert_edge(svc_vid, p_vid, 'Routes', {'source': 'eks-etl'})
                stats['edges'] += 2

        logger.info(f"T11: {len(k8s_svcs)} K8sService nodes + edges done")
    except Exception as e:
        logger.warning(f"T11 K8sService step failed (non-fatal): {e}")

    # ── Step 8f: K8s Deployment nodes ────────────────────────────────────────
    try:
        k8s_deploys = collect_k8s_deployments(eks_client)
        deploy_vid_map = {}

        for dep in k8s_deploys:
            dep_vid = upsert_vertex('Deployment', dep['name'], {
                'namespace':        dep['namespace'],
                'replicas':         dep['replicas'],
                'ready_replicas':   dep['ready_replicas'],
                'updated_replicas': dep['updated_replicas'],
                'available':        dep['available'],
                'strategy':         dep['strategy'],
                'app_label':        dep['app_label'],
            }, 'eks-etl')
            deploy_vid_map[dep['name']] = dep_vid
            stats['vertices'] += 1

            # Deployment → Microservice (Manages)
            if dep['ms_alias']:
                ms_vid = find_vertex_by_name(dep['ms_alias'])
                if ms_vid and dep_vid:
                    upsert_edge(dep_vid, ms_vid, 'Manages', {'source': 'eks-etl'})
                    stats['edges'] += 1
                    neptune_query(
                        f"g.V('{ms_vid}').property(single,'replica_count',{dep['replicas']})"
                    )

        # Deployment -[Manages]-> Pod (via app_label)
        deploy_app_map = {dep['app_label']: deploy_vid_map.get(dep['name'])
                          for dep in k8s_deploys if dep['app_label']}
        for pod in eks_pods:
            dep_vid = deploy_app_map.get(pod.get('service_name', ''))
            p_vid = pod_vid_map.get(pod['name'])
            if p_vid and dep_vid:
                upsert_edge(dep_vid, p_vid, 'Manages', {'source': 'eks-etl'})
                stats['edges'] += 1

        logger.info(f"T8f: {len(k8s_deploys)} Deployment nodes + Manages edges done")
    except Exception as e:
        logger.warning(f"T8f Deployment step failed (non-fatal): {e}")

    # ── Step 8g: HPA nodes ───────────────────────────────────────────────────
    try:
        k8s_hpas = collect_k8s_hpas(eks_client)
        for hpa in k8s_hpas:
            hpa_vid = upsert_vertex('HPA', hpa['name'], {
                'namespace':        hpa['namespace'],
                'min_replicas':     hpa['min_replicas'],
                'max_replicas':     hpa['max_replicas'],
                'current_replicas': hpa['current_replicas'],
                'desired_replicas': hpa['desired_replicas'],
                'target_kind':      hpa['target_kind'],
                'target_name':      hpa['target_name'],
            }, 'eks-etl')
            stats['vertices'] += 1

            # HPA → Deployment (Manages)
            if hpa['target_kind'] == 'Deployment':
                dep_vid = deploy_vid_map.get(hpa['target_name'])
                if dep_vid and hpa_vid:
                    upsert_edge(hpa_vid, dep_vid, 'Manages', {'source': 'eks-etl'})
                    stats['edges'] += 1

        logger.info(f"T8g: {len(k8s_hpas)} HPA nodes done")
    except Exception as e:
        logger.warning(f"T8g HPA step failed (non-fatal): {e}")



    # ── Step 9: SQS ───────────────────────────────────────────────────────────
    sqs_queues = collect_sqs_queues(sqs_client)
    sqs_arn_to_name = {}
    sqs_vid_map = {}
    for q in sqs_queues:
        extra = {k: v for k, v in {
            'environment': q.get('environment'),
            'system':      q.get('system'),
            'team':        q.get('team'),
        }.items() if v is not None}
        q_vid = upsert_vertex('SQSQueue', q['name'], {
            'arn': q['arn'],
            'url': q['url'],
            'is_dlq': q['is_dlq'],
            'health_status': 'healthy',
            **extra,
        }, q['managed_by'])
        sqs_vid_map[q['name']] = q_vid
        sqs_arn_to_name[q['arn']] = q['name']
        stats['vertices'] += 1
        if q_vid and region_vid:
            try:
                upsert_edge(q_vid, region_vid, 'LocatedIn', {'source': 'aws-etl'})
                stats['edges'] += 1
            except Exception as _re:
                logger.debug(f"region_vid upsert_edge: {_re}")

    for fn in lambda_fns:
        fn_vid = fn_vid_map.get(fn['name'])
        if not fn_vid:
            continue
        for var_name, var_val in fn.get('env_vars', {}).items():
            if not isinstance(var_val, str):
                continue
            q_name = sqs_arn_to_name.get(var_val) if var_val.startswith('arn:') else (var_val if var_val in sqs_vid_map else None)
            if q_name and sqs_vid_map.get(q_name):
                upsert_edge(fn_vid, sqs_vid_map[q_name], 'AccessesData', {
                    'source': 'aws-etl', 'evidence': f'env:{var_name}'
                })
                stats['edges'] += 1

    try:
        esm_paginator = lambda_client.get_paginator('list_event_source_mappings')
        for page in esm_paginator.paginate():
            for mapping in page.get('EventSourceMappings', []):
                fn_name = mapping.get('FunctionArn', '').split(':')[-1]
                src_arn = mapping.get('EventSourceArn', '')
                esm_state = mapping.get('State', '')
                if ':sqs:' not in src_arn.lower():
                    continue
                q_name = src_arn.split(':')[-1]
                if fn_name in fn_vid_map and q_name in sqs_vid_map:
                    upsert_edge(sqs_vid_map[q_name], fn_vid_map[fn_name], 'TriggeredBy', {
                        'source': 'aws-etl',
                        'call_type': 'async',
                        'esm_state': esm_state,
                    })
                    stats['edges'] += 1
    except Exception as e:
        logger.warning(f"TriggeredBy edges failed (non-fatal): {e}")

    # ── Step 10: SNS ──────────────────────────────────────────────────────────
    sns_topics = collect_sns_topics(sns_client)
    sns_vid_map = {}
    sns_arn_to_name = {}
    for topic in sns_topics:
        t_vid = upsert_vertex('SNSTopic', topic['name'], {
            'arn': topic['arn'],
            'subscriptions_confirmed': topic['subscriptions_confirmed'],
        }, topic['managed_by'])
        sns_vid_map[topic['name']] = t_vid
        sns_arn_to_name[topic['arn']] = topic['name']
        stats['vertices'] += 1
        if t_vid and region_vid:
            try:
                upsert_edge(t_vid, region_vid, 'LocatedIn', {'source': 'aws-etl'})
                stats['edges'] += 1
            except Exception as _re:
                logger.debug(f"region_vid upsert_edge: {_re}")

    for topic in sns_topics:
        t_vid = sns_vid_map.get(topic['name'])
        if not t_vid:
            continue
        try:
            paginator = sns_client.get_paginator('list_subscriptions_by_topic')
            for page in paginator.paginate(TopicArn=topic['arn']):
                for sub in page.get('Subscriptions', []):
                    if sub.get('Protocol') == 'sqs':
                        q_name = sqs_arn_to_name.get(sub.get('Endpoint', ''))
                        if q_name and sqs_vid_map.get(q_name):
                            upsert_edge(t_vid, sqs_vid_map[q_name], 'PublishesTo', {'source': 'aws-etl'})
                            stats['edges'] += 1
        except Exception as e:
            logger.warning(f"SNS subscriptions {topic['name']}: {e}")

    # ── Step 11: S3 ───────────────────────────────────────────────────────────
    s3_buckets = collect_s3_buckets_in_region(s3_client)
    s3_vid_map = {}
    for bucket in s3_buckets:
        b_vid = upsert_vertex('S3Bucket', bucket['name'], {
            'region': bucket['region'],
        }, bucket['managed_by'])
        s3_vid_map[bucket['name']] = b_vid
        stats['vertices'] += 1
        if b_vid and region_vid:
            try:
                upsert_edge(b_vid, region_vid, 'LocatedIn', {'source': 'aws-etl'})
                stats['edges'] += 1
            except Exception as _re:
                logger.debug(f"region_vid upsert_edge: {_re}")

    for fn in lambda_fns:
        fn_vid = fn_vid_map.get(fn['name'])
        if not fn_vid:
            continue
        for var_name, var_val in fn.get('env_vars', {}).items():
            if isinstance(var_val, str) and var_val in s3_vid_map:
                upsert_edge(fn_vid, s3_vid_map[var_val], 'AccessesData', {
                    'source': 'aws-etl', 'evidence': f'env:{var_name}'
                })
                stats['edges'] += 1

    # ── Step 12: ECR ──────────────────────────────────────────────────────────
    ecr_repos = collect_ecr_repositories(ecr_client)
    for repo in ecr_repos:
        r_vid = upsert_vertex('ECRRepository', repo['name'], {
            'arn': repo['arn'],
            'uri': repo['uri'],
        }, repo['managed_by'])
        stats['vertices'] += 1
        if r_vid and region_vid:
            try:
                upsert_edge(r_vid, region_vid, 'LocatedIn', {'source': 'aws-etl'})
                stats['edges'] += 1
            except Exception as _re:
                logger.debug(f"region_vid upsert_edge: {_re}")

    # ── Step 13: BusinessCapability layer ─────────────────────────────────────
    biz_stats = upsert_business_capabilities()
    stats['vertices'] += biz_stats.get('created', 0)
    stats['edges'] += biz_stats.get('edges', 0)

    # ── Step 13b: Microservice → Infra direct deps ────────────────────────────
    ts_now = int(time.time())
    for svc_name, deps in MICROSERVICE_INFRA_DEPS.items():
        # 从 service_mappings.json 反查服务类型（lambda vs k8s）
        _svc_type = 'k8s'
        try:
            _sm_services = _sm_data.get('service_types', {})
            _svc_type = _sm_services.get(svc_name, 'k8s')
        except NameError:
            pass
        svc_vid = upsert_vertex('Microservice', svc_name, {
            'namespace': MICROSERVICE_NAMESPACE.get(svc_name, 'default'),
            'source': 'business-layer',
            'fault_boundary': 'region' if _svc_type == 'lambda' else 'az',
            'region': REGION,
            'recovery_priority': MICROSERVICE_RECOVERY_PRIORITY.get(svc_name, 'Tier2'),
            'service_type': _svc_type,
        }, 'manual')
        if not svc_vid:
            continue
        for dep in deps:
            try:
                nc = dep['name_contains']
                infra_label = dep['label']
                edge_label  = dep['edge']
                evidence    = dep.get('evidence', 'cdk-stack')
                declared_in = dep.get('declared_in', 'etl_aws')
                neptune_query(
                    f"g.V().hasLabel('{infra_label}').has('name', containing('{nc}'))"
                    f".as('infra')"
                    f".V('{svc_vid}')"
                    f".coalesce("
                    f"  __.out('{edge_label}').where(__.hasLabel('{infra_label}').has('name',containing('{nc}'))),"
                    f"  __.addE('{edge_label}').to('infra')"
                    f").property('source','aws-etl')"
                    f".property('evidence','{evidence}')"
                    f".property('declared_in','{declared_in}')"
                    f".property('last_updated',{ts_now})"
                )
                stats['edges'] += 1
            except Exception as e:
                logger.warning(f"T01 edge {svc_name}->{dep}: {e}")

    try:
        neptune_query(
            "g.V().hasLabel('LambdaFunction').has('name',containing('statusupdater'))"
            ".as('fn')"
            ".V().hasLabel('DynamoDBTable').has('name',containing('ddbpetadoption'))"
            f".coalesce("
            f"  __.inE('AccessesData').where(__.outV().hasLabel('LambdaFunction').has('name',containing('statusupdater'))),"
            f"  __.addE('AccessesData').from('fn')"
            f").property('source','aws-etl')"
            f".property('evidence','source:petstatusupdater/index.js#UpdateCommand')"
            f".property('last_updated',{ts_now})"
        )
        stats['edges'] += 1
    except Exception as e:
        logger.warning(f"statusupdater→DynamoDB edge failed: {e}")

    # ── Step 13b-post: sync has_db_dependency ──────────────────────────────────
    try:
        neptune_query(
            "g.V().hasLabel('Microservice')"
            ".where(__.out('AccessesData','ConnectsTo').hasLabel('RDSCluster','DynamoDBTable','Database'))"
            ".property(single,'has_db_dependency',true)"
        )
        neptune_query(
            "g.V().hasLabel('Microservice')"
            ".not(__.out('AccessesData','ConnectsTo').hasLabel('RDSCluster','DynamoDBTable','Database'))"
            ".property(single,'has_db_dependency',false)"
        )
    except Exception as e:
        logger.warning(f"has_db_dependency sync failed: {e}")

    # ── Step 13c: Microservice → EKSCluster LocatedIn ────────────────────────
    try:
        eks_vid_r = neptune_query(
            f"g.V().hasLabel('EKSCluster').has('name','{EKS_CLUSTER_NAME}').id().next()"
        )
        eks_vid_raw = eks_vid_r.get('result', {}).get('data', {}).get('@value', [])
        if eks_vid_raw:
            eks_vid = eks_vid_raw[0].get('@value', eks_vid_raw[0]) if isinstance(eks_vid_raw[0], dict) else eks_vid_raw[0]
            eks_vid = str(eks_vid)
            for svc_name in MICROSERVICE_RECOVERY_PRIORITY.keys():
                if svc_name in ('trafficgenerator',):
                    continue
                svc_vid = upsert_vertex('Microservice', svc_name, {
                    'namespace': 'default', 'source': 'business-layer',
                    'fault_boundary': 'az', 'region': REGION,
                    'recovery_priority': MICROSERVICE_RECOVERY_PRIORITY.get(svc_name, 'Tier2'),
                }, 'manual')
                if svc_vid:
                    upsert_edge(svc_vid, eks_vid, 'LocatedIn', {
                        'source': 'aws-etl', 'last_updated': ts_now
                    })
                    stats['edges'] += 1
            if region_vid:
                upsert_edge(eks_vid, region_vid, 'LocatedIn', {
                    'source': 'aws-etl', 'last_updated': ts_now
                })
                stats['edges'] += 1
    except Exception as e:
        logger.warning(f"T02 EKS LocatedIn step failed: {e}")

    # ── Step 13d: Tag monitoring RDS ─────────────────────────────────────────
    try:
        neptune_query(
            "g.V().hasLabel('RDSCluster','RDSInstance')"
            ".has('name', containing('grafana'))"
            ".sideEffect(__.properties('system_type').drop())"
            ".property('system_type','monitoring').iterate()"
        )
    except Exception as e:
        logger.warning(f"T06 grafana tag failed: {e}")

    # ── Step 14: ETL Lambda → NeptuneCluster WritesTo ─────────────────────────
    ETL_LAMBDA_NAMES = [
        'neptune-etl-from-aws',
        'neptune-etl-from-deepflow',
        'neptune-etl-from-cfn',
    ]
    NEPTUNE_CLUSTER_NAME = os.environ.get('NEPTUNE_CLUSTER_NAME', 'your-neptune-cluster')
    try:
        nc_vid_raw = neptune_query(
            f"g.V().has('NeptuneCluster','name','{NEPTUNE_CLUSTER_NAME}').id()"
        )['result']['data']['@value']

        def _extract_vid(raw):
            if not raw: return None
            v = raw[0]
            if isinstance(v, dict): return v.get('@value', str(v))
            return str(v)

        nc_vid = _extract_vid(nc_vid_raw)
        for etl_fn in ETL_LAMBDA_NAMES:
            etl_vid_resp = neptune_query(
                f"g.V().has('LambdaFunction','name','{etl_fn}').id()"
            )['result']['data']['@value']
            etl_vid = _extract_vid(etl_vid_resp)
            if etl_vid:
                neptune_query(f"g.V('{etl_vid}').property(single,'managed_by','etl')")
                if nc_vid:
                    upsert_edge(etl_vid, nc_vid, 'WritesTo', {'source': 'aws-etl'})
                    stats['edges'] += 1
    except Exception as e:
        logger.warning(f"Step 14 ETL→Neptune WritesTo failed (non-fatal): {e}")

    # ── Step 15: GC ───────────────────────────────────────────────────────────
    gc_total = run_gc(
        session, ec2_client, eks_client, elb_client, lambda_client,
        sfn_client, ddb_client, rds_client, sqs_client, sns_client,
        s3_client, ecr_client,
    )
    if gc_total:
        stats['gc_dropped'] = gc_total

    # ── Step 16: CloudWatch Alarms → health_status ────────────────────────────
    try:
        DIM_TO_LABEL = {
            'InstanceId':           ('EC2Instance', 'instance_id'),
            'FunctionName':         ('LambdaFunction', 'name'),
            'QueueName':            ('SQSQueue', 'name'),
            'DBInstanceIdentifier': ('RDSInstance', 'id'),
            'LoadBalancer':         ('LoadBalancer', 'arn_suffix'),
        }
        alarm_pager = cw_client.get_paginator('describe_alarms')
        degraded_count = 0
        for page in alarm_pager.paginate(StateValue='ALARM'):
            for alarm in page.get('MetricAlarms', []):
                for dim in alarm.get('Dimensions', []):
                    name_key = dim.get('Name', '')
                    val = dim.get('Value', '')
                    if name_key not in DIM_TO_LABEL or not val:
                        continue
                    label, prop = DIM_TO_LABEL[name_key]
                    vids = neptune_query(
                        f"g.V().hasLabel('{label}').has('{prop}','{safe_str(val)}').id()"
                    ).get('result', {}).get('data', {}).get('@value', [])
                    if vids:
                        vid = vids[0]
                        vid = vid.get('@value', vid) if isinstance(vid, dict) else vid
                        alarm_n = safe_str(alarm.get('AlarmName', ''))
                        neptune_query(
                            f"g.V('{vid}').property(single,'health_status','degraded')"
                            f".property(single,'alarm_name','{alarm_n}')"
                        )
                        degraded_count += 1
                    break
        if degraded_count:
            logger.info(f"health_status=degraded: {degraded_count} nodes (CloudWatch ALARM)")
        else:
            logger.info("health_status: all nodes healthy (no active ALARMs)")
    except Exception as e:
        logger.warning(f"CW Alarm health_status failed (non-fatal): {e}")

    # ── Step 17: Schema cleanup ───────────────────────────────────────────────
    try:
        neptune_query(
            "g.E().hasLabel('Serves')"
            ".where(outV().hasLabel('BusinessCapability'))"
            ".drop().iterate()"
        )
    except Exception as e:
        logger.warning(f"T04 Serves edge cleanup failed (non-fatal): {e}")

    try:
        neptune_query(
            "g.V().hasLabel('Microservice').has('name', within('xray-daemon','xray-service'))"
            ".drop().iterate()"
        )
    except Exception as e:
        logger.warning(f"T03 xray-daemon cleanup failed (non-fatal): {e}")

    try:
        neptune_query(
            "g.V().hasLabel('Microservice').has('source','aws-etl').drop().iterate()"
        )
    except Exception as e:
        logger.warning(f"Step 17c Microservice aws-etl cleanup failed (non-fatal): {e}")

    # ── Step 17d: 清理占位符值 -1 ──────────────────────────────────────────
    try:
        neptune_query(
            "g.V().hasLabel('Microservice').has('error_rate',-1.0)"
            ".sideEffect(__.properties('error_rate').drop()).iterate()"
        )
        neptune_query(
            "g.V().hasLabel('Microservice').has('replica_count',-1)"
            ".sideEffect(__.properties('replica_count').drop()).iterate()"
        )
    except Exception as e:
        logger.warning(f"Step 17d placeholder cleanup failed (non-fatal): {e}")

    logger.info(f"=== neptune-etl-from-aws complete: {stats} ===")
    return stats


def handler(event, context):
    """Lambda entry point."""
    try:
        result = run_etl()
        return {"statusCode": 200, "body": result}
    except Exception as e:
        logger.error(f"ETL failed: {e}", exc_info=True)
        raise
