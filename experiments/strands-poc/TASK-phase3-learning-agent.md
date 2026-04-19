# TASK: Phase 3 Module 2 — LearningAgent 迁移到 Strands Agents

> **任务归属**: 编程猫
> **发起**: 大乖乖 + 架构审阅猫
> **日期**: 2026-04-XX（Gate A 通过后启动）
> **范围**: 把 `chaos/code/agents/learning_agent.py` 从直调 Bedrock 迁到 Strands Agents
> **预计工作量**: 3 周（migration-strategy § 3.3 Phase 3 Week 7-9 标准流程）
> **成功标志**: 3 个切换阶段 PR 全部合入 main + 所有验收门槛达标 + `learning_direct.py` 冻结
> **前序模块**: HypothesisAgent（Phase 3 Module 1，已冻结 2026-04-18）

---

## 0. 必读顺序

1. `migration-strategy.md`（§ 3.3 Phase 3 + § 6.3 切换门槛 + § 6.4 Prompt Caching）
2. `TASK-phase3-shared-notes.md`（共享规范，**§ 3.2 是 LearningAgent 的缓存设计要点**）
3. `retros/hypothesis-retro.md`（**必读** — § 6 Top 3 建议 + § 7 模板修改建议，直接影响本模块执行）
4. `TASK-phase3-hypothesis-agent.md`（结构参考）
5. `TASK-L2-prompt-caching.md`（Prompt Caching 实现参考）
6. `report.md`（L0/L1/L2 + Phase 3 Module 1 经验总结）
7. **当前模块代码**：
   - `chaos/code/agents/learning_agent.py` — 465 行，现版实现
   - `chaos/code/agents/hypothesis_agent.py` — 向后兼容 shim（Module 1 产出）
   - `chaos/code/runner/config.py` — `REGION`
   - `chaos/code/runner/neptune_client.py` — Gremlin 查询封装

---

## 1. 工作目录

```
/home/ubuntu/tech/graph-dependency-platform
```

---

## 2. 前置条件（Gate A 确认）

启动本任务前必须确认：

### 不可豁免（必须全部满足）

- [ ] HypothesisAgent（Module 1）已冻结，`timeline.md` status = `frozen`
- [ ] `make_hypothesis_engine()` 在 orchestrator 中稳定工作 ≥ 1 周
- [ ] Bedrock 月度预算足够承受 +20-30% 临时增幅（LearningAgent LLM 调用少，增幅低于 Module 1）
- [ ] `chaos/code/agents/learning_agent.py` 无进行中的功能开发（冻结窗口确认）

### 可豁免（大乖乖 Slack 确认即可）

- [ ] Smart Query L2 稳定期 ≥ 4 周（可口头豁免，但需记录）
- [ ] `docs/migration/timeline.md` 中 `learning-agent` 的 `freeze_date` / `delete_date` 已填（可在 Week 3 补填）
- [ ] 大乖乖已打 tag `v-last-direct-learning-YYYYMMDD`（可在 PR7 时补打）

*不可豁免项任一不满足 → 暂停启动，通知大乖乖。*

---

## 3. 本模块依赖的 env 变量清单

| 变量名 | 用途 | 与其他模块差异 |
|--------|------|---------------|
| `NEPTUNE_HOST` | Neptune 集群地址 | ⚠️ **不是 `NEPTUNE_ENDPOINT`**（Smart Query 用 `NEPTUNE_ENDPOINT`，chaos 模块用 `NEPTUNE_HOST`）|
| `BEDROCK_REGION` | Bedrock 调用区域 | 同 HypothesisAgent |
| `BEDROCK_MODEL` | Bedrock 模型 ID | 同 HypothesisAgent |
| `LEARNING_ENGINE` | 引擎切换 `direct\|strands` | 本模块专用（对应 HypothesisAgent 的 `HYPOTHESIS_ENGINE`）|
| `DYNAMODB_TABLE_EXPERIMENTS` | 实验历史表 | 本模块专用，`ExperimentQueryClient` 读取 |
| `HYPOTHESES_PATH` | 假设文件路径 | 继承自 HypothesisAgent |

