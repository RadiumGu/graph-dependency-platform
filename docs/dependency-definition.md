# 依赖的定义

本项目对「依赖」的定义由**五个正交维度 + 一层生命周期**构成。五个维度回答五个不同的问题，任何一个都不能替代另一个：

| 维度 | 回答的问题 | 落盘属性 |
|---|---|---|
| ① 语义 | 这条边算不算「依赖」？ | 边类型的 `dependency` 标记（契约级，不落在边上） |
| ② 来源 | 这条依赖是**声明**的还是**观测**到的？ | `dependency_kind` |
| ③ 存在性 | 它**是不是真的**？ | `verify_status` + `verify_confidence` |
| ④ 强度 | 它**有多要紧**？ | `verify_dependency_class` |
| ⑤ 范围 | 它**属于被观测系统吗**？ | 节点的 `scope`（边的 scope 由两端派生） |

唯一权威声明是 `profiles/graph_contract.yaml`；`infra/lambda/shared/python/graph_contract_data.py` 是它的生成物，`graph_contract.py` 是手工维护的 API。**本文档是这份声明的转述，不是判据本身**——改契约必须同步改这里。

---

## ① 语义维度：哪些边类型算依赖

契约里 29 种边类型，只有 **6 种**带 `dependency: true`：

`AccessesData`、`Calls`、`Delegates`、`DependsOn`、`InvokesTool`、`Retrieves`

其余 23 种是结构边、归属边、事件边，不参与依赖分析。判据只有一个入口：

```python
from graph_contract import is_dependency_edge, dependency_edge_labels
```

`graph_contract.py:345`。它取代了此前散在三个 ETL 里各自复制的 `DEPENDENCY_EDGE_LABELS` 常量。

**带 `dependency: true` 的边必须写 `dependency_kind`。**

### 一个刻意的拆分

`InvokesTool` 是 dependency，`Invokes` 不是。契约注释里写明了理由：Lambda/SNS/StepFunction → LambdaFunction 那种调用不构成依赖关系，把两种语义塞进同一个标签，会让「按 dependency 过滤」的查询同时命中两类边。凡是遇到「同一个动词在两种语境下语义不同」，正确做法是拆标签，不是靠属性区分。

---

## ② 来源维度：`dependency_kind`

| 取值 | 含义 | 谁写 |
|---|---|---|
| `static` | **声明**的依赖 | `aws-etl`、`cfn-etl`、`business-layer`、`manual-fix` |
| `dynamic` | **运行时观测**到的依赖 | `Calls`；以及 `source` 以 `deepflow` 开头的 `DependsOn` / `AccessesData` |
| `live` | 查询层合成，**不落盘** | = `dynamic AND active = true` |

分界线是**声明 vs 观测**，不是「配置 vs 流量」。

`live` 为什么必须存在（`rca/neptune/neptune_queries.py:75` 的注释）：`dependency_kind` 单独不够——缩容到 0 副本的服务，它的边仍然是 `dynamic`，按 `dynamic` 过滤会把已经不存在的依赖返回出来。实测过的例子是 petsite 的三个上游全是 `dynamic`，但其中两个属于 awesomeshop 且副本数为 0，只有按 `live` 过滤才返回正确的空集。

`dependency_kind` 是**写一次**属性，与 `first_seen`、`source` 同组（`EDGE_WRITE_ONCE_ATTRS`），后续 ETL 不得改写。

---

## ③ 存在性维度：`verify_status` / `verify_confidence`

**这一维是本项目区别于业界的部分。** 2026-09-05 的业界调研结论是：多源写同一张图的范式（ServiceNow IRE 属性级仲裁、Backstage 声明式单一权威、Cartography 幂等收敛、New Relic/Dynatrace 边过期）都在解决「谁说了算」和「消失的边怎么删」，但**没有任何一家验证边本身是否为真**。这条轴是本项目自己的。

### 状态

`untested` / `confirmed` / `refuted` / `inconclusive`

`inconclusive` 不是失败，是「本次干预无法对这条边施加有效检验」——例如 Chaos Mesh 打不到 Lambda、目标已缩容到 0、源根本不在集群里、自环边。它**不递增** `verify_refute_count`，因此不会累计触发删边。

### 置信度

`verify_confidence ∈ [0,1]`，由 log-odds 累加后经 sigmoid 归一：

