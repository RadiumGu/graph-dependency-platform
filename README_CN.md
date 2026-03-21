🇨🇳 中文文档 | [🇺🇸 English](./README.md)

# rca_engine — Neptune 图谱 + AIOps 根因分析引擎

基于 AWS Lambda 的 AIOps 根因分析引擎，核心功能：
1. 接收 CloudWatch/SNS 告警（例如 `HTTPCode_Target_5XX_Count > 5`）
2. 对故障进行 P0/P1/P2 严重度分级
3. 执行多层 RCA：DeepFlow L7/L4 → CloudTrail → Neptune 图谱遍历 → **插件化 AWS 服务探针**
4. 通过 Bedrock Claude 生成 Graph RAG 根因报告
5. 发送 Slack 通知（含证据链和建议操作）
6. 将故障记录写入 Neptune 知识库

> **前置条件**：需要先使用 [graph-dp-cdk](../graph-dp-cdk/) 项目构建 Neptune 依赖关系图谱。

---

## 系统架构

```
CloudWatch Alarm (HTTPCode_Target_5XX_Count > 5)
          │
          ▼
       SNS Topic (petsite-rca-alerts)
          │
          ▼
    handler.py                         ← Lambda 入口
          │
  ┌───────┼──────────────────────────────────────────────────────┐
  ▼       ▼                                                      ▼
core/     core/rca_engine.py                         core/graph_rag_reporter.py
故障      多层 RCA：                                   Bedrock Claude
分级器    1.  DeepFlow L7（HTTP 5xx 调用链）            + Neptune 子图
  │       1b. DeepFlow L4（TCP RST/超时/SYN重传）       + 服务→Pod→EC2→AZ 路径
  │       2.  CloudTrail 变更事件                      + CloudWatch 指标
  │       3.  Neptune 图谱候选根因                     + collectors/infra_collector
  │           ├─ 服务调用链（Calls/DependsOn）          + Layer2 AWS 探针结果
  │           ├─ 基础设施：图遍历（q10）                + CW Logs 采样
  │           └─ 基础设施：EC2/ASG 探针（q10 为空时）  → 结构化根因报告
  │       3b. 时序验证（图路径深度 × 时间戳）
  │       3c. CW Logs 采样（ERROR/FATAL）
  │       3d. Layer2 AWS 服务探针（并行执行）
  │           ├─ SQSProbe / DynamoDBProbe / LambdaProbe
  │           ├─ ALBProbe / StepFunctionsProbe
  │           └─ EC2ASGProbe（兜底，仅限基础设施故障）
  │       4.  置信度评分（最高 100）
  │             │
  │       neptune/neptune_queries.py  collectors/infra_collector.py
  │       Q1-Q8（服务层）              实时 Pod 状态（K8s API）
  │       Q9-Q11（基础设施层）         实时 DB 指标（CloudWatch RDS）
  ▼
actions/playbook_engine.py → actions/semi_auto.py → actions/action_executor.py
（故障 Playbook）             （半自动执行）           （kubectl rollout/scale）
          │
          ▼
  actions/slack_notifier.py  ← Slack Incoming Webhook + 确认按钮
  actions/incident_writer.py ← Neptune Incident 节点 + S3 归档 + Bedrock KB 索引
```

---

## 核心设计：多层根因检测

### 第一层：Neptune 图谱遍历（首选）

图谱中已包含由 ETL 构建的完整基础设施链路：

```
微服务 ─[RunsOn]→ Pod ─[RunsOn]→ EC2实例 ─[LocatedIn]→ 可用区
```

- **Q10** 查询所有 `state != 'running'` 的 EC2 节点，反向遍历找到受影响的 Pod 和服务
- **Q11** 扩展爆炸半径：给定故障 EC2 ID，找出所有受影响的服务（不仅限于告警服务）
- 在 ETL 近期运行、Neptune 中 `EC2Instance.state` 为最新状态时效果最佳

### 第二层：插件化 AWS 服务探针（`collectors/aws_probers.py`）

一套可自由扩展的探针框架，在 Neptune 图谱遍历的同时**并行执行（Step 3d）**，用于覆盖 Neptune 无法感知的 AWS 托管服务故障。

#### 设计原理

