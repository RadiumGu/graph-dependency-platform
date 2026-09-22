# ETL 流程详解：从观测到图谱

本文讲**一条 ETL 从触发到写进 Neptune 的完整过程**，用两条形态差异最大的链路做实例：

- `etl_agentcore` —— 数据源是 AWS 控制面 API + CloudWatch Insights，身份键现成
- `etl_deepflow` —— 数据源是 ClickHouse 里的 eBPF flow_log，身份键要现场翻译

读这篇之前如果想先看全局，`architecture-graph-io-map.md` 有模块 × 图谱的读写矩阵；
想看各 ETL 的规模与复杂度，见 `../lessons/etl-complexity-and-maintenance.md`。
本文不重复那两份，只讲机制。

---

## 0. 线上有哪些 ETL，各自怎么被触发

实测 `aws events list-rules`（ap-northeast-1）：

| 调度 | 规则 | 目标 Lambda |
|---|---|---|
| `rate(5 minutes)` | `neptune-etl-every-5min` | `neptune-etl-from-deepflow` |
| `rate(15 minutes)` | `neptune-etl-every-15min` | `neptune-etl-from-aws` |
| `rate(15 minutes)` | `neptune-etl-agentcore-every-15min` | `neptune-etl-from-agentcore` |
| `rate(15 minutes)` | `neptune-etl-appsignals-every-15min` | `neptune-etl-from-appsignals` |
| `rate(1 hour)` | `neptune-etl-xray-hourly` | `neptune-etl-from-xray` |
| `cron(0 18 * * ? *)` | `neptune-etl-cfn-daily` | `neptune-etl-from-cfn` |
| 部署事件 | `neptune-etl-cfn-on-deploy` | `neptune-etl-from-cfn` |
| 资源变更事件 | `neptune-etl-trigger-{alb,ec2,eks,rds,elasticache}` | → SQS `neptune-etl-trigger-queue` |

定时链路和事件链路并存：定时保证最终一致，事件驱动缩短资源变更的可见延迟。

---

## 1. 两个时间轴：定义不在运行时序列里

**「采集」和「定义」不在同一条时间轴上**，把它们排成一列会得出错误的因果。

### 构建期（定义在这里，每次改契约才发生）

```
探测数据源实际发什么      scripts/probe_agent_span_attrs.py
  ↓                       —— 不要按 semconv 规范假设
改 profiles/graph_contract.yaml          ← 唯一权威，人只改这个
  ↓ scripts/gen_graph_contract.py --write
infra/lambda/shared/python/graph_contract_data.py   ← 生成产物
  ↓ tests/test_35（--check）守住产物与源一致
冻结，随 Lambda 层一起部署
```

ETL 运行时**不读 YAML**：四个 ETL 独立打包，`profiles/` 不在部署包里。
生成纯 Python 字面量后 Lambda 侧零运行时依赖（不需要 pyyaml，也不用把
profiles 打进每个包）。

### 运行期（每轮，契约已是常量）

```
触发
 └─> 1. 采集    各子采集独立返回 (status, rows)
     2. 门禁    assert_node_type / assert_edge_type / assert_source
     3. 写入    Gremlin upsert → Neptune
     4. 上报    collection_status（区分「空」与「拿不到」）
```

所以**定义是前置条件而不是步骤**：没有定义，`assert_node_type` 直接拒绝写入。
而两个轴的先后**恰好相反** —— 构建期是「先探测再定义」，运行期是「定义早已就位，
只剩采集」。先定义再去采，定出来的类型和属性名会跟数据源实际发的对不上，
**而那个失败长得像「没数据」**。

⚠️ `graph_contract_data.py` 是**生成产物却被 git 跟踪**，与
`infra/lambda/rca_window_flush/` 同一个模式。但这里做对了两件事：docstring
明写「自动生成，请勿手工编辑」，且 `test_35` 用 `--check` 断言产物与源一致。
改契约务必改 YAML —— 直接改产物会在下一次 `--check` 时被打回。

