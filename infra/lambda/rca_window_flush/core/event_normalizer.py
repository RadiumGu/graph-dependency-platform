"""
event_normalizer.py - 告警事件标准化层

将来自不同信号源（CloudWatch Alarm、DeepFlow、手动触发）的告警
统一转换为 UnifiedAlertEvent，消除字段命名差异。

fingerprint = sha256(source + service_name + metric_name + threshold_direction)
用于跨时间窗口的去重。
"""
import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# CloudWatch MetricName → 语义 metric_name 映射（复用 handler.py 的映射）
_CW_METRIC_MAP: dict[str, str] = {
    'HTTPCode_Target_5XX_Count': 'error_rate',
    'HTTPCode_Target_4XX_Count': 'error_rate',
    'TargetResponseTime': 'latency_p99',
    'CPUUtilization': 'cpu_utilization',
    'MemoryUtilization': 'memory_utilization',
    'DatabaseConnections': 'db_connections',
    'ReadLatency': 'db_latency',
    'WriteLatency': 'db_latency',
}

_DEFAULT_SERVICE = os.environ.get('DEFAULT_AFFECTED_SERVICE', 'petsite')


@dataclass
class UnifiedAlertEvent:
    """标准化后的告警事件。

    所有字段均有默认值，调用方按需填写。
    fingerprint 在 __post_init__ 中自动生成（若未手动传入）。
    """
    alert_id: str = ''
    fingerprint: str = ''
    source: str = ''              # cloudwatch_alarm | deepflow | manual
    title: str = ''
    body: str = ''
    severity: str = 'P2'          # P0 | P1 | P2
    status: str = 'ALARM'         # ALARM | OK | INSUFFICIENT_DATA
    start_time: str = ''
    # 服务定位
    service_name: str = ''        # Neptune 服务名（CANONICAL 规范化后）
    service_namespace: str = 'default'
    k8s_cluster_name: str = ''
    # 指标信息
    metric_name: str = ''
    metric_value: float = 0.0
    threshold: float = 0.0
    threshold_direction: str = 'above'  # above | below
    # 关联信息
    trace_id: str = ''
    deploy_id: str = ''
    alarm_name: str = ''
    # 原始信号（保留便于调试）
    raw: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        """自动填充 fingerprint 和 start_time。"""
        if not self.start_time:
            self.start_time = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        if not self.fingerprint:
            self.fingerprint = _make_fingerprint(
                self.source, self.service_name,
                self.metric_name, self.threshold_direction,
            )


def _make_fingerprint(source: str, service_name: str,
                      metric_name: str, threshold_direction: str) -> str:
    """计算告警指纹（sha256 前 16 位十六进制）。

    Args:
        source: 信号来源
        service_name: 规范化服务名
        metric_name: 语义指标名
        threshold_direction: 阈值方向

    Returns:
        16 字符十六进制字符串
    """
    raw = f"{source}|{service_name}|{metric_name}|{threshold_direction}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _canonical_service(raw_name: str) -> str:
    """将 K8s deployment 名 / 裸服务名映射到 Neptune 规范服务名。

    Args:
        raw_name: 原始服务名（可能是 deployment 名或已规范化的服务名）

    Returns:
        Neptune 服务名；若映射不到则返回原始值（小写化）
    """
    from config import CANONICAL
    normalized = raw_name.strip().lower()
    # 直接命中 CANONICAL key（deployment 名）
    if normalized in CANONICAL:
        return CANONICAL[normalized]
    # 已是 Neptune 服务名（CANONICAL value）
    if normalized in set(CANONICAL.values()):
        return normalized
    # 部分匹配：deployment 名包含服务关键词
    for dep, svc in CANONICAL.items():
        if dep in normalized or normalized in dep:
            return svc
    logger.debug(f"service_name not in CANONICAL, using as-is: {normalized}")
    return normalized


