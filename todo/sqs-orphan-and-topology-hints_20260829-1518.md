# 恢复流量后暴露的两个缺陷：SQS 孤儿队列与拓扑提示钉死

**日期**：2026-08-29 15:18 UTC

两个缺陷都不是新引入的回归，而是 traffic-generator 修复后**生产侧重新活跃，把原本休眠的问题照了出来**。

---

# 缺陷一：SQS 队列无消费者（未修，建议先不动）

## 事实

队列 `ServicesEks2-sqspetadoption2E8B1217-4T0KP1GHgwoj`：

```
ApproximateNumberOfMessages          916  （10 分钟前是 665，约 +25/分钟）
ApproximateNumberOfMessagesNotVisible  0
DLQ 深度                                0
VisibilityTimeout                    300
MessageRetentionPeriod            345600  = 4 天
RedrivePolicy      maxReceiveCount=3 → ServicesEks2-sqspetadoptiondlqEEEFF2AC-EQwBtdEFe8jQ
队列策略                              无（仅靠 IAM 身份策略授权）
```

## 排查链

**1. 无任何 Lambda 事件源映射。** 逐个遍历账号内 41 个 Lambda，唯一存在 ESM 的是 `neptune-etl-trigger` 指向它自己的队列。按队列 ARN 反查 `list-event-source-mappings` 返回空。

**2. CFN 模板里就没声明消费者。** `ServicesEks2` 栈共 193 个资源：

- 队列 `sqspetadoption2E8B1217` **只被一个资源引用**：SSM 参数 `petstorequeueurlB5F1CA11`（即 `/petstore/queueurl`）
- **没有任何 `AWS::Lambda::EventSourceMapping`**
- **没有任何 IAM 策略声明过 `sqs:` 动作**

`Applications` 栈（24 个资源）同样：无 SQS 资源、无 ESM、无 `sqs:` 动作。账号内唯一 ECS 集群属 `TranslatorStack`，无关。

**3. 生产者的授权来自托管策略。** petsite 的 SA 角色 `Applications-PetSiteServiceAccount6A1851C9-y5J6EGm1X5bM` 挂了 **`AmazonSQSFullAccess`**（还有 `AmazonSNSFullAccess`、`AmazonSSMFullAccess`、X-Ray，以及一条 `states:StartExecution` 内联语句）。所以模板不声明也能发。

**4. `pay-for-adoption` 不是消费者。** 其 IRSA 角色权限只有 Secrets Manager（Aurora 密钥）、SSM、DynamoDB（`BatchWriteItem`/`ListTables`/`Scan`/`Query`）、X-Ray——**完全没有 SQS 权限**。它通过 HTTP `/api/home/completeadoption` 直接落库，不走队列。

**5. 历史上从未工作过。** `NumberOfMessagesReceived` 近 14 天逐日全为 0。对照 `NumberOfMessagesSent` 前 13 天也全为 0，只有跨到今天的那个桶有 919——**那正是本次修复之后产生的**。DLQ 为 0 进一步佐证：从未有人消费过、更没有消费失败。

## 结论

**消费者从未部署。** 队列是架构占位：创建出来、URL 发布到 SSM 供生产者发现，但没有任何东西被接上来排空它。领养业务的真实落库走的是 petsite → `pay-for-adoption` 的 **HTTP 同步路径**；SQS 消息是一条**并行的、无人消费的孤儿路径**。

`ApproximateAgeOfOldestMessage` 已从 631s 增长到 1471s（20 分钟内），会持续增长直到 4 天保留期到点后静默过期。

## 影响与建议

| 维度 | 评估 |
|---|---|
| 功能 | 无影响——真实落库走 HTTP 路径 |
| 成本 | 可忽略（SQS $0.40/百万请求；即使 36k/天也约 $0.0005/天） |
| 监控 | 现有告警 `petsite-adoption-dlq-nonempty` 只盯 **DLQ**，主队列积压**无告警**，所以这个积压不会被任何人发现 |
| 韧性演练 | 这条链路只能压到**生产侧**，消费侧不存在，做混沌实验时不能把它当完整链路 |

**建议不要贸然加消费者**——那会改变应用语义，并可能与 HTTP 路径形成双写、重复落库。合理动作是二选一：

1. **记录为已知架构缺口**（推荐），并给主队列加一条 `ApproximateNumberOfMessagesVisible` 告警，使积压可见而不是静默过期。
2. 若确认要走事件驱动，则应先明确「HTTP 路径与 SQS 路径谁是权威」，再设计幂等消费者，而不是简单挂个 Lambda。

---

# 缺陷二：拓扑感知路由把 search-service 钉死（已修）

## 症状

两个 `search-service` Pod 负载 **76.1 / 23.9**，其余服务都在 56/44 到 67/33。

## 被推翻的第一个假设

最初判断是「.NET `HttpClient` 连接池复用导致长连接钉在单个 Pod」。**实测推翻**：

```
11.0.3.216 → 11.0.3.211   301 条连接  2139 请求  7.1 请求/连接
11.0.2.230 → 11.0.3.232   305 条连接  2130 请求  7.0 请求/连接
```

