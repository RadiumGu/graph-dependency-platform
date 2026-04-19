# TASK: Phase 3 Module 6 — DR Executor (Cross-Region DR Plan Execution Engine)

> **任务归属**: 编程猫
> **发起**: 大乖乖 + 架构审阅猫
> **日期**: 2026-04-XX（Gate A 通过后启动）
> **范围**: 新建 `dr-plan-generator/executor_direct.py` + `executor_strands.py`，将现有 DR plan 执行逻辑迁移到 Strands Agent，保持 direct 版冻结
> **预计工作量**: 2 周（migration-strategy Phase 3 Week 19-20）
> **成功标志**: 所有 PR 合入 main + 验收门槛达标 + `executor_direct.py` 冻结 + 2 个完整演练通过
> **前序模块**: Module 1-5 全部冻结
> **⚠️ 本模块特殊性**: Phase 3 **风险最高**的模块 — 跨 region 操作（Route53/Aurora/S3）、影响生产、最后迁移。`dry_run=True` 是不可商量的 factory 默认值。

---

## 0. 必读顺序

1. `TASK-phase3-shared-notes.md`（共享规范，**§ 3.6 是 DR Executor 的缓存设计要点**）
2. `retros/runner-retro.md`（**必读** — § 6 Top 3 建议 + § 7 模板修改建议，直接影响本模块）
3. `retros/policy-guard-retro.md`（**必读** — § 6 Top 3、§ 7 产出清单标注建议）
4. `TASK-phase3-chaos-runner.md`（结构参考 + 最新模板）
5. `migration-strategy.md`（§ 3.3 Phase 3 + § 6.3 切换门槛 + § 6.4 Prompt Caching）
6. **当前模块代码**（必读，理解现有实现）：
   - `dr-plan-generator/models.py` — DRPlan / DRPhase / DRStep / ImpactReport 数据模型
   - `dr-plan-generator/validation/verification_models.py` — StepVerificationResult / PhaseResult / RehearsalReport / ExecutionContext
   - `dr-plan-generator/validation/plan_validator.py` — PlanValidator.validate()
   - `dr-plan-generator/assessment/rto_estimator.py` — RTOEstimator.estimate()
   - `dr-plan-generator/assessment/impact_analyzer.py` — ImpactAnalyzer.assess_impact()
   - `dr-plan-generator/graph/graph_analyzer.py` — GraphAnalyzer 拓扑排序 + 层分类

---

## 1. 工作目录

```
/home/ubuntu/tech/graph-dependency-platform
```

---

## 2. 前置条件（Gate A 确认）

### 不可豁免（必须全部满足）

- [ ] Chaos Runner（Module 5）已冻结，`timeline.md` status = `frozen`
- [ ] Module 1-5 全部冻结且无 P0 回归
- [ ] Bedrock 月度预算充足（DR 演练涉及多 step × 多次 LLM 调用）
- [ ] 大乖乖已确认 2 个完整 DR 演练场景（AZ failover + Region failover）
- [ ] 跨 region IAM role 已配置（source region + target region 都有 Bedrock / Route53 / Aurora / S3 权限）
- [ ] dry-run 专用环境已准备（staging 集群 + 隔离的 Route53 hosted zone）

### 可豁免（大乖乖 Slack 确认即可）

- [ ] Module 5 稳定期 ≥ 4 周（可口头豁免，但需记录）
- [ ] `docs/migration/timeline.md` 中 `dr-executor` 的 `freeze_date` / `delete_date` 已填（可在 Week 2 补填）
- [ ] 大乖乖已打 tag `v-last-direct-dr-executor-YYYYMMDD`（可在 PR-final 时补打）

*不可豁免项任一不满足 → 暂停启动，通知大乖乖。*

---

## 3. 本模块依赖的 env 变量清单

| 变量名 | 用途 | 与其他模块差异 |
|--------|------|---------------|
| `BEDROCK_REGION` | Bedrock 调用区域 | 同前序模块 |
| `BEDROCK_MODEL` | Bedrock 模型 ID | 同前序模块 |
| `DR_EXECUTOR_ENGINE` | 引擎切换 `direct\|strands` | 本模块专用 |
| `DR_EXECUTOR_DRY_RUN` | `true\|false`，默认 `true` | 本模块专用，**必须默认 true** |
| `DR_SOURCE_REGION` | DR 源 region（如 `us-east-1`） | 本模块专用 |
| `DR_TARGET_REGION` | DR 目标 region（如 `us-west-2`） | 本模块专用 |
| `DR_SOURCE_PROFILE` | AWS profile for source region | 本模块专用 |
| `DR_TARGET_PROFILE` | AWS profile for target region | 本模块专用 |
| `ENVIRONMENT` | `staging\|production` | 同前序模块 |

> ⚠️ **sys.path 提醒**：所有 import 用 `from models import ...` 或 `from validation.xxx import ...`，**不用全限定路径**（Module 1-5 反复踩坑）。

---

## 4. 任务范围（严格限定）

**做**：
- 新建 `ExecutorBase` 抽象基类（DR plan 执行流程骨架）
- 新建 `executor_direct.py` — Direct 版（调 Bedrock 辅助 step 执行决策）
- 新建 `executor_strands.py` — Strands Agent 版（单 Agent + N tool，按 plan phase/step 顺序执行）
- factory `make_dr_executor(dry_run=True)` — **dry_run 默认值必须是 True**
- 2 个完整演练的 Golden Set（L1 + L2 两层）
- Prompt Caching 部分缓存集成（拆稳定段 + 可变段）
- Failure strategy 执行（ROLLBACK / RETRY / MANUAL / SKIP / ABORT）
- PlanValidator 联动（执行前 validate，CRITICAL → abort）
- 每次调用返回标准化 dict（含 `engine` / `model_used` / `latency_ms` / `token_usage` / `phase_results` / `step_results` / `failure_log`）

