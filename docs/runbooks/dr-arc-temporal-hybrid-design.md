# 东京 → 首尔切换：ARC Region switch + Temporal 混合方案设计

**状态**：设计稿，待审（2026-10-18）
**范围**：PetSite（EKS + Aurora Global + DynamoDB + Lambda）从 ap-northeast-1 切到 ap-northeast-2，以及回切
**已实测**：可逆步骤由 ARC 执行、Temporal 核实，两个方向都跑通（§9）
**关联**：执行模型细节 [`dr-temporal-orchestration.md`](dr-temporal-orchestration.md)、部署记录 [`deployment-record.md`](deployment-record.md)

---

## 1. 目标与约束

**目标**：一个平台管理一次复杂应用的跨区域切换。机器做可逆的准备和全部核实，人做每一个判断。全程可审计、可演练、可回切，最后有大屏展示进度。

**硬约束**（来自负责人，不在本文讨论范围内）：

| 约束 | 对设计的影响 |
|---|---|
| 东京始终是主站点；演示之外不做真切换 | 不可逆步骤默认由人执行，§4 |
| 半自动生成步骤 → 人审核调整 → 执行 | 计划有版本、要批准、要演练，§3 |
| 前门走 **CloudFront**，**DNS 托管不变** | 不能用 Route 53 健康检查执行块，前门用自定义 Lambda，§5 |
| 任何公网入口都必须有认证或经 CloudFront（NAT 除外） | 首尔 ALB 保持内网，用 VPC origin 接入，§5 |
| 参数与 secret 优先用 AWS 托管服务 | 审批身份走 IAM / CloudTrail，不用自报字段，§6 |

**非目标**：全自动切换（告警直接触发单向门）；多活；把 Temporal UI 当成作战大屏。

---

## 2. 分工

一句话：**ARC 动手，Temporal 把关，人拍板。**

| 层 | 负责 | 明确不负责 |
|---|---|---|
| **Temporal**（首尔自托管） | 根据依赖图生成计划初稿；计划版本、批准、「演练必须覆盖被批准的那一版」硬闸门；13 类只读探针（每类一个独立 activity，一份计划实测 27 次探测）；发起并监视 ARC 执行；用**与 ARC 不同的数据源**核实结果；从 CloudTrail 查审批人；执行记录写 S3 | 直接改生产资源（worker 角色里没有写权限，test_128 用 AST 扫描守着）；替人批准 |
| **ARC Region switch** | 执行 AWS 动作（扩缩容、Aurora、前门 Lambda）；带 IAM 身份的人工审批步骤；每个区域独立的数据面；每 30 分钟评估一次计划配置与权限 | 判断切换后业务是否真的恢复（执行块只报告自己做没做完） |
| **人** | 四个决策点（§3）；在 ARC 里批准；执行被标为「人执行」的单向门 | 手工敲可逆的准备命令 |

**为什么不只用其中一个**

- 只用 ARC：没有「计划是谁写的、批的是哪一版、演练是否覆盖这一版」，也不会独立核实业务是否恢复。实测里 ARC 报 completed 时，Lambda 判断的是 ASG InService，而 k8s 节点是否 Ready 是另一回事。
- 只用 Temporal：要自己维护各个 AWS 动作、审批身份、区域独立数据面和计划评估。这些 AWS 已经托管好了，自己做只是白付维护成本。

**为什么编排核心选 Temporal 而不是 Kestra**：切换是一个状态机，不是流水线，包含分支、按条件等待、人工裁决和超时即停。这些逻辑用代码写，在真实服务器上可测，也能做反向验证。Kestra 领先的地方在开箱即用的 RBAC、审批界面和看板，在本方案里分别由 ARC 审批 + IAM、S3 执行记录和自建大屏补上。选型细节见附录 A。

---

## 3. 端到端流程

