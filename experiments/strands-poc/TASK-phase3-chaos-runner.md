# TASK: Phase 3 Module 5 — Chaos Runner (5-Phase Experiment Execution Engine)

> **任务归属**: 编程猫
> **发起**: 大乖乖 + 架构审阅猫
> **日期**: 2026-04-XX（Gate A 通过后启动）
> **范围**: 新建 `chaos/code/runner/runner_direct.py` + `runner_strands.py`，将现有 `runner.py` 的 5-phase 实验执行流程迁移到 Strands Agent，保持 direct 版冻结
> **预计工作量**: 3 周（migration-strategy § 3.3 Phase 3 Week 16-18）
> **成功标志**: 所有 PR 合入 main + 验收门槛达标 + `runner_direct.py` 冻结
> **前序模块**: HypothesisAgent（Module 1，冻结）、LearningAgent（Module 2，冻结）、RCA Layer2 Probers（Module 3，冻结）、Chaos PolicyGuard（Module 4，冻结）
> **⚠️ 本模块特殊性**: 这是 **Phase 3 风险最高的模块** — 第一个真正 mutate K8s/FIS 状态的模块。`dry_run=True` 是不可商量的 factory 默认值。

---

## 0. 必读顺序

1. `TASK-phase3-shared-notes.md`（共享规范，**§ 3.5 是 Chaos Runner 的缓存设计要点**）
2. `retros/policy-guard-retro.md`（**必读** — § 6 Top 3 建议 + § 7 模板修改建议，直接影响本模块）
3. `retros/layer2-retro.md`（**必读** — § 2.3 Strands conversation history 累积陷阱、§ 6 Top 3）
4. `retros/learning-retro.md`（嵌套 Agent / import 路径 / incremental save）
5. `migration-strategy.md`（§ 3.3 Phase 3 + § 6.3 切换门槛 + § 6.4 Prompt Caching）
6. `TASK-phase3-chaos-policy-guard.md`（结构参考 + 最新模板）
7. `report.md`（Phase 3 Module 1-4 经验总结）
8. **当前模块代码**（必读，理解现有实现）：
   - `chaos/code/runner/runner.py` — 现有 5-phase 执行引擎（637 行）
   - `chaos/code/runner/experiment.py` — Experiment 数据模型
   - `chaos/code/runner/fault_injector.py` — Chaos Mesh 故障注入
   - `chaos/code/runner/fis_backend.py` — FIS 故障注入
   - `chaos/code/runner/result.py` — ExperimentResult 数据模型
   - `chaos/code/runner/config.py` — 配置
   - `chaos/code/orchestrator.py` — 实验编排入口

---

## 1. 工作目录

```
/home/ubuntu/tech/graph-dependency-platform
```

---

## 2. 前置条件（Gate A 确认）

### 不可豁免（必须全部满足）

- [ ] Chaos PolicyGuard（Module 4）已冻结，`timeline.md` status = `frozen`
- [ ] `make_policy_guard()` 在 runner 中稳定工作 ≥ 1 周
- [ ] Module 1-4 全部冻结且无 P0 回归
- [ ] Bedrock 月度预算充足（Runner 涉及多次 LLM 调用 × dry-run 实验）
- [ ] 大乖乖已确认 3 个 dry-run 实验场景（pod-delete / network-latency / FIS scenario）
- [ ] dry-run 专用 K8s namespace `chaos-sandbox` 已创建且隔离

### 可豁免（大乖乖 Slack 确认即可）

- [ ] Module 4 稳定期 ≥ 4 周（可口头豁免，但需记录）
- [ ] `docs/migration/timeline.md` 中 `chaos-runner` 的 `freeze_date` / `delete_date` 已填（可在 Week 3 补填）
- [ ] 大乖乖已打 tag `v-last-direct-runner-YYYYMMDD`（可在 PR-final 时补打）

*不可豁免项任一不满足 → 暂停启动，通知大乖乖。*

---

## 3. 本模块依赖的 env 变量清单

| 变量名 | 用途 | 与其他模块差异 |
|--------|------|---------------|
| `BEDROCK_REGION` | Bedrock 调用区域 | 同前序模块 |
| `BEDROCK_MODEL` | Bedrock 模型 ID | 同前序模块 |
| `CHAOS_RUNNER_ENGINE` | 引擎切换 `direct\|strands` | 本模块专用 |
| `CHAOS_RUNNER_DRY_RUN` | `true\|false`，默认 `true` | 本模块专用，**必须默认 true** |
| `CHAOS_SANDBOX_NAMESPACE` | dry-run 实验隔离 namespace | 默认 `chaos-sandbox` |
| `POLICY_GUARD_ENGINE` | PolicyGuard 引擎 | 复用 Module 4 |
| `ENVIRONMENT` | `staging\|production` | 同前序模块 |

> ⚠️ **sys.path 提醒**：所有 import 用 `from runner.xxx import ...` 或 `from engines.xxx import ...`，**不用 `chaos.code.runner.*` 全限定路径**（Module 1-4 反复踩坑）。

---

## 4. 任务范围（严格限定）

