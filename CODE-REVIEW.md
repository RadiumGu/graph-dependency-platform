# 🔍 `rca_engine` 代码审阅报告

**审阅日期**：2026-03-19
**审阅目标**：该代码要共享给他人使用/参考
**项目定位**：基于 Neptune 依赖图谱的故障根因分析引擎，接收 CloudWatch 告警 → 故障分级 → 自动/半自动恢复 → Graph RAG RCA 报告 → Incident 知识库闭环

**代码规模**：12 个 Python 模块，~2500 行

**参考文档**：
- `/home/ubuntu/tech/rca/TDD-fault-recovery-rca.md` — 技术设计文档（权威）
- `/home/ubuntu/tech/rca/RCA-SYSTEM-DOC.md` — 系统技术文档（含资源清单）

---

## 整体评价

这是一个**设计非常完整**的故障分析系统——从告警接收、故障分级、Playbook 匹配、半自动恢复、5 步 RCA 分析、Graph RAG 报告生成，到 Incident 知识库闭环和重复故障检测，覆盖了故障管理的完整生命周期。模块划分清晰（12 个文件各司其职），代码注释充分，设计意图透明。

但作为共享项目，有几个层面需要改进。

---

## 【AWS PSA 视角】

### ⚠️ 1. 敏感信息硬编码（与 graph-dp-cdk 同类问题）

全仓库有 **19 处**硬编码的敏感值：

| 内容 | 出现位置 |
|------|---------|
| Account ID `926093770964` | `deploy.sh` |
| Neptune endpoint（含 cluster ID `czbjnsviioad`） | `neptune_client.py`、`deploy.sh` |
| ClickHouse 内网 IP `11.0.2.30` | `rca_engine.py` |
| VPC Subnet/SG ID | `deploy.sh` |
| Bedrock KB ID `0RWLEK153U` | `graph_rag_reporter.py` |
| EKS Cluster 名 `PetSite` | 多处 |
| SNS Topic ARN | `deploy.sh` |
| Slack Webhook URL 路径 | `deploy.sh` |
| SSM Parameter 路径 `/petsite/*` | `slack_notifier.py`、`action_executor.py` |

**建议**：
- `deploy.sh` 顶部的变量全部改为从 `.env` 或参数读取，提供 `.env.example`
- Python 代码中的 `os.environ.get('X', '<硬编码默认值>')` 把默认值改为空字符串或占位符
- `KB_ID` 和 `BEDROCK_MODEL` 同样应该走环境变量

### ⚠️ 2. `playbook_engine.py` 动态建议中硬编码了内网 IP

