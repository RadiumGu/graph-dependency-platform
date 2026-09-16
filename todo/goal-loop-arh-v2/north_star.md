# North Star — 把东京区 vpc-010ab37a3f9f74725 的应用纳入 Next-gen Resilience Hub

> 创建:2026-08-29 06:15 UTC · 锚目录:`/home/ec2-user/works/graph-dependency-platform/todo/goal-loop-arh-v2/`
> 本文件是**不变目标**。仅用户修改。代理每轮重读。

---

## 1. 目标(一句话)

把 `vpc-010ab37a3f9f74725`(ap-northeast-1,账号 926093770964)里**所有在运行的应用**纳管进
Next generation Resilience Hub,每个 service 跑完至少一次 failure mode assessment,
并把 ARH 产出的**拓扑边与依赖清单导出成机器可读文件**,供后续 join 进 Neptune 图谱。

「纳管」的定义是三件事同时成立:
1. 应用被建模成一个 ARH v2 `service`,挂了至少一个 input source;
2. 该 service 绑定了带 RTO/RPO/SLO 目标的 resilience policy;
3. 该 service 至少有一次 `status=SUCCESS` 的 failure mode assessment。

只建 service 不评估 **不算纳管** —— 那样既拿不到 findings,也拿不到拓扑边。

---

## 2. Definition of Done(5 条,全部可用 shell 核验)

以下命令均需 `export PATH="$HOME/.local/bin:$PATH"`(官方 aws-cli v2.36.34,系统 `/usr/bin/aws`
是 2.33.15 不支持 `resiliencehubv2`)与 `--region ap-northeast-1`。

### DoD-1 建模层就位
```bash
aws resiliencehubv2 list-systems  --region ap-northeast-1 --query 'length(systemSummaries)'   # 期望 >= 1
aws resiliencehubv2 list-services --region ap-northeast-1 --query 'length(serviceSummaries)'  # 期望 >= 4
aws resiliencehubv2 list-policies --region ap-northeast-1 --query 'length(policySummaries)'   # 期望 >= 3
```
且每个 service 的 `policyArn` 非空、`list-input-sources --service-arn <arn>` 返回 >= 1 条。

### DoD-2 零遗漏覆盖
`coverage-ledger.md` 存在,且其中列出的每个 VPC 应用组都处于以下两态之一:
- **已纳管** —— 其代表性资源 ARN 出现在某个 service 的 `list-resources` 输出里;
- **明确不纳管** —— 带一条具体理由(平台组件 / 临时资源 / ARH 不支持该类型)。

核验:`bash verify_dod.sh 2` 退出码 0(脚本在阶段 4 产出,自身也是一张卡)。

### DoD-3 目标基线可查
3 条 policy(tier0/tier1/tier2)均已创建,`get-policy` 能读出非零的
`availabilitySlo.target` 与 `multiAz.rtoInMinutes` / `.rpoInMinutes`。

### DoD-4 评估跑通
每个 service:
```bash
aws resiliencehubv2 list-failure-mode-assessments --region ap-northeast-1 \
  --service-arn <arn> --query "length(assessmentSummaries[?status=='SUCCESS'])"   # 期望 >= 1
```

### DoD-5 图谱可消费的产出落盘
`exports/` 目录下存在且非空:
- `topology-edges-<service>.json` —— 来自 `list-service-topology-edges`
- `dependencies-<service>.json` —— 来自 `list-dependencies`
- `findings-<service>.json` —— 来自 `list-failure-mode-findings`

至少 S1(PetSite)三份齐全且 JSON 数组非空。

---

## 3. 发现来源(无可领卡时按序跑)

1. `aws resiliencehubv2 list-failure-mode-findings --service-arn <arn> --status OPEN`
   —— 每条 HIGH/MEDIUM finding 若属于本 VPC 可修范围,建一张卡
2. 评估失败时的 `errorCode` —— `INVALID_PERMISSIONS` / `CMK_ACCESS_DENIED` /
   `INPUT_VALIDATION` / `POLICY_VALIDATION` 各自对应一类可修问题
3. 覆盖率对账 —— 把 `list-resources` 的并集与任务 B 的资产清单做差集,漏项建卡
4. `list-dependencies` 里 ARH 发现但 Neptune 图谱里没有的依赖 —— 建卡记为图谱补边候选
5. 标签缺口 —— 任何还需要「按 ARN 手动加入」的资源都是一张补标签卡

---

## 4. 关键事实(实测,不要重新推导)

