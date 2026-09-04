# 调研：图谱平台复用在 GenAI Agent 依赖关系上

> 生成时间：2026-09-04 背景：Graph Dependency Platform 当前面向微服务（EKS + AWS 托管服务），本文调研将图谱扩展到 GenAI Agent 系统依赖关系管理的可行性。

---

## 1. 为什么 Agent 系统需要依赖图谱

Agent 系统的依赖比传统微服务**多出三个维度**：

| 依赖类型 | 传统微服务 | GenAI Agent 系统 |
| --- | --- | --- |
| 服务间调用 | A 调 B（HTTP/gRPC） | Agent 委派 Sub-Agent（A2A 协议 / DELEGATED_TO） |
| 数据访问 | Service → DB / S3 | Agent → Knowledge Base / RAG 数据源 / Memory Store |
| 工具调用 | — | Agent → MCP Server → Tool（**新增维度**） |
| 模型依赖 | — | Agent → LLM Model（**新增维度**） |
| 会话跨 Trace | — | 多轮对话跨多个 Trace，共享 conversation_id |

更关键的是：Agent 的依赖关系是 **LLM 在运行时决定**的——不是代码写死的。同一个 Agent 面对不同 query 可能调用完全不同的工具和 sub-agent。这意味着：

1. **static/dynamic 的区分更加重要**——Agent 的配置声明了它"可以"调哪些工具，但运行时它"实际"调了哪些，两者经常不一致。
2. **依赖图是动态变化的**——不是部署后固定，而是随每个 query 变化。
3. **爆炸半径分析更复杂**——一个 Tool 下线，不是所有使用它的 Agent 都会受影响，只有那些在特定 query 模式下依赖它的才会。

---

## 2. 采集源：用什么工具采集 Agent 依赖关系

### 2.1 AgentCore Observability（核心采集源）

**来源**：[AWS Well-Architected Agentic AI Lens - AGENTREL07-BP03](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentrel07-bp03.html)

AgentCore Observability 是默认的遥测服务，OTel 兼容，**自动采集以下内容**（无需手动埋点）：

| 自动采集的 Span | 对应的图谱边 | 关键属性 |
| --- | --- | --- |
| LLM inference call | Agent → Model (EXECUTED) | `gen_ai.provider.name`, `gen_ai.response.model`, token counts |
| Tool invocation | Agent → Tool (CALLED / INVOKED) | `gen_ai.tool.name`, 执行结果, 耗时 |
| Memory operation | Agent → Memory Store (ACCESSED) | 读/写类型, key |
| Reasoning step | 内部 Span（不产生边，但可用于分析） | step 内容, 决策路径 |
| Agent delegation | Agent → Sub-Agent (DELEGATED_TO) | 被委派 agent ID, task type |

**三层结构**：Session → Trace → Span

- Session = 完整用户交互（可能跨多个 Trace）
- Trace = 单次请求-响应周期
- Span = 具体操作（LLM 调用 / 工具调用 / 推理步骤）

**实现方式**：

```
AgentCore Runtime → 自动 OTel instrumentation → CloudWatch（metrics + logs + traces）
                                               → X-Ray（分布式追踪）

```

### 2.2 Strands Agents Framework Traces

Strands 框架内置 OTel instrumentation，自动捕获：

- Reasoning steps 作为子 Span
- Tool calls 带 `gen_ai.tool.name` 属性
- 六条 LLM 路径的执行链

**与 AgentCore Observability 的关系**：Strands traces 可以直接被 AgentCore Observability 消费，两者是互补而非替代。

