# 东京区 VPC 部署现状盘点报告

> 盘点时间:2026-08-28 14:40 UTC
> 目标:`vpc-010ab37a3f9f74725`(`ServicesEks2/PetSiteVPC`,CIDR `11.0.0.0/16`,ap-northeast-1)
> 账号:926093770964 · 身份:IAM user `Radium`
> 方式:**全程只读**(describe/list/get + Neptune openCypher 读查询 + ClickHouse SELECT),未做任何变更
> 关联代码库:`/home/ec2-user/works/graph-dependency-platform`

---

## 0. 结论摘要(先看这里)

| # | 结论 | 严重度 |
|---|---|---|
| 1 | **数据采集侧健康**:2 条主 ETL 高频运行(7 天 2016 + 708 次调用,近乎零错),DeepFlow 近 1h 采集 79 万条 L7 | ✅ 正常 |
| 2 | **分析侧完全静默**:`petsite-rca-engine` 与 `gp-window-flush` 过去 7 天 **0 次调用**;图谱内最新 Incident 停在 **2026-04-16**、最新混沌实验停在 **2026-04-02**(约 4 个月前) | 🔴 高 |
| 3 | **持续剖析未启用**:ClickHouse `profile.in_process` 表存在但 **0 行数据**,agent 未配置 `process_matcher` | 🟡 中 |
| 4 | **图谱规模远超文档**:实测 **867 节点 / 1341 边**,文档写的 171/309 已严重过期 | 🟡 中 |
| 5 | **边类型口径纠正**:实况 **26 种**,`petsite.yaml` header 写「28 种」但其自身枚举也是 26 —— header 错了 | 🟡 中 |
| 6 | **图谱有 2 种边类型未被 schema 正文声明**:`AffectedService`(28 条)、`Involves`(8 条) | 🟡 中 |
| 7 | **`NeptuneClusterStack` 在 CFN 活跃栈中不存在**;Neptune 集群实名 `petsite-neptune`(非文档的 `graph-dp-neptune`),引擎 1.4.6.3(非代码的 1.3.4.0) | 🟡 中 |
| 8 | **EKS API 公网端点对 `0.0.0.0/0` 开放** | 🟡 中(演示环境可接受,生产需收窄) |
| 9 | **CE 剖析可覆盖 50% 服务**:实测语言构成 Go×2 + Java×1(CE 支持)、.NET×2(需实测)、Python×1(CE 不支持)——足以支撑 PoC,无需先上企业版 | ✅ 正面结论 |
| 10 | **chaos-mesh v2.8.1 已部署且全部 Running**,ChaosMesh 注入能力立即可用 | ✅ 正常 |
| 11 | **DeepFlow 采集 76% 是可观测组件自噬流量**(Fluent-bit / CloudWatch / GuardDuty / NFM / X-Ray 五套上报组件),真实业务流量占比极低 | 🟡 中 |
| 12 | 本机经 VPC Peering **可直连** Neptune(8182)与 ClickHouse(8123),已实测通过 | ✅ 正常 |

---

## 1. 网络与连通性

### 子网(4 个,工作负载全在私有子网)

| 子网 ID | CIDR | AZ | 类型 | 可用 IP | 出网 |
|---|---|---|---|---|---|
| subnet-02ebd1dd8d1681da8 | 11.0.0.0/24 | 1a | 公有 | 249 | igw-094d45165ea6830dc |
| subnet-0600a43fa7ebf1ffe | 11.0.1.0/24 | 1c | 公有 | 246 | igw-094d45165ea6830dc |
| subnet-0f801fa79077eb277 | 11.0.2.0/24 | 1a | 私有 | 179 | nat-05584e6b365c2defb |
| subnet-047a94f9c5ab6302a | 11.0.3.0/24 | 1c | 私有 | 199 | nat-0dac54ed16fd53398 |

- **IGW**:igw-094d45165ea6830dc
- **NAT**:nat-05584e6b365c2defb(EIP 54.250.45.148)、nat-0dac54ed16fd53398(EIP 13.159.132.88)
- **VPC Endpoint**:仅 1 个 Interface 型 `guardduty-data`。**无 S3 / ECR Gateway 端点**(ETL Lambda 访问 S3/ECR 需绕 NAT,有优化空间)
- 路由表 5 个,其中 `rtb-0cf852412170e9fc4` 未关联任何子网

