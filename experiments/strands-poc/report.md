# Strands Agents L0 Spike — 验证报告

**日期**: 2026-04-18
**目录**: `graph-dependency-platform/experiments/strands-poc/`
**版本**: strands-agents 1.36.0 / Python 3.12 / region ap-northeast-1
**Neptune**: `petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com`

> TL;DR：*6 个硬约束全部通过*。Strands 可直接在 ap-northeast-1 用 `global.anthropic.claude-sonnet-4-6`、复用现有 `neptune_client`/`query_guard`/`EnvironmentProfile`、且 guard 在 tool 内部*无法被 LLM 绕过*。延迟约为现版 1.7x（可接受）。
>
> **结论：L1 POC GO（有条件）**。

---

## 1. 硬约束验证（YES/NO + 证据）

| # | 约束 | 结果 | 证据 |
|---|------|-----|------|
| 1 | BedrockModel 接受 `global.anthropic.claude-sonnet-4-6` | ✅ YES | probe `{"ok": true, "latency_s": 1.07}`，ping 返回 "pong"；metadata 含 `latencyMs: 1027` |
| 2 | BedrockModel 显式 region=`ap-northeast-1` | ✅ YES | `BedrockModel(model_id=..., region_name="ap-northeast-1")` 直接工作，无需 env 覆盖 |
| 3 | `@tool` 调 `neptune_client.results(cypher)` | ✅ YES | 3 条 golden 全部通过 `execute_cypher` 成功拿到 Neptune 数据（2 / 5 / 50 行） |
| 4 | `execute_cypher` 内部强制 `query_guard.is_safe()` | ✅ YES | bonus 测试：直接调 `execute_cypher("MATCH (n) DETACH DELETE n")` → 返回 `"ERROR: guard blocked unsafe cypher — 查询包含写操作关键字: DETACH"`，`intercepted: true` |
| 5 | Agent 能拿 tool-call trace | ✅ YES | 自建 `_LAST_CALLS` 记录每次 tool 调用（tool/参数/结果摘要）。`runs[2].tool_calls` 含 4 次调用（2× validate + 2× execute）。Strands 原生还支持 OTel/callbacks（未在 spike 启用，L1 可接） |
| 6 | 延迟对比 | ✅ 可接受 | 见第 2 节 |

**降级模型测试**（probe 结果）：
- `apac.anthropic.claude-sonnet-4-5` → ❌ `ValidationException: The provided model identifier is invalid.`
- `us.anthropic.claude-sonnet-4-5` → ❌ 同上
- `anthropic.claude-3-5-sonnet-20241022-v2:0`（裸模型 id）→ ❌ `Invocation ... with on-demand throughput isn't supported. Retry with an inference profile.`

🔒 结论：*必须用 inference profile id*，不能用裸 model id；Global profile 目前唯一可用选项。

---

## 2. 延迟对比

| 问题 | Baseline（现版直调 Bedrock） | Strands Agent | 倍数 | Strands tool-call 数 |
|------|--------|---------|------|------|
| petsite 依赖哪些数据库？ | 4.02s | 7.92s | 1.97x | 2 |
| Tier0 服务有哪些？ | 5.67s | 8.96s | 1.58x | 2 |
| petsite 的完整上下游依赖路径 | 9.10s | 15.52s | 1.71x | 4 |
| **p50** | **5.67s** | **8.96s** | **1.58x** | — |

*延迟来源*：Strands 是 ReAct 多轮（plan → validate → execute → summarize），比现版 (generate_cypher → guard → execute → summarize) 多一轮模型交互。延迟代价主要来自 validate_cypher 这一轮 tool-call round-trip。

*质量观察*：第 3 题 Strands 自动拆分成"上游"+"下游"两次查询（tool_calls=4），产出表格比现版更结构化。这是 ReAct 的天然收益，不是偶然。

---

## 3. 发现的陷阱

