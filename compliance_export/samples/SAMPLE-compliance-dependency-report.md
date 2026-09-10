# 依赖关系映射记录 —— 管理层自评估

## 文档控制

| 项 | 值 |
|---|---|
| 报告编号 | `DEP-MAP-20260910T062500Z`（由快照时刻确定，全局唯一） |
| 报告名称 | 依赖关系映射记录 —— 管理层自评估 |
| 基准时点（as-at） | `2026-09-10T06:25:00Z` |
| 版本纪律 | 每次导出为独立版本，文件名带快照时刻，不覆盖历史（SYSC 15A.6.2R 要求保存 each version 满 6 年） |
| 密级 | 〈待指定〉 |
| 编制 | `compliance_export` 自动生成，无人工编辑 |
| 审核 | 〈待签署〉 |
| 批准 | 〈待签署〉—— SYSC 15A.7.1R 要求 governing body 批准并定期审查 |
| 分发范围 | 〈待指定〉 |

## 1 报告性质与适用范围

**1.1 本报告是管理层自行编制的书面记录**，用于满足 FCA SYSC 15A.6.1R 对依赖关系 mapping 的记录要求，以及 DORA Art. 8(4)、BCBS 运营韧性原则四、《关基条例》第九条一类的穿透式依赖映射要求。

**1.2 本报告不是鉴证报告，未按 ISAE 3000 (Revised) 执行。** 报告由本组织自有工具从可观测性数据自动生成，未经独立执业者鉴证，不含 ISAE 3000 §69(h)(i)(j) 所要求的「依准则执行」、「适用 ISQC 1 质量控制」与「遵守 IESBA Code 独立性要求」声明。任何将本报告视为独立鉴证结论的引用都是误用。

**1.3 本报告不是 DORA Art. 28 信息登记册（Register of Information）。** 那份登记册由 ITS (EU) 2024/2956 规定 16 张互联模板、以 xBRL-CSV 报送，其主连接键是 `contractual arrangement reference number`（合同编号），字段为通知期、适用法律、20 位 LEI、退出计划。本组织的图谱不含合同数据，**结构上无法产出该登记册**；强行套用只会得到一份强制字段大面积为空的失败报送件。本报告借用其 B_06.01 的取值词汇（见 §4），仅此而已。

**1.4 本报告的结论仅在 §2 所载基准时点成立**，不构成对该时点之后系统状态的陈述。

## 2 报告基准与数据来源

| 项 | 值 |
|---|---|
| 快照时刻（基准日） | `2026-09-10T06:25:00Z` |
| 数据源 | `petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com` |
| 业务功能数 | 3 |
| 一跳依赖边数 | 44 |
| 依赖边类型（10 种） | `AccessesData / Calls / Delegates / DependsOn / Invokes / InvokesTool / PublishesTo / Retrieves / RoutesToRuntime / RoutesVia` |

**2.1 依赖边清单来源**：`graph_contract.dependency_edge_labels()`（契约，单一来源）。本仓库曾因四处各抄一份清单而产生分歧，其中两处漂移到实际错误，故清单只有一个出处。

**2.2 跨表可交叉校验**：本报告全部表格取自同一次会话、同一快照时刻。活图谱正被 ETL 持续改写，不同时刻导出的数字会不同，这是正常的；但同一份报告内部若出现矛盾，则是缺陷。

**2.3 完整字段见随附 CSV**。本 Markdown 呈现的是审阅所需的列；机器可读的完整字段集（含 `confidence`、`source`、`last_seen`、`verify_experiment`）在同名 CSV 中，两者取自同一快照。

## 3 适用标准（Applicable Criteria）

ISAE 3000 §69(d) 要求识别适用标准。下表把条文依据集中成交叉引用，而不是散在各节的括号里 —— 散落的引用无法被逐条核对。

**表 1：适用标准与本报告对应节**

