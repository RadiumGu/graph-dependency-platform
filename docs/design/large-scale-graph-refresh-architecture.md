# 大规模依赖图的更新与刷新架构

本文回答一个具体问题：**图长到几十万节点、上百万条边之后，某个节点或边发生变化，依赖关系怎么刷新。**

结论先行：

1. **这个规模不是"大图"。** 百万边整张图装得进单机内存，全图 Tarjan 求割点是毫秒级。驱动架构变化的不是"算不下"，而是写入吞吐、失活对账、以及派生结论的失效传播。
2. **先把边分成「可对账」与「需推断」两类（§4.0）。** 实测本项目 64% 的边（5,956 条基础设施拓扑边）真值可从控制面 API 随时枚举，它们只需集合对账，**不需要 TTL／置信度／双时态**；真正需要那套机制的依赖语义边只有一百多条。这个划分把难题的作用域缩小两个数量级，是本文最有操作价值的一条。
3. **原始边的刷新走"变更驱动 + 时间窗聚合"**，不是全量轮询。⚠️ 这是**为增长准备**的方向，不是当前的救火 —— 实测当前最慢的写入方只用掉 Lambda 上限的 9%（§1.2）。
4. **派生结论按能否增量维护分成三类，各用各的策略。** 其中拓扑排序与割点**没有任何通用增量引擎能维护**，只能全量重算 —— 好在这个规模全量就是秒级。
5. **不引入 IVM 引擎，不自研全动态图算法。** 前者对本场景四类结论只覆盖两类且删除路径最弱；后者学术上漂亮、工程上无可用实现。
6. **Neptune 不支持水平分片**，写吞吐无法横向扩 —— 这是本架构最硬的边界，必须在设计里显式承认。
7. **当前没有性能瓶颈，优化应推迟并配告警（§8.0）。** 实测三个写入方分别用掉 Lambda 上限的 0.4%／9%／0.4%。真正紧迫的是图谱**当前**陈述的正确性：已验证的 44 条边里 inconclusive（16）几乎与 confirmed（17）持平。

⚠️ **本文经过一次大幅实测更正。** 第一版第 1 节标题写着"（实测）"但内容是推算，数字错了一到两个数量级；第一版的「第一阶段四项改动」里三项前提不成立。完整更正记录见 §9 —— 引用本文任何数字前请先确认它属于 §9.2 之后的版本。

证据分级贯穿全文：**〔实测〕**= 本项目实测或论文实验数据；**〔官方〕**= AWS/厂商官方文档；**〔理论〕**= 论文的分析结论，无实验数字；**〔查不到〕**= 公开信息缺失，明确标注而不猜。

---

## 0. 先纠正一个前提：这个规模在图领域不算大

把目标规模放进图处理领域的坐标系：

| 图 | 顶点 | 边 |
|---|---|---|
| 本项目当前(实测) | **2,182**(其中 `Pod` 1,545 = **71%**) | **9,293** |
| **本文目标** | **~10⁵** | **~10⁶** |
| Twitter（图分区论文常用） | 41 M | 1.4 B |
| UK-2007 web 图 | 105 M | 3.3 B |

