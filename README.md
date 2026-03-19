# rca_engine — PetSite 故障恢复与 RCA 引擎

Lambda `petsite-rca-engine` 的完整源代码（ap-northeast-1）。

## 架构概览

```
SNS Alert / CW Alarm
        │
        ▼
  handler.py              ← Lambda 主入口，编排各模块
        │
  ┌─────┼──────────────────────────────────┐
  ▼     ▼                                  ▼
fault_  rca_engine.py               graph_rag_reporter.py
class-  （Neptune Q1-Q7               （Bedrock Claude Sonnet 4-6
ifier   规则引擎 RCA）                  Graph RAG 分析）
  │           │                               │
  │     neptune_queries.py           infra_collector.py
  │     Q1 影响面  Q2 Tier0           实时采集 Pod/AZ（K8s API）
  │     Q3 上游依赖 Q4 服务信息        实时采集 DB指标（CloudWatch）
  │     Q5 历史    Q6 Pod状态 *
  │     Q7 DB状态 *
  │     (* 从 Neptune 图谱读，
  │        ETL 每 15 分钟同步)
  ▼
playbook_engine.py → semi_auto.py → action_executor.py
（故障手册）          （半自动执行）    （kubectl rollout/scale）
        │
        ▼
  slack_notifier.py   ← 发 Slack 消息 + 确认按钮
  incident_writer.py  ← Neptune Incident 节点写回 + S3存档 + KB索引
```

## 模块说明

| 文件 | 功能 |
|------|------|
| `handler.py` | Lambda 入口，解析 SNS/CW 事件，串联所有模块 |
| `fault_classifier.py` | 故障分级 P0/P1/P2，决定是否允许自动执行 |
| `rca_engine.py` | 规则引擎 RCA：DeepFlow + CloudTrail + Neptune 五路证据 |
| `neptune_queries.py` | Neptune openCypher 查询 Q1~Q7 |
| `neptune_client.py` | Neptune HTTP 客户端（SigV4 签名） |
| `graph_rag_reporter.py` | Graph RAG：Neptune子图 + DeepFlow + 基础设施层 → Claude → RCA报告 |
| `infra_collector.py` | 实时采集 Pod状态/AZ（K8s API）+ DB指标（CloudWatch RDS） |
| `playbook_engine.py` | 根据根因选择故障手册（4个 Playbook） |
| `semi_auto.py` | P1/P2 半自动执行逻辑，发 Slack 确认按钮 |
| `action_executor.py` | kubectl 操作执行（rollout restart/undo, scale） |
| `slack_notifier.py` | Slack 消息格式化 + Incoming Webhook 发送 |
| `incident_writer.py` | 写入 Neptune Incident 节点 + S3 存档 + Bedrock KB 索引 |
| `scan-service-db-mapping.py` | 维护工具：扫描 K8s Deployments 发现服务→DB 映射关系 |
| `service-db-mapping.json` | 扫描结果：服务→DB 映射表（更新后需同步到 ETL `SERVICE_DB_MAPPING`）|

## Neptune 图谱结构（含基础设施层）

```
Microservice(pethistory)
  ──Calls──▶ Microservice(petsite)
  ──RunsOn──▶ Pod(pethistory-xxx) ──LocatedIn──▶ AZ(ap-northeast-1a)
  ──RunsOn──▶ Pod(pethistory-yyy) ──LocatedIn──▶ AZ(ap-northeast-1c)
  ──ConnectsTo──▶ Database(adoptions)
                   ──BelongsTo──▶ RDSCluster(serviceseks2-database...)
  ──TriggeredBy──▶ Incident(inc-2026-xx)
```

Pod/Database/EC2Instance 节点由 `neptune-etl-from-aws` Lambda（Step 8b）每 15 分钟同步一次。

EC2Instance（EKS Node）现在直连 AZ：
```
EC2Instance(petsite-eks-node-1) ──LocatedIn──▶ AZ(ap-northeast-1a)
EC2Instance(petsite-eks-node-2) ──LocatedIn──▶ AZ(ap-northeast-1c)
```
三层 AZ 关联均已打通：Pod → AZ、EC2Instance → AZ、RDSInstance → AZ。

## 数据流：两路基础设施数据

| 来源 | 更新频率 | 用途 |
|------|---------|------|
| Neptune Q6/Q7（ETL写入） | 每15分钟 | 图遍历、关联分析、历史趋势 |
| infra_collector（实时） | 告警触发时 | 故障瞬间的 Pod重启数、DB连接数/CPU |

两路数据并存注入 Claude Prompt，互补：ETL 提供图谱关系，实时采集提供最新状态。

## 置信度评分体系

```
confidence_breakdown:
  deepflow:    0-40  （有5xx数据 → +40）
  cloudtrail:  0-30  （近期有变更 → +30）
  graph:       0-20  （确认为链路起点 → +20）
  history:     0-10  （Bedrock KB 历史案例匹配 → +10）
```

## 关键配置（Lambda 环境变量）

| 变量 | 值 |
|------|-----|
| NEPTUNE_ENDPOINT | petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com |
| EKS_CLUSTER_NAME | PetSite |
| REGION | ap-northeast-1 |
| BEDROCK_MODEL | global.anthropic.claude-sonnet-4-6 |

## 部署

```bash
cd /home/ubuntu/tech/rca_engine
bash deploy.sh
```

`deploy.sh` 会自动执行：`pip install -t . -q` → 打 zip → `aws lambda update-function-code`

## 相关代码与文档

| 路径 | 说明 |
|------|------|
| [graph-dp-cdk](../graph-dp-cdk/) | Neptune ETL CDK 项目（构建依赖图谱）|
| `./scan-service-db-mapping.py` | 服务→DB映射扫描工具 |
| `./service-db-mapping.json` | 扫描结果：服务→DB 映射表 |
