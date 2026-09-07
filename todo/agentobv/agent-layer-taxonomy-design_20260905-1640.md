# Agent 层分类学：`AgentGateway` / `AgentRuntime` / `AgentTool` 的建模与 `RoutesTo` 拆分

**日期**：2026-09-05 16:40 UTC
**状态**：待裁决（本文只出方案，未改任何代码或数据）
**起因**：核对图谱契约时发现 5 条 `AgentGateway-[RoutesTo]->AgentTool` 边携带
`dependency_kind: static`，而 `RoutesTo` 声明为 `dependency: false`。
追查根因时发现问题比「属性写错位置」深一层。

---

## 0. 核实方法与结论（先说这个，因为它推翻了两个直觉假设）

**核实手段**：`bedrock-agentcore-control` 控制面 API
（`ListGateways` / `ListGatewayTargets` / `GetGatewayTarget` / `ListAgentRuntimes`）
＋ 对活图谱的 openCypher 普查。**下面每条都是实测，不是推断。**

### 实测事实

**① 5 个 gateway target 的 `targetType` 全部是 `AGENTCORE_RUNTIME`，
配置指向的是运行时，不是工具** `[实测 2026-09-05]`：

| target 名 | `targetConfiguration.http.agentcoreRuntime.arn` |
|---|---|
| `nutrition` | `runtime/WaggleAINutrition-2NEiCS7VJw` |
| `orchestrator` | `runtime/WaggleAIOrchestrator-K85tG867Xt` |
| `ordering` | `runtime/WaggleAIOrdering-Mnr1HiASuX` |
| `concierge` | `runtime/WaggleAIConcierge-Yi6Ub97Ylw` |
| `adoption` | `runtime/WaggleAIAdoption-4gnHUlDACh` |

这 5 个 runtime **在图里本来就有对应节点**（`ListAgentRuntimes` 返回 6 个，
其中 5 个是 WaggleAI 系列，另一个是 `graph_dependency_mcp`）。

**② 网关的自述就是「入口 + agent 间路由」** `[实测]`：
`WaggleAIGateway` 的 `description` 原文是
*"Ingress + agent-to-agent routing for the Waggle AI multi-agent system"*，
`authorizerType: AWS_IAM`，`status: READY`。

**③ 图里 13 个 `AgentTool` 节点分成两批，来源不同、粒度不同** `[实测]`：

| 批次 | 条数 | `tool_key` 形态 | 写入依据 |
|---|---|---|---|
| 网关派生 | 5 | `gateway/waggleaigateway-…#<target 名>` | 控制面 target 列表 |
| span 派生 | 8 | `runtime/WaggleAI…#<工具名>` | `aws/spans` 结构化日志 |

**④ `AgentGateway` 零入边** `[实测]`。全图没有任何边指向它。

### 两个被推翻的假设（记下来，因为它们各会导致一个错误方案）

**假设 A（我的）：「gateway target 与 span tool 是同一实体的重复」** ——
**错**。`tool_key` 带父 ARN 作用域，两批的键空间不重叠，
所以**不存在节点碰撞**。契约的身份键设计在这里是对的，
`AgentTool.identity = 'tool_key'` + `immutable: true` 挡住了本来会发生的合并。
> 若按这个假设去做「去重」，会把两批本来正确分开的节点错误合并。

**假设 B（原方案要的）：「gateway target 是容器，需要 `AgentGatewayTarget` 节点类型」** ——
**也错**。target 不是一个中间层实体，它就是「网关声明了一条到某 runtime 的路由」，
**指向的正是已经建模的 `AgentRuntime`**。见 §3.1。
> 若按这个假设去做，会凭空插入一层没有独立失效语义的节点。

**真正的缺陷是：那 5 个网关派生的节点类型写错了。**
它们代表的是「网关到 runtime 的一条路由」，被写成了 `AgentTool`。

---

## 1. 当前图谱与真实拓扑的差距

### 图里现在是这样 `[实测]`

