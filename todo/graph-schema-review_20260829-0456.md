# 图数据库 schema 合理性核查

**日期**：2026-08-29 04:56 UTC
**触发**：准备为 X-Ray / AutoTracing 接入新增节点与边类型之前，先核查现有 schema 是否合理。
**规模**：867 节点 / 1341 边，31 节点类型 / 26 边类型 / 74 组合。

---

## 结论

schema 的**分层设计是合理的**（业务能力 → 微服务 → K8s 对象 → AWS 资源 → 网络/区域），
但有**两类实质缺陷**，其中第一类是静默的、会污染任何读属性的查询，**必须在接入新数据源前修掉**，
否则新 ETL 会复制同一个 bug。

| # | 问题 | 严重度 | 影响面 |
|---|---|---|---|
| ① | **属性多值累积**（SET 基数）导致查询笛卡尔扇出 | **高·静默** | 7 个节点类型；最坏 1 节点 → 8 行，单类型 31 节点 → 307 行 |
| ② | 同一对节点被多种边类型重复表达 | 中 | 12 组重复，共 ~107 对节点 |
| ③ | 度量字段用 `-1.0` 作哨兵 | 中·静默 | LambdaFunction `error_rate` 184/307 是 -1.0 |
| ④ | 命名/粒度分裂 | 低 | `managedBy` vs `managed_by`；Database/RDSInstance/RDSCluster 三层 |

---

## ① 属性多值累积（最重要）

### 现象

同一个节点的同一个属性存了**多个值**。读属性时 Neptune 按值组合**扇出成多行**：

```
MATCH (n:LambdaFunction) RETURN labels(n)[0], count(*)        → 31    （不碰属性）
MATCH (n:LambdaFunction) RETURN count(*), n.error_rate, ...   → 307   （碰 3+ 个属性）
```

单节点精确验证：

```
MATCH (n:LambdaFunction {name:'neptune-etl-from-cfn'})
RETURN count(*), count(DISTINCT id(n)),
       count(DISTINCT n.managedBy), count(DISTINCT n.avg_duration_ms),
       count(DISTINCT n.last_updated)
```
| fanout_rows | real_nodes | distinct_managedBy | distinct_avg_ms | distinct_last_updated |
|---|---|---|---|---|
| **8** | **1** | 2 | 2 | 2 |

**1 个真节点，8 行输出** —— 2×2×2 的笛卡尔积。

### 一处必须更正的初判

我最初看到 `neptune-easy-etl-from-cfn` 出现 3 次、`managedBy` 分别是 `cloudformation` 和 `cdk`，
判断为「重复节点」。**这是错的**：同名重复节点查询返回**空**，全图没有重复节点。
真相是单节点属性多值造成的扇出——比重复节点更隐蔽，因为节点数看起来完全正常。

### 影响面

> **2026-08-29 05:11 更正**：下表用 `last_updated` 作探针，**低估了两个数量级**。
> 真正最严重的属性是 `last_scanned`（只有 11 个节点有它，所以用 `last_updated` 探针完全测不到）：
>
> | 探针属性 | 真节点 | 扇出行 | 倍数 |
> |---|---|---|---|
> | `last_updated` | 31 | 43 | 1.4× |
> | **`last_scanned`** | **9** | **1,253** | **139×** |
>
> 单节点最多 **172 个不同的 `last_scanned` 值**（LambdaFunction 平均 139、DynamoDBTable 172、StepFunction 166）。
> 172 ≈ etl_cfn 的运行次数——**该属性每轮新增一个值，无界增长**。
>
> **教训**：量化多值扇出时，探针属性要选「多值最严重的那个」，不是「覆盖节点最多的那个」。
> 用错探针会得出一个看似温和的数字，掩盖两个数量级的问题。

以 `last_updated` 单个属性衡量（**已知低估**，保留原始记录）：

| 节点类型 | 真节点 | 扇出行 | 多出 |
|---|---|---|---|
| LambdaFunction | 31 | 43 | +12 |
| S3Bucket | 33 | 34 | +1 |
| SQSQueue | 7 | 8 | +1 |
| SNSTopic | 5 | 6 | +1 |
| DynamoDBTable | 4 | 5 | +1 |
| RDSCluster | 3 | 4 | +1 |
| StepFunction | 2 | 3 | +1 |

