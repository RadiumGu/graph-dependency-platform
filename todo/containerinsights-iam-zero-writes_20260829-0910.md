# Container Insights 零写入：IAM 缺失导致约 3.5 个月观测数据全丢

- 日期：2026-08-29
- 集群：`PetSite`（ap-northeast-1）
- 严重度：高 —— 8 个 Pod 持续运行、消耗资源与网络，产出为零
- 性质：既存问题，**与当日 addon 升级无关**（v4.10 与 v6.5 走同一条投递路径）

## 症状

1. `/aws/containerinsights/PetSite/{performance,application,dataplane,host}` 四个日志组 `storedBytes = 0`。
2. 06:00–08:25 UTC 共 2.4 小时窗口内，四个组的 `IncomingBytes` **零个数据点**。
3. `performance` 组 `DescribeLogStreams` 返回 **0 个 log stream** —— 连流都没建起来。
4. `ContainerInsights` 与 `ApplicationSignals` 两个 CloudWatch 命名空间 `ListMetrics` 均为 **0 个指标**。
5. 但 8 个 Pod（4× cloudwatch-agent + 4× fluent-bit）全部 `1/1 Running`，agent 日志显示 `Everything is ready. Begin running and processing data.`

## 根因

agent 自己把错误原文打出来了（`kubectl logs -n amazon-cloudwatch`）：

```
AccessDeniedException: User: arn:aws:sts::926093770964:assumed-role/
  ServicesEks2-petsiteNodegroupworkers1cNodeGroupRole-rQ11jg5VfTw9/i-032e64effbbed37dd
is not authorized to perform: logs:PutLogEvents on resource:
  arn:aws:logs:ap-northeast-1:926093770964:log-group:/aws/containerinsights/PetSite/performance
because no identity-based policy allows the logs:PutLogEvents action
```

伴随：`"Exporting failed. Rejecting data.", "rejected_items": 702`

**身份链路**：addon 的 `serviceAccountRoleArn` 为 `null`、`podIdentityAssociations` 为空数组 —— 既无 IRSA 也无 Pod Identity，凭据落到**节点实例角色**。两个 nodegroup 的角色策略仅 4 条：

```
AmazonSSMManagedInstanceCore
AmazonEKS_CNI_Policy
AmazonEC2ContainerRegistryReadOnly
AmazonEKSWorkerNodePolicy
```

inline 策略 0 条。**`CloudWatchAgentServerPolicy` 从未挂载** —— 而这条策略才提供 `logs:PutLogEvents` / `CreateLogStream` / `CreateLogGroup` / `cloudwatch:PutMetricData`。

## 持续时长

`application` / `dataplane` / `host` 三组存在历史 stream，最后事件时间戳 `1778613528775` = **2026-05-12**，之后中断。故失效已约 3.5 个月。`performance` 组则连流都没有过。

## 修复

给两个节点角色附加托管策略（additive，原 4 条未动）：

```
arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy   （默认版本 v3）
```

角色：
- `ServicesEks2-petsiteNodegroupworkers1aNodeGroupRole-YXpfPBKFoRCE`（nodegroup `petsiteNodegroupworkers1a60-ZJElxYDbKT8H`）
- `ServicesEks2-petsiteNodegroupworkers1cNodeGroupRole-rQ11jg5VfTw9`（nodegroup `petsiteNodegroupworkers1cFE-rBSMHnU8raBy`）

已核对 v3 策略文档确实包含缺失的四个动作，另附带 `xray:PutTraceSegments` / `PutTelemetryRecords` / `GetSamplingRules` / `GetSamplingTargets` / `GetSamplingStatisticSummaries`、`ec2:DescribeTags` / `DescribeVolumes`、`logs:PutRetentionPolicy`、`ssm:GetParameter`。

挂载时刻约 **08:34 UTC**。

## 验证

**agent 侧零拒绝** —— 四个 Pod 最后一次 `AccessDeniedException`：

| Pod | 最后拒绝时间 |
|---|---|
| `cloudwatch-agent-c577f` | 08:31:50Z |
| `cloudwatch-agent-2vwv9` | 08:31:59Z |
| `cloudwatch-agent-zkccx` | 08:32:01Z |
| `cloudwatch-agent-tmbbr` | 08:33:32Z |

全部早于挂载时刻，之后再无一条。

**日志组恢复写入**（按 `lastEventTimestamp > 08:33:20Z` 筛新鲜流）：

| 日志组 | 新鲜流数 | 备注 |
|---|---|---|
| `performance` | 4 | 从 0 个流起步，09:10 时已覆盖 4 个节点 |
| `application` | 36 | 容器日志恢复（此前停在 2026-05-12） |
| `dataplane` | 4 | 4 节点 kubelet/containerd |
| `host` | 4 | 4 节点 host.messages |

## 一处需要更正的早期判断

初次看到 `application` 组为空时，我依据 addon `configurationValues` 里只声明了 `metrics_collected`、没有 `logs_collected` / `container_logs`，推断"容器日志采集压根没开"。**这个判断是错的**：fluent-bit 走自己独立的采集路径，不读 agent 的那段配置。IAM 修好后 `application` 组立刻恢复到 36 条活跃流，证明堵点始终是同一个权限缺失。

教训：同一个根因造成多个症状时，不要给每个症状单独编一个解释。

## 遗留项（未解决）

**`ContainerInsights` 与 `ApplicationSignals` 的 `ListMetrics` 至 09:10 仍为 0**，距修复已约 35 分钟，而 `performance` 组已有 4/4 节点在写。EMF 落入日志组后 CloudWatch 应自动抽取为指标，35 分钟仍无指标**超出正常传播延迟**，需进一步查：

- 写入 `performance` 组的事件是否包含合规的 `_aws.CloudWatchMetrics` 结构
- `cloudwatch:PutMetricData` 是否另有资源级限制
- Application Signals 是否需要额外的 onboarding 步骤（不只是 agent 配置开关）

## 成本影响

修复前三个多月这 8 个 Pod 白跑，CloudWatch 侧零费用。修复后开始真实计费：四个日志组 ingestion（`application` 36 条活跃流为主要量）+ Container Insights 自定义指标 + Application Signals 单独计费。四个组 retention 均为 7 天。

`metrics_collection_interval: 300` 已是省钱档（默认 60 秒）。

详细成本讨论见 `cloudwatch-cost-findings_20260829-0910.md`。

## 回滚

```
iam detach-role-policy --policy-arn arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy
```
对上述两个角色执行即可，其余配置未动。
