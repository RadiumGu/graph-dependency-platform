# 设计目标达成度评估 — 图数据库作为依赖关系单一源头

> 评估时间:2026-08-28 17:05 UTC
> 评估对象:`/home/ec2-user/works/graph-dependency-platform`
> 活图:Neptune `petsite-neptune`(868 节点 / 1343 边,ap-northeast-1)
> 方法:4 路并行代码审计 + **活图与集群实测交叉验证**(凡标 ✅实测 的结论均有 openCypher / kubectl 证据,非代码推断)

---

## 0. 评分总览

| # | 设计目标 | 达成度 | 一句话判断 |
|---|---|---|---|
| 1 | 图数据库作为依赖关系**唯一源头** | **~60%** | 运行时/基础设施拓扑层达成;服务身份与静态元数据层是**双源头** |
| 2 | 管理**动态**与**静态**依赖 | **~55%** | 两类都进了同一张图,但是**混装**而非分区——查询层主动抹平二者 |
| 3 | 处理依赖的**退化**与**变化** | **~40%** | 最弱。"变化"基本未实现;"退化"指标齐全但是死数据 |
| 4 | 被**各种 agent 快速调用** | **~55%** | 查询能力强,但锁在 Python 进程与 VPC 内,**零对外契约** |

**结论**:核心设计是成立的,且"以图为中心"这条主线在消费端贯彻得不错(DR 分析、子图查询、SPOF 检测全部查图,零硬编码)。
但有 **一个会让图谱输出错误结论的缺陷**(P0-1)和 **一个已在生产静默失效的闭环**(P0-2),
这两个不修,上层所有分析的可信度都存疑。

---

## 1. 最严重问题:图谱正在断言不存在的依赖 🔴

### 1.1 实测证据

```cypher
MATCH ()-[r:Calls]->()
RETURN count(r), count(r.source), count(r.active), count(r.last_seen)
```

| 指标 | 实测值 | 含义 |
|---|---|---|
| `Calls` 边总数 | 18 | 微服务间真实依赖 |
| 带 `source` 的 | **0** | 动态依赖边**无溯源标记** |
| 带 `active` 的 | 18 | 软删除字段**已就位** |
| 带 `last_seen` 的 | 18 | 时效字段**已就位** |
| `active=false` 的 | **0** | 软删除机制**从未被使用过** |
| 带 `first_seen` 的 | **0** | 无法回答"这条依赖是新增的吗" |

### 1.2 陈旧度实测(✅实测)

| Calls 边 | `last_seen` | 陈旧 | `active` |
|---|---|---|---|
| petsite → petsearch | 2026-08-28 | 新鲜 | True |
| list-adoptions → petsearch | 2026-06-30 | **59 天** | True |
| petsite → payforadoption | 2026-04-21 | **129 天** | True |
| petsite → petlistadoptions | 2026-03-21 | **160 天** | True |
| gateway-service → auth-service | 2026-03-19 | **162 天** | True |

**18 条依赖边中只有 1 条新鲜,其余 17 条陈旧 2–5 个月,却全部 `active=True`。**

### 1.3 已造成实际错误推理(✅实测)

`gateway-service → auth-service` 属 `awesomeshop` 命名空间。kubectl 实测该命名空间:

```
auth-service      desired=0   ready=<none>
frontend          desired=0   ready=<none>
gateway-service   desired=0   ready=<none>
order-service     desired=0   ready=<none>
points-service    desired=0   ready=<none>
product-service   desired=0   ready=<none>
```

**6 个 Deployment 副本数全为 0 —— 服务已整体下线**,但图谱仍声称该依赖活跃、带 376 次调用量。

后果已在本日 RCA 日志中出现:

```
causal_weight: gateway-service→petsite = 0.0 (0/67)
causal_weight: order-service→petsite  = 0.0 (0/67)
```

**RCA 正在把缩容到零的服务当作 petsite 的上游做根因推理。**

