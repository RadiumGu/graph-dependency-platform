"""
topology_correlator.py - 告警拓扑关联层

职责：将同一时间窗口内的多条告警，按以下优先级分组成 EventGroup：
  1. 拓扑关联  — Neptune q1_blast_radius + q3_upstream_deps 找调用链关系
  2. 命名空间  — 同一 K8s namespace 的告警归为同组
  3. 兜底      — 每条告警独立成组

每个 EventGroup 包含：
  - root_candidate_service / root_candidate_alert：最可能的根因
  - evidence_alerts：佐证告警列表
  - confidence：关联置信度（0.0-1.0）
  - blast_radius：影响服务列表
"""
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class EventGroup:
    """拓扑关联后的告警分组。

    Attributes:
        group_id: 分组唯一 ID（uuid4 hex[:12]）
        root_candidate_service: 最可能是根因的服务名
        root_candidate_alert: 根因告警（UnifiedAlertEvent）
        evidence_alerts: 佐证告警列表（不含根因告警本身）
        correlation_type: topology | namespace | standalone
        confidence: 关联置信度（0.0-1.0）
        blast_radius: 受影响服务列表（来自 Neptune q1）
        created_at: 分组创建时间 ISO 字符串
    """
    group_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    root_candidate_service: str = ''
    root_candidate_alert: Optional[object] = None   # UnifiedAlertEvent
    evidence_alerts: list = field(default_factory=list)
    correlation_type: str = 'standalone'             # topology | namespace | standalone
    confidence: float = 0.5
    blast_radius: list = field(default_factory=list)  # list[str]
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    )

    @property
    def all_alerts(self) -> list:
        """根因告警 + 佐证告警的完整列表。"""
        base = [self.root_candidate_alert] if self.root_candidate_alert else []
        return base + self.evidence_alerts

    @property
    def severity(self) -> str:
        """取分组内最高严重度。"""
        order = {'P0': 0, 'P1': 1, 'P2': 2}
        alerts = self.all_alerts
        if not alerts:
            return 'P2'
        return min(
            (getattr(a, 'severity', 'P2') for a in alerts),
            key=lambda s: order.get(s, 2),
        )