`Microservice` 也受影响：15 节点 → 17 行（`az` 有 2 个值）。

### 根因

受影响的 7 个类型**恰好是同时被两个 ETL 写的那批 AWS 资源**：
`etl_aws`（Describe APIs，写运行时属性）与 `etl_cfn`（CloudFormation，写 `declared_in`/`stack_name`）。
两边用 SET 基数写同一属性，**值累积而不替换**。

> **2026-08-29 05:11 更正：上面这段根因判断基本是错的。** 代码逐点核查后：
>
> | ETL | 顶点写法 | 是否累积 |
> |---|---|---|
> | `etl_aws/neptune_client.py:upsert_vertex` | `mergeV` option-map（SET）**但尾部有 `.property(single,…)` 全量重写兜底** | **否，自愈** |
> | `etl_aws/cloudwatch.py:update_lambda_metrics` 等 | `.property(single, k, v)` | 否 |
> | **`etl_cfn/neptune_etl_cfn.py:get_or_create_vertex`（72–94）** | `.option(Merge.onMatch, ['stack_name':…, 'last_scanned': ts])`，**无尾部 single** | **是** |
> | `etl_deepflow:batch_upsert_nodes`（≈620–665） | 同样 option-map 无尾部 single | 潜在，但 onMatch 的值稳定（ip/namespace/env），SET 去重压住了 |
> | `etl_deepflow:batch_fetch_dependency_and_update` | `.property(single,…)` | 否 |
>
> **真实根因是两条：**
> 1. **当前唯一还在流血的**：`etl_cfn` 的 option-map 用默认 SET 基数，而 `last_scanned` 是每轮变化的时间戳
>    → 每次运行新增一个 distinct 值，**无界增长**（已达 172）。
> 2. **绝大部分多值是历史脏数据**，由加入 `property(single)` 修复**之前**的旧版 ETL 写入。
>    **铁证**：`avg_duration_ms` 在当前代码树里 **grep 零命中（无任何 Python 写入者）**，却是多值的
>    ——只可能来自旧部署。
>
> 两个 ETL 在共享节点上写的其实是**互斥的属性集**：etl_aws 写 `managedBy`/`last_updated`/指标，
> etl_cfn 只写 `stack_name`/`source`/`created_at`/`last_scanned`。**它们不争写同一个属性。**
>
> **`managedBy` vs `managed_by` 也不是两个 ETL 的分裂**，而是 etl_aws 内部的**改名断层**：
> collector 返回的字典键是 `managed_by`，handler 把它当**函数实参**传入，
> `upsert_vertex` 内部写成驼峰 `managedBy`。图里那 125 个蛇形 `managed_by` 是
> **旧属性名的残留，当前代码不再写它**。

### 一个不能照搬的"正确模板"

`Calls`(18) / `DependsOn`(21) / `AccessesData`(30) 三种边零扇出，
**但原因不是 `etl_deepflow` 用了正确的单值语义**——而是 **Neptune/TinkerPop 的边属性天生就是单基数**，
边根本不支持多值。所以 `batch_upsert_edges` 里那些不带 `single` 的 `.property('calls', …)` 是安全的，
但**把它的写法照搬到顶点仍会累积**。

顶点的正确参照模板是 `etl_deepflow:batch_fetch_dependency_and_update`（≈1058–1069）
与 `etl_aws/cloudwatch.py:update_lambda_metrics`（302）那种显式 `.property(single, k, v)`。

**规范：所有顶点标量属性一律 `.property(Cardinality.single, k, v)`，不要依赖 `mergeV` option-map。**

### 仓库里已有一半的解药

`infra/fix_neptune_data.py` 的 **Fix 3 `fix_last_updated_cardinality()`** 已经在做
「取 max → drop → single 重写」，说明这是**已知的历史遗留**，只是覆盖面不够
（只治 `last_updated`，没治 `last_scanned` / `avg_duration_ms` / `managedBy` / `az`）。

### 存量规约的自指问题：有干净答案

我原先担心「`last_updated` 自己就是多值的，怎么用它挑其它属性的当前值」。答案是：

