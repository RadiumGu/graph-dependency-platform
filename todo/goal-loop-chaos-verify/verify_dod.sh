#!/usr/bin/env bash
# verify_dod.sh —— 逐条核验 north_star.md 的 8 条 DoD。
#
# 用法：  cd todo/goal-loop-chaos-verify && ./verify_dod.sh
#         ./verify_dod.sh --local-only     # 跳过需要 Neptune / AWS 的检查
#
# 约定：每条打印 "PASS <id> <说明>" 或 "FAIL <id> <说明>" 或 "SKIP <id> <原因>"。
# 退出码 0 表示零 FAIL。SKIP 不算失败，但 DoD 未达成前不许创建 STOP。
#
# 不变量提醒（见 north_star.md §4）：
#   - 必须用 python3.11。python3（3.9）会产生约 19 条假失败。
#   - 层里的 Neptune 客户端读 REGION，不读 AWS_REGION。

set -uo pipefail

REPO="/home/ec2-user/works/graph-dependency-platform"
PY=python3.11
REGION_DEFAULT=ap-northeast-1
LOCAL_ONLY=0
[[ "${1:-}" == "--local-only" ]] && LOCAL_ONLY=1

PASS=0; FAIL=0; SKIP=0
ok()   { echo "PASS $1  $2"; PASS=$((PASS+1)); }
no()   { echo "FAIL $1  $2"; FAIL=$((FAIL+1)); }
skip() { echo "SKIP $1  $2"; SKIP=$((SKIP+1)); }

cd "$REPO" || { echo "FAIL repo  $REPO 不存在"; exit 1; }

command -v $PY >/dev/null 2>&1 || { echo "FAIL env  $PY 不存在——不要退回 python3"; exit 1; }

echo "===== DoD-1 契约门禁全线生效（线上） ====="
if [[ $LOCAL_ONLY -eq 1 ]]; then
  skip 1.1 "--local-only"
else
  FNS="neptune-etl-from-aws neptune-etl-from-deepflow neptune-etl-from-xray neptune-etl-from-cfn"
  vers=""; modes_bad=0
  for f in $FNS; do
    cfg=$(aws lambda get-function-configuration --function-name "$f" \
            --region $REGION_DEFAULT --output json 2>/dev/null) || { cfg=""; }
    if [[ -z "$cfg" ]]; then modes_bad=1; continue; fi
    v=$(echo "$cfg" | $PY -c "import sys,json,re;d=json.load(sys.stdin);ls=[l['Arn'] for l in d.get('Layers',[]) if 'neptune-client-base' in l['Arn']];print(ls[0].rsplit(':',1)[1] if ls else 'none')")
    m=$(echo "$cfg" | $PY -c "import sys,json;d=json.load(sys.stdin);print(d.get('Environment',{}).get('Variables',{}).get('GRAPH_CONTRACT_MODE','enforce'))")
    vers="$vers $v"
    [[ "$m" == "off" || "$m" == "warn" ]] && modes_bad=1
  done
  uniq_v=$(echo $vers | tr ' ' '\n' | sort -u | tr '\n' ',')
  if [[ "$modes_bad" -eq 0 ]]; then ok 1.1 "四函数 GRAPH_CONTRACT_MODE 均非 off/warn"
  else no 1.1 "有函数处于 off/warn 或不可读"; fi
  minv=$(echo $vers | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -n | head -1)
  if [[ -n "$minv" && "$minv" -ge 6 ]]; then ok 1.2 "层版本均 >=6（实际 $uniq_v）"
  else no 1.2 "层版本未全部 >=6（实际 $uniq_v）"; fi
  if [[ "$(echo $vers | tr ' ' '\n' | grep -c . )" == "$(echo $vers | tr ' ' '\n' | sort -u | grep -c .)x" ]]; then :; fi
  nuniq=$(echo $vers | tr ' ' '\n' | grep -E '^[0-9]+$' | sort -u | wc -l)
  if [[ "$nuniq" == "1" ]]; then ok 1.3 "四函数层版本一致"; else no 1.3 "层版本漂移：$uniq_v"; fi
