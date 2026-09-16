# CloudWatch 日志量与成本发现（PetSite 非生产集群）

- 日期：2026-08-29
- 集群：`PetSite`（ap-northeast-1）
- 触发：addon 升级后核查"日志量有无跳变"，查出的跳变**不是升级引起的**

## 发现一：控制面审计日志于 08:12 被开启

`/aws/eks/PetSite/cluster` 在 06:00–08:25 UTC 整个窗口内**只有 3 个数据点**，08:10 之前完全无量：

| UTC（5 分钟桶） | IncomingBytes |
|---|---|
| 08:10 | **262.12 MB** |
| 08:15 | 6.83 MB |
| 08:20 | 22.62 MB ← 当日 addon 升级落在此桶 |

CloudTrail 定性：

```
EventName: UpdateClusterConfig
Time:      2026-08-29T08:12:51Z
Username:  Radium
Resource:  PetSite
```

当前 `cluster_logging` 配置：

```
audit, authenticator                  → enabled
api, controllerManager, scheduler     → disabled
```

262 MB 是开启瞬间的首次刷入。addon 升级仅贡献 08:20 桶的 22.62 MB（addon 更新的 API 调用 + 8 个 Pod 重建的 authenticator 记录），属预期量级。

### 量级估算

按 08:15 桶的 6.83 MB / 5 min 稳态推算：**约 1.97 GB/天**。

按东京区 CloudWatch Logs ingestion 单价量级换算月成本约在数十美元区间，但**具体金额请用 AWS Pricing Calculator 或 Cost Explorer 核算**，不要采用本文的估算值做预算依据。

`/aws/eks/PetSite/cluster` 的 retention 已设为 **7 天**，这一点是好的（限制了 storage 侧累积）。

### 建议

非生产集群长期开启 audit 的性价比低。两个方向：
- 用完即关（`eks update-cluster-config --logging`）
- 保留但确认 7 天 retention 足够，并纳入成本监控

## 发现二：三套观测栈并存

同一个非生产集群上同时跑着：

| 栈 | 状态 |
|---|---|
| DeepFlow（eBPF L7 + ClickHouse） | 自建，EC2 上 docker-compose 四容器 |
| AWS X-Ray | DaemonSet + 各服务 SDK |
| CloudWatch Container Insights | addon，**此前 3.5 个月零产出，2026-08-29 08:34 刚修通** |

Container Insights 从修通那一刻起才真正开始计费。此前的 8 个 Pod 属纯消耗（详见 `containerinsights-iam-zero-writes_20260829-0910.md`）。

四个 `/aws/containerinsights/PetSite/*` 日志组 retention 均为 **7 天**。

## 发现三：修通 Container Insights 的连带成本影响是双向的

正向（省）：观测自噪声引起的 DeepFlow 采集量下降 80.4%，ClickHouse 侧存储与查询成本随之下降（详见 `deepflow-noise-reduction_20260829-0910.md`）。

反向（增）：
- 四个 containerinsights 日志组开始 ingestion，`application` 组 36 条活跃流为主要量
- Container Insights 自定义指标开始计费（`metrics_collection_interval: 300`，已是省钱档）
- Application Signals 单独计费

净方向需在完整计费周期后用 Cost Explorer 观察，本次无法从几十分钟的窗口得出结论。

## 待决事项

1. audit 日志是否保留 —— 需要它的排查场景是否还在？
2. Container Insights 是否值得保留 —— 集群已有 DeepFlow + X-Ray，三套观测栈在非生产环境是否必要。若判定不必要，卸掉 `amazon-cloudwatch-observability` addon 比调优它更省。
3. 四个 containerinsights 日志组的 retention 是否从 7 天再缩短。
4. `ContainerInsights` / `ApplicationSignals` 指标至 09:10 仍为 0（距修复 35 分钟），需先确认这条链路真的通了，再谈它的成本与价值。
