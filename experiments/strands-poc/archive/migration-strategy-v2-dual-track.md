# Strands Agents 全平台迁移策略

> **日期**: 2026-04-18（v3 Strangler Fig 单向迁移，已定为正式策略）
> **作者**: 架构审阅猫（基于 L0 Spike report.md 的后续决策文档）
> **状态**: Draft，待 L1 POC 验证后确定执行路径
> **变更历史**:
> - v1 2026-04-18 早：初稿，基于 L0 Spike 结果
> - v2 2026-04-18 下午：Smart Query Wave 1-5（profile 化 + golden CI + 空结果重试 + Opus 升级）已合并 main，更新决策前提、接口设计、L1 POC 范围
> **相关文档**:
> - `experiments/strands-poc/report.md` — L0 Spike 验证结果
> - `blog/gp-improve/smart-query-accuracy-improvement.md` — Smart Query 改进计划（含 P1' profile 配置化）
> - `blog/gp-improve/harness-resilience-testing/improvements.md` — Phase 2/3 多 Agent 场景清单

---

## 1. 目标

为 graph-dependency-platform 提供 **从 "直调 Bedrock" 到 "Strands Agents 框架"** 的全平台迁移路径，覆盖：

- Smart Query 自然语言查询引擎（`rca/neptune/nl_query.py`）
- HypothesisAgent（chaos 假设生成）
- LearningAgent（coverage 分析 + 迭代学习）
- RCA Layer2 Probers（6 个并行 Prober）
- ChaosRunner / DR PolicyGuard / Resilience Score 计算
- 任何未来新增的 Agent 场景

核心问题：**代码仓库如何在迁移期间既保留现版 direct 实现，又承载 Strands 新版？**

---

## 2. 决策前提

迁移前必须已完成：

1. ✅ **L0 Spike 通过**（`experiments/strands-poc/report.md` — 6 硬约束全 YES，L1 GO 有条件）
2. ⏳ **L1 Smart Query POC 通过** — shadow 对跑 golden set，Strands 准确率 ≥ baseline - 5%、p50 ≤ 2x、token 成本 ≤ 3.5x
3. ✅ **P1' profile 配置化落地**（Wave 1 合并，`profiles/petsite.yaml` 已成为 schema/few-shot/guard 唯一来源）
4. ✅ **Golden Query Set + 准确率 CI**（Wave 3 合并，`tests/golden/petsite.yaml` 20 cases + `test_golden_accuracy.py`，当前 baseline 20/20 = 100%）
5. ✅ **空结果自动重试**（Wave 4 合并，带否定查询白名单 `_NEGATION_HINTS`）
6. ✅ **复杂问题 Opus 升级**（Wave 5 合并，从 profile `complex_keywords` 读需求）

⚠️ 任何一项未达标都不进入全平台迁移阶段。

**Wave 1-5 对 Strands 迁移的影响**：
- 👍 profile 化 + golden CI 两块硬基础已就位，L1 POC 可以*直接跑 shadow 对跑*不需要再建度量体系
- 👍 baseline 20/20 pass 是明确的准确率红线，Strands 不达到 19/20 就不能 cutover
- ⚠️ Wave 4 的 `retried: bool` 字段 + Wave 5 的 `_select_model` 都需要在 Strands 版中有等价实现
- ⚠️ `NLQueryEngine.query()` 返回 dict 新增了 `retried` 字段，抽象接口需要规范化

---

## 3. 四种仓库组织方案

### 方案 1：双实现并列 + 统一 factory

每个 Agent 都有 `*_direct.py` + `*_strands.py` 两份，env 开关切换。

**目录结构**：
```
rca/
├── engines/
│   ├── base.py               # 抽象基类 NLQueryBase / HypothesisBase ...
│   └── factory.py            # make_engine("nlquery"|"hypothesis"|...)
├── neptune/
│   ├── nl_query_direct.py    # 现版
│   └── nl_query_strands.py
├── agents/
│   ├── hypothesis_direct.py
│   ├── hypothesis_strands.py
│   ├── learning_direct.py
│   └── learning_strands.py
├── probers/
│   ├── layer2_direct.py
│   └── layer2_strands.py     # multi-agent 编排
└── chaos/
    ├── runner_direct.py
    └── runner_strands.py
```

