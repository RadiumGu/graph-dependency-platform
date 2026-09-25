# 东京 → 韩国切换手册

> 最后核实:2026-09-25 ｜ 对应基础设施:`infra/dr-korea/` 01–11 + `dr-plan-generator/worker/`
> 每一条「已验证」都能在 `deployment-record.md` 的 4.x 节找到当时的原始证据。

## 一句话结论

**现在切,你会得到一个基础设施层完好、应用层不能服务用户的站点。**

具体说:EKS 节点会起来、数据库会提升、petsite 的 pod 会变成 `Running` 且健康检查
全绿 —— 但没有入口让用户进来,没有配置让它连上后端,而且**没有任何自动判据会告诉你
这件事**。这是本手册最重要的一句话,详见第四节。

守夜灯的设计目标本来就是「基础设施常备、应用按需」,所以这不是故障,是**当前完成度**。
把它推到「真能接管」还差的工作量在第五节,已按实测清点估过。

---

## 二、现在切,一步步会发生什么

### 步骤 0:前提检查(2 分钟)

```bash
# Temporal 与 worker 是否在线（IP 已固定为 10.20.1.10，实例重建也不变）
aws ssm send-command --region ap-northeast-2 --instance-ids i-09380e417a0177ed4 \
  --document-name AWS-RunShellScript \
  --parameters 'commands=["systemctl is-active temporal dr-worker"]'

# Aurora 全局集群的复制延迟（切换前必看，这决定你会丢多少数据）
aws rds describe-global-clusters --region ap-northeast-1 \
  --global-cluster-identifier petsite-global \
  --query 'GlobalClusters[0].GlobalClusterMembers[].{cluster:DBClusterArn,writer:IsWriter}'
```

复制延迟本身要看 CloudWatch 的 `AuroraGlobalDBReplicationLag`(单位毫秒)。
**这个数决定你该用 switchover 还是 allow-data-loss**,见步骤 2。

### 步骤 1:扩起节点(约 50 秒)—— ✅ 已真演练

由 Temporal workflow 的 `scale_up_nodegroup` activity 执行。
实测:`desiredSize` 0→2,**48 秒**后 2 个节点 `Ready=True`。

核实判据是 `count_ready_nodes()`,直接查 k8s API 数
`status.conditions[type=Ready]` 为 `True` 的节点数。

> ⚠️ **不要用「ASG 扩了」当判据。** 「ASG 有 2 个实例」≠「集群有 2 个可调度节点」。
> 同理缩容时必须看 ASG 的 `LifecycleState` —— 实测并排证据:
> ASG 层显示 `Terminating:Wait`,而 EC2 层显示 `running`,**只看 EC2 分不出来**。

### 步骤 2:提升数据库 —— ⚠️ 权限与链路已验证,**从未真提升过**

四种裁决已全部 dry-run 验过(`ordered` / `allow_data_loss` / `abort` / 非法值),
0 失败事件。权限 `rds:FailoverGlobalCluster` 已放开,带三个 ARN:

```
arn:aws:rds::926093770964:global-cluster:petsite-global                    ← 全局集群（无 region 段）
arn:aws:rds:ap-northeast-1:…:cluster:serviceseks2-databaseb269d8bb-…       ← **当前主集群**
arn:aws:rds:ap-northeast-2:…:cluster:dr-korea-aurora-secondarycluster-…    ← 目标从集群
```

> ⚠️ **漏掉中间那个「当前主集群」会让调用被拒,而报错只说没权限,不会说少了哪个 ARN。**

`--switchover` 与 `--allow-data-loss` **互斥**:

| 场景 | 用哪个 | 后果 |
|---|---|---|
| 东京还活着(计划内演练) | `--switchover` | 无数据丢失,但**需要东京可达** |
| 东京已经挂了 | `--allow-data-loss` | 丢掉未复制的部分,按复制延迟估 |

两个分支都**显式传参**,不依赖 API 默认值 —— 一个明确的裁决不能落在默认行为上。

**这一步会让东京主库从 writer 变 reader,属于必须先问用户的操作。**

### 步骤 3:部署应用 —— ❌ 这一步现在走不通

这是手册里唯一一个「没有可执行步骤」的环节。原因见下两节。

### 步骤 4:切 DNS —— ❌ 未配置

没有 Route 53 的故障转移记录集,没有健康检查。**属于必须先问用户的操作。**

---

## 三、能力矩阵:哪些是真验证过的

