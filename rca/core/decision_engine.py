"""
decision_engine.py - 自动化决策引擎（Phase 4 闭环层）

根据故障严重度和 RCA 置信度，决定自动化处理程度：
  manual      — 仅通知，需人工操作
  semi_auto   — 通知 + 展示建议操作（Slack 确认按钮）
  auto        — 执行白名单内的安全操作，不需人工确认

核心数据结构：
  AUTOMATION_POLICY: severity × confidence_band → action_level
  SAFE_AUTO_ACTIONS: 允许自动执行的操作白名单
  NEVER_AUTO: 绝对禁止自动执行的操作集合
"""
import logging
import os

logger = logging.getLogger(__name__)

# ── 策略矩阵 ──────────────────────────────────────────────────────────────────
# confidence_band: high(≥80), medium(50-79), low(<50)
# value: manual | semi_auto | auto
AUTOMATION_POLICY: dict[str, dict[str, str]] = {
    'P0': {
        'high':   'semi_auto',   # P0 无论如何不全自动，最多 semi_auto
        'medium': 'semi_auto',
        'low':    'manual',
    },
    'P1': {
        'high':   'semi_auto',
        'medium': 'semi_auto',
        'low':    'manual',
    },
    'P2': {
        'high':   'auto',        # P2 高置信度允许自动处理
        'medium': 'semi_auto',
        'low':    'manual',
    },
}

# 允许自动执行的安全操作（幂等、可回滚、不影响数据）
SAFE_AUTO_ACTIONS: frozenset[str] = frozenset({
    'restart_pod',
    'scale_up_replicas',
    'clear_cache',
    'rotate_connection_pool',
    'enable_circuit_breaker',
    'throttle_traffic',
    'send_slack_notification',
    'create_pagerduty_alert',
    'trigger_runbook',
})

# 绝对禁止自动执行的操作（破坏性/不可逆/影响大）
NEVER_AUTO: frozenset[str] = frozenset({
    'delete_database',
    'drop_table',
    'terminate_instance',
    'delete_stack',
    'modify_iam_policy',
    'disable_service',
    'rollback_deployment',   # 需要人工确认变更范围
    'force_failover_rds',
    'detach_ebs_volume',
    'modify_security_group',
})


