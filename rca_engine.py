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
CH_HOST = os.environ.get('CLICKHOUSE_HOST', '11.0.2.30')
CH_PORT = int(os.environ.get('CLICKHOUSE_PORT', '8123'))

# K8s deployment 名 → Neptune 服务名
DEPLOYMENT_TO_SVC = {
    'petsite-deployment': 'petsite',
    'search-service':     'petsearch',
    'service-petsite':    'petsite',
    'pay-for-adoption':   'payforadoption',
    'list-adoptions':     'petlistadoptions',
    'pethistory-deployment': 'petadoptionshistory',
    'pethistory-service': 'petadoptionshistory',
}

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
    # 服务名 → request_domain 前缀映射
    svc_domain_map = {
        'petsite':            'service-petsite',
        'petsearch':          'search-service',
        'payforadoption':     'pay-for-adoption',
        'petlistadoptions':   'list-adoptions',
        'petadoptionshistory': 'pethistory-service',
    }
    
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

def step2_cloudtrail_changes(window_minutes: int = 30) -> list:
    """
    Step 2: 查 CloudTrail，找故障前的变更事件（Deploy/UpdateFunction/etc）
    返回: [{'time': ..., 'event': ..., 'resource': ..., 'user': ...}]
    """
    try:
        ct = boto3.client('cloudtrail', region_name=REGION)
        start = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        
        # 关注的变更事件
        change_events = [
            'UpdateFunctionCode', 'UpdateFunctionConfiguration',
            'CreateDeployment', 'UpdateDeployment',
            'ModifyDBCluster', 'ModifyDBInstance',
            'PutScalingPolicy', 'UpdateAutoScalingGroup',
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
    从有错误的服务出发，找没有上游错误但自身有错误的节点（根因）
    """
    from . import neptune_client as nc
    
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
    from . import neptune_client as nc
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
    from .neptune_queries import q8_log_source

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
                temporal_info: dict = None) -> list:
    """
    Step 4: 置信度评分
    
    评分维度（共100分）：
    - 时间线最早出现异常: +40分
    - 有近期配置变更: +30分  
    - 无上游故障（自身是链路起点）: +20分
    - 历史曾发生同类故障: +10分（Phase 4 实现）
    """
    if not error_services:
        return [{'service': affected_service, 'confidence': 0.3,
                 'evidence': ['no DeepFlow error data, using affected_service as candidate']}]
    
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
        
        # 历史故障：同一服务曾出现类似故障 +10
        try:
            from . import neptune_queries as nq
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
        # Step 1: DeepFlow
        error_services = step1_deepflow_errors(affected_service, window_minutes=30)
    except Exception as e:
        logger.error(f"Step1 failed: {e}")
        error_services = []
    
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

    # Step 4: 评分（含时序验证加权）
    scored = step4_score(error_services, changes, candidates, affected_service)

    # Step 3c: CW Logs 采样（在 step4 之后，已有 top candidates）
    log_samples = {}
    try:
        log_samples = step3c_log_sampling(scored[:2], window_minutes=5)
    except Exception as e:
        logger.warning(f"Step3c log sampling failed (non-fatal): {e}")
    
    elapsed = round(time.time() - start, 1)
    
    result = {
        'root_cause_candidates': scored[:3],  # Top 3
        'blast_radius': [c.get('name') for c in classification.get('affected_capabilities', [])],
        'error_services': error_services,
        'recent_changes': changes[:5],
        'analysis_time_sec': elapsed,
        'log_samples': log_samples,
        'top_candidate': scored[0] if scored else None,
    }
    
    logger.info(f"RCA complete in {elapsed}s: {json.dumps(result, ensure_ascii=False)[:300]}")
    return result


def check_repeat_incidents(service: str, window_days: int = 7, threshold: int = 3) -> dict:
    """
    检查同一服务是否在近期反复故障（Phase 4：知识库监控）
    返回: {'is_repeat': bool, 'count': int, 'incidents': [...]}
    """
    from . import neptune_queries as nq
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
