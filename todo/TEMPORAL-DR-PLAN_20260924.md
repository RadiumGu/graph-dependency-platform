# DR plan 接入 Temporal + 韩国守夜灯灾备站点 —— 执行计划

**这份文件是 200 轮目标循环的工作依据。** 循环消息只是指针,细节都在这里。
当前 phase / next 由 `session_ledger_read` 给出,**它比本文件更权威**
(本文件记计划,台账记进展)。

## 用户的原始要求(逐条)

1. 先看本地更新的代码 ✅ 已完成
2. 下载 `https://github.com/RadiumGu/temporal-mcp` 到 `/home/ec2-user/works` ✅ 已完成
3. 验证 temporal-mcp 的功能,有问题就改
4. 把 temporal-mcp server 部署到 AgentCore
5. 改造 dr plan 生成模块,改为通过 temporal-mcp server 生成
6. 在韩国 region 创建 petsite 灾备站点,守夜灯模型:
   - EKS 集群内 node 平时不拉起,切换时才拉起
   - 数据库同步,但采用较小的实例
   - 只保留 petsite 运行的必要环境:EKS、数据库、AgentCore、DeepFlow 服务器
   - **Neptune 先不管**
   - **处理到容灾环境搭建时,先输出清单并等用户确认,确认后再部署**
   - **韩国 region 先不要暴露公网访问**

---

## 五个阶段(按真实依赖排序,前一阶段不通不要跳过)

### A. Temporal 服务端 —— 总前提

**✅ 方案已由用户定下(2026-09-24)**:**自建在韩国 region(ap-northeast-2)的一台 EC2 上。**
不用 Temporal Cloud。

这个选择在架构上是对的,值得记下理由:**编排者必须能在主站失效时存活。**
Temporal 要执行的正是「从 ap-northeast-1 切到 ap-northeast-2」这件事,
把它放在主站等于让灭火器和火在同一个房间。

**已查明的事实**(2026-09-24,不要重新勘察):

- `temporal-mcp` 是 TypeScript MCP server,~3600 行,28 个文件
- 依赖只有 `@modelcontextprotocol/sdk` + `zod` —— **它是个薄 HTTP 客户端**
- `src/client.ts:113` 读 `TEMPORAL_ADDRESS`,缺了就抛错;示例值
  `http://localhost:8080` 是 Temporal 的 **HTTP API**(不是 gRPC 7233);
  另读 `TEMPORAL_NAMESPACE`(默认 `default`)与 `TEMPORAL_API_KEY`
- **它自身既不含 Temporal 服务端,也不含 worker**
- 本机无 `temporal` CLI、无 `TEMPORAL_*` 环境变量
- 工具链就绪:node v22.12.0 / npm 10.9.0;`npm install` 已跑过,
  `build` / `typecheck` / 16 个 vitest 全通过

**韩国 region 现状(2026-09-24 实测,基本是空的)**:

    VPC        只有默认 vpc-5707343f (172.31.0.0/16)
    子网        4 个
    NAT        0 个          ← 注意
    IGW        1 个
    EC2        0 台
    EKS        0 个集群
    AgentCore  control plane 正常响应（返回空列表而非报错）→ **该 region 可用**

#### ⚠️ 「不暴露公网访问」与「装得上软件」的张力

「不暴露公网」指的是**入站**:无 public ALB、无公网 IP、安全组无 0.0.0.0/0 入站。
但装 Temporal 要下载二进制/容器镜像,**那是出站**。两条路:

| 方案 | 月成本(粗估) | 说明 |
|---|---|---|
| 私有子网 + NAT 网关(出站) | ~35-40 USD + 流量 | 入站零暴露,出站可用。装完可以拆掉 NAT |
| 私有子网 + VPC 接口端点 | ~22 USD(3 个端点) | 只够 SSM 管理,**不够拉镜像**;拉镜像还要 S3 + ECR 端点 |

**当前倾向:私有子网 + NAT 网关(仅出站)。** 它同时解决两件事 ——
SSM Session Manager 可用(我才能在无公网入站的前提下管理这台机器),
以及能拉 Temporal 镜像。装完后 NAT 可拆,届时机器仍可经 SSM 端点管理。

⚠️ **这是新增计费资源,创建前须与 E 的清单一并给用户确认。**

