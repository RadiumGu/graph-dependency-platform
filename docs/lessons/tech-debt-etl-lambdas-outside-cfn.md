<!-- 归档溯源：本文件由 2026-09-21 的文档整理从下述原路径移入 docs/ -->
> 📄 **原路径**：`todo/tech-debt-etl-lambdas-outside-cfn.md`

# 技术债：3 个生产 ETL Lambda 不在任何 CloudFormation 栈里

**立卡日期**：2026-09-06
**发现路径**：查「为什么 105 个节点 scope=unknown」时，发现 3 个 ETL Lambda 判不出归属

## 事实

`NeptuneEtlStack` 里只有 2 个 ETL：

| 函数 | 创建方式 | scope 判据 |
|---|---|---|
| `neptune-etl-from-aws` | CloudFormation | 栈归属 → platform ✅ |
| `neptune-etl-from-deepflow` | CloudFormation | 栈归属 → platform ✅ |
| `neptune-etl-from-xray` | 手工 `aws lambda create-function` | **无栈归属** |
| `neptune-etl-from-agentcore` | 手工 `aws lambda create-function` | **无栈归属** |
| `neptune-etl-from-appsignals` | 手工 `aws lambda create-function`（2026-09-06 本人建） | **无栈归属** |

## 为什么这不只是「标签不好看」

`scope=unknown` **不在选边器的 `EXCLUDED_SCOPES`** 里
（`chaos/code/runner/edge_verification.py:75`，只排除
`platform / scaffolding / observability / cluster-infra`）。

所以这三个函数曾经作为**故障注入靶点**待在池子里。实测确认过一条：

```
neptune-etl-from-xray -AccessesData-> xray    (priority=1, src_scope=unknown)
```

平台会给自己的 ETL 注故障。

## 已做的处置（症状）

契约加了 `node_scope.name_prefix_map`：

```yaml
name_prefix_map:
  neptune-etl-from-: platform
  neptune-etl-trigger: platform
```

6 个 ETL 现在全部判为 platform，覆盖率 92.6% → 93.0%。

## 还没做的（病因）

**前缀规则让症状消失，但病因是三个生产 Lambda 绕过了基础设施即代码。** 后果：

1. **重建栈会漏掉它们。** 有人从 `NeptuneEtlStack` 重建环境，只会得到 2 个 ETL，
   另外 3 个（含 X-Ray 和 Application Signals 两个数据源）静默缺失 ——
   图上会少掉一批依赖边，而没有任何东西报错。
2. **配置漂移无人看管。** 实测 2026-09-06：4 个函数指向 Layer v19，
   `neptune-etl-from-appsignals` 还在 v15，因为重指 Layer 的人是手工逐个改的、漏了一个。
3. **前缀规则本身是个绕过。** 我在同一份契约里写着「业务资源不要加进来，
   它们的权威声明在 profile」，而这条规则做的事正是给一批**本该由栈声明**的
   资源另开一个声明源。它是权宜之计，不是设计。

## 建议

把 3 个手工 Lambda 纳入 `NeptuneEtlStack`（或一个新栈），
然后**删掉 `name_prefix_map` 里的这两条** —— 栈归属能判出来时，前缀规则就该退场。

判断这件事已完成的标准：删掉 `name_prefix_map` 后跑
`scripts/label_node_scope.py`，6 个 ETL 仍全部为 platform。

## ✅ 已完成（2026-10-06）

3 个函数用 `cdk import` 纳入 `NeptuneEtlStack`，`name_prefix_map` 已删除。
验收：`scripts/label_node_scope.py` 下 8 个 `neptune-etl*` 节点**全部经
CloudFormation 归属**判为 platform，unknown 维持 101 条（与删除前一致，无回归）。
栈内 Lambda 从 4 个增至 7 个。

过程中踩到四件事，都比纳入栈本身更值得记：

**① 非 ASCII 的 Description 造成永不收敛的脏 diff，永久阻塞 import。**
4 个 Lambda 的 `description` 里写的是 `→`，而线上模板存的是字面 `?`。
部署过 3 次都没收敛。而 `cdk import` 的前置条件是「没有待结算的资源更新」，
所以它把 import 卡死了。`cdk diff` 默认还把这 4 条折叠成一行
「Omitted 4 changes because they are likely mangled non-ASCII characters」，
要 `--strict` 才看得见。已全部改 ASCII `->`。

**② `AWS::Lambda::Permission` 不可导入，连带 3 条 EventBridge 规则也进不了栈。**
CFN 的 IMPORT changeset 只能导入、不能创建，而 `targets.LambdaFunction`
必然带一个 Permission 资源。CDK 会把它标成 `skipping`，但它仍在模板里，
于是 CFN 报 `Cannot invoke "String.split(String)" because "pid" is null`。
**现状：3 个函数已在栈内，3 条规则与 3 个 Permission 仍在栈外（手工）。**
规则继续正常触发（它们指向函数 ARN，import 不改 ARN）。
这是本次遗留的、比原问题小一号的同类债。

**③ import 之后 CFN 记录的 Code/Layer 是模板里的新值，而函数仍跑旧代码。**
`cdk import` 不修改资源，但把模板属性记成了「当前状态」。于是
`cdk diff` 干净、`cdk deploy` 也不会推代码 —— 一个静默漂移。
修法不是造个假改动，而是用 CFN 已记录的那个 S3 对象去
`update-function-code`，让现实与记录一致。

