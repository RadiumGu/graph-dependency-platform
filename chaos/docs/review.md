# 📋 混沌工程自动化平台 — 架构审阅意见

**审阅日期**: 2026-03-07  
**审阅文档**: `docs/prd.md` (v0.2) + `docs/tdd.md` (v0.1)  
**审阅视角**: 大规模 AWS 金融系统 SRE  
**总体评级**: 🟡 修改后发布  

---

> **总体评价**：设计思路和整体架构方向是对的，FMEA 驱动 + Neptune 图谱反馈 + RCA 闭环验证的设计比市面上大多数混沌工程平台想得更远。但下面标注 🔴 的问题如果不解决，在金融级系统中跑起来会出数据污染和安全问题。建议先修 P0 的 4 个问题再进入编码阶段。

---

## 一、AWS PSA 视角

### ⚠️ 严重问题

#### 1. SQL/Gremlin 注入风险——金融系统不可接受 🔴

`metrics.py` 直接用 f-string 拼 SQL：
```python
pod_service_0 LIKE '%{service}%'
```
`graph_feedback.py` 直接拼 Gremlin：
```python
f"g.V().has('name', '{svc}')"
```
如果 service 名包含引号或特殊字符，SQL/Gremlin 直接被注入。即便当前是内部系统、service 名可控，这种模式在金融系统的代码审查中会被直接打回。**必须参数化查询**。DeepFlow Querier 如果不支持参数化，至少需要严格白名单校验输入。

#### 2. IAM Policy 与代码声明矛盾 🔴

TDD 6.1 的 IAM Policy 包含 `dynamodb:Scan`，但 `query.py` 明确声称"禁止 Scan"。这在审计时会被质疑——如果禁止 Scan，就不要授予 Scan 权限。最小权限原则。

#### 3. Stop Conditions 实现与规格不匹配 🔴

PRD 声称 Stop Conditions 是 P0 核心功能，YAML 里有 `window: "30s"` 配置，但 TDD 代码注释写着：

> "预留，当前按单次快照判断"

这不是 windowed stop condition，而是单点采样判断。对金融级系统：
- 10 秒采样间隔太粗，一次网络抖动就可能误触发熔断
- 真正的 windowed 判断应该是"连续 N 个采样点（或滑动窗口内平均值）满足条件才触发"
- 没有 windowed 实现就不要在 YAML 里暴露 `window` 字段，否则给用户虚假的安全感

#### 4. Graph Feedback 边更新范围过宽 🔴

`graph_feedback.py._update_edge()` 的 Gremlin 查询：
```groovy
g.E().hasLabel('Calls')
 .where(__.outV().has('name', '{svc}'))
 .or(__.inV().has('name', '{svc}'))
```
这会更新**所有**连接到该服务的 Calls 边。实际上一次 pod_kill 实验只验证了特定的上下游关系（比如 petsite → petsearch），不应该把 petsite 的所有 Calls 边都标记为相同的 `chaos_dependency_type`。这会产生误导性数据，后续 FMEA 和 RCA 推理都会被污染。

**改进**：实验 YAML 应明确声明验证的是哪条边（source → target），graph_feedback 只更新对应的边。

#### 5. DynamoDB GSI-2 热分区风险 🟡

`status-start_time-index` 的 HASH key 是 `status`。正常运行时绝大多数记录是 `PASSED`，这意味着一个分区键承载了几乎所有数据。DynamoDB 单分区 3000 RCU / 1000 WCU 的限制在数据量大时会成为瓶颈。

对于 MVP 阶段数据量不大，可以接受——但文档应明确记录这个已知限制和未来优化方向（比如用复合键 `status#YYYY-MM`）。

### 🟡 需要改进

#### 6. 没有实验审批流程

Fidelity 的 Chaos Buffet 核心之一是**治理流程**：谁能跑实验、需要谁审批、哪些服务哪些时段可以注入。当前设计完全没有审批层，CLI 一条命令就直接注入。即便是 staging 环境，金融系统也需要至少一个 `--confirm` 交互或审批记录。PRD 提到"生产需额外确认"但没有任何机制设计。

#### 7. Emergency Cleanup 的可靠性

