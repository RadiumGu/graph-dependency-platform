"""
playbook_engine.py - Playbook 匹配与恢复建议生成
4个预定义 Playbook + 动态降级
"""
import logging

logger = logging.getLogger(__name__)

PLAYBOOKS = {
    'single_az_down': {
        'name': '单 AZ 不可用',
        'risk': 'LOW',
        'trigger': lambda ctx: (
            ctx.get('service_info', {}).get('fault_boundary') == 'az'
            and ctx.get('signal', {}).get('metric') == 'az_availability'
        ),
        'steps': [
            '确认其他 AZ 副本健康：`kubectl get pods -o wide`',
            '将故障 AZ 从 ALB target group 摘除',
            '扩容健康 AZ 副本：`kubectl scale deployment/{svc} --replicas={n}`',
            '验证流量切换：`aws elbv2 describe-target-health`',
            '告警降噪：屏蔽故障 AZ 告警 30 分钟',
        ],
        'auto_exec': True  # LOW 风险可半自动
    },
    'crashloop': {
        'name': 'Pod CrashLoopBackOff',
        'risk': 'MEDIUM',
        'trigger': lambda ctx: (
            ctx.get('signal', {}).get('metric') == 'pod_status'
            and 'crashloop' in str(ctx.get('signal', {}).get('value', '')).lower()
        ),
        'steps': [
            '获取 Pod 日志（最近 100 行）：`kubectl logs {pod} --tail=100`',
            '分析错误类型（OOM / ConfigError / 未知）',
            '如 OOM → 临时增加 memory limit，执行 rollout restart',
            '如 ConfigError → 检查 ConfigMap/Secret 最近变更，考虑回滚',
            '如未知 → 升级人工处理',
        ],
        'auto_exec': False
    },
    'db_connection_exhausted': {
        'name': '数据库连接池耗尽',
        'risk': 'LOW',
        'trigger': lambda ctx: (
            ctx.get('signal', {}).get('metric') in ('rds_connections', 'db_timeout_errors')
            and ctx.get('signal', {}).get('value', 0) > 0.9
        ),
        'steps': [
            '立即：重启受影响的应用 Pod（释放数据库连接）',
            '短期：调大 RDS max_connections（参数组）',
            '后续诊断：分析 slow query log，定位连接泄漏',
        ],
        'auto_exec': True
    },
    'alb_5xx_spike': {
        'name': '全站 5xx 率飙升',
        'risk': 'DEPENDS',
        'trigger': lambda ctx: (
            ctx.get('signal', {}).get('metric') in ('alb_5xx_rate', 'error_rate')
            and ctx.get('signal', {}).get('value', 0) > 0.2
        ),
        'steps': [
            '查 DeepFlow：确认哪个服务返回 5xx（需调用链分析）',
            '查 Neptune：确认该服务的 recovery_priority 和 fault_boundary',
            '如 Tier0 → 立即 rollout restart + 扩容',
            '如配置变更导致 → 回滚 deployment：`kubectl rollout undo deployment/{svc}`',
            '如外部依赖 → 启用 circuit breaker / fallback',
        ],
        'auto_exec': False
    }
}

def match(classification: dict) -> dict:
    """
    给定故障分类结果，返回匹配的 Playbook 和恢复建议
    
    返回: {
        'matched_playbook': 'single_az_down' | None,
        'playbook_detail': {...},
        'steps': [...],
        'can_auto_exec': bool,
        'risk': 'LOW|MEDIUM|HIGH|DEPENDS'
    }
    """
    # 尝试匹配预定义 Playbook
    for pb_id, pb in PLAYBOOKS.items():
        try:
            if pb['trigger'](classification):
                logger.info(f"Matched playbook: {pb_id}")
                svc = classification.get('affected_service', 'UNKNOWN')
                steps = [s.replace('{svc}', svc) for s in pb['steps']]
                
                # P0 永远不全自动
                can_auto = pb['auto_exec'] and classification.get('severity') != 'P0'
                
                return {
                    'matched_playbook': pb_id,
                    'playbook_name': pb['name'],
                    'steps': steps,
                    'can_auto_exec': can_auto,
                    'risk': pb['risk'],
                    'mode': 'semi-auto' if can_auto else 'suggest'
                }
        except Exception as e:
            logger.warning(f"Playbook {pb_id} trigger check failed: {e}")
    
    # 无匹配 Playbook → 动态生成通用建议
    logger.info("No playbook matched, generating dynamic suggestions")
    return _dynamic_suggest(classification)

def _dynamic_suggest(classification: dict) -> dict:
    """基于故障分类生成通用恢复建议"""
    svc = classification.get('affected_service', 'UNKNOWN')
    severity = classification.get('severity', 'P2')
    svc_info = classification.get('service_info', {})
    
    steps = [
        f'检查 {svc} 服务状态：`kubectl get pods -l app={svc} -o wide`',
        f'查看最近日志：`kubectl logs -l app={svc} --tail=50`',
        f'检查服务事件：`kubectl describe deployment {svc}`',
        '查 CloudWatch 最近告警历史，确认是否有相关指标异常',
        '如无明显原因，执行 rollout restart 作为初步恢复',
    ]
    
    if severity == 'P0':
        steps.insert(0, '⚠️ P0 故障：先诊断，不要盲目重启')
        steps.insert(1, '查 DeepFlow（$CLICKHOUSE_HOST:20416）：确认调用链根因')
    
    return {
        'matched_playbook': None,
        'playbook_name': '动态生成（无匹配 Playbook）',
        'steps': steps,
        'can_auto_exec': False,
        'risk': 'UNKNOWN',
        'mode': 'suggest'
    }
