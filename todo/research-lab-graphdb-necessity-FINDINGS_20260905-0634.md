# FINDINGS — 依赖关系管理是否必须使用图数据库

**Question:** 业界实际如何进行系统依赖关系管理，依赖关系管理是否必须使用图数据库？按场景分档判定哪些查询真正需要图引擎、哪些用关系型/时序/指标存储即可，并标注证据等级。

---

## 执行摘要与推荐（EXECUTIVE SUMMARY）

**核心结论：不必须。"依赖关系管理必须用图数据库"是伪命题。** 在按场景切出的六档里，只有一档（真 graph-hard）是图引擎的硬需求，其余五档用关系型/时序/派生存储即可；而这一档的触发条件是**图的密度（fan-out）叠加无界深度并需要路径/图算法**，不是"跳数超过 N"。

**三条最有分量的证据支柱：**
1. **业界用脚投票。** 6 家主流 APM 只有 **Dynatrace** 一家上真图存储（Grail，可变+可遍历+独立保留），其余（New Relic/Datadog/Grafana Tempo/Kiali/AppDynamics）都从遥测**按需派生**边；开源侧 Backstage/Marquez 用 **Postgres**、DataHub 官方默认 **Elasticsearch 而非 Neo4j**、只有 Cartography 强制图库；**没有一个主流云厂商**（AWS Config/X-Ray、Azure Resource Graph 实为 KQL-over-Kusto 误称且 join 上限 3、GCP CAI）对外暴露变深度图遍历。[official-doc]
2. **"图恒快"经不起独立复现。** 最常被引的 Neo4j-in-Action 表出自厂商关联作者，独立复现翻转（4 跳 MySQL 0.93s vs Neo4j 65s）；两份独立同机基准显示浅查 Postgres 常赢、写得好的 CTE 3 跳能打赢 Neo4j。真正拉开差距的是**稠密图 ≥3 跳（≈77×）与图算法（PageRank ≈160×、社区发现无 SQL 等价）**。[independent-benchmark]
3. **图库有真实的运维代价与弃用先例。** 具名生产迁移 Capacities（Dgraph→Postgres，DB 成本降约 10×、基建省约 70%）、Sightfull（GraphDB→Postgres）；运维痛点多来自 **Neo4j 自家文档**（单主写、分片 MERGE 慢、备份 OOM）。[named-production + vendor-own-docs]

**给决策者的操作建议：**
- **默认从关系型起步**：关系库 + 规范化边表 + 应用侧遍历，或用 **SQL/PGQ / PuppyGraph** 直接在现有关系/湖仓表上做有界遍历，省掉单独图库与双写 ETL。仅当命中"加权最短/关键路径、图算法、稠密图无界传递闭包"才上原生图库（Neo4j/Neptune）或 GA 托管图（Spanner Graph）。
- **别把"图 vs 关系"和"边是否为真"混为一谈**：边的生命周期/来源/多源仲裁是坐在存储之上的**数据契约层**（Backstage、ServiceNow IRE 都在关系库上实现），换图库解决不了数据可信。
- **需要历史时点拓扑/审计就别用纯 derive-on-read**：派生模型答不了"上周二谁依赖谁"，须落到物化存储或数据契约层。
- **真上图库时，选型≈选方言**：GQL 可移植性今日仍 aspirational（仅 Google/MS Fabric shipped 真端点），把迁移成本压最低的现实做法是押 **openCypher/Cypher 家族**。

**完整的六档判定表见下文"【DoD 成品】按场景分档判定表"章节。** 证据分层（official-doc ＞ named-production ＞ peer-reviewed ＞ independent-benchmark ＞ vendor-claim）、≥3 条独立反证、以及"未证实"清单分别见对应章节。

**诚实边界（未证实，不可当结论引用）：** Datadog/AppDynamics/CSPM(Wiz/Orca/Prisma) 后端存储引擎官方未披露；阿里云/华为云 Config"无图遍历"为文档缺席推断；SCA reachability 厂商宣称的 90–95% 降噪无独立同行评审在该量级复现（同行评审只证实方向：~20% 依赖未部署、~87% 有 clean client）；Netflix 2019 血缘存储付费墙未取到；"provenance 才是难点非存储"为跨五家架构推断（moderate）。

---

## Cycle 0 — 基线三锚点：APM 厂商存储 / 开源目录血缘存储 / 图查询硬需求与基准

### A. 可观测性/APM 厂商：依赖拓扑的真实存储 (SQ1) — evidence: official-doc

| 厂商 | 承载存储 | 查询语言 | 是否图数据库 |
|---|---|---|---|
| Dynatrace Smartscape/Grail | Grail-native 拓扑存储，node/edge 可变、upsert、35 天保留 | DQL (`smartscapeNodes`/`smartscapeEdges`/`traverse`) | **是（唯一真图存储）** |
| New Relic 实体关系 | NRDB（云原生多租列存/事件库），关系由 relationship synthesis 规则合成后写入 | NRQL / NerdGraph | 否（通用库，非图 DB） |
| Datadog Service Map | 无公开图存储，从 APM span + peer tags 实时推导 | 无专用图查询语言 | derived（按需计算） |
| Grafana Tempo service graph | Prometheus 兼容时序库，span→`traces_service_graph_request_total` 指标 | PromQL | 否 / derived |
| Kiali (Istio) | 无自有拓扑存储，每次从 Prometheus 里 Istio 遥测现算 | PromQL | 否 / derived |
| AppDynamics / Splunk flow map | Controller 按时间窗动态生成；后端存储 flow-map 文档未披露 | 无公开查询语言 | derived |

