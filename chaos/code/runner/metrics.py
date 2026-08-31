"""
metrics.py - DeepFlow 指标采集（ClickHouse 8123 直连）

表：flow_log.l7_flow_log
  response_status: 0=正常, >=1=异常（1=异常,2=不存在,3=服务端异常,4=客户端异常）
  response_duration: 微秒
  request_domain: 格式 svc-name.namespace.svc.cluster.local
"""
from __future__ import annotations
import time
import logging
import requests
from dataclasses import dataclass

from .experiment import MetricsSnapshot
from .config import DEEPFLOW_CH_HOST, DEEPFLOW_CH_PORT

logger = logging.getLogger(__name__)


def _ch_query(sql: str) -> dict:
    """执行 ClickHouse 查询，返回 FORMAT JSON 结果"""
    try:
        r = requests.post(
            f"http://{DEEPFLOW_CH_HOST}:{DEEPFLOW_CH_PORT}/",
            data=(sql + " FORMAT JSON").encode(),
            headers={"Content-Type": "text/plain"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.error(f"ClickHouse 查询失败: {e}")
        return {}


class DeepFlowMetrics:
    """
    查询 DeepFlow ClickHouse，获取服务实时 SLI 指标
    通过 request_domain 字段匹配服务（含 K8s DNS 格式）

    ⚠️ SLI 必须只统计应用层协议，**不能把 DNS 混进来**（2026-08-31 实测缺陷）。

    原实现只按 `request_domain LIKE '%svc%'` 过滤，于是同一个服务名的 **DNS 查询**
    也被计入成功率。而 K8s 默认 ndots=5 会把 `svc.ns.svc.cluster.local` 逐个
    拼上搜索域再查一遍，产生大量 NXDOMAIN（`response_status=4` / `response_code=3`），
    这些**预期内的**失败被当成服务故障：

        list-adoptions   混合口径 31.37%  ->  仅 HTTP 100.00%
        search-service   混合口径 69.16%  ->  仅 HTTP 100.00%
        petsite          混合口径 100%    ->  仅 HTTP 100.00%
        （5 分钟窗口：HTTP 20,645 条全部成功；DNS 16,856 条里 13,516 条 NXDOMAIN）

    后果有两层，第二层更要紧：
      1. 稳态检查 `success_rate >= 95%` 永远过不了，实验在 preflight 就失败；
      2. 噪声底盘既大又随 DNS 行为波动，据此算出的退化率不可信 ——
         一次真实的 20pp HTTP 退化可能被 DNS 抖动淹没或伪造出来。
         这直接损害边验证的判定（north_star §4 不变量 7 的同类错误：
         「坏掉」和「正常」在指标上分不开）。

    这属于本项目反复出现的「粒度/口径错配」缺陷类别，不是采集覆盖不足。
    """

    # 应用层协议白名单。用 l7_protocol_str 而不是硬编码数字枚举
    # （实测本环境 HTTP=20 / DNS=120，但数字枚举是 DeepFlow 内部实现，会变）。
    APP_PROTOCOLS = ("HTTP", "HTTP1", "HTTP2", "gRPC")

    @classmethod
    def _proto_filter(cls) -> str:
        quoted = ", ".join(f"'{p}'" for p in cls.APP_PROTOCOLS)
        return f"AND l7_protocol_str IN ({quoted})"

    def collect(
        self,
        service: str,
        namespace: str = "default",
        window_seconds: int = 60,
    ) -> MetricsSnapshot:
        """
        查询指定服务近 window_seconds 秒的指标
        服务名匹配 request_domain LIKE '%{service}%'

        只统计应用层协议（见类文档）——把 DNS 混进来会让 ndots 搜索域展开
        产生的预期 NXDOMAIN 被当成服务故障，实测能把 100% 的服务报成 31%。
        """
        sql = f"""
SELECT
    countIf(response_status = 0) AS success_cnt,
    count() AS total_cnt,
    quantile(0.99)(response_duration) / 1000.0 AS p99_latency_ms
FROM flow_log.l7_flow_log
WHERE start_time > now() - INTERVAL {window_seconds} SECOND
  AND response_duration > 0
  {self._proto_filter()}
  AND request_domain LIKE '%{service}%'
"""
        ts = int(time.time())
        try:
            data = _ch_query(sql)
            rows = data.get("data", [])
            if rows:
                row = rows[0]
                total   = int(row.get("total_cnt", 0) or 0)
                success = int(row.get("success_cnt", 0) or 0)
                p99     = float(row.get("p99_latency_ms", 0) or 0)
                success_rate = round(success / total * 100, 2) if total > 0 else 100.0
                return MetricsSnapshot(
                    timestamp=ts,
                    success_rate=success_rate,
                    latency_p99_ms=round(p99, 1),
                    total_requests=total,
                    # total==0 时这一行是「查询成功但窗口内没有任何应用层请求」。
                    # 它与「查询失败」不同（后者走下面的 fallback，ok=False），
                    # 但同样不能当成可信的谷值 —— 交给调用方按 ok + total 判断。
                    ok=True,
                )
        except Exception as e:
            logger.warning(f"metrics.collect({service}) 失败: {e}")

        # fallback：**采集失败**。返回 success_rate=100 是为了不误触 guardrail，
        # 但必须打上 ok=False —— 否则这个 (100%, 0 requests) 会被下游当成
        # 「服务健康但流量归零」的真实观测，把一次查询抖动变成「吞吐塌陷 100%」，
        # 进而把一条边误判成 confirmed（2026-08-31 实测）。
        return MetricsSnapshot(timestamp=ts, success_rate=100.0, latency_p99_ms=0.0,
                               total_requests=0, ok=False)

    def collect_steady(
        self,
        service: str,
        namespace: str = "default",
        window_seconds: int = 60,
        samples: int = 3,
        interval: int = 10,
    ) -> MetricsSnapshot:
        """
        采集多次取平均，用于稳态基线（Phase 1）和恢复验证（Phase 5）
        """
        snapshots = []
        for i in range(samples):
            snap = self.collect(service, namespace, window_seconds)
            snapshots.append(snap)
            if i < samples - 1:
                time.sleep(interval)

        ts = int(time.time())
        # 只用采集成功的采样点算均值。全部失败时 ok=False 传下去 ——
        # 基线本身不可信的话，任何以它为分母的退化率都是编的。
        good = [s for s in snapshots if getattr(s, 'ok', True)] or snapshots
        avg_sr  = round(sum(s.success_rate for s in good) / len(good), 2)
        avg_p99 = round(sum(s.latency_p99_ms for s in good) / len(good), 1)
        total   = good[-1].total_requests
        logger.info(f"稳态快照 {service}: success_rate={avg_sr}%, p99={avg_p99}ms (n={samples})")
        return MetricsSnapshot(timestamp=ts, success_rate=avg_sr, latency_p99_ms=avg_p99,
                               total_requests=total,
                               ok=any(getattr(s, 'ok', True) for s in snapshots))