**做**：
- 提取 `runner.py` 为 `RunnerBase` 抽象基类（保留 5-phase 流程骨架）
- 新建 `runner_direct.py` — 基于现有 `runner.py` 逻辑的 Direct 版（调 Bedrock 辅助决策）
- 新建 `runner_strands.py` — Strands Agent 版（单 Agent，用 `@tool` 封装 K8s/FIS 操作）
- factory `make_runner_engine(dry_run=True)` — **dry_run 默认值必须是 True**
- 3 个 dry-run 实验的 Golden Set（L1 + L2 两层）
- Prompt Caching 部分缓存集成（拆稳定段 + 可变段）
- PolicyGuard 联动（phase0 调 guard，deny → abort）
- abort / rollback 机制
- 每次调用返回标准化 dict（含 `engine` / `model_used` / `latency_ms` / `token_usage` / `phase_results` / `decision_log`）

**不做**：
- 不改已冻结的 Module 1-4
- 不做真实 production 故障注入（Golden CI 全部 dry-run 或 mock）
- 不改 `orchestrator.py` 的核心编排逻辑（只替换 runner 构造方式）
- 不做新的 PolicyGuard 规则（复用 Module 4 现有规则）
- 不做跨 region DR（那是 Module 6 DR Executor 的事）

---

## 5. 推荐架构

### 5.1 架构图

```
     ┌──────────────────────────┐
     │  orchestrator.py         │
     │  experiment.run()        │
     └───────────┬──────────────┘
                 │
     ┌───────────▼──────────────┐
     │  RunnerBase (factory)    │
     │  dry_run=True (default)  │
     └───────────┬──────────────┘
                 │
     ┌───────────▼──────────────────────────────────────┐
     │  Strands Runner Agent（单 Agent + N tool）       │
     │  生命周期 = 一次实验（5 phase 间复用）           │
     │                                                   │
     │  system_prompt:                                   │
     │    [0] STABLE_CHAOS_FRAMEWORK (cached)            │
     │    [1] this_experiment_config  (not cached)       │
     │                                                   │
     │  @tool: inject_fault()                            │
     │  @tool: check_steady_state()                      │
     │  @tool: observe_metrics()                         │
     │  @tool: check_stop_conditions()                   │
     │  @tool: recover_fault()                           │
     │  @tool: collect_logs()                            │
     │  @tool: policy_guard_check()                      │
     │                                                   │
     │  Phase 0 → 1 → 2 → 3 → 4 → 5                    │
     └──────────────────────────────────────────────────┘
```

### 5.2 为什么是 1 Agent + N tool，不是多 Agent

**判断标准**（来自 Module 3 retro）："子任务需要 LLM 吗？"

5 个 phase 是**同一次实验的顺序流程**，每个 phase 之间有强依赖（phase2 必须在 phase1 通过后执行）。用多 Agent 会引入 Agent 间通信 + 状态传递的复杂度，得不偿失。

| 维度 | 多 Agent 方案 | 1 Agent + N tool（推荐） |
|------|-------------|------------------------|
| 内存 | 5-7 × Agent 实例 | ~200-400 MB（单实例） |
| 状态传递 | Agent 间需要序列化 | Agent 内天然共享 context |
| 缓存利用 | 每 Agent 独立缓存池 | 单 Agent 一个缓存池，phase 间复用 |
| abort/rollback | 需要 coordinator | Agent 自然中断流程 |

**选型结论**：1 Agent + 7 tool，Agent 在一次实验的 5 个 phase 间复用实例（享受 cache_read），实验结束后销毁。

### 5.3 备选方案（附录 A）

见文末附录 A。

---

## 6. Agent 实例生命周期管理

> 来自 Module 4 retro § 6 Top 1：Runner Agent 的生命周期 = 一次实验。

### Chaos Runner 的生命周期策略：**一次实验内复用，实验结束后销毁**

这与前序模块（每次调用新建）不同。理由：

| 维度 | 每次调用新建 | 一次实验内复用（推荐） |
|------|------------|----------------------|
| cache_read | 基本为 0（每次 cache_write） | phase 间可享受 cache_read（~5min TTL 内） |
| conversation history | 无累积 | 会累积，但**有价值** — phase3 观测需要知道 phase1 基线 |
| 内存 | 每次新建销毁 | 单实验内稳定，实验结束释放 |
| 跨实验污染 | 不存在 | 不存在（实验结束销毁） |

### 实现方式

```python
class StrandsRunner(RunnerBase):
    """一次实验复用一个 Agent 实例，实验结束销毁。"""

    def run(self, experiment: Experiment) -> ExperimentResult:
        # 每次 run() 新建 Agent — 一次实验一个实例
        agent = Agent(
            model=self._build_model(),
            system_prompt=self._build_system_prompt(experiment),
            tools=[
                self.tool_inject_fault,
                self.tool_check_steady_state,
                self.tool_observe_metrics,
                self.tool_check_stop_conditions,
                self.tool_recover_fault,
                self.tool_collect_logs,
                self.tool_policy_guard_check,
            ],
            # conversation_manager 不设或用 SlidingWindow
            # phase 间的 history 对 Runner 有价值（phase3 需要 phase1 基线上下文）
        )

        # Agent 驱动 5 phase 流程
        result = agent(self._format_run_prompt(experiment))

        # 实验结束，agent 局部变量自动释放
        return self._parse_result(result)
```

### 硬规则

