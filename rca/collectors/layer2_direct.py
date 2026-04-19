"""
layer2_direct.py — DirectLayer2Prober: 原 aws_probers.py 逻辑封装到 Layer2ProberBase。

纯 boto3 + ThreadPoolExecutor，无 LLM 调用。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from engines.base import Layer2ProberBase

logger = logging.getLogger(__name__)

# 延迟导入 probe registry，保持兼容
_probes_loaded = False
_registry = []


def _ensure_probes():
    global _probes_loaded, _registry
    if _probes_loaded:
        return
    # 导入 aws_probers 以触发 @register_probe 装饰器
    from collectors import aws_probers  # type: ignore  # noqa: F401
    _registry = aws_probers._PROBE_REGISTRY
    _probes_loaded = True


# Probe name → class name 映射
_PROBE_NAME_MAP = {
    "cloudwatch": ("SQSProbe", "DynamoDBProbe"),
    "sqs": ("SQSProbe",),
    "dynamodb": ("DynamoDBProbe",),
    "logs": ("LambdaProbe",),
    "lambda": ("LambdaProbe",),
    "network": ("ALBProbe",),
    "alb": ("ALBProbe",),
    "deployment": ("EC2ASGProbe",),
    "ec2asg": ("EC2ASGProbe",),
    "xray": ("StepFunctionsProbe",),
    "stepfunctions": ("StepFunctionsProbe",),
}


def _probe_result_to_dict(r) -> dict:
    """Convert ProbeResult dataclass to dict."""
    return {
        "service_name": r.service_name,
        "healthy": r.healthy,
        "score_delta": r.score_delta,
        "summary": r.summary,
        "details": r.details if hasattr(r, "details") else {},
        "evidence": r.evidence if hasattr(r, "evidence") else [],
        "engine": "direct",
        "token_usage": None,
        "trace": [],
    }


class DirectLayer2Prober(Layer2ProberBase):
    """Direct Layer2 Prober — 原 aws_probers.py 的 run_all_probes() 封装。"""

    ENGINE_NAME = "direct"

    def __init__(self, profile: Any = None) -> None:
        super().__init__(profile=profile)
        _ensure_probes()

    def run_probes(
        self,
        signal: dict,
        affected_service: str,
        timeout_sec: int = 10,
    ) -> dict:
        t0 = time.time()
        relevant = [p for p in _registry if p.is_relevant(signal, affected_service)]
        logger.info(
            "Layer2 direct probers: running %d/%d probes for service=%s",
            len(relevant), len(_registry), affected_service,
        )

        results = []
        with ThreadPoolExecutor(max_workers=min(len(relevant), 6)) as executor:
            futures = {executor.submit(p.probe, signal, affected_service): p for p in relevant}
            for future in as_completed(futures, timeout=timeout_sec):
                probe = futures[future]
                try:
                    result = future.result()
                    if result is not None:
                        results.append(_probe_result_to_dict(result))
                except Exception as e:
                    logger.warning("Probe %s failed: %s", type(probe).__name__, e)

        elapsed_ms = int((time.time() - t0) * 1000)
        score = self.total_score_delta(results)

        return {
            "probe_results": results,
            "summary": self._make_summary(results),
            "score_delta": score,
            "engine": "direct",
            "model_used": None,
            "latency_ms": elapsed_ms,
            "token_usage": None,
            "trace": [],
            "error": None,
        }

    def run_single_probe(
        self,
        probe_name: str,
        signal: dict,
        affected_service: str,
    ) -> dict:
        t0 = time.time()
        target_classes = _PROBE_NAME_MAP.get(probe_name.lower(), ())
        for p in _registry:
            if type(p).__name__ in target_classes:
                try:
                    result = p.probe(signal, affected_service)
                    if result is not None:
                        d = _probe_result_to_dict(result)
                        d["latency_ms"] = int((time.time() - t0) * 1000)
                        return d
                except Exception as e:
                    return {
                        "service_name": probe_name,
                        "healthy": True,
                        "score_delta": 0,
                        "summary": f"Probe error: {e}",
                        "details": {},
                        "evidence": [],
                        "engine": "direct",
                        "token_usage": None,
                        "trace": [],
                    }
        return {
            "service_name": probe_name,
            "healthy": True,
            "score_delta": 0,
            "summary": f"No probe found for '{probe_name}'",
            "details": {},
            "evidence": [],
            "engine": "direct",
            "token_usage": None,
            "trace": [],
        }

    @staticmethod
    def _make_summary(results: list[dict]) -> str:
        anomalies = [r for r in results if not r.get("healthy", True)]
        if not anomalies:
            return "No anomalies detected across monitored AWS services."
        parts = [f"{r['service_name']}: {r['summary']}" for r in anomalies]
        return f"{len(anomalies)} anomaly/anomalies detected: " + "; ".join(parts)
