# HypothesisAgent Migration Retrospective

> 作者：编程猫
> 完成日期：2026-04-18
> 目标读者：架构审阅猫（写 LearningAgent / Prober / Chaos / DR 模块 TASK 时读）

## 1. 模块信息

- **Module**: hypothesis-agent
- **Duration**: planned 3w, *actual 1 天*（Gate A 人工豁免，L2 合并当天启动）
- **PRs**: 7/7 计划；*已合并 6 + chore 2 + prioritize cache 追加 1 = 实际 main 上 9 个提交*
- **Start / End Commit**: `c9f0059` (PR1) → `de5f11c` (prioritize cache)（PR7 待合）
- **Golden baseline**:
  - direct *18/20 = 90%* ✅（≥18/20 门槛）
  - strands *20/20 = 100%* ✅（P0-bugfix `5376421` 后，2026-04-18 同日完成）
- **Cache hit ratio**:
  - direct 66.3%（prioritize 加缓存前）→ 预估合并后 ~85%
  - strands 76.2%（上一版 69.0% → 修后）
- **Cost savings vs no-cache（估算）**:
  - direct generate+prioritize 一次性调用，prioritize 缓存前单次节省 ~40%；合并后 ~60%
  - strands 多 cycle + 缓存 internal reuse，稳态 ~65%

## 2. TASK 质量反馈（给架构审阅猫的直接输入 ★★）

### 2.1 让我困惑过的描述

- *`direct_file: rca/agents/hypothesis_direct.py`*（timeline.md）vs *`chaos/code/agents/hypothesis_direct.py`*（TASK §4.2）— 两边不一致，我走了错路径 5 分钟才问你。架构审阅猫后来承认 timeline.md 里 L1 PR6 时占位错了，顺手修了 3 个。*教训*：timeline 占位路径不能凭猜，写 L1 PR6 时必须架构审阅猫 grep 过再落地，而不是"等 Phase 3 再说"。
- *§4.5 Golden Set 两版说法冲突*：初版写 `expected_hypotheses: [{fault_type, target_category}]` 硬编码；我提了问题后 §4.5 被改写成"scenarios + sample + human review"行为约束式。这是*正确方向*但文字冲突增加了沟通成本。
- *§2 Gate A 5 项预置条件* 中"L2 稳定 4 周" 在大乖乖口头豁免后显得形式化。*建议*：下个模块 TASK 在 §2 里明写"以下条件可大乖乖 slack 确认豁免"，哪些不可豁免（如 freeze_date 已填是可豁免的，Bedrock 配额是不可豁免的）。

### 2.2 漏了应该提醒的陷阱

- *chaos/code/ 用的 env 是 `NEPTUNE_HOST` 而不是 `NEPTUNE_ENDPOINT`*（Smart Query 用后者）。我第一次跑 verify_cache 失败 10 分钟才发现。*下个模块必写*：每个模块 TASK 要列"此模块依赖的关键 env 变量清单（与其他模块差异标红）"。
- *Prompt Caching 最低 **1024 tokens**，不是 "3000 chars"*（Smart Query L2 用 3000 chars 是英文场景，中文等价要 4000+ chars）。TASK 引用 L2 的经验时没区分中英文场景。
- *chaos/code/main.py 的 sys.path 默认不含 `rca/`*，engines factory 要生效必须手动加。Smart Query 场景下 demo/app.py 已经加过，所以 Phase 2 没踩。*教训*：Phase 3 每个模块的调用方都是不同子目录，sys.path 结构要在 PR6（wire factory）时重点检查。
- *`cache_prompt` 在 Strands 1.36 已 deprecated*，用起来还是可以，但跑测试时会吐 53 条 `UserWarning: cache_prompt is deprecated. Use SystemContentBlock with cachePoint instead.`。*建议*：`strands_common.build_bedrock_model` 应该先做次升级，改用 `cache_config=CacheConfig(strategy="auto")`。

### 2.3 多余或过严的约束

- *§6 硬约束 #5 "Neptune Gremlin 保持不变，复用 runner/neptune_client.py"*：这其实自动就会发生（因为 @tool 就是包装它）。对下个模块（LearningAgent 也是查 Neptune）*这条可删*。
- *§8 Gate B 门槛 "strands golden ≥18/20"* 跟 "允许 10% 回归" 在数字上不严谨。20 × 10% = 2 个允许，就是 18/20。写法模糊。*建议*：写成 "strands golden ≥ direct - 2 (allow 10% regression)"。
- *§7.4 `verify_cache_<module>.py`*：每个模块都要写一份自检脚本，但其实 3/4 代码都一样（只换 engine class 名），*建议*：把公共 harness 抽到 `experiments/strands-poc/verify_cache_common.py`，每模块只写 factory+sample question 两行。

