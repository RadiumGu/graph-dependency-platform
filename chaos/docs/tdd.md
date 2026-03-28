# 📐 Technical Design Document (TDD)
## Chaos Engineering Automation Platform

**版本**: 0.7  
**日期**: 2026-03-13  
**作者**: 爱吃肉  
**状态**: Draft  
**关联 PRD**: `docs/prd.md`  
**代码仓库**: https://github.com/RadiumGu/chaos-automation  
**工作目录**: `/home/ubuntu/tech/chaos/`

---

## 1. 系统架构

### 1.1 整体架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                    chaos-automation                                  │
│                                                                     │
│  ┌──────────────┐    ┌──────────────────────────────────────────┐  │
│  │ FMEA Generator│    │            Experiment Runner             │  │
│  │              │    │                                          │  │
│  │ Neptune ──→  │    │  Phase0      Phase1      Phase2          │  │
│  │ DeepFlow ──→ │    │  Preflight → SteadyState → FaultInject   │  │
│  │ FMEA Table   │    │                              ↓           │  │
│  └──────────────┘    │  Phase3          Phase4      │           │  │
│                      │  Observation+  → Recovery  → Phase5      │  │
│  ┌──────────────┐    │  Guardrails ↓               SteadyState  │  │
│  │ Experiment   │    │  Stop Condition              After        │  │
│  │ YAML Library │──→ │  → abort()              ↓  Report Gen    │  │
│  └──────────────┘    └──────────────┬───────────────────────────┘  │
│         ▲                           │                               │
│         │                ┌──────────┴──────────┐                   │
│  ┌──────┴───────────┐   │   FaultInjector      │                   │
│  │ Template          │   │   (Abstract Layer)   │                   │
│  │ Generator         │   └──┬──────────────┬────┘                  │
│  │ (LLM Agent)       │      │              │                        │
│  │                   │      │              │                        │
│  │ ┌─────────────┐  │      │              │                        │
│  │ │Chaosmesh-MCP│  │      │              │                        │
│  │ │(生成+验证)  │  │      │              │                        │
│  │ ├─────────────┤  │      │              │                        │
│  │ │aws-api-mcp  │  │      │              │                        │
│  │ │(FIS 生成+   │  │      │              │                        │
│  │ │ API 验证)   │  │      │              │                        │
│  │ └─────────────┘  │      │              │                        │
│  └──────────────────┘      │              │                        │
│                             │              │                        │
└─────────────────────────────│──────────────│────────────────────────┘
                              │              │
          ┌───────────────────┤              ├──────────────────────┐
          ▼                   │              ▼                      │
  ┌───────────────┐   ┌──────┴───────┐  ┌──────────────────┐      │
  │ Chaosmesh-MCP │   │  AWS FIS     │  │   DeepFlow API   │      │
  │ fault_inject  │   │  (boto3)     │  │  (11.0.2.30:     │      │
  │ (执行)        │   │  (执行)      │  │   20416)         │      │
  │ K8s Pod/容器  │   │ Lambda 故障  │  │                  │      │
  │ 网络/HTTP/IO  │   │ RDS failover │  │ success_rate     │      │
  │ 时间/内核     │   │ EC2/EBS/Node │  │ latency_p99      │      │
  │ ...24 types   │   │ VPC 网络中断 │  │ error_rate       │      │
  └───────────────┘   │ API 限流注入 │  └──────────────────┘      │
          │           │ CW Alarms    │           │                 │
          ▼           └──────────────┘           │                 │
  ┌───────────────┐           │                  │    ┌────────────┴───┐
  │  Chaos Mesh   │           ▼                  │    │  AWS Services  │
  │  EKS Cluster  │   ┌──────────────┐           │    │                │
  │  ap-northeast │   │ FIS 实验模板 │           │    │ ┌────────────┐ │
  │  default ns   │   │ (AWS 托管)   │           │    │ │  DynamoDB  │ │
  └───────────────┘   │ Stop Cond:   │           │    │ │chaos-exper-│ │
                      │ CW Alarm ARN │◀──────────┘    │ │iments     │ │
                      └──────────────┘    观测         │ └────────────┘ │
                                                      │ ┌────────────┐ │
                                                      │ │  Lambda    │ │
                                                      │ │petsite-rca-│ │
                                                      │ │engine      │ │
                                                      │ └────────────┘ │
                                                      │ ┌────────────┐ │
                                                      │ │ CloudWatch │ │
                                                      │ │FIS Stop    │ │
                                                      │ │Conditions  │ │
                                                      │ └────────────┘ │
                                                      │ ┌────────────┐ │
                                                      │ │  S3 (可选) │ │
                                                      │ │Bedrock KB  │ │
                                                      │ └────────────┘ │
                                                      └────────────────┘

  隔离边界：chaos-* 命名的 AWS 资源与 PetSite 业务资源（ServicesEks2-*）完全独立
  工具边界：Chaos Mesh 负责 K8s 层，FIS 负责 AWS 托管服务 + 基础设施层
  生成/执行分离：模板生成通过 MCP（LLM Agent + API 验证），执行通过确定性路径（boto3/Chaosmesh-MCP）
```

### 1.2 目录结构

```
/home/ubuntu/tech/chaos/
├── Chaosmesh-MCP/              ← 已有，故障注入 MCP Server（独立 git）
│                                  github.com/RadiumGu/Chaosmesh-MCP
├── code/                       ← chaos-automation 主代码（本 TDD 范围）
│   ├── runner/
│   │   ├── __init__.py
│   │   ├── experiment.py       ← Experiment 数据模型（YAML 解析 + Schema 验证）
│   │   ├── runner.py           ← 主执行引擎（5 Phase）
│   │   │                          含 Phase3 观测循环内嵌 Stop Conditions 逻辑
│   │   │                          注：时间到自动停由 Chaos Mesh duration / FIS duration 负责
│   │   │                              指标超阈值提前熔断在此实现
│   │   ├── fault_injector.py   ← 故障注入抽象层（统一接口，按 backend 分派）
│   │   ├── chaosmesh_backend.py ← Chaos Mesh 后端（K8s Pod/容器/网络/IO/时间/内核）
│   │   ├── fis_backend.py      ← AWS FIS 后端（Lambda/RDS/EC2/EBS/网络基础设施）
│   │   ├── metrics.py          ← DeepFlow Querier API 指标查询
│   │   ├── query.py            ← DynamoDB 查询层（所有读操作统一入口，禁止 Scan）
│   │   ├── rca.py              ← RCA Engine（petsite-rca-engine Lambda）调用
│   │   ├── report.py           ← 报告生成（Markdown）+ DynamoDB 写入
│   │   └── graph_feedback.py   ← Neptune 图谱反馈（依赖强弱写回 Calls/DependsOn 等边）
│   ├── fmea/
│   │   └── fmea.py             ← FMEA 生成器（Neptune 图谱 + DynamoDB 历史数据）
│   ├── gen_template.py         ← Neptune 智能场景生成器（F8，2026-03-12 新增）
│   │                              交互式 / 命令行模式，自动从 Neptune 读取 Tier + 调用关系
│   │                              动态推算阈值 + Stop Conditions，生成 YAML 模板
│   ├── experiments/            ← 预置实验 YAML 库（Git 版本管理，不上 S3）
│   │   ├── tier0/
│   │   │   ├── petsite-pod-kill.yaml
│   │   │   ├── petsearch-pod-kill.yaml
│   │   │   └── payforadoption-pod-kill.yaml
│   │   ├── tier1/
│   │   │   └── petlistadoptions-pod-kill.yaml
│   │   ├── network/
│   │   │   ├── petsite-network-delay.yaml
│   │   │   └── petsearch-network-loss.yaml
│   │   └── fis/                ← FIS 专用实验（AWS 托管服务 + 基础设施层）
│   │       ├── lambda/
│   │       │   ├── fis-lambda-delay-petstatusupdater.yaml
│   │       │   └── fis-lambda-error-petadoptionshistory.yaml
│   │       ├── rds/
│   │       │   ├── fis-aurora-failover.yaml
│   │       │   └── fis-aurora-reboot.yaml
│   │       ├── eks-node/
│   │       │   ├── fis-eks-terminate-node-1a.yaml
│   │       │   └── fis-eks-terminate-node-1c.yaml
│   │       └── network-infra/
│   │           ├── fis-network-disrupt-az-1a.yaml
│   │           └── fis-ebs-io-latency.yaml
│   ├── infra/
│   │   ├── dynamodb_setup.py   ← chaos-experiments 表建表脚本（一次性执行）
│   │   └── fis_setup.py        ← FIS IAM Role + CloudWatch Alarm 创建脚本
│   └── main.py                 ← CLI 入口
├── docs/
│   ├── prd.md                  ← 产品需求文档
│   └── tdd.md                  ← 本文件（技术设计文档）
└── validation-results/         ← 实验报告 + 执行日志输出目录（已有）
```

**模块职责说明**：

| 模块 | 职责 | Stop Condition 角色 |
|------|------|-------------------|
| `runner.py` | 5 Phase 流程编排 | Phase3 观测循环内嵌指标熔断逻辑 |
| `fault_injector.py` | 故障注入抽象层，按 `backend` 分派到 chaosmesh 或 fis | — |
| `chaosmesh_backend.py` | Chaos Mesh 后端：通过 Chaosmesh-MCP 间接调用 K8s 故障注入 | 到期由 Chaos Mesh duration 自动停 |
| `fis_backend.py` | AWS FIS 后端：通过 boto3 直接调用 FIS API（确定性执行路径）；已实现全部 15 种 fault type | FIS 原生 Stop Conditions (CloudWatch Alarm) |
| `experiment.py` | YAML 解析、数据模型、Schema 验证 | 解析 `stop_conditions` 和 `backend` 配置 |
| `metrics.py` | DeepFlow 指标采集 | 提供实时指标供 runner 判断 |
| `query.py` | DynamoDB 所有读操作统一入口（只用 Query 走 GSI，禁止 Scan）| — |
| `report.py` | Markdown 报告生成 + DynamoDB 写入 + Bedrock LLM 分析结论 | 记录熔断原因（`abort_reason`）|
| `rca.py` | Lambda RCA 调用 + 结果验证 | — |
| `fmea.py` | Neptune + DynamoDB 驱动的 FMEA 生成 | — |
| `gen_template.py` | Neptune 驱动的智能场景生成器（F8）；交互式或 CLI 模式，自动填充 Tier 阈值 + Stop Conditions | — |

> ⚠️ **`guardrails.py` 已移除**：Stop Conditions 逻辑内嵌在 `runner.py` Phase3 观测循环中。
> Chaos Mesh 的 `duration` 字段 / FIS 的实验时长负责到期自动停；我们只处理指标触发的提前熔断。
> FIS 实验额外拥有原生 CloudWatch Alarm Stop Conditions，作为双重保险。

---

## 2. 数据存储设计

### 2.1 DynamoDB 表设计

> **隔离原则**：混沌工程表统一使用 `chaos-` 前缀，与 PetSite 业务表（`ServicesEks2-*`）完全隔离，互不影响。

#### 2.1.1 主表：`chaos-experiments`

**用途**：每次实验执行的完整记录，支持历史查询、趋势分析、FMEA Probability 计算。

**Key 设计**：
```
PK (HASH):  experiment_id   String
SK (RANGE): start_time      String (ISO8601)
```

**完整 Schema**：

| 字段名 | 类型 | 必填 | 说明 |
|--------|------|------|------|
| `experiment_id` | S | ✅ PK | `exp-{service}-{fault_type}-{yyyyMMdd-HHmmss}` |
| `start_time` | S | ✅ SK | ISO8601，e.g. `2026-03-04T14:30:12+08:00` |
| `experiment_name` | S | ✅ | YAML 中定义的实验名 |
| `target_service` | S | ✅ | 目标服务名，e.g. `petsite` |
| `target_namespace` | S | ✅ | K8s namespace，e.g. `default` |
| `target_tier` | S | ✅ | `Tier0` / `Tier1` / `Tier2` |
| `fault_type` | S | ✅ | `pod_kill` / `network_delay` / ... |
| `fault_mode` | S | ✅ | `fixed-percent` / `all` / `one` / ... |
| `fault_value` | S | ✅ | 注入参数值，e.g. `50` |
| `fault_duration` | S | ✅ | 注入持续时长，e.g. `2m` |
| `status` | S | ✅ | `PASSED` / `FAILED` / `ABORTED` / `ERROR` |
| `abort_reason` | S | — | Stop Condition 触发时记录熔断原因 |
| `end_time` | S | ✅ | 实验结束时间 ISO8601 |
| `duration_seconds` | N | ✅ | 实验总耗时（秒） |
| `steady_state_before` | M | ✅ | 注入前稳态快照 `{success_rate, latency_p99}` |
| `steady_state_after` | M | ✅ | 恢复后稳态快照 |
| `impact_min_success_rate` | N | ✅ | 实验期间成功率最低值（%），FMEA 概率计算来源 |
| `impact_max_latency_p99` | N | ✅ | 实验期间延迟 p99 最高值（ms）|
| `recovery_seconds` | N | — | 从恢复操作到稳态的时长（秒）|
| `rca_enabled` | BOOL | ✅ | 是否触发了 RCA |
| `rca_expected` | S | — | 期望根因服务名 |
| `rca_actual` | S | — | RCA 给出的实际根因 |
| `rca_confidence` | N | — | RCA 置信度（0.0-1.0）|
| `rca_match` | BOOL | — | RCA 是否命中期望根因 |
| `report_path` | S | ✅ | 本地报告文件路径 |
| `yaml_source` | S | ✅ | 实验 YAML 来源文件路径 |
| `ttl` | N | — | Unix timestamp，默认 90 天后过期 |

**Partition Key 命名规范**：
```
experiment_id = "exp-{service}-{fault_type}-{yyyyMMdd-HHmmss}"
示例：
  exp-petsite-pod-kill-20260304-143012
  exp-petsearch-network-delay-20260304-160000
  exp-payforadoption-pod-failure-20260305-090000