**切换方式**：
```bash
AGENT_FRAMEWORK=direct    # 全部 direct
AGENT_FRAMEWORK=strands   # 全部 strands
AGENT_FRAMEWORK=mixed     # 按模块：NLQUERY_ENGINE=strands HYPOTHESIS_ENGINE=direct ...
```

**优点**：
- 100% 可回滚，任一模块挂了单独切回 direct
- 灰度能力最强

**缺点**：
- 代码量翻倍，维护双轨成本重
- 6 个月后 direct 版会变僵尸代码
- Bug 要修两次

**适合**：生产稳定性要求极高，3-6 个月双轨灰度，最终删 direct。

---

### 方案 2：长期 Branch 隔离（Git-native）⭐

主干保持 direct 现状，开长期分支 `strands-migration` 全面重写。

**分支结构**：
```
main                         # direct 实现，持续维护生产
├── strands-migration        # 长期分支，全平台 Strands 化
│   ├── feature/smart-query-strands
│   ├── feature/hypothesis-strands
│   ├── feature/learning-strands
│   └── feature/rca-layer2-strands
└── release/*                # 生产部署分支
```

**里程碑**：
1. 每个子模块迁移完成 → 合入 `strands-migration`，跑 golden
2. 所有模块完成 + 指标达标 → `strands-migration` → `main` 单次合并（big bang cutover）
3. 合并前打 tag `v-last-direct-YYYYMMDD` 作回滚点
4. 合并后 1 个月内遗留问题全修完，删除 direct 残留

**优点**：
- 主干干净，一套实现
- 实验阶段不污染生产代码
- Git 历史清晰，可随时 checkout 回看旧版

**缺点**：
- ⚠️ 长期分支的 merge conflict 随时间爆炸（6 个月后合主干可能 1-2 周）
- 不能同机跑两版（要切分支 + 重启）
- 无法渐进上线，风险集中爆发

**适合**：团队 1-2 人，迁移期 ≤ 2 个月，能接受 big bang cutover。

**缓解 merge conflict**：
- 每周把 `main` rebase 到 `strands-migration`
- `main` 的 bugfix 做小 patch cherry-pick 到 migration

---

### 方案 3：Monorepo 多包（最长远）

按 package 拆分，Strands 版是独立包。

**目录结构**：
```
graph-dependency-platform/
├── packages/
│   ├── gp-core/              # neptune_client / query_guard / profiles
│   │   └── pyproject.toml
│   ├── gp-agents-direct/     # 现版 Agent 实现
│   │   └── pyproject.toml
│   ├── gp-agents-strands/    # Strands 版
│   │   └── pyproject.toml
│   └── gp-app/               # Streamlit + orchestrator
│       └── pyproject.toml    # extras: [direct] or [strands]
├── pyproject.toml            # workspace root (uv workspace / hatch workspace)
└── deployment/
    ├── direct.yaml
    └── strands.yaml
```

**切换方式**：
```bash
pip install "gp-app[direct]"     # 生产
pip install "gp-app[strands]"    # POC
```

**优点**：
- 接口纪律最强（跨包只能走 public API）
- 两版独立打包、独立版本、独立测试
- 未来可加 `gp-agents-langgraph` / `gp-agents-bedrock-agents` 多实现竞速
- 真到第三个客户复用时天然支持

**缺点**：
- 改造成本最大（~1-2 人日拆包）
- 需要 workspace 工具（uv / hatch / pdm）
- 小团队大概率过度工程

**适合**：平台运营 ≥ 2 年、2+ 客户、多框架长期共存。

---

### 方案 4：单干净实现 + Git 标签归档

最激进：全面改 Strands，不保留双实现。

**流程**：
1. `git tag v-last-direct-20260418`
2. `main` 上分多个 PR 逐个模块替换 direct → strands
3. 每个 PR 跑 golden set，不达标不合
4. 全部替换完成 → direct 代码永久消失在 git 历史里
5. 生产回滚 = `git checkout v-last-direct + 重部署`