**不做**：
- 不改已冻结的 Module 1-5
- 不做真实 production DR 切换（Golden CI 全部 dry-run 或 mock）
- 不改 `models.py` / `plan_validator.py` / `graph_analyzer.py` 的核心逻辑
- 不做 DR plan 生成（那是现有 `main.py cmd_plan` 的事）
- 不做新的验证规则（复用 PlanValidator）

---

## 5. 数据模型速查表（★ Module 5 retro Top 1 落实）

> Runner 模块 3 个 bug 全因属性路径不对。本节列出 DR Executor 用到的所有关键数据模型和属性路径。

### 5.1 DRPlan（`models.py`）

```python
plan.plan_id          # str — 唯一标识
plan.scope            # str — "az" | "region" | "service"
plan.source           # str — 源 region/AZ
plan.target           # str — 目标 region/AZ
plan.phases           # List[DRPhase] — 正向切换阶段列表
plan.rollback_phases  # List[DRPhase] — 回滚阶段列表
plan.impact_assessment            # Optional[ImpactReport]
plan.impact_assessment.estimated_rto_minutes  # int
plan.impact_assessment.estimated_rpo_minutes  # int
plan.estimated_rto    # int（minutes）
plan.estimated_rpo    # int（minutes）
plan.affected_services  # List[str]
plan.affected_resources # List[str]
plan.validation_status  # str — "pending" | "valid" | "invalid"
plan.graph_snapshot_time  # str — ISO timestamp
```

**⚠️ 反序列化**：用 `DRPlan.from_dict(d)` — 自动处理嵌套 phases/steps/impact。不要手动解析。

### 5.2 DRPhase（`models.py`）

```python
phase.phase_id          # str
phase.name              # str — 如 "Pre-flight", "L0-Infra", "L1-Data", "L2-Compute", "L3-App"
phase.layer             # str — "preflight" | "L0" | "L1" | "L2" | "L3" | "validation"
phase.steps             # List[DRStep]
phase.estimated_duration  # int（minutes）
phase.gate_condition    # str — phase 间 gate 检查条件
```

**⚠️ 反序列化**：用 `DRPhase.from_dict(d)` — 自动处理嵌套 steps。

### 5.3 DRStep（`models.py`）

```python
step.step_id            # str
step.order              # int — 执行顺序
step.parallel_group     # Optional[str] — 同组可并行
step.resource_type      # str — "route53" | "aurora" | "s3" | "eks" | "elasticache" 等
step.resource_id        # str — AWS resource ARN/ID
step.resource_name      # str
step.action             # str — "failover" | "promote" | "replicate" | "switch" 等
step.command            # str — 实际执行的 AWS CLI / kubectl 命令
step.validation         # str — 验证命令
step.expected_result    # str
step.rollback_command   # str — 回滚命令
step.estimated_time     # int（seconds）
step.requires_approval  # bool — 是否需要人工确认
step.tier               # Optional[str]
step.dependencies       # List[str] — 依赖的 step_id 列表
```

**⚠️ 反序列化**：用 `DRStep.from_dict(d)`。

### 5.4 StepVerificationResult（`verification_models.py`）

```python
result.step_id              # str
result.phase_id             # str
result.resource_name        # str
result.command_success      # bool
result.command_output       # str
result.command_exit_code    # int
result.validation_success   # bool
result.validation_output    # str
result.rollback_success     # Optional[bool]
result.rollback_output      # Optional[str]
result.actual_duration_seconds  # float
result.estimated_duration_seconds  # int
result.rto_accuracy         # property: actual / estimated
result.passed               # property: command_success and validation_success
result.issues               # List[str]
```

### 5.5 PhaseResult（`verification_models.py`）

```python
phase_result.phase_id       # str
phase_result.phase_name     # str
phase_result.steps          # List[StepVerificationResult]
phase_result.gate_check_passed  # bool
phase_result.gate_condition     # Optional[GateCondition]
phase_result.all_steps_passed   # property
phase_result.failed_steps       # property → List[str]
```

### 5.6 RehearsalReport（`verification_models.py`）

```python
report.plan_id              # str
report.scope                # str
report.environment          # str
report.actual_rto_minutes   # int
report.estimated_rto_minutes  # int
report.rto_accuracy         # property
report.phase_results        # List[PhaseResult]
report.step_results         # List[StepVerificationResult]
report.rollback_success     # bool
report.rollback_duration_seconds  # int
report.success              # property — all phases passed
```

### 5.7 GateCondition（`verification_models.py`）

```python
gate.type                   # GateType — HARD_BLOCK | SOFT_WARN | INFO
gate.require_all            # bool
gate.auto_retry             # int — 重试次数
gate.retry_interval_seconds # int
gate.timeout_minutes        # int
gate.on_timeout             # str — "abort_and_hold" | "abort_and_rollback" | "continue"
gate.on_failure             # str — "diagnose" | "rollback" | "skip"
```

### 5.8 ExecutionContext（`verification_models.py`）

```python
ctx.aws_region              # str
ctx.aws_profile             # str
ctx.k8s_context             # str
ctx.neptune_endpoint        # str
ctx.role_arn                # str
```

### 5.9 Enums

```python
from validation.verification_models import (
    VerificationLevel,    # DRY_RUN | STEP_BY_STEP | FULL_REHEARSAL
    StepStrategy,         # ISOLATED | CUMULATIVE | CHECKPOINT
    CheckStatus,          # PASS | FAIL | WARN | SKIP
    GateType,             # HARD_BLOCK | SOFT_WARN | INFO
)
```

---

## 6. 现有方法复用清单（★ Module 5 retro Top 2 落实）

> Runner 模块教训：用已有方法，不要自己解析。以下是 DR Executor 必须复用的现有方法。