`etl_agentcore` 的 `lambda_handler` 就是这个顺序的直译：

```python
cp           = collect_control_plane()          # ① 控制面 → 节点
gw_targets   = collect_gateway_targets(...)
declarations = collect_service_to_runtime_declarations()   # SSM 声明边
activity     = collect_runtime_activity(...)               # runtime 活性
node_stats   = write_control_plane(cp, gw_targets, round_ts, ...)

span_status, span_rows = collect_spans(...)     # ② 运行时 span → 调用边
edge_stats   = write_span_edges(span_rows, round_ts)
```

采集顺序不是随意的：方法 1（SSM 声明边）要用 runtime 列表校验 ARN 存在，
方法 3（runtime 活性）要按 runtime 逐个取指标，所以两者都排在控制面之后。

---

## 2. 采集：两种数据源，两套坑

### 2.1 `etl_agentcore` — 两条链路刻意分开

| 链路 | 数据源 | 产出 | 失败表现 |
|---|---|---|---|
| ① 控制面 | `bedrock-agentcore-control` / `bedrock` / `bedrock-agent` | 资源节点 | **节点缺失** |
| ② 运行时 | per-runtime span 日志（Insights） | 调用边 | **边缺失**（"有 agent 但没依赖"） |

分开的理由是架构性的：**控制面 API 看不到"谁调了谁"**。Runtime 列表告诉你有哪些
agent，不告诉你 orchestrator 路由到了哪个子 agent —— 那只存在于运行时 span 里。
同理 `etl_xray` 也只能从 `GetServiceGraph` 拿拓扑。

两个实测出来的配置陷阱：

- **span 不在共享的 `aws/spans` 里。** AgentCore 的 span 按 runtime 分散在
  `/aws/bedrock-agentcore/runtimes/<runtime_id>-<endpoint>/` 的 `spans` 流。
  这里曾默认 `aws/spans`，是个静默 bug：同一条查询打两个地方，`aws/spans` 返回
  **0 行**（它有数据，但是别的服务的 span），per-runtime 返回 **3 行**。
  一个默认值造成三个现象 —— 每轮报 `spans empty`（不是没被调用，是问错了地方）、
  AgentTool 节点永远拿不到属性、所有 agent 依赖边都是空的。
- **CloudWatch namespace 是连字符。** `AWS/Bedrock-AgentCore` 有 235 个指标，
  `AWS/BedrockAgentCore` 返回 0 个。

采集窗口的取值也有依据：runtime 活性回看 **24h** 而非 6h，因为 agent 调用是稀疏
突发的（实测同一天 03:25 一批、07:47 一批，中间四小时空白），6h 窗口会频繁读到空。

### 2.2 `etl_deepflow` — ClickHouse SQL

```sql
SELECT IPv4NumToString(ip4_1) AS server_ip,
    quantile(0.5)(response_duration)/1000  AS p50_latency_ms,
    quantile(0.99)(response_duration)/1000 AS p99_latency_ms,
    count()/300 AS rps,
    countIf(response_status >= 1) / count() AS error_rate
FROM flow_log.l7_flow_log
WHERE toUnixTimestamp(time) > toUnixTimestamp(now()) - 300
    AND response_duration > 0 AND ip4_1 != 0
GROUP BY server_ip
```

两个字段陷阱：

- `l7_flow_log` **没有 `server_ip` 字段**，服务端 IP 是 `ip4_1`。
- `response_status` 是**枚举而不是 HTTP 状态码**：`0` 正常 / `1` 异常 / `2` 不存在 /
  `3` 服务端异常 / `4` 客户端异常。所以错误率判据是 `>= 1`，按 HTTP 语义写成
  `>= 400` 会得到一个恒为 0 的错误率，**而且不报错**。

---

## 3. 身份解析：`etl_deepflow` 独有的一段

`etl_agentcore` 采到 ARN 就能直接当身份键用。eBPF 不行 —— 它看到的是 IP，
而图谱节点是 `Microservice.name`，中间隔着一次翻译：

