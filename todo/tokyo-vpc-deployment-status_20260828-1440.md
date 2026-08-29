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


---

# 附录 A · DeepFlow 与可观测性栈补充盘点

**补充日期**：2026-08-29 05:30 UTC
**触发**：为四支柱 Traces 缺口接入做准备，需要读到 DeepFlow 实际生效的采集配置。
**方法**：本轮首次用 **SSM SendCommand** 进入 deepflow-server 实例（全部只读命令），
此前只从外部访问过 ClickHouse 8123。

## A.1 DeepFlow 相关 EC2（3 台，全部 running）

| Name | Instance ID | 类型 | 私有 IP | AZ | SG | 用途 |
|---|---|---|---|---|---|---|
| **deepflow-server** | `i-0cf272a12ecff87f3` | t4g.xlarge (arm64) | **11.0.2.30** | ap-northeast-1a | `sg-0df44a2c5eaebcd7f` | server + ClickHouse + MySQL + app |
| nfm-deepflow-test | `i-00f46b680713b9b14` | t4g.small | 11.0.2.112 | ap-northeast-1a | `sg-0395b3086a6bb201b` | 标签 `Phase: exploration` |
| grafana-x86 | `i-077d4ba09815bd2b8` | t3.large | 11.0.2.42 | ap-northeast-1a | `sg-0df44a2c5eaebcd7f` | 展示层 |

三台同在 `vpc-010ab37a3f9f74725` / `subnet-0f801fa79077eb277`，
启动时间均为 2026-05-26T06:53:55Z，标签 `System=deepflow` / `Team=observability-team` / `ManagedBy=manual`。

## A.2 访问路径（重要，此前未记录）

**三台都挂 `AmazonSSMRoleForInstancesQuickSetup` 实例配置文件，SSM 状态 Online。**
这是读取 DeepFlow 内部状态的唯一可行路径 —— 安全组只对外暴露 ClickHouse 8123，
server API 端口从本机不可达（实测 `curl http://11.0.2.30:30417` → HTTP 000）。

Amazon Linux 2023，SSM agent 3.3.4108.0。

## A.3 部署形态：docker-compose，4 个容器

| 容器 | 镜像 | 状态 |
|---|---|---|
| `deepflow-server` | `deepflow-ce/deepflow-server:v7.0` | Up 3 months |
| `deepflow-clickhouse` | `deepflow-ce/clickhouse-server:23.8.7.24` | Up 3 months |
| `deepflow-mysql` | `deepflow-ce/mysql:8.0.31` | Up 3 months |
| `deepflow-app` | `deepflow-ce/deepflow-app:v7.0` | Up 3 months |

镜像源为 `registry.cn-hongkong.aliyuncs.com`。
**注：`chaos/docs/prd.md` 记的 ClickHouse 23.10 是错的，实测 23.8.7.24。**

## A.4 端口映射（容器 → 宿主机）

| 服务 | 容器端口 | 宿主机端口 | 备注 |
|---|---|---|---|
| controller HTTP API | 20417 | **30417** | `/v1/vtap-group-configuration/` 等 REST 入口 |
| querier | 20416 | 20416 | 直通，Go 404 页面可探活 |
| agent gRPC | 20035 | 30035 | agent ↔ server |
| — | 20033 | 30033 | |
| — | 20080 | 20080 | 直通 |
| deepflow-app | 20418 | 20418 | |
| ClickHouse HTTP | 8123 | **8123** | **唯一对外可达的端口** |
| MySQL | **30130** | 未映射 | 见 server.yaml，不是默认 3306 |

**踩过的坑**：宿主机上探 `127.0.0.1:20417` 会得到 HTTP 000 ——
controller 在宿主机侧是 **30417**。容器内才是 20417。

## A.5 两个访问陷阱

1. **`deepflow-ctl` 不在镜像里。** 宿主机 `/usr/local/bin/` 只有 docker-compose 软链；
   `deepflow-server` 容器内只有一个 274 MB 的 `/bin/deepflow-server` 单体二进制，
   `docker exec deepflow-server deepflow-ctl` 会报 `executable file not found in $PATH`。
   要读配置只能走 REST API 或直查 MySQL。

