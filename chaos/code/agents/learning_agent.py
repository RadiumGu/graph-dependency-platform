"""
learning_agent.py — 向后兼容 shim。

re-export DirectBedrockLearning as LearningAgent，
保证 `from chaos.code.agents.learning_agent import LearningAgent` 零改动。

Phase 3 Module 2 迁移产物。实际实现在 learning_direct.py。
"""
from .learning_direct import DirectBedrockLearning as LearningAgent  # noqa: F401

__all__ = ["LearningAgent"]
