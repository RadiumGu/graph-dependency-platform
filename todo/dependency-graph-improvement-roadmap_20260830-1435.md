# 依赖图谱改进路线 与 对外答辩要点

**日期**：2026-08-30 14:35 UTC
**依据**：`industry-dependency-management-practices_20260830-1435.md`（业界机制）、`graph-correctness-audit-and-gaps_20260830-1435.md`（现状实测）、`etl-complexity-and-maintenance_20260830-1435.md`（复杂度量化）

---

## 零、一个先验的修正

此前的判断是「本系统的缺陷全部落在三类——粒度错配、写了但没人读、身份不唯一——所以瓶颈在数据契约而非采集覆盖面」。

调研后这个判断**证实了一半，需要修正一半**：

- **证实**：业界对「多源写一张依赖图」的成熟答案确实是数据契约，而且是声明式、可评审、可校验的契约（New Relic entity-definitions、ServiceNow IRE、Cartography schema-generated cleanup）。方向没错，而且本项目的三类缺陷在实测中全部被再次确认。
- **需要修正**：「采集覆盖面不是瓶颈」说过头了。**连接池与长连接造成的系统性假阴性是一个独立的第四类问题**——它不是「少接了一个源」，而是已有源的机制盲区，业界至今没有公开定量。这一类必须单列，不能并入前三类。

---

## 一、改进路线（按「被问倒风险 × 修复成本」排序）

### P0-1 — 让 schema 在运行时具备否决权

**为什么最优先**：`petsite.yaml` 被宣称为权威 schema，但四个 ETL 无一 import 它（`profiles/schema.py:94` 定义为不解析的 `Optional[str]`，`:145` 还 `extra: allow`）。任何拼错或新造的标签会被静默写进 Neptune。这是外部专家最先会戳穿的一点，也是 ETL 复杂度与维护困难的共同根因。

**做法**（抄 New Relic entity-definitions）：

1. 把 `graph_schema_text` 从自由文本块升级为**结构化声明**。每个节点类型至少声明：
   - `identity_key`：身份属性名，且**必须是不可变字段**
   - `required_provenance`：必须写入的溯源属性集
   - `expires`：该类型的 TTL（见 P2-1）
   每个边类型至少声明：两端允许的节点类型、`dependency_kind` 取值域、`required_provenance`、`expires`。
2. **`relationshipType` 做成闭集**（New Relic 的 14 种是范例），不允许随手加新边类型。
3. ETL 侧加一个门禁函数，`upsert_vertex` / `upsert_edge` 入口第一件事就是校验 label 已声明、身份属性已提供、必需溯源属性齐全；不满足**抛异常而不是静默写入**。
4. 抄 New Relic 的冲突约束并做成校验：**同名属性不能为两个不同类型抽取 identifier，除非用 conditions 区分，且一个类型的 conditions 不能是另一个的超集。**

**顺带解决**：这一步做完，`etl_aws/neptune_client.py:149` 那句「规范定义见 profiles/petsite.yaml」的注释就变成真的了。

---

### P0-2 — 身份键从不变量派生

**为什么同等优先**：`etl_aws/neptune_client.py:101-108` 默认 `id_key='name'`，只有 EC2 传了不可变的 `instance_id`（`handler.py:145`）。而代码自己的注释（`neptune_client.py:62-77`）记录着「实测 14 个 EC2Instance 里 4 个是重复实体」。30 多种其它类型仍走可变的 `name`。

业界共同模式：**身份键 = 从属性确定性派生；派生输入变 → 产生新实体，而不是同实体改名。** Dynatrace 强调 ID 稳定性正是因为 upsert 靠稳定 ID 合并观测；New Relic 是 identifier 变 → GUID 变 → 新 entity。

**做法**：

1. 在 P0-1 的结构化 schema 里逐类型声明 `identity_key`，取值必须是不可变字段——AWS 资源用 **ARN**，EC2 用 `instance_id`，CFN 用 `physical_id`（`etl_cfn:331-338` 已经是了），K8s 对象用 `uid` 或 `namespace/name`。
2. 把 `name` 降级为普通可变属性。
3. 把 `tests/test_34_ec2_identity_and_nfm.py:22/48` 那两个断言**参数化推广到全部节点类型**，并把 `:164` 的重复实体检测从**仅告警改为失败**。

**风险与迁移**：改身份键会让已有节点在下一轮 upsert 时被当成新实体。需要一次性迁移脚本按新旧身份键做合并，不能直接切。这一点必须在方案里写清楚，否则会造成大批重复节点。

---

### P1-1 — 统一 upsert / cleanup 层，由 schema 生成

**抄 Cartography 的 `update_tag` 范式**：