| 方法 | 文件 | 用途 | 不要自己做 |
|------|------|------|-----------|
| `DRPlan.from_dict(d)` | `models.py` | 反序列化 plan JSON | ❌ 不要手动解析嵌套 phases/steps |
| `DRPhase.from_dict(d)` | `models.py` | 反序列化 phase | ❌ 不要手动构造 DRPhase |
| `DRStep.from_dict(d)` | `models.py` | 反序列化 step | ❌ 不要手动构造 DRStep |
| `PlanValidator().validate(plan)` | `plan_validator.py` | 执行前静态验证 | ❌ 不要自己检查依赖环/完整性 |
| `RTOEstimator().estimate(phases)` | `rto_estimator.py` | RTO 估算 | ❌ 不要自己算 RTO |
| `ImpactAnalyzer().assess_impact(...)` | `impact_analyzer.py` | 影响评估 | ❌ 不要自己算影响范围 |
| `GraphAnalyzer().topological_sort_within_layer(...)` | `graph_analyzer.py` | 拓扑排序 | ❌ 不要自己实现拓扑排序 |
| `GraphAnalyzer().detect_parallel_groups(...)` | `graph_analyzer.py` | 并行组检测 | ❌ 不要自己判断哪些 step 可并行 |
| `StepVerificationResult.passed` | `verification_models.py` | step 通过判断 | ❌ 不要自己检查 command_success and validation_success |
| `StepVerificationResult.rto_accuracy` | `verification_models.py` | RTO 精度 | ❌ 不要自己算 actual/estimated |
| `RehearsalReport.success` | `verification_models.py` | 整体成功判断 | ❌ 不要自己遍历 phase_results |
| `DryRunReport.ready` | `verification_models.py` | dry-run 就绪 | ❌ 不要自己检查 critical checks |
| `DryRunReport.blockers` | `verification_models.py` | 获取阻塞项 | ❌ 不要自己过滤 |

> ⚠️ **开工前确认**：`python3 -c "from models import DRPlan; print(DRPlan.__dataclass_fields__.keys())"` 验证属性名。

---

## 7. 推荐架构

### 7.1 架构图

```
     ┌──────────────────────────┐
     │  main.py cmd_plan        │ ← 生成 DRPlan（已有）
     └───────────┬──────────────┘
                 │ DRPlan JSON
     ┌───────────▼──────────────┐
     │  PlanValidator.validate()│ ← 静态验证（已有）
     └───────────┬──────────────┘
                 │ ValidationReport.valid == True
     ┌───────────▼──────────────┐
     │  ExecutorBase (factory)  │
     │  dry_run=True (default)  │
     └───────────┬──────────────┘
                 │
     ┌───────────▼──────────────────────────────────────┐
     │  Strands DR Executor Agent（单 Agent + N tool）  │
     │  生命周期 = 一次 DR 演练                         │
     │                                                   │
     │  system_prompt:                                   │
     │    [0] STABLE_DR_FRAMEWORK (cached)               │
     │    [1] this_dr_plan + current_state (not cached)  │
     │                                                   │
     │  @tool: execute_step()                            │
     │  @tool: validate_step()                           │
     │  @tool: rollback_step()                           │
     │  @tool: check_gate_condition()                    │
     │  @tool: check_cross_region_health()               │
     │  @tool: request_manual_approval()                 │
     │  @tool: get_step_status()                         │
     │  @tool: abort_and_rollback()                      │
     │                                                   │
     │  Phase: preflight → L0 → L1 → L2 → L3 → valid   │
     │  Per step: execute → validate → gate check        │
     │  On failure: apply failure_strategy               │
     └──────────────────────────────────────────────────┘
```

### 7.2 为什么是 1 Agent + N tool

DR 演练是**顺序执行的 plan phases**，每个 phase 内多个 steps 按拓扑排序执行。与 Runner 一样，phase 间有强依赖，多 Agent 得不偿失。

| 维度 | 多 Agent | 1 Agent + N tool（推荐） |
|------|---------|------------------------|
| 内存 | 6+ Agent 实例 | ~300-500 MB（单实例） |
| 跨 step 上下文 | 需要序列化传递 | Agent 内天然共享 |
| 缓存利用 | 独立缓存池 | 单池，step 间复用 |
| failure strategy | 需要 coordinator | Agent 自然处理 |

---

## 8. Agent 实例生命周期管理

### DR Executor 的生命周期策略：**一次 DR 演练内复用，演练结束后销毁**

| 维度 | 每 step 新建 | 一次演练内复用（推荐） |
|------|-------------|----------------------|
| cache_read | 基本为 0 | step 间可享受 cache_read |
| conversation history | 无累积 | **有价值** — 后续 step 需要知道前面 step 结果 |
| 跨演练污染 | 不存在 | 不存在（演练结束销毁） |

### 实现方式

```python
class StrandsExecutor(ExecutorBase):
    def execute(self, plan: DRPlan, scope: VerificationScope) -> RehearsalReport:
        # 每次 execute() 新建 Agent
        agent = Agent(
            model=self._build_model(),
            system_prompt=self._build_system_prompt(plan, scope),
            tools=[
                self.tool_execute_step,
                self.tool_validate_step,
                self.tool_rollback_step,
                self.tool_check_gate_condition,
                self.tool_check_cross_region_health,
                self.tool_request_manual_approval,
                self.tool_get_step_status,
                self.tool_abort_and_rollback,
            ],
        )
        result = agent(self._format_execution_prompt(plan))
        return self._parse_report(result)
```

### 硬规则

1. **Agent 实例生命周期 = 一次 `execute()` 调用**
2. **不要在 `__init__` 创建 Agent 跨演练复用**
3. **conversation history 在演练内有价值** — step N+1 需要 step N 的结果上下文
4. **如果 history 爆炸（>50k tokens）**，用 `SlidingWindowConversationManager(window_size=20)` 限制