**优点**：
- 代码最干净，零维护负担
- 不留双轨诱惑

**缺点**：
- ⚠️ 回滚成本高（CDK / ECR / Neptune schema 全要重跑）
- 不支持灰度
- 无 safety net（客户 PA 视角强烈反对）

**适合**：对 Strands 信心极强 + 完整 E2E 测试 + 快速回滚能力。

---

## 4. 推荐路径：方案 2 + 方案 1 的灰度片段 ⭐

**阶段 1：Smart Query L1 POC（1 周，main 分支）**

- 只动 `rca/neptune/`
- 用方案 1 的 factory 模式：`nl_query.py`（现版改名 `DirectBedrockNLQuery`）+ `nl_query_strands.py` + `nl_query_factory.py`
- env 开关：`NLQUERY_ENGINE=direct|strands`
- Strands 做可选依赖（`pyproject.toml` extras_require）+ 软失败 import
- Streamlit `?engine=strands|baseline|both` shadow 对跑
- L1 验收指标见 `experiments/strands-poc/report.md` 第 4 节

**阶段 2：开长期分支（Smart Query 通过后）**

```bash
git tag v-last-direct-20260418
git checkout -b strands-migration
```

每个模块起 feature 分支：
```
feature/hypothesis-strands
feature/learning-strands
feature/rca-layer2-strands
feature/chaos-runner-strands
feature/policy-guard-strands
feature/resilience-score-strands
```

每个 feature 完成后：
1. 合入 `strands-migration`（不合 main）
2. 跑该模块的 golden set + 回归测
3. 保留 direct 实现作为 factory 回退路径

**阶段 3：集成验证（2-4 周）**

在 `strands-migration` 分支上：
1. 跑整套 E2E 测试（Smart Query + Chaos 实验 + RCA + DR）
2. 观察 token 成本、延迟、可观测性（OTel trace → CloudWatch）
3. 端到端 golden set 1 周稳定运行
4. 每周 rebase `main` 防止 conflict 堆积

**阶段 4：Big Bang Cutover**

1. 再次打 tag `v-last-direct-final-YYYYMMDD`
2. PR: `strands-migration` → `main`
3. 合并后 24h 内严密监控（CloudWatch + golden CI 每小时跑）
4. env 开关 `AGENT_FRAMEWORK=direct` 保留 1 个月作紧急回滚

**阶段 5：清理（cutover 后 1 个月）**

前提：生产无回滚事件、指标全绿。

- 删除所有 `*_direct.py` 实现
- 删除 factory 的 direct 分支
- `pyproject.toml` 中 `strands` 从 extras 改为 required
- 保留 `docs/migration/strands-migration-history.md` 作为决策记录

---

## 5. 配置与依赖管理

### 5.1 依赖策略

**推荐：extras_require + 软失败 import**

```toml
# pyproject.toml
[project]
dependencies = [
    "boto3",
    "pyyaml",
    # ... 现有核心
]

[project.optional-dependencies]
strands = [
    "strands-agents>=1.36",
    "strands-agents-tools>=0.5",
]
```

**软失败 import**（每个 `*_strands.py` 文件顶部）：

```python
try:
    from strands import Agent, tool
    from strands.models import BedrockModel
    _STRANDS_AVAILABLE = True
except ImportError:
    _STRANDS_AVAILABLE = False


class StrandsNLQueryEngine(NLQueryBase):
    def __init__(self, profile=None):
        if not _STRANDS_AVAILABLE:
            raise RuntimeError(
                "Strands engine 需要: pip install -e '.[strands]'"
            )
        ...
```

Factory 回退：

```python
def make_engine(profile=None):
    engine = os.environ.get("NLQUERY_ENGINE", "direct").lower()
    if engine == "strands":
        try:
            from neptune.nl_query_strands import StrandsNLQueryEngine
            return StrandsNLQueryEngine(profile=profile)
        except RuntimeError as e:
            logger.warning(f"Strands 不可用，回退 direct: {e}")
    from neptune.nl_query_direct import DirectBedrockNLQuery
    return DirectBedrockNLQuery(profile=profile)
```