> ⚠️ **sys.path 提醒**：`chaos/code/main.py` 的 sys.path 默认不含 `rca/`，PR6（wire factory）时必须检查 `from rca.engines.factory import make_learning_engine` 是否需要手动加路径。参考 HypothesisAgent Module 1 踩过的同一个坑（retro § 3.1 坑 5）。

---

## 4. 任务范围（严格限定）

**做**：
- `chaos/code/agents/learning_agent.py` 从"直调 Bedrock"改造为"Strands Agents"
- 引入 `LearningBase` 抽象基类 + factory
- 建立 LearningAgent 的 Golden Set（10 个 coverage snapshot 场景）
- Prompt Caching 默认启用（system prompt）
- `LearningBase` 方法返回 dict 规范化（含 `engine` / `model_used` / `latency_ms` / `token_usage{input,output,cache_read,cache_write}` / `trace`）

**不做**：
- 不改 `ExperimentQueryClient`（DynamoDB 查询保持不变）
- 不改 `chaos/code/runner/` 其他模块
- 不改 HypothesisAgent（已冻结）
- 不加 Prober / PolicyGuard 等下游模块
- 不做"新增能力"（本任务只是 framework 迁移，业务逻辑等价）

---

## 5. 产出清单

### 5.1 抽象层（engines 包扩展）

修改 `rca/engines/base.py`（已存在，仅扩展）：

```python
class LearningBase(ABC):
    """Chaos 学习引擎基类。"""
    
    def __init__(self, hypothesis_engine=None, profile: Any = None): ...
    
    @abstractmethod
    def analyze(self, experiment_results: list[dict]) -> dict:
        """
        聚合实验结果，生成 coverage 分析。
        纯 Python 聚合逻辑，无 LLM 调用。
        Returns: { "coverage": dict, "engine": str, "latency_ms": int, ... }
        """
    
    @abstractmethod
    def iterate_hypotheses(self, coverage: dict) -> dict:
        """
        基于 coverage gap 调用 HypothesisAgent 补新假设。
        依赖 Phase 3 Module 1 产出的 make_hypothesis_engine()。
        Returns: { "new_hypotheses": list, "engine": str, ... }
        """
    
    @abstractmethod
    def update_graph(self, learning_data: dict) -> dict:
        """
        将学习结果写入 Neptune 图谱。
        Returns: { "vertices_updated": int, "edges_updated": int, ... }
        """
    
    @abstractmethod
    def generate_report(self, analysis: dict) -> dict:
        """
        生成 Markdown 报告。
        Returns: { "report_md": str, "engine": str, ... }
        """
    
    @abstractmethod
    def generate_recommendations(self, analysis: dict) -> dict:
        """
        ⚡ 唯一的 LLM 调用点。
        Returns: {
            "recommendations": list[str],
            "engine": str,
            "model_used": str | None,
            "latency_ms": int,
            "token_usage": {
                "input": int, "output": int, "total": int,
                "cache_read": int, "cache_write": int,
            } | None,
            "trace": list[dict],
            "error": str | None,
        }
        """
```

修改 `rca/engines/factory.py`：添加 `make_learning_engine(profile=None)`，读 env `LEARNING_ENGINE=direct|strands`。

### 5.2 具体实现

```
chaos/code/agents/
├── learning_agent.py          # 向后兼容 shim (re-export DirectBedrockLearning as LearningAgent)
├── learning_direct.py         # 原 LearningAgent 主体改名 DirectBedrockLearning，继承 LearningBase
├── learning_strands.py        # 新增 StrandsLearningAgent，继承 LearningBase
└── learning_tools.py          # Strands @tool 定义
```

### 5.3 Tool 定义（`learning_tools.py`）

