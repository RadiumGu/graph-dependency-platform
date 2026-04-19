"""
executor_base.py — DR Executor 抽象基类。

所有 DR 执行引擎必须继承此基类。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from models import DRPlan
    from validation.verification_models import RehearsalReport, VerificationLevel


class ExecutorBase(ABC):
    """DR plan executor base class."""

    ENGINE_NAME: str = "base"

    def __init__(self, dry_run: bool = True) -> None:
        self.dry_run = dry_run
        self.tags: dict = {}

    @abstractmethod
    def execute(self, plan: "DRPlan", level: "VerificationLevel | None" = None) -> "RehearsalReport":
        """Execute a complete DR plan.

        Args:
            plan: Validated DRPlan to execute.
            level: Verification level (DRY_RUN / STEP_BY_STEP / FULL_REHEARSAL).
                   Defaults to DRY_RUN if dry_run=True.

        Returns:
            RehearsalReport with all phase/step results.
        """