| 条文依据 | 该条要求什么 | 本报告对应节 |
|---|---|---|
| DORA (EU) 2022/2554 Art. 8(4) | map the links and interdependencies | §5 依赖关系映射 |
| DORA (EU) 2022/2554 Art. 8(5) | 识别与第三方的 interconnections | §6 第三方范围分列 |
| FCA SYSC 15A.4.1R | identify and document people/processes/technology/facilities/information | §5、§10 |
| FCA SYSC 15A.5.3R | carry out scenario testing 的测试证据 | §5 取证方法与结论 |
| FCA SYSC 15A.2.5R | must set an impact tolerance for each IBS | §9（本报告披露为未设定） |
| FCA SYSC 15A.2.7G(10) | common operational resources 的聚合影响 | §7 技术集中度 |
| FCA SYSC 15A.6.1R(3) | mapping 方法与如何支撑测试的书面记录 | 附录 A |
| FCA SYSC 15A.6.1R(7) | vulnerabilities 及整改行动与时限理由 | §10 |
| FCA SYSC 15A.6.2R | retain each version …… at least 6 years | 文档控制（文件名带快照时刻） |
| FCA SYSC 15A.7.1R | governing body 批准并定期审查该书面记录 | 文档控制（待签署） |
| BCBS Principles for Operational Resilience 原则四 | mapping interconnections and interdependencies | §5 |
| 《关键信息基础设施安全保护条例》第九条 | 识别关键业务及其依赖的网络设施与信息系统 | §5 |
| ITS (EU) 2024/2956 B_06.01 | functions identification 的字段与枚举词汇（借用词汇，非报送） | §4、§9 |
| NIST OSCAL observation.method | TEST / EXAMINE / INTERVIEW 取证方法受控词汇 | §4、§5 |
| ISAE 3000 (Revised) §69(e) | 重大固有局限性的描述 | §10 |

## 4 术语与取值定义

正式文档必须定义自己用的每个取值。本节的取值词汇刻意对齐外部标准（ITS (EU) 2024/2956 的枚举措辞、NIST OSCAL 的取证方法、SOC 2 的结论措辞），而不使用内部实现词。

**表 2：术语与取值定义**

| 术语 / 取值 | 定义 |
|---|---|
| 依赖对象标识符 | AWS 物理资源 ID、Kubernetes 服务名、AWS 服务端点名或 agent 工具键，按对象类型而定。本平台**不生成**另一套展示名 —— 图谱里没有的名称不会被编造出来，标识符即该对象在其所属系统中的真实身份键。 |
| 依赖类型 | 契约定义的依赖边类型，来源为 `graph_contract.dependency_edge_labels()`。承载/放置类关系不在此列，单列于 §8。 |
| 取证方法 TEST | 主动故障注入实测。对应 NIST OSCAL `observation.method=TEST`，也对应 SYSC 15A.5.3R 的 scenario testing。证据强度最高。 |
| 取证方法 EXAMINE | 审阅运行时遥测与配置（来源见随附 CSV 的 `source` 列），未做主动注入。对应 OSCAL `observation.method=EXAMINE`。可证明「观测到过」，不能证明「切断后业务确实受损」。 |
| Confirmed — no exceptions noted | 已做故障注入，且观测到预期的业务侧退化。该依赖成立且被实测确证。 |
| Inconclusive | 已做故障注入，但观测退化不足以判定。**刻意不与「未测试」合并** —— 零流量与健康在指标上无法区分，不可据此判「依赖不成立」。 |
| Not tested — assessment not performed | 未做故障注入。措辞与取值对齐 ITS (EU) 2024/2956 B_06.01.0050 枚举码 3 (Assessment not performed)：法定模版把「未评估」作为显式取值，不是空值。 |
| 未评估（Assessment not performed） | 该字段在图谱中无值。对 `verify_status` 表示未做场景测试；对 `dependency_kind` 表示 ETL 尚未打标（如边类型分类新近变更）。**不折叠为 0 或空档** —— 「我们知道自己不知道」是审计要看的诚实度。 |
| 未设定（Not defined） | 该阈值尚未设定。ITS B_06.01.0080/0090 对未定义的 RTO/RPO 规定填 `0`，本报告在人类可读产出中写作「未设定」以免与真实的 0 秒混淆；机器可读产出（CSV）保留空值。 |
| 范围 observed / external / scaffolding | observed = 本组织自有并被观测到；external = 组织外部服务端点（DORA Art. 8(5) 的 interconnections）；scaffolding = 构建/部署期基础设施，非运行时业务依赖。 |
| drift 漂移状态 | declared_not_observed = 配置声明存在但当轮未观测到；observed_then_silent = 曾观测到、当轮静默。两者都不等于「依赖已消失」。 |