```python
from strands import tool

@tool
def query_experiment_history(service: str = "", limit: int = 50) -> str:
    """查询 DynamoDB 实验历史记录。可选 service 过滤。"""
    ...

@tool
def query_coverage_snapshot(experiment_ids: list[str]) -> str:
    """查询指定实验的 coverage 快照数据。"""
    ...

@tool
def query_graph_learning_nodes(service: str = "") -> str:
    """查询 Neptune 中已有的学习节点和边。"""
    ...

@tool
def invoke_hypothesis_engine(coverage_gaps: list[str], max_hypotheses: int = 5) -> str:
    """基于 coverage gap 调用 HypothesisAgent 生成补充假设。
    依赖 make_hypothesis_engine()（Phase 3 Module 1 产出）。"""
    ...
```

> ⚠️ **Neptune 查询 helper 重构**：HypothesisAgent 的 `hypothesis_tools.py` 里 `query_infra_snapshot` 直接调了 Direct class 作为 helper（retro § 3.5 指出的问题）。本模块**不要再依赖 Direct class**。Neptune 查询 helper 应抽到 `chaos/code/runner/neptune_helpers.py`，`learning_tools.py` 和 `hypothesis_tools.py` 都从那里引用。

### 5.4 Strands engine 关键要求

- **BedrockModel**：`build_bedrock_model(cache_config=CacheConfig(strategy="auto"))` — 复用 `rca/engines/strands_common.py`
  > ⚠️ **`cache_prompt` 已 deprecated**（Strands 1.36+）：不要用 `cache_prompt="default"`，改用 `CacheConfig(strategy="auto")`。HypothesisAgent 跑测试时吐了 53 条 `UserWarning`（retro § 2.2）。
- **system prompt**：`build_learning_system_prompt(profile)` — 从 profile YAML 读取 coverage schema + 分析模板，**稳定前缀预计 3-5k tokens**
  > ⚠️ **接近 1024 token 缓存下限**：必须在 `__init__` 调用 `assert_cacheable(system_prompt, min_tokens=1024)` 验证（见 § 5.5）。如果不够 1024 tokens，需要补充 coverage 维度定义和评估规则说明文档使其达标。
- **禁用事项**：直调版的 retry / manual prompt 拼接逻辑不搬过来（交给 ReAct 多轮）
- **预计 Strands ReAct cycle 数**：1-2 cycles（LLM 调用点只有 `_generate_recommendations`，大部分是纯 Python 数据处理）
- **Strands session memory**：timeline.md 指出需要 session memory — 用于跨实验轮次保留 coverage 变化趋势上下文

### 5.5 Prompt Caching 集成

#### 参考实现
- `rca/neptune/nl_query_direct.py` — Direct 缓存参考
- `rca/engines/strands_common.py` — Strands 缓存参考

#### 本模块缓存对象
- coverage schema（维度定义 + 评估规则）
- 分析模板

#### 本模块不缓存的对象
- 本轮 coverage snapshot（JSON 数据）
- 上轮 verdict 历史

#### 本模块预期缓存命中率（稳态）
- ≥ 50%（调用频次低，稳态收益中等 20-30%）

#### assert_cacheable 强制检查

```python
# rca/engines/strands_common.py — 已有或需新增
def assert_cacheable(system_prompt: str, min_tokens: int = 1024):
    """用 tiktoken / anthropic tokenizer 精确验证 system prompt 达到缓存下限。
    不用 chars 估算 — HypothesisAgent retro 证实 chars 不可靠（中英文差异）。"""
    import tiktoken
    enc = tiktoken.encoding_for_model("cl100k_base")
    token_count = len(enc.encode(system_prompt))
    assert token_count >= min_tokens, (
        f"System prompt only {token_count} tokens, "
        f"below {min_tokens} minimum for Bedrock prompt caching. "
        f"Add coverage dimension definitions to reach threshold."
    )
```

每个引擎的 `__init__` 必须调用 `assert_cacheable(self.system_prompt, min_tokens=1024)`。

#### 本模块特殊陷阱
- system prompt 预计 3-5k tokens（分析型），**接近缓存下限**。如果 profile YAML 的 coverage schema 较短，可能不到 1024 tokens → 静默失效（Bedrock 不报错）
- 调用频次低（每轮实验 1 次），**冷启动后第 2 次调用才能看到 cache_read > 0**，不要被第一次 cache_read=0 误判为缓存失效
- 考虑与 HypothesisAgent 共享 base system prompt（如果模型相同可复用缓存池）

