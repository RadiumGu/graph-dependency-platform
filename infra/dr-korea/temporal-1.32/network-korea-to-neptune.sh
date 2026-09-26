#!/usr/bin/env bash
# infra/dr-korea/temporal-1.32/network-korea-to-neptune.sh
#
# 让韩国灾备站点的 worker 能直连东京的 Neptune（图谱快照导出要读它）。
#
#   在能调 AWS 的机器上执行（需要 ec2:CreateRoute 与
#   ec2:AuthorizeSecurityGroupIngress）：
#       bash network-korea-to-neptune.sh --dry-run    # 先看它要改什么
#       bash network-korea-to-neptune.sh --apply
#       bash network-korea-to-neptune.sh --revert     # 回退
#
# ## 为什么此前不通 —— 不是没有对等，是两处配置缺失
#
# 2026-09-26 实测：
#   · 对等 pcx-09fc849e6ac38e7e1（11.0.0.0/16 东京 ↔ 10.20.0.0/16 韩国）**active**
#   · 韩国侧路由表**有** 11.0.0.0/16 的去程
#   · Neptune 的 DNS 从韩国**解析得到** 11.0.2.187
#   · 8182 **不通**
#
# 解析得到而端口不通，说明路径存在、被配置挡住。缺的正好两处：
#
#   ① Neptune 所在两个子网的路由表**没有回程** 10.20.0.0/16
#      （同 VPC 里另外两张表有，Neptune 那两张没有）
#   ② Neptune 安全组 8182 的入站只放了 10.1.0.0/16（openclaw VPC），
#      没有韩国的网段
#
# ## ⚠️ 安全组只放 10.20.1.0/24，不放整个 10.20.0.0/16
#
# 这是刻意收紧，理由具体而不是原则性的：**韩国那个 VPC 里除了 Temporal EC2，
# 还跑着 5 个 AgentCore 运行时**（temporal_mcp 与 4 个 WaggleAI：
# Ordering / Orchestrator / Nutrition / Concierge）。它们没有任何理由能访问
# 生产环境的图数据库 —— 而放 /16 就等于把访问权一并给了它们。
#
# 10.20.1.0/24 是 Temporal EC2（10.20.1.10）所在的子网，也就是 worker 真正
# 跑的地方。将来若把快照 worker 做成 AgentCore serverless worker 落在
# 10.20.0.0/24 / 10.20.2.0/24，再按需补规则 —— 那时补一条，是一次有据可查的
# 授权，而不是一开始就把门开到最大。
#
# ## 这是对**生产 VPC** 的网络变更
#
# 两条路由 + 一条安全组规则，都作用在 PetSite 生产 VPC（vpc-010ab37a3f9f74725）。
# 都是只加不减、可逆。--revert 会原样撤掉这三项。
#
# ## 另一条没选的路，记在这里免得以后重新论证
#
# 也可以反过来：快照导出跑在**东京**、只往 S3 写，韩国只读 S3 —— 那样完全
# 不需要开这个入站路径，依赖方向也更顺（灾备站点消费产物，而不是反向伸进
# 主站点的数据库）。没选它的原因是：那需要在主区域新增计算资源，而这套
# 「守夜灯」设计的目标恰恰是主区域之外尽量少放东西；且本方案能收到 /24。
# 如果哪天觉得「灾备 VPC 常驻一条通往生产图库的入站路径」不可接受，
# 那条路是现成的替代方案。

set -euo pipefail

# ── 实测得到的资源标识（2026-09-26）────────────────────────────────────
TOKYO_REGION=ap-northeast-1
PEERING=pcx-09fc849e6ac38e7e1
KOREA_CIDR=10.20.0.0/16              # 回程路由用整个 VPC：路由不是授权
WORKER_CIDR=10.20.1.0/24             # 安全组只放 worker 所在子网
NEPTUNE_SG=sg-00590f44d50a19e5f
NEPTUNE_PORT=8182
# Neptune 实例所在的两个子网各自的路由表（实测它们没有回程路由，
# 同 VPC 的 rtb-0587fa144c140f9fe / rtb-0804db320bf936c6c 有）
ROUTE_TABLES="rtb-01af777bd633bef7b rtb-0a3b78f0a483c2b5c"
NEPTUNE_HOST=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com
WORKER_INSTANCE=i-09380e417a0177ed4
KOREA_REGION=ap-northeast-2

MODE=""
case "${1:-}" in
  --dry-run) MODE=dry ;;
  --apply)   MODE=apply ;;
  --revert)  MODE=revert ;;
  *) echo "用法: $0 --dry-run | --apply | --revert" >&2; exit 2 ;;
esac

say() { printf '\n\033[1m── %s ──\033[0m\n' "$1"; }

# ── 现状（只读）────────────────────────────────────────────────────────
say "现状"
for rt in $ROUTE_TABLES; do
  have=$(aws ec2 describe-route-tables --region "$TOKYO_REGION" --route-table-ids "$rt" \
    --query "RouteTables[0].Routes[?DestinationCidrBlock=='$KOREA_CIDR'].VpcPeeringConnectionId" \
    --output text)
  printf '  %s  回程 %s -> %s\n' "$rt" "$KOREA_CIDR" "${have:-（无）}"
done
sg_have=$(aws ec2 describe-security-groups --region "$TOKYO_REGION" --group-ids "$NEPTUNE_SG" \
  --query "SecurityGroups[0].IpPermissions[?FromPort==\`$NEPTUNE_PORT\`].IpRanges[].CidrIp" \
  --output text)
