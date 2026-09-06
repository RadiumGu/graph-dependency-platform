# 对外介绍本项目的提纲（含实施细节版）

> 初版生成：2026-08-31 07:50 UTC
> **本次更新：2026-09-05 14:2x UTC** —— 全文逐节重新核对，所有 `[实测]` 数字已重新查询活图谱，
> 所有 `[代码]` 数字已重新对账仓库。本次共修正 **14 组过期数字**与 **3 处已过时的结论**，
> 变更清单见文末「附 D：本次更新的核对结果」。
>
> 依据：本仓库当前代码（`infra/lambda/etl_*`、`profiles/graph_contract.yaml`、
> `infra/lambda/shared/python/`、`chaos/code/runner/`、`dr-plan-generator/`、`rca/`）
> 与同一时刻对活图谱的实测查询（Neptune `petsite-neptune`，ap-northeast-1）。
>
> 本文所有数字都标了来源：`[代码]` 可在仓库里核对，`[实测]` 为 **2026-09-05 14:2x UTC**
> 对生产图谱的现场查询，`[论文]` 为外部文献。**不要引用没有标记的数字。**
>
> ⚠️ **8/31 到 9/5 之间图谱规模与验证状态都发生了实质变化**（节点 +25%、边 +46%、
> 验证覆盖率从 0 变为非 0）。如果你手上有 8/31 版本的 slide，**必须重做数字页**。

---

## 讲这个项目最大的风险

把它讲成"又一个依赖拓扑工具"——听众脑子里立刻挂上 Datadog / ServiceNow 的标签，
后面全白讲。所以提纲的骨架是：**先用业界范式建立共同语言，指出所有范式都没解的那个缺口，
再讲我们怎么把系统真建起来，最后讲证伪。**

主线听众设定为**技术同行 / 架构评审**（45–60 分钟）。其他听众的删改见第 11 节。

---

## 0. 一句话定位（讲之前先想清楚，不上 slide）

> 「市面上的依赖图都在回答『我看到了什么』。这个项目回答的是『我看到的是真的吗』。」

这句话决定后面每一页的取舍。**不要**开场就说「我们做了一个基于 Neptune 的依赖关系平台」——
那是实现，不是命题。

---

## 1. 开场钩子：一个他们答不出的问题（60 秒）

不要自我介绍，不要讲背景。直接问听众：

> 「你们手上应该都有一张服务依赖图——APM 画的、CMDB 里的、或者架构师手绘的。
> 我想问的是：**你怎么知道上面那条边是真的？**」

停一下，让他们自己想。接着补第二问：

> 「反过来问：如果图上少了一条边，你有任何机制会发现吗？」

**为什么这样开场**：这两个问题任何有系统的人都答不上来，而且答不上来会有点不适——
这个不适就是你接下来所有内容的需求。比任何「痛点分析」页都有效。

---

## 2. 业内主流做法（18 分钟，建立可信度）

本节三层递进，**给没有背景的听众按顺序讲，不要跳**：
2.0 数据从哪来（七条路线各自的系统性盲区）→ 2.1 为什么用图数据库（会被问，先讲掉）
→ 2.2 多源冲突时谁说了算（四种范式，逐个展开）。
时间预算：2.0 = 3 分钟，2.1 = 3 分钟，**2.2 = 12 分钟（本节主体）**。
时间紧时可砍的顺序：2.1 的「什么时候不该用」→ 2.0 的混淆概念表 → 2.2 内部按它自己
末尾那份裁剪清单砍（**四种范式本身不要减成三种**，少任何一种都会让第 3 节的转折失去支点）。

### 2.0 先讲清「依赖关系管理」到底在解什么问题（3 分钟，给没有背景的人）

**不要跳过这一段。** 后面的四种范式是「多个数据源冲突时谁说了算」——那是第二层问题。
听众如果连第一层（数据从哪来、为什么这件事难）都没建立，四种范式听起来就只是四个厂商名字。

#### 一句话定义

> **依赖关系管理 = 维护一份「谁依赖谁」的清单，并且让它一直是对的。**

难点全在后半句。前半句谁都会做——画个架构图、开个 Excel、在 wiki 上记一笔。
问题是系统天天在变，**清单会腐坏，而且腐坏时不报错**。

三个具体的问题场景，任何做过运维的人都会点头：

| 问题 | 没有依赖图时怎么办 |
|---|---|
| 「这台数据库要停机维护，会影响哪些业务？」 | 群里问一圈，等有人想起来 |
| 「这个服务挂了，根因在上游哪一环？」 | 一个个翻监控大盘 |
| 「这个服务能不能下线？还有谁在调它？」 | 不敢下线，于是永远留着 |

#### 业界七条路线：**按「数据从哪来」分类**

这是理解全景的正确切法。不是按厂商，是按**证据来源**——因为每条路线能看到什么、
一定看不到什么，完全由它的数据来源决定。

| # | 路线 | 数据从哪来 | 边的语义是什么 | **系统性看不到什么** |
|---|---|---|---|---|
| 1 | **CMDB / 配置管理数据库**<br>ITIL、ServiceNow CSDM | 各种发现工具 + 人工录入，汇总进一个中心库 | 「配置项之间的关系」，含业务服务到基础设施的映射 | 依赖**人和工具去喂**；没人喂的部分就是空白，而空白看起来和「没有依赖」一样 |
| 2 | **SBOM 软件物料清单**<br>SPDX（ISO/IEC 5962:2021）、CycloneDX（ECMA-424） | 构建期从包管理器抽取 | 「这个制品由哪些库组成」，含传递依赖 | **只管构建期，不管运行时。** 它能告诉你用了哪个版本的 log4j，**不能**告诉你服务 A 在调服务 B |
| 3 | **服务目录 / 开发者门户**<br>Backstage | 人工写 YAML 声明（`catalog-info.yaml`） | 由 `spec.*` **派生**的实体关系，只有一侧声明、另一侧是镜像 | 声明和现实的偏差。**它记录的是「意图」不是「事实」** |
| 4 | **APM / 分布式追踪**<br>OTel service graph、Datadog service map | 应用内埋点产生的 span，按 `parent_span_id` 拼调用链 | 「一次请求经过了哪些服务」，精确到 AWS 资源名 | **没埋点的服务完全不存在。** 且粒度取决于埋点报了什么属性 |
| 5 | **eBPF 零埋点网络观测**<br>Cilium Hubble、Pixie、DeepFlow | 内核 hook（`tcp_v4_connect` / `inet_csk_accept`）抓连接与 L7 | 「谁和谁在通信」，覆盖所有进程无需改代码 | TLS 加密内容、**连接池复用后的调用**、DNS 缓存命中、被 sidecar/NAT 遮蔽的真实两端 |
| 6 | **基础设施即代码的依赖图**<br>Terraform graph、CFN `DependsOn` | 直接读 IaC 源码 | 「创建顺序 / 资源引用」 | **声明期的关系不等于运行时的关系**；且只覆盖被 IaC 管的资源 |
| 7 | **云厂商托管服务**<br>AWS Config relationships、Resilience Hub、Cloud Map、Application Signals | 云控制面自己知道的资源关系 | 资源级的直接/间接关系 | 粒度由厂商定；应用层调用关系通常缺失或很粗 |

**讲这张表时要强调的一句话**：

> 「注意最后一列。**每一条路线都有系统性盲区，而且盲区是由它的数据来源决定的、无法靠
> 『把这条路线做得更好』来消除。** 想补上，只能换一个数据来源。这就是为什么本项目用了
> 五类源而不是一类——不是为了堆料，是因为盲区不重叠。」

#### 两个最容易被混淆的概念，先拆掉

**混淆一：SBOM ≠ 服务依赖。** 这两件事都叫「依赖」，但完全不是一回事：

|  | SBOM 管的 | 本项目管的 |
|---|---|---|
| 依赖的两端 | 制品 → 代码库（log4j 2.14.1） | 服务 → 服务 / 服务 → 数据存储 |
| 什么时候确定 | 构建期，编译完就固定了 | 运行时，随流量变化 |
| 典型用途 | 供应链安全、漏洞影响面（Log4Shell 我中招了吗） | 故障影响面、根因定位、下线评估 |
| 会不会「消失」 | 不会。重新构建才变 | **会。** 流量停了这条边就该失效——这是本项目一半的复杂度来源 |

**混淆二：「有观测」≠「有边」。** 这是本项目的核心立场，也是第 3 节转折点的伏笔：

> 「Datadog、OTel servicegraph、Elastic APM **都没有持久的边实体**——边的『存在』
> 等于查询时间窗内有观测。所以在那些系统里，『这条边消失了』这个问题**根本不成立**，
> 你只能问『最近 15 分钟有没有观测到』。**在我们调研的这批系统里，只有 New Relic 和
> Dynatrace 把边建成了带生命周期的持久对象。**」

理解了这一点，下面四种范式在解什么就清楚了：**它们解的是「多个数据源往同一张持久图里写，
冲突时谁说了算、边什么时候算消失」**。这是第二层问题——只有把边建成持久实体的系统才会遇到。

---

### 2.1 为什么要用图数据库（3 分钟，会被问，先讲掉）

这个问题一定会被问，而且很多回答是错的。**先说结论，再说为什么，最后说什么时候不该用。**

#### 结论：不是为了快，是为了「查询写得出来」

> 「图数据库最常被宣传的卖点是快。**这个理由在我们这里不是主要理由，而且对 Neptune
> 来说说法本身就不准确**（下面会给官方文档）。真实理由是：我们要问的问题在关系数据库里
> **写不出来，或者写出来没人维护得动**。」

#### 我们实际要问的问题长什么样

| 业务问题 | 图查询的形状 |
|---|---|
| 这台数据库停机影响哪些业务？ | 从一个点**反向**走，**不限跳数**，直到走不动 |
| 这个服务的根因可能在哪？ | 从故障点向上游走 1~N 跳，按边属性（延迟、错误率）加权 |
| 哪些服务是单点？ | 找**入度高但没有副本**的点——需要在遍历中做聚合 |
| 这两个服务有没有共同的下游？ | 两条路径求交 |

四个问题的共同点：**路径长度不固定**。

在 SQL 里，「不固定跳数」意味着**递归 CTE**（`WITH RECURSIVE`）或者按最大深度写 N 次自连接。
可以写，但：

- 每加一个约束条件（只走 `dependency=true` 的边、只走 `active=true` 的边）都要改递归体
- 多种边类型混合遍历时，递归体里要 `UNION` 多张表
- 写完之后**没人敢改**——而依赖分析的查询是要天天改的

图查询语言把「变长路径」做成了一个语法原语。同一个「反向可达」在 openCypher 里是：

```cypher
MATCH (target)<-[:Calls|AccessesData*1..5]-(upstream)
WHERE target.name = 'petsite-neptune' AND ALL(r IN relationships(path) WHERE r.active = true)
RETURN DISTINCT upstream.name
```

`*1..5` 就是「1 到 5 跳」。**这才是选图数据库的真实理由：问题的形状和语言的原语对上了。**

#### 关于「索引自由邻接」：一个必须纠正的说法

> 「如果有人拿『index-free adjacency（索引自由邻接）』来解释图数据库为什么快，
> 而你用的是 Neptune，**那个解释是错的**。」

`index-free adjacency` 是 Neo4j 体系的实现特征：每个节点直接存着邻居的物理指针，
所以每跳是 O(1)、和数据总量无关。**但这不是所有图数据库的实现方式，Neptune 就不是。**