| 层 | 状态 | 决定性证据 | 记录 |
|---|---|---|---|
| Temporal 服务端 | ✅ | 1.29.7 + PG16 + UI 2.54.1,固定 IP `10.20.1.10` | 4.3 / 4.20 |
| 实例重建后自恢复 | ✅ | 换实例后**零手工介入**全自动恢复,AgentCore 连上新 IP | 4.20 |
| AgentCore runtime | ✅ | `temporal_mcp` v4 READY | 4.7 |
| 节点扩容 | ✅ | 真演练 48 秒,独立核实 2 节点 `Ready=True` | 4.13 / 4.16 |
| 缩容核实 | ✅ | 看 ASG `LifecycleState`,活环境抓到 `Terminating:Wait` | 4.16 |
| ECR 镜像在韩国 | ✅ | 7 个镜像 digest 逐个比对一致;新推送 15s `COMPLETE` | 4.17 |
| **韩国能真拉起镜像** | ✅ | pod `Running` / restarts=0,镜像来自 ap-northeast-2 | 4.21 |
| IRSA(7 个应用 SA) | ✅ | SA token 换到生产角色凭据;错 SA 被 `AccessDenied` | 4.18 |
| **IRSA 在 pod 里真工作** | ✅ | 启动日志 `Found credentials using the AWS SDK's default credential search` | 4.21 |
| 数据库提升权限+链路 | ⚠️ | 四种裁决各走对分支,0 失败 —— **但从未真提升** | 4.19 |

| 应用配置 | ❌ | 韩国 SSM `/petstore` **0 个参数**(东京 41 个) | 4.21 |
| LB controller 的 IRSA | ✅ | 韩国 SA token 换到生产角色;bogus SA 被 `AccessDenied` | 五③ |
| 外部入口(ALB/目标组) | ✅ | 真流量走通:ALB→目标组→pod,`GET /health/status` 200 | 4.23 |
| **petsite 能否服务用户** | ❌ | **不能** —— HTTP 200 但页面是 `Error - …`;`/petstore/searchapiurl` ParameterNotFound | 4.23 |
| DNS 切换 | ❌ | 未配置 | — |

---

## 四、⚠️ 最危险的一点:失效形态不是崩溃

在一个**零配置**的 petsite 上,实测所有 k8s 层面的信号:

```
pod 状态         Running        ✅
restarts         0              ✅
readiness 探针    通过           ✅
/health/status   200 "Alive"    ✅
```

因为 `/health/status` 返回的是**硬编码的 5 字节字符串**,完全不碰配置。

**所以任何基于「pod 健康」的灾备就绪检查,在一个什么都没配好的站点上是全绿的。**

#### 2026-09-25 更新:比这更坏一层 —— **状态码也会骗人**

入口打通之后拿到了真流量的答案。在一个**零配置**的 petsite 上:

| 信号 | 结果 |
|---|---|
| pod `Running` / restarts 0 | ✅ 绿 |
| readiness 探针 | ✅ 绿 |
| `/health/status` | ✅ 200 "Alive" |
| **ALB 目标健康** | ✅ **healthy** |
| **`GET /` 的 HTTP 状态码** | ✅ **200** |
| 页面 `<title>` | ❌ `Error - Observability PetAdoptions` |

根因:`PetSite.Configuration.ParameterRefreshManager` 去取
`/petstore/searchapiurl` → `ParameterNotFound` → 渲染错误页 → **以 200 返回**。

**从 pod 到 ALB 到 HTTP 状态码,整条链路全绿,而用户看到的是错误页。**
唯一能区分的判据是**页面内容**。
崩溃会告警,这个不会 —— 它会让你以为切换成功了。

对我们自己的自动化也是同一条边界:`count_ready_nodes()` 只回答
**「集群有可调度容量」**,它不回答「应用能服务」。这两件事之间隔着配置与后端依赖,
而**目前没有任何自动判据能跨过去**。

### 附带的一条方法论

同一次演练里,pod proxy 打出 `GET / → 302`、`/adoptionlist → 404`。
差点据此写成「韩国的 petsite 起不来」—— **做了东京对照组才发现东京一模一样**:

```
             /health/status   GET /   /adoptionlist
韩国            200 (5B)        302        404
东京（对照）     200 (5B)        302        404
```

**一个只在单侧观察到的现象,在有对照之前不能当成差异。**

---

## 五、到「真能接管」还差什么(实测清点,非估算)

### ① 工作负载:东京跑 7 个 Deployment,不是 1 个

2026-09-25 读东京活集群 `petadoptions` 命名空间:

```
list-adoptions          2/2   sa=list-adoptions-sa
pay-for-adoption        2/2   sa=pay-for-adoption-sa
petfood                 2/2   sa=petfood-sa
pethistory-deployment   2/2   sa=pethistory-sa          ← 镜像用可变 tag :latest
petsite-deployment      2/2   sa=petsite-sa             ← 只有这个做过韩国演练
search-service          2/2   sa=search-service-sa
traffic-generator       1/1   sa=traffic-generator-sa
```

韩国侧目前 **0 个**(演练用的那个已删)。7 个 SA 的 IRSA 信任都已补好。

### ② ~~入口~~ —— ✅ 已于 2026-09-25 打通(4.23)

原来的缺口是:`petadoptions` 下**没有 Ingress**,7 个 Service 全是 `ClusterIP`,
真正的入口是 `TargetGroupBinding` 把 pod 绑进 CDK 建的 ALB 目标组,
而**那些目标组 ARN 是 region 专属的,照搬到韩国无效** —— 清单能过校验、
pod 能起来,只是永远没有流量。静态检查发现不了。

现在韩国有自己的一份:

```
ALB        dr-korea-petsite-alb    internal / active / 2a+2b
目标组      dr-korea-petsite-tg     8080  健康检查 /health/status
           dr-korea-pethistory-tg  80    健康检查 /health/status
controller Helm chart 3.0.0（与东京同版本）
```

**决定性证据**(与部署不同的手段:查 `elbv2 describe-target-health`,
不看 controller 日志):

```
10.20.1.88:8080  healthy   ← 连续 6 次稳定
真流量 GET /health/status → HTTP 200 "Alive"（从 Temporal 实例打 internal ALB）
```

#### ⚠️ 健康检查刻意偏离东京

东京 petsite 目标组的健康检查是 `GET /` 期望 200,而**实测 0/2 healthy**
(`Target.ResponseCodeMismatch`)—— 因为 petsite 在 `/` 上做会话分配重定向
(`Location: /?userId=…`),永远不是 200。**韩国不能照抄那份配置。**

#### ⚠️ 缩容到零会死锁 30 分钟

coredns 的 PDB(`maxUnavailable=1`,2 副本)在只剩一个节点时永远无法满足,
节点卡在 `Terminating:Wait` 直到 `HeartbeatTimeout=1800` 超时。
因果验证过:降到 1 副本后 2 分钟内就终止。
**扩容路径要把 coredns 恢复成 2 副本,缩容路径要先降到 1。**

### ③ ~~漏掉的第 8 个 IRSA 消费者~~ —— ✅ 已于 2026-09-25 补上

LB controller 自己也用 IRSA,而它最初不在映射里:

```
kube-system/aws-load-balancer-controller  v3.0.0  ready=2/2
    sa:   alb-ingress-controller
    role: ServicesEks2-LoadBalancerServiceAccountB6807779-QEjXooFf4b6b
```

漏掉它的表现极坏:**controller 装上了、pod 起来了、一个 ALB 也不建** ——
而灾备站点没有 ALB 就等于没有入口。

**决定性证据**(与部署不同的手段:真做一次 token 交换,不看脚本自己的报告):

```
韩国 kube-system/alb-ingress-controller 的 token
  → arn:aws:sts::926093770964:assumed-role/ServicesEks2-LoadBalancer…/korea-lbc-verify   ✅
同命名空间下一个 bogus SA 的 token
  → AccessDenied: Not authorized to perform sts:AssumeRoleWithWebIdentity                ✅
```

后者证明 `:sub` 限定真的生效 —— 不是「该集群任何 SA 都能 assume」。

#### 顺带修掉了根因

漏掉它的根因不是忘了某一项,而是**命名空间隐含在脚本的常量里**
(`NAMESPACE = "petadoptions"`),让人只会去想「petadoptions 下有哪些 SA」。

现在 `infra/dr-korea/irsa-korea-mapping.json` 的每一项都自带 `namespace`,
脚本里的那个常量已删除,`korea_statement()` 把 namespace 收成**必填位置参数**
—— 默认值会把「忘了写」变成「静默用了 petadoptions」,而那个错误的表现是永远 403。

#### ⚠️ 这条信任带来的影响面

那个角色挂的托管策略 `ServicesEks2-LoadBalancerSAPolicy6C6E33B0-PNHgXHQuKjvt`
共 15 条语句,其中 9 条 `Resource` 是 `*`,**0 条按 region 限定**。
所以韩国集群的 controller 在凭据层面**也能操作东京的 ALB**。

接受的理由:守夜灯平时零节点、controller 不运行,且这是 demo 环境。
要收紧应当给策略加 `aws:RequestedRegion` 条件,**而不是不加信任**
(不加信任的后果是韩国建不出入口)。

### ④ 集群 addon:韩国 1 个,东京 5 个

