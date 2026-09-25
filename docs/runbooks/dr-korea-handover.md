# 韩国守夜灯灾备站点 —— 交接文档

> 写给**没参与这段工作、要接手它的人**。
> 先读这份,再按需要去看 `dr-korea-switchover.md`(操作手册)和
> `deployment-record.md`(4.1–4.29,每一步的完整证据,136 KB)。
>
> 最后更新:2026-09-25 ｜ 对应 main 分支 PR #27 之后

---

## 零、一句话

**韩国(`ap-northeast-2`)有一套 petsite 的守夜灯站点,平时零节点、月成本约几十美元,
数据库实时同步。已经真演练过:扩容后 4 个业务页面里 3 个渲染正确,
数据库真提升为主库、验证可写、又真切回东京。**

剩下的差距是清楚的,写在第四节,**没有一条是「大概可以」**。

---

## 一、现在能做什么 / 不能做什么

| 能力 | 状态 | 证据 |
|---|---|---|
| 扩容出可服务的 petsite | ✅ | 4 页面实测标题,3/4 正确 |
| 数据库接管 | ✅ | 真提升 57 秒 + 验证可写 + 回切 52 秒 |
| 入口(internal ALB) | ✅ | 目标 healthy、页面 200 |
| 7 个工作负载 | ✅ | Deployment 7/7,SA/ConfigMap/环境变量全对 |
| region 内后端 | ✅ | DynamoDB 3 表 / S3 / SQS / SNS / EventBridge / Step Functions |
| 41 个 SSM 参数 | ✅ | 分六档处理,SecureString 用指纹核对 |
| 可观测性 | ✅ | `amazon-cloudwatch-observability` 与东京同版本 |
| **公网访问** | ❌ | ALB 刻意建成 internal(用户决定「韩国先不暴露公网」) |
| **`/Checkout` 页面** | ❌ | **应用自身缺陷,东京也一样** —— 见第四节 |
| **DNS 切换** | ❌ | 从未做过,不在授权范围 |
| **DynamoDB 数据实时同步** | ❌ | 现在是脚本单次复制;全局表需改东京生产表 |
| **WaggleAI(Bedrock/AgentCore)** | ❌ | 未建,成本台阶高,待决定 |

---

## 二、⚠️ 这套东西最值得你知道的一件事

**「全绿」在这套系统里出现过四种完全不同的假象。** 这是整段工作最贵的产出:

| 形态 | 表现 | **唯一**能区分的判据 |
|---|---|---|
| 健康探针骗人 | `/health/status` 返回**硬编码 5 字节 `"Alive"`**,根本不碰配置 | 打业务页面,看内容 |
| 状态码骗人 | `GET /` 返回 **HTTP 200**,内容是 `<title>Error - …</title>` | **看页面 `<title>`,不看状态码** |
| pod 根本不存在 | ServiceAccount 缺失 → ReplicaSet `FailedCreate` → **`kubectl get pod` 一个异常都看不到** | 看 Deployment/ReplicaSet 的 conditions |
| 只在某条请求路径显形 | 资源策略只授权了 1 个角色 → `/PetListAdoptions` **挂 60 秒返回 504**,而 Deployment 7/7 就绪 | **逐个业务页面打一遍** |

**验收判据就一条:逐个业务页面打一遍,看 `<title>`。** 其它都骗过我至少一次。

配套的两条:

- **零流量与健康在指标上无法区分** → 没有流量时任何「指标正常」都是 inconclusive
- **「没测到」≠「坏了」** —— `HTTP 000` 是安全组静默丢包,
  `connection refused` 可能只是我用错了端口(真实发生过:petfood 是 8080 不是 80)

---

## 三、接管操作:顺序不能错

### 3.1 扩容(必须按这个顺序)

```bash
# ① coredns 恢复 2 副本 —— 缩容时降到 1 过
kubectl -n kube-system scale deploy coredns --replicas=2

# ② 扩容节点
aws eks update-nodegroup-config --region ap-northeast-2 --cluster-name dr-korea-petsite \
  --nodegroup-name dr-korea-workers --scaling-config minSize=0,maxSize=3,desiredSize=2

# ③ ⚠️ 等 LB controller 的 webhook endpoint 出来，再动任何 Service/TGB
kubectl -n kube-system get endpoints aws-load-balancer-webhook-service
```