fi

# 已部署代码是否含两参数签名 —— 用签名判定，不用标记字符串（不变量 6）
if [[ $LOCAL_ONLY -eq 1 ]]; then
  skip 1.4 "--local-only（需下载线上包）"
else
  skip 1.4 "需下载线上函数包逐文件 diff，见 T-270 验收步骤"
fi

echo
echo "===== DoD-2 活图谱零已知结构缺陷 ====="
if [[ $LOCAL_ONLY -eq 1 ]]; then
  skip 2.x "--local-only"
else
  AUDIT="$REPO/todo/goal-loop-chaos-verify/_dod_graph_audit.py"
  if [[ -f "$AUDIT" ]]; then
    REGION=${REGION:-$REGION_DEFAULT} $PY "$AUDIT" && ok 2.1 "端点/身份/重复边普查通过" \
      || no 2.1 "普查发现违约，详见上方输出"
  else
    skip 2.1 "缺 _dod_graph_audit.py —— 首轮请把上轮用过的普查查询固化成脚本"
  fi
fi

echo
echo "===== DoD-3 边验证闭环真实跑通 ====="
if grep -q "edge_verification" chaos/code/runner/runner.py 2>/dev/null; then
  ok 3.1 "runner.py 已引用 edge_verification"
else
  no 3.1 "runner.py 尚未接入 edge_verification（T-210）"
fi
# 边属性不得使用 property(single, ...)：只看 Gremlin 字符串字面量，不看文档字符串
if $PY - <<'PY'
import re, pathlib, sys
bad = []
for p in pathlib.Path('chaos/code').rglob('*.py'):
    t = p.read_text(encoding='utf-8', errors='ignore')
    for m in re.finditer(r"""g\.E\(\)[^"']*?property\(\s*single""", t, re.S):
        bad.append(f"{p}:{t[:m.start()].count(chr(10))+1}")
if bad:
    print("  边属性上仍有 property(single, ...):", *bad, sep="\n    ")
    sys.exit(1)
sys.exit(0)
PY
then ok 3.2 "边属性无 property(single, ...)"; else no 3.2 "边属性仍用 property(single, ...)（T-212）"; fi

if [[ $LOCAL_ONLY -eq 1 ]]; then skip 3.3 "--local-only"
else skip 3.3 "需查活图谱 verify_status/verified_by 非空（T-214）"; fi

