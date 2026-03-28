# graph-driven-chaos 改进计划

> 基于 AWS Strands Chaos Engineering Agents 的借鉴分析，结合现有图谱驱动架构优势，制定的系统性改进路线。
> 日期：2026-03-20

---

## 现状总结

### 已有能力
- ✅ Neptune 图谱驱动目标发现（Target Resolver）
- ✅ 5 Phase 确定性执行引擎（Pre-flight → Inject → Observe → Recover → Verify）
- ✅ FIS + Chaos Mesh 双后端
- ✅ 代码级 Stop Condition + 自动熔断
- ✅ RCA 根因分析（Lambda Engine）
- ✅ 图谱反馈（chaos_resilience_score）
- ✅ DynamoDB 历史记录 + Markdown 报告
- ✅ gen_template.py 从图谱生成实验模板

### 缺失能力（与 Strands 对比）
- ❌ AI 驱动的假设生成（当前靠手写 YAML 或机械模板）
- ❌ 假设优先级排序（当前靠 Tier 手动分级）
- ❌ 实验结果闭环学习（当前只写 score，不自动迭代）
- ❌ Tag 过滤目标资源（当前按名称匹配，无多租户隔离）
- ❌ Workflow Orchestrator 批量编排（当前单实验执行）
- ❌ 可观测性结构化日志（当前基础 logging）

---

## 改进架构

```
                          ┌──────────────────────────┐
                          │   Workflow Orchestrator   │
                          │  (批量编排 + 调度策略)     │
                          └────────────┬─────────────┘
                                       │
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
              ▼                        ▼                        ▼
   ┌──────────────────┐    ┌───────────────────┐    ┌──────────────────┐
   │ Hypothesis Agent │    │ ExperimentRunner  │    │  Learning Agent  │
   │ (LLM 假设生成)    │    │ (5 Phase 引擎)    │    │ (LLM 闭环学习)   │
   └────────┬─────────┘    └────────┬──────────┘    └────────┬─────────┘
            │                       │                        │
            │    ┌──────────────────┤                        │
            │    │                  │                        │
            ▼    ▼                  ▼                        ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │                        数据层                                     │
   │  Neptune 图谱 │ DynamoDB 历史 │ hypotheses.json │ targets-*.json  │
   └──────────────────────────────────────────────────────────────────┘
```

---

## Phase 1：Hypothesis Agent（AI 假设生成）

### 目标
从 Neptune 图谱 + 代码仓库自动生成覆盖 5 大故障域的混沌工程假设，并按优先级排序。

### 故障域覆盖
1. **Compute** — Pod 崩溃、节点故障、CPU/内存压力
2. **Data** — RDS 主从切换、DynamoDB 限流、数据一致性
3. **Network** — 延迟注入、丢包、DNS 故障、AZ 网络隔离
4. **Dependencies** — 上下游服务不可用、超时、级联故障
5. **Resources** — EBS IO 延迟、S3 不可用、Lambda 并发耗尽

### 设计

#### 新文件：`code/agents/hypothesis_agent.py`

```python
class HypothesisAgent:
    """
    AI 驱动的混沌工程假设生成器。
    
    输入源：
    1. Neptune 图谱拓扑（服务依赖、资源关联、历史故障）
    2. 代码仓库分析（可选，分析 IaC 和应用代码）
    3. 历史实验结果（DynamoDB）
    
    输出：
    - hypotheses.json（假设库，含优先级排序）
    - 可直接喂给 gen_template.py 生成实验 YAML
    """
    
    def generate(self, 
                 graph_context: dict,      # Neptune 图谱数据
                 repo_url: str = None,     # 代码仓库（可选）
                 history: list = None,     # 历史实验结果
                 max_hypotheses: int = 50  # 生成数量上限
                 ) -> list[Hypothesis]:
        """
        生成假设流程：
        1. 从 Neptune 查询完整拓扑（服务 + 依赖 + 资源 + 历史故障）
        2. 从代码仓库提取 IaC 模式（CDK/CFN/Helm）
        3. 调用 Bedrock LLM 生成假设（按故障域分类）
        4. 用 LLM 评估每个假设的优先级（影响面 × 可行性 × 学习价值）
        5. 去重 + 排序 → 输出
        """
    
    def prioritize(self, hypotheses: list[Hypothesis]) -> list[Hypothesis]:
        """
        优先级排序维度：
        - business_impact: 业务影响（Tier0 > Tier1 > Tier2）
        - blast_radius: 爆炸半径（越小越安全）
        - feasibility: 技术可行性（有现成 FIS action 的优先）
        - learning_value: 学习价值（未测试过的故障模式优先）
        - dependency_depth: 依赖深度（上游服务故障传播链越长越有价值）
        """
    
    def to_experiment_yamls(self, hypotheses: list[Hypothesis], output_dir: str):
        """将假设转化为实验 YAML（替代 gen_template.py 的部分功能）"""
```

