# TASK: Phase 3 Module 4 — Chaos PolicyGuard (Pre-Execution Policy Guard)

> **任务归属**: 编程猫
> **发起**: 大乖乖 + 架构审阅猫
> **日期**: 2026-04-XX（Gate A 通过后启动）
> **范围**: 新建 `chaos/code/policy/guard_direct.py` + `guard_strands.py`，实现 pre-execution policy guard — 单次 LLM 调用判断实验是否允许执行
> **预计工作量**: 3 周（migration-strategy § 3.3 Phase 3 Week 13-15）
> **成功标志**: 所有 PR 合入 main + 验收门槛达标 + `guard_direct.py` 冻结
> **前序模块**: HypothesisAgent（Module 1，冻结）、LearningAgent（Module 2，冻结）、RCA Layer2 Probers（Module 3，冻结）
> **⚠️ 本模块特殊性**: 这是**新增能力**（非现有代码迁移），且是 Phase 3 最简单的模块 — 单次 LLM 调用，无 ReAct 多轮，无 multi-agent

---

## 0. 必读顺序

1. `TASK-phase3-shared-notes.md`（共享规范，**§ 3.4 是 PolicyGuard 的缓存设计要点**）
2. `retros/layer2-retro.md`（**必读** — § 6 Top 3 建议 + § 7 模板修改建议，直接影响本模块）
3. `retros/learning-retro.md`（**必读** — § 6 Top 3 建议，嵌套 Agent / import 路径 / incremental save）
4. `migration-strategy.md`（§ 3.3 Phase 3 + § 6.3 切换门槛 + § 6.4 Prompt Caching）
5. `TASK-phase3-rca-layer2.md`（结构参考）
6. `report.md`（Phase 3 Module 1-3 经验总结）
7. **当前模块代码**：无现有实现（新增能力），但参考：
   - `chaos/code/runner/runner.py` — PolicyGuard 的消费方（runner 执行前调 guard）
   - `chaos/code/orchestrator.py` — 实验编排入口
   - `chaos/code/agents/hypothesis_strands.py` — Strands Agent 实现参考

---

## 1. 工作目录

```
/home/ubuntu/tech/graph-dependency-platform
```

---

## 2. 前置条件（Gate A 确认）

### 不可豁免（必须全部满足）

- [ ] RCA Layer2 Probers（Module 3）已冻结，`timeline.md` status = `frozen`
- [ ] `make_layer2_engine()` + `make_learning_engine()` + `make_hypothesis_engine()` 在 runner 中稳定工作 ≥ 1 周
- [ ] Bedrock 月度预算足够（PolicyGuard 单次调用成本极低，主要成本在 Golden CI）
- [ ] 大乖乖已确认 10 个规则场景的业务规则（时间窗/namespace 白名单/故障类型限制等）

### 可豁免（大乖乖 Slack 确认即可）

- [ ] Module 3 稳定期 ≥ 4 周（可口头豁免，但需记录）
- [ ] `docs/migration/timeline.md` 中 `chaos-policy-guard` 的 `freeze_date` / `delete_date` 已填（可在 Week 3 补填）
- [ ] 大乖乖已打 tag `v-last-direct-guard-YYYYMMDD`（可在 PR-final 时补打）

*不可豁免项任一不满足 → 暂停启动，通知大乖乖。*

---

## 3. 本模块依赖的 env 变量清单

| 变量名 | 用途 | 与其他模块差异 |
|--------|------|---------------|
| `BEDROCK_REGION` | Bedrock 调用区域 | 同前序模块 |
| `BEDROCK_MODEL` | Bedrock 模型 ID | 同前序模块 |
| `POLICY_GUARD_ENGINE` | 引擎切换 `direct\|strands` | 本模块专用 |
| `POLICY_GUARD_RULES_PATH` | 规则 YAML 文件路径 | 本模块专用，默认 `chaos/code/policy/rules.yaml` |

> ⚠️ **sys.path 提醒**：新建 `chaos/code/policy/` 目录，PYTHONPATH 须包含 `chaos/code`。所有 import 用 `from policy.xxx import ...` 或 `from agents.xxx import ...`，**不用 `chaos.code.policy.*` 全限定路径**（Module 1-3 反复踩坑）。

---

## 4. 任务范围（严格限定）

