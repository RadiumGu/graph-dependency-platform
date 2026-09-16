# 任务：接 AWS DevOps Agent 闭环 —— 主动发起调查 / 消费官方计划 schema / 带门禁的评分卡

**创建**：2026-09-15　**来源**：读完 AWS 工作坊
`Closed-Loop Resilience with AWS FIS and DevOps Agent: Break It, Fix It, Prove It`
（`39ba0428-00f1-4206-a186-1468186180c3`）之后的三项落地

---

## 背景：为什么是这三件事

我们的 MCP server 已经把 23 条图谱查询暴露给 DevOps Agent（`mcp/README.md`），
但那份 README 自己记着一个实测数字：

| 提问方式 | 会去查图谱 | 比例 |
|---|---|---|
| 不提图谱 | 2/8 | **25%** |
| 点名要求查图谱 | 4/4 | **100%** |

**能力已经建好，但被用到的概率只有四分之一。** 这是全项目投入产出比最差的一处
落差 —— 不是缺功能，是缺"让它一定被用上"的机制。

工作坊给了三把钥匙，都不需要我们重建已有的东西：

1. `aws devops-agent create-backlog-task` 能**由我们**发起调查，
   而它的 `description` 是自由文本 —— 查图谱的指令可以写进任务本身；
2. 处置计划有**官方 JSON 契约**，我们的处置层现在是自己一套分类；
3. 评分卡把混沌工程从"我们学到了些东西"变成"恢复目标达成/未达成"。

---

## 前置事实（2026-09-15 实测，别再重新查一遍）

### CLI 可用，命令集很全

`aws-cli/2.36.34` 自带 `aws devops-agent`，58 个子命令。与本任务相关的：

```
create-backlog-task    发起调查（taskType=INVESTIGATION）
list-journal-records   读调查时间线与 findings
list-recommendations   读建议
list-executions        查执行状态
create-trigger         按条件自动触发（condition 支持 schedule.expression）
update-approval-action 人在环的批准动作
associate-service      关联 MCP server 等外部工具
validate-aws-associations
```

### ⚠️ agent space 在**东京**，不在 us-east-1（我第一版查错了 region）

```
$ aws devops-agent list-agent-spaces --region ap-northeast-1
petsite-devops   agentSpaceId=60c2f48f-b6e3-4dce-a0a3-4144228b2051
                 createdAt=2026-05-17  locale=zh-CN
```

我第一版只查了 `us-east-1`（因为 AWS 博客写"agent 本身跑在 us-east-1"），
拿到空列表就写下"agent space 已不存在"。**错了** —— 服务确实在 us-east-1，
但 **agent space 是区域性资源，我们的建在东京**。

更该批评的是：这个 agentSpaceId **本来就硬编码在我们自己的
`scripts/emit_graph_coverage_metrics.py` 里**（`AGENT_SPACE` 常量）。
先查代码就不会错。写"某资源不存在"之前，先在仓库里 grep 一遍它的 ID。

### MCP 关联是活的

```
$ aws devops-agent list-associations --agent-space-id 60c2f48f-... --region ap-northeast-1
```

两条关联，都 `status: valid`：

| associationId | serviceId | 内容 |
|---|---|---|
| `1ab74ccb-…` | `aws` | `DevOpsAgentRole-AgentSpace-ir08y3xz`，accountType=monitor |
| `d96fc34a-…` | `47663b32-…` | **mcpserver，23 个 q* 工具全在** |

**所以三项都能端到端验证，不需要先建任何东西。**
`create-backlog-task` 的 `associationId` 字段填 `d96fc34a-27aa-416f-bba6-fea28660e235`
就能把调查指向我们的 MCP server。

DevOps Agent 服务本身只在 `us-east-1`，但 agent space 与调查都在东京跑。
**所有 `aws devops-agent` 调用都要显式带 `--region ap-northeast-1`。**

### 关键 schema（`--generate-cli-skeleton` 取得，不是猜的）

```json
// create-backlog-task
{
  "agentSpaceId": "",
  "reference": { "system": "", "title": "", "referenceId": "",
                 "referenceUrl": "", "associationId": "" },
  "taskType": "INVESTIGATION",
  "title": "",
  "description": "",          // ← 自由文本，这是我们的杠杆
  "priority": "CRITICAL",
  "clientToken": ""
}
```

```json
// 处置计划（工作坊 Module 2 正文给出的契约）
{
  "planId": "mp-...",
  "investigationId": "inv-...",
  "status": "READY",
  "confidence": "HIGH",
  "actions": [
    { "sequence": 1, "type": "...", "target": "...",
      "parameters": {}, "rationale": "..." }
  ],
  "rollback": {}
}
```

工作坊出现过的 action type：`SCALE_OUT`、`ISOLATE`、回滚。

### 工作坊里一个对我们是一等问题的坑

