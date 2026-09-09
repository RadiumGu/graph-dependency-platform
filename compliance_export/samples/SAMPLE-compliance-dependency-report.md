# 合规依赖关系报告

| 项 | 值 |
|---|---|
| **快照时刻（基准日）** | `2026-09-09T15:32:49Z` |
| 数据源 | `petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com` |
| 业务能力数 | 3 |
| 一跳依赖边数 | 43 |
| 依赖边类型（10 种） | `AccessesData / Calls / Delegates / DependsOn / Invokes / InvokesTool / PublishesTo / Retrieves / RoutesToRuntime / RoutesVia` |

> **依赖边清单来源**：`graph_contract.dependency_edge_labels()`（契约，单一来源）。
> 本报告全部表格取自**同一次会话、同一快照时刻** —— 跨表数字可交叉校验。
> 活图谱正被 ETL 持续改写，不同时刻导出的数字会不同，这是正常的；
> 但同一份报告内部若出现矛盾，则是缺陷。

## 三栏分列统计

> 三个维度回答三个不同问题，**刻意不合并成单一覆盖率**：
> 平均会让「已声明但从未观测」与「已观测但从未验证」互相抵消。

### 证据等级

> 凭什么说这条依赖成立（SYSC 15A.5.3R 的测试证据）

| 取值 | 条数 | 占比 |
|---|---|---|
| （无此字段） | 24 | 56% |
| inconclusive | 10 | 23% |
| confirmed | 9 | 21% |

### 声明 vs 观测

> 配置声明的，还是运行时观测到的

| 取值 | 条数 | 占比 |
|---|---|---|
| dynamic | 33 | 77% |
| static | 8 | 19% |
| （无此字段） | 2 | 5% |

### 第三方范围

> 哪些是第三方（DORA Art. 8(5) 的 interconnections）

| 取值 | 条数 | 占比 |
|---|---|---|
| observed | 28 | 65% |
| external | 11 | 26% |
| scaffolding | 4 | 9% |

## 功能映射表（按业务能力）

### AdoptionHistoryView（Tier1）— 一跳依赖 3 条，多跳可达 3 个对象

| 服务 | 边类型 | 被依赖对象 | 对象类型 | scope | kind | verify | conf | source | drift |
|---|---|---|---|---|---|---|---|---|---|
| pethistory | AccessesData | serviceseks2-databaseb269d8bb-efjeyzicx2ak | RDSCluster | observed | static | inconclusive | – | aws-etl | ok |
| pethistory | AccessesData | serviceseks2-databasewriter2462cc03-fwgfu4gossqe | RDSInstance | observed | dynamic | – | – | deepflow-l4 | – |
| pethistory | DependsOn | pet-adoptions-history | ECRRepository | observed | dynamic | – | – | deepflow-etl | – |

### PetAdoptionFlow（Tier0）— 一跳依赖 27 条，多跳可达 37 个对象

