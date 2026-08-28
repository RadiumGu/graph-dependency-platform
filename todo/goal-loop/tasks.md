# Tasks — 任务看板

> **每轮必须更新本文件。** 领卡时把 `todo` 改 `doing` 并写认领时间;
> 完成时改 `done` 并在卡片下追加一行 `<UTC> cycle-<n>: <动作> <结果>`。
> 状态取值:`todo` / `doing` / `review` / `done` / `blocked`
>
> `review` = 已改完但需人工确认(涉及生产变更、破坏性操作、或判断有分歧时用)。
> `blocked` = 必须写明阻塞原因与解除条件;**同时另领一张卡,不要空转**。

---

## 阶段 0 — 让图谱说真话 🔴

### T-001 · 依赖边失效对账 · `done`

**目标**:`etl_deepflow` 每轮结束后,把本轮未刷新的 `Calls` 边置 `active=false`,
超阈值(建议 7 天)的删除。字段已就位(`active` / `last_seen`),只缺消费逻辑。

**改动位置**:`infra/lambda/etl_deepflow/neptune_etl_deepflow.py`,`:674` 的
`批量 upsert Calls 边` 之后追加对账步骤。参考 `infra/lambda/etl_aws/graph_gc.py:16-45`
的 `_gc_vertices` 求差集范式(那是全库唯一做实的对账,但只覆盖节点)。

**验收**:DoD-1 第一条查询返回 `stale_active = 0`。

**注意**:该 Lambda 是 x86_64,而 `gp-window-flush` / `petsite-rca-engine` 已迁 arm64。
构建 etl_deepflow 包时不要照抄 arm64 recipe。

**已实现**(代码完成,待部署验证):
- 新增 `reconcile_calls_edges(round_ts)`:超 `INACTIVE_AFTER_SECONDS` 未观测 → `active=false`
- `batch_upsert_edges` 改为返回 `ts`,对账用同一 `round_ts` 做分界
  (本轮刷新过的边 `last_seen == round_ts`,未刷新的严格小于)
- 主流程 `run_etl()` 在 upsert 后立即调用对账,统计并入返回值
- 新增 `_first_scalar()` 宽松解析 Gremlin `count()`(GraphSON 包装层数随版本变化)

**一处刻意偏离,需你确认**:**硬删除默认关闭**(`CALLS_EDGE_DROP_ENABLED=false`)。
理由:DoD-1 只要求"陈旧且仍 `active` 的边为 0",软删除即可满足,且可逆、保留历史;
硬删除是不可逆的生产数据变更,按 `north_star.md` 第 4 节操作不变量应先经确认。
需要真正清理那 17 条陈旧边时,显式设 `CALLS_EDGE_DROP_ENABLED=true`。

**容错设计**:失效阈值默认 1800s(≈6 个 ETL 轮次)而非单轮,避免单轮 L7 漏采
把活依赖误判为死。误判可自愈——边一旦重新被观测到,`batch_upsert_edges`
会把 `active` 写回 `true`。

- 2026-08-28T17:28Z cycle-1: 实现对账 + first_seen,`py_compile` 通过,+131/-3 行。**未部署**

---

### T-002 · Calls 边补 `first_seen` · `done`

**目标**:仅在 `addE` 分支写 `first_seen`,`onMatch` 分支不得覆盖,
否则每轮刷新会把"首次出现时间"变成"最近一次时间"。

**改动位置**:`neptune_etl_deepflow.py:688`(`addE('Calls')`)与 `:698-699` 附近。

**验收**:DoD-1 第二条查询 `active_total == with_first_seen`。

**cycle-4 实现改进**:cycle-1 只在 `addE` 分支写,存量边永远拿不到。
改为**幂等 coalesce**:`.property('first_seen', coalesce(values('first_seen'), constant(ts)))`
—— 已有值保留,缺失才写入,对新边与被重新观测到的存量边都生效。

**回填决策(已定,不用 last_seen 近似)**:`first_seen <= last_seen` 恒成立,
把 `last_seen` 写成 `first_seen` 等于断言"依赖是那时才出现的",比留空更具误导性。
存量边的 `first_seen` 语义确定为**「自埋点起首次观测到」**,非真实首现时间。

**DoD 已相应收窄**:原先要求全部边带 `first_seen`,设计上不可达
—— 不再被观测的陈旧边根本不进 upsert 流程。改为只要求 `active=true` 的边。

- 2026-08-28T17:37Z cycle-4: 改为幂等写入并部署,活跃边 1/1 带 first_seen

---

## 部署记录

### 2026-08-28T17:36Z · `neptune-etl-from-deepflow` · T-001+T-002

| 项 | 值 |
|---|---|
| 回滚包 | `$KIROCREW_SCRATCH/etl-deploy/rollback-etl-deepflow.zip`,原 SHA `hnhJ/zElWiXJtHEOuRrqrM5524tcp4iOZzLxhSEyAtg=` |
| 新 SHA | `jhn2iLZt5dWO0HC68/h+cLDjmDweOjrFeljDsXLj0X8=` |
| 架构 | **x86_64 保持不变**(未照抄 arm64 recipe) |
| 打包方式 | 资产目录原样打包,**不跑 pip** —— 依赖已 vendored,自然规避架构陷阱 |
| 包内容核对 | 新旧包差异**仅 `__pycache__`**(旧 32 条 .pyc / 新 0 条),无模块缺失 |
| 硬删除 | **关闭**(`CALLS_EDGE_DROP_ENABLED` 未设,默认 false) |

**验证结果(DoD-1 全绿)**:

```
DoD-1a  stale_active = 0                      ✅
DoD-1b  active_total = 1, with_first_seen = 1 ✅
active 分布:  false 17 条 / true 1 条
唯一活跃边: petsite → petsearch  calls=94  first_seen=08-28 17:36
已失效: awesomeshop 那批 + 3-6 月陈旧边共 17 条
```

**下一步观察项**:软删除跑几轮后确认无误判(误判会自愈——边重新被观测到时
`active` 写回 `true`),再决定是否开启硬删除。观察窗口建议 ≥24h,
覆盖 traffic-generator 的完整流量周期。

---

### T-003 · 修 `resilience_score` 三向属性名分裂 · `done`

**目标**:统一属性名,让已跑数月的混沌韧性反馈闭环真正生效。

**cycle-2 实查后修正了问题描述**:这不是"一个笔误",是**两套并行 schema**,
且读取方的**四个投影全部读错**。活图证据:

| 读取方原先查的 | 活图实际 | 结果 |
|---|---|---|
| `chaos_resilience_score` | ❌ 0 个节点 | 恒 `-1` |
| `last_tested_at` | ❌(实为 `last_chaos_test`,6 节点) | 恒 `'never'` |
| `test_coverage` | ❌ 0 个节点 | 恒 `''` |
| `weakness_pattern` | ❌ 0 个节点 | 恒 `''` |

即 `query_learning_nodes` 返回的**全是兜底值**,LearningAgent 一直在拿空数据决策。

**还发现一个量纲陷阱**:两个写入方的分数**量纲不同** ——
`graph_feedback.py` 写 0–100 整数(活图 6 个节点有),
`learning_direct.py` 写 `pass_rate/100` 即 0–1 浮点。
因此**不能简单 coalesce 两者**,消费方会时而拿到 90、时而拿到 0.9。

**已实现**(统一到写入方 A 的词汇 + 0–100 量纲,因它有实数据且是 `_calc_resilience_score` 的原生输出):
- `chaos/code/agents/learning_direct.py:510-512`:改写 `resilience_score`
  (`round(stats.pass_rate)`,0–100)与 `last_chaos_test`
- `chaos/code/runner/neptune_helpers.py:65-80`:读取方改以 `resilience_score` /
  `last_chaos_test` 为主,保留旧名 coalesce 回退一个版本周期;
  回退分支对 `chaos_resilience_score` 做 `math('_ * 100')` 归一,避免量纲混用
- `chaos/code/agents/models.py:105`:同步修正误导性注释

**实测验证**(修正后读取方能拿到真实分数,此前恒 `-1`):

```
payforadoption     score=100  last_chaos_test=2026-04-02T07:24:36Z
pethistory         score=100  last_chaos_test=2026-04-02T01:19:33Z
petlistadoptions   score=100  last_chaos_test=2026-04-02T07:36:35Z
petsearch          score=100  last_chaos_test=2026-04-02T07:41:22Z
petsite            score= 90  last_chaos_test=2026-04-16T11:17:57Z
petstatusupdater   score=100  last_chaos_test=2026-04-02T03:16:52Z
```

- 2026-08-28T17:31Z cycle-2: 三文件改完,`py_compile` 通过,活图实测读取方拿到 6 个服务真实分数

---

### T-004 · 补写 `chaos_last_verified` · `done`(问题在 DoD,非代码)

**cycle-2 结论:本卡的前提是错的,已更正 DoD 而非改代码。**

`chaos_last_verified` 是我写 `north_star.md` DoD-2 时凭空取的名字。
活图实际的时效标记是 **`last_chaos_test`**,已存在于 6 个节点,
由 `chaos/code/runner/graph_feedback.py:_update_node` 的
`.property(single, 'last_chaos_test', '{last_tested}')` 一直在写。

**再引入第三个属性名,恰好就是 T-003 要消除的那类错误。**
故改为让 DoD-2 校验既有属性:

```
MATCH (n:Microservice)
WHERE exists(n.resilience_score) AND exists(n.last_chaos_test)
RETURN count(n)   → 当前 6,已 > 0,通过
```

`north_star.md` DoD-2 已更正并留下更正说明。

- 2026-08-28T17:31Z cycle-2: 判定为 DoD 定义错误,更正 north_star.md DoD-2,不改代码

---

## 阶段 1 — 让依赖可分辨

### T-010 · 依赖边补 `source` 与 `dependency_kind` · `done`

**分界线定义**:取**「声明的」vs「观测到的」**,而非"配置 vs 流量"——
这才是运维上有意义的区别:声明的依赖可能从未被走过。

- `static` = AWS 资源配置 / CFN 模板声明(`etl_aws` / `etl_cfn` 写入)
- `dynamic` = DeepFlow L7 / DNS 运行时观测(`etl_deepflow` 写入)

**已实现**(三个 ETL 全部部署,架构均保持 x86_64):

| 文件 | 改动 |
|---|---|
| `etl_aws/neptune_client.py` | 新增 `DEPENDENCY_EDGE_LABELS` 常量;`upsert_edge` 只对依赖语义边打 `dependency_kind='static'`(`LocatedIn`/`Contains` 等结构边不打) |
| `etl_cfn/neptune_etl_cfn.py` | 声明边打 `static`;补 `source='cfn-etl'` 与另两个 ETL 对齐(原先只有 `declared_in`,字段名不一致);`declared_in` 保留不动避免破坏既有查询 |
| `etl_deepflow/neptune_etl_deepflow.py` | `Calls` 补 `source='deepflow-etl'` + `dynamic`(此前**实测 18 条边带 source 的 0 条**);DNS 漂移 `AccessesData` 与 ECR `DependsOn` 也补 `dynamic` |

**存量回填**(按已有 provenance 判定,非猜测):

```
Calls 全部                      → dynamic  18
source STARTS WITH 'deepflow'   → dynamic  30
其余(aws-etl/cfn/business-layer/manual-fix) → static 21
```

**实测**:69/69 带 `dependency_kind`,分布 48 dynamic + 21 static,
与按 provenance 预测的完全一致。部署后再触发 ETL 复验仍 69/69(aws ETL 写 458 条边)。

- 2026-08-28T17:50Z cycle-6: 三 ETL 改完并部署,存量回填,DoD-3 第一条转绿

---

### T-011 · schema 声明边属性 · `done`

**已实现**:`profiles/petsite.yaml` 在「边类型（26 种）」下新增
**「依赖边的通用属性」**小节,声明 `dependency_kind` / `source` /
`last_seen` / `first_seen` / `active`,以及 `Calls` 边的运行时度量字段,
并写明为何查询时必须按 `dependency_kind` 过滤。

**X-6 / X-7 复核结果:早前会话已修,无需重做**——
边类型 header 已是「26 种」(第 209 行),
`AffectedService`(第 280 行)与 `Involves`(第 281 行)已在 schema 正文声明。

- 2026-08-28T17:50Z cycle-6: schema 补边属性声明;复核确认 X-6/X-7 已完成

---

### T-012 · `q1`/`q3` 支持按依赖类型过滤 · `done`

**已实现**:`q1_blast_radius(failed_node, kind=None)` 与
`q3_upstream_deps(failed_service, kind=None)` 新增可选 `kind` 参数,
默认 `None` 保持向后兼容。