#### 数据模型：`code/agents/models.py`

```python
@dataclass
class Hypothesis:
    id: str                          # 唯一 ID
    title: str                       # 简短标题
    description: str                 # 详细描述
    steady_state: str                # 稳态假设（"成功率 >= 99%"）
    fault_scenario: str              # 故障场景描述
    expected_impact: str             # 预期影响
    failure_domain: str              # compute | data | network | dependencies | resources
    target_services: list[str]       # 目标服务（逻辑名）
    target_resources: list[str]      # 目标资源类型
    backend: str                     # fis | chaosmesh
    priority: int                    # 1 = 最高优先级
    priority_scores: dict            # {business_impact, blast_radius, feasibility, learning_value}
    status: str                      # draft | tested | validated | invalidated
    linked_experiments: list[str]    # 关联实验 ID
    generated_by: str                # "hypothesis-agent-v1"
    generated_at: str                # ISO timestamp
    source_context: dict             # 生成时的图谱/代码上下文
```

#### 存储：`code/hypotheses.json`

```json
{
  "generated_at": "2026-03-20T...",
  "generator": "hypothesis-agent-v1",
  "model": "anthropic.claude-sonnet-4-20250514",
  "graph_snapshot": {
    "vertices": 521,
    "edges": 1382,
    "services": 14
  },
  "hypotheses": [
    {
      "id": "H001",
      "title": "petsearch 单 AZ 故障时搜索降级",
      "failure_domain": "compute",
      "priority": 1,
      "...": "..."
    }
  ]
}
```

### Neptune 查询模板

```gremlin
# 1. 获取完整服务依赖图（假设生成的核心输入）
g.V().hasLabel('Microservice')
  .project('name','tier','deps','callers','resources')
  .by('name')
  .by('recovery_priority')
  .by(out('DependsOn').values('name').fold())
  .by(in('DependsOn').values('name').fold())
  .by(out('RunsOn','DependsOn').hasLabel('LambdaFunction','RDSCluster','DynamoDBTable','SQSQueue').values('name','arn').fold())

# 2. 获取历史故障模式（学习价值评估用）
g.V().hasLabel('Incident')
  .project('service','type','duration','impact')
  .by(out('AffectedService').values('name'))
  .by('incident_type')
  .by('duration_minutes')
  .by('impact_level')

# 3. 获取 AZ 分布（网络/节点故障场景用）
g.V().hasLabel('AvailabilityZone')
  .project('az','subnets','instances')
  .by('name')
  .by(in('InAZ').hasLabel('Subnet').count())
  .by(in('InAZ').hasLabel('EC2Instance').count())
```

### LLM Prompt 结构

```
你是混沌工程假设专家。基于以下服务拓扑和依赖关系，生成混沌工程假设。

## 服务拓扑
{neptune_topology_json}

## 历史实验结果
{dynamodb_history}

## 历史故障事件
{neptune_incidents}

## 要求
1. 覆盖 5 大故障域：compute, data, network, dependencies, resources
2. 每个假设必须包含：稳态假设、故障场景、预期影响、验证标准
3. 优先关注 Tier0/Tier1 服务
4. 考虑级联故障（A 依赖 B，B 故障时 A 如何表现）
5. 避免生成已经验证过的重复假设
6. 按 JSON 格式输出
```

### 实现优先级
1. 先做 Neptune 图谱 → LLM 生成假设（核心链路）
2. 再加代码仓库分析（可选增强）
3. 最后加历史实验去重（迭代优化）

---

## Phase 2：Learning Agent（闭环学习）

### 目标
分析实验历史，生成改进建议，自动产出下一轮假设，形成闭环。

### 设计

#### 新文件：`code/agents/learning_agent.py`

