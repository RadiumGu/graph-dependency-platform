#!/usr/bin/env bash
# 用 Chaos Mesh NetworkChaos 逐条验证图数据库里的运行时依赖边。
#
# 三态判据（「我测不了」和「依赖不存在」是两件不同的事）：
#   confirmed    断掉 B 之后 A 明显退化，且对照组不变
#   refuted      注入确实生效（AllInjected=True）但 A 没退化 → 图声称的依赖不成立
#   unverifiable 注入没生效 / 拿不到依据 → 记录原因，继续下一条
#
# ⚠️ 为什么必须先查 AllInjected：
#    PodChaos 的 pod-failure 在本集群（K8s 1.35）**完全无法生效** ——
#    它要往运行中的 Pod 插 pause initContainer，而 spec.initContainers 不可变：
#      Failed to apply chaos: Pod is invalid: spec.initContainers: Forbidden
#    此时探测结果会**全绿**。若不查 AllInjected 就直接看探测，
#    会把每一条边都错误地判成 refuted —— 假结论比没结论更糟。
#    所以本脚本在读探测结果之前，一律先断言 AllInjected=True。
#
# 为什么用 direction:to + target 而不是直接杀下游：
#    只切断 A→B 这一条边，B 本身保持健康，
#    才能区分「A 依赖 B」与「B 挂了」。直接杀 B 两种解释无法分辨。
#
# 自动恢复：靠 spec.duration 到期由 Chaos Mesh 自己撤销 tc 规则，
#    满足「故障注入必须可自动恢复，不得留下持续故障状态」的硬约束。
#    脚本额外在收尾时 delete 一次并断言 AllRecovered。

set -uo pipefail

K="${KUBECTL:-$HOME/bin/kubectl}"
NS=petadoptions
ALB=internal-petsite-internal-lt-1660792065.ap-northeast-1.elb.amazonaws.com
DURATION="${DURATION:-90s}"
SAMPLES="${SAMPLES:-8}"
OUT="${OUT:-/tmp/edge_verdicts.json}"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# 单次探测：跟随重定向（petsite 对无 userId 的请求会 302 到 /?userId=xxx，
# 不跟随会把正常行为记成失败），并识别「返回 200 的错误页」。
#
# ⚠️ PROBE_TIMEOUT 必须与 DURATION 一起算：故障期间请求会**超时**而非快速失败，
#    所以探测的最坏耗时 ≈ 2 × SAMPLES × PROBE_TIMEOUT（被测 + 对照两个 probe）。
#    实测正常时 24 次采样只要 6 秒，但故障期每次要耗满超时 ——
#    原来 timeout=15s、duration=90s 的组合下，窗口扣掉 22s 等待只剩 68s，
#    68 ÷ 15 ≈ 4 次请求，之后窗口到期、剩余请求全部成功。
#    这就是「无论采样 8 次还是 24 次，失败数都恰好是 4」的原因。
PROBE_TIMEOUT="${PROBE_TIMEOUT:-6}"
probe() {
  local url="$1" ok=0
  for _ in $(seq 1 "$SAMPLES"); do
    local code
    code=$(timeout $((PROBE_TIMEOUT+2)) curl -sSL --max-time "$PROBE_TIMEOUT" \
             -o "$TMP/p.html" -w "%{http_code}" "$url" 2>/dev/null)
    if [ "$code" = "200" ] && ! grep -q "Oops! Something went wrong" "$TMP/p.html" 2>/dev/null; then
      ok=$((ok+1))
    fi
  done
  echo "$ok"
}

# 窗口必须容纳最坏探测耗时 + 规则生效等待，否则探测跑出窗口 → 假 refuted。
_worst=$(( 2 * SAMPLES * PROBE_TIMEOUT + 30 ))
_dur_num=${DURATION%s}
if [ "$_dur_num" -lt "$_worst" ]; then
  echo "⚠️  DURATION=$DURATION 不足以容纳最坏探测耗时 ${_worst}s，自动调整为 ${_worst}s"
  DURATION="${_worst}s"
fi

RESULTS=()

