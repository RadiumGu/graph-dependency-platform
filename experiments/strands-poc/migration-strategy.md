# Strands Agents 单向迁移策略（v3）

> **日期**: 2026-04-18
> **作者**: 架构审阅猫
> **状态**: 正式迁移策略（唯一权威版本）
> **适用前提**: 已明确决定 Strands 是**终态**，direct 代码只作过渡期回滚网，不再长期维护
> **变更历史**:
> - v3 2026-04-18：基于 v2 重写，采用 Strangler Fig 模式单向替换，删除 Monorepo / 长期分支 / 双轨运营 等选项

---

## 0. 早期方案对比（仅作参考）

早期曾提出过双轨方案，现已归档到 `archive/migration-strategy-v2-dual-track.md`。如果你看到的是本文档，说明**已决定走单向迁移**，不需要再对比备选。

| 维度 | 归档的双轨方案 | 本策略（单向） |
|------|-------------------|------------------|
| **终态** | 未明确，可能长期双轨 | ✅ Strands 单一实现 |
| **Git 工作流** | 长期分支 + big bang cutover | 单一 main + 小 PR 渐进 |
| **迁移模式** | 灰度/双轨/big bang 混合 | Strangler Fig 逐模块替换 |
| **冻结机制** | 未强制 | ✅ 冻结日 + 删除日 + CI 强制检查 |

---

## 1. 核心策略：Strangler Fig

> _逐模块替换，每个模块有明确的"退休日"。新代码从第一天起写 Strands，旧代码冻结后定期删除。_

```
Now           M1            M2           M3            M4-M6
 │            │             │            │             │
 ├─ direct ──┬─ direct ────┬─ frozen ───┬─ deleted ───▶
 │           │             │            │
 │           └─ strands ───┴─ strands ──┴─ strands ────▶
 │
 ▼
新功能一律 Strands，零新增 direct 代码
```

### 三条硬规则（写进 CONTRIBUTING）

1. **冻结日（Freeze Date）之后**，direct 代码**不接受新功能**，只允许修 P0 bug（SEV-1/2）
2. **删除日（Delete Date）到期强制删**，不延期；延期需走变更评审 + 书面理由
3. **新功能一律 Strands**，禁止"就在 direct 里加一下，以后再迁"

---

## 2. 前置条件 Checklist

进入 Phase 1 前必须全部 YES：

- [x] L0 Spike 通过（`experiments/strands-poc/report.md`）
- [x] Wave 1-5 已合并（profile 化 + Golden CI + 空结果重试 + Opus 升级）
- [ ] Opus 预算防护就位（token bucket 或 CloudWatch 告警）
- [ ] Wave 0 before baseline 已拿到（证明 Wave 1-5 收益幅度）
- [ ] 团队至少 2 人能接手 Strands 开发
- [ ] Bedrock 月度预算 ≥ 当前 3x 可承受
- [ ] CloudWatch / X-Ray OTel retention 策略敲定
- [ ] 迁移 RFC 已评审通过（含冻结日 / 删除日 / 负责人）

---

## 3. 五阶段规划（总周期 21-27 周）

### Phase 0 — 准备期（2 周）

1. 补 Opus 预算防护
2. 补 Wave 0 before/after 对比
3. 发布迁移 RFC，公告冻结日与删除日
4. 确认团队容量 + 预算

### Phase 1 — 铺地基（2 周）

**目标**：一次性抽象层，之后每模块迁移套模板。

```
rca/engines/
├── __init__.py
├── base.py              # 所有 Agent 抽象基类
├── factory.py           # 迁移期 env 开关（Phase 4 后删）
└── strands_common.py    # 共享：BedrockModel 构造、tool helpers、OTel 配置
```

**基类清单**（必须在 Phase 1 一次写完）：

