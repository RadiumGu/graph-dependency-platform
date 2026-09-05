# 图数据库做依赖关系管理：企业场景与价值的业界调研

> 调研时间 2026-09-05。12 条并行调研线，覆盖可观测性/APM、云厂商原生、开源平台、
> 安全攻击路径、数据血缘、CMDB/ITSM、韧性与混沌工程、FinOps、监管合规、
> 图数据库技术本身、AI/Agent 方向，以及一条专门找反面证据的线。
>
> **证据分层约定**（全文遵守）：
> `[监管原文]` 法条与条款号 · `[官方文档]` 厂商产品文档 · `[独立研究]` 第三方或学术
> · `[厂商宣称]` 营销口径 · `[溯源失败]` 广泛传播但找不到原始出处 · `[观点]` 具名判断

---

## 0. 一句话总账

依赖图的价值密度取决于两个**正交**维度：**这个业务问题本质上是不是「路径问题」**，
以及**图里的数据是否可信**。前者决定价值上限（是路径问题，图才不可替代），
后者决定价值能否兑现（数据不可信，建模再精细也无用）。

业界的现状是：**第一个维度已经被充分证明，第二个维度几乎全线失守**——依赖图的
准确性被广泛吐槽，却极少被系统地量化验证。这正是当前最大的空白。

|  | **数据可信** | **数据不可信** |
|---|---|---|
| **是路径问题** | 供应链可达性、攻击路径、合规映射 → **价值最硬** | 变更爆炸半径、故障根因 → **价值被数据质量卡住** |
| **不是路径问题** | 资产清单、成本打标 → 关系库就够，图是过度设计 | —— |

---

## 1. 企业场景地图（按价值证据强度排序，而非按技术分类）

### 第一梯队：有硬证据或法定强制

#### 1.1 监管合规的依赖映射 —— 唯一「不做不合法」的场景

这是本次调研中最硬的买单驱动。四大辖区的法条**直接以条文文字命令映射依赖**，
且要求穿透到多跳与第三方分包链，本质上无法用清单满足。

| 辖区 | 法规 | 条文要求 | 状态 |
|---|---|---|---|
| 欧盟 | **DORA** (EU) 2022/2554 | Art. 8(1) 记录业务功能与资产的「roles and **dependencies**」；Art. 8(4) 「**map** the configuration... and the **links and interdependencies** between the different information assets and ICT assets」；Art. 8(5) 识别与第三方的 interconnections；Art. 28(3) 维护 **register of information** 覆盖全部 ICT 第三方合同并年报监管；Art. 29 评估**分包链**集中度风险；Art. 31 ESAs 据 interdependence 指定 CTPP | 2025-01-17 起适用 |
| 英国 | PRA/FCA **SYSC 15A** / SS1/21 | 识别 important business services、设 impact tolerance、并「identify and document the **people, processes, technology, facilities and information** necessary to deliver each」 | 过渡期 2025-03-31 已截止 |
| 欧盟 | **NIS2** (EU) 2022/2555 | Art. 21(2)(d) supply chain security，涵盖「relationships between each entity and its **direct suppliers or service providers**」；Art. 20 管理层个人责任 | 已生效 |
| 美国 | OCC 2023-17 / **FFIEC Appendix J** | 第三方全生命周期风险管理，点名 concentration risk 与 subcontractors；Appendix J 要求识别**第四方**分包链 | 2023-06-06 生效 |
| 国际 | **BCBS** Operational Resilience Principles (2021) | 原则四即「**Mapping interconnections and interdependencies**」：映射交付关键运营所必需的内外部互联互赖，含依赖第三方的部分 | 2021 |

`[监管原文]` 罚则是真的：DORA Art. 35(6)-(8) 对关键第三方提供商可按**全球日均营业额
1%/天**处以定期罚款；Art. 42(6) 主管当局可作为最后手段**要求金融实体终止**使用该提供商。
英国已有执法先例——**TSB 因运营韧性与外包风险管理失败被 FCA+PRA 联合罚款 £48,650,000**
（2022-12），其前 CIO 另于 2023-04 被 PRA 个人罚款。

`[官方文档]` BCBS 监管通讯（newsletter 34）指出：**映射互联互赖与设定容忍度是各行采纳
该原则时最普遍的难点**——即监管要求已明确，能力普遍不达标。这是市场空间。

