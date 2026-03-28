# 📄 Product Requirement Document (PRD)
## Chaos Engineering Automation Platform

**版本**: 0.6  
**日期**: 2026-03-13  
**作者**: 爱吃肉  
**状态**: Draft  
**变更**: v0.6 RCA 三状态改造 + 报告 LLM 分析结论 + FIS 后端实现

---

## 1. Executive Summary

### 1.1 产品愿景

在 PetSite 可观测性平台（DeepFlow + Neptune + RCA）的基础上，构建一套**混沌工程自动化平台**，通过主动注入故障验证系统弹性，并闭环验证 RCA 系统的诊断准确性。

### 1.2 行业参考

#### Fidelity Chaos Buffet
Fidelity（富达投资）的 Chaos Buffet 是目前企业级混沌工程平台的标杆实践：支撑 21 个业务部门、3000+ 核心应用、累计执行超 12 万次故障测试。其核心设计理念：

- **不是故障注入工具，而是编排与管理控制平面**——底层 AWS FIS / Chaos Mesh 是插件，上层负责安全护栏、模板管理、流水线集成
- **安全护栏（Guardrails）是第一优先级**：每个实验强制绑定 Stop Conditions，超阈值自动熔断，
- **自助式模板库**：SRE 封装模板，普通开发者填参数即可运行，无需懂混沌工程原理
- **成熟度模型 Level 0→4**：从基础设施故障 → 应用层故障 → 跨服务编排 → AI 自动生成 FMEA

**我们 vs Fidelity 的差异化优势**：

| 能力 | Fidelity | 我们 |
|------|---------|------|
| 可观测性 | CloudWatch | ✅ DeepFlow eBPF（更深，零侵扰）|
| 根因分析 | 人工判断 | ✅ RCA Engine 自动验证（Graph RAG + Sonnet 4-6）|
| 知识图谱 | 无 | ✅ Neptune（171节点，天然 FMEA 输入）|
| 安全护栏 | ✅ 强 | ❌ MVP 必须补齐的核心差距 |
| 规模 | 21部门 3000应用 | 单团队，快速迭代 |

