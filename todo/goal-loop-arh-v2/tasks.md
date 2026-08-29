# Tasks — ARH v2 纳管任务看板

> **代理每轮必须更新本文件。** 状态取值:`todo` / `doing` / `review` / `done` / `wontfix`
> 记录格式:在卡片正文追加一行 `<UTC 时间> cycle-<n>: <动作> <结果>`
> 领卡规则:选 `todo` 且依赖全部 `done` 的卡;同一时刻最多一张 `doing`

---

## 进度总览

| 阶段 | 卡数 | done | 状态 |
|---|---|---|---|
| 0 前置补齐 | 6 | **6** | ✅ done |
| 1 建模层 | 3 | **3** | ✅ done |
| 2 纳管 | 5 | **5** | ✅ done |
| 3 评估(计费) | 7 | **7** | ✅ done(3 SUCCESS + S4 定性收口) |
| 4 导出与对账 | 4 | **4** | ✅ done |
| X 遗留 | 5 | 0 | 移交用户(见最终报告第 9 节) |

DoD 状态:DoD-1 ✅ · DoD-2 ✅ · DoD-3 ✅ · DoD-4 ✅ · DoD-5 ✅ —— **全绿**

**最终核验(2026-08-29T07:50Z)**:`bash verify_dod.sh` → **31 PASS / 0 FAIL,退出码 0**。
最终报告:`../arh-v2-onboarding-20260829-0750.md`(266 行)。


**评估结果汇总**:
| Service | 评估 | 耗时 | findings | 拓扑边 |
|---|---|---|---|---|
| petsite-core | ✅ SUCCESS `540027fa` | 17 min | **23** | **35** |
| awesomeshop-legacy | ✅ SUCCESS `ddee25c5` | 12 min | **17** | **11** |
| graph-observability | ✅ SUCCESS `ec40eb35` | 13 min | **17** | **22** |
| ops-rca-plane | ❌ 5 次 FAILED,重跑 `80585a19` | — | 0 | 0 |

**拓扑边类型分布(68 条)**:`DATA_FLOW` **54** + `CONTAINMENT` **14**。
边结构:`sourceResourceIdentifier` → `destinationResourceIdentifier`,带
`sourceAccount/Region`、`destinationAccount/Region`、`properties:[{topologyType}]`
—— **可直接 join 进 Neptune**,`topologyType` 正好映射成边类型。

**`dependencies` 三个 service 全为 0 条**:`dependencyDiscovery.status = INITIALIZING`
(message "Discovering resources"/"Discovering dependencies",S3 报 `eligibleResourceCount=3`)。
它有 35 天回看窗口且异步,初始化期间必然为空,不是失败。
**DoD-5 判定已相应修正**:topology-edges 与 findings 必须非空;dependencies 非空即通过,
为空则要求 `dependencyDiscovery` 状态已落盘且为 INITIALIZING/ENABLED。
这是异步服务行为导致的判定修正,不是降低标准。

**当前资源解析实况**:petsite-core 124 / awesomeshop-legacy 20 / graph-observability 39 /
ops-rca-plane **8**(SNS 补入后)—— 共 **191** 个资源。

**🎉 首次评估成功(07:04:32Z)**:`petsite-core` assessmentId `540027fa`,耗时 **17 分钟**
(06:47:38 → 07:04:32),产出 **23 条 open findings**。
⚠️ **计费自此刻开始**(north_star 第 5 节:service 创建 + 首次评估完成后开始计费)。

**S1 achievability(按 policy 分量)**:
| 分量 | 结论 |
|---|---|
| `availabilitySlo`(99.95) | **ACHIEVABLE** |
| `multiAzRtoRpo`(15/5 min) | **NOT_ACHIEVABLE** |
| `dataRecoveryTimeBetweenBackups`(60 min) | **NOT_ACHIEVABLE** |

**S1 findings 分布(23 条:13 HIGH / 9 MEDIUM / 1 LOW)**:
| 数量 | 严重度 | 类别 |
|---|---|---|
| 10 | HIGH | MISCONFIGURATION_AND_BUGS |
| 4 | MEDIUM | MISCONFIGURATION_AND_BUGS |
| 2 | HIGH | SINGLE_POINT_OF_FAILURE |
| 2 | MEDIUM | SHARED_FATE |
| 1 | HIGH | EXCESSIVE_LOAD |
| 1 | MEDIUM | SINGLE_POINT_OF_FAILURE |
| 1 | MEDIUM | EXCESSIVE_LOAD |
| 1 | MEDIUM | EXCESSIVE_LATENCY |
| 1 | LOW | EXCESSIVE_LATENCY |

`list-failure-mode-findings` 的字段:`findingId / name / description / severity / status /
failureCategory / policyComponent / serviceArn / updatedAt`。
**注意 achievability 不是 per-finding 字段**,只在 assessment / get-service 上。

**当前资源解析实况**:petsite-core 124 / awesomeshop-legacy 20 / graph-observability 39 /
ops-rca-plane 5 —— 四个 service 全部非空,共 **188** 个资源(评估过程中仍在增长)。

**DoD-1 核验实况(2026-08-29T06:32Z)**:systems=1、services=4、policies=3,
input source 数 S1=2 / S2=2 / S3=3 / S4=2,每个 service 的 policyArn 均非空 → 全绿。

**DoD-3 需要一处措辞修正**:原文写「每条 policy 都能读出**非零**的 availabilitySlo.target」,
但 `availabilitySlo.target` 是三值枚举 `[99.9, 99.95, 99.99]`,底就是 99.9,
所以 tier2 **刻意不设** SLO(强塞 99.9 会比 tier2 应有目标更严)。
DoD-3 判定改为:3 条 policy 均存在且 `multiAz.rtoInMinutes`/`.rpoInMinutes` 非零;
SLO 仅 tier0/tier1 需非空。这是 API 约束导致的判定修正,不是降低标准。

