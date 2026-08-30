# 图谱依赖关系正确性：现状实测与缺口

**日期**：2026-08-30 14:35 UTC
**范围**：`/home/ec2-user/works/graph-dependency-platform`，严格只读实测，所有断言带 `文件:行号`
**写入 Neptune 的路径**：`etl_aws`、`etl_deepflow`、`etl_xray`、`etl_cfn` 四个 ETL

---

## 摘要判定表

| 机制 | 判定 | 一句话 |
|---|---|---|
| Schema 运行时强制 | **不存在** | 那份「权威 schema」的真实用途是喂 LLM prompt，四个 ETL 无一 import |
| Provenance 溯源 | **部分实现** | `source` 是唯一全覆盖字段；`observed_by`/`confidence`/`scope` 从不写入；时间戳字段名三家不统一 |
| 软删除 / reconciliation | **部分实现，覆盖破碎** | 只有 Calls 与 xray 边会失效；AWS 是无阈值硬删且不删任何边；CFN 完全不清理 |
| 多源冲突裁决 | **部分实现且自相矛盾** | 三个源保护 `source`，AWS 通用边与 CFN 无条件覆盖 |
| 漂移检测 | **已实现（边实例级）** | 但「declared」不是 schema 声明，是图中已有的 static 边 |
| 守门测试 | **部分实现** | 有 schema 一致性与属性单基数；缺写时拒绝、跨类型身份键、多源优先级 |
| 身份键 | **部分实现** | 结构上不拼 `~id`（对的），但默认用可变的 `name`，只有 EC2 修好 |

---

## 1. Schema 强制 —— 不存在

`profiles/petsite.yaml:173` 起的 `neptune.graph_schema_text` **不是结构化字段，而是一个自由文本块**（节点 33 种在 `:175`，边 26 种在 `:266`），用途是注入 LLM prompt。

- `/home/ec2-user/works/graph-dependency-platform/profiles/schema.py:94` 把它定义为 `Optional[str]`（不解析的字符串）
- `/home/ec2-user/works/graph-dependency-platform/profiles/schema.py:145` `model_config = {"extra": "allow"}` 显式允许未知字段
- 唯一校验入口 `profiles/schema.py:150` `validate_profile()` 只用 Pydantic 校验 profile 结构，**不涉及节点/边类型**
- `profiles/` 下无 `is_valid_node_type` / `valid_edge` / 白名单函数（grep 零命中）

**四个 ETL 无一 import profiles**：grep `from profiles|import profiles` 在 `infra/lambda/` 下只命中 `rca_window_flush`。写标签是字符串字面量直拼 Gremlin，无门禁：

- `/home/ec2-user/works/graph-dependency-platform/infra/lambda/etl_aws/neptune_client.py:56` `upsert_vertex`、`:155` `upsert_edge` 对传入 label 不校验
- `/home/ec2-user/works/graph-dependency-platform/infra/lambda/etl_cfn/neptune_etl_cfn.py:126-148` `addE('{rt}')` 直写
- `etl_aws/neptune_client.py:149` 只有一句注释 `# 规范定义见 profiles/petsite.yaml`，代码从不加载该 YAML

唯一的「声明 ⊆ 活图」类型比对在**测试期**：`tests/test_24_live_schema_consistency.py:105/115`、`tests/test_11_schema_consistency.py:89/140`、`tests/test_20_integration_schema.py:139/179`。

**后果**：任何拼错的或新造的节点/边标签都会被静默写入 Neptune，生产上没有任何东西会拦。这同时是 ETL 复杂度与维护困难的根因——每个 ETL 都得自己重新表达一遍图的形状。

---

## 2. Provenance 溯源属性 —— 部分实现，四源覆盖不均

