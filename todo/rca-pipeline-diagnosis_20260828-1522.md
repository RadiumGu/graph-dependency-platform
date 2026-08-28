# RCA 链路诊断报告 —— 为什么分析侧 7 天零调用

> 诊断时间:2026-08-28 15:22 UTC
> 对象:`petsite-rca-engine` / `gp-window-flush` 触发链路
> 方式:**全程只读**(AWS API describe/get + 代码核对),未做任何变更
> 工具:本次使用新接入的托管 AWS MCP Server(`aws___run_script`)一次性拉取跨服务接线关系

---

## 0. 结论:链路是通的,没有故障。零调用的原因是**没有告警触发过**

链路接线、IAM 权限、调度器角色**全部正确**。之前怀疑的"链路断了"不成立。但在核查过程中发现 **1 个真实的潜在缺陷**和 **1 个时间线事实**,见第 3、4 节。

| 判断 | 结论 |
|---|---|
| SNS → Lambda 订阅 | ✅ 存在且正确 |
| Lambda 资源策略(主路径) | ✅ 正确 |
| EventBridge Scheduler 角色权限 | ✅ 正确(信任策略 + 内联策略均无误) |
| `gp-alert-buffer` 表 | ✅ 存在,当前 0 项(空闲态正常) |
| **告警是否触发过** | ❌ **30 天内 0 次** —— 这就是根因 |
| **`rca-alerts` 主题路径** | ⚠️ **订阅存在但缺 Lambda 权限,该路径会失败** |

---

## 1. 实际接线关系(已验证)

```
                     ┌─ petsite-rca-alb-5xx-high        (OK, 末次变更 2026-04-01)
CloudWatch Alarm ×4 ─┼─ petsite-canary-failed           (OK, 末次变更 2026-03-05)
                     ├─ petsite-adoption-dlq-nonempty   (OK, 末次变更 2026-03-03)
                     └─ petsite-stepfn-execution-failed (OK, 末次变更 2026-03-03)
        │
        ▼ SNS: petsite-rca-alerts
   ✅ 订阅 → lambda:petsite-rca-engine
   ✅ 资源策略 Sid=rca-sns-trigger,Principal=sns.amazonaws.com,
      条件 ArnLike AWS:SourceArn = petsite-rca-alerts
        │
        ▼ (告警聚合窗口)
   DynamoDB gp-alert-buffer  ← 当前 0 项
        │
        ▼ 运行时动态创建一次性 EventBridge Schedule
   ✅ 角色 gp-alert-window-scheduler-role
      信任:scheduler.amazonaws.com
      内联:lambda:InvokeFunction on gp-window-flush(含 :* 别名)
        │
        ▼
   lambda:gp-window-flush  ← 日志组不存在(从未被调用)
```

### 一个我一开始误判、核对代码后纠正的点

我最初看到 **EventBridge Scheduler 有 0 个 schedule**、且 `gp-window-flush` **没有资源策略**,以为它"完全没有触发器、是死代码"。核对 `/home/ec2-user/works/graph-dependency-platform/rca/core/alert_buffer.py` 的 `_schedule_flush()` 后确认这是**设计如此**:

- schedule 由 rca-engine 在运行时**按需创建**(`at(<窗口结束+5s>)` 一次性定时器),并带 `ActionAfterCompletion: DELETE` **自动删除**。所以空闲时 0 个 schedule 是**正常状态**。
- 该 schedule 携带 `RoleArn`,EventBridge Scheduler 走 **IAM 角色**调用 Lambda,**不需要**资源策略。所以"没有资源策略"也**不是缺陷**。

> 教训:AWS 资源"缺失"不等于配置错误,得先读代码确认设计意图。

---

## 2. 根因证据链:确实没有任何告警触发

四条独立证据互相印证:

| 证据 | 数据 |
|---|---|
| SNS 发布量(近 30 天) | `petsite-rca-alerts` = **0**、`rca-alerts` = **0**(对照:`petsite-ops-alerts` = 2,发生于 8/8 与 8/9) |
| 4 个 RCA 告警状态 | 全部 **OK**,末次状态变更在 2026-03/04 |
| 告警历史 API | `DescribeAlarmHistory` 返回 **0 条**(CloudWatch 保留约 2 周,即近 2 周无状态跳变) |
| `gp-alert-buffer` | **0 项** |

**结论:分析侧零调用是"输入端没有信号",而非"处理链路损坏"。** 平台处于正确的空闲待触发状态。

---

## 3. 发现的真实缺陷:`rca-alerts` 路径缺权限

**有两个 SNS 主题订阅了同一个 Lambda:**

| 主题 | 订阅 `petsite-rca-engine` | Lambda 资源策略是否允许 |
|---|---|---|
| `petsite-rca-alerts` | ✅ 有订阅 | ✅ **允许**(Sid `rca-sns-trigger`) |
| `rca-alerts` | ✅ 有订阅 | ❌ **不允许** |

`petsite-rca-engine` 的资源策略**只有一条语句**,条件为 `ArnLike AWS:SourceArn = arn:aws:sns:...:petsite-rca-alerts`。

**后果:任何向 `rca-alerts` 主题发布的消息,在投递到 Lambda 时会因权限不足而失败。** 因为该主题近 30 天发布量为 0,这个缺陷至今**从未暴露**——属于潜伏问题。

修复二选一(**均为写操作,需你批准**):
- **方案 A(推荐,更干净)**:删除 `rca-alerts` 主题上指向 `petsite-rca-engine` 的冗余订阅,统一只用 `petsite-rca-alerts`。
- **方案 B**:为 Lambda 补一条 `AddPermission`,允许 `rca-alerts` 作为 SourceArn。

