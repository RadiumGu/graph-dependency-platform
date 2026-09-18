# 依赖关系映射记录 —— 管理层自评估

## 文档控制

| 项 | 值 |
|---|---|
| 报告编号 | `DEP-MAP-20260917T080525Z`（由快照时刻确定，全局唯一） |
| 报告名称 | 依赖关系映射记录 —— 管理层自评估 |
| 基准时点（as-at） | `2026-09-17T08:05:25Z` |
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
| 快照时刻（基准日） | `2026-09-17T08:05:25Z` |
| 数据源 | `petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com` |
| 业务功能数 | 3 |
| 一跳依赖边数 | 50 |
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
| DORA (EU) 2022/2554 Art. 8(5) | 识别与第三方的 interconnections | §7 第三方范围分列 |
| FCA SYSC 15A.4.1R | identify and document people/processes/technology/facilities/information | §5、§11 |
| FCA SYSC 15A.5.3R | carry out scenario testing 的测试证据 | §5 取证方法与结论 |
| FCA SYSC 15A.2.5R | must set an impact tolerance for each IBS | §10（本报告披露为未设定） |
| FCA SYSC 15A.2.7G(10) | common operational resources 的聚合影响 | §8 技术集中度 |
| FCA SYSC 15A.6.1R(3) | mapping 方法与如何支撑测试的书面记录 | 附录 A |
| FCA SYSC 15A.6.1R(7) | vulnerabilities 及整改行动与时限理由 | §11 |
| FCA SYSC 15A.6.2R | retain each version …… at least 6 years | 文档控制（文件名带快照时刻） |
| FCA SYSC 15A.7.1R | governing body 批准并定期审查该书面记录 | 文档控制（待签署） |
| BCBS Principles for Operational Resilience 原则四 | mapping interconnections and interdependencies | §5 |
| 《关键信息基础设施安全保护条例》第九条 | 识别关键业务及其依赖的网络设施与信息系统 | §5 |
| ITS (EU) 2024/2956 B_06.01 | functions identification 的字段与枚举词汇（借用词汇，非报送） | §4、§10 |
| NIST OSCAL observation.method | TEST / EXAMINE / INTERVIEW 取证方法受控词汇 | §4、§5 |
| ISAE 3000 (Revised) §69(e) | 重大固有局限性的描述 | §11 |

## 4 术语与取值定义

正式文档必须定义自己用的每个取值。本节的取值词汇刻意对齐外部标准（ITS (EU) 2024/2956 的枚举措辞、NIST OSCAL 的取证方法、SOC 2 的结论措辞），而不使用内部实现词。

**表 2：术语与取值定义**

| 术语 / 取值 | 定义 |
|---|---|
| 切断手段（severance） | 得出 `Confirmed` 所使用的故障注入手段，决定该结论的**适用范围**。本轮出现两种：`iam-deny`（注入期间该依赖调用全程返回 AccessDenied）与 `rds-reboot`（数据库实例重启，实测中断仅约 16~18 秒）。**两者不等价** —— 后者不支撑「数据库长时间或彻底不可用」场景下的结论。逐手段的范围定义见 §6。未记录该字段的 `Confirmed` 视为范围不明。 |
| 证据通道（evidence channel） | 判定所依据的观测来源。`xray-edge+business-probe` 为消费方侧调用统计加业务功能探针的双通道；`rds-event+business-probe` 表示**消费方侧没有调用遥测**，注入生效性改由资源自身事件（RDS `DB instance shutdown`/`restarted`）证明。后者是一个已披露的观测缺口，不是等价替代。 |
| 依赖对象标识符 | AWS 物理资源 ID、Kubernetes 服务名、AWS 服务端点名或 agent 工具键，按对象类型而定。本平台**不生成**另一套展示名 —— 图谱里没有的名称不会被编造出来，标识符即该对象在其所属系统中的真实身份键。 |
| 依赖类型 | 契约定义的依赖边类型，来源为 `graph_contract.dependency_edge_labels()`。承载/放置类关系不在此列，单列于 §9。 |
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

重要性分级 `Tier1`；一跳直接依赖 **4** 条；多跳可达 **4** 个对象。

