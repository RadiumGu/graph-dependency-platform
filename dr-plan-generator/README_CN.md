[English](./README.md) | 中文文档

# DR Plan Generator

基于知识图谱的智能容灾切换计划生成器 — 从 Neptune 依赖图谱自动生成有序、可执行、可回滚的容灾切换计划。

## 概述

当 Region 或可用区故障时，几十个相互依赖的微服务、数据库、队列、负载均衡器需要**按正确顺序切换**。切错顺序可能导致数据不一致或服务雪崩。

DR Plan Generator 利用 Neptune 依赖图谱（由 `infra/` ETL 管道构建）来：

1. **分析**故障爆炸半径（哪些服务、数据库、资源受影响）
2. **排序**资源的依赖顺序（Kahn's 拓扑排序算法）
3. **生成**分层切换计划：数据层 → 计算层 → 流量层
4. **附带**每一步的 AWS CLI / kubectl 命令、验证检查和回滚指令
5. **检测**单点故障（SPOF）并估算 RTO/RPO

## 架构

```
Neptune 知识图谱（171+ 节点，17 种边类型）
        │
        ▼
┌─────────────────────────────────────────┐
│           dr-plan-generator              │
│                                          │
│  graph/       → Neptune 查询（Q12-Q16）   │
│  graph/       → 依赖分析                  │
│                （拓扑排序、SPOF、分层）      │
│  planner/     → 计划生成                  │
│                （Phase 0-4 + 回滚）        │
│  assessment/  → 影响评估 & RTO 估算       │
│  validation/  → 静态验证 + chaos 导出     │
│  output/      → Markdown / JSON / LLM    │
└─────────────────────────────────────────┘
        │
        ▼
  可执行 DR 计划（Markdown + JSON）
  + 回滚计划
  + Chaos 验证实验
```

## 切换阶段

每个生成的计划都遵循严格的分层顺序：

| Phase | 名称 | 操作 |
|-------|------|------|
| 0 | 预检 | 目标连通性、Replication Lag 验证、DNS TTL 降低 |
| 1 | 数据层切换 | RDS/Aurora failover、DynamoDB Global Table 切换、SQS/S3 端点更新 |
| 2 | 计算层切换 | EKS 工作负载按 Tier 顺序扩容、Lambda 验证、健康检查 |
| 3 | 流量层切换 | ALB 健康确认、Route 53 DNS 切换、CDN 源站更新 |
| 4 | 切换后验证 | 端到端验证、性能基线对比、监控确认 |

**回滚**按反向顺序：流量 → 计算 → 数据，所有步骤需审批。

## 快速开始

### 安装

```bash
cd dr-plan-generator
pip install -r requirements.txt
export NEPTUNE_ENDPOINT=<你的 Neptune 端点>
export REGION=ap-northeast-1
```

### 生成计划

```bash
# AZ 级切换
python3 main.py plan --scope az --source apne1-az1 --target apne1-az2,apne1-az4

# Region 级切换
python3 main.py plan --scope region --source ap-northeast-1 --target us-west-2

# 排除指定服务
python3 main.py plan --scope az --source apne1-az1 --target apne1-az2 --exclude petfood

# JSON 输出
python3 main.py plan --scope az --source apne1-az1 --target apne1-az2 --format json
```

### 影响评估

```bash
python3 main.py assess --scope az --failure apne1-az1
```

### 验证计划

```bash
python3 main.py validate --plan plans/dr-az-xxx.json
```

### 生成回滚计划

```bash
python3 main.py rollback --plan plans/dr-az-xxx.json
```

### 导出 chaos 验证实验

```bash
python3 main.py export-chaos --plan plans/dr-az-xxx.json --output ../chaos/code/experiments/dr-validation/
```

## CLI 参考

| 命令 | 说明 |
|------|------|
| `plan` | 生成 DR 切换计划 |
| `assess` | 对故障场景进行影响评估 |
| `validate` | 验证已有计划 JSON（退出码 0=通过，1=失败） |
| `rollback` | 从已有计划生成回滚计划 |
| `export-chaos` | 导出计划假设为 chaos 实验 YAML |

## 核心特性

### 拓扑排序（Kahn's 算法）

