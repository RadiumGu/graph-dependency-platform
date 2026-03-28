"""
cloudwatch.py - CloudWatch metrics collection for EC2 and Lambda nodes.
"""

import datetime
import logging
import boto3
from neptune_client import neptune_query, safe_str
from config import REGION, EKS_CLUSTER_NAME

logger = logging.getLogger()


def get_cloudwatch_metric(cw_client, namespace, metric_name, dimensions, stat, period_sec, lookback_min):
    try:
        end_time = datetime.datetime.utcnow()
        start_time = end_time - datetime.timedelta(minutes=lookback_min)
        resp = cw_client.get_metric_statistics(
            Namespace=namespace, MetricName=metric_name,
            Dimensions=dimensions, StartTime=start_time, EndTime=end_time,
            Period=period_sec, Statistics=[stat],
        )
        datapoints = sorted(resp.get('Datapoints', []), key=lambda x: x['Timestamp'])
        return float(datapoints[-1].get(stat, -1)) if datapoints else -1.0
    except Exception as e:
        logger.warning(f"CW {namespace}/{metric_name}: {e}")
        return -1.0


def discover_cwagent_disk_dims(cw_client, instances: list) -> dict:
    result = {}
    for inst in instances:
        iid = inst['id']
        try:
            r = cw_client.list_metrics(
                Namespace='CWAgent',
                MetricName='disk_used_percent',
                Dimensions=[
                    {'Name': 'InstanceId', 'Value': iid},
                    {'Name': 'path', 'Value': '/'},
                ]
            )
            if r.get('Metrics'):
                result[iid] = r['Metrics'][0]['Dimensions']
        except Exception as e:
            logger.warning(f"discover_cwagent_disk_dims {iid}: {e}")
    return result


