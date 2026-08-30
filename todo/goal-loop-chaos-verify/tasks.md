# Tasks —— 看板

**每轮必须更新这个文件。** 状态：`todo` / `doing` / `review` / `done` / `blocked` / `wontfix`。
卡号 `T-2xx` 段，避开 `todo/goal-loop/tasks.md` 已用的 T-001..T-096。

路径均相对 `/home/ec2-user/works/graph-dependency-platform/`。

---

## 进度总览

| Stage | 卡数 | done | doing | blocked | todo |
|---|--:|--:|--:|--:|--:|
| 0 卫生与基线 | 4 | 0 | 0 | 0 | 4 |
| **0.5 把 Strands 跑起来** ★ | 4 | 2 | 1 | 0 | 1 |
| **0.6 接入 AgentCore** | 2 | 0 | 1(review) | 0 | 1 |
| 1 边验证接入 runner ★ | 6 | 2 | 0 | 0 | 4 |
| 2 判定分辨力 | 4 | 0 | 0 | 0 | 4 |
| 3 AWS 侧单边隔离 + 选边 | 4 | 0 | 0 | 0 | 4 |
| 4 LLM 约束与评测 | 5 | 0 | 0 | 0 | 5 |
| 5 定期演练 | 4 | 0 | 0 | 0 | 4 |
| 6 契约剩余项 | 4 | 0 | 0 | 0 | 4 |
| 7 部署与清理 | 3 | 0 | 0 | 3 | 0 |
| **8 闭环校正** ★ 终点 | 4 | 0 | 0 | 0 | 4 |
| （T-202 方向写反，作废） | 1 | — | — | — | wontfix |
| **合计** | **45** | **4** | **2** | **3** | **36** |

基线：提交 `3477896` 时 **442 passed / 145 skipped / 0 failed**。
**当前水位（cycle-2 后）：443 passed / 144 skipped / 0 failed**。
活图谱基线：1731 边、94 条依赖边、`verify_status=untested` 94 条（已验证占比 **0.0**）。
`verify_dod.sh --local-only`：cycle-0 PASS=3 → cycle-1 PASS=3/FAIL=9 →
**cycle-2 后 PASS=7 FAIL=5 SKIP=5**。**DoD-9 四项检查全绿。**

---

## Stage 0 —— 卫生与基线

### T-200 删除异常文件 `chaos/=23.0.0` · `todo`
- 位置：`chaos/=23.0.0`（1024 字节，疑 `pip install structlog=23.0.0` 误重定向）
- 做法：确认内容确为 pip 输出后 `git rm`
- 验收：`ls chaos/=23.0.0` 不存在；测试数不变
- 依赖：无

### T-201 处理过期冻结注释 · `todo`
- 位置：`chaos/code/agents/hypothesis_direct.py` 顶部 `delete_date: 2026-08-18`（已过期 12 天）
- 做法：冻结期已过 —— 判定该注释描述的待删代码是否真能删；不能删就更新日期并写明理由
- 验收：文件内无已过期的 `delete_date`
- 依赖：无

### T-202 ~~Strands 死代码定性~~ → **作废，方向写反** · `wontfix`
- 初版写的是「删除，或保留但标注未启用」。按 north_star §1.5（用户明确的硬约束：
  LLM 开发一律走 Strands + 尽量用 AgentCore），**这个方向是错的**。
- 实测事实推翻了「死代码」这个判断：**19 个文件 / 4583 行** strands 实现、
  **6 个**引擎开关、**13 处**回退分支，目标版本 `rca/engines/factory.py:36` 写明
  `strands-agents>=1.36`（不是 0.x —— 我一度也说错了这一点）。这是一等架构，不是残渣。
- 替代卡：**T-205 / T-206 / T-207**（Stage 0.5）与 **T-208**（Stage 0.6）。

---

## Stage 0.5 —— 把 Strands 跑起来（所有 LLM 工作的硬前置）★

### T-205 安装 strands 依赖并解注释 · `done`（cycle-1）
- 已装 **strands-agents 1.54.0** + **strands-agents-tools 0.8.7**（`python3.11 -m pip install --user`，
  沿用 boto3 所在的 user site；本机无 venv、`sudo` 被 `no_new_privs` 禁用）
- `requirements-dev.txt` 从「可选依赖」段**上移为必装**，下界 `>=1.36` / `>=0.5`
  —— 原先写 `>=0.1` 与 `rca/engines/factory.py:36` 的实际要求不一致
- 五个 import 路径全部实测可用：`Agent`、`tool`、`strands.models.{BedrockModel,CacheConfig}`、
  `strands.telemetry.StrandsTelemetry`、`SlidingWindowConversationManager`
- 最小端到端调用 **1.2s** 成功（`global.anthropic.claude-sonnet-4-6` @ ap-northeast-1）
- 全量测试 **443 passed / 144 skipped / 0 failed**（基线 442/145/0）——
  装上没破任何东西，净解锁 1 个测试
- ⚠️ **抓到一个 1.54 的 API 变更**：`cache_prompt is deprecated. Use SystemContentBlock with
  cachePoint instead.` 影响 `rca/engines/strands_common.py` 的 `build_bedrock_model`。
  目前只是 DeprecationWarning、功能正常 → 新卡 **T-209**

