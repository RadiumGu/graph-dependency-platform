"""chaos/code/runner/xray_metrics.py — 用 X-Ray 服务图测「注入生效性」

## 这个模块解锁了什么

判定链要判 `refuted`（证伪）之前必须先证明「我真的打断了它」——
这道门禁在 `infra/lambda/shared/python/graph_confidence.py`（2026-08-31 加入）：

    if injection_confirmed is not True:
        return (STATUS_INCONCLUSIVE, '...证伪需要先证明打断确实发生...')

而 `injection_confirmed` 的来源 `edge_took_effect`（`runner.py:698-719`）
**只从 DeepFlow 的边级流量算出来**。DeepFlow 抓的是集群内 Pod 的 L7 流量，
所以凡是**源不在集群内**的边，它必然返回 None。

2026-09-09 实测这个盲区有多大 —— 78 条「工具能打但没跑」的边里：

    源在集群内（DeepFlow 能测）: 37 条
    源不在集群内（必然 None）  : 41 条
        LambdaFunction -> ...   32 条
        StepFunction   -> ...    8 条
        SNSTopic       -> ...    1 条

那 41 条**跑了注入也只会得到 inconclusive** —— 付出注入的代价，
覆盖率一分不涨。X-Ray 服务图看得见 Lambda 与 Step Functions，
所以它正好补上这块。

## 为什么用 GetServiceGraph 而不是 GetTraceSummaries

服务图**本身就是按边聚合**的：每个 Service 的 `Edges[]` 里带
`SummaryStatistics{TotalCount, OkCount, ErrorStatistics, FaultStatistics}`，
一次调用就拿到一条边的调用数与成功率。用 GetTraceSummaries 则要拉回原始
trace 再自己按 span 聚合 —— 数据量大一个量级，且要自己处理采样率。

## 与 DeepFlow 路径的一个**语义差异**（刻意的）

DeepFlow 那条 ClickHouse 查询只能写 `start_time > now() - INTERVAL n SECOND`，
所以基线窗（900s）与注入窗（180s）是**嵌套**的 —— 基线里含了注入期，
这会稀释掉一部分退化信号。

X-Ray 接受显式 StartTime/EndTime，所以这里做成**不重叠**：

    基线窗 = [now - baseline_seconds - injection_seconds, now - injection_seconds]
    注入窗 = [now - injection_seconds, now]

这更正确。没有顺手去改 DeepFlow 那条是因为它要改查询形状（加下界），
属于另一件事，不在本模块范围内。

## 一个必须说清的口径退让

`MetricsSnapshot.latency_p99_ms` 在这里装的是**均值**，不是 p99 ——
X-Ray 的 `SummaryStatistics` 只给 `TotalResponseTime`（总耗时，秒），
拿不到分位数。之所以可以接受：生效性判定只看成功率退化与调用数变化
（`runner.py` 里 `drop >= 5.0 or thin > 0`），不看延迟。
但**不要**把这个字段拿去做延迟判据 —— 会把均值当分位数用。
字段名保持一致是为了与 DeepFlow 的 MetricsSnapshot 同形可替换。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

try:                                              # 与 runner 内其他模块同样的双导入
    from .experiment import MetricsSnapshot
except ImportError:                               # pragma: no cover
    from experiment import MetricsSnapshot        # type: ignore

REGION = os.environ.get('REGION') or os.environ.get('AWS_REGION') \
    or 'ap-northeast-1'

#: `GetServiceGraph` 的**硬上限是 6 小时**，超了直接
#: `InvalidRequestException: Time range cannot be longer than 6 hours`。
#: 2026-09-09 实测踩到：传 24h 窗口时每条边都静默拿到 None ——
#: 而 None 在本模块的语义是「测不出生效性」，于是一个纯粹的参数越界
#: 会被读成「X-Ray 看不到这条边」。这正是必须分段而不是让它报错的原因。
#: `infra/lambda/etl_xray/neptune_etl_xray.py` 早就这么做了（XRAY_MAX_WINDOW_SECONDS），
#: 这里不另立一套。
XRAY_MAX_WINDOW_SECONDS = 6 * 3600


def _segments(start_ts: int, end_ts: int) -> list[tuple[int, int]]:
    """把任意长度的窗口切成 <= 6h 的片段（从后往前切）。"""
    out: list[tuple[int, int]] = []
    end = int(end_ts)
    remaining = max(0, int(end_ts) - int(start_ts))
    if remaining == 0:
        return [(int(start_ts), int(end_ts))]
    while remaining > 0:
        span = min(remaining, XRAY_MAX_WINDOW_SECONDS)
        out.append((end - span, end))
        end -= span
        remaining -= span
    return out


#: X-Ray 服务图里节点名与图谱节点名的对不齐是常态：
#: Lambda 在服务图里是函数名，Step Functions 是状态机名，
#: 而 AWS 托管服务是粗粒度服务名（'DynamoDB'、'S3'）。
#: 所以匹配用**双向子串**而不是相等 —— 与 etl_xray 的既有做法一致。
def _name_matches(graph_name: str, xray_name: str) -> bool:
    if not graph_name or not xray_name:
        return False
    a, b = graph_name.lower(), xray_name.lower()
    return a == b or a in b or b in a


class XRayEdgeMetrics:
    """按 X-Ray 服务图取一条边的调用统计。

    与 `metrics.DeepFlowMetrics.collect_edge_flow` 同形（都返回
    `MetricsSnapshot`），所以 runner 可以在 DeepFlow 返回 ok=False 时
    直接换用它，无需改判定链。
    """

    def __init__(self, client: Any = None) -> None:
        self._client = client

    def _xray(self):
        if self._client is None:
            import boto3
            self._client = boto3.client('xray', region_name=REGION)
        return self._client

    def _edge_stats(self, src: str, dst: str,
                    start_ts: int, end_ts: int) -> tuple[int, int, float] | None:
        """返回 (total_count, ok_count, total_response_time_seconds) 或 None。

        None 的含义是**这条边在这个窗口里没有出现在服务图里**，
        与「出现了但调用数为 0」不同 —— 后者会返回 (0, 0, 0.0)。
        这个区分直接决定 ok 标志，不能合并。

        ## 为什么必须扫**所有**同名节点再聚合（2026-09-09 实测修正）

        X-Ray 把一个 Lambda 表示成**两个同名节点**，真实出边挂在后者上：

            neptune-etl-trigger (AWS::Lambda)           -> neptune-etl-trigger  [Function]
            neptune-etl-trigger (AWS::Lambda::Function) -> neptune-etl-from-aws [AWS::Lambda]

        第一版实现遇到第一个同名节点就 return，于是挑中 `AWS::Lambda` 那个、
        在它的 Edges 里找不到目标、返回 None —— 一条**明明测得出**的边
        被误判成「测不出生效性」，而这正是本模块要消灭的那种失败。
        """
        try:
            services: list[dict] = []
            paginator = self._xray().get_paginator('get_service_graph')
            for seg_start, seg_end in _segments(start_ts, end_ts):
                for page in paginator.paginate(StartTime=seg_start,
                                               EndTime=seg_end):
                    services.extend(page.get('Services', []) or [])
        except Exception as exc:                              # noqa: BLE001
            logger.warning('X-Ray GetServiceGraph [%s,%s] 失败: %s',
                           start_ts, end_ts, exc)
            return None

        # ReferenceId -> Name。边只带 ReferenceId，必须先建索引才能解析目标名。
        by_ref = {s.get('ReferenceId'): s for s in services}
        same_edge = (src or '').strip().lower() == (dst or '').strip().lower()

        total = ok_n = 0
        rt = 0.0
        found = False

        for svc in services:
            # Type='client' 是 X-Ray 给调用方补的影子节点，不是真实被调对象。
            # 不跳过会让「源」匹配到一个同名影子，其 Edges 与真实服务不同。
            if svc.get('Type') == 'client':
                continue
            if not _name_matches(src, svc.get('Name') or ''):
                continue
            for edge in svc.get('Edges', []) or []:
                peer = by_ref.get(edge.get('ReferenceId')) or {}
                peer_name = peer.get('Name') or ''
                if not _name_matches(dst, peer_name):
                    continue
                # `AWS::Lambda -> AWS::Lambda::Function` 同名那条是 X-Ray 表示
                # 「调用进入函数执行」的内部结构，不是一条依赖。
                # 只在**要找的不是自环**时排除它 —— 真自环
                # （self-loop-from-trace）另有处理，不该在这里被吞掉。
                if not same_edge and _name_matches(src, peer_name):
                    continue
                st = edge.get('SummaryStatistics') or {}
                total += int(st.get('TotalCount') or 0)
                ok_n += int(st.get('OkCount') or 0)
                rt += float(st.get('TotalResponseTime') or 0.0)
                found = True

        return (total, ok_n, rt) if found else None

    def collect_edge_flow(
        self,
        client_service: str,
        server_service: str,
        window_seconds: int = 180,
        end_ts: int | None = None,
    ) -> MetricsSnapshot:
        """取 `client_service -> server_service` 在窗口内的调用统计。

        与 DeepFlow 同一约定：**采集不到必须 ok=False**。
        返回 (100%, 0 requests, ok=True) 会被下游读成
        「路径健康但零流量」的真实观测，那是一个不同的结论。
        """
        now = int(end_ts or time.time())
        start = now - int(window_seconds)
        stats = self._edge_stats(client_service, server_service, start, now)
        if stats is None:
            return MetricsSnapshot(timestamp=now, success_rate=100.0,
                                   latency_p99_ms=0.0, total_requests=0, ok=False)
        total, ok_n, rt = stats
        rate = round(ok_n / total * 100, 2) if total > 0 else 100.0
        # 注意：这里装的是**均值**（见模块 docstring 的口径退让一节）
        mean_ms = round(rt / total * 1000.0, 1) if total > 0 else 0.0
        return MetricsSnapshot(timestamp=now, success_rate=rate,
                               latency_p99_ms=mean_ms, total_requests=total,
                               ok=True)

    def took_effect(
        self,
        client_service: str,
        server_service: str,
        injection_seconds: int = 180,
        baseline_seconds: int = 900,
        drop_threshold_pct: float = 5.0,
        end_ts: int | None = None,
    ) -> tuple[bool | None, str]:
        """回答「注入真的作用到这条边了吗」。

        返回 (生效性, 人话理由)。生效性三态，**None 不是 False**：
            True  —— 观测到这条边退化了，打断确实发生
            False —— 这条边照常工作，注入没作用到它
            None  —— 测不出来（窗口内查不到这条边），什么都没证明

        门槛与 DeepFlow 路径保持一致（5%）：这里回答的是「有没有作用到链路」
        这个是非问题，不是「影响有多大」。用 confirm 那条 20% 的线
        会把「生效但影响小」误判成「没生效」，反而放宽 refuted。
        """
        now = int(end_ts or time.time())
        inj_start = now - injection_seconds
        # 不重叠：基线窗在注入窗**之前**结束（见模块 docstring）
        base = self._edge_stats(client_service, server_service,
                                inj_start - baseline_seconds, inj_start)
        inj = self._edge_stats(client_service, server_service, inj_start, now)

        if base is None:
            return None, (f'X-Ray 在基线窗内看不到 {client_service} -> '
                          f'{server_service} —— 无基线可比，不判生效性')
        b_total, b_ok, _ = base
        if b_total <= 0:
            return None, (f'基线窗内这条边 0 次调用 —— 无从打断，'
                          f'任何注入期数字都是噪声')
        if inj is None:
            # 基线有流量、注入期这条边从服务图里**消失**了 —— 这是最强的生效证据：
            # 不是变慢或报错，而是调用根本没发生。
            return True, (f'基线 {b_total} 次调用，注入期这条边从 X-Ray 服务图中'
                          f'消失 —— 调用未发生，打断确认生效')
        i_total, i_ok, _ = inj
        b_rate = b_ok / b_total * 100.0
        i_rate = (i_ok / i_total * 100.0) if i_total > 0 else 0.0
        drop = b_rate - i_rate
        thin = b_total - i_total

        if drop >= drop_threshold_pct:
            return True, (f'成功率 {b_rate:.1f}% -> {i_rate:.1f}%'
                          f'（退化 {drop:.1f}% >= {drop_threshold_pct}%），'
                          f'打断确认生效')
        if i_total <= 0:
            return True, (f'基线 {b_total} 次调用、注入期 0 次 —— '
                          f'调用停止，打断确认生效')
        if thin > 0:
            return True, (f'调用数 {b_total} -> {i_total}（减少 {thin} 次），'
                          f'打断确认生效')
        return False, (f'成功率 {b_rate:.1f}% -> {i_rate:.1f}%、'
                       f'调用数 {b_total} -> {i_total} —— 这条边照常工作，'
                       f'注入未作用到它')
