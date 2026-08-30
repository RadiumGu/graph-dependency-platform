# ETL 复杂度实测与可维护性

**日期**：2026-08-30 14:35 UTC
**方法**：Python `ast` 模块统计（未 grep 估算）。`radon` 本机未安装，圈复杂度用 ast 分支节点计数近似（`if/for/while/except/BoolOp/comprehension/IfExp/assert` +1，`and/or` 按子句数）。

---

## 0. 先剔除一个会误导所有人的数字

`infra/lambda/etl_*/` 目录里 88–96 个 `.py` 文件中**绝大多数是打包进 Lambda 的第三方库**（`requests`、`urllib3`、`certifi`、`charset_normalizer`、`idna`、`yaml`）。

- `etl_aws/`：磁盘 **91 文件 / 40,371 行**，第一方仅 **14 文件 / 3,863 行**（约 90% 是 requests/urllib3 副本）
- `etl_cfn/`：磁盘 **96 文件 / 42,890 行**，第一方仅 **1 文件 / 459 行**

**下面所有数字均已剔除 vendored 库。** 若有人拿目录行数说「你这 ETL 八万行」，先纠正这一点。

---

## 1. 真实规模

| 模块 | 第一方文件 | 总行数 | 函数数 | 最长函数（行数 / 位置） |
|---|--:|--:|--:|---|
| etl_aws | 14 | 3,863 | 68 | **1,234 行** `run_etl` — `infra/lambda/etl_aws/handler.py:62`（L62–1295） |
| etl_deepflow | 1 | 1,865 | 30 | 258 行 `run_etl` — `infra/lambda/etl_deepflow/neptune_etl_deepflow.py:1600` |
| etl_xray | 1 | 861 | 17 | 136 行 `fetch_xray_service_graph` — `infra/lambda/etl_xray/neptune_etl_xray.py:233` |
| etl_cfn | 1 | 459 | 11 | 93 行 `extract_declared_deps` — `infra/lambda/etl_cfn/neptune_etl_cfn.py:228` |
| etl_trigger | 1 | 111 | 2 | 60 行 `handler` — `infra/lambda/etl_trigger/neptune_etl_trigger.py:33` |
| rca_window_flush | 34 | 7,492 | 175 | 289 行 `generate_rca_report` — `infra/lambda/rca_window_flush/core/graph_rag_reporter.py:355` |
| shared（Lambda 层） | 1 | 99 | 5 | 33 行 `_get_creds` — `infra/lambda/shared/python/neptune_client_base.py:30` |

**五个写入/触发 ETL 合计：18 文件 / 7,159 行 / 128 函数。** 含 rca 的第一方合计：52 文件 / 14,651 行 / 303 函数。

这个体量不算大——**问题不在总量，在分布**。

---

## 2. 复杂度热点

### 行数最多的十个函数

| 行数 | ~CC | 嵌套 | 参数 | 函数 / 位置 |
|--:|--:|--:|--:|---|
| **1234** | **325** | 6 | 0 | `run_etl` `etl_aws/handler.py:62` |
| 289 | 43 | 7 | 5 | `generate_rca_report` `rca_window_flush/core/graph_rag_reporter.py:355` |
| 258 | 41 | 4 | 0 | `run_etl` `etl_deepflow/neptune_etl_deepflow.py:1600` |
| 227 | 49 | 7 | 6 | `step4_score` `rca_window_flush/core/rca_engine.py:489` |
| 165 | 28 | 7 | 2 | `run_drift_detection` `etl_deepflow/neptune_etl_deepflow.py:724` |
| 140 | 13 | 4 | 2 | `_update_causal_weights` `rca_window_flush/actions/incident_writer.py:247` |
| 136 | 35 | 5 | 1 | `fetch_xray_service_graph` `etl_xray/neptune_etl_xray.py:233` |
| 123 | 19 | 4 | 1 | `_process_group` `rca_window_flush/window_flush_handler.py:80` |
| 122 | 27 | 5 | 1 | `_get_neptune_subgraph` `rca_window_flush/core/graph_rag_reporter.py:177` |
| 118 | 24 | 7 | 0 | `upsert_business_capabilities` `etl_aws/business_layer.py:22` |

### 其他极值

- **近似 CC 最高**：`run_etl`=325、`step4_score`=49、`generate_rca_report`=43、deepflow `run_etl`=41、`_resolve_datastore_ips`=40（`neptune_etl_deepflow.py:322`）、`_build_group_context`=36、`fetch_xray_service_graph`=35、`run_drift_detection`=28、`run_gc`=27（`etl_aws/graph_gc.py:49`）
- **嵌套最深**：8 层 — `step3_graph_candidates` `rca_window_flush/core/rca_engine.py:174`
- **参数最多**：`run_gc` **12 个** `etl_aws/graph_gc.py:49`；`_send_with_buttons` 10 个；`batch_fetch_dependency_and_update` 8 个