# 参数：边名 源标签 目标标签 被测探测URL 对照探测URL
verify_edge() {
  local edge="$1" src="$2" dst="$3" url="$4" ctrl="$5"
  local name; name="vc-$(echo "$edge" | tr '[:upper:]_>/ ' '[:lower:]----' | tr -cd 'a-z0-9-' | cut -c1-50)"
  echo "───────────────────────────────────────────────"
  echo "边: $edge   （$src ✂ $dst）"

  local base ctrl_base
  base=$(probe "$url"); ctrl_base=$(probe "$ctrl")
  echo "  基线      被测=$base/$SAMPLES  对照=$ctrl_base/$SAMPLES"
  if [ "$base" -lt $((SAMPLES/2)) ]; then
    echo "  → unverifiable：基线本身就不健康（$base/$SAMPLES），退化判据失去意义"
    RESULTS+=("{\"edge\":\"$edge\",\"verdict\":\"unverifiable\",\"reason\":\"baseline unhealthy $base/$SAMPLES\"}")
    return
  fi

  cat > "$TMP/$name.yaml" <<YAML
apiVersion: chaos-mesh.org/v1alpha1
kind: NetworkChaos
metadata:
  name: $name
  namespace: $NS
spec:
  action: partition
  mode: all
  duration: $DURATION
  direction: to
  selector:
    namespaces: [$NS]
    labelSelectors:
      app: $src
  target:
    mode: all
    selector:
      namespaces: [$NS]
      labelSelectors:
        app: $dst
YAML

  $K apply -f "$TMP/$name.yaml" >/dev/null 2>&1
  sleep 22

  # ── 纪律：先断言注入真的生效，再看探测结果 ──
  local injected
  injected=$($K get networkchaos "$name" -n $NS \
    -o jsonpath='{range .status.conditions[?(@.type=="AllInjected")]}{.status}{end}' 2>/dev/null)
  if [ "$injected" != "True" ]; then
    local warn
    warn=$($K get events -n $NS --field-selector "involvedObject.name=$name" 2>/dev/null \
           | grep -i warning | tail -1 | cut -c1-150)
    echo "  → unverifiable：注入未生效 AllInjected=$injected"
    [ -n "$warn" ] && echo "     事件: $warn"
    RESULTS+=("{\"edge\":\"$edge\",\"verdict\":\"unverifiable\",\"reason\":\"chaos not injected (AllInjected=$injected)\"}")
    $K delete networkchaos "$name" -n $NS >/dev/null 2>&1
    return
  fi

  local during ctrl_during t0 t1
  t0=$(date +%s)
  during=$(probe "$url"); ctrl_during=$(probe "$ctrl")
  t1=$(date +%s)

  # ── 纪律（第二道）：探测**结束后**注入必须仍然有效 ──
  #
  # 否则探测会跑出故障窗口，后半程打在已自动恢复的系统上。
  # 这个坑真实发生过且极具误导性：duration=90s、22s 等待 + 24 次采样，
  # 探测总耗时超过窗口，结果六条边全部从 confirmed 翻成 refuted。
  # 识破它的线索是**失败绝对数在两次运行里完全相同（都是 4 次）**，
  # 而不是失败比例相同 —— 8 样本时 4/8=50%（判 confirmed），
  # 24 样本时 4/24=16.7%（判 refuted）。同一条边只因样本数变化就翻转结论，
  # 说明有效故障时间是固定的，即窗口早已到期。
  local still
  still=$($K get networkchaos "$name" -n $NS \
    -o jsonpath='{range .status.conditions[?(@.type=="AllInjected")]}{.status}{end}' 2>/dev/null)
  local elapsed=$((t1-t0))
  echo "  注入期    被测=$during/$SAMPLES  对照=$ctrl_during/$SAMPLES  (探测耗时 ${elapsed}s，结束时 AllInjected=$still)"
  if [ "$still" != "True" ]; then
    echo "  → unverifiable：探测（${elapsed}s）跑出了故障窗口（duration=$DURATION），后半程打在已恢复的系统上"
    RESULTS+=("{\"edge\":\"$edge\",\"verdict\":\"unverifiable\",\"reason\":\"probe (${elapsed}s) outran fault window $DURATION; AllInjected=$still at end\",\"samples\":$SAMPLES}")
    $K delete networkchaos "$name" -n $NS >/dev/null 2>&1
    return
  fi

  # 等自动恢复
  for _ in $(seq 1 10); do
    local rec
    rec=$($K get networkchaos "$name" -n $NS \
      -o jsonpath='{range .status.conditions[?(@.type=="AllRecovered")]}{.status}{end}' 2>/dev/null)
    [ "$rec" = "True" ] && break
    sleep 15
  done
  sleep 10
  local after
  after=$(probe "$url")
  echo "  恢复后    被测=$after/$SAMPLES"
  $K delete networkchaos "$name" -n $NS >/dev/null 2>&1

  # 判据用**契约的退化百分比**，不是「掉到基线一半以下」这种自定阈值：
  #   confirm_degradation_pct=20.0  refute_degradation_pct=5.0
  # 中间带（5%~20%）既不足以确认也不足以否证 → inconclusive，
  # 硬塞进 confirmed 或 refuted 都是过度解读。
  # 另外记录 samples，写回时要与契约的 min_observation_requests 比对。
  local deg
  deg=$(python3 -c "b=$base; d=$during; print(round((b-d)/b*100,1) if b else 0.0)")
  local ctrl_deg
  ctrl_deg=$(python3 -c "b=$ctrl_base; d=$ctrl_during; print(round((b-d)/b*100,1) if b else 0.0)")
  echo "  退化      被测=${deg}%  对照=${ctrl_deg}%"

  local ctrl_stable
  ctrl_stable=$(python3 -c "print(1 if $ctrl_deg <= 5.0 else 0)")
  local is_conf is_ref
  is_conf=$(python3 -c "print(1 if $deg >= 20.0 else 0)")
  is_ref=$(python3 -c "print(1 if $deg <= 5.0 else 0)")

  if [ "$ctrl_stable" != "1" ]; then
    echo "  → unverifiable：**对照组也退化了** ${ctrl_deg}% —— 无法把被测的退化归因到这一条边"
    RESULTS+=("{\"edge\":\"$edge\",\"verdict\":\"unverifiable\",\"reason\":\"control degraded ${ctrl_deg}%\",\"baseline\":$base,\"during\":$during,\"samples\":$SAMPLES}")
  elif [ "$is_conf" = "1" ]; then
    echo "  → confirmed：退化 ${deg}% ≥ 契约阈值 20%，对照稳定（${ctrl_deg}%）"
    RESULTS+=("{\"edge\":\"$edge\",\"verdict\":\"confirmed\",\"baseline\":$base,\"during\":$during,\"after\":$after,\"degradation_pct\":$deg,\"control_degradation_pct\":$ctrl_deg,\"samples\":$SAMPLES}")
  elif [ "$is_ref" = "1" ]; then
    echo "  → refuted：注入生效且探测全程在窗口内，但退化仅 ${deg}% ≤ 5% —— 依赖不成立"
    RESULTS+=("{\"edge\":\"$edge\",\"verdict\":\"refuted\",\"baseline\":$base,\"during\":$during,\"after\":$after,\"degradation_pct\":$deg,\"samples\":$SAMPLES}")
  else
    echo "  → inconclusive：退化 ${deg}% 落在 5%~20% 的中间带，证据不足以定论"
    RESULTS+=("{\"edge\":\"$edge\",\"verdict\":\"inconclusive\",\"reason\":\"degradation ${deg}% between refute(5%) and confirm(20%) thresholds\",\"baseline\":$base,\"during\":$during,\"after\":$after,\"degradation_pct\":$deg,\"samples\":$SAMPLES}")
  fi
}