Module 2 正文：DevOps Agent **正确识别出 CPU 尖峰是 FIS 注入的**，
因此判定"这是演练"、**不给处置建议**。工作坊用一句"真实场景里它会建议扩容"带过。

**我们整个平台的故障都是 FIS / Chaos Mesh 打的。** 如果它一看到 FIS 就说是演练，
那闭环演练**验不了处置路径** —— 而这正是 Module 4 声称在做的事。

这一条必须实测确认，不能假设。它决定第三项评分卡里"处置成功率"这个维度
在演练环境下是否可测。

---

## 三项实施

### 第一项：主动发起调查（解决 25% 采纳率）

**做法**：写 `scripts/devops_agent_investigate.py`，用 `create-backlog-task`
从我们的告警/实验管道发起调查，并把**证据纪律写进 `description`**。

`description` 里要给的不是"请查图谱"这种客套，而是可执行的约束：

- 点名要调用哪几个 MCP 工具（`q1_blast_radius`、`q16_single_point_of_failure`、
  `q22_edge_verification_verdicts` 等）
- 明确告诉它：`refuted` 的边**不得用于推理**，`untested` 的边可用但必须声明未验证
- 要求引用具体实验 ID 与退化幅度，而不是"存在 FIS 模板"
  （模板是意图不是结果 —— README 已记下这个失败模式）

**判据**：不是"脚本跑通"，而是**采纳率**。发起 N 次，统计
`list-journal-records` 里有多少次真的调用了图谱工具。基线是 25%。

### 第二项：处置层消费官方计划 schema

**现状**：`rca/actions/playbook_engine.py` + `semi_auto.py` 是自己一套分类
（`_exec_db_connection` → `rollout_restart`、`_exec_single_az` → `scale_deployment`）。

**做法**：新增 `rca/actions/mitigation_plan.py`，把官方 schema 解析成内部动作，
并在执行前加两道闸：

1. **`confidence` 闸** —— 计划自称 `HIGH` 不等于可信。用**我们图谱的边级证据**
   交叉验证：计划要动的 target，它的依赖边是 `confirmed` 还是 `untested`？
   拿 `untested` 的边推出来的处置，不该按 HIGH 执行。
2. **`rollback` 必须存在** —— 没有回滚方案的计划不自动执行。
   这与"故障注入必须可自动恢复"是同一条纪律的另一面。

保留 Mode 1 / Mode 2 两态（工作坊自己也推荐先全走人在环）。
`semi_auto.py` 已经在这个位置，不重建。

### 第三项：评分卡，判据用我们的门禁

**不抄它的判据**。工作坊的评分卡量"每阶段耗时 + 是否恢复"，
存 DynamoDB，发 CloudWatch `ClosedLoop/Resilience` 命名空间
（`DrillRecoveryRate`、MTTR）。形态可以抄，判据不行。

**为什么不行**：我这个会话里两次把错误结论写进图谱，根因都是
"流程跑通了 ≠ 证明了什么"：

- `took_effect` 拿不等长窗口比绝对计数（基线 1800s / 注入 120s），
  `8470-548>0` 判"生效"，而速率是 4.71 → 4.57 次/秒，**根本没变**；
- 复合实验删了 Pod，观测方吞吐必然塌陷，拿它当"依赖被切断"的证据。

两次都产出 `dependency_class=soft`，而 `soft` 会被 DR 影响面分析读成
"这条依赖不影响可用性"并在预案里降级。**用测量缺陷得出的 soft 比 untested
危险得多。**

一份"注入了 → 告警响了 → 处置跑了 → 打勾"的评分卡，就是这类假证据的批量版本。

**我们的判据**：每条演练记录必须带
- `injection_confirmed`（速率级校验，见 `tests/test_70`）
- 观测窗与基线窗**等长**（见 `scripts/verify_external_target_edges.py` 的注释）
- 若用了复合手法，必须标明观测方信号是否被混淆（复合模式下不写 verify_status）

达不到这些的演练记为 `inconclusive`，**不计入恢复率的分子也不计入分母** ——
与 `verified_ratio_addressable_pct` 把永久天花板从分母里扣掉是同一个道理。

---

## 实施结果（2026-09-15 当天完成）

### 第一项 ✅ 成了，而且第一次就反驳了我

`scripts/devops_agent_investigate.py`。真发一次调查
（task `f385a566-…`，exec `exe-ops1-a1559ef5-…`），`list-journal-records`
里出现 **10 个不同的图谱工具**：

```
q22_edge_verification_verdicts  25 次
q1_blast_radius                 24
q3_upstream_deps                19
q16_single_point_of_failure     18
q18_chaos_history               16
q23_verification_coverage       14   ← 我没在 description 里点名
q21_observation_source_coverage 14   ← 同上
q17_incidents_by_resource       13   ← 同上
q20_dependency_verification      8   ← 同上
q14_cross_region_resources       1
```

**采纳率从 25% 提到了 100%**，而且它自己扩展到了我没要求的工具。