| 服务 | 边类型 | 被依赖对象 | 对象类型 | scope | kind | verify | conf | source | drift |
|---|---|---|---|---|---|---|---|---|---|
| payforadoption | AccessesData | ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM | DynamoDBTable | observed | static | – | – | aws-etl | declared_not_observed |
| payforadoption | AccessesData | StepFnStateMachine76D362E8-3jkn8j2OUpdQ | StepFunction | observed | dynamic | – | – | deepflow-dns | observed_then_silent |
| payforadoption | AccessesData | dynamodb | AWSServiceEndpoint | external | dynamic | confirmed | – | nfm | – |
| payforadoption | AccessesData | serviceseks2-databaseb269d8bb-efjeyzicx2ak | RDSCluster | observed | static | confirmed | – | aws-etl | declared_not_observed |
| payforadoption | AccessesData | serviceseks2-databasewriter2462cc03-fwgfu4gossqe | RDSInstance | observed | dynamic | – | – | deepflow-l4 | – |
| payforadoption | AccessesData | serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb | S3Bucket | observed | dynamic | – | – | deepflow-dns | observed_then_silent |
| payforadoption | AccessesData | ssm | AWSServiceEndpoint | external | dynamic | – | – | xray | – |
| payforadoption | DependsOn | cdk-hnb659fds-container-assets-926093770964-ap-northeast-1 | ECRRepository | scaffolding | dynamic | inconclusive | – | deepflow-etl | – |
| petsite | AccessesData | ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM | DynamoDBTable | observed | dynamic | – | – | deepflow-dns | declared_not_observed |
| petsite | AccessesData | StepFnStateMachine76D362E8-3jkn8j2OUpdQ | StepFunction | observed | dynamic | – | – | xray | declared_not_observed |
| petsite | AccessesData | serviceseks2-databaseb269d8bb-efjeyzicx2ak | RDSCluster | observed | dynamic | confirmed | – | deepflow-dns | declared_not_observed |
| petsite | AccessesData | serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb | S3Bucket | observed | dynamic | – | – | deepflow-dns | declared_not_observed |
| petsite | AccessesData | sns | AWSServiceEndpoint | external | dynamic | – | – | appsignals-etl | – |
| petsite | AccessesData | ssm | AWSServiceEndpoint | external | dynamic | confirmed | – | xray | – |
| petsite | AccessesData | stepfunctions | AWSServiceEndpoint | external | dynamic | – | – | appsignals-etl | – |
| petsite | AccessesData | sts | AWSServiceEndpoint | external | dynamic | – | – | xray | – |
| petsite | Calls | payforadoption | Microservice | observed | dynamic | inconclusive | – | deepflow-etl | – |
| petsite | Calls | petfood | Microservice | observed | dynamic | confirmed | – | deepflow-etl | – |
| petsite | Calls | pethistory | Microservice | observed | dynamic | confirmed | – | deepflow-etl | – |
| petsite | Calls | petlistadoptions | Microservice | observed | dynamic | inconclusive | – | deepflow-etl | – |
| petsite | Calls | petsearch | Microservice | observed | dynamic | confirmed | – | deepflow-etl | – |
| petsite | DependsOn | ServicesEks2-sqspetadoption2E8B1217-4T0KP1GHgwoj | SQSQueue | observed | dynamic | – | – | xray | – |
| petsite | DependsOn | WaggleAIOrchestrator | AgentRuntime | observed | static | – | – | agentcore-etl | – |
| petsite | DependsOn | cdk-hnb659fds-container-assets-926093770964-ap-northeast-1 | ECRRepository | scaffolding | dynamic | inconclusive | – | deepflow-etl | – |
| petsite | DependsOn | petadoptions/petsite | ECRRepository | observed | dynamic | – | – | deepflow-etl | – |
| petsite | PublishesTo | ServicesEks2-sqspetadoption2E8B1217-4T0KP1GHgwoj | SQSQueue | observed | – | – | – | aws-etl | declared_not_observed |
| petsite | PublishesTo | ServicesEks2-topicpetadoption192CAB8F-Dn0iiU45RZ1g | SNSTopic | observed | – | – | – | aws-etl | declared_not_observed |

### PetInventoryManagement（Tier1）— 一跳依赖 13 条，多跳可达 10 个对象

| 服务 | 边类型 | 被依赖对象 | 对象类型 | scope | kind | verify | conf | source | drift |
|---|---|---|---|---|---|---|---|---|---|
| ServicesEks2-statusupdaterservicelambdafn37242E00-0SHsIkrhwJ32 | AccessesData | ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM | DynamoDBTable | observed | static | – | – | aws-etl | – |
| ServicesEks2-statusupdaterservicelambdafn37242E00-0SHsIkrhwJ32 | AccessesData | dynamodb | AWSServiceEndpoint | external | dynamic | – | – | xray | – |
| petlistadoptions | AccessesData | serviceseks2-databaseb269d8bb-efjeyzicx2ak | RDSCluster | observed | static | confirmed | – | aws-etl | ok |
| petlistadoptions | AccessesData | serviceseks2-databasewriter2462cc03-fwgfu4gossqe | RDSInstance | observed | dynamic | – | – | deepflow-l4 | – |
| petlistadoptions | Calls | petsearch | Microservice | observed | dynamic | inconclusive | – | deepflow-etl | – |
| petlistadoptions | DependsOn | cdk-hnb659fds-container-assets-926093770964-ap-northeast-1 | ECRRepository | scaffolding | dynamic | inconclusive | – | deepflow-etl | – |
| petsearch | AccessesData | ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM | DynamoDBTable | observed | static | inconclusive | – | aws-etl | ok |
| petsearch | AccessesData | dynamodb | AWSServiceEndpoint | external | dynamic | confirmed | – | nfm | – |
| petsearch | AccessesData | s3 | AWSServiceEndpoint | external | dynamic | inconclusive | – | xray | – |
| petsearch | AccessesData | serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb | S3Bucket | observed | static | – | – | aws-etl | ok |
| petsearch | AccessesData | ssm | AWSServiceEndpoint | external | dynamic | – | – | xray | – |
| petsearch | AccessesData | sts | AWSServiceEndpoint | external | dynamic | – | – | xray | – |
| petsearch | DependsOn | cdk-hnb659fds-container-assets-926093770964-ap-northeast-1 | ECRRepository | scaffolding | dynamic | inconclusive | – | deepflow-etl | – |

## 技术集中度（SYSC 15A.2.7G(10) / DORA Art. 29-31 的技术输入）

> 原文要求评估 *the potential aggregate impact of disruptions to multiple
> important business services, in particular where such services rely on
> **common operational resources** as identified by the firm's mapping exercise*。