### 5.2 环境变量约定

```bash
# 全局开关（阶段 4 前有效）
AGENT_FRAMEWORK=direct|strands|mixed    # 默认 direct

# mixed 模式下按模块切换
NLQUERY_ENGINE=direct|strands
HYPOTHESIS_ENGINE=direct|strands
LEARNING_ENGINE=direct|strands
RCA_LAYER2_ENGINE=direct|strands
CHAOS_RUNNER_ENGINE=direct|strands
POLICY_GUARD_ENGINE=direct|strands

# Strands 专属
STRANDS_MODEL_ID=global.anthropic.claude-sonnet-4-6
STRANDS_REGION=ap-northeast-1
STRANDS_OTEL_ENDPOINT=https://xray.ap-northeast-1.amazonaws.com    # 可选
```

### 5.3 部署配置

**direct 部署**（默认生产）：
```bash
pip install -e .
export AGENT_FRAMEWORK=direct
```

**strands 部署**（阶段 1-3 POC）：
```bash
pip install -e ".[strands]"
export AGENT_FRAMEWORK=strands
export STRANDS_MODEL_ID=global.anthropic.claude-sonnet-4-6
```

---

## 6. 抽象接口设计

迁移前每个 Agent 类型需要抽出基类。以 NLQuery 为例：

```python
# rca/engines/base.py
from abc import ABC, abstractmethod
from typing import Any

class NLQueryBase(ABC):
    """自然语言图查询引擎接口。

    规范化 Wave 1-5 已有的返回结构，新增迁移期元数据字段。
    """

    def __init__(self, profile: Any = None): ...

    @abstractmethod
    def query(self, question: str) -> dict:
        """
        Returns:
            {
              # --- Wave 1-5 已存在字段 ---
              "question": str,
              "cypher": str,
              "results": list,
              "summary": str,
              "retried": bool,           # Wave 4：空结果重试
              # --- 迁移期新增字段（所有实现必须提供） ---
              "engine": str,             # "direct" | "strands"
              "model_used": str,         # Wave 5：实际用的 model id（sonnet / opus）
              "latency_ms": int,
              "token_usage": {           # 可选：便于监控成本
                  "input": int, "output": int, "total": int
              } | None,
              "trace": list[dict],       # Strands 的 tool-call 链，direct 版为 []
              "error": str | None,
            }
        """

class HypothesisBase(ABC):
    @abstractmethod
    def generate(self, service_name: str, context: dict) -> list[dict]: ...

class LearningBase(ABC):
    @abstractmethod
    def analyze_coverage(self) -> dict: ...
    @abstractmethod
    def suggest_next(self) -> list[dict]: ...

# ... 每个 Agent 类型一个 Base
```

统一约定：
- 所有实现返回 dict 必须含 `engine` / `latency_ms` / `trace` 字段
- `trace` 可以是空 list（direct 版）或 tool-call list（strands 版）
- 错误不抛异常，封装在 `error` 字段（便于 factory 回退）

---

## 7. 测试与度量

### 7.1 Shadow 对跑测试

```python
# tests/test_agent_shadow.py
@pytest.mark.parametrize("question", GOLDEN_QUESTIONS)
def test_nlquery_shadow(question):
    os.environ["NLQUERY_ENGINE"] = "direct"
    direct_result = make_engine("nlquery").query(question)

    os.environ["NLQUERY_ENGINE"] = "strands"
    strands_result = make_engine("nlquery").query(question)

    # 结果行数差异不超过 20%
    assert abs(len(direct_result["results"]) - len(strands_result["results"])) \
        <= 0.2 * max(len(direct_result["results"]), 1)

    # Strands 延迟不超过 direct 的 2x
    assert strands_result["latency_ms"] <= 2 * direct_result["latency_ms"]
```

### 7.2 CI 矩阵

每次 PR 跑 3 组：
```yaml
strategy:
  matrix:
    engine: [direct, strands, shadow]
```