- https://eur-lex.europa.eu/eli/reg/2022/2554/oj
- https://www.fca.org.uk/firms/operational-resilience
- https://www.bankofengland.co.uk/news/2022/december/tsb-fined-for-operational-resilience-failings
- https://www.bis.org/fsi/fsisummaries/op_resilience.htm

#### 1.2 供应链安全的可达性分析 —— 量化收益最扎实

SBOM 只回答「存在哪些依赖」；叠加调用图做**可达性分析**才回答「我的代码是否真的
调用了那个含漏洞的函数」。这是本次调研里量化证据最好的场景，因为它有**偏独立**的数据：

| 来源 | 数字 | 层级 |
|---|---|---|
| Semgrep 对 1,100 个开源项目 | 1,614 条 Dependabot 告警中**仅 31 条（约 2%）可达** | `[独立研究]` |
| JFrog 2025 供应链报告 | 检查可达性后 **98% 的 critical 告警被降级** | `[独立研究]` |
| Endor Labs | 函数级可达性降噪最多 **95%**、findings 减少 92%；某 Java 应用 Snyk 报 500+ CVE，Endor 剩 <50 | `[厂商宣称]` |
| Savant（arXiv:2506.17798） | 语义制导可达性 83.8% precision / 73.8% recall | `[独立研究]` |

**关键的反证也要一并引用**：Semgrep 自己批评**传递依赖**的可达性可行动性低——静态
分析假设每个分支都可能执行，call graph 常把仅在特定参数配置下才触发的漏洞误标为
「可达」。而且各家「可达」的定义并不统一（call graph vs AST/语义）。
`[独立研究]` 这个「既有收益又有反证」的组合比任何单方宣称都可信。

- https://semgrep.dev/blog/2024/overrated-and-underperforming-transitive-reachability-analysis
- https://www.endorlabs.com/learn/reachability-analysis

#### 1.3 攻击路径分析 —— 「非图不可」论证最清晰

攻击本质是**关系的遍历**：攻击者串联一系列孤立看无害的条件（暴露入口 + 过度权限身份
+ 可达漏洞 + 敏感数据）。传统 CSPM 把每条发现独立评分（公开 S3 桶无论是否可达都告警），
图则把这些「有毒组合 toxic combinations」串成真正可利用的路径。

| 层面 | 代表 | 节点/边 | 回答什么问题 |
|---|---|---|---|
| 云 | Wiz Security Graph、MS Defender for Cloud、Orca、Prisma Cloud | 云资源 / IAM 角色策略 / 密钥 / 网络；边为「可被公网访问」「承载此身份」「可读此桶」「含此 CVE」 | 从公网暴露的工作负载，经过度授权身份，能否到达最敏感数据库 |
| 身份 | **BloodHound** | AD/Entra 对象；边为 `MemberOf`/`AdminTo`/`CanRST`/ADCS，**有向且方向即攻击方向** | 给定普通用户，到 Domain Admin 的**最短路径** |
| 容器 | **KubeHound**（Datadog 开源）、IBM Bloodhound-Kube | Pod/ServiceAccount/RoleBinding/Node/Secret；边为 RBAC 授权、挂载、`can-exec`、容器逃逸、联邦进云 IAM | K8s 攻击的可预测三段式：Pod 失陷→逃逸到节点→经 pod identity 进云 |

`[观点]` 云侧与身份侧的降噪倍数**多为厂商宣称，尚缺独立基准**——Wiz 未给出可独立
验证的公开数字。共同软肋：API 快照滞后于运行态、补偿控制（网络策略/WAF）未必被建模、
「可达 ≠ 可利用」带来残余误报、大环境路径爆炸导致「路径疲劳」。

- https://bloodhound.specterops.io/opengraph/developer/graph-theory
- https://learn.microsoft.com/en-us/azure/defender-for-cloud/concept-attack-path

### 第二梯队：价值明确但量化弱，且普遍被数据质量卡住

#### 2.1 变更风险评估与爆炸半径（CMDB/ITSM）

机制是清楚的：**CSDM** 提供 business capability → application service → 基础设施的数据
模型骨架，**Service Mapping**（top-down discovery）负责画准，**IRE** 作为多源写入的
去重与权威源裁定闸门。创建变更请求的瞬间平台即可自动填充 affected CIs、自动识别其它
受影响服务、自动推导应审批人——**CAB 自动化完全寄生于关系数据的准确度**。