```
_get_eks_k8s_session()                 拿 EKS token
  → GET /api/v1/nodes                  node → AZ 映射
  → GET /api/v1/pods                   pod_ip → {name, namespace, type, az}
  → 只保留 INCLUDED_NAMESPACES = {default, awesomeshop}
```

这一段引入了一个其他 ETL 没有的失败模式：**采集成功但翻译失败**。
`_get_eks_k8s_session()` 拿不到 endpoint 就返回空 map，后面所有边都无从写入 ——
而现象跟"没有流量"完全一样。

同一份 IP 映射还被复用去解析数据库端点（`_resolve_datastore_ips`），
所以 `AccessesData` 有两个不同 source：实测 `deepflow-dns` 18 条、`deepflow-l4` 6 条。

---

## 4. 点怎么定义

`profiles/graph_contract.yaml` 是唯一权威，改完要跑 `scripts/gen_graph_contract.py --write`。
一个节点类型的字段很少：

```yaml
AgentRuntime:
  identity: arn              # 身份键 —— upsert 的匹配依据
  expires_seconds: 604800
  immutable: true
  writer: etl_agentcore      # 谁能写它
  note: Bedrock AgentCore Runtime。arn 形如 arn:aws:bedrock-agentcore:<region>:<acct>:runtime/<id>
```

**身份键不是都用 `name`**，这是最容易出事的地方。`AgentTool` 用的是复合键：

```yaml
AgentTool:
  identity: tool_key         # <owner_arn>#<tool_name>
```

理由写在契约注释里：tool **没有 ARN**，它是 agent 进程内注册的函数，或 Gateway 聚合
出的虚拟 MCP 工具。只用 `tool_name` 的话，两个 agent 各注册一个同名 `get_pet`
会被并成一个节点 —— 那正是 FIS chaos 模板里"tool 名精确匹配"踩过的同一个坑
（名字对不上就零注入，而实验仍报成功）。

---

## 5. 边怎么定义

```yaml
Delegates:
  src: [AgentRuntime]
  dst: [AgentRuntime]
  pairs: [[AgentRuntime, AgentRuntime]]   # 端点配对
  dependency: true            # 「A 依赖 B」语义 → 需要 dependency_kind
  expires_seconds: 21600
  transitive: true            # ⚠️ 见下
```

### 为什么是 `pairs` 而不是平铺 `src`/`dst`

平铺白名单的校验强度会退化成笛卡尔积，实测放行过真缺陷：`Manages` 加进真实存在的
HPA→Deployment 之后，平铺白名单连 `Deployment→Deployment`（查出的错源边之一）
都会放行。所以 `assert_edge_type` 在两端都已知时一律走 `pairs`，只知道一端才退回平铺。

### `transitive` 是一个必须存在的开关

`Delegates` 实际是两跳压成的一条直连边：

```
AgentRuntime -[RoutesVia]-> AgentGateway -[RoutesToRuntime]-> AgentRuntime
```

压成直连便于回答"orchestrator 依赖哪些 agent"，**代价是它在可达性分析里制造了
一条物理上不存在的旁路**。具体后果：割点/咽喉点分析在检查"绕开网关是否还能到
adoption"时会命中这条 Delegates 边，于是判定网关**不是**单点故障 —— 而网关恰恰是
那 4 条委派的唯一通路。

所以：

- 做**路径/可达性**推导的查询（`q_articulation_chokepoints`）必须**排除** transitive 边
- 做"谁依赖谁"的语义查询应当**包含**它

同一份数据，两类查询要的是两种图，这个标志就是区分它们的开关。

判据是：**这条边的两端之间是否还存在别的、代表同一次调用的节点**。是 → transitive。
不要把它误用成"弱依赖"或"间接依赖"。

### `dependency_kind` 三个取值

```
static    = 配置/模板声明了这条依赖，但不代表当前有流量
dynamic   = 持续观测到流量
inference = LLM 在运行时按 query 决定的调用
```

`etl_deepflow` 的 `Calls` 用 `dynamic`，`etl_agentcore` 的工具调用用 `inference`。
后者的默认值改过一次，原因值得记：

