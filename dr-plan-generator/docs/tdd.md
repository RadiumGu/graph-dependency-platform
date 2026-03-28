# DR Plan Generator — 技术设计文档（TDD）

> 版本：v1.0
> 日期：2026-03-28
> 状态：草案
> 前置文档：[PRD](./prd.md)

---

## 1. 概述

### 1.1 目标

基于 Neptune 知识图谱，自动生成分层、有序、可执行、可回滚的容灾切换计划。

### 1.2 设计原则

| 原则 | 说明 |
|------|------|
| **图谱驱动** | 一切依赖关系从 Neptune 图谱获取，不硬编码拓扑 |
| **分层切换** | 数据层 → 计算层 → 流量层，严格按层级顺序 |
| **可并行优化** | 同层内无依赖关系的步骤可并行执行 |
| **每步可回滚** | 每个 Step 附带回滚命令，支持任意断点回退 |
| **CLI 独立** | main.py 纯参数驱动，不依赖任何 AI 框架 |
| **Agent 通用** | AGENT.md 一份指令适配 OpenClaw / Claude Code / kiro-cli |

### 1.3 系统边界

```
                    ┌─────────────────────────────┐
                    │     dr-plan-generator        │
                    │                              │
  Neptune ─────────►│  graph/    → 图谱查询+分析    │
  (Q12-Q16)         │  planner/  → 计划生成        │──► plans/*.json
                    │  assessment/→ 影响评估        │──► plans/*.md
  AWS APIs ────────►│  validation/→ 验证+导出       │──► chaos experiments
  (实时状态补充)     │  output/   → 渲染输出        │
                    │                              │
                    └─────────────────────────────┘
```

---

## 2. 架构设计

### 2.1 模块架构

```
main.py (CLI 入口)
    │
    ├── plan 命令 ──────────────────────────────────────────┐
    │   │                                                   │
    │   ▼                                                   ▼
    │   graph/graph_analyzer.py                    planner/plan_generator.py
    │   ├─ extract_affected_subgraph()             ├─ generate_plan()
    │   ├─ classify_by_layer()                     ├─ build_phases()
    │   ├─ topological_sort_within_layer()         └─ attach_commands()
    │   ├─ find_critical_path()                         │
    │   └─ detect_parallel_groups()                     ▼
    │       │                                    planner/step_builder.py
    │       ▼                                    ├─ build_rds_step()
    │   graph/queries.py                         ├─ build_dynamodb_step()
    │   ├─ q12_az_dependency_tree()              ├─ build_eks_step()
    │   ├─ q13_data_layer_topology()             ├─ build_lambda_step()
    │   ├─ q14_cross_region_resources()          ├─ build_alb_step()
    │   ├─ q15_critical_path()                   └─ build_route53_step()
    │   └─ q16_single_point_of_failure()
    │
    ├── assess 命令 ────► assessment/impact_analyzer.py
    │                     ├─ assess_impact()
    │                     ├─ assessment/rto_estimator.py
    │                     └─ assessment/spof_detector.py
    │
    ├── validate 命令 ──► validation/plan_validator.py
    │                     ├─ check_cycles()
    │                     ├─ check_completeness()
    │                     └─ check_ordering()
    │
    ├── rollback 命令 ──► planner/rollback_generator.py
    │                     └─ generate_rollback()
    │
    └── export-chaos ───► validation/chaos_exporter.py
                          └─ export_to_chaos_yaml()
```

### 2.2 数据流

```
[用户输入]
  scope=az, source=apne1-az1, target=apne1-az2,apne1-az4
    │
    ▼
[graph/queries.py] ── Q12: 查 AZ1 所有资源 + 依赖链
    │                  Q13: 数据层拓扑
    │                  Q16: 单点故障检测
    ▼
[graph/graph_analyzer.py]
    │  1. extract_affected_subgraph() → 受影响子图
    │  2. classify_by_layer() → L0/L1/L2/L3 分层
    │  3. topological_sort_within_layer() → 层内排序
    │  4. detect_parallel_groups() → 并行组标记
    │  5. find_critical_path() → 关键路径 + RTO 估算
    ▼
[planner/plan_generator.py]
    │  1. build_phases() → Phase 0-4
    │  2. 遍历分层排序结果，为每个节点：
    │     → step_builder 生成 command/validation/rollback
    │  3. attach_gate_conditions() → Phase 间门控
    │  4. 计算 estimated_rto / estimated_rpo
    ▼
[output/] → Markdown + JSON 输出
```


---

## 3. Neptune 查询设计（Q12–Q16）

### 3.1 复用现有基础

复用 `rca/neptune/neptune_client.py` 的 SigV4 签名 HTTP 客户端和 openCypher 查询模式。DR 模块创建自己的 `graph/neptune_client.py`，从 shared 基类继承。

```python
# graph/neptune_client.py
import os, json, boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

NEPTUNE_ENDPOINT = os.environ.get('NEPTUNE_ENDPOINT', '')
NEPTUNE_PORT = int(os.environ.get('NEPTUNE_PORT', '8182'))
REGION = os.environ.get('REGION', 'ap-northeast-1')

def query(cypher: str, params: dict = None) -> list:
    """执行 openCypher，返回 results 列表"""
    # SigV4 签名 + HTTPS POST /openCypher
    # 与 rca/neptune/neptune_client.py 相同模式
    ...
```

### 3.2 新增查询

#### Q12: AZ 依赖树

```cypher
// 给定 AZ，查询所有部署在该 AZ 的资源及上下游依赖
MATCH (az:AvailabilityZone {name: $az_name})
      <-[:LocatedIn]-(resource)
RETURN resource.name AS name, labels(resource)[0] AS type,
       resource.recovery_priority AS tier,
       resource.state AS state
UNION
// 扩展：这些资源依赖的数据层
MATCH (az:AvailabilityZone {name: $az_name})
      <-[:LocatedIn]-(resource)-[:AccessesData|DependsOn|WritesTo]->(data_resource)
RETURN data_resource.name AS name, labels(data_resource)[0] AS type,
       data_resource.recovery_priority AS tier,
       data_resource.state AS state
```

#### Q13: 数据层拓扑

```cypher
// 所有数据存储及其被哪些服务依赖
MATCH (svc)-[:AccessesData|DependsOn|WritesTo]->(ds)
WHERE labels(ds)[0] IN ['RDSCluster', 'RDSInstance', 'DynamoDBTable',
                         'S3Bucket', 'SQSQueue', 'NeptuneCluster']
RETURN ds.name AS data_store, labels(ds)[0] AS ds_type,
       ds.az AS ds_az,
       collect(DISTINCT svc.name) AS dependent_services,
       ds.recovery_priority AS tier
```

#### Q14: 跨 Region 资源