更有价值的是它的 `finding`：

> **petsearch 对 DynamoDB 的依赖已被确认（与用户前提冲突）**
> 用户前提认为依赖强度未定，但依赖图谱 q22 显示存在**两条 confirmed 边、
> 均为 100% 退化**（故障注入验证已确认）。仍需补齐 q16 SPOF 细节及
> 观测源对账后再下最终结论。
> `finding_type: hypothesis`

它引用了具体工具、标为**假设而非结论**、说明还缺什么 —— 正是
`description` 里那套证据纪律要的行为。

### 🔴 它反驳我的那件事是真的：我漏了第四种注入手段

核实图谱：

| 边 | verify_status | 退化 | 实验 |
|---|---|---|---|
| `petsearch -> ddbpetadoption`（DynamoDBTable） | **confirmed** | 100% | **`iam-deny-probe_20260913-155349`** |
| `petsearch -> dynamodb`（AWSServiceEndpoint） | confirmed | 100% | `exp-dynamodb-fis-network-disrupt-20260831-160742` |

第一条**正是我 2026-09-09 撤回的那条边**。9-13 另一个会话用
**IAM deny**（拿掉权限而不是切网络）验成了，干净的 100% 退化。

我当时的结论是「网络层打不断托管服务依赖，需要复合实验」——
那个结论就其范围而言没错，但我**没想到还有 IAM 这条路**：

```
NetworkChaos + externalTargets  打不断（端点 IP 轮换 / S3 是 Gateway 端点）
DNSChaos                        打不断（长连接 + DNS 缓存）
FIS API 注入                    AWS 只支持 ec2/kinesis
DNSChaos + 删 Pod（复合）       能打断，但观测方信号被混淆
IAM deny                        ✅ 干净打断，100% 退化
```

**IAM deny 免疫 DNS 缓存、IP 轮换、连接复用**，而且不需要 FIS 支持该 service ——
SDK 每次调用都要签名鉴权，权限没了立刻拿到 `AccessDenied`。
这应该是托管服务依赖验证的**首选**手段，我那三条死路本来可以不用走。

### 第二项 ✅ `rca/actions/mitigation_plan.py`

官方计划 schema 的消费层 + 两道闸（证据交叉验证、rollback 必存），
默认 Mode 2（人在环）+ `dry_run=True`。18 条门禁 `tests/test_72`。

`refuted` 的 target **抛异常拒绝**而不是降级 —— 那不是信心不足，
是推理前提为假。

### 第三项 ✅ `scripts/emit_resilience_scorecard.py`

三道门禁：注入确实生效（速率级）／观测方信号未被混淆／基线窗与注入窗等长。
不满足的记 `inconclusive`，**两边都不计**。8 条门禁 `tests/test_73`。

实测结果很尖锐：现有 8 条演练记录**全部证据不可信**，
恢复率报 `None` 而不是 0%。若照抄工作坊判据，这 8 条会全部打勾变成
「恢复率 100%」。

### ⚠️ 评分卡的已知缺口

它现在只读 `todo/chaos-external-edge-run_*.json`，也就是**我自己那轮实验**的
留痕。上面那次成功的 `iam-deny-probe` 不在这个 glob 里，所以没进评分卡 ——
这直接导致「8 条全部不可信」这个偏悲观的读数。

修法是把演练记录的来源从"某个脚本的留痕文件"改成**图谱上的
`verify_experiment` + `verify_degradation`**（那是唯一权威来源），
但要先确认这些字段够不够支撑三道门禁的判定
（尤其"基线窗与注入窗等长"目前只在留痕 JSON 里有）。留给下一轮。

---

## 执行顺序与理由

1. **第二项**（计划 schema）—— 纯代码、可离线穷举测试
2. **第一项**（发起调查）—— agent space 与 MCP 关联都是活的，可端到端测采纳率
3. **第三项**（评分卡）—— 判据依赖前两项的产物

## 环境约束（踩过的）

- **所有 `aws devops-agent` 调用必须带 `--region ap-northeast-1`**。
  默认 region 或 us-east-1 会返回空列表，而空列表看起来像"资源不存在"，
  不像"你查错地方了"。
- agentSpaceId `60c2f48f-b6e3-4dce-a0a3-4144228b2051`
  已硬编码在 `scripts/emit_graph_coverage_metrics.py`，别再另立一份。
- MCP 关联 associationId `d96fc34a-27aa-416f-bba6-fea28660e235`。

## 留给人决定的事

- **是否验证"FIS 标签导致拒绝出计划"**：需要真发一次调查并读
  `list-journal-records`。这是读操作 + 一次 agent 任务，成本低但会消耗
  agent task 配额（`get-account-usage` 可查）。
- **处置动作是否允许自动执行**：第二项默认只走 Mode 2（人在环），
  要不要对某几类场景开 Mode 1 由人定。
