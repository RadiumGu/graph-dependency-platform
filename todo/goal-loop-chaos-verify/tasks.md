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
| 1 边验证接入 runner ★ | 11 | 11 | 0 | 0 | 0 |
| 2 判定分辨力 | 4 | 0 | 0 | 0 | 4 |
| 3 AWS 侧单边隔离 + 选边 | 4 | 0 | 0 | 0 | 4 |
| 4 LLM 约束与评测 | 5 | 0 | 0 | 0 | 5 |
| 5 定期演练 | 4 | 0 | 0 | 0 | 4 |
| 6 契约剩余项 | 8 | 3 | 0 | 0 | 5 |
| 7 部署与清理 | 3 | 3 | 0 | 0 | 0 |
| **9 FIS 打开托管资源边** ★ | 7 | 2 | 1 | 0 | 4 |
| **8 闭环校正** ★ 终点 | 4 | 0 | 0 | 0 | 4 |
| （T-202 方向写反，作废） | 1 | — | — | — | wontfix |
| **合计** | **61** | **22** | **3** | **0** | **36** |

基线：提交 `3477896` 时 **442 passed / 145 skipped / 0 failed**。
**当前水位：487 passed / 144 skipped / 0 failed**（python3.11）。
活图谱（2026-08-31 16:13Z 实测）：94 条依赖边，
**confirmed 10 / refuted 1 / inconclusive 2 / untested 81 —— 已判定覆盖率 13.83%**
（今晨开始 0.0%）。横跨 `Calls` / `AccessesData` 两种边类型，
Chaos Mesh / FIS 两种后端，且出现**第一条被证伪的边**（`petsearch -> s3`）。
端点组合违约 **0**（清 211 条后完整跑一轮 ETL 复核）；未声明 source 取值 **0**。
`verify_dod.sh --local-only`：cycle-0 PASS=3 → cycle-1 FAIL=9 → cycle-2 PASS=7 →
**cycle-4 后 PASS=8 FAIL=4 SKIP=5**。**DoD-9 四项全绿，DoD-3 的 3.1/3.2 转绿**。

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

### T-210 runner 采集观测方指标 · `done`（cycle-4）★
- **三角色标注落地**：新增 `ObservationTarget`（`chaos/code/runner/experiment.py`），
  接受 `'svc'` / `'ns/svc'` / dict 三种写法，带 `edge_label` 与 `min_baseline_requests`。
  YAML 里写 `target.observers:` 或顶层 `observation_targets:` 均可，
  默认边类型取 `graph_feedback.edges` 第一项（与写回边类型保持一致，避免两处漂移）
- **Phase1** `_collect_observer_baselines`：为每个观测方采基线。
  **刻意不因观测方无流量而让实验失败** —— 那是数据质量问题，应由判定层判 inconclusive；
  判 refuted 才有害（会删真实边）
- **Phase3** `_collect_observer_snapshots`：注入期逐点采样；单个观测方失败不中断实验
- **Phase3 结束** `_log_observer_evidence`：打印逐条边的证据（退化/基线请求/采样/usable），
  只呈现不判定 —— 阈值全在契约的 `edge_verification`，单一声明
- **收尾** `_verify_edges`：按 `candidate_edges(injection_target)` 的 `observer` 字段
  与观测方一一对应，逐条 `verify_edge` + `write_verdict`。
  注入期请求量取各窗口**最大值**而非求和：每个快照本身是 60s 窗口计数，
  求和会因窗口重叠虚高；取 max 与基线同量纲，且真零流量时仍为 0 ——
  偏向「判不了」而非「判边不存在」，与不变量 7 同向
- **零条写回成功时显式报 error**：这正是历史上被静默吞掉 21 次的症状，不能当「没边可写」放过
- 结果模型（`result.py`）新增 `observer_steady_before` / `observer_snapshots` /
  `observer_min_success_rate` + `observer_degradation_rate()` / `observer_has_real_traffic()` /
  `observer_evidence()`。**缺基线或缺注入期采样时返回 `None` 而不是 `0.0`** ——
  0.0 会被下游读成「完全没退化」从而判 refuted
