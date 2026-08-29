# DeepFlow 观测自噪声压制：73.2% → 4.6%，总采集量 -80%

- 日期：2026-08-29
- 集群：`PetSite`（ap-northeast-1）
- 数据源：DeepFlow ClickHouse `flow_log.l7_flow_log`（11.0.2.30:8123）
- **时区注意**：该 ClickHouse 服务端时区为 **UTC+8**，本文表格中 `16:xx` 为服务端本地时间，对应 UTC `08:xx`

## 三阶段实测（等长 7 分钟窗口）

| | S1 修复前<br>16:24–16:31 | S2 仅修 IAM<br>16:37–16:44 | S3 +ndots+关 scraper<br>16:58–17:05 |
|---|---|---|---|
| 总行数 | 113,998 | 37,482 | **22,335** |
| 行/分钟 | 16,285 | 5,355 | **3,191** |
| `logs.*` 自噪声行数 | 83,414 | 7,144 | **1,028** |
| 自噪声占比 | **73.2%** | 19.1% | **4.6%** |
| NXDOMAIN 行数 | 70,507 | 9,388 | **2,262** |
| NXDOMAIN 占比 | 61.8% | 25.0% | **10.1%** |

累计降幅：总量 **−80.4%**，`logs.*` 自噪声 **−98.8%**，NXDOMAIN **−96.8%**。

对照组：S1 与 S2 的"非 logs 流量"分别为 30,584 与 30,255（−1.1%），业务侧 `search-service.petadoptions` 五个变体两窗口恒为 2,688 行 —— 证明降幅全部来自观测自噪声，未误伤业务采集。

## 机制：ndots 搜索域展开，不是 HTTP 重试

`l7_protocol_str` 为 **DNS**，不是 HTTP。这一点最初判断错了 —— 曾表述为"失败的 PutLogEvents HTTP 重试"，实际被 eBPF 记下的是 DNS 层的放大效应。

五个域名变体正是 Kubernetes `ndots:5` 的搜索路径展开：

```
logs.ap-northeast-1.amazonaws.com                                      ← 真名，rcode 0
logs.ap-northeast-1.amazonaws.com.amazon-cloudwatch.svc.cluster.local  ← NXDOMAIN
logs.ap-northeast-1.amazonaws.com.svc.cluster.local                    ← NXDOMAIN
logs.ap-northeast-1.amazonaws.com.cluster.local                        ← NXDOMAIN
logs.ap-northeast-1.amazonaws.com.ap-northeast-1.compute.internal      ← NXDOMAIN
```

S1 的 rcode 分布：NXDOMAIN(3) 66,508 : 成功(0) 16,906 ≈ **3.93 : 1**，正好是"1 次投递 = 5 次 DNS 查询、4 次白查"。

完整因果链：`AccessDenied` 是 permanent error → agent 每次 flush 被拒后立刻 force flush 重试 → 每次重试触发一轮 5 次 DNS 查询 → 4 次 NXDOMAIN 打进 CoreDNS 并被 eBPF 全量抓下。IAM 修通后重试循环消失，回到 `metrics_collection_interval: 300` 的正常节奏。

两个 DaemonSet 均为 `hostNetwork: true` + `dnsPolicy: ClusterFirstWithHostNet` —— 后者是"用宿主网络但仍走集群 DNS 与集群搜索域"，这是搜索域展开的来源。

## 改动一：DaemonSet `ndots=1`（无官方路径，直接 patch）

对 `amazon-cloudwatch` 命名空间的 `cloudwatch-agent` 与 `fluent-bit` 打：

```json
{"spec":{"template":{"spec":{"dnsConfig":{"options":[{"name":"ndots","value":"1"}]}}}}}
```

`dnsPolicy` 保持 `ClusterFirstWithHostNet` 不变，search 域仍保留作兜底 —— 只改尝试顺序，不削弱解析能力。Pod 内实测 `/etc/resolv.conf`：

```
search amazon-cloudwatch.svc.cluster.local svc.cluster.local cluster.local ap-northeast-1.compute.internal
options ndots:1
```

效果：S3 中 `logs.*` 的 4 个 NXDOMAIN 变体**全部归零**，只剩 1,028 行成功查询。