```
ProbeRegistry（通过 @register_probe 装饰器自动注册）
    │
    ├── SQSProbe            ← 队列积压 + DLQ 消息堆积
    ├── DynamoDBProbe       ← 读/写限流 + 系统错误
    ├── LambdaProbe         ← 函数错误 / 限流 / 执行时间接近超时
    ├── ALBProbe            ← ELB_5XX / 连接拒绝 / 不健康目标 / 高延迟
    ├── StepFunctionsProbe  ← 执行失败 / 超时 / 被限流
    └── EC2ASGProbe         ← EKS 节点非 running（仅在 Neptune q10 无结果时激活）
```

每个探针实现两个方法：

```python
class BaseProbe:
    def is_relevant(self, signal: dict, affected_service: str) -> bool:
        """当前告警/服务是否需要运行此探针？"""

    def probe(self, signal: dict, affected_service: str) -> Optional[ProbeResult]:
        """执行探测；发现异常返回 ProbeResult，否则返回 None。"""
```

所有探针返回统一的 `ProbeResult` 数据结构：

```python
@dataclass
class ProbeResult:
    service_name: str    # 如 "SQS"、"DynamoDB"
    healthy: bool        # False 表示发现异常
    score_delta: int     # 对 RCA 置信度评分的加分（0~40）
    summary: str         # 一行描述性发现
    evidence: list       # 证据条目，注入 Slack 消息和 Graph RAG Prompt
    details: dict        # 原始数据，用于调试
```

`run_all_probes()` 通过 `ThreadPoolExecutor`（超时 12 秒）并发运行所有相关探针，然后：
- 汇总所有异常探针的 `score_delta`（上限 40 分）
- 将证据追加到评分流水线的 `top_candidate`
- 将所有探针发现注入 Graph RAG Prompt，供 Bedrock Claude 分析

#### 故障类型覆盖范围

| 故障类型 | Neptune（第一层） | AWS 探针（第二层） |
|---------|-----------------|-----------------|
| EC2 节点宕机 / 可用区故障 | ✅ Q10 + Q11 | ✅ EC2ASGProbe（兜底） |
| Pod CrashLoop / OOM | ✅ Q6 + infra_collector | — |
| RDS 连接池耗尽 | ✅ infra_collector | — |
| **SQS 队列积压 / DLQ 消息堆积** | ❌ | ✅ SQSProbe |
| **DynamoDB 限流** | ❌ | ✅ DynamoDBProbe |
| **Lambda 错误 / 限流** | ❌ | ✅ LambdaProbe |
| **ALB ELB 侧 5XX / 连接拒绝** | ❌ | ✅ ALBProbe |
| **Step Functions 执行失败** | ❌ | ✅ StepFunctionsProbe |
| 应用代码部署出错 | — | ✅ CloudTrail（step2） |

#### 如何添加新探针

**无需修改 `rca_engine.py` 或任何其他文件**，只需在 `collectors/aws_probers.py` 中新增一个类：

```python
@register_probe                          # 导入时自动注册
class MyServiceProbe(BaseProbe):

    def is_relevant(self, signal, affected_service):
        return affected_service in ('my-service', 'petsite')

    def probe(self, signal, affected_service) -> Optional[ProbeResult]:
        # 查询 AWS API 或 CloudWatch
        # ...
        return ProbeResult(
            service_name='MyService',
            healthy=False,
            score_delta=20,
            summary='发现异常',
            evidence=['metric=value'],
        )
```

---

## 前置依赖

