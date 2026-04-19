# LearningAgent Migration Retrospective

> 作者：编程猫
> 完成日期：2026-04-19
> 目标读者：架构审阅猫（写 RCA Layer2 Probers / PolicyGuard 模块 TASK 时读）

## 1. 模块信息

- **Module**: learning-agent (Phase 3 Module 2)
- **Duration**: planned 3w, *actual 1 天*（Gate A 人工豁免"稳定 ≥ 1 周"）
- **Commits**: 5 个（`abbc801` → `2a9931c`）
- **Golden baseline**:
  - direct *10/10 = 100%* ✅
  - strands *10/10 = 100%* ✅
- **Cache hit ratio**: 稳态 *85%*（Run 2/3 cache_read ~11k tokens）
- **Cost savings vs no-cache（估算）**: ~65%（system prompt + tool schema 复用）
- **freeze_date**: 2026-04-26 | **delete_date**: 2026-08-26

## 2. TASK 质量反馈（给架构审阅猫的直接输入 ★★）

### 2.1 做得好的（vs Module 1 TASK 改进）

- §2 前置条件分"可豁免 / 不可豁免"两列 — *非常好用*，Gate A 确认时直接对照表，30 秒搞定
- §3 env 变量清单标注 `NEPTUNE_HOST` vs `NEPTUNE_ENDPOINT` 差异 — *救命*，Module 1 retro 提的这个建议落实了，省了至少 10 分钟调试
- §5.6.3 sample 脚本模板含抗 SIGPIPE + 场景级 try/except — 直接复制用，采样中途 2 次被 SIGKILL（不是 SIGPIPE）也能 resume
- §9 Gate B 决策矩阵 — 没用上（全过了），但知道不达标时该走路径 A 还是 B 让我心里有底

### 2.2 让我困惑或不好用的地方

- *§5.1 LearningBase 5 个抽象方法的签名*：TASK 写了两版（§5.1 简版 + §6 详版），详版多了 `iterate_hypotheses(coverage, existing_hypotheses)` 的双参数签名，简版只写了 `iterate_hypotheses(coverage)`。我按详版实现的，但简版让我犹豫了 2 分钟
- *§5.4 "预计 ReAct 1-2 cycles"*：实测 Strands 在 `generate_recommendations` 里会调 4 个 tools（experiment_history + coverage_snapshot + graph_nodes + invoke_hypothesis_engine），实际 ReAct 2-3 cycles（不是 1-2）。预估偏低导致我一开始以为 Strands 哪里配错了
- *§5.5 assert_cacheable 的代码示例用 `tiktoken.encoding_for_model("cl100k_base")`*：这个 API 在新版 tiktoken 已改为 `tiktoken.get_encoding("cl100k_base")`。代码示例应该跟最新 API 走
- *§10 PR 切分*（PR5/PR6/PR7）：对于 LearningAgent 这种迁移范围小的模块（只有 1 个 LLM 调用点），3 个 PR 太碎。我实际用 2 个 commit 就完成了 Week 1 + Week 2 的全部内容。*建议*：下个 TASK 按实际复杂度建议 PR 数，不强制 7 个

### 2.3 漏了应该提醒的陷阱

- *`invoke_hypothesis_engine` tool 会递归 spawn Strands agent*：当 `HYPOTHESIS_ENGINE=strands` 时，learning_strands 的 tool 里调 `make_hypothesis_engine()` 会创建嵌套的 Strands agent，内存膨胀到 SIGKILL。*TASK 没有提到这个嵌套风险*。我的修复：tool 内强制 `HYPOTHESIS_ENGINE=direct`
- *factory.py 的 import 路径*：TASK 里的 factory import 用 `chaos.code.agents.*`，但 PYTHONPATH 设的是 `chaos/code`，实际应该用 `agents.*`。Module 1 没遇到是因为 hypothesis 的 factory import 碰巧对了
- *采样脚本被 SIGKILL 不是 SIGPIPE*：Module 1 retro 强调防 SIGPIPE，但我遇到的是系统级 SIGKILL（疑似 cgroup/OOM killer 误杀）。SIGPIPE 防御写了但没触发，真正救命的是 *incremental save*（每个场景完成后写 all_samples.json）

### 2.4 下个 TASK 建议加的字段

