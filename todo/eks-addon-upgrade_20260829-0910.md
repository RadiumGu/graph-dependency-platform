# PetSite EKS Addon 升级记录

- 日期：2026-08-29
- 集群：`PetSite`（ap-northeast-1，k8s 1.35，platform `eks.20`，状态 ACTIVE）
- 性质：非生产集群，用户明确授权升级
- 执行路径：EKS 控制面 API（boto3 `UpdateAddon`），未经 eks-mcp-server

## 结果

| Addon | 升级前 | 升级后 | updateId | 结果 |
|---|---|---|---|---|
| `amazon-cloudwatch-observability` | v4.10.0-eksbuild.1 | v6.5.0-eksbuild.1 | `abfd799e-d849-3bb5-8aa5-855ae6f33929` | Successful |
| `aws-ebs-csi-driver` | v1.55.0-eksbuild.2 | v1.65.0-eksbuild.1 | `0077d756-00e9-393d-b0ee-d92776b516e2` | Successful |
| `aws-network-flow-monitoring-agent` | v1.1.3-eksbuild.1 | v1.1.6-eksbuild.1 | `ac80ba0f-6be9-36ff-85f9-055ed05404af` | Successful |
| `eks-pod-identity-agent` | v1.3.10-eksbuild.2 | v1.3.10-eksbuild.3 | `488f7af0-a21e-3807-ace2-34489006194a` | Successful |
| `aws-guardduty-agent` | v1.16.0-eksbuild.2 | 未动 | — | 已是 defaultVersion |

四个 update 全部 `Successful`、`errors: []`，addon 状态 ACTIVE、`health.issues` 全空。

## 两个执行决策

**目标版本取 `defaultVersion` 而非列表最新。** 只有 `eks-pod-identity-agent` 两者不一致：AWS 为 k8s 1.35 标记的 default 是 v1.3.10-eksbuild.3，列表里另有更新的 **v1.4.0-eksbuild.1**。AWS 未将 1.4.0 提为该 k8s 版本的推荐档，故停在 default。

**`resolveConflicts: OVERWRITE`，但把原有 `configurationValues` 显式回填。** 三个 addon 带自定义配置（observability、network-flow-monitoring-agent、pod-identity-agent），先 `DescribeAddon` 取出再原样透传，避免 OVERWRITE 清空用户配置。`aws-ebs-csi-driver` 本无配置值。

## 验证方式

1. `DescribeUpdate` 轮询至终态 —— 4/4 Successful。
2. `DescribeAddon` —— 版本落位、ACTIVE、health 无 issue。
3. **工作负载层**：`status.phase!=Running` 全命名空间查询返回 **0 个 Pod**。这一步比 addon ACTIVE 更硬 —— addon 状态为 ACTIVE 时底层 Pod 仍可能 CrashLoopBackOff。
4. Pod label 版本核对：`ebs-csi-*` 的 `app.kubernetes.io/version: 1.65.0`；`cloudwatch-agent` 镜像版本 `1.300071.0b1720`；`eks-pod-identity-agent` `pod-template-generation: 3`；`fluent-bit` 随 observability addon 一起重建至 `generation: 3`。

Pod 在 08:21–08:22 UTC 完成滚动重建。

## 回滚参照

```
amazon-cloudwatch-observability    v4.10.0-eksbuild.1
aws-ebs-csi-driver                 v1.55.0-eksbuild.2
aws-network-flow-monitoring-agent  v1.1.3-eksbuild.1
eks-pod-identity-agent             v1.3.10-eksbuild.2
```

## 遗留项

- `eks-pod-identity-agent` 可再升至 v1.4.0-eksbuild.1（超出 AWS 推荐档，非生产可考虑）。
- 集群未安装 `vpc-cni` / `coredns` / `kube-proxy` 作为 EKS managed addon —— 这三者以自管方式运行（`aws-node`、`coredns`、`kube-proxy` Pod 存在但无对应 addon 条目），所以本次升级的爆炸半径不含核心网络组件。若要纳管需单独评估。

## 连带发现

升级本身干净，但升级后被问到"采集量有无跳变"，由此查出两条与升级无关的既存问题，分别记录在：

- `containerinsights-iam-zero-writes_20260829-0910.md`
- `cloudwatch-cost-findings_20260829-0910.md`
