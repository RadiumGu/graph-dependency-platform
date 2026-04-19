# ADR: LearningAgent Migration to Strands Agents

- **Status**: Accepted
- **Date**: 2026-04-19
- **Author**: 编程猫
- **Module**: chaos/code/agents/learning_agent.py → learning_direct.py + learning_strands.py

## Context

LearningAgent 是混沌工程闭环的核心模块：分析实验结果 → 生成改进建议 → 迭代假设 → 更新图谱 → 输出报告。
Phase 3 Module 2 将其从"直调 Bedrock"迁移到 Strands Agents 框架。

## Decision

### 架构选择
- **只有 `generate_recommendations()` 走 Strands ReAct**，其余 4 个方法（analyze / iterate_hypotheses / update_graph / generate_report）保持纯 Python，delegate 给 DirectBedrockLearning
- 原因：LearningAgent 只有 1 个 LLM 调用点，其余全是 Python 聚合/Gremlin 写入。ReAct 开销不值得用在纯计算上

### 缓存策略
- 使用 `CacheConfig(strategy="auto")`（不用 deprecated `cache_prompt`）
- System prompt 扩展到 1200+ tokens（加 fault type reference table）以通过 1024 token 缓存下限
- `assert_cacheable()` 使用 tiktoken `get_encoding("cl100k_base")`，不按 chars 估算

### Neptune Helper 解耦
- 新建 `chaos/code/runner/neptune_helpers.py`，抽取公共 Gremlin 查询
- `hypothesis_tools.py` 的 `query_infra_snapshot` 不再依赖 `DirectBedrockHypothesis._query_infra_snapshot` 私有方法

### Golden Set 设计
- 10 个场景，4 类分桶（核心服务 / 特殊后端 / 错误输入 / 边界）
- 行为约束式验证（不硬编码 expected 精确列表）
- 从 DynamoDB 导出真实 fixture

## Results

| 指标 | Direct | Strands | Gate B 要求 |
|------|--------|---------|------------|
| Golden | 10/10 | 10/10 | ≥ 9/10 |
| 集成测试 | ✅ | — | 通过 |
| assert_cacheable | ✅ | ✅ | 两端均 ≥ 1024 tokens |

## Consequences

- `learning_agent.py` 现在是 shim（re-export），向后兼容
- `LEARNING_ENGINE=direct|strands` env 切换
- 1 周灰度后冻结 `learning_direct.py`