```cypher
// 有跨 Region 副本配置的资源
MATCH (r)-[:ReplicatedTo]->(replica)
RETURN r.name AS source_name, labels(r)[0] AS type,
       r.az AS source_az,
       replica.name AS replica_name, replica.az AS replica_az
UNION
// DynamoDB Global Table
MATCH (dt:DynamoDBTable)
WHERE dt.global_table = true
RETURN dt.name AS source_name, 'DynamoDBTable' AS type,
       dt.az AS source_az,
       dt.replica_regions AS replica_name, '' AS replica_az
```

#### Q15: 关键路径（Tier0 最长依赖链）

```cypher
// Tier0 服务的完整依赖链深度
MATCH path = (svc:Microservice)-[:Calls|DependsOn*1..10]->(dep)
WHERE svc.recovery_priority = 'Tier0'
RETURN svc.name AS service,
       length(path) AS depth,
       [n IN nodes(path) | n.name] AS chain,
       [n IN nodes(path) | labels(n)[0]] AS types
ORDER BY depth DESC
```

#### Q16: 单点故障检测

```cypher
// 只部署在单个 AZ 且被多个服务依赖的资源
MATCH (resource)<-[:AccessesData|DependsOn|WritesTo|RunsOn]-(svc)
WITH resource, collect(DISTINCT svc.name) AS services, count(DISTINCT svc) AS svc_count
WHERE svc_count >= 2
  AND size([(resource)-[:LocatedIn]->(az:AvailabilityZone) | az.name]) = 1
RETURN resource.name AS resource_name, labels(resource)[0] AS type,
       [(resource)-[:LocatedIn]->(az) | az.name][0] AS single_az,
       services, svc_count
ORDER BY svc_count DESC
```

---

## 4. 图谱分析引擎（graph/graph_analyzer.py）

### 4.1 核心算法

```python
class GraphAnalyzer:
    """依赖图谱分析引擎"""

    # 资源类型到切换层级的映射
    LAYER_MAP = {
        # L0 - 数据层（最先切换）
        'RDSCluster': 'L0', 'RDSInstance': 'L0',
        'DynamoDBTable': 'L0', 'NeptuneCluster': 'L0',
        'NeptuneInstance': 'L0', 'S3Bucket': 'L0',
        'SQSQueue': 'L0', 'SNSTopic': 'L0',
        # L1 - 基础设施层
        'EC2Instance': 'L1', 'EKSCluster': 'L1',
        'Pod': 'L1', 'SecurityGroup': 'L1',
        # L2 - 应用层
        'K8sService': 'L2', 'Microservice': 'L2',
        'LambdaFunction': 'L2', 'StepFunction': 'L2',
        'BusinessCapability': 'L2',
        # L3 - 流量层（最后切换）
        'LoadBalancer': 'L3', 'TargetGroup': 'L3',
        'ListenerRule': 'L3',
    }

    def extract_affected_subgraph(self, scope, source) -> dict:
        """
        根据故障范围提取受影响子图
        Returns: {nodes: [...], edges: [...]}
        """
        if scope == 'az':
            nodes = queries.q12_az_dependency_tree(source)
        elif scope == 'region':
            # Region 下所有 AZ 的资源
            nodes = queries.q12_az_dependency_tree_by_region(source)
        elif scope == 'service':
            # 指定服务 + 其依赖链
            nodes = queries.q1_blast_radius(source)
        return self._enrich_with_edges(nodes)

    def classify_by_layer(self, subgraph) -> dict:
        """
        将节点按切换层级分类
        Returns: {'L0': [...], 'L1': [...], 'L2': [...], 'L3': [...]}
        """
        layers = {'L0': [], 'L1': [], 'L2': [], 'L3': []}
        for node in subgraph['nodes']:
            layer = self.LAYER_MAP.get(node['type'], 'L2')
            layers[layer].append(node)
        return layers

    def topological_sort_within_layer(self, layer_nodes, edges) -> list:
        """
        层内拓扑排序
        被依赖的资源排前面（先切换）
        使用 Kahn's algorithm
        """
        # 构建邻接表（只包含同层内的边）
        in_degree = {n['name']: 0 for n in layer_nodes}
        adj = {n['name']: [] for n in layer_nodes}
        node_names = set(n['name'] for n in layer_nodes)

        for edge in edges:
            if edge['from'] in node_names and edge['to'] in node_names:
                adj[edge['from']].append(edge['to'])
                in_degree[edge['to']] += 1

        # Kahn's algorithm
        queue = [n for n in in_degree if in_degree[n] == 0]
        result = []
        while queue:
            # 同 in_degree 按 Tier 排序（Tier0 先）
            queue.sort(key=lambda x: self._tier_priority(x))
            node = queue.pop(0)
            result.append(node)
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        return result

    def detect_parallel_groups(self, sorted_nodes, edges) -> list:
        """
        识别可并行执行的步骤组
        无依赖关系的同层节点可以并行
        Returns: [['group-1', [node_a, node_b]], ['group-2', [node_c]], ...]
        """
        ...

    def find_critical_path(self, layers, edges) -> dict:
        """
        找到从 L0 到 L3 的最长路径（决定最小 RTO）
        Returns: {path: [...], estimated_minutes: int}
        """
        ...
```

### 4.2 环路检测

```python
def detect_cycles(self, nodes, edges) -> list:
    """
    检测依赖环路（不应存在于切换计划中）
    使用 DFS 染色法
    Returns: 环路列表，空列表表示无环
    """
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n['name']: WHITE for n in nodes}
    cycles = []

    def dfs(node, path):
        color[node] = GRAY
        path.append(node)
        for neighbor in adj.get(node, []):
            if color[neighbor] == GRAY:
                cycle_start = path.index(neighbor)
                cycles.append(path[cycle_start:] + [neighbor])
            elif color[neighbor] == WHITE:
                dfs(neighbor, path)
        path.pop()
        color[node] = BLACK

    for node in color:
        if color[node] == WHITE:
            dfs(node, [])
    return cycles
```


---

## 5. 切换计划生成器（planner/）

### 5.1 Plan Generator