### 1.4 根因

`infra/lambda/etl_deepflow/neptune_etl_deepflow.py`:

| 行号 | 内容 |
|---|---|
| `:674` | `批量 upsert Calls 边` — 纯 upsert |
| `:688` | `__.addE('Calls').from('s')` |
| `:698` | `.property('active',true)` — **只写 true,从不写 false** |
| `:699` | `.property('last_seen',{ts})` — 写了,**无任何消费方** |

全仓库搜 `active.*false`、基于 `last_seen` 的清理逻辑:**0 命中**。
即"本轮未出现则失效"的对账(reconciliation)从未实现。

对比:`etl_aws` **确实**做了节点级对账(`infra/lambda/etl_aws/graph_gc.py:16-45`,
`_gc_vertices` 求差集后 `.drop()`,调用点 `handler.py:1168`)——**但只覆盖节点,不覆盖边**。
`etl_cfn`(`neptune_etl_cfn.py`)则纯 upsert、零删除,CFN 声明的依赖边永久累积。

---

## 2. 第二个问题:混沌韧性闭环已静默失效 🔴

### 2.1 三向属性名分裂(✅实测)

| 角色 | 文件:行号 | 属性名 |
|---|---|---|
| 写入方 A | `chaos/code/runner/graph_feedback.py:96` | `resilience_score` |
| 写入方 B | `chaos/code/agents/learning_direct.py:510` | `chaos_resilience_score` |
| **读取方** | `chaos/code/runner/neptune_helpers.py:69` | **`chaos_resilience_score`** |

活图实测:

```
带 resilience_score 的节点:        6 个（值 90–100）
带 chaos_resilience_score 的节点:  0 个
```

读取方查的属性**在图里根本不存在**,`coalesce(values('chaos_resilience_score'), constant(-1))`
每次都落到 `-1` 兜底。**混沌实验的韧性反馈闭环从未真正闭合过。**

### 2.2 附带:退化指标无时效标记

6 个带 `resilience_score` 的节点,其 `chaos_last_verified` **全部为 None** ——
连"这个分数有多旧"都无法判断,更无从做时效衰减。

### 2.3 `causal_weight` 处于休眠

`rca/actions/incident_writer.py:241-299` 写入 `causal_weight`,但代码自注释
(`:250-253`)明说「尚未纳入 `step4_score()` 评分,待积累 100+ 真实告警后启用」。
全仓库确认**只有写入方,无读取方**。

补充:其 `total` 取该服务**全历史** Incident 计数(`:270-274`),是单调累积比率,
**旧共现与新共现同权、永不衰减** —— 启用前需改为滑窗或指数衰减。

### 2.4 无"退化 → 响应"闭环

`chaos/code/runner/graph_feedback.py:60-66`:`dependency_type=none` 时仅
`logger.warning("SUSPICIOUS EDGE...建议人工复核")`,无自动动作。

`rca/core/decision_engine.py:86-150` 确有决策闭环(severity × confidence_band →
manual / semi_auto / auto),但**输入只有告警严重度与置信度,完全不读图上的
`resilience_score` / `causal_weight` / `error_rate` / `health_status`**。

退化指标目前是**只写不回喂的遥测**。

---

## 3. 目标 1 详析:两个"唯一源头"在竞争

`profiles/petsite.yaml:18` **自称** `Single Source of Truth`。于是形成一个**分层但未声明**的双源头结构:

| 层 | 源头 | 状态 |
|---|---|---|
| 运行时 / 基础设施拓扑 | Neptune | ✅ 干净 |
| 服务身份 / 静态元数据 | `profiles/petsite.yaml` | ⚠️ 与 Neptune 重复 |

### 3.1 违背清单

