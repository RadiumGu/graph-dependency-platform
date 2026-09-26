#!/usr/bin/env bash
# infra/dr-korea/temporal-1.32/provision-mcp-credential.sh
#
# 把 Cognito app client 的 secret 搬到 Secrets Manager，供韩国的 DR worker 读取。
#
#       bash provision-mcp-credential.sh --dry-run
#       bash provision-mcp-credential.sh --apply
#       bash provision-mcp-credential.sh --verify     # 只验，不改
#
# ## 澄清一个容易混的点
#
# 这里有两个都叫「secret」的东西，它们不是一回事：
#
#   ① Cognito app client secret
#      —— Cognito 在创建 graphdp-mcp-m2m 这个 app client 时**自动生成**的。
#         它早就存在（实测 describe-user-pool-client 的 HasSecret=true）。
#         只能读，不能造。
#
#   ② Secrets Manager 里的 secret
#      —— 用来**存放** ① 的容器，让 worker 能用实例角色读到它。
#         这个才是本脚本要创建的。
#
# 所以本脚本做的是「从 ① 读出来放进 ②」，不是凭空造一个凭证。
#
# ## 为什么不让人手工复制粘贴
#
# 那个值一旦经过屏幕，就会同时留在：终端回滚缓冲、shell 历史、
# 以及（若走 --secret-string '…'）进程 argv —— 而 argv 对同机所有用户
# 通过 `ps` 可见，哪怕只有一瞬。
#
# 本脚本让它只经过一个 umask 077 的临时文件，用完 shred 掉。
# 全程不 echo，不进 argv，不进历史。
#
# ## 为什么 secret 放韩国而不是东京
#
# worker 在韩国。凭证放同区域意味着读它不依赖东京。
# Cognito 与 MCP runtime 本身在东京，但快照只在东京健康时跑 ——
# 那层依赖是固有的；把凭证也放东京是白添一层。

set -euo pipefail

# ── 实测确认的坐标（2026-09-26）──────────────────────────────────────────
POOL_REGION=ap-northeast-1
POOL_ID=ap-northeast-1_Dwd1wVX7j          # graphdp-mcp-pool
CLIENT_ID=2u5s7r3gprc8mo86890sdi1h7t      # graphdp-mcp-m2m，flows=[client_credentials]
SECRET_REGION=ap-northeast-2              # 韩国，与 worker 同区
SECRET_NAME="${DR_GRAPH_MCP_SECRET_NAME:-dr-graph-mcp-m2m}"
SCOPE=graphdp-mcp/invoke
COGNITO_DOMAIN=graphdp-mcp-1788589178

MODE="${1:-}"
case "$MODE" in
  --dry-run|--apply|--verify) : ;;
  *) echo "用法: $0 --dry-run | --apply | --verify" >&2; exit 2 ;;
esac

say() { printf '\n\033[1m── %s ──\033[0m\n' "$1"; }