**做**：
- 新建 `chaos/code/policy/` 目录，实现 PolicyGuard 的 direct + strands 双引擎
- `PolicyGuardBase` 抽象基类 + factory
- 10 个规则场景的 Golden Set
- Prompt Caching 评估与集成（见 § 8 缓存专题）
- 接入 `chaos/code/runner/runner.py`（runner 执行前调 guard 判断 allow/deny）
- 每次调用返回标准化 dict（含 `engine` / `model_used` / `latency_ms` / `token_usage` / `decision` / `reasoning`）

**不做**：
- 不改已冻结的 HypothesisAgent / LearningAgent / RCA Layer2 Probers
- 不改 `runner.py` 的核心执行逻辑（只在入口加 guard 调用）
- 不做多轮 ReAct（PolicyGuard 是单次 LLM 调用，判断 allow/deny）
- 不做运行时规则热更新（规则从 YAML 读，重启生效）
- 不做审计日志持久化（本模块只返回决策，持久化由 runner 负责）

---

## 5. 推荐架构

### 5.1 架构图

```
     ┌──────────────────────────┐
     │  runner.py / orchestrator│
     │  experiment.run()        │
     └───────────┬──────────────┘
                 │ 执行前
     ┌───────────▼──────────────┐
     │     PolicyGuard          │
     │  (single LLM call)      │
     │                          │
     │  Input:                  │
     │   - experiment metadata  │
     │   - current time/context │
     │   - rules (from YAML)   │
     │                          │
     │  Output:                 │
     │   - allow / deny         │
     │   - reasoning            │
     │   - matched_rules[]      │
     └───────────┬──────────────┘
                 │
          allow? ─┬─ yes → 继续执行
                  └─ no  → 中止 + 记录原因
```

### 5.2 为什么是 1 Agent，不是多 Agent

**判断标准**（来自 Module 3 retro § 6 Top 3）："子任务需要 LLM 吗？" — PolicyGuard 的唯一任务是"读规则 + 读实验 metadata → 输出 allow/deny"，这是**单次判断**，不需要多轮推理、不需要调 tool 查数据、不需要并行探查。

| 维度 | 多 Agent 方案 | 1 Agent 方案（推荐） |
|------|-------------|---------------------|
| 内存 | 不适用 | ~80-120 MB |
| 代码量 | 过度设计 | 2 个文件（direct + strands） |
| 延迟 | 不适用 | 单次 LLM call ~1-3s |
| 准确性 | 规则简单，不需要多视角 | 规则全在 system prompt 里，单次足够 |

**选型结论**：1 Agent + 0 tool。PolicyGuard 不需要 `@tool`（没有外部数据源要查），system prompt 包含全部规则，user message 是实验 metadata，LLM 直接返回 JSON 决策。

### 5.3 备选方案（附录 A）

见文末附录 A。

---

## 6. Agent 实例生命周期管理

> 来自 Module 3 retro § 6 Top 2：如果 Agent 被多次调用，必须管理 conversation history。

### PolicyGuard 的生命周期策略：**每次调用新建实例**

理由：
- PolicyGuard 每次调用是独立判断，不需要前次对话上下文
- 如果复用实例，`SlidingWindowConversationManager` 会累积历史，导致 inputTokens 线性增长（Module 3 实测：6k → 17k → 28k）
- PolicyGuard 调用频次低（每个实验执行前 1 次），新建实例的开销（~100ms）可忽略

### 实现方式

```python
class StrandsPolicyGuard(PolicyGuardBase):
    """每次 evaluate() 新建 Agent 实例。"""

    def evaluate(self, experiment: dict, context: dict) -> dict:
        # 每次调用新建，不复用
        agent = Agent(
            model=self._build_model(),
            system_prompt=self._system_prompt,
            # 不设 conversation_manager — 单次调用不需要
        )
        result = agent(self._format_user_message(experiment, context))
        return self._parse_decision(result)
```

### 硬规则

1. **不要在 `__init__` 里创建 Agent 实例然后在 `evaluate()` 里复用** — 会累积 history
2. **不要设 `SlidingWindowConversationManager`** — 单次调用不需要 conversation 管理
3. **如果未来需要"连续评估多个实验"的批量模式**，用 for 循环每次新建，不要复用实例

---

## 7. Strands Metrics 已知限制

> 来自 Module 3 retro § 6 Top 1：Strands `accumulated_usage` 不含 cacheRead/cacheWrite。

### 问题

Strands `EventLoopMetrics.accumulated_usage` 只有 `inputTokens` / `outputTokens` / `totalTokens`，**不传递 Bedrock response 中的 `cacheReadInputTokens` / `cacheWriteInputTokens`**。

Module 1-2 能报告缓存 token 是因为它们从 Bedrock raw response 提取，不通过 Strands metrics。

