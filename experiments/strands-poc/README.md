# Strands Agents POC & Migration

> AWS Strands Agents 在 graph-dependency-platform 的可行性验证 + 全平台迁移策略。
>
> 📅 2026-04-18 | 🔍 架构审阅猫

---

## 📖 应该读什么（按顺序）

### 1. `report.md` — L0 Spike 验证结果 ✅
Strands 能否在本项目落地的 6 个硬约束逐项验证。**结论：L1 POC GO（有条件）**。

### 2. `migration-strategy.md` — 正式迁移策略
Strangler Fig 单向迁移：direct 代码冻结 → 删除，终态单一 Strands 实现。
5 Phase / 21-27 周 / 6 模块串行，含冻结日 + 删除日 CI 强制机制。

---

## 🗂 目录内容

### 策略文档
- `report.md` — L0 Spike 报告（必读）
- `migration-strategy.md` — 迁移策略（启动 L1 前读）
- `TASK-L1-smart-query.md` — 给编程猫的 L1 POC 任务指令

### POC 代码（可执行，验证 Strands 可行性）
- `spike.py` — Strands Agent POC（3 个 @tool + BedrockModel + guard）
- `baseline.py` — 现版 NLQueryEngine 同题对跑
- `golden_questions.yaml` — 3 条测试问题
- `run_spike.sh` / `run_baseline.sh` — 一键执行脚本
- `requirements.txt` — Strands 依赖

### 运行结果（L0 Spike 产出）
- `spike_result.json` — Strands 版完整 trace（tool-call / 延迟 / 摘要）
- `baseline_result.json` — direct 版对比数据

### 归档
- `archive/migration-strategy-v2-dual-track.md` — 早期双轨方案（4 方案对比），已弃用；保留作决策历史

---

## 🚀 如何复跑 POC

```bash
cd experiments/strands-poc
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
bash run_spike.sh      # 跑 Strands 版
bash run_baseline.sh   # 跑 direct 版
```

需要：
- AWS 凭证（Bedrock + Neptune SigV4）
- `NEPTUNE_ENDPOINT` 环境变量（默认 petsite 集群）
- `ap-northeast-1` 区域 Bedrock 访问权

---

## ⚠️ 注意事项

1. **生产代码零改动** — 本目录独立，不污染 `rca/` / `profiles/` / `.env`
2. **只验证可行性** — 不是生产实现；L1 POC 开始才进入工程化
3. **`.venv/` 有 248MB** — 已加入 .gitignore（如果没有请补上），不要 commit
4. **策略文档只有一份** — `migration-strategy.md` 是唯一权威版本。`archive/` 下的旧版只作审计参考，**不要**基于它制定决策

---

## 🔗 相关文档

### 本仓库内
- `tests/golden/petsite.yaml` + `tests/test_golden_accuracy.py` — Smart Query Golden CI（L1 POC 直接复用）
- `tests/golden/BASELINE.md` — 当前准确率基线（Wave 1-5 产出，20/20 = 100%）
- `rca/neptune/nl_query.py` — Smart Query 引擎当前实现（Wave 1-5 后）
- `profiles/petsite.yaml` — PetSite 的配置化 schema + few-shot + guard 规则

### 仓库外（技术内部文档，位于 `~/tech/blog/gp-improve/`）
- `smart-query-accuracy-improvement.md` — Smart Query Wave 1-5 源计划（已落地）
- `harness-resilience-testing/improvements.md` — Resilience Score / Probe / GameDay 增量能力清单
