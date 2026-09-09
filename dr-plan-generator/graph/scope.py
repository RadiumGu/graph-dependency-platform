"""
graph/scope.py — 白名单锚定的切换范围解析

为什么弃用原来的黑名单
----------------------
原 ``plan_generator._filter_excluded`` 按服务名逐个剔除。三个致命问题：

1. **要穷举名字。** 五个 ``etl_*`` Lambda 得一个个列，新增一个就漏一个，
   而漏掉的表现是「DR 计划里多了一个不该切的东西」——不报错。
2. **污染节点会漏进来。** 图里有 DeepFlow 注入的虚构服务
   （``awesomeshop`` 命名空间，副本数全 0）。黑名单模式下必须逐个拉黑。
3. **类型层面表达不了。** ``NeptuneCluster`` 这个**类型**没问题，是那个
   **实例**（图谱自身存储）不该切。类型注册表说不了这件事。

改为白名单锚定
--------------
以 profile 的 ``services`` 为锚点集合（profile 本身就是「这个 workload 由
哪些服务组成」的权威声明），沿范围边做可达性遍历。不可达的自动出局——
Neptune、``etl_*``、``awesomeshop`` 那批都不需要点名。

外加一层 ``dr.excluded`` 显式拒绝（带 reason），处理「确实可达但不该切」
的情况。两层是互补的：可达性管普遍情形，显式拒绝管例外，而且**排除必须
带原因**——审计要能看出是有意排除而不是漏了。

本模块是**纯函数**：只吃 nodes/edges，不碰 Neptune。离线路径（``plan --offline``）
是灾时的主路径，范围裁剪必须在那条路上同样生效。
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

#: 兜底的范围边集合。正常从 registry/plan_policy.yaml 的 scope_policy 读，
#: 此处仅在策略缺失时使用，保持与该文件一致。
_FALLBACK_SCOPE_EDGES = (
    "AccessesData", "Calls", "Delegates", "DependsOn", "InvokesTool",
    "Retrieves", "WritesTo", "PublishesTo", "RunsOn", "BelongsTo",
    "ForwardsTo", "RoutesTo", "Invokes", "InvokesVia",
)

#: 兜底的排序边集合 = 契约里 dependency: true 的全集。
#:
#: ⚠️ 这份清单曾漂移：注释写「6 种」、实际列 6 项，而契约 2026-09-08 已是 10 种，
#: 漏掉 Invokes（16 条）/ PublishesTo（2 条）/ RoutesToRuntime / RoutesVia。
#: 恢复顺序按依赖边拓扑排，漏边就会把该先恢复的排到后面。
#: 与契约的一致性由 tests/test_53_drift_label_coverage.py 静态锁定 ——
#: 别再手改这里而不改那条断言。
_FALLBACK_ORDERING_EDGES = (
    "AccessesData", "Calls", "Delegates", "DependsOn", "Invokes",
    "InvokesTool", "PublishesTo", "Retrieves", "RoutesToRuntime", "RoutesVia",
)

_FALLBACK_EXCLUDED_VERIFY = ("refuted",)


@dataclass
class ExclusionRecord:
    """一条排除记录。``reason`` 必填——审计要能看出为何被排除。"""

    name: str
    node_type: str
    reason: str
    rule: str


@dataclass
class ScopedSubgraph:
    """裁剪后的子图与排除清单。"""

    nodes: List[Dict[str, Any]] = field(default_factory=list)
    edges: List[Dict[str, Any]] = field(default_factory=list)
    excluded: List[ExclusionRecord] = field(default_factory=list)
    #: 实际用作起点的锚点名（用于诊断：锚点一个都没命中时范围会是空的）
    anchors_matched: List[str] = field(default_factory=list)
    anchors_missing: List[str] = field(default_factory=list)

    def as_subgraph(self) -> Dict[str, Any]:
        """转成 GraphAnalyzer 期待的 ``{"nodes":…, "edges":…}``。"""
        return {"nodes": self.nodes, "edges": self.edges}

    def exclusion_table(self) -> List[Dict[str, str]]:
        """摊平成可渲染的行，供报告展示。"""
        return [
            {"name": e.name, "type": e.node_type, "reason": e.reason, "rule": e.rule}
            for e in self.excluded
        ]


class ScopeResolver:
    """按 profile 锚点与显式排除规则裁剪子图。

    Args:
        anchors: 锚点服务名（通常来自 profile 的 ``services`` 键）。
        excluded_rules: ``dr.excluded`` 列表，每项含 ``reason`` 及
            ``name`` / ``pattern`` / ``type`` / ``namespace`` 之一。
        scope_edge_types: 判定归属可走的边。
        ordering_edge_types: 决定先后的边（供调用方排序用）。
        excluded_verify_statuses: 不采信的边验证状态。
    """

    def __init__(
        self,
        anchors: Sequence[str],
        excluded_rules: Optional[Sequence[Dict[str, Any]]] = None,
        scope_edge_types: Optional[Iterable[str]] = None,
        ordering_edge_types: Optional[Iterable[str]] = None,
        excluded_verify_statuses: Optional[Iterable[str]] = None,
    ) -> None:
        self.anchors = list(anchors)
        self.excluded_rules = list(excluded_rules or [])
        self.scope_edge_types: Set[str] = set(scope_edge_types or _FALLBACK_SCOPE_EDGES)
        self.ordering_edge_types: Set[str] = set(
            ordering_edge_types or _FALLBACK_ORDERING_EDGES
        )
        self.excluded_verify_statuses: Set[str] = set(
            excluded_verify_statuses or _FALLBACK_EXCLUDED_VERIFY
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def from_profile(
        cls, profile: Any, policy: Optional[Any] = None
    ) -> "ScopeResolver":
        """从 profile（+ 可选 policy）构造。

        Args:
            profile: DRProfile 实例。
            policy: 可选的 PolicyLoader；缺省时用内置兜底边集合。

        Returns:
            ScopeResolver。
        """
        services = profile.get("services", {}) or {}
        anchors: List[str] = []
        for key, meta in services.items():
            anchors.append(key)
            if isinstance(meta, dict):
                # 图谱里的名字可能与 profile 键不同（profile 做名字翻译）
                neptune_name = meta.get("neptune_name")
                if neptune_name and neptune_name != key:
                    anchors.append(neptune_name)
                for alias in meta.get("aliases", []) or []:
                    anchors.append(alias)

        scope_cfg: Dict[str, Any] = {}
        if policy is not None:
            try:
                scope_cfg = policy.raw.get("scope_policy", {}) or {}
            except Exception:  # noqa: BLE001
                scope_cfg = {}

        return cls(
            anchors=anchors,
            excluded_rules=profile.dr_excluded,
            scope_edge_types=scope_cfg.get("scope_edge_types"),
            ordering_edge_types=scope_cfg.get("ordering_edge_types"),
            excluded_verify_statuses=scope_cfg.get("excluded_verify_statuses"),
        )

    def filter_ordering_edges(
        self, edges: Sequence[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """只留下可用于决定恢复先后的边。

        排除两类：非依赖边（位置/归属关系不表达先后）与已证伪的边。
        """
        return [
            e for e in edges
            if e.get("type") in self.ordering_edge_types
            and not self._is_refuted(e)
        ]

    def resolve(
        self, nodes: Sequence[Dict[str, Any]], edges: Sequence[Dict[str, Any]]
    ) -> ScopedSubgraph:
        """裁剪出属于本 workload 的子图。

        Args:
            nodes: 全部候选节点。
            edges: 全部候选边。

        Returns:
            ScopedSubgraph，含保留的节点/边与带原因的排除清单。
        """
        by_name = {n.get("name"): n for n in nodes if n.get("name")}

        # ① 显式拒绝优先：这些即便可达也不进范围。
        denied: Dict[str, ExclusionRecord] = {}
        for node in nodes:
            hit = self._match_exclusion(node)
            if hit is not None:
                reason, rule = hit
                name = str(node.get("name", ""))
                denied[name] = ExclusionRecord(
                    name=name,
                    node_type=str(node.get("type", "")),
                    reason=reason,
                    rule=rule,
                )

        # ② 可达性遍历。已证伪的边不作为归属依据。
        usable_edges = [
            e for e in edges
            if e.get("type") in self.scope_edge_types and not self._is_refuted(e)
        ]
        adjacency: Dict[str, Set[str]] = {}
        for e in usable_edges:
            src, dst = e.get("from", ""), e.get("to", "")
            if not src or not dst:
                continue
            # 无向遍历：归属关系两个方向都算（服务→队列、ALB→服务）
            adjacency.setdefault(src, set()).add(dst)
            adjacency.setdefault(dst, set()).add(src)

        anchor_set = set(self.anchors)
        matched = sorted(anchor_set & set(by_name))
        missing = sorted(anchor_set - set(by_name))

        reachable: Set[str] = set()
        frontier = [a for a in matched if a not in denied]
        while frontier:
            current = frontier.pop()
            if current in reachable:
                continue
            reachable.add(current)
            for neighbour in adjacency.get(current, ()):  # noqa: B007
                if neighbour in reachable or neighbour in denied:
                    continue
                frontier.append(neighbour)

        if not matched:
            logger.warning(
                "No anchor matched the graph (anchors=%d, nodes=%d). Scope would be "
                "empty; check that the profile's service names match graph names.",
                len(anchor_set), len(by_name),
            )

        # ③ 组装结果，并把「可达但未纳入」的记为超出范围。
        kept_nodes: List[Dict[str, Any]] = []
        excluded: List[ExclusionRecord] = []
        for node in nodes:
            name = str(node.get("name", ""))
            if name in denied:
                excluded.append(denied[name])
            elif name in reachable:
                kept_nodes.append(node)
            else:
                excluded.append(ExclusionRecord(
                    name=name,
                    node_type=str(node.get("type", "")),
                    reason="out_of_scope：从本 workload 的锚点不可达",
                    rule="reachability",
                ))

        kept_names = {n.get("name") for n in kept_nodes}
        kept_edges = [
            e for e in edges
            if e.get("from") in kept_names and e.get("to") in kept_names
            and not self._is_refuted(e)
        ]

        return ScopedSubgraph(
            nodes=kept_nodes,
            edges=kept_edges,
            excluded=excluded,
            anchors_matched=matched,
            anchors_missing=missing,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _is_refuted(self, edge: Dict[str, Any]) -> bool:
        """该边是否已被故障注入证伪。

        证伪的边必须剔除：采信它会把**不存在的依赖**写进恢复顺序，
        让计划为一个假约束串行等待。而 untested / inconclusive 予以保留——
        证据不足不等于不存在，容灾场景宁可多算一条。
        """
        status = edge.get("verify_status")
        return bool(status) and str(status) in self.excluded_verify_statuses

    def _match_exclusion(
        self, node: Dict[str, Any]
    ) -> Optional[Tuple[str, str]]:
        """节点是否命中某条显式排除规则。

        Returns:
            ``(reason, rule_description)``，未命中返回 None。
        """
        name = str(node.get("name", ""))
        node_type = str(node.get("type", ""))
        namespace = str(node.get("namespace", "") or "")

        for rule in self.excluded_rules:
            if not isinstance(rule, dict):
                continue
            reason = str(rule.get("reason", "unspecified"))
            if "name" in rule and name == str(rule["name"]):
                return reason, f"name={rule['name']}"
            if "pattern" in rule and fnmatch.fnmatch(name, str(rule["pattern"])):
                return reason, f"pattern={rule['pattern']}"
            if "type" in rule and node_type == str(rule["type"]):
                return reason, f"type={rule['type']}"
            if "namespace" in rule and namespace and namespace == str(rule["namespace"]):
                return reason, f"namespace={rule['namespace']}"
        return None
