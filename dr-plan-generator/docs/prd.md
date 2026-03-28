# DR Plan Generator — 产品需求文档（PRD）

> 版本：v1.1  
> 日期：2026-03-28  
> 状态：草案  
> 作者：编程猫 + 大乖乖  
> 更新：v1.1 增加双层架构（CLI + Agent Instructions）设计

---

## 1. 背景与问题

### 1.1 现状

在复杂的 AWS 微服务架构中，容灾切换（Disaster Recovery Switchover/Failover）面临以下核心挑战：

- **依赖关系不透明**：几十个微服务、数据库、队列、缓存之间的调用和依赖关系复杂，人工梳理容易遗漏
- **切换顺序不确定**：先切数据库还是先切应用？切错顺序可能导致数据不一致或服务雪崩
- **手动 Runbook 容易过时**：人工编写的 DR Runbook 与实际架构脱节，每次架构变更都需要同步更新
- **无法快速评估影响范围**：某个 Region/AZ 不可用时，哪些服务受影响？影响链路有多长？缺乏量化评估
- **回滚计划缺失**：切换失败后怎么回滚？回滚的依赖顺序又是什么？

### 1.2 我们的优势

**现有平台已经解决了最难的部分 — 依赖关系的自动发现与持续维护。**

Neptune 知识图谱包含：
- 22 种节点类型（Region → AZ → VPC → Subnet → EC2 → Pod → Service → BusinessCapability）
- 17 种边类型（Calls, DependsOn, RunsOn, LocatedIn, AccessesData, WritesTo …）
- 171+ 节点，持续通过 ETL 自动更新（5min/15min/实时）
- 服务分级（Tier0/Tier1/Tier2）+ 恢复优先级排序
- 完整的基础设施拓扑链

**我们缺的只是：把图谱能力转化为可执行的 DR 切换计划。**

---

## 2. 产品定位

**DR Plan Generator** 是 Graph Dependency Platform 的第四个子项目，定位为：

> **基于知识图谱的智能容灾切换计划生成器** — 从 Neptune 依赖图谱自动生成有序、可执行、可回滚的容灾切换计划。

### 2.1 在平台中的位置

```
graph-dependency-platform/
├── infra/              # 基础设施 + ETL → 构建依赖图谱
├── rca/                # 根因分析 → 故障定位
├── chaos/              # 混沌工程 → 验证韧性
└── dr-plan-generator/  # 🆕 容灾计划 → 有序切换
```

**数据流**：
```
infra/ ETL → Neptune 知识图谱
                │
                ├──→ rca/   （告警时）诊断根因
                ├──→ chaos/ （平时）验证韧性
                └──→ dr-plan-generator/ （DR 演练/真实灾难时）生成切换计划
```

---

## 3. 目标用户与场景

### 3.1 目标用户

| 角色 | 使用场景 |
|------|---------|
| **SRE / 运维工程师** | DR 演练前生成切换计划，执行时按计划逐步操作 |
| **架构师** | 评估架构的容灾能力，发现单点故障和依赖瓶颈 |
| **管理层** | 查看 RTO/RPO 评估报告，了解业务影响范围 |

### 3.2 核心场景

| 场景 | 描述 | 优先级 |
|------|------|--------|
| **S1: Region 级容灾切换** | 主 Region（ap-northeast-1）不可用，需切换到备 Region | P0 |
| **S2: AZ 级容灾切换** | 单个可用区故障，需将工作负载迁移到其他 AZ | P0 |
| **S3: 服务级灰度切换** | 按服务/业务能力维度逐步切换，支持部分切换 | P1 |
| **S4: DR 演练计划生成** | 定期演练前自动生成最新切换计划 | P1 |
| **S5: 影响评估** | 给定故障范围，快速评估影响面和业务中断范围 | P1 |
| **S6: 回滚计划生成** | 切换失败时的反向回滚计划 | P0 |

---

## 4. 功能需求

### 4.1 核心功能

#### F1: 依赖图谱分析（Graph Analysis）

从 Neptune 知识图谱提取容灾相关的依赖关系，构建切换依赖树。

