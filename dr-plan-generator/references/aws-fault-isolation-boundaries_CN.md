# AWS 故障隔离边界 — 参考摘要

> 来源：[AWS Fault Isolation Boundaries 白皮书](https://docs.aws.amazon.com/whitepapers/latest/aws-fault-isolation-boundaries/aws-fault-isolation-boundaries.html)
> 发布日期：2022 年 11 月 16 日（Amazon Web Services）
> 用途：指导 DR 计划生成器的故障隔离假设，避免错误分类

---

## 1. AWS 基础设施层次结构

```
分区 Partition（aws / aws-cn / aws-us-gov）
  └── 区域 Region（如 ap-northeast-1）
       └── 可用区 Availability Zone（如 apne1-az1）
            └── 数据中心
```

- **分区（Partition）**：IAM 硬隔离边界。跨分区操作不支持。
- **区域（Region）**：与其他 Region 隔离。故障范围限于单个 Region。
- **可用区（AZ）**：独立供电、网络、连接。设计为独立故障。

---

## 2. 服务范围分类

### ⚡ 这是 DR 计划生成的核心表格

| 范围 | 故障域 | 示例 | AZ 单点故障风险？ |
|------|--------|------|-------------------|
| **可用区级（Zonal）** | 单个 AZ | EC2、EBS、RDS（单 AZ）、EKS 节点 | **是** — 绑定到特定 AZ |
| **区域级（Regional）** | 单个 Region（跨 AZ） | DynamoDB、SQS、SNS、S3、Lambda、ALB/NLB、API Gateway | **否** — AWS 管理多 AZ 冗余 |
| **全局级（Global）** | 分区范围 | IAM、Route 53、CloudFront、Global Accelerator | **否** — 分布在多个 Region/PoP |

### 可用区级服务（AZ 绑定 → SPOF 候选）

资源部署到**特定 AZ**，随该 AZ 故障而中断：

- **Amazon EC2** 实例
- **Amazon EBS** 卷
- **RDS 单 AZ** 实例（不是 Multi-AZ）
- **EKS 工作节点**（基于 EC2，绑定到节点所在 AZ）
- **ElastiCache** 单节点
- **Neptune** 单实例
- **Directory Service**（单 AZ 部署）

**DR 影响**：这些是主要的 SPOF 候选。如果仅部署在一个 AZ，则为单点故障。

### 区域级服务（设计为多 AZ → AZ 故障时不是 SPOF）

AWS 在多个 AZ 之上构建这些服务。用户通过**单个区域端点**交互：

- **Amazon DynamoDB** — 数据自动分布在多个 AZ
- **Amazon SQS** — 区域级服务，设计为多 AZ
- **Amazon SNS** — 区域级服务
- **Amazon S3** — 数据跨多个 AZ 分布，自动从 AZ 故障恢复
- **AWS Lambda** — 在 Region 内跨多个 AZ 运行
- **Amazon API Gateway** — 区域端点
- **Elastic Load Balancing (ALB/NLB)** — 跨 AZ 分发（但目标实例是可用区级的！）
- **AWS Step Functions** — 区域级服务
- **Amazon Kinesis** — 区域级服务
- **Amazon EventBridge** — 区域级服务

**DR 影响**：这些服务不需要 AZ 级别切换。它们不是单 AZ 的 SPOF 风险。对于 Region 级 DR，需要跨 Region 复制或重新创建。

### 全局级服务（分区范围）

控制面在单个 Region，数据面全局分布：

- **AWS IAM** — 控制面在 us-east-1，数据面在每个 Region
- **Route 53 Public DNS** — 控制面在 us-east-1，数据面在数百个 PoP
- **Amazon CloudFront** — 控制面在 us-east-1，数据面在边缘节点
- **AWS Global Accelerator** — 控制面在 us-west-2，数据面在边缘
- **AWS Organizations** — 控制面在 us-east-1

**DR 影响**：控制面故障期间数据面操作继续工作。恢复路径中不要依赖控制面操作（创建/更新/删除）。

---

## 3. 控制面 vs 数据面

| 方面 | 控制面（Control Plane） | 数据面（Data Plane） |
|------|------------------------|---------------------|
| 功能 | CRUDL 操作（创建、读取、更新、删除、列表） | 服务的主要功能 |
| 复杂度 | 高（工作流、业务逻辑、数据库） | 低（刻意简化） |
| 故障概率 | 较高（更多活动部件） | 较低（更少组件） |
| 示例 | 启动 EC2 实例、创建 S3 桶、描述 SQS 队列 | 运行中的 EC2 实例、读取 S3 对象、Route 53 DNS 解析 |

**关键 DR 原则**：
> **恢复路径中优先使用数据面操作。控制面操作标注为风险 — 尤其是跨 Region 依赖的操作（如 Route 53 控制面在 us-east-1）。尽量灾前预置资源，但承认 100% 纯数据面恢复是理想目标，不一定总能做到。**

---

## 4. 静态稳定性 — DR 核心原则

**定义**：系统在故障期间无需动态变更即可继续工作。

**关键规则**：
1. 预置足够容量应对 AZ 丢失（如 3 个 AZ × 3 个实例 = 承受 1 个 AZ 丢失）
2. 灾前预置所有资源（ELB、S3 桶、DNS 记录）
3. 恢复时不依赖自动扩缩或资源创建
4. 切换时不依赖控制面操作

**成本权衡**：单 AZ 弹性的静态稳定需要约 50% 额外容量（跨 AZ 的 N+1）。

---

## 5. 常见反模式（DR 恢复中绝对不能做）

| 反模式 | 为什么会失败 | 正确做法 |
|--------|-------------|---------|
| 修改 Route 53 记录进行切换 | 依赖 us-east-1 的 Route 53 控制面 | 使用基于健康检查的切换（数据面），预置记录 |
| 切换时创建/更新 IAM 角色 | IAM 控制面在 us-east-1 | 预置所有 IAM 资源 |
| 灾难时创建新 ELB | 依赖 Route 53 控制面创建 DNS 记录 | 在 DR Region 预置 ELB |
| 创建新 S3 桶 | CreateBucket 依赖 us-east-1 | 预置所有桶 |
| 灾难时预置 RDS 实例 | 依赖 RDS 控制面 + Route 53 的 DNS | 预置只读副本 |
| 使用 STS 全局端点 | 默认指向 us-east-1 | 配置区域级 STS 端点 |
| 更新 CloudFront 源站进行切换 | 依赖 us-east-1 的 CF 控制面 | 使用源站组 + 故障转移 |
| 修改 Global Accelerator 权重 | 依赖 us-west-2 的 AGA 控制面 | 使用基于健康检查的路由 |

---

## 6. 跨 Region 复制注意事项

- AWS 不提供同步跨 Region 复制
- 异步复制 = 切换时可能丢数据（RPO > 0）
- 跨 Region 延迟为几百到几千英里 → 显著性能影响
- 多 Region 切换需要严格的堆栈隔离和协调切换
- 定期演练切换至关重要

---

## 7. 各服务 DR 指南

### RDS/Aurora
- **单 AZ**：可用区级，SPOF 风险
- **Multi-AZ**：Region 内自动故障转移（Aurora ~60 秒，RDS 数分钟）
- **跨 Region 只读副本**：需手动提升；依赖 RDS 控制面
- **Aurora Global Database**：托管跨 Region 复制，延迟约 1 秒

### DynamoDB
- **标准表**：区域级，多 AZ 自动。不是 AZ SPOF
- **全局表**：多 Region 复制，延迟 < 1 秒。双活（Active-Active）

### S3
- **标准**：区域级，多 AZ 自动。不是 AZ SPOF
- **跨 Region 复制（CRR）**：异步，需预配置
- **桶创建/删除**：依赖 us-east-1 — 必须预置！

### SQS / SNS
- **区域级服务**：多 AZ 自动。不是 AZ SPOF
- 无原生跨 Region 复制
- 多 Region 需重新创建或使用扇出

### Lambda
- **区域级服务**：跨多个 AZ 运行。不是 AZ SPOF
- Function URL 创建依赖 Route 53 控制面（us-east-1）
- DR 需预置所有 Lambda 资源

### ELB (ALB/NLB)
- **区域级服务**：跨 AZ 分发
- **重要**：ELB 本身是区域级的，但**目标实例是可用区级的**
- 创建新 ELB 依赖 Route 53 控制面 — 必须预置！
- 健康检查是数据面（可靠）

### EKS
- **控制面**：区域级，AWS 托管
- **工作节点**：可用区级（基于 EC2），绑定 AZ
- **节点故障**：使用多 AZ 节点组
- **托管 K8s 控制面**：区域端点，创建时依赖 Route 53

### Neptune
- **单实例**：可用区级，SPOF 风险
- **带副本的集群**：Region 内多 AZ
- **无原生跨 Region 复制**（截至本文撰写时）

---

## 8. 对 DR 计划生成器的影响

### SPOF 检测规则（已修正）

```python
# AZ 绑定的资源 → AZ 级 SPOF 候选
AZ_BOUND_TYPES = {
    "EC2Instance",
    "EBSVolume",
    "RDSInstance",      # 仅单 AZ；Multi-AZ 有备用
    "RDSCluster",       # 写入实例在一个 AZ
    "NeptuneInstance",
    "NeptuneCluster",   # 写入实例在一个 AZ
    "ElastiCacheNode",
    "EKSNodeGroup",     # 基于 EC2，绑定 AZ
}

# 非 AZ 绑定的资源 → 永远不标记为 AZ SPOF
REGIONAL_TYPES = {
    "DynamoDBTable",    # 区域级，多 AZ 自动
    "SQSQueue",         # 区域级，多 AZ 自动
    "SNSTopic",         # 区域级，多 AZ 自动
    "S3Bucket",         # 区域级，多 AZ 自动
    "LambdaFunction",   # 区域级，跨 AZ 运行
    "StepFunction",     # 区域级
    "LoadBalancer",     # 区域级（目标是可用区级，但 LB 本身不是）
    "APIGateway",       # 区域级
    "EventBridgeRule",  # 区域级
}
```

### Phase 0 预检项（基于白皮书）

1. **验证 DR 资源已预置**（非临时创建）
2. **检查复制延迟** — 跨 Region 数据存储
3. **验证区域级 STS 端点**（非全局）
4. **验证 Route 53 健康检查**基于数据面
5. **确认 IAM 角色/策略**存在于目标 Region
6. **检查 ELB** 已在目标预置

### 恢复路径规则

1. Phase 1-4 **只使用数据面操作**
2. Phase 0 **预置一切**（灾前）
3. **RDS 故障转移** = 数据面（提升副本）✅
4. **DynamoDB 全局表** = 数据面（已激活）✅
5. **Route 53 切换**通过健康检查 = 数据面 ✅
6. **Route 53 记录更新** = 控制面 ❌（恢复中避免）
7. **创建新 ELB** = 控制面 ❌（预置）
8. **创建新 S3 桶** = 控制面 ❌（预置）

---

## 9. DR 计划生成器决策矩阵

| 问题 | 是 | 否 |
|------|-----|-----|
| 资源是可用区级的（EC2、EBS、RDS 单 AZ）？ | 标记为潜在 AZ SPOF | 不是 AZ SPOF 风险 |
| 资源是区域级的（DynamoDB、SQS、S3、Lambda）？ | 跳过 AZ SPOF 检测 | 检查是否为可用区级 |
| 恢复步骤需要创建新资源？ | ⚠️ 标记为控制面依赖 | ✅ 恢复路径安全 |
| 恢复步骤修改 Route 53 记录？ | ⚠️ 依赖 us-east-1 控制面 | ✅ 无跨 Region 控制面依赖 |
| 资源已在目标预置？ | ✅ 静态稳定 | ⚠️ 风险：灾时依赖控制面 |

---

## 10. 附录 A — 分区级服务指南（静态稳定性）

每个分区级服务的控制面在单个 Region；数据面分布式。核心：**恢复路径中绝不使用控制面操作**。

| 服务 | 控制面位置 | 控制面故障时数据面行为 | 静态稳定性方案 |
|------|-----------|----------------------|---------------|
| **IAM** | us-east-1 | 认证授权继续工作。STS（独立数据面）正常。 | 预置 break-glass 用户并将凭证存入保险箱。恢复时不创建/修改角色。 |
| **AWS Organizations** | us-east-1 | SCP 继续评估。委托管理员正常。 | 使用 session tags 动态授权（数据面）。恢复时不修改 SCP。 |
| **账户管理** | us-east-1 | 现有账户正常工作。 | 预置所有 DR 账户。故障时不创建新账户。 |
| **Route 53 ARC** | us-west-2 | 恢复集群数据面正常。路由控制可查询。 | 书签/硬编码 5 个区域集群端点。使用 CLI/SDK 而非控制台。 |
| **Network Manager** | us-west-2 | Cloud WAN 数据面不受影响。 | 恢复时不通过 NM 更改网络。主动导出 CW 指标到 S3。 |
| **Route 53 Private DNS** | us-east-1 | 与公共 DNS 相同 — 解析继续。 | 与 Route 53 公共相同：使用基于健康检查的切换。 |

---

## 11. 附录 B — 边缘网络全局服务指南

| 服务 | 控制面位置 | 控制面故障时数据面行为 | 静态稳定性方案 |
|------|-----------|----------------------|---------------|
| **Route 53 Public DNS** | us-east-1 | DNS 解析 + 健康检查继续。通过健康检查状态变更的记录更新正常。 | 使用基于健康检查的切换（ARC 路由控制）。预置 DNS 记录。恢复中绝不调用 ChangeResourceRecordSets。 |
| **CloudFront** | us-east-1 | 缓存 + 分发继续。源站故障转移正常。失效请求可能失败。 | 使用源站故障转移组。恢复时不修改分发配置。 |
| **ACM（CloudFront 用）** | us-east-1 | 现有证书正常。自动续期正常。 | 恢复时不创建/更改证书。 |
| **WAF / WAF Classic** | us-east-1 | 现有 Web ACL + 规则继续生效。 | 恢复时不更新 WAF 规则。 |
| **Global Accelerator** | us-west-2 | Anycast 路由继续。健康检查正常。流量权重生效。 | 使用基于健康检查的切换。恢复时不修改流量拨号或端点。 |
| **Shield Advanced** | us-east-1 | DDoS 防护继续。健康检查响应正常。 | 预配置 DR 资源到防护组。恢复时不添加防护。 |

---

## 12. 附录 C — 单 Region 服务（DR 风险）

这些服务**仅在一个 Region** 存在 — 无多 Region 选项：
- AWS Marketplace（Catalog API、Commerce Analytics、Entitlement）
- Billing & Cost Management（Cost Explorer、CUR、Budgets、Savings Plans）
- AWS Chatbot、AWS DeepRacer、AWS Device Farm
- Alexa for Business、Amazon Chime、Amazon Mechanical Turk

**DR 影响**：如果工作流依赖这些服务，没有故障转移选项。需提前规划。

---

## 13. 静态稳定性 — 深入分析

**AWS 核心设计原则**：系统在依赖项故障期间无需变更即可继续运行。

关键特性：
1. **数据面独立性**：资源一旦预置，运行不依赖控制面
2. **无循环依赖**：服务设计为无互相阻塞即可恢复
3. **状态保持**：控制面故障期间数据面维持现有状态

**静态稳定性示例**：
- EC2 实例一旦启动：无论 EC2 控制面健康状况如何都保持运行
- VPC、S3 桶/对象、EBS 卷：全是数据面，无控制面依赖
- Route 53 健康检查：数据面，控制面故障期间继续评估
- IAM 认证授权：每个 Region 有独立数据面，IAM 控制面故障时正常工作

**对 DR 计划生成器的影响**：Phase 1-4 的每个步骤都应检查：
- 是否需要创建资源？→ **非静态稳定** → 标记为风险
- 是否仅使用现有资源？→ **静态稳定** → 安全
- 是否修改配置？→ **依赖控制面** → 灾前预配置

---

*修改 SPOF 检测逻辑、步骤构建器命令或预检项时，应参考本文档。*
