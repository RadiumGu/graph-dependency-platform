"""
cloudwatch.py - CloudWatch metrics collection for EC2 and Lambda nodes.
"""

import datetime
import logging
import time
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
    """
    从 CloudWatch 取 NFM 的**监视器级（= VPC 级）**聚合指标。

    ⚠️ 返回值是**每个监视器一份聚合值**，不是每个实例一份。
    监视器的 localResources 是 `AWS::EC2::VPC`，所以这份数字描述的是整个 VPC。
    绝不能逐个复制给 VPC 内的实例 —— 见 map_nfm_metrics_to_ec2 的注释。
    per-instance 数据请用 fetch_nfm_per_flow_metrics()。
    """
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


# NFM per-flow 查询的 destination category。
# 实测（2026-08-29，petsite-nfm-monitor）各类别的数据量：
#   INTRA_AZ 5 条 / INTER_AZ 5 条 / UNCLASSIFIED 5 条
#   INTER_VPC / AMAZON_S3 / AMAZON_DYNAMODB 均 0 条
# INTER_AZ **有数据且有重传**，这对本项目的 fault_boundary='az' 模型直接相关，
# 所以必须单独统计，不能与 INTRA_AZ 混在一起。
NFM_FLOW_CATEGORIES = ('INTRA_AZ', 'INTER_AZ', 'UNCLASSIFIED')
NFM_FLOW_QUERY_TIMEOUT = 40
NFM_FLOW_LIMIT = 50


def fetch_nfm_per_flow_metrics(window_minutes: int = 60) -> dict:
    """
    用 NFM 的 top-contributors 查询取**逐流**网络质量，按实例归集。

    ## 为什么必须走这条路

    原实现只取监视器级（VPC 级）聚合，再把同一份数字复制给 VPC 内每个实例。
    实测后果：7 个 EC2 节点的 `net_rtt_avg_ms` 全部等于 38.25 ——
    「PetSite-Node-az1a-1 的 RTT 是 38.25ms」这句话是**假的**，
    那是整个 VPC 的平均值。这与把泛化的 X-Ray `S3` 节点当成某个具体 bucket
    是同一类错误：**把粗粒度观测归属到细粒度实体**。

    per-flow 查询给出的字段（实测）：
      localIp / localInstanceId / localAz / localSubnetId
      remoteIp / remoteInstanceId / remoteAz / remoteSubnetId / targetPort
      value（该指标在这条流上的值）
      traversedConstructs（真实网络路径：Instance → ENI → ENI → Instance）
      kubernetesMetadata（localServiceName/localPodName/localPodNamespace + remote 同理）

    返回 {instance_id: {net_retransmissions_flow, net_retrans_inter_az,
                        net_flow_count, nfm_scope, ...}}
    —— 键是**实例 ID**（不可变），不是 name。

    注意：这里刻意**不**写服务对之间的边。NFM 的 kubernetesMetadata 足以支撑
    「service → service + TCP 质量」的边（那会让 NFM 成为图谱里第三个平行拓扑源），
    但那是独立一块工作，半做出来只会留下一批语义不明的边。
    """
    per_inst = {}
    try:
        nfm = boto3.client('networkflowmonitor', region_name=REGION)
        monitors = nfm.list_monitors().get('monitors', [])
        end = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
        start = end - datetime.timedelta(minutes=window_minutes)
        ts_fmt = '%Y-%m-%dT%H:%M:%S'

        for m in monitors:
            monitor_name = m.get('monitorName', '')
            if not monitor_name:
                continue
            for category in NFM_FLOW_CATEGORIES:
                try:
                    q = nfm.start_query_monitor_top_contributors(
                        monitorName=monitor_name,
                        startTime=start.strftime(ts_fmt),
                        endTime=end.strftime(ts_fmt),
                        metricName='RETRANSMISSIONS',
                        destinationCategory=category,
                        limit=NFM_FLOW_LIMIT,
                    )
                    qid = q.get('queryId')
                    if not qid:
                        continue
                    # 轮询等结果。NFM 的查询是异步的，实测 SUCCEEDED 约需 10 秒。
                    waited = 0
                    status = ''
                    while waited < NFM_FLOW_QUERY_TIMEOUT:
                        time.sleep(4)
                        waited += 4
                        st = nfm.get_query_status_monitor_top_contributors(
                            monitorName=monitor_name, queryId=qid)
                        status = st.get('status', '')
                        if status in ('SUCCEEDED', 'FAILED'):
                            break
                    if status != 'SUCCEEDED':
                        logger.warning(
                            "NFM per-flow 查询未成功 monitor=%s category=%s status=%s",
                            monitor_name, category, status or 'TIMEOUT')
                        continue
                    res = nfm.get_query_results_monitor_top_contributors(
                        monitorName=monitor_name, queryId=qid)
                    for c in res.get('topContributors', []) or []:
                        val = float(c.get('value') or 0)
                        # 一条流的两端都要记账：本端发生重传，对端也在这条路径上。
                        for side in ('local', 'remote'):
                            iid = c.get(f'{side}InstanceId')
                            if not iid:
                                continue
                            e = per_inst.setdefault(iid, {
                                'net_retransmissions_flow': 0.0,
                                'net_retrans_inter_az': 0.0,
                                'net_flow_count': 0,
                            })
                            e['net_retransmissions_flow'] += val
                            e['net_flow_count'] += 1
                            if category == 'INTER_AZ':
                                e['net_retrans_inter_az'] += val
                except Exception as exc:  # noqa: BLE001
                    # 单个 category 失败不该让整轮 NFM 采集失败 ——
                    # 少一个类别只是覆盖面变窄，抛出去连已拿到的也白跑。
                    logger.warning("NFM per-flow 查询失败 monitor=%s category=%s: %s",
                                   monitor_name, category, exc)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"fetch_nfm_per_flow_metrics failed (non-fatal): {e}")

    for iid, e in per_inst.items():
        # nfm_scope 显式标注这批数字的真实粒度，避免下游再次把聚合当单机。
        e['nfm_scope'] = 'instance'
    logger.info("NFM per-flow: %d 个实例有逐流数据", len(per_inst))
    return per_inst