`[官方文档]` ServiceNow 自己的技术博文标题就是「变更被评为低风险，却拖垏了三个服务」，
复盘结论是变更「在基础设施层被正确定级，却对服务级 blast radius 完全失明」。

**但这个场景的价值被数据质量卡住了**，见第 4 节。ITIL 4 用 change enablement 提供流程、
用 service configuration management 提供数据，但**框架假定数据是准的，不负责保证准确度**。

#### 2.2 数据血缘的影响分析与隐私合规

- **影响分析**：改一列前先看下游哪些模型/看板会崩
- **故障溯源**：Monte Carlo 的 Troubleshooting Agent 自动遍历血缘定位根因，并区分
  SELECT 直接关系与 WHERE/JOIN 间接关系
- **「这份 PII 流到哪了」**：`[官方文档]` OpenLineage 明确把 GDPR/HIPAA/CCPA/BCBS/PCI
  列为**列级**血缘的核心驱动力，`transformationType` 字段专为追踪敏感个人信息使用而设。
  **这是列级（而非表级）血缘不可替代的场景。**

`[官方文档]` **但列级血缘的覆盖率被系统性高估**。DataHub 自己的「Not supported」清单
最坦诚，可直接引用：标量 UDF/表值函数只能指到输入列、`json_extract`/`UNNEST`/Struct
子字段不可靠、`MERGE INTO` 不生成列级血缘、**`WHERE/GROUP BY/JOIN/HAVING` 中引用的列
不算入血缘**、动态 SQL 无法静态分析、仅支持 sqlglot 覆盖的 20+ 方言且 schema 过期会
导致血缘错误。Apache Atlas 的列级血缘长期是弱项（API 根本不返回列级信息）。

`[观点]` **未见任何独立第三方对「列级血缘」本身的量化收益研究**——治理平台的
「199% ROI / $8.8M NPV」类数字来自厂商赞助的 Forrester TEI，应视作营销。

- https://docs.datahub.com/docs/lineage/sql_parsing
- https://openlineage.io/blog/column-lineage/

### 第三梯队：价值真实但有前提条件

#### 3.1 韧性工程与灾备

`[官方文档]` **Google SRE 的依赖分级**是这个领域最有用的概念工具：
**hard dependency**（其宕机=你也宕机）、**soft dependency**（设计得当则无影响）、
以及介于两者之间的 **degraded**（如缓存失效只降级延迟）。依赖可以是直接 RPC、
间接传递、或**结构隐式**（zone/region、DNS、服务发现）。

`[独立研究]` **真正用依赖图决定「注入哪里」的成熟案例只有 Netflix**：
**LDFI（Lineage-Driven Fault Injection，SIGMOD 2015）**从一次**成功请求**的血缘出发，
用 SAT 求解器反向计算「哪些故障组合可能推翻这个成功结果」，把指数级故障空间收敛到
少数关键实验。**ChAP** 建立在 FIT 之上，用请求路径元数据对**单个依赖**注入并做自动
金丝雀对照，专门验证「非关键依赖失败不会导致整体宕机」。

**而商业/开源工具都不内建从依赖拓扑自动选靶**：AWS FIS、Gremlin、Chaos Mesh、
LitmusChaos 的实验模板全部由人指定目标资源。Gremlin 提供依赖可视化辅助人决策，
靶点选择仍是人工。

可引用的量化：
- `[独立研究]` arXiv:2506.11176 —— 从 trace 自动抽取活体依赖图做蒙特卡洛故障仿真，
  与真实混沌实验对比：DeathStarBench 上无副本时实测韧性 0.186 vs 图预测 0.161，
  有副本时两者均收敛至 0.305，**平均绝对误差 ≤ 0.0004**
- `[独立研究]` ErrorPrism（arXiv:2509.26463）—— 字节跳动 **67 个生产微服务**上重建
  102 个真实错误传播路径，**准确率 97.0%**，优于静态分析与 LLM 基线

#### 3.2 FinOps 共享成本分摊与容量规划

依赖图在这里的作用是把**直接标签覆盖不到的共享/idle 成本**从中央黑洞变成沿消费链
可传播、可 chargeback 的量。

