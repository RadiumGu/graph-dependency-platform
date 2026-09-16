# agentobv — PetSite 接入 Agent 系统 + Agent 可观测性

**调研日期**：2026-09-04
**输入**：`../research-agent-dependency-graph_20260904-0749.md`（图谱扩展到 GenAI Agent 的可行性调研）
**方法**：6 条并行网络调研线（限定纯网络、禁止描述本地文件）+ 主会话对本地仓库与活环境实测交叉核实

---

## 文档

| 文件 | 内容 |
|---|---|
| `01-agent系统接入方案_20260904-0815.md` | 问题一：怎么在 PetSite 里加 agent 系统 |
| `02-agent可观测性方案_20260904-0815.md` | 问题二：怎么观测它，怎么接进现有依赖图谱 |
| `03-可复用资产与证据清单_20260904-0815.md` | 证据底座、可复用代码、**以及查不到什么**（第 4.1 节已撤销重写） |
| `04-FIS模板库与agent注入_20260904-0830.md` | **推翻了 `03` 一个结论**：agent 注入该用 FIS 编排；含四个陷阱（三个会产假 PASS） |
| `05-etl_xray影响面量化_20260904-0835.md` | 执行顺序第 1 步的**产出**：影响面极窄、TS 当前未开、基线已记录、残余一个未知含可逆实测方案 |

---

## 三条最重要的结论

### 1. 不要从零设计 —— 上游已经建好了

`aws-samples/one-observability-demo`（PetSite 自己的上游）已有 `src/applications/microservices/waggle_ai_agents/`：**五个 agent、五种框架**（Strands 编排 + LangGraph / CrewAI / LlamaIndex / OpenAI Agents 四个子 agent）、跑在 **AgentCore Runtime**、6 个 CDK 构造（Runtime / Gateway / Memory / Guardrail / Autoreload / KB）、10 篇 RAG 知识文档。

**真正的工作是移植，而移植成本来自 fork 已落后 277 个提交且上游重构了整个目录布局**（`PetAdoptions/` → `src/`，`petlistadoptions` 从 Go 重写成 Python）。

推荐**只移植 agent 模块、作为第三个独立 stack**，不做整体升级——那会把「加一个 agent」变成「重做整套部署」。

### 2. 观测有一个账号级硬门槛，而它会碰到本项目已有的 `etl_xray`

**必须开启 CloudWatch Transaction Search**，否则看不到任何 agent 的 span/trace。而官方原文：

> "Transaction search is configured for **the entire account** and switches **all spans ingestion through X-Ray** into cost effective collection mode."
> "Ingest 100 percent of spans as structured logs in CloudWatch... **Index a percentage of spans as trace summaries in X-Ray**."

本项目有一条 `neptune-etl-from-xray` 靠 X-Ray 建依赖边。**开启前必须先量它的基线并评估索引百分比的影响**——此前一轮刻意没开的顾虑，现在被官方文档坐实为真实机制。

另一条硬限制：**ADOT Collector 不支持 agent observability，只能用 ADOT SDK 进程内埋点**。这否掉了「复用集群里现有 4 个 `aws-otel-collector` sidecar」的想法。

### 3. 两处既有说法被推翻

| 原说法 | 实际 |
|---|---|
| VPC Lattice 能让 Gateway 直连 ClusterIP | **NOT FOUND** —— 官方只说「私有 MCP server / 内部 REST API / 数据库」，没说 ClusterIP。前面仍需 internal LB 或自建 MCP server 适配层 |
| `gen_ai.system` vs `gen_ai.provider.name` 是 instrumentor 实现不一致 | `gen_ai.system` 是**被官方弃用并移出注册表**的旧属性。这是版本迁移，不是厂商差异 |

并且：**全部 `gen_ai.*` 属性的 stability 都是 `development`，没有一个 stable**。这与本项目「身份键必须不可变」的契约硬约束冲突 —— 处置办法是把身份键钉在 **AWS 侧标识**（agent ARN / Bedrock model ID / KB ID）而非 OTel 属性上。

