#!/usr/bin/env bash
# arc-approve.sh — 人在 ARC Region switch 执行里批准（或拒绝）一步。**必须带 MFA。**
#
# 审批角色的信任策略要求 aws:MultiFactorAuthPresent（20-arc-region-switch-poc.yaml）。
# agent 用的长期访问密钥没有 MFA 上下文，所以它能起执行、看执行，批不了 ——
# 这个脚本要你当场输入 MFA 动态码，就是那道闸门。
#
# 用法：
#   bash infra/dr-korea/arc-approve.sh <execution-id> [approve|decline] [step-name]
#
# 也可以在控制台批：以带 MFA 的身份登录 → 切换角色到 petsite-dr-poc-approver →
# ARC → Region switch → 计划 → 执行 → 批准。
set -euo pipefail

EXEC_ID="${1:?用法：arc-approve.sh <execution-id> [approve|decline] [step-name]}"
DECISION="${2:-approve}"
STEP="${3:-approve-prepare}"
REGION="${ARC_REGION:-ap-northeast-2}"          # 在要激活的区域的数据面上批
PLAN_NAME="${ARC_PLAN_NAME:-petsite-dr-poc}"
case "$DECISION" in approve|decline) ;; *) echo "第二个参数只能是 approve 或 decline" >&2; exit 2;; esac

ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
ME="$(aws sts get-caller-identity --query Arn --output text)"
ROLE="arn:aws:iam::${ACCOUNT}:role/${PLAN_NAME}-approver"
MFA="$(aws iam list-mfa-devices --query 'MFADevices[0].SerialNumber' --output text)"
[ "$MFA" != "None" ] || { echo "当前身份 ${ME} 没有 MFA 设备 —— 审批角色不允许没有 MFA 的人扮演" >&2; exit 1; }

PLAN_ARN="$(aws arc-region-switch list-plans-in-region --region "$REGION" \
  --query "plans[?name=='${PLAN_NAME}'].arn | [0]" --output text)"

echo "身份：${ME}"
echo "计划：${PLAN_ARN}"
echo "执行：${EXEC_ID}   步骤：${STEP}   决定：${DECISION}"
read -r -p "MFA 动态码：" CODE

CREDS="$(aws sts assume-role --role-arn "$ROLE" --role-session-name "approve-${USER:-human}" \
  --serial-number "$MFA" --token-code "$CODE" --duration-seconds 900 \
  --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text)"
read -r AK SK ST <<<"$CREDS"

# 只在这个子 shell 里用审批角色的临时凭据，不污染调用方
(
  export AWS_ACCESS_KEY_ID="$AK" AWS_SECRET_ACCESS_KEY="$SK" AWS_SESSION_TOKEN="$ST"
  echo "--- 批准前的状态 ---"
  aws arc-region-switch get-plan-execution --region "$REGION" --plan-arn "$PLAN_ARN" \
    --execution-id "$EXEC_ID" --query '{state:executionState,steps:stepStates[].[name,status]}' --output json
  read -r -p "确认 ${DECISION} 步骤 ${STEP}？输入 yes 继续：" OK
  [ "$OK" = "yes" ] || { echo "未确认，什么都没做"; exit 1; }
  aws arc-region-switch approve-plan-execution-step --region "$REGION" --plan-arn "$PLAN_ARN" \
    --execution-id "$EXEC_ID" --step-name "$STEP" --approval "$DECISION" \
    --comment "by ${ME} via arc-approve.sh"
  echo "已提交：${DECISION}。Temporal 会在下一次轮询（约 20 秒）看到变化。"
)