> 单点头号热点：`etl_aws/handler.py:62 run_etl` 一个函数 1,234 行、~CC 325，是整个 etl_aws 采集流程的巨型顺序编排体，规模是第二名的 4.8 倍。

对照 **Cartography**（架构同构：多 module 写同一图库）——它控制复杂度的手段不是少写代码，而是把每个 module 固定成 `Get → Transform → Load → Cleanup` 四段，且 **Load 与 Cleanup 由 schema 对象生成**：

```python
cleanup_job = GraphJob.from_node_schema(EMRClusterSchema(), common_job_parameters)
cleanup_job.run(neo4j_session)
```

所以 `run_etl` 那 1,234 行里，真正属于业务的是 Get / Transform，**Load / Cleanup 那部分本该不存在**。

---

## 3. 重复与耦合

| 重复项 | 位置对照 | 说明 |
|---|---|---|
| Neptune 底层客户端**字节级重复** | `infra/lambda/etl_aws/neptune_client_base.py` ≡ `infra/lambda/shared/python/neptune_client_base.py`（md5 均 `d019cdb…`，各 99 行） | 两份完全相同的 SigV4 + HTTP Gremlin 客户端 |
| Neptune 客户端**第三份分叉实现** | `infra/lambda/rca_window_flush/neptune/neptune_client.py`（99 行，md5 `c64f34e…`） | 与上面两份**不同**的独立实现 |
| upsert 幂等惯用法**各自手写** | `etl_aws/neptune_client.py:56` `upsert_vertex`、`:155` `upsert_edge`；deepflow `:1097` `batch_upsert_nodes`(mergeV)、`:1171` `batch_upsert_edges`；xray `:521`、`:672`；cfn `:71` `get_or_create_vertex`、`:115` `upsert_cfn_edge` | 每个 ETL 各造一套。`coalesce(` 出现次数：deepflow 5、business_layer 4、handler 3、xray 3 |
| ECR 镜像名解析**逐字符相同** | `etl_aws/business_layer.py:207` == `etl_deepflow/neptune_etl_deepflow.py:1007`，均为 `image.split(ecr_suffix)[-1].split(':')[0].split('@')[0]` | 完全相同的一行 |
| cfn **代码注释自认**复制 etl_aws | `etl_cfn/neptune_etl_cfn.py:83`「这与 etl_aws/neptune_client.py:upsert_vertex 的兜底模式一致」；`:130`「依赖语义边集合见 etl_aws 的 DEPENDENCY_EDGE_LABELS」 | 作者已知的复制 |
| ARN `split(':')[-1]` 归名散落 6 处 | `business_layer.py:207`、`collectors/data_stores.py:97`、`collectors/lambda_sfn.py:105`、`graph_gc.py:109`、`handler.py:405/942/947` | 无共享 ARN 解析器 |
| 时间窗 `int(time.time())` 各自计算 7 处 | deepflow `:541/752/1173/1280/1517`、handler `:1041`、xray `:248` | 无共享 window 工具；`round_ts` 手工穿参 |

**耦合的另一面（非重复）**：四个写入 ETL **确实**都 `from neptune_client_base import neptune_query, safe_str, extract_value, REGION`（`business_layer.py:10`、`neptune_etl_deepflow.py:39`、`neptune_etl_xray.py:73`、`neptune_etl_cfn.py:26`）。**最底层 HTTP + SigV4 是共享的，重复只发生在上层 upsert 语义。**

---

## 4. ETL → 节点/边类型写入矩阵

权威声明在 `profiles/petsite.yaml` 的 `neptune.graph_schema_text`：文本自述 **33 种节点、26 种边**（`:175` / `:266`）。

> **计数矛盾待核实**：schema 文本写 33 种节点，2026-08-28 从活图谱实测是 31 种；schema 自己注释说「活图谱通常少 1 种即 `TopologyChange`」，但 33−31=2，差值对不上。需程序化重数，不要再引用声明的数字。

### 节点类型 × ETL（仅列有写入者的）

