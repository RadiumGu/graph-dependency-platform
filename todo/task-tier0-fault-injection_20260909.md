# 任务：给 PetAdoptionFlow（Tier0）补故障注入验证

**创建**：2026-09-09　**创建者**：合规报告线的会话（本文件是交接书，非本会话执行）
**目标读者**：接手执行故障注入的 agent 或工程师
**前置阅读**：`compliance_export/README.md`、`todo/decks/合规依赖报告-能力评估与补齐路线.md`

---

## 为什么这件事值得做（一句话）

**这是全项目唯一的乘数项。** 导出层、门禁、demo 页都在改善「怎么呈现」和
「怎么防退化」，但没有一项能改变一个事实：43 条依赖里只有 9 条有实测证据。

SYSC 15A.5.3R 原文要求 firm *must* carry out scenario testing；15A.6.1R(3)(c)
还要求书面记录「映射如何用于**支撑场景测试**」。**一份 21% 确证率的映射，
其余 79% 与填表工具的产出在审计眼里没有实质区别。**

这一项不需要新建模、不需要新观测源 —— 纯粹是实验排期问题。

---

## 范围：17 条可做 / 3 条卡住

`PetAdoptionFlow` 一跳依赖 27 条，其中 7 条已 `confirmed`，**待办 20 条**。
下表是 2026-09-09 用真实判定器（`chaos/code/runner/injectability.py`）跑出的结果。

### ✅ 可今天就做（17 条）

按注入手段分组，**建议的执行顺序就是下面的顺序**（信号最干净的排前面）。

#### 第一批：`Calls` 服务间调用（2 条）—— 先做这批

| 源 | 边类型 | 目标 | 当前 verify |
|---|---|---|---|
| petsite | Calls | payforadoption | `inconclusive` |
| petsite | Calls | petlistadoptions | `inconclusive` |

**为什么先做**：观测方（petsite）与被观测方都是 K8s 服务，指标完整；
chaosmesh/FIS 都能直打；退化信号最干净。而且这两条当前是 `inconclusive`
（注入过但退化不足以判定），说明实验路径已经通过一次 —— 大概率是注入强度或
观测窗口的问题，调参即可，不是从零开始。

#### 第二批：FIS 直打的 AWS 托管服务（8 条）

| 源 | 边类型 | 目标 | 类型 |
|---|---|---|---|
| petsite | AccessesData | `ServicesEks2-ddbpetadoption7B7CFEC9-3B00` | DynamoDBTable |
| payforadoption | AccessesData | `ServicesEks2-ddbpetadoption7B7CFEC9-3B00` | DynamoDBTable |
| petsite | AccessesData | `serviceseks2-s3bucketpetadoptioncb20dce5` | S3Bucket |
| payforadoption | AccessesData | `serviceseks2-s3bucketpetadoptioncb20dce5` | S3Bucket |
| payforadoption | AccessesData | `serviceseks2-databasewriter2462cc03-fwgf…` | RDSInstance |
| petsite | AccessesData | `ssm` / `sns` / `sts` / `stepfunctions` | AWSServiceEndpoint |
| payforadoption | AccessesData | `ssm` | AWSServiceEndpoint |

判定器原话：*目标类型 DynamoDBTable / S3Bucket / RDSInstance /
AWSServiceEndpoint 可由 fis 直接注入*。

⚠️ `AWSServiceEndpoint` 那 4 条目标是**泛化服务名**（`ssm`/`sns`/`sts`/
`stepfunctions`），不是具体资源。FIS 的 `fis_api_unavailable` / `fis_api_throttle`
按 service+operations 打，正好匹配这种粒度 —— 参考
`chaos/code/experiments/fis/api-injection/fis-api-unavailable-rds.yaml`。

#### 第三批：chaosmesh 源侧切断（7 条）

