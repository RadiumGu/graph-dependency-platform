# TASK: Smart Query L1 POC - Strands Agents 迁移第一步

> **任务归属**: 编程猫
> **发起**: 大乖乖 + 架构审阅猫
> **日期**: 2026-04-18
> **范围**: Phase 2 Smart Query L1 POC(严格限定,不扩展)
> **预计工作量**: 3-5 人日
> **成功标志**: 6 个 PR 合并 + 验证步骤 4 全部达标 + Streamlit 上 `NLQUERY_ENGINE=strands|direct` 切换可见效果

---

## 0. 上下文背景

- AWS Strands Agents 的 L0 Spike 已完成(`experiments/strands-poc/report.md`),6 个硬约束全部 YES,验证了 `global.anthropic.claude-sonnet-4-6` + `ap-northeast-1` 可用、Strands `@tool` 能调 Neptune、`query_guard` 在 tool 内部可硬拦截 LLM。
- Smart Query Wave 1-5 已全部合并到 main(profile 配置化 + Golden CI + 空结果重试 + Opus 升级),当前 baseline 20/20 = 100%。
- 全平台迁移策略见 `experiments/strands-poc/migration-strategy.md`(Strangler Fig 单向迁移)。本任务只做 Phase 1 地基 + Phase 2 Smart Query。

---

## 1. 工作目录

```
/home/ubuntu/tech/graph-dependency-platform
```

---

## 2. 必读(按顺序)

1. `experiments/strands-poc/README.md` - 目录向导
2. `experiments/strands-poc/report.md` - L0 Spike 验证结果(6 硬约束全通过)
3. `experiments/strands-poc/migration-strategy.md` - 迁移策略(重点读第 3 节 Phase 1 + Phase 2)
4. `experiments/strands-poc/spike.py` - POC 参考实现
5. `rca/neptune/nl_query.py` - 现版 Smart Query 引擎(Wave 1-5)
6. `rca/neptune/schema_prompt.py` - profile 化后的 prompt 构建
7. `rca/neptune/query_guard.py` - 安全校验(不得绕过)
8. `profiles/profile_loader.py` - EnvironmentProfile 加载
9. `profiles/petsite.yaml` - 当前 profile 配置
10. `tests/test_golden_accuracy.py` + `tests/golden/petsite.yaml` - 现有 Golden CI(20 cases, baseline 20/20)

---

## 3. 任务范围(严格限定)

只做 *Phase 1 地基* + *Phase 2 Smart Query L1 POC*。

**不动**其他 Agent(HypothesisAgent / LearningAgent / RCA Layer2 Probers / ChaosRunner / PolicyGuard / DR Executor)。

---

## 4. 产出清单

### 4.1 Phase 1 地基(必须先做)

```
rca/engines/
├── __init__.py
├── base.py              # 只实现 NLQueryBase;其他 Base 留 TODO 注释占位
├── factory.py           # make_nlquery_engine(profile=None)
└── strands_common.py    # BedrockModel 构造 + tool helpers;OTel 接入留 TODO
```

### 4.2 Phase 2 Smart Query 实现

```
rca/neptune/
├── nl_query.py              # 改为薄 re-export(向后兼容)
├── nl_query_direct.py       # 原 NLQueryEngine 主体,改名 DirectBedrockNLQuery
├── nl_query_strands.py      # 新增 StrandsNLQueryEngine
└── strands_tools.py         # 3 个 @tool
```

**具体要求**:
- `nl_query_direct.py` - 把 `nl_query.py` 现有 `NLQueryEngine` 主体搬过来,类改名为 `DirectBedrockNLQuery`,继承 `NLQueryBase`
- `nl_query.py` - 改为薄 re-export:
  ```python
  # 向后兼容 import:from neptune.nl_query import NLQueryEngine
  from neptune.nl_query_direct import DirectBedrockNLQuery as NLQueryEngine  # noqa: F401
  ```