`runner.py` 在 exception handler 里调用 `_emergency_cleanup()`，但如果是进程被 kill（SIGKILL）、机器断电、或 Python 进程 OOM，这个 cleanup 不会执行。需要：
- 运行时写一个 "lock file" 记录当前实验的 Chaos Mesh experiment name
- 启动前检查有无残留实验（Phase 0 已有"检查无其他实验运行中"，但需要能自动清理上次残留）
- 或者完全依赖 Chaos Mesh 的 `duration` 字段作为兜底（TDD 已提到，但应更显式地作为安全网文档化）

#### 8. DeepFlow 查询的可靠性

`metrics.py` 查询超时 5 秒，但没有重试逻辑。DeepFlow ClickHouse 在高负载时查询延迟可能波动。建议：
- 至少 1 次重试（带退避）
- 查询失败时的降级策略：是 abort 实验还是跳过本次采样？当前代码没处理

#### 9. RCA 验证的模糊匹配过于宽松

```python
return expected.lower() in result.root_cause.lower()
```
如果 expected 是 `pet`，那 `petsite`、`petsearch`、`petfood` 全部匹配。金融系统需要精确匹配 + 可配置的匹配策略（exact / contains / regex）。

#### 10. TTL 90 天可能不够

金融系统的审计要求通常是 1-3 年。90 天 TTL 对合规报告来说太短。建议：
- 实验记录 TTL 至少 365 天
- 重要实验（FAILED/ABORTED）不设 TTL，或归档到 S3

---

## 二、客户 PA 视角

### ⚠️ 严重问题

#### 1. 运维负担被低估——5 个外部依赖的同时协调 🔴

一次实验同时依赖：Chaos Mesh、DeepFlow API、Neptune Proxy、Lambda(RCA)、DynamoDB。任何一个不可用，实验就会失败或产生不完整数据。文档没有回答：
- DeepFlow server 挂了怎么办？（metrics.py 返回什么？Runner 怎么处理？）
- Neptune proxy 挂了怎么办？（graph_feedback 失败是否阻断实验？）
- 这些外部依赖本身的健康检查在 Phase 0 是否都覆盖了？

**建议**：Phase 0 Preflight 应显式列出所有外部依赖的健康检查项，并定义每个依赖不可用时的降级策略（skip vs abort）。

#### 2. Resilience Score 计算方式不科学 🔴

```python
score = max(0, 100 - result.degradation_rate())
```
这意味着每次实验都会**覆盖**上次的 resilience_score。如果第一次实验是 pod_kill（degradation 90%，score=10），第二次是 network_delay 100ms（degradation 5%，score=95），那 score 从 10 跳到 95——这完全不反映服务的真实弹性。

**改进方向**：
- Score 应该是所有历史实验的加权聚合，不是最近一次的覆盖
- 不同故障类型权重不同（pod_kill 比 network_delay 100ms 重要得多）
- 或者改为按故障类型分别记录 score，不做简单聚合

#### 3. 缺少成本估算 🟡

作为架构决策者，需要知道这套系统的运行成本。文档只说 DynamoDB "按需计费，成本极低"，但完全没有估算：
- DynamoDB 按需模式的写入成本（3 个 GSI 意味着每次写入实际是 4 次写，每个 GSI 投影 ALL）
- Neptune 已有的实例成本 + 额外的 graph_feedback 写入
- DeepFlow EC2 实例成本（已有，但应列入总拥有成本）
- Lambda RCA 调用成本
- 这套系统每月/每次实验的运行成本是多少？

### 🟡 需要改进

#### 4. 实验模板的安全边界缺失

当前任何人可以写 YAML 定义 `mode: all` + `fault_type: pod_kill` + `target: petsite(Tier0)`，直接打掉所有核心服务 Pod。缺少：
- 对 Tier0 服务的额外保护（比如禁止 `mode: all`，或强制要求 `value <= 50%`）
- 最大故障注入比例限制（blast radius cap）
- YAML Schema 验证应包含这些安全约束，不只是格式校验

#### 5. FMEA Occurrence 双源数据混合问题

`_calc_occurrence()` 在有历史实验时用 DynamoDB 失败率，没有时 fallback DeepFlow error_rate。这两个数据源的语义完全不同：
- DynamoDB 存的是"混沌实验失败率"——人为注入故障导致的失败
- DeepFlow error_rate 是"自然错误率"——正常运行时的错误

