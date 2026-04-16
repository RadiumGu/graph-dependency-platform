"""
handler.py - RCA Lambda 入口（Phase 3 + Alert Aggregation）

Note on import structure
------------------------
All heavy module imports (rca_engine, fault_classifier, graph_rag_reporter,
incident_writer, etc.) are deferred to inside lambda_handler() rather than
placed at the module top level. This is a deliberate Lambda cold-start
optimisation: the Python interpreter still has to load these modules on the
first invocation, but subsequent invocations in the same container reuse the
already-loaded modules via the module cache. Keeping top-level imports minimal
also makes unit-testing the handler easier because heavyweight AWS dependencies
are not imported until actually needed.

Alert Aggregation Flow (Phase 4 新增)
--------------------------------------
P0 告警: bypass 缓冲 → 立即执行完整 RCA（_fast_track_rca）
P1/P2 告警: 标准化(EventNormalizer) → 写缓冲(AlertBuffer) → 返回 202
到期处理: EventBridge Scheduler 触发 window_flush_handler.py
"""
import os, json, logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)

CW_METRIC_MAP = {
    'HTTPCode_Target_5XX_Count': 'error_rate',
    'HTTPCode_Target_4XX_Count': 'error_rate',
    'TargetResponseTime': 'latency_p99',
}
DEFAULT_AFFECTED_SERVICE = 'petsite'

def _parse_cw_alarm(alarm):
    if alarm.get('NewStateValue') != 'ALARM':
        return None
    trigger = alarm.get('Trigger', {})
    description = alarm.get('AlarmDescription', '')
    affected = DEFAULT_AFFECTED_SERVICE
    for p in description.split():
        if p.startswith('service:'):
            affected = p.split(':', 1)[1]
    return {
        'source': 'cloudwatch_alarm',
        'affected_resource': affected,
        'metric': CW_METRIC_MAP.get(trigger.get('MetricName', ''), trigger.get('MetricName', '')),
        'value': 0.5, 'threshold': 0.05,
        'alarm_name': alarm.get('AlarmName', ''),
    }

def lambda_handler(event, context):
    from core import fault_classifier, rca_engine
    from actions import playbook_engine, semi_auto, slack_notifier

    logger.info(f"RCA triggered: {json.dumps(event)[:300]}")

    if 'Records' in event:
        msg_str = event['Records'][0]['Sns']['Message']
        try:
            msg = json.loads(msg_str)
        except Exception:
            return {'statusCode': 400, 'body': 'invalid SNS message'}
        signal = _parse_cw_alarm(msg) if 'AlarmName' in msg else msg
        if signal is None:
            return {'statusCode': 200, 'body': 'skipped'}
    else:
        signal = event

    # 支持 resolve 操作（由 Slack interaction 或手动调用）— 保持不变
    if signal.get('action') == 'resolve' and signal.get('incident_id'):
        try:
            from actions import incident_writer
            incident_writer.resolve_incident(
                signal['incident_id'],
                signal.get('resolution', 'manual resolve'),
                signal.get('mttr_seconds', 0)
            )
            return {'statusCode': 200, 'body': json.dumps({'resolved': signal['incident_id']})}
        except Exception as e:
            logger.error(f"Resolve failed: {e}")
            return {'statusCode': 500, 'body': str(e)}

    # ── Alert Aggregation Flow ────────────────────────────────────────────────
    # 检查 FEATURE_FLAGS：alert_buffer_enabled
    try:
        from config import FEATURE_FLAGS
        buffer_enabled = FEATURE_FLAGS.get('alert_buffer_enabled', False)
    except Exception:
        buffer_enabled = False

    if buffer_enabled:
        return _aggregation_flow(signal, fault_classifier, rca_engine,
                                 playbook_engine, semi_auto)

    # ── 原有直通流程（feature flag 关闭时） ───────────────────────────────────
    affected_service = signal.get('affected_resource', '')
    if not affected_service:
        return {'statusCode': 400, 'body': 'missing affected_resource'}

    return _direct_rca_flow(affected_service, signal,
                            fault_classifier, rca_engine,
                            playbook_engine, semi_auto)


