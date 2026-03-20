# Chaos 实验模板说明文档

> 路径：`/home/ubuntu/tech/chaos/code/`  
> 最后更新：2026-03-13

---

## 概览

本平台使用**双工具并行策略**进行混沌实验：

- **Chaos Mesh**（via Chaosmesh-MCP）：K8s Pod/容器/网络/应用层故障（24 种已验证）
- **AWS FIS**：AWS 托管服务层 + 基础设施层故障（Lambda/RDS/EC2/EBS/网络）

`gen_template.py` 是 Chaos Mesh 实验的交互式模板生成器，连接 Neptune 图谱自动读取每个微服务的 **Tier 等级**和**调用关系**，智能填充稳态阈值、Stop Conditions、RCA 配置。FIS 实验模板当前手动编写，存放在 `experiments/fis/` 目录下；未来可通过 `aws-api-mcp-server` + LLM Agent 辅助生成（见下方"MCP 辅助 FIS 模板生成"章节）。

**生成与执行分离原则**：模板生成可以通过 MCP 工具进行 API 级验证，但执行阶段始终走确定性路径（Runner + boto3/Chaosmesh-MCP），不依赖 LLM 可用性。

---

## 目录结构

```
/home/ubuntu/tech/chaos/code/
├── gen_template.py          # Chaos Mesh 模板生成器
├── main.py                  # 实验运行器（双后端：Chaos Mesh + FIS）
├── experiments/             # 所有实验模板
│   ├── tier0/               # Tier0 服务 Chaos Mesh 实验
│   ├── tier1/               # Tier1 服务 Chaos Mesh 实验
│   ├── tier2/               # Tier2 服务 Chaos Mesh 实验
│   ├── network/             # 按故障类型组织（备用）
│   └── fis/                 # AWS FIS 实验（AWS 托管服务 + 基础设施层）
│       ├── lambda/          # Lambda 故障（petstatusupdater / petadoptionshistory）
│       ├── rds/             # Aurora MySQL failover / reboot
│       ├── eks-node/        # EKS 节点终止
│       └── network-infra/   # AZ 网络中断 / EBS IO 延迟
├── fmea/                    # FMEA 故障模式分析
├── runner/                  # 运行时工具
│   ├── fault_injector.py    # 故障注入抽象层
│   ├── chaosmesh_backend.py # Chaos Mesh 后端
│   ├── fis_backend.py       # AWS FIS 后端
│   └── ...
└── infra/                   # 基础设施配置
    ├── dynamodb_setup.py    # DynamoDB 建表
    └── fis_setup.py         # FIS IAM Role + CloudWatch Alarms
```

---

## 快速开始

### Chaos Mesh 实验（K8s 层）

```bash
cd /home/ubuntu/tech/chaos/code

# 1. 完全交互式（推荐首次使用）
python3 gen_template.py

# 2. 指定服务 + 故障类型（跳过前两步选择）
python3 gen_template.py --service petsearch --fault network_delay

# 3. 查看所有服务 + Tier + 上下游关系
python3 gen_template.py --list-services
```

### FIS 实验（AWS 托管服务层）

FIS 实验模板存放在 `experiments/fis/` 目录下，手动编写（因为目标资源是 AWS 托管服务，不在 Neptune 图谱中）。

```bash
# 查看已有 FIS 模板
ls experiments/fis/*/

# 运行 FIS 实验（dry-run 先验证）
python3 main.py run --file experiments/fis/lambda/fis-lambda-delay-petstatusupdater.yaml --dry-run

# 正式运行 FIS 实验
python3 main.py run --file experiments/fis/lambda/fis-lambda-delay-petstatusupdater.yaml
```

### 通用运行命令

生成模板后，运行实验：

```bash
# 先 dry-run 验证配置
python3 main.py run --file experiments/tier0/petsearch-network-delay.yaml --dry-run

# 正式运行
python3 main.py run --file experiments/tier0/petsearch-network-delay.yaml
```

---

## Chaos Mesh 模板字段说明

> 以下适用于 `backend: chaosmesh`（默认）的实验模板。