**输入**：
- 故障范围定义（Region / AZ / 指定服务列表）
- DR 目标（目标 Region / AZ）

**处理**：
- 查询 Neptune 获取受影响的所有节点和边
- 拓扑排序（Topological Sort）：根据依赖关系确定切换顺序
- 层级划分：数据层 → 基础设施层 → 应用层 → 流量层
- 识别关键路径（Critical Path）和并行可执行的切换组

**输出**：
- 依赖树可视化
- 受影响服务/资源完整列表
- 切换顺序的 DAG（有向无环图）

#### F2: 切换计划生成（Plan Generation）

基于依赖分析结果，生成分步骤、可执行的切换计划。

**计划结构**：
```
Phase 0: Pre-flight Check（预检）
  - 备站点健康检查
  - 数据同步状态验证（RDS replica lag、DynamoDB Global Table 同步状态）
  - DNS TTL 预降低

Phase 1: Data Layer（数据层切换）
  - Step 1.1: RDS/Aurora promote read replica → writer
  - Step 1.2: DynamoDB Global Table 切换写入端点
  - Step 1.3: S3 Cross-Region Replication 验证
  - 验证点：数据层可写可读

Phase 2: Compute Layer（计算层切换）
  - Step 2.1: EKS 工作负载启动/扩容（按 Tier 顺序）
  - Step 2.2: Lambda 函数验证
  - Step 2.3: 依赖服务健康检查（按拓扑顺序）
  - 验证点：所有 Tier0 服务 healthy

Phase 3: Network Layer（网络/流量层切换）
  - Step 3.1: ALB/NLB 健康检查确认
  - Step 3.2: Route 53 DNS 切换 / Global Accelerator 权重调整
  - Step 3.3: CDN 源站切换
  - 验证点：终端用户流量到达备站点

Phase 4: Validation（切换后验证）
  - 端到端功能验证
  - 性能基线对比
  - 监控告警确认
```

**每个 Step 包含**：
- 具体操作命令（AWS CLI / kubectl / API 调用）
- 预期结果与验证方法
- 预估执行时间
- 失败回滚指令
- 负责人/审批要求

#### F3: 回滚计划生成（Rollback Plan）

切换计划的镜像反转 — 按反向依赖顺序生成回滚步骤。

- 流量层 → 计算层 → 数据层（与切换相反）
- 每个步骤标注"回滚到哪个状态"
- 数据层回滚的特殊处理（避免数据丢失）

#### F4: 影响评估报告（Impact Assessment）

给定故障范围，生成影响评估报告。

**报告内容**：
- 受影响服务列表（按 Tier 分组）
- 受影响业务能力（BusinessCapability）
- 预估 RTO（基于切换步骤数和每步预估时间）
- 预估 RPO（基于数据同步配置）
- 关键依赖瓶颈识别（单 AZ 部署、无跨 Region 副本的数据库等）
- 风险评估矩阵

#### F5: 计划验证（Plan Validation）

**静态验证**：
- 依赖环路检测（不应存在切换环路）
- 完整性检查（所有受影响资源都包含在计划中）
- 顺序一致性（依赖的服务先于被依赖的服务切换）

**动态验证（与 chaos/ 联动）**：
- 将 DR 计划中的关键假设转化为混沌实验
- 验证备站点是否真的能承接流量
- 验证 RDS failover 时间是否在预期范围内

### 4.2 输出格式

| 格式 | 用途 |
|------|------|
| **Markdown** | 人工阅读、审批、打印 |
| **JSON** | 程序消费、自动化执行系统对接 |
| **YAML** | 与 chaos/ 实验模板格式一致，可转化为验证实验 |

### 4.3 CLI 接口设计