```
Microservice  --DependsOn(static)-->    AgentRuntime          (1)
AgentRuntime  --InvokesTool(inference)--> AgentTool           (8)
AgentRuntime  --Delegates(inference)-->  AgentRuntime         (2)
AgentRuntime  --Retrieves(inference)-->  KnowledgeBase        (1)
AgentGateway  --RoutesTo(static)-->     AgentTool【类型错】   (5)
AgentTool     --DependsOn(static)-->    Microservice          (1)
```

### 控制面证明的真实拓扑

```
（外部调用方 / petsite）
        │
        ▼
   AgentGateway  ← ingress，AWS_IAM 授权，唯一
        │  RoutesTo（控制面声明的 5 条 target）
        ▼
   AgentRuntime × 5   Orchestrator / Adoption / Nutrition / Ordering / Concierge
        │  InvokesTool（span 观测）        │  Delegates（agent 间，实际经网关）
        ▼                                  ▼
   AgentTool × 8                      AgentRuntime
        │
        ▼
   Microservice / KnowledgeBase
```

### 四个缺陷，按严重度排

| # | 缺陷 | 后果 | 严重度 |
|---|---|---|---|
| **P1** | 5 个 `AgentTool` 节点类型错误，实为网关到 runtime 的路由 | 图里有 5 个不存在的「工具」；网关与 runtime 之间的真实关系完全没有表达 | **高** |
| **P2** | `AgentGateway` 零入边，而它是唯一 ingress | **网关挂掉 = 5 个 runtime 全部不可达 + 所有 agent 间调用中断，而影响面分析完全看不出来**。这是一个未建模的 SPOF | **高** |
| **P3** | `Delegates`(2) 与 orchestrator 的 4 条 `InvokesTool` 绕过网关直连 | 实际路径经过网关，图里是直连。故障注入若按图选靶，会漏掉网关这一跳 | 中 |
| **P4** | `RoutesTo` 重载两种无关语义 | 修完 P1 后端点对是 `[AgentGateway, AgentRuntime]` + `[LoadBalancer, TargetGroup]`，仍是两种。「按 dependency 过滤」的查询会同时命中两类 | 中 |

> **P2 值得单独强调**：这不是「属性标错」这类记账问题，而是**图对一个真实存在的
> 单点故障保持沉默**。项目的核心主张是「图要能支撑影响面分析与容灾恢复顺序推导」，
> 而这里图会给出一个偏乐观的答案——比给不出答案更糟。

---

## 2. 一条判定原则（下面三部分都从它推出来）

本项目已有的判据（契约 `InvokesTool` 的 note、`dependency_definition.md`）可归纳为：

> **一个东西该不该成为节点，取决于它是否有独立的失效语义**——
> 即「它坏了」是否是一个可以与两端都不同的、可观测、可注入的事件。
>
> **一条边该不该是依赖边，取决于「目标不可用时源是否降级」。**
>
> **两种语义不得共用一个标签**，否则按 `dependency` 过滤的查询会同时命中结构边与依赖边。

---

## 3. 方案

### 3.1 第一部分：`AgentGatewayTarget` —— **建议不引入**

**理由**：gateway target 没有独立的失效语义。
它不是一个可以「自己坏掉」的东西——`GetGatewayTarget` 返回的
`targetConfiguration` / `credentialProviderConfigurations` / `status`
描述的是**这条路由本身的属性**，而路由的两端（网关、runtime）都已经是节点。
插入一层 target 节点会：

- 多一跳，却不增加任何可判定的失效边界（`Gateway → Target → Runtime`
  的中间节点永远与两端同生共死）
- 让所有影响面查询的路径长度 +1，而 §1 那张真实拓扑图里没有对应的物理层
- 违反上面的判定原则

**替代做法**：把 target 的信息作为**边属性**承载在 `Gateway → Runtime` 的边上：

| 边属性 | 取值来源 | 用途 |
|---|---|---|
| `target_id` | `ListGatewayTargets[].targetId`（如 `BLEULLONR7`） | 回溯到控制面对象 |
| `target_name` | `.name`（如 `nutrition`） | 人读 |
| `target_type` | `.targetType`（`AGENTCORE_RUNTIME`） | 将来出现 `LAMBDA` / `OPENAPI` 类型时区分 |
| `credential_provider` | `GATEWAY_IAM_ROLE` | 权限类故障的排查线索 |
| `target_status` | `.status`（`READY`） | 声明态是否健康 |

