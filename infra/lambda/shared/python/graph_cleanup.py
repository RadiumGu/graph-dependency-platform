"""契约驱动的边生命周期收敛。

## 它解决什么

引入之前，边的失效机制是**四套各不相同、且三处缺失**的：

| 源 | 机制 | 覆盖范围 |
|---|---|---|
| DeepFlow | 软删除 `active=false`，阈值 1800s | **仅 `Calls`** |
| X-Ray | 软删除，阈值 6h | **仅 `source='xray'` 的边** |
| AWS | **硬删除** `.drop()`，无时间阈值 | 仅约 14 种节点，**不含任何边** |
| CFN | **无** | — |

后果是 `AccessesData` / `DependsOn` 写了 `active=true` 与 `last_seen`，
却**没有任何路径把 `active` 翻回 false** —— 观测停止后这些边永久留在图里变成
ghost 边，而影响面分析会把它们与真实依赖等权对待。
参见 todo/graph-correctness-audit-and-gaps_20260830-1435.md 缺口 4。

## 两种失效语义，刻意不混

本模块只做**第一种**：

1. **观测式失效（本模块）** —— 只作用于 `dependency_kind='dynamic'` 的边。
   判据是「超过该边类型声明的 expires_seconds 未被刷新」。
   TTL 按边类型从 profiles/graph_contract.yaml 读，不是全局阈值 ——
   「Pod 属于哪个 Node」与「服务 A 调用服务 B」的合理过期时间差两个数量级。
   这与 New Relic 关系 `expires`（默认 PT75M，允许 10min–72h，每类关系各自声明）
   是同一思路。

2. **声明式失效（不在本模块）** —— `static` 边表示「AWS 配置或 CFN 模板声明了这条依赖」。
   它**不该**因为 DeepFlow 没观测到就被置 false —— 那正是 `drift_status`
   的 `declared_not_observed` 要表达的信息，把它当失效会丢掉这个信号。
   声明式失效的正确判据是「本轮采集里模板/配置不再声明它」，
   即 Cartography 的 update_tag 模式，属于各 ETL 自己的 reconcile 职责。

**把两者混在一起会静默删掉真实的架构声明**，所以这里用
`has('dependency_kind','dynamic')` 显式限定。

## 安全姿态

- 默认**只统计不改写**（`GRAPH_EDGE_EXPIRY_ENABLED` 未设或非 'true'），
  与 etl_deepflow 的 `DROP_ENABLED` 默认 false 同一策略：
  部署代码不等于立刻开始改图，开关由人显式打开。
- 只软删除（`active=false`），**从不硬删**。硬删边界（`retention_seconds`）
  当前只有 `Calls` 声明了，由 etl_deepflow 自己的既有逻辑执行。
- 缺 `last_seen` 的历史边（旧 aws/cfn 边只有 `last_updated`/`last_scanned`）
  **不会被匹配**，因此不会被误置 false —— 保守方向是对的。
"""
from __future__ import annotations

import logging
import os

from graph_contract import EDGE_TYPES, TIMESTAMP_FIELD

logger = logging.getLogger()


def expiry_enabled() -> bool:
    return (os.environ.get('GRAPH_EDGE_EXPIRY_ENABLED') or '').strip().lower() == 'true'


def expiring_edge_labels() -> list[tuple[str, int]]:
    """返回 [(边类型, expires_seconds)]，只含声明了 TTL 的类型。

    expires_seconds 为 None 的是结构边 —— 生命周期跟随两端节点、不独立过期
    （对应 Dynatrace 的 static edge 继承 node lifetime 语义）。
    """
    return sorted((lb, spec['expires_seconds'])
                  for lb, spec in EDGE_TYPES.items()
                  if spec.get('expires_seconds'))


def _count_query(label: str, cutoff: int) -> str:
    return (f"g.E().hasLabel('{label}')"
            f".has('dependency_kind','dynamic')"
            f".has('active', true)"
            f".has('{TIMESTAMP_FIELD}', lt({cutoff}))"
            f".count()")


def _deactivate_query(label: str, cutoff: int) -> str:
    return (f"g.E().hasLabel('{label}')"
            f".has('dependency_kind','dynamic')"
            f".has('active', true)"
            f".has('{TIMESTAMP_FIELD}', lt({cutoff}))"
            f".property('active', false)"
            f".property('deactivated_at', {cutoff})"
            f".iterate()")


def deactivate_stale_dynamic_edges(neptune_query, round_ts: int,
                                   only_labels=None) -> dict:
    """把过期的 dynamic 边置 active=false。

    Args:
        neptune_query: 查询函数（由调用方注入，便于单测替换，也避免本模块
                       在 import 期就依赖 Neptune 凭证）
        round_ts:      本轮的基准时间戳（秒）。由调用方传入而不是各自取
                       time.time() —— 同一轮里所有判定必须用同一个基准，
                       否则同一批边会因执行先后落在不同的 cutoff 上。
        only_labels:   限定边类型，None 表示全部声明了 TTL 的类型。

    Returns:
        {'enabled': bool, 'per_label': {label: {'expires': int, 'stale': int,
                                                'deactivated': int}}}
    """
    enabled = expiry_enabled()
    result = {'enabled': enabled, 'per_label': {}}

    for label, expires in expiring_edge_labels():
        if only_labels and label not in only_labels:
            continue
        cutoff = round_ts - expires
        try:
            resp = neptune_query(_count_query(label, cutoff))
            vals = (resp or {}).get('result', {}).get('data', {}).get('@value', [])
            raw = vals[0] if vals else 0
            stale = raw.get('@value', raw) if isinstance(raw, dict) else raw
        except Exception as e:  # 单个类型失败不该中断整轮
            logger.warning("edge-expiry: 统计 %s 失败（非致命）: %s", label, e)
            continue

        entry = {'expires': expires, 'stale': int(stale or 0), 'deactivated': 0}
        if entry['stale'] and enabled:
            try:
                neptune_query(_deactivate_query(label, cutoff))
                entry['deactivated'] = entry['stale']
                logger.info("edge-expiry: %s 置 active=false %d 条（TTL %ds）",
                            label, entry['stale'], expires)
            except Exception as e:
                logger.warning("edge-expiry: 置 %s 失效失败（非致命）: %s", label, e)
        elif entry['stale']:
            logger.info(
                "edge-expiry[dry-run]: %s 有 %d 条 dynamic 边已超过 TTL %ds 未刷新。"
                "设 GRAPH_EDGE_EXPIRY_ENABLED=true 才会真正置 active=false。",
                label, entry['stale'], expires)
        result['per_label'][label] = entry

    return result