### 2.4 下个 TASK 建议加的字段

- *"本模块的 HYPOTHESIS_ENGINE 等价 env 变量是什么"*（LearningAgent 就是 `LEARNING_ENGINE`）
- *"此模块在 chaos/code/main.py 或 orchestrator.py 中的调用点列表"*（不只是"我知道 HypothesisAgent() 被调"，而是具体文件行号）
- *"Golden scenarios 按业务风险分类的建议"*（S001 核心服务、S007 特殊后端如 Lambda、S016 错误输入、S018 边界 — 每个模块都该有这 4 类分桶）
- *"Strands agent 在本模块预计的 ReAct cycle 数"*（Hypothesis 实测 3，RCA Layer2 会多得多）

## 3. 技术教训

### 3.1 踩到的坑（编号与对后续模块的建议配对）

**坑 1: sample_for_golden.py 被 `| tail -30` 管道 SIGPIPE 杀掉**
- 症状：脚本跑 30min+ 没输出就消失，`cases.draft.yaml` 没生成
- 根因：exec 用 `... | tail -30` 时如果子命令输出 > 30 行后 tail 关管道，脚本下次 print stderr 就 SIGPIPE
- 建议：下个模块的 sample 脚本必须把进度 log 写文件 tee（不依赖管道），并且*主循环外层套 try*，让单场景失败不挂整个脚本。

**坑 2: Prompt Caching 静默失效**
- 症状：Direct cache 加完之后 token 统计里 `cache_read=0 cache_write=0`
- 根因：Anthropic Bedrock 对 < 1024 tokens 的 cacheable block 静默忽略，不报错
- 建议：每个加缓存的 system prompt 必须在 `__init__` 加 `assert len(sys_prompt) > 3000`（中文）或 `> 1024 tokens`（用 tiktoken 精确测）；缓存激活后第一次跑 `verify_cache_*.py` 必须验证 cache_write > 0。

**坑 3: Strands ReAct 出现了 S016/S017 的意外行为**
- S016：传 `service_filter="nonexistent-service-xyz"`，direct 抛 ValueError；strands agent 自己绕过 check，造了 5 个假设
- S017：max=1 的单 hypothesis 采样结果和 direct 不一致（direct http_chaos, strands dns_chaos）
- 根因：Strands 的"积极补救"本能违反 should_error 断言；小样本单次采样本身就有偏差
- 建议：should_error 类 golden 必须在 system prompt 里加一条"如果 tool 报错就原样返回错误，不要自己补救"的硬规则；单样本 case 不做 fault_type 强断言，只做 `result_min_rows >= 1`。

**坑 4: Strands MaxTokensReached（S018）**
- 症状：`max_hypotheses=30` 时生成 15+ 个就超 output_tokens 挂了
- 根因：Strands BedrockModel 默认 `max_tokens=4096`，大批量生成不够
- 建议：`build_bedrock_model` 应该暴露 `max_tokens` 参数，对广度扫场景调到 8192/16384。

**坑 5: timeline.md 的 direct_file 路径占位**
- 在 L1 PR6 我随手写 `rca/agents/hypothesis_direct.py` 作为占位，实际代码在 `chaos/code/agents/`。到 Phase 3 Module 1 才发现，已经被架构审阅猫修正。
- 建议：所有 placeholder 文件路径不能写 `~`（未知），要写 "TBD-<具体目录>-<具体文件>"，或者直接 grep 现有代码位置填具体路径。

**坑 6: experiments/ 被 .gitignore 排除后再恢复的成本**
- 大乖乖期间改 .gitignore 排除 experiments/，我恢复花了一次 commit
- 建议：experiments/ 留在 repo 里，retros/ 这类敏感的可单独 .gitignore。如果担心泄漏，就把 internal role 词从 TASK 里脱敏，而不是整目录排除。

### 3.2 Golden Set 构建经验

- *实际耗时*：
  - scenarios.yaml 编写：15min（直接基于 Neptune 里 14 个 service 的已知 tier 挑）
  - direct 采样脚本跑：*52 min*（20 scenarios × 2 runs，含 3 个 Bedrock read-timeout 各 13min），远超 "1 人日" 预期
  - 人工 review cases.yaml：10min（主要调 sparse 场景的 min_hypotheses=0，加 S007 的 must_not_include pod_kill）
