# Roadmap

阶段**顺序有依赖**，但同阶段内的卡可以任意顺序做。选卡规则见 §选卡优先级。

---

## Stage 0 —— 卫生与基线（无风险，先清干净）

清掉会干扰后续判断的噪声，让"失败集为空"成为可信的验收信号。

- 删 `chaos/=23.0.0` 异常文件（`pip install structlog=23.0.0` 误重定向产物）
- 处理过期冻结注释 `hypothesis_direct.py: delete_date: 2026-08-18`（已过期 12 天）
- Strands 死代码：`strands` 包未安装、`requirements-dev.txt` 里被注释、无启用开关，
  工厂只能回退 direct —— 决定「删」还是「明确标注为未启用实现」，不要留薛定谔状态
- 补 `etl_deepflow` datastore-flow 链路 7 个零覆盖函数的守门测试
  （`_resolve_datastore_ips` CC≈40、`fetch_datastore_flows`、`upsert_datastore_flows`、
  `_get_eks_k8s_session`、`ch_query_json`、`get_aws_session`、`_first_scalar`）
- 补 `etl_xray` 的 `deactivate_stale_xray_edges` 测试（**软删除逻辑本身没有测试**）

**出口**：`python3.11 -m pytest` 全绿且 ≥ 442；`ls chaos/=23.0.0` 不存在。

> ⚠️ 初版这里有一条「Strands 死代码定性（删除或标注未启用）」——**方向写反了，已删除**。
> 见 north_star §1.5：Strands 是硬约束下的既定实现路径。相关工作移到 Stage 0.5。

---

## Stage 0.5 —— 把 Strands 跑起来（所有 LLM 工作的前置）★

现状不是"实现缺失"而是"**依赖未装 + 开关未切**"：19 个文件 / 4583 行 strands 实现已存在，
6 个引擎开关齐备，但 `strands` 包不在环境里，`requirements-dev.txt:56` 被注释，
六个工厂全部**静默**回退 direct。

- 安装 `strands-agents>=1.36`、`strands-agents-tools>=0.5`（PyPI 现值 1.54.0 / 0.8.7，同主版本线）
- 取消 `requirements-dev.txt:56` 的注释并写明版本下界
- 六个引擎开关默认值改为 `strands`，逐个实测跑通
- 13 处「回退 direct」分支保留，但必须**显式告警** ——
  静默回退会让"约束已满足"和"依赖没装"看起来一样（同 north_star §4 不变量 14）
- 加一条能力对账测试：LLM 新能力不得只存在于 direct 侧
- 保持 Bedrock inference profile（`global.*`）形态，显式 `ap-northeast-1`（现存代码已如此）

**依赖**：无。**这是 Stage 4 的硬前置**，也应尽早做——越晚切换，direct 侧越容易长出
strands 侧没有的能力，迁移成本越高。
**出口**：DoD-9 前四项。

---

## Stage 0.6 —— 接入 AgentCore

东京区四项能力**全部可用**（Runtime microVM ✓ / Runtime Instances ✓ / Memory ✓ / Gateway ✓），
所以"区域不支持"不能作为不接的理由。SDK：`bedrock-agentcore 1.22.0`、
`bedrock-agentcore-starter-toolkit 0.3.12`。

按对本项目的价值排序，**至少接一项**：

1. **Gateway** —— 把图查询（`candidate_edges`、爆炸半径、SPOF）与注入能力暴露为 MCP 工具。
   收益最直接：现在这些能力锁在 Python 进程和 VPC 里、对外零契约（审计报告里
   "agent 可调用性 ~55%" 的扣分点正是这个）。
2. **Memory** —— 跨轮记忆。目标 B 的"上一轮结果影响下一轮候选"（T-251）本来要自建，
   Memory 是托管等价物。东京区支持跨区推理。
3. **Observability** —— agent 轨迹。直接服务 DoD-6 的幻觉率统计，
   否则要自己埋点收集 LLM 决策轨迹。
4. **Runtime** —— 托管 agent 执行。与 Stage 5 的调度相关，但 EventBridge + Lambda 已够用，
   优先级最低。

**不接的项必须在 tasks.md 写明技术理由**，不能默认跳过。
**依赖**：Stage 0.5（先有 strands agent，才谈得上放进 AgentCore）。
**出口**：DoD-9 第五项。