```

---

#### 2.1.2 GSI（全局二级索引）

**GSI-1：`target_service-start_time-index`**
```
用途：查询某服务的全部历史实验，按时间排序
HASH:  target_service
RANGE: start_time
典型查询：petsite 最近 10 次实验记录
```

**GSI-2：`status-start_time-index`**
```
用途：查询所有 FAILED / ABORTED 的实验（护栏触发分析）
HASH:  status
RANGE: start_time
典型查询：本月所有被熔断的实验
```

**GSI-3：`experiment_name-start_time-index`**
```
用途：同一实验名的历史趋势对比
HASH:  experiment_name
RANGE: start_time
典型查询：petsite-pod-kill-tier0 历次结果，计算 FMEA Probability
```

---

#### 2.1.3 与现有 PetSite 表的隔离验证

```
现有 PetSite 表：ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM
  HASH: pettype (S)
  RANGE: petid (S)
  用途：宠物领养业务数据

混沌工程表：chaos-experiments
  HASH: experiment_id (S)
  RANGE: start_time (S)
  用途：混沌实验执行记录

隔离方式：
  1. 命名不重叠（chaos-* vs ServicesEks2-*）
  2. IAM Policy 独立：chaos-automation runner 只有 chaos-* 表的读写权限
  3. Region 相同（ap-northeast-1），但资源完全独立