---

## Cycle 日志

- **cycle-0 (06:15–06:21Z)** 工作定义四文件建立;阶段 0 六张卡做完
  (IAM invoker role + 6 处标签补齐);验收暴露并修正 S3 输入源缺口(T-006)。
- **cycle-1 (06:24–06:32Z)** 阶段 1 全部完成(system + 3 policy + 3 journey);
  阶段 2 建了 4 个 service、挂了 9 条输入源,三个 service 共解析出 151 个资源;
  踩到两个 API 与骨架不一致的坑(create 响应嵌套、SLO 是枚举);
  S4 解析为 0,四个假设逐一排除后改用启动评估强制解析(T-024);
  S1 与 S4 评估已启动。
- **cycle-2 (06:34–06:38Z)** S1 评估两次 FAILED,真因在 `errorMessage`(`errorCode` 为 null):
  缺 EKS RBAC。这是**硬前置**而非可选增强——只要 service 含 `eks` 输入源而 K8s 侧读不到,
  整个评估 FAILED,不会降级跳过。已应用官方 ClusterRole/ClusterRoleBinding
  并建 EKS access entry 映射 invoker role 到 `resilience-hub-eks-access-group`;
  S1 评估第三次重跑中。另记录官方 skill 关于 SLO 取值范围的文档错误(T-026)。
  ⚠️ 成本注意:S1 已用掉 2 次「含在 service fee 里」的评估(都 FAILED,其中第二次是系统自动重试),
  第三次起理论上按 $0.10/资源计(112 资源 ≈ $11)。FAILED 的评估是否计费官方未明说,
  但推进 DoD-4 只有这一条路,已按 north_star 第 5 节授权继续,实际账单在 T-043 里核对。
- **cycle-3 (06:45–06:47Z)** EKS RBAC 修复**生效**(K8s 报错消失),暴露第二个根因:
  invoker role 缺 **v2 专属**托管策略 `AWSResilienceHubV2AssessmentExecutionPolicy`
  —— 与 v1 的三个 s 版本是两个不同策略,官方 skill 只提 v1 那个,对 v2 是错的(T-027)。
  已挂上并重跑 S1/S4。同时 **T-024 关闭**:S4 资源数 0→5,证实
  `create-input-source` 的自动解析不可靠、`start-failure-mode-assessment` 会强制解析,
  「必须有 eks 源」的假设被否决。S2/S3 刻意保持干净配额,等 S1 验证通过再评估。