```python
# planner/plan_generator.py

class PlanGenerator:
    """切换计划生成主引擎"""

    def __init__(self, analyzer: GraphAnalyzer, step_builder: StepBuilder):
        self.analyzer = analyzer
        self.step_builder = step_builder

    def generate_plan(self, scope, source, target, exclude=None,
                      options=None) -> DRPlan:
        """
        生成完整切换计划
        1. 图谱分析 → 受影响子图
        2. 分层 + 排序
        3. 构建 Phase 0-4
        4. 为每个节点生成 Step
        5. 计算 RTO/RPO
        """
        # Step 1: 提取受影响子图
        subgraph = self.analyzer.extract_affected_subgraph(scope, source)

        # 排除指定服务
        if exclude:
            subgraph = self._filter_excluded(subgraph, exclude)

        # Step 2: 分层 + 排序
        layers = self.analyzer.classify_by_layer(subgraph)
        sorted_layers = {}
        for layer_name, nodes in layers.items():
            sorted_layers[layer_name] = \
                self.analyzer.topological_sort_within_layer(nodes, subgraph['edges'])

        # Step 3: 构建 Phases
        phases = self._build_phases(sorted_layers, source, target, options)

        # Step 4: 影响评估
        impact = ImpactAnalyzer().assess_impact(subgraph)

        # Step 5: 计算 RTO/RPO
        rto = RTOEstimator().estimate(phases)
        rpo = self._estimate_rpo(layers['L0'])

        return DRPlan(
            plan_id=f"dr-{scope}-{int(time.time())}",
            created_at=datetime.utcnow().isoformat(),
            scope=scope,
            source=source,
            target=target,
            affected_services=[n['name'] for n in subgraph['nodes']
                               if n['type'] in ('Microservice', 'K8sService')],
            affected_resources=[n['name'] for n in subgraph['nodes']],
            phases=phases,
            rollback_phases=[],  # 由 rollback_generator 填充
            impact_assessment=impact,
            estimated_rto=rto,
            estimated_rpo=rpo,
            validation_status='pending',
            graph_snapshot_time=datetime.utcnow().isoformat(),
        )

    def _build_phases(self, sorted_layers, source, target, options) -> list:
        """构建 5 个标准 Phase"""
        phases = []

        # Phase 0: Pre-flight
        phases.append(self._build_preflight_phase(source, target, sorted_layers))

        # Phase 1: Data Layer (L0)
        if sorted_layers.get('L0'):
            phases.append(self._build_data_phase(sorted_layers['L0'], source, target))

        # Phase 2: Compute Layer (L1 + L2)
        compute_nodes = sorted_layers.get('L1', []) + sorted_layers.get('L2', [])
        if compute_nodes:
            phases.append(self._build_compute_phase(compute_nodes, source, target))

        # Phase 3: Network/Traffic Layer (L3)
        if sorted_layers.get('L3'):
            phases.append(self._build_network_phase(sorted_layers['L3'], source, target))

        # Phase 4: Validation
        phases.append(self._build_validation_phase(sorted_layers))

        return phases
```

### 5.2 Step Builder（按资源类型生成命令）

```python
# planner/step_builder.py

class StepBuilder:
    """为每种资源类型生成具体的切换/验证/回滚命令"""

    def build_step(self, node, source, target, context) -> DRStep:
        """根据资源类型分发到具体 builder"""
        builder = getattr(self, f'_build_{node["type"].lower()}_step', None)
        if builder:
            return builder(node, source, target, context)
        return self._build_generic_step(node, source, target)

    def _build_rdscluster_step(self, node, source, target, ctx) -> DRStep:
        """RDS/Aurora 切换步骤"""
        cluster_id = node['name']
        return DRStep(
            step_id=f"rds-{cluster_id}",
            order=0,
            resource_type='RDSCluster',
            resource_id=node.get('id', ''),
            resource_name=cluster_id,
            action='promote_read_replica',
            command=f"aws rds failover-db-cluster "
                    f"--db-cluster-identifier {cluster_id} "
                    f"--region {target}",
            validation=f"aws rds describe-db-clusters "
                       f"--db-cluster-identifier {cluster_id} "
                       f"--region {target} "
                       f"--query 'DBClusters[0].Status' --output text",
            expected_result="available",
            rollback_command=f"aws rds failover-db-cluster "
                            f"--db-cluster-identifier {cluster_id} "
                            f"--region {source}",
            estimated_time=300,  # 5 min for Aurora failover
            requires_approval=True,
            tier=node.get('tier'),
            dependencies=[],
        )

    def _build_dynamodbtable_step(self, node, source, target, ctx) -> DRStep:
        """DynamoDB Global Table 切换"""
        table_name = node['name']
        return DRStep(
            step_id=f"ddb-{table_name}",
            order=0,
            resource_type='DynamoDBTable',
            resource_id=node.get('id', ''),
            resource_name=table_name,
            action='switch_global_table_region',
            command=f"# DynamoDB Global Table: 将写入端点切换到 {target}\n"
                    f"# 应用层配置更新（环境变量 / Parameter Store）\n"
                    f"aws ssm put-parameter --name '/petsite/dynamodb-region' "
                    f"--value '{target}' --overwrite --region {target}",
            validation=f"aws dynamodb describe-table "
                       f"--table-name {table_name} --region {target} "
                       f"--query 'Table.TableStatus' --output text",
            expected_result="ACTIVE",
            rollback_command=f"aws ssm put-parameter --name '/petsite/dynamodb-region' "
                            f"--value '{source}' --overwrite --region {source}",
            estimated_time=60,
            requires_approval=True,
            tier=node.get('tier'),
            dependencies=[],
        )

    def _build_microservice_step(self, node, source, target, ctx) -> DRStep:
        """EKS 微服务切换（按 Tier 排序）"""
        svc_name = node['name']
        tier = node.get('tier', 'Tier2')
        return DRStep(
            step_id=f"svc-{svc_name}",
            order=0,
            resource_type='Microservice',
            resource_id=node.get('id', ''),
            resource_name=svc_name,
            action='scale_up_and_verify',
            command=f"kubectl scale deployment {svc_name} --replicas=3 "
                    f"--context {target}-cluster\n"
                    f"kubectl rollout status deployment/{svc_name} "
                    f"--timeout=120s --context {target}-cluster",
            validation=f"kubectl get deployment {svc_name} "
                       f"--context {target}-cluster "
                       f"-o jsonpath='{{.status.readyReplicas}}'",
            expected_result="3",
            rollback_command=f"kubectl scale deployment {svc_name} --replicas=0 "
                            f"--context {target}-cluster",
            estimated_time=120,
            requires_approval=(tier == 'Tier0'),
            tier=tier,
            dependencies=[],  # 由 plan_generator 填充
        )

    def _build_loadbalancer_step(self, node, source, target, ctx) -> DRStep:
        """ALB/NLB 流量切换"""
        lb_name = node['name']
        return DRStep(
            step_id=f"lb-{lb_name}",
            order=0,
            resource_type='LoadBalancer',
            resource_id=node.get('id', ''),
            resource_name=lb_name,
            action='verify_health_and_switch_dns',
            command=f"# 1. 验证目标 ALB 健康\n"
                    f"aws elbv2 describe-target-health "
                    f"--target-group-arn $TG_ARN --region {target}\n"
                    f"# 2. Route 53 DNS 切换\n"
                    f"aws route53 change-resource-record-sets "
                    f"--hosted-zone-id $ZONE_ID "
                    f"--change-batch file://dns-failover.json",
            validation=f"dig +short petsite.example.com",
            expected_result="<target ALB DNS>",
            rollback_command=f"aws route53 change-resource-record-sets "
                            f"--hosted-zone-id $ZONE_ID "
                            f"--change-batch file://dns-rollback.json",
            estimated_time=180,
            requires_approval=True,
            tier=None,
            dependencies=[],
        )
```

