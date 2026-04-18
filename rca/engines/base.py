"""engines/base.py — NLQuery / future agent engines 的抽象基类。

规范 see TASK-L1-smart-query.md § 5.

Phase 1 只实现 NLQueryBase；其他 Base（HypothesisBase / LearningBase /
ProberBase / ChaosGuardBase / DRExecutorBase）留 TODO 占位，
Phase 3 再补。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class NLQueryBase(ABC):
    """自然语言图查询引擎基类。

    query() 返回 dict 必须包含：
      - question: str
      - cypher: str
      - results: list
      - summary: str
      - retried: bool        # Wave 4；strands 固定 False
      - engine: str          # "direct" | "strands"
      - model_used: str | None
      - latency_ms: int
      - token_usage: dict | None   # {"input","output","total","cache_read","cache_write"}
                                     # L2 加 cache_* 字段（L1 调用方不依赖，getter 保护）
      - trace: list[dict]    # direct 固定 []；strands 填 tool-call 链
      - error: str | None   # 成功时可缺省或设 None
    """

    def __init__(self, profile: Any = None) -> None:
        self.profile = profile

    @abstractmethod
    def query(self, question: str) -> dict:
        """执行自然语言查询。实现必须填齐上面所有字段。"""


# TODO(phase-3): 以下 Base 为未来 6 个模块预留占位，本 Phase 不实现。
# - class HypothesisBase(ABC): ...
# - class LearningBase(ABC): ...
# - class ProberBase(ABC): ...
# - class ChaosPolicyGuardBase(ABC): ...
# - class ChaosRunnerBase(ABC): ...
# - class DRExecutorBase(ABC): ...
