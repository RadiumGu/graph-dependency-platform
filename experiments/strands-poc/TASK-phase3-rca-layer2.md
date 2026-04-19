# TASK: Phase 3 Module 3 — RCA Layer2 Probers 迁移到 Strands Agents (Multi-Agent)

> **任务归属**: 编程猫
> **发起**: 大乖乖 + 架构审阅猫
> **日期**: 2026-04-XX（Gate A 通过后启动）
> **范围**: 把 `rca/collectors/aws_probers.py` 的 6 个并行 Prober 从"ThreadPoolExecutor + 直调 Bedrock"迁到 Strands multi-agent 编排（orchestrator + 6 子 agent）
> **预计工作量**: 3 周（migration-strategy § 3.3 Phase 3 Week 10-12）
> **成功标志**: 所有 PR 合入 main + 验收门槛达标 + `layer2_direct.py` 冻结
> **前序模块**: HypothesisAgent（Module 1，冻结 2026-04-18）、LearningAgent（Module 2，冻结 2026-04-26）
> **⚠️ 本模块特殊性**: Phase 3 嵌套 Agent 风险最高的模块 — 6 个 Prober 子 Agent + 1 个 Orchestrator，Strands 套 Strands 内存爆炸风险极大

---

## 0. 必读顺序

1. `migration-strategy.md`（§ 3.3 Phase 3 + § 6.3 切换门槛 + § 6.4 Prompt Caching）
2. `TASK-phase3-shared-notes.md`（共享规范，**§ 3.3 是 RCA Layer2 的缓存设计要点**）
3. `retros/learning-retro.md`（**必读** — § 6 Top 3 建议 + § 7 模板修改建议，直接影响本模块执行）
4. `retros/hypothesis-retro.md`（**必读** — § 6 Top 3 + § 2.2 漏提的陷阱，仍有适用建议）
5. `TASK-phase3-learning-agent.md`（结构参考 + Neptune helper 抽取产物）
6. `TASK-L2-prompt-caching.md`（Prompt Caching 实现参考）
7. `report.md`（L0/L1/L2 + Phase 3 Module 1/2 经验总结）
8. **当前模块代码**：
   - `rca/collectors/aws_probers.py` — 539 行，现版 6 个 Probe 实现 + 并行调度
   - `rca/core/rca_engine.py` — Layer2 调用入口
   - `rca/core/topology_correlator.py` — 拓扑关联
   - `chaos/code/runner/neptune_helpers.py` — Neptune 查询公共 helper（Module 2 产出）
   - `rca/engines/factory.py` — 引擎工厂（需扩展）
   - `rca/engines/strands_common.py` — BedrockModel + OTel helper

---

## 1. 工作目录

```
/home/ubuntu/tech/graph-dependency-platform
```

---

## 2. 前置条件（Gate A 确认）

### 不可豁免（必须全部满足）

- [ ] LearningAgent（Module 2）已冻结，`timeline.md` status = `frozen`
- [ ] `make_learning_engine()` + `make_hypothesis_engine()` 在 orchestrator 中稳定工作 ≥ 1 周
- [ ] Bedrock 月度预算足够承受 +40-60% 临时增幅（**6 个子 Agent 并行调用，是之前模块的 3-5 倍**）
- [ ] `rca/collectors/aws_probers.py` 无进行中的功能开发（冻结窗口确认）
- [ ] Neptune 集群在高峰期能承受 6 个并发查询（当前 `run_all_probes` 已是 6 并发，但 Strands 版每个 Prober 可能发多轮查询）

### 可豁免（大乖乖 Slack 确认即可）

- [ ] Smart Query L2 稳定期 ≥ 4 周（可口头豁免，但需记录）
- [ ] `docs/migration/timeline.md` 中 `rca-layer2-probers` 的 `freeze_date` / `delete_date` 已填（可在 Week 3 补填）
- [ ] 大乖乖已打 tag `v-last-direct-layer2-YYYYMMDD`（可在 PR-final 时补打）

*不可豁免项任一不满足 → 暂停启动，通知大乖乖。*

---

## 3. 本模块依赖的 env 变量清单

| 变量名 | 用途 | 与其他模块差异 |
|--------|------|---------------|
| `NEPTUNE_HOST` | Neptune 集群地址 | ⚠️ **不是 `NEPTUNE_ENDPOINT`**（Smart Query 用 `NEPTUNE_ENDPOINT`，RCA 模块用 `NEPTUNE_HOST`）|
| `BEDROCK_REGION` | Bedrock 调用区域 | 同前序模块 |
| `BEDROCK_MODEL` | Bedrock 模型 ID | 同前序模块 |
| `LAYER2_ENGINE` | 引擎切换 `direct\|strands` | 本模块专用 |
| `AWS_DEFAULT_REGION` | AWS SDK 默认区域（Prober 用） | 被 `rca/config.py` 的 `get_region()` 消费 |
| `HYPOTHESIS_ENGINE` | HypothesisAgent 引擎 | ⚠️ **Prober tool 内部如果调 hypothesis engine，必须强制 `direct`**（见 § 6 嵌套 Agent 风险评估）|
| `LEARNING_ENGINE` | LearningAgent 引擎 | 同上 |

> ⚠️ **sys.path 提醒**：`rca/collectors/aws_probers.py` 使用 `from shared import get_region`，PYTHONPATH 须包含 `rca/`。PR-factory（接线 PR）时必须检查 import 路径。

---

## 4. 任务范围（严格限定）