1. **Agent 实例的生命周期 = 一次 `run()` 调用** — `run()` 结束后 Agent 不再被引用
2. **不要在 `__init__` 创建 Agent 然后跨 `run()` 复用** — 会跨实验污染
3. **conversation history 在实验内是有价值的** — phase3 需要 phase1 基线对比，不要每 phase 新建
4. **如果 history 导致 inputTokens 爆炸（>50k）**，用 `SlidingWindowConversationManager(window_size=10)` 限制

---

## 7. ⚠️ 安全专题（本模块核心关切）

> 来自 Module 4 retro § 6 Top 2：Runner 是第一个真正 mutate 状态的模块。

### 7.1 dry_run 默认值

```python
def make_runner_engine(dry_run: bool = True) -> RunnerBase:
    """dry_run=True 是不可商量的默认值。"""
    engine = os.environ.get("CHAOS_RUNNER_ENGINE", "direct")
    # env 变量也默认 true
    env_dry_run = os.environ.get("CHAOS_RUNNER_DRY_RUN", "true").lower() == "true"
    effective_dry_run = dry_run and env_dry_run  # 两个都 False 才真正执行

    if engine == "strands":
        from runner.runner_strands import StrandsRunner
        return StrandsRunner(dry_run=effective_dry_run)
    else:
        from runner.runner_direct import DirectRunner
        return DirectRunner(dry_run=effective_dry_run)
```

- **切换到 `dry_run=False` 需要 env + 代码参数双重确认** — 防止误操作
- Golden CI 永远 `dry_run=True`
- L2 集成测试在 `chaos-sandbox` namespace 跑，仍然是 `dry_run=False` 但隔离

### 7.2 PolicyGuard 联动

- Phase 0 必须先调 `PolicyGuard.evaluate()` — deny → raise `PrefightFailure`，实验不执行
- PolicyGuard 故障（超时 / 异常）→ **fail-closed**：deny，不执行
- PolicyGuard 结果记录到 `ExperimentResult.policy_guard`（审计）

### 7.3 Abort / Rollback 机制

现有 `runner.py` 已有 `AbortException` + `_emergency_cleanup`。Strands 版必须保留：

```
Phase 3 观测期发现 stop condition 触发
  → raise AbortException
  → Agent 中断后续 phase
  → _emergency_cleanup: 删除注入的 ChaosExperiment / 停止 FIS 实验
  → 记录 abort_reason
```

| 场景 | 行为 |
|------|------|
| Stop condition 触发（success_rate < 阈值） | AbortException → cleanup → result.status = "ABORTED" |
| LLM 调用失败 | 不影响物理故障注入（tool 直接操作 K8s/FIS），但决策链中断 → abort |
| Agent 超时（单次实验 >30min） | 外部 timeout → cleanup |
| K8s API 失败 | tool 返回 error → Agent 决定 abort 还是 retry |
| dry_run 模式 | tool 执行时跳过实际 K8s/FIS 操作，返回 mock 结果 |

### 7.4 Namespace 保护

- `chaos-sandbox`：dry-run 实验专用，L2 集成测试在此 namespace
- `petsite-staging`：允许真实实验（但仍受 PolicyGuard 规则约束）
- `petsite-prod` / `default` / `kube-system`：**绝对禁止** — PolicyGuard R002 + Runner 代码双重检查

```python
PROTECTED_NAMESPACES = frozenset({"default", "kube-system", "kube-public", "kube-node-lease"})

def _validate_namespace(self, namespace: str):
    if namespace in PROTECTED_NAMESPACES:
        raise PrefightFailure(f"禁止在受保护 namespace 执行实验: {namespace}")
```

### 7.5 Blast Radius 硬限制

| 环境 | 允许的 blast_radius |
|------|-------------------|
| chaos-sandbox | single-pod, service |
| staging | single-pod, service, namespace |
| production | single-pod（仅限） |

---

## 8. Prompt Caching 专题 — 部分缓存

> shared-notes § 3.5 明确要求拆稳定段 + 可变段。

### 8.1 缓存设计

Runner 的 system prompt 包含两个语义层：

| 层 | 内容 | 缓存？ | 预估 tokens |
|----|------|--------|------------|
| **稳定段** | 7-phase 流程定义 + K8s/FIS 操作语义 + stop condition 规则 + tool 使用规范 | ✅ 缓存 | 3-5k |
| **可变段** | 本次实验配置（target / params / duration）+ 上下文 | ❌ 不缓存 | 0.5-1k |

### 8.2 拆分实现

```python
"system": [
    {
        "type": "text",
        "text": STABLE_CHAOS_FRAMEWORK,
        "cache_control": {"type": "ephemeral"},
    },
    {
        "type": "text",
        "text": self._format_experiment_config(experiment),
        # 不加 cache_control — 每次实验不同
    },
]
```

### 8.3 稳定段内容（STABLE_CHAOS_FRAMEWORK）

