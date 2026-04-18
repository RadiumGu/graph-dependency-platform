# TASK: Smart Query L2 POC — Bedrock Prompt Caching 集成

> **任务归属**: 编程猫
> **发起**: 大乖乖 + 架构审阅猫
> **日期**: 2026-04-18
> **范围**: 给 Direct 和 Strands 两个引擎接入 Bedrock Prompt Caching
> **预计工作量**: 2-3 人日
> **成功标志**: 两引擎稳态 cache hit ratio ≥ 60%，月度 Bedrock 账单下降 ≥ 35%

---

## 0. 上下文

Smart Query L1 POC 已合并（`42933cf` → `f4d7665`），Direct 20/20 + Strands 20/20 双引擎 baseline 达标。

L1 POC 完成后发现两件事需要优化：
1. Strands 版 p50 延迟 9.3s / p99 19s，token 是 Direct 的 ~3x，**长期成本不可持续**
2. Direct 版 system prompt（schema + 30 few-shot + 规则）~4700 tokens，每次 query 重复发送 — 浪费

**Bedrock Prompt Caching** 正好解决这个：前缀稳定的 system prompt 缓存，命中时 input 成本降到 10%。

核心数据（Sonnet 4.6 价格）：
- 无缓存 input: $3.00 / 1M tokens
- Cache write: $3.75 / 1M tokens（125% of input，一次性成本）
- Cache read: $0.30 / 1M tokens（10% of input）

**预估节省**：
| 引擎 | 无缓存/月 | 有缓存/月 | 节省 |
|------|-----------|-----------|------|
| Direct | ~$495 | ~$306 | **38%** |
| Strands | ~$1080 | ~$510 | **52%** |

（按 50 DAU × 10 query/day × 30 day = 15000 queries 估算）

---

## 1. 工作目录

```
/home/ubuntu/tech/graph-dependency-platform
```

---

## 2. 必读（按顺序）

1. `experiments/strands-poc/report.md` — L0 Spike 验证结果，特别是 7.7 节"Phase 3 启动前补作业"
2. `rca/neptune/nl_query_direct.py` — Direct 引擎，要改 `_generate_cypher`
3. `rca/neptune/nl_query_strands.py` — Strands 引擎，`_build_agent` + `_extract_token_usage`
4. `rca/engines/strands_common.py` — `build_bedrock_model` 构造函数
5. `rca/neptune/strands_tools.py` — 3 个 @tool 定义
6. `tests/test_golden_accuracy.py` — Golden CI 框架，需扩展缓存命中率列
7. `tests/golden/BASELINE-direct.md` / `BASELINE-strands.md` — 当前基线
8. AWS 官方文档：
   - https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html
   - https://docs.anthropic.com/en/docs/build-with-claude/prompt-caching（Anthropic schema 细节）

---

## 3. 任务范围（严格限定）

本任务*只做 Prompt Caching 集成*。不扩展 Golden Set，不改 Streamlit UI 主体逻辑，不启动 Phase 3 其他模块迁移。

---

## 4. 产出清单

### 4.1 Direct 引擎改造

修改 `rca/neptune/nl_query_direct.py`：

- `_generate_cypher`：把 `system` 从字符串改为带 `cache_control` 的 list 格式
- 解析 `usage.cache_read_input_tokens` / `usage.cache_creation_input_tokens`
- 扩展 `_last_tokens` 新增 `cache_read` / `cache_write` 字段
- `_summarize` **不加缓存**（每次问题 + 结果都不同，写入是浪费）

关键代码片段：

```python
resp = self.bedrock.invoke_model(
    modelId=model_id,
    body=json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 1024,
        "system": [
            {
                "type": "text",
                "text": self.system_prompt,
                "cache_control": {"type": "ephemeral"},  # ← 新增
            }
        ],
        "messages": [{"role": "user", "content": question}],
    }),
)

body = json.loads(resp['body'].read())
usage = body.get('usage') or {}
cache_read = int(usage.get('cache_read_input_tokens', 0) or 0)
cache_write = int(usage.get('cache_creation_input_tokens', 0) or 0)
# 累计到 self._last_tokens，新增 cache_read / cache_write 子字段
```

### 4.2 Strands 引擎改造

修改 `rca/engines/strands_common.py` 的 `build_bedrock_model`：

**步骤 1**（先验证）：读 Strands 1.36.0 `BedrockModel` 源码，确定是否原生支持 `cache_prompt` / `cache_tools` 参数。路径：

```
/home/ubuntu/tech/graph-dependency-platform/experiments/strands-poc/.venv/lib/python3.12/site-packages/strands/models/bedrock.py
```

**步骤 2**（按支持情况分支）：

- 若 Strands 原生支持 `cache_prompt`：
  ```python
  return BedrockModel(
      model_id=model_id or DEFAULT_MODEL,
      region_name=region or DEFAULT_REGION,
      cache_prompt="default",
      cache_tools="default",
  )
  ```