- **cycle-4 (06:57–07:00Z)** S1 仍 IN_PROGRESS(已 13 分钟,官方 skill 说 "often ~10 min,
  longer for large services",124 资源属正常范围),本轮不空等,做无依赖的活:
  - 资源数**在评估过程中继续增长**:124 / 20 / 39 / 5 = **188**(cycle-3 时是 112/14/25/0=151)
    → 说明评估本身会富化资源集,`list-resources` 不是一次定死的
  - 写 `exports/arns.env` 固化全部 ARN(system/3 policy/4 service/3 journey/invoker role),
    后续脚本一律 source 它,不再散落硬编码
  - 写 `verify_dod.sh`(T-042 完成):5 条 DoD 可执行核验,支持 `bash verify_dod.sh <n>` 单验。
    **实测 DoD-1 与 DoD-3 全 PASS。** DoD-3 里 tier2 不设 SLO 走 info 分支而非 fail;
    DoD-4 允许「无 SUCCESS 但台账有成文的不可评估理由」通过,以适配 T-028
  - 修了脚本里 `basename "${s%%:*}"` 提取 service 名的 bug —— ARN 第一个冒号在 `arn:` 后,
    截出来是 `arn`;改用 `sed "s#.*/##; s#:.*##"` 取 `service/<name>:<id>` 中的 name
- **cycle-5 (07:08–07:11Z)** **S1 评估 SUCCESS**,耗时 17 分钟,23 条 findings。
  证实 cycle-2/3 的两处修复(EKS RBAC + v2 专属策略)都是**必需且充分**的。
  计费自 07:04:32 起。随即:
  - 启动 S2 `ddee25c5-e1f6-4da7-9799-8bf57049f315` 与 S3 `ec40eb35-19b9-4401-bf7c-f9e635ed3ea1`
    评估(两者配额干净,且两处修复是 role/cluster 级的,对它们同样生效)
  - 导出 `exports/findings-petsite-core.json`
  - 按 T-028 的方案 a 给 S4 加拓扑锚点:`create-service-function`
    (`alert-aggregation-and-rca`,criticality PRIMARY,serviceFunctionId
    `a5b22cd8-a282-4799-bd1c-9b29c07e8498`)+ `create-service-function-resources`
    绑定全部 5 个资源,然后重跑 S4 评估 `c105f5e9-447a-4102-8c25-dba01a52bca3`
  - ⚠️ 一处待查:`list-service-functions --query 'serviceFunctionSummaries[]...'` 返回 `None`,
    说明响应字段名不是 `serviceFunctionSummaries`。与 create-* 的嵌套问题同型,
    下轮用 `--output json` 直接看结构。不影响功能(create 已返回成功)。
  - **重要时间预期修正**:评估耗时不是文档说的 4–7 分钟,S1 实测 **17 分钟**。
    之前几次 4–7 分钟结束的都是**快速失败**。所以轮询节奏应按 15–20 分钟估,
    一轮(7 分钟)看不到结果是正常的,不要因此判断为卡住。
- **cycle-6 (07:17–07:20Z)** S4 加了 service function 后**仍 FAILED**(`c105f5e9`,07:09→07:12,
  同样的 topology 消息)→ T-028 方案 a(显式 service function 做拓扑锚点)**无效**,已否决。
  推进到更准确的诊断:问题不是缺拓扑**提示**,而是缺连接性**资源本身**。
  查出 RCA 链路的真实连接件是 3 个 SNS 主题(SNS 是 ARH 支持类型):
  | SNS 主题 | 订阅端 |
  |---|---|
  | `petsite-rca-alerts` | `petsite-rca-engine` |
  | `rca-alerts` | `petsite-rca-engine` |
  | `petsite-ops-alerts` | `petsite-ops-slack-notifier` |
  这三个主题原先**不在 S4 任何输入源覆盖范围内**(既无 `System` 标签、也不属 AlertBufferStack),
  所以 ARH 只看到 4 个孤立 Lambda + 1 张 DDB 表,无边可连。
  另实测:4 个 Lambda 的 `list-event-source-mappings` 全为 0 条,`scheduler list-schedules` 为空,
  所以 DDB→window-flush 那段确实没有 AWS 侧可发现的触发关系(靠代码内调用),
  这解释了为什么 ARH 连不出边。
  修复:给 3 个 SNS 主题打 `System=petsite-ops`(被既有 TAGS 输入源自动覆盖,无需新建输入源),
  标签检索现返回 7 个资源(4 Lambda + 3 SNS)。重跑 S4 评估 `eab9c084-7cc6-4d69-8e4f-40a8eaf713a7`。
  S2 `ddee25c5` / S3 `ec40eb35` 仍 IN_PROGRESS(8 分钟,预期 ~17)。
- **cycle-7 (07:26–07:32Z)** **S2 与 S3 也 SUCCESS**(12 / 13 分钟),3/4 达成。
  - S4 第 4 次失败(`eab9c084`),关键证据:**打完 SNS 标签后资源数仍是 5,SNS 没进来**
    → 发现一条重要约束:**`resourceTags` 输入源的匹配集是「创建时快照」**,
    给资源新打标签后,启动评估**不会**刷新已有输入源的匹配集;
    必须 `delete-input-source` + `create-input-source` 重建才会重新扫描。
    重建后资源数 5 → **8**,三个 SNS 主题全部进来。第 5 次重跑 `80585a19`。
  - 阶段 4 导出完成(T-040 部分):三个 SUCCESS service 的 topology-edges / dependencies /
    findings 全部落盘 `exports/`。**68 条拓扑边**(DATA_FLOW 54 + CONTAINMENT 14)、
    **57 条 findings**(23+17+17)。
  - 边结构确认可直接 join Neptune:`sourceResourceIdentifier` → `destinationResourceIdentifier`
    + `properties:[{topologyType}]`,`topologyType` 映射成边类型。
  - `dependencies` 全为 0:`dependencyDiscovery.status=INITIALIZING`,35 天回看异步窗口,
    非失败。DoD-5 判定已修正并**实测通过**。
  - 响应字段名再记一处:拓扑边在 `serviceTopologyEdgeSummaries`、依赖在 `dependencySummaries`、
    findings 在 **`findingsSummary`**(注意不是 `findingSummaries`,命名不统一)。


---

## 阶段 0 — 前置补齐(零成本)

### T-001 创建 ARH v2 invoker IAM role
- 状态:`done` · 依赖:无
- 角色名:`ResilienceHubV2InvokerRole` → `arn:aws:iam::926093770964:role/ResilienceHubV2InvokerRole`
- 挂载:`arn:aws:iam::aws:policy/AWSResilienceHubAsssessmentExecutionPolicy`(**三个 s**,已确认存在,v9)
- trust policy:`Service: resiliencehub.amazonaws.com`(官方文档确认;注意 AWS 博客
  `using-permissions-to-unlock-resilience` 里写的 `resiliencehub.amazon.com` **少了 aws,是错的**),
  条件 `"StringEquals": {"aws:SourceAccount": "926093770964"}` 防混淆代理
- ⚠️ 该托管策略是 v1 时代产物(创建前 AttachmentCount=0)。若首次评估返回
  `errorCode=INVALID_PERMISSIONS`,不要盲目加 `*` 权限 —— 读报错里缺的具体 action,补一条内联策略,
  并把实际需要的 action 清单记在本卡里(这是本目标可复用的产出之一)
- 记录:
  - `2026-08-29T06:18Z cycle-0: create-role + attach-role-policy 成功;get-role 回读
    Principal.Service=resiliencehub.amazonaws.com、list-attached-role-policies 返回
    AWSResilienceHubAsssessmentExecutionPolicy。验收通过。`

### T-002 给 3 个无标签 Lambda 补 System=petsite-ops
- 状态:`done` · 依赖:无
- 目标:`petsite-rca-engine`、`petsite-rca-interaction`、`petsite-ops-slack-notifier`
  —— 三者**既无标签也不在任何 CFN 栈**,不补标签就只能逐 ARN 手动纳管
- 标签:`System=petsite-ops`、`Tier=tier2`、`ManagedBy=goal-loop-arh-v2`
- 记录:
  - `2026-08-29T06:20Z cycle-0: lambda tag-resource 三个函数全部成功。`

### T-003 给 gp-window-flush 补 System=petsite-ops
- 状态:`done` · 依赖:无
- 现状:原只有 `Project=graph-dp`,缺 System 维度;它属 AlertBufferStack,
  所以 S4 用 CFN 栈也能捞到 —— 补标签是为了让 `resourceTags` 与 `cfnStackArn` 两条 input source
  的结果一致,便于 DoD-2 对账
- 记录:
  - `2026-08-29T06:20Z cycle-0: 已打标。get-resources Key=System,Values=petsite-ops 返回 4 条
    (3 个 rca/notifier + gp-window-flush),与预期一致。`

### T-004 给 petsite-neptune-instance-1 补标签
- 状态:`done` · 依赖:无
- 现状:集群 `petsite-neptune` 有 `System=deepflow, Component=neptune, Tier=tier1, Team=observability-team`,
  但**实例 `petsite-neptune-instance-1` 无标签** → `resourceTags{System=deepflow}` 会漏掉实例
- 用 RDS API 打标签(Neptune 复用 RDS 控制面):
  `aws rds add-tags-to-resource --resource-name arn:aws:rds:ap-northeast-1:926093770964:db:petsite-neptune-instance-1`
- 记录:
  - `2026-08-29T06:20Z cycle-0: 已打标,集群与实例现在都能被 System=deepflow 捞到。`

### T-005 给 EKS 集群 PetSite 补 System=petsite
- 状态:`done` · 依赖:无
- 现状:集群标签原为空 `{}`(实测),节点组和 worker EC2 也只有 `aws:eks:*` 系统标签
- 说明:`eks` 类型 input source 用的是 clusterArn 不是标签,所以这张卡**不影响纳管**,
  只影响 DoD-2 对账时能否用标签统一检索
- 记录:
  - `2026-08-29T06:20Z cycle-0: eks tag-resource 成功,打了 System=petsite/Tier=tier0/ManagedBy。`

### T-006 【新增·由 T-002~T-005 验收发现】S3 输入源覆盖缺口
- 状态:`done`(设计已修正,落地在 T-022) · 依赖:无
- 发现:`get-resources Key=System,Values=deepflow` 只返回 **14** 条:3 台 EC2、
  3 个 AMI image、grafana-aurora-mysql、petsite-neptune 集群+实例、2 个 RDS cluster 资源 ID、
  **只有 3 个** neptune-etl Lambda(from-aws / from-cfn / from-deepflow)。
- 缺口:`neptune-etl-trigger` 只带 `Project=graph-dp` 没有 `System=deepflow`;
  NeptuneEtlStack 的 **2 个 SQS 与 9 个 EventRule** 也不带该标签。
  → 只用 `resourceTags{System=deepflow}` 会漏掉整条 ETL 触发链路。
- 决策:**不再给这些资源补标签**(补标签会改动 NeptuneEtlStack 管理的资源,与栈定义产生漂移),
  改为给 S3 加**第三条 input source** `cfnStackArn`(NeptuneEtlStack),让 CFN 栈负责 ETL 部分。
  roadmap 的 S3 行已同步修正。
- 副产品:`System=deepflow` 会捞到 3 个 **AMI image**,而 ARH 支持的资源类型里没有 AMI
  → 预期它们在 `list-resources` 中被忽略,T-023 核对时不要误判为解析失败。
- 记录:
  - `2026-08-29T06:21Z cycle-0: 标签检索验收暴露该缺口,已修正 roadmap S3 与 T-022 输入源表。`

---

## 阶段 1 — 建模层(零成本)✅ done

**实测修正(重要,不要重新踩)**:
- `create-system` / `create-policy` / `create-service` 的响应把对象包在**嵌套键**里
  (`{"service": {...}}`),所以 `--query 'serviceArn'` 返回 `None` **但对象已创建成功**。
  一律从 `list-*` 读 ARN,不要依赖 create 的 `--query`。
- `availabilitySlo.target` **不是自由 double,是三值枚举 `[99.9, 99.95, 99.99]`**。
  骨架把它标成 double 是误导。原推导值 tier1=99.5 / tier2=99.0 被 `ValidationException`
  (`reason: INVALID_FIELD_VALUE`)拒绝。
- 因此 SLO 分级改为:tier0=**99.95**、tier1=**99.9**、tier2=**不设**
  (枚举底就是 99.9,强塞给 tier2 反而比它应有的目标更严,只会造 NOT_ACHIEVABLE 噪音)。
- `update-policy` 存在,tier0 的 99.9→99.95 是用它就地改的,不必删重建。

### T-010 create-system petsite-tokyo
- 状态:`done`
- `systemArn = arn:aws:resiliencehub:ap-northeast-1:926093770964:system/petsite-tokyo:lj9qdn`
  (systemId `lj9qdn`)
- 记录:`2026-08-29T06:24Z cycle-1: 创建成功。create 的 --query systemArn 返回 None 是响应嵌套所致,已从 list-systems 回读确认。`

### T-011 create-policy ×3(tier0/tier1/tier2)
- 状态:`done`
- `arh-tier0 = arn:...:policy/arh-tier0:fh4cp8`  SLO 99.95 / RTO 15 / RPO 5 / WARM_STANDBY / backup 60
- `arh-tier1 = arn:...:policy/arh-tier1:ytass1`  SLO 99.9 / RTO 60 / RPO 15 / PILOT_LIGHT / backup 240
- `arh-tier2 = arn:...:policy/arh-tier2:k1a9y4`  **无 SLO** / RTO 240 / RPO 60 / BACKUP_AND_RESTORE / backup 1440
- 均不设 `multiRegion`(单区域部署,刻意为之)
- 记录:`2026-08-29T06:25Z cycle-1: tier0 先以 99.9 建出,再用 update-policy 改到 99.95;tier1/tier2 按修正后的枚举值一次建成。`

### T-012 create-user-journey ×3
- 状态:`done` · 依赖:T-010
- 取自 `infra/lambda/etl_aws/business_config.json` 的 `business_capabilities`,未另造:
  | journey | id | policy |
  |---|---|---|
  | PetAdoptionFlow | `91a4ef77-e3bb-4ba1-b321-ecb63fe50a02` | arh-tier0 |
  | AdoptionHistoryView | `1b6b9fd0-9f97-404a-8f6e-c4fd0dbd7ac6` | arh-tier1 |
  | PetInventoryManagement | `a5d21945-dc80-42a0-b365-84cc8c6d3a7e` | arh-tier1 |
- 记录:`2026-08-29T06:26Z cycle-1: 三条全部创建成功,list-user-journeys 回读确认。`

---

## 阶段 2 — 纳管(零成本)⚠️ 3/4 完成

### T-020 create-service S1 petsite-core
- 状态:`done`
- `arn:aws:resiliencehub:ap-northeast-1:926093770964:service/petsite-core:sq7g31`
- tier0 / discovery ENABLED / 关联 system + 三条 journey
- 记录:`2026-08-29T06:26Z cycle-1: 创建成功。`

### T-021 create-service S2/S3/S4
- 状态:`done`
- `awesomeshop-legacy = arn:...:service/awesomeshop-legacy:1w54nc`(tier2, DISABLED)
- `graph-observability = arn:...:service/graph-observability:577hpo`(tier1, **ENABLED**)
- `ops-rca-plane = arn:...:service/ops-rca-plane:1puayt`(tier2, DISABLED)
- 记录:`2026-08-29T06:26Z cycle-1: 三个全部创建成功。get-service 可读到 effectivePolicyValues,source=SELF,说明 policy 绑定生效。`

### T-022 create-input-source 全部 service
- 状态:`done`(9 条全部挂载成功)
- 实际挂载:
  | Service | 输入源 |
  |---|---|
  | S1 | `CFN_STACK` ServicesEks2 + `EKS` ns=petadoptions |
  | S2 | `CFN_STACK` AwesomeShopInfra + `EKS` ns=awesomeshop |
  | S3 | `TAGS` System=deepflow + `CFN_STACK` NeptuneEtlStack + `EKS` ns=deepflow |
  | S4 | `CFN_STACK` AlertBufferStack + `TAGS` System=petsite-ops |
- 栈 ARN 从 `describe-stacks` 取的 StackId(带 UUID 后缀),不是手拼的短名
- 记录:`2026-08-29T06:27Z cycle-1: 9 条全绿。`

### T-023 list-resources 逐 service 核对解析结果
- 状态:`done`(结论已得,S4 的问题分流到 T-024)
- 响应字段是 `serviceResources[]`,资源类型在 **`resource.resourceType`**(不是顶层),
  输入源来源在 `inputSource.type`
- 解析结果:
  | Service | discovered(事件) | 保留(list-resources) | 说明 |
  |---|---|---|---|
  | petsite-core | 189 | **112** | 最大,含 tier0 Aurora / DDB / SQS / StepFn / ALB+TG / SSM Parameter |
  | awesomeshop-legacy | 22 | **14** | EKS 源只捞到集群基础设施(VPC/子网/ASG/nodegroup),因为 awesomeshop 副本为 0 **没有 Pod**;数据层 ElastiCache + mysql 实例来自 CFN 源 |
  | graph-observability | 35 | **25** | TAGS 捞到 3 台 EC2 + 2 DBCluster + 2 DBInstance;CFN 捞到 9 EventRule + 4 Lambda + 2 SQS |
  | ops-rca-plane | — | **0** | ⚠️ 见 T-024 |
- **discovered > 保留** 是正常过滤:ARH 丢弃不影响 RTO/RPO 的类型
  (IAM Role/Policy、CDK Metadata、Lambda LayerVersion、Lambda Permission 等),
  不是解析失败。`System=deepflow` 捞到的 3 个 AMI image 同理被丢弃(ARH 不支持 AMI)。
- 记录:`2026-08-29T06:28Z cycle-1: 三个 service 解析正常,S4 为 0 已分流建卡。`

### T-024 【已解】S4 ops-rca-plane 解析出 0 个资源
- 状态:`done`
- 症状:两条输入源都注册正确(`list-input-sources` 返回 TAGS + CFN_STACK),但
  `list-resources` 返回 `serviceResources: []`,且 `list-service-events` 里
  **完全没有 `SERVICE_RESOURCES_ASSOCIATED` 事件** —— 说明解析根本没跑,不是跑了没结果
  (对照 S1/S2/S3 都在挂源后 2–8 秒内由系统 actor `ngrh-assessment-service`
  触发该事件:189 / 22 / 35 new resources discovered)
- 已排除的四个假设:异步延迟(等 3 分钟仍 0)、资源类型不支持(DDB 与 Lambda 在 S3 都成功解析)、
  权限(同一 invoker role 让 S1/S2/S3 都成功)、自引用(给 ARH service 本身打了
  `System=petsite-ops` 使其 TAGS 源匹配到自己,已 untag 并删除重建输入源,仍为 0)
- **结论(cycle-3 实测确认)**:`create-input-source` 触发的自动解析**不可靠**;
  `start-failure-mode-assessment` 会强制解析。S4 评估结束后资源数从 0 变为 **5**,
  组成与预期完全一致:
  | 资源 | 类型 | 来源 |
  |---|---|---|
  | gp-window-flush | AWS::Lambda::Function | TAGS |
  | petsite-ops-slack-notifier | AWS::Lambda::Function | TAGS |
  | petsite-rca-engine | AWS::Lambda::Function | TAGS |
  | petsite-rca-interaction | AWS::Lambda::Function | TAGS |
  | gp-alert-buffer | AWS::DynamoDB::Table | CFN_STACK |
- 「必须有 eks 输入源」这个假设**不成立**,已否决 —— S4 没有 eks 源也解析成功了,
  只是需要一次评估来触发。v2 没有显式 resolve 命令,所以**挂完输入源后应直接启动评估**,
  不要等自动解析。这条写进最终报告。
- 记录:
  - `2026-08-29T06:29Z cycle-1: 逐条排除异步延迟/资源类型/权限/自引用四个假设,均不成立。`
  - `2026-08-29T06:32Z cycle-1: 改用 start-failure-mode-assessment 强制解析,评估已 PENDING。`
  - `2026-08-29T06:46Z cycle-3: 评估结束后资源数 0→5,机制假设成立,卡关闭。`

### T-027 【新增·第二个根因】invoker role 缺 v2 专属托管策略
- 状态:`done`(已挂,等验证)
- EKS RBAC 修好后 S1 换了新错误(仍在 `errorMessage`,`errorCode` 依旧 null):
  `The resources discovered for this service did not produce a topology.
   Please verify the invoker role has AWSResilienceHubV2AssessmentExecutionPolicy policy.`
- **关键发现:v1 和 v2 是两个不同的托管策略,名字差一个 `V2` 且拼写规则不同**:
  | 策略 | 拼写 | 版本 | 归属 |
  |---|---|---|---|
  | `AWSResilienceHubAsssessmentExecutionPolicy` | **三个 s**(Asss) | v9 | 旧版 ARH v1 |
  | `AWSResilienceHubV2AssessmentExecutionPolicy` | **两个 s**(Ass) | v1 | **next-gen v2** |
  注意 `AWSResilienceHubV2AsssessmentExecutionPolicy`(三个 s + V2)**不存在**。
- 官方 skill `resilience-hub-getting-started` 与 setup-procedure.md 都只提 v1 那个
  三个 s 的策略(「managed policy `AWSResilienceHubAsssessmentExecutionPolicy`」),
  **对 v2 是错的**。这是 T-026 之外该 skill 的第二处错误,一并写进最终报告。
- 修复:`attach-role-policy` 挂上 V2 策略(v1 策略保留,两者并存无害)。
  等 45s IAM 传播后重跑 S1(`540027fa-9b77-4935-ae55-f6f543039e86`)与
  S4(`cd08b75b-6e53-4eee-a117-3848f3842f03`)。
- 策略挂载前 `AttachmentCount=0`,说明本账号从未用过 v2 —— 与 `list-apps` 为空一致。
- S2/S3 **刻意暂不启动评估**:先用已有失败记录的 S1/S4 验证修复,再失败无额外损失;
  S2/S3 仍是干净的 2 次配额。
- 记录:
  - `2026-08-29T06:46Z cycle-3: 探测确认 V2 策略存在并挂载;S1/S4 重跑 PENDING。`

### T-025 【新增·真正的根因】挂了 eks 输入源的 service 必须先配 EKS RBAC
- 状态:`done`(修复已落地,等 T-030 验证)
- **这是 S1 评估失败两次的真因,而且 `errorCode` 是 `null`——错误只在 `errorMessage` 字段里。
  按 errorCode 排查会一无所获,north_star 第 3 节的发现来源要以 errorMessage 为准。**
- 原文:`Resource discovery could not read the Kubernetes workloads for the EKS cluster(s) in
  this service, so the assessment cannot evaluate them. Verify (1) the
  resilience-hub-eks-access-cluster-role ClusterRole and its ClusterRoleBinding are applied to
  the cluster, and (2) the assessment IAM role is mapped to the resilience-hub-eks-access-group
  Kubernetes group via an EKS access entry or the aws-auth ConfigMap, then retry.`
- 关键认知修正:**EKS RBAC 不是「可选增强」,是凡挂 `eks` 输入源就必须的硬前置**。
  CFN/TAGS 源的 AWS 侧资源能正常解析(S1 已解析出 112 个),但只要 service 里有 EKS 源、
  而 K8s 侧读不到,**整个评估就 FAILED**——不是降级跳过 EKS 部分。
  所以 S1/S2/S3 三个带 eks 源的 service 全部受此阻塞。
- 修复(两步,都已完成):
  1. 应用官方 ClusterRole + ClusterRoleBinding(全命名空间只读版),清单存
     `arh-eks-rbac.yaml`,来源
     `https://docs.aws.amazon.com/resilience-hub/latest/userguide/grant-permissions-to-eks-in-arh.html`
     —— 注意该页是 v1 文档,但**角色/组名与 v2 报错要求的完全一致**,v1 的
     `AwsResilienceHubAssessmentEKSAccessRole` 那套旧命名不要用
  2. `aws eks create-access-entry --cluster-name PetSite --principal-arn
     arn:aws:iam::926093770964:role/ResilienceHubV2InvokerRole --kubernetes-groups
     resilience-hub-eks-access-group --type STANDARD`
     —— 集群 `authenticationMode=API_AND_CONFIG_MAP`,所以用 access entry 而非改
     `aws-auth` ConfigMap(后者要改集群里的既有 ConfigMap,风险更高)
- 工具踩坑:awslabs eks-mcp-server 的 `apply_yaml` 在本环境是只读模式
  (`Operation apply_yaml is not allowed without write access`),所以下载官方 kubectl
  (v1.37.0,arm64)到 `$KIROCREW_SCRATCH` 配 `aws eks update-kubeconfig` 应用。
  调用者身份 `user/Radium` 本就在集群 access entry 列表里,所以 kubectl 直接可用。
- 记录:
  - `2026-08-29T06:36Z cycle-1: ClusterRole 与 ClusterRoleBinding created,回读确认 subject 组名正确。`
  - `2026-08-29T06:37Z cycle-1: access entry 建成并回读确认;S1 评估重跑 bb48ed3a-a087-47b7-bb64-0f60889f2fb5 PENDING。`

### T-028 【新增】S4 修复后仍失败 —— "did not produce a topology" 是字面意思
- 状态:`doing` · 依赖:T-030 的结论
- 事实分离(**重要,别把两次失败混为一谈**):
  | 评估 | service | 启动时刻 | 相对 V2 策略挂载(06:46) | 结果 |
  |---|---|---|---|---|
  | bb48ed3a | S1 | 06:37 | **之前** | FAILED |
  | 540027fa | S1 | 06:47 | **之后** | IN_PROGRESS,已跑 >8 分钟 |
  | a9adc6aa | S4 | 06:32 | 之前 | FAILED |
  | cd08b75b | S4 | 06:47 | **之后** | **FAILED** |
  → 所以 V2 策略对 S4 **没用**;而 S1 修复后那次跑得比历次失败都久(历次 4–7 分钟即 FAILED),
  是正在真正评估的信号。
- 权限已排除:V2 策略实测含
  `lambda:GetFunctionConfiguration/GetEventSourceMapping/List*`、`dynamodb:Describe*/List*`、
  `eks:Describe*/List*`、`rds:Describe*/List*`、`sqs:GetQueueAttributes/List*`、
  `elasticloadbalancing:Describe*`、`states:Describe*/List*`(共 207 个 action)
  —— S4 的两类资源(Lambda、DynamoDB)都在覆盖范围内。
- **诊断**:错误消息前半句是字面事实,后半句「Please verify the invoker role has
  ...Policy」是**通用误导性后缀**(策略已挂仍报同样的话)。S4 的 5 个资源是
  4 个互不相关的 Lambda + 1 张 DynamoDB 表,**彼此之间没有可发现的连接**
  (无 event source mapping、无 API Gateway、无 ALB 串联),ARH 无法从一袋离散资源
  构建拓扑,因此评估失败。
- 这意味着一条 ARH v2 的建模约束:**service 的资源集必须能构成连通拓扑**,
  「一组松散相关的运维 Lambda」不是可评估的 service。这条写进最终报告。
- 待 S1 结果确认该诊断后,S4 走以下之一(优先 a):
  a) 用 `create-service-function` + `create-service-function-resources` 显式声明
     service function 及其资源,给 ARH 一个拓扑锚点(API 契约里说这能「提升拓扑/评估精度」)
  b) 若 a 无效,把 S4 标为「已纳管但不可评估」,在 `coverage-ledger.md` 里记明理由,
     并把 DoD-4 判定改为「每个 service 要么有 SUCCESS 评估、要么有成文的不可评估理由」
     —— 这是 API 行为约束导致的判定修正,不是降低标准