```
每轮 sync 启动时生成单调时间戳 update_tag
  → 所有 ETL 把它作为统一时间戳字段写到每个节点和每条边
  → cleanup 处理 lastupdated ≠ 本轮 update_tag 的一切
```

关键细节（三条本项目现在都缺）：

- **`scoped_cleanup`**：只处理「连到当前正在同步的 sub-resource」的陈旧对象。本项目现在是单账号单 VPC，一旦扩到多账号就会互删——**这条要在扩展之前加**。
- **`cascade_delete`**：父节点陈旧被删时连带一层子节点；本轮被重新挂到新父的子节点受保护。
- **`ON CREATE SET firstseen` 只设一次**，后续不动。

**这一步一举解决四件事**：

| 现状 | 位置 | 解决方式 |
|---|---|---|
| 4 套手写 upsert | `etl_aws/neptune_client.py:56/155`、deepflow `:1097/1171`、xray `:521/672`、cfn `:71/115` | 由 schema 生成 Load |
| 3 处缺失的 cleanup | `AccessesData`/`DependsOn` 永不失效；CFN 边完全不清理；AWS 不删任何边 | 由 schema 生成 Cleanup，覆盖全类型 |
| 时间戳字段名漂移 | `last_updated`(aws) / `last_scanned`(cfn) / `last_seen`(deepflow, xray) | 统一为一个字段 |
| `run_etl` 1,234 行 | `etl_aws/handler.py:62` | Load/Cleanup 部分整体消失，只留 Get/Transform |

**同时必须显式区分 Cartography 的两种多源写法**：

- **Simple Relationship Pattern**：只按 ID 引用对端、不带对端额外属性 → 只定义关系 schema，`MATCH` 已有节点连边
- **Composite Node Pattern**：引用对端且带对端自己没有的字段 → 定义「A 眼中的 B」schema，target 同一个 label，`MERGE` 累积属性

`Microservice.az` 曾累积成两个值（`neptune_etl_deepflow.py:1097` 注释）就是把 Composite 当 Simple 写的后果。

**顺带清掉**：Neptune 客户端的 2 份字节级副本（`etl_aws/neptune_client_base.py` ≡ `shared/python/neptune_client_base.py`，md5 均 `d019cdb…`）+ 1 份分叉（`rca_window_flush/neptune/neptune_client.py`，md5 `c64f34e…`）；以及重复 2 次的 ECR 解析、散落 6 处的 ARN 归名、散落 7 处的时间窗计算。

---

### P1-2 — 属性级权威表

**抄 ServiceNow IRE**，落成一张四列表：

```
(节点/边类型, 属性) → [(来源, 优先级, 是否受保护, 陈旧后接管)]
```

**要修的具体不一致**：现在 X-Ray（`neptune_etl_xray.py:719-725`）、DeepFlow-L4（`:565-568`）、NFM（`cloudwatch.py:514`）**保护** `source`；而 AWS 通用边（`neptune_client.py:161`）和 CFN（`neptune_etl_cfn.py:143`）**无条件覆盖** `source`——同一条边的首次发现者会被后一轮静默抹掉。

三条可直接抄的机制：

1. **数值小 = 优先级高**，权威源的属性受保护
2. **被拒的写入要明示**（对应 IRE 的 `maskedAttributes`），log warn 而不是静默丢弃或静默累积
3. **null 单独控制**（对应 `glide.reconciliation.override.null`），防止某个源用空值覆盖好数据

同时补齐 `observed_by` / `confidence` / `scope` 三个从不写入的溯源属性。**特别是 `scope`**——「指标 scope ≠ 拓扑 scope」这类问题业界公开文档里没有任何原语可解，把它显式标注出来是可以拿出去讲的差异化，但**目前只是提案，代码里零写入**，得先落地。

---

### P2-1 — 每类边声明自己的 TTL

现状是四套语义：DeepFlow Calls 1800s（`neptune_etl_deepflow.py:1246`）、X-Ray 6h（`neptune_etl_xray.py:100`）、AWS 无阈值硬删（`graph_gc.py:39-45`）、CFN 无。

抄 New Relic 的 `expires`（ISO-8601，默认 PT75M，允许 10 分钟到 72 小时）：**TTL 是边类型的声明属性，不是全局判据**。「Pod 属于哪个 Node」和「服务 A 调用服务 B」的合理过期时间差两个数量级，用一个全局判据必然一头过激一头迟钝。

另外抄 Dynatrace 的 `lifetime.start/end` 双字段 + 固定保留期（它是 35 天）作为硬删边界。

---

### P2-2 — 三条守门测试

把三类反复出现的缺陷变成回归报警：