- *direct 采样 2 次基本够*（2 次 intersection 就能看出 fault_type 稳定分布），但对边界场景（S017 max=1）2 次太少。*建议*：对 `max_hypotheses <= 2` 或 `service_filter` 指向稀疏拓扑节点的场景，采 5 次。
- *must_not_include 人工 review 的判断依据*：主要是*领域知识*（Lambda 没 Pod、RDS 不能 pod_kill、压测服务不能 DNS chaos）。建议在 TASK 里列出"每类 backend 的禁止 fault_type 清单"作为 review checklist。
- *行为约束式 golden 实际效果*：✅ 有效。它让 strands 16/20 和 direct 18/20 可比，而不是因为 LLM 每次细节差异都 fail。但*对 should_error 和 min_hypotheses=1 这两类"硬"断言不够抗噪*，要配合小样本多次采样。

### 3.3 Prompt Caching

- *稳态命中率*：direct 66.3%（generate 缓存生效，prioritize 改造前不生效）→ 合并 `de5f11c` 后 prioritize 也缓存，预估 85%；strands 69.0%
- *与预期差异*：
  - TASK §2 预期 HypothesisAgent 40-55% 节省，实测 strands 命中率 69% 对应成本 ~60% 节省，*略超预期*
  - 但 direct 最初 66.3% 低于预期，原因是 prioritize 没加缓存 — *shared-notes §3.1 没说 prioritize 也要缓存*
- *对"部分缓存"模块（Runner/DR）的启示*：
  - Chaos Runner 和 DR Executor 的 system 被设计成"稳定段 + 可变段"，但如果稳定段短于 1024 tokens 照样失效。*建议* shared-notes §3.5/3.6 加"稳定段字数下限 4000 chars（中文）"硬约束。
- *profile 改动导致 cache invalidation 的实际冲击*：未观察到（本模块 TASK 期间 profile 没改）。*待验证*：下个 profile 更新（Wave 6 若有）需要记录 cache_write 重新出现的成本。

### 3.4 Strands SDK 使用失败点

- `cache_prompt` / `cache_tools` 行为符合文档，但 *deprecated warning 污染测试输出*（53 条/20 tests）
- ReAct 多轮 tool selection 有意外：
  - *S016 应该 error 却成功生成*：Strands agent 把"service_filter 不存在"当成"给个近似的"，自己假设是新服务生成了 5 个合理的 pod_kill 假设。这个"积极补救"在业务上其实很好（用户拼错名字时 agent 纠正并输出），但违反 should_error 断言
  - *没陷入循环*，也没重复调 tool，表现比 Smart Query L1 q014 更稳定
- *BedrockModel 的 retry / timeout 表现*：
  - 5 分钟 read timeout 会触发 botocore adaptive retry 5 次 ≈ 25 分钟才放弃 → 实际 golden 跑在 S008/S010/S018 耗 40+ 分钟。*建议*：`build_bedrock_model` 加 `read_timeout=90` 参数，快速失败更好。
  - MaxTokensReached（S018）不是 timeout，而是生成超 4096 tokens 被 Strands runtime 抛 exception。这个*正是 Strands 吃亏 direct 的地方* — direct 只要 Bedrock 返回就完事，不管是不是 truncated；Strands runtime 额外校验。
- *与 L0/L1/L2 的 Strands 使用差异*：L2 prompt caching 在 Smart Query 是 L1 功能的优化层；Hypothesis 是 L0 级新接入，所以踩 L2 已经踩过的坑又踩了一遍（chars vs tokens）。*建议* shared-notes 加一节 "L2 已知坑清单" 作为 Phase 3 每模块必读。

### 3.5 图谱依赖（Neptune 查询等）

- *查询性能不是瓶颈*：`query_topology` 返 14 个 service ~ 500ms
- *新增 tool 的必要*：*暂无*。4 个 @tool 够用，Strands agent 可以灵活组合
- *但*：hypothesis_tools.py 的 `query_infra_snapshot` 偷懒直接调 DirectBedrockHypothesis._query_infra_snapshot，*建议*下个模块（比如 LearningAgent）不要再依赖 Direct class 作为 helper，应该把 Neptune 查询 helper 抽到 `chaos/code/runner/neptune_helpers.py`。

## 4. 跨猫协作

