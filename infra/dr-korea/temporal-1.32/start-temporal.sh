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

mkdir -p /etc/temporal/config
dockerize -template \
  /etc/temporal/config_template.yaml:/etc/temporal/config/docker.yaml

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
