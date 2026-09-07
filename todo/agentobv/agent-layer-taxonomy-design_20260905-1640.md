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

### 3.4 采集路径：从网关 CLIENT span 派生 —— **已实测验证，且优于现有做法**

`[实测 2026-09-07]` 网关这一跳在 span 里**有结构化记录**，不只是日志文本。
样本（`/aws/bedrock-agentcore/runtimes/WaggleAIOrchestrator-K85tG867Xt-DEFAULT`）：

```json
{
  "scope": {"name": "opentelemetry.instrumentation.httpx", "version": "0.65b0"},
  "traceId": "6a9ee3124648b5e73d93a8107a962d91",
  "spanId": "855f3583ac996e96",
  "parentSpanId": "5741625a76fa2708",
  "name": "POST",
  "kind": "CLIENT",
  "resource": {"attributes": {
      "cloud.resource_id":
        "arn:aws:bedrock-agentcore:…:runtime/WaggleAIOrchestrator-K85tG867Xt/runtime-endpoint/DEFAULT:DEFAULT"}},
  "attributes": {
    "http.url": "https://waggleaigateway-th4m2rp46p.gateway.bedrock-agentcore.…/adoption/invocations",
    "aws.remote.service": "waggleaigateway-th4m2rp46p.gateway.bedrock-agentcore.ap-northeast-1.amazonaws.com",
    "aws.remote.operation": "POST /adoption",
    "http.status_code": 200
  }
}
```

**四个字段刚好凑齐一条边所需的一切**：

| 字段 | 给出什么 |
|---|---|
| `resource.attributes.cloud.resource_id` | 源 runtime 的 ARN —— **正是 ETL 现在已经在用的那个字段** |
| `attributes.aws.remote.service` | 网关主机名，内含网关 id `waggleaigateway-th4m2rp46p` → 映射到 `AgentGateway` 节点 |
| `attributes.aws.remote.operation` | **`POST /adoption` —— 直接给出网关 target 名** |
| `attributes.http.status_code` | 成败信号，可用于置信度 |

筛选条件干净：`name = "POST"` + `kind = "CLIENT"` +
`scope.name = "opentelemetry.instrumentation.httpx"`。

#### 覆盖度实测（09-04 → 09-07）

| target | 调用数 | first_seen | last_seen | status |
|---|---|---|---|---|
| `POST /adoption` | 145 | 09-04 19:53 | 09-07 16:15 | 200 |
| `POST /nutrition` | 61 | 09-04 19:54 | 09-07 10:10 | 200 |
| `POST /ordering` | 42 | 09-04 19:30 | 09-07 12:50 | 200 |
| `POST /concierge` | **17** | 09-04 19:54 | **09-05 03:25** | 200 |

**四个子 agent 全部出现**，方法可泛化，不是只对 adoption 有效。

#### 这条路径还顺手消掉一类既有缺陷

现在 `Delegates` 边的派生依赖 `etl_agentcore` 里的**硬编码映射表**
`_DELEGATION_TOOLS`（tool 名 → runtime 名）。那张表 2026-09-06 才补上
`concierge_chat` / `food_ordering` —— **它漏一项就静默少一条依赖边**，
而「少一条边」在图上与「本来就没有这个依赖」无法区分。

网关 span 不需要这张表：`aws.remote.operation` 给出的是**网关 target 名**，
而 target → runtime 的映射**本来就来自控制面** `ListGatewayTargets`
（ETL 已经在调）。于是：

```
span: cloud.resource_id ─────────────────► 源 AgentRuntime
      aws.remote.service ──────────────► AgentGateway
      aws.remote.operation "POST /x" ──► target "x" ──[控制面]──► 目标 AgentRuntime
```

**一条 span 同时能派生三件事**：`RoutesVia`（源 runtime → 网关）、
`Delegates`（源 runtime → 目标 runtime，不再需要硬编码表）、
以及对 `RoutesToRuntime`（网关 → 目标 runtime）的动态印证。

> **建议**：`Delegates` 改由网关 span 派生，`_DELEGATION_TOOLS` 保留作为
> **不经网关的直连委派**的兜底（若存在），并在注释里写明主路径已换。
> 这把「靠人肉维护名字映射表」换成「靠控制面的真值」——
> 与本项目「身份键必须由不变量派生，不能用 name」是同一条原则。

#### 一处必须同时改的过滤器

现有 `SPAN_QUERY` 有 `| filter ispresent(op)`，其中
`op = attributes.gen_ai.operation.name`。网关 CLIENT span **没有 gen_ai 属性**，
会被这个过滤器整批排除 —— 这就是它至今没被采集到的原因。
需要新增一条独立查询，**不要**放宽原查询的过滤器
（原查询的 `ispresent(op)` 是刻意的，见该处注释）。

---

## 4. 迁移步骤（有严格顺序，不能调换）