- 若 Strands 1.36 还没暴露：
  - 方案 A：用 `additional_request_fields` 透传 Converse API 的 `cachePoint`（查 Converse API 官方 schema）
  - 方案 B：subclass `BedrockModel`，在 `_format_request` 里手动注入 `cachePoint`

选方案前*先写一个 10 行实验脚本*在 `experiments/strands-poc/` 下验证：发两次相同问题，看 `result.metrics.accumulated_usage` 里有没有 `cacheReadInputTokens` 非 0 字段。只有实验通过才改 strands_common.py。

### 4.3 Token usage 扩展

修改 `rca/engines/base.py` 的文档字串（抽象层），`NLQueryBase.query()` 返回 dict 的 `token_usage` 字段扩展：

```python
"token_usage": {
    "input": int,        # 不含缓存的常规 input tokens
    "output": int,
    "total": int,        # input + output + cache_read + cache_write
    "cache_read": int,   # ← 新增：命中缓存的 tokens
    "cache_write": int,  # ← 新增：本次写入缓存的 tokens
} | None,
```

修改两个引擎使返回的 dict 带这些字段。

### 4.4 Golden CI 扩展

修改 `tests/test_golden_accuracy.py`：

- `BASELINE-<engine>.md` 新增 metric 行：`Avg Cache Hit Ratio`
- 公式：`cache_read / (cache_read + input)`（input 是"非缓存"部分）
- 只在 token_usage 有 cache_read 时统计（否则 "N/A"）

修改 `tests/test_nlquery_shadow.py`：

- 汇总部分新增缓存命中率对比列

### 4.5 文档更新

修改 `experiments/strands-poc/report.md`：

- 在 "7.7 Phase 3 启动前补作业" 里把"主环境 strands 依赖管理正式化"之前加一条新完成项：`Prompt Caching 接入（2026-04-XX commit XXXXX）`
- 新增 "8. L2 Prompt Caching 效果" 章节：
  - 集成前/后的延迟对比
  - 月度成本下降数据（基于 Golden CI 命中率外推）
  - 陷阱（Opus 独立缓存池 / ReAct 中间 tool_result 不影响 system cache 等）

修改 `experiments/strands-poc/CUSTOMER-REFERENCE-nl-to-graph-query.md`：

- 第 9 节"成本参考"新增 "9.1 Prompt Caching 降本" 小节
- 给客户展示"开启缓存后月度账单下降 40-50%"

---

## 5. 硬约束

1. ⚠️ **`_summarize` 绝不加缓存** — 每次问题 + 结果都不同，只会导致 cache write 浪费
2. ⚠️ **Cache point 只放在 system prompt 和 tool schema** — 不要放 user message / messages 数组里（前缀不稳定）
3. ⚠️ **TTL 用默认 5 分钟** — 不升级到 1 小时（贵 2x，Smart Query 不需要）
4. ⚠️ **不动 `query_guard.py`** — 和缓存无关
5. ⚠️ **不改 `profiles/petsite.yaml`** — 缓存是透明优化，profile 内容不变
6. ⚠️ **Strands 版如果 Strands SDK 不支持 cache，不要 hack 太深** — 先提 upstream issue，本地 patch 保守实现
7. ⚠️ **Cache 集成不能破坏现有 token_usage 统计** — `input` / `output` / `total` 语义保持兼容；新字段 `cache_read` / `cache_write` 加在尾部
8. ⚠️ **Profile 改动导致 cache 失效是正常的** — 不要用锁避免 cache miss，否则 profile 改了热 reload 不生效

---

## 6. 验证步骤

### 6.1 单元测试

```bash
cd rca && PYTHONPATH=.:.. pytest ../tests/ -v
```
确保现有测试全过，没有 regression。

### 6.2 Direct 引擎缓存生效性（本地脚本）

在 `experiments/strands-poc/` 新增 `verify_cache_direct.py`：
- 构造 `DirectBedrockNLQuery`
- 连续跑 3 次同一问题 `"petsite 依赖哪些数据库？"`
- 第 1 次应有 `cache_write > 0`、`cache_read == 0`
- 第 2/3 次应有 `cache_read > 0`、`cache_write == 0`
- 如第 2 次还是 cache_write > 0 → 说明 cache_control 没生效，停手排查

### 6.3 Strands 引擎缓存生效性

同 6.2，但针对 `StrandsNLQueryEngine`。

### 6.4 Golden CI（真调 Bedrock + Neptune）

```bash
cd rca && PYTHONPATH=.:.. RUN_GOLDEN=1 NLQUERY_ENGINE=direct  pytest ../tests/test_golden_accuracy.py -v
cd rca && PYTHONPATH=.:.. RUN_GOLDEN=1 NLQUERY_ENGINE=strands pytest ../tests/test_golden_accuracy.py -v
```

### 6.5 Shadow 对比

```bash
cd rca && PYTHONPATH=.:.. RUN_GOLDEN=1 pytest ../tests/test_nlquery_shadow.py -v -s
```

观察缓存命中率 + 每 case 的 cache_read 分布。

---

## 7. 验收门槛