- `nl_query_strands.py` - `StrandsNLQueryEngine`,继承 `NLQueryBase`
- `strands_tools.py` - 3 个 `@tool`:
  - `get_schema_section(section: str = "all") -> str`
  - `validate_cypher(cypher: str) -> str`
  - `execute_cypher(cypher: str) -> str`
  - ⚠️ `execute_cypher` 内部必须先调 `query_guard.is_safe()`;不安全直接返回错误字符串,**不执行 Cypher**

### 4.3 Streamlit 入口

```
demo/pages/2_Smart_Query.py    # 改 1 行:NLQueryEngine() → make_nlquery_engine()
```

### 4.5 迁移文档框架

```
docs/migration/
└── timeline.md              # 迁移时间线(所有 7 个模块均填入)
scripts/
└── check_migration_deadlines.py  # 空壳文件,作 Phase 3 预留
```

要求:
- `docs/migration/timeline.md` 必须涵盖 `migration-strategy.md` 第 3.3 节列7 个模块,具体结构见第 11 节 PR 6
- `scripts/check_migration_deadlines.py` 框架代码可直接拷自 `migration-strategy.md` 第 5.2 节,本 Phase **不接入 CI**,不执行删除检查逻辑会因 freeze_date/delete_date 为空而跳过,留作 Phase 3 启动时激活

### 4.6 测试

```
tests/
├── test_golden_accuracy.py    # 改造为 engine matrix
├── test_nlquery_shadow.py     # 新增:direct vs strands 对比
└── golden/
    ├── BASELINE-direct.md     # 现有 BASELINE.md 重命名
    └── BASELINE-strands.md    # 新生成
```

**具体要求**:
- `test_golden_accuracy.py` 改造:
  ```python
  @pytest.mark.parametrize("engine_name", ["direct", "strands"])
  def test_golden_case(engine_name, case, results_accumulator): ...
  ```
  engine fixture 根据 engine_name 调 `make_nlquery_engine()`;BASELINE.md 按 engine 分别写入 `BASELINE-direct.md` / `BASELINE-strands.md`
- `test_nlquery_shadow.py` 新增:同一问题两 engine 对比,输出 cypher diff + 结果行数 diff + 延迟比;*不 block PR*,打印到 stdout

---

## 5. NLQueryBase 接口规范(严格遵循)

```python
# rca/engines/base.py
from abc import ABC, abstractmethod
from typing import Any

class NLQueryBase(ABC):
    def __init__(self, profile: Any = None): ...

    @abstractmethod
    def query(self, question: str) -> dict:
        """
        返回 dict 必须包含以下所有字段。

        现版 NLQueryEngine 已有:question / cypher / results / summary / retried

        本次新增元数据字段:engine / model_used / latency_ms / token_usage / trace

        {
            "question": str,
            "cypher": str,
            "results": list,
            "summary": str,
            "retried": bool,              # Wave 4 字段;direct 按实际填,strands 固定 False
            # --- 本次新增元数据 ---
            "engine": str,                # "direct" | "strands"
            "model_used": str | None,     # 实际用的 model id(Wave 5 Sonnet/Opus)
            "latency_ms": int,            # 端到端耗时
            "token_usage": dict | None,   # {"input": int, "output": int, "total": int};拿不到填 None
            "trace": list[dict],          # direct 固定 [];strands 填 tool-call 链
            "error": str | None,
        }
        """
```

---

## 6. 硬约束(不得违反)