判分水岭：**拓扑是否可变、可持久化、可图遍历**。Dynatrace 的 node/edge 有 upsert + `traverse` + 独立保留期 = 真图存储；其余五家的边是"有请求就有边、无请求就消失"的指标/span 派生物 = 计算态。
来源：[Smartscape on Grail](https://docs.dynatrace.com/docs/platform/grail/smartscape-on-grail)、[NR E&R via NRQL](https://docs.newrelic.com/docs/nrql/get-started/query-entities-and-relationships-via-nrql/)、[DD Service Map](https://docs.datadoghq.com/tracing/services/services_map/)、[Tempo service graphs](https://grafana.com/docs/tempo/latest/metrics-from-traces/service_graphs/)、[Kiali Prometheus](https://kiali.io/docs/configuration/p8s-jaeger-grafana/prometheus/)、[Splunk/AppD flow maps](https://help.splunk.com/en/appdynamics-saas/application-performance-monitoring/25.6.0/business-applications/flow-maps)。

### B. 开源目录/血缘平台：存储选型与官方理由 (SQ2) — evidence: official-doc

| 平台 | 存储 | 需要图库? | 官方理由 |
|---|---|---|---|
| Backstage catalog | SQLite(开发)/**PostgreSQL**(生产)，实体与 relations 存关系表 | **否** | 官方仅支持 "either SQLite or PostgreSQL"；生产推荐 Postgres |
| Cartography | **Neo4j**（核心必需） | **是** | "consolidates infrastructure assets and the relationships … powered by a Neo4j database"，"particularly good at exposing otherwise hidden dependency relationships" |
| DataHub (LinkedIn) | 主存 MySQL/Postgres + Elasticsearch + graph service（ES 或 Neo4j 二选一） | **否（可插拔）** | "support **either Elasticsearch or Neo4j** … **We recommend Elasticsearch**"；Helm 可 `neo4j-community.enabled=false` |
| OpenLineage/Marquez | **PostgreSQL**（规范化关系模型） | **否** | "normalized representation … efficiently associating (upstream, downstream) dependencies" |
| Netflix Service Topology | 自研图层跑在内部分布式 KV 之上 | 自研（非现成图库） | "graph database layer designed for fast traversal"，三源各存一图分区 |

来源：[Backstage DB](https://backstage.io/docs/golden-path/deployment/database/)、[Cartography](https://docs.cartography.dev/)、[DataHub graph migration](https://docs.datahub.com/docs/how/migrating-graph-service-implementation)、[Marquez](https://marquezproject.ai/docs/quickstart/)、[Netflix 转述 InfoQ](https://www.infoq.com/news/2026/06/netflix-microservices-realtime/)。

### C. 哪些查询是 graph-hard，递归 CTE 卡在哪 (SQ4)

关系型的共因短板：每一跳 = 一次自连接（B-tree 重定位 `O(log n)`、须物化整个 frontier），无 index-free adjacency；递归 CTE 是集合语义**无早停**（命中目标仍须算完整层）；"去重 vs 留路径"根本张力（`UNION` 去重丢路径、`UNION ALL` 留路径但行数组合爆炸）。

| 查询类 | 递归 CTE 能做吗 | 崩点 |
|---|---|---|
| 加权最短/关键路径 | 基本不能 | 无原生 BFS/Dijkstra、无优先队列早停；须枚举全路径再取 MIN → 指数级（须 pgRouting） |
| 环检测 | 能但笨拙 | 手工在数组列累积已访问节点，漏 depth guard 或用 UNION ALL 就永不返回 |
| 爆炸半径 / k-hop | 能 | 稠密图上 fan-out 爆炸、中间行组合膨胀（生产 "CTE blew up" 主因） |
| 可达性 | 能 | 无早停：命中目标仍算完整 frontier |
| 图算法 (PageRank/Louvain/中心性) | 无实用 SQL 等价 | — |

**反直觉 nuance：** 深度本身往往不是瓶颈——稀疏图（平均度 5）6 跳去重 CTE 仅 0.16ms（small-world frontier 饱和）。真正炸的是稠密图上的路径枚举/结果集膨胀。

### D. 公开基准（严格区分独立 vs 厂商）(SQ4)

- **Neo4j in Action 经典 FoF 表**（vendor-adjacent，复现存疑）：1M 人 d3 RDBMS 30.27s vs Neo4j 0.168s、d4 1543s vs 1.36s、d5 未完成 vs 2.13s。[datascience.SE #77](https://datascience.stackexchange.com/questions/77/is-this-neo4j-comparison-to-rdbms-execution-time-correct)
- **独立复现失败（结论翻转）**：4 跳 MySQL 0.93s vs Neo4j 65–75s。[SO 17773644](https://stackoverflow.com/questions/17773644)
- **joelonsql/graph-query-benchmarks**（PG core 贡献者，独立）：soc-pokec 1.63M 节点/30.6M 边，3 跳 FoF 低度种子 PG 299ms vs Neo4j 528ms（**PG 赢**）；高度种子（结果 103 万行）PG 1782ms vs **Neo4j 崩溃**。[repo](https://github.com/joelonsql/graph-query-benchmarks)
- **AlexPasheva/graph-db-poc**（独立，真实引文图 541K/5.19M）：1 跳 PG 0.82ms vs Neo4j 5.63ms（PG 赢浅查）；**3 跳 p95 PG 26.4s vs Neo4j 341ms ≈77×**；**PageRank PG 354s vs Neo4j 2.2s ≈160×**；Louvain 无 SQL 等价。[repo](https://github.com/AlexPasheva/graph-db-poc)
- **markaicode**（独立，PG-only）：200K/1M 平均度 5，6 跳去重 0.16ms、6 跳路径枚举 6.22ms。[blog](https://markaicode.com/vs/neo4j-vs-postgres/)
- **TigerGraph / Oracle PGX 的 100×** 多为 graph-vs-graph（vs Neo4j），**不是** graph-vs-relational，不能当"图 vs 递归 CTE"证据。[Rusu arXiv 1907.07405](https://arxiv.org/html/1907.07405v1)、[Oracle PDF](https://www.oracle.com/a/tech/docs/ldbc-graph-benchmark-2020-06-30-neo-only-v3.1.pdf)

**收敛结论：** "关系型必输"是伪命题；递归 CTE 的真正不可用区不是"深度 N"，而是 (a) 加权最短/关键路径、(b) 图算法、(c) 稠密真实图上无界深度可达/爆炸半径（≈3 跳起入秒级/几十秒）。稀疏低度图 6 跳仍 <2ms。

---

## Cycle 1 — 云厂商原生 / graph-on-relational 新品类 / 安全攻击路径

### E. 云厂商原生：图式查询能力与粒度 (SQ3) — evidence: official-doc

**结论：没有一个主流云厂商向用户暴露"变深度图遍历"查询语言。**

| 服务 | 暴露图遍历? | 查询语言 | 粒度 |
|---|---|---|---|
| AWS Resilience Hub | 否 | 仅 List/Describe API | 物理资源(ARN)聚合成 app-component |
| AWS Config 高级查询+Aggregator | 否（关系是字段非遍历） | SQL `SELECT` 子集 | configuration item（resourceId/ARN 级） |
| AWS X-Ray / Application Signals | "是图"但**无遍历查询语言** | 无 DSL，`GetServiceGraph` 按时间窗整块返回 nodes+edges JSON | **service 级**（由 trace 派生） |
| Azure Resource Graph | **否，误称** | **KQL-over-Kusto 列式**，关系靠 `join`（硬上限 3 join/query） | ARM 资源级 |
| GCP Cloud Asset Inventory | 否 | search + `queryAssets`(SQL-like) + BigQuery SQL | asset 级 + IAM + relationship 记录 |
| 阿里云 配置审计 / 华为云 Config | 未见图能力（推断） | 类 SQL / 条件查询 | 资源级 |

**关键 nuance 定性（Azure Resource Graph 是不是图）：** 不是。官方"支持的 KQL 语言元素"页枚举算子为 `count/distinct/extend/join/limit/mv-expand/order/parse/project/sort/summarize/take/top/union/where`——**不含 `graph-match`/`make-graph`**。KQL 这门语言在 ADX/Fabric 里确有图语义，但**ARG 这个治理服务没有暴露它们**，跨资源关系只能 `join` 且上限 3。
来源：[Resilience Hub API](https://docs.aws.amazon.com/resilience-hub/latest/APIReference/API_ListAppVersionResourceMappings.html)、[Config query](https://docs.aws.amazon.com/config/latest/developerguide/querying-AWS-resources.html)、[X-Ray GetServiceGraph](https://docs.aws.amazon.com/xray/latest/api/API_GetServiceGraph.html)、[ARG query language](https://learn.microsoft.com/en-us/azure/governance/resource-graph/concepts/query-language)、[GCP CAI SQL](https://cloud.google.com/asset-inventory/docs/query-assets-with-sql)。

### F. graph-on-relational 新品类：是否让"单独部署图库"失效 (SQ7) — evidence: official-doc + vendor

| 产品 | 方式 | 生产就绪? | 消除独立图库? | 硬限制 |
|---|---|---|---|---|
| SQL/PGQ (ISO 9075-16:2023) | 在关系表上声明**只读属性图视图**，`GRAPH_TABLE`+`MATCH` | 标准已定稿，就绪度看引擎；**PG19 GRAPH_TABLE 尚未进稳定版** | 部分（是查询面非存储） | 只标准化**模式匹配，不含图算法**（PageRank/中心性） |
| PuppyGraph | 零-ETL **图查询引擎**跑在 Iceberg/Delta/Postgres 等现有表上，Gremlin+Cypher | 是（商用 GA） | 读/分析场景是（"eliminates ETL pipelines"，vendor claim） | 是**计算层非存储**，写走源系统；定位实时分析/GraphRAG 非 OLTP |
| DuckPGQ | DuckDB 上的 SQL/PGQ + 图算法 | **否，实验/研究**（"not in latest release"，segfault 警告） | 否（嵌入式分析） | 含算法但未稳定 |
| Apache AGE | Postgres 扩展加 openCypher（图存在 PG 内） | 可用（ASF 顶级项目） | 部分 | **默认不建索引**；变长边"do not scale well with path length"；无向关系取不回 |
| Google Spanner Graph | Spanner 上 ISO-GQL，与 SQL 互操作 | **是，GA(2025-02)** | 是（但**须在 Spanner 上**） | 非"跑在你现有 DB 上"，是托管图 DB |
| SQL Server / Fabric graph tables | node/edge 表 + `MATCH`（ASCII-art），`SHORTEST_PATH` | 生产（2017 起）但功能冻结 | 否 | 任意长度**只能在 SHORTEST_PATH 内**且**只返回一条**；无图算法库；跨库图查询不支持；node/edge 表不镜像到 OneLake |

**裁决：** graph-on-relational 真正消除的是 **ETL/双存储** 问题——SQL/PGQ（表上只读视图）与 PuppyGraph（Iceberg/Postgres 零拷贝）让你直接在已有关系/湖仓表上跑 `MATCH` 多跳遍历，**一个独立原生图库在架构上变为可选**。但硬限制正好卡在依赖管理的核心：(a) **图算法不在标准范围**（只有 DuckPGQ 实验版与 PuppyGraph 带算法）；(b) **变长/无界深度路径是普遍软肋**（SQL Server 只在 SHORTEST_PATH 内且只回一条、AGE 变长边不 scale）；(c) 成熟度不均（Oracle 23ai/Spanner Graph/PuppyGraph 就绪，PG SQL/PGQ 与 DuckPGQ 未就绪）。所以：**有界模式匹配 → 可弃独立图库；无界传递闭包 + 图算法 + 图侧写/OLTP → 仍需 Neptune/Neo4j 或 GA 托管图（Spanner Graph）。**
来源：[PG19 property graphs](https://www.postgresql.org/docs/19/ddl-property-graphs.html)、[PuppyGraph docs](https://docs.puppygraph.com/)、[DuckPGQ](http://duckdb.org/community_extensions/extensions/duckpgq.html)、[Apache AGE](https://age.apache.org/)、[Spanner Graph](https://cloud.google.com/spanner/docs/graph)、[SQL Server SHORTEST_PATH](https://learn.microsoft.com/en-us/sql/relational-databases/graphs/sql-graph-shortest-path)。

### G. 安全攻击路径 / 供应链可达性：最强"真需要图"的一档 (SQ8)

**"attackers think in graphs" 出处：** John Lambert（微软）2015 GitHub gist *"Defenders think in lists. Attackers think in graphs. As long as this is true, attackers win."*（primary/practitioner，非同行评审）。[gist](https://github.com/JohnLaTwC/Shared)

| 域 | 工具 | 图引擎 | 降噪宣称 | 独立证据? |
|---|---|---|---|---|
| AD 攻击路径 | BloodHound (SpecterOps) | **Neo4j**（代码可核验）+ Postgres 应用库，Cypher | "shortest path to Domain Admin" | 开源可审计，**图使用是事实非营销** |
| 云 CSPM 有毒组合 | Wiz / Orca / Prisma | 宣称"graph"，**引擎未披露** | 仅定性（"数千告警→少数真实路径"） | **无**，仅厂商文档 |
| SCA 可达性 | Endor Labs / Snyk / Arnica / Checkmarx | function-level call graph | **"up to 92–95%"** 降噪 | **仅厂商宣称** |

**独立/同行评审证据（关键——戳破 90–95% 神话）：** 厂商 90–95% 数字**无任何独立同行评审在该量级复现**。同行评审证实**方向**（多数被标记 CVE 不可达/未部署）但数字更低、口径不同：
- **Pashchenko 等 ESEM 2018**（同行评审）：**~20% 漏洞依赖根本未部署**、不可利用；~81% 一次版本升级可修。[arXiv 1808.09753](https://arxiv.org/abs/1808.09753v1)
- **SōjiTantei (IEICE 2022)**（同行评审）：78 个 npm 漏洞里 **68 个(~87%) 至少有一个"clean client"**（漏洞函数不可达），检测精度 83.3%。[link](https://www.researchgate.net/publication/357502821)
- **Ponta 等 EMSE 2020**（同行评审）：静态+动态可达性判定库漏洞代码是否可达。[Springer](https://link.springer.com/article/10.1007/s10664-020-09830-x)
- **Foo/Veracode 2019（关键警告）：** 静态调用图**双向不可靠**——**~25.5% 静态调用链不可行**（假阳），反射/动态分发导致假阴——即可达性过滤会**静默丢掉真实可达的漏洞**。[arXiv 1909.00973](https://arxiv.org/html/1909.00973v2)

**裁决：** 核心操作（"到 Domain Admin 的最短路"、"哪个暴露入口经有毒组合链到皇冠资产"、"漏洞函数是否从入口传递可达"）**不可约地是图操作**（传递可达/最短路/多跳），展平成列表须无界递归 join 组合爆炸——这是 Lambert 命题的技术硬核，也是 BloodHound 用图是事实而非口号的原因。**但数据本质仍是关系派生的**：每条边（ACL/IAM/网络路由/调用边）都来自关系源，图是**派生分析模型不是不可替代的记录系统**。定档 **"图原生模型、存储引擎不可知（engine-agnostic）"**：比多数分析更强（遍历即产品），但弱于"只有图库能做"（输入是关系型、小直径路径查询多后端可行）。降噪 90–95% 按厂商营销处理，独立结论是方向性成立、量级未证、且静态可达性应用于"排序"而非"静默压制"。

---

## Cycle 2 — 弃用反证(SQ5) / 边生命周期与仲裁(SQ6) / GQL-ISO 标准(SQ9)【9 个子问题全部闭合】

### H. 企业弃用图库 / 图库运维痛点 (SQ5)

**严格门槛下的诚实计数：找到 2 条扎实的具名"图→关系型"迁移，Airbnb 是第 3 条但语义特殊（务必不要误引）。**

| 组织/来源 | From → To | 理由 | 可信度 | 日期 |
|---|---|---|---|---|
| **Capacities**（笔记应用，锚点案例） | Dgraph 自托管 → PostgreSQL(RDS) | "graph tax"：中等数据量下 **CPU 不可预测地高**；被迫昂贵纵向扩展 or 复杂集群。**DB 成本降至 ~1/10、基建省 ~70%**；另发现 Dgraph 多年**静默存下坏 UTF-8/null 字节**，迁到严格 Postgres 才暴露 | 具名公司工程博客 | 2026-01-12 |
| **Sightfull**（业务指标分析） | GraphDB → PostgreSQL | "尽管有种种承诺，我们看到大量缺点与问题…促使我们离开 GraphDB 转向 Postgres"（切换于 2023-07；细节部分付费墙） | 具名公司工程博客 | 2024-11 |
| **Airbnb**（身份图，⚠️语义特殊） | 第三方 **SaaS 图库 → 自托管 JanusGraph+DynamoDB**（**不是**关系型） | SaaS 长尾延迟 P95≈2.1s/P99≈5.0s、需定期手动重启、厂商锁定、无法调优/细粒度权限。结论是"自建图库"而非"放弃图"——**不可当作"Airbnb 弃用图库"引用** | 具名公司工程博客 | 2024/2025 |
| HN 从业者（2 条） | Neo4j/OngDB → Postgres | 佐证性轶事，细节偏薄 | 匿名论坛（低） | 2021–2022 |
| IBM Graph（反-反例） | IBM Graph 退役 → 迁客户**到** JanusGraph | 非"图→关系"，列出以防被误引 | 厂商博客 | 2017 |

**图库运维痛点（多数来自 Neo4j 自家文档，最不可辩驳）：**
- **单主写入天花板**：Neo4j 只有集群 leader（1 台）能接受写，写只能纵向扩展（[Neo4j Spark FAQ](https://neo4j.com/docs/spark/current/faq/)）。
- **分片不成熟**：分片属性库上 `MERGE` "在任何有意义的规模下都很慢"（[Neo4j sharded limitations](https://neo4j.com/docs/operations-manual/current/scalability/sharded-property-databases/limitations-and-considerations/)）。
- **备份沉重**：IO 受限 ≈144GB/hr，在生产节点跑备份可 OOM，需独立服务器（[Neo4j backup](https://neo4j.com/docs/upgrade-migration-guide/current/version-4/tutorials/online-backup-copy-database/)）。
- **超级节点/高扇出爆炸**：亚毫秒遍历被单个膨胀超节点拖成数秒（Neo4j Medium + Airbnb 实测 P99 disproportionate）。
- **查询规划器不可预测**、**JanusGraph 混合索引部分写失败→永久不一致**（[JanusGraph docs](https://docs.janusgraph.org/master/advanced-topics/cdc-mixed-index/)）、**GDS 全内存**、**ETL/双写负担**。

### I. 依赖边生命周期 / 来源仲裁 / "边是否为真" (SQ6) — evidence: official-doc

| 系统 | 边新增 | 边过期 | 多源仲裁 | provenance |
|---|---|---|---|---|
| Dynatrace Smartscape(Grail) | OpenPipeline 抽取；static 继承节点生命周期、dynamic=point-in-time | static 随节点过期、dynamic 受 bucket 保留约束 | 弱（靠 Smartscape ID 去重，无跨源优先级引擎） | 中（dynamic 边显式 point-in-time，可做历史时点） |
| Datadog Request Flow / Service Map | trace 实时构建（derive-on-read） | **隐式**：仅当窗口内有 trace 流过才存在 | 无（唯一来源=trace） | 弱（无显式删除/来源事件） |
| Kiali(Istio) | 实时流量+Istio 配置合成（derive-on-read） | **隐式**：无流量边默认消失；需手动开 "Idle Edges" 才补既往 | 无 | 弱 |
| **ServiceNow CMDB IRE** | `createOrUpdateCIEnhanced` 经识别规则去重 upsert | 显式：按类保留 | **强（标杆）**：识别规则+**reconciliation rules 定权威源**+**data source precedence**（低优先级更新被 `maskedAttributes` 拒） | 强（每属性记 discovery source） |
| **Backstage catalog** | entity provider 从权威源 add/remove | 显式：provider **eager deletion** + processor 停 emit→**orphaning**（mark-and-sweep，`orphanStrategy: delete\|keep`） | 中（"no two providers can output the same entity" 分桶所有权） | 强（`backstage.io/managed-by-location` 记来源） |

**derive-on-read 在两处根本性失效：**（a）**历史时点拓扑**——Kiali 官方明说无流量边默认消失，"Idle Edges"也只是"该窗口是否有 trace"，**无法区分"没依赖"与"当时没流量"**；只有物化存储（Smartscape point-in-time / CMDB / Backstage 的显式 upsert+删除事件）才把"边曾经为真"变成可查事实。（b）**过期与 provenance**——derive-on-read 的过期是隐式副作用，无"被谁删除"事件、无多源仲裁（只有一个来源）。

**裁决：「边是否为真」与 graph-vs-relational 正交。** 边生命周期+provenance+多源仲裁是坐在存储选型**之上**的数据契约层：Backstage 在 **Postgres** 上实现完整生命周期与所有权 provenance（证明图语义+边生命周期不需图库）；ServiceNow IRE 在**关系表**上解决最难的多源仲裁；Dynatrace 的 static/dynamic 语义是**建模决策**与引擎无关。（"数据质量/provenance 才是难点而非存储"无单一厂商原话，是五家架构一致推出的结论，moderate。）

### J. GQL / ISO/IEC 39075:2024 标准与锁定风险 (SQ9)

**标准：** GQL 于 2024-04 发布，是**自 1987 年 SQL 以来 ISO 首个全新数据库语言**（同一委员会 WG3），目标是图数据定义与操作的**跨实现可移植**，与 SQL/PGQ(9075-16:2023) 共享图模式匹配核心 GPML；2026 已出 TC1 勘误。[ISO 76120](https://www.iso.org/standard/76120.html)

| 厂商/产品 | 查询语言 | GQL 一致性 |
|---|---|---|
| Google Spanner Graph / BigQuery | GQL + SQL/PGQ | **Shipped（GA）** |
| Microsoft Fabric Graph | GQL | **Shipped（preview）**，有逐特征一致性表 |
| Neo4j | Cypher（向 GQL 收敛） | **Shipped-aligned 但仍缺部分强制特征**，非纯 GQL 端点 |
| TigerGraph | GSQL V3 | Announced-aligned（吸收 ASCII art+openCypher，无整体一致性声明） |
| Amazon Neptune | openCypher/Gremlin/SPARQL | **None**（参与制定但未发布 GQL 端点） |
| Oracle 23ai | SQL/PGQ + PGQL | **None**（走 SQL/PGQ 而非独立 GQL） |

**锁定风险裁决：** 可移植性今日**基本仍是 aspirational**——无两家能保证同一条 GQL 查询无改动跨引擎运行，Cypher/openCypher/Gremlin/SPARQL/GSQL/PGQL 互不兼容，实现成熟度参差，标准 2026 还在出勘误。**今日选依赖图存储仍等于选一种方言**；把迁移成本压最低的现实做法是**押 openCypher/Cypher 家族**（向 GQL 收敛最快，Neptune 亦可用 openCypher）。
来源：[Spanner GQL](https://cloud.google.com/spanner/docs/graph/iso-standards)、[Fabric conformance](https://learn.microsoft.com/en-us/fabric/graph/gql-conformance)、[Neo4j GQL conformance](https://neo4j.com/docs/cypher-manual/current/appendix/gql-conformance/)、[Oracle SQL/PGQ](https://blogs.oracle.com/database/property-graphs-in-oracle-database-23ai-the-sql-pgq-standard)。

---

## Cycle 3 — 【DoD 成品】按场景分档判定表：依赖管理是否需要图数据库

**一句话答案：** 不是。"依赖关系管理必须用图数据库"是伪命题。只有一档（真 graph-hard：加权最短/关键路径、图算法、稠密图无界传递闭包）是图引擎的硬需求；其余五档用关系型/时序/派生存储即可，且拐点取决于**图的密度而非跳数**。业界用脚投票印证：6 家 APM 仅 Dynatrace 上真图存储、云厂商无一对外暴露图遍历。

### 成品判定表（每档：典型查询 → 是否需图库 → 推荐存储 → 证据等级+出处 → 代表/反例）

| 档 | 典型查询/场景 | 需要图数据库? | 推荐存储 | 代表系统 | 证据等级 |
|---|---|---|---|---|---|
| **① derive-on-read（遥测派生）** | 当前实时服务地图、"现在谁在调谁" | **否** | 时序/指标库（Prometheus）或 trace 派生，无需持久图 | Datadog Service Map、Kiali、Grafana Tempo、New Relic、AWS X-Ray | official-doc（strong）|
| **② 规范化关系表** | 目录、血缘、有界 join、直接父子依赖 | **否** | PostgreSQL / MySQL（边存规范化表，遍历应用侧算） | Backstage、OpenLineage/Marquez、AWS Config、Azure Resource Graph、GCP CAI | official-doc（strong）|
| **③ 图可选 / 可插拔** | 有界模式匹配、少数几跳读、GraphRAG | **否（图是查询面非存储）** | 关系/湖仓表 + SQL/PGQ 视图 或 PuppyGraph 零-ETL 引擎；或图后端可关（DataHub 默认 ES） | DataHub、SQL/PGQ(Oracle 23ai/Spanner/BigQuery)、PuppyGraph | official-doc + vendor（strong/moderate）|
| **④ 图原生模型、引擎不可知** | 攻击路径、可达性、"到 Domain Admin 最短路"、有毒组合 | **需要图"模型"，引擎可关系可图** | 遍历即产品，但边来自关系源；Neo4j / 关系+图层 / 派生 call-graph 均可 | BloodHound(Neo4j 事实)、Wiz/Orca/Prisma(引擎未披露)、SCA reachability | 事实(BloodHound README) + peer-review(降噪方向) |
| **⑤ 真 graph-hard（硬需求）** | 加权最短/关键路径、PageRank/Louvain/中心性、稠密图无界传递闭包/爆炸半径 | **是（确需图引擎或图算法库）** | 原生图库(Neo4j/Neptune) 或 GA 托管图(Spanner Graph)；关系递归 CTE 在此崩 | — | independent-benchmark（strong）|
| **⑥ 真图存储物化（少数选择）** | 可变拓扑 + 可图遍历 + 独立保留期 + 历史时点 | 选真图存储（厂商用脚投票的少数） | 图原生数据湖仓（Grail） | Dynatrace Smartscape/Grail | official-doc（strong）|

### 两条横切注解（emergent Q1 / Q2，并入判定表）
- **【拐点取决于密度而非跳数】** 递归 CTE 的不可用区不是"深度 N"：稀疏图（平均度 5）6 跳去重仍 <2ms；稠密真实图 3 跳即入秒级/几十秒（AlexPasheva 3 跳 p95 26s）。判定表第⑤档的触发条件应写成"**稠密图（高 fan-out）+ 无界深度 + 需路径/算法**"，而非"跳数超过 N"。[independent-benchmark]
- **【derive-on-read vs 物化存储的本质取舍】** 派生模型（①）省去存储与 ETL，但**根本无法回答历史时点拓扑（"上周二谁依赖谁"）和边 provenance/过期**——无流量的边直接消失，分不清"没依赖"与"没流量"。需要历史/审计/边生命周期的场景必须落到物化存储（②③⑥）或数据契约层（SQ6）。[official-doc: Kiali Idle Edges]

### 三条决策规则（把判定表变成可操作的选型逻辑）
1. **默认从关系型起步**（②）——除非命中第⑤档的硬查询，否则关系库 + 规范化边表 + 应用侧遍历（或 SQL/PGQ / PuppyGraph 直接在现有表上做有界遍历）成本最低、无双写 ETL。
2. **"图 vs 关系"和"边是否为真"是两个正交问题**——边生命周期/provenance/多源仲裁是坐在存储之上的**数据契约层**（Backstage/ServiceNow IRE 都在关系库上实现），换图库解决不了数据可信，别把它当选型理由。
3. **真要上图库时，选型=选方言**——GQL 可移植性今日仍 aspirational（仅 Google/MS Fabric shipped 真端点），把迁移成本压最低的现实做法是押 **openCypher/Cypher 家族**。

---

## 反证登记（DoD 要求 ≥3 条独立"图不必要/落地失败"案例）—— ✅ 已达标（≥3，含具名生产迁移）
1. **Backstage** 官方仅用关系库、依赖图应用侧计算——大规模开发者门户不需图库（official-doc）。
2. **DataHub** 官方默认推荐 Elasticsearch 而非 Neo4j、图后端可关闭——图库对元数据依赖非必需（official-doc）。
3. **独立基准翻转**：4 跳 MySQL 0.93s vs Neo4j 65s；joelonsql 3 跳 PG 打赢 Neo4j 且 Neo4j 在高度节点崩溃——"图恒快"被独立实测证伪（independent-repo）。
4. **Capacities（具名生产迁移）**：Dgraph→Postgres，"graph tax"/CPU 不可预测，DB 成本降 ~10×、基建省 ~70%（named-company blog, 2026-01）。
5. **Sightfull（具名生产迁移）**：GraphDB→Postgres，"离开 GraphDB 转向 Postgres"（named-company blog, 2023-07 切换）。
6. **Neo4j 自家文档承认的硬限制**：单主写、分片 MERGE 慢、备份 OOM——图库运维痛点非道听途说（vendor-own-docs）。
_(补充：Airbnb 是"弃用第三方 SaaS 图库"但转向自托管 JanusGraph 而非关系型，属**语义特殊**的反证，已明确标注不可误引为"Airbnb 放弃图库"。)_

**证据强度分层：** official-doc（案例 1、2）＞ named-company production（案例 4、5）＞ vendor-own-docs（案例 6）＞ independent-repo benchmark（案例 3）；均独立于"图库厂商宣称"。

---

## 反证补强候选（存储选型层面，非"上线后弃用"）
_(仍需继续挖真正的"企业弃用图库回退关系型"生产案例 → SQ5。补强候选：Azure Resource Graph 用 KQL-over-Kusto 而非图遍历、且刻意不暴露图算子 = 超大规模云治理场景选择非图引擎的官方证据 official-doc；SQL/PGQ 把图定义为"关系表上的只读视图"= 标准委员会认定图查询可在关系存储上表达 official-doc。)_

---

## 未证实 / 查不到证据（诚实登记）
- Datadog Service Map 的后端存储引擎：官方未公开（derived 属推断）。
- AppDynamics/Splunk flow-map 持久化后端：flow-map 文档未明示。
- Netflix 2019《Building and Scaling Data Lineage》存储实现：官方原文 403 付费墙，未取到可引用细节。
- Wiz/Orca/Prisma CSPM 的后端图引擎类型：官方文档未披露（graph-shaped 但引擎是实现选择）。
- 阿里云/华为云 Config"不暴露图遍历"：基于公开文档只检索到类-SQL、未穷尽验证，属推断（moderate）。
- SCA 90–95% 降噪：无独立同行评审在该量级复现。

---

## Research State（下一轮用）

**✅ 全部 9 个 authoritative 子问题已闭合（strong）：** SQ1 APM 存储、SQ2 OSS 目录/血缘、SQ3 云厂商原生（无一暴露图遍历）、SQ4 graph-hard 查询+基准、SQ5 弃用反证+运维痛点（≥3 独立反证已达标）、SQ6 边生命周期与仲裁（与存储正交）、SQ7 graph-on-relational（图库变可选）、SQ8 安全攻击路径（图原生模型/引擎不可知）、SQ9 GQL/ISO 标准（可移植性仍 aspirational）。

**DoD 三要件状态：**
- (i) 场景分档判定表 —— 6 档已在下文成型，**尚需组装成带每档证据等级+出处的最终成品表**（下一轮主任务）。
- (ii) ≥3 条独立反证 —— **✅ 已达标**（6 条，分层：official-doc / named-production / vendor-own-docs / independent-benchmark）。
- (iii) 未证实清单 —— **✅ 持续维护中**。

**6 档判定表（成型，待组装为成品）：**
| 档 | 场景/查询 | 结论 | 代表 | 证据 |
|---|---|---|---|---|
| ① derive-on-read | 实时服务地图、当前拓扑 | 不需图库；但无历史时点/无边 provenance | Datadog/Kiali/Tempo/X-Ray | official-doc |
| ② 规范化关系表 | 有界 join、目录、血缘 | 关系库即可 | Backstage/Marquez/Config/ARG/CAI | official-doc |
| ③ 图可选/可插拔 | 有界模式匹配、多跳读 | 图库可选（SQL/PGQ、PuppyGraph）；图是查询面非存储 | DataHub/SQL:PGQ/PuppyGraph | official-doc+vendor |
| ④ 图原生模型、引擎不可知 | 攻击路径、可达性 | 需图"模型"，引擎可关系可图 | BloodHound/CSPM/SCA | 事实(BloodHound)+peer-review(降噪方向) |
| ⑤ 真 graph-hard | 加权最短/关键路径、PageRank/Louvain、稠密图无界传递闭包 | **确需图引擎/算法**（拐点取决密度非跳数） | — | independent-benchmark |
| ⑥ 真图存储物化 | 可变拓扑+可遍历+独立保留 | 少数选真图存储（用脚投票） | Dynatrace Grail | official-doc |

**弱点/待补（诚实登记，均已入 FINDINGS）：** Netflix 2019 血缘存储、Datadog/AppD/CSPM 后端未披露；阿里/华为 Config 无图为推断；SCA 90–95% 无独立复现；Sightfull 细节付费墙；"provenance 是难点非存储"为架构推断（moderate）。

**下一轮方向（first-principles）：** DoD 成品判定表已于 cycle 3 组装完成（见上文"【DoD 成品】"章节，6 档 + 3 决策规则 + 2 横切注解）。DoD 三要件现已**全部齐备**：(i) 场景分档判定表✅、(ii) ≥3 独立反证✅（6 条分层）、(iii) 未证实清单✅。主体研究实质完成。剩余 cycle 的边际价值：可选地深挖未证实项（Netflix 2019 血缘存储、Datadog/CSPM 后端——但均已诚实登记为未披露，深挖回报低）。**因此后续 cycle 不再派调研子代理**；保持研究 State 稳定，直到 (a) 收到 FINALIZE guidance → 立即置顶执行摘要，或 (b) 到最终 cycle(19) → 置顶执行摘要+推荐。中间 cycle 若无新 guidance 且无高价值开放项，应避免为"有事做"制造低质产出。
