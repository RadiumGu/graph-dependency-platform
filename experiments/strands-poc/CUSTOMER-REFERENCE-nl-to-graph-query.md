# Natural Language to Graph Query — Reference Architecture

> **用途**: 为图数据库（Amazon Neptune / Graph 类）场景提供"让业务用户用自然语言查询图"的参考实现
> **推荐路径**: 直接基于 **AWS Strands Agents** 构建，跳过"裸调 Bedrock"的中间态
> **来源**: 一个内部项目（基于 Amazon Neptune + Bedrock + Strands Agents）的落地经验
> **日期**: 2026-04-18
> **读者**: 正在构建图知识平台、受困于 openCypher / Gremlin / SPARQL 编写成本的客户团队

---

## 0. 背景与问题

图数据库（Neptune / Neo4j / TigerGraph / JanusGraph）解决了关系型数据库不擅长的"多跳关系查询"问题，但把能力开放给终端用户时常见两类痛点：

1. **查询语言学习曲线陡**: openCypher / Gremlin / SPARQL 语法对非技术用户门槛过高
2. **图 Schema 演进快**: 节点/边类型频繁增删，查询模板维护成本巨大

常见的效率不高的做法：
- ❌ 让 LLM 裸生成 Cypher，无 schema grounding，准确率 < 50%
- ❌ 只做 exact-match 意图分类 + 模板参数化，覆盖度上不去
- ❌ **自己写 Agent 编排框架**，陷入 tool-call 循环 / memory / 错误处理的重复造轮子

---

## 1. 核心建议：直接用 AWS Strands Agents

对于**新建项目**，我们的强烈建议是：

