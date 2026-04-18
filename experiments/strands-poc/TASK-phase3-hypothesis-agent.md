# TASK: Phase 3 Module 1 — HypothesisAgent 迁移到 Strands Agents

> **任务归属**: 编程猫
> **发起**: 大乖乖 + 架构审阅猫
> **日期**: 2026-04-XX（Gate A 通过后启动）
> **范围**: 把 `chaos/code/agents/hypothesis_agent.py` 从直调 Bedrock 迁到 Strands Agents
> **预计工作量**: 3 周（migration-strategy § 3.3 Phase 3 Week 4-6 标准流程）
> **成功标志**: 3 个切换阶段 PR 全部合入 main + 所有验收门槛达标 + `hypothesis_direct.py` 冻结

---

## 0. 必读顺序

1. `migration-strategy.md`（§ 3.3 Phase 3 + § 6.3 切换门槛 + § 6.4 Prompt Caching）
2. `TASK-phase3-shared-notes.md`（共享规范，§ 3.1 HypothesisAgent 的缓存设计要点）
3. `TASK-L1-smart-query.md`（模板参考）
4. `TASK-L2-prompt-caching.md`（Prompt Caching 实现参考）
5. `report.md`（L0/L1/L2 经验总结）
6. **当前模块代码**：
   - `chaos/code/agents/hypothesis_agent.py` — 517 行，现版实现
   - `chaos/code/agents/models.py` — `Hypothesis` 数据模型
   - `chaos/code/runner/neptune_client.py` — Gremlin 查询封装
   - `chaos/code/runner/config.py` — `BEDROCK_REGION` / `BEDROCK_MODEL`

---

## 1. 工作目录

```
/home/ubuntu/tech/graph-dependency-platform
```

---

## 2. 前置条件（Gate A 确认）

启动本任务前必须确认：

- [ ] Smart Query L2 稳定期 ≥ 4 周，baseline 未退化
- [ ] Smart Query L2 的 Prompt Caching 实测命中率 ≥ 50%
- [ ] `docs/migration/timeline.md` 中 `hypothesis-agent` 的 `freeze_date` / `delete_date` 已填
- [ ] 大乖乖已打 tag `v-last-direct-YYYYMMDD`
- [ ] Bedrock 月度预算足够承受 +30-50% 临时增幅

*任一不满足 → 暂停启动，通知大乖乖。*

---

## 3. 任务范围（严格限定）

**做**：
- `chaos/code/agents/hypothesis_agent.py` 从"直调 Bedrock"改造为"Strands Agents"
- 引入 `HypothesisBase` 抽象基类 + factory
- 建立 HypothesisAgent 的 Golden Set（15-20 个假设场景）
- Prompt Caching 默认启用（system prompt + tool schemas）
- `HypothesisBase.generate()` 返回 dict 规范化（含 `engine` / `model_used` / `latency_ms` / `token_usage{input,output,cache_read,cache_write}` / `trace`）

**不做**：
- 不改 `models.py`（Hypothesis 数据结构保持不变）
- 不改 `neptune_client.py`（复用现有 Gremlin 查询）
- 不改 `chaos/code/runner/` 其他模块
- 不加 LearningAgent / Prober 等下游模块
- 不做"新增能力"（本任务只是 framework 迁移，业务逻辑等价）

---

## 4. 产出清单

### 4.1 抽象层（engines 包扩展）

修改 `rca/engines/base.py`（已存在，仅扩展）：

```python
class HypothesisBase(ABC):
    """Chaos 假设生成引擎基类。"""
    
    def __init__(self, profile: Any = None): ...
    
    @abstractmethod
    def generate(
        self,
        topology: list[dict] = None,
        max_hypotheses: int = 10,
        service: str | None = None,
    ) -> dict:
        """
        Returns:
            {
                "hypotheses": list[Hypothesis],   # 业务产出
                "prioritized": list[Hypothesis],  # 带排序的版本（可选）
                "engine": str,                    # "direct" | "strands"
                "model_used": str | None,
                "latency_ms": int,
                "token_usage": {
                    "input": int, "output": int, "total": int,
                    "cache_read": int, "cache_write": int,
                } | None,
                "trace": list[dict],              # Strands tool-call chain；direct 为 []
                "error": str | None,
            }
        """
```