`deactivate_stale_dynamic_edges` 用 `has('dependency_kind','dynamic')` 挑边，于是
`Retrieves → nutrition-kb` 在 2026-09-05 被置 `active=false` —— 而那个知识库客观存在
（控制面 `list_knowledge_bases` 就返回它），nutrition agent 也确实依赖它，只是几个
小时没人问营养问题。**图谱因此给出的是一个错误陈述，而不是过期陈述。**

对 `petsite → petsearch`（300 秒内 25,042 次）来说"30 分钟没调用"确实说明变了；
对一天被调 35 次的 agent 工具，"6 小时没调用"什么也不说明。这正是本项目核心不变量
禁止的事：零流量与健康在指标上无法区分 → 一律 inconclusive，绝不判 refuted。

---

## 6. 门禁

`infra/lambda/shared/python/graph_contract.py` 提供：

| 函数 | 作用 |
|---|---|
| `assert_node_type(label)` | 节点类型必须已在契约声明 |
| `assert_edge_type(label, src, dst)` | 边类型已声明；两端已知时按 `pairs` 校验 |
| `assert_source(source, context)` | `source` 取值必须在契约词表内 |
| `identity_prop_for(label)` | 取该类型声明的身份键 |
| `is_dependency_edge(label)` | 是否需要 `dependency_kind` |

`assert_source` 是补上去的门禁。`SOURCES` 词表从引入起就存在且被 re-export，但
**没有任何一处检查读它**。2026-08-31 对活图谱普查的实测后果：

| 取值 | 条数 | 状态 |
|---|---|---|
| `eks-etl` | 1228 | 代码在写（`handler.py` 13 处），未声明 |
| `aws-etl-static` | 3 | 代码在写（`handler.py:376`），未声明 |
| `deepflow` | 8 | 代码在写（节点），未声明（契约里叫 `deepflow-etl`） |
| `manual` | 1 | 无代码在写，手工遗留，未声明 |

`is_dependency_edge` 取代了此前散在三个 ETL 里各自复制的 `DEPENDENCY_EDGE_LABELS`
常量 —— 那份复制在 `etl_aws/neptune_client.py` 的注释里已被作者标注为待收敛项。

---

## 7. 写入：两种 upsert 写法

### 7.1 `etl_agentcore` —— write-once 属性放进 `addV` 分支

```groovy
g.V().has('AgentRuntime','arn','arn:aws:...').fold()
 .coalesce(
    __.unfold(),                                   // 已存在 → 取出
    __.addV('AgentRuntime')                        // 不存在 → 新建
      .property(single,'arn','arn:aws:...')
      .property(single,'source','agentcore-etl')   // ← 只在新建时写
      .property(single,'first_seen', 1758...) )    // ← 只在新建时写
 .property(single,'environment','prod')
 .property(single,'last_seen', 1758...)            // ← 每轮更新
```

Python 侧先过门禁再取身份键：

```python
def _upsert_node(label, identity_value, props, round_ts):
    assert_node_type(label)
    assert_source(SOURCE, f'_upsert_node(label={label})')
    id_key = identity_prop_for(label)
    if not id_key:
        raise RuntimeError(f'契约未声明 {label} 的身份键')
```

**`property(single, ...)` 里的 `single` 不能省。** Gremlin 顶点属性默认 SET 基数，
不带 `single` 是**追加而非覆盖**，会静默累积多值。本仓库为此清理过 1,897 个冗余值
（`infra/fix_property_cardinality.py`）。

### 7.2 `etl_deepflow` —— write-once 属性用幂等写法

```groovy
g.V().has('Microservice','name','petsite').as('s')
 .V().has('Microservice','name','petsearch')
 .coalesce(
    __.inE('Calls').where(__.outV().has('name','petsite')),
    __.addE('Calls').from('s')
 )
 .property('first_seen', __.coalesce(__.values('first_seen'), __.constant(ts)))
 .property('source',     __.coalesce(__.values('source'),     __.constant('deepflow-etl')))
 .property('dependency_kind', __.coalesce(__.values('dependency_kind'), __.constant('dynamic')))
 .property('calls', 25042)
 .property('active', true)
 .property('last_seen', ts)
```

