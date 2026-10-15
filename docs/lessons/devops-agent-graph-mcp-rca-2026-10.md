# 验证：东京 DevOps Agent 调用 AgentCore 上的依赖图 MCP 加速 RCA

**日期**：2026-10-15
**结论**：**可行，而且两种方式要一起用** —— MCP 是能力，Skill 是让它被正确使用的规程。
但验证过程中发现**线上 MCP 已漂移 40 天、在 RCA 场景下给出方向颠倒的答案**，
这个问题比「能不能接」更要紧，已修复并加了门禁。

---

## 一、接入链路：已经全部打通

| 环节 | 现状 | 证据 |
|---|---|---|
| Agent Space | `petsite-devops`（东京） | `list-agent-spaces` |
| MCP 注册 | `graph-dependency`，OAuth client_credentials（Cognito） | `list-services` |
| 关联到 Space | 24 个工具全部 allowlist，均标 `READ_ONLY` | `list-associations` |
| AgentCore Runtime | `graph_dependency_mcp-12Vg2Z9XXu`，VPC 模式，JWT authorizer | `get-agent-runtime` |
| 端到端 | 以 DevOps Agent 同样的路径（Cognito 取 token → Bearer → Runtime）调用成功，单次 1.5–2.8s | 实测 |

**DevOps Agent 侧的要求**（官方文档 + 已注册实例核对）：MCP server 在账号级注册
（支持 OAuth Client Credentials / API Key / Bearer Token 三种鉴权），再在
Agent Space 里按工具 allowlist。AgentCore Runtime + Cognito M2M 正好对上
OAuth Client Credentials —— 这是官方博客（EKS 节点诊断、ServiceNow）同款路径。

**历史使用率**：近 15 个执行里 12 个调用了 graph MCP。所以「agent 会不会用它」
不是问题 —— **问题是它用到的答案对不对**。

---

## 二、真正的问题：线上 MCP 漂移了 40 天，RCA 答案方向颠倒

### 发现

线上 Runtime v3 部署于 **2026-09-05**。此后它打包的依赖代码：

| 文件 | v3 之后的修改次数 |
|---|---|
| `rca/neptune/neptune_queries.py` | 5 |
| `profiles/graph_contract.yaml` | 14 |

**MCP 一次都没重新部署。** 其中 `1c1a1b7`（2026-09-06）修了 q1/q3 的
**方向反了 + 只看 2/7 种依赖边** —— 修复到了 Lambda 和本地，从没到 MCP。

### 后果（实测，同一张图）

v3 一列是**把 v3 包里的原版 `neptune_queries.py` 加载起来、打同一个线上 Neptune** 跑出来的，
不是推算。v3 的 q3 是 `MATCH (upstream)-[r:Calls|DependsOn]->(n {name: $svc})` ——
入边，且只看 2 种边。

| 查询 | 线上 v3 | main |
|---|---|---|
| `q3(petsearch)` 根因候选 | WaggleAIAdoption, payforadoption, petlistadoptions, petsite, search_available_pets, trafficgenerator —— **全是调用 petsearch 的（受害者）** | ddbpetadoption, S3 桶, ssm, sts, dynamodb … —— petsearch 依赖的（真正的根因候选） |
| `q1(ddbpetadoption)` 影响面 | **0** | **21** |

**DevOps Agent 问「petsearch 慢，根因是什么」，拿到的是「谁在调用 petsearch」**
—— 带着 `_provenance` 与「确定性查询，无 LLM 参与」的权威标签。
2026-09-20 的 `petsearch-latency-high` 调查调用了 `q3_upstream_deps(failed_service=petsearch)`；
当天线上跑的是 v3（2026-09-05 部署，直到 2026-10-15 才更换）。
journal 只记录调用、不保存工具返回，所以**无法直接取证那次拿到的内容**；
但 v3 对同一入参的返回已在上表实测 —— 是受害者列表。

### 修复

- Runtime 更新到 v5（v4 修代码，v5 修描述），authorizer / VPC 完好
- 新增 `graph_mcp/deploy_runtime.sh`：此前每次手敲 `update-agent-runtime`
- 新增 `tests/test_137_mcp_runtime_not_stale.py`：比对线上 artifact 与 main 的
  6 个源码文件哈希。负向验证：本地改一个字节，门禁立即变红

部署时踩到两个坑，已固化进脚本：

1. **必须全量重传配置块** —— 只传 artifact 会清掉 authorizer / network
2. **不得在请求里出现 `requireServiceS3Endpoint`** —— 对 2026-06-08 之后创建的
   agent，即使传的就是当前值 `false` 也会 `ValidationException`。
   所以「get 回来原样 update 回去」这个直觉做法会失败

---

## 三、第二个问题：工具描述建议的 `kind='live'` 会滤掉大多数根因

q1/q3 的描述原本写「建议 kind='live'」。`live = dynamic AND active=true`，
它滤掉的不只是失效边，还有**全部声明边（static）** —— 而数据层依赖
（→ DynamoDB / RDS / S3 / SQS）大多正是声明边：SDK 长连接与 VPC 端点
不产生观测源能看到的流量。