```yaml
name: petsearch-network-delay-tier0-20260312
description: "..."
backend: chaosmesh          # 可选，默认 chaosmesh

# ─── 目标服务 ───────────────────────────────────────────────────────
target:
  service: petsearch       # 注入故障的服务名
  namespace: default       # K8s namespace
  tier: Tier0              # 来自 Neptune recovery_priority 属性

# ─── 故障配置 ───────────────────────────────────────────────────────
fault:
  type: network_delay      # 故障类型（见下方支持列表）
  mode: fixed-percent      # 注入模式：one/all/fixed-percent/random-max-percent
  value: "50"              # 百分比（fixed-percent 模式）
  duration: "3m"           # 故障持续时间
  latency: "200ms"         # 类型专属参数
  jitter: "10ms"

# ─── 稳态假设 ───────────────────────────────────────────────────────
steady_state:
  before:                  # 注入前必须满足（否则实验不启动）
    - metric: success_rate
      threshold: ">= 95%"  # Tier0=95%, Tier1=90%, Tier2=80%
      window: "1m"
  after:                   # 实验结束后恢复验证
    - metric: success_rate
      threshold: ">= 95%"
      window: "5m"
    - metric: latency_p99
      threshold: "< 5000ms" # Tier0=5s, Tier1=8s, Tier2=15s
      window: "5m"

# ─── 紧急停止条件 ────────────────────────────────────────────────────
stop_conditions:
  - metric: success_rate
    threshold: "< 66%"     # = before_sr × (1 - fault_tolerance)
    window: "30s"
    action: abort
  - metric: latency_p99
    threshold: "> 8000ms"  # Tier0=8s, Tier1=15s, Tier2=30s
    window: "30s"
    action: abort

# ─── RCA 自动触发 ────────────────────────────────────────────────────
rca:
  enabled: true            # Tier0/Tier1 自动开启
  trigger_after: "30s"
  expected_root_cause: petsearch  # 预期根因

# ─── Neptune 图谱反馈 ────────────────────────────────────────────────
graph_feedback:
  enabled: true
  edges:
    - Calls
```

---

## Tier 阈值规则

| 等级 | 注入前 SR | 恢复后 SR | 恢复后 p99 | Stop SR | Stop p99 | RCA |
|------|-----------|-----------|------------|---------|----------|-----|
| Tier0 | ≥ 95% | ≥ 95% | < 5000ms | < 66% | > 8000ms | ✅ |
| Tier1 | ≥ 90% | ≥ 90% | < 8000ms | < 63% | > 15000ms | ✅ |
| Tier2 | ≥ 80% | ≥ 80% | < 15000ms | < 64% | > 30000ms | ❌ |

> Stop Condition 成功率 = `before_sr × (1 - fault_tolerance)`，确保故障期间不会误触发。

---

## 支持的故障类型

### Pod 故障
| 类型 | 说明 | 默认时长 |
|------|------|----------|
| `pod_kill` | 随机杀 Pod | 2m |
| `pod_failure` | 使 Pod 持续失败（无法重启） | 3m |
| `container_kill` | 杀指定容器 | 2m |

### 网络故障
| 类型 | 说明 | 专属参数 |
|------|------|----------|
| `network_delay` | 注入延迟 | `latency`, `jitter` |
| `network_loss` | 丢包 | `loss`（%） |
| `network_corrupt` | 包损坏 | `corrupt`（%） |
| `network_duplicate` | 包重复 | `duplicate`（%） |
| `network_bandwidth` | 限速 | `rate`（如 1mbps） |
| `network_partition` | 网络分区 | `direction`, `external_targets` |

### 应用/协议故障
| 类型 | 说明 | 专属参数 |
|------|------|----------|
| `http_chaos` | HTTP 响应延迟/错误/替换 | `action`, `port`, `delay` |
| `dns_chaos` | DNS 解析错误/随机 | `action`, `patterns` |
| `io_chaos` | 磁盘 IO 延迟/错误 | `action`, `volume_path`, `delay` |
| `time_chaos` | 时钟偏移 | `time_offset`（如 -5m） |

### 资源压力
| 类型 | 说明 | 专属参数 |
|------|------|----------|
| `pod_cpu_stress` | CPU 压力 | `workers`, `load`（%） |
| `pod_memory_stress` | 内存压力 | `size`（如 256MB） |

### 内核故障
| 类型 | 说明 | 风险 |
|------|------|------|
| `kernel_chaos` | 内核级故障注入 | ⚠️ 高危，仅建议 Tier2 |

---

## 已有模板索引

| 文件 | 服务 | 故障类型 | Tier |
|------|------|----------|------|
| `experiments/tier0/petsearch-network-delay.yaml` | petsearch | network_delay | Tier0 |
| `experiments/tier0/petsite-pod-kill.yaml` | petsite | pod_kill | Tier0 |
| `experiments/tier0/payforadoption-http-chaos.yaml` | payforadoption | http_chaos | Tier0 |
| `experiments/tier1/pethistory-network-delay.yaml` | pethistory | network_delay | Tier1 |
| `experiments/tier1/petlistadoptions-network-loss.yaml` | petlistadoptions | network_loss | Tier1 |
| `experiments/tier1/petstatusupdater-pod-cpu-stress.yaml` | petstatusupdater | pod_cpu_stress | Tier1 |