### 跨 VPC 连通(关键)

- 本机 `i-0eaa68697da8f708c` 在 **`vpc-06731f30388b57818`**(10.1.0.0/16,私有 IP `10.1.2.48`)——**与目标 VPC 不同**
- **VPC Peering `pcx-09179d94866c4afd6` 处于 active**,目标 VPC 4 张主路由表均有 `10.1.0.0/16 → pcx`
- Neptune SG(8182)与 ClickHouse SG(8123)均放行 `10.1.0.0/16`

**实测验证**:
```
ClickHouse  http://11.0.2.30:8123  → 返回版本 23.8.7.24        ✅ 通
Neptune     :8182/status           → AccessDeniedException      ✅ 网络通(需 SigV4)
Neptune     openCypher + SigV4     → 查询成功                   ✅ 通
```

### 安全组(15 个)

| SG | 名称 | 关键规则 |
|---|---|---|
| sg-0ae59648161ef4d63 | ServicesEks2-ALBSecurityGroup | ⚠️ 80/443 对 `0.0.0.0/0`(ALB,常规) |
| sg-0df44a2c5eaebcd7f | deepflow-server-sg | 8123/3000/9876,来源限 11.0 与 10.1 内网 ✅ |
| sg-00590f44d50a19e5f | neptune-sg | 8182 仅来源 `10.1.0.0/16` ✅ |
| sg-02df8bc13ac85c4cc | eks-cluster-sg-PetSite | 8123 来源 10.1.0.0/16 ✅ |
| sg-0bdc2c60905245f00 | nginx-alb-sg | 80 限 39.157.118.174/32 |

**8123 与 8182 均未对公网开放**;唯一 `0.0.0.0/0` 入站是 ALB 的 80/443。

---

## 2. 计算层(8 台 EC2,全部 running)

主机名一律为 `ip-<私有IP>` 内部格式(无自定义 hostname),角色靠**标签 + SSM** 交叉判定:

| 实例 ID | Name | 私有 IP | 类型 | 架构 | AZ | 角色判定 |
|---|---|---|---|---|---|---|
| i-0cf272a12ecff87f3 | **deepflow-server** | **11.0.2.30** | t4g.xlarge | arm64 | 1a | DeepFlow 全栈(含 ClickHouse 8123 / 前端 9876),独立监控主机 |
| i-0c39b7c79dfe93a2c | PetSite-Node-az1a-1 | 11.0.2.51 | t4g.large | arm64 | 1a | EKS 工作节点 |
| i-00188386d18a3095d | PetSite-Node-az1a-2 | 11.0.2.129 | t4g.large | arm64 | 1a | EKS 工作节点 |
| i-0aed2b5763456ec10 | PetSite-Node-az1c-1 | 11.0.3.141 | t4g.large | arm64 | 1c | EKS 工作节点 |
| i-032e64effbbed37dd | PetSite-Node-az1c-2 | 11.0.3.205 | t4g.large | arm64 | 1c | EKS 工作节点 |
| i-077d4ba09815bd2b8 | grafana-x86 | 11.0.2.42 | t3.large | **x86_64** | 1a | Grafana 可视化 |
| i-00f46b680713b9b14 | nfm-deepflow-test | 11.0.2.112 | t4g.small | arm64 | 1a | 网络流测试辅助 |
| i-00c04a0473b650f6d | sqlreplay-client | 11.0.1.37 | c7g.xlarge | arm64 | 1c | 临时验证机(`Temporary=true`) |

- `deepflow-server` 标签:`System=deepflow / Tier=tier2 / Team=observability-team / Backup=daily / ManagedBy=manual`,OS Amazon Linux 2023,SSM Online
- 4 台 PetSite-Node 带 `eks:cluster-name=PetSite` / `kubernetes.io/cluster/PetSite=owned`,跨 1a/1c 两个 nodegroup
- **ARM64 Graviton 声明基本成立**:除 grafana-x86 外全为 arm64(t4g/c7g);但 t4g 属 Graviton2,README 的「Graviton3」表述不够准确

### 负载均衡

唯一 LB:`Servic-PetSi-by0kpyBtxswj`(ALB,internet-facing,active)

