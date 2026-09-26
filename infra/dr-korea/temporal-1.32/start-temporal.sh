#!/bin/sh
# /opt/temporal/start-temporal.sh
#
# server 镜像的 entrypoint 只有一行 `exec temporal-server start` —— 它不渲染
# 配置。这个脚本补上 auto-setup 以前做的那一步，并且**照抄上游 entrypoint.sh
# 的取值逻辑**，不自己另发明一套。
#
# ⚠️ 广播地址必须在**每次启动时**解析：它是容器的 IP，烘死一个值会在下次
# 重启后导致 membership 对不上 —— 表现为服务起来了但任务不流动，
# 正是本项目最警惕的「启动成功 ≠ 在工作」。
#
# 放成脚本文件而不是写在 compose 的 entrypoint 里，是因为 compose 会对
# `$` 做插值（实测 `docker compose config` 直接报
# "invalid interpolation format"），转义 `$$` 能过但会让这段逻辑变得难读，
# 而这段逻辑恰恰是不该难读的那种。
set -eu

# pipefail 在 alpine 的 sh 里可用；与上游一致。
# shellcheck disable=SC3040
(set -o pipefail 2>/dev/null) && set -o pipefail

self_ip() {
  getent hosts "$(hostname)" | awk '{print $1; exit}'
}

: "${BIND_ON_IP:=$(self_ip)}"
export BIND_ON_IP

if [ "$BIND_ON_IP" = "0.0.0.0" ] || [ "$BIND_ON_IP" = "::0" ]; then
  : "${TEMPORAL_BROADCAST_ADDRESS:=$(self_ip)}"
  export TEMPORAL_BROADCAST_ADDRESS
fi

echo "start-temporal: BIND_ON_IP=$BIND_ON_IP BROADCAST=${TEMPORAL_BROADCAST_ADDRESS:-<unset>}"

mkdir -p /etc/temporal/config 2>/dev/null || true
if [ ! -w /etc/temporal/config ]; then
  cat >&2 <<'ERR'
start-temporal: /etc/temporal/config 不可写，拒绝启动。

原因几乎一定是这个：server:1.32.0 以 uid 1000（temporal）运行，而镜像里
/etc/temporal 属 root —— 容器内建不出这个目录。

修法（compose 里已包含，缺的是宿主机那一步）：
    mkdir -p /opt/temporal/config
    chown 1000:1000 /opt/temporal/config
并确保 compose 有这条卷：
    - /opt/temporal/config:/etc/temporal/config

在这里响亮失败是刻意的：不加这个检查时表现是 crash loop，
而 crash loop 的日志里只有一句 Permission denied，指不出是 uid 不匹配。
ERR
  exit 1
fi

dockerize -template \
  /etc/temporal/config_template.yaml:/etc/temporal/config/docker.yaml

# ⚠️ 渲染出 docker.yaml 不等于服务端会去读它。
# temporal-server 的默认 environment 是 development —— 它读 development.yaml。
# 必须由 compose 传 TEMPORAL_ENVIRONMENT=docker（2026-09-26 实测踩过：
# 不设它服务端照样起来，但跑的不是我们给的配置）。这里核对一次并说清后果。
case "${TEMPORAL_ENVIRONMENT:-development}" in
  docker) : ;;
  *)
    echo "start-temporal: TEMPORAL_ENVIRONMENT=${TEMPORAL_ENVIRONMENT:-development}" \
         "—— 服务端会读 config/\${TEMPORAL_ENVIRONMENT}.yaml，" \
         "而我们渲染的是 config/docker.yaml。拒绝以一套不生效的配置启动。" >&2
    exit 1 ;;
esac

# 渲染结果做一次最低限度的自检：空文件或没渲染到 persistence 段，
# 都应该在这里响亮失败，而不是让服务端带着半个配置起来。
if [ ! -s /etc/temporal/config/docker.yaml ]; then
  echo "start-temporal: 渲染出的配置为空，拒绝启动" >&2
  exit 1
fi
if ! grep -q "^persistence:" /etc/temporal/config/docker.yaml; then
  echo "start-temporal: 渲染出的配置缺 persistence 段，拒绝启动" >&2
  exit 1
fi

exec temporal-server start