```python
# rca/engines/base.py
class NLQueryBase(ABC): ...          # Smart Query
class HypothesisBase(ABC): ...       # chaos 假设生成
class LearningBase(ABC): ...         # coverage 分析
class ProberBase(ABC): ...           # RCA Layer2
class ChaosRunnerBase(ABC): ...      # chaos 执行
class PolicyGuardBase(ABC): ...      # pre-execution 策略
class DRExecutorBase(ABC): ...       # DR 执行
```

**统一返回结构**（所有 Agent）：

```python
{
    # 业务字段（每个 Agent 各自定义）
    ...,
    # 元数据（所有实现必须提供）
    "engine": str,                    # "direct" | "strands"
    "model_used": str | None,         # 实际用的 model id
    "latency_ms": int,
    "token_usage": dict | None,
    "trace": list[dict],              # Strands 的 tool-call 链，direct 为 []
    "error": str | None,
}
```

**Phase 1 交付物**：
- engines 包 + 基类
- `strands_common.py` 含 OTel → CloudWatch 接通（Phase 2 开始就要用）
- `tests/test_migration_deadlines.py` — 冻结 / 删除日 CI 检查
- `docs/migration/timeline.md` — 每个模块的冻结日 / 删除日公开表
- 更新 CONTRIBUTING.md：三条硬规则写进去

**本阶段结束 = 基础设施就位，之后每迁一个模块都是固定流程。**

---

### Phase 2 — Smart Query 先迁（3 周）

**为什么先迁它**：
- 业务影响最可控（只读查询，不改状态）
- Golden CI 已就位（Wave 3 的 20 cases），可直接量化验证
- L0 Spike 已证明技术可行

**Week 1-2：实现 + shadow 对跑**

```
rca/neptune/
├── nl_query.py            # 保留文件，内部 re-export（向后兼容）
├── nl_query_direct.py     # 原 NLQueryEngine 主体改名 DirectBedrockNLQuery
└── nl_query_strands.py    # 新增 StrandsNLQueryEngine
```

- `nl_query.py` 的 `NLQueryEngine` 保持可 import（通过 factory 返回实例）
- `test_golden_accuracy.py` 改造为 engine matrix：
  ```python
  @pytest.mark.parametrize("engine_name", ["direct", "strands"])
  def test_golden_case(engine_name, case): ...
  ```
- shadow 报告输出到 PR comment：每 case 的 cypher diff + 结果行数 diff + 延迟比

**Week 3：灰度切换**

- Streamlit `?engine=strands` 参数打开，通知内部用户试用 1 周
- CloudWatch 看板：成功率 / p99 延迟 / token 消耗 / Opus 命中率
- 每日比对：strands baseline 与 direct baseline

**达标门槛**（未达不进 Phase 3）：
- ✅ 准确率 ≥ 19/20（允许 5% 下降）
- ✅ p50 ≤ 2x direct，p99 ≤ 2.5x direct
- ✅ 月度 token 成本 ≤ 2.5x direct
- ✅ 1 周灰度无 SEV-1/2 事件

**达标后立即：**

1. 默认 engine 切到 strands（env 默认值）
2. 🔒 冻结 `nl_query_direct.py`：
   ```python
   # nl_query_direct.py 顶部
   """
   🔒 FROZEN 2026-05-15
   此文件仅保留作为 Strands 迁移期回滚兜底。
   禁止新增功能，只允许修 P0 bug（SEV-1/2）。
   预计删除日：2026-09-15
   """
   ```
3. 打 tag `v-strands-cutover-smartquery-YYYYMMDD`
4. 更新 `docs/migration/timeline.md`

---

### Phase 3 — 批量迁移（12-18 周）

按 **业务风险 × 技术复杂度** 递增排序，串行迁：