### 两种写法怎么选

`coalesce(values(...), constant(...))` 的语义是**已有值保留、缺失才补**。
`etl_deepflow` 不能用 7.1 那种"挪进 `addE` 分支"的简单办法，因为**存量的 11 条
无 `source` 边永远不会被重新创建**，挪进分支它们就永远补不上。幂等写法两头都顾。

判断依据：

- 只需保证新边正确 → 放进 `addV` / `addE` 分支（更简单）
- 还要给存量数据补值 → 用 `coalesce(values, constant)` 幂等写法

### write-once 属性为什么必须守住

`source` / `dependency_kind` / `first_seen` 是契约声明的 `edge_write_once_attrs`，
记录"**谁首先发现了这条依赖**"。被后写的源无条件覆盖等于抹掉发现史。

`etl_deepflow` 的 `source` 那行是 2026-09-06 修的。原来是裸写
`.property('source','deepflow-etl')`，而它**位于 `coalesce` 闭合之后** ——
即每轮无条件覆盖：`etl_xray` 先发现的 3 条 `Calls` 边一旦被 deepflow 也观测到，
`source='xray'` 就被改写掉。`test_35::g08` 的 docstring 自己记着 `etl_aws` 与
`etl_cfn` 犯过同一个错，却没人检查这个文件 —— **声明在、门禁不在**。

`first_seen` 同理，不能裸写在分支之外，否则"首次出现时间"退化成"最近一次时间"。
也不要用 `last_seen` 回填存量边：`first_seen <= last_seen` 恒成立，把 `last_seen`
当 `first_seen` 等于断言依赖"那时才出现"，比留空更糟。

---

## 8. 失活与删除：两级，硬删默认关闭

```python
INACTIVE_AFTER_SECONDS = 1800      # 30 分钟 ≈ 6 轮 → active=false
DROP_AFTER_SECONDS     = 604800    # 硬删，默认 DROP_ENABLED=false
```

阈值取 30 分钟而不是 5 分钟（一轮），是因为单轮 L7 查询偶发漏采不该立刻把活依赖
判死。硬删默认关闭的理由是删除不可逆，而软删（`active=false`）已足以让图谱停止
断言不存在的依赖，且保留历史可供追溯。

这个机制是补上去的，背景比机制本身更有说明性：**原先只 upsert、从不失效**，
`active` 和 `last_seen` 两个字段写了但全仓库没有任何消费方。2026-08-28 实测：
18 条 `Calls` 边里 17 条陈旧 2-5 个月却全部 `active=true`，其中
`gateway-service → auth-service` 对应的 awesomeshop 命名空间 6 个 Deployment
副本数全为 0（服务已下线），图谱仍声称该依赖活跃并带着 376 次调用量。
后果：RCA 把缩容到零的服务当作上游做根因推理。

另外注意采集窗口与 TTL 的关系。`etl_agentcore` 的 span 回看 6h 与
`Delegates`/`InvokesTool`/`Retrieves`/`AccessesData` 的 `expires_seconds=21600`
**刻意取同一个值**：

- 采集窗口 < TTL → 边在"还没到期但本轮没看到"时被误判失活
- 采集窗口 > TTL → 写进来的边立刻就是过期的

---

## 9. 采集状态不是布尔

```python
class _probe_status:
    OK = 'ok'
    EMPTY = 'empty'
    FAILED = 'failed'
    CONTRADICTORY = 'contradictory'
```

不用 `None` / `[]` 表示失败的理由：`[]` 与"调用失败"在下游看起来一样，而含义相反 ——
前者是"确实没有 agent"，后者是"我不知道有没有"。把后者当前者会让**权限丢失表现为
"图里 agent 消失了"而无人报警**。