**表 3：AdoptionHistoryView 的一跳直接依赖**

| # | 依赖方 | 依赖类型 | 依赖对象标识符 | 对象类型 | 范围 | 取证方法 | 结论 | 例外与备注 |
|---|---|---|---|---|---|---|---|---|
| 1 | pethistory | AccessesData | `serviceseks2-databaseb269d8bb-efjeyzicx2ak` | RDSCluster | observed | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 漂移 `declared_not_observed`；实验 `exp-serviceseks2-databaseb269d8bb-efjeyzicx2ak-fis-rds-reboot-20260831-110323` |
| 2 | pethistory | AccessesData | `serviceseks2-databasereader1f54479b8-hpyukqlufzus` | RDSInstance | 未评估（Assessment not performed） | TEST | Confirmed（数据库实例重启，范围受限） | **范围限制**：实例重启造成的**瞬时**中断（实测约 16~18 秒），覆盖「短暂丢失」场景；**不覆盖**「数据库长时间或彻底不可用」，亦不覆盖延迟升高；【观测来源】⚠️ **消费方侧无调用遥测**；生效性由资源自身事件（RDS `DB instance shutdown`/`restarted`）证明，业务影响由探针证明；实验 `rds-fault-probe_20260915-052215` |
| 3 | pethistory | AccessesData | `serviceseks2-databasewriter2462cc03-fwgfu4gossqe` | RDSInstance | observed | EXAMINE | Not tested — assessment not performed |  |
| 4 | pethistory | DependsOn | `pet-adoptions-history` | ECRRepository | observed | EXAMINE | Not tested — assessment not performed |  |

### 5.2 PetAdoptionFlow

重要性分级 `Tier0`；一跳直接依赖 **32** 条；多跳可达 **47** 个对象。

**表 4：PetAdoptionFlow 的一跳直接依赖**