`[官方文档]` **OpenCost 规范**把集群总成本拆为
`Workload Costs + Cluster Idle Costs + Cluster Overhead Costs`，工作负载成本按
`max(request, usage)` 在 container 级计算再向上聚合；**Idle Cost 被显式建模为「已配置
但未分配给任何工作负载的资源成本」**——谁「预留」了容量、谁真正「用」了，决定 idle 归谁。
`[官方文档]` **AWS Split Cost Allocation Data** 对 ECS/EKS 做容器级归因，CPU:内存权重
按 Fargate 价格取 **9:1**；2025-10 起支持 Kubernetes labels。
`[官方文档]` **FinOps Foundation** 的 Allocation Capability 定义共享成本可用固定分摊、
按比例、或**代理指标（proxy metrics）**决定可变比例；Run 级成熟度要求「用计量工具捕获
使用量以更高精度归因共享成本」——即把依赖/使用数据作为分摊输入。

可引用的量化（多为业界共识量级，非独立测量）：
- `[厂商宣称]` 企业默认 **20–40%** 成本 untagged、无法归属 owner；
  「30% untagged → 团队成本视图最多 70% 真实」
- `[厂商宣称]` **28–50%** 云支出可避免，**32–40%** 预算浪费在 idle/oversized 资源

现实障碍：标签策略「存在但无强制」（tag key 是自由文本、部署管线不阻止零标签资源上线）；
部分成本天然不可分割（税、支持费、跨服务控制面）——官方承认「完全一致性难以达到」。

### 新兴：证据分场景，或尚未成型

#### 4.1 GraphRAG —— 已落地但「普遍优于 RAG」是营销叙事

`[独立研究]` arXiv:2502.11371（*RAG vs. GraphRAG: A Systematic Evaluation*，2025-02，
2026-03 更新至 v3）在统一预处理/检索/生成协议下对比，结论是**分场景各有胜负**：
vanilla RAG 擅长答案落在具体段落的**局部**问题，GraphRAG 擅长跨全库的**主题/全局**问题，
最佳做法是二者**融合**而非替换。arXiv:2506.05690 更直接指出「GraphRAG 在许多真实任务上
频繁劣于 vanilla RAG」。复现分析显示 ROUGE-2 上 GraphRAG 输给普通 RAG
（SQuALITY 6.99 vs 10.08，QMSum 3.23 vs 6.32）。

`[官方文档]` **成本代价微软自己承认**：LazyGraphRAG 博客称新方案把索引成本降到
「与向量 RAG 相同、仅为全量 GraphRAG 的 **0.1%**」，全局查询成本可降 **700 倍**——
这等于官方承认原版 GraphRAG 的索引/查询开销是主要痛点。

`[厂商宣称]` V7 Labs 的 1000 文档基准称图谱在最难问题上 91.7% vs 向量 36.3%——但作者
自己承认「本体与语料同步构建、每题都可由图谱回答」，属自证。

#### 4.2 AIOps 把拓扑作为 LLM 推理约束 —— 最活跃的研究方向

核心动机：纯 LLM 的 RCA **拓扑无关**，会犯「症状放大偏差」（把根因错误归到显眼的
下游受害者）。2025–2026 多项工作用图拓扑约束 LLM 推理：TopoEvo（arXiv:2605.15611）、
LLM-Guided Graph Structure Learning（MDPI Computers 15/7/412，用 LLM 补全静态拓扑
缺失的传播路径）、UModel 重建使根因定位精度**提升 8%**（arXiv:2606.04799）、
Kubernetes 的 Auditable Graph-Guided RCA（arXiv:2606.08590，用类型化证据图 +
确定性图操作约束搜索并校验结论）、DBAIOps（arXiv:2508.01136）。

`[观点]` 方向共识明确、有量化提升，但**仍以研究原型和挑战赛为主，未见成熟商用独立评测**。

#### 4.3 Agent / MCP 依赖治理 —— 最真实的空白

`[官方文档]` **trace 层已成熟**：LangSmith、Langfuse、Arize AX/Phoenix、Comet Opik
都已把每次 model call / tool call / agent step 作为结构化 trace 捕获。关键分野是
agent-native 工具能捕获步骤间**因果依赖**，LLM-first 工具只记独立事件需手动关联。