1. **写时类型拒绝**：断言向 `upsert_vertex`/`upsert_edge` 传入未声明的 label 会抛异常。现在全部是事后子集断言（`test_24_live_schema_consistency.py:105`），没有写时强制的测试。
2. **溯源存在性**：断言每条边都带 `source` + 统一时间戳字段。现在 `source` 只在个别写入点被断言（`test_12:173/478`、`test_31:203`）。
3. **跨类型身份键不可变**：把 `test_34_ec2_identity_and_nfm.py:22/48` 参数化到全部节点类型，并把重复检测从告警改为失败。

另外两条值得补：**「stale 边被置 `active=false`」的直接行为断言**（现在 `test_c01` 只验过滤条件含 `active=true`）；**多源写同一条边的优先级断言**（现在只有 X-Ray vs 已存在边这一对）。

已有的好基础不要动：`tests/test_31_property_cardinality.py:40/100/168/203`（属性单基数，针对 `Microservice.az` 多值）、`test_33_xray_parallel_source.py:355`（溯源保护）。

---

### P2-3 — 给无覆盖的高风险路径补测试

`etl_deepflow` 的整条 datastore-flow 摄取链路无任何测试：`_resolve_datastore_ips`（~CC 40）、`fetch_datastore_flows`、`upsert_datastore_flows`、`_get_eks_k8s_session`、`ch_query_json`、`get_aws_session`、`_first_scalar`。

而这条链路正是 2026-08-29 为消除「微服务→存储」盲区新加的（盲区 13→10）。**最新、最复杂、CC 最高的代码路径覆盖率为零。**

`etl_xray` 侧 `deactivate_stale_xray_edges` 也无测试——**软删除逻辑本身没有测试**。

---

### P3-1 — Chaos 逐边证伪（差异化能力）

这是本项目**唯一无法被商业产品复制**的能力。Datadog / Dynatrace / New Relic 都没有故障注入后端，它们做不了这件事。

文献支撑充分：

- **Gremlin**（Heorhiadi et al., ICDCS 2016）——系统化故障注入验证微服务的学术源头
- **Krasnovsky & Zorkin, arXiv:2506.11176（2025-06）**——从 Jaeger trace 抽依赖图 → Monte-Carlo 故障模拟 → 真实 chaos 实验对照，replicated 场景 0.305 vs 0.305（MAE ≤ 0.0004）
- **Mystery Machine**（OSDI 2014）——「假设所有边存在，用海量样本逐条反驳」的方法论原型

**关键**：那篇 2025 论文亲口承认的局限正是反用它的动机——**「我们假设已知所有依赖边；漏掉一条边会导致模型高估韧性。」**

**做法**：不是用图预测韧性，而是**用 chaos 实测证伪图里的每条边**。对边 A→B，从已有 60 种故障里选「A 故障 / 延迟」注入：

- B 的成功率与延迟**无显著变化** ⇒ 边可疑（假边，或存在但非 load-bearing）
- **显著变化** ⇒ 边被确证，且顺带得到影响强度，可写回边权重

配套引入 **Uber CRISP** 的关键路径视角作为重要性维度：一条存在但从不落在关键路径上的边，对延迟无影响，验证优先级可以降低。

**注意 Mystery Machine 的固有局限同样适用**：覆盖率决定召回——一条边若在样本里从不出现，既不会被断言也不会被反驳。所以 chaos 证伪能提高精确率，但提不了召回率。

---

### P3-2 — 把连接池假阴性做成可引用的数字（业界空白）

Research 明确结论：**连接池 / 长连接导致依赖发现系统性假阴性，机制清楚，但公开文献没有任何定量。** eBPF 依赖发现论文（arXiv:2608.04413、2510.15490）只定性承认覆盖率问题。

而本项目已经握着两组实测证据：

1. **Route 53 Resolver 直接测量**：5 分钟 93 条 DNS 查询，Aurora / DynamoDB / SQS / ElastiCache 端点各 **0 条**。最锋利的是同类资源对照——`grafana-aurora-mysql` 有 360 次/24h（grafana 会重连），`serviceseks2-databaseb269d8bb` **零次**（pod 持连接池）。**可见性纯由连接行为决定，与资源类型无关。**
2. **连接复用率量化对照**（2026-08-30 实测）：petsite → search 是 2.49–3.29 请求/连接（连接存活 p50 3010ms、p95 64s）；list-adoptions → search 是 1.53–1.61（p50 仅 22–27ms）。**同一个服务端，两个客户端差一个数量级。**

**补一个对照实验即可成文**：在 DeathStarBench（Thrift RPC + 连接池，真值由构造已知）上分别以「强制每请求新建连」与「开启连接池」两种配置跑 eBPF 依赖发现，diff 出的漏边即为连接池假阴性率。

---

## 二、对外答辩要点

### 「图里的依赖关系怎么校验？」

分四层答，不要只答一层：