- **时间戳类属性无自指死锁**：`last_updated` / `last_scanned` 是数值，直接对**它自己的值列表取 max**。
- **非时间戳标量无法按时间挑选**：Neptune 的多值属性**不为每个 value 携带逐值时间戳**，
  值级 provenance 已经丢了，没法把某个 `managedBy` 值关联到某个 `last_updated` 值。只能：
  1. **drop 全部 → 让已改成 single 的权威 ETL 下一轮重新填充**（etl_aws 现在就是 single，重跑即修复）；或
  2. 按固定优先级确定性选一个，drop 其余。

**规约顺序**：① 直接 drop 无写入者的死属性 `avg_duration_ms`；
② `last_updated` / `last_scanned` 取 max 塌缩；
③ 其余标量 drop-all，触发 etl_aws / etl_deepflow 重跑用 single 填充；
④ 把蛇形 `managed_by` 的值并入 `managedBy` 后整属性 drop。


这与已修的 `causal_weight`「全历史单调累积」是同一个错误形态，
也是「两套实现掩盖同一缺陷」（Pattern C）叠加「静默失败」（Pattern B）。

### 为什么危险

1. **任何读这些属性的查询都返回膨胀的行数**，且行数取决于历史写入次数——随时间恶化。
2. **聚合结果错误**：`avg(n.error_rate)` 是在历史累积值上求平均，不是当前值。
3. **「哪个值是当前的」无定义**。`n.last_updated` 本该用来判新旧，它自己就是多值的。
4. **schema 自我暴露了这一点**：`get_graph_schema` 把这些属性推断为 union 类型
   （`LambdaFunction.error_rate: ["DOUBLE","LIST"]`、`managedBy: ["LIST","STRING"]`、
   `Microservice.az: ["LIST","STRING"]`、`S3Bucket.last_updated: ["LIST","INTEGER"]` 等），
   但**声明式 schema（`profiles/petsite.yaml`）里没有任何地方说这些属性是多值的**。

### 修法

写入侧改为**单值语义**：Gremlin 用 `property(Cardinality.single, k, v)`，
openCypher 用 `SET n.k = v`（注意这需要 `neptune-db:DeleteDataViaQuery`，
Fix B 已加过该权限——Neptune 授予的是一条查询「可能执行的动作」的并集）。
存量数据需要一次规约（每属性保留 `last_updated` 最大的那个值）。

**这必须先做**：X-Ray ETL 会写 `runtime_verified` / `drift_status` / `last_updated`，
而 `AccessesData` 已有 23 条边带这些属性——按现状写入会直接产生多值。

---

## ② 同一对节点被多种边类型重复表达

实测 12 组（同一有向节点对上存在 ≥2 种边类型）：

| 源 → 目标 | 边类型 | 节点对数 | 判断 |
|---|---|---|---|
| K8sService → Pod | `RunsOn` + `Routes` | 26 + 8 = 34 | **应只保留 `Routes`**：Pod 不"运行在" Service 上 |
| Incident → Microservice | `TriggeredBy` + `AffectedService` | 17 + 11 = 28 | 语义确实不同（根因 vs 受影响），但**方向可疑**，见下 |
| Deployment → Pod | `Manages` + `RunsOn` | 13 + 13 = 26 | **应只保留 `Manages`**：Pod 不"运行在" Deployment 上 |
| Incident → Microservice | `Involves` + `MentionsResource` | 6 + 2 = 8 | 两者都是"提及"，**语义重叠** |
| Incident → Microservice | `TriggeredBy` + `MentionsResource` | 5 + 3 = 8 | 同上 |
| LambdaFunction → LambdaFunction | `AccessesData` + `Invokes` | 2 | **`Invokes` 才对**：调用不是访问数据 |
| LoadBalancer → TargetGroup | `ForwardsTo` + `RoutesTo` | 1 | **纯重复**，两个词一个意思 |

### 三个具体的建模问题

**（a）`RunsOn` 跨了抽象层。** 它同时表达
`Microservice→Pod`、`Namespace→Pod`、`K8sService→Pod`、`Deployment→Pod`、`Pod→EC2Instance`。
只有最后一条是真正的"运行在"。`Namespace RunsOn Pod` 语义是反的——Pod 运行**在** Namespace 里。
`RunsOn` 有 **221 条边**，是第二大边类型，这个歧义影响所有拓扑遍历。