## 5 依赖关系映射（按业务功能）

对应 DORA Art. 8(4)、SYSC 15A.4.1R 的 technology 维度。列设计对齐 SOC 2 Section IV「tests of controls and results of tests」的惯例：先声明取证方法，再给结论，例外单列。

### 5.1 AdoptionHistoryView

重要性分级 `Tier1`；一跳直接依赖 **3** 条；多跳可达 **3** 个对象。

**表 3：AdoptionHistoryView 的一跳直接依赖**

| # | 依赖方 | 依赖类型 | 依赖对象标识符 | 对象类型 | 范围 | 取证方法 | 结论 | 例外与备注 |
|---|---|---|---|---|---|---|---|---|
| 1 | pethistory | AccessesData | `serviceseks2-databaseb269d8bb-efjeyzicx2ak` | RDSCluster | observed | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `exp-serviceseks2-databaseb269d8bb-efjeyzicx2ak-fis-rds-reboot-20260831-110323` |
| 2 | pethistory | AccessesData | `serviceseks2-databasewriter2462cc03-fwgfu4gossqe` | RDSInstance | observed | EXAMINE | Not tested — assessment not performed |  |
| 3 | pethistory | DependsOn | `pet-adoptions-history` | ECRRepository | observed | EXAMINE | Not tested — assessment not performed |  |

### 5.2 PetAdoptionFlow

重要性分级 `Tier0`；一跳直接依赖 **27** 条；多跳可达 **38** 个对象。

**表 4：PetAdoptionFlow 的一跳直接依赖**