每层内按依赖顺序排列资源。无相互依赖的同层资源自动标记为可并行执行。

### 单点故障检测

自动识别只部署在单个 AZ 且被多个服务依赖的资源，在计划中标记为 ⚠️ 警告。

### 每步都有回滚

不生成没有回滚命令的步骤。回滚计划反转 Phase 顺序，所有步骤标记为需要审批。

### Neptune 查询（Q12–Q16）

| 查询 | 用途 |
|------|------|
| Q12 | AZ/Region 依赖树 — 所有资源及依赖链 |
| Q13 | 数据层拓扑 — 所有数据存储及其依赖服务 |
| Q14 | 跨 Region 资源 — Global Table、跨 Region 副本 |
| Q15 | 关键路径 — Tier0 最长依赖链（决定最小 RTO） |
| Q16 | 单点故障 — 单 AZ 部署且被多服务依赖的资源 |

## AI Agent 集成

`AGENT.md` 提供通用 Agent 指令，适用于 OpenClaw / Claude Code / kiro-cli，引导用户交互式生成 DR 计划。详见 [AGENT.md](./AGENT.md)。

## 示例

基于 PetSite 拓扑的预生成示例：

| 示例 | 场景 | 服务 | 步骤 | RTO |
|------|------|------|------|-----|
| [AZ 切换](examples/az-switchover-apne1-az1.md) | AZ1 → AZ2+AZ4 | 7 服务 / 14 资源 | 19 + 15 回滚 | ~34min |
| [AZ 排除切换](examples/az-switchover-exclude-petfood.md) | AZ1 → AZ2+AZ4（排除 petfood） | 6 服务 / 13 资源 | 18 + 14 回滚 | ~32min |
| [Region 切换](examples/region-switchover-apne1-to-usw2.md) | 东京 → 美西 | 7 服务 / 22 资源 | 28 + 23 回滚 | ~55min |

## 项目结构

```
dr-plan-generator/
├── main.py                  # CLI 入口
├── models.py                # 数据模型（DRPlan, DRPhase, DRStep, ImpactReport）
├── config.py                # 配置（环境变量）
├── AGENT.md                 # 通用 AI Agent 指令
├── graph/                   # Neptune 图谱层
│   ├── neptune_client.py    # openCypher + SigV4 客户端
│   ├── queries.py           # DR 查询（Q12–Q16）
│   └── graph_analyzer.py    # 拓扑排序、层级分类、SPOF、环路检测
├── planner/                 # 计划生成层
│   ├── plan_generator.py    # 计划生成引擎（Phase 0–4）
│   ├── step_builder.py      # 各资源类型命令生成
│   └── rollback_generator.py# 回滚计划生成
├── assessment/              # 评估层
│   ├── impact_analyzer.py   # 影响评估
│   ├── rto_estimator.py     # RTO/RPO 估算
│   └── spof_detector.py     # 单点故障检测
├── validation/              # 验证层
│   ├── plan_validator.py    # 静态验证
│   └── chaos_exporter.py    # 导出 chaos 实验
├── output/                  # 输出层
│   ├── markdown_renderer.py # Markdown 输出
│   ├── json_renderer.py     # JSON 输出
│   └── summary_generator.py # LLM 执行摘要（Bedrock Claude）
├── examples/                # 预生成示例
├── tests/                   # 70 个单元测试
└── docs/
    ├── prd.md               # 产品需求文档
    └── tdd.md               # 技术设计文档
```

## 测试

```bash
python3 -m pytest tests/ -v
# 70 个测试：图谱分析器 (32) + 步骤构建器 (23) + 计划验证器 (15)
```

## 环境变量

| 变量 | 必须 | 默认值 | 说明 |
|------|------|--------|------|
| `NEPTUNE_ENDPOINT` | 是* | — | Neptune 集群端点 |
| `NEPTUNE_PORT` | 否 | `8182` | Neptune 端口 |
| `REGION` | 否 | `ap-northeast-1` | AWS Region |
| `BEDROCK_MODEL` | 否 | `global.anthropic.claude-sonnet-4-6` | LLM 摘要生成模型 |

*使用 mock 数据（examples、tests）时不需要。