| 源 | 边类型 | 目标 | 类型 |
|---|---|---|---|
| petsite | AccessesData | `StepFnStateMachine76D362E8-3jkn8j2OUpdQ` | StepFunction |
| payforadoption | AccessesData | `StepFnStateMachine76D362E8-3jkn8j2OUpdQ` | StepFunction |
| petsite | DependsOn | `ServicesEks2-sqspetadoption2E8B1217-4T0K` | SQSQueue |
| petsite | PublishesTo | `ServicesEks2-sqspetadoption2E8B1217-4T0K` | SQSQueue |
| petsite | PublishesTo | `ServicesEks2-topicpetadoption192CAB8F-Dn…` | SNSTopic |
| petsite | DependsOn | **`WaggleAIOrchestrator`** | AgentRuntime |
| （见下方注意事项） | | | |

判定器原话：*目标类型无后端可直接打，但源 Microservice 在集群内，
可由 chaosmesh 在源侧切断出向流量*。

**`WaggleAIOrchestrator` 那条值得单说** —— 我原先以为 agent 层拿不到验证，
那个结论只适用于**两端都在 AgentCore** 的 `Delegates`/`InvokesTool` 边
（AgentCore 托管运行时不是 Pod，DeepFlow 的 eBPF 采集不到，实测 6 个
`agentic_ai` 类型 ENI 零 L7/L4 记录）。**这一条的观测方是 petsite**，
K8s 服务、指标完整，源侧切断完全可行。**agent 层至少有一条边今天就能拿到确证。**

⚠️ SQS 那两条（`DependsOn` 与 `PublishesTo` 指向同一个队列）是**同一物理关系的
两条边**（aws-etl 与 xray 各写一条）。注入一次即可，但结论要写回两条边，
否则依赖计数会一半有证据一半没有。

### ❌ 卡住的 3 条 —— **不要硬做**

| 源 | 边类型 | 目标 | phase | 判定 |
|---|---|---|---|---|
| petsite | DependsOn | `cdk-hnb659fds-container-assets-926093770…` | `startup` | `needs_compound_experiment` |
| payforadoption | DependsOn | `cdk-hnb659fds-container-assets-926093770…` | `startup` | `needs_compound_experiment` |
| petsite | DependsOn | `petadoptions/petsite` | `startup` | `needs_compound_experiment` |

**为什么不能硬做**：ECR 是**构建/启动期**依赖。镜像仓库不可用时**运行中的 Pod
完全不受影响**，只在扩容或重启时才体现。所以需要「切断 ECR + 触发重启」的
复合实验，而后端**没有 `startAfter` 时序编排支持**（`aws:fis:wait` 已注册进
`FIS_ACTION_MAP`，但 `fis_backend.py` 零 `startAfter` 实现）。

硬注入的结果只会是**又一批 `inconclusive`** —— 而 `inconclusive` 已经有 10 条了。
增加一个「看起来做过实验、实际什么都没证明」的记录，比留着 `未验证` 更糟：
后者诚实，前者会让人以为已经查过。

**判定器有个坑要知道**：`injectability(src, dst)` **不带 `reason_text` 时会返回
`injectable`**，只有传入 `'image-repo-dependency'` 才返回 `needs_compound_experiment`。
所以别只看类型级判定就以为 20 条都能做 —— 要先查边上的 `phase` 属性。

```python
# 正确的判定方式
from runner.injectability import injectability
phase = <从图上查 d.phase>
verdict, why = injectability(src_label, dst_label,
                             'image-repo-dependency' if phase == 'startup' else '')
```

---

## 怎么执行

### 环境

```bash
cd /home/ec2-user/works/graph-dependency-platform
export NEPTUNE_ENDPOINT=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com
export REGION=ap-northeast-1 AWS_DEFAULT_REGION=ap-northeast-1
export PYTHONPATH=infra/lambda/shared/python
```

`neptune_client_base` 读 `REGION` 而不是 `AWS_REGION`（否则 403）。
测试必须用 `python3.11`。

### 入口

```bash
cd chaos/code
python3 main.py --help                      # 看清子命令
python3 main.py run --file experiments/<新建的>.yaml --dry-run
python3 main.py run --file experiments/<新建的>.yaml
python3 main.py suite --dir experiments/generated/    # 批量
```

`main.py:61` 用 `load_experiment()` + `ExperimentRunner()`；支持 `--dry-run`
与 `--duration` 覆盖。**每条新实验先 `--dry-run` 跑一遍**。

### 实验 YAML 范式

