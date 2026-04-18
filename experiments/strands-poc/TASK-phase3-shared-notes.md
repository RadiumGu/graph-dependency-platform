# Phase 3 模块迁移 TASK 说明（含 Prompt Caching）

> **用途**: Phase 3 每个模块的迁移 TASK 共享说明，避免重复
> **适用范围**: HypothesisAgent / LearningAgent / RCA Layer2 Probers / Chaos PolicyGuard / Chaos Runner / DR Executor
> **日期**: 2026-04-18（Smart Query L2 Prompt Caching 完成后更新）
> **状态**: 每个具体模块的 `TASK-<module>.md` 在启动该模块迁移时单独发布，引用本文件作为共享规范

---

## 0. 使用方法

Phase 3 启动时，每个模块对应一份 `TASK-<module>.md`，结构参照 `TASK-L1-smart-query.md`，但**共享以下内容来源于本文件**：

- 接口规范 — 见 `migration-strategy.md § 5`
- Prompt Caching 集成 — 见下文 § 2（本文件）
- Git 策略 — 见 `migration-strategy.md § 4`
- 冻结 / 删除日 CI — 见 `migration-strategy.md § 5`
- 度量门槛 — 见 `migration-strategy.md § 6`

每个具体模块的 TASK 只需要写该模块**差异化**的部分：业务范围、golden set、硬约束、引擎特定陷阱。

---

## 1. Prompt Caching 是每个模块的默认动作

基于 Smart Query L2 POC（2026-04-XX 完成）验证的经验，**所有 Phase 3 模块**在迁移时默认集成 Prompt Caching，**不作为可选项**。理由：

- Smart Query L2 实测节省 Direct 38% / Strands 52% 月度成本
- Phase 3 各模块前缀比 Smart Query 更稳定（业务规则 YAML 化）
- RCA Layer2 / HypothesisAgent 是多次 LLM 调用场景，缓存杠杆更大
- 不装缓存就上线 = 账单翻倍但换回的可观测性与 ReAct 能力被成本抵消，*得不偿失*

---

## 2. 各模块 Prompt Caching 预估收益

| 模块 | 稳定前缀大小 | 每次事件调用次数 | 预估节省 | 备注 |
|------|-------------|------------------|---------|------|
| HypothesisAgent | 5-8k tokens | 3-5 次 LLM call | *40-55%* | 多轮生成 + 筛选 + 详化 |
| LearningAgent | 3-5k tokens | 1-2 次 LLM call | *20-30%* | 调用频次低，稳定期收益中等 |
| RCA Layer2 Probers (6 个) | 6-10k × 6 Agent | 6 × 2-4 次 | *50-60%* | **最大杠杆**，multi-agent 并行 |
| Chaos PolicyGuard | 2-3k tokens | 1 次 | *15-25%* | **注意下限 1024 tokens**，如果规则不够长要补 |
| Chaos Runner | 3-5k 稳定 + 可变 | 每 phase 2-3 次 | *20-30%* | **部分缓存**，拆稳定段 + 本次配置 |
| DR Executor | 4-6k 稳定 + 可变 | 每步 2 次 | *20-35%* | **部分缓存**，拓扑规则稳定 + plan 可变 |

---

## 3. 按模块的 Prompt Caching 设计要点

### 3.1 HypothesisAgent

**缓存对象**：
- Neptune 图 schema（从 profile YAML 读）
- 历史故障目录 + 典型假设模式
- Agent 调用规则

**不缓存**：
- 本次故障的上下文（service_name / affected_region）
- 已有假设 trace（用于筛除重复）

**特殊点**：多轮生成时（先列 10 个候选 → 筛选 3 个 → 详化）每轮 system 都相同，缓存价值大。

### 3.2 LearningAgent

**缓存对象**：
- coverage schema（维度定义 + 评估规则）
- 分析模板

**不缓存**：
- 本轮 coverage snapshot（JSON 数据）
- 上轮 verdict 历史

**特殊点**：调用频次低（每轮实验 1 次），缓存在稳定期才有效；考虑与 HypothesisAgent 共享 base system prompt（如果模型相同可复用缓存池）。

### 3.3 RCA Layer2 Probers（6 个并行 Prober）

**缓存对象**（每个 Prober 独立）：
- Prober 职责定义（CloudWatch / X-Ray / Neptune / Logs / Deployment / Network）
- 事件分类 schema
- Service catalog 引用

**不缓存**：
- 本次事件详情
- 来自其他 Prober 的 observation

**特殊点**：
- **multi-agent 并行调用** = 6 个缓存池同时工作，需要监控每个 Prober 的命中率
- 使用 Strands 的 `agent.as_tool(agent_b)` 做 orchestration 时，子 Agent 的缓存要独立计算
- 事件高峰期（如一次区域故障触发 50 个事件）缓存命中率预期能达 80%+