| # | 依赖方 | 依赖类型 | 依赖对象标识符 | 对象类型 | 范围 | 取证方法 | 结论 | 例外与备注 |
|---|---|---|---|---|---|---|---|---|
| 1 | payforadoption | AccessesData | `ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM` | DynamoDBTable | observed | EXAMINE | Not tested — assessment not performed | 漂移 `declared_not_observed` |
| 2 | payforadoption | AccessesData | `StepFnStateMachine76D362E8-3jkn8j2OUpdQ` | StepFunction | observed | EXAMINE | Not tested — assessment not performed | 漂移 `observed_then_silent` |
| 3 | payforadoption | AccessesData | `dynamodb` | AWSServiceEndpoint | external | TEST | Confirmed — no exceptions noted | 实验 `exp-dynamodb-fis-network-disrupt-20260831-160742` |
| 4 | payforadoption | AccessesData | `serviceseks2-databaseb269d8bb-efjeyzicx2ak` | RDSCluster | observed | TEST | Confirmed — no exceptions noted | 漂移 `declared_not_observed`；实验 `exp-serviceseks2-databaseb269d8bb-efjeyzicx2ak-fis-rds-reboot-20260831-110323` |
| 5 | payforadoption | AccessesData | `serviceseks2-databasewriter2462cc03-fwgfu4gossqe` | RDSInstance | observed | EXAMINE | Not tested — assessment not performed |  |
| 6 | payforadoption | AccessesData | `serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb` | S3Bucket | observed | EXAMINE | Not tested — assessment not performed | 漂移 `observed_then_silent` |
| 7 | payforadoption | AccessesData | `ssm` | AWSServiceEndpoint | external | EXAMINE | Not tested — assessment not performed |  |
| 8 | payforadoption | DependsOn | `cdk-hnb659fds-container-assets-926093770964-ap-northeast-1` | ECRRepository | scaffolding | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `image-repo-dependency: cutting ECR affects only new Pod image pulls, not already-running Pods; no runtime degradation observable` |
| 9 | petsite | AccessesData | `ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM` | DynamoDBTable | observed | EXAMINE | Not tested — assessment not performed | 漂移 `declared_not_observed` |
| 10 | petsite | AccessesData | `StepFnStateMachine76D362E8-3jkn8j2OUpdQ` | StepFunction | observed | EXAMINE | Not tested — assessment not performed | 漂移 `declared_not_observed` |
| 11 | petsite | AccessesData | `serviceseks2-databaseb269d8bb-efjeyzicx2ak` | RDSCluster | observed | TEST | Confirmed — no exceptions noted | 漂移 `declared_not_observed`；实验 `exp-serviceseks2-databaseb269d8bb-efjeyzicx2ak-fis-rds-reboot-20260831-110323` |
| 12 | petsite | AccessesData | `serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb` | S3Bucket | observed | EXAMINE | Not tested — assessment not performed | 漂移 `declared_not_observed` |
| 13 | petsite | AccessesData | `sns` | AWSServiceEndpoint | external | EXAMINE | Not tested — assessment not performed |  |
| 14 | petsite | AccessesData | `ssm` | AWSServiceEndpoint | external | TEST | Confirmed — no exceptions noted | 实验 `exp-petsite-network-partition-20260831-165936` |
| 15 | petsite | AccessesData | `stepfunctions` | AWSServiceEndpoint | external | EXAMINE | Not tested — assessment not performed |  |
| 16 | petsite | AccessesData | `sts` | AWSServiceEndpoint | external | EXAMINE | Not tested — assessment not performed |  |
| 17 | petsite | Calls | `payforadoption` | Microservice | observed | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `exp-pay-for-adoption-http-chaos-20260905-053059` |
| 18 | petsite | Calls | `petfood` | Microservice | observed | TEST | Confirmed — no exceptions noted | 实验 `networkchaos-partition:petsite->petfood` |
| 19 | petsite | Calls | `pethistory` | Microservice | observed | TEST | Confirmed — no exceptions noted | 实验 `exp-pethistory-http-chaos-20260905-064354` |
| 20 | petsite | Calls | `petlistadoptions` | Microservice | observed | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `exp-list-adoptions-http-chaos-20260905-064919` |
| 21 | petsite | Calls | `petsearch` | Microservice | observed | TEST | Confirmed — no exceptions noted | 实验 `exp-search-service-http-chaos-20260905-073516` |
| 22 | petsite | DependsOn | `ServicesEks2-sqspetadoption2E8B1217-4T0KP1GHgwoj` | SQSQueue | observed | EXAMINE | Not tested — assessment not performed |  |
| 23 | petsite | DependsOn | `WaggleAIOrchestrator` | AgentRuntime | observed | EXAMINE | Not tested — assessment not performed |  |
| 24 | petsite | DependsOn | `cdk-hnb659fds-container-assets-926093770964-ap-northeast-1` | ECRRepository | scaffolding | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `image-repo-dependency: cutting ECR affects only new Pod image pulls, not already-running Pods; no runtime degradation observable` |
| 25 | petsite | DependsOn | `petadoptions/petsite` | ECRRepository | observed | EXAMINE | Not tested — assessment not performed |  |
| 26 | petsite | PublishesTo | `ServicesEks2-sqspetadoption2E8B1217-4T0KP1GHgwoj` | SQSQueue | observed | EXAMINE | Not tested — assessment not performed | 漂移 `declared_not_observed` |
| 27 | petsite | PublishesTo | `ServicesEks2-topicpetadoption192CAB8F-Dn0iiU45RZ1g` | SNSTopic | observed | EXAMINE | Not tested — assessment not performed | 漂移 `declared_not_observed` |

### 5.3 PetInventoryManagement

重要性分级 `Tier1`；一跳直接依赖 **14** 条；多跳可达 **11** 个对象。

**表 5：PetInventoryManagement 的一跳直接依赖**