| # | 依赖方 | 依赖类型 | 依赖对象标识符 | 对象类型 | 范围 | 取证方法 | 结论 | 例外与备注 |
|---|---|---|---|---|---|---|---|---|
| 1 | payforadoption | AccessesData | `ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM` | DynamoDBTable | observed | EXAMINE | 引导路径依赖 — 调用真实存在，但只在引导/管理端点上，无用户可见功能依赖，切断不产生业务退化 | 漂移 `declared_not_observed`；实验 `source-audit-20260915` |
| 2 | payforadoption | AccessesData | `StepFnStateMachine76D362E8-3jkn8j2OUpdQ` | StepFunction | observed | EXAMINE | 建模产物 — 源码/IaC 审计证明该调用不存在 | 漂移 `declared_not_observed`；实验 `source-audit-20260915` |
| 3 | payforadoption | AccessesData | `dynamodb` | AWSServiceEndpoint | external | EXAMINE | 引导路径依赖 — 调用真实存在，但只在引导/管理端点上，无用户可见功能依赖，切断不产生业务退化 | 实验 `source-audit-20260915` |
| 4 | payforadoption | AccessesData | `serviceseks2-databaseb269d8bb-efjeyzicx2ak` | RDSCluster | observed | TEST | Confirmed（未声明，范围受限） | **范围限制**：⚠️ 边上未记录切断手段 —— 无法判断该结论的适用范围；⚠️ 未记录证据通道；实验 `exp-serviceseks2-databaseb269d8bb-efjeyzicx2ak-fis-rds-reboot-20260831-110323` |
| 5 | payforadoption | AccessesData | `serviceseks2-databasereader1f54479b8-hpyukqlufzus` | RDSInstance | 未评估（Assessment not performed） | TEST | Confirmed（数据库实例重启，范围受限） | **范围限制**：实例重启造成的**瞬时**中断（实测约 16~18 秒），覆盖「短暂丢失」场景；**不覆盖**「数据库长时间或彻底不可用」，亦不覆盖延迟升高；实验 `rds-fault-probe_20260914-022850` |
| 6 | payforadoption | AccessesData | `serviceseks2-databasewriter2462cc03-fwgfu4gossqe` | RDSInstance | observed | EXAMINE | Not tested — assessment not performed |  |
| 7 | payforadoption | AccessesData | `serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb` | S3Bucket | observed | EXAMINE | Not tested — assessment not performed | 漂移 `declared_not_observed` |
| 8 | payforadoption | AccessesData | `sqs` | AWSServiceEndpoint | external | EXAMINE | Not tested — assessment not performed |  |
| 9 | payforadoption | AccessesData | `ssm` | AWSServiceEndpoint | external | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `iam-deny-probe_20260913-220634` |
| 10 | payforadoption | Calls | `petsearch` | Microservice | observed | TEST | Confirmed（服务选择器黑洞）— no exceptions noted | 实验 `svc-blackhole-probe_20260915-101103` |
| 11 | payforadoption | Calls | `petstatusupdater` | Microservice | observed | EXAMINE | Not tested — assessment not performed |  |
| 12 | payforadoption | DependsOn | `cdk-hnb659fds-container-assets-926093770964-ap-northeast-1` | ECRRepository | scaffolding | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `image-repo-dependency: cutting ECR affects only new Pod image pulls, not already-running Pods; no runtime degradation observable` |
| 13 | petsite | AccessesData | `ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM` | DynamoDBTable | observed | EXAMINE | 建模产物 — 源码/IaC 审计证明该调用不存在 | 漂移 `declared_not_observed`；实验 `source-audit-20260915` |
| 14 | petsite | AccessesData | `StepFnStateMachine76D362E8-3jkn8j2OUpdQ` | StepFunction | observed | TEST | Confirmed（IAM 拒绝）— no exceptions noted | 漂移 `declared_not_observed`；⚠️ 未登记的证据通道 `stepfn-execution-history+business-probe`；实验 `stepfn-three-ring-1789627178` |
| 15 | petsite | AccessesData | `bedrockagentcore` | AWSServiceEndpoint | 未评估（Assessment not performed） | EXAMINE | Not tested — assessment not performed |  |
| 16 | petsite | AccessesData | `serviceseks2-databaseb269d8bb-efjeyzicx2ak` | RDSCluster | observed | EXAMINE | 建模产物 — 源码/IaC 审计证明该调用不存在 | 漂移 `declared_not_observed`；实验 `source-audit-20260915` |
| 17 | petsite | AccessesData | `serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb` | S3Bucket | observed | EXAMINE | 建模产物 — 源码/IaC 审计证明该调用不存在 | 漂移 `declared_not_observed`；实验 `source-audit-20260915` |
| 18 | petsite | AccessesData | `sns` | AWSServiceEndpoint | external | EXAMINE | Not tested — assessment not performed |  |
| 19 | petsite | AccessesData | `ssm` | AWSServiceEndpoint | external | TEST | Confirmed（未声明，范围受限） | **范围限制**：⚠️ 边上未记录切断手段 —— 无法判断该结论的适用范围；【退化指标】仅吞吐塌陷、成功率未变（`abort` 类故障不产生响应行）。同属早期词汇，描述指标而非观测来源；实验 `exp-petsite-network-partition-20260831-165936` |
| 20 | petsite | AccessesData | `stepfunctions` | AWSServiceEndpoint | external | EXAMINE | Not tested — assessment not performed |  |
| 21 | petsite | AccessesData | `sts` | AWSServiceEndpoint | external | EXAMINE | Not tested — assessment not performed |  |
| 22 | petsite | Calls | `payforadoption` | Microservice | observed | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `exp-pay-for-adoption-http-chaos-20260905-053059` |
| 23 | petsite | Calls | `petfood` | Microservice | observed | TEST | Confirmed（未声明，范围受限） | **范围限制**：⚠️ 边上未记录切断手段 —— 无法判断该结论的适用范围；⚠️ 未记录证据通道；实验 `networkchaos-partition:petsite->petfood` |
| 24 | petsite | Calls | `pethistory` | Microservice | observed | TEST | Confirmed（未声明，范围受限） | **范围限制**：⚠️ 边上未记录切断手段 —— 无法判断该结论的适用范围；【退化指标】仅吞吐塌陷、成功率未变（`abort` 类故障不产生响应行）。同属早期词汇，描述指标而非观测来源；实验 `exp-pethistory-http-chaos-20260905-064354` |
| 25 | petsite | Calls | `petlistadoptions` | Microservice | observed | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `exp-list-adoptions-http-chaos-20260905-064919` |
| 26 | petsite | Calls | `petsearch` | Microservice | observed | TEST | Confirmed（未声明，范围受限） | **范围限制**：⚠️ 边上未记录切断手段 —— 无法判断该结论的适用范围；【退化指标】仅吞吐塌陷、成功率未变（`abort` 类故障不产生响应行）。同属早期词汇，描述指标而非观测来源；实验 `exp-search-service-http-chaos-20260905-073516` |
| 27 | petsite | DependsOn | `ServicesEks2-sqspetadoption2E8B1217-4T0KP1GHgwoj` | SQSQueue | observed | TEST | Confirmed（IAM 拒绝）— no exceptions noted | 实验 `iam-deny-probe_20260913-212633` |
| 28 | petsite | DependsOn | `WaggleAIOrchestrator` | AgentRuntime | observed | EXAMINE | Not tested — assessment not performed |  |
| 29 | petsite | DependsOn | `cdk-hnb659fds-container-assets-926093770964-ap-northeast-1` | ECRRepository | scaffolding | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `image-repo-dependency: cutting ECR affects only new Pod image pulls, not already-running Pods; no runtime degradation observable` |
| 30 | petsite | DependsOn | `petadoptions/petsite` | ECRRepository | observed | EXAMINE | Not tested — assessment not performed |  |
| 31 | petsite | PublishesTo | `ServicesEks2-sqspetadoption2E8B1217-4T0KP1GHgwoj` | SQSQueue | observed | TEST | Confirmed（IAM 拒绝）— no exceptions noted | 实验 `iam-deny-probe_20260913-212633` |
| 32 | petsite | PublishesTo | `ServicesEks2-topicpetadoption192CAB8F-Dn0iiU45RZ1g` | SNSTopic | observed | TEST | Confirmed（IAM 拒绝）— no exceptions noted | 漂移 `declared_not_observed`；实验 `iam-deny-probe_20260913-203500` |