2. **MySQL root 必须走 TCP，不能走 socket。**
   `docker exec deepflow-mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD"` →
   `ERROR 1045 Access denied for user 'root'@'localhost'`；
   加上 `-h mysql -P 30130` 强制 TCP 后即成功（授权是 `root@'%'`，不含 `root@'localhost'`）。
   密码必须**在容器内**用 `$MYSQL_ROOT_PASSWORD` 取用，跨 shell 传会失败。

## A.6 采集配置现状：**从未配置过**

```
GET http://127.0.0.1:30417/v1/vtap-group-configuration/
→ HTTP 200  {"OPT_STATUS":"SUCCESS","DESCRIPTION":"","DATA":[]}

mysql> SELECT http_log_trace_id, http_log_span_id, http_log_x_request_id
       FROM deepflow.vtap_group_configuration;
→ 零行
```

`vtap_group_configuration` 表**零行**，说明**没有任何 agent-group 配置被创建过**，
全部 agent 跑在内置默认值上。而控制 trace 上下文提取的三个列确实存在：
`http_log_trace_id` / `http_log_span_id` / `http_log_x_request_id`。

**这解释了为什么 `l7_flow_log.trace_id` 是 0 / 789,055**：
DeepFlow 默认只提取 `traceparent`（W3C）与 `sw8`（SkyWalking），
而本环境的应用发的是 `X-Amzn-Trace-Id`（AWS X-Ray 传播器，
aws-otel-collector 与 X-Ray SDK 的默认）。**头对不上，不是没埋点。**

## A.7 EKS 侧追踪现状（补充 §4 的 workload 部分）

**X-Ray 有两套接入并存：**

| 方式 | 服务 |
|---|---|
| `xray-daemon` DaemonSet 4/4（`default` ns）+ headless Service `xray-service` UDP 2000 | `petsite`(.NET)、`pethistory` 通过 `AWS_XRAY_DAEMON_ADDRESS` |
| `aws-otel-collector:v0.47.0` **sidecar** | `search-service`、`pay-for-adoption`、`list-adoptions`、`pethistory` |

**已实测的语言/运行时**（补全此前「6 个服务待声明 language/runtime」的遗留项）：

| Deployment | 图谱服务名 | 运行时 | 副本 | collector sidecar |
|---|---|---|---|---|
| `search-service` | petsearch | **Java** (`java -jar app.jar`) | 2/2 | 是 |
| `pay-for-adoption` | payforadoption | **Go** 1.23.12 | 2/2 | 是 |
| `list-adoptions` | petlistadoptions | **Go** 1.23.12 | 2/2 | 是 |
| `pethistory-deployment` | pethistory | **Python 3.12** (Flask) | 2/2 | 是 |
| `petsite-deployment` | petsite | **.NET** (`dotnet PetSite.dll`) | 2/2 | 否（走 xray-daemon） |
| `traffic-generator` | trafficgenerator | 压测器 | 1/1 | 否 |

**自动埋点能力现状**：
- CRD `instrumentations.cloudwatch.aws.amazon.com`（v1alpha1）**存在**，
  operator `amazon-cloudwatch-observability-controller-manager` **1/1 Running**
- 但 **`Instrumentation` CR 一个都没有**，从未启用
- `adot` addon **未安装**
- CRD 的 `spec` 暴露语言字段：`java, python, dotnet, nodejs, go, apacheHttpd, nginx`，
  但 **Go 的自动埋点是 eBPF 方案、需特权 sidecar**，成熟度明显弱于字节码语言
- 该 operator 的目的地是 **CloudWatch Application Signals**，
  与现有 sidecar→X-Ray 是**两套模型**

## A.8 一个待解决的断裂

**`petsite` 是入口服务（图谱里唯一 active 的 `Calls` 边是 `petsite → petsearch`），
配了 `AWS_XRAY_DAEMON_ADDRESS`，但在 X-Ray 服务图里完全不出现。**

X-Ray 把 `PetSearch` 当入口看（只有一个 `client` 影子节点指向它），
说明**没有来自 petsite 的上游 span**。追踪链在入口就断了 ——
这比 `trace_id` 为 0 更根本，且不是改 DeepFlow 配置能解决的。

## A.9 X-Ray 数据面（24h 实测，需分 4 段查：单次请求上限 6 小时）