### 为什么只能直接 patch

两条官方路径都不存在，已核实：

| 路径 | 结果 |
|---|---|
| `AmazonCloudWatchAgent` CRD（`amazoncloudwatchagents.cloudwatch.aws.amazon.com`） | 全 CRD 中含 `dns` 的字段路径 **0 个**；CR spec 顶层 23 个键里有 `hostNetwork` 但无 dnsConfig/dnsPolicy |
| addon `configurationSchema`（`describe-addon-configuration`） | 含 `dns` 的路径 **0 个**。13 个可配节：`admissionWebhooks` `agent` `agents` `applicationSignals` `containerInsights` `containerLogs` `dcgmExporter` `kubeStateMetrics` `manager` `neuronMonitor` `nodeExporter` `otelContainerInsights` `tolerations` |

### 耐久性：比预期好，但仍属脆弱

`cloudwatch-agent` DaemonSet 由 operator 从 CR 生成（`ownerReferences: AmazonCloudWatchAgent`，`managed-by: amazon-cloudwatch-agent-operator`），本以为会被 reconcile 掉。实测：

- 打完 patch 后 20 秒未回滚
- 随后一次 `resolveConflicts: OVERWRITE` 的 addon 更新重渲染后，两个 DaemonSet 的 `dnsConfig` **均保留**

但 operator 的 reconcile 触发条件未摸清，CR 被改动或 addon 再升级仍可能丢。**建议定期核查或写入 IaC。** 核查命令：

```
kubectl get ds -n amazon-cloudwatch cloudwatch-agent fluent-bit \
  -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.spec.template.spec.dnsConfig}{"\n"}{end}'
```

## 改动二：关闭 dcgmExporter / neuronMonitor（官方路径，持久）

通过 addon `configurationValues` 追加：

```json
{"dcgmExporter": {"enabled": false}, "neuronMonitor": {"enabled": false}}
```

update `f15c5669-3f59-3c42-935f-8eab62c4c947`，Successful，零 error。

依据：这两个 scraper 分别面向 NVIDIA GPU（DCGM）与 AWS Inferentia/Trainium（Neuron），而集群 4 个节点全是 **t4g.large（Graviton2）**，既无 GPU 也无加速器，对应 Service 不存在，只能永久产生 NXDOMAIN。它们在 08:26:54 由 Prometheus scraper 启动后出现，S1 480 行 / S2 840 行，S3 **完全消失**（两个 DaemonSet 已从集群移除）。

此路径走 addon 配置，**跨 addon 升级持久有效**。有价值的节（`agent` `containerInsights` `applicationSignals` `containerLogs` `nodeExporter` `kubeStateMetrics`）一个未动。

## 剩余 4.6% 的性质

剩余 1,028 行/7分钟（约 147/分钟）全部是 `logs.ap-northeast-1.amazonaws.com` 的**成功查询**，即 4 个节点每 300 秒投递 EMF + 容器日志时必需的解析。再压只有两条路，边际收益已很小：

1. 加长 `metrics_collection_interval` —— 牺牲指标粒度
2. 给 deepflow-agent 加采集过滤，把观测流量排除在 L7 采集之外 —— 治采集侧而非源头

剩余 NXDOMAIN 2,262 行（10.1%）已不来自 `amazon-cloudwatch` 命名空间（该部分归零），而是集群其他服务的短名解析，典型如业务侧 `search-service.petadoptions.svc.cluster.local` 的五个变体（恒定 2,688 行）。要动需改业务 Pod 的 dnsConfig，超出观测组件范围。

## 回滚参照

- 两个 DaemonSet 原 `dnsConfig` 为空（字段不存在）
- addon 原 `configurationValues` 仅含 `agent` 一节，无 `dcgmExporter` / `neuronMonitor`

## 与既有记录的关系

此前盘点记录的"约 76% 的 DeepFlow L7 采集为观测自噪声、约 60 万行/小时"，本次实测 S1 为 73.2%、16,285 行/分钟（≈98 万行/小时），量级一致。该项曾被标记为"最高价值的成本优化"，现已完成源头治理部分。