def fetch_ec2_cloudwatch_metrics_batch(cw_client, instances: list) -> dict:
    if not instances:
        return {}
    end_time = datetime.datetime.utcnow()
    start_time = end_time - datetime.timedelta(minutes=15)

    non_eks_instances = [i for i in instances if not i.get('is_eks_node')]
    cwagent_disk_dims = discover_cwagent_disk_dims(cw_client, non_eks_instances) if non_eks_instances else {}

    queries = []
    id_map = {}
    for inst in instances:
        iid = inst['id']
        safe_id = iid.replace('-', '_')
        id_map[f"cpu_{safe_id}"] = (iid, 'cpu_util_avg')
        id_map[f"netin_{safe_id}"] = (iid, 'network_in_bytes')
        id_map[f"netout_{safe_id}"] = (iid, 'network_out_bytes')
        dims = [{'Name': 'InstanceId', 'Value': iid}]
        queries += [
            {'Id': f"cpu_{safe_id}", 'MetricStat': {'Metric': {'Namespace': 'AWS/EC2', 'MetricName': 'CPUUtilization', 'Dimensions': dims}, 'Period': 300, 'Stat': 'Average'}},
            {'Id': f"netin_{safe_id}", 'MetricStat': {'Metric': {'Namespace': 'AWS/EC2', 'MetricName': 'NetworkIn', 'Dimensions': dims}, 'Period': 300, 'Stat': 'Average'}},
            {'Id': f"netout_{safe_id}", 'MetricStat': {'Metric': {'Namespace': 'AWS/EC2', 'MetricName': 'NetworkOut', 'Dimensions': dims}, 'Period': 300, 'Stat': 'Average'}},
        ]
        if inst.get('is_eks_node'):
            node_name = inst.get('private_dns', '')
            if node_name:
                ci_dims = [
                    {'Name': 'InstanceId', 'Value': iid},
                    {'Name': 'NodeName', 'Value': node_name},
                    {'Name': 'ClusterName', 'Value': EKS_CLUSTER_NAME},
                ]
                id_map[f"mem_{safe_id}"] = (iid, 'memory_util')
                id_map[f"disk_{safe_id}"] = (iid, 'disk_util')
                queries += [
                    {'Id': f"mem_{safe_id}", 'MetricStat': {'Metric': {'Namespace': 'ContainerInsights', 'MetricName': 'node_memory_utilization', 'Dimensions': ci_dims}, 'Period': 300, 'Stat': 'Average'}},
                    {'Id': f"disk_{safe_id}", 'MetricStat': {'Metric': {'Namespace': 'ContainerInsights', 'MetricName': 'node_filesystem_utilization', 'Dimensions': ci_dims}, 'Period': 300, 'Stat': 'Average'}},
                ]
        else:
            inst_type = inst.get('instance_type', '')
            if inst_type:
                cwa_mem_dims = [
                    {'Name': 'InstanceId', 'Value': iid},
                    {'Name': 'InstanceType', 'Value': inst_type},
                ]
                id_map[f"cwmem_{safe_id}"] = (iid, 'memory_util')
                queries.append({'Id': f"cwmem_{safe_id}", 'MetricStat': {'Metric': {'Namespace': 'CWAgent', 'MetricName': 'mem_used_percent', 'Dimensions': cwa_mem_dims}, 'Period': 300, 'Stat': 'Average'}})
            if iid in cwagent_disk_dims:
                id_map[f"cwdisk_{safe_id}"] = (iid, 'disk_util')
                queries.append({'Id': f"cwdisk_{safe_id}", 'MetricStat': {'Metric': {'Namespace': 'CWAgent', 'MetricName': 'disk_used_percent', 'Dimensions': cwagent_disk_dims[iid]}, 'Period': 300, 'Stat': 'Average'}})

    results = {}
    try:
        for i in range(0, len(queries), 500):
            resp = cw_client.get_metric_data(MetricDataQueries=queries[i:i+500], StartTime=start_time, EndTime=end_time)
            for r in resp.get('MetricDataResults', []):
                mid = r['Id']
                if mid not in id_map:
                    continue
                iid, metric_key = id_map[mid]
                val = r['Values'][0] if r.get('Values') else -1.0
                if iid not in results:
                    results[iid] = {}
                results[iid][metric_key] = val
    except Exception as e:
        logger.warning(f"EC2 batch CW failed: {e}")

    out = {}
    for inst in instances:
        iid = inst['id']
        raw = results.get(iid, {})
        cpu = raw.get('cpu_util_avg', -1.0)
        net_in = raw.get('network_in_bytes', -1.0)
        net_out = raw.get('network_out_bytes', -1.0)
        mem = raw.get('memory_util', -1.0)
        disk = raw.get('disk_util', -1.0)
        out[iid] = {
            'cpu_util_avg': round(cpu, 2) if cpu >= 0 else -1.0,
            'network_in_mbps': round(net_in / 300 / 1024 / 1024 * 8, 4) if net_in >= 0 else -1.0,
            'network_out_mbps': round(net_out / 300 / 1024 / 1024 * 8, 4) if net_out >= 0 else -1.0,
            'memory_util': round(mem, 2) if mem >= 0 else -1.0,
            'disk_util': round(disk, 2) if disk >= 0 else -1.0,
        }
    return out