| 组件 | 说明 |
|-----|------|
| **Neptune 图谱** | 由 [graph-dp-cdk](../graph-dp-cdk/) 构建。包含 Microservice、Pod、EC2Instance、AZ 节点及 `Calls`、`DependsOn`、`RunsOn`、`LocatedIn` 边，ETL 每 15 分钟运行一次。 |
| **EKS 集群** | 目标 Kubernetes 集群，Lambda 需要 `eks:DescribeCluster` 权限。 |
| **DeepFlow / ClickHouse** | eBPF 可观测性平台，包含 `l7_flow_log`（HTTP 5xx）和 `l4_flow_log`（TCP RST/超时/SYN 重传）数据表。 |
| **Bedrock** | Claude Sonnet（`bedrock:InvokeModel`）+ 知识库（`bedrock-agent-runtime:Retrieve`）。 |
| **Slack** | Incoming Webhook URL，存储在 SSM Parameter Store 中。 |
| **IAM Role** | Lambda 执行角色需要：`neptune-db:*`、`eks:DescribeCluster`、`cloudtrail:LookupEvents`、`cloudwatch:GetMetricData`、`logs:*`、`ssm:GetParameter*`、`bedrock:InvokeModel`、`bedrock-agent-runtime:Retrieve`、`ec2:DescribeInstances`、`rds:Describe*`、`autoscaling:DescribeAutoScalingGroups`、`sqs:ListQueues`、`sqs:GetQueueAttributes`、`dynamodb:ListTables`、`lambda:ListFunctions`、`lambda:GetFunctionConfiguration`、`states:ListStateMachines`、`elasticloadbalancing:Describe*`。 |

---

## 环境变量

| 变量名 | 必填 | 默认值 | 说明 |
|-------|------|--------|------|
| `REGION` | 否 | `ap-northeast-1` | AWS 区域 |
| `NEPTUNE_ENDPOINT` | **是** | — | Neptune 集群 Endpoint 主机名 |
| `NEPTUNE_PORT` | 否 | `8182` | Neptune 端口 |
| `EKS_CLUSTER_NAME` | **是** | — | EKS 集群名（infra_collector 和 EC2ASGProbe 使用） |
| `CLICKHOUSE_HOST` | **是** | — | ClickHouse/DeepFlow 主机地址（内网 IP） |
| `CLICKHOUSE_PORT` | 否 | `8123` | ClickHouse HTTP 端口 |
| `BEDROCK_MODEL` | 否 | `global.anthropic.claude-sonnet-4-6` | Bedrock 模型 ID |
| `BEDROCK_KB_ID` | **是** | — | Bedrock 知识库 ID |
| `SLACK_WEBHOOK_URL` | 否 | — | Slack Incoming Webhook（由 `deploy.sh` 从 SSM 注入） |
| `SLACK_CHANNEL` | 否 | — | Slack 频道 ID |

---

## 置信度评分（step4）

```
基础评分（最高 100）：
  +40  最早出现异常的服务（L7 5xx 或 L4 TCP 错误）
  +30  与服务相关的近期 CloudTrail 变更事件
  +20  Neptune 图谱确认该服务为调用链起点（无上游错误）
  +10  Bedrock KB 找到历史相似故障案例

基础设施层（叠加）：
  +40  Neptune Q10 或 EC2ASGProbe 发现非 running 状态的 EC2 节点
       （单 AZ 集中分布时标注于证据中）

Layer2 AWS 服务探针（叠加，总计上限 +40）：
  +20~30  SQS / DynamoDB / Lambda / ALB / StepFunctions 发现异常
          （多个探针的 score_delta 求和后取上限）

L4 TCP 信号（L7 无数据时）：
  +40  检测到 SYN 重传（Pod 完全不可达）
  +15  TCP RST 数量 > 10
  +10  TCP 超时数量 > 20

交叉验证（L7 有数据时）：
  +10  L4 异常与 L7 发现相互印证

时序验证（step3b）：
  +0~10  DeepFlow 首次报错时间与 Neptune 图路径深度吻合

上限：min(score, 100)
```

---

## Neptune 查询参考

| 查询 | 用途 |
|------|------|
| **Q1** `q1_blast_radius` | 下游影响：服务 → 5 跳 `Calls/DependsOn` + BusinessCapability |
| **Q2** `q2_tier0_status` | 所有 Tier0 服务：故障边界、可用区、副本数 |
| **Q3** `q3_upstream_deps` | 调用故障服务的上游服务 |
| **Q4** `q4_service_info` | 单个服务属性 |
| **Q5** `q5_similar_incidents` | 该服务的历史已解决故障记录 |
| **Q6** `q6_pod_status` | Neptune 中的 Pod 状态（ETL 写入） |
| **Q7** `q7_db_connections` | 服务关联的数据库连接数 |
| **Q8** `q8_log_source` | 服务对应的 CloudWatch 日志组 |
| **Q9** `q9_service_infra_path` | **服务 → Pod → EC2 → AZ** 完整基础设施链路 |
| **Q10** `q10_infra_root_cause` | **集群中所有非 running EC2**，反向遍历找到受影响的 Pod 和服务，含 AZ 影响分析 |
| **Q11** `q11_broader_impact` | 给定故障 EC2 ID，找出所有受影响的服务（爆炸半径） |