| 文件:行号 | 内容 | 重复 or 补充 | 风险 |
|---|---|---|---|
| `profiles/petsite.yaml:24-93` | 各服务 `tier: Tier0/1/2` | 重复 Neptune `Microservice.recovery_priority` | 高 |
| `profiles/petsite.yaml:85-101` | `aws_resources.sqs_queues/dynamodb_tables/lambda_functions` | 重复 Neptune `AccessesData/DependsOn/PublishesTo` 边 | 高 |
| `profiles/petsite.yaml`(graph_schema_text 内)`## 已知服务名` | 服务清单硬编码进 LLM prompt | 重复 Neptune 节点集 | 高 |
| `rca/core/graph_rag_reporter.py:22-28` | `SVC_TO_CW` — 服务→CloudWatch ns/dim | 重复;**明明 YAML 有 `cloudwatch` 段却不读**,只列 5 个服务、缺 petstatusupdater | 高 |
| `rca/collectors/aws_probers.py:257-261` | `SERVICE_FUNCTION_MAP` | 重复 YAML `lambda_functions`;**YAML 注释第 88 行声称"已消除 aws_probers 硬编码",实际仍在** | 高 |
| `rca/collectors/layer2_tools.py:199-203` | `SERVICE_FUNCTION_MAP`(第 3 份副本) | 重复 | 高 |
| `infra/lambda/rca_window_flush/config.py:22-64` | 硬编码 `CANONICAL` / `NEPTUNE_TO_K8S_LABEL` | **已实际漂移**(见 3.2) | **严重** |
| `chaos/code/gen_fis_batch.py:216-224` | `EKS_POD_SERVICES` + 故障过滤表 | 重复服务清单 | 中高 |
| `chaos/code/gen_template.py:390-393` | Neptune 不可达时的离线兜底服务清单 | 补充(仅离线),但会静默漂移 | 中 |
| `rca/data/service-db-mapping.json` | 服务→RDS + secret_arn | 一半补充(凭证)一半重复(关联);由扫 K8s 生成,**不源自 Neptune** | 中 |
| `scripts/generate_service_mappings.py:31-53` | 从 YAML 物化 `service_mappings.json` | 构建期快照,源是 YAML 非 Neptune | 中 |

### 3.2 已发生的漂移(不是风险,是事实)(✅实测)

活图 `Microservice` 节点名(15 个):

```
artillery, artillery-write, auth-service, gateway-service, list-adoptions,
order-service, payforadoption, pethistory, petlistadoptions, petsearch,
petsite, petstatusupdater, points-service, product-service, trafficgenerator
```

| 探测名 | 活图 | `rca_window_flush/config.py` |
|---|---|---|
| `pethistory` | ✅ 存在 | ❌ 未用 |
| `petadoptionshistory` | ❌ **不存在** | ✅ **硬编码使用** |
| `petfood` | ❌ **不存在** | ✅ **硬编码存在** |

`infra/lambda/rca_window_flush/config.py` 脱离了 YAML,引用了图中不存在的服务名。

### 3.3 合规范式(可作为收敛参照)

- `dr-plan-generator/graph/queries.py`(Q12–Q16、`q_edges_for_subgraph`)——拓扑全部来自 Neptune,**零硬编码**
- `rca/core/graph_rag_reporter.py:_get_neptune_subgraph`——子图/基础设施路径查图
- `demo/pages/1_Graph_Explorer.py`——实时查 Neptune(仅配色硬编码)
- `chaos/code/neptune_sync.py`——把混沌结果**写回**图(方向正确)
- `infra/lib/*.ts`(CDK)——**未**硬编码服务拓扑
- `rca/config.py` / `rca/neptune/schema_prompt.py`——已改为从 YAML 加载

---

## 4. 目标 2 详析:混装,而非分区

### 4.1 ETL → 节点/边 映射