实测（2026-10-15）：

| 查询 | `kind="live"` | 不带 kind |
|---|---|---|
| q3(petsearch) 数据层根因候选 | 0 | 2 |
| q3(payforadoption) | 1 | 6 |
| q3(petsite) | 2 | 6 |
| **q1(RDS 集群 serviceseks2-database…) 影响面** | **0** | **11** |
| q1(ddbpetadoption) | 4 | 21 |

**用 live 查一次 RDS 故障会得到「无影响」。** 而真正陈旧（active=false）
的边每个服务只有 0–3 条 —— 多给几个候选的代价远小于漏掉根因。

修复：q1/q3 的工具描述改为「RCA 时不要传 kind」并附实测数据；
内部调用方 `rca/collectors/layer2_tools.py`（Layer2 探针据此决定去查哪些资源）
同步去掉 live。`graph_rag_reporter.py` 的 live **刻意保留** —— 那里判的是
「传播方向是否被实际观测到的调用印证」，声明边混进来会把「可能传播」当成
「实际传播」。两处注释都写明了这个区别。

---

## 四、要不要 Skill：要，而且是新写的

### 为什么光有 MCP 不够

2026-09-07 的实测：同一个依赖问题问 12 次，**不点名图谱时只有 25% 会去查**。
MCP 的工具描述只在 agent 已经决定要用某个工具时才被读到；
**「什么时候该先查图谱」只能靠 Skill 的 description 触发**。

另外上面两个问题（q3 名字里的 upstream 与语义相反、live 会漏根因）都属于
「工具本身没错、但用法有陷阱」—— 这正是官方文档里 Skill 的定位之一：
*Amplify custom tools — document when to invoke specific tools, what parameters
to use, and how to interpret results.*

### 新 Skill：`dependency-graph-rca`

位置：`graph_mcp/devops_agent_skill/dependency-graph-rca/`

```
dependency-graph-rca/
├── SKILL.md                              # RCA 主流程（Step 0–5 + 5 条硬规则）
└── references/
    ├── verification-semantics.md         # 判定四态、观测层 vs 干预层、方向、kind
    └── tool-quick-reference.md           # 24 个工具速查 + 常见错误表
```

与旧 skill `dependency-verification-graph`（2026-09-07）的区别：旧的定位是
「回答依赖问题时先查图」，**新的定位是 RCA 流程本身** —— description 按官方要求
从 agent 视角列出触发场景（incident / alarm / latency / 5xx / 具体服务名），
正文给出「先根因候选 → 再查变更 → 再影响面 → 按证据加权」的顺序。
旧 skill 已设为 `INACTIVE`（未删除，可随时恢复）。

按官方约束逐项校验通过：

| 约束 | 实际 |
|---|---|
| name：小写字母数字连字符，≤64，不以连字符首尾 | `dependency-graph-rca` |
| description：建议 ≥100、最多 1024 字符 | 754 |
| 无 `scripts/` 目录（否则上传被拒） | 无 |
| zip ≤ 6 MB / ≤ 100 文件 | 14.5 KB / 3 文件 |

已通过 `create-asset`（zip 方式）上传到 `petsite-devops`，Agent Type = Generic。

---

## 五、端到端盲测：Skill + MCP 在真实调查里的表现

发起一次只给症状的调查（未提图谱、未提 skill）：

> petsearch p99 latency has been elevated for the last 30 minutes and some
> search requests from the petsite frontend are timing out.

恰好 petsearch 在 13:10–13:17 UTC **真的发生了延迟尖峰**（p90 约 0.5s→2.9s），
所以这是一次真实事件的调查。

### Agent 的前 10 个动作

```
 0 datetime
 1 fs_read  <agent 沙箱>/skills/user/dependency-graph-rca 的 SKILL 文件   ← 第 2 个动作就加载了新 skill
 2 fs_read  /aidevops/memory/monitors/INDEX.md
 3 graph-dependency_q4_service_info          {service_name: petsearch}
 4 graph-dependency_q3_upstream_deps         {failed_service: petsearch}           ← 没传 kind
 5 graph-dependency_q1_blast_radius          {failed_node: petsearch}              ← 没传 kind
 6 graph-dependency_q19_topology_changes     {service_name: petsearch, since_seconds: 86400}
 7 graph-dependency_q22_edge_verification_verdicts {service_name: petsearch}
 8 graph-dependency_q18_chaos_history        {service_name: petsearch}
 9 graph-dependency_q9_service_infra_path    {service_name: petsearch}
```

**7 个图谱查询全部在看任何指标之前完成**，顺序与 skill 的 Step 0→5 一致，
q1/q3 都按 skill 的要求**没传 kind**。

### 图谱结论驱动了后续分派

随后派出的 5 个子代理中，`petsearch-datalayer` 拿到的是 q3 返回的**精确资源名**
（`ServicesEks2-ddbpetadoption7B7CFEC9-…`、`serviceseks2-s3bucketpetadoption…`），
不是「去查查数据库」。