def _aggregation_flow(signal: dict,
                      fault_classifier, rca_engine,
                      playbook_engine, semi_auto) -> dict:
    """告警聚合分流入口。

    P0 → bypass 缓冲，直接走 _fast_track_rca
    P1/P2 → 标准化 + 写缓冲，返回 202

    Args:
        signal: 解析后的原始信号字典
        fault_classifier/rca_engine/playbook_engine/semi_auto: lazy-imported 模块

    Returns:
        Lambda 响应字典
    """
    from core.event_normalizer import EventNormalizer
    from core.alert_buffer import AlertBuffer, P0_BYPASS_BUFFER

    normalizer = EventNormalizer()
    alert_event = normalizer.normalize(signal)

    if alert_event is None:
        logger.info("Aggregation flow: signal normalized to None, skipping")
        return {'statusCode': 200, 'body': 'skipped'}

    # 快速分类以确定是否 P0
    try:
        pre_class = fault_classifier.classify(alert_event.service_name, signal)
        severity = pre_class.get('severity', 'P1')
    except Exception as e:
        logger.warning(f"Pre-classify failed: {e}, defaulting to P1")
        severity = 'P1'
        pre_class = {
            'severity': 'P1', 'strategy': 'Parallel',
            'affected_service': alert_event.service_name,
            'service_info': {}, 'affected_capabilities': [],
            'affected_services': [], 'tier0_impact_count': 0,
            'signal': signal,
        }

    # P0 bypass
    if severity == 'P0' and P0_BYPASS_BUFFER:
        logger.info(f"P0 alert bypass buffer: svc={alert_event.service_name}")
        return _fast_track_rca(
            alert_event.service_name, signal, pre_class,
            rca_engine, playbook_engine, semi_auto,
        )

    # 非 P0：写入缓冲，返回 202
    try:
        buf = AlertBuffer()
        is_new = buf.put_alert(alert_event)
        logger.info(
            f"Alert buffered: svc={alert_event.service_name} "
            f"fp={alert_event.fingerprint[:8]} new={is_new}"
        )
    except Exception as e:
        logger.error(f"AlertBuffer put failed: {e}. Falling back to direct RCA.")
        # 缓冲失败时降级到直通流程
        return _direct_rca_flow(
            alert_event.service_name, signal,
            fault_classifier, rca_engine, playbook_engine, semi_auto,
        )

    return {
        'statusCode': 202,
        'body': json.dumps({
            'status': 'buffered',
            'fingerprint': alert_event.fingerprint,
            'service': alert_event.service_name,
            'window_id': alert_event.start_time[:16],
        }, ensure_ascii=False),
    }


def _fast_track_rca(affected_service: str, signal: dict,
                    classification: dict,
                    rca_engine, playbook_engine, semi_auto) -> dict:
    """P0 bypass 快速通道：保持与原有完整 RCA 流程完全一致。

    Args:
        affected_service: Neptune 规范服务名
        signal: 原始信号字典
        classification: 故障分类结果
        rca_engine/playbook_engine/semi_auto: lazy-imported 模块

    Returns:
        Lambda 响应字典
    """
    severity = classification['severity']

    playbook = playbook_engine.match(classification)
    exec_result = semi_auto.execute(classification, playbook)

    rca_result = None
    try:
        rca_result = rca_engine.analyze(affected_service, classification)
        try:
            from core import graph_rag_reporter
            _log_samples = rca_result.get('log_samples', {})
            rag_report = graph_rag_reporter.generate_rca_report(
                affected_service, classification, rca_result,
                log_samples=_log_samples,
            )
            rca_result['rag_report'] = rag_report
            _send_rag_report(rag_report, classification)
            logger.info(f"Graph RAG report sent (fast-track): confidence={rag_report.get('confidence')}")
        except Exception as rag_err:
            logger.error(f"Graph RAG failed (fast-track): {rag_err}", exc_info=True)
            _send_rca_report(rca_result, classification, playbook)
    except Exception as e:
        logger.error(f"RCA analysis failed (fast-track): {e}", exc_info=True)

    result = {
        'severity': severity,
        'strategy': classification['strategy'],
        'playbook': playbook.get('matched_playbook'),
        'execution': exec_result,
        'rca': rca_result,
        'affected_capabilities': [c.get('name') for c in classification.get('affected_capabilities', [])],
        'fast_track': True,
    }

    incident_id = None
    if rca_result:
        try:
            from actions import incident_writer
            rag = rca_result.get('rag_report', {})
            report_text = rag.get('reasoning', '') if rag else ''
            incident_id = incident_writer.write_incident(
                classification, rca_result, report_text=report_text,
            )
            logger.info(f"Incident created (fast-track): {incident_id}")
        except Exception as e:
            logger.error(f"Incident write failed (fast-track): {e}")

    repeat_info = None
    try:
        repeat_info = rca_engine.check_repeat_incidents(affected_service)
        if repeat_info.get('needs_deep_rca'):
            _send_repeat_alert(affected_service, repeat_info, classification)
    except Exception as e:
        logger.warning(f"Repeat check failed (fast-track): {e}")

    result['incident_id'] = incident_id
    result['repeat_info'] = repeat_info
    return {'statusCode': 200, 'body': json.dumps(result, ensure_ascii=False)}