### 对 PolicyGuard 的影响

- 如果 Gate B 要求验证缓存命中率，需要**绕过 Strands metrics**
- 两种方案：
  - **(a) hook boto3 Bedrock response**：在 `_build_model()` 时注入回调，截获每次 `invoke_model` 的 raw response，提取 `usage` 字段
  - **(b) 用 Strands callback**：`Agent(callbacks=[UsageExtractor()])` — 如果 Strands SDK 支持 model-call-level callback

### 实现建议

```python
class UsageExtractor:
    """从 Strands Agent 的每次 model call 提取完整 usage（含缓存 token）。"""

    def __init__(self):
        self.usages = []

    def on_model_response(self, response: dict):
        usage = response.get("usage", {})
        self.usages.append({
            "input": usage.get("inputTokens", 0),
            "output": usage.get("outputTokens", 0),
            "cache_read": usage.get("cacheReadInputTokens", 0),
            "cache_write": usage.get("cacheWriteInputTokens", 0),
        })
```

> ⚠️ 如果两种方案都无法在 PolicyGuard 启动前验证可行性，**退而求其次**：Gate B 的缓存验证改用 `verify_cache_policy_guard.py` 脚本（Direct 版直接读 Bedrock response），Strands 版只验证"缓存前后 inputTokens 下降"作为间接指标。

---

## 8. Prompt Caching 专题

### 8.1 核心问题：system prompt 可能不够 1024 tokens

> shared-notes § 3.4 明确指出：PolicyGuard 的 system prompt 预估 2-3k tokens，**但如果规则 YAML 简短，可能接近甚至低于 1024 token 缓存下限**。

Module 2 retro 坑 3：初版 system prompt 482 tokens < 1024，通过加 fault type reference table 扩到 1270 tokens。
Module 3 retro 坑 5：初版 729 tokens，加 Anomaly Classification Guide 扩到 ~1100 tokens。

### 8.2 两个方案

| 方案 | 描述 | 优点 | 缺点 |
|------|------|------|------|
| **A: 充实 system prompt 到 ≥ 1500 tokens** | 把规则 YAML 展开为详细说明文档（每条规则加 rationale + example），加 domain reference | 享受缓存 | 需要写额外内容；如果内容没有信息量，LLM 可能忽略 |
| **B: 不加缓存** | system prompt 保持精简，不追求 1024 下限 | 实现简单；单次调用成本本来就低（~$0.01） | 无缓存节省 |

### 8.3 推荐：方案 A（优先尝试），方案 B（回退）

**执行步骤**：
1. 先写规则 YAML + system prompt，跑 `assert_cacheable`
2. 如果 ≥ 1024 tokens → 方案 A，启用缓存
3. 如果 < 1024 tokens：
   - 尝试扩充：加每条规则的 rationale + example（自然语言，对 LLM 理解有价值）
   - 加 Policy Evaluation Framework（评估维度 + 判断标准 + 边界案例处理指南）
   - 再跑 `assert_cacheable`
4. 如果扩充后仍 < 1024 tokens，或扩充内容没有实质信息量 → 方案 B，不加缓存
5. **无论哪个方案，在 BASELINE-*.md 里明确记录选择和理由**

### 8.4 缓存预期（如果方案 A 可行）

| 指标 | 预估值 |
|------|--------|
| 稳定前缀大小 | 1.5-3k tokens |
| 每次事件调用次数 | 1 次 |
| 稳态缓存命中率 | ≥ 50%（如果同一 runner 实例连续执行多个实验） |
| 预估成本节省 | 15-25%（Phase 3 最低，因为调用次数少） |

### 8.5 缓存对象 vs 不缓存对象

**缓存对象**（放 system prompt）：
- 全部策略规则（从 YAML 展开）
- 业务时间窗定义
- Namespace 白名单
- 故障类型限制清单
- Policy Evaluation Framework（评估维度 + 判断标准）

**不缓存**（放 user message）：
- 本次实验 metadata（名称 / 目标 / fault type / target namespace / 执行时间）
- 当前上下文（时间 / 环境 / 前次实验结果）

### 8.6 assert_cacheable 强制检查

```python
from engines.strands_common import assert_cacheable

# __init__ 里第一件事
assert_cacheable(self._system_prompt, min_tokens=1024)
```

> ⚠️ 用 `tiktoken.get_encoding("cl100k_base")`，**不用 `encoding_for_model`**（Module 2 retro 坑 4）。

---

## 9. 产出清单

### 9.1 抽象层

新建 `chaos/code/policy/base.py`：