### 3.4 Chaos PolicyGuard

**缓存对象**：
- 全部策略规则（YAML → system prompt）
- 业务时间窗 / namespace 白名单

**不缓存**：
- 本次实验 metadata（名称 / 目标 / fault type）

**⚠️ 陷阱**：如果策略规则 YAML 较短（< 1024 tokens），Sonnet 的最低缓存阈值不达标，*缓存会静默失效*。解决：
- 方案 A：在 system prompt 里加详细的规则说明文档（让 LLM 理解语义），达到 2-3k tokens
- 方案 B：不加缓存，单次调用成本本来就低

### 3.5 Chaos Runner

**缓存对象**：
- 7-phase 标准流程定义
- K8s / FIS 操作语义
- Stop condition 规则

**不缓存**：
- 本次实验配置（target / params / duration）
- 实时 probe 结果

**⚠️ 拆分要求**：
```python
"system": [
    {"type": "text", "text": STABLE_CHAOS_FRAMEWORK,
     "cache_control": {"type": "ephemeral"}},
    {"type": "text", "text": this_experiment_config},  # 不缓存
]
```

### 3.6 DR Plan Execution

**缓存对象**：
- 拓扑排序规则
- RTO/RPO 计算公式
- Failure strategy 语义（ROLLBACK / RETRY / MANUAL / SKIP / ABORT）
- Phase 转换规则

**不缓存**：
- 本次 DR plan 具体 steps
- 当前 step 状态 / 前面 step 的结果

**⚠️ 拆分要求**：同 3.5。

---

## 4. 每个模块 TASK 必备章节模板

每个 `TASK-<module>.md` 在写时要包含以下段落（内容各异）：

```markdown
## X.Y Prompt Caching 集成（引用规范 § 2 + 本节要点）

### 参考实现
- `rca/neptune/nl_query_direct.py` — Direct 缓存参考
- `rca/engines/strands_common.py` — Strands 缓存参考

### 本模块缓存对象
- （列出稳定前缀）

### 本模块不缓存的对象
- （列出每次不同的部分）

### 拆分策略（仅 Chaos Runner / DR Executor 需要）
- 稳定段放 `system[0]` + cache_control
- 可变段放 `system[1]` 不加 cache_control

### 本模块预期缓存命中率（稳态）
- ≥ 50%（RCA Layer2 / Hypothesis ≥ 60%）

### 本模块特殊陷阱
- （列出该模块独有的坑）
```

---

## 5. 每模块 Golden CI 扩展

每个模块的 `BASELINE-<engine>.md` 都要加：

```markdown
| Metric | Value |
|--------|-------|
| ...   | ...   |
| Avg cache hit ratio (stable-state) | XX% |
| Monthly cost (with caching) | $YYY/month |
| Monthly cost (no caching, estimated) | $ZZZ/month |
| Caching savings | AA% |
```

---

## 6. 和 Smart Query L2 的衔接

Smart Query L2 的 caching 实现被后续模块参考。Phase 3 启动前确认以下事实：

- [ ] `rca/engines/strands_common.py` 的 `build_bedrock_model(cache_prompt=..., cache_tools=...)` 已稳定工作 4 周
- [ ] Golden CI 的 cache hit ratio 列已规范化（`tests/test_golden_accuracy.py` 统一处理）
- [ ] CloudWatch 看板有"Agent 缓存效果"页，可按模块维度查看
- [ ] `NLQueryBase.query()` 返回 dict 的 `token_usage.{cache_read, cache_write}` 字段规范已固化

如以上任一未满足 → 先在 Smart Query 上补完，再启动 Phase 3。

---

## 7. 不要做的事

1. ❌ **不要把 Prompt Caching 做成模块可选功能** — 默认开，不接受"这次先不做缓存"
2. ❌ **不要让每个模块重复写缓存接入代码** — 复用 Smart Query L2 的 helper 函数
3. ❌ **不要只测"功能正确"不测"命中率"** — 6.3 门槛里命中率是硬指标
4. ❌ **不要无脑缓存 Chaos Runner / DR Executor 的全 system prompt** — 必须拆稳定段 + 可变段
5. ❌ **不要在 Phase 3 跑一半发现缓存不生效才回头修** — 每模块 Week 1 必须跑 `verify_cache_<module>.py` 脚本证明缓存命中

---

## 8. 参考文档

- `TASK-L2-prompt-caching.md` — Smart Query L2 缓存接入完整任务（模板）
- `migration-strategy.md § 6.4` — 整体 caching 集成规范
- `report.md § 8` — Smart Query L2 实测效果（Phase 3 启动前补充）
- AWS 官方：<https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html>

---

*本文件由 Phase 3 各模块 TASK 共享引用，维护时一处改，全部受益。*
