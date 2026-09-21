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
      - engine: str          # 实现标签。2026-09-21 起只有 "strands"（direct 已删）
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


# ── Phase 3 三模块的 Base 住在各自模块里，不在本文件 ──────────────────────
#
# 这里原先写着 `TODO(phase-3): 以下 Base 为未来模块预留占位`，列出
# ChaosPolicyGuardBase / ChaosRunnerBase / DRExecutorBase 三个待建类。
#
# ⚠️ 2026-09-21 更正：那条注释**双重过期**，照它做会白干一遍 ——
#
#   1. 这三个模块的 Phase 3 早已完成（2026-09-20 起 direct 实现全部删除、
#      七个模块全部 strands 化），它们不是「未来」；
#   2. 它们的 Base **已经存在**，只是按模块就近放置而非集中到本文件：
#
#          PolicyGuardBase  → chaos/code/policy/base.py:10
#          RunnerBase       → chaos/code/runner/base.py:19
#          ExecutorBase     → dr-plan-generator/executor_base.py:16
#
# 所以本文件只收 rca 自己用的四个 Base（NLQuery / Hypothesis / Learning /
# Layer2Prober）。就近放置是有意的：chaos 与 dr-plan-generator 是独立部署
# 单元，把它们的抽象基类拖进 rca/ 会让部署包多带一个跨目录依赖。


class HypothesisBase(ABC):
    """Chaos 混沌工程假设生成引擎基类（Phase 3 Module 1）。

    *核心契约*：子类实现 generate_with_meta() / prioritize_with_meta()，返回 dict：
      - hypotheses: list  # 业务产出，generate 有值；prioritize 可为空
      - prioritized: list # 有排序的版本，generate 为空；prioritize 有值
      - engine: str       # 实现标签。2026-09-21 起只有 "strands"（direct 已删）
      - model_used: str | None
      - latency_ms: int
      - token_usage: dict | None  # {input,output,total,cache_read,cache_write}
      - trace: list[dict]         # Strands tool-call 链
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
      - engine: str           # 实现标签。2026-09-21 起只有 "strands"（direct 已删）
      - model_used: str | None
      - latency_ms: int
      - token_usage: dict | None  # {input,output,total,cache_read,cache_write}
      - trace: list[dict]         # Strands tool-call 链
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


class Layer2ProberBase(ABC):
    """RCA Layer2 Prober 引擎基类（Phase 3 Module 3）。

    *核心契约*：子类实现 run_probes() 和 run_single_probe()，
    并行执行 6 个 Prober，返回汇总结果。

    *向后兼容*：提供 format_probe_results() 和 total_score_delta()
    使 rca_engine.py 无需改动调用方式。
    """

    ENGINE_NAME = "base"

    def __init__(self, profile: Any = None) -> None:
        self.profile = profile

    @abstractmethod
    def run_probes(
        self,
        signal: dict,
        affected_service: str,
        timeout_sec: int = 30,
    ) -> dict:
        """\u5e76\u884c\u6267\u884c 6 \u4e2a Prober\uff0c\u8fd4\u56de\u6c47\u603b\u7ed3\u679c\u3002

        Returns:
            {
                "probe_results": list[dict],   # ProbeResult dicts
                "summary": str,                # 汇总摘要
                "score_delta": int,            # 总分增量 (cap 40)
                "engine": str,                 # 只有 "strands"（direct 已删）
                "model_used": str | None,
                "latency_ms": int,
                "token_usage": dict | None,
                "trace": list[dict],
                "error": str | None,
            }
        """

    @abstractmethod
    def run_single_probe(
        self,
        probe_name: str,
        signal: dict,
        affected_service: str,
    ) -> dict:
        """\u5355\u72ec\u6267\u884c\u4e00\u4e2a Prober\uff08\u7528\u4e8e\u8c03\u8bd5/\u6d4b\u8bd5\uff09\u3002

        Returns:
            {
                "service_name": str,
                "healthy": bool,
                "score_delta": int,
                "summary": str,
                "details": dict,
                "evidence": list[str],
                "engine": str,
                "token_usage": dict | None,
                "trace": list[dict],
            }
        """

    # \u2014\u2014 \u5411\u540e\u517c\u5bb9 helper \u2014\u2014

    @staticmethod
    def format_probe_results(results: list[dict]) -> str:
        """\u683c\u5f0f\u5316 probe \u7ed3\u679c\u4e3a prompt block\u3002"""
        if not results:
            return "[Layer2 AWS Probers]\nNo anomalies detected across monitored AWS services."
        lines = []
        for r in results:
            svc = r.get("service_name", "Unknown")
            healthy = r.get("healthy", True)
            summary = r.get("summary", "")
            evidence = r.get("evidence", [])
            # 原先写作 f"Status: {'OK' if healthy else '\u26a0\ufe0f ANOMALY'}" ——
            # f-string 表达式部分含反斜杠在 **Python 3.12 之前是 SyntaxError**
            # (PEP 701 才放宽)。Lambda 运行时是 3.12 所以生产可用,但本地 3.10/3.11
            # 连解析都过不去:本文件导入失败 → engines.factory 失败 →
            # 实测 78 个测试 error 全部源于这一行。
            # 把转义序列提到 f-string 之外,使本文件在 3.10+ 均可解析。
            _status = "OK" if healthy else "\u26a0\ufe0f ANOMALY"
            block = [f"[{svc} Probe]",
                     f"Status: {_status}",
                     f"Summary: {summary}"]
            for e in evidence:
                block.append(f"  - {e}")
            lines.append("\n".join(block))
        return "\n\n".join(lines)

    @staticmethod
    def total_score_delta(results: list[dict]) -> int:
        """\u6c47\u603b\u5f02\u5e38\u5206\u6570 (cap 40)\u3002"""
        return min(
            sum(r.get("score_delta", 0) for r in results if not r.get("healthy", True)),
            40,
        )
