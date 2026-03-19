#!/bin/bash
# deploy.sh - RCA Lambda 部署脚本
#
# 用法:
#   cp .env.example .env          # 按需填写 .env
#   bash deploy.sh                # 正式部署
#   bash deploy.sh --dry-run      # 仅预览，不执行 AWS 操作
#
# 前置条件:
#   - aws-cli >= 2.x 已安装并配置好凭证
#   - zip 已安装
#   - pip3 已安装
#   - IAM Role 已创建（deploy.sh 不创建 Role）
#   - .env 文件已按 .env.example 填写

set -e

# ── 解析参数 ─────────────────────────────────────────────────────────────────
DRY_RUN=false
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=true ;;
  esac
done

run() {
  if $DRY_RUN; then
    echo "[DRY-RUN] $*"
  else
    "$@"
  fi
}

# ── 前置条件检查 ──────────────────────────────────────────────────────────────
echo "=== 前置条件检查 ==="
missing=0
for cmd in aws zip pip3; do
  if ! command -v "$cmd" &>/dev/null; then
    echo "  ✗ $cmd 未安装"
    missing=1
  else
    echo "  ✓ $cmd"
  fi
done
if [ "$missing" -eq 1 ]; then
  echo "请先安装缺失工具后重试"
  exit 1
fi

# ── 加载 .env ──────────────────────────────────────────────────────────────────
ENV_FILE="$(dirname "$0")/.env"
if [ ! -f "$ENV_FILE" ]; then
  echo "错误：$ENV_FILE 不存在。请先复制 .env.example 并填写变量。"
  exit 1
fi
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

# ── 必填变量校验 ───────────────────────────────────────────────────────────────
required_vars=(ACCOUNT REGION FUNCTION_NAME ROLE_ARN SUBNET_IDS SG_IDS NEPTUNE_ENDPOINT)
all_set=1
for var in "${required_vars[@]}"; do
  val="${!var}"
  if [ -z "$val" ]; then
    echo "  ✗ 环境变量 $var 未设置（请检查 .env）"
    all_set=0
  fi
done
if [ "$all_set" -eq 0 ]; then
  exit 1
fi

echo ""
if $DRY_RUN; then
  echo "=== DRY-RUN 模式：以下操作仅打印，不实际执行 ==="
fi