> **例外情形要写清**：如果将来出现 `targetType` 不指向图内已建模实体的 target
> （例如指向一个外部 OpenAPI 端点），那个端点**才**需要一个节点——
> 但那时该建的是 `ExternalEndpoint` 之类，仍然不是 `AgentGatewayTarget`。
> **判据是「路由的另一端是否已经是节点」，不是「target 要不要建模」。**

### 3.2 第二部分：指向网关的边 —— **需要，且是两条不同的失效路径**

P2 要补的不是一条边，而是**两条语义不同的入边**：

#### (a) 外部调用方 → 网关 —— **2026-09-07 已推翻，不要做**

~~`Microservice -[DependsOn]-> AgentGateway`~~

**实测证明 PetSite 不经网关。** 三条证据：

1. 图里已有 `petsite -[DependsOn]-> WaggleAIOrchestrator`（`AgentRuntime`），
   `source=agentcore-etl`、`dependency_kind=static` `[实测 2026-09-07]`
2. `todo/adot-migration-goal/GOAL.md` 的 DoD D1 明确把目标定为
   「图谱里 `petsite → WaggleAIOrchestrator` 这条边」——**指向运行时，不是网关**
3. 同文档记录的异常栈证明 PetSite 走的是 AWS SDK 直调
   （`WaggleController.SendMessage` → `XRayPipelineHandler` → SDK），
   即 `InvokeAgentRuntime`，路径上没有网关这一跳

> **本文 9-05 初版在这里判断错了，而且错得有代价**：原 §5 的 D1 建议
> 「把那条 `Microservice → AgentRuntime` 改为指向 `AgentGateway`」——
> 若照做会**删掉一条正确的边、换上一条不存在的边**，并让影响面分析
> 从「偏乐观」变成「偏悲观且错误」。
>
> **教训**：网关 `description` 自述 "Ingress + agent-to-agent routing" 里的
> "Ingress" 指的是**它自己有对外入口的能力**，不等于「所有入站流量都经过它」。
> **拿资源的自述当拓扑证据，是这次判断错的直接原因。**

**这条边缺的不是建模，是观测。** `GOAL.md` 已查明：`petsite → AgentCore`
在服务图上不出现的原因是**那条路径上长期只有失败调用**
（"成功的调用才在服务图上落下游节点"），不是缺埋点。
所以 `petsite → WaggleAIOrchestrator` 现在是 `static`（声明态），
要把它推到 `dynamic`/`confirmed` 靠的是合成流量，不是加边。

**2026-09-07 实测已证实这条路径,不再是推断** `[实测]`。
orchestrator 的运行时日志里有持续的网关出站请求:

```
INFO [httpx] [trace_id=6a9ee56a0a3bfddf4a62a5c45db38423 span_id=5fe316a2b7fa457f
     resource.service.name=WaggleAIOrchestrator.DEFAULT]
  - HTTP Request: POST https://waggleaigateway-th4m2rp46p.gateway.bedrock-agentcore.ap-northeast-1.amazonaws…
```

查询窗口 09-04 → 09-07，命中数按天：**155 / 331 / 663 / 468**（09-07 未过完），
频率约每 5 分钟一次，**从未中断**。日志组
`/aws/bedrock-agentcore/runtimes/WaggleAIOrchestrator-K85tG867Xt-DEFAULT`。

**这条证据改变两件事：**

**① P2 从「疑似盲区」升级为「已确认的活体 SPOF」。**
网关不是装饰性资源，它承载着全部 agent 间流量。它失效 ⇒ orchestrator
到 4 个子 agent 全断 ⇒ 多 agent 系统整体失效。**而图对此完全沉默。**