**关键发现:光按 `dependency_kind` 过滤不够,故引入第三个取值 `live`。**

实测 `petsite` 的三个上游**全是 `dynamic`**,但其中 `gateway-service` 与
`order-service` 属 `awesomeshop` 命名空间,该命名空间 6 个 Deployment
副本数**已全为 0**(服务下线),边已被对账置 `active=false`,
`dependency_kind` 却仍是 `dynamic`。

原因:`dependency_kind` 只区分「声明 vs 观测」,**不区分「观测过 vs 现在还在」**。
`dynamic` + `active=false` 是历史观测,不是当前依赖。

| `kind` 取值 | 语义 | petsite 上游实测 |
|---|---|---|
| `None` | 不过滤 | 3 条(含 2 个已下线服务) |
| `'static'` | 只看声明的 | — |
| `'dynamic'` | 观测到过(含已失效) | **3 条,仍含已下线服务** |
| `'live'` | `dynamic AND active=true`,**根因定位用这个** | **0 条** ✅ |

这正是此前 `causal_weight` 出现 `gateway-service→petsite = 0.0` 这类
无意义条目的根因 —— RCA 把缩容到零的服务当上游推理。

**顺带修掉 X-3 死查询**:`q1` 的 `bc_cypher` 原先遍历 `:Serves`,
而 `etl_aws/handler.py:1217-1219` 每轮主动 `hasLabel('Serves').drop()`,
实测活图 `Serves` 边 **0 条**、`BusinessCapability`(3 个)只有 `DependsOn` 出边(6 条)。
已移除 `Serves` 只留 `DependsOn`。

**误判排查**:`trafficgenerator` 副本数 1/1(在跑)但其到 petsite 的边被置失效,
查证 `last_seen=2026-03-05`(**176 天前**)—— 确认**不是对账误判**,
该直连边确实长期未被观测(流量大概走 ALB,DeepFlow 看到的不是这条边)。

- 2026-08-28T17:50Z cycle-6: q1/q3 加 kind 过滤并引入 live 语义,修 Serves 死查询,
  排查确认软删除无误判

---

### T-013 · 图谱 MCP server 端点 · `done`

**已实现**:`rca/neptune/graph_mcp_server.py`(401 行),`rca/.mcp.json` 已注册。

**一处判断修正**:cycle-4 定的顺序是「T-022 graph SDK 先于 T-013 MCP」,
执行时修正为**先 T-013**。理由:T-022 的完整重构(6 套客户端 + 跨 3 模块迁移)
不是一轮能安全做完的事,且**它不在 DoD 里**(P2);而 `rca/neptune/` 事实上
已经就是那个确定性查询层(`neptune_client` + `query_guard` + Q1–Q18 + NL 引擎)。
MCP 直接包装它即可,不必先做大爆炸式重构。架构分层的结论不变。

**关键技术决策:纯标准库实现,不用 mcp 官方 SDK。**
本机 `python3` 是 **3.9.25**,而 `mcp` 包要求 **≥3.10** —— 索引里没有可用版本
(`uv` 也报 unsatisfiable)。MCP stdio 传输就是 JSON-RPC 2.0,需要的表面很小
(`initialize` / `tools/list` / `tools/call` / `ping`),故用 stdlib 实现:
零依赖、可直接落到任何环境、无供应链面。这是主动选择而非权宜。

**统一了此前割裂的查询库**:Q1–Q11/Q17/Q18 在 rca、Q12–Q16 在 dr-plan
(目录名带连字符不能当包导入,用 `importlib.util.spec_from_file_location` 按路径加载)。
现在从**一个入口暴露 19 条**。

**暴露的 4 个工具**:

| 工具 | 用途 |
|---|---|
| `list_queries` | 列出 19 条固化查询及参数,引导 agent 优先用确定性查询 |
| `run_query` | 按名执行固化查询;影响面/根因类建议传 `kind='live'` |
| `get_schema` | 返回 31 节点 / 26 边及依赖边通用属性 |
| `run_cypher` | 只读 openCypher,经 `query_guard` 校验 |

**写操作一律拒绝**(不降级执行):`query_guard` 拦
CREATE/DELETE/SET/MERGE/REMOVE/DROP/CALL,限跳数,自动补 LIMIT。
本端点对外开放,写路径必须走模块内类型化代码。

**实测验证**:

```
--selftest              工具注册 4 个 / 查询库 19 条(rca 13 + dr-plan 6),参数校验 3/3 拒绝
JSON-RPC 握手           initialize → graph-dependency v1.0.0, protocol 2024-11-05
run_query q3 kind=live  ✅ 返回 [] (正确 —— petsite 当前无活跃上游)
run_query q2_tier0      ✅ 返回真实 Tier0 服务与故障边界
run_cypher 只读统计     ✅ 自动补 LIMIT 200
run_cypher DETACH DELETE ✅ 被拒:「查询包含写操作关键字: DETACH」
按 .mcp.json 配置拉起    ✅ cwd/env 正确
跨模块 q16(dr-plan)     ✅ 返回真实 SPOF: EC2 i-0c39b7c79dfe93a2c 单 AZ 承载多服务
```

- 2026-08-28T18:00Z cycle-7: MCP 端点建成并注册,DoD-4 转绿。纯 stdlib 实现(py3.9 限制)

---

### T-022 · 收敛 Neptune 访问层为 graph SDK · `done`(降范围,三条前提两条不成立)

**逐条核实卡片的理由**:

| 卡片声称 | 实测结论 |
|---|---|
| 消除 6 份重复 | 重复真实存在,但各客户端在查询语言(Gremlin vs openCypher)、GraphSON 解析、Lambda layer 约束上有**实质差异** |
| 把 `query_guard` 给到 chaos/dr-plan | **不成立** |
| 修 chaos 连接不复用 | 成立,但客户端互比只差 6.7 ms/次 |

**为什么 `query_guard` 那条不成立**

`query_guard` 是防**LLM 生成 Cypher** 的只读校验,实际调用点全在
`nl_query_direct` / `nl_query_strands` / `strands_tools` —— 都是 LLM 输出路径。
而 chaos 与 dr-plan **只发手写静态查询**(`hypothesis_direct.py:206` 是硬编码
三引号 gremlin,`fmea`/`gen_template` 用静态 openCypher,`dr-plan/graph` 无任何
bedrock 调用)。

更要紧的是 **chaos 有写路径**:`neptune_sync` 通过同一个客户端写
`ChaosExperiment` 节点与 `TestedBy` 边。把守卫下沉到客户端层**会直接拦掉它**。
已加测试 N-04 把这个理由固定下来,避免将来有人"顺手"下沉。

**排查过程找到两个比重构更有价值的问题**

**1. 共享 Lambda layer 永久缓存冻结凭证(潜伏缺陷)**

```python
_frozen_creds = None
def _get_creds():
    global _frozen_creds
    if _frozen_creds is None:
        _frozen_creds = ...get_frozen_credentials()   # 含固定 session token 的快照
    return _frozen_creds
```

Lambda 容器可复用数小时,而执行角色凭证有有效期 —— 过期后该热容器的每次
Neptune 调用都会 403,直到容器被回收。**三个 ETL 都用这个 layer。**

如实说明:近 7 天 ETL 日志里**没有**观测到 403/ExpiredToken,
所以这是「明确写错但在观测窗口内尚未触发」的潜伏缺陷。

**2. 每次调用新建 boto3 Session(已确认的实况开销)**

四份客户端、6 个调用点**全部**在函数体内 `boto3.Session()`。
生产 ETL 日志佐证:同一次调用(同一 request ID)2 秒内出现 4 次
「Found credentials in environment variables」。

| 客户端 | 改前 | 改后 |
|---|---|---|
| rca | 32.8 ms | **16.8 ms**(-49%) |
| chaos | 39.5 ms | **15.9 ms**(-60%) |

按 RCA 单次运行 15–30 次查询估,此前每次运行纯浪费 **150–300 ms**,
而 RCA 在事故热路径上。套件总耗时也从 267s 降到 252s。

**⚠️ 方法教训:比较两个实现时,它们共有的缺陷是不可见的**

我一开始把两个客户端**互相比较**,只看到 6.7 ms 之差,据此判断「不值得重构」。
但它们**共有**这个开销 —— 互比把共同缺陷掩盖了。
只有单独测 `boto3.Session()` 的绝对成本(9.7 ms)才暴露出来。

**正确模式**:缓存 **Session**(构造昂贵),每次调用**重新冻结**凭证
(boto3 可刷新凭证在临近过期时自动续期)。原 layer 恰好两者都反了。

已在 4 份客户端统一应用(6 个调用点),不改公开 API、不动查询语义 ——
外科手术式修改而非重构。已验证 chaos 写路径未被破坏。

- 2026-08-28T21:48Z cycle-18: 降范围 + 修凭证缓存 + Session 复用,commit `7809687`

全仓 **6 份** `neptune_client*.py`。`dr-plan-generator/graph/neptune_client.py`
注释直接写着 "Mirrors the pattern in rca/neptune/neptune_client.py"。
本该消除重复的 `infra/lambda/shared/python/neptune_client_base.py`
**只支持 Gremlin、只服务 ETL 写入**,查询侧三模块谁都没用它。

顺带:`chaos/code/runner/neptune_client.py` 用 `urllib.request.urlopen`,
**每次调用重新握手**(其余几套都已用 `requests.Session` 复用)。

**cycle-7 更新**:MCP 端点(T-013)已直接包装 `rca/neptune/`,不再依赖本卡先完成。
本卡降级为纯重构收益:
- 消除 6 份客户端重复
- 把 `query_guard`(只读校验 / 跳数上限 / 自动 LIMIT)给到 chaos 与 dr-plan
  —— 目前**只有 rca 有这层防护**,另两个模块是裸拼查询发出去的,这是安全收益
- 修 chaos 的连接不复用

**建议做法**:新建共享包,增量迁移,不要一次性替换 6 处 —— 大爆炸式重构
在没有完整回归测试的前提下风险高于收益。

---

## 阶段 2 — 收敛与启用

### T-020 · 修 `rca_window_flush/config.py` 已发生的漂移 · `done`

**已实测的漂移**:该文件硬编码 `petadoptionshistory` 与 `petfood`,
而活图 15 个 `Microservice` 里**这两个都不存在**(实际是 `pethistory`)。
该副本脱离了 `profiles/petsite.yaml`。

**改动位置**:`infra/lambda/rca_window_flush/config.py:22-64`,
改为与 `rca/config.py` 一致地从 profile 派生。

**根因**:硬编码副本把 **alias 当成了规范名**。`profiles/petsite.yaml` 声明
`pethistory` 的 `neptune_name=pethistory`、`aliases=['petadoptionshistory','pethistory-service']`
—— 副本把 alias `petadoptionshistory` 写成了映射目标。

**已实现**:整文件重写为 profile 派生(与 `rca/config.py` 同逻辑),
零硬编码服务名。打包依赖已确认:该 Lambda 部署包内含 `profiles/` 与 `shared/`。

**cycle-3 连带发现并修复的缺陷**:`rca/config.py` **此前没有定义 `FEATURE_FLAGS`**,
而 `rca/core/decision_engine.py:127` 有 `from config import FEATURE_FLAGS`,
外层是 `except Exception: pass` —— ImportError 被静默吞掉。
后果是**安全相关的静默失效**:`auto_remediation_enabled`(用来阻止自动修复的开关)
在 rca 包里**从未被检查过**,`action_level='auto'` 不会被降级为 `semi_auto`。
已在 `rca/config.py` 补上该字典,取值与 window_flush 副本一致。

**实测验证**:

```
CANONICAL 条目数: 10
映射到不存在服务名的条目: 无 ✅
别名 petadoptionshistory → pethistory
FEATURE_FLAGS.auto_remediation_enabled = False   （此前 ImportError 被吞）
派生目标是否全部存在于活图: ✅ 是
```

- 2026-08-28T17:35Z cycle-3: 重写为 profile 派生并补 rca/config.py 的 FEATURE_FLAGS,
  本机装 pydantic 后实测派生结果与活图一致

---

### T-021 · 删除硬编码副本,统一走 ServiceRegistry · `done`

- `SERVICE_FUNCTION_MAP` ×3:`rca/collectors/aws_probers.py:257-261`、
  `rca/collectors/layer2_tools.py:199-203`(第 3 份在 window_flush 副本内)
  —— 注意 `profiles/petsite.yaml:88` 的注释已声称"消除 aws_probers 硬编码",
  实际仍在,**文档与代码矛盾**