---

## 部署

### 1. 配置

```bash
cp .env.example .env
# 填写：ACCOUNT、FUNCTION_NAME、ROLE_ARN、SUBNET_IDS、SG_IDS、
#       NEPTUNE_ENDPOINT、EKS_CLUSTER、CLICKHOUSE_HOST、BEDROCK_KB_ID
```

### 2. 执行部署

```bash
bash deploy.sh           # 完整部署
bash deploy.sh --dry-run # 预览（不执行）
```

`deploy.sh` 执行步骤：
1. `pip install requests` 到构建目录
2. 递归复制源码目录（`core/`、`neptune/`、`collectors/`、`actions/`、`data/`）及根目录 `.py` 文件
3. 打包为 Lambda 部署包（zip）
4. `aws lambda update-function-code`
5. 配置环境变量（从 SSM 读取 Slack webhook）
6. 创建/验证 SNS Topic + Lambda 订阅
7. 冒烟测试

### 3. 触发方式

Lambda 由 SNS 在 CloudWatch Alarm 触发时自动调用。也可手动测试：

```bash
# 通过 SNS Payload（生产格式）
cat << 'EOF' > /tmp/test-payload.json
{
  "Records": [{
    "Sns": {
      "Message": "{\"AlarmName\":\"petsite-rca-alb-5xx-high\",\"NewStateValue\":\"ALARM\",\"NewStateReason\":\"test\",\"Trigger\":{\"Namespace\":\"AWS/ApplicationELB\",\"MetricName\":\"HTTPCode_Target_5XX_Count\",\"Dimensions\":[{\"name\":\"TargetGroup\",\"value\":\"targetgroup/Servic-PetSi-BGUX1XK3RN6D/8d4815db7d125b15\"},{\"name\":\"LoadBalancer\",\"value\":\"app/Servic-PetSi-by0kpyBtxswj/bbe5082588a126fc\"}]}}"
    }
  }]
}
EOF

aws lambda invoke \
  --function-name petsite-rca-engine \
  --payload fileb:///tmp/test-payload.json \
  --region ap-northeast-1 \
  /tmp/rca-output.json

cat /tmp/rca-output.json | python3 -m json.tool
```

---

## 模块说明

