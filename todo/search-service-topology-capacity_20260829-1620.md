# search-service 的拓扑分散：任务前提不成立，真正的约束是集群容量

**日期**：2026-08-29 15:48–16:20 UTC
**起因**：上一轮我自己提的建议 ——「给 search-service 加 topologySpreadConstraints，
让拓扑提示真正生效而不是被反复摘掉」。
**结论**：**这个建议的前提是错的**，约束早就在了；真正卡住的是集群 CPU 容量，
而且拓扑提示在 HPA 存在的前提下**在设计上就不可能稳定生效**。

---

## 一、前提被推翻：约束早就在了

```json
// searchserviceDeployment513D27D7（从 ServicesEks2 栈的 CFN 模板取出）
"topologySpreadConstraints":[
  {"maxSkew":1,"topologyKey":"topology.kubernetes.io/zone","whenUnsatisfiable":"ScheduleAnyway"},
  {"maxSkew":1,"topologyKey":"kubernetes.io/hostname","whenUnsatisfiable":"ScheduleAnyway"}]
```

**五个 deployment 的约束完全相同**，四个正确分散在 1a/1c，只有 search-service
两个 Pod 都在 1c。而 1a 节点无 taint、可调度、Pod 数 17/35 与 23/35。

所以「加约束」无从下手 —— 该问的是「为什么同样的约束只有它失效」。

## 二、失效原因：不是打分输了，是偏好**根本无法满足**

第一反应是「`ScheduleAnyway` 是软偏好，被资源打分盖过了」。
把它改成 `DoNotSchedule` 之后，调度器**直接把原因说了出来**：

```
FailedScheduling: 0/4 nodes are available: 4 Insufficient cpu.
（加上约束后）0/4 nodes are available: 1 node(s) didn't match pod topology
             spread constraints, 3 Insufficient cpu.
```

Pod 需要 **544m**（512m 应用 + 32m otel sidecar），而节点可分配 1930m：

| 节点 | AZ | 已请求 | 剩余 | 够 544m 吗 |
|---|---|---|---|---|
| `ip-11-0-2-129` | 1a | 1393m (72%) | **537m** | ❌ 差 **7m** |
| `ip-11-0-2-51` | 1a | 1481m (76%) | 449m | ❌ 差 95m |
| `ip-11-0-3-141` | 1c | 1557m (80%) | 373m | ❌ |
| `ip-11-0-3-205` | 1c | 1755m (90%) | 175m | ❌ |

**全集群没有一个节点放得下一个 search-service Pod。**

这同时解释了 06:23 的原始现象：当时 1a 大概同样装不下，
`ScheduleAnyway` 不是「输给了资源打分」，而是**在做它该做的事** ——
偏好无法满足时允许落地，而不是让 Pod 无限 Pending。

> `DoNotSchedule` 在这个容量下会让 search-service **永久停在单副本**。
> 实测确认后立刻回滚。这条不能靠推理省掉：不试就不知道是差 7m 还是差 500m。

## 三、容量去哪了：观测栈比被观测的应用还重

```
petadoptions                 1892m  30%   ← 被观测的应用
amazon-cloudwatch            1300m  21%  ┐
deepflow                      550m   8%  ├ 观测栈合计 2054m（33%）
amazon-network-flow-monitor    204m   3%  ┘
amazon-guardduty              800m  12%
kube-system                  1240m  20%
chaos-mesh                    200m   3%
────────────────────────────────────────
合计                          6186m / 7720m = 80% 承诺
```

而**节点实际 CPU 只有 6–35%**。所以这不是真的没算力，
是 **request 与实际用量完全脱节**：

| Pod | request | 实际 | 虚高 |
|---|---|---|---|
| search-service | 544m | 176–199m | 3x |
| list-adoptions | 160m | 1–23m | 7–80x |
| pay-for-adoption | 160m | 2–5m | 32–80x |
| pethistory | 82m | 1m | 82x |
| **petsite** | **0m** | 39m | **完全没有 request** |

节点是 **t4g.large**（2 vCPU / 8 GiB，ON_DEMAND），1a 节点组 min2 / max3 / desired2。

## 四、即使解决了容量，拓扑提示仍然不会稳定生效