### 结论里图谱独有的信息

Agent 产出的 topology 记录原文：

> **Blast radius (fault-injection confirmed):** petsite→petsearch 62%
> (explains frontend timeouts), payforadoption, petlistadoptions (Tier1),
> trafficgenerator (Tier2), WaggleAI search_available_pets 96%.
>
> **Topology changes:** only a recurring `WaggleAIAdoption→petsearch`
> deactivation (low-traffic agent) — not the trigger.

- 「petsite→petsearch 62%」来自 q22 的 `verify_degradation` —— 用**故障注入证实**的
  依赖强度解释了题干里「前端超时」的症状。这个数字 CloudWatch 里不存在。
- 用 q19 **排除**了拓扑变更这条线。

### 证据纪律

- 第一个假设（S3 CreateBucket，来自 q3 的候选）被 X-Ray **证伪**
  （409 在基线窗口也存在）—— 图谱给候选、遥测做裁决，这是设计意图
- 最终指向 STS / SSM 凭证解析，它们也在 q3 的 8 个候选里
- 所有延迟数值来自 X-Ray / CloudWatch；缺口如实标注
  （「S3 request metrics 未开」「X-Ray 采样稀疏，无真 p99」）
- `verify_status` 在调查记录中出现 26 次，`untested` 10 次 —— 引用了证据等级

### 未能核实的部分

- 「没有 skill 时会怎样」的对照组本次没跑 —— 结论里的「加速」是定性的
  （图谱查询全在指标之前、子代理拿到精确资源名），**不是计时对比**
- 只跑了 1 次调查。2026-09-07 那次的 25% vs 100% 是 12 次的统计，
  这次的新 skill 还没有同等规模的样本

---

## 六、支线发现：新网关的 target 被建成了幻影工具

全量回归时 `test_75` 变红。不是之前那次的 botocore 层丢失（层完好），
而是 2026-10-10 有人新建了网关 `migrationdesk-pricing-gw`，它的 target
是一种 ETL 没见过的形态：

| | WaggleAI 的 5 个 target | 新网关的 `pricing-runtime` |
|---|---|---|
| `targetType` | `AGENTCORE_RUNTIME` | `MCP_SERVER` |
| 指向 runtime 的方式 | `targetConfiguration.http.agentcoreRuntime.arn` | `targetConfiguration.mcp.mcpServer.endpoint` 是一个 AgentCore Runtime 的 **invocations URL**（ARN URL 编码在路径里） |

旧解析器只认前一种，于是后一种掉进 AgentTool 分支、造出一个不存在的「工具」。
而它指向的 `AgentRuntime(migrationdesk_pricing)` 早已是图里的节点。

修复：`_runtime_arn_from_target` 识别 MCP_SERVER 型 —— **只认 AgentCore 的
invocations URL**，外部 MCP server 仍是真的 tool 端点（`test_58_13` 守着这个边界）。
部署后 ETL 写出 `RoutesToRuntime: 6`（原 5 + 新 1），幻影已清理，
`test_75_02` 的类型白名单从 `{AGENTCORE_RUNTIME}` 扩为两种已核实的 runtime 型。

这件事与「DevOps Agent 加速 RCA」直接相关：DevOps Agent 问「migrationdesk 的
pricing 网关依赖什么」时，修复前图谱会答「一个叫 pricing-runtime 的工具」——
一个不存在的东西。

## 七、对你原问题的回答

**直接调用 MCP 还是封装 Skill？** 两个都要，分工不同：

- **MCP 是能力**：已注册、已关联、24 个工具全部可用。不需要额外封装 ——
  DevOps Agent 原生支持 OAuth 的远程 MCP，AgentCore Runtime 就是标准形态。
- **Skill 是规程**：决定「先查图谱」这件事会不会发生，以及查的时候避开工具上的
  陷阱（方向、kind）。官方说 description 是 agent 决定是否加载 skill 的唯一依据，
  所以 description 必须写触发场景，而不是写「这是什么」。

**参考样例的定位不同**：`eks-resilience-checker` 是「执行一套检查并出报告」的
自包含 skill，靠 kubectl / aws CLI 自己取数；我们的 skill 是「指导如何使用
已接入的 MCP」—— 它本身不取数，只告诉 agent 用哪个工具、按什么顺序、
怎么解读结果。所以没有照搬它的结构，按官方 Skill 规范写。

## 八、建议的后续

1. **跑一组对照**：同一症状各跑 5 次「有 skill / 无 skill」，记录首个图谱调用的
   位置、是否传 kind、结论里是否出现方向错误 —— 把「加速」变成数字
2. **用「从 GitHub 导入」替代 zip 上传**：官方支持从 GitHub 目录导入并一键 Sync，
   skill 就能随仓库版本走，不会再出现「仓库改了线上没跟」—— 正是这次 MCP 漂移
   的同类问题。需要 Space 关联 GitHub 账号（私有仓需有读权限）
3. **把 test_137 加进 CI 的定时任务**：它需要 AWS 凭证，当前只在本地跑