# ── 现状 ────────────────────────────────────────────────────────────────
say "现状"
printf '  Cognito 池   %s\n' "$POOL_ID"
HAS=$(aws cognito-idp describe-user-pool-client --region "$POOL_REGION" \
        --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID" \
        --query 'UserPoolClient.{n:ClientName,f:AllowedOAuthFlows,s:ClientSecret!=null}' \
        --output json)
echo "  app client   $HAS"
if aws secretsmanager describe-secret --region "$SECRET_REGION" --secret-id "$SECRET_NAME" \
     --query 'ARN' --output text 2>/dev/null; then
  EXISTS=1; echo "  ↑ Secrets Manager 里已存在该 secret"
else
  EXISTS=0; echo "  Secrets Manager  尚无 $SECRET_NAME"
fi

if [ "$MODE" = --dry-run ]; then
  say "将要做的事"
  cat <<EOF
  1. 从 Cognito 读 app client 的 client_id 与 client_secret，
     写入一个 umask 077 的临时文件，整形为：
         {"client_id": "...", "client_secret": "..."}
     （键名必须正好是这两个 —— graph_mcp_client.py 按这两个键取）
  2. $([ "$EXISTS" = 1 ] && echo "put-secret-value 更新已存在的" || echo "create-secret 新建") \
$SECRET_NAME @ $SECRET_REGION
  3. shred 掉临时文件
  4. 用它真的换一次 token 做验证（这是唯一的判据）

  值全程不经过屏幕、不进 argv、不进 shell 历史。
EOF
  exit 0
fi

# ── 搬运 ────────────────────────────────────────────────────────────────
if [ "$MODE" = --apply ]; then
  say "搬运凭证"
  OLD_UMASK=$(umask); umask 077
  TMP=$(mktemp "${TMPDIR:-/tmp}/mcpcred.XXXXXX")
  # 出口即销毁：脚本中途失败也不留下明文凭证。
  trap 'shred -u "$TMP" 2>/dev/null || rm -f "$TMP"; umask "$OLD_UMASK"' EXIT

  # JMESPath 直接整形成目标键名 —— 少一步手工改写就少一处打错的机会。
  aws cognito-idp describe-user-pool-client --region "$POOL_REGION" \
    --user-pool-id "$POOL_ID" --client-id "$CLIENT_ID" \
    --query 'UserPoolClient.{client_id:ClientId,client_secret:ClientSecret}' \
    --output json > "$TMP"

  # 只核对形状，不打印内容。
  python3 - "$TMP" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
missing = [k for k in ("client_id", "client_secret") if not d.get(k)]
if missing:
    sys.exit(f"  ✗ 从 Cognito 取回的 JSON 缺 {missing} —— 该 app client 可能没有 secret")
print(f"  ✓ 形状正确：client_id {len(d['client_id'])} 字符，"
      f"client_secret {len(d['client_secret'])} 字符（值未打印）")
PY

  # file:// 而不是把值放进命令行 —— argv 通过 ps 对同机可见。
  if [ "$EXISTS" = 1 ]; then
    aws secretsmanager put-secret-value --region "$SECRET_REGION" \
      --secret-id "$SECRET_NAME" --secret-string "file://$TMP" \
      --query 'VersionId' --output text | sed 's/^/  已更新，新版本 /'
  else
    aws secretsmanager create-secret --region "$SECRET_REGION" \
      --name "$SECRET_NAME" \
      --description "Cognito m2m client for graph_dependency_mcp ($SCOPE)" \
      --secret-string "file://$TMP" \
      --query 'ARN' --output text | sed 's/^/  已创建 /'
  fi
fi

# ── 验证：唯一的判据是真的换到一个 token ────────────────────────────────
say "验证"
# ⚠️ 「secret 建好了」不是判据 —— 值可能是空的、可能整形错了、
# scope 可能与 resource server 不符。这三种都会让 secret 看起来正常
# 而 worker 在半夜失败。唯一的判据是真的换一次 token。
OLD_UMASK=$(umask); umask 077
T=$(mktemp "${TMPDIR:-/tmp}/mcptok.XXXXXX")
trap 'shred -u "$T" 2>/dev/null || rm -f "$T"; umask "$OLD_UMASK"' EXIT
aws secretsmanager get-secret-value --region "$SECRET_REGION" --secret-id "$SECRET_NAME" \
  --query 'SecretString' --output text > "$T" 2>/dev/null || {
    echo "  ✗ 读不到 secret（还没建？或当前身份无 GetSecretValue）"; exit 1; }

CODE=$(python3 - "$T" "$COGNITO_DOMAIN" "$POOL_REGION" "$SCOPE" <<'PY'
import base64, json, sys, urllib.parse, urllib.request
cred = json.load(open(sys.argv[1]))
domain, region, scope = sys.argv[2], sys.argv[3], sys.argv[4]
url = f"https://{domain}.auth.{region}.amazoncognito.com/oauth2/token"
body = urllib.parse.urlencode({"grant_type": "client_credentials", "scope": scope}).encode()
basic = base64.b64encode(f"{cred['client_id']}:{cred['client_secret']}".encode()).decode()
req = urllib.request.Request(url, data=body, method="POST", headers={
    "Content-Type": "application/x-www-form-urlencoded",
    "Authorization": f"Basic {basic}"})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        d = json.loads(r.read())
    # 只报长度与有效期，不打印 token 本身
    print(f"OK {len(d.get('access_token',''))} {d.get('expires_in')}")
except urllib.error.HTTPError as e:
    print(f"HTTP{e.code} {e.read().decode(errors='replace')[:160]}")
except Exception as e:
    print(f"ERR {type(e).__name__} {e}")
PY
)
case "$CODE" in
  OK\ *) set -- $CODE
    echo "  ✓ 换到 access token（$2 字符，有效期 $3 秒，值未打印）"
    echo "  ✓ 凭证、scope、resource server 三者一致 —— 这是真判据" ;;
  HTTP400*) echo "  ✗ $CODE"
    echo "    invalid_client -> secret 不对；invalid_scope -> scope 与 resource server 不符" ; exit 1 ;;
  *) echo "  ✗ $CODE"; exit 1 ;;
esac

cat <<EOF

── 下一步 ──
  1. 授权（B 方案唯一需要的新权限；**不需要** InvokeAgentRuntime，
     因为 JWT 路径由 Bearer token 授权而非 SigV4）：
         bash iam-grants.sh --apply mcp-secret-read

  2. dr-worker.service 里加这几项：
         DR_GRAPH_MCP_RUNTIME_ARN=arn:aws:bedrock-agentcore:ap-northeast-1:<账号>:runtime/graph_dependency_mcp-12Vg2Z9XXu
         DR_GRAPH_MCP_COGNITO_DOMAIN=$COGNITO_DOMAIN
         DR_GRAPH_MCP_SECRET_ID=$SECRET_NAME
         DR_GRAPH_MCP_SCOPE=$SCOPE
         DR_GRAPH_CONTRACT_VERSION=1

  3. 拿真 token 打一次 tools/list，**看清真实响应形状再写快照 activity**。
     响应形状我只是按 MCP 规范推断的，没见过真的 —— 照推断写完再去碰，
     就是在赌。
EOF
exit 0