#### ⚠️ temporal-mcp 放哪个 region 决定了要不要跨 region 打通

Temporal 在 ap-northeast-2 的私有子网里,那么:

- **temporal-mcp 也部到 ap-northeast-2 的 AgentCore** → 同 region 同 VPC,
  无需跨 region 打通。且与「灾备站点要有 AgentCore」这条要求重合,
  **一份资源满足两个目的**。← 当前倾向
- temporal-mcp 部在 ap-northeast-1 → 需要 VPC 对等/Transit Gateway 跨 region,
  多一层故障面,而且主站挂了这条链路也可能受影响

**决定:temporal-mcp 部到 ap-northeast-2 的 AgentCore。**
理由同上:编排链路整体留在灾备侧才有意义。

#### worker 也必须落在这里

temporal-mcp 只能管理/查询/启动 workflow,**执行 DR 步骤要另有 Temporal worker**。
worker 需要对**两个 region**都有 AWS 操作权限(要能扩韩国的节点组、
也要能读/切主站的资源)。这是阶段 D 的核心设计问题,见下。

### B. 验证并修 temporal-mcp

⚠️ **那 16 个测试只覆盖 `src/client.ts` 一个文件**,`src/tools/` 下这些全无覆盖:

    workflows.ts            711 行
    schedules.ts            286 行
    workflow-rules.ts       190 行
    nexus-endpoints.ts      171 行
    workflow-history.ts     173 行
    activities.ts           141 行
    batch-operations.ts     128 行
    namespaces.ts           123 行
    worker-deployments.ts   111 行
    search-attributes.ts     78 行
    task-queues.ts           64 行
    cluster.ts               63 行

而且 16 个全是离线单测,不触达真实 Temporal API。
**「build 通过 + 测试全绿」证明不了任何一个工具真的能用。**

真实验证:对着活 Temporal 逐个工具实测,至少覆盖
`workflows` / `schedules` / `namespaces` / `task-queues`。
有问题就改,并为改过的地方补能抓到该问题的测试(反向验证过)。

### C. 部署 temporal-mcp 到 AgentCore(ap-northeast-2)

**已有直接先例**:ap-northeast-1 的 AgentCore 上已经跑着
`graph_dependency_mcp`(状态 READY)—— 本项目自己的 MCP server。
先去看它是怎么打包和注册的,packaging/托管模式可以照搬。

    aws bedrock-agentcore-control list-agent-runtimes --region ap-northeast-1
      → graph_dependency_mcp / WaggleAIOrdering / WaggleAIOrchestrator
        / WaggleAINutrition / WaggleAIConcierge   全部 READY

参考代码:

    graph_mcp/                       本项目的 MCP server（**Python**）
    infra/lambda/etl_agentcore/      AgentCore 的 ETL（含 Gateway target 处理）

⚠️ `graph_mcp` 是 Python 而 temporal-mcp 是 **Node** —— 打包形态可能不同,
先确认 AgentCore Runtime 对 Node 的支持方式(容器?还是有 Node 运行时?)。

---

## 🚦 每个阶段的部署都必须记录 —— 这是闸门,不是建议

用户明确要求:**部署的关键步骤要记录下来。**

落点:`docs/runbooks/deployment-record.md`(已建,并已挂进 `docs/README.md` 索引)。
那份手册里定义了每次部署必记的**七项**:

    ① 目标            ② 前置状态(可核对值)   ③ 实际执行的命令(逐条、照抄可跑)
    ④ 生效核实        ⑤ 失败过的做法+原因     ⑥ 回滚方式   ⑦ 日期

**规矩:一次部署没有记录,就不算完成。** 不要等全部做完再补记 ——
补记时失败过的做法和当时的前置状态已经想不起来了,而那两项恰恰最值钱。

④ 生效核实必须用**与部署不同的手段**。本项目实测过三次「命令成功但没生效」:

- `git pull` 在展示站目标机中止,而 md5 不变、服务仍 `active`
- botocore 版本旧,把 `GetGatewayTarget` 的 `http` 成员静默剥掉 —— 调用不报错
- cron 手动跑脚本能通,但调度链路(command 模式)在本宿主根本不可用

### D. 改造 dr-plan-generator 经 temporal-mcp 生成并保存计划

