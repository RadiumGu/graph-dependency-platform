# Temporal 执行可读性改造 + DR 编排引擎选型调研

**调研日期**：2026-10-07
**触发**：`DrPlanWorkflow` 的 `plan-20261006-tokyo-to-seoul` 在 UI 上跑了 1 天 10 小时等人工裁决，详情页看不出在等什么、等到哪一步。
**环境**：Temporal Server **1.32.0**（韩国 ap-northeast-2 自托管）、Python SDK **1.33.0**、UI 版本未核实。
**方法**：3 路并行调研（User Metadata API / 外部可读性 / UI 与 Timeline），结论全部按 `sdk-python` 的 **`1.33.0` tag（提交 `ab52fdd`，2026-09-15）** 逐个签名复核，不采信子代理抓的 `main` 分支。本仓库现状由 grep 实查。

> ⚠️ **一个会直接报错的版本陷阱**：`main` 分支上 `sleep()` / `upsert_search_attributes()` / `execute_activity()` 都带 `event_groups` 参数，**1.33.0 没有**。它是 **1.34.0**（2026-09-30）才加的。照抄 `main` 的签名会 `TypeError`。


> **落地情况（2026-10-18 补记，正文保持 10-07 原样）**
> - 第二章的四个入口已在 [PR #78](https://github.com/RadiumGu/graph-dependency-platform/pull/78) 全部接上：`set_current_details`（计划与执行的步骤表、「现在等你做什么」）、`wait_condition(timeout_summary=)`、activity / `workflow.sleep` 的 `summary=`、`start-plan` 的 `static_summary` / `static_details`。渲染集中在 `dr-plan-generator/worker/ui_text.py`，从已解析的 `dr-runbook` 块生成，没有手写第二份（照本文第二章末尾的警告）。
> - 下面第一章那张「全仓库零引用」表是 **10-07 的快照**，已不再成立。
> - 本文第四章「不要为观感改成轮询」照做：30 天计时器保留，只加了标签。
> - 守卫：`tests/test_134_temporal_ui_readability.py`（每个带超时的等待、每个 sleep 必须有标签；summary 按 UTF-8 字节不超 200）。
> - 第七章的选型结论已收进 [`docs/runbooks/dr-arc-temporal-hybrid-design.md`](../runbooks/dr-arc-temporal-hybrid-design.md) 附录 A。

---

## 零、一句话结论

**这不是 Temporal UI 的缺陷，是三个官方可读性入口没接满。** 截图里 `User Metadata` 标签页空着、Timeline 条带只有裸编号 —— 因为整份 runbook 正文被放在 workflow Input 里，而 Input 是给 workflow 吃参数的地方，UI 只会原样折行。

分工是清楚的：

| 要解决的问题 | 正确的承载字段 | 本仓库现状 |
|---|---|---|
| 列表页一行看出这是什么计划 | `static_summary` | ❌ 全仓库零引用 |
| 详情页看到 runbook 正文 | `static_details` | ❌ 全仓库零引用 |
| 实时看到「现在第几步、在等谁」 | `workflow.set_current_details()` | ❌ 全仓库零引用 |
| Timeline 条带上有人话标签 | activity/timer 的 `summary=` | ⚠️ 只在 `probe_cli.py` 的 standalone 调用里用了 |
| 列表页按步骤过滤 | 自定义 Search Attribute | ✅ 已有 8 个，worker 启动校验 |
| 运维直接在 UI 里批准 | `@workflow.update` + validator | ✅ 已有（但下游流程未利用 UI 这条路） |

**三者里只有 Search Attribute 是被索引、可查询、可过滤的**；memo 和 user metadata 都只是展示。这个区分决定哪个值该放哪 —— 想在列表页过滤的必须是 SA，想给人看的放 user metadata。

---

## 一、本仓库现状盘点（grep 实查，2026-10-07）

### 已经具备的（不要重做）

- **8 个自定义 Search Attribute**，worker 启动时 `ensure_search_attributes` 校验齐备。
- `upsert_search_attributes` 已在步骤推进处调用：`dr-plan-generator/worker/runbook_workflow.py:252`、`:373`（`_SA_STEP_NAME.value_set(s.id)`）。
- 三个查询：`runbook_workflow.py:216` 的 `runbook_state`、`plan_workflow.py:593` 的 `plan_state`、`:628` 的 `revision_log`。
- 带 validator 的 update 链（`revise_plan` / `approve_plan` / `decide` / `confirm_step` / `resolve_gate`）。
- `arc_bridge.py` 已存在 —— ARC Region switch 这条线已动工。
- Server 1.32.0 ≥ 1.20，且 8 个 SA 已在工作，说明 **advanced Visibility 前置条件已满足**，自定义属性过滤可用。

### 确认为零的（这就是改造面）

| API | 全仓库引用数 |
|---|---|
| `static_summary`（启动参数） | 0 |
| `static_details`（启动参数） | 0 |
| `workflow.set_current_details()` | 0 |
| `workflow.get_current_details()` | 0 |
| `wait_condition(..., timeout_summary=)` | 0 |
| `workflow.sleep(..., summary=)` | `arc_bridge.py:352/:374/:389` 用了 `sleep` 但**没带 summary** |

> `dr-plan-generator/output/summary_generator.py` 里的 `_static_summary()` 与此无关 —— 那是生成计划正文的函数，同名而已。

---

## 二、第一层：User Metadata（1.33.0 已可用，投入产出最高）

### 已核实的真实签名

按 `1.33.0` tag 逐个核对：

| 字段 | 签名 | 出现位置 | 限制 |
|---|---|---|---|
| `static_summary` | `start_workflow(..., static_summary: str \| None = None)` | **列表页** + User Metadata 页 | 单行，200 字节 |
| `static_details` | `start_workflow(..., static_details: str \| None = None)` | 详情页 User Metadata | 多行，20K 字节 |
| 运行中详情 | `def set_current_details(description: str) -> None` | User Metadata 页 Current Details | 多行 Markdown |
| 读回详情 | `def get_current_details() -> str` | — | — |
| 定时器标签 | `async def sleep(duration: float \| timedelta, *, summary: str \| None = None)` | **Timeline 条带** + Event History 行内 | 单行，200 字节 |
| 等待超时标签 | `async def wait_condition(fn, *, timeout=None, timeout_summary: str \| None = None)` | 同上 | 文档建议当作 timer ID 用 |
| activity 标签 | `execute_activity(..., summary: str \| None = None)` | 同上 | 200 字节 |
| 子 workflow | `start_child_workflow(..., static_summary=None, static_details=None)` | 子执行自己的页面 | 同上 |

### 四个坑

1. **形参名是 `description` 不是 `details`**。源码原文：`def set_current_details(description: str) -> None`。
2. **`asyncio.sleep` 不支持 `summary`**，必须换成 `workflow.sleep`。这是 Timeline 上那条 `1 (30d)` 只显示裸编号的原因。`arc_bridge.py` 的三处 `workflow.sleep` 加上 `summary=` 即可，零行为变化。
3. **Markdown 会渲染，但受限**：官方限定 "standard Markdown excluding images, HTML, and scripts"。表格、勾选框、行内代码都可用。
4. **Timeline 渲染 activity summary 需要 UI ≥ v2.34.6**。写了不显示就是 UI 版本不够 —— 这条必须先验。`probe_cli.py:92` 已经把 summary 截到 120 字符，与 UI 的 120 字符截断阈值一致，说明这个经验仓库里已经有了。

### 改法示例（只用 1.33.0 已核实 API）

```python
from datetime import timedelta
from temporalio import workflow

# 步骤推进处：SA（可查询）与 Current Details（可读）一起更新
def _publish(self, step: int, total: int, phase: str, kind: str, waiting: bool) -> None:
    workflow.upsert_search_attributes([_SA_STEP_NAME.value_set(phase)])
    mark = "⏸ 等待人工" if waiting else "▶ 执行中"
    workflow.set_current_details(
        f"## 步骤 {step} / {total} — {phase}\n\n"
        f"**类型** `{kind}`　**状态** {mark}\n\n"
        f"| 已完成 | 当前 | 剩余 |\n|---|---|---|\n"
        f"| {step - 1} | {phase} | {total - step} |"
    )

# 探针 activity：给 Timeline 条带贴中文标签
await workflow.execute_activity(
    probe_name,
    start_to_close_timeout=timedelta(minutes=10),
    summary=f"[核查] {probe_question[:120]}",   # 200 字节上限，UI 超 120 字符截断
)

# 沉降窗口：用 workflow.sleep 才能带标签
await workflow.sleep(timedelta(minutes=3), summary="复制延迟沉降窗口")

# 人工裁决：单定时器 + Update 提前释放
await workflow.wait_condition(
    lambda: step_id in self._decisions,
    timeout=timedelta(days=30),
    timeout_summary=f"await-decision-{step_id}",
)
```

启动侧把 runbook 正文从 Input 搬到 `static_details`：

```python
await client.start_workflow(
    DrPlanWorkflow.run,
    id="plan-20261006-tokyo-to-seoul",
    task_queue="dr-plan-queue",
    static_summary="DR 计划：东京 → 首尔（12 步，4 处人工）",   # 列表页就这一行
    static_details=(
        "# 东京 → 首尔 容灾切换\n\n"
        "每个变更由人执行。Temporal 只做两件事：把命令原文展示给你、"
        "在你确认执行后用独立的只读探针核实终态。\n\n"
        "| # | 类型 | 步骤 |\n|---|---|---|\n"
        "| 1 | 核查 | 核查首尔数据层 |\n"
        "| 3 | 人工·可逆 | 首尔节点组 0 → 2 |\n"
        "| 5 | 裁决 | Aurora 切换方式 |\n"
    ),
)
```

> **注意与现有执行模型的关系**：`dr-runbook` 块是唯一权威，`static_details` 只是它的**展示副本**。两者必须由同一处生成，否则会出现「UI 上写的和块里不一样」这种比看不懂更糟的状态。建议从已解析的块渲染 `static_details`，不要手写第二份。

---

## 三、第二层：外部可读性（大部分已具备）

### 已有部分

8 个自定义 SA + 三个查询 + 带 validator 的 update 链都在。`runbook_workflow.py:373` 已经在每步把步骤名写进 SA —— 列表页具备了「卡在哪一步」的数据基础。

### 没有利用的那条路

官方文档确认 Web UI 的 Workflow Actions 支持 *"send a Signal or Update"* —— **运维可以直接在 Temporal UI 里给运行中的 workflow 发 Update 完成裁决**，不需要 CLI，也不需要先自建前端。

但本项目 2026-10-06 的决定是 **agent 通道只读**（`TEMPORAL_DENY_TOOLS` 禁掉 start/signal/update/cancel/terminate/pause/schedule），人的动作走 `probe_cli.py` 经 SSM，为的是让 CloudTrail 拿到真实 IAM 身份。**UI 发 Update 与这条审计要求是冲突的** —— UI 发出的 update 只带 UI 登录身份，不经过 SSM，CloudTrail 上没有对应的 IAM 主体。

**结论：不要因为"UI 能发 Update"就打开这条路。** 它是一条可用但会破坏现有审计链的捷径。记录在此是为了说明「技术上可行」与「本项目应该用」是两回事；如果将来审计要求放宽，这是现成的省事路径。

### 参考：自定义 SA 的建立与类型

```
temporal operator search-attribute create -n <ns> --name DRStep --type Int
```

类型关键字只有七个：`Text` `Keyword` `Int` `Double` `Bool` `Datetime` `KeywordList`。

- Python 侧构造 Double 的方法叫 **`SearchAttributeKey.for_float`**，没有 `for_double`。
- 字典形式的 `upsert_search_attributes` 已废弃，源码原文：*"The dictionary form of attributes is DEPRECATED and if used, result in invalid key types on the typed_search_attributes property"*。用 typed key。
- `@workflow.query` 必须是**非 async** 方法且不得改状态（async query handler 已废弃）。

---

## 四、第三层：Timeline 被长定时器压垮

### 现象与根因

详情页 Timeline 上一条 `1 (30d)` 的定时器横贯全宽，真实步骤被压成几个像素，`Hide idle time` 按钮灰掉点不动。

该按钮来自 PR [temporalio/ui#3627](https://github.com/temporalio/ui/pull/3627)（2026-07-15 合入），实现在 `src/lib/components/lines-and-dots/timeline-graph/build-time-segments.ts` 与 `timeline-collapsed-layer.svelte`，作用是压缩事件组之间的空白段。

**变灰的确切代码条件未核实**（GitHub 代码搜索需登录）。按功能推断：这份 history 只有一个长定时器、事件之间没有多段可压缩的空白，折叠器无事可做所以禁用。**这是推断，不是读到的代码。**

同一个 PR 还加了缩放控件（`src/lib/holocene/zoom-svg.svelte`），可以先试缩放。`Descending` 和 `Filter` 只作用于事件列表，不重新缩放时间轴，对此无效。搜过 issue，**没有**专门报此症状的 open issue。

### 不要为了观感改成轮询（代价已量化）

| 方案 | history 代价 | Timeline 效果 |
|---|---|---|
| 单个 30 天定时器（现状） | `TimerStarted` + `TimerFired` 两个事件 —— 这就是 34.31 KB / 7 次状态转换的来源 | 一条长条，压垮全宽 |
| 每小时轮询 720 次 | 每次迭代多一对定时器事件 + 一轮 WorkflowTask ≈ 3–4 事件，合计数千事件与状态转换 | 可读，且折叠器有空白可压 |
| `wait_condition(timeout=30d)` + Update 释放 | **与裸定时器相同**（底层就是一个定时器） | 仍是一条长条，但实际几乎等不满 |

**结论：保留单定时器，用 `wait_condition` + Update 提前释放。** 拿两个数量级的 history 换几个像素的观感不值，而且朝 10k 事件 / 50k 状态转换的软上限走，最后会被迫 Continue-As-New。

### 官方正解是 Event Groups，但还不能押

- **SDK 侧**：`event_groups` 参数 + `workflow.create_event_group()` 在 **1.34.0**（2026-09-30）落地，本项目在 1.33.0，差一个小版本。
- **UI 侧仍是草稿**：[#3899 Event Group Markers support (experimental)](https://github.com/temporalio/ui/issues/3899)（2026-09-10）、[#3986 Show and filter by event group markers](https://github.com/temporalio/ui/issues/3986)（2026-10-07）。

这个功能正是「把步骤归到命名阶段」，与本项目 7 阶段切换顺序天然对口，**值得盯**，但 UI 侧没落地之前不要把方案押在上面。升级到 1.34.0 本身是安全的（纯新增可选参数），但收益要等 UI。

---

## 五、明确不该做的

- **不要为 Timeline 观感把长定时器改成轮询**（代价见上）。
- **不要指望把 runbook 塞进 Input 能变好看**。Input 是 workflow 参数，UI 只会原样折行。正文属于 `static_details`。
- **不要先去改 UI**。`ui-server` 可配的只有 `disableWriteActions` 这类动作开关、`feedbackUrl`、以及 `customUi.iframeExtensions`（在 `app.top-nav.sub-nav` 之类槽位嵌 iframe）—— **没有** banner 文案项，**也不能隐藏标签页**。
- **不要为此上 Codec Server**。它（`codec.endpoint`，暴露 `/decode` `/encode` `/download`）确实能把不透明 payload 转成任意展示形态，而且跑的是自己的 codec 逻辑、`/decode` 返回什么 UI 就显示什么 —— 但那是给加密 payload 的重武器，本问题用 user metadata 就解决了。
- **不要因为 UI 能发 Update 就开那条路**（见 §3，与现有 CloudTrail 审计链冲突）。

### 临时看清一个 run

```
temporal workflow trace -w <id>              # 实时树状，含子 workflow
temporal workflow show -w <id> --detailed    # 事件分段打印，无像素压缩
```

第三方可视化只有一个像样的：[jonymusky/temporal-diagram-generator](https://github.com/jonymusky/temporal-diagram-generator)（36★、MIT、读 Event History JSON 出 Mermaid 图）。`mavdi/temporalio-graphs` 是 0★，且它画的是代码定义的 DAG 而非某次运行的 history。

---

## 六、收口：Temporal UI 是调试器，不是作战大屏

上面三层能把详情页从「看不懂」拉到「能看懂」：列表页一眼看出卡在第几步等裁决、详情页有渲染好的 runbook 表格与进度、Timeline 每条带有人话标签。**对排查已经够了。**

但它仍不是能挂墙的切换大屏 —— 没有 RTO 燃尽、没有「在等谁」的显式呼叫、刷新靠轮询。

好消息是现有的 `runbook_state` / `plan_state` 查询加那 8 个 Search Attribute **正好就是自建大屏的数据源**，不白做。

---

## 七、附：DR 编排引擎选型调研（2026-10-07）

同日做过一轮「跨 region 容灾调度平台」的开源选型调研（7 路并行扫 GitHub），结论记在此以免重复劳动。

### 结论一：开源界没有对口且在维护的编排平台

负面结论本身有证据：

- GitHub 搜 `disaster recovery orchestration` 62 个结果全是 1–5★ 的 2025 年 AI 玩具；`failover orchestrator` 225 个结果全是「模型故障转移 / VPN 隧道」噪音；`switchover automation region` 与 `game day disaster recovery runbook` **各返回 0 个结果**。
- 中文 `容灾切换` / `异地多活` / `容灾演练` 三个查询返回的全是 SEO 刷词仓库 —— 国内厂商的多活管控平台基本不在 GitHub 上。
- **alibaba/AppActive** 479★ 看着对口，但**功能性提交止于 2022-08-12**（`7f549df`），2025-03-20 只动过 README。且它是数据面中间件（流量单元路由 + 切换期禁写窗口），不是计划编排器。只有「单元路由表 + 切换期冻结写入」这个概念值得借。
- **Velero / RamenDR / OCM / Kanister** 全部健康活跃，但只倒 K8s 内部，碰不到 RDS / Route53 / ALB。Ramen 的 `DRPlacementControl` CRD 是最好的「故障转移计划」schema 参考，仅此而已。

候选编排引擎各自倒在一个具体点上：

| 候选 | 维护状态 | 出局原因 |
|---|---|---|
| AWX | 提交活跃，但 README 明示发布自 **2024-07-02 冻结**（"releases paused during a large scale refactoring"） | Workflow Approval Node 最对口，但两年多无可用发布版 |
| Rundeck | **健康**，v6.2.1（2026-09-09）月度节奏，"已停滞"传闻被数据推翻 | 暂停/审批门（Job Resume Plugin）是**企业版**功能，社区版正好缺最关键那一项 |
| StackStorm | 提交到 2026-09 还活着，但 v3.8.1（2023-12）→ v3.9.0（2025-10）有 **22 个月发布空窗** | `core.ask` 设计好，代价是 Mongo + RabbitMQ + Redis |
| n8n | 极活跃 | **Sustainable Use License 非 OSI**。内部自用许可证原文允许（"internal business purposes"），但不能转售 / 嵌入 / 对外托管 |
| Camunda 8 | 极活跃 | 核心 Zeebe + Operate + Tasklist 是 **Camunda License 1.0（源码可见、非 OSI）** |
| Kestra | 活跃，v2.0.5（2026-10-05） | **Apache-2.0，五个里唯一许可证干净的**；`io.kestra.plugin.core.flow.Pause` 现成人工门 + Gantt 视图。自建首选 |
| Temporal | 活跃，v1.32.0 | MIT（server 与 UI 均 MIT）、事件溯源、抗区域故障最强。**本项目已采用** |

### 结论二：ARC Region switch 覆盖了计划引擎这一层

AWS 的 Amazon Application Recovery Controller **Region switch**（plan-based，非老的 routing controls）执行块是一个 tagged union，已核实包含：

`customActionLambdaConfig`、`executionApprovalConfig`、`parallelConfig`、`regionSwitchPlanConfig`、`globalAuroraConfig`、`rdsPromoteReadReplicaConfig`、`rdsSwitchoverReadReplicaConfig`、`rdsCreateCrossRegionReadReplicaConfig`、`ecsCapacityIncreaseConfig`、`eksResourceScalingConfig`、`ec2AsgCapacityIncreaseConfig`、`route53HealthCheckConfig`、`arcRoutingControlConfig`、`neptuneGlobalDatabaseConfig`、`documentDbConfig`、`auroraServerlessScalingConfig`、`lambdaEventSourceMappingConfig`

- **人工门**：`executionApprovalConfig`（含 `approvalRole` + `timeoutMinutes`）+ `approve-plan-execution-step` API。
- **自定义步骤**：`customActionLambdaConfig`（任意 Lambda）。
- **结构**：Plan → Workflow → Step → ExecutionBlock 四级，子计划可嵌套一层，`parallelConfig` 并行分支。
- **执行模式**：graceful / ungraceful / post-recovery。
- **自动触发**：CloudWatch 告警 Trigger。
- **RTO**：建计划时设定，实际恢复时间在执行详情页与之对比。
- 每次执行自动出 PDF 报告投递到 S3（约 30 分钟内），可作 DR 审计证据。跨账号编排支持。

**两个会咬人的工程约束**：

1. 每区有独立数据平面端点 `arc-region-switch.<region>.api.aws`。**撤离某区必须从健康区调 `start-plan-execution`，激活某区必须从该区调** —— 端点调错会失败或产生意外行为。
2. 执行可暂停可取消，但**已启动的步骤不可逆**，只能靠另一次执行回退。

**可用性已实查**：CFN 资源类型 `AWS::ARCRegionSwitch::Plan` 在 `ap-northeast-1` / `ap-northeast-2` / `us-east-1` / `ap-southeast-1` 均 `isAvailableIn`，即计划本身可纳入 IaC。

官方文档明确提醒：**Plan Evaluation 通过不等于可用，必须真跑过**（校验的只是 IAM、资源配置、容量）。

### 结论三：GitHub 上值得抄的是 ARC 的参考实现

| 仓库 | 状态 | 可复用什么 |
|---|---|---|
| `aws-samples/sample-multi-region-application-architecture` | **活跃**，v1.3.1（2026-07-28），最近提交 2026-08-05 `17a280d` | `infrastructure/arc-region-switch-plan.yaml` 里有 `FailoverApproval` + `FinalApproval` 两个人工审批步骤与自动步骤交织、并行分支、嵌套子计划 —— **最直接的抄法** |
| `aws-samples/drs-tools` → `drs-plan-automation` | 轻度维护，2025-02-06 `ffcbfa8`，30★ | **唯一一个 AWS 官方带受保护 Web UI 的样板**：`drs-plan-automation/fe/` React + CloudFront + Cognito；波次模型，PreWave/PostWave 用 SSM runbook |
| `aws-samples/aws-cross-region-dr-databases`（即所谓 DR Orchestrator Framework） | **已过时**，止于 2024-05-08 `ec3695a`，9★ | 分层 manifest 思路；README 自承「故障转移决策需人工介入且不在范围内」。在 ARC 面前无采用价值 |
| `aws-samples/amazon-route53-arc-dr-automation` | **已归档**（2026-02-04 转只读） | 仅作 ARC routing control + RDS 全局故障转移的 Step Functions 写法参考 |

> `aws-samples/dr-orchestrator` 这个 URL 是 404 的，真实仓库是上表第三行。

### 结论四：大屏这一层开源界没有现成的

- **Volkov Labs 的 `business-*` 全家桶已于 2025-08～09 全部归档**（business-charts 179★ 于 2025-09-22 归档等）。很多「Grafana 做大屏」方案都指向它们 —— 不要在归档代码上建。
- Grafana 本体（v13.2.3，2026-09-29，AGPL-3.0）的 **State timeline** / **Status history** 面板能按「每实体每状态带起止时间」渲染步骤状态带，**Canvas** 做自由布局。AGPL 对内部自用无义务，只有改 Grafana 本体并对外提供网络服务才触发 §13。
- 但 BI 面板做不到三件事，而这三件恰是切换大屏的核心：**RTO 倒计时燃尽、「卡在等某人审批」的显式提示+确认按钮、真推送**（面板是轮询）。
- Argo Workflows 与 Temporal 的 UI 是真执行进度视图，但**只能看自家引擎里的执行，无法指向外部编排器的 API**。

**结论：大屏必须自建一页前端**，数据源用编排器自己的 API（本项目即 `runbook_state` / `plan_state` 查询 + 8 个 SA；ARC 方案则订阅 EventBridge 的 plan execution 事件）。v1 想抢时间可用 Grafana State timeline 顶一阵，不要指望长期够用。

---

## 八、核实边界

### 自己按 `1.33.0` tag 核过

模块已重构为 `temporalio/workflow/_context.py`；`sleep` / `wait_condition` / `set_current_details` / `get_current_details` / `upsert_search_attributes` / `upsert_memo` 的完整签名；activity 与 child workflow 的 `summary` / `static_summary` / `static_details`；`event_groups` 在 1.33.0 **不存在**、1.34.0 存在。ARC 侧：服务存在、执行块 union 完整清单、审批块参数、`approve_plan_execution_step` API、每区数据平面端点与调用方向约束、四个区域的 CFN 资源可用性。

### 未核实（不要当事实引用）

- `Hide idle time` 变灰的**确切代码条件** —— 推断，未读到代码。
- 自定义 Search Attribute 是否会成为列表页的**独立列** —— 过滤与排序已证实，独占一列无文档依据。
- Web UI 从**哪个版本**开始支持发 Update —— 当前文档证实支持，首个版本号未查到。
- 基础 user metadata 所需的最低 **Server** 版本 —— 只证实了 UI **v2.34.6** 对应 Timeline 上的 activity summary 渲染。本项目 Server 1.32.0 是否足够**未证实**。
- 本仓库 `wait_condition` 的使用情况 —— grep 结果被截断（`truncated: true`），**不能据此断言它未被使用**。截图里那条 30 天定时器的来源需另查。
- UI 当前版本号 —— 未取。

### 可直接实测补齐的两条

```
temporal operator cluster health     # Server 版本
# UI 版本：浏览器打开 UI 看右下角
```

---

## 九、建议的执行顺序

1. **先验版本**：UI 是否 ≥ v2.34.6。不够则第一层里的 Timeline 标签写了不显示，但 `static_summary` / `static_details` / `set_current_details` 仍可能生效（取决于 Server 对 `user_metadata` 字段的支持，见 §8 未核实项）。
2. **最小改动验证收益**：只加 `static_summary` + `static_details` + 一个探针 activity 的 `summary=`，起一个 run 看 UI。改动量最小、效果最直观。
3. **接 `set_current_details`**：插到 `runbook_workflow.py:373` 已有的 `upsert_search_attributes` 旁边 —— 那里本来就是步骤推进点，SA 与 Current Details 一次写完。
4. **给 `arc_bridge.py:352/:374/:389` 的三处 `workflow.sleep` 补 `summary=`**，零行为变化。
5. **`static_details` 从已解析的 `dr-runbook` 块渲染，不要手写第二份**（见 §2 末尾的警告）。
6. **暂不升 1.34.0**，等 UI 侧 Event Groups 落地（盯 #3899 / #3986）。