---

## 9. ⚠️ 安全专题（本模块最高关切）

> DR Executor 是整个 Phase 3 风险最高的模块 — 跨 region、影响生产。

### 9.1 dry_run 默认值

```python
def make_dr_executor(dry_run: bool = True) -> ExecutorBase:
    engine = os.environ.get("DR_EXECUTOR_ENGINE", "direct")
    env_dry_run = os.environ.get("DR_EXECUTOR_DRY_RUN", "true").lower() == "true"
    effective_dry_run = dry_run and env_dry_run  # 两个都 False 才真执行

    if engine == "strands":
        from executor_strands import StrandsExecutor
        return StrandsExecutor(dry_run=effective_dry_run)
    else:
        from executor_direct import DirectExecutor
        return DirectExecutor(dry_run=effective_dry_run)
```

### 9.2 PlanValidator 联动

- 执行前必须调 `PlanValidator().validate(plan)` — CRITICAL issue → abort，不执行
- Validator 故障 → **fail-closed**：abort
- 验证结果记录到 RehearsalReport

### 9.3 Failure Strategy 执行（核心机制）

每个 DRStep 失败时，按 failure strategy 决定下一步：

| Strategy | 行为 | 适用场景 |
|----------|------|---------|
| **ROLLBACK** | 执行 step.rollback_command → 中断当前 phase → 触发全局回滚 | Route53 failover 失败 |
| **RETRY** | 重试当前 step（最多 3 次，间隔 30s/60s/120s） | 网络瞬断、API throttling |
| **MANUAL** | 暂停执行，等待人工确认后继续或中止 | requires_approval step |
| **SKIP** | 跳过当前 step，记录 warning，继续下一个 | 非关键验证 step |
| **ABORT** | 立即中止整个演练 → 触发全局回滚 | 不可恢复的错误 |

**⚠️ Failure strategy 如何确定**：每个 step 的 failure strategy 由 step 的 `resource_type` + `action` 决定。建议在 Executor 中维护一个 strategy map：

```python
FAILURE_STRATEGY_MAP = {
    ("route53", "failover"):    "ROLLBACK",
    ("aurora", "promote"):      "ROLLBACK",
    ("s3", "replicate"):        "RETRY",
    ("eks", "switch"):          "ROLLBACK",
    ("elasticache", "failover"):"RETRY",
    # 默认
    "default":                  "ABORT",
}
```

### 9.4 全局回滚机制

```
任何 phase/step 触发 ROLLBACK 或 ABORT
  → 停止正向执行
  → 按 plan.rollback_phases 反向执行回滚
  → 每个回滚 step 也执行 validate
  → 记录 rollback_success + rollback_duration
```

### 9.5 跨 Region 安全约束

| 操作 | 安全要求 |
|------|---------|
| Route53 failover | 切换前验证目标 region health check pass |
| Aurora replica promotion | promote 前验证 replication lag < RPO |
| S3 cross-region replication | 验证 replication status = COMPLETED |
| EKS workload switch | 切换前验证 target cluster 健康 |

### 9.6 requires_approval gate

- `step.requires_approval == True` → 暂停，调 `request_manual_approval` tool
- dry_run 模式下自动 approve（不真暂停）
- production 模式下必须等人工确认

### 9.7 Blast Radius 硬限制

| 环境 | 允许范围 |
|------|---------|
| staging | AZ failover, region failover（隔离环境） |
| production | **仅 dry-run** — Golden CI 和 L1 测试全部 dry-run |

---

## 10. Prompt Caching 专题 — 部分缓存

> shared-notes § 3.6 明确要求拆稳定段 + 可变段。

### 10.1 缓存设计

| 层 | 内容 | 缓存？ | 预估 tokens |
|----|------|--------|------------|
| **稳定段** | 拓扑排序规则 + RTO/RPO 计算公式 + Failure strategy 语义 + Phase 转换规则 + tool 使用规范 | ✅ 缓存 | 4-6k |
| **可变段** | 本次 DR plan 具体 steps + 当前 step 状态 + 前面 step 结果 | ❌ 不缓存 | 1-3k |

### 10.2 拆分实现

```python
"system": [
    {
        "type": "text",
        "text": STABLE_DR_FRAMEWORK,
        "cache_control": {"type": "ephemeral"},
    },
    {
        "type": "text",
        "text": self._format_plan_context(plan, current_state),
        # 不加 cache_control — 每次演练 / 每个 step 不同
    },
]
```

### 10.3 稳定段内容（STABLE_DR_FRAMEWORK）

```
## Role
You are a DR Plan Executor. You execute disaster recovery plans following
a strict phase-by-phase, step-by-step protocol using tools to interact
with AWS services across regions.

## Execution Protocol
1. Pre-flight: Validate plan (PlanValidator), check cross-region health
2. Execute phases in order: preflight → L0 (infra) → L1 (data) → L2 (compute) → L3 (app) → validation
3. Within each phase: execute steps in topological order (respect dependencies)
4. After each step: validate → check gate condition
5. On step failure: apply failure_strategy (ROLLBACK/RETRY/MANUAL/SKIP/ABORT)
6. On phase completion: check phase gate_condition before proceeding

## Failure Strategy Semantics
- ROLLBACK: Execute step.rollback_command → abort current phase → trigger global rollback
- RETRY: Retry step (max 3, exponential backoff 30s/60s/120s)
- MANUAL: Pause execution, call request_manual_approval, wait for human input
- SKIP: Log warning, mark step as SKIPPED, continue to next step
- ABORT: Immediately abort entire rehearsal → trigger global rollback

## Phase Transition Rules
- Phase N+1 starts ONLY if Phase N gate_condition is met
- HARD_BLOCK gate failure → ABORT entire rehearsal
- SOFT_WARN gate failure → log warning, continue (with human override option)
- INFO gate → always continue

## Cross-Region Safety Rules
- NEVER execute Route53 failover without verifying target health check
- NEVER promote Aurora replica if replication lag > RPO threshold
- ALWAYS verify target region connectivity before any cross-region operation
- In dry_run mode, report expected outcomes without actual mutations

## RTO/RPO Tracking
- Track actual_duration for every step
- Compare against estimated_time for RTO accuracy
- Report deviations > 50% as warnings
- Calculate cumulative RTO at phase boundaries

## Tool Usage Rules
- Always call check_cross_region_health before first cross-region operation
- For requires_approval steps, always call request_manual_approval (auto-approved in dry_run)
- If a tool returns error, apply the step's failure_strategy
- abort_and_rollback is the nuclear option — triggers full plan.rollback_phases

## Safety Invariants
- NEVER skip validation after step execution
- NEVER proceed to next phase if HARD_BLOCK gate fails
- ALWAYS execute rollback_phases on ABORT (even in dry_run — for validation)
- In dry_run mode, all AWS CLI commands return mock success results
```