- 测试 `tests/test_37_observer_metrics.py` **12 条全绿**，其中 o06 是最关键的一条：
  零流量必须判 `inconclusive`、绝不能是 `refuted`
- 示例实验 `chaos/code/experiments/tier1/verify-edges-into-search-service.yaml`：
  在 `search-service` 注入 `http_chaos abort`（不用 delay，见 T-223 理由），
  观测 `petsite` + `list-adoptions` —— 这两条边 2026-08-29 实测确有真实流量
  （12,236 / 2,028+1,014 请求），含爆炸半径说明。解析实测正确识别 2 个观测方

### T-213 观测方基线请求量下限 · `done`（随 T-210 一并落地）
- `ObservationTarget.min_baseline_requests`（默认 10）+ `observer_has_real_traffic()`
- 判定侧 `classify_intervention` 的 `min_observation_requests` 门在基线**与**注入期两侧都查
- 陷阱已固定为测试：`metrics.collect()` 无数据时 fallback
  `success_rate=100.0 / total_requests=0` —— **零流量和健康在指标上完全一样**

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

### T-214 跑通第一条边的真实验证 · `done`（2026-08-31 08:47Z）★
- **实测结果（活图谱可查）**：
  - `petsite -[Calls]-> petsearch` → **confirmed**，置信度 **0.9959**，退化 74.92%
  - `petlistadoptions -[Calls]-> petsearch` → **confirmed**，置信度 **0.9933**，退化 71.93%
  - `verify_by=chaos-runner`、`verify_experiment=exp-search-service-http-chaos-20260831-084223`
  - **已验证覆盖率 0.0% → 2.13%（2/94）**
- 判定靠的是**合成退化率**：成功率只掉 3.51pp，吞吐塌陷 74.92%（abort 不产生 response 行，
  成功率对它是盲的）。只看成功率会把这条真实的边判成 refuted
- 拿到判定前又修掉三个缺陷，见 T-214e / T-214f / T-214g
- 入口：`chaos/code/experiments/tier1/verify-edges-into-search-service.yaml`
  （在 `search-service` 注入 `http_chaos abort` 3m，观测 `petsite` + `list-adoptions`）
- 先 `--dry-run` 走通相位与规格校验，再真实注入（向非生产 EKS 的注入已获概括授权）
- 验收：活图谱可查 `verify_status ∈ {confirmed, refuted}`、`verify_by='chaos-runner'`、
  `verify_last` 非空（属性名以 `write_verdict()` 实际写入的为准，不是 `verified_*`）
- 依赖：T-210 ✅、T-213 ✅。**注意 `kubectl` 不在 PATH**（cycle-2 实测），
  真实注入前要先解决这个环境问题

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

### T-264 `source` 词表门禁 · `done`（2026-08-31）★
- **缺陷**：`SOURCES` 从引入起就在契约里、也被 `graph_contract.py` re-export，
  但**没有任何一处检查读它**。活图谱普查实测 4 种未声明取值共 **1240 条**：
  `eks-etl` 1228（handler.py 13 处在写）、`deepflow` 8 个节点、`aws-etl-static` 3、`manual` 1
- **对照**：同一份 YAML 里**被** `assert_edge_type` 检查的节点/边类型 **零漂移**。
  漂移量与「有没有门禁」相关，与「声明得好不好」无关
- 已做：
  1. `graph_contract.assert_source()` + `is_declared_source()`，走既有 `GRAPH_CONTRACT_MODE`
  2. 契约补声明 `eks-etl` / `aws-etl-static`（**刻意的语义区分**，不是拼错：
     K8s API vs AWS 控制面、静态声明 vs 运行时观测）
  3. `deepflow` 判为 `deepflow-etl` 的**同义漂移**，收敛写入侧而**不**扩词表 ——
     否则 `edge_verification._OBSERVER_MARKERS` 这类按源分派的逻辑会漏判
  4. `upsert_vertex` / `upsert_edge` 两个收口点接门禁
  5. `test_38` 的 g04 静态扫描兜住没有运行时收口点的 ETL（deepflow/xray/cfn 各自手拼 Gremlin）