```
告警/人工发起
  │
  ▼
[D0 宣告灾难] ── 人 ── 依据：外部合成探测、ALB 5xx、Health Dashboard、Aurora 复制状态
  │
  ▼
DrPlanWorkflow（计划层，长期存在）
  生成初稿（依赖图 → dr-runbook 块，未知项标 needs_human_input）
  → 人修订 → 人批准 vN（validator 当场拒绝不合格的版本）
  → 演练 vN（DrRunbookWorkflow rehearsal：真跑全部探针，不做变更）
  → 授权执行（要求 drilled == approved == current）
  │
  ▼
DrRunbookWorkflow（执行层，每个获批版本一条）
  │ 对每一步：前置探针 →（ARC 执行 或 人执行）→ 后置探针（独立来源）
  │
  ├─ 可逆准备 ── ArcPlanExecutionWorkflow → ARC：ManualApproval → Lambda 扩容
  ├─ [D1 数据怎么处理] ── 人 ── switchover（不丢数据）/ failover-allow-data-loss / abort
  ├─ Aurora 提升 ── 单向门 ── 当前由人执行；将来进 ARC，需双人审批（§11）
  ├─ [D2 前门切不切] ── 人 ── 依据：首尔后置探针全 PASS
  ├─ 前门切换 ── ARC 自定义 Lambda 改 CloudFront 源（§5）
  ▼
[D3 宣告稳定 / 回切] ── 人 ── 回切是另一份反向计划，不是自动 saga 回滚
```

四个人工决策点给人看的都必须是**数据**，不是一个「确定吗」的按钮。决策页（大屏的一部分，§10）展示复制延迟、预估 RPO、探针结果和前门现状。

---

## 4. 步骤分类

判断「这一步要不要人」看的是**能不能撤回、影响多大**，不是能不能自动化。

| 类别 | 步骤 | 执行者 | ARC 执行块 | 前置 → 后置探针 | 回滚 | ungraceful 时 |
|---|---|---|---|---|---|---|
| 只读 | 数据层核查（每个断言一步，执行时逐条由人确认结果） | Temporal 跑、人确认 | — | aurora_global_membership、aurora_writer_region、aurora_replication_lag、dynamodb_table_active | — | 照跑 |
| 只读 | 制品核查（同上） | Temporal 跑、人确认 | — | ecr_image_present、lambda_function_active、eks_nodegroup_status | — | 照跑 |
| 可逆 | 首尔节点组 0 → N | ARC | CustomActionLambda（`dr-korea-arc-scale-up`）| desired=0 → eks_ready_nodes ≥ N、asg_lifecycle_settled | 同计划激活东京 | 照跑 |
| 可逆 | 部署首尔工作负载（**每服务一步**，`infra/dr-korea/workloads/`） | 人（待迁 ARC） | EKSResourceScaling 或 Lambda | — → k8s_deployment_available（+ 有目标组的再核 alb_target_health） | kubectl delete | 照跑 |
| 可逆 | 回切时首尔缩回 0 | ARC | CustomActionLambda，`deactivatingRegion` | desired=N → desired=0、ASG 清空 | 再激活首尔 | **跳过**（东京不可用时不依赖它） |
| 单向门 | Aurora switchover / failover | 人（D1 之后） | 将来：AuroraGlobalDatabase | aurora_global_membership → aurora_writer_region=首尔 | 另一次计划内切换 | failover 才允许 |
| 可逆 | 前门切到首尔 | ARC | CustomActionLambda（§5）| 首尔后置全 PASS → 外部 http_get | 同 Lambda 指回东京 | 照跑（在激活区域运行） |

**不变量**（test_131 守着）：
- ARC 计划里每个工作流的第一步都是 ManualApproval；
- PoC 计划里只允许 ManualApproval 和 CustomActionLambda 两种块；
- 依赖停用区域的步骤在 ungraceful 模式下跳过。

---

## 5. 前门：CloudFront（DNS 托管不变）

**现状**：`rainmeadows.com` 不在本账号的 Route 53 里；首尔 ALB `dr-korea-petsite-alb` 是 internal；没有指向 PetSite 的 CloudFront 分发；Temporal UI 经东京 ALB（443 prio 4）+ Cognito 访问。

**设计**：

```
用户 ──DNS（不动，CNAME 到 CloudFront）──▶ CloudFront 分发（全局）
                                          ├─ 源 A：东京 ALB（现有，公网）
                                          └─ 源 B：首尔内网 ALB（VPC origin，不暴露公网）
```

- 切换 = 改分发的默认行为指向的源（A → B），由 ARC 自定义 Lambda 块执行，`RegionToRun: activatingRegion`，所以不依赖东京。
- 两个源各配一个 **origin custom header 密钥**，ALB 侧只放行带这个头的请求。头部值放 Secrets Manager。
- 后置核实**从外部**做：从首尔 worker 用 http_get 请求 CloudFront 域名，校验响应头里的区域标记。不读 CloudFront API 的返回值，「API 说改了」不等于流量真的过去了。
- 回切：同一个 Lambda 指回源 A。