### 5.6 Golden Set

新增 `tests/golden/learning/`：

```
tests/golden/learning/
├── scenarios.yaml               # 10 个 coverage snapshot 场景
├── cases.yaml                   # 基于 direct 采样生成的行为约束式 golden
├── BASELINE-direct.md           # 每次 RUN_GOLDEN 时生成
└── BASELINE-strands.md
```

#### 5.6.1 Golden 的哲学：行为约束而非精确匹配

LearningAgent 的输出包含 coverage 分析 + 推荐建议，天然有发散性。**不硬编码 expected 精确列表，用行为约束**：

```yaml
# cases.yaml 单条结构：
- id: l001
  scenario: "petsite 跨 AZ 故障后 coverage 分析"
  coverage_snapshot_source: "experiments/strands-poc/fixtures/coverage_snapshot_l001.json"
  
  # 推荐数量范围
  min_recommendations: 1
  max_recommendations: 10
  
  # 推荐必须覆盖的维度（direct 采样 3 次后统计出的高频维度）
  dimension_must_include_any:
    - "availability"
    - "fault-isolation"
  
  # 绝对不应该出现的推荐类型（领域知识）
  recommendation_must_not_include:
    - "delete_service"     # 不合理的推荐
  
  # coverage 分数变化方向
  coverage_trend: "improving"  # or "stable" | "degrading"
```

#### 5.6.2 Golden 场景按业务风险分类（4 类分桶）

| 分桶 | 场景数 | 说明 | 场景 ID 范围 |
|------|--------|------|-------------|
| 核心服务 | 3 | petsite / payment / order — Tier0 服务的 coverage 分析 | l001-l003 |
| 特殊后端 | 3 | Lambda / RDS / ElastiCache — 非 K8s 后端的 coverage 特征 | l004-l006 |
| 错误输入 | 2 | 空 snapshot / 畸形 JSON — should_error 或 graceful fallback | l007-l008 |
| 边界 | 2 | 极大 snapshot（100+ 实验）/ 单实验 — 聚合逻辑边界 | l009-l010 |

#### 5.6.3 Golden 构建步骤（分两阶段，Week 1 完成）

**阶段 1：准备 10 个 coverage snapshot fixture**

从 DynamoDB 实验历史中导出 10 个有代表性的 snapshot：
```bash
# 导出脚本
chaos/code/agents/export_coverage_snapshots.py
# 输出到
experiments/strands-poc/fixtures/coverage_snapshot_l001.json ... l010.json
```

**阶段 2：用 direct 版采样建 baseline**

写 `chaos/code/agents/sample_for_golden_learning.py`：

> ⚠️ **抗 SIGPIPE + 场景级 try/except + 进度写文件**（retro § 6 Top 1 强制要求）：
> ```python
> import logging, signal
> signal.signal(signal.SIGPIPE, signal.SIG_DFL)
> 
> fh = logging.FileHandler("experiments/strands-poc/samples/learning/run.log")
> logger = logging.getLogger("sample_learning")
> logger.addHandler(fh)
> 
> for scenario in scenarios:
>     try:
>         # 调用 DirectBedrockLearning 采样 3 次
>         # 对 min_recommendations=1 或 空 snapshot 场景，采 5 次
>         ...
>         logger.info(f"✅ {scenario['id']} done, {len(results)} samples")
>     except Exception as e:
>         logger.error(f"❌ {scenario['id']} failed: {e}")
>         continue  # 单场景失败不挂整组
> ```

- 读 `scenarios.yaml`
- 对每个场景调用 `DirectBedrockLearning().generate_recommendations(...)` 连续 3 次（边界场景 5 次）
- 统计每个场景的推荐维度分布
- 输出 `cases.yaml` 草稿，由**人工 review** 后 commit

### 5.7 测试文件

- `tests/test_learning_golden.py` — engine matrix（direct / strands）parametrize
- `tests/test_learning_shadow.py` — direct vs strands 对比报告