> 从第一天起就基于 **[AWS Strands Agents](https://strandsagents.com/)** 构建，不要先做"直接调 Bedrock/或Netpune MCP server"的中间态。

理由：
- Strands Agents 是 AWS 主导、Apache-2.0 开源的 Agent SDK，已在 Amazon Q Developer / AWS Glue / VPC Reachability Analyzer 等 production 场景落地
- **核心代码量只有 ~80 行**（Agent 定义 + tool 定义）
- 原生 ReAct + tool-call trace + session memory + OTel 可观测性
- 未来扩展多 Agent（例如给图查询之外再加 RCA / 假设生成 / 告警降噪）时，**框架不变，只加 Agent**

"直接调 Bedrock" 的方案会遇到以下问题：
- 需要 tool-call trace 做调试 → 自己写一套
- 需要多轮会话记忆 → 自己写一套
- 需要 OTel 接 CloudWatch → 自己写一套
- 需要加第 2 个 Agent（不是查询用途）→ 抽象编排层重写

**与其以后重写，不如开始就用框架**。

---

## 2. 架构概览

```
┌────────────────────────────────────────────────────────────┐
│  用户 (业务分析师 / SRE / 产品经理)                         │
│       "petsite 依赖哪些数据库？"                            │
└──────────────────────────┬─────────────────────────────────┘
                           │ 自然语言
                           ▼
┌────────────────────────────────────────────────────────────┐
│  Strands Agent (核心)                                      │
│  - Model: BedrockModel(global.anthropic.claude-sonnet-4-6) │
│  - Tools:                                                   │
│    ├─ get_schema_section   (按需读 schema)                  │
│    ├─ validate_cypher      (安全校验)                       │
│    └─ execute_cypher       (内部强制 Guard + 执行)          │
│  - System prompt: schema + few-shot + 规则                  │
│  - ReAct 多轮: plan → tool → observe → plan...              │
└──────────────────────────┬─────────────────────────────────┘
                           │ openCypher (通过 tool 出口)
                           ▼
┌────────────────────────────────────────────────────────────┐
│  Safety Guard (在 tool 内部，LLM 无法绕过)                 │
│  - READ-only (阻断 CREATE/DELETE/MERGE/SET/...)            │
│  - Hop depth limit (防止 *1..∞ 路径爆炸)                    │
│  - Result size limit (LIMIT 兜底)                          │
└──────────────────────────┬─────────────────────────────────┘
                           ▼
                ┌──────────────────────┐
                │  Amazon Neptune      │
                │  (openCypher / SPARQL)│
                └──────────┬───────────┘
                           │ rows
                           ▼
          Agent 整合结果 → 自然语言摘要 + tool-call trace
                           ▼
                     终端用户界面 (Streamlit / Web / API)
```

**三条设计原则**：
1. Agent 只定义 model + tools + prompt，不自己写编排逻辑
2. Safety Guard **不在 prompt 里**，而在 tool 的 Python 代码里强制执行
3. Schema / few-shot / guard 规则全部外置到 YAML profile，换环境零代码改动

---

## 3. 为什么选 Strands Agents（专题）

这一节集中说明 Strands Agents 相对"裸调 LLM"和其他 Agent 框架的优势。

### 3.1 极简的 API，生产级的能力

完整的可用 Agent 大约 30 行代码：

```python
from strands import Agent, tool
from strands.models import BedrockModel

@tool
def execute_cypher(cypher: str) -> str:
    """执行只读 openCypher 查询。内部强制 Safety Guard。"""
    safe, reason = query_guard.is_safe(cypher)
    if not safe:
        return f"ERROR: unsafe — {reason}"
    cypher = query_guard.ensure_limit(cypher)
    rows = neptune_client.results(cypher)
    return json.dumps(rows[:200], default=str)[:4000]

# 同样定义 get_schema_section / validate_cypher

agent = Agent(
    model=BedrockModel(
        model_id="global.anthropic.claude-sonnet-4-6",
        region_name="ap-northeast-1",
    ),
    tools=[get_schema_section, validate_cypher, execute_cypher],
    system_prompt=build_system_prompt(profile),
)

result = agent("petsite 依赖哪些数据库？")
# result.message          → 自然语言答案
# result.metrics           → token usage + cycles + latency
# result 的 tool-call 历史 → 完整可审计链路
```

对比"自己搭 Agent 框架"：相同功能至少 300-500 行，还要自己处理 tool schema 生成、结果 parsing、错误重试、token 统计、trace 采集。

### 3.2 Model-driven 架构，不和业务 workflow 耦合

其他 Agent 框架（LangChain / LangGraph）要求开发者用 DAG / state machine 显式定义流程。Strands 反其道而行 —— **让现代 LLM 自己决策**：

- 简单问题 → Agent 一次 tool 调用就返回
- 复杂问题 → Agent 自动 ReAct 多轮（"先看 schema 再写 Cypher 再校验再执行"）
- 空结果 → Agent 自动换关系名重试

开发者不写任何"if 空结果 → retry"的流程代码，**Agent 自己决定**。这个理念在 Amazon Q Developer 团队的实践中把"新 Agent 开发时间从几个月压到几周"。

### 3.3 原生 tool-call trace + OTel 可观测性

每次 Agent 调用自带完整结构化 trace：

```json
{
  "message": "petsite 依赖 2 个数据库：RDS / DynamoDB...",
  "metrics": {
    "cycles": 3,
    "latency_ms": 8960,
    "accumulated_usage": {"inputTokens": 12800, "outputTokens": 340}
  },
  "tool_calls": [
    {"tool": "get_schema_section", "args": {"section": "nodes"}, ...},
    {"tool": "validate_cypher", "args": {"cypher": "MATCH..."}, ...},
    {"tool": "execute_cypher", "args": {"cypher": "MATCH..."}, "rows": 2}
  ]
}
```

`STRANDS_TELEMETRY=otlp` + `OTEL_EXPORTER_OTLP_ENDPOINT` 一条 env 即可把全部 trace 接到 CloudWatch / X-Ray / Langfuse / Datadog。

### 3.4 多 Agent 编排是第一等公民

图查询场景常常不是孤立的。一个完整的 RCA 平台可能有：

- Smart Query Agent — 自然语言查图
- Hypothesis Agent — 基于图拓扑生成故障假设
- Chaos Runner Agent — 执行混沌实验
- RCA Prober Agent — 从 CloudWatch / X-Ray 采集证据
- Learning Agent — 闭环学习 + coverage 分析

**用裸 Bedrock 实现** = 5 套各自的编排/trace/memory 逻辑，加起来 2000+ 行胶水代码。

**用 Strands Agents 实现** = 5 个 Agent 共享同一套框架 API，`agent_a.as_tool(agent_b)` 即可互相调用，multi-agent orchestration 原生支持。

Strands 1.0 明确把 "production-ready multi-agent orchestration" 作为核心能力。

### 3.5 模型无关（vendor-flexible）

`BedrockModel` 可替换为 `AnthropicModel` / `LiteLLMModel` / `OllamaModel`：

```python
# 开发测试：用本地 Ollama 跑
from strands.models import OllamaModel
model = OllamaModel(model_id="llama3.1", host="http://localhost:11434")

# 生产：切 Bedrock（零代码改动，只改 factory）
model = BedrockModel(model_id="global.anthropic.claude-sonnet-4-6", ...)

# 合规敏感：切 Anthropic API direct
from strands.models import AnthropicModel
model = AnthropicModel(model_id="claude-sonnet-4-6", ...)
```

不会被单一厂商锁死。对金融 / 政府客户尤其重要。

### 3.6 Session Memory / 会话记忆

用户追问 "那 payforadoption 呢？" 这种依赖上下文的场景，Strands 原生支持：

```python
agent = Agent(model=..., tools=..., session_manager=FileSessionManager(".sessions"))
agent.invoke("petsite 依赖哪些数据库？", session_id="user-123")
agent.invoke("那 payforadoption 呢？", session_id="user-123")  # 自动带上文
```

也可插拔 `mem0.ai` / `Langfuse Memory` 等第三方记忆后端，不用自己写。

### 3.7 安全由 tool 保证，不由 prompt 保证

Agent 框架最大的安全风险是 prompt injection — 用户输入能诱导 LLM 绕过 system_prompt 里的规则。

Strands 的设计鼓励**把安全约束放进 tool 代码**：

```python
@tool
def execute_cypher(cypher: str) -> str:
    # ⚠️ 关键：这是 Python 代码，不是 prompt 指令
    # LLM 即使被 injection 想写 DELETE 也无效
    if not query_guard.is_safe(cypher):
        return "ERROR: blocked"
    ...
```

LLM 只能看到"工具调用失败"，看不到/改不了校验逻辑。这是远比"在 prompt 里写'禁止生成 DELETE'"可靠得多的方案。

### 3.8 AWS 原生生态集成

- IAM SigV4 + Bedrock Guardrails 开箱即用
- 可直接部署到 Lambda / Fargate / App Runner
- OTel 无缝对接 CloudWatch Logs + X-Ray
- 支持 Bedrock Inference Profiles（Global / Regional 路由）
- Strands Labs 社区贡献的 MCP Tool Server、Agent Builder 等扩展

### 3.9 生产案例

Strands Agents 1.0（2025-07 发布）的公开 production 用户：

- Amazon Q Developer — AWS 的 AI Coding Assistant
- AWS Glue — ETL 服务的 AI 助手
- VPC Reachability Analyzer — 网络可达性 AI 诊断
- Accenture / Anthropic / Meta / PwC — 多家合作伙伴贡献

**成熟度足以承载企业生产负载**。

---

## 4. 核心设计模式

### 4.1 Profile YAML — 环境隔离的核心

把"平台专属知识"抽成一个 YAML，代码里不硬编码任何业务词：

```yaml
# profiles/<environment>.yaml
env_name: production
neptune_endpoint: prod-graph.cluster-xxx.<region>.neptune.amazonaws.com

schema:
  nodes:
    - label: Microservice
      properties: [name, service_type, recovery_priority, owner]
      property_values:
        recovery_priority: ['Tier0', 'Tier1', 'Tier2']
  edges:
    - type: AccessesData
      from: Microservice
      to: [RDS, DynamoDB, S3]
      description: "服务读写数据存储"

few_shot_examples:
  - q: "petsite 依赖哪些数据库？"
    cypher: "MATCH (m:Microservice {name:'petsite'})-[:AccessesData]->(d) RETURN d"
  # ... 25-30 条覆盖拓扑 / 依赖 / 故障 / 审计四大意图

common_relations: [AccessesData, DependsOn, Calls, RunsOn, LocatedIn]

guard_rules:
  allowed_labels: [Microservice, RDS, DynamoDB, ...]
  forbidden_operations: [CREATE, DELETE, MERGE, SET, DROP]
  default_limit: 200

complex_keywords:
  zh: ['完整', '全部', '影响', '所有上下游', '路径', '链路']
  en: ['full', 'all', 'impact', 'upstream', 'downstream', 'path']
```

**收益**：
- 新客户/新环境部署 = `cp profile-template.yaml new-env.yaml` + 改 schema + 跑 schema_extractor 半自动补 few-shot
- 预期 0.5-1 人日完成新环境上线
- Schema 演进时只改 YAML 不改代码

### 4.2 System Prompt 结构

```
你是 Amazon Neptune openCypher 查询生成器。根据用户的自然语言问题，
生成正确的 openCypher 查询。

## 图 Schema（从 profile YAML 注入）
Node: Microservice { name, service_type, recovery_priority, owner, ... }
  service_type: ['eks', 'lambda', 'ecs']
  recovery_priority: ['Tier0', 'Tier1', 'Tier2', 'Tier3']
Node: RDS { name, engine, multi_az, ... }
...
Edge: AccessesData from Microservice to [RDS, DynamoDB, S3]
  description: "服务读写数据存储"
Edge: DependsOn from Microservice to Microservice
  description: "服务间依赖"
...

## 规则
1. 只生成 READ 查询，禁止 CREATE/DELETE/SET/MERGE/REMOVE/DROP/CALL
2. RETURN 必须带有意义的别名
3. 多跳遍历限制 *1..5
4. ⚠️ 微服务访问数据库用 AccessesData，不是 DependsOn
   ⚠️ DependsOn 仅用于 ECR 镜像 / SQS 队列依赖

## Agent 调用规则
1. 先判断问题涉及哪些节点/关系；不确定时调用 get_schema_section
2. 生成 Cypher 后先调用 validate_cypher，通过后再 execute_cypher
3. 必须完整保留问题中的所有过滤条件（如 severity='P0'），不得泛化
4. 如果结果为空且你怀疑关系名错了，换关系名重试最多 1 次
5. 最后用 2-4 句自然语言总结结果

## 示例（25-30 条 few-shot）
Q: petsite 依赖哪些数据库？
Cypher: MATCH (m:Microservice {name:'petsite'})-[:AccessesData]->(d)
        WHERE d:RDS OR d:DynamoDB OR d:S3 RETURN d
...
```

### 4.3 Safety Guard — 绝不让 LLM 绕过

```python
# query_guard.py
_WRITE_KEYWORDS = re.compile(
    r'\b(CREATE|DELETE|DETACH|SET|MERGE|REMOVE|DROP|CALL)\b',
    re.IGNORECASE,
)
_HOP_PATTERN = re.compile(r'\*\d+\.\.(\d+)')
MAX_HOP_DEPTH = 6

def is_safe(cypher: str) -> tuple[bool, str]:
    if _WRITE_KEYWORDS.search(cypher):
        return False, "包含写操作关键字"
    for hop in _HOP_PATTERN.findall(cypher):
        if int(hop) > MAX_HOP_DEPTH:
            return False, "遍历深度超限"
    return True, "OK"
```

*设计要点*：Guard 在 `execute_cypher` tool 的 Python 代码里强制执行，不是 LLM 决策的一部分。LLM 即使被 prompt injection 想写 DELETE 也无效，字符串匹配直接拦截。

### 4.4 模型选型（region 约束关键）

我们在 `ap-northeast-1` 的实测结果：

| Model ID | 可用性 |
|----------|--------|
| `global.anthropic.claude-sonnet-4-6` | ✅ 唯一可用 |
| `apac.anthropic.claude-sonnet-4-5` | ❌ ValidationException |
| `us.anthropic.claude-sonnet-4-5` | ❌ ValidationException |
| `anthropic.claude-3-5-sonnet-20241022-v2:0`（裸 ID）| ❌ on-demand throughput not supported |

**结论**：
1. 必须用 Inference Profile，不能用裸 model id
2. 不同 region 可用的 profile 前缀（`global.*` / `us.*` / `apac.*`）不同，上线前必须 probe 一次
3. Global profile 跨 region 路由，**有单点依赖风险**，建议维护备选清单并季度探活

### 4.5 复杂问题自动升级模型

成本优化：简单问题用 Sonnet，复杂问题检测关键词自动切 Opus：

```python
def build_agent_for_question(question: str, profile):
    ck = profile.complex_keywords
    needles = (ck.get('zh') or []) + (ck.get('en') or [])
    ql = question.lower()
    heavy = any(kw.lower() in ql for kw in needles)
    model_id = HEAVY_MODEL if heavy else DEFAULT_MODEL
    return Agent(
        model=BedrockModel(model_id=model_id, region_name="ap-northeast-1"),
        tools=[get_schema_section, validate_cypher, execute_cypher],
        system_prompt=build_system_prompt(profile),
    )
```

---

## 5. 生产数据（20 条 golden query set 实测）

| 指标 | 数值 |
|------|------|
| 准确率（exact match + result correct） | **20/20 = 100%** |
| p50 延迟 | 9.3 秒 |
| p99 延迟 | 19.0 秒 |
| 平均单次 Bedrock cost | $0.01-0.03 |
| 月度成本（中度使用 50 DAU × 10 queries） | ~$180-350 |
| Tool-call trace | ✅ 完整 JSON（每次 ReAct 平均 2-4 cycles）|

**延迟看起来比"裸调 Bedrock"高（那个方案 p50 约 5.5s），是因为 ReAct 原生做多轮推理**（validate → execute → 必要时重试）。换来的是：
- 完整可审计 trace
- 准确率在 schema 变化 / 用户问题刁钻时更稳定（ReAct 能自纠）
- 零编排代码，后续扩展是线性成本

---

## 6. 落地路径建议

| 阶段 | 周期 | 产出 |
|------|------|------|
| **Week 1-2**: Schema 抽取 + Profile 化 | 2 周 | `profile.yaml` + schema_extractor.py 可从 Neptune 元数据自动生成骨架 |
| **Week 3**: Few-shot bootstrap | 1 周 | 用 LLM 基于 schema 生成 15-20 条种子 few-shot，人工筛到 25-30 条 |
| **Week 4**: Golden Query Set | 1 周 | 建立 20 条准确率 baseline CI，用户真实问题采样 |
| **Week 5**: Strands Agent 上线 | 1 周 | ~80 行 Agent + Tool 代码，目标 golden 95%+ |
| **Week 6-7**: 准确率调优 | 2 周 | few-shot 优化 + Opus 关键词触发 + 关系歧义修正 |
| **Week 8**: 稳定期 + 用户反馈 | 1 周 | CloudWatch 看板建立、P0 bug 修复 |
| **可选 Week 9+**: 加 session memory / multi-agent | — | 扩展新 Agent 场景（RCA / 假设生成等） |

8 周即可上线稳定生产版本。

---

## 7. 注意事项

| 陷阱 | 影响 | 缓解 |
|------|------|------|
| LLM 混用语义相近的关系名（`AccessesData` vs `DependsOn`）| 返回空结果，用户误以为真的没有数据 | System prompt 里显式标注 + few-shot 反例 + Agent 调用规则里加"空结果换关系名重试" |
| 返回结果超大导致 Streamlit OOM | 浏览器崩溃 | `ensure_limit()` 自动补 `LIMIT 200` |
| 多跳查询路径爆炸（`*1..∞`）| Neptune 慢查询，账单飙升 | Guard 限制 `MAX_HOP_DEPTH=6` |
| 数据漂移导致 golden 失效（比如 severity 从 P0 变成 P1）| 看似 prompt 在退化，实际是数据变了 | Golden 断言要"动态"（读取当前数据找最高 severity），不要硬编码常量 |
| Bedrock region 不支持某些 inference profile | 上线时发现模型不可用 | 上线前 probe 一次所有候选 profile，维护 fallback 清单 |
| Prompt injection 尝试写 DELETE | 安全事故 | Guard 在 tool 内部，不在 prompt 里；即使 LLM 生成 DELETE 也执行不了 |
| Opus 成本 | 月度账单增加 | 加 rate limit / budget cap；CloudWatch 告警 |
| Strands SDK 升级 breaking | 多 Agent 同时挂 | pin 版本 + 升级前跑完整 golden 矩阵 |
| Agent 泛化过滤条件（如把 `severity='P0'` 忽略）| 返回无关结果 | Agent 调用规则里明确"必须完整保留问题中的过滤条件" |

---

## 8. 我们的建议

### 8.1 Do

- **直接基于 AWS Strands Agents 构建**，不要先做"裸调 Bedrock"的中间态
- 2 天花在 schema profile 化 + few-shot 库（数量胜过质量，25-30 条起步）
- 1 天花在 Golden Query Set + 准确率 CI（后期做任何 prompt 调优的前提）
- Safety Guard 从第一天就要有，放在 tool 的 Python 代码里，绝对**不在 prompt 里**
- 支持 Opus / Sonnet 自动切换，用 profile `complex_keywords` 声明式配置
- OTel 从上线就启用（`STRANDS_TELEMETRY=otlp`），trace → CloudWatch/X-Ray
- 预留 session memory / multi-agent 扩展位（未来加 RCA / 假设生成 Agent 时无缝）

### 8.2 Don't

- **不要自己写 Agent 编排框架** — tool-call loop / memory / trace 每样都是至少 200 行精细代码，Strands 免费给你
- 不要先做 RAG + 向量库做 few-shot 检索，静态 25-30 条通常够用，未来有必要再换 RAG
- 不要做"意图分类 + 模板参数化"替代 LLM 生成，覆盖度和准确率都不如现代 LLM + good prompt
- 不要把 schema 拆成"按 query 类型加载"，Neptune 场景 schema 通常 < 50KB，全量注入 system prompt 足够
- 不要把 Safety 规则写在 system_prompt 里（LLM 可被 injection 绕过）
- 不要裸 Bedrock model id（on-demand throughput 不支持），必须用 Inference Profile
- 不要 pin 到单一 region profile，要维护 fallback 清单

### 8.3 什么时候不该用 Strands Agents

不是所有场景都适合：

- 纯 SQL → 查询结果的简单翻译，**且**永远不会做多轮对话、多 Agent 编排、tool-call 审计 → 直接调 LLM 更省钱（延迟 / token 成本都低 1.5-2x）
- 极度延迟敏感场景（p99 < 2s 的实时推荐）→ ReAct 多轮天然不适合
- 批处理离线任务不需要 Agent 特性 → 直接调 LLM + 静态 prompt 足够

对于**交互式图查询 + 未来会扩展到多 Agent 场景**（RCA / Ops / 假设生成），Strands 是非常推荐。

---

## 9. 成本参考

基于一个中等规模图（~30 节点类型 × ~30 边类型 × ~1000 节点 × ~5000 边）、日活 50 用户、每用户日均 10 次查询的场景：

| 项目 | 月度成本 | 备注 |
|------|---------|------|
| Bedrock (Strands Agent + Sonnet 默认 + Opus 动态升级) | $ 90～180 | 单次查询 $0.01-0.03 |
| Neptune (db.t3.medium serverless) | $200 | 中小规模图 |
| CloudWatch Logs + X-Ray trace | $10-30 | OTel trace 全量采集 |
| Lambda / Fargate (托管 Streamlit) | $30-80 | 看流量 |
| **合计** | **$230-320 / 月** | ~10 人团队的典型图查询平台成本 |

*ReAct 多轮导致 Bedrock cost 比"裸调"高 2-3x*，但绝对值仍在可接受范围。考虑到节省的开发 / 维护人力，TCO 更优。

---

### 9.1 Bedrock Prompt Caching 降本（强烈推荐）

开启 Prompt Caching 后月度账单再降 *40-60%*，实测数据（2026-04-18, 20 条 golden 完整跑一轮）：

| 引擎 | Cache Hit Ratio | 月度成本（未缓存）| 月度成本（启用缓存）| 节省 |
|------|----------------|----------|----------|------|
| Direct Bedrock | *99.6%* | ~$441 | ~$229 | *-48%* |
| Strands Agent  | *90.9%* | ~$1312 | ~$505 | *-62%* |

技术实现极简：

**Strands 版**（一行配置）：
```python
BedrockModel(
    model_id="global.anthropic.claude-sonnet-4-6",
    region_name="ap-northeast-1",
    cache_prompt="default",   # system prompt 缓存
    cache_tools="default",    # tool schema 缓存
)
```

**Direct Bedrock 版**（改一个字段）：
```python
bedrock.invoke_model(body=json.dumps({
    "system": [{
        "type": "text",
        "text": system_prompt,
        "cache_control": {"type": "ephemeral"},  # ← 新增，5 分钟 TTL
    }],
    "messages": [{"role": "user", "content": question}],
}))
```

关键前提条件：
- system prompt 必须 > 1024 tokens（~3000 chars），Sonnet/Opus 的最低缓存 size。大多数包含完整 schema + 15+ few-shot 的生产配置都远远超过。
- Profile 改动 → system prompt 改 → 缓存失效，下次调用付 125% write 成本后重新稳态。高频改 profile 的场景实际节省略低；企业场景的 profile 通常周级以上改动，不影响。
- `_summarize` 或其他结果依赖的调用 *不能*加缓存（前缀不稳定）。项目里这是硬约束。

成本对比（Sonnet 4.6 官方单价）：

| | Regular Input | Cache Write | Cache Read | Output |
|---|---|---|---|---|
| 单价 / 1M tokens | $3.00 | $3.75 | $0.30 | $15 |
| 稳态占比 | 不变 | 1 次 write 后摊至小量 | 10% 价代替 90% input | 不变 |

**结论**：Caching 是当前 Bedrock Agent 场景的"免费午餐"——代码改动 < 10 行，月度账单降 40-60%。特别对 Agent ReAct 多轮场景（每轮都重发 system + tool schema）收益最明显。

---

## 10. 参考资料

- [AWS Strands Agents 官网](https://strandsagents.com/) — 文档、教程、示例
- [AWS Strands Agents GitHub](https://github.com/strands-agents) — 源码 + Issues + 社区贡献
- [Amazon Bedrock Inference Profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles.html) — Global / Regional 路由详解
- [Amazon Neptune openCypher](https://docs.aws.amazon.com/neptune/latest/userguide/access-graph-opencypher.html)
- [AWS Open Source Blog: Introducing Strands Agents 1.0](https://aws.amazon.com/blogs/opensource/introducing-strands-agents-1-0-production-ready-multi-agent-orchestration-made-simple/)
- [Strands Agents SDK: A technical deep dive into agent architectures and observability](https://aws.amazon.com/blogs/machine-learning/strands-agents-sdk-a-technical-deep-dive-into-agent-architectures-and-observability/)

本参考实现（内部项目，可按需脱敏分享）：
- Strands Agent 核心实现：~220 行 Python
- Tool 定义 + Safety Guard：~180 行 Python
- Profile YAML：~400 行 YAML（含 schema + 30 条 few-shot）
- Golden CI：~140 行 Python + 20 条 YAML cases

---

## 11. 附录：为什么 Phase 1 方案（直接调 Bedrock）不推荐为起点

我们内部先做过 "直接调 Bedrock" 的 Phase 1 实现，再迁移到 Strands Agents 的 Phase 2。**如果重来一次，会直接从 Strands 开始**。

Phase 1 的遗留问题：
- 每次迭代都要写"生成 → guard → execute → 摘要"的胶水代码
- 加一个"空结果重试"功能 → 自己写 `_should_retry_on_empty` + `_retry_with_hint` ~60 行
- 加一个"复杂问题升 Opus" → 自己写 `_select_model` + 关键词匹配 ~20 行
- 想看 tool-call trace → 没有，只能看 log
- 想加 session memory → 推倒重来
- 想加第 2 个 Agent（例如 RCA）→ 发现编排逻辑完全不能复用，又写一遍

这些都是**框架给你的东西**。从 Strands 起步能直接跳过，把人力投入到业务 schema / few-shot / golden set 这些真正有价值的事。

*我们做迁移付出了~3 周人力*，如果你项目还没开始，可以省下这 3 周。

---

## 联系方式

如需进一步讨论架构细节、代码脱敏分享、或 AWS Solution Architect 支持，请联系你的 AWS 客户团队。
