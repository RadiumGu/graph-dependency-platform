# 用 AWS Resilience Hub(Next-gen / v2)拓扑发现完善依赖图谱 — 评估与改进方案

> 生成时间:2026-08-27 16:47 UTC
> 对象:`/home/ec2-user/works/graph-dependency-platform`
> 结论速览:**可采纳,但定位为"合规与恢复目标"增量图层,不替代现有 DeepFlow L7 实时调用拓扑。** 建议新增第 5 条 ETL。

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

## 6. 一句话建议

**值得做,但按"增量图层"接入**:让 Resilience Hub 补齐现有平台最缺的"官方 RTO/RPO 基准 + policy 合规 + SOP"语义层,DeepFlow 的实时 L7 调用拓扑仍是不可替代的核心;先做东京区可用性探测与单组件 PoC,再规划第 5 条 ETL 的全量落地。