- 验收：`test_38_source_vocabulary.py` 8 个用例全绿；全量 461 → **469 passed / 0 failed**
- 依赖：无

### T-265 `upsert_edge` 丢弃调用方 `source` · `done`（2026-08-31）★
- **这是 T-264 查证过程中带出的更严重缺陷**，两者必须一起修：
  不修它的话门禁形同虚设 —— 所有边都写死 `aws-etl`，自然永远合规
- **缺陷**：写一次语义被实现成「丢弃调用方取值」——
  `write_once = {'source': 'aws-etl'}` 硬编码 + `if ks in write_once: continue`。
  于是 handler.py 里 **13 处 `eks-etl` + 1 处 `aws-etl-static` 全部静默失效**
- **实测确认**：`upsert_edge(..., {'source':'eks-etl'})` 生成的 Gremlin 里只有 `'aws-etl'`
- **为什么活图谱看不出来**：`coalesce` 保护了存量边，所以 1228 条 `eks-etl` 还在。
  这个 bug 只影响**此后新建**的边 —— 不会立刻暴露，只会让 provenance 缓慢腐坏
- 正确语义是两件事分开：写一次 = **已存在的边不覆盖**（coalesce 保证）；
  取什么值 = **首写者说了算**（调用方传进来的那个）
- 顺带保留了上一版**正确**的部分：`dependency_kind` 仍由函数按边类型判定，
  调用方不得指定（g08 守门），否则 static/dynamic 会随调用点各说各话
- 验收：g05/g06/g07/g08 四个用例；`dependency_kind` 不被调用方覆盖
- 依赖：无

### T-266 存量 `source` 取值归一 · `done`（2026-08-31 08:59Z，用户批准）
- 8 个 `Microservice` 节点 `deepflow` → `deepflow-etl`；1 条边 `manual` → `manual-fix`
- 复核：**图里出现的每个 source 取值都在契约词表内**（节点 3 种 / 边 11 种，未声明 0）
- 归一后复跑 deepflow ETL 正常（`within('deepflow','deepflow-etl')` 兼容读生效）
- 对象：8 个 `Microservice` 节点 `source='deepflow'` → `deepflow-etl`；
  1 条 `ProtectsAccess` 边 `source='manual'` → `manual-fix`（无代码在写，手工遗留）
- **必须在 T-270 之后**：旧代码还在线上时，下一轮 deepflow ETL 会把 `deepflow` 原样写回
- 读取侧已做兼容：`etl_deepflow` 的存量清理查询改为
  `has('source', within('deepflow','deepflow-etl'))` —— 只改写入侧会让那条查询
  从此匹配不到任何节点，属于「修一个缺陷引入一个静默失效」，本仓库已经犯过一次（F8）
- 循环该做到：写出可执行命令 + 影响面。**不执行**

### T-267 无 `source` 的 230 条边 / 209 个节点 · `todo`
- 现状：边 `TriggeredBy` 126、`TestedBy` 60、`MentionsResource` 16、`Calls` 11、
  `Involves` 8、`LocatedIn` 6、其余 3；节点 209 个
- 写入方多为 rca（Incident）与 chaos（ChaosExperiment），它们不走 etl_aws 的收口点
- **先判定该不该强制**：`source` 对「谁首先发现了这条依赖」有意义，
  对 Incident/ChaosExperiment 这类**本模块自产实体**未必有意义。
  草率地强制 required 会逼出一堆无信息量的 `source='rca'`
- 验收：契约里对每个边类型标明 `source_required: true|false` 并有测试校验
- 依赖：T-263（`written_by` 对账）—— 两张卡回答的是同一个问题的两面

