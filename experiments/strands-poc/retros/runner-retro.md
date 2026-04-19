# Chaos Runner Migration Retrospective

> 作者：编程猫
> 完成日期：2026-04-19
> 目标读者：架构审阅猫（写 Module 6 DR Executor TASK 时读）

## 1. 模块信息

- Module: chaos-runner (Phase 3 Module 5)
- Duration: planned 3w, actual 1d（大乖乖豁免 Gate A 等待期）
- PRs: 6/7 (merged), retro = 7th
- Start Commit: `e881a30` → End Commit: `97cff58`
- Golden baseline: Direct 5/5+1skip, Strands 5/5+1skip (L1 6 cases)
- Cache: Strands l1-001 cache_read=95,988 / l1-002 cache_read=25,363
- System prompt: 1169 tokens (cache eligible ✅)

## 2. TASK 质量反馈（给架构审阅猫的直接输入 ★★）

### 2.1 让我困惑过的描述
- §11 L1 cases 描述了 6 个 case 的预期，但没有定义 mock tool 的返回值格式。我需要自己设计 `observe_metrics` 返回 85% success_rate、`check_stop_conditions` 返回 `{breached: bool}` 等 JSON 格式。建议下个 TASK 给出 mock tool response schema。
- §6 说"一次实验内 5 phase 复用"但 7-phase protocol 有 phase0-phase5 共 6 个 phase。Phase numbering 有歧义（0-indexed vs 1-indexed count）。

### 2.2 漏了应该提醒的陷阱
- **Experiment 对象属性路径**: `exp.fault.type` 不是 `exp.fault_type`，`exp.fault.duration` 不是 `exp.duration`。这导致 PolicyGuard 在 runner 内拿到 `"unknown"` 并 deny 所有实验。TASK 没提醒 Experiment 数据模型的嵌套结构。
- **StopCondition.threshold 是字符串** `"< 90"` 不是数字 `90`。直接做 `current < cond.threshold` 是 float vs string 比较，Python 3 会 TypeError。必须用 `StopCondition.is_triggered(MetricsSnapshot)` 方法。
- **ExperimentResult 动态属性**: `log_collection` 只在某些 phase 被 set，直接 `result.log_collection` 会 AttributeError。需要 `getattr` 保护。

### 2.3 多余或过严的约束
- 无。§7 安全专题的约束全部合理，特别是 dry_run 双重 gate。

### 2.4 下个 TASK 建议加的字段
- **数据模型速查表**: Experiment / ExperimentResult / StopCondition / MetricsSnapshot 的关键属性路径，避免猜测嵌套结构
- **Mock tool response 的 JSON schema**: 每个 tool 返回什么字段

## 3. 技术教训

### 3.1 踩到的坑（编号与对后续模块的建议配对）

- **坑 1: Experiment 属性嵌套** — `exp.fault.type` 不是 `exp.fault_type`。→ 建议 Module 6: 开始前先 `print(dir(exp))` 和 `print(vars(exp.fault))` 确认属性路径。
- **坑 2: StopCondition threshold 解析** — threshold 是 operator+number 字符串，不能直接比较。→ 建议 Module 6: 复用 `StopCondition.is_triggered()` 而不是自己解析。
- **坑 3: Direct engine L1 测试需要真实集群** — DirectRunner 委托给 ExperimentRunner，即使 dry_run=True 也会在 _phase0_preflight 检查 Pod 存在性。→ 建议 Module 6: 如果 DR Executor 也检查真实资源，L1 测试需要做 "cluster-not-available → acceptable" 的 fallback。

### 3.2 Golden Set 构建经验
- L1 6 cases 设计耗时 ~20min，最难的是 l1-002（stop condition abort）— 需要确保 mock tool 返回触发 threshold 的数据
- 行为约束式 golden（只验 status，不验具体 reasoning text）效果极好 — Strands Agent 每次输出不同的详细分析，但 status 稳定一致
- `must_not_include` 验证在 Runner 模块没用到（status 已经够了）

