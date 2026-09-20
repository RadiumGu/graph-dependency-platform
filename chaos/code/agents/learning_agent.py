"""learning_agent.py — 向后兼容 shim。

`LearningAgent` 这个名字保留，保证
`from chaos.code.agents.learning_agent import LearningAgent` 零改动。

2026-09-20：指向从 `learning_direct.DirectBedrockLearning` 改为
`learning_strands.StrandsLearningAgent` —— direct 实现已删除。
那四个不含 LLM 的方法（analyze / iterate_hypotheses / update_graph /
generate_report，共 304 行）搬到 `learning_common.LearningCommonMixin`，
由 strands 版继承提供。
"""
from .learning_strands import StrandsLearningAgent as LearningAgent  # noqa: F401

__all__ = ["LearningAgent"]