### T-214e 候选边查询用 K8s 名匹配图谱规范名 · `done`（2026-08-31）★
- **症状**：实验 PASSED、证据齐全、`usable=True`，但写回 **0/2**，
  日志只说「图谱中无 `petsite -> search-service` 的候选边」——而两条边**明明在图里**
- **根因**：`candidate_edges()` 拿 K8s 服务名匹配图谱节点名，而图谱 Microservice 用规范名
  （`search-service`→`petsearch`、`list-adoptions`→`petlistadoptions`）；
  且查询**不带标签**，匹到了同名的 Deployment / K8sService 节点，它们没有 Calls 入边
- **与 183 条错源边同一根因家族**：按名字匹配、不带标签、名字跨标签重复（本图 12 组）
- 修法：走 `shared/service_registry.ServiceRegistry.resolve()`（解析表唯一来源是
  `profiles/petsite.yaml`，与 ETL 的 `service_mappings.json` 同源）；观测方名字同样解析；
  **「无候选边」从 info 升级为 warning** 并打印候选边实际源端点
- 依赖：无

### T-214f 采集失败伪装成「零流量」污染谷值 · `done`（2026-08-31）★ 最危险
- **症状**：修完 T-214e 后拿到证据，但 `petsite` 与 `list-adoptions` **同时**
  「谷值请求=0 / 吞吐塌陷 100%」——而 petsite 被负载机持续打流量，不可能真的归零
- **根因**：`metrics.collect()` 查询异常时 fallback 成 `(100%, 0 requests)`，
  `observer_min_requests` 取 min，一次 ClickHouse 抖动就把谷值压成 0
- **为什么最危险**：它不让实验失败，而是产出**高置信度的错误 confirmed**
  （100pp 远超 20pp 判定线）。静默产出错误结论的系统比报错的系统坏得多
- 深层原因：`(success_rate=100, total_requests=0)` 三义组合——「查询失败」「真零流量」
  「真健康」在数据结构上无法区分。成功率通道当初专门防过，吞吐通道后加、没跟上纪律
- 修法：`MetricsSnapshot.ok` 显式标志；`ok=False` 只入列表留痕、不参与任何 min；
  `collect_steady` 只用 ok 采样点算均值；`_verify_edges` 的注入期请求量同样只看 ok
- 验收：修后同一条边 `petsite` 基线 658 → 谷值 **165**（不再是 0），吞吐塌陷 74.92%；
  守门用例 `test_o90` / `test_o91`
- 依赖：无

### T-214g Lambda 层缺 `requests`，`etl_xray` 已死 2 天 · `done`（2026-08-31）★
- **症状**：`etl_xray` 每次调用 `ModuleNotFoundError: No module named 'requests'`，
  查日志**自 2026-08-29 08:00 起一直失败**，发现时已 2 天
- **根因**：`neptune_client_base` 懒加载 `requests`；其余三个函数各自 vendored 了它
  （包 600KB–1.4MB），**只有 etl_xray 不带**（16KB），靠层提供——而层 `:6` 里从来没有
- **最值得记的不是 bug 而是它活了 2 天**：一整个观测源静默死亡、无人发现。
  而本项目的命题正是「你怎么知道边是真的」——数据源能死两天没人知道，
  那么基于它的所有「未观测到」判定都是假阴性。这是 X-Ray 85% 假阴性教训的**运维版**
- 修法：共享依赖放进层，发布 `:8` = 5 个 `.py` + requests/urllib3/certifi/idna/charset_normalizer
- **记一次我自己的失误**：先发的层 `:7` 只打了 5 个 `.py`，凭源码目录推断层内容。
  教训：替换共享产物前先下载旧产物对比，不要凭目录推断
- 依赖：无

### T-214h Phase 5 只看 SLI，不看 Pod readiness · `done`（2026-08-31 10:40Z）★
- **症状**：`abort` 注入必然打伤目标 Pod（liveness 探针失败 → 重启循环，
  **CRD 已正常删除也一样**，只能删 Pod 重建）。本轮两次注入各处置一次
