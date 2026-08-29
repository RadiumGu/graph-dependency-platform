# 用 AWS Resilience Hub(Next-gen / v2)拓扑发现完善依赖图谱 — 评估与改进方案

> ⚠️ **本文档的结论已于 2026-08-29 被实测推翻。第 5 条 ETL 判定为「已验证不做」。**
> 下面第 1–6 节是 2026-08-27 基于文档研究得出的**原始结论,已过时,保留作为决策留痕**。
> **请先读文末的[第 7 节:实测复核与最终判定](#7-实测复核与最终判定2026-08-29)。**

> 生成时间:2026-08-27 16:47 UTC
> 对象:`/home/ec2-user/works/graph-dependency-platform`
> 原结论速览(**已推翻**):~~可采纳,但定位为"合规与恢复目标"增量图层,不替代现有 DeepFlow L7 实时调用拓扑。建议新增第 5 条 ETL。~~

---

## 1. Resilience Hub 是否提供自动拓扑/依赖发现

**提供**。ARH 通过**输入源解析**间接发现资源拓扑(声明输入源 → 服务异步解析出资源并按 AppComponent 分组),而非爬取运行时调用:

- 支持输入源:**CloudFormation stack、AWS Resource Groups、Terraform state、AppRegistry/myApplications、EKS cluster(ARN + namespace)**。
  参见 [How AWS Resilience Hub works](https://docs.aws.amazon.com/resilience-hub/latest/userguide/how-it-works.html) 与 [create-app](https://docs.aws.amazon.com/cli/latest/reference/resiliencehub/create-app.html)。
- 解析动作:`ResolveAppVersionResources` 触发异步解析,`DescribeAppVersionResourcesResolutionStatus` 轮询状态。
  参见 [API_DescribeAppVersionResourcesResolutionStatus](https://docs.aws.amazon.com/resilience-hub/latest/APIReference/API_DescribeAppVersionResourcesResolutionStatus.html)。

### Next generation Resilience Hub(用户所说的 "v2")
2026-05-19 首发,新命名空间 `resiliencehubv2`,引入以 **Service** 为中心的模型和真正的**自动依赖发现(dependency discovery)**:

- `create-input-source`(CFN / Terraform / Resource tags / EKS) — [Add input sources to a service](https://docs.aws.amazon.com/resilience-hub/latest/userguide/next-gen-add-input-source.html)
- `GetService` / `ListServices` 返回 `DependencyDiscoveryConfig`(含 `eligibleResourceCount`、`message` 状态字段),2026-06-30 文档化
- EventBridge 发出 **new dependency discovery** 事件(2026-07-09),可驱动增量更新
- 以上变更见 [doc-history](https://docs.aws.amazon.com/resilience-hub/latest/userguide/doc-history.html)

---

## 2. 发现粒度与资源覆盖

- **粒度:资源级 / 依赖关系级,非 L7 调用级。** 发现"哪些资源属于同一应用、如何分组、跨 AZ/Region 布局",**不采集实时服务间调用链**。资源归入 AppComponent — [Grouping resources](https://docs.aws.amazon.com/resilience-hub/latest/userguide/AppComponent.grouping.html)。
- **EKS 支持**:2023-03 起支持([whats-new](https://aws.amazon.com/about-aws/whats-new/2023/03/aws-resilience-hub-amazon-eks/)),经 **K8s RBAC** 分析 Deployment/ReplicaSet/Pod 与集群整体韧性,支持多 namespace、跨 Region,但**仅 stateless 工作负载** — [Assess EKS resiliency](https://docs.aws.amazon.com/eks/latest/userguide/integration-resilience-hub.html)。

---

## 3. 对现有图谱的增量价值 vs 重叠

| 维度 | 结论 |
|------|------|
| **纯增量(现有 4 条 ETL 无法产出)** | 官方 **estimated RTO/RPO 基准**(按 disruption type 计算最坏值,应用/组件级)、**resilience policy 合规状态** + drift detection、**SOP / 推荐告警 / FIS 实验建议**、跨 AZ/Region 容灾评估 |
| **重叠且更弱(勿用其替代)** | 基础设施资源拓扑 —— 与 DeepFlow(L7)、`etl_aws`(Describe API)、`etl_cfn` 重叠,且 ARH 无实时调用链,不如现有方案 |

### 现有 4 条 ETL 的盲区,ARH 恰好补上
现有 ETL 采集的是"**是什么、连什么、调用多频**",但缺少"**恢复目标是多少、是否达标、怎么恢复**"这一层官方韧性语义。ARH 的 RTO/RPO 基准与 policy 合规正是 dr-plan-generator 目前只能靠资源类型默认值估算的部分 —— 接入后可把 DR 计划的 RTO 估算从"经验默认"升级为"官方评估基准"。

---

## 4. 局限(采纳前须知)

- **无实时 L7 调用拓扑** —— 这正是 DeepFlow eBPF 已做得更好之处,ARH 不触及。
- **EKS 仅覆盖 stateless**;有状态工作负载与细粒度服务间关系不建模。
- 解析**异步、非实时**;v2 的 EventBridge 事件缓解但仍偏粗粒度。
- **区域可用性需核实**:经典 ARH 在 `ap-northeast-1` 可用,但 Next-gen(`resiliencehubv2`)区域清单较新,**建议对东京区做一次 CLI 探测确认**:
  ```bash
  aws resiliencehubv2 list-services --region ap-northeast-1
  # 若命名空间/区域不可用,回退经典 resiliencehub 的 describe-app-assessment
  ```

---

## 5. 具体集成方案:新增第 5 条 ETL(韧性评估 ETL)

**定位:ARH 作"合规与恢复目标"图层,DeepFlow 继续作"实时调用"图层,二者以资源 ID 为锚点互补,不互相取代。**

### 5.1 数据流
```
PetSite EKS cluster ARN + CloudFormation stack
        │ (作为 ARH 输入源)
        ▼
AWS Resilience Hub (resiliencehubv2)
   GetService / ListServices → RTO/RPO、policy 合规、SOP
        │
        ▼  新增 Lambda: neptune-etl-from-resiliencehub (每日 / 或 EventBridge 触发)
Amazon Neptune
   新节点: ResiliencePolicy / RTOAssessment / SOP
   新边:   HAS_RTO / COMPLIES_WITH / RECOMMENDS_SOP
        │ 以 ARN/资源 ID 与 DeepFlow 的 Microservice 节点 join 对齐
```

### 5.2 实现要点
1. **输入源绑定**:以 PetSite 的 EKS cluster ARN + 关键 CloudFormation stack 作为 ARH 输入源,让其发现结果与图谱现有资源通过 **ARN / 资源 ID 对齐**(复用 `shared/service_registry.py` 的映射)。
2. **采集**:定期调用 `resiliencehubv2` 的 `GetService`/`ListServices`,以及经典 ARH 的评估结果 API(`describe-app-assessment` / `list-app-assessment-*`),拉取 RTO/RPO、policy 合规、SOP。
3. **写入**:沿用 `infra/lambda/shared/python/neptune_client_base.py` 的 SigV4 + 幂等 `MERGE`,新增节点/边类型(见上)。
4. **增量更新**:订阅 ARH 的 EventBridge **dependency-discovery / drift** 事件,复用现有 `etl_trigger` 的 SQS 通道做增量刷新,避免全量轮询。
5. **消费方**:
   - `dr-plan-generator` 的 RTO 估算改为优先读 `RTOAssessment` 节点,缺失时回退默认值。
   - `rca` 报告可附上受影响服务的 policy 合规状态与推荐 SOP。

### 5.3 落地成本(粗估)
- 一条新 ETL Lambda + 一个 EventBridge/Scheduler 规则,复用现有共享层与角色,CDK 侧改动集中在 `infra/lib/neptune-etl-stack.ts`。
- 图 schema 扩展 3 节点类型 + 3 边类型(同时应借机修正[偏差审计报告](./doc-code-discrepancies_20260827-1647.md)里 schema 计数不一致的问题)。
- 主要风险在 **v2 区域可用性** 与 **EKS stateless 限制**,建议先在东京区做 CLI 探测 + 小范围 PoC(单个 AppComponent)验证 join 对齐效果,再决定是否全量接入。

---

## 6. 一句话建议(2026-08-27 原文,已推翻)

~~**值得做,但按"增量图层"接入**:让 Resilience Hub 补齐现有平台最缺的"官方 RTO/RPO 基准 + policy 合规 + SOP"语义层,DeepFlow 的实时 L7 调用拓扑仍是不可替代的核心;先做东京区可用性探测与单组件 PoC,再规划第 5 条 ETL 的全量落地。~~

---

## 7. 实测复核与最终判定(2026-08-29)

**判定:第 5 条 ETL —— 已验证不做(wontfix)。**

2026-08-29 已把东京区 `vpc-010ab37a3f9f74725` 的全部应用真实纳管进 next-gen ARH
(1 system / 3 user journey / 3 policy / 4 service / 191 资源,3 个 service 评估成功,
产出 57 条 findings 与 68 条拓扑边)。完整过程与实测数据见
[arh-v2-onboarding-20260829-0750.md](./arh-v2-onboarding-20260829-0750.md),
产出文件在 `goal-loop-arh-v2/exports/`。**基于真实数据,本文档原先的三条采纳理由有两条被推翻,第三条经济上不成立。**

### 7.1 理由一「补依赖发现的短板」——⚠️ 已作废,须重测(2026-08-29T08:18Z 更正)

> **本节 08:07 之前写下的结论是在错误前提上得出的,现予作废。**
> 当时判定「ARH 依赖发现无用、与本项目 DNS 假阴性同源」,依据是三个 service 的
> `list-dependencies` 全窗口为 0。但事后查明:**依赖发现的资源发现步骤当时是静默损坏的** ——
> 官方 v1 ClusterRole 模板不含 `services`,而 next-gen 每 10 分钟对每个命名空间
> `list services` 一次并被 403 拒绝(EKS 审计日志确证,从 06:37 到 08:07 连续 1.5 小时)。
> 后果:`petsite-core` 的 `eligibleResourceCount` 一直是 `null`,**它根本没走到 DNS 分析那一步**。
> 补齐 `services` 权限后,`petsite-core` 的合格资源数 null→**6**、
> `graph-observability` 3→**8**,message 转 null(= ready to view)。
> **因此「依赖发现在本环境无用」这条尚无结论,须在 DNS 分析产出后重测(见 tasks.md T-097)。**

仍然有效的部分:ARH 依赖发现的机制确实是 **DNS query log 分析**(35 天回看),
前置条件要求计算资源「make DNS queries through Route 53 resolvers」,
且「discovers either EC2 instances (**preferred**) or VPC (fallback)」。
这一机制与本项目已证实产生假阴性的信号同源
(`neptune_etl_deepflow.py:328` 只用 DNS 作观测源 → 26 条边里 22 条误判
`declared_not_observed`),所以**理论上的盲区风险依然存在** ——
但这需要用修复后的真实数据验证,不能像本节原先那样直接断言。

`graph-observability` 修复前那 3 个合格资源(3 台 DeepFlow EC2,经 EC2 API 发现,
不受 K8s 权限影响)确实产出零依赖,该观察不受此次更正影响。

**关键教训:评估 SUCCESS ≠ K8s 权限充分。** 三个 service 的 failure mode assessment 全部成功,
因为评估只读 pods/deployments/replicasets(均被允许);而依赖发现额外需要 `services`,
被拒后没有任何显式失败信号 —— 只表现为 `eligibleResourceCount` 停在 null。
`list-service-events` 本应暴露它,但该 API **自身报错**
`Invalid service response: ServiceEventMetadata must have one and only one member set.`
(CLI 无法解析承载该错误的事件类型,AWS 侧 union 约束 bug),
所以唯一可行的诊断路径是 **EKS 控制面审计日志**。

### 7.2 理由二「用 ARH 拓扑边补图谱」——推翻:ARH 的边严格更粗

68 条边里 **46 条是基础设施管道**,`etl_aws` 的 Describe 早已覆盖:

| 数量 | 边 |
|---|---|
| 12 | `eks/cluster → ec2/subnet`(DATA_FLOW) |
| 12 | `ec2/subnet → ec2/vpc`(CONTAINMENT) |
| 6 | `autoScalingGroup → ec2/subnet` |
| 4 | nodegroup → subnet / cluster |
| 2 | `iam/role → eks/cluster` |
| 2+2 | loadbalancer → subnet、targetgroup → vpc |

真正的应用级依赖只有十余条,而且**锚点是 `eks/cluster` 而不是微服务**。与活图对照:

| ARH 给的 | Neptune 已有的 |
|---|---|
| `eks/cluster → rds/cluster`(1 条,语义仅「PetSite 集群访问 Aurora」) | `payforadoption → RDSCluster` 证据 `source:repository.go#CreateTransaction`;`pethistory` / `petlistadoptions` / `petsite` / `list-adoptions` 各自独立成边 |
| `eks/cluster → sqs`(1 条) | `petsite -[PublishesTo]→ SQSQueue` 证据 `source:PaymentController.cs#PostMessageToSqs`;`petstatusupdater -[DependsOn]→ SQSQueue` 与 DLQ |
| — | `petsearch → DynamoDBTable` 证据 `source:SearchController.java#search` |

**Neptune 是微服务粒度 + 文件行级 provenance,ARH 是集群粒度 + 无 provenance。**
灌入只会制造一批更粗、更无证据的重复边,直接恶化本项目最大的欠项
(设计目标「单一事实源」当前仅 ~60%,已有 `managedBy`/`managed_by`、
`resilience_score`/`chaos_resilience_score` 等多处命名与来源分裂的前例)。

### 7.3 理由三「findings 与 achievability 是新信息」——成立,但撑不起 ETL

findings 与 achievability(按 policy 分量给出 `ACHIEVABLE` / `NOT_ACHIEVABLE`)
**确实是 Describe API 无法推导的新信息**。但:

- **刷新经济性**:每 service 每月只含 **2 次**评估,第 3 次起 $0.10/资源
  (`petsite-core` 124 资源 ≈ 每次 $12)。一个月只能刷 2 次的数据源不是 ETL 的对象,
  是季度审计报告的对象。
- **无消费方**:`rca` 不读、`dr-plan-generator` 不读、Q1–Q18 无一条查询需要它。
  本仓库反复出现「写了但没人读」的模式(`graph_mcp_server.py` 410 行零调用、
  `AlertBuffer` 从未接入、`query_learning_nodes` 读四个不存在的属性),不应再增一例。
- **会漂移的第二副本**:findings 在每次评估时整批重生成,`status` 还能被人改成
  `RESOLVED` / `IRRELEVANT`。同步进图谱即制造又一个需要一致性校验的副本 ——
  与既定偏好「引入第二个存储要等到能同时设计一致性校验时再做」冲突
  (活证据:S3 Vectors 索引 56% 孤儿向量污染了 RCA 提示词)。

### 7.4 真正值得做的替代方向

57 条 findings 里有两条是**依赖类**问题,本该是本项目图谱的主场却完全没有捕获:

- `External Docker Hub dependency for Prometheus image blocks pod recovery`(SHARED_FATE)
  —— 外部镜像仓库依赖,Neptune 的 31 种节点类型里没有对应类型
- `Lambda functions share unreserved concurrency pool creating shared fate`(SHARED_FATE)
  —— 并发池共享命运,26 种边类型里没有对应边

结论:**图谱缺的不是「更多边」,而是整个维度** —— 外部依赖与资源池共享命运。
补这两类节点/边由自有 ETL 持续采集即可,不受 2 次/月配额限制,
比灌 ARH 的粗粒度边价值高得多。

### 7.5 ARH 的正确定位

**当作外部审计工具,产出留在 markdown 报告里定期人读,不进图谱。**
它的不可替代价值在**配置层**:PodDisruptionBudget 缺失、健康探针缺失、无 HPA、
Aurora 无 failover replica、备份保留仅 1 天、DynamoDB 未开 PITR、
单 NAT Gateway 跨 AZ 共享命运、ARC zonal shift 未启用、缺删除保护 ——
这些既不是拓扑也不是依赖,而是 DeepFlow 的 L7 与 Neptune 的拓扑都不覆盖的资源属性。

对应看板卡:`goal-loop-arh-v2/tasks.md` 的 **T-090 已置 `wontfix`**。