1. ⚠️ **不改 `rca/neptune/query_guard.py`** - 两个 engine 共享同一套 guard
2. ⚠️ **不改 `profiles/` 下任何文件** - Wave 1 的 profile 化必须保持原样
3. ⚠️ **不改 `rca/neptune/schema_prompt.py`** - `build_system_prompt()` 两版都复用
4. ⚠️ **Strands engine 的 `execute_cypher` tool 内部必须先 `query_guard.is_safe()`** - 不安全的 cypher 直接返回错误字符串,**绝不**让 Agent 有机会绕过
5. ⚠️ **Strands engine 用** `BedrockModel(model_id="global.anthropic.claude-sonnet-4-6", region_name="ap-northeast-1")` - 不支持裸模型 id,不支持 `apac.*` / `us.*` profile(L0 Spike 已验证)
6. ⚠️ **Strands engine 禁用 Wave 4 的 `_should_retry_on_empty` 逻辑** - 依赖 Strands 原生 ReAct 多轮,避免双层重试
7. ⚠️ **Wave 5 的 Opus 升级逻辑必须在 Strands 版有等价实现** - 通过 system_prompt 提示或 model 选择器,命中 `profile.complex_keywords` 时用 Opus
8. ⚠️ **Streamlit 入口改 1 行就够** - 不要动页面其他逻辑
9. ⚠️ **向后兼容** - `from neptune.nl_query import NLQueryEngine` 必须仍可 import,现有使用者零改动
10. ⚠️ **不污染主环境** - Strands 依赖装法见第 10 节

---

## 7. 切换方式(env 开关)

```bash
export NLQUERY_ENGINE=direct     # 默认
export NLQUERY_ENGINE=strands    # 启用 Strands
```

**factory 实现要求**:
- 默认 `direct`
- 请求 `strands` 但依赖未装 → `logger.warning` + 回退 `direct`(不崩)
- 不使用全局 `AGENT_FRAMEWORK`(那是 Phase 3+ 的事,本 Phase 只认 `NLQUERY_ENGINE`)

factory 参考骨架:
```python
# rca/engines/factory.py
import logging
import os
from typing import Any

from engines.base import NLQueryBase

logger = logging.getLogger(__name__)


def make_nlquery_engine(profile: Any = None) -> NLQueryBase:
    engine = os.environ.get("NLQUERY_ENGINE", "direct").lower()
    if engine == "strands":
        try:
            from neptune.nl_query_strands import StrandsNLQueryEngine
            return StrandsNLQueryEngine(profile=profile)
        except ImportError as e:
            logger.warning(
                "Strands engine 不可用(%s),回退 direct。"
                "安装:pip install strands-agents strands-agents-tools", e,
            )
    from neptune.nl_query_direct import DirectBedrockNLQuery
    return DirectBedrockNLQuery(profile=profile)
```

---

## 8. 验证步骤(按顺序执行,逐项确认)

### 8.1 单元测试(不含 RUN_GOLDEN)
```bash
cd rca && PYTHONPATH=.:.. pytest ../tests/ -v
```

### 8.2 Streamlit 冒烟:direct 版
```bash
NLQUERY_ENGINE=direct streamlit run demo/pages/2_Smart_Query.py
```
自测以下 3 条问题(来自 `experiments/strands-poc/golden_questions.yaml`):
- `petsite 依赖哪些数据库?`
- `Tier0 服务有哪些?`
- `petsite 的完整上下游依赖路径`

截图 + log 贴自己的规划文件(不进 `experiments/strands-poc/`,避免目录膨胀)。

### 8.3 Streamlit 冒烟:strands 版
```bash
NLQUERY_ENGINE=strands streamlit run demo/pages/2_Smart_Query.py
```
自测同 3 条问题,截图 + log 同样贴自己规划文件。

### 8.4 Golden CI(真调 Bedrock + Neptune,有成本)
```bash
cd rca && PYTHONPATH=.:.. RUN_GOLDEN=1 NLQUERY_ENGINE=direct  pytest ../tests/test_golden_accuracy.py -v
cd rca && PYTHONPATH=.:.. RUN_GOLDEN=1 NLQUERY_ENGINE=strands pytest ../tests/test_golden_accuracy.py -v
```
- direct 必须 ≥ 19/20
- strands 必须 ≥ 19/20(允许 1 case 回归)