---

## FIS 模板字段说明

> 以下适用于 `backend: fis` 的实验模板。FIS 实验目标是 AWS 托管服务（Lambda/RDS/EC2 等），不受 Chaos Mesh 管辖。

```yaml
name: fis-lambda-delay-petstatusupdater
description: "Inject 3s delay into petstatusupdater Lambda invocations"
backend: fis                 # 指定使用 FIS 后端

# ─── 目标资源 ───────────────────────────────────────────────────────
target:
  service: petstatusupdater  # 目标服务名（用于报告和 DynamoDB 记录）
  namespace: lambda          # 标识为 Lambda 函数（不是 K8s namespace）
  tier: Tier1

# ─── 故障配置 ───────────────────────────────────────────────────────
fault:
  type: fis_lambda_delay     # FIS 故障类型（fis_ 前缀）
  duration: "5m"
  extra_params:              # FIS 专属参数
    function_arn: "arn:aws:lambda:ap-northeast-1:926093770964:function:petstatusupdater"
    delay_ms: 3000           # 延迟毫秒数
    percentage: 100          # 注入比例

# ─── 稳态假设 ───────────────────────────────────────────────────────
steady_state:
  before:
    - metric: success_rate
      threshold: ">= 90%"
      window: "1m"
  after:
    - metric: success_rate
      threshold: ">= 90%"
      window: "5m"

# ─── 紧急停止条件 ────────────────────────────────────────────────────
stop_conditions:
  - metric: success_rate
    threshold: "< 50%"
    window: "30s"
    action: abort
    cloudwatch_alarm_arn: "arn:aws:cloudwatch:ap-northeast-1:926093770964:alarm:chaos-petstatusupdater-sr-critical"
    # ↑ FIS 原生 Stop Condition：CloudWatch Alarm ARN
    # Runner 自研 Stop Condition + FIS 原生 Stop Condition = 双重保险

# ─── RCA 自动触发 ────────────────────────────────────────────────────
rca:
  enabled: true
  trigger_after: "30s"
  expected_root_cause: petstatusupdater

# ─── Neptune 图谱反馈 ────────────────────────────────────────────────
graph_feedback:
  enabled: true
  edges:
    - DependsOn              # Lambda → SQS/DynamoDB 依赖验证（非 Calls 边）
```

### FIS 故障类型速查

| fault.type | FIS Action | 目标资源类型 | 必填 extra_params |
|-----------|-----------|-------------|------------------|
| `fis_lambda_delay` | `aws:lambda:invocation-add-delay` | Lambda | `function_arn`, `delay_ms`, `percentage` |
| `fis_lambda_error` | `aws:lambda:invocation-error` | Lambda | `function_arn`, `percentage` |
| `fis_rds_failover` | `aws:rds:failover-db-cluster` | Aurora/RDS | `cluster_arn` |
| `fis_rds_reboot` | `aws:rds:reboot-db-instances` | Aurora/RDS | `instance_arn` |
| `fis_eks_terminate_node` | `aws:eks:terminate-nodegroup-instances` | EKS Nodegroup | `nodegroup_arn` |
| `fis_ec2_stop` | `aws:ec2:stop-instances` | EC2 | `tags` or `instance_ids` |
| `fis_ebs_io_latency` | `aws:ebs:volume-io-latency` | EBS | `volume_ids`, `latency_ms` |
| `fis_network_disrupt` | `aws:network:disrupt-connectivity` | Subnet/VPC | `subnet_arn` |
| `fis_api_throttle` | `aws:fis:inject-api-throttle-error` | IAM Role | `role_arn`, `service`, `operations` |
| `fis_api_unavailable` | `aws:fis:inject-api-unavailable-error` | IAM Role | `role_arn`, `service`, `operations` |

### FIS 前置要求

1. **IAM Role**：`chaos-fis-experiment-role` 已创建（`python3 main.py setup --fis`）
2. **CloudWatch Alarms**：FIS Stop Conditions 依赖的告警已创建
3. **Lambda Extension**：目标 Lambda 函数已添加 FIS Extension Layer（仅 `aws:lambda:*` 需要）
4. **S3 Bucket**：FIS ↔ Lambda Extension 通信用的 S3 bucket 已创建

### FIS 已有模板索引