用混沌实验失败率作为 FMEA Occurrence 输入会导致悖论：**越多做混沌实验（且越多失败），Occurrence 越高，RPN 越高，越需要做实验**——这是正反馈死循环。

**改进**：Occurrence 应该始终基于自然错误率（DeepFlow），DynamoDB 历史数据用来辅助参考，不应作为主要输入。

#### 6. 文档结构问题

TDD 有两个 Section 5（"5. 实验 YAML Schema" 和 "5. 关键流程时序图"），章节编号混乱。YAML Schema 小节编号是 4.1/4.2，但在第 5 节下面。这影响可读性和可引用性。

#### 7. report.py 和 query.py 的 DynamoDB 客户端不一致

`query.py` 使用低级 DynamoDB client（`{"S": value}` 格式），`report.py` 的 `put_item` 看起来像是用 Resource（高级接口），两者混用会导致类型不匹配。应统一使用同一层级的客户端。

---

## 三、共同关注点汇总

| # | 问题 | 严重度 | 涉及模块 |
|---|------|--------|---------|
| 1 | SQL/Gremlin 注入风险 | 🔴 | metrics.py, graph_feedback.py |
| 2 | Stop Conditions 名不副实（单点判断 vs windowed） | 🔴 | runner.py |
| 3 | Graph feedback 边更新范围过宽，污染图谱数据 | 🔴 | graph_feedback.py |
| 4 | Resilience score 计算逻辑覆盖而非聚合 | 🔴 | graph_feedback.py |
| 5 | FMEA Occurrence 双源数据语义冲突 | 🟡 | fmea.py |
| 6 | 无实验审批 / blast radius 约束 | 🟡 | 整体设计 |
| 7 | Emergency cleanup 不可靠（进程异常退出） | 🟡 | runner.py |
| 8 | 外部依赖的降级策略缺失 | 🟡 | runner.py Phase 0 |
| 9 | IAM Scan 权限与代码声明矛盾 | 🟡 | TDD 6.1 |
| 10 | report.py vs query.py DynamoDB 客户端不一致 | 🟡 | TDD 3.4 / 3.7 |

---

## 四、行动建议（按优先级）

### P0 — 必须在 M1 前修复

1. **参数化所有外部查询** — metrics.py 的 SQL 和 graph_feedback.py 的 Gremlin，消灭注入风险
2. **实现真正的 windowed Stop Conditions** — 滑动窗口或连续 N 次采样；如果 MVP 做不到，把 YAML 里的 `window` 字段删掉，诚实标注"当前为单点采样"
3. **Graph feedback 精确到具体边** — YAML 增加 `source_service` + `target_service`，Gremlin 只更新特定的 source→target 边
4. **移除 IAM Policy 中的 Scan 权限**

### P1 — M2 前完成

5. **增加 blast radius 约束** — Tier0 服务禁止 `mode: all`，YAML Schema 校验时强制 `value <= 75%`（可配置）
6. **修复 resilience_score 计算** — 改为加权历史聚合，或按故障类型分别记录
7. **修复 FMEA Occurrence 数据源** — 主源用 DeepFlow 自然错误率，DynamoDB 历史只作辅助参考
8. **Phase 0 增加全部外部依赖健康检查 + 降级策略矩阵**
9. **统一 DynamoDB 客户端层级**（全用低级或全用高级）

### P2 — 进入 M3 前完成

10. 增加实验审批机制（至少一个 `--confirm` 交互 + 审计日志）
11. Emergency cleanup 增加 lock file 机制
12. TTL 延长到 365 天，FAILED/ABORTED 记录不设 TTL
13. 补充成本估算章节
14. 修复 TDD 章节编号

---

## 附录：值得肯定的设计点

- **FMEA 驱动实验优先级**：Neptune 图谱 + DeepFlow 三维输入自动计算 RPN，比拍脑袋决定先测哪个好得多
- **`chaos_` 前缀命名约定**：ETL 字段与实验字段严格隔离，新人易理解，不会误覆盖
- **ADR-001 FIS vs Chaos Mesh 决策**：务实选择，预留 FIS 扩展点但不过早引入复杂度
- **DynamoDB query.py 统一查询入口**：禁止 Scan、走 GSI 的设计原则正确
- **Graph feedback 闭环设计**：从"静态拓扑"到"经过验证的弹性拓扑"，这个思路在行业里很少见，是真正的差异化