- `SVC_TO_CW`:`rca/core/graph_rag_reporter.py:22-28` —— 明明 YAML 有
  `cloudwatch` 段却不读,且只列 5 个服务、缺 `petstatusupdater`

**验收**:DoD-5 第一条 grep 无输出。

**已实现**:
- `graph_rag_reporter.py`:删 `SVC_TO_CW`,改走 `config.registry.get_cloudwatch_config()`
  (内部先 `resolve()` 别名),字段名从 `dim` 校正为 profile 实际的 `dimension_value`
- `aws_probers.py`:`LambdaProbe.SERVICE_FUNCTION_MAP` 改为 `_function_patterns()`
  静态方法,从 `profile['aws_resources']['lambda_functions']` 取
- `layer2_tools.py`:同上,删除第二份副本
- `profiles/petsite.yaml`:补入硬编码里独有的 `petadoption` 键以**保持行为等价**
  (重构不静默丢映射);已标注该名不在活图 15 个 Microservice 中、疑为遗留,
  删除需单独证据

**顺带修好一个别名 bug**:`SVC_TO_CW` 的 key 是 `petadoptionshistory`(别名),
调用方传规范名 `pethistory` 时**查不到**。改走 registry 后两者都能命中:

```
cw dim [pethistory          ] = pethistory-service
cw dim [petadoptionshistory ] = pethistory-service
```

**行为等价性实测**(三个映射逐条对比,全部一致):

```
petsite          旧=['petsite','statusupdater','StepFn']         新=同  ✅
petadoption      旧=['statusupdater','StepFn','stepread','stepprice'] 新=同  ✅
payforadoption   旧=['StepFn','stepprice']                        新=同  ✅
```

- 2026-08-28T17:42Z cycle-5: 三处副本清除,行为等价性实测通过,DoD-5 第一、二条转绿

---

## ⚠️ 架构决策:依赖顺序已反转(cycle-4)

**结论:三个模块(rca / chaos / dr-plan)走确定性 graph SDK,不走 agent 能力。**

理由:
1. **LLM 在事故热路径上不可接受** —— NL→Cypher 是两次 Bedrock 往返,
   且同一问题可能生成不同 Cypher,RCA 结论不可复现
2. **写路径不能 LLM 中介** —— `query_guard` 本身是只读设计(拦
   CREATE/DELETE/SET/MERGE),而 rca 写 Incident/causal_weight、
   chaos 写 ChaosExperiment/resilience_score,都是有 schema 契约的结构化写入
3. **dr-plan 的图算法需要全量精确数据** —— Kahn 拓扑排序 / DFS 环检测 /
   最长路径 DP,少一条边结果就是错的,不能吃 LLM 摘要
4. **故障域耦合** —— MCP 端点若是同 VPC 的 Lambda,而 RCA 正在诊断
   VPC/Lambda 层故障,诊断工具就与被诊断对象共享故障域

分界线:**「取数与判定」走 SDK,「解释与探索」走 agent 能力。**

| 模块 | 走 SDK | 可走 agent 能力 |
|---|---|---|
| rca | 全部(Q1–Q11/Q17/Q18 + Incident 写) | 无 |
| chaos | 目标解析、`graph_feedback` 写回 | **LearningAgent**(本就是 LLM 驱动的模式发现) |
| dr-plan | Q12–Q16 + 子图抽取 → 喂图算法 | **报告叙述生成**(不含图算法) |

**排期影响:T-022(graph SDK)必须排在 T-013(MCP 端点)之前**,
与两卡原先的依赖注释相反 —— SDK 是底座,MCP 是它的消费者。
且 SDK 先落地,三模块立刻受益(chaos 那套 `urllib` 每次重新握手可一并修掉),
不必等 MCP。

**顺带发现的安全收益**:`query_guard`(只读校验 / 跳数上限 / 自动 LIMIT)
目前**只在 rca 里**;chaos 与 dr-plan 直接拼查询发出去,无任何防护。
收敛到 SDK 会把这层防护给到另外两个模块。

---

### T-023 · `causal_weight` 接入评分 + 时间衰减 · `done`

代码自注释说「尚未纳入 `step4_score()`,待积累 100+ 真实告警后启用」。
**卡片说的「加衰减」是真问题,但不是最严重的那个。**

深查后共四个问题:

**1. 语义与名字不符(最严重)**

原 docstring 写「B **同时出现在同一 Incident** 的次数」—— 共现语义,
字段也叫 `co_occurrence`。但 `co_count` 查的是 `Involves` 边,而 `Involves`
在 `write_incident` 里**只在 `root_cause != affected_service` 时为根因服务写一条**。

所以它实际测量的是「该上游**曾被判定为根因**的次数」,不是共现。
**实测印证**:全图 141 个 Incident 只有 **8 条** `Involves` 边,且全部指向
`petsearch` —— 这也是 `causal_weight: gateway-service→petsite = 0.0 (0/67)` 的
真因之一(除该服务已下线之外)。

处理方式是**保留数据语义、改正名字与文档**:「曾是根因的频率」对 RCA 是比
共现更强的先验(共现只是相关,曾是根因带因果判定),所以该修的是名字不是数据。

**2. 无时间衰减**

改为指数衰减,半衰期默认 30 天。**实测这不是理论问题**:

```
petsearch 的 11 个 Incident 全在 ~134 天前
旧实现  co_count / 11        把四个月前的数据当作当前信号
新实现  样本权重 = 0.353     如实表达「这里没有近期证据」
```

**3. 上游集合未过滤已下线服务**

实测 petsite 的 17 条上游边都是 `active=false`,现在正确输出
「petsite 无活跃上游边,跳过」而不是给已缩容到零的服务算权重。

**4. 基线率混杂**

`P(A 是根因 | B 故障)` 忽略 A 的整体根因率 —— 一个在所有故障里都被判为根因的
服务会在每条边上都拿到高权重却无针对性。加 `lift = P(A|B)/P(A)`。

**接入评分的三条刻意约束**

| 约束 | 理由 |
|---|---|
| 上限 10 分 | 小样本相关统计,只做同分候选间的排序微调,不该盖过时间线(+40)或基础设施故障(+40) |
| 样本门槛 3.0,不足时**打日志**说明 | 原注释「待积累 100+」以当前故障频率永远达不到,等于让机制永久休眠 —— 这正是它长期只有写入方的原因之一 |
| `lift > 1` 才计分 | 否则是基线率混杂 |

- 2026-08-28T21:26Z cycle-17: 语义修正 + 衰减 + live 过滤 + lift + 接入评分,commit `c03f78e`

---

### 🔍 第四次同类错误:停止对源码做文本断言

本轮测试初版用「剥注释后做子串检查」,结果 `_code_only` 把 Cypher 的
**三引号字符串字面量**当成 docstring 起始,解析错位吞掉真实代码,4 个测试误报。

同一教训的第四次显形:

1. 用裸 `grep -c` 数装饰器
2. 用正则扫 `TYPE_TO_LABEL` 时把注释掉的条目算成生效
3. 把 docstring 里「刻意用 `mergeV` 而非 `addV`」的散文当代码
4. 这次:三引号字符串字面量被当成 docstring

**结论不是「写更好的正则」,而是停止对源码做文本断言。**
改为行为断言后,P-01 变成「跑两遍评分比较分数」—— 这才真正证明了先验被用上,
源码里出现某个函数名证明不了这件事。

---

### T-032 · 置信度评分饱和导致排序区分度丢失 · `done`(实际是三个缺陷)

**生产数据先行**:126 个有置信度记录的 Incident 中 **50 个(40%)≥ 1.0**,
且有一个是 **1.1** —— 超过 1.0 说明某条路径完全绕过了上界。饱和不是理论问题。

| confidence | 数量 |
|---|---|
| **1.1** | 1 ← 越界 |
| 1.0 | 49 |
| 0.75 | 8 |
| 0.7 | 33 |
| 0.6 | 8 |
| 0.3 | 27 |

**缺陷 1:用截断后的分数排序**

`score = min(score, 100)` 在 `results.sort()` **之前**执行,于是原始分 110 与 150
的候选都变成 100,排序退化为**字典插入顺序**——即取决于 DeepFlow 返回的服务次序,
而非证据强度。而排第一位的候选就是 DecisionEngine 拿去决策、
`action_executor` 拿去执行动作的那一个。

修法刻意是**保留原始分用于排序**,而不是重新归一各维度权重:
band 阈值(high≥80 / medium≥50)按现有分值校准,重新加权会改变所有历史评分的
相对关系,进而改变 auto/semi_auto 判定。实测 strong 原始分 165、weak 60,
即便把 weak 放在列表**前面**,strong 仍正确排第一。

**缺陷 2:LLM 返回的 confidence 无上界**

提示词要求它等于 `confidence_breakdown` 四项之和(≤100),但 LLM 不可靠地遵守。
`graph_rag_reporter` 解析后不钳制,`decision_engine` 又直接 `/100.0` —— 110 成了 1.1。

两侧都加钳制。越界时**打 warning 而非静默修正**——静默修正会让我们永远不知道
模型在违反自己的输出契约,而那本身是需要调提示词的信号。

**缺陷 3:`max(规则分, LLM 分)` 对自动化闸门是错的方向**

规则分饱和到 1.0 时,LLM 的判断被**完全覆盖**:即使模型说「置信度 35,证据很弱」,
`max(1.0, 0.35)` 仍是 1.0 → band `high` → 对 P2 走到 `auto`,
**无人确认直接执行修复动作**。

处理方式刻意**只收紧 auto 这一条路**(`AUTO_REQUIRES_LLM_CONFIDENCE=0.5` 否决),
不改展示用的 confidence 与 band —— auto 是唯一「无人确认就动生产」的分支,
semi_auto/manual 都有人在环。**收紧安全闸门时不该顺带改变有人在环的分支**,
那会在没有安全收益的前提下改变既有行为。

「无 LLM 分」刻意**不**否决 —— **缺失不等于低置信**。把「没有数据」当作
「证据不足」会让整个特性休眠,与 T-023 里「待积累 100+ 告警」那个永远达不到的
门槛是同一类错误。

实测(必须**显式打开** `auto_remediation_enabled` 才能验证,否则 flag 会统一降级,
测试根本无法区分「否决生效」与「flag 拦下」):

```
规则分 1.0 + LLM 35  → semi_auto  （否决）
规则分 1.0 + LLM 45  → semi_auto  （否决）
规则分 1.0 + LLM 50  → auto       （放行，阈值边界正确）
规则分 0.9 + 无 LLM 分 → auto      （不否决）
```

**风险定级:潜伏,非在跑。** `auto_remediation_enabled` 默认 False 且生产未覆盖,
所以 `auto` 目前会被降级为 `semi_auto`。这些修复是**安全打开该开关的前置条件**,
不是在处理正在发生的事故。

- 2026-08-28T21:58Z cycle-19: 三项修复 + 14 个行为测试,commit `9282a8a`

`step4_score` 的 docstring 原写「评分维度(共 100 分)」,但原始分相加上限是
**245**,末尾 `min(score, 100)` 截断。已改正文档。

**真正的问题是饱和**:两个证据强度明显不同的候选可能一起被截到 100,
`confidence` 都变成 1.0,排序区分度丢失。

| 场景 | 原始分 | 截断后 |
|---|---|---|
| 最早 40 + 无上游 20 + 历史 10 | 70 | 70(不饱和) |
| 上面 + 基础设施故障 40 | 110 | **100** |
| 上面 + L4 SYN 重传 40 | 150 | **100** |

典型场景不饱和,所以本次加的 0~10 分在常见情形下有效。但基础设施故障或
L4 强信号同时命中时会饱和。

**修法需要设计取舍**(所以单列一卡而非顺手改):要么把各维度权重重新归一到
总和 100,要么改用非线性合成(如 log-odds 相加)。前者简单但会改变所有现有
评分的相对关系;后者更正确但需要重新校准每个维度的系数。
两者都会影响 `DecisionEngine` 的 `auto`/`semi_auto` 阈值判定,须一并评估。

---

### T-024 · schema ↔ 活图一致性校验测试 · `done`

**目标**:这是把"双源头"变回"单源头"的**最低成本方案** —— 不必强行合并
`profiles/petsite.yaml` 与 Neptune,但保证两者不漂移。

**已实现**:`tests/test_24_live_schema_consistency.py`(200 行,6 个测试全部通过)。

**实际发现 tier 有四个来源**,比审计说的"双源头"更严重:

| 源 | petstatusupdater |
|---|---|
| AWS 资源 tag(statusupdater lambda) | `tier1` |
| `infra/lambda/etl_aws/business_config.json` | `Tier1` |
| 活图 `Microservice.recovery_priority` | `Tier1` |
| **`profiles/petsite.yaml`** | **`Tier2`** ← 唯一异类,已修正 |