def _direct_rca_flow(affected_service: str, signal: dict,
                     fault_classifier, rca_engine,
                     playbook_engine, semi_auto) -> dict:
    """原有直通 RCA 流程（feature flag 关闭时或缓冲失败降级时使用）。

    保持与改造前 lambda_handler 完全相同的处理逻辑。

    Args:
        affected_service: 受影响服务名
        signal: 原始信号字典
        fault_classifier/rca_engine/playbook_engine/semi_auto: lazy-imported 模块

    Returns:
        Lambda 响应字典
    """
    if not affected_service:
        return {'statusCode': 400, 'body': 'missing affected_resource'}

    # 故障分类
    try:
        classification = fault_classifier.classify(affected_service, signal)
    except Exception as e:
        logger.error(f"Classification failed: {e}", exc_info=True)
        classification = {
            'severity': 'P1', 'strategy': 'Parallel',
            'affected_service': affected_service,
            'service_info': {}, 'affected_capabilities': [],
            'affected_services': [], 'tier0_impact_count': 0,
            'signal': signal,
        }

    severity = classification['severity']

    # Playbook 匹配 + 执行
    playbook = playbook_engine.match(classification)
    exec_result = semi_auto.execute(classification, playbook)

    # Phase 3: P0/P1 场景同步执行 RCA 分析
    rca_result = None
    if severity in ('P0', 'P1'):
        try:
            rca_result = rca_engine.analyze(affected_service, classification)
            # Graph RAG：用 Bedrock + Neptune + CloudWatch 生成报告
            try:
                from core import graph_rag_reporter
                _log_samples = rca_result.get('log_samples', {})
                rag_report = graph_rag_reporter.generate_rca_report(
                    affected_service, classification, rca_result,
                    log_samples=_log_samples,
                )
                rca_result['rag_report'] = rag_report
                _send_rag_report(rag_report, classification)
                logger.info(f"Graph RAG report sent: confidence={rag_report.get('confidence')}")
            except Exception as rag_err:
                logger.error(f"Graph RAG failed: {rag_err}", exc_info=True)
                _send_rca_report(rca_result, classification, playbook)
        except Exception as e:
            logger.error(f"RCA analysis failed: {e}", exc_info=True)

    result = {
        'severity': severity,
        'strategy': classification['strategy'],
        'playbook': playbook.get('matched_playbook'),
        'execution': exec_result,
        'rca': rca_result,
        'affected_capabilities': [c.get('name') for c in classification.get('affected_capabilities', [])],
    }
    # Phase 3: 写 Incident 节点
    incident_id = None
    if severity in ('P0', 'P1') and rca_result:
        try:
            from actions import incident_writer
            incident_id = incident_writer.write_incident(classification, rca_result)
            logger.info(f"Incident created: {incident_id}")
        except Exception as e:
            logger.error(f"Incident write failed: {e}")

    # Phase 4: 重复故障检测
    repeat_info = None
    if severity in ('P0', 'P1'):
        try:
            from core import rca_engine as rca_mod
            repeat_info = rca_mod.check_repeat_incidents(affected_service)
            if repeat_info.get('needs_deep_rca'):
                _send_repeat_alert(affected_service, repeat_info, classification)
        except Exception as e:
            logger.warning(f"Repeat check failed: {e}")

    result['incident_id'] = incident_id
    result['repeat_info'] = repeat_info
    return {'statusCode': 200, 'body': json.dumps(result, ensure_ascii=False)}