---

## 值得单独记住的两件事

**AgentCore Evaluations（GA 2026-03-31）的 `expected_trajectory`** —— 它评的是 agent **走了哪条路径**而不只是答案对不对。这正好是「验证一条 `Delegates` / `Invokes` 边真实存在」需要的判据形态，比 LLM-as-judge 更贴合本项目的证伪目标。

**AWS FIS 的 action 清单里没有任何 Bedrock / AgentCore / GenAI action，但这不代表 FIS 做不了 agent 注入。** `aws-samples/fis-template-library` 的 `agentcore-strands-agent-faults` 用通用的 `aws:ssm:start-automation-execution` 把 FIS 作为**编排器**，注入点放在 agent 进程内（plugin 读 SSM Parameter Store 的 `/chaos/{runtime_id}/*`），在 **tool 边界**注入——pre-hook 取消调用（`timeout`/`network_error`/`execution_error`/`validation_error`）或 post-hook 污染结果（`truncate_fields`/`remove_fields`/`corrupt_values`）。

> 我最初写的是「不要指望 FIS」，**那是错的，已撤销**。这是同一类推理错误的第三次（前两次：误判 78 条边阻塞在自建 SSM、误判 `disrupt-vpc-endpoint` 可用）。准确的纪律是：「list-actions 里有 ≠ 环境里能选出目标」的**反面同样成立**——「清单里没有 ≠ 这件事做不到」，要枚举到「官方样例怎么组合现有原语」这一层。详见 `04`。

**这个模板自带一台假 PASS 生成器,必须先建门禁再跑**：tool 名是精确匹配的，**名字对不上就零注入而实验仍报「成功」**。与本项目已踩过的坑同形（T-214h 假 PASSED、`petsearch->s3` 注入未生效却一度判 confirmed）。门禁需双判据：SSM 参数确认 `active=true` **且**至少一条 `activated chaos` 日志。

---

## 执行顺序（依赖已排过，不要按文档顺序做）

1. ~~量 `etl_xray` 基线~~ **✅ 已完成（`05`）** —— 只调 `GetServiceGraph`、只取拓扑、TS 当前未开、基线 52 Services / 49 Edges
2. 评估并开启 Transaction Search
3. 扩契约（6 类节点 + 6 类边 + `dependency_kind` 的 `inference` 取值）+ 守门测试 —— **契约先于 ETL**，否则写入门禁会拒
4. 移植并部署 agent
5. **实测各框架实际发出的 span 属性名** —— 官方没给可照抄的属性矩阵（两处 NOT FOUND），只能实测
6. 写 `etl_agentcore`（arm64）并部署
7. 复测 `etl_xray` 与步骤 1 基线对比
8. **agent 侧证伪**（用 FIS 编排，展开见 `04` 第六节）：
   - 8a. 部署**专用 chaos runtime**，与生产 runtime 分离（生产构建不含 chaos 模块、无 `/chaos/*` 权限）
   - 8b. vendored `strands_agentcore_chaos.py`，接 `plugins=chaos_plugins()`
   - 8c. 建 3 个 IAM 角色（FIS 实验 / SSM Automation / chaos runtime 执行）+ 部署 `ChaosExperiment` SSM 文档
   - 8d. **把 `fault_injections` 的 tool 名改成本环境真实注册的 tool 名** —— 模板默认值是 demo 的 `get_move`/`get_pokemon`，不改就零注入假成功
   - 8e. **先建注入生效门禁**（SSM 参数 + `activated chaos` 日志双判据），再跑第一个实验
   - 8f. 从 `fault_rate` 0.05–0.1 与单个 `timeout` 起步，再升级到 `corrupt_values` 与多 tool
   - 8g. 注意 `maxDuration` 必须 > `DurationSeconds` + 15s，否则实验窗口静默截断