### 5.8 文档

- `experiments/strands-poc/report.md` 新增 `§ 10 Phase 3 Module 2 — LearningAgent` 章节
- `docs/migration/timeline.md` 更新 `learning-agent.status` 从 `planned` → `active` → `frozen`（每阶段一次）
- `docs/migration/decisions/learning-migration-adr.md`（Week 3 切换完成后写 ADR）

---

## 6. LearningBase 接口规范

```python
class LearningBase(ABC):
    """Chaos 学习引擎基类。"""
    
    def __init__(self, hypothesis_engine=None, profile: Any = None):
        """
        hypothesis_engine: make_hypothesis_engine() 的返回值，
        用于 iterate_hypotheses() 补充假设。
        """
        ...
    
    @abstractmethod
    def analyze(self, experiment_results: list[dict]) -> dict:
        """
        纯 Python 聚合。无 LLM 调用。
        
        Returns:
            {
                "coverage": dict,          # coverage 维度分析
                "gaps": list[str],         # 未覆盖的维度
                "engine": str,             # "direct" | "strands"
                "latency_ms": int,
                "error": str | None,
            }
        """
    
    @abstractmethod
    def generate_recommendations(self, analysis: dict) -> dict:
        """
        ⚡ 唯一的 LLM 调用点。
        
        Returns:
            {
                "recommendations": list[str],
                "engine": str,
                "model_used": str | None,
                "latency_ms": int,
                "token_usage": {
                    "input": int, "output": int, "total": int,
                    "cache_read": int, "cache_write": int,
                } | None,
                "trace": list[dict],
                "error": str | None,
            }
        """
```

---

## 7. 硬约束

1. ⚠️ **业务行为等价** — `generate_recommendations()` 产出必须与直调版在相同输入下**行为可对齐**（通过 Golden Set 验证）
2. ⚠️ **不改 ExperimentQueryClient** — DynamoDB 查询保持不变，下游消费者零改动
3. ⚠️ **Prompt Caching 默认启用** — system prompt 必须缓存（参考 `TASK-phase3-shared-notes.md § 3.2`）；使用 `CacheConfig(strategy="auto")`，**不用已 deprecated 的 `cache_prompt`**
4. ⚠️ **Global inference profile** — 使用 `global.anthropic.claude-sonnet-4-6` 或 `global.anthropic.claude-opus-4-7`，不支持裸 model id
5. ⚠️ **Neptune 查询走 `neptune_helpers.py`** — 不依赖 Direct class 的私有方法作为 helper（retro § 3.5 建议），抽公共 helper 到 `chaos/code/runner/neptune_helpers.py`
6. ⚠️ **`chaos/code/agents/learning_agent.py` 保持可 import** — `from chaos.code.agents.learning_agent import LearningAgent` 兼容
7. ⚠️ **Strands engine 禁用任何人工 retry 逻辑** — 依赖 ReAct 原生多轮
8. ⚠️ **Token usage 必填** — `cache_read` / `cache_write` 字段必须有
9. ⚠️ **不动 `chaos/code/runner/`**（除了新增 `neptune_helpers.py`）— Runner 本 Phase 不迁移
10. ⚠️ **assert_cacheable 强制** — system prompt 必须通过 `assert_cacheable(prompt, min_tokens=1024)` 检查（按 tokens 算，不按 chars）
11. ⚠️ **依赖 Module 1 产出** — `iterate_hypotheses()` 必须通过 `make_hypothesis_engine()` 获取 HypothesisAgent 实例，不直接 import `DirectBedrockHypothesis`

---

## 8. 验证步骤

### 8.1 单元测试（本地，不真调）
```bash
cd chaos/code && PYTHONPATH=.:../..  pytest ../../tests/test_learning_golden.py -v -k "not goldenreal"
```

> ⚠️ **sys.path 提醒**：`chaos/code` 模块的测试需要 `PYTHONPATH` 预配置，参考 `chaos/code/main.py` 开头的 sys.path 添加逻辑。