```python
from abc import ABC, abstractmethod
from typing import Any

class PolicyGuardBase(ABC):
    """Pre-execution policy guard 基类。"""

    def __init__(self, rules_path: str | None = None): ...

    @abstractmethod
    def evaluate(
        self,
        experiment: dict,
        context: dict | None = None,
    ) -> dict:
        """
        评估实验是否允许执行。

        Args:
            experiment: 实验 metadata
                {
                    "name": str,
                    "fault_type": str,        # e.g. "pod-delete", "network-latency"
                    "target_namespace": str,   # e.g. "petsite-prod"
                    "target_service": str,     # e.g. "payment-service"
                    "duration_sec": int,
                    "blast_radius": str,       # "single-pod" | "service" | "namespace" | "cluster"
                }
            context: 执行上下文（可选）
                {
                    "current_time": str,       # ISO 8601
                    "environment": str,        # "staging" | "production"
                    "recent_incidents": list,  # 近期未关闭的 incident
                    "recent_experiments": list, # 近期执行的实验
                }

        Returns:
            {
                "decision": "allow" | "deny",
                "reasoning": str,           # LLM 生成的判断理由
                "matched_rules": list[str], # 匹配到的规则 ID
                "confidence": float,        # 0.0-1.0
                "engine": str,              # "direct" | "strands"
                "model_used": str | None,
                "latency_ms": int,
                "token_usage": {
                    "input": int, "output": int, "total": int,
                    "cache_read": int, "cache_write": int,
                } | None,
                "error": str | None,
            }
        """
```

新建或修改 `chaos/code/engines/factory.py`（或 `chaos/code/policy/factory.py`）：

```python
def make_policy_guard(rules_path: str | None = None) -> PolicyGuardBase:
    engine = os.environ.get("POLICY_GUARD_ENGINE", "direct")
    if engine == "strands":
        from policy.guard_strands import StrandsPolicyGuard
        return StrandsPolicyGuard(rules_path=rules_path)
    else:
        from policy.guard_direct import DirectPolicyGuard
        return DirectPolicyGuard(rules_path=rules_path)
```

> ⚠️ **factory import 路径确认**：用 `from policy.guard_strands import ...`，**不用 `chaos.code.policy.*`**（Module 1-3 反复踩坑）。

### 9.2 具体实现

```
chaos/code/policy/
├── __init__.py
├── base.py                    # PolicyGuardBase 抽象基类
├── factory.py                 # make_policy_guard()
├── guard_direct.py            # Direct 版：直接调 Bedrock
├── guard_strands.py           # Strands 版：Strands Agent 单次调用
├── rules.yaml                 # 10 个策略规则定义
└── rules_schema.py            # 规则 YAML 的 schema 校验
```

### 9.3 规则 YAML 结构（`rules.yaml`）

```yaml
# 10 个策略规则场景
rules:
  - id: R001
    name: "业务高峰时间窗保护"
    description: "工作日 09:00-18:00 (UTC+8) 不允许在 production 执行破坏性实验"
    condition:
      environment: "production"
      time_window: { weekday: [1,2,3,4,5], hours: [9,18] }
      fault_types: ["pod-delete", "node-drain", "network-partition"]
    action: "deny"
    severity: "critical"

  - id: R002
    name: "Namespace 白名单"
    description: "只允许在指定 namespace 执行实验"
    condition:
      allowed_namespaces: ["petsite-staging", "petsite-canary", "chaos-sandbox"]
    action: "deny_if_not_in_list"
    severity: "critical"

  - id: R003
    name: "故障类型限制"
    description: "禁止集群级故障注入（node-drain-all / cluster-shutdown）"
    condition:
      blocked_fault_types: ["node-drain-all", "cluster-shutdown", "az-failure"]
    action: "deny"
    severity: "critical"

  - id: R004
    name: "爆炸半径限制"
    description: "production 环境不允许 namespace 或 cluster 级别的爆炸半径"
    condition:
      environment: "production"
      blocked_blast_radius: ["namespace", "cluster"]
    action: "deny"
    severity: "high"

  - id: R005
    name: "连续实验间隔"
    description: "同一 service 的两次实验间隔不少于 30 分钟"
    condition:
      min_interval_minutes: 30
      scope: "same_service"
    action: "deny"
    severity: "medium"

  - id: R006
    name: "活跃 Incident 保护"
    description: "存在未关闭的 SEV-1/2 incident 时，不允许新实验"
    condition:
      active_incidents_severity: [1, 2]
    action: "deny"
    severity: "critical"

  - id: R007
    name: "单日实验次数上限"
    description: "同一 service 每天最多 5 次实验"
    condition:
      max_daily_experiments: 5
      scope: "same_service"
    action: "deny"
    severity: "medium"

  - id: R008
    name: "持续时间上限"
    description: "单次实验持续时间不超过 600 秒"
    condition:
      max_duration_sec: 600
    action: "deny"
    severity: "high"

  - id: R009
    name: "Staging 宽松模式"
    description: "staging 环境允许所有故障类型，但仍受持续时间和次数限制"
    condition:
      environment: "staging"
    action: "allow_with_limits"
    severity: "info"

  - id: R010
    name: "周末/节假日保护"
    description: "周末和指定节假日不允许 production 实验"
    condition:
      environment: "production"
      blocked_days: ["saturday", "sunday"]
      blocked_dates: []  # 节假日列表，由运维填充
    action: "deny"
    severity: "high"
```