### T-206 六个引擎开关默认切到 strands · `done`（cycle-1）
- **真实 env 名与初版记录不同**（已同步修正 north_star）：
  `CHAOS_RUNNER_ENGINE`（不是 `RUNNER_ENGINE`）、`POLICY_GUARD_ENGINE`（不是 `GUARD_ENGINE`）
- 六处默认值 `direct` → `strands`：
  `chaos/code/runner/factory.py:17`、`chaos/code/policy/factory.py:14`、
  `rca/engines/factory.py:28/60/90/119`
- **逐个实测（清空所有 `*_ENGINE` env 后调工厂）→ 6/6 返回 strands 实现**：
  ```
  NLQUERY_ENGINE       -> StrandsNLQueryEngine
  HYPOTHESIS_ENGINE    -> StrandsHypothesisAgent
  LEARNING_ENGINE      -> StrandsLearningAgent
  LAYER2_ENGINE        -> StrandsLayer2Prober
  CHAOS_RUNNER_ENGINE  -> StrandsRunner
  POLICY_GUARD_ENGINE  -> StrandsPolicyGuard
  ```
- 切换后全量 **443 passed / 0 failed**，零回归（没有测试假定 direct 是默认）
- 顺带修掉三处过期 docstring（`rca/engines/factory.py:4/57/87` 还写着「默认 direct」）

### T-207 回退 direct 显式告警 + 能力对账测试 · `doing`（前半已满足）
- ✅ **告警已存在**，六处回退全部 `logger.warning`（`rca/engines/factory.py:34/40/66/70/...`、
  `chaos/code/runner/factory.py:31/33`、`chaos/code/policy/factory.py:20/22`）——
  这一半不需要改，初版假设它缺失是错的
- ⬜ 仍待做：**能力对账测试** —— direct 与 strands 的能力集比对，strands 缺失即失败。
  这是防止约束随时间腐蚀的唯一机制
- 依赖：T-206 ✅

### T-209 strands 1.54 的 cache_prompt 弃用 · `todo`（cycle-1 新增）
- 现象：`UserWarning: cache_prompt is deprecated. Use SystemContentBlock with cachePoint instead.`
- 位置：`rca/engines/strands_common.py` 的 `build_bedrock_model`（传了 `cache_prompt`）
- 影响：目前仅告警、功能正常；但 prompt 缓存是成本项，弃用参数可能已不生效 ——
  需实测缓存命中率确认是否真的失效（`test_hypothesis_shadow` 会打印 cache read/write）
- 依赖：无

---

## Stage 0.6 —— 接入 AgentCore

### T-208 接入 AgentCore —— 选定 **Observability** · `review`（cycle-2）
**取舍已定，理由基于实测而非我上轮的排序。** 上轮我把 Gateway 排第一，但查清前置后改选 Observability。

**为什么不是 Gateway（改判，非放弃 → 见 T-210b）**
- ✅ 好消息：`authorizerType` 枚举实测含 **`AWS_IAM`** 与 `NONE`，不只 `CUSTOM_JWT`
  —— **不需要先立 Cognito**，这推翻了我原以为的大前置（那份 inbound-auth 文档只讲 JWT，
  CFN 的 `AuthorizerConfiguration` 还写着 CustomJWTAuthorizer 必填，但 API 层面 IAM 就够）
- ❌ 真正的阻塞：`targetConfiguration.mcp` 支持 `lambda`/`openApiSchema`/`smithyModel`/
  `mcpServer`/`apiGateway`/`connector`，最自然的是 `lambda` + `toolSchema`，
  但**账号里没有任何图查询 Lambda**（现存 7 个全是 ETL/RCA），`.mcp.json` 也是空的。
  要先建一个在 VPC 内、挂 `neptune-client-base` 层、能连 Neptune 的新函数 → 独立卡 T-210b

**为什么是 Observability（配置级，零新建计费资源）**
- `rca/engines/strands_common.py` 已有完整 telemetry 骨架：`ensure_telemetry()`
  + `STRANDS_TELEMETRY=off|console|otlp` + X-Ray ID generator + OTLP exporter
- **console 模式实测出真 span**，且是 GenAI 语义约定：
  `invoke_agent Strands Agents` / `chat` / `execute_event_loop_cycle` /
  `gen_ai.choice` / `gen_ai.user.message` / `gen_ai.system.message`
  —— 正是 AgentCore Observability 消费的格式
- 直接服务 **DoD-6**：T-243 的幻觉率统计需要 agent 决策轨迹，否则要自己埋点

**本轮已做**
- 装 `bedrock-agentcore 1.22.0` + `aws-opentelemetry-distro 0.19.0`
  （≥0.18.0 才支持 `OTEL_EXPORTER_OTLP_TRACES_HEADERS`）
- 建日志组 `/aws/bedrock-agentcore/runtimes/graph-dep-chaos-agent` + 流 `runtime-logs`，14 天保留
- 写 `chaos/scripts/enable_agentcore_observability.sh`（7 个 OTel env 变量 + `--check` + 闸门隔离），
  `--check` 实跑正确识别出闸门未开

**未做（唯一剩项）**：Transaction Search 未启用 → 卡 **T-208b**。
面板要看到 span 必须开它，但那是账号级 + 计费 + 可能影响 etl_xray 的变更。

### T-208b 验证并启用 CloudWatch Transaction Search · `todo`（cycle-2 新增，含闸门）
- 实测现状：`aws xray get-trace-segment-destination` → `{"Destination":"XRay","Status":"ACTIVE"}`，
  而 AgentCore Observability 要求 `CloudWatchLogs`；indexing rule 采样率 **0.0**
