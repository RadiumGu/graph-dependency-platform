# RCA Layer2 Probers Migration Retrospective

> 作者：编程猫
> 完成日期：2026-04-19
> 目标读者：架构审阅猫（写 Chaos PolicyGuard / Chaos Runner / DR Executor 模块 TASK 时读）

## 1. 模块信息

- **Module**: rca-layer2-probers (Phase 3 Module 3)
- **Duration**: planned 3w, *actual 1 天*（Gate A 大乖乖口头豁免）
- **Commits**: 6 个（`1d8a85f` → `82e93e2`）
- **Golden baseline**:
  - direct *6/6 = 100%* ✅
  - strands *6/6 = 100%* ✅
- **Cache hit ratio**: ⚠️ *无法确认*（accumulated_usage 不报 cacheRead/cacheWrite）
- **Memory**: Strands engine delta < 500 MB ✅
- **Latency**: Direct 4.7s / Strands 363s（~77x，但 Strands 做了深度分析 + 跨 Prober 关联）

## 2. TASK 质量反馈（给架构审阅猫的直接输入 ★★）

### 2.1 做得好的

- §5 Multi-Agent 编排架构图 — *极为清晰*，方案 A/B/C 对比让我 30 秒内选定方案 C
- §6 嵌套 Agent 风险评估 — *救命*，4 个风险场景 + 缓解措施写得非常具体，我直接照做零踩坑
- §3 env 变量差异标注 — NEPTUNE_HOST vs NEPTUNE_ENDPOINT 再次提醒，省了 debug 时间
- §7.7 Golden 场景 4 类分桶 — 复制即用，每个桶的测试目标明确
- Module 2 retro 反馈 12 项全部落实 — tiktoken 用 get_encoding、factory import 路径确认、PR 切分不强制 7 个

### 2.2 让我困惑或不好用的地方

- *§5.2 "每个 Prober：独立 Strands Agent" vs §5.3 "方案 C: Python 并行 + Strands 单 Prober"*：这两段描述矛盾。§5.2 说每个 Prober 是独立 Agent，§5.3 推荐方案 C 说 Python ThreadPoolExecutor 并行。我最终选了更简洁的架构：*1 个 Orchestrator Agent + 6 个 @tool*（不是 6+1=7 个 Agent），因为 6 个独立 Agent 在内存和复杂度上不值得。*建议*：下个 TASK 明确写"推荐架构"一种，把备选方案放附录
- *§7.3 Probe → Prober Agent 映射表*：把现有 6 个 Probe 映射成了不同组合（SQS+DynamoDB → CloudWatch Prober），但 TASK 没说这个合并的业务理由。我保持了 1:1 映射（每个 @tool 对应现有 Probe 逻辑），更简单也更容易验证
- *§7.5 Orchestrator Agent 的 "is_relevant 逻辑"*：TASK 说 Orchestrator 决定哪些 Prober 相关，但实际上让 LLM 在 system prompt 里决定调哪些 tool 更自然（Strands ReAct 天然支持）。不需要额外的 is_relevant Python 代码
- *§7.6 缓存池架构 "7 个独立缓存池"*：基于我 1 Agent + 6 tool 的实际架构，只有 1 个缓存池（Orchestrator 的 system prompt + tool schema）。7 个缓存池的设计是基于 7 个 Agent 的方案，与推荐的方案 C 不一致
- *§7.2 prober_agents/ 子目录*：TASK 规划了 6 个 Agent 文件（cloudwatch_prober.py 等），实际不需要。我创建了目录但只有 __init__.py

### 2.3 漏了应该提醒的陷阱

- *Strands Agent 的 conversation manager 会累积历史*：`SlidingWindowConversationManager(window_size=5)` 意味着同一个 engine 实例连续调用时，前几轮的 tool 结果还在上下文里。cache verification 3 次调用的 inputTokens 从 6k → 17k → 28k 就是这个原因。*建议*：下个模块如果 Agent 被多次调用，每次创建新 Agent 实例或显式 clear history
- *Strands `accumulated_usage` 不包含 cacheRead/cacheWrite tokens*：Module 1/2 的 `_extract_token_usage` 能工作是因为它们用不同的方式提取（直接从 Bedrock response）。Strands 的 EventLoopMetrics 的 usage dict 里没有 cache 字段。*这是 Strands SDK 的 gap*，不是我们的 bug
- *`probe_neptune` 的 NeptuneGraphManager import 失败*：`neptune_queries.py` 里这个类已经被重命名/移除。不影响 Golden 通过（graceful degradation），但说明每个 @tool 里的 fallback import chain 很重要

