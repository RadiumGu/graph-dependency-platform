# Chaos PolicyGuard Migration Retrospective

> 作者：编程猫
> 完成日期：2026-04-19
> 目标读者：架构审阅猫（写 Chaos Runner / DR Executor 模块 TASK 时读）

## 1. 模块信息

| 项目 | 值 |
|------|-----|
| 模块名 | chaos-policy-guard (Phase 3 Module 4) |
| Direct 文件 | `chaos/code/policy/guard_direct.py` |
| Strands 文件 | `chaos/code/policy/guard_strands.py` |
| Golden 通过率 | Direct 12/12, Strands 12/12 |
| Shadow 一致性 | 12/12 |
| 缓存方案 | 方案 A（system prompt 1839 tokens，≥ 1024 阈值） |
| 总 commit 数 | 3（`1978ef0` → `fe1f61a`） |
| 实际耗时 | ~40 分钟（含缓存 bug 调查时间） |

## 2. TASK 质量反馈（给架构审阅猫的直接输入 ★★）

### 2.1 做得好的

1. **§8 Prompt Caching 专题写得极好** — 方案 A/B 决策树 + assert_cacheable 强制检查 + 1024 token 阈值，让我第一时间跑验证就知道走哪条路
2. **§6 Agent 实例生命周期管理** — "每次调用新建实例"写明了 WHY（避免 conversation history 累积），不用猜
3. **§11 硬约束完整** — 12 条全部有用，特别是 #11（CacheConfig import 路径）直接避免了 Module 3 同款 bug
4. **§5.2 "为什么是 1 Agent 不是多 Agent"** — 省了我做架构决策的时间，直接开写
5. **模块复杂度匹配** — TASK 说"最简单的模块"，实际确实是，没有过度设计

### 2.2 让我困惑或不好用的地方

1. **§9.2 提到了 `rules_schema.py`** — 产出清单里列了但没给 schema 定义，也没说是否必须。我没写（rules.yaml 结构简单，schema 校验 ROI 不高）。建议：如果不是必须的，从产出清单移除或标注"可选"
2. **§10.2 golden 结构里有 `reasoning_must_include_any` 字段** — 实际测试中发现 LLM reasoning 文本变化大，这个字段很难稳定通过。我没实现这个断言。建议：要么删掉，要么改成"recommended but not enforced"

### 2.3 漏了应该提醒的陷阱

1. **Bedrock converse() API 不返回 cacheReadInputTokens** — Direct 引擎用 `converse()` API，返回的 `usage` 里没有 cache 字段（只有 inputTokens/outputTokens）。这意味着 Direct 引擎的 `token_usage.cache_read` 永远是 0，不是 bug 而是 API 限制。TASK §7 只讨论了 Strands metrics 的限制，没提 Direct 引擎的 converse() API 也有这个问题
2. **Strands 每次新建 Agent 时 cache_write 而不是 cache_read** — 因为 §6 要求每次新建实例，Bedrock 端没有跨实例的 cache session，所以 Strands 的 cache_read 在"每次新建"模式下也基本为 0（只有 cache_write）。只有在短时间内连续调用（<5min TTL）且 Bedrock 端 cache 未过期时才能 cache_read

### 2.4 下个 TASK 建议加的字段

1. **`converse() vs invoke_model() API 的 cache 字段差异`** — 如果下个模块也需要 token usage 报告
2. **`Agent 实例复用 vs 每次新建`的缓存影响矩阵** — Module 5 Runner 可能需要在一次实验里多次调用 Agent，实例复用策略影响缓存命中

## 3. 技术教训

### 3.1 踩到的坑

**坑 1：无坑（真的）**

PolicyGuard 是 4 个模块里唯一没踩坑的。原因：
- Module 3 retro 的 CacheConfig import 路径教训直接写进了 TASK 硬约束
- 单次 LLM 调用，无 tool，无 ReAct，复杂度极低
- System prompt 1839 tokens，远超 1024 阈值，不需要补充

**坑 0.5：Module 3 缓存 bug 是在本模块开始前调查的**

大乖乖要求"调查缓存 token 报告问题"，发现 Layer2 的 `from strands.types.models import CacheConfig` 模块不存在。这个 bug 的修复（`5944d9c`）虽然在 Module 4 TASK 交付前完成，但严格说是 Module 3 的遗留问题。已更新 layer2-retro.md。

### 3.2 架构决策记录

| 决策 | 选项 | 选了 | 理由 |
|------|------|------|------|
| 引擎数量 | 1 Agent vs 2 Agent | 1 Agent | TASK §5.2 明确，单次判断无需多 Agent |
| 缓存方案 | A（启用）vs B（不启用） | A | 1839 tokens > 1024 阈值 |
| rules_schema.py | 写 vs 不写 | 不写 | rules.yaml 结构简单，YAML schema 校验 ROI 不高 |
| reasoning_must_include_any | 实现 vs 跳过 | 跳过 | LLM reasoning 变化太大，hard assert 会导致 flaky test |
| Runner 集成位置 | phase0_preflight vs 独立 phase | phase0 | TASK §9.1 要求 pre-execution，phase0 是最早的检查点 |