把 request 降到 256m 后 1a 装得下了，`DoNotSchedule` 也满足了，
Pod 确实进了 1a。恢复 `service.kubernetes.io/topology-mode: auto` 之后，
EndpointSlice 控制器**又直接把原因说了出来**：

```
TopologyAwareHintsDisabled: Unable to allocate minimum required endpoints to each
zone without exceeding overload threshold (3 endpoints, 2 zones), addressType: IPv4
```

两个等权 AZ（各 2 节点 × 1930m，比例 0.50 / 0.50），3 个端点 →
每个 AZ 该拿 1.5 个；给一个 AZ 分 2 个就超过控制器的 20% 过载阈值 → **整体禁用提示**。

对照仍开着注解的 `list-adoptions`（**2 个端点**、1a/1c 各一）：

```
11.0.2.45   zone=1a  forZones=['ap-northeast-1a']   ✅
11.0.3.177  zone=1c  forZones=['ap-northeast-1c']   ✅
```

> **所以拓扑提示要求端点数能按 AZ 的 CPU 比例整除。两个等权 AZ 就意味着端点数必须是偶数。**
>
> 而 `search-service-hpa` 是 min2 / max6 —— 它会经过 3 和 5。
> **只要 HPA 落在奇数副本，提示就自动关闭。**
> 这不是配置问题，是这套机制与 HPA 的结构性冲突。

这也反过来解释了原始缺陷为什么那么难看：**2 个端点都在 1c** 时，
控制器为了让 1a 也拿到约一半流量，只能把其中一个 1c 端点标成
`forZones=[1a]` —— 提示零收益（1a 客户端无论如何都要跨 AZ），
纯粹造成确定性钉死（76/24）。

## 五、我这一轮改出来的一个真实副作用（记录在案）

降 request 会**同时改变 HPA 的扩容阈值**，因为 HPA 的 `averageUtilization`
是相对 request 的百分比：

```
旧：60% × 512m  → 实际用量到 307m 才扩容
降 request 后未调目标：60% × 256m → 实际用量到 154m 就扩容（阈值被腰斩）
```

实测后果：HPA 从 2 副本一路扩到 **5 副本**，而 `maxSurge:0 / maxUnavailable:1`
让 Deployment 卡在混合 ReplicaSet 状态（2 个 256m Pod + 1 个 512m Pod），
换不下去、并出现 Pending。

**修法是等比提高 HPA 目标**，让绝对阈值不变：

```
120% × 256m = 307m   ← 与改动前完全一致
```

## 六、最终落地状态

| 项 | 改动前 | 现在 | 理由 |
|---|---|---|---|
| `search-service` CPU request | 512m | **256m** | 实测用量 176–199m；512m 在当前集群无处可放 |
| CPU limit | 512m | 512m（不动） | 突发能力不变 |
| QoS | Burstable | Burstable（不变） | sidecar 的 cpu request≠limit，本来就不是 Guaranteed |
| HPA 目标 | 60% | **120%** | 等比补偿，绝对扩容阈值仍是 307m |
| zone 约束 | ScheduleAnyway | ScheduleAnyway（不动） | 硬约束在当前容量下会死锁 |
| — 新增 | — | `nodeTaintsPolicy: Honor`<br>`nodeAffinityPolicy: Honor` | 语义正确、无副作用；将来改硬约束时，AZ 故障会自动放松而非死锁 |
| `topology-mode` 注解 | 已被摘除 | 保持摘除 | 见第四节：与 HPA 结构性冲突 |
| search-service 在 1a 的 Pod | **0 个** | **1 个** | 降 request 后 1a 装得下了 |

**净收益**：1a 从没有 search 端点变成有；总预留从 1088m 降到 768m；
未来调度不再被容量卡死。**代价**：无（HPA 行为经等比补偿后不变）。

## 七、要让拓扑提示真正稳定生效，需要什么

三件事必须同时成立，缺一不可：

1. **容量**：1a 有节点放得下 search-service Pod。
   已通过降 request 达成；也可以给 1a 节点组扩到 3 个节点
   （`maxSize` 已经是 3，只需改 `desiredSize`）—— 一台 t4g.large ON_DEMAND
   在东京约 **$0.0864/hr ≈ $63/月**。