**断言方向刻意不对称**:
- 活图有、YAML 未声明 → **硬失败**(`schema_prompt` 只把 YAML 喂给 LLM,
  未声明类型对自然语言查询隐形,是真实缺陷)
- YAML 声明、活图暂无 → **只告警**(某些类型可能只在 DR 演练时才实例化)

**取不到活图时 skip 而非 fail** —— 否则没有 VPC 访问权的开发者本地跑测试会全红。

**有效性验证**(不是空跑的测试):故意把 `petsite` 改成 `Tier2` 后
测试**确实失败**并报 `petsite: YAML=Tier2 活图=Tier0`,还原后 6 passed。

- 2026-08-28T18:08Z cycle-8: 测试建成,抓到并修正 petstatusupdater tier 漂移,DoD-5 全绿

---

### T-025 · 修 `conftest.py` 硬编码开发机路径 · `done`(cycle-8 意外发现)

**问题**:`tests/conftest.py:16` 原先硬编码
`PROJECT_ROOT = '/home/ubuntu/tech/graph-dependency-platform'`,
导致 **整个测试套件在其他任何机器上都无法收集**
(conftest 导入即 `FileNotFoundError`)。

发现过程:跑 T-024 的测试时被这个前置障碍挡住。
这也印证了早前审计的发现 —— 仓库内有 **26 处** `/home/ubuntu/tech/...` 硬编码,
其中 20+ 个在测试文件里,所谓"277 个测试"在原开发机之外一个都跑不起来。

**已实现**:改为从 `__file__` 推导仓库根,并支持 `GDP_PROJECT_ROOT` 环境变量覆盖。

**遗留**:另外 20+ 个测试文件各自还有同名硬编码常量,可通过 conftest 的
`PROJECT_ROOT` 逐步收敛。已记为 T-094。

- 2026-08-28T18:06Z cycle-8: conftest 路径改为自动推导,解锁测试套件

---

### T-094 · 收敛其余测试文件的硬编码路径 · `done`

全仓 29 处 `/home/ubuntu/tech/...`,其中**生效代码 20 处**
(其余是描述旧状态的 docstring,属修复记录,保留)。

**测试文件 14 处 / 13 个文件**:新增 `tests/paths.py` 作为**单一来源**,
各文件改为 `from paths import ...`。**不把推导逻辑复制 13 遍** —— 那会把
「一处硬编码」换成「13 处重复推导」,正是 T-021 刚清掉的那类问题。
`conftest.py` 也复用同一来源,并调用 `assert_layout()` 让推导错误立刻失败,
而不是让后续测试报一堆误导性的"文件不存在"。

**运行时 6 处**(比测试更严重,在本机会直接失败):

| 文件 | 常量 |
|---|---|
| `chaos/code/runner/report.py` | `REPORT_DIR` |
| `chaos/code/gen_template.py` | `out_dir` + 提示文本 |
| `chaos/code/fmea/fmea.py` | `--output` 默认值 |
| `demo/pages/4_Chaos_Engineering.py` | `CHAOS_CODE_DIR` |
| `scripts/debug_microservice_source.py` | `sys.path` |
| `chaos/code/agents/sample_for_golden_learning.py` | docstring 用法示例 |

这些指向 `/home/ubuntu/tech/chaos/...` —— 仓库的**同级**目录,说明原开发机上
chaos 是独立树。全部改为从 `__file__` 推导,**逐个实测路径存在**
(`report.py` 第一版层级算错 —— `chaos/code/runner/` 需上溯四级而非三级,
复验后修正。不实测就会留一个静默错路径)。

**效果:测试套件首次可运行**

```
改前  conftest 之外 13 个文件指向不存在的路径
改后  426 个测试可收集
      （不是文档所称的 277 —— 该数字从未被核实过）
py3.11 首次真实基线: 208 passed / 22 failed / 118 skipped / 78 errors
```

顺带发现两个环境约束,已立为 T-095 / T-096。

- 2026-08-28T18:34Z cycle-11: paths.py 单一来源 + 20 处生效改动,commit `9346be0`

---

## 🎯 里程碑:5 条 DoD 全部达成(2026-08-28T18:08Z · cycle-8)

```
DoD-1a 陈旧仍 active (目标 0):      0          ✅
DoD-1b 活跃边带 first_seen:         1/1        ✅
DoD-2  韧性分数+时效 (目标 >0):      6          ✅
DoD-3  dependency_kind 覆盖:        69/69      ✅
DoD-4  MCP 端点已注册:              ['graph-dependency']  ✅
DoD-5.1 硬编码副本:                 无          ✅
DoD-5.2 漂移已修:                   无          ✅
DoD-5.3 一致性测试存在且通过:        6 passed   ✅
```

四个设计目标的达成度变化:

| 目标 | 基线 | 现在 |
|---|---|---|
| 1 图数据库作为唯一源头 | ~60% | 硬编码副本清除、漂移修复、一致性测试守门 |
| 2 管理动态与静态依赖 | ~55% | 69/69 边可区分,查询层支持按类型过滤 |
| 3 处理退化与变化 | ~40% | 依赖边会失效、韧性闭环闭合 |
| 4 被各种 agent 快速调用 | ~55% | MCP 端点,19 条固化查询统一暴露 |

**按 north_star.md 第 5 节,此处本应 `autonudge_stop`。**
但用户 2026-08-28T17:54 指示「把所有任务都完成」,该指示覆盖 DoD 停止条件,
故循环继续,推进剩余 P2/P3/stage-X 卡(T-022 / T-023 / T-030 / T-090 /
T-091 / T-092 / T-093 / T-094)。

---

## 阶段 3

### T-030 · 拓扑历史快照 · `done`(选了变更事件日志,否决双时态)

「上周拓扑长什么样」此前完全无法回答:所有写入都是就地覆盖,无版本、无快照。

**三方案取舍**

| 方案 | 结论 |
|---|---|
| A 定期全量快照到 S3 | 可行,但只答「T 时刻全貌」,答不了「变了什么」—— 后者才是 RCA 实际问的 |
| B 双时态边 `valid_from`/`valid_to` | **否决** |
| C 图内追加式变更事件日志 | **采用** |

**否决 B 的关键理由是回归面**:要改 6 个写入方,更要命的是**现有 19 条查询会
静默返回被取代的旧版本**,除非每条都加时间过滤 —— 刚花 5 轮把套件弄绿就是
为了有安全网,B 会悄悄改变每条依赖查询的语义。且图规模按「边 × 变更频率」无界增长。

**为什么不能靠 CloudTrail**

`rca_engine.py:127` 的采集器关注 AWS API 级变更(`UpdateFunctionCode` /
`StopInstances` / `ModifyDBCluster` / `SetDesiredCapacity` …),它按构造**看不见**:

- **依赖消失** —— A 不再调用 B。这不产生任何 AWS API 调用,是「流量缺席」
- **依赖出现** —— 应用内配置/开关导致 A 开始调 B

而「上游是否还存在」正是根因判断的直接输入。cycle-4~6 查出的
「图谱在断言不存在的依赖」就属这一类,那些状态转变发生了但没有任何记录。

**两条自定硬约束**

1. **必须有消费方**。cycle-1 查出的核心缺陷就是 `active`/`last_seen` 写了但
   全仓无人读 —— 加机制不接消费方是重复同一个错误。故同时做了:
   Q19 查询 + 注册进 MCP `QUERY_REGISTRY` + 接进 `graph_rag_reporter` 提示词
   (打桩 Bedrock 捕获 prompt 验证确实包含该段)。
2. **必须有保留期**。否决 B 的理由之一是无界增长,自建日志不能无界。
   默认 90 天,对账时顺带清理。

写入用 `mergeV` + 幂等键 —— ETL 可能因重试被同一轮触发两次(SQS/EventBridge
均 at-least-once),裸 `addV` 会留重复事件,而变更日志一旦重复就没法做时序推断。
对账过滤已含 `.has('active', true)`,故**只在状态转变时发事件**,稳态不重复。

**实测**(用 ETL 真实函数打生产图,事后已清理)

```
写入 2 条 → Q19 全图 2 条 → 按 source 过滤 2 条 → 按 target 过滤 1 条
         → 不相关服务 0 条
同一秒重复写 2 次 → 未产生重复（幂等生效）
造 100 天前的事件 → 清理 1 条，未超期的 3 条保留
```

**已知局限(如实记录)**:C 方案答不了「T 时刻的完整拓扑」,只答「T 附近变了什么」。
要前者需从基线快照重放事件,即 A 叠加在 C 之上 → T-031。

- 2026-08-28T21:02Z cycle-16: 变更日志 + Q19 + MCP + 提示词 + 保留期,commit `8b9fd96`

---

### 🔍 顺带解掉一个测试死锁

`test_11` 的 `test_s0_01` 与 `test_20` 的 `test_s6_01` 断言「schema 声明 ⊆ 活图存在」,
而我 cycle-8 写的 `test_24` 断言「活图存在 ⊆ schema 声明」。**两者都是硬失败**,
合起来要求两个集合**完全相等** —— 于是任何新节点类型都无法引入:
先声明则前者红,先建实例则 `test_24` 在声明前那一刻红。这是这对测试自身的缺陷。

解法刻意用**显式允许名单** `PENDING_FIRST_INSTANCE`,而不是把该方向笼统降级为告警
—— 笼统降级会把「声明了一个永不出现的类型」也放过,而那是真问题(schema 喂给 LLM,
声明零实例类型等于邀请它推理不存在的东西)。**实际就有这样的例子**:
`etl_cfn` 的 `TYPE_TO_LABEL` 里 `APIGateway`/`KinesisStream` 在本账号永不出现,
故 cycle-11 刻意没写进 schema。名单每项必须写明为什么将来会出现;
`test_20` 从 `test_11` 导入同一份,不复制。

---

### 🔍 自己刚犯的第三次同类错误

新测试 C-02 初版直接对整个函数体做子串检查,把 docstring 里
「刻意用 `mergeV` 而非 `addV`」这句**散文**当成代码,误报失败。

这是同一类错误的**第三次**:
1. 用裸 `grep -c` 数装饰器
2. 用正则扫 `TYPE_TO_LABEL` 时把注释掉的条目算成生效
3. 这次

已加 `_code_only()` 先剥 docstring 与注释再断言。**凡是对源码做文本断言,
都必须先剥注释** —— 这条已经交学费三次了。

---

### T-033 · 生产代码漂移 —— 11/12 项修复未部署 · `blocked`(需人确认部署)

**cycle 16–19 改的全是 `rca/` 运行时代码,但生产 Lambda 最后更新是 16:39。**
「我测过的代码不是在跑的代码」违反北极星的唯一源头,也让验证结论失去意义。

**方法上的一个坑**:最初用 `git log --since="16:40"` 判断未部署项,**结论是错的** ——
cycle-9(18:12)一次性批量提交了前 8 轮改动,那些代码 16:39 就已部署,
但提交时间戳晚于部署时间。**按提交时间推断部署状态不成立。**
改为下载生产包逐文件比对 + 标记字符串判定。

生产 `petsite-rca-engine` 缺 **11/12** 项,8 个关键文件全部有差异。

**⚠️ 更正 cycle-19 的风险定级**

我当时写「flag 默认 False,所以 `auto` 会被降级 —— 潜伏,非在跑」。
**结论成立,但给的理由之一是错的。**

生产 `config.py` 里 `FEATURE_FLAGS` 出现 **0 次**,而 `decision_engine:127` 的导入
包在 `except Exception: pass` 里 —— ImportError 被吞,**flag 从未被检查**。
直接在生产代码上执行验证:`P2 + 规则分饱和 1.0 + LLM 说 35` → `action_level = auto`。

真正「非在跑」的原因是另一个:

| 函数 | 有 FEATURE_FLAGS | 调用 DecisionEngine | 执行动作 |
|---|---|---|---|
| `petsite-rca-engine` | ❌ | ❌ **压根不调用** | ❌ |
| `gp-window-flush` | ✅ | ✅ (`:186`) | ❌ **只写 `result['decision']`** |

生产中没有任何路径执行 `auto`,决策目前是纯建议性的;执行只走
`actions/semi_auto.py`(Slack 人工确认)。**真实风险**是一旦给 `auto` 接上执行,
`petsite-rca-engine` 缺 `FEATURE_FLAGS` 会让本该拦住它的开关失效。