**做**：
- `rca/collectors/aws_probers.py` 的 6 个 Probe 从 ThreadPoolExecutor 并行改造为 Strands multi-agent 编排
- 引入 `Layer2ProberBase` 抽象基类 + factory
- 新增 Orchestrator Agent（调度 6 个 Prober 子 Agent）
- 建立 Layer2 的 Golden Set（6 个已知故障场景）
- Prompt Caching 默认启用（每个 Prober 独立缓存池）
- 每个 Prober 返回 dict 规范化（含 `engine` / `model_used` / `latency_ms` / `token_usage` / `trace`）

**不做**：
- 不改 `rca/core/rca_engine.py` 的调用接口（只换内部实现）
- 不改 `rca/core/topology_correlator.py`
- 不改已冻结的 HypothesisAgent / LearningAgent
- 不加新 Probe 类型（本任务只迁现有 6 个）
- 不做 L3 / Decision Engine 改动
- 不做"新增能力"（本任务只是 framework 迁移，业务逻辑等价）

---

## 5. Multi-Agent 编排架构

### 5.1 架构图

```
                    ┌─────────────────────────┐
                    │   rca_engine.py          │
                    │   run_layer2_probes()    │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │  Layer2Orchestrator      │
                    │  (Strands Agent)         │
                    │                          │
                    │  tools:                  │
                    │   probe_cloudwatch()     │
                    │   probe_xray()           │
                    │   probe_neptune()        │
                    │   probe_logs()           │
                    │   probe_deployment()     │
                    │   probe_network()        │
                    └──┬───┬───┬───┬───┬───┬──┘
                       │   │   │   │   │   │
          ┌────────────┘   │   │   │   │   └────────────┐
          ▼                ▼   ▼   ▼   ▼                ▼
    ┌──────────┐   ┌─────┐ ┌─────┐ ┌────┐ ┌──────────┐ ┌─────────┐
    │CloudWatch│   │X-Ray│ │Nept.│ │Logs│ │Deployment│ │ Network │
    │ Prober   │   │Prob.│ │Prob.│ │Pro.│ │  Prober  │ │ Prober  │
    │ (Agent)  │   │(Ag.)│ │(Ag.)│ │(A.)│ │ (Agent)  │ │ (Agent) │
    └──────────┘   └─────┘ └─────┘ └────┘ └──────────┘ └─────────┘
         │              │      │      │         │            │
    ┌────▼────┐    ┌────▼──┐  │  ┌───▼───┐ ┌───▼────┐ ┌────▼────┐
    │CW API   │    │X-Ray  │  │  │CW Logs│ │CodeDep.│ │VPC Flow │
    │Metrics  │    │Traces │  │  │Insight│ │ECS/EKS │ │Route53  │
    └─────────┘    └───────┘  │  └───────┘ └────────┘ └─────────┘
                              │
                         Neptune DB
```

### 5.2 编排模式

- **Orchestrator**：Strands Agent，system prompt 描述事件上下文 + 6 个 Prober 的职责分工
- **每个 Prober**：独立 Strands Agent，通过 `agent.tool()` 注册为 Orchestrator 的 tool
- **⚠️ 不用 `agent.as_tool(agent_b)` 的嵌套 Strands 模式**（见 § 6 风险评估），改用 **`@tool` 包装 direct 调用**

### 5.3 并行调度

现有 `run_all_probes()` 用 `ThreadPoolExecutor(max_workers=6)`。Strands 版有两种方案：

| 方案 | 描述 | 风险 |
|------|------|------|
| A: Orchestrator 串行调 6 个 tool | Orchestrator 每轮 ReAct 调 1 个 tool | 慢（6 轮 ReAct = 6 次 LLM call 只为决定"调下一个"） |
| B: Orchestrator 一轮调全部 tool | Strands parallel tool call 特性 | ⚠️ Bedrock 并发限制；需验证 Strands 是否支持 |
| **C: Python 并行 + Strands 单 Prober**（推荐） | Python ThreadPoolExecutor 并行调 6 个 Strands Prober Agent，Orchestrator 只做结果汇总 | 兼顾性能和控制 |

**推荐方案 C**：保留 Python 级并行（已验证稳定），每个 Prober 独立是 Strands Agent（可多轮 ReAct 深入探查），Orchestrator 用 Strands Agent 做最终汇总和决策。

---

## 6. 嵌套 Agent 风险评估 ⚠️⚠️⚠️

> **这是 Phase 3 嵌套 Agent 风险最高的模块。** Module 2 retro § 6 Top 1 和 § 3.1 坑 1 已证实：Strands tool 内调其他 Strands Agent 会内存爆炸被 SIGKILL。

### 6.1 风险场景枚举

| 场景 | 嵌套层数 | 风险等级 | 缓解措施 |
|------|---------|---------|---------|
| Orchestrator (Strands) 调 Prober (Strands) | 2 层 | 🔴 **极高** | 不用 `agent.as_tool()`，用 `@tool` 包装 |
| Prober tool 内调 `make_hypothesis_engine()` | 2-3 层 | 🔴 **极高** | tool 内强制 `HYPOTHESIS_ENGINE=direct` |
| Prober tool 内调 `make_learning_engine()` | 2-3 层 | 🔴 **极高** | tool 内强制 `LEARNING_ENGINE=direct` |
| 6 个 Prober 同时实例化 Strands Agent | 6 个进程 | 🟡 **高** | 监控总内存，设 cgroup 告警 |

### 6.2 硬规则