```
东京  amazon-cloudwatch-observability, aws-ebs-csi-driver, aws-guardduty-agent,
      aws-network-flow-monitoring-agent, eks-pod-identity-agent
韩国  aws-guardduty-agent
```

按用户早先的决定,DeepFlow 与 Neptune 在灾备 region 不管,所以
`aws-network-flow-monitoring-agent` 可以不补。其余按需。

### ⑤ region 内的后端依赖 —— 这是最大的一块

东京 `/petstore` 前缀下 41 个参数,韩国 0 个。**但照搬参数值没用**,因为值指向
东京的资源:

```
rdsendpoint / rds-reader-endpoint / rdssecretarn   东京 Aurora 与 Secrets
queueurl / snsarn / petadoptionsstepfnarn          东京 SQS / SNS / StepFunctions
dynamodbtablename / s3bucketname                   东京的表和桶
dataprotection/key-* ×4（SecureString）             ASP.NET Data Protection 密钥
agent/waggleairuntimearn                           东京的 AgentCore runtime
```

在「东京挂了」的场景下,复制这些值**等于让韩国去连一堆不存在的后端**。
真正需要的是韩国侧自己的 DynamoDB / SQS / SNS / StepFunctions / S3 / API GW,
或者改用全局版本的服务。

**这是一个独立的工程项,不是配置复制。**

---

## 六、判据陷阱清单(踩过的,别再踩)

| 看到 | 不能推出 | 真相在哪 |
|---|---|---|
| pod `Running` + 健康检查绿 | 应用能服务 | `/health/status` 是硬编码字符串,不碰配置 |
| ASG 有 N 个实例 | 集群有 N 个可调度节点 | 查 k8s API 数 `Ready=True` |
| EC2 `running` | 实例没在排空 | 查 ASG `LifecycleState` |
| 单侧出现异常响应 | 这就是差异 | 必须有对照组 —— 东京一模一样 |
| `curl` 超时 / `HTTP 000` | 服务坏了 | 安全组静默丢包。**「没测到」≠「坏了」** |
| 两侧镜像 digest 一致 | 目标侧能拉起来 | 要真起一次 pod(4.21 才补上这一环) |
| 清单通过 YAML 校验 | 资源引用有效 | TargetGroupBinding 的 ARN 是 region 专属的 |
| worker 在队列上接单 | 跑的是新代码 | 进程可能还拿着内存里的旧模块 |
| 指标平稳 | 系统健康 | 零流量与健康在指标上**无法区分** → inconclusive |

---

## 七、探活的正确姿势

**不要**从 VPC 内另一台机器直连 pod IP —— pod 的 ENI 挂的是集群安全组,
只放行来自自己的流量,你会得到一片 `HTTP 000`,而那是「没测到」不是「坏了」。

走 k8s API 的 pod proxy,用控制面这条已验证的通路,不需要改任何安全组:

```
GET {cluster_endpoint}/api/v1/namespaces/{ns}/pods/{pod}:{port}/proxy/{path}
```

访问控制面 443 还需要三个条件,缺一不可,而**每个缺失的表现都不一样**:

| 缺什么 | 表现 |
|---|---|
| EKS AccessEntry | 调不通 k8s API |
| 控制面 443 入站规则 | **`curl` 超时**(不是 refused —— 安全组静默丢包) |
| 对应资源的 RBAC | **403** |

> `AmazonEKSViewPolicy` **不含 `nodes`**(官方资源表里全是 namespace 内资源);
> `AmazonEKSAdminViewPolicy` 是 `*/*` 且文档明确写着含 Secrets。
> 所以 worker 用的是只含 `nodes` 的自定义 ClusterRole,绑到 k8s **组**
> `dr-node-readers` —— 访问条目的 username 含 `{{SessionName}}`,运行时才定,
> 绑用户名绑不住。

**IAM/EKS 授权是最终一致的**:改完立刻用会失败,而错误不会告诉你「是因为还没传播」。
等 20–25 秒。

---

## 八、演练时的纪律

- 临时提权(如 `AmazonEKSClusterAdminPolicy`)**用完立刻摘,并核实
  `list-associated-access-policies` 返回空数组**。本会话有一次演练结束时忘了摘,
  是下一轮开头才补上的。
- 演练产物放 S3 要放进 worker 角色允许的前缀(`plans/*` 或 `worker/*`)。
  放错会 403 —— **处置是挪文件,不是为演练放宽权限边界。**
- 演练用的 k8s 资源用完删掉,并核实剩余数为 0。
- 节点组缩回 `desiredSize=0`,别把守夜灯留成常亮。
