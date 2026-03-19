"""
fault_classifier.py - 故障严重度评估
P0 / P1 / P2 分级逻辑
"""
import logging
from . import neptune_queries as nq

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