| Week | 模块 | 原因 |
|------|------|------|
| 4-6 | HypothesisAgent | 生成类，错了重新生成；Neptune 图谱 + LLM 已有 |
| 7-9 | LearningAgent | 分析类，不改状态；需要 Strands 的 session memory |
| 10-12 | RCA Layer2 Probers（6 个并行 Prober）| **multi-agent 编排主场**，Strands 1.0 的杀手级场景 |
| 13-15 | Chaos PolicyGuard | 加入 pre-execution policy_guard |
| 16-18 | Chaos Runner | 真正 mutate K8s/FIS 状态，晚于上面 |
| 19-20 | DR Plan Execution | 最敏感（跨 region、影响生产）|

**每模块 3 周标准流程**：

```
Week 1: Strands 实现 + 单测 + golden set 建立（如无）+ shadow 对跑
Week 2: 灰度（env 切 strands + 监控 + 每日对比）
Week 3: 达标 → 切换默认 → 冻结 direct → 写 ADR → 打 tag → 下一个
```

**纪律**：
- ❌ 不并行迁两个模块（单人容量 + 降低相互影响）
- ❌ 不跳过 shadow 对跑
- ❌ 不破坏冻结（有 PR 改冻结文件自动 fail）
- ✅ 每月做一次 migration review（进度 / 成本 / 风险）
- ✅ 每个模块有明确 owner + 2 人 review

---

### Phase 4 — 统一清理（2 周）

前置：所有模块已默认 Strands，且**各自稳定 ≥ 4 周**。

**Week 1：断开 direct 路径**
- 依赖管理升级为必装（根据 Phase 1 选定的方式）：
  - 如采用顶层 `pyproject.toml`：`strands` 从 `optional-dependencies` 改为 `dependencies`
  - 如采用 `requirements.txt`：去掉「可选」注释开关，直接作为必装项
- `factory.py` 中 direct 分支改为 `raise DeprecationWarning`
- CI 中 `direct` matrix 禁用
- 公告最后删除窗口（7 天缓冲，有问题立刻回滚 tag）

**Week 2：物理删除**
- 删除所有 `*_direct.py` 文件
- 删除 `nl_query.py` 等 re-export shim（直接用 `nl_query_strands.py` → 可改名回 `nl_query.py`）
- `factory.py` 简化为直接返回 Strands 实例，或整个删除（调用方直接 new Strands 类）
- `engines/base.py` 可保留（type annotation + 将来第 2 种实现时不手忙脚乱）
- 删 `docs/migration/timeline.md` 中已完成条目（归档到 `docs/archive/`）
- 打 tag `v-strands-only-YYYYMMDD`

**本阶段结束 = main 上只有一套 Strands 实现。**

---

### Phase 5 — 收尾（持续）

- 保留 `v-last-direct-YYYYMMDD` tag 作永久回滚锚点
- 保留 `docs/migration/decisions/*.md`（每模块的 ADR）作决策记录
- 本文档（v3）标记 `status: completed`，归档到 `docs/archive/migration/`
- `CONTRIBUTING.md` 中"三条硬规则"保留，作为未来类似迁移的模板
- 每季度做一次 Strands SDK 版本升级 review（pin 版本不动）

---

## 4. Git 工作流：单一主干 + 小 PR

**不开长期分支**（Strangler 模式下长期分支是累赘）。

```
main
├── PR-001: engines/base.py + factory skeleton       # Phase 1
├── PR-002: strands_common.py + OTel                 # Phase 1
├── PR-003: nl_query_direct rename + strands impl    # Phase 2 W1-2
├── PR-004: golden test engine matrix                # Phase 2 W1-2
├── PR-005: enable strands default for Smart Query   # Phase 2 W3
├── PR-006: freeze nl_query_direct.py                # Phase 2 W3
├── PR-007: hypothesis_strands.py + golden           # Phase 3 模块 1
├── PR-008: enable strands for Hypothesis            # Phase 3 模块 1
├── PR-009: freeze hypothesis_direct.py              # Phase 3 模块 1
├── ...（每模块重复 3 个 PR）
├── PR-NNN: strands as required dep + kill direct    # Phase 4
└── PR-NNN+1: delete all *_direct.py                 # Phase 4
```

