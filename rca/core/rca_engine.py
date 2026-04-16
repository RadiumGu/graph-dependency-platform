"""
rca_engine.py - 根因分析引擎（Phase 3）

5步分析流程：
1. DeepFlow 调用链：找最早出现 5xx 的服务
2. CloudTrail：故障前 30 分钟内的变更事件
3. Neptune 图谱：反向遍历依赖链，找根因候选
4. 置信度评分
5. 输出 RCA 报告
"""
import os, json, logging, time, boto3
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)

REGION = os.environ.get('REGION', 'ap-northeast-1')
CH_HOST = os.environ.get('CLICKHOUSE_HOST', '')
CH_PORT = int(os.environ.get('CLICKHOUSE_PORT', '8123'))

from config import CANONICAL as DEPLOYMENT_TO_SVC, NEPTUNE_TO_DEPLOYMENT

def _ch_query(sql: str) -> list:
    """执行 ClickHouse 查询，返回行列表"""
    import urllib3
    http = urllib3.PoolManager()
    resp = http.request('POST', f'http://{CH_HOST}:{CH_PORT}',
                        body=sql.encode(), headers={'Content-Type': 'text/plain'})
    if resp.status != 200:
        logger.error(f"ClickHouse error: {resp.data[:200]}")
        return []
    rows = []
    for line in resp.data.decode().strip().split('\n'):
        if line:
            rows.append(line.split('\t'))
    return rows

def step1_deepflow_errors(affected_service: str, window_minutes: int = 30) -> list:
    """
    Step 1: 查 DeepFlow，找故障窗口内出现 5xx 的服务及最早时间
    返回: [{'service': ..., 'first_error': ..., 'error_count': ..., 'error_rate': ...}]
    """
    # 服务名 → request_domain 前缀映射（从 config 派生）
    svc_domain_map = {v: k for k, v in NEPTUNE_TO_DEPLOYMENT.items()}
    
    sql = f"""
SELECT 
    splitByChar('.', request_domain)[1] AS svc,
    MIN(start_time) AS first_error,
    COUNT(*) AS error_cnt,
    countIf(response_code >= 500) * 100.0 / COUNT(*) AS error_rate_pct
FROM flow_log.l7_flow_log
WHERE start_time > now() - INTERVAL {window_minutes} MINUTE
  AND response_code >= 500
  AND request_domain LIKE '%.default.svc.cluster.local'
GROUP BY svc
ORDER BY first_error ASC
FORMAT TSV
"""
    rows = _ch_query(sql)
    result = []
    for row in rows:
        if len(row) >= 4:
            svc_raw = row[0].strip()
            # 反向映射到 Neptune 服务名
            neptune_name = DEPLOYMENT_TO_SVC.get(svc_raw, svc_raw)
            result.append({
                'service': neptune_name,
                'service_raw': svc_raw,
                'first_error': row[1].strip(),
                'error_count': int(row[2]),
                'error_rate_pct': float(row[3]),
            })
    logger.info(f"DeepFlow errors: {result}")
    return result


def step1b_deepflow_l4_errors(window_minutes: int = 10) -> list:
    """
    Step 1b: 查 DeepFlow L4 flow_log，找 TCP 层异常
    弥补 L7 检测盲区：Node 停机/网络断开时不产生 HTTP 5xx，但会产生：
      - close_type IN (3,4): TCP RST（连接重置）
      - close_type = 5: TCP timeout
      - retrans_syn > 0: SYN 重传（Pod 完全不可达的最强信号）

    返回: [{'server_ip': ..., 'tcp_rst': ..., 'tcp_timeout': ..., 'syn_retrans': ..., 'total': ...}]
    """
    sql = f"""
SELECT IPv4NumToString(ip4_1) AS server_ip,
    countIf(close_type IN (3,4)) AS tcp_rst,
    countIf(close_type = 5) AS tcp_timeout,
    sum(retrans_syn) AS syn_retrans,
    count() AS total
FROM flow_log.l4_flow_log
WHERE time > now() - INTERVAL {window_minutes} MINUTE
  AND ip4_1 != 0
  AND (close_type IN (3,4,5) OR retrans_syn > 0)
GROUP BY server_ip
HAVING tcp_rst + tcp_timeout + syn_retrans >= 5
ORDER BY syn_retrans DESC, tcp_rst + tcp_timeout DESC
LIMIT 20
FORMAT TSV
"""
    rows = _ch_query(sql)
    result = []
    for row in rows:
        if len(row) >= 5:
            result.append({
                'server_ip': row[0].strip(),
                'tcp_rst': int(row[1]),
                'tcp_timeout': int(row[2]),
                'syn_retrans': int(row[3]),
                'total': int(row[4]),
            })
    logger.info(f"L4 anomalies: {len(result)} IPs with TCP errors")
    return result

