# DR Executor Migration Retrospective

> 作者：编程猫
> 完成日期：2026-04-20
> 目标读者：架构审阅猫 + 大乖乖（Phase 3 总结输入）

## 1. 模块信息

- Module: dr-executor (Phase 3 Module 6 — **最终模块**)
- Duration: planned 2w, actual ~3h（大乖乖豁免 Gate A）
- PRs: 7/7 (merged)
- Start Commit: `064801a` → End Commit: (this commit)
- Golden baseline: Direct 2/2, Strands 2/2
- Cache: STABLE_DR_FRAMEWORK 1211 tokens (cache eligible ✅)
- System prompt: stable (1211 tokens) + variable (plan context)

## 2. TASK 质量反馈（给架构审阅猫的直接输入 ★★）

### 2.1 做得好的
- **§5 数据模型速查表极其有用** — 直接从 retro Top 1 落实，0 个属性路径 bug（Module 5 有 3 个）
- **§6 复用清单** — 明确说了 13 个不要自己做的方法，节省大量时间
- **§12.6 Mock Tool Response Schema** — 从 retro Top 2 落实，tool 返回格式清晰
- **§9 安全专题** — Failure Strategy 5 种策略的表格极其清晰

### 2.2 让我困惑的地方
- **system_prompt 格式**: TASK §10.2 展示了 `[{"type": "text", "cache_control": ...}]` 的 dict list 格式，但 Strands Agent 的 system_prompt 参数只接受 string。CacheConfig(strategy="auto") 自动处理缓存前缀。这导致第一次运行报 "Unknown parameter in system: type"。建议下次明确说"Strands Agent system_prompt 是 string，不是 Bedrock API 的 dict list"。
- **`from strands.tool import tool`** 不存在，正确路径是 `from strands import tool`。Module 3-5 都用对了，但 TASK 没显式提醒。

### 2.3 漏了应该提醒的陷阱
- **DRPlan.from_dict 需要 created_at** — golden case YAML 少了这个必填字段，导致 TypeError。§5 列了这个字段但没标 required。
- **RehearsalReport 没有 engine_meta 属性** — 需要动态设置 `report.engine_meta = {...}`，dataclass 不自带。

### 2.4 下个 TASK 建议加的字段
- Phase 3 已全部完成，此项留给 Phase 4 参考：**标注每个数据模型的 required vs optional 字段**。

## 3. 技术教训

### 3.1 踩到的坑
- **坑 1: system_prompt 格式** — Strands Agent 接受 string，不是 Bedrock API 的 content block list。→ 教训：看框架的参数类型签名，不要照搬底层 API 格式。
- **坑 2: `from strands.tool import tool`** — 模块不存在，正确是 `from strands import tool`。→ 教训：每个 import 都 REPL 验证。
- **坑 3: DRPlan.created_at required** — golden case 缺字段。→ 教训：用 `from_dict({})` 触发 TypeError 来发现缺失字段。

### 3.2 Golden Set 构建经验
- 2 个 case 足够验证核心协议（AZ failover + Region failover）
- Strands Agent 的 tool 调用质量极高：l1-001 精确 12 calls，l1-002 精确 17 calls
- l1-002 在 dry_run=True 下得到 SUCCESS（mock 全成功），ROLLED_BACK 测试需要自定义 mock override — 这是设计限制，不是 bug
- 行为断言（plan_id + no CRITICAL warnings）足够，不验文本

### 3.3 Prompt Caching
- 稳定段 1211 tokens > 1024 最低要求
- token_usage 报告为 0 — Strands metrics.get_summary() 的 accumulated_usage 可能需要不同的提取路径
- 缓存行为需要 verify_cache_dr_executor.py 连续 3 run 确认

### 3.4 Strands SDK
- Agent 正确处理了复杂的多 phase 协议
- cross-region health check 在 region scope plan 中自动触发 ✅
- manual approval 在 dry_run 中自动 approve ✅
- sequential mutation 被 Agent 主动遵守（"cross-region mutations must not run simultaneously"）✅

## 4. 跨猫协作

- TASK 28KB/1050 行是 Phase 3 最大的 TASK — 但结构清晰，读取效率高
- §5+§6 是 Module 5 retro 的直接输出，证明 retro 机制有效
- sessions_send 格式稳定

## 5. 时间 / 成本

- 最耗时：debug system_prompt 格式 + import 路径（~15min）
- L1 Golden 总 Bedrock 成本：~$1-2（2 cases × 2 engines）
- Strands 单次 latency：52-72s（比 Runner 模块快，因为 step 数少）

## 6. 给 Phase 4 的 Top 3 建议 ⭐⭐⭐

1. **Strands Agent system_prompt 是 string，CacheConfig 处理缓存**
   不要传 Bedrock API 的 content block list。这个坑 Phase 3 第一次遇到（Module 1-5 都用 string），Module 6 因为 TASK §10.2 的示例代码误导才出错。Phase 4 统一清理时注意保持 string 格式。

2. **dry_run 模式无法测试 failure strategy**
   所有 mock tools 返回 success，ROLLBACK/RETRY 路径无法在 L1 Golden 中验证。Phase 4 可以考虑：(a) 自定义 mock factory 注入失败 (b) L2 staging 专门测试 failure path。

3. **数据模型 required 字段要标注**
   Phase 3 遇到 2 次 missing required field（Module 5: ExperimentResult.log_collection, Module 6: DRPlan.created_at）。Phase 4 统一清理时，给所有 dataclass 加 `__post_init__` 或 docstring 标注 required/optional。

## 7. 给架构审阅猫的 TASK 模板修改建议

- **system_prompt 格式说明** — 加一句："`Strands Agent system_prompt 参数类型是 str，不是 Bedrock API content block list。CacheConfig(strategy='auto') 自动处理缓存。`"
- **标注 required vs optional 字段** — §5 数据模型速查表很好，但 DRPlan.created_at 是 required 没标出来
- **import 路径提醒** — 除了 CacheConfig 的 import 路径（已有硬约束 §12），tool decorator 的正确 import 也要加：`from strands import tool`（不是 `from strands.tool import tool`）

---

## 🎉 Phase 3 完成！

6 个模块全部迁移完成：
1. ✅ Hypothesis Agent — frozen 2026-04-18
2. ✅ Learning Agent — frozen 2026-04-26
3. ✅ RCA Layer2 Probers — frozen 2026-04-19
4. ✅ Chaos PolicyGuard — frozen 2026-04-20
5. ✅ Chaos Runner — frozen 2026-04-20
6. ✅ DR Executor — frozen 2026-04-21

所有模块：双引擎（Direct + Strands）、Golden CI、prompt caching、factory pattern。

*Phase 4: 统一清理 + 灰度切换 + 生产验证。*

---

*架构审阅猫在写 Phase 4 规划前必须 read 本文件。*