### 8.2 Golden CI（真调 Bedrock + Neptune + DynamoDB，有成本）
```bash
cd chaos/code && PYTHONPATH=.:../.. RUN_GOLDEN=1 LEARNING_ENGINE=direct  pytest ../../tests/test_learning_golden.py -v
cd chaos/code && PYTHONPATH=.:../.. RUN_GOLDEN=1 LEARNING_ENGINE=strands pytest ../../tests/test_learning_golden.py -v
```

### 8.3 Shadow 对比
```bash
cd chaos/code && PYTHONPATH=.:../.. RUN_GOLDEN=1 pytest ../../tests/test_learning_shadow.py -v -s
```
输出：
- 每 case 的推荐数量差异（direct vs strands）
- 推荐维度分布对比
- 延迟倍数 / token 倍数 / 缓存命中率

### 8.4 缓存生效验证

使用公共 harness `experiments/strands-poc/verify_cache_common.py`（retro § 2.3 建议抽取的公共组件），本模块只需提供 factory + sample input：

```python
# experiments/strands-poc/verify_cache_learning.py
from verify_cache_common import run_cache_verification
from rca.engines.factory import make_learning_engine

run_cache_verification(
    engine_factory=make_learning_engine,
    sample_input={"analysis": SAMPLE_COVERAGE_ANALYSIS},
    method_name="generate_recommendations",
    repeat=3,
)
```

验证逻辑（公共 harness 实现）：
- 连续 3 次相同输入
- 第 1 次 `cache_write > 0`
- 第 2/3 次 `cache_read > 0`

### 8.5 集成验证
跑一次完整 chaos 实验流程（orchestrator → hypothesis → **learning** → runner），确保：
- `iterate_hypotheses()` 正确调用 `make_hypothesis_engine()` 获取 strands 实例
- `update_graph()` 正确写入 Neptune
- `generate_report()` 输出格式不变
- 下游 runner 能消费新版 LearningAgent 产出

---

## 9. 验收门槛（Gate B - Phase 3 Module 2 Week 3 检查）

| 指标 | 门槛 |
|------|------|
| Direct Golden | ≥ 9/10 |
| Strands Golden | ≥ direct - 1（即 strands golden ≥ direct_score - 1）|
| 业务等价性（行为约束维度） | 所有 case 满足 dimension_must_include_any / must_not_include |
| Shadow 纵向自测（同 engine 3 次）| 推荐维度分布差异 ≤ 20% |
| Shadow 横向对比（direct vs strands）| 推荐维度分布差异 ≤ 20% |
| Strands p50 latency | ≤ 1.5x direct（LLM 调用少，差距应更小）|
| 稳态缓存命中率（Strands）| ≥ 50% |
| 稳态缓存命中率（Direct）| ≥ 55% |
| Prompt Caching 带来的成本下降 | ≥ 20%（vs 本模块无缓存 baseline）|
| 1 周灰度 SEV-1/2 | 0 |
| 集成测试（orchestrator → learning → runner）| 通过 |
| assert_cacheable 检查 | 两个 engine 的 system prompt 均 ≥ 1024 tokens |

### Gate B 不达标时的决策矩阵

| 情况 | 路径 A（首选） | 路径 B（降级） |
|------|---------------|---------------|
| direct golden ≥ 9 但 strands golden < direct - 1 | 针对性修 strands 未通过的 case，重跑 Golden | 接受差异，Week 2 继续灰度 1 周后重新评估 |
| 缓存命中率 < 50% | 检查 system prompt 是否达 1024 tokens 下限；补充 coverage 定义文档 | 降低门槛到 40%，写入 retro 说明架构原因 |
| Strands latency > 1.5x direct | 检查 ReAct cycle 数是否异常（应为 1-2）；减少不必要的 tool call | 接受 2x 上限，LearningAgent 调用频次低影响有限 |
| 集成测试失败 | 检查 `make_hypothesis_engine()` 是否正确返回；检查 Neptune 写入权限 | 回退到 direct，排查后重试（最多 2 次） |

**同一问题 3 次失败 → 停手，写入 progress log，通知大乖乖。**