**已知限制，必须讲清楚**：
1. CloudFront 是全局服务，配置变更走它的控制面。东京区域故障不影响它，但它本身的可用性不在我们的区域隔离设计之内。
2. 配置变更需要几分钟传播，这段时间计入 RTO。后置探针要带重试窗口。
3. 有一个替代方案：源组故障转移（origin group failover）。它是自动的，CloudFront 看到源 A 返回 5xx 就转到 B。它比 Lambda 切换快，但等于把 D2 这个人工决策交给了机器。**本方案不采用**，原因是与 Aurora 写节点不同步时，流量先过去会造成写入失败。

**同一套方案也给 Temporal UI 和大屏用**：在首尔加第二个 CloudFront 分发 + Cognito，取代经东京 ALB 的入口（§7 的缺口）。

---

## 6. 身份、审批与审计

| 项 | 实现 | 证据 |
|---|---|---|
| 谁能批准 ARC 步骤 | **只有**扮演计划里配置的审批角色 `petsite-dr-poc-approver` 的会话。管理员直接调 `ApprovePlanExecutionStep` 会被拒 | 10-18 实测：IAM 用户 Radium（AdministratorAccess）直接批，返回 `not allowed to approve this execution` |
| 审批角色要求 MFA | 信任策略 `aws:MultiFactorAuthPresent=true` | 不带 MFA 扮演 → AccessDenied（10-18 两次恢复后复验） |
| worker / agent 的边界 | worker 角色只能对 `petsite-dr-poc` 发起执行和读取；不能 Approve / Cancel / UpdatePlanExecution(Step) | `simulate-principal-policy`：Start allowed，其余 implicitDeny，其他计划 implicitDeny |
| 审批人身份记录 | 从**执行所在区域**的 CloudTrail 读取 `ApprovePlanExecutionStep`，记录主体、MFA 标记、来源 IP；被拒的尝试也记，但只有成功的那条才算「找到了」 | 执行记录 `plans/arc-petsite-dr-poc/executions/*.json` |
| Temporal 内的人工动作 | Update + validator；人经 `probe_cli.py` → SSM 调用，CloudTrail 里是真实 IAM 身份。Update 里的 operator 字段是自报的，只记录、不当作授权依据 | `plan_workflow.py` 文件头 |
| agent 通道 | temporal-mcp：**2026-10-20 起放开 `update_workflow`**（负责人决定），agent 可经 MCP 推进计划与执行，署名是自报的、CloudTrail 只见 AgentCore 角色；start/signal/cancel/terminate/pause/schedule 写仍禁用 | 10-20 真实调用核实：approve_plan 到达 validator 并被拒（前门未填），cancel 被拦 |
| Temporal UI 写操作 | **待决**：`TEMPORAL_DISABLE_WRITE_ACTIONS` 已实测会在服务端拦截（405）；打开后人也不能在 UI 上点取消 | 10-06 临时容器实测 |

**应急通道（审批人拿不到 MFA 时）**：
- **规定做法**：在模板里加 `RequireMfa` 参数（默认 true）。切到 false 必须通过变更集部署，批准后立即切回，并在执行备注里写明原因。
- **反例**：10-18 的两次批准是手工改一份临时模板去部署的。两个窗口共 73 + 43 秒，执行记录里是 `mfa=false`。能用，但留痕靠的是人自觉，不是机制，所以必须参数化（§11）。

---

## 7. 编排器自身的可用性

切换工具不能依赖它要逃离的那个区域。

| 组件 | 位置 | 东京完全不可用时 |
|---|---|---|
| Temporal Server + PostgreSQL | 首尔 `i-09380e417a0177ed4` | ✅ 可用 |
| Temporal worker | 同一台主机 | ✅ 可用 |
| ARC 数据面 | 每个区域独立 | ✅ 激活首尔走首尔数据面 |
| ARC 控制面（建计划、改计划） | 全局 | ⚠️ 不可用时不能**改**计划，但能**执行** |
| 人工审批（扮演角色） | IAM / STS 全局 | ⚠️ 依赖 IAM 全局服务 |
| 计划正文与执行记录 | 首尔 S3 桶（版本控制） | ✅ 可用 |
| **Temporal UI 入口** | **东京 ALB + Cognito** | ❌ **缺口**：指挥台会跟东京一起挂。临时办法是 SSM 端口转发到 8080；正式方案是 §5 的首尔 CloudFront 入口 |
| 依赖图（Neptune） | 东京 | ❌ 生成新计划需要它；已生成并批准的计划不需要 |