建议先确认 `rca-alerts` 是历史遗留还是有其他生产者在用,再决定。

---

## 4. 时间线事实:rca-engine 最后一次运行是一次部署后的冒烟测试

| 事件 | 时间(UTC) |
|---|---|
| `petsite-rca-engine` 代码/配置末次修改 | 2026-04-16 09:00:37 |
| `AlertBufferStack` 创建完成 | 2026-04-22 04:51:43 |
| **`petsite-rca-engine` 末次实际执行** | **2026-04-22 04:53:30** |
| 图谱内最新 Incident 节点 | 2026-04-16 |
| 图谱内最新 ChaosExperiment | 2026-04-02 |

rca-engine 的最后一次运行在 `AlertBufferStack` 创建完成后 **不到 2 分钟**——几乎可以确定是部署后的一次手工冒烟测试,而不是真实告警驱动。

**这意味着:告警聚合窗口(alert buffer)这条路径自部署以来从未在真实场景下端到端跑过。** `gp-window-flush` 的日志组不存在,证明它**一次都没被调用**。

---

## 5. 混沌 → RCA 的集成方式(纠正一个可能的误解)

我一开始注意到 5 个 `chaos-*-sr-critical` 告警指向 `petsite-ops-alerts`(只连 Slack 通知器)而非 `petsite-rca-alerts`,怀疑"跑混沌实验不会触发 RCA"。

核对 `/home/ec2-user/works/graph-dependency-platform/chaos/code/runner/rca.py` 后确认**这不是缺陷**:

> `rca.py` 头部注释:**"Lambda 入参格式(直接 invoke,非 SNS)"**
> 入参:`{ "affected_resource": "petsite", "source": "chaos-runner" }`

chaos 模块通过 `RCATrigger` **直接 invoke** `petsite-rca-engine`,完全绕过 SNS 路径。而 `chaos-*-sr-critical` 告警的真实用途是 **FIS 实验的 stop-condition 护栏**(见 `chaos/code/infra/fis_setup.py`),用于在实验超出阈值时中止实验,不是 RCA 触发器。

**对 P0 验证的影响:跑一次混沌实验确实能验证 RCA 引擎本身,但走的是直接 invoke 路径,验证不到 `告警 → SNS → buffer → window_flush` 这条链。** 两条路径需要分开验证。

---

## 6. 其他观察

- **59 个告警中 28 个没有任何 action**,即触发后不通知任何人。绝大多数是 CloudWatch Application Insights 自动创建的(无害),但以下几个值得确认是否有意为之:
  - `DocumentDB-ErrorRate-High`(描述为 FIS docdb 实验护栏)
  - `chaos-petstatusupdater-sr-critical`(**其他 4 个同类告警都有 action,只有它没有**——疑似遗漏)
  - `devops-agent-slack-chatbot-{apigw-5xx,ddb-throttle,errors}`
- **告警 action 目标分布**:`petsite-ops-alerts` 22 个、`kronos-alarms` 5 个、`petsite-rca-alerts` **仅 4 个**。也就是说 22 个运维告警只会发 Slack,**不会进入 RCA 分析**。这是设计选择还是漏配,值得确认——若希望更多信号驱动 RCA,这里是最直接的扩面点。

---

## 7. 建议的验证路径(按可信度排序)

### 路线一:验证 SNS 全链路(推荐,最贴近真实)
向 `petsite-rca-alerts` 主题发布一条模拟 CloudWatch Alarm 格式的消息,观察:
1. rca-engine 是否被调用(看日志组是否出现新 stream)
2. `gp-alert-buffer` 是否写入条目
3. 是否创建了 `gp-flush-*` 一次性 schedule
4. 窗口到期后 `gp-window-flush` 是否首次产生日志组
5. Neptune 是否新增 Incident 节点

这条路径能一次性验证**目前从未跑通过的 buffer + window_flush 环节**。
⚠️ 需要 `sns:Publish` 写权限,且会触发真实的 Slack 通知。

### 路线二:直接 invoke rca-engine(最小侵入)
按 `rca.py` 的入参格式直接调用:`{"affected_resource": "petsite", "source": "chaos-runner"}`。
验证 RCA 引擎五步分析本身是否仍工作(DeepFlow 查询、Neptune 遍历、Bedrock 报告)。
**但验证不到 SNS/buffer 链路。**

### 路线三:跑真实混沌实验(最完整但风险最高)
chaos-mesh v2.8.1 已就绪。走 chaos runner 的直接 invoke 路径,能验证 RCA 准确性并回写 `ChaosExperiment` + `TestedBy`。
⚠️ 会对 PetSite 注入真实故障。

---

## 8. 需要决策的写操作

以下操作我**未执行**,均需你批准:

| 操作 | 风险 | 目的 |
|---|---|---|
| 修复 `rca-alerts` 权限缺口(方案 A 删订阅 / B 补权限) | 低 | 消除潜伏缺陷 |
| 向 `petsite-rca-alerts` 发布测试消息 | 中(会发真实 Slack 通知) | 验证全链路 |
| 直接 invoke `petsite-rca-engine` | 低 | 验证引擎本体 |
| 为 `chaos-petstatusupdater-sr-critical` 补 alarm action | 低 | 补齐与同类告警的一致性 |

---

*本报告所有数据均为 2026-08-28 实测,非引用文档。诊断结论经代码交叉核对(`alert_buffer.py` / `rca.py` / `fis_setup.py`),避免把"设计如此"误判为缺陷。*
