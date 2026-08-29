# Coverage Ledger — vpc-010ab37a3f9f74725 应用纳管台账

> 生成:2026-08-29 07:40 UTC · 账号 926093770964 · region ap-northeast-1
> 这是 DoD-2 的核验对象。**VPC 内每个应用组必须落在「已纳管」或「明确不纳管」两态之一,不允许待定。**
> 证据来源:`exports/resources-<service>.json`(由 `list-resources` 导出,共 191 个资源)

---

## 1. 已纳管 —— 4 个 ARH v2 service

| Service | Tier / Policy | 输入源 | 资源数 | 评估 | findings | 拓扑边 |
|---|---|---|---|---|---|---|
| `petsite-core` | tier0 / arh-tier0 | CFN `ServicesEks2` + EKS `petadoptions` | **124** | ✅ SUCCESS | 23 | 35 |
| `awesomeshop-legacy` | tier2 / arh-tier2 | CFN `AwesomeShopInfra` + EKS `awesomeshop` | **20** | ✅ SUCCESS | 17 | 11 |
| `graph-observability` | tier1 / arh-tier1 | TAGS `System=deepflow` + CFN `NeptuneEtlStack` + EKS `deepflow` | **39** | ✅ SUCCESS | 17 | 22 |
| `ops-rca-plane` | tier2 / arh-tier2 | CFN `AlertBufferStack` + TAGS `System=petsite-ops` | **8** | ❌ **不可评估**(见第 3 节) | 0 | 0 |

顶层建模:system `petsite-tokyo`(`lj9qdn`)+ 3 条 user journey
(`PetAdoptionFlow` tier0 / `AdoptionHistoryView` tier1 / `PetInventoryManagement` tier1,
取自 `infra/lambda/etl_aws/business_config.json` 的 `business_capabilities`)。

---

## 2. 逐应用组核对 —— 每一项都有归属

### 2.1 PetSite 核心业务 → `petsite-core`(124 资源)

| VPC 内的东西 | ARH 解析结果 | 状态 |
|---|---|---|
| EKS `petadoptions` 6 个 Deployment(petsite / list-adoptions / pay-for-adoption / pethistory / search-service / traffic-generator) | `AWS::EKS::Deployment` ×6 + `AWS::EKS::ReplicaSet` ×6 | ✅ 已纳管 |
| PetSite ALB + 2 TargetGroup | `LoadBalancer` ×1 + `TargetGroup` ×2 + `Listener` ×2 + `ListenerRule` ×4 | ✅ |
| petadoption DynamoDB 表 | `AWS::DynamoDB::Table` ×1 | ✅ |
| petadoption SQS + DLQ | `AWS::SQS::Queue` ×2 | ✅ |
| Step Functions 状态机 | `AWS::StepFunctions::StateMachine` ×2 | ✅ |
| **tier0 Aurora PostgreSQL** 集群 + 实例 | `RDS::DBCluster` ×1 + `RDS::DBInstance` ×3 | ✅ |
| EKS 集群 / 节点组 / addon | `EKS::Cluster` ×1 + `Nodegroup` ×4 + `Addon` ×3 + `AccessEntry` ×1 | ✅ |
| VPC / 子网 / 路由 / IGW / NAT | `VPC` ×2 + `Subnet` ×4 + `Route` ×4 + `RouteTable` ×4 + `InternetGateway` ×1 + `NatGateway` ×1 | ✅ |
| ServicesEks2 栈内的 20 个 Lambda | `AWS::Lambda::Function` ×20 | ✅ |
| 其他栈内资源(SSM 参数 29、API Gateway 9、KMS、S3、SNS、ApplicationInsights、ResourceGroups) | 同名类型 | ✅ |

> EKS worker EC2 实例**不以 `EC2::Instance` 出现**,而是被折叠成
> `AutoScaling::AutoScalingGroup` ×2 + `EKS::Nodegroup` ×4 —— ARH 只导入 top-level 资源,
> 子资源由父资源属性推导。这不是漏项。