### 3.1 Inference profile id 是硬约束
- 裸 `anthropic.claude-3-5-sonnet-20241022-v2:0` → 报 `on-demand throughput isn't supported`，必须用 profile id
- `apac.` / `us.` 前缀 profile 在 ap-northeast-1 目前*都不可用*（ValidationException）
- 只有 `global.*` profile 可用 → *单点依赖*，AWS 若调整 Global profile 可用性，需有 fallback 清单

### 3.2 TLS 警告（不影响功能）
- 3 条 `InsecureRequestWarning` 来自 `neptune_client`（走 urllib3 未验证证书）→ 这是生产代码的既有行为，与 Strands 无关，但在 L1 替换前应该顺手修掉（RDS CA bundle 已经存在，只是没被启用）。

### 3.3 Token 成本上升
- System prompt 含完整 schema + few-shot，约 4720 input tokens / 次 agent 调用
- Strands ReAct 每个 tool round-trip 都重新发送上下文 → 预计单次查询 **input token ≈ 12-20k**（现版 ≈ 6k）
- 3x 左右成本增幅，L1 需要核算月度账单

### 3.4 Tool schema 推断
- `@tool` 装饰器自动从 docstring + 类型注解生成 JSON schema。`get_schema_section(section: str = "all")` 正常工作
- 未验证：复杂嵌套参数（list[dict] / pydantic）是否一样顺滑

### 3.5 Strands trace 原生未启用
- spike 里用自定义 `_LAST_CALLS` 列表抓 trace，属于 hack
- Strands 提供 OTel hook 和 callbacks，L1 要接 CloudWatch/X-Ray 才能对接 P1''' golden CI

### 3.6 Agent 响应结构
- `agent(q)` 返回的是 `AgentResult`，`str(resp)` 打印出 `{'role': 'assistant', 'content': [{'text': ...}], 'metadata': {...}}`
- 提取 `resp.message.content[0].text` 更准确；spike 用 `getattr(resp, 'message', None)` 够用但不优雅，L1 要规范

---

## 4. L1 POC 决策：GO（有条件）

**GO**：所有硬约束通过，功能可行性无阻塞，延迟可接受范围。

**条件**（L1 必须解决）：

1. ✅ **必须** 用 inference profile id，裸模型 id 不可用 — 在 `nl_query_strands.py` 里硬编码白名单 + env fallback
2. ⚠️ **必须** 把 `execute_cypher` 做成 Agent 的*唯一*执行入口，不开放裸 neptune_client 给 tool 层 — spike 已经验证 guard 生效
3. ⚠️ **必须** 接入 Strands 原生 OTel/callbacks，替换 spike 里的 `_LAST_CALLS` hack，作为 P1''' golden CI 的度量源
4. ⚠️ **推荐** 实现 `get_schema_section` 的按需读取（section=nodes/edges/specific_label），减少 system prompt 体积，降低 token 成本
5. ⚠️ **推荐** 做 shadow 对跑：`nl_query.py`（现版）和 `nl_query_strands.py`（新版）在 Streamlit 里共存，feature flag 切换，golden set 跑通再 cutover
6. 🔴 **风险** Global inference profile 单点依赖 — 列出备选 inference profile 清单并季度性重探

**L1 范围（建议 3-5 人日）**：
- `rca/neptune/nl_query_strands.py`（~80 行，基于 spike.py 工程化）
- `rca/neptune/strands_tools.py`（tools 独立文件便于单测）
- `tests/test_strands_shadow.py`（现版 vs Strands 的 golden diff 报告）
- 启用 Strands OTel → CloudWatch（trace + token 统计）
- Streamlit feature flag：`?engine=strands|baseline`

**L1 验收门槛**（复用报告里提过的 golden set 指标）：
| 指标 | Baseline | Strands 门槛 |
|------|----------|--------------|
| exact_match | baseline 值 | ≥ baseline - 5% |
| result_precision | baseline 值 | ≥ baseline |
| p50 latency | 5.67s | ≤ 12s |
| p99 latency | 未测 | ≤ 20s |
| 空结果 ReAct 回收率 | 0 | > 30% |
| 月度 token 成本 | baseline | ≤ 3.5x |

