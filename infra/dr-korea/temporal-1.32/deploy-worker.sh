#!/usr/bin/env bash
# infra/dr-korea/temporal-1.32/deploy-worker.sh
#
# 把 dr-plan-generator 的 worker 代码部署到韩国灾备站点，并**用独立手段
# 证明新代码真的在跑**。
#
#   ① 在**操作者机器**上发布（要有写 S3 的权限，比如你的 Mac / 这台工作机）：
#       bash deploy-worker.sh --publish
#   ② 在**主机**上部署（经 SSM，用实例角色）：
#       sudo bash deploy-worker.sh --provision            # 默认带冒烟验证
#       sudo bash deploy-worker.sh --provision --no-smoke
#       sudo bash deploy-worker.sh --provision --ref main
#
# ## 为什么必须拆成两步 —— 这是一条该保住的安全属性
#
# 2026-09-26 第一版把发布和部署写在一起、都在主机上跑，结果 `aws s3 sync`
# 全部 AccessDenied。当时的诱惑是「给实例角色加个 PutObject 就好了」。
# **不要那样做。** 实例角色对 worker/* 刻意只有 GetObject，含义是：
#
#     worker 不能改写自己的代码来源。
#
# 这条约束正是这套灾备编排存在的理由之一 —— 一个能改写自己下次要执行什么的
# 进程，它的「已审核、已演练」结论一文不值。给它 PutObject 会让整条
# 审计链在最不显眼的地方失效：计划审批、演练闸门全都还在，而被执行的代码
# 可以在两次审批之间被执行者自己换掉。
#
# 所以发布权限属于操作者，不属于 worker。这不是不便，是设计。
#
# ## 为什么不直接 systemctl restart 就算完
#
# 仓库里已有 provision-worker.sh，它做得对：S3 是唯一代码来源，按 `.py` 的
# **内容指纹**决定是否重启（不用 mtime，因为 sync 会重写 mtime 而内容可能没变）。
# 它的注释里记着 2026-09-25 踩到的坑：
#
#     磁盘上的 md5 与本地一致   ✅  看起来部署成功了
#     worker 在队列上接单       ✅  核实判据也通过了
#     实际跑的还是旧代码        ❌
#
# 也就是说「worker 已在队列上接单」这个判据**分不出新旧代码**。
# 本脚本补的正是那一环：起一条真的 DrPlanWorkflow 并查它的状态。
# 旧 worker 不认识这个 workflow type —— 它会让执行停在 RUNNING 且永不前进，
# 而**那个形态与「队列名拼错」「没有 worker」完全一样**。
# 所以冒烟验证是三态的：成功 / 明确失败 / 无法判断，绝不把第三种写成第一种。
#
# ## 便携性
#
# 发布这一步会在 macOS 上跑，那里的 /bin/bash 是 3.2。
# 紧跟全角字符的变量引用**必须加花括号**：bash 3.2 会把全角字节算进变量名，
# 配合 `set -u` 直接退出。2026-09-26 实测就是它让第一次 apply 停在
# 安全组那一步之前（路由已加、SG 未加）。已全仓修正，改动时别退回去。

set -euo pipefail

REPO=RadiumGu/graph-dependency-platform
# 默认 main：feat/dr-plan-review-lifecycle 已于 2026-09-26 合入 main，
# 继续指向它会发布一份落后的代码 —— 而那份代码看起来发布成功了。
REF="${DR_WORKER_REF:-main}"
RAW_BASE=""            # 见下，取决于 REF
NS=default
QUEUE=dr-plan-queue
CLI_IMAGE=temporalio/admin-tools:1.32.0
SMOKE=1
MODE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --publish)   MODE=publish; shift ;;
    --provision) MODE=provision; shift ;;
    --no-smoke)  SMOKE=0; shift ;;
    --ref)       REF="$2"; shift 2 ;;
    *) echo "未知参数 $1" >&2; exit 2 ;;
  esac
done
RAW_BASE="https://raw.githubusercontent.com/$REPO/$REF/dr-plan-generator/worker"

if [ -z "$MODE" ]; then
  cat >&2 <<'USAGE'
必须指明模式 —— 这两步在不同的机器上、用不同的身份执行，不是同一件事：

  ① 操作者机器（有写 S3 的权限）：
       bash deploy-worker.sh --publish

  ② worker 主机（用实例角色，经 SSM）：
       sudo bash deploy-worker.sh --provision