`CONTRADICTORY` 与 `FAILED` 同等对待，两者都意味着本轮结果不可信，区别只在诊断
信息：`FAILED` 是调用没成功，`CONTRADICTORY` 是调用成功但结果自相矛盾。

而上面那个 span 日志组的坑说明**连四态还不够**：「问对了地方、确实没有」与
「问错了地方、所以没有」在计数上都是 0，`EMPTY` 只能表达前者。

---

## 10. 写入方分工：两套字段并存

`etl_deepflow` 的 docstring 定下的约定：

```
ETL 只写非 chaos_ 字段          call_type, error_rate, p99_latency_ms, strength
chaos-automation 只写 chaos_*   chaos_dependency_type, chaos_degradation_rate,
                                chaos_recovery_time_seconds, chaos_verified_by
```

规则是双向的：ETL 不覆盖 `chaos_*`，chaos-automation 不覆盖 ETL 字段。

这不是冗余，是**刻意留出对比面**：若 `DependsOn.strength=strong` 但
`chaos_dependency_type=weak/none`，说明 ETL 的判断偏保守，需要人工复核。
ETL 的静态判断和实验的实测结论各占一列，谁也不许覆盖谁。

---

## 11. 对账清单必须从契约来

`etl_deepflow` 的 `run_drift_detection()` 把"声明存在的依赖"与"运行时观测到的依赖"
对账。这个清单原本手写成四个标签：

```python
('AccessesData', 'PublishesTo', 'InvokesVia', 'ConsumesFrom')
```

2026-09-08 实测的问题：`ConsumesFrom` **在契约与图谱里都不存在**（0 条），
`InvokesVia` 只有一条没有写入方的孤儿边；同时漏掉了 8 种依赖边，其中 `DependsOn`
正是 `etl_xray` 用来表示 `Microservice → SQSQueue` 的标签。

后果不是漏报而是**误报**：判 `has_declared` 时看不见 `DependsOn`，于是"声明存在
且运行时已观测到"被判成 `declared_not_observed`。实测 9 条 `declared_not_observed`
里有 2 条是这样的假告警（22%）。现在改成 `_drift_edge_labels()` 从契约读。

---

## 12. 已知不一致（尚未修）

- **契约声称"写入时强制门禁"并不完全成立。** `etl_xray` 与共享层
  `neptune_client_base.py` **完全没接门禁**。`etl_agentcore` 与 `etl_deepflow` 接了。
  所以"图里的数据都过了契约校验"目前是句不准确的话。
- **agent 调用边是单源的。** `Delegates` / `InvokesTool` / `Retrieves` 全部
  `source=agentcore-etl`，没有第二个独立证据源可交叉验证。对比服务层的
  `AccessesData` 有 7 个来源（`xray` 27、`deepflow-dns` 18、`aws-etl` 10、
  `deepflow-l4` 6、`nfm` 3、`cfn-etl` 2、`appsignals-etl` 2）。
- **`InvokesVia` 只有 1 条**，来自 SSM 参数 `/petstore/agent/waggleairuntimearn`
  的声明式推导，不是观测出来的。

---

## 附：这套设计里反复出现的同一个形状

把两条链路的历史问题排在一起看，它们是同一件事的不同实例：

| 简单写法 | 实测后果 |
|---|---|
| 裸写 `.property('source', ...)` | 抹掉 `etl_xray` 的发现史 |
| agent 工具边标 `dynamic` | 客观存在的知识库被置 `active=false` |
| drift 清单手写 | 22% 假告警 |
| 只 upsert、不失效 | 下线服务在图里永久活跃，RCA 拿它做根因 |
| span 日志组用默认值 | 每轮诚实地报 empty，而数据在别处躺了一整天 |
| 平铺 `src`/`dst` 白名单 | 放行 `Deployment→Deployment` 错源边 |
| 节点属性不带 `single` | 静默累积 1,897 个冗余值 |

共同点是：**每一处都给出了一个错误陈述，而不是缺失陈述**。
错误陈述比没有陈述更难发现，因为它看起来是正常工作的。