1. **任何 `@tool` 函数体内调用其他 engine，必须临时设 `os.environ["XXX_ENGINE"] = "direct"`，调完恢复原值。** 不做会被 SIGKILL，debug 困难（看不到 OOM 日志）。
2. **不用 `agent.as_tool(agent_b)` 模式做 Prober 注册。** 改用 `@tool` 装饰器包装每个 Prober 的调用函数。
3. **Orchestrator 的 system prompt 不引用子 Agent 的 system prompt。** 避免 token 膨胀。
4. **每个 Prober Agent 实例化时设 `max_tokens=4096`。** 避免单个 Prober 输出过长导致 MaxTokensReached（Module 1 retro § 3.1 坑 4）。

### 6.3 内存预算估算

```
单个 Strands Agent 内存 ≈ 80-120 MB（含 BedrockModel + tool schema）
6 Prober × 120 MB = 720 MB
1 Orchestrator × 120 MB = 120 MB
总计 ≈ 840 MB

+ Python 基础 + boto3 clients ≈ 300 MB
总进程内存 ≈ 1.1 GB
```

> ⚠️ 如果 cgroup 限制 < 2 GB，6 个并行 Prober 有被 SIGKILL 的风险。**必须在 Gate A 确认运行环境内存限制 ≥ 2 GB**。

---

## 7. 产出清单

### 7.1 抽象层（engines 包扩展）

修改 `rca/engines/base.py`（已存在，仅扩展）：

```python
class Layer2ProberBase(ABC):
    """RCA Layer2 Prober 引擎基类。"""

    def __init__(self, profile: Any = None): ...

    @abstractmethod
    def run_probes(
        self,
        signal: dict,
        affected_service: str,
        timeout_sec: int = 30,
    ) -> dict:
        """
        并行执行 6 个 Prober，返回汇总结果。

        Args:
            signal: 事件信号（alarm payload / event metadata）
            affected_service: 受影响的服务名
            timeout_sec: 每个 Prober 的超时（默认 30s，Strands 版需更长）

        Returns:
            {
                "probe_results": list[dict],   # 每个 Prober 的 ProbeResult
                "summary": str,                # 汇总摘要（Orchestrator 生成）
                "score_delta": int,            # 总分增量（cap at 40）
                "engine": str,                 # "direct" | "strands"
                "model_used": str | None,
                "latency_ms": int,
                "token_usage": {
                    "input": int, "output": int, "total": int,
                    "cache_read": int, "cache_write": int,
                    "per_prober": dict[str, dict],  # 每个 Prober 的独立 token 统计
                } | None,
                "trace": list[dict],
                "error": str | None,
            }
        """

    @abstractmethod
    def run_single_probe(
        self,
        probe_name: str,
        signal: dict,
        affected_service: str,
    ) -> dict:
        """
        单独执行一个 Prober（用于调试/测试）。

        Returns:
            {
                "service_name": str,
                "healthy": bool,
                "score_delta": int,
                "summary": str,
                "details": dict,
                "evidence": list[str],
                "engine": str,
                "token_usage": dict | None,
                "trace": list[dict],
            }
        """
```

修改 `rca/engines/factory.py`：添加 `make_layer2_engine(profile=None)`，读 env `LAYER2_ENGINE=direct|strands`。

> ⚠️ **factory.py import 路径确认**：所有新增 import 必须用 `from collectors.aws_probers import ...` 或 `from engines.xxx import ...`，**不用 `rca.collectors.aws_probers`**（PYTHONPATH 是 `rca`，不是 `rca` 的父目录）。Module 1 + Module 2 都踩过这个坑。

### 7.2 具体实现

```
rca/collectors/
├── aws_probers.py                # 向后兼容 shim（保持 run_all_probes 可 import）
├── layer2_direct.py              # 原 aws_probers.py 核心逻辑改名，继承 Layer2ProberBase
├── layer2_strands.py             # 新增 Strands multi-agent 实现
├── layer2_orchestrator.py        # Orchestrator Agent 定义
├── layer2_tools.py               # 6 个 Prober 的 @tool 定义
└── prober_agents/
    ├── __init__.py
    ├── cloudwatch_prober.py      # CloudWatch Metrics Prober Agent
    ├── xray_prober.py            # X-Ray Traces Prober Agent
    ├── neptune_prober.py         # Neptune Graph Prober Agent
    ├── logs_prober.py            # CloudWatch Logs Prober Agent
    ├── deployment_prober.py      # Deployment (ECS/EKS/CodeDeploy) Prober Agent
    └── network_prober.py         # Network (VPC Flow/Route53) Prober Agent
```

### 7.3 现有 Probe → Strands Prober Agent 映射

| 现有 Probe (aws_probers.py) | 目标 Strands Prober Agent | 数据源 | 备注 |
|-----|------|--------|------|
| SQSProbe + DynamoDBProbe | CloudWatch Prober | CW Metrics API | 合并为按 metric 维度探查 |
| LambdaProbe | Logs Prober | CW Logs Insights | Lambda 错误主要从日志分析 |
| ALBProbe | Network Prober | VPC Flow Logs + ALB Access Logs | 网络层面异常 |
| EC2ASGProbe | Deployment Prober | ECS/EKS Describe + CodeDeploy | 部署状态变更 |
| StepFunctionsProbe | X-Ray Prober | X-Ray Traces | 链路追踪 |
| （新增） | Neptune Prober | Neptune Graph Query | 拓扑关联探查（利用 neptune_helpers.py） |

### 7.4 Tool 定义（`layer2_tools.py`）

