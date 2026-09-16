"""
graph_feedback.py - 实验结果写回 Neptune 图谱

通过统一 neptune_client.py（SigV4 直连）写回 Gremlin 查询。
启动前执行 connectivity check，不可用时记录警告而非静默失败。
"""
from __future__ import annotations
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .result import ExperimentResult

from .neptune_client import query_gremlin, check_connectivity

logger = logging.getLogger(__name__)

# 需要写回的故障类型（对应 Calls 边）
CALLS_EDGE_FAULT_TYPES = {
    "pod_kill", "pod_failure", "pod_failure",
    "network_delay", "network_loss", "network_corrupt",
    "network_partition", "http_chaos",
}


class GraphFeedback:
    """
    将混沌实验结果写回 Neptune，更新 Calls 边和 Microservice 节点属性
    """

    def write_back(self, result: "ExperimentResult"):
        if result.status not in ("PASSED", "FAILED", "ABORTED"):
            logger.info(f"跳过 Neptune 写回（status={result.status}）")
            return

        # 写回前检查 Neptune 连通性，不可用则提前失败（有明确日志）
        if not check_connectivity():
            raise RuntimeError("Neptune 不可达，图谱反馈跳过（检查 VPC 网络 / IAM 权限）")

        degradation = result.degradation_rate()
        dep_type    = self._classify(degradation)
        last_verified = result.end_time.isoformat() if result.end_time else ""
        exp_id = result.experiment_id

        props = {
            "chaos_dependency_type":       dep_type,
            "chaos_degradation_rate":      round(degradation, 2),
            "chaos_recovery_time_seconds": round(result.recovery_seconds or 0, 1),
            "chaos_last_verified":         last_verified,
            "chaos_verified_by":           exp_id,
        }

        svc = result.experiment.target_service

        # 1. 更新 Calls 边（pod/network/http chaos）
        if result.experiment.fault.type in CALLS_EDGE_FAULT_TYPES:
            self._update_calls_edges(svc, props)

        # 2. 更新 Microservice 节点弹性属性
        self._update_node(svc, result, dep_type)

        # 3. dependency_type=none → 可疑边告警
        if dep_type == "none":
            logger.warning(
                f"⚠️ SUSPICIOUS EDGE: {svc} 的 Calls 入边 "
                f"degradation_rate={degradation:.1f}% → 依赖可能是 ETL 误识别。"
                f"experiment_id={exp_id}。"
                f"注意：本判定基于注入目标自身指标，不足以证伪一条边 —— "
                f"证伪需在被依赖方注入、观测调用方，见 runner/edge_verification.py"
            )

    def _update_calls_edges(self, svc: str, props: dict):
        """更新**指向** svc 的 Calls 边（入边）。

        两处修复（2026-08-30）：

        1. **去掉 `property(single, ...)`**。Neptune 对边属性拒绝基数说明，实测
           返回 `400 UnsupportedOperationException: "Cardinality specification
           may not be used with Edge properties."`。原实现因此 100% 失败，
           异常被下面的 except 吞成 logger.error —— 活图谱实测 21 个 Calls 类
           实验跑完后，19 条 Calls 边上 chaos_* 属性全部为 0，**这条写回路径
           从未成功过一次**。边属性天生单值，直接 property() 即可。

        2. **只写入边，不再写出边**。原查询是
           `where(outV().has(name,svc).or_(inV().has(name,svc)))`，把同一个判定
           写给 svc 的所有出边和入边。在 svc 注入故障只能检验「谁依赖 svc」，
           对「svc 依赖谁」毫无信息 —— 写上去等于凭空伪造验证证据。

        更根本的问题（观测对象错误：degradation_rate 采的是注入目标自己的指标，
        而验证边 A→B 必须在 B 注入、观测 A）不在本方法的修复范围内，
        由 runner/edge_verification.py 提供正确实现。本方法保留的是
        「注入目标周边的粗粒度经验标注」，语义已在属性名上与 verify_* 区分。
        """
        gremlin = f"""
g.E().hasLabel('Calls')
 .where(__.inV().has('name', '{svc}'))
 .property('chaos_dependency_type',       '{props["chaos_dependency_type"]}')
 .property('chaos_degradation_rate',      {props["chaos_degradation_rate"]})
 .property('chaos_recovery_time_seconds', {props["chaos_recovery_time_seconds"]})
 .property('chaos_last_verified',         '{props["chaos_last_verified"]}')
 .property('chaos_verified_by',           '{props["chaos_verified_by"]}')
""".strip()
        try:
            self._run_gremlin(gremlin)
            logger.info(f"✅ Neptune Calls 边已更新: {svc} → {props['chaos_dependency_type']}")
        except Exception as e:
            logger.error(f"Neptune Calls 边更新失败: {e}")

    def _update_node(self, svc: str, result: "ExperimentResult", dep_type: str):
        """更新 Microservice 节点弹性属性"""
        score        = self._calc_resilience_score(result, dep_type)
        last_tested  = result.end_time.isoformat() if result.end_time else ""

        gremlin = f"""
g.V().hasLabel('Microservice').has('name', '{svc}')
 .property(single, 'last_chaos_test',   '{last_tested}')
 .property(single, 'resilience_score',  {score})
 .sideEffect(
     __.coalesce(
         __.values('chaos_test_count').store('x'),
         __.constant(0).store('x')
     )
 )
 .property(single, 'chaos_test_count',
     __.cap('x').unfold().math('_ + 1'))
""".strip()
        try:
            self._run_gremlin(gremlin)
            logger.info(f"✅ Neptune 节点已更新: {svc} resilience_score={score}")
        except Exception as e:
            logger.warning(f"Neptune 节点更新失败（非致命）: {e}")

    def _calc_resilience_score(self, result: "ExperimentResult", dep_type: str) -> int:
        score = max(0, int(100 - result.degradation_rate()))
        if result.recovery_seconds is not None:
            if result.recovery_seconds < 60:
                score = min(100, score + 5)
            elif result.recovery_seconds > 300:
                score = max(0, score - 10)
        if result.status == "ABORTED":
            score = max(0, score - 10)
        return score

    def _classify(self, degradation_rate: float) -> str:
        if degradation_rate >= 80:   return "strong"
        elif degradation_rate >= 20: return "weak"
        else:                        return "none"

    def _run_gremlin(self, query: str):
        """
        执行 Gremlin 查询，通过 neptune_client.py SigV4 直连 Neptune。
        """
        query_gremlin(query)