def _send_rag_report(rag: dict, classification: dict):
    """发送 Graph RAG 生成的 RCA 报告到 Slack"""
    import urllib3
    webhook = os.environ.get('SLACK_WEBHOOK_URL', '')
    if not webhook:
        return
    svc = classification['affected_service']
    severity = classification['severity']

    # 防御：如果 root_cause 是嵌套 JSON 字符串，二次解析提取
    def _unwrap_json_field(val, field_name):
        """如果 val 是 JSON 字符串（LLM 返回了整个 JSON 而非字段值），提取指定字段"""
        if isinstance(val, str) and val.strip().startswith('{'):
            try:
                parsed = json.loads(val)
                if isinstance(parsed, dict):
                    return parsed
            except (json.JSONDecodeError, ValueError):
                # 可能是截断的 JSON，尝试提取引号内的值
                import re
                m = re.search(rf'"{field_name}"\s*:\s*"([^"]+)"', val)
                if m:
                    return {field_name: m.group(1)}
        return None

    # 检查 rag 是否实际上是未解析的 JSON 字符串
    root_cause_raw = rag.get('root_cause', '未知')
    unwrapped = _unwrap_json_field(root_cause_raw, 'root_cause')
    if unwrapped:
        # root_cause 存的是整个 LLM JSON 输出，用解析后的值替换
        rag = {**rag, **unwrapped}
        root_cause_raw = rag.get('root_cause', '未知')

    def _str(val, default='未知'):
        if val is None:
            return default
        if isinstance(val, dict):
            return val.get('root_cause', val.get('description', str(val)[:200]))
        return str(val)

    def _num(val, default=0):
        if isinstance(val, (int, float)):
            return val
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    conf = _num(rag.get('confidence', 0))
    root_cause = _str(root_cause_raw)
    action = _str(rag.get('recommended_action', '人工处理'))
    reasoning = _str(rag.get('reasoning', ''))
    evidence = rag.get('evidence', [])
    source = rag.get('source', '')
    source_tag = '🤖 AI分析' if 'bedrock' in source else '📐 规则引擎'

    # 确保 evidence 是 list[str]
    if isinstance(evidence, list):
        evidence = [_str(e, '') for e in evidence if e]
    else:
        evidence = [_str(evidence)]

    ev_text = '\n'.join(f'• {e}' for e in evidence[:3]) if evidence else '• 证据不足'
    text = (
        f"🔍 *[{severity}] RCA 报告：{svc}* {source_tag}\n\n"
        f"*根因：* {root_cause}\n"
        f"*置信度：* {conf:.0f}%\n"
        f"*建议操作：* {action}\n\n"
        f"*证据：*\n{ev_text}\n\n"
        f"*推理：* {reasoning[:300]}"
    )
    http = urllib3.PoolManager()
    http.request('POST', webhook, body=json.dumps({'text': text}).encode(),
                 headers={'Content-Type': 'application/json'})

def _send_repeat_alert(svc: str, repeat_info: dict, classification: dict):
    """发送重复故障深度 RCA 告警"""
    import urllib3
    webhook = os.environ.get('SLACK_WEBHOOK_URL', '')
    if not webhook:
        return
    count = repeat_info.get('count', 0)
    text = (
        f"🔁 *[重复故障警报] {svc}*\n"
        f"过去 7 天内已发生 *{count}* 次故障，建议启动深度 RCA\n"
        f"_请检查是否有未解决的根本原因（内存泄漏、配置缺陷等）_"
    )
    http = urllib3.PoolManager()
    http.request('POST', webhook, body=json.dumps({'text': text}).encode(),
                 headers={'Content-Type': 'application/json'})

def _send_rca_report(rca_result: dict, classification: dict, playbook: dict):
    """发送 RCA 分析报告到 Slack"""
    import urllib3
    webhook = os.environ.get('SLACK_WEBHOOK_URL', '')
    if not webhook:
        return
    
    svc = classification['affected_service']
    severity = classification['severity']
    elapsed = rca_result.get('analysis_time_sec', 0)
    top = rca_result.get('top_candidate', {})
    candidates = rca_result.get('root_cause_candidates', [])
    changes = rca_result.get('recent_changes', [])
    
    # 根因候选列表
    candidates_text = ''
    for i, c in enumerate(candidates[:3]):
        conf = int(c.get('confidence', 0) * 100)
        evs = ' | '.join(c.get('evidence', []))[:80]
        candidates_text += f"{i+1}. *{c['service']}* 置信度 {conf}% — {evs}\n"
    
    # 近期变更
    changes_text = ''
    for ch in changes[:3]:
        changes_text += f"• `{ch['event']}` {ch['resource'][:30]} ({ch['time'][:16]})\n"
    changes_text = changes_text or '无近期变更'
    
    text = (
        f"🔍 *[{severity}] RCA 分析完成：{svc}* （{elapsed}s）\n\n"
        f"*根因候选：*\n{candidates_text or '数据不足，无法确定根因'}\n"
        f"*近期变更：*\n{changes_text}"
    )
    
    http = urllib3.PoolManager()
    http.request('POST', webhook, body=json.dumps({'text': text}).encode(),
                 headers={'Content-Type': 'application/json'})