```python
class LearningAgent:
    """
    实验结果学习 Agent。
    
    输入：
    1. DynamoDB 实验历史（PASSED/FAILED/ABORTED）
    2. Neptune 图谱当前状态
    3. 现有假设库（hypotheses.json）
    
    输出：
    - learning_report.md（分析报告）
    - 更新 hypotheses.json（新假设 + 状态更新）
    - Neptune 图谱更新（resilience_score、weakness_pattern）
    """
    
    def analyze(self, 
                experiments: list[dict],     # DynamoDB 历史
                graph_context: dict,         # Neptune 当前图谱
                hypotheses: list[Hypothesis] # 现有假设
                ) -> LearningReport:
        """
        分析流程：
        1. 统计实验通过率/失败率 by 服务/故障域
        2. 识别重复失败模式（同一服务连续失败 → 系统性问题）
        3. 发现未覆盖的故障域（有服务但没实验）
        4. 评估恢复时间趋势（越来越快 = 弹性提升）
        5. 生成改进建议
        """
    
    def iterate_hypotheses(self, 
                           report: LearningReport,
                           existing: list[Hypothesis]
                           ) -> list[Hypothesis]:
        """
        基于分析结果迭代假设：
        - 标记已验证的假设为 validated
        - 标记失败的假设为 needs_attention
        - 生成后续假设（更深层/更极端的故障场景）
        - 填补未覆盖的故障域
        """
    
    def update_graph(self, report: LearningReport):
        """
        更新 Neptune 图谱：
        - 服务 resilience_score（多次实验加权平均）
        - weakness_pattern（已知弱点标签）
        - last_tested_at（最后测试时间）
        - test_coverage（已覆盖的故障域）
        """
```

#### 学习维度

```python
@dataclass
class LearningReport:
    # 统计
    total_experiments: int
    pass_rate: float
    avg_recovery_seconds: float
    
    # 按服务分析
    service_stats: dict[str, ServiceStats]  # {service: {pass_rate, avg_recovery, tested_domains}}
    
    # 模式识别
    repeated_failures: list[FailurePattern]  # 重复失败模式
    coverage_gaps: list[CoverageGap]         # 未覆盖的故障域
    improvement_trends: list[Trend]          # 改善趋势
    
    # 建议
    recommendations: list[Recommendation]    # 改进建议
    new_hypotheses: list[Hypothesis]         # 新生成的假设
    
    # 图谱更新
    graph_updates: list[GraphUpdate]         # 需要写回 Neptune 的更新
```

### 闭环流程

```
                    ┌─────────────────────────┐
                    │                         │
                    ▼                         │
        Hypothesis Agent                     │
        (生成/迭代假设)                        │
                    │                         │
                    ▼                         │
        gen_template.py                      │
        (假设 → 实验 YAML)                    │
                    │                         │
                    ▼                         │
        ExperimentRunner                     │
        (5 Phase 执行)                        │
                    │                         │
                    ▼                         │
        Learning Agent  ──────────────────────┘
        (分析结果 → 新假设)
```

---

## Phase 3：Workflow Orchestrator（批量编排）

### 目标
支持按策略批量编排多个实验，取代手动逐个执行。

### 设计

#### 新文件：`code/orchestrator.py`

```python
class WorkflowOrchestrator:
    """
    实验批量编排器。
    
    执行策略：
    1. by_tier: 按 Tier 顺序（Tier2 → Tier1 → Tier0），低风险先行
    2. by_priority: 按假设优先级顺序
    3. by_domain: 按故障域分组，每组内并行
    4. full_suite: 完整回归套件
    """
    
    def run_suite(self,
                  experiments: list[str],      # YAML 路径列表
                  strategy: str = "by_tier",   # 编排策略
                  max_parallel: int = 1,       # 并行数（默认串行）
                  stop_on_failure: bool = True, # 失败停止
                  cooldown: int = 60,          # 实验间冷却时间（秒）
                  tags: dict = None,           # 资源过滤 tag
                  dry_run: bool = False
                  ) -> SuiteResult:
        """
        执行流程：
        1. 加载所有实验 YAML
        2. 按策略排序
        3. resolve 所有目标（一次性，写入 targets-*.json）
        4. 逐个/并行执行
        5. 汇总结果 → 调用 Learning Agent 分析
        """
    
    def generate_and_run(self,
                         max_hypotheses: int = 20,
                         top_n: int = 5,
                         **kwargs
                         ) -> SuiteResult:
        """
        端到端自动化：
        1. Hypothesis Agent 生成假设
        2. 取 top_n 个优先级最高的
        3. 生成实验 YAML
        4. 执行实验
        5. Learning Agent 分析
        """
```

#### CLI 入口扩展

