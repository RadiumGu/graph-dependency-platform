# Roadmap — 分阶段路线

> 每轮由代理重读。阶段内可并行,阶段间按序(后一阶段依赖前一阶段的产出 ARN)。
> 资产依据:`../../` 会话中三路发现的实测结果(见 `north_star.md` 第 4 节事实表)。

---

## 服务切分决策(先看这个,后面所有阶段都基于它)

任务 B 提出 6 个候选 service(S1–S6)。**本目标收敛为 4 个**,理由是 ARH v2 按 service
计费($15/月起),而 S3/S4/S5 三组资源共享同一个 `System=deepflow` 标签、可用**一个** input
source 一次性覆盖,拆成 3 个 service 只换来更细的 policy 粒度、多花 $30/月。
拆分是后续可逆的精化动作(`create-service` 再挂一个更窄的 input source 即可),不是现在必须做的。

| # | Service 名 | 组成 | input source | Tier / policy | 依赖发现 |
|---|---|---|---|---|---|
| **S1** | `petsite-core` | EKS `petadoptions` ns 全部 Deployment、PetSite ALB + TG、petadoption DDB、2 SQS、StepFn、**tier0 Aurora PG** | `cfnStackArn`(ServicesEks2)**+** `eks{PetSite,[petadoptions]}` 两条 | tier0 | **ENABLED** |
| **S2** | `awesomeshop-legacy` | EKS `awesomeshop` ns(6 Deployment 全 0 副本)、mysql 单实例、ElastiCache | `cfnStackArn`(AwesomeShopInfra)+ `eks{PetSite,[awesomeshop]}` | tier2 | DISABLED |
| **S3** | `graph-observability` | deepflow-server / grafana-x86 / nfm-test 三台 EC2、grafana-aurora-mysql、**petsite-neptune** 集群+实例、4 个 neptune-etl Lambda + trigger + 2 SQS + 9 EventRule、EKS `deepflow` ns | `resourceTags{System=deepflow}` **+** `cfnStackArn`(NeptuneEtlStack) **+** `eks{PetSite,[deepflow]}` 三条 | tier1 | **ENABLED** |
| **S4** | `ops-rca-plane` | gp-alert-buffer DDB、gp-window-flush、petsite-rca-engine、petsite-rca-interaction、petsite-ops-slack-notifier | `cfnStackArn`(AlertBufferStack)+ `resourceTags{System=petsite-ops}` | tier2 | DISABLED |

**明确不建模为 service**(在 `coverage-ledger.md` 里记不纳管理由):
`chaos-mesh` ns(混沌工具本身)、`kube-system` 附加组件、`amazon-cloudwatch` ns、
CDK EKS provider Lambda(ServicesEks2-* / Applications-*)、`devops-agent-*`、`openclaw*`、
`sqlreplay-*`(`Temporary=true`)、停机的 `i-00c04a0473b650f6d`、以及所有位于
`vpc-06731f30388b57818` 的 ALB/TG。

---

## User journey 层(直接复用仓库已有的单一事实源)

`infra/lambda/etl_aws/business_config.json` 的 `business_capabilities` 已经定义了三条业务能力,
形状与 ARH v2 的 user journey 完全对应,**不要另造一套**:

| User journey | recovery_priority | 覆盖服务 |
|---|---|---|
| `PetAdoptionFlow` | Tier0 | petsite, payforadoption |
| `AdoptionHistoryView` | Tier1 | pethistory |
| `PetInventoryManagement` | Tier1 | petsearch, petlistadoptions, statusupdater |

三条 journey 全部挂在同一个 system 下,S1 的 `associatedSystems[].userJourneyIds` 关联三条。
S2/S3/S4 不属于面向终端用户的旅程,只关联 system 不关联 journey。

---

## Policy 目标值(由 tier 推导,推导规则写在这里以便审计)

`business_config.json` 的 `microservice_recovery_priority` / `lambda_recovery_priority` /
`ec2_recovery_priority` 给的是 Tier0/1/2 分级,**AWS 不会替用户决定 RTO/RPO 具体数值**,
故下表是本目标的推导值,用户可随时改 policy 而不影响其余阶段:

| Policy | availabilitySlo | multiAz RTO / RPO | DR 方式 | dataRecovery |
|---|---|---|---|---|
| `arh-tier0` | 99.9 | 15 min / 5 min | `WARM_STANDBY` | 60 min |
| `arh-tier1` | 99.5 | 60 min / 15 min | `PILOT_LIGHT` | 240 min |
| `arh-tier2` | 99.0 | 240 min / 60 min | `BACKUP_AND_RESTORE` | 1440 min |

不设 `multiRegion` —— 本 VPC 是单区域部署,设了必然全部判 `NOT_ACHIEVABLE`,
只会淹没有价值的 findings。这一条是刻意的,不是遗漏。

---

## 阶段 0 — 前置补齐(零成本,不触发计费)🟢

**为何最先做**:input source 用 `resourceTags` 的前提是标签齐;`create-service` 的
`permissionModel.invokerRoleName` 是必填,角色不存在则整条链走不通。