```bash
# 生成 Region 级 DR 切换计划
python3 main.py plan \
  --scope region \
  --source ap-northeast-1 \
  --target us-west-2

# 生成 AZ 级切换计划
python3 main.py plan \
  --scope az \
  --source apne1-az1 \
  --target apne1-az2,apne1-az4

# 生成指定服务的切换计划
python3 main.py plan \
  --scope service \
  --services petsite,petsearch,payforadoption \
  --target us-west-2

# 影响评估
python3 main.py assess \
  --scope az \
  --failure apne1-az1

# 计划验证
python3 main.py validate \
  --plan plans/dr-plan-2026-03-28.json

# 生成回滚计划
python3 main.py rollback \
  --plan plans/dr-plan-2026-03-28.json

# 导出为 chaos 验证实验
python3 main.py export-chaos \
  --plan plans/dr-plan-2026-03-28.json \
  --output ../chaos/code/experiments/dr-validation/
```

### 4.4 双层架构：CLI + Agent Instructions

DR 计划生成天然需要多轮交互（参数多且有依赖、需要基于图谱数据做建议、结果需要人审核调整）。采用 **CLI 核心 + 通用 Agent 指令** 的双层架构：

```
┌─────────────────────────────────────────────────────────┐
│  Agent Instructions（AGENT.md — 交互层）                  │
│  自然语言 → 理解意图 → 图谱分析建议 → 调用 CLI → 展示结果  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐               │
│  │ OpenClaw │  │Claude Code│  │ kiro-cli │  …通用        │
│  └──────────┘  └──────────┘  └──────────┘               │
└──────────────────────┬──────────────────────────────────┘
                       │ 调用
┌──────────────────────▼──────────────────────────────────┐
│  CLI 核心（main.py — 执行层）                             │
│  纯参数驱动 / --non-interactive / JSON+Markdown 输出      │
│  可独立运行 / 可 CI/CD 集成 / 可脚本调用                   │
└─────────────────────────────────────────────────────────┘
```

#### 关键设计原则

1. **一份 Agent 指令，多工具通用** — `AGENT.md` 是纯 Markdown，不绑定任何特定 AI 框架
2. **CLI 完全独立** — `main.py` 不知道调用者是人还是 AI，所有参数可命令行传入
3. **AI 是交互翻译层** — 把自然语言转成 CLI 参数，把 CLI 输出转成可读摘要
4. **无 AI 也能用** — `main.py plan --scope az --source apne1-az1 --non-interactive` 直接出结果

#### 各 AI 工具的加载方式

| AI 工具 | 加载方式 | 说明 |
|---------|---------|------|
| **OpenClaw** | `skills/dr-plan/SKILL.md` → 引用 `AGENT.md` | OpenClaw 的 SKILL.md 做薄封装，核心逻辑在 AGENT.md |
| **Claude Code** | `CLAUDE.md` 引用 | 项目 CLAUDE.md 中 include AGENT.md |
| **kiro-cli** | 项目上下文引用 | kiro 的 project context 中引用同一个文件 |
| **其他 Agent** | 直接读取 AGENT.md | 任何能读 Markdown 的 AI Agent 都可以使用 |

#### AGENT.md 交互流程

```
[用户] AZ1 挂了，帮我出切换计划
    │
    ▼
[Agent] Step 1: 理解需求 → scope=az, source=apne1-az1
    │
    ▼
[Agent] Step 2: 图谱分析
    │   运行: main.py assess --scope az --failure apne1-az1 --format json
    │   → "AZ1 有 5 个 EC2, 8 个 Pod, 2 个 RDS，影响 3 个 Tier0 服务"
    │   → "⚠️ petsite-db writer 只在 AZ1，建议切到 AZ2+AZ4"
    │   → 让用户确认目标、排除项、数据层策略
    │
    ▼
[用户] 确认，排除 petfood
    │
    ▼
[Agent] Step 3: 生成计划
    │   运行: main.py plan --scope az --source apne1-az1 \
    │          --target apne1-az2,apne1-az4 --exclude petfood
    │   → "计划已生成，4 Phase、23 Step，预估 RTO 12 分钟"
    │
    ▼
[Agent] Step 4: 迭代调整（用户可要求修改）
    │
    ▼
[Agent] Step 5: 可选后续
    │   → 回滚计划 / chaos 验证导出 / 计划验证
```

---

## 5. 技术设计要点

### 5.1 Neptune 查询扩展