### 2.2 AwesomeShop(计算层已下线)→ `awesomeshop-legacy`(20 资源)

| VPC 内的东西 | ARH 解析结果 | 状态 |
|---|---|---|
| EKS `awesomeshop` 6 个 Deployment(**副本数全为 0**) | `AWS::EKS::Deployment` ×6 | ✅ 已纳管(ARH 按声明导入,不因副本 0 跳过) |
| awesomeshop mysql 单实例 | `AWS::RDS::DBInstance` ×1 | ✅ |
| awesomeshop ElastiCache | `AWS::ElastiCache::CacheCluster` ×1 | ✅ |
| 集群基础设施(VPC / 4 子网 / 2 ASG / 2 Nodegroup / 2 SG 规则) | 同名类型 | ✅ |

> ⚠️ **真实欠项**:6 个 Deployment 全 0 副本(计算层已下线)而 mysql 实例与 ElastiCache
> **仍在运行计费**。本目标只如实建模不做处置,已记为 T-094 提请用户决策。

### 2.3 图谱可观测性平面 → `graph-observability`(39 资源)

| VPC 内的东西 | ARH 解析结果 | 状态 |
|---|---|---|
| deepflow-server / nfm-deepflow-test / grafana-x86 三台 EC2 | `AWS::EC2::Instance` ×3 | ✅ |
| **petsite-neptune** 集群 + 实例 | 计入 `RDS::DBCluster` ×2 / `DBInstance` ×2(与 grafana-aurora-mysql 合计) | ✅ |
| grafana-aurora-mysql 集群 + 实例 | 同上 | ✅ |
| 4 个 neptune-etl Lambda(from-aws / from-cfn / from-deepflow / trigger) | `AWS::Lambda::Function` ×4 | ✅ |
| NeptuneEtlStack 的 9 条 EventBridge 规则 | `AWS::Events::Rule` ×9 | ✅ |
| NeptuneEtlStack 的 2 个 SQS + QueuePolicy + EventSourceMapping | 同名类型 | ✅ |
| EKS `deepflow` 命名空间工作负载(prometheus-nfm / yace-nfm) | `AWS::EKS::Deployment` ×2 + `ReplicaSet` ×2 | ✅ |

> Neptune 实例原先无标签会被 `System=deepflow` 漏掉,已在 T-004 补标签。
> `neptune-etl-trigger` 只有 `Project=graph-dp` 无 `System` 标签,靠第三条
> `cfnStackArn`(NeptuneEtlStack)输入源补齐(T-006 的修正)。

### 2.4 告警聚合 / RCA 运维面 → `ops-rca-plane`(8 资源,不可评估)

| VPC 内的东西 | ARH 解析结果 | 状态 |
|---|---|---|
| petsite-rca-engine / petsite-rca-interaction / petsite-ops-slack-notifier / gp-window-flush | `AWS::Lambda::Function` ×4 | ✅ 已纳管 |
| gp-alert-buffer DynamoDB 表 | `AWS::DynamoDB::Table` ×1 | ✅ |
| petsite-rca-alerts / rca-alerts / petsite-ops-alerts SNS 主题 | `AWS::SNS::Topic` ×3 | ✅ |

资源已全部纳入,但**评估无法完成** —— 理由见第 3 节。

---

## 3. `ops-rca-plane` 不可评估 —— 证据链

`errorMessage`(5 次评估一致):
`The resources discovered for this service did not produce a topology.
Please verify the invoker role has AWSResilienceHubV2AssessmentExecutionPolicy policy.`

**消息后半句是误导性通用后缀**:该策略已挂载(cycle-3),且它让 S1/S2/S3 三个 service
都评估成功了。前半句「did not produce a topology」才是字面事实。

三个不同机制的尝试,全部否决:

| # | 尝试 | 结果 | 排除了什么 |
|---|---|---|---|
| 1 | 挂 `AWSResilienceHubV2AssessmentExecutionPolicy` | FAILED | IAM 权限 —— 同一策略让另外三个成功 |
| 2 | `create-service-function` + `create-service-function-resources` 显式声明拓扑锚点 | FAILED | 「缺拓扑提示」假设 |
| 3 | 补 3 个 SNS 主题作连接件(需删除重建 TAGS 输入源才生效,资源数 5→8) | FAILED | 「缺连接性资源」假设 |

**最可能的机制**(观察,未再验证):`ops-rca-plane` 是**唯一**资源集里没有任何
`AWS::EC2::VPC` / `AWS::EC2::Subnet` / `AWS::EC2::Instance` 的 service。
三个评估成功的 service 都含 VPC + Subnet,且它们的 `CONTAINMENT` 边(共 14 条)
全部来自网络层。纯 serverless 且不含网络资源的资源集似乎无法构成 ARH 认可的拓扑。

**另一项支撑证据**:该链路的实际连接关系有一半不在 AWS 控制面上 ——
4 个 Lambda 的 `list-event-source-mappings` **全为 0 条**,`scheduler list-schedules` 为空。
`gp-alert-buffer → gp-window-flush` 是**代码内直接调用**,ARH 看不见。
这本身就是一条值得记录的可观测性欠项:运维面的编排关系没有任何 AWS 侧的声明式表达。

**结论**:`ops-rca-plane` 记为「已纳管、不可评估」。资源已在 ARH 内可查
(`list-resources` 返回 8 条)、policy 已绑定、输入源已生效;只是 failure mode assessment
无法产出结果,因此该 service 不产生 findings 与拓扑边。

---

## 4. 明确不纳管 —— 逐项理由

| 对象 | 不纳管理由 |
|---|---|
| EKS `chaos-mesh` 命名空间(controller-manager / dashboard / dns-server) | 混沌工程**工具本身**,不是被保护的业务负载;仓库已有独立 chaos 模块管理 |
| EKS `kube-system`(aws-load-balancer-controller / cluster-autoscaler / coredns / ebs-csi / metrics-server) | EKS 平台附加组件,由托管服务负责,不构成独立应用 |
| EKS `amazon-cloudwatch` 命名空间(observability controller) | 可观测性附加组件,同上 |
| CDK EKS provider Lambda(`ServicesEks2-*` / `Applications-*` 自定义资源) | 部署期一次性执行的自定义资源 provider,非运行期负载 |
| `devops-agent-*`、`openclaw*` 相关资源 | 属其他项目,且主体在 `vpc-06731f30388b57818` 不在本 VPC |
| `sqlreplay-verify` Aurora 集群、`sqlreplay-client` EC2(`i-00c04a0473b650f6d`,**已停机**) | 标签 `Temporary=true`,临时验证资源 |
| `chaos-experiments` DynamoDB 表 | 混沌实验记录表,属工具链状态而非业务数据 |
| `Applications` CFN 栈(ECR 仓库 + K8s 清单自定义资源) | ECR 不在 ARH 支持的资源类型内;栈内 K8s 清单已通过 EKS 输入源覆盖 |
| `System=deepflow` 标签命中的 3 个 AMI image | ARH 支持的资源类型清单中没有 AMI,已被自动过滤(非解析失败) |
| 其他 VPC 的 ALB / TargetGroup(`openclaw-alb-v2` / `devops-agent-cn-bridge-alb` / `Transl-Alb16`) | 位于 `vpc-06731f30388b57818`,超出本目标范围 |
| `streamlit-demo-tg`(指向 10.1.2.198) | 目标在本机所在 CIDR `10.1.0.0/16`,跨 VPC,不属本 VPC 应用 |

---

## 5. 待定项

**无。** 上表每一项均已归入「已纳管」或「明确不纳管」。