- `direct` — 现版回归保护
- `strands` — 新版功能验证
- `shadow` — 对跑差异报告（失败不 block，但输出到 PR comment）

### 7.3 度量指标

每个 Agent 都要记录：

| 指标 | direct baseline | strands 门槛 |
|------|-----------------|--------------|
| exact_match（可对比场景） | 实测 | ≥ baseline - 5% |
| p50 latency | 实测 | ≤ 2x baseline |
| p99 latency | 实测 | ≤ 2.5x baseline |
| 单次 token 消耗 | 实测 | ≤ 3.5x baseline |
| 错误率 | 实测 | ≤ baseline |
| tool-call 平均次数 | 0 | ≥ 2（ReAct 是否起作用） |

---

## 8. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| Global inference profile 单点依赖 | 🔴 高 | 季度性探活 + 准备 region profile 白名单 |
| Token 成本 3x | 🟡 中 | schema 按需读取 tool + system prompt 瘦身 |
| Strands SDK 1.x → 2.x breaking change | 🟡 中 | pin 版本 + 每次升级过完整 golden set |
| 长期分支 merge conflict | 🟡 中 | 每周 rebase main，小 PR 拆分 |
| ReAct 多轮超时 | 🟡 中 | 每个 tool 设 timeout，Agent 总超时 30s |
| guard 被 LLM 绕过 | 🔴 高 | 所有 write-capable tool 内部必须先调 `query_guard.is_safe()`；code review 硬要求 |
| profile 切换进程内串 | 🟢 低 | Streamlit 每请求读 env；multi-tenant 用 session state 隔离 |
| OTel trace 成本 | 🟢 低 | 采样率默认 10%，golden CI 强制 100% |
| Opus 无预算防护（Wave 5） | 🔴 高 | 复杂查询连续命中 → Bedrock 账单爆炸。补 token bucket / CloudWatch 预算告警 |
| Wave 4 retry 在 Strands 下冗余 | 🟢 低 | Strands ReAct 原生支持空结果重试，避免两层 retry 叠加。Strands 版禁用 `_should_retry_on_empty` |
| `_DEFAULT_PROFILE` 单例 | 🟡 中 | 多租户进程切 profile 需重启；L2 前补 session-level profile injection |

---

## 9. 决策清单

以下问题必须在进入阶段 2 前答案明确：

1. [ ] L1 Smart Query POC 的 golden 指标是否全部达标？（baseline 20/20，Strands 门槛 ≥ 19/20）
2. [x] ✅ P1' profile 配置化是否完成？（Wave 1 已合并）
3. [x] ✅ Golden CI 是否已就位？（Wave 3 已合并，baseline 100%）
4. [ ] Opus 预算防护是否就位？（Wave 5 引入 Opus 升级，目前无 budget cap）
5. [ ] 团队是否至少 2 人能接手 Strands 开发（避免单点依赖）？
6. [ ] 运维是否接受"1-2 个月迁移期间部署流程分叉"？
7. [ ] 月度 Bedrock 账单预算是否能承受 ~3x 增幅（Strands ReAct 多轮 + Opus 升级叠加）？
8. [ ] CloudWatch / X-Ray OTel retention 策略是否敲定？
9. [ ] 是否存在硬性客户承诺（SLA）要求迁移期间必须零回归？

全部 YES → 进入阶段 2。任一 NO → 原地打磨 L1，不启动全平台迁移。

---

## 10. 参考实现骨架

### 阶段 1 Smart Query（L1 POC）文件清单