### 8.5 Shadow 对比
```bash
cd rca && PYTHONPATH=.:.. RUN_GOLDEN=1 pytest ../tests/test_nlquery_shadow.py -v -s
```
观察 p50 / p99 延迟倍数和差异 case。

---

## 9. 验收门槛(任一不达标 → 不 merge)

| 指标 | 门槛 |
|------|------|
| direct golden | ≥ 19/20 |
| strands golden | ≥ 19/20 |
| strands p50 延迟 | ≤ 2x direct |
| strands p99 延迟 | ≤ 2.5x direct |
| AccessesData vs DependsOn 关系识别 | **q001 和 q006 的 `cypher_must_not_contain: [DependsOn]` 断言必须全部通过**(不新增 must_not 条目;golden 扩充是独立工作项) |
| 现有调用方(Streamlit / notebook) | 零改动能跑 |

---

## 10. 依赖管理

**结论**：允许在 `streamlit-demo.service` 所用主环境（`/usr/bin/python3`）安装 Strands 依赖，但需按以下 SOP 操作。

**背景**：8.3 冒烟 + 第 14 节完成标志 #3 要求线上 Streamlit 能切 `NLQUERY_ENGINE=strands`，若主环境不装，factory 会 `ImportError → fallback direct`，POC 实际没验证到 Strands。所以主环境必须装。

### 10.1 安装前备份

```bash
/usr/bin/pip3 freeze > /home/ubuntu/backups/pip-freeze-before-strands-$(date +%Y%m%d).txt
```

### 10.2 主环境安装

```bash
/usr/bin/pip3 install 'strands-agents>=1.36' 'strands-agents-tools>=0.5'
```

### 10.3 安装后验证主环境未损

```bash
# streamlit-demo.service 重启
 sudo systemctl restart streamlit-demo.service
 sudo systemctl status streamlit-demo.service --no-pager

# 检查 demo 其他页面也能访问（验证 strands 的 pydantic/boto3 等依赖链没有与现有包版本冲突）
 curl -sI https://rainmeadows.com/streamlit/Smart_Query
# 逐个点过 sidebar 里其他页面（Graph / RCA / Chaos 等）确认能加载
```

若发现依赖冲突 → 立即回滚：以备份的 pip-freeze 文件 `pip install -r` 恢复现状，通知大乖乖。

### 10.4 仓库内标注

更新 `demo/requirements.txt`（非注释式）：
```
# Strands Agents — 已在 streamlit-demo.service 节点主环境安装。
# 启用：export NLQUERY_ENGINE=strands
strands-agents>=1.36
strands-agents-tools>=0.5
```

### 10.5 factory 回退逻辑保留

开发者本地可能没在 venv 装 strands，factory 的「`ImportError` → 回退 direct 」逻辑必须保留（第 7 节骨架代码已写）。

### 10.6 禁止项

- ❌ 不写入任何顶层 `pyproject.toml`（repo 根现在没有，别创建）
- ❌ 不写入 `dr-plan-generator/requirements.txt`（不相关）
- ❌ 不把 strands 装进 `/usr/lib/python3/dist-packages`（包管理器区域）

---

## 11. Git 提交策略

拆成 *6 个小 PR*(每个 PR 单一职责,各自 review):

| # | PR 标题 | 范围 |
|---|---------|------|
| 1 | `feat(engines): add NLQueryBase + factory skeleton` | Phase 1 地基 |
| 2 | `refactor(smart-query): rename NLQueryEngine → DirectBedrockNLQuery, add re-export shim` | 只重构,无行为变化 |
| 3 | `feat(smart-query): add StrandsNLQueryEngine + strands_tools` | 新增 Strands 实现 |
| 4 | `test(smart-query): enable engine matrix in golden + add shadow test` | 测试改造 |
| 5 | `feat(smart-query): wire factory into Streamlit entry` | 1 行改动 + 冒烟验证 |
| 6 | `docs(migration): add docs/migration/timeline.md for all planned modules` | 建立迁移时间线(含所有 7 个模块占位) |