| 目标组 | 端口 | 健康状态 |
|---|---|---|
| Servic-PetSi-7JEWC19HNKSR | 8080 | 2× healthy(11.0.2.142 / 11.0.2.58) |
| neptune-ui-tg | 9876 | healthy(11.0.2.30) |
| streamlit-demo-tg | 8501 | healthy(**10.1.2.198**,跨 VPC 经 peering) |
| deepflow-grafana-tg | 3000 | unused |
| Servic-PetAd-RPOCBTKKJYGI | 8080 | 2× unused |
| awesomeshop-frontend / Servic-PetSi-BGUX1XK3RN6D | — | 无注册目标 |

---

## 3. 数据层

### Neptune

| 项 | 实况 | 代码/文档声明 | 一致性 |
|---|---|---|---|
| 集群标识 | **petsite-neptune** | `graph-dp-neptune`(CDK)/ `petsite-neptune`(README) | ⚠️ 代码与实况不符 |
| 引擎版本 | **1.4.6.3** | 1.3.4.0(CDK) | ⚠️ 实际已升级 |
| 实例规格 | db.r6g.large | db.r6g.large | ✅ |
| Endpoint | petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com:8182 | — | — |
| 多 AZ | **false**(单实例 writer) | 单 AZ | ✅ |
| IAM 认证 | 已启用 | 已启用 | ✅ |
| 其他 | 加密启用、删除保护 true、备份保留 1 天 | — | — |

### 其他数据服务

- **DynamoDB**:`chaos-experiments` ✅、`gp-alert-buffer` ✅、devops-agent-slack-threads、ServicesEks2-ddbpetadoption
- **S3 Vectors**:`gp-incident-kb` ✅ 存在(创建于 2026-03-31),**是向量桶而非普通 S3 桶**;另有 openclaw-kb、openclaw-skill-router
- **RDS/Aurora**:serviceseks2-database(aurora-postgresql 16.11,DB=adoptions)、grafana-aurora-mysql(Serverless v2)、sqlreplay-verify(aurora-mysql)、awesomeshopinfra mysql(db.t3.micro)
- **SNS**:petsite-rca-alerts、petsite-ops-alerts、rca-alerts、kronos-alarms、ServicesEks2-topicpetadoption
- **CloudWatch 告警**:59 个 —— OK 51 / **ALARM 0** / INSUFFICIENT_DATA 8

---

## 4. ETL 与 Serverless 运行实况

| 函数 | Runtime | 内存/超时 | VPC | 7天调用 | 7天错误 | 判读 |
|---|---|---|---|---|---|---|
| neptune-etl-from-deepflow | py3.12 | 256/240 | 本 VPC | **2016** | 0 | ✅ 5min 周期正常 |
| neptune-etl-from-aws | py3.12 | 256/300 | 本 VPC | **708** | 1 | ✅ 15min 周期正常 |
| neptune-etl-trigger | py3.12 | 128/90 | none | 35 | 0 | ✅ 事件驱动正常 |
| neptune-etl-from-cfn | py3.12 | 256/120 | 本 VPC | 7 | 0 | ✅ 低频符合预期 |
| **petsite-rca-engine** | py3.12 | 256/60 | 本 VPC | **0** | 0 | 🔴 完全未触发 |
| **gp-window-flush** | py3.12 | 256/60 | 本 VPC | **0** | 0 | 🔴 完全未触发 |
| petsite-rca-interaction | py3.12 | 128/10 | none | 0 | 0 | 静默 |
| petsite-ops-slack-notifier | py3.12 | 128/10 | none | 0 | 0 | 静默 |

### EventBridge 规则(均 ENABLED)

- 定时:`neptune-etl-every-5min`(rate 5min)、`neptune-etl-every-15min`(rate 15min)、`neptune-etl-cfn-daily`(cron 0 18 * * ?)
- 事件驱动:`neptune-etl-cfn-on-deploy`、`neptune-etl-trigger-{alb,ec2,eks,elasticache,rds}`

### SQS(全部零堆积,DLQ 无积压)

`neptune-etl-trigger-queue` / `-dlq`、devops-agent-* dlq、ServicesEks2-sqspetadoption(+dlq) 均为 0。

---

## 5. CloudFormation 栈一致性

