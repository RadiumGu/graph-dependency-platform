# North Star —— 依赖图谱可证伪化 + 混沌工程闭环

> 建立于 2026-08-30。锚定分支 `fix/graph-single-source-of-truth`，基线提交 `3477896`。
> 这个文件是**不变目标**。roadmap 和 tasks 可以随时改，这里不行。
> 改动本文件需要用户明确指示，并在 tasks.md 记录改动理由。

---

## 1. 目标（一句话）

让这张依赖图谱成为**唯一能证伪自己的依赖图谱**：每条依赖边的存在性都能被一次故障注入实验确证或推翻，
并且这套证伪能力按计划自动重复运行，而不是靠人工敲 CLI。

拆成两个用户明确给出的子目标：

- **目标 A —— 验证图数据库里的依赖关系**：对边 `A -[X]-> B`，在 **B** 注入故障、观测 **A** 的 SLI；
  A 显著退化 => 边被确证（并得到影响强度）；A 无变化 => 边存疑（幽灵边或非 load-bearing）。
- **目标 B —— 定期进行混沌工程**：实验选择由图查询驱动（爆炸半径 / SPOF / 未验证边优先），
  按计划自动执行，结果回灌图谱与下一轮假设生成。

**战略依据**（这是对外叙事的主轴，不是修辞）：Datadog / Dynatrace / New Relic 都有依赖图，
但都**没有故障注入后端**，做不了证伪。arXiv:2506.11176（ICSE'26 NIER）亲口承认其局限是
「我们假设已知所有依赖边；漏掉一条边会导致模型高估韧性」——反用它就是本项目的差异化。

---

## 1.5 技术约束（用户 2026-08-30 明确指定，不可协商）

> **LLM / agent 开发一律走 Strands Agents，并尽量用 Amazon Bedrock AgentCore。**

这不是偏好，是硬约束。它推翻了本文件初版的两处判断，已修正：

- 初版把 `*_strands.py` 当「死代码」，给出的选项是「删除或标注为未启用」——**方向完全相反**。
- 初版把 Stage 4 的 LLM 约束工作定位为「给 `hypothesis_direct` 打补丁」——
  那是在错误的实现上加固。正确做法是让 **strands 路径成为默认且唯一新增能力的落点**。

实测事实（2026-08-30，程序化统计，不是印象）：

| 事实 | 数字 |
|---|---|
| 涉及 strands 的第一方文件 | **19 个 / 4583 行** |
| 引擎开关 env | **6 个**：`HYPOTHESIS_ENGINE`、`LEARNING_ENGINE`、`NLQUERY_ENGINE`、`LAYER2_ENGINE`、`POLICY_GUARD_ENGINE`、`CHAOS_RUNNER_ENGINE`（**注意后两个的全名**——初版写成 `GUARD_ENGINE`/`RUNNER_ENGINE` 是错的） |
| 「回退 direct」分支 | **13 处** |
| 目标版本 | `rca/engines/factory.py:36` 写明 `strands-agents>=1.36` + `strands-agents-tools>=0.5` |
| PyPI 现值 | `strands-agents 1.54.0`、`strands-agents-tools 0.8.7`（同主版本线，移植风险低） |
| AgentCore SDK | `bedrock-agentcore 1.22.0`、`bedrock-agentcore-starter-toolkit 0.3.12` |
| AgentCore 东京可用性 | Runtime microVM ✓ / Runtime Instances ✓ / Memory ✓ / Gateway ✓ —— **无区域阻塞** |

所以现状**不是**"实现缺失"，而是**依赖未安装 + 开关未切**：`strands` 包不在环境里，
`requirements-dev.txt:56` 那行被注释，六个工厂全部静默回退 direct。

**推论**：`direct` 实现的定位从"主实现"降为"**依赖不可用时的应急回退**"。
新增的 LLM 能力**只允许**加在 strands 侧；direct 侧只接受 bug 修复，不接受新功能。

---

## 2. Definition of Done（全部 shell 可核验，跑 `./verify_dod.sh`）

> 阅读顺序：**DoD-9 / DoD-10 列在最前**，因为它们是 2026-08-30 用户追加的硬约束
> （Strands/AgentCore 强制、以及"验证完要真的去改图"），后加但优先级最高。
> DoD-1..DoD-8 是初版的八条，编号不变以免引用失效。