| 证据类型 | 权重 |
|---|---|
| 静态声明 | 每个源 **+1.0** |
| 运行时观测 | 每个源 **+0.5**，总量**封顶 +1.5** |
| 干预确证 | **+4.0**，可翻转先验 |
| 干预证伪 | **−4.0** |

观测封顶的依据：arXiv:2607.09449 给出的「样本越多越容易被虚假相关性诱导出假边」临界阈值。不封顶的话，一条假边只要 ETL 跑得够久就会变成高置信。干预不封顶，因为它是可重复的主动实验。

### 阈值

| 参数 | 值 | 含义 |
|---|---|---|
| `confirm_degradation_pct` | 20.0 | 观测方退化 ≥ 此值判 confirmed |
| `refute_degradation_pct` | 5.0 | 退化 ≤ 此值判 refuted |
| `min_observation_requests` | 20 | 低于此请求数不作判定 |
| `stale_verification_seconds` | 2592000 | 验证 30 天后过期 |
| `hard_degradation_pct` | 70.0 | 强度维度判 hard 的门槛（见 ④） |

`verify_*` 这批属性的写入权威**只有** `chaos-runner` 一个。

### 合法取值是离散集合（诊断判据）

`confidence()` 是 `round(sigmoid(log-odds), 4)`，**没有 clamp**。因为权重都是 0.5 的整数倍，合法输出落在离散集合上，这给出一条可判定的违约模式：

- **零证据是 0.5，不是 0.0。** 「什么都不知道」与「几乎确定不存在」是相反的语义。
- **0.0 需要 log-odds ≤ −9.9，即至少 3 次证伪。** 所以「`verify_confidence` 恰为 0.0 而 `verify_refute_count` 为 0 或缺失」的边，其置信度不可能是 `confidence()` 算出来的。
- **1.0 是可以合法达到的**（log-odds ≥ 9.9），不要当越界残留处理。实测 petsite→petsearch：3 次确证 12.0 + 观测封顶 1.5 = 13.5 → 1.0。

已实测的取值集合（2026-09-05 活图谱，54 条带 confidence 的边）：`0.6225`(=σ0.5) / `0.7311`(=σ1.0) / `0.8808`(=σ2.0) / `0.982`(=σ4.0) / `0.989`(=σ4.5) / `0.9933`(=σ5.0) / `0.9959`(=σ5.5) / `0.9975` / `0.9998` / `0.9999` / `1.0`，全部吻合权重体系；外加 **33 条 0.0 属于历史违约**（见文末）。

---

## ④ 强度维度：`verify_dependency_class`

`hard` / `degraded` / `soft` / `unclassified`。**与存在性正交**：存在性回答边是不是真的，强度回答它有多要紧。影响面分析、容量规划、故障预算用的是这一维。

出处是 Google Cloud《Defining SLOs for services with dependencies》(CRE Life Lessons, 2018-05)，官方定义逐字对应：

| 取值 | 官方定义 |
|---|---|
| `hard` | 它出故障意味着调用方也出故障 |
| `degraded` | 介于两者之间（如缓存失效只降级延迟，不失败） |
| `soft` | **设计得当**则它出故障对调用方无影响（例：尽力而为的日志/追踪） |
| `unclassified` | 判过但分不了级——**显式写值，不留空**，因为「属性缺失」与「判过但分不了级」在查询上无法区分 |

### 官方定义的缺口，与本项目补的两条判据

官方那套是**设计期分类**：`soft` 的定义带条件从句「if they were designed appropriately」，断言的是设计意图，不是观测事实。官方从未打算回答「怎么从数据反推分级」，因为它假设你知道自己的设计。本项目要从故障注入实测反推，因此加了两条官方没有的判据：

1. **`throughput_only` 通道永不判 hard，最高只到 degraded。** `hard` 的定义正是「调用方自己不行了」，而这恰恰是**成功率通道**表达的东西；纯吞吐分不清「自己失败」与「上游不再调它」。实测反例：断 DynamoDB 后 pay-for-adoption 成功率退化 0.00pp 而吞吐塌陷 100%，那个塌陷实为 petsite 传导。
2. **`soft` 需要两个前提同时成立**，各挡掉一种混淆：
   - `injection_confirmed is True` —— 否则分不清 `soft` 与「注入根本没生效」
   - `independent_observing_sources >= 1` —— 否则分不清 `soft` 与「边不存在」

第 2 条补上的是官方定义的一个真实缺口：**一条设计良好的 soft dependency，在干预数据上与一条根本不存在的边完全同形。** 这也是为什么 `soft` 判定必须挂在独立观测证据上，而不能仅凭「打断它没影响」。