```python
from strands import tool
import os

@tool
def probe_cloudwatch(signal: dict, affected_service: str) -> str:
    """探查 CloudWatch Metrics 异常：SQS 积压、DynamoDB 限流、自定义指标。"""
    ...

@tool
def probe_xray(signal: dict, affected_service: str) -> str:
    """探查 X-Ray Traces 异常：延迟飙升、错误链路、下游依赖故障。"""
    ...

@tool
def probe_neptune(signal: dict, affected_service: str) -> str:
    """探查 Neptune 图谱中的拓扑异常：依赖链断裂、环路、孤立节点。
    使用 neptune_helpers.py 公共 helper。"""
    ...

@tool
def probe_logs(signal: dict, affected_service: str) -> str:
    """探查 CloudWatch Logs Insights 异常：Lambda 错误率、OOM、超时。"""
    ...

@tool
def probe_deployment(signal: dict, affected_service: str) -> str:
    """探查部署状态变更：ECS service event、EKS pod restart、CodeDeploy rollback。"""
    ...

@tool
def probe_network(signal: dict, affected_service: str) -> str:
    """探查网络层异常：ALB 5xx 飙升、VPC Flow 拒绝、Route53 健康检查失败。"""
    ...
```

> ⚠️ **每个 `@tool` 函数体内如果需要调用 hypothesis/learning engine**：
> ```python
> @tool
> def probe_neptune(signal: dict, affected_service: str) -> str:
>     # 如果需要调其他 engine，强制 direct
>     original_engine = os.environ.get("HYPOTHESIS_ENGINE", "direct")
>     os.environ["HYPOTHESIS_ENGINE"] = "direct"
>     try:
>         # ... 业务逻辑 ...
>         pass
>     finally:
>         os.environ["HYPOTHESIS_ENGINE"] = original_engine
> ```

### 7.5 Orchestrator Agent（`layer2_orchestrator.py`）

```python
class Layer2Orchestrator:
    """
    Strands Orchestrator Agent。
    不直接嵌套子 Agent，而是通过 @tool 调用 6 个 Prober。
    自身是一个 Strands Agent，负责：
    1. 决定哪些 Prober 与当前事件相关（is_relevant 逻辑）
    2. 汇总各 Prober 结果
    3. 生成跨 Prober 关联分析（这是 Direct 版做不到的增值点）
    """
```

### 7.6 Prompt Caching 集成

#### 参考实现
- `rca/neptune/nl_query_direct.py` — Direct 缓存参考
- `rca/engines/strands_common.py` — Strands 缓存参考

#### 本模块缓存对象（每个 Prober 独立）
- Prober 职责定义（CloudWatch / X-Ray / Neptune / Logs / Deployment / Network）
- 事件分类 schema
- Service catalog 引用

#### 本模块不缓存的对象
- 本次事件详情（signal payload）
- 来自其他 Prober 的 observation

#### 缓存池架构

```
Orchestrator 缓存池:  ~3-5k tokens (事件分类 + Prober 职责概览)
CloudWatch Prober:    ~6-10k tokens (Metric 命名空间 + 异常阈值规则)
X-Ray Prober:         ~6-10k tokens (Trace 分析模式 + 错误分类)
Neptune Prober:       ~6-10k tokens (Graph schema + 拓扑规则)
Logs Prober:          ~6-10k tokens (Log Insights 查询模板 + 错误模式)
Deployment Prober:    ~6-10k tokens (ECS/EKS 事件分类 + 回滚判断)
Network Prober:       ~6-10k tokens (VPC Flow 分析 + Route53 规则)
────────────────────────────────────────────────
总计:                 ~40-65k tokens 缓存空间
```

#### 本模块预期缓存命中率（稳态）
- 单 Prober: ≥ 60%（每个 Prober 在同一事件窗口内多次调用复用 system prompt）
- 事件高峰期（区域故障 50+ 事件）: ≥ 80%（shared-notes § 3.3 预估）
- **预估成本节省: 50-60%**（shared-notes § 2 预估）

#### 本模块特殊陷阱
- **6 个缓存池同时工作** — 需要监控每个 Prober 的命中率，不能只看总体
- **Orchestrator + 6 Prober = 7 个 system prompt 都要达 1024 token 下限** — 任何一个不达标都会静默失效
- **并行调用时缓存 TTL 竞争** — 6 个 Prober 几乎同时调用，第一次都是 cache_write，第二次才有 cache_read
- **Profile 更新导致 7 个缓存池同时失效** — 成本冲击是单 Agent 的 7 倍

#### assert_cacheable 强制检查

每个 Prober Agent + Orchestrator 的 `__init__` 必须调用：

```python
from engines.strands_common import assert_cacheable

def assert_cacheable(system_prompt: str, min_tokens: int = 1024):
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")  # ⚠️ 用 get_encoding，不用 encoding_for_model
    token_count = len(enc.encode(system_prompt))
    assert token_count >= min_tokens, (
        f"System prompt only {token_count} tokens, "
        f"below {min_tokens} minimum for Bedrock prompt caching."
    )
```

### 7.7 Golden Set

新增 `tests/golden/layer2/`：

```
tests/golden/layer2/
├── scenarios.yaml               # 6 个已知故障场景
├── cases.yaml                   # 基于 direct 采样生成的行为约束式 golden
├── BASELINE-direct.md           # 每次 RUN_GOLDEN 时生成
└── BASELINE-strands.md
```

#### 7.7.1 Golden 的哲学：行为约束而非精确匹配

```yaml
# cases.yaml 单条结构：
- id: p001
  scenario: "petsite EKS Pod CrashLoopBackOff + SQS 积压"
  signal_source: "experiments/strands-poc/fixtures/signal_p001.json"
  
  # 预期触发的 Prober
  probers_must_trigger:
    - "cloudwatch"   # SQS 积压
    - "deployment"   # Pod 状态
    - "logs"         # 错误日志
  
  # 预期检测到异常
  min_anomalies: 2
  max_anomalies: 6
  
  # 必须出现的证据关键词
  evidence_must_include_any:
    - "CrashLoopBackOff"
    - "SQS"
    - "backlog"
  
  # 分数增量范围
  min_score_delta: 10
  max_score_delta: 40
  
  # Orchestrator 汇总必须提到的关联
  summary_must_include_any:
    - "Pod"
    - "queue"
```

