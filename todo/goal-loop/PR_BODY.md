# fix: 让依赖图谱停止断言不存在的依赖

## 一句话

图谱此前**只会长,不会忘** —— 依赖只增不减、退化指标只写不读。
本 PR 修掉这两点,并补上让外部 agent 接入图谱的契约。

## 为什么现在必须改:图谱正在输出错误结论

实测(2026-08-28,活图 + kubectl 交叉验证):

| Calls 边 | `last_seen` | 陈旧 | `active` |
|---|---|---|---|
| petsite → petsearch | 2026-08-28 | 新鲜 | true |
| list-adoptions → petsearch | 2026-06-30 | 59 天 | **true** |
| petsite → payforadoption | 2026-04-21 | 129 天 | **true** |
| gateway-service → auth-service | 2026-03-19 | 162 天 | **true** |

18 条依赖边里 **17 条陈旧 2–5 个月,却全部 `active=true`**。

而 `gateway-service → auth-service` 所属的 `awesomeshop` 命名空间,
**6 个 Deployment 副本数已全为 0**(服务整体下线),
图谱仍声称该依赖活跃并带 376 次调用量。

后果已出现在生产 RCA 日志里:

```
causal_weight: gateway-service→petsite = 0.0 (0/67)
causal_weight: order-service→petsite  = 0.0 (0/67)
```

**RCA 正在把缩容到零的服务当作 petsite 的上游做根因推理。**

`active` / `last_seen` 两个字段早就写入了,但全仓库**没有任何消费方** ——
这是为软删除准备好却从未接线的机制。

## 改了什么

### 变化(change):依赖边现在会失效

`etl_deepflow` 每轮 upsert 后做对账:超阈值未被观测 → `active=false`。
硬删除做成可选且**默认关闭**(`CALLS_EDGE_DROP_ENABLED`)——
软删除已足以让图谱停止说假话,且可逆、保留历史;删除是不可逆的生产数据变更。

失效阈值默认 1800s(≈6 个 ETL 轮次)而非单轮,避免单轮 L7 漏采误判;
**误判自愈** —— 边重新被观测到时 `active` 写回 `true`。

`first_seen` 用幂等 coalesce 写入。**刻意不用 `last_seen` 回填存量边**:
`first_seen ≤ last_seen` 恒成立,把 `last_seen` 当 `first_seen` 等于断言
依赖"那时才出现",比留空更具误导性。

### 退化(degradation):韧性反馈闭环终于闭合

`query_learning_nodes` 的**四个投影全部读错**,返回的全是兜底值:

```
读取方查 chaos_resilience_score  → 活图 0 个节点拥有   恒 -1
读取方查 last_tested_at          → 活图 0 个节点拥有   恒 'never'
读取方查 test_coverage           → 活图 0 个节点拥有   恒 ''
读取方查 weakness_pattern        → 活图 0 个节点拥有   恒 ''
真正落数据的是 resilience_score / last_chaos_test（各 6 个节点）
```

LearningAgent 一直在拿空数据决策。这不是一个笔误,是**两套并行 schema**,
且有量纲陷阱:一方写 0–100 整数,另一方写 `pass_rate/100` 即 0–1 浮点 ——
**不能简单 coalesce**,消费方会时而拿到 90、时而拿到 0.9。已统一并对回退分支归一。

### 区分:静态与动态依赖不再混装

三个 ETL 共写 `DependsOn` / `AccessesData`,provenance 字段名还不统一
(`etl_aws` 用 `source`、`etl_cfn` 用 `declared_in`、`Calls` 干脆没有——
实测 18 条 `Calls` 边带 `source` 的 **0 条**)。查询层 `q1`/`q3` 用
`[:Calls|DependsOn]` **主动抹平**,导致"每秒数百次的真实调用"与
"模板里声明但从未调用的依赖"在影响面分析里权重相同。

现统一补 `source` 与 `dependency_kind ∈ {static, dynamic}`,
分界线取**「声明的」vs「观测到的」**。69/69 依赖边全部带标记。

**并新增第三个取值 `live`** —— 单靠 `dependency_kind` 不够,它只区分
「声明 vs 观测」,不区分「观测过 vs 现在还在」:

| `kind` | petsite 上游实测 |
|---|---|
| 不过滤 | 3 条(含 2 个已下线服务) |
| `'dynamic'` | **3 条,仍含已下线服务** |
| `'live'` | **0 条** ✅ |

根因定位应该用 `live`。

### 接入:图谱有了对外契约

`rca/.mcp.json` 此前是空的 `{"mcpServers": {}}`,仓库里唯一含 MCP 的文件
`chaos_mcp.py` 做的是故障注入;无 REST / GraphQL / Function URL。
图谱只能在 VPC 内、用 Python、把 `rca/` 挂进 `sys.path` 才访问得到。

新增 `rca/neptune/graph_mcp_server.py`,**纯标准库**实现 JSON-RPC 2.0
(本机 py3.9.25 而 `mcp` 包要求 ≥3.10,索引无可用版本;stdlib 实现零依赖、
可落任何环境、无供应链面)。