echo
echo "===== DoD-6 LLM 输出受约束 ====="
NF=$($PY - <<'PY'
import re, pathlib
t = pathlib.Path('chaos/code/agents/hypothesis_direct.py').read_text()
blk = t[t.index('FAULT_DEFAULTS = {'):]
blk = blk[:blk.index('\nVALID_FAULT_TYPES')]
ns = {}; exec(blk, ns)
print(len(ns['FAULT_DEFAULTS']))
PY
)
CAT=$($PY -c "
import yaml; c=yaml.safe_load(open('chaos/code/runner/fault_catalog.yaml'))
print(sum(len(v) for v in c.values() if isinstance(v,(list,dict))))")
if [[ "$NF" == "$CAT" ]]; then ok 6.1 "LLM 可见故障 = catalog 全集（$CAT）"
else no 6.1 "LLM 可见 $NF 种，catalog 有 $CAT 种（T-241）"; fi

if grep -q 'FAULT_DEFAULTS\["pod_kill"\]' chaos/code/agents/hypothesis_direct.py 2>/dev/null; then
  no 6.2 "仍有静默填 pod_kill 的兜底（T-240）"
else
  ok 6.2 "无静默填 pod_kill 兜底"
fi

echo
echo "===== DoD-9 Strands / AgentCore 成为实际运行路径 ====="
if $PY -c "from strands import Agent, tool" >/dev/null 2>&1; then
  SV=$($PY -c "from importlib.metadata import version; print(version('strands-agents'))" 2>/dev/null)
  ok 9.1 "strands 可导入（strands-agents $SV）"
else
  no 9.1 "strands 未安装（T-205）—— 六个引擎全在静默回退 direct"
fi
if grep -qE '^\s*strands-agents>=1\.' requirements-dev.txt 2>/dev/null; then
  ok 9.2 "requirements-dev.txt 已解注释且下界 >=1.x"
else
  no 9.2 "requirements-dev.txt 仍注释掉 strands 或下界是 >=0.1（T-205）"
fi
# 六个引擎开关的默认值必须是 strands
BAD=$(grep -rhoE "(HYPOTHESIS|LEARNING|NLQUERY|LAYER2|GUARD|RUNNER)_ENGINE[^)]*\)\s*or\s*['\"]direct['\"]|getenv\(\s*['\"](HYPOTHESIS|LEARNING|NLQUERY|LAYER2|GUARD|RUNNER)_ENGINE['\"]\s*,\s*['\"]direct['\"]" \
      chaos/ rca/ --include="*.py" 2>/dev/null | wc -l)
if [[ "${BAD:-1}" -eq 0 ]]; then ok 9.3 "六个引擎开关默认非 direct"
else no 9.3 "仍有 $BAD 处引擎开关默认 direct（T-206）"; fi
if $PY -c "import bedrock_agentcore" >/dev/null 2>&1; then
  ok 9.4 "bedrock_agentcore 可导入"
else
  no 9.4 "AgentCore SDK 未安装（T-208）—— 东京区四项能力均可用，不可用不能作为理由"
fi

echo
echo "===== DoD-10 闭环校正（需活图谱） ====="
if [[ $LOCAL_ONLY -eq 1 ]]; then skip 10.1 "--local-only"
else skip 10.1 "需查活图谱：refuted 边数 == 已归因边数（T-281）"; fi

echo
echo "===== DoD-8 测试与卫生 ====="
[[ -e "chaos/=23.0.0" ]] && no 8.1 "异常文件 chaos/=23.0.0 仍在（T-200）" || ok 8.1 "异常文件已删"

if grep -rq "delete_date: 2026-08-18" chaos/code/agents/hypothesis_direct.py 2>/dev/null; then
  no 8.2 "过期冻结注释仍在（T-201）"
else ok 8.2 "无过期冻结注释"; fi

echo "  跑全量测试（约 1-3 分钟）..."
TOUT=$(mktemp)
if timeout 1800 $PY -m pytest -q --tb=no >"$TOUT" 2>&1; then :; fi
LINE=$(tail -5 "$TOUT" | grep -E "passed|failed" | tail -1)
NPASS=$(echo "$LINE" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+" || echo 0)
NFAIL=$(echo "$LINE" | grep -oE "[0-9]+ failed" | grep -oE "[0-9]+" || echo 0)
echo "  -> $LINE"
if [[ "${NFAIL:-0}" -eq 0 ]]; then ok 8.3 "零失败"; else no 8.3 "$NFAIL 条失败——基线是 0，任何失败都是本轮造成的"; fi
if [[ "${NPASS:-0}" -ge 442 ]]; then ok 8.4 "通过数 $NPASS >= 442"; else no 8.4 "通过数 $NPASS < 442（基线水位）"; fi
rm -f "$TOUT"

echo
echo "================ 汇总 ================"
echo "PASS=$PASS  FAIL=$FAIL  SKIP=$SKIP"
if [[ $FAIL -eq 0 && $SKIP -eq 0 ]]; then
  echo "全部 DoD 绿 —— 可以创建 STOP 并写交接文档。"
else
  echo "尚未达成（FAIL 或 SKIP 非零）——不要创建 STOP。"
fi
exit $(( FAIL > 0 ? 1 : 0 ))