只有 3 个被插桩的应用服务 + 3 个托管服务节点，共 **3 条真实边**：

| 源 | 目标 | 目标类型 | 24h OkCount | TotalResponseTime |
|---|---|---|---|---|
| PetSearch | S3 | `AWS::S3` | 2,950 | 1,347.54s |
| PetSearch | STS | `AWS::STS` | 77 | 4.06s |
| PetSearch | `ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM` | `AWS::DynamoDB::Table` | 11,512 | 61.8s |

PetSearch 本体 63,352 次 / 1,561.2s；payforadoption 51,840 次 / 7.0s；
petlistadoptions 51,837 次 / 6.93s。**24h 全窗口零 Error、零 Fault。**
payforadoption 与 petlistadoptions 解析不出任何跨服务下游边。

**名字映射规则：`lower(X-Ray名)` 即图谱名**，无逐名特例
（`PetSearch`→`petsearch`；`payforadoption`/`petlistadoptions` 本就一致）。

**两个接入障碍**：
- X-Ray 的 S3 节点名就叫 `S3`，**不是 bucket 名** → 写不出精确的 S3 边
- 图谱**无 STS 节点类型** → 接入即造孤立节点

## A.10 对四支柱的影响小结

| 支柱 | 采集 | 图谱存储 | 结论 |
|---|---|---|---|
| Metrics | ✅ | 节点属性（聚合快照） | 有 |
| Logs | ✅ | 仅 `log_source` 指针 | 有，指针模式正确 |
| Traces | ✅ **采集在跑**（X-Ray 6,949/h + AutoTracing 10.11%） | **无任何 trace 形态** | **缺口在存储层，不在采集层** |
| Profiling | ❌ CE 版仅 On-CPU、不支持 Python，`profile.in_process` 0 行 | 无 | 真的没有 |


## A.11 采集配置默认值（实测，逐字来自 server 的 example 配置）

`GET http://127.0.0.1:30417/v1/vtap-group-configuration/example/` → HTTP 200，
其中与 trace 相关的三项：

```yaml
# HTTP X-Request-ID Key
# Default: X-Request-ID
http_log_x_request_id: X-Request-ID

# TraceID Keys
# Default: traceparent, sw8.
# Note: Used to extract the TraceID field in HTTP and RPC headers, supports filling
#   in multiple values separated by commas. This feature can be turned off by
#   setting it to empty.
http_log_trace_id: traceparent, sw8

# SpanID Keys
# Default: traceparent, sw8.
http_log_span_id: traceparent, sw8
```

**默认只提取 `traceparent`（W3C）与 `sw8`（SkyWalking），不含 `X-Amzn-Trace-Id`。**
而 MySQL 里这三列的 `COLUMN_DEFAULT` 全为 `NULL` ——
说明默认值不在 DB schema 里，而在 agent 内置逻辑中，**空表即等于走内置默认**。

这就完整闭合了 `l7_flow_log.trace_id = 0 / 789,055` 的因果链：
应用（经 aws-otel-collector / X-Ray SDK）发的是 `X-Amzn-Trace-Id`，
DeepFlow 不看这个头，所以字段全空。**不是没埋点，是头对不上。**

## A.12 P2 有两条路，第二条更强

### P2a：只加 trace-id 提取头（最小改动）

建一条 agent-group 配置，把三项改为：

```yaml
http_log_trace_id: traceparent, sw8, X-Amzn-Trace-Id
http_log_span_id:  traceparent, sw8, X-Amzn-Trace-Id
```

- **零应用改动、零 Deployment 改动**
- 效果：DeepFlow 能从 L7 里提出 trace_id，把自己的 L7 记录按请求链缝合
- 局限：只拿到 trace-id 关联，拿不到应用侧真正的 span 树与属性

### P2b：让已有的 sidecar collector 同时把 OTLP 发给 DeepFlow（更强）

example 配置里的两项（**默认即开启**）：

```yaml
# Data Integration Socket
# Note: Whether to enable receiving external data sources such as Prometheus,
#   Telegraf, OpenTelemetry, and SkyWalking.
external_agent_http_proxy_enabled: 1

# Listen Port of the Data Integration Socket
# Default: 38086
external_agent_http_proxy_port: 38086
```