**为什么顺序不能换**：`dependency_kind` / `source` / `first_seen` 属于
`edge_write_once_attrs`，而 ETL 每 15 分钟跑一次。**先清数据后改代码，
15 分钟内就被写回**——这个坑本仓库已经踩过（见提纲 §4.8）。

| 步 | 动作 | 前置 | 可回滚 | 状态 |
|---|---|---|---|---|
| **M1** | 契约加 `RoutesToRuntime` / `RoutesVia`；`AgentGatewayTarget` **不加** | — | 是（仅声明） | ✅ 2026-09-07 |
| **M2** | `test_35::g18` 的 `DEPENDENCY_EDGES` 登记新标签（g18 会强制这一步） | M1 | 是 | ✅ |
| **M3** | `graph_schema_text` 同步（`g01/g02` 强制两侧类型名集合相同） | M1 | 是 | ✅ |
| **M4** | 重新生成 `graph_contract_data.py`（`g03` 强制） | M1–M3 | 是 | ✅ 31 边类型 |
| **M5** | 改 `etl_agentcore`：target 的目标节点解析为 `AgentRuntime`（按 `targetConfiguration.…arn`），边改 `RoutesToRuntime`，target 元信息落边属性 | M4 | 是（代码） | ✅ |
| **M6** | 同 ETL 补写 `RoutesVia`，并把 `Delegates` 主路径改为网关 span 派生 | M5 | 是 | ✅ |
| **M7** | **部署新 layer + 新 ETL**，观察一轮（15 min）确认新边正确产出 | M6 | 是（回退 layer 版本） | ⬜ 待部署 |
| **M8** | 清理 5 个错误的 `AgentTool` 节点及其 `RoutesTo` 边，**带 JSON 留痕** | **M7 已生效** | 数据删除，靠留痕回滚 | ⬜ |
| **M9** | 从契约 `RoutesTo` 的 pairs/src/dst 里删掉 `AgentGateway`/`AgentTool`（**必须最后做**，见下） | M8 | 是 | ⬜ |

> ### M9 为什么必须排在最后（2026-09-07 实施时发现的部署危险）
>
> 本文初版把「拆 `RoutesTo`」笼统放在 M1。**那样做会打挂生产。**
> 契约由已部署的 layer 提供，而 `assert_edge_type` 是运行时执法：
> 如果 M1 就把 `[AgentGateway, AgentTool]` 从 pairs 里删掉，
> 一旦 layer 更新而 ETL 代码还是旧的，`etl_agentcore` 每轮都会**拒写并抛错**。
>
> 所以 M1 只**新增**，`RoutesTo` 那一对错的 pair 要留到「ETL 改完 + 部署 +
> 存量清理」三步都完成之后才能删。契约里已就地写了这条迁移期说明。

### M1–M6 实施记录（2026-09-07）

**顺带修掉的三处既有缺陷**（都是实施时才暴露的）：

1. **`ListGatewayTargets` 不返回 `targetConfiguration`**，而 `_backend_kind()`
   与 `_backend_ref()` 都在读它 —— 这两个函数对网关 target **一直在空转**。
   实测证据：5 个 `owner_kind='gateway'` 的 `AgentTool` 全部
   `backend_kind='unknown'`。已在 `_paged_targets()` 里补 `GetGatewayTarget`
   逐个补齐（失败则降级保留摘要，不让一个坏 target 拖垮整轮）。
2. **`test_20::s6_02` 根本没有引用任何允许名单**，而同文件的节点用例有。
   同一份 schema、同一种「已声明尚未产生实例」的合法中间态，节点放行、边报错。
   `test_11` 早就记下了这个不对称并为此建了 `PENDING_FIRST_EDGE`，
   但只修了自己、漏了 `test_20`。已补。
3. **`_dependency_edge_labels()` 的兜底清单有两份副本**
   （`rca/neptune/` 与 `infra/lambda/rca_window_flush/neptune/`），
   由 `test_52::m05` 钉住必须一致。两份都已同步到 9 类。

**测试**：714 passed / 0 failed / 151 skipped（改动前 682 passed）。

**`PENDING_FIRST_EDGE` 已登记两项**，销账条件写在
`tests/test_11_schema_consistency.py`：部署后首轮 ETL 跑完、这两种边在活图谱里
出现即移除。**留着等于放弃对它们的存在性检查** —— 而 `RoutesVia` 恰恰是唯一能让
图谱说出「网关是单点故障」的那条边，静默为空是最坏情况。

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
>
> **G2 已实现**（2026-09-07，`tests/test_57_inbound_reachability.py`）。
> 判据不是「零入边即告警」——实测否掉了那个朴素版本：`LoadBalancer` 5 个
> 全部零入边（合法图根，上游是互联网），`AgentRuntime/graph_dependency_mcp`
> 也零入边（`scope=platform`，调用方是 Kiro 而非被观测系统）。
> 最终是三层判据：显式登记的类型 × `scope='observed'` × 减去 `KNOWN_GAPS`。

