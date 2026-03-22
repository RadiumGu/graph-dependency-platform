"""
rca.py - RCA Engine Lambda 调用 + 结果验证
Lambda 入参格式（直接 invoke，非 SNS）：
  { "affected_resource": "petsite", "source": "chaos-runner" }
Lambda 返回：
  {
    "severity": "P1",
    "rca": {
      "top_candidate": {"service": "petsite", "confidence": 0.85, ...},
      "root_cause_candidates": [...],
      ...
    }
  }
"""
from __future__ import annotations
import json
import logging
import boto3
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

from .config import REGION

LAMBDA_NAME = "petsite-rca-engine"


@dataclass
class RCAResult:
    root_cause: str = ""
    confidence: float = 0.0
    evidence: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    status: str = "not_triggered"   # not_triggered / error / success
    error_message: str = ""


class RCATrigger:
    """
    调用 petsite-rca-engine Lambda，验证根因定位准确性
    """

    def __init__(self):
        self._client = None

    @property
    def client(self):
        if self._client is None:
            self._client = boto3.client("lambda", region_name=REGION)
        return self._client

    def trigger(self, service: str, fault_type: str, start_time: str) -> RCAResult:
        """
        触发 RCA 分析
        :param service: 目标服务名，e.g. "petsite"
        :param fault_type: 故障类型，e.g. "pod_kill"
        :param start_time: 注入时间 ISO8601
        """
        payload = {
            "affected_resource": service,
            "source": "chaos-runner",
            "fault_type": fault_type,
            "fault_start_time": start_time,
        }
        logger.info(f"触发 RCA: {json.dumps(payload)}")
        try:
            resp = self.client.invoke(
                FunctionName=LAMBDA_NAME,
                InvocationType="RequestResponse",
                Payload=json.dumps(payload).encode(),
            )
            body = json.loads(resp["Payload"].read())
            logger.info(f"RCA 响应: {json.dumps(body, ensure_ascii=False)[:300]}")

            # 检查 Lambda 级错误（FunctionError）
            if "FunctionError" in resp:
                error_msg = body.get("errorMessage", str(body)[:200])
                logger.error(f"RCA Lambda 执行错误: {error_msg}")
                return RCAResult(
                    status="error",
                    error_message=f"Lambda FunctionError: {error_msg}",
                    raw=body,
                )

            result = self._parse(body)
            if result.root_cause:
                result.status = "success"
            else:
                result.status = "error"
                result.error_message = "Lambda 返回成功但 root_cause 为空"
            return result

        except Exception as e:
            logger.error(f"RCA Lambda 调用失败: {e}")
            return RCAResult(
                status="error",
                error_message=str(e),
            )

    def _parse(self, body: dict) -> RCAResult:
        # Lambda Proxy 响应：{statusCode: 200, body: "...json..."} 需要先解嵌套
        if "statusCode" in body and "body" in body:
            inner = body["body"]
            if isinstance(inner, str):
                import json as _json
                try:
                    body = _json.loads(inner)
                except (ValueError, TypeError):
                    pass
            elif isinstance(inner, dict):
                body = inner

        # 兼容两种 RCA 返回格式
        rca = body.get("rca") or body

        # 优先用 top_candidate（旧格式），fallback 到 root_cause_candidates[0]（新格式）
        top = rca.get("top_candidate")
        if not top:
            candidates = rca.get("root_cause_candidates", [])
            top = candidates[0] if candidates else {}

        root_cause  = (top.get("service") or top.get("root_cause", "")).strip()
        confidence  = float(top.get("confidence", 0.0))
        evidence    = top.get("evidence", [])
        return RCAResult(
            root_cause=root_cause,
            confidence=confidence,
            evidence=evidence,
            raw=rca,
        )

    def verify(self, result: RCAResult, expected: str) -> bool:
        """模糊匹配：expected 是 actual 的子串，或 actual 是 expected 的子串"""
        if not result.root_cause or not expected:
            return False
        return (
            expected.lower() in result.root_cause.lower()
            or result.root_cause.lower() in expected.lower()
        )