**DeepFlow agent 默认就在 38086 上接收 OpenTelemetry 数据。**
集群里已经有 4 个 `aws-otel-collector` sidecar 在跑，
只需在它们的 collector 配置里**增加一个 OTLP exporter** 指向本节点 DeepFlow agent 的 38086
（现有的 X-Ray exporter 保留不动，OTel collector 支持多 exporter 并行）。

- **零应用代码改动**（只改 collector 的 ConfigMap）
- 效果：DeepFlow 拿到**完整的 span 树**，而不只是 trace-id
- 代价：要改集群里的 collector 配置并重启 collector 容器
- 额外收益：这条路同时把 `petsite`（走 xray-daemon，无 sidecar）的缺口暴露出来
  —— 它需要单独处理，见 A.8

**判定：P2a 与 P2b 不互斥，建议先 P2a（改配置即可验证 trace_id 从 0 变非 0），
再评估 P2b。**

## A.13 顺带找到噪音过滤的配置抓手

此前记录「约 76% 的 L7 采集是可观测性自噪音，五个上报 agent 持续发往
`logs.ap-northeast-1.amazonaws.com`，约 60 万行/小时，而真实业务流量只有 4,760」。
example 配置里对应的抓手是：

```yaml
# Traffic Capture Filter
# Length: [1, 512]
# Note: If not configured, all traffic will be collected. Please
#   refer to BPF syntax: https://biot.com/capstats/bpf.html
capture_bpf:
```

**当前为空 = 全量采集**，这解释了噪音为何存在。
过滤要用 BPF 语法（基于 IP/端口，不能按域名），所以需要先解出
`logs.ap-northeast-1.amazonaws.com` 的 IP 段再写规则。

另注：`l7_log_packet_size: 1024`（协议识别最大长度）、
`l7_log_collect_nps_threshold: 10000`（超过即采样）——
当前 789k 行/小时 ≈ 219 行/秒，远低于采样阈值，说明**数据没有被采样丢弃**。

---

# 附录 B：四支柱接入与 X-Ray 平行数据源（2026-08-29 06:00–07:10 实施）

## B.1 目标的修正

出发点是「让图谱存储 4 个支柱的所有数据」。这个目标本身**不成立**：
L7 flow log 单表 789,055 行/小时,约等于**整个图谱(867 节点/1341 边)的 910 倍**。
Neptune 不该变成遥测存储。

修正后的目标 =「**4 个支柱的数据都能从图谱一跳可达**」——
图谱存指针、聚合结论与派生拓扑,原始遥测留在各自的原生存储。
日志支柱早就是这么做的(只存 `log_source` 指针)。

## B.2 X-Ray 的实际工作范围（24h 实测）

| 节点 | 类型 | 24h 调用 | 累计响应时间 |
|---|---|---|---|
| PetSearch | 服务 | 63,306 | 1,625.2s |
| payforadoption | 服务 | 51,827 | 7.0s |
| petlistadoptions | 服务 | 51,805 | 6.9s |
| PetSite | 服务 | 58 | 46.8s |
| `ServicesEks2-ddbpetadoption7B7CFEC9-3B009FBSQFAM` | DynamoDB **完整表名** | 11,503 | 66.9s |
| S3 | AWS::S3(**泛化名**) | 2,923 | **1,338.7s** |
| STS / SSM / SimpleSystemsManagement / Secrets Manager | 托管服务 | 83/12/67/2 | — |

8 条边,24h 内 **零 Error、零 Fault**。

**只有 X-Ray 能给的结论**:PetSearch 累计响应 1,625.2s,其中 S3 占 **1,338.7s(82%)**
—— 直接回答「服务慢在哪个下游」。

## B.3 X-Ray 与 DeepFlow 的分工（两者盲区不重叠）

| 维度 | X-Ray | DeepFlow |
|---|---|---|
| 前提 | **必须应用埋点** | eBPF 零埋点 |
| 覆盖面 | 只有 4 个已插桩服务 | **所有 Pod** |
| AWS 托管服务粒度 | **精确到资源名** | 只到 DNS 域名;**VPC 端点场景连域名都没有** |
| 延迟归因 | **按下游逐个归因** | 按 flow,归不到逻辑资源 |
| 噪音 | 几乎没有 | `logs.*.amazonaws.com` 及其 DNS 后缀变体约 66 万行/小时 |