petsite 10 分钟开了约 300 条连接，**不是长连接**；而且 petsite 这一路本身是**均衡的**（2139 : 2130）。更进一步，`list-adoptions` 的 `11.0.2.45` 开了 **1875 条连接却全部落在 `11.0.3.232`**——kube-proxy 是 **iptables 模式**（非 IPVS），`sessionAffinity` 全为 `None`，1875 条新连接随机 DNAT 全落一个后端在概率上不可能。

## 真正的根因

EndpointSlice 上带**拓扑感知路由提示**：

```
11.0.3.232  zone=ap-northeast-1c  hints={forZones: [ap-northeast-1a]}   ← 只服务 1a 的客户端
11.0.3.211  zone=ap-northeast-1c  hints={forZones: [ap-northeast-1c]}   ← 只服务 1c 的客户端
```

与节点位置完全对应：

| 调用方 Pod | 节点 | 节点 AZ | 落到 |
|---|---|---|---|
| `11.0.2.45` list-adoptions | `ip-11-0-2-129` | 1a | `11.0.3.232` |
| `11.0.2.230` petsite | `ip-11-0-2-129` | 1a | `11.0.3.232` |
| `11.0.3.177` list-adoptions | `ip-11-0-3-205` | 1c | `11.0.3.211` |
| `11.0.3.216` petsite | `ip-11-0-3-141` | 1c | `11.0.3.211` |

来源是 CDK 打的注解 `service.kubernetes.io/topology-mode: auto`，存在于 `search-service`、`list-adoptions`、`pay-for-adoption`、`traffic-generator`；**未打**的 `pethistory-service` 与 `service-petsite` 的 EndpointSlice 里 `forZones` 正好是 `None`——完全自洽，交叉验证成立。

## 为什么这里是病理的

**两个 search Pod 都在 AZ 1c**（节点 `ip-11-0-3-141` 与 `ip-11-0-3-205`），而集群 4 个节点分布 1a×2 / 1c×2。EndpointSlice 控制器为了让两个 AZ 各承担约一半流量，只能把其中一个 1c 的端点标成「服务 1a」。

于是：**拓扑提示本意是避免跨 AZ 流量，但这里两个端点都在 1c，1a 的客户端无论如何都要跨 AZ——提示零收益，纯粹造成确定性钉死。** 这就是关闭它的依据。

偏斜的最终来源是调用方侧的量级不均（`11.0.2.45` 发了 6301 个 SYN vs `11.0.3.177` 的 1692），被拓扑钉死放大成 76/24。

## 修复

```bash
kubectl annotate svc search-service -n petadoptions service.kubernetes.io/topology-mode-
```

**只动 `search-service`**。`list-adoptions` 与 `pay-for-adoption` 的两个 Pod 确实分处 1a/1c，它们的提示在做真实工作（保持同 AZ、省跨 AZ 流量费），刻意不动。

## 验证

hints 立即清除（`forZones=None`），150 秒后新连接重新分布：

```
修复前   11.0.3.232  76.1%   11.0.3.211  23.9%
修复后   11.0.3.232  55.1%   11.0.3.211  44.9%

每个调用方现在都能同时打到两个 Pod:
  11.0.2.230 → 3.232: 242 / 3.211: 155
  11.0.2.45  → 3.232: 537 / 3.211: 456
  11.0.3.177 → 3.232: 612 / 3.211: 205
  11.0.3.216 → 3.211: 120 / 3.232:  34
```

## 遗留

1. **CDK 会改回去。** 该注解带 `aws.cdk.eks/prune-*` label，下次 CDK 部署会重新打上。要持久化必须改 CDK 源。
2. **更好的根治是让两个 search Pod 跨 AZ 分布。** 给 Deployment 加 `topologySpreadConstraints`（`topologyKey: topology.kubernetes.io/zone`, `maxSkew: 1`）后，拓扑提示就会变成**正确**的——每个端点服务自己所在 AZ，既均衡又真正省下跨 AZ 流量。届时可以把注解加回来。当前两个 Pod 同处 1c 纯属调度巧合，没有任何约束阻止它。
3. **`list-adoptions` 连接复用很差**：`11.0.2.45` 6301 个 SYN 对应 10,296 个请求 ≈ 1.6 请求/连接；`11.0.3.177` 是 2.7。相比 petsite 的 7.1，说明 `list-adoptions` → `search-service` 基本是每请求一连接，缺少 keep-alive。这是独立于本缺陷的一项优化空间。

## 方法论

这次连着犯了两个同类错误，都靠实测纠正：

1. 先按「连接复用」下结论——被 L4 SYN 计数推翻。
2. 覆盖度首查设 `LIMIT 40` 被截断，误报 `pethistory-service` 零流量。

共同点是**在证实机制之前就给出了机制解释**。凡是要给「为什么钉在一个 Pod / 为什么没流量」这类结论，必须先拿到能区分候选机制的测量（此处是 SYN 计数 + EndpointSlice hints + 注解交叉验证），再下判断。
