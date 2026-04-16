"""
semi_auto.py - 半自动执行逻辑（Phase 2）
"""
import os, json, logging
import urllib3

logger = logging.getLogger(__name__)

def _exec_db_connection(classification, playbook):
    from actions import action_executor as ae
    svc = classification['affected_service']
    return ae.rollout_restart(svc)

def _exec_single_az(classification, playbook):
    from actions import action_executor as ae
    svc = classification['affected_service']
    current = classification.get('service_info', {}).get('replicas', 2) or 2
    target = max(int(current * 1.5), current + 1)
    return ae.scale_deployment(svc, target)

PLAYBOOK_ACTIONS = {
    'db_connection_exhausted': _exec_db_connection,
    'single_az_down': _exec_single_az,
}

def execute(classification, playbook):
    from actions import slack_notifier
    severity = classification.get('severity', 'P2')
    pb_id = playbook.get('matched_playbook')
    risk = playbook.get('risk', 'UNKNOWN')
    svc = classification['affected_service']

    if severity == 'P0':
        slack_notifier.notify_fault(classification, playbook)
        return {'mode': 'suggest', 'reason': 'P0_no_auto_exec'}

    if risk == 'LOW' and pb_id in PLAYBOOK_ACTIONS:
        logger.info(f"Semi-auto executing: {pb_id} for {svc}")
        exec_result = PLAYBOOK_ACTIONS[pb_id](classification, playbook)
        _notify_execution(classification, playbook, exec_result)
        return {'mode': 'semi-auto', 'executed': pb_id, 'result': exec_result}

    slack_notifier.notify_fault(classification, playbook)
    return {'mode': 'suggest', 'reason': f'risk={risk}'}

def _notify_execution(classification, playbook, exec_result):
    webhook = os.environ.get('SLACK_WEBHOOK_URL', '')
    if not webhook:
        return
    svc = classification['affected_service']
    severity = classification.get('severity', 'P2')
    action = exec_result.get('action', 'unknown')
    success = exec_result.get('success', False)
    icon = '✅' if success else '❌'
    text = f"{icon} *[{severity}] 自动恢复：{svc}*\n操作：{action}\n"
    if success:
        if action == 'rollout_restart':
            text += "Pod 已触发滚动重启，预计 1-2 分钟完成"
        elif action == 'scale':
            text += f"副本数：{exec_result.get('from')} → {exec_result.get('to')}"
    else:
        text += f"失败：{exec_result.get('reason','unknown')}\n_请人工介入_"
    http = urllib3.PoolManager()
    http.request('POST', webhook, body=json.dumps({'text': text}).encode(),
                 headers={'Content-Type': 'application/json'})