| ETL(依赖性质) | 节点标签 | 边类型 | 溯源字段 | 运行时度量 |
|---|---|---|---|---|
| **etl_aws**(静态-AWS) | 约 27 类 | `LocatedIn/Contains/BelongsTo/RoutesTo/HasRule/ForwardsTo/AccessesData/Invokes/RunsOn/ConnectsTo/OwnedBy/HasSG/Implements/Routes/Manages/TriggeredBy/PublishesTo/WritesTo/DependsOn` | `source='aws-etl'`(`neptune_client.py:74/105`) | 无 |
| **etl_cfn**(静态-CFN) | 9 类(`neptune_etl_cfn.py:50-61`) | `AccessesData:245` / `DependsOn:262` / `Invokes:278` / `PublishesTo:391` | **`declared_in='cfn'`**(`:111`)—— 字段名与 aws 不同 | 无 |
| **etl_deepflow**(动态-L7) | **仅 `Microservice`**(`:645`) | `Calls:688` / `AccessesData:401` / `DependsOn:1015` | `AccessesData` 有 `source='deepflow-dns'`;**`Calls` 无任何溯源** | **`Calls` 度量完整**:`calls:692` `avg_latency_us:693` `p99_latency_ms:694` `error_count:695` `error_rate:696` |

### 4.2 区分能力

| 维度 | 结论 |
|---|---|
| 靠**边类型**区分? | ❌ `DependsOn` 与 `AccessesData` 被**三个 ETL 共写** |
| 靠**边属性**区分? | ❌ 字段名不统一(`source` vs `declared_in`),且最关键的 `Calls` **完全没有** |
| **schema** 是否声明区分? | ❌ `petsite.yaml:204-278` 的 26 种边**一律裸写** `(:A)-[:Rel]->(:B)`,**不声明任何边属性** |
| **查询层**是否区分? | ❌ **主动抹平** |

### 4.3 查询层抹平的实质后果

```
rca/neptune/neptune_queries.py:14   MATCH (n {name:$node})-[:Calls|DependsOn*1..5]->(m)   # q1 影响面
rca/neptune/neptune_queries.py:52   MATCH (upstream)-[:Calls|DependsOn]->(n {name:$svc})  # q3 根因候选
```

**"每秒数百次的真实调用"与"模板里声明但从未调用的依赖"在影响面分析里权重相同。**
对 RCA 与 DR 是实质缺陷。

### 4.4 亮点

动态边的度量**很完整** —— `calls` / `avg_latency_us` / `p99_latency_ms` / `error_count` / `error_rate`,
远超"只记录存在与否"。这块设计是好的,不需要改。

### 4.5 时间维度

各 ETL 时间字段名不统一:`last_updated`(aws)/ `last_scanned`(cfn)/ `last_seen`(deepflow),
且**全系统无 `first_seen`**(✅实测 0 条)。

---

## 5. 目标 4 详析:能力很强,但没有门

### 5.1 现有入口

| 入口 | 形态 | 可被外部 agent 调用? |
|---|---|---|
| `rca/neptune/neptune_queries.py` Q1–Q11 / Q17 / Q18 | 进程内 Python 函数 | ❌ 需 import rca 包 |
| `dr-plan-generator/graph/queries.py` Q12–Q16 | 进程内 Python 函数 | ❌ |
| `rca/neptune/nl_query_direct.py`(NL→Cypher→摘要) | 进程内类 | ❌ |
| `rca/neptune/strands_tools.py` 3 个 `@tool` | Strands 框架内 | ⚠️ 仅 Strands agent |
| `rca/scripts/graph-ask.py` | CLI | ⚠️ 需本机 + VPC 内 |
| `demo/pages/*.py` | Streamlit,`sys.path.insert` 直连 | ❌ |

### 5.2 对外服务端点:不存在(✅实测)

```
$ cat rca/.mcp.json
{"mcpServers": {}}
```

- 唯一名字含 MCP 的文件 `chaos/code/runner/chaos_mcp.py` 是**故障注入**(kubectl apply CRD),与图谱查询无关
- 无 FastAPI / Flask / GraphQL / AppSync / Lambda Function URL
- CDK 里的 `ApiGateway` 字样只是 ETL **摄入** API Gateway 资源时的类型映射