**但 MCP 依赖治理明显不成熟**：MCP 把 agent 连到工具/DB/API，每个连接都是新的延迟、
成本、权限漂移、prompt injection、静默失败点。已出现的尝试：微软 Agent Governance
Toolkit（在 MCP tool call 上强制策略与信任决策）、DataRobot 主张把 MCP 连接纳入
「agentic 控制平面」（每个 server/tool/permission/agent 关系都要有 owner、scope、
运行时监控与审计）、Oracle 把工具治理下沉进数据库复用 ACL/VPD/审计。
`[观点]` 有人警示「**MCP 依赖坟场**」——2025 流行的 server 被弃维后信任边界悄然失效。
**尚无公认领导者，接近半空白。**

#### 4.4 用 LLM 自动构建/维护依赖图 —— 准确率两极分化

- 结构化/schema 血缘：LLM 表级血缘解析 **>95%**（MDPI Electronics 14/9/1762）；
  RDF 生成 few-shot 下 Llama F1 99.35%
- 开放文本抽取明显更难：关系抽取 REBEL 仅 **65.8% macro-F1**；多智能体 KG 增强
  KARMA 仅 83.1% 自验正确率；RAKG 在 MINE 达 95.91%

`[观点]` 结构化数据血缘已可用；开放域自动建图 65–85%，仍需人审，
**「全自动维护依赖图谱」被高估**。

---

## 2. 实现路线的横向对比：谁真的用了图数据库

一个反直觉的发现：**绝大多数「依赖图」产品并不用图数据库**。

| 类别 | 产品 | 图怎么建 | 底层存储 |
|---|---|---|---|
| APM | **Dynatrace Smartscape on Grail** | OneAgent 自动发现 + trace 推断 | **真图存储**：明确 nodes+edges 建模，DQL `traverse` 多跳遍历 |
| APM | New Relic | 配置驱动（YAML 规则的 entity/relationship synthesis） | NRDB 事件，关系类型闭集，默认 75 分钟过期，E&R 数据仅保留 **24 小时** |
| APM | Datadog Service Map | trace 自动发现（可选 inferred entities） | 无专用图库，30 天无 trace 自动老化 |
| 开源 | Grafana Tempo Service Graph | 纯 trace 推断（client/server span 配对） | **Prometheus 时序指标的 label**，图在可视化层重建 |
| 开源 | Kiali | Istio 遥测推断 | 查询时动态生成 JSON，无持久化 |
| 开源 | **Cartography**（Lyft→CNCF） | 拉取云资产 API，30+ provider | **真图库：Neo4j + Cypher** |
| 开源 | Backstage | 混合：人写 `catalog-info.yaml` + entity providers 自动发现 | **PostgreSQL/SQLite**，图靠查询遍历 |
| K8s | owner reference 图 | 自动（Deployment→ReplicaSet→Pod） | etcd 对象元数据 + GC 控制器内存图 |

`[观点]` 开源侧**真图数据库只有 Cartography**；商业 APM 侧只有 Dynatrace 明确宣称
图存储 + 图遍历。这说明：**很多依赖关系场景用指标 label 或关系库就够了**，
图数据库不是入场券。

**云厂商原生能力的分层**（`[官方文档]`）：
- **调用级依赖发现**（真正自动推断）：仅 **AWS Resilience Hub**（机制是分析
  **Route 53 DNS resolver 查询日志**，识别计算资源解析的所有域名，回溯 35 天，
  无需 agent）与 **Azure Application Insights**（分布式追踪）；两者都需前置启用/插桩
- **资源级关系图**：**AWS Config** 有专门的 Example Relationship Queries + 跨账户跨区
  聚合导出 API（但**不支持 JOIN/UNION/HAVING**，仅能查已被 recorder 记录的资源）；
  **GCP Cloud Asset Inventory** 有 `relatedAsset`（注意批量 `relatedAssets` 复数字段
  已 deprecated、服务端不再返回）
- **声明式分组（非发现）**：AppRegistry、SSM Application Manager（官方已发availability
  变更公告、能力收敛中）、CloudFormation `DependsOn`/`Ref`
- **已退役**：**Azure Service Map 已于 2025-09-30 退役**；VM Insights Map 与 Dependency
  Agent 弃用、2028-06-30 退役，自 2025-09-30 起门户不能再新增 VM 上线

---

## 3. 技术选型的诚实结论

