# 功能映射表（样张）— 重要业务服务的技术依赖全集

**生成日期**：2026-09-08　**数据源**：petsite-neptune 活图谱　**环境**：ap-northeast-1

## 这份东西对应哪条监管要求

它**不是** DORA 的信息登记册（Art. 28）。那份是**合同数据集** —— ITS (EU) 2024/2956
规定的 15 张互联模板、六个逻辑层、以 xBRL-CSV 年度报送，字段是合同编号、通知期、
适用法律、20 位 LEI、退出策略。本平台没有合同实体，不该也做不了那个。

本表对应的是**穿透式依赖映射**那一类要求，它们没有强制模板，但明确要求「映射」而非「列清单」：

| 出处 | 条文要求 | 本表覆盖 |
|---|---|---|
| BCBS 运营韧性原则（2021）原则四 | 映射交付关键运营所必需的内外部互联互赖，含依赖第三方的部分 | ✅ 主体 |
| DORA (EU) 2022/2554 Art. 8 | 记录资产 roles and dependencies、map 配置与 interdependencies | ✅ 主体 |
| 英国 PRA/FCA SYSC 15A / SS1/21 | 识别 important business services，并 identify and document 交付每项服务所需的 **technology** | ✅ 仅 technology 维度 |
| 中国 等保2.0 / 关基条例 | 资产识别与网络拓扑梳理；CIIO 识别关键资产及其相互依赖 | ✅ 主体 |
| DORA Art. 29 / 31 | 分包链集中度、据 interdependence 指定关键第三方 | ⚠️ 仅能提供**技术**集中度输入 |

**它也能当 DORA 登记册「功能层」的输入** —— 那一层要回答「每项 ICT 服务支撑哪些业务
功能」，本质是一次多跳图遍历，用清单做不出来。而监管点名的失败模式里就有
「关键性分类过于保守」，而关键性恰恰取决于这条服务支撑了哪些功能。

## 判据说明（审计会问的第一组问题）

**范围**：从每个 `BusinessCapability` 出发，沿 `Implements` 入边找到实现它的
微服务，再取这些服务的全部**依赖边出边**。依赖边 = 契约声明 `dependency: true`
的 10 种（`AccessesData` / `Calls` / `Delegates` / `DependsOn` / `Invokes` /
`InvokesTool` / `PublishesTo` / `Retrieves` / `RoutesToRuntime` / `RoutesVia`），
单一来源是 `graph_contract.dependency_edge_labels()`。

**刻意排除承载/放置关系**（`LocatedIn` 999 条、`RunsOn` 792 条、`BelongsTo` 469 条、
`Manages` 474 条、`Routes` 451 条）。理由不是它们不重要 —— Region 挂了什么都挂 ——
而是它们**普遍为真**，对几乎每个资源都成立，因而不携带判别信息。把它们算进依赖，
Region 会成为所有东西的咽喉点，淹没真正可操作的发现。
**代价必须说明**：EKS 集群、负载均衡器因此不出现在本表中，而它们在 DORA 视角下
确实是关键 ICT 服务。正式报告需把「基础设施承载层」作为**独立一栏**列出。

**列的含义**：

| 列 | 含义 | 审计价值 |
|---|---|---|
| `边类型` | 依赖的性质（调用 / 读写数据 / 发布 / 委派） | 区分故障传导方式 |
| `scope` | `observed`=被观测业务系统；`external`=AWS 托管服务；`scaffolding`=部署脚手架 | **哪些是第三方**，直接对应 DORA 的第三方范围 |
| `kind` | `static`=配置声明；`dynamic`=运行时观测 | **声明与观测分离**，这是本平台核心不变量 |
| `verify` | `confirmed`=故障注入确证；`inconclusive`=注入了但退化不足以判定；`untested`=未验证 | **证据等级**，见下 |
| `conf` | 置信度，`round(sigmoid(log-odds))`，零证据是 0.5 而非 0.0 | 可量化 |
| `鲜度` | 距上次观测的小时数 | 这条依赖现在还成立吗 |