---

## v0.4/0.5 增补审阅：FIS 双工具并行集成（2026-03-13）

**审阅文档**: `docs/prd.md` (v0.4) + `docs/tdd.md` (v0.5)  
**审阅焦点**: AWS FIS 集成方案

---

### 【AWS PSA 视角】

#### ✅ FIS 集成方向完全正确

ADR-002 取代 ADR-001 是合理的演进。PetSite 架构中 Lambda（petstatusupdater/petadoptionshistory）、Aurora MySQL、DynamoDB 是 Chaos Mesh 的盲区，不接 FIS 就无法验证这些层的弹性。分工原则清晰：Chaos Mesh 负责 K8s 层细粒度故障，FIS 负责 AWS 托管服务 + 基础设施层。

#### ⚠️ FIS 特有的注意事项

**1. FIS Lambda Extension 侵入性 🟡**

`aws:lambda:invocation-add-delay` / `invocation-error` 需要在目标 Lambda 上添加 FIS Extension Layer + 配置环境变量 + S3 bucket。这意味着：
- 需要修改现有 Lambda 函数配置（CFN/CDK 模板需要更新）
- FIS Extension 会增加 Lambda 冷启动时间（Extension 初始化 ~100-200ms）
- 不兼容 response streaming 的 Lambda
- Extension 需要持续 poll S3 获取故障配置，有 60 秒 ramp-up 延迟

**建议**：在 Q5 确认后，明确记录 Lambda Extension 的副作用和配置步骤。考虑用 CDK/CFN 条件部署——staging 环境加 Extension，生产环境不加。

**2. FIS Stop Conditions 只支持 CloudWatch Alarm 🟡**

FIS 原生 Stop Conditions 是 CloudWatch Alarm ARN，不支持自定义逻辑。当前设计的双重保险（Runner 自研 + FIS 原生）是好的，但需要注意：
- CloudWatch Alarm 评估周期最短 10 秒，加上 datapoint 延迟，实际熔断可能有 30-60 秒滞后
- Runner 自研 Stop Conditions（基于 DeepFlow 10s 采样）反应更快
- 两者可能同时触发，需要处理竞态——FIS 侧 stop 和 Runner 侧 abort 都会执行

**建议**：文档明确标注两种 Stop Condition 的响应速度差异，以及竞态处理策略（Runner abort 优先，FIS 原生作为兜底）。

**3. `aws:network:disrupt-connectivity` 爆炸半径 🔴**

这是整个 FIS 实验集里风险最高的 action。它通过修改 Network ACL 来阻断 subnet 流量，**影响范围是整个 subnet 内所有资源**——不仅是 EKS Pod，还包括同 subnet 的 Neptune、DeepFlow 等。如果 DeepFlow server（11.0.2.30）和 EKS 在同一个 VPC/subnet 内，这个实验会导致：
- 观测系统本身也被中断 → Runner 采集不到指标 → Stop Conditions 无法工作
- Neptune proxy 也可能不可用 → graph_feedback 失败

**建议**：
- 确认 EKS subnet 和 DeepFlow/Neptune 的 subnet 隔离情况
- 如果在同一 VPC，考虑用 FIS 的 `scope` 参数限制影响范围
- 或者 AZ 级实验只终止 EKS nodegroup 节点（`terminate-nodegroup-instances`）而非中断整个网络

**4. `aws:fis:inject-api-*-error` 当前只支持 EC2 和 Kinesis API 🟡**

PRD 6.5 里 `fis-api-unavailable-dynamodb` 试图用 API injection 模拟 DynamoDB 不可用，但 FIS 的 `inject-api-*-error` 当前只支持 `ec2` 和 `kinesis` namespace（见 AWS 文档 parameters.service 字段）。DynamoDB API 错误注入**目前 FIS 不支持**。

**替代方案**：
- 用 Chaos Mesh `dns_chaos` 阻断 `*.amazonaws.com` DNS 解析（已有模板 `dns-chaos-external`）
- 或用 Chaos Mesh `network_partition` 阻断到 DynamoDB endpoint 的网络
- 等待 FIS 扩展 API injection 到更多服务

---

### 【客户 PA 视角】

#### ⚠️ FIS 增加了运维和成本

**1. 额外的 AWS 资源管理 🟡**

