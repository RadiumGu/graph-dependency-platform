#!/usr/bin/env bash
# infra/dr-korea/temporal-1.32/iam-grants.sh
#
# 给灾备 worker 的实例角色补三项权限。**三项各自独立的内联策略**，
# 可以分别授予/撤销 —— 一个大策略会让「为什么有这条权限」无从追溯。
#
#   bash iam-grants.sh --dry-run
#   bash iam-grants.sh --apply plan-write          # DrPlanWorkflow 必需
#   bash iam-grants.sh --apply neptune-read        # 快照导出必需
#   bash iam-grants.sh --apply assume-invocation ROLE_ARN   # serverless worker 前置
#   bash iam-grants.sh --revert plan-write
#
# ## 为什么需要它们 —— 三项都实测确认过是 implicitDeny
#
# 2026-09-26 用 `aws iam simulate-principal-policy` 实测：
#     neptune-db:connect  implicitDeny
#     s3:PutObject        implicitDeny
#     sts:AssumeRole      implicitDeny
#
# ## ① plan-write：**这一项缺了 DrPlanWorkflow 第一步就挂**
#
# 现有托管策略叫 DrKoreaWorkerReadPlans，对 plans/* 只有 s3:GetObject ——
# 那是刻意的只读姿态。但计划评审生命周期要**写**版本化的计划正文
# （put_plan_version），所以第一个 activity 就会 AccessDenied。
#
# ⚠️ 一个诚实的边界要说明：计划版本的**不可覆盖性目前靠应用层**
# （put_plan_version 用 IfNoneMatch="*" 做条件写）。IAM 的 s3:PutObject
# 本身是允许覆盖的，所以「被批准并演练过的 v2」与「真执行时取到的 v2」
# 在 IAM 层面并没有被强制成同一份文件。要把它变成机制保证，正确的手段是
# 桶级 Object Lock 或一条拒绝覆盖的桶策略 —— 那是单独一件事，
# 不在本脚本里，但不应被当作已解决。
#
# ## ② neptune-read：网络通了不等于能查图
#
# Neptune 开着 IAM 数据库认证（实测 IAMDatabaseAuthenticationEnabled=True），
# 所以放通 8182 之后仍然查不到数据。这里只给**读**：connect / ReadDataViaQuery /
# GetEngineStatus，不给任何写 —— 快照导出是只读作业，
# 而 dr-plan-generator 在架构上就被定为对图只读。
#
# ## ③ assume-invocation：**必须带具体的 invocation role ARN**
#
# 本脚本**拒绝**在不给 ARN 时授予它，理由不是谨慎而是具体：
# serverless worker 的 invocation role 要按**某个 AgentCore Runtime ARN**
# 建出来，而目前还没有 worker 类型的 Runtime（账号里那 5 个运行时是
# temporal_mcp 与 4 个 WaggleAI，都不是 Temporal worker）。
# 在没有目标的情况下先建一条 sts:AssumeRole，只能写成通配 —— 那是
# 「看起来做完了但授权面过大」，比不做更糟。

set -euo pipefail

ROLE=dr-korea-temporal-TemporalRole-MbW4WYmjoR8J
PLAN_BUCKET=dr-korea-agentcore-926093770964-ap-northeast-2
ACCOUNT=926093770964
NEPTUNE_REGION=ap-northeast-1
NEPTUNE_RESOURCE_ID=cluster-TPNY7IXPX2YQ5ZIAPC5Y6EJRRM

P_PLAN=dr-plan-write
P_NEPTUNE=dr-neptune-read
P_ASSUME=dr-assume-invocation-role

doc_plan() {
  cat <<JSON
{"Version":"2012-10-17","Statement":[{
  "Sid":"WritePlanVersions",
  "Effect":"Allow",
  "Action":["s3:PutObject"],
  "Resource":"arn:aws:s3:::$PLAN_BUCKET/plans/*"
}]}
JSON
}

doc_neptune() {
  cat <<JSON
{"Version":"2012-10-17","Statement":[{
  "Sid":"ReadGraphForSnapshotExport",
  "Effect":"Allow",
  "Action":["neptune-db:connect","neptune-db:ReadDataViaQuery","neptune-db:GetEngineStatus"],
  "Resource":"arn:aws:neptune-db:$NEPTUNE_REGION:$ACCOUNT:$NEPTUNE_RESOURCE_ID/*"
}]}
JSON
}