---

## PetAdoptionFlow（Tier0）— 27 条依赖

| 服务 | 边类型 | 被依赖对象 | 对象类型 | scope | kind | verify | conf | 鲜度 |
|---|---|---|---|---|---|---|---|---|
| payforadoption | AccessesData | dynamodb | AWSServiceEndpoint | external | dynamic | **confirmed** | 1.000 | 73h |
| payforadoption | AccessesData | ssm | AWSServiceEndpoint | external | dynamic | untested | – | 62h |
| payforadoption | AccessesData | ServicesEks2-ddbpetadoption7B7CF | DynamoDBTable | observed | static | untested | – | 64h |
| payforadoption | DependsOn | cdk-hnb659fds-container-assets-9 | ECRRepository | scaffolding | dynamic | inconclusive | 0.500 | 无 |
| payforadoption | AccessesData | serviceseks2-databaseb269d8bb-ef | RDSCluster | observed | static | **confirmed** | 0.998 | 75h |
| payforadoption | AccessesData | serviceseks2-databasewriter2462c | RDSInstance | observed | dynamic | untested | – | 75h |
| payforadoption | AccessesData | serviceseks2-s3bucketpetadoption | S3Bucket | observed | dynamic | untested | – | 78h |
| payforadoption | AccessesData | StepFnStateMachine76D362E8-3jkn8 | StepFunction | observed | dynamic | untested | – | 78h |
| petsite | AccessesData | sns | AWSServiceEndpoint | external | dynamic | untested | – | 53h |
| petsite | AccessesData | ssm | AWSServiceEndpoint | external | dynamic | **confirmed** | 0.989 | 0h |
| petsite | AccessesData | stepfunctions | AWSServiceEndpoint | external | dynamic | untested | – | 53h |
| petsite | AccessesData | sts | AWSServiceEndpoint | external | dynamic | untested | – | 0h |
| petsite | DependsOn | **WaggleAIOrchestrator** | AgentRuntime | observed | static | untested | – | 0h |
| petsite | AccessesData | ServicesEks2-ddbpetadoption7B7CF | DynamoDBTable | observed | dynamic | untested | – | 16h |
| petsite | DependsOn | cdk-hnb659fds-container-assets-9 | ECRRepository | scaffolding | dynamic | inconclusive | 0.500 | 无 |
| petsite | DependsOn | petadoptions/petsite | ECRRepository | observed | dynamic | untested | – | 无 |
| petsite | Calls | payforadoption | Microservice | observed | dynamic | inconclusive | 0.996 | 53h |
| petsite | Calls | petfood | Microservice | observed | dynamic | **confirmed** | 0.993 | 49h |
| petsite | Calls | pethistory | Microservice | observed | dynamic | **confirmed** | 1.000 | 64h |
| petsite | Calls | petlistadoptions | Microservice | observed | dynamic | inconclusive | 0.996 | 64h |
| petsite | Calls | petsearch | Microservice | observed | dynamic | **confirmed** | 1.000 | 0h |
| petsite | AccessesData | serviceseks2-databaseb269d8bb-ef | RDSCluster | observed | dynamic | **confirmed** | 0.982 | 16h |
| petsite | AccessesData | serviceseks2-s3bucketpetadoption | S3Bucket | observed | dynamic | untested | – | 16h |
| petsite | PublishesTo | ServicesEks2-topicpetadoption192 | SNSTopic | observed | – | untested | – | 54h |
| petsite | PublishesTo | ServicesEks2-sqspetadoption2E8B1 | SQSQueue | observed | – | untested | – | 无 |
| petsite | DependsOn | ServicesEks2-sqspetadoption2E8B1 | SQSQueue | observed | dynamic | untested | – | 54h |
| petsite | AccessesData | StepFnStateMachine76D362E8-3jkn8 | StepFunction | observed | dynamic | untested | – | 54h |

## PetInventoryManagement（Tier1）— 11 条依赖

