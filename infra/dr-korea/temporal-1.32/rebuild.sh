#!/usr/bin/env bash
# infra/dr-korea/temporal-1.32/rebuild.sh
#
# 把韩国灾备站点的 Temporal 从 auto-setup:1.29.7 重建为 server:1.32.0。
#
#   在 i-09380e417a0177ed4（dr-korea-temporal，ap-northeast-2）上执行：
#       cd /opt/temporal
#       sudo bash rebuild.sh
#
# ⚠️ 这个脚本会**删除 /opt/temporal/pgdata**，即清空 Temporal 的全部
# workflow 历史、namespace 配置与 Schedule。执行前它会把当前状态打印出来
# 并要求输入 REBUILD 确认 —— 不要用 `yes |` 之类绕过那一步。
#
# 已确认可丢弃（2026-09-26 09:45 实测）：
#   · workflow 执行：0 条（dr-rebuild-1790322231 已被 24h 保留期清掉）
#   · 自定义 Search Attribute：0 个（worker 启动时会自动补齐）
#   · Schedule：1 个 dr-graph-snapshot（暂停态，本脚本会还原）
#   · namespace 保留期 720h（本脚本会还原）
#
# 回滚点：EBS 快照 snap-0566ddf3da1b75996（根卷 vol-05fc30731946934cd）
# 旧 compose：/opt/temporal/docker-compose.yml.bak-1.29.7

set -euo pipefail

cd /opt/temporal

RETENTION="720h"          # 30 天。24h 太短：一次演练的证据不到一天就蒸发。
NS="default"

say() { printf '\n\033[1m── %s ──\033[0m\n' "$1"; }

# ── server 镜像里**没有 temporal CLI** ─────────────────────────────────
#
# 2026-09-26 实测：server:1.32.0 里只有 temporal-server 与 dockerize。
# 所以任何 `docker compose exec temporal temporal …` 都会失败 ——
# 而用它做健康探测的后果是**探测永远超时**，看起来像服务端没起来，
# 实际服务端是好的。这类「探针自己坏了、却被读成被测对象坏了」
# 是本项目反复踩过的坑。
#
# CLI 走 admin-tools 容器，网络名**从实际容器推导**而不是假设成
# `temporal_default` —— compose 项目名跟目录名走，写死会在换目录后静默失效。
CLI_IMAGE=temporalio/admin-tools:1.32.0

net_of_temporal() {
  cid=$(docker compose ps -q temporal) || return 1
  [ -n "$cid" ] || return 1
  docker inspect "$cid" \
    --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' \
    | head -1
}

# 在 Temporal 网络里跑一条 temporal CLI 命令。
tcli() {
  net=$(net_of_temporal) || { echo "拿不到 temporal 容器的网络" >&2; return 1; }
  docker run --rm --network "$net" "$CLI_IMAGE" \
    temporal --address temporal:7233 "$@"
}

# ── 0. 前置检查：该有的文件都在 ────────────────────────────────────────
say "0. 前置检查"
for f in docker-compose.yml.new setup-schema.sh start-temporal.sh \
         dynamicconfig/docker.yaml bin/dockerize \
         tmpl/config_template_1.32.0.yaml .env; do
  [ -e "$f" ] || { echo "缺少 $f —— 先按 README 把文件备齐再跑" >&2; exit 1; }
done
docker compose -f docker-compose.yml.new config >/dev/null
echo "文件齐备，compose 语法通过。"