### 5.3 Pre-flight Phase 设计

```python
def _build_preflight_phase(self, source, target, layers) -> DRPhase:
    """Phase 0: 切换前预检"""
    steps = []

    # Step 0.1: 备站点连通性检查
    steps.append(DRStep(
        step_id='preflight-connectivity',
        action='check_target_connectivity',
        command=f"aws sts get-caller-identity --region {target}",
        validation="echo $?",
        expected_result="0",
        ...
    ))

    # Step 0.2: 数据同步状态验证
    for node in layers.get('L0', []):
        if node['type'] == 'RDSCluster':
            steps.append(DRStep(
                step_id=f'preflight-repl-{node["name"]}',
                action='check_replication_lag',
                command=f"aws rds describe-db-clusters "
                        f"--db-cluster-identifier {node['name']} "
                        f"--region {source} "
                        f"--query 'DBClusters[0].ReplicationSourceIdentifier'",
                validation="# ReplicaLag < 阈值（默认 1000ms）",
                expected_result="ReplicaLag < 1000ms",
                requires_approval=False,
                ...
            ))

    # Step 0.3: DNS TTL 预降低
    steps.append(DRStep(
        step_id='preflight-dns-ttl',
        action='lower_dns_ttl',
        command="aws route53 change-resource-record-sets ... --ttl 60",
        validation="aws route53 get-resource-record-sets ... --query TTL",
        expected_result="60",
        ...
    ))

    return DRPhase(
        phase_id='phase-0',
        name='Pre-flight Check',
        layer='preflight',
        steps=steps,
        estimated_duration=sum(s.estimated_time for s in steps) // 60,
        gate_condition='All preflight checks passed, replication lag within threshold',
    )
```


---

## 6. 回滚计划生成器

### 6.1 设计原则

回滚 ≠ 简单反转。关键区别：

| 维度 | 切换 | 回滚 |
|------|------|------|
| **顺序** | 数据→计算→流量 | 流量→计算→数据 |
| **数据层** | promote replica | 需要重新建立复制关系 |
| **风险** | 计划内，可控 | 可能在故障中执行，风险更高 |
| **审批** | 按 Tier 分级 | 全部需要审批 |

### 6.2 实现

```python
# planner/rollback_generator.py

class RollbackGenerator:

    def generate_rollback(self, plan: DRPlan) -> List[DRPhase]:
        """
        从切换计划生成回滚计划
        1. Phase 顺序反转（流量→计算→数据）
        2. 每个 Step 使用 rollback_command
        3. 数据层步骤标记 requires_approval=True
        4. 添加数据一致性检查步骤
        """
        rollback_phases = []

        # 反转 Phase 顺序（跳过 Phase 0 preflight 和 Phase 4 validation）
        switchover_phases = [p for p in plan.phases
                            if p.layer not in ('preflight', 'validation')]
        switchover_phases.reverse()

        for phase in switchover_phases:
            rollback_steps = []
            # 反转步骤顺序
            for step in reversed(phase.steps):
                rollback_step = DRStep(
                    step_id=f"rollback-{step.step_id}",
                    order=0,
                    resource_type=step.resource_type,
                    resource_id=step.resource_id,
                    resource_name=step.resource_name,
                    action=f"rollback_{step.action}",
                    command=step.rollback_command,
                    validation=step.validation,  # 验证命令复用，期望值变回原始
                    expected_result=f"Original state of {step.resource_name}",
                    rollback_command="# Manual intervention required",
                    estimated_time=step.estimated_time,
                    requires_approval=True,  # 回滚全部需审批
                    tier=step.tier,
                    dependencies=[],
                )
                rollback_steps.append(rollback_step)

            rollback_phases.append(DRPhase(
                phase_id=f"rollback-{phase.phase_id}",
                name=f"Rollback: {phase.name}",
                layer=phase.layer,
                steps=rollback_steps,
                estimated_duration=phase.estimated_duration,
                gate_condition=f"All {phase.name} rollback steps verified",
            ))

        # 添加回滚后验证 Phase
        rollback_phases.append(self._build_rollback_validation_phase(plan))

        return rollback_phases
```

---

## 7. 影响评估模块（assessment/）

### 7.1 Impact Analyzer

```python
# assessment/impact_analyzer.py

class ImpactAnalyzer:

    def assess_impact(self, subgraph, scope, source) -> ImpactReport:
        """
        生成影响评估报告
        """
        nodes = subgraph['nodes']

        # 按 Tier 分组
        by_tier = {'Tier0': [], 'Tier1': [], 'Tier2': [], 'Unknown': []}
        for n in nodes:
            tier = n.get('tier', 'Unknown')
            by_tier.setdefault(tier, []).append(n)

        # 受影响业务能力
        capabilities = [n for n in nodes if n['type'] == 'BusinessCapability']

        # 单点故障
        spof = SPOFDetector().detect(subgraph)

        # RTO/RPO 估算
        rto = RTOEstimator().estimate_from_subgraph(subgraph)
        rpo = self._estimate_rpo(nodes)

        return ImpactReport(
            scope=scope,
            source=source,
            total_affected=len(nodes),
            by_tier=by_tier,
            affected_capabilities=capabilities,
            single_points_of_failure=spof,
            estimated_rto_minutes=rto,
            estimated_rpo_minutes=rpo,
            risk_matrix=self._build_risk_matrix(by_tier, spof),
        )
```

### 7.2 RTO Estimator

```python
# assessment/rto_estimator.py

class RTOEstimator:
    """基于切换步骤和历史数据估算 RTO"""

    # 各资源类型的默认切换时间（秒）
    DEFAULT_TIMES = {
        'RDSCluster': 300,       # Aurora failover ~5min
        'RDSInstance': 600,      # RDS reboot ~10min
        'DynamoDBTable': 60,     # Global Table 秒级
        'S3Bucket': 30,          # 验证 replication
        'SQSQueue': 30,          # 切换端点
        'Microservice': 120,     # rollout + health check
        'LambdaFunction': 30,    # 函数验证
        'LoadBalancer': 180,     # health check + DNS propagation
        'K8sService': 60,        # service endpoint 更新
    }

    def estimate(self, phases: list) -> int:
        """
        计算预估 RTO（分钟）
        考虑：串行步骤累加 + 并行步骤取最长 + Phase 间门控时间
        """
        total_seconds = 0
        for phase in phases:
            phase_seconds = self._estimate_phase(phase)
            total_seconds += phase_seconds
            total_seconds += 60  # Phase 间门控/验证时间
        return max(1, total_seconds // 60)

    def _estimate_phase(self, phase) -> int:
        """考虑并行组的 Phase 时间估算"""
        groups = {}
        serial_time = 0
        for step in phase.steps:
            if step.parallel_group:
                groups.setdefault(step.parallel_group, []).append(step.estimated_time)
            else:
                serial_time += step.estimated_time

        parallel_time = sum(max(times) for times in groups.values())
        return serial_time + parallel_time
```

