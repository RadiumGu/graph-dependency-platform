# 大规模依赖图的更新与刷新架构

本文回答一个具体问题：**图长到几十万节点、上百万条边之后，某个节点或边发生变化，依赖关系怎么刷新。**

结论先行：

1. **这个规模不是"大图"。** 百万边整张图装得进单机内存，全图 Tarjan 求割点是毫秒级。驱动架构变化的不是"算不下"，而是写入吞吐、失活对账、以及派生结论的失效传播。
2. **原始边的刷新走"变更驱动 + 时间窗聚合"**，不是全量轮询。当前实现的全量轮询在这个规模会撞上 Lambda 15 分钟上限。
3. **派生结论按能否增量维护分成三类，各用各的策略。** 其中拓扑排序与割点**没有任何通用增量引擎能维护**，只能全量重算 —— 好在这个规模全量就是秒级。
4. **不引入 IVM 引擎，不自研全动态图算法。** 前者对本场景四类结论只覆盖两类且删除路径最弱；后者学术上漂亮、工程上无可用实现。
5. **Neptune 不支持水平分片**，写吞吐无法横向扩 —— 这是本架构最硬的边界，必须在设计里显式承认。

证据分级贯穿全文：**〔实测〕**= 本项目实测或论文实验数据；**〔官方〕**= AWS/厂商官方文档；**〔理论〕**= 论文的分析结论，无实验数字；**〔查不到〕**= 公开信息缺失，明确标注而不猜。

---

## 0. 先纠正一个前提：这个规模在图领域不算大

把目标规模放进图处理领域的坐标系：

| 图 | 顶点 | 边 |
|---|---|---|
| 本项目当前 | ~100 | 126 |
| **本文目标** | **~10⁵** | **~10⁶** |
| Twitter（图分区论文常用） | 41 M | 1.4 B |
| UK-2007 web 图 | 105 M | 3.3 B |