抄这两个最接近的：

- FIS 打 AWS API：`chaos/code/experiments/fis/api-injection/fis-api-unavailable-rds.yaml`
- 已生成的 RDS failover：`chaos/code/experiments/generated/payforadoption-fis-rds-failover-h002.yaml`

六阶段结构（`phase0_preflight` → `phase5_steady_state_after`）与
`steady_state.before/after` 的阈值写法照抄，不要自创字段名 —— runner 按固定
schema 解析。

### 结论怎么写回图谱

由 `chaos/code/runner/edge_verification.py` 的 `write_verdict()` 负责，
它会写这些边属性：`verify_status` / `verify_reason` / `verify_experiment` /
`verify_confidence` / `verify_confirm_count` / `verify_refute_count` 等
（完整清单见 `graph_contract_data.EDGE_VERIFICATION['attrs']`）。

**不要手写这些属性** —— 走 runner，否则会漏掉 `verify_confidence` 的
log-odds 计算（零证据是 0.5 而非 0.0）。

---

## 验收标准

- [ ] 17 条里至少 12 条拿到 `confirmed` 或 `refuted`（**`refuted` 也是成果** ——
      说明那条边不成立，该从图上清掉，这比 `未验证` 有价值）
- [ ] `PetAdoptionFlow` 的 `confirmed` 从 7 条升到 ≥ 15 条
- [ ] 每条实验有 `chaos/validation-results/` 下的报告，含实验 ID 与时间戳
- [ ] SQS 那两条同物理关系的边，结论写回**两条**
- [ ] 3 条 ECR 边保持 `未验证`，并在 `todo/` 记一条「等 startAfter 支持」的技术债
- [ ] 跑一次 `python3 -m compliance_export --dry-run` 确认证据覆盖率真的上去了

### 自检：报告里的数字会自动跟着变

`compliance_export` 的局限披露是**由快照数据算出的**，不是手写。
所以做完实验后重新导出，确证率那一行会自己变。**不要去改文档里的数字** ——
如果发现文档和导出对不上，是文档过期了，重新导出即可。

---

## 已知陷阱（都是本仓库踩过的）

1. **别凭记忆写资源名。** 本会话有五次「凭记忆写名字 → 相信空结果」的记录，
   包括把 Aurora **MySQL** 的指标名用在 PostgreSQL 集群上（127 个可用指标里
   没有那两个），差点得出「库在空转」的结论。查任何外部系统前先枚举实际存在的名字。

2. **`aws --query '{...}'` 的花括号会被 shell 当 brace 展开。** 用 `--output json`
   管道给 python。

3. **FIS 历史只留存到有限窗口。** 2026-04-02 那两条打在 `grafana-aurora-mysql`
   上的 `fis_rds_failover` 结论已被标为不可采信（FIS 历史仅到 2026-05-14，
   无法证实）。跑完实验**及时**归档报告，别指望以后能从 FIS 控制台回捞。

4. **`UserErrors` 与 `SystemErrors` 语义相反。** 前者是坏请求，后者是 runtime 坏了。
   合并当健康信号会把判定带偏 —— 实测有过「26% 错误率」实为一小时突发平铺到
   24h 的假象，稳态 0%。

5. **零流量与健康在指标上无法区分。** 这是 `inconclusive` 存在的原因。
   注入前务必确认 `steady_state.before` 的 `success_rate` 有真实流量支撑
   （`petsite-loadgen` 那台 EC2 在跑 loadgen，必要时先确认它活着）。

---

## 参考：当前基线（2026-09-09 实测）

```
PetAdoptionFlow (Tier0)  一跳依赖 27 条  多跳可达 37 个对象
  confirmed       7 条
  inconclusive    4 条
  未验证         16 条

全图 43 条依赖：confirmed 9 (21%) / inconclusive 10 (23%) / 未验证 24 (56%)
```

重新取基线：

```bash
python3 -m compliance_export --dry-run
```

---

## 实测回执（2026-09-09 16:2xZ，执行侧会话）

我按这份交接书动手了。**三处结论需要修正,一处坑要补**——都有实测支撑,写在这里
免得下一个接手的人重复踩。

