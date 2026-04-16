"""
slack_notifier.py - Slack 通知（Phase 2）
- LOW 风险自动执行后：发执行结果
- MEDIUM/DEPENDS 风险：发带 [确认执行] [跳过] 按钮的消息
- P0：发纯文字告警 + 详细诊断建议
"""
import os, json, logging
import urllib3

from config import profile as _profile

logger = logging.getLogger(__name__)
http = urllib3.PoolManager()

WEBHOOK_URL = lambda: os.environ.get('SLACK_WEBHOOK_URL', '')
SEVERITY_EMOJI = {'P0': '🔴', 'P1': '🟠', 'P2': '🟡'}
STRATEGY_DESC = {
    'Diagnose-First': '先诊断，查明根因后再恢复',
    'Parallel':       '并行：恢复 + 诊断同步进行',
    'Restore-First':  '先恢复，后诊断'
}

def notify_fault(classification: dict, playbook: dict) -> bool:
    """
    发送故障告警。
    - MEDIUM/DEPENDS 风险：带确认按钮
    - 其他：纯文字
    """
    severity = classification.get('severity', 'P2')
    risk = playbook.get('risk', 'UNKNOWN')
    svc = classification.get('affected_service', 'unknown')
    signal = classification.get('signal', {})
    caps = classification.get('affected_capabilities', [])
    bc_text = '、'.join(c.get('name','') for c in caps if c.get('name')) or '未知'
    
    metric = signal.get('metric', '')
    value = signal.get('value', 0)
    metric_text = f"{metric}: {value:.0%}" if isinstance(value, float) else f"{metric}: {value}"
    
    emoji = SEVERITY_EMOJI.get(severity, '⚠️')
    strategy = classification.get('strategy', '')
    pb_name = playbook.get('playbook_name', '动态生成')
    steps = playbook.get('steps', [])
    steps_text = '\n'.join(f"{i+1}. {s}" for i, s in enumerate(steps))
    
    # MEDIUM/DEPENDS 风险且非 P0 → 带确认按钮
    if risk in ('MEDIUM', 'DEPENDS') and severity != 'P0':
        return _send_with_buttons(
            emoji, severity, svc, metric_text, strategy, bc_text, pb_name, steps_text,
            classification, playbook
        )
    
    # 其他：纯文字告警
    text = (
        f"{emoji} *[{severity}] 故障告警：{svc}*\n"
        f"*信号：* {metric_text}\n"
        f"*策略：* {strategy} — {STRATEGY_DESC.get(strategy,'')}\n"
        f"*受影响业务：* {bc_text}\n"
        f"*Playbook：* {pb_name}\n\n"
        f"*恢复步骤：*\n```{steps_text}```\n"
        f"_建议模式：以上步骤需人工执行_"
    )
    return _post(text)

def _send_with_buttons(emoji, severity, svc, metric_text, strategy, bc_text, pb_name, steps_text,
                       classification, playbook):
    """发送带 [确认执行] [跳过] 按钮的 Block Kit 消息"""
    interact_url = _get_interact_url()
    
    # 按钮 value（传递给 interaction Lambda）
    btn_value = json.dumps({
        'service': svc,
        'action': 'rollout_restart',
        'severity': severity,
        'metric': classification.get('signal', {}).get('metric', ''),
        'playbook': playbook.get('matched_playbook', ''),
    })
    
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": (
                f"{emoji} *[{severity}] 故障告警：{svc}*\n"
                f"*信号：* {metric_text}  *业务：* {bc_text}\n"
                f"*策略：* {strategy}  *Playbook：* {pb_name}"
            )}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*建议操作：*\n```{steps_text}```"}
        },
    ]
    
    if interact_url:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ 确认执行"},
                    "style": "primary",
                    "action_id": "rca_confirm",
                    "value": btn_value
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⏭️ 跳过"},
                    "action_id": "rca_skip",
                    "value": btn_value
                }
            ]
        })
    else:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "_⚠️ 交互端点未配置，请人工执行_"}
        })
    
    return _post_blocks(blocks)

def _get_interact_url():
    """从 SSM 获取 interaction endpoint URL"""
    try:
        import boto3
        ssm = boto3.client('ssm', region_name=os.environ.get('REGION','ap-northeast-1'))
        resp = ssm.get_parameter(Name=_profile.get('parameter_store.keys.slack_interact_url', '/petsite/slack/interact-url'))
        return resp['Parameter']['Value']
    except Exception:
        return ''

def _post(text: str) -> bool:
    w = WEBHOOK_URL()
    if not w:
        logger.info(f"[DRY-RUN] {text[:100]}")
        return False
    resp = http.request('POST', w, body=json.dumps({'text': text}).encode(),
                        headers={'Content-Type': 'application/json'})
    return resp.status == 200

def _post_blocks(blocks: list) -> bool:
    w = WEBHOOK_URL()
    if not w:
        logger.info(f"[DRY-RUN] blocks: {len(blocks)} blocks")
        return False
    resp = http.request('POST', w, body=json.dumps({'blocks': blocks}).encode(),
                        headers={'Content-Type': 'application/json'})
    return resp.status == 200