- A-1 创建 IAM invoker role(挂 `AWSResilienceHubAsssessmentExecutionPolicy`,
  trust policy 加 `aws:SourceAccount=926093770964` 防混淆代理)
- A-2 给 3 个无标签 Lambda 补 `System=petsite-ops`:`petsite-rca-engine`、
  `petsite-rca-interaction`、`petsite-ops-slack-notifier`
- A-3 给 `gp-window-flush` 补 `System=petsite-ops`(现只有 `Project=graph-dp`)
- A-4 给 `petsite-neptune-instance-1` 补 `System=deepflow, Component=neptune`(集群有、实例没有)
- A-5 给 EKS 集群 `PetSite` 补 `System=petsite`(现标签为空 `{}`)

**出口条件**:role 可 `get-role` 读到;`System=deepflow` 与 `System=petsite-ops` 两个标签
各自用 `resourcegroupstaggingapi get-resources` 能捞到预期数量的资源。

---

## 阶段 1 — 建模层(零成本)🟢

- B-1 `create-system --name petsite-tokyo`
- B-2 `create-policy` ×3(arh-tier0 / arh-tier1 / arh-tier2,值见上表)
- B-3 `create-user-journey` ×3(PetAdoptionFlow / AdoptionHistoryView / PetInventoryManagement)

**出口条件**:DoD-1 的 system/policy 部分 + DoD-3 全绿。

---

## 阶段 2 — 纳管(零成本,计费尚未开始)🟢

- C-1 `create-service` ×4(S1–S4,按上表填 regions / permissionModel / policyArn /
  dependencyDiscovery / associatedSystems)
- C-2 `create-input-source` —— 每个 service 挂 1–2 条(tagged union 每次只能设一个顶层键,
  所以 S1 需要两次调用:一次 cfnStackArn、一次 eks)
- C-3 `list-resources --service-arn` 逐个核对解析结果,记录每个 service 实际捞到多少资源

**出口条件**:DoD-1 全绿。C-3 的输出是 DoD-2 对账的输入。

---

## 阶段 3 — 评估(⚠️ 此阶段开始计费)🟡

- D-1 `start-failure-mode-assessment` S1 → 轮询到 `SUCCESS`;若 `FAILED` 读 `errorCode` 建修复卡
- D-2 S3 同上(第二个启用依赖发现的 service)
- D-3 S2、S4 同上
- D-4 对每个 `FAILED` 的 errorCode 建针对性修复卡并重跑(注意:每 service 每月只含 2 次免费评估,
  第 3 次起 $0.10/资源,所以**先把权限/输入校验问题一次修干净再重跑**,不要试错式重试)

**出口条件**:DoD-4 全绿。

---

## 阶段 4 — 导出与对账(零成本)🟢

- E-1 导出 `list-service-topology-edges` / `list-dependencies` / `list-failure-mode-findings`
  到 `exports/*.json`
- E-2 写 `coverage-ledger.md`:VPC 每个应用组 → 已纳管(证据 ARN)或不纳管(理由)
- E-3 写 `verify_dod.sh`:把 5 条 DoD 变成可执行核验脚本,退出码 0 即全绿
- E-4 写最终报告到 `../arh-v2-onboarding-<时间戳>.md`,含 findings 摘要与成本实况

**出口条件**:DoD-2、DoD-5 全绿 → 全部 DoD 达成 → `autonudge_stop`。

---

## 阶段 X — 遗留欠项(可穿插,不阻塞主线)

- X-1 把 ARH 的 `list-dependencies` / topology edges join 进 Neptune(第 5 个 ETL)
  —— 这是本目标真正的下游价值,但属独立工程,本目标只负责把数据导出成文件
- X-2 ARH findings 与图谱现有 `drift_status` 对账:ARH 发现但图谱没有的依赖 = 图谱假阴性证据
- X-3 S3 拆分成 observability / neptune / etl 三个 service 以获得更细的 policy 粒度(+$30/月)
- X-4 `AWS::ResilienceHubV2::*` CFN 模板化(目前是 CLI 命令式;注意 CFN 无 InputSource 类型,
  仍需 CLI 补,所以模板化只能覆盖一半)
- X-5 `awesomeshop` 6 个 Deployment 全 0 副本但数据层还在跑 —— 是真实的成本与资安欠项,
  值得单独提给用户(本目标只如实建模,不做处置)

---

## 明确不做(除用户另行指示)

- 不跑 resilience tests(基于 FIS,$0.10/action-minute,且仓库已有独立的 chaos 模块)
- 不设 `multiRegion` 目标(单区域部署,设了全是噪音)
- 不做跨账号(`crossAccountRoles`)—— 全部资源在 926093770964 单账号内
- 不用 `designFileS3Url` 与 `tfStateFileUrl` 两种 input source(本环境无架构图文件、无 S3 tfstate)
- 不改 PetSite / AwesomeShop 应用代码,不动 tier0 Aurora 配置,不改安全组
- 不为了凑 DoD 去删除或缩容任何既有资源