2. **确定性**：zone 约束改成 `DoNotSchedule`。
   **必须配 `nodeTaintsPolicy: Honor`**（已加）—— 否则 AZ 故障时
   1a 域仍然存在但 Pod 数为 0，`maxSkew:1` 会拒绝把替换 Pod 放进 1c，
   服务卡在单副本。K8s 1.35 已 GA，服务端干跑通过。
3. **端点数为偶数**：即 HPA 必须被钉住在偶数副本。
   现实做法是 `minReplicas=maxReplicas=2`（等于放弃自动伸缩），
   或接受提示在奇数副本时自动关闭。

> **判断**：第 3 条与自动伸缩天然矛盾。对这套演示/混沌平台而言，
> 跨 AZ 流量费远小于「行为可预测」的价值，
> 所以**保持注解摘除**是合理的，与另一会话的处置一致。
> 若确实要省跨 AZ 流量，正确做法是钉死 2 副本 + 硬约束 + 加节点，三者一起上。

## 八、CDK 源码修改规格

⚠️ **PetSite 的 CDK 源码不在这台机器上** —— `~/works` 下只有
`graph-dependency-platform`。以下是从 `ServicesEks2` 栈的 CFN 模板反查出的
资源坐标与需要的改动，交给持有 CDK 仓库的人执行。

| CFN 逻辑 ID | 承载的对象 | 需要的改动 |
|---|---|---|
| `searchserviceDeployment513D27D7` | `Deployment/search-service` | ① CPU request 512m → 256m（limit 保持 512m）<br>② zone 约束加 `nodeTaintsPolicy: Honor` + `nodeAffinityPolicy: Honor` |
| `searchserviceService1AA04946` | `Service/search-service` | 移除 `service.kubernetes.io/topology-mode: auto` 注解（理由见第四、七节） |
| HPA `search-service-hpa` | 伸缩策略 | `averageUtilization` 60 → 120（等比补偿 request 减半） |

CDK 侧的 Deployment 片段应变成：

```ts
resources: {
  requests: { cpu: '256m', memory: '1Gi' },   // 实测用量 176–199m
  limits:   { cpu: '512m', memory: '1Gi' },   // 突发上限不变
},
topologySpreadConstraints: [
  {
    maxSkew: 1,
    topologyKey: 'topology.kubernetes.io/zone',
    whenUnsatisfiable: 'ScheduleAnyway',   // 硬约束需先解决容量，见第七节
    nodeTaintsPolicy: 'Honor',             // AZ 故障时自动放松而非死锁
    nodeAffinityPolicy: 'Honor',
    labelSelector: { matchLabels: { app: 'search-service' } },
  },
  {
    maxSkew: 1,
    topologyKey: 'kubernetes.io/hostname',
    whenUnsatisfiable: 'ScheduleAnyway',
    labelSelector: { matchLabels: { app: 'search-service' } },
  },
],
```

**在 CDK 落地之前，运行时改动会被下一次 `cdk deploy` 覆盖回去** ——
Service 与 Deployment 都带 `aws.cdk.eks/prune-*` label，说明它们是 CDK 托管的。

## 九、顺带发现，未处理

- **`petsite` 没有任何 CPU request**（实际用量 39m）。没有 request 的 Pod
  在调度打分和驱逐排序里都处于最不利位置，而它是入口服务。
- **`list-adoptions` / `pay-for-adoption` / `pethistory` 的 request 虚高 7–82 倍**。
  统一右调可以释放约 700m，但那是三个未被要求改动的生产服务，
  应当作为独立变更评审。
- **观测栈请求 2054m（33%）比被观测应用（1892m / 30%）还多**。
  对一个「可观测性演示平台」来说这本身就是个值得讲的数字。

## 十、方法论

这一轮两次靠**读组件自己给出的原因**纠正了我的推断，两次都比继续推理更快更准：

1. 猜「软偏好输给资源打分」 → 调度器事件说 `Insufficient cpu`，
   是偏好**根本无法满足**。
2. 猜「1a 有了端点提示就会正确」 → EndpointSlice 控制器事件说
   `3 endpoints, 2 zones` 超过过载阈值，是**端点数奇偶性**问题。

共同点：**Kubernetes 的调度器和控制器会把拒绝原因写进事件，
不要用推理去替代读它。** 与本项目 Q21 的教训同源 ——
先拿到能区分候选机制的测量，再下判断。