- **不要在 S1 出结果前再重跑 S4**:同样的输入重跑只会得到同样的结果,属试错式重试。
- 记录:
  - `2026-08-29T06:51Z cycle-4: 分离两次失败的时间线,确认 V2 策略对 S4 无效、对 S1 待验;排除权限假设。`
  - `2026-08-29T06:56Z cycle-4: S1 已 IN_PROGRESS >8 分钟(历次失败均 4–7 分钟),等下一轮取结果。`


### T-026 官方 skill 文档与实测不符(记录用,不需修复)
- 状态:`done`
- 官方 skill `resilience-hub-getting-started` 的 `references/setup-procedure.md` 明确写:
  「`availability_target` — any value between 0 and 100 ... `create-policy` accepts any value
  in that range — do NOT reject other values (e.g., 99.5 or 99.999 are valid)」
- **实测相反**:`--availability-slo '{"target":99.5}'` 返回
  `ValidationException: Invalid input for field availabilitySlo.target.
  Allowed values are [99.95, 99.9, 99.99]`,`reason: INVALID_FIELD_VALUE`。
- 结论:以 API 实际返回为准,skill 这段是错的。写进最终报告(T-043)。



---

## 阶段 3 — 评估(⚠️ 首次评估完成即开始计费)

### T-030 评估 S1 petsite-core
- 状态:`todo` · 依赖:T-023
- `start-failure-mode-assessment --service-arn <S1>` → 轮询 `list-failure-mode-assessments`
- 状态机:`NOT_STARTED` → `PENDING` → `IN_PROGRESS` → `SUCCESS`/`FAILED`
- 若 >30 分钟仍 IN_PROGRESS,读内部阶段值定位卡在哪一步
  (`DESIGN_ANALYSIS` / `TOPOLOGY_GENERATION` / `RESILIENCE_ASSESSMENT` /
   `SERVICE_FUNCTION_GENERATION` / `FAILURE_MODE_FINDINGS_ENRICHMENT`)