### 9.4 system prompt 结构

```
## Role
You are a Chaos Engineering Policy Guard. Your job is to evaluate whether a proposed chaos experiment should be allowed to execute.

## Rules
{展开的 rules.yaml 内容，每条规则包含 id / name / description / condition / action}

## Policy Evaluation Framework

### Evaluation Dimensions
1. **Time Safety** — Is the current time within a safe execution window?
2. **Target Safety** — Is the target namespace/service allowed?
3. **Fault Safety** — Is the fault type permitted in this environment?
4. **Blast Radius Safety** — Is the blast radius acceptable?
5. **Operational Safety** — Are there active incidents or recent experiments that conflict?
6. **Duration Safety** — Is the experiment duration within limits?

### Judgment Standards
- If ANY critical-severity rule is violated → DENY (no override)
- If only medium/low-severity rules are violated → DENY with explanation
- If all rules pass → ALLOW
- When multiple rules interact, apply the most restrictive

### Edge Case Handling
- Unknown fault_type → DENY (fail-closed)
- Missing context fields → DENY with "insufficient context" reasoning
- Time zone ambiguity → Assume UTC+8

## Output Format
Respond in JSON:
{
    "decision": "allow" | "deny",
    "reasoning": "...",
    "matched_rules": ["R001", "R002"],
    "confidence": 0.95
}
```

> **Token 预估**：上述 system prompt 展开后约 1500-2500 tokens（取决于 rules.yaml 的详细程度）。如果不够 1024 tokens，按 § 8.3 步骤扩充。

---

## 10. Golden Set

新增 `tests/golden/policy_guard/`：

```
tests/golden/policy_guard/
├── scenarios.yaml               # 10 个规则场景
├── cases.yaml                   # 行为约束式 golden
├── BASELINE-direct.md
└── BASELINE-strands.md
```

### 10.1 Golden 场景（10 个，对应 10 条规则）

| ID | 场景 | 预期决策 | 匹配规则 |
|----|------|---------|---------|
| g001 | 工作日 14:00 在 production 执行 pod-delete | deny | R001 |
| g002 | 工作日 22:00 在 petsite-staging 执行 pod-delete | allow | — |
| g003 | 任何时间在未知 namespace 执行任何实验 | deny | R002 |
| g004 | 执行 cluster-shutdown | deny | R003 |
| g005 | production 中 blast_radius=namespace | deny | R004 |
| g006 | 同一 service 20 分钟内第二次实验 | deny | R005 |
| g007 | 存在 SEV-1 incident 时执行实验 | deny | R006 |
| g008 | 同一 service 今天第 6 次实验 | deny | R007 |
| g009 | 持续时间 900 秒的实验 | deny | R008 |
| g010 | 周六在 production 执行实验 | deny | R010 |

另加 2 个边界场景：

| ID | 场景 | 预期 | 说明 |
|----|------|------|------|
| g011 | 畸形实验 metadata（缺 fault_type） | deny | fail-closed |
| g012 | staging 全部规则通过 | allow | R009 宽松模式 |

### 10.2 行为约束式 golden 结构

```yaml
- id: g001
  scenario: "工作日高峰期 production pod-delete"
  experiment:
    name: "test-pod-delete-payment"
    fault_type: "pod-delete"
    target_namespace: "petsite-prod"
    target_service: "payment-service"
    duration_sec: 120
    blast_radius: "single-pod"
  context:
    current_time: "2026-04-20T14:30:00+08:00"  # 周一 14:30
    environment: "production"
    recent_incidents: []
    recent_experiments: []

  expected_decision: "deny"
  must_match_rules: ["R001"]
  reasoning_must_include_any:
    - "业务高峰"
    - "business hours"
    - "time window"
```