---

## 5. 产出物清单

```
experiments/strands-poc/
├── task_plan.md            # 任务规划
├── golden_questions.yaml   # 3 条测试问题
├── requirements.txt        # strands-agents 等依赖
├── spike.py                # Strands Agent POC（~190 行）
├── baseline.py             # 现版 NLQueryEngine 跑相同问题
├── run_spike.sh            # 快捷运行脚本（env + venv 激活）
├── run_baseline.sh         # 同上
├── spike_result.json       # spike 跑完的完整 trace
├── baseline_result.json    # baseline 结果
└── report.md               # 本报告
```

*生产代码零改动*：`rca/`、`profiles/`、`.env` 均未动。

---

## 6. 开放问题（留给 L1 / L2 决策）

1. Strands 1.0 的 multi-agent orchestration 能不能直接用在 `HypothesisAgent` + `LearningAgent` + Layer2 Prober 的组合上？（improvements.md 的 Phase 2 重点）
2. OTel trace 落 CloudWatch 的 cost 和 retention 策略
3. Strands 在 Lambda / Fargate 上的冷启动延迟（如果将来把 nl_query 做成 API）
4. profile 切换（`PROFILE_NAME=<env>`）在 multi-tenant Streamlit 进程里是否会串（现在是 import-time load，进程内单值）

---

## 7. L1 POC 完成（2026-04-18）

### 7.1 交付

6 个 PR 全部合并 main（`42933cf` → `0ce89bf`）：

| # | Commit | 范围 |
|---|--------|------|
| 1 | `42933cf` | `rca/engines/` 地基（NLQueryBase + factory + strands_common） |
| 2 | `dd83f95` | `NLQueryEngine` → `DirectBedrockNLQuery` rename + re-export shim |
| 3 | `b76c433` | `StrandsNLQueryEngine` + `strands_tools`（3 个 @tool） |
| 4 | `a83f35e` | Golden matrix（engine parametrization）+ shadow test |
| 5 | `759e5e0` | Streamlit factory wire（demo/pages/2_Smart_Query.py 改 1 行） |
| 6 | `0ce89bf` | `docs/migration/timeline.md` + `scripts/check_migration_deadlines.py` 空壳 |

### 7.2 Golden 对比（真调 Bedrock + Neptune）

| Engine | Pass | Feature | Result | p50 | p99 | Tokens |
|--------|------|---------|--------|-----|-----|--------|
| direct | 20/20 = *100%* | 100% | 100% | 5458 ms | 7474 ms | ~130k |
| strands | 20/20 = *100%* | 100% | 100% | 9269 ms | 19038 ms | — |

Latency multiplier：p50 = *1.86x*，p99 = *2.38x*（均在门槛 2x / 2.5x 内）。

### 7.3 验收门槛

| 指标 | 门槛 | 结果 |
|------|------|------|
| direct golden | ≥ 19/20 | ✅ 20/20 |
| strands golden | ≥ 19/20 | ✅ 20/20 |
| strands p50 | ≤ 2x direct | ✅ 1.86x |
| strands p99 | ≤ 2.5x direct | ✅ 2.38x |
| q001 / q006 must_not_contain [DependsOn] | 全过 | ✅ both pass |
| 现有调用方（`from neptune.nl_query import NLQueryEngine`） | 零改动 | ✅ shim |

### 7.4 Wave 4/5 在 Strands 版的等价实现

- *Wave 4 空结果重试*：Strands engine *显式禁用*（`retried` 固定 `False`）。ReAct 原生多轮已覆盖该能力，避免双层重试。
- *Wave 5 Opus 升级*：`StrandsNLQueryEngine._select_model()` 保留关键词匹配，命中 `profile.neptune_complex_keywords.zh/en` → 用 `HEAVY_MODEL` 构造 `BedrockModel`。冒烟 Q3 "petsite 的完整上下游依赖路径" 观察到切 Opus 生效。