| 模块 | 文件 | 职责 |
|------|------|------|
| _（根目录）_ | `handler.py` | Lambda 入口；解析 SNS/CW 事件，编排所有模块 |
| _（根目录）_ | `config.py` | K8s Deployment ↔ Neptune 服务名映射 |
| **core/** | `rca_engine.py` | 多层 RCA 引擎：DeepFlow L7/L4 + CloudTrail + Neptune 图谱 + AWS 探针 + 评分 |
| **core/** | `fault_classifier.py` | P0/P1/P2 严重度分级；自动执行门控 |
| **core/** | `graph_rag_reporter.py` | Graph RAG：Neptune 子图 + 所有探针信号 → Claude → 结构化报告 |
| **neptune/** | `neptune_client.py` | 带 IAM SigV4 签名的 Neptune HTTP 客户端 |
| **neptune/** | `neptune_queries.py` | Neptune openCypher 查询 Q1–Q11（服务层 + 基础设施层） |
| **collectors/** | `infra_collector.py` | 实时 Pod 状态（K8s API）+ DB 指标（CloudWatch RDS） |
| **collectors/** | `aws_probers.py` | ★ **插件化 AWS 服务探针**（SQS/DynamoDB/Lambda/ALB/EC2/StepFunctions） |
| **collectors/** | `eks_auth.py` | EKS Bearer Token 生成（SigV4 预签名 STS URL） |
| **actions/** | `action_executor.py` | kubectl 操作：rollout restart/undo、scale 副本数 |
| **actions/** | `playbook_engine.py` | 故障 Playbook 匹配（4 种预定义模式） |
| **actions/** | `semi_auto.py` | P1/P2 半自动执行流程；Slack 确认交互 |
| **actions/** | `slack_notifier.py` | Slack 消息格式化 + Webhook 推送 |
| **actions/** | `incident_writer.py` | Neptune Incident 节点 + S3 归档 + Bedrock KB 索引 |
| **data/** | `service-db-mapping.json` | 服务 → DB 集群映射关系 |
| **scripts/** | `scan-service-db-mapping.py` | 扫描 K8s Deployment 发现服务→DB 关联关系 |

---

## 测试

```bash
# 推荐使用 pytest
cd <项目父目录>
python3 -m pytest rca_engine/tests/test_rca.py -v

# 或使用 unittest
python3 -m unittest rca_engine.tests.test_rca -v

# 共 17 个测试：TestStep4Score(5) + TestFaultClassifier(5) + TestPlaybookMatch(7)
```

---

## 目录结构

```
rca_engine/
├── handler.py                  # Lambda 入口（必须保留在根目录）
├── config.py                   # K8s Deployment ↔ Neptune 名称映射
├── __init__.py
├── core/                       # 核心 RCA 逻辑
│   ├── rca_engine.py           # 多层 RCA 引擎
│   ├── fault_classifier.py     # P0/P1/P2 严重度分级
│   └── graph_rag_reporter.py   # Bedrock Claude Graph RAG 报告
├── neptune/                    # 图数据库层
│   ├── neptune_client.py       # SigV4 签名 HTTP 客户端
│   └── neptune_queries.py      # Q1-Q11 openCypher 查询
├── collectors/                 # 实时数据采集
│   ├── infra_collector.py      # K8s Pod 状态 + RDS 指标
│   ├── aws_probers.py          # ★ 插件化 AWS 服务探针（第二层）
│   └── eks_auth.py             # EKS Bearer Token 生成
├── actions/                    # 执行与通知
│   ├── action_executor.py      # kubectl rollout/scale 操作
│   ├── playbook_engine.py      # 故障 Playbook 匹配
│   ├── semi_auto.py            # 半自动执行流程
│   ├── slack_notifier.py       # Slack Webhook 推送
│   └── incident_writer.py      # Neptune + S3 + Bedrock KB
├── data/
│   └── service-db-mapping.json # 服务 → DB 集群映射
├── scripts/
│   └── scan-service-db-mapping.py
├── tests/
│   └── test_rca.py             # 17 个单元测试
├── docs/
│   ├── TDD-fault-recovery-rca.md
│   └── RCA-SYSTEM-DOC.md
├── deploy.sh                   # Lambda 打包 + 部署脚本
├── .env.example
├── README.md                   # 英文文档
└── README_CN.md                # 中文文档（本文件）
```

---

## 设计文档

- [`docs/TDD-fault-recovery-rca.md`](./docs/TDD-fault-recovery-rca.md) — 技术设计文档（TDD）
- [`docs/RCA-SYSTEM-DOC.md`](./docs/RCA-SYSTEM-DOC.md) — 系统文档，含资源清单

---

## 已知限制

1. **Neptune 写权限**：RCA Lambda 对 Neptune 只有只读权限（openCypher 写操作返回 403），`subgraph_pattern` 和 `causal_weight` 的写入会被静默跳过。
2. **CloudTrail 延迟**：`StopInstances` 事件可能在前几分钟内不出现在 `LookupEvents` 中，EC2ASGProbe 作为兜底补偿。
3. **ETL-ASG 竞态条件**：EC2 停止后 ASG 会快速终止实例，ETL 可能来不及记录 `stopped` 状态，EC2ASGProbe 负责处理此场景。
4. **历史 Pod 数据积累**：Neptune 会保留历史部署中已 Failed/Succeeded 的 Pod，`gc.py` 会清理部分，但历史 Pod 的 `RunsOn` 边可能已过期。
5. **AWS 探针覆盖范围**：目前覆盖 SQS/DynamoDB/Lambda/ALB/StepFunctions，其他 AWS 服务（如 ElastiCache、Kinesis、API Gateway）暂未覆盖。如需扩展，在 `collectors/aws_probers.py` 中通过 `@register_probe` 添加新探针即可。