```bash
# 批量执行
python3 main.py suite --dir experiments/tier0/ --strategy by_tier
python3 main.py suite --dir experiments/ --strategy by_priority --top 5

# 端到端自动化
python3 main.py auto --max-hypotheses 30 --top 5 --dry-run
python3 main.py auto --max-hypotheses 30 --top 5 --tags "cluster=PetSite"

# 学习分析
python3 main.py learn --service petsite --limit 20
python3 main.py learn --all --output learning_report.md
```

---

## Phase 4：Tag-based Resource Filtering

### 目标
支持按 AWS 资源 tag 过滤目标，避免误伤非目标工作负载。

### 改动

#### `runner/target_resolver.py`

```python
class TargetResolver:
    def __init__(self, tags: dict = None):
        """
        tags: {"Environment": "prod", "Application": "PetSite"}
        所有 AWS API 发现会附加 tag 过滤条件
        """
        self._tags = tags or {}
    
    def _find_lambda_arn(self, service_name: str) -> Optional[str]:
        # 现有逻辑 + tag 过滤
        for fn in paginator.paginate():
            if service_name.lower() in fn["FunctionName"].lower():
                if self._tags and not self._match_tags(fn["FunctionArn"]):
                    continue  # tag 不匹配，跳过
                return fn["FunctionArn"]
    
    def _match_tags(self, resource_arn: str) -> bool:
        """检查资源 tag 是否匹配所有 filter tag"""
        # 用 resourcegroupstaggingapi 或对应服务的 list-tags API
```

#### Neptune 图谱增强

```gremlin
# 给 Microservice 顶点添加 tag 属性
g.V().has('Microservice','name','petsearch')
  .property('tags_environment','prod')
  .property('tags_application','PetSite')
```

---

## Phase 5：Observability 增强

### 目标
结构化日志 + CloudWatch 集成，方便调试和审计。

### 改动

#### 新文件：`code/runner/observability.py`

```python
import structlog

def get_logger(agent_name: str):
    """获取结构化 logger"""
    return structlog.get_logger(
        agent=agent_name,
        output_format="json",  # CloudWatch 友好
    )

# 在 runner.py 中使用
logger = get_logger("experiment-runner")
logger.info("phase_started", phase=0, experiment=exp.name, backend=exp.backend)
logger.info("target_resolved", service="statusupdater", arn="arn:aws:lambda:...", source="neptune")
logger.error("stop_condition_triggered", condition="success_rate < 30%", current_value=25.3)
```

### 新依赖
```
structlog>=23.0.0
```

---

## 实施路线图

| Phase | 内容 | 预计工作量 | 优先级 |
|-------|------|-----------|--------|
| **Phase 1** | Hypothesis Agent（AI 假设生成 + 优先级排序） | 2-3 天 | 🔴 最高 |
| **Phase 3** | Workflow Orchestrator（批量编排） | 1 天 | 🟠 高 |
| **Phase 2** | Learning Agent（闭环学习） | 2 天 | 🟡 中 |
| **Phase 4** | Tag-based Filtering | 0.5 天 | 🟢 低 |
| **Phase 5** | Observability 增强 | 0.5 天 | 🟢 低 |

### Phase 1 详细拆分（2-3 天）

| 步骤 | 任务 | 时间 |
|------|------|------|
| 1.1 | 数据模型（Hypothesis dataclass） | 2h |
| 1.2 | Neptune 图谱查询模块（拓扑 + 依赖 + 历史故障） | 4h |
| 1.3 | Bedrock LLM 调用封装（prompt + response parsing） | 4h |
| 1.4 | 假设生成主流程（graph → LLM → hypotheses.json） | 4h |
| 1.5 | 优先级排序（LLM 评估 + 规则加权） | 3h |
| 1.6 | gen_template.py 改造（从假设库生成 YAML） | 3h |
| 1.7 | CLI 集成 + 测试 | 2h |

---

## 设计原则

1. **图谱是核心，LLM 是增强** — Neptune 图谱提供确定性的拓扑和依赖数据，LLM 负责 reasoning 和生成，不用 LLM 做执行决策
2. **确定性执行，智能规划** — 5 Phase 引擎保持代码级确定性（Stop Condition 不靠 LLM 判断），Hypothesis/Learning Agent 负责智能规划
3. **渐进增强，不破坏现有** — 所有新功能是增量添加，现有 `main.py run --file` 流程完全不变
4. **审计留底** — hypotheses.json / targets-*.json / learning_report.md 全部留文件，可追溯
5. **LLM 调用可离线** — Agent 生成的 hypotheses.json 和 YAML 是静态文件，执行时不依赖 LLM