| 元素 | 写入的溯源属性 | 证据 |
|---|---|---|
| AWS 节点 | `source='aws-etl'`, `last_updated`, `environment`, `fault_boundary`(条件), `region`(条件) | `etl_aws/neptune_client.py:88-94,122,129` |
| AWS 边（通用） | `source='aws-etl'`, `last_updated`；`dependency_kind='static'` 仅 Calls/DependsOn/AccessesData | `etl_aws/neptune_client.py:152,159,161,162-166` |
| AWS NFM 边 | `source='nfm'`, `dependency_kind='dynamic'`, `first_seen`, `active`, `last_seen` + `nfm_*` | `etl_aws/cloudwatch.py:564-568,591-595` |
| DeepFlow Microservice 节点 | `source='deepflow'`（仅创建）, `region`, `metrics_updated_at` | `etl_deepflow/neptune_etl_deepflow.py:1128-1130,1560` |
| DeepFlow Calls 边 | `source='deepflow-etl'`, `dependency_kind='dynamic'`, `first_seen`, `last_seen`, `active`, `call_type` | `neptune_etl_deepflow.py:1193-1209` |
| DeepFlow AccessesData(L4) | `source='deepflow-l4'`, `dependency_kind`, `first_seen`, `active`, `last_seen` + `l4_*` | `neptune_etl_deepflow.py:571-577` |
| DeepFlow drift 影子边 | `source='deepflow-dns'|'xray'`, `runtime_verified`, `drift_status`, `verified_by`, `last_drift_check` | `neptune_etl_deepflow.py:862-874` |
| X-Ray 节点 | `granularity='service'`, `xray_type`, `xray_aliases`, `last_seen` | `etl_xray/neptune_etl_xray.py:543-546` |
| X-Ray 新建边 | `source='xray'`, `dependency_kind='dynamic'`, `first_seen`, `active`, `last_seen` | `etl_xray/neptune_etl_xray.py:735-741` |
| CFN 节点 | `source='cfn-etl'`, `stack_name`, `last_scanned`, `created_at`（仅创建） | `etl_cfn/neptune_etl_cfn.py:98-103` |
| CFN 边 | `declared_in='cfn'`, `source='cfn-etl'`, `stack_name`, `evidence`, `last_scanned` | `etl_cfn/neptune_etl_cfn.py:131-146` |

### 三个关键缺失

1. **`observed_by` / `confidence` / `scope` 在全项目从不作为属性写入**（跨四源 grep 零命中）。`observed_by_xray` 更是被 `tests/test_33_xray_parallel_source.py:400` 明令禁止写入。
   > 注：此前多次讨论过的 `nfm_scope='vpc'` 显式标注**只是提案，代码里不存在**。
2. **`granularity` 只在 X-Ray 节点上**（`neptune_etl_xray.py:543`），**边上没有**。
3. **时间戳字段名三家不统一**：`last_updated`（aws / deepflow-DependsOn）、`last_scanned`（cfn）、`last_seen`（deepflow Calls/L4 / xray）。均非 `profiles/petsite.yaml:280-281` 声明的统一 `last_seen`/`first_seen`。`declared_in`/`evidence` 则是 schema 未声明的额外字段。

   **后果**：跨源查询「这条边最后何时被看到」没有统一字段可用。

`active`/`first_seen`/`last_seen` 只在**动态边**上（Calls、L4-AccessesData、X-Ray 边、NFM 边）；AWS 结构节点与边、CFN 边、DeepFlow Microservice 节点均不带。

---

## 3. 生命周期 / 软删除 / reconciliation —— 覆盖破碎

| 源 | 机制 | 判据 | 作用域 | 证据 |
|---|---|---|---|---|
| **DeepFlow** | 软删除 `active=false`（硬删默认关） | `last_seen < round_ts − 1800s`（约 6 轮容错） | **仅 `Calls` 边** | `neptune_etl_deepflow.py:1381-1389`；阈值 `:1246`；硬删 `:1418-1423`，`DROP_ENABLED` 默认 false `:1252` |
| **X-Ray** | 软删除 `active=false` | `source='xray'` 且 `xray_last_seen < cutoff`（默认 6h） | **仅 `source='xray'` 边** | `etl_xray/neptune_etl_xray.py:789-807`；判据 `:797-799`；阈值 `:100` |
| **AWS** | **硬删除 `.drop()`** | 本轮 Describe 未枚举到（**无时间阈值、无软删除标记**） | **仅约 14 种节点；不含任何边** | `etl_aws/graph_gc.py:39-45` |
| **CFN** | **无** | — | — | `etl_cfn/neptune_etl_cfn.py` 全文无 `stale`/`deactivate`/`active`；只刷 `last_scanned:146` 且从不读取 |

### 未受管的空洞

- **`AccessesData`、`DependsOn` 边写了 `active=true` / `last_seen`，却无任何路径把 `active` 翻回 false**（deepflow 只 reconcile `Calls`）
- NFM 边（`cloudwatch.py`）同样写 `active` 但无失效路径
- CFN 声明的边被从模板删除后**永久残留**
- AWS 只硬删节点、不删边

判据还各不相同：DeepFlow 是 1800s 时间阈值，X-Ray 是 6h，AWS 是「本轮未观测立即硬删」。同一张图里三套语义。

---

## 4. 多源冲突 —— 自相矛盾

### 保护 `source` 的三个源

- **X-Ray**：`etl_xray/neptune_etl_xray.py:719-725` 已存在边分支只补度量 + `active`/`last_seen`，**刻意不写** `source`/`dependency_kind`/`first_seen`（注释在 `:676-677,24`）
- **DeepFlow-L4 印证**：`neptune_etl_deepflow.py:565-568` 不写 source/dependency_kind；Calls 边 `first_seen` 用 `coalesce(values('first_seen'), constant(ts))` 保留（`:1193`）
- **AWS NFM 边**：`etl_aws/cloudwatch.py:514,584-595` 已存在则只补度量，绝不覆盖 source/dependency_kind