### 3.3 Strands 增值点实测

PolicyGuard 是最不需要 Strands 的模块 — 没有 tool、没有 ReAct、没有 conversation。Direct 和 Strands 在功能上完全等价，区别只在：
- Strands 自动管理 CacheConfig（如果 import 对了）
- Strands metrics.get_summary() 提供 token usage（但 cache 字段依赖 Bedrock 端行为）

**结论**：对于"单次 LLM 调用"模式，Strands 的增值主要在缓存管理和 metrics 标准化，不在推理能力。

### 3.4 Golden Set 经验

1. **12 个 case 全部一次通过** — 这是第一次。原因：规则场景是确定性的（规则匹配），LLM 只需要理解规则并应用，不需要创造性推理
2. **system prompt 质量是关键** — 1839 tokens 的 prompt 里包含了完整的规则、评估维度、判断标准、边界处理、故障类型参考表。LLM 不需要"猜"任何东西
3. **Shadow 测试比 Golden 更有价值** — 12/12 一致性说明两个引擎的 prompt 处理完全等价

## 4. 跨猫协作

- 架构审阅猫的 TASK 质量持续提升 — Module 3 retro 反馈全部落实
- sessions_send 工作流顺畅 — retro → TASK → 实现 → retro 闭环

## 5. 时间 / 成本

| 阶段 | 耗时 | Bedrock 调用 |
|------|------|-------------|
| 代码实现（PR1+PR2） | ~10 min | 0（纯代码） |
| Golden CI Direct | 80s | 12 次 converse() |
| Golden CI Strands | 83s | 12 次 Agent 调用 |
| 总计 | ~40 min | ~24 次 LLM 调用 |

成本估算：24 × ~2K input tokens × $0.003/1K ≈ $0.14（全模块）

## 6. 给下个模块的 Top 3 建议 ⭐⭐⭐

### #1：Runner 的 Agent 实例复用策略需要提前决定

PolicyGuard 是"每次调用新建"，因为它只做一次判断。但 Runner 可能在一次实验的 5 个 phase 里多次调用 Agent（phase0 preflight → phase2 inject → phase4 recovery 判断）。如果每次新建 Agent，缓存完全浪费；如果复用 Agent，conversation history 会累积影响判断。

**建议**：TASK 里明确 Runner Agent 的生命周期 = 一次实验（5 phase），phase 之间复用实例但在实验结束后销毁。这样可以在 phase 间享受 cache_read，又不会跨实验污染。

### #2：Runner 是第一个真正 mutate 状态的模块 — dry-run 必须是 default

PolicyGuard 只读不写，所以 fail-closed = deny 就够了。Runner 会 mutate K8s/FIS 状态，fail-closed 的含义变了 — 不只是"不执行"，还可能需要"回滚已执行的部分"。

**建议**：TASK 硬约束加一条：`dry_run=True` 必须是 factory 的默认值，只有显式传入 `dry_run=False` 才真正执行。Golden CI 全部用 dry-run 跑。

### #3：Runner 的 Golden Set 不能只验 LLM 输出 — 还要验 K8s 状态变更

PolicyGuard 的 golden 只需要验 JSON 输出（decision + matched_rules）。Runner 的 golden 需要验：
1. LLM 决策输出（和 PolicyGuard 一样）
2. K8s/FIS 实际状态变更（pod 是否真的被 delete 了）
3. 恢复后状态（pod 是否真的回来了）

**建议**：Golden Set 分两层 — L1 纯 LLM 输出验证（mock K8s），L2 集成验证（真实 K8s dry-run namespace）

## 7. 给架构审阅猫的 TASK 模板修改建议

### 建议 1：产出清单的"可选"标注

当前模板的产出清单（§9）列了所有文件但没区分必须/可选。建议加 `[必须]` / `[推荐]` / `[可选]` 标签，避免实现者纠结是否要写 `rules_schema.py` 这种低 ROI 文件。

### 建议 2：Golden 行为约束的"断言强度"

`reasoning_must_include_any` 这种文本匹配断言在实践中 flaky。建议 TASK 模板区分：
- **硬断言**：`expected_decision`、`must_match_rules`（确定性，必须通过）
- **软断言**：`reasoning_must_include_any`（参考性，不 block CI）

### 建议 3：Agent 实例生命周期应成为 TASK 标准字段

Module 4 的 §6 写得很好。建议在 TASK 模板里加一个标准 section：
```
## N. Agent 实例生命周期
- 生命周期：[每次调用新建 | 单次实验复用 | 全局单例]
- 理由：...
- conversation_manager：[无 | SlidingWindow | 自定义]
```

这样每个模块都有明确的生命周期策略，不用猜。