| 服务 | 边类型 | 被依赖对象 | 对象类型 | scope | kind | verify | conf | 鲜度 |
|---|---|---|---|---|---|---|---|---|
| petlistadoptions | DependsOn | cdk-hnb659fds-container-assets-9 | ECRRepository | scaffolding | dynamic | inconclusive | 0.500 | 无 |
| petlistadoptions | Calls | petsearch | Microservice | observed | dynamic | inconclusive | 0.996 | 50h |
| petlistadoptions | AccessesData | serviceseks2-databaseb269d8bb-ef | RDSCluster | observed | static | **confirmed** | 0.998 | 73h |
| petlistadoptions | AccessesData | serviceseks2-databasewriter2462c | RDSInstance | observed | dynamic | untested | – | 73h |
| petsearch | AccessesData | dynamodb | AWSServiceEndpoint | external | dynamic | **confirmed** | 1.000 | 0h |
| petsearch | AccessesData | s3 | AWSServiceEndpoint | external | dynamic | inconclusive | 0.731 | 0h |
| petsearch | AccessesData | ssm | AWSServiceEndpoint | external | dynamic | untested | – | 49h |
| petsearch | AccessesData | sts | AWSServiceEndpoint | external | dynamic | untested | – | 0h |
| petsearch | AccessesData | ServicesEks2-ddbpetadoption7B7CF | DynamoDBTable | observed | static | untested | – | 0h |
| petsearch | DependsOn | cdk-hnb659fds-container-assets-9 | ECRRepository | scaffolding | dynamic | inconclusive | 0.500 | 无 |
| petsearch | AccessesData | serviceseks2-s3bucketpetadoption | S3Bucket | observed | static | untested | – | 0h |

## AdoptionHistoryView（Tier1）— 3 条依赖

| 服务 | 边类型 | 被依赖对象 | 对象类型 | scope | kind | verify | conf | 鲜度 |
|---|---|---|---|---|---|---|---|---|
| pethistory | DependsOn | pet-adoptions-history | ECRRepository | observed | dynamic | untested | – | 无 |
| pethistory | AccessesData | serviceseks2-databaseb269d8bb-ef | RDSCluster | observed | static | inconclusive | 0.881 | 0h |
| pethistory | AccessesData | serviceseks2-databasewriter2462c | RDSInstance | observed | dynamic | untested | – | 0h |

---

## 技术集中度（DORA Art. 29 / 31 的输入）

多个业务功能共同依赖的对象 —— 这是「关键第三方」判定的技术依据：

| 被依赖对象 | 类型 | 支撑的业务功能数 | 说明 |
|---|---|---|---|
| `serviceseks2-databaseb269d8bb` | RDSCluster | **3 / 3** | 单一 Aurora 集群承载全部三项业务功能 |
| `serviceseks2-databasewriter2462c` | RDSInstance | **3 / 3** | 同一集群的 writer 实例 |
| `ServicesEks2-ddbpetadoption7B7CF` | DynamoDBTable | 2 / 3 | |
| `cdk-hnb659fds-container-assets` | ECRRepository | 2 / 3 | 镜像仓库，构建期依赖 |
| `serviceseks2-s3bucketpetadoption` | S3Bucket | 2 / 3 | |
| `dynamodb` / `ssm` / `sts` | AWSServiceEndpoint | 2 / 3 | AWS 托管服务端点 |
| `petsearch` | Microservice | 2 / 3 | 内部服务，被 petsite 与 petlistadoptions 共同依赖 |

**咽喉点结论**：`serviceseks2-databaseb269d8bb` 是三项业务功能的共同单点。
2026-09-07 该集群补了 reader 实例（`PromotionTier=1`，AZ `ap-northeast-1c`）后
具备 failover 能力，但**该能力尚未经故障注入验证** —— 见下方「不可采信的历史结论」。

## 这份样张自身的局限（必须随报告一同披露）