#### 7.7.2 Golden 场景按业务风险分类（4 类分桶）

| 分桶 | 场景数 | 说明 | 场景 ID |
|------|--------|------|---------|
| 核心服务故障 | 2 | petsite Pod crash + payment 超时 — Tier0 服务的多维探查 | p001, p002 |
| 特殊后端故障 | 2 | Lambda 限流 + RDS failover — 非 K8s 后端的异常模式 | p003, p004 |
| 错误输入 | 1 | 畸形 signal / 不存在的 service — should_error 或 graceful degradation | p005 |
| 边界场景 | 1 | 全 Prober 都无异常（healthy 场景）— 验证空结果处理 | p006 |

#### 7.7.3 Golden 构建步骤

**阶段 1：准备 6 个故障信号 fixture**

从 RCA 历史事件中提取 6 个有代表性的 signal payload：
```bash
# 导出脚本
rca/scripts/export_layer2_signals.py
# 输出到
experiments/strands-poc/fixtures/signal_p001.json ... p006.json
```

**阶段 2：用 direct 版采样建 baseline**

写 `rca/scripts/sample_for_golden_layer2.py`：

> ⚠️ **采样脚本必须 incremental save + resume**（Module 2 retro § 6 Top 2 强制要求）：
> ```python
> import json, logging, signal as sig, os
> from pathlib import Path
> 
> sig.signal(sig.SIGPIPE, sig.SIG_DFL)
> 
> SAMPLES_DIR = Path("experiments/strands-poc/samples/layer2")
> SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
> PROGRESS_FILE = SAMPLES_DIR / "progress.json"
> ALL_SAMPLES_FILE = SAMPLES_DIR / "all_samples.json"
> 
> fh = logging.FileHandler(SAMPLES_DIR / "run.log")
> logger = logging.getLogger("sample_layer2")
> logger.addHandler(fh)
> 
> # Resume: 读已完成的场景
> completed = set()
> all_samples = []
> if PROGRESS_FILE.exists():
>     progress = json.loads(PROGRESS_FILE.read_text())
>     completed = set(progress.get("completed", []))
>     if ALL_SAMPLES_FILE.exists():
>         all_samples = json.loads(ALL_SAMPLES_FILE.read_text())
>     logger.info(f"Resuming: {len(completed)} scenarios already done")
> 
> for scenario in scenarios:
>     if scenario["id"] in completed:
>         logger.info(f"⏭️ {scenario['id']} already done, skipping")
>         continue
>     try:
>         # 调用 DirectLayer2Prober.run_probes() 采样 3 次
>         ...
>         logger.info(f"✅ {scenario['id']} done")
>         completed.add(scenario["id"])
>         # Incremental save — 每个场景完成后立即写磁盘
>         ALL_SAMPLES_FILE.write_text(json.dumps(all_samples, indent=2))
>         PROGRESS_FILE.write_text(json.dumps({"completed": list(completed)}))
>     except Exception as e:
>         logger.error(f"❌ {scenario['id']} failed: {e}")
>         continue  # 单场景失败不挂整组
> ```

### 7.8 测试文件

- `tests/test_layer2_golden.py` — engine matrix（direct / strands）parametrize
- `tests/test_layer2_shadow.py` — direct vs strands 对比报告
- `tests/test_layer2_memory.py` — **本模块新增**：内存压力测试，验证 6 Prober 并行不超 2 GB

### 7.9 文档

- `experiments/strands-poc/report.md` 新增 `§ 11 Phase 3 Module 3 — RCA Layer2 Probers` 章节
- `docs/migration/timeline.md` 更新 `rca-layer2-probers.status` 从 `planned` → `active` → `frozen`
- `docs/migration/decisions/layer2-migration-adr.md`（Week 3 切换完成后写 ADR）

---

## 8. 硬约束

1. ⚠️ **Strands tool 内调其他 engine 必须强制 direct** — 任何 `@tool` 函数体内调 `make_hypothesis_engine()` / `make_learning_engine()` / 其他 Strands Agent 时，必须临时设 `os.environ["XXX_ENGINE"] = "direct"`，调完恢复。违反此规则 = SIGKILL + 无法 debug。
2. ⚠️ **不用 `agent.as_tool(agent_b)` 嵌套 Strands** — 所有 Prober 通过 `@tool` 装饰器注册，不通过 Strands native agent-as-tool 机制。
3. ⚠️ **业务行为等价** — `run_probes()` 产出的 `probe_results` 必须与 Direct 版在相同输入下行为可对齐（通过 Golden Set 验证）
4. ⚠️ **不改 `rca/core/rca_engine.py` 的调用接口** — `rca_engine` 只调 `run_probes(signal, affected_service)`，不感知 engine 类型
5. ⚠️ **Prompt Caching 默认启用** — 7 个 Agent（1 Orchestrator + 6 Prober）全部启用 `CacheConfig(strategy="auto")`，**不用已 deprecated 的 `cache_prompt`**
6. ⚠️ **Global inference profile** — 使用 `global.anthropic.claude-sonnet-4-6` 或 `global.anthropic.claude-opus-4-7`
7. ⚠️ **Neptune 查询走 `neptune_helpers.py`** — 复用 Module 2 抽取的公共 helper
8. ⚠️ **`rca/collectors/aws_probers.py` 保持可 import** — `from rca.collectors.aws_probers import run_all_probes` 向后兼容
9. ⚠️ **Token usage 必填** — 每个 Prober 独立统计 `cache_read` / `cache_write`，Orchestrator 汇总
10. ⚠️ **assert_cacheable 强制** — 7 个 system prompt 全部通过 `assert_cacheable(prompt, min_tokens=1024)`（按 tokens 算，用 `tiktoken.get_encoding`）
11. ⚠️ **内存上限** — 6 Prober 并行 + Orchestrator 总内存 < 2 GB（通过 `test_layer2_memory.py` 验证）
12. ⚠️ **每个 Prober 设 `max_tokens=4096`** — 避免 MaxTokensReached（Module 1 retro 坑 4）
13. ⚠️ **factory.py import 用 `collectors.*` / `engines.*`** — 不用 `rca.collectors.*` 全限定路径（Module 1 + 2 retro 反复踩坑）

