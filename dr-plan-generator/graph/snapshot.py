"""
graph/snapshot.py — 图快照导出与离线加载

为什么需要这个模块
------------------
生成 DR 计划原本必须实时查 Neptune，而 Neptune 位于**主区**。主区一旦故障，
查不了图 → 生不成计划 → 工具在最需要它的时刻不可用。

AWS Well-Architected REL13-BP02 把这一点列为容灾的头号反模式
（"Dependency on control plane operations during recovery"），
REL11-BP04 进一步要求恢复过程只依赖数据面、不依赖控制面。

因此正确的工作方式是两段式：

1. **平时**（主区健康）：``snapshot`` 导出图快照，产物落到主区之外
   （跨区复制的 S3 / DR 区本地磁盘）。
2. **灾时**：``plan --offline <snapshot.json>`` 只读快照生成计划，
   全程不碰 Neptune。

加载路径刻意只用标准库，不 import ``neptune_client``/``boto3``——
离线生成计划不应当依赖任何指向主区的客户端。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: 快照格式版本。结构不兼容变更时递增，``load_snapshot`` 会拒绝未知版本。
SNAPSHOT_VERSION = 1

#: 快照被认为“新鲜”的秒数上限。超过只**告警**，绝不阻断——见 load_snapshot。
DEFAULT_MAX_AGE_SECONDS = 86400


class SnapshotError(Exception):
    """快照文件缺失、格式错误或版本不兼容。"""


def _utcnow_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


def build_snapshot(
    scope: str,
    source: str,
    nodes: List[Dict[str, Any]],
    edges: List[Dict[str, Any]],
    region: str = "",
    profile_name: str = "",
) -> Dict[str, Any]:
    """把节点与边组装成带元数据的快照 dict。

    与 Neptune 无关的纯函数，便于单测。

    Args:
        scope: ``region`` / ``az`` / ``service``。
        source: 故障源标识（快照按此范围抽取）。
        nodes: 节点 dict 列表。
        edges: 边 dict 列表（含 ``from`` / ``to`` / ``type``）。
        region: 快照导出时所在的 AWS region。
        profile_name: 导出时使用的 workload profile 名称。

    Returns:
        快照 dict。
    """
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "created_at": _utcnow_iso(),
        "scope": scope,
        "source": source,
        "region": region,
        "profile_name": profile_name,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "nodes": nodes,
        "edges": edges,
    }


def export_snapshot(
    scope: str,
    source: str,
    path: str,
    analyzer: Optional[Any] = None,
    region: str = "",
    profile_name: str = "",
) -> Dict[str, Any]:
    """查 Neptune 抽取范围内子图，写出快照文件。

    这是**平时**才跑的路径，允许依赖 Neptune。

    Args:
        scope: ``region`` / ``az`` / ``service``。
        source: 故障源标识。
        path: 输出文件路径，父目录会自动创建。
        analyzer: 可选的 GraphAnalyzer（便于注入测试替身）。
            为 None 时在函数内部惰性构造，避免模块导入期就拉起 Neptune 依赖。
        region: 写入快照元数据的 region。
        profile_name: 写入快照元数据的 profile 名。

    Returns:
        写出的快照 dict。
    """
    if analyzer is None:
        # 惰性导入：离线加载路径不应被迫导入 Neptune 客户端。
        from graph.graph_analyzer import GraphAnalyzer

        analyzer = GraphAnalyzer()

    subgraph = analyzer.extract_affected_subgraph(scope, source)
    snapshot = build_snapshot(
        scope=scope,
        source=source,
        nodes=subgraph.get("nodes", []),
        edges=subgraph.get("edges", []),
        region=region,
        profile_name=profile_name,
    )

    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, ensure_ascii=False)

    logger.info(
        "Snapshot written to %s (%d nodes, %d edges)",
        path,
        snapshot["node_count"],
        snapshot["edge_count"],
    )
    return snapshot


def snapshot_age_seconds(snapshot: Dict[str, Any], now: Optional[datetime] = None) -> Optional[float]:
    """返回快照年龄（秒）。``created_at`` 缺失或无法解析时返回 None。

    Args:
        snapshot: 快照 dict。
        now: 可注入的“当前时间”，便于单测。

    Returns:
        年龄秒数，或 None（无法判定）。
    """
    created = snapshot.get("created_at")
    if not created:
        return None
    try:
        ts = datetime.fromisoformat(created)
    except (TypeError, ValueError):
        logger.warning("Snapshot created_at is not ISO 8601: %r", created)
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return (current - ts).total_seconds()


def load_snapshot(
    path: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
) -> Dict[str, Any]:
    """读取并校验快照文件。**只用标准库，不碰 Neptune。**

    过期策略刻意是「告警不阻断」：真灾时手上只有一份几小时前的快照是常态，
    此时拒绝生成计划等于让工具在最需要的时刻失效。陈旧程度必须让使用者知道
    （也必须留在计划产物里供审计追溯），但不能成为硬闸门。

    Args:
        path: 快照文件路径。
        max_age_seconds: 超过此年龄仅记 WARNING。

    Returns:
        快照 dict，额外带 ``age_seconds`` 与 ``stale`` 两个派生键。

    Raises:
        SnapshotError: 文件不存在、非法 JSON、版本不兼容或缺必需键。
    """
    if not os.path.exists(path):
        raise SnapshotError(f"Snapshot file not found: {path}")

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise SnapshotError(f"Snapshot is not valid JSON: {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise SnapshotError(f"Snapshot root must be a JSON object, got {type(data).__name__}")

    version = data.get("snapshot_version")
    if version != SNAPSHOT_VERSION:
        raise SnapshotError(
            f"Unsupported snapshot_version {version!r} "
            f"(this build understands {SNAPSHOT_VERSION})"
        )

    for key in ("nodes", "edges"):
        if not isinstance(data.get(key), list):
            raise SnapshotError(f"Snapshot key {key!r} must be a list")

    age = snapshot_age_seconds(data)
    data["age_seconds"] = age
    data["stale"] = bool(age is not None and age > max_age_seconds)

    if data["stale"]:
        logger.warning(
            "Snapshot %s is %.1f hours old (threshold %.1f h) — "
            "proceeding anyway; recovery must not be blocked by snapshot age.",
            path,
            (age or 0) / 3600.0,
            max_age_seconds / 3600.0,
        )

    return data