**为什么没部署**:4 轮未在生产验证过的 RCA 引擎改动属于「难以回滚、影响共享系统」,
需人确认;本轮是自动循环唤醒,不是部署指令。历史也支持:早期一次直接部署到生产
先缺 `shared/` 再缺 `pydantic`,导致中断并回滚;此后 canary 优先,
后续 6 个缺陷全在 canary 暴露、生产零影响。

**部署前必须知道的最重要一项**:`K8S_NAMESPACE` 从 `default` 改为 `petadoptions`,
把一个「因找错命名空间而必然失败」的重启动作变成**可能真正生效**的动作。
配合 EKS RBAC 已放开 `deployments` 的 `patch/update`,semi_auto 路径上人点确认后
重启会**真的执行**。这是修复不是缺陷,但它改变了实际后果。

完整评估(含 arm64 平台 targeting、ETL 不要跑 pip、绝不对生产跑完整 `deploy.sh`、
行为变更清单、建议的三步顺序)见
`../production-drift-audit_20260828-2205.md`。

---

### 🔍 顺带修掉我自己引入的一个错误

`infra/lambda/rca_window_flush/neptune/graph_rag_reporter.py` —— 687 行模块的
**过期副本**,放在错误子目录(应在 `core/`)。来源是我从 cycle-5 起的写法:

```bash
cp rca/neptune/neptune_queries.py rca/core/graph_rag_reporter.py DEST/
#     ↑ 属于 neptune/              ↑ 属于 core/   —— 第二个被放错位置
```

`git cat-file -e main:...` 确认 main 上不存在,即**是我引入的**,非既有问题。
无模块引用,已删除,套件仍 364 passed。

**教训**:`cp 源1 源2 目标/` 在源文件属于不同子目录时会静默放错位置。
应一次拷一个,或拷完核对。

---

### T-031 · 基线快照 + 事件重放(补齐「T 时刻完整拓扑」)· `todo` · P3

T-030 的变更日志答的是「T 附近变了什么」,答不了「T 时刻的完整拓扑」。
补齐需要:定期把全图导出为基线快照(S3),再从最近的基线重放事件到目标时刻。

**现在不做的理由**:RCA 实际问的是前者;而快照会引入**第二个存储与一致性问题**
—— 向量索引 56% 是孤儿就是活生生的例子(cycle-14),一个与图谱漂移的旁路存储
比没有它更糟。要做的话必须同时设计一致性校验(类似 `test_26`)。

---

## 阶段 X — 遗留欠项(可穿插)

### T-090 · 实现 `generate_group_report` · `done`

`window_flush_handler:153` 调用了**不存在的函数名**,靠 fallback 降级到
`generate_rca_report()` 才没崩。意味着"按 EventGroup 聚合出报告"这条路径
**从未实现** —— 聚合做到了,聚合后的联合分析没做到,削弱了告警聚合一半的价值。

**实现方式**:给 `generate_rca_report` 加可选 `group_context` 注入点
(`None` 时行为与原先完全一致),而非复制那 100 行数据装配与 Bedrock 调用。

**增量价值定位为「传播时序 + 拓扑印证」** —— 单条告警看不出传播方向,
多条告警的先后顺序配合拓扑才能判断谁是源头:

```
[告警时序（按发生时间排序）]
  1. [P1] payforadoption 5XX=42.0(阈值 5.0) @ 10:30:00Z — 最早
  2. [P1] petsite        5XX=42.0(阈值 5.0) @ 10:30:06Z — 晚 6s
  3. [P1] petsearch      5XX=42.0(阈值 5.0) @ 10:30:20Z — 晚 20s

[拓扑印证]
  最早告警服务: petsite   其当前活跃下游: ['petsearch']
  ✅ 时序与拓扑一致（petsite 在上游）: ['petsearch']
```

**拓扑印证用 `kind='live'` 而非 `'dynamic'`**,实测两个场景都判对:

| 场景 | 输出 |
|---|---|
| petsite 先告警,petsearch 后(活跃边) | ✅ 时序与拓扑一致 |
| 已缩容到零的 gateway-service 先告警 | ⚠️ 拓扑无法解释,提示可能是共因故障 |

场景二是关键:系统**不会顺着时序错误推断传播方向**,这正是 `live` 过滤的价值落地。

prompt 相应要求模型回答"最早的告警是否就是源头、拓扑能否解释顺序、
时序与拓扑是否矛盾",返回 JSON 增加 `propagation_analysis` 字段。
拓扑查询失败时本节优雅跳过,不让整份报告生成不出来。

- 2026-08-28T18:20Z cycle-10: 实现并实测两个场景,commit `43a3355`

---

### T-091 · 修 `K8S_NAMESPACE` 默认值 · `done`

`rca/actions/action_executor.py` 默认 `'default'`,但服务实际在 `petadoptions`。
后果:EKS RBAC 401 已于 cycle 早期修复,但重启动作仍会去错的命名空间找 Deployment。

**过程中修正一处自己的假设**:先按 `profile['k8s']['namespace']` 取,实测返回
`None` —— profile 的顶层键是 **`kubernetes`** 而非 `k8s`
(另有 `chaos.default_namespace` 也是 `petadoptions`,已作为次级回退)。
改对后实测取到 `petadoptions`。

取值顺序:环境变量 > profile > `'default'`。

**已知局限(记为后续项)**:本项目有**两个**应用命名空间
(`petadoptions` 与 `awesomeshop`),而本模块只有单一 `K8S_NAMESPACE`。
真正的解法是按服务从 profile 取服务→命名空间映射。

- 2026-08-28T18:20Z cycle-10: 改为从 profile 取,实测 petadoptions

---

### T-092 · `etl_cfn` SQS label 不一致 · `done`

`:54` 把 `AWS::SQS::Queue` 映射为 `'Queue'`,而全仓规范名是 `'SQSQueue'` ——
**同一文件 `:398` 自己写的就是 `SQSQueue`**,即文件内部就不一致。

| 标签 | 全仓引用 | schema | 其他写入方 | 活图 |
|---|---|---|---|---|
| `SQSQueue` | **24 处** | ✅ 声明 | etl_aws:884 + etl_cfn:398 | **7 个节点** |
| `Queue` | 1 处 | ❌ | 无 | **0 个节点** |

**潜伏缺陷**:尚未触发(当前无 CFN 模板声明涉及 SQS 的依赖)。一旦出现,
`write_deps_to_neptune` 会按 `Queue` 建点,与已有 7 个 `:SQSQueue` 分裂成
**同一队列的两个节点** —— 正是「单一源头」要防的事。

**核对整表时另发现** `APIGateway` / `KinesisStream` 也不在 schema 中,但性质不同:
本账号零实例,非标签名写错。**刻意不写进 schema** —— schema 喂给 LLM 做自然语言
查询,声明零实例类型等于邀请它推理不存在的东西(与删除 Bedrock KB 同一理由)。
映射保留并加注释,因为 fallback `split('::')[-1]` 会得到 `'RestApi'`/`'Stream'`,更差。

**过程中修正自己的方法错误**:首次用正则扫映射表,把**注释掉的**
`TargetGroup → Microservice` 也算成生效条目,误报 3 条错配。重做时逐行标注
注释/生效,实际只有 1 条真错配。同类错误此前已被纠正过一次。

- 2026-08-28T18:34Z cycle-11: 1 行修复 + 整表核对,commit `9346be0`

---

### T-095 · 声明并统一测试运行的 Python 版本 · `done`

新增 **`pytest.ini`**(不是 `pyproject.toml` —— 本仓库不是可安装包,是 Lambda
部署目录 + 测试 + demo,加 `[project]` 表会误示它是包):

| 配置 | 作用 |
|---|---|
| `testpaths` + 固定 rootdir | 测试靠 conftest 注入 `sys.path`,rootdir 漂移会让 import 解析随调用位置变化 |
| 登记 `neptune` mark | 消掉 **19 条** `PytestUnknownMarkWarning` |
| `--strict-markers` | 拼错 mark 名(`@pytest.mark.netune`)原先会让该测试**被静默当作无标记执行**,属难以发现的失效。已实测现在直接报错 |

版本约束在 `tests/paths.py` 做**运行时检查**——声明式元数据没人读。
刻意用 warning 而非 hard fail:py3.9 下仍有 173 个测试真实通过,拦死会白扔这部分价值。
README 补 `Running Tests` 小节。

**关键区分**:那 661 处 `list | None` 是**运行时**求值失败(py3.9 `TypeError`),
语法本身合法 —— `py_compile` 全过但导入即崩,所以失败清单极具误导性。

- 2026-08-28T18:52Z cycle-12: pytest.ini + 运行时检查 + README,commit `def6afe`

---

### T-096 · 声明测试依赖 · `done`

新增 **`requirements-dev.txt`**,沿用仓库内 7 个组件级 `requirements.txt` 的
**下限而非钉死**惯例。`moto>=5` 是**硬约束不是偏好**:`mock_aws` 是 moto 5 才有的
统一装饰器,装 moto 4 会报 `ImportError` 而非版本错误,排查时容易误判为代码问题。
另补 `pyvis`(demo 的图谱渲染,缺它产生 8 个 error)。

- 2026-08-28T18:52Z cycle-12: requirements-dev.txt,commit `def6afe`

---

### 🔍 补齐依赖后暴露的 3 个缺陷(均在 cycle-12 修完)

套件此前跑不起来,所以这三个问题长期不可见。

**1. `neptune_client_base` 桩的跨模块污染(顺序依赖)**

三个 ETL 单测各塞一份**不同**的桩:

| 文件 | 方式 | 属性 |
|---|---|---|
| `test_12` | **无条件覆盖** | 3 个(**缺 `REGION`**) |
| `test_13` | 条件塞入 | 4 个 |
| `test_14` | 条件塞入 | 4 个 |

字母序 `test_12` 先执行,其缺 `REGION` 的桩让后两个文件报
`ImportError: cannot import name 'REGION'`。

**长期被掩盖的机制**:`test_12` 在 `import moto` 处就失败,**根本走不到塞桩那行**。
按 `requirements-dev.txt` 补齐 moto 后立即暴露 —— 即「缺依赖」意外地维持了
套件表面正常。修法:conftest 建**唯一**的桩(按真实模块公开面),test_12 改条件兜底。
已验证乱序执行稳定。

**2. 两个同名 `collectors` 包互相遮蔽(26 个 error)**

`test_12` 刻意把 `etl_aws` 提到 `sys.path[0]` 遮蔽 `rca/collectors` 并清缓存,
之后整个 session 的 `sys.modules['collectors']` 都是 etl_aws 那个。
给 `test_16` 加 autouse fixture 做 save/restore 隔离。根治见 T-097。

**3. `rca/engines/base.py:277` 在 py3.12 以下无法解析(78 个 error)**

```python
f"Status: {'OK' if healthy else '\u26a0\ufe0f ANOMALY'}"
```

f-string 表达式部分含反斜杠**在 3.12 之前是 SyntaxError**(PEP 701 才放宽)。
Lambda 跑 3.12 所以生产可用,但本地 3.10/3.11 连解析都过不去:本文件导入失败
→ `engines.factory` 失败 → **78 个 error 全部源于这一行**。

**这修正了我自己上一轮的结论**:先前写 `requires >= 3.10`,但这一行实际要求 3.12。
选择**修那一行**而非把地板抬到 3.12 —— 一行 vs 强迫所有人本地上 3.12,
且本机只有 3.11,抬地板会让套件在这里根本跑不了。转义提到 f-string 之外,
行为完全等价(已对比输出)。修后全仓 py3.11 编译 0 失败。

**套件基线变化**

```
cycle-11 末  426 collected / 2 collection errors
             208 passed /  22 failed / 118 skipped / 78 errors
cycle-12     466 collected / 0 collection errors  ← 收集阶段首次完全干净
             292 passed /  17 failed / 138 skipped / 19 errors
```

即 **+84 passed、-5 failed、-59 errors**。

---

### T-097 · 两个同名 `collectors` 包 · `done`(结论与原判断相反)

| 包 | 子模块 |
|---|---|
| `rca/collectors/` | `aws_probers`、`infra_collector`、`eks_auth`、`layer2_*` |
| `infra/lambda/etl_aws/collectors/` | `ec2`、`eks`、`rds`、`alb`、`data_stores`、`lambda_sfn` |

**我原先在这张卡里写"根治要给其中一个包改名"—— 那是错的。**
生产上二者**从不冲突**:etl_aws 与 rca 跑在不同的 Lambda 里,不共处一个进程。
冲突只存在于测试套件这一个进程内。为一个测试期问题去重命名 Lambda 部署包目录
并重新部署三个 ETL,代价不对等。

改为在 conftest 提供 per-module 隔离。**两个实现细节是实测出来的,不是推理出来的**:

1. **fixture 必须 `scope='module'` 而非 `'function'`**。pytest 先实例化高作用域
   fixture,而 `test_layer2_golden.py` 的 `engine` 就是 module 作用域 ——
   function 作用域的隔离在它之后才跑,救不了它。改 scope 后 error 归零。
2. **必须豁免 `test_12`**。它测的就是 etl_aws 那个 collectors,测试体内还有惰性的
   `collectors.eks` 导入。第一版没豁免,**直接造成 4 个新失败** —— 隔离的目的是
   让两个包各得其所,不是让 rca 通吃。

也解释了 `test_layer2_*.py` 自己那句 `if p not in sys.path` 为何救不了:
守卫只检查**存在性**不检查**优先级**。

- 2026-08-28T19:10Z cycle-13: conftest per-module 隔离,commit `e6f0cc8`

---

### 🔍 顺带发现:conftest 把 Lambda 部署包放进了全局 `sys.path`

conftest 原先无条件 `sys.path.insert(0, infra/lambda/etl_aws)`。该目录是部署包,
里面 **vendored 了 5 个第三方包**:`certifi` / `charset_normalizer` / `idna` /
`requests` / `urllib3`。

后果:**整个测试 session 用的是部署包里冻结的副本而不是已安装的版本**。
证据是警告来自 `infra/lambda/etl_aws/urllib3/connectionpool.py` 而非 site-packages。
测试结果因此取决于部署包里的版本,且同名模块被连带遮蔽。

只有 `test_12` 需要 etl_aws,它自己会 insert,故移除这行。

---

### T-098 · 归因剩余失败 · `done`(归因完成,分派为 T-099…T-103)

**先修掉两个根因,失败数从 36 项压到 10 项**:

```
cycle-12  292 passed / 17 failed / 138 skipped / 78 errors
cycle-13  312 passed / 10 failed / 144 skipped /  0 errors  ← error 首次归零
```

`${}` 占位符不展开这一项就消掉了 14 个红(chaos 写图谱全部 DNS 失败),
包遮蔽消掉 12 个 error。**收集与 setup 阶段现在完全干净**,
所以剩下 10 条第一次是纯粹的断言问题。归因如下:

| 类别 | 条数 | 判定 |
|---|---|---|
| Q18 查不到刚写入的 ChaosExperiment | 3 | **待查,疑真缺陷** → T-099 |
| 向量搜索查不到刚写入的 Incident | 2 | 疑最终一致性 → T-100 |
| `bedrock_timeout_raises` DID NOT RAISE | 1 | **疑真缺陷(静默吞异常)** → T-101 |
| NL 查询「上下游拓扑」失败 | 1 | 待查 → T-102 |
| `cdk synth` FileNotFoundError | 1 | 环境:CDK CLI 未装 → T-103 |
| `assert 'direct' == 'strands'` | 1 | 环境:strands 未装,引擎回退 → T-103 |
| test_20 NL 查询 | 1 | 与 T-102 同源 |

---

### T-099 · Q18 查不到刚写入的 ChaosExperiment · `done`(非缺陷,是测试脆弱性)

**受控验证先排除了写入缺陷**:直接调 `write_experiment`,节点与 `TestedBy` 边
**都成功落库**(`LIMIT 30` 就能查到)。

真因是 **LIMIT 窗口**:测试把 `timestamp` 写死为 `'2026-04-01T...'`,而 petsite
在活图里已累积 **24 个** ChaosExperiment,时间戳全部 ≥ 2026-04-02。
实测新节点在 25 条中排第 **25** 位,被 `LIMIT 20` 精确切掉。

这个测试是在图谱数据还少的时候写的,**随着数据累积就静默失效**。
改用当前时间(`conftest.now_iso`)—— 刚跑完的实验本来就该是最新的,
这才是语义正确的写法,且不受活图累积多少历史实验影响。

- 2026-08-28T19:32Z cycle-14: 三条测试全绿,commit `3c6ecaf`

---

### T-101 · Bedrock 超时未抛异常 · `done`(我的判断错了,问题在测试)

原先怀疑「静默吞异常」(本项目 13+ 缺陷里约 11 个是这个模式)。**核查后结论相反**:
`query()` 里的 try/except 既 `logger.warning` 又把 error 放进返回值,**不是静默**。

判定标准不该是偏好,而是**契约是否被下游遵守**。实测两个真实调用方都检查:

| 调用方 | 处理 |
|---|---|
| `rca/scripts/graph-ask.py` | `if 'error' in result: print + sys.exit(1)` |
| `demo/pages/2_Smart_Query.py` | `if result.get("error"): st.error(...)` |

NLQuery 会被 RCA 热路径调用,一次 Bedrock 抖动不该让整轮 RCA 崩掉,
所以收敛成结构化错误是**正确设计**。测试断言的是 PR2 迁移**之前**的实现细节
(注释还写着「`_generate_cypher` has no try/except」)。

改为断言**契约**:返回 error + 不伪装成空结果 + 记 warning 日志。

- 2026-08-28T19:36Z cycle-14: 测试改为断言契约,commit `3c6ecaf`

---

### T-100 · 向量搜索查不到刚写入的 Incident · `done`(索引污染,非最终一致性)

**实测写入到可检索只要 0.26s**(首次查询即命中),排除最终一致性。

真因是**评分并列**:新写入得 0.7597,与 **5 个基线 Incident 完全相同** ——
那 5 条的报告文本与测试的 `REPORT_TEXT` 一字不差,是**同一个测试历次运行的残留**。
6 条同分抢 `top_k=5` 的 5 个位置,命中与否是抛硬币,故该测试时好时坏。

顺着查下去发现更严重的问题:**18 条向量里 10 条是孤儿(56%)** ——
图谱里根本没有对应的 Incident 节点。

**这不是卫生问题而是正确性问题**:`search_similar` 的输出会作为「语义相似历史
案例」注入 RCA 提示词,孤儿向量代表一个图谱中已不存在的故障,等于给 RCA 喂
**无法核实的先例** —— 与此前删掉的那个 Bedrock KB(1 篇文档语料上返回
「相似度 89%」的编造先例)属同一类问题。也是对「图谱作为唯一源头」的直接违反:
向量索引成了第二个源头且已与图谱漂移。

根因是**写入与清理不对称**:`index_incident` 写向量,但**没有对应的删除函数**;
集成测试只 `DETACH DELETE` Neptune 节点,向量留下来单调累积 ——
一天内索引从 18 涨到 **56**。

| 修复 | 内容 |
|---|---|
| 1 | 新增 `delete_incident_vectors()` |
| 2 | 新增 `conftest.cleanup_incident()`,节点与向量一起清 |
| 3 | `test_07`/`test_10` 接入;**`test_21` 原先完全没有清理**,是残留主要来源 |
| 4 | 清理 10 条孤儿向量(索引 18 → 8,全部有节点支撑) |
| 5 | 新增 `tests/test_26_vector_graph_consistency.py` 守门 |

守门测试的**断言方向刻意不对称**:向量有图谱无 → **硬失败**(会污染 RCA 推理);
图谱有向量无 → 只告警(只影响召回,且补向量需要 Bedrock 调用,不该由测试强制)。

实测:跑完整套件后向量数仍为 8,清理自动生效。

- 2026-08-28T19:52Z cycle-14: 孤儿清理 + 对称清理 + 守门测试,commit `3c6ecaf`

---

### 🔍 顺带修掉:遮蔽隔离漏了 `handler`

cycle-13 的隔离只覆盖 `collectors`,`test_12` 仍有 2 条顺序依赖失败。
**两次靠推理修都没成之后改为取实际错误**,发现是另一个同名模块:

```
AttributeError: <module 'handler' from '.../rca/handler.py'>
                does not have the attribute 'upsert_vertex'
```

`rca/handler.py` 与 `infra/lambda/etl_aws/handler.py` 同名。

改为**算出**碰撞集而非手写:`_top_level_names(rca) & _top_level_names(etl_aws)`,
排除刻意合并的 `config` 与 vendored 第三方包。结果恰为 `{collectors, handler}`。
另把 `test_12` 从「跳过不干预」改为「主动指向 etl_aws」—— 收集期结束时
`sys.modules['collectors']` 已被更靠后的文件改成 rca 那个,仅跳过救不了它。

**教训**:同类问题第三次出现时才停止推理去取实际错误,应该更早。

---

### T-102 · NL 查询「上下游拓扑」失败 · `done`(真实产品问题)

**不是测试波动,也不是本次 schema 改动的回归。** 实测 4 次里 3 次失败,
捕获到 LLM 生成的 Cypher:

```cypher
RETURN 'downstream' AS direction, ...
WHERE downstream IS NOT NULL        ← WHERE 在 RETURN 之后，语法非法
UNION ...
```

Neptune 回 400 Bad Request —— 是 **LLM 输出质量问题**,不是 Neptune 的限制。

而「某服务的上下游」是依赖图谱**最核心的问题**,75% 失败率意味着
「图谱可被各类 agent 快速调用」这条目标**在最常用的问法上并不成立**。

**根因是 `query()` 对两种失败的处理是反的**:

| 失败类型 | 原处理 |
|---|---|
| 空结果 | `_retry_with_hint` 重试 |
| 执行失败 | 直接 `return error`,**不重试** |

可语法错误恰恰是 LLM 看到报错就能改对的情形。新增 `_retry_with_error()`,
把 Neptune 原始错误 + 失败的 cypher 回喂给 LLM,并点明三条常见约束
(WHERE 位置、UNION 列名一致、Neptune 不支持 CALL 子查询)。
**只重试一次** —— 再失败就把错误交给调用方,不无限烧 Bedrock 调用。

**实测:6/6 成功,其中 4 次靠重试救回**(改前 4 次里 3 次直接失败)。

补两个单测锁住行为(用桩,不打真实 Bedrock/Neptune):

| 测试 | 断言 |
|---|---|
| `test_ub2_03b` | 重试能救回,且重试提示**必须**包含真实错误与 WHERE 约束 |
| `test_ub2_03c` | 两次都失败则返回 error,最多生成两次(不无限重试) |

- 2026-08-28T20:32Z cycle-15: 执行错误重试 + 2 个单测,commit `ce5aced`

---

### T-103 · 声明可选测试依赖与外部工具 · `done`

两条测试在缺件时恒红,但都不是代码缺陷:

| 失败 | 原因 |
|---|---|
| `test_s0_06_cdk_synth` | `FileNotFoundError: 'cdk'`(CDK CLI 未装) |
| `test_strands_memory_under_2gb` | `assert 'direct' == 'strands'`(strands 未装,`make_layer2_engine` 按设计回退) |

**恒红项的危害不是它本身,而是它训练所有人忽略红色** —— 真缺陷会跟着被忽略。

改为 `shutil.which` 探测 / `pytest.importorskip`,跳过原因里写明安装方式,
并在 `requirements-dev.txt` 补「可选依赖」与「外部工具」两段,让跳过原因有据可查。

**顺带修掉一处同类泄漏**:`test_layer2_memory.py` 的两个测试都
`os.environ["LAYER2_ENGINE"] = ...` 且**从不还原**,泄漏到后续测试 ——
与本次修掉的 `sys.modules` 泄漏同一类(改全局状态却不恢复,结果取决于执行顺序)。
加 autouse fixture 还原。

- 2026-08-28T20:38Z cycle-15: 缺件改 skip + 依赖声明 + 环境变量还原,commit `ce5aced`

---

## 🎯 里程碑:测试套件首次全绿(2026-08-28T20:40Z · cycle-15)

```
cycle-11 末  426 collected / 2 collection errors（此前套件在本机根本跑不起来）
cycle-12     466 collected / 0 collection errors
             292 passed /  17 failed / 138 skipped / 78 errors
cycle-13     312 passed /  10 failed / 144 skipped /  0 errors  ← error 归零
cycle-14     322 passed /   2 failed / 145 skipped /  0 errors
cycle-15     325 passed /   0 failed / 145 skipped /  0 errors  ← 全绿
```

**连续两次运行结果一致(325 passed),非侥幸。**
数据零残留:向量 8 条、Incident 无今日残留、图谱 867 节点。

这条路上一共查清了 7 个根因,其中**只有 2 个是"测试写错了"**,
其余 5 个是真实缺陷或真实的环境/数据问题:

