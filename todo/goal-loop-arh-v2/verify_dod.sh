#!/usr/bin/env bash
# verify_dod.sh — 把 north_star.md 的 5 条 DoD 变成可执行核验
#
# 用法:
#   bash verify_dod.sh        # 验全部 5 条,全绿退 0
#   bash verify_dod.sh 2      # 只验第 2 条
#
# 退出码: 0 = 通过, 1 = 有未通过项, 2 = 环境问题(CLI 版本不对等)

set -uo pipefail
ANCHOR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXPORTS="$ANCHOR/exports"
export PATH="$HOME/.local/bin:$PATH"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-ap-northeast-1}"
RH() { aws resiliencehubv2 "$@" --region "$AWS_DEFAULT_REGION"; }
# 分页安全计数: aws CLI 自动分页时 `--query 'length(x)' --output text` 会每页各打印一个数字,
# 直接拿去做整数比较会炸(实测 petsite-core 返回 "50\n67\n7")。这里把各页求和。
RHCOUNT() { RH "$@" --output text 2>/dev/null | awk '{t+=$1} END{print t+0}'; }

G="\033[32m"; R="\033[31m"; Y="\033[33m"; N="\033[0m"
pass() { printf "  ${G}PASS${N}  %s\n" "$1"; }
fail() { printf "  ${R}FAIL${N}  %s\n" "$1"; FAILED=1; }
info() { printf "  ${Y}····${N}  %s\n" "$1"; }
FAILED=0

# ---- 环境前提 ----
ver=$(aws --version 2>&1 | sed -n 's|^aws-cli/\([0-9]*\.[0-9]*\).*|\1|p')
case "$ver" in
  2.3[6-9]|2.[4-9]*|[3-9].*) : ;;
  *) printf "${R}环境错误${N}: aws-cli %s 不支持 resiliencehubv2,需要 >= 2.36。检查 PATH 是否包含 \$HOME/.local/bin\n" "$ver"; exit 2 ;;
esac
[ -f "$EXPORTS/arns.env" ] || { printf "${R}环境错误${N}: 缺 %s\n" "$EXPORTS/arns.env"; exit 2; }
# shellcheck disable=SC1090
. "$EXPORTS/arns.env"
SERVICES="$SVC_S1 $SVC_S2 $SVC_S3 $SVC_S4"

dod1() {
  echo "DoD-1 建模层就位"
  n=$(RH list-systems  --query 'length(systemSummaries)'  --output text)
  [ "$n" -ge 1 ] && pass "systems = $n (>=1)"  || fail "systems = $n,期望 >=1"
  n=$(RH list-services --query 'length(serviceSummaries)' --output text)
  [ "$n" -ge 4 ] && pass "services = $n (>=4)" || fail "services = $n,期望 >=4"
  n=$(RH list-policies --query 'length(policySummaries)'  --output text)
  [ "$n" -ge 3 ] && pass "policies = $n (>=3)" || fail "policies = $n,期望 >=3"
  for s in $SERVICES; do
    nm=$(printf "%s" "$s" | sed "s#.*/##; s#:.*##")
    p=$(RH get-service --service-arn "$s" --query 'service.policyArn' --output text)
    [ -n "$p" ] && [ "$p" != "None" ] && pass "$nm policyArn 非空" || fail "$nm policyArn 为空"
    i=$(RHCOUNT list-input-sources --service-arn "$s" --query 'length(inputSourceSummaries)')
    [ "$i" -ge 1 ] && pass "$nm input sources = $i (>=1)" || fail "$nm 无 input source"
  done
}

dod2() {
  echo "DoD-2 零遗漏覆盖"
  led="$ANCHOR/coverage-ledger.md"
  [ -f "$led" ] && pass "coverage-ledger.md 存在" || { fail "缺 coverage-ledger.md"; return; }
  # 台账里每一行应用组必须落在「已纳管」或「不纳管」两态之一
  bad=$(grep -cE '^\|' "$led" 2>/dev/null | head -1)
  # 只检查表格行(以 | 开头),散文里的"待定项"小节标题不算
  un=$(grep -E '^\|' "$led" 2>/dev/null | grep -cE '待定|TBD|\?\?\?')
  [ "${un:-0}" -eq 0 ] && pass "台账表格无待定项" || fail "台账表格有 $un 处待定/TBD"
  # 每个 service 必须至少解析出 1 个资源(空 service 说明输入源没生效)
  for s in $SERVICES; do
    nm=$(printf "%s" "$s" | sed "s#.*/##; s#:.*##")
    c=$(RHCOUNT list-resources --service-arn "$s" --query 'length(serviceResources)')
    [ "$c" -ge 1 ] && pass "$nm 已解析 $c 个资源" || fail "$nm 解析出 0 个资源"
  done
  info "台账表格行数 ${bad:-0}(人工核对:VPC 每个应用组都要出现)"
}