---

## Stage 1 —— 边验证闭环接进 runner（目标 A 的最后一寸）★ 最高杠杆

`edge_verification.py` 已写好并对活图谱跑通，但**没有接进 runner 的相位流程**。
runner 现在只采集注入目标自己的指标 —— 「打断 B 之后 B 是否退化」近乎恒真，没有检验任何边。

- `_phase1_steady_state_before` / `_phase3_observe` 同时采集**观测方**（调用侧）SLI
- 实验规格带上三角色标注（参考仓库 `assessment-output-spec.md` §2.5）：
  Injection Target / Observation Target / Impact Target —— 这个三元组本质就是一条边的断言
- 观测方基线请求量下限校验：不足则判 `inconclusive`，不判 `refuted`
- 写回路径修正：只写**被检验的那些边**（在 B 注入 → 只更新 `* -> B` 的入边），
  当前实现写给注入目标的所有出边和入边，等于凭空伪造验证证据
- 边属性写回去掉 `property(single, ...)`（见 north_star §4 不变量 2）

**依赖**：Stage 0 出口（否则分不清失败是谁造成的）。
**出口**：DoD-3。

---

## Stage 2 —— 判定要有分辨力（否则 Stage 1 的结论不可信）

72 个历史实验**全部 `passed`**、零失败 —— 单一退化率阈值没有分辨力。

- 统计判据：control/experiment 双组 + Mann-Whitney U 聚合分数（Netflix Kayenta 范式，
  这是"稳态自动判定"最成熟的公开范式），输出 pass / fail / 需人工
- 数据完整性门（参考仓库 `workflow-guide.md` §6.0.5）：
  无 baseline / 无指标数据 → `OBSERVED (not validated)`，**禁止** `PASSED`
- 注入时长 > 熔断打开窗口 + 重试预算耗尽。微软官方文档明确重试+熔断组合会让
  「下游已失败」在上游观测不到（Circuit Breaker Pattern）
- 用 `HTTPChaos` 的 `abort`（直接短路连接）而非只用 `delay`，绕过部分缓存掩盖

**依赖**：Stage 1（先有观测方数据，才谈得上双组比较）。
**出口**：DoD-7。

---

## Stage 3 —— 把证伪能力扩到 AWS 侧的边 + 图查询驱动选边

54 条 `AccessesData` 边指向 Aurora / DynamoDB / StepFunctions —— Chaos Mesh 碰不到这些。

- **FIS 原生网络故障只能到 subnet/NACL 粒度，无法验证单条边**：在一个子网注入会同时打断
  该子网所有依赖，观测不到"是 A->Redis 断了还是 A->DynamoDB 断了"。
  解法照抄参考仓库 `fis-templates/redis-connection-failure/`：
  **用 SSM Automation 改 Security Group inbound 规则做手术刀式单边隔离** ——
  只断 `app->Redis`，不动 `app->DynamoDB`。这精确对应图里一条边。
  **这是整个参考仓库对本项目价值最高的一项**，没有它逐条边证伪只能停在方法论层面。
- 选边由**图查询**驱动，不由 LLM 看拓扑描述猜：爆炸半径、SPOF、`verify_status=untested`、
  `drift_status=declared_not_observed` 优先。参考仓库的 `aws-resilience-modeling` 用
  Mermaid + `ID->ID` 字符串、SPOF 全靠 LLM 人工推理 —— 方向应该反过来。
- 省钱做法（arXiv:2506.11176）：先用图仿真估计可用性，**只对仿真与观测冲突的边跑昂贵的 live 实验**。

**依赖**：Stage 2（判据可信之后才值得扩大注入面）。
**出口**：DoD-4 的 P0 部分（22 条候选全是 `AccessesData` 到 DB）。

---

## Stage 4 —— 约束 LLM，并给出可量化的可靠性数字

> ⚠️ 初版把这一阶段定位为「给 `hypothesis_direct` 打补丁」——**那是在应急回退路径上加固**。
> 按 north_star §1.5，改法调整为：**约束落在 Strands 侧**，用 `@tool` 的类型签名与
> 结构化输出承担 schema 校验，而不是在 direct 里手写 `_extract_json` 的后置检查。
> direct 侧只做**最低限度**的同步修补（去掉静默兜底），不在那边建新能力。