### 10.4 可变段内容

```
## Current DR Plan
Plan ID: {plan.plan_id}
Scope: {plan.scope} ({plan.source} → {plan.target})
Affected Services: {plan.affected_services}
Estimated RTO: {plan.estimated_rto} min
Estimated RPO: {plan.estimated_rpo} min
Total Phases: {len(plan.phases)}
Total Steps: {sum(len(p.steps) for p in plan.phases)}

## Current Execution State
Current Phase: {current_phase.phase_id} ({current_phase.name})
Completed Steps: {completed_step_ids}
Failed Steps: {failed_step_ids}
Dry Run: {self.dry_run}
Environment: {scope.environment}
```

### 10.5 缓存预期

| 指标 | 预估值 |
|------|--------|
| 稳定前缀大小 | 4-6k tokens |
| 每次演练 LLM 调用次数 | 10-20 次（每 step 1-2 次） |
| 稳态缓存命中率 | ≥ 50% |
| 预估成本节省 | 20-35% |

### 10.6 assert_cacheable 强制检查

```python
from engines.strands_common import assert_cacheable
assert_cacheable(STABLE_DR_FRAMEWORK, min_tokens=1024)
```

> ⚠️ 用 `tiktoken.get_encoding("cl100k_base")`，**不用 `encoding_for_model`**。

---

## 11. Strands Metrics 已知限制

> 同 Module 5：Strands `accumulated_usage` 不含 cacheRead/cacheWrite。

方案同 Runner — hook boto3 response 或用 inputTokens 下降作为间接指标。

> ⚠️ converse() API 不返回 cacheReadInputTokens。Direct 版用 `invoke_model`。

---

## 12. 产出清单

### 12.1 抽象层 [必须]

```python
# dr-plan-generator/executor_base.py
from abc import ABC, abstractmethod

class ExecutorBase(ABC):
    def __init__(self, dry_run: bool = True):
        self.dry_run = dry_run

    @abstractmethod
    def execute(self, plan: "DRPlan", scope: "VerificationScope") -> "RehearsalReport":
        """执行完整 DR plan。"""
```

### 12.2 Factory [必须]

```python
# dr-plan-generator/executor_factory.py
def make_dr_executor(dry_run: bool = True) -> ExecutorBase:
    """⚠️ dry_run=True 是不可商量的默认值。"""
```

### 12.3 Direct 版 [必须]

```
dr-plan-generator/executor_direct.py
```

### 12.4 Strands 版 [必须]

```
dr-plan-generator/executor_strands.py
```

### 12.5 Tools [必须]

```python
@tool
def execute_step(step_id: str, phase_id: str, command: str, dry_run: bool) -> dict:
    """执行单个 DR step。dry_run 模式返回 mock 结果。"""

@tool
def validate_step(step_id: str, validation_command: str) -> dict:
    """验证 step 执行结果。返回 {success: bool, output: str}。"""

@tool
def rollback_step(step_id: str, rollback_command: str) -> dict:
    """回滚单个 step。"""

@tool
def check_gate_condition(phase_id: str, gate_condition: str, step_results: list) -> dict:
    """检查 phase gate condition。返回 {passed: bool, gate_type: str}。"""

@tool
def check_cross_region_health(source_region: str, target_region: str, checks: list) -> dict:
    """检查跨 region 健康状态。Route53 health / Aurora lag / S3 replication。"""

@tool
def request_manual_approval(step_id: str, description: str, context: dict) -> dict:
    """请求人工确认。dry_run 模式自动 approve。"""

@tool
def get_step_status(step_id: str) -> dict:
    """获取 step 当前状态。"""

@tool
def abort_and_rollback(reason: str, completed_steps: list) -> dict:
    """中止演练 + 触发全局回滚。"""
```

> ⚠️ **Strands tool 内调其他 engine 必须 direct** — 如果 tool 内需要调 PlanValidator 或其他服务。

### 12.6 Mock Tool Response Schema（★ Module 5 retro § 2.1 落实）

**execute_step 返回**：
```json
{
  "step_id": "step-001",
  "success": true,
  "exit_code": 0,
  "output": "Route53 failover initiated: hosted-zone-123 → us-west-2",
  "duration_seconds": 45.2,
  "dry_run": true
}
```

**validate_step 返回**：
```json
{
  "step_id": "step-001",
  "validation_success": true,
  "output": "Health check HEALTHY in target region",
  "checks_passed": 3,
  "checks_total": 3
}
```

**check_cross_region_health 返回**：
```json
{
  "source_region": "us-east-1",
  "target_region": "us-west-2",
  "healthy": true,
  "checks": [
    {"name": "route53_health", "status": "pass"},
    {"name": "aurora_replication_lag", "status": "pass", "lag_seconds": 2},
    {"name": "s3_replication", "status": "pass"}
  ]
}
```