def step2_cloudtrail_changes(window_minutes: int = 30) -> list:
    """
    Step 2: 查 CloudTrail，找故障前的变更事件（Deploy/UpdateFunction/etc）
    返回: [{'time': ..., 'event': ..., 'resource': ..., 'user': ...}]
    """
    try:
        ct = boto3.client('cloudtrail', region_name=REGION)
        start = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        
        # 关注的变更事件（应用层 + 基础设施层）
        change_events = [
            # 应用层
            'UpdateFunctionCode', 'UpdateFunctionConfiguration',
            'CreateDeployment', 'UpdateDeployment',
            # 基础设施 — EC2/EKS
            'StopInstances', 'TerminateInstances', 'RunInstances',
            'DeleteNodegroup', 'UpdateNodegroupConfig',
            # 基础设施 — RDS
            'ModifyDBCluster', 'ModifyDBInstance',
            'FailoverDBCluster', 'RebootDBInstance',
            # Auto Scaling
            'PutScalingPolicy', 'UpdateAutoScalingGroup',
            'SetDesiredCapacity',
        ]
        
        events = []
        paginator = ct.get_paginator('lookup_events')
        for page in paginator.paginate(StartTime=start,
                                       PaginationConfig={'MaxItems': 50}):
            for e in page.get('Events', []):
                event_name = e.get('EventName', '')
                if any(ev in event_name for ev in change_events):
                    resource_names = [r.get('ResourceName', '') for r in e.get('Resources', [])]
                    events.append({
                        'time': e.get('EventTime', datetime.now(timezone.utc)).isoformat(),
                        'event': event_name,
                        'resource': ', '.join(resource_names)[:100],
                        'user': e.get('Username', 'unknown'),
                    })
        
        logger.info(f"CloudTrail changes: {len(events)} events")
        return events
    except Exception as ex:
        logger.warning(f"CloudTrail query failed: {ex}")
        return []