### DoD-10 用 chaos 模块实际校正这张图（闭环，用户 2026-08-30 追加要求）

DoD-3/DoD-4 只到"得到判定"。这一条要求**判定必须回流成修正动作** ——
验证出来不符合的依赖关系要去改，否则验证只是产出报告。

- 每条 `verify_status = refuted` 的边都有一个**归因结论**，归为下列之一，不允许悬空：
  1. **幽灵边** —— 源头 ETL 造出的假边 → 置 `active=false` **并**修掉造它的 ETL 代码路径
  2. **观测盲区** —— 边真实存在但采集看不见（例：DeepFlow 未解 DB 协议）
     → 边保留，改的是采集侧或 `drift_status` 语义
  3. **弱依赖** —— 边存在但非 load-bearing → 保留，标注影响强度，**不删**
- **禁止仅凭一次 refuted 就删边。** 必须满足：观测方基线请求量达标、注入时长穿透
  熔断+重试预算（DoD-7）、且 `verify_refute_count >= 2`。
  理由见 §4 不变量 7 —— 把真实边判成不存在比不验证更有害。
- 修正动作必须**双向留痕**：图上有 `verify_reason`，代码侧有对应提交，
  两者能相互指认（提交信息带边的三元组，边属性带 experiment id）
- 至少 **1 条**边完整走完「注入 → refuted → 归因 → 改代码或改图 → 复跑验证转为
  confirmed 或确认为幽灵边」的全流程，并留下前后对比
- 归因分布写进交接文档：多少条是幽灵边、多少条是观测盲区、多少条是弱依赖 ——
  这个分布本身就是"这张图有多准"的量化答案，也是 DoD-4 覆盖率数字的意义所在

### DoD-9 Strands / AgentCore 成为实际运行路径（§1.5 硬约束的验收）
- `strands-agents>=1.36` 与 `strands-agents-tools>=0.5` **已安装**，
  `requirements-dev.txt:56` 那行取消注释并写明版本下界
- 六个引擎开关（`HYPOTHESIS_ENGINE`、`LEARNING_ENGINE`、`NLQUERY_ENGINE`、`LAYER2_ENGINE`、
  `POLICY_GUARD_ENGINE`、`CHAOS_RUNNER_ENGINE`）**默认值为 `strands`**，且各自实测跑通一次
- 13 处「回退 direct」分支保留但必须**显式告警**（回退是应急，不能静默变成常态）
- 存在一条测试断言：**LLM 相关新能力不得只存在于 direct 侧**
  （direct 与 strands 的能力集对账，strands 缺失即失败）
- **AgentCore 至少接入一项并说明取舍**：Runtime（托管 agent 执行）/ Memory（跨轮记忆）/
  Gateway（把图查询与注入能力暴露为 MCP 工具）/ Observability（agent 轨迹）。
  东京区四项均可用，所以"不可用"不能作为不接的理由——不接必须给出技术理由并记在 tasks.md
- Bedrock 模型调用保持 inference profile（`global.*`）形态，显式 `ap-northeast-1`
  （现存 strands 代码已是这个形态，不要退回裸 model id）

### DoD-1 契约门禁全线生效（线上）
- 四个写入 Lambda（`neptune-etl-from-{aws,deepflow,xray,cfn}`）层版本 **≥ 6** 且四者一致
- 四者 `GRAPH_CONTRACT_MODE` 未被设为 `off`/`warn`（缺省即 `enforce`）
- 已部署的函数包内含 `find_vertex_by_name(name, label)` 的**两参数签名**
  （判据用签名而非标记字符串 —— 见 §4 不变量 6）

### DoD-2 活图谱零已知结构缺陷
- 端点三元组违约 **0** 条（当前 183，全部是 `find_vertex_by_name` 造的错源边）
- 身份键撞车 **0** 组（VPC 那组已合并，需保持）
- 平行重复边 **0** 条
- `Microservice -[RunsOn]-> Pod` 占该标签全部边的 **100%**（当前 36/209 = 17%）