echo "  $NEPTUNE_SG 的 $NEPTUNE_PORT 入站 CIDR：${sg_have:-（无）}"

if [ "$MODE" = dry ]; then
  say "将要做的改动（--apply 才执行）"
  for rt in $ROUTE_TABLES; do
    echo "  aws ec2 create-route --region $TOKYO_REGION --route-table-id $rt \\"
    echo "      --destination-cidr-block $KOREA_CIDR --vpc-peering-connection-id $PEERING"
  done
  echo "  aws ec2 authorize-security-group-ingress --region $TOKYO_REGION \\"
  echo "      --group-id $NEPTUNE_SG --ip-permissions \\"
  echo "      'IpProtocol=tcp,FromPort=$NEPTUNE_PORT,ToPort=$NEPTUNE_PORT,IpRanges=[{CidrIp=$WORKER_CIDR,Description=\"DR worker subnet (Korea) -> Neptune 8182\"}]'"
  exit 0
fi

if [ "$MODE" = revert ]; then
  say "回退"
  for rt in $ROUTE_TABLES; do
    aws ec2 delete-route --region "$TOKYO_REGION" --route-table-id "$rt" \
      --destination-cidr-block "$KOREA_CIDR" 2>&1 | tail -1 || echo "  $rt 无该路由"
  done
  aws ec2 revoke-security-group-ingress --region "$TOKYO_REGION" \
    --group-id "$NEPTUNE_SG" --ip-permissions \
    "IpProtocol=tcp,FromPort=$NEPTUNE_PORT,ToPort=$NEPTUNE_PORT,IpRanges=[{CidrIp=$WORKER_CIDR}]" \
    2>&1 | tail -1 || echo "  安全组无该规则"
  echo "已回退。"
  exit 0
fi

# ── 应用 ───────────────────────────────────────────────────────────────
say "1. 加回程路由（Neptune 所在子网的路由表）"
for rt in $ROUTE_TABLES; do
  if aws ec2 create-route --region "$TOKYO_REGION" --route-table-id "$rt" \
       --destination-cidr-block "$KOREA_CIDR" \
       --vpc-peering-connection-id "$PEERING" >/dev/null 2>&1; then
    echo "  ✓ $rt 已加"
  else
    echo "  · $rt 未加（很可能已存在，下面会复核）"
  fi
done

say "2. 放通 Neptune 安全组的 $NEPTUNE_PORT（只放 $WORKER_CIDR）"
if aws ec2 authorize-security-group-ingress --region "$TOKYO_REGION" \
     --group-id "$NEPTUNE_SG" --ip-permissions \
     "IpProtocol=tcp,FromPort=$NEPTUNE_PORT,ToPort=$NEPTUNE_PORT,IpRanges=[{CidrIp=$WORKER_CIDR,Description=DR worker subnet Korea to Neptune}]" \
     >/dev/null 2>&1; then
  echo "  ✓ 已放通"
else
  echo "  · 未加（很可能已存在，下面会复核）"
fi

# ── 验证：规则存在 ≠ 通 ────────────────────────────────────────────────
say "3. 验证"
# ⚠️ 只核对「规则存在」是不够的 —— 本项目已六次遇到「命令成功但没生效」。
# 唯一的判据是**从 worker 真正所在的那台机器**发起一次 TCP 连接。
for rt in $ROUTE_TABLES; do
  have=$(aws ec2 describe-route-tables --region "$TOKYO_REGION" --route-table-ids "$rt" \
    --query "RouteTables[0].Routes[?DestinationCidrBlock=='$KOREA_CIDR'].VpcPeeringConnectionId" \
    --output text)
  [ -n "$have" ] && echo "  ✓ $rt 回程路由在（$have）" || echo "  ✗ $rt 回程路由缺失"
done
sg_now=$(aws ec2 describe-security-groups --region "$TOKYO_REGION" --group-ids "$NEPTUNE_SG" \
  --query "SecurityGroups[0].IpPermissions[?FromPort==\`$NEPTUNE_PORT\`].IpRanges[].CidrIp" \
  --output text)
echo "  $NEPTUNE_PORT 入站现为：$sg_now"
case "$sg_now" in
  *"$WORKER_CIDR"*) echo "  ✓ 含 $WORKER_CIDR" ;;
  *) echo "  ✗ 不含 $WORKER_CIDR" ;;
esac

echo
echo "  从 worker 主机实测连通性（这才是判据）："
cat <<EOF
    在 $WORKER_INSTANCE 上执行：
        timeout 8 bash -c 'cat < /dev/null > /dev/tcp/$NEPTUNE_HOST/$NEPTUNE_PORT' \\
          && echo 通 || echo 不通
    或经 SSM：
        aws ssm send-command --region $KOREA_REGION --instance-ids $WORKER_INSTANCE \\
          --document-name AWS-RunShellScript --parameters \\
          'commands=["timeout 8 bash -c \\"cat < /dev/null > /dev/tcp/$NEPTUNE_HOST/$NEPTUNE_PORT\\" && echo 通 || echo 不通"]'
EOF

cat <<EOF

── 回退 ──
    bash $(basename "$0") --revert

放通之后 Neptune 的 IAM 认证仍然生效 —— 网络通了不等于能查图。
worker 的实例角色还需要 neptune-db:* 的数据面权限，那是单独一件事。
EOF
exit 0