**（b）Incident→Microservice 有四种边。**
`AffectedService`(28) / `TriggeredBy`(127) / `MentionsResource`(16) / `Involves`(8)。
其中 `TriggeredBy` 方向反了：`Incident TriggeredBy Microservice` 读作"事故被服务触发"尚可，
但它有 127 条（≈每个 Incident 1 条），实际承担的是"这个事故属于哪个服务"，
和 `AffectedService` 职责重叠。而 `Involves` 只有 8 条覆盖 141 个 Incident
（已知：全部指向 `petsearch`）——它是因果先验的分母来源，稀疏到这个程度值得单独复核。

**四条边可以收敛为一条带 `role` 属性的边**（`role: root_cause | affected | mentioned`），
但这是破坏性变更，需要同步改所有读方。

**（c）容器关系的方向约定不一致。**
`Contains`（Region→AZ，向下）与 `LocatedIn`（VPC/EKSCluster/S3Bucket→Region，向上）
表达同一种包含关系但方向相反；`BelongsTo`（Namespace→EKSCluster）与
`OwnedBy`（Microservice→Namespace）又是两个词表达同一种归属。
四个词、两种方向，做多跳遍历时必须逐个记住，容易漏。

---

## ③ 度量字段用 `-1.0` 作哨兵

```
MATCH (n:LambdaFunction) RETURN count(*), sum(CASE WHEN n.error_rate = -1.0 THEN 1 ELSE 0 END)
```
`error_rate = -1.0`：**184 / 307**；`avg_duration_ms = -1.0`：33。

`-1.0` 表示"无数据"，但它是个**合法的数值**：
- `WHERE error_rate < 0.1` 会把"无数据"判为"非常健康"
- `avg(error_rate)` 被 -1 拉低
- 排序时 -1 排在最前

正确做法是**不写该属性**（`IS NULL` 才是"无数据"的正确表达），
或改用明确的 `*_available: false` 标记。这与已修的 resilience 分数问题同源——
那次是读方查了 4 个不存在的属性名而拿到 fallback 默认值。

---

## ④ 命名与粒度分裂

**（a）`managedBy` vs `managed_by` 并存。**
`LambdaFunction` 同时有两个：`managedBy` 307 个有值、`managed_by` 125 个有值。
和已修的 `resilience_score` / `chaos_resilience_score` 分裂完全同型。

**（b）关系型数据库有三个节点类型。**
`Database`(1) / `RDSInstance`(4) / `RDSCluster`(3)。
`Database BelongsTo RDSCluster` 且 `RDSInstance BelongsTo RDSCluster`，
同时 `Microservice ConnectsTo Database`(1 条) 与 `Microservice AccessesData RDSCluster`。
**同一个"服务依赖数据库"的事实，被两种边、两种粒度表达。**
`ConnectsTo` 只有 1 条边、`Database` 只有 1 个节点——基本是死类型。

**（c）几乎死掉的类型。**
`ProtectsAccess`(1) / `ConnectsTo`(1) / `InvokesVia`(1) / `PublishesTo`(2) / `Contains`(3)。
这些不一定是错的（`Contains` 只有 3 个 AZ 是合理的），但 `ConnectsTo` 与 `InvokesVia`
各有更主流的替代边（`AccessesData`），属于该合并的。

**（d）`Microservice` 有 36 个属性。** 混了身份（`name`/`tier`/`namespace`）、
拓扑（`upstream_count`/`downstream_count`）、指标快照（`p50`/`p99`/`rps`/`error_rate`）、
混沌结果（`resilience_score`/`chaos_test_count`/`last_chaos_test`）、
网络限流（`nfm_*`）。指标快照放节点属性上是有意的设计（图谱不做时序），
但**指标与身份混在一个节点上，意味着每次指标刷新都在改拓扑节点**——
这正是 ① 多值累积的温床。

---

## 与本次 Traces 接入计划的关系

按严重度和阻塞关系，建议把计划调整为：

| 原计划 | 调整 |
|---|---|
| Stage 1 数据面勘察 | 不变 |
| — | **新增 Stage：修 ① 属性多值**（写入侧改单值 + 存量规约）。X-Ray ETL 会写 `runtime_verified`/`drift_status`/`last_updated`，不先修就会新增多值 |
| Stage 2 schema 声明 | 同时把 ①③④ 的约定写进 `profiles/petsite.yaml`（单值语义、禁用数值哨兵、`managedBy` 单一拼写） |
| Stage 3–6 P0/P1/P2 | 不变，但新 ETL 必须遵守新约定 |
| Stage 7 更新系统描述 | 把本核查结论并入 |
| Stage 8 全量验证 | 增加一条守门测试：**任何节点类型的属性扇出行数必须等于真节点数** |