### 7.3 SPOF Detector

```python
# assessment/spof_detector.py

class SPOFDetector:
    """单点故障检测"""

    def detect(self, subgraph) -> list:
        """
        检测单点故障风险
        - 单 AZ 部署的关键资源
        - 无跨 Region 副本的数据库
        - 单实例数据库（无 Multi-AZ）
        """
        spof_list = []

        # 从 Neptune Q16 查询结果补充
        q16_results = queries.q16_single_point_of_failure()
        for r in q16_results:
            spof_list.append({
                'resource': r['resource_name'],
                'type': r['type'],
                'risk': 'single_az',
                'az': r['single_az'],
                'impact': r['services'],
                'recommendation': f"部署到多个 AZ 或添加跨 Region 副本",
            })

        return spof_list
```

---

## 8. 计划验证（validation/）

### 8.1 静态验证

```python
# validation/plan_validator.py

class PlanValidator:

    def validate(self, plan: DRPlan) -> ValidationReport:
        """
        静态验证切换计划
        Returns: ValidationReport with pass/fail + issues list
        """
        issues = []

        # 1. 环路检测
        cycles = self._check_cycles(plan)
        if cycles:
            issues.append(Issue('CRITICAL', f'Dependency cycles detected: {cycles}'))

        # 2. 完整性检查
        missing = self._check_completeness(plan)
        if missing:
            issues.append(Issue('WARNING', f'Resources not covered: {missing}'))

        # 3. 顺序一致性
        ordering_violations = self._check_ordering(plan)
        if ordering_violations:
            issues.append(Issue('CRITICAL', f'Ordering violations: {ordering_violations}'))

        # 4. 回滚命令完整性
        no_rollback = [s for p in plan.phases for s in p.steps if not s.rollback_command]
        if no_rollback:
            issues.append(Issue('WARNING',
                f'{len(no_rollback)} steps missing rollback commands'))

        # 5. 图谱新鲜度
        graph_age = (datetime.utcnow() -
                     datetime.fromisoformat(plan.graph_snapshot_time)).total_seconds()
        if graph_age > 3600:  # > 1 hour
            issues.append(Issue('WARNING',
                f'Graph snapshot is {graph_age//60:.0f}min old, consider re-running ETL'))

        return ValidationReport(
            valid=all(i.severity != 'CRITICAL' for i in issues),
            issues=issues,
        )

    def _check_ordering(self, plan) -> list:
        """
        验证切换顺序不违反依赖关系
        规则：如果 A 依赖 B，则 B 必须在 A 之前切换
        """
        step_order = {}
        global_order = 0
        for phase in plan.phases:
            for step in phase.steps:
                step_order[step.resource_name] = global_order
                global_order += 1

        violations = []
        for phase in plan.phases:
            for step in phase.steps:
                for dep_id in step.dependencies:
                    if dep_id in step_order and step_order[dep_id] > step_order[step.resource_name]:
                        violations.append(
                            f"{step.resource_name} scheduled before its dependency {dep_id}")
        return violations
```

### 8.2 Chaos 验证导出

```python
# validation/chaos_exporter.py

class ChaosExporter:
    """将 DR 计划的关键假设导出为 chaos 实验 YAML"""

    def export(self, plan: DRPlan, output_dir: str) -> list:
        """
        为 DR 计划生成验证实验
        1. 数据层 failover 实验（RDS failover 时间验证）
        2. AZ 故障模拟（验证流量是否自动迁移）
        3. 服务恢复实验（验证 RTO 是否在预期内）
        """
        experiments = []

        # 为每个 RDS failover 步骤生成验证实验
        for phase in plan.phases:
            for step in phase.steps:
                if step.action == 'promote_read_replica':
                    exp = self._build_rds_failover_experiment(step, plan)
                    experiments.append(exp)

        # AZ 故障模拟
        if plan.scope == 'az':
            exp = self._build_az_failure_experiment(plan)
            experiments.append(exp)

        # 写入 YAML 文件
        for exp in experiments:
            path = os.path.join(output_dir, f"{exp['name']}.yaml")
            with open(path, 'w') as f:
                yaml.dump(exp, f, default_flow_style=False)

        return experiments

    def _build_rds_failover_experiment(self, step, plan) -> dict:
        """生成 FIS RDS failover 实验"""
        return {
            'name': f"dr-validate-{step.resource_name}-failover",
            'description': f"Validate RDS failover time for {step.resource_name}",
            'backend': 'fis',
            'target': {
                'service': step.resource_name,
                'resource_type': 'rds:cluster',
            },
            'fault': {
                'type': 'aws:rds:failover-db-cluster',
                'duration': '5m',
            },
            'steady_state': {
                'success_rate_threshold': 95,
            },
            'stop_conditions': [{
                'metric': 'success_rate',
                'threshold': 80,
            }],
            'rca': {'enabled': True},
            'tags': {'source': 'dr-plan-generator', 'plan_id': plan.plan_id},
        }
```


---

## 9. 输出渲染（output/）

### 9.1 Markdown 输出