为什么不能合成一步：实例角色对 worker/* 刻意只有 GetObject —— worker 不能
改写自己的代码来源。合成一步就会诱使人给它加 PutObject，那会让「已审核、
已演练」的结论失效：被执行的代码可以在两次审批之间被执行者自己换掉。
USAGE
  exit 2
fi

say()  { printf '\n\033[1m── %s ──\033[0m\n' "$1"; }
fail() { echo "  ✗ $*" >&2; FAILED=1; }
ok()   { echo "  ✓ $*"; }
unk()  { echo "  ? $* （无法判断 —— 不当成通过）"; INCONCLUSIVE=1; }
FAILED=0
INCONCLUSIVE=0

# temporal CLI 走 admin-tools 容器：server:1.32.0 镜像里**没有 temporal CLI**
# （只有 temporal-server 与 dockerize），用 `docker compose exec temporal temporal …`
# 的后果是探测永远超时、看起来服务端坏了而其实是好的。
# 网络名从实际容器推导，不写死 temporal_default（compose 项目名跟目录名走）。
tcli() {
  cid=$(cd /opt/temporal && docker compose ps -q temporal)
  net=$(docker inspect "$cid" \
        --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' | head -1)
  docker run --rm --network "$net" "$CLI_IMAGE" \
    temporal --address temporal:7233 --namespace "$NS" "$@"
}

# ── 0. 前置 ────────────────────────────────────────────────────────────
say "0. 前置检查（模式：${MODE}）"
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/dr-worker-stage.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT

if [ "$MODE" = publish ]; then
  # 发布只需要写 S3 的权限，不需要主机上的任何东西。
  BUCKET="${DR_CODE_BUCKET:?发布模式需要显式设 DR_CODE_BUCKET（避免误发到别的环境）}"
  echo "  分支/引用：${REF}"
  echo "  代码桶：  ${BUCKET}"
  # JMESPath 不支持字符串拼接（"x" + Arn 会报 Unknown token +）。
  echo "  身份：   $(aws sts get-caller-identity --query Arn --output text)"
else
  [ -f /opt/dr-worker/app/worker.py ] || {
    echo "/opt/dr-worker 不存在 —— 这是首次部署，请先跑 provision-worker.sh" >&2; exit 1; }
  BUCKET="${DR_CODE_BUCKET:-$(systemctl show -p Environment dr-worker.service \
    | tr ' ' '\n' | sed -n 's/^DR_PLAN_BUCKET=//p' | head -1)}"
  [ -n "$BUCKET" ] || { echo "拿不到代码桶名，请设 DR_CODE_BUCKET" >&2; exit 1; }
  echo "  代码桶：  ${BUCKET}"
  tcli operator cluster health >/dev/null 2>&1 \
    && ok "Temporal 可达" \
    || { echo "  Temporal 不可达 —— worker 起来也连不上，先修服务端" >&2; exit 1; }
fi

# ══ 发布模式 ═══════════════════════════════════════════════════════════
if [ "$MODE" = publish ]; then

say "1. 从 GitHub 取 worker 代码"
# ⚠️ dr-worker.service 也必须随包下发。
# provision-worker.sh 第 120 行读的是 $APP/dr-worker.service ——
# 主机上那份是 2026-09-24 手工装的，而 `aws s3 sync` 不带 --delete，
# 所以它一直躺在那里不被更新：改了仓库里的环境变量，主机上一点变化都没有。
# 这是本工作线第三次同类缺陷（前两次是 provision-worker.sh 自己、
# 以及 graph_mcp_client.py / probe_graph_mcp.py）。
#
# provision-worker.sh 必须**随包下发**。
# 它不在这个清单里会造成一个恰好最难看出来的故障：代码同步到了 S3，
# 但没人把它从 S3 拉到 /opt/dr-worker/app，于是 worker 用旧代码重启 ——
# 而「服务 active」「队列上有 poller」两个判据照样通过。
FILES="worker.py workflows.py plan_workflow.py activities.py snapshot_workflow.py graph_mcp_client.py probe_graph_mcp.py requirements.txt provision-worker.sh dr-worker.service"
for f in $FILES; do
  curl -fsSL -o "$STAGE/$f" "$RAW_BASE/$f"
  printf '  %-24s %6s 字节\n' "$f" "$(stat -c%s "$STAGE/$f")"
done
# 语法先过一遍再上传。上传一份语法错的代码，worker 会进重启循环，
# 而那时错误只在 journal 里 —— 在这里失败便宜得多。
for f in $STAGE/*.py; do
  python3.12 -m py_compile "$f" || { echo "  $f 语法错误，中止" >&2; exit 1; }
done
ok "$(ls "$STAGE"/*.py | wc -l) 个 .py 语法检查通过"

# ── 2. 上传并 provision ────────────────────────────────────────────────
say "2. 同步到 S3（S3 是唯一代码来源，保持既有契约）"
aws s3 sync "$STAGE/" "s3://$BUCKET/worker/" --only-show-errors \
  --exclude '__pycache__/*' --exclude '*.pyc'
ok "已同步到 s3://$BUCKET/worker/"
cat <<EOF

发布完成。下一步在 worker 主机上（经 SSM，用实例角色）执行：

    sudo bash deploy-worker.sh --provision

主机侧刻意没有写 S3 的权限 —— 见脚本头部「这是一条该保住的安全属性」。
EOF
exit 0
fi   # ══ 发布模式结束 ═══════════════════════════════════════════════════

# ══ 部署模式 ═══════════════════════════════════════════════════════════
say "3. 跑 provision-worker.sh（它按内容指纹决定是否重启）"
# provision-worker.sh 从 **S3** 取，而不是从 /opt/dr-worker/app ——
# 主机上那份可能还是旧的，甚至可能不存在（它最初是手工装的，不在 IaC 里）。
# 而把代码从 S3 落到磁盘这件事本身就是它做的，所以不能依赖磁盘上那份。
aws s3 cp "s3://$BUCKET/worker/provision-worker.sh" "$STAGE/provision-worker.sh" --only-show-errors
FP_BEFORE=$(cat /opt/dr-worker/.code.sha256 2>/dev/null || echo "none")
PID_BEFORE=$(systemctl show -p MainPID --value dr-worker.service || echo 0)
# 不加 || true：provision 失败就必须响亮地失败。
# 这里退化成 `systemctl restart` 是最坏的做法 —— 代码还在 S3 里没落地，
# 而 worker 会带着旧代码干净地重启，看起来一切正常。
DR_CODE_BUCKET="$BUCKET" bash "$STAGE/provision-worker.sh"
FP_AFTER=$(cat /opt/dr-worker/.code.sha256 2>/dev/null || echo "none")
PID_AFTER=$(systemctl show -p MainPID --value dr-worker.service || echo 0)
echo "  代码指纹：${FP_BEFORE:0:8}… → ${FP_AFTER:0:8}…"

if [ "$FP_BEFORE" = "$FP_AFTER" ]; then
  echo "  · 代码内容无变化，provision 按设计没有重启 —— 这不是问题。"
  echo "    下面的验证仍然照跑：它查的是**现在跑着什么**，与本次是否重启无关。"
elif [ "$PID_BEFORE" != "$PID_AFTER" ]; then
  ok "代码有变且进程已更换（PID $PID_BEFORE → ${PID_AFTER}）"
else
  fail "代码指纹变了但 PID 没变（${PID_AFTER}）—— 进程还拿着旧模块，这正是 2026-09-25 那个坑"
fi

# ── 3. 部署面验证 ──────────────────────────────────────────────────────
say "4. 部署面验证"
systemctl is-active --quiet dr-worker.service \
  && ok "dr-worker.service active" || fail "dr-worker.service 未运行"

# 8 个自定义属性由 worker 启动时的 ensure_search_attributes() 保证。
# 重建后的新库里一个自定义属性都没有 —— 所以这一项同时验证了
# 「新代码在跑」（旧代码只认 5 个）与「启动前置条件生效」。
SA=$(tcli operator search-attribute list 2>/dev/null | grep -cE '^\s+DR' || true)
case "$SA" in
  8) ok "8 个 DR* Search Attribute 齐备（含新增的 DRPlanId/DRPlanVersion/DRPlanState）" ;;
  5) fail "只有 5 个 DR* 属性 —— 跑的还是旧代码" ;;
  0) fail "0 个 DR* 属性 —— ensure_search_attributes 没跑，workflow 会在 upsert 时静默卡死" ;;
  *) unk "DR* 属性 $SA 个（既不是旧的 5 也不是新的 8）" ;;
esac

POLLERS=$(tcli task-queue describe --task-queue "$QUEUE" 2>/dev/null \
          | grep -ci "@" || true)
[ "$POLLERS" -ge 1 ] && ok "$QUEUE 上有 $POLLERS 个 poller" \
  || unk "$QUEUE 查不到 poller —— 以下三种无法区分：队列名拼错 / 无 worker / 有 workflow 在等但无 worker"

# ── 4. 冒烟：证明 DrPlanWorkflow 真的被注册了 ──────────────────────────
if [ "$SMOKE" = 1 ]; then
  say "5. 冒烟验证（起一条真的 DrPlanWorkflow）"
  # ⚠️ 这一步是整个脚本存在的理由。
  # 「队列上有 poller」分不出新旧代码；旧 worker 遇到未注册的 workflow type
  # 会让执行停在 RUNNING 且永不前进 —— 与「没有 worker」形态相同。
  # 只有真的起一条、并查到它的状态，才能把这三者分开。
  PLAN_ID="smoke/$(date -u +%Y%m%d-%H%M%S)"
  WF_ID="plan-smoke-$(date -u +%H%M%S)"
  INPUT=$(printf '{"plan_id":"%s","body":"# 部署冒烟\\n仅用于验证 DrPlanWorkflow 已注册。\\n","author":"deploy-worker.sh","reason":"部署后冒烟","failover_task_queue":"%s"}' "$PLAN_ID" "$QUEUE")

  if tcli workflow start --type DrPlanWorkflow --task-queue "$QUEUE" \
        --workflow-id "$WF_ID" --input "$INPUT" >/dev/null 2>&1; then
    STATE=""
    for i in $(seq 1 20); do
      STATE=$(tcli workflow query --workflow-id "$WF_ID" --type plan_state 2>/dev/null \
              | grep -o '"state"[^,]*' | head -1 || true)
      [ -n "$STATE" ] && break
      sleep 3
    done
    if [ -n "$STATE" ]; then
      ok "DrPlanWorkflow 已注册且可查询：$STATE"
      # 收口，不留一条 RUNNING 的冒烟执行在那里。
      tcli workflow update --workflow-id "$WF_ID" --name close_plan \
        --input '{"author":"deploy-worker.sh","reason":"冒烟结束"}' >/dev/null 2>&1 \
        || tcli workflow terminate --workflow-id "$WF_ID" --reason "冒烟结束" >/dev/null 2>&1 || true
      ok "冒烟执行已收口"
      echo "  留下的 S3 对象：s3://$BUCKET/plans/$PLAN_ID/v1.md"
      echo "  （计划版本按设计不可覆盖，所以它不会被清掉 —— plan_id 带 smoke/ 前缀便于批量清理）"
    else
      fail "起了执行但 20×3 秒内查不到状态 —— worker 很可能不认识 DrPlanWorkflow（跑的是旧代码）"
      echo "    查：tcli workflow describe --workflow-id $WF_ID"
    fi
  else
    fail "起 DrPlanWorkflow 失败 —— 看上面的报错"
  fi
else
  echo "  （--no-smoke：跳过。注意跳过它就等于放弃了唯一能分出新旧代码的判据）"
fi

# ── 5. 结论 ────────────────────────────────────────────────────────────
say "结论"
if [ "$FAILED" = 1 ]; then
  echo "有明确失败项 —— 不要当成部署成功。日志：journalctl -u dr-worker -n 80 --no-pager"
  exit 1
fi
if [ "$INCONCLUSIVE" = 1 ]; then
  echo "没有失败，但有**无法判断**的项 —— 按本项目纪律，这不等于通过。"
  echo "请人工核实上面标 ? 的那几条再宣布完成。"
  exit 2
fi
echo "全部通过：新 worker 在跑，DrPlanWorkflow 可用。"
echo
echo "下一步（不在本脚本里，各自需要一次决定）："
echo "  · 快照 Schedule dr-graph-snapshot 仍是暂停态：dr-snapshot-queue 上零 poller。"
echo "    启用前要么给它一个 worker，要么让它保持暂停 —— 指向无 worker 的队列"
echo "    会堆积永不前进的执行，正是这套东西要防的故障。"
echo "  · 快照 workflow 要读东京 Neptune，需要网络放通（见 network-korea-to-neptune.sh）。"
exit 0
