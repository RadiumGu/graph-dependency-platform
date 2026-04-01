[English](./README.md) | 中文文档

# Graph Dependency Platform

面向 AWS EKS 微服务的生产级 **可观测 → 知识图谱 → 智能根因分析 → 混沌验证 → 容灾计划** 完整闭环平台。

以 [PetSite](https://github.com/aws-samples/one-observability-demo)（运行在 EKS ARM64 Graviton3 上的多语言微服务应用）为目标系统，平台持续从实时流量和基础设施拓扑构建 Neptune 知识图谱，在告警触发时执行 AI 驱动的根因分析，通过 AI 驱动的混沌实验验证系统韧性，并基于图谱自动生成容灾切换计划。

---

## 平台架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                   PetSite on AWS EKS (ARM64 Graviton3)               │
│    petsite / petsearch / pethistory / payforadoption / petfood …     │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ 流量 & 指标
            ┌──────────────────▼──────────────────┐
            │         infra/（CDK + ETL）           │
            │   EKS + DeepFlow + Neptune + ALB     │
            │   模块化 ETL（事件驱动同步）            │
            │   → Neptune 知识图谱                  │
            └────┬─────────────────────┬───────────┘
                 │ 图谱查询            │ CW Alarm 触发
                 │                     │
      ┌──────────▼──────────┐  ┌──────▼───────────────────┐
      │      rca/            │  │       chaos/              │
      │  多层根因分析引擎     │  │  AI 驱动混沌工程平台       │
      │  + Layer2 探针       │  │  (Chaos Mesh + AWS FIS)   │
      │  + Graph RAG 报告    │  │                           │
      └──────────┬──────────┘  └──────┬────────────────────┘
                 │  事件写回           │  验证 RCA 准确性
                 └──────────┬──────────┘
                            │ 闭环验证
                 ┌──────────▼──────────┐
                 │  dr-plan-generator/  │
                 │  图谱驱动容灾计划     │
                 │  生成器              │
                 └─────────────────────┘
```

### 数据流

1. **infra/** — 模块化 ETL 持续采集基础设施拓扑到 Neptune（171+ 节点，19 种边类型）
2. 真实或注入的故障触发 CloudWatch Alarm → **rca/** Lambda 激活
3. **rca/** 执行多层分析（DeepFlow L7/L4 + CloudTrail + Neptune 图遍历 + Layer2 AWS 服务探针）→ 通过 Bedrock Claude 生成 Graph RAG 报告
4. **chaos/** HypothesisAgent 基于 Neptune 图谱生成假设 → 5 Phase 实验引擎注入故障 → 验证 RCA 准确性 → LearningAgent 闭环学习
5. **dr-plan-generator/** 查询 Neptune 图谱，自动生成有序、可执行的 DR 切换计划，附带回滚指令和 RTO/RPO 估算

---

## 仓库结构

```
graph-dependency-platform/
├── infra/          # 基础设施与数据层（原 graph-dp-cdk）
│   ├── bin/        #   CDK 应用入口
│   ├── lib/        #   CDK 栈（NeptuneClusterStack + NeptuneEtlStack）
│   ├── lambda/     #   ETL Lambda 函数（etl_aws / etl_deepflow / etl_cfn / etl_trigger）
│   └── _configs/   #   EKS & DeepFlow 部署配置
│
├── rca/            # 智能根因分析引擎（原 graph-rca-engine）
│   ├── core/       #   多层 RCA 引擎 + 故障分级 + Graph RAG 报告生成
│   ├── neptune/    #   Neptune 查询库（Q1–Q18，openCypher）+ 自然语言查询引擎 + Schema Prompt
│   ├── collectors/ #   Layer2 AWS 服务探针 + 基础设施采集器 + EKS 认证
│   ├── actions/    #   故障手册 + 半自动修复 + Slack 通知
│   ├── search/     #   S3 Vectors Incident 语义搜索
│   ├── scripts/    #   CLI 工具（graph-ask.py — 自然语言图谱查询）
│   └── deploy.sh   #   Lambda 部署脚本
│
├── chaos/          # AI 驱动混沌工程（原 graph-driven-chaos）
│   └── code/
│       ├── agents/       # HypothesisAgent + LearningAgent
│       ├── runner/       # 5 Phase 实验引擎 + FIS/ChaosMesh 后端
│       ├── experiments/  # 实验 YAML 模板（tier0 / tier1 / fis）
│       ├── infra/        # FIS IAM + 告警配置
│       ├── neptune_sync.py  # Neptune 同步：ChaosExperiment 节点 + TestedBy 边（新增）
│       └── fmea/         # FMEA 故障模式分析
│
├── dr-plan-generator/  # 图谱驱动容灾计划生成器（新增）
│   ├── graph/          #   Neptune 查询（Q12–Q16）+ 依赖分析器
│   ├── planner/        #   计划生成（Phase 0–4）+ 回滚 + 步骤构建
│   ├── assessment/     #   影响评估 + RTO 估算 + SPOF 检测
│   ├── validation/     #   静态验证 + chaos 实验导出
│   ├── output/         #   Markdown / JSON / LLM 摘要渲染
│   ├── examples/       #   预生成示例计划（AZ + Region）
│   └── AGENT.md        #   通用 AI Agent 指令
│
└── shared/         # 共享配置与工具
```

---

## 1. infra/ — 基础设施与数据层

**CDK 全栈部署 + 模块化 Neptune ETL 管道。**

通过 TypeScript CDK 部署所有 AWS 资源（EKS、DeepFlow、Neptune、ALB、Lambda），运行多源 ETL 管道在 Neptune 中构建实时知识图谱。

### ETL 数据源

| 数据源 | Lambda | 调度 | 采集内容 |
|--------|--------|------|---------|
| DeepFlow / ClickHouse | `neptune-etl-from-deepflow` | 每 5 分钟 | L7 服务调用拓扑、延迟、错误率 |
| AWS API | `neptune-etl-from-aws` | 每 15 分钟 | 静态基础设施拓扑（EC2、EKS、ALB、RDS、Lambda、SQS、S3 …） |
| CloudFormation | `neptune-etl-from-cfn` | 每日 + 部署时 | 声明的依赖边（`DependsOn`、环境变量引用） |
| AWS 事件 | `neptune-etl-trigger` | 实时（SQS） | 基础设施变更的增量同步 |

### 核心技术亮点

- **ARM64 全栈**：EKS + DeepFlow 运行在 Graviton3，成本降低约 40%
- **eBPF 零侵扰**：DeepFlow Agent 作为 DaemonSet 自动采集，无需修改应用代码
- **CDK 基础设施即代码**：从 VPC 到 Neptune Cluster 到 Lambda 全部代码化
- **事件驱动同步**：SQS 缓冲的 EventBridge 规则实现近实时图谱更新
- **模块化 ETL**：从 2,124 行单文件重构为 collector 插件架构

📖 **详细文档**：[`infra/README.md`](infra/README.md) | [`infra/README_CN.md`](infra/README_CN.md)

---

## 2. rca/ — 智能根因分析引擎

**CloudWatch Alarm → 多层 RCA → Graph RAG 报告 → Slack 通知 → 半自动修复。**

### 多层 RCA 流水线

```
CloudWatch Alarm (5XX > 阈值)
    │
    ▼ SNS
handler.py → fault_classifier (P0/P1/P2)
    │
    ├─ Step 1:  DeepFlow L7（HTTP 5xx 调用链）
    ├─ Step 1b: DeepFlow L4（TCP RST / 超时 / SYN 重传）
    ├─ Step 2:  CloudTrail 变更事件
    ├─ Step 3:  Neptune 图遍历（Q1–Q18）
    │           ├─ 服务调用链（Calls / DependsOn）
    │           ├─ 基础设施：Service → Pod → EC2 → AZ
    │           └─ 爆炸半径扩展
    ├─ Step 3d: Layer2 AWS 服务探针（并行执行）
    │           ├─ SQS / DynamoDB / Lambda / ALB / StepFunctions / EC2ASG
    └─ Step 4:  置信度评分（满分 100）
    │
    ▼
Graph RAG 报告（Bedrock Claude + Neptune 子图）
    │
    ▼
Slack 通知 + 半自动修复 + 事件归档
```

### Layer2 AWS 服务探针

插件化探针框架，覆盖 Neptune 拓扑无法感知的 AWS 托管服务故障：

| 探针 | 检测内容 | 设计特点 |
|------|---------|---------|
| **SQSProbe** | 队列积压、DLQ 堆积 | `@register_probe` 装饰器自动注册 |
| **DynamoDBProbe** | 读写限流、系统错误 | `ThreadPoolExecutor` 并行执行（12s 超时） |
| **LambdaProbe** | 错误率、限流、超时 | 统一 `ProbeResult` 输出 |
| **ALBProbe** | ELB 5XX、拒绝连接、不健康目标 | 探针结果自动融入评分 |
| **StepFunctionsProbe** | 执行失败、超时、限流 | 新增探针零侵入核心引擎 |
| **EC2ASGProbe** | 实例异常、ASG 容量异常 | 仅基础设施故障 fallback |

### 故障分级与响应策略

| 严重度 | 触发条件 | 执行策略 |
|--------|---------|---------|
| **P0** | Tier0 服务 + 高错误率 | 禁止自动，先诊断，人工确认 |
| **P1** | Tier0/1 + 中等影响 | Suggest 模式 + Slack 按钮确认 |
| **P2** | Tier1/2 + 低影响 | LOW 风险全自动执行 |

### Neptune 查询库（Q1–Q18）

| 查询 | 用途 | 层 |
|------|------|---|
| Q1–Q8 | 爆炸半径、Tier0 状态、上游依赖、服务信息、故障记录、Pod 状态、DB 连接、完整依赖子图 | 服务层 |
| Q9–Q11 | 服务→Pod→EC2→AZ 路径、非 running EC2 检测、跨服务爆炸半径 | 基础设施层 |
| Q17 | 涉及相同资源的历史 Incident（`MentionsResource` 边） | 非结构化 |
| Q18 | 服务混沌实验历史（`TestedBy` 边） | 非结构化 |

### 自然语言图谱查询

用中文提问，NL 查询引擎通过 Bedrock Claude 生成 openCypher，执行后返回结果与摘要：

```bash
cd rca
python3 scripts/graph-ask.py "petsite 的所有下游依赖有哪些？"
python3 scripts/graph-ask.py "哪些 Tier0 服务没做过混沌实验？"
python3 scripts/graph-ask.py "最近发生了几次 P0 故障？"
```

- **`rca/neptune/schema_prompt.py`** — 图谱 Schema 硬编码 + 6 个 few-shot 示例
- **`rca/neptune/nl_query.py`** — `NLQueryEngine`：LLM→openCypher→执行→摘要
- **`rca/neptune/query_guard.py`** — 安全校验：屏蔽写操作、限制跳数、强制 LIMIT

### Incident 语义搜索（S3 Vectors）

RCA 报告自动分块 + 向量化（Bedrock Titan v2）+ 写入 S3 Vectors 索引，下次 RCA 时语义召回相似历史故障注入 Graph RAG 上下文。

- 成本：**< $0.02/月**（vs OpenSearch Serverless ~$30+/月）
- 实现：`rca/search/incident_vectordb.py`

📖 **详细文档**：[`rca/README.md`](rca/README.md) | [`rca/README_CN.md`](rca/README_CN.md)

---

## 3. chaos/ — AI 驱动混沌工程平台

**AI 假设生成 → 5 Phase 实验引擎 → 双后端（Chaos Mesh + AWS FIS）→ 闭环学习。**

### AI Agent 系统

#### HypothesisAgent（假设生成）
- **输入**：Neptune 图谱拓扑 + TargetResolver 实时快照 + 已有实验覆盖
- **引擎**：Bedrock LLM（Claude）— 根据副本数、节点分布、资源类型自动调整参数
- **输出**：`hypotheses.json`，含优先级评分（业务影响 × 爆炸半径 × 可行性 × 学习价值）

#### LearningAgent（闭环学习）
- 读取 DynamoDB 实验历史，按服务聚合分析
- 识别重复故障模式、覆盖缺口、趋势变化
- LLM 生成改进建议 + 迭代假设库
- 更新 Neptune 图谱属性（resilience_score / failure_pattern）

#### Orchestrator（批量编排）
- 支持顺序 / 并行执行
- 4 种策略：`by_tier` / `by_priority` / `by_domain` / `full_suite`
- 标签过滤、冷却间隔、快速失败

### 5 Phase 实验引擎

| Phase | 名称 | 操作 |
|-------|------|------|
| 0 | 预检 | 目标解析、健康检查、残留实验检测 |
| 1 | 稳态基线 | 采集基线（DeepFlow 成功率 + P99 延迟） |
| 2 | 故障注入 | Chaos Mesh CRD 或 FIS 实验 |
| 3 | 观测 | 每 10s 采样、Stop Condition 护栏、自动熔断 |
| 4 | 恢复 | 等待恢复、轮询状态（300s 超时） |
| 5 | 报告 | Markdown 报告 + DynamoDB + LLM 分析 + CloudWatch Metrics |

### 双后端架构

| 后端 | 覆盖范围 | 故障类型 |
|------|---------|---------|
| **Chaos Mesh** | K8s 层 | 24 种已验证（Pod/网络/HTTP/DNS/IO/CPU/内存/时间/内核） |
| **AWS FIS** | AWS 托管服务层 | 15 种（Lambda/RDS/EKS Node/EBS/VPC 网络） |

📖 **详细文档**：[`chaos/code/README.md`](chaos/code/README.md) | [`chaos/README.md`](chaos/README.md)

---

## 4. dr-plan-generator/ — 图谱驱动容灾计划生成器

**Neptune 图谱 → 依赖分析 → 有序切换计划 → 回滚计划 → chaos 验证导出。**

通过查询 Neptune 知识图谱的依赖关系，对资源进行拓扑排序，自动生成分层、可执行的容灾切换计划。

### 切换阶段

| Phase | 名称 | 操作 |
|-------|------|------|
| 0 | 预检 | 目标连通性、Replication Lag 验证、DNS TTL 降低 |
| 1 | 数据层切换 | RDS/Aurora failover、DynamoDB Global Table 切换、SQS 端点更新 |
| 2 | 计算层切换 | EKS 工作负载按 Tier 顺序扩容、Lambda 验证、健康检查 |
| 3 | 流量层切换 | ALB 健康确认、Route 53 DNS 切换 |
| 4 | 切换后验证 | 端到端验证、性能基线对比 |

### 核心能力

- **拓扑排序**（Kahn's 算法）：确保层内依赖顺序正确
- **并行组检测**：识别可并发执行的步骤
- **单点故障检测**：标记单 AZ 部署且被多服务依赖的资源
- **RTO/RPO 估算**：基于资源类型默认时间 + 并行优化
- **每步都有回滚**：不生成无回滚命令的步骤
- **Chaos 导出**：将 DR 假设转为 chaos 实验 YAML，与 `chaos/` 联动验证

### Neptune 查询（Q12–Q16）

| 查询 | 用途 |
|------|------|
| Q12 | AZ/Region 依赖树 |
| Q13 | 数据层拓扑（所有数据存储 + 依赖服务） |
| Q14 | 跨 Region 资源（Global Table、副本） |
| Q15 | 关键路径（Tier0 最长依赖链 → 最小 RTO） |
| Q16 | 单点故障检测 |

### AI Agent 集成

`AGENT.md` 提供通用 Agent 指令，适用于 OpenClaw / Claude Code / kiro-cli，引导用户交互式完成影响评估 → 计划生成 → 回滚 → chaos 验证。

### 示例计划

| 示例 | 场景 | 步骤 | RTO |
|------|------|------|-----|
| [AZ 切换](dr-plan-generator/examples/az-switchover-apne1-az1.md) | AZ1 → AZ2+AZ4 | 19 + 15 回滚 | ~34min |
| [Region 切换](dr-plan-generator/examples/region-switchover-apne1-to-usw2.md) | 东京 → 美西 | 28 + 23 回滚 | ~55min |

📖 **详细文档**：[`dr-plan-generator/README.md`](dr-plan-generator/README.md) | [`dr-plan-generator/README_CN.md`](dr-plan-generator/README_CN.md)

---

## 关键指标

| 指标 | 数值 |
|------|------|
| Neptune 知识图谱节点 | **171+** |
| Neptune 节点类型 | **23 种** |
| Neptune 边类型 | **19 种** |
| Neptune 查询库 | **Q1–Q18（18 个查询）** |
| Chaos Mesh 已验证工具 | **30 个** |
| AWS FIS 故障类型 | **15 种** |
| Layer2 AWS 服务探针 | **6 个** |
| RCA 触发延迟 | **< 1 分钟** |
| ETL 同步周期 | 5min（DeepFlow）+ 15min（AWS）+ 实时（事件） |
| 功能测试通过率 | **47/47** |

---

## 核心 AWS 资源

| 资源 | 标识 | 用途 |
|------|------|------|
| EKS 集群 | `petsite-cluster`（ARM64 Graviton3） | 微服务运行环境 |
| Neptune | `petsite-neptune` | 知识图谱存储 |
| Lambda | `petsite-rca-engine` | RCA 主引擎 |
| Lambda（×4） | `neptune-etl-from-*` | ETL 管道 |
| Bedrock | Claude Sonnet 4.6 | Graph RAG 报告 + 混沌实验 LLM 分析 + NL 图谱查询 |
| S3 Vectors | `gp-incident-kb` | Incident 语义搜索索引 |
| DynamoDB | `chaos-experiments` | 混沌实验历史 |
| Region | `ap-northeast-1`（东京） | 主部署区域 |

---

## 环境要求

| 依赖 | 说明 |
|------|------|
| AWS 账号 | 已部署 VPC、EKS 集群、DeepFlow/ClickHouse |
| Amazon Neptune | 由 `infra/` 中的 `NeptuneClusterStack` 创建 |
| Node.js ≥ 18 | CDK CLI |
| Python ≥ 3.12 | Lambda 运行时 + chaos CLI |
| AWS CDK v2 | `npm install -g aws-cdk` |
| kubectl ≥ 1.28 | Chaos Mesh CRD 注入 |
| Chaos Mesh ≥ 2.6 | 部署在 EKS 集群上 |

---

## 快速开始

### 1. 部署基础设施（infra/）

```bash
cd infra
npm install
# 在 cdk.json 中配置 VPC、Neptune、ClickHouse、EKS 参数
cdk deploy NeptuneClusterStack
cdk deploy NeptuneEtlStack
```

### 2. 部署 RCA 引擎（rca/）

```bash
cd rca
cp .env.example .env
# 填写 Neptune endpoint、EKS 集群、ClickHouse 地址、Bedrock KB ID
bash deploy.sh
```

### 3. 运行混沌实验（chaos/）

```bash
cd chaos/code
pip install boto3 pyyaml requests structlog

# 配置 FIS 基础设施
python3 infra/fis_setup.py
python3 main.py setup

# AI 生成假设 → 执行实验 → 闭环学习
python3 main.py auto --max-hypotheses 5 --top 3 --dry-run
```

---

## 设计文档

| 文档 | 位置 |
|------|------|
| 架构设计（v18） | [`infra/_docs/ARCHITECTURE-v18.md`](infra/_docs/ARCHITECTURE-v18.md) |
| 基础设施 TDD | [`infra/_docs/TDD.md`](infra/_docs/TDD.md) |
| RCA 系统文档 | [`rca/docs/RCA-SYSTEM-DOC.md`](rca/docs/RCA-SYSTEM-DOC.md) |
| 混沌工程 PRD | [`chaos/docs/prd.md`](chaos/docs/prd.md) |
| 混沌工程 TDD | [`chaos/docs/tdd.md`](chaos/docs/tdd.md) |
| 功能测试报告 | [`chaos/docs/test-report-2026-03-20.md`](chaos/docs/test-report-2026-03-20.md) |

---

## 历史

本 monorepo 由三个独立项目合并而来（完整 Git 历史已保留）：

| 原始仓库 | → 目录 | 状态 |
|---------|--------|------|
| [RadiumGu/ETL-Neptune](https://github.com/RadiumGu/ETL-Neptune) | `infra/` | 已归档 |
| [RadiumGu/graph-rca-engine](https://github.com/RadiumGu/graph-rca-engine) | `rca/` | 已归档 |
| [RadiumGu/graph-driven-chaos](https://github.com/RadiumGu/graph-driven-chaos) | `chaos/` | 已归档 |
| [RadiumGu/graph-dependency-managerment](https://github.com/RadiumGu/graph-dependency-managerment) | `infra/`（镜像） | 已归档 |

---

## 许可证

MIT