```
## Role
You are a Chaos Engineering Experiment Runner. You execute chaos experiments 
following a strict 7-phase protocol, using tools to interact with K8s/FIS.

## 7-Phase Protocol
Phase 0: Pre-flight Check — validate environment, run PolicyGuard
Phase 1: Steady State Before — collect baseline metrics (N samples)
Phase 2: Fault Injection — inject fault via K8s ChaosExperiment or FIS
Phase 3: Observation — monitor metrics at intervals, check stop conditions
Phase 4: Fault Recovery — wait for fault expiry, verify pod health
Phase 5: Steady State After — verify recovery, generate report

## K8s/FIS Operation Semantics
- inject_fault: Creates ChaosExperiment CR (Chaos Mesh) or starts FIS experiment
- recover_fault: Deletes ChaosExperiment CR or stops FIS experiment
- dry_run mode: All mutating operations return mock results without actual state changes

## Stop Condition Rules
- If ANY stop condition is breached during Phase 3 → ABORT immediately
- Abort procedure: recover_fault → collect_logs → report with status=ABORTED
- Stop conditions are evaluated every OBSERVE_INTERVAL seconds

## Tool Usage Rules
- Always call policy_guard_check in Phase 0 before any mutation
- Never skip Phase 4 recovery — even if experiment succeeded
- Log collection is best-effort (non-fatal if fails)
- If a tool returns error, decide: retry (max 2) or abort

## Safety Invariants
- NEVER inject fault if PolicyGuard denied
- NEVER inject fault in protected namespaces
- ALWAYS clean up injected faults on abort/error (emergency_cleanup)
- In dry_run mode, report expected outcomes without actual mutations
```

### 8.4 可变段内容（每次实验不同）

```
## Current Experiment
Name: {experiment.name}
Target: {experiment.target_service} in {experiment.target_namespace}
Fault Type: {experiment.fault.type}
Duration: {experiment.duration}
Blast Radius: {experiment.blast_radius}
Backend: {experiment.backend}
Dry Run: {self.dry_run}

## Context
Environment: {environment}
Current Time: {now}
PolicyGuard Engine: {policy_guard_engine}
```

### 8.5 缓存预期

| 指标 | 预估值 |
|------|--------|
| 稳定前缀大小 | 3-5k tokens |
| 每次实验 LLM 调用次数 | 5-8 次（每 phase 1-2 次） |
| 稳态缓存命中率 | ≥ 50%（phase 间复用 Agent，缓存有效） |
| 预估成本节省 | 20-30% |

### 8.6 assert_cacheable 强制检查

```python
from engines.strands_common import assert_cacheable

# __init__ 里检查稳定段
assert_cacheable(STABLE_CHAOS_FRAMEWORK, min_tokens=1024)
```

> ⚠️ 用 `tiktoken.get_encoding("cl100k_base")`，**不用 `encoding_for_model`**。

---

## 9. Strands Metrics 已知限制

> 前序模块反复验证：Strands `accumulated_usage` 不含 cacheRead/cacheWrite。

### 对 Runner 的影响

Runner 一次实验内多次 LLM 调用，累积 token 量较大。如果不能准确报告缓存 token，Gate B 的缓存验证需要绕行。

### 方案

- **(a) hook boto3 Bedrock response**：截获每次 `invoke_model` 的 raw response 提取 `usage`
- **(b) UsageExtractor callback**：如果 Strands SDK 支持 model-call-level callback
- **(c) 退而求其次**：Direct 版直接读 Bedrock response，Strands 版用"cache 前后 inputTokens 下降"作为间接指标

> ⚠️ **converse() API 不返回 cacheReadInputTokens**（Module 4 retro § 2.3 发现）。Direct 版如果用 `converse()`，cache_read 永远为 0。建议 Direct 版用 `invoke_model`。

---

## 10. 产出清单

### 10.1 抽象层 [必须]

新建 `chaos/code/runner/base.py`（或重构现有 `runner.py`）：

```python
from abc import ABC, abstractmethod
from typing import Any

class RunnerBase(ABC):
    """5-Phase 混沌实验执行引擎基类。"""

    def __init__(self, dry_run: bool = True, tags: dict = None):
        self.dry_run = dry_run  # 默认 True
        self.tags = tags or {}

    @abstractmethod
    def run(self, experiment: "Experiment") -> "ExperimentResult":
        """执行完整的 5-phase 实验。"""
```

### 10.2 Factory [必须]

新建或修改 `chaos/code/runner/factory.py`：

```python
def make_runner_engine(dry_run: bool = True) -> RunnerBase:
    """⚠️ dry_run=True 是不可商量的默认值。"""
    engine = os.environ.get("CHAOS_RUNNER_ENGINE", "direct")
    env_dry_run = os.environ.get("CHAOS_RUNNER_DRY_RUN", "true").lower() == "true"
    effective_dry_run = dry_run and env_dry_run

    if engine == "strands":
        from runner.runner_strands import StrandsRunner
        return StrandsRunner(dry_run=effective_dry_run)
    else:
        from runner.runner_direct import DirectRunner
        return DirectRunner(dry_run=effective_dry_run)
```

> ⚠️ **factory import 路径确认**：用 `from runner.runner_strands import ...`，**不用 `chaos.code.runner.*`**。

### 10.3 Direct 版 [必须]

```
chaos/code/runner/runner_direct.py   # 基于现有 runner.py 逻辑提取
```

Direct 版保留现有 `runner.py` 的全部 5-phase 逻辑，但：
- 继承 `RunnerBase`
- LLM 辅助决策（如 phase3 stop condition 判断复杂场景）直接调 Bedrock
- `dry_run` 模式下 tool 操作返回 mock 结果

### 10.4 Strands 版 [必须]

```
chaos/code/runner/runner_strands.py  # Strands Agent + 7 @tool
```

