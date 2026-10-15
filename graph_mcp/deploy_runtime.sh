#!/usr/bin/env bash
# deploy_runtime.sh —— 把 main 上的 graph MCP 部署到 AgentCore Runtime。
#
# 为什么有这个脚本（2026-10-15）：
#   线上 Runtime v3 部署于 2026-09-05，此后它打包的 rca/neptune/neptune_queries.py
#   改了 5 次、profiles/graph_contract.yaml 改了 14 次，MCP 一次都没重新部署。
#   q1/q3 的方向修复（1c1a1b7）到了 Lambda 却没到 MCP —— DevOps Agent 问
#   「X 挂了影响谁」拿到的是「X 依赖谁」，持续了 40 天。
#   根源是每次手敲 update-agent-runtime；tests/test_137 现在守着线上与 main 一致。
#
# 两个固化下来的坑：
#   1. update-agent-runtime 必须**全量重传**所有配置块。只传 artifact 会把
#      authorizer / network 清掉（2026-09-05 首次部署时踩过：v1 没有 JWT authorizer）。
#   2. **不得**在 networkModeConfig 里带 requireServiceS3Endpoint。对 2026-06-08
#      之后创建的 agent，请求里只要出现这个键就 ValidationException ——
#      即使值就是当前的 false。所以「get 回来原样 update 回去」会失败。
#
# 用法：
#   bash graph_mcp/deploy_runtime.sh            # 构建 + 上传 + 更新 + 验证
#   bash graph_mcp/deploy_runtime.sh --rollback <s3-key>   # 指回旧包
#
# 回滚：旧包从不删除，S3 key 带 git sha 与时间戳。
set -euo pipefail

REGION=ap-northeast-1
RUNTIME_ID=graph_dependency_mcp-12Vg2Z9XXu
BUCKET=m2m-926093770964-ap-northeast-1
ROLE=arn:aws:iam::926093770964:role/GraphDpMcpAgentCoreRole
POOL=ap-northeast-1_Dwd1wVX7j
CLIENT=2u5s7r3gprc8mo86890sdi1h7t
NEPTUNE=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ "${1:-}" == "--rollback" ]]; then
  KEY="${2:?用法: --rollback <s3-key>}"
  echo "回滚到 s3://$BUCKET/$KEY"
else
  if [[ -n "$(git status --porcelain -- graph_mcp rca/neptune profiles shared dr-plan-generator/graph)" ]]; then
    echo "✗ 打包涉及的目录有未提交改动。线上必须对应一个可追溯的提交。" >&2
    exit 1
  fi
  SHA=$(git rev-parse --short HEAD)
  TMP="${TMPDIR:-/tmp}"
  PKG="$TMP/graphdp-mcp-$SHA.zip"
  bash graph_mcp/build_package.sh "$PKG" >/dev/null
  KEY="graphdp-mcp/graphdp-mcp-$SHA-$(date -u +%Y%m%dT%H%M%S).zip"
  aws s3 cp "$PKG" "s3://$BUCKET/$KEY" --only-show-errors
  echo "已上传 $KEY（$(stat -c %s "$PKG") 字节）"
fi

PREV=$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
  --agent-runtime-id "$RUNTIME_ID" \
  --query 'agentRuntimeArtifact.codeConfiguration.code.s3.prefix' --output text)
echo "当前线上: $PREV   （回滚命令: $0 --rollback $PREV）"

aws bedrock-agentcore-control update-agent-runtime --region "$REGION" \
  --agent-runtime-id "$RUNTIME_ID" \
  --role-arn "$ROLE" \
  --agent-runtime-artifact "{\"codeConfiguration\":{\"code\":{\"s3\":{\"bucket\":\"$BUCKET\",\"prefix\":\"$KEY\"}},\"runtime\":\"PYTHON_3_12\",\"entryPoint\":[\"main.py\"]}}" \
  --protocol-configuration '{"serverProtocol":"MCP"}' \
  --network-configuration '{"networkMode":"VPC","networkModeConfig":{"securityGroups":["sg-0716d6136504616ea"],"subnets":["subnet-0f801fa79077eb277","subnet-047a94f9c5ab6302a"]}}' \
  --authorizer-configuration "{\"customJWTAuthorizer\":{\"discoveryUrl\":\"https://cognito-idp.$REGION.amazonaws.com/$POOL/.well-known/openid-configuration\",\"allowedClients\":[\"$CLIENT\"]}}" \
  --environment-variables "{\"AWS_DEFAULT_REGION\":\"$REGION\",\"LOG_LEVEL\":\"INFO\",\"NEPTUNE_ENDPOINT\":\"$NEPTUNE\",\"REGION\":\"$REGION\"}" \
  --query '{version:agentRuntimeVersion,status:status}' --output json

for _ in $(seq 1 36); do
  S=$(aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
        --agent-runtime-id "$RUNTIME_ID" --query status --output text)
  [[ "$S" == "READY" ]] && break
  [[ "$S" == *FAILED* ]] && { echo "✗ $S —— 回滚: $0 --rollback $PREV" >&2; exit 1; }
  sleep 10
done
echo "Runtime: $S"

# 部署后自检：authorizer 与 VPC 必须还在（全量重传若漏了会在这里暴露）
aws bedrock-agentcore-control get-agent-runtime --region "$REGION" \
  --agent-runtime-id "$RUNTIME_ID" \
  --query '{v:agentRuntimeVersion,auth:authorizerConfiguration.customJWTAuthorizer.allowedClients,net:networkConfiguration.networkMode}' \
  --output json

echo
echo "下一步：PYTHONPATH=.:rca:chaos/code python3.11 -m pytest tests/test_137_mcp_runtime_not_stale.py -q"