FIS 集成引入了：
- `chaos-fis-experiment-role` IAM Role
- CloudWatch Alarms（每个实验场景一个）
- FIS Lambda Extension S3 bucket
- FIS 实验模板（存储在 AWS FIS 服务中）

这些资源需要 IaC 管理（CDK/CFN），否则会成为 "ClickOps" 隐患。建议 `infra/fis_setup.py` 改为 CDK construct 或 CFN template。

**2. FIS 定价 🟡**

FIS 按实验时长计费：
- 每个 action-minute 约 $0.10（标准实验）
- 一次 5 分钟实验约 $0.50
- 每月跑 50 次实验约 $25/月

成本不高，但应在文档中明确估算，避免意外。

**3. FIS 与 Chaos Mesh 的维护负担 🟡**

两套工具意味着两套维护：
- Chaos Mesh 需要随 EKS 集群升级同步升级
- FIS 是全托管服务，无需维护但 API 可能变更
- Runner 需要处理两种后端的错误和状态机差异

双后端 `FaultInjector` 抽象层是正确的设计，但需要充分的集成测试。

---

### 【共同关注点】

| # | 问题 | 严重度 | 模块 |
|---|------|--------|------|
| F1 | `aws:network:disrupt-connectivity` 可能中断观测系统本身 | 🔴 | fis_backend.py |
| F2 | `inject-api-*-error` 不支持 DynamoDB API，PRD 6.5 场景不可行 | 🟡 | PRD F6 6.5 |
| F3 | Lambda Extension 侵入性需要更详细的配置文档 | 🟡 | infra/fis_setup.py |
| F4 | 双 Stop Condition 竞态处理未明确 | 🟡 | runner.py |
| F5 | FIS 基础设施缺少 IaC 管理 | 🟡 | infra/ |

### 【行动建议】

#### P0 — M5 前必须确认
1. **确认 EKS 和 DeepFlow/Neptune 的 subnet 隔离**，决定 `disrupt-connectivity` 是否安全可用
2. **修正 `fis-api-unavailable-dynamodb` 实验**——FIS API injection 不支持 DynamoDB，改用 Chaos Mesh dns_chaos 或 network_partition 替代
3. **确认 Lambda 函数是否可加 FIS Extension Layer**（Q5）

#### P1 — M5 期间完成
4. **明确双 Stop Condition 竞态处理策略**
5. **将 `infra/fis_setup.py` 升级为 CDK/CFN**（或至少幂等可重复执行）
6. **补充 FIS 成本估算**到 PRD 或 TDD
7. **Lambda Extension 配置步骤文档化**（包含 CFN 条件部署方案）

---

## v0.5/0.6 增补审阅：MCP 辅助模板生成策略（2026-03-13）

**审阅焦点**: 实验模板生成和执行阶段 MCP 的角色定位

---

### 讨论背景

审阅发现当前双后端实现存在不对称：
- **ChaosMesh 后端**：通过 `Chaosmesh-MCP`（MCP Server）间接调用
- **FIS 后端**：通过 `boto3` 直接调用 FIS API

`aws-api-mcp-server` 拥有 FIS 全生命周期控制能力（创建模板、启动实验、查状态、停止），且 `Chaosmesh-MCP` 也有对应的 MCP Server。问题是：应该在哪个阶段引入 MCP？

### 结论：生成走 MCP，执行保留确定性路径（ADR-003）

经过讨论，确定**生成与执行分离**策略：

1. **模板生成阶段**：LLM Agent 通过 `aws-api-mcp-server`（FIS）和 `Chaosmesh-MCP`（K8s）交互式构建模板。MCP 的核心价值是 **API 级验证**——LLM 不是凭记忆猜参数，而是通过 API 交互确认 Action 参数合法、目标资源存在、ARN 格式正确。

2. **执行阶段**：Runner 保持确定性路径。FIS 侧 boto3 直调，ChaosMesh 侧通过 Chaosmesh-MCP 执行。不依赖 LLM 可用性。

3. **紧急熔断**：boto3 直调 `fis.stop_experiment()` + CloudWatch Alarm 原生 Stop Conditions，走最短路径，不经过 LLM/MCP 链路。

### 关键设计决策