```python
steps.insert(1, '查 DeepFlow：`curl http://11.0.2.30:20416/...` 确认根因')
```

这不仅是敏感信息泄露，而且对共享的接收者来说是无意义的。应该改为 `$CLICKHOUSE_HOST` 或移除。

### 3. SSL 验证全部禁用

两处 SSL 问题：
- `neptune_client.py`：`verify=False`
- `infra_collector.py`：`ssl.CERT_NONE`

Neptune 的 SSL 证书是有效的 RDS CA 签发证书，不需要禁用。K8s API 可以用 EKS 返回的 CA data 做验证（`action_executor.py` 里正确地用了 `_write_ca()`，但 `infra_collector.py` 没有）。

### 4. `DEPLOYMENT_TO_SVC` 和 `SVC_TO_DEPLOYMENT` 双向映射不一致

| 模块 | 映射方向 | 内容 |
|------|---------|------|
| `rca_engine.py` | deployment → neptune | `DEPLOYMENT_TO_SVC` |
| `action_executor.py` | neptune → deployment | `SVC_TO_DEPLOYMENT` |

这两个映射**不是精确的互逆**。比如：
- `rca_engine.py`：`'pethistory-deployment': 'petadoptionshistory'`、`'pethistory-service': 'petadoptionshistory'`（两个 key 映射到同一个 value）
- `action_executor.py`：`'petadoptionshistory': 'pethistory-deployment'`（只保留了一个反向）

加上 `graph-dp-cdk` 里的 `K8S_SERVICE_ALIAS`，同一组映射关系已经在三个地方维护了。应该统一到一个配置文件。

### 5. `graph_rag_reporter.py` 的 Prompt 注入风险

```python
prompt = f"""你是一位资深 SRE...
故障概况：
- 受影响服务：{affected_service}
...
{df_text}
{cw_text}
...
"""
```

`affected_service` 和各种文本直接拼入 prompt。虽然当前数据来源是内部系统，但如果未来有外部输入（如用户通过 Slack 手动触发并指定服务名），可能存在 prompt injection 风险。建议至少做 service name 的白名单校验。

### 6. `_get_eks_token()` 实现了三份

| 文件 | 函数 |
|------|------|
| `action_executor.py` | `_get_eks_token()` |
| `infra_collector.py` | `_get_k8s_token()` |
| `graph-dp-cdk/lambda/etl_deepflow/neptune_etl_deepflow.py` | `get_eks_token()` |

三份实现方式略有差异（一个用 `SigV4QueryAuth`，一个用 `SigV4Auth` + `base64`，一个手动构造）。应该统一为一个共享工具函数。

---

## 【客户 PA 视角】

### ⚠️ 1. 缺少独立的 README

当前 README 是从 `graph-dp-cdk` 里带过来的，内容是模块说明级别的。作为独立共享项目需要：
- **项目概述**：这个系统做什么、解决什么问题
- **架构图**：告警 → Lambda → 各模块的数据流
- **前置条件**：Neptune 图谱（需先用 graph-dp-cdk 构建）、EKS、DeepFlow、Bedrock、Slack
- **部署指南**：step-by-step
- **配置说明**：环境变量完整列表
- **使用示例**：如何手动触发测试

TDD 文档（`TDD-fault-recovery-rca.md`）写得很好，但它在仓库外面。应该把核心设计文档纳入仓库，或在 README 中充分描述。

### ⚠️ 2. `deploy.sh` 不可移植

- 硬编码了 Account ID、subnet、SG、Neptune endpoint
- 假设了特定的 IAM Role（`neptune-etl-lambda-role`）
- 没有参数化
- 缺少前置条件检查（aws cli、zip 是否安装）

对于共享项目，接收者无法直接使用这个脚本。建议：
- 所有变量从 `.env` 或命令行参数读取
- 添加 `--dry-run` 模式
- 添加前置条件检查
- 或者更好的方案：**把 RCA Lambda 也纳入 CDK 管理**（在 graph-dp-cdk 中加一个 RcaStack）

### 3. `handler.py` 的 import 结构不常规

```python
def lambda_handler(event, context):
    from rca_engine import fault_classifier, playbook_engine, semi_auto, rca_engine, slack_notifier