class DecisionEngine:
    """评估 RCA 结果并输出自动化决策建议。

    Usage:
        engine = DecisionEngine()
        decision = engine.evaluate(severity='P1', rca_result={...})
    """

    def evaluate(self, severity: str, rca_result: dict) -> dict:
        """根据严重度和 RCA 结果输出决策建议。

        Args:
            severity: 故障严重度（P0 | P1 | P2）
            rca_result: analyze() 或 analyze_group() 的返回值

        Returns:
            决策字典，含以下字段：
              action_level   (str)  — manual | semi_auto | auto
              confidence     (float) — RCA 置信度（0.0-1.0）
              confidence_band (str) — high | medium | low
              proposed_action (str) — 建议操作描述
              safe_to_auto   (bool) — 是否满足自动执行条件
              never_auto     (bool) — 是否命中禁止自动列表
              reason         (str)  — 决策理由
        """
        # 1. 提取置信度
        top = (rca_result.get('root_cause_candidates') or [{}])[0]
        confidence = float(top.get('confidence', 0) or 0)

        # 如果有 rag_report 的 confidence，优先使用（0-100 → 0.0-1.0）
        rag = rca_result.get('rag_report', {}) or {}
        rag_conf = rag.get('confidence', None)
        if rag_conf is not None:
            try:
                rag_conf_normalized = float(rag_conf) / 100.0
                confidence = max(confidence, rag_conf_normalized)
            except (TypeError, ValueError):
                pass

        confidence_band = self._confidence_band(confidence)

        # 2. 查策略矩阵
        sev = severity if severity in AUTOMATION_POLICY else 'P1'
        action_level = AUTOMATION_POLICY[sev].get(confidence_band, 'manual')

        # 3. 推断建议操作
        proposed_action = self._propose_action(rca_result, rag)
        safe_to_auto = proposed_action in SAFE_AUTO_ACTIONS
        never_auto = proposed_action in NEVER_AUTO

        # 4. 安全降级：命中 NEVER_AUTO 强制 manual
        if never_auto:
            action_level = 'manual'
        # 策略允许 auto 但操作不在白名单 → 降为 semi_auto
        elif action_level == 'auto' and not safe_to_auto:
            action_level = 'semi_auto'

        # 5. 检查 feature flag
        try:
            from config import FEATURE_FLAGS
            if not FEATURE_FLAGS.get('auto_remediation_enabled', False):
                if action_level == 'auto':
                    action_level = 'semi_auto'
        except Exception:
            pass

        reason = self._build_reason(
            severity, confidence_band, action_level,
            safe_to_auto, never_auto, proposed_action,
        )

        result = {
            'action_level': action_level,
            'confidence': round(confidence, 3),
            'confidence_band': confidence_band,
            'proposed_action': proposed_action,
            'safe_to_auto': safe_to_auto,
            'never_auto': never_auto,
            'reason': reason,
            'action': action_level,  # 兼容 window_flush_handler.py 读取
        }
        logger.info(
            f"DecisionEngine: sev={severity} conf={confidence:.2f}({confidence_band}) "
            f"→ {action_level} action={proposed_action}"
        )
        return result

    # ── 内部辅助 ─────────────────────────────────────────────────────────────

    @staticmethod
    def _confidence_band(confidence: float) -> str:
        """将置信度数值分为三档。

        Args:
            confidence: 0.0-1.0

        Returns:
            high | medium | low
        """
        if confidence >= 0.80:
            return 'high'
        if confidence >= 0.50:
            return 'medium'
        return 'low'

    @staticmethod
    def _propose_action(rca_result: dict, rag: dict) -> str:
        """从 RCA 结果推断具体建议操作。

        Args:
            rca_result: analyze() 的返回值
            rag: generate_rca_report() 的返回值

        Returns:
            操作名称字符串（对应 SAFE_AUTO_ACTIONS 中的 key）
        """
        # 优先使用 Graph RAG 的建议操作
        raw_action = rag.get('recommended_action', '') or ''
        raw_action_lower = raw_action.lower()

        # 关键词映射到操作 key
        keyword_map = [
            (['restart', 'pod', 'container', '重启'], 'restart_pod'),
            (['scale', 'replica', '扩容', '增加副本'], 'scale_up_replicas'),
            (['cache', '缓存', 'clear'], 'clear_cache'),
            (['circuit', '熔断', 'breaker'], 'enable_circuit_breaker'),
            (['throttle', '限流', 'rate limit'], 'throttle_traffic'),
            (['connection pool', '连接池', 'rotate'], 'rotate_connection_pool'),
            (['rollback', '回滚'], 'rollback_deployment'),
            (['failover', 'rds', '主从切换'], 'force_failover_rds'),
            (['iam', 'policy', '权限'], 'modify_iam_policy'),
            (['security group', '安全组'], 'modify_security_group'),
        ]
        for keywords, action_key in keyword_map:
            if any(kw in raw_action_lower for kw in keywords):
                return action_key

        # 从 RCA 证据推断（infra fault → restart_pod）
        candidates = rca_result.get('root_cause_candidates', []) or []
        for c in candidates[:1]:
            for ev in c.get('evidence', []):
                ev_lower = str(ev).lower()
                if 'ec2' in ev_lower and ('stop' in ev_lower or 'terminat' in ev_lower):
                    return 'scale_up_replicas'
                if 'pod' in ev_lower and ('crash' in ev_lower or 'oom' in ev_lower or 'restart' in ev_lower):
                    return 'restart_pod'

        # 默认：发送 Slack 通知（最安全的操作）
        return 'send_slack_notification'

    @staticmethod
    def _build_reason(severity: str, confidence_band: str, action_level: str,
                      safe_to_auto: bool, never_auto: bool, proposed_action: str) -> str:
        """构建决策理由说明。

        Args:
            severity: 故障严重度
            confidence_band: 置信度档位
            action_level: 决策结果
            safe_to_auto: 是否安全自动执行
            never_auto: 是否命中禁止列表
            proposed_action: 建议操作

        Returns:
            理由字符串
        """
        if never_auto:
            return f"操作 '{proposed_action}' 在禁止自动执行列表中，强制人工确认"
        if action_level == 'manual':
            return f"{severity}+{confidence_band}置信度，需人工决策"
        if action_level == 'semi_auto':
            if not safe_to_auto:
                return f"建议操作 '{proposed_action}' 不在安全自动化白名单，降级为半自动"
            return f"{severity}+{confidence_band}置信度，展示操作建议等待人工确认"
        if action_level == 'auto':
            return f"P2+高置信度+白名单操作 '{proposed_action}'，符合自动执行条件"
        return ''