修改 `rca/engines/factory.py`：添加 `make_hypothesis_engine(profile=None)`，读 env `HYPOTHESIS_ENGINE=direct|strands`。

### 4.2 具体实现

```
chaos/code/agents/
├── hypothesis_agent.py          # 向后兼容 shim (re-export DirectBedrockHypothesis as HypothesisAgent)
├── hypothesis_direct.py         # 原 HypothesisAgent 主体改名 DirectBedrockHypothesis，继承 HypothesisBase
├── hypothesis_strands.py        # 新增 StrandsHypothesisAgent，继承 HypothesisBase
└── hypothesis_tools.py          # Strands @tool 定义
```

### 4.3 Tool 定义（`hypothesis_tools.py`）

```python
from strands import tool
from chaos.code.runner import neptune_client as nc

@tool
def query_topology(service_filter: str = "") -> str:
    """查询 Neptune 图谱中的服务拓扑。可选 service 过滤。"""
    ...

@tool
def query_recent_incidents(limit: int = 20) -> str:
    """查询最近的故障事件（用于假设生成时的上下文）。"""
    ...

@tool
def query_fault_history(service: str = "") -> str:
    """查询某服务的历史混沌实验结果。"""
    ...

@tool
def query_infra_snapshot(services: list[str]) -> str:
    """查询指定服务的基础设施快照（K8s / RDS / Lambda 等）。"""
    ...
```

### 4.4 Strands engine 关键要求

- **BedrockModel**：`build_bedrock_model(cache_prompt="default", cache_tools="default")` — 复用 `rca/engines/strands_common.py`
- **system prompt**：`build_hypothesis_system_prompt(profile)` — 从 profile YAML 读取图 schema + 故障目录 + 假设模式，稳定前缀 5-8k tokens
- **禁用事项**：直调版的 retry / manual prompt 拼接逻辑不搬过来（交给 ReAct 多轮）

### 4.5 Golden Set

新增 `tests/golden/hypothesis/`：

```
tests/golden/hypothesis/
├── scenarios.yaml               # 15-20 个场景输入（不包含 expected 结果）
├── cases.yaml                   # 基于 direct 采样生成的行为约束式 golden
├── BASELINE-direct.md           # 每次 RUN_GOLDEN 时生成
└── BASELINE-strands.md
```

#### 4.5.1 Golden 的哲学：行为约束而非精确匹配

Hypothesis 生成天然有发散性，同一场景连跑 3 次结果不完全一样也是正常的。**不要硬编码 expected_hypotheses 的精确列表，改用行为约束**：

```yaml
# cases.yaml 单条结构：
- id: h001
  scenario: "petsite 跨 AZ 故障"
  service_filter: "petsite"
  context:
    topology_filter: "az:ap-northeast-1a,1c"
  
  # 数量范围
  min_hypotheses: 2
  max_hypotheses: 10
  
  # 至少命中其中一个 fault_type（direct 采样 3 次后统计出的高频项）
  fault_type_must_include_any:
    - "pod_kill"
    - "network_partition"
    - "pod_failure"
  
  # 绝对不应该生成的 fault_type（领域知识）
  fault_type_must_not_include:
    - "dns_chaos"     # AZ 故障不是 DNS 问题
    - "http_chaos"    # 和 AZ 故障无关
  
  # 至少覆盖一个 target_category
  target_category_must_include_any:
    - "az-isolation"
    - "replica-loss"
  
  # Tier 识别必须正确
  tier_must_equal: "Tier0"
```

#### 4.5.2 Golden 构建步骤（分两阶段，Week 1 完成）

**阶段 1：写 15-20 个场景输入**

从line上真实使用日志 / orchestrator 调用记录 / 团队关心的实验场景中挑高价值场景，先写进 `scenarios.yaml`，只包含输入：