### 3.1 图数据库 vs 递归 SQL：分界线在「深度是否可变」

`[观点]` 诚实结论：**浅 join、固定深度、以聚合/宽表分析为主时，关系库（含递归 CTE）
完全够用**。图库真正拉开差距的是**变深、多跳、路径/模式匹配**（可达性、传递闭包、
环检测）——递归自连接在此指数退化。

**对依赖管理的具体判据**：只查「直接依赖/被依赖」（1–2 跳），Postgres 递归 CTE 就够；
一旦要「传递闭包 + 深度可变 + 环检测」，图引擎（或 SQL/PGQ）价值才显现。

### 3.2 新品类：graph-on-relational

`[观点]` gdotv 的观察值得注意：「过去 18 个月三家超大规模云、两家老牌数据库厂商和
多家初创都发了 graph-on-relational 引擎」——正在形成挑战「独立图库」概念的新品类：
- **PuppyGraph** —— zero-ETL，直接在 Databricks/Iceberg/Delta/PostgreSQL/MongoDB 上
  定义图模型并用 openCypher/Gremlin 查询，无数据搬迁
- **DuckPGQ** —— DuckDB 扩展，实现 SQL:2023 的 **SQL/PGQ**
- **Apache AGE** —— PostgreSQL 扩展

对「依赖数据本就在数据湖仓」的团队，这条路省掉 ETL 与第二套系统运维。

### 3.3 GQL 标准落地程度：发布了，但实现仍在早期

`[官方文档]` **ISO/IEC 39075:2024 GQL** 于 2024-04 发布——自 1987 年 SQL 以来第一个
全新 ISO 数据库语言标准，2026 年已有勘误 Cor 1。

但**真实实现仍在早期**：Neo4j 的 Cypher「支持大多数强制 GQL 特性」，但仍有强制特性
未实现，且 APOC/GDS 等专有面被其**自家 conformance 附录**标注为偏离标准。真正声明
GQL 一致性的新实现来自 **Google Spanner Graph**（GQL+SQL 双标准）与
**Microsoft Fabric Graph**（对照 39075 最小一致性逐项映射）。
**openCypher** 是事实上的迁移路径；**Gremlin** 更像存量兼容层。

### 3.4 市场规模：绝对值不可信，增速可信

`[观点]` 各分析师的绝对值相差近 **10 倍**（MarketsandMarkets 2024 年 $0.51B ↔
The Business Research Co 2025 年 $4B），说明「图数据库」的边界定义不一（是否含知识
图谱/GraphRAG/多模），**绝对值不可尽信**。但 **CAGR 高度一致（24–30%）**，
且增长驱动力方向一致。最稳健的定性结论是 Gartner 的
「**图库是 DBMS 中增长最快的类别**，五年 CAGR 26.4%」。

---

## 4. 为什么这些系统在企业里失败 —— 六类失效机制

这一节的价值在于诚实。**依赖图的准确性在业界被广泛吐槽，却极少被系统地量化验证。**

### 4.1 治理失效，不是工具失效

`[独立研究]` 唯一能定位到具体 Gartner 文档并逐字核对的数字：**CMDB 实施成功率仅 29%**
（*Save Your Failing CMDB With a Service Data Model*，文档号 7612565）。

`[溯源失败]` **而流传最广的「Gartner 说 75% 的 CMDB 项目失败」找不到原始出处**：
直读 Infoblox 原文，它写「According to Gartner, 75 percent of CMDB initiatives fail
to meet expectations due to poor data quality」，**零引用**（无文档号、标题或链接）。
这很可能是对「29% 成功」的近似化二次传播。同样溯源失败的还有「平均准确率约 60%」
「40–70%」「只有 25% 企业获得有意义价值」等——全是供应商内容营销口径。
**EMA/Forrester 的一手可追溯数字找不到。**

`[观点]` **Forrester 首席分析师 Charles Betz 2025-10 的 “‘CMDB’ Is Dead”** 是最有
杀伤力的具名观点：客户「往往在三四次尝试之后」仍失败，根因不是工具而是「没有任何真正
懂数据管理的人参与」；并给出一线观察——**排障时 sysadmin 从不查 CMDB 缓存值，
而是直接 SSH 进设备看实时状态**。他承认过去反复失败却仍在建，只是「因为别无选择」。