一句话:**X-Ray 知道「谁调了哪个具体资源、花了多久」,DeepFlow 知道「网络上真实发生了什么,包括没埋点的东西」。**

## B.4 新增 etl_xray —— 与 DeepFlow 平行的第二拓扑源

`infra/lambda/etl_xray/neptune_etl_xray.py`

### 为什么是独立 ETL 而不是继续塞进 etl_deepflow

etl_deepflow 里原有的 `fetch_xray_dependencies()` 是**读用途**:
把 X-Ray 当漂移判定的第二证据源,只翻已存在边的 `verified_by`,**不产生任何自己的边**。
本模块是**写用途**。独立的三条理由:

1. 「两个独立观测源」必须在架构上真独立。同一个 Lambda 里,一个 bug 同时打掉两边,
   图谱里「双源印证」的展示就是假的。
2. 失败模式与节奏不同:X-Ray `GetServiceGraph` 单次窗口上限 **6 小时**
   (超过报 `Time range cannot be longer than 6 hours`,24h 视图必须分 4 段合并),
   而 DeepFlow 的 DNS 观测是 30 分钟滑窗。
3. 权限面不同:只需 `xray:GetServiceGraph`,不碰 ClickHouse、不碰 EKS token。

### 写入纪律：补充证据，不抢 provenance

- 边**已存在**(无论谁发现)→ 只补 X-Ray 度量,**绝不覆盖 `source`/`dependency_kind`**。
  原 `source` 记录的是「谁首先发现了这条依赖」。
  实测:`petsearch → DynamoDB表` 由 `aws-etl` 以 `static` 声明发现,
  补上 X-Ray 的 11,503 次调用后 `source` 仍是 `aws-etl`。
- 边**不存在** → 新建,`source='xray'`。这才是 X-Ray 自己的贡献。

**刻意不引入 `observed_by_xray` 布尔属性** —— 它完全可由 `xray_last_seen IS NOT NULL` 推导,
而能被推导出来的布尔量迟早与来源不一致(本仓库「字段有值 ≠ 值有用」已踩过四次)。

### 粒度诚实性：不许把泛化 S3 猜成某个 bucket

X-Ray 把 S3 报成一个**字面叫 `S3` 的节点**,没有 bucket 名;STS/SSM/Secrets Manager 同理。
图谱里有 **33 个 S3Bucket**。把 `S3` 猜成其中某一个(哪怕「petsearch 在静态边里只连了一个」)
是**推断而非观测** —— 用观测源的名义写推断结果就是编造。

所以新增节点类型 `AWSServiceEndpoint`,`granularity='service'`,与资源级节点明确区分。
DynamoDB 是唯一例外:X-Ray 给的是完整表名,逐字符命中图谱已有节点,所以那是资源级精确边。

这反而成了 demo 的好对照:同一依赖 `aws-etl` 给资源级、X-Ray 给服务级。

### 干跑抓到的真 bug：别名合并失败

最初节点 key 是 `(规范名, kind, xray_type)`。`SSM` 与 `SimpleSystemsManagement`
规范名都归一到 `ssm`,但**因 `xray_type` 不同而 key 没合并** ——
图谱里出现两个 `ssm` 条目,随后写进同一顶点、`xray_type` 互相覆盖,
**哪个值留下取决于 dict 迭代顺序**,而 `xray_aliases` 每次只带一半原始名。

修法:对 `aws_service`,**规范名就是身份,type 属于数据不属于键**;
`xray_type` 改存**集合**。已由 `test_x02` 锁住。

### 实际写入结果

```
AWSServiceEndpoint 节点 4 个:
  s3              type=AWS::S3                                 aliases=S3
  secretsmanager  type=AWS::Unknown                            aliases=Secrets Manager
  ssm             type=AWS::SSM; AWS::SimpleSystemsManagement  aliases=SSM; SimpleSystemsManagement
  sts             type=AWS::STS                                aliases=STS

边:4 条新建(source='xray') + 2 条双源印证,0 条跳过
```

## B.5 Q21 —— 平行源对账查询（demo 的落点）