---

## 9. 验证步骤

### 9.1 单元测试（本地，不真调）
```bash
cd rca && PYTHONPATH=. pytest ../tests/test_layer2_golden.py -v -k "not goldenreal"
```

### 9.2 Golden CI（真调 Bedrock + AWS APIs + Neptune，有成本）
```bash
cd rca && PYTHONPATH=. RUN_GOLDEN=1 LAYER2_ENGINE=direct  pytest ../tests/test_layer2_golden.py -v
cd rca && PYTHONPATH=. RUN_GOLDEN=1 LAYER2_ENGINE=strands pytest ../tests/test_layer2_golden.py -v
```

### 9.3 Shadow 对比
```bash
cd rca && PYTHONPATH=. RUN_GOLDEN=1 pytest ../tests/test_layer2_shadow.py -v -s
```
输出：
- 每 case 的异常检测数量差异（direct vs strands）
- 各 Prober 触发率对比
- 延迟倍数 / token 倍数 / 每 Prober 缓存命中率

### 9.4 缓存生效验证

使用公共 harness `experiments/strands-poc/verify_cache_common.py`：

```python
# experiments/strands-poc/verify_cache_layer2.py
from verify_cache_common import run_cache_verification
from rca.engines.factory import make_layer2_engine

engine = make_layer2_engine()

# 验证 Orchestrator 缓存
run_cache_verification(
    engine_factory=make_layer2_engine,
    sample_input={"signal": SAMPLE_SIGNAL, "affected_service": "petsite"},
    method_name="run_probes",
    repeat=3,
)

# 验证每个 Prober 独立缓存
for prober_name in ["cloudwatch", "xray", "neptune", "logs", "deployment", "network"]:
    run_cache_verification(
        engine_factory=make_layer2_engine,
        sample_input={"probe_name": prober_name, "signal": SAMPLE_SIGNAL, "affected_service": "petsite"},
        method_name="run_single_probe",
        repeat=3,
    )
```

### 9.5 内存压力测试
```bash
cd rca && PYTHONPATH=. pytest ../tests/test_layer2_memory.py -v -s
```
验证：6 Prober 并行 + Orchestrator 总内存 < 2 GB。

### 9.6 集成验证
跑一次完整 RCA 流程（event → normalize → Layer1 → **Layer2** → scoring → report），确保：
- `run_probes()` 返回格式兼容 `rca_engine.py`
- `format_probe_results()` 输出格式不变
- `total_score_delta()` 计算逻辑等价
- 下游 `decision_engine.py` 能消费新版输出

---

## 10. 验收门槛（Gate B - Phase 3 Module 3 Week 3 检查）

| 指标 | 门槛 |
|------|------|
| Direct Golden | ≥ 5/6 |
| Strands Golden | ≥ direct - 1 |
| 业务等价性（行为约束） | 所有 case 满足 probers_must_trigger / evidence_must_include_any / score_delta 范围 |
| Shadow 横向对比 | 异常检测数量差异 ≤ 30%（Strands 可能多检测出关联异常） |
| Strands p50 latency | ≤ 2x direct（multi-agent 开销更大，允许更宽松） |
| 稳态缓存命中率（每 Prober） | ≥ 60% |
| 事件高峰期缓存命中率 | ≥ 80% |
| Prompt Caching 成本下降 | ≥ 40%（vs 本模块无缓存 baseline） |
| 6 Prober 并行内存峰值 | < 2 GB |
| 1 周灰度 SEV-1/2 | 0 |
| 集成测试（event → Layer2 → scoring） | 通过 |
| assert_cacheable 检查 | 7 个 system prompt 均 ≥ 1024 tokens |

### Gate B 不达标时的决策矩阵

| 情况 | 路径 A（首选） | 路径 B（降级） |
|------|---------------|---------------|
| direct golden ≥ 5 但 strands golden < direct - 1 | 针对性修未通过的 Prober，重跑 Golden | 缩减到 4 Prober 先上线，剩余 2 个延后 |
| 单个 Prober 缓存命中率 < 60% | 检查该 Prober system prompt 是否达 1024 tokens | 该 Prober 暂不缓存，其余 5 个照常 |
| 内存峰值 > 2 GB | 减少并行度（max_workers=4），最慢 2 个串行 | 重构 Prober Agent 为轻量级（减少 tool schema） |
| Strands latency > 2x direct | 检查 Orchestrator ReAct 轮数是否异常 | 接受 3x 上限，Layer2 不在关键延迟路径上 |
| 集成测试失败 | 检查 `run_probes()` 返回格式兼容性 | 回退到 direct，排查后重试 |

**同一问题 3 次失败 → 停手，写入 progress log，通知大乖乖。**

---

## 11. Git 提交策略