| 节点类型 | etl_aws | etl_deepflow | etl_xray | etl_cfn | 多源？ |
|---|:-:|:-:|:-:|:-:|---|
| Microservice | ✔ | ✔（mergeV, source=deepflow） | | | **⚠ 多源** |
| LambdaFunction | ✔ | | | ✔ | **⚠ 多源** |
| SQSQueue | ✔ | | | ✔（`:421`） | **⚠ 多源** |
| SNSTopic | ✔ | | | ✔（`:420`） | **⚠ 多源** |
| DynamoDBTable | ✔ | | | ✔ | **⚠ 多源** |
| StepFunction | ✔ | | | ✔ | **⚠ 多源** |
| LoadBalancer | ✔ | | | ✔ | **⚠ 多源** |
| AWSServiceEndpoint | | | ✔（`:521`） | | 单源 |
| Region / AZ / VPC / Subnet / SecurityGroup / EC2Instance / EKSCluster / Namespace / Pod / K8sService / Deployment / HPA / TargetGroup / ListenerRule / RDSCluster / Database / S3Bucket / ECRRepository / BusinessCapability | ✔ | | | | 单源(aws) |

cfn 建点走 `get_or_create_vertex` + `TYPE_TO_LABEL`（`etl_cfn:50`）。**它写的每一种节点都与 etl_aws 重叠。**（APIGateway / KinesisStream 已映射但零实例、刻意不写进 schema。）

schema 声明但四个写入 ETL 都不建的：`RDSInstance`、`NeptuneCluster`、`NeptuneInstance`、`Incident`（rca 写）、`ChaosExperiment`（chaos 写）、`TopologyChange`（deepflow 变更日志）。

### 边类型 × ETL

| 边类型 | etl_aws | etl_deepflow | etl_xray | etl_cfn | 源数 |
|---|:-:|:-:|:-:|:-:|:-:|
| **AccessesData** | ✔ | ✔ | ✔（补度量） | ✔（`:277`） | **4** |
| **DependsOn** | ✔ | ✔ | | ✔（`:294`） | **3** |
| LocatedIn | ✔ | | ✔（`:555`） | | 2 |
| PublishesTo | ✔ | | | ✔（`:423`） | 2 |
| Invokes | ✔ | | | ✔（`:310`） | 2 |
| Calls | | ✔ | （仅补度量） | | 1 |
| Contains / BelongsTo / OwnedBy / HasSG / RunsOn / Implements / Routes / Manages / HasRule / ForwardsTo / RoutesTo / ConnectsTo / WritesTo / TriggeredBy | ✔ | | | | 1 |

### 唯一有实测后果的冲突点

`Microservice` 节点被 aws + deepflow 双写。`etl_deepflow/neptune_etl_deepflow.py:1097` 的注释记录：**`Microservice.az` 在 `SET` 基数下曾累积成两个值**（笛卡尔扇出）。

好消息：`tests/test_31_property_cardinality.py:40/100/168/203` 已为此写了守门测试。

---

## 5. 配置来源

| ETL | 环境变量 | SSM | profiles yaml | 硬编码 / 生成 JSON |
|---|:-:|:-:|:-:|---|
| etl_aws | ✔ `config.py:12-16,60` | ✗ | **✗** | `FAULT_BOUNDARY_MAP` `config.py:19`；`_bc`/`_sm_data` 从 service_mappings.json 加载 `config.py:87-113` |
| etl_deepflow | ✔ `:45-51,318-319,1231-1259` | ✗ | ✗ | service_mappings.json（`:57` 注释） |
| etl_xray | ✔ `:108,112` | ✗ | ✗ | `XRAY_MAX_WINDOW_SECONDS=6*3600`，**无 env** `:107` |
| etl_cfn | ✔ `:32` | ✗ | ✗ | `TYPE_TO_LABEL` `:50` |
| etl_trigger | ✔ `:26-28` | ✗ | ✗ | REGION 默认 `'ap-northeast-1'` `:28` |
| rca_window_flush | ✔ | ✔ `action_executor.py:106,118` | **✔ `profile_loader`** `action_executor.py:101`、`nl_query_direct.py:59` | — |

> **关键事实**：被称作「权威声明」的 `profiles/petsite.yaml`，**五个写入/触发 ETL 无一直接读取**；只有 `rca_window_flush` 通过 `profile_loader` 读它。写入侧靠 env 变量 + 生成的 JSON + 内联字典。
>
> **这一条同时解释了另外两个问题**：ETL 为什么复杂——每个 ETL 都得自己重新表达一遍图的形状；ETL 为什么难维护——改一处语义要在四个地方同步改，而没有任何东西会在不同步时报错。

### 真实可执行的硬编码常量（env 默认值）