echo "=== Step 1: 打包 Lambda ==="
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if ! $DRY_RUN; then
  # Build in a temp directory to keep source clean
  BUILD_DIR=$(mktemp -d)
  trap "rm -rf $BUILD_DIR" EXIT

  # Copy source files (flat, Lambda-compatible)
  cp "$SCRIPT_DIR"/*.py "$SCRIPT_DIR"/*.json "$SCRIPT_DIR"/deploy.sh "$BUILD_DIR/" 2>/dev/null
  # Install dependencies
  pip3 install requests -t "$BUILD_DIR" -q
  # Package
  cd "$BUILD_DIR"
  rm -f /tmp/rca-engine.zip
  zip -r /tmp/rca-engine.zip . -x "*.pyc" -x "__pycache__/*" -x "deploy.sh" -x "scan-service-db-mapping.py"
  echo "Package size: $(du -sh /tmp/rca-engine.zip | cut -f1)"
  cd "$SCRIPT_DIR"
else
  echo "[DRY-RUN] pip3 install requests -t $SCRIPT_DIR -q"
  echo "[DRY-RUN] zip -r /tmp/rca-engine.zip $SCRIPT_DIR ..."
fi

echo ""
echo "=== Step 2: 创建/更新 Lambda ==="
if ! $DRY_RUN; then
  if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" > /dev/null 2>&1; then
    echo "Updating existing function..."
    aws lambda update-function-code \
        --function-name "$FUNCTION_NAME" \
        --zip-file fileb:///tmp/rca-engine.zip \
        --region "$REGION"
    aws lambda wait function-updated --function-name "$FUNCTION_NAME" --region "$REGION"
    echo "Code updated."
  else
    echo "Creating new function..."
    aws lambda create-function \
        --function-name "$FUNCTION_NAME" \
        --runtime python3.12 \
        --role "$ROLE_ARN" \
        --handler handler.lambda_handler \
        --zip-file fileb:///tmp/rca-engine.zip \
        --timeout 60 \
        --memory-size 256 \
        --environment "Variables={
            NEPTUNE_ENDPOINT=$NEPTUNE_ENDPOINT,
            NEPTUNE_PORT=${NEPTUNE_PORT:-8182},
            REGION=$REGION,
            EKS_CLUSTER=${EKS_CLUSTER:-},
            CLICKHOUSE_HOST=${CLICKHOUSE_HOST:-},
            BEDROCK_MODEL=${BEDROCK_MODEL:-global.anthropic.claude-sonnet-4-6},
            BEDROCK_KB_ID=${BEDROCK_KB_ID:-}
        }" \
        --vpc-config "SubnetIds=$SUBNET_IDS,SecurityGroupIds=$SG_IDS" \
        --region "$REGION"
    echo "Function created."
  fi
else
  echo "[DRY-RUN] aws lambda create-function/update-function-code --function-name $FUNCTION_NAME ..."
fi

echo ""
echo "=== Step 3: 更新环境变量（从 SSM 读取 Webhook）==="
WEBHOOK_URL=$(aws ssm get-parameter --name "${SSM_SLACK_WEBHOOK_PATH:-/rca/slack/webhook}" --with-decryption --query 'Parameter.Value' --output text --region "$REGION" 2>/dev/null || echo "")
if [ -z "$WEBHOOK_URL" ]; then
    echo "⚠️  SSM ${SSM_SLACK_WEBHOOK_PATH:-/rca/slack/webhook} 未找到 — Slack 通知将以 dry-run 模式运行"
else
    run aws lambda update-function-configuration \
        --function-name "$FUNCTION_NAME" \
        --environment "Variables={
            NEPTUNE_ENDPOINT=$NEPTUNE_ENDPOINT,
            NEPTUNE_PORT=${NEPTUNE_PORT:-8182},
            REGION=$REGION,
            EKS_CLUSTER=${EKS_CLUSTER:-},
            CLICKHOUSE_HOST=${CLICKHOUSE_HOST:-},
            BEDROCK_MODEL=${BEDROCK_MODEL:-global.anthropic.claude-sonnet-4-6},
            BEDROCK_KB_ID=${BEDROCK_KB_ID:-},
            SLACK_WEBHOOK_URL=$WEBHOOK_URL,
            SLACK_CHANNEL=${SLACK_CHANNEL:-#rca-alerts}
        }" \
        --region "$REGION" > /dev/null
    echo "Slack webhook configured."
fi

echo ""
echo "=== Step 4: 创建 SNS Topic（RCA 触发器）==="
SNS_TOPIC_ARN=$(run aws sns create-topic --name "${SNS_TOPIC_NAME:-rca-alerts}" --region "$REGION" --query 'TopicArn' --output text 2>/dev/null || echo "arn:aws:sns:${REGION}:${ACCOUNT}:${SNS_TOPIC_NAME:-rca-alerts}")
echo "SNS Topic: $SNS_TOPIC_ARN"

echo ""
echo "=== Step 5: 订阅 Lambda 到 SNS ==="
run aws sns subscribe \
    --topic-arn "$SNS_TOPIC_ARN" \
    --protocol lambda \
    --notification-endpoint "arn:aws:lambda:${REGION}:${ACCOUNT}:function:${FUNCTION_NAME}" \
    --region "$REGION" > /dev/null

run aws lambda add-permission \
    --function-name "$FUNCTION_NAME" \
    --statement-id rca-sns-trigger \
    --action lambda:InvokeFunction \
    --principal sns.amazonaws.com \
    --source-arn "$SNS_TOPIC_ARN" \
    --region "$REGION" 2>/dev/null || echo "Permission already exists"

echo ""
echo "=== Step 6: 冒烟测试（本地调用）==="
run aws lambda invoke \
    --function-name "$FUNCTION_NAME" \
    --payload '{"source":"manual","affected_resource":"payforadoption","metric":"error_rate","value":0.87,"threshold":0.05}' \
    --region "$REGION" \
    /tmp/rca-test-output.json --log-type Tail 2>/dev/null | python3 -c "
import sys, json, base64
d = json.load(sys.stdin)
print('Status:', d.get('StatusCode'))
log = base64.b64decode(d.get('LogResult','')).decode()
print('Log tail:')
for line in log.strip().split('\n')[-10:]:
    print(' ', line)
"
if ! $DRY_RUN && [ -f /tmp/rca-test-output.json ]; then
  echo "Output:"
  python3 -m json.tool /tmp/rca-test-output.json
fi

echo ""
echo "✅ 部署完成！"
echo "SNS Topic ARN: $SNS_TOPIC_ARN"
echo "Lambda ARN: arn:aws:lambda:${REGION}:${ACCOUNT}:function:${FUNCTION_NAME}"