| 被依赖对象 | 类型 | scope | 支撑业务功能数 | 边数 |
|---|---|---|---|---|
| serviceseks2-databaseb269d8bb-efjeyzicx2ak | RDSCluster | observed | 3 / 3 | 4 |
| serviceseks2-databasewriter2462cc03-fwgfu4gossqe | RDSInstance | observed | 3 / 3 | 3 |
| ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM | DynamoDBTable | observed | 2 / 3 | 4 |
| cdk-hnb659fds-container-assets-926093770964-ap-northeast-1 | ECRRepository | scaffolding | 2 / 3 | 4 |
| dynamodb | AWSServiceEndpoint | external | 2 / 3 | 3 |
| serviceseks2-s3bucketpetadoptioncb20dce5-69ffxu9epttb | S3Bucket | observed | 2 / 3 | 3 |
| ssm | AWSServiceEndpoint | external | 2 / 3 | 3 |
| petsearch | Microservice | observed | 2 / 3 | 2 |
| sts | AWSServiceEndpoint | external | 2 / 3 | 2 |

## 基础设施承载层（单列，非服务消费关系）

> 这些边**不是**依赖。它们普遍为真、不携带判别信息，故不进依赖表；
> 但 EKS 集群与负载均衡器在 DORA 视角下确实是关键 ICT 服务，故在此列出。

| 边类型 | 条数 | 性质 |
|---|---|---|
| LocatedIn | 1085 | 承载/放置关系，非服务消费关系 |
| RunsOn | 952 | 承载/放置关系，非服务消费关系 |
| BelongsTo | 549 | 承载/放置关系，非服务消费关系 |
| Manages | 554 | 承载/放置关系，非服务消费关系 |
| Routes | 531 | 承载/放置关系，非服务消费关系 |

## 业务能力与容忍度阈值

| 业务能力 | tier | impact_tolerance_seconds | rto_target_seconds |
|---|---|---|---|
| AdoptionHistoryView | Tier1 | – | – |
| PetAdoptionFlow | Tier0 | – | – |
| PetInventoryManagement | Tier1 | – | – |

> `impact_tolerance` 与 `rto_target` **必须分列**：RTO 是恢复某个流程的
> 目标时间（内部视角），impact tolerance 是 IBS 的最大可容忍中断
> （外部危害视角），两者可以差数倍。混用是监管审查重点。

## 必须随报告一同披露的局限

**1. 证据覆盖率 21%。** 43 条依赖里 `confirmed` 仅 9 条，24 条从未验证。
`inconclusive`（注入了故障但退化不足以判定）与「未验证」刻意区分 —— 
零流量与健康在指标上无法区分，不可混为「依赖不成立」。

**2. 2 条边的 `dependency_kind` 为空。** 这些是边类型分类新近变更、尚待下轮 ETL 补标的边。空值表示「尚未打标」，不表示「不适用」。

**3. SYSC 15A.4.1R 的六要素只覆盖两项。** 该条原文要求 identify and document 
the **people, processes, technology, facilities and information** necessary 
to deliver each important business service：

| 要素 | 状态 | 依据 |
|---|---|---|
| technology | ✅ 覆盖 | Microservice / Pod / EC2 / Lambda / 各类托管服务 |
| facilities | ✅ 覆盖 | AvailabilityZone / Region / VPC / Subnet |
| people | ❌ 缺失 | 契约与活图谱均无责任人/团队实体 |
| processes | ❌ 缺失 | 同上，无流程实体 |
| information | 🟡 部分 | 数据基础设施有（AccessesData），表/字段级血缘无 |

**4. impact tolerance 未设定（3/3 个业务能力为空）。** SYSC 15A.2.5R 要求 firm *must* set an impact tolerance for each important business service。
**本报告不得出现任何「未越界」表述** —— 没有阈值就是没有阈值，
不能用「没检测到越界」掩盖「压根没有阈值可比」。

**5. 承载层不在依赖表内。** `LocatedIn` / `RunsOn` 等承载关系普遍为真、
不携带判别信息，算进依赖会让 Region 成为所有东西的咽喉点。代价是 EKS 集群与
负载均衡器不出现在依赖表中，而它们在 DORA 视角下确实是关键 ICT 服务 ——
故单列于「承载层」一节，并注明性质。

**6. 集中度是技术集中度，不是供应商集中度。** DORA Art. 29/31 要的是
供应商层面的集中度与分包链，需要 `Vendor` 法人实体节点才能回答；
本平台当前只有技术对象。第四方分包链在埋点边界外，**结构上做不到**。

**7. 本报告不是 DORA Art. 28 信息登记册。** 那份由 ITS (EU) 2024/2956 规定
15 张互联模板、以 xBRL-CSV 年度报送，字段是合同编号、通知期、适用法律、
20 位 LEI、退出策略 —— 本质是合同清单不是图。本报告对应的是 DORA Art. 8(4)
「map the links and interdependencies」、BCBS POR 原则四、SYSC 15A.4.1R
与关基条例第九条那一类**穿透式依赖映射**要求。
