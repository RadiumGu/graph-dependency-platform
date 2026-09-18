# 东京区 vpc-010ab37a3f9f74725 纳入 Next-gen Resilience Hub — 完成报告

> 执行:2026-08-29 06:15 – 07:50 UTC(约 95 分钟,goal-loop 8 轮)
> 账号 926093770964 · region ap-northeast-1 · 锚目录 `todo/goal-loop-arh-v2/`
> 核验:`bash todo/goal-loop-arh-v2/verify_dod.sh` → **5 条 DoD 全绿,退出码 0**

---

## 1. 结论

VPC 内**全部在运行的应用都已纳管**,建成 1 个 system、3 条 user journey、3 条 resilience
policy、**4 个 service、191 个资源**。3 个 service 评估成功并产出 **57 条 findings 与 68 条拓扑边**;
第 4 个(`ops-rca-plane`)资源已纳入但评估无法完成,原因已定性(第 5 节)。

| Service | Tier | 资源 | 评估 | findings | 拓扑边 |
|---|---|---|---|---|---|
| `petsite-core` | tier0 | 124 | ✅ SUCCESS(17 min) | **23** | **35** |
| `awesomeshop-legacy` | tier2 | 20 | ✅ SUCCESS(12 min) | **17** | **11** |
| `graph-observability` | tier1 | 39 | ✅ SUCCESS(13 min) | **17** | **22** |
| `ops-rca-plane` | tier2 | 8 | ❌ 不可评估(5 次尝试) | 0 | 0 |

`petsite-core` 的 achievability(按 policy 分量,不是整体一个值):
`availabilitySlo`(99.95)= **ACHIEVABLE**;`multiAzRtoRpo`(15/5 min)= **NOT_ACHIEVABLE**;
`dataRecoveryTimeBetweenBackups`(60 min)= **NOT_ACHIEVABLE**。
即当前单区域架构无法满足 tier0 的恢复目标,需架构改动而非配置调整。

---

## 2. 建成的模型

**System** `petsite-tokyo`(`arn:...:system/petsite-tokyo:lj9qdn`)

**User journey**(三条**直接取自** `infra/lambda/etl_aws/business_config.json` 的
`business_capabilities`,没有另造第二套事实源):

| Journey | Tier | 覆盖服务 |
|---|---|---|
| `PetAdoptionFlow` | tier0 | petsite, payforadoption |
| `AdoptionHistoryView` | tier1 | pethistory |
| `PetInventoryManagement` | tier1 | petsearch, petlistadoptions, statusupdater |

**Policy**(RTO/RPO 由 tier 推导,推导规则见 `roadmap.md`;刻意不设 `multiRegion` ——
单区域部署设了必然全判 NOT_ACHIEVABLE,只会淹没有价值的 findings):

| Policy | SLO | multiAz RTO/RPO | DR 方式 | 备份间隔 |
|---|---|---|---|---|
| `arh-tier0` | 99.95 | 15 / 5 min | WARM_STANDBY | 60 min |
| `arh-tier1` | 99.9 | 60 / 15 min | PILOT_LIGHT | 240 min |
| `arh-tier2` | **不设** | 240 / 60 min | BACKUP_AND_RESTORE | 1440 min |

**服务切分**从最初盘点的 6 组收敛为 4 个:S3/S4/S5 共享同一个 `System=deepflow` 标签、
一条输入源即可覆盖,拆开只换来更细的 policy 粒度、多花 $30/月,而拆分随时可逆。

---

## 3. 六个「文档说的和实际不一样」的实测结论

这些是本次纳管最可复用的产出。**每一条都会让照文档做的人卡住。**

### 3.1 v1 与 v2 是两个不同的托管策略,名字只差一个 `V2` 且拼写规则不同

| 策略 | 拼写 | 归属 |
|---|---|---|
| `AWSResilienceHubAsssessmentExecutionPolicy` | **三个 s**(Asss) | 旧版 ARH v1 |
| `AWSResilienceHubV2AssessmentExecutionPolicy` | **两个 s**(Ass) | **next-gen v2** |