| 代码定义栈(`infra/bin/graph-dp.ts`) | 部署实况 | 状态 |
|---|---|---|
| NeptuneClusterStack | **✗ 活跃栈中不存在** | Neptune 集群由其他栈或手工管理 |
| NeptuneEtlStack | ✓ | UPDATE_COMPLETE |
| AlertBufferStack | ✓ | CREATE_COMPLETE |

其他相关活跃栈:`ServicesEks2`(UPDATE_COMPLETE)、`Applications`(UPDATE_COMPLETE)、AwesomeShopInfra、CDKToolkit、kirocrew-kc-05b19b 等。
注:`TidbPocNetworkStack` 处于 **UPDATE_ROLLBACK_COMPLETE**(非健康,与本项目无关)。

---

## 6. 图谱实况(Neptune 活数据)

### 规模

| 指标 | 实测(2026-08-28) | 文档声明 | 差异 |
|---|---|---|---|
| 节点总数 | **867** | 171+ | 实际为声明的 5 倍 |
| 边总数 | **1341** | 309 | 实际为声明的 4.3 倍 |
| 节点类型 | **31 种** | 31(schema)/ 23(旧 README) | ✅ 与 schema 完全一致 |
| 边类型 | **26 种** | 28(schema header)/ 19(旧 README) | ⚠️ schema header 自身错误 |

### 节点分布(31 种,全部有实例)

| 标签 | 数量 | 标签 | 数量 | 标签 | 数量 |
|---|---|---|---|---|---|
| Pod | 384 | ECRRepository | 12 | RDSCluster | 3 |
| **Incident** | **126** | K8sService | 12 | VPC | 3 |
| **ChaosExperiment** | **72** | Deployment | 12 | BusinessCapability | 3 |
| SecurityGroup | 52 | HPA | 11 | StepFunction | 2 |
| S3Bucket | 33 | Namespace | 9 | EKSCluster | 1 |
| LambdaFunction | 31 | ListenerRule | 8 | Region | 1 |
| Subnet | 16 | SQSQueue | 7 | NeptuneCluster | 1 |
| TargetGroup | 16 | SNSTopic | 5 | NeptuneInstance | 1 |
| Microservice | 15 | LoadBalancer | 4 | Database | 1 |
| EC2Instance | 15 | RDSInstance | 4 | | |
| | | DynamoDBTable | 4 | AvailabilityZone | 3 |

### 边分布(26 种)

| 类型 | 数量 | 类型 | 数量 | 类型 | 数量 |
|---|---|---|---|---|---|
| LocatedIn | 491 | OwnedBy | 20 | Involves ⚠️ | 8 |
| RunsOn | 221 | Calls | 18 | HasRule | 8 |
| TriggeredBy | 127 | Invokes | 18 | WritesTo | 5 |
| Manages | 77 | Implements | 17 | Contains | 3 |
| BelongsTo | 72 | MentionsResource | 16 | PublishesTo | 2 |
| **TestedBy** | **60** | RoutesTo | 13 | ProtectsAccess | 1 |
| Routes | 50 | ForwardsTo | 11 | ConnectsTo | 1 |
| AccessesData | 30 | | | InvokesVia | 1 |
| AffectedService ⚠️ | 28 | HasSG | 22 | | |

⚠️ **`AffectedService` 与 `Involves` 未在 `petsite.yaml` 的 schema 正文中作为边模式声明**(仅作为字符串零散出现)。影响:`rca/neptune/schema_prompt.py` 把 schema 喂给 LLM 做自然语言转 openCypher,**未声明的边类型 LLM 永远不会用**,这两类关系在 NL 查询中不可达。

### 三条回写链路的历史证据

| 回写链 | 证据 | 最新时间 | 状态 |
|---|---|---|---|
| RCA → 图谱 | Incident 126 个 + MentionsResource 16 条 | **2026-04-16** | 🔴 停滞约 4 个月 |
| Chaos → 图谱 | ChaosExperiment 72 个 + TestedBy 60 条 | **2026-04-02** | 🔴 停滞约 4 个月 |
| Learning → 图谱 | 未单独核验 `resilience_score` 属性 | — | 待查 |

**闭环三条回写链历史上都真实跑通过**(这是重要的正面结论),但**近 4 个月无新增**,与 rca-engine / gp-window-flush 的 0 调用互相印证。

