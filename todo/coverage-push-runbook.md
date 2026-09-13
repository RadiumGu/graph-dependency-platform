# 依赖证据覆盖率推进手册（自驱循环用）

**目标**：一跳依赖边里 `verify_status=confirmed` 达到 **≥24/47（50%）**。
起点：2026-09-13 为 10/47（21%）。

## 每轮流程

```bash
cd /home/ec2-user/works/graph-dependency-platform
export NEPTUNE_ENDPOINT=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com
export REGION=ap-northeast-1 AWS_DEFAULT_REGION=ap-northeast-1
export PYTHONPATH=infra/lambda/shared/python
```

1. **查覆盖率**：`compliance_export.take_snapshot()` + `Breakdown` 看
   `verify_status` 分布。**≥24 条 confirmed 就立刻 `autonudge_stop` 并报告**。
2. **跑 1~2 条**未评估边，跑完即结束本轮：
   ```bash
   python3.11 scripts/verify_via_iam_deny.py \
     --edge "<源服务>:<目标节点类型>:<目标名>" \
     --propagation-budget 240 --hold 180 --apply
   ```
   每条约 12 分钟（含 IAM 传播等待、测量、回滚、连续 8 次恢复确认）。
3. **报告**：本轮跑了哪几条、判定、当前 confirmed 计数、下轮打算跑哪条。短。

## 候选边（按优先级）

源服务都有独立 IRSA 角色，所以 deny 的半径恰好是一个服务。

| 优先 | 源服务 | 目标 | 为什么优先 |
|---|---|---|---|
| 1 | `petsite` | `SQSQueue` ×2、`SNSTopic`、`StepFunction`、`DynamoDBTable` | 业务探针最明确（adopt 探针直接覆盖支付链路） |
| 2 | `payforadoption` | `DynamoDBTable`、`StepFunction`、`RDSInstance` ×2 | 同上，adopt 探针 |
| 3 | `petsite` | `AWSServiceEndpoint`（sns / stepfunctions / sts） | 服务级抽象，退化可能被重试掩盖 |
| 4 | `payforadoption` | `AWSServiceEndpoint`(ssm)、`Microservice`(petsearch) | ssm 断了配置读不到，影响面可能过大 |
| 5 | `pethistory` ×2、`petlistadoptions` ×2 | `RDSInstance` | history / list 探针 |

## 直接跳过，不要浪费时间

- **任何 → `S3Bucket`**：`petsearch` 每次搜索请求都调 `CreateBucket` 且必然抛
  `BucketAlreadyOwnedByYouException`，稳态成功率就是 **0%**，基线闸门必拒。
  且 `presignGetObject` 是纯本地签名、不发网络请求 —— 这条边本身是那个 bug 的产物。
- **任何 → `ECRRepository`**：拉镜像是节点上的 kubelet 做的，Chaos Mesh 选择器
  只能选 Pod，切 Pod 网络对镜像拉取无效。
- `ServicesEks2-statusupdaterservicelambdafn...` 那 2 条：源是 Lambda，
  业务探针未覆盖。

## 纪律（一条都不能破）

1. **基线闸门被拒就换边，不要放宽。** 请求数 <20 或成功率 <95% 时任何退化
   数字都是噪声。放宽闸门等于制造假 confirmed。
2. **业务探针必须匹配源服务**，见 `chaos/code/runner/business_probes.py` 的
   `SERVICE_PROBES`。源服务没登记探针时**拒绝出 confirmed** ——
   没有业务证据的 confirmed 是过度声称。
3. **`observation_only` 不写回图谱。** 它的含义是「信号不足以判定」，
   写进 `verify_status` 会让它看起来像一个结论。
4. **每次实验后必须确认线上已恢复。** 脚本会连续 8 次探针确认；若报
   「未在预算内恢复」，立刻 `kubectl rollout restart deploy/<名> -n petadoptions`
   并确认恢复，然后**停止本轮**。
5. 实验期互锁由脚本自动置位（`~/.kiro/crew/crons/chaos_lock.py`），
   合成流量 cron 会整轮跳过，不必手动处理。

## 停止条件（任一满足即 `autonudge_stop`）

- `confirmed ≥ 24` —— 目标达成
- 连续 3 条边都被基线闸门拒或判成 `observation_only` —— 方法对剩余边失效，
  需换手段（不要硬跑）
- 任何一次回滚失败且 `rollout restart` 也没恢复线上 —— 必须停下报告，
  绝不能继续注入
- 剩余未评估边里已无「源服务有 IRSA 角色且有业务探针」的候选

## 手段的诚实边界

IAM deny 证明「这条依赖承重」（DORA Art. 8(4) / SYSC 15A.4.1R 的映射诉求），
**不等于**延迟、部分失败、超时重试场景下的行为测试 —— `AccessDenied` 立即返回，
与超时挂住的失败模式不同。边上写了 `verify_severance='iam-deny'`，
合规报告据此披露证据的适用范围。