**每个 PR 单独 review**,禁止堆一起。

**PR 6 的 timeline.md 必须包含全部 7 个计划迁移的模块**(按 migration-strategy.md 第 3.3 节 Phase 3 排序):

```yaml
# docs/migration/timeline.md
# Strands Agents 迁移时间线
#
# 维护规则:
#   - 本文件为机器可读 YAML + 人类可读 Markdown
#   - scripts/check_migration_deadlines.py 会读取此文件进行 CI 检查
#   - freeze_date / delete_date 一旦写入,修改需提交书面 ADR
#   - status 取值:planned | active | frozen | deleted
#
# 迁移策略见 experiments/strands-poc/migration-strategy.md

modules:
  - name: smart-query
    direct_file: rca/neptune/nl_query_direct.py
    freeze_date: ~         # L1 POC 达标后由大乖乖填(Phase 2 Week 3)
    delete_date: ~         # 冻结日 + 4 个月
    owner: "@programming-cat"
    status: "active"
    notes: "Phase 2 L1 POC 进行中"

  - name: hypothesis-agent
    direct_file: rca/agents/hypothesis_direct.py
    freeze_date: ~
    delete_date: ~
    owner: "@programming-cat"
    status: "planned"
    notes: "Phase 3 Week 4-6;需先建立 15-20 个假设场景 golden set"

  - name: learning-agent
    direct_file: rca/agents/learning_direct.py
    freeze_date: ~
    delete_date: ~
    owner: "@programming-cat"
    status: "planned"
    notes: "Phase 3 Week 7-9;需 Strands session memory;golden set 10 个 coverage snapshot"

  - name: rca-layer2-probers
    direct_file: rca/probers/layer2_direct.py
    freeze_date: ~
    delete_date: ~
    owner: "@programming-cat"
    status: "planned"
    notes: "Phase 3 Week 10-12;Strands multi-agent 编排主场;golden set 6 个已知故障场景"

  - name: chaos-policy-guard
    direct_file: chaos/code/policy/guard_direct.py
    freeze_date: ~
    delete_date: ~
    owner: "@programming-cat"
    status: "planned"
    notes: "Phase 3 Week 13-15;pre-execution policy_guard【新增能力】;10 个规则场景"

  - name: chaos-runner
    direct_file: chaos/code/runner/runner_direct.py
    freeze_date: ~
    delete_date: ~
    owner: "@programming-cat"
    status: "planned"
    notes: "Phase 3 Week 16-18;真正 mutate K8s/FIS 状态,风险最高;3 个 dry-run 实验"

  - name: dr-executor
    direct_file: dr-plan-generator/executor_direct.py
    freeze_date: ~
    delete_date: ~
    owner: "@programming-cat"
    status: "planned"
    notes: "Phase 3 Week 19-20;跨 region、影响生产,最后迁;2 个完整演练"

tags:
  last_direct_snapshot: ""      # Phase 2 开始前由大乖乖打 v-last-direct-YYYYMMDD
  strands_only: ""              # Phase 4 结束时打 v-strands-only-YYYYMMDD

phase_gates:
  gate_a_phase0_to_1: "not_started"   # 前置条件 + RFC + 预算
  gate_b_phase2_to_3: "not_started"   # Smart Query 门槛达标
  gate_c_phase3_to_4: "not_started"   # 所有模块稳定 ≥ 4 周
  gate_d_phase4_to_5: "not_started"   # direct 已全删
```

**要求**:
- 所有 7 个模块均须写入,包含 `smart-query` 当前状态为 `active`,其他 6 个为 `planned`
- `direct_file` 路径按 `migration-strategy.md` 第 10 节约定,即使文件尚未存在也先填(服务于代码位置规范化)
- `freeze_date` / `delete_date` 留空(`~`),由大乖乖在 L1 POC 达标后填入
- 顺带创建 `scripts/check_migration_deadlines.py` 的空壳文件(内容可参考 `migration-strategy.md` 第 5.2 节),但本 Phase **不必接入 CI**,留作 Phase 3 启动时激活