def map_nfm_metrics_to_ec2(nfm_metrics: dict, ec2_instances: list) -> dict:
    """
    ⚠️ 已废弃，保留仅为兼容尚未切换的调用方。**新代码不要用它。**

    这个函数把监视器级（VPC 级）的聚合指标**逐个复制给 VPC 内每个 EC2 实例**：

        for inst in ec2_instances:
            if inst.get('vpc_id') in vpc_ids or not vpc_ids:
                ec2_nfm[inst['name']] = {...}    # ← 同一份聚合值抄 N 遍

    实测后果：7 个节点的 net_rtt_avg_ms 全部是 38.25，即整个 VPC 的平均值
    被当成了每台机器各自的 RTT。

    另有一处更危险的兜底：`or not vpc_ids` —— get_monitor 一旦失败，
    vpc_ids 为空集，于是**账号内所有实例**都会被写上这份指标，无论在哪个 VPC。
    静默、无报错，写进去的数据与真实数据在图谱里无法区分。

    正确做法：per-instance 用 fetch_nfm_per_flow_metrics()；
    VPC 级聚合应写到 VPC 节点上（见 update_vpc_nfm_metrics）。
    """
    logger.warning(
        "map_nfm_metrics_to_ec2 已废弃：它把 VPC 级聚合复制给每个实例，"
        "使「某台机器的 RTT」变成假数据。请改用 fetch_nfm_per_flow_metrics()。")
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
                # 去掉了原先的 `or not vpc_ids` 兜底：拿不到监控范围时
                # 宁可不写，也不能给所有实例写上无法与真实数据区分的假值。
                if vpc_ids and inst.get('vpc_id') in vpc_ids:
                    ec2_nfm[inst['name']] = {k: v for k, v in metrics.items()
                                              if k != 'monitor_name'}
    except Exception as e:
        logger.warning(f"map_nfm_metrics_to_ec2 failed (non-fatal): {e}")
    return ec2_nfm


def update_vpc_nfm_metrics(nfm_metrics: dict):
    """
    把监视器级聚合指标写到它真正描述的实体 —— **VPC 节点**上。

    这是 D.3 缺陷的正解：数字本身没错，错的是归属对象。
    写到 VPC 上之后，「整个 VPC 的平均 RTT 是 38.25ms」是一句真话，
    而「某台机器的 RTT 是 38.25ms」是假话。
    """
    written = 0
    try:
        nfm = boto3.client('networkflowmonitor', region_name=REGION)
        ts = int(time.time())
        for monitor_arn, metrics in (nfm_metrics or {}).items():
            monitor_name = metrics.get('monitor_name', '')
            if not monitor_name:
                continue
            try:
                detail = nfm.get_monitor(monitorName=monitor_name)
                vpc_ids = [r.get('identifier', '').split('/')[-1]
                           for r in detail.get('localResources', [])
                           if 'vpc' in r.get('identifier', '')]
            except Exception as exc:  # noqa: BLE001
                logger.warning("get_monitor(%s) 失败，跳过 VPC 级写入: %s",
                               monitor_name, exc)
                continue
            for vpc_id in vpc_ids:
                props = (f".property(single,'nfm_scope','vpc')"
                         f".property(single,'nfm_monitor','{safe_str(monitor_name)}')"
                         f".property(single,'nfm_updated_at',{ts})")
                for k, v in metrics.items():
                    if k == 'monitor_name':
                        continue
                    props += f".property(single,'{safe_str(k)}',{float(v)})"
                try:
                    neptune_query(
                        f"g.V().has('VPC','vpc_id','{safe_str(vpc_id)}'){props}")
                    written += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("写 VPC %s 的 NFM 指标失败: %s", vpc_id, exc)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"update_vpc_nfm_metrics failed (non-fatal): {e}")
    logger.info("NFM VPC 级指标已写入 %d 个 VPC 节点", written)
    return written


def update_ec2_nfm_per_flow(per_inst: dict):
    """
    把 per-flow 归集出的**实例级**指标写到 EC2 节点上。

    以 `instance_id` 定位节点（不可变），不用 name —— name 来自 Name 标签，
    可变，正是造成节点重复的根源。
    """
    written = 0
    ts = int(time.time())
    for iid, metrics in (per_inst or {}).items():
        props = f".property(single,'nfm_updated_at',{ts})"
        for k, v in metrics.items():
            if isinstance(v, str):
                props += f".property(single,'{safe_str(k)}','{safe_str(v)}')"
            else:
                props += f".property(single,'{safe_str(k)}',{float(v)})"
        try:
            neptune_query(
                f"g.V().has('EC2Instance','instance_id','{safe_str(iid)}'){props}")
            written += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("写实例 %s 的 per-flow 指标失败: %s", iid, exc)
    logger.info("NFM per-flow 指标已写入 %d 个 EC2 节点", written)
    return written


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