def fetch_lambda_cloudwatch_metrics_batch(cw_client, fns: list) -> dict:
    if not fns:
        return {}
    end_time = datetime.datetime.utcnow()
    start_time = end_time - datetime.timedelta(minutes=30)
    queries = []
    id_map = {}
    for fn in fns:
        fname = fn['name']
        safe_name = ''.join(c if c.isalnum() else '_' for c in fname)[:60]
        dims = [{'Name': 'FunctionName', 'Value': fname}]
        id_map[f"dur_p99_{safe_name}"] = (fname, 'p99_duration_ms')
        id_map[f"inv_{safe_name}"] = (fname, 'invocations')
        id_map[f"err_{safe_name}"] = (fname, 'errors')
        id_map[f"thr_{safe_name}"] = (fname, 'throttles')
        id_map[f"conc_{safe_name}"] = (fname, 'concurrent_executions')
        queries += [
            {'Id': f"dur_p99_{safe_name}", 'MetricStat': {'Metric': {'Namespace': 'AWS/Lambda', 'MetricName': 'Duration', 'Dimensions': dims}, 'Period': 900, 'Stat': 'p99'}},
            {'Id': f"inv_{safe_name}", 'MetricStat': {'Metric': {'Namespace': 'AWS/Lambda', 'MetricName': 'Invocations', 'Dimensions': dims}, 'Period': 900, 'Stat': 'Sum'}},
            {'Id': f"err_{safe_name}", 'MetricStat': {'Metric': {'Namespace': 'AWS/Lambda', 'MetricName': 'Errors', 'Dimensions': dims}, 'Period': 900, 'Stat': 'Sum'}},
            {'Id': f"thr_{safe_name}", 'MetricStat': {'Metric': {'Namespace': 'AWS/Lambda', 'MetricName': 'Throttles', 'Dimensions': dims}, 'Period': 900, 'Stat': 'Sum'}},
            {'Id': f"conc_{safe_name}", 'MetricStat': {'Metric': {'Namespace': 'AWS/Lambda', 'MetricName': 'ConcurrentExecutions', 'Dimensions': dims}, 'Period': 900, 'Stat': 'Average'}},
        ]
    raw_results = {}
    try:
        for i in range(0, len(queries), 500):
            resp = cw_client.get_metric_data(MetricDataQueries=queries[i:i+500], StartTime=start_time, EndTime=end_time)
            for r in resp.get('MetricDataResults', []):
                mid = r['Id']
                if mid not in id_map:
                    continue
                fname, metric_key = id_map[mid]
                val = r['Values'][0] if r.get('Values') else -1.0
                if fname not in raw_results:
                    raw_results[fname] = {}
                raw_results[fname][metric_key] = val
    except Exception as e:
        logger.warning(f"Lambda batch CW failed: {e}")

    fn_memory_map = {fn['name']: fn.get('memory_size', -1) for fn in fns}
    out = {}
    for fn in fns:
        fname = fn['name']
        raw = raw_results.get(fname, {})
        p99_dur = raw.get('p99_duration_ms', -1.0)
        inv = raw.get('invocations', -1.0)
        err = raw.get('errors', -1.0)
        thr = raw.get('throttles', -1.0)
        conc = raw.get('concurrent_executions', -1.0)
        out[fname] = {
            'p99_duration_ms': round(p99_dur, 2) if p99_dur >= 0 else -1.0,
            'error_rate': round(err / inv, 4) if inv > 0 and err >= 0 else -1.0,
            'throttle_rate': round(thr / inv, 4) if inv > 0 and thr >= 0 else -1.0,
            'invocations_per_min': round(inv / 30, 2) if inv >= 0 else -1.0,
            'concurrent_executions': round(conc, 2) if conc >= 0 else -1.0,
            'memory_size_mb': fn_memory_map.get(fname, -1),
        }
    return out


def fetch_nfm_ec2_metrics(cw_client) -> dict:
    result = {}
    try:
        nfm = boto3.client('networkflowmonitor', region_name=REGION)
        monitors = nfm.list_monitors().get('monitors', [])
        for m in monitors:
            monitor_arn = m.get('monitorArn', '')
            monitor_name = m.get('monitorName', '')
            if not monitor_arn:
                continue
            dims = [{'Name': 'MonitorId', 'Value': monitor_arn}]
            end = datetime.datetime.utcnow()
            start = end - datetime.timedelta(minutes=5)

            def _get_stat(metric_name, stat):
                try:
                    r = cw_client.get_metric_statistics(
                        Namespace='AWS/NetworkFlowMonitor',
                        MetricName=metric_name,
                        Dimensions=dims,
                        StartTime=start,
                        EndTime=end,
                        Period=300,
                        Statistics=[stat],
                    )
                    pts = sorted(r.get('Datapoints', []), key=lambda x: x['Timestamp'], reverse=True)
                    return pts[0][stat] if pts else -1.0
                except Exception:
                    return -1.0

            rtt_avg  = _get_stat('RoundTripTime', 'Average')
            retrans  = _get_stat('Retransmissions', 'Sum')
            health   = _get_stat('HealthIndicator', 'Average')
            timeouts = _get_stat('Timeouts', 'Sum')
            result[monitor_arn] = {
                'net_rtt_avg_ms':      rtt_avg,
                'net_retransmissions': retrans,
                'net_health_score':    health,
                'net_timeouts':        timeouts,
                'monitor_name':        monitor_name,
            }
            logger.info(f"NFM monitor {monitor_name}: rtt={rtt_avg:.1f}ms retrans={retrans} health={health}")
    except Exception as e:
        logger.warning(f"fetch_nfm_ec2_metrics failed (non-fatal): {e}")
    return result