现有 Q1–Q11 需要扩展以支持 DR 场景：

| 新查询 | 功能 |
|--------|------|
| **Q12** `q12_az_dependency_tree` | 给定 AZ，查询所有部署在该 AZ 的资源及其上下游依赖链 |
| **Q13** `q13_data_layer_topology` | 所有数据存储（RDS/DynamoDB/S3/SQS）及其被哪些服务依赖 |
| **Q14** `q14_cross_region_resources` | 跨 Region 部署的资源（Global Table、Cross-Region Replica 等） |
| **Q15** `q15_critical_path` | Tier0 服务的最长依赖链（决定最小 RTO） |
| **Q16** `q16_single_point_of_failure` | 只部署在单个 AZ 且被多个服务依赖的资源 |

### 5.2 切换顺序算法

```
1. 从 Neptune 提取受影响子图
2. 将节点按层级分类：
   - L0: 数据层（RDS, DynamoDB, S3, SQS）
   - L1: 基础设施层（EC2, EKS NodeGroup）
   - L2: 应用层（Pod, K8sService, Microservice）
   - L3: 流量层（ALB, TargetGroup, Route53）
3. 同层内做拓扑排序（按 DependsOn/Calls 边方向）
4. 识别可并行执行的步骤组（无依赖关系的同层节点）
5. 为每个步骤生成具体操作命令 + 验证检查 + 回滚指令
```

### 5.3 与现有子项目的集成

