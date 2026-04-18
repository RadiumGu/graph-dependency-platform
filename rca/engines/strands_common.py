"""engines/strands_common.py — Strands BedrockModel / tool helpers 共享代码。

Phase 1 仅占位。Phase 2 PR3 会在此补：
  - build_bedrock_model(model_id, region=ap-northeast-1)
  - wrap_tool_trace(func): 给 @tool 套 trace 采集
  - OTel 接入 hook（TODO）

硬约束：
  - model_id 必须用 inference profile（global.* / apac.* / us.*），不能裸 model id
  - region_name 必须显式传，默认 ap-northeast-1
  - L0 spike 验证：仅 global.anthropic.claude-sonnet-4-6 在 ap-northeast-1 可用
"""
from __future__ import annotations

import os
from typing import Any

DEFAULT_REGION = os.environ.get("BEDROCK_REGION") or os.environ.get("AWS_REGION") or "ap-northeast-1"
DEFAULT_MODEL = os.environ.get("BEDROCK_MODEL", "global.anthropic.claude-sonnet-4-6")
HEAVY_MODEL = os.environ.get("BEDROCK_MODEL_HEAVY", "global.anthropic.claude-opus-4-7")


def build_bedrock_model(model_id: str | None = None, region: str | None = None) -> Any:
    """构造 Strands BedrockModel。

    懒导入 strands —— 让 Phase 1 在未装 strands 的环境仍能 import factory。
    """
    from strands.models import BedrockModel  # type: ignore
    return BedrockModel(
        model_id=model_id or DEFAULT_MODEL,
        region_name=region or DEFAULT_REGION,
    )


# TODO(phase-2): OTel / CloudWatch 接入
# 参考 experiments/strands-poc/spike.py 的 _LAST_CALLS hack；L1 要用 Strands 原生 callbacks。