**rollback_step 返回**：
```json
{
  "step_id": "step-001",
  "rollback_success": true,
  "output": "Route53 reverted to us-east-1"
}
```

**check_gate_condition 返回**：
```json
{
  "phase_id": "phase-l1",
  "passed": true,
  "gate_type": "HARD_BLOCK",
  "details": "All L1 steps passed"
}
```

**request_manual_approval 返回**：
```json
{
  "step_id": "step-005",
  "approved": true,
  "approver": "auto-dry-run",
  "timestamp": "2026-04-20T10:30:00Z"
}
```

### 12.7 完整文件清单

```
dr-plan-generator/
├── executor_base.py        # [必须] ExecutorBase 抽象基类
├── executor_factory.py     # [必须] make_dr_executor(dry_run=True)
├── executor_direct.py      # [必须] Direct 版
├── executor_strands.py     # [必须] Strands 版
├── models.py               # [不改] 现有数据模型
├── validation/             # [不改] 现有验证逻辑
├── assessment/             # [不改] 现有评估逻辑
├── graph/                  # [不改] 现有图谱分析
├── main.py                 # [小改] 加 cmd_execute 子命令
├── config.py               # [可选] 加 dry_run / region 配置
```

### 12.8 测试文件

```
tests/golden/dr_executor/
├── scenarios.yaml           # [必须] 2 个完整演练场景
├── cases_l1.yaml            # [必须] L1 Golden — mock AWS 操作
├── cases_l2.yaml            # [可选] L2 Golden — staging 集成
├── BASELINE-direct.md       # [必须]
├── BASELINE-strands.md      # [必须]
```

### 12.9 其他

```
experiments/strands-poc/
├── verify_cache_dr_executor.py    # [必须] 缓存生效验证脚本
├── retros/dr-executor-retro.md    # [必须] 完成后交付
```

---

## 13. Golden Set — L1/L2 两层设计

> ★ Module 5 retro Top 3 落实：Golden 断言用行为（status），不验文本。

### 13.1 L1 Golden — Mock AWS 操作

**目的**：验证 Agent 的**执行决策质量** — 给定 DR plan，Agent 是否正确按 phase/step 顺序执行、正确处理 failure strategy。

**方法**：mock 所有 AWS 操作 tool（返回预设结果），只测 LLM 的 tool 调用序列和决策。

**Golden 只需 2 个完整演练**（规模大，每个演练 10-15 个 step）：

| ID | 场景 | Steps | 预期行为 | 验证点 |
|----|------|-------|---------|--------|
| l1-001 | AZ failover 演练（正常完成） | ~10 steps, 4 phases | 全部 phase 完成 | tool 调用序列 + phase 转换 + RTO 追踪 |
| l1-002 | Region failover（step 失败 + RETRY + ROLLBACK） | ~15 steps, 5 phases | Phase L1 某 step RETRY 2次后 ROLLBACK → 全局回滚 | failure strategy 执行 + rollback 顺序正确 |

**断言类型**：

- **硬断言**（block CI）：
  - `expected_status`：SUCCESS / ROLLED_BACK / ABORTED
  - `expected_phases_completed`：哪些 phase 完成了
  - `step_execution_order`：step 按拓扑排序执行
  - `failure_strategy_applied`：失败 step 是否按策略处理
  - `rollback_triggered`：l1-002 是否触发了全局回滚
  - `rollback_order`：回滚是否按 rollback_phases 反向执行
  - `gate_check_called`：每个 phase 结束后是否检查 gate

- **软断言**（不 block CI）：
  - `rto_tracking_quality`：是否追踪了每个 step 的 actual_duration
  - `reasoning_mentions_failure`：Agent 是否提到了失败原因

### 13.2 L2 Golden — Staging 集成

**目的**：在 staging 环境（隔离 Route53 hosted zone + Aurora staging replica）执行 dry_run=False。

| ID | 场景 | 验证点 |
|----|------|--------|
| l2-001 | AZ failover in staging | Route53 切换 → Aurora promote → 回滚 → 恢复 |
| l2-002 | Region failover in staging | 跨 region 全链路 → 回滚 |

**L2 手动执行，不进 CI 自动化。**

### 13.3 Golden 构建步骤

**阶段 1**：编写 2 个 plan JSON（l1-001 AZ failover + l1-002 region failover with failure）

**阶段 2**：用 Direct 版 + mock tools 采样建 baseline

> ⚠️ **采样脚本必须 incremental save + resume**。

**阶段 3**：人工 review + 生成 cases_l1.yaml

---

## 14. 硬约束

1. ⚠️ **`dry_run=True` 是 factory 的不可商量的默认值** — 代码参数 + env 变量双重 gate
2. ⚠️ **业务行为等价** — Direct 版和 Strands 版在相同输入下必须产出一致的 phase/step 结果
3. ⚠️ **fail-closed** — PlanValidator 异常 → abort；tool 异常 → 按 failure strategy 处理；LLM 异常 → abort + rollback
4. ⚠️ **全局回滚必须执行** — ABORT/ROLLBACK 触发后必须执行 plan.rollback_phases
5. ⚠️ **Agent 生命周期 = 一次演练** — `execute()` 内新建，结束后不再引用
6. ⚠️ **跨 region 操作前必须 health check** — 不能跳过 `check_cross_region_health`
7. ⚠️ **requires_approval step 必须调 request_manual_approval** — 即使 dry_run（自动 approve）
8. ⚠️ **Global inference profile** — `global.anthropic.claude-sonnet-4-6` 或 `global.anthropic.claude-opus-4-7`
9. ⚠️ **factory.py import 用 `from executor_direct import ...`** — 不用全限定路径
10. ⚠️ **Token usage 必填** — 返回 dict 包含 `token_usage`（含 `cache_read` / `cache_write`，即使 0）
11. ⚠️ **不用 `cache_prompt="default"`（已 deprecated）** → 用 `CacheConfig(strategy="auto")`
12. ⚠️ **CacheConfig import 路径强制为 `from strands.models import CacheConfig`**
13. ⚠️ **assert_cacheable 用 tokens 算** — `tiktoken.get_encoding("cl100k_base")`
14. ⚠️ **部分缓存必须拆稳定段 + 可变段**
15. ⚠️ **复用现有方法**（§ 6 清单）— 不要自己解析 DRPlan / 实现拓扑排序 / 检查依赖环
16. ⚠️ **step 按拓扑排序执行** — 复用 `GraphAnalyzer.topological_sort_within_layer()`