### 10.3 Golden 构建步骤

**阶段 1：编写 scenarios.yaml + fixture**

12 个场景（10 规则场景 + 2 边界场景），每个场景需要：
- experiment metadata dict
- context dict（时间 / 环境 / incidents / 最近实验）

**阶段 2：用 direct 版采样建 baseline**

写 `chaos/code/policy/sample_for_golden.py`：

> ⚠️ **采样脚本必须 incremental save + resume**（Module 2 retro § 6 Top 2 强制要求）。

**阶段 3：人工 review + 生成 cases.yaml**

PolicyGuard 场景是确定性的（规则匹配是否正确），人工 review 工作量很小。

---

## 11. 硬约束

1. ⚠️ **业务行为等价** — Direct 版和 Strands 版在相同输入下必须做出相同 allow/deny 决策（通过 Golden Set 验证）
2. ⚠️ **fail-closed** — 任何异常（LLM 调用失败 / 响应解析失败 / 超时）→ deny，不允许因为 guard 故障而放行实验
3. ⚠️ **Prompt Caching 评估** — 必须在 Week 1 跑 `assert_cacheable`，按 § 8.3 决策流程确定方案 A 还是方案 B
4. ⚠️ **Global inference profile** — 使用 `global.anthropic.claude-sonnet-4-6` 或 `global.anthropic.claude-opus-4-7`
5. ⚠️ **每次调用新建 Agent 实例** — 不复用（§ 6 生命周期管理）
6. ⚠️ **不在 PolicyGuard 里调其他 engine** — PolicyGuard 没有 `@tool`，没有嵌套风险；但如果未来加 tool，必须遵守"tool 内调其他 engine 强制 direct"的硬规则
7. ⚠️ **factory.py import 用 `policy.*` / `engines.*`** — 不用 `chaos.code.policy.*` 全限定路径
8. ⚠️ **Token usage 必填** — 返回 dict 必须包含 `token_usage`，含 `cache_read` / `cache_write`（即使是 0）
9. ⚠️ **LLM 响应必须是 JSON** — 设 `response_format={"type": "json_object"}` 或在 system prompt 强制 JSON 输出
10. ⚠️ **不用 `cache_prompt="default"`（已 deprecated）** → 用 `CacheConfig(strategy="auto")`
11. ⚠️ **CacheConfig import 路径强制为 `from strands.models import CacheConfig`** — `strands.types.models` 不存在，`strands.models.bedrock` 只有 BedrockModel。错误路径会被 `try/except ImportError` 静默吞掉，缓存永远不启用但代码正常运行（Module 3 踩坑，极难 debug）
12. ⚠️ **assert_cacheable 用 tokens 算** — `tiktoken.get_encoding("cl100k_base")`，不用 chars 估算

---

## 12. 验证步骤

### 12.1 单元测试（本地，不真调 LLM）
```bash
cd chaos/code && PYTHONPATH=. pytest ../../tests/test_policy_guard_golden.py -v -k "not goldenreal"
```

### 12.2 Golden CI（真调 Bedrock，有成本）
```bash
cd chaos/code && PYTHONPATH=. RUN_GOLDEN=1 POLICY_GUARD_ENGINE=direct  pytest ../../tests/test_policy_guard_golden.py -v
cd chaos/code && PYTHONPATH=. RUN_GOLDEN=1 POLICY_GUARD_ENGINE=strands pytest ../../tests/test_policy_guard_golden.py -v
```

### 12.3 Shadow 对比
```bash
cd chaos/code && PYTHONPATH=. RUN_GOLDEN=1 pytest ../../tests/test_policy_guard_shadow.py -v -s
```
输出：
- 每 case 的 decision 一致性（direct vs strands）
- reasoning 质量对比
- 延迟 / token 使用对比

### 12.4 缓存生效验证（如果方案 A）

```bash
cd chaos/code && PYTHONPATH=. python ../../experiments/strands-poc/verify_cache_policy_guard.py
```

```python
# experiments/strands-poc/verify_cache_policy_guard.py
from verify_cache_common import run_cache_verification
from policy.factory import make_policy_guard

run_cache_verification(
    engine_factory=make_policy_guard,
    sample_input={
        "experiment": SAMPLE_EXPERIMENT,
        "context": SAMPLE_CONTEXT,
    },
    method_name="evaluate",
    repeat=3,
)
```

