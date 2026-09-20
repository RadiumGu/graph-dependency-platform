"""
fault_classifier.py - 故障严重度评估
P0 / P1 / P2 分级逻辑
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


# severity -> strategy 的唯一映射。classify() 里原本是内联的 if/elif，
# classify_group() 修正 severity 后也要跟着改 strategy，所以抽出来共用，
# 避免两处各写一份而漂移。
_STRATEGY_BY_SEVERITY = {
    'P0': 'Diagnose-First',
    'P1': 'Parallel',
    'P2': 'Restore-First',
}

_SEVERITY_ORDER = {'P0': 0, 'P1': 1, 'P2': 2}


def classify_group(group) -> dict:
    """基于整个 EventGroup 分类，而不是只看根因告警。

    ## 为什么需要它

    `classify()` 是**单服务视角**：只算一个 affected_service 的爆炸半径。
    而一个 EventGroup 可能包含多个服务同时告警 —— 那是单服务视角看不到的
    信息，也正是 `TopologyCorrelator` 费力关联出来的东西。

    ## 与 classify() 的关系：基底 + 单调不降的修正

    不重新实现严重度矩阵（那会立刻和 `classify()` 漂移），而是以根因服务的
    `classify()` 结果为基底，再用组级信息**只升不降**地修正。

    单调不降是**有意的安全属性**，不是偷懒：
      · 「多个服务同时告警」不可能是好消息，所以组信息只该加重、不该减轻；
      · 万一拓扑关联本身有噪声（把无关告警并进一组），最坏情况是维持根因
        判定，不会因为噪声把一个真 P0 误降成 P1。

    ## 两路组级输入

    1. `group.severity` —— 组内告警**自报**的最高严重度（来自告警源）。
       `classify()` 算的是**图谱拓扑**推出的严重度。两者视角不同、都可能对，
       所以取更严重的那个。
    2. 组内涉及的不同服务数 —— 多服务同时告警说明影响面广。

    ⚠️ 刻意**不做** P1 → P0 的自动升级：P0 应当由图谱拓扑证据
    （Tier0 服务 + 多个 Tier0 业务能力受损）决定，而不是「告警条数多」。
    否则一次波及面广但无关键业务的抖动就会连发 P0，制造告警疲劳。
    P2 → P1 是安全的，因为 P1 不触发最高级响应。

    ## 历史

    `window_flush_handler.py` 从第一天就在调这个函数，但它**从来不存在**，
    调用被 `except` 接住、静默降级成 `classify(根因服务)` —— 也就是说
    「按 EventGroup 分类」这个设计从写下来就没生效过一次，而 `groups_failed`
    始终是 0，所以没人发现。2026-09-20 补实现。

    Args:
        group: EventGroup（topology_correlator.EventGroup）。

    Returns:
        与 `classify()` 同构的 dict，额外带 group_* 字段：
          group_id / correlation_type / correlation_confidence /
          group_alert_count / group_service_count / group_severity_source
    """
    root_alert = getattr(group, 'root_candidate_alert', None)
    signal = getattr(root_alert, 'raw', {}) if root_alert else {}
    root_svc = getattr(group, 'root_candidate_service', '') or ''

    # 基底：复用单服务逻辑（含 q4_service_info + q1_blast_radius 两次图谱查询）
    out = classify(root_svc, signal)
    base_sev = out['severity']

    alerts = list(getattr(group, 'all_alerts', None) or [])
    svcs = {getattr(a, 'service_name', '') for a in alerts}
    svcs.discard('')
    svc_count = len(svcs)

    # ── 修正 1：与组内告警自报的最高严重度取更严重者 ──
    reported = getattr(group, 'severity', None) or base_sev
    sev = min((base_sev, reported), key=lambda s: _SEVERITY_ORDER.get(s, 2))
    sev_source = 'graph' if sev == base_sev else 'alert_reported'
    if sev != base_sev and _SEVERITY_ORDER.get(reported, 2) < _SEVERITY_ORDER.get(base_sev, 2):
        logger.info(
            "classify_group: %s 组内告警自报 %s 比图谱推断 %s 更严重，采用前者",
            getattr(group, 'group_id', '?'), reported, base_sev,
        )

    # ── 修正 2：多服务同时告警 → 影响面广（只做 P2 → P1，理由见 docstring）──
    if svc_count >= 3 and sev == 'P2':
        logger.info(
            "classify_group: %s 组内 %d 个服务同时告警，P2 → P1",
            getattr(group, 'group_id', '?'), svc_count,
        )
        sev = 'P1'
        sev_source = 'group_breadth'

    out['severity'] = sev
    out['strategy'] = _STRATEGY_BY_SEVERITY.get(sev, out['strategy'])

    # 组内所有告警服务并入 affected_services（去重、保持既有顺序在前）
    existing = list(out.get('affected_services') or [])
    for s in sorted(svcs):
        if s not in existing:
            existing.append(s)
    out['affected_services'] = existing

    out.update({
        'group_id': getattr(group, 'group_id', ''),
        'correlation_type': getattr(group, 'correlation_type', 'standalone'),
        'correlation_confidence': getattr(group, 'confidence', 0.0),
        'group_alert_count': len(alerts),
        'group_service_count': svc_count,
        'group_severity_source': sev_source,
    })
    return out