AWS 官方文档明确写了 Neptune 的做法
（[How Neptune processes Gremlin queries using statement indexes](https://docs.aws.amazon.com/neptune/latest/userguide/gremlin-explain-background-indexing-examples.html)）：

- Neptune 通过**三个 statement 索引**访问数据：`SPOG`、`POGS`、`GPSO`
- Neptune **假设属性图的 schema 规模不大**——原文：*"Neptune assumes that the size of the
  property graph schema is not large. This means that the number of distinct edge labels and
  property names is fairly low."* 它把 distinct predicate 单独建索引并做 union scan
- **Neptune 没有 OSGP 反向遍历索引**。官方原文列出这个例子：`g.V('v1').in()`
  → 需要的索引 `OSGP` → *"Index does not exist"*

这三条对本项目都有直接后果，值得讲：

| 官方事实 | 对本项目意味着什么 |
|---|---|
| 假设 distinct 边标签/属性名数量少 | 我们 **29 种边标签**，远在假设范围内。**这反过来是「类型必须严格声明」的一个额外理由**——类型爆炸不只是治理问题，会直接打到 Neptune 的性能假设上 |
| 没有 OSGP 反向索引 | `in()` 不带标签时无索引可用。所以 4.4 里 `upsert_edge` 写的是 `__.inE('Calls')` 而**不是** `__.inE()` |
| 可以用 Explain/Profile API 查 predicate 数量 | 有官方手段验证自己有没有踩破这个假设，不用猜 |

**所以正确的说法是**：图数据库比关系数据库更适合这类查询，主要理由是**查询表达力**
（变长路径是原语）而不是普适的性能优势。性能上的对比是**有条件的**——公开的基准里
也有 SQLite 递归 CTE 在某些遍历上打赢图库的结果，「几跳以内、数据量不大」时
带索引的递归 CTE 完全够用。**把「图库一定更快」当成论据，会在懂行的听众面前翻车。**

#### 属性图还是 RDF：为什么选属性图

Neptune 同时支持两种数据模型。选择理由一句话讲完：

| | 属性图（Gremlin / openCypher） | RDF 三元组（SPARQL） |
|---|---|---|
| 边能不能带属性 | **能**，边是一等公民 | **不能**，`(主语, 谓语, 宾语)` 就三个位置 |
| 想给关系加元数据怎么办 | 直接 `.property('latency', 15011)` | 得引入具化节点（reification）把一条边拆成多个三元组 |
| 本项目的需求 | 一条边上要挂 `source` / `dependency_kind` / `first_seen` / `last_seen` / 20 个度量 | 不可行 |

**我们几乎所有的治理机制都实现在边属性上**（溯源、写一次、软删除、证伪状态），
所以属性图不是偏好，是硬需求。

#### Gremlin 还是 openCypher：本项目两个都用

| | Gremlin | openCypher |
|---|---|---|
| 出身 | Apache TinkerPop | Neo4j 的 Cypher 开放规范 |
| 风格 | **命令式**——描述「怎么走」 | **声明式**——描述「要什么」，像 SQL |
| 本项目用在哪 | **写入**（`mergeV` 的 upsert 语义干净） | **查询 / RCA**（更像 SQL，LLM 生成成功率高） |

官方保证这两者可以混用在同一张图上
（[Querying a Neptune Graph](https://docs.aws.amazon.com/neptune/latest/userguide/access-graph-queries.html)：
*"Both Gremlin and openCypher can be used to query any property-graph data stored in Neptune,
regardless of how it was loaded."*）。

#### 什么时候**不该**用图数据库（这一段决定听众信不信你）

主动说这个比讲十条优点更能建立可信度：

- **依赖深度固定且很浅**（只查直接上下游 1~2 跳）→ 关系数据库两次 join 就够，别引入新组件
- **规模小**（几千个节点、几千条边）→ 内存里建邻接表跑图算法可能更简单
- **主要负载是聚合统计**而不是路径遍历（「按团队统计服务数量」）→ 这是 SQL 的主场
- **只需要一次性快照**、不需要维护持久实体的生命周期 → 直接查 APM 的 API 就好，
  本项目一半的复杂度（软删除、TTL、多源仲裁）都是**为了维护持久图**才产生的

> 「如果你的场景落在上面任何一条里，**这个项目的做法对你是过度工程**。
> 我们之所以需要，是因为要同时满足：变长路径查询、边上要挂多源溯源、边有独立生命周期、
> 而且要能对单条边做证伪。这四条同时成立才值得上图数据库。」

---

### 2.2 四种范式：多源写同一张图时谁说了算（12 分钟，建立可信度）

目的**不是**炫耀调研量，是让听众意识到「原来这件事有成熟解法，而我不知道」。
这一节要逐个展开讲——只给一张表，听众得到的是四个厂商名字，不是四种思路。

#### 先说清「范式」这个词指的是什么（1 分钟，不要跳）

**四种范式不是四个产品，是同一道题的四种不同答案。** 题目是这个：

> 有好几个数据源都在往同一张依赖图里写。它们迟早会给出互相矛盾的答案。
> **谁说了算？** 还有——**一条边什么时候算「消失了」？**

先把题目讲具体，否则后面全是抽象名词。用本项目的真实场景举例：

| 冲突长什么样 | 具体例子 |
|---|---|
| 两个源都说有边，但属性对不上 | DeepFlow 先发现 `petsite → petlistadoptions`，X-Ray 后来也报了同一条边，但两边的延迟数字不一样。**留谁的？** |
| 一个源说有，另一个源说没有 | X-Ray 报了 `petsite → dynamodb`，DeepFlow 在同一时间窗里没抓到。**建不建这条边？** |
| 边曾经在，现在没人再报它 | 某个服务缩到 0 副本，流量停了。图里那条 `Calls` 边**是该删掉、还是该留着标记为失效？** |

第三行是最难的，也是四种范式差别最大的地方。因为**「没有观测到」和「不存在」是两件事**：
可能真的没调了，也可能只是这一轮采集漏了、采集器挂了、或者流量恰好在采样间隔之间。

**讲法提示**：讲到这里可以停一下问听众「你们现在的架构图，上一次删掉一条线是什么时候？」
——绝大多数团队的答案是「从来没删过」，这正是这道题存在的证据。

四种答案，先用一句话各自表明立场：

| # | 范式 | 代表 | 它的立场一句话 |
|---|---|---|---|
| **A** | 属性级规则仲裁 | ServiceNow IRE | 「**事先规定每个字段谁有权写。** 冲突时按优先级把低优先级的写入挡掉」 |
| **B** | 声明式单一权威 + 派生关系 | Backstage | 「**不让冲突发生。** 每个实体只能有一个主人，别人不许写」 |
| **C** | 幂等 upsert + 全量时间戳收敛 | Cartography (CNCF) | 「**不裁决。** 每轮给写过的东西盖时间戳，这轮没被盖到的就是消失了，删掉」 |
| **D** | 持久实体 + 显式 TTL | New Relic / Dynatrace | 「**边是有生命周期的对象。** 每类边自己声明能活多久，到期自动失效」 |

**这四种可以分成两组，分组比记名字重要**：

- **A、B 解的是「防冲突」**——多个源同时在写，怎么不打架。
- **C、D 解的是「管消失」**——没人再报它了，这条边怎么办。

一个成熟系统通常两组都要有。本项目也是两组都做了（下面每段末尾会对上）。

#### 范式 A：属性级规则仲裁（ServiceNow IRE）——3 分钟

**它在解什么问题。** CMDB 的典型处境：同一台服务器被 5 个发现工具报上来——网络扫描、
Agent、云 API、虚拟化平台、还有人手工填的。5 份数据都自称是这台机器，字段互相冲突。
IRE（Identification and Reconciliation Engine，识别与调和引擎）是 ServiceNow 在
**写入 CMDB 之前**必经的一道关卡。

**机制：三件事按顺序做。**

| 步骤 | 它在回答什么 | 怎么做 |
|---|---|---|
| 1. **Identification**（识别） | 「这条报上来的记录，是不是库里已有的那一条？」 | 按 CI 类配置的**识别规则**去匹配 |
| 2. **Reconciliation**（调和） | 「这个数据源，有没有权更新这个字段？」 | 按**调和规则**的授权表 + 优先级判定 |
| 3. **Data source rules**（数据源规则） | 「这个源允不允许**新建**这个类的记录？」 | 不允许时 insert 直接失败 |

**第 1 步的识别规则怎么写**，官方定义是：一条识别规则对应一个 CI 类，包含
**一个 CI identifier 加一条或多条 identifier entry**，每条 entry 定义一组
**criterion attributes（判定属性）并带自己的优先级**——先用优先级最高的那组属性去匹配，
匹配不到再降级用下一组。
（[Identification rules](https://docs.servicenow.com/bundle/rome-release-notes/page/product/configuration-management/concept/c_IdentificationRules.html)）

这里有两个官方细节，对本项目的 §4.5「身份键」有直接参考价值：

- 匹配可以基于 CI 自己的字段（**field-based**），也可以基于它的关联列表（**lookup-based**，
  比如序列号、网卡）。但官方最佳实践明确写着：*"The use of lookup identifier entry is
  **highly discouraged** as it can reduce performance."* 也就是说**用别的实体来确定自己的身份，
  是能做但要避免的**——这和我们 §4.5 的结论一致。
  （[Effective usage of CMDB Identification](https://docs.servicenow.com/bundle/madrid-security-management/page/product/configuration-management/concept/best-practices-id-reconcile.html)）
- **reference 字段不能当 criterion attribute**——身份不能建立在指向别人的引用上。

如果识别阶段发现了重复：*"If IRE detects any duplicate CIs based on any class identifiers,
**the payload is rejected and processing stops**."* ——**整个 payload 被拒、处理停止**，
不是「挑一条写进去」。
（[IRE error messages](https://docs.servicenow.com/bundle/quebec-governance-risk-compliance/page/product/configuration-management/reference/id-engine-error-messages.html)）

**第 2 步的调和规则是这个范式的核心。** 官方定义一句话说清：

> *"A reconciliation rule specifies class attributes that discovery sources are authorized to
> update, and **prevents unauthorized discovery sources from overwriting the attributes' values**.
> A reconciliation rule also specifies the prioritization among multiple discovery sources."*

所以它的粒度是 **(CI 类, 属性, 数据源) → 是否授权 + 优先级**。规则可以定在父类也可以定在子类。
更值得讲的是官方对**没有规则时**会发生什么的描述：

> *"Without reconciliation rules, discovery sources are **allowed to overwrite each other's
> updates** to attribute values."*

**默认行为是互相覆盖。** 也就是说 IRE 提供的是机制，纪律要人去配——这一点后面会再出现。
（[Reconciliation rules](https://docs.servicenow.com/bundle/paris-servicenow-platform/page/product/configuration-management/reference/r_ReconciliationRulesPrinciples.html)）

**被挡掉的写入去哪了：** 不是静默丢弃。IRE 的输出 payload 里有一个 `maskedAttributes` 字段，
列出这次被规则挡住、没有写进去的属性名。一位用户在社区贴出了对照实测——同一份输入数据跑两次，
唯一差别是调和规则的 Attributes 字段填不填：留空那次 `maskedAttributes` **列出了它想更新的
4 个字段**（说明全被挡了），显式列出全部属性那次 `maskedAttributes` **为空**（全部放行）。
`[社区实测，非官方文档]`
（[Attributes field in Reconciliation rules in IRE](https://www.servicenow.com/community/itom-forum/attributes-field-in-reconciliation-rules-in-ire-does-it-actually/m-p/1025487)）

> **这个例子值得讲**：一个「留空 = 全部授权」的直觉，实测是「留空 = 全部拒绝」。
> 语义拿不准的时候，**要有一个地方能看到被挡掉了什么**——`maskedAttributes` 就是那个地方。
> 本项目对应的设计是 §4.6 契约门禁的三种模式（`off`/`warn`/`enforce`）里的 `warn`：
> 不阻断，但把每次违规打进日志。**没有可观测的仲裁 = 无法调试的仲裁。**

**这个范式最大的坑，和本项目的核心论点是同一件事。** 社区里反复出现的一条：

> *"Reconciliation rules are **only enforced when IRE is used**. If updates are coming through
> transform maps without enabling 'Use IRE', or via scripts/direct updates, then **these rules
> can be skipped**."* `[社区，非官方文档]`

也就是说：规则配得再完整，**只要有一条写入路径绕过了 IRE，这些规则就等于不存在**。
这和我们 §4.6 那句结论字面上不同、机制上完全一样——**一份没有门禁去读的声明，等于没有声明。**
讲到这里可以点一句：「这不是 ServiceNow 做得不好，这是**声明与执法分离**这个架构的固有代价。」

**代价。** 规则数量是 `类 × 属性 × 数据源` 的量级，需要专人长期维护；规则本身也会漂移
（父类和子类的规则打架、过滤条件互相重叠）。这套东西适合有专职 CMDB 团队的组织。

**本项目抄了什么。** 抄了「属性级授权」，但**刻意做成例外清单而不是全表**：
`profiles/graph_contract.yaml` 里的 `node_attr_authority` 只声明少数几个
「只有某个源有权写」的属性（比如 `Microservice` 的 `az`、`fault_boundary`、
`recovery_priority` 只认 `aws-etl`），其余属性开放。理由和取舍在 §4.6
「属性权威表：为什么是例外清单而不是白名单」里详讲。`[代码]`

#### 范式 B：声明式单一权威 + 派生关系（Backstage）——3 分钟

**它在解什么问题。** Backstage 是 Spotify 开源、现在在 CNCF 的开发者门户。
它的立场和 IRE 相反：**不去裁决冲突，而是让冲突在结构上不可能发生。**

**机制：写入权按「桶」独占。** 每个 *entity provider*（实体提供方）负责从一个外部权威源
拉数据，并且**独占**它产出的那批实体。官方原文：

> *"The database always keeps track of the set of entities that belong to each provider;
> **no two providers can try to output the same entity**."*

两个 provider 不可能产出同一个实体——这一句就是整个范式。没有优先级表，因为没有争用。
（[The Life of an Entity](https://backstage.io/docs/features/software-catalog/life-of-an-entity)）

**数据流三段，各段职责不同**（这三个词在 Backstage 语境里有精确含义，讲的时候要区分开）：

| 阶段 | 做什么 | 关键点 |
|---|---|---|
| **Ingestion**（摄取） | provider 从外部源拿到「未处理实体」 | 只做最粗的校验（有没有 `kind`、`metadata.name`）；**精细校验规则在这一步还没生效** |
| **Processing**（处理） | processor 循环反复访问每个实体，从 `spec.*` **派生出关系**、也可能产出新实体或错误 | 关系被单独存到 relations 表 |
| **Stitching**（缝合） | 把处理产出拼成最终实体，**包括别人指向我的关系** | 缝合逻辑是固定的、不可扩展 |

**关系是派生的，不是写进去的。** 你在 `catalog-info.yaml` 里只写一侧（比如
`spec.dependsOn`），processor 会派生出双向的一对关系，另一侧是镜像。
这一点和本项目不同：**我们两端都是独立实体、边本身是持久对象**（§4.1 会讲）。

**边「消失了」怎么办——这里有两条完全不同的路径，很容易讲混。**
（*订正：本提纲上一版把这两条混成了一条，写作「provider 不再产出 → 标 orphan」，这是错的。*）

| 谁不再产出它 | 后果 | 官方语义 |
|---|---|---|
| **provider**（摄取层） | **Eager deletion（立即删除）** | 实体和它派生出的整棵子树立即 purge——前提是那些子节点不会因为别的父节点而存活 |
| **processor**（处理层） | **Orphan（孤儿化）** | 父实体不再产出这个子实体 → 那条内部边被切断 → 若再无别的边指向它，它就成了孤儿 |

孤儿化的具体表现，官方写得很细：

- stitching 会给它注入注解 `backstage.io/orphan: 'true'`
- 实体**不会**因此被移出目录，会一直留着，直到被显式删除、或者
  配置了 `orphanStrategy: delete`（**这是默认值**）自动清掉、或者被原来的父节点「认领」回去
- 想保留孤儿，要显式配 `catalog.orphanStrategy: keep`

官方给的典型场景很好讲：**有人离职，LDAP 里没有这个人了，于是目录里留下一个孤儿 `User` 实体。**
另外官方特别说明：**文件被删掉、或者文件损坏读不出来，不算孤儿化**——那属于硬错误，
会标在实体上提示所有者，处理流程照常继续。

**还有一个诚实到值得引用的设计权衡。** provider 报告变化有两种方式：
*full mutation*（给全量清单，系统自己算差集，从而知道谁该删）和
*delta mutation*（只报增删）。官方对后者的说明是：

> *"A delta mutation avoids the memory problem, but **cannot guarantee that events are never
> missed**. If your Backstage instance is down when a DELETE event arrives, the catalog ends up
> in an **inconsistent state**."*

**这一句是整个范式 B 里最该讲给听众的。** 它把「增量同步 vs 全量对账」的取舍讲透了：
增量省资源，但你系统宕机期间错过的那条删除事件，**永远不会自己回来**。
（[Incremental Entity Providers](https://backstage.io/docs/features/software-catalog/external-integrations/incremental-entity-providers/)）

还有一条 fail-safe 值得一提：processing 阶段如果产出了错误，那个有错的版本**不会**替换掉
之前无错的最终实体——**坏数据不许覆盖好数据**。

**代价。** 它记录的是**意图**，不是事实：`catalog-info.yaml` 是人写的。
更关键的是——**单一权威这个前提在观测数据上根本不成立**。DeepFlow 和 X-Ray 会
合法地、正确地报告同一条边，它们不是在打架，是在互相印证。
所以本项目不能抄这一条，这是范式选择上的硬分歧，不是我们偷懒。

**本项目抄了什么。** 抄了两条：**坏数据不许覆盖好数据**（§4.4 的写入路径在校验失败时
不写，而不是写一个残缺版本）；以及**「增量不能保证不丢事件」这个认识**——这正是本项目
除了增量 ETL 之外还要做全图三元组普查的原因（那次普查查出了 183 条源端点错误的边）。

#### 范式 C：幂等 upsert + 全量时间戳收敛（Cartography）——3 分钟

**它在解什么问题。** Cartography 是 Lyft 开源、现在在 CNCF 的工具，把云资产及其关系
拉进 Neo4j。它面对的处境和本项目最像：**多个采集模块、周期性全量拉取、写同一张图**。
它的答案最简单，也最值得抄。

**机制：一个模块 = 一次 sync = 四步。** 官方原文的结构是
`get → transform → load → cleanup`。核心在最后两步。

**`update_tag`：一次同步一个标签。** 官方定义：cartography 的全局配置带一个 `update_tag`
属性，值是**本轮 sync 启动时的 Unix 时间戳**；所有 intel 模块把**所有节点和所有关系**的
`lastupdated` 字段都设成这个值。
（[Writing intel modules](https://github.com/cartography-cncf/cartography/blob/master/docs/root/dev/writing-intel-modules.md)）

**load 生成的真实 Cypher**（官方文档里给出的生成语句，可以直接贴到 slide 上）：

```cypher
UNWIND $DictList AS item
  MERGE (i:AWSEMRCluster{id: item.Id})
  ON CREATE SET i.firstseen = timestamp()
  SET i.lastupdated = $lastupdated,
      i.arn = item.ClusterArn
```

**讲这段的时候要点出对应关系**：这就是幂等 upsert——`MERGE` 保证「有就更新、没有就建」，
`ON CREATE SET firstseen` 保证首次时间只写一次，`SET lastupdated` 每轮刷新。
**和我们 §4.4 里 Gremlin 的 `coalesce(__.V()..., __.addV(...))` 是同一个动作、不同方言。**
`firstseen` 对应我们的 `first_seen`，`lastupdated` 对应我们的 `last_seen`。`[代码]`

**cleanup：这一步就是「边消失了怎么办」的答案。** 官方原文：

> *"We now need to delete nodes and relationships that no longer exist, and we do this by
> **removing all nodes and relationships that have `lastupdated` NOT set to the `update_tag`
> of this current run**."*

一句话：**这一轮没被盖上时间戳的，就是消失了，删掉。** 不需要任何源之间的裁决——
「谁对」这个问题被换成了「谁最近说过」。

官方还提到一个细节，很能说明这个设计的分量：cartography 会**自动给 `lastupdated` 建索引**，
理由原文就是 *"this is used to enable faster cleanup jobs"*。**收敛是一等公民，不是清理脚本。**

**三个安全阀，这是这一段最有含金量的部分**（也是显示你真读过源码的地方）：

| 安全阀 | 它防的是什么 | 官方语义 |
|---|---|---|
| **`scoped_cleanup`（默认 True）** | **防「同步一个账号，删光其他账号」** | 只删除「连接在本轮正在同步的那个 sub-resource（AWS 账号 / GCP 项目）上」的陈旧节点 |
| **节点和关系要分别删** | 防「节点还在但关系变了」 | 官方自问自答：既然已经 `DETACH DELETE` 了节点，为什么还要单独删关系？——*"There are cases where the node may continue to exist but the relationships between it and other nodes have changed."* |
| **`cascade_delete`（默认 False）** | 父节点陈旧时连带删子节点 | 且本轮被重新挂上的子节点（`lastupdated` 匹配本轮）**受保护不删** |

> **第一个安全阀要重点讲。** 「按时间戳全量收敛」听起来干净，但**它默认会误删**——
> 如果不限定作用范围，同步账号 A 的那一轮会把账号 B 的所有数据都判成「本轮没被盖到」。
> 这是本项目做边过期收敛时踩过的同一类问题，也是我们把 `only_labels`
> 做成参数、把基准时间戳 `round_ts` 由调用方统一传入的原因。`[代码]`

**多源冲突它怎么处理：根本不裁决。** 官方明确鼓励多个 intel 模块写同一种节点，给了两种模式：

- **Simple Relationship**：A 只知道 B 的 ID，那就只定义关系，`MATCH` 到已有的 B 连上去。
- **Composite Node**：A 还知道 B 的额外字段（例子是 EC2 Instance API 能提供 EBS 卷的
  `deleteontermination`，而 EBS API 本身没有），那就定义一个 `BASchema`，
  **MERGE 到同一个 label 上，让属性从多个源累积**。

官方立场原文：*"each module should 'offer its own perspective' on the data."*
所以范式 C 的完整答案是：**属性上后写覆盖 + 结构上按轮次全量重算。**

**代价，也是本项目和它分道的地方。** 它的收敛动作是**真删**（`DETACH DELETE`）。
删完之后，**「这条边消失了吗」这个问题就问不出来了**——图里不再有那条边的任何痕迹，
你只能对比两次快照。而本项目一半的价值就建立在能回答这个问题上。

**本项目抄了什么、改了什么。**

| Cartography | 本项目 | 为什么改 |
|---|---|---|
| `MERGE` + `ON CREATE SET` | Gremlin `coalesce` + `property(single,...)` | 方言不同，语义一致 |
| `lastupdated` = 本轮 `update_tag` | `last_seen`（契约里 `timestamp_field: last_seen`） | 一致 |
| cleanup：`lastupdated ≠ 本轮` → `DETACH DELETE` | `deactivate_stale_dynamic_edges()`：超期 → **置 `active=false`** | **我们要能回答「这条边消失了吗」，所以不能删** |
| 收敛按「本轮同步范围」限定 | 收敛按**边类型**限定，且全轮共用一个 `round_ts` 基准 | 同一轮里所有判定必须用同一个 cutoff，否则同一批边会因执行先后落在不同基准上 `[代码注释]` |

#### 范式 D：持久实体 + 显式 TTL（New Relic / Dynatrace）——3 分钟

**它在解什么问题。** 前三种范式里，边的「存在」都是某种副产品：IRE 里边是 CI 之间的关系记录，
Backstage 里边是从声明派生的，Cartography 里边的存在等于「上一轮被盖到了」。
范式 D 是唯一**把边本身当成有生命周期的一等对象**来建模的：它有开始时间、有结束时间、
有明确的过期策略。**这也是本项目的选择。**

**New Relic：关系合成规则是声明式 YAML。** 一条真实规则长这样
（官方文档原样，可以直接上 slide）：

```yaml
relationships:
  - name: extServiceCallsExtPixieDns
    version: "1"
    origins:
      - OpenTelemetry
    conditions:
      - attribute: entity.type
        anyOf: [ "PIXIE_DNS" ]
    relationship:
      expires: PT75M              # ← 这条边的存活时长
      relationshipType: CALLS     # ← 只能取自闭集
      source:
        buildGuid:                # ← 身份从遥测字段派生出来
          account: { attribute: accountId }
          domain:  { value: EXT }
          type:    { value: SERVICE }
          identifier:
            fragments:
              - attribute: service.name
            hashAlgorithm: FARM_HASH
      target:
        extractGuid: { attribute: entity.guid }
```

四个官方事实，每一条都对应本项目的一个设计：

| New Relic 官方事实 | 对应本项目 |
|---|---|
| **`expires`**：*"the duration for which the relationship should exist **if it is not reported within that timeframe**"*；ISO-8601 格式，**默认 75 分钟**，允许范围 **10 分钟 – 72 小时（含）** | 我们的 `expires_seconds`，按边类型逐个声明 |
| **`relationshipType` 是闭集**：只能取 `CALLS`/`CONTAINS`/`HOSTS`/`SERVES`/`IS`/`OPERATES_IN`/`CONNECTS_TO`/`BUILT_FROM`/`MEASURES`/`PRODUCES`/`CONSUMES`/`MANAGES`/`OWNS`/`TEST` 共 14 种 | 我们契约里声明的 **29 种边类型**，`assert_edge_type` 在运行时执法（§4.6） |
| **`origins` 也是闭集**：APM Metrics / Infrastructure Agent / Metric API / Pixie / Prometheus / OpenTelemetry / OnHost / Browser / Mobile / Network Monitoring | 我们契约里的 `sources` 词表，**12 个声明源**（§4.6） |
| **身份从遥测派生**：三种解析器 `extractGuid`（直接取字段）/ `buildGuid`（用 account+domain+type+identifier 片段拼、可 `FARM_HASH` 哈希）/ `lookupGuid`（查候选表） | §4.5「身份键必须由不变量派生，不能用 name」——**同一个结论，独立得出** |

官方自己给了一句诚实标注，讲的时候带上会更可信：
*"the relationship synthesis mechanism is **currently in the experimental phase**."*
（[Relationship Synthesis](https://github.com/newrelic/entity-definitions/blob/main/docs/relationships/relationship_synthesis.md)）

**Dynatrace：把「时间」直接建进节点和边。** Smartscape on Grail 的模型
（[Smartscape on Grail](https://docs.dynatrace.com/docs/discover-dynatrace/platform/grail/smartscape-on-grail)）：

- 节点**没有单一时间戳**，而是两个：`lifetime.start`（*"the first time when the node was
  discovered"*）和 `lifetime.end`（*"the time when the node was last observed"*）。
  前者不变，后者随每次 upsert 持续更新。
- 查询按**时间窗重叠**判定：`lifetime.end` 是昨天的节点，查「最近 2 小时」就查不到它。
  ——注意这是**软失效**：数据还在，只是不落在你的窗口里。
- **边分两类，这一条对本项目最重要：**

| Dynatrace 边类型 | 官方语义 | 典型来源 |
|---|---|---|
| **Static** | *"the edge inherits the node's lifetime"* | 配置事实（磁盘被配置挂在某台主机上） |
| **Dynamic** | *"the edge is recorded for a specific point in time"* | 监控信号（trace 表明服务 A 调了服务 B） |

> **这个划分和本项目的 `dependency_kind` 是同一件事，独立得出。** 我们的
> `dynamic` 边来自观测（DeepFlow/X-Ray/NFM），会过期；`static` 边来自配置
> （CloudFormation、服务声明），不过期。**活图谱实测：依赖边里 dynamic 77 条、static 27 条，
> 外加 `inference` 11 条（AI Agent 的调用，Dynatrace 那套划分里没有这一档）。**
> `[实测 2026-09-05]`

- **保留期固定 35 天**：*"nodes whose `lifetime.end` is older than 35 days will be deleted,
  including all static edges. Dynamic edges will be cleaned up after 35 days as well."*
  ——这是**硬删除**，和上面的「时间窗软失效」是两重不同的语义。
- 还有两条 upsert 语义，值得作为「静默失败」的反面教材讲：
  - **没有 delete-by-omission**：*"Removing a field from the Smartscape event does not remove
    it from the node."* 要删字段必须显式传 `null`。
  - 不在允许写入清单里的字段：*"Fields outside this allowlist are **silently dropped at upsert.
    No error is returned**."* ——**静默丢弃、不报错**。这正是 §4.6 讲的那类问题。

**代价：TTL 本质上是一个猜。** 定太短，活着的依赖被误判为消失；定太长，消失的依赖继续挂在图上。
而且 TTL 和采集频率是耦合的——采集间隔一变，原来合适的 TTL 就不合适了。
**这个范式没有消除不确定性，它把不确定性变成了一个可以讨论、可以调、可以写进配置的数字。**
这本身就是进步：从「我不知道这条边还在不在」变成「我声明这类边 30 分钟没人报就算失效」。

**本项目抄了什么，附实测数字。**

| 项 | 本项目取值 | 与 New Relic / Dynatrace 对照 |
|---|---|---|
| 声明了 TTL 的边类型 | **26 种里只有 5 种**：`Calls`、`AccessesData`、`DependsOn`、`InvokesVia`、`PublishesTo` | 其余 21 种是配置事实，对应 Dynatrace 的 static edge |
| `Calls` 的 TTL | **1800 秒（30 分钟）** | 比 New Relic 默认的 75 分钟**更紧**，但落在它允许的 10 分钟–72 小时区间内 |
| 其余 4 种的 TTL | **21600 秒（6 小时）** | —— |
| 第二重语义：硬保留 | `Calls` 另有 `retention_seconds` = **604800（7 天）** | 对应 Dynatrace 的 35 天固定保留（§4.7 详讲两种语义为何刻意不混） |
| 到期动作 | **置 `active=false`，不删** | Cartography 删、Dynatrace 35 天后删；我们不删，因为要能回答「它消失了吗」 |

`[代码：profiles/graph_contract.yaml → edge_types]`

**一个可以现场亮出来的实测数字**：活图谱里 19 条 `Calls` 边，**14 条当前是 `active=false`、
5 条 `active=true`**。也就是说图谱正在明确告诉你「这 14 条依赖已经不再被观测到了」——
而它们**仍然在图里，带着 `first_seen`、`source` 和完整属性**。
换成范式 C，这 14 条已经被删了，这句话就说不出来。`[实测 2026-08-31]`

（诚实补充：另有 51 条依赖边的 `active` 属性是 `null`——它们早于这个属性引入，
没有做回填。这属于已知的存量问题，§4.8 存量清理里有对应说明。）`[实测 2026-08-31]`

#### 四种范式横向对照（讲完四段后回到这一页）

| | A · ServiceNow IRE | B · Backstage | C · Cartography | D · NR / Dynatrace |
|---|---|---|---|---|
| **多源冲突怎么办** | 按 (类,属性,源) 授权 + 优先级仲裁 | 结构上禁止：一个实体只有一个主人 | 不裁决，后写覆盖、属性累积 | 各规则独立合成，按类型收敛 |
| **边「消失」怎么判定** | 不专门处理（靠数据源新鲜度规则） | provider 删 → 立即删；processor 不再产出 → 孤儿 | 本轮 `lastupdated` 没被盖到 | TTL 到期未被再次上报 |
| **消失后的动作** | —— | 默认删（`orphanStrategy: delete`） | **`DETACH DELETE` 真删** | NR：关系不再存在；DT：软失效 + 35 天硬删 |
| **能否事后追问「它消失了吗」** | 有 Data Source History 可查 | 孤儿注解保留期间可以 | **不能**（已删除） | **可以**（生命周期字段还在） |
| **规则写在哪** | 平台配置表（人工维护） | 代码里的 provider + YAML 声明 | 代码里的 schema dataclass | **声明式 YAML 规则文件** |
| **主要代价** | 规则数量爆炸、需专职团队 | 记录的是意图不是事实 | 真删，且不限定范围就会误删 | TTL 是猜的，且与采集频率耦合 |

#### 然后抛那个让人意外的事实（整节高潮）

> **Datadog、OTel servicegraph、Elastic APM 都没有持久的边实体。** 边的「存在」等于
> **查询时间窗内有观测**。所以在这些系统里，「这条边消失了」这个问题**根本不成立**——
> 你只能问「最近 15 分钟有没有观测到」。
>
> 在我们调研的这批系统里，**只有 New Relic 和 Dynatrace 把边建成了带生命周期的持久对象**。

**为什么这样安排**：听众里一定有人在用 Datadog。告诉他「你用的那个工具在这个维度上根本
没建模」比说「我们更好」有力一百倍——而且这是事实，不是贬损。

**接着补一句，防止听成贬损**：「这不是缺陷，是定位不同。它们要回答的是『现在怎么了』，
时间窗模型对那个问题是够的。**只有当你要回答『这个依赖还在不在』的时候，
你才需要把边建成对象。**」

#### 本项目抄了哪几条、哪几条刻意没抄

给自己留台阶：**这四种范式都是好答案，都该抄。** 但要说清抄了什么、以及**哪里刻意分歧**——
后者才是听众判断你有没有真想过的地方。

| 范式来源 | 本项目对应 | 关系 |
|---|---|---|
| IRE 属性级授权 | `node_attr_authority`（例外清单）、`edge_write_once_attrs`（`source`/`dependency_kind`/`first_seen` 写一次） | **抄，但简化**：例外清单而非全表 |
| IRE「规则只在走 IRE 时生效」 | §4.6 的 `assert_*` 门禁必须在写入路径上被调用 | **抄的是教训**：声明与执法必须绑在一起 |
| Backstage 单一权威 | —— | **刻意不抄**：多源互证是本项目的前提，不是缺陷 |
| Backstage「坏数据不覆盖好数据」 | §4.4 校验失败即不写 | **抄** |
| Backstage「增量不保证不丢事件」 | 除增量 ETL 外另做全图三元组普查 | **抄的是认识**（那次普查查出 183 条错源边） |
| Cartography `MERGE` + `lastupdated` | Gremlin `coalesce` + `last_seen` | **抄，方言不同** |
| Cartography cleanup 真删 | 改为置 `active=false` | **刻意分歧**：要能回答「消失了吗」 |
| Cartography 收敛须限定范围 | `only_labels` + 全轮统一 `round_ts` | **抄** |
| New Relic 关系 `expires` | 按边类型声明的 `expires_seconds` | **抄** |
| New Relic 关系类型闭集 | **29** 种边类型 + `assert_edge_type` 运行时执法 | **抄，且加了执法** |
| New Relic 身份从遥测派生 | §4.5 身份键从不变量派生 | **独立得出同一结论** |
| Dynatrace static / dynamic 边 | `dependency_kind`（实测 dynamic **77** / static **27** / **inference 11**） | **独立得出同一结论，且多出一档**——Dynatrace 只有 static/dynamic 两档，我们为 AI Agent 的调用加了第三档 `inference` |
| Dynatrace 软失效 + 硬保留两重语义 | `expires_seconds` 与 `retention_seconds` 刻意分开（§4.7） | **抄** |

**这一节如果只能记一句**：

> 「这四种范式解的都是『多个源写同一张图时谁说了算』。它们都是好答案。
> **但注意——它们没有一个在回答『这些源合起来对不对』。**」

——这句话直接接第 3 节。

#### 时间不够时的裁剪顺序（从先砍到最后砍）

1. **范式 A 的识别规则细节**（lookup-based、criterion attributes）——那是 CMDB 专门知识，
   只留「(类,属性,源) → 授权+优先级」和「绕过 IRE 规则就失效」两句
2. **范式 B 的 delta/full mutation** ——但「增量不保证不丢事件」那一句要留
3. **横向对照表**——如果四段都讲完了，这张表是复习，可省
4. **绝对不能砍**：范式 C 的 `scoped_cleanup`（它解释了本项目为什么那样做收敛）、
   范式 D 的 static/dynamic 划分（§4.1 和 §4.7 都建立在这个区分上）、以及 Datadog 那个高潮

---

## 3. 缺口：四种范式都没解的那件事（3 分钟，全场转折点）

> 「注意这四种范式的共同点：**它们都在裁决『不同数据源之间谁对』，
> 没有一个在验证『这些源合起来对不对』。**」

两个学术硬证据，把「这是难题不是我们没做好」立住：

- **arXiv:2608.04413**（eBPF 依赖发现）自己写着：只在自建 20 服务 testbed 上「还原了已知的
  ground-truth 拓扑」——**这只证明流水线能还原已知图，不证明能发现未知依赖**，
  并坦承对罕见代码路径没有覆盖率证据。`[论文]`
- 想拿公开数据集当真值也是陷阱：**ICPE'24 的 Casper** 量化出 Alibaba 2021 trace 严格按官方
  规范只能重建 **58.32%**，而且遵守规范的工具会**静默生成大小形状都错的 trace**。`[论文]`

**结论句**：「『你怎么知道没漏边』在学界仍是开放问题。所以我们没有去做第五个数据源——
我们去做了证伪。」

---

## 4. 系统是怎么建起来的（39 分钟满讲，本次新增的主体）

**时间不够时的裁剪顺序**（从先砍到最后砍）：4.8 存量清理 → 4.9 部署形态 → 4.3 ETL 细节
→ 4.7 生命周期 → **4.11.3 收束句 → 4.11.2 的 9/5 新增件列表** → 4.6 契约压到只讲
「声明+门禁+实测漂移 43%」三点 → 4.4 压到只讲 `mergeV` 和 `coalesce` 两个 step。
**4.1（节点/边/属性定义）和 4.5（身份键）绝对不能砍**——前者是听懂后面全部内容的前提，
后者是最有共鸣的一页。
**4.11 也不能整节砍**——砍掉它，整个项目在听众眼里就只是一个精致的 ETL 作业；
时间紧就压到 2 分钟，只讲「24 个只读工具 + 离线快照」两个点。

> 本节满讲 39 分钟（4.1 扩到 5 分钟 + 新增 4.11 的 5 分钟）。全篇满讲已约 92 分钟，
> 而主线听众是 45–60 分钟——**这份提纲从来不是按满讲设计的**，裁剪顺序才是它的
> 使用方式。第 11 节按听众给了删改方案。

> 这一节的作用是让听众相信**这是一个真跑着的系统，而且他们照着能建**。
> 讲法上有个纪律：**每讲一个设计，紧跟着讲它防住了哪个已实测的错**。
> 只讲设计会像 PPT 架构，只讲 bug 会像事故复盘，两个绑在一起才是工程。

### 4.1 图谱当前长什么样 + 节点/边/属性的定义（5 分钟）

#### 先划边界：两个 VPC，一个是被观测的，一个是观测它的（1 分钟）

**这一页放在最前面，因为它一次答完听众必问的两个问题：这些节点从哪来、边界在哪。**

```
ap-northeast-1
├── PetSiteVPC        vpc-010ab37a3f9f74725 · 11.0.0.0/16   ← 被观测的系统
│   ├── EKS PetSite v1.35（4 节点 · 14 业务 Pod）
│   ├── Aurora PostgreSQL 16.11 / Aurora MySQL 8.0 / Neptune 1.4.6.3
│   ├── EC2 可观测组件（DeepFlow / Grafana）
│   └── VPC 内 Lambda × 9（含 5 个图谱 ETL）
└── agent-vpc-v2      vpc-06731f30388b57818 · 10.1.0.0/16   ← 工具与运维侧
    ├── 构建机 / 压测机 / Agent 侧 EC2
    └── TiDB PoC 子网 × 8（当前零实例）

两者经 VPC peering pcx-09179d94866c4afd6（active）连通，网段不重叠。
```

这个物理分工正好是图谱 `scope` 维度的直觉来源：**图里的节点不是同一类东西**。
被观测的业务、观测它的采集栈、管这张图的平台自己、只在部署期跑的 IaC 脚手架——
混在一张图里做影响面分析会得出荒谬结论。第 4.6 节讲的 `scope` 六档就是把这条
物理边界变成可查询的属性。

> 讲法：「先看这张图。**右边这个 VPC 里的东西，一个都不该出现在容灾计划里**——
> 它们是拿来观测左边的。但它们确实在依赖图谱里，因为 ETL 采到了。
> 这就是我们为什么要给节点标 scope。」

#### 三个词先定义清楚：节点、边、属性

图数据库只有三种东西。用本项目的真实数据各举一例，讲完听众就能读懂后面所有内容。

| 术语 | 图论叫法 | 大白话 | 本项目里是什么 |
|---|---|---|---|
| **节点** | vertex / node | **一个东西** | 一台 EC2 实例、一个微服务、一个 S3 桶、一次故障事件 |
| **边** | edge / relationship | **两个东西之间的一种有方向的关系** | 「petsite 调用 petlistadoptions」「这个 Pod 跑在那台机器上」 |
| **属性** | property | **挂在点或边上的键值对** | 实例的 `cpu_util_avg=0.77`、调用边的 `calls=63` |

每个节点有一个**标签（label）**表示它是哪类东西（`EC2Instance` / `Microservice`），
每条边也有一个标签表示这是哪种关系（`Calls` / `RunsOn`）。本项目：
**39 种节点标签、29 种边标签**，都在契约里声明（见 4.6）。`[代码]`

**最需要强调的一点：边自己也能带属性。**

> 「这是『属性图』（property graph）和很多人印象里的『关系』最大的区别。
> 在关系数据库里你可以用一张 `calls(src, dst)` 表表示调用关系，但要给这条关系挂上
> 『延迟多少、错误率多少、谁最先发现的、上次看到是什么时候』，你得再加一堆列，
> 而且一旦要按这些列做多跳查询就非常难写。**在属性图里边是一等公民**，
> 它和节点一样可以带任意属性——本项目边上最多挂了 20 个属性。」

（对比：RDF / 三元组模型的边是 `(主语, 谓语, 宾语)`，**边本身挂不了属性**，
要表达「这条关系的延迟」得引入具化节点（reification）绕一圈。Neptune 同时支持属性图
和 RDF，本项目选属性图正是因为边属性是核心需求。）

#### 一个真实节点长什么样（活图谱实测，2026-08-31）`[实测]`

`MATCH (n:EC2Instance) RETURN properties(n) LIMIT 1` 的真实返回：

```json
{
  "instance_id": "i-022fb7c32b71c72d9",     ← 身份键（不可变，见 4.5）
  "name": "openclaw-instance-v2",           ← 显示名（可变，来自 Name 标签）
  "instance_type": "m8g.xlarge",
  "state": "running",
  "az": "ap-northeast-1a",
  "private_ip": "10.1.2.198",
  "health_status": "healthy",

  "cpu_util_avg": 0.77,                     ← 以下是 CloudWatch 补的度量
  "memory_util": 16.57,
  "disk_util": 80.43,
  "network_in_mbps": 0.0009,
  "network_out_mbps": 0.0052,
  "cw_updated_at": 1788165653,

  "environment": "prod",                    ← 以下是治理/溯源元数据
  "fault_boundary": "az",
  "recovery_priority": "Tier2",
  "team": "platform-team",
  "system": "platform",
  "source": "aws-etl",                      ← 谁写的
  "managedBy": "manual",
  "last_updated": 1788165652                ← 什么时候写的
}
```

**24 个属性，可以分成四组来讲**：身份与配置（AWS Describe API 来的）、
运行度量（CloudWatch 来的）、治理标注（标签推导的）、溯源元数据（ETL 自己盖的章）。

这里有一个**必须主动说出来的诚实细节** `[实测]`：
`profiles/petsite.yaml` 的 schema 文本里 EC2Instance 只声明了 **7 个**属性
（`instance_id, name, state, az, health_status, instance_type, private_ip`），
而活节点上有 **24 个**。这不是漂移失控，是**刻意的**——契约明确写了
「**不校验属性集**」，理由是各数据源写入的属性子集本来就不同
（xray 只补度量、cfn 只写 `declared_in`），强制属性集会把「这个源没有这项数据」
误判成违约（见 4.6）。**类型必须严格声明，属性刻意开放**——这个不对称是设计选择。

#### 一条真实依赖边长什么样（活图谱实测）`[实测]`

`(petsite) -[:Calls]-> (petlistadoptions)` 这条边上的全部属性：

```json
{
  "source": "deepflow-etl",          ← 首个发现者（写一次，见 4.4）
  "dependency_kind": "dynamic",      ← 运行时观测到的，不是配置声明的
  "first_seen": 1788017504,          ← 第一次看到
  "last_seen": 1788165402,           ← 最后一次看到（软删除判据，见 4.7）
  "active": true,

  "calls": 63,                       ← 以下 DeepFlow 的 L7 度量
  "protocol": "HTTP",
  "port": 80,
  "call_type": "sync",
  "avg_latency_us": 1910723,
  "p99_latency_ms": 15011.5573,
  "error_rate": 0.0,
  "error_count": 0,

  "xray_call_count": 21102,          ← 以下 X-Ray 补的度量（前缀隔离）
  "xray_fault_count": 1,
  "xray_error_count": 0,
  "xray_total_response_time_s": 1981.055,
  "xray_last_seen": 1788105649,
  "xray_window_hours": 24
}
```

**这一页信息量很大，讲三点就够**：

1. **一条边被两个源共同丰富**：`source=deepflow-etl` 说明 DeepFlow 先发现了它，
   X-Ray 后来在同一条边上补了 6 个 `xray_*` 属性。**X-Ray 没有抢走署名**——
   这正是 4.4 那个「写一次」机制在起作用。
2. **X-Ray 的度量用前缀隔离**。不是覆盖 `calls`，而是另开 `xray_call_count`。
   两个源口径不同（DeepFlow 数 L7 请求、X-Ray 数 span），**混成一个字段就永远说不清哪个是哪个**。
   注意两个数字差了 300 多倍（63 vs 21102）——因为窗口不同（5 分钟 vs 24 小时）。
3. **`dependency_kind` 是这张图里最要紧的一个属性**，下面单独讲。

#### 29 种边里只有 6 种是「依赖」——这个区分是全场关键

契约里每种边都有一个 `dependency: true|false` 标记 `[代码]`：

| | 数量 | 边标签 | 语义 |
|---|---|---|---|
| **依赖语义边** | **6** | `Calls`、`AccessesData`、`DependsOn`、`Delegates`、`InvokesTool`、`Retrieves` | **A 坏了 B 会受影响** |
| 结构/归属边 | 23 | `RunsOn`、`BelongsTo`、`LocatedIn`、`Contains`、`ForwardsTo`、`HasSG`、`Manages`… | A 属于 B / A 在 B 里面 / A 管着 B |

> **8/31 → 9/5 的变化**：依赖语义边从 3 种扩到 6 种。新增的三种
> （`Delegates` / `InvokesTool` / `Retrieves`）是 **AI Agent 层**的依赖——
> 编排 agent 路由到子 agent、agent 调用 tool、agent 检索知识库。
> 这三种是本项目相对业界的一个额外差异点：**没有任何现成工具把 agent 的
> 委派与工具调用当成依赖边来管理**，而它们的故障传播语义和服务调用是一样的。

> 「**约 2600 条边里只有 126 条是合法的依赖边**（精确值见 4.1 末尾那张表）。**如果分不清这两类，影响面分析就会把
> 『这个 Pod 跑在哪台机器上』和『服务 A 调用服务 B』等权处理**——你会得到一张
> 什么都连着什么的图，然后发现它回答不了任何问题。」

这个标记不只是文档标注，它有**执行后果**：`is_dependency_edge()` 决定了要不要给这条边
写 `dependency_kind`，`dependency_edge_labels()` 是混沌实验选靶子的依据（见第 5 节）。
而且这份定义**只有一处**——此前三个 ETL 各自复制了一份同样的 `DEPENDENCY_EDGE_LABELS`
常量，现在统一从契约派生。`[代码注释]`

`dependency_kind` 有**三个**取值，区分**证据等级** `[实测 2026-09-05]`：

| 取值 | 条数 | 含义 | 谁写的 |
|---|---|---|---|
| `dynamic` | **77** | **运行时真的观测到流量** | DeepFlow（etl/l4/dns）/ X-Ray / NFM |
| `static` | **27** | **配置里声明了这个关系**，但没观测到流量 | AWS Describe API / CloudFormation / business-layer |
| `inference` | **11** | **从 agent span 推断**——LLM 在运行时按 query 决定调谁，既非固定配置也非稳定流量 | agentcore-etl |

> 「一条 `static` 边的意思是『文档说他们有关系』，一条 `dynamic` 边的意思是
> 『我看见他们在通信』。这两件事**经常不一致**，而不一致的地方就是最值得查的地方——
> 第 7 节的证据就是从这个缝里出来的。」

> **`inference` 这个第三档是 9/5 新增的，值得单独讲 30 秒**：Agent 的调用图不是
> 静态配置（没写在任何 IaC 里），也不是稳定流量（同一个 agent 面对不同 query 会调不同 tool）。
> 把它塞进 `dynamic` 会让「30 分钟没看到就失效」这条 TTL 误杀低频 tool；
> 塞进 `static` 又会让它永不失效。所以给了独立取值 + 独立失效语义
> （`graph_cleanup.py` 对 `inference` 边有专门分支）。`[代码]`

#### 边还带两个生命周期参数（4.7 详讲，这里先给直觉）

```yaml
Calls:
  dependency: true
  expires_seconds: 1800        # 30 分钟没看到 → 标记为不活跃（软删除）
  retention_seconds: 604800    # 7 天没看到 → 真删

AccessesData:
  expires_seconds: 21600       # 6 小时——刻意取最长源窗口（X-Ray 的 6h）

RunsOn:
  dependency: false
  expires_seconds: null        # 结构边不独立过期，生命周期跟随两端节点
```

`AccessesData` 那个 6 小时值得单独说一句：它**不是拍脑袋定的**，
是因为四个源都写这类边、其中 X-Ray 的采集窗口最长（6 小时），
取更短的阈值会**误杀 X-Ray 刚发现的边**。`[代码注释]`


活图谱实测（**2026-09-05 14:4x UTC**）`[实测]`：

| 指标 | 值 | 8/31 时 | 变化 |
|---|---|---|---|
| 节点 | **1335** 个，**39** 种标签 | 1063 / 33 | +26% / +6 类 |
| 边 | **2637** 条，**29** 种标签 | 1802 / 26 | +46% / +3 类 |
| 带 `dependency_kind` 的边 | **131** 条（dynamic 77 / static 43 / inference 11） | 94（static 21 / dynamic 73） | +39% |
| 其中**合法**（落在 7 种依赖边上） | **126** 条 | 94 | — |
| 其中**越界**（写在非依赖边上） | **5** 条 ⚠️ | 0（当时不存在此类边） | 见第 8 节 |
| **已被实验确证的依赖边** | **13 条 `confirmed`** | **0** | **从 0 到非 0** |
| 无法确证（`inconclusive`） | **25 条** | 0 | 新增 |
| 强度已分级（`verify_dependency_class`） | **9 条**（hard 1 / degraded 3 / unclassified 5） | 0 | 新增 |
| 节点分布（前 6） | Pod **735**、Incident **126**、ChaosExperiment **91**、SecurityGroup **58**、TopologyChange **56**、S3Bucket **34** | Pod 570、SG 55、S3 33、Lambda 32 | 见下 |

> ⚠️ **这张表是全文总量数字的唯一权威处，其余各节一律只用约数（「约 2600 条边」）。**
> 这不是偷懒——本次核对期间（约 20 分钟内）实测到节点 1333→1335、边 2625→2637、
> 带 `dependency_kind` 的边 **115→131**。ETL 每 15 分钟跑一次，图是活的，
> **把精确总量散写到十几处，每次更新必然漏改几处**（本次第一轮就漏了 5 处，
> 靠独立复查才抓出来）。
>
> **讲之前重跑这张表的查询即可**，别处不用动。
> 那次 +16 是 `Invokes` 边被重分类为依赖边后**合法**带上了 `dependency_kind`，
> 属于正常增长（见第 8 节对这次重分类的说明）。
>
> 相比之下 **13 / 25 / 9 这三个验证类数字是稳定的**——它们只在跑实验时才动。

**这张表最该讲的三点**：

1. **2637 条边里只有 126 条是合法的"依赖"。** 其余是 `LocatedIn`（825）、`RunsOn`（444）、
   `Manages`（300）、`BelongsTo`（294）、`Routes`（277）这类结构/归属边。
   **分不清这两类，影响面分析就会把"Pod 跑在哪台机器上"和"服务 A 调用服务 B"等权处理。**

2. **验证覆盖率从 0 变成了非 0，但仍然很低——而这正是要讲的点。**
   126 条合法依赖边里 13 条确证、25 条无法确证，其余未尝试。强度维度只覆盖 9 条。
   > 「只有 12% 的依赖边取得了干预确证，而这张图已经在支撑影响面分析与容灾恢复顺序推导。
   > **依赖图的价值不来自『全部验证过』，而来自『每条边的证据强度都被如实标注』。**」

3. **节点分布里新出现了三类实体，它们是 8/31 之后才有的**：
   `ChaosExperiment`（91）是实验记录本身、`Incident`（126）是事故实体、
   `TopologyChange`（56）是拓扑变更事件。**图开始记录自己被验证的过程**——
   这三类实体的出现，就是"从 0 到非 0"这件事在图上的物证。

### 4.2 数据源：五类，各有唯一贡献（3 分钟，一张表）

| 源 | 拿到什么 | **唯一贡献**（别的源拿不到） | 采集方式 |
|---|---|---|---|
| **AWS 控制面 API** | EC2/VPC/Subnet/SG/EKS/ALB/RDS/Lambda/SFN/DDB/SQS/SNS/S3/ECR 清单与配置 | 资源**存在性与配置事实**；AZ 归属、单 AZ 判定的唯一依据 | boto3 describe/list，单区域 |
| **Kubernetes API（EKS）** | Pod / Deployment / K8sService / HPA / Namespace | 容器编排层的**实例级归属**（哪个 Pod 在哪台节点） | EKS token + K8s REST |
| **CloudFormation 模板** | Lambda env `Ref`、SFN `DefinitionString` `GetAtt`、ALB Rule `Ref` | **声明的意图**——代码打算依赖谁，运行时看不到的也算 | `GetTemplate` + `ListStackResources` |
| **DeepFlow（eBPF → ClickHouse）** | L7 flow_log：服务间调用、时延、错误率、RPS；L4 连接；DNS 查询 | **实际观测到的服务间调用**与 SLI 度量 | ClickHouse HTTP 8123 直连 SQL |
| **AWS X-Ray** | 服务图：服务 → AWS 托管服务的调用与次数 | **服务 → 托管服务**这一层的运行时证据（DeepFlow 的 Calls 看不到） | `GetServiceGraph`（单次窗口上限 6h）`[代码]` |
| **CloudWatch + Network Flow Monitor** | EC2/Lambda 指标、NFM 逐流指标与拓扑 | 主机与网络层的**性能数值**、ENA 限流 | `GetMetricData` / NFM query |

**这一页要讲的洞察**（比表本身重要）：

> 「**这五个源不是冗余，是四种不同的认识论。** CFN 是『声明』，DeepFlow/X-Ray 是『观测』，
> AWS API 是『存在』，CloudWatch 是『度量』。把它们塞进同一张图，
> 第一个收益不是数据全了，而是**它们可以互相对账**。」

对账的具体产物是 `drift_status`，活图谱实测 `[实测 2026-09-05]`：
- `declared_not_observed` **25 条**（CFN 说有、运行时没看到）
- `ok` **7 条**（声明与观测一致）
- `observed_then_silent` **1 条**（曾观测到、之后静默）——**这是 9/5 新增的第三个取值**，
  语义上比前两者更值钱：它区分了「从来没见过」与「见过又不见了」，而后者往往意味着
  真实的下线或链路中断，前者往往只是观测盲区。

而 X-Ray 恰恰是这里的关键教训：**在引入 X-Ray 之前，漂移判定只用 DNS 作观测源**，
而 AWS SDK 通常启动时解析一次域名就复用连接，走 VPC 端点更不产生公网 DNS 查询。
结果一个**每 24h 被调用 11,512 次**的依赖，在 DNS 窗口里完全看不见——26 条边里 22 条
被误判 `declared_not_observed`（85%）。`[代码注释记录的实测]`

> 「所以『漂移』这个信号本身也需要被证伪。**加一个观测源就翻掉了 85% 的判定**——
> 这是全场关于『单一观测源不可信』最便宜的证据。」

### 4.3 ETL：五条**互相独立**的流水线（4 分钟）

`[代码]` 全部在 `infra/lambda/`，Python 3.12 Lambda，共约 **7,500** 行
（5 条 ETL 合计 7,519 行；另有 `shared/python/` 公共层 1,902 行）：

| 函数 | 源 | 触发 | 超时/内存 | 行数 | 写什么 |
|---|---|---|---|---|---|
| `neptune-etl-from-aws` | AWS API + EKS + CloudWatch/NFM | 每 **15 分钟** + 事件驱动 | 5 min / 256 MB | **3217**（7 个文件：handler + collectors/ + cloudwatch.py） | 绝大多数节点、结构边、静态依赖边 |
| `neptune-etl-from-deepflow` | ClickHouse L7/L4/DNS | 每 **5 分钟** | 4 min / 256 MB | 1865 | `Calls` 边 + 度量属性 + `drift_status` |
| `neptune-etl-from-cfn` | CFN 模板 | **CFN StackEvent** + 每日 18:00 UTC | 2 min / 256 MB | 488 | `DependsOn` 声明边 |
| `neptune-etl-from-xray`（独立部署） | X-Ray 服务图 | 定时 | — | 861 | `AWSServiceEndpoint` 节点 + `AccessesData` 边 |
| `neptune-etl-trigger` | EventBridge → SQS | 事件 | 90 s / 128 MB，**预留并发 = 1** | 111 | 不写图，延迟 30 s 后异步调 aws-etl |

**三个值得讲的实施决定**：

1. **X-Ray 做成独立 Lambda 而不是塞进 deepflow。** 理由写在代码注释里，而且是个原则性理由：
   > 「『两个独立观测源』这件事必须在架构上真独立。如果两者同一个 Lambda，
   > 一个 bug 同时打掉两边，图谱里『双源印证』的展示就是假的。」`[代码]`

   附带好处：权限面小（只要 `xray:GetServiceGraph`，不碰 ClickHouse 和 EKS token）、
   失败节奏不同（X-Ray 单次窗口上限 6h vs DeepFlow 30 分钟滑窗）。

2. **事件驱动 + 定时双轨，且触发器预留并发 = 1。** 基础设施变更 → EventBridge → SQS →
   trigger Lambda → **等 30 秒** → 异步调 ETL。等 30 秒是因为 AWS API 需要时间反映最新状态
   （例如 RDS failover 后新 IP 就绪）；并发限 1 是为了不让两个 ETL 同时写 Neptune。`[代码]`

3. **只采白名单 namespace**（`default`、`awesomeshop`）。`deepflow`、`kube-system` 这些
   监控/基础设施 namespace 刻意不进图——否则会形成一堆**孤立分量**，把图谱指标稀释掉。`[代码]`

### 4.4 怎么写进图数据库（8 分钟，本节技术核心）

#### 先花 90 秒教会听众读 Gremlin（不然后面的代码白贴）

绝大多数听众没写过 Gremlin。不铺这一层，后面两段代码就只是「一堆看不懂的点号」。

**Gremlin 是什么**：Apache TinkerPop 的图遍历语言。它不像 SQL 那样描述「我要什么结果」，
而是描述**「一个想象中的小人在图上怎么走」**——从哪个点出发、沿哪种边走、走到哪停、
路上做什么。Amazon Neptune 同时支持 Gremlin 和 openCypher 两种属性图查询语言
（还支持 SPARQL 用于 RDF），**同一张图可以用两种语言随意查**
（[AWS 官方文档](https://docs.aws.amazon.com/neptune/latest/userguide/access-graph-queries.html)：
"Both Gremlin and openCypher can be used to query any property-graph data stored in Neptune,
regardless of how it was loaded."）。本项目 **写入用 Gremlin、RCA 查询用 openCypher**——
后者更像 SQL，让 LLM 生成的成功率更高。

读 Gremlin 只需要记三条规则：

| 规则 | 说明 | 例子 |
|---|---|---|
| **1. `g` 是起点** | `g` 代表整张图，所有遍历从它开始 | `g.V()` = 「图里所有点」 |
| **2. 用 `.` 串起来，每一段叫一个 step** | 上一段的输出是下一段的输入，像 Unix 管道 | `g.V().hasLabel('Pod').count()` = 所有点 → 只留 Pod → 数个数 |
| **3. `__` 开头的是匿名遍历** | 「从当前位置再走一小段」，用在需要嵌套子查询的地方 | `.where(__.outV().hasId('x'))` = 「筛选：它的出发点是 x」 |

小抄：`V()`=点，`E()`=边，`addV/addE`=新建点/边，`inE()`=指进来的边，
`outV()`=边的出发点，`property(k,v)`=设属性，`as('s')`=给当前位置起个名字待会儿回来用。

#### 连接与鉴权：真实代码，含两个诚实说明

`infra/lambda/etl_aws/neptune_client.py` 的 `neptune_query()` `[代码]`：

```python
url = f"https://{NEPTUNE_ENDPOINT}:{NEPTUNE_PORT}/gremlin"
data = json.dumps({"gremlin": gremlin})
aws_req = AWSRequest(method="POST", url=url, data=data, headers=headers)
SigV4Auth(creds, "neptune-db", REGION).add_auth(aws_req)      # ← IAM 认证
r = _get_http_session().post(url, headers=dict(aws_req.headers),
                             data=data, verify=False, timeout=20)
```

要讲的点：

- **鉴权是 IAM + SigV4 签名**，service name 是 `neptune-db`。Neptune 没有用户名密码，
  权限完全由 IAM 策略控制——这也是为什么第 8 节里那个 403 事故的根因是
  IAM 缺 `neptune-db:DeleteDataViaQuery` 这个动作。
- **没有用批量加载器（bulk loader）**。Neptune 有 bulk loader 走 S3 + CSV，吞吐高得多，
  但我们是**增量对账**而不是全量导入——每 5/15 分钟只碰变化的那部分，所以逐条 Gremlin
  才是对的形态。
- `timeout=20`（秒）——这是**客户端**超时；Neptune 侧还有自己的查询超时。
- **`verify=False`：TLS 证书校验被关掉了。** 这是 Lambda 里为省一个 CA bundle 的常见妥协，
  但在 VPC 内也仍然是弱化。**这是一个应该修的技术债，讲的时候别藏。**

**版本要说准**（这条本身就是本项目在讲的问题）`[实测]`：

| 来源 | 版本 |
|---|---|
| CDK 代码 `infra/lib/neptune-cluster-stack.ts:102` | `engineVersion: '1.3.4.0'`（注释写 TinkerPop 3.7） |
| **活集群 `petsite-neptune` 实测** | **1.4.6.3** |

> 「顺便说一句，我这份提纲的上一版就把版本写成了 1.3.4.0——因为我照抄了 CDK。
> 而真实集群是 1.4.6.3，那个 CDK stack 根本不在活跃 CloudFormation 栈里。
> **这就是我们整个项目在解决的那类问题的一个微型样本：声明和现实分叉，而且没人报错。**」

#### 幂等 upsert 节点：真实生成的语句

`upsert_vertex()` 拼出来的 Gremlin（这是真实模板，不是简化版）`[代码]`：

```groovy
g.mergeV([(T.label): 'EC2Instance', 'instance_id': 'i-022fb7c32b71c72d9'])
 .option(Merge.onCreate, [(T.label): 'EC2Instance',
                          'name': 'openclaw-instance-v2', 'managedBy': 'manual',
                          'source': 'aws-etl', 'az': 'ap-northeast-1a', ...])
 .option(Merge.onMatch,  ['managedBy': 'manual', 'source': 'aws-etl',
                          'name': 'openclaw-instance-v2', 'az': 'ap-northeast-1a', ...])
 .property(single, 'managedBy', 'manual')
 .property(single, 'source', 'aws-etl')
 .property(single, 'name', 'openclaw-instance-v2')
 .property(single, 'az', 'ap-northeast-1a')
 ...
 .property(single, 'last_updated', 1788165652)
 .id()
```

逐 step 拆开讲：

| step | 作用 | **去掉会怎样** |
|---|---|---|
| `mergeV([...])` | 「按这组键找点，找到就用，找不到就建」——**方括号里就是身份键** | 换成 `addV()` 每轮 ETL 都新建一份，图会无限膨胀 |
| `(T.label): 'EC2Instance'` | `T.label` 是 TinkerPop 保留键，指「类型标签」 | 不带标签就会跨类型匹配——**这正是 183 条错源边的根因**（见 4.5） |
| `.option(Merge.onCreate, [...])` | **只在新建时**写这些属性 | 首次建点缺属性 |
| `.option(Merge.onMatch, [...])` | **只在命中已有点时**更新这些属性 | 已有节点永远不更新，图很快陈旧 |
| `.property(single, k, v)` | 设属性，`single` = 单值基数（覆盖而非追加） | **不写 `single` 会变成多值列表**：同一属性反复写入会累积成 `['a','a','a']`。本项目有一个 `infra/fix_property_cardinality.py` 就是清理这个历史问题的 |
| `.id()` | 返回这个点的内部 ID，供后面建边用 | 拿不到 ID 就无法建边，得再查一次 |

**为什么 `name` 要同时出现在 `onMatch` 和 `property` 链里**——这是个容易看漏的细节 `[代码注释]`：
以 `instance_id` 为身份键时，如果 `name` 只在 `onCreate` 里，那么机器改了 Name 标签之后
图谱里永远留着旧名字。代码注释直说：「否则标签改名后图谱里仍留着旧名字，
**等于只是把重复换成了陈旧**」。

#### 建边并「溯源属性只写一次」：真实生成的语句

`upsert_edge()` 拼出来的 Gremlin `[代码]`：

```groovy
g.V('<src_id>').as('s').V('<dst_id>')
 .coalesce(
     __.inE('Calls').where(__.outV().hasId('<src_id>')),    // ① 已有这条边？复用
     __.addE('Calls').from('s')                             // ② 没有才新建
 )
 .property('source',
           __.coalesce(__.values('source'), __.constant('deepflow-etl')))   // ③ 写一次
 .property('dependency_kind',
           __.coalesce(__.values('dependency_kind'), __.constant('static')))
 .property('last_seen', 1788165402)
 .property('last_updated', 1788165402)
 .property('calls', '63')
```

逐 step 拆开：

| step | 作用 | 要点 |
|---|---|---|
| `g.V('<src_id>')` | 跳到源点 | 用的是 Neptune 内部 ID，比按属性查快 |
| `.as('s')` | 给当前位置起名 `s`，待会儿回来引用 | 没有它，`addE().from('s')` 就不知道从哪出发 |
| `.V('<dst_id>')` | **再跳**到目标点（遍历位置换了） | Gremlin 里 `V()` 可以在中途重新定位 |
| `.coalesce(A, B)` | 「先试 A，A 空了才试 B」——**这是幂等的关键** | 相当于 SQL 的 `COALESCE`，但作用在遍历上 |
| `__.inE('Calls')` | 从当前位置（目标点）看**指进来**的 Calls 边 | 只看这一种标签——Neptune **没有** OSGP 反向索引，不带标签的 `in()` 无索引可用 |
| `.where(__.outV().hasId(src))` | 筛掉不是从我们这个源点来的边 | 少了这句会复用**别人指向同一目标**的边 |
| `__.addE('Calls').from('s')` | 新建边，起点用前面 `as('s')` 标记的位置 | 只在 ① 空时执行 |
| `.property('k', __.coalesce(__.values('k'), __.constant('v')))` | **属性级的写一次**：已有值就保留，没有才写 | 见下 |

#### 全场最容易讲混的一点：`coalesce` 在这条语句里出现了两次，含义完全不同

> 「同一个 `coalesce`，第一次用在**边上**，第二次用在**属性上**。听众十有八九会混。」

| 出现位置 | 在做什么 | 保证的是 |
|---|---|---|
| `.coalesce(inE(...), addE(...))` | 找边 / 建边 | **边**的幂等：跑一百遍只有一条边 |
| `.property(k, __.coalesce(__.values(k), __.constant(v)))` | 读属性 / 写属性 | **属性**的写一次：先发现者的署名不被后来者抹掉 |

第二个的必要性有真实事故背书 `[代码注释]`：

原实现是无条件 `.property('source', 'aws-etl')`。`coalesce` 命中一条已存在的边之后**照写**，
于是把 deepflow / xray 先写的 `source` 静默改成 `aws-etl`——**等于抹掉「谁首先发现了这条依赖」**。
而 xray / deepflow-L4 / NFM 三个源本来就刻意保护这些属性，只有 aws 与 cfn 两处覆盖
——**行为自相矛盾**，「谁是首个发现者」随 ETL 执行顺序变化。

**这个修复自己又出过一次 bug，值得讲**（2026-08-31 修）`[代码注释]`：
第一版把「写一次」实现成了**丢弃调用方的取值**——

```python
write_once = {'source': 'aws-etl'}     # 硬编码
for k, v in props.items():
    if ks in write_once: continue      # 调用方传的 source 被跳过
```

后果：`handler.py` 里 13 处 `{'source': 'eks-etl'}` 和 1 处 `'aws-etl-static'` **全部静默失效**，
新建的边一律写成 `aws-etl`。而活图谱还能看到 1228 条 `eks-etl`，
是因为 `coalesce` 保护了**存量**边——所以这个 bug 只影响此后新建的边，
**不会立刻暴露，只会让 provenance 缓慢腐坏**。

正确语义是两件事必须分开：

- **写一次** = 已存在的边不覆盖 → 由 `coalesce` 保证
- **取什么值** = 首写者说了算 → 也就是调用方传进来的那个

#### 字符串拼接建 Gremlin：安全边界在哪

本项目**不用参数化查询，直接拼字符串**。所以必须讲清防线在哪 `[代码]`：

```python
def safe_str(s) -> str:
    return str(s).replace("'", "\\'").replace('"', '\\"')[:256]
```

- 转义单双引号 → 防注入
- **截断到 256 字符** → 这一条要如实说明：超长值会被**静默截断**，
  对 `root_cause` 这类长文本属性是真实的数据损失风险。属于已知技术债。
- 数值属性走 `_format_prop_val()` 判断，命中 `NUMERIC_PROPS` 才不加引号——
  否则数字会被存成字符串，比较和排序全错。

#### 三件必须讲清的事（原提纲要点，保留）


（连接、鉴权、版本与超时已在上面「连接与鉴权：真实代码」一段讲过，不重复。）

**幂等 upsert 的实际写法**（这一页贴代码，是全场最"能带走"的一页）：

```python
# infra/lambda/etl_aws/neptune_client.py  upsert_vertex()
g.mergeV([(T.label): 'EC2Instance', 'instance_id': 'i-0abc...'])
 .option(Merge.onCreate, [... 'name': ..., 'source': 'aws-etl'])
 .option(Merge.onMatch,  [... 'name': ...])          # name 进 onMatch，改名要跟上
 .property(single, 'last_updated', 1756...)
 .id()
```

```python
# upsert_edge()：边的溯源属性"写一次"
g.V(src).as('s').V(dst)
 .coalesce(__.inE('Calls').where(__.outV().hasId(src)),   # 已有则复用
           __.addE('Calls').from('s'))                    # 没有才新建
 .property('source', __.coalesce(__.values('source'), __.constant('aws-etl')))
 .property('last_seen', 1756...)
```

**要讲清的三件事**：

1. **`mergeV` 的第二个参数就是身份键**——这决定了"什么是同一个东西"，是整个 ETL 最
   要紧的一行。
2. **边的 `source` 用 `coalesce(values(k), constant(v))` 而不是直接 `property(k,v)`。**
   直接写会把先发现者的 provenance 抹掉。**这个 bug 真实存在过**：xray / deepflow-L4 / NFM
   三个源刻意保护了这些属性，而 aws 与 cfn 两处覆盖——**行为自相矛盾**，
   等于「谁首先发现了这条依赖」随 ETL 执行顺序变化。`[代码注释]`
3. **边属性不能用 `property(single, ...)`。** Neptune 对边属性拒绝基数说明，实测返回
   `400 UnsupportedOperationException: "Cardinality specification may not be used with
   Edge properties."`——而这个异常曾被 `except` 吞成一行 log，导致**21 个实验跑完后
   19 条 Calls 边上的 chaos 属性全部为 0**。`[代码注释记录的实测]`

   > 「这是我最想让各位带走的一条：**一条永远失败的写入路径，配上一个吞异常的 except，
   > 可以让一个功能『存在』一整年而从未生效。**」

### 4.5 身份键：为什么绝对不能用 name（2 分钟，最有共鸣的一页）

问题陈述一句话讲完：

> 「EC2 的 `name` 取自 Name 标签。标签随时可改。一改，`mergeV` 匹配不到旧节点就**新建一个**,
> 旧节点带着当时的属性永远孤立在图里。」

**实测后果** `[代码注释记录的实测，2026-08-29]`：14 个 EC2Instance 节点里 **4 个是重复实体**——
4 台 EKS 工作节点各有两份，一份以实例 ID 命名（打 Name 标签之前建的，已停止更新 88 天），
一份以 Name 标签命名。**任何「有几台工作节点」的查询都会数两次，且一份带 3 个月前的陈旧值。**

**关键澄清**（这句能挡住一半质疑）：

> 「注意**修 name 的取值规则防不住这件事**——那条规则本来就是对的（Name 标签优先、
> 缺失回落 ID）。问题在于**拿一个可变属性当身份**。」

做法：契约里为每种节点声明 `identity`，`upsert_vertex` 从契约派生身份键 `[代码]`：

```yaml
VPC:        { identity: vpc_id,      immutable: true }
Subnet:     { identity: subnet_id,   immutable: true }
EC2Instance:{ identity: instance_id, immutable: true }
Pod:        { identity: name,        immutable: lifetime }   # Pod 重建即换名，属预期
Microservice:{ identity: name,       immutable: true }       # 规范名，是声明而非观测
```

**从契约派生而非逐个调用点传参**，理由也值得讲：早先是在调用点显式传 `identity_prop`，
结果 **Subnet / VPC / SecurityGroup / TargetGroup 四处漏掉了**。改成契约派生后，
新增类型只改契约即生效，且由 `tests/test_35_graph_contract.py` 强制两者一致。`[代码]`

回落策略也是刻意的：拿不到 `instance_id` 时**回落到 name 而不是抛错**——
「一个拿不到 ID 的实例仍然应该进图谱，只是退回旧的（有缺陷的）身份语义，
比整轮 ETL 失败好。」`[代码注释]`

### 4.6 图谱契约：一份机器可读声明 + 运行时门禁（6 分钟）

#### 先用一句话说清「契约」是什么（讲给完全没听过的人）

> **关系数据库有 schema，图数据库默认没有。契约就是我们自己补上的那个 schema，而且它带执法权。**

这句话需要展开，因为它是本节全部设计的由来：

你在 MySQL 里 `INSERT INTO order (custmer_id) VALUES (1)`——把 customer 拼错了——数据库直接报错，
因为表结构写死了有哪些列。你想插一条指向不存在客户的订单，外键约束也会拦住。

**图数据库不是这样。** Neptune（以及 Neo4j、JanusGraph 等主流图库）默认是 **schema-free** 的：
你写 `g.addV('Microservice')` 它就建一个 Microservice 节点，你写 `g.addV('Microservie')`（拼错了）
它照样建，只是多出一种你从没打算要的节点类型。属性同理，边同理。**图数据库把「有哪些类型」
这件事完全交给应用层**——这是它的灵活性来源，也是它的风险来源。

对一个只有一个写入者的小项目，这没问题。对**五条互相独立的 ETL 流水线同时往一张图里写**的系统，
这是灾难：任何一条流水线拼错一个标签、或者某个作者临时新造一个类型，图里就会静默多出一类东西，
而所有按类型统计的查询从此都是错的——**而且没有任何报错**。

所以我们做了两件事：把「允许有什么」写成一份机器可读的文件，再在写入路径上放一道门禁去执行它。

#### 引入之前是什么样（这是必须先讲的动机）

`[代码]` 门禁模块自己的文档字符串里记着这句话：

> 「引入本模块之前，四个写入 ETL（etl_aws / etl_deepflow / etl_xray / etl_cfn）**无一 import
> profiles**，运行时对节点与边类型零校验：`upsert_vertex` / `upsert_edge` 拿到什么 label 就往
> Gremlin 里直拼。」

也就是说，当时项目里确实有一份「权威 schema」（`profiles/petsite.yaml` 里的
`graph_schema_text`），但它**只在测试期被比对一次**，运行时没有任何人读它。
声明和执行是脱节的——这是本节最值得带走的一条通用教训：

> **一份没有门禁去读的声明，等于没有声明。**

这不是抽象论断，有实测数字。契约里一直有一节 `sources`（合法的数据来源词表），
但 2026-08-31 之前**没有任何一处代码检查它**。当时对活图谱普查的结果
`[实测 2026-08-31，历史记录]`：

| source 取值 | 条数 | 状态 |
|---|---|---|
| `eks-etl` | 1228 | 代码在写（handler.py 13 处），契约未声明 |
| `aws-etl-static` | 3 | 代码在写（handler.py:376），契约未声明 |
| `deepflow` | 8 | 代码在写，契约里叫 `deepflow-etl`——**同义漂移** |
| `manual` | 1 | 无代码在写，手工遗留 |
| **合计** | **1240** | 占当时全图 1802 条边 + 1063 节点的 **43%** |

同一份 YAML 文件里，**被** `assert_edge_type` 检查的那部分：**零漂移**。

> **9/5 现状更新**：这个缺陷已修完，且**存量也已归一**——活图谱里出现的 10 种 `source`
> 取值全部在契约声明的 12 个之内，未声明取值为 **0** `[实测 2026-09-05]`。
> 上面那张表是**当时**（8/31）的普查结果，**刻意保留原数字**：它是「门禁必须是代码」
> 这个论点唯一的定量证据，改成今天的 0 就没有说服力了。讲的时候要说清「这是修之前的状态」。

> 「所以漂移量和『声明写得好不好』完全无关，只和『有没有门禁』相关。这两组数字来自同一个
> 文件、同一个团队、同一段时间——唯一的差别是有没有人在运行时读它。」

#### 契约文件长什么样

`profiles/graph_contract.yaml`，**878 行**，**十**个顶层小节 `[代码 2026-09-05]`：

| 小节 | 管什么 | 大白话 |
|---|---|---|
| `version` | 契约版本号 | 改了声明要升版本，出错信息里会带 |
| `timestamp_field` | `last_seen` | 全项目统一用哪个字段表示「最后一次看到」 |
| `timestamp_legacy_aliases` | `last_updated` / `last_scanned` | 历史遗留的同义字段，读取侧还在用，记下来才能安全迁移 |
| `sources` | **12** 个合法来源 | 谁有资格往图里写、写的时候署什么名 |
| `edge_write_once_attrs` | 只许首写者写的边属性 | 见 4.4 的 write-once |
| `node_attr_authority` | 属性级权威**例外清单** | 同一个属性多个源都想写时谁说了算 |
| **`node_scope`** | **节点的归属域**（被观测系统 / 平台自身 / 部署脚手架 / 外部） | **9/5 新增的第五个维度**，见下 |
| `node_types` | **39** 种节点类型 | 每种的身份键、是否不可变 |
| `edge_types` | **29** 种边类型 | 每种的合法端点配对、是否是依赖边、过期阈值 |
| `edge_verification` | 证伪判据与阈值 | 见 5.3，阈值放这里而不是代码里 |

> **`node_scope` 值得单独讲 30 秒**：8/31 版本的文档把它列为「尚缺的维度」，9/5 已补上。
> 它解决的问题是：图里混着**被观测的系统**（petsite 业务）、**平台自身**（Neptune、5 个 ETL）、
> **部署脚手架**（CDK 的 kubectl provider Lambda）和**外部依赖**。不区分的后果很具体——
> 做影响面分析时会把「图谱平台自己的 Neptune」当成业务依赖，做容灾计划时会把 ETL Lambda
> 写进恢复步骤。`dr-plan-generator` 的范围锚定正是靠这个维度把平台设施排除出去的。

12 个合法 source 是：`aws-etl`、`eks-etl`、`aws-etl-static`、`cfn-etl`、`deepflow-etl`、
`deepflow-l4`、`deepflow-dns`、`nfm`、`xray`、`business-layer`、`manual-fix`、
**`agentcore-etl`**（9/5 新增，写 agent 层的三种依赖边）。`[代码]`

**为什么 source 要分这么细**：`eks-etl` 与 `aws-etl` 是刻意的语义区分——K8s API 和 AWS 控制面
是**不同的真值来源**；`aws-etl-static` 与 `deepflow-etl` 的区分是**不同的证据等级**
（配置里声明的关系 vs 运行时真的观测到的流量）。而 `deepflow` 和 `deepflow-etl` 是同一个源的
两个名字，这种必须收敛写入侧，**不能靠扩词表消化**——否则按源分派的下游逻辑
（`edge_verification._OBSERVER_MARKERS`）会漏判。`[代码注释]`

#### 门禁怎么执行：三种模式，一个环境变量

`infra/lambda/shared/python/graph_contract.py` `[代码]`

```
GRAPH_CONTRACT_MODE = enforce   ← 默认值
```

| 模式 | 违约时的行为 | 什么时候用 |
|---|---|---|
| `enforce` | **抛 `GraphContractError`，那一次写入失败** | 默认。静默写入未声明类型正是要消除的缺陷 |
| `warn` | 只 `logger.warning` 然后放行 | **灰度**：新接一个 ETL 或大改类型声明时先跑一轮，把真实违约摸清再切 |
| `off` | 完全跳过 | 只应在离线回放/单测夹具里用 |

实现只有三行，但语义要讲清 `[代码]`：

```python
def _violate(mode, msg):
    if mode == MODE_OFF: return
    if mode == MODE_ENFORCE: raise GraphContractError(msg)
    logger.warning("graph-contract: %s", msg)
```

模式是**每次调用都重读环境变量**（不是模块加载时读一次）——注释写明是为了让单测能用
monkeypatch 切换模式。

端点约束（「这种边的两头允许是什么类型」）有**独立开关** `GRAPH_CONTRACT_ENDPOINTS`，
也默认 `enforce`。为什么要独立：见下面「为什么保留这个开关」。

#### 五道校验，逐个用大白话说

| 校验函数 | 拦什么 | 举个会被拦住的例子 |
|---|---|---|
| `assert_node_type(label)` | 节点类型没声明 | 写 `Microservie`（拼错）→ 抛错 |
| `assert_edge_type(label, src, dst)` | 边类型没声明，**或**两头的类型组合没声明 | 写 `Deployment -[Manages]-> Deployment` → 抛错 |
| `assert_source(source, context)` | 来源不在 12 个词表内 | 写 `source='deepflow'`（应为 `deepflow-etl`）→ 抛错 |
| `identity_prop_for(label)` | 不是校验，是**派生**：告诉写入方该用哪个属性当身份键 | EC2Instance 返回 `instance_id`，见 4.5 |
| `filter_node_props(label, props, source)` | 这个来源没资格写这个属性 | 非 `aws-etl` 的源想写 Microservice 的 `az` → 被拒并 log |

**端点校验里有一个容易被忽略的设计**，值得单独讲 30 秒 `[代码注释]`：

平铺白名单（`src: [A, B]` + `dst: [C, D]`）的校验强度会**退化成笛卡尔积**。
实测后果：`Manages` 边为了容纳真实存在的 `HPA -> Deployment`，把 Deployment 加进了 dst 白名单，
结果**连 `Deployment -> Deployment` 这条本次查出的错源边也会被放行**。
所以两端都已知时一律走 `pairs` 配对列表：

```yaml
Calls:
  src: [LambdaFunction, Microservice]
  dst: [LambdaFunction, Microservice]
  pairs: [[LambdaFunction, LambdaFunction], [Microservice, Microservice]]   # ← 这个才是执法依据
```

`src`/`dst` 平铺字段保留只为兼容「只知道一端」的调用点。

#### 属性权威表：为什么是「例外清单」而不是「白名单」

`node_attr_authority` 现在的全部内容就这么点 `[代码]`：

```yaml
node_attr_authority:
  Microservice:
    az:                [aws-etl]
    fault_boundary:    [aws-etl]
    recovery_priority: [aws-etl, business-layer]
```

只有 3 个属性被登记。`may_write_node_attr()` 的逻辑是：**没登记的一律放行**。

这个方向选择是刻意的 `[代码注释]`：

> 「权威表是**例外清单**而非白名单，只登记已实测出冲突的属性。这样引入门禁不会把大量正常
> 写入判成违约。」

反过来做（白名单：没列出的就拒写）会让引入门禁这一步变成一次全量属性普查，
风险和工作量都不成比例，而且会把「这个源本来就没有这项数据」误判成违约。

被拒的属性**明示 log 而不是静默丢弃**——这一条直接抄 ServiceNow IRE 的 `maskedAttributes`：

```python
logger.warning(
    "upsert_vertex(%s, %s): 属性 %s 的权威来源不是 aws-etl，已拒绝写入。"
    "权威声明见 profiles/graph_contract.yaml 的 node_attr_authority。", label, n, masked)
```

> 「IRE 教给我们的不是『要有优先级表』——那个谁都想得到。是**『被拒的写入必须留痕』**。
> 不留痕的话，半年后有人问『这个 az 字段为什么是这个值、谁写的』，答案永远查不出来。」

#### 刻意不做的两件事（讲这个比讲做了什么更能建立可信度）

**一、不校验属性集。** 契约里没有「这种节点必须有哪些属性」这一项。理由写在代码里 `[代码注释]`：

> 「schema 文本里的属性列表是文档性的、且各源写入的属性子集本来就不同（xray 只补度量、
> cfn 只写 declared_in）。强制属性集会把『这个源没有这项数据』误判成违约。」

这一点用 4.1 那个真实节点的属性表可以直接印证：schema 声明 EC2Instance 有 7 个属性，
活图谱里实际有 24 个——多出来的是各源按自己能力补的度量。强制属性集会把这套多源协作打死。

**二、不由契约生成 schema 文本，两份声明平级并存。**
`profiles/graph_contract.yaml`（给机器）和 `profiles/petsite.yaml` 的 `graph_schema_text`（给人和 LLM）
**互不生成**，但**类型名集合必须完全相同**，由 `tests/test_35_graph_contract.py` 强制，
漂移即测试失败。`[代码注释]`

> 「这样就不会再出现『声明说 33 种、代码里写进去 34 种』。」

Lambda 运行时用的是第三份产物 `infra/lambda/shared/python/graph_contract_data.py`，
由 `scripts/gen_graph_contract.py` 从 YAML 生成，test_35 校验它没过期。
**三份文件、一个真值、测试保证同步**——这是「单一事实来源」在工程上的具体形态。

#### 从 warn 升到 enforce：方法论比结果重要

这一段值得讲，因为它示范了「怎么安全地给一个跑着的系统加约束」`[代码注释]`：

我们**没有**「跑一轮 ETL 看日志就升级」，而是对活图谱做了**全图三元组普查**——
覆盖全部 **1731 条边 / 85 种 `(srcLabel, edgeLabel, dstLabel)` 形态**。

> 「为什么不能靠跑一轮 ETL？因为**一轮 ETL 只会碰到它自己那部分形态**。
> 日志天然是一个不完整的样本，而且你不知道它漏了什么。普查是对**结果状态**取全集，
> 这才是可以拿来做升级决策的证据。」

普查结果与逐条处置：

- 违约 **204** 条
- 其中 **5 种形态（21 条）** 逐条核对确认**语义正确、只是 schema 漏声明** → 补进 `PAIR_ADDITIONS`
- 剩余 **183 条全部是同一个 bug** 造成的错源边（`find_vertex_by_name` 不带标签，见第 7 节证据一）
  → 改代码修根因 + `infra/fix_wrong_source_edges.py` 清存量
- 收紧成配对校验后又浮出 `LambdaFunction -> S3Bucket`（平铺白名单下被笛卡尔积掩盖的**合法**组合）
  → 一并补进声明

也就是说：**升级到 enforce 时，声明侧已无已知缺口。**

**为什么仍保留独立开关**（这一句是诚实性，别省）`[代码注释]`：

> 「普查只能看到**当下图里存在**的形态。低频 ETL 路径（例如周期很长的 Lambda）写的形态
> 可能当时不在图里，一旦 enforce 拒写会中断该步骤。真出现这种情况时设
> `GRAPH_CONTRACT_ENDPOINTS=warn` 先放行并收集，补进声明后再切回，不必回滚代码。」

#### 这一节如果只能记一句

> **声明要机器可读，门禁要在写入路径上，被拒要留痕，不确定的部分要留灰度开关。
> 四件事缺任何一件，这份声明都会在半年内变成注释。**


`profiles/graph_contract.yaml`，**878 行**，**39 种节点 / 29 种边**，是 ETL 写入门禁读取的权威。
`[代码]`

它约束五件事：
1. **允许的节点/边标签**——未声明的一律拒写（`assert_node_type` / `assert_edge_type`）
2. **端点配对**——每种边的合法 `(srcLabel, dstLabel)` 组合，不是平铺白名单而是**配对**列表
3. **身份键**（见 4.5）
4. **属性权威**（`node_attr_authority`）——**例外清单**，只登记已实测出冲突的属性；
   被拒的属性**明示 log** 而不是静默丢弃（对应 IRE 的 `maskedAttributes`：不明示，
   「谁赢」永远查不清）
5. **每类边的 `expires_seconds`**（见 4.7）

**为什么需要它**：在引入之前，**四个写入 ETL 无一 import profiles，运行时对标签零校验**——
任何拼错或新造的标签都会被静默写进 Neptune。`[代码]`

**从 warn 升级到 enforce 的过程本身是个方法论故事**（值得讲 30 秒）：

> 「我们没有『跑一轮 ETL 看日志』就升级，而是对活图谱做**全图三元组普查**——
> 覆盖全部 1731 条边 / 85 种 `(srcLabel, edgeLabel, dstLabel)` 形态。
> 因为一轮 ETL 只会碰到它自己那部分形态，日志天然是不完整的样本。」`[代码注释]`

普查结果与处置：违约 204 条 → 其中 5 种形态（21 条）核对为**语义正确、schema 漏声明**，
补进声明；剩余 **183 条全部是同一个 bug** 造成的错源边（见第 7 节证据一）。

仍保留 `GRAPH_CONTRACT_ENDPOINTS=warn` 开关的理由也讲一句：
**普查只能看到当下图里存在的形态**，低频 ETL 路径写的形态可能当时不在图里。

### 4.7 边的生命周期：两种失效语义，刻意不混（1.5 分钟）

引入 `graph_cleanup.py` 之前，失效机制是**四套各不相同、且三处缺失**的 `[代码]`：

| 源 | 机制 | 覆盖范围 |
|---|---|---|
| DeepFlow | 软删除 `active=false`，阈值 1800 s | **仅 `Calls`** |
| X-Ray | 软删除，阈值 6 h | **仅 `source='xray'` 的边** |
| AWS | **硬删除** `.drop()`，无时间阈值 | 仅约 14 种节点，**不含任何边** |
| CFN | **无** | — |

后果：`AccessesData` / `DependsOn` 写了 `active=true` 与 `last_seen`，
却**没有任何路径把 active 翻回 false**——观测停止后变成 ghost 边，
而影响面分析会把它们与真实依赖等权对待。

现在的做法，**两种失效语义分开**：

- **观测式失效**（`graph_cleanup` 负责）：只作用于 `dependency_kind='dynamic'` 的边，
  判据是超过**该边类型声明的** `expires_seconds` 未被刷新。
  TTL 按类型而非全局——「Pod 属于哪台机器」和「服务 A 调用服务 B」的合理过期时间差两个量级。
  例：`AccessesData` = 21600 s（取最长源窗口 = X-Ray 的 6h，否则会误杀 X-Ray 发现的边）。
- **声明式失效**（不在这个模块）：`static` 边表示「配置/模板声明了这条依赖」，
  **不该因为 DeepFlow 没观测到就置 false**——那正是 `drift_status=declared_not_observed`
  要表达的信息。

> 「**把两者混在一起会静默删掉真实的架构声明。** 所以代码里用
> `has('dependency_kind','dynamic')` 显式限定，而不是靠注释约定。」`[代码]`

### 4.8 存量清理：五个修复脚本讲的是同一件事（2 分钟）

**这一页的作用是承认「改代码只挡住新的，旧的还在库里」。** `infra/` 下五个脚本，
每个对应一个已进库的数据缺陷 `[代码]`：

| 脚本 | 修什么 | 讲台上的一句话 |
|---|---|---|
| `migrate_identity_keys.py` (247 行) | 身份键切换**前**的必备步骤：回填缺失的 id 属性、合并 id 撞车的节点 | 「**切换身份键这个动作本身会造重复**——库里没有那个 id 属性的节点，新 `mergeV` 匹配不到就新建一个」 |
| `merge_duplicate_ec2_nodes.py` (179 行) | 归并 4 个重复 EC2 实体，**迁移边而不是删** | 实测边数 42 vs 3、31 vs 3、**35 vs 16**——「后者那 16 条边直接删就是丢数据」 |
| `fix_wrong_source_edges.py` (158 行) | 清 183 条端点组合未声明的边，**契约驱动、不硬编码删什么** | 见第 7 节证据一 |
| `fix_property_cardinality.py` (325 行) | 规约顶点属性的多值累积 | 见下方，本页最值钱的一条 |
| `fix_neptune_data.py` (74 行) | 零散脏数据：`petfood` 恢复等级、RDS reader/writer 角色纠正 | 一次性手工修，如实说 |

**`fix_property_cardinality.py` 背后的缺陷值得单独讲 40 秒**，因为它是所有图数据库使用者
都会踩的坑：

> 「Gremlin/Neptune 的顶点属性**默认是 SET 基数**——不带 `Cardinality.single` 的写入是
> **追加**而不是替换。而读属性时 Neptune 按值组合**扇出成笛卡尔积**。」`[代码注释记录的实测]`

```
MATCH (n:LambdaFunction) RETURN count(*)                    -> 31     （不碰属性）
MATCH (n:LambdaFunction) WHERE n.last_scanned IS NOT NULL   -> 9 个真节点 / 1,253 行
```

最坏的单个节点累积了 **172 个不同的 `last_scanned` 值**（≈ etl_cfn 的运行次数）。

> 「注意这个 bug 的形状：**节点数是对的，一 join 到属性就炸成 40 倍。**
> 任何『先 count 看看对不对』的自检都发现不了它。」

**贯穿这一页的方法论**（比五个脚本本身重要）：

> 「五个脚本里有四个在注释里写了同一句话：**必须先部署写入侧修复，再跑清理。**
> 反过来的话，下一轮 ETL 立刻把脏数据造回来。
> **数据修复的顺序性是这类项目最容易付两次代价的地方。**」

### 4.9 Neptune 的部署形态与成本（1 分钟，管理层会问）

`[代码：infra/lib/neptune-cluster-stack.ts]`
- 引擎：**CDK 声明 1.3.4.0，活集群实测 1.4.6.3** `[实测]`——两者分叉，且该 CDK stack
  不在活跃 CloudFormation 栈中，需确认是有意手工管理还是漂移。**讲的时候按 1.4.6.3 说。**
- **预置型单 AZ**（不是 Serverless），实例 **db.r6g.large** 单写节点
- **IAM 认证开启**、存储加密开启、备份保留 7 天、备份窗口 18:00–19:00 UTC
- 私有子网 + 安全组只放通 Lambda SG 到 8182
- 代码里带了容量选型指引：`r6g.medium` < 10M 边 / `r6g.large` 10M–100M 边 /
  `r6g.xlarge` 100M–500M 边
- **`deletionProtection: false`**，注释写明"生产请设 true"——如实说，这是 demo 环境

> 「规模感：我们这张图**约 1300 节点 / 2600 边**，**在 r6g.large 上是完全过剩的**。
> 这里的成本不在图数据库，在观测数据管道。」

### 4.10 如果听众想照着建：实施顺序（1.5 分钟，这一页最容易被拍照）

**顺序本身是本项目踩出来的，不是理论推荐。** 中间四步我们都是补做的，代价见第 7 节。

1. **先定身份键，再写第一行 ETL。** 拿不可变 ID 当身份，`name` 一律降级为普通属性。
   补做的代价 = 重复实体 + 一个迁移脚本。
2. **先写机器可读契约，再接第二个数据源。** 单源时没有冲突，你会觉得契约多余；
   第二个源进来时冲突已经写进库里了。
3. **上运行时门禁，并且默认 enforce。** warn 模式的日志没人看。
4. **溯源三件套从第一天就写**：`source`（谁首先发现）、`dependency_kind`
   （static/dynamic）、`first_seen`，而且**写一次**。
5. **边的失效机制按类型声明 TTL**，动态/静态分开。别用全局阈值。
6. **接第二个观测源。** 单一观测源的假阴性率可以高到 85%（见 4.2）。
7. **把"覆盖率/一致性"做成测试**，不是做成看板。`tests/test_35_graph_contract.py`
   在契约与文字描述漂移时直接失败。
8. **最后才是证伪闭环**（第 5 节）——它需要前七步都在。

> 「反过来说：**如果只能做三件事，做 1、2、4。** 剩下的都能补，
> 身份和溯源补起来要迁移数据。」

---

### 4.11 这张图谁在消费（5 分钟，本次新增）

**为什么必须有这一节**：前面十小节讲的全是**怎么写进去**。听众到这里会问一句
「所以谁在读？」——如果答不上来，整个项目就变成一个精致的 ETL 作业。
第 0 节把定位写成「依赖的单一事实源」，这一节是那句话的兑现。

三个消费方，**共同点是都不经过 LLM 猜 Cypher**（对照见第 9 节）：

| 消费方 | 怎么读 | 读到什么 | 代码位置 |
|---|---|---|---|
| **AWS DevOps Agent** | MCP 协议，24 个只读工具 | 预置查询的结果，agent 自己决定调哪个 | `mcp/` |
| **容灾计划生成器** | 进程内 SDK（openCypher） | 拓扑 + scope + 数据层，生成可执行切换计划 | `dr-plan-generator/` |
| **RCA 引擎** | 进程内 SDK | 爆炸半径、上游候选、历史事件 | `rca/` |

#### 4.11.1 MCP server on AgentCore：给 agent 的入口

**部署形态（实测）**：

| 项 | 值 |
|---|---|
| AgentCore Runtime | `graph_dependency_mcp-12Vg2Z9XXu` · 状态 `READY` |
| 协议 | `serverProtocol: MCP` |
| 网络 | `networkMode: VPC`，两个 PetSiteVPC 子网（能直连 Neptune） |
| 执行角色 | `GraphDpMcpAgentCoreRole` |
| Endpoint | `DEFAULT` · `READY` |
| 暴露工具 | **24 个，全部 `toolClassification: READ_ONLY`** |

**三个设计取舍，每个都能讲 30 秒：**

**① 为什么选 AgentCore Runtime 而不是 Gateway。** Gateway 会自己生成
`initialize.instructions`，而我们要往里注入**证据纪律**（agent 必须报出这条结论
来自哪条边、哪个 source、验证状态是什么）。Runtime 的 instructions 完全可控，
Gateway 的注不进去。这不是偏好问题——没有证据纪律，agent 会把图谱当成
可以润色的素材。

**② 工具白名单是独立于 IAM 的第二个权限平面。** 24 个工具在
`mcp/devops-agent-association.json` 里逐个显式声明 `READ_ONLY`。
理由写在 `mcp/README.md`：**不能只依赖 IAM 收紧**——IAM 管的是「这个身份能不能
调 Neptune」，工具层管的是「agent 能不能调到那个会写的工具」。两个平面漏一个都不行。

**③ 工具是「预置查询」而不是「自然语言转 Cypher」。** 24 个工具对应
`QUERY_CATALOG` 里 24 条确定性查询，agent 的自由度在于**选哪一条**，不在于
**写什么查询**。对照第 9 节：NL→Cypher 会让结论不可复现，而 RCA 结论必须可复现。

> 讲法：「这张图最终是给 agent 看的。但**我们不让 agent 写查询**——
> 它只能从 24 个预置查询里挑。自由度在选择，不在生成。」

#### 4.11.2 容灾计划生成器：图谱最重的消费方

**它是三个消费方里代码量最大的**：`planner/step_builder.py` 45.6 KB、
`planner/plan_generator.py` 30.8 KB、`validation/plan_verifier.py` 26.8 KB、
`executor_strands.py` 29.8 KB，加 16 个测试文件。

它读图谱的四件事，每件都对应一个前面讲过的维度：

| 读什么 | 用前面哪个维度 | 干什么 |
|---|---|---|
| 依赖拓扑 | 边类型 + `dependency_kind` | Kahn 拓扑排序定切换顺序 |
| 范围锚定 | **`scope`** | 把平台自身设施、部署脚手架排除出恢复步骤 |
| 数据层 | `AccessesData` / `WritesTo` | RPO 按拓扑推导，不靠人填 |
| 关键路径 | 最长路 DP | RTO 估算与并行分组 |

**最值得讲的一条：`graph/snapshot.py` —— 离线快照。**

它直面一个必被问到的质疑：**「你的图谱本身挂了，容灾计划怎么办？」**
答案是计划生成时把所需子图落成快照文件，**灾时执行不依赖 Neptune 可用**。
这条如果不主动讲，附 A 的质疑预演里会缺一块。

另外三个 9/5 新增件也在这一节顺带提到就够：`graph/scope.py`（范围白名单锚定）、
`planner/preflight.py`（就绪检查，19.8 KB）、`assessment/rpo_estimator.py`
（RPO 按拓扑推导）。

**已生成的样例可以直接投屏**：`dr-plan-generator/examples/` 下有三份，
`region-switchover-apne1-to-usw2.md`（32 KB）是最完整的一份。

> 讲法：「容灾计划是这张图最重的消费方，也是最能证明图有用的一个——
> **它的每一步都必须能执行**，图错一条边，计划里就多一个错命令。
> 所以它反过来是图谱质量最严格的检验方。」

#### 4.11.3 一句话收束

> 「三个消费方，**没有一个是拿 LLM 去猜 Cypher 的**。
> agent 侧走 24 个预置工具，另两个走进程内 SDK。
> 这是刻意的：图谱是判据来源，判据必须可复现。」

#### 4.11.4 反过来：agent 也是被观测对象（30 秒，2026-09-05 补）

**同一个 AgentCore 上跑着两类东西，图谱必须能分开。**

| runtime | scope | 角色 |
|---|---|---|
| `graph_dependency_mcp` | `platform` | 本平台自己的 MCP server（消费图谱） |
| `WaggleAIOrchestrator` 等 5 个 WaggleAI\* | `observed` | **PetSite 的 AI 问答业务功能**（被图谱观测） |

PetSite 的 AI 问答链路：`WaggleController.cs` 读 SSM
`/petstore/agent/waggleairuntimearn` → `InvokeAgentRuntimeAsync` →
`WaggleAIOrchestrator` → 委派给 Nutrition / Adoption / Concierge / Ordering，
配 `WaggleAIGuardrail` 与 `WaggleAIMemory`，Nutrition 还 `Retrieves`
一个 KnowledgeBase。

> 讲法：「这一页是 scope 那个维度**为什么必须存在**的证明。同一种节点类型
> （AgentRuntime）里，一个是我们的工具、五个是被观测的业务功能。
> 按类型一刀切会把业务功能当平台设施排除掉——我们真的犯过这个错，见附 D。」

这一跳此前**不在图谱里**（agent 子图是孤岛），第 8 节记了它为什么五个数据源
都看不见、以及现在靠 SSM 声明补上的那条边证据等级只到 `static`。

---

## 5. 我们的答案：用故障注入证伪自己的图（6 分钟）

### 5.1 机制，一句话

> 「对边 `A -> B`，我们**在 B 注入故障，观测 A**。A 显著退化 => 这条边被确证，
> 还顺带得到影响强度。A 毫无反应 => 这条边存疑。」

必须讲清的反直觉点（全场最容易被挑战处）：

> 「**注意不是在 A 注入。** 打断 B 之后 B 自己退化，这近乎恒真，什么也没验证。
> 我们自己的代码最初就是这么写的——72 个历史实验**全部 passed、零失败**，
> 因为判定门槛根本没有分辨力。」

**主动招认这个的性价比极高**：它同时证明你真跑过、你知道哪里容易错、结论不是纸上推演。

### 5.2 实现：六个阶段（1.5 分钟）

`[代码：chaos/code/runner/runner.py]`

| 阶段 | 做什么 |
|---|---|
| phase0 preflight | 策略门禁（fail-closed，拒绝即中止）、目标解析、稳态前置检查 |
| phase1 steady_state_before | 采注入目标 + **观测方**基线（3 采样 × 60 s 窗口取均值） |
| phase2 inject | 通过 Chaos Mesh CRD 注入 |
| phase3 observe | 周期采样注入方与观测方；命中 stop condition 立即熔断；**末尾做边验证写回** |
| phase4 recover | 删 CRD、验证恢复 |
| phase5 steady_state_after | 恢复确认 |

**候选边选择是图查询，不是 LLM 猜** `[代码：edge_verification.py:114]`：

```groovy
g.V().has('name', <injection_target>).inE(<依赖边类型>)   // 只取入边
 .project('eid','label','observer','props')
```

> 「只取**入边**是刻意的：在 B 注入不会告诉你 B 依赖谁。
> 原实现把同一个判定写给注入目标的**所有出边和入边**——那不是不精确，那是伪造证据。」

### 5.3 判据与置信度：阈值在契约里，不在代码里（2 分钟）

`[代码：profiles/graph_contract.yaml → edge_verification]`

```yaml
statuses: [untested, confirmed, refuted, inconclusive]
thresholds:
  min_observation_requests: 20        # 观测方基线与注入期都要 >= 20 请求
  confirm_degradation_pct: 20.0       # 退化 >= 20pp -> confirmed
  refute_degradation_pct: 5.0         # 退化 <= 5pp  -> refuted
  stale_verification_seconds: 2592000 # 30 天后判定过期
evidence_weights:                     # log-odds 累加后 sigmoid 到 [0,1]
  static_declaration: 1.0             # 每个静态声明源 +1.0（先验）
  observed_per_source: 0.5            # 每个观测源 +0.5（似然）
  observed_cap: 1.5                   # **观测证据总量封顶**
  intervention_confirmed: 4.0         # 干预确证，可翻转先验
  intervention_refuted: -4.0
```

**三个要讲的设计**：

1. **观测证据封顶。** 依据 arXiv:2607.09449 关于「样本越多越容易被虚假相关性诱导出假边」
   的临界阈值 `[论文]`。
   > 「不封顶的话，**一条假边只要 ETL 跑得够久就会变得『高置信』**。」
2. **判定顺序先查流量，再看退化。** 因为 `metrics.collect()` 无数据时 fallback
   `success_rate=100.0 / total_requests=0`——**零流量与健康长得完全一样**。
   不先设请求量下限，一条没流量的边会被判 refuted。
3. **中间带（5%–20%）判 inconclusive。** 重试/熔断/缓存会让真实依赖只表现出轻微退化，
   判 refuted 会删掉真实边。**本模块只标注，从不删边**——`refuted` 是给人看的证据，
   不是删除指令。`[代码]`

判定写回边的属性：`verify_status` / `verify_confidence` / `verify_last` / `verify_by` /
`verify_experiment` / `verify_degradation` / `verify_reason` / `verify_confirm_count` /
`verify_refute_count`，**权威源只有 `chaos-runner`**——ETL 不得写：
「静态采集若能覆盖 `verify_status`，等于用先验抹掉干预后验，而干预是唯一能证伪一条边的证据。」
`[代码]`

### 5.4 差异化定位（30 秒）

> **Datadog / Dynatrace / New Relic 都没有故障注入后端，做不了这件事。
> 差异化不在于我们有一张依赖图——他们都有。在于这是唯一能证伪自己那张图的系统。**

学术支撑给一条就够：**arXiv:2506.11176**（ICSE'26 NIER）已跑完「trace 抽图 → Monte-Carlo
仿真 → 真实 chaos 对照」，有副本场景 **0.305 vs 0.305**（MAE ≤ 0.0004）。
而它亲口承认的局限正是反用它的动机：**「我们假设已知所有依赖边；漏掉一条边会导致模型高估韧性。」**
`[论文]`

---

## 6. 现场演示（10 分钟，准备三段、按时间砍）

**必演 —— 起点基线（30 秒，一条查询）** `[实测 2026-09-05]`
```
依赖边总数 115   按状态 {confirmed: 13, inconclusive: 25, 未标注: 77}   已确证占比 11%
强度已分级 9     {hard: 1, degraded: 3, unclassified: 5}
```
> 「**约 10% 的边经过干预确证，25 条明确标着『试过但无法验证』，其余还没轮到。
> 在座各位的系统，这三个数分别是多少？**」
>
> ⚠️ **8/31 版本这里演的是「94 条 / 100% 未验证 / 覆盖率 0.0%」，那个数字已经不成立。**
> 现在的版本杀伤力不减反增：**能说出「25 条我试过但验不了」比说「我全没验过」更专业**——
> 后者听起来像还没开始做，前者说明你已经撞到了方法的边界并如实记录。

**必演 —— 图查询驱动选边（1 分钟）** `[实测 2026-09-05]`
**25 条**边 `drift_status = declared_not_observed`：CFN 声明了服务访问数据库，
但 DeepFlow / X-Ray 从未观测到。另有 **1 条** `observed_then_silent`——曾观测到、之后静默。
> 「要么是死代码路径，要么观测是瞎的。**两种都是真问题**，分清就有价值。
> 而 `observed_then_silent` 那一条比另外 25 条更值钱——**它区分了『从来没见过』和
> 『见过又不见了』**，后者往往是真实的下线或链路中断。」

**可砍 —— 一次真实注入（4 分钟，风险最高）**
`search-service` 注入 `http_chaos abort`，观测 `petsite` + `list-adoptions`。
**务必提前录屏**：依赖活集群、活流量、Chaos Mesh 全健康，现场翻车概率不低。

**备用 —— 契约门禁（时间紧就用这个替代注入）**
拼错的 `EC2Instancee`、`Callzz` 被运行时拒绝。演示成本低、100% 可复现。

**可选加演 —— 5 页 Streamlit demo**：Graph Explorer（拓扑可视化）、Smart Query
（自然语言→图查询）、Root Cause Analysis（Graph RAG）、Chaos Engineering（实验历史 + 执行）、
DR Plan（切换计划生成）。`[代码：demo/]`

---

## 7. 实测证据：为什么这套方法能查出别的方法查不出的东西（8 分钟）

这一节是真正的护城河，因为**没有一个是读代码或看架构图能发现的**。

**证据一：183 条边的源端点是错的**
`find_vertex_by_name(name)` 两层都不带标签，而这张图里名字跨标签重复是常态——实测 **12 组**，
`gateway-service` 同时是 Deployment、K8sService、Microservice 三个节点。
返回哪个取决于**缓存插入顺序**，也就是 ETL 步骤先后。
后果：正确的 `Microservice-[RunsOn]->Pod` 只有 **36** 条，错源的 **173** 条。
**影响面分析从 Microservice 出发会漏掉大部分 Pod。**

**证据二：SLI 把 DNS 失败算进了 HTTP 成功率**
`list-adoptions` 报 31.37%、`search-service` 报 69.16%，看着像系统坏了。实际是 K8s 默认
ndots=5 的搜索域展开产生的 **NXDOMAIN** 被计入服务成功率。加协议过滤后三方全 **100%**。
（5 分钟窗口：HTTP 20,645 条全部成功；DNS 16,856 条里 13,516 条 NXDOMAIN）`[代码注释记录的实测]`
> 「杀伤力在于：**噪声底盘既大又随 DNS 行为波动**。一次真实的 20 个百分点退化会被它淹没，
> 或者被它伪造出来。」

**证据三：`abort` 故障下成功率是盲的**
实测注入目标成功率**全程 100%**，而请求量 **2240 → 56（-97%）**。
被中断的请求根本不产生 response 行，而 SLI 查询带 `response_duration > 0` 过滤。
> 「如果只看成功率，会得出『注入没生效』的结论，然后把一条真实的边判成不存在。」
> 现在的修法是用**合成退化率**：成功率下降与吞吐塌陷取 max。`[代码]`

**证据四（本次查出，已修）：`source` 词表声明了但没有门禁——真正的伤在下一层** `[实测]`
契约 `sources:` 声明 9 个合法值，活图谱里实际出现 **13 种**，未声明的共 **1240 条**：
`eks-etl`（1228 条边）、`deepflow`（8 个节点）、`aws-etl-static`（3 条）、`manual`（1 条）。
> 「标签有门禁，`source` 值没有。同一份 YAML 里**被检查的那部分零漂移，没被检查的漂了 1240 条**。
> 这比任何论证都能说明：**门禁必须是代码，不能是文档。**」

顺着这条查下去，带出一个更严重的连带缺陷：**`upsert_edge` 会丢弃调用方传入的 `source`**。
写一次语义被实现成「硬编码 `aws-etl` + 跳过调用方的同名属性」，于是 handler.py 里
**13 处 `eks-etl` + 1 处 `aws-etl-static` 全部静默失效**，新建的边一律写成 `aws-etl`。
实测确认：`upsert_edge(..., {'source':'eks-etl'})` 生成的 Gremlin 里只有 `'aws-etl'`。

> 「这个 bug 的形状值得单独讲：**在活图谱里看不出来。** `coalesce` 保护了存量边，
> 所以那 1228 条 `eks-etl` 还在那儿好好的。它只影响此后新建的边——
> **不会立刻暴露，只会让 provenance 缓慢腐坏。**
> 一个『看数据看不出来、要读代码才发现』的缺陷，恰好是守门测试存在的理由。」

两者已一并修完：`assert_source()` 门禁 + 首写值采纳调用方取值 + 8 个守门用例
（含一条静态扫描，兜住 deepflow/xray/cfn 那些没有运行时收口点的手拼 Gremlin），
全量 461 → **469 passed / 0 failed**。其中 `deepflow` 判为 `deepflow-etl` 的**同义漂移**，
收敛写入侧而不是扩词表——否则按源分派的逻辑（如 `_OBSERVER_MARKERS`）会漏判。

**证据五：节点数是对的，一 join 属性就炸成 40 倍**
Neptune 顶点属性默认 SET 基数，漏写 `Cardinality.single` 的写入是追加而非替换，
读取时按值组合扇出笛卡尔积。实测 `LambdaFunction` 直接 count 是 **31**（正确），
一旦 `WHERE n.last_scanned IS NOT NULL` 就变成 **9 个真节点 / 1,253 行**；
最坏单节点累积 **172 个** `last_scanned` 值。`[代码注释记录的实测]`
> 「**任何『先 count 一下看看对不对』的自检都发现不了它。**
> 而所有报表、所有 join、所有 LLM 读到的上下文都是错的。」

**收口成一条方法论**（务必讲，它把故事升维成洞察）：

> 「这个项目至今查出的缺陷，**没有一个是『少采了数据』**。全部落在三类：
> **粒度错配、写了但没人读、身份不唯一**。所以瓶颈在**数据契约**，不在采集覆盖面。
> 规划重心应该是把这三类变成守门测试，而不是继续接新数据源。」

---

## 8. 诚实的边界（3 分钟，别跳过）

**跳过这节，前面所有数字的可信度都会打折。** 主动说的限制，听众会当成严谨；
被问出来的，会当成隐瞒。

- **验证覆盖率：126 条合法依赖边里 13 条确证、25 条无法确证、其余未尝试；强度维度只覆盖 9 条**
  `[实测 2026-09-05]`。**8/31 时这个数是 0.0%，现在不是了——但仍然很低，而这不是要藏的事。**
  三档的含义必须分开说：
  - `confirmed`（13）＝干预实测确认依赖成立
  - `inconclusive`（25）＝**已尝试但无法施加有效检验**（Chaos Mesh 打不到 Lambda、
    目标已缩容到 0、源不在集群内）。它**不累计证伪计数**，所以不会因为「多次没验成功」
    被误删——「没能验证」与「验证为假」在系统里是两件不同的事
  - 未尝试（72）＝还没轮到
  > 「只有约 10% 的依赖边有干预确证。**但这个数字比『我们覆盖率 100%』可信得多**——
  > 后者只要被抽查命中一条假边，整张图的可信度就归零。」
- **强度维度对无法注入的依赖永久无法分级。** 托管服务（Lambda）、已缩容服务的
  `verify_dependency_class` 会长期停在 `unclassified`（实测 9 条已分级里有 5 条是它）。
  这是结构性限制，不是排期问题。
- **`source` 词表门禁已补上，存量也已归一** `[实测 2026-09-05]`：活图谱里出现的 10 种
  `source` 取值**全部在契约声明的 12 个之内**，未声明取值为 **0**。
  （8/31 版本此处记录的是「代码已挡住新的、旧的还在库里」——存量归一已完成。）
- **`awesomeshop` 命名空间的 6 个服务副本数全部为 0，但名字在图谱里** `[实测]`。
  `auth-service` / `frontend` / `gateway-service` / `order-service` /
  `points-service` / `product-service` 均 `replicas=0`、`readyReplicas` 为空——
  DeepFlow 曾采集到它们的流量，所以边是**真的曾经存在**，但运行时现在不存在。

  > 「这是『图里有边但服务不在』最干净的一个例子。**它不是脏数据**——那条边当时是真的。
  > 这正是我们为什么要有 `live = dynamic AND active=true` 这个过滤器：
  > `dependency_kind` 只区分声明与观测，答不了『它现在还在不在』。」

  实测后果：petsite 的三个上游依赖全是 `dynamic`，但其中**两个属于 awesomeshop
  且副本数为 0**——只有按 `live` 过滤才返回正确的空集。做影响面分析或容灾计划时
  必须排除，否则会把不存在的服务写进恢复步骤。

- **三个数据库集群全部单实例：无读副本、无跨区副本** `[实测]`。
  Aurora PostgreSQL 16.11、Aurora MySQL 8.0、Neptune 1.4.6.3 均如此
  （Neptune 是 `petsite-neptune-instance-1` / db.r6g.large 单实例）。

  > 「这直接决定容灾能力上限。**第 4.11.2 节讲的容灾计划生成器能算出最优切换顺序，
  > 但底层根本没有副本可切**——计划的价值在于它诚实地告出这一点，而不是假装能切。」

  这条必须和容灾计划一起讲，否则会被当场问住。Neptune 单实例还有一个与图谱自身
  相关的后果：**实例级故障即全平台读写不可用**，这也是 4.11.2 那个离线快照存在的理由。

- **~~21 条~~ 5 条边被写了 `dependency_kind`，而它们的边类型声明为 `dependency: false`**
  `[实测 2026-09-05，本条已部分修复，讲的时候按新版讲]`：

  | 边类型 | 写入方 | 条数 | `first_seen` | 现状 |
  |---|---|---|---|---|
  | ~~`Invokes`~~ | `aws-etl` / `cfn-etl` / `aws-etl-static` | ~~16~~ | null | **已消解**：契约裁定 `Invokes` 本就是依赖边，已改 `dependency: true` |
  | `RoutesTo` | `agentcore-etl` | **5** | 有值 | **仍越界** |

  **`Invokes` 那 16 条的处置方式本身就是讲点**：我们没有去堵写入方，而是**判定契约标错了**——
  `StepFunction → LambdaFunction`、`SNSTopic → LambdaFunction` 按任何定义都是依赖关系，
  把它标成 `dependency: false` 才是错的那一侧。

  > 「这里有个容易做错的选择。发现『非依赖边带了依赖属性』，直觉是加一道门禁把它挡掉。
  > **但那样会把建模错误变成静默行为**——将来任何一条真依赖被误标，都会被门禁悄悄挡住、
  > 永远没人发现。我们加的门禁只挡『属性写错位置』，同时把分类问题单独立卡去裁决。」

- **`first_seen` 大面积缺失：131 条带 `dependency_kind` 的边里只有 48 条有 `first_seen`（37%）**
  `[实测 2026-09-05，本次核对新查出，比上一版记录的严重得多]`。

  上一版这里记的是「`Invokes` 那 16 条连 `first_seen` 都是 null」。按全图重查之后
  发现**这不是那 16 条的问题，是普遍现象**：

  | 边类型 | 带 `dependency_kind` | 其中有 `first_seen` |
  |---|---|---|
  | `AccessesData` | 56 | 21 |
  | `DependsOn` | 23 | **2** |
  | `Calls` | 20 | 9 |
  | `Invokes` | 16 | **0** |
  | `InvokesTool` / `Delegates` / `Retrieves` / `RoutesTo`（均 `agentcore-etl`） | 16 | 16 |

  `first_seen` 与 `source`、`dependency_kind` 同属契约声明的 `edge_write_once_attrs`，
  **本应首写必填**。规律很清楚：**只有 `agentcore-etl` 这个最新写的 ETL 100% 写全了**，
  越老的写入路径缺得越多（`DependsOn` 只有 2/23）。

  > 「这条比我们上一版记的严重。**溯源三件套里第三件在多数边上根本没写**，
  > 所以『这条依赖是什么时候第一次被发现的』这个问题，图谱现在答不了 63%。
  > 讲第 4.10 节实施顺序时我说过『溯源三件套从第一天就写』——
  > **这条建议正是从这个缺口来的**，我们自己没做到。」

  **已知，未修。** 与「同一类错误第二次发生说明当时的修法只补了那一个洞」是同一个形状：
  `source` 那次补了词表门禁，但没有人给 `first_seen` 加同样的必填校验。
- **12 条边 `source` 为空** `[实测]`（`Calls` 11 条 + `AccessesData` 1 条），
  而 `source` 属于 `edge_write_once_attrs`，本应必填。这是 4.4 那个「写一次」机制的反例，
  **已知，未修**。
- **230 条边 / 209 个节点没有 `source`**，多为 rca 与 chaos 自产实体。
  是否该强制尚未判定——草率地设 required 会逼出一堆无信息量的 `source='rca'`。
- **`abort` 会在目标 Pod 的网络命名空间留残留**，容器重启清不掉，只能删 Pod 重建。
  这是我们踩出来的，Chaos Mesh 文档没写。
- **Chaos Mesh 的 `PodChaos/pod-failure` 在 K8s 1.35 上完全打不进去** `[实测]`：
  它的实现是往运行中的 Pod 插 pause initContainer，而 K8s 禁止修改已存在 Pod 的
  `spec.initContainers`。**危险在于探测结果会全绿**——若不先查 `AllInjected`，
  会把每条边错判成 refuted。这是判定链里「注入生效门禁」存在的直接理由。
- **`Namespace` / `Pod` / `K8sService` / `Deployment` 的身份键未按 namespace 限定**，
  契约里明写了 `scope_note`——多集群或多 namespace 同名会碰撞。**已知，未修。**
- **重试 / 熔断 / 缓存会掩盖真实依赖**，注入时长必须超过熔断窗口加重试预算才能穿透 fallback。
  做不到就会把真实边判成不存在——**比不验证更有害**。
- **零流量和健康在指标上完全一样**，所以规则是：零流量一律判 inconclusive，**绝不判 refuted**。
- **测试规模（按仓库要求的 Python 3.11 实测）**：**650 passed / 0 failed / 150 skipped**，
  **零收集错误** `[实测 2026-09-05]`。150 个 skipped 不是小数——golden / shadow / live
  类用例需要真实凭证或 Neptune 才实际执行，**所以「全绿」的含金量要打折,
  讲的时候要连 skipped 一起报**。
  （8/31 记录为 605 个 / 53 文件 / 461 passed。）
- **上一版这里写的「754 个用例 / 69 个文件 / 2 个文件收集报错」是错的,而且错得有教育意义。**
  那是用 **Python 3.9** 跑出来的:仓库 conftest 明确要求 3.10+，在 3.9 下
  `test_12_unit_etl_aws.py` 撞 `markupsafe.soft_unicode` 版本冲突、
  `test_22_ui_streamlit.py` 没装 `streamlit`,两者都是**环境问题**。
  > 「这条要讲。我们拿一个错版本的解释器跑测试,然后把**环境问题当成代码缺陷**
  > 写进了自己的『诚实的边界』——**一份声称如实记录缺陷的文档,自己报了两个假缺陷**。
  > 教训不是『要用对版本』那么浅:**任何自动采集的质量数字都要连采集环境一起记录**,
  > 否则数字会以看不出来的方式失真。」
- **LLM 混沌假设的可靠性没有公开量化指标。** 唯一有完整闭环的公开系统 ChaosEater
  （NTT, ASE'25 NIER）自己的验证是「由人类工程师和 LLM 定性验证」——**没有假设正确率或
  幻觉率**。这是需要自建评测补的空白。`[论文]`

---

## 9. GenAI 用在哪、不用在哪（3 分钟，会被问，先讲掉）

- **LLM 做**：假设生成与排序、实验代码脚手架、结果的自然语言归纳、自然语言转图查询
- **LLM 不做**：稳态是否被违反的**最终判定**（交统计判据）、**原始遥测直读**（先确定性聚合）、
  **候选边选择**（图查询算）

实现上，六条 LLM 路径的默认引擎都是 Strands（`CHAOS_RUNNER_ENGINE` / `POLICY_GUARD_ENGINE`
等环境变量默认值即 `"strands"`），并接入了 AgentCore Observability。`[代码]`

理由给证据，不给态度：ReAct 的幻觉常导致无关动作并**污染后续结果**；
LLM 直读高 volume 遥测会超出上下文容量，而推理失败会被复杂多 agent 管线掩盖。

反面教材讲一个，很有说服力：参考仓库 `aws-samples/sample-aws-resilience-skill` 用 Mermaid 图 +
`ID->ID` 字符串表示依赖，**SPOF 全靠 LLM 人工推理、爆炸半径手工估算**。
> 「方向应该反过来：**用图查询自动算爆炸半径和 SPOF 来驱动实验选择**，而不是让 LLM 画图。」

我们的对照实现是 openCypher `[代码：dr-plan-generator/graph/queries.py:166]`：
```cypher
MATCH (resource)<-[:AccessesData|DependsOn|WritesTo|RunsOn]-(svc)
WITH resource, collect(DISTINCT svc.name) AS services, count(DISTINCT svc) AS svc_count
WHERE svc_count >= 2
  AND size([(resource)-[:LocatedIn]->(az:AvailabilityZone) | az.name]) = 1
RETURN resource.name, labels(resource)[0], services, svc_count ORDER BY svc_count DESC
```
> 「SPOF 的定义在这里是**可执行的**：被 ≥2 个服务依赖，且只落在 1 个 AZ。
> 不是 LLM 的判断，是一条查询。**写入用 Gremlin、读取用 openCypher**，同一个 Neptune。」

---

## 10. 收尾（1 分钟）

不要总结，留一个能带走的东西 + 一个反问：

> 「带走一句话：**一张不能被证伪的依赖图，本质是一份没人负责的文档。**
>
> 留一个问题给各位：**你们的图，上一次被证伪是什么时候？**」

---

## 11. 按听众适配（讲之前做这个删改）

| 听众 | 砍什么 | 必须加什么 | 时长 |
|---|---|---|---|
| **技术同行 / 架构评审** | 不砍。第 4 节讲全，第 7 节可加到 5 个证据 | 4.4 的代码页、4.10 的实施顺序页 | 60 分钟 |
| **平台/SRE 团队（想照做）** | 砍第 2、3 节到各一页 | **第 4 节全部 + 4.8/4.10 加时**，成本与运维负担 | 45 分钟 |
| **客户售前** | 砍 4.4–4.8 只留 4.2 数据源表，第 7 节留 1 个证据，砍论文引用 | 「这对你意味着什么」：影响面分析可信度、变更评审、演练选靶 | 25 分钟 |
| **管理层 / 决策者** | 砍第 2 节到一句话，砍全部实现细节 | 成本（4.9）、风险（非生产验证过什么）、里程碑 | 12 分钟 |

**管理层版开场换成这句**：
> 「我们的影响面分析里，有 83% 的一类边指向了错误的源。这不是数据没采够，
> 是数据契约没有守门人。」

**SRE 版开场换成这句**：
> 「我们接了五个数据源。**最有价值的产出不是更全的图，是这五个源互相打架的地方。**」

---

## 附 A：常见质疑预演

| 会被问 | 怎么答 |
|---|---|
| 「同一属性两个源写，谁赢？」 | 最容易被问倒的一点。IRE 的答案是一张四列表；我们边级已有 `source` + `dependency_kind` 分层，节点属性级有 `node_attr_authority` **但它只是例外清单**（目前只登记了 Microservice 的 3 个属性）——直说没做完 |
| 「为什么不用 Serverless Neptune？」 | 单 AZ 预置 r6g.large 对约 2600 条边过剩，成本不在图库。**Serverless 是合理选项，我们没测**，别硬编理由 |
| 「Gremlin 还是 openCypher？」 | **写用 Gremlin（`mergeV`/`coalesce` 幂等语义直接）、读用 openCypher（路径与聚合可读性好）**，同一个 Neptune。这是刻意分工，不是历史包袱 |
| 「逐条写入不慢吗？」 | 约 2600 条边规模下不是瓶颈；deepflow ETL 做了批量合并（~700 次请求降到 20–30 次）。**上到十万级边要换 bulk loader，我们没到那个规模，别声称验证过** |
| 「粒度不够细怎么办？」 | 行业通病。**Datadog 自己的优先级链是 `peer.db.name > peer.aws.s3.bucket > peer.hostname`**——它的 S3 粒度同样取决于插桩是否报了 bucket 名。我们的对应做法是 `AWSServiceEndpoint` + `granularity='service'`，**不把 X-Ray 的 `S3` 猜成某个具体 bucket** |
| 「注入会不会搞坏生产？」 | 非生产集群 + 观测方护栏 + stop condition + CRD 删除。**并说清踩过的 netns 残留** |
| 「这套要多少人维护？」 | 诚实给：ETL 五条流水线约 7,519 行、契约一份 878 行、实验规格按边类型。**不要报一个显得很轻的数字** |
| 「你们只有 6 个微服务，规模上去还成立吗？」 | 方法成立、数字未验证。**证伪闭环的成本随边数线性增长**，所以选边必须靠图查询排优先级（爆炸半径 × 未验证）而不是全量扫 |

## 附 B：三件绝对不要做的事

1. **不要以架构图开场。** 39 类节点 29 类边的图会让听众进入「又一个 CMDB」模式，
   你后面再也拉不回来。**架构图放到 4.1，且先讲"约 2600 条边里只有 126 条是依赖"。**
2. **不要报没测过的数字。** 你手上全是实测数据，这是最大资产——一个估算数字会污染全部。
   **本文数字的时效是 2026-09-05；8/31 到 9/5 之间节点 +25%、边 +46%，说明这些数字会漂。
   讲之前重跑一遍查询。**
3. **不要再用「已验证覆盖率 0.0%」当开场素材——那个数字已经不成立了。**
   8/31 版本建议拿「在座所有人的系统这个数都是 0」开场，现在我们自己约 10%（13/126），
   继续那样说就是报错数字。**改用这个说法**：
   > 「我们的依赖图有约 10% 的边经过干预确证，25 条明确标着『试过但无法验证』，
   > 72 条还没轮到。**在座各位的系统，这三个数分别是多少？**」
   同样有杀伤力，而且是真的。

## 附 C：关键实现索引（讲完被要代码时给这张表）

| 主题 | 文件 |
|---|---|
| 幂等 upsert / 身份键 / 写一次溯源 | `infra/lambda/etl_aws/neptune_client.py` |
| SigV4 + Gremlin HTTP 客户端（Lambda Layer） | `infra/lambda/shared/python/neptune_client_base.py` |
| 图谱契约（权威声明） | `profiles/graph_contract.yaml`（**878 行 / 39 节点 / 29 边 / 12 source**） |
| 运行时写入门禁 | `infra/lambda/shared/python/graph_contract.py` |
| 置信度与判定（纯函数） | `infra/lambda/shared/python/graph_confidence.py` |
| 边生命周期收敛 | `infra/lambda/shared/python/graph_cleanup.py` |
| AWS/EKS/CloudWatch 采集 | `infra/lambda/etl_aws/`（handler + collectors/ + cloudwatch.py） |
| DeepFlow L7/L4/DNS + 漂移判定 | `infra/lambda/etl_deepflow/neptune_etl_deepflow.py` |
| CFN 声明依赖 | `infra/lambda/etl_cfn/neptune_etl_cfn.py` |
| X-Ray 平行观测源 | `infra/lambda/etl_xray/neptune_etl_xray.py` |
| 事件驱动触发 | `infra/lambda/etl_trigger/neptune_etl_trigger.py` |
| 边验证（候选/判定/写回） | `chaos/code/runner/edge_verification.py` |
| 六阶段实验编排 | `chaos/code/runner/runner.py` |
| 观测方 SLI 采集（协议过滤） | `chaos/code/runner/metrics.py` |
| **图谱 MCP server（供 DevOps Agent）** | `mcp/`：`server.py`（24 个只读工具）、`agentcore_app.py`（AgentCore 入口）、`catalog_tools.py`、`provenance.py`（证据纪律）、`devops-agent-association.json`（工具白名单）、`README.md`（部署与注册步骤） |
| SPOF / 关键路径 / 影响面查询 | `dr-plan-generator/graph/queries.py` |
| **容灾切换计划生成（9/5 大改）** | `dr-plan-generator/`：`graph/snapshot.py`（离线快照，灾时不依赖 Neptune）、`graph/scope.py`（范围白名单锚定）、`planner/preflight.py`（就绪检查）、`assessment/rpo_estimator.py`（RPO 按拓扑推导） |
| **依赖的四维定义（内部权威）** | `docs/dependency-definition.md` |
| **故障注入覆盖边界（对客户）** | `docs/fault-injection-coverage-and-production-safety.md` |
| 契约一致性守门测试 | `tests/test_35_graph_contract.py`、`test_36`、`test_37` |
| 存量数据修复（脚本） | `infra/migrate_identity_keys.py`、`merge_duplicate_ec2_nodes.py`、`fix_wrong_source_edges.py`、`fix_property_cardinality.py`、`fix_neptune_data.py`、**`scripts/backfill_verify_confidence.py`**、**`scripts/clean_nondependency_verify_attrs.py`**（后两个为 9/5 新增） |
| CDK：Neptune 集群 / ETL 栈 | `infra/lib/neptune-cluster-stack.ts`、`neptune-etl-stack.ts` |

---

## 附 D：本次更新的核对结果（2026-09-05）

**核对方法**：逐节读取原文 → 提取每一处带 `[实测]` / `[代码]` 标记的断言 →
对活图谱重新查询、对仓库重新对账 → 只改被证伪的数字，保留原结论与原措辞。

### 修正的数字（14 组）

| # | 位置 | 原值（8/31） | 现值（9/5） |
|---|---|---|---|
| 1 | 节点总数 / 标签数 | 1063 / 33 | **1333 / 39** |
| 2 | 边总数 / 标签数 | 1802 / 26 | **2625 / 29** |
| 3 | 依赖语义边**种类** | 3 | **6**（+Delegates / InvokesTool / Retrieves） |
| 4 | 带 `dependency_kind` 的边 | 94（static 21 / dynamic 73） | **115**（dynamic 77 / static 27 / **inference 11**） |
| 5 | `dependency_kind` 取值数 | 2 | **3**（新增 `inference`） |
| 6 | 已确证依赖边 | **0** | **13 confirmed + 25 inconclusive** |
| 7 | 强度已分级 | 0 | **9**（hard 1 / degraded 3 / unclassified 5） |
| 8 | `drift_status` | 23 / 8（两值） | **25 / 7 / 1**（新增 `observed_then_silent`） |
| 9 | 节点分布 | Pod 570 / SG 55 / S3 33 / Lambda 32 | **Pod 735 / Incident 126 / ChaosExperiment 91 / SG 58 / TopologyChange 56 / S3 34** |
| 10 | `RunsOn` 边数 | 310 | **444**（且最大边类型已变为 `LocatedIn` 825） |
| 11 | 契约文件 | 665 行 / 九节 | **878 行 / 十节**（新增 `node_scope`） |
| 12 | 合法 `source` 数 | 11 | **12**（+`agentcore-etl`） |
| 13 | ETL 代码量 | ~6,400 行 | **7,519 行**（etl_aws 单体 3,217） |
| 14 | 测试基线（Python 3.11） | 605 / 53 | **650 passed / 150 skipped / 0 failed，零收集错误** |

### 已过时的结论（3 处，改了论证而非只改数字）

1. **「验证覆盖率 0.0%」不再成立。** 8/31 时这是全文最锋利的一句，附 B 还建议拿它开场。
   现在是 11%（13/115）。**继续那样讲就是报错数字**，已改写为三档对比的问法。
2. **「`source` 存量还没归一」已完成。** 8/31 记录「代码已挡住新的、旧的还在库里」，
   9/5 实测活图谱里 10 种 source 全部在契约声明的 12 个之内，未声明取值为 0。
3. **「尚缺第五个维度 scope」已补上。** 契约新增 `node_scope` 小节，
   `dr-plan-generator` 的范围锚定已在用它排除平台自身设施。

### 本次新查出的问题（3 个，均已写入第 8 节「诚实的边界」）

> ⚠️ **第 1 条已在 15:1x UTC 的第二轮补充里被推翻并改写**，下面保留的是当轮原始记录。
> 现在的正确表述见第 8 节：`Invokes` 那 16 条**不是违约**（契约裁定它本就是依赖边，
> 已改 `dependency: true`），越界只剩 `RoutesTo` 5 条；而 `first_seen` 缺失是
> **全图 131 条里缺 83 条的普遍现象**，不是那 16 条的个别问题。

1. **5 条 `AgentGateway -RoutesTo-> AgentTool` 边被写了 `dependency_kind`**，
   而 `RoutesTo` 声明为 `dependency: false`。根因已定位到代码：
   `etl_agentcore` 的 `_upsert_edge()` **无条件**写 `dependency_kind`，
   而 `etl_aws` / `etl_cfn` 的同名函数都有 `if is_dependency_edge(lb):` 门禁——
   **三个 ETL 里两个有、一个没有**。
   > 这与第 7 节「证据四」**同形**：声明写了、门禁没覆盖到。上次漏的是 `source` 值，
   > 这次漏的是「哪些边有资格带这个属性」。
2. **契约的语义字段完全没有门禁**——`test_35` 17 个用例全绿，却让
   `Invokes.dependency` 从 `false` 翻成 `true`、`expires_seconds` 从 `null` 变成 `21600`
   都通过了，因为**它只比对类型名集合**。这是上一条与下一条的共同根因：
   声明、生成物、部署、注释四者可以各自漂移，靠人肉对账才发现。
3. **`Invokes` 的重分类尚未提交，但生产已在跑**。工作区 `dependency: true`、
   每个已提交版本都是 `false`；生产 `neptune-client-base:11` 已放行那 16 条边。
   **任何人从 HEAD 重新生成 layer 并部署，生产行为会静默回退。**
4. **12 条边 `source` 为空**（Calls 11 / AccessesData 1），而 `source` 属于
   `edge_write_once_attrs`，本应必填。
5. **上一版把两个环境问题误记成了代码缺陷**：用 Python 3.9 跑测试，
   `markupsafe.soft_unicode` 版本冲突与未装 `streamlit` 被记作「2 个文件收集报错」。
   按仓库要求的 3.11 复跑：**650 passed / 150 skipped / 0 failed，零收集错误**。
   > **一份声称如实记录缺陷的文档，自己报了两个假缺陷。** 教训：
   > 任何自动采集的质量数字都要连采集环境一起记录。

### 关于数字时效的一条方法论（本次核对的副产品）

核对过程中总量数字在 20 分钟内变了三次（边 2625→2637、`dependency_kind` 115→131）。
由此得到一条对讲述有直接影响的区分：

| 数字类型 | 保鲜期 | 例子 | 讲之前要不要重跑 |
|---|---|---|---|
| **总量类** | 几十分钟 | 节点数、边数、各类型边数 | **必须重跑** |
| **验证类** | 到下次跑实验为止 | confirmed / inconclusive / 已分级数 | 上台前跑一次即可 |
| **代码类** | 到下次提交为止 | 契约行数、ETL 行数、测试数 | 按 commit 核 |
| **历史证据类** | 永久 | 第 7 节五条证据 | **绝不要更新**——改了就毁掉证据 |

**而且「数字在涨」不等于「图在变好」**：本次那次 +16 一度被我判成越界写入，
核对契约后才确认是 `Invokes` 重分类带来的**合法**增长。
**报增长数字时必须同时看合法性——而判定合法性要以契约为准，不能凭记忆里的边类型清单。**

### 第二轮补充（2026-09-05 15:1x UTC，对照 `one-observability-demo/doc/topology-ap-northeast-1.md`）

起因：核对拓扑文档与活环境后发现提纲缺五块，其中两块是**整个消费侧**。

| # | 补了什么 | 位置 | 为什么是缺口而不是省略 |
|---|---|---|---|
| 1 | 两个 VPC 的分工 | 4.1 开头（新增 1 分钟） | 一次答完「节点从哪来、边界在哪」，且是 `scope` 六档的物理直觉来源。原文 `agent-vpc` / `peering` 零命中 |
| 2 | MCP server on AgentCore | **新增 4.11.1** | 原文**零处** `MCP` 字样。而它是活的部署，且是「谁消费这张图」的答案 |
| 3 | 容灾计划生成器 | **新增 4.11.2** | 原文只在目录清单、一句顺带提及、附 C 索引里出现，**没有任何一节讲它做什么**。它是代码量最大的消费方 |
| 4 | `awesomeshop` 6 服务副本为 0 | 第 8 节 | 「图里有边但服务不在」最干净的实例，也是 `live` 过滤器存在的理由 |
| 5 | 三个数据库全单实例无副本 | 第 8 节 | 直接决定容灾能力上限；讲 DR 而不讲这条会被当场问住 |

**顺带修正了两条已经失效的记录**（不是补充，是原文现在是错的）：

1. **「21 条边越界写 `dependency_kind`」→ 现在是 5 条。** `Invokes` 那 16 条在
   2026-09-05 已由契约裁定为**本来就是依赖边**（`dependency: false → true`），
   越界只剩 `RoutesTo` 5 条。原文把这 16 条当违约讲，现在会讲错。
2. **「`Invokes` 那 16 条 `first_seen` 是 null」→ 缺口比这大得多。** 全图重查：
   131 条带 `dependency_kind` 的边里只有 **48 条**有 `first_seen`（37%），
   `DependsOn` 是 2/23。规律是**只有最新的 `agentcore-etl` 100% 写全**，
   越老的写入路径缺得越多。原文把普遍现象记成了个别现象。

**时间预算已同步**：第 4 节 33 → **39 分钟**（4.1 加 1 分钟、新增 4.11 五分钟），
裁剪顺序里给 4.11 排了位置并注明「不能整节砍，紧就压到 2 分钟」。
全篇满讲约 92 分钟——这份提纲从来不是按满讲设计的，裁剪顺序才是使用方式。

**一处无法核实的边界**：`mcp/README.md` 记录了向 DevOps Agent
`register-service` / `associate-service` 的完整步骤与白名单文件，但本机 aws CLI
没有 `aidevops` 命令（`Found invalid choice 'aidevops'`），**关联是否已生效无法从这里验证**。
讲稿里 4.11.1 的表格只列了 AgentCore 侧的实测值（那些是 `bedrock-agentcore-control`
查得到的），DevOps Agent 侧的关联应表述为「已配置」而不是「已验证」。

### 第三轮补充（2026-09-05 15:5x UTC）：AI 问答这一跳，以及两个自己引入的缺陷

起因：用户问「拓扑文档第三节为什么没有 agentcore/bedrock，PetSite 有 AI 问答啊」。
查下来这不是文档漏写，是**图谱缺边**，文档忠实反映了盲区。

#### 补进第 4.11 与第 8 节的内容

**PetSite 的 AI 问答链路**（`WaggleController.cs` 读 SSM
`/petstore/agent/waggleairuntimearn` → `InvokeAgentRuntimeAsync` 流式调用
`WaggleAIOrchestrator`）此前**完全不在图谱里**。agent 子图是孤岛，只靠
`search_available_pets -> petsearch` 一条边挂在业务系统上。后果是爆炸半径查询
答不出「AgentCore 挂了会影响 PetSite 什么功能」——而这一跳真断过一次
（IRSA 缺 `bedrock-agentcore:InvokeAgentRuntime`，兜底文案把 AccessDenied 说成网络中断）。

**这是「盲区由数据来源决定」最好的现场实例**，五个源各有各的看不见的理由：

| 数据源 | 为什么看不见 |
|---|---|
| DeepFlow | 跨 VPC 到 AWS 托管服务的 HTTPS，L7 解不出 |
| X-Ray | 埋点**在**（`RegisterXRayForAllServices` + `UseXRay` 中间件都有），`PetSite -> SimpleSystemsManagement` 边存在，但到 Waggle 没有 —— SDK 内置 AWS 服务清单不含 `BedrockAgentCore` |
| AgentCore 控制面 | 只知道被调了，不知道谁调的 |
| CloudWatch | `AWS/Bedrock-AgentCore` 有数据，但维度里**没有调用方** |
| NFM | VPC 级聚合，粒度不够 |

已补：`agentcore-etl` 从 SSM 建 `petsite -DependsOn-> WaggleAIOrchestrator`
（`static`，`declared_in` 记参数名）。CloudWatch 那批改写在 runtime **节点**上
（`invocations_24h`），刻意不作为边证据——维度无调用方，当观测源是给置信度注水。

#### 两个我自己引入又修掉的缺陷（都该讲，形状很典型）

**① scope 把整个 agent 层误判成 platform。** 第二轮落地 scope 时我把
`AgentRuntime` 整类映射成 `platform`。实测 6 个 runtime 里只有 1 个
（`graph_dependency_mcp`）是平台自己的 MCP server，另外 5 个 **WaggleAI\* 是
PetSite 的业务功能**。代价是可观测的：新建的 `petsite -> WaggleAIOrchestrator`
被标成 observed→platform，而选边器排除触及 platform 的边——**这条业务关键边
被过滤出靶点池，正是引入它要修的那个盲区**。

修法是把判据换成栈归属：`WaggleAIAgents` 栈（5 个 runtime + Gateway + Guardrail +
Memory + KnowledgeBase + S3Vectors）整体归 `observed`。还顺带发现匹配键少了
`runtime_id` / `gateway_id`——AgentCore 在 CFN 里的 `PhysicalResourceId` 是
**runtime id** 不是 ARN，少了它们三个节点匹配不上栈。

> 「按类型一刀切标 scope 是错的。**同一种节点类型可以既是业务又是平台**——
> 判据必须是归属，不是类型。」

**② scope 属性写成了多值。** 标注脚本用 `property('scope',...)` 而**没带
`single`** —— Gremlin 顶点属性默认 SET 基数，不带 `single` 是追加不是覆盖。
跑三次之后实测 `WaggleAIAdoption` 同时是 `observed` 和 `platform`。

值得注意的是它的爆炸半径形状：1364 个属性挂在 1344 个节点上，只多 20 个——
因为 SET 基数会**去重相同值**，所以只有**分类发生变化**的节点才累积。
这恰恰是最危险的情形：重新分类时旧值静默留存，而只标一次的节点看不出问题。

> 「这个仓库为同一类缺陷清理过 1,897 个冗余值，我又犯了一次。
> **顶点属性一律 `property(single, ...)`** 这条纪律，代价就在这里。」

#### 一处刻意没做的事（方法 2：X-Ray 埋点）

原计划给 PetSite 的 AgentCore 客户端注册 X-Ray handler。查下来这个计划的前提是错的：
`Startup.cs:40` **早就调用了** `AWSSDKHandler.RegisterXRayForAllServices()`，
`UseXRay` 中间件也在，`AWSSDK.BedrockAgentCore` 3.7.500 也在。真正的阻碍是
X-Ray .NET SDK 的**内置 AWS 服务清单**不覆盖这个新服务。

修法是提供自定义清单（`RegisterXRayManifest`），但我**没有做**，理由是：
自定义清单可能**替换**而非合并默认清单，处理不当会连带丢掉现有的
`PetSite -> SimpleSystemsManagement` 边；而验证需要重建容器镜像 + 部署到 EKS +
观察服务图，在此之前无法确认它是否真的生效。**给一个业务应用推一个可能静默
丢边的改动，比不推更糟。** 已单独立卡。

### 第四轮补充（2026-09-05 16:5x UTC）：一个"合理但错"的排查结论

这一轮把第三轮的结论**推翻了两条**，形状很典型，值得单独讲。

#### 被推翻的结论 1：「X-Ray SDK 清单不含 AgentCore 所以没边」

第三轮我写进文档的是：埋点在，但 SDK 内置服务清单不含 `BedrockAgentCore`，
而 X-Ray SDK 已进维护模式不会再加，所以此路不通、只能迁 ADOT。

**这个结论读起来完全合理**：新服务、旧 SDK、官方明文"不再新增 library
instrumentation"，三条都是真的，还能推出一个明确的行动项。

推翻它靠两条独立证据：

1. 把清单（`DefaultAWSWhitelist.json`）拉下来数，`services` 只有 6 项，
   **不含 `SimpleSystemsManagement`** —— 可后者在服务图里有边。
   所以清单只管"给这 6 个服务额外抓哪些参数"，不是出边开关。
2. petsite 容器日志的异常栈直接打脸：
   `at Amazon.XRay.Recorder.Handlers.AwsSdk.Internal.XRayPipelineHandler.InvokeAsync[T]`
   —— 埋点就在 AgentCore 调用的管道里。

真实原因平淡得多：**这条路径上的调用当时基本都失败，失败调用不产生下游服务节点。**

> 讲法：「这是本项目最值得讲的一次自我纠错。**错的排查结论比 bug 更难发现**——
> bug 会报错，错结论会自圆其说，还会推出一个昂贵且方向错误的行动项。
> 识破它靠的是一个'不该成立却成立'的反例：SSM 不在清单里却有边。
> 所以下次遇到『某某不支持所以没有』，**先找同类反例，比顺着结论往下推便宜得多**。」

#### 被推翻的结论 2：「WaggleAIAdoption 未接入可观测性」

第三轮说它是四个 agent 里唯一没在 Application Signals 注册的。**是窗口造成的假象**：
我查的是 6 小时窗口，而它最后一次流量在 9 小时前。24 小时窗口下 5 个全在，
配置也与兄弟完全相同。

> 「判断某个服务'没接监控'之前，先确认观察窗口内它**有没有流量**。
> 空的仪表盘有两种成因，缺埋点和没请求，看起来一模一样。」

#### 一个被误读成"26% 错误率"的突发

`WaggleAIAdoption` 24h 内 34 次错误 / 130 次调用。拆开看：
**`UserErrors` 34、`SystemErrors` 0**，而且 34 次**全部落在同一小时**
（该小时 100 次调用，其余时段 30 次调用 0 错误）。稳态错误率是 **0%**。

而且这些 400 极可能就是同一个 `runtimeSessionId` 长度问题（见下）。

> 「两个教训叠在一起：**一是把突发平铺进 24 小时窗口，会把一小时的异常
> 呈现成长期病症**；二是 `Errors` 混了 user 和 system 两类，
> 前者是'调用方发了坏请求'，后者才是'这个 runtime 坏了'。
> 我把合并值写成了节点属性 `invocation_errors_24h` ——
> **我自己造了一个会带偏 RCA 的信号。**」

#### 兜底文案把 400 说成网络故障

PetSite 把客户端 `SessionId` 原样透传给 `runtimeSessionId` 不校验长度，
短于 33 字符被 AgentCore 拒，而兜底把这个 400 呈现为
`[Sorry, the connection was interrupted]`，**HTTP 状态码仍是 200**。

我用 32 字符的 id 探测时一度以为复现了线上故障。换成 UUID 后 20.5 秒正常应答。

> 「**HTTP 200 + 一句友好的错误文案，是可观测性里最坏的组合**：
> 监控看不出问题，人看到的是错误方向的提示。」

#### 真正的杠杆在一个没人看的 API 上

同一个 PetSite，两个 API 给出的下游数量差 8 倍：

| 数据源 | PetSite 的下游 |
|---|---|
| X-Ray `GetServiceGraph`（`etl_xray` 现用） | **1 个** |
| Application Signals `ListServiceDependencies` | **10 个**，带操作名 |

而 `amazon-cloudwatch-observability` add-on 在集群上**早就 ACTIVE**（v6.5.0），
Application Signals 已注册 310 个服务。

> 「找了半天怎么补一条边，结果发现**换个 API 就能多拿 8 倍的边，
> 而且一行应用代码都不用改**。这不是运气 —— 是我一开始就没盘清
> 同一份遥测数据有几个读取入口。」

⚠️ 接入前必须先归一化：Application Signals 为**每个 ReplicaSet 单独注册服务**
（`petsite-deployment-8599bcbfc7` 这样的十几个），且 petsite 有双身份
（`PetSite`/generic 来自 X-Ray SDK、`petsite-deployment`/eks 来自 OTel）。
直接接入会被发布噪声刷爆。

#### 两个反直觉的版本约束

- **ADOT .NET 不支持 AWS SDK for .NET v4**，必须留 v3。PetSite 现在的
  `3.7.500` 正好合规 —— "升到最新"在这里是错的。
- X-Ray SDK 自 2026-02-25 维护模式，升版本永远不会带来新服务支持。

> 「'用最新版'不是普适答案。这里两个组件的最优版本一个是最新、一个是次新，
> 因为它们之间有支持矩阵约束。」

### 未改动的部分（说明为什么不改）- **第 2 节全部（业内主流做法、四种范式、图数据库选型）**：外部调研与官方文档引用，
  不随本项目状态变化。逐条读过，无需更新。
- **第 7 节证据一/二/三/五**：都是历史实测记录（带 `[代码注释记录的实测]` 标记），
  记录的是**当时发现缺陷的那一刻**的数据，改成今天的数字会毁掉证据价值。**刻意保留。**
- **第 5 节六阶段流程、判据机制**：结构未变，只有阈值在契约里，已在别处更新。
- **Neptune 引擎版本 1.4.6.3**：9/5 实测仍是 1.4.6.3，与 CDK 声明的 1.3.4.0 仍然分叉。
  **该分叉未修复，原文的提醒继续有效。**