### DoD-3 边验证闭环真实跑通
- `chaos/code/runner/runner.py` 在注入相位采集**观测方**指标（`_phase1/_phase3` 引用 `edge_verification`）
- 至少 **1** 条依赖边在真实注入后落到终态：活图谱可查 `verify_status ∈ {confirmed, refuted}`
  且 `verify_by = 'chaos-runner'`、`verify_last` 非空
  （属性名以 `edge_verification.write_verdict()` 实际写入的为准：`verify_status`、
  `verify_confidence`、`verify_last`、`verify_by`、`verify_experiment`、`verify_degradation`、
  `verify_reason`、`verify_confirm_count`、`verify_refute_count` —— 不是 `verified_*`）
- 边属性写回**成功**（不是被 `except` 吞掉）：`chaos_*` / `verify_*` 属性在活图谱上非 0 条

### DoD-4 验证覆盖率达到可汇报水平
- 94 条依赖边中 `verify_status != 'untested'` ≥ **20 条**（当前 0）
- 22 条 P0 候选（`drift_status = declared_not_observed`）中 ≥ **10 条**得到终态判定
- 每条终态判定都附带证据：观测方基线请求量 ≥ 下限，否则只能是 `inconclusive`

### DoD-5 定期演练可自动触发（目标 B）
- 存在 EventBridge Scheduler/Rule 指向 suite 执行入口，且**成功执行过一次 dry-run**
- learn 产物自动回灌：下一轮假设生成读到上一轮的 `verify_status` 与覆盖快照（有测试证明）
- 注入用 IAM 角色是**最小权限**（禁止 `AmazonEC2FullAccess` / `ALLOW_WRITE_OPERATIONS=true`）
- stop-condition 告警全部带 `--treat-missing-data notBreaching`

### DoD-6 LLM 输出受约束且可量化
- `hypothesis_direct` 输出经 schema 校验：非法 `fault_type`、拓扑中不存在的 `target_services`
  被**拒绝**而不是静默填 `pod_kill` / 填 5 分
- LLM 可见故障类型从 **9** 扩到 `fault_catalog.yaml` 全集 **60**（19 chaosmesh + 37 fis + 4 scenarios）
- 有一份 golden 集给出**数字**：假设命中率、幻觉率（拓扑外服务 / 目录外故障的比例）
  —— 公开文献里没有这个数（ChaosEater 只做了定性验证），所以必须自建

### DoD-7 判定有分辨力，且不会造假"已验证"
- 稳态判定改为**统计判据**（control/experiment 双组 + Mann-Whitney U，Kayenta 范式），
  不再是单一退化率阈值（当前 72 个实验 100% `passed`，零失败 —— 说明门槛无分辨力）
- 数据完整性门：无 baseline / 无指标数据只能判 `OBSERVED (not validated)`，**禁止**判 `PASSED`
- 注入时长 > 熔断打开窗口 + 重试预算耗尽（否则真实边被判成不存在，比不验证更有害）

### DoD-8 测试与代码卫生
- `python3.11 -m pytest` 全绿，且通过数 **≥ 442**（基线 `3477896` 的水位）
- Strands 死代码清除或明确启用；`chaos/=23.0.0` 异常文件删除；过期冻结注释（`delete_date: 2026-08-18`）处理
- `etl_deepflow` datastore-flow 链路 7 个零覆盖函数补上守门测试

---

## 3. 明确的非目标

- **不删除、不停用任何 `*_strands.py`。** 见 §1.5：它们是既定实现路径，只是依赖没装。
  本文件初版曾把它们列为可删的死代码，那是错的判断，已作废。
- **不在 `direct` 实现上加新能力。** direct 只修 bug。新功能一律加在 strands 侧，
  否则会把架构重心永久钉在应急回退路径上。
- **不重写 ETL**。`run_etl` 1234 行 / CC≈325 的重构登记在 stage 6，但**不是** DoD —— 它不改变正确性。
- **不接新数据源**。本项目至今 12 个缺陷全部落在粒度错配 / 写了没人读 / 身份不唯一三类，
  没有一个是"少采了数据"。瓶颈在数据契约，不在采集覆盖面。
- **不做 DeathStarBench 连接池假阴性对照实验**。那是可发表的业界空白（登记在 stage X），
  但它验证的是"eBPF 会漏边"这个通用命题，不是本系统的正确性。
- **不给 SQS 孤儿队列补消费者**。已论证：会与 HTTP 同步路径双写重复落库，改变应用语义。
- **不追求让 LLM 做最终判定**。LLM 只做假设生成与排序、实验代码脚手架、结果自然语言归纳。