- 但实验**报 PASSED**：Phase 5 只查 SLI，而 SLI 由 HPA 新拉的健康 Pod 撑着看起来正常
- 「abort 打伤 Pod」是固有代价不是缺陷；**「留下坏 Pod 却报 PASSED」是缺陷**
- **两条判据，主判据是重启差值而非 readiness**：readiness 有滞后（实测 Phase 4 报
  `2/2 running` 之后**还要 2.5 分钟**才退化），而 `restartCount` 在注入期间就已递增
- `check_pods` 扩展返回 `restarts` / `per_pod_restarts`；Phase 0 存基线，Phase 5 比差值
- 缺基线时**不判 FAILED 而是显式留痕**（`restarts=None` 区分「没测到」与「没有重启」）
- FIS 后端刻意跳过该判据 —— 实测 FIS 路径不产生 tproxy 残留、不打伤 Pod
- **首次真实生效即抓到 3 次损伤**，其中 pethistory 那轮是决定性验证：
  `✅ Pod 检查: 2/2 ready` 但 `❌ 重启 +2` → 判 FAILED。**旧代码在这个场景会报 PASSED**
  （list-adoptions 与 pay-for-adoption 两轮更严重：2/2 → 0/2，重启 0 → 12）
- 验收：`tests/test_39_pod_damage_gate.py` 7 个用例；报告新增「注入目标 Pod 健康」节
  与处置命令。全量 471 → **478 passed / 0 failed**
- 依赖：无

### T-290 用 FIS 打开 78 条「被依赖方是 AWS 托管资源」的边 · `doing`（2026-08-31）★
- **此前判断这批边阻塞在 T-230（自建 SSM 改安全组），这个判断是错的** ——
  FIS 原生就有需要的动作，实测账号内可用：
  | FIS action | 可验证 |
  |---|---|
  | `aws:rds:reboot-db-instances` | RDSCluster 14 / RDSInstance 3 ✅ **本轮已用** |
  | `aws:network:disrupt-vpc-endpoint` | AWSServiceEndpoint 11（ssm/dynamodb/sts/s3/xray） |
  | `aws:eks:pod-network-blackhole-port` | **T-230 想要的手术刀式单边隔离，FIS 原生有** |
  | `aws:lambda:invocation-error` | LambdaFunction 8（需 Lambda 扩展层） |
  | `aws:s3:bucket-pause-replication` | S3Bucket 7（仅复制场景，用途有限） |
- 本轮已完成 Aurora（见 T-291）。下一步优先 `disrupt-vpc-endpoint`（11 条，
  且能同时覆盖走端点的 DynamoDB/S3 流量），再评估 Lambda
- **ECRRepository 12 条判定为不可运行时验证**：镜像拉取只在 Pod 启动时发生，
  属启动期依赖，注入无从观测 —— 如实记为「不适用」而非「待验证」
- 依赖：无（T-230 可降级或作废，FIS 已提供同等能力）

### T-291 Aurora 边验证（首个 FIS 边验证实验）· `done`（2026-08-31 11:10Z）★
- 规格 `chaos/code/experiments/tier1/verify-edges-into-aurora.yaml`
- **实测结果 4/4 写回**：
  | 边 | 判定 | 置信度 | 退化 |
  |---|---|--:|--:|
  | `list-adoptions -[AccessesData]-> Aurora` | confirmed | 0.998 | 63.91pp |
  | `pay-for-adoption -[AccessesData]-> Aurora` | confirmed | 0.998 | 59.57pp |
  | `petsite -[AccessesData]-> Aurora` | **confirmed** | 0.982 | 36.21% |
  | `pethistory -[AccessesData]-> Aurora` | inconclusive | 0.881 | 基线 12 < 20 下限 |
- **`petsite -> Aurora` 是本轮最有价值的一条**：它此前只有 `deepflow-dns` 单源证据
  （「观测到 petsite 解析过 Aurora 域名」）。DNS 解析不等于真的查库 ——
  判 confirmed 说明这条单源边是真的，DNS 证据在这里没有制造假边