参考：[Strands Agents Traces](https://strandsagents.com)

### 2.3 MCP Gateway Telemetry

MCP（Model Context Protocol）的调用链：

```
MCP Client → MCP Gateway → Lambda → Tool 执行

```

每一跳都有 X-Ray trace，可以复用你现有的 `etl_from_xray`。

关键点：MCP Gateway 的 telemetry 同时输出到 X-Ray（分布式追踪）和 CloudWatch（metrics + logs），提供端到端可见性。

参考：[AWS Prescriptive Guidance - MCP Server](https://docs.aws.amazon.com/prescriptive-guidance/latest/semantic-layer-agentic-ai-ontology-reasoning-virtual-knowledge-graph/mcp-server.html)

### 2.4 OTel GenAI Semantic Conventions（v1.37）

标准化属性名，是跨厂商采集的基础：

| 属性 | 含义 | 用途 |
| --- | --- | --- |
| `gen_ai.agent.id` | Agent 标识 | 节点身份键 |
| `gen_ai.tool.name` | 工具名 | Tool 节点身份键 |
| `gen_ai.provider.name` | 模型提供商 | Model 节点的 composite key 一部分 |
| `gen_ai.response.model` | 模型名 | Model 节点的 composite key 一部分 |
| `gen_ai.conversation.id` | 会话 ID | Session 节点身份键（跨 Trace） |
| `gen_ai.operation.name` | 操作类型（chat/embedding） | 边的语义分类 |

**注意：现实中各 instrumentor 实现不一致**。根据 [otel-genai-graph](https://agentic-ai-visibility.hashnode.dev/your-llm-traces-deserve-a-graph-not-a-timeline) 的调研：

- Google GenAI instrumentor 发的是 `gen_ai.system = "gemini"` 而非 `gen_ai.provider.name = "gcp.gen_ai"`
- `gen_ai.conversation.id` 很多 instrumentor 不发
- 需要一张 legacy-compat 映射表来归一化

### 2.5 A2A Protocol（Agent-to-Agent）

Agent 间委派自带 trace context propagation，可以产生 DELEGATED_TO 边。

### 2.6 Bedrock API CloudTrail

- `InvokeModel` / `InvokeAgent` 事件 → Agent → Model 边
- Knowledge Base query 事件 → Agent → KnowledgeBase 边
- 可以复用现有的 EventBridge → ETL trigger 机制

### 2.7 采集架构总览

```
AgentCore Runtime
├── Observability（默认开启，OTel 兼容）
│   ├── Session → Trace → Span 三层结构
│   ├── 自动捕获：LLM inference / tool invocation / memory ops
│   └── 输出到 CloudWatch（metrics + logs + traces）+ X-Ray
│
├── Strands Agents Framework
│   ├── 内置 OTel instrumentation
│   ├── reasoning steps 作为子 Span
│   └── tool calls 带 gen_ai.tool.name 属性
│
├── MCP Gateway
│   ├── X-Ray trace：MCP Client → Gateway → Lambda
│   └── 可以复用现有的 etl_from_xray
│
└── CloudTrail
    ├── InvokeModel / InvokeAgent 事件
    └── 可以复用现有的 EventBridge → ETL trigger

```

---

## 3. 图谱 Schema 扩展方案

### 3.1 新增节点类型

| 节点类型 | 身份键 | 来源 | 说明 |
| --- | --- | --- | --- |
| `Agent` | `agent_id` | AgentCore Observability / Strands | 一个 AI agent 实例 |
| `Model` | `(provider, model_name)` composite | OTel `gen_ai.provider.name` + `gen_ai.response.model` | 区分 openai/gpt-4o 和 openai/qwen2.5:7b-ollama |
| `Tool` | `tool_name` | OTel `gen_ai.tool.name` / MCP Gateway | 一个可调用的工具 |
| `MCP_Server` | `server_id` | MCP Gateway telemetry | 托管一组 Tool 的 MCP 服务端 |
| `KnowledgeBase` | `kb_id` | Bedrock KB API / CloudTrail | RAG 知识库 |
| `Session` | `conversation_id` | OTel `gen_ai.conversation.id` | 跨 Trace 的用户会话 |

### 3.2 新增边类型

| 边类型 | 方向 | dependency | 语义 | TTL 建议 |
| --- | --- | --- | --- | --- |
| `Invokes` | Agent → Tool | true | Agent 调用了某个 Tool | 30min（同 Calls） |
| `Delegates` | Agent → Agent | true | Agent 委派子任务给另一个 Agent | 30min |
| `ExecutesOn` | Agent → Model | true | Agent 使用某个 LLM 模型推理 | 6h（模型选择相对稳定） |
| `RetrievesFrom` | Agent → KnowledgeBase | true | Agent 从 KB 检索上下文 | 6h |
| `HostsOn` | MCP_Server → Tool | false（结构边） | MCP Server 承载某个 Tool | null（跟随节点） |
| `PartOf` | Agent → Session | false（结构边） | Agent 参与某个会话 | null |

### 3.3 dependency_kind 扩展

现有两个取值不足以覆盖 Agent 场景，建议增加第三个：

| 取值 | 含义 | 适用场景 |
| --- | --- | --- |
| `dynamic` | 运行时持续观测到流量 | 微服务间的 HTTP/gRPC 调用 |
| `static` | 配置声明了关系，未观测到流量 | CFN / AWS API 声明的资源关系 |
| `inference` | **LLM 推理时动态决定的调用** | Agent 根据 query 决定调哪个 Tool / Sub-Agent / Model。特点：不是配置写死的（≠static），也不是持续存在的（≠dynamic），而是按 query 按次触发的。一条 inference 边可能在某些 query 模式下频繁出现，在另一些模式下从不出现。 |

`inference` 类型的引入对证伪有直接影响：

- 验证 `inference` 边需要**在特定 query 模式下**注入故障，而不是持续注入
- 置信度计算需要考虑 query 分布——低频 query 触发的边置信度天然低

---

## 4. 需要新增的 ETL

### 4.1 etl_from_agentcore（核心新增）

| 属性 | 值 |
| --- | --- |
| **源** | AgentCore Observability OTel spans |
| **触发** | 定时（建议每 5 分钟，与 deepflow-etl 对齐） |
| **采集方式** | 读取 CloudWatch OTel traces / X-Ray GetServiceGraph（Agent 维度） |
| **写什么** | Agent / Model / Tool / KnowledgeBase 节点 + Invokes / Delegates / ExecutesOn / RetrievesFrom 边 |
| **独立 Lambda** | 是（同 xray-etl 的设计原则：独立观测源必须架构独立） |

### 4.2 Session 归并逻辑

Agent 场景的特殊挑战：一个多轮对话产生多个 trace_id，但共享同一个 `gen_ai.conversation.id`。

ETL 需要：

1. 按 `gen_ai.conversation.id` 合并 Session 节点（跨 Trace 的幂等 upsert）
2. 同一 Session 内的多个 Agent 通过 `PartOf` 边关联
3. 注意：很多 instrumentor 不发 `gen_ai.conversation.id`，需要 fallback 到 `session.id` / `langsmith.trace.session_id` 等 legacy key

---

## 5. 证伪扩展：Agent 系统的故障注入

| 注入方式 | 验证什么 | 对应微服务的类比 |
| --- | --- | --- |
| 禁用某个 Tool | Agent 是否真的依赖这个 Tool？禁用后输出质量是否下降？ | 下游服务注入 500 |
| 下线某个 Sub-Agent | 父 Agent 是否能 fallback？ | 下游服务完全不可用 |
| Mock 某个 Model（返回低质量结果） | Agent 输出质量是否对 Model 质量敏感？ | 下游服务注入延迟 |
| 清空 Knowledge Base | Agent 是否真的依赖 RAG 上下文？ | 数据库清空 |

**判定指标需要扩展**：

- 微服务：success_rate / latency / error_count
- Agent：**output_quality**（需要 LLM-as-judge 或人工评估）/ latency / tool_call_success_rate / hallucination_rate

这是 Agent 证伪比微服务证伪更难的地方——Agent 的"故障"表现为**输出质量下降**，而不是 HTTP 500。

---

## 6. 参考实现：otel-genai-graph

来源：[Your LLM traces deserve a graph, not a timeline](https://agentic-ai-visibility.hashnode.dev/your-llm-traces-deserve-a-graph-not-a-timeline)

一个已经在做"OTel GenAI spans → Neo4j 图"的开源项目，schema 简洁：

| 节点 | 自然键 | 对应我们可以加的 |
| --- | --- | --- |
| Session | conversation_id | Session |
| Agent | agent_id | Agent |
| Model | (provider, name) composite | Model |
| Tool | tool_name | Tool |
| DataSource | data_source_id | KnowledgeBase |
| Operation | span_id | 细粒度 Span（可选） |

**7 种边类型**：CONTAINS / EXECUTED / INVOKED / CALLED / RETRIEVED_FROM / PARENT_OF / DELEGATED_TO / ACCESSED

关键设计值得借鉴：

1. **Model 用 composite key (provider + name)**——区分 openai/qwen2.5:7b (local Ollama) 和 openai/gpt-4o (cloud)
2. **Session 跨 Trace 合并**——多个 trace_id 共享一个 conversation_id
3. **DELEGATED_TO 边**——`MATCH (a:Agent)-[:DELEGATED_TO*]->(b)` 一条查询算 Agent 爆炸半径
4. **7 个结构性不变量**（edge endpoint types / session uniqueness / cardinality / DAG check 等）——类似我们的契约门禁
5. **Legacy-compat 映射表**——处理各 instrumentor 不遵循 v1.37 规范的问题

---

## 7. 核心结论

1. **完全可以复用**。现有图谱平台的核心机制（幂等 upsert、写一次溯源、契约门禁、TTL 生命周期、混沌证伪）全部适用于 Agent 依赖关系。
2. **最大采集源是 AgentCore Observability**——它已经自动采集了 LLM 调用、Tool 调用、reasoning steps，只需写一条新 ETL 做映射。
3. **需要新增一个 dependency_kind 取值 **`inference`——Agent 的依赖是 LLM 运行时按 query 决定的，既不是 static 也不是 dynamic。
4. **证伪更难**——Agent 的"故障"是输出质量下降而非 HTTP 500，判定指标需要从 success_rate 扩展到 output_quality（LLM-as-judge）。
5. **图查询的价值更大**——Agent 系统的爆炸半径 / SPOF / 关键路径分析比微服务更复杂（多了 Tool / Model / Sub-Agent 三个维度），更不应该交给 LLM 推理。

---

## 参考文献

1. [AGENTREL07-BP03 Implement distributed tracing to track system dependencies and facilitate recovery](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentrel07-bp03.html) — AWS Well-Architected Agentic AI Lens
2. [AGENTOPS05-BP01 Establish end-to-end tracing and telemetry for agent operations](https://docs.aws.amazon.com/wellarchitected/latest/agentic-ai-lens/agentops05-bp01.html) — AWS Well-Architected Agentic AI Lens
3. [Your LLM traces deserve a graph, not a timeline](https://agentic-ai-visibility.hashnode.dev/your-llm-traces-deserve-a-graph-not-a-timeline) — otel-genai-graph 项目
4. [OTel GenAI Semantic Conventions v1.37](https://opentelemetry.io/docs/specs/semconv/gen-ai/)
5. [Amazon Bedrock AgentCore Observability](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/observability-telemetry.html)
6. [A Guide to Instrumenting AI Agents for the Agent Timeline](https://www.honeycomb.io/blog/instrumenting-ai-agents-agent-timeline-opentelemetry-guide) — Honeycomb
7. [MCP Server Telemetry](https://docs.aws.amazon.com/prescriptive-guidance/latest/semantic-layer-agentic-ai-ontology-reasoning-virtual-knowledge-graph/mcp-server.html) — AWS Prescriptive Guidance