### 覆盖 `source` 的两个源

- **AWS 通用边**：`etl_aws/neptune_client.py:172-179` 用 `coalesce(__.inE(lb).where(outV=src), addE)` 按 `(src, label, dst)` 匹配，**命中后 `:161` 的 `.property('source','aws-etl')` 无条件执行**——若 deepflow/xray 先写了同一条边，`source` 被改成 `aws-etl`。节点侧 `mergeV().option(Merge.onMatch, ...)` 用 `property(single, ...)` 亦为替换（`:112,130-135`）。**无优先级判断。**
- **CFN 边**：`etl_cfn/neptune_etl_cfn.py:137-146` coalesce 命中后 `:143` 无条件 `.property('source','cfn-etl')`。

**结论**：同一实体多源写入时，溯源是否被保护**取决于哪个 ETL 后写**。全项目没有统一的优先级/权威源仲裁器，只有各 ETL 各自的纪律。`active`/`last_seen` 各处普遍后写覆盖。

跨 ETL 的隔离靠命名空间约定（`chaos_` 前缀，见 `neptune_etl_deepflow.py:8-27` 文件头）。

---

## 5. 漂移检测 —— 已实现，但语义需澄清

写入：`etl_deepflow/neptune_etl_deepflow.py:724-888` `run_drift_detection()`

- 把「声明的依赖」（Neptune 中已有的 `static` 边）与「观测到的依赖」（DeepFlow DNS + X-Ray，OR 关系）比对
- 三态：`ok`（`:823-826`）、`declared_not_observed`（`:843-847`）、`observed_not_declared`（写影子边 `:872`）
- 字段：`drift_status`、`runtime_verified`、`verified_by`（dns / xray / dns+xray / none）、`last_drift_check`
- 作用域：`AccessesData` / `PublishesTo` / `InvokesVia` / `ConsumesFrom`（查询 `:801-808`），**不含 `Calls`**

读取方：`/home/ec2-user/works/graph-dependency-platform/rca/neptune/neptune_queries.py:355` `q20_dependency_verification()`（症状归类 `:411-437`）；catalog 登记 `rca/neptune/query_catalog.py:207`。schema 侧字段声明在 `profiles/petsite.yaml:290-292`。

### 必须澄清的语义

这里的「declared」= **已写入 Neptune 的 `static` 边**（由 AWS/CFN ETL 从资源配置与模板产生），**不是 `petsite.yaml` 的类型声明**。

「petsite.yaml 声明 ↔ Neptune 观测」的比对只在**类型层、测试期**存在（`tests/test_24_live_schema_consistency.py:105/115`），运行时不存在。若外部专家把它理解成「声明式 schema 与实测的对账」，实际对不上。

---

## 6. 身份键 —— 结构对了，字段选错了

### 好的部分

**全四源都不手工拼接 `~id`**，而是 `mergeV([label, <身份属性>])` 让 Neptune 自动分配内部 id：

- `etl_aws/neptune_client.py:131`
- `etl_deepflow/neptune_etl_deepflow.py:1143`
- `etl_cfn/neptune_etl_cfn.py:97`
- X-Ray 按 `name` 属性匹配 `etl_xray/neptune_etl_xray.py:540`

`state` / `ip` **不进身份键**（是普通属性：`handler.py:130/133`；DeepFlow `ip` 是 property 不是 key `neptune_etl_deepflow.py:1005-1010`）。

### 缺陷

**默认身份键 = `name`，而 `name` 对多数类型可变**（AWS 取自 Name tag）。`etl_aws/neptune_client.py:101-108` 选身份属性，默认 `id_key='name'`。

**只有 EC2 用不可变的 `instance_id`**（`etl_aws/handler.py:145` 传 `identity_prop='instance_id'`）。其余 30 多种类型（LoadBalancer / Lambda / DynamoDB / SQS / StepFunction…）仍以可变 `name` 为身份键。

**该缺陷已被代码自证**——`etl_aws/neptune_client.py:62-77` 注释直陈：

> 标签一变 mergeV 匹配不到旧节点就新建……实测 14 个 EC2Instance 里 4 个是重复实体（2026-08-29）

较稳的两源：CFN 的 `name` 来自稳定的 `physical_id`（`etl_cfn:331-338`）；X-Ray 的 `type_for_identity` 恒为 None（`neptune_etl_xray.py:405-425`）。

边身份均为 `(源顶点, 边label, 目标顶点)` 三元组，无显式边 id。

---

## 7. 守门测试

### 已实现