- 两个实测事实修正了原计划：
  1. 该集群**只有一个实例、MultiAZ=false**，`failover-db-cluster` 不适用，改用 reboot
  2. 初版 `namespace: rds` 被 PolicyGuard R002 正确拒绝 —— 见 T-292
- 实验整体判 **INCONCLUSIVE**（数据完备性门生效）：RDS 目标侧没有可用 SLI，
  这是如实的降级而不是失败
- **FIS 路径不打伤 Pod**（无 tproxy 残留、无 CRD 残留），Aurora 自愈回 `available`

### T-292 PolicyGuard R002 命名空间白名单对 FIS 目标语义不适用 · `todo`
- 现象：FIS 打 AWS 资源，而 R002 校验的是 K8s 命名空间白名单
  `[petsite-staging, petsite-canary, chaos-sandbox, petadoptions]`
- 实测：`namespace: rds` 被 DENY。**据此可判断仓库原有的
  `experiments/fis/rds/fis-aurora-reboot-petlistadoptions.yaml` 从未真正执行过** ——
  它写的也是 `namespace: rds`
- 本轮处置：把 namespace 填成**受影响的应用命名空间** `petadoptions`（既在白名单里，
  也确实是爆炸半径所在），**刻意不放宽白名单** —— 后者才是危险做法
- 待做：给 AWS 资源类目标定义清楚 namespace 字段的语义并写进规格文档 +
  R002 增加一条「backend=fis 时校验受影响命名空间」的显式规则，别靠约定
- 依赖：无

### T-293 观测方流量不足使 4 条边只能判 inconclusive · `todo`
- `pethistory` 实测仅 12–26 请求/120s，两次实验都因未过 20 下限判 inconclusive
  （`pethistory -> petlistadoptions`、`pethistory -> Aurora`）
- 判 inconclusive 是**正确行为**（零流量与健康在指标上分不开，不得判 refuted），
  但也意味着这些边永远拿不到结论
- 做法二选一：给负载机加 pethistory 详情页的流量配比；或延长注入与观测窗口
  让累计请求量过线。**不要降低 `min_observation_requests`** —— 那是拿判据换覆盖率
- 依赖：无

### T-294 用 FIS 验证 AWSServiceEndpoint 边 —— 4 条可做、7 条做不到 · `done`（2026-08-31 16:13Z）★
- **用户指定的 `aws:network:disrupt-vpc-endpoint` 对这批边无从施加**（实测）：
  该动作 `targets: VPCEndpoints -> aws:ec2:vpc-endpoint`，而 PetSite VPC
  （vpc-010ab37a3f9f74725）里**只有一个**端点 `vpce-0b35b2472df108d50`
  （guardduty-data）。ssm/sts/xray/s3/dynamodb 一个端点都没有 ——
  那些端点全在 **agent VPC**（vpc-06731f30388b57818，Kiro Crew 本机所在 VPC），
  与被观测的工作负载无关。PetSite 的服务经 NAT 走公网访问这些 API
- **改用 `aws:network:disrupt-connectivity`**（`targets: Subnets`，
  `scope=dynamodb|s3` 用 AWS 托管前缀列表挂拒绝 NACL，公网路径同样被拦），
  覆盖 11 条里的 4 条
- **剩下 7 条（ssm 4 / sts 2 / xray 1）任何 FIS 动作都做不到**：既无端点可断，
  AWS 也不为这三个服务发布托管前缀列表（只有 S3/DynamoDB/CloudFront/
  Ground Station/VPC Lattice 有）。替代路径见 T-296
- 代码改动：`fis_backend` 的网络类目标加 `subnet_arns` 多子网支持 ——
  EKS 的 Pod 跨两个私有子网（11.0.2.0/24 在 1a、11.0.3.0/24 在 1c），
  只断一个 AZ 会让退化率落进 inconclusive 中间带，等于自己削掉判据分辨力
- 顺带查出目录缺陷：`fault_catalog.yaml` 的 `fis_vpc_endpoint_disrupt` 声明
  `requires: [subnet_arn]`，而该动作实际要求 `aws:ec2:vpc-endpoint` 目标 ——
  声明与 AWS 侧不一致，**该故障类型从未被真正执行过**