### 5.3 PetInventoryManagement

重要性分级 `Tier1`；一跳直接依赖 **14** 条；多跳可达 **11** 个对象。

**表 5：PetInventoryManagement 的一跳直接依赖**

| # | 依赖方 | 依赖类型 | 依赖对象标识符 | 对象类型 | 范围 | 取证方法 | 结论 | 例外与备注 |
|---|---|---|---|---|---|---|---|---|
| 1 | ServicesEks2-statusupdaterservicelambdafn37242E00-0SHsIkrhwJ32 | AccessesData | `ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM` | DynamoDBTable | observed | EXAMINE | Not tested — assessment not performed |  |
| 2 | ServicesEks2-statusupdaterservicelambdafn37242E00-0SHsIkrhwJ32 | AccessesData | `dynamodb` | AWSServiceEndpoint | external | EXAMINE | Not tested — assessment not performed |  |
| 3 | petlistadoptions | AccessesData | `serviceseks2-databaseb269d8bb-efjeyzicx2ak` | RDSCluster | observed | TEST | Confirmed（未声明，范围受限） | 漂移 `declared_not_observed`；**范围限制**：⚠️ 边上未记录切断手段 —— 无法判断该结论的适用范围；⚠️ 未记录证据通道；实验 `exp-serviceseks2-databaseb269d8bb-efjeyzicx2ak-fis-rds-reboot-20260831-110323` |
| 4 | petlistadoptions | AccessesData | `serviceseks2-databasereader1f54479b8-hpyukqlufzus` | RDSInstance | 未评估（Assessment not performed） | EXAMINE | Not tested — assessment not performed |  |
| 5 | petlistadoptions | AccessesData | `serviceseks2-databasewriter2462cc03-fwgfu4gossqe` | RDSInstance | observed | TEST | Confirmed（数据库实例重启，范围受限） | **范围限制**：实例重启造成的**瞬时**中断（实测约 16~18 秒），覆盖「短暂丢失」场景；**不覆盖**「数据库长时间或彻底不可用」，亦不覆盖延迟升高；实验 `rds-fault-probe_20260915-044945` |
| 6 | petlistadoptions | Calls | `petsearch` | Microservice | observed | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `exp-search-service-http-chaos-20260905-073516` |
| 7 | petlistadoptions | DependsOn | `cdk-hnb659fds-container-assets-926093770964-ap-northeast-1` | ECRRepository | scaffolding | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `image-repo-dependency: cutting ECR affects only new Pod image pulls, not already-running Pods; no runtime degradation observable` |
| 8 | petsearch | AccessesData | `ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM` | DynamoDBTable | observed | TEST | Confirmed（IAM 拒绝）— no exceptions noted | 实验 `iam-deny-probe_20260913-155349` |
| 9 | petsearch | AccessesData | `dynamodb` | AWSServiceEndpoint | external | TEST | Confirmed（未声明，范围受限） | **范围限制**：⚠️ 边上未记录切断手段 —— 无法判断该结论的适用范围；【退化指标】成功率与吞吐**同时**塌陷。注意：该取值来自早期实验的另一套词汇，描述的是指标而非观测来源，与本节前两项不同轴；实验 `exp-dynamodb-fis-network-disrupt-20260831-160742` |
| 10 | petsearch | AccessesData | `s3` | AWSServiceEndpoint | external | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `exp-s3-fis-network-disrupt-20260831-160219` |
| 11 | petsearch | AccessesData | `serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb` | S3Bucket | observed | TEST | Confirmed（IAM 拒绝）— no exceptions noted | ⚠️ 未登记的证据通道 `business-probe-only`；实验 `iam-deny-probe_20260917-072110` |
| 12 | petsearch | AccessesData | `ssm` | AWSServiceEndpoint | external | EXAMINE | Not tested — assessment not performed |  |
| 13 | petsearch | AccessesData | `sts` | AWSServiceEndpoint | external | EXAMINE | 建模产物 — 源码/IaC 审计证明该调用不存在 | 实验 `source-audit-20260915` |
| 14 | petsearch | DependsOn | `cdk-hnb659fds-container-assets-926093770964-ap-northeast-1` | ECRRepository | scaffolding | TEST | Inconclusive — 已注入故障，观测退化不足以判定 | 实验 `image-repo-dependency: cutting ECR affects only new Pod image pulls, not already-running Pods; no runtime degradation observable` |