```yaml
- id: h001
  scenario: "petsite 跨 AZ 故障"
  service_filter: "petsite"
  topology_filter: "az:ap-northeast-1a,1c"
```

**阶段 2：用 direct 版采样建 baseline**

写一个一次性脚本 `chaos/code/agents/sample_for_golden.py`：
- 读 `scenarios.yaml`
- 对每个场景调用 `DirectBedrockHypothesis().generate(...)` 连续 3 次
- 统计每个场景的：
  - 单次 hypothesis 数量的 min/max
  - fault_type 出现频率 ≥ 2/3 的称为"高频"，进 `fault_type_must_include_any`
  - 没出现的 fault_type — 由**人工审查**判断是否为"家庭因素不应该出现"，是则进 `fault_type_must_not_include`；否则不列
  - tier 均值 → `tier_must_equal`（应该是 3 次一致的）
- 输出 `cases.yaml` 草稿，由**你人工 review** 吗决后 commit

**教训（Smart Query q014 事件教训）**：不要凭空造 expected，也不要完全相信 LLM 采样。两者结合，有数据有领域审查。

#### 4.5.3 测试的 3 层验证

`test_hypothesis_golden.py` 验证单个 engine的 cases 约束是否满足。

`test_hypothesis_shadow.py` 拆分为两个视角：

1. **纵向自测**：同一 engine 跑 3 次，hypothesis 数量 + fault_type 分布差异 ≤ 20%（验证 LLM 自我一致性）
2. **横向对比**：direct vs strands 的 fault_type 分布差异 ≤ 20%（验证两 engine 行为对齐）

门槛在 § 8 里以 “Strands 与 direct 行为近似" 为核心，而不是 "Strands 产出 == golden"。

### 4.6 测试文件

- `tests/test_hypothesis_golden.py` — engine matrix（direct / strands）parametrize
- `tests/test_hypothesis_shadow.py` — direct vs strands 对比报告

### 4.7 文档

- `experiments/strands-poc/report.md` 新增 `§ 9 Phase 3 Module 1 — HypothesisAgent` 章节
- `docs/migration/timeline.md` 更新 `hypothesis-agent.status` 从 `planned` → `active` → `frozen`（每阶段一次）
- `docs/migration/decisions/hypothesis-migration-adr.md`（Week 3 切换完成后写 ADR）

---

## 5. HypothesisBase 接口规范

```python
class HypothesisBase(ABC):
    """Chaos 假设生成引擎基类。"""
    
    def __init__(self, profile: Any = None): ...
    
    @abstractmethod
    def generate(
        self,
        topology: list[dict] = None,
        max_hypotheses: int = 10,
        service: str | None = None,
    ) -> dict:
        """
        生成混沌假设列表。
        
        业务兼容：调用方（如 orchestrator.py）原本用 HypothesisAgent().generate()
        直接拿 list[Hypothesis]；迁移后应读 result["hypotheses"]。
        为保持兼容，shim 类额外提供 .generate_list() 方法直接返回 list。
        """
    
    @abstractmethod
    def prioritize(
        self,
        hypotheses: list[Hypothesis],
    ) -> dict:
        """
        对假设排序。返回结构同 generate()，但 "hypotheses" 为空，"prioritized" 有值。
        """
```

---

## 6. 硬约束

1. ⚠️ **业务行为等价** — 调用 `.generate()` 产出的 `hypotheses` 必须与直调版在相同输入下**行为可对齐**（通过 Golden Set 验证）
2. ⚠️ **不改 models.py** — `Hypothesis` 数据结构保持不变，下游消费者零改动
3. ⚠️ **Prompt Caching 默认启用** — system prompt + tool schemas 都要缓存（参考 `TASK-phase3-shared-notes.md § 3.1`）
4. ⚠️ **Global inference profile** — 使用 `global.anthropic.claude-sonnet-4-6` 或 `global.anthropic.claude-opus-4-7`，不支持裸 model id
5. ⚠️ **Neptune Gremlin 保持不变** — 复用 `runner/neptune_client.py`，不重写 Gremlin 查询
6. ⚠️ **`chaos/code/agents/hypothesis_agent.py` 保持可 import** — `from chaos.code.agents.hypothesis_agent import HypothesisAgent` 兼容
7. ⚠️ **Strands engine 禁用任何人工 retry 逻辑** — 依赖 ReAct 原生多轮
8. ⚠️ **Token usage 必填** — `cache_read` / `cache_write` 字段必须有（参考 Smart Query L2 实现）
9. ⚠️ **不动 `chaos/code/runner/`** — Runner 本 Phase 不迁移，只有 HypothesisAgent