### 10.5 Tools [必须]

```python
@tool
def inject_fault(fault_type: str, target: str, namespace: str, duration_sec: int) -> dict:
    """Phase 2: 注入故障（Chaos Mesh 或 FIS）。dry_run 模式返回 mock。"""

@tool
def check_steady_state(service: str, namespace: str, samples: int = 3) -> dict:
    """Phase 1/5: 采集稳态指标。"""

@tool
def observe_metrics(service: str, namespace: str) -> dict:
    """Phase 3: 采集观测期指标。"""

@tool
def check_stop_conditions(metrics: dict, thresholds: dict) -> dict:
    """Phase 3: 检查 stop conditions，返回 {breached: bool, details: ...}。"""

@tool
def recover_fault(experiment_name: str, backend: str) -> dict:
    """Phase 4: 回收故障注入（删除 ChaosExperiment / 停止 FIS）。"""

@tool
def collect_logs(service: str, namespace: str, since: str = "5m") -> dict:
    """采集 Pod 日志（best-effort）。"""

@tool
def policy_guard_check(experiment: dict, context: dict) -> dict:
    """Phase 0: 调 PolicyGuard，返回 allow/deny。"""
```

> ⚠️ **Strands tool 内调其他 engine（如 PolicyGuard）必须用 direct** — 避免 Strands 嵌套。`policy_guard_check` tool 内部：`make_policy_guard()` 返回的引擎不受 Runner 引擎选择影响。

### 10.6 完整文件清单

```
chaos/code/runner/
├── __init__.py              # [必须] 更新 export
├── base.py                  # [必须] RunnerBase 抽象基类
├── factory.py               # [必须] make_runner_engine(dry_run=True)
├── runner_direct.py         # [必须] Direct 版
├── runner_strands.py        # [必须] Strands 版
├── runner.py                # [保留] 现有实现（过渡期保留，冻结后标 deprecated）
├── experiment.py            # [不改] 现有 Experiment 数据模型
├── result.py                # [不改] 现有 ExperimentResult
├── fault_injector.py        # [不改] 现有 Chaos Mesh 客户端
├── fis_backend.py           # [不改] 现有 FIS 客户端
├── config.py                # [可选] 可能需要加 dry_run / namespace 配置
├── metrics.py               # [不改] 现有 DeepFlowMetrics
├── ...                      # 其余现有文件不改
```

### 10.7 测试文件

```
tests/golden/chaos_runner/
├── scenarios.yaml           # [必须] 3 个 dry-run 实验场景 + 边界场景
├── cases_l1.yaml            # [必须] L1 Golden — 纯 LLM 输出验证（mock K8s/FIS）
├── cases_l2.yaml            # [必须] L2 Golden — 集成验证（chaos-sandbox namespace）
├── BASELINE-direct.md       # [必须]
└── BASELINE-strands.md      # [必须]
```

### 10.8 其他

```
experiments/strands-poc/
├── verify_cache_runner.py           # [必须] 缓存生效验证脚本
├── retros/runner-retro.md           # [必须] Week 3 交付
```

---

## 11. Golden Set — L1/L2 两层设计

> 来自 Module 4 retro § 6 Top 3：Runner 的 Golden 不能只验 LLM 输出。

### 11.1 L1 Golden — 纯 LLM 输出验证

**目的**：验证 Agent 的**决策质量** — 给定实验配置，Agent 是否正确地走完 5 phase 流程、做出合理决策。

**方法**：mock 所有 K8s/FIS tool（返回预设结果），只测 LLM 的 tool 调用序列和决策。

| ID | 场景 | 预期行为 | 验证点 |
|----|------|---------|--------|
| l1-001 | pod-delete dry-run（正常完成） | 5 phase 全部完成 | tool 调用序列正确 + decision_log 完整 |
| l1-002 | network-latency（stop condition 触发） | Phase 3 abort | 检测到阈值突破 → 触发 abort → cleanup |
| l1-003 | FIS scenario（正常完成） | 5 phase 全部完成 | FIS backend 分支正确 |
| l1-004 | PolicyGuard deny | Phase 0 abort | 不进入 Phase 1 |
| l1-005 | 畸形实验（缺 target_namespace） | Phase 0 abort | fail-closed |
| l1-006 | protected namespace（kube-system） | Phase 0 abort | namespace 保护生效 |

**断言类型**：

- **硬断言**（必须通过，block CI）：
  - `expected_status`：COMPLETED / ABORTED / ERROR
  - `expected_phase_reached`：最后成功完成的 phase
  - `tool_call_sequence`：关键 tool 是否被调用（顺序）
  - `policy_guard_called`：Phase 0 是否调了 PolicyGuard
  - `cleanup_on_abort`：abort 时是否调了 recover_fault

- **软断言**（参考性，不 block CI）：
  - `reasoning_quality`：Agent 的决策理由是否提到了关键指标
  - `latency_reasonable`：各 phase 延迟是否在合理范围

### 11.2 L2 Golden — 集成验证（dry-run namespace）

**目的**：验证 Agent + K8s/FIS 的**端到端行为** — 在 `chaos-sandbox` namespace 真正执行 dry-run。

**方法**：`dry_run=False` 但在隔离 namespace `chaos-sandbox` 执行。