- **翻开关前必须先验证的事**：`infra/lambda/etl_xray/` 读 X-Ray trace 建拓扑边，
  切换 segment destination 是否影响 `GetTraceSummaries` / `BatchGetTraces` **尚未验证**。
  没验证就翻，可能**静默打断本项目自己的一条 ETL 数据源** —— 这正是不变量 7 那类
  「坏掉和正常长得一样」的错误
- 另两个理由使它成为闸门：账号级（影响该 region 全部 X-Ray 消费方）、
  按摄入 span 量计费（该账号已有 EKS audit 约 $45/月）
- 做法：先起一份对照实验证明 X-Ray 读取不受影响 → 再
  `./enable_agentcore_observability.sh --enable-transaction-search`（采样从 10% 起步）
- 回退：`aws xray update-trace-segment-destination --destination XRay`
- 验收：`--check` 显示 `Destination = CloudWatchLogs`，且 etl_xray 仍能取到 trace

### T-210b 建图查询 Lambda 并接 AgentCore Gateway · `todo`（cycle-2 新增）
- 这是 T-208 里 Gateway 路线的真实前置，也是审计给「agent 可调用性 ~55%」扣分的正解：
  图查询能力现在锁在 Python 进程和 VPC 里、对外零契约
- 暴露的工具（都已有实现，不用新写逻辑）：`candidate_edges`、`coverage`、
  `select_targets_for_verification`（`chaos/code/runner/edge_verification.py`）+ 爆炸半径 / SPOF（T-231）
- 形态：新 Lambda（VPC 内、挂 `neptune-client-base` 层）→ `CreateGateway(authorizerType='AWS_IAM')`
  → `CreateGatewayTarget(targetConfiguration={'mcp':{'lambda':{...toolSchema}}})`
- **用 `AWS_IAM` 不用 `NONE`**：消费方是账号内 agent，SigV4 即可；
  且这个面会暴露图查询乃至注入能力，`NONE` 被官方明确标「not recommended」
- 依赖：T-231（选边逻辑先定型，免得 toolSchema 反复改）

---

### T-203 补 datastore-flow 链路守门测试 · `todo`
- 位置：`infra/lambda/etl_deepflow/neptune_etl_deepflow.py`
- 零覆盖的 7 个函数：`_resolve_datastore_ips`（CC≈40）、`fetch_datastore_flows`、
  `upsert_datastore_flows`、`_get_eks_k8s_session`、`ch_query_json`、`get_aws_session`、`_first_scalar`
- 背景：这是为消除「微服务->存储」盲区新加的链路（盲区 13→10），**最新、CC 最高、覆盖率为零**
- 验收：`tests/` 中出现这 7 个函数名；`etl_deepflow` 专属测试从 4 个涨到 ≥ 12 个
- 依赖：无

### T-204 补 `deactivate_stale_xray_edges` 测试 · `todo`
- 位置：`infra/lambda/etl_xray/neptune_etl_xray.py`
- 背景：**软删除逻辑本身没有测试**。同批无覆盖的还有 `_load_k8s_alias`、`_key_lower_names`、
  `_src_names`、`_src_name_predicate`
- 验收：软删除的时间窗边界（刚好过期 / 刚好不过期）各有一条断言
- 依赖：无

---

## Stage 1 —— 边验证闭环接进 runner ★ 最高杠杆

### T-210 runner 采集观测方指标 · `todo` ★
- 位置：`chaos/code/runner/runner.py` `_phase1_steady_state_before`（:261）、`_phase3_observe`（:343）
- 现状：只采注入目标自己的指标。`degradation_rate()` 回答的是「打断 B 之后 B 是否退化」，
  **近乎恒真，没有检验任何边**
- 已就绪：`chaos/code/runner/metrics.py` 的 `collect(service, ...)` 可对**任意**服务名采集
- 做法：实验规格带 Observation Target 列表；phase1/phase3 对每个观测方各采一次
- 验收：`grep -n edge_verification chaos/code/runner/runner.py` 非空；
  实验结果对象里出现观测方的 before/after SLI
- 依赖：T-203/T-204（先有干净基线）

### T-211 写回只更新被检验的边 · `done`（提交 `3477896`）
- 位置：`chaos/code/runner/graph_feedback.py:72` `_update_calls_edges`
- 原缺陷：查询把同一判定写给注入目标的**所有出边和入边**：
  `.where(__.outV().has('name',svc).or_(__.inV().has('name',svc)))`
  → 在 svc 注入只能检验「谁依赖 svc」，写上去等于**凭空伪造验证证据**
- 已改为 `:96` `.where(__.inV().has('name','{svc}'))` —— 只更新入边
- 仍待做（归入 T-210）：进一步收窄到本次实验声明的 Observation Target 对应的边

### T-212 去掉边属性上的 `property(single, ...)` · `done`（提交 `3477896`）
- 根因已实测：`400 UnsupportedOperationException: Cardinality specification may not be used with Edge properties`
  → 这条写回 **100% 失败了 21 次**，全被 `except` 吞成 `logger.error`
- `edge_verification.write_verdict()`（`:184`）已改为裸 `property()`，注释里写明理由
- **注意别改错方向**：`graph_feedback.py:116/117/124` 的 `property(single, 'last_chaos_test' /
  'resilience_score' / 'chaos_test_count')` 写的是**顶点**属性，顶点**必须**保留 `single`，
  那几行是正确的，不要一起删（见 north_star §4 不变量 2）
