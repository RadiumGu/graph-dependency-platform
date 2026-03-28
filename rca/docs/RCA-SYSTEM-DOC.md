# PetSite 故障恢复 & 根因分析系统 — 技术文档

> 权威文档，与代码同步维护
> 最后更新：2026-03-21

---

## 一、系统架构概览

```
CloudWatch Alarm (HTTPCode_Target_5XX_Count > 5)
      ↓ SNS (petsite-rca-alerts)
Lambda: petsite-rca-engine
      ├── 故障分类（P0/P1/P2）
      ├── Playbook 匹配
      ├── 半自动/全自动执行（LOW/MEDIUM 风险）
      │     └── Slack Block Kit 按钮（MEDIUM 风险确认）
      │           └── API Gateway → Lambda: rca-interaction
      ├── RCA 引擎（P0/P1）
      │     ├── Step 1:  DeepFlow L7 调用链（ClickHouse HTTP，HTTP 5xx）
      │     ├── Step 1b: DeepFlow L4（TCP RST/超时/SYN重传）
      │     ├── Step 2:  CloudTrail 变更事件（近 30 分钟）
      │     ├── Step 3:  Neptune 图谱候选根因
      │     │     ├── 服务调用链（Calls/DependsOn）
      │     │     ├── 基础设施图遍历（Q10: 非 running EC2）
      │     │     └── EC2/ASG Probe（Q10 为空时的兜底）
      │     ├── Step 3b: 时序验证（DeepFlow first_error × 图路径深度）
      │     ├── Step 3c: CloudWatch Logs 采样（ERROR/FATAL）
      │     ├── Step 3d: Layer2 AWS 服务探针（并行，≤12s）
      │     │     ├── SQSProbe / DynamoDBProbe / LambdaProbe
      │     │     ├── ALBProbe / StepFunctionsProbe
      │     │     └── EC2ASGProbe（仅限基础设施故障）
      │     └── Step 4:  置信度评分（最高 100）
      ├── Graph RAG 报告（Bedrock Claude Sonnet 4-6）
      └── Incident 节点写回 Neptune + S3 + Bedrock KB
```

---

## 二、AWS 资源清单

| 资源类型 | 名称/标识符 | 说明 |
|---------|-----------|------|
| Lambda | `petsite-rca-engine` | 主引擎 |
| Lambda | `rca-interaction` | Slack 按钮回调 |
| API Gateway | `POST /slack/interact` | 交互端点 |
| SNS | `petsite-rca-alerts` | 告警接收 Topic |
| CW Alarm | `petsite-rca-alb-5xx-high` | ALB HTTPCode_Target_5XX_Count > 5 触发 |
| IAM Role | 见 `.env` `ROLE_ARN` | 最小权限执行角色 |
| SSM | `${SSM_SLACK_WEBHOOK_PATH}` | Slack Webhook URL |
| SSM | `/rca/slack/interact-url` | API Gateway URL |
| Neptune | `${NEPTUNE_ENDPOINT}:${NEPTUNE_PORT}` | 知识图谱 |
| Bedrock | `global.anthropic.claude-sonnet-4-6` | RCA 报告生成 |
| Bedrock KB | `0RWLEK153U` | 历史案例语义检索 |
| ALB | `Servic-PetSi-by0kpyBtxswj` | PetSite 流量入口（东京区域） |

> 具体资源 ID 配置在 `.env` 文件中，参见 `.env.example`。

---

## 三、代码结构

```
rca_engine/
├── handler.py                   # Lambda 入口（handler.lambda_handler）
├── __init__.py
├── config.py                    # K8s deployment ↔ Neptune 服务名规范映射
├── core/
│   ├── rca_engine.py            # 多层 RCA 引擎（Step 1-4，含 Layer2 探针调度）
│   ├── fault_classifier.py      # P0/P1/P2 故障分类
│   └── graph_rag_reporter.py    # Graph RAG（Neptune+CW+DeepFlow+探针 → Bedrock）
├── neptune/
│   ├── neptune_client.py        # Neptune openCypher HTTP 客户端（SigV4 签名）
│   └── neptune_queries.py       # Q1-Q11 预定义查询
├── collectors/
│   ├── infra_collector.py       # 实时采集 Pod 状态 / RDS 指标
│   ├── aws_probers.py           # ★ 插件化 AWS 服务探针（Layer 2）
│   └── eks_auth.py              # EKS Bearer Token（SigV4 presigned STS URL）
├── actions/
│   ├── action_executor.py       # kubectl 操作封装（rollout/scale）
│   ├── playbook_engine.py       # Playbook 匹配（4 个预定义 + 动态降级）
│   ├── semi_auto.py             # 半自动/全自动执行逻辑
│   ├── slack_notifier.py        # Slack 告警（Block Kit 按钮）
│   └── incident_writer.py       # Incident 节点写回 Neptune + S3 + KB 索引
├── data/
│   └── service-db-mapping.json  # 服务 → DB 集群映射表
├── scripts/
│   └── scan-service-db-mapping.py  # 维护工具：扫描 K8s Deployments
├── tests/
│   └── test_rca.py              # 单元测试（17 个）
├── docs/
│   ├── TDD-fault-recovery-rca.md
│   └── RCA-SYSTEM-DOC.md        # 本文档
├── deploy.sh                    # 部署脚本（读取 .env）
├── .env.example
├── README.md                    # 英文文档
└── README_CN.md                 # 中文文档
```