| ID | 场景 | 验证点 |
|----|------|--------|
| l2-001 | pod-delete in chaos-sandbox | ChaosExperiment CR 被创建 → pod 被影响 → CR 被删除 → pod 恢复 |
| l2-002 | network-latency in chaos-sandbox | 网络注入 → 指标变化 → 恢复 |
| l2-003 | 手动触发 stop condition | abort → cleanup → 无残留 ChaosExperiment |

**L2 不在 Golden CI 中自动跑** — 需要 K8s 集群可达。L2 由编程猫手动执行 3 次，结果记入 BASELINE。

### 11.3 Golden 构建步骤

**阶段 1**：编写 L1 scenarios.yaml（6 个场景 + mock tool 返回值）

**阶段 2**：用 Direct 版 + mock tools 采样建 baseline

> ⚠️ **采样脚本必须 incremental save + resume**。

**阶段 3**：人工 review + 生成 cases_l1.yaml

**阶段 4**：在 chaos-sandbox 执行 L2 集成测试（手动 3 次）

---

## 12. 硬约束

1. ⚠️ **`dry_run=True` 是 factory 的不可商量的默认值** — 代码参数 + env 变量双重 gate，两个都 False 才真执行
2. ⚠️ **业务行为等价** — Direct 版和 Strands 版在相同输入（含 mock tool）下必须产出一致的 phase 结果
3. ⚠️ **fail-closed** — PolicyGuard 异常 → deny；tool 异常 → abort + cleanup；LLM 异常 → abort + cleanup
4. ⚠️ **abort 必须 cleanup** — 任何异常退出必须调 `_emergency_cleanup`（删除 ChaosExperiment CR / 停止 FIS）
5. ⚠️ **Agent 生命周期 = 一次实验** — `run()` 内新建，`run()` 结束后不再引用
6. ⚠️ **Protected namespaces 双重检查** — PolicyGuard 规则 + Runner 代码 `_validate_namespace()`
7. ⚠️ **Global inference profile** — 使用 `global.anthropic.claude-sonnet-4-6` 或 `global.anthropic.claude-opus-4-7`
8. ⚠️ **factory.py import 用 `runner.*` / `engines.*`** — 不用 `chaos.code.runner.*` 全限定路径
9. ⚠️ **Token usage 必填** — 返回 dict 必须包含 `token_usage`，含 `cache_read` / `cache_write`（即使是 0）
10. ⚠️ **不用 `cache_prompt="default"`（已 deprecated）** → 用 `CacheConfig(strategy="auto")`
11. ⚠️ **CacheConfig import 路径强制为 `from strands.models import CacheConfig`** — 不用 `strands.types.models`（Module 3 踩坑，silent ImportError 极难 debug）
12. ⚠️ **assert_cacheable 用 tokens 算** — `tiktoken.get_encoding("cl100k_base")`
13. ⚠️ **Strands tool 内调其他 engine（PolicyGuard）必须 direct** — 避免 Strands 嵌套
14. ⚠️ **部分缓存必须拆稳定段 + 可变段** — 不要无脑缓存整个 system prompt

---

## 13. 验证步骤

### 13.1 单元测试（本地，mock tools，不真调 LLM）
```bash
cd chaos/code && PYTHONPATH=. pytest ../../tests/test_runner_golden.py -v -k "not goldenreal and not l2"
```

### 13.2 L1 Golden CI（真调 Bedrock + mock tools）
```bash
cd chaos/code && PYTHONPATH=. RUN_GOLDEN=1 CHAOS_RUNNER_ENGINE=direct  pytest ../../tests/test_runner_golden.py -v -k "l1"
cd chaos/code && PYTHONPATH=. RUN_GOLDEN=1 CHAOS_RUNNER_ENGINE=strands pytest ../../tests/test_runner_golden.py -v -k "l1"
```

### 13.3 L2 集成测试（真实 K8s，chaos-sandbox namespace）
```bash
cd chaos/code && PYTHONPATH=. RUN_GOLDEN=1 CHAOS_RUNNER_DRY_RUN=false \
  CHAOS_SANDBOX_NAMESPACE=chaos-sandbox \
  pytest ../../tests/test_runner_golden.py -v -k "l2"
```

> ⚠️ L2 手动执行，不进 CI 自动化。

### 13.4 Shadow 对比
```bash
cd chaos/code && PYTHONPATH=. RUN_GOLDEN=1 pytest ../../tests/test_runner_shadow.py -v -s
```

### 13.5 缓存生效验证
```bash
cd chaos/code && PYTHONPATH=. python ../../experiments/strands-poc/verify_cache_runner.py
```

### 13.6 集成验证
完整 dry-run 实验流程：
```
orchestrator → PolicyGuard → allow → Runner.run() → 5 phase → report
orchestrator → PolicyGuard → deny → Runner abort → 记录原因
```

---

## 14. 验收门槛（Gate B - Phase 3 Module 5 Week 3 检查）