## 6 切断手段与证据范围

本节回答「凭什么说这条依赖已验证，以及该结论**不**适用于什么」。§5 每条 `Confirmed` 都注明了手段，手段的边界在此定义。

**已验证的 17 条边并非同质证据。**「已验证」不等于「已覆盖全部中断场景」——下表的「不覆盖」一列是本报告刻意突出的部分。

> **⚠️ 7 / 17 条 `Confirmed` 未记录切断手段。** 这些判定由早期实验写入，边上没有 `verify_severance`，因此**无法判断其结论的适用范围** ——读者不应假定它们与本轮实验同等强度。本报告不为这些边补写手段：那不是本轮采集的证据，追认手段等于伪造来源。整改方向见 §11。

**表 6：切断手段的证据范围与覆盖边数**

| 切断手段 | 中文名 | 验证边数 | 该手段的证据范围与不覆盖之处 |
|---|---|---|---|
| `unspecified` | 未声明 | 7 | ⚠️ 边上未记录切断手段 —— 无法判断该结论的适用范围 |
| `iam-deny` | IAM 拒绝 | 6 | 注入期间该依赖的调用**全程**返回 AccessDenied，覆盖「依赖不可用」场景；不覆盖延迟升高与部分失败 |
| `rds-reboot` | 数据库实例重启 | 3 | 实例重启造成的**瞬时**中断（实测约 16~18 秒），覆盖「短暂丢失」场景；**不覆盖**「数据库长时间或彻底不可用」，亦不覆盖延迟升高 |
| `k8s-service-blackhole` | 服务选择器黑洞 | 1 | 把 Kubernetes Service 的选择器改为不匹配任何 Pod，端点清空后调用方连接**立即失败**，覆盖「依赖不可用」场景；不覆盖延迟升高与部分失败。⚠️ 该手段切断的是**服务发现与路由**，被依赖服务本身仍在运行 —— 因此不覆盖「被依赖服务崩溃或返回错误响应」这一类失效 |

