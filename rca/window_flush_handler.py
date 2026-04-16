"""
window_flush_handler.py - 告警窗口批处理 Lambda 入口

由 EventBridge Scheduler 触发（2 分钟窗口到期时），执行：
  1. AlertBuffer.flush_window()  — 取出窗口内所有缓冲告警并清空
  2. TopologyCorrelator.correlate() — 拓扑关联，分成 EventGroup
  3. 对每个 EventGroup:
     a. fault_classifier.classify_group()
     b. rca_engine.analyze_group()
     c. graph_rag_reporter.generate_group_report()
     d. 写 Incident 节点
     e. DecisionEngine.evaluate() — 自动化决策

event 格式（来自 EventBridge Scheduler / 手动测试）：
  {"window_id": "2026-04-05T10:04"}  — 指定窗口
  {}                                  — 使用上一个已结束窗口
"""
import json
import logging
import os

logger = logging.getLogger()
logger.setLevel(logging.INFO)

from shared import get_region
REGION = get_region()


def window_flush_handler(event: dict, context) -> dict:
    """窗口批处理主入口。

    Args:
        event: EventBridge Scheduler 事件，可含 window_id 字段
        context: Lambda context

    Returns:
        处理结果摘要字典
    """
    window_id = event.get('window_id')  # None 时 flush_window 自动取上一窗口
    logger.info(f"WindowFlush triggered: window_id={window_id}")

    # Step 1: 取出缓冲告警
    from core.alert_buffer import AlertBuffer
    buf = AlertBuffer()
    alerts = buf.flush_window(window_id=window_id)

    if not alerts:
        logger.info("WindowFlush: no alerts in window, nothing to do")
        return {'statusCode': 200, 'body': json.dumps({'groups_processed': 0})}

    logger.info(f"WindowFlush: {len(alerts)} alerts flushed from window={window_id}")

    # Step 2: 拓扑关联
    from core.topology_correlator import TopologyCorrelator
    correlator = TopologyCorrelator()
    groups = correlator.correlate(alerts)
    logger.info(f"WindowFlush: {len(alerts)} alerts → {len(groups)} EventGroups")

    # Step 3: 对每组执行 RCA 流程
    results = []
    for group in groups:
        result = _process_group(group)
        results.append(result)

    processed = sum(1 for r in results if not r.get('error'))
    failed = len(results) - processed
    logger.info(f"WindowFlush complete: processed={processed} failed={failed}")

    return {
        'statusCode': 200,
        'body': json.dumps({
            'window_id': window_id,
            'alerts_flushed': len(alerts),
            'groups_processed': processed,
            'groups_failed': failed,
            'results': results,
        }, ensure_ascii=False, default=str),
    }