**每个 PR 约束**：
- Scope 单一（实现 / 切换 / 冻结 / 删除 各自一个 PR）
- 必须跑 golden CI 对应 matrix
- 必须 ≥ 2 人 review
- PR 描述含 `docs/migration/timeline.md` 链接 + 当前模块状态

**关键 Git tag**：
| Tag | 时机 | 用途 |
|------|------|------|
| `v-last-direct-YYYYMMDD` | Phase 2 开始前 | 永久回滚锚点 |
| `v-strands-cutover-<module>-YYYYMMDD` | 每模块切默认 | 精准回滚单模块 |
| `v-strands-only-YYYYMMDD` | Phase 4 结束 | 终态标记 |

---

## 5. 冻结与删除日 CI 检查

### 5.1 timeline.md 结构

```yaml
# docs/migration/timeline.md（机器可读 + 人类可读）
modules:
  - name: smart-query
    direct_file: rca/neptune/nl_query_direct.py
    freeze_date: "2026-05-15"
    delete_date: "2026-09-15"
    owner: "@programming-cat"
    status: "active"  # active | frozen | deleted
  - name: hypothesis
    direct_file: rca/agents/hypothesis_direct.py
    freeze_date: "2026-06-15"
    delete_date: "2026-10-15"
    owner: "@programming-cat"
    status: "active"
  # ...
```

### 5.2 CI 检查脚本

```python
# scripts/check_migration_deadlines.py
import sys
import yaml
from datetime import date
from pathlib import Path

def main():
    tl = yaml.safe_load(Path("docs/migration/timeline.md").read_text())
    today = date.today()
    errors = []
    for m in tl["modules"]:
        path = Path(m["direct_file"])
        deadline = date.fromisoformat(m["delete_date"])
        if path.exists() and today > deadline:
            errors.append(
                f"❌ {path} 已过删除日 {deadline}，请删除或提交延期 ADR"
            )
        # 冻结期检查（交给 pre-commit hook / PR bot）
    if errors:
        print("\n".join(errors))
        sys.exit(1)

if __name__ == "__main__":
    main()
```

### 5.3 冻结文件 PR 检查

GitHub Actions 或 pre-commit：
```yaml
# .github/workflows/check-frozen.yml
- name: Block edits to frozen files
  run: |
    frozen=$(git grep -l "🔒 FROZEN" -- "rca/**")
    changed=$(git diff --name-only ${{ github.event.pull_request.base.sha }}..HEAD)
    for f in $frozen; do
      if echo "$changed" | grep -q "$f"; then
        echo "❌ $f is frozen; PR must include 'P0-bugfix' label to proceed"
        exit 1
      fi
    done
```

---

## 6. 度量与监控

### 6.1 每模块必须建立的 Golden Set

| 模块 | Golden 规模 | 校验维度 |
|------|------------|---------|
| Smart Query | 20 cases（已有）| cypher feature + result correctness |
| HypothesisAgent | 15-20 假设场景 | 假设合理性 + 图谱引用正确性 |
| LearningAgent | 10 coverage snapshot | coverage 数字误差 ≤ 5% |
| RCA Layer2 Probers | 6 个已知故障场景 | 根因定位准确率 ≥ baseline |
| PolicyGuard | 10 规则场景 | pass/block 一致率 100% |
| Chaos Runner | 3 个 dry-run 实验 | 阶段转换 + 幂等性 |
| DR Executor | 2 个完整演练 | RTO/RPO 与 direct 一致 |

### 6.2 CloudWatch Dashboard（迁移期必建）

- 每 Agent 的 engine 分布（direct vs strands 调用数）
- p50 / p99 延迟（双版本并排）
- Token 消耗（input / output / 按 engine）
- 错误率 / retry 率
- Opus 升级命中率 + 预算告警

### 6.3 每模块切换门槛（必须全达标）

