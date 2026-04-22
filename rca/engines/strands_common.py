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


def build_bedrock_model(
    model_id: str | None = None,
    region: str | None = None,
    max_tokens: int | None = None,
) -> Any:
    """构造 Strands BedrockModel。

    懒导入 strands —— 让 Phase 1 在未装 strands 的环境仍能 import factory。

    Args:
        max_tokens: 覆盖默认 4096。大批量产出场景（max_hypotheses >= 20）建议 16384，
            避免 MaxTokensReachedException（Phase 3 Module 1 retro 坑 4）。
    """
    from strands.models import BedrockModel  # type: ignore
    kwargs = dict(
        model_id=model_id or DEFAULT_MODEL,
        region_name=region or DEFAULT_REGION,
        # L2 Prompt Caching: 在 system prompt + tool schema 处注入 cachePoint。
        # Strands 自动走 Bedrock Converse API 的 cachePoint，默认 ephemeral (5min TTL)。
        cache_prompt="default",
        cache_tools="default",
    )
    if max_tokens is not None:
        kwargs["max_tokens"] = int(max_tokens)
    return BedrockModel(**kwargs)


# OTel / CloudWatch 接入（2026-04-22 实装）
# ADOT Collector 在 localhost:4318 接收 OTLP，导出到 X-Ray + CloudWatch Logs。
# Strands StrandsTelemetry 负责 agent 内部 span；此处额外配 X-Ray ID generator
# 确保 trace ID 格式兼容 X-Ray（前 8 hex = epoch seconds）。


_TELEMETRY_STATE: dict = {"initialized": False}


def _setup_xray_id_generator() -> None:
    """注入 AWS X-Ray compatible trace ID generator 到全局 TracerProvider。

    X-Ray 要求 trace ID 前 32 bit 是 unix timestamp，标准 OTel random ID 会被 X-Ray 丢弃。
    必须在 TracerProvider 初始化前调用，或重新设置 provider。
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator  # type: ignore

        current = trace.get_tracer_provider()
        # 只在还没设过 provider 或者是 proxy 时替换
        if not isinstance(current, TracerProvider):
            provider = TracerProvider(id_generator=AwsXRayIdGenerator())
            trace.set_tracer_provider(provider)
    except ImportError:
        # opentelemetry-sdk-extension-aws 未装；X-Ray 可能丢 trace 但不致命
        import logging
        logging.getLogger(__name__).warning(
            "aws xray id generator not available; traces may not appear in X-Ray")
    except Exception:
        pass  # best-effort


def ensure_telemetry() -> None:
    """按 env 开关初始化 Strands OTel telemetry（幂等）。

    环境变量：
      STRANDS_TELEMETRY=off    → noop（默认）
      STRANDS_TELEMETRY=console → 开启 ConsoleSpanExporter（日志可见）
      STRANDS_TELEMETRY=otlp    → 开启 OTLPSpanExporter → ADOT → X-Ray + CloudWatch
        目标端点由标准 OTEL_EXPORTER_OTLP_ENDPOINT 控制（默认 http://localhost:4318）。

    CloudWatch 接法：
      1. ADOT Collector (Docker) 监听 :4318，配置 awsxray + awsemf exporter；
      2. STRANDS_TELEMETRY=otlp 即可，无需额外 CloudWatch adapter。
      3. X-Ray console → Service Map / Traces 可查 Strands agent 调用链。
    """
    if _TELEMETRY_STATE["initialized"]:
        return
    mode = (os.environ.get("STRANDS_TELEMETRY") or "off").lower()
    if mode in ("", "off", "0", "false", "no"):
        _TELEMETRY_STATE["initialized"] = True
        return
    try:
        # X-Ray ID generator 必须在 exporter setup 之前
        if mode == "otlp":
            _setup_xray_id_generator()

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