def _process_group(group) -> dict:
    """对单个 EventGroup 执行完整 RCA + 决策流程。

    Args:
        group: EventGroup 对象

    Returns:
        处理结果字典（含 group_id, incident_id, severity, error 等字段）
    """
    from core import fault_classifier, rca_engine, graph_rag_reporter
    from core.decision_engine import DecisionEngine

    group_id = group.group_id
    svc = group.root_candidate_service
    severity = group.severity

    logger.info(
        f"Processing group={group_id} svc={svc} severity={severity} "
        f"type={group.correlation_type} alerts={len(group.all_alerts)}"
    )

    result: dict = {
        'group_id': group_id,
        'service': svc,
        'severity': severity,
        'correlation_type': group.correlation_type,
        'alert_count': len(group.all_alerts),
    }

    # Step 3a: 故障分类（group 专用方法）
    try:
        # 从根因告警中提取 signal
        root_alert = group.root_candidate_alert
        signal = getattr(root_alert, 'raw', {}) if root_alert else {}
        classification = fault_classifier.classify_group(group)
    except Exception as e:
        logger.error(f"classify_group failed for {group_id}: {e}", exc_info=True)
        # 降级：用根因告警直接分类
        try:
            root_alert = group.root_candidate_alert
            signal = getattr(root_alert, 'raw', {}) if root_alert else {}
            classification = fault_classifier.classify(svc, signal)
        except Exception as e2:
            classification = {
                'severity': severity, 'strategy': 'Parallel',
                'affected_service': svc,
                'service_info': {}, 'affected_capabilities': [],
                'affected_services': [], 'tier0_impact_count': 0,
                'signal': signal,
            }
        logger.warning(f"classify_group degraded to classify() for {group_id}")

    result['classification_severity'] = classification.get('severity', severity)

    # Step 3b: RCA 分析（group 专用方法）
    rca_result = None
    try:
        rca_result = rca_engine.analyze_group(group, classification)
    except Exception as e:
        logger.error(f"analyze_group failed for {group_id}: {e}", exc_info=True)
        # 降级：用单告警分析
        try:
            rca_result = rca_engine.analyze(svc, classification)
            logger.warning(f"analyze_group degraded to analyze() for {group_id}")
        except Exception as e2:
            logger.error(f"analyze() also failed for {group_id}: {e2}")
            result['error'] = f"rca_engine failed: {e}"
            return result

    # Step 3c: Graph RAG 报告（group 专用方法）
    rag_report = None
    try:
        rag_report = graph_rag_reporter.generate_group_report(group, classification, rca_result)
        if rca_result:
            rca_result['rag_report'] = rag_report
    except Exception as e:
        logger.error(f"generate_group_report failed for {group_id}: {e}", exc_info=True)
        # 降级：用标准报告
        try:
            log_samples = rca_result.get('log_samples', {}) if rca_result else {}
            rag_report = graph_rag_reporter.generate_rca_report(
                svc, classification, rca_result or {}, log_samples=log_samples,
            )
            logger.warning(f"generate_group_report degraded to generate_rca_report() for {group_id}")
        except Exception as e2:
            logger.warning(f"generate_rca_report also failed for {group_id}: {e2}")

    # Step 3d: 写 Incident 节点
    incident_id = None
    if rca_result:
        try:
            from actions import incident_writer
            report_text = (rag_report or {}).get('reasoning', '')
            incident_id = incident_writer.write_incident(
                classification, rca_result,
                report_text=report_text,
            )
            logger.info(f"Incident created for group={group_id}: {incident_id}")
        except Exception as e:
            logger.error(f"write_incident failed for {group_id}: {e}")

    result['incident_id'] = incident_id

    # Step 3e: 自动化决策
    try:
        engine = DecisionEngine()
        decision = engine.evaluate(
            severity=classification.get('severity', severity),
            rca_result=rca_result or {},
        )
        result['decision'] = decision
        logger.info(f"Decision for group={group_id}: {decision.get('action')}")
    except Exception as e:
        logger.warning(f"DecisionEngine failed for {group_id}: {e}")

    # 发送 Slack 通知
    if rag_report:
        try:
            _send_group_report(rag_report, classification, group)
        except Exception as e:
            logger.warning(f"Slack notify failed for {group_id}: {e}")

    return result


def _send_group_report(rag: dict, classification: dict, group) -> None:
    """发送聚合告警 RCA 报告到 Slack。

    Args:
        rag: generate_group_report / generate_rca_report 的输出
        classification: 故障分类结果
        group: EventGroup 对象
    """
    import urllib3
    webhook = os.environ.get('SLACK_WEBHOOK_URL', '')
    if not webhook:
        return

    svc = classification.get('affected_service', group.root_candidate_service)
    severity = classification.get('severity', group.severity)
    alert_count = len(group.all_alerts)
    correlation_type = group.correlation_type

    def _s(val, default='未知'):
        if val is None:
            return default
        if isinstance(val, dict):
            return val.get('root_cause', val.get('description', str(val)[:200]))
        return str(val)

    root_cause = _s(rag.get('root_cause', '未知'))
    confidence = rag.get('confidence', 0)
    try:
        confidence = float(confidence)
    except (TypeError, ValueError):
        confidence = 0.0
    action = _s(rag.get('recommended_action', '人工处理'))
    reasoning = _s(rag.get('reasoning', ''))

    evidence = rag.get('evidence', [])
    if isinstance(evidence, list):
        evidence = [_s(e, '') for e in evidence if e]
    else:
        evidence = [_s(evidence)]
    ev_text = '\n'.join(f'• {e}' for e in evidence[:3]) if evidence else '• 证据不足'

    # 聚合标识
    group_badge = f'[聚合 {alert_count} 条告警 | {correlation_type}]' if alert_count > 1 else ''
    blast = _s(rag.get('blast_radius', ''))
    blast_line = f'\n*影响范围：* {blast}' if blast and blast != '未知' else ''

    text = (
        f"🔍 *[{severity}] RCA 报告：{svc}* {group_badge}\n\n"
        f"*根因：* {root_cause}\n"
        f"*置信度：* {confidence:.0f}%\n"
        f"*建议操作：* {action}"
        f"{blast_line}\n\n"
        f"*证据：*\n{ev_text}\n\n"
        f"*推理：* {reasoning[:300]}"
    )

    http = urllib3.PoolManager()
    http.request(
        'POST', webhook,
        body=json.dumps({'text': text}).encode(),
        headers={'Content-Type': 'application/json'},
    )
