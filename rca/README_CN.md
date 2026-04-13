中文文档 | [English](./README.md)

# rca_engine — Neptune 图谱 + AIOps 根因分析引擎

基于 AWS Lambda 的 AIOps 根因分析引擎，核心功能：
1. 接收 CloudWatch/SNS 告警（例如 `HTTPCode_Target_5XX_Count > 5`）
2. **告警聚合降噪**：同一故障链的多条告警在 DynamoDB 缓冲窗口内聚合为 1 个事件，仅触发 1 次 RCA；拓扑驱动关联将下游症状归并到根因服务
3. 对故障进行 P0/P1/P2 严重度分级
4. 执行多层 RCA：DeepFlow L7/L4 → CloudTrail → Neptune 图谱遍历 → **插件化 AWS 服务探针**
5. 通过 Bedrock Claude 生成 Graph RAG 根因报告
6. 发送 Slack 通知（含证据链和建议操作）；支持 Slack 交互确认/否定 RCA 结论，反馈写回 Neptune
7. 将故障记录写入 Neptune 知识库

> **前置条件**：需要先使用 [infra/](../infra/) 项目构建 Neptune 依赖关系图谱。

---

## 生态全景 — 四个目录，一个平台

本目录（`rca/`）是基于 PetSite (AWS EKS) 构建的可观测性 + 弹性验证平台中的 **AIOps 根因分析引擎**。平台以 monorepo 形式组织，四个目录协同工作：

```
┌─────────────────────────────────────────────────────────────────┐
│                     PetSite on AWS EKS                          │
└───────────────────────────┬─────────────────────────────────────┘
                            │
         ┌──────────────────▼──────────────────┐
         │  📦 infra/                          │
         │  CDK 基础设施 + 模块化 ETL 管道      │
         │  → 构建 Neptune 知识图谱             │
         └────┬──────────┬──────────┬──────────┘
              │ 图谱查询  │ 告警触发  │ 图谱查询
              ▼           ▼           ▼
   ┌────────────┐  ┌────────────┐  ┌──────────────────────┐
   │ 🔍 rca/    │  │ 💥 chaos/  │  │ 📋 dr-plan-          │
   │  （本目录） │  │ AI 驱动混  │  │    generator/        │
   │  多层 RCA  │  │ 沌工程平台 │  │  Neptune 驱动的       │
   │  + 探针    │  │(FIS + CM)  │  │  灾难恢复计划生成      │
   │  + Graph   │  │            │  │                      │
   │    RAG     │  │            │  │                      │
   └─────┬──────┘  └──────┬─────┘  └──────────────────────┘
         │  写入事件记录   │ 验证 RCA 准确性
         └────────────────┘
                   闭环
```