class TopologyCorrelator:
    """将告警列表关联成 EventGroup 列表。

    Usage:
        correlator = TopologyCorrelator()
        groups = correlator.correlate(alerts)
    """

    def correlate(self, alerts: list) -> list:
        """主入口：对告警列表执行拓扑关联。

        Args:
            alerts: UnifiedAlertEvent 列表

        Returns:
            EventGroup 列表（按 confidence 降序）
        """
        if not alerts:
            return []

        if len(alerts) == 1:
            return [self._standalone_group(alerts[0])]

        # 1. 批量查 Neptune 拓扑图（一次性，避免 N+1）
        topology_map = self._build_topology_map(alerts)

        # 2. 拓扑关联
        groups = self._topology_correlate(alerts, topology_map)

        # 3. 对未分组的告警做命名空间关联
        grouped_alert_fps = {
            getattr(a, 'fingerprint', '')
            for g in groups
            for a in g.all_alerts
        }
        ungrouped = [a for a in alerts if getattr(a, 'fingerprint', '') not in grouped_alert_fps]
        groups.extend(self._namespace_correlate(ungrouped))

        # 4. 剩余独立成组
        grouped_fps_after_ns = {
            getattr(a, 'fingerprint', '')
            for g in groups
            for a in g.all_alerts
        }
        for alert in alerts:
            if getattr(alert, 'fingerprint', '') not in grouped_fps_after_ns:
                groups.append(self._standalone_group(alert))

        # 5. 按 confidence 降序
        groups.sort(key=lambda g: g.confidence, reverse=True)
        logger.info(
            f"TopologyCorrelator: {len(alerts)} alerts → {len(groups)} groups "
            f"(topology={sum(1 for g in groups if g.correlation_type=='topology')}, "
            f"namespace={sum(1 for g in groups if g.correlation_type=='namespace')}, "
            f"standalone={sum(1 for g in groups if g.correlation_type=='standalone')})"
        )
        return groups

    # ── 拓扑图构建 ─────────────────────────────────────────────────────────────

    def _build_topology_map(self, alerts: list) -> dict:
        """批量查询 Neptune，建立服务间拓扑关系缓存。

        返回格式：{
            'blast_radius': {service: [downstream_services]},
            'upstream_deps': {service: [upstream_services]},
        }

        Args:
            alerts: UnifiedAlertEvent 列表

        Returns:
            拓扑关系字典
        """
        from neptune import neptune_queries as nq

        services = list({getattr(a, 'service_name', '') for a in alerts if getattr(a, 'service_name', '')})
        blast_map: dict[str, list] = {}
        upstream_map: dict[str, list] = {}

        for svc in services:
            try:
                blast = nq.q1_blast_radius(svc)
                blast_map[svc] = [s.get('name', '') for s in blast.get('services', [])]
            except Exception as e:
                logger.warning(f"q1_blast_radius failed for {svc}: {e}")
                blast_map[svc] = []
            try:
                upstreams = nq.q3_upstream_deps(svc)
                upstream_map[svc] = [u.get('name', '') for u in upstreams]
            except Exception as e:
                logger.warning(f"q3_upstream_deps failed for {svc}: {e}")
                upstream_map[svc] = []

        return {'blast_radius': blast_map, 'upstream_deps': upstream_map}

    # ── 关联策略 ───────────────────────────────────────────────────────────────

    def _topology_correlate(self, alerts: list, topology_map: dict) -> list:
        """基于拓扑关系将告警分组。

        策略：若告警 A 的服务出现在告警 B 服务的 blast_radius 中，
        则 A 可能是 B 的根因，将 B 合并到 A 的 EventGroup。

        Args:
            alerts: UnifiedAlertEvent 列表
            topology_map: _build_topology_map 返回值

        Returns:
            EventGroup 列表
        """
        blast_map = topology_map.get('blast_radius', {})
        upstream_map = topology_map.get('upstream_deps', {})

        # 服务名 → alert 列表
        svc_to_alerts: dict[str, list] = {}
        for a in alerts:
            svc = getattr(a, 'service_name', '')
            svc_to_alerts.setdefault(svc, []).append(a)

        service_names = set(svc_to_alerts.keys())
        groups: list[EventGroup] = []
        assigned: set[str] = set()  # 已分配的 fingerprint

        # 按拓扑深度排序（越上游越可能是根因）
        sorted_svcs = self._sort_by_upstream_first(list(service_names), upstream_map)

        for root_svc in sorted_svcs:
            root_alerts = svc_to_alerts.get(root_svc, [])
            # 找到还没有被分配的 root_alert
            root_unassigned = [
                a for a in root_alerts
                if getattr(a, 'fingerprint', '') not in assigned
            ]
            if not root_unassigned:
                continue

            # 查找该服务的 blast_radius，找相关告警
            downstream_svcs = set(blast_map.get(root_svc, []))
            evidence: list = []
            for evi_svc in downstream_svcs & service_names:
                for evi_alert in svc_to_alerts.get(evi_svc, []):
                    fp = getattr(evi_alert, 'fingerprint', '')
                    if fp and fp not in assigned:
                        evidence.append(evi_alert)

            if not evidence:
                continue  # 没有关联证据，不形成拓扑组

            root_alert = root_unassigned[0]
            root_fp = getattr(root_alert, 'fingerprint', '')

            # 计算置信度：证据服务数 / blast_radius 大小
            blast_size = len(downstream_svcs) or 1
            confidence = min(0.95, 0.5 + len(evidence) / blast_size * 0.5)

            group = EventGroup(
                root_candidate_service=root_svc,
                root_candidate_alert=root_alert,
                evidence_alerts=evidence,
                correlation_type='topology',
                confidence=confidence,
                blast_radius=list(downstream_svcs),
            )
            groups.append(group)

            # 标记已分配
            if root_fp:
                assigned.add(root_fp)
            for ev in evidence:
                fp = getattr(ev, 'fingerprint', '')
                if fp:
                    assigned.add(fp)

        return groups

    def _namespace_correlate(self, alerts: list) -> list:
        """将同一 namespace 的告警分组。

        Args:
            alerts: 未被拓扑关联的 UnifiedAlertEvent 列表

        Returns:
            EventGroup 列表
        """
        ns_map: dict[str, list] = {}
        for a in alerts:
            ns = getattr(a, 'service_namespace', 'default') or 'default'
            ns_map.setdefault(ns, []).append(a)

        groups: list[EventGroup] = []
        for ns, ns_alerts in ns_map.items():
            if len(ns_alerts) < 2:
                continue  # 单条，留给 standalone

            # 取 metric_value 最大的作为 root candidate
            root = max(ns_alerts, key=lambda a: getattr(a, 'metric_value', 0))
            evidence = [a for a in ns_alerts if a is not root]
            group = EventGroup(
                root_candidate_service=getattr(root, 'service_name', ''),
                root_candidate_alert=root,
                evidence_alerts=evidence,
                correlation_type='namespace',
                confidence=0.4,
                blast_radius=[getattr(a, 'service_name', '') for a in evidence],
            )
            groups.append(group)

        return groups

    @staticmethod
    def _standalone_group(alert: object) -> 'EventGroup':
        """单条告警独立成组。

        Args:
            alert: UnifiedAlertEvent

        Returns:
            standalone EventGroup
        """
        svc = getattr(alert, 'service_name', '')
        return EventGroup(
            root_candidate_service=svc,
            root_candidate_alert=alert,
            evidence_alerts=[],
            correlation_type='standalone',
            confidence=0.3,
            blast_radius=[],
        )

    @staticmethod
    def _sort_by_upstream_first(services: list, upstream_map: dict) -> list:
        """按拓扑深度排序：被更多服务依赖的（上游）排在前面。

        用"被其他 service 列为 upstream 的次数"作为权重，次数越多越靠前。

        Args:
            services: 服务名列表
            upstream_map: {service: [upstream_services]}

        Returns:
            排序后的服务名列表
        """
        # 统计每个服务在 upstream_map 中出现的次数（被依赖次数）
        dep_count: dict[str, int] = {s: 0 for s in services}
        for svc, upstreams in upstream_map.items():
            for u in upstreams:
                if u in dep_count:
                    dep_count[u] += 1

        return sorted(services, key=lambda s: dep_count.get(s, 0), reverse=True)