### 一、第一批那 2 条**不是「调参即可」,是零流量**（反证）

交接书说 `petsite -> payforadoption` / `petsite -> petlistadoptions`
「注入过但退化不足以判定，说明实验路径已经通过一次 —— 大概率是注入强度或
观测窗口的问题，调参即可」。

**实测否掉了这个判断**：

```
petsite -> payforadoption     900s 窗口: 0 次    3600s 窗口: 0 次
petsite -> petlistadoptions   900s 窗口: 0 次    3600s 窗口: 0 次
对照 petsite -> petsearch     900s 窗口: 15,422 次
对照 trafficgenerator->petsite 900s 窗口:  5,938 次
```

图上那条 inconclusive 的原因文本自己就写着：
*「**被测依赖路径本身**近期只有 0 次调用（需 >= 20）—— 观测方总流量再大也无关：
没有调用就无从打断，此时任何退化数字都是噪声。需先给这条路径造出流量再验」*

**负载生成器只压首页与搜索**，领养提交与列表路径压根没有流量。
调多大的注入强度都没用 —— 打不断一个没在跑的东西。
这批要先造流量（触发领养提交流程），再验。

⚠️ 量这个数之前先注意：`collect_edge_flow` 到 2026-09-09 才修好名字解析。
在那之前它拿图谱名去 `LIKE` 匹配，而 DeepFlow 用 K8s 名
（`petsearch`/`search-service`、`payforadoption`/`pay-for-adoption`），
**最忙的路径都会被读成 0 次**。如果你手上的旧数据显示某条边零流量，
先确认那份数据是修复之后测的（见 `chaos/code/runner/service_names.py`）。

### 二、第二批的「FIS 直打托管服务」有一个前提要说清

交接书引判定器原话「目标类型 DynamoDBTable / S3Bucket 可由 fis 直接注入」。
类型级判定没错，但**网络层切断对这类目标实测打不断**，两条路都试过：

- `NetworkChaos` + `externalTargets: dynamodb.<region>.amazonaws.com`
  —— Chaos Mesh 在 apply 时把域名解析成 IP 再装 iptables，
  AWS 区域端点有**多个轮换 IP**（pod 内解析到 35.71.114.102，SDK 后续拿到别的），
  规则只封住其中一个。S3 更糟：**Gateway 端点**，靠路由表 + 前缀列表转发，
  封单个 IP 根本不在路径上。
- `DNSChaos`（`action: error`）—— `AllInjected=True` 但依然没打断：
  应用用**长连接 + DNS 缓存**，4.7 次/秒全跑在 keep-alive 连接上，
  2 分钟窗口内 DNS 从不重查。

所以交接书推荐的 `fis_api_unavailable` / `fis_api_throttle` 这条路
**看起来比网络层切断靠谱**——它在 AWS API 层动手，不受 DNS 缓存与 IP 轮换影响。
我按这个思路做了实验，**但它撞在一个硬边界上，见下一节。**

### 二之二、FIS API 注入**不支持 DynamoDB / S3**（实测 + 文档双重确认）

我照交接书的建议写了
`chaos/code/experiments/fis/api-injection/fis-api-unavailable-dynamodb-petsearch.yaml`，
打 search-service 的 **IRSA 角色**（不是节点角色，半径精确到一个服务：
`ServicesEks2-searchserviceServiceAccountRole588AF64-Sulrl6FvKO5F`）。

dry-run 全绿、安全规则引擎放行、观测方基线识别正常
（`petsite (AccessesData): success=99.9% total=1697`）。真跑时 AWS 直接拒绝：

```
ValidationException: The service parameter value is not supported for the action.
Specify a valid service and try again.
```

AWS FIS Actions reference 对 `aws:fis:inject-api-internal-error` 的原文：

> **service** – The target AWS API namespace.
> The supported value is `ec2` and `kinesis`.

**所以 API 注入这条路对 DynamoDB / S3 走不通**，它只覆盖 `ec2` / `kinesis`。
顺带提醒：仓库里既有的 `fis-api-unavailable-rds.yaml` 写的是 `service: "rds"`，
按同一份文档它**大概也跑不起来**——那个文件可能从未真跑过，别拿它当已验证的范式。

