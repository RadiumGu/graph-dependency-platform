"""
base.py — PolicyGuardBase: Pre-execution policy guard 抽象基类。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class PolicyGuardBase(ABC):
    """Pre-execution policy guard 基类。"""

    ENGINE_NAME = "base"

    def __init__(self, rules_path: str | None = None) -> None:
        self.rules_path = rules_path

    @abstractmethod
    def evaluate(
        self,
        experiment: dict,
        context: dict | None = None,
    ) -> dict:
        """
        评估实验是否允许执行。

        Returns:
            {
                "decision": "allow" | "deny",
                "reasoning": str,
                "matched_rules": list[str],
                "confidence": float,
                "engine": str,
                "model_used": str | None,
                "latency_ms": int,
                "token_usage": dict | None,
                "error": str | None,
            }
        """
