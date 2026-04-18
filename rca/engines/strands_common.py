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


_TELEMETRY_STATE: dict = {"initialized": False}


def ensure_telemetry() -> None:
    """按 env 开关初始化 Strands OTel telemetry（幂等）。

    环境变量：
      STRANDS_TELEMETRY=off    → noop（默认）
      STRANDS_TELEMETRY=console → 开启 ConsoleSpanExporter（日志可见）
      STRANDS_TELEMETRY=otlp    → 开启 OTLPSpanExporter
        目标端点由标准 OTEL_EXPORTER_OTLP_ENDPOINT 、
        OTEL_EXPORTER_OTLP_HEADERS 控制（ADOT / CloudWatch Agent / 第三方均可）。

    CloudWatch 接法：
      1. 单独跑 aws-otel-collector / ADOT，配置 OTLP→CloudWatch Logs/X-Ray exporter；
      2. STRANDS_TELEMETRY=otlp + OTEL_EXPORTER_OTLP_ENDPOINT 指向 collector。
      通过标准 OTLP pipe 解耦，无需特别 CloudWatch adapter。
    """
    if _TELEMETRY_STATE["initialized"]:
        return
    mode = (os.environ.get("STRANDS_TELEMETRY") or "off").lower()
    if mode in ("", "off", "0", "false", "no"):
        _TELEMETRY_STATE["initialized"] = True
        return
    try:
        from strands.telemetry import StrandsTelemetry  # type: ignore
        st = StrandsTelemetry()
        if mode == "console":
            st.setup_console_exporter()
        elif mode == "otlp":
            st.setup_otlp_exporter()
        else:
            import logging
            logging.getLogger(__name__).warning(
                "STRANDS_TELEMETRY=%s unknown; falling back to console", mode)
            st.setup_console_exporter()
    except Exception as e:  # 任何失败都不能拖垮查询路径
        import logging
        logging.getLogger(__name__).warning("Strands telemetry setup failed: %r", e)
    finally:
        _TELEMETRY_STATE["initialized"] = True