- *"Strands tool 调用链中是否存在嵌套 Agent 风险"*（如果 tool 内部调 make_xxx_engine()，写清楚是否强制 direct）
- *"factory.py import 路径确认"*（`chaos.code.agents.*` vs `agents.*`，取决于 PYTHONPATH 设置）
- *"采样脚本的 incremental save 策略"*（不只是 SIGPIPE 防御，还有 SIGKILL resume）

## 3. 技术教训

### 3.1 踩到的坑

**坑 1: 嵌套 Strands Agent 导致 SIGKILL**
- 症状：`main.py learn --all` 跑到 `invoke_hypothesis_engine` tool 时被 SIGKILL
- 根因：`HYPOTHESIS_ENGINE=strands` 时，tool 内 `make_hypothesis_engine()` 创建第二个 Strands Agent（BedrockModel + tools），两层 Strands 进程叠加内存爆
- 修复：`learning_tools.py` 内强制 `os.environ["HYPOTHESIS_ENGINE"] = "direct"`
- *给下个模块*：任何 Strands tool 内部如果需要调另一个 engine，*必须强制 direct*，避免递归 spawn

**坑 2: factory.py import 路径不匹配**
- 症状：`verify_cache_learning.py` 报 `ModuleNotFoundError: No module named 'chaos'`
- 根因：factory.py 用 `from chaos.code.agents.learning_strands import ...`，但 PYTHONPATH 是 `rca:chaos/code`，不包含 `chaos` 的父目录
- 修复：改为 `from agents.learning_strands import ...`
- *给下个模块*：factory.py 所有新增 import 必须用 `agents.*` 或 `engines.*`，不用 `chaos.code.agents.*`

**坑 3: system prompt 482 tokens < 1024 缓存下限**
- 症状：`assert_cacheable` 失败，StrandsLearningAgent 构造不了
- 根因：初版 system prompt 太短（482 tokens），接近但未达 1024 下限
- 修复：加 fault type reference table（15 行 × 4 列）把 prompt 扩到 1270 tokens
- *给下个模块*：system prompt 初稿写完后第一件事就是跑 `assert_cacheable`，不要等集成测试才发现

**坑 4: tiktoken API 变更**
- 症状：`tiktoken.encoding_for_model("cl100k_base")` 抛 KeyError
- 根因：新版 tiktoken 改为 `tiktoken.get_encoding("cl100k_base")`
- 修复：换 API
- *给下个模块*：TASK 代码示例里的 tiktoken 调用要用 `get_encoding`

**坑 5: 采样脚本 2 次被 SIGKILL（不是 SIGPIPE）**
- 症状：sample_for_golden_learning.py 跑到 l005 时进程消失，exit signal SIGKILL
- 根因：不确定（30GB free memory，不像 OOM），可能是 cgroup limit 或系统级策略
- 修复：加 incremental save（每个场景完成后写 all_samples.json + progress.json），支持 resume
- *给下个模块*：采样脚本 *必须* incremental save + resume，不只是 SIGPIPE 防御

### 3.2 Golden Set 构建经验

- *实际耗时*：
  - fixture 导出（DynamoDB）：30 秒（10 个 fixture，86 条实验）
  - scenarios.yaml 编写：10 min
  - direct 采样：~12 min（8 个有 LLM 的场景 × 3-5 次，每次 ~20s）
  - 人工 review cases.yaml：5 min（自动生成 observed_frequent_categories 已经很准）
- *比 Module 1 快得多*：因为 LearningAgent 只有 1 个 LLM 调用点，场景简单
- *行为约束式 golden 完美适配*：推荐的 category 分布（coverage/resilience/process）3 个类别在所有场景都高频出现，稳定性极好
- *4 类分桶设计好用*：核心服务 / 特殊后端 / 错误输入 / 边界 — 覆盖全面，错误输入类（l007 空列表 / l008 畸形 JSON）抓到了真实的 graceful degradation 行为

### 3.3 Prompt Caching

- *稳态命中率*：85%（超预期，TASK 预估 50%）
- *原因*：Strands agent 的 system prompt + tool schema 在 3 次调用间完美复用；tool 输出变化不影响缓存
- *CacheConfig(strategy="auto") vs deprecated cache_prompt*：
  - LearningAgent 用 `CacheConfig` — 零 warning ✅
  - HypothesisAgent 冻结版还用 `cache_prompt="default"` — 每次跑都吐 warning
  - *建议*：`strands_common.build_bedrock_model()` 应升级为 `CacheConfig`，但因为 HypothesisAgent 已冻结引用它，需要向后兼容（加 kwarg 默认值）