---

## 12. 不要做的事

1. ❌ 不启动全平台迁移(Phase 3 是后续任务)
2. ❌ 不把其他 Agent(Hypothesis / Learning / Prober / Chaos / DR)加进来
3. ❌ 不改 profile YAML 内容
4. ❌ 不改 `query_guard.py`
5. ❌ 不给 direct 版加新功能(Wave 5 之后直接冻结)
6. ❌ 不建 `docs/migration/timeline.md` 之外的新文档结构(Phase 1 只需 timeline.md + check_migration_deadlines.py 空壳)
7. ❌ 不把 strands 装进 `/usr/lib/python3/dist-packages`（包管理器区域）；用 `/usr/bin/pip3 install`进 `/usr/local/lib/...` 或系统默认位置（见第 10 节 SOP）
8. ❌ 不改 `demo/pages/2_Smart_Query.py` 页面其他逻辑
9. ❌ 不为了达到准确率硬编码 workaround;如果 strands 实现不到 19/20,写清楚原因找大乖乖

---

## 13. 失败处理

- **同一错误 3 次失败**:停手,写清楚到 progress 日志,通知大乖乖
- **遇到和 L0 Spike 相同的错误**(如 `ValidationException`):先 diff `experiments/strands-poc/spike.py` 看差别,那个 spike 已经验证通过
- **Golden CI 成本**:一次 $5-10,尽量本地 `pytest` 先过了再跑 `RUN_GOLDEN=1`
- **Neptune 连接问题**:检查 `NEPTUNE_ENDPOINT` 环境变量,L0 Spike 用的是 `petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com`

---

## 14. 完成标志

1. ✅ 6 个 PR 全部合并到 main
2. ✅ 验证步骤 8.1 - 8.5 全部达标
3. ✅ Streamlit 上 `NLQUERY_ENGINE=strands|direct` 切换可见效果(cypher 生成、延迟、结果)
4. ✅ `tests/golden/BASELINE-direct.md` + `BASELINE-strands.md` 两份基线都写入仓库
5. ✅ 更新 `experiments/strands-poc/report.md` 追加"L1 POC 完成"一节,含:
   - 最终 golden 对比表(两 engine 各自 pass 率)
   - 延迟数据(p50 / p99 / 倍数)
   - token 消耗对比(如拿到)
   - Wave 4 retry / Wave 5 Opus 升级在 Strands 版的等价实现说明
   - 遇到的陷阱 / 后续 Phase 3 启动前要补的工作

---

## 15. 参考文件速查

### 现版代码(必读)
- `rca/neptune/nl_query.py` - 142 行,含 Wave 4 retry + Wave 5 Opus 升级
- `rca/neptune/query_guard.py` - 54 行,安全校验
- `rca/neptune/schema_prompt.py` - 95 行,profile 化 prompt 构建
- `profiles/profile_loader.py` - EnvironmentProfile 加载
- `profiles/petsite.yaml` - 413 行 profile 配置
- `tests/test_golden_accuracy.py` - Golden CI 框架
- `tests/golden/petsite.yaml` - 20 cases

### L0 Spike 代码(参考)
- `experiments/strands-poc/spike.py` - ~200 行 POC,含 3 个 @tool + BedrockModel
- `experiments/strands-poc/baseline.py` - direct 版对跑基准

### 策略文档
- `experiments/strands-poc/migration-strategy.md` - 重点读:
  - 第 2 节前置条件
  - 第 3.1 节 Phase 1 地基
  - 第 3.2 节 Phase 2 Smart Query
  - 第 5 节接口规范
  - 第 6 节度量与监控

---

**遇到任何不确定的地方,先在 Slack 里问大乖乖或架构审阅猫,不要自己拍脑袋决定。**