**② `dependency_kind` 应取 `dynamic`，不是 `static`。**
本文初版写 `static`（因为当时只有控制面声明作依据）。既然已实测到
**持续**流量（每 5 分钟、跨 4 天无中断），按本项目 `dependency_kind` 的定义
（`dynamic` = 持续观测到流量）就该是 `dynamic`。
这也让它落入 `deactivate_stale_dynamic_edges` 的失效管辖，
而非 `inference` 那条「稀疏突发、不判失效」的通道 —— 对这条边是正确的，
因为它的流量形态是稳态而非突发。

> **顺带修正 §5 的 D3**：原问题是「是否等 span 证明了网关这一跳再写」，
> 现在**已经证明了**，所以不存在「先写未验证的边」这个取舍。
> 而且证据就在 orchestrator 自己的日志里带 `trace_id`/`span_id`，
> **ETL 可以直接从这里派生这条边**，不需要新数据源。

#### (b) Agent 间调用 → 网关 —— **仍然需要**

```
AgentRuntime -[RoutesVia]-> AgentGateway     dependency_kind: static
```

网关自述含 "agent-to-agent routing"，且 orchestrator 的 4 条 `InvokesTool`
（`adoption` / `concierge_chat` / `food_ordering` / `nutrition_advisor`）
按名字对应的正是另外 4 个 runtime——**说明 orchestrator 调其他 agent 是经网关的**。

**为什么不复用 `DependsOn`**：这两条的失效影响面不同。
(a) 断了只影响外部入口；(b) 断了会让**多 agent 协作整体瓦解**而单 agent 直调仍可用。
合成一个标签就无法分别推导。

> **这一条的证据强度要如实标注**：target 配置证明了「网关能到 runtime」，
> 但**没有证明「orchestrator 调 adoption 时确实走了网关」**——
> 后者需要在 span 里看到网关这一跳，或做一次故障注入证伪。
> 建议先按 `dependency_kind: static` + `verify_status` 留空写入，
> **不要**直接标成已确证。这正是 §6 门禁 G3 要挡的事。

#### `expires_seconds`

两条都来自控制面声明、与流量无关，按 `Invokes` 改判后的同一判据
（依赖边必须有 TTL，由 `test_35::g07` 强制）取 **21600**（6h），
与 `InvokesTool` 一致。

### 3.3 第三部分：`RoutesTo` 拆分 —— **需要**

修完 P1 后 `RoutesTo` 的端点对是：

```
[AgentGateway, AgentRuntime]     ← agent 层，是依赖（runtime 挂了网关这条路由就是断的）
[LoadBalancer, TargetGroup]      ← 网络层，是结构（转发配置，不是「A 依赖 B」）
```

**仍然是两种无关语义**，且这次**一种是依赖、一种不是**——比拆分前更必须拆，
因为 `dependency` 是边类型级的标志，一个标签无法同时取两个值。
这正是当初 5 条边越界的**根因**：ETL 想给 agent 侧标 `dependency_kind`，
而标签的 `dependency: false` 是为网络层定的。

#### 命名候选

| 候选 | 优点 | 缺点 | 评价 |
|---|---|---|---|
| **`RoutesToRuntime`** | 与 `RoutesTo` 同族，一眼看出关系；与 `InvokesTool` 的构词法一致 | 略长 | **推荐** |
| `Fronts` | 短，语义准（网关是 runtime 的前置） | 与既有命名风格不搭，`Fronts` 在图里没有同族词 | 次选 |
| `ExposesRuntime` | 强调「对外暴露」这层语义 | 「暴露」偏权限语义，易与 Guardrail 混 | 不推荐 |
| 保留 `RoutesTo` 改为 `dependency: true` | 零迁移 | **会把 `LoadBalancer → TargetGroup` 全部误标成依赖**，实测波及网络层 **17** 条结构边（`RoutesTo` 共 22 条 = 17 网络层 + 5 agent 层）`[实测 2026-09-05]` | **不可行** |

**推荐 `RoutesToRuntime`**，理由是它遵循了 `InvokesTool` 立下的先例：
当一个已有标签的语义不能覆盖新场景时，**新立一个把差异写进名字里的标签**，
而不是放宽旧标签的定义。

#### 契约声明草案