**图谱只能在 VPC 内、用 Python、把 `rca/` 挂进 `sys.path` 才能访问。**
非 Python / VPC 外 / 异构框架的 agent —— **目前无法接入**。

### 5.3 访问层重复:6 份客户端

| 文件 | 协议 | HTTP 库 | 连接复用 |
|---|---|---|---|
| `infra/lambda/shared/python/neptune_client_base.py` | **仅 Gremlin** | requests | ✅ |
| `infra/lambda/etl_aws/neptune_client.py` | — | — | ✅ |
| `infra/lambda/rca_window_flush/neptune/neptune_client.py` | — | — | ✅ |
| `rca/neptune/neptune_client.py` | openCypher | requests | ✅ Session |
| `dr-plan-generator/graph/neptune_client.py` | openCypher | requests | ✅(注释自认 "Mirrors the pattern in rca") |
| `chaos/code/runner/neptune_client.py` | openCypher+Gremlin | **urllib** | ❌ **每次新建连接** |

那个本该消除重复的 `shared/neptune_client_base.py` **只支持 Gremlin、只服务 ETL 写入**,
查询侧(openCypher)三个模块谁都没用它。查询库同样割裂:Q1–Q18 在 rca、Q12–Q16 在 dr-plan,
**编号体系跨模块拼接却不共享代码**。

### 5.4 性能

- 后端**零结果缓存**(唯一缓存是 `demo/pages/1_Graph_Explorer.py` 的 `@st.cache_data(ttl=60)`)
- 无批量查询接口、无重试/退避、无熔断
- `verify=False` 全线关闭 TLS 校验(VPC 内取舍,属安全债)
- NL 路径瓶颈在 **Bedrock 两次往返**(生成 Cypher + 摘要),已用 Prompt Caching 缓解

### 5.5 NL 查询覆盖度的硬约束

`schema_prompt.build_system_prompt()` 把 `petsite.yaml:149` 的 `graph_schema_text` 原样拼进
system prompt(31 节点 / 26 边 + 29 条 few-shot + 只读 guard)。

**硬约束成立:schema 正文 = NL 可达边的上界。** 任何 ETL 写入了、却没同步进 YAML 的边类型,
对自然语言查询是**隐形的**。且 schema 是静态 YAML,**无从 Neptune 实时 introspect 的机制**,
YAML 与活图之间**无一致性校验闭环**(`tests/test_11_schema_consistency.py` 校验的是代码间一致性,
非 YAML ↔ 活图)。

已发现超出 schema 声明的实际用法:`dr-plan-generator` 的 `q13`/`q16` 用
`WritesTo → RDSCluster`,而 schema 里 `WritesTo` 只声明指向 `SQS/SNS/S3/NeptuneCluster`。

---

## 6. 附带发现的 3 个小缺陷

| # | 缺陷 | 证据 |
|---|---|---|
| a | `q1` 遍历 `:Serves` 边,而 `etl_aws` 每轮主动删除它 —— **死查询** | `neptune_queries.py:21/24-25` 用 `Serves`;`etl_aws/handler.py:1217-1219` `hasLabel('Serves').drop()`;✅实测活图 `Serves` 边 **0 条**,BusinessCapability(3 个)只有 `DependsOn` 出边(6 条) |
| b | `etl_cfn` 中 SQS 的 label 不一致 | `:54` 映射为 `'Queue'`,`:389` 写入用 `'SQSQueue'` |
| c | `window_flush_handler` 调用不存在的 `generate_group_report` | 运行时 `AttributeError`,靠 fallback 降级到 `generate_rca_report()` —— **"按 EventGroup 聚合出报告"这条路径从未实现** |

---

## 7. 补充建议(按价值排序)

### P0-1 — 依赖边失效对账 🔴

**为何最高优先**:这是唯一会让图谱**输出错误结论**的缺陷,且已经在输出了(见 1.3)。