`rca/neptune/neptune_queries.py:q21_observation_source_coverage`,已注册进 `query_catalog`(22 条)。

X-Ray 的边属性必须有**读取方**,否则就是第五个「写了但从没被读」的字段。

| coverage | 实测数量 | 含义 | 运维解读 |
|---|---|---|---|
| `both` | **1** | 两源都观测到 | 最可信 —— 两种完全不同的机制互相印证 |
| `xray_only` | **5** | 只有 X-Ray | 连接复用/VPC 端点让 DNS 侧看不见 |
| `deepflow_only` | **52** | 只有 DeepFlow | 未插桩服务发起的调用 |
| `declared_only` | **20** | 只有声明 | 死代码,或两源同时有盲区 |

`both` 的那一条是 `petsite → petsearch`:DeepFlow 首先发现(96 次),X-Ray 印证(122 次)。

`xray_only` 里最有说服力的是 `petsearch → DynamoDB表` **11,503 次** ——
DeepFlow 的 DNS 观测**完全看不见**它(AWS SDK 启动时解析一次域名后复用连接)。
这正是 P0 当初发现的假阴性根因。

## B.6 petsite 入口链路修复 —— 根因是一个命名空间拼写错误

**不是缺 sidecar。** `AWS_XRAY_DAEMON_ADDRESS=xray-service.petadoptions:2000`,
而 `xray-service` 在 **`default`** 命名空间(headless,2000/UDP)。
从 petsite pod 内实测:`getent hosts xray-service.petadoptions` **无返回**;
`xray-service.default` 解析到 3 个 DaemonSet pod IP。pethistory 用的是正确地址。

改成 `xray-service.default:2000` 后,X-Ray 服务图立刻出现:

```
PetSite → PetSearch                  ← 缺失的入口边
PetSite → SimpleSystemsManagement    ← 顺带暴露的新依赖(读 Parameter Store)
```

**这就是 petsite 从来不出现在 X-Ray 服务图里的原因**,与 sidecar 无关。

### 加过 sidecar，验证后撤掉了

设计是用 `awsxray` **receiver** 让 sidecar 成为 xray-daemon 的原地替代,
再扇出到 X-Ray + DeepFlow,零应用代码改动。

- ✅ `awsxray` receiver 在 `aws-otel-collector:v0.47.0` 中**确实存在并可用**
  (`awsxrayreceiver@v0.143.0` 监听 udp 0.0.0.0:2000,X-Ray TCP proxy 也起)。
  **先在低风险的 list-adoptions 上验证,没有在入口服务上做实验。**
- ✅ sidecar 能收能发:窗口完全位于上线之后的 X-Ray 查询显示 `PetSite ok=6`
- ❌ 但 petsite 的 span **进不了 DeepFlow**。三次尝试全部无效
  (`localhost`→`127.0.0.1` 消除 IPv6 歧义、补 `resource` processor 的 `service.name`),
  且**始终无任何导出错误、DeepFlow 返回 HTTP 200**

第三次失败后停止微调,判断根本差异:能落库的 span 来自 OTel SDK 插桩的 HTTP 服务,
带 `http.method`/`net.host.ip` 等语义属性;`awsxrayreceiver` 转出的是 X-Ray 形状的 span,
**缺 DeepFlow 合成流记录所需的网络语义**。这是**结构性不兼容,不是配置旋钮能解决的**。

既然 DeepFlow 那条路走不通,给爆炸半径最大的入口服务白加一个容器和故障面就不合理。
撤回,只保留真正起作用的地址修复。配置存档在
`infra/k8s/p2b-collector-config-xray-receiver.yaml`(含全部实测记录)。

## B.7 pethistory：接入了，但它其实没埋点

给了与另外三个一致的 traces-only 双 exporter 配置(它原先**没有** `AOT_CONFIG_CONTENT`,
跑镜像内置默认配置)。

**没有做「带 metrics pipeline 的配置」,因为那等于复刻一条已经不工作的链路**:
- 内置默认配置里的 `prometheus` receiver 在抓应用 `:8080`,**每 20 秒失败一次**
- CloudWatch 里**没有任何 pethistory 相关的自定义指标**

副作用是那条持续报错的死链路消失了:新 Pod 的 `Failed to scrape` 从 25 行降到 **0 行**。