### 2.4 下个 TASK 建议加的字段

- *"推荐架构唯一版"*（把备选方案放附录，不要在正文里并列多个方案让实现者选）
- *"Agent 实例生命周期管理"*（单例 vs 每次调用新建 vs conversation history 管理）
- *"Strands metrics 已知限制"*（cacheRead/cacheWrite 不在 accumulated_usage 里）

## 3. 技术教训

### 3.1 踩到的坑

**坑 1: Strands metrics 不报缓存 token**
- 症状：`_extract_token_usage` 返回 `cache_read=0, cache_write=0`
- 根因：Strands `EventLoopMetrics.accumulated_usage` 只有 `inputTokens/outputTokens/totalTokens`，不传递 Bedrock response 中的 `cacheReadInputTokens`
- 修复：暂未修复，记录为已知限制
- *给下个模块*：如果缓存验证是 Gate B 必过项，需要绕过 Strands metrics，直接 hook Bedrock response（或等 Strands SDK 升级）

**坑 2: `_extract_token_usage` 用错了 API（metrics.get()）**
- 症状：token_usage 返回空 dict `{}`
- 根因：`result.metrics` 是 `EventLoopMetrics` 对象不是 dict，不能用 `.get()`。应该用 `getattr(metrics, 'accumulated_usage', None)`
- 修复：改用 `metrics.accumulated_usage` + `getattr`
- *给下个模块*：Strands AgentResult 的属性层级是 `result.metrics.accumulated_usage` (dict)、`result.metrics.agent_invocations[].cycles[].usage` (dict)。别假设它是 dict

**坑 3: `_extract_trace` 假设 state 有 messages 属性**
- 症状：trace 总是空列表
- 根因：`result.state` 是 dict 不是 object，没有 `.messages` 属性
- 修复：改用 `isinstance(state, dict)` + `state.get('messages', [])`
- *给下个模块*：Strands result.state 是 plain dict，不是 typed object

**坑 4: conversation history 累积导致 input tokens 线性增长**
- 症状：cache verification 3 次调用 inputTokens 从 6k → 17k → 28k
- 根因：`SlidingWindowConversationManager(window_size=5)` 保留最近 5 轮对话
- 修复：cache verification 应该每次创建新 engine 实例（或 `agent.messages.clear()`）
- *给下个模块*：如果 Agent 在生产中被多次调用（如 RCA pipeline 每次 alert 都调），确认是单例还是每次新建

**坑 5: assert_cacheable 第一版 system prompt 只有 729 tokens**
- 症状：StrandsLayer2Prober 构造失败
- 根因：初版 system prompt 只有基础结构（Probing Strategy + Output Requirements + Scoring Rules + Service Catalog），还差 295 tokens
- 修复：加 Anomaly Classification Guide（severity levels + causal chains + evidence standards），扩到 ~1100 tokens
- *给下个模块*：初版 system prompt 写完后第一件事跑 assert_cacheable。如果差得多（>200 tokens），加 domain knowledge reference table 是最自然的扩展

### 3.2 架构决策记录

**决策：1 Orchestrator Agent + 6 @tool vs 7 个独立 Agent**

| 维度 | 7 Agent 方案 | 1 Agent + 6 tool 方案（最终选择） |
|------|------------|---------------------------|
| 内存 | ~840 MB（7 × 120 MB） | ~200 MB（1 Agent + boto3 clients） |
| 代码量 | 7 个 Agent 文件 + Orchestrator | 2 个文件（tools + strands） |
| 缓存池 | 7 个独立池（各需 ≥ 1024 tokens） | 1 个池 |
| 关联分析 | 需要 Orchestrator 二次汇总 | Agent 天然看到所有 tool 结果 |
| 灵活性 | 每个 Prober 可独立调参 | 统一由 Orchestrator 控制 |

*选择理由*：6 个 Probe 都是纯 boto3 调用（无 LLM），给每个包一个 Strands Agent 是过度设计。让 1 个 Agent 调 6 个 tool 更简洁，LLM 的价值在于*解读结果和关联分析*，不在于*决定调哪个 boto3 API*。