- **一致性在抽象层（FaultInjector 接口），不在实现细节**。不应因 ChaosMesh 碰巧走了 MCP，就要求 FIS 执行也走 MCP 来"保持一致"。
- **LLM 选型**：Sonnet 级别即可。模板生成本质是结构化输入→结构化输出，领域知识靠 System Prompt 补。
- **程序化校验层**比换更贵的模型更重要——模板生成后必须过 Schema 合法性 + 安全规则检查。
- **MCP 是增强，不是唯一路径**——如果 LLM/MCP 不可用，仍可通过 gen_template.py（规则引擎）或手动编写。

### 对应文档变更

| 文档 | 变更内容 |
|------|---------|
| TDD 架构图 | 新增 Template Generator（LLM Agent）模块，标注 MCP 用于生成阶段 |
| TDD 3.3a | 明确标注两个后端实现差异（ChaosMesh 通过 MCP，FIS 通过 boto3），标注生成/执行分离 |
| TDD 3.10 | 新增 MCP 辅助模板生成完整设计 |
| TDD ADR-003 | 新增 MCP 在生成 vs 执行中的角色决策 |
| PRD 3.3 | 新增 aws-api-mcp-server 工具说明 |
| PRD F8.1 | 新增 MCP 辅助模板生成功能需求 |
| chaos-template-guide.md | 新增 MCP 辅助 FIS 模板生成章节 |

---

## v0.6/0.7 增补：RCA 三状态 + LLM 分析结论 + FIS 后端实现（2026-03-13）

**审阅焦点**: 代码实际实现与 TDD 设计的对齐

### 发现的问题

1. **RCA 全部为空**：审阅 10 份实际报告，RCA 分析全部显示"未触发或无结果"。原因：(a) 大部分实验未跑到 Phase 3（早期 ERROR/ABORT），(b) 跑到 Phase 3 的实验 Lambda 调用静默失败，错误被吞掉后统一显示"未触发"，无法区分。

2. **FIS 后端只存在于设计文档**：TDD 中的 `FISBackend` 是伪代码，`runner.py` 实际只支持 Chaos Mesh。`experiment.py` 没有 `backend` 字段。

3. **报告纯数据罗列**：没有分析结论，读者需要自己判断指标数据的含义。

### 已完成的代码修改

| 文件 | 改动 |
|------|------|
| `rca.py` | `RCAResult` 新增 `status`（not_triggered/error/success）+ `error_message`；`trigger()` 区分 Lambda FunctionError / 调用异常 / 返回空 |
| `report.py` | 报告 RCA 段区分三种状态；新增 FIS 实验信息段；新增 Bedrock LLM 分析结论（`_generate_llm_analysis`）；DynamoDB 写入新增 `backend` / `rca_status` / `rca_error` |
| `runner.py` | 按 `experiment.backend` 路由双后端（Phase 0/2/3/4 全流程）；紧急清理按 backend 分派 |
| `experiment.py` | `Experiment` 新增 `backend` 字段；`FaultSpec` 新增 `extra_params`；`StopCondition` 新增 `cloudwatch_alarm_arn`；`load_experiment()` 解析新字段 |
| `fis_backend.py` | 新建，`FISClient` 完整实现（inject/stop/status/wait_for_completion/delete_template/preflight_check），支持 15 种 FIS fault type |
| `result.py` | 新增 `fis_template_id` 字段 |

### 对应文档更新

| 文档 | 变更 |
|------|------|
| TDD 3.1 | `Experiment` 新增 `backend` 字段 |
| TDD 3.2 | Runner 注释更新为按 backend 分派 |
| TDD 3.3c | `FISBackend` → `FISClient`，反映实际实现 + backend 路由代码 |
| TDD 3.6 | `RCAResult` 新增 status/error_message + 三状态说明表 |
| TDD 3.7 | 重构为 3.7.1 报告结构 + 3.7.2 LLM 分析 + 3.7.3 DynamoDB（新增 backend/rca_status/rca_error）|
| TDD 模块表 | `fis_backend.py` 标注已实现；`report.py` 加 LLM 分析 |
| PRD F4 | 新增 RCA 三状态表 + 验收标准更新 |
| PRD F5 | 报告结构加 FIS 信息段 + LLM 分析段 + 验收标准更新 |
| PRD 6.2 | 依赖表新增 Bedrock（报告 LLM 分析） |
| chaos-template-guide.md | 注意事项加 LLM 分析说明 |
