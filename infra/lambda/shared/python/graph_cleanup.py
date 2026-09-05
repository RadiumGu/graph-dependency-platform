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

3. **稀疏观测边：标记陈旧但不失效（本模块，2026-09-05 新增）** ——
   `dependency_kind='inference'` 的边（LLM 在运行时按 query 决定的调用）。
   它们和 `dynamic` 一样是观测得来、不是配置声明的，但**观测是稀疏突发的**，
   所以「窗口内没看到」不足以推断依赖消失。

   实测代价：agent 工具调用形态是同一天 03:25 一批、07:47 一批，中间四小时空白；
   8 个工具里 7 个最后一次调用都在 03:26，09:26 之后就落不进任何 6h 窗口。
   把它们当 `dynamic` 扫，`Retrieves -> nutrition-kb` 就被置了 `active=false` ——
   而那个知识库客观存在、agent 也确实依赖它，只是几小时没人问营养问题。
   **图谱因此给出了一个错误陈述，而不是过期陈述。**

   这违反本项目最核心的不变量：
       零流量与健康在指标上无法区分 → 一律 inconclusive，绝不判 refuted
   所以这类边只写 `drift_status='observed_then_silent'` + `unobserved_seconds`，
   **绝不碰 `active`**。消费方要判断可信度时读 drift_status，
   而不是被一个布尔量误导。

**把三者混在一起会静默删掉真实的架构声明或真实的稀疏依赖**，所以
`deactivate_stale_dynamic_edges` 用 `has('dependency_kind','dynamic')` 显式限定 ——
`static` 与 `inference` 都不在它的作用域内。

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

from graph_contract import EDGE_TYPES, NODE_TYPES, TIMESTAMP_FIELD

logger = logging.getLogger()


def expiry_enabled() -> bool:
    return (os.environ.get('GRAPH_EDGE_EXPIRY_ENABLED') or '').strip().lower() == 'true'


def node_expiry_enabled() -> bool:
    """节点过期收敛的开关，与边的开关**分开** —— 两者风险面不同。

    边置 active=false 只影响依赖查询；节点置 active=false 会影响以该节点为
    端点的一切遍历。分开开关让节点侧可以先跑几轮 dry-run 再启用。
    """
    return (os.environ.get('GRAPH_NODE_EXPIRY_ENABLED') or '').strip().lower() == 'true'


def expiring_node_labels() -> list[tuple[str, int]]:
    """返回 [(节点类型, expires_seconds)]，只含声明了 TTL 的类型。"""
    return sorted((lb, spec['expires_seconds'])
                  for lb, spec in NODE_TYPES.items()
                  if spec.get('expires_seconds'))


def _node_count_query(label: str, cutoff: int) -> str:
    # 刻意**不**要求 has('active', true)：节点侧此前没有任何写入方写 active
    # （实测 581 个 Pod 里 0 个带该属性），要求它会让查询恒为空 —— 与边侧
    # 不同，边的写入方一直在写 active。判据改为「尚未被判过期」。
    return (f"g.V().hasLabel('{label}')"
            f".has('{TIMESTAMP_FIELD}', lt({cutoff}))"
            f".not(__.has('active', false))"
            f".count()")


def _node_unjudgeable_query(label: str) -> str:
    """缺少判据字段、因此**无法判定**的节点数。

    这个查询是本模块最要紧的一条。没有它，一个 TIMESTAMP_FIELD 覆盖率只有
    1.4% 的图谱会让所有 count 返回 0，然后「0 条陈旧」被读成「图谱很干净」——
    与真的干净完全同形。2026-09-04 实测正是这个状态：1077 个节点里只有 15 个
    带 last_seen。所以「不可判定」必须与「已判定为新鲜」分开上报，
    这与指标采集侧的 ok=False 标记是同一条原则。
    """
    return f"g.V().hasLabel('{label}').not(__.has('{TIMESTAMP_FIELD}')).count()"


def _node_deactivate_query(label: str, cutoff: int) -> str:
    return (f"g.V().hasLabel('{label}')"
            f".has('{TIMESTAMP_FIELD}', lt({cutoff}))"
            f".not(__.has('active', false))"
            f".property(single, 'active', false)"
            f".property(single, 'expired_at', {cutoff})"
            f".iterate()")