def map_nfm_metrics_to_ec2(nfm_metrics: dict, ec2_instances: list) -> dict:
    if not nfm_metrics:
        return {}
    ec2_nfm = {}
    try:
        nfm = boto3.client('networkflowmonitor', region_name=REGION)
        for monitor_arn, metrics in nfm_metrics.items():
            monitor_name = metrics.get('monitor_name', '')
            if not monitor_name:
                continue
            try:
                detail = nfm.get_monitor(monitorName=monitor_name)
                local_resources = detail.get('localResources', [])
                vpc_ids = {r.get('identifier', '').split('/')[-1]
                           for r in local_resources if 'vpc' in r.get('identifier', '')}
            except Exception:
                vpc_ids = set()
            for inst in ec2_instances:
                if inst.get('vpc_id') in vpc_ids or not vpc_ids:
                    ec2_nfm[inst['name']] = {k: v for k, v in metrics.items()
                                              if k != 'monitor_name'}
    except Exception as e:
        logger.warning(f"map_nfm_metrics_to_ec2 failed (non-fatal): {e}")
    return ec2_nfm


def update_ec2_nfm_metrics(name: str, metrics: dict):
    import time
    try:
        n = safe_str(name)
        ts = int(time.time())
        props = f".property(single,'nfm_updated_at',{ts})"
        for key in ['net_rtt_avg_ms', 'net_retransmissions', 'net_health_score', 'net_timeouts']:
            v = float(metrics.get(key, -1.0))
            props += f".property(single,'{key}',{v:.4f})"
        neptune_query(f"g.V().has('EC2Instance', 'name', '{n}'){props}")
    except Exception as e:
        logger.error(f"update_ec2_nfm_metrics {name}: {e}")


def update_ec2_metrics(name, metrics):
    import time
    try:
        n = safe_str(name)
        ts = int(time.time())
        props = f".property(single,'cw_updated_at',{ts})"
        for key in ['cpu_util_avg', 'network_in_mbps', 'network_out_mbps', 'memory_util', 'disk_util']:
            props += f".property(single,'{key}',{float(metrics.get(key, -1.0)):.4f})"
        neptune_query(f"g.V().has('EC2Instance', 'name', '{n}'){props}")
        return True
    except Exception as e:
        logger.error(f"update_ec2_metrics {name}: {e}")
        return False


def update_lambda_metrics(name, metrics):
    import time
    try:
        n = safe_str(name)
        ts = int(time.time())
        props = f".property(single,'cw_updated_at',{ts})"
        for key in ['p99_duration_ms', 'error_rate', 'throttle_rate', 'invocations_per_min', 'concurrent_executions']:
            props += f".property(single,'{key}',{float(metrics.get(key, -1.0)):.4f})"
        props += f".property(single,'memory_size_mb',{int(metrics.get('memory_size_mb', -1))})"
        neptune_query(f"g.V().has('LambdaFunction', 'name', '{n}'){props}")
        return True
    except Exception as e:
        logger.error(f"update_lambda_metrics {name}: {e}")
        return False