class EventNormalizer:
    """将原始信号字典转换为 UnifiedAlertEvent。

    支持的信号格式：
    - CloudWatch Alarm SNS 消息（含 AlarmName、Trigger、NewStateValue）
    - handler._parse_cw_alarm 的输出格式（含 source、affected_resource、metric）
    - 手动 / 其他格式（直接包含 service_name / affected_resource）
    """

    def normalize(self, signal: dict) -> Optional['UnifiedAlertEvent']:
        """标准化单条信号。

        Args:
            signal: 原始信号字典

        Returns:
            UnifiedAlertEvent，或 None（信号无效/应跳过时）
        """
        # 已经是 _parse_cw_alarm 输出的格式
        if signal.get('source') == 'cloudwatch_alarm':
            return self._from_parsed_cw(signal)

        # 原始 CloudWatch Alarm SNS 消息
        if 'AlarmName' in signal:
            return self._from_raw_cw_alarm(signal)

        # 手动 / generic
        return self._from_generic(signal)

    # ── 各来源解析器 ─────────────────────────────────────────────────────────

    def _from_parsed_cw(self, signal: dict) -> 'UnifiedAlertEvent':
        """处理 handler._parse_cw_alarm 格式输出。"""
        raw_svc = signal.get('affected_resource', _DEFAULT_SERVICE)
        svc = _canonical_service(raw_svc)
        metric = signal.get('metric', '')
        value = float(signal.get('value', 0))
        threshold = float(signal.get('threshold', 0))
        direction = 'above' if value >= threshold else 'below'

        return UnifiedAlertEvent(
            source='cloudwatch_alarm',
            title=f"CW Alarm: {metric} on {svc}",
            body=signal.get('alarm_name', ''),
            service_name=svc,
            metric_name=metric,
            metric_value=value,
            threshold=threshold,
            threshold_direction=direction,
            alarm_name=signal.get('alarm_name', ''),
            raw=signal,
        )

    def _from_raw_cw_alarm(self, alarm: dict) -> Optional['UnifiedAlertEvent']:
        """处理原始 CloudWatch Alarm SNS 消息。"""
        if alarm.get('NewStateValue') not in ('ALARM',):
            return None

        trigger = alarm.get('Trigger', {})
        description = alarm.get('AlarmDescription', '')
        alarm_name = alarm.get('AlarmName', '')

        # 从 AlarmDescription 提取 service: 标签
        raw_svc = _DEFAULT_SERVICE
        for token in description.split():
            if token.startswith('service:'):
                raw_svc = token.split(':', 1)[1]
                break
        svc = _canonical_service(raw_svc)

        metric_raw = trigger.get('MetricName', '')
        metric = _CW_METRIC_MAP.get(metric_raw, metric_raw)
        threshold = float(trigger.get('Threshold', 0))
        value_str = alarm.get('NewStateReason', '0')
        # NewStateReason 含数字，尝试提取
        import re
        m = re.search(r'(\d+(?:\.\d+)?)', value_str)
        value = float(m.group(1)) if m else 0.0
        direction = 'above' if value >= threshold else 'below'

        return UnifiedAlertEvent(
            source='cloudwatch_alarm',
            title=f"CW Alarm: {alarm_name}",
            body=description,
            status=alarm.get('NewStateValue', 'ALARM'),
            service_name=svc,
            metric_name=metric,
            metric_value=value,
            threshold=threshold,
            threshold_direction=direction,
            alarm_name=alarm_name,
            raw=alarm,
        )

    def _from_generic(self, signal: dict) -> 'UnifiedAlertEvent':
        """处理手动调用 / 未知格式。"""
        raw_svc = (
            signal.get('service_name')
            or signal.get('affected_resource')
            or signal.get('affected_service')
            or _DEFAULT_SERVICE
        )
        svc = _canonical_service(raw_svc)
        metric = signal.get('metric', signal.get('metric_name', ''))
        value = float(signal.get('value', signal.get('metric_value', 0)))
        threshold = float(signal.get('threshold', 0))
        direction = 'above' if value >= threshold else 'below'

        return UnifiedAlertEvent(
            source=signal.get('source', 'manual'),
            title=signal.get('title', f"Alert on {svc}"),
            body=signal.get('body', ''),
            severity=signal.get('severity', 'P2'),
            service_name=svc,
            service_namespace=signal.get('service_namespace', 'default'),
            k8s_cluster_name=signal.get('k8s_cluster_name', ''),
            metric_name=metric,
            metric_value=value,
            threshold=threshold,
            threshold_direction=direction,
            trace_id=signal.get('trace_id', ''),
            deploy_id=signal.get('deploy_id', ''),
            alarm_name=signal.get('alarm_name', ''),
            raw=signal,
        )
