# 业界如何实现依赖关系管理 —— 机制层调研

**日期**：2026-08-30 14:35 UTC
**方法**：三条并行调研线（商业可观测性厂商 / 配置管理与服务目录 / 学术与大厂工程实践），只取机制细节，不取产品介绍。每条结论区分「文档明确写了」与「推断」。

---

## 一、四种范式

| 范式 | 代表 | 裁决方式 | 边消失的语义 |
|---|---|---|---|
| **属性级规则仲裁** | ServiceNow IRE | `(CI类, 属性, 来源) → 优先级`；被拒字段进 `maskedAttributes` | precedence + staleness rules：权威源长期不更新则次级源接管 |
| **声明式单一权威 + 派生关系** | Backstage | 每个实体由唯一 provider 拥有生命周期；relations 从 `spec.*` **派生**，只有一侧声明、另一侧是镜像 | provider 不再产出 → 标记 `backstage.io/orphan` |
| **幂等 upsert + 时间戳收敛** | Cartography (CNCF) | 不做仲裁，各来源写各自视角，`MERGE` 累积属性 | `lastupdated ≠ 本轮 update_tag` = 陈旧 = 删 |
| **持久实体 + 显式 TTL** | New Relic / Dynatrace | GUID / Smartscape ID 从属性确定性派生 | NR：关系 `expires` 默认 PT75M（10min–72h）；DT：`lifetime.start/end` + 固定 35 天硬保留 |

**关键分野**：Datadog / OTel servicegraph / Elastic APM **没有持久边实体**——边的「存在」等于查询时间窗内有观测，或 Prometheus counter 是否还在涨。所以它们也就没有软删除这回事。只有 New Relic 与 Dynatrace 把边建成带生命周期的持久对象。

---

## 二、New Relic entity-definitions —— 唯一公开、声明式、可校验的身份定义范例