#### Capital One FMEA 实践
Capital One 的工程博客（[Dependency Management with FMEA](https://www.capitalone.com/tech/software-engineering/dependency-management-with-fmea/)）给出了 FMEA 在软件系统中的具体应用方法：

**标准 FMEA RPN 公式**：`RPN = Severity (S) × Occurrence (O) × Detection (D)`

| 因子 | 含义 | 评分范围 | 方向 |
|------|------|---------|------|
| **Severity (S)** | 失效后果的严重程度 | 1（轻微）~ 5（致命）| 越高越危险 |
| **Occurrence (O)** | 该失效发生的可能性 | 1（极低）~ 5（频繁）| 越高越危险 |
| **Detection (D)** | 失效到达用户前被发现的能力 | 1（必然检测到）~ 5（无法检测）| **越低越好** |

> Detection 与 S/O 方向相反：D=1 表示几乎一定能检测到（如 DeepFlow 实时告警），D=5 表示完全检测不到（无监控覆盖）。

**PetSite FMEA 三维示例**：

```
┌──────────────┬───┬───┬───┬─────┬──────────────────────────────────┐
│  服务/依赖    │ S │ O │ D │ RPN │  说明                             │
├──────────────┼───┼───┼───┼─────┼──────────────────────────────────┤
│ payforadopt  │ 5 │ 2 │ 1 │  10 │ D=1：DeepFlow+CW+RCA，必然检测   │
│ petsearch    │ 5 │ 3 │ 1 │  15 │ D=1：同上，但故障频率更高          │
│ pethistory   │ 3 │ 4 │ 2 │  24 │ D=2：Lambda CW，无 DeepFlow 覆盖 │
│ petfood      │ 1 │ 2 │ 1 │   2 │ Tier2，RPN 最低                  │
└──────────────┴───┴───┴───┴─────┴──────────────────────────────────┘
```

**缓解策略分类（Capital One 原文）**：
- Simple retry / Retry with exponential backoff
- Feature toggles（运行时降级开关）
- Circuit breaker（熔断器）
- Reduce dependencies（减少依赖）
- Self healing（自愈重启）

**FMEA 在混沌工程中的定位**：实验前的优先级输入，而非实验本身。

```
FMEA 分析 ──→ RPN 排序 ──→ 优先设计高分服务的混沌实验
```

我们的优势：**Neptune 图谱 + DeepFlow + 可观测性栈已是现成的 FMEA 三维输入**

| FMEA 因子 | 数据来源 | 自动化程度 |
|-----------|---------|-----------|
| **Severity** | Neptune Tier 分级（Tier0=5, Tier1=3, Tier2=1）| ✅ 自动 |
| **Occurrence** | DeepFlow 历史错误率 + DynamoDB 实验历史 | ✅ 自动 |
| **Detection** | 可观测性覆盖程度（DeepFlow/CloudWatch/RCA 三层）| ✅ 自动 |
| 当前缓解策略 | 人工填写 | 🔴 手动 |

---

### 1.3 背景（当前技术栈）

当前 PetSite 已具备：
- **故障采集**：DeepFlow eBPF 全栈可观测性（流量 + 指标 + 调用链）
- **知识图谱**：Neptune 服务依赖图（171 节点、309 条边）
- **根因分析**：RCA Engine（Graph RAG + Bedrock KB，Sonnet 4-6）
- **K8s 故障注入**：Chaosmesh-MCP（MCP Server，30 种故障类型，24/30 已验证）
- **AWS 托管服务故障注入**：AWS Fault Injection Service (FIS)（全托管，覆盖 Lambda/RDS/DynamoDB/EC2/EBS/网络等）

缺少的是：**将上述能力串联起来的自动化编排层**——即能够自动执行"注入 → 观测 → 分析 → 验证"完整闭环的平台。

### 1.4 核心价值主张

| 价值 | 描述 |
|------|------|
| 🔬 **弹性验证** | 主动暴露系统弱点，而非等待线上事故 |
| 🤖 **RCA 闭环验证** | 每次实验自动触发 RCA，验证根因定位准确性 |
| 📋 **合规报告** | 自动生成实验报告，记录系统弹性基线 |
| 🧠 **知识积累** | 实验结果写入 Bedrock KB，丰富历史知识库 |

---

## 2. 目标用户

| 用户角色 | 使用场景 |
|---------|---------|
| **SRE** | 执行 GameDay、验证容灾能力、设定弹性基线 |
| **开发团队** | 在 staging 验证新功能对下游的影响 |
| **架构师** | 验证架构设计的高可用假设 |

---

## 3. 系统上下文

### 3.1 PetSite 架构概览

```
Region: AWS ap-northeast-1 (东京)
Cluster: EKS PetSite (K8s 1.31, t4g.xlarge ARM64, 双 AZ: 1a/1c)

namespace: default
├── petsite          ×4  🔴 Tier0  (主入口，ALB 后端)
├── petsearch        ×4  🔴 Tier0  (搜索服务)
├── payforadoption   ×4  🔴 Tier0  (支付服务)
├── petlistadoptions ×4  🟡 Tier1  (列表服务)
├── pethistory       ×4  🟡 Tier1  (Lambda 代理)
├── petfood          ×4  🟢 Tier2  (宠物食品服务)
└── loadgenerator       (流量生成器)

AWS Managed Services:
├── Aurora MySQL         petlistadoptions 数据库
├── DynamoDB             petsearch 数据库
├── SQS                  异步消息队列
├── Lambda               petstatusupdater / petadoptionshistory
└── SNS                  通知服务
```

### 3.2 可观测性栈

```
DeepFlow (deepflow-server EC2 11.0.2.30)
├── ClickHouse 23.10     流量时序数据
├── deepflow-agent       EKS DaemonSet，eBPF 采集
└── Grafana 10.4         可视化 (grafana-x86 11.0.2.42)

Neptune (图数据库)
└── 3 ETL Lambda         etl_deepflow(5min) / etl_aws(2h) / etl_cfn(daily)

RCA Engine
├── Lambda: petsite-rca-engine    Graph RAG + Sonnet 4-6
└── Bedrock KB: 0RWLEK153U        历史事故知识库
```

### 3.3 故障注入工具

```
工具一：Chaosmesh-MCP (MCP Server) — K8s 应用层故障
├── /home/ubuntu/tech/chaos/Chaosmesh-MCP/
├── Git: https://github.com/RadiumGu/Chaosmesh-MCP
├── 已验证工具: 24/30 (pod/network/http/io/time/kernel chaos)
└── 覆盖范围: EKS Pod/容器级故障（细粒度，24种故障类型）

工具二：AWS Fault Injection Service (FIS) — AWS 托管服务 + 基础设施层故障
├── 全托管服务，ap-northeast-1 可用
├── 执行阶段：通过 boto3 / AWS CLI 调用（确定性路径）
├── 模板生成阶段：可通过 aws-api-mcp-server 辅助生成 + API 级验证（LLM Agent 模式）
├── 覆盖范围:
│   ├── Lambda: invocation-add-delay / invocation-error / invocation-http-integration-response
│   ├── RDS/Aurora: failover-db-cluster / reboot-db-instances
│   ├── DynamoDB: global-table-pause-replication
│   ├── EC2/EKS Node: stop/terminate/reboot instances / terminate-nodegroup-instances
│   ├── EBS: pause-volume-io / volume-io-latency
│   ├── 网络基础设施: disrupt-connectivity (VPC/subnet) / disrupt-vpc-endpoint
│   ├── AWS API: inject-api-internal-error / inject-api-throttle-error / inject-api-unavailable-error
│   └── S3: bucket-pause-replication
└── 原生能力: Stop Conditions (CloudWatch Alarm) / CloudTrail 审计 / IAM 访问控制

工具三：aws-api-mcp-server — FIS 模板生成辅助（LLM Agent 模式）
├── MCP Server，封装 AWS API 调用（含 FIS 全生命周期）
├── 用途：模板生成阶段 LLM Agent 通过 MCP 工具查询 AWS 资源、验证 FIS 参数
├── 不用于执行阶段（执行走 boto3 确定性路径）
└── 与 Chaosmesh-MCP 对称：LLM 统一通过 MCP 生成两种后端的模板
```

**双工具分工原则**：

| 故障层面 | 工具 | 理由 |
|---------|------|------|
| K8s Pod/容器级 | Chaos Mesh | 24 种已验证，http_chaos/time_chaos/kernel_chaos 等 FIS 没有 |
| AWS 托管服务（Lambda/RDS/DynamoDB/SQS） | FIS | Chaos Mesh 无法触及 AWS 托管服务 |
| EKS 节点级 | FIS | `terminate-nodegroup-instances`，Chaos Mesh 无等价操作 |
| AZ/VPC 网络基础设施 | FIS | `disrupt-connectivity` 是 subnet/VPC 级别 |
| AWS API 降级/限流 | FIS | `inject-api-throttle-error` 等，模拟 AWS 侧故障 |
| EKS Pod 故障（重叠区） | Chaos Mesh 优先 | 已验证且更细粒度；FIS EKS Pod 动作作为备选 |

---

## 4. 产品目标

### 4.1 核心目标（MVP）

基于 Fidelity Chaos Buffet 的经验，功能优先级按以下三级划分：

```
P0（必须有，MVP 不可缺）
├── 实验 YAML DSL               ← F1
├── 执行引擎（5 Phase）          ← F2
└── 安全护栏（Stop Conditions + 自动 delete_experiment）  ← F2 Guardrails
    ↑ Fidelity 强调最多，没有护栏就不能在生产跑

P1（有了更好，第二优先）
├── DeepFlow 稳态观测            ← F3
├── RCA 自动触发验证             ← F4
├── 实验报告生成                 ← F5
└── FIS 后端集成                 ← F2 FIS Backend（覆盖 AWS 托管服务层）

P2（进阶，稳定后再做）
├── 跨服务编排                   ← F6 6.4+
├── FMEA 优先级推荐（Neptune 驱动）← F0
└── CI/CD 集成                   ← 非目标（MVP 后）
```

| 目标 | 指标 | 优先级 |
|------|------|--------|
| **实验 YAML DSL** | 覆盖所有 24 种已验证故障类型 | P0 |
| **执行引擎（5 Phase）** | 一条命令完成"注入→观测→恢复"全流程 | P0 |
| **安全护栏** | Stop Conditions 超阈值自动 delete_experiment | P0 |
| **DeepFlow 稳态观测** | 实验前后 SLI 对比（成功率/延迟 p99）| P1 |
| **RCA 自动触发验证** | 实验结束自动调用 RCA Engine，验证根因准确性 | P1 |
| **实验报告生成** | Markdown 报告含完整时间线 + 指标 + RCA 结果 | P1 |
| **FMEA 优先级推荐** | Neptune + DeepFlow 驱动，自动排序实验优先级 | P2 |
| **跨服务编排** | 多服务顺序/并发故障注入 | P2 |
| **CI/CD 集成** | GitHub Actions 触发实验 | P2 |

### 4.2 非目标（本期不做）

- ❌ 生产环境自动触发（MVP 阶段手动触发）
- ❌ 跨 Region 实验
- ❌ UI 控制台（CLI + 报告即可）
- ❌ 告警集成（专注实验本身）

---

## 5. 功能需求

### F0: FMEA 风险优先级分析（实验前置输入）

**描述**：在执行任何混沌实验之前，先通过 Neptune + DeepFlow 自动生成 FMEA 表，确定哪些服务风险最高，驱动实验优先级排序——而不是拍脑袋决定先测哪个。

**用户故事**：
> 作为 SRE，我希望系统告诉我应该先测哪个服务，依据是数据而非直觉。

**FMEA 生成流程**：
```
Step 1: 从 Neptune 导出服务依赖清单（含直接 + 间接依赖）
Step 2: Severity   = Neptune Tier 映射（Tier0=5, Tier1=3, Tier2=1）
Step 3: Occurrence = DeepFlow 近 7 天错误率映射（1-5）
Step 4: Detection  = 可观测性覆盖层数映射（DeepFlow+CW+RCA=1, CW+RCA=2, CW=3, 无=5）
Step 5: RPN = Severity × Occurrence × Detection
Step 6: 按 RPN 降序排列，输出 FMEA 表 + 推荐实验列表
```

**Detection 评分规则（D 越低越好）**：

| D 值 | 含义 | PetSite 对应情况 |
|------|------|----------------|
| 1 | 必然检测到，实时告警 | EKS 微服务：DeepFlow eBPF + CloudWatch + RCA Engine 三层覆盖 |
| 2 | 大概率检测到，分钟级 | Lambda 函数：CloudWatch + RCA，无 DeepFlow L7 覆盖 |
| 3 | 一般检测能力，告警延迟 | 有 CloudWatch 但无 RCA 覆盖的托管服务 |
| 4 | 检测能力弱，依赖人工巡检 | 仅有基础 CloudWatch 指标 |
| 5 | 几乎无法检测 | 无监控覆盖（当前 PetSite 无此情况）|

**RPN 阈值行动**：

| RPN | 行动 |
|-----|------|
| ≥ 30 | 🔴 立即安排实验，最高优先级 |
| 15-29 | 🟡 本周安排实验 |
| < 15 | 🟢 本月内安排 |

> 注：Detection 维度的加入使 RPN 上限从 25（5×5）提升到 125（5×5×5），阈值相应调整。

**验收标准**：
- [ ] 一条命令生成 PetSite 全服务 FMEA 表
- [ ] Severity 从 Neptune 自动读取，无需手填
- [ ] Probability 从 DeepFlow 自动查询近 7 天错误率
- [ ] 输出 Markdown 格式，可直接追加到实验报告

---

### F1: 实验定义（Experiment DSL）

**描述**：用 YAML 描述一次混沌实验，包含注入参数、观测点、稳态假设、恢复步骤。

**用户故事**：
> 作为 SRE，我希望用一个 YAML 文件定义实验，而不是写脚本，以便重复执行和版本管理。

**实验 YAML 示例**：
```yaml
name: petsite-pod-kill-tier0
description: "Kill 50% of petsite pods, verify system degrades gracefully"
target:
  service: petsite
  namespace: default
  tier: Tier0

fault:
  type: pod_kill
  mode: fixed-percent
  value: "50"
  duration: "2m"

steady_state:
  before:
    - metric: success_rate
      threshold: ">= 99%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: ">= 80%"       # 允许降级
      window: "2m"
    - metric: success_rate
      threshold: ">= 99%"       # 恢复后必须恢复
      window: "5m"

rca:
  enabled: true
  trigger_after: "30s"           # 注入后 30s 触发 RCA
  expected_root_cause: petsite   # 期望 RCA 给出的根因

report:
  save_to_bedrock_kb: true
```

**验收标准**：
- [ ] 支持所有 Chaosmesh-MCP 已验证的 24 种故障类型（`backend: chaosmesh`）
- [ ] 支持 FIS 故障类型：Lambda / RDS / EKS Node / 网络 / API（`backend: fis`）
- [ ] YAML Schema 有验证，参数错误给出明确提示
- [ ] 支持多步骤实验（顺序注入多种故障）

---

### F2: 实验执行引擎（Experiment Runner）

**描述**：读取实验 YAML，按阶段执行：稳态检查 → 故障注入 → 观测 → 故障恢复 → 恢复验证。通过 `FaultInjector` 抽象层统一调度 Chaos Mesh 和 FIS 两个后端。

**执行流程**：
```
Phase 0: Pre-flight Check
  ├── 验证故障注入后端健康（Chaos Mesh 或 FIS，按 YAML backend 配置）
  ├── 验证目标服务/资源存在
  └── 检查无其他实验运行中

Phase 1: Steady State Before
  ├── 查询 DeepFlow 近 1min 指标
  └── 验证稳态假设（success_rate, latency p99）

Phase 2: Fault Injection
  ├── 按 backend 分派：Chaos Mesh（K8s 层）或 FIS（AWS 托管服务层）
  └── 记录注入时间戳 + 实验标识（Chaos Mesh experiment name / FIS experiment ID）

Phase 3: Observation + Guardrails（安全护栏）
  ├── 每 10s 采集一次 DeepFlow 指标
  ├── 记录影响曲线（success_rate / latency 变化）
  ├── 【Stop Condition 检查】超阈值立即触发 delete_experiment / stop_experiment 熔断
  ├── FIS 实验额外拥有 CloudWatch Alarm 原生 Stop Conditions（双重保险）
  └── （可选）等待 N 秒后触发 RCA

Phase 4: Fault Recovery
  ├── 实验到期自动恢复，或 Stop Condition 触发强制恢复
  └── 记录恢复时间戳

Phase 5: Steady State After
  ├── 验证稳态已恢复
  └── 生成实验报告
```

**安全护栏（Guardrails）设计**——参考 Fidelity 最强调的核心机制：

```yaml
# 实验 YAML 中的 stop_conditions 配置示例
stop_conditions:
  - metric: success_rate
    threshold: "< 50%"          # 成功率跌破 50% 立即熔断
    window: "30s"
    action: abort               # abort = 立即 delete_experiment + 报告

  - metric: latency_p99
    threshold: "> 5000ms"       # 延迟超过 5s 立即熔断
    window: "30s"
    action: abort

  - metric: error_rate_absolute
    threshold: "> 100 req/min"  # 绝对错误数异常飙升
    window: "30s"
    action: abort
```

**验收标准**：
- [ ] 任一阶段失败可 abort，自动清理故障（**Stop Conditions 必须实现，不可跳过**）
- [ ] 超时保护：实验最长不超过配置的 max_duration
- [ ] 实验结果实时写入日志文件，可中断后查看
- [ ] Stop Condition 触发时，日志明确标注熔断原因和触发时刻

---

### F3: 稳态观测（Metrics Collector）

**描述**：实验前后通过 DeepFlow Querier API 采集关键 SLI 指标，作为稳态对比依据。

**关键指标**：

| 指标 | 来源 | 查询方式 |
|------|------|---------|
| HTTP 成功率 | DeepFlow ClickHouse `application_map` | `response_status=0 / total` |
| 请求延迟 p99 | DeepFlow ClickHouse | `rrt_max` 字段 |
| Pod 就绪数 | K8s API | `v1.list_namespaced_pod` |
| 错误日志量 | DeepFlow `l7_flow_log` | `response_status >= 2` |

**DeepFlow Querier API**：
```
Host: http://11.0.2.30:20416
API: POST /v1/query/
```

**验收标准**：
- [ ] 查询超时 < 5s
- [ ] 支持自定义时间窗口（default: 60s）
- [ ] 指标对比结果可读（before / during / after 三阶段）

---

### F4: RCA 集成（RCA Trigger）

**描述**：在故障注入后自动触发 RCA Engine，验证根因定位的准确性。

**调用方式**：
```
Lambda: petsite-rca-engine
输入: { "anomaly_description": "petsite 服务成功率下降至 60%，开始时间: T+30s" }
输出: { "root_cause": "petsite", "confidence": 0.92, "evidence": [...] }
```

**验证逻辑**：
```python
expected_root_cause = experiment.rca.expected_root_cause
actual_root_cause   = rca_result["root_cause"]
match = (expected_root_cause in actual_root_cause)  # 允许模糊匹配
```

**RCA 三状态（v0.6 改进）**：

| 状态 | 含义 | 报告展示 |
|------|------|---------|
| `not_triggered` | 实验未达 Phase 3 或 `rca.enabled=false` | `⏭️ RCA 未触发（原因）` |
| `error` | Lambda 调用失败或返回空结果 | `⚠️ 触发但失败: {错误}` |
| `success` | 正常返回根因 | 根因 + 置信度 + 命中 ✅/❌ |

> 之前 `error` 和 `not_triggered` 都显示为"未触发或无结果"，导致无法区分 RCA 是没跑到还是跑了但失败了。

**验收标准**：
- [ ] RCA 调用超时设置为 60s
- [ ] 结果记录 expected vs actual，方便后续统计准确率
- [ ] RCA 失败不阻断实验继续执行
- [x] 报告区分三种 RCA 状态，不再统一显示"未触发"
- [x] DynamoDB 记录 `rca_status` 和 `rca_error` 字段

---

### F5: 实验报告（Report Generator）

**描述**：实验结束后生成 Markdown 格式的实验报告，包含完整时间线、指标数据、RCA 结果。

**报告结构**：
```
# Chaos Experiment Report: petsite-pod-kill-tier0
执行时间: 2026-03-04 14:30:00 CST
后端: chaosmesh
执行结果: PASSED / FAILED

[稳态验证]
| 阶段   | success_rate | latency_p99 |
|--------|-------------|-------------|
| Before | 99.8%        | 45ms        |
| During | 61.2%        | 312ms       |
| After  | 99.7%        | 47ms        |

[故障注入]
- 类型: pod_kill (fixed-percent 50%)
- 时间: 14:30:15 ~ 14:32:15 (2min)
- 影响 Pod 数: 2/4

[RCA 验证]
- 状态: ✅ success / ⚠️ error / ⏭️ not_triggered
- 期望根因: petsite
- 实际根因: petsite (置信度 92%)
- 结果: MATCH

[FIS 实验信息]（仅 backend=fis 时）
- FIS Template ID: ...
- FIS Experiment ID: ...
- CloudWatch Alarm Stop Conditions: ...

[🧠 AI 分析结论]
系统在 50% pod 被 kill 后表现出可接受的降级行为...
（由 Bedrock Claude Sonnet 自动生成）

[结论]
系统在 50% pod 被 kill 后，成功率降至 61%（超出预期降级阈值 80%）。
恢复用时 47s。RCA 准确定位根因。
```

**LLM 分析结论（v0.6 新增）**：
- 实验完成后调用 Bedrock Claude Sonnet 分析实验数据
- 输入：实验配置 + 稳态快照 + 影响指标 + RCA 结果（结构化 JSON）
- 输出：2-4 段中文分析（弹性达标判断 + 异常点 + 改进建议）
- 静默降级：Bedrock 不可用时跳过，不阻断报告生成
- 跳过无意义分析：实验没跑起来的 ERROR / 早期 ABORT

**验收标准**：
- [x] 报告保存到 `validation-results/` 目录，文件名含时间戳
- [x] 执行记录写入 DynamoDB `chaos-experiments` 表（详见 TDD）
- [x] 报告区分 Chaos Mesh 和 FIS 后端，FIS 实验包含模板和实验 ID
- [x] RCA 分析区分三种状态
- [x] 报告末尾附 LLM 生成的分析结论（Bedrock Claude）
- [ ] （可选）报告写入 Bedrock KB S3 bucket

---

### F6: 预置实验库（Experiment Catalog）

**描述**：为 PetSite 各服务预置标准实验场景，覆盖常见故障模式。

**实验分类**：

#### 6.1 Pod 级故障

| 实验名 | 目标服务 | 故障类型 | 预期 |
|--------|---------|---------|------|
| `tier0-pod-kill-50pct` | petsite | pod_kill 50% | 成功率 ≥ 80%，RCA → petsite |
| `tier0-pod-failure-50pct` | petsite | pod_failure 50% | 同上 |
| `tier0-petsearch-kill` | petsearch | pod_kill 50% | 搜索失败，其他服务不受影响 |
| `tier0-payforadoption-kill` | payforadoption | pod_kill 50% | 支付失败，浏览不受影响 |
| `tier1-petlistadoptions-kill` | petlistadoptions | pod_kill all | 列表不可用，其他功能正常 |

#### 6.2 网络级故障

| 实验名 | 目标服务 | 故障类型 | 预期 |
|--------|---------|---------|------|
| `network-delay-petsite-500ms` | petsite | network_delay 500ms | 延迟 p99 升高，成功率不变 |
| `network-loss-petsearch-30pct` | petsearch | network_loss 30% | 成功率下降 ~30% |
| `network-partition-payforadoption` | payforadoption | network_partition | 支付完全失败 |

#### 6.3 资源压力

| 实验名 | 目标服务 | 故障类型 | 预期 |
|--------|---------|---------|------|
| `cpu-stress-petsite-80pct` | petsite | pod_cpu_stress 80% | 延迟上升，OOM 不发生 |
| `memory-stress-petsearch-50pct` | petsearch | pod_memory_stress 50% | 延迟上升，不 OOMKilled |

#### 6.4 下游依赖故障（进阶）

| 实验名 | 目标 | 故障类型 | 预期 |
|--------|------|---------|------|
| `dns-chaos-external` | petsearch | dns_chaos error *.amazonaws.com | DynamoDB 连接失败 |
| `http-chaos-petsite-abort` | petsite | http_chaos abort | 特定路径返回 500 |

#### 6.5 AWS 托管服务故障（FIS）

> 以下实验通过 AWS FIS 执行，覆盖 Chaos Mesh 无法触及的 AWS 托管服务层。

**Lambda 故障**

| 实验名 | 目标 Lambda | FIS Action | 预期影响 |
|--------|------------|------------|---------|
| `fis-lambda-delay-petstatusupdater` | petstatusupdater | `aws:lambda:invocation-add-delay` (3s) | 宠物状态更新延迟，前端功能不阻断 |
| `fis-lambda-error-petadoptionshistory` | petadoptionshistory | `aws:lambda:invocation-error` (50%) | pethistory 查询部分失败，其他服务不受影响 |
| `fis-lambda-delay-petadoptionshistory` | petadoptionshistory | `aws:lambda:invocation-add-delay` (5s) | pethistory 响应变慢，前端超时降级 |

**数据库故障**

| 实验名 | 目标 | FIS Action | 预期影响 |
|--------|------|------------|---------|
| `fis-aurora-failover` | Aurora MySQL (petlistadoptions) | `aws:rds:failover-db-cluster` | 短暂连接中断（~30s），petlistadoptions 自动重连 |
| `fis-aurora-reboot` | Aurora MySQL Writer | `aws:rds:reboot-db-instances` | 写入中断，验证连接池重连和事务重试 |

**EKS 节点级故障**

| 实验名 | 目标 | FIS Action | 预期影响 |
|--------|------|------------|---------|
| `fis-eks-terminate-node-1a` | EKS nodegroup (AZ 1a) | `aws:eks:terminate-nodegroup-instances` (1 node) | Pod 重调度到 AZ 1c，服务短暂中断后恢复 |
| `fis-eks-terminate-node-1c` | EKS nodegroup (AZ 1c) | `aws:eks:terminate-nodegroup-instances` (1 node) | 同上，验证双 AZ 冗余 |

**网络基础设施故障**

| 实验名 | 目标 | FIS Action | 预期影响 |
|--------|------|------------|---------|
| `fis-network-disrupt-az-1a` | EKS subnet (AZ 1a) | `aws:network:disrupt-connectivity` | 模拟 AZ 故障，验证跨 AZ 流量切换 |
| `fis-ebs-io-latency` | EKS worker EBS volumes | `aws:ebs:volume-io-latency` (100ms) | 磁盘 IO 变慢，验证应用对 IO 延迟的容忍度 |

**AWS API 级故障**

| 实验名 | 目标 IAM Role | FIS Action | 预期影响 |
|--------|-------------|------------|---------|
| `fis-api-throttle-ec2` | EKS node role | `aws:fis:inject-api-throttle-error` (ec2) | EC2 API 限流，验证 EKS 控制平面和 ASG 行为 |
| `fis-api-unavailable-dynamodb` | petsearch task role | `aws:fis:inject-api-unavailable-error` | DynamoDB API 不可用，验证 petsearch 降级 |

---

### F8: Neptune 智能场景生成器（Scenario Generator）

**描述**：通过查询 Neptune 图谱，自动推算每个服务的 Tier 等级、上下游调用关系、稳态阈值和 Stop Conditions，生成可直接运行的 YAML 实验模板，替代人工填写阈值的繁琐过程。

**用户故事**：
> 作为 SRE，我只需要输入服务名和故障类型，系统自动从 Neptune 读取上下文，生成一份阈值正确、容忍度合理的 YAML 模板——而不是每次手填 95%/5000ms 这些数字。

**核心生成逻辑**：

```
输入: service + fault_type
        ↓
Neptune 图谱查询
  ├── m.recovery_priority → Tier0 / Tier1 / Tier2
  └── MATCH (a)-[:Calls]->(b) → callers（上游）/ callees（下游依赖）
        ↓
Tier 规则表 × 故障容忍度矩阵
  ├── steady_state.before/after 成功率阈值
  ├── steady_state.after latency_p99 阈值
  ├── stop_conditions.success_rate = before_sr × (1 - fault_tolerance)
  ├── stop_conditions.latency_p99
  └── rca.enabled（Tier0/Tier1=true, Tier2=false）
        ↓
生成 YAML，注入 Neptune 调用关系注释
保存到 experiments/tier{0,1,2}/{service}-{fault_type}.yaml
```

**Tier 阈值规则表**：

| Tier | 稳态 SR | 稳态 p99 | Stop SR 公式 | Stop p99 | RCA |
|------|---------|----------|-------------|---------|-----|
| Tier0 | ≥ 95% | < 5000ms | `95% × (1 - tol)` | > 8000ms | ✅ |
| Tier1 | ≥ 90% | < 8000ms | `90% × (1 - tol)` | > 15000ms | ✅ |
| Tier2 | ≥ 80% | < 15000ms | `80% × (1 - tol)` | > 30000ms | ❌ |

**故障容忍度矩阵**（`tol` 值）：

| 故障类别 | tol | 说明 |
|----------|-----|------|
| pod | 40% | Pod Kill 期间成功率允许大幅下降 |
| network | 30% | 网络故障 |
| app/http | 30% | 应用层故障 |
| stress | 20% | 资源压力影响相对较小 |
| kernel | 50% | 内核故障风险最高，容忍度最大 |

**示例**（`petsite`，Tier0，`pod_kill`）：
- `stop_sr = 95% × (1 - 0.40) = 57%`（成功率跌破 57% 才熔断，正常降级波动不误触发）
- `stop_p99 > 8000ms`

**支持故障类型（14 种）**：

| 类别 | 故障类型 |
|------|---------|
| Pod | `pod_kill` `pod_failure` `container_kill` |
| 网络 | `network_delay` `network_loss` `network_corrupt` `network_duplicate` `network_bandwidth` `network_partition` |
| 应用 | `http_chaos` `dns_chaos` `io_chaos` `time_chaos` |
| 资源 | `pod_cpu_stress` `pod_memory_stress` |
| 内核 | `kernel_chaos` ⚠️ |

**当前已生成模板索引**（2026-03-12）：

| 文件 | 服务 | 故障类型 | Tier |
|------|------|----------|------|
| `tier0/petsearch-network-delay.yaml` | petsearch | network_delay | Tier0 |
| `tier0/petsite-pod-kill.yaml` | petsite | pod_kill | Tier0 |
| `tier0/payforadoption-http-chaos.yaml` | payforadoption | http_chaos | Tier0 |
| `tier1/pethistory-network-delay.yaml` | pethistory | network_delay | Tier1 |
| `tier1/petlistadoptions-network-loss.yaml` | petlistadoptions | network_loss | Tier1 |
| `tier1/petstatusupdater-pod-cpu-stress.yaml` | petstatusupdater | pod_cpu_stress | Tier1 |

**验收标准**：
- [x] 从 Neptune 自动读取 Tier + 调用关系，不需要手动输入
- [x] Tier 规则表 × 故障容忍度矩阵自动推算所有阈值
- [x] 支持 14 种故障类型，每种专属参数交互式采集
- [x] YAML 头部注入 Neptune 上下文注释（上游/下游调用方）
- [x] 模板按 Tier 自动分类保存到 `experiments/tier{0,1,2}/`
- [x] 支持 `--service` / `--fault` / `--list-services` 命令行参数（跳过交互）

---

### F8.1: MCP 辅助模板生成（LLM Agent 模式扩展）

**描述**：在 F8 规则引擎基础上，引入 `aws-api-mcp-server` 和 `Chaosmesh-MCP` 作为 LLM Agent 的工具，实现 API 级验证的模板生成。特别针对 FIS 模板（当前需手动编写），通过 MCP 工具让 LLM 实时查询 AWS 资源、验证参数合法性。

**用户故事**：
> 作为 SRE，我希望告诉系统"给 petstatusupdater Lambda 生成一个延迟注入实验"，系统自动查询 Lambda 函数 ARN、验证 FIS Extension 是否安装、生成正确的 YAML——而不是手动查 ARN 再填入模板。

**核心架构：生成与执行分离**：
```
模板生成（LLM Agent + MCP 工具）
  ├── aws-api-mcp-server：查询 AWS 资源 + 验证 FIS 参数 + 创建 FIS 模板
  ├── Chaosmesh-MCP：查询 K8s 资源 + 验证故障类型
  └── 输出：经过 API 验证的 YAML 实验模板

模板执行（确定性 Runner，不依赖 LLM）
  ├── FISBackend：boto3 直接调用 FIS API
  ├── ChaosMeshBackend：通过 Chaosmesh-MCP 执行
  └── 紧急熔断：boto3 直调 + CloudWatch Alarm（最短路径）
```

**MCP 在生成阶段的价值**：

| 能力 | 纯 Prompt 生成 | LLM + MCP 工具 |
|------|---------------|----------------|
| Action 参数合法性 | 靠映射表，可能幻觉 | API 直接校验 |
| 目标资源存在性 | 靠快照（可能过期）| 实时查 AWS 资源 |
| ARN 格式正确性 | 模型拼接易错 | 从 API 响应直接获取 |
| IAM / Stop Conditions | 易遗漏 | 强制必填 |

**LLM 选型**：Sonnet 级别即可（Claude Sonnet / GPT-4o / Nova Pro）。领域知识靠 System Prompt 补（模板 schema + 映射表 + 约束规则），不需要 Opus 级别。

**注意事项**：
- `aws-api-mcp-server` 调用 `CreateExperimentTemplate` 会在 AWS 上创建真实资源（模板定义），需要清理机制
- 生成阶段创建模板 ≠ 启动实验，`StartExperiment` 必须在 Runner 的确定性路径中完成
- MCP 是增强，不是唯一路径——如果 LLM/MCP 不可用，仍可通过 gen_template.py 或手动编写

**验收标准**：
- [ ] LLM Agent 通过 aws-api-mcp-server 查询 Lambda/RDS/EKS 资源信息
- [ ] LLM Agent 生成的 FIS YAML 经过 Schema + 安全规则程序化校验
- [ ] 生成的模板可直接被 Runner 执行（格式兼容、资源 ARN 正确）
- [ ] 废弃的 FIS 实验模板有清理机制（避免 AWS 侧堆积）

---

## 6. 技术架构

### 6.1 组件关系

```
chaos-automation/
├── runner/
│   ├── experiment.py       # Experiment 数据模型（YAML 解析）
│   ├── runner.py           # 实验执行引擎（5 Phase 流程）
│   ├── metrics.py          # DeepFlow 指标查询
│   ├── rca.py              # RCA Engine 调用
│   ├── report.py           # 报告生成
│   ├── fault_injector.py   # 故障注入抽象层（统一接口）
│   ├── chaosmesh_backend.py  # Chaos Mesh 后端
│   └── fis_backend.py      # AWS FIS 后端
├── experiments/            # 预置实验 YAML 库
│   ├── tier0/
│   ├── tier1/
│   ├── network/
│   └── fis/                # FIS 专用实验（AWS 托管服务层）
│       ├── lambda/
│       ├── rds/
│       ├── eks-node/
│       └── network-infra/
├── docs/
│   ├── prd.md              # 本文档
│   └── tdd.md              # 技术设计文档
└── README.md
```

### 6.2 关键依赖

| 依赖 | 用途 | 接口 |
|------|------|------|
| Chaosmesh-MCP | K8s Pod/容器/网络故障注入 | Python 直接调用 `fault_inject.py` |
| AWS FIS | AWS 托管服务 + 基础设施故障注入 | boto3 `fis` client (`create_experiment_template` / `start_experiment`) |
| aws-api-mcp-server | FIS 模板生成阶段 LLM Agent 工具（资源查询 + 参数验证） | MCP Server（LLM 工具调用） |
| DeepFlow Querier | 指标采集 | HTTP POST `http://11.0.2.30:20416/v1/query/` |
| AWS Lambda | RCA 触发 | boto3 `invoke("petsite-rca-engine")` |
| Kubernetes Python Client | Pod 状态检查 | `v1.list_namespaced_pod()` |
| CloudWatch | FIS Stop Conditions + 指标监控 | boto3 `cloudwatch` client |
| Bedrock | 报告 LLM 分析结论（Claude Sonnet） | boto3 `bedrock-runtime` `invoke_model` |
| Bedrock KB S3 | 报告归档 | boto3 S3 `put_object` |

### 6.3 执行方式

MVP 阶段：**CLI 手动触发**

```bash
# 执行单个实验（Chaos Mesh）
python runner/runner.py --experiment experiments/tier0/petsite-pod-kill.yaml

# 执行单个实验（FIS）
python runner/runner.py --experiment experiments/fis/lambda/fis-lambda-delay-petstatusupdater.yaml

# 执行全部 Tier0 实验
python runner/runner.py --suite tier0 --dry-run

# 执行全部 FIS 实验
python runner/runner.py --suite fis --dry-run

# 查看最近报告
ls -lt validation-results/
```

---

### F7: 图数据库依赖属性反馈（Neptune Graph Feedback）

**描述**：实验结束后，将验证结果回写到 Neptune 图数据库，标注依赖的强弱类型及弹性指标，使图谱从"静态拓扑"进化为"经过验证的弹性拓扑"。

**用户故事**：
> 作为 SRE，我希望 Neptune 图谱里的依赖边不只是"有调用"，而是标注"强依赖还是弱依赖"，以便 RCA 推理和 FMEA 分析时使用更准确的数据。

**核心价值**：

```
混沌实验验证的不只是"系统能不能扛住"，
更重要的是回答 Neptune 图谱里每条边的本质：
  petsite → petsearch  是强依赖还是弱依赖？
  payforadoption → Aurora  挂了会不会级联失败？
```

**需要写回的边类型（基于现有 Neptune Schema）**：

| 边标签 | 当前属性 | 新增混沌验证属性 | 说明 |
|--------|---------|----------------|------|
| `Calls` | `call_type=sync` | `dependency_type`, `degradation_rate`, `recovery_time_seconds`, `last_verified`, `verified_by` | Microservice → Microservice，DeepFlow ETL 写入的动态调用关系 |
| `DependsOn` | — | `dependency_type`, `degradation_rate`, `last_verified` | BusinessCapability → 基础设施（DynamoDB/RDS/SQS 等）|
| `AccessesData` | — | `dependency_type`, `degradation_rate`, `last_verified` | Lambda → DynamoDB/SQS/S3，数据访问依赖 |
| `Invokes` | — | `dependency_type`, `degradation_rate`, `last_verified` | StepFunction → Lambda，同步调用链 |
| `TriggeredBy` | `call_type=async` | `dependency_type`, `degradation_rate`, `last_verified` | SQS → Lambda，异步触发链 |

**新增边属性定义**（统一使用 `chaos_` 前缀，与 ETL 字段严格区分）：

| 属性 | 前缀 | 写入方 | 说明 | 取值 |
|------|------|--------|------|------|
| `chaos_dependency_type` | chaos_ | graph_feedback.py | 实测依赖强弱类型 | `strong`/`weak`/`none`/`unverified` |
| `chaos_degradation_rate` | chaos_ | graph_feedback.py | 下游故障时上游成功率下降幅度（%）| 0.0~100.0 |
| `chaos_recovery_time_seconds` | chaos_ | graph_feedback.py | 下游恢复后上游恢复正常的时间（秒）| — |
| `chaos_last_verified` | chaos_ | graph_feedback.py | 最近一次混沌验证时间 ISO8601 | — |
| `chaos_verified_by` | chaos_ | graph_feedback.py | 验证实验 ID | `exp-petsite-pod-kill-...` |

> **命名约定**：ETL（neptune-etl-from-deepflow / aws / cfn）写入的字段无前缀（`strength`, `call_type` 等）；混沌实验写入的字段统一加 `chaos_` 前缀。新人看到 `chaos_*` 即知来源是实验实测。

**`chaos_dependency_type` 判定规则**：

```
chaos_degradation_rate >= 80%  →  strong   （强依赖，不可降级）
chaos_degradation_rate 20-79%  →  weak     （弱依赖，部分降级）
chaos_degradation_rate < 20%   →  none     （实测无影响，依赖关系存疑）
未执行实验                      →  unverified
```

**节点新增属性（Microservice）**：

| 属性 | 类型 | 说明 |
|------|------|------|
| `resilience_score` | int | 综合弹性评分 0-100（基于历次实验结果计算）|
| `last_chaos_test` | string | 最近一次被混沌测试的时间 |
| `chaos_test_count` | int | 累计被混沌测试次数 |

**图谱进化示意**：

```
实验前（Neptune 只知道有调用）：
  petsite --[Calls {call_type=sync, dependency_type=unverified}]--> petsearch

实验后（Neptune 知道这是强依赖）：
  petsite --[Calls {
    call_type=sync,
    chaos_dependency_type=strong,
    chaos_degradation_rate=94.0,
    chaos_recovery_time_seconds=47,
    chaos_last_verified=2026-03-04T14:32:15+08:00,
    chaos_verified_by=exp-petsite-pod-kill-20260304-143012
  }]--> petsearch
```

**与 RCA 的闭环**：
- RCA Engine 做图遍历时，可过滤 `dependency_type=weak` 的边，减少误报
- FMEA 生成器直接读 `degradation_rate` 作为 Probability 输入，无需估算
- 若实验发现 `degradation_rate < 20%`，说明该 `Calls` 边可能是误识别，触发人工复核告警

**验收标准**：
- [ ] 实验结束后自动调用 Neptune 写回接口（graph_feedback.py）
- [ ] `Calls` 边属性更新成功，可通过 Gremlin 查询验证
- [ ] `DependsOn` / `AccessesData` 边的反馈支持（取决于实验目标类型）
- [ ] Microservice 节点 `resilience_score` 随实验次数自动更新

---

## 7. 非功能需求

| 需求 | 要求 |
|------|------|
| **安全** | 仅在 staging/dev 集群使用；生产需额外确认 |
| **幂等** | 实验可重复执行，同名实验自动加时间戳后缀 |
| **可观察性** | 执行日志写到文件，实验进度实时打印 |
| **故障安全** | 任何异常（包括脚本 crash）自动清理 Chaos 实验资源 |
| **超时保护** | 实验最长执行时间默认 10 分钟，可配置 |

---

## 8. 成熟度模型与里程碑

### 8.1 混沌工程成熟度模型（参考 Fidelity Level 0-4）

| Level | 阶段 | 核心能力 | 我们的对应计划 |
|-------|------|---------|-------------|
| **Level 0** | 起步 | 手动执行基础设施故障（关服务器、网络分区）| Chaosmesh-MCP ✅ 已完成 |
| **Level 1** | 自动化 | 自动化执行 + 稳态验证 + 安全护栏 | **MVP 目标** |
| **Level 2** | 应用层 | 应用级故障（HTTP abort、超时重试、并发压测）| 第 3-4 周 |
| **Level 3** | 全链路 | 跨服务编排、多 AZ 故障、业务视角验证 | 进阶阶段 |
| **Level 4** | AI 驱动 | 自动生成 FMEA、自动推荐实验（LLM）| 未来演进（我们有 Sonnet 4-6，基础已具备）|

### 8.2 里程碑计划

| 阶段 | 内容 | Level | 目标日期 |
|------|------|-------|---------|
| **M1: 框架搭建** | FMEA 生成器 + 实验 DSL + Runner 骨架 + DeepFlow 指标查询 | L1 | 第 1 周 |
| **M2: 核心实验 + 护栏** | Tier0 全部实验 YAML + Stop Conditions 安全护栏 | L1 | 第 2 周 |
| **M3: RCA 闭环** | RCA 集成 + 报告生成 + Bedrock KB 写入 | L1→L2 | 第 3 周 |
| **M4: 实验库完善** | 网络/HTTP/IO 实验 + 文档完善 | L2 | 第 4 周 |
| **M5: FIS 集成** | FIS 后端接入 + Lambda/RDS/节点级实验 + CloudWatch Stop Conditions | L2→L3 | 第 5 周 |

---

## 9. 开放问题

| # | 问题 | 状态 |
|---|------|------|
| Q1 | DeepFlow 指标查询的具体 SQL 待确认（application_map 表结构） | 🔴 待确认 |
| Q2 | RCA Engine 的输入格式是否支持时间范围参数 | 🔴 待确认 |
| Q3 | Bedrock KB S3 bucket 写入权限（当前 Lambda role 是否足够）| 🟡 待验证 |
| Q4 | 是否需要支持并发实验（同时对多个服务注入）| 🟢 暂不支持 |
| Q5 | FIS Lambda 注入需要 FIS Lambda Layer + S3 bucket 配置，需确认 Lambda 函数是否可加 Layer | 🔴 待确认 |
| Q6 | FIS 实验 IAM Role 的权限范围，需与现有 EKS/Lambda IAM 隔离 | 🟡 待设计 |
| Q7 | FIS `aws:rds:failover-db-cluster` 对 Aurora MySQL 单实例集群是否适用 | 🟡 待验证 |

---

## 附录 A：相关文档

| 文档 | 路径 |
|------|------|
| 可观测性平台 PRD | `/home/ubuntu/tech/PRD.md` |
| 系统拓扑 | `/home/ubuntu/tech/PETSITE-SYSTEM-TOPOLOGY.md` |
| Neptune 节点设计 | `/home/ubuntu/tech/NEPTUNE-NODE-DESIGN.md` |
| RCA 相关文档 | `/home/ubuntu/tech/rca/` |
| Chaosmesh-MCP | `/home/ubuntu/tech/chaos/Chaosmesh-MCP/` |
| 验证结果 | `/home/ubuntu/tech/chaos/validation-results/` |
| AWS FIS 文档 | `https://docs.aws.amazon.com/fis/latest/userguide/` |

## 附录 B：Chaosmesh-MCP 已验证工具清单（2026-02-28）

✅ 成功（24）：health_check、list_namespaces、list_services_in_namespace、get_load_test_results、get_logs、load_generate、pod_kill、pod_failure、container_kill、network_partition、network_bandwidth、network_delay、network_loss、network_corrupt、network_duplicate、pod_cpu_stress、pod_memory_stress、host_cpu_stress、host_memory_stress、http_chaos、io_chaos、time_chaos、kernel_chaos、delete_experiment

✅ 已修复（4）：host_disk_fill、host_read_payload、host_write_payload、dns_chaos（2026-03-04，commit ab231f2）

⏭️ Skip（2）：inject_delay_fault、remove_delay_fault（需要 Istio，当前集群未安装）

## 附录 C：AWS FIS 可用 Action 清单（PetSite 相关）

> 完整列表见 [AWS FIS Actions Reference](https://docs.aws.amazon.com/fis/latest/userguide/fis-actions-reference.html)

### Lambda Actions（需要 FIS Lambda Extension Layer）
| Action | 说明 | PetSite 适用目标 |
|--------|------|-----------------|
| `aws:lambda:invocation-add-delay` | 注入调用延迟 | petstatusupdater, petadoptionshistory |
| `aws:lambda:invocation-error` | 注入调用错误 | petstatusupdater, petadoptionshistory |
| `aws:lambda:invocation-http-integration-response` | 修改 HTTP 集成响应 | petstatusupdater, petadoptionshistory |

### RDS/Aurora Actions
| Action | 说明 | PetSite 适用目标 |
|--------|------|-----------------|
| `aws:rds:failover-db-cluster` | 强制 Aurora 集群 failover | Aurora MySQL (petlistadoptions) |
| `aws:rds:reboot-db-instances` | 重启 RDS/Aurora 实例 | Aurora MySQL Writer/Reader |

### EKS Actions
| Action | 说明 | PetSite 适用目标 |
|--------|------|-----------------|
| `aws:eks:terminate-nodegroup-instances` | 终止 nodegroup 中的节点 | PetSite EKS t4g.xlarge 节点 |
| `aws:eks:pod-delete` | 删除 Pod（与 Chaos Mesh pod_kill 重叠） | 备选 |
| `aws:eks:pod-cpu-stress` | Pod CPU 压力（与 Chaos Mesh 重叠） | 备选 |
| `aws:eks:pod-memory-stress` | Pod 内存压力（与 Chaos Mesh 重叠） | 备选 |
| `aws:eks:pod-network-latency` | Pod 网络延迟（与 Chaos Mesh 重叠） | 备选 |
| `aws:eks:pod-network-packet-loss` | Pod 丢包（与 Chaos Mesh 重叠） | 备选 |
| `aws:eks:pod-network-blackhole-port` | Pod 端口黑洞（与 Chaos Mesh 重叠） | 备选 |
| `aws:eks:pod-io-stress` | Pod IO 压力（与 Chaos Mesh 重叠） | 备选 |
| `aws:eks:inject-kubernetes-custom-resource` | 注入自定义 K8s CR（可用于 apply Chaos Mesh CRD） | 高级编排 |

### EC2/EBS Actions
| Action | 说明 | PetSite 适用目标 |
|--------|------|-----------------|
| `aws:ec2:stop-instances` | 停止 EC2 实例 | EKS worker nodes, DeepFlow server |
| `aws:ec2:terminate-instances` | 终止 EC2 实例 | EKS worker nodes |
| `aws:ebs:pause-volume-io` | 暂停 EBS IO | EKS worker EBS volumes |
| `aws:ebs:volume-io-latency` | EBS IO 延迟注入 | EKS worker EBS volumes |

### 网络 Actions
| Action | 说明 | PetSite 适用目标 |
|--------|------|-----------------|
| `aws:network:disrupt-connectivity` | VPC/Subnet 级网络中断 | 模拟 AZ 故障 |
| `aws:network:disrupt-vpc-endpoint` | VPC Endpoint 中断 | 测试 VPC Endpoint 依赖 |

### API Fault Injection Actions
| Action | 说明 | PetSite 适用目标 |
|--------|------|-----------------|
| `aws:fis:inject-api-internal-error` | AWS API 500 错误 | EC2/Kinesis API |
| `aws:fis:inject-api-throttle-error` | AWS API 限流 | EC2/Kinesis API |
| `aws:fis:inject-api-unavailable-error` | AWS API 不可用 | EC2/Kinesis API |