```python
# output/markdown_renderer.py

class MarkdownRenderer:

    def render(self, plan: DRPlan) -> str:
        """渲染为可读的 Markdown DR 计划文档"""
        lines = []
        lines.append(f"# DR 切换计划 — {plan.scope.upper()} 级")
        lines.append(f"")
        lines.append(f"> 生成时间：{plan.created_at}")
        lines.append(f"> 故障范围：{plan.source} → 切换目标：{plan.target}")
        lines.append(f"> 预估 RTO：{plan.estimated_rto} 分钟")
        lines.append(f"> 预估 RPO：{plan.estimated_rpo} 分钟")
        lines.append(f"> 图谱快照：{plan.graph_snapshot_time}")
        lines.append(f"")

        # 影响摘要
        lines.append(f"## 影响摘要")
        lines.append(f"")
        lines.append(f"| 维度 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 受影响服务 | {len(plan.affected_services)} |")
        lines.append(f"| 受影响资源 | {len(plan.affected_resources)} |")
        lines.append(f"| Tier0 服务 | {len(plan.impact_assessment.by_tier.get('Tier0', []))} |")
        lines.append(f"")

        # SPOF 警告
        if plan.impact_assessment.single_points_of_failure:
            lines.append(f"### ⚠️ 单点故障风险")
            for spof in plan.impact_assessment.single_points_of_failure:
                lines.append(f"- **{spof['resource']}** ({spof['type']}) — "
                             f"仅在 {spof['az']}，影响 {len(spof['impact'])} 个服务")
            lines.append(f"")

        # 各 Phase
        for phase in plan.phases:
            lines.append(f"## {phase.phase_id}: {phase.name}")
            lines.append(f"")
            lines.append(f"预估耗时：{phase.estimated_duration} 分钟")
            lines.append(f"门控条件：{phase.gate_condition}")
            lines.append(f"")

            for i, step in enumerate(phase.steps, 1):
                approval = " 🔒 需审批" if step.requires_approval else ""
                parallel = f" (并行组: {step.parallel_group})" if step.parallel_group else ""
                lines.append(f"### Step {phase.phase_id}.{i}: "
                             f"{step.action} — {step.resource_name}"
                             f"{approval}{parallel}")
                lines.append(f"")
                lines.append(f"**资源类型**: {step.resource_type}")
                if step.tier:
                    lines.append(f"**服务等级**: {step.tier}")
                lines.append(f"**预估时间**: {step.estimated_time}s")
                lines.append(f"")
                lines.append(f"**执行命令**:")
                lines.append(f"```bash")
                lines.append(step.command)
                lines.append(f"```")
                lines.append(f"")
                lines.append(f"**验证**:")
                lines.append(f"```bash")
                lines.append(step.validation)
                lines.append(f"```")
                lines.append(f"预期结果: `{step.expected_result}`")
                lines.append(f"")
                lines.append(f"**回滚**:")
                lines.append(f"```bash")
                lines.append(step.rollback_command)
                lines.append(f"```")
                lines.append(f"")

        return '\n'.join(lines)
```

### 9.2 JSON 输出

```python
# output/json_renderer.py

class JSONRenderer:

    def render(self, plan: DRPlan) -> str:
        """序列化为 JSON（程序消费用）"""
        return json.dumps(dataclasses.asdict(plan), indent=2, ensure_ascii=False)
```

### 9.3 LLM 摘要生成

```python
# output/summary_generator.py

class SummaryGenerator:
    """调用 Bedrock Claude 生成管理层可读的执行摘要"""

    def generate(self, plan: DRPlan) -> str:
        prompt = f"""你是 AWS SRE 专家。基于以下 DR 切换计划，生成一份简洁的执行摘要。

切换范围：{plan.scope} 级，从 {plan.source} 切换到 {plan.target}
受影响服务：{len(plan.affected_services)} 个（其中 Tier0: {len(plan.impact_assessment.by_tier.get('Tier0', []))} 个）
切换步骤：{sum(len(p.steps) for p in plan.phases)} 步
预估 RTO：{plan.estimated_rto} 分钟
预估 RPO：{plan.estimated_rpo} 分钟

单点故障风险：
{json.dumps(plan.impact_assessment.single_points_of_failure, ensure_ascii=False)}

请生成：
1. 一段话摘要（给管理层看）
2. 关键风险点（最多3个）
3. 建议的审批节点
"""
        response = bedrock.invoke_model(
            modelId=BEDROCK_MODEL,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 1000,
            })
        )
        return json.loads(response['body'].read())['content'][0]['text']
```

---

## 10. CLI 入口（main.py）

```python
#!/usr/bin/env python3
"""DR Plan Generator — CLI 入口"""

import argparse
import json
import sys
import os

from graph.neptune_client import NeptuneClient
from graph.graph_analyzer import GraphAnalyzer
from graph import queries
from planner.plan_generator import PlanGenerator
from planner.rollback_generator import RollbackGenerator
from planner.step_builder import StepBuilder
from assessment.impact_analyzer import ImpactAnalyzer
from validation.plan_validator import PlanValidator
from validation.chaos_exporter import ChaosExporter
from output.markdown_renderer import MarkdownRenderer
from output.json_renderer import JSONRenderer
from output.summary_generator import SummaryGenerator


def cmd_plan(args):
    """生成切换计划"""
    analyzer = GraphAnalyzer()
    builder = StepBuilder()
    generator = PlanGenerator(analyzer, builder)

    plan = generator.generate_plan(
        scope=args.scope,
        source=args.source,
        target=args.target,
        exclude=args.exclude.split(',') if args.exclude else None,
    )

    # 自动生成回滚计划
    plan.rollback_phases = RollbackGenerator().generate_rollback(plan)

    # 静态验证
    report = PlanValidator().validate(plan)
    if not report.valid:
        print(f"⚠️  验证发现 {len(report.issues)} 个问题：", file=sys.stderr)
        for issue in report.issues:
            print(f"  [{issue.severity}] {issue.message}", file=sys.stderr)

    # 输出
    _output(plan, args)
    print(f"\n✅ 计划已生成: {plan.plan_id}", file=sys.stderr)
    print(f"   受影响服务: {len(plan.affected_services)}", file=sys.stderr)
    print(f"   切换步骤: {sum(len(p.steps) for p in plan.phases)}", file=sys.stderr)
    print(f"   预估 RTO: {plan.estimated_rto} 分钟", file=sys.stderr)


def cmd_assess(args):
    """影响评估"""
    analyzer = GraphAnalyzer()
    subgraph = analyzer.extract_affected_subgraph(args.scope, args.failure)
    impact = ImpactAnalyzer().assess_impact(subgraph, args.scope, args.failure)

    if args.format == 'json':
        print(json.dumps(dataclasses.asdict(impact), indent=2, ensure_ascii=False))
    else:
        print(MarkdownRenderer().render_impact(impact))


def cmd_validate(args):
    """验证已有计划"""
    with open(args.plan) as f:
        plan = DRPlan.from_dict(json.load(f))
    report = PlanValidator().validate(plan)
    print(f"验证结果: {'✅ PASS' if report.valid else '❌ FAIL'}")
    for issue in report.issues:
        print(f"  [{issue.severity}] {issue.message}")


def cmd_rollback(args):
    """生成回滚计划"""
    with open(args.plan) as f:
        plan = DRPlan.from_dict(json.load(f))
    plan.rollback_phases = RollbackGenerator().generate_rollback(plan)
    _output(plan, args, rollback_only=True)


def cmd_export_chaos(args):
    """导出为 chaos 验证实验"""
    with open(args.plan) as f:
        plan = DRPlan.from_dict(json.load(f))
    os.makedirs(args.output, exist_ok=True)
    experiments = ChaosExporter().export(plan, args.output)
    print(f"已导出 {len(experiments)} 个验证实验到 {args.output}")