---

## 8. 光有边还不够：SPOF 判据对区域级托管服务是失明的

**这一节是 2026-09-07 实施 M1–M6 时发现的，它决定了前面那些边有没有用。**

### 问题

`q16_single_point_of_failure` 的判据是
「**只在单个 AZ**（`size([(r)-[:LocatedIn]->(az)]) = 1`）且被 ≥2 个服务依赖」。

实测：**`AgentGateway` 与全部 6 个 `AgentRuntime` 的 `LocatedIn` 边都是 0**，
所以那个条件恒为假 —— **无论加多少条边，网关都不会被 q16 命中。**

这不是 q16 的 bug。它回答的是「一次 AZ 故障会打掉哪些被多方依赖的资源」，
对区域级托管服务而言排除是**正确的**：AZ 挂了不影响一个 regional 服务。
缺的是**第二种故障模型**。

### 新增 `q_articulation_chokepoints`（`dr-plan-generator/graph/queries.py`）

判据：对每个 (上游 u, 候选 n, 下游 d)，若**不经 n** 就没有任何 u→d 的路径
（1..3 跳，只走**物理**依赖边），则 d 被 n 阻断。按阻断数排序。

刻意**不合并进 q16**：两者是两种故障模型，
q16 问「AZ 没了谁受影响」，本查询问「这个组件没了，谁就到不了它后面的东西」。
合并会得出没人能解释的答案 —— 与 `RoutesTo` 一个标签两种语义是同类错误。

实测输出（2026-09-07，`min_blocked=2`，共 **10 条**，规模可人工消费）：

| 类型 | 咽喉点 | 阻断 | 上游 |
|---|---|---|---|
| Microservice | `payforadoption` | 8 | 1 |
| Microservice | `petsearch` | 7 | 3 |
| Microservice | `petlistadoptions` | 4 | 1 |
| **AgentRuntime** | **`WaggleAIOrchestrator`** | **4** | 1 |
| StepFunction | `StepFnStateMachine76D362E8-…` | 3 | 2 |
| … | （其余 5 条阻断 2–3） | | |

orchestrator 被正确识别（petsite → orchestrator → 4 个工具），是个好的自检。

### 一个必须先解决的陷阱：`Delegates` 会把网关藏起来

部署后 orchestrator→adoption 会**同时**存在两条表示：
`Delegates` 直连边，和 `RoutesVia → AgentGateway → RoutesToRuntime` 两跳。

于是任何「绕开网关是否还能到 adoption」的检查都会命中那条 `Delegates`，
**判定网关不是咽喉点** —— 恰好把这套机制要找的东西藏起来。

处置：契约给 `Delegates` 加 **`transitive: true`**，
含义是「本边是一条多跳路径的汇总，不是一次物理调用」。
新增访问器 `is_transitive_edge()` / `physical_dependency_edge_labels()`，
可达性类查询只用物理边集合，语义类查询（「谁依赖谁」）仍包含 transitive 边。
**两类查询要的是两种不同的图，这个标志就是那个开关。**

> ⚠️ `transitive` 的判据是「两端之间是否还存在别的、代表同一次调用的节点」，
> **不是**「弱依赖」或「间接依赖」。误加一条等于在图上凭空断开一条真实路径。
> `tests/test_59_chokepoint_spof.py::t59_01` 锁住这个集合。
>
> 顺带一个没做的判断：`InvokesTool` 其实是**混合**的 ——
> orchestrator 的 4 个工具经网关（逻辑），其余 runtime 的工具是进程内（物理）。
> 那是**边实例属性而非类型属性**，用 `transitive` 这个类型级标志表达不了。
> 目前不影响结论（工具节点与 runtime 节点是不同节点，不构成绕过网关的路径），
> 但若将来要做更精细的路径推导，这里需要一个实例级的标注机制。

### 已知局限（写在查询的 docstring 里，不要当成完整 SPOF 判定）

- **不判冗余**：拓扑上唯一通路即被列出，「是不是真的只有一个副本」不回答。
- **路径截断 3 跳**：更长的绕行不会被发现，因此可能**高估**阻断。
- **不按 `upstream` 过滤**：`upstream=1` 的是单链瓶颈而非扇入型枢纽。
  刻意不滤 —— **网关部署后 `fan_in` 恰好只有 1**（仅 orchestrator），
  按扇入过滤会把最该找到的那个节点漏掉。这是设计这条判据时最反直觉的一点。

### M7 的验收判据

部署后首轮 ETL 跑完，`q_articulation_chokepoints` 应当出现
`AgentGateway/WaggleAIGateway`，阻断数约等于经网关可达的子 agent 数。
**在此之前它不会出现**，因为 `RoutesVia`/`RoutesToRuntime` 还没有实例
（见 `tests/test_11_schema_consistency.py` 的 `PENDING_FIRST_EDGE`）。

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