### 12.5 集成验证
跑一次完整实验流程：
```
orchestrator → PolicyGuard.evaluate() → allow → runner.run() → ...
orchestrator → PolicyGuard.evaluate() → deny → 中止 + 记录原因
```
确保 runner 正确消费 guard 的输出。

---

## 13. 验收门槛（Gate B - Phase 3 Module 4 Week 3 检查）

| 指标 | 门槛 |
|------|------|
| Direct Golden | ≥ 11/12 |
| Strands Golden | ≥ direct - 1 |
| Decision 一致性（Direct vs Strands） | 12/12（allow/deny 必须完全一致） |
| fail-closed 测试 | 3 个异常场景全部 deny |
| 稳态缓存命中率（如果方案 A） | ≥ 50% |
| Prompt Caching 成本下降（如果方案 A） | ≥ 15% |
| Strands p50 latency | ≤ 2x direct |
| 1 周灰度 SEV-1/2 | 0 |
| 集成测试（orchestrator → guard → runner） | 通过 |
| assert_cacheable 检查（如果方案 A） | system prompt ≥ 1024 tokens |

### Gate B 不达标时的决策矩阵

| 情况 | 路径 A（首选） | 路径 B（降级） |
|------|---------------|---------------|
| Decision 不一致 | 检查 system prompt 是否两版一致；检查 JSON 解析是否容错 | 以 Direct 为 ground truth，调 Strands prompt |
| 缓存命中率 < 50% | 检查 system prompt token 数；检查调用间隔是否超过 5min TTL | 切方案 B（不加缓存） |
| fail-closed 测试失败 | 这是 P0 — 停手修复 | 无降级路径 |
| Strands latency > 2x direct | 检查是否意外触发 ReAct 多轮 | 接受 3x，PolicyGuard 不在关键延迟路径 |

**同一问题 3 次失败 → 停手，写入 progress log，通知大乖乖。**

---

## 14. Git 提交策略

| # | PR | 范围 | 复杂度 |
|---|-----|------|--------|
| 1 | `feat(policy): add PolicyGuardBase + factory + rules.yaml` | 抽象层 + 规则定义 | 低 |
| 2 | `feat(policy): implement DirectPolicyGuard + StrandsPolicyGuard` | 双引擎实现 | 中 |
| 3 | `test(policy): golden set + shadow + cache verification` | 12 cases + 测试 | 中 |
| 4 | `feat(runner): wire PolicyGuard into runner pre-execution` | 接线 + 集成测试 | 低 |
| 5 | `docs(migration): freeze guard_direct.py + ADR` | 冻结 + 文档 | 低 |

> 📝 Module 2 retro 建议 "PR 切分按实际复杂度，不强制 7 个"。PolicyGuard 是最简单的模块，5 个 PR 足够。PR2 如果代码量少可合并到 PR1。

每个 PR 单独 review。PR5 合并时必须同步更新 `docs/migration/timeline.md`（`status: frozen`）。

---

## 15. 3 周流程

### Week 1
- PR 1 合并（基类 + factory + rules.yaml）
- PR 2 开发（DirectPolicyGuard + StrandsPolicyGuard）
- **⚠️ 写完 system prompt 后第一件事跑 `assert_cacheable`**
- 按 § 8.3 决策流程确定缓存方案

### Week 2
- PR 2 + 3 合并
- 跑 Golden CI 两个 engine 拿到 baseline
- 跑缓存验证（如果方案 A）
- PR 4 合并（接线到 runner）
- env `POLICY_GUARD_ENGINE=strands` 在灰度节点开启

### Week 3
- 每日 shadow 对比
- Gate B 检查所有门槛
- 通过 → PR 5 合并（冻结），打 tag
- 未通过 → 延期 1 周并写 ADR

---

## 16. 不要做的事

1. ❌ 不改已冻结的 Module 1-3
2. ❌ 不做多轮 ReAct（单次调用足够）
3. ❌ 不加 `@tool`（PolicyGuard 不需要查外部数据源）
4. ❌ 不复用 Agent 实例（每次新建）
5. ❌ 不在异常时返回 allow（fail-closed）
6. ❌ 不用 `cache_prompt="default"`（已 deprecated）
7. ❌ 不用 chars 估算缓存下限
8. ❌ 不用 `from strands.types.models import CacheConfig`（模块不存在，silent fallback）
9. ❌ 不用 `chaos.code.policy.*` 全限定 import
10. ❌ 不做规则热更新（重启生效即可）
11. ❌ 不把 PolicyGuard 和 Chaos Runner 合并迁移

---

## 17. 失败处理