但**它的 Pod IP 始终不出现在 DeepFlow 的 OTLP span 里,且它在 X-Ray 服务图里从来没出现过**
—— 说明应用侧根本没有活跃的 OTel 插桩。环境变量和 sidecar 都齐,但没有 trace 可转发。
配置改动无害,不过别把它记成「已接入」。

## B.8 图谱状态变化

| 指标 | 本轮前 | 本轮后 |
|---|---|---|
| 节点 | 867 | **890** |
| 边 | 1,341 | **1,475** |
| 活图谱节点类型 | 31 | **32**(+AWSServiceEndpoint) |
| 边类型 | 26 | 26(复用,未新增) |
| schema 声明节点类型 | 32(表头误写 31) | **33**(表头已修正) |
| query_catalog | 21 | **22**(+Q21) |
| 测试 | 372 passed | **384 passed / 0 failed / 145 skipped** |

**顺带修正了一处早就存在的文档错误**:schema 表头写「31 种」,
但节点段落里程序化统计是 **32** 个声明。差在 `TopologyChange` ——
它 0 实例(在 `PENDING_FIRST_INSTANCE` 里),所以**活图谱 31 种、schema 声明 32 种**。
那个表头写的是活图谱数,不是声明数。已改为声明数并写明这个区别。

## B.9 部署状态与未完成项

**etl_xray 目前是手动跑的,尚未部署为 Lambda。**
现有四个 ETL Lambda 均为 `python3.12`/256MB。部署需要:新建函数、IAM 角色
(只需 `xray:GetServiceGraph` + Neptune 写权限)、EventBridge 调度。
`test_x12` 对此只**告警不阻塞** —— 让它阻塞会训练人忽略失败。

本地运行方式(依赖由调用方提供,模块内**不改全局 `sys.path`**):
```bash
cd infra/lambda/etl_xray
PYTHONPATH=../shared/python python3.11 neptune_etl_xray.py 24
```

仍未定位:**非 `single` 写 `last_updated` 的顶点写入方**。
6 个 CFN 触及的类型在规约后又回到 2 个值。已排除 `property(single)` 失效、
`upsert_vertex`、生产/分支代码漂移、边写入。**不要臆造根因。**


---

# 附录 C：etl_xray 部署为 Lambda（2026-08-29 07:55–08:15 实施）

## C.1 部署结果

| 项 | 值 |
|---|---|
| 函数 | `neptune-etl-from-xray` |
| 运行时 | python3.12 / x86_64 / 180s / 256MB |
| 角色 | `neptune-etl-lambda-role`(**复用**;`etl-xray-read` 内联策略此前已加) |
| 层 | **`neptune-client-base:3`**(新发布,见 C.2) |
| VPC | `subnet-0f801fa79077eb277`,`subnet-047a94f9c5ab6302a` / `sg-078f24929b25f09cd` |
| 环境变量 | `NEPTUNE_ENDPOINT`,`NEPTUNE_PORT=8182`,`REGION`,`XRAY_LOOKBACK_HOURS=24`,`XRAY_STALE_SECONDS=21600` |
| 包内容 | `neptune_etl_xray.py` + `service_mappings.json`(10.7KB) |
| 调度 | EventBridge `neptune-etl-xray-hourly` = `rate(1 hour)`,ENABLED |

**为什么是 1 小时**:X-Ray 服务图按部署节奏变化而非按秒;回看窗口本就 24h,
5 分钟一轮只是重复读同一批数据;失效阈值 6h,每小时刷新留出 6 次余量。

**VPC 出网到 X-Ray API 已实测可达** —— 首次调用虽失败,但失败点在写 Neptune 阶段,
说明 `GetServiceGraph` 已成功返回。

## C.2 顺带修掉一个层的根因缺陷：neptune-client-base v2 不带 requests

首次调用报 `No module named 'requests'`。原因是 **层里的
`neptune_client_base.py` 自己 `import requests`,而层却不打包它** ——
所以现有四个 ETL 每个都在自己目录里 vendor 了一份(etl_deepflow 里 119 个文件入库)。