echo "════ 阶段 C：依赖边注入验证 ════"
echo "  duration=$DURATION  samples=$SAMPLES"
echo ""

verify_edge "petsite-Calls->petsearch" petsite search-service \
  "http://$ALB/?selectedPetType=puppy&selectedPetColor=brown" \
  "http://$ALB/PetListAdoptions?userId=1000"

verify_edge "petsite-Calls->petlistadoptions" petsite list-adoptions \
  "http://$ALB/PetListAdoptions?userId=1000" \
  "http://$ALB/FoodService?userId=1000&petType=puppy"

verify_edge "petsite-Calls->pethistory" petsite pethistory \
  "http://$ALB/pethistory" \
  "http://$ALB/FoodService?userId=1000&petType=puppy"

verify_edge "pethistory-Calls->petlistadoptions" pethistory list-adoptions \
  "http://$ALB/pethistory" \
  "http://$ALB/FoodService?userId=1000&petType=puppy"

verify_edge "petlistadoptions-Calls->petsearch" list-adoptions search-service \
  "http://$ALB/PetListAdoptions?userId=1000" \
  "http://$ALB/FoodService?userId=1000&petType=puppy"

echo ""
echo "════ 判定汇总 ════"
printf '%s\n' "${RESULTS[@]}" | python3 -c "
import sys, json
rows=[json.loads(l) for l in sys.stdin if l.strip()]
for r in rows:
    extra=''
    if 'baseline' in r: extra=f\"  {r['baseline']}→{r.get('during')}→{r.get('after','-')}\"
    print(f\"  {r['verdict']:14s} {r['edge']:42s}{extra}  {r.get('reason','')}\")
json.dump(rows, open('$OUT','w'), ensure_ascii=False, indent=1)
print()
from collections import Counter
for v,n in Counter(r['verdict'] for r in rows).most_common(): print(f'  {v}: {n}')
print(f'  已写 $OUT')
"

echo ""
echo "════ 收尾断言：不得留下任何故障 ════"
LEFT=$($K get networkchaos,podchaos --all-namespaces --no-headers 2>/dev/null | wc -l)
echo "  残留故障实验数: $LEFT $([ "$LEFT" = "0" ] && echo '✅' || echo '⚠️ 需清理')"