| # | 依赖方 | 依赖类型 | 依赖对象标识符 | 对象类型 | 范围 | 取证方法 | 结论 | 例外与备注 |
|---|---|---|---|---|---|---|---|---|
| 1 | ServicesEks2-statusupdaterservicelambdafn37242E00-0SHsIkrhwJ32 | AccessesData | `ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM` | DynamoDBTable | observed | EXAMINE | Not tested — assessment not performed |  |
| 2 | ServicesEks2-statusupdaterservicelambdafn37242E00-0SHsIkrhwJ32 | AccessesData | `dynamodb` | AWSServiceEndpoint | external | EXAMINE | Not tested — assessment not performed |  |
| 3 | petlistadoptions | AccessesData | `serviceseks2-databaseb269d8bb-efjeyzicx2ak` | RDSCluster | observed | TEST | Confirmed — no exceptions noted | 实验 `exp-serviceseks2-databaseb269d8bb-efjeyzicx2ak-fis-rds-reboot-20260831-110323` |
| 4 | petlistadoptions | AccessesData | `serviceseks2-databasereader1f54479b8-hpyukqlufzus` | RDSInstance | 未评估（Assessment not performed） | EXAMINE | Not tested — assessment not performed |  |
| 5 | petlistadoptions | AccessesData | `serviceseks2-databasewriter2462cc03-fwgfu4gossqe` | RDSInstance | observed | EXAMINE | Not tested — assessment not performed |  |
| 6 | petlistadoptions | Calls | `petsearch` | Microservice | observed | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `exp-search-service-http-chaos-20260905-073516` |
| 7 | petlistadoptions | DependsOn | `cdk-hnb659fds-container-assets-926093770964-ap-northeast-1` | ECRRepository | scaffolding | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `image-repo-dependency: cutting ECR affects only new Pod image pulls, not already-running Pods; no runtime degradation observable` |
| 8 | petsearch | AccessesData | `ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM` | DynamoDBTable | observed | EXAMINE | Not tested — assessment not performed |  |
| 9 | petsearch | AccessesData | `dynamodb` | AWSServiceEndpoint | external | TEST | Confirmed — no exceptions noted | 实验 `exp-dynamodb-fis-network-disrupt-20260831-160742` |
| 10 | petsearch | AccessesData | `s3` | AWSServiceEndpoint | external | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `exp-s3-fis-network-disrupt-20260831-160219` |
| 11 | petsearch | AccessesData | `serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb` | S3Bucket | observed | EXAMINE | Not tested — assessment not performed |  |
| 12 | petsearch | AccessesData | `ssm` | AWSServiceEndpoint | external | EXAMINE | Not tested — assessment not performed |  |
| 13 | petsearch | AccessesData | `sts` | AWSServiceEndpoint | external | EXAMINE | Not tested — assessment not performed |  |
| 14 | petsearch | DependsOn | `cdk-hnb659fds-container-assets-926093770964-ap-northeast-1` | ECRRepository | scaffolding | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `image-repo-dependency: cutting ECR affects only new Pod image pulls, not already-running Pods; no runtime degradation observable` |


## 6 证据状态分列统计

三个维度回答三个不同问题，**刻意不合并成单一比率**：平均会让「已声明但从未观测」与「已观测但从未验证」互相抵消，而那是监管审查最容易挑的点。

### 6.1 证据等级

凭什么说这条依赖成立（SYSC 15A.5.3R 的测试证据）。

**表 6：证据等级**

| 取值 | 条数 | 占比 |
|---|---|---|
| 未评估（Assessment not performed） | 26 | 59% |
| confirmed | 9 | 20% |
| inconclusive | 9 | 20% |

### 6.2 声明 vs 观测

配置声明的，还是运行时观测到的。

**表 7：声明 vs 观测**

| 取值 | 条数 | 占比 |
|---|---|---|
| dynamic | 34 | 77% |
| static | 8 | 18% |
| 未评估（Assessment not performed） | 2 | 5% |

### 6.3 第三方范围

哪些是第三方（DORA Art. 8(5) 的 interconnections）。