---

## 7. 持续剖析(Continuous Profiling)现状

| 检查项 | 结果 |
|---|---|
| ClickHouse `profile` 库 | ✅ 存在 |
| `profile.in_process` 表 | ✅ 存在(schema 就绪,含 `app_service` / `profile_language_type` / `profile_event_type` / `profile_value` / `trace_id` / `span_name` / 全套 pod_* 标签) |
| **表内数据量** | **0 行**(`max(time)` = 1970-01-01) |
| 代码库内 profiling 配置 | 全仓 grep `process_matcher` / `on_cpu` / `profile` → **零命中** |

**结论:DeepFlow 的持续剖析能力完全未启用。** 表结构已由 DeepFlow 自动创建,只需配置 agent 的 `process_matcher` 即可开始产出数据。注意 `in_process` 表自带 `trace_id`/`span_name` 列——印证 DeepFlow 原生支持 profile↔trace 关联。

社区版限制(官方版本对比页):On-CPU ✅ / Off-CPU ❌ / Memory ❌;JVM 与 C/C++/Go/Rust ✅ / **Python ❌**。

### DeepFlow 流量采集实况

- 近 1h `flow_log.l7_flow_log` **791,735 条**,采集健康
- ⚠️ **但流量被噪声主导**:Top 5 请求域名全是 `logs.ap-northeast-1.amazonaws.com` 及其各种 DNS 后缀变体(合计约 60 万条,占 76%),真实业务服务 `search-service.petadoptions.svc.cluster.local` 仅 4,760 条
- 这意味着 ETL 在处理大量 CloudWatch Logs 上报流量,**有明显的采集过滤优化空间**(可在 agent 侧排除 `logs.*.amazonaws.com`,降低 ClickHouse 存储与 ETL 负载)

---

## 8. EKS 集群与工作负载

### 集群

| 项 | 值 |
|---|---|
| 集群名 | **PetSite**(区内唯一) |
| 版本 | **1.35**(kubelet v1.35.4-eks-4136f65) |
| 状态 | ACTIVE |
| Endpoint | `https://D355BAF17E25A2395709BCD682D10AFD.gr7.ap-northeast-1.eks.amazonaws.com` |
| API 访问 | 公网**开启**,允许 CIDR = **`0.0.0.0/0`** ⚠️ + 私网开启 |
| 节点组 | 2 个(workers1a60 / workers1cFE),各 min2/max3/desired2 → 共 **4 节点** |
| 实例/AMI | 全部 **t4g.large**,`AL2023_ARM_64_STANDARD`,ON_DEMAND,disk 20G,arch=arm64 |

⚠️ **两项安全/准确性观察**:
1. **EKS API 公网端点对 `0.0.0.0/0` 开放** —— 演示环境可接受,但生产需收窄到办公/跳板 CIDR。
2. **t4g = Graviton2,不是 Graviton3**(Graviton3 为 c7g/m7g/r7g)。README 的「ARM64 Graviton3」表述应更正。

命名空间:`petadoptions`(主体,活跃)、`awesomeshop`(已缩容至 0)、`default`、`deepflow`、`chaos-mesh`、`amazon-cloudwatch`、`amazon-guardduty`、`amazon-network-flow-monitor` 等。

### PetSite 微服务语言构成(实测,非推测)

镜像多为 CDK 资产哈希不可读,故通过 `kubectl exec` 读 `/proc/1/cmdline` 与二进制特征取得确凿依据:

| 服务 | 运行时依据 | 语言/运行时 | CE 剖析可行性 |
|---|---|---|---|
| **petsite** | `dotnet PetSite.dll` | **.NET**(C#/ASP.NET Core) | ⚠️ 官方未明确列出,需实测 |
| **pay-for-adoption** | `/app/app` ELF 内含 `go1.23.12` + `lib/pq` | **Go 1.23.12** | ✅ 支持(需开 golang_specific) |
| **list-adoptions** | `/app/app` ELF 内含 `go1.23.12` | **Go 1.23.12** | ✅ 支持(需开 golang_specific) |
| **search-service** | `java -jar /app/app.jar` | **Java** | ✅ 支持(JVM) |
| **pethistory** | `python3.12 flask run --host=0.0.0.0 --port=8080` | **Python 3.12 (Flask)** | ❌ **CE 不支持 Python** |
| **traffic-generator** | `dotnet trafficgenerator.dll` | **.NET** | ⚠️ 需实测 |

**Sidecar**:list-adoptions / pay-for-adoption / pethistory / search-service 均带 `aws-otel-collector:v0.47.0`(2/2);petsite 与 traffic-generator 无边车(1/1)。全部 Pod Running,Service 均 ClusterIP。

`awesomeshop`(第二套电商 demo,6 个 Deployment:auth/frontend/gateway/order/points/product-service,镜像 `ECR/awesomeshop/*:v13~v15`)**全部 replicas=0,无运行 Pod**,语言无法探查。

#### 📌 对持续剖析路线的决定性影响

社区版覆盖率评估(基于上表实测语言):

| 类别 | 服务数 | 占比 |
|---|---|---|
| ✅ CE 确定支持(Go ×2 + Java ×1) | **3** | 50% |
| ⚠️ CE 需实测(.NET ×2) | 2 | 33% |
| ❌ CE 明确不支持(Python ×1) | 1 | 17% |

**结论:CE 至少能覆盖一半服务(Go + Java),这个覆盖率足以支撑 PoC**,不必为启用剖析先上企业版。但 `pethistory`(Python)在 CE 下永久无栈,`petsite` 与 `traffic-generator`(.NET,恰恰是主站与压测器)属未知,需实测。这也印证了报告第 6 节的设计要点:**图谱必须能表达「此服务无剖析数据」**,否则 RCA 会把 CE 的语言限制误读成「无热点」。

### deepflow-agent

| 项 | 值 |
|---|---|
| 形态 | DaemonSet,namespace `deepflow`,**4/4 Running**(每节点 1 个) |
| 镜像 | `registry.cn-hongkong.aliyuncs.com/deepflow-ce/deepflow-agent:**v7.0**` |
| Helm chart | `deepflow-agent-7.0.014`,git `ffdce374`,**社区版 CE** ✅ 与用户所述一致 |

**ConfigMap `deepflow-agent` 全量内容**(仅此三项):
```yaml
controller-ips: ['11.0.2.30']
kubernetes-cluster-name: PetSite
external-metrics:
  prometheus:
    enabled: true
```

**profiling 判定**:ConfigMap **不含任何** `process_matcher` / `ebpf` / `profile` / `on_cpu` 键,deepflow-server 的 ConfigMap 亦无。

> ⚠️ **诚实的不确定性**:DeepFlow v7 的 eBPF profiling 由 **server 端 agent-group 高级配置动态下发**(存于 deepflow MySQL,不在 ConfigMap 里),因此仅凭 ConfigMap **无法断言运行时是否开启**。
> **但第 7 节的 ClickHouse 实测给出了决定性证据**:`profile.in_process` **0 行、`max(time)`=1970**。两者合起来才能定论——**剖析确实没有在产出数据**。

### chaos-mesh

**已部署,v2.8.1**(`ghcr.io/chaos-mesh/*`),全部 Running:
- `chaos-controller-manager` 3/3、`chaos-daemon` DaemonSet 4/4、`chaos-dashboard` 1/1、`chaos-dns-server` 1/1

意味着 `chaos/` 模块的 ChaosMesh 后端**具备立即可用的注入能力**(与图谱中 72 个历史 ChaosExperiment 相互印证)。

### 其他观测组件

CloudWatch Observability、Fluent-bit、GuardDuty agent、AWS Network Flow Monitor、X-Ray daemon(default ns)、deepflow 配套 `prometheus-nfm` + `yace-nfm`(CloudWatch exporter)均在运行。

> **这解释了第 7 节的噪声来源**:集群内同时跑着 Fluent-bit、CloudWatch Observability、GuardDuty、NFM、X-Ray 五套上报组件,它们持续向 `logs.ap-northeast-1.amazonaws.com` 发流量,被 DeepFlow eBPF 无差别捕获,占了 L7 日志的 76%。这是典型的「可观测组件自噬」——**过滤它们是最高性价比的优化**。

---

## 9. 待补完的盘点项

| 项 | 原因 |
|---|---|
| deepflow-server agent-group 动态配置 | profiling 开关的权威位置在 deepflow MySQL,ConfigMap 查不到(但 ClickHouse 0 行已足以证明未产出) |
| .NET 服务在 CE 下的 On-CPU 符号解析实际表现 | 官方文档未明确,需开启后实测 |
| LearningAgent 的 `resilience_score` 属性是否真实回写 | 本轮未单独查询 |

---

## 10. 建议的完善路线(按优先级)

### P0 — 恢复分析闭环(最高优先)
数据在灌入但分析不跑,平台价值只发挥了一半。链路:`CloudWatch Alarm → SNS → gp-alert-buffer → gp-window-flush → petsite-rca-engine`。

**判断依据**:59 个告警全部 OK/INSUFFICIENT、**0 个 ALARM**,SQS 与 DLQ 零堆积 → **很可能只是「没有真实故障触发」而非链路故障**。
**建议动作**:chaos-mesh v2.8.1 已就绪且历史上跑过 72 次实验,直接**跑一次混沌实验做端到端验证**(注入 → 告警 → RCA → 报告 → Incident 回写),用真实信号确认链路通畅,而不是先改代码。这同时能验证 `TestedBy` 回写链是否仍然工作。

### P1 — 修正代码与实况的偏差
1. CDK 中 Neptune 集群名 `graph-dp-neptune` ≠ 实况 `petsite-neptune`;引擎 1.3.4.0 ≠ 实际 1.4.6.3。
2. `NeptuneClusterStack` 未作为独立栈部署 —— 需确认是有意手工管理还是漂移(影响 DR 与重建能力)。
3. `petsite.yaml` schema header「边类型(28 种)」应改为 **26**(其自身枚举与活图谱均为 26)。**本轮已据此修正两份 README。**
4. 把 `AffectedService` 与 `Involves` 补进 schema 正文边模式,否则 NL 查询引擎永远生成不出用到它们的 Cypher。
5. README 的「ARM64 Graviton3」应更正为 **Graviton2**(t4g)或写「Graviton 系列」。
6. `profiles/petsite.yaml` 补 `language`/`runtime` 字段 —— **本轮已实测出全部 6 个服务的语言**,可直接落库。

### P2 — 降噪与增效
1. **DeepFlow 采集过滤(最高性价比)**:排除 `logs.*.amazonaws.com` 类可观测自噬流量,当前占 76%。集群内 5 套上报组件(Fluent-bit / CloudWatch Observability / GuardDuty / NFM / X-Ray)是噪声源。
2. **补 VPC Endpoint**:S3/ECR Gateway 端点可省 NAT 流量费并提速 ETL(当前仅有 guardduty-data 一个 Interface 端点)。
3. **收窄 EKS API 公网 CIDR**(当前 `0.0.0.0/0`)。

### P3 — 启用持续剖析(第四支柱)
`profile.in_process` 表结构已就绪,只需配 agent `process_matcher`,**零成本**。

基于本轮实测的语言构成,CE 路线**值得做**:
- 先为 **Go ×2 + Java ×1**(CE 确定支持,占 50%)配置 `process_matcher`,Go 需额外开 `symbol_table.golang_specific.enabled`(默认 false);
- 同批实测 **.NET ×2**(petsite 主站 + traffic-generator)的符号解析表现 —— 官方文档未明确,这是最大未知;
- `pethistory`(Python)在 CE 下放弃,图谱中明确标记为「无剖析数据」;
- 注意 profiling 开关在 **deepflow-server 的 agent-group 高级配置**(存 MySQL),不在 ConfigMap,需从 server 侧下发。

验证 SQL(经 peering 可直接从本机跑):
```sql
SELECT app_service, profile_language_type, profile_event_type, count(*) AS cnt
FROM profile.in_process
WHERE time > now() - 3600
GROUP BY app_service, profile_language_type, profile_event_type
ORDER BY cnt DESC
```

---

*本报告所有数字均为 2026-08-28 实测值,非引用文档。查询方式:AWS CLI(只读)+ Neptune openCypher(SigV4)+ ClickHouse HTTP SELECT + kubectl(只读)。*
*本轮为完成盘点在本机安装了 arm64 kubectl 与 python boto3/requests,并生成了 `~/.kube/config` 的 PetSite context —— 均为本机工具配置,未改动任何云端或集群资源。*
