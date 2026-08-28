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

### T-022 · 收敛 Neptune 访问层为 graph SDK · `todo`(P2,不阻塞 DoD)

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

### T-022 · 收敛 Neptune 访问层为 graph SDK · `todo`

全仓 **6 份** `neptune_client*.py`。`dr-plan-generator/graph/neptune_client.py`
注释直接写着 "Mirrors the pattern in rca/neptune/neptune_client.py"。
本该消除重复的 `infra/lambda/shared/python/neptune_client_base.py`
**只支持 Gremlin、只服务 ETL 写入**,查询侧三模块谁都没用它。

顺带:`chaos/code/runner/neptune_client.py` 用 `urllib.request.urlopen`,
**每次调用重新握手**(其余几套都已用 `requests.Session` 复用)。

**依赖**:建议在 T-013 之后 —— 让 MCP 端点与三模块共用同一层。

---

### T-023 · `causal_weight` 接入评分 + 时间衰减 · `todo`

代码自注释(`rca/actions/incident_writer.py:250-253`)明说
「尚未纳入 `step4_score()` 评分,待积累 100+ 真实告警后启用」。
告警链路已于 2026-08-28 修通,开始积累了。

**接入前必须先改**:其 `total` 取该服务**全历史** Incident 计数(`:270-274`),
是单调累积比率,**旧共现与新共现同权、永不衰减** —— 需改滑窗或指数衰减。

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

### T-094 · 收敛其余测试文件的硬编码路径 · `todo`

仓库内约 20 个测试文件各自硬编码
`PROJECT_ROOT = '/home/ubuntu/tech/graph-dependency-platform'`
(`test_00` / `test_02` / `test_03` / `test_11` / `test_12` / `test_13` / `test_14` /
`test_17` / `test_18` / `test_19` / `test_20` / `test_21` / `test_22` / `test_23` 等),
以及 `scripts/debug_microservice_source.py`、`chaos/code/runner/report.py`、
`chaos/code/fmea/fmea.py`、`chaos/code/gen_template.py`、`demo/pages/4_Chaos_Engineering.py`。

conftest.py 已改为自动推导(T-025),这些文件应改为从 conftest 或
从自身 `__file__` 推导,不再各自写死。

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

### T-030 · 拓扑历史快照 · `todo`

"上周拓扑长什么样"目前完全无法回答 —— 所有写入均为就地覆盖
(`property(single,...)` / `mergeV` onMatch),无版本标签、时间分区、快照导出。
四个目标里唯一需要**新增机制**而非修补的一项。

方案二选一:定期快照导出(新增一个 Lambda),或给写入加有效期区间(bi-temporal)。
**建议先在 tasks.md 追加一张调研卡比较两者**,不要直接开工。

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

### T-092 · `etl_cfn` SQS label 不一致 · `todo`

`infra/lambda/etl_cfn/neptune_etl_cfn.py:54` 把 `AWS::SQS::Queue` 映射为 `'Queue'`,
而 `:389` 写入时用的是 `'SQSQueue'`。

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
