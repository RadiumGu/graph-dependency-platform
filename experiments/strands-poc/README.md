# Strands Agents POC & Migration

> **中文** | [🌐 English](./README.en.md)

> AWS Strands Agents 在 graph-dependency-platform 的可行性验证 + 全平台迁移策略。
>
> 📅 2026-04-18 | 🔍 架构审阅猫 & 编程猫

---

## 📖 应该读什么（按顺序）

### 1. `report.md` — L0 Spike + L1 POC + L2 POC 全过程记录 ✅
- §1-6：L0 Spike 6 硬约束验证 → *GO*
- §7：L1 POC 完成（Direct 20/20 + Strands 20/20，6 个 PR 合并 main）
- §8：*L2 Prompt Caching 完成（direct 99.6% / strands 90.9% hit ratio，成本降 48%/62%）*

### 2. `migration-strategy.md` — 正式迁移策略
Strangler Fig 单向迁移：direct 代码冻结 → 删除，终态单一 Strands 实现。
5 Phase / 21-27 周 / 6 模块串行，含冻结日 + 删除日 CI 强制机制。

### 3. `TASK-L1-smart-query.md` / `TASK-L2-prompt-caching.md` — 任务指令
L1/L2 的详细任务书，包含硬约束、验收门槛、PR 拆解规范。*已全部完成*。

---

## 🗂 目录内容

### 策略文档
- `report.md` — L0/L1/L2 阶段汇总报告（必读）
- `migration-strategy.md` — 迁移策略（启动新 phase 前读）
- `TASK-L1-smart-query.md` — L1 POC 任务书（已完成 2026-04-18）
- `TASK-L2-prompt-caching.md` — L2 Prompt Caching 任务书（已完成 2026-04-18）

### POC 代码（可执行）
- `spike.py` — L0 Strands Agent POC（3 @tool + BedrockModel + guard）
- `baseline.py` — 现版 NLQueryEngine 同题对跑
- `golden_questions.yaml` — 3 条冒烟问题
- `run_spike.sh` / `run_baseline.sh` — 一键执行脚本
- `requirements.txt` — Strands 依赖
- *`verify_cache_direct.py`* — L2 Direct 引擎缓存生效自检（3 次同问，验 write→read→read）
- *`verify_cache_strands.py`* — L2 Strands 引擎缓存生效自检

### 运行结果
- `spike_result.json` — L0 Strands 版完整 trace
- `baseline_result.json` — L0 direct 版对比数据

### 客户参考资料
- `CUSTOMER-REFERENCE-nl-to-graph-query.md` — 推荐 Strands Agents 作为默认起点；§9.1 *新增 Prompt Caching 降本实测数据（-48% / -62%）*

### 归档
- `archive/migration-strategy-v2-dual-track.md` — 早期双轨方案，已弃用；保留作决策历史

---

## 🏁 当前状态（2026-04-18）

| Phase | 状态 | 关键产出 |
|-------|------|---------|
| L0 Spike | ✅ 完成 | 6 硬约束全过 |
| L1 POC | ✅ 完成 | 6 PR 合并，Direct 20/20 + Strands 20/20 双引擎 baseline |
| L2 Prompt Caching | ✅ 完成 | 4 PR 合并，direct 99.6% / strands 90.9% hit ratio，月度成本降 48%/62% |
| Phase 2 稳定期 | 🚧 观察中 | 4 周后填 freeze_date 启动 Phase 3 |
| Phase 3 批量迁移 | ⬜ 未开始 | Hypothesis / Learning / Probers / Chaos / DR 6 模块 |

---

## 🚀 如何复跑

### L0 Spike（独立 venv）
```bash
cd experiments/strands-poc
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
bash run_spike.sh      # 跑 Strands 版
bash run_baseline.sh   # 跑 direct 版
```

### L1 Golden CI（生产代码 + 主环境 strands）
```bash
cd /home/ubuntu/tech/graph-dependency-platform
RUN_GOLDEN=1 NLQUERY_ENGINE=direct  pytest tests/test_golden_accuracy.py
RUN_GOLDEN=1 NLQUERY_ENGINE=strands pytest tests/test_golden_accuracy.py
```

### L2 Cache 生效自检
```bash
NEPTUNE_ENDPOINT=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com \
  python3 experiments/strands-poc/verify_cache_direct.py
NEPTUNE_ENDPOINT=... python3 experiments/strands-poc/verify_cache_strands.py
```

需要：AWS 凭证（Bedrock + Neptune SigV4）、`ap-northeast-1` 区域 Bedrock 访问权。

---

## ⚠️ 注意事项

1. *L1 之后生产代码已动*：`rca/engines/`、`rca/neptune/nl_query_*.py` 均为本项目产出；本目录只保留 POC 脚本
2. *主环境已装 strands*：`/usr/bin/pip3 install --user --break-system-packages 'strands-agents>=1.36' 'strands-agents-tools>=0.5'`；备份 `~/backups/pip-freeze-before-strands-20260418.txt`
3. *线上 Streamlit 默认 strands*：`/etc/systemd/system/streamlit-demo.service.d/nlquery.conf` 固化了 `NLQUERY_ENGINE=strands`
4. *`.venv/` 有 248MB*：已加入 .gitignore，不要 commit
5. *策略文档只有一份*：`migration-strategy.md` 是唯一权威；`archive/` 仅作审计

---

## 🔗 相关文档

### 本仓库内
- `tests/golden/petsite.yaml` + `tests/test_golden_accuracy.py` — Smart Query Golden CI
- `tests/golden/BASELINE-direct.md` / `BASELINE-strands.md` — 最新准确率 + 缓存命中率基线
- `rca/neptune/nl_query_direct.py` — Direct 引擎实现（Wave 1-5 + L2 caching）
- `rca/neptune/nl_query_strands.py` — Strands 引擎实现（L1 + L2 caching）
- `rca/neptune/nl_query.py` — 向后兼容 shim（Phase 4 删除）
- `rca/engines/base.py` / `factory.py` / `strands_common.py` — Engine 抽象层（Phase 1 地基）
- `rca/neptune/strands_tools.py` — 3 个 Strands `@tool`
- `profiles/petsite.yaml` — PetSite 配置化 schema + few-shot + guard
- `docs/migration/timeline.md` — 7 模块迁移时间线
- `scripts/check_migration_deadlines.py` + `.github/workflows/migration-checks.yml` — 冻结/删除日 CI

### 仓库外（技术内部文档，位于 `~/tech/blog/gp-improve/`）
- `smart-query-accuracy-improvement.md` — Smart Query Wave 1-5 源计划（已落地）
- `harness-resilience-testing/improvements.md` — Resilience Score / Probe / GameDay 增量清单