# ── 1. 现状存证 ────────────────────────────────────────────────────────
say "1. 重建前现状（存证，便于事后对账）"
# ⚠️ 这一步刻意用 `docker exec temporal-temporal-1 temporal …` —— 它打的是
# **还没被替换掉的 auto-setup 容器**，那个镜像里有 temporal CLI。
# 不要把它「顺手改成」后面用的 tcli：tcli 依赖新栈已经起来，而这里新栈还没起。
STAMP=$(date -u +%Y%m%d-%H%M%S)
mkdir -p "pre-rebuild-$STAMP"
{
  echo "=== 时间 (UTC) ==="; date -u
  echo; echo "=== 容器 ==="; docker ps --format '{{.Names}} {{.Image}} {{.Status}}'
  echo; echo "=== workflow ==="
  docker exec temporal-temporal-1 temporal workflow list --limit 50 2>&1 || true
  echo; echo "=== schedule ==="
  docker exec temporal-temporal-1 temporal schedule list 2>&1 || true
  echo; echo "=== search attributes ==="
  docker exec temporal-temporal-1 temporal operator search-attribute list 2>&1 || true
  echo; echo "=== namespace ==="
  docker exec temporal-temporal-1 temporal operator namespace describe "$NS" 2>&1 || true
} | tee "pre-rebuild-$STAMP/state.txt"

docker exec temporal-temporal-1 temporal schedule describe \
  --schedule-id dr-graph-snapshot --output json \
  > "pre-rebuild-$STAMP/schedule-dr-graph-snapshot.json" 2>&1 || \
  echo "（没有 dr-graph-snapshot，或导出失败 —— 继续）"

say "确认"
cat <<'WARN'
接下来会：
  1) 停掉当前 Temporal 栈
  2) **删除 /opt/temporal/pgdata**（清空 workflow 历史 / namespace / Schedule）
  3) 以 server:1.32.0 重建，并显式跑 schema 初始化
  4) 还原 namespace 保留期 720h 与 dr-graph-snapshot（暂停态）

回滚：EBS 快照 snap-0566ddf3da1b75996
WARN
read -r -p '输入 REBUILD 继续，其它任何输入取消： ' ans
[ "$ans" = "REBUILD" ] || { echo "已取消，未做任何改动。"; exit 0; }

# ── 2. 停栈并清库 ──────────────────────────────────────────────────────
say "2. 停栈"
docker compose down --remove-orphans

say "3. 清空 pgdata（先改名保留一份，磁盘够的话这份是第二道保险）"
if [ -d pgdata ]; then
  mv pgdata "pgdata.old-$STAMP"
  echo "旧数据目录留在 pgdata.old-$STAMP —— 确认新栈健康后再删。"
fi
mkdir -p pgdata

# ── 3. 切换 compose 并启动 ─────────────────────────────────────────────
say "4. 切换 compose 到 1.32"
cp docker-compose.yml "docker-compose.yml.bak-$STAMP" 2>/dev/null || true
mv docker-compose.yml.new docker-compose.yml

# server:1.32.0 以 uid 1000 运行，而镜像里 /etc/temporal 属 root。
# 渲染目标目录必须由宿主机提供且可写，否则容器进 crash loop，
# 日志里只有一句 Permission denied（2026-09-26 实测踩过）。
say "4b. 备好可写的配置渲染目录（uid 1000）"
mkdir -p config
chown 1000:1000 config
ls -ld config

say "5. 起 postgres 并跑 schema 初始化"
docker compose up -d postgresql
docker compose up temporal-schema          # 前台跑，便于看到 schema 输出
echo "schema 作业退出码：$?"

say "6. 起 server 与 UI"
docker compose up -d temporal temporal-ui

say "7. 等服务端就绪"
for i in $(seq 1 60); do
  if tcli operator cluster health >/dev/null 2>&1; then
    echo "服务端就绪（第 ${i} 次探测）。"; break
  fi
  sleep 3
  if [ "$i" = 60 ]; then
    echo "服务端 180 秒未就绪 —— 看 docker compose logs temporal" >&2
    exit 1
  fi
done

# ── 4. 还原配置 ────────────────────────────────────────────────────────
say "8. 创建 namespace 并设保留期（server 镜像不像 auto-setup 那样自动建）"
tcli operator namespace describe "$NS" >/dev/null 2>&1 \
  || tcli operator namespace create --namespace "$NS" --retention "$RETENTION"
tcli operator namespace update --namespace "$NS" --retention "$RETENTION" 2>&1 | tail -2 || true