各方（含 Gartner 一手观点）高度收敛于**治理与数据鲜度**：Gartner 直言「CMDB 常被尝试
却鲜有成效；多数团队没建立让数据保持相关的治理；项目一结束就不再是优先级」。
ServiceNow 自己的博文标题是「手工维护 CMDB 是一份没人干的全职工作」。

### 4.2 自动发现的三种系统性错误

`[官方文档]` 有工单级证据：
- **假阳性**：SolarWinds NPM 因 CAM/网桥数据误导**算出错误的父子依赖**——看到一次
  二层可达就建边
- **粒度错配**：ServiceNow Service Mapping 中多个应用共用一个入口/负载均衡器，
  导致服务图相互**合并成一个**
- **假阴性**：「Service Mapping Incomplete」——发现了主服务却漏掉依赖 CI

`[官方文档]` 结构性缺陷（ServiceNow 官方博客）：自底向上自动发现「**没有自动更新数据
的路径，必须重新扫描才能保持准确**」——调度扫描与真实变更节奏不对齐，图天生滞后。

### 4.3 声明式目录「写完就腐烂」

Backstage 的设计前提是开发者把 `catalog-info.yaml` 与代码放一起手工维护。
`[观点]` 反面证据是间接但明确的：**企业不得不发 RFC 强制「组织内所有仓库根目录必须
包含 catalog-info.yaml」并设定推行时间表**；**Spotify Portal 干脆为没有该文件的仓库
自动开 PR 生成占位文件**。需要强制令和自动补文件，本身就说明自愿维护不成立。

`[独立研究]` 更具体的腐烂形态见 GitHub issue：删 User 后 Group 上残留 `hasMember`
（#13774）、unregister 后残留 stale relations（#20030）、错误状态实体删不掉（#15521）、
provider 不真正删除只标 orphan（#18054）。**孤儿删除语义是持续痛点。**

对比 Cartography 的痛点则完全不同：最大抱怨是每次必须**全量 sync、无近实时增量**
（Discussion #1199），以及 cleanup 删太多。

### 4.4 采样率让稀有依赖成为结构性盲点 —— 最干净的可复现结论

`[独立研究]` 这是本次调研里最锋利的一条：
- 1% 采样下 100 个请求里 99 个不留痕；**若某低频错误/依赖只占 0.5% 请求，
  它很可能根本不出现在采样数据里**
- **尾采样也救不了**：尾采样「只能评估你事先定义的判据，你没写过规则的未知失败模式
  照样被采样丢弃」
- arXiv:2107.07703 从统计角度证实部分采样的 trace 会难以还原真实分布

`[官方文档]` OpenTelemetry 官方「1% 采样能准确代表其余 99%」的说法**只对高频均质请求
成立**，对依赖发现的长尾恰恰失效。**所以「稀有依赖 = 盲点」不是调参能解决的。**

### 4.5 eBPF/服务网格只见连接性，推不出异步逻辑边

`[独立研究]` 机制清晰：eBPF 依赖发现论文从 **13,615 个网络事件里抽出 32 条边**，
本质是连接性/网络流量——它看到 `producer→broker`、`broker→consumer` 两段 TCP，
但**推不出 producer 和 consumer 之间的逻辑依赖**。Confluent/New Relic 都指出 Kafka 是
「隐式依赖」，无同步调用链可跟。`[观点]` 一篇 Netflix/Uber 复盘描述得最直白：
工作流「**在 Kafka topic 中间蒸发了，没有告警，因为没有单个组件失败**」。

加密流量方面：eBPF 能覆盖 HTTPS 只因为它在内核态**加密前/解密后截明文**；
若不做这种内核挂钩，mesh/sidecar 层看到的就是密文，建不出应用级依赖。

### 4.6 韧性机制主动掩盖真实依赖 —— 与验证方法论直接相关

`[官方文档]` **Google SRE 明确点出**：**重试、超时、缓存/陈旧数据、回退（fallback）
会扭曲依赖对上游的真实影响**——例如 0.1% 的依赖失败经重试后未必仍是 0.1%；
超时（10s）与依赖 SLO（30s）不匹配又会改变实际效果。熔断器与缓存会让一个真实的
**硬依赖在正常观测下「看不见」**，直到缓存过期或熔断打开才暴露级联失败。

