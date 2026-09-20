# Strands Agents 迁移时间线
#
# 维护规则：
#   - 本文件前半部分是机器可读 YAML（用 `yaml.safe_load` 读整份文件，
#     YAML 解析会止于第一段非 YAML 文本；Markdown 注释写在之后）
#   - scripts/check_migration_deadlines.py 会读取此文件进行 CI 检查
#   - freeze_date / delete_date 一旦写入，修改需提交书面 ADR
#   - status 取值：planned | active | frozen | deleted
#
# 迁移策略见 experiments/strands-poc/migration-strategy.md

modules:
  - name: smart-query
    direct_file: rca/neptune/nl_query_direct.py
    freeze_date: 2026-04-22
    delete_date: 2026-04-29   # 冻结一周（大乖乖指定）
    owner: "@programming-cat"
    status: "frozen"
    notes: "Phase 2 L1 POC 完成（2026-04-18）；direct 20/20, strands 20/20。2026-04-22 冻结，一周后解冻。2026-09-20 更正：此处原写 strands 19/20，与实测基线矛盾 —— tests/golden/BASELINE-strands.md（Last run 2026-04-18 08:43:19 UTC，与 direct 同一次运行、同一提交 6ac836a）记的是 Pass 20/20 = 100.0%。基线由测试直接产出，本 notes 是手写的，以基线为准。2026-09-20 重跑双引擎并完成调优，**卡点已消除**。① 重测发现那批 04-18 数据早已过期：strands p99 自然降到 15851ms（vs direct 7297ms = 2.17x），原记的 5.58x 五个月前就不再成立 —— 卡了五个月只因为没人重跑。② 随后做了两处结构调优：ReAct 从 3 轮压到 2 轮（删掉强制前置的 validate_cypher —— 安全校验在 execute_cypher 内部无条件执行、从不依赖 Agent 先调 validate，那一轮是净亏；安全性不变），并删掉 get_schema_section（同一份 schema 已完整在 system prompt 里，实测被调用 0 次）。③ 引擎侧不再为拿完整结果重跑一次 Neptune（tool 执行时就把未截断 rows 存下）。调优后 p99 11476ms = **1.57x**（门槛 ≤2.5x，余量 37%）、token 2.28x、p50 5197ms、准确率仍 20/20。三次测量同日、同 20 条用例、同环境。"

  - name: hypothesis-agent
    direct_file: chaos/code/agents/hypothesis_direct.py
    freeze_date: 2026-04-18
    delete_date: 2026-08-18
    owner: "@programming-cat"
    status: "deleted"
    notes: "Phase 3 Module 1 完成（2026-04-18）；direct 18/20、strands 20/20（P0-bugfix 后，commit 5376421）。Cache hit direct 66% / strands 76.2%。冻结期内禁止修改 direct，除非 P0-bugfix label。"

  - name: learning-agent
    direct_file: chaos/code/agents/learning_direct.py
    freeze_date: 2026-04-26
    delete_date: 2026-08-26
    owner: "@programming-cat"
    status: "active"
    notes: "Phase 3 Module 2 完成（2026-04-19）；direct 10/10、strands 10/10。灰度中，4/26 冻结。"

  - name: rca-layer2-probers
    direct_file: rca/collectors/layer2_direct.py
    freeze_date: 2026-04-19
    delete_date: 2026-08-19
    owner: "@programming-cat"
    status: "deleted"
    notes: "Phase 3 Module 3 PR1-5 完成（2026-04-19）；Direct 6/6、Strands 6/6。灰度切换完成。"

  - name: chaos-policy-guard
    direct_file: chaos/code/policy/guard_direct.py
    freeze_date: 2026-04-19
    delete_date: 2026-08-19
    owner: "@programming-cat"
    status: "deleted"
    notes: "Phase 3 Module 4 完成（2026-04-19）；Direct 12/12、Strands 12/12。Shadow 12/12。缓存方案 A（1839 tokens）。"

  - name: chaos-runner
    direct_file: chaos/code/runner/runner_direct.py
    freeze_date: 2026-04-20
    delete_date: 2026-08-20
    owner: "@programming-cat"
    status: "deleted"
    notes: "Phase 3 Week 16-18；L1 Golden 6/6 both engines；dry_run double gate；7 tools"

  - name: dr-executor
    direct_file: dr-plan-generator/executor_direct.py
    freeze_date: 2026-04-21
    delete_date: 2026-08-21
    owner: "@programming-cat"
    status: "deleted"
    notes: "Phase 3 最终模块；L1 Golden 2/2 both engines；8 tools + failure strategy + partial caching"

tags:
  last_direct_snapshot: "v-last-direct-20260422"
  strands_only: ""              # Phase 4 结束时打 v-strands-only-YYYYMMDD

phase_gates:
  gate_a_phase0_to_1: "passed"          # 2026-04-18 L0 spike 全通过 + L1 地基 (PR1) + engine 骨架
  gate_b_phase2_to_3: "not_started"     # 需 Smart Query L1 POC 稳定 ≥ 4 周、成本核算、Shadow 长期对比
  gate_c_phase3_to_4: "not_started"     # 所有模块稳定 ≥ 4 周
  gate_d_phase4_to_5: "not_started"     # direct 已全删

# -----------------------------------------------------------------------------
# 人类可读说明（YAML 解析不会消费以下内容，Markdown 渲染器会）
# -----------------------------------------------------------------------------

---

## 迁移策略总览

本仓库目前有两套 NL Query / Agent 实现共存：
- **direct**：直接调 Bedrock，成熟稳定，是 freeze-and-delete 的对象。
- **strands**：基于 Strands Agents 的 ReAct 实现，是最终目标。

通过 `NLQUERY_ENGINE` env 切换。Factory (`rca/engines/factory.py`) 未装 Strands 时自动回退 direct。

## 当前进度（2026-04-18）

| Phase | 状态 | 备注 |
|-------|------|------|
| Phase 0 — 准备期 | ✅ 完成 | L0 Spike 6 硬约束全通过（见 `experiments/strands-poc/report.md`）|
| Phase 1 — 铺地基 | ✅ 完成 | `rca/engines/` + factory 合并到 main |
| Phase 2 — Smart Query 先迁 | 🚧 L1 POC 完成 | direct 20/20、strands 19/20 baseline 已入仓；待大乖乖填 `freeze_date` |
| Phase 3 — 批量迁移 | ⬜ 未开始 | 等 Phase 2 稳定 ≥ 4 周 + Gate B |
| Phase 4 — 统一清理 | ⬜ 未开始 | 所有模块稳定 ≥ 4 周后 |
| Phase 5 — 收尾 | ⬜ 未开始 | 物理删除 + 打 tag |

## CI 接入计划（待激活）

- `scripts/check_migration_deadlines.py`：本 Phase **未接入 CI**；Phase 3 启动前由大乖乖激活。
  当前由于 `freeze_date`/`delete_date` 均为空，脚本会在遇到空值时 skip 对应模块。

## ADR 参考

暂无 ADR。首个 ADR 预计在 Phase 2 Week 3（smart-query 冻结时）落地。
