#!/usr/bin/env bash
# run_golden_suite.sh — 七个 golden 套件的**唯一权威跑法**。
#
# ## 为什么要有这个脚本
#
# 七个 golden 测试各自在文件头写自己的运行命令，`cd` 的目录和 PYTHONPATH
# 都不一样。七份各自维护的命令必然漂移 —— 事实上到 2026-09-21 它们全都还写着
# `XXX_ENGINE=direct`，而 direct 实现已在当天全部删除：照着文档跑的人会以为
# 自己在测 direct，实际拿到的是 strands。又一个假信号。
#
# 所以把跑法收成一份。各测试文件头改为指向这里。
#
# ## 这些 golden 守什么
#
# 它们是**唯一**会真调 LLM 与真实 AWS 的测试 —— 也就是说「strands 的行为
# 是否退化」只有它们能发现。而它们默认 skip（需 RUN_GOLDEN=1），
# 且 GitHub 托管 runner 既没有 AWS 凭据也到不了 VPC 内的 Neptune，
# 所以 CI 里从来没跑过。这个缺口是 2026-09 那一串问题的根源之一：
# 基线测的环境与线上跑的环境长期不一致而无人知晓。
#
# ## 成本（基于实测 token 明细，不是猜）
#
# smart-query 20 条实测 total=513876 tokens，其中 cache_read=462046
# （命中率 94.3%，cache 读单价约为 input 的 1/10），折算 ≈ $0.44。
# 七套合计 76 条，按 hypothesis/layer2 单条更贵（生成 20 个假设、
# 编排 6 个 probe）取 2-3 倍权重，全量一次约 **$3-5**、耗时约 10-15 分钟。
#
#     每次 PR（约 10 次/天）  ≈ $1250/月   ← 不建议
#     每日一次               ≈ $126/月
#     每周一次               ≈ $17/月
#
# ## 用法
#
#     bash scripts/run_golden_suite.sh --list        # 只列出要跑什么，不执行
#     bash scripts/run_golden_suite.sh --dry-run     # 打印命令，不执行
#     bash scripts/run_golden_suite.sh               # 全跑
#     bash scripts/run_golden_suite.sh policy_guard  # 只跑一套（调试用，最便宜）
#
# 退出码：0 = 全部通过；非 0 = 有套件失败（失败套件名会列在汇总里）。
# 适合挂 cron / systemd timer，非 0 退出时告警。
#
# ## 前置
#
# 必须在**有 AWS 凭据且能访问 VPC 内 Neptune** 的机器上跑。
# 缺凭据时本仓库 9 处签名路径会抛「凭据未解析到」—— 那是刻意的，
# 用来拒绝在无法真实验证的环境下给出绿灯。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-python3.11}"

# 每套一行：<名称>|<工作目录（相对仓库根）>|<PYTHONPATH>|<测试文件（相对仓库根）>|<额外 pytest 参数>
#
# ⚠️ 刻意**不设** XXX_ENGINE 变量：2026-09-21 起七个模块都只有 strands
# 一种实现，factory 也去掉了回退分支。设 `=direct` 不会报错但会误导
# （实际仍返回 strands），所以不设比设错好。
SUITES=(
  "smart_query|rca|.:..|tests/test_golden_accuracy.py|"
  "layer2|rca|.:..|tests/test_layer2_golden.py|"
  "hypothesis|.|rca:chaos/code|tests/test_hypothesis_golden.py|"
  "learning|.|rca:chaos/code|tests/test_learning_golden.py|"
  "policy_guard|chaos/code|.:../..|tests/test_policy_guard_golden.py|"
  "chaos_runner|chaos/code|.:../..|tests/test_runner_golden.py|-k l1"
  "dr_executor|dr-plan-generator|.:..|tests/test_dr_executor_golden.py|-k l1"
)

WANT="${1:-}"
MODE="run"
case "$WANT" in
  --list)    MODE="list";    WANT="" ;;
  --dry-run) MODE="dry";     WANT="" ;;
  --help|-h) sed -n '2,50p' "${BASH_SOURCE[0]}"; exit 0 ;;
esac

if [ "$MODE" = "list" ]; then
  printf "  %-14s %-18s %s\n" "套件" "工作目录" "测试文件"
  for s in "${SUITES[@]}"; do
    IFS='|' read -r name wd pp file extra <<<"$s"
    printf "  %-14s %-18s %s %s\n" "$name" "$wd" "$file" "$extra"
  done
  exit 0
fi

FAILED=()
PASSED=()
START=$(date +%s)

for s in "${SUITES[@]}"; do
  IFS='|' read -r name wd pp file extra <<<"$s"
  [ -n "$WANT" ] && [ "$WANT" != "$name" ] && continue

  # 测试文件路径要相对于工作目录换算
  rel_file="$ROOT/$file"
  cmd=( env "PYTHONPATH=$pp" "RUN_GOLDEN=1" "$PY" -m pytest "$rel_file" -q )
  [ -n "$extra" ] && cmd+=( $extra )

  echo "───────────────────────────────────────────────"
  echo "▶ $name   (cwd=$wd)"
  if [ "$MODE" = "dry" ]; then
    echo "  cd $ROOT/$wd && ${cmd[*]}"
    continue
  fi

  if ( cd "$ROOT/$wd" && "${cmd[@]}" ); then
    PASSED+=("$name")
  else
    FAILED+=("$name")
  fi
done

[ "$MODE" = "dry" ] && exit 0

ELAPSED=$(( $(date +%s) - START ))
echo "═══════════════════════════════════════════════"
echo "golden 汇总（耗时 ${ELAPSED}s）"
echo "  通过: ${#PASSED[@]}  ${PASSED[*]:-}"
echo "  失败: ${#FAILED[@]}  ${FAILED[*]:-}"

if [ ${#FAILED[@]} -gt 0 ]; then
  echo
  echo "❌ 有 golden 套件失败。这类失败意味着 **LLM 行为可能已退化** ——"
  echo "   它们是唯一能发现这件事的测试，别当成 flaky 直接重跑了事。"
  exit 1
fi

if [ ${#PASSED[@]} -eq 0 ]; then
  echo
  echo "⚠️ 一个套件都没跑 —— 参数写错了？这不是成功。"
  exit 2
fi

echo
echo "✅ 全部 golden 通过"