### 边界说明

Google SRE 从未把这三级定义为图里的边属性，它始终是 SLO 与风险分析语境下的分类。写成边属性是本项目的扩展。同样，官方还有一组独立词汇（`critical` / `noncritical` backends）和一条定量判据（critical dependency 必须比你多一个 9，"rule of the extra 9"），本项目目前**没有**实现那条定量判据。

---

## 生命周期：边什么时候消失

每种边在契约里声明 `expires_seconds`（软删除／标记 `active=false`）与 `retention_seconds`（硬删除边界）。

**依赖边的过期时间比节点短一到两个数量级**，这是刻意的：

| 边类型 | `expires_seconds` | `retention_seconds` |
|---|---|---|
| `Calls` | **1800**（30 分钟） | 604800（7 天）——唯一有硬删边界的边类型 |
| `AccessesData`、`DependsOn`、`Delegates`、`InvokesTool`、`Retrieves` | **21600**（6 小时） | null（只软删） |
| `InvokesVia`、`PublishesTo`（非依赖边，但也是观测产物） | **21600**（6 小时） | null |
| 其余 21 种结构边 | **null** | null |

`null` 的语义是「生命周期跟随两端节点、不独立过期」，对应 Dynatrace 的 static edge 继承 node lifetime。作为对照，**节点**的过期时间是 39 种里 31 种为 604800（7 天）、1 种 259200（3 天）、7 种 null——不要把节点的 7 天误当成边的过期时间。

`graph_cleanup` 的观测式失效**只作用于** `dependency_kind='dynamic'` 的边（用 `.has('dependency_kind','dynamic')` 显式限定）：声明的边不会因为没人调用就消失，只有观测边会。

---

## 已知缺口

### 门禁只校验「谁在写」，不校验「写什么」和「写到哪」

`EDGE_VERIFICATION.authority` 只声明写入者必须是 `chaos-runner`。已实测到同一根因的三种表现，根因都是旧版 `scripts/write_edge_verdicts.py` 直接写契约里的**证据权重**而非归一化置信度（confirmed→+4.0 / refuted→−4.0 / unverifiable→**0.0**）：

| # | 表现 | 为什么漏网 | 状态 |
|---|---|---|---|
| 1 | 11 条 `verify_confidence` = ±4.0 | 越界，被值域门禁抓到 | 已修（`test_i05` 把守） |
| 2 | 33 条 `verify_confidence` = 0.0 | **落在 [0,1] 内**，值域门禁放行 | 17 条依赖边已回填（`test_i06` 把守）；16 条见下 |
| 3 | 16 条非依赖边带整套 `verify_*` | 不校验能写到哪种边 | 已清除（`test_i07` 把守，删前落盘留存） |

第 2 条有连带影响：靶点选择按 `verify_confidence` 升序排（越低越不确定 → 信息增益越大，`chaos/code/runner/edge_verification.py:453`），所以这批边会永久霸占队列头部——而它们恰恰是**已判明无法用现有手段注入**的边。回填只修了「数值撒谎」，「不可注入的边反复被选中」是独立缺口（T-305）。

工具：`scripts/backfill_verify_confidence.py`、`scripts/clean_nondependency_verify_attrs.py`，两者默认 dry-run。

### 第五个维度：scope（T-306，已落地）

**状态**：契约已声明（`node_scope`）、标注脚本已落地（`scripts/label_node_scope.py`）、全图 1333 个节点已标注、选边器已接入过滤。剩余未做的只有把 `Invokes` 的 `dependency` 标记改对。

起因是上表第 3 条暴露的不是门禁漏洞，而是**缺一个维度**：16/16 全部 `Invokes` 边都被靶点选择器当依赖边选中过——16/16 而非零星几条，说明是两个组件对「什么算依赖」给出了相反答案。

语义上契约那一侧才可疑：`StepFunction → LambdaFunction`（指向 profile 明确声明的 `stepread` / `stepprice`）与 `SNSTopic → LambdaFunction`（告警投递链）按任何定义都是依赖。但那 16 条里 13 条是**平台自身工具链与部署脚手架**。所以真正的分界不是「算不算依赖」，而是**「算不算被观测系统的一部分」**。

#### 判据：权威归属，不是名字模式

原先我把 `platform` / `scaffolding` 的判据定在名字前缀上，并把它登记为最大未决点——因为它与「不要用裸词 grep 做判据」冲突。2026-09-05 查证后这个未决点**已经解决**：两条判据都能落到资源的固有属性上。