| 目录 | 定位 |
|------|------|
| **[infra/](../infra/)** | 基础设施层 — CDK 栈、Neptune ETL 管道、DeepFlow + AWS 拓扑采集 |
| **rca/** | AIOps 根因分析引擎 — 多层根因分析、插件化 AWS 探针、Bedrock Graph RAG 报告（本目录） |
| **[chaos/](../chaos/)** | AI 驱动的混沌工程 — 假设生成、5 阶段实验引擎、FIS + Chaos Mesh 闭环学习 |
| **[dr-plan-generator/](../dr-plan-generator/)** | 灾难恢复计划生成器 — Neptune 图谱驱动的 DR 步骤生成、回滚方案 |

**数据流：** `infra/` ETL 填充 Neptune → CloudWatch 告警触发 `rca/` 执行根因分析 → `chaos/` 注入故障验证 RCA 准确性 → `dr-plan-generator/` 基于图谱生成灾难恢复计划 → 结果回写 Neptune。

---

## 系统架构

### 核心 RCA 流水线

```
CloudWatch Alarm
    │
    ▼ SNS
handler.py
    │
    ├─ [聚合路径] ───→ Phase 4 告警聚合（详见下方章节）
    │   event_normalizer → alert_buffer → topology_correlator → decision_engine
    │
    ├─ fault_classifier.py ──────────────────────────────→ P0/P1/P2 分级
    │
    ├─ rca_engine.py ── 多层 RCA ──────────────────────────────────────┐
    │   Step 1:  DeepFlow L7（HTTP 5xx 调用链）                        │
    │   Step 1b: DeepFlow L4（TCP RST/超时/SYN 重传）                  │
    │   Step 2:  CloudTrail 变更事件                                   │
    │   Step 3:  Neptune 图谱遍历（服务→Pod→EC2→AZ）                   │
    │   Step 3b: 时序验证（图路径深度 × 时间戳）                        │
    │   Step 3c: CW Logs 采样（ERROR/FATAL）                           │
    │   Step 3d: Layer2 探针（并行）  ←── collectors/aws_probers.py    │
    │   Step 3e: 历史上下文           ←── neptune Q17/Q18              │
    │   Step 3f: 语义相似故障搜索     ←── search/incident_vectordb.py  │
    │   Step 4:  置信度评分（最高 100）                                 │
    │                                                                  │
    ├─ graph_rag_reporter.py ─────────────────────────────────────────┘
    │   Bedrock Claude + Neptune 子图 → 结构化根因报告
    │
    ├─ actions/
    │   ├─ playbook_engine.py ──→ 故障 Playbook 匹配
    │   ├─ semi_auto.py ────────→ P1/P2 半自动执行
    │   ├─ action_executor.py ──→ kubectl rollout/scale
    │   ├─ slack_notifier.py ───→ Slack 通知 + 交互按钮
    │   └─ incident_writer.py ──→ Neptune Incident + S3 + Bedrock KB + S3 Vectors
    │
    └─ feedback_collector.py ───→ Slack 反馈 → Neptune 写回 (Q19/Q20)
```

### Phase 4 告警聚合（六层架构）

告警聚合层架构详见下方 **[Phase 4：告警降噪与智能事件管理](#phase-4告警降噪与智能事件管理)** 章节。

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
| **Neptune 图谱** | 由 [infra/](../infra/) 构建。包含 Microservice、Pod、EC2Instance、AZ 节点及 `Calls`、`DependsOn`、`RunsOn`、`LocatedIn` 边，ETL 每 15 分钟运行一次。 |
| **EKS 集群** | 目标 Kubernetes 集群，Lambda 需要 `eks:DescribeCluster` 权限。 |
| **DeepFlow / ClickHouse** | eBPF 可观测性平台，包含 `l7_flow_log`（HTTP 5xx）和 `l4_flow_log`（TCP RST/超时/SYN 重传）数据表。 |
| **Bedrock** | Claude Sonnet（`bedrock:InvokeModel`）+ 知识库（`bedrock-agent-runtime:Retrieve`）。 |
| **Slack** | Incoming Webhook URL，存储在 SSM Parameter Store 中。 |
| **DynamoDB** | `gp-alert-buffer` 表（由 CDK AlertBufferStack 创建），用于 Phase 4 告警聚合缓冲窗口。 |
| **IAM Role** | Lambda 执行角色需要：`neptune-db:*`、`eks:DescribeCluster`、`cloudtrail:LookupEvents`、`cloudwatch:GetMetricData`、`logs:*`、`ssm:GetParameter*`、`bedrock:InvokeModel`、`bedrock-agent-runtime:Retrieve`、`ec2:DescribeInstances`、`rds:Describe*`、`autoscaling:DescribeAutoScalingGroups`、`sqs:ListQueues`、`sqs:GetQueueAttributes`、`dynamodb:ListTables`、`dynamodb:PutItem`、`dynamodb:UpdateItem`、`dynamodb:GetItem`、`dynamodb:Query`、`lambda:ListFunctions`、`lambda:GetFunctionConfiguration`、`lambda:InvokeFunction`、`states:ListStateMachines`、`elasticloadbalancing:Describe*`、`scheduler:CreateSchedule`、`iam:PassRole`。 |

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
| `BUFFER_TABLE_NAME` | 否 | `gp-alert-buffer` | Phase 4 告警缓冲 DynamoDB 表名 |
| `SCHEDULER_ROLE_ARN` | 否 | — | EventBridge Scheduler 执行角色 ARN（由 CDK AlertBufferStack 输出） |
| `WINDOW_FLUSH_FUNCTION_ARN` | 否 | — | 窗口到期处理 Lambda ARN（gp-window-flush，由 CDK AlertBufferStack 输出） |

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
| **Q17** `q17_incidents_by_resource` | 涉及相同资源的历史已解决故障（`MentionsResource` 边） |
| **Q18** `q18_chaos_history` | 服务的混沌实验历史（`TestedBy` 边） |
| **Q19** `q19_confirmed_incidents` | 查询已被 Slack 反馈确认的 Incident，按服务/时间筛选 |
| **Q20** `q20_false_positive_rate` | 计算指定时间段内各服务的 RCA 误报率（被否定的 Incident 比例） |

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
| _（根目录）_ | `handler.py` ★ | Lambda 入口；解析 SNS/CW 事件，编排所有模块；**Phase 4 新增聚合分流入口，FEATURE_FLAGS 控制** |
| _（根目录）_ | `window_flush_handler.py` | **Phase 4 新增**：窗口到期批量处理 Lambda；由 EventBridge Scheduler 触发 |
| _（根目录）_ | `config.py` ★ | 服务名映射（★ 现在从 `profiles/petsite.yaml` 动态加载，硬编码作为回退）+ FEATURE_FLAGS |
| **core/** | `rca_engine.py` ★ | 多层 RCA 引擎：DeepFlow L7/L4 + CloudTrail + Neptune 图谱 + AWS 探针 + 评分；**Phase 4 新增 `analyze_group()`** |
| **core/** | `fault_classifier.py` ★ | P0/P1/P2 严重度分级；自动执行门控；**Phase 4 新增 `classify_group()`** |
| **core/** | `graph_rag_reporter.py` ★ | Graph RAG：Neptune 子图 + 所有探针信号 → Claude → 结构化报告；**Phase 4 新增 `generate_group_report()`** |
| **core/** | `event_normalizer.py` | **Phase 4 新增**：`UnifiedAlertEvent` 统一事件模型 + `EventNormalizer` 标准化所有告警源 |
| **core/** | `alert_buffer.py` | **Phase 4 新增**：DynamoDB `gp-alert-buffer` 表操作；2 分钟聚合窗口；P0 直通 bypass |
| **core/** | `topology_correlator.py` | **Phase 4 新增**：`EventGroup` 拓扑关联；Neptune blast_radius + upstream 识别；下游症状归并根因 |
| **core/** | `decision_engine.py` | **Phase 4 新增**：自动化策略矩阵（severity × confidence）；编排 RCA 后续动作 |
| **neptune/** | `neptune_client.py` | 带 IAM SigV4 签名的 Neptune HTTP 客户端 |
| **neptune/** | `neptune_queries.py` ★ | Neptune openCypher 查询 Q1–Q20（服务层 + 基础设施层 + 非结构化层 + 反馈层） |
| **neptune/** | `schema_prompt.py` | 图谱 Schema LLM Prompt + 6 个 few-shot 示例（NL 查询用） |
| **neptune/** | `nl_query.py` | `NLQueryEngine`：自然语言→openCypher→执行→摘要（Bedrock Claude） |
| **neptune/** | `query_guard.py` | openCypher 安全校验：屏蔽写操作、限制跳数、强制 LIMIT |
| **collectors/** | `infra_collector.py` | 实时 Pod 状态（K8s API）+ DB 指标（CloudWatch RDS） |
| **collectors/** | `aws_probers.py` | ★ **插件化 AWS 服务探针**（SQS/DynamoDB/Lambda/ALB/EC2/StepFunctions） |
| **collectors/** | `eks_auth.py` | EKS Bearer Token 生成（SigV4 预签名 STS URL） |
| **actions/** | `action_executor.py` | kubectl 操作：rollout restart/undo、scale 副本数 |
| **actions/** | `playbook_engine.py` | 故障 Playbook 匹配（4 种预定义模式） |
| **actions/** | `semi_auto.py` | P1/P2 半自动执行流程；Slack 确认交互 |
| **actions/** | `slack_notifier.py` | Slack 消息格式化 + Webhook 推送 |
| **actions/** | `incident_writer.py` | Neptune Incident 节点 + 实体提取（`MentionsResource` 边）+ S3 归档 + Bedrock KB + S3 Vectors 索引 |
| **actions/** | `feedback_collector.py` | **Phase 4 新增**：Slack 反馈收集（确认/否定 RCA 结论）+ Neptune 写回（更新 Incident 节点） |
| **search/** | `incident_vectordb.py` | S3 Vectors Incident 索引：分块 + 向量化（Bedrock Titan v2）+ 语义搜索 |
| **data/** | `service-db-mapping.json` | 服务 → DB 集群映射关系 |
| **scripts/** | `scan-service-db-mapping.py` | 扫描 K8s Deployment 发现服务→DB 关联关系 |
| **scripts/** | `graph-ask.py` | CLI：自然语言提问图谱，返回 Cypher + 结果 + 中文摘要 |

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
├── handler.py                  # Lambda 入口（含 Phase 4 聚合分流，必须保留在根目录）
├── window_flush_handler.py     # Phase 4：窗口到期批量处理 Lambda
├── config.py                   # K8s Deployment ↔ Neptune 名称映射 + FEATURE_FLAGS
├── __init__.py
├── core/                       # 核心 RCA 逻辑
│   ├── rca_engine.py           # 多层 RCA 引擎（含 analyze_group）
│   ├── fault_classifier.py     # P0/P1/P2 严重度分级（含 classify_group）
│   ├── graph_rag_reporter.py   # Bedrock Claude Graph RAG 报告（含 generate_group_report）
│   ├── event_normalizer.py     # Phase 4：UnifiedAlertEvent + EventNormalizer
│   ├── alert_buffer.py         # Phase 4：DynamoDB 缓冲窗口（2 min，P0 bypass）
│   ├── topology_correlator.py  # Phase 4：EventGroup 拓扑关联（blast_radius + upstream）
│   └── decision_engine.py      # Phase 4：自动化策略矩阵（severity × confidence）
├── neptune/                    # 图数据库层
│   ├── neptune_client.py       # SigV4 签名 HTTP 客户端
│   ├── neptune_queries.py      # Q1-Q20 openCypher 查询
│   ├── schema_prompt.py        # 图谱 Schema Prompt + few-shot 示例
│   ├── nl_query.py             # NLQueryEngine：自然语言→openCypher（Bedrock Claude）
│   └── query_guard.py          # 安全校验：屏蔽写操作、限制跳数、强制 LIMIT
├── collectors/                 # 实时数据采集
│   ├── infra_collector.py      # K8s Pod 状态 + RDS 指标
│   ├── aws_probers.py          # ★ 插件化 AWS 服务探针（第二层）
│   └── eks_auth.py             # EKS Bearer Token 生成
├── actions/                    # 执行与通知
│   ├── action_executor.py      # kubectl rollout/scale 操作
│   ├── playbook_engine.py      # 故障 Playbook 匹配
│   ├── semi_auto.py            # 半自动执行流程
│   ├── slack_notifier.py       # Slack Webhook 推送
│   ├── incident_writer.py      # Neptune + 实体提取 + S3 + Bedrock KB + S3 Vectors
│   └── feedback_collector.py   # Phase 4：Slack 反馈收集 + Neptune 写回
├── search/
│   └── incident_vectordb.py    # S3 Vectors Incident 语义搜索
├── data/
│   └── service-db-mapping.json # 服务 → DB 集群映射
├── scripts/
│   ├── scan-service-db-mapping.py
│   └── graph-ask.py            # CLI：自然语言图谱查询
├── tests/
│   └── test_rca.py             # 单元测试
├── docs/
│   ├── TDD-fault-recovery-rca.md
│   └── RCA-SYSTEM-DOC.md
├── deploy.sh                   # Lambda 打包 + 部署脚本
├── .env.example
├── README.md                   # 英文文档
└── README_CN.md                # 中文文档（本文件）
```

---

---

## Phase A：非结构化数据整合

### 实体提取 & MentionsResource 边

`actions/incident_writer.py` 从 RCA 报告文本中提取实体（服务名 + EC2 实例 ID），并在 Neptune 中创建 `Incident -[:MentionsResource]→ Resource` 边。Q17 利用这些边查找涉及相同资源的历史故障。

### 混沌实验整合（Q18）

每次混沌实验 Phase 5 结束后，`chaos/code/neptune_sync.py` 写入 `ChaosExperiment` 节点，并建立 `Microservice -[:TestedBy]→ ChaosExperiment` 边。Q18 查询该历史为 RCA 提供上下文。

### 增强 Graph RAG 上下文

`core/graph_rag_reporter.py` 在 RCA 报告中新增三段历史上下文：

1. **历史故障记录** — Q17：涉及相同资源的历史 Incident
2. **混沌实验历史** — Q18：受影响服务的过往实验
3. **语义相似故障** — S3 Vectors 语义搜索

---

## Phase B：自然语言图谱查询

### 自然语言查询引擎

```python
from neptune.nl_query import NLQueryEngine

engine = NLQueryEngine()
result = engine.query("petsite 依赖哪些数据库？")
# result = { "question": ..., "cypher": ..., "results": [...], "summary": "..." }
```

### CLI 工具

```bash
cd rca
python3 scripts/graph-ask.py "petsite 的所有下游依赖有哪些？"
python3 scripts/graph-ask.py "哪些 Tier0 服务没做过混沌实验？"
python3 scripts/graph-ask.py "AZ ap-northeast-1a 有多少个 Pod？"
python3 scripts/graph-ask.py "最近一周发生了几次 P0 故障？"
```

### 安全校验（query_guard.py）

| 规则 | 说明 |
|------|------|
| 写操作屏蔽 | 拒绝含 `CREATE / DELETE / SET / MERGE / REMOVE / DROP / CALL` 的查询 |
| 跳数上限 | 拒绝可变长度遍历深度 > 6 的查询 |
| LIMIT 强制 | 无 LIMIT 子句时自动追加 `LIMIT 200` |

### Incident 语义搜索（S3 Vectors）

```python
from search.incident_vectordb import index_incident, search_similar

# 故障写入时自动触发（incident_writer.py）
index_incident(incident_id, report_text, metadata)

# 下次 RCA 时自动调用（graph_rag_reporter.py）
results = search_similar("DynamoDB 限流导致服务超时", top_k=3)
```

成本：**< $0.02/月**（vs OpenSearch Serverless ~$30+/月）

---

## Phase 4：告警降噪与智能事件管理

### 背景与动机

在大规模故障场景下，单一根因往往触发数十条级联告警（ALB 5xx → Pod 异常 → DB 连接超时 → 业务指标下降）。若每条告警都独立触发一次 RCA，不仅造成重复分析噪声，还会影响 on-call 人员对根因的判断。Phase 4 在告警入口处引入三层聚合机制，将同一故障链的多条告警收敛为 **1 个 EventGroup**，仅触发 1 次 RCA。

### 六层架构与数据流

```
Layer 0: 原始告警
  CloudWatch Alarm × N  →  SNS Topic  →  handler.py
                                               │
                                    FEATURE_FLAGS.alert_aggregation
                                         enabled?
                                        YES /     \ NO（直通）
                                           ▼
Layer 1: 事件标准化
  core/event_normalizer.py
  UnifiedAlertEvent { alert_id, source, severity, affected_service,
                      group_key, raw_payload, timestamp }
                                           │
                                           ▼
Layer 2: 缓冲聚合
  core/alert_buffer.py
  DynamoDB gp-alert-buffer
  - TTL 2 分钟（P0 立即 bypass）
  - group_key = hash(affected_service + fault_type)
  - 同 group_key 的告警追加到同一条目（set_add）
                                           │
                              窗口到期 / 窗口满 / P0 直通
                                           ▼
Layer 3: 拓扑关联
  core/topology_correlator.py
  - Q1 blast_radius：识别下游受影响节点
  - Q3 upstream_deps：识别潜在根因上游
  - 下游症状告警归并 → EventGroup { root_service, symptom_services, alerts[] }
                                           │
                                           ▼
Layer 4: 决策编排
  core/decision_engine.py
  策略矩阵（severity × confidence）：
  ┌──────────┬──────────┬────────────────────────────┐
  │ severity │confidence│ 动作                        │
  ├──────────┼──────────┼────────────────────────────┤
  │ P0       │ ≥80      │ 自动执行 + Slack 通知        │
  │ P0       │ <80      │ Slack 确认后执行             │
  │ P1/P2    │ ≥70      │ Slack 通知 + 建议操作        │
  │ P1/P2    │ <70      │ Slack 通知（仅信息）         │
  └──────────┴──────────┴────────────────────────────┘
                                           │
                                           ▼
Layer 5: RCA 分析
  core/rca_engine.analyze_group()
  core/fault_classifier.classify_group()
  core/graph_rag_reporter.generate_group_report()
  （与单告警 RCA 逻辑复用，输入为 EventGroup）
                                           │
                                           ▼
Layer 6: 闭环反馈
  actions/feedback_collector.py
  - Slack 交互按钮：[✅ 确认根因] [❌ 否定] [🔄 重新分析]
  - 反馈写回 Neptune：更新 Incident.feedback 属性
  - Q19 查询已确认 Incident；Q20 统计误报率
  - 误报率持续升高时自动调整置信度阈值
```

### 窗口到期处理

```bash
# EventBridge Scheduler 每 2 分钟触发一次 gp-window-flush Lambda
# window_flush_handler.py 扫描到期的 AlertBuffer 窗口 → 触发 EventGroup 处理
```

### 新增基础设施（CDK AlertBufferStack）

| 资源 | 说明 |
|------|------|
| DynamoDB `gp-alert-buffer` | 告警缓冲表；TTL 字段自动清理过期窗口 |
| Lambda `gp-window-flush` | 窗口到期批量处理；由 EventBridge Scheduler 触发 |
| EventBridge Scheduler Role | 允许 Scheduler 调用 `gp-window-flush` Lambda |

### 部署 AlertBufferStack

```bash
cd infra
# 1. 部署 CDK 栈（首次）
cdk diff --app "npx ts-node bin/graph-dp.ts" AlertBufferStack
cdk deploy --app "npx ts-node bin/graph-dp.ts" AlertBufferStack

# 2. 获取输出值并更新 Lambda 环境变量
BUFFER_TABLE=$(aws cloudformation describe-stacks \
  --stack-name AlertBufferStack \
  --query 'Stacks[0].Outputs[?OutputKey==`BufferTableName`].OutputValue' \
  --output text --region ap-northeast-1)

SCHEDULER_ROLE=$(aws cloudformation describe-stacks \
  --stack-name AlertBufferStack \
  --query 'Stacks[0].Outputs[?OutputKey==`SchedulerRoleArn`].OutputValue' \
  --output text --region ap-northeast-1)

FLUSH_FN=$(aws cloudformation describe-stacks \
  --stack-name AlertBufferStack \
  --query 'Stacks[0].Outputs[?OutputKey==`WindowFlushFunctionArn`].OutputValue' \
  --output text --region ap-northeast-1)

aws lambda update-function-configuration \
  --function-name petsite-rca-engine \
  --environment "Variables={BUFFER_TABLE_NAME=$BUFFER_TABLE,SCHEDULER_ROLE_ARN=$SCHEDULER_ROLE,WINDOW_FLUSH_FUNCTION_ARN=$FLUSH_FN}" \
  --region ap-northeast-1
```

### FEATURE_FLAGS 说明

Phase 4 全部能力通过 `config.py` 中的 `FEATURE_FLAGS` 字典控制，**默认全部关闭**，可按需逐步开启：

```python
FEATURE_FLAGS = {
    "alert_aggregation": False,    # 告警聚合缓冲（Layer 1-3）
    "topology_correlation": False, # 拓扑驱动关联（Layer 3）
    "decision_engine": False,      # 自动化策略矩阵（Layer 4）
    "feedback_loop": False,        # Slack 闭环反馈（Layer 6）
}
```

通过环境变量覆盖（无需重新部署代码）：

```bash
aws lambda update-function-configuration \
  --function-name petsite-rca-engine \
  --environment "Variables={FEATURE_FLAG_ALERT_AGGREGATION=true}" \
  --region ap-northeast-1
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
6. **Phase 4 默认关闭**：告警聚合（`alert_aggregation`）、拓扑关联（`topology_correlation`）、决策引擎（`decision_engine`）和闭环反馈（`feedback_loop`）四个 FEATURE_FLAGS 默认全部为 `False`，需部署 CDK AlertBufferStack 并手动开启。开启前请确保 `gp-alert-buffer` DynamoDB 表已创建、`BUFFER_TABLE_NAME` 等环境变量已配置。
7. **告警聚合窗口延迟**：开启聚合后，非 P0 告警最多等待 2 分钟才会触发 RCA，对响应时延有一定影响。P0 告警走 bypass 通道，不受此限制。