**证据覆盖率不足。** 41 条依赖里 `confirmed` 只有 **9 条（22%）**，
`inconclusive` **9 条**，其余 **23 条 `untested`**。一份 22% 确证率的映射放到审计面前
会被问穿。`inconclusive` 的含义是「注入了故障但观测到的退化不足以判定」，
不是「不成立」—— 本平台刻意区分二者，因为零流量与健康在指标上无法区分。

**两条 `PublishesTo` 边的 `kind` 为空。** 该边类型 2026-09-08 才从
`dependency: false` 翻为 `true`（理由：`etl_xray` 把 `SQSQueue` 映射成
`DependsOn`（算依赖）、`SNSTopic` 映射成 `PublishesTo`（不算），同一种语义关系
两种待遇，无正当理由）。`upsert_edge` 是契约驱动的，下一轮 ETL 会自动补上
`dependency_kind`，但**当前这份样张里它是空的**。

**SQSQueue 被双重表示。** `petsite -PublishesTo-> SQS` 与
`petsite -DependsOn-> SQS` 是同一个物理关系的两条边（分别来自 aws-etl 与 xray）。
任何「依赖计数」指标会重复计算。这是边类型学的遗留问题，不是数据错误。

**`scaffolding` 的 ECR 依赖是构建期而非运行时。** 4 条指向
`cdk-hnb659fds-container-assets` 的边 `phase=startup` —— 镜像仓库不可用时
**运行中的 Pod 不受影响**，但无法扩容或重启。`injectability.py` 判它为
`NEEDS_COMPOUND`（需「切断 + 触发重启」的复合实验），而 runner 尚未实现时序编排
（`aws:fis:wait` 已注册但后端无 `startAfter` 支持）。所以这 4 条的
`inconclusive` / `conf=0.500` 是**结构性未验证**，不是验证失败。

**鲜度分布很宽（0h ~ 78h）。** `AccessesData` 的契约 TTL 是 6 小时，
所以 73–78h 那批已远超 TTL。它们仍在表中是因为 TTL 收敛只作用于
`dependency_kind='dynamic'` 且不豁免的边，而多数超期项是 `static`（配置声明），
声明式依赖不因未观测而失效 —— 那是 `drift_status=declared_not_observed`
要表达的信息，不是失效。

**缺失的维度。** SS1/21 要求的 people / processes / information 三个维度
本图谱完全没有（39 种节点类型里无任何责任人、团队、流程、数据分类实体）；
impact tolerance 亦无 —— 那是机构董事会设定的承诺值，不该由工具估算。

## 不可采信的历史结论（审计追溯时会碰到）

`chaos/code/experiments/generated/` 下两个 `fis_rds_failover` 实验
（`payforadoption-h002`、`petlistadoptions-h004`）的 `cluster_arn` 原为
`grafana-aurora-mysql` —— Grafana 自己的数据库，与这两个业务服务无关。
根因是 `business_config.json` 用 `depends_on_types: ["RDSCluster"]` **按类型展开**，
而账号内有 3 个 RDS 集群。两次实验于 2026-04-02 执行、记录为
`passed / degradation_rate=0.0`，**「passed」很可能只是因为切了不相干的库**。
FIS 侧实验历史只保留到 2026-05-14，4 月那次的实际靶标已无法证实。

已于 2026-09-06 更正 ARN 并在文件头留下说明；这两条结论标为不可采信、需重跑。

## 复现方式

```bash
export NEPTUNE_ENDPOINT=<cluster-endpoint>
export REGION=ap-northeast-1
export PYTHONPATH=infra/lambda/shared/python
# 依赖边清单的单一来源
python3.11 -c "from graph_contract import dependency_edge_labels; print(sorted(dependency_edge_labels()))"
```

遍历判据：`BusinessCapability ← Implements ← Microservice → <依赖边> → *`。
边类型清单不得硬编码 —— 仓库内曾有四份分歧实现（契约 / RCA 兜底 /
`schema_prompt.py` / `dr-plan-generator`），其中一次漂移导致线上漏掉 16 条
`Invokes` 边而本地正确。`tests/test_52::m04` 与 `tests/test_35::g18` 是为此立的门禁。