def expire_stale_nodes(neptune_query, round_ts: int, only_labels=None) -> dict:
    """把过期节点置 active=false（**软过期，不删除**）。

    不删除的理由与边一致：删节点会连带删掉其上所有边，一次误判就不可逆；
    软过期可以先观察几轮，判据错了改回来即可。真正要物理删除的走
    infra/reap_stale_nodes.py，那条路要求「向源端实查一遍清单」这个更强的前提。

    Args:
        neptune_query: 查询函数（调用方注入，便于单测替换）
        round_ts:      本轮基准时间戳（秒），同一轮所有判定共用一个基准
        only_labels:   限定类型，None 表示全部声明了 TTL 的类型

    Returns:
        {'enabled': bool, 'unjudgeable_total': int,
         'per_label': {label: {'expires', 'stale', 'expired', 'unjudgeable'}}}

        `unjudgeable` 不为 0 意味着该类型有节点缺 TIMESTAMP_FIELD，
        本轮对它们**什么都没判**。调用方必须把它当告警而不是 0 风险。
    """
    enabled = node_expiry_enabled()
    result = {'enabled': enabled, 'unjudgeable_total': 0, 'per_label': {}}

    def _num(resp):
        vals = (resp or {}).get('result', {}).get('data', {}).get('@value', [])
        raw = vals[0] if vals else 0
        return int((raw.get('@value', raw) if isinstance(raw, dict) else raw) or 0)

    for label, expires in expiring_node_labels():
        if only_labels and label not in only_labels:
            continue
        cutoff = round_ts - expires
        try:
            stale = _num(neptune_query(_node_count_query(label, cutoff)))
            unjudge = _num(neptune_query(_node_unjudgeable_query(label)))
        except Exception as e:      # 单个类型失败不该中断整轮
            logger.warning("node-expiry: 统计 %s 失败（非致命）: %s", label, e)
            continue

        entry = {'expires': expires, 'stale': stale, 'expired': 0,
                 'unjudgeable': unjudge}
        result['unjudgeable_total'] += unjudge
        if unjudge:
            logger.warning(
                "node-expiry: %s 有 %d 个节点缺 %s，本轮**未对它们做任何判定** "
                "—— 不要把 stale=%d 读成「只有这么多陈旧节点」",
                label, unjudge, TIMESTAMP_FIELD, stale)
        if stale and enabled:
            try:
                neptune_query(_node_deactivate_query(label, cutoff))
                entry['expired'] = stale
                logger.info("node-expiry: %s 置 active=false %d 个（TTL %ds）",
                            label, stale, expires)
            except Exception as e:
                logger.warning("node-expiry: 置 %s 失败（非致命）: %s", label, e)
        elif stale:
            logger.info("node-expiry[dry-run]: %s 有 %d 个节点超过 TTL %ds 未刷新",
                        label, stale, expires)
        result['per_label'][label] = entry

    return result


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


def _mark_query(label: str, cutoff: int, round_ts: int) -> str:
    """把陈旧的 inference 边标成 observed_then_silent，**不碰 active**。

    与 `_deactivate_query` 的唯一区别就是这一点，而这一点是全部要义：
    `active=false` 断言「这条依赖不存在」，而我们能证明的只是「窗口内没观测到」。
    稀疏调用的 agent 工具上，后者推不出前者。
    """
    return (f"g.E().hasLabel('{label}')"
            f".has('dependency_kind','inference')"
            f".has('{TIMESTAMP_FIELD}', lt({cutoff}))"
            f".property('drift_status','observed_then_silent')"
            f".property('unobserved_since',{cutoff})"
            f".property('last_drift_check',{round_ts})"
            f".iterate()")


def _mark_count_query(label: str, cutoff: int) -> str:
    return (f"g.E().hasLabel('{label}')"
            f".has('dependency_kind','inference')"
            f".has('{TIMESTAMP_FIELD}', lt({cutoff}))"
            f".count()")


def _mark_fresh_query(label: str, cutoff: int, round_ts: int) -> str:
    """重新被观测到的 inference 边要把 drift_status 翻回 ok。

    少了这一步，一条边被标 silent 之后即使 agent 又开始调用它，
    图谱也会一直说它 silent —— 那是另一种形式的「判定正确但不可见」。
    """
    return (f"g.E().hasLabel('{label}')"
            f".has('dependency_kind','inference')"
            f".has('drift_status','observed_then_silent')"
            f".has('{TIMESTAMP_FIELD}', gte({cutoff}))"
            f".property('drift_status','ok')"
            f".property('last_drift_check',{round_ts})"
            f".iterate()")


def mark_stale_inference_edges(neptune_query, round_ts: int,
                               only_labels=None) -> dict:
    """把陈旧的 `dependency_kind='inference'` 边标记为观测静默，**不置 active=false**。

    为什么单独一个函数而不是给 deactivate_stale_dynamic_edges 加参数：
    两者的**结论类型不同**。前者输出一个否定断言（依赖不存在了），
    后者输出一个不确定性标记（我这段时间没看到）。把它们塞进同一个函数、
    用一个布尔开关切换，下一个读代码的人会以为只是「软一点的删除」。

    与 dynamic 那条路径共用同一个开关（GRAPH_EDGE_EXPIRY_ENABLED）：
    这里只写诊断属性、不改变边的可用性，风险远低于置 false，但仍受同一开关约束 ——
    「部署代码不等于立刻开始改图」这条姿态对所有写路径一致。

    Returns:
        {'enabled': bool, 'per_label': {label: {'expires': int,
                                                'silent': int, 'marked': int,
                                                'refreshed': int}}}
    """
    enabled = expiry_enabled()
    result = {'enabled': enabled, 'per_label': {}}
    for label, expires in expiring_edge_labels():
        if only_labels and label not in only_labels:
            continue
        cutoff = round_ts - expires
        try:
            resp = neptune_query(_mark_count_query(label, cutoff))
            vals = (resp or {}).get('result', {}).get('data', {}).get('@value', [])
            raw = vals[0] if vals else 0
            silent = raw.get('@value', raw) if isinstance(raw, dict) else raw
        except Exception as e:  # 单个类型失败不该中断整轮
            logger.warning("inference-drift: 统计 %s 失败（非致命）: %s", label, e)
            continue
        entry = {'expires': expires, 'silent': int(silent or 0),
                 'marked': 0, 'refreshed': 0}
        if enabled:
            if entry['silent']:
                try:
                    neptune_query(_mark_query(label, cutoff, round_ts))
                    entry['marked'] = entry['silent']
                except Exception as e:  # noqa: BLE001
                    logger.warning("inference-drift: 标记 %s 失败（非致命）: %s", label, e)
            try:
                neptune_query(_mark_fresh_query(label, cutoff, round_ts))
            except Exception as e:  # noqa: BLE001
                logger.warning("inference-drift: 复位 %s 失败（非致命）: %s", label, e)
        result['per_label'][label] = entry
    return result


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