- 仍待做：异常不能再静默吞 —— 至少要计数并在实验报告里显式呈现（归入 T-210）
- 核验：`verify_dod.sh` 的 3.2 已 PASS（只扫 `g.E()` 上下文里的 `property(single`，
  不误伤顶点写入，也不误伤文档字符串）

### T-213 观测方基线请求量下限 · `todo`
- 位置：`chaos/code/runner/edge_verification.py`
- 陷阱：`metrics.collect()` 无数据时 fallback `success_rate=100.0 / total_requests=0` ——
  **零流量和健康在指标上完全一样**，不设下限会把真实边判成"不存在"
- 验收：零流量输入 → `inconclusive`（已有单测，接入 runner 后需端到端复验）
- 依赖：T-210

### T-214 跑通第一条边的真实验证 · `todo`（需批准，见 north_star §6）
- 目标：任选 1 条 `Calls` 边（`candidate_edges('petsite')` 返回 trafficgenerator / gateway-service /
  order-service → petsite 三条入边，方向已验证正确）
- 验收：活图谱可查 `verify_status ∈ {confirmed, refuted}`、`verified_by='chaos-runner'`、`verified_at` 非空
- 依赖：T-210..T-213 全绿

---

## Stage 2 —— 判定分辨力

### T-220 统计判据替换单一阈值 · `todo`
- 依据：Netflix Kayenta 的 judge —— control/experiment 双组 + Mann-Whitney U 聚合分数，
  自动判 pass / fail / 需人工。这是"稳态自动判定"最成熟的公开范式
- 现状证据：72 个历史实验**全部 `passed`**、零失败 → 门槛无分辨力
- 验收：同一份历史数据下，判定结果不再是 100% pass；有一条测试构造"应判 fail"的输入
- 依赖：T-210

### T-221 数据完整性门 · `todo`
- 依据：参考仓库 `workflow-guide.md` §6.0.5
- 规则：无 baseline / 无指标数据 → `OBSERVED (not validated)`，**禁止** `PASSED`
- 理由：否则图里会堆满无证据的假"已验证"，**比没有验证更糟**
- 验收：缺 baseline 的输入不可能得到 `PASSED`（穷举单测）
- 依赖：T-220

### T-222 注入时长穿透 fallback · `todo`
- 依据：微软 Circuit Breaker Pattern 官方文档 —— 重试+熔断组合会让「下游已失败」在上游观测不到
- 做法：注入时长 > 熔断打开窗口 + 重试预算耗尽；把这两个值作为实验规格的必填项
- 风险：不处理会把真实存在的边判成"不存在"，**比不验证更有害**
- 验收：实验规格校验拒绝时长不足的实验
- 依赖：T-220

### T-223 用 `HTTPChaos abort` 而非只用 `delay` · `todo`
- 理由：`abort` 直接短路连接，可绕过部分缓存掩盖；`delay` 会被缓存吸收
- 验收：边验证类实验默认 `action: abort`
- 依赖：T-222

---

## Stage 3 —— AWS 侧单边隔离 + 图查询驱动选边

### T-230 SSM Automation 改 SG 做单边隔离 · `todo` ★
- 参考：`chaos-engineering-on-aws/references/fis-templates/redis-connection-failure/`
  （已下载到 `/home/ec2-user/works/`）
- 为什么必须：**FIS 原生网络故障只能到 subnet/NACL 粒度** —— 在一个子网注入会同时打断该子网
  所有依赖，观测不到"是 A->Redis 断了还是 A->DynamoDB 断了"。改 SG inbound 才是手术刀，
  精确对应图里一条边
- 目标边：54 条 `AccessesData`（Aurora / DynamoDB / StepFunctions）
- 验收：能对单条 `svc -> Aurora` 隔离而不影响同 svc 的 DynamoDB 访问（实测两条边的 SLI 分离）
- 依赖：T-221（判据可信后再扩注入面）

### T-231 图查询驱动选边 · `todo`
- 位置：`chaos/code/runner/edge_verification.py` 已有 `candidate_edges()`，需扩为爆炸半径 / SPOF 排序
- 反面教材：参考仓库 `aws-resilience-modeling` 用 Mermaid + `ID->ID` 字符串，
  **无可查询图模型、SPOF 全靠 LLM 人工推理**、爆炸半径手工估算。我们方向应反过来
- 验收：选边输出带 `blast_radius` / `is_spof` / `verify_status` 三个可解释字段
- 依赖：无

### T-232 仿真优先、live 实验兜底 · `todo`
- 依据：arXiv:2506.11176（ICSE'26 NIER）—— 先从已有制品抽图 + 连通性/副本数仿真估计可用性，
  **只对仿真与观测冲突的边跑昂贵的 live 实验**，在 DeathStarBench 上用真实注入验证过
- 验收：给出"仿真与观测冲突"的边清单，作为实验队列的输入
- 依赖：T-231

### T-233 验证 22 条 P0 候选 · `todo`（需批准）
- P0 全是 `drift_status=declared_not_observed`：CFN 声明了服务访问 DB，但 DeepFlow/X-Ray 从未观测到
  → 要么是死代码路径，要么观测是瞎的（DeepFlow 可能没解 DB 协议）。**两种都必须查清**
- 验收：22 条中 ≥ 10 条得到终态判定（DoD-4）
- 依赖：T-230、T-214