### 3.3 Prompt Caching
- Strands l1-001: cache_read=95,988（system prompt 被 Bedrock 缓存）
- l1-002: cache_read=25,363（较短的 interaction）
- System prompt 1169 tokens > 1024 最低要求 ✅
- 新 Agent 实例每次调用仍然享受 Bedrock 端的 prompt cache（5 min TTL window）

### 3.4 Strands SDK 使用失败点
- 无严重失败。7 个 tool 的 ReAct loop 稳定，Strands Agent 正确地走完 phase0→phase5。
- l1-001 做了 19 个 tool call（6 轮 observe + check_stop），l1-002 做了 7 个 tool call（1 轮观察就 abort）— Agent 对 stop condition 的反应非常敏锐
- l1-003 (FIS backend) 也正确执行，Agent 识别出不同 backend 并调整了 report 格式
- 无循环调用 / 无不该调的 tool

### 3.5 图谱依赖（Neptune 查询等）
- Runner 模块不直接查 Neptune，但 observe_metrics tool 可以间接触发
- 测试用 mock tool，不涉及真实 Neptune 查询

## 4. 跨猫协作

- sessions_send 消息格式够用。架构审阅猫及时发了 TASK 和新规则。
- TASK 路径准确（`experiments/strands-poc/TASK-phase3-chaos-runner.md`）
- 大乖乖豁免 Gate A 等待期，加速了交付
- 新增 PR 级汇报规则（21:17）— 已落实
- L1 设计在本模块首次使用，验证了"mock tool + 行为断言"模式可行

## 5. 时间 / 成本

- 最耗时阶段：L1 Golden 调试（3 个 bug 串联 — fault_type 提取 → stop_condition 解析 → log_collection 属性）
- 可并行：PR1-3 的 DirectRunner 和 StrandsRunner 可以并行开发（它们的依赖只有 RunnerBase）
- 总 Bedrock 测试成本：~$2-3（L1 Golden 6 cases × 2 engines × 多次调试运行）
- Strands 单次 L1 case 平均 latency: 50-110s（主要是 tool call 轮数多）

## 6. 给下个模块的 Top 3 建议 ⭐⭐⭐

1. **先搞清数据模型再写 tool integration**
   Runner 模块 3 个 bug 全部是因为不了解 Experiment/ExperimentResult/StopCondition 的属性结构。Module 6 (DR Executor) 也会操作这些对象。建议 TASK 里加一个"数据模型速查"小节，列出关键属性路径。开工前先 `python3 -c "from runner.experiment import *; print(Experiment.__dataclass_fields__.keys())"` 确认。

2. **用已有的 `is_triggered()` / `describe()` 方法，不要自己解析 threshold**
   StopCondition、SteadyStateCheck 等 dataclass 都有解析方法。自己解析 `"< 90"` 字符串既容易出错，又重复造轮子。DR Executor 如果需要判断恢复状态，应该复用 `MetricsSnapshot.get()` + `SteadyStateCheck` 的判断逻辑。

3. **L1 Golden 用行为断言（status），不要验证 Agent 输出的文本**
   Strands Agent 每次生成不同的分析文本，但 status（PASSED/ABORTED/FAILED）稳定一致。DR Executor 的 golden 测试也应该只验 `result.status` + `result.recovery_status`，不要验 `reasoning` 字符串。

## 7. 给架构审阅猫的写 TASK 模板修改建议

- **§11 Golden Cases 加 mock tool response schema**: 每个 tool 的返回 JSON 格式应该在 TASK 里定义，而不是让编程猫自己设计。
- **新增"数据模型速查"小节**: 列出 Module 用到的所有 dataclass 的关键属性路径（含嵌套），避免猜测。
- **Phase numbering 统一**: 要么 phase0-5（0-indexed），要么 phase1-6。Runner TASK 里混用了"5 phase"和"phase0-phase5"。

---

*架构审阅猫在写 Module 6 DR Executor TASK 前必须 read 本文件，并在 TASK 的相应章节落实 § 6 Top 3 建议。*
