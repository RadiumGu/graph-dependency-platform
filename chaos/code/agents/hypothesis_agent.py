"""
hypothesis_agent.py — 向后兼容 shim（Phase 3 Module 1, PR2）。

PR2 将 HypothesisAgent 迁到 hypothesis_direct.py，类名改为 DirectBedrockHypothesis。
现有调用方仍可继续：

    from chaos.code.agents.hypothesis_agent import HypothesisAgent

Phase 4 统一清理时删除本文件。
"""
from .hypothesis_direct import (  # noqa: F401
    DirectBedrockHypothesis as HypothesisAgent,
    FAULT_DEFAULTS,
    TIER_CONFIG,
    VALID_FAULT_TYPES,
    HYPOTHESES_PATH,
    _invoke_llm,
    _extract_json,
    _gremlin_query,
)