**表 8：第三方范围**

| 取值 | 条数 | 占比 |
|---|---|---|
| observed | 28 | 64% |
| external | 11 | 25% |
| scaffolding | 4 | 9% |
| 未评估（Assessment not performed） | 1 | 2% |


## 7 技术集中度

对应 SYSC 15A.2.7G(10) 的原文要求：评估 *the potential aggregate impact of disruptions to multiple important business services, in particular where such services rely on common operational resources as identified by the firm's mapping exercise*。

**表 9：被多个业务功能共同依赖的对象**

| 被依赖对象标识符 | 对象类型 | 范围 | 支撑业务功能数 | 边数 |
|---|---|---|---|---|
| `serviceseks2-databaseb269d8bb-efjeyzicx2ak` | RDSCluster | observed | 3 / 3 | 4 |
| `serviceseks2-databasewriter2462cc03-fwgfu4gossqe` | RDSInstance | observed | 3 / 3 | 3 |
| `ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM` | DynamoDBTable | observed | 2 / 3 | 4 |
| `cdk-hnb659fds-container-assets-926093770964-ap-northeast-1` | ECRRepository | scaffolding | 2 / 3 | 4 |
| `dynamodb` | AWSServiceEndpoint | external | 2 / 3 | 3 |
| `serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb` | S3Bucket | observed | 2 / 3 | 3 |
| `ssm` | AWSServiceEndpoint | external | 2 / 3 | 3 |
| `petsearch` | Microservice | observed | 2 / 3 | 2 |
| `sts` | AWSServiceEndpoint | external | 2 / 3 | 2 |

## 8 基础设施承载层（单列，非服务消费关系）

本节所列边**不是**依赖。它们普遍为真、不携带判别信息，故不进依赖表；但 EKS 集群与负载均衡器在 DORA 视角下确实是关键 ICT 服务，故在此列出，以免读者误以为被遗漏。范围排除的理由见 §10.5。

**表 10：承载/放置类关系**

| 边类型 | 条数 | 性质 |
|---|---|---|
| LocatedIn | 1113 | 承载/放置关系，非服务消费关系 |
| RunsOn | 1020 | 承载/放置关系，非服务消费关系 |
| BelongsTo | 583 | 承载/放置关系，非服务消费关系 |
| Manages | 588 | 承载/放置关系，非服务消费关系 |
| Routes | 565 | 承载/放置关系，非服务消费关系 |

## 9 业务功能与容忍度阈值

阈值取值词汇对齐 ITS (EU) 2024/2956 B_06.01.0080/0090 的口径（见 §4）。

**表 11：业务功能的重要性分级与容忍度阈值**

| 业务功能 | 重要性分级 | impact_tolerance_seconds | rto_target_seconds |
|---|---|---|---|
| AdoptionHistoryView | Tier1 | 未设定（Not defined） | 未设定（Not defined） |
| PetAdoptionFlow | Tier0 | 未设定（Not defined） | 未设定（Not defined） |
| PetInventoryManagement | Tier1 | 未设定（Not defined） | 未设定（Not defined） |

**9.1 两个阈值必须分列。** RTO 是恢复某个流程的目标时间（内部视角）；impact tolerance 是重要业务服务的最大可容忍中断（外部危害视角）。两者可以差数倍，混用是监管审查重点。

## 10 重大固有局限性与范围排除

本节对应 ISAE 3000 §69(e) 与 SYSC 15A.6.1R(7)。每一条的数字均由本次快照现算，不是手写的固定文本 —— 手写的披露会过期。

**10.1 证据覆盖率 20%。** 44 条依赖里经实测确证（TEST/Confirmed）仅 9 条，26 条未测试。整改方向为扩大故障注入覆盖，当前受限于注入手段对 agent 层与部分托管服务的可达性。

**10.2 2 条边的 `dependency_kind` 为未评估。** 这些是边类型分类新近变更、尚待下轮 ETL 补标的边。该取值表示「尚未打标」，不表示「不适用」。

