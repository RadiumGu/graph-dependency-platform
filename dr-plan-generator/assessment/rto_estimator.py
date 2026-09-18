"""
assessment/rto_estimator.py — RTO 估算

设计原则：**设计值只是起点，实测值才是依据。**

金融监管要的是实测 RTO，不是设计 RTO。审计师会追问「这个数字怎么来的」，
一张硬编码查表答不上来。所以这里分三层：

1. **分策略的基线查表**。原实现只有一张 ``DEFAULT_TIMES``，对 pilot light 与
   warm standby 用同一组数字——而两者差着「节点冷启动 + 首次镜像拉取」这一整段，
   pilot light 会被系统性低估。
2. **演练实测值覆盖基线**。``plans/measurements.json`` 里有该 action 的历史实测
   耗时时，取滚动均值而非查表值。跑过几次演练之后，估算就不再是猜。
3. **估算依据可追溯**。``estimate_with_basis()`` 同时返回每一项用的是实测还是
   查表，供报告与审计留痕。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

#: warm standby：恢复区已有容量且服务在跑，只需扩副本。
#: AWS 定义："everything is already deployed and running … only requires you to scale up"。
WARM_STANDBY_TIMES: Dict[str, int] = {
    "RDSCluster": 300,       # Aurora 跨区提升
    "RDSInstance": 600,
    "DynamoDBTable": 30,     # Global Table 只需校验副本
    "S3Bucket": 60,          # 校验复制追平
    "SQSQueue": 30,
    "SNSTopic": 30,
    "Microservice": 120,     # 扩副本 + rollout（镜像已在节点上缓存）
    "LambdaFunction": 30,
    "LoadBalancer": 180,
    "K8sService": 60,
    "EC2Instance": 180,
    "EKSCluster": 300,
    "EKSNodeGroup": 60,
    "Pod": 60,
}

#: pilot light：计算层常态关闭，恢复要先造容量。
#: 与 warm standby 的差额集中在容器与节点上：
#:   · EKSNodeGroup —— 扩容 + 等节点 Ready，实测 EC2 节点加入 180–300s
#:   · Microservice —— 目标区节点是全新的，本地无镜像缓存，
#:     arm64 镜像首次拉取实测 60–120s，叠加 rollout
#: 数据层与流量层不受策略影响，故沿用同值。
PILOT_LIGHT_TIMES: Dict[str, int] = {
    **WARM_STANDBY_TIMES,
    "Microservice": 300,
    "EKSNodeGroup": 360,
    "EC2Instance": 300,
    "Pod": 180,
}

#: 未知资源类型的兜底。
FALLBACK_STEP_SECONDS = 60

#: 阶段之间的 gate/确认开销（秒）。
INTER_PHASE_GATE_SECONDS = 60

#: 实测值文件名（位于 PLANS_DIR 下）。
MEASUREMENTS_FILENAME = "measurements.json"


def load_measurements(path: Optional[str] = None) -> Dict[str, List[float]]:
    """读取历史实测耗时。

    Args:
        path: measurements.json 路径；None 时按 ``config.PLANS_DIR`` 推导。

    Returns:
        ``{action: [seconds, …]}``；文件不存在或损坏时返回空 dict
        （估算退回查表，而不是让整个生成失败）。
    """
    if path is None:
        try:
            import config

            path = os.path.join(config.PLANS_DIR, MEASUREMENTS_FILENAME)
        except Exception:  # noqa: BLE001
            return {}
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Cannot read measurements from %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, List[float]] = {}
    for action, samples in data.items():
        if isinstance(samples, list):
            numeric = [float(s) for s in samples if isinstance(s, (int, float))]
            if numeric:
                out[str(action)] = numeric
    return out


def record_measurement(
    action: str, seconds: float, path: Optional[str] = None, keep: int = 10
) -> None:
    """把一次实测耗时追加到 measurements.json（保留最近 ``keep`` 次）。

    只保留最近若干次是刻意的：环境会变（机型、镜像大小、副本数），
    很久以前的样本会把均值拖偏。

    Args:
        action: 步骤 action 名。
        seconds: 实测耗时。
        path: 文件路径；None 时按 ``config.PLANS_DIR`` 推导。
        keep: 每个 action 保留的样本数。
    """
    if path is None:
        try:
            import config

            os.makedirs(config.PLANS_DIR, exist_ok=True)
            path = os.path.join(config.PLANS_DIR, MEASUREMENTS_FILENAME)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Cannot resolve measurements path: %s", exc)
            return
    data = load_measurements(path)
    samples = data.setdefault(action, [])
    samples.append(float(seconds))
    data[action] = samples[-keep:]
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
    except OSError as exc:
        logger.warning("Cannot write measurements to %s: %s", path, exc)


class RTOEstimator:
    """按策略与历史实测值估算 RTO。

    Args:
        strategy: ``pilot_light`` / ``warm_standby``。
        measurements: ``{action: [seconds, …]}``；None 时自动加载。
    """

    #: 保留旧名以兼容既有调用；等同 warm standby 基线。
    DEFAULT_TIMES: Dict[str, int] = WARM_STANDBY_TIMES

    def __init__(
        self,
        strategy: str = "warm_standby",
        measurements: Optional[Dict[str, List[float]]] = None,
    ) -> None:
        self.strategy = strategy
        self.table = (
            PILOT_LIGHT_TIMES if strategy == "pilot_light" else WARM_STANDBY_TIMES
        )
        self.measurements = (
            measurements if measurements is not None else load_measurements()
        )

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def estimate(self, phases: List[Any]) -> int:
        """估算总 RTO（分钟）。

        Args:
            phases: DRPhase 列表。

        Returns:
            RTO 分钟数（最小 1）。
        """
        minutes, _ = self.estimate_with_basis(phases)
        return minutes

    def estimate_with_basis(self, phases: List[Any]) -> Tuple[int, Dict[str, Any]]:
        """估算 RTO，并返回**依据**。

        依据是审计要的东西：哪些步骤用了实测值、样本数多少、哪些还只是查表。

        Args:
            phases: DRPhase 列表。

        Returns:
            ``(minutes, basis)``。``basis`` 含 ``measured_actions`` /
            ``estimated_actions`` / ``sample_counts`` / ``confidence``。
        """
        total_seconds = 0
        measured: Dict[str, int] = {}
        estimated: List[str] = []

        for phase in phases:
            phase_seconds, phase_measured, phase_estimated = self._estimate_phase(phase)
            total_seconds += phase_seconds + INTER_PHASE_GATE_SECONDS
            measured.update(phase_measured)
            estimated.extend(phase_estimated)

        total_steps = len(measured) + len(set(estimated))
        confidence = (
            "measured" if total_steps and not estimated
            else "partial" if measured
            else "design_values_only"
        )
        basis = {
            "strategy": self.strategy,
            "measured_actions": sorted(measured),
            "sample_counts": measured,
            "estimated_actions": sorted(set(estimated)),
            "confidence": confidence,
            "note": (
                "confidence=design_values_only 表示全部来自查表，尚无演练实测数据。"
                "监管场景需要实测 RTO —— 跑过演练并回写 measurements.json 后此值会改变。"
            ),
        }
        return max(1, total_seconds // 60), basis

    def estimate_from_subgraph(self, subgraph: Dict[str, Any]) -> int:
        """无阶段结构时的粗估（全部串行，保守上界）。

        Args:
            subgraph: 含 ``nodes`` 的 dict。

        Returns:
            RTO 分钟数。
        """
        total = sum(
            self.table.get(n.get("type", ""), FALLBACK_STEP_SECONDS)
            for n in subgraph.get("nodes", [])
        )
        return max(1, total // 60)

    def step_seconds(self, step: Any) -> Tuple[int, bool]:
        """返回某步骤的用时与「是否来自实测」。

        实测优先于步骤自带的估值，步骤估值优先于类型查表。

        Args:
            step: DRStep。

        Returns:
            ``(seconds, is_measured)``。
        """
        action = getattr(step, "action", "") or ""
        samples = self.measurements.get(action)
        if samples:
            return int(sum(samples) / len(samples)), True
        declared = getattr(step, "estimated_time", 0) or 0
        if declared:
            return int(declared), False
        rtype = getattr(step, "resource_type", "") or ""
        return self.table.get(rtype, FALLBACK_STEP_SECONDS), False

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _estimate_phase(self, phase: Any) -> Tuple[int, Dict[str, int], List[str]]:
        """计算单阶段用时，并区分实测与估算来源。

        同一 ``parallel_group`` 内只计最长的一步；无分组的步骤串行累加。

        Args:
            phase: DRPhase。

        Returns:
            ``(seconds, {action: sample_count}, [estimated_action, …])``。
        """
        groups: Dict[str, List[int]] = {}
        serial_time = 0
        measured: Dict[str, int] = {}
        estimated: List[str] = []

        for step in phase.steps:
            seconds, is_measured = self.step_seconds(step)
            action = getattr(step, "action", "") or ""
            if is_measured:
                measured[action] = len(self.measurements.get(action, []))
            else:
                estimated.append(action)

            if step.parallel_group:
                groups.setdefault(step.parallel_group, []).append(seconds)
            else:
                serial_time += seconds

        parallel_time = sum(max(times) for times in groups.values())
        return serial_time + parallel_time, measured, estimated