单点：Temporal 是单机部署。要么接受「切换期间 Temporal 主机不能同时出事」，要么迁到 Temporal Cloud 的多区域 namespace（费用见官方报价）。

---

## 8. 故障模式与处理

| 故障 | 当前处理 | 代码 |
|---|---|---|
| ARC 报完成，但后置探针 FAIL | 结论记为 `arc-completed-but-postcheck-failed`，不是成功 | `arc_bridge.py` |
| 等人超时 | 停在原地（stalled），**不取消 ARC 执行**，不替人做决定 | `arc_bridge.py`、`runbook_workflow.py` |
| 发起执行失败 | 结论记为 `arc-start-failed`，执行记录照样导出 | PR #71 |
| 在错误的区域发起 | 发起用 target_region，查询用执行 ID 的区域前缀 | PR #77 |
| worker 的 SDK 版本落后 | 幂等不依赖 `clientToken`：先按备注里的幂等标记查找，SDK 支持时才传参 | PR #71 |
| CloudTrail 投递延迟 | 最多等 20 分钟，每分钟查一次，等到一条成功的审批记录 | PR #77 |
| worker 重新部署后长执行回放失败 | **部分缺口**：只改展示的变更（PR #78）已用线上全部 DR history 做回放核对。行为变更必须用 `workflow.patched()`，PR #71/#77 没用，导致两条已结束的 ARC 执行回放失败（执行记录在 S3 上完整，无损失） | §11 |
| 演练覆盖的不是批准的那一版 | 硬闸门：要求 drilled == approved == current | `plan_workflow.py` |
| 有人直接发起执行模式 | 拒绝：服务端写入的父执行信息必须是 DrPlanWorkflow | `runbook_workflow.py` |
| 计划批准后环境悄悄变了 | **缺口**：没有定期复检，§11 | — |

---

## 9. 小验证结果（实测证据）

**计划**：`petsite-dr-poc`（CFN 栈 `dr-korea-arc-poc` + 东京 `petsite-arc-noop`）。只含 ManualApproval 和扩缩 Lambda，不碰 Aurora 和前门。两个区域的计划评估都是 passed，零告警。

| 轮次 | 时间 | 结果 | 原因 / 修复 |
|---|---|---|---|
| 1 | 10-07 11:46 | 发起失败 | worker 的 boto3 1.40.47 没有 `clientToken` → PR #71 |
| 2 | 10-07 11:55 | 等审批，10-08 超时 | 批准没有到达 ARC（CloudTrail 和 ARC 事件都没有记录） |
| 3 | 10-18 10:56 | ✅ 激活首尔 `verified` | 批准后 36 秒完成；k8s Ready=1，ASG InService=1 |
| 4 | 10-18 11:10 | 回切发起失败 | 「激活东京」必须在东京数据面发起 → PR #77 |
| 5 | 10-18 11:18 | ✅ 回切 `verified` | 批准后约 3 分钟完成；desired=0，ASG 清空；审批人记录正确 |

**这次没有证明的**：「agent 批不了」。设计上 agent 没有 MFA、扮演不了审批角色；但第 3、5 轮是负责人授权 agent 临时去掉 MFA 后批准的（§6 的反例）。下一轮要由人带 MFA 批准一次，才能证明这一点。

**学到的三点**：
1. 本地测试全绿不代表线上可用：SDK 版本不同，测试就测不到。
2. ARC 对调用区域有硬性要求，文档里有写，但只有真跑才会撞上。
3. 「谁批的」要用独立来源去查，而且要区分被拒的尝试和成功的批准。

---

## 10. 大屏

**定位**：给指挥者和客户看的只读页面，不是操作入口。Temporal UI 仍然是排查用的调试器。

**数据源**（都已存在）：

| 区块 | 来源 | 刷新 |
|---|---|---|
| 当前阶段、在等谁、第几步 | Temporal 查询 `plan_state` / `runbook_state` / `arc_state`，以及 search attribute `DRPlanState`、`DRStepName` | 5 秒 |
| 每步进度带（开始/结束/耗时/结论） | 执行记录（S3）+ ARC `ListPlanExecutionEvents` | 10 秒 |
| 两边业务 SLO | CloudWatch：东京、首尔的领养成功率、ALB 5xx | 30 秒 |
| 数据层 | Aurora 复制延迟、写节点所在区域（探针） | 30 秒 |
| RTO 计时 | 从 D0 宣告时间起算，对比计划里的 `RecoveryTimeObjectiveMinutes` | 1 秒 |
| 审批与决策 | 审批人（CloudTrail）、D1/D2 的选择和理由 | 事件触发 |