仓库：[newrelic/entity-definitions](https://github.com/newrelic/entity-definitions)

- 每个 entity type 一个 `definition.yml`，顶层 `domain` + `type`；`synthesis.rules` 下每条规则**至少要有 `identifier` 和 `name`** 才能生成实体。
- `conditions` 用于收窄匹配，否则「任何带 hostname 的遥测都会造出实体」。matcher：`present` / `anyOf` / `startsWith` / `regex`，默认大小写不敏感。**多条件是 AND，不支持 OR。**
- GUID 由四段组成：`account | domain | type | identifier`，URL-safe base64、无 padding、`|` 分隔。identifier 超长用 `encodeIdentifierInGUID: true`。
- 关系合成里 identifier 可由多个 **fragment** 拼接（硬编码串 / 属性值 / 属性值的一部分），支持 `toUpperCase`/`toLowerCase`，可用 `FARM_HASH` 哈希。例：`["k8s:", service.name, ":pod:"]`。
- 边规则文件命名 `<TYPE>-to-<TYPE>.yml`；两端 GUID 三种解析器：`extractGuid`（属性里直接有）、`buildGuid`（四段拼）、`lookupGuid`（用 candidates 查找，**可连到未插桩实体**）。
- `relationshipType` 是**闭集**：CALLS / CONTAINS / HOSTS / SERVES / IS / OPERATES_IN / CONNECTS_TO / BUILT_FROM / MEASURES / PRODUCES / CONSUMES / MANAGES / OWNS / TEST。

### 冲突消解规则（写死且机器可校验）

> 同名属性不能为两个不同的 Domain-Type 抽取 identifier，除非设了 conditions 加以区分，**且一个 entity 的 conditions 不能是另一个的超集**。

### 治理也是机制

每个 entity type 有强制 `ownership`（primary 唯一 + secondary 多个）；改定义要开 PR、跑自动校验、owner + Entity Platform team **双重评审**才能合并。

### 生命周期

- 关系有显式 `expires`（ISO-8601），**默认 PT75M**，允许 10 分钟到 72 小时。语义：该时间内未被再次上报则关系消失。
- entity 停止上报后**默认保留 8 天**再删除（为了能事后 debug）。

**可借之处**：这是把「身份定义」从代码里抽出来做成配置的完整范例——身份怎么拼、关系类型闭集、TTL、冲突约束、评审流程，全部是声明。

---

## 三、Cartography 的 `update_tag` 范式 —— 最可直接照搬的工程细节

文档：[Writing a new intel module](https://cartography-cncf.github.io/cartography/dev/writing-intel-modules.html)

架构与本项目同构：多个 intel module 把资产及关系写入**同一个图数据库**。每个 module 固定四段：**Get → Transform → Load → Cleanup**。

### 幂等 upsert

```cypher
UNWIND $DictList AS item
  MERGE (i:AWSEMRCluster{id: item.Id})
  ON CREATE SET i.firstseen = timestamp()
  SET i.lastupdated = $lastupdated, i.arn = item.ClusterArn
  MERGE (i)<-[r:RESOURCE]-(j)
  ON CREATE SET r.firstseen = timestamp()
  SET r.lastupdated = $lastupdated
```

硬约束：所有节点必须有 `id`（唯一标识，AWS 用 ARN）、`lastupdated`、`firstseen`；所有关系必须有 `lastupdated`、`firstseen`。

### 陈旧收敛

```
每轮 sync 启动时生成单调时间戳 update_tag
  → 所有 module 把它作为 lastupdated 写到每个节点和每条边
  → cleanup 删除 lastupdated ≠ 本轮 update_tag 的一切
```

- **Cleanup 由 schema 生成，不是手写**：
  ```python
  cleanup_job = GraphJob.from_node_schema(EMRClusterSchema(), common_job_parameters)
  cleanup_job.run(neo4j_session)
  ```
- **`scoped_cleanup=True`（默认）**：只删「连到当前正在同步的 sub-resource（如某个 AWS account）」的陈旧对象。因为账户是逐个同步的，不该误删其他租户。
- **`cascade_delete`**：父节点经 `RESOURCE` 边陈旧被删时连带删**一层**子节点；本轮被重新挂到新父的子节点受保护。
- **`ON CREATE SET firstseen` 只设一次**，后续 sync 不动，保留首次发现时间。
- `lastupdated` 自动建索引，专为加速 cleanup。

### 两种多源写法（必须显式区分）

- **Simple Relationship Pattern**：模块 A 只按 ID 引用 B、不带 B 的额外属性 → 只定义关系 schema，Load 时 `MATCH` 到已有 B 并连边。
- **Composite Node Pattern**：模块 A 引用 B 且带 B 自己没有的字段 → 定义 `BASchema`（命名上明示「A 眼中的 B」），target 同一个 node label，靠 `MERGE` 把两来源属性**累积**到同一节点。

设计原则原文：「**每个 intel module 提供自己对图的视角**」，**鼓励**多个模块修改同一节点类型，不假设别的模块会摄入相同数据。

---

## 四、ServiceNow IRE —— 属性级权威的完整模型

### Identification（身份识别）

- **Identifier**（`cmdb_identifier`）定义在类层级、可被子类继承；子类规则 `active=false` 则继承父类。
- **Identifier entry**（`cmdb_identifier_entry`）：带 `order` 优先级的多条匹配条目。含 `attributes`（criterion attributes，参与匹配的字段集）、`entry_type`（**Standard** 直接匹配目标表 / **Lookup** 经关联表匹配）、`search_table`。
- **first-match-wins**：按优先级逐条尝试，第一条命中即「更新已有 CI」，全不命中则新建。

`cmdb_ci_hardware` 的默认优先级链示例：
```
1:   name
50:  mac_address
90:  product_instance_id
100: serial_number,serial_number_type  (lookup)
200: serial_number
300: name
400: mac_address,name                  (lookup)
```

### De-duplication

首次识别成功后在 `sys_object_source` 写一条 **source native key ↔ 目标 CI sys_id** 的映射；后续同源再来时若 native key 已存在，**IRE 跳过识别流程直接更新**。这是主要防重机制。重复 CI 通常源于该映射缺失或高优先级识别属性数据缺失。

### Reconciliation（属性级权威）

- **权威源锁定**：若 `ServiceNow` 被设为某属性的唯一权威源，其他源**可创建 CI 并填初值，但不能后续修改**该属性；被拒字段进 `maskedAttributes`（**明示拒绝，不静默丢弃**）。
- **优先级仲裁**：数值小 = 优先级高（如 `ServiceNow=100` > `ServiceWatch=200`）。
- **null 保护**：`glide.reconciliation.override.null` 单独控制来源能否把字段写成空；权威源要清空需把属性加入 "Update with null" 列表。
- **写前条件过滤**：`glide.identification_engine.enable_reconciliation_filter_before_update` 允许在通过 precedence 检查后，再用目标 CI 的条件过滤器放行/拦截（如 `operational_status != Retired` 才允许更新）。
- **时间维度**：precedence + staleness rules + Data Source History 会让长期不更新的权威源把控制权让给次级源。

### 可抽象成表，但必须是四列

```
(类, 属性) → [(来源, 优先级, 是否受保护, 陈旧后接管)]
```

三个 caveat：规则有类继承（表要按具体类展开或标注继承）；社区实证发现选「specific attributes」的行为更像**来源解析顺序**而非严格白名单，仍可能写空/新增扩展属性；静态表需要加「陈旧后接管来源」一列才完整。

---

## 五、粒度错配：全行业都没有银弹

共同机制：**粒度由「插桩报了哪些属性 + 一条显式优先级链」决定，缺细粒度属性就塌缩到粗粒度节点，而非自动展开。**

- **Datadog** 的 inferred services 优先级链：
  - Database：`peer.db.name` > `peer.aws.s3.bucket` / `peer.aws.dynamodb.table` / `peer.cassandra.contact.points` > `peer.hostname` > `peer.db.system`
  - Queue：`peer.messaging.destination` > `peer.kafka.bootstrap.servers` / `peer.aws.sqs.queue` > `peer.messaging.system`
  - Inferred service：`peer.service` > `peer.rpc.service` > `peer.hostname`

  **所以 Datadog 的 S3 依赖粒度也取决于插桩是否报了 bucket 名**——缺 bucket 名就回退到 host 级。X-Ray 泛化 S3 是行业通病，不是自家实现缺陷。

  另有一条反高基数规则值得抄：**匹配 IP 格式的 peer 属性值被强制改写成 `blocked-ip-address`**。

- **OTel / Tempo** 数据库节点命名链：`peer.service > server.address > network.peer.address:port > db.namespace > db.name`。

- **Elastic APM** 是唯一把粒度显式拆成两维的：`service.target.type` + `service.target.name`（至少一个非空）。有库名才有 `mysql/my-db`，没有就是 `mysql`；rabbitmq 队列 → `rabbitmq/my-queue`；HTTP → `host:80`。目的明确写着是「为 service map / dependencies 提供更高粒度」。

- **AppDynamics** 是唯一把粒度写成硬建模约束的：「任何一个被识别的 backend，其调用**必须只由一个下游 tier 处理**」。文档举例：若 tier A 调 `localhost:4040` 的 HTTP router 再按 URL 转发到 B 或 C，就必须写自定义命名规则把足够的 URL 段纳入 backend properties，使去 B 和去 C 得到不同身份。另提供 **aggregate / split backends** 做人工再标注，以及 backend 注册上限 + 手动删除。

- **Dynatrace** 用 `id_components` 决定实体身份粒度，`id_classic` 做不同粒度视图的交叉引用（如 `CLOUD_APPLICATION_INSTANCE` 在 Grail 叫 `K8S_POD`）。

**最关键的一条**：「指标 scope ≠ 拓扑 scope」（VPC 级聚合指标归属到单实例）这类问题，**业界公开文档里没有任何原语来解**。能查到最接近的只有 New Relic 的 `instrumentation.name/provider` 来源标注和 Dynatrace 的 `id_classic`。

---

## 六、验证方法论

### 6.1 Mystery Machine 的证伪法（OSDI 2014）

Chow et al., *The Mystery Machine: End-to-end Performance Analysis of Large-scale Internet Services*, OSDI 2014.

前提是「没有人能提供完整准确的系统模型」，但请求量极大（论文用了 **130 万条 Facebook 请求**），于是反过来做：

1. **先假设最强模型**——任意两个 trace segment 之间，假设所有可能的因果关系都成立。三类：happens-before、mutual exclusion、pipeline。
2. **用每条 trace 反驳**——只要有**一条** trace 出现「B 在 A 之前」这样的反例，就删掉 A⇒B 这条假设；互斥关系只要被观测到一次共现就删掉。
3. **剩下没被任何 trace 反驳的**就是幸存模型。论文报告约几千条 trace 后关系数趋于稳定。

**一句话：边不是被证明出来的，是没被反驳掉的。**

固有局限：**覆盖率决定召回**——一条边若在样本里从不出现，既不会被断言也不会被反驳。

### 6.2 混沌验证：有成熟先例

- **Gremlin**（Heorhiadi et al., **ICDCS 2016**）是系统化故障注入验证微服务的学术源头——操纵服务间网络流量注入失败，做受控的「失败如何传播」实验。
- **Krasnovsky & Zorkin, arXiv:2506.11176（2025-06）** 做了完整闭环：从 Jaeger trace 抽出 DeathStarBench Social Network 依赖图 → 图上跑 Monte-Carlo 故障模拟（P_fail=30%，450 万样本）→ **在真实系统上跑对应 chaos 实验**（随机杀 30% 容器 + wrk2 压测）→ 对照。
  - 无副本：预测 0.161 vs 实测 0.186
  - 有副本：**0.305 vs 0.305**（MAE ≤ 0.0004，整体相对误差约 8%）

  该论文亲口承认的局限正是反用它的动机：**「我们假设已知所有依赖边；漏掉一条边会导致模型高估韧性。」**

### 6.3 RCA 文献的盲区

**绝大多数 RCA 论文把依赖图当既定输入，只验证「用它定位根因准不准」，几乎不验证图本身对不对。**

| 路线 | 代表 | 图来源 | 验证图本身？ |
|---|---|---|---|
| 调用图驱动 | MonitorRank (SIGMETRICS'13)、MicroRCA (NOMS'20) | 直接用 trace 拓扑做随机游走 | ❌ 假定为真 |
| 因果发现 | Microscope (ICSOC'18)、CausalRCA (arXiv:2209.02500)、Neural Granger RUN (arXiv:2402.01140) | 用指标时序跑 PC 算法 / Granger 因果学出 DAG | ⚠️ 验证的是 RCA 命中率 |

2024 年综述 *RCA for Microservices based on Causal Inference: How Far Are We?*（arXiv:2408.13729）明确指出因果类 RCA 缺乏对所学因果图正确性的独立 ground truth。**「校验依赖图」在学界仍是开放问题。**

反直觉的可用之处：因果发现的边**不来自调用 trace**，所以可当双源交叉验证的第二源——统计因果图上有 A→B 而调用 trace 上没有（或反之）即为可疑边。

### 6.4 七种可操作的验证手段

| # | 手段 | 怎么验证 A→B | 先例 | 证据强度 |
|---|---|---|---|---|
| 1 | **主动故障注入** | 杀/降级 A，观测 B 是否受影响 | Gremlin ICDCS'16；arXiv:2506.11176 | **强** |
| 2 | **反事实证伪** | 假设边存在，用海量 trace 找反例 | Mystery Machine OSDI'14 | **强** |
| 3 | **双源交叉验证** | 边须在两个独立源同时出现（trace ∧ eBPF；调用图 ∧ 统计因果图） | arXiv:2608.04413 建议 diff 两者 | 中 |
| 4 | **统计因果发现** | PC 算法 / Granger 从指标时序独立推断 | Microscope、CausalRCA、RUN | 中 |
| 5 | **契约测试** | 消费者声明对 provider 的调用契约 | [Pact](https://docs.pact.io/) | 中（只验声明的边） |
| 6 | **配置声明比对** | IaC / 网格策略声明 vs 运行时观测图 diff | 服务网格策略 | 中 |
| 7 | **关键路径重要性** | 边是否 load-bearing | [Uber CRISP](https://www.uber.com/us/en/blog/crisp-critical-path-analysis-for-microservice-architectures/) | 中 |

---

## 七、eBPF 派的系统性盲区

三者（Pixie / Cilium Hubble / DeepFlow）都 hook `tcp_v4_connect`（出站）/ `inet_csk_accept`（入站），**探针「每条 TCP 连接建立触发一次，而非每包触发一次」**（arXiv:2608.04413 明确表述）。

四类盲区：

1. **TLS/加密**——TLS 1.3 后内核层只见密文，须把探针上移到用户态 TLS 库 uprobe。Pixie 博客记载的脆弱性：OpenSSL 的 `SSL` 结构体内存偏移在 v1.1.0/v1.1.1/v3.0.0 间逐版本变化；**BoringSSL 滚动发布无版本号**几乎无法定偏移；**静态链接 + stripped 二进制**（Envoy 上游发行常见）使 uprobe 无法 attach → 该服务的边直接缺失；custom BIO 应用无法把明文关联回连接。Pixie 自报完整性检查 99.937% 通过但**明确列出 5 个不兼容程序**。

2. **连接池复用 / 长连接**——被复用的长连接在建立后不再触发建连事件，所以「首包之后的所有后续调用」在建连级观测里不可见；即使抓到建连，调用量也被严重低估；HTTP/2 与 gRPC 多路复用把多条逻辑边**坍缩成一条 TCP 边**。

3. **DNS 作为观测源**——客户端 DNS 缓存命中不再发查询；直连 IP、ClusterIP、已建立的长连接都不触发解析；连接池复用同样跳过。**若同时用 eBPF 建连和 DNS 作两个源，二者一起漏，不互补。**

4. **被 sidecar / NAT 遮蔽**——Istio/Envoy 模式下真实 A→B 被拆成 `app → 127.0.0.1 sidecar` 与 `sidecar → 远端`；arXiv:2510.15490 明确点名 NAT 使被动观测网络元数据的方法「无法准确推断服务依赖」。

**一个诚实的验证细节**：arXiv:2608.04413 只在自建 20 服务 testbed 上「恢复了已知的 ground-truth 拓扑」，作者反复强调这**只证明流水线能还原已知图，不证明能发现未知依赖**，并坦承「真实依赖图是重尾偏斜的，罕见代码路径只在特定条件触发，我们对那种情形的覆盖率与归因准确率没有证据」。

---

## 八、公开数据集：拿它当真值前必须清洗

| 数据集 | 适用性 |
|---|---|
| **DeathStarBench**（ASPLOS'19）/ **TrainTicket** | 拓扑自建、真值由构造已知、可注入故障 —— **最适合做验证闭环** |
| **Alibaba cluster-trace-microservices** v2021/v2022 | 规模与重尾真实，适合压测方法在生产尺度下的假阴性，**但需先清洗** |

**关键警告**：ICPE 2024 的 Casper 论文（*Systemizing and Mitigating Topological Inconsistencies in Alibaba's Microservice Call-graph Datasets*）量化发现：2021 数据集严格按官方规范只能重建 **58.32%** 的 trace，Casper 绕过不一致后提到 **83.82%**；2022 数据集 86.42% → 98.6%。而且**严格遵守官方规范的工具会静默忽略这些不一致，生成大小和形状都错的 trace，从而误导研究者**。

拿它当 ground truth 前不清洗，等于用一个有错的真值评判自己的图。

---

## 九、一个业界空白

**连接池 / 长连接导致依赖发现系统性假阴性，机制清楚，但公开文献没有任何定量。**

eBPF 依赖发现论文（arXiv:2608.04413、2510.15490）都只**定性**承认覆盖率/重尾/罕见路径问题，没有给出「连接池导致的漏边率 = X%」这样的数字。pgbouncer 等连接池文献量化的是性能与级联超时，不是依赖发现的假阴性率。

补一个对照实验就能填上：在 DeathStarBench（Thrift RPC + 连接池，真值已知）上分别以「强制每请求新建连」与「开启连接池」两种配置跑 eBPF 发现，diff 出的漏边即为连接池假阴性率。

---

## 来源清单

**可观测性厂商**
- Datadog Inferred Services：https://docs.datadoghq.com/tracing/services/inferred_services/
- Datadog USM：https://docs.datadoghq.com/universal_service_monitoring/setup/
- Dynatrace Smartscape：https://docs.dynatrace.com/docs/discover-dynatrace/platform/smartscape
- Smartscape on Grail（lifetime / upsert / 35 天保留）：https://docs.dynatrace.com/docs/discover-dynatrace/platform/grail/smartscape-on-grail
- New Relic entity-definitions：https://github.com/newrelic/entity-definitions
- OTel servicegraph connector：https://github.com/open-telemetry/opentelemetry-collector-contrib/blob/main/connector/servicegraphconnector/README.md
- Tempo service graphs（virtual nodes / span multiplier）：https://grafana.com/docs/tempo/latest/metrics-from-traces/service_graphs/
- AppDynamics backend detection：https://help.splunk.com/en/appdynamics-saas/application-performance-monitoring/26.5.0/configure-instrumentation/backend-detection-rules/default-automatic-backend-discovery
- Elastic service.target spec：https://github.com/elastic/apm/blob/main/specs/agents/tracing-spans-service-target.md

**配置管理 / 服务目录**
- ServiceNow IRE 概念：https://www.servicenow.com/docs/bundle/xanadu-servicenow-platform/page/product/configuration-management/concept/c_CMDBIdentifyandReconcile.html
- IRE reconciliation rules：https://www.servicenow.com/community/cmdb-articles/understanding-ire-reconciliation-rules/ta-p/3289239
- 识别规则 / identifier entry / 去重：https://www.servicenow.com/community/itom-articles/how-to-verify-identification-rules-for-service-graph-connectors/ta-p/3417132
- Data source precedence + staleness：https://docs.servicenow.com/bundle/istanbul-it-service-management/page/product/configuration-management/task/t_DefineDataSourcePrecedence.html
- Backstage well-known relations：https://backstage.io/docs/features/software-catalog/well-known-relations/
- Backstage entity references：https://backstage.io/docs/features/software-catalog/references/
- Backstage life of an entity：https://backstage.io/docs/features/software-catalog/life-of-an-entity/
- Cartography intel module（update_tag / cleanup 全套）：https://cartography-cncf.github.io/cartography/dev/writing-intel-modules.html

**AWS 原生源**
- Config direct/indirect relationships：https://docs.aws.amazon.com/config/latest/developerguide/faq.html
- Cloud Map：https://docs.aws.amazon.com/cloud-map/latest/dg/what-is-cloud-map.html
- Application Signals Service Map：https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/ServiceMap.html
- Resilience Hub 依赖发现（DNS 日志 + 35 天回看）：https://docs.aws.amazon.com/resilience-hub/latest/userguide/next-gen-how-discovery-works.html
- AppComponent grouping：https://docs.aws.amazon.com/resilience-hub/latest/userguide/AppComponent.grouping.html

**学术**
- Mystery Machine, OSDI'14：https://www.usenix.org/system/files/conference/osdi14/osdi14-paper-chow.pdf
- Canopy, SOSP'17：https://research.facebook.com/publications/canopy-end-to-end-performance-tracing-at-scale/
- Krasnovsky & Zorkin 2025（chaos 交叉验证）：arXiv:2506.11176
- MonitorRank, SIGMETRICS'13 / Microscope, ICSOC'18 / MicroRCA, NOMS'20
- CausalRCA：arXiv:2209.02500 ｜ Neural Granger RUN：arXiv:2402.01140 ｜ How Far Are We：arXiv:2408.13729
- eBPF 零插桩依赖发现：arXiv:2608.04413 ｜ Retrofitting Service Dependency Discovery：arXiv:2510.15490
- Pixie eBPF TLS tracing：https://blog.px.dev/ebpf-tls-tracing-past-present-future/
- Casper（Alibaba trace 不一致），ICPE'24：https://research.spec.org/icpe_proceedings/2024/proceedings/p276.pdf
- Alibaba clusterdata：https://github.com/alibaba/clusterdata/blob/master/cluster-trace-microservices-v2021/README.md
- DeathStarBench, ASPLOS'19：https://arxiv.org/pdf/1905.11055.pdf