> #### ⚠️ 第 ③ 步不是保险起见,零节点时集群**拒绝创建任何 Service**
>
> LB controller 的 webhook 预置在集群里,而它自己需要节点:
>
> ```
> mservice.elbv2.k8s.aws            failurePolicy=Fail  services            namespaceSelector={}
> mtargetgroupbinding.elbv2.k8s.aws failurePolicy=Fail  targetgroupbindings namespaceSelector={}
> ```
>
> `namespaceSelector={}` = **所有命名空间**。零节点时没有 endpoint,于是:
>
> ```
> Error from server (InternalError): failed calling webhook "mservice.elbv2.k8s.aws":
>   no endpoints available for service "aws-load-balancer-webhook-service"
> ```
>
> **这是直接实验出来的**:同一个探针 Service,零节点时被拒,
> controller 就绪后 `created`。装 `amazon-cloudwatch-observability`
> 时就是这么失败的 —— 报错完全指不到「节点为零」这个原因。

### 3.2 缩容(顺序相反)

```bash
# ① 先把 coredns 降到 1 副本 —— 不降会死锁 30 分钟
kubectl -n kube-system scale deploy coredns --replicas=1
# ② 再缩节点
aws eks update-nodegroup-config … --scaling-config minSize=0,maxSize=3,desiredSize=0
# ③ 摘掉任何临时提权，并核实返回空数组
aws eks list-associated-access-policies --region ap-northeast-2 \
  --cluster-name dr-korea-petsite --principal-arn <role-arn> \
  --query 'associatedAccessPolicies[].policyArn' --output json
```

> **为什么必须先降 coredns:** PDB 算术。
> 副本=2 / 可用=1(另一个 Pending 无处调度)/ `maxUnavailable=1` → 允许驱逐 **0** 个。
> 而 ASG 的 `Terminate-LC-Hook` 的 `HeartbeatTimeout=1800`,所以节点会卡满 **30 分钟**。
> 降到 1 副本后 `allowed` 由 0 变 1,**卡了 6 分钟的节点 2 分钟内就终止了** —— 因果验证过。

### 3.3 数据库切换

```bash
# 提升韩国（--region 是**当前主库**所在的 region）
aws rds switchover-global-cluster --region ap-northeast-1 \
  --global-cluster-identifier petsite-global \
  --target-db-cluster-identifier arn:aws:rds:ap-northeast-2:926093770964:cluster:dr-korea-aurora-secondarycluster-5ctcqnmbkro4

# 回切（注意 --region 变成了 ap-northeast-2，因为主库已经在韩国）
aws rds switchover-global-cluster --region ap-northeast-2 \
  --global-cluster-identifier petsite-global \
  --target-db-cluster-identifier arn:aws:rds:ap-northeast-1:926093770964:cluster:serviceseks2-databaseb269d8bb-efjeyzicx2ak
```

三个容易弄错的点(出处
`AmazonRDS/latest/AuroraUserGuide/aurora-global-database-disaster-recovery.html`):

1. 有**专用命令** `switchover-global-cluster`,不必用 `failover-global-cluster --switchover`
2. `--region` 是**主库所在的 region** —— **回切时它变了**
3. `--target-db-cluster-identifier` 必须是 **ARN**(裸标识符定位不到跨 region 的集群)

**东京还活着时绝不要用 `--allow-data-loss`。** 实测耗时:提升 ≈57 秒、回切 ≈52 秒。

---

## 四、还没解决的事(每条都有实测结论,没有一条是估的)

### 4.1 `/Checkout` 打不开 —— 应用自身缺陷,**东京也一样**

`petfood-rs/src/repositories/cart_repository.rs:338`:

```rust
.get_item().key("user_id", AttributeValue::S(user_id.to_string()))   // 只给分区键
```

`GetItem` 必须给全主键,而 carts 表是复合主键(`user_id` + `item_id`)。
两侧表结构**并排比对过,逐字相同** → 东京会以完全相同的方式失败。

**刻意没把韩国表改成单键**:那能让韩国的 `/Checkout` 好起来,
但会让灾备站点的行为与生产不一致。要修就两边一起修(改应用,用 `Query`)。

### 4.2 东京生产站点的两个真实问题(未动,不在授权范围)