| # | PR | 范围 | 复杂度 |
|---|-----|------|--------|
| 1 | `feat(engines): add Layer2ProberBase + factory` | 抽象层 + factory 扩展 | 低 |
| 2 | `refactor(layer2): extract DirectLayer2Prober from aws_probers.py + shim` | rename + 抽取，无行为变化 | 中 |
| 3 | `feat(layer2): add 6 Prober Agents + Orchestrator + tools` | Strands multi-agent 核心实现 | **高** |
| 4 | `test(layer2): golden set + engine matrix + shadow + memory test` | 测试 + 6 cases | 中 |
| 5 | `feat(layer2): wire factory into rca_engine.py` | 调用方切 factory | 低 |
| 6 | `docs(migration): freeze layer2_direct.py + ADR` | 切换完成 + 冻结标记 | 低 |

> 📝 Module 2 retro 建议"PR 切分按实际复杂度，不强制 7 个"。本模块 6 个 PR 反映了实际复杂度分布：PR3 是核心（6 个 Agent + Orchestrator），PR4 是测试重点，其余轻量。PR3 如果过大可进一步拆分为 3a（Prober Agents）+ 3b（Orchestrator + 接线）。

每个 PR 单独 review。PR6 合并时必须同步更新 `docs/migration/timeline.md`（`status: frozen`）。

---

## 12. 3 周流程

### Week 1
- PR 1 + 2 合并
- PR 3 开发（核心工作量：6 个 Prober Agent + Orchestrator）
- 准备 Golden fixture（6 个 signal）
- ⚠️ 每个 Prober Agent 完成后立即跑 `assert_cacheable` — 不要等 6 个都写完

### Week 2
- PR 3 + 4 合并
- 跑 Golden CI 两个 engine 拿到 baseline 数字
- 跑 `verify_cache_layer2.py` 确认 7 个缓存池全部生效
- 跑 `test_layer2_memory.py` 确认内存在预算内
- PR 5 合并（rca_engine.py 切 factory）
- env `LAYER2_ENGINE=strands` 在灰度节点开启

### Week 3
- 每日 shadow 对比 + 看板监控（每个 Prober 独立看板）
- Gate B 检查所有门槛
- 通过 → PR 6 合并（冻结），打 tag `v-strands-cutover-layer2-YYYYMMDD`
- 未通过 → 延期 1 周并写 ADR；同一模块最多延 3 次

---

## 13. 不要做的事

1. ❌ 不改 `rca/core/rca_engine.py` 的接口（只换内部实现）
2. ❌ 不改已冻结的 HypothesisAgent / LearningAgent
3. ❌ 不用 `agent.as_tool(agent_b)` 嵌套 Strands（用 `@tool` 包装）
4. ❌ 不把 Layer2 和 PolicyGuard 合并迁移
5. ❌ 不跳过 Golden Set 建立（必须 6 个 cases）
6. ❌ 不跳过 Prompt Caching（7 个 Agent 全部启用）
7. ❌ 不跳过内存压力测试
8. ❌ 不用 `apac.*` / `us.*` / 裸 model id
9. ❌ 不在冻结期 `DirectLayer2Prober` 里加新功能
10. ❌ 不自行决定延期删除日（必须走 ADR）
11. ❌ 不用 `cache_prompt="default"`（已 deprecated） → 用 `CacheConfig(strategy="auto")`
12. ❌ 不用 chars 估算缓存下限 → 用 `assert_cacheable(prompt, min_tokens=1024)` + `tiktoken.get_encoding`
13. ❌ 不用 `rca.collectors.*` 全限定 import → 用 `collectors.*`
14. ❌ 不在 `@tool` 函数体内创建嵌套 Strands Agent

---

## 14. 失败处理

- **SIGKILL（最可能的失败模式）** → 首先检查是否违反了嵌套 Agent 硬规则（§ 6.2）；其次检查总内存是否超 cgroup 限制；最后检查是否有 Prober 的 tool 内偷偷调了 Strands engine
- **单个 Prober Golden 回归** → 检查该 Prober 的 system prompt 是否丢了关键规则；Strands Prober 的 ReAct 可能主动查更多数据源导致行为差异
- **缓存命中率不均匀（部分 Prober 高、部分低）** → 检查低命中率 Prober 的 system prompt token 数；某些 Prober（如 Neptune）的 system prompt 可能因 schema 较短不达标
- **Orchestrator ReAct 轮数过多** → 检查 system prompt 是否清晰描述了 Prober 职责划分；如果 Orchestrator "犹豫不决"反复调同一 Prober，需要在 system prompt 加明确的调度规则
- **sample_for_golden 脚本挂了** → 检查 incremental save + resume 是否实现（§ 7.7.3）
- **同一问题 3 次失败** → 停手，写入 progress log，通知大乖乖

---

## 15. RCA Layer2 特殊点备忘

### 15.1 迁移范围大

6 个 Probe 类 + 并行调度 + Orchestrator 汇总 = Phase 3 最复杂的迁移。**不要尝试一次性写完全部 Prober**，建议顺序：
1. 先做 CloudWatch Prober（最简单，基于 CW Metrics API）
2. 再做 Logs Prober（CW Logs Insights，有现成查询模板）
3. 再做 Neptune Prober（复用 neptune_helpers.py）
4. 然后 Deployment / Network / X-Ray（复杂度递增）
5. 最后做 Orchestrator（等所有 Prober 就绪后）

### 15.2 与 Direct 版的关键差异