```yaml
  RoutesToRuntime:
    dependency: true
    src: [AgentGateway]
    dst: [AgentRuntime]
    pairs:
      - [AgentGateway, AgentRuntime]
    expires_seconds: 21600
    note: '网关到 agent runtime 的路由，来自控制面 ListGatewayTargets。

      **刻意不复用 RoutesTo**：那个标签用于 LoadBalancer → TargetGroup 的
      转发配置且 dependency: false。本边是真依赖 —— runtime 不可用时
      网关这条路由就是断的，且网关是该 runtime 唯一的外部入口。
      一个标签无法同时取两个 dependency 值，这也是 2026-09-05 那 5 条边
      越界携带 dependency_kind 的根因。

      target 的元信息（target_id / target_name / target_type /
      credential_provider）作为**边属性**承载，不单独建 AgentGatewayTarget
      节点 —— 那一层没有独立失效语义，见
      todo/agentobv/agent-layer-taxonomy-design_20260905-1640.md §3.1。'

  RoutesVia:
    dependency: true
    src: [AgentRuntime]
    dst: [AgentGateway]
    pairs:
      - [AgentRuntime, AgentGateway]
    expires_seconds: 21600
    note: 'agent 间调用经网关路由（网关自述 "agent-to-agent routing"）。

      与 DependsOn 分开是刻意的：本边断裂会让**多 agent 协作整体瓦解**，
      而单 agent 直调仍可用；DependsOn -> AgentGateway 断裂只影响外部入口。
      两者失效影响面不同，合成一个标签就无法分别推导。

      ⚠️ 证据强度：控制面只证明了「网关能到 runtime」，**未证明**
      「orchestrator 调 adoption 时确实走了网关」。写入时 verify_status 留空，
      需 span 里看到网关这一跳、或一次故障注入才能置 confirmed。'
```

---

## 4. 迁移步骤（有严格顺序，不能调换）

**为什么顺序不能换**：`dependency_kind` / `source` / `first_seen` 属于
`edge_write_once_attrs`，而 ETL 每 15 分钟跑一次。**先清数据后改代码，
15 分钟内就被写回**——这个坑本仓库已经踩过（见提纲 §4.8）。

| 步 | 动作 | 前置 | 可回滚 |
|---|---|---|---|
| **M1** | 契约加 `RoutesToRuntime` / `RoutesVia`；`AgentGatewayTarget` **不加** | — | 是（仅声明） |
| **M2** | `test_35::g18` 的 `DEPENDENCY_EDGES` 登记新标签（g18 会强制这一步） | M1 | 是 |
| **M3** | `graph_schema_text` 同步（`g01/g02` 强制两侧类型名集合相同） | M1 | 是 |
| **M4** | 重新生成 `graph_contract_data.py`（`g03` 强制） | M1–M3 | 是 |
| **M5** | 改 `etl_agentcore`：target 的目标节点解析为 `AgentRuntime`（按 `targetConfiguration.…arn`），边改 `RoutesToRuntime`，target 元信息落边属性 | M4 | 是（代码） |
| **M6** | 同 ETL 补写 `RoutesVia` 与 `Microservice → AgentGateway` | M5 | 是 |
| **M7** | **部署新 layer + 新 ETL**，观察一轮（15 min）确认新边正确产出 | M6 | 是（回退 layer 版本） |
| **M8** | 清理 5 个错误的 `AgentTool` 节点及其 `RoutesTo` 边，**带 JSON 留痕** | **M7 已生效** | 数据删除，靠留痕回滚 |
| **M9** | 处置那条跳过网关的 `Microservice → AgentRuntime` 边（见 D1） | M8 + 裁决 | 同上 |

**M8 的留痕格式照既有先例**
`todo/removed-verify-attrs-nondependency-edges_20260905-0959.json`
与 `scripts/clean_nondependency_verify_attrs.py`——
本仓库同类清理一律先导出被删对象再删，不接受临时跑条查询。

> **M7 之前不要做 M8。** 当前那 5 条边是**固定集合**（5 个 target）、
> `dependency_kind` 又是 write-once，所以**它不增长**
> （16:0x 与 16:3x 两次实测都是 5 条）。等待的成本是零。