### 二之三、那么这类边到底怎么验：复合实验，且必须有对照臂

三条路的实测结论汇总：

| 手段 | 结果 |
|---|---|
| NetworkChaos + externalTargets | 打不断（端点 IP 轮换 / S3 是 Gateway 端点） |
| DNSChaos 单独用 | 打不断（长连接 + DNS 缓存，窗口内不重查） |
| FIS API 注入 | **AWS 不支持该 service** |
| **DNSChaos + 删 Pod（复合）** | **打断成功**：调用速率 4.55 → 1.73 次/秒，降 62% |

复合实验是目前唯一能真正打断这类依赖的手段
（`scripts/verify_external_target_edges.py --compound`）。
但它的观测方信号被 Pod 重启混淆，所以**还差一个对照臂**才能自动判定，
详见下一节。

### 三、ECR 那 3 条：交接书说「后端没有 startAfter 编排」——绕过去了

`fis_backend.py` 确实零 `startAfter` 实现。但复合实验不必走 FIS：
`scripts/verify_external_target_edges.py --compound` 用 kubectl 直接编排
（施加故障 → 等 `AllInjected` → `delete pod` → 观测 → 清理 + 等 Pod 恢复 Ready）。

用 `delete pod` 而不是 `rollout restart`：后者滚动更新会等新 Pod Ready 才删旧的，
故障期间始终有健康旧 Pod 在服务，观测方看不到任何变化。

**但 ECR 这一类有个更硬的障碍，交接书没提**：
**拉镜像是节点上的 kubelet 做的，不是 Pod。** Chaos Mesh 的选择器只能选 Pod，
切 Pod 的网络对 kubelet 的镜像拉取无效。要打断 ECR 得做**节点级**中断。

顺带一个决定这 3 条边「是否真实」的事实——`imagePullPolicy`：

```
list-adoptions / pay-for-adoption / pethistory / petsite / search-service : Always
petfood / traffic-generator                                               : IfNotPresent
```

`Always` 的每次重启都必须拉 ECR，依赖是真的；`IfNotPresent` 的镜像已缓存则
根本不拉，ECR 不可用也照常起来。**这两类不该给同一个结论。**

### 四、补一个坑：复合实验里观测方信号**没有判别力**

我踩了，而且写错了数据两次（都已撤回，见
`todo/retracted-false-soft-verdicts_20260909-*.json`）。

删 Pod 会让上游对它的调用**必然**塌陷，于是「吞吐降了」既能读成
「依赖被切断」也能读成「Pod 在重启」。拿它喂判定链会得到一个
`dependency_class=soft` —— 而 `soft` 会被 DR 影响面分析读成
「这条依赖不影响可用性」并在预案里降级。**用混淆信号得出的 soft
比 untested 危险得多。**

有判别力的信号是「**新 Pod 在故障下能否 Ready**」：它不受 Pod 重启本身干扰，
因为重启是两种情况的共同前提。

```
起不来 -> 目标是启动期硬依赖
起得来 -> 至少不是启动期硬依赖
```

所以 `--compound` 现在产出 `verdict='observation_only'` 与结构化观察，
**刻意不写 `verify_status`**。要自动判定需补一个**对照臂**：
同样删 Pod 但不切断目标，对比两次的就绪时间与错误率。

实测一条（`petsearch -[AccessesData]-> DynamoDBTable`）：
注入生效性=True（调用速率 4.55 -> 1.73 次/秒，降 62%），
**故障下 Pod 就绪 2/2** → DynamoDB **不是** petsearch 的启动期硬依赖。

### 五、你那条「判定器不带 reason_text 会返回 injectable」的提醒——收到并已修

我的 `scripts/preflight_edge_traffic.py` 原先硬编码了边形态
`{('DependsOn','Microservice','ECRRepository')}` 来识别复合实验类。
实测 `phase='startup'` 的边恰好全是这个形态（13 条），所以**碰巧没出错**，
但方式是错的。已改成读边上的 `phase` 属性推出 reason_text 再问判定器
（`_PHASE_TO_REASON` / `_reason_text_for`）。谢谢这条，它省了我一次线上误判。