| 指标 | 门槛 |
|------|------|
| L1 Direct Golden | ≥ 5/6 |
| L1 Strands Golden | ≥ direct - 1 |
| L1 行为一致性（Direct vs Strands） | 6/6（phase 结果 + tool 调用序列一致） |
| L2 集成测试（chaos-sandbox） | 3/3（手动执行） |
| fail-closed 测试 | 3 个异常场景全部 abort + cleanup |
| abort cleanup 验证 | abort 后无残留 ChaosExperiment CR |
| dry_run 默认值测试 | factory 默认 → dry_run=True |
| namespace 保护测试 | protected namespace → PrefightFailure |
| 稳态缓存命中率 | ≥ 50%（phase 间 Agent 复用） |
| Prompt Caching 成本下降 | ≥ 20% |
| Strands p50 latency（L1） | ≤ 3x direct |
| 1 周灰度 SEV-1/2 | 0 |
| assert_cacheable | 稳定段 ≥ 1024 tokens |

### Gate B 不达标时的决策矩阵

| 情况 | 路径 A（首选） | 路径 B（降级） |
|------|---------------|---------------|
| 行为不一致 | 检查 system prompt + tool 实现是否两版一致 | 以 Direct 为 ground truth |
| 缓存命中率 < 50% | 检查稳定段 tokens；检查实验内 phase 间隔是否超 5min TTL | 接受更低命中率 |
| fail-closed 测试失败 | **P0 — 停手修复，无降级路径** | — |
| abort 后有残留 CR | **P0 — 停手修复** | — |
| L2 集成失败 | 分析是 K8s 环境问题还是 Agent 决策问题 | 延期 1 周排查 |
| Strands latency > 3x | 检查是否 tool 调用过多 / ReAct 轮数过多 | 接受 5x（Runner 不在延迟敏感路径） |

**同一问题 3 次失败 → 停手，写入 progress log，通知大乖乖。**

---

## 15. Git 提交策略

| # | PR | 范围 | 复杂度 |
|---|-----|------|--------|
| 1 | `refactor(runner): extract RunnerBase + factory` | 抽象层 + factory + dry_run 双重 gate | 中 |
| 2 | `feat(runner): implement DirectRunner` | Direct 版（从 runner.py 提取） | 中-高 |
| 3 | `feat(runner): implement StrandsRunner + tools` | Strands 版 + 7 个 @tool | 高 |
| 4 | `test(runner): L1 golden set + shadow + cache verification` | 6 L1 cases + 测试框架 | 中 |
| 5 | `test(runner): L2 integration tests (chaos-sandbox)` | L2 集成 + namespace 保护测试 | 中 |
| 6 | `feat(orchestrator): wire StrandsRunner into orchestrator` | 接线 + 集成验证 | 低 |
| 7 | `docs(migration): freeze runner_direct.py + ADR` | 冻结 + 文档 | 低 |

> PR3 是最大最复杂的 PR — 7 个 tool + Agent 流程 + 部分缓存 + 安全机制。如果太大，可拆为 PR3a（tool 实现）+ PR3b（Agent 编排 + 缓存）。

每个 PR 单独 review。PR7 合并时必须同步更新 `docs/migration/timeline.md`（`status: frozen`）。

---

## 16. 3 周流程

### Week 1
- PR 1 合并（RunnerBase + factory + dry_run 双重 gate）
- PR 2 开发（DirectRunner 从 runner.py 提取）
- PR 3 开始（StrandsRunner tool 实现）
- **⚠️ 稳定段 system prompt 写完后第一件事跑 `assert_cacheable`**

### Week 2
- PR 2 + 3 合并
- PR 4 合并（L1 Golden CI 两个 engine）
- 跑缓存验证
- PR 5：L2 集成测试在 chaos-sandbox 手动执行 3 次
- PR 6 合并（接线 orchestrator）
- env `CHAOS_RUNNER_ENGINE=strands` + `CHAOS_RUNNER_DRY_RUN=true` 灰度

### Week 3
- 每日 shadow 对比（dry-run 模式）
- Gate B 检查所有门槛
- 通过 → PR 7 合并（冻结），打 tag
- 未通过 → 延期 1 周并写 ADR
- **⚠️ 必须交 retro：`experiments/strands-poc/retros/runner-retro.md`**

---

## 17. 不要做的事

1. ❌ 不改已冻结的 Module 1-4
2. ❌ 不在 Golden CI 用 `dry_run=False`（L1 全部 mock，L2 仅在 chaos-sandbox）
3. ❌ 不在 production namespace 执行任何测试
4. ❌ 不跨实验复用 Agent 实例（每次 `run()` 新建）
5. ❌ 不在异常时跳过 cleanup（abort 必须 cleanup）
6. ❌ 不用 `cache_prompt="default"`（已 deprecated）
7. ❌ 不用 chars 估算缓存下限
8. ❌ 不用 `from strands.types.models import CacheConfig`
9. ❌ 不用 `chaos.code.runner.*` 全限定 import
10. ❌ 不无脑缓存整个 system prompt（必须拆稳定段 + 可变段）
11. ❌ 不在 tool 内嵌套 Strands Agent（调 PolicyGuard 用 direct）
12. ❌ 不合并 Runner 和 DR Executor 的迁移

---

## 18. 失败处理

- **LLM 调用失败** → abort + cleanup（Runner 不能"半执行"，要么全完成要么全回滚）
- **tool 返回 error** → Agent 决定 retry（max 2）还是 abort；abort 必须 cleanup
- **Agent 超时（>30min）** → 外部 watchdog kill → cleanup
- **K8s API 不可达** → Phase 0 preflight 检查应捕获；如果 Phase 2 才发现 → abort + cleanup
- **PolicyGuard 超时** → fail-closed → abort
- **缓存验证不通过** → 检查稳定段 tokens + 拆分是否正确；退而求其次用不拆分方案
- **Direct vs Strands 行为不一致** → 检查 system prompt 是否一致；检查 tool 实现差异
- **L2 集成测试失败** → 区分 K8s 环境问题 vs Agent 决策问题
- **同一问题 3 次失败** → 停手，写入 progress log，通知大乖乖