字段(`active` / `last_seen`)都已就位,只缺消费逻辑:

| 改动位置 | 内容 |
|---|---|
| `infra/lambda/etl_deepflow/neptune_etl_deepflow.py`(`:674` 之后) | 加一轮 Calls 边对账:本轮 `last_seen` 未刷新 → `active=false`;超阈值(如 7 天)→ `.drop()` |
| 同上 `:698` 附近 | 补 `first_seen`(仅在 `addE` 分支写,`onMatch` 不覆盖) |
| `infra/lambda/etl_cfn/neptune_etl_cfn.py` | 用 stack 级 `last_scanned` 做声明边对账 |
| `infra/lambda/etl_aws/graph_gc.py` | 把 GC 从**仅节点**扩展到**边级** |

### P0-2 — 修 `resilience_score` 属性名分裂 🔴

**一行改动,让已跑数月的混沌反馈闭环真正生效。**

统一 `chaos/code/runner/graph_feedback.py:96`、
`chaos/code/agents/learning_direct.py:510`、
`chaos/code/runner/neptune_helpers.py:69` 三处为同一属性名,
并补写 `chaos_last_verified`(当前 6 个节点全为 None)。

### P1-1 — 依赖边补 `dependency_kind` 并让查询可过滤

1. 所有依赖边统一补 `source` 与 `dependency_kind ∈ {static, dynamic}`
2. `profiles/petsite.yaml:204-278` 的 schema 中**约定该属性**(当前边定义不带任何属性)
3. `neptune_queries.py:14/52` 的 `q1`/`q3` 支持按该属性过滤

**直接提升 RCA 准确度** —— 不再把声明依赖与真实调用等权。

### P1-2 — 一个 MCP server 端点

目标 4 最大缺口,**投入产出最高**:把现成的 Q1–Q18 + NL 引擎包成 MCP tools,
任意 agent(任意语言、任意框架)即可接入。查询能力已全部具备,只差这层门。

注意 `rca/.mcp.json` 当前是空的 `{"mcpServers": {}}`,是个待填的占位。

### P2-1 — 收敛访问层

- 把 3 套 openCypher 客户端 + 割裂的 Q1–Q18 / Q12–Q16 合成一个 graph SDK,供 MCP 端点与三模块共用
- `chaos/code/runner/neptune_client.py` 改用 Session 复用(当前每次新建连接)
- 删除 `SERVICE_FUNCTION_MAP` 的 3 份副本与 `SVC_TO_CW`,统一走 `ServiceRegistry`
- 修 `infra/lambda/rca_window_flush/config.py` 的已发生漂移

### P2-2 — `causal_weight` 接入评分 + 时间衰减

代码自注释「待积累 100+ 真实告警后启用」——**告警链路已于今日修通,开始积累了**。
接入 `rca/core/rca_engine.py` 的 `step4_score()` 前,须先把 `total` 的**全历史单调累积**
改为滑窗或指数衰减(`incident_writer.py:270-274`)。

### P2-3 — schema ↔ 活图一致性校验

加一个测试(参照已有的 `test_11_schema_consistency.py`)断言:
YAML 声明的节点/边类型集 == 活图实际类型集,YAML 服务集与 tier == Neptune 节点集与 `recovery_priority`。
**这是把"双源头"变回"单源头"的最低成本方案** —— 不必强行合并,但保证不漂移。

### P3 — 拓扑历史快照

"上周拓扑长什么样"目前**完全无法回答**(所有写入均为就地覆盖,无版本/快照/时间分区)。
这是四个目标里唯一需要**新增机制**而非修补的一项:定期快照导出,或给写入加有效期区间(bi-temporal)。

---

## 8. 一句话总结

**主线设计是对的,消费端也确实以图为中心;但图谱当前"只会长,不会忘"** ——
依赖只增不减、退化指标只写不读。
先补 P0-1 与 P0-2,让图说真话、让闭环真闭合,再谈 P1 的分区与开放。