---

## 4. 运行不变量（违反即回归，每轮都成立）

1. **`python3.11` 是唯一正确解释器**。`python3`（3.9）会产生约 19 条**假失败** + 2 个收集错误，
   那些不是代码缺陷。用 3.9 得出的任何基线都作废。
2. **边属性禁用 `property(single, ...)`**。Neptune 返回
   `400 UnsupportedOperationException: Cardinality specification may not be used with Edge properties`。
   这一条让 chaos 写回路径 100% 失败了 21 次而无人发现。顶点属性则**必须**用 `single`。
3. **层里的 Neptune 客户端读 `REGION`，不读 `AWS_REGION`**。只设后者会得到
   `403 Credential should be scoped to a valid region`。
4. **部署顺序不可颠倒**：`migrate_identity_keys --apply` → `--audit` 复核 → 发布层 + 四函数指过去 → 部署函数代码。
   反了会让 mergeV 在共享身份键的重复节点上行为不确定。
5. **干预权重必须 > 可达到的最大先验**。当前 ±4.0 > 静态 2x1.0 + 观测封顶 1.5 = 3.5。
   否则存在"任何单次实验都无法证伪"的边，而证伪能力正是整件事的立足点。已写成断言，不要放松。
6. **判定部署状态用逐文件 diff 或函数签名，不用标记字符串 grep，也不用 commit 时间戳。**
7. **零流量与中间带（5%~20% 退化）一律判 `inconclusive`，绝不判 `refuted`。**
   `metrics.collect()` 无数据时 fallback `success_rate=100.0 / total_requests=0` ——
   **零流量和健康在指标上完全一样**。
8. **`verify_*` 属性的写入权威只给 `chaos-runner`**。ETL 若能写就等于用先验抹掉后验。
9. **门禁白名单豁免必须带理由**。无理由的豁免等于把门禁关掉（例：`Serves` 是废弃标签清理，非写入）。
10. **不信任声明的计数**。程序化推导 + 与声明数交叉核对。本轮已两次靠这条抓到错：
    正则漏数字导致 26 边只解析出 23；fault catalog 我报过 52，实际 **60**。
11. **不用 `grep -c` 数装饰器/注解**，注释里的匹配会虚高。
12. **混沌注入 IAM 最小权限**。参考仓库的 quick-start 给的是宽权限，它自己也旁注了生产要收紧。
13. **任何 LLM / agent 逻辑的改动只落在 Strands 侧**（`@tool` 定义 + Agent 编排），
    优先用 AgentCore 的托管能力而不是自建等价物。`direct` 侧只接受 bug 修复。
    每次动 LLM 代码前先自问：这个能力在 strands 路径上存在吗？不存在就先在那边建。
14. **回退到 direct 必须留下告警**。静默回退会让"约束已满足"和"依赖没装"看起来一样 ——
    这与不变量 7（零流量和健康长得一样）是同一类错误。

---

## 5. 停止条件

- **全部 DoD 绿** → 创建 `STOP` 并写交接文档。
- **剩余任务全部 blocked 在用户**（生产部署、真实注入授权）→ 创建 `STOP` 并写交接。
- **同一张卡 3 轮无实质进展** → post blocker **一次**，把卡置 `blocked`，换另一张卡。
  同一个 blocker 不重复播报。
- **真实注入产生了预期外的用户可见影响** → 立即 `autonudge_stop` 并报告，不自行恢复。
- **边际价值为负时主动收口**：不要为了"有事做"去执行已论证过不该做的卡片，或制造文档。

---

## 6. 需要用户批准才能执行的动作（循环只准备，不执行）

| 动作 | 为什么要批准 | 循环该做到哪一步 |
|---|---|---|
| 部署四个 ETL 函数代码 | 生产变更 | 打好包、写好一条可执行命令、验证层已就绪 |
| `fix_wrong_source_edges.py --apply` | 改活图谱数据 | dry-run 出准确条数；**必须在函数代码部署之后**，否则旧代码下轮原样重建 |
| 真实故障注入（非生产 EKS） | 有可观测影响 | 实验规格、stop-condition、爆炸半径评估全部就绪，一条命令可跑 |
| 开启 EventBridge 定期注入 | 无人值守注入 | 规则建好但置 `DISABLED`，附启用命令 |