- **LLM 返回非 JSON** → 重试 1 次（加 "Please respond in JSON only." 到 user message）；仍失败 → deny + error
- **LLM 超时（>10s）** → deny + error（单次调用不应超 5s）
- **assert_cacheable 不达标** → 按 § 8.3 尝试扩充；仍不够 → 方案 B
- **Direct vs Strands decision 不一致** → 检查 system prompt 是否完全相同；检查 JSON 解析差异
- **sample_for_golden 脚本挂了** → 检查 incremental save + resume（§ 10.3）
- **同一问题 3 次失败** → 停手，写入 progress log，通知大乖乖

---

## 18. factory.py import 路径确认检查项

| 检查点 | 文件 | 检查内容 |
|--------|------|---------|
| PR1 提交前 | `chaos/code/policy/factory.py` | import 用 `from policy.guard_direct import ...`，不用 `chaos.code.*` |
| PR2 提交前 | `chaos/code/policy/guard_strands.py` | import 用 `from engines.strands_common import ...` |
| PR4 提交前 | `chaos/code/runner/runner.py` | 调 `make_policy_guard()` 的 import 路径 |
| 每个 PR | `PYTHONPATH` | 确认 `PYTHONPATH=chaos/code` 或 `cd chaos/code && PYTHONPATH=.` |

---

## 19. 参考资料

- `chaos/code/runner/runner.py` — PolicyGuard 的消费方
- `chaos/code/orchestrator.py` — 实验编排入口
- `chaos/code/agents/hypothesis_strands.py` — Strands Agent 实现参考
- `chaos/code/agents/learning_strands.py` — Module 2 Strands 实现参考
- `rca/engines/strands_common.py` — BedrockModel + assert_cacheable helper
- `experiments/strands-poc/retros/layer2-retro.md` — Module 3 retro（**必读**）
- `experiments/strands-poc/retros/learning-retro.md` — Module 2 retro（**必读**）
- AWS Strands Agents 文档：<https://strandsagents.com/>
- Bedrock Prompt Caching 文档：<https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html>

---

## 20. 完成标志

1. ✅ 5 个 PR 全部合并 main
2. ✅ 12.1-12.5 验证步骤全部通过
3. ✅ 13 验收门槛全部达标（或按决策矩阵处理）
4. ✅ `BASELINE-direct.md` + `BASELINE-strands.md` 入仓
5. ✅ `docs/migration/timeline.md` 更新 `chaos-policy-guard.status: frozen`
6. ✅ tag `v-strands-cutover-guard-YYYYMMDD` 已打
7. ✅ ADR 落地
8. ✅ `report.md` 新增 PolicyGuard 章节
9. ✅ assert_cacheable 通过（或明确记录方案 B）
10. ✅ 集成测试通过（orchestrator → guard → runner）
11. ✅ **Retrospective 已交**：`experiments/strands-poc/retros/policy-guard-retro.md`
12. ✅ sessions_send 给架构审阅猫一条 `[RETRO] policy-guard 完成，Top 3: ...` 消息

---

## 附录 A：备选架构

### A.1 纯规则引擎（无 LLM）

用 Python 规则引擎（如 `rule-engine` 库）做硬编码规则匹配，不调 LLM。

**优点**：零成本、确定性、延迟 <1ms
**缺点**：无法处理模糊场景（如"这个实验虽然在时间窗内但影响极小，是否放行？"）；规则变更需要代码改动；不在 Strands 迁移范围内

**不选原因**：PolicyGuard 的价值在于 LLM 能理解实验语义，做出规则交叉判断（如 R001 + R004 同时触发时的综合 reasoning）。纯规则引擎做不到这一点。且本模块目标是验证 Strands 在"简单单次调用"场景的表现。

### A.2 1 Agent + N tool（规则检查 tool 化）

每条规则做一个 `@tool`（check_time_window / check_namespace / ...），Agent ReAct 决定调哪些 tool。

**优点**：灵活、可扩展
**缺点**：10 条规则 = 10 个 tool，Agent ReAct 需要 2-3 轮才能调完所有相关 tool，延迟 × 3；tool schema 加到 system prompt 后可能 > 5k tokens（成本增加）；规则是确定性检查，不需要 LLM 决定"该不该查"

**不选原因**：过度设计。10 条规则放 system prompt 里，LLM 一次读完一次判断，比 ReAct 多轮更快更便宜。

---

**下一个模块**：Chaos Runner（Phase 3 Week 16-18），TASK 文件在本模块完成后才发布。

**遇到任何不确定的地方，先在 Slack 里问大乖乖或架构审阅猫，不要自己拍脑袋决定。**