**④ 我自己把 `botocore-current` 层弄丢了。**
修复 ③ 时我读的是 `Layers[0].Arn`（单数），`update-function-configuration
--layers` 只写回一个层，`neptune-etl-from-agentcore` 的
`botocore-current:1` 被丢掉。症状不是报错，而是 `test_75` 变红：
Lambda 自带 botocore 不认 `targetConfiguration` 这个 tagged union 的
`http` 成员，**静默剥掉**它，于是 5 个 AGENTCORE_RUNTIME 网关 target
被建成 AgentTool 节点。已在 CDK 里显式声明该层（否则下次部署再丢一次），
并用 `scripts/purge_phantom_gateway_targets.py` 清掉残留。

**附带修好的一个缺口**：删掉前缀规则后 unknown 从 101 升到 103 ——
多出来的是 `neptune-etl-trigger-queue` 与 `-dlq`。它们**本来就由本栈声明**，
只是 SQS 的 `PhysicalResourceId` 是队列 URL 而图里的 `name` 是队列名，
`load_stack_index` 查不中。已让索引额外登记 URL 形态物理 ID 的尾段。
值得注意的是：那条前缀规则同时遮住了它要解决的问题**和这个无关的问题** ——
权宜之计的遮盖范围往往比设立它的人以为的更宽。

## 相关

- 选边器对 `unknown` 的处置是个未决问题：当前是「照选」。
  我判断**不该**改成一律降级 —— `petfood` / `trafficgenerator` / 5 个 `AgentTool`
  也是 unknown 而它们是真业务节点，降级会把它们一起压掉，
  等于从另一个方向重造「判不出被当成不重要」的盲区。
  倾向的方向是 annotate + warn（把 scope 未解析的靶点显式报出来），不改选择行为。

---

# 同类问题的第二例：业务 Aurora 集群与 CDK 声明不一致

**记录日期**：2026-09-07
**发现路径**：查「为什么 fis_rds_failover 跑不出可信结论」时发现

这一条与上面的 ETL Lambda 是**同一种病**（实盘绕过 IaC），但方向相反：
上面是资源不在栈里，这里是资源在栈里、却被带外改掉了。

## 事实

`serviceseks2-databaseb269d8bb-efjeyzicx2ak`（aurora-postgresql 16.11，DB=`adoptions`），
由 `ServicesEks2` 栈管理（`logical-id: DatabaseB269D8BB`，`ManagedBy=cdk`）。

CDK 源码（`PetAdoptions/cdk/pet_stack/lib/services-eks.ts`，自 2026-02-15 起）声明：

```ts
writer: rds.ClusterInstance.provisioned('writer', { instanceType: T4G.MEDIUM }),
readers: [ rds.ClusterInstance.provisioned('reader1', { promotionTier: 1,
                                            instanceType: T4G.MEDIUM }) ],
```

2026-09-07 实盘（修复前）：

| | CDK 声明 | 实盘 |
|---|---|---|
| writer | `db.t4g.medium` provisioned | **`db.serverless`** |
| reader1 | `db.t4g.medium`，tier 1 | **不存在**（`DBInstanceNotFound`） |
| 集群成员数 | 2 | **1** |

而 CloudFormation 侧 `Databasereader1F54479B8` 的状态是 **`UPDATE_COMPLETE`**，
物理 ID `serviceseks2-databasereader1f54479b8-hpyukqlufzus` —— **栈认为这个实例存在，
而 RDS 侧根本没有它**。栈本身最后一次更新是 2026-09-04T19:15:51，状态正常。
也就是说这两处漂移都发生在带外，CloudFormation 完全不知情。

## 已做的处置

补建了 reader，**刻意复用 CloudFormation 期待的那个标识符**
（`serviceseks2-databasereader1f54479b8-hpyukqlufzus`），而不是另起新名 ——
这样漂移从「资源缺失」缩小到「规格不一致」，而规格不一致在 writer 上本来就存在。
另起新名只会多一个栈不认识的资源，让情况更糟。

规格取 `db.serverless`（与现有 writer 一致）、AZ 取 `ap-northeast-1c`
（writer 在 `1a`，否则跨 AZ failover 测不出东西）、`PromotionTier=1`（与声明一致）。
标签 `ManagedBy=cdk-drift-repair` / `Purpose=chaos-failover-target`。
成本：Serverless v2 按 ACU 计，集群 `Min 0.5 / Max 4.0`，空闲约 $0.06/小时。

## 刻意没做：把 CDK 改成 serverless 以消除差异

两个理由：

1. **那等于把一次来历不明的手工变更固化成声明。** 不知道当初为什么把 writer
   改成 `db.serverless` —— 可能是有意的成本优化，也可能是某次调试的残留。
   在不知道意图的情况下改 CDK，等于宣布「实盘是对的、声明是错的」，
   而方向可能正好相反。
2. **`one-observability-demo` 的 origin 是公开上游 `aws-samples/one-observability-demo`。**
   把一个公开示例的基础设施定义改成匹配某一套私有部署的漂移，对上游不合适。
   （本仓库只推 `myfork`。）

## 遗留风险

**下次 `cdk deploy ServicesEks2` 会尝试把两个实例的规格改回 `db.t4g.medium`**，
那是一次带中断的变更，且没人会预期到。这个栈还管着 EKS、6 个 AgentCore 构造、
全部微服务，爆炸半径很大 —— 不该为了一个实例规格去动它。

要收口，先弄清当初改成 serverless 的意图，再决定是改声明还是改实盘。