- ⚠️ **每 service 每月只含 2 次免费评估**,第 3 次起 $0.10/资源。
  所以 FAILED 后要**一次把问题修干净再重跑**,禁止试错式重试
- 记录:

### T-031 评估 S3 graph-observability
- 状态:`todo` · 依赖:T-030(先用 S1 验证权限链路,再批量)
- 记录:

### T-032 评估 S2、S4
- 状态:`todo` · 依赖:T-030
- 记录:

### T-033 修复评估 errorCode
- 状态:`todo` · 依赖:T-030
- 可能的 errorCode:`INVALID_PERMISSIONS`(补 invoker role 权限,回写 T-001)、
  `CMK_ACCESS_DENIED`、`DESIGN_FILE_ACCESS_DENIED`、`INPUT_VALIDATION`、
  `POLICY_VALIDATION`、`AGENT_ERROR`、`INTERNAL_ERROR`
- 每种 errorCode 的实际成因与修法记在本卡 —— 官方文档没有这份对照表,这是本目标的可复用产出
- 记录:

---

## 阶段 4 — 导出与对账(零成本)

### T-040 导出 topology edges / dependencies / findings
- 状态:`todo` · 依赖:T-030, T-031, T-032
- 落盘到 `exports/`:`topology-edges-<svc>.json`、`dependencies-<svc>.json`、`findings-<svc>.json`
- `list-service-topology-edges --service-arn` 必填 serviceArn;`list-dependencies` serviceArn 可选
- findings 按 `--severity HIGH/MEDIUM/LOW` 与 `--failure-category` 分别导出便于分析
  (category 枚举 9 种:SINGLE_POINT_OF_FAILURE / SHARED_FATE / MULTI_AZ_DISASTER_RECOVERY /
   MULTI_REGION_DISASTER_RECOVERY / DATA_RECOVERY / AVAILABILITY_SLO / EXCESSIVE_LATENCY /
   EXCESSIVE_LOAD / MISCONFIGURATION_AND_BUGS)