Direct 版（`aws_probers.py`）每个 Probe 是纯 Python + boto3 调用，**没有 LLM 调用**。Strands 版的增值点是：
- 每个 Prober Agent 可以 **多轮 ReAct 深入探查**（如 CloudWatch Prober 发现 SQS 积压后，进一步查 DLQ 消息内容）
- Orchestrator 可以做 **跨 Prober 关联分析**（如 Deployment rollback + Logs 错误飙升 → 关联为"部署导致"）
- 但这也意味着 **Strands 版会显著更慢更贵**（6 个 Agent × 2-4 ReAct cycles × LLM 调用）

### 15.3 成本预估

```
Direct 版：6 个 Prober × 0 LLM call = $0（纯 API 调用）
Strands 版：
  6 Prober × 2-3 ReAct cycles × ~$0.03/call = $0.36-0.54/事件
  1 Orchestrator × 1-2 cycles × ~$0.05/call = $0.05-0.10/事件
  总计 ≈ $0.4-0.6/事件

月度（假设 100 事件/月）：
  Strands 无缓存: ~$50/月
  Strands 有缓存（50-60% 节省）: ~$20-25/月
```

### 15.4 Golden Set 用历史事件

Golden Set 基于 RCA 历史事件快照（fixture 文件），不是实时查 AWS API。这避免了：
- 测试结果受实时基础设施状态影响
- 每次 Golden 都产生 AWS API 成本（CloudWatch / X-Ray / Logs 查询有费用）
- 但 fixture 需要包含足够多的 AWS API mock 数据供 Prober 消费

### 15.5 Strands 建议质量提升点

Direct 版 Prober 只做"有没有异常"的二元判断。Strands 版可以做：
- **异常归因**：不只说"SQS 积压"，还说"因为消费者 Pod crash 导致消费停滞"
- **跨 Prober 关联**：把 CloudWatch 的 SQS 异常 + Logs 的 OOM + Deployment 的 Pod restart 串联起来
- **建议下一步**：基于异常模式建议排查方向

这是 Strands 版的核心增值，也是 Golden Set 中 `summary_must_include_any` 约束的来源。

---

## 16. factory.py import 路径确认检查项

> Module 1 + Module 2 retro 反复踩 import 路径的坑。本模块在以下节点强制检查：

| 检查点 | 文件 | 检查内容 |
|--------|------|---------|
| PR1 提交前 | `rca/engines/factory.py` | 新增 `make_layer2_engine()` 的 import 用 `from collectors.layer2_direct import ...`，不用 `rca.collectors.*` |
| PR3 提交前 | `rca/collectors/layer2_tools.py` | 所有 import 用 `from collectors.xxx` 或 `from engines.xxx`，不用全限定 |
| PR3 提交前 | `rca/collectors/prober_agents/*.py` | 同上 |
| PR5 提交前 | `rca/core/rca_engine.py` | 调 `make_layer2_engine()` 的 import 路径 |
| 每个 PR | `PYTHONPATH` | 确认 `PYTHONPATH=rca` 或 `cd rca && PYTHONPATH=.` |

---

## 17. 参考资料

- `rca/collectors/aws_probers.py` — 现版 6 个 Probe 实现（迁移源）
- `rca/core/rca_engine.py` — Layer2 调用入口
- `chaos/code/agents/hypothesis_strands.py` — Module 1 Strands 实现参考
- `chaos/code/agents/learning_strands.py` — Module 2 Strands 实现参考
- `chaos/code/runner/neptune_helpers.py` — Neptune 查询公共 helper（Module 2 产出）
- `rca/engines/strands_common.py` — BedrockModel + OTel helper
- `experiments/strands-poc/retros/learning-retro.md` — Module 2 retrospective（**必读**）
- `experiments/strands-poc/retros/hypothesis-retro.md` — Module 1 retrospective（**必读**）
- AWS Strands Agents 文档：<https://strandsagents.com/>
- Bedrock Prompt Caching 文档：<https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html>

---

## 18. 完成标志

1. ✅ 6 个 PR 全部合并 main
2. ✅ 9.1-9.6 验证步骤全部通过
3. ✅ 10 验收门槛全部达标（或按决策矩阵处理）
4. ✅ `BASELINE-direct.md` + `BASELINE-strands.md` 入仓
5. ✅ `docs/migration/timeline.md` 更新 `rca-layer2-probers.status: frozen`
6. ✅ tag `v-strands-cutover-layer2-YYYYMMDD` 已打
7. ✅ `docs/migration/decisions/layer2-migration-adr.md` 落地
8. ✅ `report.md § 11` 写完，含延迟/成本/每 Prober 缓存命中率数据
9. ✅ `rca_engine.py` 成功调用 `make_layer2_engine()` 获取 strands 实例，下游 scoring/decision 消费无异常
10. ✅ 7 个 system prompt 全部通过 `assert_cacheable` 检查
11. ✅ 内存压力测试通过（< 2 GB）
12. ✅ **Retrospective 已交**：`experiments/strands-poc/retros/layer2-retro.md`，重点交付 § 6 Top 3 + § 7 模板建议（给 PolicyGuard TASK 用）
13. ✅ sessions_send 给架构审阅猫一条 `[RETRO] layer2-probers 完成，Top 3: ...` 消息

---

**下一个模块**：Chaos PolicyGuard（Phase 3 Week 13-15），TASK 文件在本模块完成后才发布。

**⚠️ timeline.md 注意**：`direct_file: rca/probers/layer2_direct.py` 是占位路径，实际代码在 `rca/collectors/aws_probers.py`（迁移后拆分为 `rca/collectors/layer2_direct.py`）。PR1 合并前必须修正 timeline.md。

**遇到任何不确定的地方，先在 Slack 里问大乖乖或架构审阅猫，不要自己拍脑袋决定。**