顺带**统一了割裂的查询库**:Q1–Q11/Q17/Q18 在 rca、Q12–Q16 在 dr-plan,
现从一个入口暴露 19 条。写操作一律拒绝而非降级执行。

**架构定位**:三个模块(rca / chaos / dr-plan)**自己不走这个端点**,
它们直接调用确定性查询层。理由:LLM 在事故热路径上带来延迟与不确定性;
写路径有 schema 契约不能 LLM 中介;dr-plan 的图算法需要全量精确数据;
MCP 端点若与被诊断对象同处一个 VPC 会共享故障域。
分界线是「取数与判定」走确定性层,「解释与探索」走 MCP。

### 唯一源头:消除副本与漂移

`SERVICE_FUNCTION_MAP` 有 3 份副本、`SVC_TO_CW` 1 份,而
`profiles/petsite.yaml` 的注释已声称"消除 aws_probers 硬编码" ——
**文档与代码矛盾**。两处都不只是重复,还各带一个**把 alias 当规范名**的缺陷:

- `SVC_TO_CW` 的 key 是 `petadoptionshistory`(alias),
  调用方传规范名 `pethistory` 时**查不到**
- `rca_window_flush/config.py` 把 `pethistory-deployment` 映射到
  `petadoptionshistory`,但活图 15 个 `Microservice` 里**不存在该名**;
  另含活图同样不存在的 `petfood`

新增 `tests/test_24_live_schema_consistency.py` 守门。首次运行即抓到
`petstatusupdater` 的 tier 漂移 —— 四方核对后确认 **YAML 是唯一异类**:

| 源 | 值 |
|---|---|
| AWS 资源 tag | `tier1` |
| `business_config.json` | `Tier1` |
| 活图 `recovery_priority` | `Tier1` |
| **`profiles/petsite.yaml`** | **`Tier2`** ← 已修 |

即 tier 实际有**四个来源**。测试断言方向**刻意不对称**:活图有而 YAML
未声明 → 硬失败(`schema_prompt` 只把 YAML 喂给 LLM,未声明类型对自然语言
查询隐形);YAML 声明而活图暂无 → 只告警。取不到活图时 skip 而非 fail。

### 顺带解锁了整个测试套件

`tests/conftest.py` 硬编码
`PROJECT_ROOT = '/home/ubuntu/tech/graph-dependency-platform'`,
导致**整个测试套件在其他任何机器上都无法收集**(导入即 `FileNotFoundError`)。
仓库内共 **26 处**这类开发机路径硬编码,20+ 个在测试文件里 ——
所谓"277 个测试"在原开发机之外一个都跑不起来。已改为从 `__file__` 推导。

## 验证

所有结论以**活图 openCypher + kubectl + AWS API 交叉验证**,非代码推断。

```
依赖边失效对账      陈旧仍 active: 0（原 17）
first_seen         活跃边 1/1
韧性分数可读        6 个服务拿到真实分数（原恒为 -1）
dependency_kind    69/69（48 dynamic + 21 static）
MCP 端点            握手 ✅ 真实查询 ✅ 跨模块 q16 ✅ DETACH DELETE 被拒 ✅
一致性测试          6 passed；注入假漂移后确实失败（非空跑）
```

全链路已在生产重复验证 4 轮,每轮 `processed=1 failed=0`,
`DecisionEngine → semi_auto` 这一环此前从未被执行过。
告警接收路径从 34 秒降到 0.36 秒。

## 已知未做(不阻塞本 PR)

- `T-022` 收敛 6 套 Neptune 客户端为 graph SDK。顺带会把 `query_guard`
  (只读校验/跳数上限/自动 LIMIT)给到 chaos 与 dr-plan —— **目前只有 rca 有
  这层防护**,另两个模块是裸拼查询发出去的
- `T-023` `causal_weight` 接入评分。代码自注释"待积累 100+ 真实告警后启用";
  接入前须先把全历史单调累积改为滑窗或指数衰减
- `T-030` 拓扑历史快照。"上周拓扑长什么样"目前完全无法回答
- `T-090` `window_flush_handler` 调用不存在的 `generate_group_report`,
  靠 fallback 降级 —— 按 EventGroup 聚合出报告这条路径从未实现
- `T-091` `K8S_NAMESPACE` 默认 `default`,而服务实际在 `petadoptions`
- `T-094` 其余 20+ 测试文件的硬编码路径

完整看板见 `todo/goal-loop/tasks.md`,评估依据见
`todo/design-goals-assessment_20260828-1705.md`。

## 生产侧已应用(不在本 PR 的代码里)

本次同时对生产做了若干配置变更,记录在
`todo/tokyo-env-changes-applied_20260828-1630.md`:
8 条 IAM 内联策略(增量添加,未改写既有策略,便于单独回滚)、
2 个 Lambda 迁 arm64、5 项环境变量修正、
删除一个处于 `DELETE_UNSUCCESSFUL` 的孤儿 Bedrock KB 及其 pgvector 表
(理由与影响面核查见 `todo/bedrock-kb-pgvector-removal_20260828-1635.md`)。