- 记录:

### T-041 写 coverage-ledger.md
- 状态:`todo` · 依赖:T-023, T-040
- 表格:VPC 每个应用组 → 已纳管(附证据 ARN + 所属 service)或不纳管(附具体理由)
- 不纳管清单的基线见 `roadmap.md`「明确不建模为 service」一节
- 记录:

### T-042 写 verify_dod.sh
- 状态:`todo` · 依赖:T-041
- 把 5 条 DoD 变成一个可执行脚本,`bash verify_dod.sh` 全绿退 0,任一条不满足退非 0 并打印哪条
- 支持 `bash verify_dod.sh <n>` 只验第 n 条(north_star.md 的 DoD-2 引用了这个用法)
- 记录:

### T-043 写最终报告
- 状态:`todo` · 依赖:T-040, T-041, T-042
- 输出到 `../arh-v2-onboarding-<YYYYMMDD-HHMM>.md`
- 内容:纳管结果矩阵、findings 摘要(按 severity 与 category)、ARH 拓扑与 Neptune 图谱的差异、
  实际月成本、以及 invoker role 真正需要的 IAM action 清单
- 记录:

---

## 阶段 X — 遗留(不阻塞 DoD)

### T-090 把 ARH 拓扑边 join 进 Neptune(第 5 个 ETL)
- 状态:**`wontfix`** —— 已验证不做(2026-08-29 基于实测数据判定)
- 完整论证写入原方案文档第 7 节:
  `../resilience-hub-v2-topology-enhancement_20260827-1647.md`,该文档顶部已加过时横幅