**AWS 资源 → CloudFormation 栈归属**

```
describe-stacks → ParentId 非空 ⇒ 嵌套栈 ⇒ scaffolding
```

实测账号内 19 个栈，恰好 3 个是嵌套栈，且那 7 个 CDK provider framework Lambda **全部**落在其中两个里（KubectlProvider 2 个 + ClusterResourceProvider 5 个）。这不是命名巧合——CDK 把 provider framework 放进独立嵌套栈是它的架构事实。

这条判据同时纠正了名字判据的一个错误：`ServicesEks2-GuardDutyCleanupLambda` 名字像脚手架，但它在**主栈**里，是被观测系统的资源。栈归属对，名字错。

`CDKToolkit`（bootstrap 栈）也归 `scaffolding`——那 6 条 `DependsOn` 指向的 `cdk-hnb659fds-container-assets` 正是它的 **ECR Repository**（不是 S3 桶），这也印证了那批边 `image-repo-dependency` 不可注入的判定是准确的。

**K8s 对象 → namespace**

| namespace | 节点数 | scope |
|---|---|---|
| `petadoptions` | 302 | `observed` |
| `awesomeshop` | 25 | `observed` |
| `kube-system` | 184 | `cluster-infra` |
| `amazon-cloudwatch` / `amazon-guardduty` / `amazon-network-flow-monitor` / `deepflow` | 94/45/38/29 | `observability` |
| `chaos-mesh` | 44 | `platform` |
| `default` | 26 | 需逐个判 |

#### 六档而非四档

`observability` 必须与 `platform` 分开：前者是**被观测系统的观测者**（采集栈），后者是**本依赖图谱平台**。合并就分不清「谁在观测」与「谁在管依赖图」，而这两者的自噪声治理是完全不同的问题（曾把观测自噪声从 73.2% 压到 4.6%，那批就是 `observability`）。`cluster-infra` 也不能合进 `external`，否则 CoreDNS 这类真依赖会被误判成外部系统。

| scope | 判据 |
|---|---|
| `observed` | 顶层栈 ∈ {ServicesEks2, Applications, AwesomeShopInfra} 且非嵌套；或 namespace ∈ {petadoptions, awesomeshop} |
| `observability` | namespace ∈ {amazon-cloudwatch, amazon-guardduty, amazon-network-flow-monitor, deepflow} |
| `platform` | 栈 ∈ {NeptuneEtlStack, AlertBufferStack}；或 namespace = chaos-mesh；或平台自产的分析件（`Incident` / `ChaosExperiment` / `TopologyChange`） |
| `scaffolding` | 栈 ParentId 非空（嵌套栈），或栈 = CDKToolkit |
| `cluster-infra` | namespace = kube-system |
| `external` | 其余顶层栈（WaggleAIAgents / devops-agent-* / TidbPoc* / TranslatorStack / kirocrew-* / PVRE），或不属任何栈 |
| `unknown` | **解析不出的必须显式写这个值**，不得默认成任何一档 |

最后一行是硬要求，与「不分级时写 `unclassified` 而非留空」同一条教训：属性缺失与「判过但判不出」在查询上无法区分。

#### 覆盖率（实测，1333 个节点）

标注结果（`scripts/label_node_scope.py --apply`，1333/1333 写入成功）：

| scope | 节点数 |
|---|---|
| `observed` | 394 |
| `platform` | 346 |
| `observability` | 210 |
| `cluster-infra` | 185 |
| `unknown` | 131 |
| `external` | 57 |
| `scaffolding` | 10 |

可解析 1202/1333 = **90.2%**。剩余 131 条 `unknown` = 26 个 `default` namespace 混放 + 105 个不属任何 CloudFormation 栈的手工创建资源（图里 `managedBy=manual` 就有 79 个）。

把「不属任何栈」直接判 `external` 能凑到 100%，但会误判手工创建却属于被观测系统的资源（`petsite-ops-alerts` 就是 `managedBy=manual`），所以按设计显式写 `unknown`——没有为了数字好看而破例。

#### 选边器如何消费 scope

排除的是**触及** `platform` / `scaffolding` / `observability` / `cluster-infra` 的边，而**不是**要求两端都是 `observed`。差别很实在：110 条依赖边里只有 50 条两端都是 `observed`，另有 34 条一端是 `unknown`——要求两端 `observed` 会连带丢掉这 34 条，那是把「解析不出」当成「不该打」。