入口(`/home/ec2-user/works/graph-dependency-platform/dr-plan-generator`):

    planner/plan_generator.py      785 行   计划生成主流程
    planner/step_builder.py       1090 行   步骤构造
    planner/rollback_generator.py  122 行
    planner/preflight.py           468 行
    output/markdown_renderer.py    488 行
    output/json_renderer.py         52 行
    executor_strands.py            700 行   现有执行器
    tests/                                  既有门禁,不许弄坏

⚠️ **关键架构问题,动代码前先想清楚**:temporal-mcp 只能管理/查询/启动
workflow,**执行 DR 步骤需要另有 Temporal worker**。要回答:

- 谁来跑 worker(EKS pod?Lambda?EC2?)
- worker 怎么拿到执行 AWS 操作的权限(IRSA?实例角色?)
- 现有 `executor_strands.py` 与 Temporal 执行是替换还是并存
- 计划的「保存」形态:Temporal workflow 定义?Schedule?还是仅作为 payload 存档

### E. 韩国(ap-northeast-2)守夜灯灾备站点

守夜灯(pilot light)模型的具体含义:

- EKS 控制平面常开,**节点组期望容量 0**,切换时才扩容
- 数据库持续同步(跨 region 只读副本),但**实例规格小于主站**
- 只保留 petsite 运行的必要环境:EKS / 数据库 / AgentCore / DeepFlow
- **Neptune 不管**
- **不暴露公网访问** —— 无 public ALB、无 NAT 出站暴露面、无公网 IP

🚦 **硬闸门:部署前必须先输出完整清单给用户确认,然后停下等回复。**
清单至少包含:

    资源逐项(类型、规格、数量、所在 AZ/子网)
    月成本估算(分项 + 合计)
    公网暴露面(逐项说明为什么是零)
    与主站(ap-northeast-1)的差异点
    回滚/拆除方式
    切换时需要人工介入的步骤

**清单发出后本轮结束,不要擅自创建任何资源。**

---

## 纪律(本会话已被这些坑咬过)