---

## 15. 验证步骤

### 15.1 单元测试（本地，mock tools，不真调 LLM）
```bash
cd dr-plan-generator && PYTHONPATH=. pytest ../tests/test_dr_executor_golden.py -v -k "not goldenreal and not l2"
```

### 15.2 L1 Golden CI（真调 Bedrock + mock tools）
```bash
cd dr-plan-generator && PYTHONPATH=. RUN_GOLDEN=1 DR_EXECUTOR_ENGINE=direct  pytest ../tests/test_dr_executor_golden.py -v -k "l1"
cd dr-plan-generator && PYTHONPATH=. RUN_GOLDEN=1 DR_EXECUTOR_ENGINE=strands pytest ../tests/test_dr_executor_golden.py -v -k "l1"
```

### 15.3 L2 集成测试（staging 环境）
```bash
cd dr-plan-generator && PYTHONPATH=. RUN_GOLDEN=1 DR_EXECUTOR_DRY_RUN=false \
  DR_SOURCE_REGION=us-east-1 DR_TARGET_REGION=us-west-2 \
  pytest ../tests/test_dr_executor_golden.py -v -k "l2"
```

> ⚠️ L2 手动执行，不进 CI 自动化。

### 15.4 Shadow 对比
```bash
cd dr-plan-generator && PYTHONPATH=. RUN_GOLDEN=1 pytest ../tests/test_dr_executor_shadow.py -v -s
```

### 15.5 缓存生效验证
```bash
cd dr-plan-generator && PYTHONPATH=. python ../experiments/strands-poc/verify_cache_dr_executor.py
```

### 15.6 集成验证
```
main.py plan → DRPlan JSON → PlanValidator.validate() → valid
  → make_dr_executor(dry_run=True).execute(plan, scope)
  → RehearsalReport
```

---

## 16. 验收门槛（Gate B - Phase 3 Module 6 Week 2 检查）

| 指标 | 门槛 |
|------|------|
| L1 Direct Golden | 2/2 |
| L1 Strands Golden | ≥ direct - 0（2 个 case 必须全过） |
| L1 行为一致性 | 2/2（phase 结果 + step 执行顺序一致） |
| L2 集成测试（staging） | 2/2（手动执行） |
| failure strategy 测试 | ROLLBACK + RETRY + SKIP 场景全部正确 |
| 全局 rollback 验证 | rollback 后 plan.rollback_phases 全部执行 |
| dry_run 默认值测试 | factory 默认 → dry_run=True |
| cross-region health check | 跨 region 操作前必调 |
| 稳态缓存命中率 | ≥ 50% |
| Prompt Caching 成本下降 | ≥ 20% |
| Strands p50 latency（L1） | ≤ 3x direct |
| 1 周灰度 SEV-1/2 | 0 |
| assert_cacheable | 稳定段 ≥ 1024 tokens |

### Gate B 不达标时的决策矩阵

| 情况 | 路径 A（首选） | 路径 B（降级） |
|------|---------------|---------------|
| 行为不一致 | 检查 system prompt + tool 实现一致性 | 以 Direct 为 ground truth |
| 缓存命中率 < 50% | 检查稳定段 tokens 和 step 间隔 | 接受更低命中率 |
| failure strategy 测试失败 | **P0 — 停手修复，无降级** | — |
| rollback 未执行 | **P0 — 停手修复** | — |
| L2 集成失败 | 区分 AWS 环境 vs Agent 决策问题 | 延期 1 周 |
| Strands latency > 3x | 检查 tool 调用轮数 | 接受 5x（DR 不在延迟敏感路径） |

**同一问题 3 次失败 → 停手，写入 progress log，通知大乖乖。**

---

## 17. Git 提交策略

| # | PR | 范围 | 复杂度 |
|---|-----|------|--------|
| 1 | refactor(dr): extract ExecutorBase + factory | 抽象层 + factory + dry_run 双重 gate | 中 |
| 2 | feat(dr): implement DirectExecutor | Direct 版 | 中-高 |
| 3 | feat(dr): implement StrandsExecutor + tools | Strands 版 + 8 @tool + failure strategy | 高 |
| 4 | test(dr): L1 golden set + shadow + cache | 2 L1 cases + 测试框架 | 中 |
| 5 | test(dr): L2 integration tests (staging) | L2 集成 + cross-region 验证 | 中 |
| 6 | feat(dr): wire executor into main.py | 接线 | 低 |
| 7 | docs(migration): freeze executor_direct + ADR | 冻结 + 文档 | 低 |

> PR3 最复杂 — 8 tool + failure strategy + 部分缓存 + 跨 region 安全。可拆 PR3a（tool）+ PR3b（Agent 编排 + 缓存）。

---

## 18. 2 周流程

### Week 1
- PR 1 合并（ExecutorBase + factory + dry_run 双重 gate）
- PR 2 开发（DirectExecutor）
- PR 3 开始（StrandsExecutor + tools + failure strategy）
- **⚠️ 稳定段写完后第一件事跑 `assert_cacheable`**