`platform` 那一档是**安全问题**不只是噪声：12 条 `platform → platform` 边是本平台自己的 ETL/RCA 链，往那里注入可能打断记录本次实验判定的那条管道——`neptune-etl-*` 挂了，这次实验的结论就写不回图。

实测选边结果（110 条依赖边全账）：

| 归类 | 条数 |
|---|---|
| 选中 | 80 |
| scope:platform 排除 | 16 |
| scope:scaffolding 排除 | 6 |
| no_observer（自环） | 4 |
| unreachable_by_any_backend | 4 |

scope 过滤跑在能力矩阵**之前**：先问「该不该打」再问「能不能打」。

#### 其他设计约束

- **scope 是节点属性，边的 scope 由两端派生**——否则要给 2472 条边各标一次。
- **不是 write-once**：资源可能从一个栈迁到另一个栈，scope 会变。但要进 `NODE_ATTR_AUTHORITY` 约束谁能写。
- **删数据是错的**：`petsite-rca-engine` 与 `neptune-etl-*` 都依赖 Neptune，即 RCA 引擎依赖它要诊断的那套系统的观测存储，Neptune 挂了 RCA 就查不了。这是本平台恰该发现的**自举依赖**单点风险，删掉等于自己造盲区。且项目已有硬约束「Neptune 不能删、ETL 可改不可删」。

#### 实施顺序上的硬前提

把 `Invokes` 改成 `dependency: true` 的前提**已经具备**：能力矩阵判定 `SNSTopic → LambdaFunction` 与 `StepFunction → LambdaFunction` 都是 `injectable`（FIS 有 3 个 Lambda 动作），不会被错误排除；scope 过滤已接入选边器，那 13 条脚手架/平台边会被 `scope:platform` / `scope:scaffolding` 拦掉。

原先记录的阻碍是：那 16 条边的 `verify_*` 已被清除、现在是 `untested`，而当时的排除判据依赖**存储的理由 token**，`untested` 边没有 token、排除不了。矩阵替掉字符串判据之后这个阻碍消失了——矩阵按节点类型算，不需要任何历史记录。

### 注入能力矩阵：后端 × 目标类型 × 注入位置

`injectability.py` 从 `fault_catalog.yaml` 构建（不硬编码动作清单，加一条动作矩阵自动跟着变）：

| 后端 | 动作 | 可直接打的目标类型 | 源侧切断 |
|---|---|---|---|
| Chaos Mesh | 19 | Pod / Microservice / Deployment | **是**（NetworkChaos `externalTargets`） |
| FIS | 36 | 加 LambdaFunction、DynamoDBTable、S3Bucket、Subnet、AWSServiceEndpoint、RDSCluster/Instance | 否 |

三条轴必须分开，混在一起会得出错误结论（第一版就是）：

| 轴 | 例子 | 与后端有关？ |
|---|---|---|
| 工具触不到目标 | Chaos Mesh 打不到 Lambda | **是**——矩阵管这个 |
| 可注入但稳态不可观测 | 镜像仓库依赖（只在拉镜像时用到） | 否，`needs_compound_experiment` |
| 没有观测方 | 自环、合成流量源 | 否，`no_observer` |

第一版有两条结论是错的：`chaos-mesh-cannot-target-lambda` 判永久排除（但 FIS 能打 Lambda），`image-repo-dependency` 判永久排除（实为需复合实验：切断 + 触发重启）。

`precondition_unmet`（副本为 0、源不在集群）压到**最低优先级 pri=9** 而不是排除：有真靶点时永不被选中，队列空了才轮到，那时重试代价最低。永久排除会造成盲区（扩容回来后再也不验证）。

未映射事实（如实登记）：FIS 的 `volume_arns` / `nodegroup_arn` / `asg_arn` / `route_table_arn` / `role_arn` 在图谱里**没有对应节点类型**，这些动作永远选不出靶点。

### 其他

- `hard` 的定量判据只有退化率阈值，没有实现官方的 "rule of the extra 9"。
- 强度维度依赖故障注入才能判定，因此对无法注入的边（Lambda、已缩容服务）永久停在 `unclassified`。

---

_相关文档：`profiles/graph_contract.yaml`（唯一权威声明）、`todo/neptune-selection-assessment_20260905-0650.md`（存储选型评估）、`todo/research-lab-graphdb-necessity-FINDINGS_20260905-0634.md`（业界调研，含四种范式对比）_