def _output(plan, args, rollback_only=False):
    """统一输出处理"""
    fmt = getattr(args, 'format', 'markdown')
    out_dir = getattr(args, 'output_dir', 'plans')
    os.makedirs(out_dir, exist_ok=True)

    if fmt == 'json':
        content = JSONRenderer().render(plan)
        path = os.path.join(out_dir, f"{plan.plan_id}.json")
    else:
        content = MarkdownRenderer().render(plan)
        path = os.path.join(out_dir, f"{plan.plan_id}.md")

    with open(path, 'w') as f:
        f.write(content)
    print(content)


def main():
    parser = argparse.ArgumentParser(description='DR Plan Generator')
    sub = parser.add_subparsers(dest='command')

    # plan
    p = sub.add_parser('plan', help='Generate DR switchover plan')
    p.add_argument('--scope', required=True, choices=['region', 'az', 'service'])
    p.add_argument('--source', required=True, help='Failure source (region/az/service name)')
    p.add_argument('--target', required=True, help='DR target (region/az)')
    p.add_argument('--exclude', help='Comma-separated services to exclude')
    p.add_argument('--format', default='markdown', choices=['markdown', 'json'])
    p.add_argument('--output-dir', default='plans')
    p.add_argument('--non-interactive', action='store_true')

    # assess
    a = sub.add_parser('assess', help='Impact assessment')
    a.add_argument('--scope', required=True, choices=['region', 'az', 'service'])
    a.add_argument('--failure', required=True, help='Failure source')
    a.add_argument('--format', default='markdown', choices=['markdown', 'json'])

    # validate
    v = sub.add_parser('validate', help='Validate existing plan')
    v.add_argument('--plan', required=True, help='Path to plan JSON file')

    # rollback
    r = sub.add_parser('rollback', help='Generate rollback plan')
    r.add_argument('--plan', required=True, help='Path to plan JSON file')
    r.add_argument('--format', default='markdown', choices=['markdown', 'json'])

    # export-chaos
    e = sub.add_parser('export-chaos', help='Export plan as chaos validation experiments')
    e.add_argument('--plan', required=True, help='Path to plan JSON file')
    e.add_argument('--output', required=True, help='Output directory for experiment YAMLs')

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    cmds = {
        'plan': cmd_plan, 'assess': cmd_assess, 'validate': cmd_validate,
        'rollback': cmd_rollback, 'export-chaos': cmd_export_chaos,
    }
    cmds[args.command](args)


if __name__ == '__main__':
    main()
```


---

## 11. AGENT.md 设计（通用 Agent 指令）

### 11.1 文件定位

`dr-plan-generator/AGENT.md` — 纯 Markdown，不含任何框架特定语法。

### 11.2 各工具加载方式

**OpenClaw**:
```
# skills/dr-plan/SKILL.md
When user mentions DR plan, failover, disaster recovery, or 容灾切换:
1. Read dr-plan-generator/AGENT.md
2. Follow the instructions
```

**Claude Code**:
```markdown
# In project CLAUDE.md
## DR Plan Generator
When working on DR plans, read and follow `dr-plan-generator/AGENT.md`.
```

**kiro-cli**:
```markdown
# In .kiro/context.md or project instructions
For DR planning tasks, follow instructions in dr-plan-generator/AGENT.md
```

### 11.3 AGENT.md 内容结构

```markdown
# DR Plan Generator — Agent Instructions

## 激活条件
用户提到以下关键词时激活：
- 容灾切换 / DR 计划 / failover plan / 灾备 / 切换演练
- 影响评估 / 单点故障 / SPOF
- RTO / RPO / 回滚计划

## 工作目录
cd <project-root>/dr-plan-generator

## 交互流程

### Step 1: 理解需求
询问并确认：
- scope: region / az / service（必须）
- source: 故障源（必须）
- target: 切换目标（可基于图谱建议）

### Step 2: 影响评估（推荐先做）
运行: python3 main.py assess --scope <scope> --failure <source> --format json
解读输出，向用户展示：
- 受影响服务数量和 Tier 分布
- 单点故障风险
- 预估 RTO/RPO
如果发现 SPOF，主动警告用户

### Step 3: 生成计划
确认参数后运行:
python3 main.py plan --scope <scope> --source <source> --target <target> [--exclude ...] --format markdown
展示计划摘要：Phase 数、Step 数、预估 RTO

### Step 4: 迭代调整
用户可能要求：
- 排除某些服务 → 加 --exclude
- 调整切换策略 → 修改 step_builder 参数
- 查看某个 Phase 细节 → 从输出中摘取

### Step 5: 后续操作（询问用户）
- 生成回滚计划: python3 main.py rollback --plan <file>
- 导出 chaos 验证: python3 main.py export-chaos --plan <file> --output <dir>
- 验证计划: python3 main.py validate --plan <file>
- 生成 LLM 摘要（给管理层看）

## CLI 完整参考
（列出所有命令和参数）

## 常见问题
- Neptune 连接失败 → 检查 NEPTUNE_ENDPOINT 环境变量
- 图谱数据过旧 → 建议先跑一次 ETL: aws lambda invoke --function-name neptune-etl-from-aws ...
- 某个资源类型没有切换命令 → step_builder 会生成 generic step，提示用户手动补充
```

---

## 12. 数据模型（完整定义）

```python
# models.py

from dataclasses import dataclass, field
from typing import List, Optional

@dataclass
class DRStep:
    step_id: str
    order: int
    parallel_group: Optional[str] = None
    resource_type: str = ''
    resource_id: str = ''
    resource_name: str = ''
    action: str = ''
    command: str = ''
    validation: str = ''
    expected_result: str = ''
    rollback_command: str = ''
    estimated_time: int = 60          # 秒
    requires_approval: bool = False
    tier: Optional[str] = None
    dependencies: List[str] = field(default_factory=list)

@dataclass
class DRPhase:
    phase_id: str
    name: str
    layer: str                        # preflight / L0 / L1 / L2 / L3 / validation
    steps: List[DRStep] = field(default_factory=list)
    estimated_duration: int = 0       # 分钟
    gate_condition: str = ''

@dataclass
class ImpactReport:
    scope: str
    source: str
    total_affected: int = 0
    by_tier: dict = field(default_factory=dict)
    affected_capabilities: list = field(default_factory=list)
    single_points_of_failure: list = field(default_factory=list)
    estimated_rto_minutes: int = 0
    estimated_rpo_minutes: int = 0
    risk_matrix: dict = field(default_factory=dict)