---

## 四、故障严重度分类

| 严重度 | 触发条件 | 执行策略 |
|--------|---------|---------|
| P0 | Tier0 服务 + 影响多个核心业务能力 | 禁止自动执行，Diagnose-First，人工确认 |
| P1 | Tier0 服务 或 影响 1 个核心业务能力 | suggest 模式 + Slack 按钮确认（MEDIUM 风险） |
| P2 | Tier1/Tier2 服务，不影响核心能力 | LOW 风险可全自动执行 |

---

## 五、Playbook 列表

| Playbook ID | 触发场景 | 风险等级 | 默认操作 |
|------------|---------|---------|---------|
| `db_connection_exhausted` | `rds_connections` 或 `db_timeout_errors` > 90% | LOW | `rollout_restart` |
| `alb_5xx_spike` | `alb_5xx_rate` 或 `error_rate` > 20% | DEPENDS | 根据分析结果判断 |
| `crashloop` | Pod `CrashLoopBackOff` | MEDIUM | `rollout_undo` |
| `single_az_down` | `az_availability` 异常，`fault_boundary=az` | LOW | `scale_deployment` |

无匹配 Playbook 时，`_dynamic_suggest()` 根据故障分类自动生成通用恢复建议。

---

## 六、Layer 2 AWS 服务探针（`collectors/aws_probers.py`）

在 RCA 流程 Step 3d 并行运行，补充 Neptune 图谱无法覆盖的托管服务故障。

| 探针 | 检测内容 | score_delta |
|------|---------|------------|
| `SQSProbe` | 队列积压 / DLQ 消息堆积 | +20 |
| `DynamoDBProbe` | 读写限流 / SystemErrors | +15~25 |
| `LambdaProbe` | 函数错误 / 限流 / 执行时间接近超时 | +10~25 |
| `ALBProbe` | ELB_5XX / 连接拒绝 / 不健康目标 / 高延迟 | +10~30 |
| `StepFunctionsProbe` | 执行失败 / 超时 / 被限流 | +20 |
| `EC2ASGProbe` | EKS 节点非 running（Neptune Q10 为空时激活） | +40 |

**扩展方式**：在 `aws_probers.py` 中添加 `@register_probe` 装饰的类，无需修改其他文件。

---

## 七、Graph RAG RCA 报告流程

```
1. Neptune 子图提取：服务 2-hop 依赖拓扑（openCypher）
2. DeepFlow L7 调用链：最近 30 分钟 5xx 错误时序
3. DeepFlow L4：TCP RST / 超时 / SYN重传异常
4. CloudWatch 指标：ALB 5xx 总量 + Pod CPU 均值/峰值
5. CloudTrail 变更：近 30 分钟的部署/配置变更事件
6. infra_collector：实时 Pod 状态 + RDS 连接/CPU/内存
7. Layer2 AWS 探针：SQS/DynamoDB/Lambda/ALB/StepFunctions 实时探测
8. CW Logs 采样：top candidate 服务的 ERROR/FATAL 日志
9. Bedrock KB：语义相似历史案例检索（Knowledge Base ID: 0RWLEK153U）

→ 组装 Prompt → Bedrock Claude Sonnet 4-6
→ 输出：根因 + 置信度(0-100) + confidence_breakdown + 建议操作 + 推理过程 + 影响范围
```

**降级策略**：Bedrock 调用失败时自动降级为规则引擎结果（`rule_engine_fallback`）。

---

## 八、置信度评分体系