---

## 7. 验证步骤

### 7.1 单元测试（本地，不真调）
```bash
cd chaos/code && PYTHONPATH=.:../..  pytest ../../tests/test_hypothesis_golden.py -v -k "not goldenreal"
```

### 7.2 Golden CI（真调 Bedrock + Neptune，有成本）
```bash
cd chaos/code && PYTHONPATH=.:../.. RUN_GOLDEN=1 HYPOTHESIS_ENGINE=direct  pytest ../../tests/test_hypothesis_golden.py -v
cd chaos/code && PYTHONPATH=.:../.. RUN_GOLDEN=1 HYPOTHESIS_ENGINE=strands pytest ../../tests/test_hypothesis_golden.py -v
```

### 7.3 Shadow 对比
```bash
cd chaos/code && PYTHONPATH=.:../.. RUN_GOLDEN=1 pytest ../../tests/test_hypothesis_shadow.py -v -s
```
输出：
- 每 case 的 hypothesis 数量差异（direct vs strands）
- fault_type 分布对比
- 延迟倍数 / token 倍数 / 缓存命中率

### 7.4 缓存生效验证
写 `experiments/strands-poc/verify_cache_hypothesis.py`（参考 L2 的 `verify_cache_direct.py`）：
- 连续 3 次相同输入
- 第 1 次 cache_write > 0
- 第 2/3 次 cache_read > 0

### 7.5 集成验证
跑一次完整 chaos 实验流程（orchestrator → hypothesis → runner），确保下游 runner 能消费新版 `Hypothesis` 对象（应该不变）。

---

## 8. 验收门槛（Gate B - Phase 3 Week 3 检查）

| 指标 | 门槛 |
|------|------|
| Direct Golden | ≥ 18/20 |
| Strands Golden | ≥ 18/20（允许 10% 回归）|
| 业务等价性（行为约束维度） | 所有 case 满足 fault_type_must_include_any / must_not_include / tier_must_equal |
| Shadow 纵向自测（同 engine 3 次）| fault_type 分布差异 ≤ 20% |
| Shadow 横向对比（direct vs strands）| fault_type 分布差异 ≤ 20% |
| Strands p50 latency | ≤ 2x direct（Hypothesis 生成 LLM 重 task，绝对值会高）|
| 稳态缓存命中率（Strands）| ≥ 60% |
| 稳态缓存命中率（Direct）| ≥ 70% |
| Prompt Caching 带来的成本下降 | ≥ 35%（vs 本模块无缓存 baseline）|
| 1 周灰度 SEV-1/2 | 0 |
| 集成测试（orchestrator → runner）| 通过 |

**任一不达标 → 不切默认 / 不冻结**。

---

## 9. Git 提交策略（对齐 L1 POC 6 PR 模板，本次拆成 7 个 PR）

| # | PR | 范围 |
|---|-----|------|
| 1 | `feat(engines): add HypothesisBase + factory` | 抽象层 |
| 2 | `refactor(hypothesis): rename HypothesisAgent → DirectBedrockHypothesis + shim` | rename only，无行为变化 |
| 3 | `feat(hypothesis): add StrandsHypothesisAgent + hypothesis_tools` | 新增 Strands 实现 |
| 4 | `feat(hypothesis): enable Prompt Caching on both engines` | 缓存接入 |
| 5 | `test(hypothesis): golden set + engine matrix + shadow` | 测试 + 20 cases |
| 6 | `feat(hypothesis): wire factory into orchestrator` | 调用方切 factory |
| 7 | `docs(migration): freeze hypothesis_direct.py + ADR` | 切换完成 + 冻结标记 |

