#!/usr/bin/env bash
# enable_agentcore_observability.sh —— 把 Strands agent 的 OTel 轨迹接进
# Amazon Bedrock AgentCore Observability（CloudWatch GenAI Observability 面板）。
#
# 背景：本项目 6 条 LLM 路径已全部走 Strands（DoD-9），而 rca/engines/strands_common.py
# 里已有完整的 telemetry 骨架（ensure_telemetry()，STRANDS_TELEMETRY=off|console|otlp）。
# 2026-08-30 console 模式实测出真 span，且是 GenAI 语义约定
# （invoke_agent / chat / execute_event_loop_cycle / gen_ai.choice / gen_ai.user.message）
# —— 正是 AgentCore Observability 消费的格式。所以接入是**配置级**的，不需要新建计费资源。
#
# 用法：
#   source ./enable_agentcore_observability.sh          # 只导出 env（安全，可逆）
#   ./enable_agentcore_observability.sh --check          # 只检查前置条件
#   ./enable_agentcore_observability.sh --enable-transaction-search   # ⚠️ 见下方警告
#
# ─────────────────────────────────────────────────────────────────────────────
# ⚠️ 唯一的闸门：CloudWatch Transaction Search
#
# 实测（2026-08-30）：`aws xray get-trace-segment-destination` 返回
#   {"Destination": "XRay", "Status": "ACTIVE"}
# 而 AgentCore Observability 要求 Destination = CloudWatchLogs。
# 且 indexing rule 的 DesiredSamplingPercentage 是 0.0。
#
# 为什么不默认执行：
#   1. **账号级变更**，影响该 region 全部 X-Ray 消费方；
#   2. **计费**：Transaction Search 按摄入 CloudWatch Logs 的 span 量收费
#      （该账号已有 EKS audit 约 $45/月，不宜再无意识叠加）；
#   3. **本项目自身有 X-Ray 依赖** —— `infra/lambda/etl_xray/` 读 X-Ray trace 建拓扑边。
#      切换 destination 是否影响 GetTraceSummaries / BatchGetTraces **尚未验证**。
#      在验证之前翻这个开关，可能静默打断本项目的一条 ETL 数据源。
#
# 回退：aws xray update-trace-segment-destination --destination XRay --region <r>
# ─────────────────────────────────────────────────────────────────────────────

set -uo pipefail

REGION="${AWS_DEFAULT_REGION:-ap-northeast-1}"
AGENT_NAME="${AGENTCORE_AGENT_NAME:-graph-dep-chaos-agent}"
LOG_GROUP="/aws/bedrock-agentcore/runtimes/${AGENT_NAME}"
LOG_STREAM="runtime-logs"
TRACE_STREAM="span-logs"

# ── 1. Strands 侧开关（本项目自有骨架）──
export STRANDS_TELEMETRY=otlp

# ── 2. ADOT 管道（AgentCore Observability 要求）──
export AGENT_OBSERVABILITY_ENABLED=true
export OTEL_PYTHON_DISTRO=aws_distro
export OTEL_PYTHON_CONFIGURATOR=aws_configurator
export OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf
export OTEL_TRACES_EXPORTER=otlp

# aws.log.group.names 是把数据索引进 AgentCore Observability 面板的关键；
# 少了它 trace 只会进通用 CloudWatch Logs，面板上看不到。
export OTEL_RESOURCE_ATTRIBUTES="service.name=${AGENT_NAME},aws.log.group.names=${LOG_GROUP}"
export OTEL_EXPORTER_OTLP_LOGS_HEADERS="x-aws-log-group=${LOG_GROUP},x-aws-log-stream=${LOG_STREAM},x-aws-metric-namespace=bedrock-agentcore"

# 把 span 投到自有日志组而非 aws/spans（需 ADOT >= 0.18.0；本机实测 0.19.0）。
# 注意：启用这一项还需给该日志组加资源策略，允许 xray.amazonaws.com 调 logs:PutLogEvents，
# 否则 X-Ray 投不进去。默认注释掉，先用 aws/spans。
# export OTEL_EXPORTER_OTLP_TRACES_HEADERS="x-aws-log-group=${LOG_GROUP},x-aws-log-stream=${TRACE_STREAM}"

case "${1:-}" in
  --check)
    echo "region      = $REGION"
    echo "agent name  = $AGENT_NAME"
    echo "log group   = $LOG_GROUP"
    echo
    echo "--- ADOT ---"
    python3.11 -c "from importlib.metadata import version; print('  aws-opentelemetry-distro', version('aws-opentelemetry-distro'))" \
      2>/dev/null || echo "  ❌ aws-opentelemetry-distro 未安装：python3.11 -m pip install --user aws-opentelemetry-distro"
    python3.11 -c "from importlib.metadata import version; print('  strands-agents', version('strands-agents'))" \
      2>/dev/null || echo "  ❌ strands-agents 未安装"
    echo
    echo "--- 日志组 ---"
    aws logs describe-log-groups --log-group-name-prefix "$LOG_GROUP" --region "$REGION" \
      --query "logGroups[].[logGroupName,retentionInDays]" --output text 2>/dev/null \
      | sed 's/^/  /' || echo "  ❌ 日志组不存在"
    echo
    echo "--- Transaction Search（闸门）---"
    DEST=$(aws xray get-trace-segment-destination --region "$REGION" --query Destination --output text 2>/dev/null)
    echo "  Destination = ${DEST:-unknown}"
    if [[ "$DEST" == "CloudWatchLogs" ]]; then
      echo "  ✅ 已启用"
    else
      echo "  ⚠️  未启用 —— AgentCore Observability 面板不会显示 span。"
      echo "      翻开关前必须先验证它对 infra/lambda/etl_xray/ 的 X-Ray 读取无影响。"
    fi
    aws xray get-indexing-rules --region "$REGION" \
      --query "IndexingRules[].[Name,Rule.Probabilistic.DesiredSamplingPercentage]" --output text 2>/dev/null \
      | sed 's/^/  sampling: /'
    ;;
  --enable-transaction-search)
    echo "⚠️  这是账号级、计费、且可能影响 etl_xray 的变更。"
    echo "    请先确认已验证 X-Ray 读取路径不受影响（见本文件顶部第 3 条）。"
    read -r -p "    输入 ENABLE 继续："  ans
    [[ "$ans" == "ENABLE" ]] || { echo "已取消。"; exit 1; }
    aws xray update-trace-segment-destination --destination CloudWatchLogs --region "$REGION"
    # 采样率从低起步，观察成本后再调
    aws xray update-indexing-rule --name Default \
      --rule '{"Probabilistic":{"DesiredSamplingPercentage":10.0}}' --region "$REGION" || true
    echo "回退：aws xray update-trace-segment-destination --destination XRay --region $REGION"
    ;;
  "")
    echo "已导出 AgentCore Observability 环境变量（STRANDS_TELEMETRY=otlp）。"
    echo "跑 --check 看前置条件，--enable-transaction-search 翻那个闸门。"
    ;;
  *)
    echo "未知参数：$1"; exit 2 ;;
esac