| 集成 | 方式 |
|------|------|
| **infra/ (Neptune)** | 直接查询 Neptune 图谱（复用 `neptune_client_base.py`） |
| **infra/ (ETL)** | 依赖 ETL 保持图谱最新（ETL 频率决定计划的准确性） |
| **rca/** | 复用 Neptune 查询库（Q1–Q11）+ 共享 EKS 认证模块 |
| **chaos/** | 导出 DR 验证实验 → chaos 5-Phase 引擎执行 |

### 5.4 LLM 增强（Bedrock Claude）

- **计划审查**：将生成的计划交给 LLM 审查，识别潜在遗漏和风险
- **自然语言摘要**：为管理层生成易读的执行摘要和影响说明
- **历史学习**：从过去的 DR 演练记录中学习，优化时间估算和风险评估

---

## 6. 数据模型

### 6.1 DR Plan

```python
@dataclass
class DRPlan:
    plan_id: str                    # 唯一标识
    created_at: str                 # 生成时间
    scope: str                      # region / az / service
    source: str                     # 故障源（region/az/service 名称）
    target: str                     # 目标（备 region/az）
    affected_services: List[str]    # 受影响服务列表
    affected_resources: List[str]   # 受影响资源列表
    phases: List[DRPhase]           # 切换阶段列表
    rollback_phases: List[DRPhase]  # 回滚阶段列表（逆序）
    impact_assessment: ImpactReport # 影响评估
    estimated_rto: int              # 预估 RTO（分钟）
    estimated_rpo: int              # 预估 RPO（分钟）
    validation_status: str          # 验证状态
    graph_snapshot_time: str        # 图谱快照时间（标记数据新鲜度）

@dataclass
class DRPhase:
    phase_id: str
    name: str                       # e.g. "Data Layer Switchover"
    layer: str                      # L0/L1/L2/L3
    steps: List[DRStep]
    estimated_duration: int         # 预估耗时（分钟）
    gate_condition: str             # 进入下一 Phase 的门控条件

@dataclass
class DRStep:
    step_id: str
    order: int                      # 执行顺序
    parallel_group: Optional[str]   # 可并行的步骤组 ID
    resource_type: str              # e.g. "RDSCluster", "K8sService"
    resource_id: str                # Neptune 节点 ID
    resource_name: str              # 资源名称
    action: str                     # 具体操作类型
    command: str                    # AWS CLI / kubectl 命令
    validation: str                 # 验证命令
    expected_result: str            # 预期结果
    rollback_command: str           # 回滚命令
    estimated_time: int             # 预估耗时（秒）
    requires_approval: bool         # 是否需要人工审批
    tier: Optional[str]             # 服务 Tier（Tier0/1/2）
    dependencies: List[str]         # 前置步骤 ID 列表
```

---

## 7. 非功能需求

| 维度 | 要求 |
|------|------|
| **性能** | 单次计划生成 < 30 秒（包含 Neptune 查询 + LLM 调用） |
| **准确性** | 依赖关系准确性取决于 ETL 最后同步时间，计划中标注图谱新鲜度 |
| **可追溯** | 每次生成的计划保存到本地 + 可选 S3 归档 |
| **幂等性** | 相同输入 + 相同图谱状态 → 生成相同计划 |
| **离线可用** | Neptune 不可达时，支持从缓存的图谱快照生成计划（降级模式） |

---

## 8. 项目结构（预期）

```
dr-plan-generator/
├── main.py                     # CLI 入口
├── AGENT.md                    # 🆕 通用 Agent 指令（OpenClaw / Claude Code / kiro-cli 通用）
├── config.py                   # 配置（Neptune endpoint, Region, Bedrock model）
├── graph/
│   ├── neptune_client.py       # Neptune 查询客户端（复用 shared/）
│   ├── queries.py              # DR 专用查询（Q12–Q16）
│   └── graph_analyzer.py       # 依赖图分析（拓扑排序、层级划分、关键路径）
├── planner/
│   ├── plan_generator.py       # 切换计划生成主引擎
│   ├── rollback_generator.py   # 回滚计划生成
│   ├── step_builder.py         # 单步骤命令构建（按资源类型）
│   └── parallel_optimizer.py   # 并行优化（识别可并行步骤）
├── assessment/
│   ├── impact_analyzer.py      # 影响评估
│   ├── rto_estimator.py        # RTO 估算
│   └── spof_detector.py        # 单点故障检测
├── validation/
│   ├── plan_validator.py       # 静态验证（环路检测、完整性、顺序一致性）
│   └── chaos_exporter.py       # 导出为 chaos 验证实验
├── output/
│   ├── markdown_renderer.py    # Markdown 输出
│   ├── json_renderer.py        # JSON 输出
│   └── summary_generator.py    # LLM 生成执行摘要
├── plans/                      # 生成的计划输出目录
├── tests/
├── docs/
│   ├── prd.md                  # 本文档
│   └── tdd.md                  # 技术设计文档（待写）
├── requirements.txt
└── README.md
```

---

## 9. 里程碑

| 阶段 | 内容 | 预期 |
|------|------|------|
| **M1: 基础能力** | Neptune 图谱分析 + AZ 级切换计划生成 + Markdown 输出 | — |
| **M2: 完整切换** | Region 级切换 + 回滚计划 + 影响评估 + JSON 输出 | — |
| **M3: 智能增强** | LLM 审查 + 并行优化 + chaos 验证导出 | — |
| **M4: 自动化** | 定期生成 + S3 归档 + Slack 通知 + 计划 diff | — |

---

## 10. 风险与约束

| 风险 | 缓解措施 |
|------|---------|
| Neptune 图谱不完整（ETL 未覆盖的资源） | 计划中标注"图谱未覆盖资源"警告；支持手动补充依赖 |
| 跨 Region 资源信息不在图谱中 | M1 阶段新增 ETL collector 采集跨 Region 副本信息 |
| DR 目标 Region 无基础设施 | 计划生成时检测并报警；区分"热备"和"冷备"场景 |
| 命令执行权限不足 | 计划中标注所需 IAM 权限；支持 dry-run 模式只输出命令不执行 |
| 数据层切换的数据一致性风险 | Phase 0 预检中加入 replication lag 检查；不满足阈值时阻断切换 |

---

## 11. 成功指标

| 指标 | 目标 |
|------|------|
| 计划生成时间 | < 30 秒 |
| 依赖关系覆盖率 | ≥ 95%（对比人工梳理结果） |
| 切换顺序正确性 | 100%（无依赖违反） |
| DR 演练使用率 | 每次 DR 演练均使用自动生成计划 |

---

*本文档为初始版本，待讨论确认后进入 TDD 阶段。*