每个 PR 单独 review。PR 7 合并时必须同步更新 `docs/migration/timeline.md`（`status: frozen`）。

---

## 10. 3 周流程（参考 migration-strategy § 3.3）

### Week 1
- PR 1 + 2 + 3 + 4 + 5 合并
- 跑 Golden CI 两个 engine 拿到 baseline 数字
- 跑 verify_cache_hypothesis.py 确认缓存生效

### Week 2
- PR 6 合并（orchestrator 切 factory）
- env `HYPOTHESIS_ENGINE=strands` 在灰度节点开启
- 每日 shadow 对比 + 看板监控
- 任何 SEV-1/2 事件立即回滚 env

### Week 3
- Gate B 检查所有门槛
- 通过 → PR 7 合并（冻结），打 tag `v-strands-cutover-hypothesis-YYYYMMDD`
- 未通过 → 延期 1 周并写 ADR 说明原因；同一模块最多延 3 次，否则升级到大乖乖决策

---

## 11. 不要做的事

1. ❌ 不改 `models.py` 的 Hypothesis 数据结构
2. ❌ 不重写 Gremlin 查询（复用 neptune_client.py）
3. ❌ 不把 HypothesisAgent 和 LearningAgent 合并迁移（违反"串行"纪律）
4. ❌ 不跳过 Golden Set 建立（必须 15-20 个 cases）
5. ❌ 不跳过 Prompt Caching（默认启用，不接受"这次先不做"）
6. ❌ 不在 Strands 版保留任何手工 retry 逻辑
7. ❌ 不用 `apac.*` / `us.*` / 裸 model id（L0 Spike 已验证不可用）
8. ❌ 不在冻结期 `DirectBedrockHypothesis` 里加新功能
9. ❌ 不自行决定延期删除日（必须走 ADR）

---

## 12. 失败处理

- **Golden 大量回归** → 检查 agent 规则是否丢了"必须生成完整 fault_type 覆盖"的约束；参考 Smart Query L1 q014 事件处理方式
- **缓存命中率 < 50%** → 检查 system prompt 是否在动态拼接（每次不同导致 miss）；Neptune topology 不应出现在 cacheable 前缀里
- **Strands `BedrockModel` 不支持 cache_tools** → 参考 L2 POC 的 fallback 方案（`additional_request_fields`）
- **同一问题 3 次失败** → 停手，写入 progress log，通知大乖乖

---

## 13. 参考资料

- `rca/neptune/nl_query_strands.py` — Strands 引擎最佳实践（5 区域 system prompt 拆分 / token_usage / trace 抽取）
- `rca/engines/strands_common.py` — BedrockModel + OTel helper
- `tests/test_golden_accuracy.py` — Golden CI 框架，可直接复用
- AWS Strands Agents 文档：<https://strandsagents.com/>
- Bedrock Prompt Caching 文档：<https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html>

---

## 14. 完成标志

1. ✅ 7 个 PR 全部合并 main
2. ✅ 7.1-7.5 验证步骤全部通过
3. ✅ 8 验收门槛全部达标
4. ✅ `BASELINE-direct.md` + `BASELINE-strands.md` 入仓
5. ✅ `docs/migration/timeline.md` 更新 `hypothesis-agent.status: frozen`
6. ✅ tag `v-strands-cutover-hypothesis-YYYYMMDD` 已打
7. ✅ `docs/migration/decisions/hypothesis-migration-adr.md` 落地
8. ✅ `report.md § 9` 写完，含延迟/成本/缓存命中率数据
9. ✅ orchestrator.py 成功调用 `make_hypothesis_engine()` 获取 strands 实例，下游 runner 消费无异常

---

**下一个模块**：LearningAgent（Phase 3 Week 7-9），TASK 文件在本模块完成后才发布。

**遇到任何不确定的地方，先在 Slack 里问大乖乖或架构审阅猫，不要自己拍脑袋决定。**