---

## 5. 待裁决项

| ID | 问题 | 选项 | 裁决（2026-09-07 复核后） |
|---|---|---|---|
| **D1** | 那条 `Microservice -DependsOn-> AgentRuntime`（`petsite → WaggleAIOrchestrator`）怎么办 | (a) 改为指向 `AgentGateway`；(b) 保留 | **(b) 保留，不动。** 9-05 初版建议 (a)，**已推翻**——PetSite 直调 runtime，不经网关，见 §3.2(a)。这条边是对的，缺的是观测而非建模 |
| **D2** | 新标签命名 | `RoutesToRuntime` / `Fronts` / 其他 | **`RoutesToRuntime`**，理由见 §3.3。不变 |
| **D3** | `RoutesVia` 是否现在就写入 | (a) 现在写，`verify_status` 留空；(b) 等 span 证明了网关这一跳再写 | **问题已消解（2026-09-07）**。日志已证明 orchestrator 持续向网关发 POST（4 天、每 5 分钟、无中断），所以不存在「写未验证的边」这个取舍。`dependency_kind` 取 **`dynamic`**（不是初版写的 `static`），见 §3.2(b) |
| **D4** | orchestrator 那 4 条 `InvokesTool` 是否要重构 | (a) 保留；(b) 改为经网关两跳 | **(a)**。它们是观测到的事实，不该按推断改写。不变 |
| **D5** | `graph_dependency_mcp` 这个 runtime（不在网关 target 里）是否要建模其入口 | — | 单独议；按 `node_scope` 应归 `platform` 而非 `observed`。不变 |

---

## 6. 需要同时补的门禁（否则本方案会以别的形式再漂一次）

这次缺陷能存活，是因为**四层之间没有校验**：契约声明、生成物、部署的 layer、
ETL 实际写入。已补两条（`4280cc9`），本方案还需三条：

| 门禁 | 挡什么 | 实现要点 |
|---|---|---|
| **G1** | **边的目标节点类型必须与控制面一致** | 拿 `ListGatewayTargets` 的 `targetConfiguration` 解析出的 ARN 类型，与 ETL 实际写入的 `dst_label` 比对。**这条直接挡住本次的 P1**——若当初有它，5 个错类型节点根本写不进去 |
| **G2** | **ingress 型节点必须有入边** | 声明为 ingress 的节点类型（`AgentGateway`、`LoadBalancer`）若零入边则告警。**这条挡 P2**——零入边的 ingress 必然意味着有一条依赖没被建模 |
| **G3** | **控制面声明的边不得直接标 `confirmed`** | 只有 chaos 干预结果才能置 `verify_status: confirmed`；控制面来源的边最高只能到「声明存在」。挡 §3.2(b) 那种「配置证明了 A→B 能通」被误当成「证明了实际走这条路」 |

> **G2 是这三条里最值钱的**：它把「有没有漏建模一条依赖」变成了一个
> **可自动检测的图性质**，而不是靠人肉巡检。
> 本次 P2 是我在追一个属性写错时**偶然**发现的——那不是一种可靠的发现机制。

---

## 7. 与既有结论的关系

- **不影响** `4280cc9` 已提交的 `is_dependency_edge` 门禁与 `g19`：
  那道门禁本身是对的，`RoutesTo`（网络层语义）确实不该带 `dependency_kind`。
  本方案是把 agent 侧的**真实依赖**用正确的标签表达出来，两者互补。
- **`Invokes` 改判为 `dependency: true` 的先例支持本方案**：
  两次都是「原标签的 `dependency` 值是按另一种场景定的，新场景不适用」。
  区别是 `Invokes` 的两种用法语义相同（都是调用），放宽标志即可；
  而 `RoutesTo` 的两种用法语义不同，必须拆标签。
- **提纲（`todo/project-intro-outline_20260831-0750.md`）第 8 节**那条
  「5 条 `RoutesTo` 越界」的记录，在本方案落地后应改写为
  「根因是目标节点类型错误 + 标签语义重载，已按 §4 迁移」。