---

## 19. factory.py import 路径确认检查项

| 检查点 | 文件 | 检查内容 |
|--------|------|---------|
| PR1 提交前 | `chaos/code/runner/factory.py` | import 用 `from runner.runner_direct import ...`，不用 `chaos.code.*` |
| PR3 提交前 | `chaos/code/runner/runner_strands.py` | import 用 `from engines.strands_common import ...` |
| PR3 提交前 | `runner_strands.py` 的 tool | `policy_guard_check` 内 import 用 `from policy.factory import make_policy_guard` |
| PR6 提交前 | `chaos/code/orchestrator.py` | 调 `make_runner_engine()` 的 import 路径 |
| 每个 PR | `PYTHONPATH` | 确认 `PYTHONPATH=chaos/code` 或 `cd chaos/code && PYTHONPATH=.` |

---

## 20. 参考资料

- `chaos/code/runner/runner.py` — 现有 5-phase 实验引擎（迁移源）
- `chaos/code/runner/experiment.py` — Experiment 数据模型
- `chaos/code/runner/fault_injector.py` — Chaos Mesh 客户端
- `chaos/code/runner/fis_backend.py` — FIS 客户端
- `chaos/code/policy/guard_strands.py` — PolicyGuard Strands 版（Module 4）
- `rca/engines/strands_common.py` — BedrockModel + assert_cacheable helper
- `experiments/strands-poc/retros/policy-guard-retro.md` — Module 4 retro（**必读**）
- `experiments/strands-poc/retros/layer2-retro.md` — Module 3 retro（**必读**）
- AWS Strands Agents 文档：<https://strandsagents.com/>
- Bedrock Prompt Caching 文档：<https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html>

---

## 21. 完成标志

1. ✅ 7 个 PR 全部合并 main
2. ✅ 13.1-13.6 验证步骤全部通过
3. ✅ 14 验收门槛全部达标（或按决策矩阵处理）
4. ✅ `BASELINE-direct.md` + `BASELINE-strands.md` 入仓
5. ✅ `docs/migration/timeline.md` 更新 `chaos-runner.status: frozen`
6. ✅ tag `v-strands-cutover-runner-YYYYMMDD` 已打
7. ✅ ADR 落地
8. ✅ `report.md` 新增 Chaos Runner 章节
9. ✅ assert_cacheable 通过（稳定段 ≥ 1024 tokens）
10. ✅ L1 Golden 全部通过
11. ✅ L2 集成测试 3/3 通过
12. ✅ dry_run 默认值验证通过
13. ✅ abort + cleanup 验证通过（无残留 CR）
14. ✅ **Retrospective 已交**：`experiments/strands-poc/retros/runner-retro.md`
15. ✅ sessions_send 给架构审阅猫一条 `[RETRO] chaos-runner 完成，Top 3: ...` 消息

---

## 附录 A：备选架构

### A.1 5 个独立 Agent（每 phase 一个）

每个 phase 独立 Agent，phase 间通过序列化 ExperimentResult 传递状态。

**优点**：每个 Agent 职责单一，prompt 简短
**缺点**：5 个 Agent 内存开销 × 5；phase 间状态序列化复杂；无法享受 phase 间缓存复用；abort 需要跨 Agent coordinator
**不选原因**：过度设计。5 phase 是顺序流程，单 Agent 天然处理，不需要引入 Agent 间通信开销。

### A.2 0 Agent（纯 Python 流程 + 单点 LLM 调用）

保留现有 `runner.py` 的 Python 流程控制，只在需要 LLM 判断的点（如 phase3 复杂 stop condition）调单次 LLM。

**优点**：最小变动；LLM 成本最低
**缺点**：不在 Strands 迁移范围内；无法验证 Strands 在"有状态流程 + 工具调用"场景的表现
**不选原因**：本模块的迁移目标就是验证 Strands Agent 驱动复杂有状态流程的能力。纯 Python + 单点 LLM 不满足 Phase 3 目标。

### A.3 1 Agent + ReAct 自由编排（不预设 phase 顺序）

让 Agent 自行决定 tool 调用顺序，不硬编码 5 phase。

**优点**：最大灵活性；Agent 可根据情况跳过 phase
**缺点**：不可预测 — Agent 可能跳过 Phase 4 recovery（致命）；可能在 Phase 2 反复注入故障；tool 调用序列不可控导致 Golden 极难写
**不选原因**：安全。Runner 的 phase 顺序是安全约束（必须 preflight → inject → observe → recover → verify），不能交给 LLM 自由决定。System prompt 里硬编码 7-phase 协议，Agent 负责在每个 phase 内做决策，但 phase 顺序不可变。

---

**下一个模块**：DR Executor（Phase 3 Week 19-20），TASK 文件在本模块完成后才发布。

**遇到任何不确定的地方，先在 Slack 里问大乖乖或架构审阅猫，不要自己拍脑袋决定。**