```
rca/
├── engines/
│   ├── __init__.py
│   ├── base.py                # NLQueryBase 接口（含 retried/engine/model_used/trace/...）
│   └── factory.py             # make_nlquery_engine()
├── neptune/
│   ├── nl_query.py            # ⚠️ 保留文件名不动，内部改为轻量 re-export（向后兼容
│   │                          #    `from neptune.nl_query import NLQueryEngine`）
│   ├── nl_query_direct.py     # 现 NLQueryEngine 主体搬过来，改名 DirectBedrockNLQuery
│   └── nl_query_strands.py    # 新增：StrandsNLQueryEngine
└── tests/
    ├── test_golden_accuracy.py  # ⚠️ 改造为支持 matrix: [direct, strands]，复用同一套 cases
    └── test_nlquery_shadow.py   # 新增：direct vs strands diff 报告（非 block）

demo/pages/
└── 2_Smart_Query.py           # 改 1 行：NLQueryEngine() → make_nlquery_engine()

pyproject.toml                 # 加 extras_require.strands
```

**关键约束**：
- `from neptune.nl_query import NLQueryEngine` 必须保持可用，避免破坏 demo / notebook 里的使用
- Wave 4 的 `_NEGATION_HINTS` 否定查询白名单、Wave 5 的 `_select_model` / `complex_keywords` 逻辑 *必须* 在 Strands 版中有对应实现（可以是 tool + prompt 提示，也可以是 Agent 外包 wrapper）
- Strands 版在 ReAct 多轮中 token 成本天然更高，**Opus 升级策略必须叠加预算上限**（见第 8 节风险表新增行）

### 阶段 2-3 全平台文件清单

```
rca/engines/base.py            # + HypothesisBase / LearningBase / ProberBase / ...
rca/agents/
  ├── hypothesis_base.py       # 抽共享逻辑
  ├── hypothesis_direct.py
  ├── hypothesis_strands.py
  ├── learning_direct.py
  └── learning_strands.py
rca/probers/
  ├── layer2_direct.py
  └── layer2_strands.py        # Strands multi-agent orchestrator
chaos/code/runner/
  ├── runner_direct.py
  └── runner_strands.py
chaos/code/policy/
  ├── guard_direct.py
  └── guard_strands.py
```

---

## 11. 回退策略

### 阶段 1 回退
- `unset NLQUERY_ENGINE`（或 `=direct`）即回退，代码不动

### 阶段 2-3 回退
- feature 分支在 `strands-migration` 内独立存在，出问题单分支 revert

### 阶段 4 回退
- 1 个月 env 开关保留期：`export AGENT_FRAMEWORK=direct` 即回退
- 紧急情况：`git checkout v-last-direct-final-YYYYMMDD` + 重部署

### 阶段 5 回退（direct 已删除）
- Git 历史找 tag，cherry-pick direct 实现回 main
- 预计 2-4 人日，不建议作为常规路径
- 真到这一步说明 Strands 有严重问题，同时应该启动 langgraph / bedrock-agents 的备选评估

---

## 12. 后续文档

迁移执行过程中需持续维护：

- `experiments/strands-poc/report.md` — L0 结果（已完成）
- `experiments/strands-poc/l1-smart-query-report.md` — L1 POC 报告（待写）
- `docs/migration/decisions.md` — 每个模块迁移的 ADR 记录
- `docs/migration/strands-migration-history.md` — 最终回顾 + 经验总结（cutover 后补）
- `docs/agent-framework.md` — Strands 架构说明 + 新 Agent 开发指南（阶段 5 后）

---

## 13. 总结

**最短路径**：L0（已完成）→ L1 Smart Query（方案 A factory）→ Branch 隔离全平台（方案 2）→ cutover → 清理（方案 4 的终态）。

**核心原则**：
1. 先验证再迁移，不拿生产试错
2. 迁移期保留回滚能力，cutover 后保留 1 个月 safety net
3. P1' profile 配置化必须独立完成，和 Strands 迁移不耦合
4. guard/安全校验在任何 Agent 框架下都是硬约束，绝不退让
5. 双实现是过渡手段不是终态，1 个月内收敛

**不要做的**：
- 不要方案 1 做长期双轨（6 个月后维护灾难）
- 不要方案 4 直接 cutover（客户 PA 视角绝对反对）
- 不要在 Smart Query 单点没验证通过时就开始全平台改造
- 不要把 profile 化和 Strands 迁移捆绑（耦合会让两件事都做不完）

———

_本文档是 Draft，执行前需要编程猫、架构审阅猫、大乖乖三方确认。_