〔实测〕百万条边序列化后是几百 MB 量级。**整张图能装进单机内存**，METIS 这类离线分区算法能轻松跑完 —— 而 METIS 跑不动 Twitter/Web 图，那才是流式分区算法被发明出来的理由（[CUTTANA, arXiv 2312.08356](https://arxiv.org/html/2312.08356v1)）。

这条判断决定了后面所有取舍。**如果照着"分布式大图"的套路设计，会为不存在的问题付出复杂度，并引入真实的正确性风险**（见 §6 那条铁律）。

⚠️ 本文按"稳定在百万边量级"设计。若预期增长到十亿边以上，§6 的分区部分需要换成完整的分布式方案，其余章节仍适用。

---

## 1. 当前实现会在哪里断（实测）

| 机制 | 当前实现 | 百万边下的后果 |
|---|---|---|
| 采集 | EventBridge 定时 5/15min/1h/daily，每轮**全量** | 采集量翻四个数量级 |
| 边写入 | `for e in edges:` → 逐条 `neptune_query()` | 按同 VPC 往返 2 ms 估算，百万条约 33 分钟，**超过 Lambda 15 分钟上限** |
| 节点写入 | 链式 `mergeV`，`BATCH_SIZE=20` | 已批量，但边没享受到 |
| 失活对账 | `g.E().hasLabel('Calls')` + `last_seen` 过滤 | 每 5 分钟扫一遍该 label 全部边 |
| 并发 | `etl_trigger` 设 `reservedConcurrentExecutions=1`（注释写明"防止并发写入 Neptune"） | 这个保护直接成为吞吐天花板 |
| 存储 | `db.r6g.large`（2 vCPU / 16 GiB），**单实例无读副本** | buffer pool 约 10 GiB，热点子图大概率装不下 |

代码位置：`/home/ec2-user/works/graph-dependency-platform/infra/lambda/etl_deepflow/neptune_etl_deepflow.py`
（边写入 `:1355`，失活对账 `:1575`、`:1592`，批大小 `:73`）

**一处更正**：本文早先的口头结论说 `g.E().hasLabel('Calls')` 是"全边扫描"，不准确。见 §2 的索引部分 —— 它走 POGS 索引的前缀 range scan，扫的是**该 label 下全部边**而非全库。在以 `Calls` 为主的图里两者接近；在多 label 的百万边图里差别很大。

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
3. **失活对账从"扫 label 全部边"改成水位线 + 分片。** Neptune 没有原生 TTL，也没有 (label, 属性) 复合索引，所以"每轮扫 label 全部边再过滤 `last_seen`"是结构性的。改法是按 `scope` 或 source 分片，每轮只处理一个分片；或维护按 `last_seen` 分桶的候选集。
4. **读写分离。** 加只读副本，查询与 Streams 消费者走副本，ETL 写主库。代价是接受复制延迟。

### 3.3 `reservedConcurrentExecutions=1` 怎么处理

这个设置的原意是"防止并发写入 Neptune"，但 Neptune 本身支持多线程并发写（官方推荐多线程 + 50–100 批大小）。真正需要串行的是**同一条边的写入顺序**，不是全局。

改法是**按边的 key 分片并发**：用 `(src, dst, label)` 哈希决定分片，同分片内串行、跨分片并行。这样并发度从 1 提到分片数，而同一条边的写入顺序仍然有保证。注意 `db.r6g.large` 只有 4 个查询执行线程，并发度要与实例规格匹配 —— 盲目提高并发只会排队。

---

## 4. 边的生命周期：这是"刷新"真正难的地方

### 4.1 问题：零流量不等于依赖不存在

本项目已经踩过一次：`Retrieves → nutrition-kb` 因为几小时没人问营养问题被置 `active=false`，而那个知识库客观存在（控制面 `list_knowledge_bases` 就返回它）。**图谱给出的是错误陈述，而不是过期陈述。**

〔实测，外部〕阿里巴巴 2021 集群 trace 的公开分析给了这件事的硬证据：同一入口的不同请求走出的调用图不同，且行为随时间显著变化（[arXiv 2504.13141](https://arxiv.org/abs/2504.13141)、[alibaba/clusterdata](https://github.com/alibaba/clusterdata/blob/master/cluster-trace-microservices-v2021/README.md)）。**单个刷新窗口内某条边零流量，完全可能只是这一窗没走到那条条件分支。**

百万边规模下，稀疏调用边的占比会显著高于现在（126 条边的环境里高频边占多数），这个误判会放大。

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

这对本项目尤其契合 —— 契约里已有的 `scope` 概念正好是分区键的雏形。同 namespace 内的服务互调远多于跨 namespace，所以跨区边比例天然低。

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

**第一阶段：解除当前瓶颈（纯代码改动，现在就能做）**

1. 边写入批量化（`mergeE`，按 200 records 折算批大小）
2. 启用并定期刷新 DFE statistics，用 `explain` 验证 `hasLabel + last_seen` 的实际计划
3. 失活对账按 `scope` 分片，每轮只处理一片
4. `reservedConcurrentExecutions` 从 1 提到按边 key 哈希分片的并发度，与实例线程数匹配

**第二阶段：刷新机制换驱动方式**

5. 把已有的事件驱动链路提升为主路径，全量对账降到每日
6. 引入 5 分钟量纲的时间窗聚合，同窗内同一条边合并写入
7. 加只读副本，查询与后续的 Streams 消费者走副本

**第三阶段：边的生命周期**

8. 边上补 `valid_from` / `valid_to`，失活改为关闭有效期而非翻 `active`
9. 补 `confirm_count`，查询按置信排序
10. TTL 按 `(label, dependency_kind)` 分级查表，替换全局 `expires_seconds`

**第四阶段：派生结论**

11. 启用 Neptune Streams，建轮询消费者（跑在只读副本上，按 `commitNum`+`opNum` 重组事务边界）
12. 聚合型结论改增量物化
13. 评估 Neptune Analytics 承接割点/连通分量的全量重算
14. 建差分测试 + 定期全量对账

**每个阶段都要先实测再进下一阶段。** 特别是"单 writer 每秒能写多少边"这个数字 —— 〔查不到〕官方没有，必须用真实数据形状压测得出，它决定了第二阶段之后还需不需要应用层分区。

---

## 9. 本文推翻的三个判断

这几条是我在调研前给出的口头结论，调研后证明不准确，记在这里免得被继续引用：

| 我说过 | 实际 |
|---|---|
| "`g.E().hasLabel('Calls')` 是全边扫描" | 不是全库扫。label 在 P 位，走 POGS 前缀 range scan，扫的是该 label 下全部边。真正的问题是**没有 (label, 属性) 复合索引** |
| "几十万节点上全图割点分析不可行，O(V·(V+E))" | **错**。Tarjan 是 O(V+E)，百万边单机毫秒级。几十万节点在图领域是小图，整图装得进内存。反而"分区后各自算割点"才是错的做法 |
| "割点增量维护有算法但复杂" / "是 polylog 的，不是不可行" | 理论对（Õ(log²n)），**工程上不可用**：零可用实现，摊还最优的实测最慢，删除尾延迟可达 10⁴ 秒。主流库全都只支持加边不支持删边分裂 |

另外补一条更正：我提过"按边的历史调用频率自适应 TTL"作为解法，调研后确认**没有任何厂商公开这样的成品算法**。它是可行的自研方向，但不能当成业界既有实践引用。

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

**明确查不到（勿当依据）**
- 单 writer 每秒写入边/节点数：AWS 无公布，须实测
- Bulk Loader 每小时边数：AWS 无承诺值
- 每节点/边/属性字节数公式：AWS 未提供
- Datadog / Dynatrace 的精确刷新周期与边过期判据：官方只给"实时"措辞
- 按单条边历史频率自适应 TTL 的成品算法：无厂商公开
- "用 IVM 引擎维护图可达性/割点作为生产派生结论"的公开案例：未找到