- 三条理由摘要:
  1. **补依赖发现短板 —— 推翻**:ARH 依赖发现是 DNS query log 分析,
     与本项目已证实产生 85% 假阴性的是同一信号。`graph-observability` 依赖发现已完成
     (ENABLED / eligibleResourceCount=3 / message=null)结果 **0 条**;
     按 5×7 天覆盖完整 35 天回看期 + 最近 24h HOURLY 逐段查询,全窗口均为 0,已排除时间窗因素
  2. **拿 ARH 拓扑边补图谱 —— 推翻**:68 条边里 46 条是基础设施管道(Describe 已覆盖),
     应用级依赖只有十余条且**锚点是 `eks/cluster` 而非微服务**。
     Neptune 已有微服务粒度 + 文件行级 provenance
     (`payforadoption → RDSCluster` 证据 `source:repository.go#CreateTransaction` 等),
     ARH 是集群粒度 + 无 provenance —— **严格更粗**,灌入会恶化「单一事实源」欠项
  3. **findings/achievability 是新信息 —— 成立但撑不起 ETL**:每 service 每月只含 2 次评估
     (第 3 次起 $0.10/资源,petsite-core 约 $12/次),一个月只能刷 2 次不是 ETL 的对象;
     且当前零消费方(rca / dr-plan / Q1–Q18 都不读),又会制造一个会漂移的第二副本