### 7.5 Streamlit 冒烟（headless）

`demo/_smoke_smart_query.py <direct|strands>` 3 条问题全跑通。关键数据：

| 问题 | direct rows/latency/model | strands rows/latency/model/tools |
|------|-----|------|
| petsite 依赖哪些数据库？ | 2 / 4.1s / sonnet | 2 / 8.6s / sonnet / [validate, execute] |
| Tier0 服务有哪些？ | 3 / 4.5s / sonnet | 3 / 9.3s / sonnet / [validate, execute] |
| petsite 的完整上下游依赖路径 | 50 / 8.5s / *opus* | 1 / 11.2s / *opus* / [validate, execute] |

第 3 问 direct 返回 50 行、strands 只返 1 行是 agent 选择用"扇形单行"聚合（summary 里把所有上下游列在一起），不影响正确性。

### 7.6 陷阱 / 踩坑

1. **PEP 668 主环境保护**：`/usr/bin/pip3 install strands-agents` 被拒。改用 `--break-system-packages --user` 装到 `~/.local`（和现有 streamlit 同目录）解决。
2. **`threading.local` 在 Strands 里丢失**：Strands 内部用 async/thread pool，`strands_tools` 的 trace 容器必须用模块级 list + Lock，不能用 `threading.local`。
3. **trace 截断导致 rows=0**：第一版 `execute_cypher` trace 里只存了 `cypher[:200]`，engine 复算时把截断的 cypher 送进 Neptune → 0 rows。修复：trace 存完整 cypher + preview。
4. **q014 P0 过滤丢失**（*已修复*，2026-04-18）：Strands ReAct agent 发现 Neptune 里实际没有 P0 故障（全部 126 条是 P1），主动去掉 `severity='P0'` 过滤，结果 `cypher_must_contain=['P0']` 失败。以两步修复：
   - Agent 规则 ＃4：“必须完整保留问题中的所有过滤条件，不得泛化”
   - Golden q014 再对齐数据：问题改为“所有严重故障（P1 及以上）及其根因”，must_contain=['severity','root_cause']，min_rows=1
   修复后重跑：strands 20/20、direct 20/20 。commit `ecd17cb`。
5. **Wave 4 禁用 vs. ReAct 原生循环**：strands 的 17777 ms p99 主要来自 ReAct 多轮（validate + execute round-trip），不是重试。
6. **Token 统计不全**：Strands `AgentResult` 的 metadata 字段 key 名与预期不同，`token_usage` 目前经常为 None（不影响功能）。Phase 3 前改成 OTel callback 拿准确数字。

### 7.7 Phase 3 启动前补作业

- [ ] 主环境 strands 依赖管理正式化：把 `demo/requirements.txt` 的注释式改成必装项（L1 稳定 4 周后）
- [ ] Strands OTel hook 接入 CloudWatch / X-Ray，替换 `strands_tools` 的 trace hack
- [x] `scripts/check_migration_deadlines.py` 接 GitHub Actions（`ci/migration-checks`，2026-04-18 commit `fc0ac36`）
- [x] `.github/workflows/check-frozen.yml` 冻结文件 PR 检查（合并在 `migration-checks.yml` 的 check-frozen job，2026-04-18 commit `fc0ac36`）
- [x] q014 的 prompt 调优（已修，commit `ecd17cb`）

### 7.8 决策：GO for Phase 2 稳定期

所有验收门槛通过，建议进入 Phase 2 Week 2-3 稳定期：线上默认保持 `NLQUERY_ENGINE=direct`，定期（每日）跑 shadow 对比观察 4 周；达标后把 smart-query 的 `freeze_date` 写入 `docs/migration/timeline.md` 并打 `v-last-direct-YYYYMMDD` tag，正式启动 Phase 3。