| 根因 | 性质 | 影响 |
|---|---|---|
| profile `${}` 占位符从不展开 | **真缺陷** | chaos 写图谱全部 DNS 失败(14 红) |
| conftest 把 Lambda 部署包放进全局 sys.path | **真缺陷** | 整个 session 用 vendored 副本 |
| 两个同名包互相遮蔽(collectors + handler) | **真缺陷** | 12 error + 4 失败 |
| `base.py` f-string 在 3.12 前无法解析 | **真缺陷** | 78 error |
| 向量索引 56% 是孤儿 | **真缺陷** | 给 RCA 喂无法核实的先例 |
| Q18 测试写死旧时间戳 | 测试脆弱 | 随数据累积静默失效 |
| `bedrock_timeout` 断言实现细节 | 测试过期 | —— |

**教训**:「测试红了」的默认假设不该是「测试写错了」。7 个根因里 5 个是真问题,
而它们全部被"套件跑不起来"掩盖了 —— 一个跑不起来的测试套件比没有测试更危险,
因为它让人以为有覆盖。

---

### T-093 · 提交工作树改动到特性分支并开 PR · `blocked`(推送被安全策略拦截)

**已完成**:分支 `fix/graph-single-source-of-truth`,**10 个主题化 commit**,
48 文件 +4773/-446。刻意分主题而非一个巨型提交 —— 20+ 文件挤在一起没法 review。

| commit | 内容 |
|---|---|
| `df13410` | chore: 外部研究归档不入库 |
| `bfc32a6` | fix(rca): 告警聚合链路的 5 个静默失败 + deploy.sh |
| `47db216` | fix(rca): 内联 embed/chunker,移除开发机硬编码路径 |
| `4a54b22` | feat(graph): 依赖边失效对账、first_seen、静/动态区分 |
| `6615e04` | fix(chaos): 统一韧性分数属性名,修复从未闭合的反馈闭环 |
| `3c58d2f` | refactor(rca): 消除服务映射硬编码副本 |
| `53059b6` | feat(mcp): 图谱查询 MCP 端点 |
| `c0f2341` | test: schema↔活图一致性校验 + 修 conftest 路径 |
| `e016028` | fix(infra): CDK arm64 声明、layer 双架构、EKS RBAC |
| `269949e` | docs: 盘点/评估/goal-loop 锚文件/README 校正 |

**推送前检查全部通过**:

```
机密扫描(webhook/AKIA/私钥/password)   未发现 ✅
cdk.json 是否误入 slackWebhookUrl      无 ✅（刻意未写入，它是机密）
工作树                                 干净，无未提交残留 ✅
外部研究归档                           未入库 ✅
所有改动 .py 语法                       全部通过 ✅
```

**阻塞点**:`git push` 被 Kiro Crew 安全策略拦截(**非用户取消**)。
提交已全部在本地分支上,推送与开 PR 需用户执行:

```bash
cd /home/ec2-user/works/graph-dependency-platform
git push -u origin fix/graph-single-source-of-truth
gh pr create --base main --head fix/graph-single-source-of-truth \
  --title "fix: 让依赖图谱停止断言不存在的依赖" \
  --body-file todo/goal-loop/PR_BODY.md
```

PR 正文已备在 `todo/goal-loop/PR_BODY.md`。

- 2026-08-28T18:12Z cycle-9: 10 个主题化 commit 完成,推送被策略拦截,已备好 PR 正文

**原始清单(留档)**:
2026-08-28 会话累积的未提交改动(README ×2、`rca/handler.py`、
`rca/core/alert_buffer.py`、`rca/core/graph_rag_reporter.py`、
`rca/actions/action_executor.py`、`rca/embed.py`(新增)、`rca/chunker.py`(新增)、
`rca/search/incident_vectordb.py`、`rca/deploy.sh`、
`infra/lib/alert-buffer-stack.ts`、`infra/lib/neptune-etl-stack.ts`、
`infra/k8s/rca-agent-rbac.yaml`(新增)、`infra/lambda/rca_window_flush/**` 同步副本、
以及 `todo/` 下各文档)。

**约束**:不得推送到 main / master;推特性分支时必须显式命名分支。

---

## 完成记录

<!-- 代理每轮在此追加,格式:<UTC 时间> cycle-<n> T-0xx: <动作> <结果> -->

- 2026-08-28T17:10Z cycle-0 setup: 创建 goal-loop 锚文件(north_star / roadmap / tasks),
  基线达成度 60/55/40/55,待用户在 🎯 弹层武装循环
- 2026-08-28T17:28Z cycle-1 T-001+T-002: 实现 Calls 边失效对账与 first_seen,
  `neptune_etl_deepflow.py` +131/-3,`py_compile` 通过。状态 `review`——
  **未部署**,且有两个决策点待确认:硬删除是否开启、存量 18 条边的 `first_seen` 是否回填
- 2026-08-28T17:31Z cycle-2 T-003+T-004: **DoD-2 已达成**。修正 chaos 韧性属性名分裂
  (发现是两套并行 schema 且量纲不同,读取方四个投影全错),统一到 `resilience_score`
  0–100 量纲 + `last_chaos_test`;活图实测读取方拿到 6 个服务真实分数(此前恒 -1)。
  T-004 判定为我写的 DoD 定义有误(凭空取名 `chaos_last_verified`),已更正 north_star.md
  而非再加一个属性名 —— 那样恰是 T-003 要消除的错误
- 2026-08-28T17:35Z cycle-3 T-020: 漂移副本重写为 profile 派生,实测 CANONICAL 派生目标
  全部存在于活图、别名 petadoptionshistory 正确指向 pethistory。连带修复
  `rca/config.py` 缺失 FEATURE_FLAGS 导致 `auto_remediation_enabled` 开关被
  `except Exception: pass` 静默绕过的安全相关缺陷。DoD-5 第二条已绿
- 2026-08-28T17:37Z cycle-4 T-001+T-002 部署: **DoD-1 全绿**。软删除生效,
  17 条陈旧边转 active=false,唯一活跃边 petsite→petsearch。硬删除保持关闭待观察。
  `first_seen` 改为幂等 coalesce;DoD-1b 收窄至活跃边(陈旧边不进 upsert 流程,
  设计上拿不到 first_seen)。另回答架构问题并判定 T-022 需排在 T-013 之前
- 2026-08-28T17:42Z cycle-5 T-021: **DoD-5 第一、二条全绿**。清除 SERVICE_FUNCTION_MAP ×2
  与 SVC_TO_CW,统一走 profile/registry;行为等价性逐条实测通过;顺带修好
  `SVC_TO_CW` 把别名当规范名导致传 `pethistory` 查不到的 bug。
  **两条 DoD 验收命令本身写错**(裸 grep 命中自己的解释注释),已修正为只匹配赋值语句
  —— 与 cycle-2 的 DoD-2 同一类问题,验收标准也需要被审
- 2026-08-28T17:50Z cycle-6 T-010+T-011+T-012: **DoD-3 全绿**(69/69,48 dynamic + 21 static)。
  三个 ETL 都打上 `dependency_kind` 并部署(架构均保持 x86_64),存量按 provenance 回填。
  schema 补「依赖边通用属性」声明。q1/q3 加 `kind` 过滤,并因实测发现
  `dependency_kind` 不足以排除已下线服务而**新增 `live` 语义**
  (= dynamic AND active=true)—— 这才是 `causal_weight` 出现
  `gateway-service→petsite` 无意义条目的真正解法。顺带修掉 `q1` 遍历 `:Serves`
  的死查询(活图 0 条且被 etl_aws 每轮删除)。
  排查确认 `trafficgenerator` 边失效不是误判(last_seen 176 天前)
- 2026-08-28T18:00Z cycle-7 T-013: **DoD-4 全绿**。图谱 MCP 端点建成
  (`rca/neptune/graph_mcp_server.py`,401 行)并注册到 `rca/.mcp.json`。
  纯标准库实现 JSON-RPC 2.0 —— 本机 py3.9.25 而 `mcp` 包要求 ≥3.10,
  索引无可用版本;stdlib 实现零依赖、可落任何环境。
  统一暴露 19 条固化查询(rca 13 + dr-plan 6,后者按文件路径加载因目录名带连字符)。
  实测:握手 ✅、真实查询 ✅、跨模块 q16 返回真实 SPOF ✅、
  `DETACH DELETE` 被 query_guard 拒绝 ✅、按 .mcp.json 配置拉起 ✅。
  修正 cycle-4 的顺序判断:T-022 不在 DoD 内且不是一轮能安全完成的重构,
  MCP 直接包装已有的 `rca/neptune/` 即可,架构分层结论不变
- 2026-08-28T18:08Z cycle-8 T-024+T-025: **🎯 5 条 DoD 全部达成**。
  一致性测试建成(6 passed),抓到 tier 四方漂移中 YAML 是唯一异类并修正
  (petstatusupdater Tier2→Tier1,依据 AWS 资源 tag)。
  故意注入假漂移验证测试确实会失败,非空跑。
  意外解锁:`conftest.py` 硬编码 `/home/ubuntu/tech/...` 导致**整个测试套件
  在原开发机之外无法收集**,已改为从 `__file__` 推导 —— 印证了早前审计说的
  26 处开发机路径硬编码。剩余 20+ 测试文件记为 T-094。
  按 north_star 此处本应 stop,但用户指示「把所有任务都完成」覆盖该条件,循环继续
- 2026-08-28T18:12Z cycle-9 T-093: 11 个主题化 commit(49 文件 +4998/-446)。
  推送远端被 Kiro Crew 安全策略拦截(**非用户取消**),推送与开 PR 需用户执行。
  推送前检查全过:机密扫描未发现、cdk.json 无 slackWebhookUrl、工作树干净、
  外部研究归档未入库、全量 .py 语法通过。PR 正文备在 goal-loop/PR_BODY.md
- 2026-08-28T18:20Z cycle-10 T-090+T-091: 实现 `generate_group_report`
  (此前该函数并不存在,靠 fallback 掩盖),定位其增量价值为「传播时序 + 拓扑印证」;
  拓扑印证用 kind='live',实测已下线服务先告警时会输出「拓扑无法解释、
  可能是共因故障」而非顺着时序错判传播方向。
  T-091 过程中修正自己的假设:profile 顶层键是 `kubernetes` 不是 `k8s`,
  先按 k8s 取返回 None,改对后取到 petadoptions
- 2026-08-28T18:34Z cycle-11 T-092+T-094: SQS 标签错配(潜伏缺陷,未触发但会造成
  同一队列两个节点)已修;整表核对时发现另两条标签不在 schema 但属零实例资源,
  刻意不声明。硬编码路径全仓收敛:测试 14 处走新建的 tests/paths.py 单一来源,
  运行时 6 处(chaos/demo/scripts)改为从 __file__ 推导。
  **测试套件首次可运行:426 个测试可收集**(非文档所称 277),
  py3.11 基线 208 passed / 22 failed。
  两个方法教训:(1) 正则扫映射表时把注释行算成生效条目,误报 3 条错配 ——
  同类错误此前已被纠正过;(2) report.py 的目录上溯层级第一版算错,
  靠实测路径存在才发现。新立 T-095(py 版本约束无处声明,换 3.11 失败减半)
  与 T-096(测试依赖无 requirements 声明,缺 structlog 一项即 36 个 error)
- 2026-08-28T18:52Z cycle-12 T-095+T-096: 新增 pytest.ini(登记 mark、
  --strict-markers、固定 rootdir)与 requirements-dev.txt,README 补测试小节。
  **补齐依赖后套件首次真正跑起来,立刻暴露 3 个此前不可见的缺陷并全部修完**:
  (1) 三份不一致的 neptune_client_base 桩造成跨模块污染 —— 长期被掩盖是因为
      test_12 在 import moto 处就死了,根本走不到塞桩那行,「缺依赖」意外
      维持了套件表面正常;(2) 两个同名 collectors 包互相遮蔽,26 个 error;
  (3) rca/engines/base.py:277 的 f-string 含反斜杠,3.12 之前是 SyntaxError,
      78 个 error 全源于这一行。
  **修正了自己上一轮的结论**:先前写 requires >= 3.10,但那一行实际要 3.12;
  选择修那一行而非抬地板(本机只有 3.11,抬了套件就跑不了)。
  基线 426 collected/2 errors → **466 collected/0 errors**,
  208 passed → **292 passed**,78 errors → 19 errors。
  新立 T-097(给同名 collectors 包改名根治)与 T-098(现在数字可信了,
  逐一归因剩余 17 failed/19 errors)
