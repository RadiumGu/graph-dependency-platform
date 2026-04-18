"""engines/factory.py — NLQuery 引擎工厂，按环境变量 NLQUERY_ENGINE 选实现。

规则：
  - 默认 direct
  - NLQUERY_ENGINE=strands 且依赖已装 → strands
  - NLQUERY_ENGINE=strands 但 strands 未装 → 日志 warning + 回退 direct（不崩）
"""
from __future__ import annotations

import logging
import os
from typing import Any

from engines.base import NLQueryBase

logger = logging.getLogger(__name__)


def make_nlquery_engine(profile: Any = None) -> NLQueryBase:
    """构造 NLQuery 引擎。

    Args:
        profile: EnvironmentProfile；为 None 时由具体 engine 在 __init__ 内加载默认 profile。

    Returns:
        NLQueryBase 具体实现。
    """
    engine = (os.environ.get("NLQUERY_ENGINE") or "direct").lower()
    if engine == "strands":
        try:
            from neptune.nl_query_strands import StrandsNLQueryEngine  # type: ignore
            return StrandsNLQueryEngine(profile=profile)
        except ImportError as e:
            logger.warning(
                "Strands engine 不可用 (%s)；回退 direct。"
                "主环境安装：/usr/bin/pip3 install 'strands-agents>=1.36' 'strands-agents-tools>=0.5'",
                e,
            )
        except Exception as e:  # 构造期失败也回退，避免线上崩
            logger.warning("Strands engine 构造失败 (%r)；回退 direct。", e)

    try:
        from neptune.nl_query_direct import DirectBedrockNLQuery  # type: ignore
        return DirectBedrockNLQuery(profile=profile)
    except ImportError:
        # PR2 rename 前的过渡期：回退到现版 NLQueryEngine
        from neptune.nl_query import NLQueryEngine  # type: ignore
        try:
            return NLQueryEngine(profile=profile)
        except TypeError:
            return NLQueryEngine()