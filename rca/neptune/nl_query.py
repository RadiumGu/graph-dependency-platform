"""nl_query.py — 向后兼容 shim。

`NLQueryEngine` 这个名字被若干测试与集成点使用，所以保留。

2026-09-20：指向从 `nl_query_direct.DirectBedrockNLQuery` 改为
`nl_query_strands.StrandsNLQueryEngine` —— direct 实现已删除。

⚠️ 用这个 shim 的调用方注意一处**行为差异**：
strands 的 `_pack()` **总是**带 `error` 这个 key（成功时值为 `None`），
而 direct 只在出错时才放。所以判错误必须用 `result.get('error')`，
不能用 `'error' in result` —— 后者在 strands 下永远为真，会把每次成功
都判成失败。（这个坑实际发生过：test_e2e02 因此显示「strands 通过率不足」，
而两个引擎其实都成功了。）

新代码请直接走 `engines.factory.make_nlquery_engine()`，不要用这个 shim：
引擎选择应归 factory（env `NLQUERY_ENGINE`），调用方不该绑死具体实现。
"""
from neptune.nl_query_strands import StrandsNLQueryEngine as NLQueryEngine  # noqa: F401
from neptune.schema_prompt import build_system_prompt  # noqa: F401
from neptune import neptune_client as nc  # noqa: F401
from neptune import query_guard  # noqa: F401
from engines.strands_common import (  # noqa: F401
    DEFAULT_MODEL as MODEL,
    HEAVY_MODEL as MODEL_HEAVY,
    DEFAULT_REGION as REGION,
)

__all__ = ["NLQueryEngine", "build_system_prompt", "nc", "query_guard",
           "MODEL", "MODEL_HEAVY", "REGION"]