---

## 10. Git 提交策略（7 个 PR）

| # | PR | 范围 |
|---|-----|------|
| 1 | `feat(engines): add LearningBase + factory` | 抽象层 |
| 2 | `refactor(learning): rename LearningAgent → DirectBedrockLearning + shim` | rename only，无行为变化 |
| 3 | `refactor(neptune): extract neptune_helpers.py from Direct classes` | Neptune 查询 helper 抽取（惠及后续所有模块）|
| 4 | `feat(learning): add StrandsLearningAgent + learning_tools` | 新增 Strands 实现 + Prompt Caching |
| 5 | `test(learning): golden set + engine matrix + shadow` | 测试 + 10 cases |
| 6 | `feat(learning): wire factory into main.py` | 调用方切 factory |
| 7 | `docs(migration): freeze learning_direct.py + ADR` | 切换完成 + 冻结标记 |

每个 PR 单独 review。PR 7 合并时必须同步更新 `docs/migration/timeline.md`（`status: frozen`）。

---

## 11. 3 周流程（参考 migration-strategy § 3.3）

### Week 1
- PR 1 + 2 + 3 + 4 + 5 合并
- 跑 Golden CI 两个 engine 拿到 baseline 数字
- 跑 `verify_cache_learning.py` 确认缓存生效
- ⚠️ 如果 system prompt < 1024 tokens → 补充 coverage 维度定义达标

### Week 2
- PR 6 合并（main.py 切 factory）
- env `LEARNING_ENGINE=strands` 在灰度节点开启
- 每日 shadow 对比 + 看板监控
- 任何 SEV-1/2 事件立即回滚 env

### Week 3
- Gate B 检查所有门槛（参照 § 9 决策矩阵处理不达标项）
- 通过 → PR 7 合并（冻结），打 tag `v-strands-cutover-learning-YYYYMMDD`
- 未通过 → 延期 1 周并写 ADR 说明原因；同一模块最多延 3 次，否则升级到大乖乖决策

---

## 12. 不要做的事

1. ❌ 不改 `ExperimentQueryClient`（DynamoDB 查询保持不变）
2. ❌ 不改已冻结的 `hypothesis_direct.py`
3. ❌ 不把 LearningAgent 和 Prober 合并迁移（违反"串行"纪律）
4. ❌ 不跳过 Golden Set 建立（必须 10 个 cases）
5. ❌ 不跳过 Prompt Caching（默认启用，不接受"这次先不做"）
6. ❌ 不在 Strands 版保留任何手工 retry 逻辑
7. ❌ 不用 `apac.*` / `us.*` / 裸 model id（L0 Spike 已验证不可用）
8. ❌ 不在冻结期 `DirectBedrockLearning` 里加新功能
9. ❌ 不自行决定延期删除日（必须走 ADR）
10. ❌ 不用 `cache_prompt="default"`（已 deprecated） → 用 `CacheConfig(strategy="auto")`
11. ❌ 不用 chars 估算缓存下限 → 用 `assert_cacheable(prompt, min_tokens=1024)`
12. ❌ 不依赖 Direct class 的私有方法做 Neptune 查询 → 用 `neptune_helpers.py`

---

## 13. 失败处理

- **Golden 大量回归** → 检查 agent 规则是否丢了推荐维度约束；LearningAgent 的 LLM 调用只有 1 个，排查范围比 HypothesisAgent 更小
- **缓存命中率 < 50%** → 首先检查 system prompt tokens 数（assert_cacheable 应该已拦截）；其次检查是否有动态拼接导致 miss
- **system prompt < 1024 tokens** → 补充 coverage schema 维度定义 + 评估规则详细说明，使稳定前缀达标
- **Strands `BedrockModel` 不支持 `CacheConfig`** → fallback 到 `additional_request_fields`（参考 L2 POC）
- **`iterate_hypotheses()` 调 `make_hypothesis_engine()` 失败** → 检查 `HYPOTHESIS_ENGINE` env 和 Module 1 冻结是否完整
- **sample_for_golden 脚本挂了** → 检查是否按 retro § 6 Top 1 实现了抗 SIGPIPE + 场景级 try/except + 进度写文件
- **同一问题 3 次失败** → 停手，写入 progress log，通知大乖乖