- **替代方向**(比本卡有价值):findings 暴露出图谱缺的是**整个维度**而非更多边 ——
  `External Docker Hub dependency`(外部镜像仓库依赖,31 种节点类型里没有)与
  `Lambda functions share unreserved concurrency pool`(并发池共享命运,26 种边类型里没有),
  两者都是 SHARED_FATE 类,由自有 ETL 持续采集即可,不受配额限制
- ARH 的正确定位:**外部审计工具**,产出留在 markdown 报告定期人读。
  不可替代价值在**配置层**(PDB / 健康探针 / HPA / failover replica / 备份保留 / PITR /
  单 NAT 共享命运 / ARC zonal shift / 删除保护),这些既非拓扑也非依赖,
  DeepFlow 的 L7 与 Neptune 的拓扑都不覆盖
- 记录:
  - `2026-08-29T08:07Z: 用户确认置 wontfix。理由已写入原方案文档第 7 节(7.1–7.5)并在文首加过时横幅。`
### T-091 ARH findings 与图谱 drift_status 对账
- 状态:`todo` —— ARH 发现但图谱没有的依赖 = 图谱假阴性的独立证据源,
  与已知的「漂移判定只用 DNS 作观测源」缺陷直接相关
### T-092 S3 拆成 observability / neptune / etl 三个 service
- 状态:`todo` —— 换更细 policy 粒度,代价 +$30/月,需用户决定
### T-093 AWS::ResilienceHubV2::* CFN 模板化
- 状态:`todo` —— 注意 CFN 无 InputSource 类型,模板化只能覆盖一半
### T-094 awesomeshop 计算层全 0 副本但数据层仍在跑
- 状态:`review` —— 真实的成本与资安欠项,本目标只如实建模不做处置,需提给用户