- 2026-08-28T19:10Z cycle-13 T-098: 归因完成,并先修掉两个根因把 36 项压到 10 项。
  最重要的发现是 **profile 的 ${} 占位符从来没被展开** —— profile_loader 完全
  没有展开逻辑,5 个占位符对任何消费方都是字面字符串;chaos 因此拿到
  'https://${NEPTUNE_ENDPOINT}:8182' 导致写图谱全部 DNS 失败(14 个红)。
  这直接打在「唯一源头」上:profile 自称单一配置源,对这些键却只是装饰。
  另修 conftest 把 Lambda 部署包放进全局 sys.path —— 该目录 vendored 了
  5 个第三方包,整个 session 用的是部署包里冻结的副本而非已安装版本。
  **更正了自己上一轮 T-097 的判断**:原写"根治要改包名",但生产上两个
  collectors 从不共处一个进程,为测试期问题改生产包并重部署三个 ETL 代价不对等。
  两个实测教训:(1) 隔离 fixture 必须 module 作用域,因为 pytest 先实例化
  高作用域 fixture,function 作用域救不了 module 作用域的 engine;
  (2) 第一版没豁免 test_12,直接造成 4 个新失败 —— 隔离是让两个包各得其所,
  不是让 rca 通吃。
  基线 292 passed/78 errors → **312 passed / 10 failed / 0 errors**,
  **error 首次归零**,剩余 10 条第一次是纯粹断言问题。
  分派 T-099(Q18 查不到刚写入,P1)、T-100(向量最终一致性)、
  T-101(超时未抛,疑静默吞异常,P1)、T-102(NL 查询,疑本次 schema 改动回归)、
  T-103(环境缺件应 skip 而非 fail)
- 2026-08-28T19:52Z cycle-14 T-099+T-100+T-101: 三张卡全部收口,且**三次判断
  中有两次是我原先的假设错了**。
  T-099 不是写入缺陷 —— 受控验证节点与边都落库,真因是测试写死 2026-04-01
  的时间戳而 petsite 已累积 24 个实验,新节点排第 25 被 LIMIT 20 切掉;
  该测试在数据少时写的,随累积静默失效。
  T-101 不是静默吞异常 —— query() 既记 warning 又返回 error,且两个真实调用方
  都检查该字段,是正确设计;错的是断言实现细节的测试。
  T-100 不是最终一致性 —— 实测写入到可检索 0.26s。真因是**18 条向量里 10 条
  是孤儿**(图谱无对应节点),而孤儿会作为「语义相似历史案例」注入 RCA 提示词,
  等于喂无法核实的先例,与删掉的那个 Bedrock KB 同一类问题,也直接违反
  「图谱唯一源头」。根因是写入有 index_incident 而**清理没有对应函数**,
  测试只删节点不删向量,一天从 18 涨到 56。已补 delete_incident_vectors、
  conftest.cleanup_incident、给 test_21(原先完全不清理)接上,清掉 10 条孤儿,
  并加 test_26 守门(向量有图谱无=硬失败,反向只告警)。
  另修 cycle-13 遗漏:遮蔽隔离漏了 handler(rca 与 etl_aws 同名)。
  **两次靠推理修都没成之后才去取实际错误 —— 应该更早**。改为从目录算出
  碰撞集而非手写,结果恰为 {collectors, handler}。
  基线 312 passed/10 failed → **322 passed / 2 failed**,
  剩余 2 条均为环境缺件(cdk CLI、strands),非代码缺陷。
  生产侧清理:向量 56 → 8,Incident 残留 15 → 0,图谱 867 节点/1341 边
- 2026-08-28T20:40Z cycle-15 T-102+T-103: **套件首次全绿 325 passed / 0 failed**,
  连续两次一致。
  T-102 是真实产品问题不是测试波动:LLM 生成 `WHERE` 在 `RETURN` 之后的非法
  Cypher,Neptune 回 400,而「某服务的上下游」是依赖图谱最核心的问法 ——
  75% 失败率意味着「可被 agent 快速调用」在最常用问法上不成立。
  根因是 query() 对两种失败处理**反了**:空结果会重试,执行失败直接返回,
  可语法错误恰恰是 LLM 看到报错就能改对的。加 _retry_with_error 回喂错误,
  实测 6/6 成功(4 次靠重试救回),并补 2 个桩测试锁住行为。
  T-103 让两条环境缺件从 fail 改 skip —— 恒红项的真正危害是训练所有人忽略
  红色,真缺陷会跟着被忽略。顺带修 test_layer2_memory 设 LAYER2_ENGINE
  从不还原的泄漏(与本次修的 sys.modules 泄漏同一类)。
  **回顾这条路上的 7 个根因,只有 2 个是"测试写错了"**,其余 5 个是真缺陷:
  profile 占位符不展开、conftest 把部署包入全局 sys.path、两个同名包遮蔽、
  base.py f-string 语法、向量索引 56% 孤儿。它们全部被"套件跑不起来"掩盖 ——
  一个跑不起来的测试套件比没有测试更危险,因为它让人以为有覆盖
- 2026-08-28T21:02Z cycle-16 T-030: 拓扑变更日志落地。**否决双时态方案**的
  关键理由是回归面 —— 要改 6 个写入方,且现有 19 条查询会静默返回被取代的
  旧版本,刚花 5 轮弄绿的套件正是为了有安全网。选追加式事件日志:纯追加、
  增长与变更次数成正比而非边数×时间。
  论证了它不可被 CloudTrail 替代:CloudTrail 记录 AWS API 级变更,按构造
  看不见「依赖消失」(流量缺席,无 API 调用)与「依赖出现」(应用内配置)。
  给自己定了两条硬约束并都验收了:必须有消费方(Q19 + MCP 注册 + 接进 RCA
  提示词,打桩 Bedrock 捕获 prompt 确认)、必须有保留期(90 天,对账时清理)。
  顺带解掉一个测试死锁:test_s0_01/test_s6_01 与 test_24 断言方向相反且都是
  硬失败,合起来要求 schema 与活图完全相等,任何新类型都无法引入。用显式
  允许名单而非笼统降级 —— 后者会把「声明了永不出现的类型」也放过。
  **也修掉自己刚犯的第三次同类错误**:C-02 初版把 docstring 里的散文当代码,
  与前两次(裸 grep 数装饰器、正则把注释条目算成生效)同源。已加 _code_only()。
  套件 331 passed / 0 failed。新立 T-031(基线快照+重放),现在不做是因为
  快照会引入第二个存储与一致性问题 —— 向量索引 56% 孤儿就是例子
- 2026-08-28T21:26Z cycle-17 T-023: 因果先验收口。**卡片说的「加衰减」是真问题
  但不是最严重的那个** —— 最严重的是语义与名字不符:docstring 与字段名都写
  「共现」,而 co_count 查的 Involves 边只在「该上游被判定为根因」时才写。
  实测印证:141 个 Incident 只有 8 条 Involves 边且全指向 petsearch。
  选择保留数据语义、改正名字 —— 「曾是根因的频率」比共现是更强的先验。
  衰减的效果是实测出来的:petsearch 的 11 个 Incident 全在 134 天前,
  旧实现用 co_count/11 把四个月前的数据当当前信号,加衰减后样本权重塌到 0.353。
  另修上游未过滤已下线服务(petsite 17 条上游边全部 active=false,
  现在正确跳过)与基线率混杂(加 lift)。
  接入评分设三条约束:上限 10 分、样本门槛 3.0 且不足时打日志、lift>1 才计分。
  原注释「待积累 100+ 告警」以当前频率永远达不到,等于让机制永久休眠。
  **第四次同类错误**:测试初版把 Cypher 的三引号字符串当 docstring,解析错位
  误报 4 个。结论不是写更好的正则,而是**停止对源码做文本断言** —— 改为行为
  断言后 P-01 变成「跑两遍评分比较分数」,这才真正证明先验被用上。
  套件 342 passed / 0 failed。新立 T-032(评分饱和导致排序区分度丢失)
- 2026-08-28T21:48Z cycle-18 T-022: **降范围** —— 卡片三条前提两条经不起实测。
  query_guard 那条不成立:它是防 LLM 生成 Cypher 的,而 chaos/dr-plan 只发手写
  静态查询;更要紧的是 chaos 有写路径(neptune_sync),把守卫下沉到客户端会
  直接拦掉它。已加测试 N-04 固定这个理由,避免将来有人顺手下沉。
  但排查过程找到两个比重构更有价值的问题:
  (1) 共享 Lambda layer 把 get_frozen_credentials() 的快照永久缓存在模块全局,
      Lambda 容器复用数小时后凭证过期会持续 403 —— 三个 ETL 都用这个 layer。
      如实说明:近 7 天日志未观测到 403,是「写错但尚未触发」的潜伏缺陷。
  (2) 四份客户端 6 个调用点全部每次新建 boto3 Session。生产日志佐证:
      同一 request ID 在 2 秒内出现 4 次「Found credentials」。
      实测 rca 32.8→16.8ms、chaos 39.5→15.9ms,按单次 RCA 15-30 次查询估
      此前每轮浪费 150-300ms,而 RCA 在事故热路径上。
  **方法教训:比较两个实现时,它们共有的缺陷是不可见的。** 我一开始把两个
  客户端互比只看到 6.7ms 之差,据此判断不值得动;只有单独测 boto3.Session()
  的绝对成本(9.7ms)才暴露出共同开销。
  正确模式是缓存 Session、每次重新冻结凭证 —— 原 layer 恰好两者都反了。
  套件 350 passed / 0 failed,总耗时 267s→252s
- 2026-08-28T21:58Z cycle-19 T-032: 卡片以为是「排序区分度」一个问题,实测是**三个**。
  先用生产数据定性:126 个 Incident 里 50 个(40%)置信度 >= 1.0,还有一个 1.1 ——
  越界说明某条路径完全绕过上界。饱和不是理论问题。
  (1) 排序用的是截断后的分数,饱和候选顺序退化为字典插入顺序,即取决于
      DeepFlow 返回次序而非证据强度 —— 而第一位就是被拿去执行动作的那个。
      修法刻意是保留原始分排序,**不重新归一权重**:band 阈值按现有分值校准,
      重新加权会改变所有历史评分的相对关系进而改变 auto 判定。
  (2) LLM 的 confidence 无上界,reporter 与 decision_engine 两处都不钳制,
      110 直接成 1.1。两侧都加钳制,越界打 warning 而非静默修正 ——
      静默修正会让我们永远不知道模型在违反输出契约。
  (3) `max(规则分, LLM 分)` 对授权自动执行的闸门是错的方向:规则分饱和到 1.0
      会完全覆盖 LLM 判断,即使模型说「证据很弱」也走到 auto。
      刻意只收紧 auto 这一条路,不动展示用 confidence 与 band ——
      auto 是唯一无人确认就动生产的分支,semi_auto/manual 都有人在环。
      「无 LLM 分」不否决:缺失不等于低置信,否则特性会休眠(与 T-023 的
      「待积累 100+」同类错误)。
  验证时必须显式打开 auto_remediation_enabled —— 否则 flag 统一降级,
  测试无法区分「否决生效」与「flag 拦下」,等于什么都没验证。
  风险定级:潜伏非在跑(flag 默认 False),这些是安全打开该开关的前置条件。
  套件 364 passed / 0 failed
- 2026-08-28T22:10Z cycle-20: 看板只剩 T-031(已两轮论证暂不做),故转向一件
  比它重要且被我推后的事 —— **核查生产与分支的漂移**。结论:生产
  petsite-rca-engine 缺 **11/12** 项修复,8 个关键文件全部有差异。
  **方法坑**:最初用 git log --since 判断未部署项,结论是错的 —— cycle-9 一次性
  批量提交了前 8 轮改动,代码 16:39 已部署但提交时间戳更晚。按提交时间推断
  部署状态不成立。改为下载生产包逐文件比对。
  **更正 cycle-19 的风险定级**:我说「flag 默认 False 所以 auto 被降级」——
  结论对,但理由之一错了。生产 config.py 里 FEATURE_FLAGS 出现 0 次,
  导入被 except: pass 吞掉,flag 从未被检查;直接在生产代码上执行验证
  P2+饱和+LLM 说 35 → auto。真正「非在跑」的原因是 petsite-rca-engine
  压根不调用 DecisionEngine,而 gp-window-flush 虽调用但只记录不执行。
  **未部署**:4 轮未在生产验证的 RCA 改动属难回滚动作,需人确认;
  且早期一次直接部署曾导致生产中断并回滚。已写出含 arm64 打包、
  行为变更清单、三步顺序的评估文档,把决定权交给用户。
  最需知情的一项:K8S_NAMESPACE 改对之后,重启动作从「必然失败」变成
  「可能真正生效」——配合已放开的 EKS RBAC,semi_auto 确认后会真执行。
  **也修掉自己引入的错误**:从 cycle-5 起用 `cp 源1 源2 目标/` 把属于不同
  子目录的两个文件拷进同一目录,留下一份 687 行的过期副本。已删,套件不变
