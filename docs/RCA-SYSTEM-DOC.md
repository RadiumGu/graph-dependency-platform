# PetSite 故障恢复 & 根因分析系统 — 技术文档

> 权威文档，与代码同步维护
> 最后更新：2026-03-19

---

## 一、系统架构概览

```
CloudWatch Alarm
      ↓ SNS
Lambda: rca-engine
      ├── 故障分类（P0/P1/P2）
      ├── Playbook 匹配
      ├── 半自动/全自动执行（LOW/MEDIUM 风险）
      │     └── Slack Block Kit 按钮（MEDIUM 风险确认）
      │           └── API Gateway → Lambda: rca-interaction
      ├── RCA 引擎（P0/P1）
      │     ├── DeepFlow 调用链（ClickHouse HTTP）
      │     ├── CloudWatch 指标（CPU/ALB 5xx）
      │     ├── CloudTrail 变更事件
      │     └── Neptune 子图（服务依赖拓扑）
      ├── Graph RAG 报告（Bedrock Claude Sonnet 4-6）
      └── Incident 节点写回 Neptune
```

---

## 二、AWS 资源清单

| 资源类型 | 名称/标识符 | 说明 |
|---------|-----------|------|
| Lambda | `rca-engine` | 主引擎 |
| Lambda | `rca-interaction` | Slack 按钮回调 |
| API Gateway | `POST /slack/interact` | 交互端点 |
| SNS | `rca-alerts` | 告警接收 Topic |
| CW Alarm | `rca-alb-5xx-high` | ALB 5xx 触发器 |
| IAM Role | 见 `.env` `ROLE_ARN` | 最小权限执行角色 |
| SSM | `${SSM_SLACK_WEBHOOK_PATH}` | Slack Webhook URL |
| SSM | `/rca/slack/interact-url` | API Gateway URL |
| Neptune | `${NEPTUNE_ENDPOINT}:${NEPTUNE_PORT}` | 知识图谱 |
| Bedrock | `${BEDROCK_MODEL}` | RCA 报告生成 |
| Bedrock KB | `${BEDROCK_KB_ID}` | 历史案例语义检索 |

> 具体资源 ID 配置在 `.env` 文件中，参见 `.env.example`。

---

## 三、代码结构

```
rca_engine/                      ← 仓库根目录（也是 Lambda zip 根目录）
├── handler.py                   # Lambda 入口（handler.lambda_handler）
├── __init__.py                  # 使目录成为 rca_engine Python 包
├── config.py                    # K8s deployment ↔ Neptune 服务名规范映射
├── eks_auth.py                  # EKS token 获取（SigV4 presigned STS URL）
├── fault_classifier.py          # P0/P1/P2 故障分类
├── playbook_engine.py           # Playbook 匹配（4 个预定义 + 动态降级）
├── semi_auto.py                 # 半自动/全自动执行逻辑
├── action_executor.py           # kubectl 操作封装（rollout/scale）
├── slack_notifier.py            # Slack 告警（Block Kit 按钮）
├── neptune_client.py            # Neptune openCypher HTTP 客户端（SigV4）
├── neptune_queries.py           # Q1-Q8 预定义查询
├── rca_engine.py                # RCA 5步分析（DeepFlow+CloudTrail+Neptune）
├── graph_rag_reporter.py        # Graph RAG（Neptune+CW+DeepFlow → Bedrock）
├── infra_collector.py           # 实时采集 Pod 状态/DB 指标
├── incident_writer.py           # Incident 节点写回 Neptune + S3 + KB 索引
├── service-db-mapping.json      # 服务 → DB 映射表
├── scan-service-db-mapping.py   # 维护工具：扫描 K8s Deployments
├── deploy.sh                    # 部署脚本（读取 .env）
├── .env.example                 # 环境变量模板
├── tests/
│   └── test_rca.py              # 单元测试（step4_score, classify, match）
└── docs/
    ├── TDD-fault-recovery-rca.md  # 技术设计文档
    └── RCA-SYSTEM-DOC.md          # 本文档
```

---

## 四、故障严重度分类

| 严重度 | 触发条件 | 执行策略 |
|--------|---------|---------|
| P0 | tier0 服务 + 高错误率 | 禁止自动执行，Diagnose-First，人工确认 |
| P1 | tier0/tier1 + 中等影响 | suggest 模式 + Slack 按钮确认（MEDIUM 风险） |
| P2 | tier1/tier2 + 低影响 | LOW 风险全自动执行 |

---

## 五、Playbook 列表

| Playbook ID | 触发场景 | 风险等级 | 默认操作 |
|------------|---------|---------|---------|
| `db_connection_exhausted` | RDS 连接数 >80% | LOW | `rollout_restart` |
| `alb_5xx_spike` | ALB 5xx >5% | DEPENDS | 根据历史判断 |
| `crashloop` | Pod CrashLoopBackOff | MEDIUM | `rollout_undo` |
| `single_az_down` | 单 AZ 不可用 | LOW | `scale_deployment` |

---

## 六、Graph RAG RCA 报告流程

```
1. Neptune 子图提取：服务 2-hop 依赖拓扑
2. DeepFlow 调用链：最近 30 分钟 5xx 错误时序
3. CloudWatch 指标：ALB 5xx 总量 + Pod CPU 均值/峰值
4. CloudTrail 变更：近 30 分钟的部署/配置变更事件
5. 历史 Incident：Neptune 中同服务历史故障记录
6. Bedrock KB：语义相似历史案例检索

→ 组装 Prompt → Bedrock Claude Sonnet 4-6
→ 输出：根因 + 置信度(0-100) + 建议操作 + 推理过程 + 影响范围
```

**降级策略**：Bedrock 调用失败时自动降级为规则引擎结果（`rule_engine_fallback`）

---

## 七、Slack 交互按钮

- **MEDIUM 风险** Playbook → 发送带按钮的 Block Kit 消息
- 点击 **[确认执行]** → API Gateway → `rca-interaction` Lambda → 异步触发执行
- 点击 **[跳过]** → 消息更新为跳过状态
- 点击后消息立即替换（防重复点击）

Slack App Interactivity URL 存储在 SSM `/rca/slack/interact-url`。

---

## 八、速率限制

- 同一服务自动操作 **≤3次/30分钟**（SSM 计数器）
- P0 严重度：**永远不自动执行**
- 重置方式：`aws ssm delete-parameter --name /rca/rate-limit/{service}`

---

## 九、K8s 权限

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

## 十、置信度评分体系

```
confidence_breakdown (总分 = 四项之和，最高 100):
  deepflow:    0-40  — DeepFlow 有 5xx 调用链时序证据
  cloudtrail:  0-30  — 有近期配置变更事件关联
  graph:       0-20  — Neptune 图谱确认为链路起点
  history:     0-10  — Bedrock KB 找到相似历史案例
```

额外：`step3b_temporal_validation` 交叉验证 DeepFlow `first_error` 时间戳与 Neptune 图路径深度的一致性（+0~10 causal_score）。

---

## 十一、已知问题 & 改进方向

| 问题 | 优先级 | 说明 |
|------|--------|------|
| 因果边无权重（`error_propagation_weight`） | ⭐⭐ | 误判上游为根因 |
| 异常子图模式匹配未实现 | ⭐⭐⭐ | 历史匹配只统计次数 |
| DeepFlow `app_service` 字段空 | ⭐ | 已绕过（用 request_domain 解析） |