| 项 | 值 |
|---|---|
| 官方 CLI | `/home/ec2-user/.local/bin/aws` v2.36.34,支持 `resiliencehubv2`(58 子命令) |
| 系统 CLI | `/usr/bin/aws` v2.33.15 —— **不支持 v2 命名空间,别用** |
| botocore/boto3 | 1.42.97(PyPI 最新)**不含** `resiliencehubv2`,所以 Python 路径不可用,一律走 CLI |
| 当前身份 | `arn:aws:iam::926093770964:user/Radium` |
| ARH v1 存量 | 空(`list-apps` / `list-resiliency-policies` 均空)→ 无需 `import-app` |
| IAM 托管策略 | `arn:aws:iam::aws:policy/AWSResilienceHubAsssessmentExecutionPolicy`(**三个 s**,v9)已确认存在 |
| input source 类型全集 | tagged union 恰选一个:`resourceTags` / `cfnStackArn` / `tfStateFileUrl` / `eks{clusterArn,namespaces}` / `designFileS3Url` |
| policy 目标维度 | `availabilitySlo.target`(%)、`multiAz{rtoInMinutes,rpoInMinutes,disasterRecoveryApproach}`、`multiRegion{...}`、`dataRecovery.timeBetweenBackupsInMinutes` |
| DR 方式枚举 | `ACTIVE_ACTIVE` / `HOT_STANDBY` / `WARM_STANDBY` / `PILOT_LIGHT` / `BACKUP_AND_RESTORE` |
| CFN 支持 | 有 `AWS::ResilienceHubV2::{System,Service,ServiceFunction,UserJourney,Policy}`,**没有 InputSource / Assessment** → 编排层必须用 CLI |
| Terraform | AWS provider 只有 v1 `aws_resiliencehub_*`;v2 无原生资源 |
| aws-samples/awslabs | **无 next-gen 批量纳管仓库**;`aws-resilience-hub-tools` 全是 v1 API |
| 配额 | 100 services/region(可调)、500 可评估资源/service、20 input source/service、**2 次免费评估/service/月** |
| 「EKS 仅 stateless」 | **在 next-gen 文档中不成立**,是 v1 早期博客的阶段性说法;本 VPC 也无 StatefulSet,该约束不影响本目标 |

---

## 5. 成本(必须知情)

next-gen ARH **无免费额度**(6 个月免费仅属旧版 v1)。计费在 **service 创建 + 首次 failure
mode 评估完成后** 开始,service 删除即停止。

| 项 | 单价 | 本目标用量 | 月成本 |
|---|---|---|---|
| Service fee(含 ≤150 资源 + 2 次评估/月) | $15 / service / 月 | 4 个 service | **$60** |
| Dependency discovery(35 天回看) | $10 / service / 月 | 仅 S1、S3 启用 | **$20** |
| 超额评估 / 超 150 资源 | $0.10 / 资源 / 评估 | 预期 0(每 service 每月 ≤2 次评估) | $0 |
| Resilience tests(FIS) | $0.10 / action-minute | **本目标不做** | $0 |
| **合计** | | | **≈ $80 / 月** |

参照:同 VPC 的 `petsite-neptune`(db.r6g.large)月成本已约 $250,故 $80 属同量级可接受开销。

**要停止计费**:删掉 4 个 service 即可(`delete-service`),system/policy 本身不计费。

---

## 6. 操作不变量

- **只用官方工具**:AWS CLI v2 / 官方 SDK / CFN 官方资源类型。缺工具先升级官方工具,
  再查 aws-samples / awslabs,最后才自研;自研只允许在**编排层**(shell/python 调官方 CLI),
  不得手写 SigV4、不得手搓 service model。
- 不 `git push`(安全策略拦截,推送留给用户)
- 不读凭证文件原文;打印环境变量时只报 key 名不报 value
- **不动 PetSite / AwesomeShop 应用自身代码**,不改生产安全组,不收窄 EKS 公网 CIDR
- 不改 tier0 Aurora(`serviceseks2-databaseb269d8bb-efjeyzicx2ak`)的任何配置
- 允许的写操作仅限:创建 IAM role/policy、给资源**补标签**、创建 ARH v2 的
  system/policy/user-journey/service/input-source、触发 assessment
- **禁止**:`delete-*`(除回滚自己刚创建的 ARH 对象)、`terminate-*`、改动任何既有资源的
  非标签属性
- 一轮 = 一个原子步骤(≤5 次工具调用),每轮必须更新 `tasks.md`
- 聊天里保持安静,只在 DoD 达成 / 硬阻塞 / STOP 触发时说话

---

## 7. 停止条件(只有两条)

1. **目标达成** —— 第 2 节 5 条 DoD 全绿 → `autonudge_stop(reason="DoD met")`
2. **不可恢复的基础设施故障** —— 主机/工具链本身坏了且绕不过去(磁盘满、网络分区、
   凭证提供方连续 3 轮不可用、CLI 二进制消失)→ 记一行诊断并 `autonudge_stop(reason="infra: ...")`

**以下都不是停止理由**:评估失败、权限报错、配额不足、不知道怎么做、试过两次没成、
某张卡看起来被阻塞。评估 `FAILED` 带 `errorCode` 正是要修的东西,那就是工作本身。

真遇到需要用户决策的事(要超出第 6 节允许的写操作、要花超出第 5 节预算的钱、
要推送分支),把卡改 `review` 并**只提一次** blocker,然后换另一张卡继续,不要空转。
