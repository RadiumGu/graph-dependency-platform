"""
observability.py — 结构化日志 + CloudWatch Metrics 发布

用法：
    from .observability import get_logger, ChaosMetrics
    logger = get_logger("experiment-runner")
    logger.info("phase_started", phase=0, experiment="petsite-pod-kill")

    metrics = ChaosMetrics()
    metrics.publish_experiment_metrics(result)
    metrics.publish_phase_timing("exp-id", "phase0", 3.5)
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

import boto3
import structlog

from .config import REGION

if TYPE_CHECKING:
    from .result import ExperimentResult


def get_logger(agent_name: str) -> structlog.stdlib.BoundLogger:
    """获取结构化 logger，输出 JSON 格式（CloudWatch 友好）"""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    return structlog.get_logger(agent=agent_name)


_cw_logger = get_logger("chaos-metrics")

NAMESPACE = "ChaosEngineering"


class ChaosMetrics:
    """CloudWatch Metrics 发布 — 实验指标 + Phase 耗时"""

    def __init__(self):
        self._cw = boto3.client("cloudwatch", region_name=REGION)

    def _put(self, metric_data: list[dict]):
        try:
            self._cw.put_metric_data(Namespace=NAMESPACE, MetricData=metric_data)
        except Exception as e:
            _cw_logger.warning("cloudwatch_put_failed", error=str(e))

    def publish_experiment_metrics(self, result: "ExperimentResult"):
        """发布实验结果指标到 CloudWatch"""
        dims = [
            {"Name": "Service", "Value": result.experiment.target_service},
            {"Name": "FaultType", "Value": result.experiment.fault.type},
            {"Name": "Status", "Value": result.status},
        ]
        ts = result.end_time or result.start_time
        md = [
            {"MetricName": "ExperimentDuration", "Dimensions": dims, "Value": result.duration_seconds, "Unit": "Seconds", "Timestamp": ts},
            {"MetricName": "RecoveryTime", "Dimensions": dims, "Value": result.recovery_seconds or 0.0, "Unit": "Seconds", "Timestamp": ts},
            {"MetricName": "MinSuccessRate", "Dimensions": dims, "Value": result.min_success_rate, "Unit": "Percent", "Timestamp": ts},
            {"MetricName": "MaxLatencyP99", "Dimensions": dims, "Value": result.max_latency_p99, "Unit": "Milliseconds", "Timestamp": ts},
            {"MetricName": "DegradationRate", "Dimensions": dims, "Value": result.degradation_rate(), "Unit": "Percent", "Timestamp": ts},
            {"MetricName": "ExperimentCount", "Dimensions": dims, "Value": 1, "Unit": "Count", "Timestamp": ts},
            {"MetricName": "ExperimentPassed", "Dimensions": dims, "Value": 1.0 if result.status == "PASSED" else 0.0, "Unit": "Count", "Timestamp": ts},
        ]
        self._put(md)

    def publish_phase_timing(self, experiment_id: str, phase: str, duration_seconds: float):
        """发布单个 Phase 耗时"""
        self._put([{
            "MetricName": "PhaseDuration",
            "Dimensions": [
                {"Name": "ExperimentId", "Value": experiment_id},
                {"Name": "Phase", "Value": phase},
            ],
            "Value": duration_seconds,
            "Unit": "Seconds",
        }])