**实现**：一个静态页 + 一个只读 Lambda（经 Temporal HTTP API 7243 和 AWS SDK 取数），经首尔 CloudFront + Cognito 对外。v1 如果赶时间，可以先用 CloudWatch Dashboard 顶上。不要建在已归档的 Grafana 插件上（附录 A）。

**同步做的事**：Temporal UI 本身已经可读（PR #78）。计划和执行的 Current Details 里有一步一行的步骤表、完整命令，以及「现在等你执行什么、怎么确认」；Timeline 上的计时器和探针都有中文标签。

---

## 11. 路线图与待决事项

| 阶段 | 内容 | 状态 |
|---|---|---|
| ✅ 已完成 | 计划层与执行层；12 类探针；ARC 执行可逆步骤 + Temporal 核实，两个方向实测；agent 通道只读；Temporal UI 可读 | PR #58–#78 |
| 下一步 | `RequireMfa` 参数化（§6）；DR 工作流回放门禁：把线上 history 存成测试夹具，CI 里用 Replayer 回放，行为变更必须带 `workflow.patched()`；由人带 MFA 批准一次（§9）| 未开始 |
| 下一步 | CloudFront 前门 + 前门切换 Lambda + 外部后置探针（§5）；首尔的 Temporal UI 与大屏入口（§7）| 未开始 |
| 下一步 | 每天定时复检：用 Temporal Schedule 跑前置探针，出现 FAIL 就撤销「可以执行」 | 未开始 |
| 之后 | Aurora 执行块进 ARC 计划（单向门，双人审批：两个审批角色依次批准）| 未开始 |
| 之后 | FIS 演示场景：Cross-Region Connectivity。它模拟的是「区域间失联」，不是「东京整个挂了」，演示时要讲清楚 | 未开始 |
| 之后 | 回切 runbook（Aurora 反向 switchover、东京重建从库）| 未开始 |

**需要负责人决定**：
1. Temporal UI 要不要改成只读（安全性和便利性的取舍，§6）。
2. 演示用方案 A（真切换后切回）还是 B（东京不动，Aurora 只讲不演）。
3. 双人审批由谁担任，两个审批角色分别信任谁。
4. Temporal 继续单机部署，还是迁到 Temporal Cloud（§7）。

---

## 附录 A：选型摘要

- **Temporal vs Kestra**：核心选 Temporal（理由见 §2）。Kestra 2.0 是 Apache-2.0、自带 Pause/HumanTask 和甘特图，如果团队以运维为主、步骤基本线性、看重开箱 RBAC，它更合适。不建议两者同时用，那样就有两个状态来源。
- **ARC 各功能**：Region switch 在用，并持续加功能；routing control 和 zonal shift 在用；**readiness check 自 2026-04-30 起不再接受新客户**，我们用自己的探针代替。
- **开源界**：没有对口且还在维护的跨区域切换编排平台。最直接的参考是 `aws-samples/sample-multi-region-application-architecture` 里的 ARC 计划模板（审批步骤和自动步骤交错）。详见 [`docs/lessons/temporal-execution-readability-2026-10.md`](../lessons/temporal-execution-readability-2026-10.md) 第七节（10-07 的调研）。

## 附录 B：关键坐标

| 项 | 值 |
|---|---|
| ARC 计划 | `arn:aws:arc-region-switch::926093770964:plan/petsite-dr-poc:z9ri7k` |
| 模板 | `infra/dr-korea/20-arc-region-switch-poc.yaml`、`infra/tokyo/07-arc-region-switch-noop.yaml` |
| 审批角色 | `petsite-dr-poc-approver`（MFA）；人工批准脚本 `infra/dr-korea/arc-approve.sh` |
| Temporal | `10.20.1.10`：7233 gRPC / 7243 HTTP / 8080 UI；队列 `dr-plan-queue` |
| 桥接代码 | `dr-plan-generator/worker/arc_bridge.py`；UI 文本 `ui_text.py` |
| 执行记录 | `s3://dr-korea-agentcore-926093770964-ap-northeast-2/plans/<plan>/executions/` |
| 门禁 | test_128（worker 无写调用）、test_131（混合方案不变量）、test_132（桥接行为）、test_134（UI 可读性）、test_135（本文与代码一致） |