现状是幻觉防护的直接缺口：`hypothesis_direct` 只做 `_extract_json` + 逐字段 `.get(默认值)`，
没有 schema 校验，`target_services` 不与拓扑对账，`fault_type` 匹配不到时**默认填 `pod_kill`**，
排序分数缺失静默填 5 分。

- **Strands 侧**：用 `@tool` 类型签名 + 结构化输出约束假设结构；
  `target_services` 与 `fault_type` 做成**工具入参校验**，非法输入在工具边界就被拒 ——
  这比生成后再校验更强，因为模型拿不到非法选项
- **direct 侧**：仅去掉三处静默兜底（填 `pod_kill`、填 5 分、不对账拓扑），不加新功能
- `fault_type` 必须在 `fault_catalog.yaml` 内；LLM 可见故障 **9 → 60**
- **自建评测**：golden 集给出假设命中率 / 幻觉率。ChaosEater（NTT, arXiv:2511.07865, ASE'25 NIER）
  是唯一有完整闭环的公开系统，但它的验证是「由人类工程师和 LLM 定性验证 CE 周期是否合理」，
  **没有量化的假设正确率或幻觉率** —— 这是公开文献的空白，谁先给出数字谁就有话可说。
  AgentCore Observability 的 agent 轨迹可直接用作这个统计的数据源（见 Stage 0.6）
- 反面证据一致，划清 LLM 边界：ReAct 幻觉会污染后续结果（arXiv:2502.08224）；
  LLM 直读高 volume 遥测超出上下文容量、推理失败被多 agent 管线掩盖（arXiv:2601.22208）；
  MAST 指出早期一次推理错配会复合放大。
  → LLM 做假设生成与排序、代码脚手架、结果归纳；**不做**稳态最终判定、不直读原始遥测。

**依赖**：**Stage 0.5**（必须先让 strands 成为运行路径，否则约束加在没人跑的代码上）。
**出口**：DoD-6。

---

## Stage 5 —— 定期演练（目标 B）

全量 grep 无 EventBridge / cron / Step Functions / scheduler。
`action_scheduler.py` 只是复合实验内部的时序编排，**不是**调度。目前"定期"靠人工敲 CLI。

- EventBridge Scheduler / Rule → suite 执行入口，**先建为 `DISABLED`**
- learn 产物自动回灌下一轮假设生成（读上一轮 `verify_status` 与覆盖快照）
- 最小权限 IAM 角色（禁止照抄参考仓库的 `AmazonEC2FullAccess` / `ALLOW_WRITE_OPERATIONS=true`）
- stop-condition 告警一律 `--treat-missing-data notBreaching` ——
  否则实验启动初期无数据会误触发，这个坑一定会踩

**依赖**：Stage 2 + Stage 3（无人值守注入的前提是判据可信、爆炸半径可控）。
**出口**：DoD-5。

---

## Stage 6 —— 图谱契约剩余项（P1-2 未完部分）

- 属性级权威表补全：`(类型, 属性) -> [(来源, 优先级, 是否受保护, 陈旧后接管)]`。
  已做完的只是 `source` 写一次；ServiceNow IRE 三个可抄机制里
  **「被拒写入要明示」（`maskedAttributes`）和「null 单独控制」还没做**。
- 节点侧 scoped cleanup：Cartography 的 `scoped_cleanup` 限制删除半径、`cascade_delete` 一层、
  `firstseen` 只设一次。**多账号 / 多 VPC 扩展之前必须有，否则会互删。**
- 显式区分 Simple Relationship Pattern 与 Composite Node Pattern
  （`Microservice.az` 曾累积成两个值，本质就是把 Composite 当 Simple 写）
- "声明了但四个写入 ETL 都不建"的类型对账：`RDSInstance`、`NeptuneCluster`、`NeptuneInstance`、
  `Incident`（rca 写）、`ChaosExperiment`（chaos 写）、`TopologyChange` —— 归类为
  「其他模块写」还是「声明冗余」，别让 33 vs 31 的差值继续悬着

**依赖**：无强依赖，随时可插。优先级低于 Stage 1-5。

---