**10.3 SYSC 15A.4.1R 的六要素只覆盖两项。** 该条原文要求 identify and document the people, processes, technology, facilities and information necessary to deliver each important business service：

**表 12：SYSC 15A.4.1R 六要素的覆盖状态**

| 要素 | 状态 | 依据 |
|---|---|---|
| technology | 覆盖 | Microservice / Pod / EC2 / Lambda / 各类托管服务 |
| facilities | 覆盖 | AvailabilityZone / Region / VPC / Subnet |
| people | 缺失 | 契约与活图谱均无责任人/团队实体 |
| processes | 缺失 | 同上，无流程实体 |
| information | 部分 | 数据基础设施有（数据访问类边），表/字段级血缘无 |

**10.4 impact tolerance 未设定（3/3 个业务功能）。** SYSC 15A.2.5R 要求 firm *must* set an impact tolerance for each important business service。**本报告不得出现任何「未越界」表述** —— 没有阈值就是没有阈值，不能用「没检测到越界」掩盖「压根没有阈值可比」。

**10.5 承载层不在依赖表内（范围排除）。** 承载/放置类关系普遍为真、不携带判别信息，算进依赖会让 Region 成为所有东西的咽喉点。代价是 EKS 集群与负载均衡器不出现在依赖表中，而它们在 DORA 视角下确实是关键 ICT 服务 —— 故单列于 §8 并注明性质。

**10.6 集中度是技术集中度，不是供应商集中度（范围排除）。** DORA Art. 29/31 要的是供应商层面的集中度与分包链，需要法人实体节点才能回答；本平台当前只有技术对象。第四方分包链在埋点边界外，结构上做不到。

**10.7 未采用 ITS B_05.02 的 `Rank` 分层（范围排除）。** 法定模版用 `Rank`（直接第三方=1，分包商逐级>1）表达供应链层级。本报告的多跳可达数是技术依赖深度，与 Rank 的法人分包语义不同，**刻意不混用该字段名**。

**10.8 无历史版本查询能力。** SYSC 15A.6.2R 要求保存 each version 满 6 年。本报告通过「文件名带快照时刻、不覆盖历史」满足留存，但图谱本身无双时态，无法回答「三个月前这条依赖是什么状态」。

## 附录 A 编制方法与所执行工作摘要

对应 ISAE 3000 §69(k)（所执行工作的信息性摘要）与 SYSC 15A.6.1R(3)(9)（mapping 方法与所用方法论的书面记录）。

**A.1 数据获取。** 对 `petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com` 执行只读 openCypher 查询。导出层不含任何写操作，由 `tests/test_54_compliance_export.py::m07` 静态扫描锁定 —— 生成合规报告的过程若会改动被报告的对象，报告本身就不可采信。

**A.2 依赖边的判定口径。** 依赖边类型清单取自契约函数，共 10 种；承载/放置类关系不计入，且经门禁校验两集合零重叠。

**A.3 一跳与多跳分开呈现。** 报告主体用一跳直接依赖 —— 每条边有明确的源、目标、观测来源与证据等级，是可归责的单位。多跳可达数作为补充统计，回答「这项业务功能一共牵连多少资产」，两种口径刻意不合并。

**A.4 取证方法的判定。** `TEST` 表示已执行故障注入实验；`EXAMINE` 表示仅审阅运行时遥测与配置。词汇取自 NIST OSCAL `observation.method`，其强度序列 TEST > EXAMINE > INTERVIEW 源自 NIST SP 800-53A。

**A.5 快照一致性。** 基准时刻在快照构造时取一次，全部表格共用。由 `m05` 以 AST 扫描锁定「`take_snapshot` 内取当前时刻恰好一次」—— 分次取时刻会让同一份报告的不同表落在不同基准日上。

**A.6 本报告的自动化程度。** 全部数字与披露文本由代码从快照现算，无人工编辑环节。因此本报告不含人工判断，也不含对数字合理性的复核 —— 该复核是 SYSC 15A.7.1R 所要求的 governing body 审批环节的内容。