| 类别 | 位置 |
|---|---|
| Schema 一致性（强） | `tests/test_11_schema_consistency.py:89/140/231/283`、`test_20_integration_schema.py:139/179`、`test_24_live_schema_consistency.py:105/115/144/167/177` |
| 身份键不可变（**仅 EC2**） | `tests/test_34_ec2_identity_and_nfm.py:22`（`test_n01`）、`:48`（`test_n02` 可变字段进 onMatch 不进身份键） |
| 重复实体检测（**仅 EC2 且仅告警**） | `test_34_ec2_identity_and_nfm.py` 约 `:164`（`test_n06`） |
| 别名归一去重 | `test_33_xray_parallel_source.py:55/127` |
| 溯源保护 | `test_33_xray_parallel_source.py:355`（`test_x07` 已存在边不得丢失 source/dependency_kind） |
| 生命周期 | `test_27_topology_change_log.py:100`（reconcile 过滤 `active=true`）、`:137`（保留期）；节点 GC `test_12_unit_etl_aws.py:864`（**断言弱**，只验返回非负 int） |
| 漂移 | `test_32_xray_drift_source.py:50/91/123/167`、`test_33:435` |
| 属性有读取方（个案） | `test_32:167`(Q20)、`test_27:159/186`(Q19)、`test_33:419/435`(Q21)、`test_28`(causal prior) |
| **属性单基数**（防多值累积） | `tests/test_31_property_cardinality.py:40/100/168/203` |

> 最后一条是针对 `Microservice.az` 曾累积成两个值（`neptune_etl_deepflow.py:1097` 注释记录）的守门测试，**已经存在**。

### 实际缺失

1. **无「写入时拒绝未声明类型」的测试**——全部是事后子集断言，无写时强制
2. **无「每条边/节点都带 `source`/统一时间戳」的存在性守门**（`source` 仅个别写入点断言：`test_12:173/478`、`test_31:203`）
3. **无枚举全部写入属性、逐一验证有读取方的通用守门**（靠 Q19/Q20/Q21 个案人肉覆盖）
4. **无「stale 边被置 `active=false`」的直接行为断言**（`test_c01` 只验过滤条件含 `active=true`）
5. **无「两个 ETL 写同一条边 → 合并/优先级」的通用断言**（只有 X-Ray vs 已存在边这一对，加 `tier` 标量优先级 `test_24:144/177`）
6. **无跨 30+ 节点类型的通用「可变字段不得进身份键」/去重断言**（仅 EC2 专门覆盖，且重复检测只告警）
7. **无对写入 `drift_status` 字面值的断言**（只验行为与查询形态）

---

## 缺口清单 —— 按「外部专家最容易问倒的顺序」

**1. `petsite.yaml` 被宣称为权威 schema，但运行时四个 ETL 根本不 import 它、不做任何类型校验。**
任何拼错或新造的标签都会被静默写入（`etl_aws/neptune_client.py:56/155`、`etl_cfn/neptune_etl_cfn.py:126`）。「权威声明」与运行时现实脱节。

**2. 身份键默认用可变的 `name`，只有 EC2 一种类型修好了。**
代码自己记录了「4/14 EC2 是重复实体」（`neptune_client.py:62-77`），而 30 多种其它类型仍走 `name`。被问「除了 EC2，你怎么保证 Lambda/LB/DynamoDB 改个 tag 不产生重复节点」——答案是保证不了。

**3. 多源冲突无统一权威源，且行为自相矛盾。**
X-Ray / DeepFlow-L4 / NFM 刻意保护 `source`，而 AWS 通用边（`neptune_client.py:161`）和 CFN（`neptune_etl_cfn.py:143`）**无条件覆盖**——同一条边的首次发现者可能被后一轮 ETL 静默抹掉。没有优先级模型能解释「谁赢」。

**4. 软删除 / reconciliation 覆盖破碎。**
只有 `Calls` 与 `xray` 边会失效；`AccessesData`/`DependsOn` 写了 `active` 却永不翻回 false；CFN 边完全不清理；AWS 只硬删约 14 种节点、不删任何边。图里会永久累积 ghost 边，且判据不一（本轮未观测 / 30min / 6h）。

**5. 溯源属性不完整且命名漂移。**
`observed_by`/`confidence`/`scope` 从不写入；`active`/`first_seen`/`last_seen` 只在部分边上；时间戳三家分别叫 `last_updated`/`last_scanned`/`last_seen`——跨源查询「最后何时被看到」没有统一字段。

**6. 漂移检测的「declared」并非 `petsite.yaml`，而是图中已有的 static 边**（`neptune_etl_deepflow.py:801-808`），且只覆盖四种边类型、不含 `Calls`。

**7. 守门测试留有系统性空洞**——上述 1–6 的缺陷大多没有测试会在回归时报警。