`AWSResilienceHubV2AsssessmentExecutionPolicy`(三个 s + V2)**不存在**。
官方 skill `resilience-hub-getting-started` 与其 `references/setup-procedure.md`
**只提 v1 那个**,照它做的 v2 service 会在评估阶段失败。两个都挂上无害。

### 3.2 EKS RBAC 是硬前置,不是可选增强

只要 service 含 `eks` 输入源而 K8s 侧读不到,**整个评估 FAILED**,不会降级跳过 EKS 部分
只评 AWS 侧 —— `petsite-core` 的 124 个 AWS 资源早已解析完毕,照样全盘失败。
两步都必须做:

1. 应用 ClusterRole `resilience-hub-eks-access-cluster-role` + ClusterRoleBinding
   到组 `resilience-hub-eks-access-group`(官方 YAML 在
   [grant-permissions-to-eks-in-arh](https://docs.aws.amazon.com/resilience-hub/latest/userguide/grant-permissions-to-eks-in-arh.html);
   该页是 v1 文档但**角色/组名与 v2 报错要求完全一致**,v1 的
   `AwsResilienceHubAssessmentEKSAccessRole` 那套旧命名不要用)
2. `aws eks create-access-entry --kubernetes-groups resilience-hub-eks-access-group`
   把 invoker role 映射进去(集群 `authenticationMode=API_AND_CONFIG_MAP` 时优先用
   access entry,不必改既有 `aws-auth` ConfigMap)

清单已存 `todo/goal-loop-arh-v2/arh-eks-rbac.yaml`。

### 3.3 `errorCode` 恒为 null,错误只在 `errorMessage`

`list-failure-mode-assessments` 的 `errorCode` 字段在本次 8 次失败中**全部为 null**。
官方文档列了一整套 errorCode 枚举(`INVALID_PERMISSIONS` / `CMK_ACCESS_DENIED` /
`INPUT_VALIDATION` / `POLICY_VALIDATION` / `AGENT_ERROR` …),按它排查会一无所获。
**永远读 `errorMessage`。** 而且要注意 errorMessage 会带**误导性通用后缀** ——
"did not produce a topology. Please verify the invoker role has ...Policy" 这句里,
策略已挂载时后半句依然照样输出。

### 3.4 `availabilitySlo.target` 是三值枚举,不是自由 double

实际只接受 `[99.9, 99.95, 99.99]`。`--generate-cli-skeleton` 把它标成 double(`0.0`),
官方 skill 更明确写「any value between 0 and 100 … do NOT reject other values
(e.g., 99.5 or 99.999 are valid)」—— **都是错的**。传 99.5 得
`ValidationException: Allowed values are [99.95, 99.9, 99.99]`,`reason: INVALID_FIELD_VALUE`。
后果:枚举底就是 99.9,所以**无法给低等级服务设一个更宽松的可用性目标**,
只能不设(本次 tier2 即如此)。

### 3.5 `create-*` 的响应把对象包在嵌套键里

`create-system` / `create-policy` / `create-service` 的响应形如 `{"service": {...}}`,
所以 `--query 'serviceArn'` 返回 `None` **但对象已创建成功**。差点误判为失败而重复创建。
一律从 `list-*` 回读 ARN。响应字段名也不统一:拓扑边在 `serviceTopologyEdgeSummaries`、
依赖在 `dependencySummaries`、而 findings 在 **`findingsSummary`**(单数,无 `-ies`)。

### 3.6 `resourceTags` 输入源的匹配集是「创建时快照」

给资源**新打标签后,启动评估不会刷新已有输入源的匹配集**。本次实测:给 3 个 SNS 主题打上
`System=petsite-ops` 后跑评估,资源数仍是 5;`delete-input-source` + `create-input-source`
重建同一条 TAGS 源后立刻变成 8。
**任何靠标签做纳管的流程都要注意:资源打了标签 ≠ 会被纳入。**

附带一条相关行为:`create-input-source` 触发的自动解析**不可靠**。
`ops-rca-plane` 挂完两条输入源后 3 分钟资源数仍为 0、且事件流里没有
`SERVICE_RESOURCES_ASSOCIATED`;而 `start-failure-mode-assessment` 会强制解析。
v2 没有 v1 的 `resolve-app-version-resources` 显式命令,**所以挂完输入源应直接启动评估**。

---

## 4. 其他实测要点

- **评估耗时约 12–17 分钟**,不是文档说的几分钟。快速结束(4–7 分钟)的都是**失败**,
  失败走短路径。轮询节奏按 15–20 分钟估。
- **`dependencies` 为零有三个不同原因,不要混为一谈**(2026-08-29T07:59Z 复核):

  | Service | status | eligibleResourceCount | message | 原因 |
  |---|---|---|---|---|
  | `petsite-core` | ENABLED | **null** | "Discovering resources" | 首轮资源发现**还没跑完**(文档:该字段在首轮完成前为 null) |
  | `graph-observability` | ENABLED | **3** | **无(null)** | 按官方状态表 = **已完成、可查看**;确实发现**零个**依赖 |
  | `awesomeshop-legacy` | **DISABLED** | — | "Enable dependency discovery…" | **本目标刻意关闭**(付费加购 $10/service/月,只给 S1/S3 开) |
  | `ops-rca-plane` | **DISABLED** | 0 | 同上 | 同上 |

  官方状态对照表(`next-gen-discovery-status.html`):`ENABLED` + `eligibleResourceCount ≥ 1`
  + `message = null` 即「Dependencies are ready to view」。**S3 正处此态而结果为空。**

- **⚠️ 重要反向结论:ARH 的依赖发现用的是 DNS query log 分析,与本项目已证实的假阴性同源。**
  前置条件(`next-gen-discovery-prerequisites.html`)明确要求:计算资源必须
  「make DNS queries through Route 53 resolvers」,且 ARH「discovers either EC2 instances
  (**preferred**) or VPC (fallback)」。后半句解释了为什么 S3 的 39 个资源里只有 **3** 个合格 ——
  就是 DeepFlow 的三台 EC2(`i-0cf272a12ecff87f3` / `i-00f46b680713b9b14` / `i-077d4ba09815bd2b8`),
  EKS pod 与 VPC 内 Lambda 在有 EC2 时都不计入。

  而本项目早已定性:`neptune_etl_deepflow.py:328` 的漂移判定只用 DNS 作观测源,
  导致 26 条带漂移状态的边里 22 条(85%)被误判 `declared_not_observed` ——
  因为 AWS SDK 启动时解析一次就复用连接、走 VPC 端点更不产生公网 DNS 查询。
  **ARH v2 的依赖发现有完全相同的盲区**,所以原
  `resilience-hub-v2-topology-enhancement` 方案里「用 ARH 补依赖发现短板」这一预期
  **不成立**。ARH 的真正增量在**配置层**(PodDisruptionBudget / 健康探针 / 备份保留 /
  PITR / 单 NAT 共享命运 / ARC zonal shift),不在依赖发现。

  补充实测:本账号本区域 `list-resolver-query-log-configs` **一条配置都没有**;
  VPC 使用 `AmazonProvidedDNS`(故「使用 Route 53 解析器」这条前置满足)。
  文档称 ARH「uses Next generation Resilience Hub's own service credentials to access
  DNS query data」,即不依赖客户自建日志 —— 但从外部无法证实它实际读到了什么,
  因此「零依赖」是「DNS 里确实没有」还是「ARH 取不到数据」**无法区分**。
  能确定的是前者与本项目已知的 DNS 盲区完全吻合。

- **`list-dependencies` 有时间窗参数,且窗口长度被死死限定**(2026-08-29T08:03Z 实测)。
  参数:`queryRangeStartTime` / `queryRangeEndTime` / `queryRangeGranularity`。约束:
  | granularity | 窗口长度要求 |
  |---|---|
  | `HOURLY` | **恰好 24 小时** |
  | `DAILY` | **恰好 168 小时(7 天)** |

  超出即 `ValidationException: HOURLY granularity requires a query range of exactly 24 hours.`
  → **没有任何单次调用能覆盖完整的 35 天回看期**,程序化导出必须分 5 段(5 × 7 天)查询后合并。
  控制台默认只显示 **1 天**(截图:"Viewing data from 2026-08-28 to 2026-08-29"),
  这会让稀疏依赖在默认视图里看不见 —— 排查时先把日期拉宽。

  **但本次已排除窗口因素**:按 5 × 7 天覆盖完整 35 天回看期逐段查询,
  加上最近 24h 的 HOURLY 查询,`petsite-core` 与 `graph-observability` **全部返回 0**。
  底层依赖集合确实为空,不是窄窗口掩盖了数据。
- **EKS worker EC2 实例不以 `EC2::Instance` 出现**,而是折叠成
  `AutoScaling::AutoScalingGroup` + `EKS::Nodegroup` —— ARH 只导入 top-level 资源。
  第一眼会以为漏了 4 台节点机。
- **副本数为 0 的 Deployment 仍会被导入**(awesomeshop 6 个全 0 副本都在),ARH 按声明导入。
- **ARH 不支持的类型被静默过滤**:`System=deepflow` 捞到 3 个 AMI image,不出现在
  `list-resources` 里。所以「发现数 > 保留数」是正常的(S1 189→124),不是解析失败。
- `awslabs eks-mcp-server` 的 `apply_yaml` 在本环境是只读
  (`not allowed without write access`),应用 RBAC 用了官方 kubectl v1.37.0 arm64。
- **`--query 'length(x)' --output text` 在自动分页时每页各打印一个数字**。
  `petsite-core` 的资源计数返回 `50\n67\n7`(合计 124),直接做整数比较会报
  `integer expression expected`。`verify_dod.sh` 里已加 `RHCOUNT` 求和 helper。

---

## 5. `ops-rca-plane` 不可评估 — 定性结论

`errorMessage`(5 次一致):`The resources discovered for this service did not produce a topology.`

三个不同机制的尝试全部否决:

| 尝试 | 结果 | 排除了 |
|---|---|---|
| 挂 v2 专属托管策略 | FAILED | IAM 权限(同一策略让另外三个成功) |
| `create-service-function` + `create-service-function-resources` 做拓扑锚点 | FAILED | 「缺拓扑提示」 |
| 补 3 个 SNS 主题作连接件(资源数 5→8) | FAILED | 「缺连接性资源」 |

**最可能的机制**:`ops-rca-plane` 是**唯一**资源集里没有任何 `AWS::EC2::VPC` /
`Subnet` / `EC2::Instance` 的 service。三个成功 service 都含 VPC + Subnet,
且全部 14 条 `CONTAINMENT` 边都来自网络层。纯 serverless 且不含网络资源的集合
似乎构不成 ARH 认可的拓扑。

**一条顺带查实的真实欠项**:该链路一半的编排关系不在 AWS 控制面上 ——
4 个 Lambda 的 `list-event-source-mappings` **全为 0 条**、`scheduler list-schedules` 为空,
`gp-alert-buffer → gp-window-flush` 是**代码内直接调用**。ARH 看不见它,
本项目的 Neptune 图谱同样看不见。运维面的编排缺少声明式表达,这本身值得单独立项。

---

## 6. findings 摘要(57 条)

分布:**HIGH 23 / MEDIUM 32 / LOW 2**。类别以 `MISCONFIGURATION_AND_BUGS` 为主,
其次 `SINGLE_POINT_OF_FAILURE`。全文见 `exports/findings-<service>.json`。

### 6.1 ARH 独立命中的、本项目已知的问题(交叉验证成立)

| ARH finding | 与既有认知的对应 |
|---|---|
| `All six deployments have zero replicas — complete service outage`(HIGH) | 正是本次盘点标为 T-094 的 awesomeshop 欠项 —— 计算层全 0 副本而 mysql + ElastiCache 仍在计费 |
| `Neptune single instance in one AZ is a single point of failure`(HIGH) | `petsite-neptune` 单实例 `db.r6g.large`,与图谱 tier1 定级一致 |
| `ClickHouse single EC2 instance is a single point of failure`(HIGH) | ClickHouse 跑在 `i-0cf272a12ecff87f3`(deepflow-server),单点 |
| `EKS worker nodes span only two AZs`(MEDIUM,三个 service 都命中) | 4 台 worker 分布在 1a/1c 两个 AZ,与盘点一致 |

### 6.2 ARH 发现而本项目图谱**看不到**的问题(增量价值所在)

这些全是 K8s 与 AWS 配置层的属性,DeepFlow 的 L7 调用链和 Neptune 的拓扑都不覆盖:

- `No PodDisruptionBudgets protect deployments`(三个 service 都命中)
- `Frontend petsite-deployment missing all health probes and resource requests`(HIGH)
- `Pethistory-deployment missing readiness probe allows traffic to unready pods`(HIGH)
- `No Horizontal Pod Autoscaler on any production deployment`(HIGH)
- `No preStop hooks cause dropped requests during pod termination`
- `No pod anti-affinity allows all replicas to land on same node`
- `Missing topology spread constraints`
- `Aurora PostgreSQL cluster has no failover replica creating database SPOF`(HIGH)
- `Aurora PostgreSQL cluster has minimal 1-day backup retention period`(HIGH)
- `DynamoDB table lacks point-in-time recovery for pet adoption data`(HIGH)
- `Single zonal NAT Gateway creates cross-AZ shared fate for outbound traffic`(HIGH)
- `No centralized backup plan protecting stateful resources`(HIGH)
- `Critical data stores and infrastructure lack deletion protection`
- `ARC zonal shift / autoshift not enabled`(多条,ALB 与 EKS 都有)
- `External Docker Hub dependency for Prometheus image blocks pod recovery`(SHARED_FATE)
- `Lambda functions share unreserved concurrency pool creating shared fate`

最后两条尤其值得注意 —— 它们是**依赖类**问题,正是本项目图谱的主场,却都没被图谱捕获:
外部镜像仓库依赖不在 Neptune 的节点类型里,Lambda 并发池的共享命运也没有对应边。

---

## 7. 图谱可消费的产出(下一步的原料)

`exports/` 目录:

| 文件 | 内容 |
|---|---|
| `topology-edges-<service>.json` | **68 条边**:`DATA_FLOW` 54 + `CONTAINMENT` 14 |
| `findings-<service>.json` | 57 条 findings |
| `dependencies-<service>.json` | 0 条 —— 三个原因各异,见第 4 节;**ARH 依赖发现与本项目已证实的 DNS 假阴性同源,不能补依赖短板** |
| `resources-<service>.json` | 191 个资源(带 `resourceType` 与 `inputSource.type`) |
| `arns.env` | 全部 ARN(system / 3 policy / 4 service / 3 journey / invoker role) |

边结构可直接映射进 Neptune:
```
sourceResourceIdentifier --[topologyType]--> destinationResourceIdentifier
  + sourceAccount / sourceRegion / destinationAccount / destinationRegion
```
`topologyType` 正好当边类型用。按 ARN join 既有图谱节点即可,这是原
`resilience_hub_v2_topology_enhancement` 方案里「第 5 个 ETL」的输入。

---

## 8. 成本实况

| 项 | 单价 | 用量 | 月成本 |
|---|---|---|---|
| Service fee(含 ≤150 资源 + 2 次评估/月) | $15 / service / 月 | 4 | **$60** |
| Dependency discovery | $10 / service / 月 | 2(S1、S3) | **$20** |
| 超额评估 | $0.10 / 资源 / 评估 | 见下 | 待账单确认 |
| **合计(基线)** | | | **≈ $80 / 月** |

计费自 **07:04:32Z**(首次评估完成)开始。next-gen **无免费额度**(6 个月免费仅属旧版 v1)。

⚠️ **超额评估风险**:排障期间 `petsite-core` 跑了 4 次评估(3 失败 1 成功)、
`ops-rca-plane` 跑了 5 次(全失败),都超过每月含 2 次的额度。
FAILED 的评估是否计费官方文档未明说 —— 若计费,`petsite-core` 第 3–4 次约
$0.10 × 124 × 2 ≈ **$25**,`ops-rca-plane` 第 3–5 次约 $0.10 × 8 × 3 ≈ **$2.4**(超额评估
最低按 50 资源计,则约 $15)。**需在下月账单里核对实际数额。**

**停止计费**:`aws resiliencehubv2 delete-service --service-arn <arn>` 删掉 4 个 service 即可;
system 与 policy 本身不计费。

---

## 9. 遗留与建议下一步

| # | 事项 | 说明 |
|---|---|---|
| ~~T-090~~ | ~~把 68 条拓扑边 join 进 Neptune(第 5 个 ETL)~~ | **已验证不做(wontfix,2026-08-29)** —— ARH 的边严格更粗(集群粒度、无 provenance),Neptune 已有微服务粒度 + 文件行级证据;依赖发现与本项目已证实的 DNS 假阴性同源;findings 每月只能刷 2 次且零消费方。完整论证见 `resilience-hub-v2-topology-enhancement_20260827-1647.md` 第 7 节 |
| **T-095** | **给图谱补两类缺失维度**(替代 T-090,价值更高) | findings 暴露出图谱缺的是整个维度而非更多边:`External Docker Hub dependency`(外部镜像仓库依赖,31 种节点类型里没有)与 `Lambda functions share unreserved concurrency pool`(并发池共享命运,26 种边类型里没有),两者都是 SHARED_FATE 类,由自有 ETL 采集不受配额限制 |
| T-091 | ARH findings 与图谱 `drift_status` 对账 | ARH 发现而图谱没有的依赖 = 图谱假阴性的独立证据源,与已知的「漂移判定只用 DNS 作观测源」缺陷直接相关 |
| T-092 | S3 拆成 observability / neptune / etl 三个 service | 换更细 policy 粒度,代价 +$30/月,需用户决定 |
| T-093 | `AWS::ResilienceHubV2::*` CFN 模板化 | 有 System/Service/ServiceFunction/UserJourney/Policy 五种资源,但**没有 InputSource 与 Assessment**,模板化只能覆盖一半,输入源仍需 CLI 补 |
| T-094 | awesomeshop 计算层全 0 副本而数据层仍在计费 | ARH 已独立判为 HIGH:`complete service outage`。真实成本与资安欠项,需用户决策处置 |
| — | `petsite-core` 的依赖发现仍在首轮(`eligibleResourceCount` 为 null),可隔天复查 | 但**不要期待它能补依赖短板** —— 见第 4 节:机制是 DNS query log,与本项目已证实的假阴性同源。且「EC2 优先」规则下 S1 的计算面是 EKS pod 与 Lambda,EKS 节点又被折叠成 ASG/Nodegroup,合格资源数可能很小甚至为 0 |
| — | 若要给 S2/S4 也开依赖发现:`update-service --dependency-discovery ENABLED` | 每个 +$10/月。鉴于上一条的结论,**建议不开** |
| — | 依 findings 修 tier0 的两项 NOT_ACHIEVABLE | Aurora 加 failover replica、延长备份保留、DynamoDB 开 PITR 是最直接的几项 |