| 指标 | 门槛 |
|------|------|
| Direct golden | 维持 ≥ 19/20（不退化）|
| Strands golden | 维持 ≥ 19/20（不退化）|
| Direct cache hit ratio（稳态，第 2 条开始） | ≥ 70% |
| Strands cache hit ratio（稳态）| ≥ 60% |
| Direct 月度成本外推 | 下降 ≥ 35% |
| Strands 月度成本外推 | 下降 ≥ 45% |
| 现有 `token_usage.input` / `.output` / `.total` 字段语义 | 保持兼容 |

**任一不达标 → 不 merge**。

---

## 8. Git 提交策略

拆成 *4 个小 PR*：

| # | PR | 范围 |
|---|-----|------|
| 1 | `feat(direct): enable Bedrock prompt caching on system prompt` | 只改 nl_query_direct.py + token_usage schema |
| 2 | `feat(strands): enable Bedrock prompt caching via BedrockModel` | 只改 strands_common.py + strands engine token extract |
| 3 | `test(smart-query): cache hit ratio in golden + shadow` | 测试扩展 |
| 4 | `docs(strands-poc): L2 prompt caching report section` | report.md + CUSTOMER-REFERENCE 更新 |

每个 PR 单独 review。

---

## 9. 不要做的事

1. ❌ 不扩展 Golden Set cases（超出 L2 范围）
2. ❌ 不做 Streamlit UI 改动（用户看不到缓存命中，不必暴露）
3. ❌ 不动 Opus / Sonnet 动态升级逻辑（Wave 5）
4. ❌ 不做 session memory（Phase 3 规划项）
5. ❌ 不并行迁其他模块
6. ❌ 不在 `_summarize` 加缓存（见硬约束 1）
7. ❌ 不追求 "1-hour TTL" 高级特性
8. ❌ 不把缓存做成 env 开关（默认开就好，不增加配置复杂度）

---

## 10. 踩坑提醒

基于架构审阅猫的调研总结：

1. **Cache invalidation** — Profile YAML 改 few-shot → system prompt 变 → 缓存失效，下一次付 125% write 成本。正常行为。
2. **Opus 独立缓存池** — Wave 5 切 Opus 时第一次是 cache miss；考虑给 Opus 也建 warm-up 脚本。
3. **ReAct 中间 tool_result 不影响 system cache** — 只要 cache point 放在 system / tools 处，messages 数组里的变化不会让前缀 miss。
4. **最低缓存大小** — Sonnet/Opus 最低 1024 tokens。如果 profile 精简到 system prompt < 1024 tokens 会静默失效。加 assert 防御：
   ```python
   assert len(self.system_prompt) > 3000, "system prompt 太短会让 prompt caching 失效"
   ```
5. **多租户 profile 切换** — 每个 profile system prompt 不同 → 各自独立缓存池，OK，不用特别处理。
6. **Strands 的 cache_tools 价值** — 3 个 `@tool` 的 JSON schema 约 800 tokens，每次 ReAct 都重发。`cache_tools="default"` 能把这部分也缓存掉。

---

## 11. 失败处理

- **cache_read 始终为 0** → 先检查 `system` 字段结构是否正确（必须是 `[{type, text, cache_control}]`）；再看模型 id 是否支持（Sonnet 3.5+ / Opus 4+ / Haiku 3.5+ 都支持）
- **Strands 里 cachePoint 不生效** → 先用 `print(agent.model._format_request(...))` 打出完整 body 对比 AWS Converse API 文档；确认 cachePoint 在 system 数组而不是嵌套错位
- **同一错误 3 次失败** → 停手，写到 progress 日志，通知大乖乖

---

## 12. 完成标志

1. ✅ 4 个 PR 合并到 main
2. ✅ 验证步骤 6.1-6.5 全部达标
3. ✅ Golden CI 的 BASELINE-*.md 新增 `Avg Cache Hit Ratio` 列并有真实数字
4. ✅ `report.md` L2 章节写好，含前后延迟/成本对比
5. ✅ `CUSTOMER-REFERENCE-nl-to-graph-query.md` 的 9.1 节写好
6. ✅ 新增的 `verify_cache_direct.py` / `verify_cache_strands.py` 留在 `experiments/strands-poc/`，将来做 Bedrock 版本升级时可复用

---

## 13. 参考数据（方便你对账）

**架构审阅猫的估算**（未实测，供你校验）：

```
Direct 单次 query（~6k input + 1k output）:
  无缓存: $0.033
  有缓存稳态: $0.0204 (-38%)

Strands 单次 query（~18k input，ReAct 3 轮 + 1.2k output）:
  无缓存: $0.072
  有缓存稳态: $0.034 (-52%)
```

跑完 Golden CI 后，用 `BASELINE-*.md` 里的 `Total tokens` + `Avg Cache Hit Ratio` 反推真实单价，对比上面估算是否合理。若实测大幅偏离（比如 Direct 节省只有 20%），需要查是哪里浪费（可能 system prompt 太短、或 few_shot 更新太频繁）。

---

**遇到任何不确定的地方，先在 Slack 里问大乖乖或架构审阅猫，不要自己拍脑袋决定。**