### Week 2
- PR 2 + 3 合并
- PR 4 合并（L1 Golden CI）
- 缓存验证
- PR 5：L2 staging 手动执行 2 次
- PR 6 合并（接线 main.py）
- 灰度：`DR_EXECUTOR_ENGINE=strands` + `DR_EXECUTOR_DRY_RUN=true`
- Gate B 检查
- 通过 → PR 7 合并（冻结），打 tag
- 未通过 → 延期 + ADR
- **⚠️ 必须交 retro：`experiments/strands-poc/retros/dr-executor-retro.md`**

---

## 19. 不要做的事

1. ❌ 不改已冻结的 Module 1-5
2. ❌ 不在 Golden CI 用 `dry_run=False`（L1 全部 mock）
3. ❌ 不在 production 执行任何真实 DR 切换
4. ❌ 不跨演练复用 Agent 实例
5. ❌ 不在 ABORT/ROLLBACK 时跳过 rollback_phases
6. ❌ 不用 `cache_prompt="default"`（已 deprecated）
7. ❌ 不用 chars 估算缓存下限
8. ❌ 不用 `from strands.types.models import CacheConfig`
9. ❌ 不用全限定 import 路径
10. ❌ 不无脑缓存整个 system prompt（必须拆稳定段 + 可变段）
11. ❌ 不在 tool 内嵌套 Strands Agent
12. ❌ 不自己实现拓扑排序 / 依赖环检测 / RTO 计算（复用 § 6 清单）
13. ❌ 不自己解析 DRPlan JSON（用 `DRPlan.from_dict()`）
14. ❌ 不跳过 cross-region health check 直接执行跨 region 操作

---

## 20. 失败处理

- **LLM 调用失败** → abort + 全局 rollback
- **tool 返回 error** → 按 step 的 failure strategy 处理（RETRY/ROLLBACK/SKIP/ABORT）
- **Agent 超时（>2h total）** → 外部 watchdog kill → 全局 rollback
- **PlanValidator 返回 CRITICAL** → abort 不执行
- **cross-region health check 失败** → abort 不执行
- **requires_approval 超时** → 按 GateCondition.on_timeout 处理
- **缓存验证不通过** → 检查稳定段 tokens + 拆分正确性
- **Direct vs Strands 行为不一致** → 检查 system prompt + tool 实现
- **同一问题 3 次失败** → 停手，写入 progress log，通知大乖乖

---

## 21. factory.py import 路径确认检查项

| 检查点 | 文件 | 检查内容 |
|--------|------|---------|
| PR1 提交前 | `executor_factory.py` | import 用 `from executor_direct import ...` |
| PR3 提交前 | `executor_strands.py` | import 用 `from engines.strands_common import ...` |
| PR6 提交前 | `main.py` | 调 `make_dr_executor()` 的 import 路径 |
| 每个 PR | `PYTHONPATH` | 确认 `cd dr-plan-generator && PYTHONPATH=.` |

---

## 22. 参考资料

- `dr-plan-generator/models.py` — DRPlan / DRPhase / DRStep 数据模型
- `dr-plan-generator/validation/verification_models.py` — StepVerificationResult / RehearsalReport
- `dr-plan-generator/validation/plan_validator.py` — PlanValidator
- `dr-plan-generator/assessment/rto_estimator.py` — RTOEstimator
- `dr-plan-generator/graph/graph_analyzer.py` — 拓扑排序 + 并行组
- `chaos/code/runner/runner_strands.py` — Runner Strands 版（Module 5，结构参考）
- `rca/engines/strands_common.py` — BedrockModel + assert_cacheable helper
- `experiments/strands-poc/retros/runner-retro.md` — Module 5 retro（**必读**）
- AWS Strands Agents 文档：https://strandsagents.com/
- Bedrock Prompt Caching 文档：https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html

---

## 23. 完成标志

1. ✅ 7 个 PR 全部合并 main
2. ✅ 15.1-15.6 验证步骤全部通过
3. ✅ 16 验收门槛全部达标（或按决策矩阵处理）
4. ✅ `BASELINE-direct.md` + `BASELINE-strands.md` 入仓
5. ✅ `docs/migration/timeline.md` 更新 `dr-executor.status: frozen`
6. ✅ tag `v-strands-cutover-dr-executor-YYYYMMDD` 已打
7. ✅ ADR 落地
8. ✅ `report.md` 新增 DR Executor 章节
9. ✅ assert_cacheable 通过（稳定段 ≥ 1024 tokens）
10. ✅ L1 Golden 全部通过
11. ✅ L2 集成测试 2/2 通过
12. ✅ dry_run 默认值验证通过
13. ✅ failure strategy 测试通过
14. ✅ 全局 rollback 测试通过
15. ✅ **Retrospective 已交**：`experiments/strands-poc/retros/dr-executor-retro.md`
16. ✅ sessions_send 给架构审阅猫：`[RETRO] dr-executor 完成，Top 3: ...`
17. ✅ **Phase 3 全部 6 个模块完成** 🎉

---

## 附录 A：备选架构

### A.1 多 Agent（每 phase 一个 Agent）

**不选原因**：同 Runner — phase 间强依赖，多 Agent 引入不必要的通信开销。

### A.2 0 Agent（纯 Python 流程 + 单点 LLM）

**不选原因**：不满足 Strands 迁移验证目标。DR Executor 是验证 Strands 处理"跨 region 有状态流程 + failure strategy"的关键场景。

### A.3 自由编排（不预设 step 顺序）

**不选原因**：安全。Step 执行顺序是拓扑排序约束（依赖关系），不能交给 LLM 自由决定。System prompt 明确 phase/step 顺序，Agent 在每个 step 内做执行决策。

---

**这是 Phase 3 最后一个模块。完成后 Phase 3 结束，进入 Phase 4 统一清理。**

**遇到任何不确定的地方，先在 Slack 里问大乖乖或架构审阅猫，不要自己拍脑袋决定。**
