#!/bin/bash
# PetSite 跨 VPC 流量生成器 —— EC2 初始化脚本
#
# 运行位置 : agent-vpc-v2 (10.1.0.0/16) / subnet-057b2d3519422d28b / ap-northeast-1a
#            该子网是 agent-vpc-v2 中唯一带 11.0.0.0/16 对等路由的子网。
# 访问路径 : 容器 -> VPC 对等 -> 内网 ALB petsite-internal-lt -> PetSite Pod
#            全程不经公网。
#
# 关键点:镜像里的 SSM 配置提供程序在运行时读不到 /petstore 下的键(已验证),
#        所以三个配置项全部用环境变量注入,容器因此 **不需要任何 SSM 权限**。
set -euxo pipefail
exec > >(tee -a /var/log/loadgen-init.log) 2>&1

REGION=ap-northeast-1
ACCT=926093770964
REPO=cdk-hnb659fds-container-assets-926093770964-ap-northeast-1
TAG=28111322a93d1fbacfdf11a76a1b8579f400c9d701351b6470404e567ae0bf89
IMG="$ACCT.dkr.ecr.$REGION.amazonaws.com/$REPO:$TAG"
ALB=internal-petsite-internal-lt-1660792065.ap-northeast-1.elb.amazonaws.com

dnf install -y docker
systemctl enable --now docker

# 先确认内网 ALB 两个入口都通,不通就早失败,避免起一堆报错容器
curl -sf --max-time 10 -o /dev/null "http://$ALB/"
curl -sf --max-time 10 -o /dev/null "http://$ALB:8081/health/status"

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "$ACCT.dkr.ecr.$REGION.amazonaws.com"
docker pull "$IMG"
docker tag "$IMG" petsite-trafficgen:local

# 伸缩助手:trafficgen-scale <容器数>
cat > /usr/local/bin/trafficgen-scale <<'SCALE'
#!/bin/bash
# 用法: trafficgen-scale <目标容器数>
set -euo pipefail
TARGET="${1:?用法: trafficgen-scale <容器数>}"
ALB=internal-petsite-internal-lt-1660792065.ap-northeast-1.elb.amazonaws.com
CUR=$(docker ps -q --filter "name=^trafficgen-" | wc -l)
echo "当前 $CUR 个,目标 $TARGET 个"
if [ "$TARGET" -gt "$CUR" ]; then
  for i in $(seq $((CUR+1)) "$TARGET"); do
    docker run -d --restart unless-stopped --name "trafficgen-$i" \
      -e AWS_REGION=ap-northeast-1 \
      -e petsiteurl="http://$ALB" \
      -e searchapiurl="http://$ALB:8081/api/search?" \
      -e trafficdelaytime=1 \
      -e TRAFFIC_GENERATOR_HEADER='X-Traffic-Generator:ec2-crossvpc' \
      petsite-trafficgen:local
  done
else
  for i in $(seq $((TARGET+1)) "$CUR"); do
    docker rm -f "trafficgen-$i" 2>/dev/null || true
  done
fi
docker ps --filter "name=^trafficgen-" --format '  {{.Names}}\t{{.Status}}'
SCALE
chmod +x /usr/local/bin/trafficgen-scale

# 先只起 1 个:每轮会调 /housekeeping/ 与 deletepetadoptionshistory,
# 对 tier0 Aurora 有写入与删除压力,刻意不一次拉高,由人工决定伸缩。
/usr/local/bin/trafficgen-scale 1

echo "初始化完成 $(date -u +%FT%TZ)"
