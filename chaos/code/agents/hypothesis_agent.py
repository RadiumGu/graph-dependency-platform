"""hypothesis_agent.py — 向后兼容 shim。

`HypothesisAgent` 这个名字仍被 `chaos/code/main.py` 与 `orchestrator.py`
使用（它们要的是 `.load()` / `.save()` / `.to_experiment_yamls()` 这些
附属方法），所以名字保留。

2026-09-20：指向从 `hypothesis_direct.DirectBedrockHypothesis` 改为
`hypothesis_strands.StrandsHypothesisAgent` —— direct 实现已删除，
三个附属方法搬到 `hypothesis_common` 并由 strands 版薄委托。

`VALID_FAULT_TYPES` 现在从 `runner/fault_registry.py` 的权威表派生
（19 个），而不是 direct 里那份过期的硬编码副本（9 个）。
"""
from .hypothesis_common import VALID_FAULT_TYPES  # noqa: F401
from .hypothesis_strands import StrandsHypothesisAgent as HypothesisAgent  # noqa: F401

__all__ = ["HypothesisAgent", "VALID_FAULT_TYPES"]