doc_assume() {
  cat <<JSON
{"Version":"2012-10-17","Statement":[{
  "Sid":"AssumeServerlessWorkerInvocationRole",
  "Effect":"Allow",
  "Action":["sts:AssumeRole"],
  "Resource":"$1"
}]}
JSON
}

show() {
  echo "角色：$ROLE"
  echo "现有内联策略：$(aws iam list-role-policies --role-name "$ROLE" --query 'PolicyNames' --output text)"
  echo "现有托管策略：$(aws iam list-attached-role-policies --role-name "$ROLE" --query 'AttachedPolicies[*].PolicyName' --output text)"
  echo
  echo "三项权限的当前判定（模拟）："
  aws iam simulate-principal-policy \
    --policy-source-arn "arn:aws:iam::$ACCOUNT:role/$ROLE" \
    --action-names s3:PutObject neptune-db:connect sts:AssumeRole \
    --query 'EvaluationResults[*].[EvalActionName,EvalDecision]' --output text | sed 's/^/  /'
}

case "${1:-}" in
  --dry-run)
    show
    echo
    echo "── plan-write ──";     doc_plan
    echo "── neptune-read ──";   doc_neptune
    echo "── assume-invocation ──"
    echo "  需要 invocation role ARN 才能生成 —— 目前不应授予，见脚本头部说明。"
    ;;
  --apply)
    case "${2:-}" in
      plan-write)
        aws iam put-role-policy --role-name "$ROLE" --policy-name "$P_PLAN" \
          --policy-document "$(doc_plan)"
        echo "已授予 $P_PLAN"
        ;;
      neptune-read)
        aws iam put-role-policy --role-name "$ROLE" --policy-name "$P_NEPTUNE" \
          --policy-document "$(doc_neptune)"
        echo "已授予 $P_NEPTUNE"
        ;;
      assume-invocation)
        ARN="${3:-}"
        case "$ARN" in
          arn:aws:iam::*:role/*) : ;;
          *)
            cat >&2 <<'ERR'
拒绝执行：assume-invocation 必须带一个具体的 invocation role ARN。

原因不是谨慎，是具体：serverless worker 的 invocation role 要按某个
AgentCore Runtime ARN 建出来，而现在还没有 worker 类型的 Runtime
（账号里的 5 个是 temporal_mcp 与 4 个 WaggleAI，都不是 Temporal worker）。
没有目标就只能写通配 —— 那是「看起来做完了但授权面过大」。

正确顺序：
  1) 先把 worker 打包成 AgentCore Runtime 并部署，拿到 Runtime ARN
  2) 用官方 CloudFormation 模板建 invocation role（带 External ID），
     TemporalIamRoleArn 传本角色
  3) 回来跑：bash iam-grants.sh --apply assume-invocation <invocation-role-arn>
ERR
            exit 2 ;;
        esac
        aws iam put-role-policy --role-name "$ROLE" --policy-name "$P_ASSUME" \
          --policy-document "$(doc_assume "$ARN")"
        echo "已授予 $P_ASSUME -> $ARN"
        ;;
      *) echo "用法: --apply plan-write|neptune-read|assume-invocation <arn>" >&2; exit 2 ;;
    esac
    echo
    echo "复核："; show
    ;;
  --revert)
    case "${2:-}" in
      plan-write)        aws iam delete-role-policy --role-name "$ROLE" --policy-name "$P_PLAN" ;;
      neptune-read)      aws iam delete-role-policy --role-name "$ROLE" --policy-name "$P_NEPTUNE" ;;
      assume-invocation) aws iam delete-role-policy --role-name "$ROLE" --policy-name "$P_ASSUME" ;;
      *) echo "用法: --revert plan-write|neptune-read|assume-invocation" >&2; exit 2 ;;
    esac
    echo "已撤销 ${2}"
    ;;
  *)
    echo "用法: $0 --dry-run | --apply <grant> [arn] | --revert <grant>" >&2
    exit 2 ;;
esac