@dataclass
class DRPlan:
    plan_id: str
    created_at: str
    scope: str
    source: str
    target: str
    affected_services: List[str] = field(default_factory=list)
    affected_resources: List[str] = field(default_factory=list)
    phases: List[DRPhase] = field(default_factory=list)
    rollback_phases: List[DRPhase] = field(default_factory=list)
    impact_assessment: Optional[ImpactReport] = None
    estimated_rto: int = 0            # 分钟
    estimated_rpo: int = 0            # 分钟
    validation_status: str = 'pending'
    graph_snapshot_time: str = ''

    @classmethod
    def from_dict(cls, d: dict) -> 'DRPlan':
        """从 JSON dict 反序列化"""
        phases = [DRPhase(**p) for p in d.pop('phases', [])]
        rollback = [DRPhase(**p) for p in d.pop('rollback_phases', [])]
        impact = ImpactReport(**d.pop('impact_assessment', {})) if d.get('impact_assessment') else None
        return cls(**d, phases=phases, rollback_phases=rollback, impact_assessment=impact)

@dataclass
class ValidationReport:
    valid: bool
    issues: list = field(default_factory=list)

@dataclass
class Issue:
    severity: str    # CRITICAL / WARNING / INFO
    message: str
```

---

## 13. 依赖与环境

### 13.1 Python 依赖

```
# requirements.txt
boto3>=1.34
requests>=2.28
pyyaml>=6.0
```

无额外重型依赖。图算法（拓扑排序、环路检测）使用标准库实现。

### 13.2 环境变量

| 变量 | 必须 | 默认值 | 说明 |
|------|------|--------|------|
| `NEPTUNE_ENDPOINT` | **是** | — | Neptune 集群端点 |
| `NEPTUNE_PORT` | 否 | `8182` | Neptune 端口 |
| `REGION` | 否 | `ap-northeast-1` | AWS Region |
| `BEDROCK_MODEL` | 否 | `global.anthropic.claude-sonnet-4-6` | LLM 摘要生成模型 |

### 13.3 IAM 权限

DR Plan Generator 运行在控制机上（非 Lambda），使用本机 AWS credentials。

需要的权限：
- `neptune-db:connect` + `ReadDataViaQuery` — 图谱查询
- `rds:DescribeDBClusters` — 数据库状态检查
- `dynamodb:DescribeTable` — DynamoDB 状态检查
- `elbv2:DescribeTargetHealth` — ALB 健康检查
- `eks:DescribeCluster` — EKS 集群信息
- `bedrock:InvokeModel` — LLM 摘要生成（可选）

---

## 14. 测试策略

### 14.1 单元测试

| 模块 | 测试重点 |
|------|---------|
| `graph_analyzer.py` | 拓扑排序正确性、环路检测、层级分类、并行组识别 |
| `step_builder.py` | 各资源类型命令生成、回滚命令完整性 |
| `plan_generator.py` | Phase 构建、步骤排序、RTO 计算 |
| `rollback_generator.py` | 反转顺序正确性、审批标记 |
| `plan_validator.py` | 环路检测、顺序验证、完整性检查 |
| `rto_estimator.py` | 串行/并行时间计算 |

### 14.2 Mock 策略

Neptune 查询使用 fixture JSON 文件 mock，不依赖真实 Neptune 连接。

```python
# tests/fixtures/az1_subgraph.json
{
    "nodes": [
        {"name": "petsite-db", "type": "RDSCluster", "tier": "Tier0", "az": "apne1-az1"},
        {"name": "petsite", "type": "Microservice", "tier": "Tier0", "az": "apne1-az1"},
        ...
    ],
    "edges": [
        {"from": "petsite", "to": "petsite-db", "type": "AccessesData"},
        ...
    ]
}
```

### 14.3 集成测试

- 连接真实 Neptune，验证 Q12–Q16 查询正确性
- 端到端：`main.py plan --scope az --source apne1-az1 --target apne1-az2` → 验证输出完整性

---

## 15. 实现计划（对应 PRD 里程碑）

### M1: 基础能力

| 任务 | 文件 | 优先级 |
|------|------|--------|
| Neptune 客户端 | `graph/neptune_client.py` | P0 |
| Q12–Q16 查询 | `graph/queries.py` | P0 |
| 图谱分析引擎 | `graph/graph_analyzer.py` | P0 |
| 数据模型 | `models.py` | P0 |
| Step Builder（RDS/DynamoDB/Microservice/ALB） | `planner/step_builder.py` | P0 |
| Plan Generator | `planner/plan_generator.py` | P0 |
| Markdown 渲染 | `output/markdown_renderer.py` | P0 |
| CLI 入口 | `main.py` | P0 |
| 单元测试 | `tests/` | P0 |

### M2: 完整切换

| 任务 | 文件 |
|------|------|
| Region 级切换支持 | `graph/queries.py` + `plan_generator.py` |
| 回滚计划生成 | `planner/rollback_generator.py` |
| 影响评估 | `assessment/impact_analyzer.py` |
| SPOF 检测 | `assessment/spof_detector.py` |
| RTO 估算 | `assessment/rto_estimator.py` |
| JSON 输出 | `output/json_renderer.py` |
| 计划验证 | `validation/plan_validator.py` |

### M3: 智能增强

| 任务 | 文件 |
|------|------|
| LLM 摘要 | `output/summary_generator.py` |
| 并行优化 | `planner/parallel_optimizer.py` |
| Chaos 验证导出 | `validation/chaos_exporter.py` |
| AGENT.md | `AGENT.md` |

### M4: 自动化

| 任务 | 说明 |
|------|------|
| 定期生成 | EventBridge + Lambda 定时触发 |
| S3 归档 | 计划自动上传 S3 |
| 计划 diff | 对比两次生成的计划差异 |

---

## 16. ADR（架构决策记录）

### ADR-001: CLI + AGENT.md 双层架构

**状态**：已采纳

**背景**：DR 计划生成需要多轮交互，但也需要支持自动化/CI。

**决策**：CLI 作为独立执行层，AGENT.md 作为通用 AI 交互指令层。

**理由**：
- CLI 可独立运行，不依赖 AI 框架
- AGENT.md 一份文件适配 OpenClaw / Claude Code / kiro-cli
- 关注点分离：CLI 负责逻辑，AGENT.md 负责交互

### ADR-002: openCypher 而非 Gremlin

**状态**：已采纳

**背景**：Neptune 同时支持 Gremlin 和 openCypher。现有 rca/ 使用 openCypher。

**决策**：DR 查询统一使用 openCypher。

**理由**：
- 与 rca/ 查询风格一致
- openCypher 对图遍历查询更直观
- Neptune 对 openCypher 支持已成熟

### ADR-003: 拓扑排序使用 Kahn's Algorithm

**状态**：已采纳

**决策**：层内排序使用 Kahn's Algorithm（BFS 拓扑排序）。

**理由**：
- 自然支持并行组检测（同一轮入队的节点可并行）
- 环路检测作为副产品（排序结果数 < 节点数 = 有环）
- 标准库实现，无额外依赖

---

*本文档为技术设计初稿，将随开发迭代更新。*