- etl_aws `config.py:12-16`：`NEPTUNE_PORT=8182`、`ENVIRONMENT='prod'`、占位 `'YOUR_NEPTUNE_ENDPOINT'`/`'YOUR_AWS_REGION'`/`'YOUR_EKS_CLUSTER_NAME'`
- deepflow `:46-51,318,1231-1236`：`CH_PORT=8123`、`INTERVAL_MIN=6`、`BATCH_SIZE=20`、`XRAY_WINDOW_SECONDS=1800`、`CALLS_INACTIVE_AFTER_SECONDS=1800`、`CALLS_DROP_AFTER_SECONDS=604800`
- xray `:107-108`：`XRAY_LOOKBACK_HOURS=24`、`XRAY_MAX_WINDOW_SECONDS=21600`
- cfn `:32`：`CFN_STACK_NAMES='ServicesEks2,Applications'`
- trigger `:26-28`：`TRIGGER_DELAY_SECONDS=30`、`ETL_FUNCTION_NAME='neptune-etl-from-aws'`、`REGION='ap-northeast-1'`

**服务名 / ARN / IP / account-id 全部只出现在 docstring 与注释里**（作为实测观测值的文档），**不是可执行常量**：account `926093770964` 与完整 SNS ARN 在 `neptune_etl_xray.py:619-620`；私有 IP `11.0.2.135` 等在 `neptune_etl_deepflow.py:159-160,446` 与 `neptune_etl_xray.py:207`（ClickHouse SQL 示例注释）。代码正文中**未发现**任何硬编码 IP 常量赋值。

---

## 6. 测试覆盖

`pytest --collect-only`：**549 项收集**（含参数化实例），`def test_` **351 个函数**，49 个测试文件（未真跑套件）。

| ETL | 专属单测 | 测试函数 | 代码规模 |
|---|---|--:|---|
| etl_deepflow | `tests/test_13_unit_etl_deepflow.py` | **4** | 30 函数 / 1,865 行 |
| etl_aws | `tests/test_12_unit_etl_aws.py` | 15 | 68 函数 / 3,863 行（另有 `test_08` 14 个、`test_11` 7 个） |
| etl_cfn | `tests/test_14_unit_etl_cfn.py` | 7 | 11 函数 / 459 行 |
| etl_xray | 无专属文件 | 30（`test_32` 4 + `test_33` 26） | 17 函数 / 861 行 |
| etl_trigger | 无专属文件 | 3（在 `test_14:289-379`） | 2 函数 / 111 行 |

### 完全无测试覆盖的函数

（函数名从未在 `tests/` 任何文件出现——按名引用的下界，非精确覆盖率）

**etl_deepflow（28 个顶层函数中 7 个）**：`get_aws_session`、`ch_query_json`、`_resolve_datastore_ips`（**~CC 40**）、`fetch_datastore_flows`、`upsert_datastore_flows`、`_get_eks_k8s_session`、`_first_scalar`

→ **整条 datastore-flow 摄取链路无测试。** 而这条链路正是 2026-08-29 为消除「微服务→存储」盲区新加的（盲区 13→10）。**最新、最复杂、CC 最高的代码路径，覆盖率为零。**

**etl_xray（17 中 5 个）**：`_load_k8s_alias`、`_key_lower_names`、`_src_names`、`_src_name_predicate`、**`deactivate_stale_xray_edges`**

→ 最后一个是**软删除逻辑本身没有测试**。

**etl_trigger**：`_extract_resource_id`（`:95`）未按名出现。

测试计划文档 `tests/plan/test-plan-v2-20260416.md:16` 自记 ETL Trigger「❌ 无」，但实际已在 `test_14` 补了 3 个 handler 测试。

---

## 7. 正式回答：ETL 是不是太复杂

**算复杂，但本质复杂与偶然复杂并存，而且本质那部分是自洽的、可辩护的。**

### 本质复杂（无法回避）

要把 AWS API、CloudFormation 模板、DeepFlow eBPF、X-Ray、CloudWatch NFM 五类异构源融进同一张 33 节点 / 26 边的图，「声明 vs 观测」的对账天然需要 `source` + `dependency_kind` + `drift_status` 这类消歧属性；`AccessesData` 被 4 个源写是问题域决定的，不是乱来。

Cartography 的设计原则说得很直白：**「每个 intel module 提供自己对图的视角」，鼓励多模块修改同一节点类型**。所以多源写同一类型本身不是缺陷——**没有显式区分 Simple Relationship Pattern 与 Composite Node Pattern 才是**。`Microservice.az` 那个多值缺陷，本质就是把 Composite 当 Simple 写了。

### 偶然复杂（可消除，四条）

1. `etl_aws/handler.py:62` 的 1,234 行未拆分编排（~CC 325）
2. Neptune 客户端 2 份字节级副本 + 1 份分叉实现
3. upsert 幂等惯用法在 4 个 ETL 里各手写一遍；ECR 解析、ARN 归名、时间窗计算重复散落
4. 「权威」profile 不被任何写入 ETL 读取，配置退化成 env 默认值加内联字典

**前三条是同一个根因的表现——缺一层由 schema 生成的公共载入 / 清理层。** 第四条是那个根因本身。