**表 7：证据通道分布**

| 证据通道 | 边数 | 说明 |
|---|---|---|
| `xray-edge+business-probe` | 7 | 【观测来源】消费方侧调用统计（X-Ray）＋ 源服务业务功能探针，双通道 |
| `unknown` | 3 | ⚠️ 未记录证据通道 |
| `throughput_only` | 3 | 【退化指标】仅吞吐塌陷、成功率未变（`abort` 类故障不产生响应行）。同属早期词汇，描述指标而非观测来源 |
| `rds-event+business-probe` | 1 | 【观测来源】⚠️ **消费方侧无调用遥测**；生效性由资源自身事件（RDS `DB instance shutdown`/`restarted`）证明，业务影响由探针证明 |
| `stepfn-execution-history+business-probe` | 1 | ⚠️ 未登记的证据通道 |
| `both` | 1 | 【退化指标】成功率与吞吐**同时**塌陷。注意：该取值来自早期实验的另一套词汇，描述的是指标而非观测来源，与本节前两项不同轴 |
| `business-probe-only` | 1 | ⚠️ 未登记的证据通道 |

**共同限制（适用于全部手段）**：本轮取证均为**可用性**维度的切断实验，不覆盖延迟升高、部分失败、数据正确性与容量耗尽等场景。因此本报告不对依赖做 hard／soft 分级 —— 分级需要延迟与部分失败场景的证据，而那些实验尚未进行。


## 7 证据状态分列统计

三个维度回答三个不同问题，**刻意不合并成单一比率**：平均会让「已声明但从未观测」与「已观测但从未验证」互相抵消，而那是监管审查最容易挑的点。

### 6.1 证据等级

凭什么说这条依赖成立（SYSC 15A.5.3R 的测试证据）。

**表 8：证据等级**

| 取值 | 条数 | 占比 |
|---|---|---|
| confirmed | 17 | 34% |
| 未评估（Assessment not performed） | 16 | 32% |
| inconclusive | 10 | 20% |
| modeling_artifact | 5 | 10% |
| bootstrap_only | 2 | 4% |

### 6.2 声明 vs 观测

配置声明的，还是运行时观测到的。

**表 9：声明 vs 观测**

| 取值 | 条数 | 占比 |
|---|---|---|
| dynamic | 40 | 80% |
| static | 8 | 16% |
| 未评估（Assessment not performed） | 2 | 4% |

### 6.3 第三方范围

哪些是第三方（DORA Art. 8(5) 的 interconnections）。

**表 10：第三方范围**

| 取值 | 条数 | 占比 |
|---|---|---|
| observed | 30 | 60% |
| external | 12 | 24% |
| scaffolding | 4 | 8% |
| 未评估（Assessment not performed） | 4 | 8% |


## 8 技术集中度

对应 SYSC 15A.2.7G(10) 的原文要求：评估 *the potential aggregate impact of disruptions to multiple important business services, in particular where such services rely on common operational resources as identified by the firm's mapping exercise*。

**表 11：被多个业务功能共同依赖的对象**

| 被依赖对象标识符 | 对象类型 | 范围 | 支撑业务功能数 | 边数 |
|---|---|---|---|---|
| `serviceseks2-databaseb269d8bb-efjeyzicx2ak` | RDSCluster | observed | 3 / 3 | 4 |
| `serviceseks2-databasereader1f54479b8-hpyukqlufzus` | RDSInstance | 未评估（Assessment not performed） | 3 / 3 | 3 |
| `serviceseks2-databasewriter2462cc03-fwgfu4gossqe` | RDSInstance | observed | 3 / 3 | 3 |
| `ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM` | DynamoDBTable | observed | 2 / 3 | 4 |
| `cdk-hnb659fds-container-assets-926093770964-ap-northeast-1` | ECRRepository | scaffolding | 2 / 3 | 4 |
| `dynamodb` | AWSServiceEndpoint | external | 2 / 3 | 3 |
| `petsearch` | Microservice | observed | 2 / 3 | 3 |
| `serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb` | S3Bucket | observed | 2 / 3 | 3 |
| `ssm` | AWSServiceEndpoint | external | 2 / 3 | 3 |
| `sts` | AWSServiceEndpoint | external | 2 / 3 | 2 |