| 文件 | 目标 | FIS Action | 场景 |
|------|------|------------|------|
| `experiments/fis/lambda/fis-lambda-delay-petstatusupdater.yaml` | petstatusupdater | invocation-add-delay | Lambda 延迟注入 |
| `experiments/fis/lambda/fis-lambda-error-petadoptionshistory.yaml` | petadoptionshistory | invocation-error | Lambda 错误注入 |
| `experiments/fis/rds/fis-aurora-failover.yaml` | Aurora MySQL | failover-db-cluster | 数据库 failover |
| `experiments/fis/rds/fis-aurora-reboot.yaml` | Aurora MySQL | reboot-db-instances | 数据库重启 |
| `experiments/fis/eks-node/fis-eks-terminate-node-1a.yaml` | EKS nodegroup | terminate-nodegroup-instances | AZ 1a 节点终止 |
| `experiments/fis/eks-node/fis-eks-terminate-node-1c.yaml` | EKS nodegroup | terminate-nodegroup-instances | AZ 1c 节点终止 |
| `experiments/fis/network-infra/fis-network-disrupt-az-1a.yaml` | EKS subnet | disrupt-connectivity | AZ 网络中断 |
| `experiments/fis/network-infra/fis-ebs-io-latency.yaml` | EBS volumes | volume-io-latency | EBS IO 延迟 |

---

## MCP 辅助 FIS 模板生成（进阶）

> 详见 TDD 3.10 + ADR-003。

当前 FIS 模板需要手动编写，容易出错（ARN 格式、Action 参数、目标资源不存在等）。引入 `aws-api-mcp-server` 后，LLM Agent 可在模板生成阶段通过 MCP 工具进行 API 级验证。

### 原理

```
SRE: "给 petstatusupdater Lambda 生成一个 3s 延迟注入实验"
        ↓
LLM Agent
  ├── 调用 aws-api-mcp-server 查询 Lambda 函数列表 → 获取 function ARN
  ├── 调用 aws-api-mcp-server 验证 FIS Action 参数 → 确认 invocation-add-delay 可用
  ├── 从 Neptune/DeepFlow 获取 Tier + 调用关系 → 推算阈值
  └── 生成 YAML 模板（参数经过 API 验证）
        ↓
程序化校验（Schema + 安全规则）
        ↓
保存到 experiments/fis/lambda/
```

### 与执行阶段的关系

- **生成阶段**：LLM + MCP 工具 → 确保模板准确
- **执行阶段**：Runner + boto3 直调 → 确保执行可靠
- **紧急熔断**：boto3 `stop_experiment()` + CloudWatch Alarm → 不经过 LLM/MCP

**MCP 是增强，不是唯一路径**。如果 LLM/MCP 不可用，仍可手动编写 FIS 模板。

---

## 服务依赖图（来自 Neptune）

```
trafficgenerator [Tier2]
  └─→ petsite [Tier0]
        ├─→ petsearch [Tier0]
        ├─→ payforadoption [Tier0]
        ├─→ pethistory [Tier1]
        │     └─→ petlistadoptions [Tier1]
        │               └─→ petsearch [Tier0]
        └─→ petlistadoptions [Tier1]
```

---

## 注意事项

### 通用
1. **先 dry-run**：正式运行前务必 `--dry-run`，确认 Steady State 满足
2. **Stop Conditions 是护栏**：触发后实验自动 abort，不要随意调高阈值
3. **Tier0 服务谨慎操作**：petsite/petsearch/payforadoption 是核心链路
4. **报告含 LLM 分析**：实验完成后报告末尾会有 Bedrock Claude 生成的分析结论（弹性判断 + 改进建议），Bedrock 不可用时自动跳过

### Chaos Mesh 实验
4. **RCA 联动**：模板开启 `rca.enabled` 时，故障注入 30s 后会自动触发 RCA 分析
5. **Neptune 图谱反馈**：实验结果会更新 Neptune 图谱中的 `Calls` 边权重

### FIS 实验
6. **CloudWatch Alarm Stop Conditions**：FIS 实验有双重保险（Runner 自研 + FIS 原生 CloudWatch Alarm）
7. **Lambda Extension 前置**：`fis_lambda_*` 类型实验需要目标 Lambda 已安装 FIS Extension Layer
8. **Aurora Failover 影响范围**：`fis_aurora_failover` 会影响所有使用该 Aurora 集群的服务，不仅是 petlistadoptions
9. **AZ 网络中断高风险**：`fis_network_disrupt` 影响整个 subnet，可能连带影响非目标服务，需特别小心
10. **FIS 实验会产生 CloudTrail 日志**：所有 FIS 操作都有审计记录，适合合规要求