- *sessions_send 消息格式够用* — 但每次结果都要带"commit hash + 断言数字"才能让架构审阅猫（和大乖乖）判断进度
- *TASK 引用的跨文件 path 有不准*：`rca/agents/hypothesis_direct.py`（错） vs `chaos/code/agents/`（实际）
- *heartbeat / stalled 检测未触发*（Bedrock read-timeout 时 pytest 还在跑，只是慢）
- *需要找大乖乖确认的决策有 3 个*：(1) Gate A 豁免；(2) experiments/ 恢复 repo；(3) cache hit 未达门槛是否接受。其中 (1)(3) *本可以预先写进 TASK 的 decision tree*：
  - "Gate A 条件不全满足时：人工豁免条件 = xxx；不可豁免 = yyy"
  - "cache hit 未达 ≥70% 时：自动路径 = 调 prioritize；降级路径 = 接受并写进 retro"
- *L1/L2 的被推翻的设计*：无。但 Smart Query L2 的 "3000 chars 门槛"被 Hypothesis 场景证明*不适用所有模块*，应该改成 tokens 单位。

## 5. 时间 / 成本

- *最长阶段*：direct Golden 跑 21 min（S008/S018 Bedrock read timeout 各 13 min）
- *可并行化步骤*：
  - sample_for_golden.py 和 PR5 的测试框架编写可以并行（我当时串行了）
  - Direct Golden 和 Strands Golden 如果不担心 Bedrock rate limit 可以并行
- *总 Bedrock 成本*（估算）：
  - sample 20×2 = 40 次 generate ≈ $0.4
  - direct Golden 20 次 generate+prioritize ≈ $0.8
  - strands Golden 20 次 × 3 cycles ≈ $1.5（缓存生效后降低）
  - 总计 ≈ **$3**

## 6. 给下个模块的 Top 3 建议 ⭐⭐⭐

1. **Sample 脚本必须抗 SIGPIPE + 场景级 try/except + 进度写文件**
   每个模块的 `sample_for_golden.py` 不要直接 stderr print，改为 `logging.FileHandler` 写 `experiments/strands-poc/samples/<module>/run.log`。主循环外层必须 try/except 包每个 scenario，失败打印场景 ID 后继续。下个模块若是 LearningAgent，场景 60 min+ 不要让一个 Bedrock 偶发错误杀掉整组。

2. **Prompt Caching 必须按 tokens 算，不按 chars**
   `rca/engines/strands_common.py` 应添加辅助：
   ```python
   def assert_cacheable(system_prompt: str, min_tokens: int = 1024):
       # 用 tiktoken / anthropic 官方 tokenizer 精确测
       ...
   ```
   每个模块的 Strands/Direct engine `__init__` 调用它，而不是 `assert len(sys) > 3000`。下个模块（LearningAgent）system prompt 估计更短（分析型），不用 tokens 计量很容易踩。

3. **Gate B 的硬门槛需要提前写决策树**
   TASK §8 应该增加一节 "Gate B 不达标时的决策矩阵"，例如：
   - direct golden ≥18 && strands golden <18 → (A) 针对性修 strands; (B) 接受差异，Week 2 继续灰度一周再冲
   - cache hit <70% → (A) 找缓存未覆盖的调用点加缓存; (B) 降低门槛到 60% 若是架构原因
   - 等等。
   这能避免每次达不到门槛都要 sessions_send 问大乖乖，把决策成本前置。

## 7. 给架构审阅猫的写 TASK 模板修改建议

- *`TASK-phase3-shared-notes.md` §3 "按模块的 Prompt Caching 设计要点"* 每个子节补 "稳定段最低字数（中文） ≥ 4000 chars"，当前表格只列 "2-3k tokens" 这种口语估计
- *shared-notes §4 "每个模块 TASK 必备章节模板"* 应加 "### 本模块依赖的 env 变量清单" 一节
- *shared-notes §7.1 "单元测试 (不含 RUN_GOLDEN)"* 应提醒 "chaos/code 模块的测试需要 sys.path 预配置，参考 `chaos/code/main.py` 开头"
- *shared-notes §8.4 retro 模板* 的 § 3.1 "踩到的坑" 应该是*枚举+对后续建议配对*（我这里已经用了编号格式），建议把它写进模板里强制结构
- *下个 TASK-phase3-<module>.md 模板* 必须加：
  - *§2 前置条件* 分 "可豁免 / 不可豁免" 两列
  - *§4.5 Golden Set* 明写 "sample 脚本必须抗 SIGPIPE + 场景级 try/except"
  - *§8 Gate B* 加"决策矩阵"子节
- *timeline.md 的 direct_file 路径* 在每次模块启动前必须 grep 验证，不能 Phase 1 拍脑袋填

---

*架构审阅猫在写 LearningAgent TASK 前必须 read 本文件，并在下个 TASK 的相应章节落实 § 6 Top 3 建议。*