## Stage 7 —— 部署与数据清理（需用户批准，见 north_star §6）

顺序**不可颠倒**：

```
① infra/migrate_identity_keys.py --apply     # 已跑过，保持撞车为 0
② --audit 复核
③ 发布层 + 四个函数指过去                     # 已完成，四者均在 :6
④ 部署四个 ETL 函数代码                       # 待批准 ← 当前卡在这
⑤ infra/fix_wrong_source_edges.py --apply    # 必须在 ④ 之后
⑥ GRAPH_EDGE_EXPIRY_ENABLED=true             # 让边过期收敛真正生效
```

回退不必回滚代码：设 `GRAPH_CONTRACT_MODE=warn` 即降为只告警。

**出口**：DoD-1、DoD-2。

---

## Stage 8 —— 闭环：用 chaos 实际校正这张图 ★ 这才是终点

用户 2026-08-30 追加的要求：**chaos 模块建成后，要用它验证当前的依赖关系，不符合的就去改。**

Stage 1-3 只做到"得到判定"，这一阶段要求判定**回流成修正动作** ——
否则验证只是产出报告，图还是那张图。

- 对 94 条依赖边成批跑验证（按 T-231 的爆炸半径 / SPOF / untested 优先级排队）
- 每条 `refuted` 的边**必须归因**，三类之一，不允许悬空：

  | 归因 | 含义 | 修正动作 |
  |---|---|---|
  | 幽灵边 | 源头 ETL 造出的假边 | 置 `active=false` **且**修掉造它的 ETL 代码路径 |
  | 观测盲区 | 边真实存在但采集看不见（如 DeepFlow 未解 DB 协议） | 边保留，改采集侧或 `drift_status` 语义 |
  | 弱依赖 | 边存在但非 load-bearing | 保留，标注影响强度，**不删** |

- **删边的门槛必须高**：观测方基线请求量达标 + 注入时长穿透熔断/重试 + `verify_refute_count >= 2`。
  一次 refuted 不足以删边 —— 把真实边判成不存在比不验证更有害（north_star §4 不变量 7）
- 修正**双向留痕**：图上 `verify_reason`，代码侧提交信息带边的三元组，两者能相互指认
- 归因分布进交接文档：多少幽灵边 / 多少观测盲区 / 多少弱依赖 ——
  **这个分布就是"这张图有多准"的量化答案**
- 已知的高价值靶子：22 条 P0（`declared_not_observed`，CFN 声明但从未观测到的 DB 依赖）
  必然落进"幽灵边"或"观测盲区"，**两种都是真问题**，分清即有价值

**依赖**：Stage 1（闭环跑通）+ Stage 2（判据可信）+ Stage 3（AWS 侧边可注入）。
**出口**：DoD-10。

---

## Stage X —— 已登记但不在 north star 内（不要主动做）

- `run_etl` 1234 行 / CC≈325 拆分；2 份字节级相同的 Neptune 客户端 + 1 份分叉实现；
  4 套手写 upsert；ECR 镜像名解析重复 2 处、ARN 归名散落 6 处、时间窗各自计算 7 处
- 连接池假阴性的 DeathStarBench 对照实验（可发表的业界空白，已有两组本地实测数据）
- SQS 主队列积压告警缺失（现有告警只盯 DLQ）
- `search-service` 加 `topologySpreadConstraints` 做跨 AZ 根治
- `list-adoptions -> search` keep-alive：`repository.go:125` 用 `http.DefaultTransport`
  （`MaxIdleConnsPerHost` 回落到 2），且 `resp.Body` 从未 `Close()`；
  更大的杠杆是那个 **12.4 倍扇出**（search 支持一次返回全量）
- `awesomeshop` 6 个 Deployment 全 `0/0`，计算停了数据层可能仍计费

---

## 选卡优先级

1. **先关 DoD 的卡，压过 backlog 卡。** 一张不在任何 DoD 里的卡，无论多容易都往后放。
2. 阶段内优先**无外部依赖、无生产风险**的卡。
3. 同等条件下优先**能产生可验证数字**的卡（数字能驳倒或确证判断，文档不能）。
4. 一张卡卡住 3 轮 → post blocker 一次 → 置 `blocked` → 换卡，不要空转重试。