②④ 是破坏性变更（要改所有读方），**不建议塞进本批**——应单独立卡，
先补一条 schema 守门测试防止继续恶化，再分批收敛。

---

## 方法论记录

本次又验证了一条已固化的教训，而且是**反向**验证的：
我先按「同名节点出现 3 次」判为重复节点，**判错了**——同名重复查询返回空。
正确的判定要用 `count(DISTINCT id(n))` 对照 `count(*)`。

**在 Neptune 上判断"有几个节点"，不能看返回行数**：
只要碰到多值属性，行数就是属性值的笛卡尔积。这也解释了为什么这个缺陷能存活至今——
节点总数（867）一直是对的，只有读属性时才暴露。


---

## 附:一个诚实标注的未解问题（2026-08-29 06:30）

规约后 6 个 CFN 类型的 `last_updated` **再次出现 2 个值**（相差 25 秒）。
我没能定位到写入者，把已排除的可能性和已证实的事实都记下来，
避免下一个人重走这条路。

### 已证实（实测，非推断）

| 事实 | 证据 |
|---|---|
| `property(single,k,v)` 在本集群行为正确 | 活图谱实验：带 single 连写 3 次仍 1 个值；不带 single 写 1 次即 2 个值 |
| `etl_aws.upsert_vertex` 正确 | 真实调用 3 次（含改值），`last_updated`/`managedBy`/`region`/`source` 全部保持 1 个值 |
| 生产 `etl_aws.neptune_client.py` 与分支**逐行一致** | 下载生产包对 `upsert_vertex` 做 diff，无差异 |
| 生产 `etl_cfn` 仍是旧代码 | onMatch map 里 `last_scanned` 每轮 SET-add，**这是 `last_scanned` 再生的确证来源**（修在分支，未部署）|
| 四个 ETL 部署包里所有不带 single 的 `last_updated` 写入**都是边写入** | 逐个看上下文：`addE` / `inE` / `g.E()` / `upsert_edge` |

### 已排除

- 不是 `property(single)` 失效
- 不是 `etl_aws.upsert_vertex`
- 不是生产与分支的代码差异（该函数无差异）
- 不是边写入误伤顶点（Neptune 边属性天生单值）

### 仍未解释

**谁在顶点上写不带 single 的 `last_updated`。** 两个值相差 25 秒，
而 etl_aws 每 15 分钟一轮 —— 说明是同一轮内的两次写入，或另有写入者。

下一步该怎么查（不要再走代码检查这条路，已走到尽头）：
1. 把 `last_updated` 规约为单值，记下时刻 T
2. 每 30 秒轮询这 6 个节点的投影行数，记录**第一次**变成 2 行的时刻
3. 用该时刻对照四个 Lambda 的 CloudWatch 调用日志，锁定是哪个函数的哪次调用
4. 拿到函数后再看它那一轮的具体执行路径

### 这如何影响了测试设计

原先 L-01 直接断言活图谱无扇出，于是**因生产落后而红**。这是错的测试设计：
让仓库测试因未部署代码而失败，会训练出「忽略这条失败」的习惯，
反而掩盖真正的写入侧回归。

已改为：
- **L-00（阻塞）**：真 Neptune 上验证单值写入语义成立，且最后写入的值胜出
- **L-02（阻塞）**：用桩捕获 ETL 发出的 Gremlin，断言刷新字段走 `property(single,…)`
- **L-01（只告警）**：报告活图谱现状，用 `warnings.warn` 而非 assert
  —— 本仓库已有同型先例（`test_26` 对未向量化的 Incident 也只告警）

顺带修掉一个顺序依赖缺陷：原 L-00 直接 import `etl_aws.neptune_client`，
单独跑时因 conftest 的 `config` 桩缺 `FAULT_BOUNDARY_MAP` 而 **skip**，
与别的测试同跑时才 import 成功并**失败** —— skip 掩盖了真实错误。
改为验证写入语义本身后，单跑与同跑结果一致。