| 指标 | 门槛 |
|------|------|
| 准确率 | ≥ baseline - 5% |
| p50 latency | ≤ 2x direct |
| p99 latency | ≤ 2.5x direct |
| 月度 token 成本 | ≤ 2.5x direct |
| 1 周灰度 SEV-1/2 | 0 |
| 关键用户反馈 | 至少 3 位内部用户确认可用 |

---

## 7. 风险管控

| 风险 | 发生概率 | 影响 | 缓解 |
|------|---------|------|------|
| Strands 某模块达不到指标 | 🟡 中 | 该模块延迟 | 该模块冻结推迟 1 个月，不影响其他模块 |
| Bedrock 账单超预算 | 🟡 中 | 可能暂停迁移 | Phase 2 结束立即外推月度成本；超预算暂停 Phase 3 |
| Strands SDK 1.x → 2.x breaking | 🟡 中 | 所有已迁模块受影响 | pin 版本 + 每次升级过完整 golden 矩阵 |
| Global inference profile 失效 | 🔴 高 | 所有 Strands 不可用 | 季度探活 + 维护备选 profile 清单 + 紧急 fallback 脚本 |
| 僵尸 direct 代码延迟删除 | 🟡 中 | 违反纪律、积技术债 | 删除日 CI 自动 fail PR |
| 团队人员流动 | 🟡 中 | 迁移停摆 | 每模块 2 人 owner + 文档双语 |
| 客户生产事故 | 🟡 中 | 需紧急回滚 | Tag 永久保留；48h 内能回滚到 v-last-direct |
| Opus 无预算 cap | 🔴 高 | 账单爆炸 | Phase 0 必须补 token bucket + 告警 |
| Wave 4 retry 与 Strands ReAct 双重 | 🟢 低 | 延迟翻倍、成本翻倍 | Strands 版关闭 `_should_retry_on_empty`，依赖 ReAct 原生能力 |

---

## 8. 成本与时间预算

### 8.1 工期

| Phase | 工期 | 工作量 |
|-------|------|-------|
| 0 准备 | 2 周 | 3 人日 |
| 1 地基 | 2 周 | 5 人日 |
| 2 Smart Query | 3 周 | 8 人日 |
| 3 批量（6 模块 × 3 周）| 12-18 周 | 40-50 人日 |
| 4 清理 | 2 周 | 3 人日 |
| **总计** | **21-27 周（5-7 月）** | **~60 人日** |

### 8.2 Bedrock 账单预估

| 阶段 | 账单倍数（相对 Phase 0）|
|------|---------|
| Phase 1-2 中期 | +10%（仅 Smart Query 走 Strands）|
| Phase 3 中期 | +50%（约一半模块走 Strands + Opus）|
| Phase 4 开始 | +150-200%（全量 Strands + ReAct + Opus）|
| Phase 4 + 3 个月后 | +100%（通过 schema 按需读取 / prompt 瘦身降本）|

**关键节点**：Phase 2 结束立即做月度账单外推，超预算则暂停 Phase 3 启动。

---

## 9. 与其他规划的协调

1. **profile 化（P1'）已完成** — 天然支持多客户，迁移期不影响新客户接入
2. **Golden CI** — 每个模块都要有自己的 golden set，Smart Query 是模板
3. **Well-Architected 5 支柱** — 迁移期持续监控，不因迁移把可靠性打下来
4. **客户 PA 视角 — 季度评审** — "继续迁 vs 停下来" 避免沉没成本陷阱
5. **improvements.md 的 Resilience Score / Probe / GameDay** — 是 Phase 3 之后的增量能力，可在 Strands 化之后加（multi-agent 天然更合适）

---

## 10. 不要做的事

1. ❌ **不并行迁多个模块** — 看似快实则乱
2. ❌ **不跳过 shadow 对跑** — "感觉变好"不算
3. ❌ **不在冻结期往 direct 里加新功能** — 破坏纪律，永远迁不完
4. ❌ **不无限期保留 direct** — 到期就删，延期需书面 ADR
5. ❌ **不把 Strands 当银弹** — 纯数据处理不强行 Agent 化，暴露为 `@tool` 即可
6. ❌ **不开长期分支** — Strangler 模式要求小步快走
7. ❌ **不依赖单一模型** — Global profile 有风险，维护备选清单
8. ❌ **不在迁移期做大重构** — 迁移 + 重构 = 双重不确定性