---

## Stage 4 —— LLM 约束与可量化评测

### T-240 假设输出约束 —— **落在 Strands 侧** · `todo`
- ⚠️ 初版这张卡写的是「给 `hypothesis_direct` 加 schema 校验」——按 north_star §1.5 调整方向：
  **约束加在 strands 侧**，direct 只做最低限度同步修补，不在那边建新能力
- **主路径（strands）**：`chaos/code/agents/hypothesis_tools.py`（4 个 `@tool`）+
  `hypothesis_strands.py:249` 的 `Agent(...)`
  - 用 `@tool` 的类型签名 + 结构化输出承担校验
  - `target_services` / `fault_type` 做成**工具入参校验**，非法输入在工具边界即被拒 ——
    这比生成后再校验更强：模型根本拿不到非法选项
- **兜底路径（direct）**：仅去掉三处静默兜底，不加新功能
  1. `hypothesis_direct.py:719` `FAULT_DEFAULTS.get(fault_type, FAULT_DEFAULTS["pod_kill"])` —— 匹配不到静默填 `pod_kill`
  2. 排序分数缺失静默填 5 分
  3. `target_services` 不与拓扑对账
- 验收：非法 `fault_type` / 拓扑外服务在两条路径上都不产生实验；
  strands 路径的拒绝发生在工具边界（有测试证明）
- 依赖：**T-206**（strands 必须先是运行路径）

### T-241 LLM 可见故障 9 → 60 · `todo`
- 现状（已程序化核实）：`VALID_FAULT_TYPES = list(FAULT_DEFAULTS.keys())` 只有 **9** 种
  （`pod_kill`, `pod_failure`, `network_delay`, `network_loss`, `network_partition`,
  `pod_cpu_stress`, `pod_memory_stress`, `dns_chaos`, `http_chaos`），
  而 `fault_catalog.yaml` 有 **60** 条（chaosmesh 19 + fis 37 + fis_scenarios 4）
- 注意：我上一轮报过"52 条"，**那个数字是错的**，以 60 为准
- 做法：`FAULT_DEFAULTS` 改为从 `fault_catalog.yaml` 派生，别再手写第二份
- 验收：`len(VALID_FAULT_TYPES) == 60` 且有一条漂移测试钉住两者相等
- 依赖：T-240

### T-242 边类型 → 注入手段映射表 · `todo`
- 做法：26 类边 x 60 条故障，产出「哪条边能用哪种手段证伪」的映射；
  没有可用手段的边类型要显式标为"当前不可证伪"
- 验收：映射表进 `profiles/graph_contract.yaml` 或独立 YAML，有测试校验覆盖全部 26 类边
- 依赖：T-241

### T-243 LLM 假设可靠性量化评测 · `todo` ★ 业界空白
- 空白依据：ChaosEater（NTT, arXiv:2511.07865, ASE'25 NIER）是唯一有完整闭环的公开系统，
  但其验证是「由人类工程师和 LLM 定性验证 CE 周期是否合理」——
  **没有量化的假设正确率或幻觉率**
- 指标：假设命中率（预测的影响与实测一致的比例）、幻觉率（拓扑外服务 / 目录外故障 / 不存在边的比例）
- 验收：golden 集给出**具体数字**，可复跑
- 依赖：T-240、T-214（要有实测结果才能算命中率）

### T-244 划清 LLM 边界并写进文档 · `todo`
- 反面证据：ReAct 幻觉污染后续结果（arXiv:2502.08224）；LLM 直读高 volume 遥测超上下文、
  推理失败被多 agent 管线掩盖（arXiv:2601.22208）；MAST 指出早期推理错配复合放大
- 结论：LLM 做假设生成与排序、实验代码脚手架、结果自然语言归纳；
  **不做**稳态是否被违反的最终判定（交统计判据）、**不直读**原始遥测（先确定性聚合）
- 验收：代码里不存在"LLM 输出直接决定 pass/fail"的路径
- 依赖：T-220

---

## Stage 5 —— 定期演练（目标 B）

### T-250 EventBridge 调度（建为 DISABLED） · `todo`
- 现状：全量 grep 无 EventBridge / cron / Step Functions / scheduler。
  `action_scheduler.py` 只是复合实验内部的时序编排，**不是**调度
- 做法：Scheduler/Rule → suite 执行入口，先建 `DISABLED`，附启用命令
- 验收：dry-run 成功一次；规则状态为 `DISABLED`
- 依赖：T-221、T-230

### T-251 learn 产物自动回灌 · `todo`
- 做法：下一轮假设生成读上一轮 `verify_status` 与覆盖快照
  （`chaos/code/agents/export_coverage_snapshots.py` 已存在，接上）
- 注意：`learning_direct.update_graph` 那条路径**同样从未生效**（活图谱 `test_coverage` /
  `weakness_pattern` 全为 0），根因同 T-212
- 验收：有测试证明第 N+1 轮的候选集受第 N 轮结果影响
- 依赖：T-212、T-250

### T-252 最小权限注入角色 · `todo`
- 反面教材：参考仓库 MCP quick-start 给的是 `ALLOW_WRITE_OPERATIONS=true` + `AmazonEC2FullAccess`
  （它自己也旁注了生产要最小权限）——**定期自动注入绝不能照抄**
- 验收：角色策略里无 `*FullAccess`，动作逐条列举并限定资源
- 依赖：T-250