def step3_graph_candidates(affected_service: str, error_services: list) -> list:
    """
    Step 3: 用 Neptune 图谱找根因候选
    两层遍历：
      1) 服务调用链：Calls/DependsOn 方向找链路起点
      2) 基础设施链：Service→Pod→EC2Instance 找故障节点
    """
    from neptune import neptune_client as nc
    from neptune import neptune_queries as nq

    error_svc_names = {s['service'] for s in error_services}
    
    if not error_svc_names:
        error_svc_names = {affected_service}
    
    candidates = []
    for svc in error_svc_names:
        # 找这个服务的上游（调用方）
        cypher = """
        MATCH (upstream)-[:Calls|DependsOn]->(n {name: $svc})
        RETURN upstream.name AS name, upstream.recovery_priority AS priority
        """
        upstreams = nc.results(cypher, {'svc': svc})
        upstream_names = {u.get('name') for u in upstreams if u.get('name')}
        
        # 如果上游没有出现在错误列表里，这个服务更可能是根因
        has_upstream_error = bool(upstream_names & error_svc_names)
        
        candidates.append({
            'service': svc,
            'has_upstream_error': has_upstream_error,
            'upstream_services': list(upstream_names),
        })

    # 基础设施层根因探测（通过图遍历，不枚举 EC2 API）
    try:
        infra_fault = nq.q10_infra_root_cause(affected_service)
        logger.info(f"step3 infra_fault: has_infra={infra_fault.get('has_infra_fault')}, "
                     f"unhealthy_ec2={len(infra_fault.get('unhealthy_ec2', []))}")

        # Fallback：如果图遍历没找到（ETL 滞后或 ASG 已清理），实时查 EC2 API
        if not infra_fault.get('has_infra_fault'):
            try:
                ec2_client = boto3.client('ec2', region_name=REGION)
                # 查最近有状态变化的 EKS 节点（stopped/stopping/terminated）
                resp = ec2_client.describe_instances(Filters=[
                    {'Name': 'tag:eks:cluster-name', 'Values': [os.environ.get('EKS_CLUSTER_NAME', 'PetSite')]},
                    {'Name': 'instance-state-name', 'Values': ['stopped', 'stopping', 'shutting-down', 'terminated']},
                ])
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
                for res in resp.get('Reservations', []):
                    for inst in res.get('Instances', []):
                        # 只看最近 30 分钟内状态变化的
                        state_transition = inst.get('StateTransitionReason', '')
                        launch_time = inst.get('LaunchTime')
                        # terminated 的实例可能很多，用 StateTransitionReason 中的时间过滤
                        # 格式: "User initiated (2026-03-19 15:22:33 GMT)"
                        import re as _re
                        ts_match = _re.search(r'\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', state_transition)
                        if ts_match:
                            try:
                                transition_time = datetime.strptime(ts_match.group(1), '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
                                if transition_time < cutoff:
                                    continue  # 太旧了，跳过
                            except Exception:
                                pass

                        inst_id = inst['InstanceId']
                        state = inst['State']['Name']
                        az = inst.get('Placement', {}).get('AvailabilityZone', '')
                        tags = {t['Key']: t['Value'] for t in inst.get('Tags', [])}
                        name = tags.get('Name', inst_id)
                        logger.info(f"step3 EC2 API fallback: {inst_id} state={state} az={az} reason={state_transition[:80]}")
                        infra_fault['unhealthy_ec2'].append({
                            'ec2_id': inst_id, 'ec2_name': name,
                            'state': state, 'az': az,
                            'affected_pods': [], 'affected_services': [],
                        })
                        infra_fault['has_infra_fault'] = True
            except Exception as e2:
                logger.warning(f"step3 EC2 API fallback failed: {e2}")

        if infra_fault.get('has_infra_fault'):
            for ec2 in infra_fault.get('unhealthy_ec2', []):
                candidates.append({
                    'service': affected_service,
                    'has_upstream_error': False,
                    'upstream_services': [],
                    'infra_fault': True,
                    'ec2_id': ec2.get('ec2_id', ''),
                    'ec2_state': ec2.get('state', ''),
                    'az': ec2.get('az', ''),
                    'affected_pods': ec2.get('affected_pods', []),
                    'affected_services': ec2.get('affected_services', []),
                })
    except Exception as e:
        logger.warning(f"step3 infra root cause failed: {e}")
    
    return candidates


def step3b_temporal_validation(affected_service: str, error_services: list) -> dict:
    """
    Step 3b: 时序验证 — DeepFlow first_error 时间戳 × Neptune 图路径深度
    
    原理：
      - Neptune Q3 返回调用链中每个服务距 affected_service 的图深度
        （深度越大 = 越上游 = 越可能是根因）
      - DeepFlow 记录每个服务第一次出错的时间
      - 如果"图深度大的服务 first_error 更早"→ 时序与图路径一致 → 提高根因置信度
    
    返回: {
        'svc_name': {
            'graph_depth': int,       # 在调用图中距 affected_service 的跳数
            'first_error': str,       # ISO 时间戳
            'temporal_consistent': bool,  # 时序是否和图方向一致
            'causal_score': int,      # 0-10，加权到 confidence
        }
    }
    """
    from neptune import neptune_client as nc
    from datetime import datetime

    result = {}

    # 1. 从 Neptune 获取调用链深度（upstream → affected_service 方向）
    cypher = """
    MATCH path = (upstream:Microservice)-[:Calls*1..5]->(root:Microservice {name: $svc})
    RETURN upstream.name AS name, length(path) AS depth
    ORDER BY depth ASC
    """
    try:
        graph_rows = nc.results(cypher, {'svc': affected_service})
    except Exception as e:
        logger.warning(f"temporal_validation graph query failed: {e}")
        return {}

    # depth_map: {svc_name: min_depth}（同一服务可能有多条路径，取最短）
    depth_map = {}
    for row in graph_rows:
        name = row.get('name')
        depth = row.get('depth', 0)
        if name and (name not in depth_map or depth < depth_map[name]):
            depth_map[name] = depth

    # affected_service 自身深度为 0
    depth_map[affected_service] = 0

    # 2. DeepFlow first_error 时间 → datetime 对象
    def parse_ts(ts_str: str):
        try:
            return datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
        except Exception:
            return None

    error_time_map = {}
    for svc_info in error_services:
        svc = svc_info.get('service')
        ts = parse_ts(svc_info.get('first_error', ''))
        if svc and ts:
            error_time_map[svc] = ts

    if not error_time_map:
        return {}

    # 3. 对齐：图深度越大的服务，first_error 应该越早
    #    若 depth_i > depth_j 且 first_error_i < first_error_j → 时序一致
    consistent_count = 0
    inconsistent_count = 0

    for svc, depth in depth_map.items():
        if svc not in error_time_map:
            continue
        t = error_time_map[svc]
        matches = 0
        mismatches = 0
        for other_svc, other_depth in depth_map.items():
            if other_svc == svc or other_svc not in error_time_map:
                continue
            other_t = error_time_map[other_svc]
            # 如果 depth > other_depth（我更上游），我应该先出错
            if depth > other_depth:
                if t < other_t:    # 我确实先出错 → 一致
                    matches += 1
                else:
                    mismatches += 1
        temporal_consistent = matches >= mismatches
        # causal_score：越上游(depth大)且时序一致 → 加分越高
        causal_score = min(10, depth * 2) if temporal_consistent else 0
        result[svc] = {
            'graph_depth': depth,
            'first_error': error_time_map[svc].isoformat(),
            'temporal_consistent': temporal_consistent,
            'causal_score': causal_score,
            'matches': matches,
            'mismatches': mismatches,
        }

    logger.info(f"temporal_validation result: {result}")
    return result


def step3c_log_sampling(top_candidates: list, window_minutes: int = 5) -> dict:
    """
    Step 3c: CloudWatch Logs 采样
    
    根据 top_candidates 的 log_source（从 Neptune Q8 获取），
    拉取最近 window_minutes 分钟内的 ERROR/FATAL 日志行（最多 20 条）。
    
    返回 {service_name: [log_line, ...]}
    仅查 top 2 候选，避免超时。
    """
    import boto3, re, time
    from neptune.neptune_queries import q8_log_source

    logs_client = boto3.client('logs', region_name=REGION)
    end_time = int(time.time() * 1000)
    start_time = end_time - window_minutes * 60 * 1000
    results = {}

    for candidate in top_candidates[:2]:
        svc = candidate.get('service', '')
        if not svc:
            continue
        try:
            log_source = q8_log_source(svc)
        except Exception as e:
            logger.warning(f"Q8 log_source query failed for {svc}: {e}")
            log_source = ''

        if not log_source or not log_source.startswith('cwlogs:///'):
            continue

        # 解析 log_source: cwlogs:///log_group?filter=xxx
        uri = log_source[len('cwlogs://'):]  # 保留前导 / 确保日志组路径正确
        if '?' in uri:
            log_group, qs = uri.split('?', 1)
            params = dict(p.split('=', 1) for p in qs.split('&') if '=' in p)
            filter_pattern = params.get('filter', '')
            node_filter = params.get('node', '')
            if node_filter:
                filter_pattern = node_filter
        else:
            log_group, filter_pattern = uri, ''

        # CloudWatch Logs Insights 查询 ERROR/FATAL 行
        query = 'fields @timestamp, @message | filter @message like /ERROR|FATAL|Exception|panic/ | sort @timestamp desc | limit 20'
        try:
            resp = logs_client.start_query(
                logGroupName=log_group,
                startTime=start_time // 1000,
                endTime=end_time // 1000,
                queryString=query,
            )
            query_id = resp['queryId']
            # 最多等 8 秒
            for _ in range(8):
                time.sleep(1)
                r = logs_client.get_query_results(queryId=query_id)
                if r['status'] in ('Complete', 'Failed', 'Cancelled'):
                    break
            lines = []
            for row in r.get('results', []):
                fields = {f['field']: f['value'] for f in row}
                msg = fields.get('@message', '').strip()
                if msg and filter_pattern.lower() in msg.lower():
                    lines.append(msg[:200])  # 截断长行
                elif msg and not filter_pattern:
                    lines.append(msg[:200])
            if lines:
                results[svc] = lines[:10]
                logger.info(f"step3c: {svc} got {len(lines)} log lines from {log_group}")
            else:
                logger.info(f"step3c: {svc} no ERROR logs found in {log_group}")
        except Exception as e:
            logger.warning(f"step3c CW Logs query failed for {svc}/{log_group}: {e}")

    return results

def step4_score(error_services: list, cloudtrail_changes: list,
                graph_candidates: list, affected_service: str,
                temporal_info: dict = None,
                l4_anomalies: list = None) -> list:
    """
    Step 4: 置信度评分
    
    评分维度（共100分）：
    - 时间线最早出现异常（L7 或 L4）: +40分
    - 有近期配置变更: +30分  
    - 无上游故障（自身是链路起点）: +20分
    - 历史曾发生同类故障: +10分（Phase 4 实现）
    """
    if l4_anomalies is None:
        l4_anomalies = []

    # 如果 L7 没有数据，检查 L4 是否有 TCP 层异常
    if not error_services and not l4_anomalies:
        return [{'service': affected_service, 'confidence': 0.3,
                 'evidence': ['no DeepFlow L7/L4 error data, using affected_service as candidate']}]
    
    # 如果 L7 无数据但 L4 有异常，用 L4 数据构造候选服务
    if not error_services and l4_anomalies:
        # L4 异常 IP → 构造虚拟 error_services（标记来源为 L4）
        for anomaly in l4_anomalies[:5]:
            error_services.append({
                'service': affected_service,  # 暂用 affected_service，后续可通过 IP→Pod 反查
                'first_error': 'N/A (L4)',
                'error_count': anomaly['tcp_rst'] + anomaly['tcp_timeout'] + anomaly['syn_retrans'],
                'error_rate_pct': 0,
                '_l4_source': True,
                '_l4_detail': anomaly,
            })
        # L4 场景下只保留一条候选（都指向同一个 affected_service）
        error_services = error_services[:1]

    # 最早出现错误的服务得 +40
    earliest = error_services[0]['service'] if error_services else None
    
    results = []
    for svc_info in error_services:
        svc = svc_info['service']
        score = 0
        evidence = []
        
        # 时间线：最早出现错误
        if svc == earliest:
            score += 40
            evidence.append(f"最早出现 5xx 错误（{svc_info['first_error']}）")
        
        # 配置变更
        related_changes = [c for c in cloudtrail_changes
                          if svc.lower() in c.get('resource', '').lower()
                          or svc.lower() in c.get('event', '').lower()]
        if related_changes:
            score += 30
            evidence.append(f"近期有配置变更：{related_changes[0]['event']} ({related_changes[0]['time'][:16]})")
        
        # 图谱：无上游故障
        graph_info = next((c for c in graph_candidates if c['service'] == svc), None)
        if graph_info and not graph_info['has_upstream_error']:
            score += 20
            evidence.append("无上游服务同时故障（链路起点）")

        # 图谱：基础设施层故障（EC2 停止/终止）
        infra_candidates = [c for c in graph_candidates if c.get('infra_fault')]
        if infra_candidates:
            # 从图中直接获取故障 EC2 信息
            ec2_details = []
            total_affected_pods = 0
            affected_azs = set()
            for ic in infra_candidates:
                ec2_id = ic.get('ec2_id', '')
                ec2_state = ic.get('ec2_state', '')
                az = ic.get('az', '')
                pods = ic.get('affected_pods', [])
                total_affected_pods += len(pods)
                if az:
                    affected_azs.add(az)
                ec2_details.append(f"{ec2_id}({ec2_state}, {az})")
            score += 40  # 基础设施故障是强信号
            evidence.append(
                f"⚠️ 图遍历发现基础设施层故障：EC2 节点 {', '.join(ec2_details)} "
                f"状态异常，影响 {total_affected_pods} 个 Pod"
            )
            # 列出受影响的服务
            all_affected_svcs = set()
            for ic in infra_candidates:
                all_affected_svcs.update(s for s in ic.get('affected_services', []) if s)
            if all_affected_svcs:
                evidence.append(f"受影响服务（图反向遍历）: {', '.join(all_affected_svcs)}")
            if len(affected_azs) == 1:
                evidence.append(f"故障集中在单 AZ: {list(affected_azs)[0]}（可能是 AZ 级故障）")
        
        # 历史故障：同一服务曾出现类似故障 +10
        try:
            from neptune import neptune_queries as nq
            history = nq.q5_similar_incidents(svc, limit=3)
            if history:
                score += 10
                evidence.append(f"历史上曾发生 {len(history)} 次类似故障（最近：{history[0].get('root_cause','?')}）")
        except Exception:
            pass

        # 时序验证：DeepFlow first_error × 图路径深度一致性 +0~10
        if temporal_info and svc in temporal_info:
            ti = temporal_info[svc]
            if ti.get('temporal_consistent') and ti.get('causal_score', 0) > 0:
                score += ti['causal_score']
                depth = ti.get('graph_depth', 0)
                evidence.append(
                    f"时序验证一致：图路径深度={depth}，"
                    f"first_error={ti['first_error'][:19]}，"
                    f"传播方向与调用链吻合（+{ti['causal_score']}分）"
                )
            elif not ti.get('temporal_consistent'):
                evidence.append(
                    f"⚠️ 时序验证异常：first_error={ti['first_error'][:19]}，"
                    f"错误出现顺序与调用链方向不符（可能不是根因，或存在循环调用）"
                )
        
        # 错误率高
        err_rate = svc_info.get('error_rate_pct', 0)
        if err_rate > 50:
            evidence.append(f"错误率 {err_rate:.1f}%")

        # L4 异常信号（TCP RST/timeout/SYN 重传）
        if svc_info.get('_l4_source') and svc_info.get('_l4_detail'):
            l4 = svc_info['_l4_detail']
            syn_r = l4.get('syn_retrans', 0)
            rst = l4.get('tcp_rst', 0)
            timeout = l4.get('tcp_timeout', 0)
            if syn_r > 0:
                score += 40  # SYN 重传 = Pod 完全不可达，最强信号
                evidence.append(f"L4 检测：SYN 重传 {syn_r} 次（Pod 不可达）")
            if rst > 10:
                score += 15
                evidence.append(f"L4 检测：TCP RST {rst} 次（连接被拒绝）")
            if timeout > 20:
                score += 10
                evidence.append(f"L4 检测：TCP timeout {timeout} 次")
        elif l4_anomalies:
            # L7 有数据的服务，检查是否也有 L4 异常（交叉验证）
            total_syn = sum(a.get('syn_retrans', 0) for a in l4_anomalies)
            total_rst = sum(a.get('tcp_rst', 0) for a in l4_anomalies)
            if total_syn > 10:
                score += 10
                evidence.append(f"L4 交叉验证：集群有 {total_syn} 次 SYN 重传")
        
        score = min(score, 100)  # 置信度上限 100%
        results.append({
            'service': svc,
            'confidence': round(score / 100, 2),
            'score': score,
            'evidence': evidence,
            'error_count': svc_info['error_count'],
        })
    
    results.sort(key=lambda x: x['score'], reverse=True)
    return results

def analyze(affected_service: str, classification: dict) -> dict:
    """
    主分析入口，执行完整 RCA 流程
    目标：< 3 分钟出结果
    """
    start = time.time()
    logger.info(f"RCA analysis started for: {affected_service}")
    
    try:
        # Step 1: DeepFlow L7（HTTP 5xx）
        error_services = step1_deepflow_errors(affected_service, window_minutes=30)
    except Exception as e:
        logger.error(f"Step1 failed: {e}")
        error_services = []

    # Step 1b: DeepFlow L4（TCP RST/timeout/SYN重传）
    l4_anomalies = []
    try:
        l4_anomalies = step1b_deepflow_l4_errors(window_minutes=10)
    except Exception as e:
        logger.warning(f"Step1b L4 failed (non-fatal): {e}")
    
    try:
        # Step 2: CloudTrail
        changes = step2_cloudtrail_changes(window_minutes=30)
    except Exception as e:
        logger.error(f"Step2 failed: {e}")
        changes = []
    
    try:
        # Step 3: Neptune 图谱
        candidates = step3_graph_candidates(affected_service, error_services)
    except Exception as e:
        logger.error(f"Step3 failed: {e}")
        candidates = []

    try:
        # Step 3b: 时序验证（DeepFlow first_error × 图路径深度）
        temporal_info = step3b_temporal_validation(affected_service, error_services)
    except Exception as e:
        logger.warning(f"Step3b temporal validation failed (non-fatal): {e}")
        temporal_info = {}

    # Step 4: 评分（含时序验证加权 + L4 异常）
    scored = step4_score(error_services, changes, candidates, affected_service,
                         l4_anomalies=l4_anomalies)

    # Step 3c: CW Logs 采样（在 step4 之后，已有 top candidates）
    log_samples = {}
    try:
        log_samples = step3c_log_sampling(scored[:2], window_minutes=5)
    except Exception as e:
        logger.warning(f"Step3c log sampling failed (non-fatal): {e}")

    # Step 3d: Layer 2 — AWS Service Probers（插件化多服务探测）
    aws_probe_results = []
    try:
        from collectors.aws_probers import run_all_probes, total_score_delta
        # 告知 EC2ASGProbe 是否 Neptune 图层已找到基础设施故障（避免重复）
        neptune_found_infra = any(c.get('infra_fault') for c in candidates)
        probe_signal = {**classification.get('signal', {}),
                        'neptune_infra_fault': neptune_found_infra}
        aws_probe_results = run_all_probes(probe_signal, affected_service, timeout_sec=12)
        # 将 probe score 叠加到 top candidate
        probe_score_bonus = total_score_delta(aws_probe_results)
        if probe_score_bonus > 0 and scored:
            scored[0]['score'] = min(scored[0]['score'] + probe_score_bonus, 100)
            scored[0]['confidence'] = round(scored[0]['score'] / 100, 2)
            scored[0]['evidence'].append(
                f"Layer2 AWS probers detected anomalies (+{probe_score_bonus} pts): "
                + "; ".join(r.summary for r in aws_probe_results if not r.healthy)
            )
        logger.info(f"Step3d AWS probers: {len(aws_probe_results)} results, "
                    f"bonus={probe_score_bonus}")
    except Exception as e:
        logger.warning(f"Step3d AWS probers failed (non-fatal): {e}")

    elapsed = round(time.time() - start, 1)

    result = {
        'root_cause_candidates': scored[:3],  # Top 3
        'blast_radius': [c.get('name') for c in classification.get('affected_capabilities', [])],
        'error_services': error_services,
        'l4_anomalies': l4_anomalies,
        'recent_changes': changes[:5],
        'analysis_time_sec': elapsed,
        'log_samples': log_samples,
        'aws_probe_results': [
            {'service': r.service_name, 'healthy': r.healthy,
             'summary': r.summary, 'evidence': r.evidence}
            for r in aws_probe_results
        ],
        'top_candidate': scored[0] if scored else None,
    }
    
    logger.info(f"RCA complete in {elapsed}s: {json.dumps(result, ensure_ascii=False)[:300]}")
    return result


def check_repeat_incidents(service: str, window_days: int = 7, threshold: int = 3) -> dict:
    """
    检查同一服务是否在近期反复故障（Phase 4：知识库监控）
    返回: {'is_repeat': bool, 'count': int, 'incidents': [...]}
    """
    from neptune import neptune_queries as nq
    history = nq.q5_similar_incidents(service, limit=10)
    
    # 过滤最近 window_days 天
    from datetime import datetime, timezone, timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    recent = [h for h in history if h.get('id', '') > f'inc-{cutoff[:10]}']
    
    is_repeat = len(recent) >= threshold
    if is_repeat:
        import logging
        logging.getLogger(__name__).warning(
            f"⚠️ 重复故障警报：{service} 在过去 {window_days} 天内出现 {len(recent)} 次故障，需深度 RCA"
        )
    
    return {
        'is_repeat': is_repeat,
        'count': len(recent),
        'incidents': recent[:5],
        'needs_deep_rca': is_repeat,
    }
