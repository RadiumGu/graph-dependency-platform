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