- **动 `graph-dependency-platform` 任何文件前先 `git status`** —— 有并发会话在
  同一工作树提交,最近一次是几分钟前。分支 `main` 且**受保护,不直推**:
  开特性分支 + PR(上次是 [PR #3](https://github.com/RadiumGu/graph-dependency-platform/pull/3))
- 销毁类 AWS 操作(terminate/delete)**不执行**,只给命令让用户跑;stop 可以
- **不猜符号名/属性名/路径** —— 本会话为此错了 4 次:
  `nc.query_gremlin`(真名在 `neptune_client_base.neptune_query`)、
  `C.run_query`(真名 `C.gquery`)、`gquery(q, params)`(只接一个参数)、
  `SKILL_FILE` 的 `mcp/` 路径(改名成了 `graph_mcp/`)。先 grep 确认真名
- 同一函数多处改动**一次写完**,别增量叠加(叠过一次把文件改成 `SyntaxError`);
  每次编辑后立刻 `ast.parse` 验语法
- 新增门禁必须**反向验证**(注入已知缺陷确认变红);断言前剥掉注释与 docstring,
  否则会匹配到自己写的解释文字(已犯 5 次以上)
- 改完跑**全量** pytest(基线 1126 passed / 90 skipped / 0 failed),别只跑相关几个
- 判据错过 13 次以上、功能没写错过 —— 怀疑某个数字异常时,
  **先查「这个形状是不是已经被刻意处理过」**(契约词表、既有注释往往写着设计意图)。
  最近一次:我把 18 条 deepflow-dns 边当成「逃过过期机制」,
  而它们是契约里唯一的 `sparse_observation_sources`,被刻意排除
- **必须 python3.11**(`python3` 是 3.9)
- Neptune:`NEPTUNE_ENDPOINT=petsite-neptune.cluster-czbjnsviioad.ap-northeast-1.neptune.amazonaws.com`、`REGION=ap-northeast-1`
- 安全策略会拦这些形状:`git push` 目标带变量展开、单条命令混内联 Python 与 `aws` CLI、
  `aws configure list`、`aws lambda list-functions`、递归 grep 根目录。拆开写


## 缺口分析:「守夜灯」离「能接管」还差多少(2026-09-24 读活集群得出)

这一节不是推测,是从东京 `PetSite` 集群(1.35、公网 endpoint)读到的 18 个
Deployment 反推出来的。**它改变了任务范围的判断**,所以单独记一节。

### petsite 真正跑的东西

namespace `petadoptions` 里 7 个业务 Deployment:

| Deployment | 副本 | 端口 | ServiceAccount |
|---|---|---|---|
| `petsite-deployment` | 2 | 8080 | `petsite-sa` |
| `list-adoptions` | 2 | 80 | `list-adoptions-sa` |
| `pay-for-adoption` | 2 | 80 | `pay-for-adoption-sa` |
| `search-service` | 2 | 80 | `search-service-sa` |
| `pethistory-deployment` | 2 | 8080 | `pethistory-sa` |
| `petfood` | 2 | 8080 | `petfood-sa` |
| `traffic-generator` | 1 | 80 | `traffic-generator-sa` |

另有 11 个平台组件:`amazon-cloudwatch`(1)、`chaos-mesh`(3)、
`deepflow`(2:`prometheus-nfm`/`yace-nfm`)、`kube-system`(5:
`aws-load-balancer-controller` v3.0.0、`cluster-autoscaler` v1.34.0、
`coredns`、`ebs-csi-controller`、`metrics-server`)。

### 四个此前没被计入的障碍

**① 镜像仓库在要灾备的那个 region —— 这是灾备的致命点。**

6 个业务服务里 5 个用的是 CDK 的 asset 仓库,tag 是内容哈希:

```
926093770964.dkr.ecr.ap-northeast-1.amazonaws.com/
  cdk-hnb659fds-container-assets-926093770964-ap-northeast-1:<sha256>
```

只有 `pethistory` 用具名仓库(`pet-adoptions-history:latest`)。
**东京挂了就拉不到镜像**,而韩国侧现在有 0 个 ECR 仓库。
「依赖跨 region 拉取」在真灾难场景下等于没有方案。

**② 每个服务都用 IRSA,而 IRSA 绑的是集群的 OIDC provider。**

韩国集群的 OIDC issuer 与东京不同。那 7 个 `*-sa` 对应的 IAM 角色,
信任策略里只写了东京集群的 provider → **在韩国起来的 pod 拿不到凭据**。
这一点很容易漏,而且**只在真切换时才炸**:清单照抄过去、pod 能起来、
然后所有 AWS 调用 403。

**③ 配置在 region 内,不会随 Aurora 全局数据库过去。**

- SSM Parameter Store,`/petstore` 前缀(`searchapiurl`、`petlistadoptionsurl`、
  `paymentapiurl`、`pethistoryurl`、`rumscript`、`petfoodapiurl` …)
- Secrets Manager:`DatabaseSecret3B817195-…`,在 `ap-northeast-1`
- 这些都是 region 内资源。东京挂了,韩国侧读不到。

**④ 依赖面远超 EKS + DB。**

从环境变量里读出来的:DynamoDB 表(`ServicesEks2-ddbpetfoodfoods…`、
`…carts…`)、EventBridge bus(`ServicesEks2petfoodeventbus…`)、
SQS 队列、S3 桶、API Gateway(`9dw5r2dqlb.execute-api.ap-northeast-1`)、
Bedrock AgentCore runtime ARN(`/petstore/agent/waggleairuntimearn`)。

### 结论

当前的守夜灯站点(EKS 空集群 + Aurora 从集群 + Temporal + AgentCore)
**基础设施层是通的**,扩容与数据库提升都已有可核实的执行路径。
但**应用层接管还不成立** —— 上面四条每一条都足以让切换后的 petsite 起不来。

而且 petsite 的部署清单**不在本仓库**(本仓库只有
`infra/k8s/streamlit-demo.yaml`),由另一个 CDK 项目管
(access entry 里可见 `ServicesEks2-petsiteCreationRole…`)。
所以「准备 petsite 的 k8s 清单」不是在这个仓库里写几个 YAML 能完成的事,
它要么改那个 CDK 项目,要么从活集群导出再改造。

**这些都需要用户决定范围**,所以到此为止,不自行扩大。
## 退出条件

五个阶段全部完成并验证过,或卡在必须用户决定的事上
(E 的清单确认、花钱、开公网、销毁类操作)。两种情况都调 `autonudge_stop` 说明原因。
`max_cycles=200` 是防跑飞的上限,**不是成功标志**。