say "9. 还原 dr-graph-snapshot（**建成暂停态**）"
# 暂停是刻意的：dr-snapshot-queue 上零 poller，指向无 worker 的队列会堆积
# 永不前进的执行 —— 正是这套东西要防的那个故障。
tcli schedule create \
  --schedule-id dr-graph-snapshot \
  --interval 6h \
  --overlap-policy Skip \
  --paused \
  --workflow-id dr-snapshot \
  --type ExportSnapshotWorkflow \
  --task-queue dr-snapshot-queue 2>&1 | tail -3 || \
  echo "（Schedule 创建失败或已存在 —— 见 pre-rebuild-$STAMP/ 里导出的规格手工比对）"

# ── 5. 验证 ────────────────────────────────────────────────────────────
say "10. 验证"
echo "镜像与服务端版本："
docker inspect "$(docker compose ps -q temporal)" --format '  image={{.Config.Image}}'
tcli operator cluster describe 2>&1 | grep -iE "ServerVersion|ClusterName" | sed 's/^/  /' || true
echo
echo "集群健康："
tcli operator cluster health 2>&1 | head -2 | sed 's/^/  /'
echo
echo "namespace 保留期："
tcli operator namespace describe "$NS" 2>&1 | grep -iE "Retention|State" | sed 's/^/  /'
echo
echo "Schedule："
tcli schedule list 2>&1 | head -5 | sed 's/^/  /'
echo
echo "HTTP API（temporal-mcp 连这个）："
curl -s -o /dev/null -w "  7243 -> HTTP %{http_code}\n" \
  "http://localhost:7243/api/v1/namespaces/$NS/workflows?pageSize=1"
echo "Web UI："
curl -s -o /dev/null -w "  8080 -> HTTP %{http_code}\n" http://localhost:8080/
echo
echo "配置是否真的生效（TEMPORAL_ENVIRONMENT=docker 才会读渲染出的 docker.yaml）："
docker compose exec -T temporal sh -c 'echo "  TEMPORAL_ENVIRONMENT=$TEMPORAL_ENVIRONMENT"' || true
echo "  渲染出的配置：$(wc -l < config/docker.yaml 2>/dev/null || echo 0) 行"
echo
echo "WCI（dynamic config 是否被读到）："
docker compose logs temporal 2>&1 \
  | grep -iE "workercontroller|worker.controller|dynamic.?config" | tail -5 | sed 's/^/  /' \
  || echo "  日志里没匹配到 —— 不代表没生效，但值得再查一次"
echo
echo "启动后的 error 级日志条数："
# ⚠️ `grep -c` 在**数到 0 条时返回 1**。这里的 0 是好消息，不是失败 ——
# 所以要 `|| true`，否则 set -e 会让整个脚本在验证通过时以非零退出，
# 把一次成功的重建报成失败。
ERRS=$(docker compose logs temporal 2>&1 | grep -ciE '"level":"error"|level=error' || true)
echo "  $ERRS 条"

cat <<EOF

── 完成 ──

剩下的两步不在本脚本里，因为它们各自需要一次决定：

  1) 部署新的 worker 代码（分支 feat/dr-plan-review-lifecycle）。
     现在跑的 worker 是旧代码，不认识 DrPlanWorkflow —— 用 MCP 起它会得到
     「started:true / RUNNING 但永不前进」。拉取 + 重启 dr-worker 后，
     worker 启动时会把 8 个 Search Attribute 一并注册上。

  2) serverless worker 还缺：服务端到 AgentCore 控制面/数据面的网络、
     实例角色的 sts:AssumeRole、以及官方模板建的 invocation role。
     WCI 开着只是把服务端侧的门打开。

存证目录：/opt/temporal/pre-rebuild-$STAMP/
旧数据：  /opt/temporal/pgdata.old-${STAMP}（确认健康后再删）
回滚：    EBS 快照 snap-0566ddf3da1b75996
EOF

# 显式成功退出。上面最后几条验证里有 grep，而 grep 数到 0 条时返回 1 ——
# 那个 0 是好消息。不写这一行，一次通过的重建会以非零码结束。
exit 0