1. **写入时** — schema 门禁（P0-1 落地后）：类型闭集、身份键必须来自不变量、必需溯源属性齐全，不满足即拒绝
2. **写入后** — 生命周期收敛（P1-1）：`update_tag` 范式 + 每类边自己的 TTL（P2-1）
3. **跨源** — 双源交叉验证：`source` + `dependency_kind` 让声明边与观测边并存而非互相覆盖，`drift_status` 三态（`ok` / `declared_not_observed` / `observed_not_declared`）产出死依赖与影子依赖
4. **主动** — chaos 逐边证伪（P3-1），这一层商业产品做不到

可以补一句业界现状为背景：**只有 New Relic 与 Dynatrace 把边建成带显式生命周期的持久对象；Datadog / OTel servicegraph / Elastic 都没有持久边实体，边的「存在」等于查询时间窗内有观测。**

### 「ETL 是不是太复杂？」

先纠正数字：目录里 90% 是打包的 `requests`/`urllib3`。真实规模是五个写入 ETL 共 **18 文件 / 7,159 行 / 128 函数**。

然后区分两类：

- **本质复杂可辩护**：五类异构源融进一张图，「声明 vs 观测」对账天然需要消歧属性；`AccessesData` 被 4 源写是问题域决定的。Cartography 的设计原则原文就是「每个 intel module 提供自己对图的视角」，**鼓励**多模块写同一节点类型。
- **偶然复杂承认并给出计划**：1,234 行未拆编排、2 份客户端副本 + 1 份分叉、4 套手写 upsert、权威 profile 不被读取。前三条是同一根因——缺一层由 schema 生成的公共载入/清理层。

### 「怎么维护 ETL 代码？」

核心答案：**把声明从代码里抽出来，让它有否决权。**

- New Relic 的 `entity-definitions` 是独立仓库的 YAML + PR + 自动校验 + owner 双评审；身份怎么拼、关系类型闭集、TTL、冲突约束全部是配置
- ServiceNow 的 identification / reconciliation rules 都是**数据表**（`cmdb_identifier`、`cmdb_identifier_entry`），带 order 优先级、支持类继承
- 本项目已有 `profiles/petsite.yaml`，方向正确，缺的是让它在运行时被读取并具备否决权（P0-1）

### 「粒度错配你怎么处理？」

这题全行业都没银弹，可以坦然说明：粒度由「插桩报了哪些属性 + 一条显式优先级链」决定，缺细粒度属性就塌缩。**Datadog 自己的 S3 依赖粒度也取决于插桩是否报了 bucket 名**（`peer.db.name` > `peer.aws.s3.bucket` > `peer.hostname` > `peer.db.system`），所以 X-Ray 泛化 S3 是行业通病。

两个可借的正面应对：Elastic 把粒度拆成 `service.target.type` + `service.target.name` 两维；AppDynamics 把「一个 backend 必须只由一个下游 tier 处理」写成硬建模约束并提供 aggregate/split 人工再标注。

**而「指标 scope ≠ 拓扑 scope」（VPC 级聚合归属单实例）业界公开文档里没有任何原语可解**——所以 `scope` 显式标注是可以拿出去讲的，但要诚实说明目前是提案、代码零写入（P1-2）。

### 「你怎么知道你没漏边？」

最狠的问题，业界也答不好，正好说明这是领域难题。arXiv:2608.04413 那篇 eBPF 依赖发现论文自己写着：只在自建 20 服务 testbed 上还原了已知拓扑，**这只证明流水线能还原已知图，不证明能发现未知依赖**，并坦承「对偏斜流量与罕见代码路径没有覆盖率证据」。

本项目能给出的答案比它强：chaos 逐边证伪（提高精确率）+ 已实测的连接池假阴性量化（诚实界定召回率边界）。

### 「用公开数据集验证吧」——这是个陷阱

若有人建议用 Alibaba microservice trace 当 ground truth：ICPE'24 的 Casper 论文量化出 **2021 数据集严格按官方规范只能重建 58.32% 的 trace**（Casper 绕过不一致后 83.82%；2022 数据集 86.42% → 98.6%），而且**严格遵守官方规范的工具会静默忽略不一致，生成大小和形状都错的 trace**。

要做验证闭环应该用 **DeathStarBench / TrainTicket**——拓扑自建、真值由构造已知、可注入故障。

---

## 三、一句话战略结论

**这个系统的差异化不在于它有一张依赖图——商业产品都有。差异化在于它是唯一能证伪自己那张图的系统**（Chaos Mesh + FIS + 60 种故障 + Neptune 里带溯源的边）。

而要让这个差异化站得住，前提是把契约层补上：**schema 有否决权、身份键来自不变量、cleanup 由 schema 生成、属性权威可解释**。否则证伪的是一张自己都不确定形状的图。