```

所有 import 都在函数内部。这是 Lambda 冷启动优化的常见做法（延迟加载），但对于共享代码来说不够清晰。建议在文件顶部加注释说明为什么这样做。

### 4. `service-db-mapping.json` 与 `infra_collector.py` 中的 `STATIC_DB_MAPPING` 重复

`infra_collector.py` 里硬编码了一份 `STATIC_DB_MAPPING`，注释说"写入代码避免文件路径问题"。但同时 `service-db-mapping.json` 文件也存在。两者的维护会不同步。

建议统一为读取 JSON 文件（Lambda 打包时包含），或完全通过 Neptune 图谱查询（`ConnectsTo` 边已经由 ETL 写入了）。

### 5. 没有测试

关键的评分逻辑（`step4_score`）、故障分级（`classify`）、Playbook 匹配（`match`）都是可以独立单元测试的纯函数。当前 0 测试。

### 6. `RCA-SYSTEM-DOC.md` 中的代码结构描述已过时

```
/home/ubuntu/tech/rca/lambda/
├── handler.py
├── rca_engine/
│   ├── fault_classifier.py
```

这是旧的目录结构，代码已经移到 `/home/ubuntu/tech/rca_engine/`。如果这份文档要随代码共享，需要更新。

---

## 【共同关注点】

1. **敏感信息**（Account ID、内网 IP、Neptune endpoint、KB ID）必须在共享前清除
2. **缺少可用的 README 和部署指南**
3. **服务名映射表多处重复维护**（rca_engine 内部 2 份 + graph-dp-cdk 至少 2 份 = 4 份）
4. **SSL 验证禁用**需要修复
5. **设计文档（TDD、RCA-SYSTEM-DOC）应随代码一起共享**，但需要先更新

---

## 【行动建议】（优先级排序）

### 🔴 P0 — 共享前必须完成

1. **清除所有敏感信息**
   - `deploy.sh`：Account ID、Subnet/SG、Neptune endpoint、SNS ARN → 读取 `.env` 或参数
   - `neptune_client.py`、`rca_engine.py`：Neptune endpoint、ClickHouse IP → 默认值改为占位符 `<NEPTUNE_ENDPOINT>`
   - `graph_rag_reporter.py`：`KB_ID` → 环境变量
   - `playbook_engine.py`：移除内网 IP
   - `deploy.sh`：提供 `.env.example`

2. **重写 README.md**
   - 项目概述 + 架构图
   - Prerequisites（Neptune 图谱依赖 graph-dp-cdk）
   - 环境变量完整列表（从所有 `os.environ.get()` 调用中汇总）
   - 部署步骤
   - 手动测试示例

3. **纳入设计文档**
   - 将 `TDD-fault-recovery-rca.md` 复制到 `docs/` 目录（或精简后纳入 README）
   - 更新 `RCA-SYSTEM-DOC.md` 中的目录结构描述

### 🟡 P1 — 代码质量

4. **统一服务名映射**
   - 创建 `config.py` 或 `service_mapping.json`，统一管理 K8s deployment ↔ Neptune 服务名的映射
   - `rca_engine.py`（`DEPLOYMENT_TO_SVC`）和 `action_executor.py`（`SVC_TO_DEPLOYMENT`）引用同一数据源
   - 理想情况下，这个映射应该和 `graph-dp-cdk` 共享（可以作为 JSON 配置文件在两个项目间同步）

5. **统一 EKS token 获取**
   - 提取 `_get_eks_token()` 到独立模块（如 `eks_auth.py`），`action_executor.py` 和 `infra_collector.py` 共用

6. **修复 SSL 验证**
   - `neptune_client.py`：使用 RDS CA bundle（`verify='/path/to/rds-combined-ca-bundle.pem'`）
   - `infra_collector.py`：使用 EKS 返回的 CA data（参考 `action_executor.py` 的做法）

7. **消除 `STATIC_DB_MAPPING` 重复**
   - 统一读取 `service-db-mapping.json`，或直接从 Neptune 查询 `ConnectsTo` 边

### 🟢 P2 — 加分项

8. **添加单元测试**（至少覆盖 `step4_score`、`classify`、`match`）
9. **`handler.py` 顶部加注释**说明延迟 import 的原因（冷启动优化）
10. **`deploy.sh` 添加 `--dry-run` 模式**和前置条件检查
11. **考虑将 RCA Lambda 纳入 CDK 管理**（在 graph-dp-cdk 中加 RcaStack，避免手动 shell 部署）
12. **添加 LICENSE 文件**
13. **`graph_rag_reporter.py` 的 prompt 中 service name 做白名单校验**

---

## 总评

这个 RCA 引擎的设计水平很高——5 步 RCA 分析（DeepFlow 调用链 → CloudTrail 变更 → Neptune 图谱 → 时序验证 → 置信度评分）+ Graph RAG 报告 + Incident 知识库闭环，是一个完整的 AIOps 故障管理方案。模块划分合理（每个文件 50-500 行，职责清晰），代码注释充分，容错处理到位（关键步骤都有 try/except + 降级逻辑）。

共享前的核心工作是**安全清理 + README + 设计文档打包**。代码本身质量不错，主要是配置外部化和重复代码消除。