---

## 11. 决策门（Go / No-Go Gates）

每个 Phase 之间设置决策门，**必须通过才能进下一 Phase**：

### Gate A（Phase 0 → 1）
- [ ] 所有前置条件 ✅
- [ ] 迁移 RFC 通过
- [ ] 预算 + 团队确认

### Gate B（Phase 2 → 3）
- [ ] Smart Query 所有门槛达标
- [ ] 灰度 1 周无 SEV-1/2
- [ ] 月度成本外推 ≤ 预算

### Gate C（Phase 3 → 4）
- [ ] 所有模块切换完成
- [ ] 各自稳定 ≥ 4 周
- [ ] timeline.md 中所有 `status: frozen`，无 `active`

### Gate D（Phase 4 → 5）
- [ ] CI 中 direct matrix 已禁用
- [ ] 所有 `*_direct.py` 已删除
- [ ] `v-strands-only-YYYYMMDD` tag 已打

**Gate 不通过 → 暂停并开 review，不硬推进。**

---

## 12. 回滚策略

### 12.1 单模块回滚（Phase 3 期间）

```bash
# 1. env 开关回切
export <MODULE>_ENGINE=direct

# 2. 如需要代码回退：checkout 单模块
git checkout v-strands-cutover-<module>-YYYYMMDD -- rca/<module>/
```

### 12.2 紧急全量回滚（Phase 4 前）

```bash
export AGENT_FRAMEWORK=direct
# 不需要代码变更，因为 *_direct.py 还在
```

### 12.3 Phase 4 后回滚

```bash
# direct 已删，需 cherry-pick 回来
git checkout v-last-direct-YYYYMMDD -- rca/<module>/<file>_direct.py
git checkout v-last-direct-YYYYMMDD -- rca/engines/factory.py
# 启用 factory 的 direct 分支 + 重新部署
# 预计 2-4 人日
```

**Phase 4 后回滚 = 灾难级事件，说明 Strands 有严重问题，同时应该评估 langgraph / bedrock-agents 备选。**

---

## 13. 总结

**核心路径**：准备 → 地基 → Smart Query 先行 → 6 模块串行 → 清理 → 收尾。

**核心原则**：
1. 单向推进，不回头（Gate 不通过才暂停，不回退）
2. 串行迁移，每模块 3 周标准流程
3. 冻结 + 删除日强制，CI 自动检查
4. 小 PR 多次提交，不开长期分支
5. 每模块必须有 golden set + 量化门槛
6. Tag 永久保留作回滚锚点

**终态（Phase 4 结束）**：
- main 只有一套 Strands 实现
- Strands 相关依赖成为必装（pyproject.toml 或 requirements.txt）
- `docs/migration/` 归档到 archive
- 所有 direct 代码在 git 历史里可查

---

## 14. 相关文档

- `experiments/strands-poc/report.md` — L0 Spike 结果
- `experiments/strands-poc/archive/migration-strategy-v2-dual-track.md` — 早期双轨方案（已归档，仅供历史参考）
- `~/tech/blog/gp-improve/smart-query-accuracy-improvement.md` — Wave 1-5 源计划（仓库外）
- `~/tech/blog/gp-improve/harness-resilience-testing/improvements.md` — Resilience Score / Probe / GameDay 增量能力清单（仓库外）
- `docs/migration/timeline.md` — 模块冻结 / 删除日（Phase 1 建立）
- `docs/migration/decisions/*.md` — 每模块 ADR（Phase 3 产出）

---

_本文档是 Draft，执行前需要编程猫、架构审阅猫、大乖乖三方确认，并正式发布为迁移 RFC。_
