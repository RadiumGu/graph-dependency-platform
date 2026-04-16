"""
fault_classifier.py - 故障严重度评估
P0 / P1 / P2 分级逻辑

新增方法（Phase 4）：
  classify_group(group) — 对 EventGroup 进行联合分类，利用多告警上下文
  原有 classify() 保持不变（向后兼容）
"""
import logging
from neptune import neptune_queries as nq

logger = logging.getLogger(__name__)

# 严重度决策矩阵
SEVERITY_MATRIX = {
    # (tier0_count, tier0_all_down) -> severity
    # tier0_count: 受影响的 Tier0 BusinessCapability 数量
}

def classify(affected_service: str, signal: dict) -> dict:
    """
    评估故障严重度
    
    signal: {
        "source": "cloudwatch_alarm | deepflow | manual",
        "metric": "error_rate | latency_p99 | availability",
        "value": 0.95,
        "threshold": 0.05
    }
    
    返回: {
        "severity": "P0|P1|P2",
        "strategy": "Diagnose-First|Parallel|Restore-First",
        "affected_capabilities": [...],
        "affected_services": [...],
        "service_info": {...}
    }
    """
    logger.info(f"Classifying fault for service: {affected_service}")
    
    # 获取服务信息
    svc_info = nq.q4_service_info(affected_service)
    
    # 获取爆炸半径
    blast = nq.q1_blast_radius(affected_service)
    capabilities = blast.get('capabilities', [])
    
    # 计算受影响的 Tier0 BusinessCapability 数量
    tier0_bc = [c for c in capabilities if c.get('priority') == 'Tier0']
    
    # 故障服务自身的优先级
    svc_priority = svc_info.get('priority', 'Tier2')
    
    # 严重度评估逻辑
    if svc_priority == 'Tier0' and len(tier0_bc) >= 2:
        # Tier0 服务故障且影响多个核心业务能力 → P0
        severity = 'P0'
        strategy = 'Diagnose-First'
    elif svc_priority == 'Tier0' or len(tier0_bc) >= 1:
        # Tier0 服务故障或影响1个核心业务能力 → P1
        severity = 'P1'
        strategy = 'Parallel'
    else:
        # Tier1/Tier2 服务，不影响核心业务能力 → P2
        severity = 'P2'
        strategy = 'Restore-First'
    
    # 信号强度加权（错误率极高 → 升级严重度）
    error_value = signal.get('value', 0)
    error_threshold = signal.get('threshold', 0.05)
    if error_value > 0.8 and severity == 'P2':
        severity = 'P1'
        strategy = 'Parallel'
    
    return {
        'severity': severity,
        'strategy': strategy,
        'affected_service': affected_service,
        'service_info': svc_info,
        'affected_capabilities': capabilities,
        'affected_services': blast.get('services', []),
        'tier0_impact_count': len(tier0_bc),
        'signal': signal
    }


def classify_group(group: 'EventGroup') -> dict:
    """对 EventGroup 进行联合严重度分类。

    在单告警 classify() 基础上，利用 EventGroup 中的多服务视角：
    - 若分组内有多个 Tier0 服务告警 → 升级为 P0
    - 采用 blast_radius 中已知受影响服务计数加权
    - evidence_alerts 作为辅助信息，不单独触发升级

    保持现有 classify() 不变，本方法是独立的新增方法。

    Args:
        group: topology_correlator.EventGroup 对象

    Returns:
        与 classify() 格式相同的分类字典，新增 group_id 和 evidence_count 字段
    """
    root_alert = group.root_candidate_alert
    svc = group.root_candidate_service

    # 从根因告警中提取 signal
    signal: dict = {}
    if root_alert is not None:
        signal = getattr(root_alert, 'raw', {}) or {}

    # 基础分类（复用现有逻辑）
    base = classify(svc, signal)

    severity = base['severity']
    strategy = base['strategy']
    tier0_count = base.get('tier0_impact_count', 0)

    # 聚合增强：统计分组内涉及的 Tier0 服务数
    evidence_svcs = [
        getattr(a, 'service_name', '') for a in group.evidence_alerts
        if getattr(a, 'service_name', '')
    ]
    tier0_evidence_svcs = 0
    for ev_svc in evidence_svcs:
        try:
            ev_info = nq.q4_service_info(ev_svc)
            if ev_info.get('priority') == 'Tier0':
                tier0_evidence_svcs += 1
        except Exception:
            pass

    combined_tier0 = tier0_count + tier0_evidence_svcs

    # 升级决策：多个 Tier0 服务同时受影响
    if combined_tier0 >= 2 and severity != 'P0':
        severity = 'P0'
        strategy = 'Diagnose-First'
        logger.info(
            f"classify_group: upgraded to P0 — combined_tier0={combined_tier0} "
            f"group={group.group_id}"
        )
    elif combined_tier0 >= 1 and severity == 'P2':
        severity = 'P1'
        strategy = 'Parallel'
        logger.info(
            f"classify_group: upgraded to P1 — combined_tier0={combined_tier0} "
            f"group={group.group_id}"
        )

    result = {
        **base,
        'severity': severity,
        'strategy': strategy,
        'tier0_impact_count': combined_tier0,
        'group_id': group.group_id,
        'evidence_count': len(group.evidence_alerts),
        'correlation_type': group.correlation_type,
    }
    logger.info(
        f"classify_group: group={group.group_id} svc={svc} "
        f"severity={severity} evidence_svcs={len(evidence_svcs)}"
    )
    return result