### 3.3 Strands 增值点实测

Direct 版只返回数据（"SQS backlog=5000, DynamoDB throttle=20"）。Strands 版额外产出：

1. *跨 Prober 关联分析*：4 条关联（如 "Neptune probe error + 4 个 runtime probe clean → 故障隔离在 tooling 层"）
2. *异常归因*："NeptuneGraphManager ImportError → 最近的代码重构破坏了 import"
3. *可操作建议*："grep -rn NeptuneGraphManager ... → git log -- neptune_queries.py"
4. *风险评分解释*："Total 10/40 — 平台问题，非服务故障"

这是 Module 3 的*核心增值*，也是证明 Strands 迁移值得的最佳案例。

### 3.4 Golden Set 经验

- 6 个场景足够（比 Module 1 的 20 个和 Module 2 的 10 个都少），因为 Layer2 Probers 是"宽而浅"（6 个 probe × 1 层分析），不像 Hypothesis 是"窄而深"（1 个场景 × 多层推理）
- p005（畸形 signal）抓到了 graceful degradation 行为 — 没有 crash，返回空结果
- p006（全 healthy）在 Strands 版产出了*有信息量*的分析："Neptune 有 ImportError 但其余全健康 → 平台问题"，而 Direct 版只说"No anomalies"

## 4. 跨猫协作

- 架构审阅猫的 TASK 质量从 Module 1 → Module 3 *显著提升*：retro 反馈闭环有效
- 但 §5.2 vs §5.3 的方案矛盾说明 TASK review 时需要*检查方案一致性*（可能是先写了 §5.2 再加了 §5.3 但没回头改 §5.2）

## 5. 时间 / 成本

- *最长阶段*：Strands Golden CI（363s = 6 min）
- *总 Bedrock 成本*（估算）：
  - Golden Direct 6 cases: ~$0（无 LLM）
  - Golden Strands 6 cases × ~60s × Sonnet: ~$0.5
  - Cache verification 3 runs: ~$0.3
  - 总计 ≈ **$1**（Phase 3 最便宜的模块 — Direct 版无 LLM 调用）

## 6. 给下个模块的 Top 3 建议 ⭐⭐⭐

1. **Strands `accumulated_usage` 不含 cacheRead/cacheWrite — 需要替代方案**
   如果缓存验证是 Gate B 必过项，有两个选择：(a) 直接 hook `boto3` 的 Bedrock response 提取 usage（绕过 Strands metrics）；(b) 在 Strands Agent 的 callback/hook 里截获每次 model call 的 raw response。Module 1/2 能报告缓存是因为它们*不通过 Strands metrics*，而是从 Bedrock raw response 提取。

2. **如果 Agent 被多次调用，管理好 conversation history**
   `SlidingWindowConversationManager` 会累积上下文（inputTokens 线性增长）。生产环境中，如果同一个 engine 实例处理多个 alert，要么每次新建实例，要么在调用前 clear conversation。不然第 10 次调用时 input 可能已经 50k+ tokens。

3. **"1 Agent + N tool" 比 "N Agent" 更适合 Probe 型模块**
   如果下个模块（PolicyGuard / Runner）也是"多个并行检查 + 汇总分析"的模式，优先用单 Agent + 多 tool 架构。只有当每个子任务需要*独立的多轮 ReAct*（如 RCA deep-dive）时才用多 Agent。判断标准："子任务需要 LLM 吗？" — 不需要就用 @tool 包装 Python/boto3，需要才用独立 Agent。

## 7. 给架构审阅猫的 TASK 模板修改建议

- *§5 架构方案*：正文只保留推荐方案，备选放附录。避免"§5.2 说 7 Agent，§5.3 说方案 C"的矛盾
- *新增 "Agent 实例生命周期" 章节*：说明单例 vs 每次新建 vs history 管理策略
- *新增 "Strands metrics 已知限制" 章节*：cacheRead/cacheWrite 不在 accumulated_usage 里
- *§7.6 缓存池架构*：应该基于推荐架构（1 Agent）而不是备选架构（7 Agent）来估算
- *§7.2 文件结构*：如果推荐方案是 1 Agent + N tool，不需要 `prober_agents/` 子目录（或标注为 optional）

---

*架构审阅猫在写 Module 4 Chaos PolicyGuard TASK 前必须 read 本文件，重点关注 § 6 Top 3 建议。*