```
基础评分（最高 100）：
  +40  DeepFlow L7 有 5xx 调用链时序证据（或 L4 SYN重传）
  +30  有近期 CloudTrail 变更事件关联
  +20  Neptune 图谱确认为调用链起点（无上游错误）
  +10  Bedrock KB 找到相似历史案例

基础设施层（叠加）：
  +40  Neptune Q10 或 EC2ASGProbe 发现非 running 状态 EC2 节点

Layer2 AWS 探针（叠加，上限 +40）：
  +10~30  各探针 score_delta 之和（取上限 40）

L4 TCP 信号（L7 无数据时）：
  +40  SYN重传（Pod 完全不可达）
  +15  TCP RST > 10
  +10  TCP 超时 > 20

时序交叉验证（step3b）：
  +0~10  DeepFlow first_error 时间戳与 Neptune 图路径深度吻合

上限：min(score, 100)
```

---

## 九、Neptune 查询参考（openCypher）

| 编号 | 查询函数 | 用途 |
|------|---------|------|
| Q1 | `q1_blast_radius` | 下游影响：服务 → 5 跳 + BusinessCapability |
| Q2 | `q2_tier0_status` | 所有 Tier0 服务状态：故障边界、AZ、副本数 |
| Q3 | `q3_upstream_deps` | 调用故障服务的上游服务 |
| Q4 | `q4_service_info` | 单个服务属性 |
| Q5 | `q5_similar_incidents` | 历史已解决故障 |
| Q6 | `q6_pod_status` | Neptune 中的 Pod 状态（ETL 写入） |
| Q7 | `q7_db_connections` | 服务关联数据库连接数 |
| Q8 | `q8_log_source` | 服务对应 CloudWatch 日志组 |
| Q9 | `q9_service_infra_path` | 服务 → Pod → EC2 → AZ 完整链路 |
| Q10 | `q10_infra_root_cause` | 集群中所有非 running EC2，反向遍历受影响服务 |
| Q11 | `q11_broader_impact` | 给定故障 EC2 ID，找所有受影响服务（爆炸半径） |

---

## 十、Slack 交互按钮

- **MEDIUM 风险** Playbook → 发送带按钮的 Block Kit 消息
- 点击 **[确认执行]** → API Gateway → `rca-interaction` Lambda → 异步触发执行
- 点击 **[跳过]** → 消息更新为跳过状态
- 点击后消息立即替换（防重复点击）

Slack App Interactivity URL 存储在 SSM `/rca/slack/interact-url`。

---

## 十一、速率限制

- 同一服务自动操作 **≤ 3 次 / 30 分钟**（SSM 计数器）
- P0 严重度：**永远不自动执行**
- 重置方式：`aws ssm delete-parameter --name /rca/rate-limit/{service}`

---

## 十二、K8s 权限

```yaml
ClusterRole: rca-engine
rules:
  - resources: ["deployments"]
    verbs: ["get", "patch"]          # rollout_restart / scale
  - resources: ["pods"]
    verbs: ["list", "get"]           # 状态检查
```

EKS aws-auth 映射：Lambda 执行角色 → `rca-engine` ClusterRole

---

## 十三、IAM 权限清单

Lambda 执行角色需要：

```
neptune-db:*
eks:DescribeCluster
cloudtrail:LookupEvents
cloudwatch:GetMetricData, GetMetricStatistics
logs:FilterLogEvents, DescribeLogGroups
ssm:GetParameter, GetParameters
bedrock:InvokeModel
bedrock-agent-runtime:Retrieve
ec2:DescribeInstances
rds:DescribeDBClusters, DescribeDBInstances
autoscaling:DescribeAutoScalingGroups
sqs:ListQueues, GetQueueAttributes
dynamodb:ListTables
lambda:ListFunctions, GetFunctionConfiguration
states:ListStateMachines
elasticloadbalancing:DescribeLoadBalancers, DescribeTargetGroups,
                     DescribeTargetHealth, DescribeListeners
```

---

## 十四、已知问题 & 改进方向

| 问题 | 优先级 | 说明 |
|------|--------|------|
| 因果边无权重（`error_propagation_weight`） | ⭐⭐ | 可能误判上游为根因 |
| 异常子图模式匹配未实现 | ⭐⭐⭐ | 历史匹配只统计次数 |
| DeepFlow `app_service` 字段为空 | ⭐ | 已绕过（用 request_domain 解析） |
| Neptune 写权限不足 | ⭐ | openCypher 写返回 403，`subgraph_pattern` / `causal_weight` 写入跳过 |
| AWS 探针覆盖不完整 | ⭐⭐ | 尚未覆盖 ElastiCache、Kinesis、API Gateway 等 |