```

---

#### 2.1.4 建表规格

```python
# infra/dynamodb_setup.py 建表参数
{
    "TableName": "chaos-experiments",
    "BillingMode": "PAY_PER_REQUEST",   # 按需计费，实验量少，成本极低
    "KeySchema": [
        {"AttributeName": "experiment_id", "KeyType": "HASH"},
        {"AttributeName": "start_time",    "KeyType": "RANGE"},
    ],
    "AttributeDefinitions": [
        {"AttributeName": "experiment_id",   "AttributeType": "S"},
        {"AttributeName": "start_time",      "AttributeType": "S"},
        {"AttributeName": "target_service",  "AttributeType": "S"},
        {"AttributeName": "status",          "AttributeType": "S"},
        {"AttributeName": "experiment_name", "AttributeType": "S"},
    ],
    "GlobalSecondaryIndexes": [
        {
            "IndexName": "target_service-start_time-index",
            "KeySchema": [
                {"AttributeName": "target_service", "KeyType": "HASH"},
                {"AttributeName": "start_time",     "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "status-start_time-index",
            "KeySchema": [
                {"AttributeName": "status",     "KeyType": "HASH"},
                {"AttributeName": "start_time", "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
        {
            "IndexName": "experiment_name-start_time-index",
            "KeySchema": [
                {"AttributeName": "experiment_name", "KeyType": "HASH"},
                {"AttributeName": "start_time",      "KeyType": "RANGE"},
            ],
            "Projection": {"ProjectionType": "ALL"},
        },
    ],
    "TimeToLiveSpecification": {
        "AttributeName": "ttl",
        "Enabled": True,
    },
}
```

---

### 2.2 本地文件存储

| 内容 | 路径 | 格式 |
|------|------|------|
| 实验 YAML 定义 | `code/experiments/**/*.yaml` | YAML |
| 实验执行报告 | `validation-results/{experiment_id}-report.md` | Markdown |
| 执行日志 | `validation-results/{experiment_id}.log` | 纯文本 |

> 实验 YAML 不存 S3，Git 是唯一版本管理来源。

---

## 3. 核心模块设计

### 3.1 Experiment 数据模型（`experiment.py`）

```python
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class SteadyStateCheck:
    metric: str          # "success_rate" | "latency_p99" | "error_rate"
    threshold: str       # ">= 99%" | "< 5000ms"
    window: str          # "1m" | "30s"

@dataclass
class StopCondition:
    metric: str
    threshold: str       # "< 50%" | "> 5000ms"
    window: str
    action: str = "abort"
    cloudwatch_alarm_arn: Optional[str] = None  # FIS 原生 Stop Condition（CloudWatch Alarm ARN）

@dataclass
class FaultSpec:
    type: str            # "pod_kill" | "network_delay" | "fis_lambda_delay" | ...
    mode: str            # "fixed-percent" | "all" | "one" (Chaos Mesh 用)
    value: str           # "50" (Chaos Mesh 用)
    duration: str        # "2m"
    # 可选参数（根据 fault_type 不同）
    latency: Optional[str] = None
    loss: Optional[str] = None
    container_names: Optional[list] = None
    extra_params: Optional[dict] = None  # FIS 专属参数（function_arn, cluster_arn 等）

@dataclass
class RcaSpec:
    enabled: bool = False
    trigger_after: str = "30s"          # 注入后多久触发
    expected_root_cause: Optional[str] = None

@dataclass
class Experiment:
    name: str
    description: str
    target_service: str
    target_namespace: str
    target_tier: str
    fault: FaultSpec
    steady_state_before: list[SteadyStateCheck]
    steady_state_after: list[SteadyStateCheck]
    stop_conditions: list[StopCondition]
    backend: str = "chaosmesh"              # "chaosmesh" | "fis"
    backend: str = "chaosmesh"              # "chaosmesh" | "fis"
    rca: RcaSpec = field(default_factory=RcaSpec)
    max_duration: str = "10m"               # 超时保护
    save_to_bedrock_kb: bool = False
```

---

### 3.2 执行引擎（`runner.py`）

```python
class ExperimentRunner:
    """
    5 Phase 执行流程：
    Phase 0: Pre-flight Check（按 backend 分派：Chaos Mesh 检查残留 + Pod 健康 / FIS 检查服务可用）
    Phase 1: Steady State Before
    Phase 2: Fault Injection（按 backend 分派：ChaosMCPClient / FISClient）
    Phase 3: Observation + Guardrails（按 backend 分派熔断：ChaosMesh delete / FIS stop_experiment）
    Phase 4: Fault Recovery（FIS: 先等 wait_for_completion + 清理模板；ChaosMesh: duration 到期自动恢复）
    Phase 5: Steady State After + Report + LLM 分析
    """

    def run(self, experiment: Experiment) -> ExperimentResult:
        result = ExperimentResult(experiment)
        try:
            self._phase0_preflight(experiment, result)
            self._phase1_steady_state_before(experiment, result)
            self._phase2_inject(experiment, result)
            self._phase3_observe(experiment, result)     # 含 Guardrails
            self._phase4_recover(experiment, result)
            self._phase5_steady_state_after(experiment, result)
        except AbortException as e:
            result.status = "ABORTED"
            result.abort_reason = str(e)
            self._emergency_cleanup(result.chaos_experiment_name)
        except Exception as e:
            result.status = "ERROR"
            self._emergency_cleanup(result.chaos_experiment_name)
            raise
        finally:
            self._save_report(result)       # 无论成功失败都生成报告
            self._save_to_dynamodb(result)  # 无论成功失败都写 DynamoDB
        return result
```

---

### 3.3 Stop Conditions（内嵌于 runner.py）

> **设计决策**：Stop Conditions 逻辑直接内嵌在 Phase3 观测循环中，不单独抽 `guardrails.py`。
>
> - **时间到自动停**：由 Chaos Mesh `duration` 字段原生负责，无需我们处理
> - **指标超阈值熔断**：在 Phase3 循环内判断，触发则调用 `delete_experiment()` 提前终止

```python
def _phase3_observe(self, experiment: Experiment, result: ExperimentResult):
    """
    Phase3: 观测 + Stop Conditions 检查
    Chaos Mesh duration 负责到期自动停；
    我们只处理"指标超阈值 → 提前熔断"这一个场景。
    """
    end_time = time.time() + parse_duration(experiment.fault.duration)
    rca_triggered = False

    while time.time() < end_time:
        snapshot = self.metrics.collect(
            service=experiment.target_service,
            namespace=experiment.target_namespace,
        )
        result.record_snapshot(snapshot)

        # Stop Condition 检查（指标触发提前熔断）
        for cond in experiment.stop_conditions:
            if cond.is_triggered(snapshot):
                logger.error(f"🚨 Stop Condition triggered: {cond.describe(snapshot)}")
                self.fault_injector.delete(result.chaos_experiment_name)
                raise AbortException(cond.describe(snapshot))

        # RCA 触发（仅一次）
        if (experiment.rca.enabled
                and not rca_triggered
                and result.elapsed_since_injection() >= parse_duration(experiment.rca.trigger_after)):
            self._trigger_rca(experiment, result)
            rca_triggered = True

        time.sleep(10)
```

**StopCondition 数据类**（定义在 `experiment.py`）：

```python
@dataclass
class StopCondition:
    metric: str        # "success_rate" | "latency_p99" | "error_rate"
    threshold: str     # "< 50%" | "> 5000ms"
    window: str        # "30s"（预留，当前按单次快照判断）
    action: str = "abort"

    def is_triggered(self, snapshot: MetricsSnapshot) -> bool:
        value = snapshot.get(self.metric)
        op, threshold = self._parse_threshold(self.threshold)
        return op(value, threshold)

    def describe(self, snapshot: MetricsSnapshot) -> str:
        value = snapshot.get(self.metric)
        return f"{self.metric}={value} 满足停止条件 {self.threshold}"
```

---

### 3.3a 故障注入抽象层（`fault_injector.py`）

> **设计原则**：Runner 不直接依赖 Chaos Mesh 或 FIS，而是通过统一的 `FaultInjector` 接口注入故障。
> YAML 中 `backend` 字段决定走哪个后端，Runner 无需关心底层实现差异。
>
> **后端实现差异**（对 Runner 透明）：
> - **ChaosMesh 后端**：通过 `Chaosmesh-MCP`（MCP Server）间接调用 `fault_inject.py` 中的故障函数
> - **FIS 后端**：通过 `boto3` 直接调用 AWS FIS API（`create_experiment_template` → `start_experiment`）
> - 两种实现对 Runner 统一，由 `FaultInjector` 接口约束
>
> **模板生成 vs 执行分离**（见 ADR-003）：
> - **生成阶段**：LLM Agent 可通过 `aws-api-mcp-server`（FIS）和 `Chaosmesh-MCP`（K8s）交互式构建模板，利用 API 级验证确保准确性
> - **执行阶段**：Runner 拿到已验证的 YAML 模板，确定性执行，不依赖 LLM 可用性
> - **紧急熔断**：FIS 侧 boto3 直调 + CloudWatch Alarm 原生 Stop Conditions 双重保险，不经过 MCP/LLM 链路

```python
from abc import ABC, abstractmethod
from typing import Optional

class FaultInjector(ABC):
    """故障注入统一接口"""

    @abstractmethod
    def inject(self, experiment: Experiment) -> InjectionResult:
        """注入故障，返回实验标识（Chaos Mesh experiment name 或 FIS experiment ID）"""
        ...

    @abstractmethod
    def delete(self, experiment_ref: str) -> None:
        """强制清理故障（Stop Condition 触发 / emergency cleanup）"""
        ...

    @abstractmethod
    def status(self, experiment_ref: str) -> str:
        """查询实验状态：running / completed / stopped / failed"""
        ...

    @abstractmethod
    def preflight_check(self) -> bool:
        """后端健康检查（Phase 0 使用）"""
        ...


class InjectionResult:
    experiment_ref: str    # Chaos Mesh experiment name 或 FIS experiment ID
    backend: str           # "chaosmesh" | "fis"
    start_time: str        # ISO8601
    expected_duration: str  # "2m" / "PT2M"


def create_injector(backend: str) -> FaultInjector:
    """工厂方法，按 backend 配置创建对应的注入器"""
    if backend == "chaosmesh":
        from .chaosmesh_backend import ChaosMeshBackend
        return ChaosMeshBackend()
    elif backend == "fis":
        from .fis_backend import FISBackend
        return FISBackend()
    else:
        raise ValueError(f"Unknown backend: {backend}")
```

**Runner 中的使用方式**：

```python
# runner.py Phase2
def _phase2_inject(self, experiment, result):
    injector = create_injector(experiment.backend)
    injection = injector.inject(experiment)
    result.experiment_ref = injection.experiment_ref
    result.backend = injection.backend
```

---

### 3.3b Chaos Mesh 后端（`chaosmesh_backend.py`）

```python
class ChaosMeshBackend(FaultInjector):
    """
    现有 Chaosmesh-MCP 封装，调用 fault_inject.py 中的各故障函数
    覆盖：pod_kill / pod_failure / container_kill / network_* / http_chaos /
          dns_chaos / io_chaos / time_chaos / kernel_chaos / *_stress
    """

    def inject(self, experiment: Experiment) -> InjectionResult:
        # 调用 Chaosmesh-MCP 对应的故障函数
        func = getattr(self.mcp, experiment.fault.type)
        result = func(
            service_name=experiment.target_service,
            namespace=experiment.target_namespace,
            mode=experiment.fault.mode,
            value=experiment.fault.value,
            duration=experiment.fault.duration,
            **experiment.fault.extra_params,
        )
        return InjectionResult(
            experiment_ref=result["experiment_name"],
            backend="chaosmesh",
            start_time=datetime.now(UTC).isoformat(),
            expected_duration=experiment.fault.duration,
        )

    def delete(self, experiment_ref: str) -> None:
        self.mcp.delete_experiment(experiment_name=experiment_ref)

    def status(self, experiment_ref: str) -> str:
        # Chaos Mesh 实验到期自动结束，通过 K8s API 查 CR 状态
        ...

    def preflight_check(self) -> bool:
        return self.mcp.health_check()["status"] == "ok"
```

---

### 3.3c AWS FIS 后端（`fis_backend.py`）

> **已实现**（v0.7）。接口与 `ChaosMCPClient` 对齐，Runner 按 `experiment.backend` 字段切换。

```python
import boto3
import json
import time
from datetime import datetime, timezone

UTC = timezone.utc

# fault.type → FIS actionId 映射
FIS_ACTION_MAP = {
    "fis_lambda_delay":         "aws:lambda:invocation-add-delay",
    "fis_lambda_error":         "aws:lambda:invocation-error",
    "fis_lambda_http_response": "aws:lambda:invocation-http-integration-response",
    "fis_rds_failover":         "aws:rds:failover-db-cluster",
    "fis_rds_reboot":           "aws:rds:reboot-db-instances",
    "fis_eks_terminate_node":   "aws:eks:terminate-nodegroup-instances",
    "fis_ec2_stop":             "aws:ec2:stop-instances",
    "fis_ec2_terminate":        "aws:ec2:terminate-instances",
    "fis_ebs_pause_io":         "aws:ebs:pause-volume-io",
    "fis_ebs_io_latency":       "aws:ebs:volume-io-latency",
    "fis_network_disrupt":      "aws:network:disrupt-connectivity",
    "fis_vpc_endpoint_disrupt": "aws:network:disrupt-vpc-endpoint",
    "fis_api_internal_error":   "aws:fis:inject-api-internal-error",
    "fis_api_throttle":         "aws:fis:inject-api-throttle-error",
    "fis_api_unavailable":      "aws:fis:inject-api-unavailable-error",
}

class FISClient:
    """
    AWS FIS 故障注入客户端（确定性执行路径，boto3 直调）。
    负责：Lambda 故障 / RDS failover / EC2/EBS 故障 / 网络基础设施 / API 注入
    """

    REGION = "ap-northeast-1"
    FIS_ROLE_ARN = "arn:aws:iam::926093770964:role/chaos-fis-experiment-role"

    def __init__(self):
        self.fis = boto3.client("fis", region_name=self.REGION)

    def inject(self, experiment: Experiment) -> dict:
        """
        1. 创建 FIS 实验模板
        2. 配置 Stop Conditions（CloudWatch Alarm ARN）
        3. 启动实验
        返回 {"experiment_id": ..., "template_id": ...}
        """
        ft = experiment.fault
        extra = ft.extra_params or {}
        action_id = FIS_ACTION_MAP.get(ft.type)

        # 构建 action + target + stop conditions
        action = {"actionId": action_id, "parameters": {...}, "targets": {...}}
        target = self._build_target(ft.type, extra)
        stop_conditions = [{"source": "aws:cloudwatch:alarm", "value": arn}
                           for sc in experiment.stop_conditions
                           if (arn := sc.cloudwatch_alarm_arn)]

        # 创建模板 + 启动实验
        template = self.fis.create_experiment_template(...)
        template_id = template["experimentTemplate"]["id"]
        exp = self.fis.start_experiment(experimentTemplateId=template_id, ...)
        experiment_id = exp["experiment"]["id"]

        return {"experiment_id": experiment_id, "template_id": template_id}

    def stop(self, experiment_id: str) -> None:
        """停止 FIS 实验（紧急熔断 — 最短路径，不经过 LLM/MCP）"""
        self.fis.stop_experiment(id=experiment_id)

    def status(self, experiment_id: str) -> str:
        """查询 FIS 实验状态: initiating / running / completed / stopping / stopped / failed"""
        resp = self.fis.get_experiment(id=experiment_id)
        return resp["experiment"]["state"]["status"]

    def wait_for_completion(self, experiment_id: str, timeout: int = 600,
                            poll_interval: int = 10) -> str:
        """等待 FIS 实验完成（Phase 4 使用），返回最终状态"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.status(experiment_id)
            if state in ("completed", "stopped", "failed", "cancelled"):
                return state
            time.sleep(poll_interval)
        return "timeout"

    def delete_template(self, template_id: str) -> None:
        """清理 FIS 实验模板（避免堆积）"""
        self.fis.delete_experiment_template(id=template_id)

    def preflight_check(self) -> bool:
        """验证 FIS 服务可用 + IAM Role 存在"""
        try:
            self.fis.list_experiments(maxResults=1)
            return True
        except Exception:
            return False
```

**Runner 中 backend 路由**：

```python
# runner.py — 按 experiment.backend 分派
def __init__(self):
    self.injector = ChaosMCPClient()   # Chaos Mesh 后端
    self.fis      = FISClient()        # FIS 后端

# Phase 0: Pre-flight
if exp.backend == "fis":
    if not self.fis.preflight_check():
        raise PrefightFailure("FIS 服务不可用")
else:
    # Chaos Mesh: 检查残留实验 + Pod 健康

# Phase 2: Inject
if exp.backend == "fis":
    fis_result = self.fis.inject(exp)
    result.chaos_experiment_name = fis_result["experiment_id"]
    result.fis_template_id = fis_result["template_id"]
else:
    mcp_result = self.injector.inject(...)

# Phase 3: Stop Condition 触发时
if exp.backend == "fis":
    self.fis.stop(result.chaos_experiment_name)
else:
    self.injector.delete(...)

# Phase 4: Recovery
if exp.backend == "fis":
    final_state = self.fis.wait_for_completion(experiment_id)
    self.fis.delete_template(result.fis_template_id)
# 然后等 Pod 恢复（两个后端通用）
```

    # ── FIS Action 构建 ─────────────────────────────────────────────

    def _build_action(self, experiment: Experiment) -> dict:
        """根据 fault.type 映射到 FIS action"""
        fis_action_map = {
            # Lambda 故障
            "fis_lambda_delay":          "aws:lambda:invocation-add-delay",
            "fis_lambda_error":          "aws:lambda:invocation-error",
            "fis_lambda_http_response":  "aws:lambda:invocation-http-integration-response",
            # RDS/Aurora 故障
            "fis_rds_failover":          "aws:rds:failover-db-cluster",
            "fis_rds_reboot":            "aws:rds:reboot-db-instances",
            # EKS 节点级
            "fis_eks_terminate_node":    "aws:eks:terminate-nodegroup-instances",
            # EC2 实例
            "fis_ec2_stop":              "aws:ec2:stop-instances",
            "fis_ec2_terminate":         "aws:ec2:terminate-instances",
            # EBS
            "fis_ebs_pause_io":          "aws:ebs:pause-volume-io",
            "fis_ebs_io_latency":        "aws:ebs:volume-io-latency",
            # 网络
            "fis_network_disrupt":       "aws:network:disrupt-connectivity",
            "fis_vpc_endpoint_disrupt":  "aws:network:disrupt-vpc-endpoint",
            # API 注入
            "fis_api_internal_error":    "aws:fis:inject-api-internal-error",
            "fis_api_throttle":          "aws:fis:inject-api-throttle-error",
            "fis_api_unavailable":       "aws:fis:inject-api-unavailable-error",
        }

        action_id = fis_action_map.get(experiment.fault.type)
        if not action_id:
            raise ValueError(f"Unknown FIS fault type: {experiment.fault.type}")

        action = {
            "actionId": action_id,
            "parameters": self._build_action_params(experiment),
            "targets": {"target-0": "target-0"},
        }
        return action

    def _build_action_params(self, experiment: Experiment) -> dict:
        """构建 FIS action 参数"""
        params = {
            "duration": self._to_iso_duration(experiment.fault.duration),
        }
        # 按 fault_type 添加专属参数
        extra = experiment.fault.extra_params or {}
        if "percentage" in extra:
            params["percentage"] = str(extra["percentage"])
        if "delay_ms" in extra:
            params["duration"] = self._to_iso_duration(experiment.fault.duration)
        return params

    def _to_iso_duration(self, duration: str) -> str:
        """将 '2m' / '30s' / '1h' 转换为 ISO 8601 格式 'PT2M' / 'PT30S' / 'PT1H'"""
        if duration.endswith("m"):
            return f"PT{duration[:-1]}M"
        elif duration.endswith("s"):
            return f"PT{duration[:-1]}S"
        elif duration.endswith("h"):
            return f"PT{duration[:-1]}H"
        return duration

    # ── FIS Target 构建 ─────────────────────────────────────────────

    def _build_target(self, experiment: Experiment) -> dict:
        """根据 fault.type 构建 FIS target"""
        fis_type = experiment.fault.type
        extra = experiment.fault.extra_params or {}

        if fis_type.startswith("fis_lambda"):
            return {
                "resourceType": "aws:lambda:function",
                "resourceArns": [extra["function_arn"]],
                "selectionMode": "ALL",
            }
        elif fis_type.startswith("fis_rds"):
            return {
                "resourceType": "aws:rds:cluster",
                "resourceArns": [extra["cluster_arn"]],
                "selectionMode": "ALL",
            }
        elif fis_type == "fis_eks_terminate_node":
            return {
                "resourceType": "aws:eks:nodegroup",
                "resourceArns": [extra["nodegroup_arn"]],
                "selectionMode": "COUNT(1)",  # 默认终止 1 个节点
            }
        elif fis_type.startswith("fis_ec2"):
            return {
                "resourceType": "aws:ec2:instance",
                "resourceTags": extra.get("tags", {"chaos-target": "true"}),
                "selectionMode": extra.get("selection_mode", "COUNT(1)"),
            }
        elif fis_type.startswith("fis_network"):
            return {
                "resourceType": "aws:ec2:subnet",
                "resourceArns": [extra["subnet_arn"]],
                "selectionMode": "ALL",
            }
        else:
            raise ValueError(f"Cannot build target for: {fis_type}")

    # ── FIS Stop Conditions ────────────────────────────────────────

    def _build_stop_conditions(self, experiment: Experiment) -> list:
        """
        将实验 YAML 中的 stop_conditions 映射为 FIS 原生 Stop Conditions
        FIS Stop Condition = CloudWatch Alarm ARN
        需要预先创建 CloudWatch Alarm（见 infra/fis_setup.py）
        """
        conditions = []
        for sc in experiment.stop_conditions:
            if sc.cloudwatch_alarm_arn:
                conditions.append({
                    "source": "aws:cloudwatch:alarm",
                    "value": sc.cloudwatch_alarm_arn,
                })
        return conditions
```

**FIS YAML 示例**（Lambda 延迟注入）：

```yaml
name: fis-lambda-delay-petstatusupdater
description: "Inject 3s delay into petstatusupdater Lambda invocations"
backend: fis

target:
  service: petstatusupdater
  namespace: lambda        # 标识为 Lambda 函数
  tier: Tier1

fault:
  type: fis_lambda_delay
  duration: "5m"
  extra_params:
    function_arn: "arn:aws:lambda:ap-northeast-1:926093770964:function:petstatusupdater"
    delay_ms: 3000
    percentage: 100         # 100% 的调用注入延迟

steady_state:
  before:
    - metric: success_rate
      threshold: ">= 90%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: ">= 90%"
      window: "5m"

stop_conditions:
  - metric: success_rate
    threshold: "< 50%"
    window: "30s"
    action: abort
    cloudwatch_alarm_arn: "arn:aws:cloudwatch:ap-northeast-1:926093770964:alarm:chaos-petstatusupdater-sr-critical"

rca:
  enabled: true
  trigger_after: "30s"
  expected_root_cause: petstatusupdater

graph_feedback:
  enabled: true
  edges:
    - DependsOn            # Lambda → SQS/DynamoDB 依赖验证
```

**FIS YAML 示例**（Aurora failover）：

```yaml
name: fis-aurora-failover
description: "Force Aurora MySQL failover, verify petlistadoptions reconnection"
backend: fis

target:
  service: petlistadoptions
  namespace: rds
  tier: Tier1

fault:
  type: fis_rds_failover
  duration: "1m"            # FIS failover 动作本身很快，duration 是观测窗口
  extra_params:
    cluster_arn: "arn:aws:rds:ap-northeast-1:926093770964:cluster:petsite-aurora-cluster"

steady_state:
  before:
    - metric: success_rate
      threshold: ">= 90%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: ">= 90%"
      window: "5m"

stop_conditions:
  - metric: success_rate
    threshold: "< 30%"
    window: "60s"
    action: abort
    cloudwatch_alarm_arn: "arn:aws:cloudwatch:ap-northeast-1:926093770964:alarm:chaos-petlistadoptions-sr-critical"

rca:
  enabled: true
  trigger_after: "30s"
  expected_root_cause: petlistadoptions
```

**FIS YAML 示例**（EKS 节点终止）：

```yaml
name: fis-eks-terminate-node-1a
description: "Terminate 1 EKS worker node in AZ 1a, verify Pod rescheduling"
backend: fis

target:
  service: eks-nodegroup
  namespace: eks
  tier: Tier0              # 节点故障影响所有服务

fault:
  type: fis_eks_terminate_node
  duration: "10m"           # 观测窗口：Pod 重调度 + 新节点加入
  extra_params:
    nodegroup_arn: "arn:aws:eks:ap-northeast-1:926093770964:nodegroup/PetSite/petsite-ng/..."
    selection_mode: "COUNT(1)"

steady_state:
  before:
    - metric: success_rate
      threshold: ">= 95%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: ">= 95%"
      window: "5m"

stop_conditions:
  - metric: success_rate
    threshold: "< 50%"
    window: "60s"
    action: abort
    cloudwatch_alarm_arn: "arn:aws:cloudwatch:ap-northeast-1:926093770964:alarm:chaos-eks-sr-critical"

rca:
  enabled: true
  trigger_after: "60s"
  expected_root_cause: eks-node
```

---

### 3.3d FIS 基础设施配置（`infra/fis_setup.py`）

```python
"""
FIS 实验所需的 AWS 基础设施：
1. FIS Experiment IAM Role
2. CloudWatch Alarms（用作 FIS Stop Conditions）
3. FIS Lambda Extension Layer 配置
"""

import boto3

REGION = "ap-northeast-1"
ACCOUNT_ID = "926093770964"

# ── 1. FIS IAM Role ────────────────────────────────────────────────

FIS_ROLE_NAME = "chaos-fis-experiment-role"

FIS_TRUST_POLICY = {
    "Version": "2012-10-17",
    "Statement": [{
        "Effect": "Allow",
        "Principal": {"Service": "fis.amazonaws.com"},
        "Action": "sts:AssumeRole",
    }]
}

FIS_PERMISSIONS = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "FISLambdaActions",
            "Effect": "Allow",
            "Action": [
                "lambda:GetFunction",
                "lambda:GetFunctionConfiguration",
            ],
            "Resource": f"arn:aws:lambda:{REGION}:{ACCOUNT_ID}:function:pet*"
        },
        {
            "Sid": "FISRDSActions",
            "Effect": "Allow",
            "Action": [
                "rds:FailoverDBCluster",
                "rds:RebootDBInstance",
                "rds:DescribeDBClusters",
                "rds:DescribeDBInstances",
            ],
            "Resource": f"arn:aws:rds:{REGION}:{ACCOUNT_ID}:cluster:petsite-*"
        },
        {
            "Sid": "FISEKSActions",
            "Effect": "Allow",
            "Action": [
                "eks:DescribeNodegroup",
                "ec2:TerminateInstances",
                "ec2:DescribeInstances",
                "autoscaling:DescribeAutoScalingGroups",
            ],
            "Resource": "*",
            "Condition": {
                "StringEquals": {
                    "aws:ResourceTag/eks:cluster-name": "PetSite"
                }
            }
        },
        {
            "Sid": "FISNetworkActions",
            "Effect": "Allow",
            "Action": [
                "ec2:CreateNetworkAcl*",
                "ec2:DeleteNetworkAcl*",
                "ec2:DescribeNetworkAcl*",
                "ec2:ReplaceNetworkAclAssociation",
                "ec2:DescribeSubnets",
                "ec2:DescribeVpcs",
            ],
            "Resource": "*"
        },
        {
            "Sid": "FISEBSActions",
            "Effect": "Allow",
            "Action": [
                "ebs:PutVolumeIo",
                "ec2:DescribeVolumes",
            ],
            "Resource": "*"
        },
        {
            "Sid": "FISAPIInjection",
            "Effect": "Allow",
            "Action": [
                "fis:InjectApiInternalError",
                "fis:InjectApiThrottleError",
                "fis:InjectApiUnavailableError",
            ],
            "Resource": "*"
        },
        {
            "Sid": "FISCloudWatch",
            "Effect": "Allow",
            "Action": [
                "cloudwatch:DescribeAlarms",
            ],
            "Resource": f"arn:aws:cloudwatch:{REGION}:{ACCOUNT_ID}:alarm:chaos-*"
        },
        {
            "Sid": "FISLogging",
            "Effect": "Allow",
            "Action": [
                "logs:CreateLogDelivery",
                "logs:PutLogEvents",
            ],
            "Resource": "*"
        },
    ]
}

# ── 2. CloudWatch Alarms（FIS Stop Conditions）─────────────────────

STOP_CONDITION_ALARMS = [
    {
        "AlarmName": "chaos-eks-sr-critical",
        "MetricName": "HTTPCode_Target_5XX_Count",
        "Namespace": "AWS/ApplicationELB",
        "Statistic": "Sum",
        "Period": 60,
        "EvaluationPeriods": 2,
        "Threshold": 100,     # 超过 100 个 5XX/分钟
        "ComparisonOperator": "GreaterThanThreshold",
    },
    {
        "AlarmName": "chaos-petstatusupdater-sr-critical",
        "MetricName": "Errors",
        "Namespace": "AWS/Lambda",
        "Statistic": "Sum",
        "Period": 60,
        "EvaluationPeriods": 2,
        "Threshold": 50,
        "ComparisonOperator": "GreaterThanThreshold",
        "Dimensions": [{"Name": "FunctionName", "Value": "petstatusupdater"}],
    },
    {
        "AlarmName": "chaos-petlistadoptions-sr-critical",
        "MetricName": "HTTPCode_Target_5XX_Count",
        "Namespace": "AWS/ApplicationELB",
        "Statistic": "Sum",
        "Period": 60,
        "EvaluationPeriods": 2,
        "Threshold": 50,
        "ComparisonOperator": "GreaterThanThreshold",
    },
]

# ── 3. FIS Lambda Extension ───────────────────────────────────────
# Lambda 函数需要添加 FIS Extension Layer 才能使用 aws:lambda:* actions
# Layer ARN 因 Region 而异
FIS_LAMBDA_LAYER_ARN = f"arn:aws:lambda:{REGION}:aws:layer:AWS-FIS-Extension:latest"
# 还需要为 Lambda 函数配置 S3 bucket 用于 FIS ↔ Extension 通信
FIS_S3_BUCKET = "chaos-fis-config-926093770964"
```

> ⚠️ **IAM 权限边界**：`chaos-fis-experiment-role` 只能操作标记了 `chaos-target` 或 `petsite-*` 的资源。
> 与 PetSite 业务 IAM Role（`ServicesEks2-*`）完全隔离。

---

### 3.4 DynamoDB 查询层（`query.py`）

> **设计原则**：所有 DynamoDB 读操作统一收口在此模块，**只用 `Query` 走 GSI，禁止 `Scan`**。
> `fmea.py`、`report.py`、CLI `history` 命令均通过此模块读取数据，不各自写 boto3 调用。

```python
from datetime import datetime, timedelta, timezone
from typing import Optional
import boto3

UTC = timezone.utc

class ExperimentQueryClient:
    """
    chaos-experiments 表的所有查询入口
    对应 GSI：
      GSI-1  target_service-start_time-index   → 按服务查历史
      GSI-2  status-start_time-index           → 按状态查熔断
      GSI-3  experiment_name-start_time-index  → 按实验名查趋势
    """

    TABLE  = "chaos-experiments"
    REGION = "ap-northeast-1"

    def __init__(self):
        self.ddb = boto3.client("dynamodb", region_name=self.REGION)

    # ── GSI-1: 按服务查历史 ─────────────────────────────────────────
    def list_by_service(self, service: str, days: int = 90,
                        limit: int = 50) -> list[dict]:
        """
        用途：CLI history 命令 / FMEA _calc_occurrence()
        走：target_service-start_time-index
        返回最新的 limit 条，按时间倒序
        """
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        resp = self.ddb.query(
            TableName=self.TABLE,
            IndexName="target_service-start_time-index",
            KeyConditionExpression="target_service = :s AND start_time >= :t",
            ExpressionAttributeValues={
                ":s": {"S": service},
                ":t": {"S": since},
            },
            ScanIndexForward=False,   # 最新的排前面
            Limit=limit,
        )
        return resp["Items"]

    # ── GSI-2: 按状态查熔断 ─────────────────────────────────────────
    def list_by_status(self, status: str, days: int = 30,
                       service_filter: Optional[str] = None) -> list[dict]:
        """
        用途：护栏触发分析 / 本月所有 ABORTED 实验
        走：status-start_time-index
        注：status 是 DynamoDB 保留字，需用 ExpressionAttributeNames
        可选 service_filter 做客户端二次过滤（GSI-2 不含 target_service 字段）
        """
        since = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        resp = self.ddb.query(
            TableName=self.TABLE,
            IndexName="status-start_time-index",
            KeyConditionExpression="#st = :s AND start_time >= :t",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":s": {"S": status},
                ":t": {"S": since},
            },
            ScanIndexForward=False,
        )
        items = resp["Items"]
        if service_filter:
            items = [i for i in items
                     if i.get("target_service", {}).get("S") == service_filter]
        return items

    # ── GSI-3: 按实验名查趋势 ───────────────────────────────────────
    def list_by_experiment_name(self, name: str,
                                limit: int = 20) -> list[dict]:
        """
        用途：同一实验名历次结果对比（FMEA 趋势 / 报告聚合）
        走：experiment_name-start_time-index
        """
        resp = self.ddb.query(
            TableName=self.TABLE,
            IndexName="experiment_name-start_time-index",
            KeyConditionExpression="experiment_name = :n",
            ExpressionAttributeValues={":n": {"S": name}},
            ScanIndexForward=False,
            Limit=limit,
        )
        return resp["Items"]

    # ── 直接按 PK 查单条 ────────────────────────────────────────────
    def get(self, experiment_id: str, start_time: str) -> Optional[dict]:
        """用于报告回查、RCA 关联等场景"""
        resp = self.ddb.get_item(
            TableName=self.TABLE,
            Key={
                "experiment_id": {"S": experiment_id},
                "start_time":    {"S": start_time},
            },
        )
        return resp.get("Item")

    # ── 辅助：计算某服务历史失败率 ──────────────────────────────────
    def calc_failure_rate(self, service: str, days: int = 90) -> Optional[float]:
        """
        FMEA _calc_occurrence 专用
        返回 0.0~100.0 的失败率；无历史记录时返回 None（调用方 fallback DeepFlow）
        """
        items = self.list_by_service(service, days=days, limit=200)
        if not items:
            return None
        total  = len(items)
        failed = sum(1 for i in items
                     if i.get("status", {}).get("S") in ("FAILED", "ABORTED"))
        return round(failed / total * 100, 1)
```

**GSI 使用规则速查**：

| 查询场景 | 走哪个 GSI | 说明 |
|---------|-----------|------|
| 某服务历史实验 | GSI-1 | FMEA occurrence / CLI history |
| 所有 FAILED 实验 | GSI-2 | 护栏分析 |
| 某服务 + 某状态 | GSI-1 → 客户端过滤 | 数据量少，客户端过滤可接受 |
| 同名实验趋势 | GSI-3 | 多次执行效果对比 |
| 精确查单条 | PK 直查 | 需知道 experiment_id + start_time |

> ⚠️ **何时需要 GSI-4（目前不加）**：
> 若实验数量超过 1000 条后，"按服务+状态"的客户端过滤性能下降，可考虑增加：
> ```
> GSI-4：service_status-start_time-index
>   HASH:  service_status   # 写入时计算 f"{target_service}#{status}"
>   RANGE: start_time
> ```
> 当前 MVP 阶段不引入，避免过早优化。

---

### 3.5 DeepFlow 指标查询（`metrics.py`）

**接口**：`POST http://11.0.2.30:20416/v1/query/`

```python
class DeepFlowMetrics:

    BASE_URL = "http://11.0.2.30:20416"

    def collect(self, service: str, namespace: str = "default",
                window_seconds: int = 60) -> MetricsSnapshot:
        """
        查询指定服务近 window_seconds 秒的 SLI 指标
        """
        end_ts = int(time.time())
        start_ts = end_ts - window_seconds

        sql = f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN response_status = 0 THEN 1 ELSE 0 END) AS success,
                MAX(rrt_max) AS latency_p99_us
            FROM flow_metrics.application_map.1m
            WHERE time >= {start_ts}
              AND time <= {end_ts}
              AND pod_ns_id = 4
              AND l7_protocol > 0
              AND (
                pod_service_0 LIKE '%{service}%'
                OR pod_service_1 LIKE '%{service}%'
              )
        """
        # ⚠️ 具体 SQL 字段待与实际 ClickHouse Schema 确认（开放问题 Q1）
        resp = requests.post(
            f"{self.BASE_URL}/v1/query/",
            json={"sql": sql, "db": "flow_metrics"},
            timeout=5,
        )
        data = resp.json()
        total   = data["result"][0]["total"]
        success = data["result"][0]["success"]
        lat_us  = data["result"][0]["latency_p99_us"]

        return MetricsSnapshot(
            timestamp=end_ts,
            success_rate=round(success / total * 100, 2) if total > 0 else 0,
            latency_p99_ms=round(lat_us / 1000, 1),
            total_requests=total,
        )
```

---

### 3.6 RCA 集成（`rca.py`）

```python
@dataclass
class RCAResult:
    root_cause: str = ""
    confidence: float = 0.0
    evidence: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)
    status: str = "not_triggered"   # not_triggered / error / success
    error_message: str = ""         # error 时记录具体原因


class RCATrigger:
    """
    调用 petsite-rca-engine Lambda，验证根因定位准确性
    """
    LAMBDA_NAME = "petsite-rca-engine"
    REGION      = "ap-northeast-1"

    def trigger(self, service: str, fault_type: str,
                start_time: str) -> RCAResult:
        payload = {
            "affected_resource": service,
            "source": "chaos-runner",
            "fault_type": fault_type,
            "fault_start_time": start_time,
        }
        try:
            resp = self.lambda_client.invoke(
                FunctionName=self.LAMBDA_NAME,
                Payload=json.dumps(payload),
            )
            body = json.loads(resp["Payload"].read())

            # 检查 Lambda 级错误
            if "FunctionError" in resp:
                return RCAResult(
                    status="error",
                    error_message=f"Lambda FunctionError: {body.get('errorMessage', '')}",
                    raw=body,
                )

            result = self._parse(body)
            if result.root_cause:
                result.status = "success"
            else:
                result.status = "error"
                result.error_message = "Lambda 返回成功但 root_cause 为空"
            return result

        except Exception as e:
            return RCAResult(status="error", error_message=str(e))

    def verify(self, result: RCAResult,
               expected: str) -> bool:
        """模糊匹配，允许 expected 是 actual 的子串"""
        if not result.root_cause or not expected:
            return False
        return (expected.lower() in result.root_cause.lower()
                or result.root_cause.lower() in expected.lower())
```

**RCA 三状态说明**：

| 状态 | 含义 | 报告中的展示 |
|------|------|-------------|
| `not_triggered` | RCA 未启用，或实验在 Phase 3 前终止 | `⏭️ RCA 未触发（原因说明）` |
| `error` | RCA Lambda 调用失败或返回空结果 | `⚠️ 触发但失败: {错误信息}` |
| `success` | RCA 正常返回根因 | 根因 + 置信度 + 命中 ✅/❌ |

> 之前 `error` 和 `not_triggered` 都显示为"未触发或无结果"，导致无法区分 RCA 是没跑到还是跑了但失败了。

---

### 3.7 DynamoDB 写入 + 报告生成（`report.py`）

#### 3.7.1 Markdown 报告结构

报告由 `Reporter.generate_markdown()` 程序化生成，包含以下段落：

| 段落 | 内容 | 来源 |
|------|------|------|
| 基本信息 | 实验 ID / 名称 / 后端 / 目标 / 故障类型 / 状态 | ExperimentResult |
| 稳态快照 | 注入前 / 恢复后 success_rate + latency_p99 | Phase 1 / Phase 5 |
| 实验影响 | 期间成功率最低值 / P99 最高值 / 恢复耗时 | Phase 3 聚合 |
| 观测时序 | 每 10s 一行的 success_rate + p99 | Phase 3 snapshots |
| RCA 分析 | 三种状态区分展示（见 3.6） | Phase 3 RCA 触发 |
| 稳态验证 | Phase 5 各项检查结果 ✅/❌ | Phase 5 |
| FIS 实验信息 | Template ID / Experiment ID / CloudWatch Alarm（仅 FIS 后端） | Phase 2 |
| 🧠 AI 分析结论 | LLM 生成的弹性分析和建议 | Bedrock Claude |

#### 3.7.2 LLM 分析结论

实验数据收集完成后，调用 Bedrock Claude 生成分析结论：

```python
BEDROCK_MODEL = "us.anthropic.claude-sonnet-4-20250514"
BEDROCK_REGION = "us-east-1"

def _generate_llm_analysis(self, result: ExperimentResult) -> str:
    """
    调用 Bedrock Claude 生成实验分析结论。
    失败时静默返回空（不阻断报告生成）。
    """
    # 跳过无意义的分析（实验没跑起来）
    if result.status == "ERROR" and not result.snapshots:
        return ""
    if result.status == "ABORTED" and not result.inject_time:
        return ""

    # 构建实验数据摘要（结构化 JSON）
    data_summary = {
        "experiment_name": exp.name,
        "backend": exp.backend,
        "target_service": exp.target_service,
        "target_tier": exp.target_tier,
        "fault_type": exp.fault.type,
        "status": result.status,
        "steady_state_before": {...},
        "steady_state_after": {...},
        "impact_min_success_rate": ...,
        "recovery_seconds": ...,
        "rca": {...} if available,
    }

    # Bedrock invoke_model → Claude Sonnet
    # Prompt 要求：2-4 段话总结、弹性是否达标、异常点、下一步建议
    # 语气专业直接，面向 SRE / 架构师读者，中文输出
    resp = bedrock.invoke_model(modelId=BEDROCK_MODEL, ...)
    return analysis
```

**设计原则**：
- **静默降级**：Bedrock 不可用时返回空字符串，报告仍然完整生成（只缺少 AI 分析段）
- **跳过无意义分析**：实验在 Phase 0/1 就终止的（没有观测数据），不调用 LLM
- **成本控制**：使用 Sonnet（非 Opus），max_tokens=800，单次调用成本 < $0.01

#### 3.7.3 DynamoDB 写入

```python
class Reporter:

    TABLE_NAME = "chaos-experiments"
    REGION     = "ap-northeast-1"

    def save_to_dynamodb(self, result: ExperimentResult):
        item = {
            "experiment_id":           result.experiment_id,
            "start_time":              result.start_time.isoformat(),
            "experiment_name":         result.experiment.name,
            "target_service":          result.experiment.target_service,
            "target_namespace":        result.experiment.target_namespace,
            "target_tier":             result.experiment.target_tier,
            "fault_type":              result.experiment.fault.type,
            "fault_mode":              result.experiment.fault.mode,
            "fault_value":             result.experiment.fault.value,
            "fault_duration":          result.experiment.fault.duration,
            "backend":                 result.experiment.backend,
            "status":                  result.status,
            "end_time":                result.end_time.isoformat(),
            "duration_seconds":        Decimal(str(result.duration_seconds)),
            "steady_state_before":     result.steady_state_before.to_dict(),
            "steady_state_after":      result.steady_state_after.to_dict(),
            "impact_min_success_rate": Decimal(str(result.min_success_rate)),
            "impact_max_latency_p99":  Decimal(str(result.max_latency_p99)),
            "recovery_seconds":        Decimal(str(result.recovery_seconds)),
            "rca_enabled":             result.experiment.rca.enabled,
            "rca_status":              result.rca_result.status if result.rca_result else "not_triggered",
            "rca_error":               result.rca_result.error_message if result.rca_result else "",
            "rca_expected":            result.experiment.rca.expected_root_cause,
            "rca_actual":              result.rca_actual,
            "rca_confidence":          Decimal(str(result.rca_confidence)),
            "rca_match":               result.rca_match,
            "report_path":             result.report_path,
            "yaml_source":             result.yaml_source,
            "ttl":                     int(time.time()) + 90 * 86400,  # 90天 TTL
        }
        if result.abort_reason:
            item["abort_reason"] = result.abort_reason

        self.ddb.put_item(TableName=self.TABLE_NAME, Item=item)
        logger.info(f"✅ DynamoDB 写入成功: {result.experiment_id}")
```

> **DynamoDB 新增字段**：`backend`（chaosmesh/fis）、`rca_status`（not_triggered/error/success）、`rca_error`（错误信息）

---

### 3.8 FMEA 生成器（`fmea.py`）

```python
class FMEAGenerator:
    """
    从 Neptune + DeepFlow + DynamoDB 历史数据自动生成 FMEA 表
    标准公式：RPN = Severity × Occurrence × Detection
      Severity:  失效后果的严重程度（越高越危险）
      Occurrence: 失效发生的可能性（越高越危险）
      Detection:  失效到达用户前被发现的能力（越低越好，D=1 最佳）
    """

    TIER_SEVERITY = {"Tier0": 5, "Tier1": 3, "Tier2": 1}
    REGION        = "ap-northeast-1"

    # Detection 评分规则（基于可观测性覆盖层数，越低越好）
    # PetSite 可观测性栈：DeepFlow eBPF（L7）/ CloudWatch / RCA Engine
    DETECTION_RULES = {
        # EKS 微服务：三层覆盖（DeepFlow + CW + RCA）→ D=1
        "Microservice": 1,
        # Lambda：两层（CloudWatch + RCA，无 DeepFlow L7）→ D=2
        "LambdaFunction": 2,
        # RDS/DynamoDB/SQS：CloudWatch + RCA → D=2
        "RDSCluster": 2, "DynamoDBTable": 2, "SQSQueue": 2,
        # 其他托管服务：CloudWatch 基础指标 → D=3
        "default": 3,
    }

    def generate(self) -> list[FMEARecord]:
        services = self._query_neptune_services()
        records  = []

        for svc in services:
            s   = self.TIER_SEVERITY.get(svc.tier, 1)
            o   = self._calc_occurrence(svc.name)
            d   = self.DETECTION_RULES.get(svc.node_type,
                      self.DETECTION_RULES["default"])
            rpn = s * o * d

            records.append(FMEARecord(
                service=svc.name, tier=svc.tier,
                node_type=svc.node_type,
                severity=s, occurrence=o, detection=d,
                rpn=rpn,
                detection_reason=self._detection_reason(svc.node_type),
            ))

        return sorted(records, key=lambda r: r.rpn, reverse=True)

    def _calc_occurrence(self, service: str) -> int:
        """
        从 DynamoDB chaos-experiments 表（GSI-1）查历史失败率
        同时参考 DeepFlow 近 7 天 error_rate
        映射规则（O 越高越危险）：
          历史实验失败率 ≥ 80%  → 5
          60-79%               → 4
          40-59%               → 3
          20-39%               → 2
          < 20%                → 1
        无历史实验时 fallback: DeepFlow 7天平均 error_rate
        """
        failure_rate = self.query_client.calc_failure_rate(service, days=90)

        if failure_rate is None:
            # 无历史实验记录 → fallback DeepFlow 7天 error_rate
            error_rate = self.metrics.get_7d_error_rate(service)   # 0.0~1.0
            failure_rate = error_rate * 100

        if failure_rate >= 80:   return 5
        elif failure_rate >= 60: return 4
        elif failure_rate >= 40: return 3
        elif failure_rate >= 20: return 2
        else:                    return 1

    def _detection_reason(self, node_type: str) -> str:
        reasons = {
            "Microservice":   "DeepFlow eBPF + CloudWatch + RCA（三层覆盖，D=1）",
            "LambdaFunction": "CloudWatch + RCA，无 DeepFlow L7（D=2）",
            "RDSCluster":     "CloudWatch + RCA（D=2）",
            "DynamoDBTable":  "CloudWatch + RCA（D=2）",
            "SQSQueue":       "CloudWatch + RCA（D=2）",
        }
        return reasons.get(node_type, "CloudWatch 基础指标（D=3）")
```

**FMEA 输出示例**：

```
┌──────────────────┬──────┬───┬───┬───┬─────┬──────────────────────────────────────┐
│ 服务              │ Tier │ S │ O │ D │ RPN │ Detection 说明                        │
├──────────────────┼──────┼───┼───┼───┼─────┼──────────────────────────────────────┤
│ petsearch        │ Tier0│ 5 │ 3 │ 1 │  15 │ DeepFlow+CW+RCA（三层，D=1）          │
│ pethistory(λ)    │ Tier1│ 3 │ 4 │ 2 │  24 │ CW+RCA，无 DeepFlow（D=2）            │
│ payforadoption   │ Tier0│ 5 │ 2 │ 1 │  10 │ DeepFlow+CW+RCA（三层，D=1）          │
│ petsite          │ Tier0│ 5 │ 1 │ 1 │   5 │ DeepFlow+CW+RCA（三层，D=1）          │
└──────────────────┴──────┴───┴───┴───┴─────┴──────────────────────────────────────┘
RPN 阈值：≥30 🔴立即 | 15-29 🟡本周 | <15 🟢本月
```

> **Detection 对混沌实验的启示**：D 值高（检测能力弱）的服务，即使 RPN 不高，也应优先补充监控覆盖——否则即使故障发生也发现不了，混沌实验的意义大打折扣。

---

### 3.9 Neptune 智能场景生成器（`gen_template.py`）

> **对应 PRD F8，2026-03-12 新增。**

#### 3.9.1 职责

`gen_template.py` 连接 Neptune 图谱，自动读取每个微服务的 **Tier 等级** 和 **调用关系**，结合内置的 Tier 规则表与故障容忍度矩阵，动态推算稳态阈值、Stop Conditions、RCA 开关，生成可直接运行的 YAML 实验模板。

核心价值：**SRE 只需指定"测哪个服务"和"注什么故障"，所有数字自动填好**，不再手工推算 95%/5000ms/57% 这些阈值。

#### 3.9.2 模块结构

```python
# gen_template.py 主要类与函数

class ServiceContext:
    """从 Neptune 拉取服务上下文（Tier + 调用关系）"""
    def load()          # MATCH (m:Microservice) + MATCH (a)-[:Calls]->(b)
    def list_services() → list[str]
    def get(name)       → {tier, callers, callees}
    def tier(name)      → "Tier0" | "Tier1" | "Tier2"
    def callers(name)   → list[str]   # 上游调用方
    def callees(name)   → list[str]   # 下游依赖

FAULT_TYPES: dict      # 14 种故障类型元数据 {label, category, duration}
TIER_CONFIG: dict      # Tier 规则表（before_sr/after_sr/after_p99/stop_sr/stop_p99）
FAULT_TOLERANCE: dict  # 故障容忍度矩阵 {category → tol}

def prompt_fault_params(fault_type)  → dict    # 交互式采集专属参数
def build_fault_block(fault_type, params) → str # YAML fault 段生成
def generate_yaml(service, fault_type, params, tier, callers, callees) → str
def interactive(preset_service, preset_fault)   # 主流程
```

#### 3.9.3 阈值推算逻辑

```python
# Tier 规则表
TIER_CONFIG = {
    "Tier0": {before_sr=95, after_sr=95, after_p99=5000, stop_sr=50, stop_p99=8000,  rca=True},
    "Tier1": {before_sr=90, after_sr=90, after_p99=8000, stop_sr=30, stop_p99=15000, rca=True},
    "Tier2": {before_sr=80, after_sr=80, after_p99=15000,stop_sr=20, stop_p99=30000, rca=False},
}

# 故障容忍度矩阵
FAULT_TOLERANCE = {
    "pod":    0.40,   # Pod Kill 期间成功率允许下降 40%
    "network":0.30,
    "app":    0.30,
    "stress": 0.20,
    "kernel": 0.50,
}

# Stop Condition 成功率推算
stop_sr = max(
    int(tier_config["before_sr"] * (1 - fault_tolerance)),
    tier_config["stop_sr"]          # 兜底下限（避免阈值过低失去意义）
)
# 示例：petsite (Tier0) + pod_kill → 95% × (1 - 0.40) = 57%
```

#### 3.9.4 Neptune 查询

```python
NEPTUNE_URL = "https://petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com:8182/openCypher"

# 查节点 Tier
MATCH (m:Microservice) RETURN m.name AS name, m.recovery_priority AS tier

# 查调用关系（构建 callers/callees）
MATCH (a:Microservice)-[:Calls]->(b:Microservice) RETURN a.name AS src, b.name AS dst
```

认证：SigV4（boto3 `get_credentials()` + `SigV4Auth`），与 `fmea.py` 一致。

#### 3.9.5 模板保存路径

```
experiments/
├── tier0/   ← recovery_priority=Tier0 的服务模板
├── tier1/   ← recovery_priority=Tier1 的服务模板
└── tier2/   ← recovery_priority=Tier2 的服务模板
```

> **与 `fmea.py` 的分工**：`fmea.py` 计算"应该测哪个服务"（RPN 优先级），`gen_template.py` 生成"怎么测"（YAML 模板）。两者是流水线关系：FMEA → 优先级 → 生成器 → 模板 → Runner 执行。

#### 3.9.6 CLI 接口

```bash
# 完全交互式
python3 gen_template.py

# 指定服务 + 故障类型（跳过前两步选择，参数仍交互采集）
python3 gen_template.py --service petsite --fault pod_kill

# 仅列出服务 + Tier + 调用关系（只读，不生成模板）
python3 gen_template.py --list-services
```

#### 3.9.7 已生成模板（2026-03-12）

| 文件 | 服务 | 故障类型 | Tier | Stop SR |
|------|------|----------|------|---------|
| `tier0/petsearch-network-delay.yaml` | petsearch | network_delay | Tier0 | < 66% |
| `tier0/petsite-pod-kill.yaml` | petsite | pod_kill | Tier0 | < 57% |
| `tier0/payforadoption-http-chaos.yaml` | payforadoption | http_chaos | Tier0 | < 66% |
| `tier1/pethistory-network-delay.yaml` | pethistory | network_delay | Tier1 | < 63% |
| `tier1/petlistadoptions-network-loss.yaml` | petlistadoptions | network_loss | Tier1 | < 63% |
| `tier1/petstatusupdater-pod-cpu-stress.yaml` | petstatusupdater | pod_cpu_stress | Tier1 | < 72% |

---

### 3.10 MCP 辅助模板生成（LLM Agent 模式）

> **对应 ADR-003，2026-03-13 新增。**

#### 3.10.1 背景与动机

`gen_template.py`（3.9）基于预定义的 Tier 规则表和故障容忍度矩阵生成 Chaos Mesh 模板，是确定性的规则引擎。但 FIS 模板当前需要手动编写，且存在以下问题：

- FIS Action 参数复杂（function_arn / cluster_arn / subnet_arn 等），容易出错
- FIS 实验模板需要与实际 AWS 资源对应，手写无法验证资源是否存在
- 随着环境变化（新增 Lambda 函数、节点组变更），模板需要同步更新

引入 `aws-api-mcp-server` 后，LLM Agent 可以在模板**生成阶段**通过 MCP 工具进行 API 级验证，确保生成的模板准确且可执行。

#### 3.10.2 架构：生成与执行分离

```
┌─────────────────────────────────────────────────────────────┐
│                模板生成阶段（LLM Agent 驱动）                  │
│                                                             │
│  业务需求 + 环境上下文                                        │
│         ↓                                                   │
│  LLM Agent（Sonnet 级别即可）                                │
│  ├── aws-api-mcp-server                                     │
│  │   ├── 查询 AWS 资源（Lambda functions / RDS clusters /    │
│  │   │   EKS nodegroups / subnets）                         │
│  │   ├── 验证 FIS Action 参数合法性                           │
│  │   └── 创建 FIS 实验模板（API 级验证）                      │
│  └── Chaosmesh-MCP                                          │
│      ├── 查询 K8s namespace / services / pods               │
│      └── 验证故障类型和参数                                   │
│         ↓                                                   │
│  输出：经过 API 验证的 YAML 实验模板                           │
│         ↓                                                   │
│  程序化校验（Schema validation + 安全规则检查）                │
│         ↓                                                   │
│  人工审批（可选）                                             │
└─────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────┐
│                执行阶段（确定性 Runner）                       │
│                                                             │
│  Runner 读取 YAML → FaultInjector 分派                       │
│  ├── ChaosMeshBackend（通过 Chaosmesh-MCP 执行）             │
│  └── FISBackend（通过 boto3 直接调用 FIS API 执行）           │
│                                                             │
│  紧急熔断：                                                  │
│  ├── Runner 自研 Stop Conditions（DeepFlow 10s 采样）         │
│  ├── FIS 原生 CloudWatch Alarm Stop Conditions（兜底）        │
│  └── boto3 直调 fis.stop_experiment()（不经过 LLM/MCP）       │
└─────────────────────────────────────────────────────────────┘
```

#### 3.10.3 MCP 工具在生成阶段的价值

| 能力 | 纯 Prompt 生成 YAML | LLM + MCP 工具辅助生成 |
|------|---------------------|----------------------|
| FIS Action 参数合法性 | 靠 prompt 里的映射表，可能幻觉 | API 直接校验，不存在的 action 会报错 |
| 目标资源存在性 | 不知道（靠环境快照，可能过期） | 实时查 AWS 资源，确认 target 存在 |
| IAM / Stop Conditions | LLM 容易遗漏必填项 | Tool 可以强制必填 |
| ARN 格式正确性 | 模型拼接容易出错 | 从 API 响应中直接获取 ARN |

**核心区别**：MCP 把"试错"从"生成后校验"前移到"生成时验证"。LLM 不是凭记忆猜模板，而是通过 API 交互确认模板能不能用。

#### 3.10.4 LLM 选型

模板生成任务本质是**结构化输入 → 结构化输出**，对 LLM 要求：

| 能力 | 说明 | 难度 |
|------|------|------|
| YAML/JSON 生成准确性 | 输出的模板必须能被 Runner 解析执行 | 中 |
| 指令遵循 | 严格按 schema 生成，不幻觉出不存在的 fault.type | 中 |
| 工具调用 | 正确调用 MCP 工具查询 / 验证 | 中 |
| 领域知识 | **靠 System Prompt 补**（模板 schema + 映射表 + 约束规则）| 低 |

**推荐**：Sonnet 级别（Claude Sonnet / GPT-4o / Nova Pro）即可。不需要 Opus/o3 级别——这不是深度推理任务，成本高 10 倍但模板质量不会好 10 倍。

**关键**：模板生成后必须经过**程序化校验层**（Schema 合法、fault.type 在白名单内、target 资源存在、duration 不超上限），这比换更贵的模型有用得多。

#### 3.10.5 环境快照 vs 实时 MCP 查询

两种模式可并存：

| 模式 | 适用场景 | 优劣 |
|------|---------|------|
| **环境快照 + Prompt** | 批量生成多个模板；网络受限环境 | 更快、更便宜；但快照可能过期 |
| **实时 MCP 查询** | 单次精确生成；环境频繁变化 | 准确性最高；但依赖 MCP 可用性、成本更高 |

推荐：**日常批量生成用环境快照，关键模板用 MCP 实时验证**。环境快照可从 DeepFlow（已有拓扑数据）+ Neptune（服务 Tier）+ AWS API（一次性采集）生成。

#### 3.10.6 注意事项

1. **`aws-api-mcp-server` 的 FIS tool 如果调用了 `CreateExperimentTemplate` API，会在 AWS 上创建真实资源**。生成阶段创建的模板（template）需要清理机制，避免废弃模板堆积。
2. **生成阶段创建模板 ≠ 启动实验**。`CreateExperimentTemplate` 只是创建模板定义，不会执行故障注入。`StartExperiment` 才是实际执行——这一步必须在 Runner 的确定性路径中完成。
3. **Agent 不可用时的 fallback**：如果 LLM Agent / MCP 不可用，仍可通过 `gen_template.py`（规则引擎）生成 Chaos Mesh 模板，或手动编写 FIS 模板。MCP 是增强，不是唯一路径。

---

## 4. Neptune 图数据库反馈接口设计

### 4.1 设计原则

基于现有 Neptune 图谱（v2.2，171节点，309条边），混沌实验结果通过新增模块 `graph_feedback.py` 写回，**只新增属性，不修改现有结构**。

```
现有写入路径（不动）：
  DeepFlow ClickHouse → neptune-etl-from-deepflow（每5分钟）→ Calls 边
  AWS API            → neptune-etl-from-aws（每2小时）      → 各节点

新增写入路径：
  chaos-automation Runner → graph_feedback.py → Neptune
  （仅在实验结束后触发，频率极低，不影响 ETL 节奏）
```

### 4.2 需要写回的边类型

当前 Neptune 有 13 种边类型（含 `ConnectsTo`），混沌实验可以验证其中 5 种的依赖强度：

```
Calls        Microservice → Microservice     DeepFlow L7 动态调用（最主要）
DependsOn    BusinessCapability → 基础设施   业务层对基础设施的依赖
AccessesData Lambda → DynamoDB/SQS/S3       Lambda 数据访问依赖
Invokes      StepFunction → Lambda           同步调用链
TriggeredBy  SQS → Lambda                   异步触发链（call_type=async）

不写回（无需验证依赖强度）：
  Contains / LocatedIn / BelongsTo / RoutesTo / PublishesTo / WritesTo / RunsOn / ConnectsTo / Serves
```

### 4.3 边属性 Schema（现有 + 新增）

#### 字段命名约定（Field Naming Convention）

> **核心规则**：ETL 写的字段无前缀，混沌实验写的字段统一加 **`chaos_`** 前缀。
> 新人看到任何 `chaos_*` 字段，即可确定：这是混沌工程实验的实测数据，不是 ETL 推断。

```
ETL 写入（无前缀）：    call_type, error_rate, strength, p99_latency_ms, ...
混沌实验写入（chaos_）：chaos_dependency_type, chaos_degradation_rate, ...

互不覆盖：ETL 不写 chaos_* 字段；graph_feedback.py 不写非 chaos_* 字段
```

---

**`Calls` 边完整属性（现有 + 新增）**：

| 属性 | 前缀 | 写入方 | 说明 |
|------|------|--------|------|
| `call_type` | 无 | neptune-etl-from-deepflow | `sync`（L7 HTTP/gRPC 均为同步）|
| `protocol` | 无 | neptune-etl-from-deepflow | `HTTP` / `gRPC` |
| `port` | 无 | neptune-etl-from-deepflow | 目标端口 |
| `calls` | 无 | neptune-etl-from-deepflow | 调用次数（近5分钟）|
| `avg_latency_us` | 无 | neptune-etl-from-deepflow | 平均延迟（微秒）|
| `p99_latency_ms` | 无 | neptune-etl-from-deepflow | P99 延迟（毫秒）|
| `error_rate` | 无 | neptune-etl-from-deepflow | 当前错误率（0.0~1.0）|
| `error_count` | 无 | neptune-etl-from-deepflow | 错误次数 |
| `active` | 无 | neptune-etl-from-deepflow | 是否活跃调用 |
| `last_seen` | 无 | neptune-etl-from-deepflow | 最近一次观测到的时间戳 |
| **`chaos_dependency_type`** | **chaos_** | **graph_feedback.py** | 实测依赖类型：`strong`/`weak`/`none`/`unverified` |
| **`chaos_degradation_rate`** | **chaos_** | **graph_feedback.py** | 下游故障时上游成功率下降幅度（%）|
| **`chaos_recovery_time_seconds`** | **chaos_** | **graph_feedback.py** | 下游恢复后上游恢复正常的时间（秒）|
| **`chaos_last_verified`** | **chaos_** | **graph_feedback.py** | 最近一次混沌验证时间 ISO8601 |
| **`chaos_verified_by`** | **chaos_** | **graph_feedback.py** | 验证实验的 experiment_id |

---

**`DependsOn` 边完整属性（现有 + 新增）**：

| 属性 | 前缀 | 写入方 | 说明 |
|------|------|--------|------|
| `strength` | 无 | neptune-etl-aws / deepflow-etl | ETL **静态预设**的依赖强度（当前全部为 `strong`）|
| `phase` | 无 | neptune-etl-aws / deepflow-etl | `runtime` / `startup` |
| `source` | 无 | neptune-etl-aws / deepflow-etl | `business-layer` / `deepflow-etl` |
| `last_updated` | 无 | neptune-etl-aws / deepflow-etl | ETL 最近一次更新时间戳（Unix）|
| **`chaos_dependency_type`** | **chaos_** | **graph_feedback.py** | 实测依赖类型，与 `strength` 语义相近但来源不同 |
| **`chaos_degradation_rate`** | **chaos_** | **graph_feedback.py** | 实测影响幅度（%）|
| **`chaos_last_verified`** | **chaos_** | **graph_feedback.py** | 最近一次混沌验证时间（区别于 `last_updated`）|
| **`chaos_verified_by`** | **chaos_** | **graph_feedback.py** | 验证实验 ID |

> **`strength` vs `chaos_dependency_type` 对比说明**：
>
> | | `strength`（ETL） | `chaos_dependency_type`（混沌实验）|
> |--|--|--|
> | 来源 | 静态配置 / 拓扑推断 | 实验实测 |
> | 可信度 | 低（假设性）| 高（实证）|
> | 当前值 | 全部为 `strong` | 未验证为 `unverified` |
> | 更新频率 | ETL 每2小时覆写 | 每次实验后更新 |
>
> **差异时的含义**：
> - `strength=strong` + `chaos_dependency_type=weak` → ETL 判断偏保守，该依赖实际可降级
> - `strength=strong` + `chaos_dependency_type=none` → 依赖边可能是 ETL 误识别，触发人工复核

---

**`AccessesData` / `Invokes` 边**（现有字段不动，新增 `chaos_*`）：

| 现有属性（无前缀，CFN ETL 写）| `declared_in`, `evidence`, `last_scanned`, `last_updated`, `source`, `stack_name` |
|---|---|

| 新增属性 | 前缀 | 写入方 |
|---------|------|--------|
| **`chaos_dependency_type`** | chaos_ | graph_feedback.py |
| **`chaos_degradation_rate`** | chaos_ | graph_feedback.py |
| **`chaos_last_verified`** | chaos_ | graph_feedback.py |
| **`chaos_verified_by`** | chaos_ | graph_feedback.py |

**`TriggeredBy` 边**（当前无属性，全部新增）：

| 新增属性 | 前缀 | 说明 |
|---------|------|------|
| **`chaos_dependency_type`** | chaos_ | 异步依赖，影响通常滞后且较温和 |
| **`chaos_degradation_rate`** | chaos_ | 实测影响（%）|
| **`chaos_last_verified`** | chaos_ | 验证时间 |
| **`chaos_verified_by`** | chaos_ | 验证实验 ID |

---

#### `chaos_dependency_type` 判定规则

```python
def classify_dependency(degradation_rate: float) -> str:
    """
    基于混沌实验实测的成功率下降幅度，判定 chaos_dependency_type
    仅由 graph_feedback.py 写入，ETL 代码不使用此函数
    """
    if degradation_rate >= 80:   return "strong"     # 不可降级
    elif degradation_rate >= 20: return "weak"       # 可部分降级
    else:                        return "none"       # 几乎无影响，依赖存疑
    # 从未执行实验时默认值: "unverified"
```

### 4.4 新增节点属性 Schema（Microservice）

```python
CHAOS_FEEDBACK_NODE_PROPS = {
    "resilience_score":  int,   # 0-100，基于历次实验综合评分
    "last_chaos_test":   str,   # ISO8601
    "chaos_test_count":  int,   # 累计测试次数（递增）
}
```

### 4.5 graph_feedback.py 实现

```python
import sys
sys.path.insert(0, "/home/ubuntu/tech")  # 复用现有 neptune-proxy

import requests
from runner.experiment import ExperimentResult

NEPTUNE_PROXY = "http://localhost:9876"  # neptune-proxy.py，已有

class GraphFeedback:
    """
    实验结果回写到 Neptune 图谱
    通过本地 neptune-proxy.py（9876端口，带 SigV4 签名）
    """

    def write_back(self, result: ExperimentResult):
        if result.status not in ("PASSED", "FAILED", "ABORTED"):
            return  # ERROR 状态跳过，数据不可信

        degradation = result.degradation_rate()
        dep_type    = self._classify(degradation)

        props = {
            "chaos_dependency_type":        dep_type,
            "chaos_degradation_rate":       degradation,
            "chaos_recovery_time_seconds":  result.recovery_seconds or 0,
            "chaos_last_verified":          result.end_time.isoformat(),
            "chaos_verified_by":            result.experiment_id,
        }

        # 1. 更新对应的边属性
        self._update_edge(result, props)

        # 2. 更新 Microservice 节点弹性属性
        self._update_node(result, dep_type)

        # 3. 若 dependency_type=none，触发人工复核告警
        if dep_type == "none":
            self._alert_suspicious_edge(result)

    def _update_edge(self, result: ExperimentResult, props: dict):
        """
        根据实验的 fault_type 和目标，判断要更新哪种边
        当前 MVP 只处理 Calls 边（Microservice → Microservice）
        DependsOn / AccessesData 等边在进阶阶段支持
        """
        svc = result.experiment.target_service

        # Calls 边：kill/failure/network chaos → 影响的是 Calls 关系
        if result.experiment.fault.type in (
            "pod_kill", "pod_failure", "network_delay",
            "network_loss", "network_partition", "http_chaos",
        ):
            gremlin = f"""
                g.E().hasLabel('Calls')
                 .where(__.outV().has('name', '{svc}'))
                 .or(
                     __.inV().has('name', '{svc}')
                 )
                 .property(single, 'chaos_dependency_type',        '{props["chaos_dependency_type"]}')
                 .property(single, 'chaos_degradation_rate',       {props["chaos_degradation_rate"]})
                 .property(single, 'chaos_recovery_time_seconds',  {props["chaos_recovery_time_seconds"]})
                 .property(single, 'chaos_last_verified',          '{props["chaos_last_verified"]}')
                 .property(single, 'chaos_verified_by',            '{props["chaos_verified_by"]}')
            """
            self._run_gremlin(gremlin)

    def _update_node(self, result: ExperimentResult, dep_type: str):
        """更新 Microservice 节点弹性属性"""
        svc   = result.experiment.target_service
        score = self._calc_resilience_score(result, dep_type)

        gremlin = f"""
            g.V().hasLabel('Microservice').has('name', '{svc}')
             .property(single, 'last_chaos_test',  '{result.end_time.isoformat()}')
             .property(single, 'resilience_score', {score})
             .property(single, 'chaos_test_count',
                 __.coalesce(
                     __.values('chaos_test_count').map({{it -> it.get() + 1}}),
                     __.constant(1)
                 )
             )
        """
        self._run_gremlin(gremlin)

    def _calc_resilience_score(self, result: ExperimentResult,
                                dep_type: str) -> int:
        """
        弹性评分（0-100）：
          基础分 = 100 - degradation_rate
          恢复快加分，恢复慢扣分
          实验被熔断（ABORTED）额外扣 10 分
        """
        score = max(0, 100 - result.degradation_rate())
        if result.recovery_seconds and result.recovery_seconds < 60:
            score = min(100, score + 5)
        elif result.recovery_seconds and result.recovery_seconds > 300:
            score = max(0, score - 10)
        if result.status == "ABORTED":
            score = max(0, score - 10)
        return int(score)

    def _classify(self, degradation_rate: float) -> str:
        if degradation_rate >= 80:   return "strong"
        elif degradation_rate >= 20: return "weak"
        else:                        return "none"

    def _alert_suspicious_edge(self, result: ExperimentResult):
        """dependency_type=none 说明该 Calls 边可能是 ETL 误识别"""
        import logging
        logging.warning(
            f"⚠️  SUSPICIOUS EDGE: {result.experiment.target_service} 的 Calls 边 "
            f"degradation_rate={result.degradation_rate():.1f}% → 可能是 DeepFlow 误识别，"
            f"建议人工复核 Neptune 图谱。experiment_id={result.experiment_id}"
        )

    def _run_gremlin(self, query: str):
        resp = requests.post(
            f"{NEPTUNE_PROXY}/gremlin",
            json={"gremlin": query},
            timeout=10,
        )
        resp.raise_for_status()
```

### 4.6 runner.py 集成点

```python
# runner.py Phase5 末尾，报告生成之后
def _phase5_steady_state_after(self, experiment, result):
    # ... 稳态验证 ...
    self._save_report(result)
    self._save_to_dynamodb(result)

    # 图数据库反馈（F7）
    if experiment.graph_feedback.enabled:
        graph_feedback = GraphFeedback()
        graph_feedback.write_back(result)
        logger.info(f"✅ Neptune 图谱已更新: {experiment.target_service}")
```

**实验 YAML 新增配置项**：
```yaml
graph_feedback:
  enabled: true              # 默认 true（MVP 阶段）
  edges:
    - Calls                  # 必填，根据 fault_type 自动匹配
    # - DependsOn            # 进阶，测试基础设施依赖时启用
    # - AccessesData         # 进阶，测试 Lambda → DDB 依赖时启用
```

### 4.7 验证查询（实验后确认写入成功）

```groovy
// 查看 petsite 所有 Calls 边的混沌验证结果
g.V().has('name','petsite')
 .outE('Calls')
 .project('to','chaos_dep','chaos_degradation','chaos_verified')
   .by(__.inV().values('name'))
   .by(coalesce(values('chaos_dependency_type'), constant('unverified')))
   .by(coalesce(values('chaos_degradation_rate'), constant(-1.0)))
   .by(coalesce(values('chaos_last_verified'),    constant('never')))

// FMEA 用：找所有实测为强依赖的边（爆炸半径分析）
g.E().hasLabel('Calls')
 .has('chaos_dependency_type','strong')
 .project('from','to','degradation')
   .by(__.outV().values('name'))
   .by(__.inV().values('name'))
   .by('chaos_degradation_rate')
 .order().by('degradation', desc)

// 找 ETL 与实验结论不一致的 DependsOn 边（待人工复核）
g.E().hasLabel('DependsOn')
 .has('strength', 'strong')
 .has('chaos_dependency_type', within('weak','none'))
 .project('from','to','chaos_dep','chaos_verified_by')
   .by(__.outV().values('name'))
   .by(__.inV().values('name'))
   .by('chaos_dependency_type')
   .by('chaos_verified_by')

// 找从未被混沌验证过的 Calls 边
g.E().hasLabel('Calls')
 .not(has('chaos_dependency_type'))
 .project('from','to')
   .by(__.outV().values('name'))
   .by(__.inV().values('name'))
```

### 4.8 与现有 ETL 的协作关系

```
neptune-etl-from-deepflow（每5分钟）
  → 写 Calls 边：qps, p99_latency, error_rate, call_type
  → 不覆盖混沌验证属性（dependency_type 等）

chaos graph_feedback（实验结束时触发）
  → 写 Calls 边：dependency_type, degradation_rate, recovery_time_seconds
  → 写 Microservice 节点：resilience_score, last_chaos_test

两者互不干扰，property(single,...) 只更新自己负责的字段。
```

---

## 5. 实验 YAML Schema

### 4.1 完整 Schema 定义

```yaml
# experiments/tier0/petsite-pod-kill.yaml
name: petsite-pod-kill-tier0            # 唯一实验名（用于 GSI-3 趋势查询）
description: "Kill 50% petsite pods"   # 人类可读描述

target:
  service: petsite
  namespace: default
  tier: Tier0

fault:
  type: pod_kill                        # 对应 Chaosmesh-MCP 工具名
  mode: fixed-percent
  value: "50"
  duration: "2m"
  # 扩展参数（按 fault.type 不同）
  # latency: "100ms"                   # network_delay 用
  # loss: "30"                         # network_loss 用
  # container_names: ["main"]          # container_kill 用

steady_state:
  before:
    - metric: success_rate
      threshold: ">= 99%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: ">= 99%"
      window: "5m"
    - metric: latency_p99
      threshold: "< 200ms"
      window: "5m"

stop_conditions:                        # Guardrails，必填
  - metric: success_rate
    threshold: "< 50%"
    window: "30s"
    action: abort
  - metric: latency_p99
    threshold: "> 5000ms"
    window: "30s"
    action: abort

rca:
  enabled: true
  trigger_after: "30s"
  expected_root_cause: petsite

options:
  max_duration: "10m"                   # 超时保护
  save_to_bedrock_kb: false
```

### 4.2 fault.type 与 Chaosmesh-MCP 工具映射

| fault.type | Chaosmesh-MCP 函数 | 额外参数 |
|-----------|-------------------|---------|
| `pod_kill` | `pod_kill()` | — |
| `pod_failure` | `pod_failure()` | — |
| `container_kill` | `container_kill()` | `container_names` |
| `pod_cpu_stress` | `pod_cpu_stress()` | `workers`, `load` |
| `pod_memory_stress` | `pod_memory_stress()` | `size` |
| `network_delay` | `network_delay()` | `latency`, `jitter` |
| `network_loss` | `network_loss()` | `loss` |
| `network_corrupt` | `network_corrupt()` | `corrupt` |
| `network_partition` | `network_partition()` | `direction`, `external_targets` |
| `http_chaos` | `http_chaos()` | `action`, `port`, `delay` |
| `io_chaos` | `io_chaos()` | `action`, `volume_path`, `delay` |
| `time_chaos` | `time_chaos()` | `time_offset` |
| `kernel_chaos` | `kernel_chaos()` | `fail_kern_request` |

---

## 5. 关键流程时序图

### 5.1 正常实验流程

```
Runner          Chaosmesh-MCP    DeepFlow API     Lambda(RCA)     DynamoDB
  │                  │                │                │               │
  │── Phase0 ──────────────────────────────────────────────────────── │
  │   health_check()──►              │                │               │
  │   list_services()─►              │                │               │
  │                  │                │                │               │
  │── Phase1 ──────────────────────────────────────────────────────── │
  │               collect(window=60s)─►                │               │
  │               ◄── MetricsSnapshot │                │               │
  │   [verify steady state before]   │                │               │
  │                  │                │                │               │
  │── Phase2 ──────────────────────────────────────────────────────── │
  │── pod_kill() ───►│                │                │               │
  │   ◄── experiment_name            │                │               │
  │                  │                │                │               │
  │── Phase3 (loop every 10s) ─────────────────────────────────────── │
  │               collect()──────────►                │               │
  │               ◄── snapshot       │                │               │
  │   check_guardrails(snapshot)      │                │               │
  │   [T+30s: trigger RCA]            │                │               │
  │──────────────────────────────────────── invoke() ─►               │
  │──────────────────────────────────────── ◄── RCAResult             │
  │                  │                │                │               │
  │── Phase4 ──────────────────────────────────────────────────────── │
  │   [fault expired, auto recovered] │                │               │
  │                  │                │                │               │
  │── Phase5 ──────────────────────────────────────────────────────── │
  │               collect(window=300s)►                │               │
  │               ◄── snapshot       │                │               │
  │   [verify steady state after]    │                │               │
  │   generate_report()               │                │               │
  │── put_item() ──────────────────────────────────────────────────── ►│
  │   ◄── OK                         │                │               │
```

### 5.2 Guardrail 触发流程（熔断）

```
Runner          Chaosmesh-MCP    DeepFlow API
  │                  │                │
  │── Phase3 (T+20s) ──────────────── │
  │               collect()──────────►│
  │               ◄── success_rate=43% │
  │                  │                │
  │   check_guardrails()              │
  │   [43% < 50% threshold]           │
  │   raise AbortException            │
  │                  │                │
  │── Emergency Cleanup ────────────── │
  │── delete_experiment() ──►         │
  │   ◄── OK                          │
  │                  │                │
  │   status = "ABORTED"              │
  │   abort_reason = "success_rate=43% < 50% for 30s"
  │   generate_report()               │
  │   save_to_dynamodb(status=ABORTED)│
```

---

## 6. 基础设施

### 6.1 DynamoDB 建表

```bash
# 一次性执行
cd /home/ubuntu/tech/chaos/code
python infra/dynamodb_setup.py
```

IAM 权限要求（runner 执行环境）：
```json
{
  "Effect": "Allow",
  "Action": [
    "dynamodb:PutItem",
    "dynamodb:GetItem",
    "dynamodb:Query"
  ],
  "Resource": [
    "arn:aws:dynamodb:ap-northeast-1:926093770964:table/chaos-experiments",
    "arn:aws:dynamodb:ap-northeast-1:926093770964:table/chaos-experiments/index/*"
  ]
}
```

> ⚠️ **不授予** PetSite 业务表（`ServicesEks2-*`）任何权限。
> ⚠️ **已移除 Scan 权限**——query.py 禁止 Scan，IAM Policy 也不应授予。

**FIS 专用 IAM Role**：

```json
{
  "Role": "chaos-fis-experiment-role",
  "TrustPolicy": { "Service": "fis.amazonaws.com" },
  "Permissions": "见 TDD 3.3d infra/fis_setup.py"
}
```

> FIS IAM Role 与 chaos-automation runner IAM 隔离：
> - `chaos-fis-experiment-role`：FIS 服务 assume，执行故障注入动作
> - Runner 本机 IAM：调用 `fis:CreateExperimentTemplate` / `fis:StartExperiment` / `fis:StopExperiment`

**Runner 额外 IAM 权限（FIS 管理）**：

```json
{
  "Effect": "Allow",
  "Action": [
    "fis:CreateExperimentTemplate",
    "fis:StartExperiment",
    "fis:StopExperiment",
    "fis:GetExperiment",
    "fis:ListExperiments",
    "fis:DeleteExperimentTemplate",
    "iam:PassRole"
  ],
  "Resource": [
    "arn:aws:fis:ap-northeast-1:926093770964:experiment/*",
    "arn:aws:fis:ap-northeast-1:926093770964:experiment-template/*",
    "arn:aws:iam::926093770964:role/chaos-fis-experiment-role"
  ]
}
```

### 6.2 运行环境

```
控制机：10.1.2.198（当前机器）
Python：3.10+
依赖：
  kubernetes>=32.0.1
  boto3>=1.34
  requests>=2.31
  pyyaml>=6.0
  dataclasses-json>=0.6
```

### 6.3 CLI 接口

```bash
# 执行单个实验（Chaos Mesh 后端）
python code/main.py run --file experiments/tier0/petsite-pod-kill.yaml

# 执行单个实验（FIS 后端）
python code/main.py run --file experiments/fis/lambda/fis-lambda-delay-petstatusupdater.yaml

# 执行某 Tier 下所有实验
python code/main.py run --tier tier0 --dry-run    # dry-run 不实际注入

# 执行所有 FIS 实验
python code/main.py run --suite fis --dry-run

# 生成 FMEA 报告
python code/main.py fmea --output validation-results/fmea-$(date +%Y%m%d).md

# 查询历史记录
python code/main.py history --service petsite --limit 10
# 底层：ExperimentQueryClient.list_by_service("petsite", days=90, limit=10)
# 输出：表格展示 experiment_id / status / fault_type / impact_min_success_rate / rca_match

python code/main.py history --service petsite --status ABORTED --days 30
# 底层：ExperimentQueryClient.list_by_status("ABORTED", days=30, service_filter="petsite")

python code/main.py history --name petsite-pod-kill-tier0
# 底层：ExperimentQueryClient.list_by_experiment_name("petsite-pod-kill-tier0")

# 建表 + FIS 基础设施（首次执行）
python code/main.py setup           # DynamoDB 建表
python code/main.py setup --fis     # FIS IAM Role + CloudWatch Alarms
```

---

## 7. 开放问题（技术待确认项）

| # | 问题 | 影响模块 | 优先级 |
|---|------|---------|--------|
| Q1 | DeepFlow Querier API 的具体 SQL：`application_map` 表字段 `response_status`/`rrt_max` 的准确名称，及 `pod_service_0/1` 的过滤条件 | `metrics.py` | 🔴 P0，需在 M1 前确认 |
| Q2 | `petsite-rca-engine` Lambda 的入参格式：是否接受时间范围参数，返回 JSON 结构 | `rca.py` | 🔴 P0，需在 M3 前确认 |
| Q3 | Bedrock KB S3 bucket 写入权限：runner IAM role 是否已有 `s3:PutObject` 到 `petsite-rca-incidents-926093770964` | `report.py` | 🟡 P1 |
| Q4 | Neptune openCypher 查询接口：`neptune-proxy.py` 还是直接连 Neptune endpoint | `fmea.py` | 🟡 P2 |
| Q5 | FIS Lambda Extension Layer：petstatusupdater / petadoptionshistory 是否可以添加 Layer？是否使用 response streaming（不兼容 FIS Extension）？ | `fis_backend.py` | 🔴 P0，需在 M5 前确认 |
| Q6 | FIS Lambda Extension S3 Bucket 创建 + IAM 配置：需要为 FIS ↔ Lambda Extension 通信创建独立 S3 bucket | `infra/fis_setup.py` | 🟡 P1 |
| Q7 | Aurora MySQL 集群当前是单 Writer 还是有 Reader？`aws:rds:failover-db-cluster` 需要至少 2 个实例才有意义 | `fis_backend.py` | 🟡 P1 |
| Q8 | FIS `aws:network:disrupt-connectivity` 对 EKS 使用的 subnet 的影响范围——是否会影响 Neptune/DeepFlow 等非 EKS 资源 | `fis_backend.py` | 🟡 P1 |

---

## 8. 架构决策记录（ADR）

### ADR-001：FIS 与 Chaos Mesh 集成方式

**日期**: 2026-03-04  
**状态**: ⚠️ 已被 ADR-002 取代

**背景**：存在两种集成架构：

- **方案 A（独立调用）**：Runner 直接调用 Chaosmesh-MCP 执行 K8s 故障；未来如需 AWS 层故障（EC2/Lambda/RDS）可额外接入 FIS
- **方案 B（FIS 统一编排）**：Runner 只调用 FIS，K8s 故障通过 FIS `aws:eks:inject-kubernetes-custom-resource` action 将 Chaos Mesh CRD apply 到集群，Chaos Mesh Controller 执行实际注入；实验记录、Stop Conditions、Cleanup 全部由 FIS 托管

| 维度 | 方案 A | 方案 B |
|------|--------|--------|
| 实验记录 | DynamoDB + 本地文件 | FIS 统一（+ CloudTrail）|
| Stop Conditions | 自研 guardrails.py | FIS 原生 CloudWatch Alarm |
| 自动清理 | delete_experiment() | FIS 自动 |
| K8s 故障覆盖 | ✅ Chaos Mesh 全部 24 种 | FIS 原生 7 种 + inject-k8s-cr |
| 已验证程度 | ✅ 2026-02-28 全量验证 | 需重新验证 |
| 复杂度 | 低 | 高（FIS IAM Role + EKS RBAC 配置）|

**原决策**：采用方案 A（仅 Chaos Mesh）

**已被 ADR-002 取代**：实际需求需要同时覆盖 K8s 层和 AWS 托管服务层，纯方案 A 无法满足。

---

### ADR-002：双工具并行策略（Chaos Mesh + FIS）

**日期**: 2026-03-13  
**状态**: ✅ 已决策

**背景**：PetSite 架构包含 EKS 微服务 + AWS 托管服务（Lambda、Aurora MySQL、DynamoDB、SQS），单一工具无法覆盖全部故障场景。ADR-001 选择的纯 Chaos Mesh 方案在 AWS 托管服务层存在覆盖盲区。

**方案 C（双工具并行）**：Chaos Mesh 和 FIS 各司其职，通过统一的 `FaultInjector` 抽象层对 Runner 透明。

```
                      FaultInjector（统一接口）
                     ┌──────────┴──────────┐
                     │                     │
              ChaosMeshBackend        FISBackend
              K8s 层故障               AWS 托管服务层故障
              ├── Pod 生命周期         ├── Lambda 故障
              ├── 容器故障             ├── RDS/Aurora failover
              ├── 网络（Pod 级）       ├── EKS 节点终止
              ├── HTTP/DNS/IO          ├── EBS IO 故障
              ├── CPU/内存压力         ├── VPC/Subnet 网络中断
              ├── 时间/内核            ├── AWS API 注入
              └── 24 种已验证          └── VPC Endpoint 中断
```

**决策**：**采用方案 C**

**分工原则**：

| 故障层面 | 工具 | 理由 |
|---------|------|------|
| K8s Pod/容器级 | **Chaos Mesh** | 24 种已验证，http_chaos/time_chaos/kernel_chaos 是独有能力 |
| AWS 托管服务 | **FIS** | Lambda/RDS/DynamoDB/SQS，Chaos Mesh 无法触及 |
| EKS 节点级 | **FIS** | `terminate-nodegroup-instances`，测试节点失效 + Pod 重调度 |
| AZ/VPC 网络基础设施 | **FIS** | `disrupt-connectivity` 是 subnet/VPC 级别 |
| AWS API 降级/限流 | **FIS** | `inject-api-throttle-error`，模拟 AWS 侧降级 |
| EKS Pod 故障（重叠区） | **Chaos Mesh 优先** | 已验证更充分，细粒度更高 |

**理由**：
1. **覆盖完整性**：K8s 层 + AWS 托管服务层 = PetSite 全栈故障覆盖
2. **复用已有投入**：Chaos Mesh 24 种工具已验证，不丢弃
3. **FIS 独有能力**：Lambda 故障注入、RDS failover、AZ 级网络中断是 Chaos Mesh 无法替代的
4. **FIS 原生安全**：CloudWatch Alarm Stop Conditions + CloudTrail 审计，为生产环境准备
5. **统一接口**：`FaultInjector` 抽象层对 Runner 透明，YAML 中 `backend: chaosmesh|fis` 即可切换

**FIS 引入的额外工作**：
- FIS Experiment IAM Role 创建（`chaos-fis-experiment-role`）
- CloudWatch Alarm 创建（FIS Stop Conditions）
- Lambda 函数添加 FIS Extension Layer（用于 `aws:lambda:*` actions）
- FIS 配置 S3 Bucket（Lambda Extension 通信用）

**FIS 与 Chaos Mesh 在 EKS Pod 层的重叠处理**：
- FIS 有 `aws:eks:pod-delete/pod-cpu-stress/pod-memory-stress/pod-network-*` 等 7 种 EKS Pod actions
- 与 Chaos Mesh 功能重叠，但 Chaos Mesh 额外支持 `http_chaos`、`time_chaos`、`kernel_chaos`、`dns_chaos` 等 FIS 没有的类型
- 决策：EKS Pod 层**默认用 Chaos Mesh**（已验证 + 更细粒度），FIS EKS Pod actions 仅作为备选（如 Chaos Mesh 不可用时的 fallback）
- 高级场景：FIS `aws:eks:inject-kubernetes-custom-resource` 可将 Chaos Mesh CRD 通过 FIS 注入，实现 FIS 统一编排 + Chaos Mesh 执行的混合模式（P2 考虑）

**预留扩展点**：
```yaml
# 实验 YAML backend 配置
backend: chaosmesh    # K8s 层故障（默认）
backend: fis          # AWS 托管服务层故障
# backend: fis+chaosmesh  # 未来：FIS 编排 + Chaos Mesh 执行（inject-k8s-cr）
```

---

### ADR-003：MCP 在模板生成 vs 执行中的角色

**日期**: 2026-03-13  
**状态**: ✅ 已决策

**背景**：当前双后端实现存在不对称——ChaosMesh 后端通过 `Chaosmesh-MCP`（MCP Server）间接调用，FIS 后端通过 `boto3` 直接调用 AWS FIS API。讨论中确认 `aws-api-mcp-server` 拥有 FIS 全生命周期控制能力（创建模板、启动实验、查询状态、停止实验），是否应引入 MCP 统一两个后端？

**分析**：

| 方案 | 描述 | 优势 | 风险 |
|------|------|------|------|
| A. 执行也走 MCP | LLM Agent 通过 MCP 驱动 FIS 全生命周期 | 两个后端对称；Agent 能根据执行反馈动态调整 | 执行路径依赖 LLM 可用性；紧急熔断多一跳延迟 |
| B. 仅生成走 MCP | 生成阶段 MCP 辅助验证，执行阶段保持确定性 | 生成准确性 + 执行可靠性兼得 | 生成和执行使用不同链路，需要对齐 |
| C. 完全不引入 MCP | FIS 侧保持 boto3 直调 | 最简单 | 模板生成无 API 级验证，手写易出错 |

**决策**：**采用方案 B — 生成走 MCP，执行保留确定性路径**

**理由**：
1. **MCP 在生成阶段的价值确实存在**——`aws-api-mcp-server` 的 FIS tool 能让 LLM 在生成模板时通过 API 验证 Action 参数合法性、目标资源存在性、IAM 配置完整性，比 LLM 纯靠 prompt 生成 YAML 更准确
2. **执行阶段不应依赖 LLM**——Runner 是确定性程序，读 YAML → 分派后端 → 按步骤执行。如果 LLM/MCP 不可用，Runner 仍能独立执行预生成的模板
3. **紧急熔断必须走最短路径**——`fis.stop_experiment()` 通过 boto3 直调，不经过 LLM → MCP 链路，最大化熔断响应速度
4. **FIS 原生 CloudWatch Alarm Stop Conditions 不依赖任何外部系统**——作为终极兜底安全网

**一致性在抽象层，不在实现细节**：
- Runner 层面：`FaultInjector` 接口统一了两个后端，Runner 不感知底层是 MCP 还是 boto3
- 生成层面：LLM Agent 统一通过 MCP（`Chaosmesh-MCP` + `aws-api-mcp-server`）生成两种后端的模板

**实现路线**：
```
Phase 1（当前）：FIS 模板手动编写，Chaos Mesh 模板由 gen_template.py 规则引擎生成
Phase 2（近期）：引入 aws-api-mcp-server，LLM Agent 辅助生成 FIS 模板（API 级验证）
Phase 3（进阶）：LLM Agent 根据环境快照 + 业务需求，自动为两个后端生成模板
```

---

## 9. 变更记录

| 版本 | 日期 | 变更内容 |
|------|------|---------|
| 0.1 | 2026-03-04 | 初稿，含架构/DynamoDB/核心模块设计 |
| 0.2 | 2026-03-04 | 新增 ADR-001：FIS vs Chaos Mesh 架构决策（采用方案 A）|
| 0.3 | 2026-03-07 | 新增 3.4 `query.py` 查询层设计；补全 `fmea.py._calc_occurrence()` 实现；CLI history 命令补充三种查询模式 |
| 0.4 | 2026-03-12 | 新增 3.9 `gen_template.py` Neptune 智能场景生成器设计（对应 PRD F8）；更新目录结构和模块职责表 |
| 0.5 | 2026-03-13 | 新增 FIS 双工具并行架构：`fault_injector.py` 抽象层 + `fis_backend.py` + `chaosmesh_backend.py`；ADR-002 取代 ADR-001；新增 FIS IAM / CloudWatch / Lambda Extension 基础设施设计；更新架构图、数据模型、开放问题 |
| 0.6 | 2026-03-13 | 新增 3.10 MCP 辅助模板生成设计（LLM Agent + aws-api-mcp-server + Chaosmesh-MCP）；ADR-003 生成/执行分离策略；更新架构图加入 Template Generator；明确标注两个后端的实现差异（MCP vs boto3）|
| 0.7 | 2026-03-13 | `fis_backend.py` 完整实现（FISClient，15 种 fault type）；`experiment.py` 新增 backend/extra_params/cloudwatch_alarm_arn 字段；`runner.py` 按 backend 路由双后端；`rca.py` 三状态改造（not_triggered/error/success）；`report.py` 新增 Bedrock LLM 分析结论 + FIS 实验信息段；`result.py` 新增 fis_template_id |
