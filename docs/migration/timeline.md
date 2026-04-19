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
    freeze_date: ~         # L1 POC 达标后由大乖乖填（Phase 2 Week 3）
    delete_date: ~         # 冻结日 + 4 个月
    owner: "@programming-cat"
    status: "active"
    notes: "Phase 2 L1 POC 完成（2026-04-18）；direct 20/20, strands 19/20"

  - name: hypothesis-agent
    direct_file: chaos/code/agents/hypothesis_direct.py
    freeze_date: 2026-04-18
    delete_date: 2026-08-18
    owner: "@programming-cat"
    status: "frozen"
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
    freeze_date: ~
    delete_date: ~
    owner: "@programming-cat"
    status: "active"
    notes: "Phase 3 Module 3 PR1-5 完成（2026-04-19）；Direct 6/6、Strands 6/6。灰度中。缓存 token 报告待调查。"

  - name: chaos-policy-guard
    direct_file: chaos/code/policy/guard_direct.py
    freeze_date: ~
    delete_date: ~
    owner: "@programming-cat"
    status: "active"
    notes: "Phase 3 Module 4 完成（2026-04-19）；Direct 12/12、Strands 12/12。Shadow 一致性 12/12。缓存方案 A 启用（1839 tokens）。灰度中。"

  - name: chaos-runner
    direct_file: chaos/code/runner/runner_direct.py
    freeze_date: ~
    delete_date: ~
    owner: "@programming-cat"
    status: "planned"
    notes: "Phase 3 Week 16-18；真正 mutate K8s/FIS 状态，风险最高；3 个 dry-run 实验"

  - name: dr-executor
    direct_file: dr-plan-generator/executor_direct.py
    freeze_date: ~
    delete_date: ~
    owner: "@programming-cat"
    status: "planned"
    notes: "Phase 3 Week 19-20；跨 region、影响生产，最后迁；2 个完整演练"

tags:
  last_direct_snapshot: ""      # Phase 2 开始前由大乖乖打 v-last-direct-YYYYMMDD
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