- **petsite 目标组 `Servic-PetSi-7JEWC19HNKSR` 是 0/2 healthy**
  (`Target.ResponseCodeMismatch`)。健康检查是 `GET /` 期望 200,
  而 petsite 在 `/` 上做会话分配重定向(`Location: /?userId=…`),**永远不是 200**。
  它确实挂在公网 ALB 的 443 监听器上。两个独立测量互相印证。
- `/petstore/searchimage` 指向 `petsearch-java:latest`,而**两个 region 的 ECR 都没有这个仓库**。

### 4.3 「东京真的可写」是 inconclusive

回切后「东京恢复为 writer」有三处一致的证据(RDS 事件 + 集群成员 + 实例角色)。
但**行为验证做不到**,原因是结构性的:

```
list-access-entries  东京 PetSite    20 个条目，无 Temporal 角色
list-access-entries  韩国 dr-korea   有 dr-korea-temporal-TemporalRole-…
```

DR worker **从设计上打不到东京集群**,也没有从韩国 VPC 到东京库的网络通路。
**记 inconclusive,不记通过。**

### 4.4 两项等你决定

| 事项 | 为什么要你决定 | 代价 |
|---|---|---|
| **DynamoDB 全局表** | 需两处改动**东京生产表**:开 Streams(实测 `StreamSpecification` 是 `null`)+ 加韩国副本 | 现在是脚本单次复制,切换后韩国的表是**冷数据** |
| **WaggleAI 档** | Bedrock guardrail/memory/**知识库** + AgentCore gateway/runtime | 知识库需向量存储,是**数量级更高**的成本台阶;另内网压测 ALB ≈ $16/月 |

---

## 五、资源清单

```
EKS 集群         dr-korea-petsite（1.35，私有 endpoint）
节点组           dr-korea-workers（平时 desiredSize=0）
ASG              eks-dr-korea-workers-4ad06992-7957-b47d-7adf-72656f7ffdf0
VPC              vpc-0238efd50c0bf0dac（10.20.0.0/16）
子网             subnet-0f599ec0925b9158b(2a) / subnet-0ad20a5cd85143dff(2b)
                 两者只有 kubernetes.io/role/internal-elb=1
                 （要改公网:加 kubernetes.io/role/elb 标签 + 改 ALB Scheme）
ALB              dr-korea-petsite-alb（internal）
                 internal-dr-korea-petsite-alb-263460690.ap-northeast-2.elb.amazonaws.com
Aurora           dr-korea-aurora-secondarycluster-5ctcqnmbkro4（全局集群 petsite-global）
DB 密钥          dr-korea/petadoptions/database-C31ZSc
Temporal 实例    i-09380e417a0177ed4 / 10.20.1.10（IP 已固定）
Temporal 角色    dr-korea-temporal-TemporalRole-MbW4WYmjoR8J
桶               dr-korea-agentcore-926093770964-ap-northeast-2
                 worker 角色只能读 plans/* 与 worker/* 两个前缀
韩国 OIDC        oidc.eks.ap-northeast-2.amazonaws.com/id/978D6D13181C50CF75450B933B976ADD
```

CFN 栈在 `infra/dr-korea/`,`01`–`18` 按序号部署。
**`15-korea-workloads.yaml` 是生成的,不要手改** —— 改 `scripts/gen_korea_workloads.py`。

---

## 六、addon:为什么只补了 1 个而不是 5 个

东京 5 个、韩国原先 1 个。「按需」的意思是**先测再补**:

| addon | 处置 | 依据(实测,不是估的) |
|---|---|---|
| `amazon-cloudwatch-observability` | ✅ 补 | 这个 demo 的主题;零节点时不起 pod,不花钱 |
| `aws-ebs-csi-driver` | ❌ 不补 | 扫清单的 `volumes`:只有 `configMap`,**无 PVC** |
| `eks-pod-identity-agent` | ❌ 不补 | `list-pod-identity-associations` 只有 1 个关联,且属于 `amazon-network-flow-monitor`;petsite 全走 IRSA |
| `aws-network-flow-monitoring-agent` | ❌ 不补 | 用户明确「流量监控不管」 |
| `aws-guardduty-agent` | — | 韩国**已经有**(账户级策略装的) |

授权走**节点角色**(这个 addon 的 `serviceAccountRoleArn` 实测是 `null`)。
韩国节点角色原先比东京正好少 `CloudWatchAgentServerPolicy`,补了这一条 ——
**刻意不多加**,东京也只有这 5 条。

---

## 七、判据纪律(判据错过 15 次,功能实现从未错)

这个比例本身就是信息:**难的不是把事情做对,是判断自己有没有做对。**

| 错法 | 正确做法 |
|---|---|
| 手抄中文标点写断言 | 用 `tests/zh_text.py` 的 `assert_contains`(全角/半角害过三次) |
| 文本匹配查代码结构 | 用 **AST** —— 文本匹配会撞上**解释这件事的注释** |
| `assert "X" in src` | 会被 `_X` 误配 → 用 AST 查元组成员 |
| 断言跨行 | 文件里那句话被换行拆开了 → 匹配不跨行的片段 |
| 查「函数存在」 | 查 `main()` 里的 **Call**;但**源码检查证明不了行为** → 补行为测试并写下边界 |
| 按 `phase != Running` 过滤 pod | 按**就绪数**(`Running` 但未就绪会被漏掉) |
| `length(Parameters)` 数分页结果 | **按页各算一次** → 取 `Parameters[].Name` 再数行 |
| 用输出文本判断「不存在」 | **用退出码**(错误在 stderr) |
| 按 `sed 's/^/  /'` 的**显示**缩进数 YAML 层级 | 看 `repr`,别看加过前缀的显示 |
| 读不出值就下结论 | **先怀疑自己的查询** —— JMESPath 写错让我以为切换花了 11 分钟(实际 57 秒) |

**反向验证的纪律**(踩过三次):

- 扰动脚本要 `assert 原串 in 内容`,否则「没扰动成功」与「门禁漏了」表现完全一样
- 要**数出现次数并全局替换**,只替第一处时门禁可能匹配到第二处
- **不要把扰动脚本输出重定向到 `/dev/null`**,会把失败藏起来
- 排查命令**别加 `2>/dev/null`、别 `tail` 截断** —— 真错误两次被我自己截掉

---

## 八、门禁

`tests/test_80` – `test_106`。相关的:

| 文件 | 守什么 |
|---|---|
| `test_98` / `test_99` | 演练清单不许被当成能用的配置;手册里的坏消息不许被删 |
| `test_100` | TGB 的 ARN 必须是韩国的;ALB 保持 internal |
| `test_101` | 生成器:SA 与 IRSA 映射同源、黑名单拷 pod spec、ConfigMap 引用 |
| `test_102` | SSM 参数六档;**D 档手工列表必须排在形状推断之前** |
| `test_103` | 密钥资源策略覆盖全部 7 个业务角色;额外消费者必须登记 |
| `test_104` | 环境变量兜底扫描;Lambda wrapper 与层成对 |
| `test_105` | **表的契约不只是主键**(GSI/属性定义/投影);carts 表不许「修好」 |
| `test_106` | 切换演练:回切事前写好、基线对照留着、**inconclusive 不许改成通过** |

跑全量:

```bash
NEPTUNE_ENDPOINT=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com \
REGION=ap-northeast-1 python3.11 -m pytest tests/ -q     # 必须 python3.11
```

CI 有**通过数下限 1040**。合并前查通过数,不只看「没报错」。

---

## 九、关停(命令只列出,**不要直接跑**)

销毁按**逆序**,`09` 那个栈在东京:

```bash
for s in dr-korea-addons dr-korea-stepfn dr-korea-backends dr-korea-alb \
         dr-korea-irsa-oidc dr-korea-ecr-repos dr-korea-temporal \
         dr-korea-eks dr-korea-aurora dr-korea-network; do
  aws cloudformation delete-stack --region ap-northeast-2 --stack-name $s
done
aws cloudformation delete-stack --region ap-northeast-1 --stack-name dr-korea-ecr-replication
```

⚠️ 三件事:

1. **韩国 Aurora 是全局集群成员** —— 删它之前要先从 `petsite-global` 移除
2. ECR 仓库与 DynamoDB 表是 `Retain`,**栈删了资源还在**,要单独删
3. 东京那个复制配置是 **registry 级**的,删掉会影响该账户在东京的所有复制行为