- **结果**：`petsearch -> dynamodb` confirmed（1.000，成功率+吞吐双通道）、
  `payforadoption -> dynamodb` confirmed（1.000，仅吞吐通道）、
  `petsearch -> s3` **refuted**（0.731）—— **本项目第一条被证伪的边**
- 环境收口：两次注入后 NACL 全部还原、零非默认 NACL 残留、Pod 全 Running

### T-295 护栏第三次对准了处理本身 —— 需要 canary 观测方概念 · `todo` ★
- 本轮第三次踩到同一个错误。前两次在注入目标侧（T-214b），这次在**观测方侧**：
  DynamoDB 实验护栏挂 `any_observer` 阈值 `< 20%`，T+~60s 时 `search-service`
  掉到 **0.0%** 熔断，零判定。根因：**petsearch 的主存储就是 DynamoDB，
  这条依赖是全量的**，观测方必然归零 —— 而归零正是这条边成立的证据
- 一句话结论（与 T-214b 同构，只换主体）：
  > 对全量依赖的边，**观测方的成功率不是护栏信号，它就是处理本身**
- 本轮处置：该实验 `stop_conditions: []`，靠 FIS duration 自动回滚 +
  max_duration + 有界影响面三层兜底（实测熔断后 NACL 立刻还原，零残留）
- 待做：引入 **canary 观测方** —— 声明为「预期不依赖注入目标」、只用于护栏、
  不参与判定。本实验没有干净 canary（petsite 经 petsearch 会传导退化，
  list-adoptions 走 Aurora 但列进 observers 会产生一次「无候选边」跳过），
  所以需要规格层面新增 `canaries:` 段而不是复用 `observers:`
- 依赖：无

### T-296 ssm / sts / xray 那 7 条边的替代验证路径 · `todo`
- FIS 两条路都走不通（见 T-294）。可行替代，按侵入性从低到高：
  1. **Chaos Mesh NetworkChaos + `externalTargets`** —— 从调用方 Pod 按域名做
     L3/L4 分区（如只拦 `ssm.ap-northeast-1.amazonaws.com`）。单服务粒度、
     不需新建基础设施、且不依赖 DNS 缓存行为
  2. **Chaos Mesh DNSChaos + `patterns`** —— 只对特定域名返回错误。更轻，
     但 SDK 启动时解析一次即复用连接，长连接场景会假阴性
  3. **新建 interface VPC 端点后再用 `disrupt-vpc-endpoint`** —— 能用用户指定的
     动作，但要真花钱（每端点每 AZ 约 $10/月）**且改变了被测依赖路径本身**
     （NAT/公网 → PrivateLink），验证对象就不是原来那条边了。不推荐
- 推荐 1。**刻意不选 3**：为了让某个工具用得上而改变被测系统，是本末倒置
- 依赖：无

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

### T-270 部署四个 ETL 函数代码 · `done`（2026-08-31 08:57Z，用户批准）
- 层 **`:7` → `:8`**（`:7` 是我的失误产物，见 T-214g）；四个函数代码全部更新
- 逐个实跑验证：aws 310 节点/462 边、deepflow 5 节点/7 边、cfn 6 deps、xray 35 节点/28 边
- **门禁违约 0 条** —— 契约 enforce + 新增 source 词表门禁均未拦住任何合法写入
- 已就绪：层 `:6` 已发布，四个函数**均已指向 `:6`**（顺带修掉了 xray 在 `:5`、其余 `:2` 的既存漂移）
- 未生效的修复都在函数包里：`find_vertex_by_name` 两参数签名、契约写入门禁
- 回退：设 `GRAPH_CONTRACT_MODE=warn` 即降为只告警，不必回滚代码
- 循环该做到：打好包 + 一条可执行命令 + 验证层就绪。**不执行**

