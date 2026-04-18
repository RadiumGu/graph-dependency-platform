"""
nl_query.py — 向后兼容 shim。

PR2 (Strands L1 POC) 将 NLQueryEngine 迁移到 nl_query_direct.py 并重命名为
DirectBedrockNLQuery。现有使用者仍可：

    from neptune.nl_query import NLQueryEngine

Phase 4 统一清理时删除本文件。
"""
from neptune.nl_query_direct import (  # noqa: F401
    DirectBedrockNLQuery as NLQueryEngine,
    MODEL,
    MODEL_HEAVY,
    REGION,
    _NEGATION_HINTS,
    nc,
    query_guard,
    build_system_prompt,
)