〔实测〕百万条边序列化后是几百 MB 量级。**整张图能装进单机内存**，METIS 这类离线分区算法能轻松跑完 —— 而 METIS 跑不动 Twitter/Web 图，那才是流式分区算法被发明出来的理由（[CUTTANA, arXiv 2312.08356](https://arxiv.org/html/2312.08356v1)）。

这条判断决定了后面所有取舍。**如果照着"分布式大图"的套路设计，会为不存在的问题付出复杂度，并引入真实的正确性风险**（见 §6 那条铁律）。

⚠️ 本文按"稳定在百万边量级"设计。若预期增长到十亿边以上，§6 的分区部分需要换成完整的分布式方案，其余章节仍适用。

---

## 1. 当前实现的实测基线

⚠️ **本节数字全部来自 2026-09-26 实测**(Neptune openCypher、CloudWatch 指标、Neptune `explain`)。本文第一版这一节是**按推算**写的,数字错了一到两个数量级,现已按实测重写 —— 更正记录见 §9。

### 1.1 图的真实构成:9,293 条边,两类语义混在一起

总计 **9,293 条边**、**31 种 edge label**。按量排序:

| 类别 | label | 边数 | 有 `active` | 有 `verify_status` |
|---|---|---|---|---|
| 基础设施拓扑 | `RunsOn` | 1,921 | 0 | 0 |
| | `LocatedIn` | 1,167 | 0 | 0 |
| | `BelongsTo` | 964 | 0 | 0 |
| | `Manages` | 958 | 0 | 0 |
| | `Routes` | 946 | 0 | 0 |
| **小计** | | **5,956(64%)** | **0** | **0** |
| 依赖语义 | `AccessesData` | 68 | 46 | 20 |
| | `Calls` | 12 | 12 | 10 |
| | `DependsOn` | 9 | 3 | 8 |
| | `InvokesTool` | 8 | 8 | 0 |
| | 其余 23 种 | 各 ≤ 19 | 部分 | 部分 |

三个关键事实:

- **占量 64% 的基础设施拓扑边身上 `active` 全是 0** —— 它们不参与失活判定,只记 `last_seen`。
- **真正承载依赖语义的边只有一百多条**,其中被验证过的仅 **44 条**。
- 最大的节点类别是 `Pod`(**1,544** 个),远超其他所有类型。

这个构成直接决定 §4 与 §5 的设计取向,见 §4.0 的两类边划分。

### 1.2 写入耗时:当前不是瓶颈

CloudWatch 近 24 小时:

| Lambda | 平均 | 最大 | 调用数 | 并发峰值 |
|---|---|---|---|---|
| `neptune-etl-from-deepflow` | 6.7 s | 10.1 s | 288 | 1 |
| `neptune-etl-from-aws` | **79.9 s** | 85.1 s | 117 | **3** |
| `neptune-etl-from-cfn` | 3.8 s | 3.8 s | 1 | 1 |

最慢的写入方用掉 Lambda 15 分钟上限的 **9%**,约 10 倍余量。

⚠️ 本文第一版说「逐条写边,百万条约 33 分钟超过 Lambda 上限」——**那是推算,而实测那个逐条写边的 `etl_deepflow` 只用 6.7 秒**。它写的是 `Calls` 等少量边,不是量的主体。占量 64% 的基础设施边由 `etl_aws` 写,而 `etl_aws` 是唯一模块化的 ETL(7 个文件),其 `upsert_edge(src_id, dst_id, label, props)` 已经用顶点 id 而非 name。

### 1.3 并发:限制加错了对象,而并发已在生产安全运行

`infra/lib/neptune-etl-stack.ts:376` 设 `reservedConcurrentExecutions: 1`,注释写「防止并发写入 Neptune」。实测这个意图**从未实现**:

- `etl_trigger` **根本不写 Neptune**(`neptune_query`/`gremlin` 在 `neptune_etl_trigger.py` 里出现 **0** 次),它只异步 invoke 下游。
- 真正写 Neptune 的三个 Lambda **都没有并发限制**。
- `neptune-etl-from-aws` 实测**并发峰值 3**,每天如此,**无事故记录**。

git 追溯:该设置来自 `da3a04c2`(2026-03-10)「事件驱动 ETL 触发机制」的**初次实现提交**,与 SQS 队列、`memorySize` 一起写下,**不是为修某个事故补的**。仓库里唯一标题含「并发写入者」的记录(`todo/deploy-result_20260830-1530.md`)讲的是 EKS kubectl patch 冲突,与 Neptune 无关。

**并发写入在本项目是已被生产验证安全的**,原因是写入全部走 upsert + `coalesce` 保护,天然幂等。

### 1.4 失活对账:边级只扫 12 条,另有一套顶点级 GC

`neptune_etl_deepflow.py:1575` 的 `g.E().hasLabel('Calls')` 是唯一的**边级**失活对账,而 `Calls` 只有 **12 条**。

`etl_aws` 侧另有 `graph_gc.py`(150 行),其 `_gc_vertices` 是**顶点级** GC(`g.V().hasLabel(...)`)。两侧是两套不同机制:一边翻边的 `active`,一边删顶点。

### 1.5 存储与索引统计

`petsite-neptune` / `db.r6g.large`(2 vCPU / 16 GiB),引擎 1.4.6.3,**单实例无读副本**。buffer pool 约 10 GiB —— 当前数据量远未构成压力。

**DFE 统计已启用且新鲜**:`autoCompute: True`、`active: True`,实测刷新时间距查询仅 10 分钟。`explain` 证实估计值精确:

```
Calls  label  →  rangeCountEstimate=12     (实际 12)
RunsOn label  →  rangeCountEstimate=1964   (实际 1964)
last_seen 谓词 →  rangeCountEstimate=11232
```

所以 §8 第一版列的「启用并定期刷新 DFE statistics」**本来就是现状,无需改动**。

`explain` 同时证实了 §2.4 那个结构判断 —— 两个**独立** pattern 做 join,没有复合索引:

```
DFEPatternNode(EL(...el://Calls...)   project DISTINCT[?1], {rangeCountEstimate=12})
DFEPatternNode(EP(?1, ep://last_seen, ?9) objectFilters=(< ...), {rangeCountEstimate=11232})
```

`Calls` 的 12 对 `last_seen` 的 11232,选择度差 936 倍,planner 正确地先扫 label 侧。`RunsOn` 的 1964 对 11232 只差 5.7 倍,优势小得多 —— **这正是统计必须新鲜的原因**,而它本来就是新鲜的。

附带发现:`last_seen` 估计 **11232 > 总边数 9293**,说明节点上也有 `last_seen`,该谓词扫描跨越点和边。

### 1.6 所以当前没有性能瓶颈

四项曾被列为「第一阶段改动」的事,实测后三项前提不成立(详见 §9)。**性能优化应推迟到实测触发条件成立**,触发条件与告警见 §8.0。

---

## 2. Neptune 的硬约束（决定架构边界）

全部针对 **Neptune Database**（事务型集群）。Neptune Analytics 是独立的内存图分析引擎，能力与语义不同，见 §5.3。

### 2.1 不支持分片 —— 最硬的一条

〔官方〕Neptune **不支持水平分片**："Neptune is single-sharded by design"（[Streams 文档](https://docs.aws.amazon.com/neptune/latest/userguide/streams.html)）。含义：

- **读**能扩：最多 **15 个只读副本**，跨区域可用 Global Database。
- **写不能扩**：单 writer 实例是写吞吐的天花板，只能靠换更大规格纵向扩（r6g 线性扩展到 16xlarge，[instance-types](https://docs.aws.amazon.com/neptune/latest/userguide/instance-types.html)）。
- 单集群存储上限 **128 TiB**（中国区/GovCloud 64 TiB），节点/边数量本身**无上限**（[limits](https://docs.aws.amazon.com/neptune/latest/userguide/limits.html)）。

〔社区〕真正超出单 writer 写吞吐时，没有原生方案，通行做法是**应用层分区**（按业务键切到多个集群，跨集群查询在应用层 fan-out 合并）。这是实打实的架构负担，设计里要显式承认 Neptune 不解决超单机写扩展。

### 2.2 写入：批大小与请求上限

| 项 | 数值 | 来源 |
|---|---|---|
| 推荐批大小（插入） | 每批 **50–100** 个 vertex/edge，属性多则取 50 | [batch add](https://docs.aws.amazon.com/neptune/latest/userguide/best-practices-gremlin-java-batch-add.html) |
| 推荐批大小（upsert） | `mergeV`/`mergeE` 每批约 **200 records**；1 record = 一个 label 或一个属性，所以"4 属性带 label 的点"= 5 records | [efficient upserts](https://docs.aws.amazon.com/neptune/latest/userguide/gremlin-efficient-upserts.html) |
| 单 HTTP 请求上限 | **150 MB**，超出返回 400。**WebSocket 连接不受此限** | [limits](https://docs.aws.amazon.com/neptune/latest/userguide/limits.html) |
| 查询执行线程 | 每 vCPU **2 个** → `db.r6g.large` 只有 **4 个** | [instance-types](https://docs.aws.amazon.com/neptune/latest/userguide/instance-types.html) |

〔查不到〕**AWS 未公布"单 writer 每秒写多少边"的数字。** 这取决于数据形状，必须用真实的边/属性形状实测得出。本文不引用任何未标来源的 ops/s。

### 2.3 Bulk Loader 不是 upsert

〔官方〕Loader **本质是 insert**。默认把"更新已存在的值"当错误；只有设 `updateSingleCardinalityProperties=TRUE` 才允许更新，**且仅限单基数顶点属性**（[bulk-load-optimize](https://docs.aws.amazon.com/neptune/latest/userguide/bulk-load-optimize.html)）。Loader API 是 **non-ACID**。

这条直接否掉一个常见设想："用 Bulk Loader 做周期性全量刷新"。本项目的写入语义是 upsert + write-once 属性保护（`source`/`first_seen` 记录"谁首先发现了这条依赖"），Loader 给不了这个语义。**Loader 只适合初始装载与灾难重建，不适合日常对账。**

### 2.4 索引：没有 (label, 属性) 复合索引

〔官方〕Neptune 固定建 **3 个** quad 索引（SPOG / POGS / GPSO），**无用户自定义二级索引**。LPG 里边 label 落在 P 位（[storage-indexing](https://docs.aws.amazon.com/en_us/neptune/latest/userguide/feature-overview-storage-indexing.html)）。

对本项目的热路径 `hasLabel('Calls') + last_seen < N`：

- 不是全库扫，但会退化成「扫该 label 全部边 → 逐个过滤 `last_seen`」或「扫 `last_seen` 范围 → 过滤 label」，**二者取决于 DFE 统计的选择度估计**。
- **没有复合索引可用**，引擎无法一次命中。
- **必须启用并定期刷新 DFE statistics**，否则 planner 选错扫描方向（[DFE statistics](https://docs.aws.amazon.com/neptune/latest/userguide/neptune-dfe-statistics.html)）。用 `explain` 验证实际计划。

⚠️ 默认**不建** OSGP 反向索引，所以无标签的 `in()`/`both()`/`drop()` 可能很慢。开 OSGP 的代价：**写入慢至多 23%、存储 +20%**，且只能在 lab mode + 空集群下开。

### 2.5 Neptune Streams 能当变更源，但有三个约束

〔官方〕Streams 是 GA 能力，**严格顺序、无重复、不丢**，change log 与事务同步写入（[streams](https://docs.aws.amazon.com/neptune/latest/userguide/streams.html)）。

| 项 | 数值 |
|---|---|
| 保留期 | 默认 **7 天**，可调 **1–90 天**（`neptune_streams_expiry_days`） |
| 读取 | HTTP GET `/propertygraph/stream`，`limit` 1–100,000（默认 10），**硬性 10 MB 响应上限** |
| 分片 | 不分片 |

三个必须写进设计的约束：

1. **无原生 Lambda 触发。** 官方明确 log stream 不产生可触发事件，必须自建轮询消费者 + checkpoint。
2. **拉取共享 DB 资源。** `GetRecords` 与业务查询抢同一实例的执行线程 —— 消费者应跑在**只读副本**上。
3. **同步写入有代价。** 官方明说启用 Streams 会让"写性能轻微下降"。

另外 Gremlin 一个操作可能产生**多条** change record（多 label/多属性各一条，UPDATE 拆成删+插），消费者要按 `commitNum` + `opNum` 重组事务边界。

### 2.6 内存：工作集要装进 buffer pool

〔官方〕buffer pool 约占 **2/3 系统内存**，其余 1/3 给查询线程与 OS。判据明确：**`BufferCacheHitRatio` 持续低于 99.9% 就该升实例**（[metrics 最佳实践](https://docs.aws.amazon.com/neptune/latest/userguide/best-practices-general-metrics.html)）。

〔查不到〕AWS 未提供"每节点/每边/每属性 X 字节"的公式。可从模型推：数据按 quad 存且默认建 3 份索引，所以底层存储 ≈ 逻辑数据 × 3 + 字典开销。实操是先按此估存储，再用 `BufferCacheHitRatio` 反推需要多大实例把热点装下。

---

## 3. 原始边的刷新：变更驱动 + 时间窗聚合

### 3.1 外部先例：Netflix Service Topology

〔官方，经 InfoQ 转述〕Netflix 的做法与本项目的取向高度一致，值得逐条对照（[InfoQ 报道一](https://www.infoq.com/news/2026/06/netflix-microservices-realtime/)、[报道二](https://www.infoq.com/news/2026/08/netflix-service-topology/)）：

- **三源合一，各存独立图分区**：eBPF 网络流（内核级全覆盖，但缺应用层语义）、IPC 指标（有端点/协议，但只覆盖主动上报的服务）、聚合 trace（有真实条件分支，但受采样限制）。**每层补另一层的盲区**，可单层查也可三层合并。
- **5 分钟窗口批处理**，三阶段管线：消费多区域 Kafka → intermediary resolution（把经过 LB/NAT/网关的多跳网络路径**折叠成直接的应用间边**）→ 元数据富化后写图。
- **增量流式 + 时间窗聚合，不是全量重建。**
- **背压优先于丢数据**：背压一路传回 Kafka 消费者暂停，"宁可延迟新鲜度，也不丢数据、不给不完整的图"。
- 保存**时间窗聚合器快照 + 属性级变更历史**，不留完整图快照、也不回放事件日志，即可重建任意时刻拓扑。

两条官方教训直接适用：

> **"不完整或不正确的依赖数据，比没有数据更糟，因为它在故障中把工程师引向错误结论。"**

这正是本项目"错误陈述比缺失陈述更危险"的同一句话，来自一个万级服务规模的生产系统。

> "静态或延迟的依赖图在每天多次部署的环境里被证明无用。"

还有一条热点教训：早期设计把 popular destination 的 intermediary resolution 集中处理，导致**部分实例承受 100 倍常规流量**同时做重 I/O 富化，逼出了三阶段拆分。本项目若做类似的路径折叠，要注意同样的热点。

### 3.2 本项目的目标架构

```
数据源（eBPF flow / API / span / 模板）
  │
  │ ① 变更驱动为主路径：资源变更事件 → SQS → 处理
  │    全量对账降为每日/每周，只用来兜住漏采
  ▼
② 时间窗聚合（5 分钟量纲，参照 Netflix）
  │  同窗内同一条边的多次观测合并成一次写入
  ▼
③ 批量 upsert（mergeE 每批 ~200 records）
  │  write-once 属性仍用 coalesce(values, constant) 幂等写法
  ▼
Neptune（写主库）
  │
  ├─ Neptune Streams ──> 派生结论失效（§5）
  └─ 只读副本 ────────> 查询 + Streams 消费者
```

关键改动有四项，按代价从低到高：

1. **边写入批量化。** 节点已经这么做了（`BATCH_SIZE=20` 链式 `mergeV`），边还是逐条。改成 `mergeE` 批量提交，批大小按 **200 records** 折算（注意 1 record = 一个 label 或一个属性，不是一条边）。这是纯代码改动，应该先做。
2. **把已有的事件驱动链路提升为主路径。** 本项目**已经有**这条链路：ALB/EC2/EKS/RDS/ElastiCache 变更 → SQS → 触发 `etl_aws`。现在它是定时全量的补充；大规模下要反过来 —— 变更驱动为主，全量对账降到每日，只兜漏采。
3. **失活对账的分片键需要先造出来。** Neptune 没有原生 TTL,也没有 (label, 属性) 复合索引,所以"扫 label 再过滤 `last_seen`"是结构性的。
   ⚠️ 本文第一版建议"按 `scope` 分片" —— **实测 `scope` 在边上一条都没有**(31 种 label、9,293 条边,`with_scope` 全为 0),它是**节点**属性(`Pod` 1,545/1,545 有值),`test_84_scope_written_at_upsert.py` 针对的也是 `node_scope.type_map`。所以这个分片键**当前不存在**。若要分片,得先决定用什么键(候选:源顶点的 `scope`、`source`、或按 `last_seen` 分桶),而这在边级失活只扫 12 条边的当下没有必要。
4. **读写分离。** 加只读副本，查询与 Streams 消费者走副本，ETL 写主库。代价是接受复制延迟。

### 3.3 `reservedConcurrentExecutions=1` 怎么处理

这个设置的原意是"防止并发写入 Neptune"，但 Neptune 本身支持多线程并发写（官方推荐多线程 + 50–100 批大小）。真正需要串行的是**同一条边的写入顺序**，不是全局。

改法是**按边的 key 分片并发**：用 `(src, dst, label)` 哈希决定分片，同分片内串行、跨分片并行。这样并发度从 1 提到分片数，而同一条边的写入顺序仍然有保证。注意 `db.r6g.large` 只有 4 个查询执行线程，并发度要与实例规格匹配 —— 盲目提高并发只会排队。

---

## 4. 边的生命周期：这是"刷新"真正难的地方

### 4.0 先分两类边：可对账的 vs 需推断的

实测 9,293 条边里混着两类生命周期完全不同的边，却用同一套 `last_seen` + `active` 机制管理。**这是设计混淆，也是后面所有麻烦的根源。**

| | **可对账**（reconcilable） | **需推断**（inferred） |
|---|---|---|
| 真值来源 | 控制面 API 随时可枚举完整集合 | **无任何 API 能给出真值** |
| 本项目实例 | `RunsOn` `LocatedIn` `BelongsTo` `Manages` `Routes` `HasSG` `HasRule` | `Calls` `AccessesData` `DependsOn` `InvokesTool` `Retrieves` |
| 实测边数 | **5,956（64%）** | 一百多 |
| 实测 `active` | **全为 0** | 有 |
| 正确机制 | **集合对账** —— 控制面有的留、没有的删 | TTL + 置信度 + 双时态 |
| 需要 TTL／置信度 | **不需要** | 需要 |

**实测 `active` 在可对账那类上全为 0，说明实现其实已经默认了这个区分，只是概念上没分清** —— 而文档、契约、TTL 设计一直在按单一模型谈。

这个划分把「边的生命周期」这个难题的作用域从 9,293 条缩到一百多条，**难度下降两个数量级**。在一百多条边上实现完整的置信模型是轻松的；在 9,293 条上做既是过度工程，对那 5,956 条还是错的抽象。

#### 业界先例：ServiceNow 的 IRE

〔业界〕这个二分对应 ServiceNow CMDB 的 **IRE（Identification and Reconciliation Engine）**，它把问题拆成两个各自独立的规则集（[ServiceNow 社区文档](https://www.servicenow.com/community/cmdb-articles/cmdb-understanding-identification-and-reconciliation-rules-and/ta-p/3520826)）：

| 问题 | 由谁回答 |
|---|---|
| 「这个 CI 已存在还是新的？」 | **Identification Rules** |
| 「多个源更新同一个 CI，该信谁？」 | **Reconciliation Rules** |

三条对本项目直接适用的设计：

1. **识别歧义时拒绝处理，而不是猜。** 当多条 CI 同时匹配传入数据，ServiceNow「**will NOT guess**」—— 识别失败、不更新任何 CI、记 IRE 错误、标为 ambiguous match。官方定性：「If Identification is ambiguous, CMDB **refuses to process**. This is **by design** to protect data integrity.」
   这与本项目的 fail-safe 纪律是同一原则：零流量与健康在指标上无法区分时判 `inconclusive` 而非 `refuted`。
2. **对账作用在属性级，不是整条记录。** 按 source priority 决定每个属性由谁权威，而且「**Only ONE source wins per attribute. There is no merging logic unless custom logic is built.**」同优先级会产生不确定结果，官方明确定性为 **bad design**，最佳实践是「clearly define **single system of record per attribute**」。
   本项目的 `source` write-once 相当于「首个发现者赢」，这是一种对账策略，但**不是按属性区分权威源的那种**。合理的细化方向：度量属性（`calls` / `error_rate`）由 deepflow／nfm 权威，声明属性（`dependency_kind`）由 cfn 权威，而 `source` / `first_seen` 保持 write-once 记录发现史。
3. **CMDB 最典型的失败模式是「采集跑了但对账没跑」。** 〔业界〕「Most CMDB failures trace back to the same point: **discovery runs, but reconciliation does not.**」（[Virima](https://virima.com/blog/asset-discovery-reconciliation-automation)）
   **本项目当前正处于这个状态**：8 个源在跑 discovery，但没有一层决定「谁该赢、几个源确认了」。

另一条值得记住的定性：「A CMDB is **a graph database with an unusually hostile operating environment**: the thing it describes changes constantly, several systems describe it differently, nobody is accountable for its accuracy, and **its failure mode is silent**.」（[rezolve.ai](https://www.rezolve.ai/blog/cmdb-architecture-scalability)）—— 与 Netflix 那句「不完整数据比没有数据更糟」是同一洞察的两面。

### 4.0.1 三态观测模型：`nutrition-kb` 误判的规范化解法

本项目踩过的那次误判（`Retrieves → nutrition-kb` 因几小时无人提问被置 `active=false`，而知识库客观存在）有一个**规范化的解法**，来自 IETF 的多源印证草案（[draft-chandra-agent-registry-corroboration-00](https://www.ietf.org/archive/id/draft-chandra-agent-registry-corroboration-00.html)）。

它要求把每次观测分成**三态而非两态**：

| 状态 | 含义 |
|---|---|
| `present` | 源给出了这条记录 |
| `absent` | 源**积极断言**这个对象不存在 |
| `error` | 其他任何情况：连接失败、超时、无法解析、采集器没跑 |

核心规则（原文 MUST 级）：

> "A resolver MUST NOT raise; failures are `error` claims. An `error` claim **MUST be excluded from the diff entirely**. This rule is central: **a source that failed to answer has asserted nothing and MUST NOT be treated as claiming absence** — otherwise every transient fault becomes a false omission finding."

**`nutrition-kb` 那次误判的根因正是把 `error`（没观测到）当成了 `absent`（观测到不存在）。** 这个模型比本文早先说的「不确定时判 inconclusive」更精确 —— 它指出没观测到应当**完全不参与**判定，而不是产生一个弱结论。

配套的三条：

- **单源支持不能算「已确认」。** 「A sweep with fewer than two `present`-or-`absent` claims has nothing to corroborate; its verdict is `INSUFFICIENT`, and an implementation **MUST NOT report agreement** in that case.」→ 本项目里只有一个源看见的边应显式标记为「证据不足」，而不是 confirmed。
- **分歧本身也要确认，不只是存在要确认。** `suspected`（首次观测到分歧）vs `confirmed`（超过 staleness window 后再次观测到同一分歧）。「Consumers SHOULD treat `suspected` findings as monitoring signals and `confirmed` findings as evidence.」staleness window 由各源的传播特性（TTL、同步间隔）决定。
- **一致也必须记录。** 「A corroboration trail recording only disagreements **cannot prove its checks ran**; the agreement record is the positive attestation.」→ 对应本项目已有的纪律：必须能证明检查跑过，不能只记录失败。

### 4.0.2 ⚠️ 8 个源不等于 8 个独立源

这是同一份草案 Security Considerations 里最该被记住的一条：

> "Corroboration among k sources is worth exactly the **independence** among them. Sources sharing an upstream feed, an operator, or an incentive **corroborate each other's misinformation by construction**. This procedure records which sources agreed, enabling diversity-weighted consumption; **it cannot manufacture diversity**."

本项目的 8 个源**存在明显的共享上游**：

| 疑似同源组 | 共享的上游 |
|---|---|
| `deepflow-dns` + `deepflow-l4` | 同一套 DeepFlow eBPF 采集 |
| `xray` + `appsignals-etl` | 都源自 AWS 的 trace／span 数据 |
| `aws-etl` + `cfn-etl` | 都源自 AWS 控制面 |

**所以 `confirm_count` 必须按独立性分组计算**，不能简单数源的个数 —— 否则 `deepflow-dns` 与 `deepflow-l4` 同时确认会被当成两条独立证据，而它们实际只是一条。真正强的印证是**跨组**的，例如 eBPF 组 + trace 组同时看见。

这意味着 §8.1 第 1 项的 `confirmed_by` 设计要带上分组信息，而不只是一个源名列表。

### 4.1 问题：零流量不等于依赖不存在

本项目已经踩过一次：`Retrieves → nutrition-kb` 因为几小时没人问营养问题被置 `active=false`，而那个知识库客观存在（控制面 `list_knowledge_bases` 就返回它）。**图谱给出的是错误陈述，而不是过期陈述。**

〔实测，外部〕阿里巴巴 2021 集群 trace 的公开分析给了这件事的硬证据：同一入口的不同请求走出的调用图不同，且行为随时间显著变化（[arXiv 2504.13141](https://arxiv.org/abs/2504.13141)、[alibaba/clusterdata](https://github.com/alibaba/clusterdata/blob/master/cluster-trace-microservices-v2021/README.md)）。**单个刷新窗口内某条边零流量，完全可能只是这一窗没走到那条条件分支。**

百万边规模下,稀疏调用边的占比会显著高于现在(依赖语义边只有一百多条,其中高频边占多数),这个误判会放大。

### 4.2 公开先例：三种做法，没有第四种

| 做法 | 谁在用 | 具体 |
|---|---|---|
| **显式 TTL，按关系类型分级** | New Relic | 每条关系有 `expires` 字段，**默认 75 分钟**，可配 **10 分钟 – 72 小时**，不同关系类型可设不同值（[relationship_synthesis](https://github.com/newrelic/entity-definitions/blob/main/docs/relationships/relationship_synthesis.md)） |
| **双时态有效期 + 确认次数** | Edge Delta | 每条边记"何时开始为真、何时停止为真"，退役=**关闭有效期而非删除**；记录被多少次 discovery run 确认，按 confirmation count 排序，一次性出现的弱边排后（[Blast Radius Is a Graph Query](https://edgedelta.com/company/blog/blast-radius-is-a-graph-query)） |
| **查询侧滑动窗口** | Grafana Tempo / OTel、AWS X-Ray | 不存持久图，边就是 Prometheus counter；"消失"= 查询窗内无增量。X-Ray 按 1 分钟–6 小时聚合 |

第三种简单，但**结构上无法区分"零流量"与"依赖已删除"** —— 两者都表现为该窗无数据，只能靠拉长窗口掩盖。几十万节点规模应走前两种。

〔查不到〕**没有任何厂商公开说明"按单条边历史调用频率自适应 TTL"的成品算法。** 本文早先口头提过这个思路，这里更正：它没有公开先例，属于自研范畴，要自己承担验证成本。目前公开可复用的最佳近似是 New Relic 的按类型分级 + Edge Delta 的确认次数置信，两者组合。

### 4.3 本项目的设计

在现有 `last_seen` / `active` / `expires_seconds` 之上补三件：

**① 双时态有效期，退役不删除。**
边上存 `valid_from` / `valid_to`。失活时写 `valid_to` 而不是只翻 `active=false`，查询默认只看 `valid_to IS NULL` 的边。好处是历史可追溯 —— 故障回溯要问"上周这条依赖在不在"，而当前实现的 `active=false` 丢掉了"什么时候不在了"。

**② 确认次数，取代"最后一次看见"单一判据。**
边上存 `confirm_count`（被多少轮采集确认过）与 `observation_window_count`。一条被确认过 500 次的边突然一窗没出现，和一条只被确认过 1 次的边没出现，**置信度完全不同**。查询按置信排序，UI 显式区分。

**③ TTL 按边类型分级，不用全局单值。**
当前 `dependency_kind` 三值（`static` / `dynamic` / `inference`）已经是分级的雏形，但 TTL 是全局 `expires_seconds`。改成按 `(label, dependency_kind)` 查表：

| 边的性质 | TTL 量纲 | 依据 |
|---|---|---|
| 同步 RPC，高频（如 300 秒 25,042 次） | 30 分钟 | 高频边一窗缺失确实说明变了 |
| agent 工具调用，稀疏突发（如一天 35 次） | 24–72 小时 | 6 小时缺失什么也不说明 |
| 配置/模板声明（`static`） | 不过期 | 声明不会因为没流量而失效 |

10 分钟 – 72 小时这个区间取自 New Relic 的公开配置范围，可作起点。

**④ 失活判据保持 fail-safe。**
本项目在 chaos 侧已有一条同形的纪律：无法确定时长时不能返回 0（那会让闸门判"没超限"从而放行），要返回一个必然触发闸门的值。边失活同理 —— **不确定时应判 `inconclusive` 而不是 `active=false`**。这条不变量在当前契约里已经写明（"零流量与健康在指标上无法区分 → 一律 inconclusive，绝不判 refuted"），要让失活逻辑也遵守它。

---

## 5. 派生结论的失效：分成三类，各用各的策略

这是"依赖关系如何刷新"里真正难的部分。图上不只有采集来的边，还有从边算出来的结论。**按能否增量维护，它们分三类。**

### 5.1 第一类：聚合型 —— 可以增量，最便宜

覆盖率、计分板这类。它们是计数，边状态从 `untested` 变 `confirmed` 就 ±1。

**策略**：在图上物化计数器，消费 Streams 时增量更新。不需要任何新组件。失效范围是局部的、单调的。

### 5.2 第二类：路径/可达性型 —— 理论可增量，但代价高

可达性、连通性。这类是递归查询，理论上有成熟的增量维护框架（IVM / Differential Dataflow）。

**但实测数据不支持在本场景引入它们。** 你让我读的那篇论文（Ammar 等，**PVLDB 15(11), 2022**，[arXiv 2208.00273](https://arxiv.org/abs/2208.00273)）结论很明确：

- 差分计算维护递归查询比每次重算快 **5 个数量级以上** —— 但**内存开销高到不可用**。
- 〔实测〕给 10 GB 内存，只能同时维护 **10 个** SPSP 查询；**20 个就 OOM**（硬件 12 核 / 32 GB）。
- **删除更糟**：对带权最短路，删除比例越高，vanilla 差分计算**越慢**（累积负 multiplicity 差分）。只有带 early-dropping 优化的版本才反而变快，而那些优化**只存在于论文的研究原型里**，不是 Materialize / Feldera 的现货能力。

引擎侧的可用性也不乐观：

| 引擎 | 递归查询 | 删除 | 备注 |
|---|---|---|---|
| Materialize | ✅ `WITH MUTUALLY RECURSIVE` | ✅ | arrangement 全驻内存；Emulator 不能上生产 |
| Feldera / DBSP | ✅ 且**递归里可带非单调聚合** | ✅ `insert_delete` | Apache 2.0 可自托管；递归压进单个 circuit step，流式注解时无法 GC |
| Flink | ❌ SQL 无递归 CTE | ✅ | DataStream `iterate` 已弃用 |
| RisingWave | ❌ 不支持递归 CTE | ✅ | 官方称"流计算非必需"（[issue #15135](https://github.com/risingwavelabs/risingwave/issues/15135)） |

〔查不到〕**没有找到"用 IVM 引擎维护图的可达性/连通性/割点作为生产派生结论"的公开报道。** 最接近的一例是 VMware 的 Differential Datalog（用于 OVN 控制平面做关系增量维护），而**该项目已归档停止维护**，且只能跑在单机内存内（[vmware-archive/differential-datalog](https://github.com/vmware-archive/differential-datalog)）。

**策略**：不引入 IVM 引擎。用 §5.4 的方案。

### 5.3 第三类：全局图论性质 —— 没有通用增量方案

割点、桥、双连通分量、拓扑排序。

**拓扑排序**要求稳定的线性序，不是 fixpoint 计算；**割点**基于 DFS 的 low-point 计算，不是单调传播。**没有任何通用 IVM 引擎能原生增量维护这两类** —— 即使引入了引擎，这两类仍然要全量重算。

那专用的全动态图算法呢？理论上是有的：全动态双连通性（割点维护）能做到 amortized Õ(log²n) 更新、Õ(log n) 查询（[arXiv 2503.21733](https://arxiv.org/abs/2503.21733)），桥的维护也有对应结构（[arXiv 1707.06311](https://arxiv.org/html/1707.06311v3)）。

**但工程上不可用。** 〔实测〕SIGMOD 2025 的实验论文（[arXiv 2501.02278](https://arxiv.org/abs/2501.02278)）原话：

> *"current solutions are not ready to be deployed in systems as is, as every data structure has critical weaknesses when used in practice."*

而且它测的是**更基础的连通性**，割点/桥比"两点是否连通"严格更难，连研究实现都几乎没有。具体的反直觉数据：

- **摊还最优 ≠ 实际最快，反而常常最慢。** 摊还成本最低的 lazy local tree 实测**是最慢的**；HDT 的分层优化在电网图上**比不分层的 HKS 慢 1335×**。
- **LCT 删除比其他结构慢最多 10⁴×**；结构树的单次删除尾延迟可达 **10⁴ 秒**（YouTube 图上某次删边）。
- 主流库全都只有"增量"并查集，不支持删边分裂：Boost.Graph 的 `incremental_components` 基于 disjoint-sets（结构上没有删边操作），petgraph、Neo4j、NetworkX、igraph 同理 —— 删边后重算连通分量。
- 唯一"接近实用"的候选是 2024 年的 cluster forest（C++ 研究代码），比 HDT 省 6–20× 内存、快 1.4–6.2×，但它解的是**连通性，不是割点**。

**盈亏平衡点由"变更的局部性"而非"图规模"决定。** 增量结构本质上就是"把重算局部化到受影响的小分量"—— D-tree 的经验数据是删边后被遍历的小树**通常少于 10 个节点**。如果频繁产生大分量的合并/分裂（大直径、长路径图），增量结构会退化，定期全量重算反而更稳。

### 5.4 本项目的策略：全量重算 + 脏分量局部化

回到 §0 的规模判断：**百万边全图装进内存，一次 Tarjan 求全部割点/桥/双连通分量是毫秒级。**

所以：

| 派生结论 | 策略 | 触发 |
|---|---|---|
| 覆盖率、计分板 | 图上物化计数器，增量更新 | Streams 事件 |
| 可达性、连通分量 | union-find 处理加边（近乎常数）；删边时对**受影响分量**局部 BFS/DFS 重算 | Streams 事件 |
| **割点、桥、拓扑排序** | **全量重算**（单机 Tarjan / Kahn） | 定时 + 脏标记触发 |

〔实测〕内存估算（~50 万点 / ~200 万边）：union-find + 邻接表约 **50–100 MB**，比 HDT 的 1–3 GB 小一到两个数量级，且**没有病态尾延迟**（重算有明确上界 O(分量大小)）。吞吐方面，每秒几十到几百条变更的需求比这些结构的实测吞吐（最慢的 Python 实现也有 9.5 万条/秒）低三个数量级。

**一个现成的替代品值得评估**：Neptune Analytics 内置 WCC / SCC / PageRank / 中心性 / 社区检测，`mutate` 变体**把算法结果直接写回节点属性**，可从 Neptune DB 或 S3 加载快照（[algorithms](https://docs.aws.amazon.com/neptune-analytics/latest/userguide/algorithms.html)）。它是批引擎不是增量的，但在百万边规模上全量跑一遍是秒级。好处是几乎零新架构 —— 同一个 AWS 账号、同一份图数据，没有 CDC 胶水、没有跨库一致性问题。

### 5.5 正确性怎么保证

增量结果与全量重算结果必须一致。三层做法：

1. **差分测试。** 随机的插入/删除/查询序列，让增量结构与"每步用并查集/BFS 从头算的 ground truth"逐查询比对。这正是那些论文验证自己实现的方式（每阶段百万级查询对照暴力结果）。
   ⚠️ 照本项目已有的纪律：ground truth 要**独立计算、每次重算、且包含"必为空/不连通"的负例**，不要冻结期望值，也不要用被测组件自己派生真值。
2. **定期全量对账。** 低峰期全量重算，与增量结构 diff，漂移就以全量为准覆盖并告警。
   ⚠️ 照本项目已有的纪律：对账判据要跑在**部署后的实际结果**上，而不是源码文本上；对账后要再空跑一轮确认漂移归零。
3. **删除是 bug 高发区。** 替换边搜索、层级下推、树/非树边标记的级联效应远大于插入。有删除的场景，定期全量对账几乎是必备的。

---

## 6. 分区：什么时候需要，以及一条铁律

### 6.1 这个规模不需要为"算不下"分区

见 §0。METIS 对百万边完全可行且质量最好。**驱动分区的不是算力，而是运维诉求**：多租户隔离、爆炸半径、团队 ownership、查询就近。

### 6.2 如果要分，用业务边界而不是图算法

〔实测/生产〕大规模生产图数据库在线服务默认用简单的哈希/业务边界分区，而非图算法分区：

- **JanusGraph** 默认**随机分区**，文档直言"当前不支持显式分区"，只建议图长到**数百亿边**时才考虑显式分区启发式（[partitioning](https://docs.janusgraph.org/v0.5/advanced-topics/partitioning/)）。
- **Facebook TAO** 按 shard + 地理分布，优先可用性与单机效率，靠缓存层吸收跨分区代价，而不是靠图算法把割边降到最低。
- Facebook 的 Social Hash Partitioner 是**离线批量**优化数据布局，在线路径仍靠稳定的 hash/locality 分片（[arXiv 1707.06665](https://arxiv.org/pdf/1707.06665.pdf)）。

理由是**稳定性优先于最优性**：图算法分区均衡但**会随图变化而变**，导致数据频繁迁移、查询路由不稳定、缓存失效、难以推理"某数据在哪台机"。业务边界（AWS 账号 / region / VPC / K8s namespace / 业务域）人为但稳定，且天然对齐故障域、权限域、团队边界。

这对本项目尤其契合 —— 契约里已有的 `scope` 概念正好是分区键的雏形。同 namespace 内的服务互调远多于跨 namespace,所以跨区边比例天然低。
⚠️ 但注意 `scope` **目前只写在节点上**(实测边上覆盖率 0,见 §8 第一阶段第 3 项)。若要把它当分区键,得先决定边的 `scope` 从哪来 —— 继承源顶点、还是独立写入。

〔理论 + 实测〕两个必须记住的数字：

- **随机哈希分区的期望跨区边比例 = 1 − 1/p**（p = 分区数）→ **8 分区随机哈希约 87.5% 的边是跨区边**。这解释了为什么 naive 哈希对遍历型负载是灾难。
- 好的分区能压到多低，强烈依赖图的局部性结构：web 图（强局部性）可低至 **4.5%**；社交图（弱局部性）即便最优也高达 **39%**（[CUTTANA](https://arxiv.org/html/2312.08356v1)）。按业务域组织的依赖图应落在 web 图这一档。

另外幂律图应选**点割**（vertex-cut）而非边割：少数 hub 顶点占据绝大多数边，边割要把 hub 的海量边表劈开，被切边数随分区数急剧恶化（PowerGraph 的核心论点）。服务依赖图里"被上千服务依赖的公共组件"就是这种 hub。

### 6.3 铁律：全局性质不能在物理分区内独立计算

**分区后各分区独立算割点会算错，同时产生假阳性和假阴性。**

- 一个顶点可能在**每个分区内部看都不是割点**，但在全图是割点 —— 它连接的两个连通块恰好被切到不同分区，局部看不到它们靠这个点相连。
- 反之，一个顶点在某分区内**局部看像割点**，但全图并不断 —— 那两块通过跨区边经别的分区仍连通。

所以：

> **分区是为了存储与查询扩展。全局图论性质（割点、可达性、环检测）必须在全图或其正确的骨架抽象上计算，绝不能在物理分区内独立计算后简单合并。**

这是本文最容易被工程实现踩的一条。对割点分析来说，假阴性会**漏报真实单点风险**，假阳性会误导容量规划 —— 而这个项目的全部价值就在于精确定位单点故障。

若图真的增长到必须分布式的规模，正确做法是**边界节点汇总 / 骨架图**：识别每个分区的边界顶点，构建一张小得多的商图概括跨区连通关系，全局性质先在骨架图上算再回填分区内（局部精确 + 边界层全局精确）。增量维护则参考时变网络的分布式割点识别协议（[arXiv 2512.04409](https://arxiv.org/html/2512.04409)）—— 边增删时只从变更点传播更新受影响节点，不做全局重初始化。

---

## 7. 被否决的方案与理由

| 方案 | 否决理由 |
|---|---|
| 引入 IVM 引擎（Materialize / Feldera）维护派生结论 | 四类结论只覆盖两类；拓扑排序与割点仍需全量。递归 + 删除是这类引擎最贵最不成熟的路径（10 GB 只撑 10 个递归查询）。收益换来"新引擎 + CDC 管道 + 跨库一致性"三份运维债。无公开生产先例 |
| 自研全动态双连通性维护割点 | 理论 Õ(log²n) 但**零可用实现**；SIGMOD 2025 明说这类结构"not ready to be deployed"；摊还最优的实测最慢；删除尾延迟可达 10⁴ 秒 |
| 用 Bulk Loader 做周期性全量刷新 | Loader **本质是 insert 不是 upsert**，给不了本项目的 write-once 属性语义；non-ACID |
| 用图算法（METIS / 流式）做在线存储分区 | 分区会随图漂移 → 迁移、路由不稳、缓存失效。生产界默认业务边界分区 |
| 靠拉长查询窗口解决"零流量 vs 已删除" | 这是无持久图方案（Grafana/X-Ray）的做法，**结构上无法区分**两者，只能掩盖 |
| 按单条边历史频率自适应 TTL | 〔查不到〕无公开先例，属自研。可以做，但要自己承担验证成本，不能当成业界既有实践 |

---

## 8. 分阶段实施

### 8.0 前提:性能优化推迟,但要有告警兜住

§1 实测证明当前无性能瓶颈。**推迟优化必须配自动告警,否则「触发条件」只是一句空话** —— 等超时了才发现就晚了。这是本项目已有的纪律:判据要能自动触发,不能靠人记得去看。

| 触发条件 | 阈值(当前实测) | 含义 |
|---|---|---|
| `etl_aws` Duration | > 300 s(现 79.9 s) | 逼近 Lambda 900 s 上限的 1/3,该做批量化 |
| `BufferCacheHitRatio` | < 99.9% | AWS 官方判据,工作集装不进 buffer pool,该升实例 |
| 边总数 | > 50,000(现 9,293) | 失活对账与索引扫描开始有实际代价 |
| `Pod` 节点占比 | 持续 > 80%(现 71%) | 低价值节点挤占图,该重审建模粒度 |

### 8.1 第一阶段:先修「正在错」,不是「以后会错」

实测后的优先级与本文第一版完全不同 —— 第一版四项里三项前提不成立(§9)。真正紧迫的是图谱**当前**陈述的正确性。

1. **补多源交叉印证。** 本项目有 8 个独立采集源(`aws-etl` / `deepflow-dns` / `deepflow-l4` / `xray` / `appsignals-etl` / `nfm` / `cfn-etl` / `manual-fix`),但 `source` 是刻意的 write-once,**只记首个发现者** —— 这个 provenance 语义是对的,`upsert_edge` 为它修过两次 bug,不该动。代价是图里看不出「有几个源确认了这条边」。
   做法是新增 `confirmed_by`(所有确认源的集合)与 `confirm_count`,不碰 `source`。**这是零语义风险的纯增量改动,而且不需要等时间累积 —— 8 个源已经在跑,立刻就能产出置信度**,并给未验证的边排出验证优先级:单源支持的先验,多源印证的可降级。
2. **查清 inconclusive 率。** 已验证的 44 条边里 confirmed 17、**inconclusive 16**,几乎一样多。`Calls` 是 4 confirmed vs 6 inconclusive,`DependsOn` 是 2 vs 6 —— 两者都是「没结论」多于「有结论」。**在修好产出率之前扩大验证覆盖是白费力气。**
   ⚠️ 但 inconclusive 也可能是**正确的** fail-safe 输出 —— 零流量与健康在指标上确实无法区分,此时判 inconclusive 而非 refuted 正是契约要求的行为。所以这一步的产出可能是「无需改动,当前行为正确」。
3. **处理已知的错误陈述。** 5 条 `modeling_artifact`(建模产物冒充真实依赖,全部落在 `AccessesData` 上)、1 条 `source='manual-fix'` 且从未验证的边。

### 8.2 第二阶段:建模决策

4. **定 `Pod` 的建模归属。** 1,545 个 Pod 占节点总数 **71%**,而 Pod 每次部署全部换名 —— 抖动最高、价值密度最低,`graph_gc.py` 的存在就是为清理它们。
   两条路:(a) Pod 不进图,依赖分析用 Service/Deployment 粒度,Pod 信息作节点属性或按需查 K8s API;(b) 保留 Pod,但显式接受 GC 成本与规模曲线。
   **这个决定必须在第 5 项之前做**,因为它会改掉两类边划分里最大的一类(`RunsOn` 1,921 条正是 Pod→Node)。
5. **落地「可对账 vs 需推断」的两类边划分**(见 §4.0),逐 label 归类并配各自的刷新机制。

### 8.3 第三阶段:刷新机制

6. 把已有的事件驱动链路提升为主路径,全量对账降到每日
7. 引入 5 分钟量纲的时间窗聚合,同窗内同一条边合并写入
8. 加只读副本,查询与后续的 Streams 消费者走副本
9. 边上补 `valid_from` / `valid_to`,失活改为关闭有效期而非翻 `active`
10. TTL 按 `(label, dependency_kind)` 分级查表,替换全局 `expires_seconds` —— **仅适用于「需推断」那一类**

### 8.4 第四阶段:派生结论

11. 启用 Neptune Streams,建轮询消费者(跑在只读副本上,按 `commitNum`+`opNum` 重组事务边界)
12. 聚合型结论改增量物化
13. 评估 Neptune Analytics 承接割点/连通分量的全量重算
14. 建差分测试 + 定期全量对账

### 8.5 只在 §8.0 触发条件成立后才做

15. 边写入批量化(`mergeE` + 批量解析顶点 id)—— **改 `etl_aws` 而不是 `etl_deepflow`**。前者占写入量 64% 且 `upsert_edge` 已用顶点 id;后者实测只占 6.7 秒。注意 `upsert_edge` 已因 provenance 语义修过两次 bug,是高风险函数。
16. 决定 `reservedConcurrentExecutions=1` 的去留 —— 它锁的是不写 Neptune 的 trigger,实际并发已达 3 且安全(§1.3)。改注释或删设置前,要先确认它对 SQS 消息堆积有无别的价值。

〔查不到〕「单 writer 每秒能写多少边」AWS 官方没有,必须用真实数据形状压测。它决定第三阶段之后还需不需要应用层分区。

---

## 9. 本文自我更正的记录

两批更正：第一批来自调研（推翻调研前的口头结论），第二批来自 2026-09-26 的实测（推翻本文第一版基于推算写下的内容）。记在这里免得被继续引用。

### 9.1 第一批：调研推翻的判断

| 我说过 | 实际 |
|---|---|
| "`g.E().hasLabel('Calls')` 是全边扫描" | 不是全库扫。label 在 P 位，走 POGS 前缀 range scan，扫的是该 label 下全部边。真正的问题是**没有 (label, 属性) 复合索引** |
| "几十万节点上全图割点分析不可行，O(V·(V+E))" | **错**。Tarjan 是 O(V+E)，百万边单机毫秒级。几十万节点在图领域是小图，整图装得进内存。反而"分区后各自算割点"才是错的做法 |
| "割点增量维护有算法但复杂" / "是 polylog 的，不是不可行" | 理论对（Õ(log²n)），**工程上不可用**：零可用实现，摊还最优的实测最慢，删除尾延迟可达 10⁴ 秒。主流库全都只支持加边不支持删边分裂 |
| "按边的历史调用频率自适应 TTL" 是可行解法 | **没有任何厂商公开这样的成品算法**。可行的自研方向，但不能当业界既有实践引用 |

### 9.2 第二批：实测推翻本文第一版的内容

⚠️ 这一批更值得记住 —— 它们全是**我把推算当成了实测**。第一版第 1 节标题写着"（实测）"，内容却是按往返延迟推出来的。

| 第一版写的 | 实测（2026-09-26） |
|---|---|
| 「本项目当前 ~100 节点 / **126** 条边」 | **2,182 节点 / 9,293 条边**，31 种 edge label。126 是某个子集查询的结果，不是总量。有 `verify_status` 的仅 44 条 |
| 「逐条写边，百万条约 33 分钟，**超过 Lambda 15 分钟上限**」 | 那个逐条写边的 `etl_deepflow` 实测 **6.7 秒**。最慢的 `etl_aws` 是 79.9 秒 = Lambda 上限的 **9%**，约 10 倍余量 |
| 「失活对账每 5 分钟扫一遍该 label 全部边」 | 边级失活扫的 `Calls` 只有 **12 条**。另有一套独立的顶点级 GC（`graph_gc.py`），两侧机制不同 |
| 「`reservedConcurrentExecutions=1` 这个保护直接成为吞吐天花板」 | **这个保护从未生效**。它加在 `etl_trigger` 上，而该 Lambda 根本不写 Neptune（`neptune_query` 出现 0 次）；三个真正的写入方都无并发限制，实测并发峰值 **3**，每天如此且无事故 |
| 「必须启用并定期刷新 DFE statistics」 | **本来就是启用且新鲜的**：`autoCompute: True`，刷新时间距查询 10 分钟，`explain` 显示估计值精确（Calls 12↔12、RunsOn 1964↔1964）|
| 「失活对账按 `scope` 分片」 | **`scope` 在边上一条都没有**（9,293 条边覆盖率 0）。它是**节点**属性（`Pod` 1,545/1,545）。这个分片键当前不存在 |
| 第一阶段四项「纯代码改动，现在就能做」 | **三项前提不成立**。实测后的正确优先级见 §8.1 —— 先修「正在错」（多源印证、inconclusive 率、已知错误陈述），不是性能 |

### 9.3 这两批错误的共同模式

**读到一句话就当成事实，没去验。** 具体形态：

- 把注释的声明（"防止并发写入 Neptune"）当成生效的行为
- 把某个子集查询的结果（126）当成全局基线
- 把往返延迟推算当成实测，还在标题里写"（实测）"
- 把节点属性（`scope`）当成边属性，因为记得"最近补过 scope 写入"

修正它们只用了不到一小时的实测 —— `openCypher` 数三次、CloudWatch 查一次指标、`explain` 跑两次、`git blame` 一次。**代价不对称到这个程度，说明「先验后写」不是纪律问题而是效率问题。**

---

## 附：证据分级与主要来源

**Neptune 官方文档**
[Streams](https://docs.aws.amazon.com/neptune/latest/userguide/streams.html) ·
[Limits](https://docs.aws.amazon.com/neptune/latest/userguide/limits.html) ·
[Instance types](https://docs.aws.amazon.com/neptune/latest/userguide/instance-types.html) ·
[Storage & indexing](https://docs.aws.amazon.com/en_us/neptune/latest/userguide/feature-overview-storage-indexing.html) ·
[Efficient upserts](https://docs.aws.amazon.com/neptune/latest/userguide/gremlin-efficient-upserts.html) ·
[Bulk load optimize](https://docs.aws.amazon.com/neptune/latest/userguide/bulk-load-optimize.html) ·
[DFE statistics](https://docs.aws.amazon.com/neptune/latest/userguide/neptune-dfe-statistics.html) ·
[Analytics algorithms](https://docs.aws.amazon.com/neptune-analytics/latest/userguide/algorithms.html)

**论文（含实验数据）**
[Optimizing Differentially-Maintained Recursive Queries on Dynamic Graphs (PVLDB 2022)](https://arxiv.org/abs/2208.00273) ·
[Experimental comparison of tree-data structures for fully-dynamic connectivity (SIGMOD 2025)](https://arxiv.org/abs/2501.02278) ·
[Fully dynamic biconnectivity in Õ(log²n)](https://arxiv.org/abs/2503.21733) ·
[CUTTANA: Scalable Graph Partitioning](https://arxiv.org/html/2312.08356v1) ·
[Distributed Articulation Point Identification in Time-Varying Networks](https://arxiv.org/html/2512.04409) ·
[Alibaba microservice trace analysis](https://arxiv.org/abs/2504.13141)

**生产系统**
[Netflix Service Topology (InfoQ 转述一)](https://www.infoq.com/news/2026/06/netflix-microservices-realtime/) ·
[Netflix Service Topology (InfoQ 转述二)](https://www.infoq.com/news/2026/08/netflix-service-topology/) ·
[New Relic relationship TTL](https://github.com/newrelic/entity-definitions/blob/main/docs/relationships/relationship_synthesis.md) ·
[Edge Delta: Blast Radius Is a Graph Query](https://edgedelta.com/company/blog/blast-radius-is-a-graph-query) ·
[JanusGraph partitioning](https://docs.janusgraph.org/v0.5/advanced-topics/partitioning/) ·
[Grafana service graphs](https://grafana.com/docs/tempo/latest/metrics-from-traces/service_graphs/)

**CMDB 与多源印证（本轮新增）**
[ServiceNow IRE：识别与对账规则](https://www.servicenow.com/community/cmdb-articles/cmdb-understanding-identification-and-reconciliation-rules-and/ta-p/3520826) ·
[IETF draft：Multi-Source Corroboration（三态观测／独立性警告）](https://www.ietf.org/archive/id/draft-chandra-agent-registry-corroboration-00.html) ·
[Virima：discovery 跑了但 reconciliation 没跑](https://virima.com/blog/asset-discovery-reconciliation-automation) ·
[Virima：把关系边当 discovery 产出而非人工录入](https://virima.com/blog/ci-relationship-accuracy-without-data-stewards) ·
[rezolve.ai：CMDB 的失败模式是静默的](https://www.rezolve.ai/blog/cmdb-architecture-scalability)

**本项目实测（2026-09-26，勿用第一版的推算值）**
- 图规模：2,182 节点（`Pod` 1,545 = 71%）／9,293 边／31 种 edge label／39 种 node label
- 写入耗时：`etl_deepflow` 6.7 s、`etl_aws` 79.9 s、`etl_cfn` 3.8 s（Lambda 上限 900 s）
- 并发峰值：`etl_aws` = 3，无事故
- DFE：`autoCompute: True`，估计值精确（Calls 12↔12、RunsOn 1964↔1964）
- 验证结论：confirmed 17／inconclusive 16／modeling_artifact 5／bootstrap_only 2／untested 2

**明确查不到（勿当依据）**
- 单 writer 每秒写入边/节点数：AWS 无公布，须实测
- Bulk Loader 每小时边数：AWS 无承诺值
- 每节点/边/属性字节数公式：AWS 未提供
- Datadog / Dynatrace 的精确刷新周期与边过期判据：官方只给"实时"措辞
- 按单条边历史频率自适应 TTL 的成品算法：无厂商公开
- "用 IVM 引擎维护图可达性/割点作为生产派生结论"的公开案例：未找到