---

## 14. LearningAgent 特殊点备忘

### 14.1 迁移范围小

LLM 调用只有 `_generate_recommendations()` 一个点。`analyze()` 是纯 Python 聚合、`iterate_hypotheses()` 委托给 HypothesisAgent、`update_graph()` 是 Neptune 写入、`generate_report()` 是模板渲染。**Strands 迁移的核心只有 `_generate_recommendations()`**，其余方法几乎是 copy-paste。

### 14.2 依赖 Module 1 产出

`iterate_hypotheses()` 调用 `make_hypothesis_engine()` 获取 HypothesisAgent 实例。这意味着：
- Module 1 必须已冻结且稳定（§ 2 不可豁免条件）
- 测试时需要 `HYPOTHESIS_ENGINE` env 配置
- Golden Set 的 `iterate_hypotheses` 相关 case 依赖 Module 1 的行为稳定性

### 14.3 Golden Set 用 coverage snapshot

Golden Set 基于 DynamoDB 实验历史快照（fixture 文件），**不是实时查询**。这避免了测试结果受实时数据变化影响，但需要定期更新 fixture（建议每月一次或 profile 变更后）。

### 14.4 缓存收益预期

调用频次低（每轮实验 1 次），缓存稳态收益中等（20-30%）。**不要因为收益低就跳过** — 缓存是默认动作，收益数据记录在 BASELINE 里供后续决策参考。

---

## 15. 参考资料

- `chaos/code/agents/hypothesis_agent.py` — Module 1 向后兼容 shim（迁移后结构参考）
- `chaos/code/agents/hypothesis_strands.py` — Module 1 Strands 实现参考
- `rca/engines/strands_common.py` — BedrockModel + OTel helper
- `tests/test_hypothesis_golden.py` — Golden CI 框架（可复用 parametrize 模式）
- `experiments/strands-poc/retros/hypothesis-retro.md` — Module 1 retrospective（**必读**）
- AWS Strands Agents 文档：<https://strandsagents.com/>
- Bedrock Prompt Caching 文档：<https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html>

---

## 16. 完成标志

1. ✅ 7 个 PR 全部合并 main
2. ✅ 8.1-8.5 验证步骤全部通过
3. ✅ 9 验收门槛全部达标（或按决策矩阵处理）
4. ✅ `BASELINE-direct.md` + `BASELINE-strands.md` 入仓
5. ✅ `docs/migration/timeline.md` 更新 `learning-agent.status: frozen`
6. ✅ tag `v-strands-cutover-learning-YYYYMMDD` 已打
7. ✅ `docs/migration/decisions/learning-migration-adr.md` 落地
8. ✅ `report.md § 10` 写完，含延迟/成本/缓存命中率数据
9. ✅ main.py 成功调用 `make_learning_engine()` 获取 strands 实例，下游 runner 消费无异常
10. ✅ `neptune_helpers.py` 抽取完成，`hypothesis_tools.py` 也已迁移到使用它
11. ✅ **Retrospective 已交**：`experiments/strands-poc/retros/learning-retro.md` 写完（模板见 `TASK-phase3-shared-notes.md § 8`），重点交付 § 6 Top 3 建议 + § 7 给架构审阅猫的模板修改建议
12. ✅ sessions_send 给架构审阅猫一条 `[RETRO] learning 完成，Top 3: ...` 消息

---

**下一个模块**：RCA Layer2 Probers（Phase 3 Week 10-12），TASK 文件在本模块完成后才发布。

**⚠️ timeline.md 注意**：`direct_file: rca/agents/learning_direct.py` 是占位路径，实际代码在 `chaos/code/agents/learning_direct.py`。PR1 合并前必须修正 timeline.md。

**遇到任何不确定的地方，先在 Slack 里问大乖乖或架构审阅猫，不要自己拍脑袋决定。**
