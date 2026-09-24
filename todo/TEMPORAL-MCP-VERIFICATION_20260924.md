# temporal-mcp 验证矩阵(阶段 B)

**方法**:先对着 Temporal 官方 OpenAPI 规范
(`https://raw.githubusercontent.com/temporalio/api/master/openapi/openapiv3.yaml`)
做**离线**比对,把「一定会坏」和「要看服务端版本」的分开,再拿活集群逐个实测。
这样不必等 EC2 就能先缩小范围。

**日期**:2026-09-24 离线比对完成,实测待 Temporal 服务端就绪。

---

## 一、先纠正 README 里的一处错误

`src/client.ts:116` 的报错文案与 README 都举例 `http://localhost:8080`。
**8080 是 Temporal Web UI 的端口,不是 HTTP API 的。**

    Temporal HTTP API 默认端口 = 7243
    它是 WorkflowService(gRPC) 的 grpc-gateway,把 REST/JSON 子集映射到 /api/v1

按 8080 配会连到 Web UI,得到的错误不会指向真因。**这一条要改。**

## 二、我判错的两处(记下来,免得再判一次)

我最初怀疑这两组端点是编造的,**都错了**:

| 端点 | 我的怀疑 | 规范里的事实 |
|---|---|---|
| `workflows/{id}/pause` `/unpause` | Temporal 没有 workflow 级 pause | **存在** —— `PauseWorkflowExecution` / `UnpauseWorkflowExecution` |
| `namespaces/{ns}/activities` | activity 不是可独立列举的资源 | **存在** —— `ListActivityExecutions` / `DescribeActivityExecution` |

但两组都带条件,这才是要实测的点:

- 两组都标 **"This is an experimental API and the behavior may change"**
- 而且受 **namespace capability 门控**:`DescribeNamespace` 的
  `capabilities.workflowPause` 与 `capabilities.standaloneActivities`。
  服务端版本不够时这些工具会失败,**而失败不是 temporal-mcp 的缺陷**。

👉 **实测第一步就该是 `describe_namespace`,把 capabilities 抄下来** ——
它决定了哪些工具在这套服务端上根本不适用。判据教训:
**看到一个端点像是编造的,先去规范里查,不要凭印象。**

## 三、离线就能确认的实质缺口

### ⚠️ `start_workflow` 只发三个字段,DR 场景不够用

`handleStartWorkflow` 构造的 body 只有:

    workflowType: { name }
    taskQueue:    { name }
    input:        { payloads: [ ... ] }    （可选）

而规范的 `StartWorkflowExecutionRequest` 还支持这些,**对 DR 都不是可选项**:

| 字段 | 为什么 DR 需要它 |
|---|---|
| `workflowExecutionTimeout` / `workflowRunTimeout` | 卡住的切换必须有服务端侧的上界。缺了就只能靠人盯 |
| `memo` | 记「这是哪份计划、哪个 region、生成于何时」。缺了事后无从对账 |
| `searchAttributes` | 按 region / 计划 ID 检索历次切换。缺了只能靠 workflow_id 命名约定 |
| `retryPolicy` | 切换步骤的重试语义 |
| `identity` | 谁发起的切换 |
| `requestId` | 幂等去重(需实测服务端是否强制) |

`encodePayload` 把 input 包成 `json/plain` 的单个 payload,格式是对的。

**payload 大小上限不要猜**:`DescribeNamespace` 返回
`NamespaceInfo.limits.blobSizeLimitError`,是运行时可查的真值。
本仓库的 region 切换计划样例有 1219 行 markdown,必须先量一下再决定
「整份计划进 input」还是「input 只放引用」。

### 方法与路径比对:抽查三处都对

    workflow-count    GET  + query 参数        ✓ 与规范一致
    list_workflows    GET  + query/pageSize   ✓ 与规范一致
    start_workflow    POST /workflows/{id}    ✓ 与规范一致

(规范里 `{execution. workflow_id}` / `{task_queue. name}` 这类是 grpc-gateway 的
字段路径参数,替换后就是普通的 id/name,temporal-mcp 的拼法没问题。)

### 待核:nexus 端点路径

规范里 nexus 相关路径同时出现 `/api/v1/nexus/endpoints` 与
`/cluster/nexus/endpoints/{id}/update` 两种形状(生成的 spec 自身不一致),
而 temporal-mcp 统一用 `/api/v1/nexus/endpoints`。
**这一条只能实测**,离线判不了。优先级低 —— DR 场景用不到 Nexus。

## 四、36 个工具的实测优先级

DR 场景真正用得到的排前面。**先做只读的,确认连通再动写操作。**

### P0 —— 不通就没法往下走

    describe_namespace     先抄 capabilities 与 limits（决定后面哪些工具适用）
    get_cluster_info       连通性 + 服务端版本
    list_namespaces        只读冒烟

### P1 —— DR 主链路

    start_workflow         启动切换（注意上面那六个缺失字段）
    describe_workflow      看切换状态
    query_workflow         查进度（需 worker 实现 query handler）
    signal_workflow        人工审批/放行某一步
    get_workflow_history   事后复盘：每一步何时做、结果如何
    list_workflows         历次切换
    describe_task_queue    **worker 在不在** —— 没有 poller 就没人执行

### P2 —— 「保存计划」的候选机制

    create_schedule / describe_schedule / list_schedules / delete_schedule

一个**暂停状态的 Schedule** 可以承载「已保存但未触发的切换计划」,
靠 `PatchSchedule` 的 `triggerImmediately` 手动触发。
⚠️ 但规范里 `ScheduleAction.startWorkflow` 明确说
**`workflow_id_reuse_policy` 与 `cron_schedule` 两个字段无效**,
且启动的 workflow id 会被追加时间戳 —— 除非设
`SchedulePolicies.keepOriginalWorkflowId`。这些都要实测确认 temporal-mcp 透传了没有。

### P3 —— 控制面

    terminate_workflow / cancel_workflow / pause_workflow / unpause_workflow
    （后两个受 capabilities.workflowPause 门控）

### P4 —— DR 用不到,能通就行

    activities 系列（受 capabilities.standaloneActivities 门控）
    batch-operations / nexus-endpoints / worker-deployments
    workflow-rules / search-attributes / task-queues 的其余部分

## 五、现有测试的覆盖真相

`npm test` 是 16 passed,但:

    test/client.test.ts    16 个测试，只覆盖 src/client.ts
    src/tools/*.ts         ~3400 行，**零覆盖**

而且 16 个全是离线单测,不触达任何真实 Temporal API。
**「build 通过 + 测试全绿」证明不了任何一个工具能用** ——
这一条已写进台账的 tried_approach,不要再把它当作已验证。

改完任何工具后,新增测试必须**反向验证**(注入已知缺陷确认变红)。