## 9 基础设施承载层（单列，非服务消费关系）

本节所列边**不是**依赖。它们普遍为真、不携带判别信息，故不进依赖表；但 EKS 集群与负载均衡器在 DORA 视角下确实是关键 ICT 服务，故在此列出，以免读者误以为被遗漏。范围排除的理由见 §10.5。

**表 12：承载/放置类关系**

| 边类型 | 条数 | 性质 |
|---|---|---|
| LocatedIn | 1617 | 承载/放置关系，非服务消费关系 |
| RunsOn | 1955 | 承载/放置关系，非服务消费关系 |
| BelongsTo | 1068 | 承载/放置关系，非服务消费关系 |
| Manages | 1062 | 承载/放置关系，非服务消费关系 |
| Routes | 1050 | 承载/放置关系，非服务消费关系 |

## 10 业务功能与容忍度阈值

阈值取值词汇对齐 ITS (EU) 2024/2956 B_06.01.0080/0090 的口径（见 §4）。

**表 13：业务功能的重要性分级与容忍度阈值**

| 业务功能 | 重要性分级 | impact_tolerance_seconds | rto_target_seconds |
|---|---|---|---|
| AdoptionHistoryView | Tier1 | 未设定（Not defined） | 未设定（Not defined） |
| PetAdoptionFlow | Tier0 | 未设定（Not defined） | 未设定（Not defined） |
| PetInventoryManagement | Tier1 | 未设定（Not defined） | 未设定（Not defined） |

**9.1 两个阈值必须分列。** RTO 是恢复某个流程的目标时间（内部视角）；impact tolerance 是重要业务服务的最大可容忍中断（外部危害视角）。两者可以差数倍，混用是监管审查重点。

## 11 重大固有局限性与范围排除

本节对应 ISAE 3000 §69(e) 与 SYSC 15A.6.1R(7)。每一条的数字均由本次快照现算，不是手写的固定文本 —— 手写的披露会过期。

**11.1 证据覆盖率 34%。** 50 条依赖里经实测确证（TEST/Confirmed）仅 17 条，16 条未测试。整改方向为扩大故障注入覆盖，当前受限于注入手段对 agent 层与部分托管服务的可达性。

**11.1a 上述分母含 6 条粒度重复，去重后覆盖率为 39%（17/44）。** 同一条依赖被两条边表示（端点级 `AWSServiceEndpoint` 与资源级资源节点），两条都为真但不是两个依赖，分母因此虚高。其中 6 条可从可评估分母移出；另 0 条**拒绝移出** —— 这些边的判定恰好落在粗粒度那一侧而细粒度为空，移出会把已采集到的实测结论从分母里藏掉。该 0 条的整改动作是把证据归并到资源粒度那条边，属语义搬迁，须逐条确认切断手段的作用域是否适用于资源粒度，不做自动归并。

**11.2 已验证的 17 条边并非同等强度证据。** 「已验证」这一个计数掩盖了三个不同的缺口，逐项披露如下（详见 §6）：

- **7 条未记录切断手段**，因此其结论的适用范围不明。这些判定由早期实验写入。**本报告不为它们追认手段** —— 那不是本轮采集的证据。整改方向：重跑这些边的切断实验并记录手段，或将其降级回未评估。

- **3 条仅做过瞬时中断测试**（数据库实例重启，实测中断约 16~18 秒）。该证据**不支撑**「数据库长时间或彻底不可用」场景下的结论。整改方向：补充长时中断场景的实验。

- **`verify_evidence_channel` 字段语义不统一。** 4 条边的取值来自早期词汇（描述「哪个指标退化」），与本轮词汇（描述「哪个观测来源」）**不在同一语义轴上**。同一字段承载两套词汇会让筛选与统计失真。整改方向：拆成两个字段，或为历史值补记来源 —— 但不得靠推测回填。