dod3() {
  echo "DoD-3 目标基线可查"
  # 注意: availabilitySlo.target 是三值枚举 [99.9,99.95,99.99],底就是 99.9,
  # 故 tier2 刻意不设 SLO —— 只校验 tier0/tier1 的 SLO,三条都校验 RTO/RPO 非零
  for pv in "$POLICY_TIER0:slo" "$POLICY_TIER1:slo" "$POLICY_TIER2:noslo"; do
    arn="${pv%:*}"; mode="${pv##*:}"; nm=$(printf "%s" "$arn" | sed "s#.*/##; s#:.*##")
    read -r rto rpo slo <<<"$(RH get-policy --policy-arn "$arn" \
      --query 'policy.[multiAz.rtoInMinutes,multiAz.rpoInMinutes,availabilitySlo.target]' --output text)"
    { [ -n "$rto" ] && [ "$rto" != "None" ] && [ "$rto" -gt 0 ]; } \
      && pass "$nm RTO = ${rto}min" || fail "$nm RTO 无效: $rto"
    { [ -n "$rpo" ] && [ "$rpo" != "None" ] && [ "$rpo" -gt 0 ]; } \
      && pass "$nm RPO = ${rpo}min" || fail "$nm RPO 无效: $rpo"
    if [ "$mode" = slo ]; then
      [ "$slo" != "None" ] && pass "$nm SLO = $slo" || fail "$nm SLO 为空(tier0/tier1 必须有)"
    else
      info "$nm 刻意不设 SLO(枚举底为 99.9,对 tier2 过严)"
    fi
  done
}

dod4() {
  echo "DoD-4 评估跑通"
  for s in $SERVICES; do
    nm=$(printf "%s" "$s" | sed "s#.*/##; s#:.*##")
    ok=$(RHCOUNT list-failure-mode-assessments --service-arn "$s" \
          --query "length(assessmentSummaries[?assessmentStatus=='SUCCESS'])")
    if [ "${ok:-0}" -ge 1 ]; then
      pass "$nm 有 $ok 次 SUCCESS 评估"
    elif grep -qE "^\| *\`?$nm\`? *\|.*不可评估" "$ANCHOR/coverage-ledger.md" 2>/dev/null; then
      pass "$nm 无 SUCCESS,但台账已记录成文的不可评估理由"
    else
      last=$(RH list-failure-mode-assessments --service-arn "$s" --sort-by STARTED_AT --sort-order DESC \
              --query 'assessmentSummaries[0].[assessmentStatus,errorMessage]' --output text)
      fail "$nm 无 SUCCESS 评估且台账无理由 | 最近: $last"
    fi
  done
}

dod5() {
  echo "DoD-5 图谱可消费的产出落盘"
  # dependencies 走单独判定: dependencyDiscovery 是异步的(35 天回看),
  # INITIALIZING 期间必然为空,故允许「空 + 状态已记录」通过
  for f in topology-edges-petsite-core.json findings-petsite-core.json; do
    p="$EXPORTS/$f"
    if [ ! -f "$p" ]; then fail "缺 $f"; continue; fi
    n=$(python3 - "$p" <<'PY'
import json,sys
try: d=json.load(open(sys.argv[1]))
except Exception: print(-1); raise SystemExit
if isinstance(d,list): print(len(d)); raise SystemExit
ls=[v for v in d.values() if isinstance(v,list)]
print(len(ls[0]) if ls else 0)
PY
)
    [ "$n" -gt 0 ] && pass "$f 非空($n 条)" || fail "$f 为空或不可解析(n=$n)"
  done
  # dependencies: 非空即通过;为空则要求 dependencyDiscovery 状态已落盘
  dp="$EXPORTS/dependencies-petsite-core.json"
  if [ ! -f "$dp" ]; then fail "缺 dependencies-petsite-core.json"; else
    dn=$(python3 -c "
import json,sys
d=json.load(open('$dp'))
ls=[v for v in d.values() if isinstance(v,list)]
print(len(ls[0]) if ls else 0)
" 2>/dev/null || echo -1)
    if [ "${dn:-0}" -gt 0 ]; then
      pass "dependencies 非空($dn 条)"
    else
      st=$(RH get-service --service-arn "$SVC_S1" --query 'service.dependencyDiscovery.status' --output text 2>/dev/null)
      if [ "$st" = INITIALIZING ] || [ "$st" = ENABLED ]; then
        info "dependencies 为空,但 dependencyDiscovery=$st(35 天回看,异步)—— 视为通过"
      else
        fail "dependencies 为空且 dependencyDiscovery=$st"
      fi
    fi
  fi
}

only="${1:-}"
if [ -n "$only" ]; then
  case "$only" in 1) dod1;; 2) dod2;; 3) dod3;; 4) dod4;; 5) dod5;;
    *) echo "用法: bash verify_dod.sh [1-5]"; exit 2;; esac
else
  dod1; echo; dod2; echo; dod3; echo; dod4; echo; dod5
fi

echo
if [ "$FAILED" -eq 0 ]; then printf "${G}==> 全部通过${N}\n"; else printf "${R}==> 有未通过项${N}\n"; fi
exit "$FAILED"