`[独立研究]` 多篇 2025 论文（arXiv:2506.11176、2512.00844 等）指出弹性伸缩、
负载均衡与容错机制**遮蔽故障传播路径**，使静态依赖图与真实运行时依赖出现偏差。

**这是「必须用故障注入主动验证图、不能只靠 trace 静态推断」的学术依据。**

---

## 5. 最大的空白：依赖图的可证伪性几乎无人系统研究

`[独立研究]` 全网只找到**一篇**把依赖图当作待验证假说的可复现方法论：
**arXiv:2512.12314**（Krasnovsky, AINA 2026）——用 GitHub Actions 从 OpenTelemetry
trace 自动发现依赖图 → Monte Carlo 仿真预测可用性 → 再跑**混沌实验随机杀微服务**
做对照验证。

**而且它给出了一个值得诚实呈现的反直觉结果（null result）**：显式建模 Kafka 异步边
只让预测可用性变化约 **10⁻⁵**（0.001 个百分点）。这既证明了验证方法可行，
也提醒**图的「精细程度」不必然带来预测价值**——在该案例里连接性模型已足够。

`[观点]` 结论：**除这一个单案例外，几乎没有跨企业、标准化的「依赖图可证伪性」基准。**
业界的现状是——依赖图的准确性被广泛吐槽（第 4 节六类失效机制都有证据），
却极少被系统地量化验证。

这反过来定义了差异化的位置：市面上的依赖图产品回答「**我看到了什么**」，
几乎没有人回答「**我看到的是真的吗**」。而第 4.6 节表明后者不是学术洁癖——
重试、缓存、熔断会系统性地让真实硬依赖在观测中隐身，不主动打断就无法区分
「依赖不存在」与「依赖被韧性机制掩盖」。

---

## 6. 给决策者的取舍清单

**什么时候值得投入依赖图**
1. 落在监管强制范围内（DORA/SYSC 15A/NIS2/FFIEC）——这是合规成本，不是选择
2. 问题本质是路径问题：可达性、攻击路径、传递闭包、环检测、爆炸半径
3. 深度可变的多跳查询是常态（≥3 跳，或深度不确定）

**什么时候不值得**
1. 只查 1–2 跳的直接依赖 —— Postgres 递归 CTE 就够，图库是过度设计
2. 数据源本身不可信且没有治理投入 —— 29% 的成功率说明工具救不了治理
3. 追求「图建得更细」而非「图更可信」—— arXiv:2512.12314 的 null result 说明
   精细度的边际价值可能是 10⁻⁵

**投入的优先顺序（由证据推导）**
1. **先解决可信度，再解决覆盖面**。六类失效机制里五类是数据质量问题，只有一类
   （4.1）是治理问题。而所有下游价值（变更风险、根因、成本分摊）都寄生于准确度
2. **优先自动发现，但要知道它的三种系统性错误**（假阳/假阴/粒度错配），
   并为每种建立守门判据
3. **明确承认采样与异步的结构性盲区**，不要假装覆盖全面 —— 稀有依赖靠采样调参
   永远抓不到
4. **对关键依赖做主动验证**（故障注入），因为韧性机制会让硬依赖在观测中隐身

---

## 附：来源可信度总表

| 层级 | 本报告中的代表来源 |
|---|---|
| 监管原文（最高） | EUR-Lex DORA/NIS2 全文、FCA Handbook SYSC 15A、OCC Bulletin 2023-17、BIS/BCBS |
| 官方产品文档 | docs.aws.amazon.com、learn.microsoft.com、cloud.google.com、docs.dynatrace.com、backstage.io、opencost.io、finops.org、docs.datahub.com、openlineage.io |
| 独立/学术 | LDBC 审计基准、arXiv（2502.11371、2506.11176、2509.26463、2512.12314、2107.07703）、Semgrep 1,100 项目研究、Gartner 文档 7612565 |
| 具名观点 | Forrester Charles Betz「CMDB is dead」、gdotv 的 graph-on-relational 观察 |
| 厂商宣称（需打折） | Wiz/Endor Labs/Orca 的降噪倍数、治理平台 TEI ROI、V7 Labs GraphRAG 基准、各类云成本浪费百分比 |
| 溯源失败（不应引用） | 「Gartner 说 75% CMDB 项目失败」、「平均准确率 60%」、「只有 25% 企业获得价值」 |