**11.3 2 条边的 `dependency_kind` 为未评估。** 这些是边类型分类新近变更、尚待下轮 ETL 补标的边。该取值表示「尚未打标」，不表示「不适用」。

**11.4 SYSC 15A.4.1R 的六要素只覆盖两项。** 该条原文要求 identify and document the people, processes, technology, facilities and information necessary to deliver each important business service：

**表 14：SYSC 15A.4.1R 六要素的覆盖状态**

| 要素 | 状态 | 依据 |
|---|---|---|
| technology | 覆盖 | Microservice / Pod / EC2 / Lambda / 各类托管服务 |
| facilities | 覆盖 | AvailabilityZone / Region / VPC / Subnet |
| people | 缺失 | 契约与活图谱均无责任人/团队实体 |
| processes | 缺失 | 同上，无流程实体 |
| information | 部分 | 数据基础设施有（数据访问类边），表/字段级血缘无 |

**11.5 impact tolerance 未设定（3/3 个业务功能）。** SYSC 15A.2.5R 要求 firm *must* set an impact tolerance for each important business service。**本报告不得出现任何「未越界」表述** —— 没有阈值就是没有阈值，不能用「没检测到越界」掩盖「压根没有阈值可比」。

**11.6 承载层不在依赖表内（范围排除）。** 承载/放置类关系普遍为真、不携带判别信息，算进依赖会让 Region 成为所有东西的咽喉点。代价是 EKS 集群与负载均衡器不出现在依赖表中，而它们在 DORA 视角下确实是关键 ICT 服务 —— 故单列于 §8 并注明性质。

**11.7 集中度是技术集中度，不是供应商集中度（范围排除）。** DORA Art. 29/31 要的是供应商层面的集中度与分包链，需要法人实体节点才能回答；本平台当前只有技术对象。第四方分包链在埋点边界外，结构上做不到。

**11.8 未采用 ITS B_05.02 的 `Rank` 分层（范围排除）。** 法定模版用 `Rank`（直接第三方=1，分包商逐级>1）表达供应链层级。本报告的多跳可达数是技术依赖深度，与 Rank 的法人分包语义不同，**刻意不混用该字段名**。

**11.9 无历史版本查询能力。** SYSC 15A.6.2R 要求保存 each version 满 6 年。本报告通过「文件名带快照时刻、不覆盖历史」满足留存，但图谱本身无双时态，无法回答「三个月前这条依赖是什么状态」。

## 附录 A 编制方法与所执行工作摘要

对应 ISAE 3000 §69(k)（所执行工作的信息性摘要）与 SYSC 15A.6.1R(3)(9)（mapping 方法与所用方法论的书面记录）。

**A.1 数据获取。** 对 `petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com` 执行只读 openCypher 查询。导出层不含任何写操作，由 `tests/test_54_compliance_export.py::m07` 静态扫描锁定 —— 生成合规报告的过程若会改动被报告的对象，报告本身就不可采信。

**A.2 依赖边的判定口径。** 依赖边类型清单取自契约函数，共 10 种；承载/放置类关系不计入，且经门禁校验两集合零重叠。

**A.3 一跳与多跳分开呈现。** 报告主体用一跳直接依赖 —— 每条边有明确的源、目标、观测来源与证据等级，是可归责的单位。多跳可达数作为补充统计，回答「这项业务功能一共牵连多少资产」，两种口径刻意不合并。

**A.4 取证方法的判定。** `TEST` 表示已执行故障注入实验；`EXAMINE` 表示仅审阅运行时遥测与配置。词汇取自 NIST OSCAL `observation.method`，其强度序列 TEST > EXAMINE > INTERVIEW 源自 NIST SP 800-53A。

**A.5 快照一致性。** 基准时刻在快照构造时取一次，全部表格共用。由 `m05` 以 AST 扫描锁定「`take_snapshot` 内取当前时刻恰好一次」—— 分次取时刻会让同一份报告的不同表落在不同基准日上。

**A.6 本报告的自动化程度。** 全部数字与披露文本由代码从快照现算，无人工编辑环节。因此本报告不含人工判断，也不含对数字合理性的复核 —— 该复核是 SYSC 15A.7.1R 所要求的 governing body 审批环节的内容。