### T-253 stop-condition 告警 `notBreaching` · `todo`
- 坑：不设 `--treat-missing-data notBreaching`，实验启动初期无数据会误触发。**这个坑一定会踩**
- 验收：所有 FIS stop-condition 关联告警的 `TreatMissingData` 均为 `notBreaching`
- 依赖：T-250

---

## Stage 6 —— 契约剩余项

### T-260 属性权威表补全 · `todo`
- 已做：`source` 写一次（`coalesce(values(k), constant(v))`），etl_aws + etl_cfn 两处
- 未做（ServiceNow IRE 三机制里的后两条）：**被拒写入要明示**（对应 `maskedAttributes`，
  不能静默丢弃）、**null 值单独控制**（防某个源用空值覆盖好数据）
- 验收：被拒写入有计数与日志；契约里有 `(类型, 属性) -> 权威源` 表且被门禁读取
- 依赖：无

### T-261 节点侧 scoped cleanup · `todo`
- 抄 Cartography：`scoped_cleanup` 限制删除半径、`cascade_delete` 一层、`firstseen` 只设一次
- **多账号 / 多 VPC 扩展之前必须有，否则会互删**
- 验收：单测构造两个 account，确认清理不跨账号
- 依赖：无

### T-262 区分 Simple / Composite 写法 · `todo`
- 背景：`Microservice.az` 曾累积成两个值 —— 本质是把 Composite Node Pattern 当 Simple 写
- 做法：契约里对每个多源写入的节点类型标明属于哪种模式，门禁据此校验
- 验收：多源双写的类型（`Microservice`、`LambdaFunction`、`SQSQueue`、`SNSTopic`、
  `DynamoDBTable`、`StepFunction`、`LoadBalancer`）全部有模式标注
- 依赖：T-260

### T-263 "声明了但无 ETL 建"的类型对账 · `todo`
- 6 个类型：`RDSInstance`、`NeptuneCluster`、`NeptuneInstance`、`Incident`（rca 写）、
  `ChaosExperiment`（chaos 写）、`TopologyChange`
- 做法：归类为「其他模块写」或「声明冗余」，别让 33 vs 31 的差值继续悬着
- 验收：契约里每个类型都有 `written_by` 字段，且有测试校验声明与实际写入方一致
- 依赖：无

---

## Stage 7 —— 部署与清理（全部 blocked 在用户批准）

### T-270 部署四个 ETL 函数代码 · `blocked`（用户批准）
- 已就绪：层 `:6` 已发布，四个函数**均已指向 `:6`**（顺带修掉了 xray 在 `:5`、其余 `:2` 的既存漂移）
- 未生效的修复都在函数包里：`find_vertex_by_name` 两参数签名、契约写入门禁
- 回退：设 `GRAPH_CONTRACT_MODE=warn` 即降为只告警，不必回滚代码
- 循环该做到：打好包 + 一条可执行命令 + 验证层就绪。**不执行**

### T-271 清 183 条错源边 · `blocked`（依赖 T-270）
- 命令：`infra/fix_wrong_source_edges.py --apply`
- **必须在 T-270 之后**：旧代码下一轮 ETL 会原样重建，现在清等于白删
- 数据：`Microservice -[RunsOn]-> Pod` 正确的只有 36 条，错源 173 条（83% 错）。
  其中 108 条经 `_K8S_SVC_ALIAS` 补全后可转为正确边，剩 65 条
  （`Namespace chaos-mesh/deepflow`）本就不该有边

### T-272 开启边过期收敛 · `blocked`（依赖 T-270）
- 动作：设 `GRAPH_EDGE_EXPIRY_ENABLED=true`
- 现状：活图谱 dry-run 0 条待失效 —— 存量边只有 `last_updated`/`last_scanned` 而无 `last_seen`，
  查询要求 `has(last_seen, ...)`，**历史边不会被误置 false**（这是刻意的保守方向）

---

## Stage 8 —— 闭环：用 chaos 实际校正这张图 ★ 终点

### T-280 成批验证 94 条依赖边 · `todo`（需批准）
- 按 T-231 的排队规则（爆炸半径 / SPOF / `untested` / `declared_not_observed` 优先）跑批
- 验收：`coverage()` 报出的 `untested` 数持续下降，每轮留快照
- 依赖：T-214、T-230、T-233

### T-281 refuted 边强制归因 · `todo` ★
- 每条 `verify_status='refuted'` 的边必须归为三类之一，**不允许悬空**：

  | 归因 | 含义 | 修正动作 |
  |---|---|---|
  | 幽灵边 | 源头 ETL 造出的假边 | 置 `active=false` **且**修掉造它的 ETL 代码路径 |
  | 观测盲区 | 边真实存在但采集看不见（如 DeepFlow 未解 DB 协议） | 边保留，改采集侧或 `drift_status` 语义 |
  | 弱依赖 | 边存在但非 load-bearing | 保留，标注影响强度，**不删** |

- **删边门槛**：观测方基线请求量达标 + 注入时长穿透熔断/重试 + `verify_refute_count >= 2`。
  一次 refuted 不足以删边（不变量 7）
- 验收：`refuted` 边数 == 已归因边数；有一条测试拒绝"单次 refuted 即删边"
- 依赖：T-280