### T-271 清错源边 · `done`（2026-08-31 08:58Z）
- 实际清了 **211 条**（不是 183 —— 部署前又累积了 28 条），5 种形态：
  `K8sService->Pod` 79、`Namespace->Pod` 65、`Deployment->Pod` 57、
  `Deployment->K8sService` 6、`Deployment->Deployment` 4
- **根因修复已在生产确认有效**：清完后再跑一整轮 aws-etl，违约仍为 **0 条**
  （这才是验收标准，光删不算 —— 旧代码下一轮就会重建）
- `Microservice-[RunsOn]->Pod` 从 36 条涨到 **42 条**
- 命令：`infra/fix_wrong_source_edges.py --apply`
- **必须在 T-270 之后**：旧代码下一轮 ETL 会原样重建，现在清等于白删
- 数据：`Microservice -[RunsOn]-> Pod` 正确的只有 36 条，错源 173 条（83% 错）。
  其中 108 条经 `_K8S_SVC_ALIAS` 补全后可转为正确边，剩 65 条
  （`Namespace chaos-mesh/deepflow`）本就不该有边

### T-272 开启边过期收敛 · `done`（2026-08-31 10:17Z）
- 已设 `GRAPH_EDGE_EXPIRY_ENABLED=true`（etl_aws，唯一调用执行器的函数）
- **实测 0 条待翻转，且这是正确结果**：那 14 条超期边**已全是 `active=False`** ——
  被 etl_deepflow 自己的 `reconcile_calls_edges`（Calls 阈值 1800s）提前处理掉了。
  执行器的两个查询都带 `.has('active', true)`，所以幂等、不会重写已 false 的边
- 判据有效性反证（cutoff=now 等价 TTL=0）：`AccessesData` 20 条 / `Calls` 5 条 /
  `DependsOn` 1 条活跃动态边**都能被匹配到**，超期 0 条是因为它们在被持续刷新
- **真正价值在覆盖 `AccessesData`/`DependsOn`/`InvokesVia`/`PublishesTo` 四类**——
  此前无任何源会把它们置 false（deepflow 只管 Calls，xray 只管 source='xray'）
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

### Cycle-4 (2026-08-30 19:18Z) — T-210 完成：目标 A 的最后一寸接上了
- **runner 现在真的在检验边，而不是检验注入目标自己。** 三角色标注落地
  （`ObservationTarget`），Phase1 采观测方基线、Phase3 逐点采样、
  收尾 `_verify_edges` 按 `candidate_edges` 的 `observer` 一一对应写回
- 两个设计选择都记了理由：注入期请求量取窗口**最大值**而非求和（窗口重叠会虚高，
  取 max 与基线同量纲、真零流量仍为 0，偏向「判不了」而非「判边不存在」）；
  缺基线或缺采样时退化率返回 **None 而非 0.0**（0.0 会被读成「没退化」→ refuted）
- **零条写回成功时显式报 error** —— 那正是历史上被静默吞掉 21 次的症状
- 新测试 `tests/test_37_observer_metrics.py` **12 条全绿**，o06 是核心：
  零流量必须 `inconclusive`、绝不 `refuted`
- 示例实验 `chaos/code/experiments/tier1/verify-edges-into-search-service.yaml`：
  用 **2026-08-29 实测确有流量**的两条边（list-adoptions 12,236 / petsite 2,028+1,014 请求），
  `http_chaos abort` 而非 delay，含爆炸半径。解析实测正确识别 2 个观测方
- 顺带查明 **T-221 的数据完整性门在注入目标侧已存在**（`result.py` 的 `has_real_metrics` /
  `is_conclusive` / `data_quality`）—— 那张卡是**扩展到观测方**，不是从零建
- 测试 443 → **455 passed / 0 failed**；`verify_dod.sh` PASS 7 → **8**，DoD-3 的 3.1/3.2 转绿
- 下一张：**T-214**（跑通第一条边的真实验证）—— 入口 YAML 已就绪。
  **但 `kubectl` 不在 PATH**（cycle-2 实测），真实注入前必须先解决这个环境问题

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
