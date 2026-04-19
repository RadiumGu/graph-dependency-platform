"""
base.py — RunnerBase: 5-Phase 混沌实验执行引擎抽象基类。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from runner.experiment import Experiment
    from runner.result import ExperimentResult


PROTECTED_NAMESPACES = frozenset({
    "default", "kube-system", "kube-public", "kube-node-lease",
})


class RunnerBase(ABC):
    """5-Phase 混沌实验执行引擎基类。"""

    ENGINE_NAME = "base"

    def __init__(self, dry_run: bool = True, tags: dict | None = None) -> None:
        self.dry_run = dry_run
        self.tags = tags or {}

    @abstractmethod
    def run(self, experiment: "Experiment") -> "ExperimentResult":
        """执行完整的 5-phase 实验。"""

    def _validate_namespace(self, namespace: str) -> None:
        """Protected namespace 双重检查（PolicyGuard R002 之外的代码级保护）。"""
        from runner.runner import PrefightFailure
        if namespace in PROTECTED_NAMESPACES:
            raise PrefightFailure(
                f"禁止在受保护 namespace 执行实验: {namespace}"
            )