- *冷启动行为*：Run 1 就有 cache_read=7933（因为之前 Golden CI 跑过，缓存还没过 5min TTL）。在*完全冷启动*时，Run 1 应该 cache_write > 0, cache_read = 0

### 3.4 Strands 架构决策

- *只有 `generate_recommendations()` 走 ReAct，其余 4 个方法 delegate 给 Direct*：这是正确决策。LearningAgent 465 行代码里 400 行是纯 Python 聚合逻辑，没必要让 Strands agent 做 for 循环。ReAct 开销只花在有价值的地方（LLM 建议生成 + tool 补充上下文）
- *Strands 建议质量比 Direct 好*：Strands 会主动查 experiment_history 和 topology 补充上下文，建议更具体（引用实际 pass rate / recovery time 数字）。Direct 只有 prompt 里的 summary

## 4. 跨猫协作

- *TASK 收到后立即可执行*：12 项 retro 建议全部落实，没有需要来回确认的歧义
- *只找大乖乖确认了 4 个点*：Gate A 豁免 / 预算 / 冻结窗口 / hypothesis_tools.py 改动范围 — 全部 1 条消息搞定
- *hypothesis_tools.py 改动审批流程好*：先给大乖乖看 diff，确认后再改，清晰明确

## 5. 时间 / 成本

- *最长阶段*：Golden CI Strands（370s = 6 min），Direct（198s = 3 min）
- *采样成本*：~30 次 LLM 调用 ≈ $0.3（LearningAgent prompt 短，比 Hypothesis 便宜得多）
- *缓存验证*：3 次调用 ≈ $0.1
- *灰度冒烟*：2 次（1 次 SIGKILL + 1 次成功）≈ $0.1
- *总 Bedrock 成本*：≈ **$1.5**（Module 1 的一半）

## 6. 给下个模块的 Top 3 建议 ⭐⭐⭐

1. **Strands tool 内调其他 engine 必须强制 direct，防止嵌套 Agent 内存爆炸**
   RCA Layer2 Probers 如果有 tool 需要调 hypothesis/learning engine，*必须*在 tool 函数体内临时设 `os.environ["XXX_ENGINE"] = "direct"`，调完恢复。不这样做会在灰度阶段被 SIGKILL，而且 debug 困难（看不到 OOM 日志，因为是 cgroup 级别的 kill）。

2. **采样脚本必须 incremental save + resume，不只防 SIGPIPE**
   Module 1 retro 强调防 SIGPIPE（`signal.signal(signal.SIGPIPE, signal.SIG_DFL)`），我全照做了，但实际杀手是 SIGKILL（不可捕获）。真正有效的防御是：每个场景完成后立即 `json.dump(all_samples)` 写磁盘 + `progress.json` 记录进度，脚本启动时检查已有 samples 跳过已完成场景。RCA Probers 场景更复杂（6 个已知故障），采样更慢，没有 resume 就是灾难。

3. **factory.py import 用 `agents.*` 不用 `chaos.code.agents.*`**
   Module 2 和 Module 1 都踩了 PYTHONPATH 导致 import 失败的坑。根因是 `chaos/code` 在 PYTHONPATH 里，但 factory.py 用 `chaos.code.agents.*` 全限定路径。下个模块加 factory 时直接用 `from agents.xxx import Xxx` 或 `from probers.xxx import Xxx`，和 PYTHONPATH 保持一致。

## 7. 给架构审阅猫的 TASK 模板修改建议

- *§5 产出清单的接口签名*：如果有简版和详版，*只保留一版*（详版），删简版避免冲突
- *§5.4 "预计 ReAct cycle 数"*：改为给范围而不是点估计（如 "2-4 cycles"），因为 Strands 会根据上下文动态决定调几个 tool
- *§5.5 代码示例*：tiktoken 改用 `get_encoding("cl100k_base")`
- *§10 PR 切分*：加一句 "对于 LLM 调用点 ≤ 2 的模块，可合并为 2-3 个 PR"
- *新增 §X "嵌套 Agent 风险评估"*：列出本模块的 Strands tool 中哪些会调其他 engine，标注是否需要强制 direct
- *新增 §Y "factory.py import 路径确认"*：明确写 `from agents.xxx import Xxx`（匹配 PYTHONPATH=chaos/code）

---

*架构审阅猫在写 RCA Layer2 Probers TASK 前必须 read 本文件，重点关注 § 6 Top 3 建议。*