### T-282 走完一条边的完整闭环并留前后对比 · `todo` ★
- 流程：注入 → `refuted` → 归因 → 改代码或改图 → **复跑验证** → 转 `confirmed` 或确认为幽灵边
- 双向留痕：图上 `verify_reason`，代码提交信息带边的三元组，两者能相互指认
- 高价值靶子：22 条 P0（`declared_not_observed`）必然落进"幽灵边"或"观测盲区"，
  **两种都是真问题**，分清即有价值
- 验收：至少 1 条边有完整前后对比记录
- 依赖：T-281

### T-283 归因分布进交接文档 · `todo`
- 内容：多少条幽灵边 / 多少条观测盲区 / 多少条弱依赖
- **这个分布就是"这张图有多准"的量化答案**，也是 DoD-4 覆盖率数字的意义所在
- 依赖：T-281

---

## 上一轮已完成（2026-08-30，commits `c48d63c` / `2988c75` / `186d99f` / `3477896`）

用户 2026-08-30 17:04 问这四项是否在任务里 —— **它们已经做完了，不是待办**，
本节保留为已完成记录，避免被当成开放任务重做。全部经活环境复核（不是凭记忆）：

| 动作 | 实测状态 |
|---|---|
| `migrate_identity_keys.py --apply` 合并 VPC 重复实体 | ✅ `VPC` 3→2 节点，`vpc_id` 撞车 **0** 组（活图谱查询复核：`vpc-010ab37a3f9f74725`/`ServicesEks2/PetSiteVPC`、`vpc-06731f30388b57818`/`agent-vpc-v2`，各 1 个） |
| 打包发布新层 + 四个函数指到新版本 | ✅ 层 `:6`，四个函数**全部** `layer=6`（顺带修掉 xray 在 `:5`、其余 `:2` 的既存漂移） |
| `petsite.yaml` 的 `WritesTo` 三个错端点名 | ✅ `:377` 现为 `(:Microservice)-[:WritesTo]->(:SQSQueue|:SNSTopic|:S3Bucket)` |
| 端点约束 warn → enforce | ✅ `graph_contract.py:66` 默认 `MODE_ENFORCE`，四个函数均**未**设 `GRAPH_CONTRACT_MODE` 覆盖 |

顺带在那一轮查出并修掉的真缺陷（也已完成）：
- `find_vertex_by_name` 两层都不带标签 → `label` 改为必需参数，四处调用点全传 `Microservice`
- `:558` 遗漏的 `_K8S_SVC_ALIAS` 映射（`:592` 一直有）→ 108 条边可转为正确边
- 端点白名单从平铺 `src`/`dst`（笛卡尔积）升级为 `pairs` 配对校验
- 迁移脚本改挂边时不去重造出 13 条平行重复 `LocatedIn` → 已清理并补 `dedupe_parallel_edges`

**这批唯一的残留就是 Stage 7 的三张卡**（T-270/271/272）—— 函数**代码**还没部署，
所以 183 条错源边现在清等于白删（旧代码下一轮 ETL 会原样重建）。

---

## Cycle 日志

> 每轮追加一行：`## Cycle-N (UTC 时间) — 做了哪张卡 / 结果 / 下一张`

### Cycle-3 (2026-08-30 18:50Z) — T-208 接入 AgentCore Observability，DoD-9 四项全绿
- **取舍改判（基于实测，不是上轮的排序）**：上轮把 Gateway 排第一，查清前置后改选 **Observability**。
  - Gateway 的好消息：`authorizerType` 实测含 **`AWS_IAM`/`NONE`**，**不需要先立 Cognito**
    —— 推翻我原以为的大前置（inbound-auth 文档只讲 JWT，CFN 还写着 CustomJWTAuthorizer 必填）
  - Gateway 的真阻塞：`targetConfiguration.mcp` 最自然的是 `lambda`+`toolSchema`，
    但**账号里没有任何图查询 Lambda**（现存 7 个全是 ETL/RCA），`.mcp.json` 空 → 拆出 **T-210b**
  - Observability 是配置级、零新建计费资源：`strands_common.py` 已有完整 telemetry 骨架，
    **console 模式实测出真 span** 且是 GenAI 语义约定（`invoke_agent`/`chat`/`gen_ai.choice`…）
- 已做：装 `bedrock-agentcore 1.22.0` + `aws-opentelemetry-distro 0.19.0`；
  建日志组 `/aws/bedrock-agentcore/runtimes/graph-dep-chaos-agent` + 流（14 天保留）；
  写 `chaos/scripts/enable_agentcore_observability.sh`（`--check` 实跑正确识别闸门）
- **一个闸门没翻，理由是真的风险不是保守**：Transaction Search 实测 `Destination=XRay`
  （需 `CloudWatchLogs`）、采样 0.0。它是账号级 + 计费 + **可能影响本项目自己的 `etl_xray`**
  （读 X-Ray 建拓扑边，切 destination 是否影响 `GetTraceSummaries` 未验证）。
  没验证就翻会静默打断一条 ETL 数据源 → 卡 **T-208b**，含回退命令
- 顺带发现账号里已有一个 **不属于本项目** 的 memory `xgg_memory-5M0VYBCeFS`，未触碰
- `verify_dod.sh`：PASS 6 → **7**，**DoD-9 全部四项转绿**；测试 443 passed / 0 failed 无回归
- 下一张：**T-210**（Stage 1 ★ 最高杠杆，runner 采集观测方指标）——
  它解锁 DoD-3 → DoD-4 → DoD-10 整条链，而 T-208b/T-210b 各自有前置