修法:发布 **`neptune-client-base:3`**,把 v2 的内容 + 从 etl_deepflow 复制的
`requests/urllib3/certifi/idna/charset_normalizer`(**已在生产验证过的 vendored 副本,
未跑 pip**)一起打包,924KB。

**只让新函数指向 `:3`,现有四个 Lambda 仍停在 `:2`** —— 发布新版本是纯增量操作,
对现有函数零风险。后续若要收敛,可逐个切换并各自验证。

## C.3 部署后抓到两个真 bug（都是写 demo 脚本时暴露的）

### ① `Type='remote'` 被误判成 AWS 托管服务

实测 X-Ray 用 `Type='remote'` 报了
`search-service.petadoptions.svc.cluster.local`。原兜底逻辑是
「任何不认识的 type 都算 aws_service」,**太宽松** ——
一个**集群内 K8s 服务**被建成了 `AWSServiceEndpoint` 节点。

收紧判据:只有 type 以 `AWS::` 开头,或名字在别名表里
(`Secrets Manager` 那种被报成服务本体的情况),才算 AWS 托管服务。
其余按 K8s FQDN 处理 —— 剥掉 `.svc.cluster.` 后缀。

**只剥这一种明确形态**,不对任意含点的名字截断:否则
`logs.ap-northeast-1.amazonaws.com` 会被误伤成 `logs`,凭空造出不存在的服务。
已由 `test_x05b` / `test_x05c` 锁住。误建节点已 `DETACH DELETE` 清理。

### ② `edges_created` 谎报成功，连续三轮都报 created=1 而边总数不变

现象:连跑三次都报 `created=1`,但图谱边总数始终 1476、
带 xray 度量的边只有 **7 条而观测到 8 条**。

**双重根因**:
1. 图谱里那个服务叫 `petsearch`,**没有** `search-service` 节点
   (K8s 部署名 ≠ 图谱服务名)。修法:剥完 FQDN 再过一遍
   `service_mappings.json` 的 `k8s_alias`(里面**本来就有**
   `"search-service": "petsearch"`)—— **复用既有映射,不另造第二套**,
   两套映射是本仓库反复出问题的模式。
2. 目标节点不存在时,`.V().where(<matcher>)` 匹配不到任何东西,
   整条 traversal **静默产出空集,不抛异常也不写边**,而原实现是
   「`neptune_query` 没抛异常就 `created += 1`」。
   修法:新建分支**回读确认边真的落地**才计数,落不了记进
   `skipped_no_node`;另加 `edges_write_failed` 计数,避免失败被吞。

映射生效后 `xray_edges_seen` 从 8 降到 **7** —— 同一条依赖的两种身份
(按插桩服务名 / 按主机名)在抓取阶段就合并了,这正是正确行为。

由 `test_x05d`(映射)、`test_x05e`(禁止内联第二套映射)、
`test_x05f`(created 必须回读确认)锁住。

## C.4 最终验证

连跑三次,三次完全一致:

```
第 1 次: seen=7 created=0 corroborated=7 skipped=0 failed=0
第 2 次: seen=7 created=0 corroborated=7 skipped=0 failed=0
第 3 次: seen=7 created=0 corroborated=7 skipped=0 failed=0
带 xray 度量的边: 7   ← 与 seen 一致
边总数: 1476          ← 重复跑不增长
```

三个数字互相对得上,这是幂等与计数诚实性的联合判据。
**最初正是"三个数字对不上"暴露了 ② 那个 bug。**

## C.5 状态

| 指标 | 附录 B 时 | 现在 |
|---|---|---|
| 节点 | 890 | **891** |
| 边 | 1,475 | **1,476** |
| AWSServiceEndpoint | 4(含 1 个误建) | **4**(全部正确) |
| `source='xray'` 边 | 4 | **5** |
| 带 xray 度量的边 | 6 | **7** |
| test_33 用例 | 12 | **17** |
| 测试总计 | 384 passed | **389 passed / 0 failed / 145 skipped** |

Demo 脚本:`todo/demo-script-parallel-sources_20260829-0810.md`(460 行)。
脚本里每条命令都实跑验证过,每个预期输出都是实测值。
**幂等演示改为连跑两次** —— X-Ray 的 24h 窗口里随时会滚进新边,
拿首轮的 `created=0` 当预期,现场会翻车。

