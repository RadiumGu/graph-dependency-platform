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


class HypothesisBase(ABC):
    """Chaos 混沌工程假设生成引擎基类（Phase 3 Module 1）。

    *核心契约*：子类实现 generate_with_meta() / prioritize_with_meta()，返回 dict：
      - hypotheses: list  # 业务产出，generate 有值；prioritize 可为空
      - prioritized: list # 有排序的版本，generate 为空；prioritize 有值
      - engine: str       # "direct" | "strands"
      - model_used: str | None
      - latency_ms: int
      - token_usage: dict | None  # {input,output,total,cache_read,cache_write}
      - trace: list[dict]         # Strands tool-call 链；direct 为 []
      - error: str | None

    *向后兼容*：提供默认 generate()/prioritize() 包装 _with_meta()，返回 list
    （现版调用方 orchestrator / learning_agent / main 无改动）。
    """

    ENGINE_NAME = "base"

    def __init__(self, profile: Any = None) -> None:
        self.profile = profile

    @abstractmethod
    def generate_with_meta(
        self,
        max_hypotheses: int = 50,
        service_filter: str | None = None,
    ) -> dict:
        """生成假设，返回带元数据的 dict。子类必填全部关键字段。"""

    @abstractmethod
    def prioritize_with_meta(self, hypotheses: list) -> dict:
        """排序假设，返回带元数据的 dict。子类必填全部关键字段。"""

    # —— 向后兼容默认实现（调用 元数据版后剩下 list） ——

    def generate(
        self,
        max_hypotheses: int = 50,
        service_filter: str | None = None,
    ) -> list:
        """向后兼容：直接返回 list[Hypothesis]。"""
        return self.generate_with_meta(
            max_hypotheses=max_hypotheses, service_filter=service_filter,
        ).get("hypotheses", [])

    def prioritize(self, hypotheses: list) -> list:
        """向后兼容：直接返回 list[Hypothesis]。"""
        return self.prioritize_with_meta(hypotheses).get("prioritized", hypotheses)


class LearningBase(ABC):
    """Chaos 学习引擎基类（Phase 3 Module 2）。

    *核心契约*：子类实现 5 个方法，返回 dict 包含标准元数据字段：
      - engine: str           # "direct" | "strands"
      - model_used: str | None
      - latency_ms: int
      - token_usage: dict | None  # {input,output,total,cache_read,cache_write}
      - trace: list[dict]         # Strands tool-call 链；direct 为 []
      - error: str | None

    LLM 调用点只有 generate_recommendations()，其余 4 个方法是纯 Python 逻辑。
    """

    ENGINE_NAME = "base"

    def __init__(self, hypothesis_engine: Any = None, profile: Any = None) -> None:
        """
        Args:
            hypothesis_engine: make_hypothesis_engine() 的返回值，
                用于 iterate_hypotheses() 补充假设。
            profile: 环境 profile 配置。
        """
        self.hypothesis_engine = hypothesis_engine
        self.profile = profile

    @abstractmethod
    def analyze(self, experiment_results: list[dict]) -> dict:
        """聚合实验结果，生成 coverage 分析。纯 Python 逻辑，无 LLM 调用。

        Returns:
            {
                "coverage": dict,
                "gaps": list[str],
                "service_stats": dict,
                "repeated_failures": list,
                "improvement_trends": list,
                "engine": str,
                "latency_ms": int,
                "error": str | None,
            }
        """

    @abstractmethod
    def iterate_hypotheses(self, coverage: dict, existing_hypotheses: list) -> dict:
        """基于 coverage gap 调用 HypothesisAgent 补新假设。

        Returns:
            {
                "updated_hypotheses": list,
                "new_hypotheses": list,
                "engine": str,
                "latency_ms": int,
                "error": str | None,
            }
        """

    @abstractmethod
    def update_graph(self, learning_data: dict) -> dict:
        """将学习结果写入 Neptune 图谱。

        Returns:
            {
                "vertices_updated": int,
                "edges_updated": int,
                "engine": str,
                "latency_ms": int,
                "error": str | None,
            }
        """

    @abstractmethod
    def generate_report(self, analysis: dict) -> dict:
        """生成 Markdown 报告。

        Returns:
            {
                "report_md": str,
                "engine": str,
                "latency_ms": int,
                "error": str | None,
            }
        """

    @abstractmethod
    def generate_recommendations(self, analysis: dict) -> dict:
        """⚡ 唯一的 LLM 调用点。

        Returns:
            {
                "recommendations": list[str],
                "engine": str,
                "model_used": str | None,
                "latency_ms": int,
                "token_usage": {
                    "input": int, "output": int, "total": int,
                    "cache_read": int, "cache_write": int,
                } | None,
                "trace": list[dict],
                "error": str | None,
            }
        """