### Cycle-2 (2026-08-30 17:31Z) — T-205 + T-206 完成，Strands 成为实际运行路径
- **T-205 done**：装 strands-agents 1.54.0 / strands-agents-tools 0.8.7，
  `requirements-dev.txt` 从可选段上移为必装、下界改 `>=1.36`。
  五个 import 路径全通；最小端到端调用 **1.2s** 成功。
- **T-206 done**：六处默认值 `direct` → `strands`，
  **逐个实测 6/6 返回 Strands 实现**（清空所有 `*_ENGINE` env 后调工厂）。切换后零回归。
- **T-207 前半已满足**：六处回退**本来就有** `logger.warning` —— 初版假设它缺失是错的。
  剩能力对账测试。
- 测试水位 442 → **443 passed / 0 failed**（装 strands 净解锁 1 个）。
  `verify_dod.sh`：PASS 3 → **6**，DoD-9 前三项全绿。
- 四处实测更正 / 新发现：
  1. **真实 env 名是 `CHAOS_RUNNER_ENGINE` / `POLICY_GUARD_ENGINE`**，
     我在 cycle-0/1 写成 `RUNNER_ENGINE` / `GUARD_ENGINE` —— 已同步修 north_star
  2. strands 测试**不是被 strands 缺失挡住的**，而是 12 个 golden/shadow 文件全挂在
     `RUNN_GOLDEN=1` 之后 —— 这解释了为什么装上只解锁 1 个测试
  3. `test_hypothesis_shadow` 20 分钟超时**不是挂死**：单次 generate 实测
     strands 36.5s vs direct 20.1s，20 场景 x 2 引擎 ≈ 19 分钟。下次给它 ≥30 分钟
  4. 新卡 **T-209**：strands 1.54 报 `cache_prompt is deprecated`，
     影响 `strands_common.build_bedrock_model`，需实测缓存是否真失效
- 一个环境事实（不是本项目缺陷）：`kubectl` 不在 PATH，
  假设生成会为 ~10 个服务各 spawn 两次失败的 subprocess（实测 40 次失败），拖慢但不致错
- 下一张：**T-207 后半**（能力对账测试）或 **T-208**（接 AgentCore Gateway）。
  优先 T-208 —— 它关 DoD-9 最后一项，且 Gateway 直接补上"agent 可调用性"的扣分点

### Cycle-1 (2026-08-30 17:10Z) — 补入两条用户追加的硬约束，并纠正一处方向性错误
- **新增 DoD-9（Strands/AgentCore 强制）**。用户明确「Strands 一定要用，LLM 开发尽量用
  Strands 和 AgentCore」。这条约束初版**完全没有**，而且 `T-202` 写的是「删除 strands 死代码」
  ——**方向正好相反**，已置 `wontfix` 并替换为 T-205/206/207/208
- **新增 DoD-10（闭环校正）**。用户追加「chaos 建成后要用它验证依赖关系，不符合就去改」。
  初版只到"得到判定"，现在要求判定回流成修正动作，refuted 边强制三类归因，
  且删边门槛设为 `verify_refute_count >= 2`（一次 refuted 不足以删边）
- **新增 Stage 0.5 / 0.6 / 8**，Stage 4 从「给 direct 打补丁」改向「约束落在 strands 工具边界」
- `verify_dod.sh` 补 DoD-9 四项检查，复跑：**PASS=3 FAIL=9 SKIP=5**（9.3 精确数出 6 处默认 direct）
- 三处实测更正：
  1. strands **不是死代码** —— 19 文件 / 4583 行、6 个引擎开关、13 处回退分支
  2. 目标版本是 `>=1.36`（`rca/engines/factory.py:36`），**不是 0.x** —— 我一度也说错
  3. AgentCore 东京区**四项全可用**，"区域不支持"不能作为不接的理由
- 复核用户问的四项运维动作：**全部已完成**（见「上一轮已完成」节，逐项活环境验证）
- 下一张：**T-205**（装 strands，Stage 4 的硬前置）→ **T-206** → 然后 T-200/203/204 建干净基线

### Cycle-0 (2026-08-30 16:50Z) — 工作定义建立
- 建 `north_star.md`（8 条 DoD、12 条运行不变量、5 条停止条件、4 项需批准动作）、
  `roadmap.md`（Stage 0-7 + Stage X）、`tasks.md`（34 张卡）、`verify_dod.sh`、`README.md`
- **实跑 `verify_dod.sh --local-only` 复验了脚本本身**：PASS=3 FAIL=5 SKIP=4，
  全量测试 442 passed / 0 failed（与基线一致）
- 三处自我更正，都靠实测而非记忆：
  1. `fault_catalog` 是 **60** 条（19 chaosmesh + 37 fis + 4 scenarios），
     我上一轮报的 **52 是错的**；`FAULT_DEFAULTS` 实测 9 种（第一版正则匹到了 `TIER_CONFIG`）
  2. **T-211 / T-212 在 `3477896` 里已经修完**，我最初把它们错标成 `todo` ——
     `write_verdict()` 已用裸 `property()`，`_update_calls_edges` 已只写入边
  3. DoD-3 里我写的 `verified_by` / `verified_at` **不是真实属性名**，
     实际是 `verify_by` / `verify_last`，写错会让核验查错键
- 下一张：**T-200**（删异常文件，零风险）→ **T-203/T-204**（补测试，建立干净基线）
  → 直奔 **T-210**（Stage 1 最高杠杆，目标 A 的最后一寸）
